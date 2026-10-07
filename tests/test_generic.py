from decimal import Decimal

import pytest
from bs4 import BeautifulSoup

from app.retailers.generic import (
    GenericAdapter,
    css_path,
    currency_of,
    heuristic_availability,
    page_title,
    price_candidates,
    public_host,
    read_heuristic,
    read_meta,
    store_key,
)
from app.retailers.http import Fetcher
from app.retailers.parsing import ScrapeError
from app.retailers.registry import Registry


def soup(html):
    return BeautifulSoup(html, "html.parser")


HEURISTIC_PAGE = """<html><head><title>TV TCL 85C7K | Loja Exemplo</title></head><body>
<header><a class="cart-link" href="/cart">Carrinho</a></header>
<main class="product-detail"><h1>TV TCL 85C7K MiniLED 85"</h1>
<div class="product-price"><span class="old-price">1.499,00 €</span><span class="price-current">1.299,99 €</span></div>
<div class="stock-status">Em stock</div><button class="btn-add-to-cart">Adicionar ao carrinho</button></main>
<section class="related-products"><div class="price">29,99 €</div></section></body></html>"""


def test_heuristic_prefers_current_price_over_old_and_recommendations():
    reading = read_heuristic(soup(HEURISTIC_PAGE))
    assert (reading.price, reading.currency, reading.availability, reading.method) == (
        Decimal("1299.99"),
        "EUR",
        "in_stock",
        "heuristic",
    )
    assert reading.alternatives[0].selector.endswith("span.price-current")
    assert {c.price for c in reading.alternatives} == {
        Decimal("1299.99"),
        Decimal("1499.00"),
        Decimal("29.99"),
    }


def test_css_path_is_unique_and_short():
    s = soup(HEURISTIC_PAGE)
    element = s.select_one(".price-current")
    path = css_path(element, s)
    assert s.select(path) == [element] and path.count(">") <= 3


def test_css_path_disambiguates_siblings():
    s = soup('<div><span class="price">1</span><span class="price">2</span></div>')
    second = s.select(".price")[1]
    assert s.select(css_path(second, s)) == [second]


def test_page_title_strips_store_suffix():
    assert page_title(soup("<title>TV TCL 85C7K | Loja Exemplo</title>")) == "TV TCL 85C7K"
    assert (
        page_title(soup('<meta property="og:title" content="OG name"><title>x - y</title>'))
        == "OG name"
    )
    assert page_title(soup("<h1>Heading</h1><title>T | S</title>")) == "Heading"
    assert page_title(soup("<p>nothing</p>")) == ""


META_PAGE = (
    '<meta property="og:title" content="TCL 85C7K"><meta property="product:price:amount" content="1299.99">'
    '<meta property="product:price:currency" content="EUR"><meta property="product:availability" content="instock">'
)


def test_meta_tags_reading():
    reading = read_meta(soup(META_PAGE))
    assert (reading.price, reading.currency, reading.availability, reading.method) == (
        Decimal("1299.99"),
        "EUR",
        "in_stock",
        "meta",
    )
    assert read_meta(soup("<title>no meta</title>")) is None


def test_heuristic_availability_rules():
    assert (
        heuristic_availability(
            soup('<div class="availability">Esgotado</div><button>Adicionar ao carrinho</button>')
        )
        == "out_of_stock"
    )
    assert heuristic_availability(soup('<button class="buy">Comprar</button>')) == "in_stock"
    assert (
        heuristic_availability(soup("<button disabled>Comprar</button><p>Produto esgotado</p>"))
        == "out_of_stock"
    )
    assert heuristic_availability(soup('<a href="/cart">Carrinho</a><p>Descrição</p>')) == "unknown"


def test_read_heuristic_without_price():
    reading = read_heuristic(soup("<h1>Produto</h1><p>Sem preço aqui</p>"))
    assert (reading.price, reading.alternatives, reading.availability) == (None, [], "unknown")


@pytest.mark.parametrize(
    "host,ok",
    [
        ("www.pcdiga.com", True),
        ("loja.pt", True),
        ("localhost", False),
        ("192.168.1.10", False),
        ("nas", False),
        ("printer.local", False),
        ("store.lan", False),
        ("[::1]", False),
    ],
)
def test_public_host(host, ok):
    assert public_host(host) is ok


