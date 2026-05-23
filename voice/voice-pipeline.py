#!/usr/bin/env python3
"""LLM → Piper TTS streaming pipeline on port 8000.

LLM modes (VOICE_MODE env or per-request):
  local     — Ollama on localhost:11434
  openai    — OpenAI-compatible cloud API
  anthropic — Anthropic API

Output modes (per-request):
  stream    — HTTP streaming WAV (default, best for remote clients)
  speaker   — play on local speaker, return JSON {text, duration}
  both      — play locally AND return WAV (for monitoring/recording)

Endpoints:
  POST /voice/chat   → audio (stream/both) or JSON (speaker)
  POST /voice/tts    → direct text→WAV, no LLM
  GET  /health
"""

import asyncio
import io
import json
import logging
import os
import shutil
import struct
import subprocess
import sys
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import AsyncGenerator, Literal, Optional

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).parent))
from sentence_splitter import StreamingSentenceSplitter

# ── Config ────────────────────────────────────────────────────────────────────

VOICE_MODE    = os.getenv("VOICE_MODE",     "local")
OLLAMA_HOST   = os.getenv("OLLAMA_HOST",    "localhost:11434")
OLLAMA_MODEL  = os.getenv("OLLAMA_MODEL",   "qwen3.5:4b")
OPENAI_KEY    = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE   = os.getenv("OPENAI_API_BASE","https://api.openai.com/v1")
OPENAI_MODEL  = os.getenv("OPENAI_MODEL",   "gpt-4o-mini")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MOD = os.getenv("ANTHROPIC_MODEL",   "claude-haiku-4-5-20251001")
MODELS_DIR    = Path(__file__).parent / "models"

# Default audio output: stream | speaker | both
DEFAULT_OUTPUT = os.getenv("VOICE_OUTPUT", "stream")

# PulseAudio sink override (empty = system default)
PULSE_SINK = os.getenv("PULSE_SINK", "")

VOICE_MAP: dict[str, str] = {
    "en":                    "en_US-ryan-high",
    "en_US":                 "en_US-ryan-high",
    "en_male":               "en_US-ryan-high",
    "en_US-ryan-high":       "en_US-ryan-high",
    "en_female":             "en_US-lessac-medium",
    "en_US-lessac-medium":   "en_US-lessac-medium",
    "de":                    "de_DE-thorsten-medium",
    "de_DE-thorsten-medium": "de_DE-thorsten-medium",
}

_piper_cache: dict[str, object] = {}
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="piper")


# ── Voice loading ─────────────────────────────────────────────────────────────

def _load_voice(alias: str):
    name = VOICE_MAP.get(alias, alias)
    if name in _piper_cache:
        return _piper_cache[name]
    onnx = MODELS_DIR / f"{name}.onnx"
    if not onnx.exists():
        raise FileNotFoundError(f"Voice model not found: {onnx}")
    log.info("Loading voice: %s", name)
    from piper import PiperVoice
    v = PiperVoice.load(str(onnx))
    _piper_cache[name] = v
    log.info("Loaded: %s  sr=%d", name, v.config.sample_rate)
    return v


def _synth_raw(voice, text: str) -> list[bytes]:
    return [chunk.audio_int16_bytes for chunk in voice.synthesize(text)]


async def _synth(voice, text: str) -> list[bytes]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _synth_raw, voice, text)


# ── WAV helpers ───────────────────────────────────────────────────────────────

def _streaming_wav_header(sample_rate: int) -> bytes:
    """WAV header with max data length for chunked HTTP streaming."""
    data_size = 0x7FFF_FFFF
    byte_rate = sample_rate * 2
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", data_size + 36, b"WAVE",
        b"fmt ", 16, 1, 1,
        sample_rate, byte_rate, 2, 16,
        b"data", data_size,
    )


