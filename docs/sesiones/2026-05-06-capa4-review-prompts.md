# Prompts para multi-agent review — Capa 4

> **Uso**: copiar cada bloque (5 en total) y pasarlo como `prompt` a la `Agent` tool con
> `subagent_type=general-purpose`. Spawnear los 5 en paralelo (un solo mensaje con 5 tool
> calls), revisar reportes cuando lleguen, consolidar hallazgos en grupos G1..GN para
> review-fixes (mismo patrón Capa 2 + Capa 3).
>
> **Convenciones del review**:
> - Severidad: 🔴 CRITICAL / 🟠 HIGH / 🟡 MEDIUM / 🟢 LOW / 💡 STRATEGIC.
> - Cada finding lleva: file:line + descripción + por qué importa + sugerencia de fix.
> - Los agents NO modifican código — solo escriben reportes.
> - Cada agent corre con context-zero: el contexto vive en el prompt.

---

## Agent 1 — silent-failure-hunter

```
You are reviewing Capa 4 of a Spanish transcription + diarization service for silent failures, swallowed exceptions, fallbacks that hide bugs, and defensive code that degrades cross-user privacy. You do NOT see prior conversation — everything you need is here.

### 1. Mission

Audit ONLY the diff `0071c77..7a063f4` on branch `feat/capa4-mcp` for **silent failures**: catch blocks that swallow errors, log-and-continue paths, fallbacks that mask the actual failure, default values that hide bugs, optional auth/scope steps that fail-OPEN. Produce a structured report of findings sorted by severity.

### 2. Project context

- **Project**: `transcription-api`. Self-hosted Spanish transcription + diarization for Soluciones Andinas. Privacy is the #1 priority (`wiki/02_arquitectura.md` §0: Privacy > Simplicity > Quality > Performance > Cost).
- **Working dir**: `/Users/francobertoldi/Documents/Sandinas/IA-Tasks/IA-Tasks-Investigación-Estrategia/transcription-api`. Use absolute paths.
- **Capa 4 scope**: MCP server (Streamable HTTP) + chunked upload pattern + 7 MCP tools + 2 resources + REST `POST /api/upload` endpoint + legacy deprecation + scoping classification guard.

### 3. Files in scope (read in order)

1. `docs/sesiones/2026-05-06-capa4-mcp-spec.md` — the contract (16 ACs, 12 typed errors, 8 ALTs, decisions).
2. `git diff 0071c77..7a063f4 -- src/transcription_api/` — Capa 4 source changes.
3. `git diff 0071c77..7a063f4 -- tests/` — only to understand what is asserted (don't audit tests for silent failures, audit production code).
4. `wiki/RF/RF-MCP.md` — process steps + typed errors per RF.

### 4. What to look for

- **`except Exception:` or bare `except:`** that log + continue without surfacing the cause.
- **Fallback values** when an external call fails (e.g., `try: x = api(...) except: x = "default"`).
- **Best-effort writes** that swallow IO errors silently when the write was meaningful (cache writes, log writes — these may be intentional, distinguish).
- **`get(...)` / `getattr(...)` with a default** on dicts that should always have the key (hides shape drift).
- **Empty `if`/`else` branches** with a `pass` or trivial log.
- **Auth / scope code paths** where a failed lookup returns `None`/`False` and the caller treats that as "not authenticated" without distinguishing from "user not found" vs "DB unreachable".
- **`asyncio.create_task(...)` without storing the task or awaiting**: errors in the coroutine surface as `Task exception was never retrieved` warnings, not as visible failures.
- **`session.commit()` inside a `try/except` where the except does NOT rollback**: the partial state escapes.
- **Best-effort `last_used_at` bump in `mcp/middleware.py`**: read carefully — is it best-effort by design (RF-MCP-00 §Side effects says yes) or is it hiding a real DB failure?
- **`hmac.compare_digest`** usage in `api/upload.py` — confirm constant-time path, no short-circuit.

### 5. Severity scale

- 🔴 **CRITICAL**: Silent failure that violates Privacy (cross-user leak risk), bypasses auth, or destroys user data.
- 🟠 **HIGH**: Silent failure that produces wrong result without raising; user observes incorrect data.
- 🟡 **MEDIUM**: Silent failure that degrades functionality but is recoverable; wrong logs / missing telemetry.
- 🟢 **LOW**: Suboptimal but legitimate (e.g., log-and-continue for cleanup of orphan files).
- 💡 **STRATEGIC**: Pattern observation (e.g., "all best-effort bumps follow the same try/except shape; consider a helper").

### 6. Output format

Print a markdown report titled `## Capa 4 Review — silent-failure-hunter findings`. For each finding:

