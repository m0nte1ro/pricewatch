import time
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models import DiscoveryDraft, Listing, PriceHistory, Product, RetailerState
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
        "conditions": ["new"],
        "retailers": list(URLS),
    }


def form_data(client, **kwargs):
    return {"csrf": client.cookies.get("pricewatch_csrf"), **kwargs}


def discover(client, **kwargs):
    inputs = {
        "name": "TCL 85C7K",
        "retailers": list(URLS),
        "conditions": ["new"],
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
            conditions=["new"],
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
        conditions=["new"],
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

    async def broken(url):
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
    _, page = discover(client, urls=[URLS["worten"]])
    assert "No listings found" in page.text
    assert '<details class="panel warnings" open>' in page.text
    assert "Manual URL" in page.text


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
