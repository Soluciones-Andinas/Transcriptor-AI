# Módulo AUTH — Requerimientos Funcionales (Autenticación con Microsoft Entra ID)

**Source flow**: [`FL-AUTH-01`](../FL/FL-AUTH-01.md), [`FL-MCP-01`](../FL/FL-MCP-01.md)
**Architecture**: [`02_arquitectura.md`](../02_arquitectura.md) §3 (componente B), §8 (Seguridad)
**Data model**: [`05_modelo_datos.md`](../05_modelo_datos.md) §2 (`users`, `oauth_tokens`, `mcp_bearers`)
**ADRs relacionados** (D-067, 2026-05-11): [ADR-009](../ADR/ADR-009.md) Microsoft Entra ID SSO · [ADR-014](../ADR/ADR-014.md) (Reemplazada) → [ADR-015](../ADR/ADR-015.md) listener fail-closed · [ADR-016](../ADR/ADR-016.md) defensa en capas · [ADR-017](../ADR/ADR-017.md) upload bearer efímero. Las dependencies `get_current_user_web` / `get_current_user_mcp` arman `session.info["user_id"]` via `bypass_scoping`; sin eso, queries contra modelos per-user raisean `ScopingNotArmedError`.
**Configuración**: TTL de la cookie session es configurable vía env `SESSION_TTL_SECONDS` (default 86400 = 24h, D-069); `JWT_SECRET` debe tener ≥32 chars (validado en config-load, D-070); `OAUTH_TOKEN_ENC_KEY` debe ser base64 de 32 bytes (AES-256-GCM key, validado en config-load).
**Hardening level**: Execution-Normative

## Tabla resumen

| ID | Título | Actor | Pre-condición | Entradas | Salidas | Criterio de aceptación |
|---|---|---|---|---|---|---|
| RF-AUTH-01 | Iniciar flow OAuth | Anónimo | env vars MS_* configuradas | `GET /auth/login` | 302 redirect a Microsoft + cookie temp con state | Given user anónimo, when GET /auth/login, then redirect a MS con state válido |
| RF-AUTH-02 | Recibir callback OAuth | Anónimo | Cookie temp con state | `code`, `state` | 302 redirect a `/mcp-setup` + cookie sesión | Given callback con code válido, when GET /auth/callback, then user en DB y cookie emitida |
| RF-AUTH-03 | Validar tenant | FastAPI Auth | id_token recibido | id_token claims | OK o `AUTH_TENANT_NOT_ALLOWED` | Given tenant ≠ Sandinas, when callback, then 403 |
| RF-AUTH-04 | Emitir bearer MCP al primer login | FastAPI Auth | User recién creado | user_id | bearer plaintext (una vez) + INSERT mcp_bearers | Given primer login, when callback OK, then bearer activo en DB |
| RF-AUTH-05 | Manejar errores del provider | FastAPI Auth | MS Entra timeout o 5xx | Excepción | 502 + redirect `/login?error=AUTH_PROVIDER_UNAVAILABLE` | Given MS down, when callback, then user redirected con error |
| RF-AUTH-06 | `GET /auth/me` | Usuario | Cookie sesión válida | — | JSON con user + bearer activo (sin plaintext) | Given user logueado, when GET /auth/me, then 200 con datos |
| RF-AUTH-07 | `POST /auth/regenerate-mcp-token` | Usuario | Cookie sesión válida | — | Nuevo bearer plaintext + revoca anterior | Given user logueado, when regenerate, then nuevo bearer y viejo revocado |
| RF-AUTH-08 | Banner UI sobre estado del bearer post-logout (ALT-1) | UI / Usuario | Páginas `/mcp-setup` y `/auth/me` | Estado del bearer activo del user | Mensaje visible explicando que `POST /auth/logout` no revoca el bearer | Given user post-logout, when carga `/mcp-setup` o `/auth/me`, then ve banner que indica que el bearer sigue activo y cómo revocarlo |
| RF-AUTH-09 | `POST /auth/logout` cierra sesión web | Usuario | (ninguna) | — | 302 a `/login` + `Set-Cookie: session=; Max-Age=0` | Given cookie session presente o ausente, when POST /auth/logout, then cookie borrada y redirect a /login (D-064) |

---

## RF-AUTH-01: Iniciar flow OAuth

### Execution Sheet

| Campo | Valor |
|---|---|
| ID | RF-AUTH-01 |
| Título | Generar authorization URL y redirigir a Microsoft Entra |
| Actor primario | Usuario anónimo (browser) |
| Actor secundario | FastAPI Auth |
| Prioridad | Alta |
| Severidad | Crítica |
| Flujo origen | FL-AUTH-01 §6 paso 1-5 |

