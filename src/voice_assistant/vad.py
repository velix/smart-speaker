"""Utterance recording with webrtcvad end-of-speech detection."""
import io
import time
import wave
import webrtcvad


class UtteranceRecorder:
    def __init__(self, audio_io, cfg):
        self.audio = audio_io
        self.vad = webrtcvad.Vad(2)          # aggressiveness 0-3
        self.silence_ms = cfg["silence_ms"]
        self.max_s = cfg["max_utterance_s"]
        self.min_speech_ms = cfg["min_speech_ms"]
        self.frame_ms = 30

    def record_utterance(self, wait_s: float | None = None) -> bytes | None:
        """Record until silence_ms of silence after speech. Returns WAV bytes or None.

        wait_s caps how long to wait for speech ONSET (None = max_utterance_s,
        the historical behavior). Once speech starts, max_utterance_s caps the
        utterance length. Conversation follow-ups pass the remaining gate here.
        """
        wait = self.max_s if wait_s is None else wait_s
        frames, speech_started = [], False
        silence_accum = 0
        speech_accum = 0
        start = time.time()
        speech_start = None

        while True:
            now = time.time()
            if speech_start is None and now - start >= wait:
                break
            if speech_start is not None and now - speech_start >= self.max_s:
                break
            frame = self.audio.read_vad_frame(self.frame_ms)
            is_speech = self.vad.is_speech(frame, self.audio.rate)

            if is_speech:
                if speech_start is None:
                    speech_start = time.time()
                speech_started = True
                speech_accum += self.frame_ms
                silence_accum = 0
                frames.append(frame)
            elif speech_started:
                silence_accum += self.frame_ms
                frames.append(frame)
                if silence_accum >= self.silence_ms:
                    break
            # pre-speech audio is not kept — starts cleanly at first speech

        if speech_accum < self.min_speech_ms:
            return None
        return self._to_wav(b"".join(frames))

    @staticmethod
    def _to_wav(pcm: bytes) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(16000)
            wf.writeframes(pcm)
        return buf.getvalue()
