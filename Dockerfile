# Dockerfile — transcription-api
# Base: NVIDIA CUDA 12.1 + cuDNN 8 sobre Ubuntu 22.04 (per ADR-006).
# Single-stage build: la imagen runtime ya pesa ~5 GB por CUDA, multi-stage no aporta.
FROM nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/data/models/huggingface \
    TORCH_HOME=/data/models/torch

# Sistema: Python 3.10 (compat WhisperX), ffmpeg, libsndfile (pyannote), curl (healthcheck), git (pip), postgresql-client (pg_isready en entrypoint)
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.10 \
        python3.10-venv \
        python3-pip \
        ffmpeg \
        libsndfile1 \
        curl \
        git \
        postgresql-client \
        ca-certificates \
    && ln -sf /usr/bin/python3.10 /usr/bin/python \
    && ln -sf /usr/bin/python3.10 /usr/bin/python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Instalación non-editable: el container es immutable post-build (no editamos
# /app/src/* en runtime), así que `pip install .` produce una imagen más
# liviana que `pip install -e .` y evita el bug donde setuptools necesita
# leer src/ en tiempo de install para registrar paquetes.
#
# Layer cache strategy (post-Capa 4 hot-fix): split deps install from src/
# copy so a code-only change rebuilds in seconds instead of re-running the
# 15-25 min torch + whisperx + pyannote install.
#
# 1. COPY pyproject.toml + create a stub src/ tree → setuptools packages.find
#    sees the package and `pip install ".[pipeline]"` resolves all deps.
#    This layer only invalidates when pyproject.toml changes (deps bumped).
# 2. RUN pip install … → caches the heavy install of CUDA torch + whisperx +
#    pyannote into a single immutable layer. ~13 GB, runs once per deps bump.
# 3. COPY src/ + alembic + entrypoint → the layer that DOES change frequently.
# 4. RUN pip install --no-deps --force-reinstall . → re-register the package
#    with the real code, dropping the stub. Fast because deps are already met.
#
# Capa 3 (D-029): single-stage stays. Multi-stage saves ~1-2 GB of compilers
# in a 13 GB image — marginal vs. complexity. Decision priority §4:
# Simplicity > Performance.
COPY pyproject.toml ./
RUN mkdir -p src/transcription_api \
    && echo '__version__ = "0.0.0"' > src/transcription_api/__init__.py

RUN pip install --upgrade pip setuptools wheel \
    && pip install --extra-index-url https://download.pytorch.org/whl/cu121 \
        "torch>=2.1.0" "torchaudio>=2.1.0" \
    && pip install ".[pipeline]"

COPY src/ ./src/
COPY alembic.ini ./
COPY alembic/ ./alembic/
COPY scripts/entrypoint.sh /usr/local/bin/entrypoint.sh

RUN pip install --no-deps --force-reinstall . \
    && chmod +x /usr/local/bin/entrypoint.sh

# Crear DATA_DIR (el volumen lo monta encima en runtime, pero esto cubre el caso sin volumen)
RUN mkdir -p /data/models /data/cache

EXPOSE 8000

# Init system para limpiar zombies (subprocesses ffmpeg)
STOPSIGNAL SIGTERM

# Capa 1 ALT-1: entrypoint corre alembic upgrade head antes de uvicorn.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
