# Spec — Capa 1: PostgreSQL + ORM + Migrations

**SPEC ID:** SPEC-capa1-postgres-orm-v1
**Format:** `sandinas-dev-workflows:writing-plans` canonical structure (Input / Output / Main Flow / Acceptance Criteria / Secondary Flows)
**Branch:** `feat/capa1-postgres-orm`
**Backend repo:** `IA-Tasks-Investigación-Estrategia/transcription-api/` (commit base `8c2a13a`, infra prep `56aced4`)

> **Note on TDD adaptation:** la Capa 1 sí tiene código ejecutable (modelos, engine, lifespan). Los tests usan `testcontainers[postgres]` para arrancar Postgres efímero y validar CRUD + scoping per-user. RED = test contra DB real falla porque modelo/migration no existe. GREEN = modelo + migration aplicada; test pasa.

---

## Input

- `wiki_data_model_path`: `wiki/05_modelo_datos.md` — fuente de verdad del schema (6 tablas con tipos, constraints, índices, invariantes).
- `wiki_adr_008_path`: `wiki/ADR/ADR-008.md` — decisión de Postgres y división Postgres/filesystem.
- `wiki_rf_modules`: `wiki/RF/RF-{AUTH,MCP,TRX,IMG}.md` — tools/queries que consumen los modelos (per-user scoping invariante).
- `decided_orm`: SQLAlchemy 2.0 (async) + asyncpg + Alembic + greenlet.
- `existing_state`:
  - `pyproject.toml` ya tiene las deps (commit `56aced4`).
  - `.env.example` ya tiene `POSTGRES_*` y `DB_POOL_*`.
  - `src/transcription_api/config.py` ya expone `settings.database_url` (computed).
  - `docker-compose.yml` ya tiene servicio `postgres:16-alpine` con healthcheck.
- `target_files`:
  - `src/transcription_api/db/` (módulo nuevo).
  - `alembic/` y `alembic.ini` (carpeta y config nuevas en raíz del proyecto).
  - Modificaciones puntuales a `src/transcription_api/main.py` (lifespan + `/health`).
  - `tests/integration/test_db.py` (nuevo).

## Output

Working tree en `feat/capa1-postgres-orm` con los siguientes artefactos creados o modificados, todos commiteados con conventional commits:

**Módulo nuevo `src/transcription_api/db/`:**

| Archivo | Tipo | Contenido |
|---|---|---|
| `db/__init__.py` | Python | export `engine`, `async_session_factory`, `get_session` |
| `db/base.py` | Python | `DeclarativeBase` con metadata + naming convention para FKs/indexes/constraints; tipos comunes (`UUIDMixin`, `TimestampMixin` opcional) |
| `db/session.py` | Python | `create_async_engine` con pool sizing desde `settings`; `async_sessionmaker`; `get_session()` async generator dependency para FastAPI |
| `db/models/__init__.py` | Python | re-exports de los 6 modelos |
| `db/models/user.py` | Python | `User` table |
| `db/models/oauth_token.py` | Python | `OAuthToken` |
| `db/models/mcp_bearer.py` | Python | `McpBearer` con índice parcial UNIQUE WHERE `revoked_at IS NULL` |
| `db/models/transcription.py` | Python | `Transcription` con JSONB en `segments`/`metadata`, GIN index sobre tsvector spanish, índice composite per-user |
| `db/models/image.py` | Python | `Image` |
| `db/models/upload_session.py` | Python | `UploadSession` con índice sobre (`status`, `expires_at`) |

**Carpeta `alembic/` (en raíz del proyecto):**

| Archivo | Tipo | Contenido |
|---|---|---|
| `alembic.ini` | INI | config de Alembic, apunta a `alembic/` script_location |
| `alembic/env.py` | Python | usa `settings.database_url`, importa `Base` y `target_metadata = Base.metadata`, configurado para async |
| `alembic/script.py.mako` | Mako | template default de migrations |
| `alembic/versions/<timestamp>_initial_schema.py` | Python | migration generada con autogenerate; crea las 6 tablas, índices, constraints |

**Modificaciones a archivos existentes:**

| Archivo | Cambio |
|---|---|
| `src/transcription_api/main.py` | Lifespan: crea `engine` al startup, `await engine.dispose()` al shutdown. `/health` agrega ping `SELECT 1` y reporta `db_reachable`. |
| `Dockerfile` | Añadir `RUN alembic upgrade head` o equivalente al CMD/entrypoint (correr migrations al startup). Investigar si conviene migration-on-boot o como step manual. |

**Tests nuevos en `tests/`:**

