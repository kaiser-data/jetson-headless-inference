#!/bin/bash
# =============================================================
# Jetson Orin 8GB — AI API Controller v3.0
# GitHub: https://github.com/kaiser-data/jetson-ai-api
#
# 3 modes:
#   local          — Ollama GPU LLM only (text API)
#   voice          — Ollama GPU LLM + Piper TTS (streaming audio)
#   api            — Cloud LLM API + Piper TTS (no GPU needed)
#
# First-time setup (once, needs password):
#   ./jetson-ai.sh setup
#
# Daily use:
#   ./jetson-ai.sh start [model|task]         # local LLM only
#   ./jetson-ai.sh start voice [model|task]   # local LLM + voice
#   ./jetson-ai.sh start api                  # cloud API + voice
#   ./jetson-ai.sh switch [model|task]
#   ./jetson-ai.sh stop
# =============================================================

set -euo pipefail

PORT=11434
PIPER_PORT=5500
PIPELINE_PORT=8000
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
STATE_DIR="$HOME/.local/share/jetson-ai"
STATE_FILE="$STATE_DIR/state"
LOG="$STATE_DIR/jetson-ai.log"
MODEL_FILE="$STATE_DIR/current-model"
PIPER_PID_FILE="$STATE_DIR/piper.pid"
PIPELINE_PID_FILE="$STATE_DIR/pipeline.pid"
VOICE_DIR="$(cd "$(dirname "$0")" && pwd)/voice"

mkdir -p "$STATE_DIR"

# --- Task → model routing (edit to taste) ---
declare -A TASK_MODEL=(
    [default]="qwen3.5:4b"
    [fast]="phi4-mini"
    [reasoning]="phi4-mini"
    [code]="qwen3.5:4b"
    [german]="cas/discolm-mfto-german:latest"
    [vision]="gemma4:e2b"
    [tiny]="qwen2.5:3b"
    [chat]="llama3.2:3b"
    [quality]="llama3.1:8b"
)

# Model size index (GB) for GPU fit estimation
declare -A MODEL_GB=(
    [qwen3.5:0.8b]=1.0  [qwen3.5:2b]=2.7    [qwen3.5:4b]=3.4
    [phi4-mini]=2.5      [phi4-mini:latest]=2.5
    [llama3.2:3b]=2.0    [llama3.1:8b]=4.9
    [qwen2.5:3b]=1.9     [gemma3:latest]=3.3
    [gemma4:e2b]=7.2     [gemma4:e4b]=9.6
    [cas/discolm-mfto-german:latest]=7.7
)

# -------------------------------------------------------
_log()    { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }
_info()   { echo "  $*"; }
_warn()   { echo "  ⚠  $*"; }
_err()    { echo "  ✗  $*" >&2; exit 1; }
_sep()    { printf '  '; printf '─%.0s' {1..54}; echo; }
_free_mb(){ free -m | awk '/^Mem:/ {print $7}'; }
_free_h() { free -h | awk '/^Mem:/ {print $4 " free / " $2 " total"}'; }
_power()  { nvpmodel -q 2>/dev/null | awk '/NV Power Mode/{print $NF}' || echo "unknown"; }

_ollama_up() {
    curl -s --max-time 2 http://localhost:$PORT/ >/dev/null 2>&1
}
_wait_ollama() {
    local n=0
    while [ $n -lt 20 ]; do _ollama_up && return 0; sleep 1; n=$((n+1)); done
    return 1
}
_get_dm() {
    systemctl list-units --type=service --state=active 2>/dev/null \
        | grep -oE 'gdm3?\.service|lightdm\.service|sddm\.service' \
        | head -1 | sed 's/\.service//'
}
_current_model() { cat "$MODEL_FILE" 2>/dev/null || echo "none"; }

# Check if sudo works without password for our specific commands
_sudo_ok() {
    sudo -n nvpmodel -q >/dev/null 2>&1
}

