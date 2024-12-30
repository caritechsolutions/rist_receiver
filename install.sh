#!/bin/bash

# Exit on error
set -e

echo "Starting RIST Receiver installation..."

# Function to check if script is run as root
check_root() {
    if [ "$(id -u)" != "0" ]; then
        echo "This script must be run as root" 1>&2
        exit 1
    fi
}

# Update system and install dependencies
install_dependencies() {
    echo "Updating system and installing dependencies..."
    apt-get update
    apt-get upgrade -y
    
    # Install git if not present
    apt-get install -y git
    
    # Install system packages from repository
    # Base system packages that are definitely in apt
    apt-get install -y python3 python3-pip python3-dev python3-psutil python3-yaml build-essential \
    cmake pkg-config meson ninja-build graphviz nginx redis-server libmicrohttpd-dev ffmpeg
}

# Build and install librist
build_librist() {
    echo "Building and installing librist..."
    cd /tmp
    git clone https://code.videolan.org/rist/librist.git
    cd librist
    
    # CMake build
    mkdir -p build
    cd build
    cmake ..
    make -j$(nproc)
    make install
    ldconfig
    
    # Meson build
    cd ..
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
    wget -O /tmp/requirements.txt https://raw.githubusercontent.com/caritechsolutions/rist_receiver/main/requirements.txt
    pip3 install -r /tmp/requirements.txt
}

# Clone repository
clone_repo() {
    echo "Cloning RIST receiver repository..."
    cd /opt
    git clone https://github.com/caritechsolutions/rist_receiver.git
    cd rist_receiver
}

# Set up web directory
setup_web() {
    echo "Setting up web directory..."
    rm -rf /var/www/html/*
    cp -r /opt/rist_receiver/web/* /var/www/html/
    
    # Create content directory if it doesn't exist
    mkdir -p /var/www/html/content
    
    # Set permissions
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
    cp /opt/rist_receiver/services/rist-api.service /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable rist-api.service
}

# Start services
start_services() {
    echo "Starting services..."
    systemctl restart nginx
    systemctl start rist-api
    systemctl restart redis-server
}

# Main installation flow
main() {
    check_root
    install_dependencies
    build_librist
    install_python_deps
    clone_repo
    setup_web
    setup_tmpfs
    setup_service
    start_services
    
    echo "Installation completed successfully!"
    echo "Please check service status with: systemctl status rist-api"
}

# Run main installation
main