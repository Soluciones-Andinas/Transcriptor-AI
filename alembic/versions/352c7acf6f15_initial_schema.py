"""initial schema

Revision ID: 352c7acf6f15
Revises: 
Create Date: 2026-05-04 16:01:27.432363

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '352c7acf6f15'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema — initial Capa 1 schema per wiki/05_modelo_datos.md §2."""
    op.create_table('users',
    sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('microsoft_oid', sa.Uuid(), nullable=False),
    sa.Column('email', sa.Text(), nullable=False),
    sa.Column('display_name', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_login_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users')),
    sa.UniqueConstraint('email', name=op.f('uq_users_email')),
    sa.UniqueConstraint('microsoft_oid', name=op.f('uq_users_microsoft_oid'))
    )
    op.create_table('mcp_bearers',
    sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('token_hash', sa.Text(), nullable=False),
    sa.Column('name', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_mcp_bearers_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_mcp_bearers')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_mcp_bearers_token_hash'))
    )
    op.create_index(op.f('idx_mcp_bearers_user_id'), 'mcp_bearers', ['user_id'], unique=False)
    op.create_table('oauth_tokens',
    sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('ms_access_token_encrypted', sa.LargeBinary(), nullable=False),
    sa.Column('ms_refresh_token_encrypted', sa.LargeBinary(), nullable=False),
    sa.Column('ms_access_expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_oauth_tokens_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_oauth_tokens')),
    sa.UniqueConstraint('user_id', name=op.f('uq_oauth_tokens_user_id'))
    )
    op.create_table('transcriptions',
    sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('audio_hash', sa.Text(), nullable=False),
    sa.Column('original_filename', sa.Text(), nullable=False),
    sa.Column('original_size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('duration_seconds', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('language', sa.Text(), nullable=False),
    sa.Column('num_speakers', sa.Integer(), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('segments', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_transcriptions_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_transcriptions'))
    )
    op.create_index(op.f('idx_transcriptions_audio_hash'), 'transcriptions', ['audio_hash'], unique=False)
    op.create_table('images',
    sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('transcription_id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('filename', sa.Text(), nullable=False),
    sa.Column('caption', sa.Text(), nullable=True),
    sa.Column('mime_type', sa.Text(), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('file_path', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['transcription_id'], ['transcriptions.id'], name=op.f('fk_images_transcription_id_transcriptions'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_images_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_images'))
    )
    op.create_index(op.f('idx_images_transcription_id'), 'images', ['transcription_id'], unique=False)
    op.create_index(op.f('idx_images_user_id'), 'images', ['user_id'], unique=False)
    op.create_table('upload_sessions',
    sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('bearer_id', sa.Uuid(), nullable=False),
    sa.Column('nonce', sa.Text(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('transcription_id', sa.Uuid(), nullable=True),
    sa.Column('expected_size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('expected_mime_type', sa.Text(), nullable=True),
    sa.Column('status', sa.Text(), server_default=sa.text("'requested'"), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('uploaded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['bearer_id'], ['mcp_bearers.id'], name=op.f('fk_upload_sessions_bearer_id_mcp_bearers')),
    sa.ForeignKeyConstraint(['transcription_id'], ['transcriptions.id'], name=op.f('fk_upload_sessions_transcription_id_transcriptions')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_upload_sessions_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_upload_sessions')),
    sa.UniqueConstraint('nonce', name=op.f('uq_upload_sessions_nonce'))
    )
    op.create_index(op.f('idx_upload_sessions_user_id'), 'upload_sessions', ['user_id'], unique=False)

    # ------------------------------------------------------------------
    # Indexes that Alembic autogenerate cannot infer
    # (wiki/05_modelo_datos.md §2 — functional GIN, partial UNIQUE,
    # composite-with-DESC, composite for cleanup). All wrapped in
    # `IF NOT EXISTS` for idempotency on retried boots (review H-5).
    # ------------------------------------------------------------------

    # Composite per-user "list my recent meetings" — partial on deleted_at IS NULL.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_transcriptions_user_created "
        "ON transcriptions (user_id, created_at DESC) "
        "WHERE deleted_at IS NULL"
    )

    # GIN spanish FTS — H-8: partial on deleted_at IS NULL so the planner can
    # combine with the per-user partial without rechecking tombstoned rows.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_transcriptions_text_fts "
        "ON transcriptions USING gin (to_tsvector('spanish', text)) "
        "WHERE deleted_at IS NULL"
    )

    # At-most-one active bearer per user (RF-AUTH-07).
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_mcp_bearers_active_per_user "
        "ON mcp_bearers (user_id) WHERE revoked_at IS NULL"
    )

    # Cleanup job index for upload_sessions.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_upload_sessions_status_expires "
        "ON upload_sessions (status, expires_at)"
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Drop manually-added indexes first (reverse creation order).
    op.execute("DROP INDEX IF EXISTS idx_upload_sessions_status_expires")
    op.execute("DROP INDEX IF EXISTS uq_mcp_bearers_active_per_user")
    op.execute("DROP INDEX IF EXISTS idx_transcriptions_text_fts")
    op.execute("DROP INDEX IF EXISTS idx_transcriptions_user_created")

    op.drop_index(op.f('idx_upload_sessions_user_id'), table_name='upload_sessions')
    op.drop_table('upload_sessions')
    op.drop_index(op.f('idx_images_user_id'), table_name='images')
    op.drop_index(op.f('idx_images_transcription_id'), table_name='images')
    op.drop_table('images')
    op.drop_index(op.f('idx_transcriptions_audio_hash'), table_name='transcriptions')
    op.drop_table('transcriptions')
    op.drop_table('oauth_tokens')
    op.drop_index(op.f('idx_mcp_bearers_user_id'), table_name='mcp_bearers')
    op.drop_table('mcp_bearers')
    op.drop_table('users')