# Estimate GPU fit: green/yellow/red based on available RAM
_fit_check() {
    local MODEL="$1"
    local SHORT="${MODEL%%:*}"
    local SIZE_GB="${MODEL_GB[$MODEL]:-${MODEL_GB[$SHORT:-0]}}"
    local FREE_MB
    FREE_MB=$(_free_mb)
    local FREE_GB
    FREE_GB=$(echo "$FREE_MB" | awk '{printf "%.1f", $1/1024}')

    if [ -z "$SIZE_GB" ] || [ "$SIZE_GB" = "0" ]; then
        echo "size unknown — run 'ollama list' to check"
        return
    fi

    # Need ~10% overhead on top of model size for KV cache + compute graph
    local NEEDED_GB
    NEEDED_GB=$(echo "$SIZE_GB" | awk '{printf "%.1f", $1*1.1}')

    awk -v free="$FREE_GB" -v need="$NEEDED_GB" -v size="$SIZE_GB" 'BEGIN {
        if (free+0 >= need+0)
            printf "✓ fits  (model %.1fGB, need ~%.1fGB, free %.1fGB)\n", size, need, free
        else if (free+0 >= size+0)
            printf "⚠ tight (model %.1fGB, need ~%.1fGB, free %.1fGB) — may partially CPU fallback\n", size, need, free
        else
            printf "✗ too big (model %.1fGB, need ~%.1fGB, free %.1fGB) — WILL CPU fallback (0.3 tok/s)\n", size, need, free
    }'
}

# Detect GPU vs CPU inference from /api/ps
_gpu_pct() {
    local MODEL="$1"
    curl -s --max-time 3 "http://localhost:$PORT/api/ps" 2>/dev/null \
    | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    for m in d.get('models',[]):
        if '$(echo $MODEL | sed "s/'/\\\\'/g")' in m.get('name',''):
            sv=m.get('size_vram',0); s=m.get('size',1)
            pct=int(sv*100/s) if s else 0
            print(pct)
            sys.exit(0)
    print(0)
except: print(0)
" 2>/dev/null || echo 0
}

# Ensure model is installed; offer to pull if not
_ensure_model() {
    local MODEL="$1"
    if ollama list 2>/dev/null | awk 'NR>1{print $1}' | grep -qxF "$MODEL"; then
        return 0
    fi
    echo ""
    _warn "Model '$MODEL' not found locally."
    read -r -p "  Pull it now? (~$(echo ${MODEL_GB[$MODEL]:-'?'})GB download) [y/N]: " ans
    if [[ "$ans" =~ ^[Yy]$ ]]; then
        _log "Pulling $MODEL ..."
        ollama pull "$MODEL" || _err "Pull failed"
    else
        _err "Model not available. Run: ollama pull $MODEL"
    fi
}

# -------------------------------------------------------
# Voice / TTS service helpers

_piper_up() {
    curl -s --max-time 2 "http://localhost:$PIPER_PORT/health" >/dev/null 2>&1
}
_pipeline_up() {
    curl -s --max-time 2 "http://localhost:$PIPELINE_PORT/health" >/dev/null 2>&1
}

_tts_start() {
    local MODE="${1:-local}"    # local | api

    if ! _piper_up; then
        _log "Starting Piper TTS (port $PIPER_PORT)..."
        nohup python3 "$VOICE_DIR/piper-service.py" \
            > "$STATE_DIR/piper.log" 2>&1 &
        echo $! > "$PIPER_PID_FILE"
        local n=0
        while [ $n -lt 15 ]; do _piper_up && break; sleep 1; n=$((n+1)); done
        if _piper_up; then _log "✓ Piper TTS ready"
        else _warn "Piper TTS failed — see $STATE_DIR/piper.log"; fi
    fi

    if ! _pipeline_up; then
        _log "Starting voice pipeline (port $PIPELINE_PORT, mode=$MODE)..."
        VOICE_MODE="$MODE" nohup python3 "$VOICE_DIR/voice-pipeline.py" \
            > "$STATE_DIR/pipeline.log" 2>&1 &
        echo $! > "$PIPELINE_PID_FILE"
        local n=0
        while [ $n -lt 15 ]; do _pipeline_up && break; sleep 1; n=$((n+1)); done
        if _pipeline_up; then _log "✓ Voice pipeline ready"
        else _warn "Pipeline failed — see $STATE_DIR/pipeline.log"; fi
    fi
}

_tts_stop() {
    for F in "$PIPER_PID_FILE" "$PIPELINE_PID_FILE"; do
        if [ -f "$F" ]; then
            local PID
            PID=$(cat "$F" 2>/dev/null || true)
            [ -n "$PID" ] && kill "$PID" 2>/dev/null && _log "Stopped TTS PID $PID"
            rm -f "$F"
        fi
    done
    pkill -f "piper-service.py"  2>/dev/null || true
    pkill -f "voice-pipeline.py" 2>/dev/null || true
}

