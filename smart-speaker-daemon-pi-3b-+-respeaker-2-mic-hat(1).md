# Smart speaker daemon — Phase 1 build

Target: **Raspberry Pi 3 Model B + ReSpeaker 2-Mic HAT (KS0314)**, audio out via the HAT's 3.5 mm jack to your powered speaker. Backend: remote OpenWebUI (STT → Ollama → TTS).

Bring-up order: **1) audio loopback → 2) API smoke test → 3) button + VAD capture → 4) full pipeline → 5) wake word → 6) systemd**. Each phase has a test you run before moving on.

## Install on the Pi

```bash
sudo apt update
sudo apt install -y python3-venv python3-numpy portaudio19-dev libatlas-base-dev ffmpeg
sudo mkdir -p /opt/voice-assistant && sudo chown $USER /opt/voice-assistant
cd /opt/voice-assistant
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Find your ALSA card (usually card 1 or 2 — the Pi's onboard jack is card 0):

```bash
arecord -l && aplay -l
# quick speaker test — you should hear your voice through the powered speaker
arecord -D plughw:1,0 -f S16_LE -r 16000 -d 3 test.wav
aplay -D plughw:1,0 test.wav
```

Put that card number in `config.yaml` (`card: 1`). LED test: `python leds.py test`.

## File: `config.yaml`

```yaml
openwebui:
  base_url: "http://192.168.1.50:3000"   # your OpenWebUI instance
  api_key: "REPLACE_WITH_OPENWEBUI_API_KEY"
  model: "llama3.2:3b"
  language: "en"

audio:
  card: 1            # arecord -l → 'card N: seeed2micvoicec'
  rate: 16000
  channels: 2        # WM8960 captures stereo; we slice channel 0
  playback_volume: 0.9

wake_word:
  enabled: true
  engine: "openwakeword"
  model: "hey_jarvis"
  threshold: 0.5

vad:
  silence_ms: 600
  max_utterance_s: 12
  min_speech_ms: 200

button:
  enabled: true      # GPIO17 push-to-talk fallback / barge-in

history:
  max_turns: 10

server:
  system_prompt: "You are a voice assistant on a smart speaker. Reply in 1-2 short conversational sentences."
```

## File: `requirements.txt`

```text
PyYAML>=6.0
httpx>=0.27
numpy>=1.24
sounddevice>=0.4
webrtcvad-wheels>=2.0.14
openwakeword>=0.6.0
gpiozero>=2.0
spidev>=3.6
rpi.gpio>=0.7
```

(`webrtcvad-wheels` avoids the gcc build pain of the original `webrtcvad` on Pi.)

## File: `audio_io.py`

```python
"""Mic capture (ReSpeaker WM8960, stereo → mono slice) and playback via sounddevice."""
import io
import wave
import numpy as np
import sounddevice as sd


class AudioIO:
    def __init__(self, cfg):
        self.rate = cfg["rate"]
        self.capture_channels = cfg["channels"]
        dev = f"plughw:{cfg['card']},0"
        self.capture_stream = sd.InputStream(
            samplerate=self.rate, channels=self.capture_channels,
            dtype="int16", device=dev, blocksize=1024,
        )
        self.play_stream = sd.OutputStream(
            samplerate=self.rate, channels=1,
            dtype="int16", device=dev,
        )
        self.capture_stream.start()
        self.play_stream.start()

    def read_mono(self, n_samples: int) -> np.ndarray:
        """Read n_samples, return int16 mono (channel 0 of the mic pair)."""
        data, _ = self.capture_stream.read(n_samples)
        return data[:, 0].copy()

    def read_vad_frame(self, frame_ms: int) -> bytes:
        """Read one 16-bit mono PCM frame sized for webrtcvad (10/20/30 ms)."""
        n = int(self.rate * frame_ms / 1000)
        return self.read_mono(n).tobytes()

    def play_pcm(self, pcm: bytes, stop_event=None):
        """Play 16 kHz mono 16-bit PCM. Returns early if stop_event is set."""
        arr = np.frombuffer(pcm, dtype=np.int16)
        chunk = 1024
        for i in range(0, len(arr), chunk):
            if stop_event is not None and stop_event.is_set():
                return
            self.play_stream.write(arr[i:i + chunk])

    def play_wav_bytes(self, wav_bytes: bytes, stop_event=None):
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            assert wf.getframerate() == self.rate, f"TTS sample rate {wf.getframerate()} != {self.rate}"
            self.play_pcm(wf.readframes(wf.getnframes()), stop_event)

    def stop(self):
        self.capture_stream.stop(); self.capture_stream.close()
        self.play_stream.stop(); self.play_stream.close()
