"""Exactly-once guarantees, the encrypted key store, and the scheduler."""

from __future__ import annotations

import pytest

from app.crypto import decrypt, encrypt, fingerprint
from app.services.idempotency import (
    IdempotencyConflict,
    OperationInProgress,
    run_once,
)
from app.worker import Job, Worker


def test_the_same_key_runs_the_operation_once(session):
    calls = {"n": 0}

    def buy():
        calls["n"] += 1
        return {"order_id": "ord-1", "codes": ["ABC"]}

    payload = {"product": "p1", "quantity": 1}
    first = run_once(session, scope="order", key="k1", payload=payload, operation=buy)
    second = run_once(session, scope="order", key="k1", payload=payload, operation=buy)

    assert calls["n"] == 1, "the second call must not buy a second key"
    assert first.replayed is False
    assert second.replayed is True
    assert second.value == first.value


def test_reusing_a_key_for_a_different_request_is_an_error(session):
    run_once(
        session, scope="order", key="k1", payload={"q": 1}, operation=lambda: {"ok": True}
    )
    with pytest.raises(IdempotencyConflict):
        run_once(
            session, scope="order", key="k1", payload={"q": 99}, operation=lambda: {"ok": True}
        )


def test_a_failed_operation_can_be_retried(session):
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("supplier timed out")
        return {"order_id": "ord-2"}

    payload = {"product": "p1"}
    with pytest.raises(RuntimeError):
        run_once(session, scope="order", key="k2", payload=payload, operation=flaky)

    result = run_once(session, scope="order", key="k2", payload=payload, operation=flaky)
    assert attempts["n"] == 2
    assert result.value == {"order_id": "ord-2"}


def test_an_interrupted_purchase_is_recovered_not_repeated(session):
    """The crash-in-the-middle case.

    A process that died after claiming the key leaves an in-progress record.  The
    replay hook re-reads the supplier's order; if the purchase did land, we adopt
    it instead of buying again.
    """
    bought = {"n": 0}

    def crash():
        bought["n"] += 1
        raise KeyboardInterrupt("process killed mid-purchase")

    payload = {"product": "p1"}
    with pytest.raises(KeyboardInterrupt):
        run_once(session, scope="order", key="k3", payload=payload, operation=crash)

    # Simulate the record left behind by a hard kill (no failure recorded).
    from app.models import IdempotencyRecord

    record = (
        session.query(IdempotencyRecord)
        .filter(IdempotencyRecord.key == "k3")
        .one()
    )
    record.state = "in_progress"
    session.commit()

    recovered = run_once(
        session,
        scope="order",
        key="k3",
        payload=payload,
        operation=crash,
        on_replay=lambda _rec: {"order_id": "ord-from-supplier", "codes": ["XYZ"]},
    )
    assert recovered.replayed is True
    assert recovered.value["order_id"] == "ord-from-supplier"
    assert bought["n"] == 1, "recovery must not trigger a second purchase"


def test_an_in_progress_key_without_a_recovery_hook_refuses_to_proceed(session):
    from app.models import IdempotencyRecord
    from app.services.idempotency import request_hash

    session.add(
        IdempotencyRecord(
            scope="order",
            key="k4",
            request_hash=request_hash({"product": "p1"}),
            state="in_progress",
        )
    )
    session.commit()

    with pytest.raises(OperationInProgress):
        run_once(
            session,
            scope="order",
            key="k4",
            payload={"product": "p1"},
            operation=lambda: {"never": True},
        )


# ---------------------------------------------------------------- encryption


def test_keys_are_stored_encrypted_and_round_trip():
    code = "AAAA-BBBB-CCCC-DDDD"
    token = encrypt(code)
    assert code not in token
    assert decrypt(token) == code


def test_the_same_code_always_has_the_same_fingerprint():
    assert fingerprint("AAAA-BBBB") == fingerprint(" AAAA-BBBB ")
    assert fingerprint("AAAA-BBBB") != fingerprint("AAAA-BBBC")


# ------------------------------------------------------------------- worker


def test_a_job_never_overlaps_itself():
    now = {"t": 0.0}
    running: list[int] = []
    concurrent = {"max": 0}

    def slow():
        running.append(1)
        concurrent["max"] = max(concurrent["max"], len(running))
        now["t"] += 30  # the job takes longer than its interval
        running.pop()

    worker = Worker(
        [Job("slow", interval_seconds=10, run=slow)],
        clock=lambda: now["t"],
        sleep=lambda s: now.__setitem__("t", now["t"] + s),
    )
    for _ in range(5):
        worker.tick()
    assert concurrent["max"] == 1


def test_a_failing_job_does_not_stop_the_worker():
    now = {"t": 0.0}
    ran = {"good": 0}

    def bad():
        raise RuntimeError("upstream is down")

    def good():
        ran["good"] += 1

    worker = Worker(
        [Job("bad", 1, bad), Job("good", 1, good)],
        clock=lambda: now["t"],
        sleep=lambda s: now.__setitem__("t", now["t"] + s),
    )
    for _ in range(3):
        worker.tick()
        now["t"] += 2

    assert ran["good"] == 3
    assert worker.jobs[0].failures == 3
    assert worker.stopping is False


def test_jobs_are_rescheduled_from_completion_not_from_start():
    now = {"t": 0.0}

    def slow():
        now["t"] += 25

    job = Job("slow", interval_seconds=10, run=slow)
    worker = Worker([job], clock=lambda: now["t"], sleep=lambda _s: None)
    worker.tick()
    # Started at 0, finished at 25, so the next run is at 35 -- not at 10, which
    # would fire immediately and never let the loop catch up.
    assert job.next_run_at == 35
