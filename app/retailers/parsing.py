import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from bs4 import BeautifulSoup

from app.schemas.domain import Condition


class ScrapeError(Exception):
    """A safe, user-visible scraper failure without URLs or credentials."""


class BlockedError(ScrapeError):
    pass


def money(value: Any) -> Decimal | None:
    if value is None:
        return None
    text = re.sub(r"[^\d.,-]", "", str(value))
    if "," in text and "." in text:
        text = (
            text.replace(".", "").replace(",", ".")
            if text.rfind(",") > text.rfind(".")
            else text.replace(",", "")
        )
    elif "," in text:
        text = text.replace(",", ".")
    try:
        result = Decimal(text)
        return result.quantize(Decimal(".01")) if result.is_finite() and result > 0 else None
    except InvalidOperation:
        return None


def condition(value: str) -> Condition:
    text = value.casefold()
    for grade, result in (("a", Condition.A), ("b", Condition.B), ("c", Condition.C)):
        if re.search(rf"(?:grade|grau|grado)\s*[-:]?\s*{grade}\b", text):
            return result
    if any(x in text for x in ("refurbished", "recondicionado", "refurbishedcondition")):
        return Condition.REFURBISHED
    if any(x in text for x in ("outlet", "open box", "open-box", "caixa aberta")):
        return Condition.OUTLET
    if any(x in text for x in ("usedcondition", "usado", "segunda mão")):
        return Condition.USED
    if any(x in text for x in ("newcondition", "novo", "nuevo")):
        return Condition.NEW
    return Condition.UNKNOWN


def availability(value: str) -> str:
    value = value.casefold()
    if any(
        x in value
        for x in (
            "outofstock",
            "soldout",
            "discontinued",
            "indispon",
            "esgotado",
            "sem stock",
            "no disponible",
        )
    ):
        return "out_of_stock"
    if any(
        x in value for x in ("instock", "limitedavailability", "em stock", "disponível", "en stock")
    ):
        return "in_stock"
    if any(x in value for x in ("preorder", "backorder")):
        return "preorder"
    return "unknown"


def objects(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from objects(child)


def product_data(soup: BeautifulSoup) -> list[dict]:
    products = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.get_text())
        except (ValueError, TypeError):
            continue
        for item in objects(data):
            types = item.get("@type", [])
            if "Product" in (types if isinstance(types, list) else [types]):
                products.append(item)
    return products


def text_at(soup: BeautifulSoup, selector: str) -> str:
    item = soup.select_one(selector) if selector else None
    if item is None:
        return ""
    return str(item.get("content") or item.get_text(" ", strip=True))
