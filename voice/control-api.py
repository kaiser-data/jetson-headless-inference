#!/usr/bin/env python3
"""Jetson AI — Control & Status API on port 8080.

Manage the full AI stack remotely over Tailscale or LAN.

Endpoints:
  GET  /status                 → full system status (RAM, GPU, mode, BT, voices)
  POST /speak                  → send prompt, play on local speaker, return JSON
  POST /control/start          → start a mode (local / voice / api)
  POST /control/stop           → stop all services
  POST /control/switch         → swap LLM model while running
  GET  /control/voices         → list available voice models
  PUT  /control/output         → change audio output mode (stream/speaker/both)
  PUT  /control/sink           → change PulseAudio sink (audio routing)
  GET  /control/sinks          → list available PulseAudio sinks
  POST /bt/connect             → connect Bluetooth speaker
  POST /bt/disconnect          → disconnect Bluetooth speaker
  GET  /bt/status              → Bluetooth speaker state
  POST /sync                   → trigger calendar/email data sync
  GET  /sync/status            → show last sync timestamps from cache
  POST /power/suspend          → suspend to RAM (wake via WoL magic packet)
  POST /power/mode             → switch power profile (high = MAXN_SUPER, low = 15W)
  POST /power/headless         → stop/start the desktop (frees ~1.5 GB for models)
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

VOICE_DIR   = Path(__file__).parent
REPO_DIR    = VOICE_DIR.parent
JETSON_SH   = REPO_DIR / "jetson-ai.sh"
PIPELINE_URL = os.getenv("PIPELINE_URL",  "http://localhost:8000")
TTS_URL      = os.getenv("TTS_URL",       "http://localhost:5500")
OLLAMA_URL   = os.getenv("OLLAMA_URL",    "http://localhost:11434")
BT_SPEAKER_MAC  = os.getenv("BT_SPEAKER_MAC",  "88:88:11:07:10:5C")
BT_SPEAKER_NAME = os.getenv("BT_SPEAKER_NAME", "Boomcore P06")

# Optional bearer token — if set, all endpoints except /health require
# "Authorization: Bearer <token>". Leave empty for open LAN/Tailscale use.
API_TOKEN = os.getenv("VOICE_API_TOKEN", "")

app = FastAPI(title="Jetson AI Control", version="1.0.0")


@app.middleware("http")
async def _auth_middleware(request, call_next):
    if API_TOKEN and request.url.path != "/health":
        if request.headers.get("authorization") != f"Bearer {API_TOKEN}":
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


# ── System info helpers ───────────────────────────────────────────────────────

def _ram() -> dict:
    try:
        import re
        out = subprocess.check_output(["free", "-m"], text=True)
        m = re.search(r"Mem:\s+(\d+)\s+(\d+)\s+(\d+)", out)
        if m:
            total, used, free = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return {"total_mb": total, "used_mb": used, "free_mb": free,
                    "used_pct": round(used / total * 100)}
    except Exception:
        pass
    return {}


def _power_mode() -> str:
    try:
        out = subprocess.check_output(["nvpmodel", "-q"], text=True, stderr=subprocess.DEVNULL)
        for line in out.splitlines():
            if "NV Power Mode" in line:
                return line.split(":")[-1].strip()
    except Exception:
        pass
    return "unknown"


async def _ollama_status() -> dict:
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            ps = (await c.get(f"{OLLAMA_URL}/api/ps")).json()
            models = ps.get("models", [])
            if models:
                m = models[0]
                sv = m.get("size_vram", 0)
                s  = m.get("size", 1)
                gpu_pct = int(sv * 100 / s) if s else 0
                return {"running": True, "model": m.get("name"), "gpu_pct": gpu_pct}
            return {"running": True, "model": None, "gpu_pct": 0}
    except Exception:
        return {"running": False, "model": None, "gpu_pct": 0}


async def _pipeline_health() -> dict:
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            return (await c.get(f"{PIPELINE_URL}/health")).json()
    except Exception:
        return {"status": "offline"}


async def _tts_health() -> dict:
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            return (await c.get(f"{TTS_URL}/health")).json()
    except Exception:
        return {"status": "offline"}


def _bt_info() -> dict:
    try:
        out = subprocess.check_output(
            ["bluetoothctl", "info", BT_SPEAKER_MAC],
            text=True, stderr=subprocess.DEVNULL,
        )
        connected = "Connected: yes" in out
        return {"mac": BT_SPEAKER_MAC, "name": BT_SPEAKER_NAME, "connected": connected}
    except Exception:
        return {"mac": BT_SPEAKER_MAC, "name": BT_SPEAKER_NAME, "connected": False}


def _pulse_sinks() -> list[dict]:
    try:
        out = subprocess.check_output(["pactl", "list", "sinks", "short"], text=True)
        sinks = []
        for line in out.strip().splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                sinks.append({"id": parts[0], "name": parts[1],
                               "state": parts[4] if len(parts) > 4 else "?"})
        return sinks
    except Exception:
        return []


def _default_sink() -> str:
    try:
        return subprocess.check_output(
            ["pactl", "get-default-sink"], text=True
        ).strip()
    except Exception:
        return ""


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/status")
async def status():
    ollama, pipeline, tts, bt = await asyncio.gather(
        _ollama_status(),
        _pipeline_health(),
        _tts_health(),
        asyncio.get_event_loop().run_in_executor(None, _bt_info),
    )
    ram = await asyncio.get_event_loop().run_in_executor(None, _ram)
    return {
        "ram":      ram,
        "power":    _power_mode(),
        "llm":      ollama,
        "pipeline": pipeline,
        "tts":      tts,
        "bt":       bt,
        "audio": {
            "default_sink": _default_sink(),
            "sinks": _pulse_sinks(),
        },
    }


class SpeakRequest(BaseModel):
    prompt: str
    voice: str = "en"
    mode: Optional[str] = None          # llm mode override
    model: Optional[str] = None
    api_key: Optional[str] = None
    system: Optional[str] = None
    save_to: Optional[str] = None       # filename — pipeline saves under its recordings dir
    use_tools: bool = False             # enable calendar/email tool calling


@app.post("/speak")
async def speak(req: SpeakRequest):
    """Send a prompt — Jetson speaks it through local speakers, returns transcript."""
    body = {
        "prompt":    req.prompt,
        "voice":     req.voice,
        "output":    "speaker",
        "save_to":   req.save_to,
        "use_tools": req.use_tools,
    }
    if req.mode:    body["mode"]    = req.mode
    if req.model:   body["model"]   = req.model
    if req.api_key: body["api_key"] = req.api_key
    if req.system:  body["system"]  = req.system

    try:
        async with httpx.AsyncClient(timeout=180) as c:
            resp = await c.post(f"{PIPELINE_URL}/voice/chat", json=body)
            resp.raise_for_status()
            return resp.json()
    except httpx.ConnectError:
        raise HTTPException(503, "Voice pipeline offline — start with: ./jetson-ai.sh start voice")
    except Exception as e:
        raise HTTPException(500, str(e))


class StartRequest(BaseModel):
    mode: str = "voice"    # local | voice | api
    model: str = ""        # optional LLM model


@app.post("/control/start")
async def control_start(req: StartRequest, bg: BackgroundTasks):
    """Start an AI mode. Runs in background (takes 10–30s)."""
    cmd = ["bash", str(JETSON_SH), "start", req.mode]
    if req.model:
        cmd.append(req.model)
    bg.add_task(_run_bg, cmd, f"start {req.mode}")
    return {"status": "starting", "mode": req.mode, "model": req.model or "default"}


@app.post("/control/stop")
async def control_stop(bg: BackgroundTasks):
    bg.add_task(_run_bg, ["bash", str(JETSON_SH), "stop"], "stop")
    return {"status": "stopping"}


class SwitchRequest(BaseModel):
    model: str


@app.post("/control/switch")
async def control_switch(req: SwitchRequest):
    result = await _run_async(["bash", str(JETSON_SH), "switch", req.model])
    return {"status": "ok" if result == 0 else "error", "model": req.model}


@app.get("/control/voices")
async def control_voices():
    models_dir = VOICE_DIR / "models"
    voices = []
    for onnx in sorted(models_dir.glob("*.onnx")):
        size_mb = round(onnx.stat().st_size / 1024 / 1024, 1)
        voices.append({"name": onnx.stem, "size_mb": size_mb})
    return {"voices": voices}


class OutputRequest(BaseModel):
    output: str   # stream | speaker | both


@app.put("/control/output")
async def set_output(req: OutputRequest):
    """Change default audio output mode for this session (restarts pipeline)."""
    if req.output not in ("stream", "speaker", "both"):
        raise HTTPException(400, "output must be stream | speaker | both")
    # Write to env file that pipeline reads on restart
    env_file = REPO_DIR / ".voice_env"
    lines = []
    if env_file.exists():
        lines = [l for l in env_file.read_text().splitlines() if not l.startswith("VOICE_OUTPUT=")]
    lines.append(f"VOICE_OUTPUT={req.output}")
    env_file.write_text("\n".join(lines) + "\n")
    return {"status": "ok", "output": req.output,
            "note": "restart pipeline to apply: ./jetson-ai.sh tts stop && ./jetson-ai.sh tts start"}


class SinkRequest(BaseModel):
    sink: str   # sink name from /control/sinks


@app.put("/control/sink")
async def set_sink(req: SinkRequest):
    """Route audio to a different output device."""
    rc = await _run_async(["pactl", "set-default-sink", req.sink])
    if rc != 0:
        raise HTTPException(400, f"Failed to set sink '{req.sink}' — check /control/sinks")
    return {"status": "ok", "sink": req.sink}


@app.get("/control/sinks")
async def get_sinks():
    return {"sinks": _pulse_sinks(), "default": _default_sink()}


@app.post("/bt/connect")
async def bt_connect():
    rc = await _run_async(["bluetoothctl", "connect", BT_SPEAKER_MAC])
    if rc == 0:
        # Auto-set as default sink
        await asyncio.sleep(2)
        for sink in _pulse_sinks():
            if BT_SPEAKER_MAC.replace(":", "_").lower() in sink["name"].lower():
                await _run_async(["pactl", "set-default-sink", sink["name"]])
                return {"status": "connected", "sink": sink["name"]}
        return {"status": "connected", "sink": "not_found_in_pulse"}
    return {"status": "failed"}


@app.post("/bt/disconnect")
async def bt_disconnect():
    rc = await _run_async(["bluetoothctl", "disconnect", BT_SPEAKER_MAC])
    return {"status": "ok" if rc == 0 else "failed"}


@app.get("/bt/status")
async def bt_status():
    return await asyncio.get_event_loop().run_in_executor(None, _bt_info)


@app.get("/health")
def health():
    return {"status": "ok"}


# ── Data sync ─────────────────────────────────────────────────────────────────

CACHE_DIR    = Path.home() / ".local/share/jetson-ai/cache"
DATA_SYNC_PY = VOICE_DIR / "data-sync.py"


class SyncRequest(BaseModel):
    target: str = "both"   # both | calendar | email


@app.post("/sync")
async def sync_data(req: SyncRequest, bg: BackgroundTasks):
    """Trigger a data sync. Runs in background; check /sync/status for results."""
    if req.target not in ("both", "calendar", "email"):
        raise HTTPException(400, "target must be both | calendar | email")
    cmd = [sys.executable, str(DATA_SYNC_PY), req.target]
    bg.add_task(_run_bg, cmd, f"data-sync {req.target}")
    return {"status": "syncing", "target": req.target}


@app.get("/sync/status")
def sync_status():
    """Return last sync time and event/message counts from cache."""
    result = {}
    for name in ("calendar", "email"):
        path = CACHE_DIR / f"{name}.json"
        if not path.exists():
            result[name] = {"status": "no_cache"}
            continue
        try:
            data = json.loads(path.read_text())
            entry: dict = {
                "synced_at": data.get("synced_at"),
                "status":    "error" if data.get("error") else "ok",
            }
            if name == "calendar":
                entry["event_count"] = len(data.get("events", []))
            else:
                msgs = data.get("messages", [])
                entry["message_count"] = len(msgs)
                entry["unread_count"]  = sum(1 for m in msgs if m.get("is_unread"))
            if data.get("error"):
                entry["error"] = data["error"]
            result[name] = entry
        except Exception as e:
            result[name] = {"status": "parse_error", "error": str(e)}
    return result


# ── Power ─────────────────────────────────────────────────────────────────────

WOL_IFACE = os.getenv("WOL_IFACE", "eno1")


class SuspendRequest(BaseModel):
    delay_s: int = 3   # grace period so the HTTP response gets out first


async def _suspend_after(delay: int):
    log.info("Suspending in %ds — wake with a WoL magic packet on %s", delay, WOL_IFACE)
    await asyncio.sleep(delay)
    # Requires the NOPASSWD sudoers rule installed by wol-setup.sh
    proc = await asyncio.create_subprocess_exec(
        "sudo", "-n", "/usr/bin/systemctl", "suspend",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.error("Suspend failed (rc=%d): %s — run: sudo ./wol-setup.sh",
                  proc.returncode, stderr.decode().strip()[-300:])


# nvpmodel numbers per /etc/nvpmodel.conf: 0=15W (default), 2=MAXN_SUPER
POWER_PROFILES = {"high": "2", "low": "0"}


class PowerModeRequest(BaseModel):
    mode: str   # high | low


@app.post("/power/mode")
async def power_mode(req: PowerModeRequest):
    """Switch power profile. Needs the sudoers rules from wol-setup.sh.

    Note: resume from suspend already lands in high mode automatically
    (jetson-resume-perf.service) — this is for manual overrides.
    """
    profile = POWER_PROFILES.get(req.mode)
    if profile is None:
        raise HTTPException(400, "mode must be high | low")
    rc = await _run_async(["sudo", "-n", "/usr/sbin/nvpmodel", "-m", profile])
    if rc != 0:
        raise HTTPException(500, "nvpmodel failed — sudoers rule missing? run: sudo ./wol-setup.sh")
    if req.mode == "high":
        await _run_async(["sudo", "-n", "/usr/bin/jetson_clocks"])
    return {"status": "ok", "mode": req.mode, "power": _power_mode()}


class HeadlessRequest(BaseModel):
    headless: bool   # true = stop desktop, false = start it


@app.post("/power/headless")
async def power_headless(req: HeadlessRequest):
    """Stop/start GNOME remotely. Stopping frees ~1.5 GB of the unified 8 GB
    — required for 4 GB-class models. Needs sudoers from wol-setup.sh."""
    action = "stop" if req.headless else "start"
    for dm in ("gdm3", "gdm"):
        rc = await _run_async(["sudo", "-n", "/usr/bin/systemctl", action, dm])
        if rc == 0:
            ram = await asyncio.get_event_loop().run_in_executor(None, _ram)
            return {"status": "ok", "headless": req.headless, "dm": dm,
                    "free_mb": ram.get("free_mb")}
    raise HTTPException(500, f"could not {action} desktop — sudoers rule missing? run: sudo ./wol-setup.sh")


@app.post("/power/suspend")
async def power_suspend(req: SuspendRequest, bg: BackgroundTasks):
    """Suspend to RAM after delay_s. Wake by sending a magic packet to wake_mac."""
    delay = max(1, min(req.delay_s, 300))
    try:
        mac = Path(f"/sys/class/net/{WOL_IFACE}/address").read_text().strip()
    except Exception:
        mac = "unknown"
    bg.add_task(_suspend_after, delay)
    return {"status": "suspending", "delay_s": delay, "wake_mac": mac,
            "wake_iface": WOL_IFACE}


# ── Subprocess helpers ────────────────────────────────────────────────────────

async def _run_async(cmd: list[str]) -> int:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    return proc.returncode


async def _run_bg(cmd: list[str], label: str):
    log.info("BG task: %s", label)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    log.info("BG task done: %s  rc=%d", label, proc.returncode)
    if stderr:
        log.warning("BG stderr: %s", stderr.decode()[-500:])


if __name__ == "__main__":
    log.info("Control API starting on port 8080")
    log.info("  BT speaker: %s  (%s)", BT_SPEAKER_NAME, BT_SPEAKER_MAC)
    log.info("  Pipeline:   %s", PIPELINE_URL)
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")
