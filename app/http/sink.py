"""Persist ApiCallRecord rows.

Kept separate from the client so the transport has no database import and can be
unit-tested with a list as the sink.
"""

from __future__ import annotations

from app.db import session_scope
from app.http.client import ApiCallRecord
from app.models import ApiCall


def db_call_sink(record: ApiCallRecord) -> None:
    """Write one attempt to api_calls in its own short transaction.

    Its own transaction on purpose: the log of a failed call must survive the
    rollback of the business transaction that failed because of it.
    """
    with session_scope() as session:
        session.add(
            ApiCall(
                service=record.service,
                direction="out",
                method=record.method,
                url=record.url,
                attempt=record.attempt,
                status_code=record.status_code,
                duration_ms=record.duration_ms,
                request_body=record.request_body,
                response_body=record.response_body,
                error=record.error,
                correlation_id=record.correlation_id,
            )
        )


class ListSink:
    """Test double -- collects records in memory."""

    def __init__(self) -> None:
        self.records: list[ApiCallRecord] = []

    def __call__(self, record: ApiCallRecord) -> None:
        self.records.append(record)

    @property
    def attempts(self) -> int:
        return len(self.records)
