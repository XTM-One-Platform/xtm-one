"""The agent loop's loop breaker and step-budget nudge.

The 25 Sep 2026 run made ~40 ``read_file_content`` calls over three file ids
and nothing in the loop refused the Nth identical call. The breaker counts
every dispatched call per identical arguments and per target, warns the model
with a system message at the first bound and refuses the call at the second;
the step-budget nudge tells the model, once, where its iterations went.
"""

from __future__ import annotations

import pytest

from app.llm.base import ChatMessage, ChatResponse
from app.tools import agent_loop as al
from app.tools.agent_loop import (
    _format_step_budget_nudge,
    _loop_breaker_target,
    _LoopBreaker,
    _step_budget_nudge_iteration,
)
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from tests.test_tool_call_dedup import (
    _executor_with,
    _quiet_langfuse,  # noqa: F401 - pytest fixture
    _ScriptedLLM,
    _tool_call_response,
    _tool_messages,
)


def _breaker(**overrides) -> _LoopBreaker:
    kwargs = {
        "identical_warn": 3,
        "identical_refuse": 5,
        "similar_warn": 6,
        "similar_refuse": 10,
    }
    kwargs.update(overrides)
    return _LoopBreaker(**kwargs)


# ─────────────────────────────────────────────────────────────────────────
# Unit: the counter and its escalation
# ─────────────────────────────────────────────────────────────────────────


def test_identical_calls_warn_at_three_and_refuse_at_five():
    breaker = _breaker()
    actions = [breaker.screen("read_file_content", {"file_id": "abc"}).action for _ in range(7)]
    assert actions == ["allow", "allow", "warn", "allow", "refuse", "refuse", "refuse"]
    refused = [e for e in breaker.events if e.refused]
    assert [e.count for e in refused] == [5, 6, 7], "a refused ask still counts, so the number rises"
    assert all(e.kind == "identical" for e in breaker.events)


def test_the_warning_is_one_shot_per_argument_set():
    breaker = _breaker(identical_refuse=0)
    actions = [breaker.screen("search", {"q": "x"}).action for _ in range(6)]
    assert actions.count("warn") == 1
    # A different argument set has its own count and its own warning.
    assert breaker.screen("search", {"q": "y"}).action == "allow"


def test_similar_calls_on_one_target_warn_at_six_and_refuse_at_ten():
    """Varying ``max_chars`` / ``offset`` to defeat a cache is one target read
    N times: the similar shape catches what the identical shape cannot."""
    breaker = _breaker()
    actions = [
        breaker.screen("read_file_content", {"file_id": "abc", "max_chars": 1000 + i}).action
        for i in range(11)
    ]
    assert actions[:5] == ["allow"] * 5
    assert actions[5] == "warn"
    assert actions[6:9] == ["allow"] * 3
    assert actions[9] == "refuse"
    assert actions[10] == "refuse"
    warn = next(e for e in breaker.events if e.action == "warn")
    assert warn.kind == "similar"
    assert warn.anchor_name == "file_id" and warn.anchor == "abc"


def test_target_resolution_unwraps_the_file_id_spellings():
    assert _loop_breaker_target({"file_id": "[[FILE:abc-123]]"}) == ("file_id", "abc-123")
    assert _loop_breaker_target({"file_id": "file-abc"}) == ("file_id", "abc")
    assert _loop_breaker_target({"url": "https://x.test/a.png", "file_id": ""}) == (
        "url",
        "https://x.test/a.png",
    )
    assert _loop_breaker_target({"q": "x"}) is None
    assert _loop_breaker_target(None) is None
    assert _loop_breaker_target({"file_id": ["a", "b"]}) is None


def test_two_spellings_of_one_file_are_one_target():
    breaker = _breaker(identical_warn=0, identical_refuse=0)
    fid = "9b6f2a1e-1111-4222-8333-444455556666"
    spellings = [fid, f"[[FILE:{fid}]]", f"file:{fid}", f"file-{fid}", f"[[FILE:{fid}]]", fid]
    for i, spelling in enumerate(spellings):
        # ``max_chars`` varies (the cache-defeat shape), never ``offset``:
        # a moving offset is a page series and is exempt (below).
        decision = breaker.screen("read_file_content", {"file_id": spelling, "max_chars": 1000 + i})
    assert decision.action == "warn"
    assert decision.count == 6


def test_polling_and_wait_tools_are_never_counted():
    # ``poll_job`` is exempt because its definition declares ``live_state``,
    # never because of its name (#5303).
    breaker = _breaker(identical_warn=1, identical_refuse=1, live_tools=frozenset({"poll_job"}))
    for name in ("wait", "check_task_status", "get_task_result", "poll_job", "list_background_tasks"):
        for _ in range(3):
            assert breaker.screen(name, {"task_ids": ["t1"]}).action == "allow", name
    assert breaker.events == []


def test_a_tool_that_declares_live_state_is_never_counted():
    """A tool whose definition declares ``live_state`` - a job poller
    with no polling word in its name - is polled like the exempt tools: its
    unchanged argument is the point, and its answer changes between calls."""
    breaker = _breaker(live_tools=frozenset({"get_render_jobs"}))
    actions = [breaker.screen("get_render_jobs", {"render_ids": ["r1"]}).action for _ in range(7)]
    assert actions == ["allow"] * 7 and breaker.events == []
    # The same name without the declaration is counted like any read.
    plain = _breaker()
    assert [plain.screen("get_render_jobs", {"render_ids": ["r1"]}).action for _ in range(5)][-1] == "refuse"


def test_a_live_read_repeated_minutes_apart_is_never_refused():
    """A PR state re-read across a long babysitting run (#11488): each read
    minutes after the last is a new question, so its ask count starts over
    and no refusal comes, however many reads the run makes."""
    breaker = _breaker(live_reads=frozenset({"get_github_pr"}))
    window = al._LIVE_READ_WINDOW_S
    actions = [
        breaker.screen("get_github_pr", {"repo": "o/r", "number": 7}, now=1000.0 + i * (window + 1)).action
        for i in range(8)
    ]
    assert actions == ["allow"] * 8 and breaker.events == []


def test_a_true_identical_loop_on_a_live_read_is_still_refused():
    """The same live read seconds apart is a stuck run: warned at three,
    refused at five, exactly like any read."""
    breaker = _breaker(live_reads=frozenset({"get_github_pr"}))
    actions = [breaker.screen("get_github_pr", {"repo": "o/r", "number": 7}, now=1000.0 + i).action for i in range(6)]
    assert actions == ["allow", "allow", "warn", "allow", "refuse", "refuse"]


def test_a_refusal_of_a_live_read_ages_out_with_its_window():
    breaker = _breaker(live_reads=frozenset({"get_github_pr"}))
    args = {"repo": "o/r", "number": 7}
    for i in range(5):
        decision = breaker.screen("get_github_pr", args, now=1000.0 + i)
    assert decision.refused
    later = breaker.screen("get_github_pr", args, now=1004.0 + al._LIVE_READ_WINDOW_S + 1)
    assert later.action == "allow" and breaker.count_of("get_github_pr", args) == 1
    # A read that does not declare live_read keeps its count across the same gap.
    plain = _breaker()
    for i in range(4):
        plain.screen("read_file_content", {"file_id": "abc"}, now=1000.0 + i)
    assert plain.screen("read_file_content", {"file_id": "abc"}, now=5000.0).refused


def test_a_status_write_is_counted_whatever_its_name_says():
    """A tool's name never exempts it (#5303): ``set_slack_user_status``
    repeated with identical arguments is a stuck run like any other call.
    The exemption is the platform's own background-task tools, or a
    definition that declares ``live_state``."""
    for name in ("set_slack_user_status", "update_openaev_run_status", "post_status_update"):
        assert not _LoopBreaker.is_exempt(name), name
    for name in ("get_job_status", "poll_job", "wait_for_completion"):
        assert not _LoopBreaker.is_exempt(name), name
    assert _LoopBreaker.is_exempt("check_task_status")
    declared = _breaker(identical_warn=2, identical_refuse=3, live_tools=frozenset({"get_job_status"}))
    assert [declared.screen("get_job_status", {"id": "j"}).action for _ in range(4)] == ["allow"] * 4
    breaker = _breaker(identical_warn=2, identical_refuse=3)
    actions = [breaker.screen("set_slack_user_status", {"text": "away"}).action for _ in range(3)]
    assert actions == ["allow", "warn", "refuse"]


def test_a_write_resets_the_read_counts_but_not_the_write_counts():
    breaker = _breaker()
    for _ in range(4):
        breaker.screen("read_file_content", {"file_id": "abc"})
        breaker.screen("edit_presentation", {"presentation_id": "d", "operations": []})
    assert breaker.count_of("read_file_content", {"file_id": "abc"}) == 4
    assert breaker.count_of("edit_presentation", {"presentation_id": "d", "operations": []}) == 4
    breaker.after_write()
    assert breaker.count_of("read_file_content", {"file_id": "abc"}) == 0
    assert breaker.count_of("edit_presentation", {"presentation_id": "d", "operations": []}) == 4
    # The read's warning may fire again after the reset: it is a new question.
    for _ in range(2):
        breaker.screen("read_file_content", {"file_id": "abc"})
    assert breaker.screen("read_file_content", {"file_id": "abc"}).action == "warn"


#: Checks whose verb the shared classifier does not know, so its fallback
#: reads them as writes: a presentation capture, a no-argument queue pop, a
#: runner screenshot, and two MCP naming styles it cannot split into words.
_UNKNOWN_VERB_CHECKS: list[tuple[str, dict]] = [
    ("capture_presentation_slides", {"presentation_id": "d", "slides": "3"}),
    ("workset_next_pending", {}),
    ("computer_screenshot", {}),
    ("getJiraIssue", {"issueIdOrKey": "X-1"}),
    ("notion-fetch", {"id": "p"}),
]


def test_a_write_resets_a_check_whose_verb_the_classifier_does_not_know():
    """Edit, capture, edit, capture is a check after each write, not a repeat:
    what starts over after a write is every tool that is not a write BY ITS
    NAME, not only the tools the classifier reads as reads. Keyed on the
    classifier, the capture kept counting across the edits it verified and
    was refused at the fifth."""
    for name, _args in _UNKNOWN_VERB_CHECKS:
        assert al._is_write_tool(name), f"the premise: the fallback reads {name} as a write"
        assert not al._is_named_write(name), name
    breaker = _breaker()
    for _ in range(4):
        for name, args in _UNKNOWN_VERB_CHECKS:
            breaker.screen(name, args)
    breaker.after_write({"edit_presentation"})
    for name, args in _UNKNOWN_VERB_CHECKS:
        assert breaker.count_of(name, args) == 0, name


def test_the_tool_that_wrote_keeps_its_own_count():
    """A write's own reset never wipes the write: an unknown-verb write
    repeated identically (the same public comment five times) is still
    warned at the third call and refused at the fifth."""
    call = ("comment_elastic_case", {"case": "c", "text": "same"})
    assert not al._is_named_write(call[0]), "the premise: its verb is unknown"
    assert al._loop_breaker_write_resets(call[0]), "the premise: it triggers the reset"
    breaker = _breaker()
    actions = []
    for _ in range(5):
        decision = breaker.screen(*call)
        actions.append(decision.action)
        if not decision.refused:
            breaker.after_write({call[0]})
    assert actions == ["allow", "allow", "warn", "allow", "refuse"]


