"""add observed_date and fact_date

Revision ID: fd70891dd2e9
Revises: e15df867783d
Create Date: 2026-08-02 02:30:50.440445

Hand-trimmed after autogenerate: same recurring FTS5 false-positive as
every migration since Phase 2 Step 2 (see docs/PHASE_2_FREEZE.md §3/§8
and every prior migration's docstring) -- document_text_fts/
annotation_notes_fts and their SQLite-managed shadow tables have no
SQLAlchemy model representation, so autogenerate again proposed dropping
them. Removed -- this migration only ever adds
`ai_observations.observed_date` and `verified_facts.fact_date`, both
nullable (see docs/PHASE_4_IMPLEMENTATION_PLAN.md §1/§3 Step 0).
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fd70891dd2e9'
down_revision: Union[str, None] = 'e15df867783d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('ai_observations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('observed_date', sa.DateTime(timezone=True), nullable=True))

    with op.batch_alter_table('verified_facts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('fact_date', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('verified_facts', schema=None) as batch_op:
        batch_op.drop_column('fact_date')

    with op.batch_alter_table('ai_observations', schema=None) as batch_op:
        batch_op.drop_column('observed_date')
