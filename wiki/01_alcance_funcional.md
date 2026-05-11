# Alcance Funcional — `transcription-api`

## 1. Objetivo y Propuesta de Valor

El sistema transcribe y diariza en español, sobre infraestructura propia, archivos de audio o video provenientes de reuniones del equipo de Soluciones Andinas (Microsoft Teams o presenciales grabadas). Cada usuario, autenticado contra Microsoft Entra ID, conecta su Claude personal (Claude Code o Claude Desktop) al servidor MCP del backend; desde ese momento, le pasa archivos a su Claude y este coordina la transcripción, persiste el resultado en el backend, y genera la minuta dentro de su contexto de trabajo. El alcance operativo va desde la autenticación del usuario, pasando por el upload del archivo, la transcripción y diarización, hasta la entrega del transcript estructurado vía MCP para que la minuta se genere en el Claude del user (no en el backend).

Criterios de éxito medibles:

- WER (Word Error Rate) ≤ 8 % en español rioplatense sobre audio limpio.
- DER (Diarization Error Rate) ≤ 25 % para reuniones de 2 a 4 hablantes.
- Latencia de procesamiento ≤ 12 minutos por hora de audio en NVIDIA RTX 4060 Ti 8 GB VRAM (Whisper large-v3 cuantizado int8_float16 + pyannote 3.1).
- Re-uploads del mismo archivo dentro de las 24 horas devuelven en menos de 5 segundos (cache hit).
- Onboarding de un usuario nuevo (login + configurar MCP + primer transcript) en menos de 10 minutos.

**Valor Agregado:**

- Datos sensibles de reuniones (decisiones técnicas, costos, información de clientes) nunca abandonan la intranet privada salvo que el usuario lo decida explícitamente al pedirle a su Claude que use el MCP.
- Cero costo recurrente de LLM para minutas: se generan en el Claude del usuario (ya pago como herramienta de trabajo), no en el backend.
- Identidad corporativa reutilizada vía Microsoft Entra SSO; cero gestión de passwords.
- Cada usuario controla su histórico: ve sus propias transcripciones, las borra cuando quiere, y decide qué imágenes adjuntar para enriquecer la minuta.
- Independencia de proveedores cloud para el pipeline: control total sobre modelos, versiones y formato; no hay riesgo de discontinuidad de servicio o cambio unilateral de pricing en el camino crítico.

## 2. Componentes del Sistema

**A. Interfaz Web (UI mínima de onboarding)** — *Pendiente Capa 5 (2026-05-11)*

Página web mínima para login y configuración del MCP. No es una app completa: el grueso de la interacción ocurre en el Claude del usuario.

- Pantalla de login con redirect a Microsoft SSO.
- Pantalla `/mcp-setup` que muestra al usuario logueado la URL del MCP server y un bearer token personal con instrucciones de cómo configurarlo en Claude Code y Claude Desktop.
- (Opcional, fase posterior) Listado del histórico propio de transcripciones con borrar/exportar.

> El bundle React + Vite + Tailwind del componente A no está implementado en Capas 1-4. El backend ya expone `/auth/login`, `/auth/me`, `/auth/regenerate-mcp-token` (Capa 2) que serán los endpoints de soporte. Ver RF-UI-01 / RF-UI-02 con status `Pendiente Capa 5`.

**B. Servicio de Autenticación**

Implementa el flow OAuth 2.0 / OIDC contra el tenant Microsoft Entra ID de Sandinas y emite bearer tokens vinculados a la identidad del usuario para uso en el MCP server.

- `GET /auth/login` redirige al consent screen de Microsoft.
- `GET /auth/callback` recibe el code, intercambia por tokens, crea/actualiza el usuario en Postgres y emite cookie de sesión.
- `POST /auth/regenerate-mcp-token` revoca el bearer anterior y emite uno nuevo.
- `POST /auth/logout` cierra la sesión web.

**C. Servidor MCP**

Superficie principal de integración. Expone tools y resources para que el Claude del usuario gestione transcripciones, imágenes y consultas. Toda operación opera bajo la identidad del bearer token, con scoping per-user estricto.

- Tools de lifecycle: `request_upload_url`, `start_transcription`, `get_transcription`, `list_my_transcriptions`, `search_my_transcriptions`, `request_image_upload_url`, `attach_image`, `delete_transcription`, `get_user_info`.
- Resources browseables: `transcription://<id>`, `transcription://<id>/images/<image_id>`, `user://me/transcriptions`.
- Transport: streamable HTTP con OAuth bearer.

