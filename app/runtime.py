import asyncio
import logging
from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from app.models import DiscoveryDraft
from app.retailers.http import Fetcher
from app.retailers.registry import Registry
from app.schemas.domain import now
from app.services.discovery import DiscoveryService
from app.services.monitoring import MonitoringService
from app.services.notifications import NotificationService
from app.services.queries import QueryService
from app.services.settings import SettingsService

log = logging.getLogger(__name__)


class Runtime:
    def __init__(self, config, db):
        self.config, self.db = config, db
        self.settings = SettingsService(db)
        self.fetcher = Fetcher(db, self.settings.get)
        self.registry = Registry(self.fetcher)
        self.notifications = NotificationService(db, self.settings)
        self.discovery = DiscoveryService(db, self.registry, self.settings)
        self.monitor = MonitoringService(db, self.registry, self.settings, self.notifications)
        self.queries = QueryService(db)
        self.tasks: set[asyncio.Task] = set()
        self.scheduler = AsyncIOScheduler(timezone="UTC")

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self._done)
        return task

    def _done(self, task):
        self.tasks.discard(task)
        if not task.cancelled() and task.exception():
            log.error(
                "background_job_failed", extra={"error_type": type(task.exception()).__name__}
            )

    def start(self):
        with self.db.session() as session:
            for draft in session.scalars(
                select(DiscoveryDraft).where(DiscoveryDraft.status.in_(["pending", "running"]))
            ):
                draft.status, draft.results = (
                    "failed",
                    {"errors": ["Discovery interrupted by a restart. Please retry."]},
                )
        # A cooldown from before a restart (often an upgrade fixing the cause) must not keep a
        # store silently skipped; the first new request re-arms it if the store still blocks.
        self.fetcher.retry_now()
        if self.config.scheduler_enabled:
            self.scheduler.add_job(
                self.tick,
                "interval",
                seconds=60,
                jitter=10,
                id="price-checks",
                max_instances=1,
                coalesce=True,
                next_run_time=now() + timedelta(seconds=5),
            )
            self.scheduler.start()

    async def tick(self):
        await self.spawn(self.monitor.run())

    async def close(self):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.db.engine.dispose()
