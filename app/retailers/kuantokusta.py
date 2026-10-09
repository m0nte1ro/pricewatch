import json
from decimal import Decimal
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup

from app.retailers.base import RetailerAdapter, with_page
from app.retailers.parsing import ScrapeError
from app.schemas.domain import Condition, Identity, Snapshot


class KuantoKustaAdapter(RetailerAdapter):
    """A price-comparison page: one listing that follows the cheapest offer, shipping included."""

    name = "kuantokusta"
    label = "KuantoKusta"
    hosts = ("www.kuantokusta.pt", "kuantokusta.pt")
    search_path = ""
    searchable = False
    product_pattern = r"/p/(\d+)"
    status_note = "Partial: reads every store's offer on a KuantoKusta product page and keeps the lowest price including shipping. Plain requests are refused, so pages are read with the browser. Paste product links; there is no search."
    default_condition = Condition.NEW

    def normalize_url(self, url: str) -> str:
        # The page is the product; queryId and other parameters only track the search.
        parts = urlsplit(super().normalize_url(url))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

    async def search_product(self, identity: Identity) -> list[str]:
        return []

    async def fetch_listing(self, url: str, *, alternatives: bool = False) -> list[Snapshot]:
        url = self.normalize_url(url)
        if not self.is_product_url(url):
            raise ScrapeError("Use a KuantoKusta product page link (kuantokusta.pt/p/…)")
        html = await self.fetcher.browse(url, self.name, self.hosts)
        return with_page(self.parse(html, url), html)

    def parse(self, html: str, url: str) -> list[Snapshot]:
        data = BeautifulSoup(html, "html.parser").select_one("script#__NEXT_DATA__")
        try:
            product = json.loads(data.string)["props"]["pageProps"]["basePage"]["product"]
            name, offers = product["name"], product["offers"]
        except (AttributeError, TypeError, KeyError, ValueError):
            raise ScrapeError("KuantoKusta page data changed; adapter update required") from None
        # The page renders ten offer cards; its data holds all of them.
        best, best_total = None, None
        for offer in offers:
            price = Decimal(str(offer.get("price") or 0))
            shipping = Decimal(str((offer.get("shipping") or {}).get("minimumPrice") or 0))
            if price > 0 and (best_total is None or price + shipping < best_total):
                best, best_total = (offer, shipping), price + shipping
        offered_by = None
        if best:
            offer, shipping = best
            note = "free shipping" if not shipping else f"€{shipping:,.2f} shipping"
            offered_by = f"{offer.get('storeName') or 'Unknown store'} · {note}"
        eans = product.get("ean") or []
        return [
            Snapshot(
                retailer=self.name,
                url=self.normalize_url(url),
                retailer_product_id=str(product.get("id") or "") or self.product_id(url),
                identity=self.extract_product_identity(
                    {"brand": product.get("brand"), "gtin13": eans[0] if eans else None}, name
                ),
                title=name,
                price=best_total,
                availability="in_stock" if best else "out_of_stock",
                condition=self.default_condition,
                offered_by=offered_by,
            )
        ]
