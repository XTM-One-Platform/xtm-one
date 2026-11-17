"""Chat slash commands (#6590): the registry, the ``/context`` report, its Markdown.

The report reads what the agent loop published with its last reading, so the
loop side is pinned too: the detail it publishes sums to the buckets it
details, the tracker keeps the provider's count of the call it measured, and
neither reaches a frame.
"""

from __future__ import annotations

import pytest
from markdown_it import MarkdownIt

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.services import chat_turn_stream
from app.services.chat_commands import (
    ARGUMENT_DETAIL,
    COMMAND_CONTEXT,
    COMMAND_HELP,
    COMMANDS,
    SURFACE_WEB,
    commands_for,
    parse_command,
    usage,
)
from app.services.chat_commands.context_report import (
    CATEGORY_KEYS,
    ContextCategory,
    ContextReport,
    _categories,
    _scaled,
)
from app.services.chat_commands.render import markdown_text, render_context_report, render_help
from app.services.context_usage import (
    CONTEXT_COMMAND_META_KEYS,
    CONTEXT_USAGE_META_KEYS,
    ContextUsage,
    ContextUsageTracker,
    provider_prompt_usage,
    split_tokens,
)
from app.services.prompt_budget import section_chars
from app.tools.agent_loop import run_agent_loop
from app.tools.base import ToolDefinition

# ── The registry ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "command", "argument"),
    [
        ("/context", COMMAND_CONTEXT, None),
        ("  /CONTEXT  ", COMMAND_CONTEXT, None),
        ("/context detail", COMMAND_CONTEXT, ARGUMENT_DETAIL),
        ("/Context DETAIL", COMMAND_CONTEXT, ARGUMENT_DETAIL),
        ("/context -d", COMMAND_CONTEXT, ARGUMENT_DETAIL),
        ("/context\tdetail", COMMAND_CONTEXT, ARGUMENT_DETAIL),
        ("/help", COMMAND_HELP, None),
    ],
    ids=["bare", "case-and-spaces", "detail", "detail-case", "alias", "tab", "help"],
)
def test_a_command_is_parsed_the_same_whatever_its_case_and_spacing(text, command, argument):
    parsed = parse_command(text, SURFACE_WEB)
    assert parsed is not None
    assert parsed.command.name == command
    assert (parsed.argument.name if parsed.argument else None) == argument


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "/",
        "context",
        "/contextual",
        "/context foo",
        "/context detail please",
        "/help me",
        "/help detail",
        "/etc/hosts",
        "/ context",
        "x /context",
        "/context " + "d" * 80,
    ],
    ids=[
        "empty",
        "blank",
        "slash",
        "no-slash",
        "longer-name",
        "unknown-argument",
        "extra-token",
        "help-with-text",
        "help-has-no-argument",
        "a-path",
        "space-after-slash",
        "not-first",
        "too-long",
    ],
)
def test_anything_but_an_exact_command_is_an_ordinary_message(text):
    assert parse_command(text, SURFACE_WEB) is None


def test_a_surface_offers_only_its_commands():
    assert parse_command("/context", "slack") is None
    assert commands_for("slack") == ()
    assert [c.name for c in commands_for(SURFACE_WEB)] == [COMMAND_CONTEXT, COMMAND_HELP]


def test_usage_names_every_argument():
    by_name = {c.name: c for c in COMMANDS}
    assert usage(by_name[COMMAND_CONTEXT]) == "/context [detail]"
    assert usage(by_name[COMMAND_HELP]) == "/help"


# ── Markdown: every cell is literal text ────────────────────────────────────

_MD = MarkdownIt("commonmark").enable(["table", "strikethrough"])


def _table_cells(markdown: str) -> list[list[list]]:
    """The rows of the first table, each cell as its inline children tokens."""
    tokens = _MD.parse(markdown)
    rows: list[list[list]] = []
    row: list[list] | None = None
    cell_open = False
    for token in tokens:
        if token.type == "tr_open":
            row = []
        elif token.type == "tr_close":
            rows.append(row)
        elif token.type in ("th_open", "td_open"):
            cell_open = True
        elif token.type in ("th_close", "td_close"):
            cell_open = False
        elif token.type == "inline" and cell_open:
            row.append(token.children or [])
    return rows


_HOSTILE_NAMES = {
    "pipe": "evil|tool",
    "newline": "line1\n| injected | row |",
    "carriage-return": "a\r\nb",
    "line-separator": "a b",
    "tab": "a\tb",
    "backtick": "`code`",
    "link": "[click](https://evil.example)",
    "image": "![x](https://evil.example/x.png)",
    "html": "<img src=x onerror=alert(1)>",
    "entity": "&lt;script&gt;",
    "emphasis": "*bold* _it_ ~~gone~~",
    "autolink": "<https://evil.example>",
    "bidi-override": "safe‮gnp.exe",
    "zero-width": "a​b",
    "backslash": "a\\|b",
}


