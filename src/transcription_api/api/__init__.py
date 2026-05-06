"""HTTP API surface for the transcription service.

Spec: SPEC-capa3-pipeline-v1
- ``transcriptions`` module hosts POST and GET routes for the
  ``/api/transcriptions`` resource (Batch 6).

Routes register their own router via ``APIRouter`` and are wired into
the FastAPI app in ``main.py``. The auth dependencies live in
``transcription_api.auth.dependencies`` and are imported there, never
re-implemented here (spec §7 contract).
"""
from .transcriptions import router as transcriptions_router

__all__ = ["transcriptions_router"]
