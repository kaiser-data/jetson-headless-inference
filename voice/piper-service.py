#!/usr/bin/env python3
"""Piper TTS server — OpenAI /v1/audio/speech compatible on port 5500.

Voices auto-loaded from ../voice/models/*.onnx
Aliases: "en" → en_US-lessac-medium, "de" → de_DE-thorsten-medium
"""

import io
import logging
import sys
import wave
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

MODELS_DIR = Path(__file__).parent / "models"

# Short alias → full model filename stem
VOICE_MAP: dict[str, str] = {
    "en":                  "en_US-lessac-medium",
    "en_US":               "en_US-lessac-medium",
    "en_US-lessac":        "en_US-lessac-medium",
    "en_US-lessac-medium": "en_US-lessac-medium",
    "de":                  "de_DE-thorsten-medium",
    "de_DE":               "de_DE-thorsten-medium",
    "de_DE-thorsten":      "de_DE-thorsten-medium",
    "de_DE-thorsten-medium": "de_DE-thorsten-medium",
}

_cache: dict[str, object] = {}  # name → PiperVoice


def _resolve(alias: str) -> str:
    """Resolve alias or raw model name to the canonical stem."""
    return VOICE_MAP.get(alias, alias)


def _load(alias: str):
    name = _resolve(alias)
    if name in _cache:
        return _cache[name]
    onnx = MODELS_DIR / f"{name}.onnx"
    if not onnx.exists():
        raise FileNotFoundError(
            f"Model '{name}.onnx' not found in {MODELS_DIR}. "
            f"Available: {[p.stem for p in MODELS_DIR.glob('*.onnx')]}"
        )
    log.info("Loading voice: %s", name)
    from piper import PiperVoice  # deferred so startup is instant
    voice = PiperVoice.load(str(onnx))
    _cache[name] = voice
    log.info("Loaded: %s  sample_rate=%d", name, voice.config.sample_rate)
    return voice


def _to_wav(voice, text: str) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        voice.synthesize_wav(text, wf)
    return buf.getvalue()


app = FastAPI(title="Piper TTS", version="1.0.0")


class SpeechRequest(BaseModel):
    model: str = "en"
    input: str
    speed: Optional[float] = 1.0
    response_format: Optional[str] = "wav"


@app.post("/v1/audio/speech")
async def speech(req: SpeechRequest):
    if not req.input.strip():
        raise HTTPException(400, "input is empty")
    try:
        voice = _load(req.model)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        log.exception("Voice load error")
        raise HTTPException(500, str(e))
    wav = _to_wav(voice, req.input)
    return Response(content=wav, media_type="audio/wav")


@app.get("/v1/models")
def list_models():
    seen = set()
    out = []
    for alias, name in VOICE_MAP.items():
        if name in seen:
            continue
        seen.add(name)
        out.append({
            "id": alias,
            "name": name,
            "loaded": name in _cache,
            "available": (MODELS_DIR / f"{name}.onnx").exists(),
        })
    return {"object": "list", "data": out}


@app.get("/health")
def health():
    return {"status": "ok", "loaded": list(_cache.keys())}


if __name__ == "__main__":
    for alias in ("en", "de"):
        try:
            _load(alias)
        except FileNotFoundError:
            log.warning("Voice '%s' not found — skipping preload", alias)

    uvicorn.run(app, host="0.0.0.0", port=5500, log_level="info")
