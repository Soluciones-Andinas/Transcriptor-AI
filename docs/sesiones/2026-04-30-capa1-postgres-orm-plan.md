# Plan TDD — Capa 1: PostgreSQL + ORM + Migrations

> **For Claude:** REQUIRED SUB-SKILL: Use `sandinas-dev-workflows:executing-plans` to implement this plan.
> **Workflow:** TDD (Red-Green-Refactor) with atomic commits per batch.

**Spec source:** `docs/sesiones/2026-04-30-capa1-postgres-orm-spec.md` (commit `06ec5c1`)
**Spec ID:** `SPEC-capa1-postgres-orm-v1`
**Branch:** `feat/capa1-postgres-orm` (base: `56aced4`)
**Goal:** Stand up SQLAlchemy 2.0 async + asyncpg + Alembic over Postgres 16 with the 6 tables and indexes defined in `wiki/05_modelo_datos.md`, integrated into FastAPI lifespan + `/health`, validated end-to-end by `docker compose up`.

**Tech stack:** Python 3.10/3.11, SQLAlchemy 2.0 (async), asyncpg, Alembic, greenlet, FastAPI, pytest + pytest-asyncio + testcontainers[postgres], httpx, ruff.

**Test strategy:**
- 15 acceptance criteria → 15 integration tests (testcontainers spins up real Postgres 16-alpine).
- 5 secondary flow / error scenarios → 3 dedicated tests + 2 documented in code comments.
- Total: ~18 test functions across 5 test files.
- Unit tests (no DB) only where they add value over an integration test (none expected for Capa 1).

---

## Test Mapping

| # | AC / ERR | Acceptance Criterion | Test Function | File |
|---|---|---|---|---|
| 1 | AC-1 | Module `transcription_api.db` importable | `test_db_module_imports` | `tests/integration/test_db_module.py` |
| 2 | AC-2 | 6 models declared with correct `__tablename__` + typed `Mapped` columns + `ondelete="CASCADE"` | `test_models_metadata` | `tests/integration/test_db_module.py` |
| 3 | AC-7 | Engine + session factory derived from `settings` | `test_engine_from_settings` | `tests/integration/test_db_module.py` |
| 4 | AC-3 | Alembic initialized; `alembic check` exit 0 | `test_alembic_config` | `tests/integration/test_alembic.py` |
| 5 | AC-4 | `alembic revision --autogenerate` produces `op.create_table()` × 6 + indexes | `test_initial_migration_content` | `tests/integration/test_alembic.py` |
| 6 | AC-5 | Schema in DB after `alembic upgrade head` matches wiki §2 (column types, JSONB, BYTEA, NUMERIC, TIMESTAMPTZ) | `test_schema_columns_match_wiki` | `tests/integration/test_db_schema.py` |
| 7 | AC-6 | Indexes from wiki §2 present in `pg_indexes` (GIN tsvector spanish, partial UNIQUE on `mcp_bearers`, composite per-user, `audio_hash`, nonce, status+expires) | `test_indexes_match_wiki` | `tests/integration/test_db_schema.py` |
| 8 | AC-8 | FastAPI lifespan creates `app.state.engine` at startup; `engine.dispose()` called at shutdown | `test_lifespan_manages_engine` | `tests/integration/test_lifespan.py` |
| 9 | AC-9 + ERR-1 | `/health` returns `db_reachable: true` when DB up; `db_reachable: false` (still 200) when DB down | `test_health_db_reachable_true` + `test_health_db_reachable_false` | `tests/integration/test_health_endpoint.py` |
| 10 | AC-10 | CRUD round-trip for each of 6 models | `test_crud_<model>` × 6 (parametrized) | `tests/integration/test_db_models_crud.py` |
| 11 | AC-11 | `select(Transcription).where(user_id == A.id)` returns only A's data | `test_per_user_scoping_transcriptions` | `tests/integration/test_db_per_user_scoping.py` |
| 12 | AC-12 | Deleting a `User` cascades to dependent rows in 5 child tables | `test_cascade_delete_user` | `tests/integration/test_db_per_user_scoping.py` |
| 13 | AC-13 | Two `mcp_bearers` rows with `revoked_at IS NULL` for same user → `IntegrityError` | `test_partial_unique_active_bearer` | `tests/integration/test_db_constraints.py` |
| 14 | AC-14 | Spanish FTS query matches transcript text via `to_tsvector('spanish', text) @@ plainto_tsquery('spanish', q)` | `test_spanish_fulltext_search` | `tests/integration/test_db_constraints.py` |
| 15 | AC-15 + ALT-1 | `docker compose up --build -d` → both containers healthy → `curl /health` returns `db_reachable: true` (Alembic auto-runs at boot) | `test_compose_e2e_health` (manual smoke + script) | `tests/e2e/test_compose_e2e.sh` |
| 16 | ERR-2 | Pool exhausted documented behavior | doc-only (commented in `db/session.py`) | n/a |
| 17 | ERR-3 | Migration drift policy | doc-only (commit message + plan note) | n/a |
| 18 | ERR-4 | testcontainers Docker availability | `@pytest.mark.requires_docker` marker setup | `tests/conftest.py` |
| 19 | ERR-5 | Async URL → sync URL conversion for Alembic | covered indirectly by AC-3 + AC-4 (test ensures alembic runs) | n/a |
| 20 | ALT-2 | SQLAlchemy naming convention applied | covered indirectly by AC-4 (autogenerate uses `pk_*`, `ix_*`, `fk_*`) | n/a |

