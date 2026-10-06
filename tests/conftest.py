import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.database import Base, Database
from app.main import create_app
from app.schemas.domain import Candidate, Identity, Match, Snapshot

FIXTURES = Path(__file__).parent / "fixtures"
WORTEN_URL = "https://www.worten.pt/produtos/tv-tcl-85c7k-8376601"
URLS = {
    "worten": WORTEN_URL,
    "fnac": "https://www.fnac.pt/TV-TCL-85C7K/a12714588",
    "darty": "https://www.darty.pt/products/tv-tcl-85c7k",
    "radiopopular": "https://www.radiopopular.pt/produto/tv-tcl-85c7k",
    "amazon_es": "https://www.amazon.es/dp/B012345678",
}


@pytest.fixture
def db():
    database = Database("sqlite://")
    Base.metadata.create_all(database.engine)
    yield database
    database.engine.dispose()


@pytest.fixture
def snapshot():
    return Snapshot(
        retailer="worten",
        url=WORTEN_URL,
        retailer_product_id="8376601",
        identity=Identity(name="TCL 85C7K", brand="TCL", model="85C7K", size="85", category="tv"),
        title='TV TCL 85C7K 85"',
        price=Decimal("1199"),
        availability="in_stock",
        condition="new",
        seller="Worten",
    )


@pytest.fixture
def candidate(snapshot):
    return Candidate(
        listing=snapshot, sources=["manual"], match=Match(level="EXACT", score=1, reason="model")
    )


@pytest.fixture
def site(db, tmp_path):
    app = create_app(Config(data_dir=tmp_path, scheduler_enabled=False), database=db)
    runtime = app.state.runtime
    runtime.fetcher.min_delay = 0
    runtime.fetcher.intervals.clear()
    prices = {name: "1199.00" for name in URLS}
    requests = []

    def respond(request):
        requests.append(str(request.url))
        host = request.url.host
        retailer = next(name for name, url in URLS.items() if host in url)
        if request.url.path == "/worten-api/search-products":
            return httpx.Response(
                200,
                json={
                    "searchResponse": {},
                    "detailsResponse": {
                        "productsCanonicalsData": {"web_items": [{"url": URLS["worten"]}]}
                    },
                },
            )
        if request.url.path == "/search/suggest.json":
            return httpx.Response(
                200,
                json={
                    "resources": {
                        "results": {"products": [{"title": "TCL 85C7K", "url": URLS["darty"]}]}
                    }
                },
            )
        is_search = (
            any(x in request.url.path.lower() for x in ("search", "pesquisa"))
            or request.url.path == "/s"
        )
        if is_search:
            return httpx.Response(200, text=f'<a href="{URLS[retailer]}">TCL 85C7K</a>')
        if prices[retailer] == "blocked":
            return httpx.Response(403, text="Forbidden")
        data = {
            "@type": "Product",
            "name": "TCL 85C7K",
            "brand": {"name": "TCL"},
            "model": "85C7K",
            "offers": {
                "@type": "Offer",
                "price": prices[retailer],
                "priceCurrency": "EUR",
                "availability": "https://schema.org/InStock",
                "itemCondition": "https://schema.org/NewCondition",
                "seller": {"name": retailer},
            },
        }
        return httpx.Response(
            200,
            text=f'<h1>TCL 85C7K</h1><script type="application/ld+json">{json.dumps(data)}</script>',
        )

    runtime.fetcher.transport = httpx.MockTransport(respond)
    with TestClient(app) as client:
        client.get("/")
        yield client, runtime, prices, requests
