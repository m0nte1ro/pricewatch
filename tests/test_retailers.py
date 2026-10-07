import json
import re
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
    assert listing.identity.identifiers["gtin"] == "5901292525712"
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


def test_darty_out_of_stock_live_page(registry):
    listing = registry.adapters["darty"].parse(
        (FIXTURES / "darty_85c7k_out_of_stock_live.html").read_text(),
        "https://www.darty.pt/products/tcl-tv-mini-led-85c7k-4k-216cm",
    )[0]
    assert listing.price == Decimal("2099.99")
    assert listing.availability == "out_of_stock"
    assert listing.identity.model == "85C7K"
    # Darty labels the EAN "gtin", Worten "gtin13": both normalize to the same identifier.
    assert listing.identity.identifiers["gtin"] == "5901292525712"


@pytest.mark.parametrize(
    "data,expected",
    [
        ({"gtin13": "5901292525712"}, "5901292525712"),
        ({"gtin": "5901292525712"}, "5901292525712"),
        ({"gtin14": "05901292525712"}, "5901292525712"),
        ({"gtin12": "190198066474"}, "0190198066474"),
        ({"gtin": "n/a"}, None),
    ],
)
def test_gtin_normalization(registry, data, expected):
    identity = registry.adapters["darty"].extract_product_identity(data, "TCL 85C7K")
    assert identity.identifiers.get("gtin") == expected


class TermFetcher:
    """Answers Darty's suggest endpoint per query, like the live store does."""

    validate_url = staticmethod(Fetcher.validate_url)

    def __init__(self, results):
        self.results, self.queries = results, []

    async def get(self, url, retailer, hosts, **kwargs):
        from urllib.parse import parse_qs, urlsplit

        term = parse_qs(urlsplit(url).query)["q"][0]
        self.queries.append(term)
        products = [{"title": t, "url": u} for t, u in self.results.get(term, [])]
        return json.dumps({"resources": {"results": {"products": products}}})


async def test_search_falls_back_from_name_to_model_to_barcode():
    from app.retailers.darty import DartyAdapter

    oos = ("Smart TV TCL 85C7K QLED Mini-LED", "/products/tcl-tv-mini-led-85c7k-4k-216cm")
    identity = identify("TCL 85C7K", identifiers={"gtin": "5901292525712"})
    # Live behaviour: "TCL 85C7K" only suggests in-stock neighbours; "85C7K" finds the page.
    fetcher = TermFetcher(
        {"TCL 85C7K": [("Smart TV TCL 85C7L", "/products/tcl-85c7l")], "85C7K": [oos]}
    )
    links = await DartyAdapter(fetcher).search_product(identity)
    assert links == ["https://www.darty.pt/products/tcl-tv-mini-led-85c7k-4k-216cm"]
    assert fetcher.queries == ["85C7K"]
    fetcher = TermFetcher({"5901292525712": [oos]})
    links = await DartyAdapter(fetcher).search_product(identity)
    assert fetcher.queries == ["85C7K", "TCL 85C7K", "5901292525712"]
    assert len(links) == 1


RADIO_URL = "https://www.radiopopular.pt/produto/tv-tcl-85c7l"


def radio_page(**replacements):
    html = (FIXTURES / "radio_85c7l_promo_live.html").read_text()
    for old, new in replacements.items():
        assert old in html, old
        html = html.replace(old, new)
    return html


def test_radio_popular_reads_the_product_header_not_similar_products(registry):
    # Live page: the "similar products" carousel carries its own itemprop price (2.699,99),
    # stock and sku before the product's; the header holds the product's real figures.
    listing = registry.adapters["radiopopular"].parse(radio_page(), RADIO_URL)[0]
    assert (listing.title, listing.identity.model) == ("TV TCL 85C7L", "85C7L")
    assert listing.price == Decimal("2499.99")
    assert (listing.promo_price, listing.promo_code) == (Decimal("1999.99"), "TV20")
    assert listing.deal_price == Decimal("1999.99")
    assert (listing.availability, listing.condition) == ("in_stock", "new")
    assert listing.retailer_product_id == "134791"


