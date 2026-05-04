# Test Plan — Módulo MCP (Servidor MCP y endpoints REST de soporte)

**Source RFs**: [`RF/RF-MCP.md`](../RF/RF-MCP.md)
**Stack**: pytest 8.x + pytest-asyncio + httpx (REST tests) + cliente MCP del SDK Anthropic (E2E MCP tests) + freezegun

## Convenciones

- Cada user en tests es un fixture distinto. La cobertura cross-user requiere al menos 2 users en la DB.
- Tools MCP se testean usando el cliente del SDK `mcp` (test-only); no se golpea por HTTP raw el endpoint `/mcp`.
- Bearer tokens se setean en el header `Authorization` para tools MCP y en `Authorization: Bearer <upload_bearer>` para REST upload.

## TP-MCP-11: Auth middleware MCP (RF-MCP-11)

| Test ID | Tipo | Descripción | Setup | Acción | Aserciones |
|---|---|---|---|---|---|
| TP-MCP-11-pos-01 | Integration | Bearer válido pasa | Bearer activo en DB | tool call con `Authorization: Bearer <plaintext>` | tool ejecuta; `last_used_at` actualizado |
| TP-MCP-11-neg-01 | Integration | Bearer revocado | bearer con `revoked_at NOT NULL` | tool call | 401 + `MCP_BEARER_REVOKED` |
| TP-MCP-11-neg-02 | Integration | Bearer inexistente | Header con plaintext que no hashea a ningún row | tool call | 401 + `MCP_BEARER_INVALID` |
| TP-MCP-11-neg-03 | Integration | Sin header Authorization | — | tool call | 401 |
| TP-MCP-11-cov-01 | Cobertura | last_used_at se actualiza | bearer con `last_used_at` antiguo | tool call | nuevo `last_used_at` cercano a `now()` |

## TP-MCP-01: Tool request_upload_url (RF-MCP-01)

| Test ID | Tipo | Descripción | Setup | Acción | Aserciones |
|---|---|---|---|---|---|
| TP-MCP-01-pos-01 | Integration | Audio request | User X autenticado | tool `request_upload_url(kind="audio", file_size_bytes=100MB)` | 200; row `upload_sessions` con `user_id=X, kind='audio', status='requested'`; response tiene `upload_url`, `upload_id`, `bearer`, `expires_at` |
| TP-MCP-01-pos-02 | Integration | Image request transcription propia | User X con transcription T | tool `request_upload_url(kind="image", transcription_id=T, file_size_bytes=2MB, mime_type="image/png")` | 200; upload_session con `transcription_id=T` |
| TP-MCP-01-neg-01 | Integration | Image transcription ajena | Transcription T del user Y | User X tool con `transcription_id=T` | 404 + `TRANSCRIPTION_NOT_FOUND` |
| TP-MCP-01-neg-02 | Unit | File too large | `file_size_bytes` > MAX | tool | 413 + `FILE_TOO_LARGE` |
| TP-MCP-01-neg-03 | Unit | Mime no permitido | `mime_type="image/svg+xml"` | tool | 400 + `UNSUPPORTED_EXTENSION` |

## TP-MCP-02: Tool start_transcription (RF-MCP-02)

| Test ID | Tipo | Descripción | Setup | Acción | Aserciones |
|---|---|---|---|---|---|
| TP-MCP-02-pos-01 | E2E | Cache miss completa | Upload uploaded; cache vacío; modelos mock o reales con audio corto | tool `start_transcription(upload_id)` | 200; `transcription_id` retornado; row `transcriptions`; cache filesystem poblado; upload_session `consumed` |
| TP-MCP-02-pos-02 | E2E | Cache hit | Cache pre-poblado con audio_hash matching | tool | 200 con `cache_hit=true`; total_duration < 10s; row `transcriptions` nueva (histórico per-user) |
| TP-MCP-02-neg-01 | Integration | Upload ajeno | Upload del user Y | User X tool | 404 + `UPLOAD_SESSION_NOT_FOUND` |
| TP-MCP-02-neg-02 | Integration | Already consumed | Upload con `status='consumed'` | tool | 409 + `UPLOAD_SESSION_ALREADY_CONSUMED` |
| TP-MCP-02-neg-03 | Mock | Lock busy | Otro request en curso (mock que retiene lock 10s) | tool | 503 + `LOCK_BUSY` |

## TP-MCP-03: Endpoint REST POST /api/upload (RF-MCP-03)

