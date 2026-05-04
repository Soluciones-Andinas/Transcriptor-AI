# Plan de implementación — `transcription-api`

**Stack elegido (Fase 1 / MVP)**: WhisperX (Whisper large-v3 + pyannote 3.1) + FastAPI + Docker
**Plataforma objetivo**: rig intranet con GPU 16 GB VRAM, Linux preferido
**Tiempo estimado a MVP funcional**: 3-5 días de trabajo

## Arquitectura

```
┌─────────────────────────────────────────────────────────┐
│  Cliente (cualquier máquina en intranet)                │
│  $ curl -F file=@reunion.mp4 http://rig:8000/transcribe │
└──────────────────────┬──────────────────────────────────┘
                       │ HTTP multipart
                       ▼
┌─────────────────────────────────────────────────────────┐
│  Rig (16 GB VRAM, Linux)                                │
│  ┌───────────────────────────────────────────────────┐  │
│  │  FastAPI (uvicorn :8000)                          │  │
│  │  - POST /transcribe  (multipart upload)           │  │
│  │  - GET  /health                                   │  │
│  │  - GET  /docs        (Swagger)                    │  │
│  └────────────────────┬──────────────────────────────┘  │
│                       │                                  │
│  ┌────────────────────▼──────────────────────────────┐  │
│  │  Pipeline (síncrono)                              │  │
│  │  1. ffmpeg: MP4/MP3 → WAV mono 16 kHz             │  │
│  │  2. WhisperX large-v3 → transcript + timestamps   │  │
│  │  3. pyannote 3.1 → diarización                    │  │
│  │  4. Merge → JSON con segments + speakers          │  │
│  └────────────────────┬──────────────────────────────┘  │
│                       │                                  │
│  ┌────────────────────▼──────────────────────────────┐  │
│  │  Response: JSON con transcript diarizado          │  │
│  └───────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

## API contract

### `POST /transcribe`

**Request** (multipart/form-data):
- `file`: archivo de audio/video (mp4, mp3, wav, m4a, flac)
- `min_speakers` (opcional, int, default 1): pista para diarización
- `max_speakers` (opcional, int, default 8): pista para diarización
- `language` (opcional, string, default "es"): forzar idioma

**Response** (application/json):
```json
{
  "duration_seconds": 3247.5,
  "language": "es",
  "num_speakers": 3,
  "segments": [
    {
      "start": 0.0,
      "end": 4.2,
      "speaker": "SPEAKER_00",
      "text": "Buenas, gracias por venir a la reunión.",
      "words": [
        {"word": "Buenas", "start": 0.12, "end": 0.58, "speaker": "SPEAKER_00"},
        {"word": "gracias", "start": 0.71, "end": 1.10, "speaker": "SPEAKER_00"}
      ]
    }
  ],
  "metadata": {
    "model": "whisperx-large-v3",
    "diarizer": "pyannote/speaker-diarization-3.1",
    "processing_seconds": 187.4,
    "rtf": 17.3
  }
}
```

**Errores**:
- `400`: archivo inválido / formato no soportado
- `413`: archivo > 500 MB (configurable)
- `500`: error en pipeline (con detalle del paso fallido)
- `503`: servicio ocupado (si en futuro se agrega cola)

### `GET /health`

Response:
```json
{"status": "ok", "gpu_available": true, "vram_free_mb": 13800}
```

## Estructura del proyecto

```
transcription-api/
├── README.md                       # Quickstart
├── docker-compose.yml              # Servicio único con GPU pass-through
├── Dockerfile                      # CUDA + Python + dependencias
├── pyproject.toml                  # Dependencias + tooling
├── .env.example                    # HF_TOKEN, DATA_DIR, etc.
├── .gitignore
├── docs/
│   ├── INVESTIGACION.md            # ← ya creado
│   ├── PLAN.md                     # ← este archivo
│   ├── DECISIONES.md               # ADRs
│   ├── DEPLOYMENT.md               # cómo poner el servicio en el rig
│   └── PROMPTS_MINUTAS.md          # templates Cowork4Teams (Fase 2 fuera de la API)
├── src/
│   └── transcription_api/
│       ├── __init__.py
│       ├── main.py                 # FastAPI app + lifespan
│       ├── api/
│       │   ├── __init__.py
│       │   ├── routes.py           # POST /transcribe, GET /health
│       │   └── schemas.py          # pydantic models de request/response
│       ├── pipeline/
│       │   ├── __init__.py
│       │   ├── audio.py            # MP4/MP3 → WAV 16 kHz mono via ffmpeg
│       │   ├── transcribe.py       # WhisperX wrapper
│       │   ├── diarize.py          # pyannote wrapper
│       │   └── merge.py            # combinar transcript + speakers
│       └── config.py               # settings (HF_TOKEN, model paths)
├── scripts/
│   ├── download_models.sh          # Pre-descarga modelos al disco
│   ├── benchmark.py                # Mide WER/DER sobre audio de referencia
│   └── smoke_test.sh               # Test E2E con audio sample
├── tests/
│   ├── unit/
│   │   ├── test_audio.py
│   │   └── test_merge.py
│   └── integration/
│       └── test_pipeline.py
└── examples/
    ├── client_curl.sh
    ├── client_python.py
    └── sample_response.json
