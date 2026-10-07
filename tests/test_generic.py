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
    read_rule,
    store_key,
    teach,
)
from app.retailers.http import Fetcher
from app.retailers.parsing import ScrapeError
from app.retailers.registry import Registry
from app.schemas.domain import PriceRule
from app.services.rules import RuleService


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


def test_price_less_structured_data_keeps_its_out_of_stock_state():
    adapter = GenericAdapter(Fetcher(None, None), "storeone.pt")
    snapshot = adapter.parse(
        '<h1>TCL 85C7K</h1><script type="application/ld+json">{"@type":"Product","name":"TCL 85C7K",'
        '"offers":{"availability":"https://schema.org/OutOfStock"}}</script>',
        "https://www.storeone.pt/p",
    )[0]
    assert (snapshot.method, snapshot.price, snapshot.availability) == (
        "heuristic",
        None,
        "out_of_stock",
    )


def test_price_less_structured_data_names_a_page_without_a_title():
    adapter = GenericAdapter(Fetcher(None, None), "storeone.pt")
    snapshot = adapter.parse(
        '<script type="application/ld+json">{"@type":"Product","name":"TCL 85C7K",'
        '"offers":{"availability":"https://schema.org/InStock"}}</script>',
        "https://www.storeone.pt/p",
    )[0]
    assert (snapshot.title, snapshot.price, snapshot.availability) == (
        "TCL 85C7K",
        None,
        "in_stock",
    )


OLD_PRICE_FIRST_PAGE = """<html><head><title>TV TCL 85C7K | Store One</title></head><body><main class="product"><h1>TV TCL 85C7K</h1>
<span class="price">1.499,00 €</span> <span class="price">1.299,99 €</span><div class="stock">Em stock</div></main></body></html>"""


def test_teach_derives_selectors():
    s = soup(HEURISTIC_PAGE)
    rule = teach(s, Decimal("1299.99"), "in_stock")
    assert s.select_one(rule.price_selector).get_text(strip=True) == "1.299,99 €"
    assert rule.availability_mode == "text"
    assert s.select_one(rule.availability_selector).get_text(strip=True) == "Em stock"
    reading = read_rule(s, rule)
    assert (reading.price, reading.availability, reading.method) == (
        Decimal("1299.99"),
        "in_stock",
        "rule",
    )


def test_teach_uses_cart_button_presence_when_no_stock_text():
    s = soup(
        '<h1>P</h1><span class="amount">999 €</span><button class="add">Adicionar ao carrinho</button>'
    )
    rule = teach(s, Decimal("999"), "in_stock")
    assert rule.availability_mode == "presence"
    assert read_rule(s, rule).availability == "in_stock"
    assert (
        read_rule(soup('<h1>P</h1><span class="amount">999 €</span>'), rule).availability
        == "out_of_stock"
    )


def test_teach_picks_most_specific_element_and_rejects_unknown_price():
    s = soup(
        '<div class="box"><span class="v">999 €</span><p class="note">Também por 999 € em loja</p></div>'
    )
    assert teach(s, Decimal("999"), None).price_selector.endswith("span.v")
    with pytest.raises(ScrapeError):
        teach(soup(HEURISTIC_PAGE), Decimal("5"), None)


def test_rule_service_round_trip(db):
    rules = RuleService(db)
    assert rules.get("storeone.pt") is None
    rules.save("storeone.pt", PriceRule(price_selector="span.a"))
    rules.save("storeone.pt", PriceRule(price_selector="span.b", availability_selector="div.s"))
    assert rules.get("storeone.pt").price_selector == "span.b"
    assert list(rules.all()) == ["storeone.pt"]
    rules.delete("storeone.pt")
    assert rules.get("storeone.pt") is None


