# pricewatch

A self-hosted price monitor for a small homelab. Python 3.12+, FastAPI, SQLite, SQLAlchemy, Jinja2, HTMX, Chart.js and APScheduler. One application process; no queue server, Redis, or separate frontend build.

## What works

- Add a product by model, one URL, multiple URLs, or model + URLs. Manual URLs seed identity **and** augment independent retailer discovery.
- Background discovery with a persistent review screen; exact matches selected by default, uncertain matches unchecked, conflicting models rejected.
- Products separate from retailer listings, with seller and condition preserved. Exact model/brand/size and manufacturer identifiers drive matching.
- Product editing, pause/resume, individual listing controls, per-product/global checks, and removal to a recoverable archive.
- Scheduled checks, change-based history with daily samples, Chart.js series/ranges, threshold/availability/discovery events, read state, and durable ntfy delivery retries.
- Global/per-store polling, allowed conditions, request settings, proxy and optional browser fallback through the UI.
- Local frontend assets, SQLite migrations, structured logs, Docker Compose deployment, online backups, and fixture-backed tests.

## Retailer coverage

Live checks were made on 2026-10-06. These are implementation/validation results, not a guarantee that a retailer will continue allowing automated requests.

| Retailer | Status | Coverage and limits |
|---|---|---|
| Worten PT | Supported | Live product JSON-LD, price, availability, seller, identity and public storefront search endpoint validated, including discovery of Worten Outlet Grade A offers. Explicit outlet grades override generic `NewCondition`. Worten rejects HTTP/1.1 from a browser user agent; requests use HTTP/2. Search queries the bare model number, which the storefront index returns consistently. Only concrete returned offers are monitored; this is not exhaustive marketplace-offer enumeration. |
| FNAC PT | Partial | Live search, product JSON-LD, price, stock, seller, condition and GTIN validated over HTTP/2. FNAC runs bot protection that can still answer 403; that is reported and cooled down, with no bypass. Other marketplace sellers on a product are not enumerated. |
| Darty PT | Partial | Live Shopify product JSON-LD, price, model and stock validated. Public Shopify product search is also live-validated. Darty sells its own new stock, so offers without outlet/refurbished markers are `new`; seller can be unknown. This adapter is for Portugal, not darty.com. |
| Rádio Popular | Partial | Live price/stock microdata and product/search links validated. It sells its own new stock, so offers without outlet/refurbished markers are `new`; seller is not exposed. |
| Amazon ES | Partial | Live search, buy-box price, seller and stock validated. Search-result links are reduced to `/dp/ASIN` (plus `smid`). Offers sold by Amazon are `new`; third-party sellers stay `unknown` unless the page says new/used/renewed. Bot protection and changing markup can prevent checks. |

Condition comes from page evidence first: outlet grades, open box, refurbished/reacondicionado/renewed and used markers always win. Without evidence, an adapter may declare a default only when the store sells exclusively its own new stock (Darty, Rádio Popular, and offers sold by Amazon itself). Otherwise the condition is **unknown**, never assumed new. Allow unknown in an individual product's settings only if you want those offers included in price alerts. Retailer settings expose capabilities and recent failures. A blocked adapter does not stop the others.

## Quick start: Docker Compose

The host needs Docker Engine and Docker Compose v2. Python, pip, Chromium, Playwright, migration tooling, and the web server are included in the image. No `.env` file or manual database command is required.

```bash
git clone https://github.com/m0nte1ro/pricewatch.git
cd pricewatch
docker compose up -d --build
```

Open **http://SERVER_IP:8080**. On first start Compose creates `./data`, the entrypoint prepares its permissions, runs migrations, and starts the scheduler and web UI. The first image build downloads Chromium and its system libraries; later builds reuse cached dependency layers.

The entrypoint briefly runs as root to handle a newly created bind directory, then drops to the dedicated `pricewatch` user (UID/GID 10001) before migrations and the application. The application image is read-only at runtime; writable locations are the data mount and temporary browser directories. One container exposes only port 8080. It uses Docker's `unless-stopped` restart policy and does not require an application systemd service, host networking, privileged mode, or a Docker socket mount.

Use **one worker and one application instance per database**. The scheduler, task locks, and retailer rate limiter live inside this process. The app assumes a trusted LAN; no signup is required.