---

## Batch Plan

| Batch | Tasks | ACs | Goal |
|---|---|---|---|
| **B1 — Foundation** | T1, T2, T3 | AC-1, AC-2, AC-7 | `db/` module: Base + 6 models + engine/session from settings |
| **B2 — Alembic + migration** | T4, T5, T6 | AC-3, AC-4, AC-5 | Alembic init, autogenerate initial migration, schema columns verified in real DB |
| **B3 — Indexes** | T7 | AC-6 | All wiki indexes (GIN tsvector, partial UNIQUE, composites) present after `upgrade head` |
| **B4 — FastAPI integration** | T8, T9 | AC-8, AC-9, ERR-1 | Lifespan owns engine; `/health` reports DB reachability without crashing |
| **B5 — Behaviors** | T10, T11, T12 | AC-10, AC-11, AC-12 | CRUD + per-user scoping + cascade delete |
| **B6 — Constraints + E2E** | T13, T14, T15 | AC-13, AC-14, AC-15 | Partial UNIQUE, Spanish FTS, docker compose smoke |

Stop and report after each batch. Wait for "continue" before next batch.

---

## Tasks

### Task T1 — `db/` module skeleton (AC-1)

**Source:** SPEC-capa1-postgres-orm-v1, AC-1
**Criterion:** `python -c "from transcription_api.db import engine, async_session_factory, get_session"` exits 0.

**Files:**
- Test: `tests/integration/test_db_module.py::test_db_module_imports`
- Impl: `src/transcription_api/db/__init__.py`, `db/base.py`, `db/session.py`

**RED:**
```python
def test_db_module_imports():
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-1 — module is importable and exposes engine, async_session_factory, get_session.
    """
    from transcription_api.db import engine, async_session_factory, get_session
    from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

    assert isinstance(engine, AsyncEngine)
    assert isinstance(async_session_factory, async_sessionmaker)
    assert callable(get_session)
```
Run: `pytest tests/integration/test_db_module.py::test_db_module_imports` → MUST FAIL (ModuleNotFoundError).

**GREEN:** Create `db/base.py` (DeclarativeBase + naming convention), `db/session.py` (engine + sessionmaker + `get_session` async generator), `db/__init__.py` (re-exports).

**Commit RED → GREEN:**
- `test(db): SPEC-capa1 AC-1 — db module importable`
- `feat(db): SPEC-capa1 AC-1 — base + session + __init__`

---

### Task T2 — 6 ORM models (AC-2)

**Source:** SPEC-capa1-postgres-orm-v1, AC-2
**Criterion:** All 6 models exist, with exact `__tablename__`, typed `Mapped[T]` columns, FKs with `ondelete="CASCADE"` per wiki §2.

