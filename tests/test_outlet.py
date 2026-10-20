import json
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.models import Alert, OutletFeed, OutletItem, OutletPrice, OutletRule
from app.retailers.http import Fetcher
from app.retailers.parsing import ScrapeError
from app.retailers.registry import Registry
from app.schemas.domain import FeedItem, Preferences
from app.services.outlet import EMPTY_READ, apply
from tests.conftest import FIXTURES, GENERIC_PAGES
from tests.test_web import form_data

OUTLET_URL = "https://www.worten.pt/outlet/tv-video-e-som/tvs"
API_URL = "https://www.worten.pt/worten-api/search-products"
CATEGORY_HTML = (FIXTURES / "worten_outlet_category_live.html").read_text()
FEED_JSON = (FIXTURES / "worten_outlet_feed_live.json").read_text()


def tv(item_id: str, title: str, price: str = "999", *, in_stock: bool = True) -> FeedItem:
    return FeedItem(
        item_id=item_id,
        title=title,
        url=f"https://www.worten.pt/produtos/tv-{item_id}",
        price=Decimal(price),
        in_stock=in_stock,
    )


@pytest.fixture
def feed(db):
    with db.session() as session:
        row = OutletFeed(retailer="worten", url=OUTLET_URL)
        session.add(row)
    return row.id


def read(db, feed_id: int, found: list[FeedItem]):
    with db.session() as session:
        apply(session, session.get(OutletFeed, feed_id), found)


def items(db) -> dict[str, OutletItem]:
    with db.session() as session:
        return {row.item_id: row for row in session.scalars(select(OutletItem))}


def alerts(db) -> list[tuple[str, str]]:
    with db.session() as session:
        return [
            (a.event_type, a.message) for a in session.scalars(select(Alert).order_by(Alert.id))
        ]


def bell(db, item_id: str):
    with db.session() as session:
        session.scalar(select(OutletItem).where(OutletItem.item_id == item_id)).belled = True


def rule(db, text: str):
    with db.session() as session:
        session.add(OutletRule(text=text))


def test_worten_category_page_names_the_category_its_script_searches():
    adapter = Registry(Fetcher(None, None)).adapters["worten"]
    assert adapter.feed_context(CATEGORY_HTML, OUTLET_URL) == "01G2Z1Z2GXXCNCP90BZRP243EW"
    with pytest.raises(ScrapeError, match="isn't a product listing"):
        adapter.feed_context(CATEGORY_HTML, "https://www.worten.pt/outlet/informatica")


def test_worten_listing_response_gives_every_product_with_its_price_and_stock():
    adapter = Registry(Fetcher(None, None)).adapters["worten"]
    found = {item.item_id: item for item in adapter.feed_items(json.loads(FEED_JSON))}
    assert set(found) == {"7544485", "8910700", "9015156"}
    oled = found["7544485"]
    assert oled.title.startswith("TV LG OLED65CX5 (Outlet Grade A - OLED - 65''")
    assert oled.url == (
        "https://www.worten.pt/produtos/tv-lg-oled65cx5-outlet-grade-a-oled-65-165-cm-4k-ultra-hd-smart-tv-7544485"
    )
    assert (oled.price, oled.original_price, oled.in_stock) == (
        Decimal("1199.67"),
        Decimal("2999.00"),
        True,
    )
    assert found["9015156"].price == Decimal("160.97")


async def test_worten_feed_asks_the_search_api_for_the_pages_category(db):
    bodies = []

    def respond(request):
        if request.url.path == "/worten-api/search-products":
            bodies.append(json.loads(request.content))
            return httpx.Response(200, text=FEED_JSON)
        return httpx.Response(200, text=CATEGORY_HTML)

    fetcher = Fetcher(db, Preferences)
    fetcher.transport, fetcher.min_delay = httpx.MockTransport(respond), 0
    found = await Registry(fetcher).adapters["worten"].fetch_feed(OUTLET_URL + "?utm_source=x")
    assert len(found) == 3
    assert bodies[0]["contexts"] == ["01G2Z1Z2GXXCNCP90BZRP243EW"]
    assert bodies[0]["params"]["pageNumber"] == 0


def test_only_stores_with_a_feed_reader_take_outlet_pages():
    registry = Registry(Fetcher(None, None))
    assert registry.for_feed(OUTLET_URL).name == "worten"
    with pytest.raises(ScrapeError, match="aren't supported yet"):
        registry.for_feed("https://www.fnac.pt/outlet")
    with pytest.raises(ScrapeError, match="category or outlet page"):
        registry.for_feed("https://www.worten.pt/produtos/tv-tcl-85c7k-8376601")


def test_first_read_records_everything_without_alerting(db, feed):
    rule(db, "OLED")
    read(db, feed, [tv("1", "TV LG OLED65CX5"), tv("2", "TV TCL 85C7L")])
    assert set(items(db)) == {"1", "2"}
    assert alerts(db) == []


def test_a_new_product_matching_a_name_alert_raises_one_alert(db, feed):
    read(db, feed, [tv("1", "TV TCL 85C7L")])
    rule(db, "77 oled")
    rule(db, "LG OLED")
    read(db, feed, [tv("1", "TV TCL 85C7L"), tv("2", 'TV LG OLED77C4 (OLED - 77")', "1499")])
    assert alerts(db) == [
        (
            "outlet_new",
            'NEW IN OUTLET · matches LG OLED · TV LG OLED77C4 (OLED - 77") · 1499.00 EUR',
        )
    ]


