import json
from datetime import datetime
from decimal import Decimal
from urllib.parse import parse_qsl

import httpx
import pytest
from sqlalchemy import select

import app.services.market as market
from app.marketplaces.registry import Marketplaces
from app.marketplaces.wallapop import CHANGED
from app.models import Alert, MarketListing, MarketSearch
from app.retailers.http import Fetcher
from app.retailers.parsing import ScrapeError
from app.schemas.domain import MarketItem, Preferences
from app.services.market import Filters, apply
from app.services.notifications import NotificationService
from app.services.settings import SettingsService
from tests.conftest import FIXTURES
from tests.test_web import form_data

SEARCH_URL = "https://es.wallapop.com/search?keywords=ps5&max_sale_price=300"
SEARCH_JSON = (FIXTURES / "wallapop_search_live.json").read_text()


def wallapop(db=None, respond=None):
    fetcher = Fetcher(
        db,
        lambda: Preferences(),
        transport=httpx.MockTransport(respond) if respond else None,
        min_delay=0,
    )
    return Marketplaces(fetcher)["wallapop"]


def found(listing_id: str, title: str = "PS5 Slim", price: str = "250") -> MarketItem:
    return MarketItem(
        listing_id=listing_id,
        title=title,
        url=f"https://es.wallapop.com/item/{listing_id}",
        price=Decimal(price),
        place="Madrid",
    )


@pytest.fixture
def search(db):
    with db.session() as session:
        row = MarketSearch(provider="wallapop", url=SEARCH_URL, label="ps5 · up to €300")
        session.add(row)
    return row.id


def read(db, search_id: int, listings: list[MarketItem]):
    with db.session() as session:
        apply(session, session.get(MarketSearch, search_id), "Wallapop", listings)


def rows(db) -> dict[str, MarketListing]:
    with db.session() as session:
        return {row.listing_id: row for row in session.scalars(select(MarketListing))}


def alerts(db) -> list[tuple[str, str]]:
    with db.session() as session:
        return [
            (a.event_type, a.message) for a in session.scalars(select(Alert).order_by(Alert.id))
        ]


def test_a_wallapop_search_link_keeps_its_filters_and_drops_order_and_tracking():
    provider = wallapop()
    url = "https://pt.wallapop.com/app/search?keywords=ps5&order_by=price_low_to_high&min_sale_price=100&latitude=38.72&longitude=-9.14&search_id=abc&utm_source=x"
    assert provider.normalize_url(url) == (
        "https://pt.wallapop.com/search?keywords=ps5&latitude=38.72&longitude=-9.14&min_sale_price=100"
    )
    assert provider.normalize_url("https://wallapop.com/search?keywords=ps5") == (
        "https://es.wallapop.com/search?keywords=ps5"
    )


def test_a_wallapop_link_that_is_not_a_search_is_refused():
    provider = wallapop()
    with pytest.raises(ScrapeError, match="Use a Wallapop search link"):
        provider.normalize_url("https://es.wallapop.com/item/ps5-slim-1323061913")
    with pytest.raises(ScrapeError, match="keywords or a category"):
        provider.normalize_url("https://es.wallapop.com/search?min_sale_price=100")
    with pytest.raises(ScrapeError, match="supported domains"):
        provider.normalize_url("http://es.wallapop.com/search?keywords=ps5")


def test_a_search_is_named_by_its_keywords_and_price_range():
    provider = wallapop()
    assert provider.describe(SEARCH_URL) == "ps5 · up to €300"
    assert provider.describe(SEARCH_URL + "&min_sale_price=100.50") == "ps5 · €100.5–€300"
    assert provider.describe("https://es.wallapop.com/search?category_ids=24200") == "a category"


def test_only_known_marketplaces_are_followed():
    with pytest.raises(ScrapeError, match="Paste a search link from Wallapop"):
        Marketplaces(Fetcher(None, None)).for_url("https://www.olx.pt/ads/q-ps5/")


async def test_wallapop_is_read_newest_first_from_madrid_unless_the_link_says_where(db):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, text=SEARCH_JSON)

    provider = wallapop(db, respond)
    listings = await provider.fetch_listings(SEARCH_URL)
    sent = dict(parse_qsl(requests[0].url.query.decode()))
    assert requests[0].url.host == "api.wallapop.com"
    assert requests[0].headers["X-DeviceOS"] == "0"
    assert sent == {
        "keywords": "ps5",
        "max_sale_price": "300",
        "latitude": "40.41956",
        "longitude": "-3.69196",
        "order_by": "newest",
        "source": "search_box",
    }
    await provider.fetch_listings(
        "https://pt.wallapop.com/search?keywords=ps5&latitude=38.72&longitude=-9.14"
    )
    sent = dict(parse_qsl(requests[1].url.query.decode()))
    assert (sent["latitude"], sent["longitude"]) == ("38.72", "-9.14")
    assert [item.listing_id for item in listings] == [
        "pj943gwle26e",
        "e65qx3nl7m6o",
        "wzvgwlo59qzl",
    ]


async def test_a_wallapop_listing_reads_title_price_place_photo_and_reservation(db):
    provider = wallapop(db, lambda request: httpx.Response(200, text=SEARCH_JSON))
    first, edge = (await provider.fetch_listings(SEARCH_URL))[:2]
    assert first.title == "Hellraiser Revival Day One Edition NOVO LACREADO"
    assert first.url == (
        "https://es.wallapop.com/item/hellraiser-revival-day-one-edition-nuevo-precintad-1323061913"
    )
    assert (first.price, first.place, first.reserved, first.ships) == (
        Decimal("60.00"),
        "Barcelona",
        False,
        True,
    )
    assert first.image_url.startswith("https://cdn.wallapop.com/images/")
    assert first.posted_at == datetime(2026, 11, 10, 23, 18, 5, 667000)
    assert (edge.title, edge.price, edge.reserved) == (
        "Controle DualSense Edge para PS5",
        Decimal("108.00"),
        True,
    )


