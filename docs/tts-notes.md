# TTS findings — OpenWebUI + Mistral Voxtral (2026-10-26 — setup updated 2026-11-01)

This note records why the speaker does not use the OpenWebUI gateway for TTS.
It records what Mistral Voxtral would need if it is ever wanted. It records
how the current engine stack (Kokoro on the GPU, local piper as fallback) was
chosen.

## What was found empirically

OpenWebUI 0.11.0 at `http://openwebui-host.local:3020`:

- STT: **works** (`POST /api/v1/audio/transcriptions`, Whisper local, model `base`).
- Chat: **works** (`POST /api/chat/completions`, e.g. `ministral-3b-2512`).
- TTS `POST /api/v1/audio/speech`:
  - With no TTS engine configured → **404**.
  - With engine "MistralAI", model `tts-1`, voice `alloy` (the defaults,
    which are **OpenAI** values) → **400** propagated from Mistral:
    `External: 400, url=https://api.mistral.ai/v1/audio/speech`.
  - Same 400 with `model: voxtral-mini-tts-latest` in the request body.

## Why: Mistral's TTS is voice cloning, not preset voices

Mistral's speech API (`api.mistral.ai/v1/audio/speech`) expects:

- `model`: e.g. `voxtral-mini-tts-2603` (aliases like `voxtral-mini-tts-latest`
  also exist. The instance's model list showed `voxtral-mini-tts-2603`,
  `voxtral-mini-tts-latest`, `voxtral-mini-tts-3-0`)
- **`voice_id`**: the ID of a voice created in the Mistral console
  (Studio → Audio → Voices) by uploading a ~10–20 s reference clip. A one-off
  `ref_audio` (base64 clip) per request is also possible.
- There is **no built-in voice catalog** like OpenAI's `alloy`/`echo`. The
  OpenWebUI defaults `tts-1`/`alloy` are rejected.
- Response per the SDK: base64 `audio_data` (JSON), formats mp3/wav/pcm/flac/opus.
- The API includes content moderation (403 on disallowed text).

## To make Mistral TTS work through OpenWebUI (future option)

1. A voice is created at console.mistral.ai (Studio → Voices). A reference
   clip with the needed rights is uploaded. Its `voice_id` is obtained.
2. OpenWebUI Admin → Settings → Audio → TTS (engine MistralAI):
   - TTS Model: `voxtral-mini-tts-2603`
   - TTS Voice: the `voice_id`
3. Verification is done with the speaker icon on a chat message.
4. Then `tts.engine: "openwebui"` is set in the speaker's config.yaml. Piper
   stays as the automatic fallback.

Caveat: not verified end-to-end. It is untested whether OpenWebUI 0.11's
MistralAI TTS engine maps its "voice" field to `voice_id` and decodes the
base64 `audio_data` response. It must be tested before it is relied on.

## Why the built-in OpenWebUI TTS is not an option

The "Transformers.js" engine is actually **SpeechT5** (Python transformers).
It is broken on current images. It loads speaker embeddings via
`load_dataset('Matthijs/cmu-arctic-xvectors')`. Modern `datasets` versions
dropped support for script-based datasets
(`RuntimeError: Dataset scripts are no longer supported`). This was verified
against OpenWebUI's main branch on GitHub (still present as of 2026-10-26).
Upgrading to 0.12.x does not fix it.

## Current setup (chosen 2026-11-01)

TTS is **Kokoro-FastAPI on the GPU box** (`tts.engine: "openai"` in
config.yaml. Deployment is in `docs/kokoro-tts-deploy.md`). The voice was
judged clearly better than piper in blind listening. First audio is ~0.16 s
with the client-side sentence pipeline. **Local piper on the Pi is the
automatic fallback** (`tts.fallback: "piper"`). The speaker keeps talking when
the GPU box or the network is down.

The OpenWebUI TTS path remains a config option (`tts.engine: "openwebui"`). It
needs a working server-side engine (see above for why the built-in one is
broken).

### Piper provenance (updated for 64-bit)

The Pi now runs 64-bit Raspberry Pi OS, so piper comes from the maintained
`piper-tts` wheel (OHF piper1-gpl, v1.8.0) via `uv sync`. No frozen 2023 binary
is used anymore. On the old 32-bit bullseye system, the only piper that ever
shipped an armv7 binary was the archived `rhasspy/piper` repo (Nov 2023). The
maintained successor publishes no armv7 wheels (same root cause as
openwakeword's missing armv7 onnxruntime — both were reasons for the 64-bit
migration). `scripts/setup-pi-tts.sh` still downloads a standalone piper
binary + the voice model for manual testing. The daemon itself uses the
wheel's Python API. Voices come from the maintained `rhasspy/piper-voices` HF
dataset.
