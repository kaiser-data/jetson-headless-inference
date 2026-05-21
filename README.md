# Jetson Orin 8GB — Headless AI API

Run local LLMs as a LAN API endpoint on a **NVIDIA Jetson Orin 8GB** (JetPack 6.x).  
Switch between headless AI mode and normal Ubuntu desktop with a single command.

**Hardware:** Jetson Orin Nano 8GB · LPDDR5 unified memory · CUDA 12.6 · JetPack 6.x  
**Backend:** [Ollama](https://ollama.com) — OpenAI-compatible REST API

---

## Quick Start

```bash
# First time only (sets up sudoers + ollama performance config)
./jetson-ai.sh setup

# Start headless AI API
./jetson-ai.sh start               # loads qwen3.5:4b (default)
./jetson-ai.sh start reasoning     # loads phi4-mini (task alias)

# Swap model without restarting
./jetson-ai.sh switch code         # → qwen3.5:4b
./jetson-ai.sh switch vision       # → gemma4:e2b

# Benchmark
./jetson-ai.sh bench

# Restore Ubuntu desktop
./jetson-ai.sh stop
```

---

## How It Works

```
┌─────────────────── Jetson Orin 8GB ─────────────────────┐
│  Unified RAM (8GB — CPU + GPU share the same pool)       │
│                                                           │
│  Normal mode  : GNOME (~1.5GB) + OS (~0.5GB) = 6GB free  │
│  Headless mode: OS (~0.5GB) only             = 7GB free   │
│                                                           │
│  ollama.service  ──► 0.0.0.0:11434                        │
│  OLLAMA_FLASH_ATTENTION=1 (−30% KV cache RAM)            │
│  OLLAMA_KV_CACHE_TYPE=q8_0 (−50% KV cache RAM)           │
│  nvpmodel MAXN_SUPER  (2× faster GPU clocks)             │
└───────────────────────────────────────────────────────────┘
              │  LAN (192.168.x.x:11434)
    ┌─────────┴──────────┐
    │  Pi / Laptop / App │  OpenAI SDK / curl / Python
    └────────────────────┘
```

**`start`** does all of this automatically:
1. Sets power mode → `MAXN_SUPER` (2× faster clocks)
2. Stops GNOME desktop (frees 1.5 GB RAM)
3. Pre-loads model into GPU memory
4. Warns if model is too large (CPU fallback = 100× slower)

**`stop`** reverses all of it cleanly.

---

## Model Guide

| Model | Size | tok/s* | Best for |
|---|---|---|---|
| `qwen3.5:0.8b` | 1.0 GB | ~35 | Ultra-fast, simple queries |
| `qwen2.5:3b` | 1.9 GB | ~22 | Fast multilingual |
| `llama3.2:3b` | 2.0 GB | ~20 | General chat |
| `phi4-mini` ★ | 2.5 GB | ~18 | Reasoning / math / agents |
| `qwen3.5:4b` ★ | 3.4 GB | ~13 | **Best all-round (default)** |
| `gemma3:latest` | 3.3 GB | ~12 | Quality general |
| `llama3.1:8b` | 4.9 GB | ~8 | High quality (headless only) |
| `gemma4:e2b` | 7.2 GB | ~5 | Vision / multimodal |

*Estimated headless + MAXN_SUPER. ★ = recommended

### Task Aliases

```bash
./jetson-ai.sh start default    # → qwen3.5:4b
./jetson-ai.sh switch fast      # → phi4-mini
./jetson-ai.sh switch reasoning # → phi4-mini
./jetson-ai.sh switch code      # → qwen3.5:4b
./jetson-ai.sh switch vision    # → gemma4:e2b
./jetson-ai.sh switch german    # → cas/discolm-mfto-german
./jetson-ai.sh switch tiny      # → qwen2.5:3b
./jetson-ai.sh switch quality   # → llama3.1:8b
```

---

## API Usage

The API is OpenAI-compatible — works as a drop-in for most tools.

### Python (OpenAI SDK)
```python
from openai import OpenAI

client = OpenAI(
    base_url="http://192.168.0.115:11434/v1",
    api_key="ollama"
)
response = client.chat.completions.create(
    model="qwen3.5:4b",
    messages=[{"role": "user", "content": "Explain edge AI in 2 sentences."}]
)
print(response.choices[0].message.content)
```

### Python (requests)
```python
import requests

r = requests.post("http://192.168.0.115:11434/api/generate", json={
    "model": "qwen3.5:4b",
    "prompt": "Your prompt here",
    "stream": False,
    "keep_alive": -1
})
print(r.json()["response"])
```

### curl
```bash
curl http://192.168.0.115:11434/api/generate \
  -d '{"model":"qwen3.5:4b","prompt":"Hello!","stream":false}'
```

### Streaming
```python
import requests, json

with requests.post("http://192.168.0.115:11434/api/generate",
    json={"model": "qwen3.5:4b", "prompt": "Count to 5", "stream": True},
    stream=True) as r:
    for line in r.iter_lines():
        if line:
            chunk = json.loads(line)
            print(chunk.get("response", ""), end="", flush=True)
```

---

## Boot Mode Selector

Get a 10-second menu on every SSH/TTY login:

```bash
echo 'source ~/gamma4_models/boot-choice.sh' >> ~/.bashrc
```

```
  ╔══════════════════════════════════════════════╗
  ║         JETSON ORIN — BOOT MODE              ║
  ╠══════════════════════════════════════════════╣
  ║  [1] Ubuntu Desktop              ← last  ║
  ║  [2] AI API  — qwen3.5:4b               ║
  ║  [3] AI API  — phi4-mini (fast)         ║
  ║  [4] AI API  — choose model             ║
  ║  [5] Shell only (no desktop/AI)         ║
  ╚══════════════════════════════════════════════╝

  Auto-starting [1] in 10s ... (press 1-5 to change)
```

Skip for one session: `JETSON_AI_SKIP_MENU=1 bash`

---

## Test Suite

```bash
./test-models.sh          # test all installed models
./test-models.sh phi4-mini  # test one model
```

Output:
```
  Model                               Result
  ──────────────────────────────────────────────────────────
  qwen2.5:3b                          ✓ PASS  22.1 tok/s  GPU:94%  1.9GB
  phi4-mini:latest                    ✓ PASS  18.3 tok/s  GPU:91%  2.5GB
  qwen3.5:4b                          ✓ PASS  13.1 tok/s  GPU:88%  3.4GB
  gemma4:e2b                          ⚠ WARN  slow (4.8 tok/s, GPU:42%)
  gemma4:e4b                          ✗ FAIL  CPU fallback (0.3 tok/s, GPU:0%)
```

---

## Speed Optimization Stack

All applied automatically by `setup` + `start`:

| Technique | Effect | Implementation |
|---|---|---|
| `MAXN_SUPER` power mode | ~2× faster GPU clocks | `nvpmodel -m 2` + `jetson_clocks` |
| Stop desktop | +1.5 GB free → models fit fully in GPU | `systemctl stop gdm3` |
| Flash Attention | −30–50% KV cache memory | `OLLAMA_FLASH_ATTENTION=1` |
| KV cache quantization | −50% KV memory vs default | `OLLAMA_KV_CACHE_TYPE=q8_0` |
| Model pinned in RAM | No reload delay between calls | `OLLAMA_KEEP_ALIVE=-1` |
| Systemd drop-in | Env vars survive restarts | `/etc/systemd/system/ollama.service.d/` |

### The Silent CPU Fallback Trap

When a model is too large for GPU memory, Ollama falls back to CPU **silently**:

```
GPU inference → 13–35 tok/s  ✓ usable
CPU inference →  0.3 tok/s   ✗ 100× slower, unusable
```

`bench` and `switch` automatically detect and warn about this. Fix: use a smaller model or run `start` to stop the desktop first.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `sudo: password required` | Run `./jetson-ai.sh setup` first |
| `< 2 tok/s` (CPU fallback) | Stop desktop: `./jetson-ai.sh stop` then `start` again |
| Model load timeout | Model too large — try `phi4-mini` or `qwen3.5:4b` |
| Desktop doesn't restore | `sudo systemctl start gdm3` |
| API unreachable from LAN | Check `OLLAMA_HOST=0.0.0.0` is set: `systemctl show ollama \| grep Env` |
| Port already in use | `sudo systemctl restart ollama` |

---

## Files

| File | Purpose |
|---|---|
| `jetson-ai.sh` | Main controller |
| `boot-choice.sh` | Login menu (desktop vs AI API) |
| `test-models.sh` | Model test suite |

State saved to `~/.local/share/jetson-ai/`

---

## Comparison vs Alternatives

| | **This setup** | **NanoLLM** | **llama.cpp direct** |
|---|---|---|---|
| Speed | Good (MAXN + flash attn) | Best (TensorRT) | ~10% faster than ollama |
| OpenAI API | ✓ | ✗ | needs wrapper |
| Model switching | 1 command | manual | manual |
| Desktop restore | automatic | manual | manual |
| Install complexity | Low | High (Docker+CUDA) | Medium |
| LAN API | ✓ | ✗ | needs extra server |

---

**Hardware:** NVIDIA Jetson Orin Nano 8GB · JetPack 6.4.7 · CUDA 12.6 · Ollama 0.21+
