"""Per-user model classification guard — pre-Capa-4 hardening.

Spec: Capa 4 review-fix follow-up to Batch 5 structural insight.

The original `_scoped_models()` enrolls models that have a `user_id`
column. If a future per-user model lacks that column (e.g. a developer
adds a `Note` model with `owner_id` or forgets to denormalize the FK),
the listener silently classifies it as non-scoped and SELECT/UPDATE/
DELETE return all rows across users — a fail-OPEN at the listener
level even though every tool builds a per-user session.

`_validate_model_classification()` runs at `enable_per_user_scoping()`
startup and asserts every ORM model is EITHER per-user (carries
`user_id`) OR explicitly in `_NON_SCOPED_MODELS` (deliberately global).
A model in neither group raises `ScopingClassificationError` and the
service refuses to start.

These tests use mock mappers so the production registry is not touched.
A separate test exercises the real registry to confirm current state
classifies cleanly.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from transcription_api.db.scoping import (
    ScopingClassificationError,
    _validate_model_classification,
)


def _make_mapper(class_name: str, *, has_user_id: bool) -> MagicMock:
    """Build a mock SQLAlchemy mapper exposing the bits we inspect."""
    mapper = MagicMock()
    mapper.class_ = type(class_name, (), {})
    columns = {"user_id": object()} if has_user_id else {}
    mapper.columns = MagicMock()
    mapper.columns.keys = lambda: columns.keys()
    return mapper


def test_validate_passes_when_all_models_classified():
    """Mixed registry: User in allowlist + Transcription per-user → no raise."""
    mappers = [
        _make_mapper("User", has_user_id=False),
        _make_mapper("Transcription", has_user_id=True),
    ]
    _validate_model_classification(mappers)  # must not raise


def test_validate_passes_when_only_per_user_models():
    """Registry without any non-scoped model still passes."""
    mappers = [
        _make_mapper("Transcription", has_user_id=True),
        _make_mapper("Image", has_user_id=True),
    ]
    _validate_model_classification(mappers)  # must not raise


def test_validate_raises_on_unclassified_model():
    """Note model lacks `user_id` and is not in _NON_SCOPED_MODELS → raise."""
    mappers = [
        _make_mapper("User", has_user_id=False),
        _make_mapper("Note", has_user_id=False),
    ]
    with pytest.raises(ScopingClassificationError) as exc_info:
        _validate_model_classification(mappers)
    msg = str(exc_info.value)
    assert "Note" in msg
    assert "user_id" in msg
    assert "_NON_SCOPED_MODELS" in msg


def test_validate_lists_all_unclassified_models():
    """Multiple offenders → all named in the error for one-shot fix."""
    mappers = [
        _make_mapper("User", has_user_id=False),
        _make_mapper("Note", has_user_id=False),
        _make_mapper("Tag", has_user_id=False),
        _make_mapper("Transcription", has_user_id=True),
    ]
    with pytest.raises(ScopingClassificationError) as exc_info:
        _validate_model_classification(mappers)
    msg = str(exc_info.value)
    assert "Note" in msg
    assert "Tag" in msg


def test_validate_real_registry_classifies_cleanly():
    """Smoke: production registry passes the guard today.

    This is the load-bearing test. If a future PR adds a per-user model
    without `user_id`, this test fails immediately — no need to wait for
    a leaked-data incident in production. If a future PR adds a global
    model (e.g. Config), this test fails with an actionable message
    asking the author to add the class name to `_NON_SCOPED_MODELS`.
    """
    _validate_model_classification()  # uses Base.registry.mappers


def test_enable_per_user_scoping_validates_before_install(monkeypatch):
    """Wire check: `enable_per_user_scoping` invokes the validator.

    A misclassified registry must prevent listener install — otherwise
    the guard is decorative and a startup with a broken model would
    proceed silently.
    """
    from transcription_api.db import scoping

    # Simulate a broken registry by swapping the validator to one that
    # raises on the first call. The wire is correct iff
    # enable_per_user_scoping propagates that error and does NOT mark
    # the listener as installed.
    monkeypatch.setattr(scoping, "_LISTENER_INSTALLED", False)

    def raising_validator(mappers=None):
        raise ScopingClassificationError("simulated broken registry")

    monkeypatch.setattr(
        scoping, "_validate_model_classification", raising_validator
    )

    with pytest.raises(ScopingClassificationError, match="simulated"):
        scoping.enable_per_user_scoping()

    assert scoping._LISTENER_INSTALLED is False, (
        "listener must remain uninstalled when validation fails"
    )
