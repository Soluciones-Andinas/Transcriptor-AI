"""add upload_bearer_hash to upload_sessions

Spec: SPEC-capa4-mcp-v1, AC-15 (D-044-impl).

Adds ``upload_bearer_hash TEXT NOT NULL`` to ``upload_sessions``. The
column stores the ``SHA-256(plaintext).hex()`` of the ephemeral bearer
emitted by ``request_upload_url`` (RF-MCP-01 step 3); ``POST /api/upload``
validates the incoming ``Authorization: Bearer <plaintext>`` against this
hash via ``hmac.compare_digest`` (RF-MCP-03 step 4).

Pre-flight invariant: this migration aborts if ``upload_sessions`` has
existing rows. ADD COLUMN NOT NULL on a populated table requires a
``DEFAULT`` we cannot supply (the value is per-row plaintext-derived);
forcing the operator to clear stale sessions is safer than silently
backfilling random hashes.

Capa 4 ships the table empty in production (Capa 1 created it but no
code path inserted into it pre-Capa-4). The pre-flight is the safety
net for legacy deployments that experimented with manual rows.

Revision ID: 1a4f8c9b2d6e
Revises: 352c7acf6f15
Create Date: 2026-05-06
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1a4f8c9b2d6e"
down_revision: str | Sequence[str] | None = "352c7acf6f15"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema — add upload_bearer_hash column with pre-flight check."""
    bind = op.get_bind()
    count = bind.execute(
        sa.text("SELECT COUNT(*) FROM upload_sessions")
    ).scalar()
    if count and count > 0:
        # Defensive abort. ADD COLUMN NOT NULL on a populated table fails
        # downstream anyway; failing here gives the operator a clear
        # actionable error instead of a generic Postgres NOT NULL
        # violation. See spec §10 (Risks): the case is "casi nula"
        # (Capa 1 left the table empty) but worth guarding.
        raise RuntimeError(
            f"upload_sessions has {count} pre-existing rows; ABORT — "
            "upload_bearer_hash is NOT NULL and the migration cannot "
            "backfill plaintext-derived hashes for existing rows. "
            "Operator must DELETE the rows (or backfill upload_bearer_hash "
            "manually) before re-running `alembic upgrade head`."
        )

    op.add_column(
        "upload_sessions",
        sa.Column("upload_bearer_hash", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema — drop upload_bearer_hash column."""
    op.drop_column("upload_sessions", "upload_bearer_hash")
