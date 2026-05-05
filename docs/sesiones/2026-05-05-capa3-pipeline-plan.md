# Capa 3 — Plan TDD del audio pipeline

> **For Claude**: workflow TDD (RED→GREEN→REFACTOR) con commits atómicos.
>
> **Spec source**: `docs/sesiones/2026-05-05-capa3-pipeline-spec.md` (SPEC-capa3-pipeline-v1).
> **Goal**: pipeline end-to-end Whisper int8_float16 + pyannote 3.1 sobre REST con bearer auth, cache filesystem per-user, lock global, persistencia Postgres.
> **Tech stack**: Python 3.10, FastAPI, WhisperX, pyannote.audio, ffmpeg, asyncio, pytest + respx + testcontainers.
> **Test strategy**: 15 ACs distribuidos en 7 batches. Modelos Whisper/pyannote mockeados en unit tests; tests integration con `requires_gpu` corren solo con CUDA disponible (auto-skip CPU).

---

## Test mapping

| AC | Criterio (resumido) | Test |
|---|---|---|
| AC-1 | mp3 nuevo → 200 + JSON + row + cache | `test_post_transcription_processes_new_audio` |
| AC-2 | mismo user reupload → cache_hit (sin Whisper invoke) | `test_post_transcription_cache_hit_skips_pipeline` |
| AC-3 | sin bearer → 401 | `test_post_transcription_unauthenticated` |
| AC-4 | extensión inválida → 400 | `test_post_transcription_invalid_extension` |
| AC-5 | file > MAX_UPLOAD_MB → 413 | `test_post_transcription_too_large` |
| AC-6 | concurrent posts → 2do 503 | `test_orchestrator_global_lock_blocks_concurrent` |
| AC-7 | OOM → 500 + lock libre + DB sin row | `test_orchestrator_releases_lock_on_gpu_error` |
| AC-8 | GET ajeno → 404 | `test_get_transcription_cross_user_returns_404` |
| AC-9 | /health reporta models loading→ready | `test_health_reports_model_states` |
| AC-10 | cleanup borra TTL vencido | `test_cleanup_purges_expired_cache` |
| AC-11 | timeout pipeline → 504 | `test_orchestrator_pipeline_timeout` |
| AC-12 | mismo file 2 users → 2 corridas + 2 caches separados | `test_cache_is_per_user` |
| AC-13 | GET propio → 200 con shape | `test_get_transcription_returns_full_result` |
| AC-14 | VRAM peak ≤ 7.5 GB | `test_vram_budget_under_threshold` (smoke en rig) |
| AC-15 | HF_TOKEN inválido → service up + 503 verboso | `test_pyannote_load_failure_keeps_service_up` |

---

## Convenciones

- **Branch**: `feat/capa3-pipeline` (cortar desde `feat/capa2-auth-msentra` mergeado a master). Si Capa 2 no se mergea aún, cortar desde la misma branch.
- **Commit format**: `<type>(<scope>): SPEC-capa3 <AC-id> — <desc>`. Ejemplos:
  - `test(pipeline): SPEC-capa3 AC-1 — RED test for new audio path`
  - `feat(pipeline): SPEC-capa3 AC-1 — implement orchestrate happy path`
- **Markers**: tests que necesitan GPU usan `@pytest.mark.requires_gpu`; tests que necesitan Docker (testcontainers) usan `@pytest.mark.requires_docker`.
- **Stub vs real model**: en unit tests, mockear `whisperx.load_model()` y `Pipeline.from_pretrained()` con dummy callables que devuelven shape esperada. En integration tests con `requires_gpu`, cargar modelos reales una vez por sesión (fixture session-scoped).

---

## Batches

### Batch 1 — Foundation: Dockerfile + lifespan + models loaders + /health

**Goal**: imagen Docker con `[pipeline]` extras, lifespan que carga ambos modelos en VRAM, `/health` extendido reporta estado.

**Cubre ACs**: AC-9, AC-15.

#### Task 1.1 — Multi-stage Dockerfile con `[pipeline]` extras

