"""STT engine dispatch and Ollama voxtral payload shape."""
import base64

from voice_assistant.assistant import VoiceAssistant
from voice_assistant.ollama_stt import OllamaSTT


def test_default_stt_is_openwebui_none_client():
    va = object.__new__(VoiceAssistant)
    assert VoiceAssistant._make_stt({}) is None
    assert VoiceAssistant._make_stt({"stt": {"engine": "openwebui"}}) is None


def test_ollama_engine_selected():
    cfg = {"stt": {"engine": "ollama", "ollama": {
        "base_url": "http://gpu:11434/v1", "model": "voxtral-mini"}}}
    stt = VoiceAssistant._make_stt(cfg)
    assert isinstance(stt, OllamaSTT)
    assert stt.model == "voxtral-mini"
    assert stt.base == "http://gpu:11434/v1"


def test_unknown_engine_raises():
    try:
        VoiceAssistant._make_stt({"stt": {"engine": "magic"}})
    except ValueError as e:
        assert "magic" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_voxtral_payload_shape():
    stt = OllamaSTT({"base_url": "http://gpu:11434/v1", "model": "gemma4:e4b"})
    wav = b"RIFFxxxx" * 100
    p = stt.payload(wav)
    assert p["model"] == "gemma4:e4b"
    assert p["keep_alive"] == "30m"
    msg = p["messages"][0]
    parts = msg["content"]
    assert msg["role"] == "user"
    assert parts[0]["type"] == "text"
    assert parts[1]["type"] == "input_audio"
    assert parts[1]["input_audio"]["format"] == "wav"
    assert base64.b64decode(parts[1]["input_audio"]["data"]) == wav


def test_keep_alive_configurable():
    stt = OllamaSTT({"base_url": "http://gpu:11434/v1", "model": "m", "keep_alive": "60m"})
    assert stt.payload(b"x")["keep_alive"] == "60m"


def test_vocabulary_hints_in_prompt():
    stt = OllamaSTT({"base_url": "http://gpu:11434/v1", "model": "m",
                     "hints": ["fitlen", "Ollama"]})
    parts = stt.payload(b"x")["messages"][0]["content"]
    assert "verbatim: fitlen, Ollama" in parts[0]["text"]
    no_hints = OllamaSTT({"base_url": "http://gpu:11434/v1", "model": "m"})
    assert "verbatim" not in no_hints.prompt


def test_transcribe_dispatch():
    va = object.__new__(VoiceAssistant)

    class FakeOllama:
        def transcribe(self, wav):
            return "B-transcript"

    class FakeClient:
        def transcribe(self, wav):
            return "A-transcript"

    va.stt, va.client = FakeOllama(), FakeClient()
    assert va._transcribe(b"w") == "B-transcript"
    va.stt = None
    assert va._transcribe(b"w") == "A-transcript"