| Archivo | Tipo | Contenido |
|---|---|---|
| `tests/conftest.py` | pytest | fixture `pg_container` con `testcontainers[postgres]`; `engine`/`session` por test; aplicar Alembic upgrade head antes de tests |
| `tests/integration/test_db_schema.py` | pytest | valida que el schema en la DB tenga las 6 tablas con tipos correctos (incluyendo JSONB y tsvector index) tras aplicar la migration |
| `tests/integration/test_db_models_crud.py` | pytest | CRUD básico por modelo: insert + select |
| `tests/integration/test_db_per_user_scoping.py` | pytest | crear 2 users; insertar transcripciones para cada uno; validar que un query con filter `user_id = A` no retorna datos del user B |
| `tests/integration/test_health_endpoint.py` | pytest | `httpx.AsyncClient` contra la app; `/health` con DB reachable retorna `db_reachable: true` y status 200 |

## Main Flow

1. **Branch verificado**: `feat/capa1-postgres-orm` ya creado y con commit `56aced4` aplicado.
2. **Definir base ORM** (`db/base.py` + `db/session.py` + `db/__init__.py`):
   1. `DeclarativeBase` subclass con `metadata` + naming convention.
   2. `create_async_engine(settings.database_url, pool_size=..., max_overflow=...)`.
   3. `async_sessionmaker` con `expire_on_commit=False`.
   4. `get_session()` async generator dependency para FastAPI.
3. **Definir 6 modelos** uno por archivo en `db/models/`:
   1. `User` con `microsoft_oid` UNIQUE, `email`, `display_name`, `created_at`, `last_login_at`.
   2. `OAuthToken` con `user_id` FK CASCADE, `ms_access_token_encrypted` BYTEA, `ms_refresh_token_encrypted` BYTEA, `ms_access_expires_at`, índice UNIQUE en `user_id`.
   3. `McpBearer` con `user_id` FK CASCADE, `token_hash` UNIQUE, `name` nullable, `last_used_at` nullable, `revoked_at` nullable, **índice parcial** UNIQUE WHERE `revoked_at IS NULL` (un único activo por user).
   4. `Transcription` con `user_id` FK CASCADE, `audio_hash`, `original_filename`, `original_size_bytes`, `duration_seconds NUMERIC(10,2)`, `language`, `num_speakers`, `text`, `segments JSONB`, `metadata JSONB`, `created_at`, `deleted_at` nullable.
      - Índice composite `(user_id, created_at DESC) WHERE deleted_at IS NULL`.
      - Índice GIN sobre `to_tsvector('spanish', text)` para full-text search.
      - Índice secundario sobre `audio_hash` (para idempotencia + analytics).
   5. `Image` con `transcription_id` FK CASCADE, `user_id` FK CASCADE, `filename`, `caption` nullable, `mime_type`, `size_bytes`, `file_path`, `created_at`, `deleted_at` nullable.
   6. `UploadSession` con `user_id` FK CASCADE, `bearer_id` FK, `nonce` UNIQUE, `kind` ('audio'|'image'), `transcription_id` nullable, `expected_size_bytes`, `expected_mime_type` nullable, `status`, `expires_at`, `created_at`, `uploaded_at` nullable, `consumed_at` nullable.
      - Índice `(status, expires_at)` para cleanup eficiente.
4. **Inicializar Alembic**:
   1. `alembic init alembic` desde la raíz.
   2. Editar `alembic/env.py`: import `Base` desde `transcription_api.db.base`, import models para que se registren, set `target_metadata = Base.metadata`. Usar `settings.database_url` con conversión sync (alembic no soporta async URL directamente; usar `psycopg` o el equivalente sync de `asyncpg` para alembic — convertir `postgresql+asyncpg://` a `postgresql+psycopg://` solo para alembic, o ejecutar alembic con asyncio loop).
   3. Editar `alembic.ini` para `script_location = alembic` y `sqlalchemy.url = ` (vacío, override en env.py desde settings).
5. **Generar migration inicial**:
   1. `alembic revision --autogenerate -m "initial schema"`.
   2. Revisar el diff: que incluya las 6 tablas, índices (especialmente el GIN tsvector y el parcial UNIQUE), constraints, FKs.
   3. Si autogenerate no captura tsvector index o parcial UNIQUE: agregar manualmente en la migration.
6. **Validar la migration en testcontainer**:
   1. Levantar Postgres efímero con testcontainers.
   2. `alembic upgrade head` apuntando a esa instancia.
   3. Inspeccionar `pg_indexes` y `information_schema.columns` para confirmar que los índices y tipos están como esperamos.
7. **Integrar con FastAPI lifespan**:
   1. En `main.py` lifespan: crear `engine` (lazy si hace falta), almacenar en `app.state.engine`.
   2. `await engine.dispose()` al shutdown.
   3. `get_session()` queda en `db/session.py` y se usa como FastAPI dependency.
