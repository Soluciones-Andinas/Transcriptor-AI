# FL-MIN-01 — Generación de minuta en Claude del usuario

## 1. Objetivo

Permitir al usuario, desde su Claude (Code o Desktop), generar la minuta de una reunión consumiendo el transcript y las imágenes asociadas vía MCP. La generación pasa en el contexto del Claude del user; el backend solo provee datos read-only.

## 2. Alcance

**In**: el user le pide a su Claude "generá la minuta de la reunión X". Claude usa tools/resources MCP (`get_transcription`, `transcription://<id>`, `transcription://<id>/images/<image_id>`) para traer transcript + imágenes y compone la minuta dentro de su contexto. El user lee o copia la minuta donde necesite.

**Out**: backend genera minutas; persistencia de minutas en backend; integración con sistemas de gestión de minutas (Confluence, Notion, etc.) por parte del backend.

## 3. Actores y ownership

| Actor | Ownership |
|---|---|
| Usuario | Inicia la generación con un prompt al Claude. |
| Claude del usuario | Compone la minuta en su contexto LLM usando los datos traídos vía MCP. |
| MCP Server | Sirve tools y resources read-only sobre las transcripciones e imágenes del user. |
| Postgres | Datos persistidos (transcripciones, imágenes). |
| Filesystem blobs | Binarios de imágenes. |

## 4. Precondiciones

1. User logueado con bearer MCP válido configurado en su Claude.
2. Existe la transcripción `transcription_id` y pertenece al user.
3. (Opcional) hay imágenes asociadas a la transcripción.

## 5. Postcondiciones

**Éxito**:
- El user obtiene la minuta como texto en su Claude (o Markdown, dependiendo del prompt).
- El backend NO persiste la minuta; es output del LLM del user.
- Los logs del backend registran las tool calls y resource fetches que Claude hizo (`mcp_request_received`, `transcription_listed`, etc.).

**Error**:
- Si transcript no existe: `TRANSCRIPTION_NOT_FOUND` y Claude lo reporta al user.
- Si la generación falla en Claude (rate limit, timeout, etc.): el user lo ve y reintenta.

## 6. Secuencia principal

```mermaid
sequenceDiagram
    participant U as Usuario
    participant CC as Claude Code/Desktop
    participant MCP as MCP Server
    participant PG as Postgres
    participant FS as Filesystem (blobs)

    U->>CC: "armá la minuta de la reunión sobre arquitectura"
    Note over CC: Claude no sabe el transcription_id; debe descubrirlo
    CC->>MCP: tool list_my_transcriptions(limit=20)
    MCP->>PG: SELECT transcriptions WHERE user_id=... ORDER BY created_at DESC
    PG-->>MCP: lista de transcriptions
    MCP-->>CC: array de { id, original_filename, created_at, duration_seconds }
    Note over CC: Claude infiere cuál es "la reunión sobre arquitectura"
    CC->>MCP: tool search_my_transcriptions(query="arquitectura")
    MCP->>PG: SELECT ... WHERE to_tsvector('spanish', text) @@ to_tsquery(...)
    PG-->>MCP: resultados ranqueados
    MCP-->>CC: array con scores
    CC->>MCP: resource transcription://<id>
    MCP->>PG: SELECT transcriptions WHERE id=... AND user_id=...
    PG-->>MCP: TranscriptionResult JSON
    MCP-->>CC: JSON completo (segments, words, speakers, metadata, images metadata)
    Note over CC: Claude opcionalmente trae imágenes
    CC->>MCP: resource transcription://<id>/images/<image_id>
    MCP->>PG: SELECT images WHERE id=... AND user_id=...
    PG-->>MCP: metadata + file_path
    MCP->>FS: read file_path
    FS-->>MCP: binario imagen
    MCP-->>CC: imagen como resource (multimodal)
    Note over CC: Claude compone la minuta usando transcript + imágenes
    CC-->>U: minuta en texto/Markdown
```

## 7. Camino alternativo / errores

| Condición | Manejo |
|---|---|
| User pide minuta sin especificar reunión y tiene 0 transcripciones | `list_my_transcriptions` retorna []; Claude lo dice al user |
| `transcription_id` referenciado no existe o no es del user | `TRANSCRIPTION_NOT_FOUND` (404); Claude reporta el error al user |
| Transcript ambiguo (varios matches con la query) | Claude lista los candidatos al user para que elija |
| Imagen referenciada no existe o no es del user | `IMAGE_NOT_FOUND`; Claude continúa con el resto del contexto |
| Error de generación en Claude (context window exceeded, rate limit) | El user lo ve en su Claude; puede pedir minuta más corta o partir la reunión |
| MCP server unreachable | Claude reporta connection error; user reintenta |

## 8. Slice de arquitectura

Componentes activados (todos READ-ONLY):
- C. MCP Server (tools `list_my_transcriptions`, `search_my_transcriptions`, `get_transcription`; resources `transcription://`).
- G. Persistencia Relacional (lecturas).
- Filesystem blobs (lectura de imágenes).

ADRs aplicables: [ADR-011](../ADR/ADR-011.md), [ADR-012](../ADR/ADR-012.md).

## 9. Touchpoints de datos

**Entidades leídas**: `transcriptions`, `images`. NINGUNA escritura.

**Eventos de log**: `mcp_request_received` (uno por tool call), `transcription_listed`, `transcription_searched`.

**Datos que salen de la intranet**: transcript + metadatos + imágenes binarias viajan por HTTPS al MCP client (Claude Code / Desktop) que corre en la máquina del user. De ahí, Claude (cliente) puede enviar a Anthropic para inferencia. **Esa salida es la única que sale de Sandinas, y ocurre solo por petición explícita del user.**

## 10. RF candidatos

| RF candidato | Cubre |
|---|---|
| RF-MCP-04 | Tool `list_my_transcriptions(limit, offset, sort)`: paginación y ordenamiento |
| RF-MCP-05 | Tool `search_my_transcriptions(query, limit)`: full-text search con tsvector |
| RF-MCP-06 | Tool `get_transcription(transcription_id)`: retorna JSON completo |
| RF-MCP-07 | Resource `transcription://<id>`: lecture-only, idempotente |
| RF-MCP-08 | Resource `transcription://<id>/images/<image_id>`: imagen binaria como MCP resource |

## 11. Cuellos de botella, riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| Transcripts grandes exceden context window de Claude | Tools soportan `offset` o segmentación por tiempo (futuro); por ahora user puede pedir resumen progresivo |
| Privacy: el user envía transcript a Anthropic implícitamente | Documentar claramente: "al pedirle a tu Claude que genere la minuta, los datos viajan a Anthropic"; cumple porque es decisión consciente del user |
| Costos en tokens del Claude del user | Es decisión del user; no afecta al backend |
| Cross-user data leak (Claude pide transcript de otro user) | El bearer del MCP filtra; per-user scoping en cada query SQL (`WHERE user_id = ...`) |

## 12. RF handoff checklist

- [x] Actores y ownership explícitos.
- [x] Diagrama mermaid del camino principal.
- [x] Errores documentados.
- [x] Eventos clave listados.
- [x] Riesgos y mitigaciones explícitos.
- [x] RFs candidatos enumerados.
- [x] No hay decisiones críticas abiertas.
- [x] Listo para `crear-rf`.
