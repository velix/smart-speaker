#!/usr/bin/env python3
"""A/B harness for TTS engines.

A = piper     (current production: local, on the Pi)
B = openai    (proposed: OpenAI-compatible endpoint on gpu-machine, e.g.
              Kokoro-FastAPI on port 8880 — see docs/kokoro-tts-deploy.md)

Modes (run on the Pi, from /opt/voice-assistant):

  uv run python scripts/tts-ab-test.py synth
      Synthesize sample replies through both engines, save WAVs to
      tts-corpus/ (A-NN.wav / B-NN.wav) + latency stats (time to first
      chunk, total). The daemon may keep running — no mic/speaker needed.

  uv run python scripts/tts-ab-test.py play
      Stop the daemon, play each pair through the speaker (A then B),
      restart the daemon. Listen and judge voice quality.
"""
import argparse
import subprocess
import time
from datetime import datetime
from pathlib import Path

CORPUS = Path(__file__).resolve().parent.parent / "tts-corpus"
SERVICE = "voice-assistant"

SAMPLE_REPLIES = [
    "Tomorrow looks sunny with a high near twenty degrees.",
    "Sure! What would you like to know?",
    "I have added milk and eggs to your shopping list.",
    "Your next meeting is at three thirty in the afternoon.",
    "The living room lights are now off.",
]


def _wav(pcm: bytes) -> bytes:
    import io
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm)
    return buf.getvalue()


def _engines():
    from voice_assistant.assistant import load_config
    from voice_assistant.audio_io import AudioIO
    from voice_assistant.piper_engine import PiperEngine
    from voice_assistant.openai_tts import OpenAITTS
    cfg = load_config(str(Path(__file__).resolve().parent.parent / "config.yaml"))
    convert = lambda p, r, c: AudioIO._convert_pcm(p, r, c, 16000)
    return {
        "A piper": PiperEngine(cfg["tts"]["piper"], convert),
        "B openai": OpenAITTS(cfg["tts"]["openai"], convert),
    }


def synth():
    engines = _engines()
    CORPUS.mkdir(exist_ok=True)
    # warm both engines so measurements reflect steady-state, not model loads
    for name, eng in engines.items():
        try:
            for _ in eng.chunks("Warm up."):
                pass
            print(f"{name}: warmed")
        except Exception as e:
            print(f"{name}: WARMUP FAILED: {str(e)[:120]}")
    rows = []
    for i, text in enumerate(SAMPLE_REPLIES, 1):
        row = {"n": i, "text": text}
        for name, eng in engines.items():
            t0 = time.perf_counter()
            first = None
            pcm = b""
            try:
                for ch in eng.chunks(text):
                    if first is None:
                        first = time.perf_counter() - t0
                    pcm += ch
                row[name] = (first, time.perf_counter() - t0, pcm)
            except Exception as e:
                print(f"{name} on sample {i} FAILED: {str(e)[:120]}")
                row[name] = None
        for name, v in row.items():
            if name in ("n", "text"):
                continue
            if v:
                out = CORPUS / f"{name.split()[0]}-{i:02d}.wav"
                out.write_bytes(_wav(v[2]))
        a, b = row.get("A piper"), row.get("B openai")
        print(f"sample {i}: " + (f"A first={a[0]:.2f}s total={a[1]:.2f}s" if a else "A FAILED")
              + " | " + (f"B first={b[0]:.2f}s total={b[1]:.2f}s" if b else "B FAILED"))
        rows.append(row)
    for name in ("A piper", "B openai"):
        ok = [r[name] for r in rows if r.get(name)]
        if ok:
            print(f"- {name}: first-chunk mean {sum(v[0] for v in ok)/len(ok):.2f}s, "
                  f"total mean {sum(v[1] for v in ok)/len(ok):.2f}s ({len(ok)}/{len(rows)})")
    print(f"\nWAVs saved in {CORPUS} — listen with: uv run python scripts/tts-ab-test.py play")


def play():
    import yaml
    with open(Path(__file__).resolve().parent.parent / "config.yaml") as f:
        card = yaml.safe_load(f)["audio"]["card"]

    pairs = sorted(CORPUS.glob("A-*.wav"))
    if not pairs:
        raise SystemExit("no A-*.wav in tts-corpus — run synth first")

    active = subprocess.run(["systemctl", "is-active", "--quiet", SERVICE]).returncode == 0
    if active:
        print(f"stopping {SERVICE} for exclusive speaker access...", flush=True)
        subprocess.run(["sudo", "systemctl", "stop", SERVICE], check=True)
    # PortAudio enumerates at import — only import after the device is free
    status = Path(f"/proc/asound/card{card}/pcm0p/sub0/status")
    deadline = time.time() + 20
    while time.time() < deadline:
        if "closed" in status.read_text():
            break
        time.sleep(0.25)
    from voice_assistant.audio_io import AudioIO
    try:
        import numpy as np
        audio = AudioIO({"card": card, "rate": 16000, "channels": 2})
        for a in pairs:
            b = a.with_name(a.name.replace("A-", "B-"))
            for label, f in [("A (piper)", a)] + ([("B (openai)", b)] if b.exists() else []):
                print(f"playing {label}: {f.name} ...", flush=True)
                import wave
                with wave.open(str(f), "rb") as w:
                    pcm = w.readframes(w.getnframes())
                audio.play_pcm(pcm)
                time.sleep(1.0)
        audio.stop()
    finally:
        if active:
            print(f"restarting {SERVICE}...")
            subprocess.run(["sudo", "systemctl", "start", SERVICE], check=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("synth")
    sub.add_parser("play")
    args = ap.parse_args()
    (synth if args.cmd == "synth" else play)()


if __name__ == "__main__":
    main()
