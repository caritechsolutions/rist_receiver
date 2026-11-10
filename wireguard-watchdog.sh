#!/bin/bash

###############################################################################
# WireGuard/RIST Watchdog Script
# Monitors the WireGuard tunnel and auto-restarts WireGuard + RIST channel
# services if the tunnel goes down while the WireGuard server is still
# reachable (indicates a firewall/PPPoE reconnect issue).
#
# Defaults below can be overridden in /etc/default/wireguard-watchdog
###############################################################################

# Configuration (defaults)
TUNNEL_IP="10.8.0.1"                    # WireGuard tunnel IP to test
WG_SERVER_IP="108.181.93.3"             # WireGuard server external IP
INTERNET_TEST_IP="8.8.8.8"              # External IP to test internet connectivity
WG_INTERFACE="wg0"                      # WireGuard interface
RIST_SERVICE_PATTERN="rist-channel"     # Pattern for RIST services
LOG_FILE="/var/log/wireguard-watchdog.log"
EMAIL=""                                # Alert address (empty = log only)
CHECK_INTERVAL=30                       # Check every 30 seconds
INTERNET_CHECK_RETRY_DELAY=60           # Wait 60s before rechecking internet
PING_COUNT=3                            # Pings per check (fails only if all fail)
HEARTBEAT_EVERY=5                       # Log "tunnel OK" every N good checks (0 = never)
ALERT_COOLDOWN=1800                     # Min seconds between "server unreachable" alerts
IDLE_INTERVAL=60                        # Recheck delay while WireGuard is not configured

# Local overrides (not in the repo)
CONFIG_FILE="${WATCHDOG_CONFIG:-/etc/default/wireguard-watchdog}"
# shellcheck disable=SC1090
[ -f "$CONFIG_FILE" ] && . "$CONFIG_FILE"

WG_SERVICE="wg-quick@${WG_INTERFACE}"
WG_CONF="${WG_CONF:-/etc/wireguard/${WG_INTERFACE}.conf}"

# Function to log messages
log_message() {
    local timestamp
    timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$timestamp] $1" | tee -a "$LOG_FILE"
}

# Function to send email alert (only if EMAIL is set and mail is installed)
send_alert() {
    local subject="$1"
    local message="$2"
    if [ -z "$EMAIL" ]; then
        return 0
    fi
    if ! command -v mail >/dev/null 2>&1; then
        log_message "EMAIL NOT SENT (mail command not installed): $subject"
        return 0
    fi
    if printf '%b\n' "$message" | mail -s "$subject" "$EMAIL"; then
        log_message "EMAIL SENT: $subject"
    else
        log_message "EMAIL FAILED: $subject"
    fi
}

# Function to restart services
restart_services() {
    log_message "========== SERVICE RESTART INITIATED =========="

    # Restart WireGuard
    log_message "Restarting WireGuard service: $WG_SERVICE"
    systemctl restart "$WG_SERVICE"
    sleep 5

    # Restart RIST services (only units that are currently loaded)
    log_message "Restarting RIST services..."
    systemctl restart "${RIST_SERVICE_PATTERN}*.service" 2>/dev/null

    log_message "========== SERVICE RESTART COMPLETED =========="

    send_alert "WireGuard Service Restarted" "WireGuard and RIST services were restarted due to tunnel connectivity loss.\n\nTime: $(date)\nServer: $(hostname)"
}

# Function to check if IP is reachable
ping_ip() {
    local ip="$1"
    ping -c "$PING_COUNT" -W 2 "$ip" &>/dev/null
}

# WireGuard counts as configured once wg0.conf exists and is not the
# installer's template
wg_configured() {
    [ -f "$WG_CONF" ] || return 1
    grep -q "YOUR_PRIVATE_KEY" "$WG_CONF" && return 1
    return 0
}

# Main watchdog loop
log_message "WireGuard Watchdog started (PID: $$)"
log_message "Configuration: Tunnel=$TUNNEL_IP, Server=$WG_SERVER_IP, Internet=$INTERNET_TEST_IP"
log_message "Check interval: ${CHECK_INTERVAL}s, Email: ${EMAIL:-none (log only)}"
log_message "====================================================="

ok_count=0
last_unreachable_alert=0
idle_logged=0

while true; do
    # Do nothing on receivers that have no WireGuard config yet
    if ! wg_configured; then
        if [ "$idle_logged" -eq 0 ]; then
            log_message "INFO: WireGuard not configured ($WG_CONF) - watchdog idle"
            idle_logged=1
        fi
        sleep "$IDLE_INTERVAL"
        continue
    fi
    if [ "$idle_logged" -eq 1 ]; then
        log_message "INFO: WireGuard config detected - monitoring started"
        idle_logged=0
    fi

    # Check tunnel connectivity
    if ping_ip "$TUNNEL_IP"; then
        # Tunnel is UP - all good
        ok_count=$((ok_count + 1))
        if [ "$HEARTBEAT_EVERY" -gt 0 ] && [ $((ok_count % HEARTBEAT_EVERY)) -eq 0 ]; then
            log_message "OK: Tunnel is UP ($TUNNEL_IP reachable)"
        fi
    else
        # Tunnel is DOWN - investigate
        ok_count=0
        log_message "WARNING: Tunnel is DOWN (cannot ping $TUNNEL_IP)"

        # Check if WireGuard server is reachable
        if ping_ip "$WG_SERVER_IP"; then
            # Server is UP but tunnel is DOWN - restart services
            log_message "ALERT: Server is UP but tunnel is DOWN - indicates firewall/PPPoE issue"
            restart_services
        else
            # Server is also DOWN - check internet connectivity
            log_message "INFO: Server also unreachable ($WG_SERVER_IP)"

            if ping_ip "$INTERNET_TEST_IP"; then
                # Internet is UP but server is DOWN - send alert
                log_message "ALERT: Internet is UP but WireGuard server is unreachable"
                now=$(date +%s)
                if [ $((now - last_unreachable_alert)) -ge "$ALERT_COOLDOWN" ]; then
                    send_alert "WireGuard Server Unreachable" "WireGuard server ($WG_SERVER_IP) is unreachable, but internet connectivity ($INTERNET_TEST_IP) is available.\n\nTime: $(date)\nServer: $(hostname)\nTunnel IP: $TUNNEL_IP\nWG Server: $WG_SERVER_IP"
                    last_unreachable_alert=$now
                fi
            else
                # Internet is DOWN - wait and retry
                log_message "INFO: Internet is also DOWN - waiting ${INTERNET_CHECK_RETRY_DELAY}s before retry"
                sleep "$INTERNET_CHECK_RETRY_DELAY"
                continue
            fi
        fi
    fi

    # Wait before next check
    sleep "$CHECK_INTERVAL"
done
