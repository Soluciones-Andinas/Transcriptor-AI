# AGENTS.md — `transcription-api` Governance Policy

This document governs how any AI agent (Claude Code, Cursor, Codex, Aider, OpenCode, Gemini CLI, etc.) operates inside this project. It is the cross-platform companion to [`CLAUDE.md`](CLAUDE.md). Both files share semantics; wording may differ.

---

## 1. Skill Invocation Semantics

Skills are not optional decoration; they encode the team's working agreements.

- When the user explicitly invokes a skill (for example `/sandinas-wiki-skills:crear-rf`), execute it.
- When a task matches a skill's documented trigger conditions, invoke that skill even if the user did not name it. Do not silently substitute a different approach.
- Workflow skills (`ps-contexto`, `brainstorming`, `ps-trazabilidad`, `crear-alcance`, `crear-arquitectura`, `crear-flujo`, `crear-rf`) follow the order defined in the Workflow Catalog (§3).
- If a skill is required for the task class but is not available in the agent's runtime, halt and report the missing skill. Do not fabricate equivalent behavior.

---

## 2. Orchestration Mode

Default to orchestration with explicit context loading and traceability closure.

1. Start with `ps-contexto` before planning or implementation. The skill must read `<ARCH>` to identify active components and ownership before any decision.
2. Run `brainstorming` once after context is loaded and before planning or execution. Skip only for small or trivial tasks (see §3).
3. Close critical context gaps before execution. If a question is unanswered after brainstorming, do not write code. Ask the user.
4. Prefer orchestrator partitioning and delegation when tasks are partitionable (multiple independent files, multiple modules, parallel research questions).
5. Close tasks with `ps-trazabilidad` when the change is large, risky, or multi-module.

### Additional mandatory rules

- If the task edits `<GOVERNANCE_AGENTS>` or `<GOVERNANCE_CLAUDE>`, use skill `ps-crear-agentsclaudemd`.
- If the change is large, risky, or multi-module, run `ps-trazabilidad` with thorough review before final closure.
- Skill `ps-contexto` must read `<ARCH>` to identify active components and responsibilities before planning or execution.

### Graphify-first rule (project-specific)

Before reading any wiki file to answer a question, query the local graphify knowledge graph at `<GRAPH>` via the MCP server defined in `.mcp.json`. The graph reduces token usage roughly 18x for queries about wiki structure, ADRs, flows, requirements, and their cross-references. Read the original file only when the graph lacks the requested detail or when implementing changes (the graph is a map; modifications require opening the territory).

After any meaningful change to wiki, ADR, or RF documents, run `/graphify --update` to keep the graph synchronized. Stale graphs produce confidently wrong answers.

---

## 3. Workflow Catalog

### Standard Task Flow

For typical feature, refactor, or non-trivial bugfix work:

1. Load context with `ps-contexto`.
2. Query `<GRAPH>` for the affected concepts (graphify-first).
3. Run `brainstorming` to align scope and approach.
4. Identify the affected RF / FL / ADR.
5. Implement changes following the Project Decision Priority (§4).
6. Update synchronized documents per §8.
7. Run `ps-trazabilidad` if the change touches more than one module.

### Large / Risky / Multi-Module Change Flow

For changes that touch contracts, error taxonomies, state machines, or multiple modules:

1. All steps of the Standard Task Flow.
2. Mandatory `ps-trazabilidad` with thorough review.
3. Mandatory documentation synchronization (§8) before closure.
4. Run `/graphify --update` and verify no critical edges were lost in the graph.
5. Surface the change in the corresponding ADR. If the change supersedes an existing decision, create a new ADR with status `Replaces ADR-NNN`. Do not edit the previous ADR.

### AGENTS.md / CLAUDE.md Policy Change Flow

For any change to governance policies:

1. Use skill `ps-crear-agentsclaudemd`.
2. Update `<GOVERNANCE_AGENTS>` and `<GOVERNANCE_CLAUDE>` in the same change. Never one without the other.
3. Validate that both files express the same semantics.
4. Report added, removed, and modified rules in the change description.

### Small / Trivial Task Flow