### Precondiciones detalladas

| # | Condición | Verificación |
|---|---|---|
| 1 | Variables `MS_CLIENT_ID`, `MS_TENANT_ID`, `MS_REDIRECT_URI` configuradas | env vars validadas en startup |
| 2 | El user no tiene cookie `session` válida (si la tiene, redirect directo a `/mcp-setup`) | middleware lee cookie |

### Inputs

Sin inputs explícitos. Es `GET /auth/login`.

### Process Steps (Happy Path)

| # | Paso | Componente |
|---|---|---|
| 1 | Generar `state` aleatorio (32 bytes hex) | FastAPI |
| 2 | Generar PKCE `code_verifier` (43-128 chars) y `code_challenge = SHA256(verifier)` | FastAPI |
| 3 | Set-Cookie `oauth_state` (`HttpOnly`, `Secure`, `SameSite=Lax`, TTL 5 min) con `{state, code_verifier}` firmado | FastAPI |
| 4 | Construir authorization URL: `https://login.microsoftonline.com/<MS_TENANT_ID>/oauth2/v2.0/authorize?client_id=<MS_CLIENT_ID>&response_type=code&redirect_uri=<MS_REDIRECT_URI>&scope=openid%20profile%20email%20User.Read&state=<state>&code_challenge=<challenge>&code_challenge_method=S256` | FastAPI |
| 5 | Emitir log `auth_login_started` con `request_id` | FastAPI |
| 6 | Responder 302 a la authorization URL | FastAPI |

### Outputs

| Campo | Tipo | Destino | Efecto |
|---|---|---|---|
| Redirect 302 a Microsoft | HTTP | Browser | Browser carga consent screen de MS |
| Cookie `oauth_state` | HTTP cookie | Browser | Persiste state + verifier para validar el callback |
| Log `auth_login_started` | log | stdout | Trazabilidad |

### Typed Errors

Sin errores propios; `RF-AUTH-05` cubre fallas de comunicación.

### Special Cases and Variants

- **User ya logueado** (cookie `session` válida): `GET /auth/login` redirect directo a `/mcp-setup` sin iniciar flow.
- **Doble click en login**: cada call genera nuevo state y verifier; el más reciente prevalece (cookie pisa).
- **Multi-tab**: si el user abre `/auth/login` en una segunda tab/ventana mientras la primera todavía está esperando consent en Microsoft, la cookie `oauth_state` de la segunda tab **pisa** la de la primera (mismo nombre, path, domain). Al volver del callback con el `state` query param de la primera tab, el match contra la cookie actual falla → `AUTH_INVALID_STATE` (RF-AUTH-02). Comportamiento esperado y documentado: la última `/auth/login` gana, las anteriores deben re-iniciar el flow. No es un bug; es la consecuencia natural de tener una cookie nombrada per-domain.

### Data Model Impact

- Cookie temp `oauth_state` (transitoria, no DB).

### Expanded Acceptance Criteria (Gherkin)

```gherkin
Scenario: User anónimo inicia login
  Given el user no tiene cookie session
  When GET /auth/login
  Then la respuesta es 302
    And el header Location es URL de Microsoft con client_id, redirect_uri, state, code_challenge
    And se setea cookie oauth_state HttpOnly Secure
    And el log contiene auth_login_started

Scenario: User ya logueado intenta login
  Given el user tiene cookie session válida
  When GET /auth/login
  Then la respuesta es 302
    And el header Location es /mcp-setup
```

### Test Traceability

| Test ID | Tipo |
|---|---|
| TP-AUTH-01-pos-01 | Positivo |
| TP-AUTH-01-pos-02 | Positivo (user ya logueado) |
| TP-AUTH-01-cov-01 | Cobertura (verifica state aleatorio único entre calls) |

### No Ambiguities Left

- **Forbidden assumptions**: no se asume scope `offline_access` (no se gestiona refresh token aún en MVP).
- **Closed decisions**:
  - PKCE S256 obligatorio; cookie temp con TTL 5 min.
  - Cookie `oauth_state` firmada con `itsdangerous.URLSafeTimedSerializer(secret, salt="oauth-state-v1")` (D-072): payload short-lived que nunca sale a un third party; tamper-evidence + expiry son suficientes sin overhead de JWT.
- **Out of scope**: MFA específico (heredado de tenant); login con cuenta externa.

**TODO explicit = 0**.

