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

install_python_deps() {
    echo "Installing Python dependencies..."
    apt-get update
    
    # First ensure pip is installed
    apt-get install -y python3-pip

    # Try installing with apt-get first
    if ! apt-get install -y \
        python3-fastapi \
        python3-flask \
        python3-flask-cors \
        python3-psutil \
        python3-pydantic \
        python3-yaml \
        python3-uvicorn \
        python3-aiofiles; then
        
        echo "Some packages weren't available via apt. Installing missing packages via pip..."
        
        # Install packages that might have failed via pip
        pip3 install fastapi
        pip3 install "uvicorn[standard]"
    fi

    # Install GPUtil via pip as it's not available in apt
    pip3 install GPUtil
}

# Clone repository
clone_repo() {
    echo "Cloning RIST receiver repository..."
    mkdir -p /root/rist
    cd /root
    rm -rf rist/*
    git clone https://github.com/caritechsolutions/rist_receiver.git rist
    chmod -R 755 /root/rist
}

# Stop all RIST services
stop_services() {
    echo "Stopping all RIST services (if they exist)..."
    
    # Stop any running channel services
    if systemctl list-units --full --all | grep -q "rist-channel-"; then
        for service in $(systemctl list-units --full --all | grep "rist-channel-" | awk '{print $1}'); do
            echo "Stopping $service"
            systemctl stop "$service" || echo "Service $service was not running"
        done
    else
        echo "No rist-channel services found"
    fi
    
    # Stop main API service if it exists
    if systemctl list-unit-files | grep -q "rist-api"; then
        echo "Stopping rist-api service"
        systemctl stop rist-api || echo "rist-api service was not running"
    else
        echo "No rist-api service found"
    fi

    # Stop failover service if it exists
    if systemctl list-unit-files | grep -q "rist-failover"; then
        echo "Stopping rist-failover service"
        systemctl stop rist-failover || echo "rist-failover service was not running"
    else
        echo "No rist-failover service found"
    fi
    
    # Kill any remaining ffmpeg processes (pkill with || true already handles non-existent processes)
    echo "Checking for ffmpeg processes..."
    pkill -9 ffmpeg 2>/dev/null || true
    pkill -9 ristreceiver 2>/dev/null || true
    
    # Wait a moment for processes to clean up
    sleep 2
}

# Kill processes using a directory
kill_processes_using_directory() {
    local dir="$1"
    echo "Finding processes using $dir..."
    
    # Find processes using the directory
    lsof "$dir" | awk '{print $2}' | grep -v PID | while read pid; do
        if [ ! -z "$pid" ]; then
            echo "Killing process $pid using $dir"
            kill -9 "$pid" 2>/dev/null || true
        fi
    done
    
    # Wait a moment for processes to die
    sleep 2
}

# Set up web directory
setup_web() {
    echo "Setting up web directory..."
    
    # Stop services before unmounting
    stop_services
    
    # Unmount content directory if it's mounted
    if mountpoint -q /var/www/html/content; then
        echo "Unmounting content directory..."
        # Try to find and kill processes using the directory
        kill_processes_using_directory "/var/www/html/content"
        
        # Try normal unmount
        umount /var/www/html/content 2>/dev/null || {
            echo "Normal unmount failed, trying force unmount..."
            umount -f /var/www/html/content 2>/dev/null || {
                echo "Force unmount failed, trying lazy unmount..."
                umount -l /var/www/html/content 2>/dev/null || {
                    echo "WARNING: Could not unmount directory. Continuing anyway..."
                }
            }
        }
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

# Set up API service
setup_service() {
    echo "Setting up systemd services..."
    
    echo "Creating log directory..."
    mkdir -p /var/log/ristreceiver
    chmod 755 /var/log/ristreceiver
    
    echo "Installing service files..."
    cp /root/rist/services/rist-api.service /etc/systemd/system/
    cp /root/rist/services/rist-failover.service /etc/systemd/system/
    
    # Update service file to use correct path
    echo "Updating service paths..."
    sed -i 's|/opt/rist_receiver|/root/rist|g' /etc/systemd/system/rist-api.service
    
    echo "Reloading systemd..."
    systemctl daemon-reload
    systemctl enable rist-api.service
    systemctl enable rist-failover.service
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
        echo "Checking service status..."
        systemctl status rist-api --no-pager
        systemctl start rist-failover
        echo "Checking service status..."
        systemctl status rist-failover --no-pager
    else
        echo "ERROR: Config file not found, cannot start rist-api!"
        exit 1
    fi
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
