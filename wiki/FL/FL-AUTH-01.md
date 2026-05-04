# FL-AUTH-01 — Login con Microsoft Entra ID

## 1. Objetivo

Autenticar al usuario contra el tenant Microsoft Entra ID de Sandinas y dejarlo logueado en la app con cookie web + bearer MCP listos para usar.

## 2. Alcance

**In**: flow OAuth 2.0 Authorization Code con PKCE contra Microsoft Entra; creación o actualización del registro `users` en Postgres; emisión de cookie de sesión JWT; emisión de bearer MCP inicial al primer login.

**Out**: registro de usuarios externos a Sandinas, MFA custom, recuperación de password (no aplica con SSO), administración del tenant.

## 3. Actores y ownership

| Actor | Ownership |
|---|---|
| Usuario | Inicia el flujo desde el browser; aprueba el consent de Microsoft. |
| UI React | Renderiza `/login` y trigger del redirect; recibe sesión post-callback. |
| FastAPI Auth | Construye el authorization URL, valida el callback, intercambia code por tokens, persiste el user, emite cookie. |
| Microsoft Entra ID | Valida credenciales del usuario, emite tokens. |
| Postgres | Almacena `users`, `oauth_tokens`, `mcp_bearers`. |

## 4. Precondiciones

1. App registrada en Azure Portal del tenant Sandinas con `redirect_uri = <BASE_URL>/auth/callback` y scopes `openid profile email User.Read`.
2. Variables de entorno `MS_CLIENT_ID`, `MS_CLIENT_SECRET`, `MS_TENANT_ID`, `MS_REDIRECT_URI` configuradas.
3. Postgres reachable.
4. Backend tiene clave para encriptar refresh tokens (`OAUTH_TOKEN_ENC_KEY`) y firmar JWTs (`JWT_SECRET`).

## 5. Postcondiciones

**Éxito**:
- Existe registro en `users` con el `microsoft_oid` del usuario (creado o actualizado `last_login_at`).
- Existe (o se actualizó) registro en `oauth_tokens` con el access+refresh tokens encriptados.
- Existe (al primer login) un registro activo en `mcp_bearers` con `revoked_at IS NULL`.
- Cliente recibe cookie `session` (HttpOnly, Secure, SameSite=Strict).
- Browser redirigido a `/mcp-setup`.

**Error**:
- Sin cookie emitida.
- Sin filas insertadas en Postgres si el callback falla.
- Browser redirigido a `/login?error=<error_code>`.

## 6. Secuencia principal

```mermaid
sequenceDiagram
    participant U as Usuario (browser)
    participant UI as UI React
    participant API as FastAPI Auth
    participant MS as Microsoft Entra ID
    participant PG as Postgres

    U->>UI: GET /login
    UI-->>U: render botón "Iniciar sesión"
    U->>API: GET /auth/login
    API->>API: genera state + PKCE verifier, guarda en cookie temp
    API-->>U: 302 redirect a Microsoft Entra
    U->>MS: GET /authorize?client_id=...&code_challenge=...
    MS-->>U: consent screen; user aprueba
    MS-->>U: 302 redirect a /auth/callback?code=...&state=...
    U->>API: GET /auth/callback?code=...&state=...
    API->>API: valida state, recupera PKCE verifier
    API->>MS: POST /token (code + verifier + client_secret)
    MS-->>API: access_token, refresh_token, id_token
    API->>API: valida id_token (issuer, audience, exp)
    API->>API: extrae oid, email, name del id_token
    alt user nuevo
        API->>PG: INSERT users + INSERT oauth_tokens + INSERT mcp_bearers
        API->>API: emit log auth_user_created
    else user existente
        API->>PG: UPDATE users.last_login_at + UPDATE oauth_tokens
        API->>API: emit log auth_user_login
    end
    API->>API: firma JWT cookie de sesión
    API-->>U: 302 redirect a /mcp-setup + Set-Cookie session=<JWT>
    U->>UI: GET /mcp-setup (con cookie)
    UI-->>U: muestra config MCP
```

## 7. Camino alternativo / errores

| Condición | Manejo |
|---|---|
| `state` no coincide con cookie temp | 400 + redirect `/login?error=AUTH_INVALID_STATE`; log `auth_callback_received` con `success=false` |
| `code` inválido al hacer token exchange con MS | 400 + redirect `/login?error=AUTH_INVALID_OAUTH_CODE` |
| `id_token.tid` (tenant id) ≠ `MS_TENANT_ID` configurado | 403 + redirect `/login?error=AUTH_TENANT_NOT_ALLOWED` |
| MS Entra no disponible (timeout/5xx) | 502 + redirect `/login?error=AUTH_PROVIDER_UNAVAILABLE` |
| Postgres no reachable | 503; el cliente puede reintentar |
| Cookie temp con state ausente (user pegó URL `/auth/callback` directo) | 400 + redirect `/login?error=AUTH_INVALID_STATE` |

## 8. Slice de arquitectura

Componentes activados (de [`02_arquitectura.md`](../02_arquitectura.md) §3):
- A. UI React (renderiza `/login`).
- B. Servicio de Autenticación (FastAPI Auth).
- G. Persistencia Relacional (Postgres).

ADRs aplicables: [ADR-008](../ADR/ADR-008.md), [ADR-009](../ADR/ADR-009.md), [ADR-010](../ADR/ADR-010.md).

## 9. Touchpoints de datos

**Entidades creadas/actualizadas**: `users`, `oauth_tokens`, `mcp_bearers` (al primer login).

**Entidades transitorias**: cookie temp con `state` y PKCE verifier (TTL 5 min).

**Eventos de log** (de [`05_modelo_datos.md`](../05_modelo_datos.md) §7): `auth_login_started`, `auth_callback_received`, `auth_user_created`, `auth_user_login`, `mcp_bearer_generated`.

## 10. RF candidatos

| RF candidato | Cubre |
|---|---|
| RF-AUTH-01 | Iniciar flow OAuth: generar state, PKCE, redirect a MS |
| RF-AUTH-02 | Recibir callback OAuth, validar, intercambiar code, crear/actualizar user |
| RF-AUTH-03 | Validar tenant del id_token (rechazar tenants externos) |
| RF-AUTH-04 | Emitir bearer MCP al primer login |
| RF-AUTH-05 | Manejar errores del provider (timeouts, 5xx de MS) |

## 11. Cuellos de botella, riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| Outage de Microsoft Entra impide login | Aceptable para uso interno; documentar status page |
| Refresh token rotado por MS sin actualizarlo | Background task que refresca tokens antes de expirar; en su defecto, re-login del user |
| Cookie de sesión robada en máquina del user | `HttpOnly`, `Secure`, `SameSite=Strict`; TTL 24h; no se almacenan claims sensibles en el JWT |
| Encriptación de refresh token comprometida si `OAUTH_TOKEN_ENC_KEY` se filtra | Rotación de la key requiere re-login de todos; documentar |

## 12. RF handoff checklist

- [x] Actores y ownership explícitos.
- [x] Diagrama mermaid del camino principal.
- [x] Camino de error documentado.
- [x] Estados y eventos clave listados.
- [x] Cuellos de botella y mitigaciones explícitos.
- [x] RFs candidatos enumerados.
- [x] No hay decisiones críticas abiertas.
- [x] Listo para `crear-rf`.
