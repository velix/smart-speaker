"""Smoke tests — no hardware or network needed."""
import subprocess
import sys
import wave
import io

import yaml


def test_config_has_all_sections():
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    for key in ("openwebui", "audio", "wake_word", "vad", "button", "history", "server"):
        assert key in cfg, f"missing config section: {key}"


def test_all_modules_import():
    from voice_assistant import audio_io, openwebui_client, vad, wake_word  # noqa: F401
    from voice_assistant import assistant, leds  # noqa: F401


def test_utterance_wav_roundtrip():
    from voice_assistant.vad import UtteranceRecorder

    pcm = b"\x00\x00" * 16000  # 1 s of silence
    wav_bytes = UtteranceRecorder._to_wav(pcm)
    with wave.open(io.BytesIO(wav_bytes)) as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 16000
        assert wf.getnframes() == 16000


def test_console_script_help():
    from voice_assistant.assistant import main

    # --help exits via SystemExit(0) and prints usage
    try:
        sys.argv = ["voice-assistant", "--help"]
        main()
    except SystemExit as e:
        assert e.code == 0
    else:
        raise AssertionError("--help did not exit")


def test_leds_degrade_gracefully():
    from voice_assistant.leds import Leds

    leds = Leds()
    # on non-Pi hosts spidev is absent, so this must be False, not raise
    assert leds.ok is False
    leds.wakeup(); leds.listen(); leds.think(); leds.speak(); leds.off()
