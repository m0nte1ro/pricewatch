import logging
import random
import re
from datetime import timedelta
from decimal import Decimal
from urllib.parse import urlsplit

from sqlalchemy import select

from app.models import Listing, PriceHistory, Product
from app.retailers.generic import GenericAdapter
from app.retailers.http import user_waiting
from app.retailers.parsing import BlockedError, ScrapeError
from app.schemas.domain import Candidate, Identity, Preferences, Snapshot, now
from app.services.alerts import evaluate
from app.services.matching import deduplicate, identify, match_identity, normalize

log = logging.getLogger(__name__)

# A pasted link kept while its store refuses requests: saved now, read on a later check.
UNREAD = "unread"
NOT_READ = "Not read yet: the store refused requests. It is read automatically on a later check."
UNREAD_RETRY = timedelta(minutes=15)


def unread_snapshot(adapter, url: str) -> Snapshot:
    """A placeholder for a link the store would not serve; the URL slug names the product."""
    slug = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    title = re.sub(r"[-_]+", " ", slug).strip() or adapter.label
    return Snapshot(
        retailer=adapter.name,
        url=url,
        identity=identify(title),
        title=title,
        availability="unknown",
        condition=adapter.default_condition,
        method=UNREAD,
    )


def record_snapshot(
    session, product: Product, listing: Listing, snapshot: Snapshot, *, initial: bool = False
):
    if snapshot.method == UNREAD:
        # Nothing was observed: no history row and no events until the page is read.
        listing.title, listing.extraction_method, listing.last_error = (
            snapshot.title,
            UNREAD,
            NOT_READ,
        )
        return
    first_read = listing.extraction_method == UNREAD
    if first_read:
        # The placeholder guessed seller and condition; the page now says what they are.
        listing.condition, listing.seller = str(snapshot.condition), snapshot.seller
        listing.seller_key = normalize(snapshot.seller)
        listing.retailer_product_id = snapshot.retailer_product_id
    evaluate(session, product, listing, snapshot, initial=initial or first_read)
    session.add(
        PriceHistory(
            listing_id=listing.id,
            timestamp=snapshot.observed_at,
            price=snapshot.price,
            availability=snapshot.availability,
            condition=str(snapshot.condition),
            currency=snapshot.currency,
            method=snapshot.method,
            promo_price=snapshot.promo_price,
        )
    )
    if listing.current_price != snapshot.price:
        listing.previous_price = listing.current_price
        log.info("price_changed", extra={"listing_id": listing.id})
    listing.current_price = snapshot.price
    listing.promo_price, listing.promo_code = snapshot.promo_price, snapshot.promo_code
    listing.offered_by = snapshot.offered_by
    listing.original_price = snapshot.original_price
    listing.currency = snapshot.currency
    listing.availability = snapshot.availability
    listing.extraction_method = snapshot.method
    listing.title = snapshot.title
    listing.last_seen_at = snapshot.observed_at
    listing.last_checked_at = now()
    listing.last_error = None


def verify_offer(listing: Listing, snapshots: list[Snapshot]) -> tuple[Snapshot | None, str | None]:
    """This listing's offer in a fresh reading of its page, or why it can no longer be trusted.

    The listing must have its product loaded.
    """
    identity = product_identity(listing.product)
    # A pasted link saved despite naming another model is checked against that model,
    # the one the owner accepted, so only a further change is flagged.
    pasted = identify(listing.title)
    if "manual" in listing.sources and match_identity(identity, pasted).level == "CONFLICT":
        identity = pasted
    snapshot = (
        snapshots[0]
        if listing.extraction_method == UNREAD and snapshots
        else next(
            (
                s
                for s in snapshots
                if str(s.condition) == listing.condition
                and normalize(s.seller) == listing.seller_key
            ),
            None,
        )
    )
    if snapshot is None:
        return (
            None,
            "Original seller/condition offer is missing. Rediscover to review changed offers.",
        )
    level = match_identity(identity, snapshot.identity, snapshot.condition).level
    if level == "CONFLICT":
        return None, "Product identity changed on retailer page; review required"
    if level == "LOW" and normalize(snapshot.title) != normalize(listing.title):
        return None, "Product identity can no longer be verified; review required"
    return snapshot, None


def effective_interval(listing: Listing, preferences: Preferences | None) -> int:
    if listing.check_interval_minutes is not None:
        return listing.check_interval_minutes
    if preferences is None:
        return 60
    return preferences.retailer_intervals.get(listing.retailer, preferences.polling_minutes)


