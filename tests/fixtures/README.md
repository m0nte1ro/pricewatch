Fixture provenance:

- `worten_live.html`: reduced HTML from the public Worten TCL 85C7K page, retrieved 2026-10-06. Includes the original product JSON-LD and seller element. The description is irrelevant to parsing and may be removed without changing the test.
- `worten_search_live.json`: reduced response from Worten's public storefront search endpoint, retrieved 2026-10-06. Keeps canonical links and result count. The storefront returns nearby models when the exact one is absent; tests deliberately reject them.
- `darty_live.html`, `darty_search_live.json`, `radio_live.html`: reduced public Darty TCL 55P8L and Rádio Popular TCL 85C7L pages retrieved 2026-10-06. Missing seller/condition are intentionally asserted as unknown.
- `worten_outlet.html`, `fnac.html`, `darty.html`, `radiopopular.html`, `amazon_es.html`: synthetic contract fixtures. These test parsing branches, not live availability or production retailer coverage.

No credentials, cookies, or account data are saved. Live price assertions are fixture observations, not current-price guarantees.
