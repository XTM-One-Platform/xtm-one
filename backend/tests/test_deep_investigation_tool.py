"""Deep Investigation as a tool an agent can call.

Three properties, each of which is a way the tool could look like it works
while quietly breaking something the composer path already gets right.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.builtin_tools.investigation import build_deep_investigation_tool


def _run(monkeypatch, *, text: str, result=None, capture: dict | None = None):
    """Replace the metered wrapper and record how it was called."""
    import app.services.deep_investigation_chat as dic

    async def _turn(_db, **kwargs):
        if capture is not None:
            capture.update(kwargs)
        if result is not None and kwargs.get("result_out") is not None:
            kwargs["result_out"].append(result)
        return text

    monkeypatch.setattr(dic, "run_deep_investigation_turn", _turn, raising=True)


def _result(investigation_id: str):
    return SimpleNamespace(investigation=SimpleNamespace(id=investigation_id))


async def _call(tool, **kwargs):
    """Invoke the tool the way the agent loop does."""
    import uuid

    from app.tools.base import ToolCall

    _defs, executor = tool
    result = await executor.execute_tool(
        ToolCall(id=str(uuid.uuid4()), name="deep_investigation", arguments=kwargs)
    )
    return str(result.content or "")


@pytest.mark.asyncio
async def test_the_run_goes_through_the_metered_wrapper(monkeypatch) -> None:
    """Never around it.

    The wrapper owns the agentic quota check and the usage increment. A tool
    that called the orchestrator directly would let an agent spend
    investigations nobody counted — which is exactly what the WebSocket branch
    did once by routing around this same function.
    """
    seen: dict = {}
    _run(monkeypatch, text="report", result=_result("i-1"), capture=seen)

    tool = build_deep_investigation_tool(None, "user-1", conversation_id="c-1")
    await _call(tool, subject="APT29")

    assert seen["subject"] == "APT29"
    assert seen["user_id"] == "user-1"
    assert seen["conversation_id"] == "c-1"


@pytest.mark.asyncio
async def test_the_block_is_attached_from_the_run_not_from_the_model(monkeypatch) -> None:
    """The id travels out of band; the marker never reaches the model.

    Asking a model to reproduce `[[INVESTIGATION:uuid]]` means a model that
    paraphrases it, or forgets it, silently costs the reader the panel. The
    chat layer attaches it from this list instead.
    """
    from app.services.investigation_events import build_investigation_marker

    marker = build_investigation_marker("i-42")
    _run(monkeypatch, text=f"The report body.\n\n{marker}", result=_result("i-42"))

    out: list = []
    tool = build_deep_investigation_tool(None, "u", investigations_out=out)
    body = await _call(tool, subject="APT29")

    assert out == ["i-42"]
    assert marker not in body
    assert body == "The report body."


@pytest.mark.asyncio
async def test_two_parallel_calls_in_one_response_run_once(monkeypatch) -> None:
    """The loop runs one response's tool calls concurrently: the slot is
    reserved before the run starts, so the second call is refused instead of
    passing the cap alongside the first."""
    import asyncio

    import app.services.deep_investigation_chat as dic

    runs: list[str] = []
    release = asyncio.Event()

    async def _turn(_db, **kwargs):
        runs.append(kwargs["subject"])
        await release.wait()
        kwargs["result_out"].append(_result("i-1"))
        return "report"

    monkeypatch.setattr(dic, "run_deep_investigation_turn", _turn, raising=True)
    out: list = []
    tool = build_deep_investigation_tool(None, "u", investigations_out=out)

    first = asyncio.create_task(_call(tool, subject="APT29"))
    await asyncio.sleep(0)
    try:
        # A second run would wait on the same event: bounded, so a regression
        # fails here instead of hanging.
        second = await asyncio.wait_for(_call(tool, subject="APT29 infrastructure"), 2)
    finally:
        release.set()
    await first

    assert runs == ["APT29"]
    assert second.startswith("Refused")
    assert out == ["i-1"]


@pytest.mark.asyncio
async def test_a_refused_run_frees_its_slot_and_a_failed_one_keeps_it(monkeypatch) -> None:
    from app.services.investigation_events import build_investigation_marker

    out: list = []
    tool = build_deep_investigation_tool(None, "u", investigations_out=out)

    _run(monkeypatch, text="Refused: unknown source pack.")
    await _call(tool, subject="APT29")
    assert out == []

    marker = build_investigation_marker("0b9e3c7a-1111-4222-8333-944455556666")
    _run(monkeypatch, text=f"The investigation could not be completed.\n\n{marker}")
    body = await _call(tool, subject="APT29")
    assert out == ["0b9e3c7a-1111-4222-8333-944455556666"]
    assert marker not in body


@pytest.mark.asyncio
async def test_the_live_block_streams_during_an_agent_turn(monkeypatch) -> None:
    """`on_event` is forwarded, or an agent turn produces a report minutes
    later with no visible progress — losing the point of the streamed state."""
    seen: dict = {}
    _run(monkeypatch, text="r", result=_result("i-1"), capture=seen)

    async def _sink(_frame):
        return None

    tool = build_deep_investigation_tool(None, "u", on_event=_sink)
    await _call(tool, subject="APT29")
    assert seen["on_event"] is _sink


@pytest.mark.asyncio
async def test_an_empty_subject_never_reaches_the_wrapper(monkeypatch) -> None:
    called = {"n": 0}

    import app.services.deep_investigation_chat as dic

    async def _turn(_db, **_kw):  # pragma: no cover - must not run
        called["n"] += 1
        return ""

    monkeypatch.setattr(dic, "run_deep_investigation_turn", _turn, raising=True)

    tool = build_deep_investigation_tool(None, "u")
    for subject in ("", "   ", "\n\t"):
        assert "required" in await _call(tool, subject=subject)
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_a_refused_run_returns_the_wrappers_message(monkeypatch) -> None:
    """Quota exceeded, unknown pack, blank subject: the wrapper already turned
    each into a sentence for the reader, and no run exists to cite."""
    _run(monkeypatch, text="You have reached your daily quota.", result=None)

    out: list = []
    tool = build_deep_investigation_tool(None, "u", investigations_out=out)
    assert await _call(tool, subject="APT29") == "You have reached your daily quota."
    assert out == []


@pytest.mark.asyncio
async def test_the_selected_packs_reach_the_run(monkeypatch) -> None:
    seen: dict = {}
    _run(monkeypatch, text="r", result=_result("i-1"), capture=seen)

    await _call(
        build_deep_investigation_tool(None, "u"),
        subject="FIMI",
        source_packs=["knowledge-bases", "mcp-tools"],
    )
    assert seen["source_pack_slugs"] == ["knowledge-bases", "mcp-tools"]

    # A fresh builder: one run per turn, so a second call on the same one is
    # refused before it reaches the wrapper.
    await _call(build_deep_investigation_tool(None, "u"), subject="FIMI")
    # Omitted means "let the classifier recommend", not "none".
    assert seen["source_pack_slugs"] is None


def test_the_tool_is_registered_in_the_platform_catalogue() -> None:
    """This registry's KEYS are the set of built-in tool ids that exist.

    A skill package or agent naming one that is absent is rejected at import,
    so a tool wired into the chat loop but missing here is an agent that
    cannot be saved.
    """
    import inspect

    from app.services import builtin_tool_catalog

    source = inspect.getsource(builtin_tool_catalog)
    assert 'catalog["deep_investigation"]' in source


def test_the_agent_and_its_handover_are_seeded() -> None:
    from app.agents.builtin_agents import BUILTIN_AGENTS

    by_slug = {a["slug"]: a for a in BUILTIN_AGENTS}
    agent = by_slug["deep-investigation-agent"]

    # The tool, and deliberately nothing that could substitute for it: given
    # `web_fetch` this agent would read a page and write an uncited paragraph.
    assert agent["builtin_tool_ids"] == ["deep_investigation"]

    # `consult`, not `transfer`: the investigation is work handed back, not a
    # change of who the user is talking to.
    assert by_slug["ctem-assistant"]["handover_type_slugs"][
        "deep-investigation-agent"
    ] == "consult"


def test_the_persona_forbids_answering_from_memory() -> None:
    """The one instruction that decides whether this agent is trustworthy."""
    from pathlib import Path

    persona = (
        Path(__file__).resolve().parents[1]
        / "app" / "agents" / "seed_data" / "agent_persona"
        / "deep-investigation-agent.md"
    ).read_text(encoding="utf-8")

    assert "Never answer from your own knowledge" in persona
    assert "Reproduce citations exactly" in persona


# ── Reaching the user through a consulting agent ────────────────────────────
#
# Found by running it: CTEM consulted the Deep Investigation Agent, received a
# report with 16 numbered sources, rewrote it into a tidy French summary — and
# dropped every marker. The reader got a confident, well-formatted document
# with no traceability at all, which is the failure this whole feature exists
# to prevent. Three guards, in decreasing order of how much they depend on a
# model behaving.


def test_investigation_frames_are_forwarded_unwrapped_through_a_consult() -> None:
    """The block belongs to the CONVERSATION, not to the consultation.

    Every other inner frame is wrapped in a `consult_progress` envelope. The
    component that renders the block reads `status` at the top level, so a
    wrapped `investigation_step` is invisible — which is why a two-minute
    investigation showed the user nothing but "still working".
    """
    import inspect

    from app.services import agent_chat
    from app.services.investigation_events import INVESTIGATION_FRAME_STATUSES

    source = inspect.getsource(agent_chat)
    assert "INVESTIGATION_FRAME_STATUSES" in source
    # The three the block is driven by, and no more: widening this set would
    # start leaking unrelated inner frames past the envelope.
    assert {
        "investigation_planned",
        "investigation_step",
        "investigation_complete",
    } == INVESTIGATION_FRAME_STATUSES


def test_every_investigation_frame_status_is_in_the_forwarded_set() -> None:
    """Guards the drift: a new frame the block needs, added to the builders
    and not to the set, would be silently swallowed by the envelope."""
    import inspect
    import re

    from app.services import investigation_events
    from app.services.investigation_events import INVESTIGATION_FRAME_STATUSES

    emitted = set(
        re.findall(r'"status":\s*"(investigation_\w+)"', inspect.getsource(investigation_events))
    )
    assert emitted == set(INVESTIGATION_FRAME_STATUSES)


def test_the_block_is_attached_even_when_a_consulting_agent_rewrites_the_report() -> None:
    """The guarantee that does not depend on a model.

    The consulting agent may do anything with the prose — and on the first
    real run it dropped all 16 citations. The record of what was queried is
    attached from the run itself, through the same list the parent turn owns.
    """
    import inspect

    from app.services import agent_chat

    consult = inspect.getsource(agent_chat).split("async def _consult_agent")[1]
    assert "investigations_out=investigations_out" in consult


def test_a_cited_consult_answer_asks_the_caller_to_preserve_the_citations() -> None:
    """The weakest of the three guards — an instruction a model can ignore —
    which is exactly why the block is attached structurally as well."""
    import inspect

    from app.services import agent_chat

    source = inspect.getsource(agent_chat)
    assert "_CITATION_MARKER.search(_return_text)" in source
    assert "Reproduce them EXACTLY" in source

    # And it fires only when there is something to preserve.
    assert agent_chat._CITATION_MARKER.search("A claim [3].")
    assert not agent_chat._CITATION_MARKER.search("No citations here.")


def test_the_consulting_persona_carries_the_same_rule() -> None:
    from pathlib import Path

    persona = (
        Path(__file__).resolve().parents[1]
        / "app" / "agents" / "seed_data" / "agent_persona" / "ctem-assistant.md"
    ).read_text(encoding="utf-8")

    assert "Deep Investigation Agent" in persona
    assert "CONSULT (do not transfer)" in persona
    assert "reproduce the citations exactly" in persona


# ── The last link: what the consulting agent wrote ─────────────────────────
#
# `validate` checks the DRAFT the synthesis produced, and refuses one citing
# an excerpt it was not given. Nothing checked the agent that then rewrote
# that draft for the user — and the first real run through one produced [14],
# [16] and [17] against a key stopping at [13]. The block was attached, so the
# real sources were there; the prose still offered three citations resolving
# to nothing.


def test_a_marker_the_run_does_not_back_stops_looking_checkable() -> None:
    """Neutralised, not deleted.

    The sentence WAS presented as sourced. Quietly removing the marker would
    hide that a claim lost its support instead of showing it.
    """
    from app.services.investigation_report import (
        UNRESOLVED_MARKER,
        reconcile_citations,
    )

    text, replaced = reconcile_citations(
        "Attributed to the FSB [4]. Invented [14]. Also invented [16].", {1, 2, 3, 4}
    )
    assert text == f"Attributed to the FSB [4]. Invented {UNRESOLVED_MARKER}. Also invented {UNRESOLVED_MARKER}."
    assert replaced == (14, 16)


def test_a_real_marker_is_left_exactly_as_written() -> None:
    from app.services.investigation_report import reconcile_citations

    text, replaced = reconcile_citations("A [1]. B [2][3].", {1, 2, 3})
    assert text == "A [1]. B [2][3]."
    assert replaced == ()


def test_nothing_is_rewritten_without_a_key_to_check_against() -> None:
    """A run that published no report has no key.

    Rewriting every bracketed number then would mangle text that was never a
    citation — a version range, a footnote, a list index.
    """
    from app.services.investigation_report import reconcile_citations

    for empty in (set(), [], None):
        text, replaced = reconcile_citations("See [9] and [1999].", empty or set())
        assert text == "See [9] and [1999]."
        assert replaced == ()


def test_the_reader_is_told_how_many_were_neutralised() -> None:
    """Disclosed, not silently fixed: a message that quietly lost citations
    reads as one that never had them."""
    from app.services.investigation_report import (
        UNRESOLVED_MARKER,
        unresolved_disclosure,
    )

    line = unresolved_disclosure((14, 16, 16))
    assert "2 citation(s)" in line
    assert UNRESOLVED_MARKER in line
    assert unresolved_disclosure(()) == ""


def test_the_chat_layer_reconciles_before_attaching_the_block() -> None:
    """Order matters: the marker line must not be rewritten as a citation."""
    import inspect

    from app.api.v1 import chat

    source = inspect.getsource(chat)
    reconcile_at = source.index("_reconcile_investigation_citations(")
    attach_at = source.index("build_investigation_marker(_iid)")
    assert reconcile_at < attach_at


@pytest.mark.asyncio
async def test_a_lookup_failure_costs_the_check_not_the_reply(monkeypatch) -> None:
    """The reconciliation improves a message that already exists. Losing the
    whole reply because a query raised would be a far worse trade."""
    from app.api.v1.chat import _reconcile_investigation_citations

    class _Boom:
        async def execute(self, *_a, **_k):
            raise RuntimeError("database went away")

    text = "An answer citing [14]."
    assert await _reconcile_investigation_citations(_Boom(), text, ["not-a-uuid"]) == text

    import uuid as _uuid

    assert (
        await _reconcile_investigation_citations(_Boom(), text, [str(_uuid.uuid4())])
        == text
    )


def test_a_renumbered_answer_gets_the_runs_own_source_list() -> None:
    """The failure a marker check cannot catch.

    A consulting agent rewrote the report into `[A1]`, `[A6]`, `[A8]` — a
    scheme of its own. Those are not this run's numbers and never were, so no
    amount of marker-matching recognises them, and the prose reads as sourced
    while resolving to nothing. The citations cannot be repaired from here;
    the sources can still be handed to the reader.
    """
    from app.services.investigation_report import render_source_key

    key = render_source_key([
        {"n": 1, "label": "MITRE ATT&CK", "href": "https://m/", "source_name": "Web research"},
        {"n": 2, "label": "doctrine.md", "source_name": "VIGINUM FIMI Doctrine"},
    ])
    assert "not this investigation's" in key
    assert "[1] [MITRE ATT&CK](https://m/)" in key
    # No address: named, never linked — the reader may have no access to it.
    assert "[2] doctrine.md — VIGINUM FIMI Doctrine" in key


def test_a_malformed_source_row_is_skipped_not_rendered() -> None:
    from app.services.investigation_report import render_source_key

    assert render_source_key([{"n": 1}, {"label": "no number"}, "junk", None]) == ""


@pytest.mark.asyncio
async def test_the_key_is_appended_only_when_nothing_resolves(monkeypatch) -> None:
    """A message that still carries real markers keeps its own numbering; the
    fallback is for one where the run's numbers appear nowhere."""
    import uuid as _uuid

    from app.api.v1.chat import _reconcile_investigation_citations

    sources = [{"n": 1, "label": "MITRE", "href": "https://m/", "source_name": "Web"}]

    class _DB:
        async def execute(self, *_a, **_k):
            return SimpleNamespace(all=lambda: [(sources,)])

    inv = [str(_uuid.uuid4())]

    renumbered = await _reconcile_investigation_citations(
        _DB(), "Attributed to the GRU. [A1]", inv
    )
    assert "sources it actually queried" in renumbered
    assert "[1] [MITRE](https://m/)" in renumbered

    intact = await _reconcile_investigation_citations(
        _DB(), "Attributed to the GRU [1].", inv
    )
    assert "sources it actually queried" not in intact
    assert intact == "Attributed to the GRU [1]."


