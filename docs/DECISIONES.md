# Architectural Decision Records (ADRs)

Decisiones técnicas tomadas durante la investigación y diseño del proyecto. Cada decisión se mantiene aunque después se cambie — para revertirla se agrega un ADR nuevo que la supersede.

---

## ADR-001 — Stack STT: WhisperX (Whisper large-v3) sobre Canary-1B-v2

**Fecha**: 2026-04-30
**Estado**: Aceptada

### Contexto

NVIDIA Canary-1B-v2 (agosto 2025) tiene WER 2,90 % en español sobre Fleurs vs 3-6 % de Whisper large-v3 — es objetivamente mejor en el benchmark. Sin embargo, Canary no tiene framework integrador con diarización.

### Decisión

Arrancar el MVP con WhisperX. Diferir Canary a Fase 2 con validación empírica.

### Razones

1. WhisperX trae diarización + forced alignment integrados. Canary + pyannote requiere 4-6 días de glue code.
2. La diferencia 2,90 % vs 3-6 % es en benchmark sintético (Fleurs). En audio real con jerga técnica argentina y ruido de fondo de oficina, la diferencia puede ser menor.
3. Para captura de requerimientos vía Cowork4Teams, un WER de 5 % es perfectamente usable — el LLM corrige errores menores por contexto.
4. WhisperX tiene comunidad enorme; troubleshooting más rápido en early-stage.

### Consecuencias

- Avanzamos rápido al MVP funcional.
- Quedamos con peor WER teórico que la alternativa state-of-the-art.
- En Fase 4 medimos WER sobre audio propio. Si excede 8 %, evaluamos migración a Canary.

---

## ADR-002 — Diarización: pyannote 3.1 sobre NVIDIA Sortformer

**Fecha**: 2026-04-30
**Estado**: Aceptada

### Contexto

NVIDIA liberó Streaming Sortformer (agosto 2025), un modelo de diarización end-to-end de 117 M parámetros. Tiene DER 10,15 % en CALLHOME (2-6 hablantes), comparable o mejor que pyannote 3.1.

### Decisión

Usar pyannote 3.1.

### Razones

1. La documentación oficial de Sortformer explicita: "English-focused training; reduced performance on non-English speech".
2. Nuestras reuniones son 100 % en español rioplatense.
3. pyannote 3.1 es multilingüe por diseño; entrenado con datasets que incluyen español.
4. Sortformer está limitado a 4 speakers (degrada a partir de 5); reuniones de equipo pueden tener más.
5. pyannote 3.1 tiene 3+ años de batalla en producción; Sortformer es nuevo.

### Consecuencias

- Aceptamos DER 11-19 % en lugar de potencial 10 % (que tampoco está garantizado en español).
- Evitamos riesgo de under-performance no detectado.
- Reevaluación: cuando NVIDIA libere variante multilingüe de Sortformer (probable en 2026).

---

## ADR-003 — API síncrona sin queue ni callbacks

**Fecha**: 2026-04-30
**Estado**: Aceptada

### Contexto

Un MP4 de 1 h tarda ~10-12 min en procesarse en una RTX 16 GB. Las opciones son:

A. Síncrona: el cliente espera la respuesta HTTP completa.
B. Asíncrona con job ID: cliente sube → recibe ID → poll/webhook para resultado.

### Decisión

Síncrona.

### Razones

1. Volumen esperado: ~20 reuniones/mes ≈ 1 cada 2-3 días. Concurrencia es ~0.
2. Cliente confirmó: "no nos interesa hacer la llamada después, darle el ID y demás". Quiere simpleza.
3. El cliente puede ejecutar el upload en background con curl & y revisar el JSON cuando termine.
4. Evita complejidad operativa: no Redis, no workers separados, no estado persistente.

### Consecuencias

- Cliente debe tolerar request HTTP largo (timeout configurable).
- Si volumen sube a >5 reuniones/día, hay que migrar a async. ADR nuevo en ese momento.
- Lock global: solo 1 request a la vez en Fase 1. Segundo request recibe 503.

---

## ADR-004 — No incluir generación de minutas en la API

**Fecha**: 2026-04-30
**Estado**: Aceptada

### Contexto

La HU original imaginaba un pipeline completo: audio → transcript → minuta IA generada. El cliente decidió separar: la API solo entrega el transcript diarizado y las minutas se hacen manualmente con Cowork4Teams + capturas.

### Decisión

API mono-propósito: audio → transcript diarizado JSON. Out of scope: minutas, action items, sentiment, traducciones.

### Razones

1. Single Responsibility Principle: API simple es API mantenible.
2. La calidad de minutas depende fuertemente del prompt + capturas + contexto humano. Automatizarla sin contexto produce minutas mediocres.
3. Cowork4Teams ya está pago y disponible para el equipo; reutiliza la subscripción.
4. Mantiene los datos sensibles (transcripts crudos) en la intranet; solo el contenido editado por humanos sale a Cowork4Teams.

### Consecuencias

- La API no requiere integración con Claude/OpenAI/Gemini.
- Cero costos recurrentes de API LLM.
- Cliente tiene control total sobre el output final.

---

## ADR-005 — Whisper large-v3 sobre large-v2

**Fecha**: 2026-04-30
**Estado**: Aceptada

### Contexto

WhisperX corre con large-v2 en <8 GB VRAM. Large-v3 requiere ~10-12 GB con diarización.

### Decisión

Usar large-v3.

### Razones

1. Tenemos 16 GB VRAM disponible — sobra margen.
2. Large-v3 mejora ~10-15 % WER en idiomas no-inglés vs large-v2.
3. La diferencia se nota en términos técnicos del dominio (programación, infra, requerimientos) que abundan en nuestras reuniones.

### Consecuencias

- Concurrencia limitada a 1 request a la vez (compatible con ADR-003).
- Si en el futuro hace falta concurrencia, se evalúa bajar a large-v2 o `compute_type=int8`.

---

## ADR-006 — Docker como mecanismo de deployment

**Fecha**: 2026-04-30
**Estado**: Aceptada

### Contexto

Hay 3 opciones para correr el servicio: Docker, virtualenv directo en el rig, conda environment.

### Decisión

Docker con nvidia-container-toolkit.

### Razones

1. CUDA + cuDNN + PyTorch + ffmpeg + system deps: la combinación es delicada. Una imagen reproducible elimina "works on my machine".
2. Si el rig se reinstala o se cambia la GPU, `docker compose up` reconstruye todo.
3. Aislamiento: el servicio no contamina el sistema host con dependencias Python.
4. Volume mount para modelos: se descargan una vez (~10 GB total) y persisten entre rebuilds.

### Consecuencias

- Requiere instalar nvidia-container-toolkit en el rig (one-time).
- Imagen pesada (~8 GB con CUDA base + modelos pre-descargados).
- Build time ~10-15 min la primera vez.
