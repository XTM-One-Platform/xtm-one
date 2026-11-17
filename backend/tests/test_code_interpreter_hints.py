"""File hints name ``code_interpreter`` only when the turn offers it.

The footer of a capped inline attachment, the renderings the same reader serves
(an HTML page's embedded data, a large CSV, a JSON text past the parse cap), the
``query_file_json`` notes and ``read_file_content``'s window cap, last window
and description all sent the model to ``code_interpreter`` and its mount path
whether or not the tool was offered - and on an XTM One deployment it is off by
default. A model told to call a tool it lacks calls it (refused: a wasted round
and a failed call in the trace) or promises a computation it cannot run.

Every runtime text now reads ONE surface: the tools ``run_agent_loop`` was
handed, bound for its whole run (``offered_tools()``), through ONE predicate
(``code_interpreter_offered``). Outside a loop nothing is known, so nothing is
named. Where the tool IS offered every text is byte for byte what it was.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import io
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.datastructures import Headers, UploadFile

from app.llm import file_resolver
from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse, FileRef
from app.services import attachment_extract as ax
from app.services.builtin_tools import build_code_interpreter_tool, build_generate_file_tool
from app.services.code_interpreter import code_interpreter_offered, mount_path_hint
from app.tools import agent_loop as al
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    FileContext,
    ToolCall,
    ToolDefinition,
    fit_descriptions_to_surface,
    offered_tools,
    offering_tools,
)

APP_DIR = Path(__file__).resolve().parents[1] / "app"


@pytest.fixture(autouse=True)
def _fresh_render_cache():
    ax.clear_render_cache()
    yield
    ax.clear_render_cache()


def _ci_surface() -> list[ToolDefinition]:
    """A surface carrying the real built-in code interpreter definition."""
    return build_code_interpreter_tool()[0]


def _other_surface() -> list[ToolDefinition]:
    """A surface with tools, none of them the code interpreter."""
    return [ToolDefinition(name="search_knowledge", description="Search.", source="builtin:knowledge_base")]


#: The three ways a text can be produced: the tool offered, other tools
#: offered, and outside any loop (nothing known).
SURFACES = ("offered", "other", "outside")


def _surface(kind: str):
    """``with _surface(kind):`` - the surface a test case runs under."""
    if kind == "offered":
        return offering_tools(_ci_surface())
    if kind == "other":
        return offering_tools(_other_surface())
    return contextlib.nullcontext()


def _assert_names_no_code_interpreter(text: str) -> None:
    assert "code_interpreter" not in text
    assert "attachments/" not in text, "no mount path either"


def _rows(n: int) -> list[dict]:
    return [{"name": f"Acc {i}", "segment": ["Fed", "State"][i % 2], "acv": 1000 + i} for i in range(n)]


def _big_html(rows: int = 3_000) -> bytes:
    return (
        "<html><head><title>Model</title>"
        f"<script>const ROWS = {json.dumps(_rows(rows))}; function draw() {{}}</script></head>"
        "<body><h1>Coverage</h1>"
        + "".join(f"<p>Narrative paragraph {i} about the plan.</p>" for i in range(2_500))
        + "</body></html>"
    ).encode("utf-8")


def _big_csv(rows: int = 6_000) -> bytes:
    body = "\n".join(f"Acc {i},{['Fed', 'State'][i % 2]},{1000 + i}" for i in range(rows))
    return ("name,segment,acv\n" + body).encode()


# ---------------------------------------------------------------------------
# The one source of truth
# ---------------------------------------------------------------------------


def test_the_predicate_is_true_for_the_built_in_code_interpreter_only():
    """Pinned to the REAL definition: renaming the tool or its source label
    without the predicate would silently stop every hint."""
    assert code_interpreter_offered(_ci_surface()) is True
    assert code_interpreter_offered([*_other_surface(), *_ci_surface()]) is True
    assert code_interpreter_offered(None) is False
    assert code_interpreter_offered(()) is False
    assert code_interpreter_offered(_other_surface()) is False
    impostor = ToolDefinition(name="code_interpreter", description="x", source="mcp:Sandbox")
    assert code_interpreter_offered([impostor]) is False, "a same-named MCP tool mounts nothing"


def test_outside_a_loop_nothing_is_offered():
    assert offered_tools() is None
    with offering_tools(_ci_surface()):
        assert [t.name for t in offered_tools()] == ["code_interpreter"]
        with offering_tools(None):
            assert offered_tools() == ()
        assert [t.name for t in offered_tools()] == ["code_interpreter"]
    assert offered_tools() is None


class _ScriptedLLM(BaseLLMProvider):
    provider_name = "stub"
    context_window = 100_000

    def __init__(self, script: list[ChatResponse]) -> None:
        self._script = list(script)

    async def chat(self, messages, model=None, tools=None, **kwargs):
        if self._script:
            return self._script.pop(0)
        return ChatResponse(content="done", model="stub")

    async def chat_stream(self, messages, **kwargs):
        yield ""

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


@pytest.fixture
def _quiet_langfuse(monkeypatch):
    monkeypatch.setattr(al, "_lf_start_llm_gen", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_generation", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_tool_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_start_iter_span", lambda **kw: None)
    monkeypatch.setattr(al, "_lf_start_tool_span", lambda **kw: None)


def _probe_loop(name: str, seen: list) -> tuple[_ScriptedLLM, ToolDefinition, CompositeToolExecutor]:
    """A loop that calls the ``name`` tool once, which records the bound surface."""
    executor = CallableToolExecutor()

    async def _probe(**_kwargs):
        seen.append(offered_tools())
        return "ok"

    executor.register(name, _probe)
    definition = ToolDefinition(name=name, description="Probe.", source="test")
    composite = CompositeToolExecutor()
    composite.add_tools([definition], executor)
    llm = _ScriptedLLM(
        [
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_calls",
                tool_calls=[ToolCall(id=f"call-{name}", name=name, arguments={})],
            ),
            ChatResponse(content="done", model="stub"),
        ]
    )
    return llm, definition, composite


@pytest.mark.asyncio
async def test_the_loop_binds_the_tools_it_was_handed_for_its_whole_run(_quiet_langfuse):
    seen: list = []
    llm, probe, composite = _probe_loop("probe", seen)
    tools = [probe, *_ci_surface()]
    await al.run_agent_loop(
        llm, [ChatMessage(role="user", content="go")], tools, composite, max_iterations=10, progressive_tools=False
    )
    assert [t.name for t in seen[0]] == ["probe", "code_interpreter"]
    assert offered_tools() is None, "the binding ends with the run"


@pytest.mark.asyncio
async def test_a_nested_run_in_the_callers_task_hands_its_surface_back(_quiet_langfuse):
    """A consult or a transfer runs its own loop, possibly in the caller's
    task: inside it the nested surface, after it the caller's again."""
    seen: list = []
    llm, probe, composite = _probe_loop("inner_probe", seen)
    outer = _ci_surface()
    with offering_tools(outer):
        await al.run_agent_loop(
            llm, [ChatMessage(role="user", content="q")], [probe], composite, max_iterations=10, progressive_tools=False
        )
        assert [t.name for t in seen[0]] == ["inner_probe"]
        assert offered_tools() == tuple(outer)