# ── One investigation per turn ──────────────────────────────────────────────
#
# Asked to research APT28, the agent decomposed the question into NINE
# facet-queries and got back nine reports each numbered from [1]. Merging nine
# incompatible keys is impossible, so the consulting agent invented a scheme
# of its own and every citation stopped resolving. The renumbering was not
# misbehaviour — it was the only coherent move left, and the multiplicity is
# what caused it.


@pytest.mark.asyncio
async def test_a_second_investigation_in_one_turn_is_refused(monkeypatch) -> None:
    calls = {"n": 0}

    import app.services.deep_investigation_chat as dic

    async def _turn(_db, **kwargs):
        calls["n"] += 1
        if kwargs.get("result_out") is not None:
            kwargs["result_out"].append(_result(f"i-{calls['n']}"))
        return "report"

    monkeypatch.setattr(dic, "run_deep_investigation_turn", _turn, raising=True)

    out: list = []
    tool = build_deep_investigation_tool(None, "u", investigations_out=out)

    assert await _call(tool, subject="APT28") == "report"
    refusal = await _call(tool, subject="APT28 TTPs and malware arsenal")

    assert calls["n"] == 1
    assert out == ["i-1"]
    assert refusal.startswith("Refused:")
    # Names what it already has, so the model can use it instead of rephrasing.
    assert "'APT28'" in refusal
    # And says WHY, since the reason is the one that decides the answer's worth.
    assert "separately numbered" in refusal


