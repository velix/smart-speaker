# voice-assistant

Smart speaker daemon for **Raspberry Pi 3 Model B + ReSpeaker 2-Mic HAT (KS0314)**,
running 64-bit Raspberry Pi OS (trixie).
Audio in/out via the HAT (mic capture, 3.5 mm jack to a powered speaker).

The Pi stays thin. Wake word, VAD capture, and playback run locally.
Everything intelligent runs on the homelab: STT and TTS on Ollama/Kokoro
(GPU box), reasoning/personality/knowledge behind OpenWebUI.

Pipeline: **openWakeWord (local) → webrtcvad → STT → LLM → TTS → playback**, with
barge-in (wake word or the HAT button stops playback) and a health endpoint for
uptime-kuma.

Current engine choices (all swappable in `config.yaml`, A/B validated
2026-11-01/02 — see `KNOWLEDGE.md` for the measurements):

| Stage | Engine | Where | Measured |
|---|---|---|---|
| Wake word | openWakeWord `hey_jarvis` (onnx) | Pi | RTF ~0.5 (real-time) |
| STT | Ollama `gemma4:e4b` via `input_audio` chat + vocabulary `hints` | GPU box | ~0.3–0.6 s |
| LLM | OpenWebUI workspace model `jarvis` (server-side chat) | OpenWebUI | ~1–3 s (tool turns) |
| TTS | Kokoro-FastAPI (`af_heart`), clause-pipelined | GPU box (GPU) | first audio ~0.16 s |
| TTS fallback | piper (maintained `piper-tts` wheel) | Pi (local) | ~1.7 s, automatic |

End-to-end: roughly **1.5–2 s** from end of speech to first audio.

