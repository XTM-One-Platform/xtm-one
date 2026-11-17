"""The provider's message in the AI Gateway's 502 for an upstream 4xx (#7).

XTM-One-Platform/ai-gateway#348 appends it: "provider error: upstream returned
status 400 Bad Request: <message>". The effort and temperature retries read it
as they read a direct 400; an older gateway's 502, which has none, is left alone.
"""

from __future__ import annotations

from typing import Any

import httpx
import openai
import pytest

from app.llm.openai_compat import FUNCTION_TOOLS_UNSUPPORTED, create_with_effort_retry, gateway_provider_message
from tests.gateway_helpers import gateway_refusal_body

_EFFORT_REFUSAL = (
    "Unsupported value: 'reasoning_effort' does not support 'none' with this model. "
    "Supported values are: 'low', 'medium', and 'high'."
)
_TEMPERATURE_REFUSAL = "Unsupported parameter: 'temperature' is not supported with this model."


def _gateway_502(provider_message: str | None, *, status: str = "400 Bad Request", retryable: bool = False):
    body = {**gateway_refusal_body(provider_message, status=status)["error"], "retryable": retryable}
    request = httpx.Request("POST", "https://gateway.example/v1/chat/completions")
    return openai.InternalServerError(body["message"], response=httpx.Response(502, request=request), body=body)


def test_the_provider_message_is_read_after_the_status():
    assert gateway_provider_message(_gateway_502("a: b")) == "a: b"


@pytest.mark.parametrize(
    "exc",
    [
        _gateway_502(None),
        _gateway_502("a", retryable=True),
        _gateway_502("a", status="500 Internal Server Error"),
    ],
    ids=["older gateway", "retryable", "upstream 5xx"],
)
def test_no_provider_message_is_read_from_another_502(exc):
    assert gateway_provider_message(exc) is None


async def test_a_refused_effort_behind_the_gateway_is_retried_with_a_listed_value():
    calls: list[dict[str, Any]] = []

    async def create(params: dict[str, Any]) -> str:
        calls.append(params)
        if params.get("reasoning_effort") == "none":
            raise _gateway_502(_EFFORT_REFUSAL)
        return "ok"

    assert await create_with_effort_retry(create, {"reasoning_effort": "none"}, "m") == "ok"
    assert [c.get("reasoning_effort") for c in calls] == ["none", "low"]


async def test_a_refused_temperature_behind_the_gateway_is_retried_without_it():
    calls: list[dict[str, Any]] = []

    async def create(params: dict[str, Any]) -> str:
        calls.append(params)
        if "temperature" in params:
            raise _gateway_502(_TEMPERATURE_REFUSAL)
        return "ok"

    assert await create_with_effort_retry(create, {"model": "m", "temperature": 0.2}, "m") == "ok"
    assert calls == [{"model": "m", "temperature": 0.2}, {"model": "m"}]


@pytest.mark.parametrize("provider_message", [None, "Invalid 'messages[1].content': string too long."])
async def test_a_gateway_502_without_such_a_message_is_sent_once(provider_message):
    calls: list[dict[str, Any]] = []

    async def create(params: dict[str, Any]) -> str:
        calls.append(params)
        raise _gateway_502(provider_message)

    with pytest.raises(openai.InternalServerError):
        await create_with_effort_retry(create, {"temperature": 0.2, "reasoning_effort": "none"}, "m")
    assert len(calls) == 1


async def test_tools_refused_with_every_effort_behind_the_gateway_end_on_one_400():
    async def create(params: dict[str, Any]) -> str:
        if params.get("reasoning_effort") == "none":
            raise _gateway_502(_EFFORT_REFUSAL)
        raise _gateway_502(
            "Function tools with reasoning_effort are not supported for this model in "
            "/v1/chat/completions. To use function tools, use /v1/responses or set reasoning_effort to 'none'."
        )

    with pytest.raises(openai.BadRequestError) as raised:
        await create_with_effort_retry(create, {"reasoning_effort": "low"}, "m")
    assert raised.value.status_code == 400
    assert raised.value.code == FUNCTION_TOOLS_UNSUPPORTED
