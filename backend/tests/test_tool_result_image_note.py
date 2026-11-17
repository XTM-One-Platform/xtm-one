"""Tool-result images carry a data-only statement on every provider path.

Text visible in a screenshot or a downloaded image is the one part of a tool
result no quarantine fence can mark. Each builder puts one shared sentence
(``TOOL_RESULT_IMAGE_DATA_NOTE``) next to the pixels of an image-bearing tool
result - once per result, never per image, never in the system prompt - and a
request without tool-result images carries nothing new.
"""

from __future__ import annotations

import json

import pytest

from app.llm.anthropic_provider import AnthropicProvider
from app.llm.base import (
    TOOL_RESULT_IMAGE_DATA_NOTE,
    TOOL_RESULT_VISION_NOTICE,
    ChatMessage,
    FileRef,
    build_openai_messages_async,
)
from app.llm.bedrock_provider import BedrockProvider
from app.llm.gemini_provider import GeminiProvider
from app.tools.base import ToolCall

_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00"
    b"\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82"
)

pytestmark = pytest.mark.asyncio


def _png(name: str) -> FileRef:
    return FileRef.from_bytes(_TINY_PNG, filename=name, content_type="image/png")


def _transcript(files: list[FileRef] | None, *, user_image: bool = False) -> list[ChatMessage]:
    return [
        ChatMessage(role="system", content="You are a helper."),
        ChatMessage(role="user", content="Look.", files=[_png("upload.png")] if user_image else None),
        ChatMessage(role="assistant", content="", tool_calls=[ToolCall(id="call_1", name="screenshot", arguments={})]),
        ChatMessage(role="tool", content="Screenshot captured.", tool_call_id="call_1", name="screenshot", files=files),
    ]


def _dump(payload) -> str:
    return json.dumps(payload, default=lambda o: repr(o))


async def _anthropic(messages):
    _system, conversation = await AnthropicProvider(api_key="k")._build_anthropic_messages(messages)
    return conversation


async def _bedrock(messages):
    _system, converse = await BedrockProvider(api_key="AKIA||secret||us-east-1")._build_converse_messages(messages)
    return converse


async def _gemini(messages):
    _system, contents = await GeminiProvider(api_key="k")._build_gemini_contents(messages)
    return contents


async def _openai(messages):
    return await build_openai_messages_async(messages, "openai")


_BUILDERS = [_openai, _anthropic, _bedrock, _gemini]


@pytest.mark.parametrize("build", _BUILDERS)
async def test_an_image_bearing_tool_result_carries_the_note_once(build):
    payload = await build(_transcript([_png("a.png"), _png("b.png")]))

    assert _dump(payload).count(TOOL_RESULT_IMAGE_DATA_NOTE) == 1


@pytest.mark.parametrize("build", _BUILDERS)
async def test_a_request_without_tool_result_images_carries_nothing_new(build):
    for messages in (_transcript(None), _transcript(None, user_image=True)):
        assert TOOL_RESULT_IMAGE_DATA_NOTE not in _dump(await build(messages))


async def test_anthropic_puts_the_note_right_after_the_pixels_inside_the_tool_result():
    conversation = await _anthropic(_transcript([_png("a.png")]))

    block = conversation[-1]["content"][0]
    assert block["type"] == "tool_result"
    assert [c["type"] for c in block["content"]] == ["text", "image", "text"]
    assert block["content"][-1]["text"] == TOOL_RESULT_IMAGE_DATA_NOTE


async def test_bedrock_and_gemini_add_it_to_the_tool_result_text_without_a_new_block():
    converse = await _bedrock(_transcript([_png("a.png")]))
    inner = converse[-1]["content"][-1]["toolResult"]["content"]
    assert [next(iter(b)) for b in inner] == ["text", "image"]
    assert inner[0]["text"] == "Screenshot captured.\n\n" + TOOL_RESULT_IMAGE_DATA_NOTE

    contents = await _gemini(_transcript([_png("a.png")]))
    response = next(p.function_response for p in contents[-1].parts if getattr(p, "function_response", None))
    assert response.response["result"] == "Screenshot captured.\n\n" + TOOL_RESULT_IMAGE_DATA_NOTE


async def test_openai_carries_it_in_the_existing_vision_notice():
    oai = await _openai(_transcript([_png("a.png")]))

    vision = [m for m in oai if m["role"] == "user" and isinstance(m.get("content"), list)]
    assert vision[-1]["content"][0] == {"type": "text", "text": TOOL_RESULT_VISION_NOTICE}
    assert TOOL_RESULT_VISION_NOTICE.endswith(" " + TOOL_RESULT_IMAGE_DATA_NOTE)
