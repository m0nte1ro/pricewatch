import ipaddress
import logging
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from itertools import islice
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup, CData, NavigableString, Tag

from app.retailers.base import RetailerAdapter
from app.retailers.parsing import ScrapeError, availability, condition, money, text_at
from app.schemas.domain import Condition, Identity, PriceCandidate, PriceRule, Snapshot
from app.services.matching import identify

log = logging.getLogger(__name__)

PRICE_HINT = re.compile(r"price|pre[cç]o|valor|amount", re.I)
OLD_HINT = re.compile(
    r"old|before|antes|anterior|regular|was|compare|strike|pvpr|list-price|riscado", re.I
)
CONTEXT_HINT = re.compile(r"product|produto|item|detail|buy|offer", re.I)
NOISE_HINT = re.compile(
    r"related|recommend|sugest|also|carousel|slider|upsell|cross|bundle|accessor", re.I
)
STOCK_HINT = re.compile(r"stock|avail|dispon", re.I)
CART_HINT = re.compile(r"adicionar ao carrinho|add to cart|comprar|buy now|añadir al carrito", re.I)
IDENT = re.compile(r"[A-Za-z_-][A-Za-z0-9_-]*")
LONG_NUMBER = re.compile(r"\d{4,}")
TITLE_SEPARATOR = re.compile(r" [|\-–—] ")
LOCAL_SUFFIXES = (
    ".local",
    ".localhost",
    ".internal",
    ".lan",
    ".home",
    ".arpa",
    ".test",
    ".example",
    ".invalid",
)
PUBLIC_ONLY = "Only public https:// store links can be monitored"
OUT_OF_STOCK_TEXT = ("esgotado", "indisponível", "sem stock", "fora de stock", "out of stock")
CURRENCIES = (("EUR", "€"), ("GBP", "£"), ("USD", "$"))
# Letter codes count only as whole words ("euros" and "europeia" are not EUR), but a digit may
# touch them: some stores write "1.299,99EUR".
CURRENCY_MARK = re.compile(r"[€$£]|(?<![a-z])(?:EUR|GBP|USD)(?![a-z])", re.I)
BARE_AMOUNT = re.compile(r"[\d\s.,]+")
# Exact vocabulary only: parsing.availability() matches substrings, and "in stock" is inside
# Spanish "sin stock".
META_STOCK = {
    **dict.fromkeys(
        ("instock", "in stock", "in_stock", "available", "limitedavailability", "onlineonly"),
        "in_stock",
    ),
    **dict.fromkeys(
        (
            "oos",
            "out of stock",
            "outofstock",
            "out_of_stock",
            "soldout",
            "sold out",
            "discontinued",
        ),
        "out_of_stock",
    ),
    **dict.fromkeys(("preorder", "pre-order", "presale", "backorder"), "preorder"),
}


@dataclass
class Reading:
    title: str
    price: Decimal | None
    currency: str
    availability: str
    method: str
    alternatives: list[PriceCandidate]


