"""add optimization ranges to contracts

Revision ID: 399e3626bf1d
Revises: 858314e558ab
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "399e3626bf1d"
down_revision: Union[str, Sequence[str], None] = "858314e558ab"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("contracts", sa.Column("ev_curve_annotations", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column("contracts", sa.Column("optimization_ranges", sa.JSON(), nullable=False, server_default="[]"))


def downgrade() -> None:
    op.drop_column("contracts", "optimization_ranges")
    op.drop_column("contracts", "ev_curve_annotations")
