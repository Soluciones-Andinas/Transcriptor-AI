# Drift log — Capa 1 + inicio Capa 2 (2026-05-05)

> **Propósito**: registrar deltas entre lo que el wiki / spec / plan asumían y lo que la realidad mostró durante la implementación. Cada entrada debería ser actionable: o se corrigió el wiki/plan/proceso, o queda como warning para iteraciones futuras.
>
> **Convención de severidad**:
> - 🔴 **CRITICAL** — si no se corrige, futuras capas o re-runs heredan el bug
> - 🟠 **HIGH** — afectó esta implementación, ya corregido pero merece doc para no repetirlo
> - 🟡 **MEDIUM** — fricción menor, workaround aplicado, conviene revisar al refactorizar
> - 🟢 **LOW** — cosmético / informativo

---

## Categoría 1 — Drifts wiki ↔ realidad de hardware

### D-001 🔴 Hardware GPU del rig: RTX 5060 Ti 16 GB → RTX 4060 Ti 8 GB

**Asumido (wiki original 2026-04-30)**: rig con NVIDIA GeForce RTX 5060 Ti, 16 GB VRAM.

**Reality (smoke en rig 2026-05-05, `nvidia-smi`)**: NVIDIA GeForce RTX 4060 Ti, 8 GB VRAM.

**Propagación de la asunción incorrecta**:
- `wiki/01_alcance_funcional.md` — restricciones técnicas, criterios de latencia.
- `wiki/02_arquitectura.md` — §1 resumen ejecutivo, §3 C4 boundary, §5 stack table, §6 deployment node.
- `wiki/ADR/ADR-001.md` — toda la justificación de WhisperX large-v3 con `compute_type=float16`.
- `wiki/ADR/ADR-005.md` — math de VRAM combinada (large-v3 + pyannote ~12 GB / 16 GB).
- `.env.example`, `docker-compose.yml`, `src/transcription_api/config.py` — `COMPUTE_TYPE=float16` default.
- `README.md`, `CLAUDE.md` — descripción top-level del proyecto.
- `src/transcription_api/gpu.py` — comments y docstrings.
- `tests/integration/test_gpu_detection.py` — mocks usaban "RTX 5060 Ti".

**Resolución (commit `57bfe81`)**: cascada de 11 archivos. Stack STT cambia de `compute_type=float16` (~10-11 GB Whisper + ~2-3 GB pyannote = no entra) a `compute_type=int8_float16` (~5-6 GB + 2-3 GB = entra apretado). ADR-001 reescrito in-place (con suspensión explícita de la regla de inmutabilidad).

**Lección**: el wiki SDD se basó en una asunción de hardware no validada empíricamente. Para futuras decisiones que dependan de specs físicas, hacer `nvidia-smi` o equivalente **antes** de cerrar el ADR. Aún quedan dos validaciones empíricas pendientes:
1. ¿Whisper large-v3 int8 + pyannote 3.1 entran juntos en 8 GB sin OOM bajo carga real? (Capa 4)
2. ¿El WER en español rioplatense con int8_float16 es aceptable (<8%)? (Capa 4)

Si alguna falla → fallback a `large-v3-turbo` o Canary + glue code (Opción D / B en ADR-001 actualizado).

---

## Categoría 2 — Drifts wiki ↔ código (convenciones)

### D-002 🟠 Naming de UNIQUE indexes: `idx_*` (wiki) vs `uq_*` (SQLAlchemy)

**Asumido (wiki/05_modelo_datos.md original)**:
```
Index: `idx_users_email` (UNIQUE), `idx_users_microsoft_oid` (UNIQUE).
Index: `idx_oauth_tokens_user_id` (UNIQUE — un solo token activo por user).
Index: `idx_mcp_bearers_user_id`, `idx_mcp_bearers_token_hash` (UNIQUE).
```

**Reality**: SQLAlchemy + naming convention emite `uq_<table>_<col>` para `UniqueConstraint` y `idx_<table>_<col>` solo para `Index` no-único. Postgres muestra ambos en `pg_indexes` (los UNIQUE constraints están implementados como unique indexes). Pero a nivel SQL/SQLAlchemy son distintos:
- `UniqueConstraint('email')` → emite `ALTER TABLE ... ADD CONSTRAINT uq_users_email UNIQUE (email)`
- `Index('idx_users_email', ..., unique=True)` → emite `CREATE UNIQUE INDEX idx_users_email ON users (email)`

