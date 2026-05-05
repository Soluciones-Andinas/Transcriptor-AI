# Prompt para agentes — Capa 3 Batch executor

> **Uso**: copiar el bloque de abajo y pasarlo como `prompt` a la `Agent` tool con
> `subagent_type=general-purpose`. Reemplazar `<<N>>` por el número de batch a ejecutar
> (1..7). Un agente = un batch. Después de cada batch, revisar el reporte y decidir si
> spawnear el siguiente.
>
> **Spawning ejemplo (en Claude Code main session)**:
> ```
> Agent({
>   description: "Execute Capa 3 Batch 1",
>   subagent_type: "general-purpose",
>   prompt: <el bloque de abajo con <<N>> = 1>
> })
> ```

---

## Prompt (copiar desde acá hasta el final)

You are executing one batch of a TDD plan for a Python audio-pipeline service. You do NOT see the prior conversation — everything you need is in this prompt and in the files referenced below.

### 1. Required first action — invoke executing-plans skill

Before any other tool call, invoke the Sandinas team's `executing-plans` skill via the Skill tool:

```
Skill(skill="plugin:sandinas-dev-workflows:executing-plans", args="docs/sesiones/2026-05-05-capa3-pipeline-plan.md")
```

If the skill is not available in your runtime, **halt and report**: "executing-plans skill not available in runtime, cannot proceed without team's TDD discipline encoded".

The skill enforces:
- RED → GREEN → REFACTOR cycle per task.
- Atomic commits per RED and per GREEN.
- Traceability matrix updates as ACs close.
- Conventional commit format.

### 2. Mission

Execute **only Batch <<N>>** of the Capa 3 plan. Do NOT proceed to other batches. Pause + exit with a structured report when Batch <<N>> finishes (or you hit a blocker).

### 3. Project context

- **Project**: `transcription-api`. Self-hosted Spanish transcription + diarization service for Soluciones Andinas. FastAPI + WhisperX (Whisper large-v3 int8_float16) + pyannote 3.1 + Postgres. Dockerized with NVIDIA pass-through to a private rig (RTX 4060 Ti 8 GB).
- **Working dir**: `/Users/francobertoldi/Documents/Sandinas/IA-Tasks/IA-Tasks-Investigación-Estrategia/transcription-api`. Use absolute paths.
- **Branch**: `feat/capa3-pipeline` (already cut from master, upstream tracking set). Confirm with `git branch --show-current` before any commit.
- **Stack**: Python 3.10–3.11. FastAPI 0.115+. SQLAlchemy 2.0 async + asyncpg. pytest + pytest-asyncio + respx + testcontainers + asgi-lifespan.
- **Local dev environment**: `.venv/bin/python -m pytest ...` and `.venv/bin/python -m ruff check src/ tests/` from the working dir. The venv is pre-built at `.venv/` with Capa 1+2 deps installed.
- **GPU**: the developer's local Mac is CPU only — pipeline tests must `mock` torch/whisperx/pyannote or carry `@pytest.mark.requires_gpu` (auto-skipped on CPU machines via `tests/conftest.py`). The actual rig runs Docker images and is out of your reach.
- **Capas previas (1+2)**: already merged to master and present in working tree. **Do not modify** auth/, db/, or alembic/ — except the narrow exception of using `db.scoping.bypass_scoping` from the orchestrator (Batch 5) which is the documented escape hatch.

### 4. Files you must read in this order

1. `docs/sesiones/2026-05-05-capa3-pipeline-spec.md` — SPEC-capa3-pipeline-v1: 15 ACs, 10 typed errors, 4 ALT flows, decisions. Memorize the AC IDs and the error codes; they appear verbatim in commit messages and tests.
2. `docs/sesiones/2026-05-05-capa3-pipeline-plan.md` — TDD plan: 7 batches with task-level RED/GREEN code skeletons. Locate Batch <<N>> and read its full text.
3. `CLAUDE.md` (project root) — governance: skill invocation rules, decision priority (Privacy > Simplicity > Quality > Performance > Cost), language conventions (code English, docs Spanish).
4. `docs/sesiones/2026-05-05-wiki-drifts.md` — drift log. If your work creates a NEW divergence from spec/wiki not yet captured, append a new D-NN entry at the end of the file before pushing.

### 5. Definition of "Batch <<N>> complete"

1. **All RED tests** for Batch <<N>>'s tasks written, run with pytest, FAIL as expected, committed (one commit per RED).
2. **All GREEN implementations** written, RED tests now PASS, committed (one commit per GREEN).
3. **Lint passes**: `.venv/bin/python -m ruff check src/transcription_api/ tests/` is clean.
4. **No regressions**: existing Capa 1+2 tests still pass — at minimum run:
   ```
   .venv/bin/python -m pytest tests/unit tests/integration/test_config_security.py -q
   ```
   (Docker-required tests are auto-skipped if no daemon.)
5. **Commits pushed**: `git push origin feat/capa3-pipeline` succeeds.
6. **Plan checkboxes flipped**: in `docs/sesiones/2026-05-05-capa3-pipeline-plan.md` traceability matrix, mark `[x]` for ACs closed by this batch.
7. **No `[pending]` work in your scope**: every task in Batch <<N>>'s section has a corresponding commit hash.

### 6. Commit message format

```
<type>(<scope>): SPEC-capa3 <AC-id> — <short description>
```

