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
- **Prefer `-qat` tags for Gemma 4**: `gemma4:e2b-it-qat` = 4.3 GB vs plain `e2b` 7.2 GB, near-identical quality. Ollama default tags are already Q4_K_M (edge-optimal); avoid `q8` variants on 8 GB.
- **Adding a model**: update BOTH `TASK_MODEL` (alias) and `MODEL_GB` (fit-check size) associative arrays in `jetson-ai.sh`, plus the `cmd_list`/`cmd_tasks` display rows and the README chart. A model missing from `MODEL_GB` degrades gracefully (size 0 → "size unknown") but the fit warning is lost.
- Task aliases: default/code→qwen3.5:4b · fast/reasoning→phi4-mini · tiny→qwen3.5:0.8b · vision→gemma4:e2b-it-qat · vision-max→e4b-it-qat (headless only) · quality→llama3.1:8b · german→discolm (7.7 GB, tight).

## Repo conventions

- Bash runs under `set -euo pipefail`: quote everything; assoc-array fallbacks must be `${ARR[key]:-default}` (the `:-` **outside** the brackets — inside is an unbound-variable crash).
- README architecture diagram = `assets/architecture.svg` + `architecture-dark.svg` via `<picture>` (GitHub strips CSS). Edit the light SVG, then regenerate dark with the sed color map: `#1f2328→#e6edf3 #57606a→#9198a1 #f6f8fa→#161b22 #d0d7de→#30363d #ffffff→#0d1117 #0969da→#4493f8 #8250df→#a371f7 #bc4c00→#f0883e` (keep `#76B900`). Preview with `cairosvg` (pip).
- GitHub remote is `jetson-headless-inference` (repo dir name differs).
