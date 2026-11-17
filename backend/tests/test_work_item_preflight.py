"""A background task on a finished work item ends before its first model call.

A claim on a ``done`` / ``cancelled`` item answers the stored result,
but a spawned run reached that claim only after reading code for dozens of
iterations. The task body now reads the item its task text names in a
``work_item_id`` field and, when it is finished, completes the task at once
with that answer: no agent turn, nothing charged.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.work_item_preflight import (
    finished_item_text,
    finished_work_item_answer,
    held_by_another_run,
    held_item_text,
    named_work_item_id,
)
from tests.agentic_gate_stub import stub_agentic_gate
from tests.test_sub_agent_result_files import (
    _exec_db,
    _FakeSessionCM,
    _incremental_writer,
    _task_and_agent,
)

ITEM = "2f9cb40d-35c6-48bf-93c7-0e1842b42750"
OTHER = "6859b613-ca6b-4f1d-b6b1-96ebb7d71e0e"


@pytest.fixture(autouse=True)
def _agentic_ai_available(monkeypatch):
    stub_agentic_gate(monkeypatch)


def _finished(status: str = "done") -> dict:
    return {
        "id": ITEM,
        "title": "one run per work item",
        "status": status,
        "claimed_by_agent_id": "9c1142f6-d1e1-434a-9099-1c63e3286156",
        "origin_run_id": None,
        "updated_at": "2026-10-31T07:00:00+00:00",
        "result": "PR #42 merged",
    }


# -- which item a task names -------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        f"Work the item.\nwork_item_id: {ITEM}",
        f"work_item_id={ITEM.upper()}",
        f'Trigger payload: {{"trigger_type": "work_item", "work_item_id": "{ITEM}"}}',
        f"work_item_id: `{ITEM}` (claimed for you)",
    ],
)
def test_the_explicit_field_names_the_item(text):
    assert named_work_item_id(text) == ITEM


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        f"See work item {ITEM} for context.",  # prose, not the field
        f"work_item_id: {ITEM}\nwork_item_id: {OTHER}",  # two items: none
        "work_item_id: not-a-uuid",
    ],
)
def test_no_single_explicit_field_names_nothing(text):
    assert named_work_item_id(text) is None


def test_the_same_id_twice_is_one_item():
    assert named_work_item_id(f"work_item_id: {ITEM}\nwork_item_id={ITEM}") == ITEM


# -- the answer ----------------------------------------------------------------


def test_the_answer_says_when_by_whom_and_the_result():
    text = finished_item_text(_finished())
    assert "is already done (at 2026-10-31T07:00:00+00:00" in text
    assert "report the stored result below and stop" in text
    assert text.endswith("Result:\nPR #42 merged")


async def test_a_finished_item_gives_the_answer():
    get = AsyncMock(return_value=_finished("cancelled"))
    with patch("app.services.work_item.get_work_item", new=get):
        answer = await finished_work_item_answer(f"work_item_id: {ITEM}", user_id="u-1")
    assert answer is not None
    assert answer.startswith("Ended before its first model call")
    assert "is already cancelled" in answer
    get.assert_awaited_once_with(ITEM, user_id="u-1")


@pytest.mark.parametrize("item", [None, {**_finished(), "status": "claimed"}, {**_finished(), "status": "open"}])
async def test_an_open_claimed_or_invisible_item_lets_the_task_run(item):
    with patch("app.services.work_item.get_work_item", new=AsyncMock(return_value=item)):
        assert await finished_work_item_answer(f"work_item_id: {ITEM}", user_id="u-1") is None


async def test_a_task_naming_no_item_reads_nothing():
    get = AsyncMock()
    with patch("app.services.work_item.get_work_item", new=get):
        assert await finished_work_item_answer("triage the open alerts", user_id="u-1") is None
    get.assert_not_awaited()


# -- a platform event watch's task -------------------------------------


def _completed_watch_text(brief: str = "Record the completion in shared state.") -> str:
    """The text a watch on ``work_item.completed`` gives its task, as the watch writes it."""
    from types import SimpleNamespace

    from app.services.platform_event_task_watches import task_text

    watch = SimpleNamespace(id=uuid.uuid4(), task=brief)
    payload = {
        "event_id": "evt-1",
        "object_type": "work_item",
        "object_id": ITEM,
        "occurred_at": "2026-11-02T14:49:00+00:00",
        "work_item_id": ITEM,
        "status": "done",
        "correlation_key": "retest:task-watch-slice3",
    }
    return task_text(watch, "work_item.completed", payload)


def test_a_field_inside_a_quarantine_block_names_no_item():
    text = _completed_watch_text()
    assert f'"work_item_id": "{ITEM}"' in text, "the event's id stays in the quarantined payload"
    assert named_work_item_id(text) is None


def test_the_brief_s_own_field_still_names_the_item_beside_an_event():
    assert named_work_item_id(_completed_watch_text(f"Work it.\nwork_item_id: {OTHER}")) == OTHER


async def test_a_completed_event_watch_task_is_not_stopped_by_the_finished_item():
    get = AsyncMock(return_value=_finished())
    with patch("app.services.work_item.get_work_item", new=get):
        assert await finished_work_item_answer(_completed_watch_text(), user_id="u-1") is None
    get.assert_not_awaited()


async def test_a_brief_naming_a_finished_item_still_ends_beside_an_event():
    get = AsyncMock(return_value=_finished())
    with patch("app.services.work_item.get_work_item", new=get):
        answer = await finished_work_item_answer(
            _completed_watch_text(f"Work the ONE work item.\nwork_item_id: {ITEM}"), user_id="u-1"
        )
    assert answer is not None and answer.startswith("Ended before its first model call")


def test_the_claim_answer_is_the_same_text():
    from app.services.builtin_tools.work_items import _already_finished_text

    assert _already_finished_text(_finished()) == finished_item_text(_finished())


# -- the task body -----------------------------------------------------------


def _db(task) -> MagicMock:
    res = MagicMock()
    res.scalar_one_or_none = MagicMock(return_value=task)
    db = MagicMock()
    db.execute = AsyncMock(return_value=res)
    db.commit = AsyncMock()
    return db


async def test_a_task_on_a_finished_item_completes_with_no_model_call():
    from app.services.sub_agent import _execute_sub_agent_task

    task, _agent = _task_and_agent()
    task.status = "pending"
    chat = AsyncMock(return_value=("never", None))
    delegation = AsyncMock()
    task_id = str(uuid.uuid4())
    with (
        patch("app.core.database.async_session", lambda: _FakeSessionCM(_db(task))),
        patch("app.services.agent_chat.run_agent_chat", new=chat),
        patch("app.services.sub_agent._update_task_delegation", new=delegation),
        patch("app.services.sub_agent.publish_task_event", new=AsyncMock()),
        patch("app.services.sub_agent._target_agent_name", new=AsyncMock(return_value="Developer")),
        patch("app.services.work_item.get_work_item", new=AsyncMock(return_value=_finished())),
    ):
        outcome = await _execute_sub_agent_task(
            task_id=task_id,
            user_id=str(uuid.uuid4()),
            target_agent_id=str(uuid.uuid4()),
            task_description=f"Work the ONE work item.\nwork_item_id: {ITEM}",
        )

    chat.assert_not_awaited()
    assert outcome.status == "completed"
    assert task.status == "completed"
    assert "is already done" in task.result
    assert task.result.endswith("PR #42 merged")
    delegation.assert_awaited_once()
    assert delegation.await_args.args[1:] == (task_id, "completed")


async def test_a_task_on_an_open_item_runs_its_turn():
    from app.services.sub_agent import _execute_sub_agent_task

    task, agent = _task_and_agent()
    chat = AsyncMock(return_value=("done", None))
    with (
        patch("app.core.database.async_session", lambda: _FakeSessionCM(_exec_db(task, agent))),
        patch("app.services.agent_chat.run_agent_chat", new=chat),
        patch("app.services.sub_agent._update_task_delegation", new=AsyncMock()),
        patch("app.services.sub_agent_files.write_task_result_file_ids", new=_incremental_writer()),
        patch(
            "app.services.work_item.get_work_item",
            new=AsyncMock(return_value={**_finished(), "status": "open"}),
        ),
    ):
        outcome = await _execute_sub_agent_task(
            task_id=str(uuid.uuid4()),
            user_id=str(uuid.uuid4()),
            target_agent_id=str(uuid.uuid4()),
            task_description=f"work_item_id: {ITEM}",
        )

    chat.assert_awaited_once()
    assert outcome.status == "completed"


# -- an item another live run holds ------------------------------------

INBOX_RUN = "9835b177-9e24-4d2e-aaa0-8a90de027377"


def _held(*, alive: bool = True, run_id: str = INBOX_RUN, status: str = "in_progress") -> dict:
    return {
        "id": ITEM,
        "title": "one run per work item",
        "status": status,
        "claimed_by_agent_id": "9c1142f6-d1e1-434a-9099-1c63e3286156",
        "claimed_at": "2026-10-31T14:24:19+00:00",
        "claimed_by_run_id": run_id,
        "claimed_by_run": {
            "id": run_id,
            "kind": "run",
            "status": "running" if alive else "completed",
            "alive": alive,
        },
        "result": None,
    }


def test_the_held_answer_names_the_run_and_since_when():
    text = held_item_text(_held())
    assert f"is in_progress since 2026-10-31T14:24:19+00:00 by run `{INBOX_RUN}`" in text
    assert "which is still running" in text
    assert "Stop unless your task explicitly says to take it over" in text


@pytest.mark.parametrize(
    ("item", "own", "held"),
    [
        (_held(), (), True),
        (_held(), (INBOX_RUN.upper(),), False),  # the holder itself passes
        (_held(alive=False), (), False),  # an ended holder: take-over is not this check's
        (_held(status="open"), (), False),
        ({**_held(), "claimed_by_run": None}, (), False),  # a claim with no run
    ],
)
def test_held_by_another_run(item, own, held):
    assert held_by_another_run(item, own) is held


async def test_an_item_held_by_another_live_run_gives_the_answer():
    with patch("app.services.work_item.get_work_item", new=AsyncMock(return_value=_held())):
        answer = await finished_work_item_answer(f"work_item_id: {ITEM}", user_id="u-1")
    assert answer is not None
    assert answer.startswith("Ended before its first model call: another live run holds")
    assert INBOX_RUN in answer


async def test_the_holding_run_and_an_ended_holder_let_the_task_run():
    with patch("app.services.work_item.get_work_item", new=AsyncMock(return_value=_held())):
        assert (
            await finished_work_item_answer(
                f"work_item_id: {ITEM}", user_id="u-1", own_run_ids=(None, INBOX_RUN)
            )
            is None
        )
    with patch(
        "app.services.work_item.get_work_item", new=AsyncMock(return_value=_held(alive=False))
    ):
        assert await finished_work_item_answer(f"work_item_id: {ITEM}", user_id="u-1") is None


async def test_a_spawned_task_on_an_item_the_inbox_run_holds_makes_no_model_call():
    """One desk, one item: the inbox run claimed it, the spawned task stops before its turn."""
    from app.services.sub_agent import _execute_sub_agent_task

    task, _agent = _task_and_agent()
    task.status = "pending"
    task.metadata_json = {}
    chat = AsyncMock(return_value=("never", None))
    delegation = AsyncMock()
    task_id = str(uuid.uuid4())
    with (
        patch("app.core.database.async_session", lambda: _FakeSessionCM(_db(task))),
        patch("app.services.agent_chat.run_agent_chat", new=chat),
        patch("app.services.sub_agent._update_task_delegation", new=delegation),
        patch("app.services.sub_agent.publish_task_event", new=AsyncMock()),
        patch("app.services.sub_agent._target_agent_name", new=AsyncMock(return_value="Developer")),
        patch("app.services.work_item.get_work_item", new=AsyncMock(return_value=_held())),
    ):
        outcome = await _execute_sub_agent_task(
            task_id=task_id,
            user_id=str(uuid.uuid4()),
            target_agent_id=str(uuid.uuid4()),
            task_description=f"Work the ONE work item.\nwork_item_id: {ITEM}",
        )

    chat.assert_not_awaited()
    assert outcome.status == "completed"
    assert task.status == "completed"
    assert f"by run `{INBOX_RUN}`" in task.result


async def test_a_task_whose_parent_run_holds_the_item_runs_its_turn():
    """The inbox run that claimed the item spawned this task: it is the task's own run."""
    from app.services.sub_agent import _execute_sub_agent_task

    task, agent = _task_and_agent()
    parent_run = str(uuid.uuid4())
    task.metadata_json = {**(task.metadata_json or {}), "parent_run_id": parent_run}
    chat = AsyncMock(return_value=("done", None))
    with (
        patch("app.core.database.async_session", lambda: _FakeSessionCM(_exec_db(task, agent))),
        patch("app.services.agent_chat.run_agent_chat", new=chat),
        patch("app.services.sub_agent._update_task_delegation", new=AsyncMock()),
        patch("app.services.sub_agent_files.write_task_result_file_ids", new=_incremental_writer()),
        patch(
            "app.services.work_item.get_work_item",
            new=AsyncMock(return_value=_held(run_id=parent_run)),
        ),
    ):
        outcome = await _execute_sub_agent_task(
            task_id=str(uuid.uuid4()),
            user_id=str(uuid.uuid4()),
            target_agent_id=str(uuid.uuid4()),
            task_description=f"work_item_id: {ITEM}",
        )

    chat.assert_awaited_once()
    assert outcome.status == "completed"