---

## RF-AUTH-02: Recibir callback OAuth y crear/actualizar user

### Execution Sheet

| Campo | Valor |
|---|---|
| ID | RF-AUTH-02 |
| Título | Validar callback, intercambiar code, persistir user, emitir cookie de sesión |
| Actor primario | FastAPI Auth |
| Prioridad | Alta |
| Severidad | Crítica |
| Flujo origen | FL-AUTH-01 §6 pasos 6-13 |

### Precondiciones detalladas

| # | Condición | Verificación |
|---|---|---|
| 1 | Cookie `oauth_state` presente y firma válida | middleware lee cookie |
| 2 | Query param `state` coincide con `oauth_state.state` | comparación |
| 3 | Postgres reachable | health |
| 4 | Variables `MS_CLIENT_SECRET`, `OAUTH_TOKEN_ENC_KEY`, `JWT_SECRET` configuradas | env vars |

### Inputs

| Campo | Tipo | Requerido | Origen | Validación |
|---|---|---|---|---|
| `code` | string | Sí | query param | non-empty |
| `state` | string | Sí | query param | match cookie |

### Process Steps (Happy Path)

| # | Paso | Componente |
|---|---|---|
| 1 | Validar cookie `oauth_state` (firma + TTL) | FastAPI |
| 2 | Validar `state` query param == `oauth_state.state` | FastAPI |
| 3 | POST `https://login.microsoftonline.com/<MS_TENANT_ID>/oauth2/v2.0/token` con `grant_type=authorization_code`, `code`, `redirect_uri`, `client_id`, `client_secret`, `code_verifier` | FastAPI |
| 4 | Recibir `{access_token, refresh_token, id_token, expires_in}` | FastAPI |
| 5 | Validar `id_token`: firma JWKS de MS, `iss`, `aud == MS_CLIENT_ID`, `exp`, `tid == MS_TENANT_ID` (delegado en RF-AUTH-03) | FastAPI |
| 6 | Extraer `oid`, `email`, `name` del `id_token` | FastAPI |
| 7 | Buscar user en `users` por `microsoft_oid` | Postgres |
| 8a | Si no existe: INSERT `users` + INSERT `oauth_tokens` (encriptados) + INSERT `mcp_bearers` (RF-AUTH-04) | Postgres |
| 8b | Si existe: UPDATE `users.last_login_at` + UPSERT `oauth_tokens` (refresca tokens) | Postgres |
| 9 | Firmar JWT cookie de sesión con payload `{sub, oid, email, iat, exp}` (TTL 24h) | FastAPI |
| 10 | Emitir log `auth_callback_received(success=true)` y `auth_user_created` o `auth_user_login` | FastAPI |
| 11 | Borrar cookie `oauth_state` | FastAPI |
| 12 | Responder 302 a `/mcp-setup` con `Set-Cookie session=<JWT>` (HttpOnly, Secure, SameSite=Strict) | FastAPI |

### Outputs

| Campo | Tipo | Destino | Efecto |
|---|---|---|---|
| 302 redirect a `/mcp-setup` | HTTP | Browser | Onboarding |
| Cookie `session` JWT | Cookie | Browser | Sesión web 24h |
| Registro en `users` (creado o actualizado) | DB | Postgres | Identidad persistente |
| Registro en `oauth_tokens` (refresh + access encriptados) | DB | Postgres | Para refresh futuro |
| Registro en `mcp_bearers` (solo primer login) | DB | Postgres | Bearer activo |
| 4 logs estructurados | log | stdout | Auditoría |

### Typed Errors

| Código | HTTP | Causa | Trigger |
|---|---|---|---|
| `AUTH_INVALID_STATE` | 400 | Cookie `oauth_state` ausente, expirada, o `state` no coincide | Validación pasos 1-2 |
| `AUTH_INVALID_OAUTH_CODE` | 400 | MS retorna error en token exchange | Step 3 |
| `AUTH_TENANT_NOT_ALLOWED` | 403 | Validación tenant en RF-AUTH-03 | Step 5 |
| `AUTH_PROVIDER_UNAVAILABLE` | 502 | MS timeout o 5xx | Step 3 — RF-AUTH-05 |

### Special Cases and Variants

- **Mismo user en distintos browsers**: cada login emite nueva cookie session, mismo bearer MCP (no rota).
- **User reactivado en Entra**: `last_login_at` se actualiza; el user retoma su histórico.
- **Race en doble callback** (raro): cookie `oauth_state` pisada; el segundo callback puede fallar con `AUTH_INVALID_STATE`. Aceptable.

