import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select

from app.models import Alert
from app.notifications.ntfy import NtfyProvider
from app.schemas.domain import now

log = logging.getLogger(__name__)


class NotificationService:
    def __init__(self, db, settings, provider_factory=NtfyProvider):
        self.db, self.settings, self.provider_factory = db, settings, provider_factory
        self.lock = asyncio.Lock()

    async def deliver(self):
        if self.lock.locked():
            return
        async with self.lock:
            prefs = self.settings.get()
            with self.db.session() as session:
                alerts = session.scalars(
                    select(Alert)
                    .where(
                        Alert.notification_state == "pending", Alert.notification_next_at <= now()
                    )
                    .order_by(Alert.id)
                    .limit(25)
                ).all()
            provider = self.provider_factory(prefs) if prefs.ntfy_topic else None
            for alert in alerts:
                state = "skipped"
                if provider:
                    try:
                        await provider.send(
                            f"Pricewatch · {alert.event_type.replace('_', ' ')}",
                            alert.message,
                            urgent=alert.event_type == "insane_deal",
                        )
                        state = "sent"
                    except Exception:
                        state = "failed" if alert.notification_attempts >= 4 else "pending"
                        log.warning("notification_delivery_failed", extra={"alert_id": alert.id})
                with self.db.session() as session:
                    row = session.get(Alert, alert.id)
                    row.notification_state = state
                    row.notification_attempts += 1
                    row.notification_next_at = now() + timedelta(
                        minutes=2**row.notification_attempts
                    )
