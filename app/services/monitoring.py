import asyncio
import logging
import random
from datetime import timedelta

from sqlalchemy import select

from app.models import Listing, Product
from app.schemas.domain import Identity, now
from app.services.listings import record_snapshot
from app.services.matching import match_identity, normalize

log = logging.getLogger(__name__)


class MonitoringService:
    def __init__(self, db, registry, settings, notifications):
        self.db, self.registry, self.settings, self.notifications = (
            db,
            registry,
            settings,
            notifications,
        )
        self.lock = asyncio.Lock()
        self.last_run = None
        self.last_summary = "No checks yet"

    async def run(self, product_id: int | None = None, *, force: bool = False):
        if self.lock.locked():
            return
        async with self.lock:
            prefs = self.settings.get()
            with self.db.session() as session:
                query = (
                    select(Listing.id)
                    .join(Product)
                    .where(
                        Listing.enabled.is_(True),
                        Product.enabled.is_(True),
                        Product.archived.is_(False),
                        Listing.retailer.in_(prefs.enabled_retailers),
                    )
                )
                if product_id is not None:
                    query = query.where(Product.id == product_id)
                if not force:
                    query = query.where(Listing.next_check_at <= now())
                ids = list(session.scalars(query.order_by(Listing.next_check_at)))
            log.info("monitoring_started", extra={"count": len(ids)})
            semaphore = asyncio.Semaphore(3)

            async def bounded(listing_id):
                async with semaphore:
                    await self.check(listing_id)

            for offset in range(0, len(ids), 50):
                await asyncio.gather(*(bounded(i) for i in ids[offset : offset + 50]))
            self.last_run, self.last_summary = now(), f"Checked {len(ids)} listings"
            await self.notifications.deliver()

    async def check(self, listing_id: int):
        prefs = self.settings.get()
        with self.db.session() as session:
            listing = session.get(Listing, listing_id)
            product = listing.product
            identity = Identity(
                name=product.canonical_name,
                brand=product.brand,
                model=product.model,
                size=product.size,
                category=product.category,
                identifiers=product.specifications,
            )
        error, snapshot = None, None
        try:
            snapshots = await self.registry.adapters[listing.retailer].fetch_listing(listing.url)
            snapshot = next(
                (
                    s
                    for s in snapshots
                    if str(s.condition) == listing.condition
                    and normalize(s.seller) == listing.seller_key
                ),
                None,
            )
            level = (
                match_identity(identity, snapshot.identity, snapshot.condition).level
                if snapshot
                else None
            )
            if snapshot is None:
                error = "Original seller/condition offer is missing. Rediscover to review changed offers."
            elif level == "CONFLICT":
                error = "Product identity changed on retailer page; review required"
            elif level == "LOW" and normalize(snapshot.title) != normalize(listing.title):
                error = "Product identity can no longer be verified; review required"
        except Exception as exc:
            from app.retailers.parsing import ScrapeError

            error = (
                str(exc)
                if isinstance(exc, ScrapeError)
                else "Adapter failed; inspect logs or retry later"
            )
            log.warning(
                "listing_check_failed",
                extra={"listing_id": listing_id, "error_type": type(exc).__name__},
            )
        with self.db.session() as session:
            row = session.get(Listing, listing_id)
            if not row.enabled or not row.product.enabled or row.product.archived:
                return
            interval = prefs.retailer_intervals.get(row.retailer, prefs.polling_minutes)
            row.last_checked_at = now()
            row.next_check_at = now() + timedelta(minutes=interval * random.uniform(0.95, 1.05))
            if error:
                row.last_error = error
            else:
                record_snapshot(session, row.product, row, snapshot)
