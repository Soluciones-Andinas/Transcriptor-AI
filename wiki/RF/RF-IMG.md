# Módulo IMG — Requerimientos Funcionales (Imágenes asociadas a transcripciones)

**Source flow**: [`FL-IMG-01`](../FL/FL-IMG-01.md)
**Architecture**: [`02_arquitectura.md`](../02_arquitectura.md) §3 (componentes C, D, G)
**Data model**: [`05_modelo_datos.md`](../05_modelo_datos.md) §2 (tabla `images`, `upload_sessions`)
**Hardening level**: Execution-Normative

> **Cambio de contrato Capa 4 (drift D-043 + D-082, 2026-05-11)**:
> Las tools MCP `request_image_upload_url` y `attach_image` definidas en
> RF-IMG-01 y RF-IMG-03 **no fueron implementadas como tools separadas**.
> Se unificaron bajo [`RF-MCP-01`](RF-MCP.md#rf-mcp-01-tool-request_upload_url)
> con discriminador `kind`:
>
> - `request_upload_url(kind="image", transcription_id=T, mime_type, file_size_bytes)` reemplaza RF-IMG-01.
> - No existe tool `attach_image`: el endpoint `POST /api/upload-image` (RF-IMG-02) inserta la row `images` directamente y la marca `status='uploaded'` en la misma transacción. La metadata se asocia al `transcription_id` en el upload session, no en un tool MCP posterior. RF-IMG-03 queda **sin contraparte en código**; los snippets de "Process Steps" y "Acceptance Criteria" de RF-IMG-01 / RF-IMG-03 se mantienen como referencia de diseño pero **no son la spec ejecutable**.
> - El campo `caption` de `images` se **eliminó** del schema (drift D-083 cerrado 2026-05-11 con migración `2c83f1bd7e94_drop_caption_from_images.py`). No había write path: cada fila tenía `caption=NULL`. Si Capa 5 UI introduce un feature "describir imagen", se agrega columna nueva con spec dedicado.
>
> RF-IMG-02 (endpoint REST `/api/upload-image`) sigue vigente y es la spec ejecutable del upload binary para imágenes — el cliente lo invoca con el `upload_url` recibido de `request_upload_url(kind="image")`.

## Tabla resumen

| ID | Título | Estado | Reemplazo / nota |
|---|---|---|---|
| RF-IMG-01 | Tool `request_image_upload_url` | **Reemplazada** | Cubierto por RF-MCP-01 (`kind="image"`) |
| RF-IMG-02 | Endpoint `POST /api/upload-image` | **Vigente** | Spec ejecutable del binary upload |
| RF-IMG-03 | Tool `attach_image` | **Retirada** | Sin contraparte en código; `images` se inserta directamente en RF-IMG-02. Caption queda NULL (D-083) |

---

## RF-IMG-01: Tool `request_image_upload_url`

### Execution Sheet

| Campo | Valor |
|---|---|
| ID | RF-IMG-01 |
| Título | Solicitar URL firmada para subir imagen asociada a una transcripción |
| Actor primario | Bearer válido (Claude Code/Desktop) |
| Prioridad | Alta |
| Severidad | Mayor |

### Precondiciones

| # | Condición |
|---|---|
| 1 | Bearer activo |
| 2 | `transcription_id` existe en `transcriptions`, pertenece al user, no está soft-deleted |
| 3 | `mime_type` está en lista permitida (`image/png`, `image/jpeg`, `image/webp`, `image/gif`) |
| 4 | `file_size_bytes` ≤ MAX_IMAGE_UPLOAD_MB (default 25 MB) |

### Inputs

| Campo | Tipo | Requerido | Validación |
|---|---|---|---|
| `transcription_id` | UUID | Sí | Owner check |
| `file_size_bytes` | int | Sí | `> 0`, ≤ límite |
| `mime_type` | string | Sí | enum permitido |
| `filename` | string | No | Para preservar nombre del user |

### Process Steps

| # | Paso |
|---|---|
| 1 | Auth middleware → user_id |
| 2 | SELECT transcriptions WHERE id=transcription_id AND user_id=user_id AND deleted_at IS NULL |
| 3 | Si no encontrada: `TRANSCRIPTION_NOT_FOUND` (404) |
| 4 | Validar mime_type ∈ enum |
| 5 | Validar file_size_bytes ≤ límite |
| 6 | Llamar lógica común con `kind='image'` (delegar en RF-MCP-01 internamente) |
| 7 | Emitir log `image_upload_url_requested(user_id, upload_id, transcription_id)` |
| 8 | Responder `{upload_url, upload_id, bearer, expires_at}` |

### Typed Errors

| Código | HTTP | Causa |
|---|---|---|
| `TRANSCRIPTION_NOT_FOUND` | 404 | transcription_id no es del user o no existe |
| `UNSUPPORTED_EXTENSION` | 400 | mime_type no permitido |
| `FILE_TOO_LARGE` | 413 | size > límite |
| `INVALID_PARAMETER` | 400 | size <=0, filename con paths sospechosos |

### Acceptance Criteria

```gherkin
Scenario: Request OK
  Given user X con transcription T
  When tool request_image_upload_url(transcription_id=T, file_size_bytes=2MB, mime_type="image/png")
  Then 200 con upload_url, upload_id, bearer, expires_at
    And upload_session creada con kind='image', transcription_id=T

Scenario: Transcription ajena
  Given transcription T del user Y
  When user X tool request_image_upload_url(T, ...)
  Then 404 + TRANSCRIPTION_NOT_FOUND

Scenario: Mime no permitido
  When tool request_image_upload_url(... mime_type="image/svg+xml")
  Then 400 + UNSUPPORTED_EXTENSION
```

### Test Traceability

| Test ID | Tipo |
|---|---|
| TP-IMG-01-pos-01 | Positivo |
| TP-IMG-01-neg-01 | Negativo (transcription ajena) |
| TP-IMG-01-neg-02 | Negativo (mime no permitido) |
| TP-IMG-01-neg-03 | Negativo (size > límite) |

**TODO explicit = 0**.

---

## RF-IMG-02: Endpoint POST /api/upload-image

### Execution Sheet

| Campo | Valor |
|---|---|
| ID | RF-IMG-02 |
| Título | Recibir binario de imagen, validar mime real, persistir blob y metadata |
| Actor primario | Cliente HTTP |
| Prioridad | Alta |
| Severidad | Mayor |

### Process Steps

| # | Paso |
|---|---|
| 1 | Recibir multipart `file` + query `session=<nonce>` + bearer for upload |
| 2 | SELECT upload_sessions WHERE nonce=? AND status='requested' AND kind='image' |
| 3 | Si no encontrada: `UPLOAD_SESSION_NOT_FOUND` |
| 4 | Validar `now < expires_at` |
| 5 | Validar bearer: computar `received_hash = SHA-256(plaintext del header `Authorization: Bearer <plaintext>`).hex()` y comparar (constant-time, e.g. `hmac.compare_digest`) contra `upload_sessions.upload_bearer_hash`. Si no matchea: `MCP_BEARER_INVALID` (401). |
| 6 | Leer primeros bytes del archivo, detectar mime real con file-magic (ej. `python-magic`) |
| 7 | Si mime real ≠ `expected_mime_type`: `INVALID_FORMAT` (400) |
| 8 | Validar tamaño total ≤ `expected_size_bytes * 1.05` |
| 9 | Inicio transacción Postgres |
| 10 | INSERT images (id=image_id UUID, transcription_id, user_id, filename, mime_type, size_bytes, file_path) |
| 11 | Mover binario de buffer a `<DATA_DIR>/blobs/<user_id>/<transcription_id>/<image_id>.<ext>` |
| 12 | Si move falla: rollback DB; si ya se commiteó pero filesystem falla, marcar la row como inválida (campo deleted_at o flag); log ERROR |
| 13 | UPDATE upload_sessions SET status='uploaded', uploaded_at=now() |
| 14 | Commit |
| 15 | Emitir log `image_uploaded(user_id, image_id, transcription_id, size_bytes)` |
| 16 | Responder `{ok: true, image_id}` |

### Typed Errors

| Código | HTTP | Causa |
|---|---|---|
| `UPLOAD_SESSION_NOT_FOUND` | 404 | nonce desconocido/expirado |
| `INVALID_FORMAT` | 400 | mime real no coincide con declarado |
| `FILE_TOO_LARGE` | 413 | excede esperado |
| `MCP_BEARER_INVALID` | 401 | bearer no coincide |

### Acceptance Criteria

```gherkin
Scenario: Upload PNG OK
  Given upload_session válida con expected_mime_type='image/png'
  When POST /api/upload-image con archivo PNG real, bearer correcto, nonce válido
  Then 200 con image_id
    And blob existe en /data/blobs/<user_id>/<transcription_id>/<image_id>.png
    And images row existe

Scenario: Mime falsificado
  Given expected_mime_type='image/png'
  When POST /api/upload-image con archivo MP4 (renombrado a .png)
  Then 400 + INVALID_FORMAT (file-magic detecta MP4)
    And no se crea row ni blob
```

### Test Traceability

| Test ID | Tipo |
|---|---|
| TP-IMG-02-pos-01 | Positivo PNG |
| TP-IMG-02-pos-02 | Positivo JPEG |
| TP-IMG-02-neg-01 | Negativo (mime fake) |
| TP-IMG-02-neg-02 | Negativo (size mismatch) |
| TP-IMG-02-neg-03 | Negativo (session expired) |

**TODO explicit = 0**.

---

## RF-IMG-03: Tool `attach_image`

### Execution Sheet

| Campo | Valor |
|---|---|
| ID | RF-IMG-03 |
| Título | Confirmar la asociación imagen-transcripción y opcionalmente actualizar caption |
| Actor primario | Bearer válido |
| Prioridad | Alta |
| Severidad | Mayor |

### Inputs

| Campo | Tipo | Requerido |
|---|---|---|
| `transcription_id` | UUID | Sí |
| `image_id` | UUID | Sí |
| `caption` | string | No (max 500 chars) |

### Process Steps

| # | Paso |
|---|---|
| 1 | Auth middleware → user_id |
| 2 | SELECT images WHERE id=image_id AND user_id=user_id AND transcription_id=transcription_id AND deleted_at IS NULL |
| 3 | Si no encontrada: `IMAGE_NOT_FOUND` (404) |
| 4 | UPDATE images SET caption=? (si caption presente) |
| 5 | UPDATE upload_sessions SET status='consumed', consumed_at=now() WHERE id matches image's upload session |
| 6 | Emitir log `image_attached(user_id, image_id, transcription_id)` |
| 7 | Responder `{ok: true, image_id, transcription_id, caption}` |

### Typed Errors

| Código | HTTP | Causa |
|---|---|---|
| `IMAGE_NOT_FOUND` | 404 | image no existe o no es del user, o no corresponde a esa transcription |
| `INVALID_PARAMETER` | 400 | caption > 500 chars |

### Acceptance Criteria

```gherkin
Scenario: Attach con caption
  Given image uploaded a transcription T del user X
  When user X tool attach_image(T, image_id, caption="Diagrama arquitectura")
  Then 200 con caption guardada
    And images.caption = "Diagrama arquitectura"
    And upload_sessions.status='consumed'

Scenario: Attach sin caption
  When tool attach_image(T, image_id)
  Then 200; caption sigue NULL

Scenario: Attach a transcription wrong (mismatch)
  Given image_id pertenece a transcription T1
  When tool attach_image(T2, image_id)
  Then 404 + IMAGE_NOT_FOUND

Scenario: Caption demasiado larga
  When attach_image con caption de 600 chars
  Then 400 + INVALID_PARAMETER
```

### Test Traceability

| Test ID | Tipo |
|---|---|
| TP-IMG-03-pos-01 | Positivo con caption |
| TP-IMG-03-pos-02 | Positivo sin caption |
| TP-IMG-03-neg-01 | Negativo (image cross-transcription) |
| TP-IMG-03-neg-02 | Negativo (image cross-user) |
| TP-IMG-03-neg-03 | Negativo (caption muy larga) |

**TODO explicit = 0**.