```

## File: `vad.py`

```python
"""Utterance recording with webrtcvad end-of-speech detection."""
import io
import time
import wave
import webrtcvad


class UtteranceRecorder:
    def __init__(self, audio_io, cfg):
        self.audio = audio_io
        self.vad = webrtcvad.Vad(2)          # aggressiveness 0-3
        self.silence_ms = cfg["silence_ms"]
        self.max_s = cfg["max_utterance_s"]
        self.min_speech_ms = cfg["min_speech_ms"]
        self.frame_ms = 30

    def record_utterance(self, max_s=None) -> bytes | None:
        """Record until silence_ms of silence after speech. Returns WAV bytes or None."""
        frames, speech_started = [], False
        silence_accum = 0
        speech_accum = 0
        deadline = time.time() + (max_s or self.max_s)

        while time.time() < deadline:
            frame = self.audio.read_vad_frame(self.frame_ms)
            is_speech = self.vad.is_speech(frame, self.audio.rate)

            if is_speech:
                speech_started = True
                speech_accum += self.frame_ms
                silence_accum = 0
                frames.append(frame)
            elif speech_started:
                silence_accum += self.frame_ms
                frames.append(frame)
                if silence_accum >= self.silence_ms:
                    break
            # pre-speech audio is not kept — starts cleanly at first speech

        if speech_accum < self.min_speech_ms:
            return None
        return self._to_wav(b"".join(frames))

    @staticmethod
    def _to_wav(pcm: bytes) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(16000)
            wf.writeframes(pcm)
        return buf.getvalue()
```

## File: `wake_word.py`

```python
"""openWakeWord wrapper + GPIO17 button trigger (push-to-talk fallback)."""
import logging
import threading
import numpy as np

log = logging.getLogger(__name__)


class WakeWordDetector:
    def __init__(self, cfg, audio_io, button_cfg):
        self.audio = audio_io
        self.threshold = cfg.get("threshold", 0.5)
        self.enabled = cfg.get("enabled", False)
        self._flag = threading.Event()
        self._model = None
        self._button = None

        if self.enabled:
            from openwakeword.model import Model
            self._model = Model(wakeword_models=[cfg.get("model", "hey_jarvis")])
            threading.Thread(target=self._loop, daemon=True).start()
            log.info("wake word '%s' active", cfg.get("model"))

        if button_cfg.get("enabled", False):
            from gpiozero import Button
            self._button = Button(17, pull_up=True, bounce_time=0.1)
            self._button.when_pressed = self._flag.set
            log.info("GPIO17 button trigger active")

    def _loop(self):
        # openWakeWord expects 80 ms chunks of 16 kHz mono int16
        while True:
            frame = self.audio.read_mono(1280).astype(np.int16)
            scores = self._model.predict(frame)
            for name, score in scores.items():
                if score >= self.threshold:
                    log.info("wake word '%s' score %.2f", name, score)
                    self._flag.set()

    def detected(self) -> bool:
        return self._flag.is_set()

    def reset(self):
        self._flag.clear()
```

## File: `openwebui_client.py`

```python
"""HTTP client for OpenWebUI: STT, chat, TTS. Persistent connection, warmed on wake."""
import logging
import httpx

log = logging.getLogger(__name__)


