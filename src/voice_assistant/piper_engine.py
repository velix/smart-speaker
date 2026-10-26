"""Piper TTS via the maintained piper-tts Python API (OHF piper1-gpl).

PiperVoice.synthesize() natively yields one AudioChunk per sentence, so
playback starts as soon as the FIRST sentence is synthesized while later
sentences are still being generated. A feeder thread synthesizes ahead of
the player through a queue; stop() makes the feeder abandon the rest
(barge-in). No subprocess, no idle-gap heuristics.
"""
import logging
import queue
import threading

log = logging.getLogger(__name__)


class PiperEngine:
    def __init__(self, cfg: dict, convert_fn):
        """cfg: {voice}; convert_fn(pcm, rate, channels) -> playback PCM.
        (cfg may contain a legacy "bin" key — unused, the wheel provides it.)"""
        self.voice_path = cfg["voice"]
        self.convert_fn = convert_fn
        self._voice = None
        self._lock = threading.Lock()
        self._active_stop = None

    def _ensure(self):
        with self._lock:
            if self._voice is None:
                from piper import PiperVoice
                log.info("tts: loading piper voice %s", self.voice_path)
                self._voice = PiperVoice.load(self.voice_path)
            return self._voice

    @staticmethod
    def _chunk_pcm(chunk) -> bytes:
        pcm = getattr(chunk, "audio_int16_bytes", None)
        if pcm is None:
            import numpy as np
            pcm = (chunk.audio_float_array * 32767.0).astype(np.int16).tobytes()
        return pcm

    def chunks(self, text: str):
        """Yield playback-rate PCM chunks, synthesized ahead of the consumer."""
        q = queue.Queue()
        stop = threading.Event()

        def feed():
            try:
                voice = self._ensure()
                for chunk in voice.synthesize(text):
                    if stop.is_set():
                        break
                    pcm = self._chunk_pcm(chunk)
                    q.put(self.convert_fn(pcm, chunk.sample_rate, chunk.sample_channels))
            except Exception:
                if not stop.is_set():
                    log.exception("piper synthesis failed")
            finally:
                q.put(None)

        with self._lock:
            self._active_stop = stop
        threading.Thread(target=feed, daemon=True).start()
        while True:
            item = q.get()
            if item is None:
                return
            yield item

    def prewarm(self):
        """Load the model at daemon start so the first turn doesn't pay for it."""
        try:
            for _ in self._ensure().synthesize("Ready."):
                pass
        except Exception:
            log.exception("piper prewarm failed")

    def stop(self):
        """Abandon the active synthesis (barge-in). The model stays loaded."""
        with self._lock:
            stop = self._active_stop
        if stop is not None:
            stop.set()

    def close(self):
        self.stop()
