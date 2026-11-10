import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select, update

from app.models import Alert, MarketListing, MarketSearch
from app.retailers.parsing import ScrapeError
from app.schemas.domain import MarketItem, now
from app.services.outlet import mentions, price_text

log = logging.getLogger(__name__)

# Listings announced within this long are marked new on the tab.
NEW_FOR = timedelta(days=2)
# Listings kept per search. Reads see only the newest page, so older listings never come back.
KEEP = 500


@dataclass
class Filters:
    """What the table shows; every field narrows it, empty ones are ignored."""

    q: str = ""
    search: int | None = None
    max_price: Decimal | None = None
    fresh: bool = False
    hide_reserved: bool = False

    def keeps(self, row: MarketListing, fresh: set[int]) -> bool:
        return (
            all(mentions(row.title, term) for term in self.q.split())
            and (self.search is None or row.search_id == self.search)
            and (self.max_price is None or (row.price is not None and row.price <= self.max_price))
            and (not self.fresh or row.id in fresh)
            and (not self.hide_reserved or not row.reserved)
        )

    @property
    def active(self) -> bool:
        return self != Filters()


def apply(session, search: MarketSearch, label: str, found: list[MarketItem]):
    """Fold a read of the search into its listings; one not seen before is news.

    The first read records what is already there without alerting.
    """
    rows = {row.listing_id: row for row in search.listings}
    first = search.last_read_at is None
    for reading in found:
        row = rows.get(reading.listing_id)
        if row is None:
            row = rows[reading.listing_id] = MarketListing(
                search=search,
                listing_id=reading.listing_id,
                title=reading.title,
                url=reading.url,
                price=reading.price,
                image_url=reading.image_url,
                place=reading.place,
                posted_at=reading.posted_at,
                reserved=reading.reserved,
                ships=reading.ships,
                announced=not first,
            )
            session.add(row)
            session.flush()
            if not first:
                place = f" · {row.place}" if row.place else ""
                session.add(
                    Alert(
                        market_listing_id=row.id,
                        event_type="market_new",
                        new_price=row.price,
                        message=f"NEW ON {label.upper()} · {row.title} · {price_text(row.price)}{place} · {search.label}",
                    )
                )
                log.info("alert_triggered", extra={"event_type": "market_new"})
            continue
        if reading.price != row.price and row.price is not None:
            row.previous_price = row.price
        row.title, row.url, row.price = reading.title, reading.url, reading.price
        row.image_url = reading.image_url or row.image_url
        row.place, row.reserved, row.ships = reading.place, reading.reserved, reading.ships
        row.last_seen_at = now()
    prune(session, search)
    search.last_read_at, search.last_error = now(), None


def prune(session, search: MarketSearch):
    rows = sorted(search.listings, key=lambda row: (row.first_seen_at, row.id), reverse=True)
    stale = rows[KEEP:]
    if not stale:
        return
    # Their alerts stay in Activity without the listing.
    session.execute(
        update(Alert)
        .where(Alert.market_listing_id.in_([row.id for row in stale]))
        .values(market_listing_id=None)
    )
    for row in stale:
        search.listings.remove(row)


class MarketService:
    def __init__(self, db, marketplaces, notifications):
        self.db, self.marketplaces, self.notifications = db, marketplaces, notifications
        self.lock = asyncio.Lock()

    async def run(self, search_id: int | None = None, *, force: bool = False):
        """Read due searches; a scheduled run skips while another is going, a user's waits."""
        if self.lock.locked() and not force:
            return
        async with self.lock:
            with self.db.session() as session:
                query = select(MarketSearch.id).order_by(MarketSearch.next_check_at)
                if search_id is not None:
                    query = query.where(MarketSearch.id == search_id)
                if not force:
                    query = query.where(MarketSearch.next_check_at <= now())
                ids = session.scalars(query).all()
            for one in ids:
                await self.read(one)
            await self.notifications.deliver()

    def overview(self, sort: str = "", filters: Filters | None = None) -> dict:
        """The tab's contents; newest posted first, or by price ("-price" high to low)."""
        filters = filters or Filters()
        with self.db.session() as session:
            searches = session.scalars(select(MarketSearch).order_by(MarketSearch.id)).all()
            everything = [row for search in searches for row in search.listings]
        fresh = {
            row.id for row in everything if row.announced and row.first_seen_at >= now() - NEW_FOR
        }
        # A listing two searches find is still one listing: keep the first search's row.
        provider = {search.id: search.provider for search in searches}
        rows = list(
            {
                (provider[row.search_id], row.listing_id): row for row in reversed(everything)
            }.values()
        )
        rows.sort(key=lambda row: row.posted_at or row.first_seen_at, reverse=True)
        if sort.removeprefix("-") == "price":
            rows = sorted(
                (row for row in rows if row.price is not None),
                key=lambda row: row.price,
                reverse=sort.startswith("-"),
            ) + [row for row in rows if row.price is None]
        else:
            sort = ""
        return {
            "searches": searches,
            "labels": {search.id: search.label for search in searches},
            "items": [row for row in rows if filters.keeps(row, fresh)],
            "total": len(rows),
            "counts": {search.id: len(search.listings) for search in searches},
            "fresh": fresh,
            "stats": {"listings": len(rows), "new": len({row.id for row in rows} & fresh)},
            "sort": sort,
            "filters": filters,
        }

    async def read(self, search_id: int):
        with self.db.session() as session:
            search = session.get(MarketSearch, search_id)
            if search is None:
                return
            name, url = search.provider, search.url
        provider = self.marketplaces[name]
        error, found = None, []
        try:
            found = await provider.fetch_listings(url)
        except ScrapeError as exc:
            error = str(exc)
        except Exception as exc:
            error = "Provider failed; inspect logs or retry later"
            log.warning(
                "market_read_failed", extra={"retailer": name, "error_type": type(exc).__name__}
            )
        with self.db.session() as session:
            search = session.get(MarketSearch, search_id)
            if search is None:
                return
            search.next_check_at = now() + timedelta(
                minutes=search.check_interval_minutes * random.uniform(0.95, 1.05)
            )
            if error is None:
                apply(session, search, provider.label, found)
            else:
                search.last_error = error
        log.info("market_read", extra={"retailer": name, "count": len(found)})
