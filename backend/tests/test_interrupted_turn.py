"""What a chat turn cut by the graceful shutdown timeout leaves behind.

The progress ledger read from the agent loop's events (the calls started, the
calls that returned and so the calls still running), the reply the WebSocket
and REST turns store for it, and the note its memory records.
"""

from __future__ import annotations

from app.core.worker_shutdown import RECYCLE, STOP
from app.services.interrupted_turn import (
    MAX_LISTED_TOOLS,
    TRACE_TEXT_LIMIT,
    TurnProgress,
    cut_cause_text,
    interrupted_memory_note,
    interrupted_reply,
    tool_trace_entry,
)


def _start(*tools: str, iteration: int = 1) -> dict:
    return {"status": "tool_start", "tools": list(tools), "iteration": iteration}


def _done(tool: str, *, success: bool = True, output: str = "ok") -> dict:
    return {"status": "tool_done", "tool": tool, "success": success, "tool_input": "{}", "tool_output": output}


def _progress(*events: dict) -> TurnProgress:
    progress = TurnProgress()
    for event in events:
        progress.observe(event)
    return progress


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


def test_the_ledger_follows_the_calls_started_and_returned():
    progress = _progress(
        {"status": "thinking", "iteration": 1},
        _start("search", "read_page"),
        _done("search"),
        {"status": "thinking", "iteration": 2},
    )

    assert progress.started == ["search", "read_page"]
    assert [e["name"] for e in progress.trace] == ["search"]
    assert progress.running == ["read_page"]
    assert progress.iterations == 2


def test_a_name_called_twice_is_running_once_when_one_returned():
    progress = _progress(_start("search", "search", "fetch"), _done("search"), _done("fetch"))

    assert progress.running == ["search"]


def test_a_call_that_returns_without_a_start_is_completed_and_runs_nothing():
    """A pipeline step or a refused call can report a result the batch's start
    never named: it is work done, and leaves no phantom call running."""
    progress = _progress(_start("search"), _done("finalize"))

    assert [e["name"] for e in progress.trace] == ["finalize"]
    assert progress.running == ["search"]


def test_the_ledger_ignores_what_is_not_its_turns_own_tool_events():
    """A consulted agent's frames arrive wrapped: its calls are not this turn's."""
    progress = _progress(
        {"status": "consult_progress", "inner": _start("nested")},
        {"status": "thinking_text", "content": "pondering"},
        {"status": "tool_start", "tools": []},
        {"status": "tool_done"},
    )

    assert progress.started == [] and progress.trace == [] and progress.running == []


def test_reset_starts_over():
    progress = _progress({"status": "thinking", "iteration": 3}, _start("search"), _done("search"))
    progress.reset()

    assert (progress.started, progress.trace, progress.iterations) == ([], [], 0)
    assert progress.meta() == {}


def test_meta_is_the_shape_the_reasoning_details_panel_reads():
    progress = _progress(
        {"status": "thinking", "iteration": 2},
        _start("search", "search", "read_page"),
        _done("search", success=False, output="boom"),
    )

    assert progress.meta() == {
        "tool_names": ["search", "read_page"],
        "tool_call_count": 3,
        "iterations": 2,
        "tool_call_trace": [{"name": "search", "input": "{}", "output": "boom", "success": False}],
    }


def test_the_trace_entry_prefers_the_full_input_and_bounds_both_texts():
    big = "x" * (TRACE_TEXT_LIMIT + 5_000)
    entry = tool_trace_entry(
        {"status": "tool_done", "tool": "dump", "tool_input": "short", "tool_input_full": big, "tool_output": big}
    )

    assert entry["name"] == "dump" and entry["success"] is True
    assert entry["input"].startswith("x") and len(entry["input"]) < len(big)
    assert len(entry["output"]) < len(big)


# ---------------------------------------------------------------------------
# The reply
# ---------------------------------------------------------------------------


def test_a_turn_cut_before_any_tool_says_so():
    assert interrupted_reply(TurnProgress()) == (
        "This turn was interrupted before it could finish: the server process running it shut down.\n\n"
        "No tool call had completed."
    )


