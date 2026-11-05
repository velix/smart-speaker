#!/usr/bin/env python3
"""Smart speaker daemon: wake/button → VAD record → STT → LLM → TTS → playback.

Phases: run with --phase 3 to only record+save (no server); default is the
full pipeline (phase 4). Wake word activates via config.
"""
import argparse
import io
import logging
import os
import re
import threading
import time
import wave
from pathlib import Path

import yaml

from .audio_io import AudioIO
from .leds import Leds
from .openwebui_client import OpenWebUIClient
from .vad import UtteranceRecorder
from .wake_word import WakeWordDetector

log = logging.getLogger("assistant")

# Markdown emphasis, emoji, text emoticons, and citation markers — not
# speakable; strip before TTS.
_MARKDOWN = re.compile(r"(?<!\w)(\*+|_+|`+)(?=\w)(.+?)(?<=\w)\1(?!\w)", re.DOTALL)
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]")
_EMOTICON = re.compile(r"(?<!\S)[:;=8xX][-o\^'.]?[)DPp3/\\|(\]](?!\S)")
_CITATION = re.compile(r"\s*\[\d+(?:,\s*\d+)*\]")   # knowledge-tool sources: [1] [1,2]


def clean_for_tts(text: str) -> str:
    """Strip non-verbal content (markdown, emoji, emoticons, citations) for speech."""
    text = _MARKDOWN.sub(r"\2", text)          # *word* **word** _word_ `word`
    text = text.replace("*", " ").replace("_", " ").replace("`", " ")
    text = _EMOTICON.sub(" ", text)
    text = _EMOJI.sub("", text)
    text = _CITATION.sub("", text)             # [1], [2, 3] — never spoken
    text = re.sub(r"(?:^|\s)-\s", " ", text)    # markdown list bullets
    text = re.sub(r"\s*-{3,}\s*", " ", text)     # horizontal rules (---)
    text = text.replace("→", " to ").replace("=>", " to ").replace("←", " from ")
    return re.sub(r"\s+", " ", text).strip()


def strip_wake_phrase(text: str, wake_model: str = "hey_jarvis") -> str:
    """Remove a leading wake-word phrase ("hey jarvis, ...") from a transcript
    captured during an open conversation — there the wake word is optional, so
    it must not end up in the LLM context."""
    phrase = r"\s+".join(re.escape(w) for w in wake_model.replace("_", " ").split())
    return re.sub(rf"^\s*{phrase}\b[\s,.:!-]*", "", text, flags=re.IGNORECASE).strip()