**D. Endpoints REST mínimos para blobs**

Canal HTTP exclusivo para uploads binarios (audio e imágenes), porque MCP no es eficiente para transferencia de archivos grandes.

- `POST /api/upload` recibe audio multipart, devuelve `upload_id`.
- `POST /api/upload-image` recibe imagen multipart, devuelve `image_id`.
- `GET /api/health` reporta liveness, GPU, VRAM, DB reachable.

**E. Normalizador de Audio**

Convierte cualquier archivo de entrada al formato canónico requerido por los modelos de IA.

- Extrae la pista de audio principal cuando la entrada es video.
- Convierte a WAV mono 16 kHz 16-bit PCM.
- Maneja múltiples codecs (AAC, MP3, Opus, FLAC).
- Genera hash SHA-256 determinístico del audio normalizado, clave del caché efímero.

**F. Caché Temporal en Filesystem (efímero, 24 h)**

Almacena resultados de transcripción durante 24 horas por defecto, para idempotencia del pipeline. Indexado por hash del audio normalizado.

- Persistencia en disco local sin base de datos.
- Estructura: directorio por hash con `transcription.json` y `meta.json`.
- Cleanup automático en background.

**G. Persistencia Relacional (PostgreSQL)**

Almacena los datos persistentes con identidad de usuario.

- `users`: identidades de Sandinas, vinculadas al `oid` de Entra.
- `oauth_tokens`: tokens encriptados (refresh y access) y bearers MCP.
- `transcriptions`: histórico permanente con texto, segmentos JSONB, audio_hash, owner.
- `images`: metadatos de imágenes asociadas (binarios en filesystem).
- `upload_sessions`: nonces y bearers temporales de upload.

**H. Motor de Transcripción**

Genera el texto en español con timestamps por palabra y segmento, usando Whisper large-v3 vía WhisperX.

**I. Motor de Diarización**

Identifica hablantes con pyannote 3.1, asigna etiquetas anónimas (`SPEAKER_00`, `SPEAKER_01`).

**J. Ensamblador de Resultado**

Asocia palabras a hablantes por intersección temporal, produce JSON final con segments + words + speakers + metadata.

**K. Cleanup Job**

Background task que purga el caché efímero vencido.

## 3. Funcionalidad y Flujos de Trabajo

- **Entrada (autenticación)**: el usuario abre la UI, hace login con su cuenta corporativa Microsoft. La UI le muestra cómo configurar el MCP server en su Claude Code o Desktop.
- **Entrada (transcripción)**: el usuario en su Claude le pasa un MP4/MP3/WAV/M4A/FLAC. Claude usa la tool MCP `request_upload_url`, sube el archivo vía REST con su bearer, y dispara `start_transcription`.
- **Procesamiento**: el componente E normaliza y hashea; F verifica el caché efímero; si hay miss, H e I procesan en GPU; J ensambla; F persiste el caché y G persiste el histórico vinculado al user.
- **Salida**: el componente C entrega el JSON al Claude del usuario vía MCP. Claude usa el transcript (y opcionalmente imágenes asociadas) para generar la minuta dentro de su contexto.
- **Retroalimentación**: los logs alimentan al operador; el caché efímero (F) sirve re-uploads idempotentes en 24 h; el histórico (G) permite al usuario ver y buscar sus transcripciones pasadas; el cleanup (K) mantiene el caché bajo control.

## 4. Actores y Responsabilidades

### Actores

| Actor | Tipo | Alcance | Descripción |
|---|---|---|---|
| Usuario Sandinas | Humano | Local (red interna y máquinas personales) | Miembro técnico del equipo (analista, dev, líder técnico). Se autentica vía MS SSO, conecta su Claude al MCP, le pasa audios a su Claude. |
| Claude del Usuario | Sistema externo | Aplicación local (Claude Code en máquina del user, Claude Desktop) | Cliente MCP que el usuario opera. Llama tools, lee resources, genera minutas en su contexto. |
| Microsoft Entra ID | Sistema externo | Cloud (tenant Sandinas) | Identity Provider corporativo. Emite tokens OIDC. |
| Operador del Rig | Humano | Local (administrador del rig) | Mantiene drivers GPU, modelos, contenedores y backups de Postgres. |
| IT / Administración Sandinas | Humano | Tenant Microsoft | Registra la app en Azure Portal, concede permisos de read básico, gestiona membership del tenant. |
| Cleanup Job | Sistema | Interno al servicio | Tarea de fondo que purga caché vencido. |

