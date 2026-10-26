# voice-assistant

Smart speaker daemon for **Raspberry Pi 3 Model B + ReSpeaker 2-Mic HAT (KS0314)**.
Audio in/out via the HAT (mic capture, 3.5 mm jack to a powered speaker).
Backend: remote OpenWebUI (STT → Ollama → TTS).

Managed with [uv](https://docs.astral.sh/uv/).

## Layout

```
pyproject.toml            # deps, console script, build config
config.yaml               # runtime configuration (edit on the Pi)
voice-assistant.service   # systemd unit (install after phases 1–5 pass)
src/voice_assistant/
    assistant.py          # main daemon: wake/button → VAD → STT → LLM → TTS → playback
    audio_io.py           # mic capture (stereo → mono) and playback via sounddevice
    vad.py                # webrtcvad end-of-speech detection
    wake_word.py          # openWakeWord + GPIO17 button trigger
    openwebui_client.py   # OpenWebUI HTTP client (STT, chat, TTS)
    leds.py               # status LEDs (optional)
    interfaces/           # vendored seeed mic_hat apa102.py + pixels.py
tests/                    # smoke tests (no hardware needed)
```

## Development (any machine)

```bash
uv sync                          # creates .venv, installs deps + the package (editable)
uv run pytest                    # smoke tests
uv run voice-assistant --help
uv sync --extra wakeword         # also install openwakeword (Mac/dev only — see note)
```

`spidev` and `RPi-GPIO` are installed only on Linux (`sys_platform == 'linux'`),
so the project syncs and tests fine on macOS; LEDs just disable themselves.

**Wake-word note:** `openwakeword` needs `onnxruntime`, which has **no 32-bit
armv7 wheels** — so it cannot install on the Pi 3B (Raspbian 11, 32-bit).
It lives in the optional `wakeword` extra. On the Pi the daemon logs a clear
error and runs button-triggered; a Pi-compatible wake-word engine
(Porcupine or tflite) is a follow-up.

## Deploy on the Pi (git pull workflow)

The Pi runs a checkout of this repo at `/opt/voice-assistant` and pulls from
GitHub — no manual file copying.

First-time setup on the Pi:

```bash
sudo apt update
sudo apt install -y gcc python3-dev libffi-dev pkg-config libportaudio2 ffmpeg
sudo mkdir -p /opt/voice-assistant && sudo chown $USER /opt/voice-assistant
git clone https://github.com/velix/smart-speaker.git /opt/voice-assistant
cd /opt/voice-assistant
uv sync
```

After pushing from your dev machine, update the Pi with:

```bash
./scripts/pi-update.sh        # git pull --ff-only + uv sync (+ service restart if running)
```

### Secrets (this repo is public — no secrets in it)

`config.yaml` ships with empty `openwebui.base_url` / `api_key`. Provide the
real values on the Pi in **either** (or both) of:

1. **`/opt/voice-assistant/config.local.yaml`** (gitignored, overlays config.yaml):

   ```yaml
   openwebui:
     base_url: "http://192.168.2.x:3000"
     api_key: "sk-..."
   ```

2. **`/etc/voice-assistant.env`** (loaded by the systemd unit):

   ```
   OPENWEBUI_BASE_URL=http://192.168.2.x:3000
   OPENWEBUI_API_KEY=sk-...
   ```

Env vars win over the local file; the local file wins over config.yaml.

> Note: `uv` installs its own Python 3.11 on the Pi (system Python 3.9 is too
> old). `openwakeword` is NOT installed on the Pi — onnxruntime has no 32-bit
> armv7 wheels; the daemon runs button-triggered until a Pi-compatible
> wake-word engine is added.

Find your ALSA card (the Pi onboard jack is card 0, the HAT is usually 1 or 2):

```bash
arecord -l && aplay -l
arecord -D plughw:1,0 -f S16_LE -r 16000 -d 3 test.wav
aplay -D plughw:1,0 test.wav
```

Set that card number in `config.yaml` (`audio.card`), plus your OpenWebUI URL
and API key. LED test: `uv run python -m voice_assistant.leds test`.

## TTS

The daemon supports two TTS engines, selected by `tts.engine` in config.yaml:

- `"piper"` — **local, on the Pi** (default): fast, offline, no external API.
  Uses the maintained **piper-tts** wheel (OHF piper1-gpl, aarch64) via its
  Python API — streaming one chunk per sentence, model prewarmed at daemon
  start. Voice model installed by `./scripts/setup-pi-tts.sh` (the rhasspy
  binary it also downloads is only a standalone test tool now).
- `"openwebui"` — server-side via OpenWebUI `/api/v1/audio/speech`.

If the primary engine fails, the daemon automatically falls back to the
other one (when available). For why the OpenWebUI built-in/Mistral TTS
options were problematic, see `docs/tts-notes.md`.

## Health endpoint (uptime-kuma)

The daemon serves `GET http://<pi>:8310/health` when `health.enabled` is true
in config.yaml. It returns **200** while both the mic capture stream and the
playback stream are active, **503** otherwise. Example response:

```json
{
  "status": "ok",
  "uptime_s": 3600,
  "capture_mic": "ready",
  "output_speaker": "ready",
  "trigger": {"wake_word": false, "button": true},
  "tts_engine": "piper",
  "turns": 12
}
```

In uptime-kuma add an **HTTP(s)** monitor for
`http://192.168.2.145:8310/health`. Note: "output_speaker" reflects the
playback stream state on the HAT's jack — the WM8960 does not expose
physical plug detection, so a disconnected cable downstream of the jack
cannot be detected.

## Bring-up phases

Run each phase's test before moving to the next:

1. **Audio loopback** — the `arecord`/`aplay` test above through the speaker.
2. **API smoke test** — `curl -s -H "Authorization: Bearer $KEY" http://YOUR_SERVER:3000/api/models | head`
3. **Capture + VAD** — `uv run voice-assistant --phase 3`: press the HAT button,
   speak; check `/tmp/utterance.wav` plays back.
4. **Full pipeline** — `uv run voice-assistant` (button-triggered first: set
   `wake_word.enabled: false`).
5. **Wake word** — set `wake_word.enabled: true`, model `hey_jarvis` (bundled).
   Raise `threshold` if it false-triggers.
6. **systemd** — `sudo cp voice-assistant.service /etc/systemd/system/ && sudo systemctl enable --now voice-assistant`
   (only after phases 1–5 pass).

## Gotchas

- TTS output must be **16 kHz**; if your OpenWebUI TTS returns 22.05/24 kHz,
  resample or let the MP3/ffplay path handle it.
- If ALSA reports "device busy", stop any `arecord`/`aplay` first.
- The power undervoltage fix is still outstanding — do it before enabling
  systemd autostart, or boot-throttle may delay startup.
