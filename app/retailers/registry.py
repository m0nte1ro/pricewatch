from urllib.parse import urlsplit

from app.retailers.amazon_es import AmazonESAdapter
from app.retailers.darty import DartyAdapter
from app.retailers.fnac import FnacAdapter
from app.retailers.parsing import ScrapeError
from app.retailers.radiopopular import RadioPopularAdapter
from app.retailers.worten import WortenAdapter


class Registry:
    def __init__(self, fetcher):
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

    def for_url(self, url: str):
        host = urlsplit(url).hostname
        for adapter in self.adapters.values():
            if host in adapter.hosts:
                return adapter
        raise ScrapeError(
            "Unsupported retailer URL. Supported: Worten, FNAC PT, Darty PT, Rádio Popular, Amazon ES."
        )
