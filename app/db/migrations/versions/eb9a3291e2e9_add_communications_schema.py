"""add communications schema

Revision ID: eb9a3291e2e9
Revises: f87a326f960b
Create Date: 2026-08-13 19:24:43.182364

Hand-trimmed after autogenerate: same recurring FTS5 false-positive as
every migration since Phase 2 Step 2 (see docs/PHASE_2_FREEZE.md §3/§8
and every prior migration's docstring) -- document_text_fts/
annotation_notes_fts and their SQLite-managed shadow tables have no
SQLAlchemy model representation, so autogenerate again proposed dropping
them (and, in downgrade(), recreating empty stand-ins with no data).
Removed -- this migration only ever adds the eight Communications Phase
Step 1 tables (communication_accounts, communications,
communication_threads, communication_attachments,
communication_custody_events, communication_document_links,
communication_import_batches, communication_import_batch_items), see
docs/COMMUNICATIONS_PLAN.md §2. No existing table is altered.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'eb9a3291e2e9'
down_revision: Union[str, None] = 'f87a326f960b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('communication_accounts',
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('provider', sa.String(length=50), nullable=False),
    sa.Column('email_address', sa.String(length=320), nullable=False),
    sa.Column('auth_method', sa.String(length=20), nullable=False),
    sa.Column('credential_ref', sa.String(length=200), nullable=False),
    sa.Column('status', sa.String(length=20), server_default='connected', nullable=False),
    sa.Column('connected_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('disconnected_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_synced_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by', sa.String(length=200), nullable=False),
    sa.PrimaryKeyConstraint('account_id')
    )
    op.create_table('communication_import_batches',
    sa.Column('batch_id', sa.Integer(), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('case_id', sa.Integer(), nullable=True),
    sa.Column('search_criteria', sa.JSON(), nullable=True),
    sa.Column('status', sa.String(length=20), server_default='pending', nullable=False),
    sa.Column('matched_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('imported_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('skipped_duplicate_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('failed_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by', sa.String(length=200), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['communication_accounts.account_id'], ),
    sa.ForeignKeyConstraint(['case_id'], ['cases.case_id'], ),
    sa.PrimaryKeyConstraint('batch_id')
    )
    op.create_table('communication_threads',
    sa.Column('thread_id', sa.Integer(), nullable=False),
    sa.Column('case_id', sa.Integer(), nullable=True),
    sa.Column('subject_normalized', sa.String(length=998), nullable=True),
    sa.Column('participant_summary', sa.Text(), nullable=True),
    sa.Column('first_message_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_message_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('message_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['case_id'], ['cases.case_id'], ),
    sa.PrimaryKeyConstraint('thread_id')
    )
    op.create_table('communications',
    sa.Column('communication_id', sa.Integer(), nullable=False),
    sa.Column('communication_type', sa.String(length=20), server_default='email', nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=True),
    sa.Column('case_id', sa.Integer(), nullable=True),
    sa.Column('subject', sa.Text(), nullable=True),
    sa.Column('from_address', sa.String(length=320), nullable=True),
    sa.Column('from_display_name', sa.String(length=300), nullable=True),
    sa.Column('to_addresses', sa.JSON(), nullable=True),
    sa.Column('cc_addresses', sa.JSON(), nullable=True),
    sa.Column('bcc_addresses', sa.JSON(), nullable=True),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('received_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('message_id_header', sa.String(length=998), nullable=True),
    sa.Column('in_reply_to_header', sa.String(length=998), nullable=True),
    sa.Column('references_header', sa.JSON(), nullable=True),
    sa.Column('raw_headers', sa.JSON(), nullable=True),
    sa.Column('body_text', sa.Text(), nullable=True),
    sa.Column('body_html', sa.Text(), nullable=True),
    sa.Column('sha256_hash', sa.String(length=64), nullable=False),
    sa.Column('stored_path', sa.String(length=1000), nullable=False),
    sa.Column('file_size_bytes', sa.Integer(), nullable=False),
    sa.Column('mailbox_uid', sa.String(length=100), nullable=True),
    sa.Column('mailbox_folder', sa.String(length=200), nullable=True),
    sa.Column('thread_id', sa.Integer(), nullable=True),
    sa.Column('import_method', sa.String(length=20), nullable=False),
    sa.Column('imported_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('imported_by', sa.String(length=200), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['account_id'], ['communication_accounts.account_id'], ),
    sa.ForeignKeyConstraint(['case_id'], ['cases.case_id'], ),
    sa.ForeignKeyConstraint(['thread_id'], ['communication_threads.thread_id'], ),
    sa.PrimaryKeyConstraint('communication_id')
    )
    with op.batch_alter_table('communications', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_communications_sha256_hash'), ['sha256_hash'], unique=False)
        batch_op.create_index('uq_communication_account_message_id', ['account_id', 'message_id_header'], unique=True, sqlite_where=sa.text('message_id_header IS NOT NULL'))

    op.create_table('communication_attachments',
    sa.Column('attachment_id', sa.Integer(), nullable=False),
    sa.Column('communication_id', sa.Integer(), nullable=False),
    sa.Column('filename', sa.String(length=500), nullable=False),
    sa.Column('mime_type', sa.String(length=200), nullable=True),
    sa.Column('size_bytes', sa.Integer(), nullable=False),
    sa.Column('sha256_hash', sa.String(length=64), nullable=False),
    sa.Column('stored_path', sa.String(length=1000), nullable=False),
    sa.Column('is_educational_record_candidate', sa.Boolean(), server_default=sa.text('0'), nullable=False),
    sa.Column('suggested_document_type_id', sa.Integer(), nullable=True),
    sa.Column('review_status', sa.String(length=20), server_default='pending', nullable=False),
    sa.Column('resulting_document_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['communication_id'], ['communications.communication_id'], ),
    sa.ForeignKeyConstraint(['resulting_document_id'], ['documents.document_id'], ),
    sa.ForeignKeyConstraint(['suggested_document_type_id'], ['document_types.type_id'], ),
    sa.PrimaryKeyConstraint('attachment_id')
    )
    with op.batch_alter_table('communication_attachments', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_communication_attachments_sha256_hash'), ['sha256_hash'], unique=False)

    op.create_table('communication_custody_events',
    sa.Column('custody_event_id', sa.Integer(), nullable=False),
    sa.Column('communication_id', sa.Integer(), nullable=False),
    sa.Column('event_type', sa.String(length=50), nullable=False),
    sa.Column('event_timestamp', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('actor', sa.String(length=200), nullable=False),
    sa.Column('sha256_hash_at_event', sa.String(length=64), nullable=False),
    sa.Column('file_size_bytes_at_event', sa.Integer(), nullable=False),
    sa.Column('storage_location_at_event', sa.String(length=1000), nullable=False),
    sa.Column('details', sa.JSON(), nullable=True),
    sa.ForeignKeyConstraint(['communication_id'], ['communications.communication_id'], ),
    sa.PrimaryKeyConstraint('custody_event_id')
    )
    op.create_table('communication_import_batch_items',
    sa.Column('item_id', sa.Integer(), nullable=False),
    sa.Column('batch_id', sa.Integer(), nullable=False),
    sa.Column('mailbox_uid', sa.String(length=100), nullable=False),
    sa.Column('status', sa.String(length=20), server_default='pending', nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('communication_id', sa.Integer(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['batch_id'], ['communication_import_batches.batch_id'], ),
    sa.ForeignKeyConstraint(['communication_id'], ['communications.communication_id'], ),
    sa.PrimaryKeyConstraint('item_id'),
    sa.UniqueConstraint('batch_id', 'mailbox_uid', name='uq_import_batch_item_uid')
    )
    op.create_table('communication_document_links',
    sa.Column('link_id', sa.Integer(), nullable=False),
    sa.Column('document_id', sa.Integer(), nullable=False),
    sa.Column('communication_id', sa.Integer(), nullable=False),
    sa.Column('communication_attachment_id', sa.Integer(), nullable=False),
    sa.Column('linked_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['communication_attachment_id'], ['communication_attachments.attachment_id'], ),
    sa.ForeignKeyConstraint(['communication_id'], ['communications.communication_id'], ),
    sa.ForeignKeyConstraint(['document_id'], ['documents.document_id'], ),
    sa.PrimaryKeyConstraint('link_id')
    )


def downgrade() -> None:
    op.drop_table('communication_document_links')
    op.drop_table('communication_import_batch_items')
    op.drop_table('communication_custody_events')
    with op.batch_alter_table('communication_attachments', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_communication_attachments_sha256_hash'))

    op.drop_table('communication_attachments')
    with op.batch_alter_table('communications', schema=None) as batch_op:
        batch_op.drop_index('uq_communication_account_message_id', sqlite_where=sa.text('message_id_header IS NOT NULL'))
        batch_op.drop_index(batch_op.f('ix_communications_sha256_hash'))

    op.drop_table('communications')
    op.drop_table('communication_threads')
    op.drop_table('communication_import_batches')
    op.drop_table('communication_accounts')