**Files**:
- Modificar: `Dockerfile`
- Modificar: `docker-compose.yml` si hace falta volume nuevo para HF cache

**RED test** (`tests/integration/test_dockerfile_pipeline.py`):
```python
@pytest.mark.requires_docker
def test_docker_image_has_torch_and_whisperx():
    """SPEC-capa3 AC-9: imagen con [pipeline] extras instalados."""
    out = subprocess.check_output([
        "docker", "run", "--rm", "transcription-api:latest",
        "python", "-c", "import torch, whisperx, pyannote; print('ok')"
    ])
    assert b"ok" in out
```

**GREEN — Dockerfile multi-stage**:
```dockerfile
# stage 1: builder con compilers + dev libs
FROM nvidia/cuda:12.1.0-devel-ubuntu22.04 AS builder
RUN apt-get update && apt-get install -y python3.10 python3-pip ffmpeg
COPY pyproject.toml /app/
COPY src/ /app/src/
WORKDIR /app
RUN pip install --upgrade pip wheel && pip install ".[pipeline]"

# stage 2: runtime ligera (sin compilers)
FROM nvidia/cuda:12.1.0-runtime-ubuntu22.04
RUN apt-get update && apt-get install -y python3.10 ffmpeg curl && rm -rf /var/lib/apt/lists/*
COPY --from=builder /usr/local/lib/python3.10/dist-packages /usr/local/lib/python3.10/dist-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /app /app
WORKDIR /app
COPY entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
```

**Verify GREEN**:
```bash
docker build -t transcription-api:latest .
docker run --rm --gpus all transcription-api:latest python -c "import torch; print(torch.cuda.is_available())"
# Expected: True
```

**Commit**: `feat(docker): SPEC-capa3 AC-9 — multi-stage Dockerfile with [pipeline] extras`

---

#### Task 1.2 — `pipeline/stt.py::load_whisper_model()` + `pipeline/diarize.py::load_pyannote_pipeline()`

**Files**:
- Crear: `src/transcription_api/pipeline/__init__.py`
- Crear: `src/transcription_api/pipeline/stt.py`
- Crear: `src/transcription_api/pipeline/diarize.py`

**RED test** (`tests/unit/pipeline/test_model_loaders.py`):
```python
def test_load_whisper_returns_callable_transcribe():
    """SPEC-capa3 AC-9: loader devuelve un objeto con .transcribe()."""
    from transcription_api.pipeline.stt import load_whisper_model

    # Mock whisperx.load_model porque no hay GPU en CI
    with patch("transcription_api.pipeline.stt.whisperx.load_model") as m:
        m.return_value = MagicMock(transcribe=MagicMock())
        model = load_whisper_model("large-v3", "cuda", "int8_float16")
        assert hasattr(model, "transcribe")

def test_load_pyannote_raises_clear_error_on_invalid_token():
    """SPEC-capa3 AC-15: HF_TOKEN inválido → PyannoteLoadError con detail."""
    from transcription_api.pipeline.diarize import (
        PyannoteLoadError, load_pyannote_pipeline,
    )
    with patch("transcription_api.pipeline.diarize.Pipeline.from_pretrained") as m:
        m.side_effect = Exception("401 Client Error: Unauthorized")
        with pytest.raises(PyannoteLoadError) as exc:
            load_pyannote_pipeline("hf_invalid")
        assert exc.value.detail in {"hf_token_invalid", "hf_token_missing", "hf_terms_not_accepted"}
```

**GREEN**: implementar `load_whisper_model(model_size, device, compute_type)` y `load_pyannote_pipeline(hf_token)` con error classification.

**Commit**: `feat(pipeline): SPEC-capa3 AC-9 + AC-15 — model loaders with verbose errors`

---

#### Task 1.3 — Lifespan carga modelos + `/health` extendido

**Files**:
- Modificar: `src/transcription_api/main.py` (lifespan)
- Modificar: `src/transcription_api/api/health.py` (o donde viva /health)

