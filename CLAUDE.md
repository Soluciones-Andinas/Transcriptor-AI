# CLAUDE.md — `transcription-api` Governance Policy (Claude Code)

This document governs how Claude Code operates inside this project. It is the Claude-Code-specific companion to [`AGENTS.md`](AGENTS.md). Both files share the same semantics; wording may differ. Where the two files appear to conflict, raise the conflict before acting.

## What this project is, in one paragraph

`transcription-api` is a self-hosted batch API that receives audio or video files (MP4, MP3, WAV, M4A, FLAC) and returns Spanish transcriptions with speaker diarization as JSON, persisted in a 24-hour filesystem cache for idempotency. The stack is FastAPI plus WhisperX (Whisper large-v3 quantized to `int8_float16`) plus pyannote 3.1, deployed via Docker on a private rig with NVIDIA RTX 4060 Ti 8 GB VRAM in Soluciones Andinas' intranet. The full Spec-Driven Development wiki lives in `wiki/` with seven ADRs, two functional flows, nine functional requirements, and an end-to-end test matrix. The output is consumed manually via the user's Claude (Code or Desktop) to generate meeting minutes; the API itself does not generate minutes.

---

## 1. Skill Invocation Semantics

Skills are mandatory when their trigger conditions are met.

- When the user types `/skill-name` or `/plugin:skill-name`, invoke that skill via the `Skill` tool before any other reply.
- When a task implicitly matches a Sandinas wiki skill (`crear-alcance`, `crear-arquitectura`, `crear-flujo`, `crear-rf`, `ps-contexto`, `ps-trazabilidad`, `ps-crear-agentsclaudemd`), invoke it even if the user did not name it.
- When a Sandinas dev workflow skill applies (`brainstorming`, `writing-plans`, `executing-plans`, `systematic-debugging`, `dispatching-parallel-agents`, `using-git-worktrees`), follow the same rule.
- Do not silently substitute a different approach because the skill seems redundant. The skill encodes the team's working agreement.
- If a required skill is unavailable in the runtime, halt and report the absence rather than improvising.

---

## 2. Orchestration Mode

Claude Code default behavior in this project:

1. Start with `ps-contexto` for any planning or implementation task. The skill must read `<ARCH>` to identify active components and ownership before any decision is made.
2. Run `brainstorming` once after context is loaded and before writing code or implementation plans. Skip only for small or trivial tasks (see §3).
3. Close critical context gaps before execution. If a question remains unanswered, ask the user. Do not invent.
4. Prefer orchestrator partitioning when work is partitionable. Use the `Agent` tool with `subagent_type` for independent research questions, parallel file reads, or multi-file extraction.
5. Close tasks with `ps-trazabilidad` when the change is large, risky, or multi-module.

### Additional mandatory rules

- If the task edits `<GOVERNANCE_AGENTS>` or `<GOVERNANCE_CLAUDE>`, use skill `ps-crear-agentsclaudemd`. Both files change in the same edit; never one without the other.
- If the change is large, risky, or multi-module, run `ps-trazabilidad` with thorough review before final closure.
- Skill `ps-contexto` must read `<ARCH>` to identify active components and responsibilities before planning or execution.

### Graphify-first rule (project-specific, Claude Code)

The project's `.mcp.json` exposes a graphify MCP server pointing at `<GRAPH>`. When Claude Code is launched from this directory and the user approves the server, the following tools become available:

- `mcp__graphify__query_graph`
- `mcp__graphify__god_nodes`
- `mcp__graphify__get_node`
- `mcp__graphify__get_neighbors`
- `mcp__graphify__get_community`
- `mcp__graphify__shortest_path`
- `mcp__graphify__graph_stats`

Before reading any wiki file to answer a question, query the graph first. Empirical token reduction measured at this writing is roughly 18x. Read original files only when the graph lacks the requested detail or when implementing changes.

After any non-trivial change to wiki, ADR, or RF documents, run `/graphify --update` so the graph stays synchronized. Do not propagate stale graph answers.

---

## 3. Workflow Catalog

### Standard Task Flow

For typical feature, refactor, or non-trivial bugfix work:

1. Run `ps-contexto`.
2. Query `<GRAPH>` via the MCP server for the affected concepts.
3. Run `brainstorming` to align scope and approach.
4. Identify the affected RF, FL, ADR, or scope item.
5. Implement following the Project Decision Priority (§4).
6. Update the synchronized documents listed in §8.
7. If the change touches more than one module or one contract, run `ps-trazabilidad`.

