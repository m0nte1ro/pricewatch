import logging
from decimal import Decimal

from app.models import Alert, Listing, Product
from app.schemas.domain import Condition, Snapshot

log = logging.getLogger(__name__)


def emit(
    session,
    product: Product,
    listing: Listing,
    event: str,
    old: Decimal | None,
    new: Decimal | None,
    *,
    push: bool = True,
):
    prices = (
        f"{old if old is not None else '—'} → {new if new is not None else '—'} {listing.currency}"
    )
    message = f"{event.replace('_', ' ').upper()} · {product.canonical_name} · {listing.retailer} · {listing.condition} · {prices}"
    if event == "target_hit":
        message += f" · Target: {product.target_price} EUR"
    if event == "insane_deal":
        message += f" · Insane threshold: {product.insane_deal_price} EUR"
    session.add(
        Alert(
            product_id=product.id,
            listing_id=listing.id,
            event_type=event,
            old_price=old,
            new_price=new,
            message=message,
            notification_state="pending" if push else "skipped",
        )
    )
    log.info(
        "alert_triggered",
        extra={"product_id": product.id, "listing_id": listing.id, "event_type": event},
    )


def evaluate(
    session, product: Product, listing: Listing, snapshot: Snapshot, *, initial: bool = False
):
    old, new = listing.current_price, snapshot.price
    if initial:
        # Listings are only added after the user confirms them, so record without a push.
        event = (
            "new_listing"
            if snapshot.condition in (Condition.NEW, Condition.UNKNOWN)
            else "outlet_listing"
        )
        emit(session, product, listing, event, None, new, push=False)
    elif listing.availability != snapshot.availability:
        if snapshot.availability == "in_stock":
            emit(session, product, listing, "became_available", old, new)
        elif snapshot.availability == "out_of_stock":
            emit(session, product, listing, "became_unavailable", old, new)
    eligible = (
        snapshot.condition in product.allowed_conditions
        and snapshot.availability == "in_stock"
        and snapshot.currency == "EUR"
    )
    if not eligible or new is None:
        return
    previously_eligible = (
        not initial
        and listing.condition in product.allowed_conditions
        and listing.availability == "in_stock"
        and listing.currency == "EUR"
    )
    if previously_eligible and old is not None and new < old:
        emit(session, product, listing, "price_dropped", old, new)
    for threshold, event in (
        (product.target_price, "target_hit"),
        (product.insane_deal_price, "insane_deal"),
    ):
        if (
            threshold is not None
            and new <= threshold
            and (not previously_eligible or old is None or old > threshold)
        ):
            emit(session, product, listing, event, old, new)
