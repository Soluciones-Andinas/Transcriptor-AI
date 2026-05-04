# Test Plan — Módulo UI (Frontend mínimo)

**Source RFs**: [`RF/RF-UI.md`](../RF/RF-UI.md)
**Stack**: Vitest + @testing-library/react para unit; Playwright para E2E (opcional para MVP)

## Convenciones

- Tests unitarios mockean `fetch` con `vi.fn()`. Cookies se simulan vía `document.cookie` mock.
- Tests E2E (Playwright) son opcionales en MVP; se priorizan los unit + integration via testing-library.

## TP-UI-01: Login screen (RF-UI-01)

| Test ID | Tipo | Descripción | Setup | Acción | Aserciones |
|---|---|---|---|---|---|
| TP-UI-01-pos-01 | Unit | Render inicial | Sin error en query string; no logueado | render `<LoginPage />` | DOM contiene texto "Iniciar sesión con Microsoft"; botón con `onclick` que navega a `/auth/login` |
| TP-UI-01-pos-02 | Unit | Mensaje de error | URL `?error=AUTH_TENANT_NOT_ALLOWED` | render con query | DOM contiene "Tu cuenta no pertenece a Sandinas" |
| TP-UI-01-pos-03 | Integration | User logueado redirige | mock `/auth/me` retorna 200 con user | render | después de fetch, navegación a `/mcp-setup` |

## TP-UI-02: MCP Setup screen (RF-UI-02)

| Test ID | Tipo | Descripción | Setup | Acción | Aserciones |
|---|---|---|---|---|---|
| TP-UI-02-pos-01 | Integration | Render con bearer flash | mock `/auth/me` retorna `bearer.plaintext != null` | render `<MCPSetupPage />` | DOM muestra bearer en bloque copiable; warning "se muestra una sola vez"; snippets para Claude Code y Desktop con bearer interpolado |
| TP-UI-02-pos-02 | Integration | Render sin bearer flash | mock `/auth/me` retorna `bearer.plaintext == null` | render | DOM NO muestra bearer plaintext; mensaje "regenerá si lo perdiste"; snippets con placeholder `<TU_BEARER>` |
| TP-UI-02-pos-03 | Integration | Regenerar bearer | mock initial sin flash; mock `/auth/regenerate-mcp-token` retorna nuevo bearer | render → click "Regenerar" → confirm → render | UI re-renderiza con nuevo bearer plaintext en snippets |
| TP-UI-02-neg-01 | Integration | Sin auth | mock `/auth/me` retorna 401 | render | navegación a `/login` |

## Snippets verificados

Validar que los snippets generados son JSON válido y contienen los placeholders correctos:

```javascript
// tests/ui/MCPSetup.test.jsx
test("snippet Claude Code es JSON válido", () => {
  const { getByTestId } = render(<MCPSetupPage />);
  const snippet = getByTestId("snippet-claude-code").textContent;
  const parsed = JSON.parse(snippet);
  expect(parsed.mcpServers["transcription-api"].url).toBeDefined();
  expect(parsed.mcpServers["transcription-api"].headers.Authorization).toMatch(/^Bearer /);
});
```

## Cobertura objetivo

- Líneas: ≥ 80 % en componentes React.
- Cada estado de UI (logueado, no logueado, con flash, sin flash, error) tiene un test.
