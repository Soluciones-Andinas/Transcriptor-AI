# Spec — Capa 2: Microsoft Entra OAuth + Auth endpoints + MCP bearer middleware

**SPEC ID:** SPEC-capa2-auth-msentra-v1
**Format:** `sandinas-dev-workflows:writing-plans` canonical (Input / Output / Main Flow / Acceptance Criteria / Secondary Flows)
**Branch:** `feat/capa2-auth-msentra` (a crear desde `master`, base `57bfe81`)
**Backend repo:** `transcription-api/` (Capa 1 mergeada en `master` + smoke verde en rig 2026-05-05)

> **Note on TDD adaptation:** Capa 2 implementa endpoints HTTP con dependencias externas (Microsoft Entra). Tests unitarios usan `respx` o `httpx.MockTransport` para simular `/token` y `JWKS` de MS. Tests de integración usan testcontainers Postgres + mocks HTTP de MS. Sin GPU. Sin red real a MS Entra (los tests no llaman al tenant Sandinas).

---

## Input

- `wiki_rf_auth_path`: `wiki/RF/RF-AUTH.md` — 7 RFs Execution-Normative que definen los contratos exactos.
- `wiki_fl_auth_path`: `wiki/FL/FL-AUTH-01.md` — flow OAuth Authorization Code con PKCE.
- `wiki_adr_009_path`: `wiki/ADR/ADR-009.md` — decisión Microsoft Entra ID + scopes + librerías sugeridas.
- `wiki_adr_014_path`: `wiki/ADR/ADR-014.md` — per-user scoping enforcement; Capa 2 lo activa via middleware bearer.
- `wiki_data_model_path`: `wiki/05_modelo_datos.md` §2 — tablas `users`, `oauth_tokens`, `mcp_bearers` ya existen desde Capa 1.
- `decided_libraries`:
  - **`authlib>=1.3`** — OAuth 2.0 client + JWT/JWKS verification en una sola lib. Liviano, agnóstico de provider.
  - **`cryptography>=42`** — AES-256-GCM para encriptar `oauth_tokens.ms_*_encrypted` (decisión cerrada en RF-AUTH-02 §"Closed decisions").
  - **`itsdangerous>=2.2`** — firmado de cookie temp `oauth_state` (más simple que JWT para state corto).
  - `respx` (test-only) — mock HTTP para simular MS Entra sin red real.
- `existing_state` (Capa 1):
  - 6 modelos ORM listos: `User`, `OAuthToken`, `McpBearer` con `oauth_tokens.ms_*_encrypted: BYTEA` y `mcp_bearers.token_hash: TEXT UNIQUE` + partial UNIQUE on active.
  - `enable_per_user_scoping()` listener instalado en lifespan; espera `session.info["user_id"]` para activarse.
  - `.env.example` ya tiene `MS_TENANT_ID`, `MS_CLIENT_ID`, `MS_CLIENT_SECRET`, `MS_REDIRECT_URI`, `OAUTH_TOKEN_ENC_KEY`, `JWT_SECRET`, `SESSION_TTL_SECONDS`.
- `target_files`:
  - Nuevo módulo `src/transcription_api/auth/` (crypto, oauth_client, jwt_session, mcp_bearer, dependencies, routes).
  - Modificaciones a `src/transcription_api/main.py` (incluir el router auth + middleware bearer + dependency get_current_user).
  - `pyproject.toml`: agregar `authlib`, `cryptography`, `itsdangerous`, `respx[dev]`.
  - Tests en `tests/integration/auth/` y `tests/unit/auth/` (~25 tests target).

## Output

**Módulo nuevo `src/transcription_api/auth/`:**

