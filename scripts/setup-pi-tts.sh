#!/usr/bin/env bash
# One-time on-Pi setup for local piper TTS. Run ON THE PI:
#   ./scripts/setup-pi-tts.sh
# Downloads piper (armv7l build) + an English voice to /opt/piper.
set -euo pipefail

PIPER_DIR=/opt/piper
VOICE_DIR=$PIPER_DIR/voices
VOICE=en_US-lessac-medium

sudo mkdir -p "$PIPER_DIR" "$VOICE_DIR"
sudo chown -R "$USER" "$PIPER_DIR"

if [ ! -x "$PIPER_DIR/piper/piper" ]; then
    echo "Downloading piper..."
    curl -L https://github.com/rhasspy/piper/releases/latest/download/piper_linux_armv7l.tar.gz \
        | tar xz -C "$PIPER_DIR"
fi

if [ ! -f "$VOICE_DIR/$VOICE.onnx" ]; then
    echo "Downloading voice $VOICE..."
    curl -L -o "$VOICE_DIR/$VOICE.onnx" "https://huggingface.co/rhasspy/voices/resolve/main/en/$VOICE.onnx"
    curl -L -o "$VOICE_DIR/$VOICE.onnx.json" "https://huggingface.co/rhasspy/voices/resolve/main/en/$VOICE.onnx.json"
fi

echo "Synthesizing a test clip..."
echo "Piper is working. The smart speaker is nearly alive." \
    | "$PIPER_DIR/piper/piper" -m "$VOICE_DIR/$VOICE.onnx" -f /tmp/piper-test.wav
echo "Wrote /tmp/piper-test.wav — play it with: aplay -D plughw:2,0 /tmp/piper-test.wav"
