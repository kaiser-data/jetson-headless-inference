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

Tool calling (use_tools=true in request):
  Adds calendar/email tool access via local JSON cache.
  Run voice/data-sync.py to populate cache.
  Only supported in local (Ollama) and openai modes.

Endpoints:
  POST /voice/chat        → audio (stream/both) or JSON (speaker)
  POST /voice/tts         → direct text→WAV, no LLM
  POST /voice/transcribe  → audio file → text (faster-whisper, auto language detect)
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
import threading
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import AsyncGenerator, Literal, Optional

import httpx
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
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
from tools import TOOL_DEFS, TOOL_DEFS_ANTHROPIC, execute_tool

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

# Optional bearer token — if set, all endpoints except /health require
# "Authorization: Bearer <token>". Leave empty for open LAN/Tailscale use.
API_TOKEN = os.getenv("VOICE_API_TOKEN", "")

# save_to recordings land here — callers supply a filename, never a path
RECORDINGS_DIR = Path.home() / ".local/share/jetson-ai/recordings"

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

# Whisper STT — lazy-loaded on first /voice/transcribe, CPU-only on purpose:
# transcription must never compete with the LLM for the 8 GB of GPU RAM.
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")

_whisper = None
_whisper_lock = threading.Lock()
_whisper_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="whisper")


def _get_whisper():
    global _whisper
    with _whisper_lock:
        if _whisper is None:
            from faster_whisper import WhisperModel
            log.info("Loading whisper '%s' (cpu/int8) — first call downloads it", WHISPER_MODEL)
            _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
            log.info("Whisper '%s' loaded", WHISPER_MODEL)
        return _whisper


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
    """Synchronous playback — runs in thread executor to avoid blocking the loop.

    Raises RuntimeError if playback fails, so callers don't report success silently.
    """
    duration = len(pcm) / 2 / sample_rate
    sink_args = ["--device", PULSE_SINK] if PULSE_SINK else []

    if not shutil.which("paplay"):
        raise RuntimeError("paplay not found — install pulseaudio-utils")

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
        if ffmpeg.returncode != 0:
            raise RuntimeError(f"ffmpeg resample failed (rc={ffmpeg.returncode})")
        if paplay.returncode != 0:
            raise RuntimeError(f"paplay failed (rc={paplay.returncode}) — check sink '{PULSE_SINK or 'default'}'")
    else:
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(_make_wav(pcm, sample_rate))
            tmp = f.name
        try:
            subprocess.run(["paplay", tmp] + sink_args, check=True)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"paplay failed (rc={e.returncode})")
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


# ── Tool-calling LLM (non-streaming, multi-round) ────────────────────────────

MAX_TOOL_ROUNDS = 4   # max tool-call rounds before forcing a final answer

async def _llm_with_tools_local(messages: list, model: str) -> str:
    """Ollama tool-call resolution loop. Returns final assistant text."""
    async with httpx.AsyncClient(timeout=120) as client:
        for _ in range(MAX_TOOL_ROUNDS):
            payload = {
                "model": model,
                "messages": messages,
                "tools": TOOL_DEFS,
                "stream": False,
                "think": False,
                "options": {"num_predict": 1024},
            }
            resp = await client.post(f"http://{OLLAMA_HOST}/api/chat", json=payload)
            resp.raise_for_status()
            msg = resp.json().get("message", {})
            tool_calls = msg.get("tool_calls") or []

            if not tool_calls:
                return msg.get("content", "")

            messages.append({
                "role": "assistant",
                "content": msg.get("content", ""),
                "tool_calls": tool_calls,
            })
            for tc in tool_calls:
                fn     = tc.get("function", {})
                name   = fn.get("name", "")
                args   = fn.get("arguments", {})
                result = execute_tool(name, args)
                log.info("Tool: %s(%s) → %d chars", name, args, len(result))
                messages.append({"role": "tool", "content": result, "tool_name": name})

    return "I was unable to complete your request after several attempts."


