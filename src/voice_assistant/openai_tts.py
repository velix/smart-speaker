"""TTS via any OpenAI-compatible /v1/audio/speech endpoint
(Kokoro-FastAPI on the gpu-machine today; Speaches or future Ollama
Phonon-on-Linux would work the same way).

The endpoint synthesizes a WHOLE request before streaming the body, so long
replies would block first-audio for the full synthesis time. We therefore
split the reply into sentences and synthesize them one by one on a feeder
thread (same pipelining as the local piper engine): playback starts as soon
as the FIRST sentence is ready while the rest is still being generated.
"""
import logging
import queue
import threading

import httpx

from .piper_engine import split_sentences

log = logging.getLogger(__name__)


class OpenAITTS:
    def __init__(self, cfg: dict, convert_fn):
        """cfg: {base_url, model, voice, sample_rate, api_key?}."""
        self.base = cfg["base_url"].rstrip("/")
        self.model = cfg.get("model", "kokoro")
        self.voice = cfg.get("voice", "af_heart")
        self.sample_rate = int(cfg.get("sample_rate", 24000))
        headers = {}
        if cfg.get("api_key"):
            headers["Authorization"] = f"Bearer {cfg['api_key']}"
        self.convert_fn = convert_fn
        self.client = httpx.Client(
            base_url=self.base, headers=headers,
            timeout=httpx.Timeout(60.0, connect=5.0))
        self._lock = threading.Lock()
        self._active_stop = None

    def _sentence_chunks(self, text: str):
        """One sentence → converted PCM chunks, streamed from the endpoint.
        Network chunks split at arbitrary byte boundaries — carry the odd
        byte so consumers always get whole int16 samples."""
        payload = {"model": self.model, "voice": self.voice,
                   "input": text, "response_format": "wav"}
        header_skipped = False
        buf = b""
        carry = b""
        with self.client.stream("POST", "/audio/speech", json=payload) as r:
            if r.status_code != 200:
                raise RuntimeError(f"tts endpoint {r.status_code}: {r.read()[:200]}")
            for data in r.iter_bytes():
                buf += data
                if not header_skipped:
                    idx = buf.find(b"data")
                    if idx == -1:
                        continue
                    buf = buf[idx + 8:]      # skip RIFF header up to data chunk
                    header_skipped = True
                pcm = carry + buf
                buf = b""
                even = len(pcm) - (len(pcm) % 2)
                if even:
                    yield self.convert_fn(pcm[:even], self.sample_rate, 1)
                carry = pcm[even:]

    def chunks(self, text: str):
        """Yield playback-rate PCM chunks for a whole reply, pipelined:
        a feeder thread synthesizes sentence by sentence ahead of the
        consumer (which blocks on real-time playback)."""
        q = queue.Queue()
        stop = threading.Event()

        def feed():
            try:
                with self._lock:                       # one synthesis at a time
                    for sent in split_sentences(text) or [text]:
                        if stop.is_set():
                            break
                        for chunk in self._sentence_chunks(sent):
                            if stop.is_set():
                                return
                            q.put(chunk)
            except Exception as e:
                if not stop.is_set():
                    q.put(e)
            finally:
                q.put(None)

        with self._lock:
            self._active_stop = stop
        threading.Thread(target=feed, daemon=True).start()
        while True:
            item = q.get()
            if item is None:
                return
            if isinstance(item, Exception):
                raise item
            yield item

    def stop(self):
        """Abandon the active synthesis (barge-in); the model stays loaded."""
        with self._lock:
            stop = self._active_stop
        if stop is not None:
            stop.set()

    def close(self):
        self.client.close()
