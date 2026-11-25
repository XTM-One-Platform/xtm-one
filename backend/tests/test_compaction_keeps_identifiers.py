"""In-loop compaction keeps the identifiers of long tool results (#2382).

The head+tail excerpt of a tool result drops the middle of a long listing, so
the ids of the rows in that middle never reached the summariser. These tests
pin the identifier table, the re-run hint of an elided result, the caps that
keep the transcript bounded and the summary budget that carries the tables.
"""

from __future__ import annotations

import uuid

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.tools import agent_loop as al
from app.tools.base import ToolCall


class _EchoTableLLM(BaseLLMProvider):
    """A summariser that answers with every ``name = id`` line it was given."""

    provider_name = "stub"
    context_window = 200_000

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.max_tokens: list[int] = []

    async def chat(self, messages, **kwargs):
        transcript = messages[-1].content
        self.prompts.append(transcript)
        self.max_tokens.append(kwargs.get("max_tokens"))
        rows = [line.strip() for line in transcript.splitlines() if " = " in line]
        summary = "SUMMARY\n" + "\n".join(rows)
        return ChatResponse(content=summary, model="stub", stop_reason="end_turn")

    async def chat_stream(self, messages, **kwargs):
        yield "x"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


def _listing(n: int) -> tuple[str, list[str]]:
    ids = [str(uuid.UUID(int=i + 1)) for i in range(n)]
    rows = [
        f"asset-{i:02d} production web server in region eu-west, owner team platform = {ident}" + " " * 40
        for i, ident in enumerate(ids)
    ]
    return "\n".join(rows), ids


def _middle(content: str, args: dict) -> list[ChatMessage]:
    call = ToolCall(id="call-1", name="search_assets", arguments=args)
    return [
        ChatMessage(role="assistant", content="", tool_calls=[call]),
        ChatMessage(role="tool", content=content, tool_call_id="call-1", name="search_assets"),
        ChatMessage(role="assistant", content="Found the assets."),
    ]


def test_every_identifier_of_an_elided_listing_reaches_the_summariser():
    content, ids = _listing(30)
    assert len(content) > 2 * al._SEMANTIC_COMPACTION_TRANSCRIPT_MSG_CHARS
    transcript = al._build_inloop_transcript(_middle(content, {"prefix": "asset-"}))

    for ident in ids:
        assert ident in transcript
    # The row's name travels with its id.
    assert f"asset-15 production web server in region eu-west, owner team platform = {ids[15]}" in transcript


@pytest.mark.asyncio
async def test_a_summary_echoing_the_table_carries_every_identifier_and_the_rerun_hint():
    content, ids = _listing(30)
    llm = _EchoTableLLM()

    summary = await al._summarize_messages_for_context(llm, _middle(content, {"prefix": "asset-"}), model="stub")

    for ident in ids:
        assert ident in summary
    assert 're-run search_assets({"prefix": "asset-"}) to read the rest' in summary
    # The summary carrying identifier tables is given the room for them.
    assert llm.max_tokens == [al._SEMANTIC_COMPACTION_ID_TABLE_MAX_TOKENS]


def test_an_elided_result_names_its_tool_and_arguments():
    content = "header\n" + "z" * (3 * al._SEMANTIC_COMPACTION_TRANSCRIPT_MSG_CHARS) + "\nfooter"
    transcript = al._build_inloop_transcript(_middle(content, {"query": "open cases", "limit": 50}))

    assert '[Body elided: re-run search_assets({"limit": 50, "query": "open cases"}) to read the rest]' in transcript


@pytest.mark.asyncio
async def test_a_short_result_keeps_the_former_summary_budget_and_no_hint():
    llm = _EchoTableLLM()
    summary = await al._summarize_messages_for_context(
        llm, _middle("asset-01 = 00000000-0000-0000-0000-000000000001", {}), model="stub"
    )

    assert "re-run" not in summary
    assert "Body elided" not in llm.prompts[0]
    assert llm.max_tokens == [al._SEMANTIC_COMPACTION_MAX_TOKENS]


def test_the_identifier_tables_stay_bounded_per_result_and_per_compaction():
    content, _ids = _listing(400)
    messages: list[ChatMessage] = []
    for i in range(10):
        call = ToolCall(id=f"call-{i}", name="search_assets", arguments={"page": i})
        messages.append(ChatMessage(role="assistant", content="", tool_calls=[call]))
        messages.append(ChatMessage(role="tool", content=content, tool_call_id=f"call-{i}", name="search_assets"))

    _transcript, hints, id_chars = al._inloop_transcript_parts(messages)

    first_table, used = al._elided_identifier_table(
        content, al._SEMANTIC_COMPACTION_TRANSCRIPT_MSG_CHARS, al._SEMANTIC_COMPACTION_ID_TABLE_TOTAL_CHARS
    )
    assert used <= al._SEMANTIC_COMPACTION_ID_TABLE_RESULT_CHARS
    assert "more identifier(s) not listed" in first_table
    assert id_chars <= al._SEMANTIC_COMPACTION_ID_TABLE_TOTAL_CHARS
    assert len(hints) == 10


def test_json_id_values_and_urls_are_kept():
    rows = ",\n".join(
        f'{{"name": "case {i}", "id": "case--{i:04d}", "url": "https://ctem.example/cases/{i}"}}' for i in range(60)
    )
    transcript = al._build_inloop_transcript(_middle(f"[{rows}]", {}))

    assert "case--0030" in transcript
    assert "https://ctem.example/cases/30" in transcript
