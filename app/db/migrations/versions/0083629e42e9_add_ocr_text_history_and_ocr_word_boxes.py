"""add ocr_text_history and document_pages.ocr_word_boxes

Revision ID: 0083629e42e9
Revises: bd7bbb905909
Create Date: 2026-08-01 18:31:00.000000

Hand-split from the same combined autogenerate run as bd7bbb905909_*.py
(and hand-trimmed of the same standing FTS5 false-positive drops --
document_text_fts/annotation_notes_fts and their shadow tables). This
migration adds `ocr_text_history` (append-only archive of superseded raw
OCR text, see the OcrTextHistory model docstring) and
`document_pages.ocr_word_boxes` (nullable, unread by any Phase 3 UI yet).
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0083629e42e9'
down_revision: Union[str, None] = 'bd7bbb905909'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'ocr_text_history',
        sa.Column('history_id', sa.Integer(), nullable=False),
        sa.Column('page_id', sa.Integer(), nullable=False),
        sa.Column('ocr_text', sa.Text(), nullable=True),
        sa.Column('extraction_confidence', sa.Float(), nullable=True),
        sa.Column('superseded_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('superseded_by_job_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['page_id'], ['document_pages.page_id'], ),
        sa.ForeignKeyConstraint(['superseded_by_job_id'], ['ocr_jobs.job_id'], ),
        sa.PrimaryKeyConstraint('history_id'),
    )
    with op.batch_alter_table('document_pages', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ocr_word_boxes', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('document_pages', schema=None) as batch_op:
        batch_op.drop_column('ocr_word_boxes')
    op.drop_table('ocr_text_history')
