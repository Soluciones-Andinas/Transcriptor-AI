# Prompt para agentes — Capa 4 Batch executor

> **Uso**: copiar el bloque de abajo y pasarlo como `prompt` a la `Agent` tool con
> `subagent_type=general-purpose`. Reemplazar `<<N>>` por el número de batch a ejecutar
> (0..6, donde Batch 0 = migration del schema). Un agente = un batch. Después de cada
> batch, revisar el reporte y decidir si spawnear el siguiente.
>
> **Spawning ejemplo (en Claude Code main session)**:
> ```
> Agent({
>   description: "Execute Capa 4 Batch <<N>>",
>   subagent_type: "general-purpose",
>   prompt: <el bloque de abajo con <<N>> reemplazado>
> })
> ```
>
> **Orden recomendado**:
> 1. Batch 0 → migration `add_upload_bearer_hash` (bloqueante para Batch 2+).
> 2. Batch 1 → MCP server foundation + middleware (bloqueante para Batches 2..5).
> 3. Batches 2..5 → tools / resources / endpoints. Batch 2 antes de Batch 3 (start_transcription consume upload_session).
> 4. Batch 6 → legacy deprecation + smoke checklist (no bloqueante para nada).

---

## Prompt (copiar desde acá hasta el final)

You are executing one batch of a TDD plan for a Python MCP server (FastAPI + FastMCP) that wraps an existing Spanish audio-pipeline service. You do NOT see the prior conversation — everything you need is in this prompt and in the files referenced below.

### 1. Required first action — invoke executing-plans skill

Before any other tool call, invoke the Sandinas team's `executing-plans` skill via the Skill tool:

```
Skill(skill="plugin:sandinas-dev-workflows:executing-plans", args="docs/sesiones/2026-05-06-capa4-mcp-plan.md")
```

If the skill is not available in your runtime, **halt and report**: "executing-plans skill not available in runtime, cannot proceed without team's TDD discipline encoded".

The skill enforces:
- RED → GREEN → REFACTOR cycle per task.
- Atomic commits per RED and per GREEN.
- Traceability matrix updates as ACs close.
- Conventional commit format.

### 2. Mission

Execute **only Batch <<N>>** of the Capa 4 plan. Do NOT proceed to other batches. Pause + exit with a structured report when Batch <<N>> finishes (or you hit a blocker).

### 3. Project context

- **Project**: `transcription-api`. Self-hosted Spanish transcription + diarization service for Soluciones Andinas. FastAPI + WhisperX (Whisper large-v3 int8_float16) + pyannote 3.1 + Postgres. Capa 4 adds an MCP server (Streamable HTTP transport) + chunked upload pattern that wraps the existing Capa 3 pipeline.
- **Working dir**: `/Users/francobertoldi/Documents/Sandinas/IA-Tasks/IA-Tasks-Investigación-Estrategia/transcription-api`. Use absolute paths.
- **Branch**: `feat/capa4-mcp` (already cut from master at `0071c77` post-Capa-3 merge, upstream tracking set). Confirm with `git branch --show-current` before any commit.
- **Stack**: Python 3.10–3.11. FastAPI 0.115+. SQLAlchemy 2.0 async + asyncpg. pytest + pytest-asyncio + respx + testcontainers + asgi-lifespan. **NEW in Capa 4**: `mcp[server]>=1.5,<2.0` (Anthropic official Python SDK; lightweight import, no heavy ML deps).
- **Local dev environment**: `.venv/bin/python -m pytest ...` and `.venv/bin/python -m ruff check src/ tests/` from the working dir. The venv is pre-built with Capa 1+2+3 deps installed. **You must add `mcp[server]>=1.5,<2.0` to `pyproject.toml` core deps in Batch 1 Task 1.2 and pip install it into `.venv` so tests can import the SDK.**
- **GPU**: the developer's local Mac is CPU-only — pipeline tests must mock `pipeline.orchestrator.orchestrate` (the function, not the lock primitive). The actual rig runs Docker images and is out of your reach.
- **Capas previas (1+2+3)**: already merged to master and present in working tree. **Do not modify** `auth/`, `db/` (except `db/models/upload_session.py` which Batch 0 extends with `upload_bearer_hash` column), `pipeline/` (Capa 4 reuses but does not modify), `alembic/` (except adding a single new revision in Batch 0).
- **Storage paths** (already set up in Capa 3): `<DATA_DIR>/uploads/<upload_id>/original.bin` (transient), `<DATA_DIR>/cache/<user_id>/<audio_hash>/result.json` (per-user cache), `<DATA_DIR>/blobs/<user_id>/<transcription_id>/<image_id>.<ext>` (persistent images). `settings.uploads_dir`, `settings.cache_dir`, `settings.blobs_dir` are computed properties already defined.

