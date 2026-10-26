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

# Markdown emphasis, emoji, and text emoticons — not speakable; strip before TTS.
_MARKDOWN = re.compile(r"(?<!\w)(\*+|_+|`+)(?=\w)(.+?)(?<=\w)\1(?!\w)", re.DOTALL)
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]")
_EMOTICON = re.compile(r"(?<!\S)[:;=8xX][-o\^'.]?[)DPp3/\\|(\]](?!\S)")


def clean_for_tts(text: str) -> str:
    """Strip non-verbal content (markdown, emoji, emoticons) for speech."""
    text = _MARKDOWN.sub(r"\2", text)          # *word* **word** _word_ `word`
    text = text.replace("*", " ").replace("_", " ").replace("`", " ")
    text = _EMOTICON.sub(" ", text)
    text = _EMOJI.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


class VoiceAssistant:
    def __init__(self, cfg, full_pipeline=True):
        self.cfg = cfg
        self.full = full_pipeline
        self.audio = AudioIO(cfg["audio"])
        self.leds = Leds()
        self.recorder = UtteranceRecorder(self.audio, cfg["vad"])
        self.wake = WakeWordDetector(cfg["wake_word"], self.audio, cfg["button"])
        self.client = OpenWebUIClient(cfg["openwebui"]) if full_pipeline else None
        self.system_prompt = cfg["server"]["system_prompt"]
        self.max_turns = cfg["history"]["max_turns"]
        self.messages = [{"role": "system", "content": self.system_prompt}]
        self._stop_playback = threading.Event()
        self._started = time.time()
        self._turn_count = 0
        self.health = None

    # ---------- one conversation turn ----------

    def turn(self, trigger: str = "unknown"):
        self._turn_count += 1
        log.info("turn %d started: trigger=%s", self._turn_count, trigger)

        log.info("capture: listening for speech (VAD, max %.0f s)", self.recorder.max_s)
        self.wake.pause()          # exclusive access to the mic while recording
        try:
            wav = self.recorder.record_utterance()
        finally:
            self.wake.resume()     # live again for wake-word barge-in during playback
        if wav is None:
            log.info("capture: no speech detected — turn ended")
            return
        with wave.open(io.BytesIO(wav)) as wf:
            self._last_capture_s = wf.getnframes() / wf.getframerate()
        log.info("capture: %.1f s utterance captured (%d bytes)",
                 self._last_capture_s, len(wav))

        if not self.full:
            out = Path("/tmp/utterance.wav")
            out.write_bytes(wav)
            log.info("capture: saved %s (%d bytes)", out, len(wav))
            self.leds.off()
            return

        self.leds.think()
        try:
            log.info("stt: transcribing %d-byte utterance via OpenWebUI", len(wav))
            text = self.client.transcribe(wav)
            log.info("stt: transcript=%r", text)
            if not text:
                log.info("stt: empty transcript — turn ended")
                return

            self.messages.append({"role": "user", "content": text})
            log.info("llm: requesting reply from %s (context: %d messages)",
                     self.client.model, len(self.messages))
            reply = self.client.chat(self.messages)
            log.info("llm: reply=%r", reply)
            self.messages.append({"role": "assistant", "content": reply})
            if len(self.messages) > 2 * self.max_turns + 1:
                self.messages = [self.messages[0]] + self.messages[-2 * self.max_turns:]

            spoken = clean_for_tts(reply)
            log.info("tts: cleaned text=%r", spoken)
            audio_out = self._synthesize(spoken)
            log.info("tts: audio ready (%d bytes)", len(audio_out))
        except Exception:
            log.exception("pipeline failed")
            self.leds.off()
            return

        # ---- playback with barge-in (wake word/button stops audio) ----
        self._stop_playback.clear()
        self.wake.reset()
        self.leds.speak()
        log.info("playback: starting")
        player = threading.Thread(
            target=self._play, args=(audio_out,), daemon=True)
        player.start()
        while player.is_alive():
            if self.wake.detected():
                log.info("playback: barge-in via %s", self.wake.last_trigger())
                self._stop_playback.set()
            time.sleep(0.05)
        log.info("playback: done — turn %d complete", self._turn_count)
        self.leds.off()

    # ---------- TTS ----------

    def _synthesize(self, text: str) -> bytes:
        """TTS via the configured engine; falls back to the other engine."""
        engine = self.cfg.get("tts", {}).get("engine", "openwebui")
        try:
            out = self._piper_speak(text) if engine == "piper" else self.client.synthesize(text)
            log.info("tts: synthesized via %s", engine)
            return out
        except Exception:
            log.exception("%s TTS failed", engine)
            if engine == "piper":
                if self.client is None:
                    raise
                log.info("tts: falling back to openwebui")
                return self.client.synthesize(text)
            log.info("tts: falling back to piper")
            return self._piper_speak(text)

    def _piper_speak(self, text: str) -> bytes:
        """Synthesize via the local piper binary → WAV bytes."""
        import subprocess
        import tempfile
        tts = self.cfg.get("tts", {}).get("piper", {})
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            subprocess.run(
                [tts.get("bin", "/opt/piper/piper/piper"), "-m", tts["voice"],
                 "-f", f.name],
                input=text.encode(), check=True, capture_output=True, timeout=30)
            return Path(f.name).read_bytes()

    def _play(self, audio_out: bytes):
        try:
            # OpenWebUI may return WAV or MP3
            if audio_out[:4] == b"RIFF":
                self.audio.play_wav_bytes(audio_out, self._stop_playback)
            else:
                import subprocess, tempfile
                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                    f.write(audio_out)
                proc = subprocess.Popen(
                    ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", f.name])
                while proc.poll() is None:
                    if self._stop_playback.is_set():
                        proc.terminate(); break
                    time.sleep(0.05)
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
            "turns": self._turn_count,
        }
        return ok, payload

    # ---------- main loop ----------

    def run(self):
        log.info("assistant up (wake=%s button=%s full=%s)",
                 self.wake.enabled, self.wake._button is not None, self.full)
        h = self.cfg.get("health", {})
        if h.get("enabled", False):
            from .health import HealthServer
            self.health = HealthServer(h.get("port", 8310), self._health_status)
            self.health.start()
        if self.wake.enabled or self.wake._button:
            while True:
                if self.wake.detected():
                    trigger = self.wake.last_trigger()
                    self.wake.reset()
                    self.leds.wakeup()
                    if self.client:
                        log.info("network: warming OpenWebUI connection")
                        threading.Thread(target=self.client.warm, daemon=True).start()
                    self.turn(trigger)
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


if __name__ == "__main__":
    main()