## Operate, inspect health, and upgrade

```bash
docker compose ps
docker compose logs -f pricewatch
docker compose restart
docker compose down
docker compose up -d
```

`docker compose ps` shows container health. The healthcheck calls `/health`, which checks the application and database without contacting a retailer. To inspect its latest probe results:

```bash
docker inspect --format '{{json .State.Health}}' "$(docker compose ps -q pricewatch)"
```

Upgrade from the repository directory:

```bash
git pull
docker compose up -d --build
```

The replacement container uses the same database, history, settings, and alert records. Migrations run automatically before each server start. Migration failure prevents the server from starting; inspect `docker compose logs pricewatch` before trying again. Take a backup before upgrading. Configuration edited through the Web UI is stored in SQLite and survives upgrades.

## Persistence, backup, and restore

Compose bind-mounts **`./data:/var/lib/pricewatch`**. The database is `./data/pricewatch.db` on the host and `/var/lib/pricewatch/pricewatch.db` in the container. Its SQLite WAL/SHM files and any backups are also inside this data directory. SQLite already uses WAL mode, a 30-second busy timeout, and foreign keys.

Restarting, stopping, running `docker compose down`, replacing a container, or rebuilding the image does **not** remove this bind directory. **Deleting `./data` deletes your persistent application state.** Do not change the bind source during an upgrade. The directory and database are owned by container UID/GID 10001 with restrictive permissions; use container commands or an appropriate host administrator account to access them.

Create a consistent backup while the app is running using the existing SQLite backup API:

```bash
docker compose exec --user pricewatch pricewatch mkdir -p /var/lib/pricewatch/backups
docker compose exec --user pricewatch pricewatch python scripts/backup.py \
  /var/lib/pricewatch/pricewatch.db \
  "/var/lib/pricewatch/backups/pricewatch-$(date +%Y%m%d-%H%M%S).db"
```

The result is visible under `./data/backups`. Keep a copy outside the deployment machine. The database contains settings and optional notification credentials. Copying only a live `.db` file can miss transactions still in WAL, so use this backup command or stop the app before copying.

To restore, stop the app and use the same image to move the current database and WAL/SHM files aside before copying the chosen backup. Replace the example backup name with one created above:

```bash
docker compose down
docker compose run --rm --no-deps --entrypoint sh pricewatch -ec '
  test -f /var/lib/pricewatch/backups/CHOSEN-BACKUP.db
  saved="/var/lib/pricewatch/pre-restore-$(date +%Y%m%d-%H%M%S)"
  mkdir -m 700 "$saved"
  for file in /var/lib/pricewatch/pricewatch.db /var/lib/pricewatch/pricewatch.db-wal /var/lib/pricewatch/pricewatch.db-shm; do
    if [ -f "$file" ]; then mv "$file" "$saved/"; fi
  done
  cp /var/lib/pricewatch/backups/CHOSEN-BACKUP.db /var/lib/pricewatch/pricewatch.db
  chown pricewatch:pricewatch /var/lib/pricewatch/pricewatch.db
  chmod 600 /var/lib/pricewatch/pricewatch.db
'
docker compose up -d
```

The old files are retained in `pre-restore-*` for rollback. Migrations run against the restored database on startup. A backup from a newer schema may require its matching application version.

## Browser fallback

The standard image includes pinned Playwright and its matching **Chromium headless shell**, including Linux libraries. No browser software is needed on the host. Other browser engines are omitted to keep the image smaller.

HTTP remains the default. Enable **browser fallback** in the Web UI's Settings only when a retailer requires JavaScript. The existing fallback is serialized, blocks off-domain requests, and stops on CAPTCHA/human-verification challenges. Installing Chromium does not bypass blocked retailers or make partial adapters fully supported. Browser binaries belong to the image, while durable application state belongs to `./data`.

## Troubleshooting