**Files:**
- Test: `tests/integration/test_db_module.py::test_models_metadata`
- Impl: `db/models/{user,oauth_token,mcp_bearer,transcription,image,upload_session}.py` + `db/models/__init__.py`

**RED:**
```python
@pytest.mark.parametrize("model_name,expected_tablename", [
    ("User", "users"),
    ("OAuthToken", "oauth_tokens"),
    ("McpBearer", "mcp_bearers"),
    ("Transcription", "transcriptions"),
    ("Image", "images"),
    ("UploadSession", "upload_sessions"),
])
def test_models_metadata(model_name, expected_tablename):
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-2 — models declared with correct tablename and FK cascade behavior.
    """
    import transcription_api.db.models as models
    model = getattr(models, model_name)
    assert model.__tablename__ == expected_tablename
    # All FKs to users must cascade
    for fk in (c.foreign_keys for c in model.__table__.columns):
        for fkc in fk:
            if fkc.column.table.name == "users":
                assert fkc.ondelete == "CASCADE", f"{model_name}.{fkc.parent.name} must cascade"
```
RED: ImportError on `transcription_api.db.models`.

**GREEN:** Implement the 6 models per spec Main Flow §3. Each in its own file. JSONB via `sqlalchemy.dialects.postgresql.JSONB`. `BYTEA` via `sqlalchemy.LargeBinary`. `NUMERIC(10,2)` via `Numeric(10, 2)`. `created_at` defaults via `server_default=func.now()` with `timezone=True`.

**Commits:**
- `test(db): SPEC-capa1 AC-2 — model metadata expectations`
- `feat(db): SPEC-capa1 AC-2 — 6 ORM models with FK cascade`

---

### Task T3 — Engine + session from settings (AC-7)

**Source:** SPEC-capa1-postgres-orm-v1, AC-7
**Criterion:** `engine.url` is `postgresql+asyncpg://...` derived from `settings.database_url`; `pool_size` and `max_overflow` from `settings.db_pool_*`.

**Files:**
- Test: `tests/integration/test_db_module.py::test_engine_from_settings`
- Impl: `db/session.py` (already present from T1, refine).

**RED:**
```python
def test_engine_from_settings():
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-7 — engine wired to settings, asyncpg dialect, pool sizing applied.
    """
    from transcription_api.db import engine
    from transcription_api.config import settings

    assert engine.url.drivername == "postgresql+asyncpg"
    assert str(engine.url) == settings.database_url.replace(
        settings.postgres_password.get_secret_value(), "***"
    ) or engine.url.render_as_string(hide_password=False) == settings.database_url
    assert engine.pool.size() == settings.db_pool_size
```
RED: engine pool size hardcoded or url scheme wrong.

**GREEN:** `create_async_engine(settings.database_url, pool_size=settings.db_pool_size, max_overflow=settings.db_pool_max_overflow, pool_pre_ping=True)`.

**Commits:**
- `test(db): SPEC-capa1 AC-7 — engine reads pool size from settings`
- `feat(db): SPEC-capa1 AC-7 — engine wired to settings`

---

**END BATCH 1 — STOP, report, await "continue".**

---

### Task T4 — Alembic init (AC-3)

**Source:** SPEC-capa1-postgres-orm-v1, AC-3 + ERR-5
**Criterion:** `alembic check` (or `alembic heads`) exits 0; `env.py` loads `Base.metadata`.

**Files:**
- Test: `tests/integration/test_alembic.py::test_alembic_config`
- Impl: `alembic.ini`, `alembic/env.py`, `alembic/script.py.mako`, `alembic/versions/.gitkeep`

**RED:** test invokes `alembic.config.Config("alembic.ini")` and asserts `target_metadata is Base.metadata`. RED: file does not exist.

**GREEN:**
1. Run `alembic init alembic` from repo root (creates folder + ini).
2. Edit `alembic/env.py`:
   - Import `Base` from `transcription_api.db.base`.
   - Import all model modules so SQLAlchemy registers them on `Base.metadata`.
   - `target_metadata = Base.metadata`.
   - `def get_url() -> str: return settings.database_url.replace("+asyncpg", "+psycopg")` (sync dialect for Alembic per ERR-5).
   - In `run_migrations_online()` use `engine_from_config(...)` with `get_url()`.
