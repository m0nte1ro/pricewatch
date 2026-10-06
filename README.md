# pricewatch

A self-hosted price monitor for a small homelab. Python 3.12+, FastAPI, SQLite, SQLAlchemy, Jinja2, HTMX, Chart.js and APScheduler. One application process; no queue server, Redis, or separate frontend build.

## What works

- Add a product by model, one URL, multiple URLs, or model + URLs. Manual URLs seed identity **and** augment independent retailer discovery.
- Background discovery with a persistent review screen; exact matches selected by default, uncertain matches unchecked, conflicting models rejected.
- Products separate from retailer listings, with seller and condition preserved. Exact model/brand/size and manufacturer identifiers drive matching.
- Product editing, pause/resume, individual listing controls, per-product/global checks, and removal to a recoverable archive.
- Scheduled checks, change-based history with daily samples, Chart.js series/ranges, threshold/availability/discovery events, read state, and durable ntfy delivery retries.
- Global/per-store polling, allowed conditions, request settings, proxy and optional browser fallback through the UI.
- Local frontend assets, SQLite migrations, structured logs, systemd deployment, online backups, and fixture-backed tests.

## Retailer coverage

Live checks were made on 2026-10-06. These are implementation/validation results, not a guarantee that a retailer will continue allowing automated requests.

| Retailer | Status | Coverage and limits |
|---|---|---|
| Worten PT | Supported | Live product JSON-LD, price, availability, seller, identity and public storefront search endpoint validated. Explicit outlet grades override generic `NewCondition`. A subsequent live httpx run returned 403 and correctly entered cooldown. Only concrete returned offers are monitored; this is not exhaustive marketplace-offer enumeration. |
| FNAC PT | Partial | Structured-data parser, URL IDs, search route and contract fixtures. A live request returned 403. It is reported and cooled down; no bypass. |
| Darty PT | Partial | Live Shopify product JSON-LD, price, model and stock validated. Public Shopify product search is also live-validated. Seller and condition can be unknown. This adapter is for Portugal, not darty.com. |
| Rádio Popular | Partial | Live price/stock microdata and product/search links validated. Seller/condition often missing. |
| Amazon ES | Partial | ASIN normalization, buy-box selectors, search links and contract fixtures. Live coverage is not validated; bot protection and changing markup can prevent checks. |

Missing conditions are **unknown**, never assumed new. Allow unknown in an individual product's settings only if you want its offers included in price alerts. Retailer settings expose capabilities and recent failures. A blocked adapter does not stop the others.

## Run locally

```bash
python3 --version  # 3.12 or newer
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m app.migrate
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers 1
```

Open `http://localhost:8080`. Add a product, review the discovery results, and confirm. Configure notifications and defaults under Settings. No account is required. The default local database is `data/pricewatch.db`.

