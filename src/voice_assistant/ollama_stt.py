"""STT via Ollama's OpenAI-compatible chat endpoint (voxtral audio models).

Ollama accepts OpenAI-style `input_audio` content parts on
POST /v1/chat/completions; voxtral models transcribe the clip and reply with
the transcript as message content. No auth needed on the LAN.
"""
import base64
import logging

import httpx

log = logging.getLogger(__name__)


class OllamaSTT:
    def __init__(self, cfg):
        self.base = cfg["base_url"].rstrip("/")
        self.model = cfg["model"]
        self.prompt = cfg.get("prompt", "Transcribe this audio. Reply with only the transcript.")
        self.client = httpx.Client(
            base_url=self.base, timeout=httpx.Timeout(60.0, connect=5.0))

    def payload(self, wav_bytes: bytes) -> dict:
        audio_b64 = base64.b64encode(wav_bytes).decode()
        return {
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": self.prompt},
                    {"type": "input_audio",
                     "input_audio": {"data": audio_b64, "format": "wav"}},
                ],
            }],
        }

    def transcribe(self, wav_bytes: bytes) -> str:
        r = self.client.post("/chat/completions", json=self.payload(wav_bytes))
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
