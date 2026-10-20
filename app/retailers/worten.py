import json
import re
from decimal import Decimal
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from app.retailers.base import RetailerAdapter
from app.retailers.parsing import ScrapeError, money, text_at
from app.schemas.domain import FeedItem, Snapshot
from app.services.matching import normalize

COUPON_FLAG = re.compile(r"-\s*(\d{1,2})\s*%\s*c/\s*cup[ãa]o\s+([A-Z0-9]+)", re.I)
COUPON_BADGE = re.compile(r"-\s*(\d{1,2})\s*%\s*c/\s*cup[ãa]o", re.I)
COUPON_CODE = re.compile(r"cup[ãa]o\s+([A-Z0-9]{3,})\b")
SEARCH_API = "https://www.worten.pt/worten-api/search-products"
BADGES_API = "https://www.worten.pt/worten-api/cms/solve-offer-badges-batch"
BADGES_BATCH = 24
# Product photos as the storefront's cards show them: /i/<the image's "default" transform>.
IMAGES = "https://www.worten.pt/i/"
IMAGE_ID = re.compile(r"[0-9a-f]{40}")
FEED_PAGE_SIZE = 48
FEED_MAX_PAGES = 20


class WortenAdapter(RetailerAdapter):
    name = "worten"
    label = "Worten"
    hosts = ("www.worten.pt", "worten.pt")
    search_path = "/search?query={query}"
    product_pattern = r"/produtos/[^/?]+-(\d+)$"
    feed_pattern = r"^/(?!produtos/|search)[\w-]+(?:/[\w-]+)*$"
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

    async def fetch_feed(self, url: str) -> list[FeedItem]:
        url = self.normalize_url(url)
        if not self.is_feed_url(url):
            raise ScrapeError("Use a Worten category or outlet page link")
        # The page sends no product cards: its script asks the search API for the category
        # the page names, 48 at a time, then asks for each card's flags. Ask the same way.
        data = nuxt_data(await self.fetcher.get(url, self.name, self.hosts))
        path = urlsplit(url).path
        context, grid = self.feed_context(data, url), self.feed_grid(data)
        page_context = self.feed_page_context(data, path)
        offers: dict[str, FeedItem] = {}
        for page in range(FEED_MAX_PAGES):
            raw = await self.fetcher.get(
                SEARCH_API,
                self.name,
                self.hosts,
                json_body={
                    "contexts": [context],
                    "params": {
                        "pageNumber": page,
                        "pageSize": FEED_PAGE_SIZE,
                        "filters": [],
                        "sort": {"field": "rank1", "order": "ASC"},
                        "collapse": True,
                    },
                    "hasVariants": True,
                },
            )
            try:
                payload = json.loads(raw)
                offers.update(self.feed_items(payload))
                more = payload["searchResponse"].get("hasNextPage") is True
            except (ValueError, TypeError, KeyError, AttributeError):
                raise ScrapeError(
                    "Worten listing response changed; adapter update required"
                ) from None
            if not more:
                break
        else:
            raise ScrapeError(
                f"This page lists more than {FEED_MAX_PAGES * FEED_PAGE_SIZE} products"
            )
        ids = list(offers)
        for start in range(0, len(ids), BADGES_BATCH):
            raw = await self.fetcher.get(
                BADGES_API,
                self.name,
                self.hosts,
                json_body={
                    "badges": [
                        {"offerId": offer, "zone": "card_badge_winning_offer", "moduleId": grid}
                        for offer in ids[start : start + BADGES_BATCH]
                    ],
                    "contextId": page_context,
                    "uri": path,
                    "debug": False,
                },
            )
            try:
                self.feed_coupons(offers, json.loads(raw))
            except (ValueError, TypeError, KeyError, AttributeError):
                raise ScrapeError(
                    "Worten flags response changed; adapter update required"
                ) from None
        return list({item.item_id: item for item in offers.values()}.values())

    def feed_context(self, data: list, url: str) -> str:
        """The category id the page's own script searches."""
        path = urlsplit(url).path
        for ident, canonical in nuxt_objects(data, "id", "canonical"):
            if canonical == path and isinstance(ident, str) and ident:
                return ident
        raise ScrapeError("This Worten page isn't a product listing")

    def feed_grid(self, data: list) -> str:
        """The page's product grid module, which Worten wants named when asked for flags."""
        for ident, targets in nuxt_objects(data, "id", "targets"):
            if isinstance(ident, str) and "produto-filtered-grid" in str(targets):
                return ident
        raise ScrapeError("Worten listing page changed; adapter update required")

    def feed_page_context(self, data: list, path: str) -> str:
        """The page's own context key, which the flags request must quote with its path."""
        for uri, context in nuxt_objects(data, "uri", "contextId"):
            if uri == path and isinstance(context, str) and context:
                return context
        raise ScrapeError("Worten listing page changed; adapter update required")

    def feed_items(self, payload: dict) -> dict[str, FeedItem]:
        """The page's products, keyed by the offer Worten's flags refer to."""
        details = payload["detailsResponse"]
        urls = {w["id"]: w["url"] for w in details["productsCanonicalsData"]["web_items"]}
        offers = {o["offer_id"]: o for o in details["offersData"]["offers"]}
        items = {}
        for product in details["productsData"]["products"]:
            refs, offer_id = product["meta"]["refs"], product["woffer"]["offer_id"]
            path, name = urls.get(refs["webitem_id"]), product["properties"]["text"].get("name")
            offer = offers.get(offer_id)
            if not (offer and path and name and refs.get("sku")):
                continue
            price = money(offer["price"].get("final"))
            original = money(offer["price"].get("original"))
            image = (product.get("image") or {}).get("transforms", {}).get("default")
            items[offer_id] = FeedItem(
                item_id=str(refs["sku"]),
                title=name,
                url=self.normalize_url(urljoin("https://www.worten.pt", path)),
                price=price,
                original_price=original if original and price and original > price else None,
                in_stock=offer.get("stock", {}).get("is_in_stock") is True,
                image_url=f"{IMAGES}{image}" if IMAGE_ID.fullmatch(str(image)) else None,
            )
        return items

    def feed_coupons(self, items: dict[str, FeedItem], payload: dict):
        """Apply each card's "-20% c/ cupão" flag, as parse does on a product page.

        The card flag carries no code; the flag's pop-up names it ("com o cupão OFF20").
        """
        if payload.get("success") is not True:
            raise ValueError
        for result in payload["results"]:
            item = items.get(result.get("offerId"))
            if item is None or item.price is None or not result.get("ok"):
                continue
            coupons = []
            for zone in result["data"].get("zones", []):
                for badge in zone.get("badges", []):
                    config = badge["details"]["data"]["config"]
                    if match := COUPON_BADGE.search(str(config.get("text", ""))):
                        popup = BeautifulSoup(str(config.get("modalContent") or ""), "html.parser")
                        code = COUPON_CODE.search(popup.get_text(" "))
                        coupons.append((int(match.group(1)), code.group(1) if code else None))
            if coupons:
                percent, code = max(coupons, key=lambda coupon: coupon[0])
                item.promo_price = (item.price * (100 - percent) / 100).quantize(Decimal("0.01"))
                item.promo_code = code

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
            SEARCH_API,
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


def nuxt_data(html: str) -> list:
    script = BeautifulSoup(html, "html.parser").select_one("script#__NUXT_DATA__")
    try:
        data = json.loads(script.get_text()) if script else []
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def nuxt_objects(data: list, *keys: str):
    """Nuxt flattens its state into one array; objects point at their values by index."""
    for entry in data:
        if isinstance(entry, dict) and set(keys) <= entry.keys():
            try:
                yield tuple(data[entry[key]] for key in keys)
            except (IndexError, TypeError):
                continue