def _test_tools(*handlers) -> tuple[list[ToolDefinition], CompositeToolExecutor]:
    """``(name, handler)`` pairs as one run's definitions and executor."""
    executor = CallableToolExecutor()
    definitions = []
    for name, handler in handlers:
        executor.register(name, handler)
        definitions.append(
            ToolDefinition(
                name=name,
                description=f"{name}.",
                input_schema={"type": "object", "properties": {"n": {"type": "integer"}}},
                source="test",
            )
        )
    composite = CompositeToolExecutor()
    composite.add_tools(definitions, executor)
    return definitions, composite


def _calling(*calls: tuple[str, dict]) -> ChatResponse:
    """One model response calling every ``(name, arguments)`` of *calls* - several make a parallel batch."""
    return ChatResponse(
        content="",
        model="stub",
        stop_reason="tool_calls",
        tool_calls=[ToolCall(id=f"call-{name}-{uuid.uuid4().hex[:8]}", name=name, arguments=args) for name, args in calls],
    )


def _names(surface) -> list[str]:
    return [tool.name for tool in surface]


@pytest.mark.asyncio
async def test_two_concurrent_runs_each_read_their_own_surface(_quiet_langfuse):
    """Both runs are inside a tool at the same time, and each reads its own
    surface before and after the other one bound its."""
    arrived = {"a": asyncio.Event(), "b": asyncio.Event()}
    read = {"a": asyncio.Event(), "b": asyncio.Event()}
    seen: dict[str, list] = {"a": [], "b": []}

    def _probe(me: str, other: str):
        async def _handler(**_kwargs):
            seen[me].append(offered_tools())
            arrived[me].set()
            await asyncio.wait_for(arrived[other].wait(), 5)
            seen[me].append(offered_tools())
            read[me].set()
            # Neither run ends - handing a surface back - before both read twice.
            await asyncio.wait_for(read[other].wait(), 5)
            return "ok"

        return _handler

    async def _run(me: str, other: str, extra: list[ToolDefinition]) -> None:
        definitions, composite = _test_tools((f"probe_{me}", _probe(me, other)))
        llm = _ScriptedLLM([_calling((f"probe_{me}", {})), ChatResponse(content="done", model="stub")])
        await al.run_agent_loop(
            llm,
            [ChatMessage(role="user", content=me)],
            [*definitions, *extra],
            composite,
            max_iterations=5,
            progressive_tools=False,
        )

    await asyncio.gather(_run("a", "b", _ci_surface()), _run("b", "a", []))
    assert [_names(surface) for surface in seen["a"]] == [["probe_a", "code_interpreter"]] * 2
    assert [_names(surface) for surface in seen["b"]] == [["probe_b"]] * 2
    assert offered_tools() is None


