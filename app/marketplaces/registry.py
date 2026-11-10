from urllib.parse import urlsplit

from app.marketplaces.base import Marketplace
from app.marketplaces.wallapop import WallapopMarketplace
from app.retailers.parsing import ScrapeError


class Marketplaces:
    def __init__(self, fetcher):
        self.providers: dict[str, Marketplace] = {
            cls.name: cls(fetcher) for cls in (WallapopMarketplace,)
        }
        for provider in self.providers.values():
            if provider.request_interval:
                fetcher.intervals[provider.name] = provider.request_interval

    def __getitem__(self, name: str) -> Marketplace:
        return self.providers[name]

    def values(self):
        return self.providers.values()

    def for_url(self, url: str) -> Marketplace:
        host = urlsplit(url).hostname
        for provider in self.providers.values():
            if host in provider.hosts:
                return provider
        labels = ", ".join(provider.label for provider in self.providers.values())
        raise ScrapeError(f"Paste a search link from {labels}")
