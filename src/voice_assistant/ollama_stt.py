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
        # Domain vocabulary: biases the LLM transcriber toward the user's
        # proper nouns (hostnames, projects, names) that generic ASR mangles.
        hints = cfg.get("hints") or []
        if hints:
            self.prompt += (" The speaker may use these proper nouns, verbatim: "
                            + ", ".join(hints) + ".")
        self.keep_alive = cfg.get("keep_alive", "30m")
        self.client = httpx.Client(
            base_url=self.base, timeout=httpx.Timeout(60.0, connect=5.0))

    def payload(self, wav_bytes: bytes) -> dict:
        audio_b64 = base64.b64encode(wav_bytes).decode()
        return {
            "model": self.model,
            "keep_alive": self.keep_alive,
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
        if r.status_code != 200:
            # Ollama returns e.g. 404 {"error":{"message":"model 'x' not found"}}
            raise RuntimeError(f"ollama STT {r.status_code}: {r.text[:200]}")
        return r.json()["choices"][0]["message"]["content"].strip()

    def compress_reply(self, reply: str) -> str:
        """Compress an over-long LLM reply for speech. Reuses the (warm)
        STT model — a general chat model that happens to be in VRAM."""
        r = self.client.post("/chat/completions", json={
            "model": self.model,
            "keep_alive": self.keep_alive,
            "messages": [{
                "role": "user",
                "content": ("Rewrite this answer for a voice assistant. At most 2 "
                            "short sentences, each under 12 words. Keep only the most "
                            "important fact. No lists, no markdown. Answer:\n" + reply),
            }],
        })
        if r.status_code != 200:
            raise RuntimeError(f"ollama compress {r.status_code}: {r.text[:200]}")
        return r.json()["choices"][0]["message"]["content"].strip()
