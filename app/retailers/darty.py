import json
from urllib.parse import urlencode, urljoin

from app.retailers.base import RetailerAdapter
from app.retailers.parsing import ScrapeError
from app.schemas.domain import Condition, Identity
from app.services.matching import normalize


class DartyAdapter(RetailerAdapter):
    name = "darty"
    label = "Darty Portugal"
    hosts = ("www.darty.pt", "darty.pt")
    search_path = "/search?q={query}"
    product_pattern = r"/(?:products|produtos|produto)/([^/?]+)"
    status_note = "Partial Portugal adapter: live product JSON-LD and public Shopify product search validated. Sells its own new stock, so offers without outlet/refurbished markers count as new; seller is often unknown."
    default_condition = Condition.NEW
    # Darty's Shopify storefront answers 429 to requests 2 s apart.
    request_interval = 5

    def api_search_links(self, payload: dict, identity: Identity) -> list[str]:
        items = payload.get("resources", {}).get("results", {}).get("products", [])
        result = []
        for item in items:
            if identity.model and normalize(identity.model) not in normalize(item.get("title", "")):
                continue
            url = self.normalize_url(urljoin("https://www.darty.pt", item.get("url", "")))
            if self.is_product_url(url):
                result.append(url)
        return list(dict.fromkeys(result))[:4]

    async def search_term(self, term: str, identity: Identity) -> list[str]:
        query = urlencode({"q": term, "resources[type]": "product", "resources[limit]": 10})
        raw = await self.fetcher.get(
            f"https://www.darty.pt/search/suggest.json?{query}", self.name, self.hosts
        )
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or "resources" not in payload:
                raise ValueError
            return self.api_search_links(payload, identity)
        except (ValueError, TypeError):
            raise ScrapeError("Darty search response changed; adapter update required") from None
