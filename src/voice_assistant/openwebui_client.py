"""HTTP client for OpenWebUI: STT, chat, TTS. Persistent connection, warmed on wake."""
import logging
import time
import uuid
from datetime import datetime

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
        # Server-side chat (docs: "Server-Side Tool Calling (API)" / Path A):
        # without chat_id+id+stream:true+session_id, OpenWebUI runs NO tools —
        # workspace-model knowledge would silently never trigger.
        self.server_chat = cfg.get("server_chat", True)
        self.server_chat_timeout = cfg.get("server_chat_timeout", 90)
        self._session_id = f"speaker-{uuid.uuid4()}"

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
        if self.server_chat:
            try:
                return self._server_chat(messages)
            except Exception:
                log.exception("server-side chat failed — falling back to plain completion")
        r = self.client.post("/api/chat/completions", json={
            "model": self.model, "messages": messages, "stream": False,
        })
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()

    # ---------- server-side chat (Path A: full tool loop incl. knowledge) ----

    def _server_chat(self, messages: list) -> str:
        """chat_id + id + stream + session_id → OpenWebUI executes the model's
        tool calls (knowledge lookup etc.) server-side; we poll the task and
        read the final answer from a throwaway chat record."""
        user_id, assistant_id = str(uuid.uuid4()), str(uuid.uuid4())
        ts = int(time.time())
        user_msg = {"id": user_id, "role": "user", "content": messages[-1]["content"],
                    "parentId": None, "childrenIds": [assistant_id],
                    "timestamp": ts, "models": [self.model]}
        chat_id = self._create_chat(user_msg, assistant_id, ts)
        try:
            payload = {
                "model": self.model,
                "messages": messages,
                "stream": True,                      # required: tools only run on the stream path
                "chat_id": chat_id,
                "id": assistant_id,
                "user_message": user_msg,
                "session_id": self._session_id,     # THE built-in-tools switch
                "background_tasks": {"title_generation": False, "tags_generation": False,
                                     "follow_up_generation": False},
                "features": {"code_interpreter": False, "web_search": False,
                             "image_generation": False, "memory": False},
                "variables": {"{{CURRENT_DATETIME}}": datetime.now().isoformat(timespec="seconds"),
                             "{{USER_LANGUAGE}}": self.language},
            }
            r = self.client.post("/api/chat/completions", json=payload)
            r.raise_for_status()
            deadline = time.time() + self.server_chat_timeout
            while time.time() < deadline:
                t = self.client.get(f"/api/tasks/chat/{chat_id}")
                t.raise_for_status()
                if not t.json().get("task_ids"):
                    break
                time.sleep(0.4)
            c = self.client.get(f"/api/v1/chats/{chat_id}")
            c.raise_for_status()
            msg = c.json()["chat"]["history"]["messages"].get(assistant_id, {})
            content = (msg.get("content") or "").strip()
            if not content:
                raise RuntimeError("empty assistant message in chat record")
            return content
        finally:
            try:
                self.client.delete(f"/api/v1/chats/{chat_id}")
            except Exception:
                log.warning("could not delete throwaway chat %s", chat_id)

    def _create_chat(self, user_msg: dict, assistant_id: str, ts: int) -> str:
        history = {
            "currentId": assistant_id,
            "messages": {
                user_msg["id"]: user_msg,
                assistant_id: {"id": assistant_id, "role": "assistant", "content": "",
                               "parentId": user_msg["id"], "childrenIds": [],
                               "model": self.model, "modelName": self.model,
                               "modelIdx": 0, "done": False, "timestamp": ts + 1},
            },
        }
        r = self.client.post("/api/v1/chats/new", json={
            "chat": {"title": "Smart speaker", "models": [self.model], "history": history}})
        r.raise_for_status()
        return r.json()["id"]

    def synthesize(self, text: str) -> bytes:
        r = self.client.post("/api/v1/audio/speech", json={"input": text})
        r.raise_for_status()
        return r.content  # WAV/MP3 bytes
