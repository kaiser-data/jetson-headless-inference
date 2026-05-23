# Voice TTS Integration — Next Session Plan

## Goal
Add Piper TTS alongside the existing LLM API on the Jetson Orin 8GB.  
LLM runs on GPU, Piper runs on CPU — zero contention, parallel execution.

## User choices confirmed
- TTS only (no microphone/STT)
- Custom Piper `.onnx` model — **user will provide their already-trained model**
- Languages: English + German
- Streaming pipeline (low latency: first audio in ~1.6s)

---

## Before the session: user needs to do
Place trained model files here:
```
~/gamma4_models/voice/models/
├── your-voice-en.onnx        ← your trained English voice
├── your-voice-en.onnx.json
├── your-voice-de.onnx        ← your trained German voice (optional)
└── your-voice-de.onnx.json
```

---

## What gets built (5 files)

| File | What it does |
|---|---|
| `voice/piper-service.py` | OpenAI-compatible TTS server on port 5500 |
| `voice/voice-pipeline.py` | Streaming LLM→TTS pipeline on port 8000 |
| `voice/sentence_splitter.py` | Sentence boundary detector for streaming |
| `voice/train-guide.sh` | How to record + train English/German Piper voices |
| `voice/sample-client.py` | Python client example that plays streamed audio |

Plus updates to `jetson-ai.sh` and `README.md`.

---

## Architecture

```
  GPU (LLM, Ollama)              CPU (Piper TTS, ONNX)
  ┌──────────────────┐           ┌──────────────────────┐
  │  qwen3.5:4b      │  tokens   │  your-voice-en.onnx  │
  │  3.4 GB          │──────────►│  ~100 MB             │
  │  streams text    │  sentence │  ~50ms per sentence  │
  └──────────────────┘  chunks   └──────────────────────┘
        port 11434                      port 5500
              │                              │
              └──────────────────────────────┘
                          │
                    port 8000
                 Voice Pipeline API
             POST /voice/chat → streaming WAV

  Memory: OS 0.5 + LLM 3.4 + Piper 0.1 = 4.0 GB / 7.1 GB free ✓
```

## Streaming latency
```
  t=0.0s   LLM starts streaming tokens
  t=1.5s   First sentence complete
  t=1.55s  Piper synthesizes it (50ms)
  t=1.55s  ← USER HEARS FIRST WORD
  (continuous audio while LLM generates more)
```

---

## Install command (run at start of session)
```bash
pip install piper-tts fastapi uvicorn soundfile numpy
```

## Start command (after build)
```bash
./jetson-ai.sh start voice         # LLM + TTS
./jetson-ai.sh start voice phi4-mini  # with specific LLM
./jetson-ai.sh tts start           # TTS only (if LLM already running)
```

## Test commands
```bash
# TTS only
curl http://localhost:5500/v1/audio/speech \
  -d '{"model":"en","input":"Hello from Jetson!"}' --output test.wav

# LLM + streaming voice
curl http://localhost:8000/voice/chat \
  -d '{"prompt":"Explain edge AI in 2 sentences","voice":"en"}' --output response.wav
```

---

## Key design decisions
- Piper runs via `piper-tts` Python library (not subprocess) for speed
- Port 5500 = OpenAI `/v1/audio/speech` compatible (drop-in for any app using OpenAI TTS)
- Port 8000 = Combined LLM+TTS streaming endpoint
- German voice uses same port 5500 with `model: "de"` selector
- Training guide covers fine-tuning from checkpoint (much faster than from scratch)
  - English: fine-tune from `en_US-lessac-medium`
  - German: fine-tune from `de_DE-thorsten-medium`
