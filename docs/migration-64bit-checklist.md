# Migration checklist — rpi-mic to 64-bit Raspberry Pi OS

Goal: move the smart speaker from Raspbian 11 **bullseye 32-bit (armv7)** to
**64-bit Raspberry Pi OS (aarch64)** to unlock openwakeword (onnxruntime) and
maintained piper wheels. The Pi is wiped during this migration — SSH access,
secrets, and all Pi-local state are rebuilt from scratch.

Legend: **[velix]** = you do it · **[agent]** = the agent can drive it over
SSH · **[gate]** = stop if this fails.

## 0. Pre-flight (done BEFORE wiping the old system)

- [x] Secrets backed up off the Pi:
      `~/projects/smart-speaker-backup/config.local.yaml` (Mac, outside repo).
- [x] `scripts/setup-pi-tts.sh` now picks the piper tarball by `uname -m`
      (armv7l **and** aarch64).
- [x] `voice-assistant.service` fixed: `User=velix` (was `pi`).
- [ ] Optional: note anything else on the Pi worth keeping — once wiped, it's
      gone. (`~/seeed-voicecard-6.1`, `~/mic_hat-master` are re-downloadable;
      `/tmp` recordings are disposable.)

## 1. Flash [velix]

- [ ] Flash **Raspberry Pi OS **Lite** (64-bit)** to the SD card with
      Raspberry Pi Imager (Pi 3B is 64-bit capable).
- [ ] In the Imager's settings (gear icon), configure:
      - hostname: `rpi-mic`
      - username: `velix` + your password (same as before)
      - enable SSH, **allow public-key auth**, and add the existing key:
        `~/.ssh/pi-smart-speaker.pub` (cat it on the Mac to copy the line)
      - Wi-Fi credentials if you use Wi-Fi (the Pi currently uses it)
- [ ] Boot the Pi and give it ~2 min for first-boot setup.

## 2. Reconnect from the Mac [velix or agent]

- [ ] Find the new IP (router/clients page, or `ping rpi-mic.local`).
      It may differ from `192.168.2.145` — if so:
      - update `HostName` in `~/.ssh/config` under `Host pi-speaker`
      - update the uptime-kuma monitor URL (`http://<new-ip>:8310/health`)
- [ ] Clear the old SSH host key (the reimaged Pi has a new one):

      ```bash
      ssh-keygen -R 192.168.2.145   # and/or the new IP if it was used before
      ssh pi-speaker 'uname -m && uname -r'
      ```

- [ ] **Verify `uname -m` prints `aarch64`.** [gate: if not, re-flash with the
      64-bit image]

## 3. System prerequisites [agent]

- [ ] `sudo apt update && sudo apt install -y gcc python3-dev libffi-dev
      pkg-config libportaudio2 ffmpeg git`
- [ ] Install uv: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- [ ] Power sanity after fresh boot: `vcgencmd get_throttled` — expect no
      undervoltage bits (0x8/0x10000 style bits; the thermal bit may appear
      under load and is acceptable). [gate: undervoltage ⇒ revisit power
      before continuing]

## 4. Driver install — THE critical path [agent]

Do **not** use the Keyestudio wiki's Dropbox zip (`seeed-voicecard-6.1`) —
it is pinned to 2022 kernels. Use upstream:

- [ ] ```bash
      git clone https://github.com/respeaker/seeed-voicecard
      cd seeed-voicecard && sudo ./install_arm64.sh
      sudo reboot
      ```
      (only remove /etc/asound.conf first if reusing a non-fresh image)
- [ ] After reboot: `arecord -l && aplay -l` — must show
      `card N: seeed2micvoicec` (both capture and playback). Note N. [gate:
      no seeed card ⇒ do not continue; collect `sudo dmesg | grep -i wm8960`
      output and check upstream repo issues — 64-bit support is marked
      "experimental"]
- [ ] Update `audio.card` in `config.yaml` if N ≠ 2, and the test commands
      below to `plughw:N,0`.
- [ ] LED test: `uv run python -m voice_assistant.leds test` (after step 5)
      — 3 LEDs should cycle colors.

## 5. Deploy the project [agent]

- [ ] `sudo mkdir -p /opt/voice-assistant && sudo chown velix:velix /opt/voice-assistant`
- [ ] `git clone https://github.com/velix/smart-speaker.git /opt/voice-assistant`
- [ ] Restore secrets:
      `scp ~/projects/smart-speaker-backup/config.local.yaml pi-speaker:/opt/voice-assistant/`
- [ ] `cd /opt/voice-assistant && uv sync --extra wakeword`
      (64-bit now allows openwakeword — this is the whole point)
- [ ] `./scripts/setup-pi-tts.sh` — downloads the **aarch64** piper binary +
      voice, writes `/tmp/piper-test.wav`
- [ ] Play it: `aplay -D plughw:2,0 /tmp/piper-test.wav`

## 6. Verification — repeat the phases [velix + agent]

- [ ] **Phase 1 (audio)**: 6 s record + playback loopback through the
      speaker — you should hear your voice:

      ```bash
      arecord -D plughw:2,0 -f S16_LE -r 16000 -c 2 -d 6 /tmp/loop.wav
      aplay -D plughw:2,0 /tmp/loop.wav
      ```
- [ ] **Phase 2 (API)**: `uv run python -c "from voice_assistant.assistant
      import load_config; from voice_assistant.openwebui_client import
      OpenWebUIClient as C; print(C(load_config('config.yaml')['openwebui']).client.get('/api/models').status_code)"`
      → expect 200.
- [ ] **Phase 3 (capture)**: `uv run voice-assistant --phase 3`, press the
      HAT button, speak; check `/tmp/utterance.wav` plays back.
- [ ] **Phase 4 (full pipeline)**: `uv run voice-assistant`, button + speak —
      spoken reply from the speaker; log shows `trigger=button`,
      `stt:`, `llm:`, `tts: cleaned text=`, `playback:`.
- [ ] **Phase 5 (wake word — the payoff)**: FIRST measure performance —
      on the Pi, time 10 consecutive `model.predict()` calls on synthetic
      1280-sample frames; each 80 ms frame must predict in < 80 ms
      (RTF < 1) or the wake loop cannot keep up and wake word is NOT viable
      on this hardware — in that case fall back to Porcupine or tflite
      instead of tuning thresholds. If RTF is fine: restart the daemon and
      say "hey jarvis". Expected log: `wake word 'hey_jarvis' score ...`
      and `trigger=wake_word`. Tune `threshold` up if false-triggering.

## 7. systemd (phase 6) [agent, after phases pass]

- [ ] `sudo cp /opt/voice-assistant/voice-assistant.service /etc/systemd/system/`
- [ ] `sudo systemctl enable --now voice-assistant`
- [ ] `journalctl -u voice-assistant -f` shows the startup lines
      (incl. `openwakeword ... active` and `health endpoint listening`).
- [ ] Reboot, verify service is up and `vcgencmd get_throttled` shows no
      undervoltage after boot.

## 8. Post-migration bookkeeping [agent]

- [ ] Verify uptime-kuma monitor is green against the (possibly new) IP.
- [ ] Update KNOWLEDGE.md: new OS/kernel, new card number, openwakeword
      status, piper arch, IP changes. Commit + push.

## Rollback

Nothing to roll back — the old system is replaced by design. The previous
SD image can be re-flashed from the 2022 bullseye procedure (Keyestudio
wiki) if the driver fails on 64-bit and you need the speaker working again
quickly.