def _make_wav(pcm: bytes, sample_rate: int) -> bytes:
    """Build a complete, correctly-sized WAV from raw PCM."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


# ── Local speaker playback ────────────────────────────────────────────────────

def _play_speaker_sync(pcm: bytes, sample_rate: int) -> float:
    """Synchronous playback — runs in thread executor to avoid blocking the loop."""
    duration = len(pcm) / 2 / sample_rate
    sink_args = ["--device", PULSE_SINK] if PULSE_SINK else []

    if shutil.which("ffmpeg"):
        # ffmpeg resample to 16kHz (matches BT HFP) → paplay
        ffmpeg = subprocess.Popen(
            ["ffmpeg",
             "-f", "s16le", "-ar", str(sample_rate), "-ac", "1", "-i", "pipe:0",
             "-ar", "16000", "-ac", "1", "-f", "wav", "pipe:1",
             "-loglevel", "quiet"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        paplay = subprocess.Popen(
            ["paplay", "--raw", "--rate=16000", "--channels=1", "--format=s16le"]
            + sink_args,
            stdin=ffmpeg.stdout,
        )
        ffmpeg.stdout.close()   # let paplay detect EOF when ffmpeg finishes
        ffmpeg.communicate(input=pcm)
        paplay.wait()
    else:
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(_make_wav(pcm, sample_rate))
            tmp = f.name
        try:
            subprocess.run(["paplay", tmp] + sink_args)
        finally:
            os.unlink(tmp)

    return duration


async def _play_speaker(pcm: bytes, sample_rate: int) -> float:
    """Async wrapper — runs synchronous playback in thread pool."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _play_speaker_sync, pcm, sample_rate)


# ── LLM token streams ─────────────────────────────────────────────────────────

async def _stream_local(prompt: str, model: str, system: Optional[str]) -> AsyncGenerator[str, None]:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "think": False,
        "options": {"num_predict": 512},
    }
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream("POST", f"http://{OLLAMA_HOST}/api/chat", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if token := data.get("message", {}).get("content", ""):
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
        model=model or OPENAI_MODEL, messages=messages, max_tokens=1024
    ) as stream:
        async for chunk in stream:
            if delta := (chunk.choices[0].delta.content if chunk.choices else None):
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
    prompt: str, mode: str, model: str, api_key: str, api_base: str, system: Optional[str]
) -> AsyncGenerator[str, None]:
    if mode == "local":
        async for t in _stream_local(prompt, model or OLLAMA_MODEL, system):
            yield t
    elif mode == "anthropic":
        async for t in _stream_anthropic(prompt, model, api_key, system):
            yield t
    else:
        async for t in _stream_openai(prompt, model, api_key, api_base, system):
            yield t


# ── Core synthesis pipeline ───────────────────────────────────────────────────

async def _run_pipeline(voice, prompt: str, mode: str, model: str,
                        api_key: str, api_base: str, system: Optional[str]):
    """Run LLM→split→synth pipeline. Yields (sentence_text, pcm_chunks) pairs."""
    splitter = StreamingSentenceSplitter(min_chars=12)
    async for token in _llm_stream(prompt, mode, model, api_key, api_base, system):
        for sentence in splitter.feed(token):
            chunks = await _synth(voice, sentence)
            yield sentence, chunks
    if remaining := splitter.flush():
        chunks = await _synth(voice, remaining)
        yield remaining, chunks


# ── Output mode implementations ───────────────────────────────────────────────

async def _output_stream(voice, prompt, mode, model, api_key, api_base, system):
    """HTTP streaming WAV — lowest latency to first audio byte."""
    yield _streaming_wav_header(voice.config.sample_rate)
    async for _sentence, chunks in _run_pipeline(voice, prompt, mode, model, api_key, api_base, system):
        for chunk in chunks:
            yield chunk


