# transcription-api — Arquitectura para el equipo (2026-05-11)

> Documento de soporte para reunión con el equipo. Refleja el estado post-audit del 2026-05-11 (Capas 1-4 mergeadas, Capa 5 UI pendiente).

---

## 1. Qué hace el sistema, en una frase

> Recibe audio o video, devuelve transcripción en español con diarización (quién habla cuándo), persistida 24h, expuesta como tools al Claude del usuario para que genere minutas.

**Lo que NO hace**: generar minutas. Eso lo hace el Claude personal del usuario consumiendo nuestros datos vía MCP.

---

## 2. Quién usa el sistema (Context)

```mermaid
flowchart LR
    User[" Usuario Sandinas<br/>(Operador, PM, RRHH...)"]
    Claude["Claude del usuario<br/>(Code o Desktop)"]
    MSEntra["Microsoft Entra ID<br/>(tenant Sandinas)"]

    System[("transcription-api<br/>(rig RTX 4060 Ti<br/>intranet ZeroTier)")]

    User -- "1. login web<br/>(navegador)" --> System
    User -- "2. 'transcribime esto'<br/>(chat)" --> Claude
    Claude -- "3. MCP tools<br/>(bearer per-user)" --> System
    System -- "OAuth 2.0 / OIDC" --> MSEntra
    Claude -- "5. genera minuta<br/>(en el cliente)" --> User

    style System fill:#1f6feb,stroke:#0d419d,color:#fff
    style User fill:#fafafa,stroke:#666
    style Claude fill:#d8b4fe,stroke:#7c3aed
    style MSEntra fill:#0078d4,stroke:#003a70,color:#fff
```

### Decisiones clave de scope

| Decisión | Por qué |
|---|---|
| **Self-hosted en intranet**, no cloud | Privacy. Los audios contienen reuniones internas (RRHH, comercial). Salir a OpenAI/AssemblyAI = data de Sandinas en un proveedor externo. |
| **Microsoft Entra SSO**, no auth propia | Reutiliza la identidad corporativa. Cuando un empleado se va, MS desactiva la cuenta → bearer queda revocado al próximo intento. |
| **MCP-first, REST mínimo** | El consumidor principal es Claude. MCP es nativo para tools/resources con auth por bearer. REST queda solo para upload de blobs (no transportable eficientemente por MCP). |
| **Minutas en el cliente, no en el backend** | El backend solo provee datos. La minuta la construye Claude con sus propios prompts del usuario → cada uno la genera como prefiere sin que el backend tenga que mantener templates. |

---

## 3. Componentes internos (Container)

```mermaid
flowchart TB
    subgraph Cliente["Cliente del usuario"]
        Browser["Navegador<br/>(onboarding web)"]
        Claude["Claude Code/Desktop<br/>+ mcp-remote"]
    end

    subgraph API["FastAPI app — rig intranet"]
        direction TB
        Auth["Auth module<br/>OAuth MS Entra<br/>JWT cookies<br/>Bearer MCP"]
        MCP["MCP Server<br/>(Streamable HTTP)<br/>7 tools + 2 resources"]
        REST["REST<br/>/api/upload<br/>/api/transcriptions"]
        Readiness["Runtime readiness<br/>(modelos cargados?)"]
        Orchestrator["Orchestrator<br/>(global lock 1-concurrent)"]
    end

    subgraph Pipeline["GPU pipeline (lazy-loaded)"]
        direction TB
        Norm["Normalizador<br/>ffmpeg → mono 16kHz s16 PCM"]
        STT["WhisperX large-v3<br/>(int8_float16, ~5-6GB VRAM)"]
        Diar["pyannote 3.1<br/>(speaker-diarization)"]
        Merge["Merge<br/>(segments + speakers + words)"]
    end

    subgraph Storage["Storage local en rig"]
        PG[("Postgres 16<br/>users, transcriptions,<br/>images, oauth_tokens,<br/>mcp_bearers, upload_sessions")]
        FSCache["FS Cache 24h<br/>per-user<br/>SHA-256(PCM)"]
        Uploads["FS Uploads<br/>(efímero, TTL 10min)"]
    end

    Cleanup["Cleanup loop<br/>(cada 1h)"]

    Browser -- "/auth/login → MS Entra → /auth/callback<br/>cookie session JWT" --> Auth
    Browser -- "/auth/me (ve bearer MCP plaintext)" --> Auth
    Claude -- "Authorization: Bearer ...<br/>POST /mcp" --> MCP
    Claude -- "POST /api/upload<br/>(bearer efímero S3-style)" --> REST

    MCP -- "tools del usuario" --> Auth
    MCP -- "scoped queries<br/>WHERE user_id=X" --> PG
    REST -- "valida hash<br/>upload_bearer_hash" --> PG
    REST -- "lanza pipeline" --> Orchestrator
    Orchestrator -- "lock global" --> Pipeline
    Norm --> STT --> Merge
    Norm --> Diar --> Merge
    Pipeline --> FSCache
    Pipeline --> PG
    REST -- "guarda binario" --> Uploads

    Cleanup -- "TTL > 24h: borra" --> FSCache
    Cleanup -- "(D-074 pendiente)<br/>borrar uploads expired" --> Uploads
    Cleanup -- "(D-074 pendiente)<br/>UPDATE upload_sessions" --> PG

    Readiness -- "/health" --> MCP
    Readiness -- "/health" --> REST

    style Cliente fill:#f3f4f6,stroke:#9ca3af
    style API fill:#dbeafe,stroke:#3b82f6
    style Pipeline fill:#fef3c7,stroke:#f59e0b
    style Storage fill:#fce7f3,stroke:#ec4899
    style Cleanup fill:#e0e7ff,stroke:#6366f1
```

