"""Resident piper process: streams int16 PCM chunks as they are synthesized.

One piper process stays alive for the daemon's lifetime (no per-turn model
load). Text goes in on stdin line-by-line; raw PCM comes out on stdout via
--output_raw. End-of-utterance is detected as a short idle gap on stdout.
"""
import json
import logging
import os
import select
import subprocess
import time

log = logging.getLogger(__name__)

_FIRST_AUDIO_TIMEOUT_S = 20.0   # give piper this long to produce first samples
_IDLE_GAP_S = 0.4              # idle gap that marks end of this line's audio
_MAX_STREAM_S = 60.0            # safety cap per utterance


class PiperEngine:
    def __init__(self, cfg: dict, convert_fn):
        """cfg: {bin, voice}; convert_fn(pcm, src_rate, channels) -> playback PCM."""
        self.bin = cfg.get("bin", "/opt/piper/piper/piper")
        self.voice = cfg["voice"]
        self.convert_fn = convert_fn
        self._proc = None
        self._rate = None

    def _sample_rate(self) -> int:
        if self._rate is None:
            with open(self.voice + ".json") as f:
                self._rate = json.load(f)["audio"]["sample_rate"]
        return self._rate

    def _ensure(self):
        if self._proc is None or self._proc.poll() is not None:
            log.info("tts: starting resident piper (%s)", self.voice)
            self._proc = subprocess.Popen(
                [self.bin, "-m", self.voice, "--output_raw"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL)
        return self._proc

    def chunks(self, text: str):
        """Yield playback-rate PCM chunks for one line of text."""
        proc = self._ensure()
        proc.stdin.write((text + "\n").encode())
        proc.stdin.flush()

        rate = self._sample_rate()
        fd = proc.stdout.fileno()
        got_any = False
        idle = 0.0
        t0 = time.time()
        while True:
            if time.time() - t0 > _MAX_STREAM_S:
                log.warning("tts: piper stream exceeded %.0fs, stopping", _MAX_STREAM_S)
                self.stop()
                break
            r, _, _ = select.select([fd], [], [], 0.05)
            if r:
                data = os.read(fd, 8192)
                if not data:            # process died (killed or crashed)
                    self._proc = None
                    if not got_any:
                        raise RuntimeError("piper produced no audio (EOF)")
                    break
                got_any = True
                idle = 0.0
                yield self.convert_fn(data, rate, 1)
            else:
                idle += 0.05
                if not got_any and idle > _FIRST_AUDIO_TIMEOUT_S:
                    raise RuntimeError("piper produced no audio within timeout")
                if got_any and idle >= _IDLE_GAP_S:
                    break               # synthesis of this line finished

    def stop(self):
        """Kill the resident process (barge-in / errors); respawned lazily."""
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:
                pass
            self._proc = None

    def close(self):
        self.stop()