### 4. Files you must read in this order

1. `docs/sesiones/2026-05-06-capa4-mcp-spec.md` — SPEC-capa4-mcp-v1: 16 ACs, 12 typed errors, 8 ALT flows, decisions cerradas. Memorize the AC IDs, the error codes, and the tool names; they appear verbatim in commit messages and tests.
2. `docs/sesiones/2026-05-06-capa4-mcp-plan.md` — TDD plan: 7 batches (0..6) with task-level RED/GREEN code skeletons. Locate Batch <<N>> and read its full text.
3. `wiki/RF/RF-MCP.md` — autoritative contract surface. Tool signatures, process steps, error codes per RF.
4. `CLAUDE.md` (project root) — governance: skill invocation rules, decision priority (Privacy > Simplicity > Quality > Performance > Cost), language conventions (code English, docs Spanish), graphify-first protocol.
5. `docs/sesiones/2026-05-05-wiki-drifts.md` — drift log. If your work creates a NEW divergence from spec/wiki not yet captured, append a new D-NN entry at the end of "Categoría 9 — Drifts del spec Capa 4" before pushing.

### 5. Definition of "Batch <<N>> complete"

1. **All RED tests** for Batch <<N>>'s tasks written, run with pytest, FAIL as expected, committed (one commit per RED).
2. **All GREEN implementations** written, RED tests now PASS, committed (one commit per GREEN).
3. **Lint passes**: `.venv/bin/python -m ruff check src/transcription_api/ tests/` is clean.
4. **No regressions**: existing Capa 1+2+3 tests still pass — at minimum run:
   ```
   .venv/bin/python -m pytest tests/unit tests/integration -q -m "not e2e and not requires_docker_gpu and not requires_ffmpeg"
   ```
   (Tests requiring Docker daemon, GPU, or ffmpeg binaries are auto-skipped if those resources are unavailable.)
5. **Commits pushed**: `git push origin feat/capa4-mcp` succeeds.
6. **Plan checkboxes flipped**: in `docs/sesiones/2026-05-06-capa4-mcp-plan.md` traceability matrix, fill in the commit hash for ACs closed by this batch.
7. **No `[pending]` work in your scope**: every task in Batch <<N>>'s section has a corresponding commit hash.

### 6. Commit message format

```
<type>(<scope>): SPEC-capa4 <AC-id> — <short description>
```

Types: `test` (RED test), `feat` (GREEN impl), `fix` (regression fix), `refactor`, `docs`, `chore` (deps, deprecation flag, migration). Scope examples: `mcp`, `api`, `db`. Examples:

- `test(mcp): SPEC-capa4 AC-8 — RED test for bearer middleware revoked branch`
- `feat(mcp): SPEC-capa4 AC-1 — request_upload_url tool (audio + image branches)`
- `chore(api): SPEC-capa4 AC-16 — mark POST /api/transcriptions deprecated`
- `feat(db): SPEC-capa4 AC-15 — add upload_bearer_hash column + ORM`

Do NOT add `Co-Authored-By` lines (the user has attribution disabled globally).

### 7. Hard constraints (violating any → halt + report)

- Do NOT skip an AC because it looks redundant.
- Do NOT weaken the Capa 1+2+3 invariants — specifically:
  - The listener `fail-closed` (`db.scoping.ScopingNotArmedError`). Use `with bypass_scoping(session):` for legitimate cross-user operations (auth lookups, admin queries) and document why in a one-line comment.
  - The orchestrator lock (`pipeline.orchestrator._orchestrator_lock`). Capa 4 NEVER touches this primitive directly. The tool `start_transcription` calls `orchestrate(...)` which already wraps acquire/timeout/release.
- Do NOT introduce features outside Batch <<N>>'s scope (e.g., do not preemptively scaffold Batch <<N+1>>'s files).
- Do NOT touch `src/transcription_api/auth/**` (read-only; one exception: if Batch 1 needs a thin factor-out of `auth/mcp_bearer.py::verify_bearer` semantics into a callable usable from MCP context, do the minimal extraction in `auth/mcp_bearer.py` and document why).
- Do NOT touch `src/transcription_api/pipeline/**` (read-only; reuse via imports).
- Do NOT touch `wiki/**` directly. Drift discoveries → `docs/sesiones/2026-05-05-wiki-drifts.md` Categoría 9.
- Do NOT modify `alembic/versions/352c7acf6f15_initial_schema.py` (the initial migration is frozen). Batch 0 creates a NEW revision file.
- Do NOT skip hooks (`--no-verify`) or bypass signing (`--no-gpg-sign`) on commits.
- Do NOT force-push or rewrite history on `feat/capa4-mcp`.
- Do NOT mark a task `completed` if any of: tests failing, partial impl, unresolved import errors.

### 8. Mocking + heavy-deps protocol