For typo fixes, dependency bumps with no behavior change, or single-line clarifications:

1. Skip `brainstorming` and `ps-trazabilidad`.
2. Apply the change directly.
3. Still run §8 synchronization if the change touches a contract surface (rare for trivial tasks).

---

## 4. Project Decision Priority

Authoritative source: `<ARCH>` §0.

**Privacy > Simplicity > Transcription Quality > Performance > Cost**

Use this order to resolve tradeoffs in code, ADRs, and architectural discussions. Do not invert the order in implementation choices without an ADR that explicitly justifies the inversion.

Examples of the priority in action (drawn from existing ADRs):

- ADR-001: WhisperX chosen over the higher-quality Canary-1B-v2 because Simplicity > Transcription Quality at the margin where the framework is mature versus needing custom glue code.
- ADR-003: synchronous API chosen over asynchronous queue because Simplicity dominated when expected volume was low.
- ADR-004: filesystem cache chosen over Redis because Simplicity dominated, with no privacy regression.

If the architecture file does not define this priority, halt and require updating `<ARCH>` first. Do not assume.

---

## 5. Canonical Source of Truth

The following documents are authoritative. When they conflict with code, the documents win and the code must be reconciled or the documents must be amended via the appropriate skill.

| Concern | Source | Skill that maintains it |
|---|---|---|
| Functional scope | `<SCOPE>` | `crear-alcance` |
| Architecture, decision priority, ADR index | `<ARCH>` | `crear-arquitectura` |
| ADRs (per decision) | `<ADR_DIR>/ADR-NNN.md` | `crear-arquitectura` (creates), never edited after Accepted |
| Functional flows | `<FL_INDEX>` and `<FL_DIR>/FL-*.md` | `crear-flujo` |
| Functional requirements | `<RF_INDEX>` and `<RF_DIR>/RF-*.md` | `crear-rf` |
| Data model | `<DATA_MODEL>` | `crear-rf` (when contracts change) |
| Test plans | `<TP_DIR>/TP-*.md` | `crear-rf` |
| Test matrix | `<TEST_MATRIX>` | `crear-rf` |
| Knowledge graph | `<GRAPH>` and `<GRAPH_REPORT>` | `/graphify` |

The README and `docs/` files (`docs/INVESTIGACION.md`, `docs/PLAN.md`, `docs/DECISIONES.md`) are companion materials, not authoritative sources. They may lag behind the wiki. When `docs/DECISIONES.md` and `<ADR_DIR>/ADR-*.md` disagree, the formal ADRs in `<ADR_DIR>` are authoritative.

---

## 6. Placeholder Mapping for Skills

When a Sandinas wiki skill references a placeholder, resolve it as follows:

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

Do not introduce new placeholders without updating this mapping.

---

## 7. Local Search Playbook

Order of preference: graph first, ripgrep second, file read last.

### Graph queries (preferred)

The graphify MCP server exposes the following tools when Claude Code is launched from the project root:

- `query_graph` for free-form questions about wiki concepts.
- `god_nodes` to identify the most connected entities.
- `get_node` and `get_neighbors` to expand from a known concept.
- `get_community` to see all entities in a thematic cluster.
- `shortest_path` to trace how two concepts relate.
- `graph_stats` for global statistics.

For agents without MCP access, the equivalent CLI is:

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

# Trace an entity through code and docs
rg -n "TranscriptionResult" .

# Verify traceability between RF and TP
rg -n "RF-TRX-04" wiki/pruebas/
rg -n "TP-TRX-04" wiki/RF/

