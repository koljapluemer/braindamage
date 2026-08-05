"""add ev_curve to contracts

Revision ID: 858314e558ab
Revises: 94018f0ef23c
Create Date: 2026-08-05 17:41:47.754950

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '858314e558ab'
down_revision: Union[str, Sequence[str], None] = '94018f0ef23c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "contracts",
        sa.Column("ev_curve", sa.JSON(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("contracts", "ev_curve")