**RED test** (`tests/integration/test_health_models.py`):
```python
async def test_health_reports_models_ready_after_lifespan():
    """SPEC-capa3 AC-9: post-lifespan /health.models.{whisper,pyannote} = ready."""
    # Patch loaders para que no requieran GPU/red
    with patch("transcription_api.pipeline.stt.load_whisper_model") as ms, \
         patch("transcription_api.pipeline.diarize.load_pyannote_pipeline") as md:
        ms.return_value = MagicMock()
        md.return_value = MagicMock()
        async with LifespanManager(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                r = await c.get("/health")
                body = r.json()
                assert body["models"]["whisper"] == "ready"
                assert body["models"]["pyannote"] == "ready"

async def test_health_reports_pyannote_error_when_load_fails():
    """SPEC-capa3 AC-15: pyannote load fail → service UP + /health refleja error."""
    with patch("transcription_api.pipeline.stt.load_whisper_model"), \
         patch("transcription_api.pipeline.diarize.load_pyannote_pipeline") as md:
        from transcription_api.pipeline.diarize import PyannoteLoadError
        md.side_effect = PyannoteLoadError("hf_token_invalid")
        async with LifespanManager(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                r = await c.get("/health")
                body = r.json()
                # status sigue ok (el service arranca verbose)
                assert body["models"]["pyannote"] == "error"
                assert body["models"].get("pyannote_detail") == "hf_token_invalid"
```

**GREEN**: en lifespan, llamar loaders dentro de try/except, guardar el modelo en `app.state.whisper_model` / `app.state.pyannote_pipeline` o setear `app.state.{whisper,pyannote}_status = "error"` con detail.

**Commit**: `feat(api): SPEC-capa3 AC-9 + AC-15 — lifespan loads models + /health states`

---

### Batch 2 — Audio normalize + cache filesystem (per-user)

**Goal**: ffmpeg → WAV 16kHz mono → SHA-256; cache reads/writes por `(user_id, audio_hash)`.

**Cubre ACs**: AC-2, AC-12, parte de AC-1, AC-4, AC-5.

#### Task 2.1 — `pipeline/normalize.py::normalize_audio()`

**RED test** (`tests/unit/pipeline/test_normalize.py`):
```python
def test_normalize_produces_wav_16khz_mono(tmp_path):
    """SPEC-capa3 AC-1: ffmpeg normaliza a wav 16kHz mono y devuelve sha256."""
    src = _make_test_mp3(tmp_path)  # helper que genera mp3 con sine wave
    from transcription_api.pipeline.normalize import normalize_audio

    out_path, audio_hash, duration = normalize_audio(src, tmp_path)
    assert out_path.suffix == ".wav"
    # Verificar 16kHz mono usando ffprobe
    info = subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", str(out_path)])
    assert b"sample_rate=16000" in info
    assert b"channels=1" in info
    assert len(audio_hash) == 64  # sha256 hex
    assert duration > 0

def test_normalize_rejects_unknown_format(tmp_path):
    """SPEC-capa3 AC-4: extensión no soportada → AudioFormatInvalid."""
    bad = tmp_path / "fake.exe"
    bad.write_bytes(b"MZ\x90\x00")
    from transcription_api.pipeline.normalize import (
        AudioFormatInvalid, normalize_audio,
    )
    with pytest.raises(AudioFormatInvalid):
        normalize_audio(bad, tmp_path)
```

**GREEN**: invocar ffmpeg subprocess; computar sha256 del wav resultante; validar magic bytes con whitelist (`mp4`, `mp3`, `m4a`, `wav`, `flac`).

**Commit**: `feat(pipeline): SPEC-capa3 AC-1 + AC-4 — normalize audio + format validation`

---

#### Task 2.2 — `pipeline/cache.py::CacheStore` (per-user FS)

