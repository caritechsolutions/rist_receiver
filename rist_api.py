from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import yaml
import subprocess
import os
import requests
from typing import Dict, Optional, List
import logging
import json
from datetime import datetime
import time
from functools import lru_cache
import traceback
import psutil
import GPUtil

# Global variable to store previous network stats for bandwidth calculation
previous_network_stats = {}
previous_network_timestamp = 0

# Comprehensive Logging Configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("/var/log/rist-api.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = FastAPI(title="RIST Receiver API")
CONFIG_FILE = "receiver_config.yaml"
SERVICE_DIR = "/etc/systemd/system"

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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
        logger.info(f"Fetching metrics for channel {channel_id} from port {metrics_port}")
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
    """Generate systemd service file for a RIST channel"""
    input_url = channel["input"]
    if channel["settings"].get("virt_src_port"):
        input_url += f"?virt-dst-port={channel['settings']['virt_src_port']}"

    log_port = channel['metrics_port'] + 1000
    stream_path = f"/var/www/html/content/{channel['name']}"

    # Construct command parts for ristreceiver
    cmd_parts = [
        "/usr/local/bin/ristreceiver",
        f"-p {channel['settings']['profile']}",
        f"-i '{input_url}'",
        f"-o '{channel['output']}'",
        "-v 6",
        "-M",
        "--metrics-http",
        f"--metrics-port={channel['metrics_port']}",
        "-S 1000",
        f"-r localhost:{log_port}"
    ]

    # Add optional parameters
    if channel["settings"].get("buffer"):
        cmd_parts.append(f"-b {channel['settings']['buffer']}")
    
    if channel["settings"].get("encryption_type"):
        cmd_parts.append(f"-e {channel['settings']['encryption_type']}")
    
    if channel["settings"].get("secret"):
        cmd_parts.append(f"-s {channel['settings']['secret']}")

    # Create systemd service content
    service_content = f"""[Unit]
Description=RIST Channel {channel['name']}
After=network.target

[Service]
Type=simple
ExecStartPre=/bin/mkdir -p {stream_path}
ExecStart=/bin/bash -c 'exec {' '.join(cmd_parts)} & ffmpeg -i {channel["output"]} -c copy -hls_time 5 -hls_list_size 5 -hls_flags delete_segments {stream_path}/playlist.m3u8'
Restart=always
RestartSec=3
StandardOutput=append:/var/log/ristreceiver/receiver_{channel_id}.log
StandardError=append:/var/log/ristreceiver/receiver_{channel_id}.log

[Install]
WantedBy=multi-user.target
"""

    service_file = f"{SERVICE_DIR}/rist-channel-{channel_id}.service"
    try:
        with open(service_file, "w") as f:
            f.write(service_content)
    except Exception as e:
        logger.error(f"Failed to write service file: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to create service file")

    reload_systemd()
    
    if channel['enabled']:
        try:
            subprocess.run(["systemctl", "enable", f"rist-channel-{channel_id}.service"], 
                         check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to enable service: {e.stderr.decode()}")
            raise HTTPException(status_code=500, detail="Failed to enable service")

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
    
    channel_dict = channel.dict()
    config["channels"][channel_id] = channel_dict
    save_config(config)
    
    generate_service_file(channel_id, channel_dict)
    
    try:
        subprocess.run(["systemctl", "restart", f"rist-channel-{channel_id}.service"],
                      check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, 
                          detail=f"Failed to restart service: {e.stderr.decode()}")
    
    return channel_dict

@app.delete("/channels/{channel_id}")
def delete_channel(channel_id: str):
    """Delete a channel"""
    config = load_config()
    if channel_id not in config["channels"]:
        raise HTTPException(status_code=404)
    
    service_name = f"rist-channel-{channel_id}.service"
    try:
        subprocess.run(["systemctl", "stop", service_name], check=True, capture_output=True)
        subprocess.run(["systemctl", "disable", service_name], check=True, capture_output=True)
        os.remove(f"{SERVICE_DIR}/{service_name}")
    except Exception as e:
        logger.error(f"Failed to remove service: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to remove service")
    
    del config["channels"][channel_id]
    save_config(config)
    return {"status": "deleted"}

@app.put("/channels/{channel_id}/start")
def start_channel(channel_id: str):
    """Start a specific channel"""
    config = load_config()
    if channel_id not in config["channels"]:
        raise HTTPException(status_code=404)
    
    service_file = f"{SERVICE_DIR}/rist-channel-{channel_id}.service"
    if not os.path.exists(service_file):
        generate_service_file(channel_id, config["channels"][channel_id])
        
    try:
        subprocess.run(["systemctl", "start", f"rist-channel-{channel_id}.service"],
                      check=True, capture_output=True)
        return {"status": "started"}
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, 
                          detail=f"Failed to start service: {e.stderr.decode()}")

@app.put("/channels/{channel_id}/stop")
def stop_channel(channel_id: str):
    """Stop a specific channel"""
    config = load_config()
    if channel_id not in config["channels"]:
        raise HTTPException(status_code=404)
    
    try:
        subprocess.run(["systemctl", "stop", f"rist-channel-{channel_id}.service"],
                      check=True, capture_output=True)
        return {"status": "stopped"}
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, 
                          detail=f"Failed to stop service: {e.stderr.decode()}")

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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)