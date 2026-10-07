from urllib.parse import urlsplit

from app.retailers.amazon_es import AmazonESAdapter
from app.retailers.base import RetailerAdapter
from app.retailers.darty import DartyAdapter
from app.retailers.fnac import FnacAdapter
from app.retailers.generic import GenericAdapter, public_host, store_key
from app.retailers.parsing import ScrapeError
from app.retailers.radiopopular import RadioPopularAdapter
from app.retailers.worten import WortenAdapter


class Registry:
    def __init__(self, fetcher, rules=None):
        self.fetcher = fetcher
        self.rules = rules
        self.adapters = {
            cls.name: cls(fetcher)
            for cls in (
                WortenAdapter,
                FnacAdapter,
                DartyAdapter,
                RadioPopularAdapter,
                AmazonESAdapter,
            )
        }
        self.generic: dict[str, GenericAdapter] = {}

        for adapter in self.adapters.values():
            if adapter.request_interval:
                fetcher.intervals[adapter.name] = adapter.request_interval

    def __getitem__(self, name: str) -> RetailerAdapter:
        if name in self.adapters:
            return self.adapters[name]
        if name not in self.generic:
            self.generic[name] = GenericAdapter(self.fetcher, name, self.rules)
        return self.generic[name]

    def items(self):
        return self.adapters.items()

    def values(self):
        return self.adapters.values()

    def for_url(self, url: str) -> RetailerAdapter:
        host = urlsplit(url).hostname
        if host is None:
            raise ScrapeError("Enter a full https:// link")
        for adapter in self.adapters.values():
            if host in adapter.hosts:
                return adapter
        if not public_host(host):
            raise ScrapeError("Only public https:// store links can be monitored")
        return self[store_key(host)]
