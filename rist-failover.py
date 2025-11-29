#!/usr/bin/env python3
import yaml
import requests
import time
import logging
from logging.handlers import RotatingFileHandler
import sys
import traceback
import json
import os

# Logging Configuration with rotation
log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# File handler with rotation - max 5MB, keep 3 backups
file_handler = RotatingFileHandler(
    "/root/rist/rist-failover.log",
    maxBytes=5*1024*1024,  # 5MB
    backupCount=3
)
file_handler.setFormatter(log_formatter)
file_handler.setLevel(logging.INFO)

# Console handler - only warnings and above
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(log_formatter)
console_handler.setLevel(logging.WARNING)

# Configure logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.addHandler(file_handler)
logger.addHandler(console_handler)

class RISTFailover:
    def __init__(self, 
                 api_base_url='http://localhost:5000', 
                 main_config_path='/root/rist/receiver_config.yaml', 
                 backup_config_path='/root/rist/backup_sources.yaml'):
        """
        Initialize failover manager
        """
        self.api_base_url = api_base_url
        self.main_config_path = main_config_path
        self.backup_config_path = backup_config_path
        
        # Track current backup index for each channel
        self.channel_backup_index = {}
        
        # Ensure /root/rist directory exists
        os.makedirs('/root/rist', exist_ok=True)
    
    def cleanup_health_status_files(self, running_channels):
        """
        Remove health status files for non-running channels
        """
        try:
            # Check for existing health status files
            for filename in os.listdir('/root/rist'):
                if filename.endswith('_backup_health.json'):
                    channel_id = filename.replace('_backup_health.json', '')
                    
                    # If channel is not running, remove its health status file
                    if channel_id not in running_channels:
                        try:
                            file_path = os.path.join('/root/rist', filename)
                            os.remove(file_path)
                            logger.info(f"Removed health status file for non-running channel: {channel_id}")
                        except Exception as e:
                            logger.error(f"Failed to remove health status file {filename}: {e}")
        
        except Exception as e:
            logger.error(f"Error in health status file cleanup: {e}")
    
    def write_channel_health_status(self, channel_id, has_backups, is_healthy):
        """
        Write health status for a channel to a JSON file
        """
        try:
            health_status = {
                'channel_id': channel_id,
                'has_backups': has_backups,
                'is_healthy': is_healthy,
                'last_checked': time.time()
            }
            
            health_status_file = f"/root/rist/{channel_id}_backup_health.json"
            # Only log at debug level for routine healthy status
            if not is_healthy:
                logger.debug(f"{channel_id} health status: healthy={is_healthy}, has_backups={has_backups}")
            
            with open(health_status_file, 'w') as f:
                json.dump(health_status, f)
        except Exception as e:
            logger.error(f"Failed to write health status for {channel_id}: {e}")
    
    def get_running_channels(self):
        """
        Retrieve list of running channels from API
        """
        try:
            response = requests.get(f'{self.api_base_url}/channels')
            running_channels = [
                channel_id for channel_id, details in response.json().items() 
                if details.get('status') == 'running'
            ]
            logger.debug(f"Running channels: {running_channels}")
            return running_channels
        except Exception as e:
            logger.error(f"Failed to retrieve channels: {e}")
            return []
    
    def check_channel_metrics(self, channel_id):
        """
        Check metrics for a specific channel
        """
        try:
            response = requests.get(f'{self.api_base_url}/channels/{channel_id}/metrics')
            metrics = response.json()
        
            # Check if channel is unhealthy
            is_unhealthy = metrics['peers'] == 0 or metrics['quality'] < 50
        
            if is_unhealthy:
                logger.warning(f"Channel {channel_id} is unhealthy. Peers: {metrics['peers']}, Quality: {metrics['quality']}")
            
                # Check backup sources
                try:
                    with open(self.backup_config_path, 'r') as f:
                        backup_config = yaml.safe_load(f) or {}
                
                    backup_sources = backup_config.get('channels', {}).get(channel_id, [])
                    has_backups = len(backup_sources) > 0
                
                    # Write health status immediately when unhealthy
                    self.write_channel_health_status(channel_id, has_backups, False)
                except Exception:
                    # Write health status even if can't read backup sources
                    self.write_channel_health_status(channel_id, False, False)
        
            return is_unhealthy
    
        except Exception as e:
            logger.error(f"Failed to retrieve metrics for {channel_id}: {e}")
        
            # Write health status if metrics retrieval fails
            self.write_channel_health_status(channel_id, False, False)
        
            return True
    
    def get_current_input_url(self, channel_id):
        """
        Get the current input URL for a channel
        """
        try:
            response = requests.get(f'{self.api_base_url}/channels/{channel_id}')
            return response.json().get('input')
        except Exception as e:
            logger.error(f"Failed to retrieve input URL for {channel_id}: {e}")
            return None
    
    def update_backup_sources(self, channel_id, current_input_url):
        """
        Update backup sources configuration
        """
        try:
            # Read existing backup sources
            with open(self.backup_config_path, 'r') as f:
                backup_config = yaml.safe_load(f) or {'channels': {}}
            
            # Ensure channel exists in config
            if channel_id not in backup_config['channels']:
                backup_config['channels'][channel_id] = []
            
            # Append current input URL if not already present
            if current_input_url and current_input_url not in backup_config['channels'][channel_id]:
                backup_config['channels'][channel_id].append(current_input_url)
            
            # Save updated config
            with open(self.backup_config_path, 'w') as f:
                yaml.safe_dump(backup_config, f)
            
            # Initialize or increment backup index
            if channel_id not in self.channel_backup_index:
                self.channel_backup_index[channel_id] = 0
            else:
                # Move to next backup source, wrap around if needed
                self.channel_backup_index[channel_id] = (
                    self.channel_backup_index[channel_id] + 1
                ) % len(backup_config['channels'][channel_id])
            
            # Return next backup source
            return backup_config['channels'][channel_id][self.channel_backup_index[channel_id]]
        
        except Exception as e:
            logger.error(f"Failed to update backup sources for {channel_id}: {e}")
            return None
    
    def update_channel_input(self, channel_id, new_input):
        """
        Update channel input via API
        """
        try:
            # Retrieve current channel configuration
            response = requests.get(f'{self.api_base_url}/channels/{channel_id}')
            channel_config = response.json()
            
            # Update input
            channel_config['input'] = new_input
            
            # Update via API
            response = requests.put(
                f'{self.api_base_url}/channels/{channel_id}', 
                json=channel_config
            )
            
            if response.status_code == 200:
                logger.warning(f"Successfully changed {channel_id} input to: {new_input}")
                return True
            else:
                logger.error(f"Failed to update {channel_id}: {response.text}")
                return False
        
        except Exception as e:
            logger.error(f"Error updating channel {channel_id}: {e}")
            return False
    
    def run_failover(self):
        """
        Main failover checking method
        """
        logger.info("Failover monitoring started")
        
        while True:
            # Get running channels
            running_channels = self.get_running_channels()
        
            # Clean up health status files for non-running channels
            self.cleanup_health_status_files(running_channels)
        
            # Check each channel
            for channel_id in running_channels:
                # Check if channel has backup sources
                try:
                    with open(self.backup_config_path, 'r') as f:
                        backup_config = yaml.safe_load(f) or {}
                
                    backup_sources = backup_config.get('channels', {}).get(channel_id, [])
                    has_backups = len(backup_sources) > 0
                except Exception:
                    has_backups = False
            
                # First check to determine if further checks are needed
                is_first_check_unhealthy = self.check_channel_metrics(channel_id)
            
                # If first check is unhealthy, do 2 more checks
                if is_first_check_unhealthy:
                    unhealthy_checks = 1  # First check was unhealthy
                    for _ in range(2):
                        time.sleep(3)  # 3-second pause between checks
                        if self.check_channel_metrics(channel_id):
                            unhealthy_checks += 1
                
                    # Write health status 
                    self.write_channel_health_status(channel_id, has_backups, unhealthy_checks < 3)
                
                    # If consistently unhealthy, perform failover
                    if unhealthy_checks == 3 and has_backups:
                        # Get current input URL
                        current_input_url = self.get_current_input_url(channel_id)
                    
                        if current_input_url:
                            # Update backup sources and get new input
                            new_input = self.update_backup_sources(channel_id, current_input_url)
                        
                            if new_input:
                                # Update channel input
                                self.update_channel_input(channel_id, new_input)
                else:
                    # If first check is healthy, write healthy status
                    self.write_channel_health_status(channel_id, has_backups, True)
            
            # Wait 5 seconds before next iteration
            time.sleep(5)

def main():
    """
    Main execution method for failover script
    """
    failover_manager = RISTFailover()
    
    logger.info("Starting RIST Channel Failover")
    
    try:
        failover_manager.run_failover()
    except Exception as e:
        logger.error(f"Critical error in failover script: {e}")
        logger.error(traceback.format_exc())

if __name__ == "__main__":
    main()