**Resolución (commit `236b1e6` review-D)**: wiki actualizado para usar `uq_*` para constraints y `idx_*` para índices propiamente dichos. La convención SQLAlchemy gana porque es más expresiva semánticamente (constraint vs index propio).

**Lección**: el wiki SDD lo escribió alguien (yo) sin grounding fuerte en el ORM concreto que se iba a usar. Para próximos modelos de datos: validar la convención de nombres del ORM elegido **antes** de cerrar el wiki §2.

### D-003 🟡 Atributos Python `Mapped[float]` vs `Numeric(10,2)` real

**Asumido (model definition Capa 1 batch 1)**:
```python
duration_seconds: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
```

**Reality**: SQLAlchemy + asyncpg materializa `Numeric` como `decimal.Decimal`, no `float`. La anotación mentía a mypy y rompía aritmética mixta float/Decimal con `TypeError`.

**Resolución (commit `ca4917d` review-C)**: cambiado a `Mapped[Decimal]`. Test explicit `test_transcription_duration_round_trips_as_decimal`.

**Lección**: confiar en el dialect, no en la asunción "SQL numeric ≈ Python float". `Mapped[T]` debe matchear el tipo Python que el driver retorna, no el tipo conceptual SQL.

---

## Categoría 3 — Drifts plan ↔ realidad de librerías

### D-004 🟠 `itsdangerous.URLSafeTimedSerializer.loads(max_age=0)` no expira

**Asumido (plan T2 RED block)**:
```python
with pytest.raises(StateExpired):
    verify_state(token, max_age_seconds=0)
```

**Reality**: itsdangerous compara `age > max_age` en estricto, no `>=`. Dentro del mismo segundo `age=0`, así que `0 > 0` es `False` y no se dispara `SignatureExpired`.

**Resolución (commit `8f6150a` Capa 2 T2)**: cambiar a `max_age_seconds=-1` que hace `0 > -1 == True`. Documentado en docstring del test.

**Lección**: plan code blocks no son deuda compilada. Asumir behavior estándar de libs de terceros sin validar el edge case TTL es trampa común. Otros equivalentes potenciales: `time.time()` vs `time.monotonic()` para timeouts; `datetime.utcnow()` vs `datetime.now(tz=timezone.utc)` para `iat`/`exp` JWT.

### D-005 🟡 Plan T1 importa de módulos que no existen aún (T4)

**Asumido (plan T1 GREEN block)**:
```python
# src/transcription_api/auth/__init__.py
from .routes import router
from .dependencies import get_current_user_web, get_current_user_mcp
```

**Reality**: T1 se ejecuta antes que T4 (que crea routes.py) y B5/B6 (que implementan dependencies.py). Si seguís el plan textualmente, T1 GREEN falla con `ImportError`.

**Resolución (Capa 2 Batch 1 ejecución)**: en T1 GREEN creé stubs mínimos para `routes.py` (router + ping placeholder) y `dependencies.py` (callables que raise 501) para que `__init__.py` re-exports funcionen y AC-1 pase. Real implementations vienen en T4/B5/B6.

**Lección**: el plan tiene un orden de tasks que no es topológicamente consistente con sus imports. Mejorar al escribir planes: ordenar tasks por dependencias de import primero, o explicitar "stub mínimo" para módulos que se completan en tasks posteriores.

### D-006 🟡 Plan AC-1 "test_auth_module_imports" no testea el wire HTTP

**Asumido (plan T1 / AC-1)**: AC-1 es satisfecho con `from transcription_api.auth import router, get_current_user_web, get_current_user_mcp` exits 0.

**Reality**: ese test pasa con los stubs de T1 incluso sin `app.include_router(auth_router)` ejecutado. Cumple AC-1 a nivel módulo pero no garantiza que el router está vivo en la app.