| Test ID | Tipo | Descripción | Setup | Acción | Aserciones |
|---|---|---|---|---|---|
| TP-MCP-03-pos-01 | Integration | Upload audio OK | Upload session creada | `POST /api/upload?session=<nonce>` con MP4 + Bearer | 200; archivo en `/data/uploads/<id>/original.bin`; status='uploaded' |
| TP-MCP-03-pos-02 | Integration | Upload image OK | Upload session image | `POST /api/upload-image?session=<nonce>` con PNG + Bearer | 200; row `images`; binario en `/data/blobs/...` |
| TP-MCP-03-neg-01 | Unit | Bearer wrong | Bearer no coincide con `bearer_for_upload` | upload | 401 |
| TP-MCP-03-neg-02 | Unit (freezegun) | Session expired | `expires_at` en el pasado | upload | 404 + `UPLOAD_SESSION_NOT_FOUND` |
| TP-MCP-03-neg-03 | Unit | Size mismatch | Archivo 200MB, expected 100MB | upload | 413 + `FILE_TOO_LARGE` |
| TP-MCP-03-neg-04 | Unit | Mime fake | PNG declarado, MP4 real | upload-image | 400 + `INVALID_FORMAT` |

## TP-MCP-04: list_my_transcriptions (RF-MCP-04)

| Test ID | Tipo | Descripción | Setup | Acción | Aserciones |
|---|---|---|---|---|---|
| TP-MCP-04-pos-01 | Integration | User con 5 transcripts | Pre-poblar 5 rows con `user_id=X` | tool `list_my_transcriptions()` | items.length=5; total=5 |
| TP-MCP-04-pos-02 | Integration | Paginación | 30 rows | tool `list_my_transcriptions(limit=10, offset=20)` | items.length=10; total=30 |
| TP-MCP-04-neg-01 | Integration | Sin auth | — | tool sin bearer | 401 |
| TP-MCP-04-cov-01 | Integration | **Cross-user isolation** | User A con 5, User B con 3 | User A tool | items=[5 de A]; ningún item de B |

## TP-MCP-05: search_my_transcriptions (RF-MCP-05)

| Test ID | Tipo | Descripción | Setup | Acción | Aserciones |
|---|---|---|---|---|---|
| TP-MCP-05-pos-01 | Integration | Match | Transcript con text contiene "arquitectura" | tool `search_my_transcriptions(query="arquitectura")` | items con esa transcripción y `rank > 0` |
| TP-MCP-05-pos-02 | Integration | No match | Query sin matches | tool | items=[] |
| TP-MCP-05-cov-01 | Integration | **Cross-user isolation** | User A y B con texts que matchean | User A search | solo resultados de A |

## TP-MCP-06: get_transcription (RF-MCP-06)

| Test ID | Tipo | Descripción | Setup | Acción | Aserciones |
|---|---|---|---|---|---|
| TP-MCP-06-pos-01 | Integration | Get propia | Transcript T del user X | User X tool `get_transcription(T)` | 200; JSON completo + `images: []` |
| TP-MCP-06-neg-01 | Integration | Get ajena | T del user Y | User X tool | 404 + `TRANSCRIPTION_NOT_FOUND` |
| TP-MCP-06-neg-02 | Integration | Soft-deleted | T con `deleted_at NOT NULL` | User X (owner) tool | 404 |

## TP-MCP-07: Resource transcription://<id> (RF-MCP-07)

| Test ID | Tipo | Descripción | Aserciones |
|---|---|---|---|
| TP-MCP-07-pos-01 | Integration | Resource fetch propia | Resource retorna mismo JSON que `get_transcription` |
| TP-MCP-07-neg-01 | Integration | Resource cross-user | 404 |

## TP-MCP-08: Resource imágenes (RF-MCP-08)

| Test ID | Tipo | Descripción | Aserciones |
|---|---|---|---|
| TP-MCP-08-pos-01 | Integration | Fetch binary | Resource retorna binario con mime correcto |
| TP-MCP-08-neg-01 | Integration | Cross-user | 404 |
| TP-MCP-08-neg-02 | Integration | Image inexistente | 404 |

## TP-MCP-09: delete_transcription (RF-MCP-09)

| Test ID | Tipo | Descripción | Aserciones |
|---|---|---|---|
| TP-MCP-09-pos-01 | Integration | Soft delete propia | `deleted_at NOT NULL` |
| TP-MCP-09-neg-01 | Integration | Cross-user | 404 |
| TP-MCP-09-neg-02 | Integration | Idempotente | Segunda llamada → 404 |
| TP-MCP-09-cov-01 | Integration | Cascade en images | Imágenes asociadas también soft-deleted |

## TP-MCP-10: get_user_info (RF-MCP-10)

| Test ID | Tipo | Aserciones |
|---|---|---|
| TP-MCP-10-pos-01 | Integration | Retorna user actual + bearer_id |

## Helpers

```python
# tests/helpers/mcp_factory.py

@pytest.fixture
def mcp_client(test_user_bearer):
    """Cliente MCP del SDK conectado al test server con bearer del user."""
    from mcp.client.streamable_http import streamablehttp_client
    # ...

def assert_per_user_scoping(client_a, client_b, resource_id):
    """Verifica que client_a no pueda acceder a un resource de client_b."""
    # ...
```

## Cobertura objetivo

- Líneas: ≥ 85 % en módulo `mcp/`.
- Branches: ≥ 80 %.
- Cada `error_code` documentado cubierto.
- Per-user isolation: cada RF de datos tiene un `cov-*` que valida cross-user.
