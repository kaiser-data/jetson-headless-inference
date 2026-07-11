#!/usr/bin/env python3
"""Memory-pressure watchdog for the unified 8 GB pool (issue #1).

CUDA buffers can't be swap-backed, so after days of uptime the LLM can hit
`cudaMalloc failed` while `free` still shows GBs "available". This script
canary-checks that the LLM can actually allocate, and escalates recovery:

  1. restart jetson-pipeline   — drops a resident Whisper (~1 GB) back to lazy
  2. balloon                   — alloc+free RAM to force page-cache reclaim
                                 (NvMap won't trigger it on its own)
  3. restart ollama            — sudo -n, whitelisted by maint-setup.sh
  4. reboot                    — only if WATCHDOG_REBOOT=1 (.voice_env) AND
                                 the local time is 03:00-06:00

Modes:
  memory-watchdog.py                  timer run: canary, escalate only on failure
  memory-watchdog.py --reclaim        run steps 1-3 unconditionally, then verify
  memory-watchdog.py --reclaim --json machine-readable result (for the control API)

State for /status: ~/.local/share/jetson-ai/watchdog.json
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

OLLAMA_URL   = os.getenv("OLLAMA_URL", "http://localhost:11434")
CANARY_MODEL = os.getenv("WATCHDOG_CANARY_MODEL", "qwen3.5:0.8b")
STATE_FILE   = Path.home() / ".local/share/jetson-ai/watchdog.json"
OOM_MARKERS  = ("out of memory", "cudamalloc", "failed to allocate")


def _log(msg: str) -> None:
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [watchdog] {msg}", flush=True)


def _meminfo() -> dict:
    out = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, v = line.split(":", 1)
        out[k] = int(v.strip().split()[0]) // 1024   # MB
    return out


def _canary() -> tuple[bool, str]:
    """Try a 1-token generate. Returns (ok, detail)."""
    # prefer the already-loaded model — avoids an eviction just for the check
    model = CANARY_MODEL
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/ps", timeout=5) as r:
            loaded = json.load(r).get("models", [])
        if loaded:
            model = loaded[0]["name"]
    except Exception:
        pass

    body = json.dumps({"model": model, "prompt": "hi", "stream": False,
                       "options": {"num_predict": 1}}).encode()
    req = urllib.request.Request(f"{OLLAMA_URL}/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=240) as r:
            d = json.load(r)
        if d.get("error"):
            return False, f"{model}: {d['error'][:160]}"
        return True, model
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:160]
        return False, f"{model}: HTTP {e.code} {detail}"
    except Exception as e:
        return False, f"{model}: {type(e).__name__} {e}"


def step_restart_pipeline() -> bool:
    rc = subprocess.run(["systemctl", "--user", "restart", "jetson-pipeline"]).returncode
    time.sleep(8)
    return rc == 0


def step_balloon() -> bool:
    """Allocate+free RAM in chunks to force the kernel to drop page cache."""
    chunks, allocated = [], 0
    try:
        while allocated < 3000 and _meminfo().get("MemAvailable", 0) > 700:
            chunks.append(bytearray(100 * 1024 * 1024))
            allocated += 100
    except MemoryError:
        pass
    del chunks
    _log(f"balloon reclaimed via {allocated} MB allocation")
    return True


def step_restart_ollama() -> bool:
    rc = subprocess.run(["sudo", "-n", "/usr/bin/systemctl", "restart", "ollama"],
                        capture_output=True).returncode
    if rc != 0:
        _log("ollama restart denied — run: sudo ./maint-setup.sh")
        return False
    time.sleep(10)
    return True


def step_reboot() -> bool:
    if os.getenv("WATCHDOG_REBOOT", "0") != "1":
        _log("reboot skipped (WATCHDOG_REBOOT not set)")
        return False
    if not 3 <= datetime.now().hour < 6:
        _log("reboot skipped (outside 03:00-06:00 window)")
        return False
    _log("LAST RESORT: rebooting")
    subprocess.run(["sudo", "-n", "/usr/sbin/reboot"])
    return True


STEPS = [("restart_pipeline", step_restart_pipeline),
         ("balloon", step_balloon),
         ("restart_ollama", step_restart_ollama)]


def write_state(ok: bool, detail: str, escalations: list) -> dict:
    mem = _meminfo()
    state = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "llm_can_allocate": ok,
        "detail": detail,
        "escalations": escalations,
        "swap_used_mb": mem.get("SwapTotal", 0) - mem.get("SwapFree", 0),
        "mem_available_mb": mem.get("MemAvailable", 0),
    }
    if ok:
        state["last_ok_ts"] = state["ts"]
    else:
        try:
            state["last_ok_ts"] = json.loads(STATE_FILE.read_text()).get("last_ok_ts")
        except Exception:
            state["last_ok_ts"] = None
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")
    return state


def main() -> int:
    reclaim = "--reclaim" in sys.argv
    as_json = "--json" in sys.argv
    escalations = []

    if reclaim:
        for name, fn in STEPS:
            escalations.append({"step": name, "ok": fn()})
        ok, detail = _canary()
    else:
        ok, detail = _canary()
        if ok:
            _log(f"canary OK ({detail})")
        else:
            _log(f"canary FAILED: {detail}")
            is_oom = any(m in detail.lower() for m in OOM_MARKERS) or "HTTP 5" in detail
            if not is_oom:
                _log("not a memory failure — no escalation")
            else:
                for name, fn in STEPS:
                    escalations.append({"step": name, "ok": fn()})
                    ok, detail = _canary()
                    _log(f"after {name}: {'OK' if ok else 'still failing — ' + detail}")
                    if ok:
                        break
                if not ok:
                    escalations.append({"step": "reboot", "ok": step_reboot()})

    state = write_state(ok, detail, escalations)
    if as_json:
        print(json.dumps(state))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