3. Edit `alembic.ini`: `script_location = alembic`, blank `sqlalchemy.url`.

**Commits:**
- `test(db): SPEC-capa1 AC-3 — alembic config loadable`
- `feat(db): SPEC-capa1 AC-3 — alembic init with Base.metadata + sync URL`

> Note ERR-5: Alembic does not support async URLs cleanly in `online` mode; we convert `+asyncpg` → `+psycopg` only inside `env.py`. The runtime engine stays async; only Alembic's migration runner uses sync.

---

### Task T5 — Initial migration autogenerate (AC-4 + ALT-2)

**Source:** SPEC-capa1-postgres-orm-v1, AC-4 + ALT-2 (naming convention)
**Criterion:** `alembic revision --autogenerate -m "initial schema"` produces a single migration file with `op.create_table()` × 6 plus all indexes from wiki §2.

**Files:**
- Test: `tests/integration/test_alembic.py::test_initial_migration_content`
- Impl: `alembic/versions/<timestamp>_initial_schema.py` (autogenerated, then edited if autogenerate misses indexes).

**RED:** test reads the migration file and asserts it contains 6 `create_table` calls and the GIN tsvector index. RED: no migration file yet.

**GREEN:**
1. Spin up a throwaway empty Postgres (testcontainer) for autogenerate to compare against.
2. Run `alembic revision --autogenerate -m "initial schema"`.
3. Inspect output. Almost certainly need to manually add:
   - GIN tsvector index: `op.execute("CREATE INDEX idx_transcriptions_text_fts ON transcriptions USING gin (to_tsvector('spanish', text))")` (autogenerate cannot infer functional indexes).
   - Partial UNIQUE on `mcp_bearers`: `op.create_index("uq_mcp_bearers_active_per_user", "mcp_bearers", ["user_id"], unique=True, postgresql_where=text("revoked_at IS NULL"))`.
   - Composite `(user_id, created_at DESC) WHERE deleted_at IS NULL` on `transcriptions`.
4. Apply naming convention (ALT-2) on `Base.metadata` so all autogenerated names follow `pk_*`, `ix_*`, `fk_*`, `uq_*`, `ck_*`.

**Commits:**
- `test(db): SPEC-capa1 AC-4 — initial migration shape`
- `feat(db): SPEC-capa1 AC-4+ALT-2 — autogenerate + manual GIN/partial-UNIQUE/composite + naming convention`

---

### Task T6 — Schema column types match wiki §2 (AC-5)

**Source:** SPEC-capa1-postgres-orm-v1, AC-5
**Criterion:** After `alembic upgrade head` against a real Postgres, `information_schema.columns` shows `jsonb` for `transcriptions.segments` and `metadata`, `bytea` for `oauth_tokens.ms_*_encrypted`, `numeric(10,2)` for `transcriptions.duration_seconds`, `timestamp with time zone` for `created_at` columns.

**Files:**
- Test: `tests/integration/test_db_schema.py::test_schema_columns_match_wiki`
- Impl: refinements to `db/models/*.py` if RED reveals divergence.
- Fixture: `tests/conftest.py::pg_url`, `engine`, `apply_migrations` (testcontainer + `alembic upgrade head`).

**RED:** test queries `information_schema.columns WHERE table_name IN (...)` and asserts each column's `data_type` and `udt_name`. RED: testcontainer fixture missing OR migration applies but type mismatches surface.

**GREEN:** Implement `tests/conftest.py` with:
```python
@pytest.fixture(scope="session")
def pg_url() -> str:
    with PostgresContainer("postgres:16-alpine") as pg:
        url = pg.get_connection_url().replace("psycopg2", "asyncpg")
        # apply alembic upgrade head against the sync URL
        sync_url = url.replace("+asyncpg", "+psycopg")
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", sync_url)
        command.upgrade(config, "head")
        yield url
```
Fix any model dialect mismatch caught by the test.

**Commits:**
- `test(db): SPEC-capa1 AC-5 — schema columns match wiki §2`
- `feat(db): SPEC-capa1 AC-5 — testcontainer fixture + dialect fixes if any`

---

