import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from app.retailers.base import RetailerAdapter
from app.schemas.domain import Condition, Snapshot
from app.services.matching import normalize


class AmazonESAdapter(RetailerAdapter):
    name = "amazon_es"
    label = "Amazon ES"
    hosts = ("www.amazon.es", "amazon.es")
    search_path = "/s?k={query}"
    product_pattern = r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:/|$)"
    status_note = "Partial: ASIN URLs, buy-box price, seller and stock validated live. Offers sold by Amazon count as new; third-party sellers stay unknown unless the page says new/used/renewed."
    title_selector = "#productTitle"
    price_selector = "#corePrice_feature_div .a-price .a-offscreen, #corePriceDisplay_desktop_feature_div .a-price .a-offscreen"
    stock_selector = "#availability"
    seller_selector = "#sellerProfileTriggerId, #merchantInfoFeature_feature_div .offer-display-feature-text-message"
    condition_selector = "#condition, #usedBuySection"
    original_selector = "#corePriceDisplay_desktop_feature_div .a-text-price .a-offscreen"

    def normalize_url(self, url: str) -> str:
        normalized = super().normalize_url(url)
        parsed = urlsplit(normalized)
        match = re.search(self.product_pattern, parsed.path, re.I)
        if match:
            # Search links carry keywords/qid/sr; only the seller offer identifies a listing.
            query = urlencode([(k, v) for k, v in parse_qsl(parsed.query) if k == "smid"])
            return urlunsplit(("https", self.hosts[0], f"/dp/{match.group(1).upper()}", query, ""))
        return normalized

    def parse(self, html: str, url: str) -> list[Snapshot]:
        snapshots = super().parse(html, url)
        # Delivery estimates ("Estimativa de envio…") replace "En stock" text; a cart button
        # on the buy box is the reliable signal that the offer can be bought.
        buyable = BeautifulSoup(html, "html.parser").select_one(
            "#add-to-cart-button, #buy-now-button"
        )
        for snapshot in snapshots:
            if snapshot.availability == "unknown" and buyable:
                snapshot.availability = "in_stock"
            # Amazon's own buy-box stock is new; renewed/used is marked and detected above.
            if snapshot.condition == Condition.UNKNOWN and normalize(snapshot.seller) in (
                "AMAZON",
                "AMAZONES",
            ):
                snapshot.condition = Condition.NEW
        return snapshots
