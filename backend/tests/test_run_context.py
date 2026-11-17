"""A run is told its own identity: the ``Run context`` line (services/run_context.py).

Covers the three run classes the line serves: an assignment run (a
platform_event listener), a chat turn's parent run (no assignment), and a
delegation (a background task, with its parent run and task id); plus the
wiring into the assignment prompt, the background task's system prompt and
the in-progress item of ``inspect_my_traces``.
"""

from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace

from app.services import assignment_engine, sub_agent
from app.services.builtin_tools.handover import format_handover_context_prompt
from app.services.builtin_tools.traces import _live_run_summary
from app.services.run_context import (
    RUN_CONTEXT_HEADER,
    build_run_context,
    format_run_context,
    run_trigger_type,
)


def _run(**fields):
    base = {
        "id": uuid.uuid4(),
        "assignment_id": None,
        "parent_run_id": None,
        "trigger_context": None,
        "trigger_payload": None,
    }
    base.update(fields)
    return SimpleNamespace(**base)


def test_platform_event_run_names_its_run_assignment_and_trigger():
    run = _run(
        assignment_id=uuid.uuid4(),
        trigger_context={"type": "platform_event"},
        trigger_payload={"trigger_type": "platform_event", "event": "run.completed"},
    )
    block = build_run_context(run)
    assert RUN_CONTEXT_HEADER in block
    assert (
        f"run_id: {run.id}, assignment_id: {run.assignment_id}, trigger: platform_event"
        in block
    )
    assert "parent_run_id" not in block
    assert "task_id" not in block


def test_chat_handover_parent_run_has_a_null_assignment():
    run = _run(trigger_context={"type": "chat_handover"})
    block = build_run_context(run)
    assert f"run_id: {run.id}, assignment_id: null, trigger: chat_handover" in block


def test_delegation_names_its_parent_run_and_task():
    run_id, parent, task = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    block = format_run_context(
        run_id=run_id, trigger_type="delegation", parent_run_id=parent, task_id=task
    )
    assert (
        f"run_id: {run_id}, assignment_id: null, trigger: delegation, "
        f"parent_run_id: {parent}, task_id: {task}"
    ) in block


def test_trigger_type_falls_back_to_the_payload():
    run = _run(trigger_payload={"trigger_type": "delegation"})
    assert run_trigger_type(run) == "delegation"
    assert run_trigger_type(_run()) is None
    assert "trigger: null" in build_run_context(_run())


def test_no_run_id_no_line():
    assert format_run_context(run_id=None) == ""
    assert format_run_context(run_id="  ") == ""
    assert build_run_context(_run(id=None)) == ""


def test_the_line_carries_identifiers_only():
    run = _run(
        trigger_context={"type": "webhook", "headers": {"authorization": "x"}},
        trigger_payload={"token": "should-not-appear"},
    )
    block = build_run_context(run)
    assert "should-not-appear" not in block
    assert "authorization" not in block


def test_background_task_prompt_carries_its_run_context():
    line = format_run_context(run_id="r1", trigger_type="delegation", parent_run_id="p1", task_id="t1")
    fragments = format_handover_context_prompt(
        {"type": "sub_agent_task", "original_question": "do it", "run_context": line}
    )
    assert line in fragments
    # A consult or a transfer never carries a task's run context.
    assert line not in format_handover_context_prompt({"type": "consult", "run_context": line})


def test_engine_and_task_wire_the_line():
    assert "rendered_prompt += build_run_context(run)" in inspect.getsource(assignment_engine)
    assert '"run_context": format_run_context(' in inspect.getsource(sub_agent)


def test_inspect_my_traces_in_progress_item_lists_run_id_beside_trace_id():
    run_id, assignment_id, trace_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    item = _live_run_summary(
        {
            "local_trace_id": trace_id,
            "local_assignment_run_id": run_id,
            "local_assignment_id": assignment_id,
            "local_spans": [],
        },
        include_spans=False,
    )
    assert item["id"] == str(trace_id)
    assert item["run_id"] == str(run_id)
    assert item["assignment_id"] == str(assignment_id)
    chat = _live_run_summary({"local_trace_id": trace_id, "local_spans": []}, include_spans=False)
    assert chat["run_id"] is None
    assert chat["assignment_id"] is None
