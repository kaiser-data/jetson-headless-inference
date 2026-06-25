#!/usr/bin/env bash
# Download Piper TTS voice models to voice/models/
# Usage: bash voice/download-models.sh [all|en|de|en_female]
# Default: downloads all bundled voices

set -euo pipefail

MODELS_DIR="$(cd "$(dirname "$0")/models" && pwd)"
BASE_URL="https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"

declare -A VOICES=(
  ["en_US-ryan-high"]="en/en_US/ryan/high"
  ["en_US-lessac-medium"]="en/en_US/lessac/medium"
  ["de_DE-thorsten-medium"]="de/de_DE/thorsten/medium"
)

mkdir -p "$MODELS_DIR"

download_voice() {
  local name="$1"
  local path="$2"
  local onnx="$MODELS_DIR/${name}.onnx"
  local json="$MODELS_DIR/${name}.onnx.json"

  if [[ -f "$onnx" && -f "$json" ]]; then
    echo "  ✓ ${name} already present — skipping"
    return
  fi

  echo "  ↓ Downloading ${name}…"
  curl -fL --progress-bar \
    "${BASE_URL}/${path}/${name}.onnx" -o "$onnx"
  curl -fL --progress-bar \
    "${BASE_URL}/${path}/${name}.onnx.json" -o "$json"
  echo "  ✓ ${name} saved ($(du -sh "$onnx" | cut -f1))"
}

TARGET="${1:-all}"

echo "Piper voice model downloader"
echo "Target: ${TARGET}  →  ${MODELS_DIR}"
echo ""

case "$TARGET" in
  en|en_male)
    download_voice "en_US-ryan-high" "en/en_US/ryan/high" ;;
  en_female)
    download_voice "en_US-lessac-medium" "en/en_US/lessac/medium" ;;
  de)
    download_voice "de_DE-thorsten-medium" "de/de_DE/thorsten/medium" ;;
  all)
    for name in "${!VOICES[@]}"; do
      download_voice "$name" "${VOICES[$name]}"
    done ;;
  *)
    echo "Unknown target: ${TARGET}"
    echo "Usage: $0 [all|en|en_female|de]"
    exit 1 ;;
esac

echo ""
echo "Done. Models in: ${MODELS_DIR}"