def _address(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    try:
        # The resolver also reads "127.1" and "0x7f.1" as IPv4 addresses without asking DNS.
        socket.inet_aton(name)
        return True
    except OSError:
        return False


def public_host(hostname: str) -> bool:
    # "localhost." is the same name as "localhost" to the resolver.
    name = hostname.lower().strip("[]").rstrip(".")
    return "." in name and not name.endswith(LOCAL_SUFFIXES) and not _address(name)


def store_key(hostname: str) -> str:
    return hostname.lower().removeprefix("www.")


def page_title(soup: BeautifulSoup) -> str:
    if name := text_at(soup, "h1") or text_at(soup, 'meta[property="og:title"]').strip():
        return name
    title = text_at(soup, "title")
    cuts = list(TITLE_SEPARATOR.finditer(title))
    return title[: cuts[-1].start()].strip() if cuts else title


def currency_of(text: str) -> str:
    return next((code for code, sign in CURRENCIES if sign in text or code in text.upper()), "EUR")


def _hints(tag: Tag) -> str:
    return " ".join([*tag.get_attribute_list("class", ""), tag.get("id", "")])


def _segment(tag: Tag) -> tuple[str, bool]:
    # The pattern admits names such as "-1a" and tags such as "o:p" that are not valid CSS as
    # written, so every part is escaped.
    escape = tag.css.escape
    ident = tag.get("id", "")
    if IDENT.fullmatch(ident):
        return f"{escape(tag.name)}#{escape(ident)}", True
    classes = [
        c
        for c in tag.get_attribute_list("class", "")
        if IDENT.fullmatch(c) and not LONG_NUMBER.search(c)
    ]
    return escape(tag.name) + "".join(f".{escape(c)}" for c in classes), False


def _selects_only(soup: BeautifulSoup, path: str, element: Tag) -> bool:
    # Tag equality compares markup, so two identical siblings are "equal"; compare identity.
    found = soup.select(path, limit=2)
    return len(found) == 1 and found[0] is element


def _nth(tag: Tag) -> str:
    return f":nth-of-type({len(tag.find_previous_siblings(tag.name)) + 1})"


def _positional_path(element: Tag, soup: BeautifulSoup) -> str:
    parts, node = [], element
    while node is not None and not isinstance(node, BeautifulSoup):
        segment, anchored = _segment(node)
        if anchored and len(soup.select(segment, limit=2)) == 1:
            parts.append(segment)
            break
        parts.append(segment + _nth(node))
        node = node.parent
    return " > ".join(reversed(parts))


def css_path(element: Tag, soup: BeautifulSoup) -> str:
    path, anchored = _segment(element)
    if len(soup.select(path, limit=2)) > 1:
        path += _nth(element)
    node, levels = element, 0
    while not _selects_only(soup, path, element):
        node = node.parent
        if anchored or levels == 6 or node is None or isinstance(node, BeautifulSoup):
            # A position on every step pins one element below a unique id or a single root.
            return _positional_path(element, soup)
        segment, anchored = _segment(node)
        path = f"{segment} > {path}"
        levels += 1
    return path


def _priced(tag: Tag) -> bool:
    return bool(PRICE_HINT.search(_hints(tag))) or tag.get("itemprop") == "price"


def _score(element: Tag) -> int:
    near = list(islice(element.parents, 4))
    old = any(
        t.name in ("del", "s", "strike") or OLD_HINT.search(_hints(t)) for t in [element, *near[:3]]
    )
    return (
        2 * bool(PRICE_HINT.search(_hints(element)))
        + (element.get("itemprop") == "price")
        + any(CONTEXT_HINT.search(_hints(p)) for p in near)
        - 3 * old
        - 2 * any(NOISE_HINT.search(_hints(p)) for p in element.parents)
    )


def price_candidates(soup: BeautifulSoup) -> list[PriceCandidate]:
    found = []
    for element in soup.find_all(_priced):
        text = element.get_text(" ", strip=True)
        # A wrapper around an old and a current price would read as one long number.
        if (
            len(text) <= 40
            and len(CURRENCY_MARK.findall(text)) <= 1
            and (price := money(text)) is not None
        ):
            found.append((_score(element), element, price, text))
    candidates = []
    # sorted() is stable, so equal scores keep document order.
    for _, element, price, text in sorted(found, key=lambda item: -item[0]):
        selector = css_path(element, soup)
        # Pages without a single root element can defeat even a positional path. A selector
        # that reaches only its own element is never shared, so this also drops duplicates.
        if _selects_only(soup, selector, element):
            candidates.append(PriceCandidate(selector=selector, price=price, text=text))
            if len(candidates) == 8:
                break
    return candidates


def _cart_button(button: Tag) -> bool:
    label = f"{button.get_text(' ', strip=True)} {button.get('value', '')}"
    return not button.has_attr("disabled") and bool(CART_HINT.search(label))


def _in_noise(tag: Tag) -> bool:
    # Recommendation blocks show other products' stock labels and cart buttons.
    return any(NOISE_HINT.search(_hints(t)) for t in (tag, *tag.parents))


def _stock_hinted(tag: Tag) -> bool:
    return any(STOCK_HINT.search(_hints(t)) for t in (tag, *islice(tag.parents, 3)))


def _says_sold_out(soup: BeautifulSoup) -> bool:
    # Only the strings get_text() shows: script, style and comment text never reach shoppers.
    shown = (s for s in soup.find_all(string=True) if type(s) in (NavigableString, CData))
    text = " ".join(t for s in shown if (t := s.strip()) and not _in_noise(s.parent))
    return any(x in text.casefold() for x in OUT_OF_STOCK_TEXT)


def _buyable(soup: BeautifulSoup) -> Tag | None:
    buttons = soup.select("button, input[type=submit]")
    return next((b for b in buttons if _cart_button(b) and not _in_noise(b)), None)


def heuristic_availability(soup: BeautifulSoup) -> str:
    for tag in soup.find_all(lambda t: bool(STOCK_HINT.search(_hints(t)))):
        state = availability(tag.get_text(" ", strip=True))
        if state != "unknown" and not _in_noise(tag):
            return state
    # Some stores keep the cart button enabled on sold-out pages.
    if _says_sold_out(soup):
        return "out_of_stock"
    return "in_stock" if _buyable(soup) else "unknown"


def _meta(soup: BeautifulSoup, name: str) -> str:
    return (
        text_at(soup, f'meta[property="product:{name}"]')
        or text_at(soup, f'meta[property="og:{name}"]')
    ).strip()


def _meta_availability(soup: BeautifulSoup) -> str:
    tags = 'meta[property="product:availability"], meta[property="og:availability"]'
    if soup.select_one(tags) is None:
        return heuristic_availability(soup)
    # Meta readings can raise deal alerts, so an unrecognised value stays unknown rather than
    # being replaced by page guesses. Schema.org URLs map by their last path segment.
    return META_STOCK.get(_meta(soup, "availability").lower().rsplit("/", 1)[-1], "unknown")


def read_meta(soup: BeautifulSoup) -> Reading | None:
    price = money(_meta(soup, "price:amount"))
    if price is None:
        return None
    return Reading(
        title=page_title(soup),
        price=price,
        currency=_meta(soup, "price:currency").upper() or "EUR",
        availability=_meta_availability(soup),
        method="meta",
        alternatives=[],
    )


def read_heuristic(soup: BeautifulSoup) -> Reading:
    candidates = price_candidates(soup)
    best = candidates[0] if candidates else None
    return Reading(
        title=page_title(soup),
        price=best.price if best else None,
        currency=currency_of(best.text) if best else "EUR",
        availability=heuristic_availability(soup),
        method="heuristic",
        alternatives=candidates,
    )


def _rule_availability(soup: BeautifulSoup, rule: PriceRule) -> str:
    if rule.availability_selector is None:
        return heuristic_availability(soup)
    element = soup.select_one(rule.availability_selector)
    if rule.availability_mode == "presence":
        # Sold-out pages often keep the slot but disable it or relabel it "Esgotado"/"Notify me".
        return "in_stock" if element is not None and _cart_button(element) else "out_of_stock"
    if element is None:
        # Sold-out pages may replace the stock label; a missing label alone proves nothing.
        return "out_of_stock" if _says_sold_out(soup) else "unknown"
    return availability(element.get_text(" ", strip=True))


def _rule_price(text: str) -> Decimal | None:
    # A rule's element can change under it, and money() coerces any digits: instalments,
    # "from ... to" pairs, warranties and ratings would read as a confirmed wrong price.
    marks = len(CURRENCY_MARK.findall(text))
    if len(text) > 40 or marks > 1 or not (marks or BARE_AMOUNT.fullmatch(text)):
        return None
    return money(text)


def read_rule(soup: BeautifulSoup, rule: PriceRule) -> Reading | None:
    element = soup.select_one(rule.price_selector)
    text = element.get_text(" ", strip=True) if element is not None else ""
    if (price := _rule_price(text)) is None:
        return None
    return Reading(
        title=page_title(soup),
        price=price,
        currency=currency_of(text),
        availability=_rule_availability(soup, rule),
        method="rule",
        alternatives=[],
    )


def _shortest(soup: BeautifulSoup, limit: int, accept: Callable[[Tag, str], bool]) -> Tag | None:
    found = None
    for element in soup.find_all(lambda t: t.name not in ("script", "style")):
        text = element.get_text(" ", strip=True)
        # A descendant comes later in document order, so on equal text the innermost wins.
        if (
            len(text) <= limit
            and (found is None or len(text) <= found[0])
            and accept(element, text)
        ):
            found = (len(text), element)
    return found[1] if found else None


def _stock_rule(soup: BeautifulSoup, state: str | None) -> tuple[str | None, str]:
    if state in ("in_stock", "out_of_stock"):

        def says(element: Tag, text: str) -> bool:
            return availability(text) == state and not _in_noise(element)

        # A stock label beats the same words elsewhere on the page, such as a legend.
        labelled = _shortest(soup, 60, lambda e, text: says(e, text) and _stock_hinted(e))
        if element := labelled or _shortest(soup, 60, says):
            return css_path(element, soup), "text"
    if state == "in_stock" and (button := _buyable(soup)):
        return css_path(button, soup), "presence"
    return None, "text"


def teach(soup: BeautifulSoup, price: Decimal, availability: str | None) -> PriceRule:
    element = _shortest(soup, 40, lambda _, text: _rule_price(text) == price)
    if element is None:
        raise ScrapeError(f"Could not find a price of {price} on the page")
    selector, mode = _stock_rule(soup, availability)
    return PriceRule(
        price_selector=css_path(element, soup),
        availability_selector=selector,
        availability_mode=mode,
    )


class GenericAdapter(RetailerAdapter):
    status = "generic"
    status_note = "Generic reader: structured data, meta tags, then heuristics. Confirm the price once per store to teach it the right element."
    product_pattern = r"^/.+"
    default_condition = Condition.NEW

    def __init__(self, fetcher, host: str, rules=None):
        super().__init__(fetcher)
        self.name = self.label = host
        self.hosts = (host, "www." + host)
        self.rules = rules

    def normalize_url(self, url: str) -> str:
        host = urlsplit(url.strip()).hostname
        # Every fetch, stored listing and offer URL passes through here, so a non-public host
        # is refused even when it reached this adapter some other way than Registry.for_url.
        if not host or not public_host(host):
            raise ScrapeError(PUBLIC_ONLY)
        # Unknown stores may serve a product on only one of www and the bare host, so keep the
        # one that was pasted instead of picking hosts[0] like the dedicated adapters.
        normalized = urlsplit(super().normalize_url(url))
        return urlunsplit(normalized._replace(netloc=host))

    async def search_product(self, identity: Identity) -> list[str]:
        return []

    def parse(
        self, html: str, url: str, *, rule: PriceRule | None = None, alternatives: bool = False
    ) -> list[Snapshot]:
        """Read the page, with `rule` in place of the store's saved rule when given.

        `alternatives` lists the page's prices for the owner to confirm one. Scheduled checks
        never show them, so they skip the work.
        """
        soup = BeautifulSoup(html, "html.parser")
        try:
            snapshots = super().parse(html, url)
        except ScrapeError:
            snapshots = []
        rule = rule or (self.rules.get(self.name) if self.rules else None)
        reading = read_rule(soup, rule) if rule else None
        if rule and reading is None:
            # The rule is kept: the owner re-confirms the price or forgets it in Settings.
            log.warning("store_rule_failed", extra={"retailer": self.name})
        # Structured data without a price is a miss: the page itself may still show one.
        priced = next((s for s in snapshots if s.price is not None), None)
        if reading is None and priced is not None:
            if alternatives:
                priced.alternatives = price_candidates(soup)
            return [priced]
        reading = reading or read_meta(soup) or read_heuristic(soup)
        if snapshots:
            # Structured data still names the product and states its stock when the page
            # reading cannot; out-of-stock must not turn into "unknown".
            reading.title = reading.title or snapshots[0].title
            if reading.availability == "unknown":
                reading.availability = snapshots[0].availability
        snapshot = self.snapshot(reading, soup, url)
        # A heuristic reading already holds the page's candidates.
        if alternatives and snapshot.method != "heuristic":
            snapshot.alternatives = price_candidates(soup)
        return [snapshot]

    def parse_page(self, html: str, url: str, alternatives: bool) -> list[Snapshot]:
        return self.parse(html, url, alternatives=alternatives)

    def preview_rule(
        self, html: str, url: str, price: Decimal, availability: str | None
    ) -> tuple[PriceRule, Snapshot]:
        """Teach a rule from the confirmed price and read the page with it, without saving it."""
        rule = teach(BeautifulSoup(html, "html.parser"), price, availability)
        return rule, self.parse(html, url, rule=rule, alternatives=True)[0]

    def learn(self, html: str, url: str, price: Decimal, availability: str | None) -> Snapshot:
        rule, snapshot = self.preview_rule(html, url, price, availability)
        self.rules.save(self.name, rule)
        return snapshot

    def snapshot(self, reading: Reading, soup: BeautifulSoup, url: str) -> Snapshot:
        if not reading.title:
            raise ScrapeError("No product title found; is this a product page?")
        found = condition(reading.title)
        return Snapshot(
            retailer=self.name,
            url=self.normalize_url(url),
            retailer_product_id=text_at(soup, '[itemprop="sku"]') or None,
            identity=identify(reading.title),
            title=reading.title,
            price=reading.price,
            currency=reading.currency,
            availability=reading.availability,
            condition=self.default_condition if found == Condition.UNKNOWN else found,
            method=reading.method,
            alternatives=reading.alternatives,
        )
