from fastapi import FastAPI, HTTPException, Query, Body, Request, Response, Depends, Cookie
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import yaml
import subprocess
import os
import requests
from typing import Dict, Optional, List
import logging
import json
from datetime import datetime, timedelta
import time
from functools import lru_cache
import traceback
import psutil
import GPUtil
import asyncio
import uvicorn
import socket
import netifaces
import hashlib
import secrets


# Global variable to store previous network stats for bandwidth calculation
previous_network_stats = {}
previous_network_timestamp = 0

channel_monitors: Dict[str, asyncio.Task] = {}
channel_last_active: Dict[str, float] = {}

# Session storage (in-memory)
sessions: Dict[str, dict] = {}
SESSION_TIMEOUT_HOURS = 24
AUTH_CONFIG_FILE = "/root/rist/auth.yaml"

# Logging Configuration with rotation
from logging.handlers import RotatingFileHandler

log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# File handler with rotation - max 10MB, keep 3 backups
file_handler = RotatingFileHandler(
    "/var/log/rist-api.log",
    maxBytes=10*1024*1024,  # 10MB
    backupCount=3
)
file_handler.setFormatter(log_formatter)
file_handler.setLevel(logging.INFO)

# Console handler - only warnings and above
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
console_handler.setLevel(logging.WARNING)

# Configure root logger
logging.basicConfig(level=logging.INFO, handlers=[file_handler, console_handler])
logger = logging.getLogger(__name__)

app = FastAPI(title="RIST Receiver API")
CONFIG_FILE = "receiver_config.yaml"
SERVICE_DIR = "/etc/systemd/system"


# =============================================================================
# Authentication Functions
# =============================================================================

def hash_password(password: str) -> str:
    """Hash a password using SHA-256 with salt"""
    salt = "rist_receiver_salt_2024"  # Static salt for simplicity
    return hashlib.sha256(f"{salt}{password}".encode()).hexdigest()


def load_auth_config() -> dict:
    """Load authentication configuration"""
    default_config = {
        "username": "admin",
        "password_hash": hash_password("admin"),  # Default password: admin
        "initialized": False
    }
    
    if not os.path.exists(AUTH_CONFIG_FILE):
        save_auth_config(default_config)
        return default_config
    
    try:
        with open(AUTH_CONFIG_FILE, 'r') as f:
            return yaml.safe_load(f) or default_config
    except Exception as e:
        logger.error(f"Failed to load auth config: {e}")
        return default_config


def save_auth_config(config: dict):
    """Save authentication configuration"""
    try:
        os.makedirs(os.path.dirname(AUTH_CONFIG_FILE), exist_ok=True)
        with open(AUTH_CONFIG_FILE, 'w') as f:
            yaml.safe_dump(config, f)
        os.chmod(AUTH_CONFIG_FILE, 0o600)  # Restrict permissions
    except Exception as e:
        logger.error(f"Failed to save auth config: {e}")


def create_session(username: str) -> str:
    """Create a new session and return session token"""
    token = secrets.token_urlsafe(32)
    sessions[token] = {
        "username": username,
        "created": datetime.now(),
        "expires": datetime.now() + timedelta(hours=SESSION_TIMEOUT_HOURS)
    }
    return token


def validate_session(token: str) -> Optional[dict]:
    """Validate a session token"""
    if not token or token not in sessions:
        return None
    
    session = sessions[token]
    if datetime.now() > session["expires"]:
        del sessions[token]
        return None
    
    return session


def cleanup_expired_sessions():
    """Remove expired sessions"""
    now = datetime.now()
    expired = [token for token, session in sessions.items() if now > session["expires"]]
    for token in expired:
        del sessions[token]


# Dependency to check authentication
async def require_auth(request: Request):
    """Dependency that requires authentication for protected endpoints"""
    # Get session token from cookie
    token = request.cookies.get("session_token")
    
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    session = validate_session(token)
    if not session:
        raise HTTPException(status_code=401, detail="Session expired")
    
    return session


# Public endpoints that don't require auth
PUBLIC_PATHS = ["/auth/login", "/auth/status", "/auth/logout", "/docs", "/openapi.json", "/"]


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Authentication Middleware
from starlette.middleware.base import BaseHTTPMiddleware

class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Skip auth for public paths
        path = request.url.path
        if any(path.startswith(p) for p in PUBLIC_PATHS):
            return await call_next(request)
        
        # Check for valid session
        token = request.cookies.get("session_token")
        if not token or not validate_session(token):
            return JSONResponse(
                status_code=401,
                content={"detail": "Not authenticated"}
            )
        
        # Clean up expired sessions periodically
        cleanup_expired_sessions()
        
        return await call_next(request)

app.add_middleware(AuthMiddleware)


# Metrics Caching
metrics_cache = {}
system_metrics_cache = {}

class RistReceiverSettings(BaseModel):
    profile: int = Field(1, description="RIST profile")
    virt_src_port: Optional[int] = None
    buffer: int = Field(500, description="Buffer size in ms")
    encryption_type: Optional[int] = None
    secret: Optional[str] = None

class Channel(BaseModel):
    name: str
    enabled: bool = True
    input: str
    output: str
    settings: RistReceiverSettings
    metrics_port: Optional[int] = None
    status: str = "stopped"
    process_id: Optional[int] = None
    last_error: Optional[str] = None

# Global variable to store previous network stats for bandwidth calculation
network_stats_history = {}

