---
name: jetson-stack
description: Operate, verify, and debug this repo's Jetson voice AI stack — services, ports, model catalogue, and the gotchas that aren't visible from the code. Use when starting/stopping/testing services, changing pipeline/control-api/data-sync code, adding models, or debugging "no audio" / "slow inference" / "service won't start" issues.
---

# Jetson Voice AI Stack — Operations

Four services run on this device (Jetson Orin Nano 8GB). All bind 0.0.0.0.

| Port | Service | Source | Health |
|---|---|---|---|
| 11434 | Ollama (GPU LLM) | system service `ollama` | `curl :11434/` |
| 5500 | Piper TTS | `voice/piper-service.py` | `curl :5500/health` |
| 8000 | Voice pipeline (LLM→TTS) | `voice/voice-pipeline.py` | `curl :8000/health` |
| 8080 | Control API | `voice/control-api.py` | `curl :8080/health` |

Run as **systemd user services**: `jetson-piper`, `jetson-pipeline`, `jetson-control`, `jetson-bt`, plus `jetson-sync.timer` (15-min calendar/email sync). `./jetson-ai.sh` is the manual controller (start/stop/switch/bench/status).

## Critical gotchas (learned the hard way)

- **Unit files in `voice/systemd/` are templates.** They reference `%h/gamma4_models`, which does not exist — `./jetson-ai.sh install-services` rewrites that to the real repo path when copying to `~/.config/systemd/user/`. Never `cp` units manually; always reinstall via the script, and re-run it if the repo moves.
- **After editing any `voice/*.py`, restart the service** — code changes do nothing until then:
  `systemctl --user daemon-reload && systemctl --user restart jetson-piper jetson-pipeline jetson-control`
- **Config lives in `.voice_env` at the repo root** (template: `voice/config.template.env`). Loaded by systemd `EnvironmentFile` and parsed by `data-sync.py`. Contains IMAP creds and API keys — never commit (gitignored).
- **`VOICE_API_TOKEN`** (optional, in `.voice_env`): when set, ports 8000/8080 require `Authorization: Bearer <token>` on everything except `/health`. Health checks in `jetson-ai.sh` stay unauthenticated on purpose.
- **`save_to` in API requests is a bare filename**, saved under `~/.local/share/jetson-ai/recordings/` — paths are deliberately stripped (path-traversal fix). Don't "fix" it back to accepting paths.
- **Voice models are not in git.** `voice/models/*.onnx` comes from `bash voice/download-models.sh` (~240 MB). Pipeline aliases: `en` → ryan-high, `en_female` → lessac, `de` → thorsten.
- **Tool calling needs the cache.** `use_tools:true` reads `~/.local/share/jetson-ai/cache/{calendar,email}.json`, written by `python3 voice/data-sync.py` (or `POST :8080/sync`). Empty cache → the tools tell the LLM to ask the user to sync. Tools are read-only by design; keep them that way unless adding explicit confirmation flows.
- **State/logs**: `~/.local/share/jetson-ai/` — `pipeline.log`, `piper.log`, `control.log`, `sync.log`, `bt.log`, plus state files used by `jetson-ai.sh`.

## Verify a change end-to-end

```bash
systemctl --user restart jetson-piper jetson-pipeline jetson-control && sleep 8
curl -s :8000/health   # expect llm_mode, loaded_voices, tool_cache keys
curl -s :8080/health
# TTS round-trip (writes to recordings dir):
curl -s -X POST :8000/voice/tts -H "Content-Type: application/json" \
  -d '{"text":"Verification test.","voice":"en","output":"stream"}' -o /dev/null -w "%{http_code}\n"
./jetson-ai.sh status   # Pipeline line must show mode=local (not mode=?)
```

Speaker output test needs the BT speaker connected: `POST :8080/bt/connect`, then `output":"speaker"`. Playback failures now raise HTTP 500 (not silent success) — check `PULSE_SINK` and `GET :8080/control/sinks`.

## Models — the 8 GB rules

