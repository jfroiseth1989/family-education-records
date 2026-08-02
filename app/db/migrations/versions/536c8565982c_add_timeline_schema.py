"""add timeline schema

Revision ID: 536c8565982c
Revises: fd70891dd2e9
Create Date: 2026-08-02 02:45:48.927505

Hand-trimmed after autogenerate: same recurring FTS5 false-positive as
every migration since Phase 2 Step 2 (see docs/PHASE_2_FREEZE.md §3/§8
and every prior migration's docstring) -- document_text_fts/
annotation_notes_fts and their SQLite-managed shadow tables have no
SQLAlchemy model representation, so autogenerate again proposed dropping
them. Removed -- this migration only ever adds the three Phase 4 Step 1
tables (event_types, timeline_events, timeline_event_facts), see
docs/PHASE_4_IMPLEMENTATION_PLAN.md §3 Step 1.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '536c8565982c'
down_revision: Union[str, None] = 'fd70891dd2e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('event_types',
    sa.Column('type_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=50), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('type_id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('timeline_events',
    sa.Column('event_id', sa.Integer(), nullable=False),
    sa.Column('case_id', sa.Integer(), nullable=False),
    sa.Column('event_date', sa.DateTime(timezone=True), nullable=False),
    sa.Column('event_date_range_end', sa.DateTime(timezone=True), nullable=True),
    sa.Column('event_date_precision', sa.String(length=20), nullable=False),
    sa.Column('event_date_source', sa.String(length=20), nullable=False),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('event_type_id', sa.Integer(), nullable=False),
    sa.Column('created_by', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['case_id'], ['cases.case_id'], ),
    sa.ForeignKeyConstraint(['event_type_id'], ['event_types.type_id'], ),
    sa.PrimaryKeyConstraint('event_id')
    )
    op.create_table('timeline_event_facts',
    sa.Column('event_id', sa.Integer(), nullable=False),
    sa.Column('fact_id', sa.Integer(), nullable=False),
    sa.Column('is_date_source', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['event_id'], ['timeline_events.event_id'], ),
    sa.ForeignKeyConstraint(['fact_id'], ['verified_facts.fact_id'], ),
    sa.PrimaryKeyConstraint('event_id', 'fact_id')
    )


def downgrade() -> None:
    op.drop_table('timeline_event_facts')
    op.drop_table('timeline_events')
    op.drop_table('event_types')
