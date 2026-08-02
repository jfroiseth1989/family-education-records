"""add app_auth and app_sessions

Revision ID: f87a326f960b
Revises: 01ff0f6a9116
Create Date: 2026-08-02 21:12:25.314877

Hand-trimmed after autogenerate: same recurring FTS5 false-positive as
every migration since Phase 2 Step 2 (see docs/PHASE_2_FREEZE.md §3/§8
and every prior migration's docstring) -- document_text_fts/
annotation_notes_fts and their SQLite-managed shadow tables have no
SQLAlchemy model representation, so autogenerate again proposed dropping
them. Removed -- this migration only ever adds two new tables,
`app_auth` and `app_sessions` (Security Phase Step 1, see the AppAuth/
AppSession model docstrings). Both start empty; no existing table,
column, or row is touched.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f87a326f960b'
down_revision: Union[str, None] = '01ff0f6a9116'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'app_auth',
        sa.Column('auth_id', sa.Integer(), nullable=False),
        sa.Column('password_hash', sa.String(length=255), nullable=False),
        sa.Column('recovery_key_hash', sa.String(length=255), nullable=True),
        sa.Column('failed_login_attempts', sa.Integer(), nullable=False),
        sa.Column('locked_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('inactivity_lock_minutes', sa.Integer(), nullable=False),
        sa.Column('session_absolute_expiry_hours', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('password_updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('recovery_key_updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('auth_id'),
    )
    op.create_table(
        'app_sessions',
        sa.Column('session_id', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('last_activity_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('session_id'),
    )


def downgrade() -> None:
    op.drop_table('app_sessions')
    op.drop_table('app_auth')
