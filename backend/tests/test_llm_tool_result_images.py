"""Tool-result files (runner / browser screenshots) must reach the model as
native vision blocks, not a byte-count stub and not Markdown-only — every
image of a parallel batch must reach it (trace 722375f1, 2026-09-24), and a
file-bearing result is visible to the model at least once, unconditionally,
before any retention pass may touch it (the same-turn strip of 24 Sept 2026,
agent "[Sam] General Assistant"). The ``read_file_content`` recovery
path every dropped-files note advertises must itself recover (the 25-call
loop of 24 Sept 2026 22:30, agent 6ed1d48c), and the file-recovery
guard force-keeps a result once that path has failed twice for a file."""

from __future__ import annotations

import dataclasses
import logging
import re

import anyio
import pytest

from app.llm.anthropic_provider import AnthropicProvider
from app.llm.base import (
    TOOL_RESULT_VISION_NOTICE,
    BaseLLMProvider,
    ChatMessage,
    ChatResponse,
    FileRef,
    build_openai_messages_async,
)
from app.llm.bedrock_provider import BedrockProvider
from app.llm.gemini_provider import GeminiProvider
from app.services.content_safety import untrusted_block_spans
from app.tools import agent_loop as al
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    ToolCall,
    ToolDefinition,
    ToolResult,
)

_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00"
    b"\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _outside_blocks(text: str) -> str:
    """*text* without its quarantine blocks (and the line break that follows each)."""
    spans = untrusted_block_spans(text)
    out, cursor = [], 0
    for start, end in spans:
        out.append(text[cursor:start])
        cursor = end + 1 if text[end : end + 1] == "\n" else end
    out.append(text[cursor:])
    return "".join(out)


def _screenshot_messages() -> list[ChatMessage]:
    file_ref = FileRef.from_bytes(
        _TINY_PNG,
        filename="desktop-20260831T172440Z.png",
        content_type="image/png",
    )
    return [
        ChatMessage(role="user", content="Capture the Metabase People page."),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(
                    id="call_shot",
                    name="computer_screenshot",
                    arguments={},
                )
            ],
        ),
        ChatMessage(
            role="tool",
            content=(
                "Full-desktop screenshot captured on runner 'audit-box'. "
                "Include this EXACT Markdown: "
                "![Full-desktop screenshot](/api/v1/chat/files/abc/download)"
            ),
            tool_call_id="call_shot",
            name="computer_screenshot",
            files=[file_ref],
        ),
    ]


def test_openai_forwards_tool_screenshot_as_followup_user_vision():
    converted = anyio.run(
        build_openai_messages_async, _screenshot_messages(), "openai"
    )

    tool_msgs = [m for m in converted if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert isinstance(tool_msgs[0]["content"], str)
    assert "image_url" not in tool_msgs[0]["content"]

    vision = [
        m
        for m in converted
        if m["role"] == "user" and isinstance(m.get("content"), list)
    ]
    assert len(vision) == 1
    parts = vision[0]["content"]
    assert parts[0] == {"type": "text", "text": TOOL_RESULT_VISION_NOTICE}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["detail"] == "high"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")

    # Pixels must sit AFTER the tool result and BEFORE any later assistant turn.
    tool_idx = converted.index(tool_msgs[0])
    vision_idx = converted.index(vision[0])
    assert vision_idx == tool_idx + 1


@pytest.mark.asyncio
async def test_anthropic_puts_screenshot_inside_tool_result():
    provider = AnthropicProvider(api_key="test-key")
    _system, conversation = await provider._build_anthropic_messages(
        _screenshot_messages()
    )

    user_turns = [m for m in conversation if m["role"] == "user"]
    tool_turn = user_turns[-1]
    assert isinstance(tool_turn["content"], list)
    block = tool_turn["content"][0]
    assert block["type"] == "tool_result"
    content = block["content"]
    assert isinstance(content, list)
    types = [c.get("type") for c in content]
    assert "text" in types
    assert "image" in types
    image = next(c for c in content if c["type"] == "image")
    assert image["source"]["type"] == "base64"
    assert image["source"]["media_type"] == "image/png"
    assert image["source"]["data"]

    text_chars, image_count, _bytes = provider._measure_anthropic_payload(
        _system, conversation
    )
    assert image_count == 1
    assert text_chars > 0


@pytest.mark.asyncio
async def test_gemini_attaches_screenshot_next_to_function_response():
    provider = GeminiProvider(api_key="test-key")
    _system, contents = await provider._build_gemini_contents(_screenshot_messages())

    last = contents[-1]
    assert last.role == "user"
    parts = list(last.parts or [])
    assert any(
        getattr(p, "function_response", None) for p in parts
    )
    inline = [
        p
        for p in parts
        if getattr(p, "inline_data", None) is not None
    ]
    assert len(inline) == 1
    assert inline[0].inline_data.mime_type == "image/png"
    assert inline[0].inline_data.data


@pytest.mark.asyncio
async def test_bedrock_puts_screenshot_inside_tool_result():
    provider = BedrockProvider(api_key="AKIA||secret||us-east-1")
    _system, converse = await provider._build_converse_messages(_screenshot_messages())

    last = converse[-1]
    assert last["role"] == "user"
    tool_result = last["content"][-1]["toolResult"]
    inner = tool_result["content"]
    assert any("text" in block for block in inner)
    images = [block["image"] for block in inner if "image" in block]
    assert len(images) == 1
    assert images[0]["format"] == "png"
    assert images[0]["source"]["bytes"] == _TINY_PNG

    text_chars, image_count, native_bytes = provider._measure_bedrock_payload(
        _system, converse
    )
    assert image_count == 1
    assert native_bytes == len(_TINY_PNG)
    assert text_chars > 0


# ---------------------------------------------------------------------------
# End-to-end: a parallel batch of three image downloads must put THREE images
# in the next provider request (trace 722375f1: only the last two survived
# the per-message vision retention, and the model re-downloaded the first).
# ---------------------------------------------------------------------------


_INCIDENT_BLOCKS = (
    ("3e58fce1-7f2a-8174-b7bd-cc1b6a0c9c2d", "platform-overview-current.png"),
    ("3e58fce1-7f2a-8188-9c4e-f47c3d397ca2", "platform-overview-target.png"),
    ("3e58fce1-7f2a-8182-b803-d4c40ac4fe2b", "platform-overview-gaps.png"),
)


class _SnapshotLLM(BaseLLMProvider):
    """Scripted LLM that freezes the transcript it is handed on every call.

    The loop passes its live ``messages`` list; the retention passes may
    mutate those objects later, so each call keeps ``dataclasses.replace``
    copies — exactly what the provider builder saw for THAT request.
    """

    provider_name = "stub"
    context_window = 128_000

    def __init__(self, script: list[ChatResponse]) -> None:
        self._script = script
        self.requests: list[list[ChatMessage]] = []

    async def chat(self, messages, model=None, tools=None, **kwargs):
        self.requests.append([dataclasses.replace(m) for m in messages])
        response = self._script[len(self.requests) - 1]
        return response

    async def chat_stream(self, messages, **kwargs):
        yield ""

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


def _incident_executor() -> tuple[CompositeToolExecutor, list[str]]:
    executed: list[str] = []
    callable_exec = CallableToolExecutor()
    definition = ToolDefinition(
        name="download_notion_file",
        description="download a Notion file block as a reusable file",
        source="custom:test",
        input_schema={
            "type": "object",
            "properties": {
                "block_id": {"type": "string"},
                "filename": {"type": "string"},
            },
            "required": ["block_id"],
        },
    )

    async def _download(block_id: str, filename: str = "", **_kw):
        executed.append(block_id)
        ref = FileRef.from_bytes(
            _TINY_PNG, filename or f"{block_id}.png", "image/png", file_id=f"file-{block_id}"
        )
        return ToolResult(
            tool_call_id="",
            content=(
                f"Downloaded '{ref.filename}' (377.5 KB) from Notion as a reusable "
                f"file. [[FILE:{ref.file_id}]]"
            ),
            files=[ref],
        )

    callable_exec.register("download_notion_file", _download)
    composite = CompositeToolExecutor()
    composite.add_tools([definition], callable_exec)
    return composite, executed


@pytest.fixture
def _quiet_langfuse(monkeypatch):
    monkeypatch.setattr(al, "_lf_start_llm_gen", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_generation", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_tool_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_start_iter_span", lambda **kw: None)
    monkeypatch.setattr(al, "_lf_start_tool_span", lambda **kw: None)


async def _run_incident_batch() -> tuple[list[ChatMessage], list[str]]:
    """One assistant turn with three parallel image downloads, then a final
    answer. Returns the transcript the SECOND provider request carried and
    the block ids actually downloaded."""
    composite, executed = _incident_executor()
    llm = _SnapshotLLM(
        [
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id=f"call_{i}",
                        name="download_notion_file",
                        arguments={"block_id": block_id, "filename": filename},
                    )
                    for i, (block_id, filename) in enumerate(_INCIDENT_BLOCKS)
                ],
            ),
            ChatResponse(content="All three diagrams compared.", model="stub"),
        ]
    )
    messages = [
        ChatMessage(role="user", content="Compare the three architecture diagrams on the page.")
    ]
    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=6,
    )
    assert response.content == "All three diagrams compared."
    assert len(llm.requests) == 2
    return llm.requests[1], executed


