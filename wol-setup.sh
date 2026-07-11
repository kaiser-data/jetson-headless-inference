#!/usr/bin/env bash
# One-time root setup for Wake-on-LAN + remote suspend.
#
#   sudo ./wol-setup.sh [interface]     (default: eno1)
#
# Does three things:
#   1. Verifies the NIC supports magic-packet wake, enables it now
#   2. Installs a systemd unit that re-enables WoL on boot AND after
#      every resume (the driver can reset the flag on suspend cycles)
#   3. Whitelists "systemctl suspend" in sudoers so the control API
#      (port 8080, POST /power/suspend) can put the box back to sleep
set -euo pipefail

IFACE="${1:-eno1}"
ETHTOOL="/usr/sbin/ethtool"
SVC_USER="${SUDO_USER:-marty}"

_log()  { printf '\033[32m[wol-setup]\033[0m %s\n' "$*"; }
_fail() { printf '\033[31m[wol-setup]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || _fail "run with sudo: sudo ./wol-setup.sh"
[ -e "/sys/class/net/$IFACE" ] || _fail "interface $IFACE not found"

# 1. Capability check + enable now -----------------------------------------
SUPPORTED="$("$ETHTOOL" "$IFACE" | awk -F': ' '/Supports Wake-on/{print $2}')"
_log "NIC $IFACE supports Wake-on: ${SUPPORTED:-<none reported>}"
case "$SUPPORTED" in
    *g*) ;;
    *)   _fail "no magic-packet (g) support on $IFACE — WoL won't work" ;;
esac

"$ETHTOOL" -s "$IFACE" wol g
echo enabled > "/sys/class/net/$IFACE/device/power/wakeup"
_log "WoL (magic packet) enabled on $IFACE"

# 2. Persistence: re-apply on boot and after resume -------------------------
cat > /etc/systemd/system/wol-enable.service << EOF
[Unit]
Description=Enable Wake-on-LAN on $IFACE (boot + after resume)
After=network.target suspend.target

[Service]
Type=oneshot
ExecStart=$ETHTOOL -s $IFACE wol g
ExecStart=/bin/sh -c 'echo enabled > /sys/class/net/$IFACE/device/power/wakeup'

[Install]
WantedBy=multi-user.target suspend.target
EOF
systemctl daemon-reload
systemctl enable --now wol-enable.service
_log "wol-enable.service installed (runs at boot and after each resume)"

# 3. Sudoers rule for the control API ---------------------------------------
cat > /etc/sudoers.d/jetson-wake << EOF
$SVC_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl suspend
$SVC_USER ALL=(ALL) NOPASSWD: /bin/systemctl suspend
EOF
chmod 440 /etc/sudoers.d/jetson-wake
_log "sudoers rule installed: $SVC_USER may run 'systemctl suspend' without password"

# Summary --------------------------------------------------------------------
MAC="$(cat "/sys/class/net/$IFACE/address")"
IP="$(ip -4 -o addr show "$IFACE" | awk '{print $4}' | cut -d/ -f1)"
echo ""
_log "Done. Wake this box with a magic packet to:"
_log "  MAC: $MAC   (LAN IP: ${IP:-?})"
_log "From a Mac on the same LAN:  ./mac/wake-and-run.sh \"your prompt\""
_log "Suspend from anywhere:       curl -X POST http://<jetson>:8080/power/suspend"
