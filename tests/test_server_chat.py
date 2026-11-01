"""Path A server-side chat: full loop against a mocked OpenWebUI."""
import json

import httpx

from voice_assistant.openwebui_client import OpenWebUIClient


def _client(**cfg):
    base = {"base_url": "http://owui.test", "api_key": "k", "model": "jarvis"}
    base.update(cfg)
    return OpenWebUIClient(base)


def test_server_chat_full_loop():
    c = _client()
    state = {"pending": True, "deleted": False}
    captured = {}

    def handler(request):
        path = request.url.path
        if path == "/api/v1/chats/new":
            captured["chat_new"] = json.loads(request.read())
            return httpx.Response(200, json={"id": "chat-1"})
        if path == "/api/chat/completions":
            captured["completion"] = json.loads(request.read())
            return httpx.Response(200, json={"status": True, "task_ids": ["t1"]})
        if path.startswith("/api/tasks/chat/"):
            state["pending"] = False
            return httpx.Response(200, json={"task_ids": [] if not state["pending"] else ["t1"]})
        if path.startswith("/api/v1/chats/"):
            if request.method == "DELETE":
                state["deleted"] = True
                return httpx.Response(200, json=True)
            aid = captured["completion"]["id"]
            return httpx.Response(200, json={"chat": {"history": {"messages": {
                aid: {"id": aid, "content": "The answer."}}}}})
        return httpx.Response(404)

    c.client = httpx.Client(base_url=c.base, transport=httpx.MockTransport(handler))
    out = c.chat([{"role": "user", "content": "hello?"}])
    assert out == "The answer."
    assert state["deleted"]                      # throwaway chat cleaned up

    p = captured["completion"]
    assert p["stream"] is True                   # tools only run on the stream path
    assert p["chat_id"] == "chat-1"
    assert p["session_id"].startswith("speaker-")  # the built-in-tools switch
    assert p["messages"] == [{"role": "user", "content": "hello?"}]
    assert p["background_tasks"]["title_generation"] is False
    assert p["user_message"]["id"]               # caller-generated user message
    assert captured["chat_new"]["chat"]["history"]["messages"]  # chat pre-created


def test_server_chat_failure_falls_back_to_plain_completion():
    c = _client()
    captured = {}

    def handler(request):
        if request.url.path == "/api/v1/chats/new":
            return httpx.Response(500, json={"error": "boom"})
        if request.url.path == "/api/chat/completions":
            captured["plain"] = json.loads(request.read())
            return httpx.Response(200, json={"choices": [{"message": {"content": "plain reply"}}]})
        return httpx.Response(404)

    c.client = httpx.Client(base_url=c.base, transport=httpx.MockTransport(handler))
    out = c.chat([{"role": "user", "content": "hi"}])
    assert out == "plain reply"
    assert captured["plain"]["stream"] is False   # fallback = old plain mode


def test_server_chat_disabled_uses_plain_mode():
    c = _client(server_chat=False)
    captured = {}

    def handler(request):
        if request.url.path == "/api/chat/completions":
            captured["plain"] = json.loads(request.read())
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
        return httpx.Response(404)

    c.client = httpx.Client(base_url=c.base, transport=httpx.MockTransport(handler))
    assert c.chat([{"role": "user", "content": "hi"}]) == "ok"
    assert captured["plain"].get("chat_id") is None
