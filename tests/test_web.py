import time

from sqlalchemy import func, select

from app.models import DiscoveryDraft, Listing, Product
from tests.conftest import URLS


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