_tts_status() {
    if _piper_up; then
        printf "  Piper TTS : RUNNING  http://%s:%s\n" "$IP" "$PIPER_PORT"
    else
        printf "  Piper TTS : stopped\n"
    fi
    if _pipeline_up; then
        local VMODE
        VMODE=$(curl -s --max-time 2 "http://localhost:$PIPELINE_PORT/health" \
            | python3 -c "import json,sys; print(json.load(sys.stdin).get('mode','?'))" 2>/dev/null || echo "?")
        printf "  Pipeline  : RUNNING  http://%s:%s  mode=%s\n" "$IP" "$PIPELINE_PORT" "$VMODE"
    else
        printf "  Pipeline  : stopped\n"
    fi
}

# -------------------------------------------------------
cmd_setup() {
    echo ""
    _sep
    _info "First-time setup (needs sudo password once)"
    _sep
    echo ""

    # 1. Sudoers rule — only whitelist what we need
    _log "Creating sudoers rule..."
    local RULES=(
        "$(whoami) ALL=(ALL) NOPASSWD: /usr/sbin/nvpmodel"
        "$(whoami) ALL=(ALL) NOPASSWD: /usr/bin/jetson_clocks"
        "$(whoami) ALL=(ALL) NOPASSWD: /bin/systemctl stop gdm3"
        "$(whoami) ALL=(ALL) NOPASSWD: /bin/systemctl stop gdm"
        "$(whoami) ALL=(ALL) NOPASSWD: /bin/systemctl stop lightdm"
        "$(whoami) ALL=(ALL) NOPASSWD: /bin/systemctl start gdm3"
        "$(whoami) ALL=(ALL) NOPASSWD: /bin/systemctl start gdm"
        "$(whoami) ALL=(ALL) NOPASSWD: /bin/systemctl start lightdm"
        "$(whoami) ALL=(ALL) NOPASSWD: /bin/systemctl restart ollama"
        "$(whoami) ALL=(ALL) NOPASSWD: /bin/systemctl stop ollama"
        "$(whoami) ALL=(ALL) NOPASSWD: /bin/systemctl start ollama"
    )
    printf '%s\n' "${RULES[@]}" | sudo tee /etc/sudoers.d/jetson-ai > /dev/null
    sudo chmod 440 /etc/sudoers.d/jetson-ai
    _log "Sudoers rule created."

    # 2. Ollama systemd drop-in — performance env vars applied to the service
    _log "Configuring ollama service with performance settings..."
    sudo mkdir -p /etc/systemd/system/ollama.service.d/
    sudo tee /etc/systemd/system/ollama.service.d/jetson-performance.conf > /dev/null << 'EOF'
[Service]
# Bind to all interfaces so LAN devices can reach the API
Environment="OLLAMA_HOST=0.0.0.0:11434"
# Never unload model from RAM between requests
Environment="OLLAMA_KEEP_ALIVE=-1"
# Only one model at a time (save RAM)
Environment="OLLAMA_MAX_LOADED_MODELS=1"
# 2 concurrent requests — reduce to 1 if you hit OOM with large models
Environment="OLLAMA_NUM_PARALLEL=2"
# Flash Attention: 30-50% less KV cache memory (CUDA/Jetson supported)
Environment="OLLAMA_FLASH_ATTENTION=1"
# KV cache quantization: halves KV memory, negligible quality loss
Environment="OLLAMA_KV_CACHE_TYPE=q8_0"
EOF

    sudo systemctl daemon-reload
    sudo systemctl restart ollama
    sleep 3

    _log "Ollama service reconfigured and restarted."

    # 3. Verify
    if _ollama_up; then
        _log "✓ Ollama responding on port $PORT"
    else
        _warn "Ollama not responding — check: journalctl -u ollama -n 20"
    fi

    echo ""
    _sep
    _info "Setup complete. Sudo password no longer needed for AI commands."
    _info "Run: ./jetson-ai.sh start"
    _sep
    echo ""
}

