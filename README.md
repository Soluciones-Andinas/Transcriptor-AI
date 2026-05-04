# transcription-api

Servicio multi-tenant self-hosted para transcripción + diarización de reuniones en español, integrado con el Claude personal de cada usuario vía MCP. Ejecutable en GPU local de 16 GB VRAM.

**Estado**: Wiki SDD completa (alcance + arquitectura + 13 ADRs + 6 flujos + 26 RFs + matriz de pruebas). Infra Docker básica lista. Implementación funcional pendiente.

## Para qué sirve

Cada usuario de Sandinas se autentica vía Microsoft SSO, configura el MCP server en su Claude Code o Claude Desktop, y a partir de ahí le pasa archivos de audio o video a su Claude. Claude orquesta el upload + procesamiento contra el rig, recibe el transcript diarizado, y genera la minuta dentro de su contexto usando el transcript + capturas de pantalla asociadas. La intranet mantiene los datos privados; solo el transcript redactado sale a la nube cuando el usuario explícitamente le pide a su Claude que use el MCP.

## Arquitectura

```
INTRANET (rig Sandinas)                          USER MACHINE
┌──────────────────────────────────────┐         ┌──────────────────┐
│ React UI (login + mcp-setup)         │         │ Claude Code o    │
│ FastAPI (auth + REST upload + MCP)   │◄──MCP──►│ Claude Desktop   │
│   ├── Pipeline: ffmpeg → WhisperX    │  OAuth  │ con custom MCP   │
│   │           → pyannote → merge     │         │ connector        │
│   ├── Caché efímero (filesystem 24h) │         └──────────────────┘
│   └── Postgres (users, history,      │
│                tokens, images)       │         CLOUD
│                                      │         ┌──────────────────┐
│ GPU 16 GB VRAM, Docker, intranet     │◄─OIDC──►│ Microsoft Entra  │
└──────────────────────────────────────┘         │ (tenant Sandinas)│
                                                 └──────────────────┘
```

## Stack

| Componente | Tecnología | Justificación |
|---|---|---|
| HTTP / MCP | FastAPI + Uvicorn + `mcp` SDK | MCP-first ([ADR-011](wiki/ADR/ADR-011.md)); REST mínimo solo para blobs |
| STT | Whisper large-v3 (vía WhisperX) | [ADR-001](wiki/ADR/ADR-001.md): español ~3-6 % WER; framework integrado |
| Diarización | pyannote 3.1 | [ADR-002](wiki/ADR/ADR-002.md): multilingüe |
| Audio | ffmpeg | Universal |
| Persistencia | PostgreSQL 16 + filesystem | [ADR-008](wiki/ADR/ADR-008.md): Postgres para datos con identidad; filesystem para caché efímero |
| Auth | Microsoft Entra ID OAuth 2.0 / OIDC | [ADR-009](wiki/ADR/ADR-009.md): SSO contra tenant corporativo |
| Frontend | React 18 + Vite 5 + Tailwind | [ADR-010](wiki/ADR/ADR-010.md): UI mínima de onboarding (2 páginas) |
| Empaquetado | Docker + nvidia-container-toolkit | [ADR-006](wiki/ADR/ADR-006.md) |

Detalle completo en `wiki/02_arquitectura.md` y los 13 ADRs en `wiki/ADR/`.

## Estado actual de la implementación

| Fase | Entrega | Estado |
|---|---|---|
| Wiki SDD | Alcance + Arquitectura + ADRs + Flujos + RFs + Test Matrix | Listo |
| Fase 0 — Infra Docker | `Dockerfile`, `docker-compose.yml`, GPU pass-through, healthcheck | Listo (sin Postgres aún) |
| Fase 1 — Skeleton FastAPI | `GET /health` con verificación real de GPU/VRAM/disco | Listo |
| Capa 1 — Postgres | Postgres en compose, modelos SQLAlchemy, Alembic | Pendiente |
| Capa 2 — Auth | MS Entra OAuth, `/auth/*`, MCP bearer | Pendiente |
| Capa 3 — UI mínima | React + Vite, login + mcp-setup | Pendiente |
| Capa 4 — Pipeline real | RF-TRX-01..06: ffmpeg + WhisperX + pyannote | Pendiente |
| Capa 5 — REST upload | `/api/upload`, `/api/upload-image` | Pendiente |
| Capa 6 — MCP Server | tools + resources OAuth-protected | Pendiente |
| Capa 7 — Imágenes | RF-IMG-01..03 | Pendiente |
| Validación E2E | User real conecta su Claude, transcribe, genera minuta | Pendiente |

## Quickstart