### Actividades por Actor

**USUARIO SANDINAS**

- Autenticarse con su cuenta corporativa Microsoft.
- Configurar el MCP server en su Claude Code o Claude Desktop (copy-paste de credenciales).
- Pasarle archivos de audio o video a su Claude.
- Adjuntar capturas de pantalla durante o después de la reunión vía Claude (`request_image_upload_url`).
- Pedirle a su Claude que genere la minuta usando el transcript y las imágenes.
- Buscar y consultar su histórico de transcripciones.
- Borrar transcripciones que ya no quiera retener.

**CLAUDE DEL USUARIO**

- Llamar tools MCP del backend bajo la identidad del bearer del usuario.
- Subir archivos al endpoint REST `POST /api/upload` cuando se le pide transcribir.
- Componer minutas usando el transcript JSON + imágenes asociadas + contexto de la conversación con el user.

**OPERADOR DEL RIG**

- Levantar, reiniciar y monitorear el contenedor Docker del servicio.
- Mantener drivers NVIDIA y nvidia-container-toolkit actualizados.
- Renovar el HF token cuando expire.
- Hacer backup periódico de Postgres (`pg_dump`).
- Revisar logs ante incidentes.
- Aplicar actualizaciones de modelos.

**IT / ADMINISTRACIÓN SANDINAS**

- Registrar la app de transcription-api en Azure Portal (`redirect_uri`, scopes).
- Conceder admin consent si aplica.
- Mantener al equipo en el tenant (off-boarding automático cuando alguien se va).

**CLEANUP JOB**

- Escanear el caché efímero cada hora.
- Eliminar entradas con `now - created_at > TTL`.
- Loguear cantidad de entradas purgadas.

## 5. Funcionalidades (Alcance MVP)

- **Login Microsoft SSO** (Autenticación): permite al Usuario Sandinas autenticarse con su cuenta corporativa. Servido por la UI mínima + `/auth/*` endpoints.

- **Configuración del MCP Server** (Visualización + Configuración): tras el login, el usuario obtiene la URL del MCP y un bearer token personal en `/mcp-setup`, con instrucciones para Claude Code (`.mcp.json`) y Claude Desktop (`claude_desktop_config.json`). Botón para regenerar bearer.

- **Transcripción + Diarización via Claude** (Procesamiento): el usuario, desde su Claude, le pasa un archivo. Claude llama `request_upload_url`, sube por REST, llama `start_transcription`, recibe `transcription_id`, y luego `get_transcription` para el JSON. El backend persiste en Postgres asociado al user.

- **Caché de Resultados (24 h)** (Idempotencia): re-procesar el mismo audio en menos de 24 h devuelve en menos de 5 s sin recomputar. El histórico de Postgres siempre se actualiza con el resultado.

- **Histórico Persistente y Búsqueda** (Visualización): cada usuario consulta vía MCP `list_my_transcriptions` y `search_my_transcriptions` su propio histórico. El histórico no expira (retención hasta borrado manual del user).

- **Adjuntar Imágenes a Transcripciones** (ABM parcial): el usuario sube capturas vía Claude (`request_image_upload_url` + `POST /api/upload-image` + `attach_image`), las imágenes quedan asociadas a una transcripción y disponibles como resources MCP para que Claude las use al generar la minuta.

- **Generación de Minutas en Claude** (fuera del backend): el usuario pide a su Claude "armá la minuta de la reunión X". Claude consume tools/resources MCP para traer transcript + imágenes y genera la minuta en su contexto. Backend NO genera minutas.

- **Borrar Transcripciones** (ABM): el usuario borra transcripciones propias vía `delete_transcription` desde Claude o desde la UI futura.

- **Verificación de Salud** (Visualización): `GET /api/health` reporta GPU disponible, VRAM, DB reachable. Útil para el operador.

- **Limpieza Automática de Caché Efímero** (Sistema): purga periódica del caché 24 h sin intervención.

## 6. Fuera de Alcance / Evolución Futura

