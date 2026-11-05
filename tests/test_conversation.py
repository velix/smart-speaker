"""Conversational mode: follow-up turns without the wake word, speech barge-in
during playback, and the silence gate that closes a conversation — plus
backend-outage behavior (STT/LLM/TTS down mid-conversation).

No hardware or network: AudioIO, UtteranceRecorder, WakeWordDetector and
OpenWebUIClient are monkeypatched in the assistant module; the VAD recorder
and wake detector are unit-tested with synthetic frames.
"""
import io
import threading
import time
import wave

import numpy as np
import yaml

from voice_assistant import assistant as assistant_mod
from voice_assistant.assistant import VoiceAssistant, load_config
from voice_assistant.vad import UtteranceRecorder
from voice_assistant.wake_word import WakeWordDetector

CONV = {"enabled": True, "followup_gate_s": 0.3, "speech_barge_in": True,
        "barge_min_speech_ms": 60, "strip_wake_word": True}


# ---------- fakes ----------

def wav_bytes(pcm: bytes = b"\x01\x02" * 800, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm)
    return buf.getvalue()


class FakeStream:
    active = True


class FakeAudio:
    def __init__(self, rate=16000):
        self.rate = rate
        self.capture_stream = FakeStream()
        self.play_stream = FakeStream()
        self.frames = []        # scripted mic frames (bytes); silence when empty
        self.played = []        # pcm chunks actually played
        self.on_first_play = None   # hook fired inside the first play_pcm call

    def read_mono(self, n):
        if self.frames:
            f = self.frames.pop(0)
        else:
            f = b"\x00" * (n * 2)
        return np.frombuffer(f, dtype=np.int16)

    def read_vad_frame(self, frame_ms):
        n = int(self.rate * frame_ms / 1000)
        return self.read_mono(n).tobytes()

    def play_pcm(self, pcm, stop_event=None):
        if self.on_first_play is not None:
            hook, self.on_first_play = self.on_first_play, None
            hook()
        if stop_event is not None:
            deadline = time.time() + 0.15
            while time.time() < deadline and not stop_event.is_set():
                time.sleep(0.005)
            if stop_event.is_set():
                return          # barge-in cut playback short
        self.played.append(pcm)

    def wav_to_pcm(self, wav):
        with wave.open(io.BytesIO(wav), "rb") as wf:
            return wf.readframes(wf.getnframes())

    def stop(self):
        pass


class FakeRecorder:
    """Scripted utterance capture. Sets stop_event when the script runs dry so
    the run loop terminates. after_calls[i] runs a hook after call i."""
    def __init__(self, cfg):
        self.max_s = cfg["max_utterance_s"]
        self.script = []
        self.calls = []         # wait_s argument of each call
        self.after_calls = {}   # call index → hook
        self.stop_event = None

    def record_utterance(self, wait_s=None):
        self.calls.append(wait_s)
        i = len(self.calls) - 1
        if i in self.after_calls:
            self.after_calls[i]()
        if self.script:
            return self.script.pop(0)
        if self.stop_event is not None:
            self.stop_event.set()       # script ran dry → let run() terminate
        return None


class FakeWake:
    """Models the real detector: a single flag + source (no trigger queue)."""
    def __init__(self):
        self.enabled = True
        self._button = None
        self.flag = False
        self.source = None
        self.consumed = 0       # resets that actually cleared a set flag
        self.barge_history = []
        self.stop_event = None

    def fire(self, trigger):
        self.flag = True
        self.source = trigger

    def detected(self):
        return self.flag

    def last_trigger(self):
        return self.source or "unknown"

    def reset(self):
        if self.flag:
            self.consumed += 1
        self.flag = False

    def pause(self):
        pass

    def resume(self):
        pass

    def set_barge_in(self, enabled):
        self.barge_history.append(bool(enabled))


class FakeClient:
    model = "jarvis"

    def __init__(self):
        self.transcribe_script = []
        self.chat_script = []
        self.chat_calls = []        # deep-ish copy of the messages of each call
        self.warmed = 0

    def transcribe(self, wav):
        item = self.transcribe_script.pop(0) if self.transcribe_script else "hello"
        if isinstance(item, Exception):
            raise item
        return item

    def chat(self, messages):
        self.chat_calls.append([dict(m) for m in messages])
        item = self.chat_script.pop(0) if self.chat_script else "ok reply"
        if isinstance(item, Exception):
            raise item
        return item

    def synthesize(self, text):
        return wav_bytes(b"\x00\x00" * 400)

    def warm(self):
        self.warmed += 1


class FakeTTS:
    def __init__(self, fail=False, pcm=b"\x07\x00" * 100):
        self.fail = fail
        self.pcm = pcm
        self.stopped = False

    def chunks(self, text):
        if self.fail:
            raise RuntimeError("tts engine down")
        yield self.pcm

    def stop(self):
        self.stopped = True


