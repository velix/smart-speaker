"""Path A server-side chat: full loop against a mocked OpenWebUI."""
import json

import httpx

from voice_assistant.openwebui_client import OpenWebUIClient


def _client(state_path, **cfg):
    base = {"base_url": "http://owui.test", "api_key": "k", "model": "jarvis",
            "chat_state_file": str(state_path)}
    base.update(cfg)
    return OpenWebUIClient(base)


def _handler(captured, state):
    def handler(request):
        path = request.url.path
        if path == "/api/v1/chats/new":
            captured["chat_new"] = json.loads(request.read())
            state["chat_id"] = "chat-1"
            state["missing"] = False           # the new chat now exists
            return httpx.Response(200, json={"id": "chat-1"})
        if path.startswith("/api/v1/chats/") and request.method == "GET":
            if state.get("missing"):
                return httpx.Response(404, json={"detail": "not found"})
            aid = captured["completion"]["id"]
            return httpx.Response(200, json={"chat": {"history": {
                "currentId": aid,
                "messages": {aid: {"id": aid, "content": "The answer."}}}}})
        if path == "/api/chat/completions":
            captured["completion"] = json.loads(request.read())
            return httpx.Response(200, json={"status": True, "task_ids": ["t1"]})
        if path.startswith("/api/tasks/chat/"):
            return httpx.Response(200, json={"task_ids": []})
        return httpx.Response(404)
    return handler


def test_full_loop_and_chat_persistence(tmp_path):
    captured, state = {}, {}
    c = _client(tmp_path / "chat.json")
    c.client = httpx.Client(base_url=c.base, transport=httpx.MockTransport(_handler(captured, state)))

    out = c.chat([{"role": "user", "content": "turn one?"}])
    assert out == "The answer."
    assert state["chat_id"] == "chat-1"
    assert json.loads((tmp_path / "chat.json").read_text())["chat_id"] == "chat-1"

    # second turn: same chat reused (no new /api/v1/chats/new call)
    c.chat([{"role": "user", "content": "turn two?"}])
    assert "chat_new" in captured                      # still only from turn one
    p = captured["completion"]
    assert p["chat_id"] == "chat-1"
    assert p["messages"] == [{"role": "user", "content": "turn two?"}]
    assert p["user_message"]["content"] == "turn two?"
    assert p["stream"] is True                         # tools only run on the stream path
    assert p["session_id"].startswith("speaker-")      # the built-in-tools switch
    assert p["background_tasks"]["title_generation"] is False


def test_missing_chat_is_recreated(tmp_path):
    captured, state = {}, {}
    c = _client(tmp_path / "chat.json")
    # simulate a stored chat id that no longer exists on the server
    (tmp_path / "chat.json").write_text(json.dumps({"chat_id": "gone"}))
    c._chat_id = "gone"
    state["missing"] = True
    c.client = httpx.Client(base_url=c.base, transport=httpx.MockTransport(_handler(captured, state)))

    out = c.chat([{"role": "user", "content": "hello?"}])
    assert out == "The answer."
    assert captured["chat_new"]["chat"]["title"]        # a fresh chat was created
    assert c._chat_id == "chat-1"                      # and stored for next time


def test_terminal_id_opt_in(tmp_path):
    """terminal_id only sent when configured — the docs require it explicitly
    for API callers, and it is security-sensitive (voice-triggered shell)."""
    captured, state = {}, {}
    c = _client(tmp_path / "c1.json", terminal_id="term-123")
    c.client = httpx.Client(base_url=c.base, transport=httpx.MockTransport(_handler(captured, state)))
    c.chat([{"role": "user", "content": "hi"}])
    assert captured["completion"]["terminal_id"] == "term-123"

    captured2, state2 = {}, {}
    c2 = _client(tmp_path / "c2.json")
    c2.client = httpx.Client(base_url=c2.base, transport=httpx.MockTransport(_handler(captured2, state2)))
    c2.chat([{"role": "user", "content": "hi"}])
    assert "terminal_id" not in captured2["completion"]


def test_server_chat_failure_falls_back_to_plain_completion(tmp_path):
    c = _client(tmp_path / "chat.json")
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
    assert captured["plain"]["stream"] is False         # fallback = old plain mode


def test_server_chat_disabled_uses_plain_mode(tmp_path):
    c = _client(tmp_path / "chat.json", server_chat=False)
    captured = {}

    def handler(request):
        if request.url.path == "/api/chat/completions":
            captured["plain"] = json.loads(request.read())
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
        return httpx.Response(404)

    c.client = httpx.Client(base_url=c.base, transport=httpx.MockTransport(handler))
    assert c.chat([{"role": "user", "content": "hi"}]) == "ok"
    assert captured["plain"].get("chat_id") is None