**END BATCH 2 — STOP, report, await "continue".**

---

### Task T7 — Indexes match wiki §2 (AC-6)

**Source:** SPEC-capa1-postgres-orm-v1, AC-6
**Criterion:** `pg_indexes` query returns exactly: composite per-user partial on transcriptions, GIN tsvector spanish, audio_hash, mcp_bearers token_hash UNIQUE, mcp_bearers active partial UNIQUE, upload_sessions nonce UNIQUE, upload_sessions (status, expires_at).

**Files:**
- Test: `tests/integration/test_db_schema.py::test_indexes_match_wiki`
- Impl: migration adjustments if any are missing.

**RED:**
```python
def test_indexes_match_wiki(engine):
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-6 — all wiki §2 indexes present and of correct type.
    """
    expected = {
        "idx_transcriptions_user_created": ("transcriptions", "btree", "deleted_at IS NULL"),
        "idx_transcriptions_text_fts": ("transcriptions", "gin", None),
        "idx_transcriptions_audio_hash": ("transcriptions", "btree", None),
        "uq_mcp_bearers_token_hash": ("mcp_bearers", "btree", None),  # unique
        "uq_mcp_bearers_active_per_user": ("mcp_bearers", "btree", "revoked_at IS NULL"),
        "uq_upload_sessions_nonce": ("upload_sessions", "btree", None),
        "idx_upload_sessions_status_expires": ("upload_sessions", "btree", None),
    }
    async with engine.connect() as conn:
        rows = await conn.execute(text(
            "SELECT indexname, tablename, indexdef FROM pg_indexes WHERE schemaname='public'"
        ))
        actual = {r.indexname: (r.tablename, r.indexdef) for r in rows}
    for name, (table, kind, where) in expected.items():
        assert name in actual, f"missing index {name}"
        # ... assert kind in actual[name][1].lower(), where in actual[name][1] if where ...
```
RED: indexes not present (autogenerate misses them).

**GREEN:** Add missing `op.create_index(...)` / `op.execute(...)` calls in the initial migration. If model-level `Index(...)` in `__table_args__` covers it for next migrations, add there too.

**Commits:**
- `test(db): SPEC-capa1 AC-6 — wiki §2 indexes`
- `feat(db): SPEC-capa1 AC-6 — complete migration with GIN/partial/composite indexes`

---

**END BATCH 3 — STOP, report, await "continue".**

---

### Task T8 — FastAPI lifespan owns engine (AC-8)

**Source:** SPEC-capa1-postgres-orm-v1, AC-8
**Criterion:** `app.state.engine` is `AsyncEngine` after startup; `engine.dispose()` invoked at shutdown.

**Files:**
- Test: `tests/integration/test_lifespan.py::test_lifespan_manages_engine`
- Impl: `src/transcription_api/main.py` (refactor existing `app` to use `lifespan` ctx manager).

**RED:**
```python
async def test_lifespan_manages_engine():
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-8 — lifespan creates engine on startup, disposes on shutdown.
    """
    from transcription_api.main import app
    async with LifespanManager(app):
        assert isinstance(app.state.engine, AsyncEngine)
        # Disposal is verified by spying on engine.dispose
```
RED: `app.state.engine` does not exist.

**GREEN:** Convert `main.py` to use `@asynccontextmanager async def lifespan(app)` that assigns `app.state.engine = engine` and calls `await engine.dispose()` on exit.

**Commits:**
- `test(api): SPEC-capa1 AC-8 — lifespan owns engine`
- `feat(api): SPEC-capa1 AC-8 — FastAPI lifespan with engine lifecycle`

---

### Task T9 — `/health` reports DB reachability (AC-9 + ERR-1)

**Source:** SPEC-capa1-postgres-orm-v1, AC-9 + ERR-1
**Criterion:** `GET /health` returns 200 with `db_reachable: true` when DB up; 200 with `db_reachable: false` when DB down (no crash, no 503 — `/health` must always answer).

**Files:**
- Test: `tests/integration/test_health_endpoint.py::test_health_db_reachable_true`, `::test_health_db_reachable_false`
- Impl: `src/transcription_api/main.py` `/health` handler.

