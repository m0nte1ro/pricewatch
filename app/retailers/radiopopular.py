from app.retailers.base import RetailerAdapter
from app.schemas.domain import Condition


class RadioPopularAdapter(RetailerAdapter):
    name = "radiopopular"
    label = "Rádio Popular"
    hosts = ("www.radiopopular.pt", "radiopopular.pt")
    search_path = "/pesquisa/{query}"
    product_pattern = r"/produto/([^/?]+)"
    status_note = "Partial: live price, stock and search parsing validated. Sells its own new stock, so offers without outlet/refurbished markers count as new; seller is not exposed."
    default_condition = Condition.NEW
