"""TTS engine selection and fallback in VoiceAssistant._synthesize."""
import pytest

from voice_assistant.assistant import VoiceAssistant


def _bare_assistant(cfg, client=None):
    """Build a VoiceAssistant without hardware/audio init."""
    va = object.__new__(VoiceAssistant)
    va.cfg = cfg
    va.client = client
    return va


def test_piper_engine_primary():
    va = _bare_assistant({"tts": {"engine": "piper"}})
    calls = []

    def fake_piper(text):
        calls.append(("piper", text))
        return b"WAV"

    va._piper_speak = fake_piper
    assert va._synthesize("hello") == b"WAV"
    assert calls == [("piper", "hello")]


def test_piper_failure_falls_back_to_openwebui():
    class FakeClient:
        def synthesize(self, text):
            return b"SERVER-WAV"

    va = _bare_assistant({"tts": {"engine": "piper"}}, client=FakeClient())

    def failing_piper(text):
        raise RuntimeError("piper missing")

    va._piper_speak = failing_piper
    assert va._synthesize("hello") == b"SERVER-WAV"


def test_piper_failure_without_client_raises():
    va = _bare_assistant({"tts": {"engine": "piper"}}, client=None)
    va._piper_speak = lambda text: (_ for _ in ()).throw(RuntimeError("gone"))
    with pytest.raises(RuntimeError):
        va._synthesize("hello")


def test_openwebui_failure_falls_back_to_piper():
    class FakeClient:
        def synthesize(self, text):
            raise RuntimeError("server tts down")

    va = _bare_assistant({"tts": {"engine": "openwebui"}}, client=FakeClient())
    va._piper_speak = lambda text: b"PIPER-WAV"
    assert va._synthesize("hello") == b"PIPER-WAV"


def test_default_engine_is_openwebui():
    class FakeClient:
        def synthesize(self, text):
            return b"SERVER-WAV"

    va = _bare_assistant({}, client=FakeClient())
    va._piper_speak = lambda text: pytest.fail("piper must not be called")
    assert va._synthesize("hello") == b"SERVER-WAV"