@pytest.mark.asyncio
async def test_the_cap_is_per_turn_not_per_process(monkeypatch) -> None:
    """The builder is created per turn, so the count has to live with it.

    A module-level counter would refuse the second user's first question.
    """
    import app.services.deep_investigation_chat as dic

    async def _turn(_db, **kwargs):
        if kwargs.get("result_out") is not None:
            kwargs["result_out"].append(_result("i-1"))
        return "report"

    monkeypatch.setattr(dic, "run_deep_investigation_turn", _turn, raising=True)

    first = build_deep_investigation_tool(None, "u")
    assert await _call(first, subject="APT28") == "report"
    assert (await _call(first, subject="APT29")).startswith("Refused:")

    # A new turn starts fresh.
    second = build_deep_investigation_tool(None, "u")
    assert await _call(second, subject="APT29") == "report"


@pytest.mark.asyncio
async def test_a_refused_run_does_not_consume_the_turns_allowance(monkeypatch) -> None:
    """Quota, an unknown pack, a blank subject: nothing ran, so nothing is
    spent — otherwise a quota refusal would also lock out the retry."""
    import app.services.deep_investigation_chat as dic

    async def _turn(_db, **_kwargs):
        return "You have reached your daily quota."

    monkeypatch.setattr(dic, "run_deep_investigation_turn", _turn, raising=True)

    tool = build_deep_investigation_tool(None, "u")
    assert await _call(tool, subject="APT28") == "You have reached your daily quota."
    # Still allowed to try again — the cap counts runs, not attempts.
    assert not (await _call(tool, subject="APT28")).startswith("Refused:")


