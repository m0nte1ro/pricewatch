import json
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.models import Alert, OutletFeed, OutletItem, OutletPrice, OutletRule
from app.retailers.http import Fetcher
from app.retailers.parsing import ScrapeError
from app.retailers.registry import Registry
from app.retailers.worten import nuxt_data
from app.schemas.domain import FeedItem, Preferences, now
from app.services.outlet import EMPTY_READ, Filters, apply, matching_rules, mentions
from tests.conftest import FIXTURES, GENERIC_PAGES
from tests.test_web import form_data

OUTLET_URL = "https://www.worten.pt/outlet/tv-video-e-som/tvs"
API_URL = "https://www.worten.pt/worten-api/search-products"
BADGES_URL = "https://www.worten.pt/worten-api/cms/solve-offer-badges-batch"
CATEGORY_HTML = (FIXTURES / "worten_outlet_category_live.html").read_text()
FEED_JSON = (FIXTURES / "worten_outlet_feed_live.json").read_text()
BADGES_JSON = (FIXTURES / "worten_outlet_badges_live.json").read_text()


def tv(
    item_id: str,
    title: str,
    price: str = "999",
    *,
    in_stock: bool = True,
    promo: str | None = None,
    code: str | None = None,
) -> FeedItem:
    return FeedItem(
        item_id=item_id,
        title=title,
        url=f"https://www.worten.pt/produtos/tv-{item_id}",
        price=Decimal(price),
        promo_price=Decimal(promo) if promo else None,
        promo_code=code,
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
    adapter, data = Registry(Fetcher(None, None)).adapters["worten"], nuxt_data(CATEGORY_HTML)
    assert adapter.feed_context(data, OUTLET_URL) == "01G2Z1Z2GXXCNCP90BZRP243EW"
    assert adapter.feed_grid(data) == "01G39872PRDQDWYGCC5K241C6N"
    assert adapter.feed_page_context(data, "/outlet/tv-video-e-som/tvs") == (
        "b2eddc67604dc9913784c575b97bfd1c6d9678d16729eb1232aeed6984e8e1cc"
    )
    with pytest.raises(ScrapeError, match="isn't a product listing"):
        adapter.feed_context(data, "https://www.worten.pt/outlet/informatica")


def test_worten_listing_response_gives_every_product_with_its_price_and_stock():
    adapter = Registry(Fetcher(None, None)).adapters["worten"]
    found = {item.item_id: item for item in adapter.feed_items(json.loads(FEED_JSON)).values()}
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


def test_worten_card_coupon_flag_lowers_the_price_like_on_a_product_page():
    adapter = Registry(Fetcher(None, None)).adapters["worten"]
    offers = adapter.feed_items(json.loads(FEED_JSON))
    adapter.feed_coupons(offers, json.loads(BADGES_JSON))
    found = {item.item_id: item for item in offers.values()}
    oled = found["7544485"]
    assert (oled.price, oled.promo_price, oled.promo_code) == (
        Decimal("1199.67"),
        Decimal("959.74"),
        "OFF20",
    )
    assert oled.deal_price == Decimal("959.74")
    assert (found["8910700"].promo_price, found["9015156"].promo_price) == (None, None)


def test_worten_coupon_without_a_named_code_still_counts():
    adapter = Registry(Fetcher(None, None)).adapters["worten"]
    offers = adapter.feed_items(json.loads(FEED_JSON))
    badges = json.loads(BADGES_JSON)
    badges["results"][0]["data"]["zones"][0]["badges"][0]["details"]["data"]["config"][
        "modalContent"
    ] = ""
    adapter.feed_coupons(offers, badges)
    oled = next(item for item in offers.values() if item.item_id == "7544485")
    assert (oled.promo_price, oled.promo_code) == (Decimal("959.74"), None)


async def test_worten_feed_asks_the_search_api_for_the_pages_category(db):
    bodies = []

    def respond(request):
        if request.url.path == "/worten-api/search-products":
            bodies.append(json.loads(request.content))
            return httpx.Response(200, text=FEED_JSON)
        if request.url.path == "/worten-api/cms/solve-offer-badges-batch":
            bodies.append(json.loads(request.content))
            return httpx.Response(200, text=BADGES_JSON)
        return httpx.Response(200, text=CATEGORY_HTML)

    fetcher = Fetcher(db, Preferences)
    fetcher.transport, fetcher.min_delay = httpx.MockTransport(respond), 0
    found = await Registry(fetcher).adapters["worten"].fetch_feed(OUTLET_URL + "?utm_source=x")
    assert len(found) == 3
    assert bodies[0]["contexts"] == ["01G2Z1Z2GXXCNCP90BZRP243EW"]
    assert bodies[0]["params"]["pageNumber"] == 0
    assert len(bodies[1]["badges"]) == 3
    assert bodies[1]["badges"][0]["moduleId"] == "01G39872PRDQDWYGCC5K241C6N"
    assert (bodies[1]["uri"], bodies[1]["contextId"][:8]) == (
        "/outlet/tv-video-e-som/tvs",
        "b2eddc67",
    )
    assert next(item for item in found if item.item_id == "7544485").promo_code == "OFF20"


async def test_a_changed_flags_answer_fails_the_whole_read(db):
    def respond(request):
        if request.url.path == "/worten-api/search-products":
            return httpx.Response(200, text=FEED_JSON)
        if request.url.path == "/worten-api/cms/solve-offer-badges-batch":
            return httpx.Response(200, json={"success": False})
        return httpx.Response(200, text=CATEGORY_HTML)

    fetcher = Fetcher(db, Preferences)
    fetcher.transport, fetcher.min_delay = httpx.MockTransport(respond), 0
    with pytest.raises(ScrapeError, match="flags response changed"):
        await Registry(fetcher).adapters["worten"].fetch_feed(OUTLET_URL)


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
    GENERIC_PAGES[BADGES_URL] = BADGES_JSON
    response = client.post(
        "/outlet/feeds",
        data=form_data(client, url=OUTLET_URL, check_interval_minutes="30"),
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    page = client.get("/outlet").text
    assert "TV LG OLED65CX5" in page
    assert "€959.74" in page and ">OFF20<" in page and "€1,199.67 without code" in page
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
    assert '85C7L<span class="tag-count" aria-label="1 in stock">1</span>' in page
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


def test_a_coupon_appearing_is_a_price_drop_at_the_coupon_price(db, feed):
    read(db, feed, [tv("1", "TV A", "1000")])
    bell(db, "1")
    read(db, feed, [tv("1", "TV A", "1000", promo="800", code="OFF20")])
    assert alerts(db) == [
        ("outlet_price_drop", "PRICE DROP · was 1000.00 EUR · TV A · 800.00 EUR with code OFF20")
    ]
    item = items(db)["1"]
    assert (item.price, item.deal_price, item.previous_price, item.lowest_price) == (
        Decimal("1000.00"),
        Decimal("800.00"),
        Decimal("1000.00"),
        Decimal("800.00"),
    )
    with db.session() as session:
        history = session.scalars(select(OutletPrice.promo_price).order_by(OutletPrice.id)).all()
    assert history == [None, Decimal("800.00")]


def test_a_coupon_ending_is_not_a_drop_and_lowest_keeps_the_coupon_price(db, feed):
    read(db, feed, [tv("1", "TV A", "1000", promo="800")])
    bell(db, "1")
    read(db, feed, [tv("1", "TV A", "1000")])
    assert alerts(db) == []
    assert items(db)["1"].lowest_price == Decimal("800.00")


@pytest.mark.parametrize(
    ("title", "size"),
    [
        ("TV LG OLED65CX5 (Outlet Grade A - OLED - 65'' - 165 cm - 4K Ultra HD - Smart TV)", "65"),
        ("NANO LG 4K 65' 65NANO926PB A7 FA 120H DV (Outlet Grade A)", "65"),
        ('TV TCL 85C7L 85" QD-Mini LED', "85"),
        ("TV Philips 42,5″ Ambilight", "42.5"),
        ("Soundbar LG S90TY", None),
    ],
)
def test_screen_size_is_read_from_the_title(title, size):
    item = OutletItem(title=title)
    assert item.size == (Decimal(size) if size else None)


def order(runtime, sort: str) -> list[str]:
    return [item.item_id for item in runtime.outlet.overview(sort)["items"]]


@pytest.fixture
def stocked(site):
    _, runtime, _, _ = site
    with runtime.db.session() as session:
        feed = OutletFeed(retailer="worten", url=OUTLET_URL)
        session.add(feed)
        session.flush()
        listed = [
            tv("a", "TV 55'' cheap", "500"),
            tv("b", "TV 85'' coupon", "1200", promo="700"),
            tv("c", "TV 65'' dear", "900"),
            tv("d", "TV without size", "600"),
        ]
        apply(session, feed, [*listed, tv("e", "TV 98'' gone", "100")])
        apply(session, feed, listed)
    return runtime


def test_outlet_sorts_by_deal_price_keeping_out_of_stock_last(stocked):
    assert order(stocked, "price") == ["a", "d", "b", "c", "e"]
    assert order(stocked, "-price") == ["c", "b", "d", "a", "e"]


def test_outlet_sorts_by_size_with_unknown_sizes_last(stocked):
    assert order(stocked, "size") == ["a", "c", "b", "d", "e"]
    assert order(stocked, "-size") == ["b", "c", "a", "d", "e"]


def test_outlet_page_links_each_sortable_column_both_ways(site, stocked):
    client = site[0]
    page = client.get("/outlet?sort=price").text
    assert 'href="/outlet?sort=-price"' in page and 'Price<span aria-hidden="true"> ↑' in page
    assert 'href="/outlet?sort=size"' in page
    assert "Newest first" in page
    assert "Newest first" not in client.get("/outlet?sort=bogus").text


@pytest.mark.parametrize(
    ("title", "text", "found"),
    [
        ("TV HISENSE 85ABG (Outlet Grade A - LED - 85'' - 216 cm)", "85", True),
        ("TV SAMSUNG TQ65S85DAEXXC (Outlet Grade A - OLED - 65'' - 165 cm)", "85", False),
        ("TV LG OLED55CX5 (Outlet Grade B - OLED - 55'' - 140 cm)", "65", False),
        ("TV TCL 85 C7L", "85c7l", True),
        ("TV LG 55QN8500", "LG 55", True),
        ("TV LG OLED65CX5 (OLED - 65'')", "OLED65", True),
        ("TV LG OLED65CX5 (OLED - 65'')", "LED", False),
        ("TV SAMSUNG TQ85QN900FTXXC", "85", False),
    ],
)
def test_name_alerts_match_from_the_start_of_a_word(title, text, found):
    assert mentions(title, text) is found


def test_the_same_alert_saved_twice_matches_once():
    rules = [OutletRule(text="85C7L"), OutletRule(text="85 c7l"), OutletRule(text="TCL")]
    assert matching_rules(rules, "TV TCL 85C7L") == ["85 c7l", "TCL"]


def test_an_alert_already_saved_is_not_added_again(site):
    client, runtime, _, _ = site
    for text in ("85C7L", "85 c7l", "85-C7L"):
        client.post("/outlet/rules", data=form_data(client, text=text))
    with runtime.db.session() as session:
        assert session.scalars(select(OutletRule.text)).all() == ["85C7L"]


def test_an_alert_counts_each_product_once_even_on_two_pages(site):
    client, runtime, _, _ = site
    with runtime.db.session() as session:
        for url in (OUTLET_URL, OUTLET_URL + "/lg"):
            feed = OutletFeed(retailer="worten", url=url)
            session.add(feed)
            session.flush()
            apply(session, feed, [tv("1", "TV LG OLED65CX5 (OLED - 65'')")])
        session.add(OutletRule(text="OLED65"))
    overview = runtime.outlet.overview()
    assert list(overview["matches"].values()) == [1]
    assert overview["stats"]["alerted"] == 1


@pytest.fixture
def shelf(site):
    _, runtime, _, _ = site
    with runtime.db.session() as session:
        feed = OutletFeed(retailer="worten", url=OUTLET_URL)
        session.add(feed)
        session.flush()
        listed = [
            tv("a", "TV LG OLED65CX5 (Outlet Grade A - OLED - 65'')", "1200", promo="960"),
            tv("b", "TV TCL 85C7L (Outlet Grade B - 85'')", "1500"),
            tv("c", "TV Samsung QE55Q80A (Outlet Grade A - QLED - 55'')", "700"),
        ]
        apply(session, feed, [*listed, tv("d", "TV Sony 77'' (Outlet Grade C)", "900")])
        apply(session, feed, listed)
        session.scalar(select(OutletItem).where(OutletItem.item_id == "b")).belled = True
        session.add(OutletRule(text="85C7L"))
    return runtime


@pytest.mark.parametrize(
    ("filters", "shown"),
    [
        (Filters(q="oled 65"), ["a"]),
        (Filters(q="samsung"), ["c"]),
        (Filters(stock="out"), ["d"]),
        (Filters(stock="in"), ["a", "b", "c"]),
        (Filters(grade="A"), ["a", "c"]),
        (Filters(min_size=Decimal(60), max_size=Decimal(80)), ["a", "d"]),
        (Filters(max_price=Decimal(960)), ["a", "c", "d"]),
        (Filters(belled=True), ["b"]),
        (Filters(alerted=True), ["b"]),
        (Filters(q="oled", stock="out"), []),
    ],
)
def test_outlet_table_filters(shelf, filters, shown):
    items = shelf.outlet.overview("", filters)["items"]
    assert sorted(item.item_id for item in items) == shown


def test_outlet_page_filters_from_the_query_and_keeps_them_when_sorting(site, shelf):
    client = site[0]
    page = client.get("/outlet?q=tcl&grade=b&max_price=abc&sort=price").text
    assert "TV TCL 85C7L" in page and "TV Samsung" not in page
    assert '<span class="count">1 of 4</span>' in page
    assert 'href="/outlet?q=tcl&amp;grade=b&amp;max_price=abc&amp;sort=-price"' in page
    assert 'href="/outlet?sort=price">Clear filters' in page


def test_bell_rings_in_place_for_htmx(site, shelf):
    client, runtime, _, _ = site
    item_id = items(runtime.db)["a"].id
    response = client.post(
        f"/outlet/items/{item_id}/bell", data=form_data(client), headers={"HX-Request": "true"}
    )
    assert response.status_code == 200
    assert response.text.startswith("<form") and 'aria-pressed="true"' in response.text


def test_only_products_after_the_first_read_are_new(site):
    _, runtime, _, _ = site
    with runtime.db.session() as session:
        feed = OutletFeed(retailer="worten", url=OUTLET_URL, created_at=now() - timedelta(days=1))
        session.add(feed)
        session.flush()
        apply(session, feed, [tv("old", "TV A")])
        feed.items[0].first_seen_at = now() - timedelta(hours=1)
        apply(session, feed, [tv("old", "TV A"), tv("new", "TV B")])
    overview = runtime.outlet.overview()
    assert [item.item_id for item in overview["items"] if item.id in overview["fresh"]] == ["new"]
    assert overview["stats"]["new"] == 1
