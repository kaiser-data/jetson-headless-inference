# Session Handover — 2026-07-09 (evening update)

Read `.claude/skills/jetson-stack/SKILL.md` first — it holds the operational
knowledge (services, ports, verify workflow, gotchas). This file is the
*state and open decisions* snapshot.

## Evening session additions (uncommitted in this repo)

- **Suspend/WoL**: `POST :8080/power/suspend` + `wol-setup.sh` (root, not yet
  run — WoL inactive until then). Mac + Jetson on same LAN, Jetson wired.
- **STT**: `POST :8000/voice/transcribe` (faster-whisper small, CPU, lazy,
  auto language detect — verified EN + DE round-trip via Piper).
- **New separate repo** `kaiser-data/jetson-bench` (private,
  `~/dev/projects/jetson-bench`): bench.py suite + dashboard.html +
  wake-and-run.sh — all Mac-side/external tooling lives there now.
- **GNOME stopped** (headless) this session — whisper (~1 GB) + GNOME made
  3.4 GB LLM loads OOM on the unified 8 GB.

## Current state: healthy ✅

All services active and verified end-to-end (health checks + TTS round-trip):
`jetson-piper` (:5500), `jetson-pipeline` (:8000), `jetson-control` (:8080),
`jetson-bt`, plus `jetson-sync.timer`. Units were reinstalled with absolute
paths — the old `~/gamma4_models` symlink they depended on is gone, so **do not
copy units manually; use `./jetson-ai.sh install-services`**.

## What happened recently (all pushed to main)

| Commit | What |
|---|---|
| `ad76a0d` | Security/robustness audit fixes: save_to path-traversal closed (recordings dir), optional `VOICE_API_TOKEN` bearer auth on 8000/8080, systemd path repair, `_fit_check` bash crash, status `llm_mode` key, playback errors no longer silent, `/speak` forwards `use_tools`, token files chmod 600 |
| `572cc0a` | README rewritten — covers full voice stack, badges, catchy layout |
| `e16fbc3` | Theme-aware SVG architecture diagram (`assets/`), QAT model catalogue: vision→`gemma4:e2b-it-qat` (4.3GB), new `vision-max`→`e4b-it-qat` (6.1GB), tiny→`qwen3.5:0.8b` |
| `0308c18` | Project skill `.claude/skills/jetson-stack/` |
| marty-skills `af76fa5` | Portable `tailscale-endpoints` skill (works in Claude Code + OpenClaw), cross-agent install docs |

## Open decisions (user input needed)

1. **External/public access** — recommended: set `VOICE_API_TOKEN` in `.voice_env`,
   then `tailscale funnel --bg 8080` (public HTTPS, no open ports). User was
   deciding. Never funnel 11434 (auth-less Ollama) or 8000 (cloud-key fallback).
2. **Next feature** — roadmap discussed, top pick: **STT + wake word**
   (faster-whisper small + openWakeWord) to close the voice loop; then
   conversation memory (sessions in /voice/chat); then draft-only write tools.

## Pending tasks (no decision needed, just do)

- `VOICE_API_TOKEN` still **unset** — auth code deployed but inactive.
- QAT models **not pulled yet**: `ollama pull gemma4:e2b-it-qat` (4.3GB), then
  `./test-models.sh gemma4:e2b-it-qat` to validate the ~9 tok/s estimate in
  README/catalogue (currently an estimate, not measured).
- Housekeeping: move `SESSION_SUMMARY.md`/`VOICE_PLAN.md` (stale, plan completed)
  to `docs/`; add CI (shellcheck + ruff); dedupe the three `_llm_with_tools_*`
  loops in voice-pipeline.py.
- Known minor issues (accepted, low priority): `/sync/status` returns 200 even
  when cache holds an `error` field; `known_senders` uses substring matching.

## Device facts

- Repo: `~/dev/projects/gamma4_models` → GitHub `kaiser-data/jetson-headless-inference` (names differ)
- Tailnet: Jetson = `ubuntu` / `ubuntu.tailf8ce6d.ts.net` / 100.78.34.27; VPS `ubuntu-4gb-nbg1-1` (Hetzner) available as reverse-proxy option
- Skills repo: `kaiser-data/marty-skills` (has forge.py validate/dashboard tooling — run both when adding skills there)
