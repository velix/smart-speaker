---
name: smart-speaker
description: Smart speaker project — ReSpeaker 2-Mic HAT (KS0314, WM8960) on Raspberry Pi 3 Model B; actual build status, power findings, project library contents, and next steps.
---

# Smart Speaker project

## Actual build status (as of 2026-10-25)
- SSH access established from the Mac: host alias `pi-speaker` (velix@192.168.2.145, key `~/.ssh/pi-smart-speaker`, ed25519, password auth also still enabled). Pi hostname: `rpi-mic`. Raspbian 11 bullseye, 32-bit armv7, kernel 6.1.21-v7+, Python 3.9.2 system. 921 MB RAM, 26 GB free disk. `/opt` empty; `uv` and `ffmpeg` not yet installed; git present.
- ALSA on rpi-mic: card 0 = bcm2835 Headphones, card 1 = vc4-hdmi, **card 2 = seeed-2mic-voicecard (WM8960)** — config.yaml `audio.card` set to 2 accordingly.
- Deployment strategy (2026-10-25, changed): develop locally → push to GitHub (public repo `smart-speaker`) → Pi pulls from GitHub. Manual tar/scp copies are retired. Dev machine pushes via dedicated SSH key `~/.ssh/github-smart-speaker` (ssh config: Host github.com). Pi-side update: `scripts/pi-update.sh` (git pull --ff-only + uv sync + service restart). Pi deploy target: `/opt/voice-assistant` git checkout.
- Pi deploy feasibility (2026-10-25, probed via SSH): uv installed on rpi-mic (0.13.5, armv7 build) and can manage Python 3.11.17 (linux-armv7) despite system Python 3.9. All default daemon deps resolve on armv7 (numpy, sounddevice, webrtcvad-wheels, gpiozero, RPi.GPIO, spidev). BLOCKER: openwakeword requires onnxruntime, which has no 32-bit armv7 wheels → openwakeword moved to an optional `wakeword` extra (dev/Mac only); wake_word.py degrades to button-only with a clear error if the engine is missing. Pi wake-word engine (Porcupine or tflite-based) is a follow-up decision.
- Power finding update (2026-10-25, SSH survey): `vcgencmd get_throttled` = 0xd0008 at idle — bit 3 (currently undervolted) is set NOW, not only at boot. Undervoltage is active during normal operation; the power fix is a blocker for stable daemon operation.
- Board: **Raspberry Pi 3 Model B** (NOT the Pi Zero WH from the design docs).
- HAT: **Keyestudio/Seeed ReSpeaker 2-Mic HAT (KS0314)** mounted on the 40-pin header. Hardware details, ALSA setup, and a walkthrough of every example script are in [respeaker-hat-reference.md](respeaker-hat-reference.md).
- Drivers: `seeed-voicecard` installed and validated — mic, codec, LEDs all work.
- Power: Pi is fed via **micro-USB from a dedicated 65 W PD brick**.
- Power finding (2026-10-25): `vcgencmd get_throttled` = 0xd0000; dmesg shows "Undervoltage detected!" ~22 s after boot, then "Voltage normalised". Undervoltage + throttle events occur at boot inrush (CPU + WiFi + HAT enumeration). Suspects: legacy 5 V path from PD brick, cable drop, Pi 3B polyfuse (F1). Agreed next steps: measure 5 V at header pin 2 vs GND during reboot, try a thicker/shorter micro-USB cable, consider GPIO 5 V injection (bypasses polyfuse), or a dedicated 5.1 V/2.5 A supply. Acceptance: get_throttled returns 0x0 after reboot.
- Audio-out: HAT 3.5 mm jack → **powered standalone speaker** (connected 2026-10-25). No amp HAT/MT3608 needed for this path.
- Outstanding: fix the boot-time undervoltage; bring the daemon up on the Pi (software is written and dev-tested; phases 1–6 not yet run on the Pi).
- Software build (2026-10-25): Phase-1 daemon drafted AND scaffolded as a uv-managed project at `~/projects/smart-speaker` (git repo, commit `b3d597b`). Layout: `pyproject.toml` + `uv.lock` + `src/voice_assistant/` (assistant.py state machine with barge-in, audio_io.py stereo→mono via sounddevice, vad.py webrtcvad, wake_word.py openWakeWord + GPIO17 button, openwebui_client.py STT/chat/TTS, leds.py APA102 UX, vendored `interfaces/` = seeed mic_hat apa102.py + pixels.py) + `tests/` + `config.yaml` + `voice-assistant.service`. Run via the `voice-assistant` console script (`uv run voice-assistant`; `--phase 3` = record-only, default 4 = full pipeline; `--config` defaults to `./config.yaml`). Pi-only deps (`spidev`, `RPi-GPIO`) carry linux platform markers, so the project syncs and tests on macOS with LEDs degrading gracefully. Verified on Mac: `uv sync`, 5 smoke tests pass, `--help` works, LED module degrades. NOT yet run/tested on the Pi. Deploy: copy repo to `/opt/voice-assistant`, `uv sync`, follow README phases (audio loopback → API smoke → VAD capture → full pipeline → wake word → systemd). Original build plan kept as `smart-speaker-daemon-pi-3b-+-respeaker-2-mic-hat(1).md`.