```
### [<severity>] <short title>

**File**: `src/transcription_api/<path>:<line>`
**What**: <one sentence describing the silent failure>
**Why it matters**: <one or two sentences linking to Privacy / correctness>
**Sugerencia de fix**: <concrete patch idea, e.g., "raise instead of return None" or "add specific exception class">
```

End with a summary table:

```
## Resumen
| Severidad | Count |
|---|---|
| CRITICAL | N |
| HIGH | N |
| MEDIUM | N |
| LOW | N |
| STRATEGIC | N |
```

### 7. Hard constraints

- Do NOT modify any file. This is a read-only review.
- Do NOT run pytest, ruff, or any tool that mutates state.
- Do NOT report findings outside the diff range `0071c77..7a063f4`. Pre-Capa-4 code is out of scope.
- Do NOT report tests as silent failures (a passing test asserting `None` is intentional, not silent).
- Do NOT speculate about future code. Audit what exists.

### 8. Begin

Read the files in §3, scan the diff, produce the §6 report. Aim for completeness over speed — false negatives are worse than false positives in this audit.
```

---

## Agent 2 — pr-test-analyzer

```
You are reviewing Capa 4 of a Spanish transcription + diarization service for **test quality and coverage gaps**. You do NOT see prior conversation.

### 1. Mission

Audit the test suite added in `0071c77..7a063f4` (branch `feat/capa4-mcp`) and report: tests that pass but don't actually exercise the code, missing edge cases per AC, mock setups that mask bugs, integration vs unit balance issues, and tests whose failure mode would not catch a real regression.

### 2. Project context

- **Project**: `transcription-api`. Capa 4 introduces 7 MCP tools, 2 resources, 1 REST endpoint, 1 startup guard. ~50 new tests across `tests/integration/mcp/`, `tests/integration/api/`, `tests/unit/db/`.
- **Working dir**: `/Users/francobertoldi/Documents/Sandinas/IA-Tasks/IA-Tasks-Investigación-Estrategia/transcription-api`. Absolute paths.
- **TDD discipline**: each task lands as RED commit + GREEN commit + optional refactor. Hooks `pre-commit` corren ruff.
- **Skips expected**: `requires_docker` marker auto-skips on CPU dev box (no Postgres testcontainer); production CI runs them. ~190 skipped tests is normal.

### 3. Files in scope (read in order)

1. `docs/sesiones/2026-05-06-capa4-mcp-spec.md` — 16 ACs + their expected coverage.
2. `docs/sesiones/2026-05-06-capa4-mcp-plan.md` — traceability matrix (AC → Batch.Task → test file).
3. `git diff 0071c77..7a063f4 -- tests/` — every new test in scope.
4. `tests/factories.py` and `tests/conftest.py` — fixtures used.
5. The corresponding production files (only to confirm what the test exercises): `src/transcription_api/mcp/`, `src/transcription_api/api/upload.py`, `src/transcription_api/db/scoping.py`.

### 4. What to look for

- **AC coverage gaps**: an AC says "Given X When Y Then Z" — does at least one test exercise that exact path? Cross-check against the traceability matrix in the plan.
- **Tests that pass without exercising the assertion target**: e.g., `assert result is not None` after `result = mock.return_value` (the mock guarantees non-None — the test is tautological).
- **Mocks that mask reality**: `monkeypatch.setattr("module.func", AsyncMock(return_value=...))` where `func`'s real signature has changed and the test would not catch it.
- **Missing edge cases**:
  - Empty inputs (`""`, `[]`, `0`, `None` where applicable).
  - Boundary values (max_speakers=16 + 1, file_size = max_upload_mb * 1024 * 1024 ± 1, limit = 0 / max).
  - Concurrent calls (especially against `start_transcription` lock + `request_upload_url` race).
  - Soft-deleted state interactions (delete then list, delete then search).