def calculate_network_bandwidth(current_stats, current_timestamp):
    """
    Calculate network bandwidth per second with improved tracking
    """
    global network_stats_history
    
    # Initialize history if empty
    if not network_stats_history:
        for interface, stats in current_stats.items():
            network_stats_history[interface] = {
                'timestamps': [current_timestamp],
                'bytes_sent': [stats['bytes_sent']],
                'bytes_recv': [stats['bytes_recv']]
            }
        
        return {
            interface: {
                'bytes_sent': stats['bytes_sent'],
                'bytes_recv': stats['bytes_recv'],
                'bytes_sent_per_sec': 0,
                'bytes_recv_per_sec': 0
            } for interface, stats in current_stats.items()
        }
    
    bandwidth_stats = {}
    
    for interface, current_interface_stats in current_stats.items():
        # Retrieve or initialize interface history
        if interface not in network_stats_history:
            network_stats_history[interface] = {
                'timestamps': [current_timestamp],
                'bytes_sent': [current_interface_stats['bytes_sent']],
                'bytes_recv': [current_interface_stats['bytes_recv']]
            }
            bandwidth_stats[interface] = {
                'bytes_sent': current_interface_stats['bytes_sent'],
                'bytes_recv': current_interface_stats['bytes_recv'],
                'bytes_sent_per_sec': 0,
                'bytes_recv_per_sec': 0
            }
            continue
        
        # Add current stats to history
        history = network_stats_history[interface]
        history['timestamps'].append(current_timestamp)
        history['bytes_sent'].append(current_interface_stats['bytes_sent'])
        history['bytes_recv'].append(current_interface_stats['bytes_recv'])
        
        # Keep only recent measurements (last 5 entries)
        history['timestamps'] = history['timestamps'][-5:]
        history['bytes_sent'] = history['bytes_sent'][-5:]
        history['bytes_recv'] = history['bytes_recv'][-5:]
        
        # Calculate bandwidth
        if len(history['timestamps']) > 1:
            # Use the first and last measurements for bandwidth calculation
            time_diff = history['timestamps'][-1] - history['timestamps'][0]
            bytes_sent_diff = history['bytes_sent'][-1] - history['bytes_sent'][0]
            bytes_recv_diff = history['bytes_recv'][-1] - history['bytes_recv'][0]
            
            # Prevent divide by zero and negative values
            bytes_sent_per_sec = max(0, int(bytes_sent_diff / max(time_diff, 1)))
            bytes_recv_per_sec = max(0, int(bytes_recv_diff / max(time_diff, 1)))
        else:
            bytes_sent_per_sec = 0
            bytes_recv_per_sec = 0
        
        bandwidth_stats[interface] = {
            'bytes_sent': current_interface_stats['bytes_sent'],
            'bytes_recv': current_interface_stats['bytes_recv'],
            'bytes_sent_per_sec': bytes_sent_per_sec,
            'bytes_recv_per_sec': bytes_recv_per_sec
        }
    
    return bandwidth_stats

def parse_prometheus_metrics(metrics_text: str) -> dict:
    """Parse Prometheus metrics text into structured data"""
    metrics = {
        "quality": 100.0,
        "peers": 0,
        "bandwidth_bps": 0,
        "retry_bandwidth_bps": 0,
        "packets": {
            "sent": 0,
            "received": 0,
            "missing": 0,
            "reordered": 0,
            "recovered": 0,
            "recovered_one_retry": 0,
            "lost": 0
        },
        "timing": {
            "min_iat": 0,
            "cur_iat": 0,
            "max_iat": 0,
            "rtt": 0
        }
    }
    
    if isinstance(metrics_text, str):
        metrics_text = metrics_text.replace('\\n', '\n').replace('\\"', '"')
    
    try:
        for line in metrics_text.split('\n'):
            if line.startswith('#') or not line.strip():
                continue
                
            if '{' in line:
                name, rest = line.split('{', 1)
                name = name.strip()
                labels, value_part = rest.rsplit('}', 1)
                value = float(value_part.strip())
                
                if name == 'rist_client_flow_peers':
                    metrics['peers'] = int(value)
                elif name == 'rist_client_flow_bandwidth_bps':
                    metrics['bandwidth_bps'] = value
                elif name == 'rist_client_flow_retry_bandwidth_bps':
                    metrics['retry_bandwidth_bps'] = value
                elif name == 'rist_client_flow_sent_packets_total':
                    metrics['packets']['sent'] = int(value)
                elif name == 'rist_client_flow_received_packets_total':
                    metrics['packets']['received'] = int(value)
                elif name == 'rist_client_flow_missing_packets_total':
                    metrics['packets']['missing'] = int(value)
                elif name == 'rist_client_flow_reordered_packets_total':
                    metrics['packets']['reordered'] = int(value)
                elif name == 'rist_client_flow_recovered_packets_total':
                    metrics['packets']['recovered'] = int(value)
                elif name == 'rist_client_flow_recovered_one_retry_packets_total':
                    metrics['packets']['recovered_one_retry'] = int(value)
                elif name == 'rist_client_flow_lost_packets_total':
                    metrics['packets']['lost'] = int(value)
                elif name == 'rist_client_flow_min_iat_seconds':
                    metrics['timing']['min_iat'] = value
                elif name == 'rist_client_flow_cur_iat_seconds':
                    metrics['timing']['cur_iat'] = value
                elif name == 'rist_client_flow_max_iat_seconds':
                    metrics['timing']['max_iat'] = value
                elif name == 'rist_client_flow_rtt_seconds':
                    metrics['timing']['rtt'] = value
                elif name == 'rist_client_flow_quality':
                    metrics['quality'] = value

    except Exception as e:
        logger.error(f"Error parsing metrics: {e}")
        logger.error(f"Metrics text: {metrics_text}")
    
    return metrics

