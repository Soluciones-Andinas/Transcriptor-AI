# Capa 4 — Smoke E2E en el rig

> **Objetivo**: validar end-to-end el MCP server Capa 4 contra el rig real
> (NVIDIA RTX 4060 Ti 8 GB, Postgres 16, ffmpeg, modelos ya descargados)
> con un cliente Claude (Code o Desktop) conectado vía Streamable HTTP.
> Este checklist es la última puerta antes de mergear `feat/capa4-mcp` a
> master.
>
> **Spec**: SPEC-capa4-mcp-v1
> **AC cubiertos por este smoke**: AC-1, AC-2, AC-3, AC-4, AC-5, AC-7,
> AC-8, AC-10, AC-11, AC-12, AC-13, AC-14, AC-15, AC-16.
> **AC NO cubiertos** (son verificación local + tests integration):
> AC-6 (resource transcription://), AC-9 (lock contention sintética).
>
> **Operador**: Franco. Este agente no tiene acceso al rig.

---

## 1. Pre-flight (rig + base de datos)

| Paso | Comando | Resultado esperado | OK? |
|---|---|---|---|
| 1.1 | `git fetch && git checkout feat/capa4-mcp && git pull` | HEAD == último commit de Batch 6. | [ ] |
| 1.2 | `cat .env` | `PUBLIC_BASE_URL`, `DATABASE_URL`, `MCP_BEARER_PEPPER`, `JWT_SECRET`, `OAUTH_*`, `DATA_DIR` poblados (no defaults). | [ ] |
| 1.3 | `docker compose up -d db` (si Postgres en compose) o verificar service externo. | `pg_isready` retorna 0. | [ ] |
| 1.4 | `.venv/bin/alembic upgrade head` desde el repo root. | Output: `INFO  [alembic.runtime.migration] Running upgrade <prev> -> add_upload_bearer_hash` (o el revision id de Batch 0). | [ ] |
| 1.5 | `psql $DATABASE_URL -c "\d upload_sessions"` | Aparece la columna `upload_bearer_hash text NOT NULL`. (AC-15) | [ ] |
| 1.6 | `psql $DATABASE_URL -c "SELECT count(*) FROM upload_sessions WHERE upload_bearer_hash IS NULL;"` | `0`. La pre-flight de la migration ya lo garantizó pero verificamos. | [ ] |
| 1.7 | `docker compose up -d api` (o `uvicorn transcription_api.main:app ...`). | `/health` retorna `{"status":"ok","models":{"whisper":"ready","pyannote":"ready"}}`. | [ ] |

> **Si 1.4 falla con "rows pre-existing with NULL"**: STOP. La migration tiene defense pre-flight; significa que hay rows previos no contemplados (debería ser cero en producción). Reportar a `feat/capa4-mcp` antes de continuar.

---

## 2. Configurar el cliente Claude (Code o Desktop)

### 2.1 Generar dos bearers de test

> Necesitamos dos usuarios para validar el aislamiento cross-user (AC-3, AC-4, AC-5, AC-8, AC-11).

```bash
# Usuario A — dueño del flujo principal
psql $DATABASE_URL -c "INSERT INTO users (id, email, ...) VALUES (...) RETURNING id;"
# Generar bearer para A: corre el helper interno o el endpoint /auth/login + /auth/mcp-bearer
# Anotar el plaintext; el hash queda en mcp_bearers.

USER_A_BEARER=___                # plaintext devuelto por la generación
USER_A_ID=___                    # uuid del INSERT

# Usuario B — para asserts cross-user
USER_B_BEARER=___
USER_B_ID=___
```

### 2.2 `~/.claude/mcp.json` (para Claude Code) o equivalente Desktop

```json
{
  "mcpServers": {
    "transcription-api-rig": {
      "url": "http://<rig-host>/mcp",
      "headers": {
        "Authorization": "Bearer <USER_A_BEARER>"
      }
    }
  }
}
```

> Notas:
> - Streamable HTTP transport (ADR-011); no stdio, no SSE deprecated.
> - El `<rig-host>` debe ser alcanzable desde la máquina del operador (intranet OK; si HTTPS detrás de reverse proxy, usar `https://`).
> - Bearer plaintext en el config local es OK para smoke; en producción usar variables de entorno o secrets manager.

### 2.3 Reiniciar Claude Code y verificar conexión

| Paso | Acción | Resultado esperado | OK? |
|---|---|---|---|
| 2.3.1 | `/mcp list` (Claude Code) o equivalente | El server `transcription-api-rig` aparece como `connected`. (AC-12) | [ ] |
| 2.3.2 | `/mcp list-tools transcription-api-rig` | Lista los **7 tools**: `request_upload_url`, `start_transcription`, `list_my_transcriptions`, `search_my_transcriptions`, `get_transcription`, `delete_transcription`, `get_user_info`. (AC-12, RF-MCP-00) | [ ] |
| 2.3.3 | `/mcp list-resources transcription-api-rig` | Reconoce los 2 patterns: `transcription://<id>` y `transcription://<id>/images/<image_id>`. (AC-12, RF-MCP-07/08) | [ ] |
| 2.3.4 | Login web a `${PUBLIC_BASE_URL}/auth/login`, luego `GET /auth/me` con cookie | Body incluye `mcp_url: "<PUBLIC_BASE_URL>/mcp"` (no placeholder). (AC-13) | [ ] |

---

## 3. Flujo MCP-driven feliz (AC-1, AC-7, AC-10, AC-14)

> Audio real recomendado: una grabación corta (30 s – 2 min) en español rioplatense con ≥2 hablantes, formato MP3 o M4A. Anotá una palabra distintiva para la búsqueda FTS más adelante.

### 3.1 `request_upload_url`

```
USER_A → MCP tool: request_upload_url(
  kind="audio",
  file_size_bytes=<bytes_reales>,
  mime_type="audio/mpeg"
)
```

| Aserción | Resultado esperado | OK? |
|---|---|---|
| Response carries `upload_id` (uuid v4) | sí | [ ] |
| Response carries `upload_url` (formato `${PUBLIC_BASE_URL}/api/upload`) | sí | [ ] |
| Response carries `upload_bearer` (token ephemeral) | sí — anotarlo, se usa una sola vez | [ ] |
| Response carries `expires_at` (≥ ahora + 9 min) | sí | [ ] |
| `psql -c "SELECT status FROM upload_sessions WHERE id='<upload_id>';"` | `requested` | [ ] |
| `psql -c "SELECT upload_bearer_hash FROM upload_sessions WHERE id='<upload_id>';"` | NOT NULL, hash sha-256 (64 hex chars) | [ ] |

```
UPLOAD_ID=___
UPLOAD_URL=___
UPLOAD_BEARER=___
```

### 3.2 `POST /api/upload` (multipart con bearer ephemeral)

```bash
curl -X POST "$UPLOAD_URL" \
  -H "Authorization: Bearer $UPLOAD_BEARER" \
  -F "file=@/path/to/audio.mp3" \
  -F "upload_id=$UPLOAD_ID"
```

| Aserción | Resultado esperado | OK? |
|---|---|---|
| HTTP 200 | sí | [ ] |
| `psql -c "SELECT status FROM upload_sessions WHERE id='$UPLOAD_ID';"` | `uploaded` | [ ] |
| `ls $DATA_DIR/uploads/$UPLOAD_ID/` | `original.bin` (tamaño ≈ archivo original) | [ ] |

### 3.3 `start_transcription`

```
USER_A → MCP tool: start_transcription(upload_id="<UPLOAD_ID>")
```

| Aserción | Resultado esperado | OK? |
|---|---|---|
| Tool retorna sin timeout dentro del límite (≤ 1800 s para audio corto) | sí | [ ] |
| Response carries `transcription_id`, `audio_hash` (64 hex), `language="es"`, `segments` (≥1 con `speaker`), `duration_seconds`, `num_speakers`, `text_content` | sí, todos los campos poblados | [ ] |
| Logs del rig contienen `pipeline_orchestrate ok` y NO `GPUBusy` / `PipelineTimeout` | sí | [ ] |
| `psql -c "SELECT user_id FROM transcriptions WHERE id='<transcription_id>';"` | `<USER_A_ID>` | [ ] |
| `psql -c "SELECT status FROM upload_sessions WHERE id='$UPLOAD_ID';"` | `consumed` o equivalente final | [ ] |
| `ls $DATA_DIR/cache/$USER_A_ID/<audio_hash>/` | `result.json` | [ ] |
| `ls $DATA_DIR/uploads/$UPLOAD_ID/` | vacío o el directorio borrado (cleanup AC-3 Capa 4) | [ ] |
| `psql -c "SELECT last_used_at FROM mcp_bearers WHERE user_id='$USER_A_ID' ORDER BY created_at DESC LIMIT 1;"` | timestamp ≈ ahora (AC-14) | [ ] |

```
TRANSCRIPTION_ID=___
AUDIO_HASH=___
```

### 3.4 `get_transcription`

```
USER_A → MCP tool: get_transcription(transcription_id="<TRANSCRIPTION_ID>")
```

| Aserción | Resultado esperado | OK? |
|---|---|---|
| Misma payload que el response de `start_transcription` | sí (cache hit, idempotente) | [ ] |

### 3.5 `list_my_transcriptions`

```
USER_A → MCP tool: list_my_transcriptions(limit=10)
```

| Aserción | Resultado esperado | OK? |
|---|---|---|
| Lista incluye `<TRANSCRIPTION_ID>` | sí | [ ] |
| `total_count` >= 1 | sí | [ ] |
| Items NO incluyen transcripciones de USER_B (cuando exista) | sí | [ ] |

### 3.6 `search_my_transcriptions`

> Reemplazá `<palabra_distintiva>` por una palabra que sepas que está en el audio (el operador la eligió antes del 3.1).

```
USER_A → MCP tool: search_my_transcriptions(query="<palabra_distintiva>")
```

| Aserción | Resultado esperado | OK? |
|---|---|---|
| Resultado incluye `<TRANSCRIPTION_ID>` con `rank > 0` y `snippet` que contiene la palabra resaltada | sí | [ ] |
| Si la palabra NO está en el audio, response es lista vacía (no 500) | sí | [ ] |

---

## 4. Cross-user isolation (AC-2, AC-3, AC-4, AC-5, AC-8, AC-11)

### 4.1 Reconfigurar Claude Code temporalmente con `USER_B_BEARER`

> Editar `~/.claude/mcp.json` y reemplazar el bearer por el de B; reiniciar Claude Code.

| Paso | Comando | Resultado esperado | OK? |
|---|---|---|---|
| 4.1.1 | `list_my_transcriptions()` | Lista NO contiene `<TRANSCRIPTION_ID>` (es de A). (AC-3) | [ ] |
| 4.1.2 | `get_transcription(transcription_id="<TRANSCRIPTION_ID>")` | Error `TRANSCRIPTION_NOT_FOUND` (no existence leak — AC-5, AC-8 / ADR-014/015 listener AND-injects user_id) | [ ] |
| 4.1.3 | `search_my_transcriptions(query="<palabra_distintiva>")` | Lista vacía (la transcripción de A no aparece para B) | [ ] |
| 4.1.4 | `start_transcription(upload_id="<UPLOAD_ID_de_A>")` | Error `UPLOAD_SESSION_NOT_FOUND` (AC-2 cross-user upload) | [ ] |
| 4.1.5 | `delete_transcription(transcription_id="<TRANSCRIPTION_ID>")` | Error `TRANSCRIPTION_NOT_FOUND` (AC-11 cross-user delete; el row sigue intacto). | [ ] |

### 4.2 Volver a `USER_A_BEARER` antes del 5.

---

## 5. Auth + middleware (AC-8, AC-14)

| Paso | Acción | Resultado esperado | OK? |
|---|---|---|---|
| 5.1 | Borrar el bearer del config (`Authorization` ausente) → reiniciar Claude → `list_my_transcriptions()` | Error `MCP_BEARER_INVALID` (no header). | [ ] |
| 5.2 | Bearer truncado (e.g., quitar últimos 10 chars) → reiniciar → cualquier tool | Error `MCP_BEARER_INVALID` (token desconocido). | [ ] |
| 5.3 | Revocar el bearer en DB: `psql -c "UPDATE mcp_bearers SET revoked_at = now() WHERE user_id='$USER_A_ID' ORDER BY created_at DESC LIMIT 1;"` → reiniciar Claude → cualquier tool | Error `MCP_BEARER_REVOKED`. | [ ] |
| 5.4 | UN-revocar y verificar que `last_used_at` se bumpea aunque el handler falle (best-effort): forzar un error (e.g., `get_transcription` con uuid malformada) → check `last_used_at` post-call | timestamp del último intento ≈ ahora. (AC-14) | [ ] |

---

## 6. Cleanup + delete (AC-11)

> Volver a USER_A_BEARER (un-revocado).

| Paso | Comando | Resultado esperado | OK? |
|---|---|---|---|
| 6.1 | `delete_transcription(transcription_id="<TRANSCRIPTION_ID>")` | Response OK; row tiene `deleted_at IS NOT NULL`. | [ ] |
| 6.2 | `get_transcription(transcription_id="<TRANSCRIPTION_ID>")` | Error `TRANSCRIPTION_NOT_FOUND` (soft-deleted). | [ ] |
| 6.3 | Repetir 6.1 (idempotencia) | Error `TRANSCRIPTION_NOT_FOUND` (no doble-delete). | [ ] |
| 6.4 | `psql -c "SELECT id, deleted_at FROM images WHERE transcription_id='<TRANSCRIPTION_ID>';"` | Si había imágenes asociadas: todas con `deleted_at IS NOT NULL` (cascade soft-delete). | [ ] |

---

## 7. Legacy endpoint deprecation (AC-16)

> El smoke valida que `POST /api/transcriptions` SIGUE funcionando (D-026: el rig no se rompe) pero deja un WARN en los logs.

```bash
curl -X POST "$PUBLIC_BASE_URL/api/transcriptions" \
  -H "Authorization: Bearer $USER_A_BEARER" \
  -F "file=@/path/to/audio.mp3" \
  -F "language=es"
```

| Aserción | Resultado esperado | OK? |
|---|---|---|
| HTTP 200 con la payload completa de transcripción | sí | [ ] |
| Logs del rig (`docker logs api` o `journalctl -u api`) contienen al menos UNA línea `legacy_endpoint_invoked deprecated_endpoint=POST_/api/transcriptions removal_target=Capa5` | sí (al menos una entrada por invocación) | [ ] |
| `curl -s $PUBLIC_BASE_URL/openapi.json | jq '.paths."/api/transcriptions".post.deprecated'` | `true` (AC-16) | [ ] |

---

## 8. Salida del smoke

| Estado final | Acción |
|---|---|
| Todos los OK marcados | Comentar el resultado (commit hash + fecha + nombre del operador) en el PR de `feat/capa4-mcp`. Cerrar drift `D-044-impl` en `2026-05-05-wiki-drifts.md` con el commit de Batch 0. Proceder con multi-agent review pre-merge. |
| Algún OK roto | Abrir issue / drift en `2026-05-05-wiki-drifts.md` (categoría 9), bloquear el merge, reportar al canal. NO mergear hasta que el OK fallido esté resuelto o tenga un waiver explícito de Franco. |

---

## Apéndice — datos del run

> Completar al cerrar el smoke.

| Campo | Valor |
|---|---|
| Operador | ___ |
| Fecha / hora UTC | ___ |
| Commit `feat/capa4-mcp` HEAD | ___ |
| Rig host | ___ |
| Cliente Claude (Code/Desktop + versión) | ___ |
| Audio usado (path + duración + #speakers reales) | ___ |
| Palabra distintiva FTS | ___ |
| Tiempo total del smoke (min) | ___ |
| Issues encontrados (si los hubo) | ___ |
