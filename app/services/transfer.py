"""Export a watchlist to JSON and import it elsewhere.

Import only ever adds what is missing and never changes what exists, so importing the same
file twice (or into the database it came from) changes nothing.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ValidationError
from sqlalchemy import select

from app.models import Listing, PriceHistory, Product, StoreRule
from app.retailers.parsing import ScrapeError
from app.schemas.domain import now
from app.services.matching import normalize

log = logging.getLogger(__name__)

FORMAT, VERSION = "pricewatch-export", 1


class TransferError(Exception):
    """The file is not a pricewatch export this version understands."""


class HistoryEntry(BaseModel):
    timestamp: datetime
    price: Decimal | None = None
    availability: str
    condition: str
    currency: str = "EUR"
    method: str = "structured"
    promo_price: Decimal | None = None


class ListingEntry(BaseModel):
    retailer: str
    url: str
    retailer_product_id: str | None = None
    title: str
    condition: str = "unknown"
    seller: str | None = None
    sources: list[str] = []
    enabled: bool = True
    check_interval_minutes: int | None = None
    current_price: Decimal | None = None
    previous_price: Decimal | None = None
    original_price: Decimal | None = None
    currency: str = "EUR"
    availability: str = "unknown"
    extraction_method: str = "structured"
    promo_price: Decimal | None = None
    promo_code: str | None = None
    offered_by: str | None = None
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    last_checked_at: datetime | None = None
    history: list[HistoryEntry] = []


class ProductEntry(BaseModel):
    uid: str
    canonical_name: str
    brand: str | None = None
    model: str | None = None
    category: str = "general"
    size: str | None = None
    specifications: dict = {}
    target_price: Decimal | None = None
    insane_deal_price: Decimal | None = None
    enabled: bool = True
    archived: bool = False
    created_at: datetime | None = None
    listings: list[ListingEntry] = []


class RuleEntry(BaseModel):
    host: str
    price_selector: str
    availability_selector: str | None = None
    availability_mode: str = "text"


class ExportFile(BaseModel):
    format: str
    version: int
    products: list[ProductEntry] = []
    store_rules: list[RuleEntry] = []


@dataclass
class TransferResult:
    products: int = 0
    listings: int = 0
    history: int = 0
    rules: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def added(self) -> bool:
        return any((self.products, self.listings, self.history, self.rules))


def _dump(model: BaseModel) -> dict:
    return model.model_dump(mode="json")


def export_data(db) -> dict:
    with db.session() as session:
        products = []
        for product in session.scalars(select(Product).order_by(Product.created_at, Product.id)):
            listings = []
            for listing in sorted(product.listings, key=lambda x: x.id):
                history = session.scalars(
                    select(PriceHistory)
                    .where(PriceHistory.listing_id == listing.id)
                    .order_by(PriceHistory.timestamp, PriceHistory.id)
                ).all()
                fields = {
                    n: getattr(listing, n) for n in ListingEntry.model_fields if n != "history"
                }
                fields["history"] = [
                    HistoryEntry.model_validate(row, from_attributes=True) for row in history
                ]
                listings.append(ListingEntry(**fields))
            fields = {n: getattr(product, n) for n in ProductEntry.model_fields if n != "listings"}
            products.append(ProductEntry(**fields | {"listings": listings}))
        rules = [
            RuleEntry.model_validate(rule, from_attributes=True)
            for rule in session.scalars(select(StoreRule).order_by(StoreRule.host))
        ]
    return {
        "format": FORMAT,
        "version": VERSION,
        "exported_at": now().isoformat(),
        "products": [_dump(p) for p in products],
        "store_rules": [_dump(r) for r in rules],
    }


def import_data(db, registry, data: dict) -> TransferResult:
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise TransferError("This is not a pricewatch export file")
    if data.get("version") != VERSION:
        raise TransferError(f"Unsupported export version {data.get('version')!r}")
    try:
        export = ExportFile.model_validate(data)
    except ValidationError as exc:
        raise TransferError(
            f"The export file is damaged: {exc.error_count()} invalid field(s)"
        ) from None
    result = TransferResult()
    # One transaction: a file either imports completely or not at all.
    with db.session() as session:
        for entry in export.products:
            product = session.scalar(select(Product).where(Product.uid == entry.uid))
            if product is None:
                fields = entry.model_dump(exclude={"listings", "created_at"})
                product = Product(**fields, created_at=entry.created_at or now())
                session.add(product)
                session.flush()
                result.products += 1
            for listing_entry in entry.listings:
                _import_listing(session, registry, product, listing_entry, result)
        for rule in export.store_rules:
            if session.get(StoreRule, rule.host) is None:
                session.add(StoreRule(**rule.model_dump()))
                result.rules += 1
    log.info("watchlist_imported", extra={"count": result.products})
    return result


def _import_listing(session, registry, product: Product, entry: ListingEntry, result) -> None:
    # Links pass the same checks as pasted ones: a file cannot point the app at a private host.
    try:
        adapter = registry.for_url(entry.url)
        url = adapter.normalize_url(entry.url)
    except (ScrapeError, ValueError) as exc:
        result.notes.append(f"Skipped {entry.url}: {exc}")
        return
    seller_key = normalize(entry.seller)
    listing = session.scalar(
        select(Listing).where(
            Listing.product_id == product.id,
            Listing.retailer == adapter.name,
            Listing.url == url,
            Listing.condition == entry.condition,
            Listing.seller_key == seller_key,
        )
    )
    if listing is None:
        fields = entry.model_dump(exclude={"history", "retailer", "url", "first_seen_at"})
        listing = Listing(
            **fields,
            product_id=product.id,
            retailer=adapter.name,
            url=url,
            seller_key=seller_key,
            first_seen_at=entry.first_seen_at or now(),
            next_check_at=now(),  # read it again soon in its new home
        )
        session.add(listing)
        session.flush()
        result.listings += 1
    known = set(
        session.scalars(select(PriceHistory.timestamp).where(PriceHistory.listing_id == listing.id))
    )
    for row in entry.history:
        if row.timestamp not in known:
            session.add(PriceHistory(listing_id=listing.id, **row.model_dump()))
            known.add(row.timestamp)
            result.history += 1