async def _output_speaker_or_both(
    voice, prompt, mode, model, api_key, api_base, system, output: str
) -> tuple[bytes, str, float]:
    """Collect all PCM, play locally, return (wav_bytes, full_text, duration)."""
    all_pcm = b""
    full_text = ""
    async for sentence, chunks in _run_pipeline(voice, prompt, mode, model, api_key, api_base, system):
        full_text += (" " if full_text else "") + sentence
        all_pcm += b"".join(chunks)

    sr = voice.config.sample_rate
    duration = await _play_speaker(all_pcm, sr)
    wav = _make_wav(all_pcm, sr)
    return wav, full_text.strip(), duration


# ── FastAPI ───────────────────────────────────────────────────────────────────

app = FastAPI(title="Voice Pipeline", version="2.0.0")


class ChatRequest(BaseModel):
    prompt: str
    voice: str = "en"
    mode: Optional[str] = None      # llm mode: local|openai|anthropic
    output: Optional[str] = None    # audio output: stream|speaker|both
    model: Optional[str] = None
    api_key: Optional[str] = None
    api_base: Optional[str] = None
    system: Optional[str] = None
    save_to: Optional[str] = None   # optional path to save WAV recording


class TtsRequest(BaseModel):
    text: str
    voice: str = "en"
    output: Optional[str] = None    # stream|speaker|both
    save_to: Optional[str] = None


@app.post("/voice/chat")
async def voice_chat(req: ChatRequest):
    if not req.prompt.strip():
        raise HTTPException(400, "prompt is empty")
    try:
        voice = _load_voice(req.voice)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))

    llm_mode = req.mode or VOICE_MODE
    out_mode  = req.output or DEFAULT_OUTPUT

    if out_mode == "stream":
        return StreamingResponse(
            _output_stream(voice, req.prompt, llm_mode,
                           req.model or "", req.api_key or "",
                           req.api_base or "", req.system),
            media_type="audio/wav",
            headers={"Cache-Control": "no-cache"},
        )

    # speaker or both: collect + play locally
    wav, text, duration = await _output_speaker_or_both(
        voice, req.prompt, llm_mode,
        req.model or "", req.api_key or "", req.api_base or "",
        req.system, out_mode,
    )

    if req.save_to:
        Path(req.save_to).write_bytes(wav)
        log.info("Saved recording → %s", req.save_to)

    if out_mode == "both":
        return Response(content=wav, media_type="audio/wav")

    # speaker: return JSON summary
    return {
        "status": "ok",
        "text": text,
        "duration_s": round(duration, 2),
        "voice": req.voice,
        "saved_to": req.save_to,
    }


@app.post("/voice/tts")
async def voice_tts(req: TtsRequest):
    if not req.text.strip():
        raise HTTPException(400, "text is empty")
    try:
        voice = _load_voice(req.voice)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))

    out_mode = req.output or DEFAULT_OUTPUT
    loop = asyncio.get_event_loop()
    buf = io.BytesIO()
    await loop.run_in_executor(_executor, lambda: voice.synthesize_wav(req.text, wave.open(buf, "wb")))
    wav = buf.getvalue()

    if req.save_to:
        Path(req.save_to).write_bytes(wav)

    if out_mode == "speaker":
        pcm = wav[44:]
        duration = await _play_speaker(pcm, voice.config.sample_rate)
        return {"status": "ok", "text": req.text, "duration_s": round(duration, 2)}

    if out_mode == "both":
        asyncio.create_task(_play_speaker(wav[44:], voice.config.sample_rate))

    return Response(content=wav, media_type="audio/wav")


@app.get("/health")
def health():
    return {
        "status": "ok",
        "llm_mode": VOICE_MODE,
        "output": DEFAULT_OUTPUT,
        "loaded_voices": list(_piper_cache.keys()),
        "ollama_host": OLLAMA_HOST if VOICE_MODE == "local" else None,
        "pulse_sink": PULSE_SINK or "default",
    }


if __name__ == "__main__":
    log.info("Voice pipeline — llm_mode=%s  output=%s", VOICE_MODE, DEFAULT_OUTPUT)
    for alias in ("en", "de"):
        try:
            _load_voice(alias)
        except FileNotFoundError:
            log.warning("Voice '%s' not found — skipping preload", alias)
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
