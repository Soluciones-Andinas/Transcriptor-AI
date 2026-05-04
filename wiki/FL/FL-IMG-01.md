# FL-IMG-01 — Adjuntar imagen a una transcripción existente

## 1. Objetivo

Permitir al usuario, desde su Claude, asociar una imagen (captura de pantalla) a una transcripción suya, para que la imagen quede disponible como contexto al generar la minuta.

## 2. Alcance

**In**: el user le pasa a su Claude una imagen y le dice "asociala a la reunión X". Claude obtiene una signed URL de upload, sube la imagen vía REST, y registra la asociación con `attach_image`. El binario vive en filesystem; los metadatos en Postgres.

**Out**: edición de imágenes (rotar, recortar) en backend; OCR automático; thumbnails generados por backend; gestión de imágenes huérfanas.

## 3. Actores y ownership

| Actor | Ownership |
|---|---|
| Usuario | Le pasa imagen a Claude; pide asociar a transcripción específica. |
| Claude Code/Desktop | Cliente MCP; obtiene signed URL, sube binario, llama `attach_image`. |
| MCP Server | Tools `request_image_upload_url`, `attach_image`. |
| FastAPI REST | Endpoint `POST /api/upload-image`. |
| Postgres | Almacena metadatos en `images`. |
| Filesystem | Almacena binario en `<DATA_DIR>/blobs/<user_id>/<transcription_id>/<image_id>.<ext>`. |

## 4. Precondiciones

1. User logueado y con bearer MCP válido.
2. Existe la transcripción `transcription_id` y pertenece al user.
3. Disco con espacio libre suficiente.
4. MIME type de la imagen es `image/png`, `image/jpeg`, `image/webp` o `image/gif`.
5. Tamaño ≤ `MAX_IMAGE_UPLOAD_MB` (default 25 MB).

## 5. Postcondiciones

**Éxito**:
- Existe registro en `images` con `transcription_id`, `user_id`, `filename`, `caption`, `file_path`, `size_bytes`.
- Existe el binario en `<DATA_DIR>/blobs/...`.
- Cliente recibe `image_id` y la transcripción ahora tiene la imagen accesible vía `transcription://<id>/images/<image_id>`.
- Logs `image_upload_url_requested`, `image_uploaded`, `image_attached`.

**Error**:
- Sin filas insertadas (transacción rollback).
- Si llegó a subirse el binario y falla `attach_image`, el binario se considera huérfano y un cleanup futuro lo borra (tracked en `upload_sessions` con `status=expired`).

## 6. Secuencia principal

```mermaid
sequenceDiagram
    participant U as Usuario
    participant CC as Claude Code/Desktop
    participant MCP as MCP Server
    participant API as FastAPI REST
    participant FS as Filesystem
    participant PG as Postgres

    U->>CC: "asociá esta imagen a la reunión <transcription_id>"
    CC->>MCP: tool request_image_upload_url(transcription_id, file_size, mime_type)
    MCP->>PG: SELECT transcriptions WHERE id=... AND user_id=...
    alt no existe o no es del user
        MCP-->>CC: error TRANSCRIPTION_NOT_FOUND
        CC-->>U: aviso al user
    else existe
        MCP->>PG: INSERT upload_sessions (kind='image', transcription_id, ...)
        MCP-->>CC: { upload_url, upload_id, bearer, expires_at }
    end
    CC->>CC: Bash: curl -F file=@imagen.png -H "Bearer ..." upload_url
    CC->>API: POST /api/upload-image (multipart, transcription_id, caption opcional)
    API->>API: valida bearer, encuentra upload_session, valida nonce
    API->>FS: mkdir -p blobs/<user_id>/<transcription_id>/; escribe binario
    API->>PG: INSERT images (transcription_id, user_id, filename, caption, file_path, size_bytes, mime_type)
    API->>PG: UPDATE upload_sessions SET status='uploaded', uploaded_at=now()
    API-->>CC: { image_id, ok: true }
    CC->>MCP: tool attach_image(transcription_id, image_id, caption?)
    MCP->>PG: UPDATE images SET caption=... WHERE id=image_id AND user_id=...
    MCP->>PG: UPDATE upload_sessions SET status='consumed', consumed_at=now()
    MCP-->>CC: { ok: true, image_id, transcription_id }
    CC-->>U: "imagen asociada a la reunión"
```

