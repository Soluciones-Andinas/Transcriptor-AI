"""Audio pipeline (Capa 3) — STT, diarization, merge, orchestrate, cache, cleanup.

Spec: SPEC-capa3-pipeline-v1.

The package is implemented in batches; Batch 1 ships only the model loaders
(`stt.load_whisper_model`, `diarize.load_pyannote_pipeline`) used by the
lifespan to populate `app.state.{whisper,pyannote}_*`.

Heavy ML dependencies (`torch`, `whisperx`, `pyannote.audio`) are imported
lazily inside the loader functions, never at module top, so this package is
importable on CPU-only machines without the `[pipeline]` extras installed.
This is a deliberate consequence of D-008 (the dev/CI machines are CPU-only)
and lets the unit suite run via mock patching without GPU or HF network.
"""
