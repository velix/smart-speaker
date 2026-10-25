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
uv sync                # creates .venv, installs deps + the package (editable)
uv run pytest          # smoke tests
uv run voice-assistant --help
```

`spidev` and `RPi-GPIO` are installed only on Linux (`sys_platform == 'linux'`),
so the project syncs and tests fine on macOS; LEDs just disable themselves.

## Deploy on the Pi

```bash
sudo apt update
sudo apt install -y python3-venv portaudio19-dev libatlas-base-dev ffmpeg
sudo mkdir -p /opt/voice-assistant && sudo chown $USER /opt/voice-assistant

# copy the project to /opt/voice-assistant, then:
cd /opt/voice-assistant
uv sync
```

Find your ALSA card (the Pi onboard jack is card 0, the HAT is usually 1 or 2):

```bash
arecord -l && aplay -l
arecord -D plughw:1,0 -f S16_LE -r 16000 -d 3 test.wav
aplay -D plughw:1,0 test.wav
```

Set that card number in `config.yaml` (`audio.card`), plus your OpenWebUI URL
and API key. LED test: `uv run python -m voice_assistant.leds test`.

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