### Data Model Impact

- INSERTs/UPDATEs en `users`, `oauth_tokens`, `mcp_bearers`.
- Estado del flow: `AwaitingCallback → Authenticated`.

### Expanded Acceptance Criteria (Gherkin)

```gherkin
Scenario: Primer login OK
  Given el user nunca se logueó antes
    And tiene cookie oauth_state válida
    And state query param coincide
  When GET /auth/callback?code=valid&state=match
  Then la respuesta es 302 a /mcp-setup
    And existe row en users con su microsoft_oid
    And existe row en oauth_tokens con tokens encriptados
    And existe row en mcp_bearers con revoked_at IS NULL
    And la cookie session es JWT firmado con sub=user.id
    And el log contiene auth_user_created

Scenario: Login subsiguiente
  Given el user ya tiene row en users
  When callback OK
  Then users.last_login_at es ahora
    And NO se crea nuevo registro en mcp_bearers
    And el log contiene auth_user_login (no auth_user_created)

Scenario: State no coincide
  Given cookie oauth_state.state = "abc"
  When callback con state="xyz"
  Then la respuesta es 400 con error_code=AUTH_INVALID_STATE
    And NO se crea ningún registro
```

### Test Traceability

| Test ID | Tipo |
|---|---|
| TP-AUTH-02-pos-01 | Positivo (primer login) |
| TP-AUTH-02-pos-02 | Positivo (login subsiguiente) |
| TP-AUTH-02-neg-01 | Negativo (state no coincide) |
| TP-AUTH-02-neg-02 | Negativo (cookie oauth_state expirada) |
| TP-AUTH-02-neg-03 | Negativo (code inválido en MS exchange) |

### No Ambiguities Left

- **Forbidden assumptions**: no se asume rotación automática de refresh token; se hace en background o al fallar access token.
- **Closed decisions**:
  - Tokens encriptados con AES-256-GCM usando `OAUTH_TOKEN_ENC_KEY`.
  - JWT firmado HS256 con `JWT_SECRET` (mínimo 32 chars, validado en config-load — D-070). Generar con `python -c 'import secrets; print(secrets.token_urlsafe(48))'`.
  - **Email fallback** (D-066, fix H-9): el handler extrae `email = claims.get("email") or claims.get("preferred_username")`. Caso MS Entra B2B / guest donde `email` viene vacío y `preferred_username` carga el UPN. Si ambos faltan → `AUTH_PROVIDER_UNAVAILABLE`.
  - **JWKS retry on signature failure** (D-071, fix CR-1): ante `IdTokenInvalid` (firma fallida), el handler fuerza `fetch_jwks(force_refresh=True)` y reintenta una vez. Cubre el caso de rotación de la signing key de MS Entra mientras el JWKS está cacheado. Si el retry también falla → `AUTH_PROVIDER_UNAVAILABLE`. Cache TTL 24h; `asyncio.Lock` serializa refreshes concurrentes (H-5).
- **Out of scope**: invalidar cookie session si el user es removido del tenant (depende de policy del tenant).

**TODO explicit = 0**.

---

## RF-AUTH-03: Validar tenant del id_token

### Execution Sheet

| Campo | Valor |
|---|---|
| ID | RF-AUTH-03 |
| Título | Rechazar id_tokens emitidos por tenants ajenos a Sandinas |
| Actor primario | FastAPI Auth |
| Prioridad | Alta |
| Severidad | Crítica (regresión de Privacy si falla) |
| Flujo origen | FL-AUTH-01 §6 paso 5 |

### Process Steps

| # | Paso |
|---|---|
| 1 | Decodificar id_token JWT |
| 2 | Validar firma con JWKS de Microsoft (descarga y cacheo) |
| 3 | Validar claims: `iss == https://login.microsoftonline.com/<MS_TENANT_ID>/v2.0`, `aud == MS_CLIENT_ID`, `exp > now`, `nbf <= now` |
| 4 | Validar `tid == MS_TENANT_ID` |
| 5 | Si alguno falla: log ERROR + responder con `AUTH_TENANT_NOT_ALLOWED` |

### Typed Errors

| Código | HTTP | Causa |
|---|---|---|
| `AUTH_TENANT_NOT_ALLOWED` | 403 | `tid` distinto al esperado o claims inválidos |

### Gherkin

