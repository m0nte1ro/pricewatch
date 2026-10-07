import re
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from app.retailers.parsing import ScrapeError, availability, condition, money, product_data, text_at
from app.schemas.domain import Condition, Identity, Snapshot
from app.services.matching import identify, normalize


class RetailerAdapter:
    name: str
    label: str
    hosts: tuple[str, ...]
    search_path: str
    product_pattern: str
    status = "partial"
    status_note = "Structured-data parser and search links implemented; live coverage unverified."
    title_selector = "h1"
    price_selector = '[itemprop="price"]'
    stock_selector = '[itemprop="availability"]'
    seller_selector = ""
    condition_selector = ""
    original_selector = ""
    # Used only when the page shows no condition evidence. First-party stores selling only new
    # stock may set NEW; marketplaces must stay UNKNOWN so alerts never assume new.
    default_condition = Condition.UNKNOWN
    # Stores without a usable search (link-only) are left out of store search and its switches.
    searchable = True
    # Seconds between requests to this store; None uses the fetcher default (2 s).
    request_interval: float | None = None

    def __init__(self, fetcher):
        self.fetcher = fetcher

    def normalize_url(self, url: str) -> str:
        url = url.strip()
        parsed = urlsplit(url)
        self.fetcher.validate_url(url, self.hosts)
        query = [
            (k, v)
            for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if not k.lower().startswith(("utm_", "bvstate"))
            and k.lower()
            not in {
                "gclid",
                "fbclid",
                "ref",
                "tag",
                "ref_",
                "msockid",
                "_pos",
                "_psq",
                "_psid",
                "_ss",
            }
        ]
        path = re.sub(r"/{2,}", "/", parsed.path).rstrip("/") or "/"
        return urlunsplit(("https", self.hosts[0], path, urlencode(sorted(query)), ""))

    def product_id(self, url: str) -> str | None:
        match = re.search(self.product_pattern, urlsplit(url).path, re.I)
        return match.group(1) if match and match.lastindex else None

    def is_product_url(self, url: str) -> bool:
        return bool(re.search(self.product_pattern, urlsplit(url).path, re.I))

    def extract_product_identity(self, data: dict, title: str) -> Identity:
        brand = data.get("brand")
        if isinstance(brand, dict):
            brand = brand.get("name")
        ids = {}
        # Stores label the same EAN as gtin13, gtin, gtin12 or gtin14; compare one form.
        for key in ("gtin13", "gtin", "gtin14", "gtin12"):
            digits = re.sub(r"\D", "", str(data.get(key) or ""))
            if len(digits) in (12, 13, 14):
                gtin = digits.zfill(14)
                ids["gtin"] = gtin[1:] if gtin.startswith("0") else gtin
                break
        if data.get("mpn"):
            ids["mpn"] = str(data["mpn"])
        return identify(
            title,
            brand=brand if isinstance(brand, str) else None,
            model=str(data["model"]) if isinstance(data.get("model"), (str, int)) else None,
            identifiers=ids,
        )

    def parse(self, html: str, url: str) -> list[Snapshot]:
        soup = BeautifulSoup(html, "html.parser")
        products = product_data(soup)
        heading = text_at(soup, self.title_selector)
        # Recommendations are often also Product JSON-LD: select only the page's product.
        data = next(
            (x for x in products if normalize(str(x.get("name", ""))) == normalize(heading)), {}
        )
        if not data:
            data = next(
                (
                    x
                    for x in products
                    if x.get("url")
                    and urlsplit(urljoin(url, str(x["url"]))).path == urlsplit(url).path
                ),
                {},
            )
        if not data and not heading and len(products) == 1:
            data = products[0]
        title = str(data.get("name") or heading)
        if not title:
            raise ScrapeError("No product data found; adapter may need updating")
        offers = data.get("offers", {})
        if isinstance(offers, dict) and offers.get("@type") == "AggregateOffer":
            offers = offers.get(
                "offers", []
            )  # Never present a range minimum as a purchasable offer.
        offers = offers if isinstance(offers, list) else [offers]
        if not offers:
            raise ScrapeError("Only aggregate pricing found; no concrete offer available")
        result = []
        for offer in offers:
            if not isinstance(offer, dict):
                continue
            price = money(offer.get("price"))
            if price is None and len(offers) == 1 and not data.get("offers"):
                price = money(text_at(soup, self.price_selector))
            stock = availability(
                str(offer.get("availability", "")) or text_at(soup, self.stock_selector)
            )
            if price is None and stock == "unknown":
                continue
            seller = offer.get("seller", {})
            seller = seller.get("name") if isinstance(seller, dict) else seller
            seller = seller or text_at(soup, self.seller_selector) or None
            condition_text = " ".join(
                [title, text_at(soup, self.condition_selector), str(offer.get("itemCondition", ""))]
            )
            item_condition = condition(condition_text)
            if item_condition == Condition.UNKNOWN:
                item_condition = self.default_condition
            offer_url = urljoin(url, offer.get("url") or url)
            try:
                offer_url = self.normalize_url(offer_url)
            except (ScrapeError, ValueError):
                offer_url = self.normalize_url(url)
            result.append(
                Snapshot(
                    retailer=self.name,
                    url=offer_url,
                    retailer_product_id=str(
                        data.get("sku")
                        or text_at(soup, '[itemprop="sku"]')
                        or self.product_id(offer_url)
                        or ""
                    )
                    or None,
                    identity=self.extract_product_identity(data, title),
                    title=title,
                    price=price,
                    original_price=money(text_at(soup, self.original_selector)),
                    currency=str(
                        offer.get("priceCurrency")
                        or text_at(soup, '[itemprop="priceCurrency"]')
                        or "EUR"
                    ).upper(),
                    availability=stock,
                    condition=item_condition,
                    seller=seller,
                )
            )
        if not result:
            raise ScrapeError("No reliable price or stock found; adapter may need updating")
        return result

    async def fetch_listing(self, url: str, *, alternatives: bool = False) -> list[Snapshot]:
        """`alternatives` also lists the page's prices for the owner to confirm one."""
        url = self.normalize_url(url)
        if not self.is_product_url(url):
            raise ScrapeError("Use a product page URL, not a search or category page")
        html = await self.fetcher.get(url, self.name, self.hosts)
        try:
            return self.parse_page(html, url, alternatives)
        except ScrapeError:
            if not self.fetcher.get_preferences().playwright_enabled:
                raise
            html = await self.fetcher.render(url, self.name, self.hosts)
            return self.parse_page(html, url, alternatives)

    def parse_page(self, html: str, url: str, alternatives: bool) -> list[Snapshot]:
        # Only the generic reader has other prices to offer; dedicated adapters ignore it.
        return self.parse(html, url)

    def search_links(self, html: str, identity: Identity) -> list[str]:
        soup = BeautifulSoup(html, "html.parser")
        found: list[tuple[int, str]] = []
        for anchor in soup.select("a[href]"):
            try:
                url = self.normalize_url(urljoin(f"https://{self.hosts[0]}", str(anchor["href"])))
            except (ScrapeError, ValueError):
                continue
            if self.is_product_url(url):
                relevant = normalize(identity.model or identity.name) in normalize(
                    url + anchor.get_text()
                )
                if relevant or not identity.model:
                    found.append((0 if relevant else 1, url))
        # Only fetch a small number of likely matches, never an entire search catalog.
        return list(dict.fromkeys(url for _, url in sorted(found)))[:4]

    def search_terms(self, identity: Identity) -> list[str]:
        # Store searches rank in-stock items first, so "brand model" can push a discontinued or
        # out-of-stock page out of the results; the bare model finds it, and the barcode is the
        # last resort for stores that index it.
        terms = (identity.model, identity.name, identity.identifiers.get("gtin"))
        return list(dict.fromkeys(term for term in terms if term))

    async def search_product(self, identity: Identity) -> list[str]:
        for term in self.search_terms(identity):
            links = await self.search_term(term, identity)
            if links:
                return links
        return []

    async def search_term(self, term: str, identity: Identity) -> list[str]:
        query = quote(term, safe="")
        url = f"https://{self.hosts[0]}{self.search_path.format(query=query)}"
        html = await self.fetcher.get(url, self.name, self.hosts)
        links = self.search_links(html, identity)
        if not links and self.fetcher.get_preferences().playwright_enabled:
            links = self.search_links(
                await self.fetcher.render(url, self.name, self.hosts), identity
            )
        return links
