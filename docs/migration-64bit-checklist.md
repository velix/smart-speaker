# Migration checklist — rpi-mic to 64-bit Raspberry Pi OS

> **COMPLETED 2026-10-26.** Kept as the record of how the migration actually
> went (section 4 documents the driver resolution, which diverged from the
> plan). The distilled, reproducible procedure now lives in
> `docs/pi-setup-from-scratch.md`.

Goal: the smart speaker is moved from Raspbian 11 **bullseye 32-bit (armv7)**
to **64-bit Raspberry Pi OS (aarch64)** to unlock openwakeword (onnxruntime)
and maintained piper wheels. The Pi is wiped during this migration. SSH
access, secrets, and all Pi-local state are rebuilt from scratch.

Legend: **[<user>]** = manual step · **[agent]** = drivable over SSH ·
**[gate]** = work stops if this step fails.

## 0. Pre-flight (done BEFORE wiping the old system)

- [x] Secrets backed up off the Pi:
      `~/projects/smart-speaker-backup/config.local.yaml` (Mac, outside repo).
- [x] `scripts/setup-pi-tts.sh` now picks the piper tarball by `uname -m`
      (armv7l **and** aarch64).
- [x] `voice-assistant.service` fixed: `User=<user>` (was `pi`).
- [ ] Optional: anything else on the Pi worth keeping is noted. Once wiped,
      it is gone. (`~/seeed-voicecard-6.1`, `~/mic_hat-master` are
      re-downloadable. `/tmp` recordings are disposable.)

## 1. Flash [<user>]

- [ ] **Raspberry Pi OS **Lite** (64-bit)** is flashed to the SD card with
      Raspberry Pi Imager (Pi 3B is 64-bit capable).
- [ ] In the Imager's settings (gear icon), the following is configured:
      - hostname: `rpi-mic`
      - username: `<user>` + the same password as before
      - SSH is enabled, **public-key auth is allowed**, and the existing key
        is added: `~/.ssh/pi-smart-speaker.pub` (displayed on the Mac with
        cat for copying)
      - Wi-Fi credentials if Wi-Fi is used (the Pi currently uses it)
- [ ] The Pi is booted. ~2 min is allowed for first-boot setup.

## 2. Reconnect from the Mac [<user> or agent]

- [ ] The new IP is found (router/clients page, or `ping rpi-mic.local`).
      It may differ from `pi-speaker.local`. If so:
      - `HostName` in `~/.ssh/config` under `Host pi-speaker` is updated
      - the uptime-kuma monitor URL (`http://<new-ip>:8310/health`) is updated
- [ ] The old SSH host key is cleared (the reimaged Pi has a new one):

      ```bash
      ssh-keygen -R pi-speaker.local   # and/or the new IP if it was used before
      ssh pi-speaker 'uname -m && uname -r'
      ```

- [ ] **`uname -m` printing `aarch64` is verified.** [gate: if not, the 64-bit
      image is re-flashed]

## 3. System prerequisites [agent]

- [ ] `sudo apt update && sudo apt install -y gcc python3-dev libffi-dev
      pkg-config libportaudio2 ffmpeg git`
- [ ] uv is installed: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- [ ] Power sanity after fresh boot: `vcgencmd get_throttled` is run. No
      undervoltage bits are expected (0x8/0x10000 style bits). The thermal bit
      may appear under load and is acceptable. [gate: undervoltage ⇒ power is
      revisited before continuing]

## 4. Driver install — RESOLVED DIFFERENTLY THAN PLANNED (2026-10-26, actual)

Upstream `install_arm64.sh` needed three patches on trixie 6.18:

- symlink `/boot/kernel8.img`
- usr-merge fix for its `awk` header-version parsing
- `get_kernel_version` stubbed to `uname -r`

