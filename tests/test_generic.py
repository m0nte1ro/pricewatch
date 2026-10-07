from decimal import Decimal

import pytest
from bs4 import BeautifulSoup

from app.retailers.generic import (
    css_path,
    currency_of,
    heuristic_availability,
    page_title,
    public_host,
    read_heuristic,
    read_meta,
    store_key,
)


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