- **The silent CPU-fallback trap**: a model that doesn't fit GPU RAM still runs, at ~0.3 tok/s instead of 13–35. `./jetson-ai.sh bench` and `status` detect it (GPU% < 50 = bad). Headless (`start` stops GNOME) frees 1.5 GB.
- **The page-cache OOM trap** (learned 2026-07-09): after heavy disk I/O (pip installs, `ollama pull`), LLM loads fail with `cudaMalloc failed` / `NvMapMemAlloc error 12` even though `free` shows GBs "available" — NvMap won't force page-cache reclaim. Fix: `sudo sysctl vm.drop_caches=3`, or rootless: allocate+free a ~3 GB bytearray in python to balloon the cache out. Check the **buff/cache** column, not "available".
- **A Claude Code session running on the Jetson costs ~700 MB** — benchmark from the Mac (jetson-bench repo), not from an on-device session, or 4 GB-class models won't fit.
- **Prefer `-qat` tags for Gemma 4**: `gemma4:e2b-it-qat` = 4.3 GB vs plain `e2b` 7.2 GB, near-identical quality. Ollama default tags are already Q4_K_M (edge-optimal); avoid `q8` variants on 8 GB.
- **Adding a model**: update BOTH `TASK_MODEL` (alias) and `MODEL_GB` (fit-check size) associative arrays in `jetson-ai.sh`, plus the `cmd_list`/`cmd_tasks` display rows and the README chart. A model missing from `MODEL_GB` degrades gracefully (size 0 → "size unknown") but the fit warning is lost.
- Task aliases: default/code→qwen3.5:4b · fast/reasoning→phi4-mini · tiny→qwen3.5:0.8b · vision→gemma4:e2b-it-qat · vision-max→e4b-it-qat (headless only) · quality→llama3.1:8b · german→discolm (7.7 GB, tight).

## Suspend / Wake-on-LAN (headless remote use)

- One-time root setup: `sudo ./wol-setup.sh` — enables magic-packet wake on
  `eno1` (persists via `wol-enable.service`, re-applied after every resume),
  installs `jetson-resume-perf.service` (**every wake from suspend lands in
  MAXN_SUPER + jetson_clocks automatically**), and whitelists
  suspend/nvpmodel/jetson_clocks in `/etc/sudoers.d/jetson-wake`.
- `POST :8080/power/mode` (`{"mode":"high"|"low"}`) — manual profile switch
  from anywhere (high = MAXN_SUPER `-m 2`, low = 15W `-m 0`). Resume already
  auto-switches to high; use this to drop back to low when leaving it awake.
- `POST :8080/power/headless` (`{"headless":true|false}`) — stop/start GNOME
  remotely; stopping frees ~1.5 GB (needed for 4 GB-class models).
- wol-setup.sh also installs the **Ollama drop-in** (`jetson-ai.sh setup` was
  never run on this box): binds 0.0.0.0 (direct LAN/tailnet access, no SSH
  bridge), FlashAttention + q8 KV, `NUM_PARALLEL=1` (deliberately not 2 —
  halves KV; don't let `jetson-ai.sh setup` overwrite it back to 2 on 8 GB).
- `POST :8080/power/suspend` (`{"delay_s":3}`) suspends to RAM (deep/SC7);
  returns `wake_mac`. Token-protected like the rest of 8080. Without the
  sudoers rule it fails harmlessly (error in control.log).
- Wake: magic packet to `3c:6d:66:76:81:66` — **LAN broadcast only, does not
  traverse Tailscale**; sender must be on the same LAN or use a LAN relay.
- **Mac-side tooling lives in a separate repo**: `kaiser-data/jetson-bench`
  (local: `~/dev/projects/jetson-bench`) — `wake-and-run.sh` (wake → task →
  suspend; `--audio file` for STT; `NO_SUSPEND=1` to keep awake) and `bench.py`
  (connectivity/model/TTS/STT suite, saves JSON results + dashboard.html,
  `--push` commits them). This repo stays Jetson-internal.
- **Never call /power/suspend from a session running on the Jetson itself** —
  it kills your own SSH/Claude session.

## STT / transcription

- `POST :8000/voice/transcribe` — multipart `file` (wav/mp3/m4a/ogg), optional
  `language` form field, else auto-detect. Returns text, language,
  language_probability, audio_duration_s. faster-whisper, `WHISPER_MODEL` env
  (default `small`), lazy-loaded on first call (~460 MB download, then cached).
- **CPU on purpose** (never competes with the LLM for GPU) — but the loaded
  model costs **~1 GB of the same unified 8 GB**. Loaded whisper + GNOME
  running is enough to OOM a 3.4 GB LLM load ("cudaMalloc failed"). Headless
  fixes it; a pipeline restart unloads whisper back to lazy.
- Warm speed ~0.4× realtime on `small` (9 s for a 3.7 s clip); use
  `WHISPER_MODEL=base` for faster/rougher.

## Repo conventions

- Bash runs under `set -euo pipefail`: quote everything; assoc-array fallbacks must be `${ARR[key]:-default}` (the `:-` **outside** the brackets — inside is an unbound-variable crash).
- README architecture diagram = `assets/architecture.svg` + `architecture-dark.svg` via `<picture>` (GitHub strips CSS). Edit the light SVG, then regenerate dark with the sed color map: `#1f2328→#e6edf3 #57606a→#9198a1 #f6f8fa→#161b22 #d0d7de→#30363d #ffffff→#0d1117 #0969da→#4493f8 #8250df→#a371f7 #bc4c00→#f0883e` (keep `#76B900`). Preview with `cairosvg` (pip).
- GitHub remote is `jetson-headless-inference` (repo dir name differs).
