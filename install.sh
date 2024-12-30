#!/bin/bash

# Exit on error
set -e

# Clean up any previous installation attempts
rm -rf /tmp/librist
rm -rf /root/rist

echo "Starting RIST Receiver installation..."

# Function to check if script is run as root
check_root() {
    if [ "$(id -u)" != "0" ]; then
        echo "This script must be run as root" 1>&2
        exit 1
    fi
}

# Function to download with cache bypass
download_file() {
    local url="$1"
    local output="$2"
    local timestamp=$(date +%s)
    curl -sSL "${url}?v=${timestamp}" -o "$output"
}

# Update system and install dependencies
install_dependencies() {
    echo "Updating system and installing dependencies..."
    apt-get update
    apt-get upgrade -y
    
    # Install git if not present
    apt-get install -y git
    
    # Install system packages
    apt-get install -y python3 python3-pip python3-dev python3-psutil python3-yaml build-essential \
    cmake pkg-config meson ninja-build graphviz nginx redis-server libmicrohttpd-dev ffmpeg
}

# Build and install librist
build_librist() {
    echo "Building and installing librist..."
    cd /tmp
    
    # Clean up any existing librist directory
    if [ -d "librist" ]; then
        echo "Removing existing librist directory..."
        rm -rf librist
    fi
    
    git clone https://code.videolan.org/rist/librist.git
    cd librist
    
    # Configure and build with meson/ninja
    meson setup build
    cd build
    ninja
    ninja install
    ldconfig
    
    cd /tmp
}

# Install Python dependencies
install_python_deps() {
    echo "Installing Python dependencies..."
    download_file "https://raw.githubusercontent.com/caritechsolutions/rist_receiver/main/requirements.txt" "/tmp/requirements.txt"
    pip3 install -r /tmp/requirements.txt
}

# Clone repository and setup config
clone_repo() {
    echo "Cloning RIST receiver repository..."
    mkdir -p /root/rist
    cd /root
    rm -rf rist/*
    git clone https://github.com/caritechsolutions/rist_receiver.git rist
    
    echo "Setting up permissions..."
    chmod -R 755 /root/rist
    
    echo "Verifying receiver_config..."
    if [ -f "/root/rist/receiver_config" ]; then
        echo "receiver_config found"
        cat /root/rist/receiver_config
    else
        echo "WARNING: receiver_config not found!"
        ls -la /root/rist/
    fi
}

# Set up web directory
setup_web() {
    echo "Setting up web directory..."
    
    # Unmount content directory if it's mounted
    if mountpoint -q /var/www/html/content; then
        echo "Unmounting existing content directory..."
        umount /var/www/html/content
    fi
    
    echo "Clearing web directory..."
    rm -rf /var/www/html/*
    
    echo "Copying web files..."
    cp -r /root/rist/web/* /var/www/html/
    
    # Create content directory if it doesn't exist
    echo "Creating content directory..."
    mkdir -p /var/www/html/content
    
    # Set permissions
    echo "Setting web permissions..."
    chmod -R 777 /var/www/html
}

# Configure tmpfs mount
setup_tmpfs() {
    echo "Setting up tmpfs mount..."
    
    # Unmount if already mounted
    if mountpoint -q /var/www/html/content; then
        umount /var/www/html/content
    fi
    
    # Mount tmpfs
    mount -t tmpfs -o size=1G tmpfs /var/www/html/content
    
    # Add to fstab if not already present
    if ! grep -q "/var/www/html/content" /etc/fstab; then
        echo "tmpfs   /var/www/html/content   tmpfs   defaults,size=1G   0   0" >> /etc/fstab
    fi
}

# Set up API service
setup_service() {
    echo "Setting up systemd service..."
    
    echo "Creating log directory..."
    mkdir -p /var/log/ristreceiver
    chmod 755 /var/log/ristreceiver
    
    echo "Installing service file..."
    cp /root/rist/services/rist-api.service /etc/systemd/system/
    
    # Update service file to use correct path
    echo "Updating service paths..."
    sed -i 's|/opt/rist_receiver|/root/rist|g' /etc/systemd/system/rist-api.service
    
    echo "Reloading systemd..."
    systemctl daemon-reload
    systemctl enable rist-api.service
}

# Start services
start_services() {
    echo "Starting services..."
    echo "Starting nginx and redis..."
    systemctl restart nginx
    systemctl restart redis-server
    
    echo "Ensuring config is in place before starting API..."
    if [ -f "/root/rist/receiver_config.yaml" ]; then
        echo "Starting rist-api service..."
        systemctl start rist-api
    else
        echo "ERROR: Config file not found, cannot start rist-api!"
        exit 1
    fi
    
    echo "Checking service status..."
    systemctl status rist-api --no-pager
    
    echo "Checking critical files and directories..."
    ls -la /root/rist/
    ls -la /var/log/ristreceiver/
    ls -la /var/www/html/
}

# Verify config
verify_config() {
    echo "Verifying configuration..."
    if [ ! -f "/root/rist/receiver_config.yaml" ]; then
        echo "ERROR: receiver_config.yaml not found!"
        exit 1
    fi
    echo "Config file found and readable:"
    cat /root/rist/receiver_config.yaml
}

# Main installation flow
main() {
    check_root
    install_dependencies
    build_librist
    install_python_deps
    clone_repo
    verify_config
    setup_web
    setup_tmpfs
    setup_service
    start_services
    
    echo "Installation completed successfully!"
    echo "Please check service status with: systemctl status rist-api"
}

# Run main installation
main