| Archivo | Contenido |
|---|---|
| `auth/__init__.py` | re-exports públicos: `router`, `get_current_user`, `mcp_bearer_required` |
| `auth/crypto.py` | `encrypt_token(plaintext: str) -> bytes` + `decrypt_token(ciphertext: bytes) -> str` con AES-256-GCM derivado de `OAUTH_TOKEN_ENC_KEY` |
| `auth/oauth_client.py` | wrapper sobre `authlib`: build_authorize_url, exchange_code, refresh_access_token, validate_id_token (JWKS de MS) |
| `auth/state_cookie.py` | firma/valida cookie temp `oauth_state` con `itsdangerous` (state + PKCE verifier, TTL 5 min) |
| `auth/session.py` | crea/valida JWT cookie de sesión (HS256 con `JWT_SECRET`, TTL `SESSION_TTL_SECONDS`); helpers para Set-Cookie + revocar |
| `auth/mcp_bearer.py` | `generate_bearer() -> (plaintext, sha256_hash)`, `verify_bearer(session, plaintext) -> User | None` con lookup por `token_hash` y `revoked_at IS NULL` |
| `auth/flash.py` | cookie `mcp_bearer_flash` (HttpOnly, Secure, TTL 60s, SameSite=Strict) para pasar el plaintext del bearer del callback al `/auth/me` |
| `auth/dependencies.py` | `get_current_user_web` (cookie session) + `get_current_user_mcp` (Authorization Bearer) FastAPI dependencies; ambas setean `session.info["user_id"]` activando ADR-014 |
| `auth/routes.py` | `APIRouter` con: `GET /auth/login`, `GET /auth/callback`, `GET /auth/me`, `POST /auth/regenerate-mcp-token`, `POST /auth/logout` (bonus: limpia cookie session) |
| `auth/errors.py` | `AuthError` exception class + error_codes constants (AUTH_INVALID_STATE, AUTH_INVALID_OAUTH_CODE, AUTH_TENANT_NOT_ALLOWED, AUTH_PROVIDER_UNAVAILABLE, AUTH_NOT_AUTHENTICATED) + handler que mapea a HTTP responses |

**Modificaciones a archivos existentes:**

| Archivo | Cambio |
|---|---|
| `src/transcription_api/main.py` | `app.include_router(auth.router)`. Registrar exception_handler para `AuthError`. |
| `pyproject.toml` | agregar `authlib`, `cryptography`, `itsdangerous` a core deps; `respx` a `[dev]`. |
| `.env.example` | sección AUTH ya existe; agregar comentario sobre formato esperado de `OAUTH_TOKEN_ENC_KEY` (32 bytes urlsafe-b64) y `JWT_SECRET` (≥32 chars). |

**Tests nuevos:**

| Archivo | Cubre |
|---|---|
| `tests/unit/auth/test_crypto.py` | encrypt/decrypt round-trip, ciphertexts diferentes para mismo plaintext (nonce random), tamper detection |
| `tests/unit/auth/test_state_cookie.py` | sign/verify, expira tras TTL, firma inválida rechazada |
| `tests/unit/auth/test_session_jwt.py` | sign/verify cookie session, exp respetada, signature mismatch rechazado |
| `tests/unit/auth/test_mcp_bearer.py` | generate produce hash determinístico de plaintext; verify retorna User para token activo, None para revoked/missing |
| `tests/integration/auth/test_login_flow.py` | RF-AUTH-01: redirect 302 a MS, cookie state seteada, state aleatorio único entre calls; user logueado salta a /mcp-setup |
| `tests/integration/auth/test_callback_flow.py` | RF-AUTH-02 + RF-AUTH-03 + RF-AUTH-04 + RF-AUTH-05 con `respx` mockeando MS: primer login (INSERT user + tokens + bearer), login subsiguiente (UPDATE last_login), state mismatch (400), tenant mismatch (403), MS down (502) |
| `tests/integration/auth/test_me_endpoint.py` | RF-AUTH-06: con flash, sin flash, 401 sin cookie |
| `tests/integration/auth/test_regenerate_endpoint.py` | RF-AUTH-07: revoca viejo + emite nuevo, race protection con partial UNIQUE |
| `tests/integration/auth/test_mcp_bearer_middleware.py` | dependency `get_current_user_mcp` valida Bearer, rechaza revoked/missing/malformed; setea `session.info["user_id"]` activando scoping (cross-user isolation verified) |
| `tests/integration/auth/test_oauth_token_storage.py` | tokens MS encriptados con AES-256-GCM antes de INSERT; decrypt simétrico; ciphertext distinto cada vez (nonce) |

## Main Flow

1. **Branch + deps**:
   1. Crear `feat/capa2-auth-msentra` desde `master`.
   2. `pip install authlib cryptography itsdangerous && pip install respx --dev`.
   3. Update `pyproject.toml`.
2. **Crypto helpers** (`auth/crypto.py`):
   1. AES-256-GCM con key de 32 bytes derivada de `OAUTH_TOKEN_ENC_KEY` (urlsafe_b64decode).
   2. Nonce aleatorio 12 bytes per encrypt; concatenar `nonce || ciphertext || tag` en BYTEA.
   3. Decrypt parsea nonce (primeros 12 bytes), descifra el resto, retorna plaintext.
3. **State cookie helper** (`auth/state_cookie.py`):
   1. `itsdangerous.URLSafeTimedSerializer(JWT_SECRET, salt="oauth-state")` para firma + TTL 300s.
   2. Payload: `{state: str, code_verifier: str}`.