The local environment does NOT have `[pipeline]` extras (torch/whisperx/pyannote) installed. The MCP SDK (`mcp[server]>=1.5,<2.0`) IS installed in the venv after Batch 1 Task 1.2 — it is lightweight (no GPU deps).

For Batch <<N>>:

- **Batch 0 (migration)**: tests use `tests/integration/test_alembic.py` patterns that already exist. Use the testcontainers Postgres fixture (`pg_engine`) — auto-skipped if no Docker daemon, that's OK.
- **Batch 1 (MCP foundation)**: imports `from mcp.server.fastmcp import FastMCP` are top-level (the SDK is light). Mocking is for the bearer verification path: use `respx` or fixture override to inject a fake `verify_bearer` if needed; otherwise spin up a real DB row in a testcontainer.
- **Batches 2–5 (tools / endpoints)**: in tests, `unittest.mock.patch` `pipeline.orchestrator.orchestrate` to return canned dicts (do NOT actually run STT or pyannote). The lock acquisition test (AC-9) patches `orchestrate` with an `AsyncMock(side_effect=lambda **kw: asyncio.sleep(<delay>))` to simulate contention.
- **Batch 6 Task 6.2 (E2E rig smoke)**: you do NOT have rig access. Write the procedural checklist into the file `docs/sesiones/2026-05-06-capa4-rig-smoke.md` (template with empty fields for the operator) and note that the user runs it.

### 9. Drift logging discipline

If during Batch <<N>> you discover a divergence between spec/wiki and reality NOT already documented in `docs/sesiones/2026-05-05-wiki-drifts.md`, append a new entry at the end of "Categoría 9 — Drifts del spec Capa 4 (2026-05-06)" with format:

```
### D-XXX 🟡 <one-line title>

**Asumido**: ...
**Reality**: ...
**Resolución**: ...
**Lección**: ...
```

Renumber consecutively from the last D-XX in the file (D-044 is the last one as of plan creation; first new entry is D-045). Do NOT edit `wiki/` files directly even if you spot a wiki-level inconsistency — log it as drift and move on.

### 10. Stopping conditions (halt + structured report)

Stop and produce the report described in §11 if any of:

- A test for a previous capa fails after your changes (regression).
- Lint check fails after your fixes.
- An import error in `src/` you cannot trivially resolve.
- The plan or spec has a contradiction the developer needs to resolve (e.g., the FastMCP middleware API doesn't match what the plan assumes).
- A user-impact decision arises not pre-cleared (e.g., changing an error code semantics, renaming a public field, changing the MCP transport path from `/mcp`).
- Tools unavailable (no Bash, no Edit, no Skill).
- Push to origin fails (auth issue, conflict, etc.).
- Migration in Batch 0 fails the pre-flight check (rows pre-existing — should be impossible but defensive).

### 11. Final report (always print at end, even on halt)

Print a markdown report titled `## Capa 4 Batch <<N>> — Execution report`. Sections:

1. **Status**: `completed` | `partial` | `halted-blocker`.
2. **Tasks done**: bullet list with commit hashes (`abc123 — test(mcp): SPEC-capa4 AC-1 — RED test request_upload_url`).
3. **ACs covered**: bullet list `AC-N → test_function_name` (and the file path).
4. **Tests run**: numbers `N passed, M skipped` and the command line used.
5. **Lint**: `pass` or details if fail.
6. **Push**: `pushed to feat/capa4-mcp at <commit-hash>` or `not pushed because <reason>`.
7. **Drifts logged**: list of new D-XX entries (or "none").
8. **Deferred items**: anything scoped to this batch but not closed (e.g., "Task 1.4 GREEN impl uses respx mock for bearer verification; integration with real testcontainers Postgres deferred to Batch 2 fixture extension").
9. **Blockers** (if halted): description + suggested resolution path.
10. **Suggested next**: one sentence — should the user spawn an agent for Batch <<N+1>>, run the rig smoke first, or address a blocker?

### 12. What this agent invocation does NOT do

- Does NOT spawn the next batch's agent automatically. The user reads your report first.
- Does NOT run E2E rig smoke (no rig access).
- Does NOT modify `wiki/` files (drifts only into the drift log).
- Does NOT decide on changes to the spec — that requires human approval.
- Does NOT merge to master or open a PR — branch stays `feat/capa4-mcp`.
- Does NOT remove the legacy `POST /api/transcriptions` endpoint (Batch 6 only marks it deprecated; removal is a Capa 5 task).
- Does NOT touch `pipeline/orchestrator.py::_orchestrator_lock` or any pipeline code (Capa 3 frozen).

### Begin

Now invoke the `executing-plans` skill (per §1), then read the files in §4, then execute Batch <<N>> following §5–§9 discipline, then halt and produce the §11 report.