**RED test** (`tests/unit/pipeline/test_cache.py`):
```python
def test_cache_writes_and_reads_per_user(tmp_path):
    """SPEC-capa3 AC-12: cache es per-user, mismo audio_hash en distinto user no comparte."""
    from transcription_api.pipeline.cache import CacheStore

    store = CacheStore(base_dir=tmp_path)
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    audio_hash = "deadbeef" * 8

    payload = {"text": "Reunión A", "segments": []}
    store.put(user_a, audio_hash, payload)

    # User A lee su cache; User B no lo ve.
    assert store.get(user_a, audio_hash) == payload
    assert store.get(user_b, audio_hash) is None

    # Paths distintos en filesystem
    path_a = tmp_path / str(user_a) / audio_hash / "result.json"
    path_b = tmp_path / str(user_b) / audio_hash / "result.json"
    assert path_a.exists()
    assert not path_b.exists()
```

**GREEN**: `CacheStore.get(user_id, audio_hash) → dict | None`, `.put(...)`, `.delete(...)`. Path: `<base_dir>/<user_id>/<audio_hash>/result.json`.

**Commit**: `feat(pipeline): SPEC-capa3 AC-12 — per-user filesystem cache (D-027)`

---

### Batch 3 — STT (WhisperX wrapper)

**Goal**: `transcribe(model, wav_path) → segments con timestamps por palabra`. Manejar OOM/CUDA errors.

**Cubre ACs**: parte de AC-1, AC-7, AC-14.

#### Task 3.1 — `stt.transcribe()` happy path

**RED**: con modelo mockeado que retorna shape canónico de WhisperX (`{"segments": [...], "language": "es"}`), `transcribe()` retorna list[Segment] con `start/end/text/words`.

**GREEN**: thin wrapper alrededor de `model.transcribe(audio_path, batch_size=...)`.

**Commit**: `feat(pipeline): SPEC-capa3 AC-1 — stt.transcribe wraps WhisperX inference`

---

#### Task 3.2 — Mapping de errores GPU

**RED**: cuando `model.transcribe(...)` raises `torch.cuda.OutOfMemoryError`, `RuntimeError("CUDA error")`, o `RuntimeError("CUBLAS error")`, `stt.transcribe()` debe raise `GPUError(detail=...)`.

```python
def test_stt_maps_cuda_oom_to_gpu_error():
    from transcription_api.pipeline.stt import GPUError, transcribe
    model = MagicMock()
    model.transcribe.side_effect = torch.cuda.OutOfMemoryError("CUDA OOM")
    with pytest.raises(GPUError) as exc:
        transcribe(model, "/tmp/x.wav")
    assert "oom" in str(exc.value).lower()
```

**GREEN**: try/except con typed exception remapping.

**Commit**: `feat(pipeline): SPEC-capa3 AC-7 — stt maps CUDA errors to typed GPUError`

---

### Batch 4 — Diarize (pyannote) + Merge

**Goal**: `diarize(pipeline, wav_path, num_speakers=None)` → list[(start, end, speaker)]. `merge(transcript_segments, diarization_segments)` → segments con palabra-a-hablante.

**Cubre ACs**: AC-1 (full path), ALT-3 (max_speakers cap).

#### Task 4.1 — `diarize.diarize()`

**RED**: con pipeline mockeado que retorna RTTM-shape, retorna list de tuples `(start_sec, end_sec, speaker_label)`.

**GREEN**: thin wrapper alrededor de `pipeline(wav_path, num_speakers=...)`.

**Commit**: `feat(pipeline): SPEC-capa3 AC-1 — diarize wraps pyannote pipeline`

---

#### Task 4.2 — `merge.assign_speakers_to_words()`

**RED**: con transcript de 3 palabras y 2 segmentos diarizados, cada palabra debe quedar asignada al speaker cuyo segmento contiene su `(start+end)/2`.

