import asyncio
import logging
from datetime import timedelta

import httpx
from sqlalchemy import select

from app.models import Alert, MarketListing
from app.notifications.ntfy import NtfyProvider
from app.schemas.domain import now
from app.services.alerts import BUY_NOW

log = logging.getLogger(__name__)


class DeliveryError(Exception):
    """Why a notification could not be sent, in words for the owner."""


def title(event_type: str) -> str:
    if event_type == "insane_deal":
        return BUY_NOW
    return f"Pricewatch · {event_type.replace('_', ' ')}"


class NotificationService:
    def __init__(self, db, settings, provider_factory=NtfyProvider):
        self.db, self.settings, self.provider_factory = db, settings, provider_factory
        self.lock = asyncio.Lock()

    async def deliver(self):
        if self.lock.locked():
            return
        async with self.lock:
            # Re-query until drained so alerts saved while sending are not left for the next tick.
            # Failed sends are rescheduled into the future, so the loop always terminates.
            while alerts := self._due():
                await self._send(alerts)

    def _due(self) -> list[Alert]:
        with self.db.session() as session:
            return session.scalars(
                select(Alert)
                .where(Alert.notification_state == "pending", Alert.notification_next_at <= now())
                .order_by(Alert.id)
                .limit(25)
            ).all()

    async def send_now(self, event_type: str, message: str, click: str | None = None):
        """Send one notification straight away, outside Activity; DeliveryError if it can't."""
        prefs = self.settings.get()
        if not prefs.ntfy_topic:
            raise DeliveryError("Set an ntfy topic in Settings first")
        try:
            await self.provider_factory(prefs).send(
                title(event_type), message, urgent=event_type == "insane_deal", click=click
            )
        except httpx.HTTPStatusError as exc:
            raise DeliveryError(
                f"ntfy refused it (HTTP {exc.response.status_code}); check the topic and token in Settings"
            ) from None
        except ValueError:
            raise DeliveryError("The ntfy server URL in Settings is not valid") from None
        except Exception:
            log.warning("notification_delivery_failed", extra={"event_type": event_type})
            raise DeliveryError(
                "Couldn't reach the ntfy server; check its URL in Settings"
            ) from None

    def link(self, alert: Alert) -> str | None:
        """A marketplace find opens the listing itself: those go fast."""
        if alert.market_listing_id is None:
            return None
        with self.db.session() as session:
            row = session.get(MarketListing, alert.market_listing_id)
            return row.url if row else None

    async def _send(self, alerts: list[Alert]):
        prefs = self.settings.get()
        provider = self.provider_factory(prefs) if prefs.ntfy_topic else None
        for alert in alerts:
            state = "skipped"
            if provider:
                try:
                    await provider.send(
                        title(alert.event_type),
                        alert.message,
                        urgent=alert.event_type == "insane_deal",
                        click=self.link(alert),
                    )
                    state = "sent"
                except Exception:
                    state = "failed" if alert.notification_attempts >= 4 else "pending"
                    log.warning("notification_delivery_failed", extra={"alert_id": alert.id})
            with self.db.session() as session:
                row = session.get(Alert, alert.id)
                row.notification_state = state
                row.notification_attempts += 1
                row.notification_next_at = now() + timedelta(minutes=2**row.notification_attempts)