# Find all ADRs that reference a component
rg -l "pyannote" wiki/ADR/
```

### File reads (last resort)

Open files directly when implementing a change, when the graph or ripgrep returned an ambiguous result, or when the question requires reading prose context that is not represented in nodes and edges.

---

## 8. Documentation Synchronization Rule

When a change modifies any of the following surfaces, the listed downstream documents must be reviewed and updated in the same change set:

| Change | Update obligation |
|---|---|
| New or modified RF behavior | `<RF_DIR>/RF-*.md`, `<RF_INDEX>`, `<TP_DIR>/TP-*.md`, `<TEST_MATRIX>` |
| New or modified flow | `<FL_DIR>/FL-*.md`, `<FL_INDEX>`, dependent RFs |
| New or changed entity, state, or event | `<DATA_MODEL>`, dependent RFs and TPs |
| New or changed error code | `<DATA_MODEL>` §7, RFs that emit it, TPs that verify it, `<TEST_MATRIX>` cobertura section |
| New ADR or replaced ADR | `<ARCH>` §7 index, mention in affected RFs |
| New component | `<ARCH>` §3 and §5, `<SCOPE>` §2 |
| Anything that changes the wiki structure | run `/graphify --update` |

A change that touches a contract surface but skips the synchronization is incomplete and must not be marked as done.

---

## 9. Legacy Exclusion Rule

- Do not read from `<ARCHIVED>` (`wiki/old/`) when answering questions or making decisions. The directory is reserved for superseded artifacts.
- Do not read from ADRs whose status is `Replaced` or `Deprecated` as if they were authoritative. Their content is historical context only.
- The `docs/` directory contains companion materials that pre-date the wiki. When in doubt, treat the wiki as current and `docs/` as historical reference.
- Files in `graphify-out/cache/` are extraction caches, not source documents. Do not read from them.

If you need to migrate content from a legacy doc into the wiki, do it via the corresponding `crear-*` skill, not by copy-paste.

---

## 10. Language Rule

| Surface | Language |
|---|---|
| Wiki documents (`<WIKI>/**`) | Spanish |
| ADRs (`<ADR_DIR>/**`) | Spanish |
| RFs, FLs, TPs | Spanish |
| `docs/**` | Spanish |
| Code, identifiers, log keys, error codes | English |
| Code comments | English when explaining mechanism, Spanish when explaining business rule |
| `AGENTS.md`, `CLAUDE.md`, project-local `ps-*` skills | English |
| User-facing messages of the API (errors, JSON fields) | English (matches the error code taxonomy) |

User-facing chat with the team is Spanish unless the user requests otherwise.

---

## 11. Extra Collaboration Rules

### No emojis in governance and policy documents

`AGENTS.md`, `CLAUDE.md`, ADRs, RFs, FLs, TPs, `<ARCH>`, `<SCOPE>`, and `<DATA_MODEL>` must not contain emojis. Status markers in tables (Aprobado, Pendiente, Replaced) are spelled out.

### Brainstorming question protocol (mandatory)

Before asking the user a non-trivial design question, present:

1. Learning context — what the agent already knows about the question.
2. Why the question matters — what decision depends on the answer.
3. A small ASCII diagram showing the proposed change or the alternatives under consideration.
4. Per-option pros and cons.
5. The agent's recommended option, with one-line justification anchored in the Project Decision Priority.

Single-shot yes/no clarifications about a path or a number do not need this protocol.

### Graphify-first protocol

For any task that involves understanding existing wiki, ADR, RF, FL, or test plan content:

1. First call to information must be a graph query (MCP tool or `graphify query`).
2. If the graph returns useful nodes and edges, expand them with `get_neighbors` or `shortest_path` before reading any file.
3. Read a file only after the graph has narrowed the scope, or when implementing changes (writing requires opening the actual file).
4. After completing changes that affect wiki content, run `/graphify --update`.

### TODO explicit = 0

RFs and ADRs in this project follow the Execution-Normative hardening level. Do not introduce TODOs, TBDs, or unresolved placeholders into authoritative wiki documents. Open questions go to brainstorming, not to the artifact.

### ADR immutability

Once an ADR has status `Aceptada`, it is immutable. Replacement requires a new ADR with status `Replaces ADR-NNN`. The original keeps its content and only its status changes to `Reemplazada`.

### Cache and tempfile hygiene

Code that writes temporary audio files, normalized WAVs, or intermediate artifacts must clean them up in `finally` blocks. The filesystem cache at `graphify-out/cache/` and the runtime cache for transcriptions are governed by their own RFs (RF-TRX-06, RF-CACHE-02, RF-CACHE-03).