8. **Actualizar `/health`**:
   1. Inyectar `session` via dependency.
   2. Ejecutar `await session.execute(text("SELECT 1"))`.
   3. Si OK → `db_reachable: true`. Si excepción → `db_reachable: false` y log WARN.
9. **Tests de integración con testcontainers**:
   1. `conftest.py` con fixture `pg_url` (testcontainer) y `engine`/`session` derivados.
   2. `test_db_schema`: aplicar migration, validar tablas + índices + constraints.
   3. `test_db_models_crud`: por cada modelo, INSERT + SELECT.
   4. `test_db_per_user_scoping`: crear users A y B, insertar datos para cada uno, query con filter `user_id == A.id` retorna solo datos de A.
   5. `test_health_endpoint`: `/health` con DB up retorna `db_reachable: true`; con DB down retorna `db_reachable: false` (mockear o stop el container).
10. **Build verification**:
    1. `pip install -e ".[dev]"` resuelve sin conflictos.
    2. `pytest tests/integration/` corre todos los tests verde.
    3. `docker compose up --build` levanta API y Postgres; `curl /health` retorna `db_reachable: true`.

## Acceptance Criteria

- [ ] **AC-1 — Módulo db importable**: `python -c "from transcription_api.db import engine, async_session_factory, get_session"` exits 0 sin errores.
- [ ] **AC-2 — 6 modelos definidos con metadata correcta**: cada modelo tiene `__tablename__` exacto (`users`, `oauth_tokens`, `mcp_bearers`, `transcriptions`, `images`, `upload_sessions`); columnas declaradas tipadas con `Mapped[T]`; FKs con `ondelete="CASCADE"` donde el modelo de datos lo exige.
- [ ] **AC-3 — Alembic inicializado y operativo**: `alembic.ini` apunta a `alembic/`; `alembic/env.py` carga `Base.metadata`; `alembic check` (o `alembic heads`) ejecuta sin errores.
- [ ] **AC-4 — Migration inicial autogenerable**: `alembic revision --autogenerate -m "initial schema"` produce un archivo con `op.create_table(...)` para las 6 tablas + `op.create_index(...)` para los índices documentados en `wiki/05_modelo_datos.md` §2.
- [ ] **AC-5 — Schema en DB tras `alembic upgrade head` matchea wiki §2**: query `information_schema.columns` retorna exactamente los campos definidos. Especialmente: `transcriptions.segments` y `transcriptions.metadata` son `jsonb`; `oauth_tokens.ms_*_encrypted` son `bytea`; `transcriptions.duration_seconds` es `numeric(10,2)`; `created_at` columns son `timestamp with time zone`.
- [ ] **AC-6 — Índices del wiki presentes**: `pg_indexes` retorna (a) `idx_transcriptions_user_created` parcial WHERE `deleted_at IS NULL`, (b) `idx_transcriptions_text_fts` GIN sobre `to_tsvector('spanish', text)`, (c) `idx_transcriptions_audio_hash`, (d) `idx_mcp_bearers_token_hash` UNIQUE, (e) índice parcial UNIQUE en `mcp_bearers WHERE revoked_at IS NULL` (un activo por user), (f) `idx_upload_sessions_nonce` UNIQUE, (g) `idx_upload_sessions_status_expires` `(status, expires_at)`.
- [ ] **AC-7 — Engine + session factory configurados desde settings**: `engine.url` resuelve a `postgresql+asyncpg://...` desde `settings.database_url`; `pool_size` y `max_overflow` desde `settings.db_pool_*`.
- [ ] **AC-8 — FastAPI lifespan gestiona el engine**: tras startup `app.state.engine` existe y es `AsyncEngine`; tras shutdown se llama `await engine.dispose()` (verificable con mock).
- [ ] **AC-9 — `/health` reporta `db_reachable`**: con DB up, `GET /health` retorna 200 con `"db_reachable": true`; con DB down (mockeada), retorna 200 con `"db_reachable": false` (no 503; el endpoint `/health` no debe fallar por DB down — solo reportar).
- [ ] **AC-10 — CRUD básico funciona en cada modelo**: por cada uno de los 6 modelos, `INSERT` + `SELECT` round-trip exitoso en testcontainer.
- [ ] **AC-11 — Per-user scoping funciona**: dado users A y B con sus respectivas transcripciones, una query `select(Transcription).where(Transcription.user_id == A.id)` retorna solo datos de A.
- [ ] **AC-12 — Cascade delete de User borra dependientes**: borrar un user con `DELETE FROM users WHERE id = ?` cascade borra sus rows en `oauth_tokens`, `mcp_bearers`, `transcriptions`, `images`, `upload_sessions` (verificable con SELECT count antes/después).
- [ ] **AC-13 — Constraint UNIQUE parcial en `mcp_bearers`**: insertar dos rows con `revoked_at IS NULL` para el mismo `user_id` falla con `IntegrityError`.
- [ ] **AC-14 — Full-text search Spanish funciona**: dado un transcript con texto "arquitectura microservicios", una query con `func.to_tsvector('spanish', Transcription.text).match(func.plainto_tsquery('spanish', 'arquitectura'))` retorna ese transcript.
- [ ] **AC-15 — Build E2E**: `docker compose up --build -d` levanta ambos containers, healthchecks pasan, `curl http://localhost:8000/health` retorna `db_reachable: true`.

