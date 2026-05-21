#!/bin/bash
# =============================================================
# Jetson Boot Mode Chooser
# Shows a 10-second menu on every login (SSH or TTY).
# Press 1–5 to choose mode; default = desktop.
#
# Install (once):
#   echo 'source ~/gamma4_models/boot-choice.sh' >> ~/.bashrc
#
# Skip for one session:
#   JETSON_AI_SKIP_MENU=1 bash
# =============================================================

# Guard: only interactive shells, not desktop sessions, not sub-shells
[[ $- != *i* ]] && return 0
[ -n "$DISPLAY" ] || [ -n "$WAYLAND_DISPLAY" ] && return 0
[ -n "$JETSON_AI_SKIP_MENU" ] && return 0
# Don't run inside tmux/screen panes that are sub-shells
[ -n "$TMUX" ] && [ "$SHLVL" -gt 2 ] && return 0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
AI_CTRL="$SCRIPT_DIR/jetson-ai.sh"
STATE_DIR="$HOME/.local/share/jetson-ai"
LAST_CHOICE_FILE="$STATE_DIR/last-choice"

mkdir -p "$STATE_DIR"
LAST=$(cat "$LAST_CHOICE_FILE" 2>/dev/null || echo "1")
TIMEOUT=10

_print_menu() {
    clear
    echo ""
    echo "  ╔══════════════════════════════════════════════╗"
    echo "  ║         JETSON ORIN — BOOT MODE              ║"
    echo "  ╠══════════════════════════════════════════════╣"
    printf  "  ║  [1] Ubuntu Desktop              %s  ║\n" \
        "$([ "$LAST" = "1" ] && echo '← last' || echo '      ')"
    printf  "  ║  [2] AI API  — qwen3.5:4b        %s  ║\n" \
        "$([ "$LAST" = "2" ] && echo '← last' || echo '      ')"
    printf  "  ║  [3] AI API  — phi4-mini (fast)  %s  ║\n" \
        "$([ "$LAST" = "3" ] && echo '← last' || echo '      ')"
    printf  "  ║  [4] AI API  — choose model      %s  ║\n" \
        "$([ "$LAST" = "4" ] && echo '← last' || echo '      ')"
    printf  "  ║  [5] Shell only (no desktop/AI)  %s  ║\n" \
        "$([ "$LAST" = "5" ] && echo '← last' || echo '      ')"
    echo "  ╚══════════════════════════════════════════════╝"
    echo ""
}

_countdown() {
    local DEFAULT="${1:-1}"
    local KEY=""
    for i in $(seq $TIMEOUT -1 1); do
        printf "\r  Auto-starting [%s] in %2ds ... (press 1-5 to change)  " "$DEFAULT" "$i"
        if read -r -t 1 -n 1 KEY 2>/dev/null; then
            echo ""
            echo "$KEY"
            return
        fi
    done
    echo ""
    echo "$DEFAULT"
}

_check_setup() {
    if ! sudo -n nvpmodel -q >/dev/null 2>&1; then
        echo "  ⚠  First-time setup not done. Run: ./jetson-ai.sh setup"
        echo "     (Needed once for passwordless power/display control)"
        echo ""
    fi
}

# -------------------------------------------------------
_print_menu
CHOICE=$(_countdown "$LAST")

# Validate
[[ "$CHOICE" =~ ^[1-5]$ ]] || CHOICE="1"
echo "$CHOICE" > "$LAST_CHOICE_FILE"

echo ""
case "$CHOICE" in
    1)
        echo "  Starting Ubuntu Desktop..."
        sudo systemctl start gdm3 2>/dev/null \
            || sudo systemctl start gdm 2>/dev/null \
            || sudo systemctl start lightdm 2>/dev/null \
            || echo "  Could not start display manager. Run: sudo systemctl start gdm3"
        ;;
    2)
        _check_setup
        echo "  Starting AI API — qwen3.5:4b ..."
        bash "$AI_CTRL" start qwen3.5:4b
        echo ""
        echo "  Type: bash $AI_CTRL status"
        echo "  Stop: bash $AI_CTRL stop"
        ;;
    3)
        _check_setup
        echo "  Starting AI API — phi4-mini (fast) ..."
        bash "$AI_CTRL" start phi4-mini
        ;;
    4)
        _check_setup
        echo ""
        bash "$AI_CTRL" list
        printf "  Model name (or task alias): "
        read -r MODEL
        [ -z "$MODEL" ] && MODEL="qwen3.5:4b"
        bash "$AI_CTRL" start "$MODEL"
        ;;
    5)
        echo "  Shell only — no desktop, no AI service."
        echo "  Start AI manually: bash $AI_CTRL start"
        echo "  Start desktop:     sudo systemctl start gdm3"
        ;;
esac
