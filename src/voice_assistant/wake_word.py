"""openWakeWord wrapper + GPIO17 button trigger (push-to-talk fallback)."""
import logging
import threading
import time
import numpy as np

log = logging.getLogger(__name__)


class WakeWordDetector:
    def __init__(self, cfg, audio_io, button_cfg):
        self.audio = audio_io
        self.threshold = cfg.get("threshold", 0.5)
        self.enabled = cfg.get("enabled", False)
        self._flag = threading.Event()
        self._source = None
        self._paused = threading.Event()
        self._model = None
        self._button = None

        if self.enabled:
            try:
                from openwakeword.model import Model
            except ImportError as e:
                self.enabled = False
                log.error("openwakeword not installed (%s) — wake word disabled, "
                          "button trigger still active", e)
            else:
                self._model = Model(wakeword_models=[cfg.get("model", "hey_jarvis")])
                threading.Thread(target=self._loop, daemon=True).start()
                log.info("wake word '%s' active", cfg.get("model"))

        if button_cfg.get("enabled", False):
            from gpiozero import Button
            self._button = Button(17, pull_up=True, bounce_time=0.1)
            self._button.when_pressed = lambda: self._fire("button")
            log.info("GPIO17 button trigger active")

    def _loop(self):
        # openWakeWord expects 80 ms chunks of 16 kHz mono int16.
        # Paused while the VAD records — both read the same capture stream,
        # so concurrent reads would split frames between the two consumers.
        while True:
            if self._paused.is_set():
                time.sleep(0.02)
                continue
            frame = self.audio.read_mono(1280).astype(np.int16)
            scores = self._model.predict(frame)
            for name, score in scores.items():
                if score >= self.threshold:
                    log.info("wake word '%s' score %.2f", name, score)
                    self._fire("wake_word")

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
