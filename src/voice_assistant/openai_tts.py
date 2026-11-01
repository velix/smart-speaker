"""TTS via any OpenAI-compatible /v1/audio/speech endpoint
(Kokoro-FastAPI on the gpu-machine today; Speaches or future Ollama
Phonon-on-Linux would work the same way).

The response STREAMS as it is synthesized, so playback starts on the first
chunk while synthesis continues — same pipelining as the local piper engine.
WAV is requested for compatibility; the RIFF header is stripped from the
first bytes so downstream gets bare int16 PCM.
"""
import logging

import httpx

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

    def chunks(self, text: str):
        """Yield playback-rate PCM chunks, streamed from the endpoint.
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

    def close(self):
        self.client.close()