- **Port 8080 already in use:** stop the other listener or change only the host side of the port mapping in a local Compose override.
- **Unhealthy or restarting:** inspect `docker compose logs pricewatch` and the healthcheck output above. Database/migration failures are fatal rather than ignored.
- **Bind directory not writable:** the default entrypoint fixes permissions. On NFS/root-squash or with a custom Compose `user`, ensure the data directory is writable by UID/GID 10001. Do not delete it to solve a permission error.
- **Browser launch fails:** rebuild the image and inspect logs. Playwright and its browser revision are installed together; the container does not use host browser installations.
- **Retailer returns 403/429:** a 403 pauses that store for an hour; a 429 (rate limit) is retried with backoff and, if it persists, pauses the store for 10 minutes. Other stores continue. Discovery, Check now and restarts retry it once. See Retailer coverage below.
- **LXC deployment:** install Docker/Compose in an LXC configured to support containers. The application itself needs no additional host Python/browser packages.

## Native development and legacy deployment

Developers can still run the existing Python workflow:

```bash
python3 -m venv .venv  # Python 3.12+
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m app.migrate
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers 1
```

This local development mode defaults to `data/pricewatch.db`. Existing native Debian/systemd installations can use the [alternative deployment guide](deploy/README.systemd.md); systemd is not part of the recommended Docker installation.

## Architecture

```text
app/
  web/            Thin FastAPI routes, forms, CSRF validation and rendering
  schemas/        Typed identities, snapshots, match results and preferences
  models/         SQLAlchemy persistence model
  services/       Use cases: discovery, matching, monitoring, alerts, queries
  retailers/      Isolated storefront adapters, registry, parsing and HTTP I/O
  notifications/  Provider contract and ntfy implementation
  templates/      Server-rendered pages and HTMX fragments
  static/         Styles, small scripts, vendored HTMX and Chart.js
  runtime.py      Dependency wiring, bounded jobs and scheduler lifecycle
  database.py     Sessions and SQLite WAL / foreign key configuration
migrations/       Versioned Alembic schema
deploy/           Container entrypoint and legacy systemd alternative
scripts/          Backup utility
tests/           Domain, integration, transport and web tests; HTML fixtures
```

The modular monolith keeps interfaces where there is a concrete reason: retailers return `Snapshot` objects, notification providers implement `send`, and services own application behavior. Routes do not scrape websites or evaluate alerts. Adapters do not write product history. `Runtime` composes dependencies, so tests replace external HTTP without changing business logic.

Network awaits happen outside database transactions. SQLite uses WAL, foreign keys, indexed lookups and a busy timeout. Monitoring uses three concurrent checks, bounded batches, a shared per-store rate limiter, and one active monitoring run. Discovery admits two simultaneous jobs. Notification delivery is serialized and retries from persisted events.

To grow: add adapters/categories without changing routes; add notification providers without changing alert evaluation; add migrations for new data. Moving to multiple workers would require a shared lease/job mechanism and rate limiter first. PostgreSQL and distributed workers are not claimed to work in this version.

## Identity, matching and deduplication

`services/matching.py` normalizes punctuation/case and compact TV models such as `85 C7K` → `85C7K`. It extracts brand, model and screen size, and preserves suffixes such as PRO. Retailer structured data supplies GTIN/MPN when available. The model parser is TV-oriented; unrecognized categories still work through manually confirmed listings.

- **EXACT**: matching model, with no conflicting brand, size or shared manufacturer identifier.
- **HIGH**: matching manufacturer identifier with no contradictory attributes, or an exact model whose outlet/refurbished/used offer carries the retailer's own barcode (Worten relabels outlet stock).
- **LOW**: explicit checkbox confirmation required. Used for title similarity only, an exact model on a *new* offer with a different barcode, or a family model embedded in a regional code after the screen size (`QN90D` in `TQ65QN90DATXXC`).
- **CONFLICT**: a known attribute disagrees; cannot be saved under the same product.

Do not treat the numeric score as a calibrated probability. It explains the decision level. Variants with different model suffixes (`85C7K` / `85C7K PRO`) remain CONFLICT. An outlet-only manual URL never supplies the product's manufacturer identifiers.

Offers are partitioned by retailer, seller and condition before deduplication. Within that partition, prefer retailer product ID, then normalized URL, then exact normalized title/model when authoritative IDs are absent. Marketing parameters and fragments are removed; offer/seller/variant parameters are retained. Database unique constraints are a final safeguard. Manual and discovered provenance merge into one row. Reconfirming a discovery draft is idempotent, and a matching existing product is reused.

