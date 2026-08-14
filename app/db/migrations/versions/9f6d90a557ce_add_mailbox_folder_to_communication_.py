"""add mailbox_folder to communication_import_batch_items

Revision ID: 9f6d90a557ce
Revises: 7f8b8f7ea671
Create Date: 2026-08-14 20:53:52.840805

Communications Phase Step 10: an IMAP UID is only unique *within its
folder* (established in Step 9 -- see app/core/communications/
imap_client.py's module docstring), so a batch item's durable mailbox
identity must be `(mailbox_folder, mailbox_uid)` together, never
`mailbox_uid` alone. `communication_import_batch_items` (added in Step 1,
never populated by any code before Step 10) is extended with the missing
`mailbox_folder` column, and the unique constraint is widened to match.
`nullable=False` with no server default is safe here specifically
because this table has never had a single row written to it by any
shipped feature -- there is no legacy data to backfill.

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
revision: str = '9f6d90a557ce'
down_revision: Union[str, None] = '7f8b8f7ea671'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('communication_import_batch_items', schema=None) as batch_op:
        batch_op.add_column(sa.Column('mailbox_folder', sa.String(length=200), nullable=False))
        batch_op.drop_constraint(batch_op.f('uq_import_batch_item_uid'), type_='unique')
        batch_op.create_unique_constraint(
            'uq_import_batch_item_folder_uid', ['batch_id', 'mailbox_folder', 'mailbox_uid']
        )


def downgrade() -> None:
    with op.batch_alter_table('communication_import_batch_items', schema=None) as batch_op:
        batch_op.drop_constraint('uq_import_batch_item_folder_uid', type_='unique')
        batch_op.create_unique_constraint(batch_op.f('uq_import_batch_item_uid'), ['batch_id', 'mailbox_uid'])
        batch_op.drop_column('mailbox_folder')
