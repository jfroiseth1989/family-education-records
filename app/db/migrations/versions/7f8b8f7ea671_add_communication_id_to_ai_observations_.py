"""add communication_id to ai_observations and verified_facts

Revision ID: 7f8b8f7ea671
Revises: 34b632653500
Create Date: 2026-08-14 17:10:00.000000

Communications Phase Step 7: the smallest additive schema change needed
for `ai_observations`/`verified_facts` to represent a Communication-
sourced suggestion/fact, without forcing a `Communication` through the
Document-shaped `citations` table (which requires a non-null
`document_id`, a `page_id`, and quoted-text offsets/bounding boxes --
none of which apply to a deterministic metadata candidate like "Email
received from X regarding Y on Z"). Both tables were already
case-scoped rather than document-scoped at the schema level (neither had
a `document_id` column at all), so this is a pure addition: one new
nullable FK column on each, defaulting to NULL for every existing row.
`ai_observations.communication_id` is additionally UNIQUE -- a DB-level
backstop (multiple NULLs remain allowed, per standard SQL) alongside the
application-level dedup check in
app/core/communications/timeline_suggestions.py, so a Communication can
never accumulate more than one suggestion row even if that check were
ever bypassed. No other column, index, or constraint changes;
`citations`, `ai_observation_citations`, and `verified_fact_citations`
are untouched.

Hand-trimmed after autogenerate: the same recurring FTS5 false-positive
every migration since Phase 2 Step 2 has had (document_text_fts/
annotation_notes_fts/communication_text_fts and their SQLite-managed
shadow tables have no SQLAlchemy model representation) -- removed.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7f8b8f7ea671'
down_revision: Union[str, None] = '34b632653500'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('ai_observations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('communication_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_ai_observations_communication_id',
            'communications',
            ['communication_id'],
            ['communication_id'],
        )
        batch_op.create_unique_constraint(
            'uq_ai_observations_communication_id', ['communication_id']
        )

    with op.batch_alter_table('verified_facts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('communication_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_verified_facts_communication_id',
            'communications',
            ['communication_id'],
            ['communication_id'],
        )


def downgrade() -> None:
    with op.batch_alter_table('verified_facts', schema=None) as batch_op:
        batch_op.drop_constraint('fk_verified_facts_communication_id', type_='foreignkey')
        batch_op.drop_column('communication_id')

    with op.batch_alter_table('ai_observations', schema=None) as batch_op:
        batch_op.drop_constraint('uq_ai_observations_communication_id', type_='unique')
        batch_op.drop_constraint('fk_ai_observations_communication_id', type_='foreignkey')
        batch_op.drop_column('communication_id')