A product without a detected model can still be saved. A URL-only submission that cannot be parsed asks the user to retry with a model. Conflicting manual URLs remain visible in the preview and are not silently merged. Discovery searches each store by the bare model number first, then the full name, then the product barcode (GTIN/EAN), stopping at the first query with matching links. Store searches rank in-stock products first, so brand+model queries can hide an out-of-stock or retired listing that the model number still finds. FNAC's search omits unavailable products entirely; paste FNAC links for those. Barcodes are normalized to one `gtin` form across stores.

## Checks, history and alerts

Default polling is 60 minutes, configurable globally or per retailer (minimum 5 minutes). Each listing persists its next due time with ±5% jitter. The scheduler wakes roughly every minute, coalesces missed runs, and does not overlap checks. Force checks retain request rate limits. Requests use HTTP/2 with standard browser `Accept` headers. Requests to a store are paced, have a configurable timeout and response-size cap, validate redirects, and retry transient transport/5xx errors up to three times with exponential backoff.

HTTP 403 and known challenge pages produce a one-hour retailer cooldown that scheduled checks honour. HTTP 429 means "slow down", not "blocked": the request waits for `Retry-After` (or 10 s, then 30 s, never more than a minute) and retries; only a persistent 429 pauses the store, for 10 minutes. Stores that throttle harder get wider request spacing (Darty: 5 s). User actions (Add product / Find listings, Check now) and an application restart retry a cooled-down store once; if it still blocks, the cooldown is re-armed after that single request. A URL you paste is therefore always fetched. A failure records an error and preserves history and last known price. A listing with a check error is excluded from the dashboard's best-price calculation until a successful check. If a page switches seller/condition or model, the original listing is retained and marked for review.

History is written when price, currency, stock or condition changes, or after 24 hours for sampling. Charts display each seller/condition separately, with selectable ranges and legend toggles. Long histories are sampled for the chart response; stored records are retained. Historical low/high/first values cover **all EUR conditions**, including disabled offers, and the UI labels this. Current best price considers only enabled, error-free, allowed-condition, EUR, in-stock offers. Threshold currency is EUR in this first version.

Events include price drops, target/insane threshold crossings, newly discovered new/outlet offers, and transitions into/out of stock. New/outlet listing events for offers you just confirmed are recorded in Activity without a push notification; price, threshold and stock events are pushed. Delivery drains every due event, including ones saved while a previous delivery is still sending. An initial qualifying offer may trigger a threshold event; an unchanged state does not repeat it. Crossing back above and later below a threshold may alert again. Stock returning can alert again. Changing a threshold alone does not synthesize a historical crossing. Disallowed conditions never generate monetary alerts.

Removing a product archives it and stops checks without deleting history. Disabling a listing also preserves its history. Discovery is initiated from Add Product or Find / add listings; periodic polling checks known offers and does not continuously enumerate new ones. Pending discoveries interrupted by a restart become retryable failures.

## Configuration and ntfy

All retailer, polling, condition and ntfy settings can be configured through the Web UI. Environment variables are prefixed `PRICEWATCH_`; see [.env.example](.env.example). Compose passes optional logging/scheduler overrides from `.env` if present; copying that file is optional. It fixes the data directory at `/var/lib/pricewatch`. Other application environment overrides can be added explicitly in a local Compose override. Native development still loads `.env` directly.

| Variable | Default | Purpose |
|---|---|---|
| `PRICEWATCH_DATA_DIR` | `/var/lib/pricewatch` in Docker; `data` natively | SQLite directory; keep the Compose data mount |
| `PRICEWATCH_DATABASE_URL` | derived from data directory | Optional SQLite URL |
| `PRICEWATCH_SCHEDULER_ENABLED` | `true` | Disable only for tests/manual-only usage |
| `PRICEWATCH_LOG_LEVEL` | `INFO` | Structured log level |

Retailer settings, allowed conditions, intervals, HTTP settings, and ntfy are stored in SQLite and edited through the UI. Blank token/proxy fields preserve existing secrets; explicit clear checkboxes remove them. Secrets are not rendered into HTML or logged. Protect the database and backups because they include notification credentials.