def get_system_health_metrics(cache_timeout: int = 1):
    """
    Retrieve system health metrics with a simple caching mechanism
    """
    current_time = time.time()
    
    # Check if we have a cached result and it's fresh
    if (system_metrics_cache and 
        current_time - system_metrics_cache.get('timestamp', 0) < cache_timeout):
        return system_metrics_cache['data']
    
    try:
        # CPU Information
        cpu_percent = psutil.cpu_percent(interval=1)
        cpu_temps = psutil.sensors_temperatures().get('coretemp', [])
        cpu_temp = cpu_temps[0].current if cpu_temps else 0
        cpu_cores = [{'core': i, 'usage': percent} for i, percent in enumerate(psutil.cpu_percent(interval=0.1, percpu=True))]

        # Memory Information
        memory = psutil.virtual_memory()

        # Disk Information
        disk = psutil.disk_usage('/')

        # Network Information
        network_stats = {}
        for interface, stats in psutil.net_io_counters(pernic=True).items():
            network_stats[interface] = {
                'bytes_sent': stats.bytes_sent,
                'bytes_recv': stats.bytes_recv
            }
        
        # Calculate network bandwidth
        bandwidth_stats = calculate_network_bandwidth(network_stats, current_time)

        # GPU Information (Optional, requires GPUtil)
        gpu_info = []
        try:
            gpus = GPUtil.getGPUs()
            for gpu in gpus:
                gpu_info.append({
                    'name': gpu.name,
                    'load': gpu.load * 100,
                    'memory_used': gpu.memoryUsed,
                    'memory_total': gpu.memoryTotal,
                    'temperature': gpu.temperature
                })
        except Exception as gpu_error:
            logger.warning(f"GPU metrics retrieval failed: {gpu_error}")

        # Construct metrics dictionary
        metrics = {
            'cpu': {
                'average': cpu_percent,
                'cores': cpu_cores
            },
            'memory': {
                'total': memory.total,
                'used': memory.used,
                'used_percent': memory.percent
            },
            'disk': {
                'total': disk.total,
                'used': disk.used,
                'used_percent': disk.percent
            },
            'temperature': round(cpu_temp, 1),
            'network': bandwidth_stats,
            'gpu': gpu_info
        }
        
        # Cache the result
        system_metrics_cache.update({
            'timestamp': current_time,
            'data': metrics
        })
        
        return metrics
    
    except Exception as e:
        logger.error(f"Error fetching system health metrics: {e}")
        logger.error(traceback.format_exc())
        return {}

def get_cached_metrics(channel_id: str, metrics_port: int, cache_timeout: int = 1):
    """
    Retrieve metrics with a simple caching mechanism
    """
    current_time = time.time()
    
    # Check if we have a cached result and it's fresh
    if (channel_id in metrics_cache and 
        current_time - metrics_cache[channel_id]['timestamp'] < cache_timeout):
        return metrics_cache[channel_id]['data']
    
    # Fetch new metrics
    try:
        logger.debug(f"Fetching metrics for channel {channel_id} from port {metrics_port}")
        response = requests.get(f"http://localhost:{metrics_port}/metrics", timeout=2)
        
        if response.status_code != 200:
            logger.error(f"Metrics endpoint returned status {response.status_code}")
            return parse_prometheus_metrics("")
        
        metrics = parse_prometheus_metrics(response.text)
        
        # Cache the result
        metrics_cache[channel_id] = {
            'timestamp': current_time,
            'data': metrics
        }
        
        return metrics
    
    except requests.RequestException as e:
        logger.error(f"Error fetching metrics for channel {channel_id}: {e}")
        return parse_prometheus_metrics("")

def reload_systemd():
    """Reload systemd configuration"""
    try:
        subprocess.run(["systemctl", "daemon-reload"], check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to reload systemd: {e.stderr.decode()}")
        raise HTTPException(status_code=500, detail="Failed to reload systemd")

def generate_service_file(channel_id: str, channel: Dict):
    """Generate separate systemd service files for RIST and FFmpeg"""
    input_url = channel["input"]
    if channel["settings"].get("virt_src_port"):
        input_url += f"?virt-dst-port={channel['settings']['virt_src_port']}"

    log_port = channel['metrics_port'] + 1000
    stream_path = f"/var/www/html/content/{channel['name']}"
    
    # RIST service components
    rist_cmd_parts = [
        "/usr/local/bin/ristreceiver",
        f"-p {channel['settings']['profile']}",
        f"-i '{input_url}'",
        f"-o '{channel['output']}'",
        "-v 2",
        "-M",
        "--metrics-http",
        f"--metrics-port={channel['metrics_port']}",
        "-S 1000",
        f"-r localhost:{log_port}"
    ]

    # Add optional parameters
    if channel["settings"].get("buffer"):
        rist_cmd_parts.append(f"-b {channel['settings']['buffer']}")
    
    if channel["settings"].get("encryption_type"):
        rist_cmd_parts.append(f"-e {channel['settings']['encryption_type']}")
    
    if channel["settings"].get("secret"):
        rist_cmd_parts.append(f"-s {channel['settings']['secret']}")

    # RIST Service
    rist_service = f"""[Unit]
Description=RIST Channel {channel['name']}
After=network.target

[Service]
Type=simple
ExecStart={' '.join(rist_cmd_parts)}
Restart=always
RestartSec=3


[Install]
WantedBy=multi-user.target
"""

    # FFmpeg Service for diagnostic preview
    ffmpeg_service = f"""[Unit]
Description=FFmpeg HLS for Channel {channel['name']}
After=rist-channel-{channel_id}.service
BindsTo=rist-channel-{channel_id}.service

[Service]
Type=simple
ExecStartPre=/bin/bash -c "mkdir -p {stream_path} && rm -f {stream_path}/*.ts {stream_path}/*.m3u8"
ExecStart=ffmpeg -i {channel['output']} -c copy -hls_time 5 -hls_list_size 5 -hls_flags delete_segments {stream_path}/playlist.m3u8
ExecStopPost=/bin/bash -c "rm -f {stream_path}/*.ts {stream_path}/*.m3u8"
Restart=always
RestartSec=3


[Install]
WantedBy=multi-user.target
"""

    os.makedirs("/var/log/ristreceiver", exist_ok=True)

    # Write RIST service
    rist_file = f"{SERVICE_DIR}/rist-channel-{channel_id}.service"
    try:
        with open(rist_file, "w") as f:
            f.write(rist_service)
    except Exception as e:
        logger.error(f"Failed to write RIST service file: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to create RIST service file")

    # Write FFmpeg service
    ffmpeg_file = f"{SERVICE_DIR}/ffmpeg-{channel_id}.service"
    try:
        with open(ffmpeg_file, "w") as f:
            f.write(ffmpeg_service)
    except Exception as e:
        logger.error(f"Failed to write FFmpeg service file: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to create FFmpeg service file")

    subprocess.run(["systemctl", "daemon-reload"], check=True)
    
    if channel['enabled']:
        subprocess.run(["systemctl", "enable", f"rist-channel-{channel_id}.service"], check=True)
        


def get_channel_status(channel_id: str) -> str:
    """Get the status of a channel's systemd service"""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", f"rist-channel-{channel_id}.service"],
            capture_output=True,
            text=True
        )
        return "running" if result.stdout.strip() == "active" else "stopped"
    except Exception:
        return "error"