@pytest.mark.parametrize("name", list(_HOSTILE_NAMES.values()), ids=list(_HOSTILE_NAMES))
def test_a_hostile_name_stays_one_literal_cell(name):
    table = "| Item | Tokens |\n| --- | ---: |\n| " + markdown_text(name) + " | 12 |"
    rows = _table_cells(table)
    assert len(rows) == 2
    assert len(rows[1]) == 2
    children = rows[1][0]
    # Nothing but text: no link, image, code span, emphasis or raw HTML.
    assert {child.type for child in children} <= {"text"}
    rendered = "".join(child.content for child in children)
    expected = " ".join(
        "".join(" " if ch in "\n\r\t ‮​" else ch for ch in name).split()
    )
    assert rendered == expected


def test_a_long_name_is_cut_with_an_ellipsis():
    text = markdown_text("x" * 200)
    assert text == "x" * 79 + "…"


# ── The report: categories, scaling, unattributed ───────────────────────────


def _usage(breakdown: dict[str, int], level: str | None = "normal") -> ContextUsage:
    return ContextUsage(sum(breakdown.values()), 100_000, breakdown, level)


_DETAIL = {
    "system": {"persona": 300, "agent_memory": 200, "skills_catalog": 100, "platform": 400},
    "tools": [
        {"name": "search", "kind": "builtin", "group": None, "tokens": 500},
        {"name": "create_issue", "kind": "mcp", "group": "Github (MCP)", "tokens": 700},
        {"name": "list_cases", "kind": "integration", "group": "OpenCTI", "tokens": 300},
    ],
    "tool_results": {"create_issue": 2_000},
    "compaction": {"gate_tokens": 5_000, "approaching_tokens": 6_800, "trigger_tokens": 8_000, "target_tokens": 7_000},
}
_BREAKDOWN = {"system": 1_000, "tools": 500, "dynamic_tools": 1_000, "tool_results": 2_000, "conversation": 500}


def test_each_bucket_is_split_into_its_categories_and_items():
    categories = _categories(_usage(_BREAKDOWN), _DETAIL, overhead_only=False)
    assert categories.totals == {
        "system_prompt": 700,
        "memory": 200,
        "skills": 100,
        "builtin_tools": 500,
        "mcp_tools": 700,
        "integration_tools": 300,
        "tool_results": 2_000,
        "messages": 500,
    }
    assert sum(categories.totals.values()) == sum(_BREAKDOWN.values())
    assert [(i.name, i.tokens, i.group) for i in categories.items["mcp_tools"]] == [
        ("create_issue", 700, "Github (MCP)")
    ]
    assert {i.name: i.tokens for i in categories.items["system_prompt"]} == {"persona": 300, "platform": 400}


def test_a_detail_that_does_not_add_up_falls_back_to_its_bucket():
    broken = {**_DETAIL, "tools": [{"name": "search", "kind": "builtin", "group": None, "tokens": 1}]}
    categories = _categories(_usage(_BREAKDOWN), broken, overhead_only=False)
    assert categories.totals["builtin_tools"] == 500
    assert categories.totals["connected_tools"] == 1_000
    assert categories.items["builtin_tools"] == []
    assert "mcp_tools" not in categories.totals
    # The other sections still detail their buckets.
    assert categories.totals["memory"] == 200


def test_a_borrowed_reading_keeps_the_agent_overhead_only():
    categories = _categories(_usage(_BREAKDOWN), _DETAIL, overhead_only=True)
    assert set(categories.totals) == {
        "system_prompt",
        "memory",
        "skills",
        "builtin_tools",
        "mcp_tools",
        "integration_tools",
    }


def test_an_estimate_over_the_provider_count_is_scaled_down_exactly():
    categories = _categories(_usage(_BREAKDOWN), _DETAIL, overhead_only=False)
    lines = _scaled(categories, 3_001)
    assert sum(line.tokens for line in lines) == 3_001
    for line in lines:
        if line.items:
            assert sum(item.tokens for item in line.items) == line.tokens
    assert [line.key for line in lines] == [key for key in CATEGORY_KEYS if key in {line.key for line in lines}]


def test_an_estimate_under_the_provider_count_is_kept():
    categories = _categories(_usage(_BREAKDOWN), _DETAIL, overhead_only=False)
    lines = _scaled(categories, 10_000)
    assert {line.key: line.tokens for line in lines} == categories.totals