@pytest.mark.asyncio
async def test_next_request_after_parallel_image_batch_carries_every_image(
    _quiet_langfuse,
):
    """The transcript sent to the model right after the batch has all THREE
    tool results file-bearing — none stripped before the model saw them —
    and each provider builder turns them into three image blocks."""
    transcript, executed = await _run_incident_batch()
    assert executed == [block_id for block_id, _ in _INCIDENT_BLOCKS]

    tool_msgs = [m for m in transcript if m.role == "tool"]
    assert len(tool_msgs) == 3
    assert all(m.files and m.files[0].is_image for m in tool_msgs)
    assert not any("removed from the working context" in m.content for m in tool_msgs)

    # OpenAI: the pixels ride on the follow-up user message after the batch.
    oai = await build_openai_messages_async(transcript, "openai")
    vision = [
        m for m in oai if m["role"] == "user" and isinstance(m.get("content"), list)
    ]
    assert len(vision) == 1
    image_parts = [p for p in vision[0]["content"] if p.get("type") == "image_url"]
    assert len(image_parts) == 3
    assert vision[0]["content"][0] == {"type": "text", "text": TOOL_RESULT_VISION_NOTICE}

    # Anthropic: one image inside each tool_result block.
    anthropic = AnthropicProvider(api_key="test-key")
    _system, conversation = await anthropic._build_anthropic_messages(transcript)
    tool_results = [
        block
        for m in conversation
        if m["role"] == "user" and isinstance(m["content"], list)
        for block in m["content"]
        if block.get("type") == "tool_result"
    ]
    assert len(tool_results) == 3
    assert all(
        any(c.get("type") == "image" for c in block["content"]) for block in tool_results
    )
    _text, image_count, _bytes = anthropic._measure_anthropic_payload(_system, conversation)
    assert image_count == 3

    # Gemini: three inline_data parts next to the function responses.
    gemini = GeminiProvider(api_key="test-key")
    _system, contents = await gemini._build_gemini_contents(transcript)
    inline = [
        p
        for content in contents
        for p in (content.parts or [])
        if getattr(p, "inline_data", None) is not None
    ]
    assert len(inline) == 3

    # Bedrock: three image blocks inside the toolResult content.
    bedrock = BedrockProvider(api_key="AKIA||secret||us-east-1")
    _system, converse = await bedrock._build_converse_messages(transcript)
    images = [
        block["image"]
        for m in converse
        if m["role"] == "user"
        for part in m["content"]
        if "toolResult" in part
        for block in part["toolResult"]["content"]
        if "image" in block
    ]
    assert len(images) == 3
    _text, image_count, _bytes = bedrock._measure_bedrock_payload(_system, converse)
    assert image_count == 3


# ---------------------------------------------------------------------------
# End-to-end: a file-bearing result is visible to the model at least once,
# UNCONDITIONALLY — the same-turn strip of 24 Sept 2026 ("[Sam] General
# Assistant", gpt-6-astra): read_file_content and browser screenshots lost
# their pixels on the request that immediately followed them because the
# vision byte budget was spent WITHIN the just-returned batch, and the
# read_file_content recovery path was short-circuited by the dedup cache.
# ---------------------------------------------------------------------------

_DIAGRAM_ID = "3e58fce1-7f2a-8174-b7bd-cc1b6a0c9c2d"
_DIAGRAM_BYTES = 377_500
_SCREENSHOT_BYTES = 2_500_000  # a full-page capture; alone over the tight budget below
_TIGHT_FILES_BUDGET = 1024  # every image is over it: only "unseen" can keep pixels


def _sized_ref(file_id: str, filename: str, size: int) -> FileRef:
    """A FileRef whose STORED size (what the retention budget reads) is
    *size* while the bytes stay the tiny PNG the provider builders inline."""
    ref = FileRef.from_bytes(_TINY_PNG, filename, "image/png", file_id=file_id)
    ref.size = size
    return ref


def _incident_v2_executor() -> tuple[CompositeToolExecutor, list[tuple[str, str]]]:
    """``read_file_content`` (the platform's own result text) + ``browser``
    with a ``screenshot`` action, both file-bearing. Records every call
    actually EXECUTED so a dedup-cache hit is observable."""
    executed: list[tuple[str, str]] = []
    callable_exec = CallableToolExecutor()
    read_def = ToolDefinition(
        name="read_file_content",
        description="read a file attached to this conversation",
        source="builtin",
        input_schema={
            "type": "object",
            "properties": {"file_id": {"type": "string"}},
            "required": ["file_id"],
        },
    )
    browser_def = ToolDefinition(
        name="browser",
        description="headless browser",
        source="builtin",
        input_schema={
            "type": "object",
            "properties": {"action": {"type": "string"}},
            "required": ["action"],
        },
    )
    shots = {"n": 0}

    async def _read(file_id: str, **_kw):
        executed.append(("read_file_content", file_id))
        ref = _sized_ref(file_id, "platform-overview-current.png", _DIAGRAM_BYTES)
        return ToolResult(
            tool_call_id="",
            content=(
                "File: platform-overview-current.png (image/png, 377.5 KB) - the "
                "image is attached to this result."
            ),
            files=[ref],
        )

    async def _browser(action: str, **_kw):
        shots["n"] += 1
        executed.append(("browser", action))
        ref = _sized_ref(f"shot-{shots['n']}", f"screenshot_{shots['n']}.png", _SCREENSHOT_BYTES)
        return ToolResult(
            tool_call_id="",
            content=(
                f"Screenshot captured ({_SCREENSHOT_BYTES} bytes) and saved as a file. "
                "The PNG is attached as a native vision block - you can see the "
                "pixels. Read the page yourself (URL bar, labels, errors). Also "
                "include this EXACT Markdown in your response so the image renders "
                f"for the human: ![Screenshot](/api/v1/chat/files/{ref.file_id}/download)"
            ),
            files=[ref],
        )

    callable_exec.register("read_file_content", _read)
    callable_exec.register("browser", _browser)
    composite = CompositeToolExecutor()
    composite.add_tools([read_def, browser_def], callable_exec)
    return composite, executed


def _call(i: int, name: str, arguments: dict) -> ToolCall:
    return ToolCall(id=f"call_{i}", name=name, arguments=arguments)


def _tool_round(*calls: ToolCall) -> ChatResponse:
    return ChatResponse(content="", model="stub", stop_reason="tool_calls", tool_calls=list(calls))


def _tool_msgs(transcript: list[ChatMessage]) -> list[ChatMessage]:
    return [m for m in transcript if m.role == "tool"]


def _file_bearing(transcript: list[ChatMessage]) -> list[ChatMessage]:
    return [m for m in _tool_msgs(transcript) if m.files]


def _assert_no_contradiction(transcript: list[ChatMessage]) -> None:
    """(d) No tool message ever states the pixels are (or were) attached next
    to the note saying they were removed."""
    for m in _tool_msgs(transcript):
        body = m.content or ""
        if "removed from the working context" in body:
            assert "was attached" not in body, body
            assert "were attached" not in body, body
            assert "is attached" not in body, body
            assert "are attached" not in body, body
        if m.files is None and m.name in {"read_file_content", "browser"}:
            # A stripped result makes ONE statement about the pixels:
            # for a result the model has seen, that it was viewed at step N
            # and the model works from its notes - never an invitation to
            # call read_file_content again (that invitation was the retry
            # loop); for one it never saw, the recovery path.
            if "Already viewed at step" in body:
                assert "Work from your notes" in body, body
                assert "call read_file_content" not in body, body
            else:
                assert "read_file_content(file_id=" in body, body