def test_a_check_run_starts_over_after_an_edit_but_not_after_itself():
    """``sandbox_run_command`` reads as a write by its name; run, edit, run checks a
    new tree each time, so another tool's write starts its count over (#175).
    Its own repeats with nothing edited between them still escalate."""
    run = ("sandbox_run_command", {"checkout": "xo", "check": "backend-lint"})
    assert al._is_named_write(run[0]), "the premise: its name reads as a write"
    breaker = _breaker(rechecks=frozenset({run[0]}))
    for _ in range(6):
        assert breaker.screen(*run).action == "allow"
        breaker.after_write({run[0]})  # the run itself took effect: it keeps its count
        breaker.after_write({"sandbox_edit_file"})
    assert breaker.count_of(*run) == 0
    plain = _breaker()
    for _ in range(4):
        plain.screen(*run)
    plain.after_write({"sandbox_edit_file"})
    assert plain.count_of(*run) == 4, "undeclared, a write by name keeps its count"
    alone = _breaker(rechecks=frozenset({run[0]}))
    actions = []
    for _ in range(5):
        decision = alone.screen(*run)
        actions.append(decision.action)
        alone.after_write({run[0]})
    assert actions == ["allow", "allow", "warn", "allow", "refuse"]


def test_the_sandbox_run_command_definition_declares_recheck_after_write():
    from pathlib import Path

    from app.services.builtin_tools import sandbox_workspace as sw

    source = Path(sw.__file__).read_text(encoding="utf-8")
    start = source.index('name="sandbox_run_command"')
    assert "recheck_after_write=True" in source[start : source.index("landed_probe=_run_command_landed", start)]


def test_the_runner_browser_is_a_session_like_the_built_in_one():
    """``browser_control`` drives the runner's real browser: a screenshot after
    a click is a new page, and a step of that session resets no read count."""
    assert not al._loop_breaker_write_resets("browser_control")
    breaker = _breaker()
    shot = {"action": "screenshot"}
    for i in range(6):
        assert breaker.screen("browser_control", shot).action == "allow"
        breaker.after_call("browser_control", shot)
        click = {"action": "click", "selector": f"#b{i}"}
        breaker.screen("browser_control", click)
        breaker.after_call("browser_control", click)
    assert not breaker.events


def test_a_stateful_tool_resets_its_other_argument_sets():
    """Two screenshots around a navigate are two pages; two back to back are one page twice."""
    breaker = _breaker()
    shot = {"action": "screenshot"}
    for _ in range(6):
        assert breaker.screen("browser", shot).action in ("allow", "warn")
        breaker.after_call("browser", shot)
        breaker.screen("browser", {"action": "navigate", "url": "https://x.test/p"})
        breaker.after_call("browser", {"action": "navigate", "url": "https://x.test/p"})
    assert not any(e.refused for e in breaker.events)
    back_to_back = _breaker()
    actions = []
    for _ in range(5):
        actions.append(back_to_back.screen("browser", shot).action)
        back_to_back.after_call("browser", shot)
    assert actions[-1] == "refuse"


def test_a_stateful_tool_resets_its_similar_ledger_and_warnings_too():
    """``navigate`` to A, then to B, then back to A, many times over: every
    call is a different page, so neither the identical nor the similar (same
    ``url``) count may accumulate across the moves - and the one-shot warning
    recorded for a target is forgotten with it, so a return to that target
    is nudged afresh instead of silently, or worse, refused."""
    breaker = _breaker(identical_warn=0, identical_refuse=0, similar_warn=2, similar_refuse=3)
    to_a = {"action": "navigate", "url": "https://x.test/a"}
    to_b = {"action": "navigate", "url": "https://x.test/b"}
    for _ in range(8):
        assert breaker.screen("browser", to_a).action == "allow"
        breaker.after_call("browser", to_a)
        assert breaker.screen("browser", to_b).action == "allow"
        breaker.after_call("browser", to_b)
    assert breaker.events == [], "alternating targets never reach the similar bound"
    assert set(breaker._similar) == {("browser", "url", "https://x.test/b")}, (
        "only the current call's target survives a stateful call"
    )

    # The same target back to back IS one page asked about N times: still counted.
    stuck = _breaker(identical_warn=0, identical_refuse=0, similar_warn=2, similar_refuse=3)
    actions = []
    for i in range(3):
        actions.append(stuck.screen("browser", {"action": "screenshot", "url": "https://x.test/a", "n": i}).action)
        stuck.after_call("browser", {"action": "screenshot", "url": "https://x.test/a", "n": i})
    assert actions == ["allow", "warn", "refuse"]

    # A warning recorded for A is dropped when the tool moves to B, so coming
    # back to A warns again rather than never (the key was in ``_warned``).
    warned = _breaker(identical_warn=0, identical_refuse=0, similar_warn=2, similar_refuse=0)
    for i in range(2):
        warned.screen("browser", {"action": "screenshot", "url": "https://x.test/a", "n": i})
        warned.after_call("browser", {"action": "screenshot", "url": "https://x.test/a", "n": i})
    assert warned.events[-1].action == "warn"
    warned.screen("browser", to_b)
    warned.after_call("browser", to_b)
    assert warned._warned == set()
    for i in range(2):
        decision = warned.screen("browser", {"action": "screenshot", "url": "https://x.test/a", "n": 10 + i})
        warned.after_call("browser", {"action": "screenshot", "url": "https://x.test/a", "n": 10 + i})
    assert decision.action == "warn" and decision.count == 2


def test_zero_bounds_disable_the_breaker():
    breaker = _breaker(identical_warn=0, identical_refuse=0, similar_warn=0, similar_refuse=0)
    assert not breaker.enabled
    assert all(breaker.screen("search", {"q": "x"}).action == "allow" for _ in range(20))
    assert breaker.events == []


# ─────────────────────────────────────────────────────────────────────────
# Unit: an ask whose answer the harness took away is forgiven
# ─────────────────────────────────────────────────────────────────────────


def _answered(breaker: _LoopBreaker, transcript: list[ChatMessage], tool: str, args: dict, body: str) -> ChatMessage:
    """Screen one call and append + record the tool message that answers it."""
    decision = breaker.screen(tool, args)
    assert decision.action != "refuse", decision
    answer = ChatMessage(role="tool", content=body, tool_call_id=f"c{len(transcript)}", name=tool)
    transcript.append(answer)
    breaker.record_answer(tool, args, answer)
    return answer


def _clear_as_stale(answer: ChatMessage) -> None:
    """What ``_clear_stale_tool_results`` does to an older large TEXT result."""
    answer.content = al._REFETCHABLE_STUB_TEMPLATE.format(target="", tool=answer.name, chars=f"{len(answer.content):,}")
    assert al._is_refetchable_stub(answer.content)


def test_a_re_read_after_the_harness_cleared_the_answer_is_a_fresh_ask():
    """The 26 Sep 2026 shape: ``get_github_file`` on one path, its result
    cleared by the tool-result curation ("stale and re-fetchable. Re-call the
    tool only if you still need this exact data"), the file read again. The
    count is the number of asks whose answer the model still holds: a read
    after a clearing starts at one, however many times the file was read and
    cleared before - never a warning, never a refusal."""
    transcript: list[ChatMessage] = []
    breaker = _breaker(transcript=transcript)
    args = {"repo": "XTM-One-Platform/xtm-one", "path": "frontend/src/components/presentation/DeckStage.tsx"}
    for _round in range(6):
        answer = _answered(breaker, transcript, "get_github_file", args, "File: DeckStage.tsx\n" + "x" * 5000)
        assert breaker.count_of("get_github_file", args) == 1
        _clear_as_stale(answer)
    assert breaker.events == [], "six reads of a file the harness cleared five times are six first asks"


def test_a_repeat_while_the_answer_is_still_held_is_counted_as_before():
    """The other half: asking again with the answer in front of the model is
    the loop the breaker exists for - warned at three, refused at five, the
    recorded answers untouched."""
    transcript: list[ChatMessage] = []
    breaker = _breaker(transcript=transcript)
    args = {"file_id": "abc"}
    actions = []
    for _ in range(5):
        decision = breaker.screen("read_file_content", args)
        actions.append(decision.action)
        answer = ChatMessage(role="tool", content="File: a.txt\n" + "y" * 800, tool_call_id="r", name="read_file_content")
        transcript.append(answer)
        breaker.record_answer("read_file_content", args, answer)
    assert actions == ["allow", "allow", "warn", "allow", "refuse"]


def test_only_the_refetchable_stub_forgives_a_re_call():
    """A file-bearing result's stub points at ``read_file_content`` and never
    says re-fetchable (re-calling a download makes a NEW file - trace
    722375f1), a ``[Duplicate ...]`` stub says the output is still in the
    conversation, a ``[Pruned ...]`` stub is the emergency prune's. None of
    them invited a re-call, so a re-call after any of them keeps counting."""
    transcript: list[ChatMessage] = []
    breaker = _breaker(identical_refuse=0, transcript=transcript)
    args = {"block_id": "b1"}
    stubs = (
        "[Earlier download_notion_file result (900 chars) cleared to keep the working context focused.]"
        "[Image(s) removed from the working context to save space: figure.png (file id abc)]",
        "[Duplicate download_notion_file result collapsed — identical output (900 chars) appears again later in the conversation; see the most recent copy.]",
        "[Pruned tool result — originally 900 chars. Content omitted to stay within context budget.]",
    )
    for stub in stubs:
        assert not al._is_refetchable_stub(stub), stub
        answer = _answered(breaker, transcript, "download_notion_file", args, "The PNG is attached. " * 50)
        answer.content = stub
    assert breaker.count_of("download_notion_file", args) == 3
    assert breaker.screen("download_notion_file", args).action == "allow"
    assert breaker.count_of("download_notion_file", args) == 4
    assert [e.action for e in breaker.events] == ["warn"], "the third ask warned; nothing was forgiven"


def test_an_answer_compaction_dropped_from_the_transcript_forgives_the_re_ask():
    """Semantic compaction and the emergency prune drop whole old turns; an
    answer no longer in the transcript is one the model no longer has."""
    transcript: list[ChatMessage] = []
    breaker = _breaker(transcript=transcript)
    args = {"q": "x"}
    for _ in range(4):
        _answered(breaker, transcript, "search_lookup", args, "results " * 200)
    assert breaker.count_of("search_lookup", args) == 4
    assert breaker.events[-1].action == "warn"
    # Compaction rewrites the list in place, keeping only the newest turn.
    transcript[:] = transcript[-1:]
    assert breaker.count_of("search_lookup", args) == 1
    assert breaker.screen("search_lookup", args).action == "allow"
    # Without a transcript to check against, a dropped message is not detectable
    # and the count stands - the breaker never guesses.
    blind = _breaker()
    for _ in range(4):
        blind.record_answer("search_lookup", args, ChatMessage(role="tool", content="r", tool_call_id="c", name="search_lookup"))
        blind.screen("search_lookup", args)
    assert blind.count_of("search_lookup", args) == 4


def test_the_one_shot_warning_is_re_armed_by_a_full_reset_only():
    """A key whose every ask was forgiven starts over, warning included: a
    genuine repeat after the reset is nudged again instead of silently
    counted up to the refusal. A partial reset (some answers still held)
    keeps the warning spent - the model was told once about THESE asks."""
    transcript: list[ChatMessage] = []
    breaker = _breaker(identical_refuse=0, transcript=transcript)
    args = {"path": "a.css"}
    answers = [_answered(breaker, transcript, "get_github_file", args, "css " * 300) for _ in range(3)]
    assert [e.action for e in breaker.events] == ["warn"]
    # Partial: the two oldest cleared, the newest held -> count 2, no re-warn at 3.
    for answer in answers[:2]:
        _clear_as_stale(answer)
    _answered(breaker, transcript, "get_github_file", args, "css " * 300)
    assert breaker.count_of("get_github_file", args) == 2
    _answered(breaker, transcript, "get_github_file", args, "css " * 300)
    assert breaker.count_of("get_github_file", args) == 3
    assert len(breaker.events) == 1, "the warning about this key was already given"
    # Full: every held answer cleared -> the next three asks warn again.
    for answer in transcript:
        _clear_as_stale(answer)
    for _ in range(3):
        _answered(breaker, transcript, "get_github_file", args, "css " * 300)
    assert [e.action for e in breaker.events] == ["warn", "warn"]


