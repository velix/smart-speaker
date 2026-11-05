"""openWakeWord wrapper + GPIO17 button trigger (push-to-talk fallback)."""
import logging
import threading
import time
import numpy as np

log = logging.getLogger(__name__)


class WakeWordDetector:
    def __init__(self, cfg, audio_io, button_cfg, barge_cfg=None):
        self.audio = audio_io
        self.threshold = cfg.get("threshold", 0.5)
        self.enabled = cfg.get("enabled", False)
        self._flag = threading.Event()
        self._source = None
        self._paused = threading.Event()
        self._model = None
        self._button = None
        # speech barge-in: while a conversation is open, sustained speech (not
        # just the wake word) interrupts playback. One frame is fed to both the
        # wake-word model and the barge VAD — the two never read the capture
        # stream concurrently.
        self._rate = getattr(audio_io, "rate", 16000)
        barge_cfg = barge_cfg or {}
        self._barge_min_ms = int(barge_cfg.get("barge_min_speech_ms", 200))
        self._barge_vad = None
        if barge_cfg.get("speech_barge_in", False):
            import webrtcvad
            self._barge_vad = webrtcvad.Vad(2)
        self._barge_enabled = False
        self._barge_speech_ms = 0
        self._barge_armed = True

        if self.enabled:
            try:
                from openwakeword.model import Model
            except ImportError as e:
                self.enabled = False
                log.error("openwakeword not installed (%s) — wake word disabled, "
                          "button trigger still active", e)
            else:
                self._model = Model(wakeword_models=[cfg.get("model", "hey_jarvis")],
                                    inference_framework="onnx")  # tflite_runtime broken on aarch64
                threading.Thread(target=self._loop, daemon=True).start()
                log.info("wake word '%s' active", cfg.get("model"))

        if button_cfg.get("enabled", False):
            from gpiozero import Button
            self._button = Button(17, pull_up=True, bounce_time=0.1)
            self._button.when_pressed = lambda: self._fire("button")
            log.info("GPIO17 button trigger active")

    def set_barge_in(self, enabled: bool):
        """Enable/disable speech barge-in (called as conversations open/close)."""
        self._barge_enabled = bool(enabled) and self._barge_vad is not None
        self._barge_speech_ms = 0
        self._barge_armed = True
        log.info("speech barge-in %s", "enabled" if self._barge_enabled else "disabled")

    def _loop(self):
        # openWakeWord expects 80 ms chunks of 16 kHz mono int16.
        # Paused while the VAD records — both read the same capture stream,
        # so concurrent reads would split frames between the two consumers.
        while True:
            if self._paused.is_set():
                time.sleep(0.02)
                continue
            frame = self.audio.read_mono(1280).astype(np.int16)
            self._handle_frame(frame)

    def _handle_frame(self, frame):
        """One 80 ms frame: wake-word model first, then the barge-in VAD."""
        if self._model is not None:
            for name, score in self._model.predict(frame).items():
                if score >= self.threshold:
                    log.info("wake word '%s' score %.2f", name, score)
                    self._fire("wake_word")
        if self._barge_enabled and self._barge_vad is not None:
            self._barge_feed(frame)

    def _barge_feed(self, frame):
        # webrtcvad wants 10/20/30 ms frames — split the 80 ms frame as 30+30+20
        voiced = [self._barge_vad.is_speech(frame[o:o + n].tobytes(), self._rate)
                  for o, n in ((0, 480), (480, 480), (960, 320))]
        if all(voiced):
            self._barge_speech_ms += 80
        else:
            self._barge_speech_ms = 0
            self._barge_armed = True               # silence re-arms the trigger
        if self._barge_armed and self._barge_speech_ms >= self._barge_min_ms:
            log.info("barge-in: %d ms of speech", self._barge_speech_ms)
            self._fire("barge_in")
            self._barge_speech_ms = 0
            self._barge_armed = False              # no refire until silence

    def pause(self):
        self._paused.set()

    def resume(self):
        self._paused.clear()

    def _fire(self, source: str):
        self._source = source
        self._flag.set()

    def last_trigger(self) -> str:
        """What fired the current trigger: 'wake_word', 'button', or 'unknown'."""
        return self._source or "unknown"

    def detected(self) -> bool:
        return self._flag.is_set()

    def reset(self):
        self._flag.clear()
