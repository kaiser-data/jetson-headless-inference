#!/usr/bin/env bash
# One-time root setup for Wake-on-LAN + remote suspend.
#
#   sudo ./wol-setup.sh [interface]     (default: eno1)
#
# Owns the wake/power domain (maintenance access lives in maint-setup.sh):
#   1. Verifies the NIC supports magic-packet wake, enables it now
#   2. Installs a systemd unit that re-enables WoL on boot AND after
#      every resume (the driver can reset the flag on suspend cycles)
#   3. Whitelists systemctl suspend / nvpmodel / jetson_clocks in sudoers
#      so the control API (8080: /power/suspend, /power/mode) can drive
#      power state remotely without a password
#
# Wake lands in the default power mode (15W). High-inference mode is
# on-demand via POST :8080/power/mode {"mode":"high"} — clients that need
# it (bench) request it; a lone voice task doesn't pay the MAXN power bill.
#
# Companion: sudo ./maint-setup.sh — Ollama bind/perf drop-in, display
# manager stop/start, ollama restart, reboot whitelist.
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

# 3. Remove the auto-MAXN resume hook from earlier versions ------------------
# (decision: high mode is on-demand via /power/mode, not forced on every wake)
if [ -f /etc/systemd/system/jetson-resume-perf.service ]; then
    systemctl disable jetson-resume-perf.service 2>/dev/null || true
    rm -f /etc/systemd/system/jetson-resume-perf.service
    systemctl daemon-reload
    _log "removed jetson-resume-perf.service (MAXN now on-demand via /power/mode)"
fi

# 4. Sudoers rules for the control API ----------------------------------------
# Both /bin and /usr/bin variants — sudoers matches the exact resolved path
cat > /etc/sudoers.d/jetson-wake << EOF
$SVC_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl suspend
$SVC_USER ALL=(ALL) NOPASSWD: /bin/systemctl suspend
$SVC_USER ALL=(ALL) NOPASSWD: /usr/sbin/nvpmodel
$SVC_USER ALL=(ALL) NOPASSWD: /usr/bin/jetson_clocks
EOF
chmod 440 /etc/sudoers.d/jetson-wake
visudo -c -f /etc/sudoers.d/jetson-wake > /dev/null \
    || { rm -f /etc/sudoers.d/jetson-wake; _fail "sudoers syntax check failed — rules removed"; }
_log "sudoers rules installed: suspend / nvpmodel / jetson_clocks passwordless for $SVC_USER"

# Summary --------------------------------------------------------------------
MAC="$(cat "/sys/class/net/$IFACE/address")"
IP="$(ip -4 -o addr show "$IFACE" | awk '{print $4}' | cut -d/ -f1)"
echo ""
_log "Done. Wake this box with a magic packet to:"
_log "  MAC: $MAC   (LAN IP: ${IP:-?})"
_log "From the Mac (jetson-bench repo): ./run-full-test.sh"
_log "Suspend from anywhere:       curl -X POST http://<jetson>:8080/power/suspend"