### Large / Risky / Multi-Module Change Flow

For changes that touch contracts, error taxonomies, state machines, or multiple modules:

1. Execute the Standard Task Flow in full.
2. Run `ps-trazabilidad` with thorough review (mandatory).
3. Apply §8 synchronization in full (mandatory).
4. Run `/graphify --update` and inspect `god_nodes` to confirm no central node lost critical edges.
5. If the change supersedes a decision, create a new ADR with status `Replaces ADR-NNN`. Do not edit the prior ADR's body.

### AGENTS.md / CLAUDE.md Policy Change Flow

1. Use skill `ps-crear-agentsclaudemd`.
2. Edit both `<GOVERNANCE_AGENTS>` and `<GOVERNANCE_CLAUDE>` in the same change set.
3. Validate the two files express the same semantics.
4. List the added, removed, and modified rules in the change description.

### Small / Trivial Task Flow

For typo fixes, dependency bumps without behavior change, or single-line clarifications:

1. Skip `brainstorming` and `ps-trazabilidad`.
2. Apply the change directly.
3. Still apply §8 synchronization if the trivial change happens to touch a contract surface.

---

## 4. Project Decision Priority

Authoritative source: `<ARCH>` §0.

**Privacy > Simplicity > Transcription Quality > Performance > Cost**

Use this order when resolving tradeoffs. Examples already in the project:

- ADR-001 chose WhisperX over the higher-quality Canary-1B-v2: Simplicity dominated when WhisperX provided a complete framework versus 4-6 days of glue code.
- ADR-003 chose synchronous API over an async queue with job IDs: Simplicity dominated at the expected volume.
- ADR-004 chose filesystem cache over Redis or SQLite: Simplicity dominated and Privacy did not regress.

Do not invert this priority order in implementation choices without an ADR that explicitly justifies the inversion. If `<ARCH>` does not define the priority, halt and ask for an architecture update before proceeding.

---

## 5. Canonical Source of Truth

The wiki is authoritative. Code reconciles to the wiki, not the other way around.

| Concern | Source | Skill that maintains it |
|---|---|---|
| Functional scope | `<SCOPE>` | `crear-alcance` |
| Architecture, decision priority, ADR index | `<ARCH>` | `crear-arquitectura` |
| Individual ADRs | `<ADR_DIR>/ADR-NNN.md` | `crear-arquitectura` (immutable after Accepted) |
| Functional flows | `<FL_INDEX>` and `<FL_DIR>/FL-*.md` | `crear-flujo` |
| Functional requirements | `<RF_INDEX>` and `<RF_DIR>/RF-*.md` | `crear-rf` |
| Data model (filesystem entities, log events, error taxonomy) | `<DATA_MODEL>` | `crear-rf` when contracts change |
| Test plans per module | `<TP_DIR>/TP-*.md` | `crear-rf` |
| Test matrix (RF to test mapping) | `<TEST_MATRIX>` | `crear-rf` |
| Knowledge graph of the wiki | `<GRAPH>` and `<GRAPH_REPORT>` | `/graphify` |

The `docs/` directory and `README.md` are companion materials. They may lag behind the wiki and are not authoritative. When `docs/DECISIONES.md` and `<ADR_DIR>/ADR-*.md` disagree, the formal ADRs win.

---

## 6. Placeholder Mapping for Skills

| Placeholder | Path |
|---|---|
| `<WIKI>` | `wiki/` |
| `<GOVERNANCE_AGENTS>` | `AGENTS.md` |
| `<GOVERNANCE_CLAUDE>` | `CLAUDE.md` |
| `<SCOPE>` | `wiki/01_alcance_funcional.md` |
| `<ARCH>` | `wiki/02_arquitectura.md` |
| `<FL_INDEX>` | `wiki/03_FL.md` |
| `<FL_DIR>` | `wiki/FL/` |
| `<RF_INDEX>` | `wiki/04_RF.md` |
| `<RF_DIR>` | `wiki/RF/` |
| `<DATA_MODEL>` | `wiki/05_modelo_datos.md` |
| `<TEST_MATRIX>` | `wiki/06_matriz_pruebas_RF.md` |
| `<TP_DIR>` | `wiki/pruebas/` |
| `<ADR_DIR>` | `wiki/ADR/` |
| `<ARCHIVED>` | `wiki/old/` (reserved for future use) |
| `<GRAPH>` | `graphify-out/graph.json` |
| `<GRAPH_REPORT>` | `graphify-out/GRAPH_REPORT.md` |

