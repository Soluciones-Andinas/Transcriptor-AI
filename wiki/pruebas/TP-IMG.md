# Test Plan — Módulo IMG (Imágenes asociadas a transcripciones)

**Source RFs**: [`RF/RF-IMG.md`](../RF/RF-IMG.md)
**Stack**: pytest 8.x + pytest-asyncio + httpx + cliente MCP del SDK Anthropic

## Convenciones

- Fixtures de imágenes pequeñas en `tests/fixtures/images/` (PNG 100x100, JPEG 200x200, etc.).
- Cross-user isolation: cada test usa al menos 2 users distintos.

## TP-IMG-01: Tool request_image_upload_url (RF-IMG-01)

| Test ID | Tipo | Descripción | Setup | Acción | Aserciones |
|---|---|---|---|---|---|
| TP-IMG-01-pos-01 | Integration | Request OK | User X con transcription T propia | tool `request_image_upload_url(transcription_id=T, file_size_bytes=2_000_000, mime_type="image/png")` | 200; row `upload_sessions` con `kind='image', transcription_id=T, user_id=X` |
| TP-IMG-01-neg-01 | Integration | **Cross-user** | Transcription T del user Y | User X tool con T | 404 + `TRANSCRIPTION_NOT_FOUND` |
| TP-IMG-01-neg-02 | Unit | Mime no permitido | `mime_type="image/svg+xml"` | tool | 400 + `UNSUPPORTED_EXTENSION` |
| TP-IMG-01-neg-03 | Unit | Size > límite | `file_size_bytes=30_000_000` (con MAX=25MB) | tool | 413 + `FILE_TOO_LARGE` |

## TP-IMG-02: Endpoint POST /api/upload-image (RF-IMG-02)

| Test ID | Tipo | Descripción | Setup | Acción | Aserciones |
|---|---|---|---|---|---|
| TP-IMG-02-pos-01 | Integration | Upload PNG | `expected_mime_type='image/png'`; archivo PNG real | `POST /api/upload-image?session=...` con file + bearer | 200; row `images`; binario en `/data/blobs/<user>/<transcription>/<image_id>.png` |
| TP-IMG-02-pos-02 | Integration | Upload JPEG | idem JPEG | upload | 200; binario `.jpg` |
| TP-IMG-02-neg-01 | Integration | Mime fake | `expected_mime_type='image/png'` pero archivo MP4 renombrado | upload | 400 + `INVALID_FORMAT` (file-magic detecta); sin row creada; sin binario |
| TP-IMG-02-neg-02 | Unit | Size mismatch | Archivo más grande que esperado +5% | upload | 413 + `FILE_TOO_LARGE` |
| TP-IMG-02-neg-03 | Unit (freezegun) | Session expired | TTL pasado | upload | 404 + `UPLOAD_SESSION_NOT_FOUND` |

## TP-IMG-03: Tool attach_image (RF-IMG-03)

| Test ID | Tipo | Descripción | Setup | Acción | Aserciones |
|---|---|---|---|---|---|
| TP-IMG-03-pos-01 | Integration | Attach con caption | image uploaded a T del user X | User X tool `attach_image(T, image_id, caption="Diagrama")` | 200; `images.caption='Diagrama'`; `upload_sessions.status='consumed'` |
| TP-IMG-03-pos-02 | Integration | Attach sin caption | idem sin caption | tool | 200; `caption IS NULL` |
| TP-IMG-03-neg-01 | Integration | Image cross-transcription | image_id de T1 | User X tool `attach_image(T2, image_id)` | 404 + `IMAGE_NOT_FOUND` |
| TP-IMG-03-neg-02 | Integration | **Cross-user** | image_id del user Y | User X tool | 404 |
| TP-IMG-03-neg-03 | Unit | Caption muy larga | caption de 600 chars | tool | 400 + `INVALID_PARAMETER` |

## Helpers

```python
# tests/helpers/image_factory.py

@pytest.fixture
def png_fixture():
    """PNG mínimo válido para testing."""
    return Path(__file__).parent / "fixtures" / "images" / "test_100x100.png"

def make_image_record(db, transcription_id, user_id, mime="image/png"):
    """Inserta image row + binario fixture para tests."""
    # ...
```

## Cobertura objetivo

- Líneas: ≥ 85 % en módulo `images/`.
- Per-user isolation: cada RF tiene test cross-user.
- Cada `mime_type` permitido tiene al menos un test positivo.