async def _openai_image_parts(transcript: list[ChatMessage]) -> list[dict]:
    oai = await build_openai_messages_async(transcript, "openai")
    return [
        part
        for m in oai
        if m["role"] == "user" and isinstance(m.get("content"), list)
        for part in m["content"]
        if part.get("type") == "image_url"
    ]


async def _run_incident_v2(
    script: list[ChatResponse], *, max_iterations: int = 10
) -> tuple[_SnapshotLLM, list[tuple[str, str]], list[ChatMessage]]:
    composite, executed = _incident_v2_executor()
    llm = _SnapshotLLM(script)
    messages = [
        ChatMessage(
            role="user",
            content="Design the three architecture diagrams from the current one and the live page.",
        )
    ]
    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=max_iterations,
    )
    assert response.content == "Diagrams designed."
    return llm, executed, messages


@pytest.fixture
def _tight_vision_budget(monkeypatch):
    """Every image alone exceeds the vision byte budget, so under the pre-fix
    rules only the FIRST file-bearing result of the latest batch could keep
    its pixels. Whatever survives here survives because the model has not
    seen it yet — never because it happened to fit."""
    monkeypatch.setattr(al, "_compute_tool_result_files_budget", lambda _window: _TIGHT_FILES_BUDGET)


@pytest.mark.asyncio
async def test_replay_read_then_screenshot_then_read_each_request_carries_its_image(
    _quiet_langfuse, _tight_vision_budget
):
    """Replay of the incident: 1 read_file_content, then 1 screenshot, then
    read_file_content on the SAME id. Three provider requests follow the
    three results, and each carries the image it was made to show — exactly
    one under a budget no image fits — on every provider path."""
    llm, executed, _messages = await _run_incident_v2(
        [
            _tool_round(_call(1, "read_file_content", {"file_id": _DIAGRAM_ID})),
            _tool_round(_call(2, "browser", {"action": "screenshot"})),
            _tool_round(_call(3, "read_file_content", {"file_id": _DIAGRAM_ID})),
            ChatResponse(content="Diagrams designed.", model="stub"),
        ]
    )
    assert len(llm.requests) == 4
    # (2) The recovery path works: the second read_file_content EXECUTED (no
    # dedup-cache replay of the stripped first result).
    assert executed == [
        ("read_file_content", _DIAGRAM_ID),
        ("browser", "screenshot"),
        ("read_file_content", _DIAGRAM_ID),
    ]

    expected_names = ["read_file_content", "browser", "read_file_content"]
    for request_index, tool_name in zip((1, 2, 3), expected_names, strict=True):
        transcript = llm.requests[request_index]
        visible = _file_bearing(transcript)
        assert [m.name for m in visible] == [tool_name], (request_index, visible)
        # The just-returned result is the LAST tool message of the request.
        assert _tool_msgs(transcript)[-1] is visible[0]
        assert "removed from the working context" not in visible[0].content
        assert al._DEDUP_CACHE_MARKER not in visible[0].content
        _assert_no_contradiction(transcript)

        # OpenAI: one image_url part on the follow-up user message ...
        assert len(await _openai_image_parts(transcript)) == 1
        # ... Anthropic: one image block inside a tool_result ...
        anthropic = AnthropicProvider(api_key="test-key")
        _system, conversation = await anthropic._build_anthropic_messages(transcript)
        _text, image_count, _bytes = anthropic._measure_anthropic_payload(_system, conversation)
        assert image_count == 1
        # ... Gemini: one inline_data part ...
        gemini = GeminiProvider(api_key="test-key")
        _system, contents = await gemini._build_gemini_contents(transcript)
        assert (
            sum(
                1
                for content in contents
                for p in (content.parts or [])
                if getattr(p, "inline_data", None) is not None
            )
            == 1
        )
        # ... Bedrock: one image block inside a toolResult.
        bedrock = BedrockProvider(api_key="AKIA||secret||us-east-1")
        _system, converse = await bedrock._build_converse_messages(transcript)
        _text, image_count, _bytes = bedrock._measure_bedrock_payload(_system, converse)
        assert image_count == 1


@pytest.mark.asyncio
async def test_single_read_file_content_result_is_visible_whatever_the_budget_state(
    _quiet_langfuse, _tight_vision_budget
):
    """(a) A read_file_content result carries its image on the request that
    immediately follows it, with the budget already exhausted by an earlier,
    seen screenshot AND when it is the second result of its own batch behind
    a screenshot that alone exceeds the budget — the incident's shape."""
    llm, _executed, _messages = await _run_incident_v2(
        [
            # An earlier screenshot the model has seen: it exhausts the budget.
            _tool_round(_call(1, "browser", {"action": "screenshot"})),
            # The read alone in its batch ...
            _tool_round(_call(2, "read_file_content", {"file_id": _DIAGRAM_ID})),
            # ... and the read SECOND in a batch behind an over-budget screenshot.
            _tool_round(
                _call(3, "browser", {"action": "screenshot"}),
                _call(4, "read_file_content", {"file_id": _DIAGRAM_ID}),
            ),
            ChatResponse(content="Diagrams designed.", model="stub"),
        ]
    )
    assert len(llm.requests) == 4

    after_single_read = llm.requests[2]
    visible = _file_bearing(after_single_read)
    assert [m.name for m in visible] == ["read_file_content"]
    assert len(await _openai_image_parts(after_single_read)) == 1

    after_batch = llm.requests[3]
    visible = _file_bearing(after_batch)
    # BOTH results of the just-returned batch are visible: the read is no
    # longer stripped because the screenshot before it spent the budget.
    assert [m.name for m in visible] == ["browser", "read_file_content"]
    assert [m.tool_call_id for m in visible] == ["call_3", "call_4"]
    assert len(await _openai_image_parts(after_batch)) == 2
    for transcript in llm.requests:
        _assert_no_contradiction(transcript)


@pytest.mark.asyncio
async def test_browser_screenshot_result_is_visible_on_the_request_that_follows(
    _quiet_langfuse, _tight_vision_budget
):
    """(b) A browser screenshot carries its pixels on the immediately following
    request even as the SECOND file-bearing result of its batch (behind a
    read_file_content) — the incident's "two screenshots arrived, the others
    did not" was the batch position deciding, not the model."""
    llm, _executed, _messages = await _run_incident_v2(
        [
            _tool_round(
                _call(1, "read_file_content", {"file_id": _DIAGRAM_ID}),
                _call(2, "browser", {"action": "screenshot"}),
            ),
            _tool_round(_call(3, "browser", {"action": "screenshot"})),
            ChatResponse(content="Diagrams designed.", model="stub"),
        ]
    )
    assert len(llm.requests) == 3

    after_batch = llm.requests[1]
    visible = _file_bearing(after_batch)
    assert [m.name for m in visible] == ["read_file_content", "browser"]
    assert len(await _openai_image_parts(after_batch)) == 2

    after_second_shot = llm.requests[2]
    visible = _file_bearing(after_second_shot)
    assert [m.tool_call_id for m in visible] == ["call_3"]
    assert len(await _openai_image_parts(after_second_shot)) == 1
    # The seen, over-budget batch lost its pixels honestly: the note says at
    # which step the model viewed them - the step the tools RAN in, the
    # number read_file_content's own answer uses - and never invites another
    # read; the screenshot keeps the Markdown the reply needs.
    stripped = [m for m in _tool_msgs(after_second_shot) if m.files is None]
    assert [m.tool_call_id for m in stripped] == ["call_1", "call_2"]
    assert all("Already viewed at step 1" in m.content for m in stripped)
    assert not any("call read_file_content" in m.content for m in stripped)
    assert "![Screenshot](/api/v1/chat/files/shot-1/download)" in stripped[1].content
    for transcript in llm.requests:
        _assert_no_contradiction(transcript)