class VoiceAssistant:
    def __init__(self, cfg, full_pipeline=True):
        self.cfg = cfg
        self.full = full_pipeline
        self.audio = AudioIO(cfg["audio"])
        self.leds = Leds()
        self.recorder = UtteranceRecorder(self.audio, cfg["vad"])
        # conversation mode: follow-up turns without the wake word, speech
        # barge-in during playback, and a silence gate that closes it
        conv = cfg.get("conversation") or {}
        self.conversation_enabled = bool(conv.get("enabled", False))
        self.followup_gate_s = float(conv.get("followup_gate_s", 120))
        self.speech_barge_in = bool(conv.get("speech_barge_in", True))
        self.strip_wake_word = bool(conv.get("strip_wake_word", True))
        self.conversation_open = False
        self._last_turn_end = 0.0
        self._stop_event = None
        self.wake = WakeWordDetector(cfg["wake_word"], self.audio, cfg["button"], conv)
        self.client = OpenWebUIClient(cfg["openwebui"]) if full_pipeline else None
        self.stt = self._make_stt(cfg) if full_pipeline else None
        self.system_prompt = cfg["server"]["system_prompt"]
        # false when an OpenWebUI workspace model owns the prompt/personality
        self.send_system = cfg.get("server", {}).get("send_system_prompt", True)
        self.max_turns = cfg["history"]["max_turns"]
        self.messages = [{"role": "system", "content": self.system_prompt}] if self.send_system else []
        self._stop_playback = threading.Event()
        self._started = time.time()
        self._turn_count = 0
        self.health = None
        self.tts_piper = None
        self.tts_openai = None
        convert = lambda pcm, rate, ch: AudioIO._convert_pcm(pcm, rate, ch, self.audio.rate)
        if cfg.get("tts", {}).get("piper", {}).get("voice"):
            from .piper_engine import PiperEngine
            self.tts_piper = PiperEngine(cfg["tts"]["piper"], convert)
        if cfg.get("tts", {}).get("openai", {}).get("base_url"):
            from .openai_tts import OpenAITTS
            self.tts_openai = OpenAITTS(cfg["tts"]["openai"], convert)

    # ---------- STT ----------

    @staticmethod
    def _make_stt(cfg: dict):
        """Returns None for the default openwebui path (client.transcribe),
        or a dedicated STT client when stt.engine is set to one."""
        stt_cfg = cfg.get("stt", {})
        engine = stt_cfg.get("engine", "openwebui")
        if engine == "ollama":
            from .ollama_stt import OllamaSTT
            return OllamaSTT(stt_cfg.get("ollama", {}))
        if engine != "openwebui":
            raise ValueError(f"unknown stt.engine: {engine}")
        return None

    def _transcribe(self, wav: bytes) -> str:
        if self.stt is not None:
            return self.stt.transcribe(wav)
        return self.client.transcribe(wav)

    # ---------- one conversation turn ----------

    def _capture(self, wait_s=None):
        """Record one utterance. wait_s bounds the wait for speech ONSET —
        conversation follow-ups pass the remaining gate here."""
        log.info("capture: listening for speech (VAD, max %.0f s)", self.recorder.max_s)
        self.wake.pause()          # exclusive access to the mic while recording
        try:
            return self.recorder.record_utterance(wait_s=wait_s)
        finally:
            self.wake.resume()     # live again for wake-word/barge-in during playback

    def turn(self, trigger: str = "unknown"):
        self._turn_count += 1
        log.info("turn %d started: trigger=%s", self._turn_count, trigger)
        wav = self._capture()
        if wav is None:
            log.info("capture: no speech detected — turn ended")
            self.leds.off()
            return
        if not self.full:
            out = Path("/tmp/utterance.wav")
            out.write_bytes(wav)
            log.info("capture: saved %s (%d bytes)", out, len(wav))
            self.leds.off()
            return
        self._process_utterance(wav, trigger)

    def _process_utterance(self, wav: bytes, trigger: str):
        """STT → LLM → TTS → playback for one captured utterance."""
        with wave.open(io.BytesIO(wav)) as wf:
            capture_s = wf.getnframes() / wf.getframerate()
        log.info("capture: %.1f s utterance captured (%d bytes)",
                 capture_s, len(wav))

        self.leds.think()
        try:
            log.info("stt: transcribing %d-byte utterance via %s", len(wav),
                     self.cfg.get("stt", {}).get("engine", "openwebui"))
            text = self._transcribe(wav)
            if (self.conversation_open and self.strip_wake_word
                    and trigger in ("followup", "barge_in")):
                stripped = strip_wake_phrase(
                    text, self.cfg["wake_word"].get("model", "hey_jarvis"))
                if stripped != text:
                    log.info("stt: wake phrase stripped: %r → %r", text, stripped)
                text = stripped
            log.info("stt: transcript=%r", text)
            if not text:
                log.info("stt: empty transcript — turn ended")
                self.leds.off()
                return

            self.messages.append({"role": "user", "content": text})
            log.info("llm: requesting reply from %s (context: %d messages)",
                     self.client.model, len(self.messages))
            try:
                reply = self.client.chat(self.messages)
            except Exception:
                # backend dropped mid-turn: do not leave a dangling user
                # message in the history the next turn would build on
                self.messages.pop()
                raise
            log.info("llm: reply=%r", reply)
            # Workspace-model discipline drifts on knowledge-tool turns
            # (RAG chunks drown the system prompt) — enforce a word budget.
            max_words = self.cfg.get("server", {}).get("max_reply_words", 0)
            if max_words and len(reply.split()) > max_words:
                try:
                    short = (self.stt.compress_reply(reply) if self.stt is not None
                             else self.client.chat([
                                 {"role": "user", "content":
                                  "Rewrite this for a voice assistant: at most 2 short "
                                  "sentences, no markdown. Answer:\n" + reply}]))
                    log.info("llm: reply %d words over budget %d — compressed to %d words",
                             len(reply.split()), max_words, len(short.split()))
                    reply = short
                except Exception:
                    log.exception("reply compression failed — using the full reply")
            self.messages.append({"role": "assistant", "content": reply})
            limit = 2 * self.max_turns
            if self.send_system and len(self.messages) > limit + 1:
                self.messages = [self.messages[0]] + self.messages[-limit:]
            elif not self.send_system and len(self.messages) > limit:
                self.messages = self.messages[-limit:]

            spoken = clean_for_tts(reply)
            log.info("tts: cleaned text=%r", spoken)
        except Exception:
            log.exception("pipeline failed")
            self.leds.off()
            return

        # ---- streaming playback with barge-in (wake word/button/speech) ----
        self._stop_playback.clear()
        self.wake.reset()
        self.leds.speak()
        log.info("playback: starting")
        player = threading.Thread(
            target=self._play_tts, args=(spoken,), daemon=True)
        player.start()
        while player.is_alive():
            if self._stop_event is not None and self._stop_event.is_set():
                self._stop_playback.set()
                break
            if self.wake.detected():
                log.info("playback: barge-in via %s", self.wake.last_trigger())
                self._stop_playback.set()
                if self.tts_piper:
                    self.tts_piper.stop()   # kill resident synthesis mid-stream
                if self.tts_openai:
                    self.tts_openai.stop()  # abandon the in-flight sentence stream
            time.sleep(0.05)
        log.info("playback: done — turn %d complete", self._turn_count)
        self.leds.off()

    # ---------- TTS ----------

    def _server_tts_pcm(self, text: str) -> bytes:
        """OpenWebUI TTS → playback-rate PCM (WAV or MP3 response)."""
        data = self.client.synthesize(text)
        return self.audio.wav_to_pcm(data) if data[:4] == b"RIFF" else self.audio.mp3_to_pcm(data)

    def _engine_chunks(self, engine: str, text: str):
        if engine == "piper":
            if self.tts_piper is None:
                raise RuntimeError("piper voice not configured")
            yield from self.tts_piper.chunks(text)
        elif engine == "openai":
            if self.tts_openai is None:
                raise RuntimeError("openai tts not configured")
            yield from self.tts_openai.chunks(text)
        elif engine == "openwebui":
            yield self._server_tts_pcm(text)
        else:
            raise ValueError(f"unknown tts engine: {engine}")

    def _tts_chunks(self, text: str):
        """Yield playback-rate PCM chunks from the configured engine,
        falling back to tts.fallback (default: piper) on failure."""
        primary = self.cfg.get("tts", {}).get("engine", "openwebui")
        fallback = self.cfg.get("tts", {}).get("fallback", "piper")
        try:
            yield from self._engine_chunks(primary, text)
            return
        except Exception:
            if self._stop_playback.is_set():     # barge-in — not a failure
                return
            log.exception("%s TTS failed", primary)
        if fallback == primary:
            return
        try:
            log.info("tts: falling back to %s", fallback)
            yield from self._engine_chunks(fallback, text)
        except Exception:
            log.exception("tts fallback failed")

    def _play_tts(self, text: str):
        """Consume TTS chunks and play them; stop_event cuts playback short."""
        t0 = time.time()
        first = True
        try:
            for pcm in self._tts_chunks(text):
                if self._stop_playback.is_set():
                    return
                if first:
                    log.info("tts: first audio after %.0f ms", (time.time() - t0) * 1000)
                    first = False
                self.audio.play_pcm(pcm, self._stop_playback)
        except Exception:
            log.exception("playback failed")

    # ---------- health endpoint (uptime-kuma) ----------

    def _health_status(self):
        capture_ready = self.audio.capture_stream.active
        playback_ready = self.audio.play_stream.active
        ok = capture_ready and playback_ready
        payload = {
            "status": "ok" if ok else "degraded",
            "uptime_s": int(time.time() - self._started),
            "capture_mic": "ready" if capture_ready else "unavailable",
            "output_speaker": "ready" if playback_ready else "unavailable",
            "trigger": {
                "wake_word": self.wake.enabled,
                "button": self.wake._button is not None,
            },
            "tts_engine": self.cfg.get("tts", {}).get("engine", "openwebui"),
            "stt_engine": self.cfg.get("stt", {}).get("engine", "openwebui"),
            "turns": self._turn_count,
            "conversation": "open" if self.conversation_open else "idle",
        }
        return ok, payload

    # ---------- conversation state machine ----------

    def _conversation_begin(self):
        if self.conversation_enabled:
            self.conversation_open = True
            self.wake.set_barge_in(self.speech_barge_in)
            log.info("conversation opened (trigger) — follow-ups accepted for %.0f s",
                     self.followup_gate_s)

    def _conversation_followups(self, stopped=None):
        """After a turn: keep accepting follow-up speech (no wake word) until
        the user has been silent for followup_gate_s — then close the
        conversation and go back to wake-word-only idle."""
        if not self.conversation_enabled:
            return
        stopped = stopped or (lambda: False)
        self.conversation_open = True
        self.wake.set_barge_in(self.speech_barge_in)
        self.leds.listen()
        while not stopped():
            remaining = self.followup_gate_s - (time.time() - self._last_turn_end)
            if remaining <= 0:
                break
            if self.wake.detected():
                # button/barge flag raised during the window — let the main
                # loop start a fresh turn (and keep the conversation open)
                return
            wav = self._capture(wait_s=remaining)
            if wav is None:
                break                       # silence for the whole gate
            self._turn_count += 1
            log.info("turn %d started: trigger=followup", self._turn_count)
            self._process_utterance(wav, "followup")
            self._last_turn_end = time.time()
        self.conversation_open = False
        self.wake.set_barge_in(False)
        self.leds.off()
        log.info("conversation closed — back to wake word (gate %.0f s)",
                 self.followup_gate_s)

    # ---------- main loop ----------

    def run(self, stop_event=None):
        self._stop_event = stop_event
        log.info("assistant up (wake=%s button=%s full=%s conversation=%s)",
                 self.wake.enabled, self.wake._button is not None, self.full,
                 self.conversation_enabled)
        h = self.cfg.get("health", {})
        if h.get("enabled", False):
            from .health import HealthServer
            self.health = HealthServer(h.get("port", 8310), self._health_status)
            self.health.start()
        if self.tts_piper and self.cfg.get("tts", {}).get("engine") == "piper":
            threading.Thread(target=self.tts_piper.prewarm, daemon=True).start()
        if self.wake.enabled or self.wake._button:
            while not (stop_event is not None and stop_event.is_set()):
                if self.wake.detected():
                    trigger = self.wake.last_trigger()
                    self.wake.reset()
                    self.leds.wakeup()
                    if self.client:
                        log.info("network: warming OpenWebUI connection")
                        threading.Thread(target=self.client.warm, daemon=True).start()
                    self._conversation_begin()
                    self.turn(trigger)
                    self._last_turn_end = time.time()
                    self._conversation_followups(
                        stopped=lambda: stop_event is not None and stop_event.is_set())
                time.sleep(0.02)
        else:
            log.info("no trigger configured — running single turn")
            self.turn("manual")


