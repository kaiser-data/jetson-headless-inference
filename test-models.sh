#!/bin/bash
# =============================================================
# Jetson AI — Model Test Suite
# Auto-detects installed models, runs 2-prompt average,
# checks GPU vs CPU placement, reports pass/warn/fail.
# Usage:
#   ./test-models.sh              # test all installed models
#   ./test-models.sh qwen3.5:4b   # test one specific model
# =============================================================

PORT=11434
LOG_DIR="$HOME/.local/share/jetson-ai"
LOG="$LOG_DIR/test-$(date +%Y%m%d-%H%M%S).log"
mkdir -p "$LOG_DIR"

PASS=0; WARN=0; FAIL=0

# Two short prompts — balanced for speed + quality check
PROMPTS=(
    "What is 17 * 23? Think step by step."
    "Translate to German: The weather is nice today."
)

# Known model sizes (GB) for fit estimation
declare -A MODEL_GB=(
    [qwen3.5:0.8b]=1.0  [qwen3.5:2b]=2.7    [qwen3.5:4b]=3.4
    [phi4-mini]=2.5      [phi4-mini:latest]=2.5
    [llama3.2:3b]=2.0    [llama3.1:8b]=4.9
    [qwen2.5:3b]=1.9     [gemma3:latest]=3.3
    [gemma4:e2b]=7.2     [gemma4:e4b]=9.6
    [cas/discolm-mfto-german:latest]=7.7
)

_sep()  { printf '  '; printf '─%.0s' {1..60}; echo; }
_free() { free -m | awk '/^Mem:/ {print $7}'; }

_check_prereqs() {
    if ! curl -s --max-time 2 "http://localhost:$PORT/" >/dev/null 2>&1; then
        echo ""
        echo "  ✗  Ollama is not running."
        echo "     Start it: ./jetson-ai.sh start"
        echo "     Or just:  sudo systemctl start ollama"
        exit 1
    fi
}

_gpu_pct() {
    local MODEL="$1"
    curl -s --max-time 3 "http://localhost:$PORT/api/ps" 2>/dev/null \
    | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    for m in d.get('models',[]):
        if '$(echo "$MODEL" | sed "s/'/\\\\'/g")' in m.get('name',''):
            sv=m.get('size_vram',0); s=m.get('size',1)
            print(int(sv*100/s) if s else 0)
            sys.exit(0)
    print(0)
except: print(0)
" 2>/dev/null || echo 0
}