class FakeVad:
    """Any non-zero PCM frame counts as speech."""
    def is_speech(self, frame, rate):
        return any(frame)


class FakeModel:
    def __init__(self, score):
        self.score = score

    def predict(self, frame):
        return {"hey_jarvis": self.score}


# ---------- helpers ----------

def make_assistant(monkeypatch, conversation=None):
    cfg = load_config("config.yaml")
    cfg["openwebui"]["base_url"] = "http://owui.test"
    cfg["openwebui"]["api_key"] = "k"
    cfg["wake_word"]["enabled"] = False
    cfg["button"]["enabled"] = False
    cfg["stt"]["engine"] = "openwebui"
    cfg["health"]["enabled"] = False
    cfg["tts"]["engine"] = "openwebui"
    cfg["tts"]["piper"]["voice"] = None       # do not import piper off-Pi
    cfg["tts"]["openai"]["base_url"] = ""      # do not construct OpenAITTS
    if conversation is not None:
        cfg["conversation"] = conversation
    audio, recorder, wake, client = FakeAudio(), FakeRecorder(cfg["vad"]), FakeWake(), FakeClient()
    monkeypatch.setattr(assistant_mod, "AudioIO", lambda c: audio)
    monkeypatch.setattr(assistant_mod, "UtteranceRecorder", lambda a, c: recorder)
    monkeypatch.setattr(assistant_mod, "WakeWordDetector", lambda w, a, b, conv=None: wake)
    monkeypatch.setattr(assistant_mod, "OpenWebUIClient", lambda c: client)
    a = VoiceAssistant(cfg, full_pipeline=True)
    return a, audio, recorder, wake, client


def run_assistant(a, recorder, wake):
    """Drive run() on a thread; the fakes set the stop event when their scripts
    run dry, and the harness force-sets it after a timeout as a safety net."""
    ev = threading.Event()
    wake.stop_event = ev
    recorder.stop_event = ev
    t = threading.Thread(target=a.run, args=(ev,), daemon=True)
    t.start()
    t.join(timeout=3)
    ev.set()
    t.join(timeout=3)
    assert not t.is_alive(), "run() did not terminate after stop_event"
    return ev


def speech_frame(n=480):
    return b"\x01" * (n * 2)


def silence_frame(n=480):
    return b"\x00" * (n * 2)


def make_detector(barge_cfg=None):
    return WakeWordDetector({"enabled": False, "threshold": 0.9, "model": "hey_jarvis"},
                            FakeAudio(), {"enabled": False}, barge_cfg)


# ---------- config ----------

def test_config_has_conversation_section():
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    conv = cfg["conversation"]
    assert conv["enabled"] is True
    assert conv["followup_gate_s"] >= 60          # "a couple of minutes"
    assert "speech_barge_in" in conv
    assert "strip_wake_word" in conv


# ---------- wake-phrase stripping ----------

def test_strip_wake_phrase():
    from voice_assistant.assistant import strip_wake_phrase
    assert strip_wake_phrase("hey jarvis, what time is it") == "what time is it"
    assert strip_wake_phrase("Hey Jarvis what's up") == "what's up"
    assert strip_wake_phrase("hey jarvis nope", "hey_jarvis") == "nope"
    assert strip_wake_phrase("hey jarvis") == ""
    # not at the start, or a longer word starting like the phrase: untouched
    assert strip_wake_phrase("say hey jarvis please") == "say hey jarvis please"
    assert strip_wake_phrase("what time is it") == "what time is it"


# ---------- VAD recorder: wait_s ----------

def make_recorder(frames, silence_ms=60, max_s=0.5, min_speech_ms=60):
    audio = FakeAudio()
    audio.frames = list(frames)
    rec = UtteranceRecorder(audio, {"silence_ms": silence_ms,
                                    "max_utterance_s": max_s,
                                    "min_speech_ms": min_speech_ms})
    rec.vad = FakeVad()
    return rec


def test_recorder_wait_s_silence_returns_none():
    rec = make_recorder([])                        # only silence
    t0 = time.time()
    assert rec.record_utterance(wait_s=0.2) is None
    assert time.time() - t0 >= 0.15               # actually waited, not instant


def test_recorder_wait_s_captures_late_speech():
    rec = make_recorder([silence_frame()] * 5 + [speech_frame()] * 10 + [silence_frame()] * 3)
    wav = rec.record_utterance(wait_s=1.0)
    assert wav is not None
    with wave.open(io.BytesIO(wav)) as wf:
        # 10 speech frames + trailing silence until silence_ms (60 ms = 2 frames)
        assert wf.getnframes() == (10 + 2) * 480


