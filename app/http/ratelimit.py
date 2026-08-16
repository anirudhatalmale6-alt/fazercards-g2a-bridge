"""Rate limiting.

Two different problems, two different tools:

``TokenBucket``  -- an overall requests-per-second ceiling per API, so we never
                    hammer either side.  Cheap, in-process, thread-safe.

``PriceChangeBudget`` -- G2A caps *price changes per product per hour*.  That is
                    not a request-rate problem: the offending call succeeds
                    until suddenly it 429s, and by then the product is locked
                    for the rest of the hour.  We count our own changes in the
                    database and defer anything over budget to the next run.
"""

from __future__ import annotations

import datetime as dt
import threading
import time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import PriceChangeLog


class TokenBucket:
    """Classic token bucket: `rate` tokens/second, up to `capacity` in reserve."""

    def __init__(self, rate: float, capacity: int, *, clock=time.monotonic, sleep=time.sleep):
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate
        self.capacity = max(1, capacity)
        self._tokens = float(self.capacity)
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()
        self._lock = threading.Lock()

    def _refill_locked(self) -> None:
        now = self._clock()
        elapsed = now - self._updated
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._updated = now

    def try_acquire(self, tokens: float = 1.0) -> bool:
        with self._lock:
            self._refill_locked()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    def acquire(self, tokens: float = 1.0) -> float:
        """Block until a token is free.  Returns how long we waited, in seconds."""
        waited = 0.0
        while True:
            with self._lock:
                self._refill_locked()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return waited
                deficit = tokens - self._tokens
                delay = deficit / self.rate
            self._sleep(delay)
            waited += delay


class PriceChangeBudget:
    """How many more times we may change a product's price this hour."""

    def __init__(self, session: Session, *, store_code: str, limit: int):
        self.session = session
        self.store_code = store_code
        self.limit = limit

    def _window_start(self, now: dt.datetime | None = None) -> dt.datetime:
        now = now or dt.datetime.now(dt.timezone.utc)
        return now - dt.timedelta(hours=1)

    def used(self, product_ext_id: str, *, now: dt.datetime | None = None) -> int:
        stmt = select(func.count(PriceChangeLog.id)).where(
            PriceChangeLog.store_code == self.store_code,
            PriceChangeLog.product_ext_id == product_ext_id,
            PriceChangeLog.changed_at >= self._window_start(now),
        )
        return int(self.session.execute(stmt).scalar_one())

    def allows(self, product_ext_id: str, *, now: dt.datetime | None = None) -> bool:
        return self.used(product_ext_id, now=now) < self.limit

    def record(
        self,
        product_ext_id: str,
        *,
        new_price: float,
        old_price: float | None = None,
        offer_ext_id: str | None = None,
        now: dt.datetime | None = None,
    ) -> PriceChangeLog:
        row = PriceChangeLog(
            store_code=self.store_code,
            product_ext_id=product_ext_id,
            offer_ext_id=offer_ext_id,
            old_price=old_price,
            new_price=new_price,
        )
        if now is not None:
            row.changed_at = now
        self.session.add(row)
        self.session.flush()
        return row

    def prune(self, older_than_hours: int = 48) -> int:
        """Housekeeping -- the log only needs to cover the rolling window."""
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=older_than_hours)
        rows = (
            self.session.query(PriceChangeLog)
            .filter(PriceChangeLog.changed_at < cutoff)
            .delete(synchronize_session=False)
        )
        return int(rows)
