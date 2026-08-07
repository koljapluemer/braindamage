"""add postvalidation to contracts

Revision ID: 7a1c4f9d2b6e
Revises: 399e3626bf1d
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "7a1c4f9d2b6e"
down_revision: Union[str, Sequence[str], None] = "399e3626bf1d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("contracts", sa.Column("postvalidated_ranges", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column("contracts", sa.Column("postvalidated_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("contracts", "postvalidated_at")
    op.drop_column("contracts", "postvalidated_ranges")
