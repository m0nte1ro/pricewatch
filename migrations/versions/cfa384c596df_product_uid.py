"""product uid

Every product gets a permanent id that travels with exports, so an import can tell what it
already created.

Revision ID: cfa384c596df
Revises: 9c429652d1ff
Create Date: 2026-10-07 18:00:00.000000

"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "cfa384c596df"
down_revision: str | Sequence[str] | None = "9c429652d1ff"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("products") as batch_op:
        batch_op.add_column(sa.Column("uid", sa.String(36), nullable=True))
    bind = op.get_bind()
    for (product_id,) in bind.execute(sa.text("SELECT id FROM products")).all():
        bind.execute(
            sa.text("UPDATE products SET uid = :uid WHERE id = :id"),
            {"uid": str(uuid.uuid4()), "id": product_id},
        )
    with op.batch_alter_table("products") as batch_op:
        batch_op.alter_column("uid", existing_type=sa.String(36), nullable=False)
        batch_op.create_unique_constraint("uq_products_uid", ["uid"])


def downgrade() -> None:
    with op.batch_alter_table("products") as batch_op:
        batch_op.drop_constraint("uq_products_uid", type_="unique")
        batch_op.drop_column("uid")