Types: `test` (RED test), `feat` (GREEN impl), `fix` (regression fix), `refactor`, `docs`, `chore`. Scope examples: `pipeline`, `api`, `docker`. Examples:

- `test(pipeline): SPEC-capa3 AC-1 — RED test for normalize_audio happy path`
- `feat(pipeline): SPEC-capa3 AC-1 — implement ffmpeg normalize + sha256`
- `feat(api): SPEC-capa3 AC-3 — POST /api/transcriptions requires bearer`

Do NOT add `Co-Authored-By` lines (the user has attribution disabled globally).

### 7. Hard constraints (violating any → halt + report)

- Do NOT skip an AC because it looks redundant.
- Do NOT weaken the Capa 1+2 invariants — specifically, the listener `fail-closed` (`db.scoping.ScopingNotArmedError`). Use `with bypass_scoping(session):` for legitimate cross-user operations and document why in a one-line comment.
- Do NOT introduce features outside Batch <<N>>'s scope (e.g., do not preemptively scaffold Batch <<N+1>>'s files).
- Do NOT touch `src/transcription_api/auth/**`, `src/transcription_api/db/**` (exception: import `bypass_scoping`), `alembic/**`, `wiki/**`.
- Do NOT skip hooks (`--no-verify`) or bypass signing (`--no-gpg-sign`) on commits.
- Do NOT force-push or rewrite history on `feat/capa3-pipeline`.
- Do NOT mark a task `completed` if any of: tests failing, partial impl, unresolved import errors.

### 8. GPU + heavy-deps protocol

The local environment does NOT have torch/whisperx/pyannote installed (they live in `[pipeline]` extras, not in the venv). For Batch <<N>>:

- **Batch 1 (foundation)**: write loaders + lifespan + /health WITHOUT actually importing torch at module top. Use lazy imports inside functions or guard with `try/except ImportError` so unit tests on CPU machines pass via mocks.
- **Batches 2–6**: in tests, `unittest.mock.patch` the heavy deps (`whisperx.load_model`, `Pipeline.from_pretrained`, `torch.cuda.OutOfMemoryError`) so tests run on CPU.
- **Batch 1 Task 1.1 (Dockerfile)**: write the multi-stage Dockerfile but do NOT attempt to build it locally (the developer's Mac may not have nvidia-container-toolkit). Mark the AC as "implementation written, validation deferred to rig smoke (Task 7.3)" in the plan checkbox.
- **Batch 7 Task 7.3 (E2E rig smoke)**: you do NOT have rig access. Write the procedural checklist into a NEW file `docs/sesiones/2026-05-05-capa3-vram-budget.md` (template with empty fields) and note that the user runs it.

### 9. Drift logging discipline

If during Batch <<N>> you discover a divergence between spec/wiki and reality NOT already documented in `docs/sesiones/2026-05-05-wiki-drifts.md`, append a new entry at the end of "Categoría 7 — Drifts del review multi-agente Capa 2 (2026-05-05)" with format:

```
### D-XXX 🟡 <one-line title>

**Asumido**: ...
**Reality**: ...
**Resolución**: ...
**Lección**: ...
```

Renumber consecutively from the last D-XX in the file. Do NOT edit wiki/ files directly.

### 10. Stopping conditions (halt + structured report)

Stop and produce the report described in §11 if any of:

- A test for a previous capa fails after your changes (regression).
- Lint check fails after your fixes.
- An import error in `src/` you cannot trivially resolve.
- The plan or spec has a contradiction the developer needs to resolve.
- A user-impact decision arises not pre-cleared (e.g., changing an error code semantics, renaming a public field, choosing a non-default chunking strategy).
- Tools unavailable (no Bash, no Edit, no Skill).
- Push to origin fails (auth issue, conflict, etc.).

### 11. Final report (always print at end, even on halt)

Print a markdown report titled `## Capa 3 Batch <<N>> — Execution report`. Sections:

1. **Status**: `completed` | `partial` | `halted-blocker`.
2. **Tasks done**: bullet list with commit hashes (`abc123 — test(pipeline): SPEC-capa3 AC-1 — RED test`).
3. **ACs covered**: bullet list `AC-N → test_function_name` (and link to the line in the plan checkbox).
4. **Tests run**: numbers `N passed, M skipped` and the command line used.
5. **Lint**: `pass` or details if fail.
6. **Push**: `pushed to feat/capa3-pipeline at <commit-hash>` or `not pushed because <reason>`.
7. **Drifts logged**: list of new D-XX entries (or "none").
8. **Deferred items**: anything scoped to this batch but not closed (e.g., "Task 1.1 Dockerfile written but rig validation deferred").
9. **Blockers** (if halted): description + suggested resolution path.
10. **Suggested next**: one sentence — should the user spawn an agent for Batch <<N+1>>, run the rig smoke first, or address a blocker?

### 12. What this agent invocation does NOT do

- Does NOT spawn the next batch's agent automatically. The user reads your report first.
- Does NOT run E2E rig smoke (no rig access).
- Does NOT modify wiki/ files (drifts only into the drift log).
- Does NOT decide on changes to the spec — that requires human approval.
- Does NOT merge to master or open a PR — branch stays `feat/capa3-pipeline`.

### Begin

Now invoke the `executing-plans` skill (per §1), then read the files in §4, then execute Batch <<N>> following §5–§9 discipline, then halt and produce the §11 report.