async def test_a_work_item_completed_watch_task_reaches_its_model_call():
    """The event names a finished item in its quarantine block; the task still runs its turn."""
    from app.services.sub_agent import _execute_sub_agent_task

    task, agent = _task_and_agent()
    task.metadata_json = {**(task.metadata_json or {}), "origin": "platform_event_watch"}
    chat = AsyncMock(return_value=("recorded", None))
    text = _completed_watch_text()
    with (
        patch("app.core.database.async_session", lambda: _FakeSessionCM(_exec_db(task, agent))),
        patch("app.services.agent_chat.run_agent_chat", new=chat),
        patch("app.services.sub_agent._update_task_delegation", new=AsyncMock()),
        patch("app.services.sub_agent_files.write_task_result_file_ids", new=_incremental_writer()),
        patch("app.services.work_item.get_work_item", new=AsyncMock(return_value=_finished())),
    ):
        outcome = await _execute_sub_agent_task(
            task_id=str(uuid.uuid4()),
            user_id=str(uuid.uuid4()),
            target_agent_id=str(uuid.uuid4()),
            task_description=text,
        )

    chat.assert_awaited_once()
    assert "<<UNTRUSTED-" in str(chat.await_args), "the event reaches the model in its quarantine block"
    assert outcome.status == "completed"


def test_the_tool_texts_say_held_by_another_run():
    from app.services.builtin_tools.work_items import _full_item_text

    item = {**_held(), "priority": "high"}
    assert "IN PROGRESS BY ANOTHER RUN" in _full_item_text(item, [], 0, False, ("other",))
    assert "IN PROGRESS BY ANOTHER RUN" not in _full_item_text(item, [], 0, False, (INBOX_RUN,))