| Item | Motivo de exclusión | Horizonte estimado |
|---|---|---|
| Generación automática de minutas en el backend | Se hace en el Claude del user (ADR-012); cero costo y mejor privacy. | No previsto |
| Live transcription (streaming) | Modelo large-v3 no entra en VRAM con baja latencia; caso de uso no requerido. | Largo plazo |
| Soporte para clientes MCP que no sean Claude (Cursor, Goose, etc.) | El equipo usa Claude; otros clientes pueden funcionar pero no se garantiza. | Cuando aparezca demanda |
| UI con upload de audio | El path principal es Claude; la UI tiene scope acotado a onboarding. | Reevaluación post-MVP |
| Object storage externo (S3, MinIO) para uploads | Filesystem local alcanza para el volumen actual; ADR-013 contempla migración futura. | Cuando volumen lo justifique |
| Multi-GPU / clustering | 1 GPU sirve para el volumen esperado. | Largo plazo |
| Integración nativa con Microsoft Teams (`.vtt` direct ingest) | Cliente prefiere flujo via Claude; Teams transcript se procesa como cualquier audio. | No previsto |
| Análisis posterior (sentiment, action items automáticos en backend) | Eso lo hace Claude del user. | No previsto |
| Multi-tenancy con tenants externos | Solo tenant Sandinas. | No previsto |

## 7. Restricciones

| Tipo | Restricción | Impacto en alcance |
|---|---|---|
| Técnica | GPU única NVIDIA RTX 4060 Ti con 8 GB VRAM en el rig propio. | Limita concurrencia a 1 request por vez (ADR-005); large-v3 cuantizado int8_float16 + pyannote ocupan ~7-8 GB; cuantización es obligatoria por VRAM (ADR-001). |
| Técnica | Modelos open-source con licencia compatible con uso comercial. | Excluye Whisper Turbo de OpenAI; fija WhisperX (BSD-2), pyannote (MIT), modelos NeMo (CC-BY-4.0) como aceptables. |
| Técnica | Idioma español rioplatense. | Excluye modelos optimizados solo para inglés (Sortformer); requiere validación empírica de WER en audio propio. |
| Técnica | Cliente principal es Claude Code o Claude Desktop. | El proyecto no testea con otros clientes MCP. |
| Tiempo | MVP en 9-12 días de trabajo efectivo. | Justifica alcance acotado de UI y MCP-first sin features secundarias. |
| Regulatoria | Ley argentina 25.326 de Protección de Datos Personales: consentimiento de participantes para grabar reuniones. | Fuera del alcance técnico del backend; requisito operativo del usuario. |
| Organizacional | Single admin para el rig (sin equipo SRE). | Justifica deployment con Docker Compose simple, sin Kubernetes. |
| Organizacional | Dependencia del tenant Microsoft Entra de Sandinas. | Outage del tenant impide login; aceptable para uso interno. |
| Presupuesto | Costo recurrente objetivo cercano a cero. | Excluye servicios cloud de transcripción y suscripciones SaaS de meeting bots. |

## 8. Tecnología

### Aplicaciones

- **Backend / API + MCP Server**: Python 3.10–3.11 + FastAPI 0.115 + Uvicorn 0.32 + `mcp` SDK Anthropic.
- **Frontend**: React 18 + Vite 5 + Tailwind CSS 3 (UI mínima, 2-3 páginas, servida por FastAPI StaticFiles).
- **Pipeline IA**: WhisperX 3.8.5 (envuelve faster-whisper + pyannote-audio + alignment), Whisper large-v3, pyannote 3.1.
- **Audio**: ffmpeg 6.x.
- **Cliente del usuario**: Claude Code o Claude Desktop con custom MCP connector configurado.

### Persistencia

- **PostgreSQL 16** para datos persistentes con identidad (users, transcriptions, tokens, images, sessions).
- **Filesystem local** para caché efímero de pipeline (`/data/cache/`), modelos pre-descargados (`/data/models/`), uploads temporales (`/data/uploads/`), blobs de imágenes (`/data/blobs/`).

### Identidad

- **Microsoft Entra ID** (OAuth 2.0 / OIDC) como Identity Provider único.
- Librería: `authlib` o `msal` (Python).

### Software Cliente

- Browser moderno (Chrome, Firefox, Edge) para la UI de onboarding.
- Claude Code o Claude Desktop instalado en la máquina del usuario.

## 9. Infraestructura y Despliegue

- **Modelo de despliegue**: on-premise puro. Sin componentes cloud salvo Microsoft Entra (auth) y HuggingFace (descarga one-time de modelos).
- **Plataforma objetivo**: rig Linux (Ubuntu 22.04 LTS recomendado) con GPU NVIDIA, Docker 24+ con nvidia-container-toolkit y `docker compose` v2.20+.
- **Containers**: dos servicios en `docker-compose.yml`:
  - `transcription-api`: la app FastAPI con GPU pass-through.
  - `postgres`: PostgreSQL 16 con volumen persistente.
