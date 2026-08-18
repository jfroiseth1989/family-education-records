"""add iep consistency review schema

Revision ID: f94cb0298188
Revises: 9f6d90a557ce
Create Date: 2026-08-18 03:52:11.282049

IEP Consistency Review, Step 1 (see docs/IEP_CONSISTENCY_REVIEW_PLAN.md):
schema only. Four extensible lookup tables (iep_record_types,
iep_field_types, iep_inconsistency_types, iep_document_link_types) and
four data tables (iep_records, iep_record_fields, iep_document_links,
iep_inconsistency_flags) -- all purely additive. No existing table is
altered, and no row is written to any existing table by this migration.

Hand-trimmed after autogenerate: the same recurring FTS5 false-positive
every migration since Phase 2 Step 2 has had (document_text_fts/
annotation_notes_fts/communication_text_fts and their SQLite-managed
shadow tables have no SQLAlchemy model representation, so autogenerate
proposes dropping and the downgrade proposes recreating them) --
removed from both upgrade() and downgrade().
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f94cb0298188'
down_revision: Union[str, None] = '9f6d90a557ce'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'iep_document_link_types',
        sa.Column('type_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('type_id'),
        sa.UniqueConstraint('name'),
    )
    op.create_table(
        'iep_field_types',
        sa.Column('type_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('value_kind', sa.String(length=20), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('type_id'),
        sa.UniqueConstraint('name'),
    )
    op.create_table(
        'iep_inconsistency_types',
        sa.Column('type_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('type_id'),
        sa.UniqueConstraint('name'),
    )
    op.create_table(
        'iep_record_types',
        sa.Column('type_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('type_id'),
        sa.UniqueConstraint('name'),
    )
    op.create_table(
        'iep_document_links',
        sa.Column('link_id', sa.Integer(), nullable=False),
        sa.Column('case_id', sa.Integer(), nullable=False),
        sa.Column('link_type_id', sa.Integer(), nullable=False),
        sa.Column('from_document_id', sa.Integer(), nullable=False),
        sa.Column('to_document_id', sa.Integer(), nullable=True),
        sa.Column('to_communication_id', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=20), server_default='pending_review', nullable=False),
        sa.Column('method', sa.String(length=100), nullable=False),
        sa.Column('created_by', sa.String(length=200), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('reviewed_by', sa.String(length=200), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['case_id'], ['cases.case_id'], ),
        sa.ForeignKeyConstraint(['from_document_id'], ['documents.document_id'], ),
        sa.ForeignKeyConstraint(['link_type_id'], ['iep_document_link_types.type_id'], ),
        sa.ForeignKeyConstraint(['to_communication_id'], ['communications.communication_id'], ),
        sa.ForeignKeyConstraint(['to_document_id'], ['documents.document_id'], ),
        sa.PrimaryKeyConstraint('link_id'),
    )
    op.create_table(
        'iep_records',
        sa.Column('record_id', sa.Integer(), nullable=False),
        sa.Column('case_id', sa.Integer(), nullable=False),
        sa.Column('document_id', sa.Integer(), nullable=True),
        sa.Column('communication_id', sa.Integer(), nullable=True),
        sa.Column('record_type_id', sa.Integer(), nullable=False),
        sa.Column('section_label', sa.String(length=300), nullable=True),
        sa.Column('comparison_key', sa.String(length=300), nullable=True),
        sa.Column('status', sa.String(length=20), server_default='active', nullable=False),
        sa.Column('extraction_method', sa.String(length=100), nullable=False),
        sa.Column('extracted_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('created_by', sa.String(length=200), nullable=False),
        sa.ForeignKeyConstraint(['case_id'], ['cases.case_id'], ),
        sa.ForeignKeyConstraint(['communication_id'], ['communications.communication_id'], ),
        sa.ForeignKeyConstraint(['document_id'], ['documents.document_id'], ),
        sa.ForeignKeyConstraint(['record_type_id'], ['iep_record_types.type_id'], ),
        sa.PrimaryKeyConstraint('record_id'),
    )
    op.create_table(
        'iep_record_fields',
        sa.Column('field_id', sa.Integer(), nullable=False),
        sa.Column('record_id', sa.Integer(), nullable=False),
        sa.Column('field_type_id', sa.Integer(), nullable=False),
        sa.Column('text_value', sa.Text(), nullable=True),
        sa.Column('numeric_value', sa.Float(), nullable=True),
        sa.Column('date_value', sa.DateTime(timezone=True), nullable=True),
        sa.Column('unit', sa.String(length=50), nullable=True),
        sa.Column('citation_id', sa.Integer(), nullable=True),
        sa.Column('extraction_confidence', sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(['citation_id'], ['citations.citation_id'], ),
        sa.ForeignKeyConstraint(['field_type_id'], ['iep_field_types.type_id'], ),
        sa.ForeignKeyConstraint(['record_id'], ['iep_records.record_id'], ),
        sa.PrimaryKeyConstraint('field_id'),
    )
    op.create_table(
        'iep_inconsistency_flags',
        sa.Column('flag_id', sa.Integer(), nullable=False),
        sa.Column('case_id', sa.Integer(), nullable=False),
        sa.Column('inconsistency_type_id', sa.Integer(), nullable=False),
        sa.Column('comparison_mode', sa.String(length=20), nullable=False),
        sa.Column('source_a_record_id', sa.Integer(), nullable=False),
        sa.Column('source_a_field_id', sa.Integer(), nullable=True),
        sa.Column('source_b_record_id', sa.Integer(), nullable=False),
        sa.Column('source_b_field_id', sa.Integer(), nullable=True),
        sa.Column('rule_id', sa.String(length=100), nullable=False),
        sa.Column('reason_text', sa.Text(), nullable=False),
        sa.Column('extracted_value_a', sa.JSON(), nullable=True),
        sa.Column('extracted_value_b', sa.JSON(), nullable=True),
        sa.Column('status', sa.String(length=20), server_default='pending', nullable=False),
        sa.Column('user_note', sa.Text(), nullable=True),
        sa.Column('reviewed_by', sa.String(length=200), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('linked_timeline_event_id', sa.Integer(), nullable=True),
        sa.Column('linked_verified_fact_id', sa.Integer(), nullable=True),
        sa.Column('dedup_key', sa.String(length=128), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.ForeignKeyConstraint(['case_id'], ['cases.case_id'], ),
        sa.ForeignKeyConstraint(['inconsistency_type_id'], ['iep_inconsistency_types.type_id'], ),
        sa.ForeignKeyConstraint(['linked_timeline_event_id'], ['timeline_events.event_id'], ),
        sa.ForeignKeyConstraint(['linked_verified_fact_id'], ['verified_facts.fact_id'], ),
        sa.ForeignKeyConstraint(['source_a_field_id'], ['iep_record_fields.field_id'], ),
        sa.ForeignKeyConstraint(['source_a_record_id'], ['iep_records.record_id'], ),
        sa.ForeignKeyConstraint(['source_b_field_id'], ['iep_record_fields.field_id'], ),
        sa.ForeignKeyConstraint(['source_b_record_id'], ['iep_records.record_id'], ),
        sa.PrimaryKeyConstraint('flag_id'),
        sa.UniqueConstraint('case_id', 'dedup_key', name='uq_iep_flag_case_dedup_key'),
    )


def downgrade() -> None:
    op.drop_table('iep_inconsistency_flags')
    op.drop_table('iep_record_fields')
    op.drop_table('iep_records')
    op.drop_table('iep_document_links')
    op.drop_table('iep_record_types')
    op.drop_table('iep_inconsistency_types')
    op.drop_table('iep_field_types')
    op.drop_table('iep_document_link_types')