async def confirm_listing_price(
    db,
    adapter: GenericAdapter,
    listing: Listing,
    price: Decimal,
    availability: str | None,
    preferences: Preferences | None,
) -> None:
    with user_waiting():
        html = await adapter.fetcher.get(listing.url, adapter.name, adapter.hosts)
    rule, snapshot = adapter.preview_rule(html, listing.url, price, availability)
    # A confirmed price is recorded like a check, so it passes the same identity test first:
    # otherwise another model's price would raise alerts and become this listing's reference.
    snapshot, error = verify_offer(listing, [snapshot])
    if error:
        raise ScrapeError(error)
    adapter.rules.save(adapter.name, rule)
    with db.session() as session:
        row = session.get(Listing, listing.id)
        record_snapshot(session, row.product, row, snapshot)
        interval = effective_interval(row, preferences)
        row.next_check_at = now() + timedelta(minutes=interval * random.uniform(0.95, 1.05))
    log.info("store_rule_taught", extra={"retailer": adapter.name, "listing_id": listing.id})


def product_identity(product: Product) -> Identity:
    return Identity(
        name=product.canonical_name,
        brand=product.brand,
        model=product.model,
        size=product.size,
        category=product.category,
        identifiers=product.specifications,
    )


async def add_link(db, registry, product_id: int, url: str, preferences: Preferences) -> int:
    """Fetch one pasted link and save it on an existing product.

    Returns the new listing's id, or 0 when the link was already on the product.

    The owner chose both the product and the link, so there is no discovery step or store
    search: the page is read once and saved (a conflicting model is kept, as when pasted).
    """
    adapter = registry.for_url(url)
    url = adapter.normalize_url(url)
    registry.fetcher.retry_now([adapter.name])  # a user action tries a paused store once
    try:
        with user_waiting():
            snapshots = await adapter.fetch_listing(url)
    except BlockedError:
        snapshots = [unread_snapshot(adapter, url)]  # keep the link; read it later
    with db.session() as session:
        product = session.get(Product, product_id)
        if product is None:
            raise ValueError("Product not found")
        if product.archived:
            raise ValueError("Restore the archived product before adding links")
        identity = product_identity(product)
        candidates = deduplicate(
            [
                Candidate(
                    listing=snapshot,
                    sources=["manual"],
                    match=match_identity(identity, snapshot.identity, snapshot.condition),
                )
                for snapshot in snapshots
            ]
        )
        added = add_candidates(session, product, candidates, preferences)
        listing_id = added[0].id if added else 0
    log.info("link_added", extra={"product_id": product_id, "retailer": adapter.name})
    return listing_id


def add_candidates(
    session, product: Product, candidates: list[Candidate], preferences: Preferences | None = None
) -> list[Listing]:
    added = []
    for candidate in candidates:
        snapshot = candidate.listing
        rows = session.scalars(
            select(Listing).where(
                Listing.product_id == product.id,
                Listing.retailer == snapshot.retailer,
                Listing.condition == str(snapshot.condition),
                Listing.seller_key == normalize(snapshot.seller),
            )
        ).all()
        duplicate = next(
            (
                row
                for row in rows
                if (
                    row.url == snapshot.url
                    or (
                        snapshot.retailer_product_id
                        and row.retailer_product_id == snapshot.retailer_product_id
                    )
                    or (
                        not (row.retailer_product_id and snapshot.retailer_product_id)
                        and normalize(row.title) == normalize(snapshot.title)
                        and candidate.match.level in ("EXACT", "HIGH")
                    )
                )
            ),
            None,
        )
        if duplicate:
            duplicate.sources = sorted(set(duplicate.sources + candidate.sources))
            continue
        listing = Listing(
            product_id=product.id,
            retailer=snapshot.retailer,
            url=snapshot.url,
            retailer_product_id=snapshot.retailer_product_id,
            title=snapshot.title,
            condition=str(snapshot.condition),
            seller=snapshot.seller,
            seller_key=normalize(snapshot.seller),
            sources=candidate.sources,
            currency=snapshot.currency,
            availability="unknown",
        )
        session.add(listing)
        session.flush()
        record_snapshot(session, product, listing, snapshot, initial=True)
        interval = effective_interval(listing, preferences)
        listing.next_check_at = now() + (
            UNREAD_RETRY
            if snapshot.method == UNREAD
            else timedelta(minutes=interval * random.uniform(0.95, 1.05))
        )
        added.append(listing)
    return added
