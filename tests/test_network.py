import asyncio
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app.models import Alert, Product, RetailerState
from app.notifications.ntfy import NtfyProvider
from app.retailers.http import Fetcher, interactive
from app.retailers.parsing import BlockedError, RateLimitedError, ScrapeError
from app.schemas.domain import Preferences, now
from app.services.notifications import NotificationService
from app.services.settings import SettingsService


async def test_blocked_request_cools_down(db):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(403)

    fetcher = Fetcher(
        db, lambda: Preferences(), transport=httpx.MockTransport(respond), min_delay=0
    )
    for _ in range(2):
        with pytest.raises(BlockedError):
            await fetcher.get("https://www.worten.pt/", "worten", ("www.worten.pt",))
    assert len(calls) == 1
    with db.session() as session:
        assert session.get(RetailerState, "worten").blocked_until > now() + timedelta(minutes=55)


async def test_rate_limit_slows_down_and_retries(db):
    # Darty answered 429 to requests 2 s apart; that must not drop the store from discovery.
    responses = [httpx.Response(429, headers={"Retry-After": "0"}), httpx.Response(200, text="ok")]

    def respond(request):
        return responses.pop(0)

    fetcher = Fetcher(
        db, lambda: Preferences(), transport=httpx.MockTransport(respond), min_delay=0
    )
    assert await fetcher.get("https://www.darty.pt/", "darty", ("www.darty.pt",)) == "ok"
    with db.session() as session:
        assert session.get(RetailerState, "darty").blocked_until is None


@pytest.mark.parametrize("retry_after,expected_calls", [("0", 3), ("3600", 1)])
async def test_persistent_rate_limit_pauses_briefly(db, retry_after, expected_calls):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": retry_after})

    fetcher = Fetcher(
        db, lambda: Preferences(), transport=httpx.MockTransport(respond), min_delay=0
    )
    with pytest.raises(RateLimitedError):
        await fetcher.get("https://www.darty.pt/", "darty", ("www.darty.pt",))
    assert len(calls) == expected_calls  # never waits longer than a minute inside a request
    with db.session() as session:
        blocked_until = session.get(RetailerState, "darty").blocked_until
        assert now() + timedelta(minutes=5) < blocked_until < now() + timedelta(minutes=15)


async def test_redirect_to_private_host_rejected(db):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/secrets"})

    fetcher = Fetcher(
        db, lambda: Preferences(), transport=httpx.MockTransport(respond), min_delay=0
    )
    with pytest.raises(ScrapeError):
        await fetcher.get("https://www.worten.pt/", "worten", ("www.worten.pt",))
    assert len(calls) == 1


async def test_transient_retry_and_captcha_stop(db, monkeypatch):
    async def no_wait(_):
        return None

    monkeypatch.setattr("app.retailers.http.asyncio.sleep", no_wait)
    codes = [503, 502, 200]
    fetcher = Fetcher(
        db,
        lambda: Preferences(),
        transport=httpx.MockTransport(lambda _: httpx.Response(codes.pop(0), text="ok")),
        min_delay=0,
    )
    assert await fetcher.get("https://www.worten.pt/", "worten", ("www.worten.pt",)) == "ok"
    fetcher.transport = httpx.MockTransport(
        lambda _: httpx.Response(200, text="verify you are human")
    )
    with pytest.raises(BlockedError):
        await fetcher.get("https://www.worten.pt/", "worten", ("www.worten.pt",))


async def test_ntfy_unicode_payload_and_bearer_token():
    received = []

    def respond(request):
        received.append(request)
        return httpx.Response(200, json={"id": "ok"})

    provider = NtfyProvider(
        Preferences(ntfy_topic="deals", ntfy_token="private"),
        transport=httpx.MockTransport(respond),
    )
    await provider.send("Preço ótimo", "€799 → €699", urgent=True)
    import json

    body = json.loads(received[0].content)
    assert body["priority"] == 5 and body["topic"] == "deals"
    assert body["message"] == "€799 → €699"
    assert received[0].headers["Authorization"] == "Bearer private"


async def test_notification_retry_preserves_event(db):
    settings = SettingsService(db)
    settings.save(Preferences(ntfy_topic="topic"))
    with db.session() as session:
        product = Product(canonical_name="TV")
        session.add(product)
        session.flush()
        session.add(Alert(product_id=product.id, event_type="target_hit", message="TV deal"))

    class BrokenProvider:
        def __init__(self, prefs):
            pass

        async def send(self, *args, **kwargs):
            raise RuntimeError("secret should not be logged")

    service = NotificationService(db, settings, BrokenProvider)
    await service.deliver()
    with db.session() as session:
        alert = session.scalar(select(Alert))
        assert alert.notification_state == "pending"
        assert alert.notification_attempts == 1
        assert alert.message == "TV deal"


async def test_queued_requests_obey_new_cooldown(db):
    import asyncio

    calls = []

    async def respond(request):
        calls.append(request)
        await asyncio.sleep(0.01)
        return httpx.Response(403)

    fetcher = Fetcher(
        db, lambda: Preferences(), transport=httpx.MockTransport(respond), min_delay=0
    )
    results = await asyncio.gather(
        *(fetcher.get("https://www.worten.pt/", "worten", ("www.worten.pt",)) for _ in range(3)),
        return_exceptions=True,
    )
    assert all(isinstance(result, BlockedError) for result in results)
    assert len(calls) == 1


async def test_alerts_saved_during_delivery_are_sent_in_the_same_run(db):
    settings = SettingsService(db)
    settings.save(Preferences(ntfy_topic="topic"))
    with db.session() as session:
        product = Product(canonical_name="TV")
        session.add(product)
        session.flush()
        product_id = product.id
        session.add(Alert(product_id=product_id, event_type="target_hit", message="first"))
    sent = []

    class SlowProvider:
        def __init__(self, prefs):
            pass

        async def send(self, title, message, urgent=False):
            sent.append(message)
            if message == "first":
                # A second confirmation saves an alert and asks for delivery mid-send.
                with db.session() as session:
                    session.add(
                        Alert(product_id=product_id, event_type="insane_deal", message="second")
                    )
                await service.deliver()

    service = NotificationService(db, settings, SlowProvider)
    await service.deliver()
    assert sent == ["first", "second"]


async def test_interactive_requests_do_not_sit_through_rate_limit_waits(db):
    # Someone is watching a spinner: a 429 fails at once instead of waiting 60 s twice.
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "60"})

    fetcher = Fetcher(
        db, lambda: Preferences(), transport=httpx.MockTransport(respond), min_delay=0
    )
    token = interactive.set(True)
    try:
        with pytest.raises(RateLimitedError):
            await asyncio.wait_for(
                fetcher.get("https://www.darty.pt/", "darty", ("www.darty.pt",)), 5
            )
    finally:
        interactive.reset(token)
    assert len(calls) == 1
