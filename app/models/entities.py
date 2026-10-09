import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.schemas.domain import lower_price, now


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("uid", name="uq_products_uid"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    # Travels with exports, so importing the same file twice finds the product it created.
    uid: Mapped[str] = mapped_column(String(36), default=lambda: str(uuid.uuid4()))
    canonical_name: Mapped[str] = mapped_column(String(250))
    brand: Mapped[str | None] = mapped_column(String(100))
    model: Mapped[str | None] = mapped_column(String(100), index=True)
    category: Mapped[str] = mapped_column(String(100), default="general")
    size: Mapped[str | None] = mapped_column(String(30))
    specifications: Mapped[dict] = mapped_column(JSON, default=dict)
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    insane_deal_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    allowed_conditions: Mapped[list] = mapped_column(JSON, default=lambda: ["new"])
    retailers: Mapped[list] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    listings: Mapped[list["Listing"]] = relationship(back_populates="product", lazy="selectin")


class Listing(Base):
    __tablename__ = "listings"
    __table_args__ = (
        # Unique per product: several products may watch the same offer.
        UniqueConstraint(
            "product_id", "retailer", "url", "condition", "seller_key", name="uq_listings_offer_url"
        ),
        UniqueConstraint(
            "product_id",
            "retailer",
            "retailer_product_id",
            "condition",
            "seller_key",
            name="uq_listings_offer_item",
        ),
        Index("ix_listings_due", "enabled", "next_check_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    retailer: Mapped[str] = mapped_column(String(50))
    url: Mapped[str] = mapped_column(Text)
    retailer_product_id: Mapped[str | None] = mapped_column(String(150))
    title: Mapped[str] = mapped_column(String(500))
    current_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    # A lower price offered with a public promo code; deal_price is what the owner would pay.
    promo_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    promo_code: Mapped[str | None] = mapped_column(String(50))
    offered_by: Mapped[str | None] = mapped_column(String(150))
    previous_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    original_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    availability: Mapped[str] = mapped_column(String(30), default="unknown")
    condition: Mapped[str] = mapped_column(String(30), default="unknown")
    seller: Mapped[str | None] = mapped_column(String(200))
    seller_key: Mapped[str] = mapped_column(String(200), default="")
    sources: Mapped[list] = mapped_column(JSON, default=list)
    extraction_method: Mapped[str] = mapped_column(
        String(20), default="structured", server_default="structured"
    )
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    next_check_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    check_interval_minutes: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    product: Mapped[Product] = relationship(back_populates="listings")

    @property
    def deal_price(self) -> Decimal | None:
        return lower_price(self.current_price, self.promo_price)


class PriceHistory(Base):
    __tablename__ = "price_history"
    __table_args__ = (Index("ix_history_listing_time", "listing_id", "timestamp"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"))
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=now)
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    availability: Mapped[str] = mapped_column(String(30))
    condition: Mapped[str] = mapped_column(String(30))
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    promo_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    method: Mapped[str] = mapped_column(
        String(20), default="structured", server_default="structured"
    )
    # The page as fetched, gzipped, kept only for readings that set a new all-time low.
    page: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    listing_id: Mapped[int | None] = mapped_column(ForeignKey("listings.id"))
    event_type: Mapped[str] = mapped_column(String(50))
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    old_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    new_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    message: Mapped[str] = mapped_column(Text)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    notification_state: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    notification_attempts: Mapped[int] = mapped_column(Integer, default=0)
    notification_next_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class RetailerState(Base):
    __tablename__ = "retailer_states"
    name: Mapped[str] = mapped_column(String(50), primary_key=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime)
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)


class StoreRule(Base):
    __tablename__ = "store_rules"
    host: Mapped[str] = mapped_column(String(100), primary_key=True)
    price_selector: Mapped[str] = mapped_column(Text)
    availability_selector: Mapped[str | None] = mapped_column(Text)
    availability_mode: Mapped[str] = mapped_column(
        String(10), default="text", server_default="text"
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)


class DiscoveryDraft(Base):
    __tablename__ = "discovery_drafts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"))
    status: Mapped[str] = mapped_column(String(30), default="pending")
    payload: Mapped[dict] = mapped_column(JSON)
    results: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