class OpenWebUIClient:
    def __init__(self, cfg):
        self.base = cfg["base_url"].rstrip("/")
        self.model = cfg["model"]
        self.language = cfg.get("language", "en")
        self.headers = {"Authorization": f"Bearer {cfg['api_key']}"}
        self.client = httpx.Client(
            base_url=self.base, headers=self.headers,
            timeout=httpx.Timeout(60.0, connect=10.0),
        )
        self._stt_path = "/api/v1/audio/transcriptions"   # some builds use /api/audio/transcriptions

    def warm(self):
        """Fire on wake to pre-open TCP connection while the user speaks."""
        try:
            self.client.get("/api/models")
        except Exception as e:
            log.debug("warm-up failed (ok): %s", e)

    def transcribe(self, wav_bytes: bytes) -> str:
        files = {"file": ("utterance.wav", wav_bytes, "audio/wav")}
        data = {"language": self.language}
        try:
            r = self.client.post(self._stt_path, files=files, data=data)
        except httpx.HTTPStatusError:
            # retry legacy path once
            self._stt_path = "/api/audio/transcriptions"
            r = self.client.post(self._stt_path, files=files, data=data)
        r.raise_for_status()
        return r.json().get("text", "").strip()

    def chat(self, messages: list) -> str:
        r = self.client.post("/api/chat/completions", json={
            "model": self.model, "messages": messages, "stream": False,
        })
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()

    def synthesize(self, text: str) -> bytes:
        r = self.client.post("/api/v1/audio/speech", json={"input": text})
        r.raise_for_status()
        return r.content  # WAV/MP3 bytes
```

## File: `leds.py` — HAT status LEDs (adapted from the mic_hat `pixels.py`)

```python
"""APA102 status LEDs on the HAT. Optional — the daemon runs without it.

Uses the apa102.py + pixels.py from the seeed mic_hat examples; copy those two
files next to this one (they are in the project library). If they are missing,
LEDs are silently disabled.
"""
import logging

log = logging.getLogger(__name__)

try:
    from pixels import pixels as _pixels
    HAS_LEDS = True
except Exception:
    HAS_LEDS = False


class Leds:
    def __init__(self):
        self.ok = HAS_LEDS
        if not self.ok:
            log.info("pixels.py/apa102.py not found — LEDs disabled")

    def wakeup(self):   # triggered: rising warm light
        if self.ok: _pixels.wakeup()

    def listen(self):
        if self.ok: _pixels.listen()

    def think(self):
        if self.ok: _pixels.think()

    def speak(self):
        if self.ok: _pixels.speak()

    def off(self):
        if self.ok: _pixels.off()


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "test":
    import time, sys
    leds = Leds()
    for fn in (leds.wakeup, leds.listen, leds.think, leds.speak, leds.off):
        fn(); time.sleep(2)
```

## File: `assistant.py` — main daemon

```python
#!/usr/bin/env python3
"""Smart speaker daemon: wake/button → VAD record → STT → LLM → TTS → playback.

Phases: run with --phase 3 to only record+save (no server); default is the
full pipeline (phase 4). Wake word activates via config.
"""
import argparse
import logging
import threading
import time
from pathlib import Path

import yaml

from audio_io import AudioIO
from leds import Leds
from openwebui_client import OpenWebUIClient
from vad import UtteranceRecorder
from wake_word import WakeWordDetector

log = logging.getLogger("assistant")