def get_next_metrics_port(config):
    """Find the next available metrics port"""
    max_port = 9200
    for channel in config["channels"].values():
        if channel.get("metrics_port", 0) > max_port:
            max_port = channel["metrics_port"]
    return max_port + 1

def load_config():
    """Load configuration from YAML file"""
    if not os.path.exists(CONFIG_FILE):
        return {"channels": {}}
    with open(CONFIG_FILE, 'r') as f:
        return yaml.safe_load(f)



def save_config(config):
    """Save configuration to YAML file"""
    with open(CONFIG_FILE, 'w') as f:
        yaml.dump(config, f)


# =============================================================================
# Authentication Endpoints (Public)
# =============================================================================

class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@app.get("/auth/status")
def auth_status(request: Request):
    """Check if user is authenticated and if default password needs to be changed"""
    token = request.cookies.get("session_token")
    session = validate_session(token) if token else None
    
    auth_config = load_auth_config()
    
    return {
        "authenticated": session is not None,
        "username": session["username"] if session else None,
        "needs_password_change": not auth_config.get("initialized", False)
    }


@app.post("/auth/login")
def login(login_req: LoginRequest, response: Response):
    """Login and create session"""
    auth_config = load_auth_config()
    
    # Check credentials
    if login_req.username != auth_config["username"]:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    if hash_password(login_req.password) != auth_config["password_hash"]:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    # Create session
    token = create_session(login_req.username)
    
    # Set cookie
    response.set_cookie(
        key="session_token",
        value=token,
        httponly=True,
        max_age=SESSION_TIMEOUT_HOURS * 3600,
        samesite="lax"
    )
    
    logger.info(f"User {login_req.username} logged in")
    
    return {
        "status": "success",
        "message": "Login successful",
        "needs_password_change": not auth_config.get("initialized", False)
    }


@app.post("/auth/logout")
def logout(request: Request, response: Response):
    """Logout and destroy session"""
    token = request.cookies.get("session_token")
    
    if token and token in sessions:
        del sessions[token]
    
    response.delete_cookie("session_token")
    
    return {"status": "success", "message": "Logged out"}


@app.post("/auth/change-password")
def change_password(request: Request, pwd_req: ChangePasswordRequest):
    """Change password (requires authentication)"""
    token = request.cookies.get("session_token")
    session = validate_session(token)
    
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    auth_config = load_auth_config()
    
    # Verify current password
    if hash_password(pwd_req.current_password) != auth_config["password_hash"]:
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    
    # Validate new password
    if len(pwd_req.new_password) < 4:
        raise HTTPException(status_code=400, detail="Password must be at least 4 characters")
    
    # Update password
    auth_config["password_hash"] = hash_password(pwd_req.new_password)
    auth_config["initialized"] = True
    save_auth_config(auth_config)
    
    logger.info(f"Password changed for user {session['username']}")
    
    return {"status": "success", "message": "Password changed successfully"}


@app.on_event("startup")
def startup_event():
    # Initialize auth config on startup
    load_auth_config()
    
    @app.get("/")
    def wait_for_startup():
        return {"status": "ready"}

    import threading
    import time
    import requests

    def perform_keepalive():
        # Wait a bit to ensure server is fully up
        time.sleep(2)
        
        config = load_config()
        for channel_id in config["channels"]:
            try:
                response = requests.post(f"http://localhost:5000/channels/{channel_id}/keepalive")
                print(f"Keepalive for {channel_id}: {response.status_code}")
            except Exception as e:
                print(f"Keepalive failed for channel {channel_id}: {str(e)}")

    # Run keepalive in a separate thread
    threading.Thread(target=perform_keepalive, daemon=True).start()


@app.get("/system/hostname")
def get_system_hostname():
    """Get the system hostname"""
    return {"hostname": socket.gethostname()}


@app.get("/system/network-interfaces")
def get_network_interfaces():
    """Get detailed information about all network interfaces"""
    import netifaces
    
    interfaces = []
    
    for iface in netifaces.interfaces():
        iface_info = {
            "name": iface,
            "status": "down",
            "ipv4": [],
            "ipv6": [],
            "mac": None,
            "speed": None,
            "mtu": None
        }
        
        try:
            # Get addresses
            addrs = netifaces.ifaddresses(iface)
            
            # IPv4 addresses
            if netifaces.AF_INET in addrs:
                for addr in addrs[netifaces.AF_INET]:
                    iface_info["ipv4"].append({
                        "address": addr.get("addr"),
                        "netmask": addr.get("netmask"),
                        "broadcast": addr.get("broadcast")
                    })
            
            # IPv6 addresses
            if netifaces.AF_INET6 in addrs:
                for addr in addrs[netifaces.AF_INET6]:
                    # Skip link-local addresses for cleaner display
                    if not addr.get("addr", "").startswith("fe80"):
                        iface_info["ipv6"].append({
                            "address": addr.get("addr"),
                            "netmask": addr.get("netmask")
                        })
            
            # MAC address
            if netifaces.AF_LINK in addrs:
                for addr in addrs[netifaces.AF_LINK]:
                    if addr.get("addr"):
                        iface_info["mac"] = addr.get("addr")
                        break
            
            # Check if interface is up using /sys/class/net
            try:
                with open(f"/sys/class/net/{iface}/operstate", "r") as f:
                    state = f.read().strip()
                    # "up" = up, "unknown" = often used by WireGuard/tun interfaces when active
                    if state == "up":
                        iface_info["status"] = "up"
                    elif state == "unknown":
                        # For WireGuard/tunnel interfaces, check if they have an IP
                        if iface_info["ipv4"] or iface_info["ipv6"]:
                            iface_info["status"] = "up"
                        else:
                            iface_info["status"] = "down"
                    else:
                        iface_info["status"] = "down"
            except:
                # If we have an IP, assume it's up
                if iface_info["ipv4"] or iface_info["ipv6"]:
                    iface_info["status"] = "up"
            
            # Get speed (only for physical interfaces)
            try:
                with open(f"/sys/class/net/{iface}/speed", "r") as f:
                    speed = int(f.read().strip())
                    if speed > 0:
                        iface_info["speed"] = speed  # in Mbps
            except:
                pass
            
            # Get MTU
            try:
                with open(f"/sys/class/net/{iface}/mtu", "r") as f:
                    iface_info["mtu"] = int(f.read().strip())
            except:
                pass
                
        except Exception as e:
            logger.error(f"Error getting interface {iface} info: {e}")
        
        interfaces.append(iface_info)
    
    # Sort: physical interfaces first, then virtual, loopback last
    def sort_key(iface):
        name = iface["name"]
        if name == "lo":
            return (3, name)
        elif name.startswith(("eth", "en", "eno", "ens")):
            return (0, name)
        elif name.startswith("wg"):
            return (1, name)
        else:
            return (2, name)
    
    interfaces.sort(key=sort_key)
    
    return {"interfaces": interfaces}


