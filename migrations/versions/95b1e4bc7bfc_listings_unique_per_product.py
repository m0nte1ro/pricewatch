"""listings unique per product

Several products may watch the same offer (for example with different targets), so the
offer constraints now include product_id.

Revision ID: 95b1e4bc7bfc
Revises: d2b728ac005a
Create Date: 2026-10-07 15:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "95b1e4bc7bfc"
down_revision: str | Sequence[str] | None = "d2b728ac005a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The original constraints were created unnamed; SQLite batch mode reflects them under
# this convention so they can be dropped by name.
NAMING = {"uq": "uq_%(table_name)s_%(column_0_N_name)s"}
OLD = {
    "uq_listings_retailer_url_condition_seller_key": ["retailer", "url", "condition", "seller_key"],
    "uq_listings_retailer_retailer_product_id_condition_seller_key": [
        "retailer",
        "retailer_product_id",
        "condition",
        "seller_key",
    ],
}
NEW = {
    "uq_listings_offer_url": ["product_id", "retailer", "url", "condition", "seller_key"],
    "uq_listings_offer_item": [
        "product_id",
        "retailer",
        "retailer_product_id",
        "condition",
        "seller_key",
    ],
}


def upgrade() -> None:
    with op.batch_alter_table("listings", naming_convention=NAMING, recreate="always") as batch_op:
        for name in OLD:
            batch_op.drop_constraint(name, type_="unique")
        for name, columns in NEW.items():
            batch_op.create_unique_constraint(name, columns)


def downgrade() -> None:
    # Fails if several products watch the same offer: remove those duplicates first.
    with op.batch_alter_table("listings", naming_convention=NAMING, recreate="always") as batch_op:
        for name in NEW:
            batch_op.drop_constraint(name, type_="unique")
        for name, columns in OLD.items():
            batch_op.create_unique_constraint(name, columns)
