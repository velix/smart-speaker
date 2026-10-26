# Pi setup from scratch — smart speaker (rpi-mic)

Complete, reproducible build for a **fresh 64-bit Raspberry Pi OS (trixie,
Lite)** on the Raspberry Pi 3 Model B + ReSpeaker 2-Mic HAT (KS0314).
Everything below is the *validated minimal path* — no seeed-voicecard repo,
no dkms, no patches needed. (How we learned this is in
`docs/migration-64bit-checklist.md`; the seeed overlay's old-style phandles
break on 2026 firmware, but the kernel-bundled `wm8960-soundcard` overlay
works out of the box.)

## 1. Flash [manual]

- Raspberry Pi Imager → **Raspberry Pi OS Lite (64-bit)**.
- Imager settings (gear): hostname `rpi-mic`, user `velix` + password,
  SSH enabled with public-key auth (add `~/.ssh/pi-smart-speaker.pub`),
  Wi-Fi if used.

## 2. First login, sudo, apt [ssh from the Mac]

```bash
ssh-keygen -R 192.168.2.145   # only if this IP was used before (host key changes)
ssh -t velix@192.168.2.145 'echo "velix ALL=(ALL) NOPASSWD: ALL" | \
    sudo tee /etc/sudoers.d/010-velix-nopasswd > /dev/null && \
    sudo chmod 440 /etc/sudoers.d/010-velix-nopasswd'
```

(The Mac's `~/.ssh/config` should have the `pi-speaker` Host alias; see
the repo history for its exact shape.)

```bash
ssh pi-speaker
sudo apt update && sudo apt install -y gcc python3-dev libffi-dev pkg-config \
    libportaudio2 ffmpeg git
curl -LsSf https://astral.sh/uv/install.sh | sh     # uv, managed Python 3.11
```

## 3. Device tree overlay + SPI [the critical step]

Append to `/boot/firmware/config.txt`:

```
dtoverlay=wm8960-soundcard
dtparam=spi=on
```

Then reboot. Verify:

```bash
ls /dev/spidev0.1        # LEDs (APA102) — must exist
arecord -l               # card 1: wm8960soundcard — must be listed
aplay -l | grep wm8960   # playback device on the same card
sudo dmesg | grep -c "No MCLK"   # must be 0 after a capture test
arecord -D plughw:1,0 -f S16_LE -r 16000 -c 2 -d 2 /tmp/cap.wav && \
    aplay -D plughw:1,0 /tmp/cap.wav    # loopback must work
```

Do NOT install the seeed-voicecard service/overlay (respeaker/seeed-voicecard
or the Keyestudio zip): its runtime overlay application and old-style DTBO
phandles break the wm8960 clocks on 2026 firmware ("No MCLK configured").

## 4. Mixer (wm8960 via the generic overlay)

```bash
amixer -c 1 cset name="Capture Switch" on,on
amixer -c 1 cset name="Capture Volume" 39,39
amixer -c 1 cset name="ADC PCM Capture Volume" 195,195
amixer -c 1 cset name="Headphone Playback Volume" 127,127
```

Verify the noise floor is audible but not clipping (record 3 s, expect
non-trivial rms; the old seeed asound.state does NOT apply to this card):

```bash
arecord -D plughw:1,0 -f S16_LE -r 16000 -c 2 -d 3 /tmp/level.wav
```

**Mic input routing:** the generic overlay routes LINPUT1/RINPUT1 as "Mic
Jack". If capture is digital silence (rms < ~100 with the fan on), enable the
codec's input boost mixer paths (see troubleshooting).

## 5. Deploy the project

```bash
sudo mkdir -p /opt/voice-assistant && sudo chown velix:velix /opt/voice-assistant
git clone https://github.com/velix/smart-speaker.git /opt/voice-assistant
cd /opt/voice-assistant
# secrets (NOT in the repo): create config.local.yaml with openwebui
# base_url + api_key, or use OPENWEBUI_BASE_URL/OPENWEBUI_API_KEY env vars
./scripts/setup-pi-tts.sh       # piper aarch64 + en_US-lessac-low voice → /opt/piper
uv sync                          # openwakeword auto-included on aarch64 linux
uv run python -m voice_assistant.leds test    # 3 LEDs cycle colors
```

## 6. systemd

```bash
sudo cp /opt/voice-assistant/voice-assistant.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now voice-assistant
sleep 15 && systemctl is-active voice-assistant
sudo journalctl -u voice-assistant -f      # expect: wake word active, GPIO17
                                           # button active, assistant up, health
curl -s http://127.0.0.1:8310/health       # {"status": "ok", ...}
```

Updates from the Mac: push to GitHub, then `./scripts/pi-update.sh` on the Pi
(pulls, uv sync, restarts the service).

## 7. Monitors

uptime-kuma: HTTP(s) monitor → `http://192.168.2.145:8310/health`
(200 = capture+playback streams active, 503 = degraded).

## Troubleshooting (all hit during the 2026-10-26 migration)

- **"No MCLK configured" / capture EINVAL**: wrong overlay (seeed's) or the
  seeed-voicecard.service re-applying overlays at runtime. Fix: use
  `dtoverlay=wm8960-soundcard`, disable that service.
- **LEDs dead (`/dev/spidev0.1` missing)**: SPI disabled — add
  `dtparam=spi=on`.
- **gpiozero "Failed to add edge detection"**: RPi.GPIO is broken on
  64-bit/new kernels — the project depends on `lgpio` (>=0.2.2) which
  gpiozero prefers automatically.
- **openwakeword tflite crash on aarch64**: use
  `inference_framework="onnx"` (already in wake_word.py).
- **plain `uv sync` drops openwakeword**: it's a default dep only via the
  `linux + aarch64` marker — do not remove that marker.
- **pkill/pgrep over SSH**: see AGENTS.md — patterns match the SSH session's
  own command line; use systemctl or exact-name matches.