def test_radio_popular_without_a_promo_code(registry):
    html = re.sub(r'<div class="price-promocode-bar".*?</span></div>', "", radio_page(), flags=re.S)
    listing = registry.adapters["radiopopular"].parse(html, RADIO_URL)[0]
    assert (listing.promo_price, listing.promo_code, listing.deal_price) == (
        None,
        None,
        Decimal("2499.99"),
    )


@pytest.mark.parametrize(
    "button,expected",
    [
        ("Esgotado", "out_of_stock"),  # a sold-out label where the cart button was
        ("", "unknown"),  # no cart button and no stock text: never assume in stock
    ],
)
def test_radio_popular_stock_comes_from_the_product_header(registry, button, expected):
    html = re.sub(
        r'<div class="rp-button-blue buy[^"]*"[^>]*>.*?Adicionar ao carrinho\s*</div>',
        f'<div class="unavailable">{button}</div>',
        radio_page(),
        flags=re.S,
    )
    listing = registry.adapters["radiopopular"].parse(html, RADIO_URL)[0]
    assert listing.availability == expected


KK_URL = "https://www.kuantokusta.pt/p/12121920/tcl-85-85c7l-sqd-miniled-smart-google-tv-4k"


def test_kuantokusta_keeps_the_lowest_price_including_shipping(registry):
    # Chipman has the lowest sticker price (1708.95) but 124.99 shipping; Hipermercado's
    # 1748.18 with free shipping is what you would pay.
    page = (FIXTURES / "kuantokusta_85c7l_live.html").read_text()
    listing = registry.adapters["kuantokusta"].parse(page, KK_URL)[0]
    assert listing.price == Decimal("1748.18")
    assert listing.offered_by == "Hipermercado · free shipping"
    assert (listing.availability, listing.condition) == ("in_stock", "new")
    assert listing.identity.model == "85C7L"
    assert listing.identity.identifiers["gtin"] == "5901292529925"
    assert listing.retailer_product_id == "12121920"


def test_kuantokusta_names_paid_shipping():
    adapter = Registry(Fetcher(None, None)).adapters["kuantokusta"]
    page = (FIXTURES / "kuantokusta_85c7l_live.html").read_text()
    page = page.replace(
        '"Hipermercado", "storeSlug": "hipermercado", "price": 1748.18',
        '"Hipermercado", "storeSlug": "hipermercado", "price": 1799.0',
    )
    listing = adapter.parse(page, KK_URL)[0]
    assert (listing.price, listing.offered_by) == (Decimal("1748.23"), "Tek4Life · €28.33 shipping")


def test_kuantokusta_without_offers_is_out_of_stock(registry):
    page = re.sub(
        r'"offers": \[.*\]', '"offers": []', (FIXTURES / "kuantokusta_85c7l_live.html").read_text()
    )
    listing = registry.adapters["kuantokusta"].parse(page, KK_URL)[0]
    assert (listing.price, listing.availability, listing.offered_by) == (None, "out_of_stock", None)


def test_kuantokusta_urls_drop_search_tracking(registry):
    adapter = registry.adapters["kuantokusta"]
    assert adapter.normalize_url(KK_URL + "?queryId=527e58ffae478faa4ac00bee969daa90") == KK_URL
    assert registry.for_url("https://kuantokusta.pt/p/12121920/x").name == "kuantokusta"


async def test_kuantokusta_is_read_through_the_browser():
    pages = []

    class BrowserOnly(Fetcher):
        async def get(self, *args, **kwargs):  # plain requests get "Access Denied"
            raise AssertionError("KuantoKusta must not be fetched with plain HTTP")

        async def browse(self, url, retailer, hosts):
            pages.append((url, retailer))
            return (FIXTURES / "kuantokusta_85c7l_live.html").read_text()

    adapter = Registry(BrowserOnly(None, None)).adapters["kuantokusta"]
    listing = (await adapter.fetch_listing(KK_URL + "?queryId=x"))[0]
    assert pages == [(KK_URL, "kuantokusta")]
    assert listing.price == Decimal("1748.18")
