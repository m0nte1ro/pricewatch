import json
import re
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.marketplaces.base import Marketplace
from app.retailers.parsing import ScrapeError, money
from app.schemas.domain import MarketItem

SEARCH_API = "https://api.wallapop.com/api/v3/search"
API_HOSTS = ("api.wallapop.com",)
# The web client names itself so; the search API answers it with JSON.
API_HEADERS = {"Accept": "application/json", "X-DeviceOS": "0"}
# Where Wallapop starts a search for a visitor without a location.
MADRID = {"latitude": "40.41956", "longitude": "-3.69196"}
# Order, paging and tracking are pricewatch's to set, not the pasted link's.
OWN = {"order_by", "source", "search_id", "filters_source", "next_page", "step"}
CATEGORIES = ("category_id", "category_ids", "subcategory_ids", "object_type_ids")
CHANGED = "Wallapop search response changed; provider update required"


class WallapopMarketplace(Marketplace):
    name = "wallapop"
    label = "Wallapop"
    # Country sites first; links from the bare domain are read on the Spanish one.
    hosts = (
        "es.wallapop.com",
        "pt.wallapop.com",
        "it.wallapop.com",
        "www.wallapop.com",
        "wallapop.com",
    )
    example = "https://es.wallapop.com/search?keywords=ps5&max_sale_price=300"

    def normalize_url(self, url: str) -> str:
        parsed = urlsplit(url.strip())
        self.fetcher.validate_url(url.strip(), self.hosts)
        if re.sub(r"/{2,}", "/", parsed.path).rstrip("/") not in ("/search", "/app/search"):
            raise ScrapeError(
                "Use a Wallapop search link: search on Wallapop, then copy the address"
            )
        query = {
            key: value
            for key, value in parse_qsl(parsed.query)
            if value and key not in OWN and not key.startswith("utm_")
        }
        if not query.get("keywords", "").strip() and not any(key in query for key in CATEGORIES):
            raise ScrapeError("Search Wallapop for some keywords or a category first")
        host = parsed.hostname if parsed.hostname in self.hosts[:3] else self.hosts[0]
        return urlunsplit(("https", host, "/search", urlencode(sorted(query.items())), ""))

    def describe(self, url: str) -> str:
        query = dict(parse_qsl(urlsplit(url).query))
        low, high = (
            f"€{value.normalize():f}" if (value := money(query.get(key))) else ""
            for key in ("min_sale_price", "max_sale_price")
        )
        prices = (
            f"{low}–{high}"
            if low and high
            else f"from {low}"
            if low
            else f"up to {high}"
            if high
            else ""
        )
        return " · ".join(part for part in (query.get("keywords") or "a category", prices) if part)

    async def fetch_listings(self, url: str) -> list[MarketItem]:
        url = self.normalize_url(url)
        query = dict(parse_qsl(urlsplit(url).query))
        if not ("latitude" in query and "longitude" in query):
            query.update(MADRID)
        # One page, newest first: what arrived since the last read is at the top.
        query.update(order_by="newest", source="search_box")
        raw = await self.fetcher.get(
            f"{SEARCH_API}?{urlencode(query)}", self.name, API_HOSTS, headers=API_HEADERS
        )
        try:
            items = json.loads(raw)["data"]["section"]["payload"]["items"]
            return [self.listing(item, urlsplit(url).hostname) for item in items]
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ScrapeError(CHANGED) from None

    def listing(self, item: dict, host: str) -> MarketItem:
        price, created = item.get("price") or {}, item.get("created_at")
        images = item.get("images") or [{}]
        return MarketItem(
            listing_id=str(item["id"]),
            title=" ".join(str(item["title"]).split())[:500],
            url=f"https://{host}/item/{item['web_slug']}",
            price=money(price.get("amount")) if price.get("currency", "EUR") == "EUR" else None,
            image_url=(images[0].get("urls") or {}).get("small"),
            place=(item.get("location") or {}).get("city"),
            posted_at=datetime.fromtimestamp(created / 1000, UTC).replace(tzinfo=None)
            if isinstance(created, int | float)
            else None,
            reserved=(item.get("reserved") or {}).get("flag") is True,
            ships=(item.get("shipping") or {}).get("user_allows_shipping") is True,
        )