def test_split_tokens_sums_exactly_and_drops_empty_shares():
    assert split_tokens(10, {"a": 1, "b": 1, "c": 1}) == {"a": 4, "b": 3, "c": 3}
    assert split_tokens(0, {"a": 1}) == {}
    assert split_tokens(5, {"a": 0, "b": 2}) == {"b": 5}


# ── The tracker: the provider's count of the measured call ──────────────────


@pytest.mark.parametrize(
    ("usage", "prompt", "cached"),
    [
        ({"input_tokens": 1_000, "output_tokens": 50, "cache_read_input_tokens": 9_000, "cache_creation_input_tokens": 500}, 10_500, 9_000),
        ({"prompt_tokens": 12_000, "completion_tokens": 30, "prompt_tokens_details": {"cached_tokens": 8_000}}, 12_000, 8_000),
        ({"prompt_tokens": 4_000, "completion_tokens": 30}, 4_000, 0),
    ],
    ids=["anthropic-cache", "openai-gemini-cached-details", "no-cache"],
)
def test_the_provider_prompt_is_read_in_every_usage_shape(usage, prompt, cached):
    reading = provider_prompt_usage({"status": "llm_step", "llm_model": "m", "llm_usage": usage})
    assert reading is not None
    assert (reading.model, reading.prompt_tokens, reading.cached_tokens) == ("m", prompt, cached)


def test_a_rescue_step_sums_several_calls_and_is_no_reading():
    event = {"status": "llm_step", "llm_usage": {"prompt_tokens": 5}, "empty_response_retry": True}
    assert provider_prompt_usage(event) is None
    assert provider_prompt_usage({"status": "llm_step", "llm_usage": {}}) is None
    assert provider_prompt_usage({"status": "thinking", "llm_usage": {"prompt_tokens": 5}}) is None


def test_the_tracker_pairs_a_reading_with_its_own_call_and_keeps_frames_clean():
    tracker = ContextUsageTracker()
    detail = {"compaction": {"gate_tokens": 1, "approaching_tokens": 2, "trigger_tokens": 3, "target_tokens": 2}}
    # A call's count before any reading is no reading's.
    tracker.observe({"status": "llm_step", "llm_usage": {"prompt_tokens": 99}})
    assert tracker.command_meta() == {}

    tracker.observe({"status": "thinking", "context_tokens": 10, "context_window": 100, "context_detail": detail})
    tracker.observe({"status": "llm_step", "llm_model": "m", "llm_usage": {"prompt_tokens": 12}})
    assert tracker.command_meta() == {
        "context_detail": detail,
        "context_model": "m",
        "context_provider_tokens": 12,
        "context_cached_tokens": 0,
    }
    # What frames carry is the gauge reading alone.
    assert set(tracker.meta()) <= set(CONTEXT_USAGE_META_KEYS)

    # The next reading measures another prompt: the old count is dropped.
    tracker.observe({"status": "thinking", "context_tokens": 20, "context_window": 100})
    assert tracker.command_meta() == {}
    assert set(CONTEXT_COMMAND_META_KEYS).isdisjoint(CONTEXT_USAGE_META_KEYS)


def test_a_published_frame_never_carries_the_detail():
    event = {"status": "thinking", "context_tokens": 1, "context_window": 2, "context_detail": {"tools": []}}
    slim = chat_turn_stream.slim_frame(chat_turn_stream.event_frame(event))
    assert "context_detail" not in slim
    assert slim["context_tokens"] == 1


def test_the_public_widget_relays_slim_frames():
    """The public widget forwarded raw events to anonymous visitors, the
    system prompt copy of each ``tool_start`` and the detail of each
    ``thinking`` included."""
    from app.api.v1 import public_chat

    # Imported for its relay (ruff refuses an unused import).
    assert public_chat.slim_frame is chat_turn_stream.slim_frame


# ── The loop publishes the detail of each reading ───────────────────────────


