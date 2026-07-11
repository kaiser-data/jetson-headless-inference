#!/usr/bin/env bash
# One-time root setup for passwordless remote maintenance.
#
#   sudo ./maint-setup.sh
#
# Does two things:
#   1. Binds Ollama to 0.0.0.0:11434 via a systemd override (drift guard —
#      jetson-ai.sh intends this, but reinstalls/updates can lose it) and
#      restarts the service
#   2. Whitelists narrow maintenance commands in sudoers so remote clients
#      (jetson-bench, ops sessions) can run them without a password:
#      restart ollama / stop+start the display manager / reboot
#
# No secrets involved — this whitelist is the alternative to sharing a sudo
# password with remote tooling. The user is taken from whoever invoked sudo;
# nothing is hardcoded.
set -euo pipefail

SVC_USER="${SUDO_USER:-}"

_log()  { printf '\033[32m[maint-setup]\033[0m %s\n' "$*"; }
_fail() { printf '\033[31m[maint-setup]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || _fail "run with sudo: sudo ./maint-setup.sh"
[ -n "$SVC_USER" ]   || _fail "SUDO_USER is empty — run via sudo from the account that should get the whitelist, not as root directly"

# 1. Ollama reachable beyond loopback ----------------------------------------
mkdir -p /etc/systemd/system/ollama.service.d
cat > /etc/systemd/system/ollama.service.d/net.conf << 'EOF'
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
EOF
systemctl daemon-reload
systemctl restart ollama
for _ in 1 2 3 4 5; do
    sleep 2
    ss -tln | grep -q ':11434' && break
done
ss -tln | grep -qE '(0\.0\.0\.0|\*):11434' \
    && _log "Ollama now listens on 0.0.0.0:11434 (tailnet/LAN reachable)" \
    || _fail "Ollama still not on 0.0.0.0:11434 — check: systemctl status ollama"

# 2. Sudoers whitelist for remote maintenance ---------------------------------
DM_UNIT=""
DM_LINK="$(readlink -f /etc/systemd/system/display-manager.service 2>/dev/null || true)"
[ -n "$DM_LINK" ] && DM_UNIT="$(basename "$DM_LINK")"

{
    for SC in /usr/bin/systemctl /bin/systemctl; do
        echo "$SVC_USER ALL=(ALL) NOPASSWD: $SC restart ollama"
        [ -n "$DM_UNIT" ] \
            && echo "$SVC_USER ALL=(ALL) NOPASSWD: $SC stop $DM_UNIT, $SC start $DM_UNIT"
    done
    echo "$SVC_USER ALL=(ALL) NOPASSWD: /usr/sbin/reboot, /sbin/reboot"
} > /etc/sudoers.d/jetson-maint
chmod 440 /etc/sudoers.d/jetson-maint
visudo -c -f /etc/sudoers.d/jetson-maint > /dev/null \
    || { rm -f /etc/sudoers.d/jetson-maint; _fail "sudoers syntax check failed — whitelist removed"; }
_log "sudoers whitelist for $SVC_USER: restart ollama${DM_UNIT:+ / stop|start $DM_UNIT} / reboot"

echo ""
_log "Done. Remote clients can now run, passwordless:"
_log "  sudo -n systemctl restart ollama"
[ -n "$DM_UNIT" ] && _log "  sudo -n systemctl stop $DM_UNIT     # free ~500 MB before benching"
_log "  sudo -n reboot                       # OOM last resort"