**RED (true case):**
```python
async def test_health_db_reachable_true(httpx_client):
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-9 — /health reports db_reachable=true when SELECT 1 succeeds.
    """
    r = await httpx_client.get("/health")
    assert r.status_code == 200
    assert r.json()["db_reachable"] is True
```

**RED (false case):**
```python
async def test_health_db_reachable_false(monkeypatch, httpx_client):
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-9 + ERR-1 — /health does NOT crash when DB is unreachable; reports db_reachable=false.
    """
    async def boom(*a, **kw):
        raise OperationalError("connection refused", None, None)
    monkeypatch.setattr("sqlalchemy.ext.asyncio.AsyncConnection.execute", boom)
    r = await httpx_client.get("/health")
    assert r.status_code == 200
    assert r.json()["db_reachable"] is False
```

**GREEN:**
```python
@app.get("/health")
async def health():
    db_reachable = False
    try:
        async with app.state.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        db_reachable = True
    except Exception:
        logger.warning("db_unreachable_at_health_check", exc_info=True)
    return {"status": "ok", "db_reachable": db_reachable}
```

**Commits:**
- `test(api): SPEC-capa1 AC-9 — /health DB ping (up + down)`
- `feat(api): SPEC-capa1 AC-9+ERR-1 — /health reports db_reachable, never crashes`

---

**END BATCH 4 — STOP, report, await "continue".**

---

### Task T10 — CRUD round-trip per model (AC-10)

**Source:** SPEC-capa1-postgres-orm-v1, AC-10
**Criterion:** For each of 6 models, INSERT + SELECT round-trip succeeds.

**Files:**
- Test: `tests/integration/test_db_models_crud.py::test_crud_<model>` (parametrized over factories).
- Impl: helpers in `tests/factories.py` (test-only).

**RED:** Parametrized test inserting one row per model and reading back. RED: factories missing.

**GREEN:** Write minimal factory helpers (no Faker dependency — hardcoded valid values per model). Tests should pass *as-is* if T1–T7 done correctly; if not, the failure points to a model misdeclaration.

**Commits:**
- `test(db): SPEC-capa1 AC-10 — CRUD round-trip per model`
- `chore(tests): SPEC-capa1 AC-10 — minimal model factories`

---

### Task T11 — Per-user scoping (AC-11)

**Source:** SPEC-capa1-postgres-orm-v1, AC-11
**Criterion:** Filter by `user_id` returns only that user's rows.

**Files:**
- Test: `tests/integration/test_db_per_user_scoping.py::test_per_user_scoping_transcriptions`

**RED:**
```python
async def test_per_user_scoping_transcriptions(session):
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-11 — user_id WHERE filter isolates per-user data.
    """
    a = await make_user(session, email="a@x")
    b = await make_user(session, email="b@x")
    await make_transcription(session, user_id=a.id, audio_hash="h-a")
    await make_transcription(session, user_id=b.id, audio_hash="h-b")
    rows = (await session.execute(
        select(Transcription).where(Transcription.user_id == a.id)
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].audio_hash == "h-a"
```
GREEN: passes if T2 + T6 correct (this is an invariant probe, not new code).

**Commits:**
- `test(db): SPEC-capa1 AC-11 — per-user scoping invariant`

---

### Task T12 — Cascade delete (AC-12)

**Source:** SPEC-capa1-postgres-orm-v1, AC-12
**Criterion:** Deleting a `User` cascades to oauth_tokens, mcp_bearers, transcriptions, images, upload_sessions.

**Files:**
- Test: `tests/integration/test_db_per_user_scoping.py::test_cascade_delete_user`

**RED:** Insert one user with one row in each of the 5 child tables; delete the user; expect all child rows gone. RED: any FK without `ondelete="CASCADE"` leaves orphans (or raises FK violation). T2 should have caught this; this test is the runtime confirmation.

**GREEN:** Fix `ondelete="CASCADE"` in any model where T2 missed it.

**Commits:**
- `test(db): SPEC-capa1 AC-12 — cascade delete`
- `fix(db): SPEC-capa1 AC-12 — add missing ondelete=CASCADE` (only if needed)

---

**END BATCH 5 — STOP, report, await "continue".**