@pytest.mark.asyncio
async def test_second_read_file_content_after_a_strip_is_not_served_from_the_dedup_cache(
    _quiet_langfuse, _tight_vision_budget
):
    """(c) Once the first read_file_content result was stripped, the recovery
    call the note advertises EXECUTES and re-attaches the image — never a
    dedup-cache replay of the stripped text."""
    llm, executed, messages = await _run_incident_v2(
        [
            _tool_round(_call(1, "read_file_content", {"file_id": _DIAGRAM_ID})),
            _tool_round(_call(2, "browser", {"action": "screenshot"})),
            _tool_round(_call(3, "read_file_content", {"file_id": _DIAGRAM_ID})),
            ChatResponse(content="Diagrams designed.", model="stub"),
        ]
    )
    assert executed.count(("read_file_content", _DIAGRAM_ID)) == 2
    assert not any(al._DEDUP_CACHE_MARKER in (m.content or "") for m in _tool_msgs(messages))

    final_request = llm.requests[3]
    first_read, second_read = (
        m for m in _tool_msgs(final_request) if m.name == "read_file_content"
    )
    # The first read WAS stripped (seen, over budget) and says so in one
    # statement - viewed at step 1 (the step it ran in), work from the notes,
    # no invitation to read again; the second is the re-attached image.
    assert first_read.files is None
    assert _outside_blocks(first_read.content).startswith("[Image(s) removed from the working context")
    assert "Already viewed at step 1" in first_read.content
    assert "call read_file_content" not in first_read.content
    assert second_read.files and second_read.files[0].file_id == _DIAGRAM_ID
    assert second_read.content.endswith("the image is attached to this result.")
    _assert_no_contradiction(final_request)


def test_read_file_content_is_exempt_from_the_dedup_cache():
    """The tool every dropped-files note names as the recovery path can never
    be short-circuited by the cache that would replay the stripped result."""
    assert al._is_dedup_exempt("read_file_content")
    assert "read_file_content" in al.DEDUP_EXEMPT_TOOLS
    assert "read_file_content" in al._files_removed_note(
        [_sized_ref(_DIAGRAM_ID, "d.png", _DIAGRAM_BYTES)]
    )


@pytest.mark.asyncio
async def test_no_tool_message_ever_contradicts_itself_about_its_pixels(
    _quiet_langfuse, _tight_vision_budget
):
    """(d) Across a long run of reads and screenshots — the incident's eight
    file-bearing calls — no request ever carries a tool message that says the
    image is (or was) attached next to the note saying it was removed, and
    every request carries at least the image(s) it was made to show."""
    script = []
    for i in range(1, 9):
        if i % 2:
            script.append(_tool_round(_call(i, "read_file_content", {"file_id": _DIAGRAM_ID})))
        else:
            script.append(_tool_round(_call(i, "browser", {"action": "screenshot"})))
    script.append(ChatResponse(content="Diagrams designed.", model="stub"))

    llm, executed, messages = await _run_incident_v2(script, max_iterations=12)
    assert len(llm.requests) == 9
    assert len(executed) == 8
    for request_index in range(1, 9):
        transcript = llm.requests[request_index]
        _assert_no_contradiction(transcript)
        visible = _file_bearing(transcript)
        assert visible and _tool_msgs(transcript)[-1] is visible[-1]
        assert len(await _openai_image_parts(transcript)) == len(visible) >= 1
    _assert_no_contradiction(messages)


# ---------------------------------------------------------------------------
# The recovery path must recover — 24 Sept 2026 22:30, agent 6ed1d48c-8df3-
# 4eaa-b3e5-b597fc1aea6c ("[Sam] General Assistant", gpt-6-astra):
# after three Notion image downloads and forty Notion edits, read_file_content
# was answered without pixels 23 times in a row, alternating over three file
# ids and varying max_chars to defeat the dedup cache, before two calls showed
# the image. A read_file_content result is the model's ONE way back to a
# stored image: it is never stripped before the model has consumed it, never
# served from the cache, and — should both of those fail — force-kept by the
# file-recovery guard the third time a file id is answered without pixels.
# ---------------------------------------------------------------------------

_NOTION_BLOCKS = (
    ("3e58fce1-7f2a-8174-b7bd-cc1b6a0c9c2d", "platform-overview-current.png"),
    ("3e58fce1-7f2a-8188-9c4e-f47c3d397ca2", "platform-overview-target.png"),
    ("3e58fce1-7f2a-8182-b803-d4c40ac4fe2b", "platform-overview-gaps.png"),
)
_FILE_A, _FILE_B, _FILE_C = (f"file-{block_id}" for block_id, _ in _NOTION_BLOCKS)
_GUARD_LOGGER = "app.tools.agent_loop"


def _recovery_executor() -> tuple[CompositeToolExecutor, list[tuple[str, str]]]:
    """The incident's tool surface: Notion downloads (file-bearing reads), Notion
    block edits (write-classified, so they clear the dedup cache), the browser
    screenshot, and ``read_file_content`` with the platform's own result text
    and its real ``offset`` / ``max_chars`` arguments. Records every call that
    EXECUTED so a cache replay is observable."""
    executed: list[tuple[str, str]] = []
    callable_exec = CallableToolExecutor()
    known = {f"file-{block_id}": filename for block_id, filename in _NOTION_BLOCKS}
    definitions = [
        ToolDefinition(
            name="download_notion_file",
            description="download a Notion file block as a reusable file",
            source="custom:test",
            input_schema={
                "type": "object",
                "properties": {
                    "block_id": {"type": "string"},
                    "filename": {"type": "string"},
                },
                "required": ["block_id"],
            },
        ),
        ToolDefinition(
            name="update_notion_block",
            description="update a Notion block",
            source="custom:test",
            input_schema={
                "type": "object",
                "properties": {
                    "block_id": {"type": "string"},
                    "caption": {"type": "string"},
                },
                "required": ["block_id"],
            },
        ),
        ToolDefinition(
            name="read_file_content",
            description="read a file attached to this conversation",
            source="builtin",
            input_schema={
                "type": "object",
                "properties": {
                    "file_id": {"type": "string"},
                    "offset": {"type": "integer"},
                    "max_chars": {"type": "integer"},
                },
                "required": ["file_id"],
            },
        ),
        ToolDefinition(
            name="browser",
            description="headless browser",
            source="builtin",
            input_schema={
                "type": "object",
                "properties": {"action": {"type": "string"}},
                "required": ["action"],
            },
        ),
    ]
    shots = {"n": 0}

    async def _download(block_id: str, filename: str = "", **_kw):
        executed.append(("download_notion_file", block_id))
        ref = _sized_ref(f"file-{block_id}", filename or f"{block_id}.png", _DIAGRAM_BYTES)
        return ToolResult(
            tool_call_id="",
            content=(
                f"Downloaded '{ref.filename}' (377.5 KB) from Notion as a reusable "
                f"file. [[FILE:{ref.file_id}]]"
            ),
            files=[ref],
        )

    async def _update(block_id: str, caption: str = "", **_kw):
        executed.append(("update_notion_block", block_id))
        return ToolResult(
            tool_call_id="",
            content=f"Updated block {block_id}: caption set to {caption!r}.",
        )

    async def _read(file_id: str, offset: int = 0, max_chars: int | None = None, **_kw):
        executed.append(("read_file_content", file_id))
        filename = known.get(file_id, "platform-overview-current.png")
        ref = _sized_ref(file_id, filename, _DIAGRAM_BYTES)
        return ToolResult(
            tool_call_id="",
            content=(
                f"File: {filename} (image/png, 377.5 KB) - the image is attached "
                "to this result."
            ),
            files=[ref],
        )

    async def _browser(action: str, **_kw):
        shots["n"] += 1
        executed.append(("browser", action))
        ref = _sized_ref(f"shot-{shots['n']}", f"screenshot_{shots['n']}.png", _SCREENSHOT_BYTES)
        return ToolResult(
            tool_call_id="",
            content=(
                f"Screenshot captured ({_SCREENSHOT_BYTES} bytes) and saved as a file. "
                "The PNG is attached as a native vision block - you can see the "
                "pixels. Also include this EXACT Markdown in your response: "
                f"![Screenshot](/api/v1/chat/files/{ref.file_id}/download)"
            ),
            files=[ref],
        )

    callable_exec.register("download_notion_file", _download)
    callable_exec.register("update_notion_block", _update)
    callable_exec.register("read_file_content", _read)
    callable_exec.register("browser", _browser)
    composite = CompositeToolExecutor()
    composite.add_tools(definitions, callable_exec)
    return composite, executed


async def _run_recovery(
    script: list[ChatResponse], *, max_iterations: int = 30
) -> tuple[_SnapshotLLM, list[tuple[str, str]], list[ChatMessage]]:
    composite, executed = _recovery_executor()
    llm = _SnapshotLLM(script)
    messages = [
        ChatMessage(
            role="user",
            content=(
                "Align the three architecture diagrams on the Notion page and "
                "check each one renders correctly."
            ),
        )
    ]
    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=max_iterations,
    )
    assert response.content == "Diagrams aligned and checked."
    return llm, executed, messages