@app.get("/system/wireguard")
def get_wireguard_config():
    """Get WireGuard configuration and status"""
    config_path = "/etc/wireguard/wg0.conf"
    
    # Get config file contents
    config_content = ""
    config_exists = False
    if os.path.exists(config_path):
        config_exists = True
        try:
            with open(config_path, "r") as f:
                config_content = f.read()
        except Exception as e:
            logger.error(f"Failed to read WireGuard config: {e}")
    
    # Get service status
    service_status = "inactive"
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "wg-quick@wg0"],
            capture_output=True,
            text=True
        )
        service_status = result.stdout.strip()
    except:
        pass
    
    # Get service enabled status
    service_enabled = False
    try:
        result = subprocess.run(
            ["systemctl", "is-enabled", "wg-quick@wg0"],
            capture_output=True,
            text=True
        )
        service_enabled = result.stdout.strip() == "enabled"
    except:
        pass
    
    # Get WireGuard interface status if active
    wg_status = None
    if service_status == "active":
        try:
            result = subprocess.run(
                ["wg", "show", "wg0"],
                capture_output=True,
                text=True
            )
            if result.returncode == 0:
                wg_status = result.stdout
        except:
            pass
    
    return {
        "config_exists": config_exists,
        "config_content": config_content,
        "service_status": service_status,
        "service_enabled": service_enabled,
        "wg_status": wg_status
    }


@app.post("/system/wireguard")
def save_wireguard_config(config: dict = Body(...)):
    """Save WireGuard configuration and restart service"""
    config_path = "/etc/wireguard/wg0.conf"
    config_content = config.get("config_content", "")
    
    if not config_content.strip():
        raise HTTPException(status_code=400, detail="Configuration cannot be empty")
    
    # Basic validation - check for required sections
    if "[Interface]" not in config_content:
        raise HTTPException(status_code=400, detail="Invalid config: missing [Interface] section")
    
    try:
        # Ensure directory exists
        os.makedirs("/etc/wireguard", exist_ok=True)
        
        # Stop WireGuard if running
        subprocess.run(["systemctl", "stop", "wg-quick@wg0"], capture_output=True)
        
        # Write config file
        with open(config_path, "w") as f:
            f.write(config_content)
        
        # Set proper permissions (important for WireGuard)
        os.chmod(config_path, 0o600)
        
        # Enable and start WireGuard
        subprocess.run(["systemctl", "enable", "wg-quick@wg0"], check=True)
        subprocess.run(["systemctl", "start", "wg-quick@wg0"], check=True)
        
        logger.info("WireGuard configuration saved and service restarted")
        
        return {
            "status": "success",
            "message": "WireGuard configuration saved and service started"
        }
        
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to restart WireGuard: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to start WireGuard service: {e.stderr}")
    except Exception as e:
        logger.error(f"Failed to save WireGuard config: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to save configuration: {str(e)}")


@app.get("/channels")
def get_channels():
    """Retrieve all channels with their current status"""
    config = load_config()
    for channel_id in config["channels"]:
        config["channels"][channel_id]["status"] = get_channel_status(channel_id)
    return config["channels"]

@app.get("/channels/next")
def get_next_channel_info():
    """Get information for the next channel to be created"""
    config = load_config()
    next_metrics_port = get_next_metrics_port(config)
    max_num = 0
    for channel_id in config["channels"].keys():
        if channel_id.startswith('channel'):
            try:
                num = int(channel_id.replace('channel', ''))
                if num > max_num:
                    max_num = num
            except ValueError:
                continue
    return {
        "channel_id": f"channel{max_num + 1}",
        "metrics_port": next_metrics_port
    }

@app.get("/channels/{channel_id}")
def get_channel(channel_id: str):
    """Retrieve details of a specific channel"""
    config = load_config()
    if channel_id not in config["channels"]:
        raise HTTPException(status_code=404)
    channel = config["channels"][channel_id]
    channel["status"] = get_channel_status(channel_id)
    return channel

@app.post("/channels/{channel_id}")
def create_channel(channel_id: str, channel: Channel):
    """Create a new channel"""
    config = load_config()
    if channel_id in config["channels"]:
        raise HTTPException(status_code=400)
    
    if not channel.metrics_port:
        channel.metrics_port = get_next_metrics_port(config)
    
    channel_dict = channel.dict()
    config["channels"][channel_id] = channel_dict
    save_config(config)
    
    generate_service_file(channel_id, channel_dict)
    return channel_dict