- **Escalabilidad**: vertical sobre la GPU única; sin escalado horizontal en MVP.
- **Requisitos mínimos del rig**:
  - GPU NVIDIA con ≥ 8 GB VRAM (rig actual: RTX 4060 Ti 8 GB; con menos VRAM no entra Whisper large-v3 int8_float16 + pyannote), drivers ≥ 535, CUDA 12.1+.
  - 32 GB RAM recomendado.
  - 100 GB de disco libre (modelos ~10 GB + cache + Postgres + uploads + blobs + sistema).
  - Conectividad intranet en puerto 8000.
  - Conectividad outbound a `login.microsoftonline.com` y `huggingface.co`.
- **CI/CD**: fuera de alcance MVP. Deployment manual mediante `docker compose up -d` desde Git pull.

## 10. Supuestos y Criterios de Aceptación

### Supuestos

| # | Supuesto | Impacto si es falso |
|---|---|---|
| 1 | Volumen de procesamiento ≤ 5 reuniones/día con duración promedio ≤ 1 h. | Si excede, hay que evaluar cola con workers; lock global se vuelve cuello de botella. |
| 2 | Disco del rig tiene al menos 100 GB libres. | Si no, fallan uploads, Postgres bloquea o el caché se llena. |
| 3 | HuggingFace aprueba el acceso a `pyannote/speaker-diarization-3.1` con el token de Sandinas. | Hay que migrar a alternativa de diarización. |
| 4 | Sandinas tiene tenant Microsoft Entra activo y permisos para registrar app. | Sin esto no hay login; bloqueante. |
| 5 | Cada usuario del proyecto tiene Claude Code o Claude Desktop instalado. | Sin esto, el MCP no se usa; el user puede consumir el JSON manualmente desde la UI futura. |
| 6 | Audio rioplatense de calidad razonable (SNR > 10 dB). | WER y DER se degradan más allá de los criterios. |
| 7 | Reuniones con 2 a 8 hablantes. | Más de 8 degrada significativamente pyannote 3.1. |

### Criterios de Aceptación de Alto Nivel

| # | Criterio | Verificación |
|---|---|---|
| 1 | Un usuario nuevo de Sandinas hace login con MS SSO y obtiene su config MCP en menos de 1 minuto. | Test E2E en `tests/e2e/`. |
| 2 | Un usuario configura su Claude con la config provista y logra transcribir un MP4 de prueba. | Smoke test manual. |
| 3 | El servicio acepta MP4/MP3/WAV/M4A/FLAC y devuelve JSON con segmentos diarizados vía MCP. | Test E2E con cada formato. |
| 4 | WER ≤ 8 % sobre 5 reuniones reales del equipo en español rioplatense. | Cálculo manual contra transcripción gold standard. |
| 5 | DER ≤ 25 % en reuniones con 2 a 4 hablantes. | Revisión manual de cambios de hablante. |
| 6 | Re-upload del mismo archivo dentro de 24 h responde en < 5 s. | Test de carga. |
| 7 | Latencia ≤ 12 min para archivo de 1 h de duración. | Benchmark con audio de 1 h. |
| 8 | El servicio (FastAPI + Postgres) reinicia y queda funcional en < 60 s tras `docker compose restart`. | Cronometrado. |
| 9 | El histórico de un usuario es estrictamente per-user: User A jamás ve transcripciones de User B. | Test de seguridad multi-user. |
| 10 | El caché efímero purga automáticamente entradas vencidas. | Test con freezegun. |
| 11 | El bearer regenerado revoca al anterior; el viejo deja de autenticar requests MCP. | Test de auth. |

## 11. Interesados y Aprobación

| Rol | Persona/Equipo | Responsabilidad |
|---|---|---|
| Product Owner | Franco Bertoldi | Define prioridades, aprueba alcance y cambios de alcance. |
| Tech Lead | Franco Bertoldi | Valida viabilidad técnica, decide stack, aprueba ADRs. |
| Operador del Rig | Por definir (equipo IT Sandinas) | Mantiene la infraestructura física y lógica del rig + Postgres. |
| IT / Administración Sandinas | Por definir | Registra app en Azure Portal, gestiona tenant Entra. |
| Stakeholders consumidores | Equipo técnico Sandinas | Usan el servicio para captura de requerimientos en reuniones reales; retroalimentan calidad. |
