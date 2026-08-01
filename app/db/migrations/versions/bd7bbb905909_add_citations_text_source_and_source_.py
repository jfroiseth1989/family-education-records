"""add citations text_source and source_confidence

Revision ID: bd7bbb905909
Revises: d250deccf783
Create Date: 2026-08-01 18:30:51.241832

Hand-split from a combined autogenerate output (same standing FTS5
false-positive as every migration since Step 2, and this run also
detected the not-yet-generated ocr_text_history table/ocr_word_boxes
column from a later, deliberately separate migration -- see
0083629e42e9_*.py). This migration touches `citations` only.

`text_source` is NOT NULL with server_default='native': every citation
row that already exists was necessarily created before OCR existed
(Phase 2 Step 4's create_highlight() only ever sliced native extracted
text), so backfilling every existing row to 'native' is factually
correct, not a guess -- see docs/PHASE_3_DECISIONS.md §9.1. No existing
citation's provenance is lost or altered; no other column is touched.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'bd7bbb905909'
down_revision: Union[str, None] = 'd250deccf783'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('citations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('text_source', sa.String(length=20), server_default='native', nullable=False))
        batch_op.add_column(sa.Column('source_confidence', sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('citations', schema=None) as batch_op:
        batch_op.drop_column('source_confidence')
        batch_op.drop_column('text_source')
