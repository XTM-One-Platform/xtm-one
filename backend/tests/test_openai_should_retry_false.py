"""``x-should-retry: false`` is obeyed by ``retry_llm_call`` for every OpenAI-compatible provider (#1536)."""

import httpx
import openai
import pytest

from app.llm import base
from app.llm.base import retry_llm_call
from app.llm.openai_compat import is_retryable_openai

_URL = "https://api.openai.com/v1/chat/completions"


def _status_error(cls, status: int, headers: dict | None = None, body: object = None) -> openai.APIStatusError:
    response = httpx.Response(status, headers=headers or {}, request=httpx.Request("POST", _URL))
    return cls("error", response=response, body=body)


@pytest.mark.parametrize(
    ("exc", "retryable"),
    [
        (_status_error(openai.RateLimitError, 429, {"x-should-retry": "false", "retry-after": "86400"}), False),
        (_status_error(openai.RateLimitError, 429, {"X-Should-Retry": "False"}), False),
        # The gateway's long circuit-open: the body says retryable, the header says not to bother.
        (_status_error(openai.InternalServerError, 502, {"x-should-retry": "false", "retry-after": "120"},
                       {"code": "provider_error", "retryable": True}), False),
        (_status_error(openai.RateLimitError, 429), True),
        (_status_error(openai.RateLimitError, 429, {"x-should-retry": "true"}), True),
        (_status_error(openai.InternalServerError, 503, {"retry-after": "2"}), True),
        (openai.APIConnectionError(request=httpx.Request("POST", _URL)), True),
    ],
    ids=["429-false", "header-case", "502-false-over-body", "429-absent", "429-true", "503-absent", "connection"],
)
def test_only_x_should_retry_false_stops_the_retry(exc, retryable):
    assert is_retryable_openai(exc) is retryable


@pytest.mark.asyncio
async def test_a_rate_limit_not_retried_still_backs_off(monkeypatch):
    from app.services import assignment_dispatch

    noted: list[str] = []

    async def _note(source):
        noted.append(source)

    monkeypatch.setattr(assignment_dispatch, "note_rate_limited", _note)
    # No real back-off: a wrong retry fails on the call count, not after minutes of waits.
    monkeypatch.setattr(base, "_extract_retry_after", lambda _exc: None)
    monkeypatch.setattr(base.random, "uniform", lambda _a, _b: 0)
    calls = 0
    exc = _status_error(openai.RateLimitError, 429, {"x-should-retry": "false", "retry-after": "86400"})

    async def _call():
        nonlocal calls
        calls += 1
        raise exc

    with pytest.raises(openai.RateLimitError):
        await retry_llm_call(_call, is_retryable=is_retryable_openai)
    assert calls == 1
    assert noted == ["llm"]


@pytest.mark.asyncio
async def test_an_error_not_retried_that_is_no_rate_limit_does_not_back_off(monkeypatch):
    from app.services import assignment_dispatch

    noted: list[str] = []

    async def _note(source):
        noted.append(source)

    monkeypatch.setattr(assignment_dispatch, "note_rate_limited", _note)

    async def _call():
        raise _status_error(openai.BadRequestError, 400)

    with pytest.raises(openai.BadRequestError):
        await retry_llm_call(_call, is_retryable=is_retryable_openai)
    assert noted == []
