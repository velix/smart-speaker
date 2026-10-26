# TTS findings — OpenWebUI + Mistral Voxtral (2026-10-26)

Why the smart speaker currently uses on-Pi Piper TTS instead of the OpenWebUI
gateway, and how to switch back to Mistral later if wanted.

## What we found empirically

OpenWebUI 0.11.0 at `http://192.168.2.210:3020`:

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
  also exist; the instance's model list showed `voxtral-mini-tts-2603`,
  `voxtral-mini-tts-latest`, `voxtral-mini-tts-3-0`)
- **`voice_id`**: the ID of a voice you created in the Mistral console
  (Studio → Audio → Voices) by uploading a ~10–20 s reference clip —
  or a one-off `ref_audio` (base64 clip) per request.
- There is **no built-in voice catalog** like OpenAI's `alloy`/`echo`, so the
  OpenWebUI defaults `tts-1`/`alloy` are rejected.
- Response per the SDK: base64 `audio_data` (JSON), formats mp3/wav/pcm/flac/opus.
- The API includes content moderation (403 on disallowed text).

## To make Mistral TTS work through OpenWebUI (future option)

1. Create a voice at console.mistral.ai (Studio → Voices): upload a reference
   clip you have rights to → get its `voice_id`.
2. OpenWebUI Admin → Settings → Audio → TTS (engine MistralAI):
   - TTS Model: `voxtral-mini-tts-2603`
   - TTS Voice: the `voice_id`
3. Verify with the speaker icon on a chat message.
4. Then set `tts.engine: "openwebui"` in the speaker's config.yaml
   (Piper stays as automatic fallback).

Caveat: not verified end-to-end — untested whether OpenWebUI 0.11's MistralAI
TTS engine maps its "voice" field to `voice_id` and decodes the base64
`audio_data` response. Test before relying on it.

## Why the built-in OpenWebUI TTS is not an option

The "Transformers.js" engine is actually **SpeechT5** (Python transformers),
and it is broken on current images: it loads speaker embeddings via
`load_dataset('Matthijs/cmu-arctic-xvectors')`, but modern `datasets` versions
dropped support for script-based datasets
(`RuntimeError: Dataset scripts are no longer supported`). Verified against
OpenWebUI's main branch on GitHub (still present as of 2026-10-26) — upgrading
to 0.12.x does not fix it.

## Current setup (chosen)

TTS runs **locally on the Pi** with piper (see README "TTS" and
`scripts/setup-pi-tts.sh`): fast, offline, no voice cloning. The daemon's
`tts.engine` config selects the primary engine; the other engine is the
automatic fallback.

### Piper binary provenance (important)

The installed binary is from the **archived** `rhasspy/piper` repo (Nov 2023
release, `piper_linux_armv7l.tar.gz`) — the only piper release that ever
shipped a ready-made 32-bit armv7 binary. The maintained successor
`OHF-Voice/piper1-gpl` (v1.8.0, Sep 2026, Open Home Foundation) publishes
wheels only for aarch64/x86_64/macOS/Windows — **no armv7** — and building it
from source on the Pi needs onnxruntime, which has no armv7 builds either
(same root cause as openwakeword). Consequence: the Pi's piper is frozen at
the 2023 binary (self-contained, bundles onnxruntime 1.14.1; works at
RTF ~1 with the lessac-low voice, but receives no updates). Moving the Pi to
64-bit Raspberry Pi OS would unlock the maintained piper wheels *and*
openwakeword. Voices remain at the maintained `rhasspy/piper-voices`
HF dataset.
