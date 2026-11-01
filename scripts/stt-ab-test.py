#!/usr/bin/env python3
"""A/B harness for STT engines.

Records a corpus of real utterances from the HAT mic ONCE, then runs BOTH
engines over the same audio files and prints/saves a transcript + latency
comparison. Switching the live daemon between A and B afterwards is a
one-line config change (stt.engine in config.yaml).

A = openwebui  (current production: whisper on app_server via OpenWebUI)
B = ollama     (proposed: voxtral on gpu-machine via Ollama chat API)

Usage (on the Pi, from /opt/voice-assistant):

  # 1. record a corpus (stops the daemon for exclusive mic access,
  #    restarts it afterwards; speak naturally at the HAT):
  uv run python scripts/stt-ab-test.py record --n 8

  # 2. compare both engines on the corpus (daemon may keep running):
  uv run python scripts/stt-ab-test.py compare

Results are printed and saved to stt-corpus/results-<timestamp>.md.
Requires stt.ollama config in config.yaml (see stt section) and, for B,
the voxtral model pulled on the Ollama host (ollama pull voxtral-mini).
"""
import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

CORPUS = Path(__file__).resolve().parent.parent / "stt-corpus"
SERVICE = "voice-assistant"


def record(n: int):
    from voice_assistant.assistant import load_config
    from voice_assistant.audio_io import AudioIO
    from voice_assistant.vad import UtteranceRecorder

    active = subprocess.run(["systemctl", "is-active", "--quiet", SERVICE]).returncode == 0
    if active:
        print(f"stopping {SERVICE} for exclusive mic access...")
        subprocess.run(["sudo", "systemctl", "stop", SERVICE], check=True)
    try:
        cfg = load_config(str(Path(__file__).resolve().parent.parent / "config.yaml"))
        audio = AudioIO(cfg["audio"])
        recorder = UtteranceRecorder(audio, cfg["vad"])
        CORPUS.mkdir(exist_ok=True)
        i, skip = 1, 0
        while i <= n and skip < n * 2:
            print(f"\n=== Utterance {i}/{n} — speak NOW (max {recorder.max_s:.0f}s) ===", flush=True)
            wav = recorder.record_utterance()
            if wav is None:
                print("  no speech detected — say it again")
                skip += 1
                continue
            out = CORPUS / f"u{i:02d}.wav"
            out.write_bytes(wav)
            print(f"  saved {out} ({len(wav)} bytes)")
            i += 1
        audio.stop()
    finally:
        if active:
            print(f"restarting {SERVICE}...")
            subprocess.run(["sudo", "systemctl", "start", SERVICE], check=True)
    print(f"\ncorpus complete: {len(list(CORPUS.glob('*.wav')))} files in {CORPUS}")
    print("now run:  uv run python scripts/stt-ab-test.py compare")


def compare():
    from voice_assistant.assistant import load_config
    from voice_assistant.openwebui_client import OpenWebUIClient
    from voice_assistant.ollama_stt import OllamaSTT

    cfg = load_config(str(Path(__file__).resolve().parent.parent / "config.yaml"))
    engines = {
        "A openwebui": OpenWebUIClient(cfg["openwebui"]),
        "B ollama": OllamaSTT(cfg["stt"]["ollama"]),
    }
    files = sorted(CORPUS.glob("*.wav"))
    if not files:
        sys.exit(f"no corpus files in {CORPUS} — run the record command first")

    rows = []
    for f in files:
        data = f.read_bytes()
        row = {"file": f.name}
        for name, engine in engines.items():
            t0 = time.perf_counter()
            try:
                row[name] = engine.transcribe(data)
                row[name + " err"] = ""
            except Exception as e:
                row[name] = None
                row[name + " err"] = str(e)[:140]
            row[name + " s"] = time.perf_counter() - t0
        rows.append(row)
        a, b = row["A openwebui"], row["B ollama"]
        print(f"{f.name}: A={row['A openwebui s']:.2f}s B={row['B ollama s']:.2f}s "
              f"| A={a!r} | B={b!r}" + (f" (B ERR: {row['B ollama err']})" if not b else ""))

    lines = ["# STT A/B results", "",
             f"Corpus: {len(files)} utterances, {datetime.now().isoformat(timespec='seconds')}", "",
             "| file | A s | A transcript | B s | B transcript | B error |",
             "|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['file']} | {r['A openwebui s']:.2f} | {r['A openwebui'] or ''} "
                     f"| {r['B ollama s']:.2f} | {r['B ollama'] or ''} | {r['B ollama err']} |")
    lines.append("")
    for name in engines:
        ok = [r[name + " s"] for r in rows if r[name]]
        if ok:
            lines.append(f"- **{name}**: {len(ok)}/{len(rows)} succeeded, "
                         f"latency mean {sum(ok)/len(ok):.2f}s, min {min(ok):.2f}s, max {max(ok):.2f}s")
        else:
            lines.append(f"- **{name}**: 0/{len(rows)} succeeded")
    report = "\n".join(lines) + "\n"
    out = CORPUS / f"results-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
    out.write_text(report)
    print("\n" + report)
    print(f"saved: {out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("record").add_argument("--n", type=int, default=8,
                                          help="number of utterances to record")
    sub.add_parser("compare")
    args = ap.parse_args()
    if args.cmd == "record":
        record(args.n)
    else:
        compare()


if __name__ == "__main__":
    main()
