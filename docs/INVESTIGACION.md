# Investigación — Transcripción + Diarización en Español Self-Hosted

**Fecha**: 2026-04-30
**Autor**: Equipo Adopción IA — Soluciones Andinas
**HU origen**: [Investigar solución cloud para transcripción diarizada de reuniones (AssemblyAI)](https://app.notion.com/p/32e88ac892b681e5aaa2dd6e611bb2a8)

## Objetivo de la investigación

Evaluar el estado del arte en **transcripción + diarización open-source en español rioplatense**, ejecutable en un rig propio de 16 GB VRAM, expuesto vía API REST en intranet. Output esperado: archivo de audio (MP4/MP3) → transcripción diarizada con timestamps y precisión máxima.

Fuera de alcance: generación de minutas (se hará manualmente con Cowork4Teams + capturas de pantalla).

## Restricciones técnicas del rig

- **GPU**: 16 GB VRAM (asumida tipo RTX 4080 / 4060 Ti / similar)
- **Red**: intranet privada
- **Idioma**: español (rioplatense)
- **Modo**: batch (no se requiere live)
- **Audio**: archivos MP4 / MP3 / WAV de reuniones (~30 min – 2 h típico)

## Modelos STT evaluados

### 1. NVIDIA Parakeet-TDT-0.6B-v3 (agosto 2025)

| Aspecto | Valor |
|---|---|
| Parámetros | 600 M |
| Arquitectura | FastConformer-TDT |
| Idiomas soportados | 25 europeos (incluye español) |
| **Spanish WER (Fleurs)** | **3,45 %** |
| Spanish WER (MLS) | 4,39 % |
| Spanish WER (CoVoST) | 3,41 % |
| VRAM mínima | 2 GB |
| Audio máximo | 24 min (full attention) / 3 h (local attention) |
| Sample rate | 16 kHz mono |
| Timestamps | word + segment + char level |
| Puntuación / capitalización | Sí, automática |
| Licencia | CC-BY-4.0 (uso comercial OK) |

**Pros**: muy eficiente en VRAM, timestamps por palabra, modelo abierto liberado por NVIDIA reciente.
**Contras**: peor calidad en español que Canary-1B-v2.

**Repo / modelo**: https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3

### 2. NVIDIA Canary-1B-v2 (agosto 2025) — **Estado del arte en español**

| Aspecto | Valor |
|---|---|
| Parámetros | 1 B (978 M) |
| Arquitectura | FastConformer Encoder + Transformer Decoder |
| Idiomas soportados | 25 (incluye español) |
| **Spanish WER (Fleurs)** | **2,90 %** ← mejor de todos |
| Mean WER (HF Open ASR Leaderboard) | 7,15 % |
| RTFx | 749 (~10× más rápido que modelos comparables) |
| VRAM mínima | 6 GB |
| Soporta translation | Sí (X↔EN) |
| Timestamps | word + segment level |
| Licencia | CC-BY-4.0 |

**Pros**: state-of-the-art en español open-source, low VRAM, super rápido, traducción built-in.
**Contras**: no hay framework integrador listo con diarización; hay que construir glue code Canary + pyannote manualmente.

**Repo / modelo**: https://huggingface.co/nvidia/canary-1b-v2

### 3. OpenAI Whisper Large-v3 (vía WhisperX)

| Aspecto | Valor |
|---|---|
| Parámetros | 1,55 B |
| Idiomas soportados | 99+ |
| Spanish WER | ~3-6 % en audio limpio |
| VRAM (con WhisperX + diarización) | ~10-12 GB |
| Velocidad | 70× real-time con large-v2 batched |
| Licencia | MIT (Whisper), BSD-2-Clause (WhisperX) |

**Pros**: framework WhisperX maduro y battle-tested, alignment dedicado para español, integración pyannote nativa, gana 1° en Ego4d transcription challenge, comunidad gigante.
**Contras**: peor calidad en español que Canary, más lento.

**Repo**: https://github.com/m-bain/whisperX

### 4. IBM Granite Speech 3.3-8B

Mencionado por benchmarks recientes pero descartado: 8 B parámetros excede holgadamente nuestro presupuesto de VRAM y el ROI vs Canary 1B no se justifica.

## Modelos de diarización evaluados

### 1. pyannote/speaker-diarization-3.1 — **Recomendado**

| Aspecto | Valor |
|---|---|
| DER (benchmarks estándar) | 11–19 % |
| Idiomas | Multilingüe (probado en muchos) |
| VRAM | ~2 GB |
| Madurez | Muy alta, desde 2023 |
| Licencia | MIT |
| Requiere | HuggingFace token (gratis) |

**Pros**: maduro, multilingüe, comunidad enorme, integra perfectamente con WhisperX y se puede usar standalone.
**Contras**: DER ~11-19 % es aceptable pero no top; en reuniones con overlap fuerte puede confundir hablantes.

**Repo / modelo**: https://huggingface.co/pyannote/speaker-diarization-3.1

### 2. NVIDIA Streaming Sortformer (4spk-v2) — **Descartado para español**

| Aspecto | Valor |
|---|---|
| Parámetros | 117 M |
| Max speakers | 4 (degrada a partir de 5) |
| DER (CALLHOME 2-6 spk) | 10,15 % |
| **Foco entrenamiento** | **Inglés** |
| Performance no-EN | "Reduced performance" (textual de la doc) |
| Licencia | CC-BY-4.0 |

**Por qué se descarta**: la documentación oficial de NVIDIA explicita reduced performance en idiomas no-inglés. Para reuniones en español rioplatense la calidad esperada es inferior a pyannote 3.1. Reevaluar en 6-12 meses si aparece versión multilingüe.

**Repo / modelo**: https://huggingface.co/nvidia/diar_streaming_sortformer_4spk-v2

### 3. pyannote/speaker-diarization-community-1

Variante reciente con mejoras. Evaluar como upgrade incremental sobre 3.1 una vez tengamos el pipeline funcionando.

## Frameworks integradores evaluados

### 1. WhisperX (m-bain/whisperX) — **Recomendado para MVP**

| Aspecto | Valor |
|---|---|
| Versión | v3.8.5 (abril 2026) |
| Stack | faster-whisper + pyannote + wav2vec2 forced alignment |
| Idiomas alignment | en, fr, de, **es**, it (default), más vía HF |
| VRAM | <8 GB para large-v2; ~10-12 GB para large-v3 + diarización |
| Velocidad | 70× real-time con large-v2 |
| Licencia | BSD-2-Clause |

**Por qué arrancar acá**:
- Framework armado, instalación `pip install whisperx`
- Diarización integrada via pyannote
- Forced alignment dedicado en español → timestamps por palabra precisos
- Battle-tested en producción por miles de proyectos
- Comunidad activa, troubleshooting fácil

**Repo**: https://github.com/m-bain/whisperX

### 2. groxaxo/parakeet-tdt-0.6b-v3-fastapi-openai

Wrapper FastAPI sobre Parakeet con API OpenAI-compatible. Útil como **referencia de estructura del proyecto** pero **no tiene diarización**.

- Stack: FastAPI + ONNX Runtime + INT8 quantization (CPU-friendly)
- Endpoints: `/v1` (OpenAI-compat), `/docs` (Swagger)
- Docker compose listo
- License: MIT
- Stars: 159 (al momento de la investigación)

**Repo**: https://github.com/groxaxo/parakeet-tdt-0.6b-v3-fastapi-openai

## Comparativa final de stacks viables

| Stack | Spanish WER | DER | VRAM total | Madurez | Esfuerzo setup |
|---|---|---|---|---|---|
| **WhisperX (large-v3 + pyannote 3.1)** | 3-6 % | 11-19 % | ~12 GB | Muy alta | Bajo (1-2 días) |
| Canary-1B-v2 + pyannote 3.1 | **2,90 %** | 11-19 % | ~8 GB | Media | Alto (4-6 días, glue code) |
| Parakeet-TDT-0.6B-v3 + pyannote 3.1 | 3,45 % | 11-19 % | ~5 GB | Media | Alto (4-6 días, glue code) |
| Canary-1B-v2 + Sortformer | 2,90 % | Incierto en ES | ~7 GB | Baja | Muy alto + riesgo |

## Recomendación

### Fase 1 — MVP (semana 1-2)
**Stack**: WhisperX (Whisper large-v3 + pyannote 3.1) + FastAPI + Docker

Razones:
1. Framework integrado, time-to-working más bajo
2. Spanish WER 3-6 % es perfectamente usable para nuestra etapa de "captura de requerimientos manual via Cowork4Teams"
3. VRAM holgada en 16 GB (queda margen para concurrencia futura)
4. Comunidad gigante → troubleshooting acelerado
5. Forced alignment en español de fábrica → timestamps por palabra

### Fase 2 — Evaluación de upgrade (mes 2)
Una vez que el MVP corre estable, **medir con audio real propio**:
- WhisperX vs Canary-1B-v2 + pyannote 3.1
- Métrica: WER manual sobre 3-5 reuniones internas
- Si Canary mejora >2 puntos de WER en nuestro audio (no en benchmark sintético), justificar la migración

No invertir en Canary upfront porque la ganancia teórica (2,90 % vs 3-6 %) puede no replicarse en audio rioplatense con jerga técnica y ruido de fondo. Validar empíricamente antes de gastar 4-6 días en glue code.

### Fase 3 — Optimización (mes 3+)
Candidatos a evaluar cuando se justifique:
- pyannote community-1 (DER mejorado)
- Quantización INT8 para liberar VRAM
- Batch processing si volumen sube de ~20 reuniones/mes

## Decisiones documentadas

1. **No se elige Sortformer** por foco entrenamiento en inglés.
2. **No se elige Parakeet-TDT** porque Canary tiene mejor WER en español al mismo costo de glue code.
3. **No se reusa el repo `groxaxo/parakeet-fastapi`** porque no tiene diarización; se usará como referencia de estructura FastAPI.
4. **Se elige Whisper large-v3** sobre large-v2 por mejor calidad en español, asumiendo el costo extra de VRAM.
5. **Se difiere la decisión Canary vs WhisperX** a Fase 2 con datos reales propios.

## Referencias

- [NVIDIA Parakeet-TDT-0.6B-v3 (HuggingFace)](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3)
- [NVIDIA Canary-1B-v2 (HuggingFace)](https://huggingface.co/nvidia/canary-1b-v2)
- [NVIDIA Streaming Sortformer (HuggingFace)](https://huggingface.co/nvidia/diar_streaming_sortformer_4spk-v2)
- [NVIDIA NeMo Speech Models Blog](https://developer.nvidia.com/blog/nvidia-speech-ai-models-deliver-industry-leading-accuracy-and-performance/)
- [WhisperX (GitHub)](https://github.com/m-bain/whisperX)
- [pyannote-audio (GitHub)](https://github.com/pyannote/pyannote-audio)
- [pyannote/speaker-diarization-3.1 (HuggingFace)](https://huggingface.co/pyannote/speaker-diarization-3.1)
- [groxaxo/parakeet-tdt-0.6b-v3-fastapi-openai (GitHub)](https://github.com/groxaxo/parakeet-tdt-0.6b-v3-fastapi-openai)
- [Best Open Source STT 2026 — Northflank](https://northflank.com/blog/best-open-source-speech-to-text-stt-model-in-2026-benchmarks)
- [Best Speaker Diarization Models 2026 — BrassTranscripts](https://brasstranscripts.com/blog/speaker-diarization-models-comparison)
- [Choosing Whisper Variants — Modal Blog](https://modal.com/blog/choosing-whisper-variants)