Use **one worker and one application instance per database**. Its scheduler, task locks, and retailer rate limiter live inside that process. This is intentional for the target 1–2 CPU / 1–2 GB LXC. The application is designed for a trusted LAN; put authentication at your reverse proxy before exposing it elsewhere.

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
scripts/          Backup utility
tests/           Domain, integration, transport and web tests; HTML fixtures
```

The modular monolith keeps interfaces where there is a concrete reason: retailers return `Snapshot` objects, notification providers implement `send`, and services own application behavior. Routes do not scrape websites or evaluate alerts. Adapters do not write product history. `Runtime` composes dependencies, so tests replace external HTTP without changing business logic.

Network awaits happen outside database transactions. SQLite uses WAL, foreign keys, indexed lookups and a busy timeout. Monitoring uses three concurrent checks, bounded batches, a shared per-store rate limiter, and one active monitoring run. Discovery admits two simultaneous jobs. Notification delivery is serialized and retries from persisted events.

To grow: add adapters/categories without changing routes; add notification providers without changing alert evaluation; add migrations for new data. Moving to multiple workers would require a shared lease/job mechanism and rate limiter first. PostgreSQL and distributed workers are not claimed to work in this version.

## Identity, matching and deduplication

`services/matching.py` normalizes punctuation/case and compact TV models such as `85 C7K` → `85C7K`. It extracts brand, model and screen size, and preserves suffixes such as PRO. Retailer structured data supplies GTIN/MPN when available. The model parser is TV-oriented; unrecognized categories still work through manually confirmed listings.

- **EXACT**: matching model, with no conflicting brand, size or shared manufacturer identifier.
- **HIGH**: matching manufacturer identifier, with no contradictory attributes.
- **LOW**: title similarity only; explicit checkbox confirmation required.
- **CONFLICT**: a known attribute disagrees; cannot be saved under the same product.

Do not treat the numeric score as a calibrated probability. It explains the decision level. Variants with different model suffixes are deliberately kept distinct.

Offers are partitioned by retailer, seller and condition before deduplication. Within that partition, prefer retailer product ID, then normalized URL, then exact normalized title/model when authoritative IDs are absent. Marketing parameters and fragments are removed; offer/seller/variant parameters are retained. Database unique constraints are a final safeguard. Manual and discovered provenance merge into one row. Reconfirming a discovery draft is idempotent, and a matching existing product is reused.

A product without a detected model can still be saved. A URL-only submission that cannot be parsed asks the user to retry with a model. Conflicting manual URLs remain visible in the preview and are not silently merged. Name-only discovery depends on the stores' search results; a retired model can have an accessible product URL while no longer appearing in search.

## Checks, history and alerts

Default polling is 60 minutes, configurable globally or per retailer (minimum 5 minutes). Each listing persists its next due time with ±5% jitter. The scheduler wakes roughly every minute, coalesces missed runs, and does not overlap checks. Force checks retain request rate limits and cooldowns. Requests to a store are paced, have a configurable timeout and response-size cap, validate redirects, and retry transient transport/5xx errors up to three times with exponential backoff.

HTTP 403/429 and known challenge pages produce a one-hour retailer cooldown. A failure records an error and preserves history and last known price. A listing with a check error is excluded from the dashboard's best-price calculation until a successful check. If a page switches seller/condition or model, the original listing is retained and marked for review.

History is written when price, currency, stock or condition changes, or after 24 hours for sampling. Charts display each seller/condition separately, with selectable ranges and legend toggles. Long histories are sampled for the chart response; stored records are retained. Historical low/high/first values cover **all EUR conditions**, including disabled offers, and the UI labels this. Current best price considers only enabled, error-free, allowed-condition, EUR, in-stock offers. Threshold currency is EUR in this first version.

Events include price drops, target/insane threshold crossings, newly discovered new/outlet offers, and transitions into/out of stock. An initial qualifying offer may trigger a threshold event; an unchanged state does not repeat it. Crossing back above and later below a threshold may alert again. Stock returning can alert again. Changing a threshold alone does not synthesize a historical crossing. Disallowed conditions never generate monetary alerts.

Removing a product archives it and stops checks without deleting history. Disabling a listing also preserves its history. Discovery is initiated from Add Product or Find / add listings; periodic polling checks known offers and does not continuously enumerate new ones. Pending discoveries interrupted by a restart become retryable failures.

## Configuration and ntfy

Environment variables are prefixed `PRICEWATCH_`; see [.env.example](.env.example). Locally, `.env` is loaded if present. The systemd unit reads `/etc/pricewatch/pricewatch.env`.

| Variable | Default | Purpose |
|---|---|---|
| `PRICEWATCH_DATA_DIR` | `data` | SQLite directory |
| `PRICEWATCH_DATABASE_URL` | derived from data directory | Optional SQLite URL |
| `PRICEWATCH_SCHEDULER_ENABLED` | `true` | Disable only for tests/manual-only usage |
| `PRICEWATCH_LOG_LEVEL` | `INFO` | Structured log level |

Retailer settings, allowed conditions, intervals, HTTP settings, and ntfy are stored in SQLite and edited through the UI. Blank token/proxy fields preserve existing secrets; explicit clear checkboxes remove them. Secrets are not rendered into HTML or logged. Protect the database and backups because they include notification credentials.

For ntfy, set the server's root URL (for example `https://ntfy.sh` or your LAN ntfy instance), a topic, and optionally a bearer token. Subscribe to that topic using your ntfy client. Messages use ntfy's JSON publishing API; insane-deal events use priority 5. See the [ntfy publishing reference](https://docs.ntfy.sh/publish/).

Events are saved before notification delivery. Failed deliveries retry five times with increasing delays; failures and manual retry are visible in Activity. Delivery is at least once: a process crash after sending but before saving success can duplicate a notification. If ntfy is disabled, events remain in the UI with delivery state `skipped` and can be retried later.

## Debian LXC deployment