**Resolución (Capa 2 T4)**: agregué `tests/integration/auth/test_router_wire.py::test_auth_ping_returns_ok` que sí prueba el HTTP completo. Lo trace al wire de T4.

**Lección**: ACs sobre "módulo importable" deberían explícitamente cubrir "+ wire correcto en la app si aplica". Sin esto, alguien podría borrar `app.include_router(...)` y el test seguiría verde.

---

## Categoría 4 — Drifts spec ↔ realidad operativa

### D-007 🟠 Dockerfile `pip install -e .` falla por `src/` aún no copiado

**Asumido (Capa 1 Dockerfile original)**:
```dockerfile
COPY pyproject.toml ./
RUN pip install --upgrade pip setuptools wheel \
    && pip install -e .
COPY src/ ./src/
```

Patrón Node-style "copy manifest → install deps → copy code" para maximizar cache.

**Reality**: pyproject.toml tiene `[tool.setuptools.packages.find] where = ["src"]`. `pip install -e .` (editable mode) hace que setuptools lea `src/` en tiempo de install para registrar paquetes. Sin `src/` copiado todavía → `error: 'src' does not exist`.

**Resolución (commits `f07b2eb` + `abc5ced`)**:
1. Switch a non-editable: `pip install .` (containers de prod son immutable, no necesitan editable).
2. Copy `src/` antes del pip install.
3. Quitar `readme = "README.md"` de pyproject.toml (estaba bloqueando porque README.md está en `.dockerignore`).

**Lección**: el patrón de cache "manifest → deps → code" funciona en ecosistemas donde el manifest no referencia el code (Node, Go). En Python con setuptools+`packages.find` el manifest **necesita** ver el code. Para reusar layer cache en proyectos Python, generar un `requirements.txt` separado.

### D-008 🔴 Subagents en este harness NO tienen Bash/git/pip

**Asumido (planes Capa 1 y Capa 2)**: subagents pueden ejecutar pytest, pip, git via su tool surface "*".

**Reality (3 confirmaciones consecutivas)**:
1. Graphify-update agent: abortó al primer Bash call.
2. Code-executor Capa 2 Batch 1: escribió RED tests pero no pudo ejecutar pytest ni `pip install` ni `git commit`.
3. (Verificación implícita) los planes que asumían "subagent corre el TDD cycle completo" eran wishful thinking.

**Resolución**: pattern "write-only mode" para subagents. El agent escribe archivos, retorna lista + diff + comandos sugeridos. La main session corre tests + commits. Para Capa 2 Batch 2 ya lo aplico (lanzamiento de hoy).

**Lección permanente**: los planes para este harness deben asumir que subagents son **escritores no ejecutores**. Ningún task que requiera RED-verify-FAIL en un subagent es viable. Reformular a: "subagent escribe RED + GREEN; main session valida + commitea".

---

## Categoría 5 — Drifts proceso (CLAUDE.md ↔ práctica)

### D-009 🟠 ADR immutability rule suspendida para hardware fix

**Regla (CLAUDE.md §11)**: "ADRs with status `Aceptada` are immutable. Replacement requires a new ADR. The previous ADR keeps its content; only its status field changes to `Reemplazada` y `Reemplazada por: ADR-NNN`."

**Realidad (commit `57bfe81`)**: ADR-001 editado in-place. No se creó ADR-015 para superseder.

**Justificación explícita del usuario**: "claude esa regla de ADR immutability no vamos a considerarla ahora, REEMPLAZA lo que diga ADR y resto de WIKI. Estoy trabajando yo solo por ahora, mas adelante lo hacemos bien".

**Resolución**: edit in-place aceptado para esta etapa solo-yo. Documentado aquí para que cuando el equipo crezca, se haga la deuda técnica explícita: re-introducir ADRs de superseding para los cambios pre-team.

**Lección**: las reglas de gobernanza están escritas para el caso multi-equipo. En contexto solo-dev se pueden suspender deliberadamente, pero hay que **documentar la suspensión** para no perder la trazabilidad cuando llegue el momento de hacerlo bien.

### D-010 🟡 `ps-trazabilidad` corrió pero graphify update se difirió

**Regla (CLAUDE.md §8)**: "Any change in wiki structure → run `/graphify --update`".