def test_name_alerts_ignore_case_spaces_and_punctuation(db, feed):
    read(db, feed, [])
    rule(db, "85c7l")
    read(db, feed, [tv("1", "TV TCL 85 C7L"), tv("2", "TV LG 55QN8500")])
    assert [event for event, _ in alerts(db)] == ["outlet_new"]


def test_products_gone_from_the_page_turn_out_of_stock_and_stay(db, feed):
    read(db, feed, [tv("1", "TV A"), tv("2", "TV B")])
    read(db, feed, [tv("1", "TV A")])
    gone = items(db)["2"]
    assert (gone.in_stock, gone.out_of_stock_since is not None) == (False, True)
    assert alerts(db) == []


def test_an_empty_read_changes_nothing(db, feed):
    read(db, feed, [tv("1", "TV A")])
    bell(db, "1")
    with pytest.raises(ScrapeError, match=EMPTY_READ):
        read(db, feed, [])
    assert items(db)["1"].in_stock is True
    assert alerts(db) == []


def test_a_belled_product_alerts_on_drops_out_of_stock_and_return(db, feed):
    read(db, feed, [tv("1", "TV A", "900"), tv("2", "TV B")])
    bell(db, "1")
    read(db, feed, [tv("1", "TV A", "950"), tv("2", "TV B")])
    read(db, feed, [tv("1", "TV A", "850"), tv("2", "TV B")])
    read(db, feed, [tv("2", "TV B")])
    read(db, feed, [tv("1", "TV A", "850"), tv("2", "TV B")])
    assert alerts(db) == [
        ("outlet_price_drop", "PRICE DROP · was 950.00 EUR · TV A · 850.00 EUR"),
        ("outlet_out_of_stock", "OUT OF STOCK · TV A · 850.00 EUR"),
        ("outlet_back", "BACK IN OUTLET · TV A · 850.00 EUR"),
    ]
    item = items(db)["1"]
    assert (item.lowest_price, item.out_of_stock_since) == (Decimal("850.00"), None)


def test_a_product_listed_as_sold_out_counts_as_out_of_stock(db, feed):
    read(db, feed, [tv("1", "TV A")])
    bell(db, "1")
    read(db, feed, [tv("1", "TV A", in_stock=False)])
    assert [event for event, _ in alerts(db)] == ["outlet_out_of_stock"]


def test_an_unbelled_product_coming_back_alerts_only_when_a_name_alert_matches(db, feed):
    read(db, feed, [tv("1", "TV LG OLED77C4"), tv("2", "TV TCL 85C7L")])
    read(db, feed, [tv("3", "TV Sony")])
    rule(db, "OLED77")
    read(db, feed, [tv("1", "TV LG OLED77C4"), tv("2", "TV TCL 85C7L"), tv("3", "TV Sony")])
    assert alerts(db) == [
        ("outlet_back", "BACK IN OUTLET · matches OLED77 · TV LG OLED77C4 · 999.00 EUR")
    ]


def test_only_changes_are_kept_in_the_price_history(db, feed):
    read(db, feed, [tv("1", "TV A", "900")])
    read(db, feed, [tv("1", "TV A", "900")])
    read(db, feed, [tv("1", "TV A", "800")])
    with db.session() as session:
        prices = session.scalars(select(OutletPrice.price).order_by(OutletPrice.id)).all()
    assert prices == [Decimal("900.00"), Decimal("800.00")]


def test_outlet_tab_follows_a_page_and_lists_its_products(site):
    client, runtime, _, _ = site
    GENERIC_PAGES[OUTLET_URL] = CATEGORY_HTML
    GENERIC_PAGES[API_URL] = FEED_JSON
    response = client.post(
        "/outlet/feeds",
        data=form_data(client, url=OUTLET_URL, check_interval_minutes="30"),
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    page = client.get("/outlet").text
    assert "TV LG OLED65CX5" in page
    assert "3 in stock of 3 seen" in page
    again = client.post("/outlet/feeds", data=form_data(client, url=OUTLET_URL))
    assert again.status_code == 422 and "already on the Outlet tab" in again.text


def test_outlet_tab_rings_the_bell_and_keeps_name_alerts(site):
    client, runtime, _, _ = site
    with runtime.db.session() as session:
        feed = OutletFeed(retailer="worten", url=OUTLET_URL)
        session.add(feed)
        session.flush()
        apply(session, feed, [tv("1", "TV TCL 85C7L")])
        item_id = feed.items[0].id
    response = client.post(
        f"/outlet/items/{item_id}/bell", data=form_data(client), follow_redirects=False
    )
    assert response.headers["location"] == f"/outlet#item-{item_id}"
    assert items(runtime.db)["1"].belled is True
    client.post("/outlet/rules", data=form_data(client, text="  85C7L "))
    page = client.get("/outlet").text
    assert "“85C7L”" in page and "1 in stock now" in page
    assert client.post("/outlet/rules", data=form_data(client, text="''")).status_code == 422


def test_removing_a_page_keeps_its_alerts_in_activity(site):
    client, runtime, _, _ = site
    with runtime.db.session() as session:
        feed = OutletFeed(retailer="worten", url=OUTLET_URL)
        session.add(feed)
        session.flush()
        apply(session, feed, [tv("1", "TV A")])
        feed.items[0].belled = True
        apply(session, feed, [tv("1", "TV A", "800")])
        feed_id = feed.id
    client.post(f"/outlet/feeds/{feed_id}/delete", data=form_data(client))
    assert items(runtime.db) == {}
    assert "PRICE DROP" in client.get("/alerts").text