@pytest.mark.asyncio
async def test_a_nested_run_in_one_call_of_a_parallel_batch_stays_in_that_call(_quiet_langfuse):
    """A consult running its own loop in one call of a batch: the sibling call
    reads the run's surface while the nested one is bound, and the consulting
    call gets the run's surface back when the nested loop returns."""
    nested_bound = asyncio.Event()
    sibling_read = asyncio.Event()
    seen: dict[str, tuple[ToolDefinition, ...] | None] = {}

    async def _inner(**_kwargs):
        seen["nested"] = offered_tools()
        nested_bound.set()
        await asyncio.wait_for(sibling_read.wait(), 5)
        return "inner ok"

    inner_definitions, inner_composite = _test_tools(("inner_probe", _inner))

    async def _consult(**_kwargs):
        seen["consult_before"] = offered_tools()
        llm = _ScriptedLLM([_calling(("inner_probe", {})), ChatResponse(content="advice", model="stub")])
        await al.run_agent_loop(
            llm,
            [ChatMessage(role="user", content="q")],
            inner_definitions,
            inner_composite,
            max_iterations=5,
            progressive_tools=False,
        )
        seen["consult_after"] = offered_tools()
        return "consulted"

    async def _sibling(**_kwargs):
        await asyncio.wait_for(nested_bound.wait(), 5)
        seen["sibling"] = offered_tools()
        sibling_read.set()
        return "ok"

    definitions, composite = _test_tools(("consult", _consult), ("sibling", _sibling))
    llm = _ScriptedLLM([_calling(("consult", {}), ("sibling", {})), ChatResponse(content="done", model="stub")])
    await al.run_agent_loop(
        llm,
        [ChatMessage(role="user", content="go")],
        [*definitions, *_ci_surface()],
        composite,
        max_iterations=5,
        progressive_tools=False,
    )
    run_surface = ["consult", "sibling", "code_interpreter"]
    assert _names(seen["consult_before"]) == run_surface
    assert _names(seen["nested"]) == ["inner_probe"]
    assert _names(seen["sibling"]) == run_surface, "a sibling call never reads the nested surface"
    assert _names(seen["consult_after"]) == run_surface
    assert offered_tools() is None