```gherkin
Scenario Outline: Rechazo de tenants externos
  Given id_token con tid=<received_tid>
    And MS_TENANT_ID configurado = "abc-tenant-id"
  When validación de tenant
  Then resultado es <result>

  Examples:
    | received_tid    | result                              |
    | abc-tenant-id   | OK (continúa flow)                  |
    | xyz-other       | rechazo con AUTH_TENANT_NOT_ALLOWED |
    | (missing)       | rechazo                             |
```

### Test Traceability

| Test ID | Tipo |
|---|---|
| TP-AUTH-03-pos-01 | Positivo (tid match) |
| TP-AUTH-03-neg-01 | Negativo (tid distinto) |
| TP-AUTH-03-neg-02 | Negativo (claim missing) |
| TP-AUTH-03-neg-03 | Negativo (firma inválida) |

### No Ambiguities Left

- **Closed decisions**: solo single-tenant Sandinas. Multi-tenant no soportado.
- **Out of scope**: federation con otros IdPs.

**TODO explicit = 0**.

---

## RF-AUTH-04: Emitir bearer MCP al primer login

### Execution Sheet

| ID | RF-AUTH-04 |
|---|---|
| Actor | FastAPI Auth |
| Trigger | Primer login (RF-AUTH-02 paso 8a) |

### Process Steps

| # | Paso |
|---|---|
| 1 | Generar bearer plaintext: 64 chars random URL-safe (e.g. `secrets.token_urlsafe(48)`) |
| 2 | Hashear: `token_hash = SHA256(bearer_plaintext)` hex |
| 3 | INSERT mcp_bearers (id, user_id, token_hash, name='initial', created_at) |
| 4 | Mantener `bearer_plaintext` en memoria del request para devolverlo en la response del callback (vía cookie temporal o param flash) |
| 5 | Emitir log `mcp_bearer_generated(user_id, bearer_id)` |

### Special Cases

- El bearer plaintext se muestra una sola vez en `/mcp-setup`. Si el user no lo copia, debe regenerar (RF-AUTH-07).
- Pasar el bearer al frontend tras callback: cookie temp `mcp_bearer_flash` (TTL 60s, SameSite=Strict, HttpOnly) que la UI lee y borra.

### Outputs

- Row en `mcp_bearers` con `revoked_at=NULL`.
- Bearer plaintext disponible para la UI una vez.

### Test Traceability

| Test ID | Tipo |
|---|---|
| TP-AUTH-04-pos-01 | Positivo (primer login emite bearer) |
| TP-AUTH-04-pos-02 | Positivo (login posterior NO emite nuevo) |

**TODO explicit = 0**.

---

## RF-AUTH-05: Manejar errores del provider

### Execution Sheet

| ID | RF-AUTH-05 |
|---|---|
| Actor | FastAPI Auth |

### Process Steps

| # | Paso |
|---|---|
| 1 | Wrap calls a MS Entra (`/token`, JWKS) en try/except con timeout 10 s |
| 2 | Si `requests.Timeout` o status 5xx: log ERROR `auth_provider_unavailable` |
| 3 | Responder 302 a `/login?error=AUTH_PROVIDER_UNAVAILABLE` |
| 4 | Borrar cookie `oauth_state` |

### Test Traceability

| Test ID | Tipo |
|---|---|
| TP-AUTH-05-neg-01 | Mock: MS retorna 503 |
| TP-AUTH-05-neg-02 | Mock: timeout en token exchange |

**TODO explicit = 0**.

---

## RF-AUTH-06: GET /auth/me

### Execution Sheet

| ID | RF-AUTH-06 |
|---|---|
| Actor | Usuario logueado |

### Inputs

| Campo | Origen |
|---|---|
| Cookie `session` | HTTP |

### Process Steps

| # | Paso |
|---|---|
| 1 | Validar cookie session (firma JWT, exp) |
| 2 | Si inválida: 401 + `AUTH_NOT_AUTHENTICATED` |
| 3 | SELECT user from `users` WHERE id = JWT.sub |
| 4 | SELECT bearer activo from `mcp_bearers` WHERE user_id=... AND revoked_at IS NULL |
| 5 | Si no hay bearer activo (caso raro post-revoke manual): NO se auto-crea uno. `bearer_payload = None` se devuelve en la response. El UI (RF-AUTH-08) muestra un banner indicando que el user debe hacer `POST /auth/regenerate-mcp-token` para emitir uno nuevo. **Drift D-060 (2026-05-11)**: la spec previa decía "crear uno automáticamente" pero el código no lo hace; mantener la regeneración explícita es coherente con Privacy > Simplicity (no emitir plaintext en endpoints idempotentes de lectura). |
| 6 | Si hay flash cookie `mcp_bearer_flash`: incluir el plaintext en response Y borrar la cookie |
| 7 | Responder JSON: `{user: {id, email, display_name}, bearer: {id, name, plaintext_or_null, created_at}, mcp_url: <BASE_URL>/mcp}`. El campo `bearer.name` (string, valores `"initial"` o `"regenerated"`) lo expone el código desde `mcp_bearers.name` y la UI lo usa para distinguir entre bearer original y regenerado (drift D-061). |