```

## Fases de implementación

### Fase 0 — Preparación (½ día)

- [ ] Verificar drivers NVIDIA en el rig (`nvidia-smi`, CUDA ≥ 12.1)
- [ ] Instalar Docker + nvidia-container-toolkit
- [ ] Crear cuenta HuggingFace + generar token
- [ ] Aceptar términos de uso de modelo `pyannote/speaker-diarization-3.1` en HF (requiere aprobación humana, puede tardar minutos a horas)
- [ ] Reservar puerto 8000 en el firewall de la intranet

### Fase 1 — Skeleton funcional (1 día)

- [ ] Crear `pyproject.toml` con dependencias: `whisperx`, `fastapi`, `uvicorn`, `python-multipart`, `pydantic-settings`
- [ ] Implementar `src/transcription_api/main.py` con FastAPI mínimo
- [ ] Endpoint `GET /health` con check de GPU
- [ ] Endpoint `POST /transcribe` con upload + respuesta hardcoded (smoke test sin modelos)
- [ ] Test unitario de schemas
- [ ] `Dockerfile` base sobre `nvidia/cuda:12.1-cudnn8-runtime-ubuntu22.04`

### Fase 2 — Pipeline real (1-2 días)

- [ ] `pipeline/audio.py`: extracción de audio con ffmpeg (subprocess), normalización a WAV mono 16 kHz
- [ ] `pipeline/transcribe.py`: cargar `whisperx.load_model("large-v3", device="cuda", compute_type="float16")`, exponer `transcribe(wav_path) -> dict`
- [ ] `pipeline/diarize.py`: cargar `pyannote.audio` con HF token, exponer `diarize(wav_path) -> list`
- [ ] `pipeline/merge.py`: combinar segmentos transcript con speaker labels usando `whisperx.assign_word_speakers`
- [ ] Conectar el pipeline al endpoint `POST /transcribe`
- [ ] Test integración con audio sample (~1 min) acoplado en `tests/fixtures/`

### Fase 3 — Productivización (1 día)

- [ ] Lifespan event en FastAPI: cargar modelos UNA VEZ al startup (no por request) — crítico para latencia
- [ ] Logging estructurado con duración por etapa (audio extract, STT, diarize, merge)
- [ ] Manejo de archivos temporales (tempdir + cleanup garantizado en `finally`)
- [ ] Validación de tamaño y formato del upload
- [ ] CORS configurado para llamadas desde apps internas
- [ ] `docker-compose.yml` con GPU pass-through y volumen persistente para modelos
- [ ] `scripts/download_models.sh`: pre-descarga de Whisper large-v3, alignment ES, pyannote 3.1
- [ ] `scripts/smoke_test.sh`: test end-to-end con MP4 real

### Fase 4 — Validación (½ día)

- [ ] Procesar 3-5 reuniones internas reales del equipo
- [ ] Calcular WER manual sobre 5 minutos de cada reunión (gold standard transcrito a mano)
- [ ] Calcular DER aproximada (revisar manual los cambios de hablante)
- [ ] Documentar resultados en `docs/BENCHMARK_INICIAL.md`
- [ ] Si WER > 8 % o DER > 25 % → escalar a Fase 5

### Fase 5 — Mejora (opcional, si Fase 4 lo justifica)

- [ ] Probar Canary-1B-v2 + pyannote 3.1 en el mismo audio de validación
- [ ] Comparar WER cabeza a cabeza
- [ ] Decidir migración o mantener WhisperX

## Stack de dependencias (versión inicial)

`pyproject.toml`:
```toml
[project]
name = "transcription-api"
version = "0.1.0"
requires-python = ">=3.10,<3.12"
dependencies = [
    "whisperx>=3.8.5",
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.32.0",
    "python-multipart>=0.0.12",
    "pydantic>=2.9.0",
    "pydantic-settings>=2.6.0",
    "ffmpeg-python>=0.2.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.3.0", "pytest-asyncio>=0.24.0", "httpx>=0.27.0", "ruff>=0.7.0"]