```bash
# Una sola vez: copiar .env.example y completar con tu HF_TOKEN
cp .env.example .env
$EDITOR .env

# Levantar servicio (GPU + volúmenes auto-montados)
docker compose up -d

# Ver logs (modelos cargan en ~30s la primera vez que estén implementados)
docker compose logs -f

# Verificar salud (esto YA funciona en la fase actual)
curl http://localhost:8000/health
# Respuesta esperada:
# {
#   "status": "ok",
#   "version": "0.1.0",
#   "gpu_available": true,
#   "gpu_name": "NVIDIA GeForce RTX 4080",
#   "vram_total_mb": 16376,
#   "vram_free_mb": 15800,
#   "cuda_version": "12.1",
#   "data_dir_writable": true,
#   "cache_entries": 0
# }
```

Cuando se implemente Fase 2:

```bash
# Transcribir un MP4
curl -F file=@reunion.mp4 \
     -F language=es \
     -F max_speakers=4 \
     http://localhost:8000/transcribe \
     -o transcripcion.json
```

## Prerequisitos del rig

- GPU NVIDIA con 16 GB VRAM, drivers ≥ 535, CUDA 12.1+
- Docker Engine 24+ con `docker compose` plugin v2.20+
- `nvidia-container-toolkit` instalado y configurado (`nvidia-smi` debe funcionar dentro de un container)
- 50 GB libres de disco (10 GB modelos + 10 MB caché 24h + sistema)
- Cuenta HuggingFace con token y términos de `pyannote/speaker-diarization-3.1` aceptados

## Documentos

- [`docs/INVESTIGACION.md`](docs/INVESTIGACION.md) — Estado del arte 2026, modelos evaluados, comparativas
- [`docs/PLAN.md`](docs/PLAN.md) — Plan de implementación por fases, contrato de API, riesgos
- [`docs/DECISIONES.md`](docs/DECISIONES.md) — ADRs (architectural decision records)

## Decisiones rápidas

- **Por qué WhisperX y no Canary-1B-v2** (que tiene mejor WER en español): WhisperX es framework completo (transcripción + alignment + diarización integradas). Canary requeriría 4-6 días de glue code para conectar con pyannote. Se valida en Fase 4 con audio real si vale la migración.
- **Por qué no NVIDIA Sortformer** para diarización: la doc oficial admite "reduced performance on non-English speech". Para reuniones en rioplatense pyannote 3.1 es mejor opción.
- **Por qué no `groxaxo/parakeet-tdt-0.6b-v3-fastapi-openai`** (un wrapper FastAPI ya hecho): no soporta diarización. Se usa como referencia de estructura del proyecto, no como base.

## Restricciones

- Solo procesamiento batch (no live transcription). El modelo no entra en VRAM con baja latencia para streaming.
- 1 request a la vez en la primera versión (lock global). Cola con workers se agregará si el volumen lo justifica.
- Idioma: español por default. Otros idiomas funcionan pero no se garantiza calidad.

## Knowledge graph del wiki (graphify)

Este proyecto incluye un knowledge graph del wiki SDD generado con [graphify](https://github.com/safishamsi/graphifyy). El grafo vive en `graphify-out/graph.json` (163 nodos, 200 edges, 21 communities) y reduce ~18× los tokens necesarios para responder preguntas sobre el wiki.

### MCP Server (Claude Code)

El archivo `.mcp.json` en la raíz del proyecto expone graphify como MCP server, así Claude Code puede consultar el grafo directamente sin tener que leer archivos completos.

**Setup en una máquina nueva:**

```bash
# 1. Instalar graphify en el Python que esté en PATH
pip install graphifyy

# 2. (Si no existe el grafo) generarlo desde el wiki
cd transcription-api
python3 -c "import graphify"  # verificar import
# usar /graphify desde Claude Code o ejecutar el pipeline manual

# 3. Iniciar Claude Code desde la raíz del proyecto (NO desde el directorio padre)
cd transcription-api
claude
```

**Tools que expone el server una vez aprobado en Claude Code:**

| Tool | Uso |
|---|---|
| `query_graph` | Búsqueda semántica sobre el grafo |
| `god_nodes` | Top conectores (centrales del proyecto) |
| `get_node` | Detalle de un nodo específico |
| `get_neighbors` | Vecinos directos de un nodo |
| `get_community` | Todos los nodos de una community temática |
| `shortest_path` | Path mínimo entre dos conceptos |
| `graph_stats` | Stats globales |

**Notas de portabilidad:**

- `command: "python3"` y path relativo `graphify-out/graph.json` están pensados para que el `.mcp.json` funcione sin edición en cualquier máquina del equipo.
- El cwd con que Claude Code lanza el MCP server es la raíz del proyecto (donde está `.mcp.json`). Por eso es importante iniciar `claude` desde `transcription-api/` y no desde un directorio padre.
- Prerequisito en cada máquina: `python3` en PATH debe tener `graphifyy` instalado. Verificable con `python3 -c "import graphify"`.

## Ver también

- HU origen en Notion: [Investigar solución cloud para transcripción diarizada de reuniones (AssemblyAI)](https://app.notion.com/p/32e88ac892b681e5aaa2dd6e611bb2a8)