@pytest.mark.parametrize("host", ["127.1", "0x7f.1", "localhost.", "printer.local."])
def test_public_host_rejects_other_spellings_of_local_hosts(host):
    assert public_host(host) is False


def test_css_path_escapes_names_the_selector_engine_rejects():
    s = soup('<o:p class="-1a"><span class="price">1</span></o:p><span class="price">2</span>')
    first = s.select("span")[0]
    found = s.select(css_path(first, s))
    assert len(found) == 1 and found[0] is first


def test_store_key():
    assert store_key("WWW.PCDiga.com") == "pcdiga.com"
    assert store_key("loja.pcdiga.com") == "loja.pcdiga.com"


@pytest.mark.parametrize(
    "text,expected", [("1.299,99 €", "EUR"), ("£999", "GBP"), ("$999", "USD"), ("999", "EUR")]
)
def test_currency_of(text, expected):
    assert currency_of(text) == expected


def test_wrapper_holding_two_whole_prices_is_not_a_candidate():
    candidates = price_candidates(
        soup(
            '<main class="product"><div class="product-price"><span class="old-price">1499 €</span>'
            '<span class="price-current">1299 €</span></div></main>'
        )
    )
    assert (candidates[0].selector, candidates[0].price) == (
        "span.price-current",
        Decimal("1299.00"),
    )
    assert all(c.price < 10000 for c in candidates)


def test_split_cents_price_reads_as_one_amount():
    candidates = price_candidates(soup('<span class="price">783 <sup>,57 €</sup></span>'))
    assert candidates[0].price == Decimal("783.57")


NESTED_PAGE = (
    '<div class="carousel">'
    + "<div>" * 8
    + '<span class="price">1,00 €</span>'
    + "</div>" * 8
    + '</div><div class="product">'
    + "<div>" * 8
    + '<span class="price">2,00 €</span>'
    + "</div>" * 8
    + "</div>"
)


def test_candidate_selector_points_at_the_element_it_reports():
    s = soup(NESTED_PAGE)
    candidates = price_candidates(s)
    assert [c.price for c in candidates] == [Decimal("2.00"), Decimal("1.00")]
    assert [s.select_one(c.selector).get_text(" ", strip=True) for c in candidates] == [
        "2,00 €",
        "1,00 €",
    ]


def test_css_path_selects_exactly_its_element_when_short_paths_are_ambiguous():
    s = soup(NESTED_PAGE)
    first = s.select("span.price")[0]
    found = s.select(css_path(first, s))
    assert len(found) == 1 and found[0] is first


def test_candidate_is_dropped_when_no_selector_reaches_only_its_element():
    # Without a single root element, a deeper copy of the top-level chain matches first.
    s = soup(
        '<div><div></div><div><span class="price">9 €</span></div></div>'
        '<div><span class="price">5 €</span></div>'
    )
    assert [c.price for c in price_candidates(s)] == [Decimal("9.00")]


META_PRICE = '<meta property="product:price:amount" content="10">'


@pytest.mark.parametrize(
    "value,expected",
    [("out of stock", "out_of_stock"), ("oos", "out_of_stock"), ("weird", "unknown")],
)
def test_meta_availability_is_never_replaced_by_the_cart_button(value, expected):
    page = f'{META_PRICE}<meta property="product:availability" content="{value}"><button>Comprar</button>'
    assert read_meta(soup(page)).availability == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("in stock", "in_stock"),
        (" InStock ", "in_stock"),
        ("https://schema.org/SoldOut", "out_of_stock"),
        ("pre-order", "preorder"),
        ("https://schema.org/LimitedAvailability", "in_stock"),
        ("OnlineOnly", "in_stock"),
        ("PreSale", "preorder"),
    ],
)
def test_meta_availability_vocabulary(value, expected):
    page = f'{META_PRICE}<meta property="og:availability" content="{value}"><p>Esgotado</p>'
    assert read_meta(soup(page)).availability == expected


def test_meta_without_availability_tag_falls_back_to_the_page():
    assert read_meta(soup(f"{META_PRICE}<button>Comprar</button>")).availability == "in_stock"


