"""TTS engine selection and fallback in VoiceAssistant._tts_chunks."""
import threading

from voice_assistant.assistant import VoiceAssistant


class FakeAudio:
    def wav_to_pcm(self, wav):
        return b"WAV2PCM:" + wav

    def mp3_to_pcm(self, mp3):
        return b"MP32PCM:" + mp3


class FakePiper:
    def __init__(self, fail=False):
        self.fail = fail
        self.stopped = False

    def chunks(self, text):
        if self.fail:
            raise RuntimeError("piper broken")
        yield b"P1"
        yield b"P2"

    def stop(self):
        self.stopped = True


class FakeClient:
    def __init__(self, data=b"RIFFxxxx", fail=False):
        self.data = data
        self.fail = fail

    def synthesize(self, text):
        if self.fail:
            raise RuntimeError("server tts down")
        return self.data


def _bare(cfg, client=None, engine=None):
    va = object.__new__(VoiceAssistant)
    va.cfg = cfg
    va.client = client
    va.tts_engine = engine
    va.audio = FakeAudio()
    va._stop_playback = threading.Event()
    return va


def test_piper_engine_primary_streams_chunks():
    va = _bare({"tts": {"engine": "piper"}}, engine=FakePiper())
    assert list(va._tts_chunks("hello")) == [b"P1", b"P2"]


def test_piper_failure_falls_back_to_openwebui():
    va = _bare({"tts": {"engine": "piper"}}, client=FakeClient(), engine=FakePiper(fail=True))
    assert list(va._tts_chunks("hello")) == [b"WAV2PCM:RIFFxxxx"]


def test_piper_failure_without_client_yields_nothing():
    va = _bare({"tts": {"engine": "piper"}}, client=None, engine=FakePiper(fail=True))
    assert list(va._tts_chunks("hello")) == []


def test_openwebui_failure_falls_back_to_piper():
    va = _bare({"tts": {"engine": "openwebui"}}, client=FakeClient(fail=True),
               engine=FakePiper())
    assert list(va._tts_chunks("hello")) == [b"P1", b"P2"]


def test_openwebui_mp3_response_converted():
    va = _bare({"tts": {"engine": "openwebui"}}, client=FakeClient(data=b"ID3mp3data"))
    assert list(va._tts_chunks("hello")) == [b"MP32PCM:ID3mp3data"]


def test_default_engine_is_openwebui():
    va = _bare({}, client=FakeClient())
    assert list(va._tts_chunks("hello")) == [b"WAV2PCM:RIFFxxxx"]
