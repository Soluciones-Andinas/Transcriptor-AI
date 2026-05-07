"""``scoped_session`` fail-closes when ``user_id`` is not armed.

Spec: SPEC-capa4-mcp-v1
Covers (G11 — review-fixes Tier 3):
- ADR-015 fail-closed invariant — a per-user query under a session
  that has neither ``info["user_id"]`` armed nor ``scoping_bypass``
  set MUST raise ``ScopingNotArmedError``. The test guards the
  privacy invariant: the listener never silently runs an unscoped
  SELECT against a per-user model. Today the listener is exercised
  indirectly through every tool test; this is the direct contract.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from transcription_api.db.models import Transcription
from transcription_api.db.scoping import ScopingNotArmedError

pytestmark = pytest.mark.requires_docker


@pytest.mark.asyncio
async def test_unarmed_session_raises_scoping_not_armed_on_per_user_query(
    engine,
):
    """
    Spec: SPEC-capa4-mcp-v1
    Criterion: ADR-015 (G11 review-fix) — Given a fresh AsyncSession
    with no ``info["user_id"]`` armed and no ``scoping_bypass``, When
    the test executes ``select(Transcription)``, Then the listener
    raises ``ScopingNotArmedError`` rather than running the unscoped
    SELECT. The test uses ``engine`` (the conftest engine bound to
    the migrated testcontainer) and a fresh sessionmaker so the
    ``scoping_bypass`` arm that ``conftest.session`` applies for test
    drivers does NOT leak into this scenario.
    """
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as db:
        with pytest.raises(ScopingNotArmedError):
            await db.execute(select(Transcription))
