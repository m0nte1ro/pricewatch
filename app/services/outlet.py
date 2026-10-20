import asyncio
import logging
import random
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from app.models import Alert, OutletFeed, OutletItem, OutletPrice, OutletRule
from app.retailers.parsing import ScrapeError
from app.schemas.domain import FeedItem, now
from app.services.matching import normalize

log = logging.getLogger(__name__)

EMPTY_READ = "The page listed no products; keeping the last stock until it lists some again"


def matching_rules(rules: list[OutletRule], title: str) -> list[str]:
    name = normalize(title)
    return [rule.text for rule in rules if normalize(rule.text) in name]


def emit(session, item: OutletItem, event: str, message: str):
    session.add(
        Alert(
            outlet_item_id=item.id,
            event_type=event,
            old_price=item.previous_price,
            new_price=item.price,
            message=f"{message} · {item.title} · {price_text(item.price)}",
        )
    )
    log.info("alert_triggered", extra={"event_type": event})


def price_text(price: Decimal | None) -> str:
    return f"{price} EUR" if price is not None else "no price"


def apply(session, feed: OutletFeed, found: list[FeedItem]):
    """Fold a complete read of the feed into its items and raise the alerts it calls for."""
    items = {item.item_id: item for item in feed.items}
    if not found and any(item.in_stock for item in items.values()):
        # One empty answer must not flag the whole outlet as gone and ring every bell.
        raise ScrapeError(EMPTY_READ)
    rules = session.scalars(select(OutletRule)).all()
    first = feed.last_read_at is None
    seen = set()
    for reading in found:
        seen.add(reading.item_id)
        item = items.get(reading.item_id)
        if item is None:
            item = OutletItem(
                feed=feed,
                item_id=reading.item_id,
                title=reading.title,
                url=reading.url,
                price=reading.price,
                original_price=reading.original_price,
                lowest_price=reading.price,
                in_stock=reading.in_stock,
                out_of_stock_since=None if reading.in_stock else now(),
            )
            session.add(item)
            session.flush()
            record(session, item)
            # The first read records what is already there: it is not news.
            if not first and reading.in_stock and (hits := matching_rules(rules, item.title)):
                emit(session, item, "outlet_new", f"NEW IN OUTLET · matches {', '.join(hits)}")
            continue
        was_in_stock, old = item.in_stock, item.price
        item.title, item.url, item.original_price = (
            reading.title,
            reading.url,
            reading.original_price,
        )
        item.last_seen_at = now()
        if reading.price != old:
            item.previous_price, item.price = old, reading.price
        if reading.price is not None and (
            item.lowest_price is None or reading.price < item.lowest_price
        ):
            item.lowest_price = reading.price
        item.in_stock = reading.in_stock
        if reading.in_stock and not was_in_stock:
            item.out_of_stock_since = None
            hits = matching_rules(rules, item.title)
            if item.belled or hits:
                why = f" · matches {', '.join(hits)}" if hits and not item.belled else ""
                emit(session, item, "outlet_back", f"BACK IN OUTLET{why}")
        elif was_in_stock and not reading.in_stock:
            item.out_of_stock_since = now()
            if item.belled:
                emit(session, item, "outlet_out_of_stock", "OUT OF STOCK")
        elif (
            reading.in_stock
            and item.belled
            and old is not None
            and reading.price is not None
            and reading.price < old
        ):
            emit(session, item, "outlet_price_drop", f"PRICE DROP · was {price_text(old)}")
        if reading.price != old or reading.in_stock != was_in_stock:
            record(session, item)
    for item in items.values():
        if item.item_id in seen or not item.in_stock:
            continue
        item.in_stock, item.out_of_stock_since = False, now()
        record(session, item)
        if item.belled:
            emit(session, item, "outlet_out_of_stock", "OUT OF STOCK")
    feed.last_read_at, feed.last_error = now(), None


def record(session, item: OutletItem):
    session.add(OutletPrice(item_id=item.id, price=item.price, in_stock=item.in_stock))


class OutletService:
    def __init__(self, db, registry, notifications):
        self.db, self.registry, self.notifications = db, registry, notifications
        self.lock = asyncio.Lock()

    async def run(self, feed_id: int | None = None, *, force: bool = False):
        """Read due feeds; a scheduled run skips while another is going, a user's waits."""
        if self.lock.locked() and not force:
            return
        async with self.lock:
            with self.db.session() as session:
                query = select(OutletFeed.id).order_by(OutletFeed.next_check_at)
                if feed_id is not None:
                    query = query.where(OutletFeed.id == feed_id)
                if not force:
                    query = query.where(OutletFeed.next_check_at <= now())
                ids = session.scalars(query).all()
            for one in ids:
                await self.read(one)
            await self.notifications.deliver()

    def overview(self) -> dict:
        with self.db.session() as session:
            feeds = session.scalars(select(OutletFeed).order_by(OutletFeed.id)).all()
            rules = session.scalars(select(OutletRule).order_by(OutletRule.id)).all()
            # In stock first, newest arrivals on top; what left the outlet last sinks to the end.
            items = sorted(
                (item for feed in feeds for item in feed.items),
                key=lambda item: (
                    not item.in_stock,
                    -(
                        item.first_seen_at
                        if item.in_stock
                        else item.out_of_stock_since or item.first_seen_at
                    ).timestamp(),
                ),
            )
        matches = {
            rule.id: sum(
                1 for item in items if item.in_stock and matching_rules([rule], item.title)
            )
            for rule in rules
        }
        return {"feeds": feeds, "items": items, "rules": rules, "matches": matches}

    async def read(self, feed_id: int):
        with self.db.session() as session:
            feed = session.get(OutletFeed, feed_id)
            if feed is None:
                return
            retailer, url = feed.retailer, feed.url
        error, found = None, []
        try:
            found = await self.registry[retailer].fetch_feed(url)
        except ScrapeError as exc:
            error = str(exc)
        except Exception as exc:
            error = "Adapter failed; inspect logs or retry later"
            log.warning(
                "outlet_read_failed", extra={"retailer": retailer, "error_type": type(exc).__name__}
            )
        with self.db.session() as session:
            feed = session.get(OutletFeed, feed_id)
            if feed is None:
                return
            feed.next_check_at = now() + timedelta(
                minutes=feed.check_interval_minutes * random.uniform(0.95, 1.05)
            )
            if error is None:
                try:
                    apply(session, feed, found)
                except ScrapeError as exc:
                    error = str(exc)
            if error is not None:
                feed.last_error = error
        log.info("outlet_read", extra={"retailer": retailer, "count": len(found)})
