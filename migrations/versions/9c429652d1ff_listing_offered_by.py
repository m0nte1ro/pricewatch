"""listing offered_by

Price-comparison listings (KuantoKusta) follow the cheapest offer; offered_by records which
store and shipping that price comes from.

Revision ID: 9c429652d1ff
Revises: 136c811fa067
Create Date: 2026-10-07 17:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9c429652d1ff"
down_revision: str | Sequence[str] | None = "136c811fa067"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("listings") as batch_op:
        batch_op.add_column(sa.Column("offered_by", sa.String(150), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("listings") as batch_op:
        batch_op.drop_column("offered_by")