def _deep_merge(base: dict, override: dict):
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def load_config(path: str) -> dict:
    """Load config.yaml, then overlay secrets without touching the repo:
    1. optional gitignored config.local.yaml next to the config file
    2. OPENWEBUI_BASE_URL / OPENWEBUI_API_KEY env vars (systemd EnvironmentFile)
    """
    with open(path) as f:
        cfg = yaml.safe_load(f)
    local = Path(path).with_name("config.local.yaml")
    if local.exists():
        with open(local) as f:
            _deep_merge(cfg, yaml.safe_load(f) or {})
        log.info("applied config override %s", local)
    if os.environ.get("OPENWEBUI_BASE_URL"):
        cfg["openwebui"]["base_url"] = os.environ["OPENWEBUI_BASE_URL"]
    if os.environ.get("OPENWEBUI_API_KEY"):
        cfg["openwebui"]["api_key"] = os.environ["OPENWEBUI_API_KEY"]
    return cfg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", type=int, default=4,
                    help="3 = record only (no server), 4 = full pipeline")
    ap.add_argument("--config", default="config.yaml",
                    help="path to config.yaml (default: ./config.yaml)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = load_config(args.config)

    assistant = VoiceAssistant(cfg, full_pipeline=(args.phase >= 4))
    try:
        assistant.run()
    except KeyboardInterrupt:
        pass
    finally:
        assistant.audio.stop()
        assistant.leds.off()
        if assistant.health:
            assistant.health.stop()
        if assistant.tts_piper:
            assistant.tts_piper.close()
        if assistant.tts_openai:
            assistant.tts_openai.close()


if __name__ == "__main__":
    main()