@app.put("/channels/{channel_id}")
def update_channel(channel_id: str, channel: Channel):
    """Update an existing channel"""
    config = load_config()
    if channel_id not in config["channels"]:
        raise HTTPException(status_code=404)

    logger.info(f"Starting channel update for {channel_id}")
    
    channel_dict = channel.dict()
    config["channels"][channel_id] = channel_dict
    save_config(config)
    
    generate_service_file(channel_id, channel_dict)
    
    if get_channel_status(channel_id) == "running":
        try:
            subprocess.run(["systemctl", "restart", f"rist-channel-{channel_id}.service"], check=True)
            channel_keepalive(channel_id)  # This will start FFmpeg if needed
        except subprocess.CalledProcessError as e:
            raise HTTPException(status_code=500, 
                          detail=f"Failed to restart services: {e.stderr}")
    
    return channel_dict

@app.put("/channels/{channel_id}/start")
def start_channel(channel_id: str):
    """Start RIST and FFmpeg services"""
    config = load_config()
    if channel_id not in config["channels"]:
        raise HTTPException(status_code=404)
    
    try:
        # Enable service so it auto-starts on reboot
        subprocess.run(["systemctl", "enable", f"rist-channel-{channel_id}.service"], check=True)
        subprocess.run(["systemctl", "start", f"rist-channel-{channel_id}.service"], check=True)
        channel_keepalive(channel_id)  # This will start FFmpeg if needed
        return {"status": "started"}
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"Failed to start services: {e.stderr}")

@app.put("/channels/{channel_id}/stop")
def stop_channel(channel_id: str):
    """Stop RIST and FFmpeg services"""
    config = load_config()
    if channel_id not in config["channels"]:
        raise HTTPException(status_code=404)
    
    try:
        # FFmpeg will stop automatically due to BindsTo
        subprocess.run(["systemctl", "stop", f"rist-channel-{channel_id}.service"], check=True)
        # Disable service so it won't auto-start on reboot
        subprocess.run(["systemctl", "disable", f"rist-channel-{channel_id}.service"], check=True)
        return {"status": "stopped"}
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"Failed to stop services: {e.stderr}")

@app.delete("/channels/{channel_id}")
def delete_channel(channel_id: str):
    """Delete a channel and its services"""
    config = load_config()
    if channel_id not in config["channels"]:
        raise HTTPException(status_code=404)
    
    try:
        # Stop and disable both services
        for service in [f"rist-channel-{channel_id}.service", f"ffmpeg-{channel_id}.service"]:
            try:
                subprocess.run(["systemctl", "stop", service], check=True)
                subprocess.run(["systemctl", "disable", service], check=True)
                os.remove(f"{SERVICE_DIR}/{service}")
            except Exception as e:
                logger.error(f"Failed to remove service {service}: {str(e)}")
        
        del config["channels"][channel_id]
        save_config(config)
        return {"status": "deleted"}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to delete channel")


async def stop_ffmpeg(channel_id: str):
    """Stop FFmpeg service for a channel"""
    try:
        subprocess.run(["systemctl", "stop", f"ffmpeg-{channel_id}.service"], check=True)
        logger.info(f"Stopped FFmpeg for channel {channel_id}")
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to stop FFmpeg for channel {channel_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to stop FFmpeg service: {e.stderr}")

async def start_ffmpeg(channel_id: str):
    """Start FFmpeg service for a channel"""
    try:
        subprocess.run(["systemctl", "start", f"ffmpeg-{channel_id}.service"], check=True)
        logger.info(f"Started FFmpeg for channel {channel_id}")
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to start FFmpeg for channel {channel_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to start FFmpeg service: {e.stderr}")

def is_ffmpeg_running(channel_id: str) -> bool:
    """Check if FFmpeg service is running"""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", f"ffmpeg-{channel_id}.service"],
            capture_output=True,
            text=True
        )
        return result.stdout.strip() == "active"
    except Exception:
        return False

async def monitor_channel_activity(channel_id: str):
    """Monitor channel activity and stop FFmpeg if inactive"""
    logger.info(f"Starting activity monitor for channel {channel_id}")
    try:
        while True:
            if time.time() - channel_last_active[channel_id] > 120:
                logger.info(f"Channel {channel_id} inactive, stopping FFmpeg")
                await stop_ffmpeg(channel_id)
                del channel_monitors[channel_id]
                del channel_last_active[channel_id]
                break
            await asyncio.sleep(30)
    except Exception as e:
        logger.error(f"Error in monitor for channel {channel_id}: {e}")
        # Cleanup in case of error
        if channel_id in channel_monitors:
            del channel_monitors[channel_id]
        if channel_id in channel_last_active:
            del channel_last_active[channel_id]

@app.post("/channels/{channel_id}/keepalive")
async def channel_keepalive(channel_id: str):
    """Handle keepalive request for channel"""
    config = load_config()
    if channel_id not in config["channels"]:
        raise HTTPException(status_code=404, detail="Channel not found")
        
    channel_last_active[channel_id] = time.time()
    
    if channel_id not in channel_monitors:
        # Start FFmpeg if not running
        if not is_ffmpeg_running(channel_id):
            await start_ffmpeg(channel_id)
        # Start new monitor task
        channel_monitors[channel_id] = asyncio.create_task(
            monitor_channel_activity(channel_id)
        )
        logger.info(f"Started new monitor for channel {channel_id}")
    
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


@app.put("/channels/{channel_id}/ffmpeg/restart")
def restart_ffmpeg(channel_id: str):
    """Restart FFmpeg service for a channel"""
    config = load_config()
    if channel_id not in config["channels"]:
        raise HTTPException(status_code=404)
    
    try:
        subprocess.run(["systemctl", "restart", f"ffmpeg-{channel_id}.service"], check=True)
        return {"status": "restarted"}
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"Failed to restart FFmpeg service: {e.stderr}")


