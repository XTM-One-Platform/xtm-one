"""Unit tests for the ``on_attempt`` observer in ``retry_llm_call``."""

from __future__ import annotations

import pytest

from app.llm.base import retry_llm_call
from app.services.langfuse_tracing import (
    reset_current_retry_observer,
    set_current_retry_observer,
)


class _RetryableError(Exception):
    pass


class _FatalError(Exception):
    pass


def _is_retryable(exc: Exception) -> bool:
    return isinstance(exc, _RetryableError)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["kwarg", "contextvar"])
async def test_on_attempt_fires_once_per_intermediate_retry(monkeypatch, source):
    """Observer fires once per retry, with monotonically increasing 0-based index.

    Parametrized over the two ways the observer can be supplied:
    - ``kwarg``: passed explicitly via ``on_attempt=``
    - ``contextvar``: set via ``set_current_retry_observer`` and picked up by the fallback path
    """

    async def _no_sleep(_):
        return None

    monkeypatch.setattr("app.llm.base.asyncio.sleep", _no_sleep)

    calls: list[tuple[int, Exception, float]] = []
    attempts = {"n": 0}

    async def _fn() -> str:
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise _RetryableError(f"boom-{attempts['n']}")
        return "ok"

    def _observer(attempt: int, exc: Exception, delay: float) -> None:
        calls.append((attempt, exc, delay))

    if source == "kwarg":
        result = await retry_llm_call(
            _fn,
            is_retryable=_is_retryable,
            max_retries=3,
            base_delay=0.01,
            max_delay=0.05,
            on_attempt=_observer,
        )
    else:
        tok = set_current_retry_observer(_observer)
        try:
            result = await retry_llm_call(
                _fn,
                is_retryable=_is_retryable,
                max_retries=3,
                base_delay=0.01,
                max_delay=0.05,
            )
        finally:
            reset_current_retry_observer(tok)

    assert result == "ok"
    assert [c[0] for c in calls] == [0, 1]
    assert all(isinstance(c[1], _RetryableError) for c in calls)
    assert all(c[2] >= 0 for c in calls)


@pytest.mark.asyncio
async def test_on_attempt_not_called_on_first_success(monkeypatch):
    async def _no_sleep(_):
        return None

    monkeypatch.setattr("app.llm.base.asyncio.sleep", _no_sleep)

    calls: list = []

    async def _fn() -> str:
        return "ok"

    await retry_llm_call(
        _fn,
        is_retryable=_is_retryable,
        max_retries=3,
        base_delay=0.01,
        on_attempt=lambda a, e, d: calls.append((a, e, d)),
    )

    assert calls == []


@pytest.mark.asyncio
async def test_on_attempt_not_called_on_non_retryable(monkeypatch):
    async def _no_sleep(_):
        return None

    monkeypatch.setattr("app.llm.base.asyncio.sleep", _no_sleep)

    calls: list = []

    async def _fn() -> str:
        raise _FatalError("nope")

    with pytest.raises(_FatalError):
        await retry_llm_call(
            _fn,
            is_retryable=_is_retryable,
            max_retries=3,
            base_delay=0.01,
            on_attempt=lambda a, e, d: calls.append((a, e, d)),
        )

    assert calls == []


@pytest.mark.asyncio
async def test_on_attempt_not_called_on_terminal_failure(monkeypatch):
    """When all retries are exhausted, the final failure does NOT fire on_attempt."""

    async def _no_sleep(_):
        return None

    monkeypatch.setattr("app.llm.base.asyncio.sleep", _no_sleep)

    calls: list[int] = []

    async def _fn() -> str:
        raise _RetryableError("always-fails")

    with pytest.raises(_RetryableError):
        await retry_llm_call(
            _fn,
            is_retryable=_is_retryable,
            max_retries=2,
            base_delay=0.01,
            max_delay=0.02,
            on_attempt=lambda a, e, d: calls.append(a),
        )

    # max_retries=2 → initial + 2 retries = 3 total fn invocations.
    # on_attempt fires before sleeping into retry, so it fires twice
    # (before retry #1 and retry #2). The terminal third failure does NOT fire.
    assert calls == [0, 1]


@pytest.mark.asyncio
async def test_on_attempt_exception_does_not_break_retry(monkeypatch):
    """An observer that raises must not abort the retry loop."""

    async def _no_sleep(_):
        return None

    monkeypatch.setattr("app.llm.base.asyncio.sleep", _no_sleep)

    attempts = {"n": 0}

    async def _fn() -> str:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _RetryableError("boom")
        return "ok"

    def _bad_observer(*_a, **_kw):
        raise RuntimeError("observer crash")

    result = await retry_llm_call(
        _fn,
        is_retryable=_is_retryable,
        max_retries=3,
        base_delay=0.01,
        on_attempt=_bad_observer,
    )

    assert result == "ok"
    assert attempts["n"] == 2