## Secondary Flows / Errors

- **ERR-1 — Conexión DB rechazada al startup**: si `engine.connect()` o `SELECT 1` falla en lifespan → loguear ERROR `db_unreachable_at_startup`; **el lifespan no debe fallar** (deja que /health reporte el problema, no crashea el container). Docker `restart: unless-stopped` sumado al `depends_on: condition: service_healthy` ya cubre el orden de startup.
- **ERR-2 — Pool exhausted bajo carga**: SQLAlchemy levanta `TimeoutError` al pedir conexión. Aceptable acá: el endpoint que pidió la sesión retorna 503 con `error_code: DB_POOL_EXHAUSTED`. No debería suceder al volumen esperado (5 reuniones/día, ≤10 conexiones concurrentes), pero documentado.
- **ERR-3 — Migration drift / autogenerate diff inesperado**: si `alembic revision --autogenerate` produce diffs inesperados (ej: índice que no se debería tocar) → revisar manualmente la migration, ajustar a mano, no commitear sin entender qué generó. Documentar excepciones en el commit message.
- **ERR-4 — testcontainers no disponible (Docker no corriendo en dev machine)**: tests de integración fallan con error claro. Marcar tests con `@pytest.mark.requires_docker` y `pytest --no-cov -m "not requires_docker"` como fallback para correr unit tests sin Docker.
- **ERR-5 — Alembic no soporta async URL directamente**: convertir `postgresql+asyncpg://` a un dialect sync (típicamente `postgresql+psycopg2://` o `postgresql://`) solo para Alembic. Documentar en `env.py` la conversión.
- **ALT-1 — Migration on container boot vs manual**: dos opciones: (a) `entrypoint.sh` que ejecuta `alembic upgrade head` antes de uvicorn; (b) operador corre `docker compose exec api alembic upgrade head` manualmente. Para v0.1.0 elegir (a) (auto-migrations al startup, robusto para deploys frescos). Documentar en `docs/DEPLOYMENT.md` cuando se cree.
- **ALT-2 — `naming convention` en SQLAlchemy**: aplicar naming convention estándar para que Alembic genere nombres consistentes (`pk_<table>`, `fk_<table>_<col>_<reftable>`, `ix_<table>_<col>`, `uq_<table>_<col>`, `ck_<table>_<constraint>`). Esto evita renombrados espurios entre migrations.
- **ALT-3 — Encriptación de tokens (`ms_access_token_encrypted`, `ms_refresh_token_encrypted`)**: la columna es `BYTEA` y la app encripta con `OAUTH_TOKEN_ENC_KEY` antes de persistir. La encriptación misma es responsabilidad de Capa 2 (Auth), no de Capa 1. La columna queda lista pero el helper de encrypt/decrypt se escribe en Capa 2.

---

## Trazabilidad cruzada con la wiki

Cada AC referencia una sección específica de `wiki/`:

| AC | Wiki ref |
|---|---|
| AC-1, AC-7, AC-8 | `wiki/02_arquitectura.md` §3 (componente G — Persistencia Relacional) |
| AC-2, AC-5 | `wiki/05_modelo_datos.md` §2 (schemas exactos) |
| AC-6 | `wiki/05_modelo_datos.md` §2 (cada tabla lista sus índices) |
| AC-9 | `wiki/RF/RF-TRX.md` (RF-TRX-04 menciona `/health`) y diseño general |
| AC-10, AC-11 | `wiki/RF/RF-MCP.md` (per-user scoping invariante en RF-MCP-04..09) |
| AC-12 | `wiki/05_modelo_datos.md` §2 (FKs con `ON DELETE CASCADE`) |
| AC-13 | `wiki/RF/RF-AUTH.md` RF-AUTH-07 (regenerar bearer revoca el activo; un solo activo por user) |
| AC-14 | `wiki/RF/RF-MCP.md` RF-MCP-05 (search con tsvector spanish) |
