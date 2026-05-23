#!/usr/bin/env python3
"""LLM → Piper TTS streaming pipeline on port 8000.

3 modes (set VOICE_MODE env var or pass in request body):
  local     — Ollama LLM on localhost:11434 + Piper on CPU  (default)
  openai    — OpenAI-compatible cloud API + Piper on CPU
  anthropic — Anthropic API + Piper on CPU

Endpoints:
  POST /voice/chat          → streaming WAV audio
  POST /voice/tts           → direct TTS (text → WAV, no LLM)
  GET  /health              → status
"""

import asyncio
import json
import logging
import os
import struct
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import AsyncGenerator, Optional

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, Response
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).parent))
from sentence_splitter import StreamingSentenceSplitter

# ── Config from environment ──────────────────────────────────────────────────

VOICE_MODE    = os.getenv("VOICE_MODE", "local")          # local|openai|anthropic
OLLAMA_HOST   = os.getenv("OLLAMA_HOST", "localhost:11434")
OLLAMA_MODEL  = os.getenv("OLLAMA_MODEL", "qwen3.5:4b")
OPENAI_KEY    = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE   = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")
OPENAI_MODEL  = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MOD = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
MODELS_DIR    = Path(__file__).parent / "models"

VOICE_MAP = {
    "en": "en_US-lessac-medium",
    "de": "de_DE-thorsten-medium",
    "en_US-lessac-medium":  "en_US-lessac-medium",
    "de_DE-thorsten-medium": "de_DE-thorsten-medium",
}

_piper_cache: dict[str, object] = {}
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="piper")


# ── Piper voice loading ───────────────────────────────────────────────────────

def _load_voice(alias: str):
    name = VOICE_MAP.get(alias, alias)
    if name in _piper_cache:
        return _piper_cache[name]
    onnx = MODELS_DIR / f"{name}.onnx"
    if not onnx.exists():
        raise FileNotFoundError(f"Model not found: {onnx}")
    log.info("Loading voice: %s", name)
    from piper import PiperVoice
    v = PiperVoice.load(str(onnx))
    _piper_cache[name] = v
    log.info("Loaded: %s  sr=%d", name, v.config.sample_rate)
    return v


def _synth_raw(voice, text: str) -> list[bytes]:
    """Synthesize text → list of int16 PCM byte chunks (sync, for thread pool)."""
    return [chunk.audio_int16_bytes for chunk in voice.synthesize(text)]


