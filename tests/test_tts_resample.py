"""TTS WAV conversion: resample + downmix to 16 kHz mono."""
import io
import wave

import numpy as np

from voice_assistant.audio_io import AudioIO


def _wav_bytes(pcm: bytes, rate: int, channels: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels); wf.setsampwidth(2); wf.setframerate(rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def test_24k_stereo_becomes_16k_mono():
    t = np.arange(24000, dtype=np.float64) / 24000.0
    stereo = (np.stack([np.sin(2 * np.pi * 440 * t), np.sin(2 * np.pi * 440 * t)], axis=1) * 20000).astype(np.int16)
    wav = _wav_bytes(stereo.tobytes(), 24000, 2)

    with wave.open(io.BytesIO(wav), "rb") as wf:
        pcm = wf.readframes(wf.getnframes())
        out = AudioIO._convert_pcm(pcm, wf.getframerate(), wf.getnchannels(), 16000)

    arr = np.frombuffer(out, dtype=np.int16)
    assert len(arr) == 24000                      # 1 s at 16 kHz
    assert np.abs(arr).max() > 10000              # signal survived


def test_passthrough_when_already_16k_mono():
    pcm = (np.sin(np.arange(1600) * 0.1) * 10000).astype(np.int16).tobytes()
    out = AudioIO._convert_pcm(pcm, 16000, 1, 16000)
    assert out == pcm
