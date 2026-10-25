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

    @staticmethod
    def _convert_pcm(pcm: bytes, src_rate: int, channels: int, dst_rate: int) -> bytes:
        """Int16 PCM → mono at dst_rate (linear resample). TTS engines vary."""
        arr = np.frombuffer(pcm, dtype=np.int16)
        if channels == 2:
            arr = arr.reshape(-1, 2).mean(axis=1).astype(np.int16)
        if src_rate != dst_rate:
            n_out = int(len(arr) * dst_rate / src_rate)
            xs = np.arange(len(arr), dtype=np.float64)
            arr = np.interp(np.linspace(0.0, len(arr) - 1, n_out), xs, arr.astype(np.float64)).astype(np.int16)
        return arr.tobytes()

    def play_wav_bytes(self, wav_bytes: bytes, stop_event=None):
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            if wf.getsampwidth() != 2:
                raise ValueError(f"TTS sample width {wf.getsampwidth() * 8} bit unsupported")
            pcm = wf.readframes(wf.getnframes())
            pcm = self._convert_pcm(pcm, wf.getframerate(), wf.getnchannels(), self.rate)
        self.play_pcm(pcm, stop_event)

    def stop(self):
        self.capture_stream.stop(); self.capture_stream.close()
        self.play_stream.stop(); self.play_stream.close()
