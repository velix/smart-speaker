"""Health endpoint (uptime-kuma) tests."""
import json
import urllib.request

from voice_assistant.health import HealthServer
from voice_assistant.wake_word import WakeWordDetector


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        body = e.read()
        code = e.code
    else:
        code = r.status
    return code, json.loads(body) if body[:1] == b"{" else body


def test_health_ok_returns_200_with_payload():
    srv = HealthServer(0, lambda: (True, {"status": "ok", "capture_mic": "ready"}))
    srv.start()
    try:
        code, payload = _get(f"http://127.0.0.1:{srv.port}/health")
        assert code == 200
        assert payload["status"] == "ok"
        assert payload["capture_mic"] == "ready"
    finally:
        srv.stop()


def test_health_degraded_returns_503():
    srv = HealthServer(0, lambda: (False, {"status": "degraded", "output_speaker": "unavailable"}))
    srv.start()
    try:
        code, payload = _get(f"http://127.0.0.1:{srv.port}/health")
        assert code == 503
        assert payload["output_speaker"] == "unavailable"
    finally:
        srv.stop()


def test_health_unknown_path_404():
    srv = HealthServer(0, lambda: (True, {}))
    srv.start()
    try:
        code, _ = _get(f"http://127.0.0.1:{srv.port}/nope")
        assert code == 404
    finally:
        srv.stop()


def test_trigger_attribution():
    ww = object.__new__(WakeWordDetector)
    ww._flag = __import__("threading").Event()
    ww._source = None
    assert ww.last_trigger() == "unknown"
    ww._fire("button")
    assert ww.detected()
    assert ww.last_trigger() == "button"
    ww._fire("wake_word")
    assert ww.last_trigger() == "wake_word"
    ww._flag.clear()
    assert not ww.detected()
