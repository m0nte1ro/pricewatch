"""market searches

Revision ID: e3a1f0c7b912
Revises: b5639359fe67
Create Date: 2026-11-10 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e3a1f0c7b912"
down_revision: str | Sequence[str] | None = "b5639359fe67"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "market_searches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("check_interval_minutes", sa.Integer(), nullable=False),
        sa.Column("next_check_at", sa.DateTime(), nullable=False),
        sa.Column("last_read_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("url"),
    )
    op.create_table(
        "market_listings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("search_id", sa.Integer(), nullable=False),
        sa.Column("listing_id", sa.String(length=150), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("price", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("previous_price", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.Column("place", sa.String(length=150), nullable=True),
        sa.Column("posted_at", sa.DateTime(), nullable=True),
        sa.Column("reserved", sa.Boolean(), nullable=False),
        sa.Column("ships", sa.Boolean(), nullable=False),
        sa.Column("announced", sa.Boolean(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["search_id"],
            ["market_searches.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("search_id", "listing_id", name="uq_market_listings_search_listing"),
    )
    with op.batch_alter_table("market_listings", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_market_listings_search_id"), ["search_id"], unique=False
        )

    with op.batch_alter_table("alerts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("market_listing_id", sa.Integer(), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_alerts_market_listing_id"), ["market_listing_id"], unique=False
        )
        batch_op.create_foreign_key(
            "fk_alerts_market_listing_id", "market_listings", ["market_listing_id"], ["id"]
        )


def downgrade() -> None:
    """Downgrade schema."""
    # Marketplace alerts have neither product nor outlet item once their column is gone.
    op.execute("DELETE FROM alerts WHERE market_listing_id IS NOT NULL")
    with op.batch_alter_table("alerts", schema=None) as batch_op:
        batch_op.drop_constraint("fk_alerts_market_listing_id", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_alerts_market_listing_id"))
        batch_op.drop_column("market_listing_id")

    with op.batch_alter_table("market_listings", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_market_listings_search_id"))

    op.drop_table("market_listings")
    op.drop_table("market_searches")