---

## 7. Local Search Playbook

Order of preference: graph first, ripgrep second, file read last.

### Graph queries via MCP (preferred)

```
mcp__graphify__query_graph        for free-form questions
mcp__graphify__god_nodes          for central concepts
mcp__graphify__get_node           detail of a known concept
mcp__graphify__get_neighbors      one-hop expansion
mcp__graphify__get_community      thematic cluster
mcp__graphify__shortest_path      relationship between two concepts
mcp__graphify__graph_stats        global statistics
```

If the MCP server is not active in the session (Claude Code launched outside this directory or server not approved), fall back to the CLI:

```
graphify query "<question>"
graphify path "<concept_a>" "<concept_b>"
graphify explain "<concept>"
```

### Ripgrep patterns

Use these when the graph cannot answer a structural question.

```
# Discover RFs by feature keyword
rg -i "audio|transcribe|diariz" wiki/RF/

# Find an ID across the wiki
rg -n "RF-TRX-04|FL-TRX-01" wiki/

# Verify traceability RF to TP and back
rg -n "RF-TRX-04" wiki/pruebas/
rg -n "TP-TRX-04" wiki/RF/

# All ADRs that reference a component
rg -l "pyannote" wiki/ADR/

# Trace an entity through code and docs
rg -n "TranscriptionResult" .
```

### File reads (last resort)

Open files directly when implementing a change, when the graph or ripgrep returned an ambiguous result, or when the question requires reading prose context not represented in nodes and edges.

---

## 8. Documentation Synchronization Rule

When a change modifies any of the following surfaces, the listed downstream documents must be reviewed and updated in the same change set. Skipping the cascade leaves the wiki incoherent.

| Change | Update obligation |
|---|---|
| New or modified RF behavior | `<RF_DIR>/RF-*.md`, `<RF_INDEX>`, `<TP_DIR>/TP-*.md`, `<TEST_MATRIX>` |
| New or modified flow | `<FL_DIR>/FL-*.md`, `<FL_INDEX>`, dependent RFs |
| New or changed entity, state, or event | `<DATA_MODEL>`, dependent RFs and TPs |
| New or changed error code | `<DATA_MODEL>` §7, RFs that emit it, TPs that verify it, `<TEST_MATRIX>` coverage section |
| New ADR or replaced ADR | `<ARCH>` §7 index, mention in affected RFs |
| New component | `<ARCH>` §3 and §5, `<SCOPE>` §2 |
| Any change in wiki structure | run `/graphify --update` |

A change that touches a contract surface but skips the synchronization is incomplete. Do not mark the task done.

---

## 9. Legacy Exclusion Rule

- Do not treat `<ARCHIVED>` (`wiki/old/`) as authoritative. It is reserved for superseded artifacts. The directory does not exist yet; create only when an artifact is formally archived.
- Do not treat ADRs with status `Reemplazada` or `Deprecada` as active. They are historical context.
- `docs/INVESTIGACION.md`, `docs/PLAN.md`, and `docs/DECISIONES.md` predate the formal wiki and may lag. When in doubt, the wiki is current.
- `graphify-out/cache/` contains extraction caches, not source documents.
- Migrate legacy content into the wiki via the corresponding `crear-*` skill, not by copy-paste.

---

## 10. Language Rule

| Surface | Language |
|---|---|
| Wiki documents (`<WIKI>/**`) | Spanish |
| ADRs | Spanish |
| RFs, FLs, TPs | Spanish |
| `docs/**` | Spanish |
| Code, identifiers, log keys, error codes | English |
| Code comments | English when describing mechanism, Spanish when describing business rule |
| `AGENTS.md`, `CLAUDE.md`, local `ps-*` skills | English |
| API user-facing strings (error codes, JSON keys) | English |

User-facing chat with the team is Spanish unless requested otherwise.

---

## 11. Extra Collaboration Rules

### No emojis in governance and policy documents

`AGENTS.md`, `CLAUDE.md`, ADRs, RFs, FLs, TPs, `<ARCH>`, `<SCOPE>`, and `<DATA_MODEL>` must not contain emojis. Status markers in tables (Aprobado, Pendiente, Reemplazada) are spelled out.

### Brainstorming question protocol (mandatory)

