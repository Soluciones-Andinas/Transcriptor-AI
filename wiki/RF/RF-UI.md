# Módulo UI — Requerimientos Funcionales (Frontend mínimo)

**Source flow**: [`FL-AUTH-01`](../FL/FL-AUTH-01.md), [`FL-MCP-01`](../FL/FL-MCP-01.md)
**Architecture**: [`02_arquitectura.md`](../02_arquitectura.md) §3 (componente A)
**Hardening level**: Execution-Normative

> **Estado del módulo (2026-05-11)**: RF-UI-01 y RF-UI-02 quedan **Pendiente Capa 5**. El backend ya expone los endpoints de soporte (`/auth/login`, `/auth/me`, `/auth/regenerate-mcp-token`), pero el bundle React + Vite + Tailwind no está implementado todavía (no existe `src/transcription_api/ui/` ni archivos `.tsx`/`.jsx` en el repo). Esta spec se mantiene como contrato de diseño para cuando Capa 5 arranque; no usar como evidencia de que el feature está hecho.

## Tabla resumen

| ID | Título | Actor | Pre-condición | Entradas | Salidas |
|---|---|---|---|---|---|
| RF-UI-01 | Renderizar `/login` con botón MS SSO | Anónimo | — | — | HTML con botón |
| RF-UI-02 | Renderizar `/mcp-setup` con config Claude | Logueado | Cookie session válida | — | HTML con snippets de config |

---

## RF-UI-01: Renderizar `/login`

### Execution Sheet

| Campo | Valor |
|---|---|
| ID | RF-UI-01 |
| Título | Pantalla de login con botón "Iniciar sesión con Microsoft" |
| Actor primario | Usuario anónimo |
| Prioridad | Alta |
| Severidad | Mayor |

### Precondiciones

| # | Condición |
|---|---|
| 1 | Build de Vite produjo el bundle estático |
| 2 | FastAPI sirve `dist/` vía StaticFiles en `/` |

### Inputs

- Path `/login`.
- Optional query param `error` (e.g. `AUTH_TENANT_NOT_ALLOWED`).

### Process Steps

| # | Paso |
|---|---|
| 1 | React router monta el componente Login en path `/login` |
| 2 | Si query param `error` presente: renderiza banner de error legible (mapping de error_codes a strings en español) |
| 3 | Renderiza header con logo Sandinas (placeholder si no hay) |
| 4 | Renderiza botón principal "Iniciar sesión con Microsoft" |
| 5 | El botón al click navega a `GET /auth/login` (full page redirect, no fetch) |
| 6 | Footer con versión del sistema y link a soporte interno |

### Outputs

| Campo | Destino |
|---|---|
| HTML renderizado | Browser |
| Click en botón | Redirect a `/auth/login` |

### Special Cases

- **User ya logueado** (cookie session válida): React detecta cookie a través de `/auth/me` y redirige a `/mcp-setup`.
- **Error en query param**: mapping `AUTH_INVALID_STATE` → "Sesión expirada, intentá de nuevo"; `AUTH_TENANT_NOT_ALLOWED` → "Tu cuenta no pertenece a Sandinas"; `AUTH_PROVIDER_UNAVAILABLE` → "Microsoft no está disponible, reintentá en unos minutos".

### Acceptance Criteria

```gherkin
Scenario: Render inicial
  Given browser anónimo
  When GET /login
  Then HTML contiene texto "Iniciar sesión con Microsoft"
    And botón con onclick navegando a /auth/login

Scenario: Render con error
  When GET /login?error=AUTH_TENANT_NOT_ALLOWED
  Then HTML contiene "Tu cuenta no pertenece a Sandinas"

Scenario: User ya logueado
  Given cookie session válida
  When GET /login
  Then después del fetch /auth/me, React redirige a /mcp-setup
```

### Test Traceability

| Test ID | Tipo |
|---|---|
| TP-UI-01-pos-01 | Positivo (render inicial) |
| TP-UI-01-pos-02 | Positivo (mensaje de error) |
| TP-UI-01-pos-03 | Positivo (redirect si logueado) |

**TODO explicit = 0**.

---

## RF-UI-02: Renderizar `/mcp-setup` con configuración

