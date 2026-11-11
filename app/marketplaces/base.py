from app.schemas.domain import MarketItem


class Marketplace:
    """A second-hand marketplace whose searches pricewatch follows for new listings.

    A provider reads the search links of its own site: the owner sets keywords, price,
    category and location there and pastes the address.
    """

    name: str
    label: str
    # Hosts of the search pages it accepts.
    hosts: tuple[str, ...]
    # A search link, shown as the form's placeholder.
    example: str
    # Seconds between requests to this marketplace; None uses the fetcher default (2 s).
    request_interval: float | None = None

    def __init__(self, fetcher):
        self.fetcher = fetcher

    def normalize_url(self, url: str) -> str:
        """The search link in one spelling; ScrapeError when it is not a search."""
        raise NotImplementedError

    def describe(self, url: str) -> str:
        """A short name for the search, e.g. "ps5 · €100–€300"."""
        raise NotImplementedError

    async def fetch_listings(self, url: str, *, newest: bool = True) -> list[MarketItem]:
        """The newest listings the search finds; raises unless the read worked.

        `newest=False` reads the marketplace's best matches whatever their age, as its own
        site lists them: what is there now, for a search that has nothing yet.
        """
        raise NotImplementedError