async def _image_counts(transcript: list[ChatMessage]) -> dict[str, int]:
    """How many native vision blocks each provider builder emits for *transcript*."""
    counts = {"openai": len(await _openai_image_parts(transcript))}
    anthropic = AnthropicProvider(api_key="test-key")
    _system, conversation = await anthropic._build_anthropic_messages(transcript)
    _text, counts["anthropic"], _bytes = anthropic._measure_anthropic_payload(
        _system, conversation
    )
    gemini = GeminiProvider(api_key="test-key")
    _system, contents = await gemini._build_gemini_contents(transcript)
    counts["gemini"] = sum(
        1
        for content in contents
        for p in (content.parts or [])
        if getattr(p, "inline_data", None) is not None
    )
    bedrock = BedrockProvider(api_key="AKIA||secret||us-east-1")
    _system, converse = await bedrock._build_converse_messages(transcript)
    _text, counts["bedrock"], _bytes = bedrock._measure_bedrock_payload(_system, converse)
    return counts


def _assert_just_returned_reads_visible(
    transcript: list[ChatMessage], expected_file_ids: list[str]
) -> None:
    """The last ``len(expected_file_ids)`` tool messages are the just-returned
    read_file_content results, each still carrying its image and stating only
    that (no note, no cache marker)."""
    tail = _tool_msgs(transcript)[-len(expected_file_ids) :]
    assert [m.name for m in tail] == ["read_file_content"] * len(expected_file_ids)
    assert [m.files[0].file_id for m in tail if m.files] == expected_file_ids, tail
    for m in tail:
        assert m.content.endswith("the image is attached to this result."), m.content
        assert "removed from the working context" not in m.content
        assert al._DEDUP_CACHE_MARKER not in m.content


def _guard_records(caplog) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == _GUARD_LOGGER and "File-recovery guard" in r.getMessage()
    ]


@pytest.mark.asyncio
async def test_replay_of_the_22h30_run_first_read_file_content_shows_pixels(
    _quiet_langfuse, caplog
):
    """Replay of the incident's shape on the loop's REAL budgets: three Notion
    downloads in one batch, twenty-five Notion edits, a re-download, then
    read_file_content alternating over the three file ids with and without
    ``max_chars`` and one parallel read batch. Every request that follows a
    read carries the image(s) it was made to show — the FIRST read included —
    every read executes (no cache replay), the four builders agree, no message
    contradicts itself, and the file-recovery guard never has to fire."""
    caplog.set_level(logging.WARNING, logger=_GUARD_LOGGER)
    blocks = [block_id for block_id, _ in _NOTION_BLOCKS]
    script: list[ChatResponse] = [
        _tool_round(
            *[
                _call(i, "download_notion_file", {"block_id": b, "filename": f})
                for i, (b, f) in enumerate(_NOTION_BLOCKS, start=1)
            ]
        )
    ]
    call_no = 10
    for _round in range(5):
        script.append(
            _tool_round(
                *[
                    _call(call_no + j, "update_notion_block", {"block_id": f"blk-{call_no + j}", "caption": "aligned"})
                    for j in range(5)
                ]
            )
        )
        call_no += 5
    script.append(_tool_round(_call(call_no, "download_notion_file", {"block_id": blocks[0], "filename": _NOTION_BLOCKS[0][1]})))
    reads = [
        [(_FILE_A, {})],
        [(_FILE_B, {})],
        [(_FILE_C, {})],
        [(_FILE_A, {"max_chars": 9000})],
        [(_FILE_B, {"max_chars": 9000})],
        [(_FILE_A, {"max_chars": 9000}), (_FILE_C, {"max_chars": 9000})],
    ]
    for batch in reads:
        call_no += 10
        script.append(
            _tool_round(
                *[
                    _call(call_no + j, "read_file_content", {"file_id": fid, **extra})
                    for j, (fid, extra) in enumerate(batch)
                ]
            )
        )
    script.append(ChatResponse(content="Diagrams aligned and checked.", model="stub"))

    llm, executed, messages = await _run_recovery(script)
    assert len(llm.requests) == len(script)
    # Every download and every read EXECUTED: the edits between them cleared
    # the dedup cache for the re-download, and read_file_content is exempt.
    assert [b for name, b in executed if name == "download_notion_file"] == [*blocks, blocks[0]]
    read_calls = [fid for name, fid in executed if name == "read_file_content"]
    assert read_calls == [fid for batch in reads for fid, _ in batch]
    assert not any(al._DEDUP_CACHE_MARKER in (m.content or "") for m in _tool_msgs(messages))

    # ``llm.requests[k]`` is the transcript of the call that returned
    # ``script[k]``; the request that FOLLOWS read round ``script[r]`` is
    # ``llm.requests[r + 1]``, so the six read rounds are answered by the
    # last six requests.
    first_read_request = len(script) - len(reads)
    for offset, batch in enumerate(reads):
        transcript = llm.requests[first_read_request + offset]
        expected = [fid for fid, _ in batch]
        _assert_just_returned_reads_visible(transcript, expected)
        _assert_no_contradiction(transcript)
        # The request carries exactly the images still attached, the
        # just-returned read(s) among them.
        visible = _file_bearing(transcript)
        assert len(await _openai_image_parts(transcript)) == len(visible) >= len(expected)

    # The FIRST read_file_content: pixels on every provider path, and the
    # image is the read's own file, not a leftover of an older download.
    first = llm.requests[first_read_request]
    visible = _file_bearing(first)
    assert visible[-1].name == "read_file_content"
    assert visible[-1].files[0].file_id == _FILE_A
    counts = await _image_counts(first)
    assert set(counts.values()) == {len(visible)}, counts
    _assert_no_contradiction(messages)
    assert _guard_records(caplog) == []


@pytest.mark.asyncio
async def test_read_on_a_new_id_after_six_older_file_bearing_results_is_carried_by_every_builder(
    _quiet_langfuse, _tight_vision_budget
):
    """Six older file-bearing tool messages (three batches of two downloads),
    then one read_file_content on a NEW id: the next provider request carries
    exactly that image — attached to the read's own tool result — on the
    OpenAI, Anthropic, Gemini and Bedrock builders. Under a budget no image
    fits, the six older (seen) results are the ones that give way."""
    new_id = "file-new-diagram"
    script = [
        _tool_round(
            _call(1, "download_notion_file", {"block_id": "d1", "filename": "d1.png"}),
            _call(2, "download_notion_file", {"block_id": "d2", "filename": "d2.png"}),
        ),
        _tool_round(
            _call(3, "download_notion_file", {"block_id": "d3", "filename": "d3.png"}),
            _call(4, "download_notion_file", {"block_id": "d4", "filename": "d4.png"}),
        ),
        _tool_round(
            _call(5, "download_notion_file", {"block_id": "d5", "filename": "d5.png"}),
            _call(6, "download_notion_file", {"block_id": "d6", "filename": "d6.png"}),
        ),
        _tool_round(_call(7, "read_file_content", {"file_id": new_id})),
        ChatResponse(content="Diagrams aligned and checked.", model="stub"),
    ]
    llm, _executed, _messages = await _run_recovery(script)
    assert len(llm.requests) == 5

    transcript = llm.requests[4]
    assert len(_tool_msgs(transcript)) == 7
    visible = _file_bearing(transcript)
    assert [m.tool_call_id for m in visible] == ["call_7"]
    assert visible[0].files[0].file_id == new_id
    _assert_no_contradiction(transcript)
    # The six older results lost their pixels honestly: each names its file
    # id and the step the model viewed it at, never a re-read.
    older = [m for m in _tool_msgs(transcript) if m.tool_call_id != "call_7"]
    assert all(m.files is None for m in older)
    assert all("Already viewed at step" in m.content for m in older)
    assert all("file_id file-" in m.content for m in older)
    assert not any("call read_file_content" in m.content for m in older)

    # OpenAI: one image_url, on the vision message right after the read.
    oai = await build_openai_messages_async(transcript, "openai")
    vision = [
        i for i, m in enumerate(oai) if m["role"] == "user" and isinstance(m.get("content"), list)
    ]
    assert len(vision) == 1
    read_idx = next(i for i, m in enumerate(oai) if m["role"] == "tool" and m["tool_call_id"] == "call_7")
    assert vision[0] == read_idx + 1
    assert sum(1 for p in oai[vision[0]]["content"] if p.get("type") == "image_url") == 1

    # Anthropic: one image, inside the read's tool_result block.
    anthropic = AnthropicProvider(api_key="test-key")
    _system, conversation = await anthropic._build_anthropic_messages(transcript)
    tool_results = [
        block
        for m in conversation
        if m["role"] == "user" and isinstance(m["content"], list)
        for block in m["content"]
        if block.get("type") == "tool_result"
    ]
    with_image = [
        block
        for block in tool_results
        if isinstance(block["content"], list)
        and any(c.get("type") == "image" for c in block["content"])
    ]
    assert [block["tool_use_id"] for block in with_image] == ["call_7"]

    # Gemini: one inline_data part, in the content that carries the read's
    # function_response.
    gemini = GeminiProvider(api_key="test-key")
    _system, contents = await gemini._build_gemini_contents(transcript)
    inline_owners = [
        content
        for content in contents
        for p in (content.parts or [])
        if getattr(p, "inline_data", None) is not None
    ]
    assert len(inline_owners) == 1
    assert any(
        getattr(p, "function_response", None) is not None
        and p.function_response.name == "read_file_content"
        for p in (inline_owners[0].parts or [])
    )

    # Bedrock: one image block, inside the read's toolResult.
    bedrock = BedrockProvider(api_key="AKIA||secret||us-east-1")
    _system, converse = await bedrock._build_converse_messages(transcript)
    image_owners = [
        part["toolResult"]["toolUseId"]
        for m in converse
        if m["role"] == "user"
        for part in m["content"]
        if "toolResult" in part
        and any("image" in block for block in part["toolResult"]["content"])
    ]
    assert image_owners == ["call_7"]


