# Work log: pricewatch

## Where things stand
_Updated 2026-10-06_

- **Branch / state:** `main`, in sync with `origin/main` (head `7dab564`), clean working tree. All 109 tests pass and ruff is clean.
- **In progress:** live verification blocked by store-side throttling of this IP:
  - Darty answers the app (httpx) with HTTP 429 `TOO_MANY_REQUESTS` on product pages, while real Chromium gets 200. Darty search JSON still answers.
  - FNAC answers 403 to every non-browser request. This was set off by my test searches.
- **Next steps:**
  1. Once Darty answers the app again, run "Find / add listings" on the user's TCL 85C7L product (product id 3 in the container DB), searching Darty only. Confirm `darty.pt/products/smart-tv-tcl-85-sqd-mini-led-uhd-4k-85c7l-google-tv-216-cm-5901292529925` appears. Stop at the preview; the user confirms.
  2. Once FNAC answers, check that `fnac.pt/.../a12714588` (TCL 85C7K, out of stock) parses as `out_of_stock` with no deal alert. Fix the FNAC adapter if not.
  3. Keep live probing to single requests; bursts are what set off the throttling.
  4. The container needs a rebuild to pick up any new commit. Ask the user to do it; don't run it.
- **Open questions / decisions pending:**
  - Should the Playwright browser fallback kick in on 429/403 (per store, off by default)? It works (Chromium got 200 from Darty), but it gets around a limit stores deliberately apply to automated clients, against the spec's "do not bypass anti-bot". It also costs about 150–250 MB per page. Waiting on the user.
  - Offered and not yet answered: a general JSON-LD adapter, so links from unsupported stores (El Corte Inglés, PCDiga…) get monitored rather than listed as discovery notes.
  - Docker Compose is the primary deployment, while the original spec asked for systemd. The user committed Docker as the main path (`de3f560`) and hasn't said to change it.
- **User said:**
  - Commit code changes whenever they ask for a feature or fix (CLAUDE.md).
  - Don't run, start, stop or restart Docker unless told; it's expensive. Say when a rebuild is needed (CLAUDE.md).
  - Out-of-stock listings must record their price, show a clear OUT OF STOCK state, and never trigger deal alerts.
  - Manual URLs are first-class: "I may just have better sources than you do". They must always be fetched and must never block the other links.
  - Old models (85C7K) missing from FNAC/Darty search is "ok for now", but those listings should ideally appear.
- **Tried and dropped:**
  - Building Darty listings from search-suggestion JSON: it has no Darty SKU or condition, so it would duplicate the later page-based listings.
  - Sending session cookies to Darty: still 429.
  - Treating Spanish "renovado" as refurbished: it appears in normal marketing copy.
  - Treating a family code inside a regional code (`85C7K` / `85C7KPRO`) as a match: suffix variants must stay CONFLICT.
- **Was running:** two background polls (Darty product page and FNAC product page, one curl every 5 min, up to an hour). They die with the session. The user's container `pricewatch-pricewatch-1` is on port 8080, rebuilt at `40e31de`.
- **Watch out for:**
  - Worten's JSON-LD claims InStock for unbuyable items; the buy box is authoritative (`app/retailers/worten.py`).
  - FNAC search hides out-of-stock products completely; only pasted links find them.
  - Worten needs HTTP/2 (it rejects HTTP/1.1 from a browser user agent).
  - `pkill -f` patterns can match the container's uvicorn; track test servers by PID.
  - `./data` is owned by UID 10001 (the container), so native runs need `PRICEWATCH_DATA_DIR` pointing elsewhere.
- **Relevant docs:**
  - `README.md`: retailer coverage table, matching rules, and the cooldown/429 behaviour were all rewritten today.
  - `tests/fixtures/README.md`: provenance of the new live fixtures.
  - `.claude/CLAUDE.md`: the user's working rules.

## Log

### 2026-10-06
- **Did:**
  - Audited the repo against the original spec, then made live discovery work end to end:
    - HTTP/2 support.
    - Searching by model, then name, then barcode.
    - Worten Outlet matched despite its own retailer barcode.
    - Price parsing fixed ("1.299 €" now reads €1,299).
    - Amazon parsing (search links, stock from the cart button, seller detection).
    - Out-of-stock listings are recorded and shown, but get no deal alerts.
    - Cooldown no longer swallows pasted URLs.
    - 429 now means retry with backoff rather than a one-hour block.
  - Verified live: TCL 85C7L found at all 5 stores. TCL 85C7K found at Worten (new and Outlet Grade A) and at Darty, all out of stock. Browser test of the multiple-URL form passed.
- **Decided:**
  - Darty, Rádio Popular and offers sold by Amazon itself default to `new` when the page shows no outlet/refurbished markers, because those stores sell their own new stock. Third-party sellers stay `unknown`.
  - Pushes for "new listing" events are muted for listings the user just confirmed; they stay on the Alerts page.
  - Links from unsupported stores become discovery notes instead of a 422 that discards the form.
- **Commits:** `066a341`, `ed9fcdb`, `6f6670a`, `a9b4b77`, `220482e`, `40e31de` (all `fix:`); the user's `de3f560` (docker) and `7dab564` (CLAUDE.md).
- **Left open:** Darty 85C7L and FNAC 85C7K live verification (throttled); browser-fallback-on-429/403 decision; general adapter offer.
