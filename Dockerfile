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
# Trade-off de layer cache: copiamos src/ junto con pyproject.toml, así un
# cambio de código invalida la layer de install. Para este proyecto con
# deps estables y src/ chico, es aceptable. Si deps crecen, mover a un
# patrón requirements.txt-first.
COPY pyproject.toml ./
COPY src/ ./src/
COPY alembic.ini ./
COPY alembic/ ./alembic/
COPY scripts/entrypoint.sh /usr/local/bin/entrypoint.sh

RUN pip install --upgrade pip setuptools wheel \
    && pip install . \
    && chmod +x /usr/local/bin/entrypoint.sh

# Crear DATA_DIR (el volumen lo monta encima en runtime, pero esto cubre el caso sin volumen)
RUN mkdir -p /data/models /data/cache

EXPOSE 8000

# Init system para limpiar zombies (subprocesses ffmpeg)
STOPSIGNAL SIGTERM

# Capa 1 ALT-1: entrypoint corre alembic upgrade head antes de uvicorn.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
