from app.retailers.base import RetailerAdapter


class FnacAdapter(RetailerAdapter):
    name = "fnac"
    label = "FNAC Portugal"
    hosts = ("www.fnac.pt", "fnac.pt")
    search_path = "/SearchResult/ResultList.aspx?Search={query}&sft=1&sa=0"
    product_pattern = r"/a(\d+)(?:/|$)"
    status_note = "Partial: JSON-LD and search implemented. Live product request returned HTTP 403; automatic cooldown applies."
    price_selector = ".f-priceBox-price, [itemprop='price']"
    seller_selector = ".f-marketplaceOffer-sellerName"
    original_selector = ".f-priceBox-price--old"
