import json
from decimal import Decimal

import pytest

from app.retailers.http import Fetcher
from app.retailers.parsing import ScrapeError, condition, money
from app.retailers.registry import Registry
from app.services.matching import identify
from tests.conftest import FIXTURES, URLS


@pytest.fixture
def registry():
    return Registry(Fetcher(None, None))


def test_worten_actual_html(registry):
    listing = registry.adapters["worten"].parse(
        (FIXTURES / "worten_live.html").read_text(), URLS["worten"]
    )[0]
    assert listing.price == Decimal("1199.00")
    assert listing.seller == "Worten"
    assert listing.identity.model == "85C7K"
    assert listing.identity.identifiers["gtin13"] == "5901292525712"
    assert listing.availability == "in_stock"
    assert listing.condition == "new"


def test_worten_actual_search_response(registry):
    payload = json.loads((FIXTURES / "worten_search_live.json").read_text())
    links = registry.adapters["worten"].api_search_links(payload, identify("TCL 85C7L"))
    assert len(links) == 1 and "85c7l" in links[0]
    assert registry.adapters["worten"].api_search_links(payload, identify("TCL 85C7K")) == []


@pytest.mark.parametrize("retailer", ["fnac", "darty", "radiopopular", "amazon_es"])
def test_adapter_contract_fixtures(registry, retailer):
    listing = registry.adapters[retailer].parse(
        (FIXTURES / f"{retailer}.html").read_text(), URLS[retailer]
    )[0]
    assert listing.price == Decimal("999.99")
    assert listing.identity.model == "85C7K"
    assert listing.availability == "in_stock"
    assert listing.condition == "new"


def test_outlet_not_confused_with_new(registry):
    listing = registry.adapters["worten"].parse(
        (FIXTURES / "worten_outlet.html").read_text(), URLS["worten"]
    )[0]
    assert listing.condition == "outlet_grade_c"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1.299,99 €", "1299.99"),
        ("€1,299.99", "1299.99"),
        ("999,00", "999.00"),
        ("0", None),
        ("unknown", None),
    ],
)
def test_money(text, expected):
    assert money(text) == (Decimal(expected) if expected else None)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("Outlet Grade A NewCondition", "outlet_grade_a"),
        ("Grade B", "outlet_grade_b"),
        ("Caixa aberta", "outlet_open_box"),
        ("Recondicionado", "refurbished"),
        ("Usado", "used"),
        ("", "unknown"),
    ],
)
def test_conditions(value, expected):
    assert condition(value) == expected


def test_url_normalization(registry):
    assert (
        registry.adapters["worten"].normalize_url(URLS["worten"] + "/?utm_source=x#reviews")
        == URLS["worten"]
    )
    assert (
        registry.adapters["amazon_es"].normalize_url(
            "https://amazon.es/product-title/dp/B012345678/ref=something?tag=referral&smid=SELLER"
        )
        == "https://www.amazon.es/dp/B012345678?smid=SELLER"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://www.worten.pt/produtos/x-123",
        "https://www.worten.pt.evil.test/produtos/x-123",
        "https://user:pass@www.worten.pt/produtos/x-123",
        "https://127.0.0.1/produtos/x-123",
        "https://www.worten.pt:8443/produtos/x-123",
    ],
)
def test_url_restrictions(registry, url):
    with pytest.raises(ScrapeError):
        registry.adapters["worten"].normalize_url(url)


def test_aggregate_price_is_not_a_concrete_offer(registry):
    html = '<h1>TCL 85C7K</h1><script type="application/ld+json">{"@type":"Product","name":"TCL 85C7K","offers":{"@type":"AggregateOffer","lowPrice":1}}</script>'
    with pytest.raises(ScrapeError):
        registry.adapters["worten"].parse(html, URLS["worten"])


def test_bad_page_does_not_invent_price(registry):
    with pytest.raises(ScrapeError):
        registry.adapters["worten"].parse("<h1>Something went wrong</h1>", URLS["worten"])


def test_darty_live_html_reports_missing_condition(registry):
    listing = registry.adapters["darty"].parse(
        (FIXTURES / "darty_live.html").read_text(),
        "https://www.darty.pt/products/smart-tv-tcl-55p8l-qd-mini-led-55-uhd-4k-google-tv-140cm-5901292530204",
    )[0]
    assert listing.price == Decimal("499.99")
    assert listing.identity.model == "55P8L"
    assert listing.condition == "unknown"
    assert listing.retailer_product_id == "T00250456"


def test_radio_live_microdata(registry):
    listing = registry.adapters["radiopopular"].parse(
        (FIXTURES / "radio_live.html").read_text(),
        "https://www.radiopopular.pt/produto/tv-tcl-85c7l",
    )[0]
    assert listing.price == Decimal("2699.99")
    assert listing.availability == "out_of_stock"
    assert listing.identity.model == "85C7L"
    assert listing.retailer_product_id == "F126186"


def test_recommendations_do_not_replace_main_product(registry):
    html = '<h1>TCL 85C7K</h1><script type="application/ld+json">{"@type":"Product","name":"TCL 75C8K","offers":{"price":100,"availability":"InStock"}}</script>'
    with pytest.raises(ScrapeError):
        registry.adapters["worten"].parse(html, URLS["worten"])


def test_darty_live_search_filters_other_sizes(registry):
    payload = json.loads((FIXTURES / "darty_search_live.json").read_text())
    links = registry.adapters["darty"].api_search_links(payload, identify("TCL 55P8L"))
    assert len(links) == 1
    assert "55p8l" in links[0]
    assert "_psid" not in links[0]