### Execution Sheet

| Campo | Valor |
|---|---|
| ID | RF-UI-02 |
| Título | Pantalla post-login con instrucciones para configurar Claude Code y Desktop |
| Actor primario | Usuario logueado |
| Prioridad | Alta |
| Severidad | Mayor |

### Precondiciones

| # | Condición |
|---|---|
| 1 | Cookie session válida (delegado en `/auth/me` que devuelve 401 si no) |

### Process Steps

| # | Paso |
|---|---|
| 1 | React router monta componente MCPSetup en `/mcp-setup` |
| 2 | useEffect: fetch `/auth/me` con cookie |
| 3 | Si 401: redirect a `/login` |
| 4 | Si 200: extraer `{user, bearer, mcp_url}` |
| 5 | Renderizar header con `Hola, <display_name>`. |
| 6 | Si `bearer.plaintext` presente (cookie flash): mostrar warning "este token se muestra una sola vez, copialo ahora" + render del bearer en bloque copiable |
| 7 | Si `bearer.plaintext == null`: mostrar mensaje "Token activo (id: <bearer_id>). Si lo perdiste, regeneralo abajo." |
| 8 | Renderizar dos snippets pre-formateados con el `mcp_url` y el bearer (cuando esté disponible): |
|   | a. Para Claude Code: bloque JSON listo para `.mcp.json` |
|   | b. Para Claude Desktop: bloque JSON para `claude_desktop_config.json` |
| 9 | Botón "copiar al portapapeles" en cada snippet |
| 10 | Botón "Regenerar bearer" con confirm modal; al confirmar: POST `/auth/regenerate-mcp-token`, recibir nuevo bearer plaintext, re-renderizar |
| 11 | Botón "Cerrar sesión" → POST `/auth/logout` |

### Outputs

| Campo | Destino |
|---|---|
| HTML con configs | Browser |
| Click en copiar | Clipboard del user |
| Click en regenerar | API call + re-render |
| Click en logout | API call + redirect `/login` |

### Special Cases

- **Bearer plaintext perdido**: `bearer.plaintext == null` indica que se mostró antes y ya no está disponible; user debe regenerar.
- **Bearer recién regenerado**: API responde con plaintext en el body; React lo guarda en estado local (no en localStorage) y lo muestra hasta que el user navegue away.

### Snippets generados (formato exacto)

**Claude Code (`.mcp.json` en project root)**:

```json
{
  "mcpServers": {
    "transcription-api": {
      "url": "<mcp_url>",
      "headers": {
        "Authorization": "Bearer <bearer_plaintext>"
      }
    }
  }
}
```

**Claude Desktop (`claude_desktop_config.json`)**:

```json
{
  "mcpServers": {
    "transcription-api": {
      "url": "<mcp_url>",
      "transport": "streamable_http",
      "headers": {
        "Authorization": "Bearer <bearer_plaintext>"
      }
    }
  }
}
```

### Acceptance Criteria

```gherkin
Scenario: Render con bearer fresh
  Given user recién logueado, cookie flash con bearer_plaintext presente
  When render /mcp-setup
  Then HTML muestra el bearer en bloque copiable
    And HTML contiene snippets para Claude Code y Desktop con bearer interpolado
    And HTML muestra warning "se muestra una sola vez"

Scenario: Render sin bearer fresh
  Given user logueado hace 5 minutos
  When render /mcp-setup
  Then HTML NO muestra el bearer plaintext
    And mensaje "regenerá si lo perdiste"
    And snippets muestran placeholder "<TU_BEARER>"

Scenario: Regenerar bearer
  Given user logueado
  When click "Regenerar" + confirm
  Then POST /auth/regenerate-mcp-token
    And UI re-renderiza con nuevo bearer en plaintext
    And snippets se actualizan con nuevo bearer
```

### Test Traceability

| Test ID | Tipo |
|---|---|
| TP-UI-02-pos-01 | Positivo (render con flash) |
| TP-UI-02-pos-02 | Positivo (render sin flash) |
| TP-UI-02-pos-03 | Positivo (regenerar) |
| TP-UI-02-neg-01 | Negativo (sin auth → redirect login) |

**TODO explicit = 0**.