def test_the_similar_ledger_forgives_cleared_re_reads_too():
    """Six reads of one ``file_id`` varying only ``max_chars`` (the
    cache-defeat shape - a moving ``offset`` is a PAGE and lands on the page
    ledger instead), every answer cleared by curation, the reads made again.
    The similar shape counts the asks whose answer the model still holds,
    like the identical one - re-reading what the harness cleared is not the
    refusal at ten."""
    transcript: list[ChatMessage] = []
    breaker = _breaker(identical_warn=0, identical_refuse=0, transcript=transcript)
    reads = [{"file_id": "doc", "max_chars": 10_000 + i} for i in range(6)]
    first_pass = [_answered(breaker, transcript, "read_file_content", args, "page " * 300) for args in reads]
    assert [e.action for e in breaker.events] == ["warn"], "six reads of one target: the similar warn bound"
    for answer in first_pass:
        _clear_as_stale(answer)
    for args in reads:
        _answered(breaker, transcript, "read_file_content", args, "page " * 300)
    assert not any(e.refused for e in breaker.events), "re-reading cleared answers is not the refusal at ten"
    assert [e.action for e in breaker.events] == ["warn", "warn"], "the second pass is a fresh series, warned afresh at six"
    # Held re-reads still add up: ten reads over held answers are refused at ten.
    stuck = _breaker(identical_warn=0, identical_refuse=0, transcript=transcript)
    actions = [stuck.screen("read_file_content", {"file_id": "doc", "max_chars": 100 + i}).action for i in range(10)]
    assert actions[-1] == "refuse"


def test_record_answer_is_a_no_op_for_a_call_that_was_never_screened():
    breaker = _breaker()
    answer = ChatMessage(role="tool", content="done", tool_call_id="w", name="wait")
    breaker.record_answer("wait", {"seconds": 5}, answer)
    breaker.record_answer("search_lookup", {"q": "never asked"}, answer)
    assert breaker._identical == {} and breaker._similar == {}


def test_an_ask_no_site_recorded_is_held_even_with_nothing_of_it_in_the_transcript():
    """Fail-closed: a screened call whose result no site handed back is
    counted as if its answer were in front of the model - a missed hook makes
    the breaker stricter, never looser - even though the transcript holds no
    message of it (the identity check only runs on a RECORDED answer)."""
    transcript: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    breaker = _breaker(transcript=transcript)
    args = {"q": "x"}
    actions = [breaker.screen("search_lookup", args).action for _ in range(5)]
    assert actions == ["allow", "allow", "warn", "allow", "refuse"]
    assert breaker.count_of("search_lookup", args) == 5


def test_a_stub_shaped_body_the_tool_itself_returned_never_forgives():
    """``_is_refetchable_stub`` used to accept any body that
    started with ``[Earlier`` and quoted the stub's phrase, so a tool whose
    OWN output read like the stub (an integration relaying page-controlled
    text) forgave an ask whose answer was never taken away, and the identical
    loop the breaker exists for could run past the bound. Forgiveness now
    needs the HARNESS to have replaced the body: the breaker remembers the
    length the tool answered with, and only a stub that is not that body is a
    clearing. A stub-shaped answer keeps counting; a real body cleared to the
    same stub is forgiven."""
    transcript: list[ChatMessage] = []
    breaker = _breaker(transcript=transcript)
    args = {"url": "https://x.test/page"}
    fake = al._REFETCHABLE_STUB_TEMPLATE.format(target="", tool="web_fetch", chars="13,255")
    assert al._is_refetchable_stub(fake), "the premise: the body IS the stub, byte for byte"
    actions = []
    for _ in range(5):
        decision = breaker.screen("web_fetch", args)
        actions.append(decision.action)
        if decision.refused:
            continue
        answer = ChatMessage(role="tool", content=fake, tool_call_id="f", name="web_fetch")
        transcript.append(answer)
        breaker.record_answer("web_fetch", args, answer)
    assert actions == ["allow", "allow", "warn", "allow", "refuse"], "nothing was cleared, so nothing is forgiven"
    # The same stub written by the harness OVER a large body is the clearing
    # it looks like: the recorded length differs, the ask is forgiven.
    cleared = _breaker(transcript=transcript)
    for _ in range(6):
        answer = _answered(cleared, transcript, "web_fetch", args, "<html>" + "x" * 5000)
        assert cleared.count_of("web_fetch", args) == 1
        answer.content = fake
    assert cleared.events == []


def test_the_refetchable_stub_is_matched_whole_and_written_from_one_template():
    """Tripwire on the stub's wording: the matcher is derived from the template
    ``_clear_stale_tool_results`` writes, so an edit to the sentence cannot
    break detection - and it matches the COMPLETE stub, never a prefix or the
    phrase inside a longer body."""
    assert "{tool}" in al._REFETCHABLE_STUB_TEMPLATE and "{chars}" in al._REFETCHABLE_STUB_TEMPLATE
    messages = [
        ChatMessage(role="user", content="go"),
        ChatMessage(role="tool", content="x" * 5000, tool_call_id="a", name="get_github_file"),
        ChatMessage(role="tool", content="y" * 5000, tool_call_id="b", name="get_github_file"),
    ]
    al._clear_stale_tool_results(messages, live_budget_chars=1_000)
    assert al._is_refetchable_stub(messages[1].content), "what the curation really writes is what the breaker reads"
    assert messages[1].content == al._REFETCHABLE_STUB_TEMPLATE.format(target="", tool="get_github_file", chars="5,000")
    assert not al._is_refetchable_stub(messages[2].content), "the newest result is kept verbatim"
    stub = messages[1].content
    for not_a_stub in (
        stub + "\n\nand here is the page content the tool relayed",
        "[Earlier get_github_file result cleared - it is stale and re-fetchable.]",
        stub[:-1],
        "note: " + stub,
        "[Earlier download_notion_file result (900 chars) cleared to keep the working context focused.]"
        "\n\n[Image(s) removed from the working context to save space: figure.png (file id abc)]",
    ):
        assert not al._is_refetchable_stub(not_a_stub), not_a_stub


def test_a_refused_ask_keeps_counting_whatever_happens_to_the_refusal():
    """A refusal is the breaker's own answer and carried no data a re-call
    would bring back, so it is never forgiven: not when compaction drops the
    refusal message from the transcript, and not when the curation stubs it -
    a ``similar``-shape refusal quoting a long URL anchor is over the
    clearing threshold and the REAL curation writes the re-fetchable stub over
    it. Either way the key stays at the bound instead of restarting at one
    with five fresh tries."""
    # (a) refusals dropped from the transcript.
    transcript: list[ChatMessage] = []
    breaker = _breaker(transcript=transcript)
    args = {"q": "x"}
    for _ in range(4):
        _answered(breaker, transcript, "search_lookup", args, "results " * 200)
    refusals = []
    for _ in range(2):
        decision = breaker.screen("search_lookup", args)
        assert decision.refused
        refusal = ChatMessage(role="tool", content=al._format_loop_breaker_refusal("search_lookup", decision), tool_call_id="r", name="search_lookup")
        transcript.append(refusal)
        breaker.record_answer("search_lookup", args, refusal)
        refusals.append(refusal)
    # Compaction takes the whole history, the four answers and the two refusals.
    transcript[:] = []
    assert breaker.count_of("search_lookup", args) == 2, "the answers are forgiven, the refusals are not"
    # The key did not restart at one: two more asks, then the refusal again
    # (the one-shot warning stays spent while asks of this key are held).
    assert [breaker.screen("search_lookup", args).action for _ in range(3)] == ["allow", "allow", "refuse"]
    # (b) a refusal the curation stubs: a similar-shape refusal on a long URL.
    # The one real answer is large and gets cleared too - forgiven, rightly -
    # so the held refusal alone is what keeps the target at the bound.
    url = "https://example.test/" + "a" * 500
    transcript2: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    stubbed = _breaker(identical_warn=0, identical_refuse=0, similar_warn=0, similar_refuse=2, transcript=transcript2)
    # ``max_chars`` varies, never a position: a moving ``offset`` would be a
    # page series and never reach the similar bound.
    page = _answered(stubbed, transcript2, "web_fetch", {"url": url, "max_chars": 1000}, "<html>" + "x" * 5000)
    decision = stubbed.screen("web_fetch", {"url": url, "max_chars": 1001})
    assert decision.refused and decision.kind == "similar"
    refusal_text = al._format_loop_breaker_refusal("web_fetch", decision)
    assert len(refusal_text) >= al._STALE_TOOL_RESULT_MIN_CHARS, "the premise: the anchor makes the refusal clearable"
    refusal = ChatMessage(role="tool", content=refusal_text, tool_call_id="r", name="web_fetch")
    transcript2.append(refusal)
    stubbed.record_answer("web_fetch", {"url": url, "max_chars": 1001}, refusal)
    transcript2.append(ChatMessage(role="tool", content="z" * 5000, tool_call_id="n", name="other_tool"))
    al._clear_stale_tool_results(transcript2, live_budget_chars=1_000)
    assert al._is_refetchable_stub(page.content) and al._is_refetchable_stub(refusal.content), (
        "the premise: the real curation stubbed the answer AND the refusal"
    )
    assert stubbed.screen("web_fetch", {"url": url, "max_chars": 1002}).refused, (
        "the cleared answer is forgiven, the cleared refusal is not: the target stays at the bound"
    )


def test_the_real_prunes_forgive_only_the_answers_they_remove_from_the_transcript():
    """Against the real prune functions, not a hand-written transcript edit:
    ``_prune_session_context`` and the emergency prune's first phase replace a
    body with ``[Pruned ...]`` IN PLACE, its truncation phases with
    ``[Truncated ...]`` in place - the same message object, still in front of
    the model, still counted; only the eviction phase, which drops whole
    message groups, forgives - as does semantic compaction, which rebuilds the
    list without them."""
    transcript: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    breaker = _breaker(identical_warn=0, identical_refuse=0, transcript=transcript)
    args = {"repo": "r", "path": "a.py"}
    for _ in range(8):
        transcript.append(ChatMessage(role="assistant", content="", tool_calls=[]))
        _answered(breaker, transcript, "get_github_file", args, "F" * 5000)
    assert breaker.count_of("get_github_file", args) == 8
    al._prune_session_context(transcript, max_session_chars=3_000)
    assert sum(1 for m in transcript if m.role == "tool" and m.content.startswith("[Pruned tool result")) == 4
    assert breaker.count_of("get_github_file", args) == 8, "a [Pruned ...] stub invites nothing: still counted"
    before = list(transcript)
    al._emergency_prune_session_context(transcript, max_session_chars=1_500)
    assert all(any(m is o for o in before) for m in transcript), "the prune mutates in place: no message is replaced"
    kept_answers = sum(1 for m in transcript if m.role == "tool")
    evicted = 8 - kept_answers
    assert evicted > 0, "the premise: the eviction phase ran"
    assert any(m.role == "tool" and m.content.startswith("[Truncated") for m in transcript), "the premise: truncation ran too"
    assert breaker.count_of("get_github_file", args) == kept_answers, "evicted answers forgiven, truncated ones held"


