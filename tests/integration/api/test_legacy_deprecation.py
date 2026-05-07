"""Legacy POST /api/transcriptions deprecation tests (Capa 4 AC-16).

Spec: SPEC-capa4-mcp-v1
Covers (Capa 4):
- AC-16 — OpenAPI marks ``POST /api/transcriptions`` with
  ``deprecated: true`` so any SDK / Claude client enumerating tools
  detects the endpoint is on the removal path (Capa 5).

The WARN-log half of AC-16 (``legacy_endpoint_invoked
deprecated_endpoint=POST_/api/transcriptions removal_target=Capa5``) is
exercised in ``test_transcriptions.py`` because it needs the
testcontainers Postgres fixture to seed a bearer + invoke the handler.
The OpenAPI flag is decorator-time only, so this file deliberately does
NOT carry the ``requires_docker`` marker — it gives us local RED proof
on dev machines without Docker.
"""
from __future__ import annotations


def test_post_transcriptions_marked_deprecated_in_openapi():
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: AC-16 — ``app.openapi()["paths"]["/api/transcriptions"]
    ["post"]["deprecated"]`` is ``True``. The endpoint still works
    (D-026: the rig smoke-test depends on it); deprecation is the
    discoverability signal that Capa 5 will remove it.
    """
    from transcription_api.main import app

    schema = app.openapi()
    post_op = schema["paths"]["/api/transcriptions"]["post"]
    assert post_op.get("deprecated") is True, (
        "POST /api/transcriptions must carry deprecated=true per AC-16; "
        f"current op keys: {sorted(post_op.keys())}"
    )
