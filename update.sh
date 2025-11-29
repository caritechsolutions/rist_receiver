#!/bin/bash

# RIST Receiver Update Script
# This script updates only the code files (API, web interface)
# It preserves: configs, services, and channel data
#
# Usage:
#   Interactive:  ./update.sh
#   Auto-confirm: ./update.sh -y
#   Via curl:     curl -sSL "https://raw.githubusercontent.com/.../update.sh" | sudo bash -s -- -y

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Parse arguments
AUTO_CONFIRM=false
while getopts "y" opt; do
    case $opt in
        y) AUTO_CONFIRM=true ;;
        *) ;;
    esac
done

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}   RIST Receiver Update Script${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""

# Function to check if script is run as root
check_root() {
    if [ "$(id -u)" != "0" ]; then
        echo -e "${RED}This script must be run as root${NC}" 1>&2
        exit 1
    fi
}

# Function to check if RIST is installed
check_installation() {
    if [ ! -d "/root/rist" ]; then
        echo -e "${RED}Error: RIST Receiver is not installed.${NC}"
        echo -e "Please run the installation script first."
        exit 1
    fi
    
    if [ ! -f "/root/rist/receiver_config.yaml" ]; then
        echo -e "${YELLOW}Warning: No configuration file found.${NC}"
    fi
}

# Backup current code (not configs)
backup_current() {
    echo -e "${YELLOW}Creating backup of current code...${NC}"
    
    BACKUP_DIR="/root/rist_backup_$(date +%Y%m%d_%H%M%S)"
    mkdir -p "$BACKUP_DIR"
    
    # Backup code files only (not configs)
    [ -f "/root/rist/rist_api.py" ] && cp /root/rist/rist_api.py "$BACKUP_DIR/"
    [ -f "/root/rist/rist-failover.py" ] && cp /root/rist/rist-failover.py "$BACKUP_DIR/"
    [ -d "/var/www/html" ] && cp -r /var/www/html "$BACKUP_DIR/web_backup"
    
    echo -e "${GREEN}Backup created at: $BACKUP_DIR${NC}"
}

# Download latest code
download_latest() {
    echo -e "${YELLOW}Downloading latest code from repository...${NC}"
    
    # Create temp directory
    TEMP_DIR=$(mktemp -d)
    cd "$TEMP_DIR"
    
    # Clone the repo
    git clone --quiet https://github.com/caritechsolutions/rist_receiver.git
    
    echo -e "${GREEN}Download complete.${NC}"
}

# Update API files
update_api() {
    echo -e "${YELLOW}Updating API files...${NC}"
    
    # Update main API file
    if [ -f "$TEMP_DIR/rist_receiver/rist_api.py" ]; then
        cp "$TEMP_DIR/rist_receiver/rist_api.py" /root/rist/rist_api.py
        echo -e "  Updated: rist_api.py"
    fi
    
    # Update failover script
    if [ -f "$TEMP_DIR/rist_receiver/rist-failover.py" ]; then
        cp "$TEMP_DIR/rist_receiver/rist-failover.py" /root/rist/rist-failover.py
        echo -e "  Updated: rist-failover.py"
    fi
    
    # Set permissions
    chmod +x /root/rist/*.py
    
    echo -e "${GREEN}API files updated.${NC}"
}

# Update web interface
update_web() {
    echo -e "${YELLOW}Updating web interface...${NC}"
    
    # List of web files to update
    WEB_FILES=("index.html" "stats.html" "dash.html" "edit.html" "channel_config.html" "login.html")
    
    for file in "${WEB_FILES[@]}"; do
        if [ -f "$TEMP_DIR/rist_receiver/web/$file" ]; then
            cp "$TEMP_DIR/rist_receiver/web/$file" /var/www/html/
            echo -e "  Updated: $file"
        fi
    done
    
    # Preserve content directory (HLS streams)
    # Don't touch /var/www/html/content/
    
    # Set permissions
    chmod -R 755 /var/www/html
    
    echo -e "${GREEN}Web interface updated.${NC}"
}

# Update Python dependencies if requirements changed
update_dependencies() {
    echo -e "${YELLOW}Checking for new dependencies...${NC}"
    
    if [ -f "$TEMP_DIR/rist_receiver/requirements.txt" ]; then
        # Check pip version for --break-system-packages flag
        PIP_VERSION=$(pip3 --version | awk '{print $2}')
        PIP_MAJOR=$(echo $PIP_VERSION | cut -d. -f1)
        
        if [ "$PIP_MAJOR" -ge 23 ]; then
            pip3 install -r "$TEMP_DIR/rist_receiver/requirements.txt" --break-system-packages --quiet 2>/dev/null || true
        else
            pip3 install -r "$TEMP_DIR/rist_receiver/requirements.txt" --quiet 2>/dev/null || true
        fi
    fi
    
    echo -e "${GREEN}Dependencies checked.${NC}"
}

# Restart services
restart_services() {
    echo -e "${YELLOW}Restarting services...${NC}"
    
    # Restart API service
    if systemctl is-active --quiet rist-api; then
        systemctl restart rist-api
        echo -e "  Restarted: rist-api"
    fi
    
    # Restart failover service
    if systemctl is-active --quiet rist-failover; then
        systemctl restart rist-failover
        echo -e "  Restarted: rist-failover"
    fi
    
    # Restart nginx
    if systemctl is-active --quiet nginx; then
        systemctl reload nginx
        echo -e "  Reloaded: nginx"
    fi
    
    echo -e "${GREEN}Services restarted.${NC}"
}

# Cleanup temp files
cleanup() {
    echo -e "${YELLOW}Cleaning up...${NC}"
    
    if [ -n "$TEMP_DIR" ] && [ -d "$TEMP_DIR" ]; then
        rm -rf "$TEMP_DIR"
    fi
    
    echo -e "${GREEN}Cleanup complete.${NC}"
}

# Show what will be updated
show_plan() {
    echo -e "${YELLOW}This script will update:${NC}"
    echo -e "  - /root/rist/rist_api.py"
    echo -e "  - /root/rist/rist-failover.py"
    echo -e "  - /var/www/html/*.html (web interface)"
    echo ""
    echo -e "${GREEN}This script will NOT touch:${NC}"
    echo -e "  - /root/rist/receiver_config.yaml (your channel config)"
    echo -e "  - /root/rist/backup_sources.yaml (your backup sources)"
    echo -e "  - /etc/systemd/system/rist-*.service (service files)"
    echo -e "  - /var/www/html/content/ (HLS streams)"
    echo ""
}

# Main update flow
main() {
    check_root
    check_installation
    
    show_plan
    
    # Prompt for confirmation (skip if -y flag or non-interactive)
    if [ "$AUTO_CONFIRM" = false ]; then
        # Check if we're in an interactive terminal
        if [ -t 0 ]; then
            read -p "Do you want to continue with the update? (y/N): " confirm
            if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
                echo -e "${YELLOW}Update cancelled.${NC}"
                exit 0
            fi
        else
            echo -e "${YELLOW}Non-interactive mode detected. Use -y flag to auto-confirm.${NC}"
            echo -e "Example: curl -sSL \"https://...update.sh\" | sudo bash -s -- -y"
            exit 0
        fi
    fi
    
    echo ""
    backup_current
    download_latest
    update_api
    update_web
    update_dependencies
    restart_services
    cleanup
    
    echo ""
    echo -e "${GREEN}========================================${NC}"
    echo -e "${GREEN}   Update Complete!${NC}"
    echo -e "${GREEN}========================================${NC}"
    echo ""
    echo -e "Your configuration and channels have been preserved."
    echo -e "Check the service status with: ${YELLOW}systemctl status rist-api${NC}"
    echo ""
}

# Run main function
main