def test_the_tool_description_and_persona_both_state_the_rule() -> None:
    """Stated where the model reads, so it picks a broad subject rather than
    discovering the cap by hitting it."""
    from pathlib import Path

    defs, _ = build_deep_investigation_tool(None, "u")
    assert "CALL IT ONCE PER TURN" in defs[0].description

    persona = (
        Path(__file__).resolve().parents[1]
        / "app" / "agents" / "seed_data" / "agent_persona"
        / "deep-investigation-agent.md"
    ).read_text(encoding="utf-8")
    assert "One run, one subject" in persona


def test_a_consulted_run_keeps_the_conversation_it_was_started_from() -> None:
    """Found by running it: the artifact never reached the workspace.

    A consulted agent holds no thread of its own — it is doing a piece of
    work, not carrying a conversation — so the nested turn runs with
    `conversation_id=None`. An investigation it started was therefore created
    unattached, and an unattached run has no workspace to file its report
    into. The report existed; nothing could ever read it from the workspace
    the user was working in.
    """
    import inspect

    from app.services import agent_chat

    source = inspect.getsource(agent_chat)
    # The consulting turn hands its own conversation down - or, consulted
    # itself, the one it was handed...
    consult = source.split("async def _consult_agent")[1]
    assert "investigations_conversation_id=_consult_conversation_id" in consult
    assert "_consult_conversation_id = investigations_conversation_id or conversation_id" in source
    # ...and the tool prefers it over the (absent) nested one.
    assert (
        "conversation_id=investigations_conversation_id or conversation_id" in source
    )


