import asyncio
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import func, select

from app.config import Config
from app.models import Alert, Listing, PriceHistory, Product
from app.runtime import Runtime
from app.schemas.domain import now
from app.services.listings import add_candidates


async def test_real_scheduler_writes_history_and_alerts(db, candidate, tmp_path):
    runtime = Runtime(Config(data_dir=tmp_path, scheduler_enabled=True), db)
    with db.session() as session:
        product = Product(
            canonical_name="TCL 85C7K",
            brand="TCL",
            model="85C7K",
            category="tv",
            target_price=Decimal("900"),
            insane_deal_price=Decimal("800"),
            allowed_conditions=["new"],
        )
        session.add(product)
        session.flush()
        add_candidates(session, product, [candidate])
        session.scalar(select(Listing)).next_check_at = now() - timedelta(minutes=1)

    async def fetch(url):
        snapshot = candidate.listing.model_copy(deep=True)
        snapshot.price = Decimal("789")
        snapshot.observed_at = now()
        return [snapshot]

    runtime.registry.adapters["worten"] = SimpleNamespace(fetch_listing=fetch, searchable=True)
    runtime.start()
    try:
        runtime.scheduler.modify_job("price-checks", next_run_time=now())
        for _ in range(100):
            if runtime.monitor.last_run:
                break
            await asyncio.sleep(0.02)
        assert runtime.monitor.last_run is not None
        with db.session() as session:
            assert session.scalar(select(func.count()).select_from(PriceHistory)) == 2
            assert session.scalar(select(Listing)).current_price == Decimal("789")
            assert set(session.scalars(select(Alert.event_type))) >= {
                "price_dropped",
                "target_hit",
                "insane_deal",
            }
            assert session.scalar(select(Listing)).next_check_at > now()
    finally:
        await runtime.close()
