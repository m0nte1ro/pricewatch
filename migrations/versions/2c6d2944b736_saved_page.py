"""saved page

A reading that sets a new all-time low keeps the page it was read from, gzipped, so the owner
can see what the store showed at that price.

Revision ID: 2c6d2944b736
Revises: cfa384c596df
Create Date: 2026-10-09 09:28:10.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2c6d2944b736"
down_revision: str | Sequence[str] | None = "cfa384c596df"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("price_history") as batch_op:
        batch_op.add_column(sa.Column("page", sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("price_history") as batch_op:
        batch_op.drop_column("page")