def test_a_parallel_batch_of_identical_calls_records_one_answer_per_ask():
    """Three identical calls screened in one batch are three asks with no
    answer yet; their three results, appended one by one, land on the three
    asks by key and unanswered slot - never through a tool-call id map (Gemini
    reuses ``call_{name}_{index}`` across turns). Cleared together, all three
    are forgiven together."""
    transcript: list[ChatMessage] = []
    breaker = _breaker(transcript=transcript)
    args = {"q": "x"}
    for _ in range(3):
        assert breaker.screen("search_lookup", args).action in ("allow", "warn")
    asks = breaker._identical[al._dedup_key("search_lookup", args)]
    assert [a.answer for a in asks] == [None, None, None]
    answers = []
    for _ in range(3):
        answer = ChatMessage(role="tool", content="results " * 200, tool_call_id="call_search_lookup_0", name="search_lookup")
        transcript.append(answer)
        breaker.record_answer("search_lookup", args, answer)
        answers.append(answer)
    assert sorted(id(a.answer) for a in asks) == sorted(id(m) for m in answers), "one distinct message per ask"
    assert breaker.count_of("search_lookup", args) == 3
    for answer in answers:
        _clear_as_stale(answer)
    assert breaker.count_of("search_lookup", args) == 0


def test_the_user_warning_names_the_tool_the_count_and_the_shape():
    """What the reasoning pane shows: the tool, the count, the shape - never
    a bare "the same tool call is being repeated"."""
    from app.tools.agent_loop import _format_loop_breaker_user_warning, _LoopDecision

    identical = _format_loop_breaker_user_warning("get_github_file", _LoopDecision(action="warn", count=3))
    assert identical.startswith("⚠️ 'get_github_file' has been called 3 times with identical arguments this turn.")
    assert "change approach or report what I have" in identical
    similar = _format_loop_breaker_user_warning(
        "read_file_content",
        _LoopDecision(action="warn", kind="similar", count=6, anchor_name="file_id", anchor="abc"),
    )
    assert "'read_file_content' has been called 6 times on the same file_id this turn." in similar
    assert "abc" not in similar, "the anchor value is for the model's message, not the pane"


# ─────────────────────────────────────────────────────────────────────────
# Unit: the six settings behind the breaker and the vision budget
# ─────────────────────────────────────────────────────────────────────────


def test_the_breaker_settings_refuse_a_negative_bound_and_accept_zero():
    """``0`` disables a bound; a negative value is a typo and fails at startup
    instead of being clamped into a disabled breaker nobody asked for."""
    from pydantic import ValidationError

    from app.core.config import Settings

    for field in (
        "agent_loop_identical_call_warn_after",
        "agent_loop_identical_call_refuse_after",
        "agent_loop_similar_call_warn_after",
        "agent_loop_similar_call_refuse_after",
        "agent_loop_vision_views_per_turn",
        "agent_loop_read_windows_per_file",
    ):
        with pytest.raises(ValidationError, match="must be >= 0"):
            Settings(_env_file=None, **{field: -1})
        assert getattr(Settings(_env_file=None, **{field: 0}), field) == 0
    with pytest.raises(ValidationError, match="between 0 and 1"):
        Settings(_env_file=None, agent_loop_step_budget_nudge_fraction=1.5)
    with pytest.raises(ValidationError, match="between 0 and 1"):
        Settings(_env_file=None, agent_loop_step_budget_nudge_fraction=-0.1)
    assert Settings(_env_file=None, agent_loop_step_budget_nudge_fraction=0).agent_loop_step_budget_nudge_fraction == 0
    assert Settings(_env_file=None, agent_loop_step_budget_nudge_fraction=1).agent_loop_step_budget_nudge_fraction == 1
    defaults = Settings(_env_file=None)
    assert (
        defaults.agent_loop_identical_call_warn_after,
        defaults.agent_loop_identical_call_refuse_after,
        defaults.agent_loop_similar_call_warn_after,
        defaults.agent_loop_similar_call_refuse_after,
        defaults.agent_loop_step_budget_nudge_fraction,
        defaults.agent_loop_vision_views_per_turn,
        defaults.agent_loop_read_windows_per_file,
    ) == (3, 5, 6, 10, 0.6, 10, 4)


def test_a_warn_bound_at_or_past_its_refuse_bound_is_a_startup_warning(caplog):
    """The refusal is checked first, so ``warn >= refuse`` never nudges: said
    once at startup, never an error (warn ``0`` + refuse is the deliberate
    refusals-only configuration and stays silent)."""
    import logging

    from app.core.config import Settings

    with caplog.at_level(logging.WARNING, logger="app.core.config"):
        Settings(_env_file=None, agent_loop_identical_call_warn_after=5, agent_loop_identical_call_refuse_after=5)
    assert any("AGENT_LOOP_IDENTICAL_CALL_WARN_AFTER=5 is not below" in r.getMessage() for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.core.config"):
        Settings(_env_file=None, agent_loop_similar_call_warn_after=0, agent_loop_similar_call_refuse_after=10)
        Settings(_env_file=None, agent_loop_identical_call_warn_after=3, agent_loop_identical_call_refuse_after=0)
        Settings(_env_file=None)
    assert not any("is not below" in r.getMessage() for r in caplog.records)


def test_warn_at_or_past_refuse_refuses_without_a_nudge_in_the_breaker_itself():
    breaker = _breaker(identical_warn=5, identical_refuse=5)
    actions = [breaker.screen("search", {"q": "x"}).action for _ in range(6)]
    assert actions == ["allow"] * 4 + ["refuse", "refuse"]
    assert not any(e.action == "warn" for e in breaker.events)
    refusals_only = _breaker(identical_warn=0, identical_refuse=3)
    assert [refusals_only.screen("search", {"q": "x"}).action for _ in range(4)] == ["allow", "allow", "refuse", "refuse"]
    warn_only = _breaker(identical_warn=2, identical_refuse=0)
    assert [warn_only.screen("search", {"q": "x"}).action for _ in range(6)] == ["allow", "warn"] + ["allow"] * 4


# ─────────────────────────────────────────────────────────────────────────
# Unit: the step-budget nudge
# ─────────────────────────────────────────────────────────────────────────


def test_step_budget_nudge_fires_at_the_configured_fraction():
    assert _step_budget_nudge_iteration(25, 0.6) == 15
    assert _step_budget_nudge_iteration(10, 0.6) == 6
    assert _step_budget_nudge_iteration(3, 0.6) == 2
    assert _step_budget_nudge_iteration(1, 0.6) is None, "no room before the cap"
    assert _step_budget_nudge_iteration(25, 0) is None
    assert _step_budget_nudge_iteration(25, 1.0) is None
    assert _step_budget_nudge_iteration(0, 0.6) is None


def test_step_budget_nudge_names_the_top_three_tools_by_count():
    text = _format_step_budget_nudge(
        used=15,
        total=25,
        tool_call_totals={"read_file_content": 9, "browser": 4, "edit_presentation": 2, "search": 1},
    )
    assert text.startswith("[SYSTEM - step budget] You have used 60% of your step budget (15 of 25 iterations)")
    assert "read_file_content (9), browser (4), edit_presentation (2)" in text
    assert "search (1)" not in text
    assert "Finish or report" in text
    assert "on " not in _format_step_budget_nudge(used=6, total=10, tool_call_totals={}).split("iterations")[1].split(".")[0]


# ─────────────────────────────────────────────────────────────────────────
# Integration: the loop refuses the fifth identical call and warns at the third
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_stuck_run_is_refused_from_call_five(_quiet_langfuse, monkeypatch):  # noqa: F811
    """Eight identical ``search_lookup`` calls: four execute, the fifth and
    every later one are refused with a tool result the model reads, a system
    message lands after the third, and the run still ends on the model's
    answer. The dedup cache is off so every allowed call really executes."""
    monkeypatch.setattr(al, "_DEDUP_CACHE_MAX_ENTRIES", 0)
    composite, executed = _executor_with(["search_lookup"])
    total = 8
    llm = _ScriptedLLM(
        [
            _tool_call_response(ToolCall(id=f"c{i}", name="search_lookup", arguments={"q": "x"}))
            for i in range(total)
        ]
        + [ChatResponse(content="Done.", model="stub")]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)

    response = await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=30,
        on_event=on_event,
    )

    assert response.content == "Done."
    assert executed == ["search_lookup"] * 4, "the stuck run stops executing by call 5"
    refusals = [m for m in messages if m.role == "tool" and m.content.startswith("[Loop breaker] Refused")]
    assert [m.tool_call_id for m in refusals] == ["c4", "c5", "c6", "c7"]
    # The loop tags what it answered itself: what the digest reads, never the text.
    assert [m.tool_call_id for m in messages if m.role == "tool" and m.not_run == "loop_breaker"] == [
        "c4",
        "c5",
        "c6",
        "c7",
    ]
    assert "this is call 5 to 'search_lookup' with identical arguments" in refusals[0].content
    assert "this is call 8 to 'search_lookup'" in refusals[-1].content
    nudges = [
        m
        for m in messages
        if m.role == "system" and m.content.startswith("[SYSTEM - repeated identical call]")
    ]
    assert len(nudges) == 1
    assert "'search_lookup' with identical arguments 3 times" in nudges[0].content
    # The warning lands after the third call's result and before the fourth's.
    third = _tool_messages(messages, "c2")[0]
    fourth = _tool_messages(messages, "c3")[0]
    assert messages.index(third) < messages.index(nudges[0]) < messages.index(fourth)
    refused_events = [e for e in events if e.get("status") == "tool_done" and e.get("loop_breaker")]
    assert len(refused_events) == 4
    assert all(e["success"] is False for e in refused_events)


@pytest.mark.asyncio
async def test_a_refused_call_opens_a_loop_span_and_a_warning_does_too(_quiet_langfuse, monkeypatch):  # noqa: F811
    monkeypatch.setattr(al, "_DEDUP_CACHE_MAX_ENTRIES", 0)
    spans: list[dict] = []

    def _record(parent_ctx, **kwargs):
        spans.append(kwargs)
        return None

    monkeypatch.setattr(al, "_lf_start_loop_span", _record)
    composite, _executed = _executor_with(["search_lookup"])
    llm = _ScriptedLLM(
        [
            _tool_call_response(ToolCall(id=f"c{i}", name="search_lookup", arguments={"q": "x"}))
            for i in range(5)
        ]
        + [ChatResponse(content="Done.", model="stub")]
    )
    await al.run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=30,
    )
    assert [(s["action"], s["count"], s["kind"]) for s in spans] == [
        ("warn", 3, "identical"),
        ("refuse", 5, "identical"),
    ]
    assert all(s["tool_name"] == "search_lookup" for s in spans)
    assert spans[1]["arguments"] == {"q": "x"}


