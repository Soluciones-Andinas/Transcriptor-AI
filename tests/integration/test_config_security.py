"""Settings must not leak the Postgres password through diagnostics.

Spec: SPEC-capa1-postgres-orm-v1 (review fix H-6).
Privacy-first per wiki/02_arquitectura.md §0 — credentials must not surface
in `model_dump()`, `repr()`, or any default Pydantic serialization that a
diagnostic logger or middleware might invoke.
"""
from __future__ import annotations

from transcription_api.config import settings


def test_password_not_in_model_dump():
    """
    Review fix H-6: `settings.model_dump()` must not contain the cleartext
    password. SecretStr renders as `**********`.
    """
    cleartext = settings.postgres_password.get_secret_value()
    assert cleartext != ""  # sanity — make sure we actually have one to leak

    dump = settings.model_dump()
    rendered = repr(dump)
    assert cleartext not in rendered, (
        "settings.model_dump() leaks the cleartext Postgres password"
    )


def test_password_not_in_repr():
    """
    Review fix H-6: `repr(settings)` and `str(settings)` must not contain
    the cleartext password.
    """
    cleartext = settings.postgres_password.get_secret_value()
    assert cleartext not in repr(settings)
    assert cleartext not in str(settings)


def test_database_url_builder_includes_password():
    """
    Review fix H-6: `build_database_url()` IS allowed to materialize the
    cleartext (the engine factory needs it). The fix is to keep it OFF the
    settings object as a public attribute.
    """
    cleartext = settings.postgres_password.get_secret_value()
    url = settings.build_database_url()
    assert cleartext in url, (
        "build_database_url() must produce a URL with the cleartext password "
        "for SQLAlchemy"
    )
    assert "postgresql+asyncpg://" in url