# ── The cap has to survive a nested consult ─────────────────────────────────
#
# `MAX_INVESTIGATIONS_PER_TURN` counted a closure local, and the builder is
# per `run_agent_chat` — not per turn. `_consult_agent` starts a NESTED
# `run_agent_chat`, which builds its own tool with its own empty counter while
# deliberately sharing `investigations_out`. So a consulting agent got one run
# per consult, each believing it was the first.
#
# That is also how two separately-numbered citation keys reached one turn,
# which is the state the cap exists to prevent — so the two bugs were one bug,
# and this is the test neither had.


@pytest.mark.asyncio
async def test_a_nested_consult_cannot_start_a_second_investigation(
    monkeypatch,
) -> None:
    calls = {"n": 0}

    import app.services.deep_investigation_chat as dic

    async def _turn(_db, **kwargs):
        calls["n"] += 1
        if kwargs.get("result_out") is not None:
            kwargs["result_out"].append(_result(f"i-{calls['n']}"))
        return "report"

    monkeypatch.setattr(dic, "run_deep_investigation_turn", _turn, raising=True)

    # One turn, one shared list — and two builders, because the consulted
    # agent's `run_agent_chat` makes its own.
    out: list = []
    parent = build_deep_investigation_tool(None, "u", investigations_out=out)
    assert await _call(parent, subject="APT28") == "report"

    nested = build_deep_investigation_tool(None, "u", investigations_out=out)
    refusal = await _call(nested, subject="APT28 malware arsenal")

    assert calls["n"] == 1, "the nested builder must not get a fresh allowance"
    assert out == ["i-1"], "one turn, one investigation, one citation key"
    assert refusal.startswith("Refused:")
    # This builder did not run the first one, so it does not claim to know
    # the subject — it still says why.
    assert "separately numbered" in refusal


