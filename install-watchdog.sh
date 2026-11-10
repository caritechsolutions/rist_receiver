#!/bin/bash

# WireGuard/RIST Watchdog installer
#
# Standalone (downloads the files from GitHub):
#   curl -sSL "https://raw.githubusercontent.com/caritechsolutions/rist_receiver/main/install-watchdog.sh?$(date +%s)" | sudo bash
#
# From a local copy of the repo (used by install.sh and update.sh):
#   bash install-watchdog.sh /root/rist

set -e

REPO_RAW="https://raw.githubusercontent.com/caritechsolutions/rist_receiver/${WATCHDOG_BRANCH:-main}"
SRC_DIR="$1"
TMP_DIR=""

if [ "$(id -u)" != "0" ]; then
    echo "This script must be run as root" 1>&2
    exit 1
fi

cleanup() {
    if [ -n "$TMP_DIR" ] && [ -d "$TMP_DIR" ]; then
        rm -rf "$TMP_DIR"
    fi
}
trap cleanup EXIT

# No local source given - download the files
if [ -z "$SRC_DIR" ]; then
    TMP_DIR=$(mktemp -d)
    SRC_DIR="$TMP_DIR"
    mkdir -p "$SRC_DIR/services"
    ts=$(date +%s)
    echo "Downloading watchdog files..."
    for f in wireguard-watchdog.sh services/wireguard-watchdog.service services/wireguard-watchdog.logrotate; do
        curl -fsSL "${REPO_RAW}/${f}?v=${ts}" -o "$SRC_DIR/$f"
    done
fi

for f in wireguard-watchdog.sh services/wireguard-watchdog.service services/wireguard-watchdog.logrotate; do
    if [ ! -s "$SRC_DIR/$f" ]; then
        echo "ERROR: $SRC_DIR/$f not found" 1>&2
        exit 1
    fi
done

echo "Installing WireGuard/RIST watchdog..."
install -m 755 "$SRC_DIR/wireguard-watchdog.sh" /usr/local/bin/wireguard-watchdog.sh
install -m 644 "$SRC_DIR/services/wireguard-watchdog.service" /etc/systemd/system/wireguard-watchdog.service
install -m 644 "$SRC_DIR/services/wireguard-watchdog.logrotate" /etc/logrotate.d/wireguard-watchdog

# Local settings file - created once, never overwritten
if [ ! -f /etc/default/wireguard-watchdog ]; then
    cat > /etc/default/wireguard-watchdog << 'CONF'
# Local settings for wireguard-watchdog (overrides the script defaults).
# Restart after editing: systemctl restart wireguard-watchdog
#TUNNEL_IP="10.8.0.1"
#WG_SERVER_IP="108.181.93.3"
#EMAIL=""
CONF
    chmod 644 /etc/default/wireguard-watchdog
fi

systemctl daemon-reload
systemctl enable wireguard-watchdog.service
systemctl restart wireguard-watchdog.service

echo "Watchdog installed. Log: /var/log/wireguard-watchdog.log"