def test_learn_saves_rule_and_reparses(db):
    rules = RuleService(db)
    adapter = GenericAdapter(Fetcher(None, None), "storeone.pt", rules=rules)
    assert adapter.parse(OLD_PRICE_FIRST_PAGE, "https://www.storeone.pt/p")[0].price == Decimal(
        "1499.00"
    )
    snapshot = adapter.learn(
        OLD_PRICE_FIRST_PAGE, "https://www.storeone.pt/p", Decimal("1299.99"), "in_stock"
    )
    assert (snapshot.price, snapshot.method, snapshot.availability) == (
        Decimal("1299.99"),
        "rule",
        "in_stock",
    )
    assert rules.get("storeone.pt") is not None
    assert adapter.parse(OLD_PRICE_FIRST_PAGE, "https://www.storeone.pt/p")[0].method == "rule"


def test_broken_rule_falls_back_and_flags(db):
    rules = RuleService(db)
    rules.save("storeone.pt", PriceRule(price_selector="span.gone"))
    adapter = GenericAdapter(Fetcher(None, None), "storeone.pt", rules=rules)
    snapshot = adapter.parse(HEURISTIC_PAGE, "https://www.storeone.pt/p")[0]
    assert (snapshot.method, snapshot.price) == ("heuristic", Decimal("1299.99"))
    assert rules.get("storeone.pt") is not None  # kept so the user can re-confirm or forget it


def test_broken_rule_is_logged_with_its_store(db, caplog):
    rules = RuleService(db)
    rules.save("storeone.pt", PriceRule(price_selector="span.gone"))
    GenericAdapter(Fetcher(None, None), "storeone.pt", rules=rules).parse(
        HEURISTIC_PAGE, "https://www.storeone.pt/p"
    )
    assert [(r.getMessage(), r.retailer) for r in caplog.records] == [
        ("store_rule_failed", "storeone.pt")
    ]


def test_rule_reading_wins_over_structured_data_and_offers_the_page_candidates(db):
    rules = RuleService(db)
    rules.save("storeone.pt", PriceRule(price_selector="span.price-current"))
    adapter = GenericAdapter(Fetcher(None, None), "storeone.pt", rules=rules)
    snapshot = adapter.parse(
        '<h1>TCL 85C7K</h1><script type="application/ld+json">{"@type":"Product","name":"TCL 85C7K",'
        '"offers":{"price":"1499.00","availability":"https://schema.org/OutOfStock"}}</script>'
        '<span class="price-current">1.299,99 €</span>',
        "https://www.storeone.pt/p",
    )[0]
    assert (snapshot.method, snapshot.price) == ("rule", Decimal("1299.99"))
    assert [c.price for c in snapshot.alternatives] == [Decimal("1299.99")]


def test_rule_reading_keeps_structured_out_of_stock_state(db):
    rules = RuleService(db)
    rules.save("storeone.pt", PriceRule(price_selector="span.price-current"))
    adapter = GenericAdapter(Fetcher(None, None), "storeone.pt", rules=rules)
    snapshot = adapter.parse(
        '<h1>TCL 85C7K</h1><script type="application/ld+json">{"@type":"Product","name":"TCL 85C7K",'
        '"offers":{"availability":"https://schema.org/OutOfStock"}}</script>'
        '<span class="price-current">1.299,99 €</span>',
        "https://www.storeone.pt/p",
    )[0]
    assert (snapshot.method, snapshot.availability) == ("rule", "out_of_stock")


def test_teach_prefers_the_innermost_element_on_equal_text():
    s = soup(
        '<div class="price-box"><span class="price">1.299 €</span></div>'
        '<div class="stock"><span class="label">Em stock</span></div>'
    )
    rule = teach(s, Decimal("1299"), "in_stock")
    assert s.select_one(rule.price_selector) is s.select_one("span.price")
    assert s.select_one(rule.availability_selector) is s.select_one("span.label")


def test_teach_never_stores_a_rule_its_reader_would_refuse():
    with pytest.raises(ScrapeError, match="Could not find a price of 3 on the page"):
        teach(soup('<span class="x">Garantia 3 anos</span>'), Decimal("3"), None)