async def _llm_with_tools_openai(
    messages: list, model: str, api_key: str, api_base: str
) -> str:
    """OpenAI-compatible tool-call loop (also works with local OpenAI-format proxies)."""
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=api_key or OPENAI_KEY, base_url=api_base or OPENAI_BASE)

    for _ in range(MAX_TOOL_ROUNDS):
        response = await client.chat.completions.create(
            model=model or OPENAI_MODEL,
            messages=messages,
            tools=TOOL_DEFS,
            max_tokens=1024,
        )
        msg        = response.choices[0].message
        tool_calls = msg.tool_calls or []

        if not tool_calls:
            return msg.content or ""

        messages.append({"role": "assistant", "content": msg.content or "", "tool_calls": [
            {"id": tc.id, "type": "function",
             "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
            for tc in tool_calls
        ]})
        for tc in tool_calls:
            args   = json.loads(tc.function.arguments)
            result = execute_tool(tc.function.name, args)
            log.info("Tool: %s(%s) → %d chars", tc.function.name, args, len(result))
            messages.append({"role": "tool", "content": result, "tool_call_id": tc.id})

    return "I was unable to complete your request after several attempts."


async def _llm_with_tools_anthropic(
    messages: list, system: Optional[str], model: str, api_key: str
) -> str:
    """Anthropic tool-call loop."""
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=api_key or ANTHROPIC_KEY)

    for _ in range(MAX_TOOL_ROUNDS):
        kwargs = dict(
            model=model or ANTHROPIC_MOD,
            max_tokens=1024,
            messages=messages,
            tools=TOOL_DEFS_ANTHROPIC,
        )
        if system:
            kwargs["system"] = system
        response = await client.messages.create(**kwargs)

        # Collect text and tool_use blocks
        text_parts = []
        tool_uses  = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_uses.append(block)

        if not tool_uses:
            return " ".join(text_parts)

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for tu in tool_uses:
            result = execute_tool(tu.name, tu.input)
            log.info("Tool: %s(%s) → %d chars", tu.name, tu.input, len(result))
            tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

    return "I was unable to complete your request after several attempts."


async def _resolve_with_tools(
    prompt: str, mode: str, model: str, api_key: str, api_base: str, system: Optional[str]
) -> str:
    """Run the appropriate tool-calling loop and return the final answer text."""
    messages = []
    if system and mode not in ("anthropic",):
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    if mode == "local":
        return await _llm_with_tools_local(messages, model or OLLAMA_MODEL)
    if mode == "anthropic":
        return await _llm_with_tools_anthropic(messages, system, model, api_key)
    return await _llm_with_tools_openai(messages, model, api_key, api_base)


async def _synth_text_to_audio(voice, text: str):
    """Sentence-split a complete text string and yield WAV header + PCM chunks."""
    yield _streaming_wav_header(voice.config.sample_rate)
    splitter = StreamingSentenceSplitter(min_chars=12)
    for sentence in splitter.feed(text):
        for chunk in await _synth(voice, sentence):
            yield chunk
    if remaining := splitter.flush():
        for chunk in await _synth(voice, remaining):
            yield chunk


def _save_recording(wav: bytes, save_to: str) -> str:
    """Save WAV under RECORDINGS_DIR only — callers can't pick arbitrary paths."""
    name = Path(save_to).name or "recording.wav"
    if not name.endswith(".wav"):
        name += ".wav"
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    path = RECORDINGS_DIR / name
    path.write_bytes(wav)
    log.info("Saved recording → %s", path)
    return str(path)


# ── FastAPI ───────────────────────────────────────────────────────────────────

app = FastAPI(title="Voice Pipeline", version="2.1.0")

# Keep references to fire-and-forget playback tasks so they aren't GC'd mid-play
_bg_tasks: set = set()


def _spawn_bg(coro, label: str):
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)

    def _done(t):
        _bg_tasks.discard(t)
        if not t.cancelled() and t.exception():
            log.error("%s failed: %s", label, t.exception())

    task.add_done_callback(_done)


@app.middleware("http")
async def _auth_middleware(request, call_next):
    if API_TOKEN and request.url.path != "/health":
        if request.headers.get("authorization") != f"Bearer {API_TOKEN}":
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


class ChatRequest(BaseModel):
    prompt: str
    voice: str = "en"
    mode: Optional[str] = None      # llm mode: local|openai|anthropic
    output: Optional[str] = None    # audio output: stream|speaker|both
    model: Optional[str] = None
    api_key: Optional[str] = None
    api_base: Optional[str] = None
    system: Optional[str] = None
    save_to: Optional[str] = None   # filename — saved under ~/.local/share/jetson-ai/recordings/
    use_tools: bool = False          # enable calendar/email tool calling


