import json
import re
import time
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models import Alert, DiscoveryDraft, Listing, PriceHistory, Product, RetailerState
from app.retailers import generic
from app.schemas.domain import PriceRule, now
from tests.conftest import GENERIC_PAGES, URLS
from tests.test_generic import OLD_PRICE_FIRST_PAGE

STORE_PAGE = """<html><head><title>TV TCL 85C7K | Store One</title></head><body><main class="product">
<h1>TV TCL 85C7K MiniLED 85"</h1><span class="price-current">1.299,99 €</span><div class="stock">Em stock</div></main></body></html>"""


def payload(name="TCL 85C7K", urls=None):
    return {
        "name": name,
        "urls": urls or [],
        "category": "tv",
        "target_price": "900",
        "insane_deal_price": "800",
        "retailers": list(URLS),
    }


def form_data(client, **kwargs):
    return {"csrf": client.cookies.get("pricewatch_csrf"), **kwargs}


def discover(client, **kwargs):
    inputs = {
        "name": "TCL 85C7K",
        "retailers": list(URLS),
        "target_price": "900",
        "insane_deal_price": "800",
        **kwargs,
    }
    response = client.post("/discoveries", data=form_data(client, **inputs), follow_redirects=False)
    assert response.status_code == 303, response.text
    path = response.headers["location"]
    for _ in range(200):
        page = client.get(path)
        if "Confirm and monitor" in page.text or "Discovery failed" in page.text:
            break
        time.sleep(0.01)
    assert "Confirm and monitor" in page.text, page.text
    return path, page


def test_all_pages_load(site):
    client, _, _, _ = site
    for path in ("/", "/products/new", "/alerts", "/settings", "/health"):
        assert client.get(path).status_code == 200
    assert "Your watchlist" in client.get("/").text
    assert client.get("/static/vendor/htmx.min.js").status_code == 200
    assert client.get("/static/vendor/chart.umd.js").status_code == 200