## 7. Camino alternativo / errores

| Condición | Manejo |
|---|---|
| `transcription_id` no existe o pertenece a otro user | `TRANSCRIPTION_NOT_FOUND` (404); cleanup no necesario |
| MIME type no permitido | `UNSUPPORTED_EXTENSION` (400) en `request_image_upload_url` (rechazo temprano) |
| `file_size_bytes` excede límite | `FILE_TOO_LARGE` (413) en `request_image_upload_url` |
| Upload bytes corrompen | API responde 400; el binario parcial se elimina; upload_session queda en estado `requested` y expira |
| `attach_image` se llama con `image_id` que no fue uploaded | `IMAGE_NOT_FOUND` (404) |
| Disco lleno al escribir binario | 500 + log `image_uploaded_failed`; upload_session no avanza |
| Binario subido pero `attach_image` nunca llamado | upload_session queda en `uploaded`; cleanup la marca expired tras grace period; binario huérfano se borra en cleanup |

## 8. Slice de arquitectura

Componentes activados:
- C. MCP Server (tools `request_image_upload_url`, `attach_image`).
- D. REST endpoints (`POST /api/upload-image`).
- G. Persistencia Relacional (`images`, `upload_sessions`).
- Filesystem blobs (`<DATA_DIR>/blobs/`).

ADRs aplicables: [ADR-008](../ADR/ADR-008.md), [ADR-011](../ADR/ADR-011.md), [ADR-013](../ADR/ADR-013.md).

## 9. Touchpoints de datos

**Entidades**: `images` (INSERT), `upload_sessions` (INSERT + UPDATEs).

**Filesystem**: binario en `<DATA_DIR>/blobs/<user_id>/<transcription_id>/<image_id>.<ext>`.

**Eventos de log**: `image_upload_url_requested`, `image_uploaded`, `image_attached`.

## 10. RF candidatos

| RF candidato | Cubre |
|---|---|
| RF-IMG-01 | Tool `request_image_upload_url`: valida transcription ownership y crea upload_session |
| RF-IMG-02 | Endpoint `POST /api/upload-image`: recibe binario, valida session, escribe blob, INSERT image |
| RF-IMG-03 | Tool `attach_image`: confirma y opcionalmente actualiza caption |
| RF-IMG-04 | Listar imágenes asociadas a una transcripción (resource MCP) |
| RF-IMG-05 | Cleanup de imágenes huérfanas (upload sessions vencidas con `status=uploaded`) |

## 11. Cuellos de botella, riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| Imágenes huérfanas (uploaded pero no attached) | Cleanup periódico borra binarios cuyo `upload_session.status='uploaded' AND expires_at + grace < now`. Documentado en RF-IMG-05 |
| User adjunta imagen a transcripción de otro user | Validación per-user en cada paso (request URL, upload, attach) |
| Caption con XSS para minutas (Claude la lee como texto) | Caption es texto plano; Claude lo trata como string. Mitigación adicional: sanitizar en MCP response |
| Disco crece sin límite con muchas imágenes | El user es responsable de limpiar. Cleanup automático solo borra huérfanas, no asociadas |

## 12. RF handoff checklist

- [x] Actores y ownership explícitos.
- [x] Diagrama mermaid del camino principal.
- [x] Errores documentados.
- [x] Eventos clave listados.
- [x] Riesgos y mitigaciones explícitos.
- [x] RFs candidatos enumerados.
- [x] No hay decisiones críticas abiertas.
- [x] Listo para `crear-rf`.
