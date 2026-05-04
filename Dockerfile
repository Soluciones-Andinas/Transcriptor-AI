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

# Instalar dependencias Python primero para aprovechar layer cache
COPY pyproject.toml ./
RUN pip install --upgrade pip setuptools wheel \
    && pip install -e .

# Copiar código y configuración de Alembic
COPY src/ ./src/
COPY alembic.ini ./
COPY alembic/ ./alembic/
COPY scripts/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Crear DATA_DIR (el volumen lo monta encima en runtime, pero esto cubre el caso sin volumen)
RUN mkdir -p /data/models /data/cache

EXPOSE 8000

# Init system para limpiar zombies (subprocesses ffmpeg)
STOPSIGNAL SIGTERM

# Capa 1 ALT-1: entrypoint corre alembic upgrade head antes de uvicorn.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
