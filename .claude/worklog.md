# Work log: pricewatch

## Where things stand
_Updated 2026-10-09_

- **Branch / state:** `main`, in sync with `origin/main` (head `ad1dc02`), clean working tree. `link-aggregation` was merged via PR #1; the local branch still exists (fully merged, safe to delete). 274 tests pass, ruff and `alembic check` clean.
- **Next steps:**
  1. The user rebuilds the local container (`docker compose up -d --build`); it is several commits behind and the migrations (`95b1e4bc7bfc`, `136c811fa067`, `9c429652d1ff`, `cfa384c596df`) apply on start.
  2. Local Settings → Export watchlist → move the file to the CT.
  3. CT (Debian trixie, Proxmox, 2 vCPU / 2 GB / 16 GB, nesting + keyctl on): install Docker from the official repo, clone `main` to `/opt/pricewatch`, `docker compose up -d --build`, Settings → Import.
  4. Docs out of date: `docs/superpowers/standards.md` and `orchestration.md` still describe the branch-per-plan workflow and a Fable trailer; the spec still mentions condition-gated alerts (removed). Update or mark as historical.
- **Open questions / decisions pending:**
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
- **Tried and dropped:**
  - Headless shell or default headless Chromium against Cloudflare/Akamai: blocked. Only full Chromium, current headless mode, automation flag off, normal UA works.
  - Darty's Shopify `.js` product JSON: 429 like the pages. KuantoKusta search: 403.
  - Worten `feature-price-with-coupon` cookie: the server still doesn't render the coupon block; the price is derived from the "-N% c/ cupão CODE" flag.
  - The generic microdata reader on Rádio Popular: it picks the "similar products" carousel; a header-based reader replaced it.
- **Watch out for:**
  - Claude Code's permission classifier blocks anti-bot browser code unless the user explicitly authorises it in the conversation.
  - `docker compose down -v` does not clear `./data` (bind directory, owned by UID 10001).
  - `pkill -f` patterns can match the container's uvicorn; track scratch servers by PID.
  - Live probes: one request at a time. Bursts got Darty (429), FNAC and KuantoKusta (403) to block this IP.
  - Local Playwright Chromium (full + shell + ffmpeg) is in `~/.cache/ms-playwright`; the user installed its system libraries.
- **Relevant docs:**
  - `README.md`: retailer coverage table (Rádio Popular, KuantoKusta, Worten coupons), "Move a watchlist between installations", promo prices, 429/"not read yet" behaviour.
  - `.claude/CLAUDE.md`: the user's working rules.
  - `tests/fixtures/README.md`: provenance of the live fixtures added this week.

## Log

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