@pytest.mark.asyncio
async def test_identical_read_file_content_calls_all_execute_and_all_show_pixels(
    _quiet_langfuse
):
    """The same read_file_content(file_id=X) twice with identical arguments,
    then once more with the incident's ``max_chars=9000`` cache-buster: every
    call EXECUTES, every result reaches the model with its pixels, and no
    message anywhere is a cache replay."""
    script = [
        _tool_round(_call(1, "read_file_content", {"file_id": _FILE_A})),
        _tool_round(_call(2, "read_file_content", {"file_id": _FILE_A})),
        _tool_round(_call(3, "read_file_content", {"file_id": _FILE_A, "max_chars": 9000})),
        ChatResponse(content="Diagrams aligned and checked.", model="stub"),
    ]
    llm, executed, messages = await _run_recovery(script)
    assert executed == [("read_file_content", _FILE_A)] * 3
    assert len(llm.requests) == 4
    for request_index in (1, 2, 3):
        transcript = llm.requests[request_index]
        _assert_just_returned_reads_visible(transcript, [_FILE_A])
        visible = _file_bearing(transcript)
        # Under the real budget every 377.5 KB image fits; what bounds the
        # older, seen copies is the retention window of file-bearing groups.
        assert len(visible) == min(request_index, al._RECENT_TOOL_FILES_KEEP)
        assert len(await _openai_image_parts(transcript)) == len(visible)
        _assert_no_contradiction(transcript)
    assert not any(al._DEDUP_CACHE_MARKER in (m.content or "") for m in _tool_msgs(messages))


# ---------------------------------------------------------------------------
# The file-recovery guard: the belt for the case where the seen
# rule fails anyway.
# ---------------------------------------------------------------------------


@pytest.fixture
def _broken_seen_tracking(monkeypatch):
    """Simulate the failure class the guard exists for: every tool result
    counts as already seen, so the budgets alone decide what keeps its pixels
    — what a marking-order bug would produce."""
    monkeypatch.setattr(al, "_unseen_tool_result_predicate", lambda _consumed: lambda _msg: False)


@pytest.mark.asyncio
async def test_guard_force_keeps_the_third_read_after_two_pixelless_answers(
    _quiet_langfuse, _tight_vision_budget, _broken_seen_tracking, caplog
):
    """With seen-tracking broken and a budget no image fits, a
    ``[screenshot, read_file_content(X)]`` batch keeps the screenshot (the
    structural floor) and strips the read before the model saw it — twice.
    The third read_file_content(X) result is pinned: it reaches the model with
    its pixels, and the trace carries the two pixel-less answers and the
    force-keep, each with the message index and the budget state."""
    caplog.set_level(logging.WARNING, logger=_GUARD_LOGGER)
    script = []
    for i in (1, 3, 5):
        script.append(
            _tool_round(
                _call(i, "browser", {"action": "screenshot"}),
                _call(i + 1, "read_file_content", {"file_id": _FILE_A}),
            )
        )
    script.append(ChatResponse(content="Diagrams aligned and checked.", model="stub"))

    llm, executed, messages = await _run_recovery(script)
    assert executed.count(("read_file_content", _FILE_A)) == 3
    assert len(llm.requests) == 4

    # Batches 1 and 2: the read lost its pixels before the model saw it.
    for request_index, call_id in ((1, "call_2"), (2, "call_4")):
        transcript = llm.requests[request_index]
        read = next(m for m in _tool_msgs(transcript) if m.tool_call_id == call_id)
        assert read.files is None
        assert _outside_blocks(read.content).startswith("[Image(s) removed from the working context")
        _assert_no_contradiction(transcript)

    # Batch 3: the pinned read carries its image on the request that follows.
    third = llm.requests[3]
    read = next(m for m in _tool_msgs(third) if m.tool_call_id == "call_6")
    assert read.files and read.files[0].file_id == _FILE_A
    assert read.content.endswith("the image is attached to this result.")
    assert _FILE_A in {f.file_id for m in _file_bearing(third) for f in m.files}
    assert len(await _openai_image_parts(third)) == len(_file_bearing(third)) >= 1
    _assert_no_contradiction(third)
    _assert_no_contradiction(messages)

    records = _guard_records(caplog)
    pixelless = [r for r in records if "WITHOUT its pixels" in r.getMessage()]
    pins = [r for r in records if "force-keeping this result" in r.getMessage()]
    assert [r.args[0] for r in pixelless] == [_FILE_A, _FILE_A]
    assert [r.args[2] for r in pixelless] == [1, 2]  # answer N/2 before force-keep
    assert len(pins) == 1
    pin = pins[0].getMessage()
    assert "read_file_content(file_id=" + _FILE_A in pin
    assert "tool_call_id=call_6" in pin
    assert "message index" in pin
    for key in (
        "vision_budget_bytes",
        "attached_vision_bytes",
        "live_tool_result_budget_chars",
        "file_bearing_groups",
        "recent_file_groups_kept",
        "context_window",
        "transcript_chars",
    ):
        assert key in pin, key
    # The message index named is the pinned message's position in the
    # transcript the loop held when the pin was set.
    index = int(pin.split("message index ")[1].split(")")[0])
    assert messages[index].tool_call_id == "call_6"


