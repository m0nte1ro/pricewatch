from bs4 import BeautifulSoup

from app.retailers.base import RetailerAdapter
from app.retailers.generic import CART_HINT, OUT_OF_STOCK_TEXT
from app.retailers.parsing import ScrapeError, condition, money, text_at
from app.schemas.domain import Condition, Snapshot


class RadioPopularAdapter(RetailerAdapter):
    name = "radiopopular"
    label = "Rádio Popular"
    hosts = ("www.radiopopular.pt", "radiopopular.pt")
    search_path = "/pesquisa/{query}"
    product_pattern = r"/produto/([^/?]+)"
    status_note = "Partial: price, promo-code price, stock and product id read from the product header (live 2026-10-07); search parsing validated. Sells its own new stock, so offers without outlet/refurbished markers count as new; seller is not exposed."
    default_condition = Condition.NEW

    def parse(self, html: str, url: str) -> list[Snapshot]:
        soup = BeautifulSoup(html, "html.parser")
        # The page's first itemprop price/stock/sku belong to the "similar products" carousel;
        # the product's own figures are in its header.
        header = soup.select_one("#product-header-desktop") or soup.select_one(".product-header")
        if header is None:
            return super().parse(html, url)
        title = text_at(header, "h1")
        price = money(text_at(header, ".price"))
        if not title or price is None:
            raise ScrapeError("No product price found in the Rádio Popular product header")
        promo = header.select_one(".price-promocode-bar")
        promo_price = money(text_at(promo, ".price-promocode")) if promo else None
        if promo_price is not None and promo_price >= price:
            promo_price = None
        buy = header.select_one("[data-add2cart]")
        if (
            buy is not None
            and "visible" in buy.get("class", [])
            and CART_HINT.search(buy.get_text())
        ):
            stock = "in_stock"
        elif any(text in header.get_text(" ").casefold() for text in OUT_OF_STOCK_TEXT):
            stock = "out_of_stock"
        else:
            stock = "unknown"
        compare = header.select_one("[data-add-compare]")
        product_id = (buy and buy.get("data-add2cart")) or (
            compare and compare.get("data-add-compare")
        )
        found = condition(title)
        return [
            Snapshot(
                retailer=self.name,
                url=self.normalize_url(url),
                retailer_product_id=product_id or self.product_id(url),
                identity=self.extract_product_identity({}, title),
                title=title,
                price=price,
                original_price=money(text_at(header, ".old-price, .oldprice")),
                availability=stock,
                condition=self.default_condition if found == Condition.UNKNOWN else found,
                promo_price=promo_price,
                promo_code=(promo.get("data-promo-code") or None) if promo_price else None,
            )
        ]
