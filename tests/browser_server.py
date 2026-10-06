"""Local browser validation server; recorded retailer data, never used by production."""

import json
import os
from pathlib import Path

import httpx

from app.config import Config
from app.database import Base, Database
from app.main import create_app
from tests.conftest import URLS

root = Path(os.environ.get("PRICEWATCH_SMOKE_DIR", "/tmp/pricewatch-browser-smoke"))
root.mkdir(exist_ok=True)
db = Database(f"sqlite:///{root / 'smoke.db'}")
Base.metadata.create_all(db.engine)
app = create_app(Config(data_dir=root, scheduler_enabled=True), database=db)
runtime = app.state.runtime
runtime.fetcher.min_delay = 0


def respond(request):
    name = next(name for name, url in URLS.items() if request.url.host in url)
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
    if (
        any(word in request.url.path.lower() for word in ("search", "pesquisa"))
        or request.url.path == "/s"
    ):
        return httpx.Response(200, text=f'<a href="{URLS[name]}">TCL 85C7K</a>')
    price_file = root / "price.txt"
    price = price_file.read_text().strip() if price_file.exists() else "1199"
    product = {
        "@type": "Product",
        "name": "TCL 85C7K",
        "brand": {"name": "TCL"},
        "model": "85C7K",
        "offers": {
            "@type": "Offer",
            "price": price,
            "availability": "https://schema.org/InStock",
            "priceCurrency": "EUR",
            "itemCondition": "https://schema.org/NewCondition",
            "seller": {"name": name},
        },
    }
    return httpx.Response(
        200,
        text=f'<h1>TCL 85C7K</h1><script type="application/ld+json">{json.dumps(product)}</script>',
    )


runtime.fetcher.transport = httpx.MockTransport(respond)