def test_file_recovery_guard_counts_pins_and_releases():
    guard = al._FileRecoveryGuard()
    assert al._FileRecoveryGuard.file_id_of("read_file_content", {"file_id": " abc "}) == "abc"
    assert al._FileRecoveryGuard.file_id_of("read_file_content", {"file_id": ""}) is None
    assert al._FileRecoveryGuard.file_id_of("read_file_content", {}) is None
    assert al._FileRecoveryGuard.file_id_of("download_notion_file", {"file_id": "abc"}) is None

    def _read_result(call_id: str) -> ChatMessage:
        return ChatMessage(
            role="tool",
            content="File: d.png (image/png, 377.5 KB) - the image is attached to this result.",
            tool_call_id=call_id,
            name="read_file_content",
            files=[_sized_ref("abc", "d.png", _DIAGRAM_BYTES)],
        )

    # A text result (no files when appended) never counts, whatever happens to it.
    text = ChatMessage(role="tool", content="Content (complete, 12 characters):\nhello world!", tool_call_id="t0", name="read_file_content")
    guard.track(text, "notes.md")
    assert guard.observe([text]) == []
    assert guard.failures("notes.md") == 0

    first = _read_result("c1")
    guard.track(first, "abc")
    al._drop_tool_result_files(first)  # stripped before the model saw it
    failures = guard.observe([ChatMessage(role="user", content="q"), first])
    assert [(f["file_id"], f["message_index"], f["failures"]) for f in failures] == [("abc", 1, 1)]
    # Observing the same transcript again counts nothing twice.
    assert guard.observe([first]) == []
    assert not guard.should_pin("abc")

    second = _read_result("c2")
    guard.track(second, "abc")
    al._drop_tool_result_files(second)
    assert [f["failures"] for f in guard.observe([first, second])] == [2]
    assert guard.should_pin("abc")

    third = _read_result("c3")
    guard.track(third, "abc")
    guard.pin(third, "abc")
    assert guard.is_pinned(third) and not guard.is_pinned(second)
    assert guard.pinned_count == 1
    # A newer pin for the same file id replaces the older one.
    fourth = _read_result("c4")
    guard.track(fourth, "abc")
    guard.pin(fourth, "abc")
    assert not guard.is_pinned(third) and guard.is_pinned(fourth)
    assert guard.pinned_count == 1
    # Carried WITH its files: no failure counted, and the pin is released.
    assert guard.observe([first, second, third, fourth]) == []
    assert not guard.is_pinned(fourth)
    assert guard.pinned_count == 0
    # The count is not reset by a success: the run already proved the path
    # unreliable for this file, so the next result is pinned again.
    assert guard.should_pin("abc")
    # A pinned result carried WITHOUT its files (the pin failed too) is a
    # counted failure AND releases the pin: nothing is left to keep, and a
    # live pin must never outlive the request that carried it — that is
    # what keeps it inside the latest tool-call group.
    fifth = _read_result("c5")
    guard.track(fifth, "abc")
    guard.pin(fifth, "abc")
    al._drop_tool_result_files(fifth)
    assert [f["failures"] for f in guard.observe([fifth])] == [3]
    assert not guard.is_pinned(fifth)
    assert guard.pinned_count == 0
    assert guard.should_pin("abc")


def test_emergency_prune_keeps_a_pinned_result_through_every_phase():
    """A pinned read_file_content result deep in the prunable history survives
    phase 1 (tool-result stubbing), phase 2 (long-message truncation), phase 3
    (oldest-group eviction) and phase 4 (in-place truncation) with its body
    and its files intact, while the rest of the history is pruned."""
    pinned = ChatMessage(
        role="tool",
        content="File: d.png (image/png, 377.5 KB) - the image is attached to this result. " + "x" * 3_000,
        tool_call_id="pin",
        name="read_file_content",
        files=[_sized_ref("abc", "d.png", _DIAGRAM_BYTES)],
    )
    messages = [
        ChatMessage(role="system", content="persona"),
        ChatMessage(role="user", content="task"),
        ChatMessage(role="assistant", content="", tool_calls=[ToolCall(id="pin", name="read_file_content", arguments={"file_id": "abc"})]),
        pinned,
    ]
    for i in range(8):
        messages.append(
            ChatMessage(role="assistant", content="", tool_calls=[ToolCall(id=f"c{i}", name="list_items", arguments={})])
        )
        messages.append(
            ChatMessage(role="tool", content=f"result {i} " + "y" * 3_000, tool_call_id=f"c{i}", name="list_items")
        )
    original_content = pinned.content
    original_files = pinned.files
    before = sum(len(m.content or "") for m in messages)

    al._emergency_prune_session_context(messages, max_session_chars=2_500, keep=lambda m: m is pinned)

    assert pinned in messages
    assert pinned.content == original_content
    assert pinned.files is original_files
    # The assistant turn that asked for it stays with it (never an orphan).
    idx = messages.index(pinned)
    assert messages[idx - 1].role == "assistant" and messages[idx - 1].tool_calls[0].id == "pin"
    # ... and the prune still made progress on everything else.
    others = [m for m in messages if m.role == "tool" and m is not pinned]
    assert others and all(len(m.content) < 3_000 for m in others)
    assert sum(len(m.content or "") for m in messages) < before


def test_emergency_prune_with_a_pin_still_drops_the_files_of_unpinned_long_results():
    """Phase 2 carves the dropped-files note out of its per-message char budget
    for an UNPINNED long file-bearing result; that budget must never shadow the
    ``keep`` predicate (a local of the same name would
    have rebound it to an int and crashed the prune on the next message). The
    unpinned result loses its files with the note, the pinned one is intact,
    and every later message is still visited."""
    pinned = ChatMessage(
        role="tool",
        content="File: p.png (image/png, 377.5 KB) - the image is attached to this result.",
        tool_call_id="pin",
        name="read_file_content",
        files=[_sized_ref("pinned", "p.png", _DIAGRAM_BYTES)],
    )
    unpinned_long = ChatMessage(
        role="tool",
        content="File: u.png (image/png, 377.5 KB) - the image is attached to this result. " + "u" * 3_000,
        tool_call_id="u1",
        name="read_file_content",
        files=[_sized_ref("unpinned", "u.png", _DIAGRAM_BYTES)],
    )
    later_long = ChatMessage(
        role="tool",
        content="result " + "z" * 3_000,
        tool_call_id="z1",
        name="list_items",
    )
    messages = [
        ChatMessage(role="system", content="persona"),
        ChatMessage(role="user", content="task"),
        ChatMessage(role="assistant", content="", tool_calls=[ToolCall(id="pin", name="read_file_content", arguments={"file_id": "pinned"})]),
        pinned,
    ]
    for i in range(4):
        messages.append(
            ChatMessage(role="assistant", content="", tool_calls=[ToolCall(id=f"c{i}", name="list_items", arguments={})])
        )
        messages.append(
            ChatMessage(role="tool", content=f"result {i} " + "y" * 3_000, tool_call_id=f"c{i}", name="list_items")
        )
    messages.append(
        ChatMessage(role="assistant", content="", tool_calls=[ToolCall(id="u1", name="read_file_content", arguments={"file_id": "unpinned"})])
    )
    messages.append(unpinned_long)
    messages.append(
        ChatMessage(role="assistant", content="", tool_calls=[ToolCall(id="z1", name="list_items", arguments={})])
    )
    messages.append(later_long)

    al._emergency_prune_session_context(messages, max_session_chars=2_500, keep=lambda m: m is pinned)

    assert pinned in messages and pinned.files is not None
    assert pinned.content.endswith("the image is attached to this result.")
    assert unpinned_long.files is None
    assert "file_id unpinned" in unpinned_long.content
    assert "read_file_content(file_id=" in unpinned_long.content
    assert len(later_long.content) < 3_000


def test_emergency_prune_without_keep_is_unchanged_for_unpinned_results():
    """Sanity: with no ``keep`` predicate the emergency prune still stubs an
    old file-bearing result — the pin is opt-in, never a default."""
    old = ChatMessage(
        role="tool",
        content="File: d.png (image/png, 377.5 KB) - the image is attached to this result. " + "x" * 3_000,
        tool_call_id="old",
        name="read_file_content",
        files=[_sized_ref("abc", "d.png", _DIAGRAM_BYTES)],
    )
    messages = [
        ChatMessage(role="system", content="persona"),
        ChatMessage(role="user", content="task"),
        ChatMessage(role="assistant", content="", tool_calls=[ToolCall(id="old", name="read_file_content", arguments={"file_id": "abc"})]),
        old,
    ]
    for i in range(6):
        messages.append(
            ChatMessage(role="assistant", content="", tool_calls=[ToolCall(id=f"c{i}", name="list_items", arguments={})])
        )
        messages.append(
            ChatMessage(role="tool", content=f"result {i} " + "y" * 3_000, tool_call_id=f"c{i}", name="list_items")
        )
    al._emergency_prune_session_context(messages, max_session_chars=2_500)
    assert old.files is None
    assert "file_id abc" in old.content
    assert "read_file_content(file_id=" in old.content


