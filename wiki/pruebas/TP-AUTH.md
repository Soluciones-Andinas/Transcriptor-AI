# Test Plan — Módulo AUTH (Autenticación con Microsoft Entra ID)

**Source RFs**: [`RF/RF-AUTH.md`](../RF/RF-AUTH.md)
**Stack**: pytest 8.x + pytest-asyncio + httpx + freezegun + responses (mock HTTP a Microsoft Entra)

## Convenciones

- Tests de auth NO golpean Microsoft Entra real. Se mockea con `responses` o `httpx-mock` el endpoint `/oauth2/v2.0/token` y JWKS.
- Para validar firmas de id_token se usa una clave RSA fixture y un JWKS local que el código lee en lugar del de MS en tests.
- Variables de entorno para tests: `MS_CLIENT_ID=test-client`, `MS_TENANT_ID=test-tenant`, `MS_CLIENT_SECRET=test-secret`, `OAUTH_TOKEN_ENC_KEY=<32 bytes b64>`, `JWT_SECRET=<test-secret>`.

## TP-AUTH-01: Iniciar flow OAuth (RF-AUTH-01)

| Test ID | Tipo | Descripción | Setup | Acción | Aserciones |
|---|---|---|---|---|---|
| TP-AUTH-01-pos-01 | Unit | User anónimo recibe redirect a MS | Sin cookie session | `GET /auth/login` | status=302; `Location` contiene `login.microsoftonline.com/test-tenant/oauth2/v2.0/authorize`; cookie `oauth_state` setea con TTL 5 min; query incluye `client_id`, `state`, `code_challenge`, `code_challenge_method=S256` |
| TP-AUTH-01-pos-02 | Unit | User logueado va directo a /mcp-setup | Cookie `session` JWT válida | `GET /auth/login` | status=302; `Location=/mcp-setup` |
| TP-AUTH-01-cov-01 | Cobertura | State único entre 2 calls consecutivos | — | 2 GETs a `/auth/login` | states distintos |

## TP-AUTH-02: Callback OAuth (RF-AUTH-02)

| Test ID | Tipo | Descripción | Setup | Acción | Aserciones |
|---|---|---|---|---|---|
| TP-AUTH-02-pos-01 | Integration | Primer login crea user, oauth_tokens, mcp_bearer | Cookie `oauth_state` válida; mock MS retorna tokens válidos con `tid=test-tenant`; user oid no existe en DB | `GET /auth/callback?code=...&state=...` | status=302 a `/mcp-setup`; row en `users`; row en `oauth_tokens`; row en `mcp_bearers` con `revoked_at IS NULL`; cookie `session` setea; cookie temp `mcp_bearer_flash` con plaintext setea |
| TP-AUTH-02-pos-02 | Integration | Login subsiguiente actualiza last_login_at | User existente con oid | `GET /auth/callback` | `users.last_login_at` actualizado; NO se crea nuevo bearer |
| TP-AUTH-02-neg-01 | Unit | State no coincide → 400 | Cookie `oauth_state.state="abc"`; query `state="xyz"` | `GET /auth/callback` | status=302 a `/login?error=AUTH_INVALID_STATE`; sin INSERTs |
| TP-AUTH-02-neg-02 | Unit (freezegun) | Cookie `oauth_state` expirada | TTL 5 min vencido (freezegun avanza 6 min) | callback | redirect con `AUTH_INVALID_STATE` |
| TP-AUTH-02-neg-03 | Mock | MS retorna `invalid_grant` | Mock response 400 | callback | redirect con `AUTH_INVALID_OAUTH_CODE` |

## TP-AUTH-03: Validar tenant (RF-AUTH-03)

| Test ID | Tipo | Descripción | Setup | Acción | Aserciones |
|---|---|---|---|---|---|
| TP-AUTH-03-pos-01 | Unit | id_token con tid correcto | mock token con `tid=test-tenant`, firma válida | validación | OK |
| TP-AUTH-03-neg-01 | Unit | tid distinto al esperado | `tid=other-tenant` | validación | rechazo `AUTH_TENANT_NOT_ALLOWED` |
| TP-AUTH-03-neg-02 | Unit | claim missing | id_token sin `tid` | validación | rechazo |
| TP-AUTH-03-neg-03 | Unit | firma inválida | id_token firmado con clave distinta | validación | rechazo |

