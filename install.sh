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
    
    # Check pip version to determine if --break-system-packages is supported
    PIP_VERSION=$(pip3 --version | awk '{print $2}')
    PIP_MAJOR=$(echo $PIP_VERSION | cut -d. -f1)
    PIP_MINOR=$(echo $PIP_VERSION | cut -d. -f2)
    
    if [ "$PIP_MAJOR" -gt 23 ] || ([ "$PIP_MAJOR" -eq 23 ] && [ "$PIP_MINOR" -ge 0 ]); then
        pip3 install GPUtil netifaces --break-system-packages
    else
        pip3 install GPUtil netifaces
    fi
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
    
    # Stop services before clearing web directory
    stop_services
    
    # Unmount content directory if it was mounted (from older installations)
    if mountpoint -q /var/www/html/content; then
        echo "Unmounting old content directory..."
        kill_processes_using_directory "/var/www/html/content"
        umount -l /var/www/html/content 2>/dev/null || true
    fi
    
    # Remove old fstab entry if present
    sed -i '/\/var\/www\/html\/content/d' /etc/fstab 2>/dev/null || true
    
    echo "Clearing web directory..."
    rm -rf /var/www/html/*
    
    echo "Copying web files..."
    cp -r /root/rist/web/* /var/www/html/
    
    # Set permissions
    echo "Setting web permissions..."
    chmod -R 755 /var/www/html
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

# Configure logrotate for better log management
setup_logrotate() {
    echo "Configuring logrotate for system logs..."
    
    cat > /etc/logrotate.d/rsyslog << 'EOF'
/var/log/syslog
/var/log/mail.log
/var/log/kern.log
/var/log/auth.log
/var/log/user.log
/var/log/cron.log
{
        daily
        rotate 1
        size 50M
        missingok
        notifempty
        compress
        delaycompress
        sharedscripts
        su root syslog
        postrotate
                /usr/lib/rsyslog/rsyslog-rotate
        endscript
}
EOF
    
    echo "Forcing initial log rotation..."
    logrotate -f /etc/logrotate.d/rsyslog
    
    echo "Logrotate configuration completed."
}

# New function to install and configure WireGuard
install_wireguard() {
    echo "Installing WireGuard..."
    apt-get install -y wireguard
    
    echo "Setting up WireGuard configuration..."
    mkdir -p /etc/wireguard
    
    # Create a template WireGuard configuration
    cat > /etc/wireguard/wg0.conf << 'EOF'
# WireGuard configuration template
# Replace with your actual configuration
[Interface]
PrivateKey = YOUR_PRIVATE_KEY
Address = YOUR_IP_ADDRESS/24
ListenPort = 51820

[Peer]
PublicKey = PEER_PUBLIC_KEY
AllowedIPs = 0.0.0.0/0
Endpoint = PEER_ENDPOINT:51820
EOF
    
    # Set proper permissions
    chmod 600 /etc/wireguard/wg0.conf
    
    echo ""
    echo "=========================================================="
    echo "WireGuard has been installed with a template configuration."
    echo "You can configure WireGuard through the web interface at:"
    echo "   System Stats -> WireGuard VPN section"
    echo "=========================================================="
    echo ""
    
    # Not starting WireGuard service automatically since config needs editing
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

# Install MediaMTX for WebRTC streaming
install_mediamtx() {
    echo "Installing MediaMTX..."
    
    # Create directory
    mkdir -p /opt/mediamtx
    cd /opt/mediamtx
    
    # Detect architecture
    ARCH=$(uname -m)
    case $ARCH in
        x86_64)
            MTX_ARCH="linux_amd64"
            ;;
        aarch64)
            MTX_ARCH="linux_arm64v8"
            ;;
        armv7l)
            MTX_ARCH="linux_armv7"
            ;;
        *)
            echo "Unsupported architecture: $ARCH"
            exit 1
            ;;
    esac
    
    # Get latest release version
    LATEST_VERSION=$(curl -s https://api.github.com/repos/bluenviron/mediamtx/releases/latest | grep '"tag_name"' | sed -E 's/.*"([^"]+)".*/\1/')
    
    if [ -z "$LATEST_VERSION" ]; then
        echo "Failed to get latest MediaMTX version, using v1.9.3"
        LATEST_VERSION="v1.9.3"
    fi
    
    echo "Downloading MediaMTX ${LATEST_VERSION} for ${MTX_ARCH}..."
    
    # Download and extract
    curl -L "https://github.com/bluenviron/mediamtx/releases/download/${LATEST_VERSION}/mediamtx_${LATEST_VERSION}_${MTX_ARCH}.tar.gz" -o mediamtx.tar.gz
    tar -xzf mediamtx.tar.gz
    rm mediamtx.tar.gz
    
    # Make executable
    chmod +x mediamtx
    
    # Copy base config from rist installation
    if [ -f "/root/rist/mediamtx.yml" ]; then
        cp /root/rist/mediamtx.yml /opt/mediamtx/mediamtx.yml
    fi
    
    # Install systemd service
    cp /root/rist/services/mediamtx.service /etc/systemd/system/
    
    # Enable and start service
    systemctl daemon-reload
    systemctl enable mediamtx.service
    systemctl start mediamtx.service
    
    echo "MediaMTX installed successfully"
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
    setup_service
    setup_logrotate
    install_wireguard
    install_mediamtx
    start_services
    
    echo ""
    echo "=========================================================="
    echo "   Installation Complete!"
    echo "=========================================================="
    echo ""
    echo "Access the web interface at: http://$(hostname -I | awk '{print $1}')"
    echo ""
    echo "Default login credentials:"
    echo "   Username: admin"
    echo "   Password: admin"
    echo ""
    echo "Please change your password after first login!"
    echo ""
    echo "WebRTC streams available at: http://$(hostname -I | awk '{print $1}'):8889/{channel_id}"
    echo "Check service status with: systemctl status rist-api mediamtx"
    echo ""
}

# Run main installation
main