---

### Task T13 — Partial UNIQUE on active mcp_bearers (AC-13)

**Source:** SPEC-capa1-postgres-orm-v1, AC-13
**Criterion:** Two `mcp_bearers` rows with `revoked_at IS NULL` for same user → `IntegrityError`.

**Files:**
- Test: `tests/integration/test_db_constraints.py::test_partial_unique_active_bearer`

**RED:**
```python
async def test_partial_unique_active_bearer(session):
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-13 — at most one active (revoked_at IS NULL) bearer per user.
    """
    u = await make_user(session)
    await make_bearer(session, user_id=u.id, token_hash="h1", revoked_at=None)
    with pytest.raises(IntegrityError):
        await make_bearer(session, user_id=u.id, token_hash="h2", revoked_at=None)
        await session.flush()
```

**GREEN:** Already covered if T7 added the partial UNIQUE correctly. If RED passes (no IntegrityError), the index is missing the `WHERE revoked_at IS NULL` clause — fix the migration.

**Commits:**
- `test(db): SPEC-capa1 AC-13 — partial UNIQUE active bearer per user`

---

### Task T14 — Spanish full-text search (AC-14)

**Source:** SPEC-capa1-postgres-orm-v1, AC-14
**Criterion:** `to_tsvector('spanish', text) @@ plainto_tsquery('spanish', q)` matches inserted text.

**Files:**
- Test: `tests/integration/test_db_constraints.py::test_spanish_fulltext_search`

**RED:**
```python
async def test_spanish_fulltext_search(session):
    """
    Spec: SPEC-capa1-postgres-orm-v1
    Criterion: AC-14 — GIN tsvector spanish index supports plainto_tsquery matches.
    """
    u = await make_user(session)
    await make_transcription(session, user_id=u.id, text="Discutimos arquitectura de microservicios y migraciones de base de datos.")
    rows = (await session.execute(
        select(Transcription).where(
            func.to_tsvector("spanish", Transcription.text).op("@@")(
                func.plainto_tsquery("spanish", "arquitectura")
            )
        )
    )).scalars().all()
    assert len(rows) == 1
```

**GREEN:** Already works if T7 added the GIN index. RED reveals if `to_tsvector('spanish', ...)` raises (extension or stemmer missing — `postgres:16-alpine` includes spanish dictionary by default).

**Commits:**
- `test(db): SPEC-capa1 AC-14 — spanish FTS query`

---

### Task T15 — Compose E2E build verification (AC-15 + ALT-1)

**Source:** SPEC-capa1-postgres-orm-v1, AC-15 + ALT-1 (auto-migrations at boot)
**Criterion:** `docker compose up --build -d` → API + Postgres healthy → `curl /health` returns `db_reachable: true`.

**Files:**
- Test: `tests/e2e/test_compose_e2e.sh` (smoke shell script with `pytest` runner via `tests/e2e/test_compose_e2e.py` invoking `subprocess.run`).
- Impl: `Dockerfile` — modify CMD or add `entrypoint.sh` that runs `alembic upgrade head` before `uvicorn`.

**Implementation of ALT-1 (auto-migration on boot):**
```dockerfile
# entrypoint.sh
#!/usr/bin/env bash
set -euo pipefail
alembic upgrade head
exec uvicorn transcription_api.main:app --host 0.0.0.0 --port 8000
```

**RED (smoke):** Run `docker compose down -v`, then `docker compose up --build -d`, wait for healthcheck, curl `/health`. Expect 200 + `db_reachable: true`. RED: container fails because `alembic upgrade head` errors (path, URL, or migration drift).

**GREEN:** Adjust Dockerfile/entrypoint until container starts cleanly and `/health` reports DB reachable.

**Commits:**
- `test(e2e): SPEC-capa1 AC-15 — compose smoke script`
- `feat(infra): SPEC-capa1 AC-15+ALT-1 — entrypoint runs alembic upgrade head before uvicorn`

---

**END BATCH 6 — final report, run `/trazabilidad`.**

---

## Traceability Matrix

