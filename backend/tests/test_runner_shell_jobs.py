"""Background shell jobs on a runner.

``run_shell`` with ``background: true`` is answered at once by the runner with
the job it started; the ``shell_jobs`` tool lists, tails and stops jobs
through the ``shell_job`` command kind, gated by the ``run_shell`` capability.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.models.runner import COMMAND_KIND_CAPABILITY
from app.services import runner_activity
from app.services import runners as runners_service
from app.services.builtin_tools import computer_use
from tests.test_runner_activity import _call, _runner, _wire


def _job_result(kind: str, result: dict) -> dict:
    return {
        "id": "cmd-9",
        "status": "completed",
        "result": result,
        "error": None,
        "result_file_id": None,
        "kind": kind,
    }


def test_shell_job_is_gated_by_the_run_shell_capability():
    assert COMMAND_KIND_CAPABILITY["shell_job"] == "run_shell"


def test_a_background_run_shell_sends_the_flag_and_drops_the_timeout(monkeypatch):
    owner = uuid.uuid4()
    laptop = _runner(owner, name="SAMUEL-LAPTOP")
    started = {
        "job_id": "cmd-9",
        "pid": 4242,
        "status": "running",
        "background": True,
        "first_output": "Downloading...\n",
    }
    _wire(monkeypatch, [laptop], result=_job_result("run_shell", started))
    sent: dict = {}

    async def _dispatch(**kwargs):
        sent.update(kwargs)
        return "cmd-9", "pending"

    monkeypatch.setattr(computer_use, "dispatch_command", _dispatch)
    result, frames = _call(owner, laptop, "run_shell", command="curl -O big.iso", timeout=30, background=True)
    assert sent["payload"] == {"command": "curl -O big.iso", "background": True}
    assert not result.is_error
    assert "job cmd-9" in result.content and "pid 4242" in result.content
    assert "shell_jobs" in result.content and "Downloading..." in result.content
    card = frames[-1]["command"]
    assert card["status"] == "completed"
    assert card["output"]["background"] is True
    assert card["output"]["job_id"] == "cmd-9"
    assert card["output"]["job_status"] == "running"
    assert card["output"]["stdout"] == "Downloading...\n"


def test_shell_jobs_tail_sends_the_offset_and_returns_the_window(monkeypatch):
    owner = uuid.uuid4()
    laptop = _runner(owner)
    window = {"job_id": "job-1", "status": "exited", "exit_code": 0, "offset": 10, "next_offset": 14, "output": "done"}
    _wire(monkeypatch, [laptop], result=_job_result("shell_job", window))
    sent: dict = {}

    async def _dispatch(**kwargs):
        sent.update(kwargs)
        return "cmd-9", "pending"

    monkeypatch.setattr(computer_use, "dispatch_command", _dispatch)
    result, _ = _call(owner, laptop, "shell_jobs", action="tail", job_id="job-1", offset=10)
    assert sent["kind"] == "shell_job"
    assert sent["payload"] == {"action": "tail", "job_id": "job-1", "offset": 10}
    assert '"next_offset": 14' in result.content and '"output": "done"' in result.content


def test_shell_jobs_refuses_an_unknown_action_or_a_missing_job_id(monkeypatch):
    owner = uuid.uuid4()
    laptop = _runner(owner)
    _wire(monkeypatch, [laptop], result=None)
    result, _ = _call(owner, laptop, "shell_jobs", action="kill", job_id="x")
    assert "Unknown action 'kill'" in result.content
    result, _ = _call(owner, laptop, "shell_jobs", action="stop")
    assert "needs the job_id" in result.content


def test_the_tools_offer_background_and_shell_jobs():
    defs, _ = computer_use.build_computer_use_tools(
        user_id=str(uuid.uuid4()),
        is_admin=False,
        user_group_ids=[],
        agent_runner_ids=None,
        agent_runner_labels=None,
    )
    by_name = {d.name: d for d in defs}
    assert "background" in by_name["run_shell"].input_schema["properties"]
    assert by_name["shell_jobs"].input_schema["properties"]["action"]["enum"] == ["list", "tail", "stop"]


# ── GET /runners/{id}/jobs (the Machines page) ─────────────────────────────────────


def _jobs_route(monkeypatch, *, manages=True, online=True, refusal=None, dispatched=("cmd-1", "pending"), result=None):
    from app.api.v1 import runners as runners_api
    from app.services import runner_transport

    runner = SimpleNamespace(id=uuid.uuid4(), name="desk")
    monkeypatch.setattr(runners_api, "_require_enabled", lambda: None)
    monkeypatch.setattr(runners_api, "_get_visible_runner", AsyncMock(return_value=(runner, [])))
    monkeypatch.setattr(runners_api, "can_manage", lambda *_a, **_k: manages)
    monkeypatch.setattr(runners_api, "is_runner_online", lambda _r: online)
    monkeypatch.setattr(runners_api, "authorize_command", lambda *_a: refusal)
    monkeypatch.setattr(runners_api, "release_db_connection_before_loop", AsyncMock())
    dispatch = AsyncMock(return_value=dispatched)
    monkeypatch.setattr(runner_transport, "dispatch_command", dispatch)
    monkeypatch.setattr(runner_transport, "await_result", AsyncMock(return_value=result))
    give_up = AsyncMock()
    monkeypatch.setattr(runner_transport, "give_up_on_command", give_up)
    return runners_api, dispatch, give_up


def _list_jobs(api):
    return asyncio.run(api.list_runner_jobs("r-1", SimpleNamespace(id=uuid.uuid4()), AsyncMock()))


def test_the_machines_page_lists_the_jobs_the_runner_answers(monkeypatch):
    job = {"job_id": "cmd-7", "command": "sleep 600", "status": "running", "pid": 12}
    api, dispatch, _ = _jobs_route(
        monkeypatch, result={"status": "completed", "result": {"jobs": [job, "junk"]}}
    )
    answer = _list_jobs(api)
    assert answer == {"available": True, "reason": None, "jobs": [job]}
    assert dispatch.await_args.kwargs["kind"] == "shell_job"
    assert dispatch.await_args.kwargs["payload"] == {"action": "list"}


def test_only_a_manager_lists_a_machines_jobs(monkeypatch):
    api, dispatch, _ = _jobs_route(monkeypatch, manages=False)
    with pytest.raises(HTTPException) as caught:
        _list_jobs(api)
    assert caught.value.status_code == 403
    dispatch.assert_not_awaited()


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"online": False}, "offline"),
        ({"refusal": "The 'run_shell' capability is not enabled"}, "not_allowed"),
        ({"dispatched": ("", "busy")}, "busy"),
    ],
)
def test_a_machine_that_cannot_answer_is_unavailable_not_an_error(monkeypatch, kwargs, reason):
    api, _, _ = _jobs_route(monkeypatch, **kwargs)
    assert _list_jobs(api) == {"available": False, "reason": reason, "jobs": []}


def test_no_answer_in_time_gives_the_list_command_up(monkeypatch):
    api, _, give_up = _jobs_route(monkeypatch, result={"status": "dispatched", "result": None})
    assert _list_jobs(api)["reason"] == "no_answer"
    give_up.assert_awaited_once()


@pytest.mark.parametrize(
    ("version", "refused"),
    [
        ("1.261102.10390", True),
        ("1.261103.10466", True),
        ("1.261103.10467", False),
        ("1.261104.10500", False),
        (None, False),
        ("0.0.0-dev", False),
    ],
)
def test_background_jobs_need_a_runner_that_carries_them(version, refused):
    lacks = runners_service.runner_lacks_shell_jobs
    assert lacks("run_shell", {"command": "x", "background": True}, version) is refused
    assert lacks("shell_job", {"action": "list"}, version) is refused
    assert lacks("run_shell", {"command": "x"}, version) is False


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("run_shell", {"command": "sleep 60", "background": True}),
        ("shell_jobs", {"action": "list"}),
    ],
)
def test_an_older_runner_is_refused_before_dispatch(monkeypatch, tool, arguments):
    owner = uuid.uuid4()
    laptop = _runner(owner, name="DESKTOP-OLD", agent_version="1.261102.10390")
    _wire(monkeypatch, [laptop], result=None)
    dispatch = AsyncMock(return_value=("cmd-9", "pending"))
    monkeypatch.setattr(computer_use, "dispatch_command", dispatch)
    result, frames = _call(owner, laptop, tool, **arguments)
    dispatch.assert_not_awaited()
    assert "'DESKTOP-OLD' does not support background jobs" in result.content
    assert "1.261102.10390" in result.content
    assert f">= {runners_service.SHELL_JOBS_MIN_RUNNER_VERSION}" in result.content
    assert "Check for updates" in result.content
    assert ("without background" in result.content) is (tool == "run_shell")
    assert frames[-1]["command"]["status"] == runner_activity.STATUS_REFUSED


def test_a_foreground_run_shell_still_reaches_an_older_runner(monkeypatch):
    owner = uuid.uuid4()
    laptop = _runner(owner, agent_version="1.261102.10390")
    done = {"exit_code": 0, "stdout": "ok", "stderr": ""}
    _wire(monkeypatch, [laptop], result=_job_result("run_shell", done))
    result, _ = _call(owner, laptop, "run_shell", command="echo ok")
    assert "exit code: 0" in result.content


def test_a_foreground_card_is_unchanged():
    activity = runner_activity.RunnerActivity("run_shell", {"command": "ls"})
    activity.settle(runner_activity.STATUS_COMPLETED, result={"exit_code": 0, "stdout": "a", "stderr": ""})
    output = activity.record()["output"]
    assert "background" not in output and output["exit_code"] == 0