@pytest.mark.asyncio
async def test_the_refusal_names_the_subject_when_this_builder_ran_it(
    monkeypatch,
) -> None:
    """The nested case cannot name it; the direct case still should."""
    import app.services.deep_investigation_chat as dic

    async def _turn(_db, **kwargs):
        if kwargs.get("result_out") is not None:
            kwargs["result_out"].append(_result("i-1"))
        return "report"

    monkeypatch.setattr(dic, "run_deep_investigation_turn", _turn, raising=True)

    out: list = []
    tool = build_deep_investigation_tool(None, "u", investigations_out=out)
    await _call(tool, subject="APT28")
    assert "'APT28'" in await _call(tool, subject="APT28 TTPs")


# ── The key is not severed by the context cap ───────────────────────────────


@pytest.mark.asyncio
async def test_the_tool_hands_back_a_report_that_still_has_its_key(
    monkeypatch,
) -> None:
    """Through the tool, not the helper: a flat slice at the call site is the
    bug, so the test has to exercise the call site."""
    import app.services.deep_investigation_chat as dic
    from app.services.builtin_tools.investigation import MAX_RESULT_CHARS

    key = "[1] [First source](https://a.example/)\n[2] Second source — Corpus"
    long_report = ("Sourced sentence [1]. " * 4000) + "\n\n" + key
    assert len(long_report) > MAX_RESULT_CHARS

    async def _turn(_db, **kwargs):
        if kwargs.get("result_out") is not None:
            kwargs["result_out"].append(_result("i-1"))
        return long_report

    monkeypatch.setattr(dic, "run_deep_investigation_turn", _turn, raising=True)

    body = await _call(build_deep_investigation_tool(None, "u"), subject="APT28")

    assert len(body) <= MAX_RESULT_CHARS
    assert body.endswith(key), (
        "the calling model received markers with no key to resolve them"
    )


