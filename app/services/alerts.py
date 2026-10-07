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
    unconfirmed: bool = False,
    code: str | None = None,
    via: str | None = None,
):
    prices = (
        f"{old if old is not None else '—'} → {new if new is not None else '—'} {listing.currency}"
    )
    if code:
        prices += f" with code {code}"
    if via:
        prices += f" via {via}"
    if unconfirmed:
        prices += " (price unconfirmed)"
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
    # Compare what the owner would pay, so a promo-code price counts like any other drop.
    old, new = listing.deal_price, snapshot.deal_price
    code = snapshot.promo_code if new is not None and new == snapshot.promo_price else None
    # Stock events fire on heuristic readings too; their price must not read as a known one.
    unconfirmed = snapshot.method == "heuristic"
    if initial:
        # Listings are only added after the user confirms them, so record without a push.
        event = (
            "new_listing"
            if snapshot.condition in (Condition.NEW, Condition.UNKNOWN)
            else "outlet_listing"
        )
        emit(
            session,
            product,
            listing,
            event,
            None,
            new,
            push=False,
            unconfirmed=unconfirmed,
            code=code,
            via=snapshot.offered_by,
        )
    elif listing.availability != snapshot.availability:
        if snapshot.availability == "in_stock":
            emit(
                session,
                product,
                listing,
                "became_available",
                old,
                new,
                unconfirmed=unconfirmed,
                code=code,
                via=snapshot.offered_by,
            )
        elif snapshot.availability == "out_of_stock":
            emit(
                session,
                product,
                listing,
                "became_unavailable",
                old,
                new,
                unconfirmed=unconfirmed,
                code=code,
                via=snapshot.offered_by,
            )
    eligible = (
        snapshot.availability == "in_stock"
        and snapshot.currency == "EUR"
        and snapshot.method != "heuristic"
    )
    if not eligible or new is None:
        return
    previously_eligible = (
        not initial
        and listing.availability == "in_stock"
        and listing.currency == "EUR"
        and listing.extraction_method != "heuristic"
    )
    if previously_eligible and old is not None and new < old:
        emit(
            session, product, listing, "price_dropped", old, new, code=code, via=snapshot.offered_by
        )
    for threshold, event in (
        (product.target_price, "target_hit"),
        (product.insane_deal_price, "insane_deal"),
    ):
        if (
            threshold is not None
            and new <= threshold
            and (not previously_eligible or old is None or old > threshold)
        ):
            emit(session, product, listing, event, old, new, code=code, via=snapshot.offered_by)
