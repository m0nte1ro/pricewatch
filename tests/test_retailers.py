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
        ("1.299 €", "1299.00"),
        ("2.499", "2499.00"),
        ("1,299", "1299.00"),
        ("1.299.999", "1299999.00"),
        ("12,5", "12.50"),
        ("999.99", "999.99"),
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
        ("TCL 55P8L (Reacondicionado)", "refurbished"),
        ("Amazon Renewed", "refurbished"),
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
    # Search-result links must not keep the query, or every result looks relevant.
    assert (
        registry.adapters["amazon_es"].normalize_url(
            "https://www.amazon.es/TCL-65V6C/dp/B0D1234567/ref=sr_1_3?crid=X&keywords=TCL+85C7K&qid=1&sr=8-3"
        )
        == "https://www.amazon.es/dp/B0D1234567"
    )


def test_amazon_search_ignores_results_for_other_models(registry):
    html = (
        '<a href="/TCL-65V6C/dp/B0D1234567/ref=sr_1_1?keywords=TCL+85C7K">TCL 65V6C 65 pulgadas</a>'
        '<a href="/TCL-85C7K/dp/B0D7654321/ref=sr_1_2?keywords=TCL+85C7K">TCL 85C7K Mini LED</a>'
    )
    links = registry.adapters["amazon_es"].search_links(html, identify("TCL 85C7K"))
    assert links == ["https://www.amazon.es/dp/B0D7654321"]


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


def test_darty_first_party_offer_without_markers_is_new(registry):
    listing = registry.adapters["darty"].parse(
        (FIXTURES / "darty_live.html").read_text(),
        "https://www.darty.pt/products/smart-tv-tcl-55p8l-qd-mini-led-55-uhd-4k-google-tv-140cm-5901292530204",
    )[0]
    assert listing.price == Decimal("499.99")
    assert listing.identity.model == "55P8L"
    assert listing.condition == "new"
    assert listing.retailer_product_id == "T00250456"


def test_radio_live_microdata(registry):
    listing = registry.adapters["radiopopular"].parse(
        (FIXTURES / "radio_live.html").read_text(),
        "https://www.radiopopular.pt/produto/tv-tcl-85c7l",
    )[0]
    assert listing.price == Decimal("2699.99")
    assert listing.availability == "out_of_stock"
    assert listing.identity.model == "85C7L"
    assert listing.condition == "new"
    assert listing.retailer_product_id == "F126186"


def test_first_party_default_never_hides_outlet_markers(registry):
    html = (
        '<h1>TV TCL 55P8L Outlet Grade B</h1><span itemprop="price" content="399.99"></span>'
        '<link itemprop="availability" href="https://schema.org/InStock">'
    )
    listing = registry.adapters["radiopopular"].parse(
        html, "https://www.radiopopular.pt/produto/tv-tcl-55p8l"
    )[0]
    assert listing.condition == "outlet_grade_b"


def test_marketplace_without_condition_stays_unknown(registry):
    html = (
        '<span id="productTitle">TCL 85C7K</span>'
        '<div id="corePrice_feature_div"><span class="a-price"><span class="a-offscreen">999,99 €</span></span></div>'
        '<div id="availability">En stock</div>'
    )
    listing = registry.adapters["amazon_es"].parse(html, "https://www.amazon.es/dp/B0D7654321")[0]
    assert listing.condition == "unknown"


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


AMAZON_BUY_BOX = (
    '<span id="productTitle">TCL 85C7L 85 polegadas SQD-Mini LED</span>'
    '<div id="corePriceDisplay_desktop_feature_div"><span class="a-price"><span class="a-offscreen"></span></span></div>'
    '<div id="corePrice_feature_div"><span class="a-price"><span class="a-offscreen">1\xa0777,90€</span></span></div>'
    '<div id="availability">Estimativa de envio de 7 a 8 dias</div>'
    '<div id="merchantInfoFeature_feature_div"><span class="offer-display-feature-text-message">{seller}</span></div>'
    '<input id="add-to-cart-button">'
)


def test_amazon_buy_box_sold_by_amazon(registry):
    html = AMAZON_BUY_BOX.format(seller="Amazon")
    listing = registry.adapters["amazon_es"].parse(html, "https://www.amazon.es/dp/B0GVT5HYB3")[0]
    # The empty placeholder price is skipped; a cart button means buyable.
    assert listing.price == Decimal("1777.90")
    assert listing.availability == "in_stock"
    assert (listing.seller, listing.condition) == ("Amazon", "new")


def test_amazon_third_party_seller_condition_stays_unknown(registry):
    html = AMAZON_BUY_BOX.format(seller="MK TRADE SIA")
    listing = registry.adapters["amazon_es"].parse(html, "https://www.amazon.es/dp/B0GVT5HYB3")[0]
    assert (listing.seller, listing.condition) == ("MK TRADE SIA", "unknown")


def test_worten_buy_box_overrides_stale_in_stock_json_ld(registry):
    # Live page: JSON-LD says InStock, but the buy box offers only "Acompanhar disponibilidade".
    listing = registry.adapters["worten"].parse(
        (FIXTURES / "worten_outlet_unavailable_live.html").read_text(),
        "https://www.worten.pt/produtos/tv-tcl-85c7k-outlet-grade-a-miniled-85-216-cm-4k-ultra-hd-smart-tv-8751314",
    )[0]
    assert listing.price == Decimal("783.57")
    assert listing.condition == "outlet_grade_a"
    assert listing.availability == "out_of_stock"


def test_worten_buy_box_with_cart_button_is_in_stock(registry):
    html = (
        (FIXTURES / "worten_outlet_unavailable_live.html")
        .read_text()
        .replace(" add-to-cart--unavailability", "")
        .replace("<!--[--><!-- --><!--]-->", "<button>Adicionar ao carrinho</button>")
    )
    listing = registry.adapters["worten"].parse(html, URLS["worten"])[0]
    assert listing.availability == "in_stock"