### Stack en una tabla

| Capa | Tech | Por qué |
|---|---|---|
| Lenguaje | Python 3.10 | FastAPI + ML libs nativas |
| Web framework | FastAPI 0.115 + Uvicorn 0.32 | Async nativo, OpenAPI, integración con MCP |
| MCP | `mcp[server]` 1.5.x (SDK Anthropic) | Streamable HTTP transport |
| STT | WhisperX 3.8.5 + Whisper large-v3 (`int8_float16`) | Mejor relación WER/VRAM en español; cabe en 8GB junto con pyannote |
| Diarización | pyannote.audio 3.1 (`speaker-diarization-3.1`) | Más preciso que Sortformer en español |
| DB | Postgres 16-alpine | FTS español + GIN tsvector para search |
| Auth | Microsoft Entra ID OAuth 2.0 / OIDC + itsdangerous + cryptography (AES-256-GCM) | SSO corporativo |
| Infra | Docker + nvidia-container-toolkit | GPU pass-through al container |

---

## 4. Flujo 1 — Onboarding del usuario

```mermaid
sequenceDiagram
    autonumber
    actor U as Usuario
    participant B as Navegador
    participant API as transcription-api
    participant MS as MS Entra ID
    participant DB as Postgres

    U->>B: visita /login
    B->>API: GET /auth/login
    API->>API: arma state random + PKCE verifier<br/>set cookie oauth_state
    API->>B: 302 → MS Entra authorize URL
    B->>MS: redirect (login corporativo)
    U->>MS: ingresa email + password
    MS->>B: 302 → /auth/callback?code=...&state=...
    B->>API: GET /auth/callback
    API->>API: valida state cookie ↔ query
    API->>MS: POST /token (exchange code)
    MS->>API: access_token + id_token + refresh_token
    API->>API: valida id_token (JWKS, tenant)
    API->>DB: INSERT user (primer login)<br/>INSERT oauth_tokens (AES-256-GCM)<br/>INSERT mcp_bearers (name='initial')
    API->>B: Set-Cookie session JWT (24h)<br/>302 → /mcp-setup
    Note over API,B: Cookie flash con bearer plaintext<br/>(se ve UNA vez, TTL 60s)
    B->>API: GET /mcp-setup
    API->>B: HTML con mcp_url + bearer<br/>+ snippets Claude Code/Desktop
    U->>U: copia config a ~/.claude.json
```

### Notas para la reunión

- **El bearer MCP se muestra UNA SOLA VEZ** post-login (cookie flash). Si el usuario lo pierde, debe `POST /auth/regenerate-mcp-token` para emitir uno nuevo (revoca el anterior).
- **No usamos session-based auth en el MCP**: cookie del browser ≠ bearer del cliente programático. Son dos canales separados (web vs Claude headless).
- **Per-user scoping fail-closed** (ADR-015/016): cada query ORM contra modelos del usuario inyecta `WHERE user_id=X` automáticamente. Si alguna ruta olvida armar el listener, las queries levantan `ScopingNotArmedError` en vez de leakear filas de otros users.

---

## 5. Flujo 2 — Transcripción end-to-end