- **Cross-user isolation tests**: every per-user tool MUST have a `cross_user_returns_404` test. Check against the listener fail-closed (`db.scoping.ScopingNotArmedError`) — does any test path actually trigger that error path, or do they all stay in the happy "user_id armed" lane?
- **Tests that skip on CPU but never run anywhere**: a `requires_docker` test that has no daemon-equipped CI is dead code. Verify CI config (likely `.github/workflows/`) actually runs these.
- **`pytest.raises` without checking the exception's MESSAGE or `code` field**: a `raises(McpError)` that catches the wrong category passes silently.
- **Setup that creates state but does not assert on it**: `db.add(row); await db.commit()` without `await db.refresh(row)` or follow-up SELECT — does the test actually verify the row landed?
- **Test names that don't match the assertion**: `test_returns_200` that actually asserts on body content — readability + grep-ability.

### 5. Severity scale

- 🔴 **CRITICAL**: An AC has zero test coverage despite the matrix claiming otherwise; or a test passes but does not exercise the code path it claims to.
- 🟠 **HIGH**: Important edge case missing (cross-user, boundary value, concurrent call); fix is < 1h.
- 🟡 **MEDIUM**: Test quality issue (tautological, weak assertion, mismatched name) that doesn't break coverage but reduces the value of the suite.
- 🟢 **LOW**: Nitpick (test could be split, fixture name unclear, docstring missing).
- 💡 **STRATEGIC**: Pattern observation across multiple tests.

### 6. Output format

`## Capa 4 Review — pr-test-analyzer findings`. Per finding:

```
### [<severity>] <short title>

**Test**: `tests/<path>::<test_function>` (or "missing test for AC-N")
**Production target**: `src/<path>:<line>` (the code that should be exercised)
**What**: <gap or quality issue>
**Why it matters**: <impact on regression detection>
**Sugerencia de fix**: <add test for X / strengthen assertion / fix mock signature / etc.>
```

End with: `## AC coverage matrix` listing each AC and whether it has at least one *real* test (not just a stub).

```
| AC | Tested by | Real coverage? | Notes |
|---|---|---|---|
| AC-1 | tests/integration/mcp/test_request_upload_url.py::test_audio_happy + ... | YES | full chain through B3 |
...
```

### 7. Hard constraints

- Do NOT modify any file.
- Do NOT run pytest (the agent doing the spawn already ran it; you audit the static state).
- Do NOT report findings outside the diff range.
- Do NOT count `requires_docker`-skipped tests as "no coverage" — count them as "validated in CI/rig".

### 8. Begin

Read the spec ACs, the plan matrix, then walk every new test file. Cross-reference. Produce §6.
```

---

## Agent 3 — pr-review-toolkit:code-reviewer

```
You are reviewing Capa 4 of a Spanish transcription + diarization service for **general code quality**: idiomatic patterns, clarity, dead code, naming, type hints, comments, duplication. You do NOT see prior conversation.

### 1. Mission

Audit `git diff 0071c77..7a063f4` on `feat/capa4-mcp` for code quality issues that an experienced Python reviewer would flag in a PR. Apply Strunk-style ruthlessness: clear over clever, correct types, no dead branches, idiomatic SQLAlchemy / FastAPI / Pytest.

### 2. Project context

- **Stack**: Python 3.10–3.11, FastAPI 0.115+, SQLAlchemy 2.0 async + asyncpg, pytest + pytest-asyncio, FastMCP `mcp[server]>=1.5,<2.0`.
- **Working dir**: `/Users/francobertoldi/Documents/Sandinas/IA-Tasks/IA-Tasks-Investigación-Estrategia/transcription-api`.
- **Style enforcement**: ruff is in the loop (line-length 100, ignored E501 + N818). ruff PASSES on `7a063f4` — your job is what ruff doesn't catch.
- **CLAUDE.md §10 Language Rule**: code in English, error_codes English, business-rule comments Spanish, mechanism comments English. Don't flag bilingual comments — that's intentional.

