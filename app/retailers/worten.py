import json
import re
from decimal import Decimal
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.retailers.base import RetailerAdapter
from app.retailers.parsing import ScrapeError, money, text_at
from app.schemas.domain import Snapshot
from app.services.matching import normalize

COUPON_FLAG = re.compile(r"-\s*(\d{1,2})\s*%\s*c/\s*cup[ãa]o\s+([A-Z0-9]+)", re.I)


class WortenAdapter(RetailerAdapter):
    name = "worten"
    label = "Worten"
    hosts = ("www.worten.pt", "worten.pt")
    search_path = "/search?query={query}"
    product_pattern = r"/produtos/[^/?]+-(\d+)$"
    status = "supported"
    status_note = "Product JSON-LD, seller, search and outlet-grade discovery validated live over HTTP/2. Outlet grades require explicit page evidence."
    seller_selector = ".product-price-info__seller__name"
    condition_selector = ".product-condition, .outlet-grade, [data-condition]"
    original_selector = ".price__old, .product-price__old, del[itemprop='price']"

    def parse(self, html: str, url: str) -> list[Snapshot]:
        snapshots = super().parse(html, url)
        soup = BeautifulSoup(html, "html.parser")
        # "Preço com cupão: 1.999,20 · Aplica o cupão TCL20" beside the shelf price. Worten's
        # server usually sends only the "-20% c/ cupão TCL20" flag and its page script works
        # the price out; do the same, taking the biggest coupon on the product's own page.
        # Store-credit flags ("10% extra em talão") are not a lower price.
        coupon = soup.select_one(".product-price-info .price-with-coupon")
        shown = money(text_at(coupon, '[itemprop="price"]')) if coupon else None
        shown_code = (
            re.search(r"cup[ãa]o\s+([A-Z0-9]+)", coupon.get_text(" "), re.I) if coupon else None
        )
        flags = [
            (int(m.group(1)), m.group(2).upper())
            for flag in soup.select(".flags-attached [aria-label]")
            if (m := COUPON_FLAG.search(flag["aria-label"]))
        ]
        for snapshot in snapshots:
            if snapshot.price is None:
                continue
            if shown is not None:
                price, code = shown, shown_code.group(1).upper() if shown_code else None
            elif flags:
                percent, code = max(flags)
                price = (snapshot.price * (100 - percent) / 100).quantize(Decimal("0.01"))
            else:
                continue
            if price < snapshot.price:
                snapshot.promo_price, snapshot.promo_code = price, code
        # Worten's JSON-LD keeps saying InStock for listings that cannot be bought; the
        # server-rendered buy box is authoritative ("--unavailability", no cart button).
        box = soup.select_one(".add-to-cart--buy-box")
        if box is not None:
            buyable = "add-to-cart--unavailability" not in box.get("class", []) and box.select_one(
                "button"
            )
            for snapshot in snapshots:
                snapshot.availability = "in_stock" if buyable else "out_of_stock"
        return snapshots

    def api_search_links(self, payload: dict, identity) -> list[str]:
        items = (
            payload.get("detailsResponse", {})
            .get("productsCanonicalsData", {})
            .get("web_items", [])
        )
        links = []
        for item in items:
            path = item.get("url", "")
            if identity.model and normalize(identity.model) not in normalize(path):
                continue
            url = self.normalize_url(urljoin("https://www.worten.pt", path))
            if self.is_product_url(url):
                links.append(url)
        return list(dict.fromkeys(links))[:4]

    async def search_term(self, term: str, identity) -> list[str]:
        # This is the public search endpoint used by Worten's own storefront.
        raw = await self.fetcher.get(
            "https://www.worten.pt/worten-api/search-products",
            self.name,
            self.hosts,
            json_body={"query": term, "params": {}},
        )
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or "searchResponse" not in payload:
                raise ValueError
            return self.api_search_links(payload, identity)
        except (ValueError, TypeError):
            raise ScrapeError("Worten search response changed; adapter update required") from None