```mermaid
sequenceDiagram
    autonumber
    actor U as Usuario
    participant Cl as Claude<br/>(Code/Desktop)
    participant MCP as MCP server
    participant REST as REST /api/upload
    participant Orch as Orchestrator
    participant GPU as Pipeline GPU<br/>(WhisperX + pyannote)
    participant FS as FS Cache
    participant DB as Postgres

    U->>Cl: "transcribime reunion.mp4"
    Cl->>MCP: request_upload_url(<br/>kind="audio", file_size_bytes=...)<br/>Authorization: Bearer <mcp>
    MCP->>DB: INSERT upload_sessions<br/>(status='requested',<br/>upload_bearer_hash=SHA256,<br/>expires_at=now+10min)
    MCP->>Cl: {upload_url, upload_token, expires_at}
    Cl->>REST: POST /api/upload<br/>Authorization: Bearer <upload_token><br/>(binary file)
    REST->>DB: SELECT upload_sessions<br/>+ valida hmac(upload_bearer_hash)
    REST->>REST: persiste binario<br/>(/data/uploads/<id>/original.bin)
    REST->>DB: UPDATE status='uploaded'
    REST->>Cl: 200 OK

    Cl->>MCP: start_transcription(<br/>upload_id, language='es',<br/>min_speakers, max_speakers?)
    MCP->>Orch: orchestrate(upload_id, ...)
    Orch->>Orch: 🔒 acquire global lock<br/>(1-concurrent, timeout 5s)

    rect rgb(255, 251, 235)
    Note over Orch,GPU: GPU pipeline (~30s por minuto de audio)
    Orch->>GPU: normalize ffmpeg<br/>→ mono 16kHz s16 PCM
    Orch->>FS: cache.get(user_id, SHA256(PCM))
    alt Cache HIT
        FS->>Orch: result.json
    else Cache MISS
        Orch->>GPU: whisperx.transcribe(<br/>language='es',<br/>compute_type=int8_float16)
        Orch->>GPU: pyannote.diarize(<br/>min_speakers, max_speakers)
        Orch->>GPU: merge(segments, words, speakers)
        Orch->>FS: cache.put(result.json)
    end
    end

    Orch->>DB: INSERT transcriptions<br/>(text_content, segments_json,<br/>audio_hash, user_id)
    Orch->>Orch: 🔓 release lock (finally)
    MCP->>Cl: {transcription_id, segments,<br/>metadata: {cache_hit, num_speakers, ...}}
    Cl->>U: genera minuta del audio<br/>(prompt del usuario, no del backend)
```

### Notas para la reunión

- **El "rig" hace 1 transcripción a la vez** (lock global, ADR-005). Si llega un segundo request mientras hay otro corriendo, espera hasta 5s; si el lock sigue ocupado, devuelve `GPU_BUSY` con `Retry-After: 600`.
- **Cache es per-user + por hash del PCM** (no del WAV completo). Significa: el mismo audio subido dos veces por el mismo user → segundo es instantáneo. Pero el mismo audio subido por dos users distintos → se procesa dos veces (Privacy).
- **Bearer efímero para uploads** (ADR-017): el bearer MCP del cliente NO viaja al endpoint `/api/upload`. En su lugar, `request_upload_url` emite un secreto distinto con TTL 10min que sí viaja en el upload. Razón: el bearer MCP es de larga duración; si un proxy loguea el header, leak indefinido. El efímero limita el blast radius a 10 minutos.

---

## 6. Estado del proyecto (capas)

```mermaid
gantt
    title Roadmap Capas (2026)
    dateFormat YYYY-MM-DD
    axisFormat %b

    section Backend
    Capa 1 — Postgres + ORM           :done, capa1, 2026-04-30, 7d
    Capa 2 — Auth MS Entra             :done, capa2, after capa1, 7d
    Capa 3 — Pipeline GPU              :done, capa3, after capa2, 5d
    Capa 4 — MCP Server                :done, capa4, after capa3, 4d
    Capa 4 — Deploy rig validado E2E  :done, deploy, 2026-05-08, 1d
    Audit wiki drifts + fixes         :done, audit, 2026-05-11, 1d

    section Pendiente
    Capa 5 — UI Web (React)            :crit, capa5, 2026-05-12, 14d
    Cleanup upload_sessions (D-074)    :crit, d074, 2026-05-12, 2d
    Logs estructurados (D-059)         :crit, d059, after d074, 3d
```

### Capas hechas

| Capa | Qué entregó |
|---|---|
| **Capa 1** | Postgres testcontainer, ORM SQLAlchemy 2.x async, 6 modelos, alembic migrations, per-user scoping listener, FTS español, GIN indexes |
| **Capa 2** | OAuth 2.0 con MS Entra (PKCE, JWKS rotation, tenant guard), JWT session cookies, bearer MCP per-user, regenerate flow, scoping fail-closed (ADR-015) + defensa en capas (ADR-016) |
| **Capa 3** | Dockerfile con CUDA 12.1 + extras pipeline, lifespan que carga modelos, normalize ffmpeg, WhisperX wrapper, pyannote wrapper, merge, orchestrator con lock global, REST endpoints |
| **Capa 4** | MCP server Streamable HTTP, 7 tools (upload, start, get, list, search, delete, user_info), 2 resources, bearer middleware, image upload unificado bajo `request_upload_url(kind="image")`, scoping classification guard (ADR-016) |
| **Audit 2026-05-11** | 40 drifts wiki↔código documentados; 4 CRITICAL cerrados (D-050 ADR-017, D-052 taxonomía errores, D-080 magic-byte fallback, D-081 MCP URL `/mcp`) |

