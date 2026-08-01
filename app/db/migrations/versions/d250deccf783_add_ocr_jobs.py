"""add ocr_jobs

Revision ID: d250deccf783
Revises: d93ac0658fac
Create Date: 2026-08-01 06:00:57.221254

Hand-trimmed after autogenerate: same recurring issue as every migration
since Step 2 (see docs/PHASE_2_FREEZE.md §3/§8) -- the FTS5 virtual
tables (document_text_fts, annotation_notes_fts) and their SQLite-managed
shadow tables have no SQLAlchemy model representation, so autogenerate
again misread them as extra tables to drop. Removed -- this migration
only ever touches `ocr_jobs`.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd250deccf783'
down_revision: Union[str, None] = 'd93ac0658fac'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'ocr_jobs',
        sa.Column('job_id', sa.Integer(), nullable=False),
        sa.Column('document_id', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=30), server_default='queued', nullable=False),
        sa.Column('engine', sa.String(length=100), nullable=True),
        sa.Column('queued_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['document_id'], ['documents.document_id'], ),
        sa.PrimaryKeyConstraint('job_id'),
    )


def downgrade() -> None:
    op.drop_table('ocr_jobs')