class VoiceAssistant:
    def __init__(self, cfg, full_pipeline=True):
        self.cfg = cfg
        self.full = full_pipeline
        self.audio = AudioIO(cfg["audio"])
        self.leds = Leds()
        self.recorder = UtteranceRecorder(self.audio, cfg["vad"])
        self.wake = WakeWordDetector(cfg["wake_word"], self.audio, cfg["button"])
        self.client = OpenWebUIClient(cfg["openwebui"]) if full_pipeline else None
        self.system_prompt = cfg["server"]["system_prompt"]
        self.max_turns = cfg["history"]["max_turns"]
        self.messages = [{"role": "system", "content": self.system_prompt}]
        self._stop_playback = threading.Event()

    # ---------- one conversation turn ----------

    def turn(self):
        wav = self.recorder.record_utterance()
        if wav is None:
            log.info("no speech captured")
            return
        if not self.full:
            out = Path("/tmp/utterance.wav")
            out.write_bytes(wav)
            log.info("saved %s (%d bytes)", out, len(wav))
            return

        self.leds.think()
        try:
            text = self.client.transcribe(wav)
            log.info("user: %r", text)
            if not text:
                return
            self.messages.append({"role": "user", "content": text})
            reply = self.client.chat(self.messages)
            log.info("assistant: %r", reply[:120])
            self.messages.append({"role": "assistant", "content": reply})
            if len(self.messages) > 2 * self.max_turns + 1:
                self.messages = [self.messages[0]] + self.messages[-2 * self.max_turns:]
            audio_out = self.client.synthesize(reply)
        except Exception:
            log.exception("pipeline failed")
            return

        # ---- playback with barge-in (wake word/button stops audio) ----
        self._stop_playback.clear()
        self.wake.reset()
        self.leds.speak()
        player = threading.Thread(
            target=self._play, args=(audio_out,), daemon=True)
        player.start()
        while player.is_alive():
            if self.wake.detected():
                log.info("barge-in")
                self._stop_playback.set()
            time.sleep(0.05)
        self.leds.off()

    def _play(self, audio_out: bytes):
        try:
            # OpenWebUI may return WAV or MP3
            if audio_out[:4] == b"RIFF":
                self.audio.play_wav_bytes(audio_out, self._stop_playback)
            else:
                import subprocess, tempfile
                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                    f.write(audio_out)
                proc = subprocess.Popen(
                    ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", f.name])
                while proc.poll() is None:
                    if self._stop_playback.is_set():
                        proc.terminate(); break
                    time.sleep(0.05)
        except Exception:
            log.exception("playback failed")

    # ---------- main loop ----------

    def run(self):
        log.info("assistant up (wake=%s button=%s full=%s)",
                 self.wake.enabled, self.wake._button is not None, self.full)
        if self.wake.enabled or self.wake._button:
            while True:
                if self.wake.detected():
                    self.wake.reset()
                    self.leds.wakeup()
                    if self.client:
                        threading.Thread(target=self.client.warm, daemon=True).start()
                    self.turn()
                time.sleep(0.02)
        else:
            log.info("no trigger configured — running single turn")
            self.turn()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", type=int, default=4,
                    help="3 = record only (no server), 4 = full pipeline")
    ap.add_argument("--config", default="/opt/voice-assistant/config.yaml")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    with open(args.config) as f:
        cfg = yaml.safe_load(f)

    assistant = VoiceAssistant(cfg, full_pipeline=(args.phase >= 4))
    try:
        assistant.run()
    except KeyboardInterrupt:
        pass
    finally:
        assistant.audio.stop()
        assistant.leds.off()


if __name__ == "__main__":
    main()
```

## File: `voice-assistant.service`

```ini
[Unit]
Description=Smart speaker voice assistant
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/opt/voice-assistant
ExecStart=/opt/voice-assistant/.venv/bin/python assistant.py
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

Enable only after phases 1–5 pass: `sudo cp voice-assistant.service /etc/systemd/system/ && sudo systemctl enable --now voice-assistant`

## Phase-by-phase test plan

1. **Audio path** — the `arecord`/`aplay` loopback above through the powered speaker. If silent: check `alsamixer` playback level on the seeed card, and that the speaker is powered/on.
2. **API smoke test** (from the Pi):
   ```bash
   curl -s -H "Authorization: Bearer $KEY" http://YOUR_SERVER:3000/api/models | head
   ```
3. **Capture + VAD** — `python assistant.py --phase 3`, press the HAT button, speak; check `/tmp/utterance.wav` plays back.
4. **Full pipeline** — `python assistant.py` (button-triggered first: set `wake_word.enabled: false`). Speak after the button press; reply should come out of the speaker.
5. **Wake word** — set `enabled: true`, model `hey_jarvis` (bundled). Tune `threshold` up if false-triggering.
6. **systemd** — install the unit, reboot, verify it comes up and `get_throttled` is still 0x0 after boot.

## Known notes / gotchas

- `sounddevice` opens the card as `plughw:N,0` — mixing capture and playback on the WM8960 is fine (that's the designed use).
- If you get ALSA "device busy", stop any `arecord`/`aplay` first.
- TTS output must be **16 kHz**; if your OpenWebUI TTS returns 22.05/24 kHz, either resample in `audio_io.play_wav_bytes` or let the MP3/ffplay path handle it.
- Wake-word CPU on a Pi 3B is fine (~10–20% of one core with openWakeWord), but keep `sounddevice` capture in its own thread as done here.
- The power undervoltage fix is still outstanding — do that before enabling systemd autostart, or boot-throttle may delay assistant startup.