### Capas pendientes

| Capa | Por qué importa |
|---|---|
| **Capa 5 — UI Web** | Hoy el onboarding es CLI-only (el usuario tiene que ver el bearer en una respuesta JSON). RF-UI-01/02 en wiki marcadas `Pendiente Capa 5`. Stack planeado: React 18 + Vite 5 + Tailwind, servida por FastAPI StaticFiles. |
| **D-074 Cleanup uploads** | Drift detectado en audit: si un cliente con bug llama `request_upload_url` sin completar el POST, los bytes en `/data/uploads/` solo se borran al reiniciar el container. Disk leak. |
| **D-059 Logs estructurados** | Wiki declara 14+ eventos contractuales (`auth_login_started`, `stt_completed`, etc.) que el código no emite. Auditabilidad cero hasta que se cierre. |

---

## 7. Decisiones técnicas clave (decision priority)

`Privacy > Simplicity > Transcription Quality > Performance > Cost`

| Decisión | Trade-off |
|---|---|
| **ADR-001**: WhisperX `int8_float16` (no float16) | Cabe en 8GB junto con pyannote. WER ~5% peor que float16, aceptable. |
| **ADR-002**: pyannote 3.1, no Sortformer | Sortformer es 2-3% mejor en inglés pero peor en español (rioplatense específicamente). |
| **ADR-003**: API síncrona, no async + queue | Volumen esperado bajo (10-30 jobs/día). Async = complejidad sin ganancia visible. |
| **ADR-004**: Filesystem cache, no Redis | Disco es 1000× más barato; los archivos JSON son pocos KB; TTL 24h con cleanup periódico cierra el caso. |
| **ADR-005**: Lock global 1-concurrent | 8GB VRAM no permite 2 transcripciones simultáneas. Lock es lo más simple; un cliente que llega cuando hay otro corriendo recibe 503 + Retry-After. |
| **ADR-013 → ADR-017**: Upload con bearer efímero S3-style | Privacy: bearer MCP de larga duración leakable en access logs era riesgo aceptable solo si todos los proxies redactaran `Authorization`. Bearer efímero TTL 10min limita blast radius. |
| **ADR-014 → ADR-015**: Listener fail-closed | Si una ruta olvida armar `user_id` en la sesión, queries levantan `ScopingNotArmedError` en vez de devolver todas las filas. |
| **ADR-016**: Defensa en capas para scoping | Si alguien agrega un modelo sin columna `user_id`, el container no arranca (startup classification guard). |

---

## 8. Para la reunión: 3 conversaciones a tener

1. **¿Cuándo arrancamos Capa 5 (UI web)?** Hoy el onboarding es técnico (copiar bearer de JSON). Si vamos a expandir el uso al equipo no-técnico, la UI es bloqueante.

2. **¿Implementamos D-074 (cleanup uploads) en sprint inmediato o downgradamos a Pendiente Capa 5?** El disk leak es real pero el blast radius depende de cuánto vaya a crecer el uso. Decisión técnica simple, costo bajo.

3. **¿Quiénes van a tener acceso?** El bearer MCP es per-user pero no hay control de quotas. Si N personas del equipo usan la API en paralelo y N > 1, todos comparten el rig (lock global). ¿Está bien? ¿Hay que considerar Cloud Run para escalar después?

---

## Apéndice: documentos relacionados

- Wiki: `wiki/01_alcance_funcional.md`, `wiki/02_arquitectura.md`, `wiki/05_modelo_datos.md`
- ADRs: `wiki/ADR/ADR-001.md` a `ADR-017.md`
- RFs: `wiki/RF/RF-AUTH.md`, `RF-TRX.md`, `RF-CACHE.md`, `RF-MCP.md`, `RF-IMG.md`, `RF-UI.md` (Pendiente Capa 5)
- Drift log: `docs/sesiones/2026-05-05-wiki-drifts.md` (D-001 a D-049) + `docs/sesiones/2026-05-11-wiki-drift-audit.md` (D-050 a D-089)
- Demo previa: `Presentacion/demo-todolist/` (Spec-Driven Development metodología)