```

> **Nota CUDA**: WhisperX requiere PyTorch + CUDA toolkit instalados antes de pip install. Manejar en Dockerfile, no en pyproject.toml.

## Riesgos identificados

| Riesgo | Probabilidad | Impacto | Mitigación |
|---|---|---|---|
| HF no aprueba acceso a pyannote 3.1 a tiempo | Baja | Alto | Aceptar términos día 1 de la Fase 0 |
| WER en español rioplatense > esperado | Media | Medio | Fase 4 valida con audio real; Fase 5 cambia a Canary |
| ffmpeg falla con codecs raros de Teams | Baja | Bajo | Usar `ffmpeg -err_detect ignore_err` y testear con MP4 reales |
| Memoria VRAM no alcanza con large-v3 + pyannote | Baja | Medio | Bajar a `compute_type=int8` o usar large-v2 |
| Latencia por reunión > 15 min | Baja | Bajo | Aceptable para batch; si crece, agregar cola con Redis después |
| Concurrencia (2 requests simultáneos) crashea GPU | Alta sin manejo | Alto | Lock global o cola con max_workers=1 desde Fase 3 |
| Audio MP4 con 2+ tracks de audio | Baja | Medio | ffmpeg `-map 0:a:0` para tomar solo el primer track |

## Criterios de éxito del MVP

- [ ] API responde en intranet a un POST con archivo MP4 de 1 h en menos de 12 min
- [ ] Output JSON con segmentos + speakers + timestamps por palabra
- [ ] WER en español rioplatense ≤ 8 % sobre audio limpio
- [ ] DER ≤ 25 % en reuniones de 2-4 hablantes
- [ ] Servicio se reinicia en <30 s después de reboot del rig
- [ ] Documentación de deployment clara para que otra persona lo levante

## Out of scope (documentar para evitar scope creep)

- Generación de minutas → se hace manual con Cowork4Teams (HU separada)
- Live transcription → no requerido para captura de requerimientos
- Webhooks / callbacks asíncronos → respuesta síncrona alcanza por ahora
- UI web → el endpoint Swagger en `/docs` es suficiente para uso interno
- Autenticación → la red es intranet privada (re-evaluar si se expone a VPN externa)
- Almacenamiento persistente de transcripciones → cliente decide qué hacer con la respuesta
- Soporte multi-GPU → 1 GPU es suficiente al volumen actual

## Próximos pasos inmediatos

1. Validar acceso al rig (¿quién administra? ¿SSH? ¿Docker ya instalado?)
2. Generar HF token y aceptar pyannote 3.1
3. Crear el `Dockerfile` y `docker-compose.yml` base
4. Levantar skeleton FastAPI con `/health` funcionando
5. Implementar pipeline en orden: audio → transcribe → diarize → merge