## TP-AUTH-04: Bearer al primer login (RF-AUTH-04)

| Test ID | Tipo | Descripción | Setup | Acción | Aserciones |
|---|---|---|---|---|---|
| TP-AUTH-04-pos-01 | Integration | Primer login emite bearer | DB sin user | callback OK | row `mcp_bearers` creada; cookie flash con plaintext |
| TP-AUTH-04-pos-02 | Integration | Login subsiguiente NO emite | DB con user + bearer activo | callback OK | mismo bearer (no se crea nuevo) |

## TP-AUTH-05: Errores del provider (RF-AUTH-05)

| Test ID | Tipo | Descripción | Setup | Acción | Aserciones |
|---|---|---|---|---|---|
| TP-AUTH-05-neg-01 | Mock | MS retorna 503 | Mock response 503 en `/token` | callback | status=302 a `/login?error=AUTH_PROVIDER_UNAVAILABLE` |
| TP-AUTH-05-neg-02 | Mock | Timeout en token exchange | Mock con delay > 10s | callback | redirect con `AUTH_PROVIDER_UNAVAILABLE` |

## TP-AUTH-06: GET /auth/me (RF-AUTH-06)

| Test ID | Tipo | Descripción | Setup | Acción | Aserciones |
|---|---|---|---|---|---|
| TP-AUTH-06-pos-01 | Integration | Con flash cookie | Cookie session + cookie flash | `GET /auth/me` | 200; body con `bearer.plaintext != null`; cookie flash borrada en response |
| TP-AUTH-06-pos-02 | Integration | Sin flash cookie | Cookie session sin flash | `GET /auth/me` | 200; `bearer.plaintext == null`; `bearer.id != null` |
| TP-AUTH-06-neg-01 | Unit | Sin cookie session | — | `GET /auth/me` | 401 + `AUTH_NOT_AUTHENTICATED` |
| TP-AUTH-06-neg-02 | Unit (freezegun) | Cookie session expirada (TTL 24h) | freezegun avanza 25h | `GET /auth/me` | 401 |

## TP-AUTH-07: Regenerar bearer MCP (RF-AUTH-07)

| Test ID | Tipo | Descripción | Setup | Acción | Aserciones |
|---|---|---|---|---|---|
| TP-AUTH-07-pos-01 | Integration | Revocación + emisión | User logueado, bearer activo `B1` | `POST /auth/regenerate-mcp-token` | 200; body con nuevo `B2` plaintext; `B1.revoked_at IS NOT NULL`; `B2` activo |
| TP-AUTH-07-pos-02 | Integration | Bearer viejo rechazado en MCP | Tras regenerate, usar `B1` para tool MCP | tool call con `B1` | 401 + `MCP_BEARER_REVOKED` |
| TP-AUTH-07-neg-01 | Unit | Sin cookie session | — | regenerate | 401 |

## Helpers de testing

```python
# tests/helpers/auth_factory.py

def make_jwt_session(user_id: str, secret: str, ttl_seconds: int = 86400) -> str:
    """Genera cookie session JWT firmada para fixtures."""
    import jwt, time
    payload = {"sub": user_id, "iat": int(time.time()), "exp": int(time.time()) + ttl_seconds}
    return jwt.encode(payload, secret, algorithm="HS256")

def mock_microsoft_token_response(monkeypatch, ms_oid: str, tid: str = "test-tenant"):
    """Mockea POST /token de MS Entra y JWKS lookup."""
    # ... usa responses o httpx_mock
```

## Cobertura objetivo

- Líneas: ≥ 85 % en módulo `auth/`.
- Branches: ≥ 80 %.
- Cada `error_code` documentado de la familia AUTH cubierto.
- Per-user isolation no aplica acá (no hay queries de datos de user).
