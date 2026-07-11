#!/usr/bin/env bash
# One-time root setup for Wake-on-LAN + remote suspend.
#
#   sudo ./wol-setup.sh [interface]     (default: eno1)
#
# Sets up everything remote control needs:
#   1. Verifies the NIC supports magic-packet wake, enables it now
#   2. Installs a systemd unit that re-enables WoL on boot AND after
#      every resume (the driver can reset the flag on suspend cycles)
#   3. Installs a resume hook that switches to high-inference mode
#      (MAXN_SUPER + jetson_clocks) every time the box wakes from suspend
#   4. Installs the Ollama drop-in: bind 0.0.0.0 (reachable from LAN and
#      tailnet — no SSH bridge needed) + FlashAttention/q8 KV cache
#      (less memory per model on the shared 8 GB)
#   5. Whitelists systemctl suspend / nvpmodel / jetson_clocks / gdm
#      stop-start / ollama restart in sudoers so the control API (8080:
#      /power/suspend, /power/mode, /power/headless) can drive the box
#      remotely without a password
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

# 3. Resume hook: wake straight into high-inference mode ---------------------
cat > /etc/systemd/system/jetson-resume-perf.service << 'EOF'
[Unit]
Description=High-inference mode (MAXN_SUPER + max clocks) after resume
After=suspend.target

[Service]
Type=oneshot
ExecStart=/usr/sbin/nvpmodel -m 2
ExecStart=/usr/bin/jetson_clocks

[Install]
WantedBy=suspend.target
EOF
systemctl daemon-reload
systemctl enable jetson-resume-perf.service
_log "jetson-resume-perf.service installed (MAXN_SUPER on every wake)"

# 4. Ollama: remote access + memory-efficient settings ------------------------
# (jetson-ai.sh setup installs the same file with NUM_PARALLEL=2; 1 is the
#  safer choice on 8 GB — halves KV cache, single caller anyway)
mkdir -p /etc/systemd/system/ollama.service.d
cat > /etc/systemd/system/ollama.service.d/jetson-performance.conf << 'EOF'
[Service]
# Bind to all interfaces so LAN/tailnet devices can reach the API
Environment="OLLAMA_HOST=0.0.0.0:11434"
# Never unload model from RAM between requests (stays warm across suspend)
Environment="OLLAMA_KEEP_ALIVE=-1"
# Only one model at a time (save RAM)
Environment="OLLAMA_MAX_LOADED_MODELS=1"
# Single caller on this box — halves KV cache vs 2
Environment="OLLAMA_NUM_PARALLEL=1"
# Flash Attention: 30-50% less KV cache memory
Environment="OLLAMA_FLASH_ATTENTION=1"
# KV cache quantization: halves KV memory, negligible quality loss
Environment="OLLAMA_KV_CACHE_TYPE=q8_0"
EOF
systemctl daemon-reload
systemctl restart ollama
_log "Ollama drop-in installed: binds 0.0.0.0, FlashAttention + q8 KV cache"

# 5. Sudoers rules for the control API ----------------------------------------
# Both /bin and /usr/bin variants — sudoers matches the exact resolved path
cat > /etc/sudoers.d/jetson-wake << EOF
$SVC_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl suspend
$SVC_USER ALL=(ALL) NOPASSWD: /bin/systemctl suspend
$SVC_USER ALL=(ALL) NOPASSWD: /usr/sbin/nvpmodel
$SVC_USER ALL=(ALL) NOPASSWD: /usr/bin/jetson_clocks
$SVC_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl stop gdm3, /bin/systemctl stop gdm3
$SVC_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl start gdm3, /bin/systemctl start gdm3
$SVC_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl stop gdm, /bin/systemctl stop gdm
$SVC_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl start gdm, /bin/systemctl start gdm
$SVC_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart ollama, /bin/systemctl restart ollama
EOF
chmod 440 /etc/sudoers.d/jetson-wake
_log "sudoers rules installed: suspend / power / headless / ollama passwordless for $SVC_USER"

# Summary --------------------------------------------------------------------
MAC="$(cat "/sys/class/net/$IFACE/address")"
IP="$(ip -4 -o addr show "$IFACE" | awk '{print $4}' | cut -d/ -f1)"
echo ""
_log "Done. Wake this box with a magic packet to:"
_log "  MAC: $MAC   (LAN IP: ${IP:-?})"
_log "From the Mac (jetson-bench repo): ./run-full-test.sh"
_log "Suspend from anywhere:       curl -X POST http://<jetson>:8080/power/suspend"
