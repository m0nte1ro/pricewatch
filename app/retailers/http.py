import asyncio
import logging
import time
from datetime import timedelta
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select

from app.models import RetailerState
from app.retailers.parsing import BlockedError, RateLimitedError, ScrapeError
from app.schemas.domain import Preferences, now

log = logging.getLogger(__name__)


def retry_after(value: str | None) -> float | None:
    try:
        return max(0.0, float(value)) if value else None
    except ValueError:
        return None  # HTTP-date form: fall back to our own backoff


class Fetcher:
    def __init__(self, db, get_preferences, *, transport=None, min_delay: float = 2):
        self.db = db
        self.get_preferences = get_preferences
        self.transport = transport
        self.min_delay = min_delay
        # Per-store spacing for stores that throttle harder than the default (set by Registry).
        self.intervals: dict[str, float] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.last_request: dict[str, float] = {}
        self.browser_lock = asyncio.Lock()

    def state(self, retailer: str, error: str | None = None, cooldown: timedelta | None = None):
        with self.db.session() as session:
            row = session.get(RetailerState, retailer)
            if row is None:
                row = RetailerState(name=retailer)
                session.add(row)
            if error:
                row.last_failure_at, row.last_error = now(), error
                if cooldown:
                    row.blocked_until = now() + cooldown
            else:
                row.last_success_at, row.last_error, row.blocked_until = now(), None, None

    def retry_now(self, retailers=None):
        """Lift cooldowns so a user action or restart tries a store once more.

        One blocked request re-arms the cooldown, so this costs at most one request per store.
        """
        with self.db.session() as session:
            query = select(RetailerState).where(RetailerState.blocked_until.is_not(None))
            if retailers is not None:
                query = query.where(RetailerState.name.in_(list(retailers)))
            for row in session.scalars(query):
                row.blocked_until = None
                log.info("retailer_cooldown_lifted", extra={"retailer": row.name})

    @staticmethod
    def validate_url(url: str, hosts: tuple[str, ...]):
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in hosts
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise ScrapeError("Only HTTPS URLs on this retailer's supported domains are allowed")

    async def get(
        self, url: str, retailer: str, hosts: tuple[str, ...], *, json_body: dict | None = None
    ) -> str:
        self.validate_url(url, hosts)
        prefs: Preferences = self.get_preferences()
        async with self.locks.setdefault(retailer, asyncio.Lock()):
            with self.db.session() as session:
                state = session.get(RetailerState, retailer)
                if state and state.blocked_until and state.blocked_until > now():
                    raise BlockedError("Retailer temporarily unavailable; retrying after cooldown")
            # Some stores reject HTTP/1.1 from a browser user agent; negotiate HTTP/2 like one.
            async with httpx.AsyncClient(
                timeout=prefs.request_timeout,
                proxy=prefs.proxy or None,
                transport=self.transport,
                trust_env=False,
                http2=True,
                headers={
                    "User-Agent": prefs.user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                    "Accept-Language": "pt-PT,pt;q=0.9,es;q=0.8,en;q=0.7",
                },
            ) as client:
                spacing = max(self.min_delay, self.intervals.get(retailer, 0))
                for attempt in range(3):
                    await asyncio.sleep(
                        max(0, spacing - (time.monotonic() - self.last_request.get(retailer, 0)))
                    )
                    self.last_request[retailer] = time.monotonic()
                    try:
                        current = url
                        for _ in range(6):
                            self.validate_url(current, hosts)
                            async with client.stream(
                                "POST" if json_body is not None else "GET", current, json=json_body
                            ) as response:
                                if response.status_code in (301, 302, 303, 307, 308):
                                    current = str(
                                        response.url.join(response.headers.get("location", ""))
                                    )
                                    continue
                                if response.status_code == 429:
                                    raise RateLimitedError(
                                        "Retailer is rate limiting requests (HTTP 429); pausing it for 10 minutes",
                                        retry_after(response.headers.get("retry-after")),
                                    )
                                if response.status_code == 403:
                                    raise BlockedError(
                                        "Retailer returned HTTP 403; cooling down for one hour"
                                    )
                                response.raise_for_status()
                                chunks, length = [], 0
                                async for chunk in response.aiter_bytes():
                                    length += len(chunk)
                                    if length > 6_000_000:
                                        raise ScrapeError("Retailer response exceeds size limit")
                                    chunks.append(chunk)
                                html = b"".join(chunks).decode(
                                    response.encoding or "utf-8", errors="replace"
                                )
                                if any(
                                    x in html.lower()
                                    for x in (
                                        "captcha-delivery.com",
                                        "validatecaptcha",
                                        "cf-chl-",
                                        "verify you are human",
                                    )
                                ):
                                    raise BlockedError(
                                        "Retailer requested human verification; no bypass attempted"
                                    )
                                self.state(retailer)
                                log.info(
                                    "retailer_request",
                                    extra={"retailer": retailer, "status": response.status_code},
                                )
                                return html
                        raise ScrapeError("Too many retailer redirects")
                    except BlockedError as exc:
                        if isinstance(exc, RateLimitedError) and attempt < 2:
                            # Slow down and retry this request instead of dropping the store.
                            wait = (
                                exc.retry_after if exc.retry_after is not None else 10 * 3**attempt
                            )
                            if wait <= 60:
                                log.info(
                                    "retailer_rate_limited",
                                    extra={"retailer": retailer, "status": 429},
                                )
                                await asyncio.sleep(wait)
                                continue
                        self.state(retailer, str(exc), cooldown=exc.cooldown)
                        raise
                    except (httpx.HTTPError, ScrapeError) as exc:
                        retryable = isinstance(exc, httpx.TransportError) or (
                            isinstance(exc, httpx.HTTPStatusError)
                            and exc.response.status_code >= 500
                        )
                        if retryable and attempt < 2:
                            await asyncio.sleep(2**attempt)
                            continue
                        message = (
                            f"HTTP {exc.response.status_code}"
                            if isinstance(exc, httpx.HTTPStatusError)
                            else "Retailer request failed; check connectivity or adapter status"
                        )
                        self.state(retailer, message)
                        raise ScrapeError(message) from None
        raise ScrapeError("Retailer request failed")

    async def render(self, url: str, retailer: str, hosts: tuple[str, ...]) -> str:
        self.validate_url(url, hosts)
        if not self.get_preferences().playwright_enabled:
            raise ScrapeError("No product data found; page may require JavaScript rendering")
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise ScrapeError(
                "Install the browser extra and Chromium to enable Playwright"
            ) from None
        async with self.browser_lock:
            try:
                async with async_playwright() as playwright:
                    prefs = self.get_preferences()
                    proxy = {"server": prefs.proxy} if prefs.proxy else None
                    browser = await playwright.chromium.launch(headless=True, proxy=proxy)
                    try:
                        page = await browser.new_page(user_agent=prefs.user_agent)

                        async def guard(route):
                            try:
                                self.validate_url(route.request.url, hosts)
                            except (ScrapeError, ValueError):
                                await route.abort()
                                return
                            await route.continue_()

                        await page.route("**/*", guard)
                        response = await page.goto(
                            url, wait_until="domcontentloaded", timeout=prefs.request_timeout * 1000
                        )
                        if response and response.status in (403, 429):
                            raise BlockedError("Browser request blocked by retailer")
                        await page.wait_for_timeout(1500)
                        html = await page.content()
                        if any(
                            x in html.lower()
                            for x in (
                                "validatecaptcha",
                                "cf-chl-",
                                "captcha-delivery.com",
                                "verify you are human",
                            )
                        ):
                            raise BlockedError("Retailer requested human verification")
                        return html
                    finally:
                        await browser.close()
            except BlockedError as exc:
                self.state(retailer, str(exc), cooldown=exc.cooldown)
                raise
            except Exception:
                raise ScrapeError(
                    "Browser rendering failed; verify Chromium installation"
                ) from None
