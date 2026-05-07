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

## Categoría 7 — Drifts del review multi-agente Capa 2 (2026-05-05)

> Las siguientes drifts surgieron de la auditoría multi-agente sobre Capa 2
> (auth Microsoft Entra + middleware MCP). Aplicación de fixes en commits
> de los grupos 1-6 del fix plan. Por instrucción del usuario ("anota
> wiki drifts para hacer correciones luego") las correcciones al wiki se
> defieren a una sesión dedicada. Estas entradas son la lista accionable.

### D-013 🟢 [CERRADO — wiki ya correcto] RF-AUTH-02 status code: SPEC drift, NO wiki drift

**Asumido (cuando se escribió esta entrada)**: el SPEC interno
`docs/sesiones/2026-05-05-capa2-auth-spec.md` decía 200; la wiki
`wiki/RF/RF-AUTH.md` también decía 200; la implementación devolvía 302.

**Reality (verificación 2026-05-05 sesión wiki dedicada)**: la wiki
**ya decía 302** desde el refactor 2.0 (RF-AUTH.md líneas 13 y 160 —
"302 redirect a /mcp-setup", "Responder 302 a /mcp-setup con Set-Cookie
session"). Solo el SPEC interno temporal divergía. La wiki nunca tuvo el
bug; mi entrada original de drift estaba basada en una mis-lectura del
estado de la wiki.

**Resolución elegida (CR-6, instrucción explícita Franco)**: mantener
302 en código + spec. Sin acción wiki necesaria (wiki ya correcta).

**Acción pendiente sobre wiki**: ninguna. (Originalmente listada como
"cambiar 200→302" pero la verificación mostró que no aplica).

**Lección**: cuando se documenta un drift, validar el estado actual de
la wiki/spec/código antes de listar acciones. Asumir el contenido del
file sin abrirlo lleva a entradas falsas en el log; el log pierde
credibilidad cuando el lector futuro descubre que "la acción ya estaba
hecha desde antes".

---

### D-014 🔴 ADR-014 listener: contrato cambió de fail-open a fail-closed (CR-5)

**Asumido (ADR-014 / SPEC-capa1-postgres-orm-v1 review S-1)**: el listener `do_orm_execute` inyecta `WHERE user_id = X` cuando `session.info["user_id"]` está armado. Cuando NO está armado, el listener es no-op (queries retornan todas las filas — patrón "admin/migration context").

**Reality post review**: el contrato fail-open es un silent-leak hazard. Si en alguna Capa futura se olvida el `Depends(get_current_user_*)` en una ruta, queries para-user-models retornan TODAS las filas. El reviewer multi-agente flaggeó esto como CR-5.

**Resolución (esta sesión)**: listener pasa a FAIL-CLOSED. Una query SELECT/UPDATE/DELETE contra un per-user model cuyo session no tiene `user_id` armado **ni** `scoping_bypass=True` → raise `ScopingNotArmedError`. El bypass se hace ahora vía `with bypass_scoping(session): ...` (S-5) en lugar de `session.info["scoping_bypass"] = True` inline.

**Sitios que ahora usan `bypass_scoping`**:
- `auth/dependencies.py::get_current_user_web` (lookup del User por session.sub).
- `auth/mcp_bearer.py::verify_bearer` (lookup del bearer por hash).
- `auth/routes.py::callback` (todo el upsert pre-armado).
- `tests/conftest.py::session` (armado una vez para todo el lifetime de la fixture).
- `tests/integration/test_scoping_enforcement.py` reescrito para el nuevo contrato.

**Acción pendiente sobre wiki**:
- Crear ADR-015 `Listener fail-closed por defecto` con status `Reemplaza ADR-014`. ADR-014 mantiene su contenido pero status pasa a `Reemplazada`. El usuario suspendió la regla de inmutabilidad para esta etapa solo-dev (D-009), pero esta superseding sí merece ADR formal porque cambia un invariante crítico de seguridad.
- `wiki/05_modelo_datos.md`: párrafo del listener actualizado al nuevo contrato.

**Lección**: defaults de seguridad deben fail-closed, no fail-open. El reviewer multi-agente capturó esto exactamente porque su contexto era zero (no había sido condicionado a la lógica original) — argumento adicional a favor del review sin context-poisoning.

---

### D-015 🟡 Pool reuse leak risk: `db.info["user_id"]` no se limpiaba post-request (CR-4)

**Asumido (Capa 1 batch 5)**: `async_session_factory()` crea AsyncSession nueva por request. Como las AsyncSessions no son pooled (solo las connections lo son), `session.info` no podría leakear entre requests.

**Reality**: el invariante se mantiene HOY pero no es robusto a refactors. Si alguien introdujera caching de sessions (ej. una long-lived session inyectada en un job o test), `info["user_id"]` se heredaría del request previo.

**Resolución (esta sesión)**: `get_session()` en `db/session.py` ahora limpia `db.info["user_id"]` y `db.info["scoping_bypass"]` en `finally`, defensa-en-profundidad. Costo: ~zero. Beneficio: hace explícito el contrato "session.info pertenece a un request, no se hereda".

**Acción pendiente sobre wiki**: ninguna (mejora interna, no cambia contratos públicos).

**Lección**: defensive cleanup en boundaries (request teardown, transaction commit) protege contra refactors futuros aunque hoy no sea necesario.

---

### D-016 🟡 RF-AUTH-01 multi-tab: caso no especificado (S-6)

**Asumido (RF-AUTH-01)**: usuario inicia /auth/login en una tab, completa MS Entra, vuelve al callback con cookies seteadas.

**Reality**: ¿qué pasa si abre /auth/login en TAB A, queda en MS Entra, y luego abre /auth/login en TAB B? La cookie `oauth_state` de B reemplaza la de A (mismo path / domain / name). Cuando A vuelve del callback con su state-param, no matchea la cookie ahora-de-B → AUTH_INVALID_STATE.

**Comportamiento actual**: ya está cubierto correctamente (state mismatch redirige a login con error). Pero el spec no lo documenta como caso esperado.

**Acción pendiente sobre wiki**:
- `wiki/RF/RF-AUTH.md` RF-AUTH-01: añadir sección "Multi-tab" explicando que la última /auth/login gana la cookie, las anteriores fallarán con AUTH_INVALID_STATE al volver. Usuario debe re-iniciar el flow.

**Lección**: especificar comportamiento bajo concurrencia del usuario en el cliente (multi-tab, multi-window) es parte del contrato funcional, no un edge case de implementación.

---

### D-017 🟡 ALT-1 logout no revoca bearer: requisito UI no documentado (S-3)

**Asumido (ALT-1 SPEC-capa2)**: logout limpia cookie web pero NO revoca el MCP bearer. Razón: el user puede estar usándolo desde Claude Desktop (sin browser).

**Reality**: este flujo es invisible al usuario en la UI actual. Si hace logout pensando "estoy completamente cerrando sesión", no entiende que el bearer sigue activo. Un atacante que robó el bearer aún tiene acceso después del logout.

**Acción pendiente sobre wiki**: añadir RF-AUTH-08 "Banner UI sobre estado del bearer en /mcp-setup y /auth/me". Mensaje sugerido:
> "Tu MCP bearer sigue activo después del logout web. Para revocarlo, usá `POST /auth/regenerate-mcp-token` o el botón 'Revocar bearer' en /mcp-setup."

Esto es trabajo de capa UI futura; no bloquea Capa 2.

**Lección**: cuando hay separación bearer-vs-cookie con TTLs distintos, el usuario necesita feedback visual del estado de cada uno. La spec funcional debe modelar el contrato de UI, no solo el de backend.

---

### D-018 🟡 Capa 6 (MCP) contract doc inexistente (S-2)

**Asumido**: Capa 6 tendrá su propio set de RFs cuando se diseñe.

**Reality**: ya hay decisiones tomadas en Capa 2 que dependen del contrato MCP futuro:
- `mcp_url` field en /auth/me apunta a `${PUBLIC_BASE_URL}/mcp` (M-3).
- Bearer scope: per-user, no per-tool, no per-resource.
- Per-user scoping listener (ADR-014/015) asume Capa 6 hace queries ORM normales.

**Acción pendiente sobre wiki**: crear stub `wiki/RF/RF-MCP-00.md` con el contract base (path, auth scheme, scoping, tool naming). No es completo — es un anchor para que /auth/me y middleware tests dejen de quedar como referencias colgantes.

**Lección**: cuando una capa hace forward references a otra, conviene crear un stub spec en la otra capa antes de cerrar la primera. Reduce ambigüedades.

---

### D-019 🟢 Encryption key rotation no implementada (S-1)

**Asumido**: `OAUTH_TOKEN_ENC_KEY` es un único secreto AES-256-GCM rotado raramente.

**Reality**: en el flujo actual, rotar la key requiere desencriptar y re-encriptar todos los `oauth_tokens.ms_*_encrypted` en una migración. Si la key se compromete y hay que rotar fast, hay que coordinar app-down + migration window.

**Acción pendiente (estratégica)**: prefijar el ciphertext con un key-id corto (e.g., 1 byte), permitir múltiples keys activas en `Settings.OAUTH_TOKEN_ENC_KEYS` (versionado), encrypt usa la primera, decrypt prueba todas. Rotación entonces es: agregar nueva key, dejar vieja para legacy ciphertexts, eventualmente migrar y descartar la vieja.

**Cuándo**: cuando haya ≥1 incidente de rotación operativa o auditoría externa lo pida. No urgente.

**Lección**: secretos versionados no son sobre-ingeniería si el operacional lo va a necesitar; pero pueden esperar al primer pinchazo si el blast radius es bajo (auth tokens MS, no datos del cliente).

---

### D-020 🟢 last_used_at throttle no implementado (S-7)

**Asumido**: cada hit a `/_test_mcp_*` o cualquier ruta MCP futuro UPDATE-ea `mcp_bearers.last_used_at`.

**Reality**: bajo carga (Capa 6 con MCP loop interactivo) cada llamada genera un UPDATE a la misma row. Postgres serializa, hay row-level locks, escalable pero no eficiente.

**Acción pendiente (estratégica)**: throttle a "actualizar last_used_at solo si > 5 min desde la última". Implementación: comparar `now() - last_used_at > 300s` antes de hacer el UPDATE.

**Cuándo**: cuando Capa 6 muestre tasa real de calls/segundo. No urgente para Capa 2.

**Lección**: side-effects de auditoría (last_used_at, last_login_at) escalan O(N) con request rate. Throttling en boundary del request hits acceptable accuracy con una fracción del costo.

---

### D-021 🟡 Test routes pollution: prod app mutada en `_register_test_routes_once` (H-7)

**Asumido (Capa 2 Batch 6)**: registrar rutas test-only `/_test_mcp_*` en la prod app es fine porque el prefijo `_test_` es no-colisionable y `include_in_schema=False` las esconde de OpenAPI.

**Reality**: la mutación de la prod app en import-time del test module:
1. Hace que los tests dependan del orden de import (el primer import registra, los siguientes son no-op).
2. Pollutiona la prod app aunque los tests no se hayan corrido para Capa 6 (cualquier inspección de `app.routes` post-pytest las verá).
3. Hace impossible testear con una prod app "limpia" en otro test.

**Acción pendiente (refactor menor)**: en lugar de mutar la prod app, crear un sub-app fixture per-test:
```python
@pytest.fixture
async def mcp_test_app():
    sub_app = FastAPI()
    sub_app.include_router(...)
    yield sub_app
```
y montar en una mini app con la dependencia `Depends(get_current_user_mcp)`.

**Cuándo**: pre-Capa 6 (antes de añadir más test routes). Trackear como follow-up.

**Lección**: tests que mutan singletons de prod son una bomba de tiempo. Sub-app fixtures son fáciles, isolan, y permiten parallel pytest sin races.

---

### D-022 🟢 Callback handler god-function: extracción a service deferida (S-4)

**Asumido**: el callback handler está bien encapsulado en `routes.py::callback`.

**Reality**: post Group 4 fixes (CR-1, CR-3, H-9), el handler tiene ~150 líneas con 5 retorno-de-error possible y 2 ramas de upsert. Sigue legible, pero ya cerca del límite.

**Acción pendiente (refactor estructural)**: cuando se añada Capa 7 (PKCE + Entra refresh flow) o Capa de logging estructurado, extraer `auth/callback_service.py` con funciones:
- `validate_callback_state(...)` → state cookie + state param + payload.
- `exchange_and_validate(...)` → exchange_code + validate_id_token + JWKS retry.
- `upsert_user_and_tokens(...)` → bypass_scoping + INSERT/UPDATE.
- `build_callback_response(...)` → cookies + redirect.

**Cuándo**: cuando el handler vuelva a crecer. No bloquea hoy.

**Lección**: 150 líneas en un handler son tolerables si las ramas son lineales (sequential validation pipeline). El smell viene cuando hay branching paralelo o lógica reutilizable que no se reusa por estar inline.

---

### D-029 🟡 Capa 3 Batch 1: single-stage Dockerfile elegido vs multi-stage del plan

**Asumido (`docs/sesiones/2026-05-05-capa3-pipeline-plan.md` Task 1.1)**: el GREEN
del Dockerfile usa un patrón multi-stage (`builder` con `nvidia/cuda:...-devel`,
`runtime` con `nvidia/cuda:...-runtime`) para no arrastrar compilers al runtime.

**Reality (este commit)**: el `Dockerfile` ya existía single-stage con justificación
in-place ("Single-stage build: la imagen runtime ya pesa ~5 GB por CUDA, multi-stage
no aporta"). Con `[pipeline]` extras la imagen final pesa ~10-12 GB; el ahorro
multi-stage de ~1-2 GB de compilers es marginal frente a la complejidad adicional
de una segunda stage + COPY explícito de site-packages.

**Resolución (commit `52f8de5`)**: mantener single-stage. Agregar pre-install de
torch+torchaudio con `--extra-index-url cu121` y luego `pip install ".[pipeline]"`.
Comment del Dockerfile actualizado para anotar el trade-off (CLAUDE.md §4 priority:
Simplicity > Performance).

**Acción pendiente**: ninguna. Si el rig en Task 7.3 reporta un build > 12 GB que
duele en transferencias, reconsiderar multi-stage en una sesión específica con
nuevo ADR.

**Lección**: cuando un plan propone una técnica (multi-stage), revisar si el costo
real (size, complexity) la justifica con la decision priority del proyecto antes
de aplicar literalmente. El plan es hipótesis; el código en producción es el voto
definitivo. Documentar el voto en este log evita que un futuro lector crea que
hubo descuido.

---

### D-030 🟡 Capa 3 Batch 1: heavy ML imports (torch, whisperx, pyannote) son lazy en `pipeline/{stt,diarize}.py`

**Asumido (plan T1.2 RED test)**: los tests patcheaban
`transcription_api.pipeline.stt.whisperx.load_model` y
`transcription_api.pipeline.diarize.Pipeline.from_pretrained`, lo que requiere
que `whisperx` y `pyannote.audio` estén importados al top del módulo.

**Reality (este commit)**: las dev/CI machines son CPU-only y no tienen
`[pipeline]` extras instalados (D-008 ya lo señaló para subagents; aquí se
extiende a la suite local). Importar `whisperx` o `pyannote.audio` al top del
módulo rompería el `import transcription_api.pipeline.stt` en cualquier máquina
sin extras, lo que a su vez rompería `tests/unit/pipeline/test_model_loaders.py`
y el `from .pipeline import stt` en `main.py`.

**Resolución (commit `5b9a9ff`)**: introduzco indirecciones internas
`_whisperx_load_model(...)` (en `stt.py`) y `_pyannote_from_pretrained(...)`
(en `diarize.py`) que importan la lib pesada dentro del cuerpo de la función.
Los tests patchean estas indirecciones en lugar de la lib upstream. El módulo
es importable sin `[pipeline]` extras; sólo invocar el loader requiere la lib.

**Acción pendiente**: ninguna inmediata. Los Batches 3-5 deben mantener el
mismo patrón cuando agreguen wrappers `transcribe`, `diarize`, `merge` (las
indirecciones siempre dentro de `pipeline.*`, nunca en `main.py` ni en
`api/transcriptions.py`).

**Lección**: planes que asumen heavy imports al top funcionan en la dev box
del autor pero rompen en CI / en máquinas magras. Cuando el extras está
gated por hardware (GPU), priorizar lazy imports + indirección patcheable
es un default robusto, no over-engineering.

---

## Categoría 8 — Drifts del deployment al rig (operacionales, 2026-05-05)

> Estas drifts surgieron durante el primer levantamiento real de la imagen
> Docker en el rig RTX 4060 Ti, después del merge de Capa 2 a master. Son
> bugs / asunciones que pasaron desapercibidos en dev local porque la
> superficie de validación (pytest contra la `.venv` con `[dev]` extras)
> no ejerce las mismas paths que `pip install .` dentro de un container.
> Capturadas para que futuros deployments no las repitan + para informar
> el "deployment runbook" de Capa 7 cuando exista.

### D-031 🟠 logging.json access formatter: campos `client_addr` / `request_line` / `status_code` no existen en uvicorn LogRecord

**Asumido (`src/transcription_api/logging.json` original Capa 1)**:
```json
"access": {
  "format": "%(asctime)s %(levelname)s access %(client_addr)s \"%(request_line)s\" %(status_code)s"
}
```
La asunción era que el `uvicorn.access` logger emite LogRecords con
atributos nombrados `client_addr`, `request_line`, `status_code`.

**Reality (logs del primer arranque en rig 2026-05-05)**: uvicorn emite
los datos como argumentos posicionales dentro del template del mensaje
(`'%s - "%s %s HTTP/%s" %d', client_addr, method, path, version, status`)
y el LogRecord NO tiene esos atributos como `record.client_addr` etc. El
formatter custom raise `KeyError: 'client_addr'` en cada GET /health,
contaminando el stdout con un traceback completo por request HTTP exitoso.
La request en sí seguía funcionando (status 200, body correcto); solo
el handler de logging emitía el error.

**Resolución (commit `6dcacc4`)**: cambio del format a
`"%(asctime)s %(levelname)s access %(message)s"` que usa el message
template ya formateado por uvicorn. Output ahora limpio:
`2026-05-05 18:35:29 INFO access 127.0.0.1:43932 - "GET /health HTTP/1.1" 200`.

**Acción pendiente**: ninguna. Cubierto en el commit del rig fix.

**Lección**: la review multi-agente NO detecta bugs en archivos de
configuración (logging.json, .env, Dockerfile) porque esos files están
fuera del análisis de código Python. Para futuras capas, considerar un
smoke test post-build dentro del container que ejecute al menos
`GET /health` y grep ausencia de tracebacks en stderr antes de declarar
la imagen "verde". Sin esto, el bug solo aparece en runtime real.

---

### D-032 🟠 `httpx` declarado en `[dev]` extras pero usado en runtime por `oauth_client`

**Asumido (`pyproject.toml` original Capa 1)**:
```toml
[project.optional-dependencies]
dev = [
    ...
    "httpx>=0.27.0",  # FastAPI test client + tests respx
    ...
]
```
Capa 1 no usaba `httpx` en runtime (FastAPI usa Starlette internamente,
no necesita httpx). `[dev]` extras lo trae para que pytest pueda usar
`AsyncClient` + `respx` en tests. Funcionaba localmente porque la
`.venv` se instala con `pip install -e ".[dev]"`.

**Reality (logs del primer build en rig 2026-05-05)**:
```
ModuleNotFoundError: No module named 'httpx'
  File "src/transcription_api/auth/routes.py", line 47, in <module>
    from .oauth_client import (...)
  File "src/transcription_api/auth/oauth_client.py", line 33, in <module>
    import httpx
```
Capa 2 introdujo `auth/oauth_client.py` que usa `httpx.AsyncClient`
**en runtime** (para llamar `/token` y `/discovery/v2.0/keys` de MS).
Esto se omitió al promoverlo de "uso en tests" a "uso en producción".
La imagen Docker instala solo el core (`pip install .` sin extras),
así que el container crash-loopaba en el `from .auth import router`
durante startup.

**Resolución (commit `d034b51`)**: promover `httpx>=0.27.0` del
extras `[dev]` al `dependencies` core. La duplicación en `[dev]`
quedó (pip dedupea automáticamente, no rompe nada).

**Acción pendiente**: ninguna. Para Capa 3+, agregar al checklist de
PR review pre-merge: si un módulo nuevo importa una lib de `[dev]`
extras, mover a core ANTES del merge.

**Lección**: el tipo de drift "passes locally fails in container" es
recurrente cuando la dev box tiene más deps instaladas que la imagen
prod. Un CI step que haga `pip install .` (sin extras) + import del
package raíz en una imagen limpia hubiera atrapado esto sin necesidad
de rig deployment. Considerar agregar al .github/workflows o equivalente
cuando se introduzca CI.

---

### D-033 🟡 `POST GRES_PASSWORD` se hornea en el volume al primer `initdb`; cambios en `.env` no rotan la auth

**Asumido (operador siguiendo el deployment guide)**: cambiar
`POSTGRES_PASSWORD` en `.env` y reiniciar `docker compose` propaga la
nueva password al servicio de Postgres.

**Reality (primer `alembic upgrade head` en rig 2026-05-05)**:
```
psycopg.OperationalError: FATAL: password authentication failed for user "transcription"
```
El image oficial de `postgres:16-alpine` solo aplica `POSTGRES_USER` /
`POSTGRES_PASSWORD` durante `initdb` (primer arranque del volume vacío).
Una vez el volume tiene data, la imagen ignora esos env vars y la auth
se chequea contra el password original baked into `pg_hba.conf` +
`pg_authid`. Si el operador inicializó el volume con un placeholder
(`change-me-in-production`) y después cambió `.env` al password real,
el cluster sigue auth-eando con el placeholder.

**Resolución (workaround dev)**:
```bash
docker compose down -v   # -v borra el named volume postgres-data
docker compose up -d postgres   # initdb corre fresh con .env actual
```
Solo seguro en dev (data se pierde). En prod la rotación correcta es
`ALTER USER ... PASSWORD '...';` con superuser dentro del cluster
running.

**Acción pendiente**:
- Documentar en el deployment runbook (Capa 7) los dos casos:
  primer-arranque vs rotación.
- Considerar agregar al `entrypoint.sh` de la imagen un check que
  emita un WARN si `POSTGRES_PASSWORD` del `.env` difiere del que
  realmente acepta el cluster (no es trivial; podría ser un test
  de connection con timeout).

**Lección**: las imágenes oficiales de bases de datos tienen un
contrato de "init-once, operate-forever" que el operador novato no
intuye. Cualquier env var marcada como "credentials" en las imágenes
oficiales (Postgres, MySQL, MongoDB, Redis) tiene esta misma semántica.
El runbook de deployment debe ser explícito al respecto desde el día
uno.

---

### D-036 🟢 Capa 3 Batch 5: cache stores `cache_hit: false` canónicamente, override a `true` en read-time

**Asumido (intuición naïve)**: si el orchestrator detecta un cache hit
(ALT-1) y persiste la fila marcada como `cache_hit: true`, lo natural
sería ESCRIBIR `cache_hit: true` en el archivo del filesystem para que
una lectura futura lo refleje.

**Reality (este commit, `c9e6284`)**: el cache filesystem siempre
guarda `metadata.cache_hit: false`. Cuando un cache hit ocurre, el
orchestrator hace un override en memoria: `payload = {**cached,
"metadata": {**cached.metadata, "cache_hit": True}}`. La fila de DB
y el response sí llevan `cache_hit: true`; el archivo en disk no.

**Resolución**: mantener la asimetría. El cache file representa "el
trabajo computado por una corrida pasada" — su metadata es congelada
al momento del compute (modelo, diarizer, compute_type). El flag
`cache_hit` describe **esta** request, no la corrida histórica que
generó el payload. Si todas las re-reads escribieran `true` en disk,
después de N hits el archivo perdería la información de "cuándo se
computó realmente esto" (porque cada hit pisa la metadata).

**Acción pendiente**: ninguna. La invariante es un detalle del
orchestrator y los tests T5.4 lo cubren explícitamente
(`test_orchestrate_cache_hit_skips_stt_and_diarize` verifica
`result.metadata.cache_hit is True`).

**Lección**: cuando un flag existe en dos planos (request y storage),
elegir cuál es la **fuente de verdad** y derivar el otro. Aquí el
storage es immutable post-compute (`compute_hit: false` siempre);
el flag de hit-vs-miss es una propiedad de la request, no del payload.

---

### D-037 🟢 Capa 3 Batch 5: orchestrator hace `flush()`, no `commit()`

**Asumido (en algunos planes)**: los servicios de aplicación (use
cases) controlan su propia transaction, terminándola con commit/rollback.

**Reality (este commit, `c9e6284`)**: `_run_pipeline` hace `db.add(row)`
y `await db.flush()` — pero NO `commit()`. La transacción queda
abierta; el caller (la dependency `get_session()` en Batch 6) decide
commit en el happy path y rollback si la request raisea.

**Resolución**: mantener flush-only. Esto matchea el patrón FastAPI
+ SQLAlchemy 2.x async: la dependency es la dueña de la transaction
(commit en `finally`, rollback en `except`). El orchestrator es
re-usable en jobs offline (donde el caller maneja la transaction
diferente) sin tener que parametrizar el commit boundary.

**Acción pendiente**: cuando Batch 6 escriba el endpoint POST
`/api/transcriptions`, asegurar que la dependency `get_session()`
hace `await session.commit()` post-orchestrate (o tras un `try/except`
que rollback). Si esa dependency aún no existe, crearla con ese contrato.

**Lección**: separar "build the row" (orchestrator) de "commit the
transaction" (request lifecycle owner) hace que el orchestrator sea
re-usable en background workers sin tocar su firma.

---

### D-035 🟢 Capa 3 Batch 4: ALT-3 implementado como cap-by-relabel, no como pipeline re-run

**Asumido (`docs/sesiones/2026-05-05-capa3-pipeline-spec.md` ALT-3)**:
> ALT-3: pyannote detecta más speakers que el `max_speakers` hint
> → Honor el hint: re-run con `min=max=hint` o usar el resultado capped.
> Decisión: respetar el hint estricto.

La frase "re-run con min=max=hint" sugiere invocar el pipeline pyannote
una segunda vez con los parámetros forzados — lo cual duplica latencia
GPU y pico de VRAM en cada llamada con cap activo.

**Reality (este commit, `25de530`)**: el wrapper implementa
`_cap_speakers_by_duration` que opera sobre la salida ya producida —
una sola pasada por pyannote, una sola pasada extra en CPU para:
1. Sumar duración por speaker.
2. Quedarse con los top-N por duración total (most-talkative win).
3. Relabel cada segmento de un speaker no-top-N al speaker top-N
   temporalmente más cercano (mid-point distance).

**Resolución**: mantener cap-by-relabel. El spec acepta "o usar el
resultado capped" como alternativa válida. El trade-off es:
- **Pros**: una sola corrida del pipeline (~30s/min de audio en RTX
  4060 Ti); sin pico extra de VRAM; sin riesgo de que el segundo run
  con `min=max` no converja.
- **Cons**: el resultado es ligeramente menos preciso porque pyannote
  no "decidió" producir N speakers, sino que el wrapper colapsó
  N+k a N a posteriori. Para reuniones con un speaker que habla 1%
  del tiempo, ese speaker desaparece (queda relabeleado al cercano).

**Acción pendiente**: si en Task 7.3 (rig smoke) un audio real revela
que la pérdida de precisión es problemática, considerar agregar una
flag `cap_strategy = "relabel" | "rerun"` y elegir por env var.
Mientras tanto, default cap-by-relabel.

**Lección**: cuando el spec ofrece dos alternativas equivalentes, el
implementador elige la más simple por default (CLAUDE.md §4 priority:
Simplicity > Performance > Cost) y documenta la decisión para que el
reviewer la pueda contestar sin tener que descubrir el trade-off
leyendo código.

---

### D-042 🟠 Capa 3 deployment: pyannote 4.x requiere TRES HF model accepts (no dos)

**Asumido (spec §0.3 + deployment guide)**: aceptar terms en HF para
`pyannote/speaker-diarization-3.1` y `pyannote/segmentation-3.0`.

**Reality (rig deployment 2026-05-06, primer intento)**: pyannote.audio
4.x agregó un **tercer modelo gated**, `pyannote/speaker-diarization-community-1`,
que contiene el PLDA artifact (`xvec_transform.npz`) usado internamente
por la pipeline `speaker-diarization-3.1`. Sin terms accept, el load
falla con `huggingface_hub.errors.GatedRepoError: 403... Cannot access
gated repo`.

Cadena de causalidad observada en el rig:

1. Operator acepta terms en los dos modelos del spec.
2. Build pasa (los modelos se descargan lazy en runtime).
3. Lifespan corre: Whisper carga ✓, pyannote carga → `Pipeline.from_pretrained`
   triggea descarga del PLDA del tercer modelo → `GatedRepoError` 403.
4. H-5 classifier match `"gated" in str(exc) or "403" in str(exc)` →
   `DETAIL_TERMS_NOT_ACCEPTED`. /health surfacea el detail.
5. Operator visita https://huggingface.co/pyannote/speaker-diarization-community-1,
   acepta terms. Restart. Carga OK.

**Por qué no lo atrapamos en review**: el modelo `community-1` es
transitive dependency interna de pyannote 4.x. No aparece en docs
top-level del modelo `speaker-diarization-3.1`. Solo se descubre al
ejecutar `Pipeline.from_pretrained` contra HF en runtime.

**Resolución (2026-05-06)**: documentar el tercer model accept como
parte del deployment guide. Sin código a cambiar — el classifier H-5
ya surfacea bien el error, el operator solo necesita saber qué hacer
cuando lo ve.

**Acción pendiente sobre wiki/spec**: agregar a `wiki/RF/RF-TRX.md`
prerequisitos sección "HF model accepts" listando los TRES modelos
(no solo dos). Idem en el deployment runbook futuro (D-033).

**Lección**: las dependencias gated transitivas son invisibles hasta
runtime contra HF real. Mitigación: smoke test contra HF en CI o
pre-deploy script que valide el token tiene acceso a los N modelos
listados, en vez de descubrirlo en producción.

---

### D-038 🟢 Capa 3 review SD-3: audio_hash es del PCM puro, no del WAV completo

**Asumido (implementación inicial Batch 2)**: `audio_hash = SHA-256(WAV bytes)` —
hashea el output completo de ffmpeg incluyendo el header RIFF + cualquier
sub-chunk de metadata que ffmpeg emita (LIST INFO, JUNK, bext, …).

**Reality detectada en review (R1:M-9)**: ffmpeg cambia los metadata chunks
entre versiones. Un `docker compose build --no-cache` que sube ffmpeg
0.4.x → 0.5.x invalida TODO el cache filesystem aunque las muestras PCM
sean idénticas. Resultado: el cache es ffmpeg-version-dependent.

**Resolución (commit que cierra G6)**: nueva función
`_sha256_wav_pcm(path)` que parsea el RIFF y hashea SOLO el cuerpo del
chunk `data`. Fallback al full-file hash si el archivo no es RIFF/WAVE
(defense para tests + edge cases). El cache key queda estable a través
de upgrades de ffmpeg.

**Decisión Franco**: PCM puro (default propuesto, confirmado).

**Trade-off documentado**: la función es ~30 LOC más que el hash full-file,
pero ahorra invalidaciones de cache enteras. La complejidad vive en el
parser RIFF, que es estándar y bien-documentado.

**Lección**: hashes derivados de archivos generados deben hashear el
contenido SEMÁNTICO (PCM samples), no la representación física (header +
chunks). Cualquier metadata-by-side-effect (timestamps, version strings,
encoder name) hace el hash inestable a través de upgrades.

---

### D-039 🟢 Capa 3 review SD-4: num_speakers se cuenta desde merged segments

**Asumido (implementación Batch 5)**: `num_speakers = len({seg.speaker for seg in merged_segments if seg.speaker})`.

**Ambigüedad detectada en review (R1:M-1)**: si pyannote detecta 3 speakers
pero uno no recibe palabras de Whisper (e.g., "ah" o "mm" muy corto que
WhisperX descartó), `num_speakers` queda en 2 — no en 3.

**Decisión Franco**: mantener desde merged segments (default propuesto).
Razón: matches el mental model del user "los hablantes que veo en la
transcripción", no el conteo interno de pyannote.

**Implicación**: si un futuro requirement pide "número real de speakers
detectados por pyannote" (e.g., para reporting), agregar un campo
adicional `metadata.diarized_speakers_count` sin tocar `num_speakers`.

**Acción pendiente**: ninguna — comportamiento actual es el deseado;
documentación de la ambigüedad cierra el drift.

**Lección**: cuando dos sources of truth divergen ligeramente, decidir
explícitamente cuál se expone en el contrato público y mantener el
otro disponible si se necesita downstream.

---

### D-040 🟢 Capa 3 review SD-5: min_speakers se forwardea a pyannote como hint, no se enforce

**Asumido (implementación Batch 4)**: `min_speakers` y `max_speakers` se
pasan a `pyannote.Pipeline.__call__(...)` como kwargs y respetan lo que
pyannote devuelva.

**Ambigüedad detectada en review (R1:M-5, R3:M-3)**: si el cliente manda
`min_speakers=3` y pyannote devuelve solo 1 (porque genuinamente no
puede encontrar más en el audio), ¿error o aceptar? El spec no especifica.

**Decisión Franco**: forwardear como hint, aceptar lo que pyannote
devuelva (default propuesto). Razón: forzar el conteo no tiene
mecanismo confiable; pyannote es la autoridad sobre el audio real.

**Implicación**: el cliente NO debe asumir que `min_speakers=N` garantiza
N speakers en la salida. Es un hint, no un constraint.

**Acción pendiente**: ninguna en código. Vale la pena documentar este
contrato en RF-TRX wiki post-merge para que clientes futuros lo sepan
sin leer drift logs.

**Lección**: hints externos nunca son enforced strict en presencia de
constraints físicos del input. El contrato debe ser claro sobre hint
vs requirement.

---

### D-041 🟢 Capa 3 review SD-6: audio silente se cachea (resultado vacío canónico)

**Asumido (implementación Batch 5)**: cuando STT retorna `segments: []`
(audio puro silencio), el orchestrator persiste un row + cache file con
`text_content: ""`, `num_speakers: 0`, `metadata.silent_audio: true`.

**Ambigüedad detectada en review (R1:M-6)**: ¿cachear audio silente o
no? Trade-off:
- Cachear: el segundo upload del mismo archivo silente se sirve desde
  cache (~5s ahorrados).
- No cachear: el cliente puede re-grabar (e.g., arregló el mic) y la
  segunda corrida re-procesa.

**Decisión Franco**: cachear (default propuesto). Razón: Privacy no
cambia (el cache sigue siendo per-user, D-027); Performance gana sin
costo. El cliente que re-grabe va a tener un `audio_hash` distinto
porque las muestras PCM cambian — el hash del silencio "real" del mic1
no colisiona con el hash del silencio "real" del mic2.

**Implicación**: la entrada `metadata.silent_audio: true` en el cache
es informativa pero no afecta el comportamiento del cache hit.

**Acción pendiente**: ninguna — comportamiento implementado matches
la decisión.

**Lección**: cuando el cache key es derivado del contenido (PCM hash,
SD-3), las "categorías" del contenido (silente, multi-speaker, etc.)
pueden cachearse uniformemente sin lógica especial. El hash hace el
distinguishing.

---

### D-034 🟢 Capa 3 Batch 3: `stt.transcribe` retorna dict canónico, no `list[Segment]`

**Asumido (`docs/sesiones/2026-05-05-capa3-pipeline-plan.md` Task 3.1 RED)**:
> con modelo mockeado que retorna shape canónico de WhisperX
> (`{"segments": [...], "language": "es"}`), `transcribe()` retorna
> `list[Segment]` con `start/end/text/words`.

**Reality (este commit, `f31253e`)**: el wrapper retorna el dict
upstream tal cual (`{"segments": [...], "language": "..."}`). Tirar
`language` aquí significaría que el orchestrator (Batch 5) tendría que
hacer una segunda pasada por el audio para detectar idioma — el field
es parte de la response (`metadata.language`, spec §1.1) y de la fila
en `transcriptions` (Capa 1, columna `language`).

**Resolución**: mantener el dict shape upstream. Tests de Batch 3
(`test_stt_transcribe.py`) verifican `out["segments"]` y `out["language"]`,
no una bare list. El test del plan T3.1 RED dice "list[Segment] con
start/end/text/words" — interpretado como "los segments ya tienen ese
shape", no como "transcribe() retorna list".

**Acción pendiente**: ninguna. La frase del plan es ambigua; el código
y los tests hacen el contrato concreto. Si el orchestrator Batch 5
quiere acceso bare a la lista, lo hace con `result["segments"]`.

**Lección**: cuando un plan describe shapes de I/O, preferir mostrar el
ejemplo completo del retorno antes que abreviar a "list[T]". La
ambigüedad entre "retorna lista" y "tiene una lista adentro" cuesta una
decisión de diseño que el código termina forzando, y la decisión
puede ser irreversible (cambiar shape en Batch 5+ obliga a tocar
orchestrator + API + tests).

---

## Categoría 9 — Drifts del spec Capa 4 (2026-05-06)

> Drifts surgidos al verificar empíricamente los supuestos antes de escribir
> el spec de Capa 4 (MCP server). Capturados durante la auditoría de
> compatibilidad RF-MCP ↔ schema actual.

### D-043 🟡 ADR-011 lista dos tools separadas para image upload; RF-MCP-01 unifica via `kind`

**Asumido (`wiki/ADR/ADR-011.md` Tools MCP)**: el contrato MCP-first incluye
DOS tools separadas para imágenes:
- `request_image_upload_url(transcription_id)` → URL para subir imagen.
- `attach_image(transcription_id, image_id, caption?)` → Asociar imagen
  previamente uploaded a la transcripción.

Y un resource `user://me/transcriptions` que es proxy a `list_my_transcriptions`.

**Reality (`wiki/RF/RF-MCP.md`, refactor 2026-05-04)**: el RF unificó audio
e imagen en una sola tool `request_upload_url(kind, file_size_bytes,
mime_type?, transcription_id?)`. La asociación imagen-transcripción ocurre
**en el momento del upload** via el campo `transcription_id` de la
`upload_session`, no via tool separada `attach_image`. El resource
`user://me/transcriptions` no existe — la tool `list_my_transcriptions`
cubre el caso.

**Por qué se decidió así**: una sola superficie tool por kind reduce el
contrato MCP de 9 tools a 7. La asociación implícita por `transcription_id`
en la upload session evita una segunda round-trip MCP. RF-MCP-01 step 7
ya retorna `image_id` cuando `kind=image`, así que no hay nada que
"attach" después.

**Resolución**: el RF gana (canonical, autoritativo, fecha posterior). El
ADR-011 queda con drift documentado pero **no se modifica** (regla de
inmutabilidad de ADRs Aceptadas, CLAUDE.md §11). El spec de Capa 4
referencia el RF como contrato y el ADR como rationale histórico de la
decisión "MCP-first + REST mínimo para blobs".

**Acción pendiente sobre wiki**: ninguna (el RF ya canoniza el contrato;
el ADR queda inmutable). Si se decide revisitar el ADR con un ADR-016
("Refinar superficie de tools MCP"), hacerlo en sesión dedicada.

**Lección**: cuando un ADR enumera artefactos concretos (lista de tools,
endpoints), esa enumeración tiende a stale en cuanto se hace el primer
refactor del contrato. Mejor que el ADR exprese **principios** ("MCP-first
+ REST mínimo para blobs") y el RF enumere artefactos. El ADR-011 actual
mezcla ambos; futuros ADRs deberían separar nivel-de-decisión vs
nivel-de-artefacto.

---

### D-044 🟠 `upload_sessions` schema sin columna para el ephemeral `bearer_for_upload`

**Asumido (RF-MCP-01 step 3 + RF-MCP-03 step 4)**: existe un bearer
ephemeral de 32 chars random llamado `bearer_for_upload` que:
1. Se genera en `request_upload_url` y se persiste en la upload session.
2. Se entrega plaintext al cliente MCP (una sola vez).
3. Se valida en `POST /api/upload` contra el header `Authorization: Bearer <plaintext>`.

**Reality (verificación 2026-05-06 sobre `db/models/upload_session.py` +
migración `352c7acf6f15_initial_schema.py`)**: el schema de la tabla
`upload_sessions` tiene `bearer_id` (FK a `mcp_bearers.id` — el bearer
del MCP que originó el request) pero **NO tiene columna para persistir
el ephemeral bearer**. `nonce` viaja en query string (URL) y no debe
reusarse como bearer (Privacy: leak de URL = full bypass). Sin columna
para el hash del ephemeral bearer, `RF-MCP-03 step 4` ("Validar
`Authorization` bearer match con el `bearer_for_upload` registrado") no
es implementable contra el schema actual.

**Decisión Franco (2026-05-06)**: Opción A — agregar columna
`upload_bearer_hash TEXT NOT NULL` (SHA-256 hex del plaintext, mismo
patrón que `mcp_bearers.token_hash`). Privacy > Simplicity: defense-in-
depth aunque alguien lea logs/proxies, y el cost es 1 alembic migration
+ 5 LOC de hashing.

**Resolución (esta sesión, 2026-05-06)**:
- `wiki/05_modelo_datos.md` §2 `upload_sessions`: agregada la columna
  `upload_bearer_hash TEXT NOT NULL` con descripción referenciando
  RF-MCP-01 step 3 + RF-MCP-03 step 4.
- `wiki/RF/RF-MCP.md` RF-MCP-01 step 3+5+7: explicitar que el plaintext
  se hashea SHA-256 antes del INSERT y se entrega al cliente en step 7.
- `wiki/RF/RF-MCP.md` RF-MCP-03 step 4: actualizar la validación a
  `received_hash = SHA-256(header_plaintext).hex()` con `hmac.compare_digest`
  contra `upload_sessions.upload_bearer_hash`.
- Capa 4 Batch 0 (a redactar en spec): alembic migration que agregue la
  columna + index opcional. Modelo `UploadSession` actualizado.

**Acción pendiente**: la migration + ORM update queda en Capa 4 Batch 0.

**Lección**: cuando RF y schema se escriben en paralelo (Capa 1 schema
+ Capa 6 RFs), los campos derivados de runtime patterns (hash de un
plaintext que NUNCA se persiste) se omiten fácil. Para próximos cambios
de schema vs RF, hacer un grep cruzado: cada `INSERT` / `UPDATE` listado
en process steps de un RF debe matchear las columnas del modelo ORM 1:1.

---

### D-045 🟢 Capa 4 Batch 0: lint policy choque con frozen-file constraint

**Asumido (Capa 4 plan §5 step 4)**: `ruff check src/ tests/ alembic/` debe
salir clean post-Batch-0.

**Reality (esta sesión)**: el initial migration
`alembic/versions/352c7acf6f15_initial_schema.py` (frozen per Capa 4 §7
hard constraints: "Do NOT modify ... frozen") tiene 4 warnings ruff
preexistentes — 3× `UP007` (`Union[X, Y]` en `down_revision` /
`branch_labels` / `depends_on` debería ser `X | Y` por `target-version =
py310`) y 1× `UP035` (`from typing import Sequence` debería migrar a
`from collections.abc import Sequence`). `alembic/env.py:96` también
tiene un `N806` preexistente (`ALEMBIC_LOCK_KEY` debería ser lowercase).

**Resolución (esta sesión)**: lint clean en TODO lo que esta sesión toca
(`src/`, `tests/`, `alembic/versions/1a4f8c9b2d6e_add_upload_bearer_hash.py`).
El frozen file y `env.py` se dejan como están — modificarlos viola §7.
La interpretación operativa de §5 step 4 es "no introducir regresiones
de lint en archivos NUEVOS o tocados por la batch"; el legacy preexistente
es scope de un cleanup session futuro.

**Acción pendiente**: una sesión de lint cleanup (Capa 5 cleanup window
o post-merge stabilization) que: (a) desfroze el initial migration solo
para auto-fix `UP007` + `UP035` (cambios cosméticos, NO schema), (b) fix
`N806` en `env.py` o agregue per-file-noqa con justificación. Alternativa
menos invasiva: agregar `[tool.ruff.lint.per-file-ignores]` para
`alembic/versions/352c7acf6f15_initial_schema.py` y `alembic/env.py` en
`pyproject.toml`. La decisión cae fuera de Capa 4.

**Lección**: cuando un plan agrega un directorio nuevo al lint scope
(Capa 4 §5 incluye `alembic/` por primera vez en la suite), correr el
lint sobre ese directorio ANTES de redactar el plan para detectar legacy
debt — sino la batch hereda el conflicto entre "lint clean" y "frozen
file".

---

### D-046 🟡 Capa 4 Batch 1: plan asume `BearerInvalid` / `BearerRevoked` exceptions que no existen

**Asumido (`plan` Batch 1 Task 1.4 middleware skeleton)**:

```python
from ..auth.mcp_bearer import verify_bearer, BearerInvalid, BearerRevoked
...
try:
    bearer_row = await verify_bearer(session, plaintext)
except BearerInvalid:
    raise McpAuthError("MCP_BEARER_INVALID")
except BearerRevoked:
    raise McpAuthError("MCP_BEARER_REVOKED")
```

**Reality (`auth/mcp_bearer.py` actual)**: `verify_bearer` retorna
`User | None` (no exceptions tipadas). Además filtra por
`revoked_at IS NULL`, así que un bearer revoked retorna `None` igual
que un bearer inexistente — el caller no puede distinguir
`MCP_BEARER_INVALID` de `MCP_BEARER_REVOKED` con esa API.

**Resolución (este batch, commit T1.4 GREEN)**: el middleware `mcp/middleware.py`
hace su propia query directa contra `mcp_bearers` (con `bypass_scoping`
para la lookup cross-user) que NO filtra por `revoked_at`. Inspecciona
el campo después: `row is None` → `INVALID`; `row.revoked_at is not None`
→ `REVOKED`; sino → ok + bump `last_used_at`. El `verify_bearer` de Capa 2
queda intacto (§7 prohibe modificar `auth/`).

**Acción pendiente**: ninguna. Si una Capa futura quiere unificar la
distinción INVALID-vs-REVOKED en una sola helper, mover esa lógica a
`auth/mcp_bearer.py` con un nuevo `verify_bearer_with_status()` que
retorne `("ok"|"invalid"|"revoked", User|None)`. Mientras tanto, los
dos call-sites (web cookie auth + MCP middleware) tienen contracts
distintos y duplicar 8 LOC es cheaper que el refactor.

**Lección**: planes que listan imports específicos de helpers de capas
previas son hipótesis, no contratos. Validar la API real con `grep` o
`python -c "from X import Y"` antes de redactar el GREEN skeleton.

---

### D-047 🟢 Capa 4 Batch 1: tests del middleware usan raw POSTs en vez del MCP client SDK

**Asumido (`plan` Batch 1 Task 1.3 RED tests)**:

```python
async def test_mcp_revoked_bearer_returns_401(mcp_client_with_bearer, ...):
    resp = await mcp_client_with_bearer(plaintext).call_tool("_test_ping", {})
    assert resp.error.data["error_code"] == "MCP_BEARER_REVOKED"
```

Implica usar el MCP client SDK (`mcp.client.streamable_http`) + un
fixture factory `mcp_client_with_bearer(plaintext)` + un test-only tool
`_test_ping` registrado en el server.

**Reality (este batch, T1.3 RED)**: implementé los tests con raw POSTs
via `httpx.ASGITransport` + un payload JSON-RPC `initialize` minimal.
La aserción es sobre el status code (`!= 401` proves middleware passed)
y el body (`detail.error_code` para 401s). Sin client SDK, sin tool
helper, sin fixture factory.

**Resolución**: cubrir el mismo contract con menos infra. AC-8 sale
testeado por status + error_code (qué importa). AC-14 sale testeado
por DB re-fetch del row antes/después del POST (`last_used_at` se
bumpea durante AUTH del middleware, independiente de si la tool
realmente se ejecuta después). El `_test_ping` tool quedó deferido
hasta que algún batch posterior lo necesite.

**Acción pendiente**: ninguna. Si Batch 4 (`get_user_info` etc.)
quisiera testar el SDK end-to-end (handshake completo + listResources
+ callTool), entonces el `mcp_client` fixture vale crear. Hasta
entonces, raw POSTs son el camino más simple.

**Lección**: cuando un plan especifica una API de cliente para tests
(fixture factory + SDK client + decorated test tool) y la batch en
cuestión solo necesita testar el LAYER de auth, simplificar el test
a raw HTTP es ROI > 1. La cobertura del contract es la misma; la
fragilidad del test cae (independencia de la API del client SDK).

---

## Resumen ejecutivo

**Total drifts identificados**: 40 (10 Capa 2 review + 2 Capa 3 Batch 1 + 3 operacionales rig + 1 Batch 3 + 1 Batch 4 + 2 Batch 5 + 4 Capa 3 review post-fix SD-3..6 + 2 Capa 4 spec audit + 1 Capa 4 Batch 0 + 2 Capa 4 Batch 1).

**Severidad** (post-actualización 2026-05-06 incluyendo Capa 4 spec audit + Batches 0+1):
- 🔴 CRITICAL: 3 (D-001 hardware, D-008 subagent sandbox, D-014 listener fail-closed)
- 🟠 HIGH: 7 (D-002, D-004, D-006, D-007, D-009, D-031, D-032, D-044)
- 🟡 MEDIUM: 15 (D-003, D-005, D-010, D-011, D-015, D-016, D-017, D-018, D-021, D-029, D-030, D-033, D-043, D-046)
- 🟢 LOW: 15 (D-012, D-013, D-019, D-020, D-022, D-034, D-035, D-036, D-037, D-038, D-039, D-040, D-041, D-045, D-047)

**Drifts ya cerrados**: 35/37 (post sesión wiki 2026-05-06).

- **Cerrados en wiki sesión 2026-05-05** (commits `60795ab..00d25ad` en branch `feat/capa3-pipeline`): D-014, D-016, D-017, D-018.
- **D-013**: confirmado como falso drift (wiki ya correcta) — entrada actualizada.
- **D-031, D-032**: cerrados en código (commits `6dcacc4` + `d034b51`) durante deployment al rig.
- **D-038**: cerrado en código (commit `73822e8`) — `_sha256_wav_pcm` parsea RIFF para hash PCM-only.
- **D-039, D-040, D-041**: comportamiento implementado matches la decisión Franco; entrada documenta + cierra.

**Capa 3 review fixes aplicados** (5 CRITICAL + 9 HIGH + 6 SPEC DRIFTS, 7 commits):

| Group | Items | Commit | Files |
|---|---|---|---|
| G1 | CR-1 + CR-2 | `81be5ea` | orchestrator.py + new test_orchestration_lock.py |
| G2 | CR-3 + CR-4 + H-4 | `8d9ca7a` | normalize.py + main.py + test_main_lifespan_helpers.py |
| G3 | CR-5 + H-6 + H-7 + H-8 + H-9 | `8c50339` | tests/integration/api/test_transcriptions.py (+7 tests) |
| G4 | H-1 + H-2 + H-3 + H-5 | `8c8f122` | normalize.py + stt.py + main.py + api/transcriptions.py |
| G5 | SD-1 + SD-2 | `9ca40ce` | config.py + normalize.py + orchestrator.py + api |
| G6 | SD-3 + drift entries SD-4..6 | `73822e8` | normalize.py + this drift log + tests |
| G7 | Drift sync + final push | (this commit) | this drift log |

**Sesión wiki dedicada 2026-05-06 (pre-spec Capa 4)** — cerradas:

- **D-026** (REST entry): cerrado en `wiki/RF/RF-TRX.md` Nota de versión 2.1 — `POST /api/transcriptions` (Capa 3) marcado como transitional + deprecado en Capa 4 + removal en Capa 5. El contrato canónico sigue siendo MCP-driven.
- **D-027** (per-user cache): cerrado en `wiki/05_modelo_datos.md` §1, `wiki/02_arquitectura.md` §5, `wiki/RF/RF-TRX.md` (todas las refs `cache/<audio_hash>` → `cache/<user_id>/<audio_hash>`), `wiki/RF/RF-CACHE.md` RF-CACHE-02 (walk per-user + TTL via `mtime` de `result.json`), `wiki/FL/FL-TRX-01.md` (sequence diagram).
- **D-028** (lazy pyannote): cerrado en `wiki/02_arquitectura.md` §6 — agregada nota de loaders lazy `_whisperx_load_model` y `_pyannote_from_pretrained` para testability sin extras `[pipeline]`.
- **D-040** (min_speakers semantics): cerrado en `wiki/RF/RF-TRX.md` RF-TRX-01 Special Cases — explícito que `min_speakers`/`max_speakers` son hints no requirements; pyannote es la autoridad.
- **D-042** (HF 3-model accepts): cerrado en `wiki/RF/RF-TRX.md` Prerrequisitos HF — listados los TRES modelos gated (incluido `pyannote/speaker-diarization-community-1`).
- **D-043** (ADR-011 vs RF-MCP unified upload): documentado en este log; sin cambios al ADR (regla de inmutabilidad). RF gana, ADR queda con drift histórico.
- **D-044** (upload_bearer_hash column): cerrado en wiki — `05_modelo_datos.md` §2 + RF-MCP-01 step 3+5+7 + RF-MCP-03 step 4 sincronizados. La migration alembic queda como Capa 4 Batch 0.

**Drifts pendientes (no-wiki o de capas posteriores)**:

| ID | Acción | Tipo | Cuándo |
|----|--------|------|--------|
| D-010 | `/graphify --update` post wiki sync 2026-05-06 + Capa 3 fixes | proceso | después de cerrar este sync; antes del spec Capa 4 |
| D-019 | Encryption key rotation versioning (key-id prefix) | code | post-Capa 6 o auditoría externa |
| D-020 | `mcp_bearers.last_used_at` throttle 5 min | code | cuando Capa 6 muestre carga real |
| D-021 | Test sub-app fixture refactor (sacar `/_test_mcp_*` del prod app) | tests | pre-Capa 6 |
| D-022 | Callback service extraction (`auth/callback_service.py`) | refactor | cuando el handler vuelva a crecer |
| D-033 | Documentar Postgres password rotation en deployment runbook | docs | Capa 7 (deployment runbook) |
| D-044-impl | Alembic migration `add_upload_bearer_hash` + ORM update | code | Capa 4 Batch 0 |
| (RF-CACHE-03 ↔ código) | RF-CACHE-03 todavía menciona `meta.json` corrupto; el código no usa `meta.json` (TTL = mtime). Drift menor: rewrite RF-CACHE-03 para "result.json corrupto / inaccesible / no-hex hash directory". | wiki | sesión wiki dedicada futura, no bloquea Capa 4 |

**Pattern emergente para futuras capas**:
1. Antes de cerrar specs/ADRs, validar las asunciones físicas (hardware, lib semantics, dialect-specific types) con un experimento mínimo.
2. Subagents para escritura, main session para ejecución.
3. Suspensiones de reglas de gobernanza se documentan, no se silencian.
4. Plan code blocks son hipótesis ejecutable, no deuda compilada — esperar deviations en runtime.
5. **Defaults de seguridad fail-closed; el bypass siempre es explícito (context manager, no flag inline).**
6. **Reviews multi-agente sin context-poisoning capturan invariantes de seguridad que el implementador deja fail-open por inercia.**
7. **Drifts de RFs/ADRs descubiertos en review se anotan en este log y se baja una sesión wiki dedicada — no se pisan los specs en caliente.**
8. **Drifts operacionales del primer deployment (D-031, D-032, D-033) no son atrapables por reviews de código** — solo aparecen al ejecutar `pip install .` (sin `[dev]`) dentro de la imagen y arrancar el container. Considerar smoke test post-build como parte del pipeline CI.
9. **Validar el contenido actual de wiki/spec antes de listar acciones de drift** — evita entradas falsas como D-013 que quedan listadas como pendientes pero ya estaban resueltas (lección post D-013).
