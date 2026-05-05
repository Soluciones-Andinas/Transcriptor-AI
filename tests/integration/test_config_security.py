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


# ---------------------------------------------------------------------------
# Capa 2 review CR-2: JWT_SECRET length validator (≥32 chars).
# ---------------------------------------------------------------------------
def test_jwt_secret_validator_rejects_short_secrets():
    """
    Capa 2 review: CR-2.

    JWT_SECRET shorter than 32 characters enables offline brute-force forgery
    if any signed token leaks. The Settings validator must refuse to boot.
    """
    import pytest
    from pydantic import ValidationError

    from transcription_api.config import Settings

    with pytest.raises(ValidationError) as excinfo:
        Settings(
            JWT_SECRET="too-short",
            OAUTH_TOKEN_ENC_KEY=settings.oauth_token_enc_key.get_secret_value(),
            POSTGRES_PASSWORD=settings.postgres_password.get_secret_value(),
        )
    assert "JWT_SECRET" in str(excinfo.value)
    assert "32" in str(excinfo.value)


def test_jwt_secret_validator_accepts_strong_secrets():
    """A 48-char secret (well above the 32-char floor) must validate."""
    from transcription_api.config import Settings

    s = Settings(
        JWT_SECRET="x" * 48,
        OAUTH_TOKEN_ENC_KEY=settings.oauth_token_enc_key.get_secret_value(),
        POSTGRES_PASSWORD=settings.postgres_password.get_secret_value(),
    )
    assert s.jwt_secret.get_secret_value() == "x" * 48


# ---------------------------------------------------------------------------
# Capa 2 review CR-2 (sibling): OAUTH_TOKEN_ENC_KEY decode + length.
# ---------------------------------------------------------------------------
def test_oauth_token_enc_key_validator_rejects_wrong_length():
    """A urlsafe-base64 key that decodes to ≠ 32 bytes must be rejected."""
    import base64

    import pytest
    from pydantic import ValidationError

    from transcription_api.config import Settings

    sixteen_bytes = base64.urlsafe_b64encode(b"\x00" * 16).decode()  # 16, not 32
    with pytest.raises(ValidationError) as excinfo:
        Settings(
            JWT_SECRET=settings.jwt_secret.get_secret_value(),
            OAUTH_TOKEN_ENC_KEY=sixteen_bytes,
            POSTGRES_PASSWORD=settings.postgres_password.get_secret_value(),
        )
    assert "OAUTH_TOKEN_ENC_KEY" in str(excinfo.value)


def test_oauth_token_enc_key_validator_rejects_garbage_base64():
    """A non-base64 string must be rejected with a clear error."""
    import pytest
    from pydantic import ValidationError

    from transcription_api.config import Settings

    with pytest.raises(ValidationError) as excinfo:
        Settings(
            JWT_SECRET=settings.jwt_secret.get_secret_value(),
            OAUTH_TOKEN_ENC_KEY="not!valid@base64$%^",
            POSTGRES_PASSWORD=settings.postgres_password.get_secret_value(),
        )
    assert "OAUTH_TOKEN_ENC_KEY" in str(excinfo.value)


def test_oauth_token_enc_key_validator_accepts_empty():
    """Empty key remains acceptable (auth module unused in Capa 1 deployments)."""
    from transcription_api.config import Settings

    s = Settings(
        JWT_SECRET=settings.jwt_secret.get_secret_value(),
        OAUTH_TOKEN_ENC_KEY="",
        POSTGRES_PASSWORD=settings.postgres_password.get_secret_value(),
    )
    assert s.oauth_token_enc_key.get_secret_value() == ""