### Typed Errors

| Código | HTTP | Causa |
|---|---|---|
| `AUTH_NOT_AUTHENTICATED` | 401 | Cookie ausente, inválida o expirada |

### Gherkin

```gherkin
Scenario: User logueado consulta /auth/me con bearer recién creado
  Given user logueó hace 30 segundos (flash cookie aún vigente)
  When GET /auth/me
  Then 200 con user, bearer.plaintext != null, mcp_url

Scenario: User logueado consulta /auth/me sin bearer flash
  Given user logueó hace 2 minutos (flash expiró)
  When GET /auth/me
  Then 200 con bearer.plaintext == null, bearer.id != null

Scenario: User no logueado
  Given sin cookie session
  When GET /auth/me
  Then 401 + AUTH_NOT_AUTHENTICATED
```

### Test Traceability

| Test ID | Tipo |
|---|---|
| TP-AUTH-06-pos-01 | Positivo (con flash) |
| TP-AUTH-06-pos-02 | Positivo (sin flash) |
| TP-AUTH-06-neg-01 | Negativo (sin cookie) |
| TP-AUTH-06-neg-02 | Negativo (cookie expirada) |

**TODO explicit = 0**.

---

## RF-AUTH-07: POST /auth/regenerate-mcp-token

### Execution Sheet

| ID | RF-AUTH-07 |
|---|---|
| Actor | Usuario logueado |

### Process Steps

| # | Paso |
|---|---|
| 1 | Validar cookie session |
| 2 | Inicio transacción Postgres |
| 3 | UPDATE mcp_bearers SET revoked_at = now() WHERE user_id = JWT.sub AND revoked_at IS NULL |
| 4 | Generar nuevo bearer plaintext (64 chars URL-safe) |
| 5 | INSERT mcp_bearers (user_id, token_hash, name='regenerated', created_at) |
| 6 | Commit |
| 7 | Emitir logs `mcp_bearer_revoked(old_bearer_id)` y `mcp_bearer_generated(new_bearer_id)` |
| 8 | Responder JSON `{bearer: {id, name, plaintext, created_at}}`. El campo `bearer.name` (string, `"regenerated"` para los bearers creados aquí; `"initial"` para los de RF-AUTH-04) lo expone el código desde `mcp_bearers.name` (drift D-061). |

### Special Cases

- **Race en doble regenerate**: lock pesimista en `mcp_bearers WHERE user_id=... FOR UPDATE` antes del UPDATE/INSERT, o constraint UNIQUE WHERE `revoked_at IS NULL` que falla en INSERT concurrente.

### Typed Errors

| Código | HTTP | Causa |
|---|---|---|
| `AUTH_NOT_AUTHENTICATED` | 401 | Cookie inválida |
| `INTERNAL_ERROR` | 500 | DB falla |

### Gherkin

```gherkin
Scenario: User regenera bearer
  Given user con bearer activo bearer_old_id
  When POST /auth/regenerate-mcp-token
  Then 200 con nuevo bearer plaintext
    And mcp_bearers.revoked_at IS NOT NULL para bearer_old_id
    And nuevo row en mcp_bearers con revoked_at IS NULL
    And el bearer viejo deja de autenticar requests MCP (validar con request siguiente)

Scenario: User no logueado intenta regenerar
  Given sin cookie session
  When POST /auth/regenerate-mcp-token
  Then 401
```

### Test Traceability

| Test ID | Tipo |
|---|---|
| TP-AUTH-07-pos-01 | Positivo (revocación + emisión) |
| TP-AUTH-07-pos-02 | Positivo (bearer viejo es rejected en MCP tras regenerate) |
| TP-AUTH-07-neg-01 | Negativo (sin auth) |

**TODO explicit = 0**.

---

## RF-AUTH-08: Banner UI sobre estado del bearer post-logout

### Execution Sheet

