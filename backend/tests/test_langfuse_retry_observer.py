"""Tests for the Langfuse retry-attempt observer + contextvar bridge."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.llm.base import retry_llm_call
from app.services.langfuse_tracing import (
    make_retry_attempt_observer,
    reset_current_retry_observer,
    set_current_retry_observer,
)


class _RetryableError(Exception):
    pass


def _is_retryable(exc: Exception) -> bool:
    return isinstance(exc, _RetryableError)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def test_make_retry_attempt_observer_returns_none_for_missing_ctx():
    assert make_retry_attempt_observer(None) is None
    assert make_retry_attempt_observer({}) is None
    assert make_retry_attempt_observer({"trace": None}) is None


def test_make_retry_attempt_observer_emits_event():
    parent_span = MagicMock()
    parent_span.create_event = MagicMock()

    observer = make_retry_attempt_observer({"trace": parent_span, "model": "gpt-4o"})
    assert observer is not None

    err = _RetryableError("rate-limited")
    observer(0, err, 1.234)

    parent_span.create_event.assert_called_once()
    kwargs = parent_span.create_event.call_args.kwargs
    assert kwargs["name"] == "attempt:retry:1"
    assert kwargs["level"] == "WARNING"
    assert "rate-limited" in kwargs["status_message"]
    assert kwargs["metadata"]["attempt"] == 1
    assert kwargs["metadata"]["delay_s"] == 1.234
    assert kwargs["metadata"]["error_type"] == "_RetryableError"
    assert kwargs["metadata"]["observation_kind"] == "retry_attempt"
    assert "input" not in kwargs


def test_make_retry_attempt_observer_swallows_exceptions():
    parent_span = MagicMock()
    parent_span.create_event = MagicMock(side_effect=RuntimeError("langfuse down"))

    observer = make_retry_attempt_observer({"trace": parent_span})
    assert observer is not None

    # Should not raise.
    observer(0, _RetryableError("boom"), 0.5)


def test_make_retry_attempt_observer_uses_1_based_naming():
    parent_span = MagicMock()
    parent_span.create_event = MagicMock()

    observer = make_retry_attempt_observer({"trace": parent_span})
    observer(0, _RetryableError("a"), 0.1)
    observer(1, _RetryableError("b"), 0.2)
    observer(2, _RetryableError("c"), 0.3)

    names = [
        call.kwargs["name"] for call in parent_span.create_event.call_args_list
    ]
    assert names == ["attempt:retry:1", "attempt:retry:2", "attempt:retry:3"]


# ---------------------------------------------------------------------------
# Bridge: retry_llm_call picks up the contextvar when on_attempt is None
# ---------------------------------------------------------------------------
#
# Ctxvar mechanics (default / set / reset / asyncio.gather isolation) are
# already covered for the trace ctxvar in
# test_langfuse_hierarchical_tracing.py — once is enough since this is a
# property of contextvars itself, not of our code. The bridge tests below
# exercise the integration with retry_llm_call, which IS our code.


@pytest.mark.asyncio
async def test_explicit_on_attempt_wins_over_contextvar(monkeypatch):
    async def _no_sleep(_):
        return None

    monkeypatch.setattr("app.llm.base.asyncio.sleep", _no_sleep)

    explicit_calls: list[int] = []
    ctx_calls: list[int] = []
    attempts = {"n": 0}

    async def _fn() -> str:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _RetryableError("boom")
        return "ok"

    tok = set_current_retry_observer(lambda a, e, d: ctx_calls.append(a))
    try:
        await retry_llm_call(
            _fn,
            is_retryable=_is_retryable,
            max_retries=3,
            base_delay=0.01,
            on_attempt=lambda a, e, d: explicit_calls.append(a),
        )
    finally:
        reset_current_retry_observer(tok)

    assert explicit_calls == [0]
    assert ctx_calls == []
