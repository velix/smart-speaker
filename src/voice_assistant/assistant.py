#!/usr/bin/env python3
"""Smart speaker daemon: wake/button → VAD record → STT → LLM → TTS → playback.

Phases: run with --phase 3 to only record+save (no server); default is the
full pipeline (phase 4). Wake word activates via config.
"""
import argparse
import logging
import os
import threading
import time
from pathlib import Path

import yaml

from .audio_io import AudioIO
from .leds import Leds
from .openwebui_client import OpenWebUIClient
from .vad import UtteranceRecorder
from .wake_word import WakeWordDetector

log = logging.getLogger("assistant")


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

    # ---------- one conversation turn ----------

    def turn(self):
        wav = self.recorder.record_utterance()
        if wav is None:
            log.info("no speech captured")
            return
        if not self.full:
            out = Path("/tmp/utterance.wav")
            out.write_bytes(wav)
            log.info("saved %s (%d bytes)", out, len(wav))
            self.leds.off()
            return

        self.leds.think()
        try:
            text = self.client.transcribe(wav)
            log.info("user: %r", text)
            if not text:
                return
            self.messages.append({"role": "user", "content": text})
            reply = self.client.chat(self.messages)
            log.info("assistant: %r", reply[:120])
            self.messages.append({"role": "assistant", "content": reply})
            if len(self.messages) > 2 * self.max_turns + 1:
                self.messages = [self.messages[0]] + self.messages[-2 * self.max_turns:]
            audio_out = self.client.synthesize(reply)
        except Exception:
            log.exception("pipeline failed")
            return

        # ---- playback with barge-in (wake word/button stops audio) ----
        self._stop_playback.clear()
        self.wake.reset()
        self.leds.speak()
        player = threading.Thread(
            target=self._play, args=(audio_out,), daemon=True)
        player.start()
        while player.is_alive():
            if self.wake.detected():
                log.info("barge-in")
                self._stop_playback.set()
            time.sleep(0.05)
        self.leds.off()

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

    # ---------- main loop ----------

    def run(self):
        log.info("assistant up (wake=%s button=%s full=%s)",
                 self.wake.enabled, self.wake._button is not None, self.full)
        if self.wake.enabled or self.wake._button:
            while True:
                if self.wake.detected():
                    self.wake.reset()
                    self.leds.wakeup()
                    if self.client:
                        threading.Thread(target=self.client.warm, daemon=True).start()
                    self.turn()
                time.sleep(0.02)
        else:
            log.info("no trigger configured — running single turn")
            self.turn()


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


if __name__ == "__main__":
    main()
