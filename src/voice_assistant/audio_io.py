"""Mic capture (ReSpeaker WM8960, stereo → mono slice) and playback via sounddevice."""
import io
import wave
import numpy as np
import sounddevice as sd


class AudioIO:
    def __init__(self, cfg):
        self.rate = cfg["rate"]
        self.capture_channels = cfg["channels"]
        # sounddevice matches PortAudio device names by substring — the HAT
        # enumerates as "seeed-2mic-voicecard ... (hw:N,0)". ALSA strings like
        # "plughw:N,0" are NOT understood by sounddevice.
        dev = f"hw:{cfg['card']},0"
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