```python
def test_merge_assigns_each_word_to_overlapping_speaker():
    transcript = [
        {"start": 0.0, "end": 1.0, "text": "Hola", "words": [
            {"word": "Hola", "start": 0.0, "end": 1.0}
        ]},
        {"start": 1.0, "end": 2.0, "text": "soy yo", "words": [
            {"word": "soy", "start": 1.0, "end": 1.5},
            {"word": "yo", "start": 1.5, "end": 2.0},
        ]},
    ]
    diarization = [(0.0, 0.9, "SPEAKER_00"), (1.0, 2.0, "SPEAKER_01")]

    out = assign_speakers_to_words(transcript, diarization)
    assert out[0]["words"][0]["speaker"] == "SPEAKER_00"
    assert out[1]["words"][0]["speaker"] == "SPEAKER_01"
    assert out[1]["words"][1]["speaker"] == "SPEAKER_01"
```

**GREEN**: por cada palabra, encontrar el segmento de diarization que contiene su mid-point. Para palabras en gaps, asignar al speaker más cercano.

**Commit**: `feat(pipeline): SPEC-capa3 AC-1 — merge word-to-speaker assignment`

---

### Batch 5 — Orchestrator + global lock

**Goal**: `orchestrate(user, file_path, language, num_speakers, ...)` corre normalize → cache lookup → STT → diarize → merge → persist con asyncio.Lock global y error mapping.

**Cubre ACs**: AC-6, AC-7, AC-11, parte de AC-1.

#### Task 5.1 — Lock global (singleton)

**RED test** (`tests/integration/pipeline/test_orchestrator_lock.py`):
```python
async def test_orchestrator_lock_serializes_two_jobs():
    """SPEC-capa3 AC-6: dos orchestrate concurrentes → uno corre, el otro espera o falla."""
    from transcription_api.pipeline.orchestrator import orchestrate, GPUBusy

    # Mock model.transcribe con delay artificial
    async def slow_pipeline():
        await asyncio.sleep(2)
        return MOCK_RESULT

    with patch("transcription_api.pipeline.orchestrator._run_pipeline", slow_pipeline):
        # primer task lanzado, ocupa el lock
        t1 = asyncio.create_task(orchestrate(user_a, ...))
        await asyncio.sleep(0.1)  # asegura que t1 toma el lock primero
        # segundo task con LOCK_WAIT_SECONDS=0.5 → debe raisear GPUBusy
        with pytest.raises(GPUBusy):
            await orchestrate(user_b, ..., lock_timeout=0.5)
        await t1
```

**GREEN**: module-level `asyncio.Lock`. `orchestrate()` hace `await asyncio.wait_for(lock.acquire(), timeout=settings.lock_wait_seconds)`; on timeout raise `GPUBusy`.

**Commit**: `feat(pipeline): SPEC-capa3 AC-6 — orchestrator global lock + GPUBusy timeout`

---

#### Task 5.2 — Lock se libera en error path

**RED**:
```python
async def test_lock_released_on_gpu_error():
    """SPEC-capa3 AC-7: si el pipeline raisea GPUError, el lock se libera."""
    with patch("transcription_api.pipeline.orchestrator._run_pipeline") as m:
        m.side_effect = GPUError("CUDA OOM")
        with pytest.raises(GPUError):
            await orchestrate(user_a, ...)
        # El lock debe estar libre ahora
        assert not _orchestrator_lock.locked()
```

**GREEN**: `try/finally` alrededor del happy path libera el lock siempre.

**Commit**: `feat(pipeline): SPEC-capa3 AC-7 — orchestrator releases lock in finally`

---

#### Task 5.3 — Pipeline timeout (AC-11)

**RED**: `orchestrate()` con `timeout_seconds=0.5` y pipeline que tarda 2 s → raise `PipelineTimeout`.

**GREEN**: `asyncio.wait_for(_run_pipeline(...), timeout=settings.pipeline_timeout_seconds)`.

**Commit**: `feat(pipeline): SPEC-capa3 AC-11 — orchestrator pipeline timeout`

---

#### Task 5.4 — Happy path completo + persist Postgres + cache

**RED**: con loaders mockeados, `orchestrate(user_a, mp3_path)` retorna dict con `transcription_id`, escribe row en DB con `user_id = user_a.id`, escribe cache filesystem en `<user_a.id>/<hash>/result.json`. Cleanup de upload temporal después.

