from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select

from app.models import Alert, Listing, PriceHistory, Product
from app.schemas.domain import now
from app.services.listings import add_candidates, record_snapshot
from app.services.queries import QueryService, summary


def create_product(db, candidate, conditions=None):
    with db.session() as session:
        product = Product(
            canonical_name="TCL 85C7K",
            brand="TCL",
            model="85C7K",
            size="85",
            category="tv",
            target_price=Decimal("900"),
            insane_deal_price=Decimal("800"),
            allowed_conditions=conditions or ["new"],
        )
        session.add(product)
        session.flush()
        add_candidates(session, product, [candidate])
        return product.id


def events(db):
    with db.session() as session:
        return list(session.scalars(select(Alert.event_type)))


def update(db, snapshot):
    with db.session() as session:
        listing = session.scalar(select(Listing))
        record_snapshot(session, listing.product, listing, snapshot)


def test_history_changes_and_daily_sample(db, candidate):
    create_product(db, candidate)
    snapshot = candidate.listing.model_copy(deep=True)
    update(db, snapshot)
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(PriceHistory)) == 1
    snapshot.observed_at += timedelta(days=1, seconds=1)
    update(db, snapshot)
    snapshot.price = Decimal("999")
    update(db, snapshot)
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(PriceHistory)) == 3
        assert session.scalar(select(Listing)).previous_price == Decimal("1199")


def test_threshold_crossings_are_not_repeated(db, candidate):
    create_product(db, candidate)
    snapshot = candidate.listing.model_copy(deep=True)
    snapshot.price = Decimal("789")
    update(db, snapshot)
    update(db, snapshot)
    assert events(db) == ["new_listing", "price_dropped", "target_hit", "insane_deal"]
    snapshot.price = Decimal("1200")
    update(db, snapshot)
    snapshot.price = Decimal("800")
    update(db, snapshot)
    assert events(db).count("target_hit") == 2


def test_disallowed_grade_c_never_triggers_deal(db, candidate):
    candidate.listing.condition = "outlet_grade_c"
    candidate.listing.price = Decimal("500")
    create_product(db, candidate, conditions=["new", "outlet_grade_a"])
    assert events(db) == ["outlet_listing"]


def test_allowed_grade_a_and_initial_threshold(db, candidate):
    candidate.listing.condition = "outlet_grade_a"
    candidate.listing.price = Decimal("789")
    create_product(db, candidate, conditions=["new", "outlet_grade_a"])
    assert events(db) == ["outlet_listing", "target_hit", "insane_deal"]
    with db.session() as session:
        states = dict(session.execute(select(Alert.event_type, Alert.notification_state)).all())
    # The user just confirmed the listing: record it, but only push the price alerts.
    assert states == {
        "outlet_listing": "skipped",
        "target_hit": "pending",
        "insane_deal": "pending",
    }


def test_availability_transition_and_currency_safety(db, candidate):
    product_id = create_product(db, candidate)
    snapshot = candidate.listing.model_copy(deep=True)
    snapshot.availability = "out_of_stock"
    snapshot.price = Decimal("100")
    update(db, snapshot)
    assert events(db)[-1] == "became_unavailable"
    assert "insane_deal" not in events(db)
    assert QueryService(db).detail(product_id)["status"] == "OUT OF STOCK"
    snapshot.availability = "in_stock"
    snapshot.currency = "USD"
    update(db, snapshot)
    assert "became_available" in events(db)
    assert "insane_deal" not in events(db)


def test_persistence_dedup_preserves_source_and_history(db, candidate):
    product_id = create_product(db, candidate)
    candidate.sources = ["discovered"]
    with db.session() as session:
        add_candidates(session, session.get(Product, product_id), [candidate])
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(Listing)) == 1
        assert session.scalar(select(func.count()).select_from(PriceHistory)) == 1
        assert session.scalar(select(Listing)).sources == ["discovered", "manual"]


async def test_scheduled_due_check_and_blocked_retailer_isolation(site):
    _, runtime, prices, _ = site
    from tests.test_web import payload

    draft = runtime.discovery.create(payload())
    await runtime.discovery.run(draft)
    runtime.discovery.confirm(draft, [0, 1, 2, 3, 4])
    prices["worten"] = "blocked"
    prices["fnac"] = "799"
    with runtime.db.session() as session:
        for listing in session.scalars(select(Listing)):
            listing.next_check_at = now() - timedelta(minutes=1)
    await runtime.monitor.run()  # same coroutine APScheduler invokes, using persisted due times
    with runtime.db.session() as session:
        rows = {x.retailer: x for x in session.scalars(select(Listing))}
        assert rows["worten"].last_error
        assert rows["worten"].current_price == Decimal("1199")
        assert rows["fnac"].current_price == Decimal("799")
        assert rows["fnac"].next_check_at > now()
        assert session.scalar(select(func.count()).select_from(PriceHistory)) == 6
    assert "insane_deal" in events(runtime.db)


def test_out_of_stock_deal_price_is_recorded_but_never_alerts(db, candidate):
    candidate.listing.condition = "outlet_grade_a"
    candidate.listing.price = Decimal("783.57")
    candidate.listing.availability = "out_of_stock"
    product_id = create_product(db, candidate, conditions=["new", "outlet_grade_a"])
    assert events(db) == ["outlet_listing"]
    with db.session() as session:
        product = session.get(Product, product_id)
        item = summary(product)
        assert item["status"] == "OUT OF STOCK"
        assert item["best"] is None
        assert item["unavailable"].current_price == Decimal("783.57")
        history = session.scalar(select(PriceHistory))
        assert (history.price, history.availability) == (Decimal("783.57"), "out_of_stock")
    # Back in stock at the same price: now it is a real deal.
    snapshot = candidate.listing.model_copy(update={"availability": "in_stock"})
    update(db, snapshot)
    assert events(db) == [
        "outlet_listing",
        "became_available",
        "target_hit",
        "insane_deal",
    ]