def test_emergency_prune_keeps_a_pinned_result_when_the_system_prompt_must_shrink():
    """The last resort behind the pin: a pinned read_file_content result is a
    short line plus an image, so it can never be what pushes the transcript
    over the char budget — but it is un-prunable, so when the system prompt
    alone is over budget the prune has to reach phase 5 (system-prompt
    head+tail truncation) WITH the pin in place. The pin survives all five
    phases intact, the system prompt is what gives way, the transcript lands
    under budget, and the ``keep`` predicate is called as a predicate on every
    message it is asked about (phase 5 must never rebind that name — the
    phase-2 shadowing had a sibling there)."""
    pinned = ChatMessage(
        role="tool",
        content="File: p.png (image/png, 377.5 KB) - the image is attached to this result.",
        tool_call_id="pin",
        name="read_file_content",
        files=[_sized_ref("pinned", "p.png", _DIAGRAM_BYTES)],
    )
    system = ChatMessage(role="system", content="persona " * 2_500)  # 20,000 chars
    messages = [
        system,
        ChatMessage(role="user", content="task"),
        ChatMessage(role="assistant", content="", tool_calls=[ToolCall(id="pin", name="read_file_content", arguments={"file_id": "pinned"})]),
        pinned,
    ]
    for i in range(3):
        messages.append(
            ChatMessage(role="assistant", content="", tool_calls=[ToolCall(id=f"c{i}", name="list_items", arguments={})])
        )
        messages.append(
            ChatMessage(role="tool", content=f"result {i} " + "y" * 3_000, tool_call_id=f"c{i}", name="list_items")
        )
    original_content = pinned.content
    original_files = pinned.files
    asked: list[ChatMessage] = []

    def _keep(msg: ChatMessage) -> bool:
        assert isinstance(msg, ChatMessage)
        asked.append(msg)
        return msg is pinned

    budget = 12_000
    assert len(system.content) > max(budget, al._EMERGENCY_SYSTEM_TRUNCATE_MIN)
    al._emergency_prune_session_context(messages, max_session_chars=budget, keep=_keep)

    assert pinned in messages
    assert pinned.content == original_content
    assert pinned.files is original_files
    idx = messages.index(pinned)
    assert messages[idx - 1].role == "assistant" and messages[idx - 1].tool_calls[0].id == "pin"
    # Phase 5 ran: the system prompt was head+tail-truncated (persona head
    # and policy tail kept, the middle elided), never below its floor.
    assert len(system.content) < 20_000
    marker = re.search(
        r"\n\[\S [\d,]+ chars elided to fit context budget \S\]\n", system.content
    )
    assert marker is not None, system.content[:200]
    assert len(system.content) >= al._EMERGENCY_SYSTEM_MIN_KEEP
    # ... and that is what brought the transcript to budget with the pin
    # untouched — the pin is never the reason a prune cannot reach its target.
    # (Phase 5 sizes the kept head+tail to the budget and then adds its own
    # elision marker on top — a pre-existing, marker-sized slack that is not
    # the pin's: the pinned body is a single short line.)
    total = sum(len(m.content or "") for m in messages)
    assert total <= budget + len(marker.group(0)), (total, budget, marker.group(0))
    assert total - budget < len(pinned.content)
    others = [m for m in messages if m.role == "tool" and m is not pinned]
    assert others and all(len(m.content) < 3_000 for m in others)
    # The predicate was consulted about the pinned message and about the
    # others, as a predicate, through every phase.
    assert pinned in asked
    assert any(m is not pinned for m in asked)


@pytest.mark.asyncio
async def test_forced_answer_synthesis_is_a_completed_request_the_guard_observes(
    _quiet_langfuse, monkeypatch, caplog
):
    """The iteration cap can land right after a read_file_content result was
    appended: the forced-answer synthesis is then the request that shows it.
    It is a completed provider request like the in-loop ones, so the
    guard observes it — a pixel-less answer there is logged, a pin on it is
    released — instead of the run ending in a blind spot. Two iterations, a
    read on each (the second on the tools-stripped last iteration), then the
    backstop: three completed requests, three observations, the last one
    seeing the second read WITH its pixels."""
    caplog.set_level(logging.WARNING, logger=_GUARD_LOGGER)
    original = al._FileRecoveryGuard.observe
    observed: list[list[tuple[str | None, bool]]] = []

    def _spy(self, messages):
        observed.append(
            [(m.tool_call_id, bool(m.files)) for m in messages if m.role == "tool"]
        )
        return original(self, messages)

    monkeypatch.setattr(al._FileRecoveryGuard, "observe", _spy)

    composite, executed = _recovery_executor()
    llm = _SnapshotLLM(
        [
            _tool_round(_call(1, "read_file_content", {"file_id": _FILE_A})),
            _tool_round(_call(2, "read_file_content", {"file_id": _FILE_B})),
            # The forced-answer synthesis (long enough to skip the
            # stronger-prompt retry).
            ChatResponse(content="Diagrams aligned and checked. " * 5, model="stub"),
        ]
    )
    messages = [ChatMessage(role="user", content="Check the two diagrams.")]
    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=2,
    )
    assert "Diagrams aligned and checked." in response.content
    assert executed == [("read_file_content", _FILE_A), ("read_file_content", _FILE_B)]
    assert len(llm.requests) == 3

    # The synthesis carried the second read with its image — the
    # contract — on the provider path.
    synthesis = llm.requests[2]
    visible = _file_bearing(synthesis)
    assert visible and visible[-1].tool_call_id == "call_2"
    assert visible[-1].files[0].file_id == _FILE_B
    assert len(await _openai_image_parts(synthesis)) == len(visible) >= 1
    _assert_no_contradiction(synthesis)

    # Every completed request was observed, the synthesis included, and the
    # guard read the second read as carried WITH its files.
    assert len(observed) == 3
    assert observed[0] == []
    assert observed[1] == [("call_1", True)]
    assert observed[2][-1] == ("call_2", True)
    assert _guard_records(caplog) == []


@pytest.mark.asyncio
async def test_forced_answer_pixelless_answer_is_recorded_on_the_root_trace(
    _quiet_langfuse, monkeypatch, caplog
):
    """No iteration span is open when the backstop's synthesis completes, so a
    pixel-less answer observed there lands on the ROOT trace's metadata (the
    iteration span is where the in-loop ones go), tagged with the request
    that produced it, next to the WARNING. The loop's own passes cannot strip
    a one-line image read inside the backstop, so the failure class is
    injected: a prune that drops the just-returned read's files whatever the
    predicate says."""
    caplog.set_level(logging.WARNING, logger=_GUARD_LOGGER)
    enriched: list[dict] = []
    monkeypatch.setattr(
        al, "_lf_enrich_trace_metadata", lambda _ctx, meta: enriched.append(meta)
    )
    real_prune = al._prune_session_context

    def _faulty_prune(messages, max_session_chars=None, *, is_unseen=None):
        for m in messages:
            if m.role == "tool" and m.name == "read_file_content" and m.files:
                al._drop_tool_result_files(m)
        kwargs = {"is_unseen": is_unseen}
        if max_session_chars is not None:
            kwargs["max_session_chars"] = max_session_chars
        return real_prune(messages, **kwargs)

    monkeypatch.setattr(al, "_prune_session_context", _faulty_prune)

    composite, executed = _recovery_executor()
    llm = _SnapshotLLM(
        [
            _tool_round(_call(1, "read_file_content", {"file_id": _FILE_A})),
            ChatResponse(content="Diagrams aligned and checked. " * 5, model="stub"),
        ]
    )
    messages = [ChatMessage(role="user", content="Check the diagram.")]
    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=1,
    )
    assert "Diagrams aligned and checked." in response.content
    assert executed == [("read_file_content", _FILE_A)]
    assert len(llm.requests) == 2
    # The synthesis carried the read stripped (the injected fault).
    read = next(m for m in _tool_msgs(llm.requests[1]) if m.tool_call_id == "call_1")
    assert read.files is None
    _assert_no_contradiction(llm.requests[1])

    # Logged as a pixel-less answer, naming the request ...
    pixelless = [r for r in _guard_records(caplog) if "WITHOUT its pixels" in r.getMessage()]
    assert len(pixelless) == 1
    assert pixelless[0].args[0] == _FILE_A
    assert pixelless[0].getMessage().endswith("request: forced_answer")
    # ... and recorded on the root trace, with the budget state, since no
    # iteration span was open to carry it.
    assert len(enriched) == 1
    meta = enriched[0]
    answers = meta["file_recovery_pixelless_answers"]
    assert [(a["file_id"], a["tool_call_id"], a["failures"], a["phase"]) for a in answers] == [
        (_FILE_A, "call_1", 1, "forced_answer")
    ]
    assert messages[answers[0]["message_index"]].tool_call_id == "call_1"
    for key in (
        "vision_budget_bytes",
        "attached_vision_bytes",
        "live_tool_result_budget_chars",
        "file_bearing_groups",
        "recent_file_groups_kept",
        "context_window",
        "transcript_chars",
    ):
        assert key in meta["file_recovery_budget_state"], key