class TtsRequest(BaseModel):
    text: str
    voice: str = "en"
    output: Optional[str] = None    # stream|speaker|both
    save_to: Optional[str] = None   # filename — saved under ~/.local/share/jetson-ai/recordings/


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

    # ── Tool-calling path (non-streaming LLM, then synthesize final answer) ──
    if req.use_tools:
        final_text = await _resolve_with_tools(
            req.prompt, llm_mode,
            req.model or "", req.api_key or "", req.api_base or "", req.system,
        )
        log.info("Tool answer (%d chars): %s…", len(final_text), final_text[:80])

        if out_mode == "stream":
            return StreamingResponse(
                _synth_text_to_audio(voice, final_text),
                media_type="audio/wav",
                headers={"Cache-Control": "no-cache"},
            )

        # speaker or both: synthesise all PCM, then play
        splitter = StreamingSentenceSplitter(min_chars=12)
        all_pcm  = b""
        for sentence in splitter.feed(final_text):
            all_pcm += b"".join(await _synth(voice, sentence))
        if remaining := splitter.flush():
            all_pcm += b"".join(await _synth(voice, remaining))

        sr       = voice.config.sample_rate
        duration = await _play_speaker(all_pcm, sr)
        wav      = _make_wav(all_pcm, sr)

        saved = _save_recording(wav, req.save_to) if req.save_to else None

        if out_mode == "both":
            return Response(content=wav, media_type="audio/wav")

        return {
            "status": "ok",
            "text": final_text,
            "duration_s": round(duration, 2),
            "voice": req.voice,
            "saved_to": saved,
        }

    # ── Standard streaming path ───────────────────────────────────────────────
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

    saved = _save_recording(wav, req.save_to) if req.save_to else None

    if out_mode == "both":
        return Response(content=wav, media_type="audio/wav")

    return {
        "status": "ok",
        "text": text,
        "duration_s": round(duration, 2),
        "voice": req.voice,
        "saved_to": saved,
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

    def _synth_wav():
        # close the wave file so RIFF size headers get patched
        with wave.open(buf, "wb") as wf:
            voice.synthesize_wav(req.text, wf)

    await loop.run_in_executor(_executor, _synth_wav)
    wav = buf.getvalue()

    saved = _save_recording(wav, req.save_to) if req.save_to else None

    if out_mode == "speaker":
        pcm = wav[44:]
        duration = await _play_speaker(pcm, voice.config.sample_rate)
        return {"status": "ok", "text": req.text, "duration_s": round(duration, 2),
                "saved_to": saved}

    if out_mode == "both":
        _spawn_bg(_play_speaker(wav[44:], voice.config.sample_rate), "tts playback")

    return Response(content=wav, media_type="audio/wav")


@app.post("/voice/transcribe")
async def voice_transcribe(
    file: UploadFile = File(...),
    language: Optional[str] = Form(None),
):
    """Audio file (wav/mp3/m4a/ogg/...) → text. Auto-detects language unless one is given."""
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty audio file")

    def _run():
        model = _get_whisper()
        segments, info = model.transcribe(io.BytesIO(data), language=language,
                                          vad_filter=True)
        # segments is a lazy generator — consuming it does the actual work
        text = " ".join(s.text.strip() for s in segments)
        return text, info

    loop = asyncio.get_event_loop()
    try:
        text, info = await loop.run_in_executor(_whisper_executor, _run)
    except Exception as e:
        raise HTTPException(500, f"transcription failed: {e}")

    return {
        "text": text,
        "language": info.language,
        "language_probability": round(info.language_probability, 3),
        "audio_duration_s": round(info.duration, 2),
        "model": WHISPER_MODEL,
    }


@app.get("/health")
def health():
    cache_dir = Path.home() / ".local/share/jetson-ai/cache"
    cache_files = {
        p.stem: p.stat().st_mtime
        for p in cache_dir.glob("*.json")
    } if cache_dir.exists() else {}
    return {
        "status": "ok",
        "llm_mode": VOICE_MODE,
        "output": DEFAULT_OUTPUT,
        "loaded_voices": list(_piper_cache.keys()),
        "ollama_host": OLLAMA_HOST if VOICE_MODE == "local" else None,
        "pulse_sink": PULSE_SINK or "default",
        "tool_cache": {k: "present" for k in cache_files},
        "whisper": {"model": WHISPER_MODEL, "loaded": _whisper is not None},
    }


if __name__ == "__main__":
    log.info("Voice pipeline — llm_mode=%s  output=%s", VOICE_MODE, DEFAULT_OUTPUT)
    for alias in ("en", "de"):
        try:
            _load_voice(alias)
        except FileNotFoundError:
            log.warning("Voice '%s' not found — skipping preload", alias)
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