**GREEN**: orquestar normalize → cache.get → si miss: stt+diarize+merge → cache.put → DB INSERT (with `bypass_scoping` durante INSERT para no auto-scopear el INSERT que arma el row del user).

**Commit**: `feat(pipeline): SPEC-capa3 AC-1 — orchestrator happy path + persist`

---

### Batch 6 — API endpoints (POST + GET)

**Goal**: `POST /api/transcriptions` (multipart + bearer + invocar orchestrator), `GET /api/transcriptions/{id}` (scoped).

**Cubre ACs**: AC-1, AC-3, AC-4, AC-5, AC-8, AC-13.

#### Task 6.1 — POST happy path + auth

**RED test** (`tests/integration/api/test_transcriptions.py`):
```python
@pytest.mark.requires_docker
async def test_post_transcription_with_valid_bearer(client, session):
    """SPEC-capa3 AC-1 + AC-3: bearer válido + mp3 → 200 + JSON con transcription_id."""
    user, bearer_pt = await _seed_bearer(session)
    mp3 = _make_test_mp3()

    with patch("transcription_api.pipeline.orchestrator._run_pipeline") as m:
        m.return_value = MOCK_RESULT
        r = await client.post(
            "/api/transcriptions",
            files={"file": ("meeting.mp3", mp3, "audio/mpeg")},
            data={"language": "es"},
            headers={"authorization": f"Bearer {bearer_pt}"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["audio_hash"]
    assert body["transcription_id"]
    assert body["language"] == "es"

async def test_post_transcription_unauthenticated_401(client):
    """SPEC-capa3 AC-3: sin bearer → 401."""
    r = await client.post("/api/transcriptions", files={"file": ("x.mp3", b"id3", "audio/mpeg")})
    assert r.status_code == 401
```

**GREEN**: `api/transcriptions.py` con FastAPI route + `Depends(get_current_user_mcp)` + invocar `orchestrate(...)`.

**Commit**: `feat(api): SPEC-capa3 AC-1 + AC-3 — POST /api/transcriptions with bearer auth`

---

#### Task 6.2 — Validaciones formato y tamaño (AC-4, AC-5)

**RED**:
- `test_post_rejects_exe_file` (extensión y magic bytes inválidos → 400)
- `test_post_rejects_oversize` (mock Content-Length > MAX_UPLOAD_MB → 413 antes de leer body)

**GREEN**: chequeo extensión + magic bytes + Content-Length header. Si Content-Length missing, leer en streaming con tope.

**Commit**: `feat(api): SPEC-capa3 AC-4 + AC-5 — input validation (format + size)`

---

#### Task 6.3 — GET happy path + cross-user 404 (AC-13, AC-8)

**RED**:
- `test_get_returns_full_result_for_owner` (user A obtiene su transcription).
- `test_get_returns_404_for_other_user` (user B con bearer de B intenta GET id de A → 404, NO 401 ni 403).

**GREEN**: `GET /api/transcriptions/{id}` con `Depends(get_current_user_mcp)` + query a `transcriptions` (el listener fail-closed inyecta `WHERE user_id = current_user.id` automáticamente; si no hay row, 404).

**Commit**: `feat(api): SPEC-capa3 AC-8 + AC-13 — GET /api/transcriptions/{id} scoped`

---

#### Task 6.4 — `MODELS_NOT_LOADED` propaga detail al 503 (AC-15)

**RED**:
```python
async def test_post_returns_503_when_pyannote_failed_to_load(client, session):
    user, pt = await _seed_bearer(session)
    # Forzar app.state.pyannote_status = "error" con detail
    app.state.pyannote_status = "error"
    app.state.pyannote_detail = "hf_token_invalid"
    r = await client.post(
        "/api/transcriptions", files={"file": ("x.mp3", b"...", "audio/mpeg")},
        headers={"authorization": f"Bearer {pt}"},
    )
    assert r.status_code == 503
    body = r.json()
    assert body["detail"]["error_code"] == "MODELS_NOT_LOADED"
    assert "hf_token" in body["detail"]["reason"]
```