### 3. Files in scope

1. `docs/sesiones/2026-05-06-capa4-mcp-spec.md` — only for understanding intent.
2. `git diff 0071c77..7a063f4` — full Capa 4 diff (code + tests).
3. The new module tree: `src/transcription_api/mcp/{server,middleware,session}.py`, `mcp/tools/{upload,transcription}.py`, `mcp/resources.py`, `mcp/errors.py`, `api/upload.py`, plus the changes to `db/scoping.py` and `api/transcriptions.py`.

### 4. What to look for

- **Naming**: variables / functions / classes that do not match their behavior. e.g., `_resolve_active_bearer_id` if it does more than resolve.
- **Type hints**: missing on signatures, wrong (e.g., `Mapped[float]` for a `Numeric(10,2)` column — the D-003 lesson). `dict` vs `dict[str, Any]` vs a TypedDict where the shape is stable.
- **Dead code**: unused imports (ruff catches some — find the rest), unreachable branches, parameters never read, helpers called in 0 places.
- **Comments**: comments that describe WHAT (redundant with code) vs WHY (load-bearing). Comments that have rotted (describe behavior the code no longer has).
- **Duplication**: copy-paste between tools (especially `list_my_transcriptions` and `search_my_transcriptions` — both do limit clamping + user_id resolution + result serialization).
- **Magic numbers / strings**: literal `64 * 1024` for chunk size, `200`/`50` for query length, `"Bearer "` prefix length math. Should be constants with names.
- **Long functions / long files**: any function > 50 lines or file > 800 lines (project convention from CLAUDE.md). Capa 4 likely has tools that grow during edge-case adds.
- **Async correctness**: `async def` that does nothing async (no `await`, no async context). Sync IO inside async (could be `await asyncio.to_thread(...)`).
- **Error mapping**: every `except` block — does the re-raised error preserve the original via `raise X from exc`? Or does it silently lose the chain?
- **Resource cleanup**: file handles, sessions, lock acquisitions — all in `try/finally` or `async with`?
- **Imports order**: ruff handles I001 — check for lazy imports inside functions that should be at top, or top imports that cause circular dependencies (Capa 4 has the `from ..main import app` lazy import — verify it's necessary).
- **Pythonic idioms**: list comprehensions vs `for + append`, `dict.get(key, default)` vs `try/except KeyError`, f-strings vs `.format()`, walrus operator usage.

### 5. Severity scale

- 🔴 **CRITICAL**: Bug-level (wrong logic, broken type, missing await on a coroutine).
- 🟠 **HIGH**: Quality issue that bites future maintainers (heavy duplication, dead branch with non-obvious cause, function that does 3 things).
- 🟡 **MEDIUM**: Standard PR feedback (rename, extract helper, simplify).
- 🟢 **LOW**: Nitpick (comment phrasing, parameter ordering).
- 💡 **STRATEGIC**: Pattern observation (e.g., "tools share an `_error(...)` helper signature; consider a base class").

### 6. Output format

`## Capa 4 Review — code-reviewer findings`. Per finding:

```
### [<severity>] <short title>

**File**: `src/transcription_api/<path>:<line>`
**Issue**: <what's wrong>
**Suggested fix**:
\`\`\`python
# before
<offending lines>
# after
<patch>
\`\`\`
**Why**: <one sentence on motivation>
```

End with the same summary table as Agent 1.

### 7. Hard constraints

- Do NOT modify any file.
- Do NOT run ruff / mypy / pytest. The diff is what you review.
- Do NOT report style issues already enforced by ruff (E, F, I, B, UP, N, W) — ruff already passes.
- Do NOT report tests for code-quality issues; focus on production code. (Tests are in Agent 2's scope.)
- Do NOT propose changes that would weaken the Privacy invariant (e.g., "use raw SQL for performance" if it bypasses the listener).

### 8. Begin

Walk the diff file by file. Produce §6.
```

---

## Agent 4 — sandinas-code-reviewer

```
You are reviewing Capa 4 of a self-hosted Spanish transcription + diarization service against **Sandinas project conventions**. Project decision priority is `Performance > Diseño > Seguridad` per the sandinas-code-reviewer plugin convention, but for THIS project (transcription-api) the wiki overrides with `Privacy > Simplicity > Transcription Quality > Performance > Cost` (see `wiki/02_arquitectura.md` §0). When in tension, the wiki wins; flag the tension as STRATEGIC.

You do NOT see prior conversation.

### 1. Mission

Audit `git diff 0071c77..7a063f4` (branch `feat/capa4-mcp`) for **Sandinas-specific concerns**: ORM efficiency (no N+1, indexes hit correctly), security boundaries (auth, scoping, secrets), DB schema invariants, CLAUDE.md governance compliance, drift from the wiki authoritative sources.

### 2. Project context

- **Project**: `transcription-api`. Wiki at `wiki/` is authoritative; code reconciles to wiki, never the reverse.
- **Decision priority** (`wiki/02_arquitectura.md` §0): **Privacy > Simplicity > Transcription Quality > Performance > Cost**.
- **Working dir**: `/Users/francobertoldi/Documents/Sandinas/IA-Tasks/IA-Tasks-Investigación-Estrategia/transcription-api`.
- **Capa 4 hardening done pre-review**: scoping classification guard (`db.scoping._validate_model_classification`) prevents fail-OPEN on a future per-user model that omits `user_id`. Documented in spec §10 and `wiki-drifts.md` follow-up.

### 3. Files in scope

1. `wiki/02_arquitectura.md` (§0 priority, §3 components, §7 ADR index — especially ADR-011/013/015 for Capa 4 governance).
2. `wiki/RF/RF-MCP.md` and `wiki/RF/RF-IMG.md` — the canonical contract.
3. `wiki/05_modelo_datos.md` — schema authority.
4. `docs/sesiones/2026-05-06-capa4-mcp-spec.md` — Capa 4 spec.
5. `docs/sesiones/2026-05-05-wiki-drifts.md` — what's already known to be drifting.
6. `git diff 0071c77..7a063f4` — full Capa 4 implementation.

### 4. What to look for

- **Wiki ↔ code drift**: every error code in production code MUST appear in `wiki/05_modelo_datos.md` §8 error taxonomy. Every endpoint / tool MUST have a corresponding RF entry. Field names in JSON output MUST match the RF spec.
- **N+1 query patterns**: `get_transcription` does SELECT for transcription + SELECT for images. Is the second SELECT an N+1 or a single query? `list_my_transcriptions` returns `items + total` — does the count SELECT execute once or per row?
- **Index utilization**: `search_my_transcriptions` query — does it hit `idx_transcriptions_text_fts` (the GIN index)? `list_my_transcriptions` ORDER BY — does it hit `idx_transcriptions_user_created`? Verify the WHERE clauses and ORDER BY columns match the index definitions.
- **Per-user scoping**: every tool that operates on a per-user model MUST run inside `mcp_request_session(user_id)` or be wrapped in `bypass_scoping(...)` with a comment explaining the legitimate cross-user intent. ADR-015 fail-closed enforcement.
- **Secrets**: `bearer_for_upload` plaintext — entered in DB? Logged? Returned in response only once? `upload_bearer_hash` SHA-256 hex — confirm. `mcp_bearers.token_hash` — confirm Capa 2 invariants are not weakened.
- **CLAUDE.md §11 governance**:
  - No emojis in governance docs / RFs / ADRs (the spec at `docs/sesiones/2026-05-06-capa4-mcp-spec.md` is allowed emojis since it's `docs/`, not `wiki/` — verify).
  - ADR immutability — Capa 4 must NOT have edited an `Aceptada` ADR's body.
  - TODO explicit = 0 — search the diff for `TODO`, `FIXME`, `XXX`.
- **Synchronization rule (CLAUDE.md §8)**: any code change that adds an error code → does it appear in `wiki/05_modelo_datos.md` §7? Any new component → `02_arquitectura.md` §3?
- **Cleanup hygiene** (CLAUDE.md §11): files written to `<DATA_DIR>/uploads/` — cleaned up in `finally`?
- **Performance regressions vs Capa 3**: does the orchestrator call from `start_transcription` add latency vs the legacy `POST /api/transcriptions`? Are there extra DB round-trips?

### 5. Severity scale

- 🔴 **CRITICAL**: Privacy violation, secret leak, schema invariant broken, ADR mutated.
- 🟠 **HIGH**: Wiki ↔ code drift on a public contract, N+1 query, missing index hit.
- 🟡 **MEDIUM**: Synchronization rule miss (new error code not in §7, etc.), governance nitpick.
- 🟢 **LOW**: Documentation polish, comment improvements.
- 💡 **STRATEGIC**: Tension between project priority and a chosen approach.

### 6. Output format

`## Capa 4 Review — sandinas-code-reviewer findings`. Per finding:

```
### [<severity>] <short title>

**Source-of-truth doc**: `wiki/...` (where the contract lives) or "no wiki entry — drift"
**Production reference**: `src/transcription_api/<path>:<line>` (the divergent code)
**Drift**: <one sentence on the divergence>
**Privacy / Performance / Governance impact**: <one sentence on which axis>
**Sugerencia de fix**: <update wiki to match code | update code to match wiki | new ADR | new drift entry>
```

End with the same severity summary table.

### 7. Hard constraints

- Do NOT modify any file.
- Do NOT propose new ADRs in the diff range — list them as STRATEGIC followups, not MUST-DOs.
- Do NOT run any tool that mutates state.
- Do NOT report wiki edits already logged in `docs/sesiones/2026-05-05-wiki-drifts.md` as new findings — cross-reference and skip.

### 8. Begin

Read the wiki authority files first, then walk the diff. Produce §6.
```

---

## Agent 5 — code-architect

```
You are reviewing Capa 4 of a Spanish transcription + diarization service for **architecture**: layering, coupling, abstractions, future-proofing. You do NOT see prior conversation.

### 1. Mission

Audit `git diff 0071c77..7a063f4` on `feat/capa4-mcp` from an architectural lens: does the new `mcp/` package follow the patterns established by `auth/` and `pipeline/` (Capa 1+2+3)? Is `mcp_request_session` reusable beyond MCP context? Are the tools cohesive or do they leak responsibilities into modules they shouldn't? Will Capa 5 (UI) be able to consume what Capa 4 built without refactor?

### 2. Project context

- **Project**: 7-layer self-hosted service. Capas 1+2+3 merged to master; Capa 4 (MCP server + chunked upload) on `feat/capa4-mcp`; Capa 5 (UI React + cleanup-job for upload sessions) and Capa 6+ (image flow polish, observability) ahead.
- **Architectural priority** (`wiki/02_arquitectura.md` §0): Privacy > Simplicity > Transcription Quality > Performance > Cost. **Simplicity** is #2 — over-abstraction is a bug.
- **Working dir**: `/Users/francobertoldi/Documents/Sandinas/IA-Tasks/IA-Tasks-Investigación-Estrategia/transcription-api`.

### 3. Files in scope

1. `wiki/02_arquitectura.md` — §3 components, §5 microservices responsibilities, §11 supuestos.
2. `wiki/ADR/ADR-011.md` (MCP-first), `ADR-013.md` (uploads HTTP con bearer), `ADR-015.md` (scoping fail-closed).
3. `docs/sesiones/2026-05-06-capa4-mcp-spec.md` — sections 8 (estructura de módulos), 9 (out of scope), 10 (riesgos).
4. `git diff 0071c77..7a063f4` — full Capa 4.
5. The existing `src/transcription_api/auth/` and `src/transcription_api/pipeline/` modules (do NOT include in diff scope, but read them as reference for "what patterns Capa 4 should follow").

### 4. What to look for

- **Layering**: does `mcp/` import only from `auth/`, `db/`, `pipeline/`, `config`? Does any `mcp/` module reach into `api/` or `main.py` (other than the documented lazy import for `app.state`)?
- **Cohesion of `mcp/tools/`**: each tool file should encapsulate one tool. Cross-tool helpers should live in a shared module (`mcp/serialize.py` or similar). Are there `_serialize_summary`, `_serialize_full`, `_serialize_image` duplicated or factored?
- **`mcp_request_session(user_id)` reuse**: this ctx manager arms `db.info["user_id"]`. Is it Capa-4-specific or generally useful (e.g., Capa 5 background jobs)? Should it live in `db/` instead of `mcp/`?
- **Resource handlers** (`mcp/resources.py`) re-implement what `mcp/tools/transcription.py::get_transcription` already does. Is the duplication intentional (different SDK contract) or accidental?
- **Error hierarchy**: `mcp/errors.py::McpError` vs FastAPI HTTPException raised in `api/upload.py`. Are these two error systems consistent? Does a tool that wraps an API endpoint translate one into the other correctly?
- **Lock + session ownership**: `start_transcription` calls `orchestrate(...)` which is Capa 3 code. The orchestrator does `db.flush()` (not commit); the tool's `mcp_request_session` does the commit. Is this transaction boundary correct, or does the tool need to coordinate with `orchestrate` differently for image-upload-after-transcription flows in Capa 5+?
- **Lazy import `from ..main import app`**: this resolves a cycle but couples `mcp/tools/transcription.py` to the FastAPI app singleton. Is there a cleaner injection pattern (pass app.state via context)? STRATEGIC observation, not blocking.
- **Spec §9 out of scope alignment**: Capa 4 should NOT have leaked any Capa 5 concerns (cleanup job, attach_image tool, image upload-image endpoint, regenerate_mcp_token, removal of legacy endpoint). Verify.
- **Future-proofing**: when Capa 5 adds a new tool that needs the same pattern (auth + scoped session + DB op), can it be added by a junior dev in 1h, or does it require understanding 5 cross-cutting concerns?
- **Tests as architecture documentation**: do the integration tests show how the MCP flow is supposed to work end-to-end, or only the happy path of each tool in isolation? Is there a test that wires `request_upload_url → POST /api/upload → start_transcription → get_transcription` together?

### 5. Severity scale

- 🔴 **CRITICAL**: Layering violation that will require a rewrite to fix in Capa 5+.
- 🟠 **HIGH**: Coupling that will bite in Capa 5 specifically (e.g., MCP-only abstraction that the UI can't consume).
- 🟡 **MEDIUM**: Pattern inconsistency vs Capas 1-3 (Capa 4 reinvents what `auth/` already solved).
- 🟢 **LOW**: Cosmetic (file organization, naming convention drift).
- 💡 **STRATEGIC**: Architectural opportunity for the future (e.g., extract `db.session.scoped_session(user_id)` from `mcp/session.py` for reuse).

### 6. Output format

`## Capa 4 Review — code-architect findings`. Per finding:

```
### [<severity>] <short title>

**Layer / module**: `<module path>` or `<concern>`
**Concern**: <one or two sentences>
**Forward impact**: <which future Capa or use case is hurt by this>
**Sugerencia**: <refactor sketch — modules involved, new abstraction, ADR if needed>
```

End with two artifacts:

1. **Severity summary table** (same as Agent 1).
2. **Module dependency note**: a brief ascii or text description of the import graph for `mcp/` and any cycle / leak observed.

### 7. Hard constraints

- Do NOT modify any file.
- Do NOT propose ADRs in the diff range — STRATEGIC list only.
- Do NOT confuse architecture with code style (style is Agent 3's job).
- Do NOT propose abstractions that violate Simplicity (#2 priority). If a proposal adds a layer, justify why the current pain exceeds the cost.

### 8. Begin

Read §3 in order. Produce §6.
```

---

## Cómo consolidar los reportes (después que los 5 lleguen)

1. **Agrupar findings por severidad** — todos los CRITICAL primero, luego HIGH, etc.
2. **Deduplicar**: si dos agents flaggean el mismo file:line, mergear en un único finding con ambos contextos.
3. **Asignar a grupos G1..GN** (mismo patrón Capa 2/3) — agrupar por archivo afectado o concern share.
4. **Escribir** `docs/sesiones/2026-05-06-capa4-review-fixes-plan.md` con los grupos + checklist de implementación + assertion de tests post-fix.
5. **Spawnear un agent por grupo** (igual que el batch executor pattern) usando un prompt template parametrizable.


