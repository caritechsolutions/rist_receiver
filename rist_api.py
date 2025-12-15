from fastapi import FastAPI, HTTPException, Query, Body, Request, Response, Depends, Cookie
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
import glob
import shutil
import threading


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


# Custom CORS middleware that handles credentials properly
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

class CORSMiddlewareCustom(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Get the origin from the request
        origin = request.headers.get("origin", "")
        
        # Handle preflight requests
        if request.method == "OPTIONS":
            response = Response(status_code=200)
            response.headers["Access-Control-Allow-Origin"] = origin if origin else "*"
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, Cookie, Cache-Control, Pragma"
            response.headers["Access-Control-Max-Age"] = "600"
            return response
        
        # Process the request
        response = await call_next(request)
        
        # Add CORS headers to response
        response.headers["Access-Control-Allow-Origin"] = origin if origin else "*"
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, Cookie, Cache-Control, Pragma"
        
        return response

app.add_middleware(CORSMiddlewareCustom)


# Authentication Middleware
class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Skip auth for OPTIONS (preflight) requests
        if request.method == "OPTIONS":
            return await call_next(request)
        
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

# Lock to prevent concurrent metrics fetches to the same port
metrics_fetch_lock = threading.Lock()

def get_cached_metrics(channel_id: str, metrics_port: int, cache_timeout: int = 1):
    """
    Retrieve metrics with a simple caching mechanism and thread-safe fetching
    """
    current_time = time.time()
    
    # Check if we have a cached result and it's fresh
    if (channel_id in metrics_cache and 
        current_time - metrics_cache[channel_id]['timestamp'] < cache_timeout):
        return metrics_cache[channel_id]['data']
    
    # Use lock to prevent concurrent requests to the same metrics endpoint
    with metrics_fetch_lock:
        # Check cache again in case another thread just updated it
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
                'timestamp': time.time(),
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
        


def get_channel_status(channel_id: str) -> tuple:
    """
    Get the status of a channel's systemd service and any error message
    Returns: (status, last_error)
    """
    try:
        # Check systemd service state
        result = subprocess.run(
            ["systemctl", "is-active", f"rist-channel-{channel_id}.service"],
            capture_output=True,
            text=True
        )
        service_state = result.stdout.strip()
        
        logger.debug(f"Channel {channel_id} systemd state: {service_state}")
        
        if service_state == "failed":
            # Get failure reason from systemd
            reason_result = subprocess.run(
                ["systemctl", "status", f"rist-channel-{channel_id}.service", "--no-pager", "-l"],
                capture_output=True,
                text=True
            )
            # Extract last few lines for error context
            status_lines = reason_result.stdout.strip().split('\n')
            error_msg = "Service failed"
            for line in status_lines[-5:]:
                if "error" in line.lower() or "failed" in line.lower():
                    error_msg = line.strip()[:100]  # Limit length
                    break
            return ("error", error_msg)
        
        elif service_state == "active":
            # Service is running, now check stream health using CACHED metrics
            config = load_config()
            if channel_id in config["channels"]:
                channel = config["channels"][channel_id]
                metrics_port = channel.get("metrics_port")
                
                if metrics_port:
                    # Use the shared cached metrics - don't make a separate request
                    metrics = get_cached_metrics(channel_id, metrics_port, cache_timeout=2)
                    
                    peers = metrics.get("peers", 0)
                    quality = metrics.get("quality", 100)
                    
                    logger.debug(f"Channel {channel_id} cached metrics - peers: {peers}, quality: {quality}")
                    
                    # Check for unhealthy stream
                    if peers == 0:
                        return ("error", "No peers connected")
                    elif quality < 50:
                        return ("error", f"Poor stream quality: {quality:.0f}%")
            
            return ("running", None)
        
        else:
            # inactive, deactivating, etc.
            return ("stopped", None)
            
    except Exception as e:
        logger.error(f"Error checking channel status for {channel_id}: {e}")
        return ("error", str(e))

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


class HostnameChangeRequest(BaseModel):
    hostname: str
    reboot: bool = False


@app.post("/system/hostname")
def set_system_hostname(request: HostnameChangeRequest, auth: dict = Depends(require_auth)):
    """
    Change the system hostname.
    Updates /etc/hostname and /etc/hosts.
    Optionally triggers a reboot.
    """
    import re
    
    new_hostname = request.hostname.strip()
    
    # Validate hostname
    if not new_hostname:
        raise HTTPException(status_code=400, detail="Hostname cannot be empty")
    
    if len(new_hostname) > 63:
        raise HTTPException(status_code=400, detail="Hostname must be 63 characters or less")
    
    if not re.match(r'^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?$', new_hostname):
        raise HTTPException(status_code=400, detail="Hostname can only contain letters, numbers, and hyphens. Cannot start or end with a hyphen.")
    
    if new_hostname.isdigit():
        raise HTTPException(status_code=400, detail="Hostname cannot be all numbers")
    
    try:
        old_hostname = socket.gethostname()
        
        # Update /etc/hostname
        with open('/etc/hostname', 'w') as f:
            f.write(new_hostname + '\n')
        
        # Update /etc/hosts - replace old hostname with new
        with open('/etc/hosts', 'r') as f:
            hosts_content = f.read()
        
        updated_hosts = hosts_content.replace(old_hostname, new_hostname)
        
        with open('/etc/hosts', 'w') as f:
            f.write(updated_hosts)
        
        # Apply hostname immediately without reboot
        subprocess.run(['hostnamectl', 'set-hostname', new_hostname], check=True, capture_output=True)
        
        logger.info(f"Hostname changed from '{old_hostname}' to '{new_hostname}'")
        
        # Trigger reboot if requested
        if request.reboot:
            subprocess.Popen(['shutdown', '-r', '+0', 'Hostname changed - rebooting'])
            return {
                "success": True,
                "message": f"Hostname changed to '{new_hostname}'. System is rebooting...",
                "old_hostname": old_hostname,
                "new_hostname": new_hostname,
                "rebooting": True
            }
        
        return {
            "success": True,
            "message": f"Hostname changed to '{new_hostname}'. A reboot is recommended for all services to recognize the new hostname.",
            "old_hostname": old_hostname,
            "new_hostname": new_hostname,
            "rebooting": False
        }
        
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to change hostname: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to apply hostname: {e.stderr.decode() if e.stderr else str(e)}")
    except Exception as e:
        logger.error(f"Failed to change hostname: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to change hostname: {str(e)}")


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
                    if state == "up":
                        iface_info["status"] = "up"
                    elif state == "unknown":
                        if iface_info["ipv4"] or iface_info["ipv6"]:
                            iface_info["status"] = "up"
                        else:
                            iface_info["status"] = "down"
                    else:
                        iface_info["status"] = "down"
            except:
                if iface_info["ipv4"] or iface_info["ipv6"]:
                    iface_info["status"] = "up"
            
            # Get speed (only for physical interfaces)
            try:
                with open(f"/sys/class/net/{iface}/speed", "r") as f:
                    speed = int(f.read().strip())
                    if speed > 0:
                        iface_info["speed"] = speed
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
    
    if "[Interface]" not in config_content:
        raise HTTPException(status_code=400, detail="Invalid config: missing [Interface] section")
    
    try:
        os.makedirs("/etc/wireguard", exist_ok=True)
        
        subprocess.run(["systemctl", "stop", "wg-quick@wg0"], capture_output=True)
        
        with open(config_path, "w") as f:
            f.write(config_content)
        
        os.chmod(config_path, 0o600)
        
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


# =============================================================================
# Multicast Routing Configuration
# =============================================================================

NETPLAN_MCAST_FILE = "/etc/netplan/60-multicast-bridge.yaml"

class MulticastConfig(BaseModel):
    bridge_name: str = "br0"
    bridge_address: str = "10.0.0.1/24"
    multicast_route: str = "224.0.0.0/4"
    bridge_members: List[str] = []


@app.get("/system/multicast")
def get_multicast_config():
    """Get current multicast bridge configuration"""
    config = {
        "bridge_name": "br0",
        "bridge_address": "10.0.0.1/24",
        "multicast_route": "224.0.0.0/4",
        "bridge_members": [],
        "config_exists": False
    }
    
    try:
        if os.path.exists(NETPLAN_MCAST_FILE):
            config["config_exists"] = True
            with open(NETPLAN_MCAST_FILE, 'r') as f:
                netplan_config = yaml.safe_load(f)
            
            if netplan_config and "network" in netplan_config:
                bridges = netplan_config["network"].get("bridges", {})
                
                for bridge_name, bridge_config in bridges.items():
                    config["bridge_name"] = bridge_name
                    config["bridge_members"] = bridge_config.get("interfaces", [])
                    
                    addresses = bridge_config.get("addresses", [])
                    if addresses:
                        config["bridge_address"] = addresses[0]
                    
                    routes = bridge_config.get("routes", [])
                    for route in routes:
                        if route.get("to", "").startswith("224."):
                            config["multicast_route"] = route.get("to", "224.0.0.0/4")
                    break
        
        return config
        
    except Exception as e:
        logger.error(f"Failed to read multicast config: {e}")
        return config


@app.post("/system/multicast")
def save_multicast_config(config: MulticastConfig):
    """Save multicast bridge configuration to netplan"""
    try:
        logger.info(f"Saving multicast config: bridge={config.bridge_name}, members={config.bridge_members}")
        
        if config.bridge_members:
            netplan_config = {
                "network": {
                    "version": 2,
                    "bridges": {
                        config.bridge_name: {
                            "interfaces": config.bridge_members,
                            "addresses": [config.bridge_address],
                            "routes": [
                                {
                                    "to": config.multicast_route,
                                    "scope": "link"
                                }
                            ],
                            "parameters": {
                                "stp": False,
                                "forward-delay": 0
                            }
                        }
                    }
                }
            }
            
            os.makedirs(os.path.dirname(NETPLAN_MCAST_FILE), exist_ok=True)
            
            with open(NETPLAN_MCAST_FILE, 'w') as f:
                yaml.dump(netplan_config, f, default_flow_style=False, sort_keys=False)
            
            os.chmod(NETPLAN_MCAST_FILE, 0o600)
        else:
            if os.path.exists(NETPLAN_MCAST_FILE):
                os.remove(NETPLAN_MCAST_FILE)
                logger.info("Removed multicast config file (no bridge members)")
        
        try:
            result = subprocess.run(
                ["netplan", "apply"],
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode != 0:
                logger.error(f"Netplan apply failed: {result.stderr}")
                raise HTTPException(
                    status_code=500, 
                    detail=f"Failed to apply netplan: {result.stderr}"
                )
            
            logger.info("Multicast configuration applied successfully")
            
            restarted_channels = []
            config_data = load_config()
            for channel_id in config_data["channels"]:
                status, _ = get_channel_status(channel_id)
                if status == "running":
                    try:
                        subprocess.run(
                            ["systemctl", "restart", f"rist-channel-{channel_id}.service"],
                            check=True,
                            timeout=30
                        )
                        restarted_channels.append(channel_id)
                        logger.info(f"Restarted channel {channel_id} after multicast config change")
                    except Exception as e:
                        logger.error(f"Failed to restart channel {channel_id}: {e}")
            
            return {
                "status": "success",
                "message": "Multicast configuration saved and applied",
                "bridge_name": config.bridge_name,
                "bridge_members": config.bridge_members,
                "restarted_channels": restarted_channels
            }
            
        except subprocess.TimeoutExpired:
            logger.error("Netplan apply timed out")
            raise HTTPException(status_code=500, detail="Netplan apply timed out")
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to save multicast config: {e}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Failed to save configuration: {str(e)}")


@app.delete("/system/multicast")
def delete_multicast_config():
    """Remove multicast bridge configuration"""
    try:
        if os.path.exists(NETPLAN_MCAST_FILE):
            os.remove(NETPLAN_MCAST_FILE)
            
            subprocess.run(["netplan", "apply"], check=True, timeout=30)
            
            logger.info("Multicast configuration removed")
            
            return {"status": "success", "message": "Multicast configuration removed"}
        else:
            return {"status": "success", "message": "No multicast configuration to remove"}
            
    except Exception as e:
        logger.error(f"Failed to remove multicast config: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to remove configuration: {str(e)}")


# =============================================================================
# Netplan Interface Management
# =============================================================================

NETPLAN_DIR = "/etc/netplan"
RIST_MANAGED_NETPLAN = "/etc/netplan/80-rist-managed.yaml"
DHCP_CONFIG_FILE = "/etc/dhcp/dhcpd.conf"
DHCP_DEFAULT_FILE = "/etc/default/isc-dhcp-server"

# Track pending netplan changes (in-memory state)
netplan_pending = {
    "active": False,
    "timeout_thread": None,
    "backup_files": {},
    "expires_at": None
}


class InterfaceUpdateRequest(BaseModel):
    addresses: List[str] = []
    gateway: Optional[str] = None
    dns_servers: List[str] = ["8.8.8.8", "8.8.4.4"]
    dhcp4: bool = False


class DHCPSubnetRequest(BaseModel):
    interface: str
    enabled: bool
    range_start: Optional[str] = None
    range_end: Optional[str] = None
    lease_time: int = 86400
    dns_servers: List[str] = ["8.8.8.8", "8.8.4.4"]
    static_leases: List[Dict] = []


def scan_netplan_files() -> Dict:
    """
    Scan all netplan files and build a map of interfaces to their source files.
    """
    result = {
        "files": {},
        "interfaces": {}
    }
    
    netplan_files = sorted(glob.glob(f"{NETPLAN_DIR}/*.yaml"))
    
    for filepath in netplan_files:
        try:
            with open(filepath, 'r') as f:
                content = yaml.safe_load(f)
            
            if not content or "network" not in content:
                continue
                
            result["files"][filepath] = content
            
            network = content["network"]
            
            for iface_name, iface_config in network.get("ethernets", {}).items():
                result["interfaces"][iface_name] = {
                    "file": filepath,
                    "type": "ethernets",
                    "config": iface_config or {}
                }
            
            for iface_name, iface_config in network.get("bridges", {}).items():
                result["interfaces"][iface_name] = {
                    "file": filepath,
                    "type": "bridges",
                    "config": iface_config or {}
                }
            
            for iface_name, iface_config in network.get("bonds", {}).items():
                result["interfaces"][iface_name] = {
                    "file": filepath,
                    "type": "bonds",
                    "config": iface_config or {}
                }
            
            for iface_name, iface_config in network.get("vlans", {}).items():
                result["interfaces"][iface_name] = {
                    "file": filepath,
                    "type": "vlans",
                    "config": iface_config or {}
                }
                
        except Exception as e:
            logger.error(f"Error parsing netplan file {filepath}: {e}")
    
    return result


def get_interface_runtime_info(iface_name: str) -> Dict:
    """Get runtime info for an interface using netifaces"""
    info = {
        "name": iface_name,
        "exists": False,
        "ipv4": [],
        "mac": None,
        "status": "unknown"
    }
    
    try:
        if iface_name in netifaces.interfaces():
            info["exists"] = True
            addrs = netifaces.ifaddresses(iface_name)
            
            if netifaces.AF_INET in addrs:
                for addr in addrs[netifaces.AF_INET]:
                    info["ipv4"].append({
                        "address": addr.get("addr"),
                        "netmask": addr.get("netmask")
                    })
            
            if netifaces.AF_LINK in addrs:
                for addr in addrs[netifaces.AF_LINK]:
                    if addr.get("addr"):
                        info["mac"] = addr.get("addr")
                        break
            
            try:
                with open(f"/sys/class/net/{iface_name}/operstate", "r") as f:
                    state = f.read().strip()
                    info["status"] = state
            except:
                pass
                
    except Exception as e:
        logger.error(f"Error getting runtime info for {iface_name}: {e}")
    
    return info


def backup_netplan_file(filepath: str) -> str:
    """Create a backup of a netplan file, return backup content"""
    if os.path.exists(filepath):
        with open(filepath, 'r') as f:
            return f.read()
    return ""


def restore_netplan_backup(filepath: str, content: str):
    """Restore a netplan file from backup content"""
    if content:
        with open(filepath, 'w') as f:
            f.write(content)
    elif os.path.exists(filepath):
        os.remove(filepath)


def netplan_timeout_revert():
    """Called when netplan try times out - restore backups"""
    global netplan_pending
    
    if not netplan_pending["active"]:
        return
    
    logger.warning("Netplan try timeout - reverting changes")
    
    for filepath, content in netplan_pending["backup_files"].items():
        try:
            restore_netplan_backup(filepath, content)
        except Exception as e:
            logger.error(f"Error restoring {filepath}: {e}")
    
    try:
        subprocess.run(["netplan", "apply"], check=True, capture_output=True)
    except Exception as e:
        logger.error(f"Error applying reverted netplan: {e}")
    
    netplan_pending["active"] = False
    netplan_pending["backup_files"] = {}
    netplan_pending["expires_at"] = None


@app.get("/system/netplan/interfaces")
def get_netplan_interfaces(auth: dict = Depends(require_auth)):
    """
    Get all network interfaces with their netplan configuration and runtime status.
    """
    try:
        netplan_data = scan_netplan_files()
        
        system_interfaces = netifaces.interfaces()
        
        result = {
            "interfaces": [],
            "pending_changes": netplan_pending["active"],
            "pending_expires_at": netplan_pending["expires_at"].isoformat() if netplan_pending["expires_at"] else None
        }
        
        processed = set()
        
        for iface_name, iface_data in netplan_data["interfaces"].items():
            if iface_name == "lo" or iface_name.startswith("wg"):
                continue
                
            processed.add(iface_name)
            runtime = get_interface_runtime_info(iface_name)
            
            config = iface_data["config"]
            
            # Extract gateway from routes if present
            gateway = config.get("gateway4")
            if not gateway and config.get("routes"):
                for route in config.get("routes", []):
                    if route.get("to") == "default" or route.get("to") == "0.0.0.0/0":
                        gateway = route.get("via")
                        break
            
            result["interfaces"].append({
                "name": iface_name,
                "source_file": iface_data["file"],
                "type": iface_data["type"],
                "configured": True,
                "runtime": runtime,
                "config": {
                    "addresses": config.get("addresses", []),
                    "gateway4": gateway,
                    "dns_servers": config.get("nameservers", {}).get("addresses", []),
                    "dhcp4": config.get("dhcp4", False)
                }
            })
        
        for iface_name in system_interfaces:
            if iface_name == "lo" or iface_name.startswith("wg"):
                continue
            if iface_name in processed:
                continue
            
            runtime = get_interface_runtime_info(iface_name)
            
            result["interfaces"].append({
                "name": iface_name,
                "source_file": None,
                "type": "ethernets",
                "configured": False,
                "runtime": runtime,
                "config": {
                    "addresses": [],
                    "gateway4": None,
                    "dns_servers": [],
                    "dhcp4": False
                }
            })
        
        result["interfaces"].sort(key=lambda x: x["name"])
        
        return result
        
    except Exception as e:
        logger.error(f"Error getting netplan interfaces: {e}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/system/netplan/interface/{iface_name}")
def update_interface_config(iface_name: str, config: InterfaceUpdateRequest, auth: dict = Depends(require_auth)):
    """
    Update interface configuration using netplan try.
    Changes are temporary until confirmed (120 second timeout).
    """
    global netplan_pending
    
    if iface_name == "lo" or iface_name.startswith("wg"):
        raise HTTPException(status_code=400, detail="Cannot configure loopback or WireGuard interfaces here")
    
    if netplan_pending["active"]:
        raise HTTPException(status_code=409, detail="Another netplan change is pending confirmation. Confirm or wait for timeout.")
    
    try:
        netplan_data = scan_netplan_files()
        
        if iface_name in netplan_data["interfaces"]:
            target_file = netplan_data["interfaces"][iface_name]["file"]
            iface_type = netplan_data["interfaces"][iface_name]["type"]
        else:
            target_file = RIST_MANAGED_NETPLAN
            iface_type = "ethernets"
        
        backup_content = backup_netplan_file(target_file)
        
        if os.path.exists(target_file):
            with open(target_file, 'r') as f:
                netplan_config = yaml.safe_load(f) or {}
        else:
            netplan_config = {}
        
        if "network" not in netplan_config:
            netplan_config["network"] = {"version": 2}
        if iface_type not in netplan_config["network"]:
            netplan_config["network"][iface_type] = {}
        
        new_iface_config = {}
        
        if config.dhcp4:
            new_iface_config["dhcp4"] = True
        else:
            new_iface_config["dhcp4"] = False
            if config.addresses:
                new_iface_config["addresses"] = config.addresses
            if config.gateway:
                new_iface_config["routes"] = [{"to": "default", "via": config.gateway}]
            if config.dns_servers:
                new_iface_config["nameservers"] = {"addresses": config.dns_servers}
        
        netplan_config["network"][iface_type][iface_name] = new_iface_config
        
        with open(target_file, 'w') as f:
            yaml.dump(netplan_config, f, default_flow_style=False)
        
        with open(f"{target_file}.bak", 'w') as f:
            f.write(backup_content)
        
        try:
            result = subprocess.run(
                ["netplan", "try", "--timeout", "120"],
                capture_output=True,
                text=True,
                timeout=5
            )
        except subprocess.TimeoutExpired:
            pass
        except subprocess.CalledProcessError as e:
            restore_netplan_backup(target_file, backup_content)
            raise HTTPException(status_code=500, detail=f"Netplan try failed: {e.stderr}")
        
        netplan_pending["active"] = True
        netplan_pending["backup_files"] = {target_file: backup_content}
        netplan_pending["expires_at"] = datetime.now() + timedelta(seconds=120)
        
        def timeout_handler():
            time.sleep(125)
            if netplan_pending["active"]:
                netplan_timeout_revert()
        
        timeout_thread = threading.Thread(target=timeout_handler, daemon=True)
        timeout_thread.start()
        netplan_pending["timeout_thread"] = timeout_thread
        
        return {
            "status": "pending",
            "message": "Network configuration applied temporarily. Confirm within 120 seconds or changes will revert.",
            "expires_at": netplan_pending["expires_at"].isoformat(),
            "interface": iface_name,
            "file_modified": target_file
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating interface {iface_name}: {e}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/system/netplan/confirm")
def confirm_netplan_changes(auth: dict = Depends(require_auth)):
    """Confirm pending netplan changes to make them permanent"""
    global netplan_pending
    
    if not netplan_pending["active"]:
        raise HTTPException(status_code=400, detail="No pending changes to confirm")
    
    try:
        result = subprocess.run(["netplan", "apply"], capture_output=True, text=True, check=True)
        
        netplan_pending["active"] = False
        netplan_pending["backup_files"] = {}
        netplan_pending["expires_at"] = None
        
        return {
            "status": "confirmed",
            "message": "Network configuration changes have been made permanent"
        }
        
    except subprocess.CalledProcessError as e:
        logger.error(f"Error confirming netplan: {e.stderr}")
        raise HTTPException(status_code=500, detail=f"Failed to confirm changes: {e.stderr}")


@app.post("/system/netplan/revert")
def revert_netplan_changes(auth: dict = Depends(require_auth)):
    """Manually revert pending netplan changes before timeout"""
    global netplan_pending
    
    if not netplan_pending["active"]:
        raise HTTPException(status_code=400, detail="No pending changes to revert")
    
    try:
        for filepath, content in netplan_pending["backup_files"].items():
            restore_netplan_backup(filepath, content)
        
        subprocess.run(["netplan", "apply"], check=True, capture_output=True)
        
        netplan_pending["active"] = False
        netplan_pending["backup_files"] = {}
        netplan_pending["expires_at"] = None
        
        return {
            "status": "reverted",
            "message": "Network configuration changes have been reverted"
        }
        
    except Exception as e:
        logger.error(f"Error reverting netplan: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# DHCP Server Management (isc-dhcp-server)
# =============================================================================

def parse_dhcp_config() -> Dict:
    """Parse the current DHCP server configuration"""
    config = {
        "subnets": {},
        "global_options": {},
        "static_leases": []
    }
    
    if not os.path.exists(DHCP_CONFIG_FILE):
        return config
    
    try:
        with open(DHCP_CONFIG_FILE, 'r') as f:
            content = f.read()
        
        config["raw_content"] = content
        
        import re
        subnet_pattern = r'subnet\s+([\d.]+)\s+netmask\s+([\d.]+)\s*\{([^}]+)\}'
        for match in re.finditer(subnet_pattern, content, re.DOTALL):
            subnet_ip = match.group(1)
            netmask = match.group(2)
            block_content = match.group(3)
            
            subnet_config = {
                "netmask": netmask,
                "range_start": None,
                "range_end": None,
                "router": None,
                "dns_servers": [],
                "lease_time": 86400
            }
            
            range_match = re.search(r'range\s+([\d.]+)\s+([\d.]+)', block_content)
            if range_match:
                subnet_config["range_start"] = range_match.group(1)
                subnet_config["range_end"] = range_match.group(2)
            
            router_match = re.search(r'option\s+routers\s+([\d.]+)', block_content)
            if router_match:
                subnet_config["router"] = router_match.group(1)
            
            dns_match = re.search(r'option\s+domain-name-servers\s+([^;]+)', block_content)
            if dns_match:
                subnet_config["dns_servers"] = [ip.strip() for ip in dns_match.group(1).split(',')]
            
            lease_match = re.search(r'default-lease-time\s+(\d+)', block_content)
            if lease_match:
                subnet_config["lease_time"] = int(lease_match.group(1))
            
            config["subnets"][subnet_ip] = subnet_config
        
        host_pattern = r'host\s+(\S+)\s*\{([^}]+)\}'
        for match in re.finditer(host_pattern, content, re.DOTALL):
            hostname = match.group(1)
            block_content = match.group(2)
            
            mac_match = re.search(r'hardware\s+ethernet\s+([^;]+)', block_content)
            ip_match = re.search(r'fixed-address\s+([\d.]+)', block_content)
            
            if mac_match and ip_match:
                config["static_leases"].append({
                    "hostname": hostname,
                    "mac": mac_match.group(1).strip(),
                    "ip": ip_match.group(1)
                })
        
    except Exception as e:
        logger.error(f"Error parsing DHCP config: {e}")
    
    return config


def get_dhcp_interfaces() -> List[str]:
    """Get list of interfaces DHCP server is listening on"""
    interfaces = []
    
    if os.path.exists(DHCP_DEFAULT_FILE):
        try:
            with open(DHCP_DEFAULT_FILE, 'r') as f:
                content = f.read()
            
            import re
            match = re.search(r'INTERFACESv4="([^"]*)"', content)
            if match:
                interfaces = match.group(1).split()
        except Exception as e:
            logger.error(f"Error reading DHCP interfaces: {e}")
    
    return interfaces


def generate_dhcp_config(subnets: Dict, static_leases: List[Dict]) -> str:
    """Generate dhcpd.conf content"""
    lines = [
        "# DHCP Server Configuration",
        "# Generated by RIST Manager",
        "",
        "authoritative;",
        "log-facility local7;",
        ""
    ]
    
    for subnet_ip, config in subnets.items():
        lines.append(f"subnet {subnet_ip} netmask {config['netmask']} {{")
        
        if config.get("range_start") and config.get("range_end"):
            lines.append(f"    range {config['range_start']} {config['range_end']};")
        
        if config.get("router"):
            lines.append(f"    option routers {config['router']};")
        
        if config.get("dns_servers"):
            dns_str = ", ".join(config["dns_servers"])
            lines.append(f"    option domain-name-servers {dns_str};")
        
        lease_time = config.get("lease_time", 86400)
        lines.append(f"    default-lease-time {lease_time};")
        lines.append(f"    max-lease-time {lease_time * 2};")
        
        lines.append("}")
        lines.append("")
    
    if static_leases:
        lines.append("# Static Leases")
        for lease in static_leases:
            hostname = lease.get("hostname", f"host-{lease['ip'].replace('.', '-')}")
            lines.append(f"host {hostname} {{")
            lines.append(f"    hardware ethernet {lease['mac']};")
            lines.append(f"    fixed-address {lease['ip']};")
            lines.append("}")
            lines.append("")
    
    return "\n".join(lines)


def calculate_subnet_from_ip(ip_with_cidr: str) -> tuple:
    """
    Calculate subnet address and netmask from IP with CIDR.
    """
    import ipaddress
    
    try:
        network = ipaddress.ip_network(ip_with_cidr, strict=False)
        return str(network.network_address), str(network.netmask)
    except Exception:
        return None, None


@app.get("/system/dhcp/status")
def get_dhcp_status(auth: dict = Depends(require_auth)):
    """Get DHCP server status and configuration"""
    try:
        service_installed = os.path.exists("/usr/sbin/dhcpd")
        
        service_status = "unknown"
        try:
            result = subprocess.run(
                ["systemctl", "is-active", "isc-dhcp-server"],
                capture_output=True, text=True
            )
            service_status = result.stdout.strip()
        except:
            pass
        
        config = parse_dhcp_config()
        interfaces = get_dhcp_interfaces()
        
        leases = []
        leases_file = "/var/lib/dhcp/dhcpd.leases"
        if os.path.exists(leases_file):
            try:
                with open(leases_file, 'r') as f:
                    content = f.read()
                
                import re
                lease_pattern = r'lease\s+([\d.]+)\s*\{([^}]+)\}'
                for match in re.finditer(lease_pattern, content, re.DOTALL):
                    ip = match.group(1)
                    block = match.group(2)
                    
                    lease_info = {"ip": ip}
                    
                    mac_match = re.search(r'hardware\s+ethernet\s+([^;]+)', block)
                    if mac_match:
                        lease_info["mac"] = mac_match.group(1).strip()
                    
                    hostname_match = re.search(r'client-hostname\s+"([^"]+)"', block)
                    if hostname_match:
                        lease_info["hostname"] = hostname_match.group(1)
                    
                    start_match = re.search(r'starts\s+\d+\s+([^;]+)', block)
                    if start_match:
                        lease_info["starts"] = start_match.group(1).strip()
                    
                    end_match = re.search(r'ends\s+\d+\s+([^;]+)', block)
                    if end_match:
                        lease_info["ends"] = end_match.group(1).strip()
                    
                    binding_match = re.search(r'binding\s+state\s+(\w+)', block)
                    if binding_match:
                        lease_info["state"] = binding_match.group(1)
                    
                    leases.append(lease_info)
                
                leases = [l for l in leases if l.get("state") == "active"]
                
            except Exception as e:
                logger.error(f"Error parsing leases: {e}")
        
        return {
            "installed": service_installed,
            "service_status": service_status,
            "interfaces": interfaces,
            "subnets": config.get("subnets", {}),
            "static_leases": config.get("static_leases", []),
            "active_leases": leases
        }
        
    except Exception as e:
        logger.error(f"Error getting DHCP status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/system/dhcp/configure")
def configure_dhcp(request: DHCPSubnetRequest, auth: dict = Depends(require_auth)):
    """Configure DHCP server for an interface"""
    try:
        netplan_data = scan_netplan_files()
        
        interface_ip = None
        if request.interface in netplan_data["interfaces"]:
            addrs = netplan_data["interfaces"][request.interface]["config"].get("addresses", [])
            if addrs:
                interface_ip = addrs[0]
        
        if not interface_ip and request.enabled:
            runtime = get_interface_runtime_info(request.interface)
            if runtime["ipv4"]:
                ip = runtime["ipv4"][0]["address"]
                netmask = runtime["ipv4"][0]["netmask"]
                import ipaddress
                cidr = ipaddress.ip_network(f"0.0.0.0/{netmask}").prefixlen
                interface_ip = f"{ip}/{cidr}"
        
        if not interface_ip and request.enabled:
            raise HTTPException(status_code=400, detail="Interface has no IP address configured. Configure IP first.")
        
        current_config = parse_dhcp_config()
        current_interfaces = get_dhcp_interfaces()
        
        if request.enabled:
            subnet_ip, netmask = calculate_subnet_from_ip(interface_ip)
            if not subnet_ip:
                raise HTTPException(status_code=400, detail="Invalid interface IP address")
            
            router_ip = interface_ip.split('/')[0]
            
            current_config["subnets"][subnet_ip] = {
                "netmask": netmask,
                "range_start": request.range_start,
                "range_end": request.range_end,
                "router": router_ip,
                "dns_servers": request.dns_servers,
                "lease_time": request.lease_time
            }
            
            if request.interface not in current_interfaces:
                current_interfaces.append(request.interface)
            
            current_config["static_leases"] = [
                l for l in current_config.get("static_leases", [])
                if not l["ip"].startswith(subnet_ip.rsplit('.', 1)[0])
            ]
            current_config["static_leases"].extend(request.static_leases)
            
        else:
            if interface_ip:
                subnet_ip, _ = calculate_subnet_from_ip(interface_ip)
                if subnet_ip and subnet_ip in current_config.get("subnets", {}):
                    del current_config["subnets"][subnet_ip]
            
            current_interfaces = [i for i in current_interfaces if i != request.interface]
        
        if os.path.exists(DHCP_CONFIG_FILE):
            shutil.copy2(DHCP_CONFIG_FILE, f"{DHCP_CONFIG_FILE}.bak")
        
        new_config = generate_dhcp_config(
            current_config.get("subnets", {}),
            current_config.get("static_leases", [])
        )
        
        with open(DHCP_CONFIG_FILE, 'w') as f:
            f.write(new_config)
        
        interfaces_content = f'INTERFACESv4="{" ".join(current_interfaces)}"\n'
        with open(DHCP_DEFAULT_FILE, 'w') as f:
            f.write(interfaces_content)
        
        if current_interfaces and any(current_config.get("subnets", {}).values()):
            subprocess.run(["systemctl", "restart", "isc-dhcp-server"], check=True)
            service_action = "restarted"
        else:
            subprocess.run(["systemctl", "stop", "isc-dhcp-server"], capture_output=True)
            service_action = "stopped"
        
        return {
            "status": "success",
            "message": f"DHCP configuration updated. Service {service_action}.",
            "interfaces": current_interfaces,
            "subnets": list(current_config.get("subnets", {}).keys())
        }
        
    except HTTPException:
        raise
    except subprocess.CalledProcessError as e:
        logger.error(f"Error configuring DHCP: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to restart DHCP service: {e.stderr if e.stderr else str(e)}")
    except Exception as e:
        logger.error(f"Error configuring DHCP: {e}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/system/dhcp/lease/{ip}")
def delete_dhcp_lease(ip: str, auth: dict = Depends(require_auth)):
    """Delete a static DHCP lease"""
    try:
        current_config = parse_dhcp_config()
        
        current_config["static_leases"] = [
            l for l in current_config.get("static_leases", [])
            if l["ip"] != ip
        ]
        
        new_config = generate_dhcp_config(
            current_config.get("subnets", {}),
            current_config.get("static_leases", [])
        )
        
        with open(DHCP_CONFIG_FILE, 'w') as f:
            f.write(new_config)
        
        subprocess.run(["systemctl", "restart", "isc-dhcp-server"], capture_output=True)
        
        return {"status": "success", "message": f"Static lease for {ip} removed"}
        
    except Exception as e:
        logger.error(f"Error deleting lease: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Channel Endpoints
# =============================================================================

@app.get("/channels")
def get_channels():
    """Retrieve all channels with their current status"""
    config = load_config()
    for channel_id in config["channels"]:
        status, last_error = get_channel_status(channel_id)
        config["channels"][channel_id]["status"] = status
        config["channels"][channel_id]["last_error"] = last_error
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


class BulkOperationRequest(BaseModel):
    channel_ids: List[str]
    operation: str


@app.post("/channels/bulk")
def bulk_channel_operation(request: BulkOperationRequest):
    """Perform bulk operations on multiple channels"""
    config = load_config()
    results = {
        "success": [],
        "failed": []
    }
    
    for channel_id in request.channel_ids:
        if channel_id not in config["channels"]:
            results["failed"].append({"channel_id": channel_id, "error": "Channel not found"})
            continue
        
        try:
            if request.operation == "restart":
                subprocess.run(
                    ["systemctl", "restart", f"rist-channel-{channel_id}.service"],
                    check=True,
                    timeout=30
                )
                results["success"].append(channel_id)
                logger.info(f"Restarted channel {channel_id}")
                
            elif request.operation == "start":
                subprocess.run(["systemctl", "enable", f"rist-channel-{channel_id}.service"], check=True)
                subprocess.run(["systemctl", "start", f"rist-channel-{channel_id}.service"], check=True)
                results["success"].append(channel_id)
                logger.info(f"Started channel {channel_id}")
                
            elif request.operation == "stop":
                subprocess.run(["systemctl", "stop", f"rist-channel-{channel_id}.service"], check=True)
                subprocess.run(["systemctl", "disable", f"rist-channel-{channel_id}.service"], check=True)
                results["success"].append(channel_id)
                logger.info(f"Stopped channel {channel_id}")
                
            else:
                results["failed"].append({"channel_id": channel_id, "error": f"Unknown operation: {request.operation}"})
                
        except subprocess.CalledProcessError as e:
            results["failed"].append({"channel_id": channel_id, "error": str(e)})
            logger.error(f"Failed to {request.operation} channel {channel_id}: {e}")
        except subprocess.TimeoutExpired:
            results["failed"].append({"channel_id": channel_id, "error": "Operation timed out"})
            logger.error(f"Timeout during {request.operation} of channel {channel_id}")
    
    return {
        "status": "completed",
        "operation": request.operation,
        "results": results
    }


@app.get("/channels/{channel_id}")
def get_channel(channel_id: str):
    """Retrieve details of a specific channel"""
    config = load_config()
    if channel_id not in config["channels"]:
        raise HTTPException(status_code=404)
    channel = config["channels"][channel_id]
    status, last_error = get_channel_status(channel_id)
    channel["status"] = status
    channel["last_error"] = last_error
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
    
    status, _ = get_channel_status(channel_id)
    if status == "running":
        try:
            subprocess.run(["systemctl", "restart", f"rist-channel-{channel_id}.service"], check=True)
            channel_keepalive(channel_id)
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
        subprocess.run(["systemctl", "enable", f"rist-channel-{channel_id}.service"], check=True)
        subprocess.run(["systemctl", "start", f"rist-channel-{channel_id}.service"], check=True)
        channel_keepalive(channel_id)
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
        subprocess.run(["systemctl", "stop", f"rist-channel-{channel_id}.service"], check=True)
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
        if not is_ffmpeg_running(channel_id):
            await start_ffmpeg(channel_id)
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
    """Retrieve metrics for a specific channel with caching and error handling"""
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
    """Retrieve cached system health metrics"""
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
    """Check backup health status for a channel"""
    try:
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
    """Retrieve backup sources for a specific channel"""
    try:
        backup_config_path = "/root/rist/backup_sources.yaml"
        
        if not os.path.exists(backup_config_path):
            return {"channel_id": channel_id, "backup_sources": []}
        
        with open(backup_config_path, 'r') as f:
            backup_config = yaml.safe_load(f) or {}
        
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
    """Update backup sources for a specific channel"""
    try:
        backup_config_path = "/root/rist/backup_sources.yaml"
        
        try:
            with open(backup_config_path, 'r') as f:
                backup_config = yaml.safe_load(f) or {}
        except FileNotFoundError:
            backup_config = {}
        
        if 'channels' not in backup_config:
            backup_config['channels'] = {}
        
        clean_sources = list(dict.fromkeys(
            [source.strip() for source in backup_sources if source.strip()]
        ))
        
        if clean_sources:
            backup_config['channels'][channel_id] = clean_sources
        elif channel_id in backup_config['channels']:
            del backup_config['channels'][channel_id]
        
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
    """Download combined configuration backup"""
    try:
        receiver_config = load_config()
        
        backup_sources = {}
        if os.path.exists(BACKUP_SOURCES_PATH):
            with open(BACKUP_SOURCES_PATH, 'r') as f:
                backup_sources = yaml.safe_load(f) or {}
        
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
    """Check if .bak files exist for potential revert"""
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
    """Restore configuration from backup"""
    try:
        logger.info("Starting configuration restore")
        
        if "receiver_config" not in backup_data:
            raise HTTPException(status_code=400, detail="Invalid backup: missing receiver_config")
        
        current_config = load_config()
        for channel_id in current_config.get("channels", {}):
            status, _ = get_channel_status(channel_id)
            if status == "running":
                logger.info(f"Stopping channel {channel_id} for restore")
                try:
                    subprocess.run(["systemctl", "stop", f"rist-channel-{channel_id}.service"], check=True)
                except subprocess.CalledProcessError as e:
                    logger.warning(f"Failed to stop channel {channel_id}: {e}")
        
        logger.info("Creating backup of current configuration")
        
        if os.path.exists(CONFIG_FILE):
            shutil.copy2(CONFIG_FILE, f"{CONFIG_FILE}.bak")
        
        if os.path.exists(BACKUP_SOURCES_PATH):
            shutil.copy2(BACKUP_SOURCES_PATH, f"{BACKUP_SOURCES_PATH}.bak")
        
        new_channels = backup_data["receiver_config"].get("channels", {})
        for channel_id in current_config.get("channels", {}):
            if channel_id not in new_channels:
                logger.info(f"Removing channel {channel_id} (not in backup)")
                for service in [f"rist-channel-{channel_id}.service", f"ffmpeg-{channel_id}.service"]:
                    service_path = f"{SERVICE_DIR}/{service}"
                    try:
                        subprocess.run(["systemctl", "disable", service], capture_output=True)
                        if os.path.exists(service_path):
                            os.remove(service_path)
                    except Exception as e:
                        logger.warning(f"Failed to remove service {service}: {e}")
        
        save_config(backup_data["receiver_config"])
        
        backup_sources = backup_data.get("backup_sources", {"channels": {}})
        with open(BACKUP_SOURCES_PATH, 'w') as f:
            yaml.safe_dump(backup_sources, f)
        
        for channel_id, channel_data in new_channels.items():
            logger.info(f"Generating service files for {channel_id}")
            generate_service_file(channel_id, channel_data)
        
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
    """Revert configuration to .bak files"""
    try:
        logger.info("Starting configuration revert")
        
        if not os.path.exists(f"{CONFIG_FILE}.bak"):
            raise HTTPException(status_code=404, detail="No backup found to revert to")
        
        current_config = load_config()
        for channel_id in current_config.get("channels", {}):
            status, _ = get_channel_status(channel_id)
            if status == "running":
                logger.info(f"Stopping channel {channel_id} for revert")
                try:
                    subprocess.run(["systemctl", "stop", f"rist-channel-{channel_id}.service"], check=True)
                except subprocess.CalledProcessError as e:
                    logger.warning(f"Failed to stop channel {channel_id}: {e}")
        
        with open(f"{CONFIG_FILE}.bak", 'r') as f:
            bak_config = yaml.safe_load(f)
        
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
        
        shutil.copy2(f"{CONFIG_FILE}.bak", CONFIG_FILE)
        
        if os.path.exists(f"{BACKUP_SOURCES_PATH}.bak"):
            shutil.copy2(f"{BACKUP_SOURCES_PATH}.bak", BACKUP_SOURCES_PATH)
        
        for channel_id, channel_data in bak_channels.items():
            logger.info(f"Regenerating service files for {channel_id}")
            generate_service_file(channel_id, channel_data)
        
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