4. **Session JWT** (`auth/session.py`):
   1. `authlib.jose.jwt` HS256 con `JWT_SECRET`.
   2. Payload: `{sub: user_id, oid: ms_oid, email, iat, exp}`. TTL `SESSION_TTL_SECONDS`.
   3. Helper `set_session_cookie(response, user)` + `parse_session_cookie(request) -> User | None`.
5. **OAuth client** (`auth/oauth_client.py`):
   1. `build_authorize_url(state, code_challenge) -> str` (sigue exactamente el formato del RF-AUTH-01 paso 4).
   2. `async exchange_code(code, code_verifier) -> dict` con `httpx.AsyncClient` POST `/token`. Timeout 10s.
   3. `async fetch_jwks() -> dict` con cache de 24h (módulo-level dict + timestamp).
   4. `validate_id_token(id_token, jwks) -> claims` con `authlib.jose.JsonWebToken.decode` validando `iss`, `aud`, `exp`, `nbf`, `tid`.
   5. `async refresh_access_token(refresh_token) -> dict` (preparado para Capa 6 cuando se use).
6. **MCP bearer** (`auth/mcp_bearer.py`):
   1. `generate_bearer() -> tuple[str, str]`: `secrets.token_urlsafe(48)` (64 chars) + `hashlib.sha256(...).hexdigest()`.
   2. `async def verify_bearer(session, plaintext)`: SELECT user JOIN mcp_bearers ON token_hash + WHERE revoked_at IS NULL. Update `last_used_at = func.clock_timestamp()`.
7. **Flash cookie** (`auth/flash.py`):
   1. `set_bearer_flash(response, plaintext)`: cookie `mcp_bearer_flash`, TTL 60s, HttpOnly, Secure, SameSite=Strict.
   2. `pop_bearer_flash(request, response)`: lee cookie + setea Set-Cookie con `Max-Age=0` para borrarla.
8. **FastAPI dependencies** (`auth/dependencies.py`):
   1. `get_current_user_web(request, db) -> User`: parse cookie session, lookup user by id, raise `AUTH_NOT_AUTHENTICATED` si falla. Setea `db.info["user_id"] = user.id`.
   2. `get_current_user_mcp(request, db) -> User`: lee `Authorization: Bearer <plaintext>`, llama `verify_bearer`, raise `AUTH_NOT_AUTHENTICATED` si falla. Setea `db.info["user_id"] = user.id`.
9. **Routes** (`auth/routes.py`):
   1. `GET /auth/login`: si cookie session válida → 302 `/mcp-setup`; sino → genera state + PKCE, set cookie state, 302 a MS.
   2. `GET /auth/callback`: validar state → exchange code → validate id_token → upsert user → upsert oauth_tokens (encrypted) → first-login? insert mcp_bearer + flash cookie → set session cookie → 302 a `/mcp-setup`.
   3. `GET /auth/me`: dependency `get_current_user_web` → SELECT bearer activo → si flash cookie present, incluir plaintext + borrar flash → JSON `{user, bearer, mcp_url}`.
   4. `POST /auth/regenerate-mcp-token`: dependency web → tx: revocar viejo + insert nuevo → JSON `{bearer: {plaintext, ...}}`. Manejo de race con partial UNIQUE constraint (catch IntegrityError → retry una vez).
   5. `POST /auth/logout`: borrar cookie session (Set-Cookie con Max-Age=0) → 302 `/login`.
10. **Errors** (`auth/errors.py`):
    1. `class AuthError(Exception)` con `error_code`, `http_status`, `redirect_to: str | None`.
    2. `app.exception_handler(AuthError)`: si `redirect_to`, retorna 302 con `Location` y query `?error=<code>`; sino JSON `{error_code, message}`.
11. **Wire en main.py**:
    1. `from .auth import router as auth_router; app.include_router(auth_router)`.
    2. `app.exception_handler(AuthError)`.
    3. Lifespan ya tiene `enable_per_user_scoping()` desde Capa 1.
12. **Tests + smoke**:
    1. Unit tests del crypto/state/JWT/bearer (sin DB ni HTTP).
    2. Integration tests con `respx` mockeando MS Entra (no se hace red real al tenant).
    3. Cross-user isolation test: user A llama tool MCP con su bearer → ve solo sus rows; user B llama con su bearer → ve solo las suyas. Activa ADR-014.

## Acceptance Criteria

