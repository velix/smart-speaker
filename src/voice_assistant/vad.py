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

    def record_utterance(self, max_s=None) -> bytes | None:
        """Record until silence_ms of silence after speech. Returns WAV bytes or None."""
        frames, speech_started = [], False
        silence_accum = 0
        speech_accum = 0
        deadline = time.time() + (max_s or self.max_s)

        while time.time() < deadline:
            frame = self.audio.read_vad_frame(self.frame_ms)
            is_speech = self.vad.is_speech(frame, self.audio.rate)

            if is_speech:
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