def test_recorder_default_behavior_unchanged():
    rec = make_recorder([speech_frame()] * 5 + [silence_frame()] * 3, max_s=0.2)
    wav = rec.record_utterance()                   # no wait_s → old semantics
    assert wav is not None
    rec2 = make_recorder([], max_s=0.2)
    t0 = time.time()
    assert rec2.record_utterance() is None         # all-silence → None after max_s
    assert time.time() - t0 >= 0.15


# ---------- wake detector: model trigger + speech barge-in ----------

def test_wake_word_fires_on_model_score():
    det = make_detector()
    det._model = FakeModel(0.95)
    det._handle_frame(np.zeros(1280, dtype=np.int16))
    assert det.detected()
    assert det.last_trigger() == "wake_word"


def test_wake_word_below_threshold_ignored():
    det = make_detector()
    det._model = FakeModel(0.5)
    det._handle_frame(np.zeros(1280, dtype=np.int16))
    assert not det.detected()


def test_barge_in_after_min_speech_and_rearm():
    det = make_detector({"speech_barge_in": True, "barge_min_speech_ms": 60})
    det._barge_vad = FakeVad()
    det.set_barge_in(True)
    speech = np.full(1280, 100, dtype=np.int16)
    silence = np.zeros(1280, dtype=np.int16)
    det._handle_frame(speech)
    assert det.detected()
    assert det.last_trigger() == "barge_in"
    det.reset()
    det._handle_frame(speech)                     # still speaking: no refire
    assert not det.detected()
    det._handle_frame(silence)                    # silence re-arms
    det._handle_frame(speech)
    assert det.detected()
    assert det.last_trigger() == "barge_in"


def test_barge_in_needs_config_and_enable():
    det = make_detector()                          # no barge cfg → no vad
    det.set_barge_in(True)                         # must stay disabled
    det._handle_frame(np.full(1280, 100, dtype=np.int16))
    assert not det.detected()
    det2 = make_detector({"speech_barge_in": True, "barge_min_speech_ms": 60})
    det2._barge_vad = FakeVad()
    det2._handle_frame(np.full(1280, 100, dtype=np.int16))   # not enabled yet
    assert not det2.detected()


# ---------- assistant integration: conversation state machine ----------

def test_followup_turn_needs_no_wake_word(monkeypatch):
    a, audio, rec, wake, client = make_assistant(monkeypatch, CONV)
    wake.fire("wake_word")
    rec.script.extend([wav_bytes(), wav_bytes(b"\x02\x00" * 800)])
    client.transcribe_script.extend(["what time is it", "hey jarvis and then?"])
    client.chat_script.extend(["It is noon.", "Then I rest."])
    run_assistant(a, rec, wake)
    assert a._turn_count == 2
    assert len(client.chat_calls) == 2
    assert wake.consumed == 1                     # only ONE wake-word trigger consumed
    # the follow-up transcript had the wake phrase stripped
    assert client.chat_calls[1][-1] == {"role": "user", "content": "and then?"}
    # first capture = normal turn (no wait bound), follow-up = bounded window
    assert rec.calls[0] is None
    assert rec.calls[1] is not None and 0 < rec.calls[1] <= CONV["followup_gate_s"]
    # gate silence closed the conversation; barge-in switched off again
    assert a.conversation_open is False
    assert True in wake.barge_history and wake.barge_history[-1] is False
    # history carries both turns
    assert [m["role"] for m in a.messages] == ["user", "assistant", "user", "assistant"]


def test_wake_phrase_not_stripped_when_disabled(monkeypatch):
    conv = dict(CONV, strip_wake_word=False)
    a, audio, rec, wake, client = make_assistant(monkeypatch, conv)
    wake.fire("wake_word")
    rec.script.extend([wav_bytes(), wav_bytes()])
    client.transcribe_script.extend(["q1", "hey jarvis q2"])
    run_assistant(a, rec, wake)
    assert client.chat_calls[1][-1]["content"] == "hey jarvis q2"


def test_gate_expiry_closes_conversation_and_wake_word_rearms(monkeypatch):
    a, audio, rec, wake, client = make_assistant(monkeypatch, CONV)
    wake.fire("wake_word")
    rec.script.extend([wav_bytes(), None, wav_bytes()])   # turn1, gate silence, turn2
    # the second wake word only fires once the gate has closed the conversation
    rec.after_calls[1] = lambda: wake.fire("wake_word")
    client.transcribe_script.extend(["one", "two"])
    run_assistant(a, rec, wake)
    assert a._turn_count == 2
    assert len(client.chat_calls) == 2
    assert wake.consumed == 2                     # second conversation needed the wake word
    assert a.conversation_open is False


