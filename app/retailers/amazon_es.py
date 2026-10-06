import re
from urllib.parse import urlsplit, urlunsplit

from app.retailers.base import RetailerAdapter


class AmazonESAdapter(RetailerAdapter):
    name = "amazon_es"
    label = "Amazon ES"
    hosts = ("www.amazon.es", "amazon.es")
    search_path = "/s?k={query}"
    product_pattern = r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:/|$)"
    status_note = "Partial: ASIN URLs and buy-box selectors supported. Search may be blocked; seller/condition can remain unknown."
    title_selector = "#productTitle"
    price_selector = "#corePrice_feature_div .a-price .a-offscreen, #corePriceDisplay_desktop_feature_div .a-price .a-offscreen"
    stock_selector = "#availability"
    seller_selector = "#sellerProfileTriggerId"
    condition_selector = "#condition, #usedBuySection"
    original_selector = "#corePriceDisplay_desktop_feature_div .a-text-price .a-offscreen"

    def normalize_url(self, url: str) -> str:
        normalized = super().normalize_url(url)
        parsed = urlsplit(normalized)
        match = re.search(self.product_pattern, parsed.path, re.I)
        if match:
            return urlunsplit(
                ("https", self.hosts[0], f"/dp/{match.group(1).upper()}", parsed.query, "")
            )
        return normalized