_test_model() {
    local MODEL="$1"
    local SHORT="${MODEL%%:*}"
    local SIZE_GB="${MODEL_GB[$MODEL]:-${MODEL_GB[$SHORT]:-?}}"

    printf "  %-35s" "$MODEL"

    # Load model
    local LOAD_RESP
    LOAD_RESP=$(curl -s --max-time 90 "http://localhost:$PORT/api/generate" \
        -H "Content-Type: application/json" \
        -d "{\"model\":\"$MODEL\",\"prompt\":\"hi\",\"stream\":false,\"keep_alive\":-1}" 2>/dev/null)

    if ! echo "$LOAD_RESP" | python3 -c "import json,sys; json.load(sys.stdin)['response']" >/dev/null 2>&1; then
        printf "FAIL  (load timeout or error)\n"
        echo "[$MODEL] FAIL load" >> "$LOG"
        FAIL=$((FAIL+1))
        return
    fi

    local GPU
    GPU=$(_gpu_pct "$MODEL")

    # Run both prompts, collect tok/s
    local TOTAL_TPS=0 RUNS=0 LAST_RESP=""
    for PROMPT in "${PROMPTS[@]}"; do
        local RESP
        RESP=$(curl -s --max-time 60 "http://localhost:$PORT/api/generate" \
            -H "Content-Type: application/json" \
            -d "{\"model\":\"$MODEL\",\"prompt\":\"$PROMPT\",\"stream\":false,\"keep_alive\":-1}" 2>/dev/null)
        local TPS
        TPS=$(echo "$RESP" | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    ec=d.get('eval_count',0); ed=d.get('eval_duration',1)/1e9
    print(f'{ec/max(ed,0.001):.1f}')
    sys.stdout.flush()
except: print('0')
" 2>/dev/null || echo 0)
        TOTAL_TPS=$(awk -v a="$TOTAL_TPS" -v b="$TPS" 'BEGIN{print a+b}')
        RUNS=$((RUNS+1))
        LAST_RESP=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin).get('response','')[:60])" 2>/dev/null || echo "")
    done

    local AVG_TPS
    AVG_TPS=$(awk -v t="$TOTAL_TPS" -v n="$RUNS" 'BEGIN{printf "%.1f", t/n}')
    local AVG_INT
    AVG_INT=$(echo "$AVG_TPS" | cut -d'.' -f1)

    # Verdict
    local STATUS ICON
    if [ "${AVG_INT:-0}" -lt 2 ] 2>/dev/null; then
        ICON="✗"; STATUS="FAIL  CPU fallback (${AVG_TPS} tok/s, GPU:${GPU}%)"
        FAIL=$((FAIL+1))
    elif [ "${AVG_INT:-0}" -lt 6 ] 2>/dev/null; then
        ICON="⚠"; STATUS="WARN  slow (${AVG_TPS} tok/s, GPU:${GPU}%)"
        WARN=$((WARN+1))
    else
        ICON="✓"; STATUS="PASS  ${AVG_TPS} tok/s  GPU:${GPU}%  ${SIZE_GB}GB"
        PASS=$((PASS+1))
    fi

    printf "%s %s\n" "$ICON" "$STATUS"
    [ -n "$LAST_RESP" ] && printf "         └─ \"%s\"\n" "$LAST_RESP"
    echo "[$MODEL] $ICON $STATUS | $LAST_RESP" >> "$LOG"

    # Unload before next model
    curl -s "http://localhost:$PORT/api/generate" \
        -d "{\"model\":\"$MODEL\",\"keep_alive\":0}" >/dev/null 2>&1 || true
    sleep 4
}

# -------------------------------------------------------
_check_prereqs

# Determine which models to test
if [ $# -gt 0 ]; then
    MODELS=("$@")
else
    # Auto-detect all installed models, skip obvious too-big ones
    FREE_MB=$(_free)
    mapfile -t MODELS < <(ollama list 2>/dev/null | awk 'NR>1{print $1}')
    # Sort: small first (for faster feedback)
    MODELS_SORTED=()
    for m in "${MODELS[@]}"; do
        SHORT="${m%%:*}"
        GB="${MODEL_GB[$m]:-${MODEL_GB[$SHORT]:-5}}"
        echo "$GB $m"
    done | sort -n | awk '{print $2}' | mapfile -t MODELS_SORTED
    MODELS=("${MODELS_SORTED[@]}")
fi

echo ""
_sep
echo "  Jetson AI — Model Test Suite"
printf "  Date  : %s\n" "$(date '+%Y-%m-%d %H:%M')"
printf "  Power : %s\n" "$(nvpmodel -q 2>/dev/null | awk '/NV Power Mode/{print $NF}' || echo 'unknown')"
printf "  RAM   : %s free\n" "$(free -h | awk '/^Mem:/{print $4}')"
printf "  Models: %d to test\n" "${#MODELS[@]}"
_sep
echo ""
printf "  %-35s %s\n" "Model" "Result"
_sep

for MODEL in "${MODELS[@]}"; do
    _test_model "$MODEL"
done

echo ""
_sep
printf "  ✓ %d passed    ⚠ %d warnings    ✗ %d failed\n" "$PASS" "$WARN" "$FAIL"
printf "  Log: %s\n" "$LOG"
_sep
echo ""

if [ $((WARN + FAIL)) -gt 0 ]; then
    echo "  Fixes:"
    echo "   ✗ FAIL/CPU fallback  → model too large for available GPU RAM"
    echo "     Fix A: ./jetson-ai.sh stop then ./jetson-ai.sh start (stops desktop, frees ~1.5GB)"
    echo "     Fix B: switch to smaller model"
    echo ""
    echo "   ⚠ WARN/slow          → partial GPU placement or thermal throttling"
    echo "     Fix: check power mode (nvpmodel -q), should be MAXN_SUPER for benchmarks"
    echo ""
fi
