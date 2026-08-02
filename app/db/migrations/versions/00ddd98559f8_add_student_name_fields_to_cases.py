"""add student name fields to cases

Revision ID: 00ddd98559f8
Revises: 536c8565982c
Create Date: 2026-08-02 16:54:52.129070

Hand-trimmed after autogenerate: same recurring FTS5 false-positive as
every migration since Phase 2 Step 2 (see docs/PHASE_2_FREEZE.md §3/§8
and every prior migration's docstring) -- document_text_fts/
annotation_notes_fts and their SQLite-managed shadow tables have no
SQLAlchemy model representation, so autogenerate again proposed dropping
them. Removed -- this migration only ever adds three nullable columns to
`cases` (FERChronos UX refinement Step 2, see the Case model docstring):
`legal_first_name`, `legal_last_name`, `preferred_name`. Purely additive;
`label` and every existing case row are untouched.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '00ddd98559f8'
down_revision: Union[str, None] = '536c8565982c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('cases', schema=None) as batch_op:
        batch_op.add_column(sa.Column('legal_first_name', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('legal_last_name', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('preferred_name', sa.String(length=100), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('cases', schema=None) as batch_op:
        batch_op.drop_column('preferred_name')
        batch_op.drop_column('legal_last_name')
        batch_op.drop_column('legal_first_name')