Before asking the user a non-trivial design question, present:

1. Learning context — what is already known about the question.
2. Why the question matters — what decision depends on the answer.
3. A small ASCII diagram showing the proposed change or the alternatives.
4. Per-option pros and cons.
5. The recommended option with one-line justification anchored in the Project Decision Priority.

Single-shot clarifications about a path or a number do not need this protocol.

### Graphify-first protocol (Claude Code specifics)

1. The first information lookup for any wiki-related question must be an MCP graph tool call (or `graphify query` if MCP is unavailable).
2. Expand from initial nodes via `get_neighbors` or `shortest_path` before opening files.
3. Read a file only after the graph has narrowed the scope, or when writing changes.
4. After meaningful wiki edits, run `/graphify --update`.
5. If the graph is stale (last update older than the most recent wiki commit), refresh before answering.

### TODO explicit = 0

RFs in this project follow the Execution-Normative hardening level. Do not introduce TODOs, TBDs, or unresolved placeholders into authoritative wiki documents. Open questions go to brainstorming, not into the artifact.

### ADR immutability

ADRs with status `Aceptada` are immutable. Replacement requires a new ADR. The previous ADR keeps its content; only its status field changes to `Reemplazada` and `Reemplazada por: ADR-NNN`.

### Cache and tempfile hygiene

Implementation code that handles temporary audio files, normalized WAVs, or intermediate artifacts must clean them up in `finally` blocks. The filesystem cache at `<DATA_DIR>/cache/` and its TTL behavior are governed by RF-TRX-06, RF-CACHE-01, RF-CACHE-02, RF-CACHE-03.

### Tone of replies to the user

Default to Spanish, technical, direct, concise. Justify recommendations with data (benchmarks, prices, latencies) when claims are non-obvious. Do not hedge with disclaimers. When uncertain about a fact, say so explicitly and verify before answering.

### When to offer scheduling background work

After completing a change that has a natural future follow-up (a feature flag to clean up later, a soak window to verify, a graph that should be re-built after wiki changes settle), offer a one-line `/schedule` proposal at the end of the reply. Do not pile up offers across consecutive turns. Do not offer for refactors, bugfixes, or documentation tasks.

---

## 12. Testing Conventions

Mirror of `AGENTS.md` §12. Both files must stay in sync per §0 governance rule.

### Pytest markers

Four custom markers gate tests by environment dependency. Resolution lives in `tests/conftest.py::pytest_collection_modifyitems`; each auto-skips when its prerequisite is missing.

| Marker | Skip trigger | Used for |
|---|---|---|
| `requires_docker` | `docker info` does not respond within ~5s | testcontainers Postgres (most Capa 1/2/3/4 integration tests) |
| `requires_gpu` | `transcription_api.gpu.detect_accelerator()` reports no CUDA / MPS | Real WhisperX / pyannote model loaders |
| `requires_docker_gpu` | Either Docker or GPU is missing | Hybrid pipeline tests (rig-only) |
| `requires_ffmpeg` | `ffmpeg` or `ffprobe` not on PATH | Audio normalization tests |

`e2e` is reserved for full-pipeline tests; deselect it in fast iterations.

### Local invocation

Default fast loop:

```
.venv/bin/python -m pytest tests/ -q -m "not e2e and not requires_docker_gpu and not requires_ffmpeg"
```

Lint gate:

```
.venv/bin/python -m ruff check src/ tests/
```

### CI workflow

`.github/workflows/test.yml` (G14 review-fix) runs on every PR + push to `master`. Ubuntu-latest + Python 3.11 + ffmpeg + `pip install -e ".[dev]"`. The workflow runs `requires_docker` tests because the runner has Docker; testcontainers spins Postgres on demand. Heavy `[pipeline]` extras stay out (Capa 3 mocks at the loader seam per D-030).

A green local run is necessary but not sufficient — ~200 `requires_docker` tests skip on the dev box and only run on CI. Wait for the GitHub Actions check before merging.

### Test layout reminder

Integration helpers (assert_tool_error fixture, arm_context, seed_user_with_bearer) live in `tests/integration/conftest.py` (G11.7). Existing test files still ship local copies of these helpers — they migrate when touched naturally; new tests should use the conftest version.

When adding a test that does not need DB, place it in `tests/unit/` to avoid the `requires_docker` module marker. Splitting a check into "static / unit" + "runtime / integration" beats fighting the module-level marker (lesson from D-048).
