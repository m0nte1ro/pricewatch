import json
from datetime import datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qsl

import httpx
import pytest
from sqlalchemy import select

import app.services.market as market
from app.marketplaces.registry import Marketplaces
from app.marketplaces.wallapop import CHANGED
from app.models import Alert, MarketListing, MarketSearch
from app.notifications.ntfy import NtfyProvider
from app.retailers.http import Fetcher
from app.retailers.parsing import ScrapeError
from app.schemas.domain import MarketItem, Preferences, now
from app.services.market import Filters, MarketService, apply
from app.services.notifications import DeliveryError, NotificationService
from app.services.settings import SettingsService
from tests.conftest import FIXTURES
from tests.test_web import form_data

SEARCH_URL = "https://es.wallapop.com/search?keywords=ps5&max_sale_price=300"
SEARCH_JSON = (FIXTURES / "wallapop_search_live.json").read_text()
NOTHING_JSON = {"data": {"section": {"payload": {"items": []}}}}


def order(url) -> str:
    return dict(parse_qsl(httpx.URL(str(url)).query.decode()))["order_by"]


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


def test_a_wallapop_search_link_keeps_its_filters_and_drops_order_tracking_and_shipping():
    provider = wallapop()
    url = "https://pt.wallapop.com/app/search?keywords=ps5&order_by=price_low_to_high&min_sale_price=100&latitude=38.72&longitude=-9.14&search_id=abc&utm_source=x&is_shippable=false"
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


async def test_wallapop_is_read_newest_first_shipping_only_from_porto_unless_the_link_says_where(
    db,
):
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
        "latitude": "41.14961",
        "longitude": "-8.61099",
        "order_by": "newest",
        "source": "search_box",
        "is_shippable": "true",
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


async def test_a_listing_that_does_not_ship_is_left_out(db):
    answer = json.loads(SEARCH_JSON)
    items = answer["data"]["section"]["payload"]["items"]
    items[0]["shipping"]["user_allows_shipping"] = False
    del items[1]["shipping"]["item_is_shippable"]
    provider = wallapop(db, lambda request: httpx.Response(200, json=answer))
    assert [item.listing_id for item in await provider.fetch_listings(SEARCH_URL)] == [
        "wzvgwlo59qzl"
    ]


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
    # Newest first, then Wallapop's own order to show what is there.
    assert [order(url) for url in requests] == ["newest", "most_relevance"]
    page = client.get("/market").text
    assert "Controle DualSense Edge para PS5" in page
    assert "ps5 · up to €300" in page
    assert "Reserved" in page
    again = client.post("/market/searches", data=form_data(client, url=SEARCH_URL))
    assert again.status_code == 422
    assert "already on the Marketplace tab" in again.text


def test_a_search_is_checked_every_hour_unless_told_otherwise(wallapop_site):
    client, runtime, _, requests = wallapop_site
    client.post("/market/searches", data=form_data(client, url=SEARCH_URL))
    with runtime.db.session() as session:
        search = session.scalar(select(MarketSearch))
    assert search.check_interval_minutes == 60
    assert (
        timedelta(minutes=57) <= search.next_check_at - search.last_read_at <= timedelta(minutes=63)
    )
    assert 'value="60"' in client.get("/market").text


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


async def test_a_scheduled_run_reads_only_the_searches_that_are_due(db):
    requests = []

    def respond(request):
        query = dict(parse_qsl(request.url.query.decode()))
        requests.append((query["keywords"], query["order_by"]))
        return httpx.Response(200, text=SEARCH_JSON)

    provider = wallapop(db, respond)
    with db.session() as session:
        session.add_all(
            [
                MarketSearch(provider="wallapop", url=SEARCH_URL, label="ps5", next_check_at=now()),
                MarketSearch(
                    provider="wallapop",
                    url="https://es.wallapop.com/search?keywords=zidoo",
                    label="zidoo",
                    next_check_at=now() + timedelta(minutes=30),
                ),
            ]
        )
    service = MarketService(
        db, {"wallapop": provider}, NotificationService(db, SettingsService(db))
    )
    await service.run()
    assert requests == [("ps5", "newest"), ("ps5", "most_relevance")]
    await service.run()
    assert requests == [("ps5", "newest"), ("ps5", "most_relevance")]


@pytest.fixture
def quiet_search(db):
    """A search already read once that found nothing in Wallapop's last day, like zidoo."""
    with db.session() as session:
        row = MarketSearch(
            provider="wallapop",
            url="https://es.wallapop.com/search?keywords=zidoo",
            label="zidoo",
            last_read_at=now() - timedelta(hours=1),
        )
        session.add(row)
    return row.id


def answering(newest: dict, relevance: dict, requests: list):
    def respond(request):
        requests.append(order(request.url))
        return httpx.Response(200, json=newest if order(request.url) == "newest" else relevance)

    return respond


async def test_a_search_with_nothing_yet_shows_what_wallapop_lists_now_without_alerting(
    db, quiet_search
):
    requests = []
    provider = wallapop(db, answering(NOTHING_JSON, json.loads(SEARCH_JSON), requests))
    service = MarketService(
        db, {"wallapop": provider}, NotificationService(db, SettingsService(db))
    )
    await service.run(force=True)
    assert requests == ["newest", "most_relevance"]
    assert set(rows(db)) == {"pj943gwle26e", "e65qx3nl7m6o", "wzvgwlo59qzl"}
    assert not any(row.announced for row in rows(db).values())
    assert alerts(db) == []
    # Once it lists something, newest first is enough.
    await service.run(force=True)
    assert requests == ["newest", "most_relevance", "newest"]


