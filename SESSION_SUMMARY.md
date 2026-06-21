# Session Summary — Jetson Voice Agent

## What was built

### Voice pipeline (complete)

A full local voice-agent stack running headless on a Jetson Orin 8GB:

| Service | Port | File | Purpose |
|---------|------|------|---------|
| Piper TTS API | 5500 | `voice/piper-service.py` | OpenAI-compatible TTS endpoint |
| Voice Pipeline | 8000 | `voice/voice-pipeline.py` | LLM → TTS streaming server |
| Control API | 8080 | `voice/control-api.py` | Remote management via Tailscale/LAN |

### LLM modes

- **local** — Ollama on localhost:11434, default model `qwen3.5:4b`
- **openai** — OpenAI-compatible cloud API (key via `OPENAI_API_KEY`)
- **anthropic** — Anthropic API (key via `ANTHROPIC_API_KEY`)

### Audio output modes

- **stream** — HTTP chunked WAV (best for remote clients, default)
- **speaker** — play on local BT speaker, return JSON transcript
- **both** — play locally and return WAV bytes simultaneously

### Bluetooth

Speaker: **Boomcore P06** (`88:88:11:07:10:5C`) — connects via HFP (16kHz). 
Audio resampled with ffmpeg before paplay to avoid PulseAudio artifacts.

### Voices downloaded

| Alias | Model file | Size |
|-------|-----------|------|
| `en`, `en_male` | `en_US-ryan-high.onnx` | 116 MB |
| `en_female` | `en_US-lessac-medium.onnx` | 61 MB |
| `de` | `de_DE-thorsten-medium.onnx` | 61 MB |

### Systemd user services

Installed to `~/.config/systemd/user/` via `./jetson-ai.sh install-services`:

- `jetson-piper.service` — Piper TTS API
- `jetson-pipeline.service` — Voice pipeline
- `jetson-control.service` — Control API
- `jetson-bt.service` — BT auto-connect on boot
- `jetson-sync.service` + `jetson-sync.timer` — periodic calendar/email sync (every 15 min)

### Tool calling (this session)

The pipeline now supports `use_tools: true` in `/voice/chat` requests.
The LLM can call three tools:

| Tool | Description |
|------|-------------|
| `get_current_datetime` | Current date/time (no cache needed) |
| `get_calendar_events` | Upcoming calendar events from local cache |
| `get_emails` | Recent/unread inbox messages from local cache |

#### New files

```
voice/tools.py            — tool definitions (OpenAI + Anthropic format) + executors
voice/data-sync.py        — syncs CalDAV / IMAP → ~/.local/share/jetson-ai/cache/
voice/systemd/jetson-sync.service  — oneshot sync service
voice/systemd/jetson-sync.timer    — runs sync every 15 min
voice/config.template.env — credential template (copy to .voice_env)
```

#### Cache location

```
~/.local/share/jetson-ai/cache/calendar.json
~/.local/share/jetson-ai/cache/email.json
```

#### Control API additions

- `POST /sync` — trigger immediate sync (background task)
- `GET  /sync/status` — show last sync timestamps + counts

---

## Key bugs fixed (earlier in session)

| Bug | Fix |
|-----|-----|
| `synthesize_stream_raw` doesn't exist | Use `voice.synthesize(text)` → yields `AudioChunk.audio_int16_bytes` |
| Sentence splitter drops "Dr." prefix | Use `search_from` offset instead of consuming buffer text |
| qwen3.5:4b 20s thinking phase | Add `"think": False` to Ollama API payload |
| asyncio subprocess `fileno` error | Rewrite `_play_speaker` as sync `Popen` chain in thread executor |
| English voice sounding harsh | Switch default to `en_US-ryan-high`; ffmpeg resample to 16kHz |
| A2DP profile missing | Boomcore shows only HFP; use HFP + ffmpeg resample as workaround |

---

## Next steps

### 1. Configure credentials

```bash
cp voice/config.template.env .voice_env
# edit .voice_env with CalDAV/IMAP credentials
```

### 2. Install caldav package (if using CalDAV)

```bash
pip3 install caldav
```

### 3. Install the new systemd units

```bash
cp voice/systemd/jetson-sync.service ~/.config/systemd/user/
cp voice/systemd/jetson-sync.timer   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now jetson-sync.timer
```

### 4. Run first sync manually

```bash
python3 voice/data-sync.py
# check ~/.local/share/jetson-ai/cache/
```

### 5. Test tool calling

```bash
# via control API (speaker output)
curl -s http://localhost:8080/speak \
  -H "Content-Type: application/json" \
  -d '{"prompt": "What'\''s on my calendar today?", "use_tools": true}'

# or direct pipeline (streaming WAV)
curl -s http://localhost:8000/voice/chat \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Do I have any unread emails?", "use_tools": true, "output": "speaker"}'
```

### 6. Check sync status remotely

```bash
curl http://localhost:8080/sync/status
# or trigger a manual sync:
curl -X POST http://localhost:8080/sync -d '{"target":"both"}'
```

---

## Memory / resource guide

| Mode | RAM | Use case |
|------|-----|----------|
| `local` (LLM only) | ~3.9 GB | Text responses, no TTS |
| `voice` (LLM + Piper) | ~4.0 GB | Full voice agent (recommended) |
| `api` (cloud LLM + Piper) | ~0.6 GB | Cloud LLM, local TTS only |

Latency in headless/GPU mode: **2–4 s** to first audio sentence.
