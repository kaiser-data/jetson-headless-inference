#!/usr/bin/env python3
"""Sample client — test all 3 voice pipeline modes.

Usage:
  python3 sample-client.py tts "Hello from Jetson"
  python3 sample-client.py local "Explain edge AI in 2 sentences"
  python3 sample-client.py openai "Explain edge AI in 2 sentences" --key sk-...
  python3 sample-client.py anthropic "Explain edge AI" --key sk-ant-...

Requires: pip install requests pyaudio
Audio plays live as it streams (pyaudio) or saves to out.wav if pyaudio missing.
"""

import argparse
import io
import sys
import wave
import struct

import requests

PIPELINE = "http://localhost:8000"
TTS_SVC  = "http://localhost:5500"


def _play_or_save(audio_bytes: bytes, label: str = "response"):
    # 1. Try pyaudio (cross-platform)
    try:
        import pyaudio
        buf = io.BytesIO(audio_bytes)
        with wave.open(buf, "rb") as wf:
            pa = pyaudio.PyAudio()
            stream = pa.open(
                format=pa.get_format_from_width(wf.getsampwidth()),
                channels=wf.getnchannels(),
                rate=wf.getframerate(),
                output=True,
            )
            data = wf.readframes(1024)
            while data:
                stream.write(data)
                data = wf.readframes(1024)
            stream.stop_stream()
            stream.close()
            pa.terminate()
        print("  ✓ Played audio (pyaudio)")
        return
    except ImportError:
        pass

    # 2. Try paplay via ffmpeg resample → avoids HFP resampler artifacts
    import subprocess, tempfile, os
    has_paplay = subprocess.run(["which", "paplay"], capture_output=True).returncode == 0
    has_ffmpeg = subprocess.run(["which", "ffmpeg"], capture_output=True).returncode == 0
    if has_paplay:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(audio_bytes)
            tmp = f.name
        try:
            if has_ffmpeg:
                # Resample to 16kHz mono to match HFP BT profile cleanly
                p1 = subprocess.Popen(
                    ["ffmpeg", "-i", tmp, "-ar", "16000", "-ac", "1", "-f", "wav", "pipe:1", "-loglevel", "quiet"],
                    stdout=subprocess.PIPE,
                )
                subprocess.run(
                    ["paplay", "--raw", "--rate=16000", "--channels=1", "--format=s16le"],
                    stdin=p1.stdout, check=True,
                )
                p1.wait()
            else:
                subprocess.run(["paplay", tmp], check=True)
            print("  ✓ Played audio (paplay / PulseAudio)")
        finally:
            os.unlink(tmp)
        return

    # 3. Fall back to saving
    path = f"{label}.wav"
    with open(path, "wb") as f:
        f.write(audio_bytes)
    print(f"  ✓ Saved → {path}  (install pyaudio or paplay for live playback)")


def cmd_tts(args):
    """Direct TTS — text to audio, no LLM."""
    print(f"TTS: {args.text!r}  voice={args.voice}")
    resp = requests.post(
        f"{TTS_SVC}/v1/audio/speech",
        json={"model": args.voice, "input": args.text},
        timeout=30,
    )
    resp.raise_for_status()
    print(f"  Audio: {len(resp.content):,} bytes")
    _play_or_save(resp.content, "tts")


def cmd_local(args):
    """Local LLM + voice pipeline."""
    print(f"Local LLM + voice: {args.prompt!r}  voice={args.voice}")
    body = {"prompt": args.prompt, "voice": args.voice, "mode": "local"}
    if args.model:
        body["model"] = args.model
    _stream_chat(body, "local")


def cmd_openai(args):
    """OpenAI API + voice pipeline."""
    print(f"OpenAI + voice: {args.prompt!r}  voice={args.voice}")
    body = {
        "prompt": args.prompt,
        "voice": args.voice,
        "mode": "openai",
        "model": args.model or "gpt-4o-mini",
    }
    if args.key:
        body["api_key"] = args.key
    _stream_chat(body, "openai")


def cmd_anthropic(args):
    """Anthropic API + voice pipeline."""
    print(f"Anthropic + voice: {args.prompt!r}  voice={args.voice}")
    body = {
        "prompt": args.prompt,
        "voice": args.voice,
        "mode": "anthropic",
        "model": args.model or "claude-haiku-4-5-20251001",
    }
    if args.key:
        body["api_key"] = args.key
    _stream_chat(body, "anthropic")


def _stream_chat(body: dict, label: str):
    collected = b""
    first_chunk = True
    import time
    t0 = time.time()

    with requests.post(
        f"{PIPELINE}/voice/chat", json=body, stream=True, timeout=120
    ) as resp:
        resp.raise_for_status()
        for chunk in resp.iter_content(chunk_size=4096):
            if chunk:
                if first_chunk:
                    print(f"  First audio chunk: {time.time()-t0:.2f}s")
                    first_chunk = False
                collected += chunk

    print(f"  Total audio: {len(collected):,} bytes  time={time.time()-t0:.1f}s")
    _play_or_save(collected, label)


def cmd_health(args):
    for name, url in [("Piper TTS", f"{TTS_SVC}/health"), ("Pipeline", f"{PIPELINE}/health")]:
        try:
            r = requests.get(url, timeout=3)
            print(f"  {name}: {r.json()}")
        except Exception as e:
            print(f"  {name}: OFFLINE ({e})")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Voice pipeline test client")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("tts", help="Direct TTS only")
    s.add_argument("text")
    s.add_argument("--voice", default="en")

    s = sub.add_parser("local", help="Local LLM + voice")
    s.add_argument("prompt")
    s.add_argument("--voice", default="en")
    s.add_argument("--model", default="")

    s = sub.add_parser("openai", help="OpenAI API + voice")
    s.add_argument("prompt")
    s.add_argument("--voice", default="en")
    s.add_argument("--key", default="")
    s.add_argument("--model", default="")

    s = sub.add_parser("anthropic", help="Anthropic API + voice")
    s.add_argument("prompt")
    s.add_argument("--voice", default="en")
    s.add_argument("--key", default="")
    s.add_argument("--model", default="")

    sub.add_parser("health", help="Check service status")

    args = p.parse_args()
    {
        "tts":       cmd_tts,
        "local":     cmd_local,
        "openai":    cmd_openai,
        "anthropic": cmd_anthropic,
        "health":    cmd_health,
    }[args.cmd](args)