def test_a_long_report_keeps_its_source_key() -> None:
    """Markers with no key look sourced and cannot be checked, which
    `render_markdown` calls worse than no markers at all. A flat slice at the
    cap produced exactly that, because the key is at the end."""
    from app.services.builtin_tools.investigation import MAX_RESULT_CHARS, _fit

    key = "[1] [First source](https://a.example/)\n[2] Second source — Corpus"
    body = ("Sourced sentence [1]. " * 4000) + "\n\n" + key
    assert len(body) > MAX_RESULT_CHARS

    fitted = _fit(body)
    assert len(fitted) <= MAX_RESULT_CHARS
    assert fitted.endswith(key), "the key must survive the trim"
    assert "[...]" in fitted, "and the model must see that prose was dropped"


def test_a_report_within_the_cap_is_untouched() -> None:
    from app.services.builtin_tools.investigation import _fit

    body = "Short report [1].\n\n[1] A source — Web"
    assert _fit(body) == body


# ── Two keys in one turn resolve to nothing, not to the wrong thing ─────────
#
# Every run numbers its sources from [1]. The reconciliation unioned the keys
# of every investigation in the turn, so a marker written against the second
# run validated against the first and the reader followed it to a DIFFERENT
# source. That passes silently — the number is in range — which makes it worse
# than an unresolved marker: it looks checkable and points somewhere else.


def _sources(prefix: str, n: int) -> list[dict]:
    return [
        {
            "n": i,
            "label": f"{prefix} doc {i}",
            "href": f"https://{prefix.lower()}.example/{i}",
            "source_name": f"{prefix} source",
        }
        for i in range(1, n + 1)
    ]


class _TwoRunDB:
    def __init__(self, *runs: list[dict]):
        self._runs = runs

    async def execute(self, *_a, **_k):
        return SimpleNamespace(all=lambda: [(r,) for r in self._runs])


@pytest.mark.asyncio
async def test_a_marker_is_never_resolved_against_another_runs_key() -> None:
    import uuid as _uuid

    from app.api.v1.chat import _reconcile_investigation_citations

    db = _TwoRunDB(_sources("Alpha", 13), _sources("Bravo", 9))
    ids = [str(_uuid.uuid4()), str(_uuid.uuid4())]

    out = await _reconcile_investigation_citations(
        db, "Infrastructure overlaps with the 2024 cluster [9].", ids
    )

    # The prose is everything before the first rendered key line; the keys
    # themselves are numbered by construction, so assert on the prose alone.
    prose = out.split("\n\n")[0]
    assert "[9]" not in prose, (
        "a bare [9] cannot belong to either key and must not be validated "
        "against their union"
    )
    assert "[?]" in prose
    # Both keys are shown, each labelled, so the reader can see why the
    # marker was dropped and which list is which.
    assert "Alpha doc 9" in out and "Bravo doc 9" in out
    assert "Investigation 1 of 2" in out and "Investigation 2 of 2" in out
    assert "each numbers its sources from [1]" in out


@pytest.mark.asyncio
async def test_one_run_still_reconciles_normally() -> None:
    """The multi-run path must not change the single-run behaviour, which is
    the only one the per-turn cap now allows."""
    import uuid as _uuid

    from app.api.v1.chat import _reconcile_investigation_citations

    db = _TwoRunDB(_sources("Alpha", 3))
    ids = [str(_uuid.uuid4())]

    kept = await _reconcile_investigation_citations(db, "A claim [2].", ids)
    assert kept == "A claim [2]."

    out_of_range = await _reconcile_investigation_citations(db, "A claim [7].", ids)
    assert "[?]" in out_of_range
    assert "could not be resolved" in out_of_range or "[?]" in out_of_range


def test_the_trim_keeps_the_whole_key_not_just_its_tail() -> None:
    """`_fit` searched for the FIRST line starting `[n] `, so a report whose
    prose quotes a numbered list treated that as the key start and over-trimmed
    real prose to preserve text that was not the key. It now finds the last
    contiguous run of key lines, so a key beginning at `[1]` survives whole."""
    from app.services.builtin_tools.investigation import MAX_RESULT_CHARS, _fit

    key = "[1] [First](https://a/)\n[2] Second — Corpus\n[3] Third — Web"
    prose = (
        ("Sourced sentence [1]. " * 3000)
        + "\n[1] a line of prose that merely looks like a key\n"
        + ("More prose [2]. " * 900)
    )
    body = prose + "\n\n" + key
    assert len(body) > MAX_RESULT_CHARS

    out = _fit(body)
    assert len(out) <= MAX_RESULT_CHARS
    assert out.endswith(key), "the key was cut to its tail"
    assert "[...]" in out
