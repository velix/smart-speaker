"""APA102 status LEDs on the HAT. Optional — the daemon runs without it.

Uses the vendored apa102.py + pixels.py from the seeed mic_hat examples, in
the interfaces/ subpackage. If spidev or the modules are unavailable (e.g. off
the Pi), LEDs are silently disabled.
"""
import logging
import sys

log = logging.getLogger(__name__)

try:
    from .interfaces.pixels import pixels as _pixels
    HAS_LEDS = True
except Exception:
    HAS_LEDS = False


class Leds:
    def __init__(self):
        self.ok = HAS_LEDS
        if not self.ok:
            log.info("pixels.py/apa102.py not found — LEDs disabled")

    def wakeup(self):   # triggered: rising warm light
        if self.ok: _pixels.wakeup()

    def listen(self):
        if self.ok: _pixels.listen()

    def think(self):
        if self.ok: _pixels.think()

    def speak(self):
        if self.ok: _pixels.speak()

    def off(self):
        if self.ok: _pixels.off()


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "test":
    import time
    leds = Leds()
    for fn in (leds.wakeup, leds.listen, leds.think, leds.speak, leds.off):
        fn(); time.sleep(2)
