"""Request-scoped counting of upstream HTTP attempts for debug diagnostics."""

from __future__ import annotations

from contextvars import ContextVar, Token

_request_call_count: ContextVar[dict[str, int] | None] = ContextVar(
    "external_http_call_count", default=None
)


def start_external_call_count() -> Token[dict[str, int] | None]:
    return _request_call_count.set({"count": 0})


def stop_external_call_count(token: Token[dict[str, int] | None]) -> None:
    _request_call_count.reset(token)


def count_external_call() -> None:
    state = _request_call_count.get()
    if state is not None:
        state["count"] += 1


def get_external_call_count() -> int:
    state = _request_call_count.get()
    return state["count"] if state is not None else 0