**Realidad**: tras Capa 1 cierre + ADR-014 + hardware fix, el graphify update se intentó (sub-agent agente, falló por sandbox) y luego se difirió en main session porque correrlo durante el code-executor de Capa 2 Batch 1 implicaba snapshot transitorio + doble costo de tokens.

**Resolución pendiente**: correr `/graphify --update` una vez Batch 2 cierre (estado coherente: Capa 1 + Capa 2 Batches 1+2 commiteados). En cola.

**Lección**: el `--update` no es gratis (~50K tokens en este corpus). Conviene batchear los wiki changes y correr graphify una vez por capa cerrada, no después de cada commit.

---

## Categoría 6 — Drifts ambiguity → harden

### D-011 🟡 `OAUTH_TOKEN_ENC_KEY` formato no validado en `Settings`

**Asumido (.env.example)**: comentario "Generar con: python -c 'import secrets; print(secrets.token_urlsafe(32))'".

**Reality**: el dev placeholder en .env era `dev-only-32-chars-padding-padding` (32 chars literal pero NO válido base64). `auth.crypto._load_key()` raise al import del módulo (correcto, ERR-2 implementado). PERO: ningún test corría con la clave del .env real, todos usaban `monkeypatch`. La primera persona que corrió `pip install -e ".[pipeline]"` y arrancó el container localmente iba a chocar con el error.

**Resolución (parcial — code-executor agent fix)**: .env actualizado a una clave válida (32 zero bytes en base64) durante la ejecución del agent. Pero el .env del repo template (.env.example) sigue diciendo "completar con valor real" sin enforcement.

**Resolución completa pendiente**: agregar validador en `Settings` post_init que verifique que `OAUTH_TOKEN_ENC_KEY` decodifica a 32 bytes y `JWT_SECRET` tiene ≥32 chars. Capa 2 spec ya lo lista como ERR-2 + ERR-3 — implementarlo cuando llegue.

**Lección**: secrets / keys de longitud específica deben validarse en el boundary (Settings) no en el primer uso. Pydantic supports `field_validator` y `model_validator` que ejecutan al construir Settings.

### D-012 🟢 README.md gitignored pero pyproject.toml lo referenciaba

**Reality**: `.dockerignore` excluía explícitamente `*.md` y `README.md`. Al agregar `COPY README.md` al Dockerfile (para silenciar SetuptoolsWarning), el COPY falló porque README.md no estaba en build context.

**Resolución (commit `abc5ced`)**: removí `readme = "README.md"` de pyproject.toml entirely. El proyecto no es un package publicable a PyPI; el long_description metadata era residual del template inicial.

**Lección**: pyproject.toml referencia archivos que el contexto runtime puede no tener. Para servicios self-hosted (no published packages), simplificar pyproject.toml a lo mínimo necesario para `pip install`.

---

## Resumen ejecutivo

**Total drifts identificados**: 12 (1 categoría hardware, 2 wiki, 3 plan, 2 spec ops, 2 proceso, 2 ambiguity).

**Severidad**:
- 🔴 CRITICAL: 2 (D-001 hardware, D-008 subagent sandbox — ambas afectan futuros trabajos)
- 🟠 HIGH: 4 (D-002, D-004, D-006, D-007, D-009)
- 🟡 MEDIUM: 5 (D-003, D-005, D-010, D-011)
- 🟢 LOW: 1 (D-012)

**Drifts ya cerrados**: 11/12.

**Drifts pendientes de cierre**:
- D-010 (graphify --update post-Capa 2 Batch 2). En cola.
- D-011 (Settings validator para OAUTH_TOKEN_ENC_KEY/JWT_SECRET). Implementar en Capa 2 cuando se toque ERR-2/ERR-3.

**Pattern emergente para futuras capas**:
1. Antes de cerrar specs/ADRs, validar las asunciones físicas (hardware, lib semantics, dialect-specific types) con un experimento mínimo.
2. Subagents para escritura, main session para ejecución.
3. Suspensiones de reglas de gobernanza se documentan, no se silencian.
4. Plan code blocks son hipótesis ejecutable, no deuda compilada — esperar deviations en runtime.
