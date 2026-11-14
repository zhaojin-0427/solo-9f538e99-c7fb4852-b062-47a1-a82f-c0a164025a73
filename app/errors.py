"""Domain errors raised by the ingestion service."""

from __future__ import annotations

from typing import Any


class BatchRejected(Exception):
    """Raised when any rule failure requires rejecting the whole batch.

    ``violations`` is a list of dictionaries that is surfaced to the caller so
    the data operator can fix the payload without guessing.
    """

    def __init__(self, violations: list[dict[str, Any]]) -> None:
        self.violations = violations
        super().__init__(f"batch rejected with {len(violations)} violation(s)")


def violation(code: str, message: str, **context: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"code": code, "message": message}
    payload.update({key: value for key, value in context.items() if value is not None})
    return payload
