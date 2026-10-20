import asyncio
import logging
import random
import re
import unicodedata
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from app.models import Alert, OutletFeed, OutletItem, OutletPrice, OutletRule
from app.retailers.parsing import ScrapeError
from app.schemas.domain import FeedItem, now
from app.services.matching import normalize

log = logging.getLogger(__name__)

# Sortable columns of the tab, by the item attribute they sort on.
SORTS = {"price": "deal_price", "size": "size"}
WORD = re.compile(r"[A-Z0-9]+")
# Products that arrived after a page's first read and within this long are marked new.
NEW_FOR = timedelta(days=2)
EMPTY_READ = "The page listed no products; keeping the last stock until it lists some again"


def mentions(title: str, text: str) -> bool:
    """Whether the title has the text starting at the beginning of one of its words.

    Spaces and punctuation are ignored, so "85C7L" finds "TCL 85 C7L" and "LG 55" finds
    "LG 55QN8500", but "85" does not find "TQ65S85D" or "165 cm".
    """
    wanted = normalize(text)
    words = WORD.findall(unicodedata.normalize("NFKD", title).upper())
    joined, start = "".join(words), 0
    for word in words:
        if joined.startswith(wanted, start):
            return True
        start += len(word)
    return False


def matching_rules(rules: list[OutletRule], title: str) -> list[str]:
    # A rule saved twice in different spelling ("85c7l", "85 C7L") is one match.
    hits = {normalize(rule.text): rule.text for rule in rules if mentions(title, rule.text)}
    return list(hits.values())


@dataclass
class Filters:
    """What the table shows; every field narrows it, empty ones are ignored."""

    q: str = ""
    stock: str = ""
    grade: str = ""
    min_size: Decimal | None = None
    max_size: Decimal | None = None
    max_price: Decimal | None = None
    belled: bool = False
    alerted: bool = False

    def keeps(self, item: OutletItem, hits: list[str]) -> bool:
        size, price = item.size, item.deal_price
        return (
            all(mentions(item.title, term) for term in self.q.split())
            and (self.stock != "in" or item.in_stock)
            and (self.stock != "out" or not item.in_stock)
            and (not self.grade or item.grade == self.grade)
            and (self.min_size is None or (size is not None and size >= self.min_size))
            and (self.max_size is None or (size is not None and size <= self.max_size))
            and (self.max_price is None or (price is not None and price <= self.max_price))
            and (not self.belled or item.belled)
            and (not self.alerted or bool(hits))
        )

    @property
    def active(self) -> bool:
        return self != Filters()


def emit(session, item: OutletItem, event: str, message: str):
    session.add(
        Alert(
            outlet_item_id=item.id,
            event_type=event,
            old_price=item.previous_price,
            new_price=item.deal_price,
            message=f"{message} · {item.title} · {offer_text(item)}",
        )
    )
    log.info("alert_triggered", extra={"event_type": event})


def price_text(price: Decimal | None) -> str:
    return f"{price} EUR" if price is not None else "no price"


def offer_text(item: OutletItem) -> str:
    text = price_text(item.deal_price)
    if item.promo_price is not None and item.deal_price == item.promo_price:
        text += f" with code {item.promo_code}" if item.promo_code else " with coupon"
    return text


def apply(session, feed: OutletFeed, found: list[FeedItem]):
    """Fold a complete read of the feed into its items and raise the alerts it calls for.

    Prices compared, kept as previous/lowest and alerted on are deal prices: a coupon the
    card advertises counts like any other discount.
    """
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
                promo_price=reading.promo_price,
                promo_code=reading.promo_code,
                lowest_price=reading.deal_price,
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
        was_in_stock, old = item.in_stock, item.deal_price
        changed = (item.price, item.promo_price) != (reading.price, reading.promo_price)
        item.title, item.url, item.original_price = (
            reading.title,
            reading.url,
            reading.original_price,
        )
        item.price, item.promo_price, item.promo_code = (
            reading.price,
            reading.promo_price,
            reading.promo_code,
        )
        item.last_seen_at = now()
        new = reading.deal_price
        if new != old:
            item.previous_price = old
        if new is not None and (item.lowest_price is None or new < item.lowest_price):
            item.lowest_price = new
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
        elif reading.in_stock and item.belled and old is not None and new is not None and new < old:
            emit(session, item, "outlet_price_drop", f"PRICE DROP · was {price_text(old)}")
        if changed or reading.in_stock != was_in_stock:
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
    session.add(
        OutletPrice(
            item_id=item.id,
            price=item.price,
            promo_price=item.promo_price,
            in_stock=item.in_stock,
        )
    )


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

    def overview(self, sort: str = "", filters: Filters | None = None) -> dict:
        """The tab's contents; sort is "price" or "size", "-" first for high to low."""
        filters = filters or Filters()
        with self.db.session() as session:
            feeds = session.scalars(select(OutletFeed).order_by(OutletFeed.id)).all()
            rules = session.scalars(select(OutletRule).order_by(OutletRule.id)).all()
            everything = [item for feed in feeds for item in feed.items]
        # In stock first, newest arrivals on top; what left the outlet last sinks to the end.
        items = sorted(
            everything,
            key=lambda item: (
                not item.in_stock,
                -(
                    item.first_seen_at
                    if item.in_stock
                    else item.out_of_stock_since or item.first_seen_at
                ).timestamp(),
            ),
        )
        if field := SORTS.get(sort.removeprefix("-")):
            known = sorted(
                (item for item in items if getattr(item, field) is not None),
                key=lambda item: getattr(item, field),
                reverse=sort.startswith("-"),
            )
            # Stable: out-of-stock products stay at the bottom, unknown values last in each part.
            items = sorted(
                known + [item for item in items if getattr(item, field) is None],
                key=lambda item: not item.in_stock,
            )
        hits = {item.id: matching_rules(rules, item.title) for item in everything}
        in_stock = [item for item in everything if item.in_stock]
        # The first read records what was already there; only later arrivals are new.
        first_read = {
            feed.id: min((item.first_seen_at for item in feed.items), default=now())
            for feed in feeds
        }
        fresh = {
            item.id
            for item in in_stock
            if item.first_seen_at > first_read[item.feed_id] + timedelta(minutes=10)
            and item.first_seen_at >= now() - NEW_FOR
        }
        # A product listed on two followed pages is still one product.
        products = {item.item_id: item for item in in_stock}.values()
        return {
            "feeds": feeds,
            "items": [item for item in items if filters.keeps(item, hits[item.id])],
            "total": len(everything),
            "counts": {
                feed.id: (sum(1 for item in feed.items if item.in_stock), len(feed.items))
                for feed in feeds
            },
            "rules": rules,
            "matches": {
                rule.id: sum(1 for item in products if mentions(item.title, rule.text))
                for rule in rules
            },
            "hits": hits,
            "fresh": fresh,
            "stats": {
                "in_stock": len(products),
                "new": len(fresh),
                "belled": sum(1 for item in everything if item.belled),
                "alerted": sum(1 for item in products if hits[item.id]),
            },
            "sizes": sorted({item.size for item in everything if item.size is not None}),
            "grades": sorted({item.grade for item in everything if item.grade}),
            "sort": sort if field else "",
            "filters": filters,
        }

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