@app.get("/channels/{channel_id}/metrics")
def get_channel_metrics(channel_id: str):
    """
    Retrieve metrics for a specific channel with caching and error handling
    """
    try:
        config = load_config()
        if channel_id not in config["channels"]:
            logger.error(f"Channel {channel_id} not found")
            raise HTTPException(status_code=404, detail="Channel not found")
        
        channel = config["channels"][channel_id]
        metrics_port = channel.get('metrics_port')
        
        if not metrics_port:
            logger.error(f"No metrics port configured for channel {channel_id}")
            raise HTTPException(status_code=500, detail="Metrics port not configured")
        
        # Retrieve cached or fresh metrics
        metrics = get_cached_metrics(channel_id, metrics_port)
        
        return metrics
    
    except Exception as e:
        logger.error(f"Unexpected error in metrics retrieval: {e}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Internal server error retrieving metrics")

@app.get("/channels/{channel_id}/media-info")
def get_channel_media_info(channel_id: str):
    """Retrieve media information for a specific channel"""
    config = load_config()
    if channel_id not in config["channels"]:
        raise HTTPException(status_code=404)
    
    channel = config["channels"][channel_id]
    try:
        cmd = [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            "-show_programs",
            "-i", channel["output"]
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)
    except subprocess.CalledProcessError as e:
        logger.error(f"FFprobe error for channel {channel_id}: {e.stderr}")
        raise HTTPException(status_code=500, detail=f"FFprobe error: {e.stderr}")
    except json.JSONDecodeError:
        logger.error(f"Failed to parse FFprobe output for channel {channel_id}")
        raise HTTPException(status_code=500, detail="Failed to parse FFprobe output")

@app.get("/health/metrics")
def health_metrics():
    """
    Retrieve cached system health metrics
    """
    return get_system_health_metrics()

@app.get("/health")
def health_check():
    """Provide a health check endpoint"""
    config = load_config()
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "channels": len(config["channels"])
    }

@app.get("/channels/{channel_id}/backup-health")
def get_channel_backup_health(channel_id: str):
    """
    Check backup health status for a channel
    """
    try:
        # Look for a health status file created by the failover script
        health_status_file = f"/root/rist/{channel_id}_backup_health.json"
        
        if os.path.exists(health_status_file):
            with open(health_status_file, 'r') as f:
                health_data = json.load(f)
            
            return {
                "channel_id": channel_id,
                "has_backups": health_data.get('has_backups', False),
                "is_healthy": health_data.get('is_healthy', False),
                "last_checked": health_data.get('last_checked', None)
            }
        else:
            # If no health status file exists, assume no backups
            return {
                "channel_id": channel_id,
                "has_backups": False,
                "is_healthy": False,
                "last_checked": None
            }
    
    except Exception as e:
        logger.error(f"Failed to retrieve backup health for {channel_id}: {e}")
        return {
            "channel_id": channel_id,
            "has_backups": False,
            "is_healthy": False,
            "last_checked": None
        }

@app.get("/channels/{channel_id}/backup-sources")
def get_channel_backup_sources(channel_id: str):
    """
    Retrieve backup sources for a specific channel
    """
    try:
        # Load backup sources configuration
        backup_config_path = "/root/rist/backup_sources.yaml"
        
        if not os.path.exists(backup_config_path):
            return {"channel_id": channel_id, "backup_sources": []}
        
        with open(backup_config_path, 'r') as f:
            backup_config = yaml.safe_load(f) or {}
        
        # Get backup sources for the channel
        backup_sources = backup_config.get('channels', {}).get(channel_id, [])
        
        return {
            "channel_id": channel_id,
            "backup_sources": backup_sources
        }
    except Exception as e:
        logger.error(f"Failed to retrieve backup sources for {channel_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve backup sources")

@app.put("/channels/{channel_id}/backup-sources")
def update_channel_backup_sources(
    channel_id: str, 
    backup_sources: List[str] = Body(...)
):
    """
    Update backup sources for a specific channel
    """
    try:
        backup_config_path = "/root/rist/backup_sources.yaml"
        
        # Load existing backup sources configuration
        try:
            with open(backup_config_path, 'r') as f:
                backup_config = yaml.safe_load(f) or {}
        except FileNotFoundError:
            backup_config = {}
        
        # Ensure channels key exists
        if 'channels' not in backup_config:
            backup_config['channels'] = {}
        
        # Remove empty strings and duplicates
        clean_sources = list(dict.fromkeys(
            [source.strip() for source in backup_sources if source.strip()]
        ))
        
        # Update backup sources for the channel
        if clean_sources:
            backup_config['channels'][channel_id] = clean_sources
        elif channel_id in backup_config['channels']:
            # Remove channel if no backup sources
            del backup_config['channels'][channel_id]
        
        # Save updated configuration
        with open(backup_config_path, 'w') as f:
            yaml.safe_dump(backup_config, f)
        
        return {
            "channel_id": channel_id,
            "backup_sources": clean_sources,
            "message": "Backup sources updated successfully"
        }
    except Exception as e:
        logger.error(f"Failed to update backup sources for {channel_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to update backup sources")


# =============================================================================
# Configuration Backup/Restore/Revert Endpoints
# =============================================================================

BACKUP_SOURCES_PATH = "/root/rist/backup_sources.yaml"

@app.get("/config/backup")
def download_config_backup():
    """
    Download combined configuration backup (receiver_config + backup_sources)
    """
    try:
        # Load receiver config
        receiver_config = load_config()
        
        # Load backup sources
        backup_sources = {}
        if os.path.exists(BACKUP_SOURCES_PATH):
            with open(BACKUP_SOURCES_PATH, 'r') as f:
                backup_sources = yaml.safe_load(f) or {}
        
        # Combine into single backup
        combined_backup = {
            "version": "1.0",
            "timestamp": datetime.now().isoformat(),
            "hostname": socket.gethostname(),
            "receiver_config": receiver_config,
            "backup_sources": backup_sources
        }
        
        return combined_backup
    
    except Exception as e:
        logger.error(f"Failed to create config backup: {e}")
        raise HTTPException(status_code=500, detail="Failed to create config backup")