Managed with [uv](https://docs.astral.sh/uv/).

## Layout

```
pyproject.toml            # deps, console script, build config
config.yaml               # runtime configuration (all engine switches)
voice-assistant.service   # systemd unit
src/voice_assistant/
    assistant.py          # main daemon: wake/button → VAD → STT → LLM → TTS → playback
    audio_io.py           # mic capture (stereo → mono), playback, PCM/WAV/MP3 conversion
    vad.py                # webrtcvad end-of-speech detection
    wake_word.py          # openWakeWord + GPIO17 button trigger
    ollama_stt.py         # STT via Ollama chat models (gemma4) + reply compression
    openwebui_client.py   # OpenWebUI client: server-side chat (Path A), STT/TTS fallbacks
    openai_tts.py         # TTS via OpenAI-compatible /v1/audio/speech (Kokoro-FastAPI)
    piper_engine.py       # local piper TTS (fallback engine), sentence-streamed
    leds.py               # APA102 status LEDs (optional)
    health.py             # GET /health JSON endpoint
    interfaces/           # vendored seeed mic_hat apa102.py + pixels.py
docs/                     # runbooks + experiment notes (see below)
scripts/                  # pi-update.sh, setup-pi-tts.sh, STT/TTS A/B harnesses
tests/                    # unit tests (no hardware needed)
```

## Development (any machine)

```bash
uv sync                          # creates .venv, installs deps + the package (editable)
uv run pytest                    # unit tests
uv run voice-assistant --help
```

`spidev`, `RPi-GPIO`, `lgpio`, `openwakeword`, and `piper-tts` install only on
Linux (`sys_platform`/`platform_machine` markers). The project therefore syncs
and tests fine on macOS. LEDs and the wake-word model disable themselves there.
On the aarch64 Pi, `uv sync` installs everything, wake word included.

## Deploy on the Pi (git pull workflow)

The Pi runs a checkout of this repo at `/opt/voice-assistant` and pulls from
GitHub. No manual file copying is needed. First-time setup:
**`docs/pi-setup-from-scratch.md`** is the validated, reproducible runbook
(OS flash, `wm8960-soundcard` overlay, mixer gates, deploy, systemd).

After a push from the dev machine, the Pi is updated with:

```bash
./scripts/pi-update.sh        # git pull --ff-only + uv sync (+ service restart if running)
```

### Secrets (this repo is public — no secrets in it)

`config.yaml` ships with empty `openwebui.base_url` / `api_key`. The real
values are provided on the Pi in **either** (or both) of:

1. **`/opt/voice-assistant/config.local.yaml`** (gitignored, overlays config.yaml)
2. **`/etc/voice-assistant.env`** (loaded by the systemd unit):
   `OPENWEBUI_BASE_URL=...`, `OPENWEBUI_API_KEY=...`

Env vars win over the local file. The local file wins over config.yaml.

## STT

Selected by `stt.engine` in config.yaml:

- `"ollama"` (**current**) — any audio-capable Ollama chat model via the
  OpenAI-compatible `/v1/chat/completions` with an `input_audio` part
  (`gemma4:e4b`: warm ~0.3 s, better accuracy than whisper-base in the A/B
  test). Because the transcriber is an LLM, `stt.ollama.hints` biases it
  toward the configured proper nouns (hostnames, projects, names) that
  generic ASR mangles. `keep_alive` keeps the model in VRAM between turns.
  The same warm model doubles as the **reply compressor** for over-long LLM
  replies (`server.max_reply_words`). The compression call uses Ollama's
  native `/api/chat` with `think: false`. The OpenAI-compatible endpoint
  ignores it. Gemma4 otherwise burns 200–600 thinking tokens.
- `"openwebui"` — server-side Whisper (fallback — ~1.9 s with `base`).

## LLM (OpenWebUI server-side chat)

Replies come from an OpenWebUI **workspace model** (e.g. `jarvis`). The model
owns the personality and the Knowledge-collection RAG. Personality and RAG are
edited in the OpenWebUI UI. No redeploy is needed. The client uses the
documented "server-side tool calling" path (`chat_id` + message `id` +
`stream: true` + `session_id`). **Without these, OpenWebUI runs no tools
server-side and knowledge silently never triggers.**

Every voice turn is appended to one persistent sidebar chat (id kept in
`.speaker-chat.json`, gitignored). The chat is a readable log of what jarvis
heard and answered.

OpenTerminal is supported but **opt-in** (`openwebui.terminal_id` in
config.yaml). API callers must send `terminal_id` explicitly. The setting is
left unset unless voice-triggered shell execution is accepted.

Workspace tools / MCP servers attached to the model are **not** applied to API
requests automatically. The browser UI sends the model's attached tool ids in
the request. Raw API calls must include `tool_ids` themselves. The client
defaults to `openwebui.tool_ids: auto`. This mirrors the UI by reading the
model's `meta.toolIds` (`GET /api/v1/models/model?id=...`). An explicit list
can be set to override (ids from `GET /api/v1/tools/`, MCP servers as
`server:mcp:<id>`). `[]` sends none.

## TTS

Selected by `tts.engine` in config.yaml:

- `"openai"` (**current**) — any OpenAI-compatible `/v1/audio/speech`
  endpoint. Today this is Kokoro-FastAPI on the GPU box (deploy notes:
  `docs/kokoro-tts-deploy.md`). The endpoint synthesizes a whole request
  before streaming. The client therefore pipelines **sentence by sentence**
  on a feeder thread. Playback starts on the first sentence while the rest is
  still generated. Network chunks split at arbitrary byte boundaries. Odd
  bytes are carried so playback always gets whole int16 samples.
- `"piper"` — local on the Pi. Uses the maintained `piper-tts` (OHF piper1-gpl)
  wheel. Streams one chunk per sentence. The voice is installed by
  `./scripts/setup-pi-tts.sh`.
- `"openwebui"` — server-side via OpenWebUI. Requires a working TTS engine
  there. See `docs/tts-notes.md` for why the built-in one is broken.

If the primary engine fails, the daemon falls back to `tts.fallback`
(default: `piper`). The speaker keeps talking when the GPU box is down.

## Health endpoint (uptime-kuma)

`GET http://<pi>:8310/health` when `health.enabled` is true. **200** while both
the mic capture stream and the playback stream are active, **503** otherwise.
Example response:

```json
{
  "status": "ok",
  "uptime_s": 3600,
  "capture_mic": "ready",
  "output_speaker": "ready",
  "trigger": {"wake_word": true, "button": true},
  "tts_engine": "openai",
  "stt_engine": "ollama",
  "turns": 12
}
```

In uptime-kuma, an **HTTP(s)** monitor is added for
`http://pi-speaker.local:8310/health`.
Note: "output_speaker" reflects the playback stream state on the HAT's jack.
The WM8960 does not expose physical plug detection. A disconnected cable
downstream of the jack cannot be detected.

## Bring-up phases (fresh hardware)

The full runbook is `docs/pi-setup-from-scratch.md`. The short version:

1. **Audio loopback** — `arecord`/`aplay` through the HAT (after the overlay +
   mixer gates. See the runbook). The input boost switches default to OFF.
   The mics record silence without them.
2. **API smoke test** — `curl -H "Authorization: Bearer $KEY" .../api/models`.
3. **Capture + VAD** — `uv run voice-assistant --phase 3` is run. The HAT
   button is pressed. Speech is captured. `/tmp/utterance.wav` playback is
   verified.
4. **Full pipeline** — `uv run voice-assistant` is run. The button trigger is
   used first (`wake_word.enabled: false` is set).
5. **Wake word** — `wake_word.enabled: true` is set. The model `hey_jarvis`
   (bundled) is used. `threshold` is raised if false triggers occur.
6. **systemd** — `sudo cp voice-assistant.service /etc/systemd/system/ && sudo
   systemctl enable --now voice-assistant`.

## Gotchas

- sounddevice matches **PortAudio device names** (`hw:N,0`), not ALSA strings
  (`plughw:N,0`). `audio.card` is set to the HAT's card number.
- PortAudio caches its device list at import time and **drops busy devices
  entirely**. `arecord`/`aplay` is stopped before the daemon starts.
- RPi.GPIO edge detection is broken on 64-bit kernels. The daemon uses
  `lgpio` via gpiozero.
- If OpenWebUI knowledge/tools seem dead from the API, the server-side chat
  path is not in use (see "LLM" above — `session_id` is the switch).
- Process management over SSH: `pkill -f` patterns match the SSH session's own
  command line. `systemctl` is used instead (see AGENTS.md).

## Docs

- `docs/pi-setup-from-scratch.md` — full fresh-Pi runbook (overlay, mixer, deploy)
- `docs/migration-64bit-checklist.md` — the 32→64-bit migration, as executed
- `docs/kokoro-tts-deploy.md` — Kokoro-FastAPI GPU deployment (compose + gotchas)
- `docs/tts-notes.md` — why OpenWebUI/Mistral TTS didn't work. Engine survey
- `KNOWLEDGE.md` — append-only engineering log: measurements, A/B results, root causes