- [ ] **AC-1 — Auth module importable**: `from transcription_api.auth import router, get_current_user_web, get_current_user_mcp` exits 0.
- [ ] **AC-2 — Crypto round-trip**: `encrypt_token("hello") -> bytes` y `decrypt_token(bytes) == "hello"`. Dos encrypts del mismo plaintext producen ciphertexts distintos (nonce random).
- [ ] **AC-3 — Crypto tamper detection**: modificar 1 byte del ciphertext y `decrypt_token` raise `cryptography.exceptions.InvalidTag`.
- [ ] **AC-4 — State cookie sign/verify**: `sign_state({state, verifier})` y `verify_state(token)` round-trip; cookie expirada (>5 min) raise.
- [ ] **AC-5 — Session JWT**: `create_session_token(user)` + `decode_session_token(token)` round-trip con `sub`/`oid`/`email`; firma inválida raise; expiración respetada.
- [ ] **AC-6 — `GET /auth/login` (RF-AUTH-01)**: response 302 con `Location` que matchea `^https://login.microsoftonline.com/.+/oauth2/v2.0/authorize\?` y query con `client_id`, `redirect_uri`, `state`, `code_challenge`, `code_challenge_method=S256`, `scope=openid profile email User.Read`. Cookie `oauth_state` seteada `HttpOnly Secure`. Llamadas consecutivas producen states distintos.
- [ ] **AC-7 — `GET /auth/login` con sesión activa**: si cookie session válida, 302 directo a `/mcp-setup` sin tocar cookie state.
- [ ] **AC-8 — `GET /auth/callback` primer login (RF-AUTH-02 + RF-AUTH-04)**: `respx` mockea `/token` y `/jwks` de MS; tras callback exitoso, hay 1 row en `users`, 1 en `oauth_tokens` (con BYTEA no nulos y distintos al plaintext del refresh), 1 en `mcp_bearers` con `revoked_at IS NULL`. Cookie session seteada. Cookie `mcp_bearer_flash` seteada con plaintext (TTL 60s). Response 302 a `/mcp-setup`.
- [ ] **AC-9 — `GET /auth/callback` login subsiguiente**: user existente con misma `microsoft_oid`. Tras callback, `users.last_login_at` actualizado, `oauth_tokens` actualizado (UPDATE no INSERT), **NO** se crea nuevo `mcp_bearers`. Cookie session emitida.
- [ ] **AC-10 — `GET /auth/callback` state mismatch (RF-AUTH-02 ERR)**: cookie `oauth_state` con `state="abc"`, query `state=xyz`. Response 302 a `/login?error=AUTH_INVALID_STATE`. **NO** se crea ningún row en DB.
- [ ] **AC-11 — `GET /auth/callback` tenant rechazado (RF-AUTH-03)**: `id_token.tid != MS_TENANT_ID`. Response 302 a `/login?error=AUTH_TENANT_NOT_ALLOWED`. NO se crea user.
- [ ] **AC-12 — `GET /auth/callback` MS unavailable (RF-AUTH-05)**: `respx` retorna 503 al `/token`. Response 302 a `/login?error=AUTH_PROVIDER_UNAVAILABLE`. Log `auth_provider_unavailable` emitido.
- [ ] **AC-13 — `GET /auth/me` con flash (RF-AUTH-06)**: user logueado + cookie `mcp_bearer_flash` presente. Response 200 con `{user.id, user.email, bearer.plaintext != null, bearer.id, mcp_url}`. Cookie flash borrada (Set-Cookie Max-Age=0 en response).
- [ ] **AC-14 — `GET /auth/me` sin flash**: user logueado, sin cookie flash. Response 200 con `bearer.plaintext == null`, `bearer.id != null`. Sin Set-Cookie.
- [ ] **AC-15 — `GET /auth/me` no autenticado**: sin cookie session. Response 401 con `error_code: AUTH_NOT_AUTHENTICATED`.
- [ ] **AC-16 — `POST /auth/regenerate-mcp-token` (RF-AUTH-07)**: user logueado con bearer activo. Response 200 con `{bearer.plaintext, bearer.id}`. Bearer viejo tiene `revoked_at NOT NULL` en DB. Nuevo bearer activo. El plaintext viejo, si se intenta usar contra `verify_bearer`, retorna None.
- [ ] **AC-17 — Bearer middleware `get_current_user_mcp`**: dependency en una test route `/_test_mcp_protected`. Sin Authorization → 401. Con bearer válido → 200 con user. Con bearer revoked → 401. Con bearer inexistente → 401. Con bearer válido, `db.info["user_id"]` queda seteado tras la dependency.
- [ ] **AC-18 — Cross-user isolation (integración con ADR-014)**: dos users A y B. User A con bearer A llama una test route que hace `select(Transcription)` sobre datos de A y B. La query retorna SOLO datos de A. Lo mismo simétrico con B. Esto prueba que `get_current_user_mcp` activa el listener S-1.
- [ ] **AC-19 — `oauth_tokens.ms_*_encrypted` no almacena plaintext**: tras callback exitoso, query directa a Postgres `SELECT ms_access_token_encrypted, ms_refresh_token_encrypted FROM oauth_tokens WHERE user_id = ?`. Los bytes leídos no contienen el plaintext (verificable con substring check del access_token mockeado).
- [ ] **AC-20 — Logout limpia sesión**: user logueado, `POST /auth/logout` → 302 a `/login`, cookie session se borra (Set-Cookie con Max-Age=0). Subsiguiente `/auth/me` retorna 401.

