"""add date_received to documents

Revision ID: 01ff0f6a9116
Revises: 00ddd98559f8
Create Date: 2026-08-02 17:19:51.826924

Hand-trimmed after autogenerate: same recurring FTS5 false-positive as
every migration since Phase 2 Step 2 (see docs/PHASE_2_FREEZE.md §3/§8
and every prior migration's docstring) -- document_text_fts/
annotation_notes_fts and their SQLite-managed shadow tables have no
SQLAlchemy model representation, so autogenerate again proposed dropping
them. Removed -- this migration only ever adds one nullable column,
`documents.date_received` (FERChronos UX refinement Step 4, see the
Document model docstring). Purely additive; no backfill, no existing
data touched.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '01ff0f6a9116'
down_revision: Union[str, None] = '00ddd98559f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.add_column(sa.Column('date_received', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.drop_column('date_received')