Use a Debian 13 LXC for the commands below; its Python 3.13 satisfies the application's requirement. Debian 12's default Python 3.11 does not. See [Debian's Python package](https://packages.debian.org/trixie/python3.13).

Run these commands **inside the LXC, from the checked-out repository**. They install the application under `/opt/pricewatch`, configuration under `/etc/pricewatch`, and data under `/var/lib/pricewatch`.

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv rsync sqlite3 ca-certificates
python3 -c 'import sys; assert sys.version_info >= (3, 12), "Python 3.12+ required"'
sudo useradd --system --user-group --home-dir /var/lib/pricewatch \
  --shell /usr/sbin/nologin pricewatch
sudo install -d -m 0755 /opt/pricewatch
sudo install -d -m 0750 -o root -g pricewatch /etc/pricewatch
sudo install -d -m 0700 -o pricewatch -g pricewatch /var/lib/pricewatch
sudo rsync -a --exclude='.git' --exclude='.venv' --exclude='data' \
  --exclude='.env' --exclude='.agents' --exclude='.codex' --exclude='.aws' \
  --exclude='__pycache__' --exclude='.pytest_cache' --exclude='.ruff_cache' \
  ./ /opt/pricewatch/
sudo chown -R root:root /opt/pricewatch
sudo python3 -m venv /opt/pricewatch/.venv
sudo /opt/pricewatch/.venv/bin/pip install -r /opt/pricewatch/requirements.lock
sudo /opt/pricewatch/.venv/bin/pip install --no-deps /opt/pricewatch
sudo install -m 0640 -o root -g pricewatch /opt/pricewatch/.env.example \
  /etc/pricewatch/pricewatch.env
sudo install -m 0644 /opt/pricewatch/deploy/pricewatch.service \
  /etc/systemd/system/pricewatch.service
sudo systemctl daemon-reload
sudo systemctl enable --now pricewatch
sudo systemctl status pricewatch --no-pager
curl --fail http://127.0.0.1:8080/health
```

The service runs migrations before starting, as the unprivileged `pricewatch` user. It binds `0.0.0.0:8080`. Open `http://LXC_IP:8080` on your LAN. The service has a read-only system filesystem except its state directory, private temporary directory and restrictive file permissions. If the user already exists, skip `useradd`.

```bash
sudo journalctl -u pricewatch -f
sudo systemctl restart pricewatch
```

Before upgrades, take a backup, stop the service, update application code/dependencies, and restart. Do not overwrite `/etc/pricewatch/pricewatch.env` or `/var/lib/pricewatch` on upgrades. Schema upgrades run automatically at service start. There is no Docker or Kubernetes requirement.

### Optional Playwright

Normal HTTP is preferred. Enable browser rendering only when a specific retailer needs it. Browser fallback is serialized, blocks off-domain requests, and stops on human-verification challenges. It uses more memory than HTTP parsing.

```bash
sudo /opt/pricewatch/.venv/bin/pip install '/opt/pricewatch[browser]'
sudo /opt/pricewatch/.venv/bin/python -m playwright install-deps chromium
sudo -u pricewatch env PLAYWRIGHT_BROWSERS_PATH=/var/lib/pricewatch/browsers \
  /opt/pricewatch/.venv/bin/python -m playwright install chromium
sudo systemctl restart pricewatch
```

Then enable the browser fallback in Settings. For local development use `.venv/bin/pip install -e '.[browser]'` and `.venv/bin/python -m playwright install chromium`. Browser installation details: [Playwright browser documentation](https://playwright.dev/python/docs/browsers).

## Back up and restore

Use SQLite's backup API while the service is running. Copying only the `.db` file while WAL is active can lose recent transactions.

```bash
sudo install -d -m 0700 -o pricewatch -g pricewatch /var/lib/pricewatch/backups
sudo -u pricewatch /opt/pricewatch/.venv/bin/python /opt/pricewatch/scripts/backup.py \
  /var/lib/pricewatch/pricewatch.db \
  /var/lib/pricewatch/backups/pricewatch-$(date +%Y%m%d-%H%M%S).db
```

Keep backups outside the LXC too. Store `/etc/pricewatch/pricewatch.env` securely with the database backup. To restore a chosen backup, stop the service first and move the current database plus its WAL/SHM files out of the way (keep them for rollback):

```bash
sudo systemctl stop pricewatch
pricewatch_restore_dir="/var/lib/pricewatch/pre-restore-$(date +%Y%m%d-%H%M%S)"
sudo install -d -m 0700 "$pricewatch_restore_dir"
for pricewatch_file in /var/lib/pricewatch/pricewatch.db /var/lib/pricewatch/pricewatch.db-wal /var/lib/pricewatch/pricewatch.db-shm; do
  if sudo test -f "$pricewatch_file"; then
    sudo mv "$pricewatch_file" "$pricewatch_restore_dir/"
  fi
done
sudo install -m 0600 -o pricewatch -g pricewatch /path/to/chosen-backup.db \
  /var/lib/pricewatch/pricewatch.db
sudo systemctl start pricewatch
```

## Extending the application

### Add a retailer

1. Create `app/retailers/example.py` extending `RetailerAdapter`. Set `name`, `label`, allowed `hosts`, the search route and product URL/ID pattern.
2. Override `parse` and/or `search_product` for the site's actual data. Keep networking in `Fetcher`; return typed snapshots or validated product URLs. Never create a zero price or assume a missing condition is new.
3. Register the class in `Registry`. It automatically appears in settings and discovery forms.
4. Save minimal public HTML/JSON fixtures, document whether they are live or synthetic, and test search, identity, stock, prices, sellers and conditions. Check that unrelated recommendation widgets do not become the main product.
5. Start with `status = "partial"` and an honest `status_note`. Validate live behavior before calling an adapter supported.

For another notification destination, implement the `NotificationProvider.send` contract and wire the provider factory through `NotificationService`. For richer categories, extend identity extraction and fixtures, keeping conflicting identifiers authoritative. Add schema changes with `alembic revision --autogenerate -m 'description'`, inspect the migration, and apply it with `python -m app.migrate`.

## Validation

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

The recorded live URL-only run found exact TCL 55P8L listings at Darty and Rádio Popular and merged Darty's manual/discovered sources. Worten returned 403 and was isolated. Retailer behavior varies by time and network; live probes are deliberately excluded from the deterministic suite.
