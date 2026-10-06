from app.retailers.base import RetailerAdapter


class RadioPopularAdapter(RetailerAdapter):
    name = "radiopopular"
    label = "Rádio Popular"
    hosts = ("www.radiopopular.pt", "radiopopular.pt")
    search_path = "/pesquisa/{query}"
    product_pattern = r"/produto/([^/?]+)"
    status_note = "Partial: live price, stock and search parsing validated. Seller and condition are not consistently exposed and remain unknown."
