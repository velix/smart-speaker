# Kokoro-FastAPI deployment — gpu-machine (TTS engine, deployed)

Kokoro-FastAPI (remsky/Kokoro-FastAPI) is the GPU TTS server: a
single-purpose container, OpenAI-compatible `/v1/audio/speech`, streaming
responses, actively maintained. It replaced the earlier Ollama/Phonon idea
(Phonon is macOS/MLX-only as of Ollama 0.40.12 — verified at the API level)
and won the A/B against local piper (better voice, first audio ~160 ms vs
~1.7 s — measurements in KNOWLEDGE.md, 2026-11-01).

**Status: deployed and in production** as a compose stack on gpu-machine
(192.168.2.139:8880), selected by `tts.engine: "openai"` in config.yaml
(piper is the automatic fallback).

## Deploy on gpu-machine (192.168.2.139)

`/mnt/cifs/appdata/stacks/kokoro/compose.yml` (actual working config):

```yaml
services:
  kokoro-tts:
    image: ghcr.io/remsky/kokoro-fastapi-gpu:latest
    container_name: kokoro-tts
    ports:
      - "8880:8880"
    environment:
      - NVIDIA_VISIBLE_DEVICES=all
    volumes:
      - ./data:/app/api/src/data
    restart: unless-stopped
    runtime: nvidia          # REQUIRED for GPU — see gotcha below
```

```bash
mkdir -p ~/stacks/kokoro/data && cd ~/stacks/kokoro
# paste the compose.yml above, then:
docker compose up -d
# verify (first pull + model download takes a few minutes):
curl http://localhost:8880/v1/audio/voices
curl -s http://localhost:8880/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"model":"kokoro","voice":"af_heart","input":"Hello from the GPU."}' \
  --output /tmp/test.wav
```

Verify the model loaded on CUDA: `docker compose logs kokoro-tts | grep -i
"Initializing Kokoro"` — expect "on cuda". If it says cpu, see the gotcha
below.

## Gotchas found during deployment (2026-11-01)

- **`NVIDIA_VISIBLE_DEVICES=all` alone does NOT give the container a GPU.**
  Without `runtime: nvidia` (or a `deploy.resources.devices` equivalent),
  the container starts fine but synthesizes on CPU — slower, and the GPU
  sits idle. `nvidia-smi` inside a plain `cuda` test container can work while
  the compose stack still lacks `runtime: nvidia`; check the logs, not just
  the host.
- **The endpoint synthesizes the whole request before streaming the body.**
  Long replies would block first audio for the full synthesis time — the
  speaker client pipelines sentence by sentence instead (see
  `src/voice_assistant/openai_tts.py`).
- The WAV response streams in network chunks that split at arbitrary byte
  boundaries; the client carries odd bytes between reads (whole int16
  samples only) and skips the RIFF header by sentinel (`data` chunk).
- Voices: `af_heart` (in use), `af_bella`, `af_sarah`, `am_adam`,
  `bm_george`, ... — full list via `/v1/audio/voices`.
- LAN-only: no auth by default; do not port-forward it.

## Client side (the Pi)

config.yaml:

```yaml
tts:
  engine: "openai"
  fallback: "piper"        # automatic fallback if the GPU server is down
  openai:
    base_url: "http://192.168.2.139:8880/v1"
    model: "kokoro"
    voice: "af_heart"
    sample_rate: 24000
```

The A/B harness (`scripts/tts-ab-test.py`) replays identical text through
both engines for latency comparison and listening tests.