| Spec | Criterion | Test Function | Status |
|---|---|---|---|
| SPEC-capa1 | AC-1 | `test_db_module_imports` | [ ] |
| SPEC-capa1 | AC-2 | `test_models_metadata` (×6 parametrized) | [ ] |
| SPEC-capa1 | AC-3 | `test_alembic_config` | [ ] |
| SPEC-capa1 | AC-4 | `test_initial_migration_content` | [ ] |
| SPEC-capa1 | AC-5 | `test_schema_columns_match_wiki` | [ ] |
| SPEC-capa1 | AC-6 | `test_indexes_match_wiki` | [ ] |
| SPEC-capa1 | AC-7 | `test_engine_from_settings` | [ ] |
| SPEC-capa1 | AC-8 | `test_lifespan_manages_engine` | [ ] |
| SPEC-capa1 | AC-9 | `test_health_db_reachable_true` + `_false` | [ ] |
| SPEC-capa1 | AC-10 | `test_crud_<model>` (×6) | [ ] |
| SPEC-capa1 | AC-11 | `test_per_user_scoping_transcriptions` | [ ] |
| SPEC-capa1 | AC-12 | `test_cascade_delete_user` | [ ] |
| SPEC-capa1 | AC-13 | `test_partial_unique_active_bearer` | [ ] |
| SPEC-capa1 | AC-14 | `test_spanish_fulltext_search` | [ ] |
| SPEC-capa1 | AC-15 | `test_compose_e2e` (smoke shell) | [ ] |
| SPEC-capa1 | ERR-1 | `test_health_db_reachable_false` | [ ] |
| SPEC-capa1 | ERR-2 | doc-only in `db/session.py` | [ ] |
| SPEC-capa1 | ERR-3 | doc-only in initial migration commit | [ ] |
| SPEC-capa1 | ERR-4 | `requires_docker` marker in `conftest.py` | [ ] |
| SPEC-capa1 | ERR-5 | covered by AC-3+AC-4 (env.py async→sync URL) | [ ] |
| SPEC-capa1 | ALT-1 | covered by AC-15 (entrypoint) | [ ] |
| SPEC-capa1 | ALT-2 | covered by AC-4 (naming convention in Base.metadata) | [ ] |
| SPEC-capa1 | ALT-3 | NOT covered (deferred to Capa 2 by spec) | n/a |

**Coverage target:** 22/22 in-scope items (excluding ALT-3 which is explicitly deferred). 100%.

---

## Completion Checklist

- [ ] All 15 ACs have at least one passing test.
- [ ] All 4 in-scope ERR/ALT items have explicit handling (test or doc).
- [ ] `pytest tests/integration/` exits 0 against testcontainer Postgres.
- [ ] `docker compose up --build -d` → `curl /health` returns `db_reachable: true`.
- [ ] All commits follow `<type>(<scope>): SPEC-capa1 <AC-id> — <desc>` format.
- [ ] Traceability matrix updated to all `[x]`.
- [ ] After completion: invoke `sandinas-wiki-skills:ps-trazabilidad` to verify wiki ↔ code consistency.
- [ ] After completion: run `/graphify --update` to refresh the knowledge graph.

## Squash / Capa-close Message Template

```
feat(capa1): SPEC-capa1-postgres-orm-v1 — Postgres + ORM + migrations

Implements:
- SQLAlchemy 2.0 async + asyncpg + Alembic + greenlet stack.
- 6 ORM models (users, oauth_tokens, mcp_bearers, transcriptions,
  images, upload_sessions) per wiki/05_modelo_datos.md §2.
- Alembic init + initial migration with all wiki indexes
  (GIN tsvector spanish, partial UNIQUE on mcp_bearers, composite
  per-user partial on transcriptions, audio_hash, nonce, status+expires).
- FastAPI lifespan owns engine; /health pings DB without crashing
  on disconnect (ERR-1 honored).
- Auto-migrations at container boot via entrypoint.sh (ALT-1).

Tests: 18 integration tests + 1 e2e smoke, all green against
testcontainers[postgres] postgres:16-alpine.

Spec: docs/sesiones/2026-04-30-capa1-postgres-orm-spec.md (06ec5c1)
Traceability: 22/22 in-scope items covered (ALT-3 deferred to Capa 2).
```