def test_registry_resolves_known_and_generic():
    registry = Registry(Fetcher(None, None))
    assert registry.for_url("https://www.worten.pt/produtos/x-1").name == "worten"
    adapter = registry.for_url("https://www.pcdiga.com/tv-tcl-85c7k")
    assert (adapter.name, adapter.label, adapter.hosts) == (
        "pcdiga.com",
        "pcdiga.com",
        ("pcdiga.com", "www.pcdiga.com"),
    )
    assert registry["pcdiga.com"] is adapter and registry.for_url("https://pcdiga.com/x") is adapter
    assert dict(registry.items()).keys() == registry.adapters.keys()
    with pytest.raises(ScrapeError):
        registry.for_url("https://127.0.0.1/x")
    assert (
        adapter.normalize_url("https://www.pcdiga.com/tv//tcl/?utm_source=a&b=1#x")
        == "https://www.pcdiga.com/tv/tcl?b=1"
    )


def test_generic_url_keeps_the_pasted_host_lowercased():
    adapter = Registry(Fetcher(None, None)).for_url("https://PCDiga.com/TV/")
    assert adapter.normalize_url("https://PCDiga.com/TV/") == "https://pcdiga.com/TV"


def test_registry_rejects_links_without_a_host():
    with pytest.raises(ScrapeError, match="Enter a full https:// link"):
        Registry(Fetcher(None, None)).for_url("https:///produto/x")


def test_generic_parse_tiers():
    adapter = GenericAdapter(Fetcher(None, None), "storeone.pt")
    heuristic = adapter.parse(HEURISTIC_PAGE, "https://www.storeone.pt/p")[0]
    assert (heuristic.method, heuristic.price, heuristic.condition, heuristic.identity.model) == (
        "heuristic",
        Decimal("1299.99"),
        "new",
        "85C7K",
    )
    assert len(heuristic.alternatives) == 3
    meta = adapter.parse("<title>TCL 85C7K</title>" + META_PAGE, "https://www.storeone.pt/p")[0]
    assert (meta.method, meta.price) == ("meta", Decimal("1299.99"))
    structured = adapter.parse(
        '<h1>TCL 85C7K</h1><script type="application/ld+json">{"@type":"Product","name":"TCL 85C7K",'
        '"offers":{"price":"1199.00","priceCurrency":"EUR","availability":"https://schema.org/InStock"}}</script>',
        "https://www.storeone.pt/p",
    )[0]
    assert (structured.method, structured.price, structured.availability) == (
        "structured",
        Decimal("1199.00"),
        "in_stock",
    )
    with pytest.raises(ScrapeError):
        adapter.parse("<html><body><div id=app></div></body></html>", "https://www.storeone.pt/p")


def test_structured_data_is_read_without_a_title_element():
    adapter = GenericAdapter(Fetcher(None, None), "storeone.pt")
    snapshot = adapter.parse(
        '<script type="application/ld+json">{"@type":"Product","name":"TCL 85C7K",'
        '"offers":{"price":"1199.00","priceCurrency":"EUR"}}</script>',
        "https://www.storeone.pt/p",
    )[0]
    assert (snapshot.method, snapshot.title, snapshot.price) == (
        "structured",
        "TCL 85C7K",
        Decimal("1199.00"),
    )


def test_generic_adapters_never_hold_a_non_public_host():
    registry = Registry(Fetcher(None, None))
    for url in ("https://www.nas/p", "https://www.192.168.1.10/p", "https://www.0x7f.1/p"):
        with pytest.raises(ScrapeError, match="Only public https:// store links"):
            registry.for_url(url)
    with pytest.raises(ScrapeError, match="Only public https:// store links"):
        registry["nas"]
    with pytest.raises(ScrapeError, match="Only public https:// store links"):
        GenericAdapter(Fetcher(None, None), "nas").normalize_url("https://nas/admin")
    assert registry.generic == {}


def test_structured_data_without_a_price_falls_through_to_the_page():
    adapter = GenericAdapter(Fetcher(None, None), "storeone.pt")
    snapshot = adapter.parse(
        '<h1>TCL 85C7K</h1><script type="application/ld+json">{"@type":"Product","name":"TCL 85C7K",'
        '"offers":{"availability":"https://schema.org/InStock"}}</script>'
        '<span class="price-current">1.299,99 €</span>',
        "https://www.storeone.pt/p",
    )[0]
    assert (snapshot.method, snapshot.price) == ("heuristic", Decimal("1299.99"))
