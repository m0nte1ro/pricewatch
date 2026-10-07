import json
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.database import Base, Database
from app.models import Listing, PriceHistory, Product, StoreRule
from app.retailers.http import Fetcher
from app.retailers.registry import Registry
from app.schemas.domain import now
from app.services.listings import add_candidates, record_snapshot
from app.services.transfer import TransferError, export_data, import_data


@pytest.fixture
def source(db, candidate):
    """A watchlist with a product, two listings, some history and a store rule."""
    with db.session() as session:
        product = Product(
            canonical_name="TCL 85C7K",
            brand="TCL",
            model="85C7K",
            size="85",
            category="tv",
            target_price=Decimal("900"),
            insane_deal_price=Decimal("800"),
        )
        session.add(product)
        session.flush()
        second = candidate.model_copy(deep=True)
        second.listing.retailer, second.listing.url = "darty", "https://www.darty.pt/products/x"
        second.listing.retailer_product_id = "T1"
        add_candidates(session, product, [candidate, second])
        listing = session.scalar(select(Listing).where(Listing.retailer == "worten"))
        later = candidate.listing.model_copy(
            update={"price": Decimal("999.00"), "observed_at": now() + timedelta(hours=1)}
        )
        record_snapshot(session, product, listing, later)
        session.add(StoreRule(host="storeone.pt", price_selector="span.price-current"))
    return db


def fresh():
    database = Database("sqlite://")
    Base.metadata.create_all(database.engine)
    return database


def registry():
    return Registry(Fetcher(None, None))


def counts(db):
    with db.session() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (Product, Listing, PriceHistory, StoreRule)
        )


def test_export_then_import_recreates_the_watchlist(source):
    data = export_data(source)
    assert data["format"] == "pricewatch-export" and data["version"] == 1
    target = fresh()
    result = import_data(target, registry(), json.loads(json.dumps(data)))
    assert (result.products, result.listings, result.history, result.rules) == (1, 2, 3, 1)
    assert counts(target) == counts(source) == (1, 2, 3, 1)
    # Same content both sides: the target's own export matches, apart from the timestamp.
    again = export_data(target)
    assert again["products"] == data["products"] and again["store_rules"] == data["store_rules"]
    with target.session() as session:
        listing = session.scalar(select(Listing).where(Listing.retailer == "worten"))
        assert (listing.current_price, listing.previous_price) == (
            Decimal("999.00"),
            Decimal("1199.00"),
        )
        assert listing.next_check_at <= now() + timedelta(minutes=1)  # checked soon


def test_importing_the_same_file_twice_changes_nothing(source):
    data = json.loads(json.dumps(export_data(source)))
    target = fresh()
    import_data(target, registry(), data)
    before = export_data(target)
    result = import_data(target, registry(), data)
    assert (result.products, result.listings, result.history, result.rules) == (0, 0, 0, 0)
    after = export_data(target)
    assert after["products"] == before["products"]
    assert counts(target) == (1, 2, 3, 1)
    # Importing a watchlist back into its own database is a no-op too.
    assert import_data(source, registry(), data).products == 0
    assert counts(source) == (1, 2, 3, 1)


def test_import_never_changes_an_existing_product(source):
    data = json.loads(json.dumps(export_data(source)))
    target = fresh()
    import_data(target, registry(), data)
    with target.session() as session:
        session.scalar(select(Product)).target_price = Decimal("700")
    data["products"][0]["target_price"] = "650.00"
    import_data(target, registry(), data)
    with target.session() as session:
        assert session.scalar(select(Product)).target_price == Decimal("700")


def test_import_adds_only_what_is_missing(source):
    data = json.loads(json.dumps(export_data(source)))
    target = fresh()
    partial = json.loads(json.dumps(data))
    partial["products"][0]["listings"] = partial["products"][0]["listings"][:1]
    partial["products"][0]["listings"][0]["history"] = partial["products"][0]["listings"][0][
        "history"
    ][:1]
    import_data(target, registry(), partial)
    result = import_data(target, registry(), data)
    assert (result.products, result.listings, result.history) == (0, 1, 2)
    assert counts(target) == (1, 2, 3, 1)


def test_import_refuses_links_that_could_not_be_pasted(source):
    data = json.loads(json.dumps(export_data(source)))
    data["products"][0]["listings"][0]["url"] = "https://192.168.1.10/admin"
    result = import_data(fresh(), registry(), data)
    assert result.listings == 1
    assert any("192.168.1.10" in note for note in result.notes)


@pytest.mark.parametrize(
    "data",
    [{"format": "something-else", "version": 1}, {"format": "pricewatch-export", "version": 99}],
)
def test_import_rejects_files_it_does_not_understand(data):
    with pytest.raises(TransferError):
        import_data(fresh(), registry(), data)