# -------------------------------------------------------
cmd_start() {
    local ARG="${1:-default}"

    # ── Mode: api ──────────────────────────────────────────
    if [ "$ARG" = "api" ]; then
        echo ""; _sep; _info "Jetson AI — API + Voice Mode"; _sep; echo ""
        _info "Cloud LLM + Piper TTS — no local GPU LLM needed"
        _info "Set OPENAI_API_KEY or ANTHROPIC_API_KEY before starting."
        echo ""

        {
            echo "mode=api"
            echo "dm="
            echo "power=$(_power)"
            echo "started=$(date +%s)"
        } > "$STATE_FILE"

        _tts_start "api"

        echo ""; _sep
        printf "  ✓  READY (API + Voice)\n"
        _sep
        printf "  Mode       : api (cloud LLM + local TTS)\n"
        printf "  RAM        : %s\n" "$(_free_h)"
        printf "  Piper TTS  : http://%s:%s/v1/audio/speech\n" "$IP" "$PIPER_PORT"
        printf "  Pipeline   : http://%s:%s/voice/chat\n"      "$IP" "$PIPELINE_PORT"
        _sep
        _info "Pipeline request (set mode in body):"
        _info "  {\"prompt\":\"...\",\"voice\":\"en\",\"mode\":\"openai\",\"api_key\":\"sk-...\"}"
        _info "  {\"prompt\":\"...\",\"voice\":\"de\",\"mode\":\"anthropic\",\"api_key\":\"sk-ant-...\"}"
        _sep
        _info "Commands:"
        _info "  ./jetson-ai.sh stop"
        _info "  python3 voice/sample-client.py health"
        echo ""
        return
    fi

    # ── Mode: local or voice ────────────────────────────────
    local VOICE_MODE_ENABLED=0
    local MODEL_ARG="$ARG"

    if [ "$ARG" = "voice" ]; then
        VOICE_MODE_ENABLED=1
        MODEL_ARG="${2:-default}"
    fi

    local MODEL="${TASK_MODEL[$MODEL_ARG]:-$MODEL_ARG}"

    _sudo_ok || {
        _warn "Sudo not configured for passwordless use."
        _info "Run './jetson-ai.sh setup' first (needs password once)."
        echo ""
    }

    if ! _ollama_up; then
        _log "Starting ollama service..."
        sudo systemctl start ollama 2>/dev/null
        _wait_ollama || _err "Ollama failed to start. Check: journalctl -u ollama -n 30"
    fi

    _ensure_model "$MODEL"

    local MODE_LABEL="Local LLM"
    [ "$VOICE_MODE_ENABLED" -eq 1 ] && MODE_LABEL="Local LLM + Voice"
    echo ""; _sep; _info "Jetson AI — Headless Mode ($MODE_LABEL)"; _sep; echo ""

    # Save state
    local DM PREV_POWER
    DM=$(_get_dm)
    PREV_POWER=$(_power)
    {
        echo "mode=$([ "$VOICE_MODE_ENABLED" -eq 1 ] && echo voice || echo local)"
        echo "dm=$DM"
        echo "power=$PREV_POWER"
        echo "started=$(date +%s)"
    } > "$STATE_FILE"

    # Max power mode
    _log "Power: $PREV_POWER → MAXN_SUPER ..."
    sudo nvpmodel -m 2 2>/dev/null && sudo jetson_clocks 2>/dev/null \
        || _warn "Could not set power mode (run setup first)"
    _log "Power mode: $(_power)"

    # Stop desktop — frees ~1.5 GB for GPU
    if [ -n "$DM" ]; then
        _log "Stopping $DM (frees ~1.5 GB RAM)..."
        sudo systemctl stop "$DM" 2>/dev/null
        sleep 3
    fi
    _log "RAM after desktop stop: $(_free_h)"

    # GPU fit check
    _info "GPU fit: $(_fit_check "$MODEL")"

    # Load model
    _log "Loading model: $MODEL ..."
    local RESP
    RESP=$(curl -s --max-time 120 "http://localhost:$PORT/api/generate" \
        -H "Content-Type: application/json" \
        -d "{\"model\":\"$MODEL\",\"prompt\":\"hi\",\"stream\":false,\"keep_alive\":-1}" 2>/dev/null)

    if echo "$RESP" | python3 -c "import json,sys; json.load(sys.stdin)['response']" >/dev/null 2>&1; then
        echo "$MODEL" > "$MODEL_FILE"
    else
        _warn "Model load may have failed. Check: journalctl -u ollama -n 20"
    fi

    local GPU
    GPU=$(_gpu_pct "$MODEL")

    # Start voice services if requested
    if [ "$VOICE_MODE_ENABLED" -eq 1 ]; then
        _tts_start "local"
    fi

    echo ""; _sep
    printf "  ✓  READY\n"
    _sep
    printf "  Mode       : %s\n" "$MODE_LABEL"
    printf "  Model      : %s\n" "$MODEL"
    printf "  GPU layers : %s%%\n" "$GPU"
    [ "$GPU" -lt 50 ] 2>/dev/null && _warn "Low GPU% → CPU fallback likely. Free more RAM or use smaller model."
    printf "  RAM        : %s\n" "$(_free_h)"
    printf "  Power      : %s\n" "$(_power)"
    printf "  LLM API    : http://%s:%s\n" "$IP" "$PORT"
    if [ "$VOICE_MODE_ENABLED" -eq 1 ]; then
        printf "  Piper TTS  : http://%s:%s/v1/audio/speech\n" "$IP" "$PIPER_PORT"
        printf "  Pipeline   : http://%s:%s/voice/chat\n"      "$IP" "$PIPELINE_PORT"
    fi
    _sep
    _info "Endpoints:"
    _info "  /api/generate          (ollama native)"
    _info "  /v1/chat/completions   (OpenAI-compatible)"
    if [ "$VOICE_MODE_ENABLED" -eq 1 ]; then
        _info "  :5500/v1/audio/speech  (Piper TTS — OpenAI-compatible)"
        _info "  :8000/voice/chat       (LLM+TTS streaming audio)"
    fi
    _sep
    _info "Commands:"
    _info "  ./jetson-ai.sh switch <model|task>"
    _info "  ./jetson-ai.sh bench"
    _info "  ./jetson-ai.sh stop"
    echo ""
}

