"""The scheduler.

A deliberately small loop rather than Celery or APScheduler: the bridge has four
periodic jobs, they must not overlap with themselves, and every one of them is
already idempotent.  Adding a broker and a beat process would be more moving
parts to deploy and monitor for no behaviour we do not already have.

Two properties matter here:

* **A job never overlaps itself.**  If a catalogue pull takes longer than its
  interval, the next tick is skipped rather than starting a second pull that
  fights the first for rate limit budget.
* **A failing job never kills the worker.**  It is logged, recorded in
  ``sync_runs``, and retried on its next tick.
"""

from __future__ import annotations

import signal
import time
from dataclasses import dataclass, field
from typing import Callable

from app.config import get_settings
from app.db import session_scope
from app.logging_conf import configure_logging, get_logger

logger = get_logger(__name__)


@dataclass
class Job:
    name: str
    interval_seconds: float
    run: Callable[[], None]
    # Stagger the first run so a fresh container does not fire everything at once.
    initial_delay: float = 0.0
    next_run_at: float = field(default=0.0)
    running: bool = False
    failures: int = 0

    def due(self, now: float) -> bool:
        return not self.running and now >= self.next_run_at

    def schedule_next(self, now: float) -> None:
        # Fixed delay from completion, not from start: a slow job backs itself
        # off instead of queueing up.
        self.next_run_at = now + self.interval_seconds


def _sync_supplier_catalogue() -> None:
    from app.services.catalog_sync import SupplierCatalogSync
    from app.suppliers.fazercards import FazerCardsAdapter

    with session_scope() as session:
        stats = SupplierCatalogSync(session, FazerCardsAdapter()).run()
    logger.info("supplier catalogue: %s", stats)


def _sync_store_catalogue() -> None:
    import datetime as dt

    from app.services.catalog_sync import StoreCatalogSync
    from app.stores.g2a import G2AAdapter

    # Routine passes only ask for what changed; the full crawl is a one-off run
    # by hand (`sync store-catalog`), because 20 products per page over a
    # marketplace-sized catalogue is not something to repeat hourly.
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=26)
    with session_scope() as session:
        stats = StoreCatalogSync(session, G2AAdapter()).run(updated_since=since)
    logger.info("store catalogue: %s", stats)


def _match() -> None:
    from app.services.matching import MatchingEngine

    with session_scope() as session:
        stats = MatchingEngine(session).run()
    logger.info("matching: %s", stats)


def _sync_offers() -> None:
    from app.services.offer_sync import OfferSync
    from app.stores.g2a import G2AAdapter

    with session_scope() as session:
        sync = OfferSync(session, G2AAdapter())
        stats = sync.run()
        jobs = sync.reconcile_jobs()
    logger.info("offers: %s jobs: %s", stats, jobs)


def build_jobs() -> list[Job]:
    settings = get_settings()
    return [
        Job(
            "supplier-catalogue",
            settings.catalog_sync_minutes * 60,
            _sync_supplier_catalogue,
            initial_delay=5,
        ),
        Job(
            "store-catalogue",
            max(6, settings.catalog_sync_minutes) * 60 * 4,
            _sync_store_catalogue,
            initial_delay=120,
        ),
        Job("matching", settings.catalog_sync_minutes * 60, _match, initial_delay=60),
        Job(
            "offers",
            settings.price_stock_sync_minutes * 60,
            _sync_offers,
            initial_delay=90,
        ),
    ]


class Worker:
    def __init__(self, jobs: list[Job] | None = None, *, clock=time.monotonic, sleep=time.sleep):
        self.jobs = jobs if jobs is not None else build_jobs()
        self.clock = clock
        self.sleep = sleep
        self.stopping = False
        now = clock()
        for job in self.jobs:
            job.next_run_at = now + job.initial_delay

    def request_stop(self, *_args) -> None:
        logger.info("worker: stop requested, finishing the current job")
        self.stopping = True

    def tick(self) -> list[str]:
        """Run whatever is due.  Returns the names of the jobs that ran."""
        ran: list[str] = []
        now = self.clock()
        for job in self.jobs:
            if not job.due(now):
                continue
            job.running = True
            ran.append(job.name)
            try:
                job.run()
                job.failures = 0
            except Exception:
                job.failures += 1
                # Logged with the traceback and recorded in sync_runs by the job
                # itself; the worker's responsibility is only to stay alive.
                logger.exception(
                    "job %s failed (%d consecutive)", job.name, job.failures
                )
            finally:
                job.running = False
                job.schedule_next(self.clock())
        return ran

    def run_forever(self, poll_seconds: float = 1.0) -> None:
        logger.info(
            "worker: started with jobs %s", ", ".join(j.name for j in self.jobs)
        )
        while not self.stopping:
            self.tick()
            self.sleep(poll_seconds)
        logger.info("worker: stopped")


def run_forever() -> None:
    configure_logging()
    worker = Worker()
    signal.signal(signal.SIGTERM, worker.request_stop)
    signal.signal(signal.SIGINT, worker.request_stop)
    worker.run_forever()