def test_url_only_additive_discovery_multi_retailer_and_idempotent_save(site):
    client, runtime, _, requests = site
    path, page = discover(
        client, name="", urls=[URLS["worten"], URLS["worten"] + "?utm_source=test"]
    )
    assert "TCL 85C7K" in page.text
    assert "discovered + manual" in page.text
    assert any("fnac.pt" in r for r in requests)
    response = client.post(
        path + "/confirm",
        data=form_data(client, selected=["0", "1", "2", "3", "4"]),
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    product_path = response.headers["location"]
    assert client.get(product_path).status_code == 200
    assert len(client.get(product_path + "/history").json()["series"]) == 5
    client.post(path + "/confirm", data=form_data(client, selected=["0"]))
    with runtime.db.session() as session:
        assert session.scalar(select(func.count()).select_from(Product)) == 1
        assert session.scalar(select(func.count()).select_from(Listing)) == 5


def test_name_only_and_multiple_urls_without_name(site):
    client, _, _, _ = site
    discover(client, name="TCL 85C7K", urls=[])
    discover(client, name="", urls=[URLS["worten"], URLS["amazon_es"]])


def test_edit_disable_archive_restore_preserves_history(site):
    client, runtime, _, _ = site
    path, _ = discover(client)
    response = client.post(
        path + "/confirm", data=form_data(client, selected=["0"]), follow_redirects=False
    )
    product_path = response.headers["location"]
    response = client.post(
        product_path + "/edit",
        data=form_data(
            client,
            canonical_name="Living room TV",
            target_price="850",
            insane_deal_price="700",
            enabled="on",
        ),
    )
    assert "Living room TV" in response.text
    assert client.post("/listings/1/toggle", data=form_data(client)).status_code == 200
    assert "Enable" in client.get(product_path).text
    client.post(product_path + "/archive", data=form_data(client))
    assert "Living room TV" not in client.get("/").text
    assert "Living room TV" in client.get("/?archived=true").text
    assert client.get(product_path + "/history").json()["series"][0]["data"]
    client.post(product_path + "/archive", data=form_data(client))
    assert "Living room TV" in client.get("/").text


def test_invalid_forms_and_csrf(site):
    client, _, _, _ = site
    assert client.post("/checks", data={}).status_code == 403
    assert (
        client.post(
            "/checks", data=form_data(client), headers={"Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert client.post("/discoveries", data=form_data(client)).status_code == 422
    assert (
        client.post(
            "/discoveries", data=form_data(client, name="TV", target_price="NaN")
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/discoveries", data=form_data(client, name="TV", urls=["http://127.0.0.1/admin"])
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/discoveries",
            data=form_data(client, name="TV", target_price="10", insane_deal_price="20"),
        ).status_code
        == 422
    )


def test_settings_secret_not_rendered_and_retained(site):
    client, runtime, _, _ = site
    data = form_data(
        client,
        polling_minutes="45",
        request_timeout="15",
        user_agent="Pricewatch test",
        ntfy_url="https://ntfy.sh",
        ntfy_topic="example",
        ntfy_token="secret-test-value",
        retailers=["worten"],
    )
    response = client.post("/settings", data=data)
    assert response.status_code == 200
    assert "secret-test-value" not in response.text
    data["ntfy_token"] = ""
    client.post("/settings", data=data)
    assert runtime.settings.get().ntfy_token == "secret-test-value"
    assert runtime.settings.get().polling_minutes == 45


async def test_uncertain_and_conflicting_candidate_review(site):
    _, runtime, _, _ = site
    draft_id = runtime.discovery.create(payload(name="An unspecified television"))
    await runtime.discovery.run(draft_id)
    with runtime.db.session() as session:
        draft = session.get(DiscoveryDraft, draft_id)
        assert all(c["match"]["level"] == "LOW" for c in draft.results["candidates"])
    product_id = runtime.discovery.confirm(draft_id, [])
    with runtime.db.session() as session:
        assert session.get(Product, product_id).listings == []


async def test_manual_adapter_failure_does_not_stop_independent_discovery(site, monkeypatch):
    _, runtime, _, _ = site

    async def broken(url, *, alternatives=False):
        raise KeyError("unexpected retailer markup")

    monkeypatch.setattr(runtime.registry.adapters["worten"], "fetch_listing", broken)
    draft_id = runtime.discovery.create(payload(urls=[URLS["worten"]]))
    await runtime.discovery.run(draft_id)
    with runtime.db.session() as session:
        draft = session.get(DiscoveryDraft, draft_id)
        assert draft.status == "ready"
        assert len(draft.results["candidates"]) == 4
        assert any("manual URL adapter failed" in error for error in draft.results["errors"])


def cool_down(runtime, *retailers):
    with runtime.db.session() as session:
        for name in retailers:
            session.merge(
                RetailerState(name=name, blocked_until=now() + timedelta(hours=1), last_error="403")
            )


def test_pasted_url_is_fetched_even_while_its_store_is_cooling_down(site):
    # A cooldown left by an earlier failure must not swallow a URL the user just pasted.
    client, runtime, _, requests = site
    cool_down(runtime, "worten", "fnac")
    path, page = discover(client, urls=[URLS["worten"]])
    assert "temporarily unavailable" not in page.text
    assert "discovered + manual" in page.text
    assert any(r.startswith(URLS["worten"]) for r in requests)


def test_no_listings_page_opens_discovery_notes(site):
    client, runtime, prices, _ = site
    for name in URLS:
        prices[name] = "blocked"
    # Search only: a pasted link would be kept as "not read yet" instead (see below).
    _, page = discover(client, urls=[])
    assert "No listings found" in page.text
    assert '<details class="panel warnings" open>' in page.text
    assert "Retailer returned HTTP 403" in page.text


def test_restart_lifts_cooldowns(site):
    _, runtime, _, _ = site
    cool_down(runtime, "worten")
    runtime.fetcher.retry_now()
    with runtime.db.session() as session:
        assert session.get(RetailerState, "worten").blocked_until is None


def test_unreachable_store_link_is_reported_without_discarding_the_others(site):
    client, _, _, _ = site
    _, page = discover(
        client,
        urls=[URLS["worten"], "https://www.pcdiga.com/tv-tcl-85c7k", URLS["darty"] + "?ref=mine"],
    )
    assert "Manual URL (www.pcdiga.com): HTTP 404" in page.text
    assert '<details class="panel warnings" open>' in page.text
    assert page.text.count("discovered + manual") == 2


def test_listing_interval_override(site):
    client, runtime, _, _ = site
    path, _ = discover(client, urls=[URLS["worten"]], retailers=[])
    product_path = client.post(
        path + "/confirm", data=form_data(client, selected=["0"]), follow_redirects=False
    ).headers["location"]
    with runtime.db.session() as session:
        listing_id = session.scalar(select(Listing)).id
    response = client.post(
        f"/listings/{listing_id}/interval",
        data=form_data(client, minutes="15"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    with runtime.db.session() as session:
        listing = session.get(Listing, listing_id)
        assert listing.check_interval_minutes == 15
        assert listing.next_check_at <= now() + timedelta(minutes=16)
    rejected = client.post(f"/listings/{listing_id}/interval", data=form_data(client, minutes="3"))
    assert rejected.status_code == 422
    assert "Interval must be between 5 and 10080 minutes" in rejected.text
    client.post(f"/listings/{listing_id}/interval", data=form_data(client, minutes=""))
    with runtime.db.session() as session:
        assert session.get(Listing, listing_id).check_interval_minutes is None
    assert 'name="minutes"' in client.get(product_path).text


async def test_generic_link_is_monitored(site):
    client, runtime, _, _ = site
    url = "https://www.storeone.pt/produto/tcl-85c7k"
    GENERIC_PAGES[url] = STORE_PAGE
    path, page = discover(client, name="TCL 85C7K", urls=[url + "?utm_source=x"], retailers=[])
    assert "storeone.pt" in page.text and "1299.99" in page.text
    product_path = client.post(
        path + "/confirm", data=form_data(client, selected=["0"]), follow_redirects=False
    ).headers["location"]
    with runtime.db.session() as session:
        listing = session.scalar(select(Listing))
        assert (listing.retailer, listing.url, listing.condition) == ("storeone.pt", url, "new")
        assert (listing.current_price, listing.availability, listing.extraction_method) == (
            Decimal("1299.99"),
            "in_stock",
            "heuristic",
        )
        listing.next_check_at = now() - timedelta(minutes=1)
    await runtime.monitor.run()
    with runtime.db.session() as session:
        assert session.scalar(select(func.count()).select_from(PriceHistory)) == 2
        assert session.scalar(select(Listing)).last_error is None
    assert "storeone.pt" in client.get(product_path).text


def test_generic_store_key_and_url_variants_dedupe(site):
    client, runtime, _, _ = site
    GENERIC_PAGES["https://www.storeone.pt/produto/tcl-85c7k"] = STORE_PAGE
    GENERIC_PAGES["https://storeone.pt/produto/tcl-85c7k"] = STORE_PAGE
    path, page = discover(
        client,
        name="TCL 85C7K",
        retailers=[],
        urls=[
            "https://www.storeone.pt/produto/tcl-85c7k#reviews",
            "https://www.storeone.pt/produto/tcl-85c7k?gclid=1",
            "https://storeone.pt/produto/tcl-85c7k",
        ],
    )
    assert page.text.count('name="selected"') == 1
    client.post(path + "/confirm", data=form_data(client, selected=["0"]))
    with runtime.db.session() as session:
        assert session.scalar(select(func.count()).select_from(Listing)) == 1
        assert session.scalar(select(Listing)).retailer == "storeone.pt"


def test_http_link_is_rejected_at_the_form(site):
    client, _, _, _ = site
    response = client.post(
        "/discoveries",
        data=form_data(client, name="TV", urls=["http://www.storeone.pt/p"], retailers=[]),
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "url",
    [
        "https://192.168.1.10/produto/x",
        "https://localhost/produto/x",
        "https://nas/produto/x",
        "https://printer.local/p",
        "https://www.nas/produto/x",
        "https://www.192.168.1.10/produto/x",
    ],
)
def test_non_public_links_become_notes_and_are_never_requested(site, url):
    client, _, _, requests = site
    _, page = discover(client, name="TCL 85C7K", urls=[url], retailers=[])
    assert "Only public https:// store links can be monitored" in page.text
    assert not any(url.split("/")[2] in r for r in requests)


async def test_generic_cooldowns_are_per_host(site):
    client, runtime, _, _ = site
    GENERIC_PAGES["https://www.storeone.pt/p/a"] = STORE_PAGE
    GENERIC_PAGES["https://www.storetwo.pt/p/a"] = STORE_PAGE.replace("Store One", "Store Two")
    path, _ = discover(
        client,
        name="TCL 85C7K",
        urls=["https://www.storeone.pt/p/a", "https://www.storetwo.pt/p/a"],
        retailers=[],
    )
    client.post(path + "/confirm", data=form_data(client, selected=["0", "1"]))
    GENERIC_PAGES["www.storeone.pt"] = 403
    with runtime.db.session() as session:
        for listing in session.scalars(select(Listing)):
            listing.next_check_at = now() - timedelta(minutes=1)
    await runtime.monitor.run()
    with runtime.db.session() as session:
        rows = {x.retailer: x for x in session.scalars(select(Listing))}
        assert rows["storeone.pt"].last_error and rows["storetwo.pt"].last_error is None
        assert session.get(RetailerState, "storeone.pt").blocked_until is not None
        assert session.get(RetailerState, "storetwo.pt").blocked_until is None


def test_generic_page_without_price_is_kept_and_flagged(site):
    client, runtime, _, _ = site
    GENERIC_PAGES["https://www.storeone.pt/p/js"] = (
        "<html><head><title>TV TCL 85C7K | Store One</title></head><body><div id=app></div></body></html>"
    )
    path, page = discover(
        client, name="TCL 85C7K", urls=["https://www.storeone.pt/p/js"], retailers=[]
    )
    assert 'name="selected"' in page.text
    client.post(path + "/confirm", data=form_data(client, selected=["0"]))
    with runtime.db.session() as session:
        listing = session.scalar(select(Listing))
        assert (listing.current_price, listing.availability, listing.extraction_method) == (
            None,
            "unknown",
            "heuristic",
        )


def test_redirect_to_a_non_public_host_is_never_followed(site):
    client, _, _, requests = site
    GENERIC_PAGES["https://www.storeone.pt/p/r"] = (302, "https://nas/admin")
    _, page = discover(client, name="TCL 85C7K", urls=["https://www.storeone.pt/p/r"], retailers=[])
    assert "Manual URL (www.storeone.pt): Retailer request failed" in page.text
    assert requests == ["https://www.storeone.pt/p/r"]


def test_confirm_price_from_preview_teaches_store_rule(site):
    client, runtime, _, _ = site
    url = "https://www.storeone.pt/produto/tcl-85c7k"
    GENERIC_PAGES[url] = OLD_PRICE_FIRST_PAGE
    path, page = discover(client, name="TCL 85C7K", urls=[url], retailers=[])
    assert "price unconfirmed" in page.text and "1.499,00" in page.text and "1.299,99" in page.text
    assert (
        client.post(
            path + "/teach/0", data=form_data(client, price="5", availability="unknown")
        ).status_code
        == 422
    )
    assert (
        client.post(
            path + "/teach/0", data=form_data(client, price="abc", availability="in_stock")
        ).status_code
        == 422
    )
    response = client.post(
        path + "/teach/0",
        data=form_data(client, price="1299,99", availability="in_stock"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = client.get(path).text
    assert "price unconfirmed" not in page and "store rule" in page and "1299.99" in page
    assert runtime.rules.get("storeone.pt").price_selector
    client.post(path + "/confirm", data=form_data(client, selected=["0"]))
    with runtime.db.session() as session:
        listing = session.scalar(select(Listing))
        assert (listing.current_price, listing.extraction_method, listing.availability) == (
            Decimal("1299.99"),
            "rule",
            "in_stock",
        )
    assert (
        client.post(
            path + "/teach/0", data=form_data(client, price="1299,99", availability="in_stock")
        ).status_code
        == 422
    )


def test_confirm_price_from_product_page(site):
    client, runtime, _, _ = site
    url = "https://www.storeone.pt/produto/tcl-85c7k"
    GENERIC_PAGES[url] = OLD_PRICE_FIRST_PAGE
    path, _ = discover(client, name="TCL 85C7K", urls=[url], retailers=[])
    product_path = client.post(
        path + "/confirm", data=form_data(client, selected=["0"]), follow_redirects=False
    ).headers["location"]
    with runtime.db.session() as session:
        listing_id = session.scalar(select(Listing)).id
    assert "Confirm price" in client.get(product_path).text
    page = client.get(f"/listings/{listing_id}/confirm-price").text
    assert "1.499,00" in page and "1.299,99" in page
    response = client.post(
        f"/listings/{listing_id}/confirm-price",
        data=form_data(client, price="1299.99", availability="in_stock"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    with runtime.db.session() as session:
        listing = session.get(Listing, listing_id)
        assert (listing.current_price, listing.extraction_method, listing.availability) == (
            Decimal("1299.99"),
            "rule",
            "in_stock",
        )
        assert session.scalar(select(func.count()).select_from(PriceHistory)) == 2
        assert listing.next_check_at > now()
    assert '<span class="badge low">price unconfirmed</span>' not in client.get(product_path).text
    assert client.get("/listings/999/confirm-price").status_code == 404


def test_settings_lists_and_forgets_store_rules(site):
    client, runtime, _, _ = site
    runtime.rules.save("storeone.pt", PriceRule(price_selector="span.price-current"))
    page = client.get("/settings").text
    assert "storeone.pt" in page and "span.price-current" in page
    response = client.post(
        "/settings/rules/storeone.pt/forget", data=form_data(client), follow_redirects=False
    )
    assert response.status_code == 303
    assert runtime.rules.get("storeone.pt") is None
    assert "No store rules yet" in client.get("/settings").text


def test_settings_describes_a_cart_button_stock_rule(site):
    client, runtime, _, _ = site
    rule = PriceRule(
        price_selector="span.amount",
        availability_selector="button.add",
        availability_mode="presence",
    )
    runtime.rules.save("storeone.pt", rule)
    assert "in stock while an enabled cart button is shown" in client.get("/settings").text


def test_forgetting_a_store_without_a_rule_returns_to_settings(site):
    client, _, _, _ = site
    client.get("/settings")
    response = client.post(
        "/settings/rules/unknown.pt/forget", data=form_data(client), follow_redirects=False
    )
    assert (response.status_code, response.headers["location"]) == (303, "/settings")


def test_forgetting_a_store_rule_requires_the_form_token(site):
    client, runtime, _, _ = site
    runtime.rules.save("storeone.pt", PriceRule(price_selector="span.price-current"))
    client.get("/settings")
    response = client.post("/settings/rules/storeone.pt/forget", data={"csrf": "wrong"})
    assert response.status_code == 403
    assert runtime.rules.get("storeone.pt") is not None


def test_add_form_requires_links_or_stores(site):
    client, _, _, _ = site
    response = client.post("/discoveries", data=form_data(client, name="TCL 85C7K"))
    assert (
        response.status_code == 422
        and "Add at least one link, or pick stores to search" in response.text
    )
    page = client.get("/products/new").text
    assert "Also search known stores" in page and 'value="worten" checked' not in page
    # Every saved link alerts; there is no condition filter to fill in.
    assert "Alert conditions" not in page and 'name="conditions"' not in page
    assert 'name="conditions"' not in client.get("/settings").text


def test_manual_conflicting_link_is_saved_with_warning(site):
    client, runtime, _, _ = site
    GENERIC_PAGES["https://www.storeone.pt/p/other"] = STORE_PAGE.replace("85C7K", "75C8K")
    path, page = discover(
        client, name="TCL 85C7K", urls=["https://www.storeone.pt/p/other"], retailers=[]
    )
    assert "saved as pasted" in page.text and 'value="0" checked' in page.text
    response = client.post(
        path + "/confirm", data=form_data(client, selected=["0"]), follow_redirects=False
    )
    assert response.status_code == 303
    with runtime.db.session() as session:
        assert session.scalar(select(func.count()).select_from(Listing)) == 1


PASTED_URL = "https://www.storeone.pt/p/other"


def save_pasted_conflicting_link(client, runtime):
    GENERIC_PAGES[PASTED_URL] = STORE_PAGE.replace("85C7K", "75C8K")
    path, _ = discover(client, name="TCL 85C7K", urls=[PASTED_URL], retailers=[])
    client.post(path + "/confirm", data=form_data(client, selected=["0"]))
    with runtime.db.session() as session:
        session.scalar(select(Listing)).next_check_at = now() - timedelta(minutes=1)


async def test_pasted_conflicting_link_is_checked_against_its_own_model(site):
    client, runtime, _, _ = site
    save_pasted_conflicting_link(client, runtime)
    GENERIC_PAGES[PASTED_URL] = STORE_PAGE.replace("85C7K", "75C8K").replace("1.299,99", "1.199,99")
    await runtime.monitor.run()
    with runtime.db.session() as session:
        listing = session.scalar(select(Listing))
        assert (listing.last_error, listing.current_price) == (None, Decimal("1199.99"))
        assert session.scalar(select(func.count()).select_from(PriceHistory)) == 2


async def test_pasted_conflicting_link_still_flags_a_further_model_change(site):
    client, runtime, _, _ = site
    save_pasted_conflicting_link(client, runtime)
    GENERIC_PAGES[PASTED_URL] = STORE_PAGE.replace("85C7K", "65C6K")
    await runtime.monitor.run()
    with runtime.db.session() as session:
        listing = session.scalar(select(Listing))
        assert listing.last_error == "Product identity changed on retailer page; review required"
        assert listing.current_price == Decimal("1299.99")
        assert session.scalar(select(func.count()).select_from(PriceHistory)) == 1


async def test_discovered_listing_flags_a_model_change(site):
    client, runtime, _, _ = site
    path, _ = discover(client, retailers=["worten"])
    client.post(path + "/confirm", data=form_data(client, selected=["0"]))
    offer = {
        "@type": "Offer",
        "price": "999.00",
        "priceCurrency": "EUR",
        "availability": "https://schema.org/InStock",
        "itemCondition": "https://schema.org/NewCondition",
        "seller": {"name": "worten"},
    }
    other = {
        "@type": "Product",
        "name": "TCL 75C8K",
        "brand": {"name": "TCL"},
        "model": "75C8K",
        "offers": offer,
    }
    GENERIC_PAGES[URLS["worten"]] = (
        f'<h1>TCL 75C8K</h1><script type="application/ld+json">{json.dumps(other)}</script>'
    )
    with runtime.db.session() as session:
        listing = session.scalar(select(Listing))
        assert listing.sources == ["discovered"]
        listing.next_check_at = now() - timedelta(minutes=1)
    await runtime.monitor.run()
    with runtime.db.session() as session:
        listing = session.scalar(select(Listing))
        assert listing.last_error == "Product identity changed on retailer page; review required"
        assert session.scalar(select(func.count()).select_from(PriceHistory)) == 1


def test_quick_link_from_product_page(site):
    client, runtime, _, _ = site
    path, _ = discover(client, urls=[URLS["worten"]], retailers=[])
    product_path = client.post(
        path + "/confirm", data=form_data(client, selected=["0"]), follow_redirects=False
    ).headers["location"]
    assert 'placeholder="Paste a store link"' in client.get(product_path).text
    GENERIC_PAGES["https://www.storeone.pt/produto/tcl-85c7k"] = STORE_PAGE
    path, _ = discover(
        client,
        product_id=product_path.rsplit("/", 1)[1],
        urls=["https://www.storeone.pt/produto/tcl-85c7k"],
        retailers=[],
    )
    client.post(path + "/confirm", data=form_data(client, selected=["0"]))
    with runtime.db.session() as session:
        assert {x.retailer for x in session.scalars(select(Listing))} == {"worten", "storeone.pt"}


def test_discovered_conflicting_listing_stays_blocked(site, monkeypatch):
    client, runtime, _, _ = site

    async def unfiltered(identity):
        return [URLS["worten"]]

    monkeypatch.setattr(runtime.registry.adapters["worten"], "search_product", unfiltered)
    path, page = discover(client, name="TCL 75C8K", retailers=["worten"])
    assert "CONFLICT" in page.text and "saved as pasted" not in page.text
    assert re.search(r'value="0"\s+disabled>', page.text)
    response = client.post(path + "/confirm", data=form_data(client, selected=["0"]))
    assert response.status_code == 422
    assert "Conflicting models cannot be merged into this product" in response.text
    with runtime.db.session() as session:
        assert session.scalar(select(func.count()).select_from(Listing)) == 0


MODEL_CHANGED = "Product identity changed on retailer page; review required"
CHANGED_URL = "https://www.storeone.pt/p/tv"


async def flag_model_change(client, runtime) -> int:
    GENERIC_PAGES[CHANGED_URL] = STORE_PAGE
    path, _ = discover(client, name="TCL 85C7K", urls=[CHANGED_URL], retailers=[])
    client.post(path + "/confirm", data=form_data(client, selected=["0"]))
    # The store reuses the URL for another model.
    GENERIC_PAGES[CHANGED_URL] = STORE_PAGE.replace("85C7K", "65C6K").replace("1.299,99", "499,00")
    with runtime.db.session() as session:
        listing = session.scalar(select(Listing))
        listing.next_check_at = now() - timedelta(minutes=1)
        listing_id = listing.id
    await runtime.monitor.run()
    with runtime.db.session() as session:
        assert session.get(Listing, listing_id).last_error == MODEL_CHANGED
    return listing_id


async def test_confirming_a_price_on_a_changed_model_is_refused(site):
    client, runtime, _, _ = site
    listing_id = await flag_model_change(client, runtime)
    response = client.post(
        f"/listings/{listing_id}/confirm-price",
        data=form_data(client, price="499", availability="in_stock"),
        follow_redirects=False,
    )
    assert response.status_code == 422 and MODEL_CHANGED in response.text
    with runtime.db.session() as session:
        listing = session.get(Listing, listing_id)
        assert (listing.last_error, listing.current_price, listing.title) == (
            MODEL_CHANGED,
            Decimal("1299.99"),
            'TV TCL 85C7K MiniLED 85"',
        )
        assert session.scalar(select(func.count()).select_from(PriceHistory)) == 1
        assert list(session.scalars(select(Alert.event_type))) == ["new_listing"]
        listing.next_check_at = now() - timedelta(minutes=1)
    assert runtime.rules.get("storeone.pt") is None
    await runtime.monitor.run()
    with runtime.db.session() as session:
        assert session.get(Listing, listing_id).last_error == MODEL_CHANGED


async def test_confirm_page_shows_a_model_change_instead_of_the_form(site):
    client, runtime, _, _ = site
    listing_id = await flag_model_change(client, runtime)
    page = client.get(f"/listings/{listing_id}/confirm-price").text
    assert MODEL_CHANGED in page and 'name="price"' not in page
    assert "TV TCL 65C6K MiniLED" in page and "85C7K MiniLED" not in page


def test_confirm_page_shows_the_title_the_page_has_now(site):
    client, runtime, _, _ = site
    GENERIC_PAGES[CHANGED_URL] = STORE_PAGE
    path, _ = discover(client, name="TCL 85C7K", urls=[CHANGED_URL], retailers=[])
    client.post(path + "/confirm", data=form_data(client, selected=["0"]))
    GENERIC_PAGES[CHANGED_URL] = STORE_PAGE.replace("MiniLED", "QD-MiniLED")
    with runtime.db.session() as session:
        listing_id = session.scalar(select(Listing)).id
    page = client.get(f"/listings/{listing_id}/confirm-price").text
    assert "TV TCL 85C7K QD-MiniLED" in page and 'name="price"' in page


@pytest.mark.parametrize(
    ("owner", "field", "value"),
    [("listing", "enabled", False), ("product", "enabled", False), ("product", "archived", True)],
)
def test_price_cannot_be_confirmed_while_a_listing_is_not_monitored(site, owner, field, value):
    client, runtime, _, _ = site
    GENERIC_PAGES[CHANGED_URL] = OLD_PRICE_FIRST_PAGE
    path, _ = discover(client, name="TCL 85C7K", urls=[CHANGED_URL], retailers=[])
    product_path = client.post(
        path + "/confirm", data=form_data(client, selected=["0"]), follow_redirects=False
    ).headers["location"]
    with runtime.db.session() as session:
        listing = session.scalar(select(Listing))
        setattr(listing if owner == "listing" else listing.product, field, value)
        confirm_path = f"/listings/{listing.id}/confirm-price"
    assert confirm_path not in client.get(product_path).text
    assert client.get(confirm_path).status_code == 422
    response = client.post(
        confirm_path, data=form_data(client, price="1299,99", availability="in_stock")
    )
    assert response.status_code == 422 and "not monitored" in response.text
    with runtime.db.session() as session:
        assert session.scalar(select(func.count()).select_from(PriceHistory)) == 1
        assert list(session.scalars(select(Alert.event_type))) == ["new_listing"]
    assert runtime.rules.get("storeone.pt") is None


SECOND_PRICE_RULE = PriceRule(price_selector="span.price:nth-of-type(2)")


def test_a_store_rule_reading_still_offers_the_page_prices_to_confirm(site):
    client, runtime, _, _ = site
    runtime.rules.save("storeone.pt", SECOND_PRICE_RULE)
    GENERIC_PAGES[CHANGED_URL] = OLD_PRICE_FIRST_PAGE
    path, page = discover(client, name="TCL 85C7K", urls=[CHANGED_URL], retailers=[])
    assert "store rule" in page.text and "1.499,00 €" in page.text
    client.post(path + "/confirm", data=form_data(client, selected=["0"]))
    with runtime.db.session() as session:
        listing = session.scalar(select(Listing))
        assert listing.extraction_method == "rule"
    assert "1.499,00 €" in client.get(f"/listings/{listing.id}/confirm-price").text


async def test_scheduled_check_does_not_list_the_page_prices(site, monkeypatch):
    client, runtime, _, _ = site
    runtime.rules.save("storeone.pt", SECOND_PRICE_RULE)
    GENERIC_PAGES[CHANGED_URL] = OLD_PRICE_FIRST_PAGE
    path, _ = discover(client, name="TCL 85C7K", urls=[CHANGED_URL], retailers=[])
    client.post(path + "/confirm", data=form_data(client, selected=["0"]))
    calls = []
    monkeypatch.setattr(generic, "price_candidates", lambda soup: calls.append(soup) or [])
    with runtime.db.session() as session:
        session.scalar(select(Listing)).next_check_at = now() - timedelta(minutes=1)
    await runtime.monitor.run()
    with runtime.db.session() as session:
        assert session.scalar(select(func.count()).select_from(PriceHistory)) == 2
    assert calls == []


def test_link_the_url_parser_refuses_is_rejected_at_the_form(site):
    client, _, _, requests = site
    response = client.post(
        "/discoveries",
        data=form_data(client, name="TV", urls=["https://www.[::1]/p"], retailers=[]),
    )
    assert response.status_code == 422
    assert "Product links must be full https:// URLs" in response.text
    assert requests == []


STORE_URL = "https://www.storeone.pt/produto/tcl-85c7k"


def saved_product(client):
    path, _ = discover(client, urls=[URLS["worten"]], retailers=[])
    response = client.post(
        path + "/confirm", data=form_data(client, selected=["0"]), follow_redirects=False
    )
    return response.headers["location"]


def test_add_link_to_an_existing_product_saves_it_directly(site):
    client, runtime, _, _ = site
    product_path = saved_product(client)
    GENERIC_PAGES[STORE_URL] = STORE_PAGE
    with runtime.db.session() as session:
        drafts = session.scalar(select(func.count()).select_from(DiscoveryDraft))
    response = client.post(
        product_path + "/links",
        data=form_data(client, url=STORE_URL + "?utm_source=mail"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    with runtime.db.session() as session:
        # No discovery step: the link is fetched and saved in the request itself.
        assert session.scalar(select(func.count()).select_from(DiscoveryDraft)) == drafts
        listing = session.scalar(select(Listing).where(Listing.retailer == "storeone.pt"))
        assert (listing.url, listing.current_price, listing.sources) == (
            STORE_URL,
            Decimal("1299.99"),
            ["manual"],
        )
        assert response.headers["location"] == f"{product_path}?added={listing.id}"
    page = client.get(response.headers["location"]).text
    # Says exactly what was read, so a wrong reading can be removed straight away.
    assert "Added storeone.pt: €1,299.99 · in stock · new" in page
    assert f'action="/listings/{listing.id}/delete"' in page.split('class="notice"')[1]
    assert 'action="' + product_path + '/links"' in page


def test_adding_a_link_twice_keeps_one_listing(site):
    client, runtime, _, _ = site
    product_path = saved_product(client)
    GENERIC_PAGES[STORE_URL] = STORE_PAGE
    client.post(product_path + "/links", data=form_data(client, url=STORE_URL))
    response = client.post(
        product_path + "/links",
        data=form_data(client, url=STORE_URL + "#reviews"),
        follow_redirects=False,
    )
    assert response.headers["location"] == product_path + "?added=0"
    assert "already on this product" in client.get(product_path + "?added=0").text
    with runtime.db.session() as session:
        assert session.scalar(select(func.count()).select_from(Listing)) == 2


@pytest.mark.parametrize(
    "url,message",
    [
        ("https://www.storeone.pt/missing", "HTTP 404"),
        ("https://192.168.1.10/produto/x", "Only public https:// store links can be monitored"),
        ("http://www.storeone.pt/p", "Product links must be full https:// URLs"),
    ],
)
def test_a_link_that_cannot_be_added_says_why(site, url, message):
    client, runtime, _, _ = site
    product_path = saved_product(client)
    response = client.post(product_path + "/links", data=form_data(client, url=url))
    assert response.status_code == 422 and message in response.text
    with runtime.db.session() as session:
        assert session.scalar(select(func.count()).select_from(Listing)) == 1


def test_archived_product_takes_no_links(site):
    client, _, _, _ = site
    product_path = saved_product(client)
    client.post(product_path + "/archive", data=form_data(client))
    GENERIC_PAGES[STORE_URL] = STORE_PAGE
    response = client.post(product_path + "/links", data=form_data(client, url=STORE_URL))
    assert response.status_code == 422 and "Restore" in response.text
    assert 'placeholder="Paste a store link"' not in client.get(product_path).text


def test_store_names_and_prices_link_to_the_listing(site):
    client, runtime, _, _ = site
    product_path = saved_product(client)
    worten = f'href="{URLS["worten"]}"'
    # Dashboard card: the best price and its store.
    card = client.get("/").text.split('class="product-card"')[1]
    assert card.count(worten) >= 2
    # Product page: best price figure, plus the price cell in the listings table.
    product = client.get(product_path).text
    stats = product.split('class="stats"')[1].split("</div></div>")[0]
    assert worten in stats
    table = product.split("<tbody>")[1]
    assert table.count(worten) >= 2
    # Activity: each event about a listing links to it.
    assert worten in client.get("/alerts").text
    # Review screen: the price cell links to the listing too.
    _, page = discover(client, urls=[URLS["worten"]], retailers=[])
    row = page.text.split("<tbody>")[1].split("</tr>")[0]
    assert row.count(worten) >= 2


async def test_discovery_that_runs_too_long_is_stopped(site, monkeypatch):
    _, runtime, _, _ = site
    import asyncio

    async def stuck(url, **kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(runtime.registry.adapters["worten"], "fetch_listing", stuck)
    monkeypatch.setattr(runtime.discovery, "timeout", 0.05)
    draft = runtime.discovery.create(payload(urls=[URLS["worten"]]) | {"retailers": []})
    await runtime.discovery.run(draft)
    with runtime.db.session() as session:
        row = session.get(DiscoveryDraft, draft)
        assert row.status == "failed"
        assert "took longer than" in row.results["errors"][0]


def test_queued_discovery_says_it_is_waiting(site):
    client, runtime, _, _ = site
    draft = runtime.discovery.create(payload())
    assert "Waiting for another discovery to finish" in client.get(f"/discoveries/{draft}").text


def test_the_same_link_can_be_watched_by_several_products(site):
    # One link, two watches (e.g. different targets): each product keeps its own listing.
    client, runtime, _, _ = site
    first = saved_product(client)
    path, _ = discover(
        client, urls=[URLS["worten"]], retailers=[], target_price="700", insane_deal_price="600"
    )
    second = client.post(
        path + "/confirm", data=form_data(client, selected=["0"]), follow_redirects=False
    ).headers["location"]
    assert second != first
    GENERIC_PAGES[STORE_URL] = STORE_PAGE
    for product_path in (first, second):
        response = client.post(
            product_path + "/links", data=form_data(client, url=STORE_URL), follow_redirects=False
        )
        assert not response.headers["location"].endswith("?added=0")
    with runtime.db.session() as session:
        assert session.scalar(select(func.count()).select_from(Product)) == 2
        rows = session.scalars(select(Listing).order_by(Listing.product_id)).all()
        assert [(r.product_id, r.retailer) for r in rows] == [
            (1, "worten"),
            (1, "storeone.pt"),
            (2, "worten"),
            (2, "storeone.pt"),
        ]
        assert session.get(Product, 2).target_price == Decimal("700")


def test_removing_a_listing_deletes_it_and_its_history(site):
    client, runtime, _, _ = site
    product_path = saved_product(client)
    GENERIC_PAGES[STORE_URL] = STORE_PAGE
    location = client.post(
        product_path + "/links", data=form_data(client, url=STORE_URL), follow_redirects=False
    ).headers["location"]
    listing_id = int(location.rsplit("=", 1)[1])
    page = client.get(product_path).text
    assert 'data-confirm="Stop tracking this link?' in page
    response = client.post(
        f"/listings/{listing_id}/delete", data=form_data(client), follow_redirects=False
    )
    assert response.status_code == 303 and response.headers["location"] == product_path
    with runtime.db.session() as session:
        assert session.get(Listing, listing_id) is None
        assert (
            session.scalar(
                select(func.count())
                .select_from(PriceHistory)
                .where(PriceHistory.listing_id == listing_id)
            )
            == 0
        )
        # The event log keeps its entries; they just no longer point at the listing.
        assert session.scalar(select(func.count()).select_from(Alert)) >= 2
        assert all(a.listing_id != listing_id for a in session.scalars(select(Alert)))
    table = client.get(product_path).text.split("<tbody>")[1].split("</tbody>")[0]
    assert "storeone.pt" not in table
    assert client.post(f"/listings/{listing_id}/delete", data=form_data(client)).status_code == 404


def test_promo_code_price_is_shown_with_its_code(site):
    client, runtime, _, _ = site
    product_path = saved_product(client)
    with runtime.db.session() as session:
        listing = session.scalar(select(Listing))
        listing.promo_price, listing.promo_code = Decimal("999.00"), "TV20"
    card = client.get("/").text.split('class="product-card"')[1]
    assert "€999.00" in card and "with code TV20" in card
    product = client.get(product_path).text
    assert "with code TV20" in product.split('class="stats"')[1].split("</div></div>")[0]
    table = product.split("<tbody>")[1]
    assert "€1,199.00" in table and "€999.00 with code TV20" in table


async def test_a_link_the_store_refuses_is_kept_and_read_later(site):
    client, runtime, prices, _ = site
    prices["darty"] = "rate_limited"
    started = time.monotonic()
    path, page = discover(client, urls=[URLS["darty"]], retailers=[])
    assert time.monotonic() - started < 5  # no 60-second Retry-After waits
    assert "is kept and read automatically" in page.text
    assert 'value="0" checked' in page.text and "not read yet" in page.text
    product_path = client.post(
        path + "/confirm", data=form_data(client, selected=["0"]), follow_redirects=False
    ).headers["location"]
    with runtime.db.session() as session:
        listing = session.scalar(select(Listing))
        assert (listing.retailer, listing.current_price, listing.extraction_method) == (
            "darty",
            None,
            "unread",
        )
        assert "Not read yet" in listing.last_error
        assert listing.next_check_at <= now() + timedelta(minutes=16)
        assert session.scalar(select(func.count()).select_from(PriceHistory)) == 0
    assert "Not read yet" in client.get(product_path).text
    # The store allows it again: the next check reads it like any other listing.
    prices["darty"] = "1199.00"
    runtime.fetcher.retry_now()
    with runtime.db.session() as session:
        session.scalar(select(Listing)).next_check_at = now() - timedelta(minutes=1)
    await runtime.monitor.run()
    with runtime.db.session() as session:
        listing = session.scalar(select(Listing))
        assert (listing.current_price, listing.extraction_method, listing.last_error) == (
            Decimal("1199.00"),
            "structured",
            None,
        )
        assert (listing.seller, listing.condition) == ("darty", "new")
        assert session.scalar(select(func.count()).select_from(PriceHistory)) == 1


def test_add_link_keeps_a_link_the_store_refuses(site):
    client, runtime, prices, _ = site
    product_path = saved_product(client)
    prices["darty"] = "rate_limited"
    response = client.post(
        product_path + "/links", data=form_data(client, url=URLS["darty"]), follow_redirects=False
    )
    page = client.get(response.headers["location"]).text
    assert "Added Darty Portugal: not read yet" in page
    with runtime.db.session() as session:
        listing = session.scalar(select(Listing).where(Listing.retailer == "darty"))
        assert listing.extraction_method == "unread"


def test_kuantokusta_link_shows_where_the_lowest_price_is(site):
    client, runtime, _, _ = site
    from tests.conftest import FIXTURES

    async def browse(url, retailer, hosts):
        return (FIXTURES / "kuantokusta_85c7l_live.html").read_text()

    runtime.fetcher.browse = browse
    product_path = saved_product(client)
    location = client.post(
        product_path + "/links",
        data=form_data(
            client,
            url="https://www.kuantokusta.pt/p/12121920/tcl-85-85c7l-sqd-miniled-smart-google-tv-4k",
        ),
        follow_redirects=False,
    ).headers["location"]
    page = client.get(location).text
    assert "Added KuantoKusta: €1,748.18" in page
    assert "Hipermercado · free shipping" in page.split("<tbody>")[1]
    with runtime.db.session() as session:
        listing = session.scalar(select(Listing).where(Listing.retailer == "kuantokusta"))
        assert (listing.current_price, listing.offered_by) == (
            Decimal("1748.18"),
            "Hipermercado · free shipping",
        )
