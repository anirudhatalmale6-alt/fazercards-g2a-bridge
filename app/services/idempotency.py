"""Exactly-once guard.

Used by the order flow so that a retried, replayed or duplicated request buys a
key once and only once.

The important detail is the order of operations.  The record is committed
**before** the side effect runs, in its own transaction.  If the process dies
mid-purchase, the key is already claimed and a replay lands in the "in progress"
branch, where it re-reads the upstream order instead of buying again.  Writing
the record after the purchase would leave exactly the gap this class exists to
close.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.logging_conf import get_logger
from app.models import IdempotencyRecord

logger = get_logger(__name__)

STATE_IN_PROGRESS = "in_progress"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"


class IdempotencyConflict(RuntimeError):
    """Same key, different request body -- the caller has a bug, not a retry."""


class OperationInProgress(RuntimeError):
    """A concurrent attempt holds this key and has not finished yet."""


@dataclass(frozen=True)
class Outcome:
    value: Any
    replayed: bool


def request_hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()


def run_once(
    session: Session,
    *,
    scope: str,
    key: str,
    payload: Any,
    operation: Callable[[], Any],
    on_replay: Callable[[IdempotencyRecord], Any] | None = None,
) -> Outcome:
    """Execute `operation` at most once for (scope, key).

    Returns the stored response on replay.  ``on_replay`` is consulted when a
    previous attempt claimed the key but never recorded a result -- that is where
    an order flow re-reads the supplier's order to find out whether the purchase
    actually went through.
    """
    digest = request_hash(payload)

    record = IdempotencyRecord(
        scope=scope, key=key, request_hash=digest, state=STATE_IN_PROGRESS
    )
    session.add(record)
    try:
        # Claim the key in its own transaction so the claim survives a crash.
        session.commit()
        claimed = True
    except IntegrityError:
        session.rollback()
        claimed = False

    if not claimed:
        existing = (
            session.query(IdempotencyRecord)
            .filter(IdempotencyRecord.scope == scope, IdempotencyRecord.key == key)
            .one()
        )
        if existing.request_hash != digest:
            raise IdempotencyConflict(
                f"idempotency key {key!r} was already used for a different request"
            )
        if existing.state == STATE_COMPLETED:
            logger.info("idempotency: replaying stored result for %s/%s", scope, key)
            return Outcome(value=existing.response, replayed=True)
        if existing.state == STATE_FAILED:
            # A recorded failure is safe to retry: no side effect landed.
            existing.state = STATE_IN_PROGRESS
            session.commit()
            record = existing
        else:
            if on_replay is None:
                raise OperationInProgress(
                    f"{scope}/{key} is already in progress elsewhere"
                )
            recovered = on_replay(existing)
            if recovered is not None:
                existing.state = STATE_COMPLETED
                existing.response = recovered
                existing.completed_at = dt.datetime.now(dt.timezone.utc)
                session.commit()
                return Outcome(value=recovered, replayed=True)
            record = existing

    try:
        result = operation()
    except Exception:
        record.state = STATE_FAILED
        session.commit()
        raise

    record.state = STATE_COMPLETED
    record.response = result if isinstance(result, (dict, list)) else {"value": result}
    record.completed_at = dt.datetime.now(dt.timezone.utc)
    session.commit()
    return Outcome(value=result, replayed=False)