# -------------------------------------------------------
cmd_stop() {
    echo ""; _log "Stopping AI services..."

    # Stop voice services first
    if _piper_up || _pipeline_up; then
        _log "Stopping voice services..."
        _tts_stop
    fi

    # Unload model from RAM
    local MODEL
    MODEL=$(_current_model)
    if [ "$MODEL" != "none" ] && _ollama_up; then
        _log "Unloading $MODEL from RAM..."
        curl -s "http://localhost:$PORT/api/generate" \
            -d "{\"model\":\"$MODEL\",\"keep_alive\":0}" >/dev/null 2>&1 || true
        sleep 2
    fi
    rm -f "$MODEL_FILE"

    # Restore power mode
    local PREV
    PREV=$(grep "^power=" "$STATE_FILE" 2>/dev/null | cut -d= -f2 || echo "15W")
    _log "Restoring power mode → $PREV ..."
    case "$PREV" in
        15W|"NV Power Mode: 15W") sudo nvpmodel -m 0 2>/dev/null || true ;;
        25W)                       sudo nvpmodel -m 1 2>/dev/null || true ;;
        7W)                        sudo nvpmodel -m 3 2>/dev/null || true ;;
    esac

    # Restore desktop (not needed in api mode — it was never stopped)
    local DM
    DM=$(grep "^dm=" "$STATE_FILE" 2>/dev/null | cut -d= -f2 || echo "")
    if [ -n "$DM" ]; then
        _log "Restoring desktop ($DM)..."
        sudo systemctl start "$DM" 2>/dev/null || true
        _info "Desktop restoring — give it 5–10 seconds."
    fi
    rm -f "$STATE_FILE"

    _log "Done. RAM: $(_free_h)"
    echo ""
}

# -------------------------------------------------------
cmd_switch() {
    local ARG="${1:-default}"
    local MODEL="${TASK_MODEL[$ARG]:-$ARG}"

    _ollama_up || _err "Ollama not running. Use 'start' first."
    _ensure_model "$MODEL"

    # GPU fit warning before loading
    _info "GPU fit: $(_fit_check "$MODEL")"

    # Unload current
    local CUR
    CUR=$(_current_model)
    if [ "$CUR" != "none" ] && [ "$CUR" != "$MODEL" ]; then
        _log "Unloading: $CUR ..."
        curl -s "http://localhost:$PORT/api/generate" \
            -d "{\"model\":\"$CUR\",\"keep_alive\":0}" >/dev/null 2>&1 || true
        sleep 3
    fi

    # Load new
    _log "Loading: $MODEL ..."
    local RESP
    RESP=$(curl -s --max-time 120 "http://localhost:$PORT/api/generate" \
        -H "Content-Type: application/json" \
        -d "{\"model\":\"$MODEL\",\"prompt\":\"hi\",\"stream\":false,\"keep_alive\":-1}" 2>/dev/null)

    if echo "$RESP" | python3 -c "import json,sys; json.load(sys.stdin)['response']" >/dev/null 2>&1; then
        echo "$MODEL" > "$MODEL_FILE"
        local GPU
        GPU=$(_gpu_pct "$MODEL")
        _log "Loaded: $MODEL  GPU: ${GPU}%  RAM: $(_free_h)"
        [ "$GPU" -lt 50 ] 2>/dev/null && _warn "Low GPU% → slow CPU fallback. Consider a smaller model."
    else
        _warn "Switch may have failed. Check: journalctl -u ollama -n 20"
    fi
    echo ""
}