@pytest.mark.parametrize(
    "text",
    [
        "Garantia 3 anos",
        "4,5 (123 avaliações)",
        "de 1.499 € por 999 €",
        "1.299 € ou 3x 433 €",
        "Garantia europeia 3 anos",
        "Poupe 200 euros",
    ],
)
def test_rule_refuses_text_that_is_not_a_single_price(text):
    page = soup(f'<h1>P</h1><span class="x">{text}</span>')
    assert read_rule(page, PriceRule(price_selector="span.x")) is None


def test_rule_reads_a_bare_amount():
    reading = read_rule(
        soup('<h1>P</h1><span class="x">1299,99</span>'), PriceRule(price_selector="span.x")
    )
    assert (reading.price, reading.method) == (Decimal("1299.99"), "rule")


@pytest.mark.parametrize(
    ("text", "price"),
    [
        ("EUR 1.299,99", Decimal("1299.99")),
        ("1.299,99 EUR", Decimal("1299.99")),
        ("1.299,99EUR", Decimal("1299.99")),
        ("1.299 €", Decimal("1299.00")),
    ],
)
def test_rule_reads_a_price_marked_by_symbol_or_code(text, price):
    reading = read_rule(
        soup(f'<h1>P</h1><span class="x">{text}</span>'), PriceRule(price_selector="span.x")
    )
    assert (reading.price, reading.currency) == (price, "EUR")


def test_currency_word_inside_a_price_label_is_not_a_second_marker():
    candidates = price_candidates(soup('<h1>P</h1><span class="price">1.299,99 € (euros)</span>'))
    assert [c.price for c in candidates] == [Decimal("1299.99")]


def test_rule_whose_element_gained_instalment_text_falls_back(db, caplog):
    rules = RuleService(db)
    rules.save("storeone.pt", PriceRule(price_selector="div.price-box"))
    snapshot = GenericAdapter(Fetcher(None, None), "storeone.pt", rules=rules).parse(
        '<h1>TV TCL 85C7K</h1><div class="price-box"><span class="price">1.299 €</span> '
        "<small>ou 3x 433 €</small></div>",
        "https://www.storeone.pt/p",
    )[0]
    assert (snapshot.method, snapshot.price) == ("heuristic", Decimal("1299.00"))
    assert [r.getMessage() for r in caplog.records] == ["store_rule_failed"]


CAROUSEL_PAGE = """<html><head><title>TV TCL 85C7K | Store One</title></head><body>
<main class="product"><h1>TV TCL 85C7K</h1><span class="price-current">1.299,99 €</span>
<div class="availability">Em stock</div><button class="add">Adicionar ao carrinho</button></main>
<section class="related-products">
<div class="card"><span class="name">Cabo HDMI</span><span class="price">9,99 €</span><span class="badge">Em stock</span></div>
<div class="card"><span class="name">Suporte</span><span class="price">29,99 €</span><span class="badge">Em stock</span></div>
</section></body></html>"""


def test_taught_stock_rule_reads_the_product_and_not_a_recommendation_badge():
    s = soup(CAROUSEL_PAGE)
    rule = teach(s, Decimal("1299.99"), "in_stock")
    assert s.select_one(rule.availability_selector) is s.select_one("div.availability")
    sold_out = CAROUSEL_PAGE.replace(
        '<div class="availability">Em stock</div><button class="add">Adicionar ao carrinho</button>',
        '<div class="availability">Esgotado</div><button class="add" disabled>Esgotado</button>',
    )
    reading = read_rule(soup(sold_out), rule)
    assert (reading.price, reading.availability) == (Decimal("1299.99"), "out_of_stock")


