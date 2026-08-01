"""add fact observation and summary layer

Revision ID: e15df867783d
Revises: b3c8a267694b
Create Date: 2026-08-01 19:46:15.547571

Hand-trimmed after autogenerate: same recurring FTS5 false-positive as
every migration since Phase 2 Step 2 (see docs/PHASE_2_FREEZE.md §3/§8
and every prior migration's docstring) -- document_text_fts/
annotation_notes_fts and their SQLite-managed shadow tables have no
SQLAlchemy model representation, so autogenerate again proposed dropping
them. Removed -- this migration only ever adds the seven Phase 3.5 Step 1
tables (fact_types, ai_observations, ai_observation_citations,
verified_facts, verified_fact_citations, ai_summaries,
summary_source_documents). Table creation order follows FK dependency
order: fact_types and ai_observations have no dependency on each other's
data, so ai_observations is created before verified_facts (which has a
nullable FK to it for promotion lineage).
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e15df867783d'
down_revision: Union[str, None] = 'b3c8a267694b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('fact_types',
    sa.Column('type_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=50), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('type_id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('ai_observations',
    sa.Column('observation_id', sa.Integer(), nullable=False),
    sa.Column('case_id', sa.Integer(), nullable=False),
    sa.Column('fact_type_id', sa.Integer(), nullable=False),
    sa.Column('statement', sa.Text(), nullable=False),
    sa.Column('confidence_score', sa.Float(), nullable=False),
    sa.Column('method', sa.String(length=100), nullable=False),
    sa.Column('status', sa.String(length=20), server_default='pending_review', nullable=False),
    sa.Column('reviewed_by', sa.String(length=200), nullable=True),
    sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['case_id'], ['cases.case_id'], ),
    sa.ForeignKeyConstraint(['fact_type_id'], ['fact_types.type_id'], ),
    sa.PrimaryKeyConstraint('observation_id')
    )
    op.create_table('verified_facts',
    sa.Column('fact_id', sa.Integer(), nullable=False),
    sa.Column('case_id', sa.Integer(), nullable=False),
    sa.Column('fact_type_id', sa.Integer(), nullable=False),
    sa.Column('statement', sa.Text(), nullable=False),
    sa.Column('confidence_label', sa.String(length=20), nullable=False),
    sa.Column('confidence_score', sa.Float(), nullable=True),
    sa.Column('source_observation_id', sa.Integer(), nullable=True),
    sa.Column('created_by', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['case_id'], ['cases.case_id'], ),
    sa.ForeignKeyConstraint(['fact_type_id'], ['fact_types.type_id'], ),
    sa.ForeignKeyConstraint(['source_observation_id'], ['ai_observations.observation_id'], ),
    sa.PrimaryKeyConstraint('fact_id')
    )
    op.create_table('ai_summaries',
    sa.Column('summary_id', sa.Integer(), nullable=False),
    sa.Column('case_id', sa.Integer(), nullable=False),
    sa.Column('scope', sa.String(length=20), nullable=False),
    sa.Column('scope_document_id', sa.Integer(), nullable=True),
    sa.Column('scope_range_start', sa.DateTime(timezone=True), nullable=True),
    sa.Column('scope_range_end', sa.DateTime(timezone=True), nullable=True),
    sa.Column('summary_text', sa.Text(), nullable=False),
    sa.Column('generated_by', sa.String(length=100), nullable=False),
    sa.Column('generated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('review_status', sa.String(length=30), server_default='pending_review', nullable=False),
    sa.Column('reviewed_by', sa.String(length=200), nullable=True),
    sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('label_text', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['case_id'], ['cases.case_id'], ),
    sa.ForeignKeyConstraint(['scope_document_id'], ['documents.document_id'], ),
    sa.PrimaryKeyConstraint('summary_id')
    )
    op.create_table('summary_source_documents',
    sa.Column('summary_id', sa.Integer(), nullable=False),
    sa.Column('document_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['document_id'], ['documents.document_id'], ),
    sa.ForeignKeyConstraint(['summary_id'], ['ai_summaries.summary_id'], ),
    sa.PrimaryKeyConstraint('summary_id', 'document_id')
    )
    op.create_table('ai_observation_citations',
    sa.Column('observation_id', sa.Integer(), nullable=False),
    sa.Column('citation_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['citation_id'], ['citations.citation_id'], ),
    sa.ForeignKeyConstraint(['observation_id'], ['ai_observations.observation_id'], ),
    sa.PrimaryKeyConstraint('observation_id', 'citation_id')
    )
    op.create_table('verified_fact_citations',
    sa.Column('fact_id', sa.Integer(), nullable=False),
    sa.Column('citation_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['citation_id'], ['citations.citation_id'], ),
    sa.ForeignKeyConstraint(['fact_id'], ['verified_facts.fact_id'], ),
    sa.PrimaryKeyConstraint('fact_id', 'citation_id')
    )


def downgrade() -> None:
    op.drop_table('verified_fact_citations')
    op.drop_table('ai_observation_citations')
    op.drop_table('summary_source_documents')
    op.drop_table('ai_summaries')
    op.drop_table('verified_facts')
    op.drop_table('ai_observations')
    op.drop_table('fact_types')