Its runtime `dtoverlay` re-application clobbers the clock phandles ("No MCLK
configured" — wm8960 node loses its `clocks` property). The working solution:

- [x] `seeed-voicecard.service` is disabled (it re-applies the overlay at
      runtime)
- [x] `dtoverlay=wm8960-soundcard` is put in config.txt. This is the
      kernel-bundled generic WM8960 overlay. Seeed's old-style phandle DTBO
      breaks on 2026 firmware. The kernel team's overlay is built for current
      kernels.
- [x] the card enumerates as `wm8960soundcard` at **card 1** → config.yaml
      `audio.card: 1`
- [x] the mixer is set manually (the old seeed asound.state does not match
      the card name): Capture Switch on, Capture Volume 39, ADC PCM 195,
      Headphone 127
- [x] Verified: `arecord -D plughw:1,0 ...` records. Playback works. No
      "No MCLK" appears in dmesg. The noise floor is clean (rms 27).

## 5. Deploy the project [agent]

- [ ] `sudo mkdir -p /opt/voice-assistant && sudo chown <user>:<user> /opt/voice-assistant`
- [ ] `git clone https://github.com/velix/smart-speaker.git /opt/voice-assistant`
- [ ] Secrets are restored:
      `scp ~/projects/smart-speaker-backup/config.local.yaml pi-speaker:/opt/voice-assistant/`
- [ ] `cd /opt/voice-assistant && uv sync` is run. openwakeword is now a
      default dep via the `linux + aarch64` marker. No extra is needed. The
      `--extra wakeword` shown here is historical.
- [ ] `./scripts/setup-pi-tts.sh` is run. It downloads the **aarch64** piper
      binary + voice. `/tmp/piper-test.wav` is written.
- [ ] Playback is verified: `aplay -D plughw:2,0 /tmp/piper-test.wav`

## 6. Verification — repeat the phases [<user> + agent]

- [ ] **Phase 1 (audio)**: a 6 s record + playback loopback through the
      speaker is run. The voice should be audible:

      ```bash
      arecord -D plughw:2,0 -f S16_LE -r 16000 -c 2 -d 6 /tmp/loop.wav
      aplay -D plughw:2,0 /tmp/loop.wav
      ```
- [ ] **Phase 2 (API)**: `uv run python -c "from voice_assistant.assistant
      import load_config; from voice_assistant.openwebui_client import
      OpenWebUIClient as C; print(C(load_config('config.yaml')['openwebui']).client.get('/api/models').status_code)"`
      is run. The expected result is 200.
- [ ] **Phase 3 (capture)**: `uv run voice-assistant --phase 3` is run. The
      HAT button is pressed. Speech is captured. `/tmp/utterance.wav`
      playback is verified.
- [ ] **Phase 4 (full pipeline)**: `uv run voice-assistant` is run with button
      + speech. A spoken reply from the speaker is expected. The log shows
      `trigger=button`, `stt:`, `llm:`, `tts: cleaned text=`, `playback:`.
- [ ] **Phase 5 (wake word — the payoff)**:
      - Performance is measured first. On the Pi, 10 consecutive
        `model.predict()` calls on synthetic 1280-sample frames are timed.
      - Each 80 ms frame must predict in < 80 ms (RTF < 1). Otherwise the
        wake loop cannot keep up and wake word is NOT viable on this
        hardware. In that case Porcupine or tflite is used instead of
        threshold tuning.
      - If RTF is fine, the daemon is restarted and "hey jarvis" is spoken.
        Expected log: `wake word 'hey_jarvis' score ...` and
        `trigger=wake_word`. `threshold` is raised if false-triggering
        occurs.

## 7. systemd (phase 6) [agent, after phases pass]

- [ ] `sudo cp /opt/voice-assistant/voice-assistant.service /etc/systemd/system/`
- [ ] `sudo systemctl enable --now voice-assistant`
- [ ] `journalctl -u voice-assistant -f` is run. The startup lines are shown
      (incl. `openwakeword ... active` and `health endpoint listening`).
- [ ] The Pi is rebooted. The service is verified up. `vcgencmd get_throttled`
      shows no undervoltage after boot.

## 8. Post-migration bookkeeping [agent]

- [ ] The uptime-kuma monitor is verified green against the (possibly new) IP.
- [ ] KNOWLEDGE.md is updated: new OS/kernel, new card number, openwakeword
      status, piper arch, IP changes. The changes are committed and pushed.

## Rollback

Nothing is rolled back — the old system is replaced by design. The previous
SD image can be re-flashed from the 2022 bullseye procedure (Keyestudio
wiki) if the driver fails on 64-bit and the speaker is needed working again
quickly.