async def test_a_new_listing_is_announced_beside_what_wallapop_lists_now(db, quiet_search):
    newest = json.loads(SEARCH_JSON)
    newest["data"]["section"]["payload"]["items"] = newest["data"]["section"]["payload"]["items"][
        :1
    ]
    relevance = json.loads(SEARCH_JSON)
    provider = wallapop(db, answering(newest, relevance, []))
    service = MarketService(
        db, {"wallapop": provider}, NotificationService(db, SettingsService(db))
    )
    await service.run(force=True)
    assert [event for event, _ in alerts(db)] == ["market_new"]
    assert "Hellraiser Revival" in alerts(db)[0][1]
    assert {key for key, row in rows(db).items() if row.announced} == {"pj943gwle26e"}
    assert len(rows(db)) == 3


def test_a_search_with_nothing_says_so_instead_of_blaming_the_filters(wallapop_site):
    client, runtime, answer, _ = wallapop_site
    answer["json"] = NOTHING_JSON
    client.post("/market/searches", data=form_data(client, url=SEARCH_URL))
    page = client.get("/market?search=1").text
    assert "Nothing listed yet." in page
    assert "Nothing matches." not in page


def ntfy(status: int, sent: list):
    """The real ntfy provider, answering every publish with this status."""

    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(status)

    return lambda prefs: NtfyProvider(prefs, transport=httpx.MockTransport(respond))


def pinger(db, factory, topic: str = "pricewatch") -> MarketService:
    settings = SettingsService(db)
    settings.save(Preferences(ntfy_topic=topic))
    return MarketService(
        db, Marketplaces(Fetcher(None, None)), NotificationService(db, settings, factory)
    )


async def test_a_test_ping_sends_the_newest_listing_the_way_a_new_one_arrives(db, search):
    read(db, search, [found("a", "PS5 Slim", "250")])
    read(db, search, [found("b", "PS5 Pro", "420"), found("a", "PS5 Slim", "250")])
    before, sent = alerts(db), []
    await pinger(db, ntfy(200, sent)).test_ping(search)
    assert sent == [
        {
            "topic": "pricewatch",
            "title": "Pricewatch · market new",
            "message": "TEST · NEW ON WALLAPOP · PS5 Pro · 420.00 EUR · Madrid · ps5 · up to €300",
            "priority": 3,
            "click": "https://es.wallapop.com/item/b",
        }
    ]
    assert alerts(db) == before


async def test_a_test_ping_says_why_it_was_not_sent(db, search):
    read(db, search, [found("a")])
    with pytest.raises(DeliveryError, match="Set an ntfy topic in Settings first"):
        await pinger(db, ntfy(200, []), topic="").test_ping(search)
    with pytest.raises(DeliveryError, match=r"ntfy refused it \(HTTP 403\)"):
        await pinger(db, ntfy(403, [])).test_ping(search)

    def unreachable(request):
        raise httpx.ConnectError("refused")

    factory = lambda prefs: NtfyProvider(prefs, transport=httpx.MockTransport(unreachable))  # noqa: E731
    with pytest.raises(DeliveryError, match="Couldn't reach the ntfy server"):
        await pinger(db, factory).test_ping(search)


async def test_a_search_without_listings_has_nothing_to_test_with(db, search):
    with pytest.raises(ScrapeError, match="no listing to send yet"):
        await pinger(db, ntfy(200, [])).test_ping(search)


def test_the_test_ping_button_confirms_what_was_sent(wallapop_site):
    client, runtime, _, _ = wallapop_site
    client.post("/market/searches", data=form_data(client, url=SEARCH_URL))
    sent = []
    runtime.notifications.provider_factory = ntfy(200, sent)
    refused = client.post("/market/searches/1/ping", data=form_data(client))
    assert refused.status_code == 422
    assert "Set an ntfy topic in Settings first" in refused.text
    runtime.settings.save(Preferences(ntfy_topic="pricewatch"))
    response = client.post(
        "/market/searches/1/ping", data=form_data(client), follow_redirects=False
    )
    assert (response.status_code, response.headers["location"]) == (303, "/market?pinged=1")
    assert [message["message"][:22] for message in sent] == ["TEST · NEW ON WALLAPOP"]
    page = client.get("/market?pinged=1").text
    assert "Test notification sent for “ps5 · up to €300”" in page
    assert "pinged=1" not in page.split("Test notification sent")[1]


async def test_a_new_listing_reaches_ntfy_on_the_next_scheduled_read(db, search):
    answer, sent = json.loads(SEARCH_JSON), []
    items = answer["data"]["section"]["payload"]["items"]
    provider = wallapop(db, lambda request: httpx.Response(200, json=answer))
    settings = SettingsService(db)
    settings.save(Preferences(ntfy_topic="pricewatch"))
    service = MarketService(
        db, {"wallapop": provider}, NotificationService(db, settings, ntfy(200, sent))
    )
    await service.run()
    assert (len(rows(db)), sent) == (3, [])
    new = {**items[0], "id": "zidoo1", "title": "Zidoo Z9X Pro", "web_slug": "zidoo-z9x-pro-1"}
    items.insert(0, new)
    with db.session() as session:
        session.scalar(select(MarketSearch)).next_check_at = now()
    await service.run()
    assert sent == [
        {
            "topic": "pricewatch",
            "title": "Pricewatch · market new",
            "message": "NEW ON WALLAPOP · Zidoo Z9X Pro · 60.00 EUR · Barcelona · ps5 · up to €300",
            "priority": 3,
            "click": "https://es.wallapop.com/item/zidoo-z9x-pro-1",
        }
    ]
    with db.session() as session:
        assert session.scalars(select(Alert.notification_state)).all() == ["sent"]
