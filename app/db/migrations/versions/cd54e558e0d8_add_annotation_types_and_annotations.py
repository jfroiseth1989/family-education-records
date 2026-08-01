"""add annotation_types and annotations

Revision ID: cd54e558e0d8
Revises: 6375a2b94580
Create Date: 2026-08-01 04:39:28.522671

Hand-trimmed after autogenerate, same issue as the Step 3 tags migration:
the FTS5 virtual table from Step 2 has no SQLAlchemy model representation,
so autogenerate again proposed dropping/recreating document_text_fts and
its shadow tables. Removed -- this migration only touches
`annotation_types` and `annotations`.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'cd54e558e0d8'
down_revision: Union[str, None] = '6375a2b94580'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'annotation_types',
        sa.Column('type_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=50), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('type_id'),
        sa.UniqueConstraint('name'),
    )
    op.create_table(
        'annotations',
        sa.Column('annotation_id', sa.Integer(), nullable=False),
        sa.Column('case_id', sa.Integer(), nullable=False),
        sa.Column('document_id', sa.Integer(), nullable=False),
        sa.Column('page_id', sa.Integer(), nullable=True),
        sa.Column('citation_id', sa.Integer(), nullable=True),
        sa.Column('annotation_type_id', sa.Integer(), nullable=False),
        sa.Column('body_text', sa.Text(), nullable=True),
        sa.Column('color', sa.String(length=20), nullable=True),
        sa.Column('created_by', sa.String(length=200), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['annotation_type_id'], ['annotation_types.type_id'], ),
        sa.ForeignKeyConstraint(['case_id'], ['cases.case_id'], ),
        sa.ForeignKeyConstraint(['citation_id'], ['citations.citation_id'], ),
        sa.ForeignKeyConstraint(['document_id'], ['documents.document_id'], ),
        sa.ForeignKeyConstraint(['page_id'], ['document_pages.page_id'], ),
        sa.PrimaryKeyConstraint('annotation_id'),
    )


def downgrade() -> None:
    op.drop_table('annotations')
    op.drop_table('annotation_types')
