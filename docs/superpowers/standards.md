# pricewatch — rules and standards for implementers and reviewers

Read this before touching the code. It covers what the plan and the spec do not: how this
repository is written, tested and committed, and the things that have already gone wrong once.
`.claude/CLAUDE.md` holds the owner's own rules and wins over anything here.

## Non-negotiable

- **Never run, start, stop or rebuild Docker.** The owner's container runs on port 8080 and is
  expensive to rebuild. When a change needs a rebuild, say so in your report; do not do it.
- **Never contact real retailers from tests or from scripts you run while implementing.** Every
  network call in tests goes through `httpx.MockTransport` in `tests/conftest.py`. If you must
  probe a live store to understand its HTML, make **one** request, by hand, and stop. Bursts of
  scripted requests got this IP throttled by Darty (HTTP 429) and blocked by FNAC (HTTP 403)
  for more than a day.
- **No bypassing anti-bot measures.** A 403 or a CAPTCHA page means "mark the store unavailable
  and move on", never "try harder". No Playwright-on-403, no header spoofing beyond the
  existing browser-like `User-Agent`/`Accept`/HTTP/2 setup in `app/retailers/http.py`.
- **Commit at the end of every task**, nothing left uncommitted. Message: `feat:` / `fix:` /
  `docs:` / `test:` prefix, subject under 72 characters, a body that says *why*, and the last
  line `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` (or the attribution line the
  session reminder gives you, if it differs). Never commit `data/`, `.env`, `.superpowers/`, or
  `build/`.
- **No new runtime dependencies.** The stack is FastAPI, SQLAlchemy 2, Alembic, Jinja2, HTMX,
  Chart.js (vendored), httpx (with h2), BeautifulSoup, APScheduler, pydantic-settings. No CDNs:
  front-end assets live in `app/static/vendor/`.
- **Local data directory.** `./data` is owned by the container user (UID 10001); anything that
  opens the database locally fails with "unable to open database file". Run local commands
  with `PRICEWATCH_DATA_DIR=/tmp/pw-dev` (or another scratch directory). For Alembic, copy
  `alembic.ini` to a scratch file with the `sqlalchemy.url` pointed at that directory (the
  plan's Task 1 shows the exact commands).
- **Don't start servers on port 8080** (the container is there). Manual runs use
  `--port 8090`. Record the PID of anything you start and kill it by PID; `pkill -f` with a
  broad pattern once matched the container's own uvicorn.

## Code

- **Match the house style.** Code is compact and explicit: type hints everywhere, short
  functions, no abstractions without a second real use, no `# TODO`. Comments are rare and say
  *why*, never *what*; a comment that restates the next line is a defect.
- **Layering** (from the README's Architecture section): `web/` routes are thin — form parsing,
  validation, redirect; they never scrape or evaluate alerts. `services/` own behaviour.
  `retailers/` adapters return `Snapshot` objects and never write to the database. Network
  awaits happen **outside** database transactions.
- **Database access** is always `with self.db.session() as session:` (commits on exit,
  `expire_on_commit=False`). Objects returned from a closed session are detached: re-fetch
  with `session.get(...)` before mutating in a new session.
- **Money is `Decimal`**, parsed only through `money()` in `app/retailers/parsing.py`;
  quantised to cents; `0` and negatives are "no price". Thresholds and best-price are EUR only.
- **Time** is naive UTC from `now()` in `app/schemas/domain.py`. Never `datetime.now()`.
- **Errors shown to users** are `ScrapeError` messages: short, no URLs, no credentials, no
  stack traces. `BlockedError` means a one-hour cooldown; `RateLimitedError` means backoff and
  a ten-minute pause. Anything else is logged and summarised as "adapter failed".
- **Logging** is structured: `log.info("event_name", extra={...})`. `app/logging.py` only
  emits the `extra` keys it lists (`retailer`, `status`, `product_id`, `listing_id`,
  `event_type`, `count`, `confidence`, `error_type`, `alert_id`); add a key there if you need
  a new one. Never log secrets, tokens or full URLs with query strings.
- **Security conventions**: every POST route has `dependencies=[Depends(protected)]` and every
  form carries `<input type="hidden" name="csrf" value="{{ csrf }}">`. Every outbound URL goes
  through `Fetcher.validate_url` (https only, host allow-list, no userinfo, no odd ports) and,
  for generic stores, `public_host`. Secrets (ntfy token, proxy) are never rendered into HTML.
- **Templates** are dense single-line Jinja in `app/templates/`; HTMX only for polling and
  small swaps; forms work without JavaScript. Reuse the existing badge classes
  (`badge`, `low`, `supported`, `partial`, `conflict`, `out-of-stock`) and the `money` /
  `date` filters. CSS lives in the single-line `app/static/app.css`; add rules at the end of
  the relevant group, use the existing tokens (`--accent`, `--warning`, `--muted`, `--line`).
- **User-facing copy** is short and plain. Where the spec or plan fixes exact text (badge
  labels, error messages), use it verbatim — tests assert on it.
- **Lint before you finish:** `.venv/bin/ruff check app tests scripts migrations` and
  `.venv/bin/ruff format app tests scripts migrations` (line length 100; `E501` is ignored on
  purpose). Both must be clean.

## Tests

- Framework: pytest, `asyncio_mode = "auto"` (async tests need no decorator). Run with
  `.venv/bin/pytest -q`; the whole suite takes about two seconds and must stay fast — no sleeps
  longer than a few milliseconds, no real backoff waits (use `Retry-After: 0` or
  `min_delay=0`).
- Fixtures: `db` (in-memory SQLite with the full schema), `candidate`/`snapshot` (a Worten
  TCL 85C7K listing), `site` (TestClient + runtime + `MockTransport`, yields
  `client, runtime, prices, requests`). Web tests use the `discover()` and `form_data()`
  helpers in `tests/test_web.py`.
- **Test names state the behaviour**, e.g. `test_pasted_url_is_fetched_even_while_its_store_is_cooling_down`.
  One behaviour per test; assert exact values from the plan, not just "truthy".
- **TDD**: write the failing test first, run it and see it fail for the right reason, then
  implement, then run the whole suite. A test that passes before the implementation is
  testing nothing.
- **Regression tests for bugs** must fail without the fix; say so in your report.
- HTML fixtures live in `tests/fixtures/`. Synthetic fixtures are fine for parser branches.
  A fixture reduced from a live page must be minimal (title, JSON-LD, the few elements under
  test) and recorded in `tests/fixtures/README.md` with the retrieval date and what was kept.
  Never save cookies, account data or full pages.
- Never change an existing assertion to make a test pass unless the plan says the behaviour
  changed; explain every such change in the report.

## Migrations

- One Alembic revision per piece of work, generated with `revision --autogenerate` against a
  scratch database at head, then **read and corrected by hand**. Non-nullable new columns
  need a `server_default`. `downgrade()` must fully reverse `upgrade()`;
  `tests/test_storage.py` round-trips it. SQLite needs `batch_alter_table` for column changes
  (`render_as_batch=True` is already set in `migrations/env.py`).

## Reports

- Write the full report to the report file the orchestrator names. Include: what you built,
  the test commands you ran and their last line of output, any plan text you deviated from
  and why, and anything you noticed but did not fix.
- Status is one of `DONE`, `DONE_WITH_CONCERNS`, `NEEDS_CONTEXT`, `BLOCKED`. Do not guess
  through an ambiguity in the brief: ask (`NEEDS_CONTEXT`) or state the choice you made in
  the report. Do not spawn subagents of your own; review comes from the orchestrator.