**GREEN**: en POST, antes de invocar orchestrator, leer `app.state.{whisper,pyannote}_status`; si alguno es `"error"`, responder 503 con detail.

**Commit**: `feat(api): SPEC-capa3 AC-15 — MODELS_NOT_LOADED 503 with verbose detail`

---

### Batch 7 — Cleanup task + E2E rig smoke

**Goal**: cleanup TTL en lifespan + smoke E2E real en rig que cierra ADR-001.

**Cubre ACs**: AC-10, AC-14.

#### Task 7.1 — Cleanup task (asyncio task en lifespan)

**RED test** (`tests/integration/test_cleanup_task.py`):
```python
async def test_cleanup_purges_expired_entries(tmp_path):
    """SPEC-capa3 AC-10: cleanup borra entradas con mtime > TTL."""
    cache_dir = tmp_path / "cache"
    user_dir = cache_dir / str(uuid.uuid4()) / ("a" * 64)
    user_dir.mkdir(parents=True)
    stale = user_dir / "result.json"
    stale.write_text('{"text": "old"}')
    # Forzar mtime al pasado
    old = time.time() - 25 * 3600
    os.utime(stale, (old, old))

    from transcription_api.pipeline.cleanup import purge_expired
    n = await purge_expired(cache_dir, ttl_seconds=24 * 3600)
    assert n == 1
    assert not stale.exists()
```

**GREEN**: `purge_expired(base_dir, ttl_seconds)` itera por user → audio_hash → revisa mtime → unlink + rmdir vacíos.

**Commit**: `feat(pipeline): SPEC-capa3 AC-10 — cleanup task purges expired cache entries`

---

#### Task 7.2 — Cleanup task wired al lifespan

**RED**: con mock de `purge_expired`, verificar que se invoca cada `CACHE_CLEANUP_INTERVAL_SECONDS` durante el lifespan.

**GREEN**: en lifespan, `asyncio.create_task(_cleanup_loop())` que duerme + invoca `purge_expired`. Cancelar la task en shutdown.

**Commit**: `feat(api): SPEC-capa3 AC-10 — wire cleanup task to lifespan`

---

#### Task 7.3 — E2E smoke en rig con audio real

**No es test pytest**. Procedimiento manual + log de resultados:

1. En el rig: `git pull`, `docker compose build --no-cache`, `docker compose up -d`.
2. Validar `/health.gpu_available = true` y `/health.models = {whisper: "ready", pyannote: "ready"}`.
3. Subir un mp3/mp4 real de reunión Sandinas (3-5 min):
   ```bash
   curl -X POST http://localhost:8000/api/transcriptions \
     -H "Authorization: Bearer dev-bearer-please-rotate-before-prod" \
     -F file=@meeting.mp3 -F language=es | jq
   ```
4. Capturar:
   - Latencia total
   - VRAM peak via `nvidia-smi -l 1` durante la corrida (target: ≤ 7.5 GB)
   - WER subjetivo del español rioplatense (compare `text_content` contra una transcript humana de ~30 s del mismo audio)
   - `cache_hit: false` en el primer request, `cache_hit: true` en el segundo idéntico.
5. Documentar en `docs/sesiones/2026-05-05-capa3-vram-budget.md`:
   - VRAM peak medido
   - Latencia total
   - WER ground-truth subjetivo
   - Ajustes a `WHISPER_BATCH_SIZE` si OOM (default 8 → 4 si necesario)

**Commit (opcional)**: `docs(capa3): SPEC-capa3 AC-14 — VRAM budget measurements on rig`

---

## Traceability matrix