# -------------------------------------------------------
cmd_status() {
    local CUR_MODE
    CUR_MODE=$(grep "^mode=" "$STATE_FILE" 2>/dev/null | cut -d= -f2 || echo "")
    echo ""; _sep
    if _ollama_up || _piper_up || _pipeline_up; then
        local MODEL GPU
        MODEL=$(_current_model)
        _info "Mode       : ${CUR_MODE:-local}"
        _info "Power      : $(_power)"
        _info "RAM        : $(_free_h)"
        _info "Desktop    : $([ -n "$(grep "^dm=." "$STATE_FILE" 2>/dev/null)" ] && echo 'stopped (headless)' || echo 'running')"
        if _ollama_up; then
            GPU=$(_gpu_pct "$MODEL")
            _info "LLM        : RUNNING  http://$IP:$PORT  model=$MODEL  GPU=${GPU}%"
            [ "$GPU" -lt 50 ] 2>/dev/null && [ "$MODEL" != "none" ] && \
                _warn "Low GPU% — model on CPU (expect ~0.3 tok/s). Run stop+start to free RAM."
        else
            _info "LLM        : stopped"
        fi
        _tts_status
    else
        _info "Status     : STOPPED"
        _info "Power      : $(_power)"
        _info "RAM        : $(_free_h)"
        _info "Desktop    : running"
    fi
    _sep; echo ""
}

# -------------------------------------------------------
cmd_list() {
    echo ""; _sep
    _info "Model guide — Jetson Orin 8GB"
    _sep
    printf "  %-30s %-6s %-9s %-5s %s\n" "Model" "GB" "tok/s*" "Fit?" "Best for"
    _sep
    local FREE_MB
    FREE_MB=$(_free_mb)
    _row() {
        local m=$1 gb=$2 tps=$3 task=$4
        local fit
        fit=$(awk -v f="$FREE_MB" -v g="$gb" 'BEGIN{
            need=g*1024*1.1
            if(f>=need) print "✓"
            else if(f>=g*1024) print "~"
            else print "✗"
        }')
        printf "  %-30s %-6s %-9s %-5s %s\n" "$m" "${gb}GB" "$tps" "$fit" "$task"
    }
    _row "qwen3.5:0.8b"               1.0  "~35"   "Ultra-fast, simple queries"
    _row "qwen2.5:3b"                 1.9  "~22"   "Fast multilingual"
    _row "llama3.2:3b"                2.0  "~20"   "General chat"
    _row "phi4-mini ★"               2.5  "~18"   "Reasoning / math / agents"
    _row "qwen3.5:4b ★"              3.4  "~13"   "Best all-round (default)"
    _row "gemma3:latest"              3.3  "~12"   "Quality general"
    _row "llama3.1:8b"               4.9  "~8"    "High quality (headless only)"
    _row "gemma4:e2b"                 7.2  "~5"    "Vision/multimodal"
    _row "cas/discolm-german"         7.7  "~4"    "German language"
    _row "gemma4:e4b"                 9.6  "✗CPU"  "Too big — avoid"
    _sep
    _info "★ recommended  ✓ fits  ~ tight  ✗ CPU fallback"
    _info "* tok/s estimated headless + MAXN_SUPER power mode"
    echo ""
    _info "Task aliases (use with start/switch):"
    for task in $(echo "${!TASK_MODEL[@]}" | tr ' ' '\n' | sort); do
        printf "    %-12s → %s\n" "$task" "${TASK_MODEL[$task]}"
    done
    echo ""
    _info "Installed on device:"
    ollama list 2>/dev/null | awk 'NR>1 {printf "    %-35s %s %s\n", $1, $3, $4}'
    echo ""
    _info "Pull a model: ollama pull qwen3.5:0.8b"
    echo ""
}

