# Work log: pricewatch

## Where things stand
_Updated 2026-10-09_

- **Branch / state:** `main`, in sync with `origin/main` (head `636bd76`, the user's worklog commit), clean apart from this log. 278 tests pass, ruff and `alembic check` clean. Local and remote `link-aggregation` branches are fully merged, safe to delete.
- **Next steps:**
  1. The user rebuilds the local container (`docker compose up -d --build`); migrations `95b1e4bc7bfc`, `136c811fa067`, `9c429652d1ff`, `cfa384c596df` and today's `2c6d2944b736` (saved page column) apply on start.
  2. Local Settings → Export watchlist → move the file to the CT.
  3. CT (Debian trixie, Proxmox, 2 vCPU / 2 GB / 16 GB, nesting + keyctl on): install Docker from the official repo, clone `main` to `/opt/pricewatch`, `docker compose up -d --build`, Settings → Import.
  4. Docs out of date: `docs/superpowers/standards.md` (Fable trailer; "no browser on 403" contradicts KuantoKusta) and `orchestration.md` (work on a branch, contradicts stay-on-`main`); spec decision 11 likewise. Update or mark as historical.
- **Open questions / decisions pending:**
  - The user said "save the HTML and what not": built the HTML copy; a screen-sized JPEG screenshot on new lows (also attachable to ntfy) is offered on top, not confirmed.
  - Offered, not answered: a "Send to pricewatch" bookmarklet for stores that block scripts.
  - PCDiga, PowerPlanet and El Corte Inglés could be read through `Fetcher.browse` (a live test returned 200 with prices for all three). The user chose KuantoKusta instead; not wired up for those stores.
  - Parked final-review edges, not fixed: C1 wrapper bypass on stock rules; `NOISE_HINT` too broad (WooCommerce/Magento body classes → stock unknown); sold-out text anywhere → false "became unavailable" pushes. Listed in the merged PR's history.
- **User said:**
  - Commit after every fix or feature (CLAUDE.md). Don't run, start, stop or rebuild Docker unless told (CLAUDE.md).
  - Stay on the current branch (`main`); only branch when asked or clearly warranted, and say why first (also in auto memory).
  - "I give u the links. U check the price on the link every so often. U tell me if it's below my set threshold. That's it." No condition filters; several products may watch the same link.
  - Out-of-stock listings: record the price, show OUT OF STOCK clearly, never a deal alert.
  - Promo-code/coupon prices count as the price to pay.
  - Approved reading blocked stores with a real browser ("do it"); never solve CAPTCHAs.
  - Import must be immutable: importing the same file twice changes nothing.
  - Dashboard cards: the whole card opens the product; an at-all-time-low card says so.
- **Tried and dropped:**
  - Headless shell or default headless Chromium against Cloudflare/Akamai: blocked. Only full Chromium, current headless mode, automation flag off, normal UA works.
  - Darty's Shopify `.js` product JSON: 429 like the pages. KuantoKusta search: 403.
  - Worten `feature-price-with-coupon` cookie: the server still doesn't render the coupon block; the price is derived from the "-N% c/ cupão CODE" flag.
  - The generic microdata reader on Rádio Popular: it picks the "similar products" carousel; a header-based reader replaced it.
  - Wrapping the dashboard card in one `<a>`: it holds other links and a form (nested links are invalid); a stretched link on the product name is used instead.
- **Watch out for:**
  - Claude Code's permission classifier blocks anti-bot browser code unless the user explicitly authorises it in the conversation.
  - `docker compose down -v` does not clear `./data` (bind directory, owned by UID 10001).
  - `pkill -f` patterns can match the container's uvicorn; track scratch servers by PID.
  - Live probes: one request at a time. Bursts got Darty (429), FNAC and KuantoKusta (403) to block this IP.
  - Scratch Playwright checks: route-block non-local requests on every page and popup; clicking a store link or Check now on seeded data hits the real store (happened today: three single loads of `worten.pt/x`).
  - Saved store pages are third-party HTML: keep them behind the `Content-Security-Policy: sandbox` route (`app/web/products.py`, `saved_page`), never rendered inline.
  - Local Playwright Chromium (full + shell + ffmpeg) is in `~/.cache/ms-playwright`; the user installed its system libraries.
- **Relevant docs:**
  - `README.md`: "Checks, history and alerts" (All-time low badge, Saved pages), retailer coverage, "Move a watchlist between installations".
  - `docs/superpowers/standards.md`, `orchestration.md`: the out-of-date docs in next step 4.
  - `.claude/CLAUDE.md`: the user's working rules.

## Log

### 2026-10-09
- **Did:**
  - Dashboard cards open the product from anywhere (stretched link; store links and Check now stay on top; ↗ arrow removed).
  - "All-time low!" badge on a card when the best price equals the history low and the price has been higher before.
  - New all-time lows keep the fetched page HTML (gzipped, `PriceHistory.page`, deferred), linked from the card and the product page, served sandboxed with a `<base>` and a "Saved by pricewatch" banner. Verified in Playwright: clicks, badge, banner, planted script did not run.
- **Decided:**
  - A first reading is never an all-time low (badge or saved page), because it is trivially the lowest.
  - Pages are saved only on strict new lows and only from the HTML the check already fetched, because extra store requests risk throttling.
  - Pages live in SQLite with their history row (backups include them, export does not, listing removal deletes them), because there is no file cleanup to manage.
- **Commits:** `0bee919` feat: open a product from anywhere on its card; flag all-time lows; `279bece` feat: keep the store's page when a check finds a new all-time low; the user's `636bd76` worklog.
- **Left open:** screenshot on top of the HTML copy; container rebuild and CT move; out-of-date docs.

### 2026-10-07
- **Did:**
  - Wrote a link-aggregation spec and plan (`docs/superpowers/`) and executed all 11 tasks with Opus subagents plus per-task and final reviews. Fixed two Critical findings (stock rule on carousel badges; confirm-price bypassing the identity check) and four Important ones. The user merged it via PR #1.
  - Follow-ups on `main`:
    - removed alert conditions;
    - direct "Add link" on products, with a notice of what was read;
    - every store and price links out;
    - the same link can be watched by several products;
    - Remove button on listings;
    - Rádio Popular header reader, with promo-code prices;
    - Worten coupon prices;
    - promo prices drive alerts, best price and history;
    - interactive actions don't wait on 429; refused pasted links are kept as "not read yet";
    - 5-minute discovery timeout;
    - KuantoKusta provider (lowest price + shipping, via `Fetcher.browse`);
    - JSON export/import.
  - Answered CT sizing (2/2/16 is fine) and Docker install on Debian trixie (in Portuguese).
- **Decided:**
  - Darty, Rádio Popular and Amazon-sold offers default to `new`; generic stores too; the condition is information only.
  - KuantoKusta is read only through the browser (full Chromium in the image, no headless shell, ffmpeg removed).
  - Import adds only what is missing, keyed by product uid, link, history timestamp and rule host.
- **Commits:** `453b9ee`…`63ec136` (plan tasks) and `8903e49`…`0ba534d` (final-review fixes), merged in `83fd806`. Then `1177fa3`, `cf535c8`, `7c49a06`, `7fe8b5b`, `d6f650f`, `80e1ed8`, `666bc3f`, `f11ae39`, `38a2123`, `3be90b5`, `28b28c1`; the user's `ad1dc02`.
- **Left open:** container rebuild and move to the CT; the docs listed under Next steps; the parked review edges; the bookmarklet offer.

### 2026-10-06
- **Did:** audited the repo against the original spec; made live discovery work (HTTP/2; model→name→barcode search; Worten Outlet matching; "1.299 €" price parsing; Amazon parsing; out-of-stock handling; cooldowns that don't swallow pasted URLs; 429 backoff).
- **Decided:** first-party stores default to `new`; new-listing pushes are muted for confirmed listings.
- **Commits:** `066a341`, `ed9fcdb`, `6f6670a`, `a9b4b77`, `220482e`, `40e31de`; the user's `de3f560`, `7dab564`.