async def test_a_changed_wallapop_answer_fails_the_read_instead_of_listing_nothing(db):
    provider = wallapop(db, lambda request: httpx.Response(200, json={"data": {"items": []}}))
    with pytest.raises(ScrapeError, match=CHANGED):
        await provider.fetch_listings(SEARCH_URL)


def test_the_first_read_records_listings_without_alerting(db, search):
    read(db, search, [found("a"), found("b")])
    assert set(rows(db)) == {"a", "b"}
    assert not any(row.announced for row in rows(db).values())
    assert alerts(db) == []


def test_a_listing_not_seen_before_is_pushed_as_new(db, search):
    read(db, search, [found("a")])
    read(db, search, [found("b", "PS5 Pro", "420"), found("a")])
    assert alerts(db) == [
        ("market_new", "NEW ON WALLAPOP · PS5 Pro · 420.00 EUR · Madrid · ps5 · up to €300")
    ]
    assert rows(db)["b"].announced
    with db.session() as session:
        assert session.scalar(select(Alert)).notification_state == "pending"


def test_a_price_change_is_kept_without_an_alert(db, search):
    read(db, search, [found("a", price="250")])
    read(db, search, [found("a", price="220")])
    row = rows(db)["a"]
    assert (row.price, row.previous_price) == (Decimal("220.00"), Decimal("250.00"))
    assert alerts(db) == []


def test_only_the_newest_listings_of_a_search_are_kept(db, search, monkeypatch):
    monkeypatch.setattr(market, "KEEP", 2)
    read(db, search, [found("a")])
    read(db, search, [found("b")])
    read(db, search, [found("c")])
    assert set(rows(db)) == {"b", "c"}
    with db.session() as session:
        kept = [a.market_listing_id for a in session.scalars(select(Alert).order_by(Alert.id))]
    assert len(kept) == 2 and None not in kept


def test_filters_narrow_by_words_price_novelty_and_reservation(db, search):
    read(db, search, [found("a", "PS5 Slim", "250")])
    read(db, search, [found("b", "Comando PS5", "40")])
    listings = rows(db)
    fresh = {listings["b"].id}
    assert Filters(q="slim").keeps(listings["a"], fresh)
    assert not Filters(q="slim").keeps(listings["b"], fresh)
    assert not Filters(max_price=Decimal("100")).keeps(listings["a"], fresh)
    assert Filters(fresh=True).keeps(listings["b"], fresh)
    assert not Filters(fresh=True).keeps(listings["a"], fresh)


async def test_tapping_a_marketplace_notification_opens_the_listing(db, search):
    settings = SettingsService(db)
    settings.save(Preferences(ntfy_topic="topic"))
    read(db, search, [])
    read(db, search, [found("a")])
    sent = []

    class Provider:
        def __init__(self, prefs):
            pass

        async def send(self, title, message, urgent=False, click=None):
            sent.append((title, click))

    await NotificationService(db, settings, Provider).deliver()
    assert sent == [("Pricewatch · market new", "https://es.wallapop.com/item/a")]


@pytest.fixture
def wallapop_site(site):
    client, runtime, prices, requests = site
    answer = {"json": json.loads(SEARCH_JSON)}

    def respond(request):
        requests.append(str(request.url))
        return httpx.Response(200, json=answer["json"])

    runtime.fetcher.transport = httpx.MockTransport(respond)
    yield client, runtime, answer, requests


def test_following_a_search_reads_it_at_once_and_lists_its_listings(wallapop_site):
    client, runtime, _, requests = wallapop_site
    response = client.post(
        "/market/searches",
        data=form_data(client, url=SEARCH_URL, check_interval_minutes="15"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert len(requests) == 1
    page = client.get("/market").text
    assert "Controle DualSense Edge para PS5" in page
    assert "ps5 · up to €300" in page
    assert "Reserved" in page
    again = client.post("/market/searches", data=form_data(client, url=SEARCH_URL))
    assert again.status_code == 422
    assert "already on the Marketplace tab" in again.text


def test_a_link_from_an_unknown_marketplace_is_refused(wallapop_site):
    client, *_ = wallapop_site
    response = client.post(
        "/market/searches", data=form_data(client, url="https://www.olx.pt/ads/q-ps5/")
    )
    assert response.status_code == 422
    assert "Paste a search link from Wallapop" in response.text


def test_removing_a_search_forgets_its_listings_and_keeps_their_alerts(wallapop_site):
    client, runtime, answer, _ = wallapop_site
    client.post("/market/searches", data=form_data(client, url=SEARCH_URL))
    items = answer["json"]["data"]["section"]["payload"]["items"]
    items.append({**items[0], "id": "zzz", "web_slug": "ps5-zzz"})
    client.post("/market/check", data=form_data(client))
    with runtime.db.session() as session:
        search_id = session.scalar(select(MarketSearch.id))
        assert session.scalar(select(Alert.event_type)) == "market_new"
    client.post(f"/market/searches/{search_id}/delete", data=form_data(client))
    with runtime.db.session() as session:
        assert session.scalars(select(MarketListing)).all() == []
        alert = session.scalar(select(Alert))
        assert (alert.event_type, alert.market_listing_id) == ("market_new", None)
    assert "NEW ON WALLAPOP" in client.get("/alerts").text
