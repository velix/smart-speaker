# Kokoro-FastAPI deployment — gpu-machine (TTS "B" engine)

Kokoro-FastAPI (remsky/Kokoro-FastAPI) is the proposed GPU TTS server: a
single-purpose container, OpenAI-compatible `/v1/audio/speech`, streaming
responses, actively maintained. This replaces the earlier Ollama/Phonon idea
(Phonon is macOS/MLX-only as of Ollama 0.40.12 — verified at the API level).

## Deploy on gpu-machine (192.168.2.139)

`~/stacks/kokoro/compose.yml`:

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
  --output /tmp/test.wav && aplay /tmp/test.wav
```

Notes:
- Requires NVIDIA container runtime — gpu-machine has
  nvidia-container-toolkit installed but **container GPU access was still
  unverified** (homelab machines.md). If the container fails to see the GPU,
  check `docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi`.
- Voices: `af_heart` (default), `af_bella`, `af_sarah`, `am_adam`,
  `bm_george`, ... — full list via `/v1/audio/voices`.
- LAN-only: no auth by default; do not port-forward it.

## After it is up

On the Pi (or via the agent), run the A/B harness:

```bash
uv run python scripts/tts-ab-test.py synth    # latency + WAVs for both engines
uv run python scripts/tts-ab-test.py play     # listen to A/B pairs on the speaker
```

If B wins (expected: first chunk < 0.5 s vs piper's ~1.7 s, and noticeably
better voice), flip production in config.yaml:

```yaml
tts:
  engine: "openai"    # was "piper"
  fallback: "piper"   # automatic fallback if the GPU server is down
```