async def _synth(voice, text: str) -> list[bytes]:
    """Run synchronous Piper synthesis in a thread so we don't block the event loop."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _synth_raw, voice, text)


# ── WAV streaming header ──────────────────────────────────────────────────────

def _wav_header(sample_rate: int) -> bytes:
    """WAV header with max data size for HTTP chunked streaming."""
    data_size  = 0x7FFF_FFFF
    byte_rate  = sample_rate * 2        # mono 16-bit
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", data_size + 36, b"WAVE",
        b"fmt ", 16, 1, 1,               # PCM, mono
        sample_rate, byte_rate, 2, 16,   # sample rate, byte rate, block align, bits
        b"data", data_size,
    )


# ── LLM streaming generators ─────────────────────────────────────────────────

async def _stream_local(
    prompt: str, model: str, system: Optional[str]
) -> AsyncGenerator[str, None]:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "think": False,          # disable thinking mode for low-latency voice
        "options": {"num_predict": 512},
    }
    url = f"http://{OLLAMA_HOST}/api/chat"
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream("POST", url, json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                token = data.get("message", {}).get("content", "")
                if token:
                    yield token
                if data.get("done"):
                    break


async def _stream_openai(
    prompt: str, model: str, api_key: str, api_base: str, system: Optional[str]
) -> AsyncGenerator[str, None]:
    from openai import AsyncOpenAI

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    client = AsyncOpenAI(api_key=api_key or OPENAI_KEY, base_url=api_base or OPENAI_BASE)
    async with client.chat.completions.stream(
        model=model or OPENAI_MODEL,
        messages=messages,
        max_tokens=1024,
    ) as stream:
        async for chunk in stream:
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if delta:
                yield delta


async def _stream_anthropic(
    prompt: str, model: str, api_key: str, system: Optional[str]
) -> AsyncGenerator[str, None]:
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=api_key or ANTHROPIC_KEY)
    kwargs = dict(
        model=model or ANTHROPIC_MOD,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    if system:
        kwargs["system"] = system
    async with client.messages.stream(**kwargs) as stream:
        async for text in stream.text_stream:
            yield text


async def _llm_stream(
    prompt: str,
    mode: str,
    model: str,
    api_key: str,
    api_base: str,
    system: Optional[str],
) -> AsyncGenerator[str, None]:
    if mode == "local":
        async for t in _stream_local(prompt, model or OLLAMA_MODEL, system):
            yield t
    elif mode == "anthropic":
        async for t in _stream_anthropic(prompt, model, api_key, system):
            yield t
    else:  # openai or any OpenAI-compatible
        async for t in _stream_openai(prompt, model, api_key, api_base, system):
            yield t


# ── Core streaming pipeline ───────────────────────────────────────────────────

async def _audio_stream(
    voice,
    prompt: str,
    mode: str,
    model: str,
    api_key: str,
    api_base: str,
    system: Optional[str],
) -> AsyncGenerator[bytes, None]:
    yield _wav_header(voice.config.sample_rate)
    splitter = StreamingSentenceSplitter(min_chars=12)

    async for token in _llm_stream(prompt, mode, model, api_key, api_base, system):
        for sentence in splitter.feed(token):
            log.debug("Synthesizing: %.60s…", sentence)
            for chunk in await _synth(voice, sentence):
                yield chunk

    remaining = splitter.flush()
    if remaining:
        log.debug("Flushing: %.60s…", remaining)
        for chunk in await _synth(voice, remaining):
            yield chunk


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="Voice Pipeline", version="1.0.0")


class ChatRequest(BaseModel):
    prompt: str
    voice: str = "en"
    mode: Optional[str] = None          # overrides VOICE_MODE env var
    model: Optional[str] = None         # LLM model override
    api_key: Optional[str] = None       # overrides env API key
    api_base: Optional[str] = None      # OpenAI base URL override
    system: Optional[str] = None        # system prompt


class TtsRequest(BaseModel):
    text: str
    voice: str = "en"


@app.post("/voice/chat")
async def voice_chat(req: ChatRequest):
    if not req.prompt.strip():
        raise HTTPException(400, "prompt is empty")
    try:
        voice = _load_voice(req.voice)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))

    mode = req.mode or VOICE_MODE
    return StreamingResponse(
        _audio_stream(
            voice, req.prompt, mode,
            req.model or "", req.api_key or "", req.api_base or "",
            req.system,
        ),
        media_type="audio/wav",
        headers={"Cache-Control": "no-cache"},
    )


@app.post("/voice/tts")
async def voice_tts(req: TtsRequest):
    """Direct TTS — text to WAV, no LLM involved."""
    if not req.text.strip():
        raise HTTPException(400, "text is empty")
    try:
        voice = _load_voice(req.voice)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    import io as _io
    import wave as _wave
    loop = asyncio.get_event_loop()
    buf = _io.BytesIO()
    await loop.run_in_executor(
        _executor,
        lambda: voice.synthesize_wav(req.text, _wave.open(buf, "wb")),
    )
    return Response(content=buf.getvalue(), media_type="audio/wav")


@app.get("/health")
def health():
    return {
        "status": "ok",
        "mode": VOICE_MODE,
        "loaded_voices": list(_piper_cache.keys()),
        "ollama_host": OLLAMA_HOST if VOICE_MODE == "local" else None,
    }


if __name__ == "__main__":
    log.info("Voice pipeline starting — mode=%s", VOICE_MODE)
    for alias in ("en", "de"):
        try:
            _load_voice(alias)
        except FileNotFoundError:
            log.warning("Voice '%s' not found — skipping preload", alias)

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
