"""add ocr_corrections

Revision ID: b3c8a267694b
Revises: 0083629e42e9
Create Date: 2026-08-01 19:02:44.045946

Hand-trimmed after autogenerate: same recurring FTS5 false-positive as
every migration since Phase 2 Step 2 (see docs/PHASE_2_FREEZE.md §3/§8
and every prior Phase 3 migration's docstring) -- document_text_fts/
annotation_notes_fts and their SQLite-managed shadow tables have no
SQLAlchemy model representation, so autogenerate again proposed dropping
them. Removed -- this migration only ever touches `ocr_corrections`.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3c8a267694b'
down_revision: Union[str, None] = '0083629e42e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'ocr_corrections',
        sa.Column('correction_id', sa.Integer(), nullable=False),
        sa.Column('page_id', sa.Integer(), nullable=False),
        sa.Column('corrected_text', sa.Text(), nullable=False),
        sa.Column('corrected_by', sa.String(length=200), nullable=False),
        sa.Column('corrected_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.ForeignKeyConstraint(['page_id'], ['document_pages.page_id'], ),
        sa.PrimaryKeyConstraint('correction_id'),
    )


def downgrade() -> None:
    op.drop_table('ocr_corrections')
