#!/usr/bin/env bash
# One-time on-Pi setup for local piper TTS. Run ON THE PI:
#   ./scripts/setup-pi-tts.sh
# Downloads piper (armv7l build) + an English voice to /opt/piper.
# NOTE: binary comes from the ARCHIVED rhasspy/piper repo (Nov 2023) — the only
# piper release with a prebuilt armv7l binary. The maintained successor
# (OHF-Voice/piper1-gpl) has no armv7 builds; see docs/tts-notes.md.
set -euo pipefail

PIPER_DIR=/opt/piper
VOICE_DIR=$PIPER_DIR/voices
# lessac-low: faster than medium on the Pi 3B (and 16 kHz = no resampling)
VOICE=en_US-lessac-low
VOICE_PATH=en/en_US/lessac/low/en_US-lessac-low

sudo mkdir -p "$PIPER_DIR" "$VOICE_DIR"
sudo chown -R "$USER" "$PIPER_DIR"

if [ ! -x "$PIPER_DIR/piper/piper" ]; then
    echo "Downloading piper..."
    curl -L https://github.com/rhasspy/piper/releases/latest/download/piper_linux_armv7l.tar.gz \
        | tar xz -C "$PIPER_DIR"
fi

if ! python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$VOICE_DIR/$VOICE.onnx.json" 2>/dev/null; then
    echo "Voice missing or corrupt — downloading $VOICE..."
    curl -fL -o "$VOICE_DIR/$VOICE.onnx" "https://huggingface.co/rhasspy/piper-voices/resolve/main/$VOICE_PATH.onnx"
    curl -fL -o "$VOICE_DIR/$VOICE.onnx.json" "https://huggingface.co/rhasspy/piper-voices/resolve/main/$VOICE_PATH.onnx.json"
fi

echo "Synthesizing a test clip..."
echo "Piper is working. The smart speaker is nearly alive." \
    | "$PIPER_DIR/piper/piper" -m "$VOICE_DIR/$VOICE.onnx" -f /tmp/piper-test.wav
echo "Wrote /tmp/piper-test.wav — play it with: aplay -D plughw:2,0 /tmp/piper-test.wav"
