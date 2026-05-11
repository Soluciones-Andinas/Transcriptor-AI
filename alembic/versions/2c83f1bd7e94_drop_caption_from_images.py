"""drop caption from images (D-083)

Spec: drift D-083 (`docs/sesiones/2026-05-11-wiki-drift-audit.md`).

The ``caption`` column was originally specified for `attach_image`
(RF-IMG-03) — a tool that was never implemented. In Capa 4 the image
upload flow was unified under `request_upload_url(kind="image")` +
`POST /api/upload-image`, neither of which accepts a caption parameter.
There is no write path: `images.caption` has been NULL for every row
in production since Capa 4 shipped.

Drop is the cheap, honest fix. If/when Capa 5 UI introduces a "describe
this image" feature, a new migration adds the column back with a clear
spec reference.

Revision ID: 2c83f1bd7e94
Revises: 1a4f8c9b2d6e
Create Date: 2026-05-11
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2c83f1bd7e94"
down_revision: str | Sequence[str] | None = "1a4f8c9b2d6e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop ``images.caption`` — feature retired, no rows depend on it."""
    op.drop_column("images", "caption")


def downgrade() -> None:
    """Recreate ``images.caption`` as nullable TEXT (original shape)."""
    op.add_column(
        "images",
        sa.Column("caption", sa.Text(), nullable=True),
    )
