"""promo-code prices

A lower price a store offers with a public promo code is kept on the listing and in its
history, so alerts, best price and historical figures use what the owner would pay.

Revision ID: 136c811fa067
Revises: 95b1e4bc7bfc
Create Date: 2026-10-07 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "136c811fa067"
down_revision: str | Sequence[str] | None = "95b1e4bc7bfc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("listings") as batch_op:
        batch_op.add_column(sa.Column("promo_price", sa.Numeric(12, 2), nullable=True))
        batch_op.add_column(sa.Column("promo_code", sa.String(50), nullable=True))
    with op.batch_alter_table("price_history") as batch_op:
        batch_op.add_column(sa.Column("promo_price", sa.Numeric(12, 2), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("price_history") as batch_op:
        batch_op.drop_column("promo_price")
    with op.batch_alter_table("listings") as batch_op:
        batch_op.drop_column("promo_code")
        batch_op.drop_column("promo_price")