## Secondary Flows / Errors

- **ERR-1 — JWKS cache stale durante rotación de MS**: si MS rota sus keys, el JWKS cacheado puede ser stale. Mitigación: TTL del cache = 24h; en error de validación de firma, refrescar JWKS y reintentar UNA vez. Si falla de nuevo, error real → AUTH_INVALID_OAUTH_CODE.
- **ERR-2 — `OAUTH_TOKEN_ENC_KEY` inválida** (no es 32 bytes): startup falla loud con mensaje claro, no en runtime al primer encrypt. Validación en `Settings` post-init.
- **ERR-3 — `JWT_SECRET` corto** (< 32 chars): startup falla loud. Validación en `Settings` post-init.
- **ERR-4 — Race en doble regenerate**: dos POST `/auth/regenerate-mcp-token` simultáneos. Partial UNIQUE en `mcp_bearers (user_id) WHERE revoked_at IS NULL` (de ADR-014/Capa 1) hace que el segundo INSERT falle con IntegrityError. Handler retry una vez después de `revoke_active(user)`. Si vuelve a fallar, 500 con INTERNAL_ERROR.
- **ERR-5 — Cookie session pisada por re-login**: user logueado se loguea de nuevo en otro browser. Cookie session se renueva en el segundo browser; el primero sigue válido hasta TTL (no se invalida globalmente). Documentado, no es bug.
- **ALT-1 — Logout deja bearer activo**: `POST /auth/logout` solo borra cookie web, NO revoca bearers MCP. Razón: el user puede haber compartido el bearer con su Claude Desktop fuera del browser; revocar al logout sorprendería. Para revocar el bearer hay endpoint dedicado `POST /auth/regenerate-mcp-token` (RF-AUTH-07). Documentado.
- **ALT-2 — `get_current_user_web` vs `get_current_user_mcp` separados**: el web depende de cookie session firmada con `JWT_SECRET`; el MCP depende de bearer plaintext + lookup en DB. Se mantienen separados porque sus tradeoffs de revocación son distintos (cookie expira por TTL, bearer requiere DB write para revocar). Compartir un solo dependency unificada complicaría el flow.
- **ALT-3 — `/auth/logout` global vs local**: opción global (revocar bearers + cookie + invalidar todas las sesiones del user) requiere blacklist de JWTs o columna `tokens_invalidated_at` en users. Se difiere a Capa 7 si se justifica. Hoy: solo borrar cookie del browser actual.

---

## Trazabilidad cruzada con la wiki

| AC | Wiki ref |
|---|---|
| AC-1, AC-17 | `wiki/02_arquitectura.md` §3 (componente B — Servicio de Autenticación) |
| AC-2, AC-3, AC-19 | RF-AUTH-02 §"Closed decisions" (AES-256-GCM con OAUTH_TOKEN_ENC_KEY) |
| AC-4, AC-5 | RF-AUTH-01 + RF-AUTH-02 (cookies oauth_state + session) |
| AC-6, AC-7 | RF-AUTH-01 §"Process Steps" + Gherkin |
| AC-8, AC-9 | RF-AUTH-02 §"Process Steps" + Gherkin (primer login + login subsiguiente) |
| AC-10 | RF-AUTH-02 §"Typed Errors" AUTH_INVALID_STATE |
| AC-11 | RF-AUTH-03 (validación tenant) |
| AC-12 | RF-AUTH-05 (manejo errores provider) |
| AC-13, AC-14, AC-15 | RF-AUTH-06 §Gherkin |
| AC-16 | RF-AUTH-07 §Gherkin |
| AC-17, AC-18 | wiki/02_arquitectura.md §8 (Seguridad — per-user scoping) + ADR-014 (listener S-1 activado por dependency MCP) |
| AC-20 | RF-AUTH-06 (extension — logout no estaba en RFs originales pero lo agregamos por completitud) |