# -------------------------------------------------------
cmd_bench() {
    local MODEL="${1:-$(_current_model)}"
    [ "$MODEL" = "none" ] && MODEL="${TASK_MODEL[default]}"
    _ollama_up || _err "Start API first: ./jetson-ai.sh start"

    local PROMPT="In 4 sentences, explain why memory bandwidth matters more than compute for LLM inference on edge devices."
    echo ""; _sep; _info "Benchmarking: $MODEL"; _sep; echo ""
    _info "GPU fit  : $(_fit_check "$MODEL")"
    _info "GPU used : $(_gpu_pct "$MODEL")%"
    echo ""

    # Run 3 times and average
    local TOTAL_TPS=0 RUNS=0
    for run in 1 2 3; do
        printf "  Run %d/3 ... " "$run"
        local RESP
        RESP=$(curl -s --max-time 120 "http://localhost:$PORT/api/generate" \
            -H "Content-Type: application/json" \
            -d "{\"model\":\"$MODEL\",\"prompt\":\"$PROMPT\",\"stream\":false,\"keep_alive\":-1}" 2>/dev/null)

        local STATS
        STATS=$(echo "$RESP" | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    ec=d.get('eval_count',0)
    ed=d.get('eval_duration',1)/1e9
    pc=d.get('prompt_eval_count',0)
    pd=d.get('prompt_eval_duration',1)/1e9
    ld=d.get('load_duration',0)/1e9
    td=d.get('total_duration',0)/1e9
    tps=ec/max(ed,0.001)
    print(f'{tps:.1f}|{ec}|{ed:.1f}|{td:.1f}')
except Exception as e:
    print(f'0|0|0|0')
" 2>/dev/null)

        local TPS TOK DUR TOTAL
        TPS=$(echo "$STATS" | cut -d'|' -f1)
        TOK=$(echo "$STATS" | cut -d'|' -f2)
        DUR=$(echo "$STATS" | cut -d'|' -f3)
        TOTAL=$(echo "$STATS" | cut -d'|' -f4)

        printf "%s tok/s  (%s tokens in %ss)\n" "$TPS" "$TOK" "$DUR"
        TOTAL_TPS=$(awk -v a="$TOTAL_TPS" -v b="$TPS" 'BEGIN{print a+b}')
        RUNS=$((RUNS+1))
    done

    local AVG
    AVG=$(awk -v t="$TOTAL_TPS" -v n="$RUNS" 'BEGIN{printf "%.1f", t/n}')

    echo ""; _sep
    printf "  Average    : %s tok/s\n" "$AVG"
    local AVG_INT
    AVG_INT=$(echo "$AVG" | cut -d'.' -f1)
    if [ "${AVG_INT:-0}" -lt 2 ] 2>/dev/null; then
        _warn "VERY SLOW — running on CPU, not GPU!"
        _warn "Fix: ./jetson-ai.sh stop && ./jetson-ai.sh start (stops desktop)"
        _warn "Or switch to a smaller model: ./jetson-ai.sh switch phi4-mini"
    elif [ "${AVG_INT:-0}" -lt 8 ] 2>/dev/null; then
        _warn "Slow — partial CPU fallback. Try a smaller model for better speed."
    else
        _info "✓ Good GPU throughput"
    fi
    _sep; echo ""
}

# -------------------------------------------------------
cmd_tasks() {
    echo ""; _sep; _info "Task → Model Routing Guide"; _sep
    printf "  %-14s %-28s %s\n" "Task" "Model" "Why"
    _sep
    printf "  %-14s %-28s %s\n" "default"  "qwen3.5:4b"           "Best quality/speed balance"
    printf "  %-14s %-28s %s\n" "fast"     "phi4-mini"            "Lowest latency"
    printf "  %-14s %-28s %s\n" "reasoning" "phi4-mini"           "Math, logic, step-by-step"
    printf "  %-14s %-28s %s\n" "code"     "qwen3.5:4b"           "Coding, debugging, review"
    printf "  %-14s %-28s %s\n" "german"   "cas/discolm-german"   "German language tasks"
    printf "  %-14s %-28s %s\n" "vision"   "gemma4:e2b"           "Image understanding"
    printf "  %-14s %-28s %s\n" "tiny"     "qwen2.5:3b"           "Fast, minimal RAM"
    printf "  %-14s %-28s %s\n" "chat"     "llama3.2:3b"          "Casual conversation"
    printf "  %-14s %-28s %s\n" "quality"  "llama3.1:8b"          "Best output (headless only)"
    _sep
    _info "Usage:"
    _info "  ./jetson-ai.sh start reasoning"
    _info "  ./jetson-ai.sh switch code"
    echo ""
}

# -------------------------------------------------------
cmd_install_services() {
    local SYSTEMD_DIR="$HOME/.config/systemd/user"
    local SVC_SRC="$(cd "$(dirname "$0")" && pwd)/voice/systemd"

    echo ""; _sep; _info "Installing systemd user services"; _sep; echo ""
    mkdir -p "$SYSTEMD_DIR"

    for SVC in jetson-piper jetson-pipeline jetson-control jetson-bt; do
        cp "$SVC_SRC/$SVC.service" "$SYSTEMD_DIR/"
        _log "Installed $SVC.service"
    done

    systemctl --user daemon-reload
    systemctl --user enable jetson-piper jetson-pipeline jetson-control jetson-bt
    _log "Services enabled for autostart on login"

    echo ""; _sep
    _info "Services installed. They start automatically on next login."
    _info ""
    _info "Start now without rebooting:"
    _info "  systemctl --user start jetson-bt"
    _info "  systemctl --user start jetson-piper"
    _info "  systemctl --user start jetson-pipeline"
    _info "  systemctl --user start jetson-control"
    _info ""
    _info "Control API will be available at:"
    _info "  http://$IP:8080/status    ← full status"
    _info "  http://$IP:8080/speak     ← POST {prompt} to speak"
    _info "  http://$IP:8080/bt/connect"
    _sep; echo ""
}

cmd_services_status() {
    echo ""; _sep; _info "Systemd service status"; _sep
    for SVC in jetson-bt jetson-piper jetson-pipeline jetson-control; do
        local STATE
        STATE=$(systemctl --user is-active "$SVC" 2>/dev/null || echo "not-installed")
        printf "  %-24s %s\n" "$SVC" "$STATE"
    done
    echo ""; _tts_status; _sep; echo ""
}

# -------------------------------------------------------
case "${1:-help}" in
    setup)            cmd_setup ;;
    start)            cmd_start  "${2:-}" "${3:-}" ;;
    stop)             cmd_stop ;;
    switch)           cmd_switch "${2:-}" ;;
    status)           cmd_status ;;
    list)             cmd_list ;;
    bench)            cmd_bench  "${2:-}" ;;
    tasks)            cmd_tasks ;;
    pull)             shift; ollama pull "${1:-}" ;;
    log)              tail -f "$LOG" ;;
    install-services) cmd_install_services ;;
    services)         cmd_services_status ;;
    tts)
        case "${2:-help}" in
            start)  _tts_start "${3:-local}" ;;
            stop)   _tts_stop ;;
            status) _tts_status ;;
            log)    tail -f "$STATE_DIR/piper.log" "$STATE_DIR/pipeline.log" ;;
            *) _info "Usage: ./jetson-ai.sh tts start [local|api] | stop | status | log" ;;
        esac
        ;;
    *)
        echo ""
        _info "Jetson AI Controller v3.0"
        _sep
        _info "FIRST TIME:  ./jetson-ai.sh setup"
        _sep
        _info "── 3 Modes ──────────────────────────────────────────"
        _info "start  [model|task]        Mode 1: local LLM only (GPU)"
        _info "start  voice [model|task]  Mode 2: local LLM + Piper TTS"
        _info "start  api                 Mode 3: cloud API + Piper TTS (no GPU LLM)"
        _sep
        _info "stop                  Stop all services, restore desktop + power"
        _info "switch [model|task]   Hot-swap LLM model (~20s)"
        _info "status                Full status: LLM + TTS + power + RAM"
        _info "list                  Models: size, tok/s, current fit"
        _info "bench  [model]        3-run average tok/s + CPU detection"
        _info "tasks                 Task → model routing guide"
        _info "pull   <model>        Download a model (e.g. qwen3.5:0.8b)"
        _info "log                   Tail live log"
        _info "tts    start|stop|status|log   Manage voice services independently"
        _sep
        _info "── Autostart & Remote Control ────────────────────────────────"
        _info "install-services      Install systemd units (autostart on login)"
        _info "services              Show systemd service status"
        _sep
        _info "Control API (port 8080) — accessible over Tailscale/LAN:"
        _info "  GET  /status                  full system status"
        _info "  POST /speak {prompt}          speak on local speaker"
        _info "  POST /control/start {mode}    start a mode remotely"
        _info "  POST /bt/connect              connect BT speaker"
        _info "  PUT  /control/sink {sink}     switch audio output"
        _sep
        _info "Task aliases: default fast reasoning code german vision tiny chat quality"
        _sep
        _info "Memory budget:"
        _info "  local:  OS 0.5 + LLM 3.4 = 4.0 GB  (desktop off, GPU on)"
        _info "  voice:  OS 0.5 + LLM 3.4 + TTS 0.1 = 4.0 GB"
        _info "  api:    OS 0.5 + TTS 0.1 = 0.6 GB  (desktop can stay on)"
        echo ""
        ;;
esac