def test_speech_barge_in_interrupts_playback(monkeypatch):
    a, audio, rec, wake, client = make_assistant(monkeypatch, CONV)
    wake.fire("wake_word")
    rec.script.extend([wav_bytes(), wav_bytes()])
    # speech starts while the first reply is playing → barge-in trigger
    audio.on_first_play = lambda: wake.fire("barge_in")
    client.transcribe_script.extend(["first question", "barging in"])
    client.chat_script.extend(["first answer", "second answer"])
    run_assistant(a, rec, wake)
    assert a._turn_count == 2
    assert wake.consumed == 2                     # wake word + barge_in
    assert len(audio.played) == 1                 # turn 1 playback was cut short
    assert len(client.chat_calls) == 2


def test_conversation_disabled_keeps_wake_word_only_behavior(monkeypatch):
    a, audio, rec, wake, client = make_assistant(monkeypatch, {"enabled": False})
    assert a.conversation_enabled is False
    wake.fire("wake_word")
    rec.script.append(wav_bytes())
    client.transcribe_script.append("hi")
    run_assistant(a, rec, wake)
    assert a._turn_count == 1
    assert len(rec.calls) == 1 and rec.calls[0] is None   # no follow-up window opened
    assert len(client.chat_calls) == 1
    assert not any(wake.barge_history)                    # barge-in never enabled


# ---------- dropped backend services ----------

def test_stt_down_aborts_turn_history_stays_clean_and_recovers(monkeypatch):
    a, audio, rec, wake, client = make_assistant(monkeypatch, CONV)
    wake.fire("wake_word")
    rec.script.extend([wav_bytes(), wav_bytes()])
    client.transcribe_script.extend([ConnectionError("stt down"), "back again"])
    client.chat_script.append("recovered reply")
    run_assistant(a, rec, wake)
    assert len(client.chat_calls) == 1            # failed turn never reached the LLM
    assert client.chat_calls[0][-1]["content"] == "back again"
    assert a.messages == [{"role": "user", "content": "back again"},
                          {"role": "assistant", "content": "recovered reply"}]
    assert a.conversation_open is False           # gate still closed normally


def test_llm_down_pops_user_message_and_recovers(monkeypatch):
    a, audio, rec, wake, client = make_assistant(monkeypatch, CONV)
    wake.fire("wake_word")
    rec.script.extend([wav_bytes(), wav_bytes()])
    client.transcribe_script.extend(["q1", "q2"])
    client.chat_script.extend([RuntimeError("llm down"), "ok"])
    run_assistant(a, rec, wake)
    assert len(client.chat_calls) == 2            # second (follow-up) turn reached the LLM
    assert a.messages == [{"role": "user", "content": "q2"},
                          {"role": "assistant", "content": "ok"}]   # no dangling user msg


def test_empty_transcript_skips_llm(monkeypatch):
    a, audio, rec, wake, client = make_assistant(monkeypatch, CONV)
    wake.fire("wake_word")
    rec.script.append(wav_bytes())
    client.transcribe_script.append("")
    run_assistant(a, rec, wake)
    assert client.chat_calls == []


def test_tts_primary_down_falls_back_to_secondary(monkeypatch):
    a, audio, rec, wake, client = make_assistant(monkeypatch, CONV)
    a.cfg["tts"]["engine"] = "openai"
    a.cfg["tts"]["fallback"] = "piper"
    a.tts_openai = FakeTTS(fail=True)
    a.tts_piper = FakeTTS(fail=False, pcm=b"\x07\x00" * 100)
    wake.fire("wake_word")
    rec.script.append(wav_bytes())
    run_assistant(a, rec, wake)
    assert audio.played == [b"\x07\x00" * 100]    # fallback audio was played
    assert len(client.chat_calls) == 1


def test_tts_all_engines_down_turn_survives(monkeypatch):
    a, audio, rec, wake, client = make_assistant(monkeypatch, CONV)
    a.cfg["tts"]["engine"] = "openai"
    a.cfg["tts"]["fallback"] = "piper"
    a.tts_openai = FakeTTS(fail=True)
    a.tts_piper = FakeTTS(fail=True)
    wake.fire("wake_word")
    rec.script.extend([wav_bytes(), wav_bytes()])
    client.transcribe_script.extend(["q1", "q2"])
    client.chat_script.extend(["a1", "a2"])
    run_assistant(a, rec, wake)                   # must not raise
    assert audio.played == []
    assert len(client.chat_calls) == 2            # conversation kept working


# ---------- health ----------

def test_health_reports_conversation_state(monkeypatch):
    a, audio, rec, wake, client = make_assistant(monkeypatch, CONV)
    a.conversation_open = True
    ok, payload = a._health_status()
    assert payload["conversation"] == "open"
    a.conversation_open = False
    _, payload = a._health_status()
    assert payload["conversation"] == "idle"