def test_the_reply_lists_the_completed_calls_their_failures_and_the_running_ones():
    progress = _progress(
        _start("search", "search", "read_page"),
        _done("search"),
        _done("search", success=False),
        _done("read_page"),
        _start("generate_report", iteration=2),
    )

    assert interrupted_reply(progress) == (
        "This turn was interrupted before it could finish: the server process running it shut down.\n\n"
        "Completed tool calls (3):\n"
        "- `search` (2 calls, 1 failed)\n"
        "- `read_page`\n\n"
        "Still running when it stopped (1):\n"
        "- `generate_report`\n\n"
        "Last completed action: `read_page`."
    )


def test_a_last_action_that_failed_is_named_as_failed():
    reply = interrupted_reply(_progress(_start("deploy"), _done("deploy", success=False)))

    assert "- `deploy` (failed)" in reply
    assert reply.endswith("Last completed action: `deploy`, which failed.")


def test_the_reply_sums_up_past_the_listed_tools():
    names = [f"tool_{i}" for i in range(MAX_LISTED_TOOLS + 3)]
    progress = _progress(_start(*names), *(_done(name) for name in names))

    reply = interrupted_reply(progress)

    assert f"- `tool_{MAX_LISTED_TOOLS - 1}`" in reply
    assert f"- `tool_{MAX_LISTED_TOOLS}`" not in reply
    assert "- and 3 other tools" in reply


def test_a_tool_name_cannot_break_out_of_its_code_span():
    reply = interrupted_reply(_progress(_start("we`ird"), _done("we`ird")))

    assert "`weird`" in reply


# ---------------------------------------------------------------------------
# The memory note
# ---------------------------------------------------------------------------


def test_a_turn_without_an_event_stream_claims_nothing_about_its_tools():
    assert interrupted_memory_note(None) == "Interrupted before the agent replied: the server process running the turn shut down."


def test_the_note_of_a_turn_cut_before_any_tool():
    assert interrupted_memory_note(TurnProgress()) == (
        "Interrupted before the agent replied: the server process running the turn shut down. No tool call had completed."
    )


def test_the_note_names_the_last_completed_action_and_what_was_still_running():
    progress = _progress(
        _start("search", "read_page"),
        _done("search", success=False),
        _done("read_page"),
        _start("generate_report", "generate_report", iteration=2),
    )

    assert interrupted_memory_note(progress) == (
        "Interrupted before the agent replied: the server process running the turn shut "
        "down. 2 tool calls completed (1 failed); last completed action: read_page. Still "
        "running: generate_report."
    )


def test_the_note_marks_a_last_action_that_failed():
    note = interrupted_memory_note(_progress(_start("deploy"), _done("deploy", success=False)))

    assert "1 tool call completed (1 failed); last completed action: deploy (failed)." in note


# ---------------------------------------------------------------------------
# What stopped the process
# ---------------------------------------------------------------------------


def test_a_recycle_is_named_as_a_recycle_not_a_restart():
    reply = interrupted_reply(TurnProgress(), RECYCLE)

    assert reply.startswith(
        "This turn was interrupted before it could finish: the web worker running it was "
        "recycled after serving its request quota (MAX_REQUESTS) and could not wait any "
        "longer for it.\n\n"
    )


def test_a_stop_signal_names_what_asks_a_pod_to_stop():
    reply = interrupted_reply(TurnProgress(), STOP)

    assert reply.startswith(
        "This turn was interrupted before it could finish: the server process running it "
        "was stopped (a deployment, a scale-in, a node drain or a restart).\n\n"
    )


def test_the_memory_note_names_the_cause_with_the_turn_as_its_subject():
    assert interrupted_memory_note(None, STOP) == (
        "Interrupted before the agent replied: the server process running the turn was "
        "stopped (a deployment, a scale-in, a node drain or a restart)."
    )
    assert "the web worker running the turn was recycled" in interrupted_memory_note(None, RECYCLE)


def test_an_unrecorded_cause_says_only_that_the_process_shut_down():
    assert cut_cause_text(None) == cut_cause_text("anything else") == (
        "the server process running it shut down"
    )
