"""add tags and document_tags

Revision ID: 6375a2b94580
Revises: 08c778ee32af
Create Date: 2026-08-01 04:24:14.730612

Hand-trimmed after autogenerate: the raw-SQL FTS5 virtual table from the
prior migration (document_text_fts and its SQLite-managed shadow tables
_data/_config/_docsize/_idx) has no SQLAlchemy model representation, so
autogenerate misread it as "extra tables not in the model" and proposed
dropping them here. That would have deleted the Step 2 search index.
Removed those spurious drop/recreate statements -- this migration only
ever touches `tags` and `document_tags`.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6375a2b94580'
down_revision: Union[str, None] = '08c778ee32af'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'tags',
        sa.Column('tag_id', sa.Integer(), nullable=False),
        sa.Column('case_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('category', sa.String(length=100), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.ForeignKeyConstraint(['case_id'], ['cases.case_id'], ),
        sa.PrimaryKeyConstraint('tag_id'),
        sa.UniqueConstraint('case_id', 'name', name='uq_tag_case_name'),
    )
    op.create_table(
        'document_tags',
        sa.Column('document_id', sa.Integer(), nullable=False),
        sa.Column('tag_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.ForeignKeyConstraint(['document_id'], ['documents.document_id'], ),
        sa.ForeignKeyConstraint(['tag_id'], ['tags.tag_id'], ),
        sa.PrimaryKeyConstraint('document_id', 'tag_id'),
    )


def downgrade() -> None:
    op.drop_table('document_tags')
    op.drop_table('tags')