@app.get("/config/has-backup")
def check_backup_exists():
    """
    Check if .bak files exist for potential revert
    """
    receiver_bak = os.path.exists(f"{CONFIG_FILE}.bak")
    sources_bak = os.path.exists(f"{BACKUP_SOURCES_PATH}.bak")
    
    bak_timestamp = None
    if receiver_bak:
        try:
            bak_timestamp = datetime.fromtimestamp(
                os.path.getmtime(f"{CONFIG_FILE}.bak")
            ).isoformat()
        except:
            pass
    
    return {
        "has_backup": receiver_bak,
        "backup_timestamp": bak_timestamp
    }


@app.post("/config/restore")
async def restore_config(backup_data: dict = Body(...)):
    """
    Restore configuration from backup.
    - Stops all running channels
    - Saves current config as .bak
    - Restores from provided backup
    - Regenerates all service files
    """
    try:
        logger.info("Starting configuration restore")
        
        # Validate backup data
        if "receiver_config" not in backup_data:
            raise HTTPException(status_code=400, detail="Invalid backup: missing receiver_config")
        
        # Step 1: Stop all running channels
        current_config = load_config()
        for channel_id in current_config.get("channels", {}):
            if get_channel_status(channel_id) == "running":
                logger.info(f"Stopping channel {channel_id} for restore")
                try:
                    subprocess.run(["systemctl", "stop", f"rist-channel-{channel_id}.service"], check=True)
                except subprocess.CalledProcessError as e:
                    logger.warning(f"Failed to stop channel {channel_id}: {e}")
        
        # Step 2: Create .bak files of current config
        logger.info("Creating backup of current configuration")
        
        # Backup receiver_config.yaml
        if os.path.exists(CONFIG_FILE):
            import shutil
            shutil.copy2(CONFIG_FILE, f"{CONFIG_FILE}.bak")
        
        # Backup backup_sources.yaml
        if os.path.exists(BACKUP_SOURCES_PATH):
            import shutil
            shutil.copy2(BACKUP_SOURCES_PATH, f"{BACKUP_SOURCES_PATH}.bak")
        
        # Step 3: Delete service files for channels not in new config
        new_channels = backup_data["receiver_config"].get("channels", {})
        for channel_id in current_config.get("channels", {}):
            if channel_id not in new_channels:
                logger.info(f"Removing channel {channel_id} (not in backup)")
                # Remove service files
                for service in [f"rist-channel-{channel_id}.service", f"ffmpeg-{channel_id}.service"]:
                    service_path = f"{SERVICE_DIR}/{service}"
                    try:
                        subprocess.run(["systemctl", "disable", service], capture_output=True)
                        if os.path.exists(service_path):
                            os.remove(service_path)
                    except Exception as e:
                        logger.warning(f"Failed to remove service {service}: {e}")
        
        # Step 4: Save new receiver config
        save_config(backup_data["receiver_config"])
        
        # Step 5: Save new backup sources
        backup_sources = backup_data.get("backup_sources", {"channels": {}})
        with open(BACKUP_SOURCES_PATH, 'w') as f:
            yaml.safe_dump(backup_sources, f)
        
        # Step 6: Generate service files for all channels
        for channel_id, channel_data in new_channels.items():
            logger.info(f"Generating service files for {channel_id}")
            generate_service_file(channel_id, channel_data)
        
        # Reload systemd
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        
        return {
            "status": "success",
            "message": "Configuration restored successfully",
            "channels_restored": len(new_channels)
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to restore config: {e}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Failed to restore config: {str(e)}")


@app.post("/config/revert")
async def revert_config():
    """
    Revert configuration to .bak files
    """
    try:
        logger.info("Starting configuration revert")
        
        # Check if .bak files exist
        if not os.path.exists(f"{CONFIG_FILE}.bak"):
            raise HTTPException(status_code=404, detail="No backup found to revert to")
        
        # Step 1: Stop all running channels
        current_config = load_config()
        for channel_id in current_config.get("channels", {}):
            if get_channel_status(channel_id) == "running":
                logger.info(f"Stopping channel {channel_id} for revert")
                try:
                    subprocess.run(["systemctl", "stop", f"rist-channel-{channel_id}.service"], check=True)
                except subprocess.CalledProcessError as e:
                    logger.warning(f"Failed to stop channel {channel_id}: {e}")
        
        # Step 2: Load the backup config to know what channels to expect
        with open(f"{CONFIG_FILE}.bak", 'r') as f:
            bak_config = yaml.safe_load(f)
        
        # Step 3: Delete service files for channels not in .bak config
        bak_channels = bak_config.get("channels", {})
        for channel_id in current_config.get("channels", {}):
            if channel_id not in bak_channels:
                logger.info(f"Removing channel {channel_id} (not in .bak)")
                for service in [f"rist-channel-{channel_id}.service", f"ffmpeg-{channel_id}.service"]:
                    service_path = f"{SERVICE_DIR}/{service}"
                    try:
                        subprocess.run(["systemctl", "disable", service], capture_output=True)
                        if os.path.exists(service_path):
                            os.remove(service_path)
                    except Exception as e:
                        logger.warning(f"Failed to remove service {service}: {e}")
        
        # Step 4: Restore from .bak files
        import shutil
        shutil.copy2(f"{CONFIG_FILE}.bak", CONFIG_FILE)
        
        if os.path.exists(f"{BACKUP_SOURCES_PATH}.bak"):
            shutil.copy2(f"{BACKUP_SOURCES_PATH}.bak", BACKUP_SOURCES_PATH)
        
        # Step 5: Regenerate service files for all channels in .bak
        for channel_id, channel_data in bak_channels.items():
            logger.info(f"Regenerating service files for {channel_id}")
            generate_service_file(channel_id, channel_data)
        
        # Reload systemd
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        
        return {
            "status": "success",
            "message": "Configuration reverted successfully",
            "channels_restored": len(bak_channels)
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to revert config: {e}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Failed to revert config: {str(e)}")


if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(app, host="0.0.0.0", port=5000)