@pytest.mark.asyncio
async def test_a_write_in_between_makes_the_re_read_legitimate(_quiet_langfuse, monkeypatch):  # noqa: F811
    """read, write, read, write ... eight times: no refusal, no warning - a
    re-read after a mutation is a new question."""
    monkeypatch.setattr(al, "_DEDUP_CACHE_MAX_ENTRIES", 0)
    composite, executed = _executor_with(["search_lookup", "github_create_issue"])
    script = []
    for i in range(8):
        script.append(_tool_call_response(ToolCall(id=f"r{i}", name="search_lookup", arguments={"q": "x"})))
        script.append(
            _tool_call_response(ToolCall(id=f"w{i}", name="github_create_issue", arguments={"title": f"t{i}"}))
        )
    script.append(ChatResponse(content="Done.", model="stub"))
    llm = _ScriptedLLM(script)
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    await al.run_agent_loop(
        llm=llm, messages=messages, tools=composite.all_tools, executor=composite, max_iterations=40
    )
    assert executed.count("search_lookup") == 8
    assert not any(m.role == "tool" and m.content.startswith("[Loop breaker]") for m in messages)
    assert not any(m.role == "system" and "repeated identical call" in m.content for m in messages)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("write", "check", "check_args"),
    [
        # The 26 Sep 2026 run: warned at the third capture, the fifth refused.
        ("edit_presentation", "capture_presentation_slides", {"presentation_id": "d", "slides": "3"}),
        ("workset_set_summary", "workset_next_pending", {}),
        ("computer_action", "computer_screenshot", {}),
        ("editJiraIssue", "getJiraIssue", {"issueIdOrKey": "X-1"}),
        ("notion-update-page", "notion-fetch", {"id": "p"}),
    ],
)
async def test_the_same_check_after_each_write_is_never_warned_or_refused(_quiet_langfuse, monkeypatch, write, check, check_args):  # noqa: F811
    """write, check, write, check ... eight times, the check with identical
    arguments every time and a verb the classifier does not know: no warning,
    no refusal - each check is a new question after the write before it."""
    monkeypatch.setattr(al, "_DEDUP_CACHE_MAX_ENTRIES", 0)
    composite, executed = _executor_with([write, check])
    script = []
    for i in range(8):
        script.append(_tool_call_response(ToolCall(id=f"w{i}", name=write, arguments={"target": "d", "n": i})))
        script.append(_tool_call_response(ToolCall(id=f"c{i}", name=check, arguments=dict(check_args))))
    script.append(ChatResponse(content="Done.", model="stub"))
    llm = _ScriptedLLM(script)
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    await al.run_agent_loop(
        llm=llm, messages=messages, tools=composite.all_tools, executor=composite, max_iterations=40
    )
    assert executed.count(check) == 8
    assert not any(m.role == "tool" and m.content.startswith("[Loop breaker]") for m in messages)
    assert not any(m.role == "system" and "repeated identical call" in m.content for m in messages)


@pytest.mark.asyncio
async def test_a_browser_call_in_between_does_not_launder_the_read_count(_quiet_langfuse, monkeypatch):  # noqa: F811
    """The shape of the 25 Sep run: ``read_file_content`` on one file, a
    ``browser`` screenshot, the same read, another screenshot... The shared
    classifier reads ``browser`` as a write, but a step of the agent's own
    browsing session mutates none of the files a read re-reads, so it must
    not start the read count over (a real write - ``github_create_issue`` -
    still does, see the test above). The fifth identical read is refused."""
    monkeypatch.setattr(al, "_DEDUP_CACHE_MAX_ENTRIES", 0)
    assert al._is_write_tool("browser"), "the premise: the classifier calls browser a write"
    assert not al._loop_breaker_write_resets("browser")
    assert al._is_write_tool("annotate_file"), "an unknown verb reads as a write"
    assert not al._loop_breaker_write_resets("annotate_file"), (
        "read, annotate, read, annotate is the DESIGNED sequence; a reset per annotate would never count it"
    )
    assert al._loop_breaker_write_resets("github_create_issue")
    assert al._loop_breaker_write_resets("edit_presentation")
    assert not al._loop_breaker_write_resets("search_lookup")
    composite, executed = _executor_with(["read_file_content", "browser"])
    script = []
    for i in range(6):
        script.append(_tool_call_response(ToolCall(id=f"r{i}", name="read_file_content", arguments={"file_id": "abc"})))
        script.append(
            _tool_call_response(
                ToolCall(id=f"b{i}", name="browser", arguments={"action": "navigate", "url": f"https://x.test/p{i}"})
            )
        )
    script.append(ChatResponse(content="Done.", model="stub"))
    llm = _ScriptedLLM(script)
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    await al.run_agent_loop(
        llm=llm, messages=messages, tools=composite.all_tools, executor=composite, max_iterations=40
    )
    assert executed.count("read_file_content") == 4, "the fifth and sixth reads are refused"
    refusals = [m for m in messages if m.role == "tool" and m.content.startswith("[Loop breaker] Refused")]
    assert [m.tool_call_id for m in refusals] == ["r4", "r5"]
    nudges = [m for m in messages if m.role == "system" and "repeated identical call" in m.content]
    assert len(nudges) == 1 and "'read_file_content' with identical arguments 3 times" in nudges[0].content
    # The browser calls are six different pages: none is refused.
    assert executed.count("browser") == 6


def _executor_returning(handlers: dict[str, object]) -> tuple[CompositeToolExecutor, list[str]]:
    """Composite executor whose handlers return a ``ToolResult`` (files, no_op) or raise."""
    executed: list[str] = []
    callable_exec = CallableToolExecutor()
    definitions = [ToolDefinition(name=n, description=f"{n} desc", source="custom:test") for n in handlers]

    def _make(name: str, outcome: object):
        async def _handler(**kwargs):
            executed.append(name)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        return _handler

    for name, outcome in handlers.items():
        callable_exec.register(name, _make(name, outcome))
    composite = CompositeToolExecutor()
    composite.add_tools(definitions, callable_exec)
    return composite, executed


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["no_op", "failure"])
async def test_a_write_that_took_no_effect_does_not_reset_the_read_count(_quiet_langfuse, monkeypatch, shape):  # noqa: F811
    """The other half of the 25 Sep run: identical reads interleaved with
    ``edit_presentation`` probes that changed nothing (``No change`` results,
    ``no_op=True``) or with writes that failed. Neither mutated anything a
    read re-reads, so neither starts the read count over: the fifth read is
    refused. (A write that TOOK effect still resets - see the test above.)"""
    monkeypatch.setattr(al, "_DEDUP_CACHE_MAX_ENTRIES", 0)
    if shape == "no_op":
        write_outcome: object = ToolResult(
            tool_call_id="", content="No change: nothing matched; nothing was saved (revision 3 kept).", no_op=True
        )
    else:
        write_outcome = RuntimeError("the deck is locked")
    composite, executed = _executor_returning(
        {
            "read_file_content": ToolResult(tool_call_id="", content="File: a.png - already viewed."),
            "edit_presentation": write_outcome,
        }
    )
    assert al._is_write_tool("edit_presentation")
    script = []
    for i in range(6):
        script.append(_tool_call_response(ToolCall(id=f"r{i}", name="read_file_content", arguments={"file_id": "abc"})))
        script.append(
            _tool_call_response(
                ToolCall(id=f"w{i}", name="edit_presentation", arguments={"presentation_id": "d", "operations": [{"op": i}]})
            )
        )
    script.append(ChatResponse(content="Done.", model="stub"))
    llm = _ScriptedLLM(script)
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    await al.run_agent_loop(
        llm=llm, messages=messages, tools=composite.all_tools, executor=composite, max_iterations=40
    )
    assert executed.count("read_file_content") == 4, "the fifth and sixth reads are refused"
    refusals = [m for m in messages if m.role == "tool" and m.content.startswith("[Loop breaker] Refused")]
    assert [m.tool_call_id for m in refusals] == ["r4", "r5"]
    # The probes themselves differ in their operations and are never refused.
    assert executed.count("edit_presentation") == 6