## Project library (primary reference material)
All 17 project files live in the read-only project library **"Smart speaker Library"** (`doclib://01a02656-9f60-776c-86b8-11b98ec1884e`). They are the base we build on. Contents: seeed-voicecard README, mic_hat README, keyestudio KS0314 wiki page, example Python scripts (record/play, one-channel extraction, device-index lookup, APA102 LED driver, Google-Home-style pixels UX, GPIO17 button, Google Assistant, Alexa+Snowboy, mic coherence, GCC-PHAT phase/TDOA), requirements.txt, the Cursor smart-speaker architecture conversation (Aug 2026), and one build photo (20260821_235701.jpg).
- What each file does and how the HAT works: see [respeaker-hat-reference.md](respeaker-hat-reference.md).

## How the HAT works (short version; details in reference file)
- **WM8960 codec** over I2S (driver: seeed-voicecard) → one ALSA card for both stereo mic capture and playback via 3.5 mm jack or XH2.54-2P speaker header. Capture standard for all examples: 16 kHz / 2 ch / 16-bit; mono extraction via channel slicing.
- **3 × APA102 LEDs** on SPI (spidev, bus 0 CS1): start frame → per-LED 32-bit frames → end frame. `pixels.py` provides wakeup/listen/think/speak/off UX states on a background thread.
- **User button** on GPIO17 (active-low).
- Speaker out is headphone-class only — for a real speaker the HAT needs either the powered-speaker/line-in approach or an amp stage (see design plans below).

## Software plan (agreed direction)
- Edge (Pi 3B) stays thin: local wake word (openWakeWord or Porcupine — Snowboy in the examples is deprecated), VAD end-of-utterance, 16 kHz mono capture, playback. Remote server runs STT (faster-whisper) → LLM (Ollama) → TTS (Piper/Kokoro), ideally behind OpenWebUI as the single API gateway. Turn-based HTTPS first, streaming WebSocket orchestrator later. Full architecture, daemon layout, latency budget, and bring-up order are in the reference file (mirrored from the Cursor conversation in the library).
- Useful building blocks already in the library: `record_one_channel.py` (mono capture pattern), `pixels.py` (assistant UX states), `button.py` (push-to-talk fallback), `phase_test.py`/`coherence.py` (2-mic verification / DOA groundwork).
- Modernization notes: replace `np.fromstring` with `np.frombuffer`; prefer sounddevice/ALSA over ffplay subprocess for low-latency playback. The Phase-1 daemon already follows both (`np.frombuffer` in audio_io.py; sounddevice playback, ffplay only as the MP3 fallback path).

## Earlier design plans (reference only — not the current build)
From earlier project chats/design docs: Pi Zero WH, 5 V/4 A wall adapter, MT3608 boost to 6 V for the speaker, speaker connected via 3.5 mm from the HAT. The Cursor architecture doc additionally assumed a Pi Zero 2 W and an old RCA powered (active) speaker needing only line-level input — hence "no amp HAT needed" in that doc. That speaker/amp reasoning does NOT apply to the current build until Velisarios confirms which speaker is used.
Do NOT assume the MT3608 or external speaker stage exists until Velisarios confirms it.

## Differences to remember
- Design said Pi Zero WH; build is Pi 3B (higher idle current, ~1.2–1.5 A peaks) — but also far more headroom, so local wake word + VAD run comfortably.
- Design included MT3608 + powered speaker; build currently has neither.
- When the MT3608 stage is added later: keep its input wired directly to the adapter, not from the Pi's 5 V rail, so amp spikes don't drag down the Pi.

## Next steps (agreed)
1. Fix boot-time undervoltage (measure, cable swap, GPIO 5 V injection or 5.1 V/2.5 A dedicated supply; acceptance: `get_throttled` = 0x0 after reboot).
2. Decide/confirm the speaker + audio-out path (HAT 3.5 mm to powered speaker, or add amp stage).
3. Bring the daemon up on the Pi per the phased plan (deploy: copy repo to /opt/voice-assistant, `uv sync`): audio path → API smoke test → VAD loop → full STT/LLM/TTS pipeline → wake word → systemd.