| Campo | Valor |
|---|---|
| ID | RF-AUTH-08 |
| Título | Comunicar al user que el logout web NO revoca el bearer MCP |
| Actor primario | UI (frontend) |
| Actor secundario | Usuario logueado o recién deslogueado |
| Prioridad | Media |
| Severidad | Mayor (regresión de UX si no se comunica; el user puede creer "estoy fuera" mientras el bearer en Claude Desktop sigue activo) |
| Flujo origen | ALT-1 del SPEC-capa2 (logout NO revoca bearer) — formalizado a requisito UI |

### Contexto

El comportamiento implementado por `POST /auth/logout` (RF-AUTH-02 + ALT-1):

- Borra la cookie `session` web.
- **NO** revoca el bearer MCP del user.

Razón documentada: el user puede estar usando el bearer desde Claude Desktop (sin browser involucrado en esa sesión); revocar al logout web sería una sorpresa destructiva. Para revocar explícitamente, el user usa `POST /auth/regenerate-mcp-token` (RF-AUTH-07).

Este comportamiento es invisible al usuario sin ayuda de la UI: si el user piensa "logout = sesión cerrada por completo", el bearer queda en uso silencioso. RF-AUTH-08 pide que la UI haga ese estado explícito.

### Precondiciones detalladas

| # | Condición | Verificación |
|---|---|---|
| 1 | Páginas `/mcp-setup` y `/auth/me` (frontend) están renderizadas | UI build |
| 2 | El backend expone el estado del bearer activo via `GET /auth/me` (RF-AUTH-06) | Already shipped |

### Inputs

Sin inputs explícitos. La UI consume `GET /auth/me` para conocer el estado del bearer y renderiza el banner.

### Process Steps (Happy Path)

| # | Paso | Componente |
|---|---|---|
| 1 | Al cargar `/mcp-setup` o `/auth/me`, la UI llama `GET /auth/me` | Frontend |
| 2 | Si `bearer.id != null` (existe un bearer activo del user), renderizar el banner | Frontend |
| 3 | Banner texto sugerido (español): "Tu MCP bearer sigue activo después del logout web. Para revocarlo, usá `POST /auth/regenerate-mcp-token` o el botón 'Revocar bearer'." | Frontend |
| 4 | Banner action: link / botón visible que dispara `POST /auth/regenerate-mcp-token` (RF-AUTH-07) y muestra el nuevo plaintext | Frontend |

### Outputs

| Campo | Tipo | Destino | Efecto |
|---|---|---|---|
| Banner UI con texto + botón | Render React | DOM | El user entiende que el bearer es independiente de la cookie web |
| Botón "Revocar bearer" funcional | UI binding | Frontend → Backend | Triggers RF-AUTH-07 |

### Typed Errors

Sin errores propios. Los errores del backend (`AUTH_NOT_AUTHENTICATED`, `INTERNAL_ERROR`) son responsabilidad de los RFs invocados (`RF-AUTH-06`, `RF-AUTH-07`).

### Special Cases and Variants

- **User sin bearer activo** (caso raro post-logout antes de regenerate): banner cambia a "No tenés un bearer MCP activo. Iniciá sesión nuevamente para que se emita uno." — no expone el botón de revocar.
- **User con flash cookie `mcp_bearer_flash` vigente**: la UI ya está mostrando el plaintext nuevo; el banner no se muestra (no hay nada que comunicar — el user acaba de ver el bearer).

### Data Model Impact

Ninguno. RF-AUTH-08 es un requisito puramente UI; consume API existente.

### Expanded Acceptance Criteria (Gherkin)

```gherkin
Scenario: User accede a /mcp-setup post-logout
  Given el user no tiene cookie session
    And tiene un bearer activo en mcp_bearers
  When la UI carga /mcp-setup
  Then se llama GET /auth/me retorna 401
    And la UI redirect a /login

Scenario: User logueado accede a /auth/me y ve el banner
  Given el user tiene cookie session válida
    And tiene un bearer activo (RF-AUTH-04)
    And no tiene flash cookie de bearer recién emitido
  When la UI carga /auth/me
  Then la respuesta incluye {bearer: {id: ..., plaintext: null}}
    And la UI renderiza el banner explicativo
    And el banner incluye un botón visible "Revocar bearer"

Scenario: User clica "Revocar bearer"
  Given el banner está visible
  When el user clica el botón
  Then la UI llama POST /auth/regenerate-mcp-token (RF-AUTH-07)
    And muestra el nuevo plaintext una sola vez
    And refresca el banner para reflejar el nuevo bearer.id
```

### Test Traceability

