"""TTS engine dispatch, fallback chain, and OpenAI-TTS streaming."""
import threading

import httpx

from voice_assistant.assistant import VoiceAssistant
from voice_assistant.openai_tts import OpenAITTS


class FakeAudio:
    def wav_to_pcm(self, wav):
        return b"WAV2PCM:" + wav

    def mp3_to_pcm(self, mp3):
        return b"MP32PCM:" + mp3


class FakePiper:
    def __init__(self, fail=False):
        self.fail = fail

    def chunks(self, text):
        if self.fail:
            raise RuntimeError("piper broken")
        yield b"P1"
        yield b"P2"


class FakeOpenAI:
    def __init__(self, fail=False):
        self.fail = fail

    def chunks(self, text):
        if self.fail:
            raise RuntimeError("server tts down")
        yield b"O1"


class FakeClient:
    def __init__(self, data=b"RIFFxxxx", fail=False):
        self.data = data
        self.fail = fail

    def synthesize(self, text):
        if self.fail:
            raise RuntimeError("openwebui tts down")
        return self.data


def _bare(cfg, client=None, piper=None, openai=None):
    va = object.__new__(VoiceAssistant)
    va.cfg = cfg
    va.client = client
    va.tts_piper = piper
    va.tts_openai = openai
    va.audio = FakeAudio()
    va._stop_playback = threading.Event()
    return va


def test_piper_primary_streams_chunks():
    va = _bare({"tts": {"engine": "piper", "fallback": "openwebui"}},
               client=FakeClient(), piper=FakePiper())
    assert list(va._tts_chunks("hello")) == [b"P1", b"P2"]


def test_piper_failure_falls_back_to_openwebui():
    va = _bare({"tts": {"engine": "piper", "fallback": "openwebui"}},
               client=FakeClient(), piper=FakePiper(fail=True))
    assert list(va._tts_chunks("hello")) == [b"WAV2PCM:RIFFxxxx"]


def test_openai_primary_streams_chunks():
    va = _bare({"tts": {"engine": "openai", "fallback": "piper"}},
               openai=FakeOpenAI())
    assert list(va._tts_chunks("hello")) == [b"O1"]


def test_openai_failure_falls_back_to_piper():
    va = _bare({"tts": {"engine": "openai", "fallback": "piper"}},
               piper=FakePiper(), openai=FakeOpenAI(fail=True))
    assert list(va._tts_chunks("hello")) == [b"P1", b"P2"]


def test_openwebui_primary_and_fallback():
    va = _bare({"tts": {"engine": "openwebui", "fallback": "piper"}},
               client=FakeClient(data=b"ID3mp3data"), piper=FakePiper())
    assert list(va._tts_chunks("hello")) == [b"MP32PCM:ID3mp3data"]


def test_unknown_engine_falls_back():
    va = _bare({"tts": {"engine": "magic", "fallback": "piper"}}, piper=FakePiper())
    assert list(va._tts_chunks("hello")) == [b"P1", b"P2"]   # primary fails, fallback piper runs


def _mock_tts_client(payload_out, captured):
    def handler(request):
        captured.append(request.read().decode())
        wav_header = b"RIFF\xff\xff\xff\xffWAVEfmt " + b"\x00" * 24 + b"data\xff\xff\xff\xff"
        return httpx.Response(200, content=wav_header + payload_out)
    return httpx.Client(base_url="http://tts.test/v1",
                        transport=httpx.MockTransport(handler))


def test_openai_tts_streams_and_strips_header():
    captured = []
    eng = OpenAITTS({"base_url": "http://tts.test/v1", "model": "kokoro",
                     "voice": "af_heart", "sample_rate": 24000},
                    convert_fn=lambda p, r, c: p)
    eng.client = _mock_tts_client(b"\x01\x02\x03\x04", captured)
    out = list(eng.chunks("hello"))
    assert out == [b"\x01\x02\x03\x04"]          # RIFF header stripped
    import json
    body = json.loads(captured[0])
    assert body["model"] == "kokoro"
    assert body["voice"] == "af_heart"
    assert body["response_format"] == "wav"
