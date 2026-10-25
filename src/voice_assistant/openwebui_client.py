"""HTTP client for OpenWebUI: STT, chat, TTS. Persistent connection, warmed on wake."""
import logging
import httpx

log = logging.getLogger(__name__)


class OpenWebUIClient:
    def __init__(self, cfg):
        self.base = cfg["base_url"].rstrip("/")
        self.model = cfg["model"]
        self.language = cfg.get("language", "en")
        self.headers = {"Authorization": f"Bearer {cfg['api_key']}"}
        self.client = httpx.Client(
            base_url=self.base, headers=self.headers,
            timeout=httpx.Timeout(60.0, connect=10.0),
        )
        self._stt_path = "/api/v1/audio/transcriptions"   # some builds use /api/audio/transcriptions

    def warm(self):
        """Fire on wake to pre-open TCP connection while the user speaks."""
        try:
            self.client.get("/api/models")
        except Exception as e:
            log.debug("warm-up failed (ok): %s", e)

    def transcribe(self, wav_bytes: bytes) -> str:
        files = {"file": ("utterance.wav", wav_bytes, "audio/wav")}
        data = {"language": self.language}
        r = self.client.post(self._stt_path, files=files, data=data)
        if r.status_code == 404:
            # retry legacy path once
            self._stt_path = "/api/audio/transcriptions"
            r = self.client.post(self._stt_path, files=files, data=data)
        r.raise_for_status()
        return r.json().get("text", "").strip()

    def chat(self, messages: list) -> str:
        r = self.client.post("/api/chat/completions", json={
            "model": self.model, "messages": messages, "stream": False,
        })
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()

    def synthesize(self, text: str) -> bytes:
        r = self.client.post("/api/v1/audio/speech", json={"input": text})
        r.raise_for_status()
        return r.content  # WAV/MP3 bytes