For ntfy, set the server's root URL (for example `https://ntfy.sh` or your LAN ntfy instance), a topic, and optionally a bearer token. Subscribe to that topic using your ntfy client. Messages use ntfy's JSON publishing API; insane-deal events use priority 5. See the [ntfy publishing reference](https://docs.ntfy.sh/publish/).

Events are saved before notification delivery. Failed deliveries retry five times with increasing delays; failures and manual retry are visible in Activity. Delivery is at least once: a process crash after sending but before saving success can duplicate a notification. If ntfy is disabled, events remain in the UI with delivery state `skipped` and can be retried later.

## Extending the application

### Add a retailer

1. Create `app/retailers/example.py` extending `RetailerAdapter`. Set `name`, `label`, allowed `hosts`, the search route and product URL/ID pattern.
2. Override `parse` and/or `search_product` for the site's actual data. Keep networking in `Fetcher`; return typed snapshots or validated product URLs. Never create a zero price or assume a missing condition is new.
3. Register the class in `Registry`. It automatically appears in settings and discovery forms.
4. Save minimal public HTML/JSON fixtures, document whether they are live or synthetic, and test search, identity, stock, prices, sellers and conditions. Check that unrelated recommendation widgets do not become the main product.
5. Start with `status = "partial"` and an honest `status_note`. Validate live behavior before calling an adapter supported.

For another notification destination, implement the `NotificationProvider.send` contract and wire the provider factory through `NotificationService`. For richer categories, extend identity extraction and fixtures, keeping conflicting identifiers authoritative. Add schema changes with `alembic revision --autogenerate -m 'description'`, inspect the migration, and apply it with `python -m app.migrate`.

## Validation

Check the deployment using only Docker:

```bash
docker compose build --no-cache --pull
docker compose up -d --build
docker compose ps
docker compose exec --user pricewatch pricewatch python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/health').read().decode())"
```

The following commands are for contributors running the Python test suite; they are not deployment prerequisites. Container startup tests check migration ordering and ensure a migration error prevents server startup.

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
.venv/bin/ruff check app tests scripts migrations
.venv/bin/ruff format --check app tests scripts migrations
.venv/bin/alembic check
```

Tests replace network calls with `httpx.MockTransport`; they do not send real notifications or contact retailers. Fixture tests cover live reduced Worten, Darty and Rádio Popular HTML/JSON plus synthetic outlet, FNAC and Amazon cases. A real APScheduler execution test verifies scheduled history writes and threshold alerts. Migration round-trip and WAL-aware backup tests cover storage. Integration tests exercise the same monitoring coroutine used by APScheduler and the actual web routes, including URL-only creation, multiple stores, duplicate merging, settings, editing, archiving, charts, CSRF, and delivery retries. Fixture provenance is in `tests/fixtures/README.md`.

Known limits: no user accounts; single worker; EUR thresholds; no shipping/tax normalization; TV-focused model extraction; bounded search results rather than a complete retailer crawl; unknown condition/seller on partial adapters; no CAPTCHA bypass; no automatic offer migration when a retailer changes seller/condition; no full marketplace inventory enumeration. All fetched data is an observation and may differ from the final checkout price.

### Browser and live smoke checks

The optional browser check uses an isolated temporary database and deterministic retailer responses:

```bash
# Terminal 1: normal app, initialized as above
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8080
# Terminal 2: isolated test server (requires dev + browser extras)
.venv/bin/uvicorn tests.browser_server:app --host 127.0.0.1 --port 8081
# Terminal 3: browser workflow; saves screenshots under /tmp/pricewatch-browser-smoke
.venv/bin/python -m tests.browser_smoke
```

Start the isolated server with a fresh `/tmp/pricewatch-browser-smoke` database for an independent run. It checks the real dashboard, URL-only add flow, dynamic URL fields, duplicate merging, five retailer listings, chart range/series controls, forced price-drop alerts, activity/settings pages, and a 390px mobile viewport. Browser console errors fail the check.

For an **opt-in live** discovery probe using a temporary database (no notifications):

```bash
.venv/bin/python -m tests.live_smoke
```

Recorded live runs (2026-10-06): a Worten-only TCL 85C7L URL found exact listings at all five retailers and merged the manual and discovered Worten sources. A Worten TCL 85C7K URL discovered the Worten Outlet Grade A offer (€783.57) and, with an €800 insane-deal threshold, pushed TARGET HIT and an urgent INSANE DEAL notification. Retailer behavior varies by time and network; live probes are deliberately excluded from the deterministic suite.
