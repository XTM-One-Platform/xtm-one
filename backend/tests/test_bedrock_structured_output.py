"""Bedrock sends ``outputConfig`` only to a model it offers structured outputs for.

Bedrock serves Claude Sonnet 5.5 without structured outputs (Converse
``outputConfig.textFormat``, strict tools): the request carrying one is
refused, which failed every JSON-schema call over (or failed it outright with
no fallback). The schema still reaches the model through the prompt-based SAP
instructions, as on any provider without native structured output.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.llm.base import ChatMessage
from app.llm.bedrock_provider import BedrockProvider
from app.services.structured_output import build_bedrock_structured_output

_SCHEMA = build_bedrock_structured_output(
    {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
)
_ANSWER = '{"answer": "ok"}'


def _provider() -> tuple[BedrockProvider, MagicMock]:
    provider = BedrockProvider(api_key="us-east-1")
    client = MagicMock()
    client.converse.return_value = {
        "output": {"message": {"content": [{"text": _ANSWER}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 10, "outputTokens": 5},
    }
    client.converse_stream.side_effect = lambda **_: {
        "stream": [
            {"contentBlockDelta": {"delta": {"text": _ANSWER}}},
            {"messageStop": {"stopReason": "end_turn"}},
            {"metadata": {"usage": {"inputTokens": 10, "outputTokens": 5}}},
        ]
    }
    provider._client = client
    return provider, client


async def _call_every_path(provider: BedrockProvider, model: str) -> None:
    messages = [ChatMessage(role="user", content="Answer in JSON.")]
    await provider.chat(messages=messages, model=model, structured_output=_SCHEMA)
    async for _chunk in provider.chat_stream(messages=messages, model=model, structured_output=_SCHEMA):
        pass

    async def _on_token(_text: str) -> None:
        return None

    await provider.chat_stream_with_tools(
        messages=messages, model=model, on_token=_on_token, structured_output=_SCHEMA
    )


def _sent(client: MagicMock) -> list[dict]:
    return [call.kwargs for call in client.converse.call_args_list] + [
        call.kwargs for call in client.converse_stream.call_args_list
    ]


@pytest.mark.parametrize(
    "model",
    [
        "anthropic.claude-sonnet-5-5",
        "us.anthropic.claude-sonnet-5-5-v1:0",
        "global.anthropic.claude-sonnet-5-5",
    ],
)
async def test_sonnet_5_5_is_sent_no_output_config(model: str):
    provider, client = _provider()

    await _call_every_path(provider, model)

    sent = _sent(client)
    assert len(sent) == 3
    assert all("outputConfig" not in request for request in sent)


@pytest.mark.parametrize("model", ["anthropic.claude-sonnet-5", "us.anthropic.claude-opus-5-v1:0"])
async def test_a_model_with_structured_outputs_keeps_its_output_config(model: str):
    provider, client = _provider()

    await _call_every_path(provider, model)

    sent = _sent(client)
    assert len(sent) == 3
    assert all(request["outputConfig"] == _SCHEMA for request in sent)