@pytest.mark.asyncio
async def test_the_loop_enters_every_attached_image_into_the_turns_vision_ledger(_quiet_langfuse, monkeypatch):  # noqa: F811
    """A screenshot, a rendered figure or a generated image attaches its
    pixels through its own result; the loop records the file in the shared
    ``FileContext.vision_views`` so a later ``read_file_content`` is the
    second view, not a fresh one. A non-image file is not a view."""
    from app.llm.base import FileRef
    from app.tools.base import FileContext

    monkeypatch.setattr(al, "_DEDUP_CACHE_MAX_ENTRIES", 0)
    shot = FileRef.from_bytes(b"\x89PNG shot", "shot.png", "image/png")
    sheet = FileRef.from_bytes(b"a,b\n1,2", "data.csv", "text/csv")
    composite, _executed = _executor_returning(
        {
            "browser": ToolResult(tool_call_id="", content=f"[[FILE:{shot.file_id}]] screenshot taken", files=[shot]),
            "generate_file": ToolResult(tool_call_id="", content=f"[[FILE:{sheet.file_id}]]", files=[sheet]),
        }
    )
    ctx = FileContext()
    llm = _ScriptedLLM(
        [
            _tool_call_response(ToolCall(id="b0", name="browser", arguments={"action": "screenshot"})),
            _tool_call_response(
                ToolCall(id="g0", name="generate_file", arguments={"filename": "data.csv", "content": "a,b"}),
                ToolCall(id="b1", name="browser", arguments={"action": "screenshot", "full_page": True}),
            ),
            ChatResponse(content="Done.", model="stub"),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    await al.run_agent_loop(
        llm=llm,
        messages=messages,
        tools=composite.all_tools,
        executor=composite,
        max_iterations=10,
        file_context=ctx,
    )
    assert ctx.vision_step(shot.file_id) == 1, "recorded at the step of its FIRST attachment (the sequential path)"
    assert ctx.vision_step(sheet.file_id) is None, "a CSV is not a view"
    assert set(ctx.vision_views) == {shot.file_id}
    # The second screenshot re-attached the SAME file after a request had
    # carried it: the loop, not the tool, holds the once-per-turn rule for a
    # tool that does not consult the ledger - the pixels are dropped with the
    # note that dates the view, the Markdown the reply needs is kept.
    b0, _g0, b1 = (m for m in messages if m.role == "tool")
    assert b0.files == [shot] or "Already viewed at step 1" in b0.content, "seen, then possibly pruned by retention"
    assert b1.files is None
    assert "Already viewed at step 1" in b1.content and "call read_file_content" not in b1.content
    assert f"[[FILE:{shot.file_id}]]" in b1.content


class _RecordingLLM(_ScriptedLLM):
    """A scripted LLM that keeps the transcript of every request it answered,
    and can strip the files off every tool message BEFORE answering request
    *n* - the state a request goes out in when the emergency overflow prune
    took the pixels first."""

    def __init__(self, script, *, strip_files_before_request: int | None = None) -> None:
        super().__init__(script)
        self.requests: list[list[ChatMessage]] = []
        self.last_messages: list[ChatMessage] = []
        self._strip_at = strip_files_before_request

    async def chat(self, messages, model=None, tools=None, **kwargs):
        if self._strip_at is not None and len(self.requests) + 1 == self._strip_at:
            for m in messages:
                if m.role == "tool" and m.files:
                    al._drop_tool_result_files(m)
        self.requests.append(list(messages))
        self.last_messages = messages
        return await super().chat(messages, model=model, tools=tools, **kwargs)


def _read_tool(monkeypatch):
    """The platform's real ``read_file_content`` (+ ``annotate_file``), on a
    fresh ``FileContext`` holding the images named."""
    from tests.test_file_read_edit_tools import _build, _image_context

    defs, executor = _build(monkeypatch, [])
    return defs, executor, _image_context


def _read_call(i: int, file_id: str) -> ChatResponse:
    return _tool_call_response(ToolCall(id=f"r{i}", name="read_file_content", arguments={"file_id": file_id}))


@pytest.mark.asyncio
async def test_a_view_is_recorded_when_a_request_carried_the_pixels_never_when_the_result_was_appended(
    _quiet_langfuse,  # noqa: F811
    monkeypatch,
    caplog,
):
    """Marking the image as viewed at APPEND time
    made a result the emergency prune removed before any request an
    "already viewed" that never was - the next read returned notes without
    pixels, and the file-recovery guard could not count or pin that retry.

    Pinned both ways. (a) append -> pruned before any request -> the re-read
    carries the pixels again, and the guard counts the pixel-less answer.
    (b) append -> a request carried it -> the re-read returns the notes, dated
    with the step the tool ran in.
    """
    import logging

    from app.tools.base import CompositeToolExecutor

    monkeypatch.setattr(al, "_DEDUP_CACHE_MAX_ENTRIES", 0)
    defs, executor, image_context = _read_tool(monkeypatch)
    ctx, (ref,) = image_context("founders.png")
    composite = CompositeToolExecutor()
    composite.add_tools(defs, executor)
    # r1 at step 1 is stripped before request 2 (the one that would carry
    # it); r2 at step 2 carries the pixels; request 3 carries them; r3 at
    # step 3 is the second view.
    llm = _RecordingLLM(
        [_read_call(1, ref.file_id), _read_call(2, ref.file_id), _read_call(3, ref.file_id), ChatResponse(content="Done.", model="stub")],
        strip_files_before_request=2,
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    with caplog.at_level(logging.WARNING, logger="app.tools.agent_loop"):
        await al.run_agent_loop(
            llm=llm, messages=messages, tools=composite.all_tools, executor=composite, max_iterations=10, file_context=ctx
        )
    r1, r2, r3 = (m for m in messages if m.role == "tool")
    # (a) r1 went out without its pixels: not a view. r2 carries them again.
    assert r1.files is None and "call read_file_content" in r1.content, "the honest recovery note, never 'already viewed'"
    assert r2.files == [ref], "a result no request carried was never seen: the pixels come again"
    assert "the image is attached to this result" in (r2.content or "") or r2.files == [ref]
    assert any("reached the model WITHOUT its pixels" in rec.getMessage() for rec in caplog.records), (
        "the file-recovery guard counted the pixel-less answer, so it can pin the retry"
    )
    # (b) request 3 carried r2: the ledger dates the view at step 2 (the step
    # r2's tool ran in), and r3 is answered from the ledger.
    assert ctx.vision_step(ref.file_id) == 2
    assert r3.files is None
    assert "already viewed at step 2 this turn" in r3.content
    assert "annotate_file" in r3.content and "call read_file_content" not in r3.content


def _screenshot_tool(shots: list):
    """A ``browser`` handler that captures a NEW image on every call."""
    from app.llm.base import FileRef

    async def _browser(**_kwargs):
        n = len(shots) + 1
        ref = FileRef.from_bytes(b"\x89PNG shot %d" % n, f"shot{n}.png", "image/png")
        shots.append(ref)
        return ToolResult(
            tool_call_id="",
            content=(
                f"Screenshot captured and saved as shot{n}.png. The PNG is attached as a native "
                f"vision block - you can see the pixels. Include this EXACT Markdown in your "
                f"response: ![Screenshot](/api/v1/chat/files/{ref.file_id}/download)"
            ),
            files=[ref],
        )

    return _browser


def _mixed_executor(monkeypatch, shots: list):
    """``browser`` (a new screenshot per call) + the real ``read_file_content``."""
    from app.tools.base import CompositeToolExecutor

    defs, executor, image_context = _read_tool(monkeypatch)
    ctx, refs = image_context("a.png", "b.png")
    browser_exec = CallableToolExecutor()
    browser_exec.register("browser", _screenshot_tool(shots))
    composite = CompositeToolExecutor()
    composite.add_tools(defs, executor)
    composite.add_tools([ToolDefinition(name="browser", description="browser", source="custom:test")], browser_exec)
    return composite, ctx, refs


@pytest.mark.asyncio
async def test_the_vision_cap_is_applied_by_the_loop_to_screenshots_and_reads_alike(_quiet_langfuse, monkeypatch, caplog):  # noqa: F811
    """The cap was enforced inside ``read_file_content``
    only, so seven screenshots sent seven images. It is now applied where every
    tool result is appended: past ``AGENT_LOOP_VISION_VIEWS_PER_TURN`` distinct
    images - the ledger plus the images still in flight in the current batch -
    the result keeps its text and loses its pixels, with the explicit budget
    note, whatever tool produced it. A scripted run mixing screenshots and reads:
    cap 3; two screenshots in ONE batch (both in flight before any request),
    then a read (the third distinct image), then a screenshot and a read of a
    file never shown - both refused by the loop.
    """
    import logging

    from app.core.config import settings

    monkeypatch.setattr(settings, "agent_loop_vision_views_per_turn", 3)
    monkeypatch.setattr(al, "_DEDUP_CACHE_MAX_ENTRIES", 0)
    shots: list = []
    composite, ctx, (a, b) = _mixed_executor(monkeypatch, shots)
    b.notes = "b: the OLD seal"
    llm = _RecordingLLM(
        [
            _tool_call_response(
                ToolCall(id="s1", name="browser", arguments={"action": "screenshot"}),
                ToolCall(id="s2", name="browser", arguments={"action": "screenshot", "full_page": True}),
            ),
            _read_call(1, a.file_id),
            _tool_call_response(ToolCall(id="s3", name="browser", arguments={"action": "screenshot", "selector": "#x"})),
            _read_call(2, b.file_id),
            ChatResponse(content="Done.", model="stub"),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    with caplog.at_level(logging.WARNING, logger="app.tools.agent_loop"):
        await al.run_agent_loop(
            llm=llm, messages=messages, tools=composite.all_tools, executor=composite, max_iterations=10, file_context=ctx
        )
    s1, s2, r1, s3, r2 = (m for m in messages if m.role == "tool")
    # Within the budget: both screenshots of the first batch (in flight
    # together, spent together) and the read of a.
    assert s1.files == [shots[0]] and s2.files == [shots[1]]
    assert r1.files == [a]
    # Past it: the screenshot AND the read lose their pixels at the loop's
    # boundary, keep their text (minus the attachment claim) and carry the
    # explicit note naming the file, the count and the cap - never a silent
    # send, never an invitation to read again.
    for refused, ref in ((s3, shots[2]), (r2, b)):
        assert refused.files is None, refused.content
        assert "[Image(s) not attached - vision budget exhausted for this turn: 3 distinct images already shown (the cap is 3)" in refused.content
        assert ref.filename in refused.content and ref.file_id in refused.content
        assert "is attached" not in refused.content.split("[Image(s) not attached")[0]
        assert "call read_file_content" not in refused.content
    assert "your notes: b: the OLD seal" in r2.content
    assert "no notes saved" in s3.content
    assert f"![Screenshot](/api/v1/chat/files/{shots[2].file_id}/download)" in s3.content, "the Markdown the reply needs survives"
    assert not any(m.role == "tool" and m.files for m in (s3, r2))
    # The ledger holds exactly the three images a request carried.
    assert set(ctx.vision_views) == {shots[0].file_id, shots[1].file_id, a.file_id}
    assert ctx.vision_step(shots[0].file_id) == 1 and ctx.vision_step(a.file_id) == 2
    # A budget refusal is a result appended WITHOUT files: the file-recovery
    # guard never counts it as a pixel-less answer (and never pins a retry).
    assert not any("reached the model WITHOUT its pixels" in rec.getMessage() for rec in caplog.records)
    assert any("Vision budget" in rec.getMessage() for rec in caplog.records)


@pytest.mark.asyncio
async def test_a_dedup_cache_replay_of_a_shown_image_is_not_re_sent(_quiet_langfuse, monkeypatch):  # noqa: F811
    """The sequential cache-hit path appends the cached files on its own site;
    that site crosses the vision boundary too, so a replay of a
    figure the model has been shown is answered with the dated note, never
    the pixels a second time - the once-per-turn rule for a tool that does
    not consult the ledger itself."""
    from app.llm.base import FileRef
    from app.tools.base import FileContext

    figure = FileRef.from_bytes(b"\x89PNG figure", "figure.png", "image/png")
    composite, executed = _executor_returning(
        {
            "fetch_image_asset": ToolResult(tool_call_id="", content=f"[[FILE:{figure.file_id}]] The PNG is attached to this result.", files=[figure]),
        }
    )
    assert not al._is_dedup_exempt("fetch_image_asset"), "the premise: a fetch is a read the cache may replay"
    ctx = FileContext()
    same = {"url": "https://x.test/figure.png"}
    llm = _RecordingLLM(
        [
            _tool_call_response(ToolCall(id="c1", name="fetch_image_asset", arguments=same)),
            _tool_call_response(ToolCall(id="c2", name="fetch_image_asset", arguments=same)),
            ChatResponse(content="Done.", model="stub"),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    await al.run_agent_loop(
        llm=llm, messages=messages, tools=composite.all_tools, executor=composite, max_iterations=10, file_context=ctx
    )
    assert executed == ["fetch_image_asset"], "the second call is a cache replay"
    _c1, c2 = (m for m in messages if m.role == "tool")
    assert al._DEDUP_CACHE_MARKER in c2.content
    assert c2.files is None
    assert "Already viewed at step 1" in c2.content and "call read_file_content" not in c2.content
    assert "is attached" not in c2.content.split("[Image(s) removed")[0]
    assert figure.file_id in c2.content, "the note names the file the replay would have re-sent"
    assert ctx.vision_step(figure.file_id) == 1


@pytest.mark.asyncio
async def test_a_zero_cap_means_no_budget_at_the_loop(_quiet_langfuse, monkeypatch):  # noqa: F811
    from app.core.config import settings

    monkeypatch.setattr(settings, "agent_loop_vision_views_per_turn", 0)
    monkeypatch.setattr(al, "_DEDUP_CACHE_MAX_ENTRIES", 0)
    shots: list = []
    composite, ctx, _refs = _mixed_executor(monkeypatch, shots)
    llm = _RecordingLLM(
        [
            _tool_call_response(ToolCall(id=f"s{i}", name="browser", arguments={"action": "screenshot", "n": i}))
            for i in range(8)
        ]
        + [ChatResponse(content="Done.", model="stub")]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    await al.run_agent_loop(
        llm=llm, messages=messages, tools=composite.all_tools, executor=composite, max_iterations=12, file_context=ctx
    )
    results = [m for m in messages if m.role == "tool"]
    assert len(results) == 8
    assert not any("vision budget exhausted" in m.content for m in results), "no cap: nothing is refused"
    assert len(ctx.vision_views) == 8, "every distinct screenshot reached the model"


def test_the_vision_budget_follows_the_step_budget(monkeypatch):
    """The cap is per 25 steps, so a run allowed more steps sees more."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "agent_loop_vision_views_per_turn", 10)
    assert [al.vision_cap_for(n) for n in (1, 5, 25, 26, 100, 1000)] == [10, 10, 10, 20, 40, 400]
    monkeypatch.setattr(settings, "agent_loop_vision_views_per_turn", 0)
    assert al.vision_cap_for(100) == 0, "0 is still no cap"


@pytest.mark.asyncio
async def test_a_run_allowed_more_steps_looks_at_more_images(_quiet_langfuse, monkeypatch):  # noqa: F811
    """A video run ran out of sight at a cap sized for any turn. With a
    cap of 2 per 25 steps, a run of 26 steps shows 4 screenshots and only the
    fifth is refused - where a 10-step run would have refused the third."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "agent_loop_vision_views_per_turn", 2)
    monkeypatch.setattr(al, "_DEDUP_CACHE_MAX_ENTRIES", 0)
    shots: list = []
    composite, ctx, _refs = _mixed_executor(monkeypatch, shots)
    llm = _RecordingLLM(
        [
            _tool_call_response(ToolCall(id=f"s{i}", name="browser", arguments={"action": "screenshot", "n": i}))
            for i in range(5)
        ]
        + [ChatResponse(content="Done.", model="stub")]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    await al.run_agent_loop(
        llm=llm, messages=messages, tools=composite.all_tools, executor=composite, max_iterations=26, file_context=ctx
    )
    results = [m for m in messages if m.role == "tool"]
    refused = [m for m in results if "vision budget exhausted" in m.content]
    assert refused == [results[-1]] and "the cap is 4" in refused[0].content
    assert set(ctx.vision_views) == {shot.file_id for shot in shots[:4]}, "the first four reached the model"


def _file_reader() -> tuple[CompositeToolExecutor, list[str]]:
    """A ``get_github_file`` whose result is a 5 KB source file: large enough
    for the tool-result curation to clear once it is not the newest result."""
    executed: list[str] = []
    callable_exec = CallableToolExecutor()

    async def _get_github_file(**kwargs):
        executed.append(kwargs["path"])
        return f"File: {kwargs['path']} (size: 5000 bytes, branch: main)\n\n" + "x" * 5000

    callable_exec.register("get_github_file", _get_github_file)
    composite = CompositeToolExecutor()
    composite.add_tools([ToolDefinition(name="get_github_file", description="read", source="custom:test")], callable_exec)
    return composite, executed


#: What the model writes with each read in the scenarios whose results the
#: curation must be free to clear: substantive text releases every earlier
#: result of the turn from the turn protection (a tool-call-only turn keeps
#: them - see ``test_a_tool_call_only_turn_keeps_its_reads_so_a_re_read_is_a_repeat``).
_WRITTEN = "Reading the deck stage and its stylesheet to find why the slide is pillarboxed: " + "notes " * 150


def _read(call_id: str, path: str, *, written: bool) -> ChatResponse:
    call = ToolCall(id=call_id, name="get_github_file", arguments={"repo": "XTM-One-Platform/xtm-one", "path": path})
    if not written:
        return _tool_call_response(call)
    return ChatResponse(content=_WRITTEN, model="stub", stop_reason="tool_calls", tool_calls=[call])


def _alternating_reads(rounds: int, *, written: bool = True) -> list[ChatResponse]:
    paths = ("frontend/src/components/presentation/DeckStage.tsx", "frontend/src/components/presentation/presentation.css")
    script = [_read(f"r{i}", paths[i % 2], written=written) for i in range(rounds * 2)]
    return [*script, ChatResponse(content="Done.", model="stub")]


@pytest.mark.asyncio
async def test_a_re_read_the_curation_invited_is_never_warned_or_refused(_quiet_langfuse, monkeypatch):  # noqa: F811
    """The 26 Sep 2026 run, end to end: two source files read in alternation
    under a live budget too small to hold both, so the curation clears the
    older one on every iteration with "it is stale and re-fetchable. Re-call
    the tool only if you still need this exact data" - and the model does.
    Twelve reads: each file executes once and is replayed from the dedup
    cache after that, the breaker never warns and never refuses, and the
    user is never told the same call is being repeated. (The model writes
    about what it read with every call, so the turn protection has
    released the older result and the curation may clear it.)"""
    monkeypatch.setattr(al, "_compute_live_tool_result_budget", lambda _window: 1_000)
    composite, executed = _file_reader()
    llm = _ScriptedLLM(_alternating_reads(6))
    messages: list[ChatMessage] = [ChatMessage(role="user", content="why does the deck pillarbox?")]
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)

    response = await al.run_agent_loop(
        llm=llm, messages=messages, tools=composite.all_tools, executor=composite, max_iterations=30, on_event=on_event
    )
    assert response.content == "Done."
    assert executed == [
        "frontend/src/components/presentation/DeckStage.tsx",
        "frontend/src/components/presentation/presentation.css",
    ], "each file is fetched once; every later read is the cache's replay"
    replays = [e for e in events if e.get("status") == "tool_done" and e.get("cached")]
    assert len(replays) == 10
    assert not any(m.role == "tool" and m.content.startswith("[Loop breaker]") for m in messages)
    assert not any(m.role == "system" and "repeated identical call" in m.content for m in messages)
    assert not any(
        e.get("status") == "thinking_text" and "has been called" in e.get("content", "") for e in events
    ), "nothing was said to the user about a repeat"
    # The premise: the curation really did clear the older result each time.
    stubs = [m for m in messages if m.role == "tool" and al._is_refetchable_stub(m.content)]
    assert len(stubs) >= 10, "the older read is cleared on every iteration"


@pytest.mark.asyncio
async def test_a_tool_call_only_turn_keeps_its_reads_so_a_re_read_is_a_repeat(_quiet_langfuse, monkeypatch):  # noqa: F811
    """How the turn protection and the clearing compose, with no hole between
    them: the same twelve reads under the same tiny budget, but the model never writes a word. The
    turn protection keeps every read of the turn out of the byte-budget
    clearing, so no answer is taken away, no re-read is invited, and the
    held-answer rule counts them as the repeats they are - warned at three,
    refused from the fifth ask per file. Fewer clearings, and the ones that
    happen are the only ones forgiven."""
    monkeypatch.setattr(al, "_compute_live_tool_result_budget", lambda _window: 1_000)
    composite, executed = _file_reader()
    llm = _ScriptedLLM(_alternating_reads(6, written=False))
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    await al.run_agent_loop(
        llm=llm, messages=messages, tools=composite.all_tools, executor=composite, max_iterations=30
    )
    assert len(executed) == 2
    assert not any(m.role == "tool" and al._is_refetchable_stub(m.content) for m in messages), "nothing was cleared"
    refusals = [m for m in messages if m.role == "tool" and m.content.startswith("[Loop breaker] Refused")]
    assert [m.tool_call_id for m in refusals] == ["r8", "r9", "r10", "r11"], "the fifth and sixth ask per file"


@pytest.mark.asyncio
async def test_the_same_reads_with_the_answers_still_held_are_a_loop(_quiet_langfuse, monkeypatch):  # noqa: F811
    """The contrast: the same twelve reads under a budget that holds both
    files. Now every re-read asks for an answer the model has in front of
    it - warned at the third ask per file, refused from the fifth, the user
    told which tool and how many times."""
    composite, executed = _file_reader()
    llm = _ScriptedLLM(_alternating_reads(6))
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)

    await al.run_agent_loop(
        llm=llm, messages=messages, tools=composite.all_tools, executor=composite, max_iterations=30, on_event=on_event
    )
    assert len(executed) == 2
    refusals = [m for m in messages if m.role == "tool" and m.content.startswith("[Loop breaker] Refused")]
    assert [m.tool_call_id for m in refusals] == ["r8", "r9", "r10", "r11"], "the fifth and sixth ask per file"
    warnings = [e["content"] for e in events if e.get("status") == "thinking_text" and "has been called" in e.get("content", "")]
    assert warnings == [
        "⚠️ 'get_github_file' has been called 3 times with identical arguments this turn. "
        "I will change approach or report what I have instead of asking again."
    ] * 2


_DECK_STAGE = "frontend/src/components/presentation/DeckStage.tsx"
_PRESENTATION_CSS = "frontend/src/components/presentation/presentation.css"


class _ReadWhileToolsAreOffered(_ScriptedLLM):
    """Asks for the next read of *paths* (cycling) on every request that
    offers tools, and answers the moment the loop strips them - the shape of a
    model that will re-read a cleared file for as long as it is allowed to."""

    def __init__(self, paths: list[str], *, written: bool = False) -> None:
        super().__init__([])
        self._paths = paths
        self._written = written
        self.requests = 0

    async def chat(self, messages, model=None, tools=None, **kwargs):
        self.offered_tools.append([t.name for t in (tools or [])])
        self.requests += 1
        if tools:
            path = self._paths[(self.requests - 1) % len(self._paths)]
            return _read(f"r{self.requests}", path, written=self._written)
        return ChatResponse(content="Done.", model="stub")


@pytest.mark.asyncio
async def test_with_the_cache_off_an_identical_re_read_is_still_refused_because_the_collapse_keeps_the_newest_copy(
    _quiet_langfuse,  # noqa: F811
    monkeypatch,
):
    """Does the forgiveness reopen the one-key loop when the dedup cache is
    off and every re-read executes for real? No - and not thanks to the
    breaker's bound alone. Each re-read appends the SAME bytes, and the
    curation's first pass (``_collapse_redundant_tool_results``) runs before
    the stale clearing: the older identical bodies become ``[Duplicate ...]``
    stubs that say the newest copy is still in the conversation - which
    invites nothing and stays counted - so the fifth ask is refused exactly as
    before. Once the refusals are the newest results the clearing may
    stub that newest real copy too, but the refused asks are held whatever
    happens to their message, so the key stays at the bound and the failure
    brake winds the run down."""
    monkeypatch.setattr(al, "_DEDUP_CACHE_MAX_ENTRIES", 0)
    monkeypatch.setattr(al, "_compute_live_tool_result_budget", lambda _window: 1_000)
    composite, executed = _file_reader()
    llm = _ReadWhileToolsAreOffered([_DECK_STAGE])
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    response = await al.run_agent_loop(
        llm=llm, messages=messages, tools=composite.all_tools, executor=composite, max_iterations=30
    )
    assert response.content == "Done."
    assert executed == [_DECK_STAGE] * 4, "four real reads, then the refusals"
    refusals = [m for m in messages if m.role == "tool" and m.content.startswith("[Loop breaker] Refused")]
    assert [m.tool_call_id for m in refusals][:2] == ["r5", "r6"]
    assert all(m.tool_call_id.startswith("r") and int(m.tool_call_id[1:]) >= 5 for m in refusals), "every ask from the fifth on"
    duplicates = [m for m in messages if m.role == "tool" and m.content.startswith("[Duplicate ")]
    assert len(duplicates) == 3, "the three older identical copies were folded into the newest, not cleared"
    assert llm.requests < 30, "the brake ended the run well before the cap"


@pytest.mark.asyncio
async def test_with_the_cache_off_and_every_answer_cleared_the_run_ends_at_the_iteration_cap(_quiet_langfuse, monkeypatch):  # noqa: F811
    """The other shape: two files read in alternation under a live budget that
    holds one, with no dedup cache to replay them. Every older answer is the
    re-fetchable stub the clearing wrote (a different body each time, so the
    collapse has nothing to fold), so every re-read is the re-read the harness
    invited: it executes for real and is never warned, never refused -
    refusing it would be the defect. What bounds the run is the step
    budget, not the breaker: the 60% nudge lands, the wind-down strips the
    tools, the model has to answer, and the run ends at ``max_iterations`` -
    the hard backstop the breaker was never meant to replace. (The model
    writes with every read, so the turn protection has released the
    older answers to the clearing.)"""
    monkeypatch.setattr(al, "_DEDUP_CACHE_MAX_ENTRIES", 0)
    monkeypatch.setattr(al, "_compute_live_tool_result_budget", lambda _window: 1_000)
    composite, executed = _file_reader()
    llm = _ReadWhileToolsAreOffered([_DECK_STAGE, _PRESENTATION_CSS], written=True)
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)

    response = await al.run_agent_loop(
        llm=llm, messages=messages, tools=composite.all_tools, executor=composite, max_iterations=15, on_event=on_event
    )
    assert response.content == "Done."
    assert llm.offered_tools[-1] == [], "the run ended because the wind-down stripped the tools, not because the model stopped"
    assert 9 <= len(executed) < 15, "every read the model asked for while tools were offered really executed"
    assert executed == [_DECK_STAGE, _PRESENTATION_CSS] * (len(executed) // 2) + [_DECK_STAGE] * (len(executed) % 2)
    assert not any(m.role == "tool" and m.content.startswith("[Loop breaker]") for m in messages), "never refused"
    assert not any(m.role == "system" and "repeated identical call" in m.content for m in messages), "never warned"
    assert any(m.role == "system" and m.content.startswith("[SYSTEM - step budget]") for m in messages), "the 60% nudge is the backstop"
    assert any(e.get("status") == "wind_down" for e in events)
    # The premise: every answer but the newest two was cleared to the re-fetchable stub.
    tool_results = [m for m in messages if m.role == "tool"]
    assert sum(1 for m in tool_results if al._is_refetchable_stub(m.content)) >= len(tool_results) - 2


class _RecordingBreaker(al._LoopBreaker):
    """The loop's own breaker, kept reachable after the run."""

    instances: list[al._LoopBreaker] = []

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        _RecordingBreaker.instances.append(self)


@pytest.mark.asyncio
async def test_the_loop_records_an_answer_for_every_ask_of_a_parallel_batch(_quiet_langfuse, monkeypatch):  # noqa: F811
    """Three identical calls in ONE batch (the second and third are the
    batch dedup's mirrors of the first) and a fourth, different call: four
    asks screened before anything ran, four results appended one by one on the
    parallel path, every ask answered by its own message - the ids the model
    minted are never consulted."""
    monkeypatch.setattr(al, "_DEDUP_CACHE_MAX_ENTRIES", 0)
    _RecordingBreaker.instances.clear()
    monkeypatch.setattr(al, "_LoopBreaker", _RecordingBreaker)
    composite, executed = _executor_with(["search_lookup"])
    same = {"q": "x"}
    llm = _ScriptedLLM(
        [
            _tool_call_response(
                ToolCall(id="c1", name="search_lookup", arguments=same),
                ToolCall(id="c2", name="search_lookup", arguments=same),
                ToolCall(id="c3", name="search_lookup", arguments=same),
                ToolCall(id="c4", name="search_lookup", arguments={"q": "y"}),
            ),
            ChatResponse(content="Done.", model="stub"),
        ]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    await al.run_agent_loop(
        llm=llm, messages=messages, tools=composite.all_tools, executor=composite, max_iterations=10
    )
    assert executed == ["search_lookup"] * 2, "the batch dedup executed the identical call once"
    (breaker,) = _RecordingBreaker.instances
    same_asks = breaker._identical[al._dedup_key("search_lookup", same)]
    other_asks = breaker._identical[al._dedup_key("search_lookup", {"q": "y"})]
    assert len(same_asks) == 3 and len(other_asks) == 1
    assert all(a.answer is not None for a in same_asks + other_asks), "no ask left unanswered"
    assert sorted(a.answer.tool_call_id for a in same_asks) == ["c1", "c2", "c3"], "one distinct message per ask"
    assert other_asks[0].answer.tool_call_id == "c4"
    assert all(any(a.answer is m for m in messages) for a in same_asks + other_asks), "the recorded objects ARE the transcript's"
    assert [a.answer_len for a in same_asks] == [len(a.answer.content) for a in same_asks]


@pytest.mark.asyncio
async def test_an_identical_write_is_refused_too(_quiet_langfuse, monkeypatch):  # noqa: F811
    """The breaker is not a read cache: a write repeated with identical
    arguments five times is a stuck run whatever the write answered."""
    composite, executed = _executor_with(["github_create_issue"])
    llm = _ScriptedLLM(
        [
            _tool_call_response(
                ToolCall(id=f"w{i}", name="github_create_issue", arguments={"title": "same"})
            )
            for i in range(6)
        ]
        + [ChatResponse(content="Done.", model="stub")]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    await al.run_agent_loop(
        llm=llm, messages=messages, tools=composite.all_tools, executor=composite, max_iterations=30
    )
    assert executed == ["github_create_issue"] * 4
    assert [m.tool_call_id for m in messages if m.role == "tool" and m.content.startswith("[Loop breaker]")] == [
        "w4",
        "w5",
    ]


@pytest.mark.asyncio
async def test_the_step_budget_nudge_lands_once_at_sixty_percent(_quiet_langfuse, monkeypatch):  # noqa: F811
    """Fifteen iterations, three different tools: at the top of iteration 10
    (nine used, 60%) the model is told where the steps went, once - and
    before the wind-down nudge, which comes four iterations from the cap."""
    monkeypatch.setattr(al, "_DEDUP_CACHE_MAX_ENTRIES", 0)
    composite, _executed = _executor_with(["alpha_lookup", "beta_lookup", "gamma_lookup"])
    names = ["alpha_lookup"] * 4 + ["beta_lookup"] * 3 + ["gamma_lookup"] * 3
    llm = _ScriptedLLM(
        [
            _tool_call_response(ToolCall(id=f"c{i}", name=name, arguments={"q": str(i)}))
            for i, name in enumerate(names)
        ]
        + [ChatResponse(content="Done.", model="stub")]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    await al.run_agent_loop(
        llm=llm, messages=messages, tools=composite.all_tools, executor=composite, max_iterations=15
    )
    nudges = [m for m in messages if m.role == "system" and m.content.startswith("[SYSTEM - step budget]")]
    assert len(nudges) == 1
    assert "60% of your step budget (9 of 15 iterations)" in nudges[0].content
    assert "alpha_lookup (4), beta_lookup (3), gamma_lookup (2)" in nudges[0].content
    # After the ninth call's result (nine iterations used), before the tenth's.
    ninth = _tool_messages(messages, "c8")[0]
    tenth = _tool_messages(messages, "c9")[0]
    assert messages.index(ninth) < messages.index(nudges[0]) < messages.index(tenth)


@pytest.mark.asyncio
async def test_the_step_budget_nudge_never_lands_on_a_tool_stripped_iteration(_quiet_langfuse, monkeypatch):  # noqa: F811
    """On a three-iteration run 60% lands on the LAST iteration, where the
    Stage-2 strip has already taken the tools: "finish or report instead of
    retrying" next to a request that offers nothing to retry is noise, and
    a second message about the budget. Nothing is injected."""
    monkeypatch.setattr(al, "_DEDUP_CACHE_MAX_ENTRIES", 0)
    composite, _executed = _executor_with(["alpha_lookup"])
    llm = _ScriptedLLM(
        [
            _tool_call_response(ToolCall(id=f"c{i}", name="alpha_lookup", arguments={"q": str(i)}))
            for i in range(3)
        ]
        + [ChatResponse(content="Done.", model="stub")]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    await al.run_agent_loop(
        llm=llm, messages=messages, tools=composite.all_tools, executor=composite, max_iterations=3
    )
    assert not any(m.role == "system" and m.content.startswith("[SYSTEM - step budget]") for m in messages)


@pytest.mark.asyncio
async def test_the_near_duplicate_nudge_yields_to_the_breaker(_quiet_langfuse, monkeypatch):  # noqa: F811
    """A dedup-eligible read repeated: the breaker counts the same key one
    call earlier, warns and refuses; the near-duplicate nudge would be a
    second system message about the same repeat. With the breaker on it stays
    silent (the guard's own test runs with the breaker off)."""
    monkeypatch.setattr(al, "_DEDUP_CACHE_MAX_ENTRIES", 0)
    composite, _executed = _executor_with(["search_lookup"])
    llm = _ScriptedLLM(
        [_tool_call_response(ToolCall(id=f"c{i}", name="search_lookup", arguments={"q": "x"})) for i in range(6)]
        + [ChatResponse(content="Done.", model="stub")]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    await al.run_agent_loop(
        llm=llm, messages=messages, tools=composite.all_tools, executor=composite, max_iterations=30
    )
    system = [m.content for m in messages if m.role == "system"]
    assert any(text.startswith("[SYSTEM - repeated identical call]") for text in system)
    assert not any("repeated identical read" in text for text in system), "one nudge for one stuck pattern"


@pytest.mark.asyncio
async def test_the_step_budget_nudge_yields_to_a_wind_down_at_the_same_iteration(
    _quiet_langfuse, monkeypatch  # noqa: F811
):
    """On a ten-iteration run the wind-down fires at iteration 6 - the same
    one 60% lands on - so the model reads one message about its budget."""
    monkeypatch.setattr(al, "_DEDUP_CACHE_MAX_ENTRIES", 0)
    composite, _executed = _executor_with(["alpha_lookup"])
    llm = _ScriptedLLM(
        [
            _tool_call_response(ToolCall(id=f"c{i}", name="alpha_lookup", arguments={"q": str(i)}))
            for i in range(8)
        ]
        + [ChatResponse(content="Done.", model="stub")]
    )
    messages: list[ChatMessage] = [ChatMessage(role="user", content="go")]
    await al.run_agent_loop(
        llm=llm, messages=messages, tools=composite.all_tools, executor=composite, max_iterations=10
    )
    assert not any(m.role == "system" and m.content.startswith("[SYSTEM - step budget]") for m in messages)


# ─────────────────────────────────────────────────────────────────────────
# Declared positions: a part of one target per distinct position
# ─────────────────────────────────────────────────────────────────────────

_FRAME_TOOL = ToolDefinition(
    name="frame_of_video",
    description="A frame of a video.",
    position_arguments=("at_seconds", "region"),
    position_budget=60,
)


def _frames_breaker() -> _LoopBreaker:
    return _breaker(declared_positions=al._declared_positions([_FRAME_TOOL]))


def test_thirty_moments_of_one_video_are_thirty_positions_and_an_identical_repeat_is_still_refused():
    """A QA sweep of one MP4 asked ten frames and was refused at the tenth: distinct
    moments are distinct work. The same moment asked again is a repeat as before."""
    breaker = _frames_breaker()
    actions = [breaker.screen("frame_of_video", {"file_id": "mp4", "at_seconds": float(t)}).action for t in range(30)]
    assert actions == ["allow"] * 30 and breaker.events == []
    repeats = [breaker.screen("frame_of_video", {"file_id": "mp4", "at_seconds": 0.0}).action for _ in range(4)]
    assert repeats[-1] == "refuse" and breaker.events[-1].kind == "identical"


def test_an_undeclared_tool_keeps_the_similar_bound_on_one_target():
    """Without the declaration a moment is no position: ten frames of one file are ten reads of it."""
    breaker = _breaker()
    actions = [breaker.screen("frame_of_video", {"file_id": "mp4", "at_seconds": float(t)}).action for t in range(10)]
    assert actions[-1] == "refuse" and breaker.events[-1].kind == "similar"


def test_a_declared_budget_bounds_the_positions_of_one_target():
    """Positions are pages: bounded per target at the declared budget, warned at the same share as the default."""
    breaker = _frames_breaker()
    decisions = [breaker.screen("frame_of_video", {"file_id": "mp4", "at_seconds": t / 2}) for t in range(61)]
    warns = [i for i, d in enumerate(decisions) if d.action == "warn"]
    refusals = [i for i, d in enumerate(decisions) if d.refused]
    assert warns == [35] and decisions[35].threshold == 36 and decisions[35].declared
    assert refusals == [59, 60] and decisions[59].kind == "page" and decisions[59].threshold == 60
    text = al._format_loop_breaker_refusal("frame_of_video", decisions[59])
    assert "this is position 60 of the same file_id (mp4)" in text and "Answer from the positions you have" in text
    # Another video is another target, with its own budget.
    assert breaker.screen("frame_of_video", {"file_id": "other", "at_seconds": 1.0}).action == "allow"


def test_a_structured_position_is_one_position_however_its_keys_are_ordered():
    breaker = _frames_breaker()
    one = {"file_id": "mp4", "at_seconds": 4.0, "region": {"box": [0.1, 0.2, 0.5, 0.6]}}
    for _ in range(2):
        breaker.screen("frame_of_video", one)
    same = {"region": {"box": [0.1, 0.2, 0.5, 0.6]}, "at_seconds": 4.0, "file_id": "mp4"}
    assert breaker.screen("frame_of_video", same).action == "warn" and breaker.events[-1].kind == "identical"
    assert al._loop_breaker_page(one, "frame_of_video", ("at_seconds", "region")) == al._loop_breaker_page(
        same, "frame_of_video", ("at_seconds", "region")
    )


def test_the_frame_tools_declare_their_positions():
    from app.services.builtin_tools.video_studio import build_video_studio_tools

    definitions, _executor = build_video_studio_tools(user_id="")
    declared = al._declared_positions(definitions)
    assert declared["save_video_frame"] == (("at_seconds", "at_step", "region"), 60)
