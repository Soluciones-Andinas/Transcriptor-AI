# VRAM budget — Capa 3 rig smoke (Task 7.3)

> **Spec**: SPEC-capa3-pipeline-v1, AC-14 — `compute_type=int8_float16` debe
> mantener el peak de VRAM **≤ 7,500 MB** durante una corrida real del
> pipeline (deja 0.5 GB de headroom sobre los 8 GB de la RTX 4060 Ti del
> rig — D-001).
>
> **Estado**: este archivo es un **template**. Los campos están vacíos para
> que el operador los rellene con las mediciones reales en el rig. La
> implementación de Capa 3 (Batches 1-7) está cerrada por el lado del
> código y los tests; lo único que queda para cerrar AC-14 es ejecutar
> este procedimiento en el rig y guardar los números.

---

## 1. Pre-condiciones

Marcar cuando estén verificadas:

- [ ] Rig accesible vía SSH/Tailscale, NVIDIA driver activo (`nvidia-smi` sin error).
- [ ] Container build local actualizado: `docker compose build --no-cache`
      desde la branch `feat/capa3-pipeline` ya en master (o sobre el commit
      del Capa 3 close).
- [ ] `docker compose up -d` arranca el stack (Postgres + transcription-api).
- [ ] `GET /health` devuelve `gpu_available: true`, `models.whisper="ready"`,
      `models.pyannote="ready"`. Si alguno está en `error`, abortar y revisar
      `docker compose logs transcription-api` antes de seguir.
- [ ] Bearer dev seedeado en `mcp_bearers` (capa 2 del wiki documenta cómo;
      `POST /auth/regenerate-mcp-token` es la vía oficial post-login web).
- [ ] Audio fixture preparado: un mp3/mp4 de **3-5 minutos** (preferiblemente
      una reunión real de Sandinas para validar también WER en español
      rioplatense, AC criterio extendido).

---

## 2. Procedimiento

### 2.1. Iniciar la captura de VRAM

En la sesión SSH del rig, antes de cualquier POST:

```bash
nvidia-smi -l 1 --query-gpu=memory.used,memory.free,timestamp --format=csv > /tmp/vram.log &
echo "vram_logger_pid=$!"
```

`-l 1` muestrea cada 1 segundo. Anotar el PID para detener la captura
después.

### 2.2. Disparar la primera transcripción (cache miss)

Desde la misma máquina (o cualquiera con acceso al puerto 8000 del rig):

```bash
time curl -X POST http://<RIG_HOST>:8000/api/transcriptions \
  -H "Authorization: Bearer <DEV_BEARER>" \
  -F file=@meeting.mp3 \
  -F language=es \
  -F num_speakers=2 \
  | tee /tmp/transcription_first.json
```

Capturar la duración total (`real` de `time`) y verificar que el cuerpo
tenga `metadata.cache_hit: false`.

### 2.3. Disparar la segunda transcripción (cache hit ALT-1)

Inmediatamente después, con el MISMO archivo y el MISMO bearer:

```bash
time curl -X POST http://<RIG_HOST>:8000/api/transcriptions \
  -H "Authorization: Bearer <DEV_BEARER>" \
  -F file=@meeting.mp3 \
  -F language=es \
  | tee /tmp/transcription_second.json
```

Verificar `metadata.cache_hit: true` y latencia **<500 ms** (la spec lo
documenta como criterio empírico de cache hit).

### 2.4. Detener la captura

```bash
kill ${vram_logger_pid}
tail -50 /tmp/vram.log
```

Buscar el peak de `memory.used` durante la primera corrida.

---

## 3. Mediciones (a llenar)

| Campo | Valor | Qué mide |
|---|---|---|
| **VRAM peak (MB)** | _________ | Pico de `memory.used` durante la primera corrida (cache miss). Criterio AC-14: ≤ 7,500 MB. |
| **Latencia primera corrida (s)** | _________ | `real` de `time curl ...` para el POST cache miss. Comparar contra duración del audio (target: ratio < 0.2x). |
| **Latencia segunda corrida (ms)** | _________ | `real` de `time curl ...` para el POST cache hit. Target: < 500 ms. |
| **`cache_hit` segunda corrida** | _________ | `true` esperado. Si `false`, el cache no se está sirviendo — investigar antes de rendir. |
| **WER subjetivo (cualitativo)** | _________ | Comparar la transcript con la audio de oído. Categorías: "publicable", "aceptable con edición menor", "necesita re-listening". Si "necesita re-listening" → blocker para ADR-001. |
| **Duración del audio fixture (s)** | _________ | Para sanity-check de la duración total del pipeline (latencia / duración debería estar entre 0.1x y 0.3x con `int8_float16` + RTX 4060 Ti). |

---

## 4. Aceptación

**AC-14 pasa si**:

- VRAM peak ≤ 7,500 MB.
- Latencia primera corrida < 0.3x duración del audio.
- Cache hit en segunda corrida con latencia < 500 ms.
- WER subjetivo "publicable" o "aceptable con edición menor".

**Si VRAM peak supera 7,500 MB**, fallback documentado:

1. Bajar `WHISPER_BATCH_SIZE` de `8` a `4` en `.env`.
2. `docker compose up -d transcription-api` para reiniciar con el nuevo
   batch size.
3. Re-correr el procedimiento desde §2.1.

Si tras el fallback el peak sigue > 7,500 MB, escalar al equipo: revisar
si pyannote 3.1 está cargando algún modelo extra no anticipado, y
considerar `WHISPER_DEVICE=cuda --compute-type int8` (sin float16) como
último recurso (degrada calidad pero ahorra ~1-2 GB).

**Si WER subjetivo es "necesita re-listening"**, documentar ejemplos
concretos (timestamps + transcript vs audio real) y abrir un drift
contra ADR-001 — la decisión de WhisperX large-v3 con int8_float16 puede
tener que revisarse contra Canary o large-v3-turbo (Opciones B/D del
ADR).

---

## 5. Sign-off

| Campo | Valor |
|---|---|
| Operador | _________ |
| Fecha | _________ |
| Branch / commit | _________ |
| Resultado | [ ] AC-14 pasa  [ ] AC-14 falla → fallback aplicado  [ ] AC-14 falla → escalar |

---

> **Post-cierre**: cuando AC-14 pase, marcar el checkbox del plan
> `2026-05-05-capa3-pipeline-plan.md` como `[x]` y agregar una nota al
> drift log si el fallback de `WHISPER_BATCH_SIZE=4` quedó en producción
> (queda como D-XXX entry: spec asumía batch_size=8 default, prod corre
> con 4 — minor).