class _StubLLM(BaseLLMProvider):
    provider_name = "stub"
    context_window = 100_000

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        return ChatResponse(content="final answer", model=model or "stub", usage={"prompt_tokens": 1234})

    async def chat_stream(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        yield "final answer"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


async def test_the_loop_details_every_bucket_of_its_reading():
    persona, memory = "p" * 900, "m" * 600
    sections = section_chars([("persona", persona), ("agent_memory", memory)])
    messages = [
        ChatMessage(role="system", content=persona + "\n" + memory + "\nplatform rules" * 20),
        ChatMessage(role="user", content="q" * 300),
        ChatMessage(role="tool", content="r" * 900, tool_call_id="t1", name="lookup"),
    ]
    tools = [
        ToolDefinition(name="builtin_search", description="d" * 300, source="builtin:search"),
        ToolDefinition(name="mcp_thing", description="d" * 3_000, source="mcp:acme"),
    ]
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)

    await run_agent_loop(
        llm=_StubLLM(),
        messages=messages,
        tools=tools,
        max_iterations=8,
        on_event=on_event,
        context_sections=sections,
    )
    thinking = next(e for e in events if e.get("status") == "thinking")
    breakdown, detail = thinking["context_breakdown"], thinking["context_detail"]
    assert sum(detail["system"].values()) == breakdown["system"]
    assert set(detail["system"]) == {"persona", "agent_memory", "platform"}
    assert sum(row["tokens"] for row in detail["tools"]) == breakdown["tools"] + breakdown["dynamic_tools"]
    by_name = {row["name"]: row for row in detail["tools"]}
    assert (by_name["mcp_thing"]["kind"], by_name["mcp_thing"]["group"]) == ("mcp", "Acme (MCP)")
    assert (by_name["builtin_search"]["kind"], by_name["builtin_search"]["group"]) == ("builtin", None)
    assert detail["tool_results"] == {"lookup": breakdown["tool_results"]}
    compaction = detail["compaction"]
    assert compaction["approaching_tokens"] < compaction["trigger_tokens"]
    assert set(compaction) == {"gate_tokens", "approaching_tokens", "trigger_tokens", "target_tokens"}

    # And the tracker stores it with the provider's count of the same call.
    tracker = ContextUsageTracker()
    for event in events:
        tracker.observe(event)
    stored = tracker.command_meta()
    assert stored["context_detail"] == detail
    assert stored["context_provider_tokens"] == 1234


async def test_without_sections_the_system_bucket_is_left_whole():
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)

    await run_agent_loop(
        llm=_StubLLM(),
        messages=[ChatMessage(role="system", content="s" * 600), ChatMessage(role="user", content="hi")],
        max_iterations=1,
        on_event=on_event,
    )
    thinking = next(e for e in events if e.get("status") == "thinking")
    assert "system" not in thinking["context_detail"]


# ── Rendering ───────────────────────────────────────────────────────────────


def _report(**overrides) -> ContextReport:
    base = {
        "state": "measured",
        "detail": True,
        "model": "claude-x",
        "window": 200_000,
        "used_tokens": 50_000,
        "cached_tokens": 40_000,
        "new_message_tokens": 120,
        "categories": [
            ContextCategory(key="system_prompt", tokens=20_000, items=[{"name": "persona", "tokens": 20_000}]),
            ContextCategory(
                key="mcp_tools",
                tokens=30_000,
                items=[{"name": "evil|tool\n| x |", "tokens": 30_000, "group": "[srv](http://e)"}],
            ),
        ],
    }
    return ContextReport(**{**base, **overrides})


def test_the_report_renders_tables_whose_rows_keep_their_cells():
    markdown = render_context_report(_report())
    tokens = _MD.parse(markdown)
    tables = [t for t in tokens if t.type == "table_open"]
    # Used, compaction is absent here, categories, and the two detail tables.
    assert len(tables) == 4
    for t in tokens:
        if t.type == "inline":
            assert {c.type for c in t.children or []} <= {"text", "softbreak", "em_open", "em_close", "strong_open", "strong_close"}
    assert "Persona" in markdown


def test_the_compaction_level_and_its_caveat_are_two_paragraphs():
    from app.services.chat_commands.context_report import ContextCompaction

    compaction = ContextCompaction(
        level="normal",
        gate_tokens=1,
        approaching_tokens=2,
        trigger_tokens=3,
        target_tokens=3,
        free_tokens=2,
    )
    tokens = _MD.parse(render_context_report(_report(compaction=compaction)))
    paragraphs = [
        "".join(c.content for c in t.children or [])
        for t in tokens
        if t.type == "inline" and t.level == 1
    ]
    assert "Current level: normal" in paragraphs
    assert any(p.startswith("Compaction measures") for p in paragraphs)


def test_an_empty_report_says_what_to_do():
    markdown = render_context_report(ContextReport(state="none", detail=False))
    assert "No context reading yet" in markdown
    assert "table_open" not in {t.type for t in _MD.parse(markdown)}


def test_help_lists_every_command_and_argument():
    rows = _table_cells(render_help(commands_for(SURFACE_WEB)))
    first_cells = ["".join(c.content for c in row[0]) for row in rows[1:]]
    assert first_cells == ["/context [detail]", "/context detail", "/help"]


def test_the_report_flags_an_agent_edited_since_the_reading():
    assert "edited since" in render_context_report(_report(agent_changed=True))
    assert "edited since" not in render_context_report(_report())