@pytest.mark.asyncio
async def test_the_bound_surface_is_the_whole_inventory_across_a_load_tools_reveal(_quiet_langfuse, monkeypatch):
    """Progressive disclosure changes what a request CARRIES, never what the
    run offers: a gated tool is bound before it is revealed, and a reveal
    leaves the binding as it was, so a text renders the same all run long."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "progressive_tools_min_tools", 1)
    seen: list = []

    async def _probe(**_kwargs):
        seen.append(offered_tools())
        return "ok"

    definitions, composite = _test_tools(("probe", _probe))
    gated_executor = CallableToolExecutor()

    async def _search(**_kwargs):
        return "found"

    gated_executor.register("srv_search", _search)
    gated = ToolDefinition(name="srv_search", description="Search the server.", source="mcp:srv")
    composite.add_tools([gated], gated_executor)
    llm = _RecordingLLM(
        [
            _calling(("probe", {"n": 1})),
            _calling(("load_tools", {"groups": ["mcp:srv"]})),
            _calling(("probe", {"n": 2})),
            ChatResponse(content="done", model="stub"),
        ]
    )
    await al.run_agent_loop(
        llm,
        [ChatMessage(role="user", content="go")],
        [*definitions, gated, *_ci_surface()],
        composite,
        max_iterations=10,
        progressive_tools=True,
    )
    first_request = _names(llm.offered[0])
    assert "load_tools" in first_request and "srv_search" not in first_request, "the gate was active"
    assert "srv_search" in _names(llm.offered[2]), "and the reveal happened"
    assert [_names(surface) for surface in seen] == [["probe", "srv_search", "code_interpreter"]] * 2


class _Abort(BaseException):
    """Escapes every ``except Exception`` of the loop, as a cancellation does."""


@pytest.mark.asyncio
async def test_a_run_that_raises_hands_the_callers_surface_back(_quiet_langfuse):
    seen: list = []

    class _RaisingLLM(_ScriptedLLM):
        async def chat(self, messages, model=None, tools=None, **kwargs):
            seen.append(offered_tools())
            raise _Abort

    outer = _ci_surface()
    with offering_tools(outer):
        with pytest.raises(_Abort):
            await al.run_agent_loop(
                _RaisingLLM([]),
                [ChatMessage(role="user", content="go")],
                _other_surface(),
                None,
                max_iterations=3,
                progressive_tools=False,
            )
        assert _names(seen[0]) == ["search_knowledge"], "the provider call ran under the run's surface"
        assert offered_tools() == tuple(outer)


@pytest.mark.asyncio
async def test_a_cancelled_run_hands_the_callers_surface_back(_quiet_langfuse):
    started = asyncio.Event()

    class _HangingLLM(_ScriptedLLM):
        async def chat(self, messages, model=None, tools=None, **kwargs):
            started.set()
            await asyncio.Event().wait()

    outer = _ci_surface()
    after: list = []

    async def _caller() -> None:
        with offering_tools(outer):
            try:
                await al.run_agent_loop(
                    _HangingLLM([]),
                    [ChatMessage(role="user", content="go")],
                    _other_surface(),
                    None,
                    max_iterations=3,
                    progressive_tools=False,
                )
            finally:
                after.append(offered_tools())

    task = asyncio.create_task(_caller())
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert after == [tuple(outer)]


# ---------------------------------------------------------------------------
# The footer
# ---------------------------------------------------------------------------


def _footer(*, code_interpreter: bool) -> str:
    return ax.inline_truncation_footer(
        file_id="f1",
        filename="plan.html",
        content_type="text/html",
        shown=80_000,
        total=213_456,
        code_interpreter=code_interpreter,
    )


def test_the_footer_names_the_code_interpreter_only_when_it_is_offered():
    lead = (
        "[... truncated: the first 80,000 of 213,456 characters are shown. Answer from what is shown and say "
        "what you could not read; do not page through the rest to answer a question about this document. A "
        'specific later part: read_file_content(file_id="f1", mode="extract_structured", offset=80000).'
    )
    # Byte for byte the footer when the tool is offered.
    assert _footer(code_interpreter=True) == (
        lead + " A computation over the whole file (every row, counts, sums): code_interpreter, where it is "
        f"mounted at {mount_path_hint('plan.html', 'f1')}.]"
    )
    assert _footer(code_interpreter=False) == lead + "]"


def test_the_footer_has_no_default_that_names_the_tool():
    with pytest.raises(TypeError):
        ax.inline_truncation_footer(file_id="f1", filename="a.csv", content_type="text/csv", shown=1, total=2)


def _calls_in_app(function: str) -> list[tuple[Path, ast.Call]]:
    calls: list[tuple[Path, ast.Call]] = []
    for path in APP_DIR.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if f"{function}(" not in source:
            continue
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name == function:
                calls.append((path, node))
    return calls


def test_no_caller_of_the_footer_omits_the_decision():
    calls = _calls_in_app("inline_truncation_footer")
    places = {path.name for path, _ in calls}
    assert {"file_resolver.py", "chat.py"} <= places, places
    for path, node in calls:
        assert any(keyword.arg == "code_interpreter" for keyword in node.keywords), f"{path}:{node.lineno}"


# ---------------------------------------------------------------------------
# The inline fallback (provider layer)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
async def test_the_inline_fallback_of_an_html_page(surface):
    ref = FileRef.from_bytes(_big_html(), "Model TD.html", "text/html")
    with _surface(surface):
        text = (await file_resolver._extract_fallback(ref)).text
    footer = text.rsplit("\n\n[... truncated: ", 1)[1]
    assert f'read_file_content(file_id="{ref.file_id}", mode="extract_structured", offset=80000)' in footer
    if surface == "offered":
        assert f"code_interpreter, where it is mounted at {mount_path_hint(ref.filename, ref.file_id)}.]" in footer
        assert "instead of the source; for a computation over every row use code_interpreter." in text
    else:
        _assert_names_no_code_interpreter(text)
        assert footer.endswith("offset=80000).]")
        assert "read one value with query_file_json(file_id, path, block=N) instead of the source." in text


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
async def test_the_inline_fallback_of_a_large_csv(surface):
    ref = FileRef.from_bytes(_big_csv(), "accounts.csv", "text/csv")
    with _surface(surface):
        text = (await file_resolver._extract_fallback(ref)).text
    if surface == "offered":
        assert ", then the file itself; compute over every row with code_interpreter]\nCSV: 6,000" in text
    else:
        _assert_names_no_code_interpreter(text)
        assert ", then the file itself]\nCSV: 6,000 data rows" in text


def test_a_rendering_is_memoised_per_surface():
    """The flag is part of the memo key: a turn without the tool never gets
    the rendering a turn with it produced."""
    data = _big_csv()
    offered = ax.extract_attachment_text(data, "accounts.csv", "text/csv", True)
    without = ax.extract_attachment_text(data, "accounts.csv", "text/csv", False)
    assert "code_interpreter" in offered
    _assert_names_no_code_interpreter(without)
    assert ax.extract_attachment_text(data, "accounts.csv", "text/csv", True) == offered
    assert ax.extract_attachment_text(data, "accounts.csv", "text/csv") == without, "a caller that cannot know"


# ---------------------------------------------------------------------------
# The renderings and the query results, byte for byte when offered
# ---------------------------------------------------------------------------


def test_the_embedded_data_section_when_blocks_are_dropped(monkeypatch):
    monkeypatch.setattr(ax, "MAX_EMBEDDED_BLOCKS", 2)
    scripts = "".join(f'<script type="application/json">{json.dumps({"k": i})}</script>' for i in range(5))
    page = f"<html><body>{scripts}<p>t</p></body></html>"
    offered = ax.render_html_attachment(page, code_interpreter=True)
    assert (
        "2 data blocks found in the page's scripts (JSON <script> elements and JavaScript literals) - read one "
        "value with query_file_json(file_id, path, block=N) instead of the source; for a computation over "
        "every row use code_interpreter.\n3 more data blocks past the first 2 were not read - they are in the "
        "file (code_interpreter reads it whole), not in this section or in query_file_json."
    ) in offered
    without = ax.render_html_attachment(page)
    _assert_names_no_code_interpreter(without)
    assert (
        "instead of the source.\n3 more data blocks past the first 2 were not read - they are in the file, not "
        "in this section or in query_file_json."
    ) in without

    query, is_error = ax.run_json_query(page.encode(), "p.html", "text/html", "$.k", None, True)
    assert not is_error
    assert query.startswith(
        "[Partial: 3 more data blocks past the first 2 were not read - a value there is not in these results; "
        "code_interpreter reads the whole file.]"
    )
    query, _ = ax.run_json_query(page.encode(), "p.html", "text/html", "$.k", None)
    assert query.startswith(
        "[Partial: 3 more data blocks past the first 2 were not read - a value there is not in these results.]"
    )
    _assert_names_no_code_interpreter(query)


def test_script_past_the_scan_budget(monkeypatch):
    monkeypatch.setattr(ax, "_JS_SCAN_MAX_CHARS", 1_000)
    page = "<script>" + "var x = 1;\n" * 500 + "</script><p>Body.</p>"
    offered = ax.render_html_attachment(page, code_interpreter=True)
    assert "chars past the scan budget were not read - code_interpreter reads the whole file.\n" in offered
    without = ax.render_html_attachment(page)
    assert "chars past the scan budget were not read.\n" in without
    _assert_names_no_code_interpreter(without)


def test_a_json_text_past_the_parse_cap(monkeypatch):
    monkeypatch.setattr(ax, "JSON_PARSE_MAX_CHARS", 1_000)
    monkeypatch.setattr(ax, "JSON_VERBATIM_MAX_CHARS", 100)
    doc = json.dumps([{"id": i, "v": "x" * 20} for i in range(100)])
    reason = f"too large to parse here ({len(doc):,} chars; the limit is 1,000)"
    assert ax.render_json_attachment(doc, code_interpreter=True).startswith(
        f"[JSON document, {len(doc):,} chars not summarised - {reason} - code_interpreter reads it whole; "
        "shown as its text]\n"
    )
    without = ax.render_json_attachment(doc)
    assert without.startswith(f"[JSON document, {len(doc):,} chars not summarised - {reason}; shown as its text]\n")
    query, _ = ax.run_json_query(doc.encode(), "d.json", "application/json", "$[0]", None, True)
    assert f"skipped: {reason} - code_interpreter reads it whole" in query
    query, _ = ax.run_json_query(doc.encode(), "d.json", "application/json", "$[0]", None)
    assert query.endswith(f"skipped: {reason}")
    page = f'<script type="application/json">{doc}</script><p>t</p>'
    assert f"(skipped: {reason} - code_interpreter reads it whole)" in ax.render_html_attachment(
        page, code_interpreter=True
    )
    assert f"(skipped: {reason})" in ax.render_html_attachment(page)


def test_a_partial_search_and_a_partial_block(monkeypatch):
    offered = ax.format_query_result([], path="$..x", exhausted=True, visit_budget=50, code_interpreter=True)
    assert offered.startswith(
        "[Partial search: the visit budget (50 values) ran out - a match past that point is not in this result; "
        "narrow the path, or compute over the file with code_interpreter.]\n"
    )
    without = ax.format_query_result([], path="$..x", exhausted=True, visit_budget=50)
    assert without.startswith(
        "[Partial search: the visit budget (50 values) ran out - a match past that point is not in this result; "
        "narrow the path.]\n"
    )
    # A block read in part (a JSON Lines file past its record cap).
    monkeypatch.setattr(ax, "JSONL_MAX_RECORDS", 100)
    raw = "\n".join(json.dumps({"id": i}) for i in range(200)).encode()
    offered, _ = ax.run_json_query(raw, "events.jsonl", None, "$[*].missing", None, True)
    assert "[Partial block: " in offered
    assert " - a value there is not in this result; code_interpreter reads the whole file.]\n" in offered
    without, _ = ax.run_json_query(raw, "events.jsonl", None, "$[*].missing", None)
    assert "[Partial block: " in without and " - a value there is not in this result.]\n" in without
    _assert_names_no_code_interpreter(without)


# ---------------------------------------------------------------------------
# read_file_content and query_file_json
# ---------------------------------------------------------------------------


def _generated_ref(data: bytes, filename: str, content_type: str) -> FileRef:
    ref = FileRef.from_bytes(data, filename, content_type)
    ref.source = "agent_download"
    return ref


def _file_tools(*refs: FileRef):
    defs, executor = build_generate_file_tool(db=None, user_id=str(uuid.uuid4()), conversation_id=str(uuid.uuid4()))
    ctx = FileContext()
    for ref in refs:
        ctx.add(ref)
    executor.file_context = ctx
    return defs, executor


async def _call(executor, name: str, **arguments):
    return await executor.execute_tool(ToolCall(id=f"call-{name}", name=name, arguments=arguments))


_WITH_THE_TOOL = "A few windows per file are served each turn: to compute over a whole file use code_interpreter."
_WITHOUT_IT = "A few windows per file are served each turn."


def _read_description(tools) -> str:
    return next(d for d in tools if d.name == "read_file_content").description


def test_the_read_tool_description_is_resolved_against_the_final_surface():
    """The description is decided from the list the loop is handed - after
    every registration, so a same-named MCP tool that replaced the built-in
    (the composite is last-write-wins) takes the pointer away too."""
    defs, _executor = _file_tools()
    assert _read_description(defs).endswith(_WITH_THE_TOOL), "a catalog lists the tool unchanged"
    offered = fit_descriptions_to_surface([*defs, *_ci_surface()])
    assert _read_description(offered).endswith(_WITH_THE_TOOL)
    assert next(d for d in offered if d.name == "read_file_content").describe_for_surface is None, "resolved once"
    without = fit_descriptions_to_surface(list(defs))
    assert _read_description(without).endswith(_WITHOUT_IT) and "code_interpreter" not in _read_description(without)
    impostor = ToolDefinition(name="code_interpreter", description="x", source="mcp:Sandbox")
    assert _read_description(fit_descriptions_to_surface([*defs, impostor])).endswith(_WITHOUT_IT)
    plain = _other_surface()
    assert fit_descriptions_to_surface(plain) is plain, "nothing depends on the surface: the list itself"


class _RecordingLLM(_ScriptedLLM):
    def __init__(self, script: list[ChatResponse]) -> None:
        super().__init__(script)
        self.offered: list[list[ToolDefinition]] = []

    async def chat(self, messages, model=None, tools=None, **kwargs):
        self.offered.append(list(tools or []))
        return await super().chat(messages, model=model, tools=tools, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("offered", [True, False])
async def test_the_loop_sends_the_description_its_surface_calls_for(offered, _quiet_langfuse):
    defs, executor = _file_tools()
    composite = CompositeToolExecutor()
    composite.add_tools(defs, executor)
    if offered:
        composite.add_tools(*build_code_interpreter_tool())
    llm = _RecordingLLM([ChatResponse(content="done", model="stub")])
    await al.run_agent_loop(
        llm,
        [ChatMessage(role="user", content="hi")],
        composite.all_tools,
        composite,
        max_iterations=10,
        progressive_tools=False,
    )
    sent = _read_description(llm.offered[0])
    assert sent.endswith(_WITH_THE_TOOL if offered else _WITHOUT_IT)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
async def test_the_rendered_view_served_by_read_file_content(surface):
    ref = _generated_ref(_big_html(rows=50), "plan.html", "text/html")
    _defs, executor = _file_tools(ref)
    with _surface(surface):
        rendered = await _call(executor, "read_file_content", file_id=ref.file_id, mode="extract_structured")
    assert rendered.is_error is False and "## Embedded data" in rendered.content
    if surface == "offered":
        assert "for a computation over every row use code_interpreter." in rendered.content
    else:
        _assert_names_no_code_interpreter(rendered.content)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
async def test_what_query_file_json_could_not_read(surface, monkeypatch):
    monkeypatch.setattr(ax, "MAX_EMBEDDED_BLOCKS", 1)
    scripts = "".join(f'<script type="application/json">{json.dumps({"k": i})}</script>' for i in range(3))
    ref = _generated_ref(f"<html><body>{scripts}</body></html>".encode(), "p.html", "text/html")
    _defs, executor = _file_tools(ref)
    with _surface(surface):
        result = await _call(executor, "query_file_json", file_id=ref.file_id, path="$.k")
    assert result.is_error is False and "[Partial: 2 more data blocks past the first 1 were not read" in result.content
    if surface == "offered":
        assert "a value there is not in these results; code_interpreter reads the whole file.]" in result.content
    else:
        assert "a value there is not in these results.]" in result.content
        _assert_names_no_code_interpreter(result.content)


def _cap(monkeypatch, value: int) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "agent_loop_read_windows_per_file", value)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
async def test_the_last_window_and_the_window_cap(surface, monkeypatch):
    _cap(monkeypatch, 1)
    text = "".join(f"line {i:05d}\n" for i in range(2_000))
    ref = _generated_ref(text.encode(), "big.txt", "text/plain")
    _defs, executor = _file_tools(ref)
    mount = mount_path_hint(ref.filename, ref.file_id)
    with _surface(surface):
        last = await _call(executor, "read_file_content", file_id=ref.file_id, max_chars=1_000)
        refused = await _call(executor, "read_file_content", file_id=ref.file_id, offset=1_000, max_chars=1_000)
    assert refused.is_error is True
    assert refused.content.startswith(
        "Error: window budget for this file exhausted this turn, 1 of 1 served ('big.txt', "
    )
    remain = f"[TRUNCATED - {len(text) - 1_000:,} characters remain and this was the last window this turn allows for"
    if surface == "offered":
        assert (
            f"{remain} this file. Answer from what you have and say what you could not read; to compute over the "
            f"whole file use code_interpreter (mounted at {mount}).]"
        ) in last.content
        assert refused.content.endswith(
            "Answer from what you have and say what you could not read; use code_interpreter to process the "
            f"whole file (every row, counts, sums), where it is mounted at {mount}."
        )
    else:
        assert last.content.endswith(
            f"{remain} this file. Answer from what you have and say what you could not read.]"
        )
        assert refused.content.endswith("Answer from what you have and say what you could not read.")
        _assert_names_no_code_interpreter(last.content)
        _assert_names_no_code_interpreter(refused.content)


@pytest.mark.asyncio
async def test_the_refusal_still_offers_the_rendered_view_without_the_tool(monkeypatch):
    _cap(monkeypatch, 1)
    page = "<html><body>" + "".join(f"<p>Paragraph {i} of the plan.</p>" for i in range(3_000)) + "</body></html>"
    ref = _generated_ref(page.encode(), "plan.html", "text/html")
    _defs, executor = _file_tools(ref)
    with offering_tools(_other_surface()):
        await _call(executor, "read_file_content", file_id=ref.file_id, max_chars=1_000)
        refused = await _call(executor, "read_file_content", file_id=ref.file_id, offset=1_000, max_chars=1_000)
    assert refused.is_error is True
    assert refused.content.endswith(
        "say what you could not read. For the document as rendered text with its data summarised, read it with "
        'mode="extract_structured".'
    )
    _assert_names_no_code_interpreter(refused.content)


# ---------------------------------------------------------------------------
# The chat upload message
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _UploadDb:
    """The session the upload route talks to, keyed on the entity selected."""

    def __init__(self, rows: dict) -> None:
        self._rows = rows
        self.added: list = []
        self.selected: list = []

    async def execute(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        self.selected.append(entity)
        return _Result(self._rows.get(entity))

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    async def commit(self) -> None:
        return None


def _conversation(agent_id=None, overrides: dict | None = None):
    """A conversation, with a selection saved for its agent when *overrides* are given."""
    saved = {**overrides, "agent_id": str(agent_id) if agent_id else None} if overrides is not None else None
    return SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        tool_overrides_json=json.dumps(saved) if saved is not None else None,
    )


def _ga(*builtin_ids: str):
    from app.models.general_assistant_config import GeneralAssistantConfig

    return {GeneralAssistantConfig: SimpleNamespace(builtin_tool_ids=list(builtin_ids))}


def _agent_rows(agent_id, builtin_ids: list[str], deny: list[str] | None = None, *, enabled: bool = True):
    from app.models.agent import Agent

    return {
        Agent: SimpleNamespace(
            id=agent_id, enabled=enabled, builtin_tool_ids=builtin_ids, tool_deny_list=deny, knowledge_base_ids=None
        )
    }


def _user(ga_builtin_tool_ids=None):
    return SimpleNamespace(id=uuid.uuid4(), ga_builtin_tool_ids=ga_builtin_tool_ids)


def _upload_db(conv, rows: dict) -> _UploadDb:
    from app.models.chat import Conversation

    return _UploadDb({Conversation: conv, **rows})


async def _upload(
    monkeypatch,
    conv,
    rows: dict,
    user,
    *,
    data: bytes | None = None,
    filename: str = "accounts.csv",
    content_type: str = "text/csv",
    db: _UploadDb | None = None,
) -> str:
    from app.api.v1 import chat
    from app.services import file_storage as storage

    async def _stored(**_kwargs):
        return "chat/conv/accounts.csv"

    stored: dict = {}

    async def _store_message(_db, _conversation_id, **kwargs):
        stored.update(kwargs)
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(storage.file_storage, "upload", _stored)
    monkeypatch.setattr(chat, "store_message", _store_message)
    upload = UploadFile(
        file=io.BytesIO(_big_csv() if data is None else data),
        filename=filename,
        headers=Headers({"content-type": content_type}),
    )
    db = db or _upload_db(conv, rows)
    await chat.upload_file_to_conversation(
        str(conv.id), file=upload, create_message=True, user=user, db=db
    )
    from app.llm.file_resolver import upload_header
    from app.models.file_attachment import FileAttachment

    # The message names the file by its id, its name fenced; the tests
    # here read what follows that header.
    (attachment,) = [obj for obj in db.added if isinstance(obj, FileAttachment)]
    header = upload_header(str(attachment.id), filename) + "\n\n"
    assert stored["content"].startswith(header)
    return stored["content"].removeprefix(header)


@pytest.mark.asyncio
@pytest.mark.parametrize("offered", [True, False])
async def test_the_upload_message_of_a_general_assistant_conversation(offered, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "disable_code_interpreter", False)
    rows = _ga("code_interpreter", "generate_file") if offered else _ga("generate_file")
    content = await _upload(monkeypatch, _conversation(), rows, _user())
    assert content.startswith("[CSV document,")
    assert "\n\n[... truncated: the first 50,000 of" in content
    if offered:
        assert "then the file itself; compute over every row with code_interpreter]" in content
        assert "code_interpreter, where it is mounted at attachments/accounts-" in content
    else:
        _assert_names_no_code_interpreter(content)
        assert content.endswith(", offset=50000).]")


@pytest.mark.asyncio
async def test_the_upload_message_honours_the_deployment_switch(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "disable_code_interpreter", True)
    content = await _upload(monkeypatch, _conversation(), _ga("code_interpreter"), _user())
    _assert_names_no_code_interpreter(content)


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_conversation", [False, True])
async def test_an_upload_no_text_of_which_can_name_the_tool_reads_nothing_for_it(agent_conversation, monkeypatch):
    """Only a rendering or a cut names the code interpreter: a plain text file
    within the inline cap is neither, so the upload reads no General Assistant
    configuration and no extra agent row for a decision nothing would use -
    the agent row it reads is the knowledge-base step's."""
    from app.core.config import settings
    from app.models.agent import Agent
    from app.models.general_assistant_config import GeneralAssistantConfig

    monkeypatch.setattr(settings, "disable_code_interpreter", False)
    agent_id = uuid.uuid4()
    conv = _conversation(agent_id if agent_conversation else None)
    rows = _agent_rows(agent_id, ["code_interpreter"]) if agent_conversation else _ga("code_interpreter")
    db = _upload_db(conv, rows)
    notes = "Meeting notes: the FY27 plan keeps three pods and a 40% growth target.\n" * 20
    content = await _upload(
        monkeypatch, conv, rows, _user(), data=notes.encode(), filename="notes.txt", content_type="text/plain", db=db
    )
    assert content.startswith("Meeting notes:") and "[... truncated" not in content
    assert db.selected.count(GeneralAssistantConfig) == 0
    assert db.selected.count(Agent) == (1 if agent_conversation else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_conversation", [False, True])
async def test_a_structured_upload_within_the_inline_cap_is_carried_as_its_source(agent_conversation, monkeypatch):
    """An HTML page whose text fits the message cap is carried as its
    source, markup included - not rendered, so nothing names
    the code interpreter and no General Assistant configuration is read."""
    from app.core.config import settings
    from app.models.agent import Agent
    from app.models.general_assistant_config import GeneralAssistantConfig

    monkeypatch.setattr(settings, "disable_code_interpreter", False)
    agent_id = uuid.uuid4()
    conv = _conversation(agent_id if agent_conversation else None)
    rows = _agent_rows(agent_id, ["code_interpreter"]) if agent_conversation else _ga("code_interpreter")
    db = _upload_db(conv, rows)
    page = (
        "<html><head><title>Invoice</title><style>.total{color:#0015A8}</style></head><body><h1>Invoice</h1>"
        + "<p>One line item of the invoice.</p>" * 20
        + "</body></html>"
    )
    content = await _upload(
        monkeypatch, conv, rows, _user(), data=page.encode(), filename="invoice.html", content_type="text/html", db=db
    )
    assert content == page
    assert db.selected.count(GeneralAssistantConfig) == 0
    assert db.selected.count(Agent) == (1 if agent_conversation else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("paragraphs", [20, 3_000])
async def test_a_structured_upload_is_extracted_once(paragraphs, monkeypatch):
    """The upload extracted a structured file for its message,
    then the inline rule extracted it again before deciding whether to render
    it - a second full decode of up to 40 MB on the parse executor before the
    message could be created. The rule decides from the upload's own
    extraction, and the inline fallback of the next provider request reads
    what the upload learnt instead of the file."""
    from app.core.config import settings
    from app.llm import file_resolver
    from app.llm.base import FileRef
    from app.services import chunking

    monkeypatch.setattr(settings, "disable_code_interpreter", False)
    real_extract = chunking.extract_text_from_file
    calls: list[str] = []

    def _extract(content: bytes, filename: str) -> str:
        calls.append(filename)
        return real_extract(content, filename)

    monkeypatch.setattr(chunking, "extract_text_from_file", _extract)
    page = "<html><body><h1>Invoice</h1>" + "<p>One line item of the invoice.</p>" * paragraphs + "</body></html>"
    conv = _conversation()
    rows = _ga("generate_file")
    db = _upload_db(conv, rows)
    content = await _upload(
        monkeypatch, conv, rows, _user(), data=page.encode(), filename="invoice.html", content_type="text/html", db=db
    )
    assert calls == ["invoice.html"]
    assert (content == page) is (len(page) <= 50_000)
    await file_resolver._extract_fallback(FileRef.from_bytes(page.encode(), "invoice.html", "text/html"))
    assert calls == ["invoice.html"], "the next request's inline fallback extracts nothing"


@pytest.mark.asyncio
@pytest.mark.parametrize("offered", [True, False])
async def test_a_plain_text_upload_past_the_inline_cap_decides_for_its_footer(offered, monkeypatch):
    from app.core.config import settings
    from app.models.general_assistant_config import GeneralAssistantConfig

    monkeypatch.setattr(settings, "disable_code_interpreter", False)
    conv = _conversation()
    rows = _ga("code_interpreter") if offered else _ga("generate_file")
    db = _upload_db(conv, rows)
    export = "".join(f"line {i:06d} of the export\n" for i in range(3_000))
    content = await _upload(
        monkeypatch, conv, rows, _user(), data=export.encode(), filename="export.txt", content_type="text/plain", db=db
    )
    assert "\n\n[... truncated: the first 50,000 of" in content
    assert db.selected.count(GeneralAssistantConfig) == 1
    if offered:
        assert "code_interpreter, where it is mounted at attachments/export-" in content
    else:
        _assert_names_no_code_interpreter(content)
        assert content.endswith(", offset=50000).]")


@pytest.mark.asyncio
async def test_the_upload_message_of_an_agent_conversation(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "disable_code_interpreter", False)
    agent_id = uuid.uuid4()
    offered = await _upload(monkeypatch, _conversation(agent_id), _agent_rows(agent_id, ["code_interpreter"]), _user())
    assert "code_interpreter, where it is mounted at" in offered
    denied = await _upload(
        monkeypatch,
        _conversation(agent_id),
        _agent_rows(agent_id, ["code_interpreter"], deny=["code_interpreter"]),
        _user(),
    )
    _assert_names_no_code_interpreter(denied)


@pytest.mark.asyncio
async def test_the_upload_decision_reads_the_next_turns_selection(monkeypatch):
    """What the next turn's entry point reads: the conversation's saved
    built-in selection over the agent's own list, the user's General
    Assistant override over the platform one."""
    from app.api.v1.chat import _upload_offers_code_interpreter
    from app.core.config import settings

    monkeypatch.setattr(settings, "disable_code_interpreter", False)
    agent_id = uuid.uuid4()
    agent = _agent_rows(agent_id, ["code_interpreter"])
    saved_without = _conversation(agent_id, {"builtin_tool_ids": ["generate_file"]})
    assert await _upload_offers_code_interpreter(_UploadDb(agent), saved_without, _user()) is False
    saved_with = _conversation(agent_id, {"builtin_tool_ids": ["code_interpreter"]})
    assert await _upload_offers_code_interpreter(_UploadDb(_agent_rows(agent_id, [])), saved_with, _user()) is True
    assert await _upload_offers_code_interpreter(_UploadDb({}), _conversation(agent_id), _user()) is False
    disabled = _UploadDb(_agent_rows(agent_id, ["code_interpreter"], enabled=False))
    assert await _upload_offers_code_interpreter(disabled, _conversation(agent_id), _user()) is False, "runs no turn"
    ga = _UploadDb(_ga("code_interpreter"))
    assert await _upload_offers_code_interpreter(ga, _conversation(), _user()) is True
    assert await _upload_offers_code_interpreter(ga, _conversation(), _user(["generate_file"])) is False
    assert await _upload_offers_code_interpreter(_UploadDb(_ga()), _conversation(), _user(["code_interpreter"])) is True
    assert await _upload_offers_code_interpreter(_UploadDb({}), _conversation(), _user()) is False
