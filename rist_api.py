from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import yaml
import subprocess
import os
import requests
from typing import Dict, Optional
import logging
import json
from datetime import datetime

logging.basicConfig(level=logging.INFO)
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

    return metrics

def reload_systemd():
    try:
        subprocess.run(["systemctl", "daemon-reload"], check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to reload systemd: {e.stderr.decode()}")
        raise HTTPException(status_code=500, detail="Failed to reload systemd")

def generate_service_file(channel_id: str, channel: Dict):
    input_url = channel["input"]
    if channel["settings"].get("virt_src_port"):
        input_url += f"?virt-dst-port={channel['settings']['virt_src_port']}"

    log_port = channel['metrics_port'] + 1000
    stream_path = f"/var/www/html/content/{channel['name']}"

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

    if channel["settings"].get("buffer"):
        cmd_parts.append(f"-b {channel['settings']['buffer']}")
    
    if channel["settings"].get("encryption_type"):
        cmd_parts.append(f"-e {channel['settings']['encryption_type']}")
    
    if channel["settings"].get("secret"):
        cmd_parts.append(f"-s {channel['settings']['secret']}")

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
    max_port = 9200
    for channel in config["channels"].values():
        if channel.get("metrics_port", 0) > max_port:
            max_port = channel["metrics_port"]
    return max_port + 1

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {"channels": {}}
    with open(CONFIG_FILE, 'r') as f:
        return yaml.safe_load(f)

def save_config(config):
    with open(CONFIG_FILE, 'w') as f:
        yaml.dump(config, f)

@app.get("/channels")
def get_channels():
    config = load_config()
    for channel_id in config["channels"]:
        config["channels"][channel_id]["status"] = get_channel_status(channel_id)
    return config["channels"]

@app.get("/channels/next")
def get_next_channel_info():
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
    config = load_config()
    if channel_id not in config["channels"]:
        raise HTTPException(status_code=404)
    channel = config["channels"][channel_id]
    channel["status"] = get_channel_status(channel_id)
    return channel

@app.post("/channels/{channel_id}")
def create_channel(channel_id: str, channel: Channel):
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
    config = load_config()
    if channel_id not in config["channels"]:
        raise HTTPException(status_code=404)
    
    channel = config["channels"][channel_id]
    try:
        response = requests.get(f"http://localhost:{channel['metrics_port']}/metrics")
        return parse_prometheus_metrics(response.text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/channels/{channel_id}/media-info")
def get_channel_media_info(channel_id: str):
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
        raise HTTPException(status_code=500, detail=f"FFprobe error: {e.stderr}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Failed to parse FFprobe output")

@app.get("/health")
def health_check():
    config = load_config()
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "channels": len(config["channels"])
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)