| Test ID | Tipo |
|---|---|
| TP-AUTH-08-pos-01 | Positivo (banner visible cuando bearer.id != null y plaintext == null) |
| TP-AUTH-08-pos-02 | Positivo (banner oculto cuando flash cookie está vigente) |
| TP-AUTH-08-neg-01 | Negativo (banner no se muestra cuando no hay bearer activo) |
| TP-AUTH-08-int-01 | Integración (botón "Revocar bearer" dispara RF-AUTH-07) |

### No Ambiguities Left

- **Forbidden assumptions**: el banner no debe pretender que el logout web revoca el bearer; la implementación actual del backend (ALT-1) es deliberada.
- **Closed decisions**: texto en español rioplatense, botón explícito (no link sutil), siempre visible si `bearer.id != null` y `plaintext == null`.
- **Out of scope**: notificaciones email del estado del bearer (no necesario para v0.1).

> **D-063 (2026-05-11)**: la sección Test Traceability de RF-AUTH-08 referencia IDs `TP-AUTH-08-pos-01/02`, `TP-AUTH-08-neg-01`, `TP-AUTH-08-int-01` que pertenecen a la spec UI futura. Como RF-AUTH-08 es Pendiente Capa 5 (depende de RF-UI-01/02 también pendientes), esos test IDs se materializarán cuando se implemente el frontend React. La doc TP-AUTH actual cubre RF-AUTH-01 a RF-AUTH-07 (los flows con backend ya implementado).

**TODO explicit = 0**.

---

## RF-AUTH-09: POST /auth/logout

### Execution Sheet

| Campo | Valor |
|---|---|
| ID | RF-AUTH-09 |
| Título | Cerrar sesión web borrando la cookie `session` |
| Actor primario | Usuario |
| Prioridad | Media |
| Severidad | Menor |
| Flujo origen | Implícito post-RF-AUTH-02 (limpieza de sesión); referenciado por RF-AUTH-08 ALT-1 |

> **D-064 (2026-05-11)**: el endpoint estaba implementado desde Capa 2 pero sin RF dedicado. Esta sección formaliza el contrato as-built en `src/transcription_api/auth/routes.py::logout` para cerrar la trazabilidad. ALT-1 (no revoca el bearer MCP) está documentado en RF-AUTH-08.

### Precondiciones

| # | Condición | Verificación |
|---|---|---|
| 1 | El endpoint es accesible sin auth | Default: anyone puede llamarlo (defensa contra cookies corruptas) |

### Inputs

Sin body. La cookie `session` puede estar presente, ausente o ser inválida — el endpoint la borra incondicionalmente.

### Process Steps

| # | Paso |
|---|---|
| 1 | Construir `RedirectResponse(url="/login", status_code=302)` |
| 2 | `response.delete_cookie("session", path="/", secure=True, httponly=True, samesite="strict")` |
| 3 | Emitir log `auth_logout` (sin campos, ver D-065 en `wiki/05_modelo_datos.md §7`) |
| 4 | Retornar la response |

### Outputs

- HTTP 302 + `Set-Cookie: session=; Max-Age=0` + redirect a `/login`.

### Typed Errors

Ninguno. El endpoint es idempotente y best-effort.

### Special Cases and Variants

- **ALT-1 — Logout no revoca bearer MCP**: documentado y justificado en RF-AUTH-08. El user puede estar usando el bearer desde Claude Desktop (sin browser involucrado); revocar al logout web sería una sorpresa destructiva. Para revocar explícitamente: `POST /auth/regenerate-mcp-token`.
- **Cookie ausente**: el endpoint igual emite el `Set-Cookie: ... Max-Age=0` (idempotente).
- **Cookie corrupta**: igual — el endpoint no decodifica la cookie, solo la marca como expirada en el browser.

### Test Traceability

| Test ID | Tipo | Cubre |
|---|---|---|
| TP-AUTH-09-pos-01 | Positivo | User logueado → POST /auth/logout → 302 a /login + cookie cleared (existing test as `test_logout_clears_session_cookie`) |
| TP-AUTH-09-pos-02 | Positivo | El logout NO revoca el bearer MCP (existing test as `test_logout_does_not_revoke_mcp_bearer`) |

### No Ambiguities Left

- **Forbidden assumptions**: el endpoint no requiere auth; el operador no puede asumir que el caller ya esté autenticado.
- **Closed decisions**: redirect target es `/login` (siempre), no configurable. La cookie se borra con los mismos atributos con los que se setea (SameSite=Strict, Secure, HttpOnly).
- **Out of scope**: revocar bearer en el logout (ALT-1 explícito); single sign-out con MS Entra (logout del IdP no se propaga al backend).

**TODO explicit = 0**.