| Spec | Criterio | Test | Status |
|---|---|---|---|
| SPEC-capa3 | AC-1 | `test_post_transcription_with_valid_bearer` + orchestrator + normalize + stt + diarize + merge | [ ] |
| SPEC-capa3 | AC-2 | `test_cache_writes_and_reads_per_user` + `test_cache_returns_none_on_miss` (substrate). End-to-end `test_post_transcription_cache_hit_skips_pipeline` deferred to Batch 6. | [x] (substrate) / [ ] (E2E POST) |
| SPEC-capa3 | AC-3 | `test_post_transcription_unauthenticated_401` | [ ] |
| SPEC-capa3 | AC-4 | `test_normalize_rejects_extension_outside_whitelist` + `test_normalize_rejects_magic_bytes_mismatch`. API surface `test_post_rejects_exe_file` deferred to Batch 6. | [x] (normalize layer) / [ ] (POST 400) |
| SPEC-capa3 | AC-5 | `test_post_rejects_oversize` | [ ] |
| SPEC-capa3 | AC-6 | `test_orchestrator_lock_serializes_two_jobs` | [ ] |
| SPEC-capa3 | AC-7 | `test_lock_released_on_gpu_error` + `test_stt_maps_cuda_oom_to_gpu_error` | [ ] |
| SPEC-capa3 | AC-8 | `test_get_returns_404_for_other_user` | [ ] |
| SPEC-capa3 | AC-9 | `test_health_reports_models_ready_after_lifespan` + `test_load_whisper_returns_object_with_transcribe` | [x] |
| SPEC-capa3 | AC-10 | `test_cleanup_purges_expired_entries` | [ ] |
| SPEC-capa3 | AC-11 | `test_orchestrator_pipeline_timeout` | [ ] |
| SPEC-capa3 | AC-12 | `test_cache_writes_and_reads_per_user` + `test_cache_isolates_users_via_filesystem_path` | [x] |
| SPEC-capa3 | AC-13 | `test_get_returns_full_result_for_owner` | [ ] |
| SPEC-capa3 | AC-14 | E2E rig smoke (manual, registrar en `vram-budget.md`) | [ ] |
| SPEC-capa3 | AC-15 | `test_health_reports_pyannote_error_when_load_fails` + `test_load_pyannote_classifies_*` (4 tests) — 503 propagation deferred to Batch 6 (`test_post_returns_503_when_pyannote_failed_to_load`) | [x] (load failure surface) / [ ] (POST 503) |

---

## Definition of Done

- [ ] 14/15 ACs verdes en pytest (AC-14 es smoke manual)
- [ ] Imagen Docker pesa <12 GB final
- [ ] `/health` reporta `models = ready` y `gpu_available: true` en el rig
- [ ] Smoke E2E: mp3 real procesa <12 min/h y devuelve JSON coherente
- [ ] Smoke E2E: segundo upload del mismo file por mismo user → cache hit (latencia <500 ms)
- [ ] Smoke E2E: VRAM peak ≤ 7.5 GB documentado en `vram-budget.md`
- [ ] Drift D-026, D-027, D-028 anotados en `docs/sesiones/2026-05-05-wiki-drifts.md`
- [ ] Multi-agent review (igual que Capa 2) sobre código de Capa 3 antes de mergear

---

## Squash message template (para merge a master cuando todo esté verde)

```
feat(pipeline): SPEC-capa3 — audio transcription pipeline with bearer auth

End-to-end Whisper + pyannote pipeline over REST authenticated by MCP
bearer (Capa 2). Single-step multipart upload triggers normalize →
cache lookup → STT → diarize → merge → persist. Per-user filesystem
cache (D-027), global asyncio lock for GPU serialization, TTL cleanup
job in lifespan.

Implements:
- 15 ACs covering happy path, auth failures, format/size validation,
  concurrency, GPU errors, timeouts, cross-user isolation, model
  loading states, cleanup.
- 7 batches of TDD: foundation/Dockerfile, normalize+cache, STT,
  diarize+merge, orchestrator+lock, REST endpoints, cleanup+E2E.

Drifts:
- D-026: REST entry point instead of MCP tools (Capa 4 wraps).
- D-027: per-user cache (no global sharing for simplicity).
- D-028: lazy/verbose pyannote load failure (service stays up).

Tests: 14 pytest ACs + 1 E2E manual on rig with VRAM budget logged.
Spec: SPEC-capa3-pipeline-v1.
```
