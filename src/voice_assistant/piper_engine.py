"""Resident piper process: streams int16 PCM chunks as they are synthesized.

One piper process stays alive for the daemon's lifetime (no per-turn model
load). Piper only streams at SENTENCE granularity, so the reply is split
into sentences and fed line-by-line; a feeder thread synthesizes ahead of
the player, which plays sentence N while N+1 is being generated.

End-of-line detection: piper goes idle on stdout after finishing a line
(--output_raw). A short idle gap after data marks the end of that sentence.
"""
import json
import logging
import os
import queue
import re
import select
import subprocess
import threading
import time

log = logging.getLogger(__name__)

_FIRST_AUDIO_TIMEOUT_S = 25.0   # per sentence: time to produce first samples
_IDLE_GAP_S = 0.35              # idle gap that marks end of a sentence's audio
_MAX_STREAM_S = 120.0           # safety cap per utterance

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def split_sentences(text: str) -> list:
    """Split reply into sentences (piper streams at this granularity)."""
    return [s.strip() for s in _SENTENCE_RE.split(text) if s.strip()]


class PiperEngine:
    def __init__(self, cfg: dict, convert_fn):
        """cfg: {bin, voice}; convert_fn(pcm, src_rate, channels) -> playback PCM."""
        self.bin = cfg.get("bin", "/opt/piper/piper/piper")
        self.voice = cfg["voice"]
        self.convert_fn = convert_fn
        self._proc = None
        self._rate = None
        self._lock = threading.Lock()

    def _sample_rate(self) -> int:
        if self._rate is None:
            with open(self.voice + ".json") as f:
                self._rate = json.load(f)["audio"]["sample_rate"]
        return self._rate

    def _ensure(self):
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                log.info("tts: starting resident piper (%s)", self.voice)
                self._proc = subprocess.Popen(
                    [self.bin, "-m", self.voice, "--output_raw"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL)
            return self._proc

    def _read_sentence(self, proc, fd, rate):
        """Read raw PCM for one already-written line; ends on idle gap or EOF."""
        got_any = False
        idle = 0.0
        while True:
            r, _, _ = select.select([fd], [], [], 0.05)
            if r:
                data = os.read(fd, 8192)
                if not data:
                    if not got_any:
                        raise RuntimeError("piper produced no audio (EOF)")
                    return
                got_any = True
                idle = 0.0
                yield self.convert_fn(data, rate, 1)
            else:
                idle += 0.05
                if not got_any and idle > _FIRST_AUDIO_TIMEOUT_S:
                    raise RuntimeError("piper produced no audio within timeout")
                if got_any and idle >= _IDLE_GAP_S:
                    return

    def chunks(self, text: str):
        """Yield playback-rate PCM chunks for a whole reply, pipelined:
        a feeder thread synthesizes sentences ahead of the consumer."""
        q = queue.Queue()

        def feed():
            try:
                proc = self._ensure()
                rate = self._sample_rate()
                fd = proc.stdout.fileno()
                started = time.time()
                with self._lock:                      # one synthesis at a time
                    for sent in split_sentences(text) or [text]:
                        proc.stdin.write((sent + "\n").encode())
                        proc.stdin.flush()
                        for chunk in self._read_sentence(proc, fd, rate):
                            q.put(chunk)
                        if time.time() - started > _MAX_STREAM_S:
                            log.warning("tts: piper stream exceeded cap")
                            return
            except Exception as e:
                if not self._killed:                 # barge-in kill is not an error
                    log.exception("piper feeder failed: %s", e)
            finally:
                q.put(None)

        self._killed = False
        threading.Thread(target=feed, daemon=True).start()
        while True:
            item = q.get()
            if item is None:
                return
            yield item

    def prewarm(self):
        """Load the model at daemon start so the first turn doesn't pay for it."""
        try:
            for _ in self.chunks("Ready."):
                pass
        except Exception:
            log.exception("piper prewarm failed")

    def stop(self):
        """Kill the resident process (barge-in / errors); respawned lazily."""
        self._killed = True
        with self._lock:
            if self._proc is not None:
                try:
                    self._proc.kill()
                except Exception:
                    pass
                self._proc = None

    def close(self):
        self.stop()