def test_teach_takes_no_stock_evidence_from_a_recommendation_block():
    s = soup(
        '<section class="related-products"><div class="card"><span class="badge">Em stock</span>'
        '<button class="add">Comprar</button></div></section><main class="product"><h1>P</h1>'
        '<span class="amount">999 €</span><button class="add">Adicionar ao carrinho</button></main>'
    )
    rule = teach(s, Decimal("999"), "in_stock")
    assert rule.availability_mode == "presence"
    assert s.select_one(rule.availability_selector) is s.select_one("main button")
    only_carousel = soup(
        '<main><h1>P</h1><span class="amount">999 €</span></main><section class="related-products">'
        '<span class="badge">Esgotado</span><button>Comprar</button></section>'
    )
    assert teach(only_carousel, Decimal("999"), "in_stock").availability_selector is None
    assert teach(only_carousel, Decimal("999"), "out_of_stock").availability_selector is None


def test_teach_prefers_a_stock_label_then_any_text_outside_recommendations():
    page = (
        '<main><h1>P</h1><span class="amount">999 €</span><div class="stock-info">'
        "<span>Disponível em stock</span></div></main><footer><span>Em stock</span></footer>"
    )
    s = soup(page)
    rule = teach(s, Decimal("999"), "in_stock")
    assert s.select_one(rule.availability_selector) is s.select_one("div.stock-info span")
    s = soup(page.replace('class="stock-info"', 'class="info"'))
    rule = teach(s, Decimal("999"), "in_stock")
    assert s.select_one(rule.availability_selector) is s.select_one("footer span")


def test_sold_out_text_beats_an_enabled_cart_button():
    page = (
        '<h1>P</h1><span class="price">999 €</span><p class="msg">Esgotado</p>'
        '<button class="add">Comprar</button>'
    )
    assert heuristic_availability(soup(page)) == "out_of_stock"


def test_recommendation_blocks_are_not_stock_evidence():
    sold_out_elsewhere = (
        '<main><h1>P</h1><button class="add">Comprar</button></main>'
        '<section class="related-products"><p>Esgotado</p></section>'
    )
    assert heuristic_availability(soup(sold_out_elsewhere)) == "in_stock"
    buyable_elsewhere = (
        '<main><h1>P</h1><p>Descrição</p></main><section class="related-products">'
        '<span class="stock">Em stock</span><button>Comprar</button></section>'
    )
    assert heuristic_availability(soup(buyable_elsewhere)) == "unknown"


def test_sold_out_text_in_scripts_and_comments_is_not_shown_to_shoppers():
    page = '<script>var t = {"oos": "Esgotado"}</script><!-- esgotado --><button>Comprar</button>'
    assert heuristic_availability(soup(page)) == "in_stock"


STOCK_DIV = '<div class="stock">Em stock</div>'


@pytest.mark.parametrize(
    ("replacement", "expected"),
    [
        ('<p class="msg">Produto esgotado</p>', "out_of_stock"),
        ("", "unknown"),
        ('<button class="add">Comprar</button>', "unknown"),
        ('<section class="related-products"><p>Esgotado</p></section>', "unknown"),
    ],
)
def test_text_rule_whose_stock_element_is_gone_reads_only_a_sold_out_notice(replacement, expected):
    rule = teach(soup(OLD_PRICE_FIRST_PAGE), Decimal("1299.99"), "in_stock")
    page = OLD_PRICE_FIRST_PAGE.replace(STOCK_DIV, replacement)
    assert read_rule(soup(page), rule).availability == expected


CART_RULE = PriceRule(
    price_selector="span.amount", availability_selector="button.add", availability_mode="presence"
)


@pytest.mark.parametrize(
    ("button", "expected"),
    [
        ('<button class="add">Esgotado</button>', "out_of_stock"),
        ('<button class="add" disabled>Adicionar ao carrinho</button>', "out_of_stock"),
        ('<button class="add">Adicionar ao carrinho</button>', "in_stock"),
    ],
)
def test_presence_rule_needs_a_working_cart_button(button, expected):
    page = soup(f'<h1>P</h1><span class="amount">999 €</span>{button}')
    assert read_rule(page, CART_RULE).availability == expected
