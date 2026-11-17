"""Machine activity - the display record of a ``computer_use`` command.

Pure tests (no database, no Redis) over the three halves of the feature:

* the record - what a card shows for each kind of command, bounded to the tool
  event cap however large the output, and never carrying typed text;
* the funnel - ``computer_use._run_command`` reports the card on EVERY path
  (ran, failed, unavailable, refused, busy, held, crashed) without changing the
  prose the model reads;
* the chat - the per-turn fold the WebSocket persists with the message, and the
  API serialisation of what was persisted.

Plus the tripwires that keep the backend and the frontend on one vocabulary.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models.runner import Runner
from app.services import runner_activity
from app.services.builtin_tools import computer_use
from app.services.builtin_tools.computer_use import build_computer_use_tools
from app.services.runner_activity import (
    LIVE_STATUSES,
    MAX_CONSULT_DEPTH,
    MAX_RUNNER_ACTIVITY_RECORDS,
    RECORD_CHAR_BUDGET,
    RECORD_STATUSES,
    RUNNER_ACTIVITY_META_KEY,
    STATUS_COMPLETED,
    STATUS_INTERRUPTED,
    STATUS_RUNNING,
    RunnerActivity,
    RunnerActivityLog,
    encoded_size,
    head,
    head_tail,
    sanitize_activity_list,
    sanitize_record,
)
from app.tools.base import ToolCall
from app.tools.tool_events import (
    MAX_TOOL_EVENT_PAYLOAD_CHARS,
    RUNNER_COMMAND_FINISHED_EVENT,
    RUNNER_COMMAND_STARTED_EVENT,
    TOOL_PROGRESS_STATUS,
    bind_tool_event_sink,
    reset_tool_event_sink,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = BACKEND_ROOT.parent / "frontend"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _runner(owner: uuid.UUID | None, *, name: str = "laptop", online: bool = True, **columns) -> Runner:
    seen = datetime.now(UTC) - (timedelta(seconds=5) if online else timedelta(hours=2))
    return Runner(
        id=uuid.uuid4(),
        user_id=owner,
        name=name,
        platform="windows",
        key_prefix="run-xxxxxxxx",
        key_hash="hash",
        permission_mode="restricted",
        capabilities=["run_shell", "file_ops"],
        labels=[],
        is_active=True,
        status="online" if online else "offline",
        last_seen_at=seen,
        **columns,
    )


@contextlib.contextmanager
def _capture():
    frames: list[dict] = []

    async def _sink(frame):
        frames.append(frame)

    binding = bind_tool_event_sink(_sink, tool_name="run_shell")
    try:
        yield frames
    finally:
        reset_tool_event_sink(binding)


def _report(activity: RunnerActivity) -> list[dict]:
    with _capture() as frames:
        asyncio.run(activity.report())
    return frames


def _completed(kind: str, payload: dict, result: dict, *, file_id: str | None = None) -> dict:
    activity = RunnerActivity(kind, payload, command_id="cmd-1")
    activity.mark_running()
    activity.settle(STATUS_COMPLETED, result=result, file_id=file_id)
    return activity.record()


# ── Previews ─────────────────────────────────────────────────────────────────


def test_head_tail_keeps_both_ends_and_weights_the_tail():
    text = "".join(f"line {i}\n" for i in range(1000))
    preview, cut = head_tail(text, 400)
    assert cut is True
    assert len(preview) <= 400
    assert preview.startswith("line 0\n")
    assert preview.endswith("line 999\n")
    before, after = preview.split("\n...\n", 1)
    # The end of an output is where its errors are: the tail gets more room.
    assert len(after) > len(before)


def test_previews_leave_short_text_untouched():
    assert head_tail("ok\n", 400) == ("ok\n", False)
    assert head("abc", 10) == ("abc", False)
    assert head("abcdef", 3) == ("abc", True)
    assert head_tail("abcdef", 2) == ("", True)
    assert head_tail("", 0) == ("", False)


# ── The record, per kind ─────────────────────────────────────────────────────


def test_a_shell_command_carries_its_line_its_directory_and_its_streams():
    record = _completed(
        "run_shell",
        {"command": "Get-PSDrive -PSProvider FileSystem", "cwd": "C:\\repo", "timeout": 30},
        {"exit_code": 0, "stdout": "Name  Used (GB)\nC     120.4\n", "stderr": ""},
    )
    assert record["kind"] == "run_shell"
    assert record["status"] == STATUS_COMPLETED
    assert record["input"] == {"command": "Get-PSDrive -PSProvider FileSystem", "cwd": "C:\\repo"}
    assert record["output"]["exit_code"] == 0
    assert "C     120.4" in record["output"]["stdout"]
    assert record["output"]["stdout_truncated"] is False
    assert isinstance(record["duration_ms"], int) and record["duration_ms"] >= 0


def test_a_failing_exit_code_is_still_a_completed_command():
    record = _completed("run_shell", {"command": "git status"}, {"exit_code": 128, "stdout": "", "stderr": "fatal"})
    assert record["status"] == STATUS_COMPLETED
    assert record["output"]["exit_code"] == 128
    assert record["output"]["stderr"] == "fatal"


def test_a_file_read_inline_or_binary():
    inline = _completed("read_file", {"path": "C:\\notes.txt"}, {"path": "C:\\notes.txt", "content": "hello", "size": 5})
    assert inline["input"] == {"path": "C:\\notes.txt"}
    assert inline["output"] == {"content": "hello", "content_truncated": False, "size": 5}
    binary = _completed(
        "read_file",
        {"path": "/home/ops/a.bin"},
        {"path": "/home/ops/a.bin", "size": 9000, "encoding": "binary"},
        file_id="f-1",
    )
    assert binary["output"] == {"binary": True, "size": 9000, "file_id": "f-1"}


def test_a_file_write_previews_what_was_written_and_counts_bytes():
    record = _completed(
        "write_file",
        {"path": "~/report.md", "content": "# R\u00e9sum\u00e9\n"},
        {"path": "~/report.md", "bytes_written": 12},
    )
    assert record["input"]["path"] == "~/report.md"
    assert record["input"]["content"] == "# R\u00e9sum\u00e9\n"
    assert record["input"]["content_bytes"] == len("# R\u00e9sum\u00e9\n".encode())
    assert record["output"] == {"bytes_written": 12}


def test_a_listing_shows_folders_first_then_names():
    record = _completed(
        "list_dir",
        {"path": "C:\\Users\\me"},
        {
            "path": "C:\\Users\\me",
            "entries": [
                {"name": "zeta.txt", "is_dir": False, "size": 3},
                {"name": "Downloads", "is_dir": True, "size": 0},
                {"name": "alpha.txt", "is_dir": False, "size": 7},
                {"name": "Desktop", "is_dir": True, "size": 4096},
                {"bogus": True},
            ],
        },
    )
    names = [e["name"] for e in record["output"]["entries"]]
    assert names == ["Desktop", "Downloads", "alpha.txt", "zeta.txt"]
    assert record["output"]["entry_count"] == 4
    # A folder's "size" is the file system's bookkeeping, not a size.
    assert "size" not in record["output"]["entries"][0]
    assert record["output"]["entries"][2]["size"] == 7


def test_a_capture_points_at_its_file():
    record = _completed(
        "screenshot",
        {"display": "primary"},
        {"width": 1920, "height": 1080, "sha256": "x" * 64},
        file_id="shot-1",
    )
    assert record["input"] == {"display": "primary"}
    assert record["output"] == {"file_id": "shot-1", "width": 1920, "height": 1080}


@pytest.mark.parametrize("kind", ["computer_action", "browser_control"])
def test_typed_text_is_carried_as_a_length_never_as_text(kind):
    secret = "hunter2-correct-horse"
    activity = RunnerActivity(kind, {"action": "type", "text": secret, "selector": "#password"})
    record = activity.record()
    assert record["input"]["text_length"] == len(secret)
    assert secret not in json.dumps(record)


def test_browser_navigation_and_input_coordinates():
    nav = _completed(
        "browser_control",
        {"action": "navigate", "url": "https://example.com/a"},
        {"action": "navigate", "url": "https://example.com/b", "ok": True},
    )
    assert nav["input"] == {"action": "navigate", "url": "https://example.com/a"}
    assert nav["output"] == {"url": "https://example.com/b"}
    click = RunnerActivity("computer_action", {"action": "click", "x": 120, "y": 480, "button": "left"}).record()
    assert click["input"] == {"action": "click", "button": "left", "x": 120, "y": 480}


def test_a_browser_tab_switch_names_the_tab_and_where_it_landed():
    switch = _completed(
        "browser_control",
        {"action": "select_tab", "tab_id": "TAB-2"},
        {"action": "select_tab", "tab_id": "TAB-2", "title": "Signed in", "url": "https://app.example/home"},
    )
    assert switch["input"] == {"action": "select_tab", "tab_id": "TAB-2"}
    assert switch["output"] == {"url": "https://app.example/home", "title": "Signed in"}


# ── Bounds ───────────────────────────────────────────────────────────────────


def test_the_budget_leaves_room_under_the_tool_event_cap():
    assert RECORD_CHAR_BUDGET < MAX_TOOL_EVENT_PAYLOAD_CHARS


@pytest.mark.parametrize(
    ("kind", "payload", "result"),
    [
        ("run_shell", {"command": "yes"}, {"exit_code": 0, "stdout": "y\n" * 500_000, "stderr": "e" * 200_000}),
        # Non-ASCII output is escaped six characters per glyph by the encoder.
        ("run_shell", {"command": "tree"}, {"exit_code": 0, "stdout": "\u2502\u251c\u2500\u2500 \u65e5\u672c\n" * 50_000}),
        ("run_shell", {"command": "x" * 50_000, "cwd": "C:\\" + "d" * 5_000}, {"exit_code": 1, "stdout": "", "stderr": ""}),
        ("read_file", {"path": "C:\\big.log"}, {"content": "\"quoted\\path\"\n" * 40_000, "size": 600_000}),
        ("write_file", {"path": "/home/ops/out.csv", "content": "a,b,c\n" * 200_000}, {"bytes_written": 1_200_000}),
        (
            "list_dir",
            {"path": "/var/log"},
            {"entries": [{"name": f"file-{i:05d}-\u00e9\u00e8.log", "is_dir": False, "size": i} for i in range(20_000)]},
        ),
    ],
)
def test_a_huge_command_still_fits_the_frame_and_is_delivered(kind, payload, result):
    activity = RunnerActivity(kind, payload, command_id="cmd-1")
    activity.mark_running()
    activity.settle(STATUS_COMPLETED, result=result)
    record = activity.record()
    assert encoded_size(record) <= RECORD_CHAR_BUDGET
    frames = _report(activity)
    # Not dropped by the channel's own cap: the card is on screen.
    assert len(frames) == 1 and frames[0]["command"]["command_id"] == "cmd-1"


def test_a_cut_preview_says_so():
    activity = RunnerActivity("run_shell", {"command": "yes"})
    activity.mark_running()
    activity.settle(STATUS_COMPLETED, result={"exit_code": 0, "stdout": "y\n" * 500_000, "stderr": ""})
    output = activity.record()["output"]
    assert output["stdout_truncated"] is True
    assert output["stdout"]  # a preview, not nothing


# ── Lifecycle ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("command", "status"),
    [
        ({"status": "completed", "result": {"exit_code": 0}}, "completed"),
        ({"status": "failed", "error": "boom"}, "failed"),
        ({"status": "timeout", "error": "exceeded"}, "timeout"),
        ({"status": "cancelled", "error": "cancelled by an admin"}, "interrupted"),
        (
            {
                "status": "failed",
                "error": "command stopped by the user",
                "result": {"exit_code": None, "stopped": "user", "stdout": "started", "stderr": ""},
            },
            "stopped",
        ),
        ({"status": "dispatched"}, "timeout"),
        (None, "failed"),
    ],
)
def test_what_the_runner_answered_maps_onto_the_card(command, status):
    activity = RunnerActivity("run_shell", {"command": "ls"})
    activity.mark_running()
    activity.settle_from_result(command)
    record = activity.record()
    assert record["status"] == status
    if status != "completed":
        assert record["error"]


def test_a_stopped_card_names_who_stopped_it():
    # The card says who stopped the command, not "you" to every viewer.
    activity = RunnerActivity("run_shell", {"command": "sleep 60"})
    activity.mark_running()
    activity.settle_from_result(
        {
            "status": "failed",
            "error": "command stopped by the user",
            "result": {"exit_code": None, "stopped": "user", "stopped_by": {"user_id": "u-1", "name": "Ada"}},
        }
    )
    assert activity.record()["stopped_by"] == {"user_id": "u-1", "name": "Ada"}


@pytest.mark.parametrize(
    "result",
    [
        {"exit_code": None, "stopped": "user"},
        {"exit_code": None, "stopped": "user", "stopped_by": "Ada"},
        {"exit_code": None, "stopped": "user", "stopped_by": {"user_id": "", "name": "Ada"}},
    ],
)
def test_a_stopped_card_without_a_valid_stopper_names_nobody(result):
    activity = RunnerActivity("run_shell", {"command": "sleep 60"})
    activity.mark_running()
    activity.settle_from_result({"status": "failed", "error": "stopped", "result": result})
    assert activity.record()["stopped_by"] is None


def test_only_a_stopped_card_carries_a_stopper():
    activity = RunnerActivity("run_shell", {"command": "ls"})
    activity.mark_running()
    activity.settle_from_result(
        {"status": "completed", "result": {"exit_code": 0, "stopped_by": {"user_id": "u-1", "name": "Ada"}}}
    )
    assert activity.record()["stopped_by"] is None


def test_a_live_card_is_a_start_frame_and_a_settled_one_a_finish_frame():
    activity = RunnerActivity("run_shell", {"command": "ls"}, command_id="cmd-1")
    activity.mark_running()
    started = _report(activity)
    activity.settle(STATUS_COMPLETED, result={"exit_code": 0, "stdout": "a", "stderr": ""})
    finished = _report(activity)
    assert [f["event"] for f in started + finished] == [RUNNER_COMMAND_STARTED_EVENT, RUNNER_COMMAND_FINISHED_EVENT]
    assert started[0]["command"]["status"] == STATUS_RUNNING
    # The record is nested: the loop owns the frame's status / tool keys.
    assert "status" not in {k for k in started[0] if k != "command"}


def test_outside_a_chat_turn_nothing_is_built_or_sent():
    activity = RunnerActivity("run_shell", {"command": "ls"})
    calls: list[int] = []
    original = activity.record
    activity.record = lambda: calls.append(1) or original()  # type: ignore[method-assign]
    asyncio.run(activity.report())
    assert calls == []


def test_a_report_never_raises_into_the_command():
    # The funnel reports from its own ``except`` path too: a card that could
    # not be rendered must never replace the command's outcome with its own.
    activity = RunnerActivity("run_shell", {"command": "ls"}, command_id="cmd-1")

    def _broken() -> dict:
        raise RuntimeError("render bug")

    activity.record = _broken  # type: ignore[method-assign]
    with _capture() as frames:
        asyncio.run(activity.report())
    assert frames == []


def test_a_listing_is_normalised_once_whatever_the_budget_ladder(monkeypatch):
    # The runner does not cap a listing: a folder of thousands of entries must
    # not be rebuilt and re-sorted on the event loop at every budget step.
    calls: list[int] = []
    original = runner_activity._entry
    monkeypatch.setattr(runner_activity, "_entry", lambda raw: calls.append(1) or original(raw))
    entries = [{"name": f"file-{i:05d}.log", "is_dir": False, "size": i} for i in range(3000)]
    activity = RunnerActivity("list_dir", {"path": "/var/log"}, command_id="cmd-1")
    activity.mark_running()
    activity.settle(STATUS_COMPLETED, result={"entries": entries})
    first = activity.record()
    second = activity.record()
    assert len(calls) == len(entries)
    assert first == second
    assert encoded_size(first) <= RECORD_CHAR_BUDGET
    assert first["output"]["entry_count"] == 3000 and first["output"]["entries_truncated"] is True


def test_refusals_get_distinct_card_keys():
    # A synthetic key is an upsert key: two refusals of one batch must never
    # merge into one card.
    ids = {RunnerActivity("run_shell", {}).command_id for _ in range(2000)}
    assert len(ids) == 2000
    assert all(i.startswith("local-") for i in ids)


# ── The funnel ───────────────────────────────────────────────────────────────


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _Session:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, *_a, **_k):
        return _Result(self._rows)


class _SessionFactory:
    def __init__(self, rows):
        self.rows = rows

    def __call__(self):
        rows = self.rows

        class _Ctx:
            async def __aenter__(self):
                return _Session(rows)

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


def _wire(monkeypatch, rows, *, refusal=None, dispatch_status="pending", result=None, result_error=None):
    monkeypatch.setattr(computer_use, "async_session", _SessionFactory(rows))
    monkeypatch.setattr(computer_use, "authorize_command", lambda *_a, **_k: refusal)
    monkeypatch.setattr(computer_use, "command_requires_approval", lambda *_a, **_k: False)

    async def _dispatch(**_kwargs):
        return ("" if dispatch_status == "busy" else "cmd-9"), dispatch_status

    async def _await_result(cid, timeout=None, *, not_after=None):
        if result_error is not None:
            raise result_error
        return result

    monkeypatch.setattr(computer_use, "dispatch_command", _dispatch)
    monkeypatch.setattr(computer_use, "await_result", _await_result)


def _call(owner, laptop, name, *, pinned=True, **arguments):
    _, executor = build_computer_use_tools(
        user_id=str(owner),
        is_admin=False,
        user_group_ids=[],
        agent_runner_ids=None,
        agent_runner_labels=None,
        conversation_id=str(uuid.uuid4()),
        local_runner=laptop if pinned else None,
        interactive_approval=pinned,
    )
    with _capture() as frames:
        result = asyncio.run(executor.execute_tool(ToolCall(id="tc-1", name=name, arguments=arguments)))
    return result, frames


def _statuses(frames):
    return [f["command"]["status"] for f in frames if "command" in f]


def test_a_command_that_ran_reports_running_then_its_output(monkeypatch):
    owner = uuid.uuid4()
    laptop = _runner(owner, name="SAMUEL-LAPTOP")
    _wire(
        monkeypatch,
        [laptop],
        result={
            "id": "cmd-9",
            "status": "completed",
            "result": {"exit_code": 0, "stdout": "C  120 GB\n", "stderr": ""},
            "error": None,
            "result_file_id": None,
            "kind": "run_shell",
        },
    )
    result, frames = _call(owner, laptop, "run_shell", command="Get-PSDrive", cwd="C:\\")
    assert _statuses(frames) == ["running", "completed"]
    card = frames[-1]["command"]
    assert card["command_id"] == "cmd-9"
    assert card["runner_name"] == "SAMUEL-LAPTOP" and card["local"] is True and card["platform"] == "windows"
    assert card["input"] == {"command": "Get-PSDrive", "cwd": "C:\\"}
    assert card["output"]["stdout"] == "C  120 GB\n"
    # The model still reads the same prose.
    assert "exit code: 0" in result.content and "C  120 GB" in result.content


def test_browser_control_sends_the_tab_to_select_and_offers_the_tab_actions(monkeypatch):
    owner = uuid.uuid4()
    laptop = _runner(owner, name="SAMUEL-LAPTOP")
    landed = {"action": "select_tab", "tab_id": "TAB-2", "title": "Signed in", "url": "https://app.example/home"}
    _wire(
        monkeypatch,
        [laptop],
        result={
            "id": "cmd-9",
            "status": "completed",
            "result": landed,
            "error": None,
            "result_file_id": None,
            "kind": "browser_control",
        },
    )
    sent: dict = {}

    async def _dispatch(**kwargs):
        sent.update(kwargs)
        return "cmd-9", "pending"

    monkeypatch.setattr(computer_use, "dispatch_command", _dispatch)
    result, frames = _call(owner, laptop, "browser_control", action="select_tab", tab_id="TAB-2")
    assert not result.is_error
    assert sent["kind"] == "browser_control"
    assert sent["payload"] == {"action": "select_tab", "tab_id": "TAB-2"}
    assert frames[-1]["command"]["input"] == {"action": "select_tab", "tab_id": "TAB-2"}

    defs, _ = build_computer_use_tools(
        user_id=str(owner),
        is_admin=False,
        user_group_ids=[],
        agent_runner_ids=None,
        agent_runner_labels=None,
    )
    schema = {d.name: d for d in defs}["browser_control"].input_schema["properties"]
    assert {"list_tabs", "select_tab"} <= set(schema["action"]["enum"])
    assert "tab_id" in schema


# Derived, not a literal: a high-entropy literal next to "SECRET" trips the betterleaks generic-api-key rule.
_VARIABLE_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "xtm-one-test-runner-variable"))


def _variables(monkeypatch, values: dict[str, str]):
    seen: dict = {}

    async def _load(_db, user_id, group_ids=None):
        seen["identity"] = (user_id, group_ids)
        return values

    monkeypatch.setattr("app.services.variable_resolver.load_variables", _load)
    return seen


@pytest.mark.parametrize("tool", ["browser_control", "computer_action"])
def test_typed_text_resolves_a_secret_variable_before_dispatch(monkeypatch, tool):
    owner = uuid.uuid4()
    laptop = _runner(owner, name="SAMUEL-LAPTOP")
    _wire(
        monkeypatch,
        [laptop],
        result={
            "id": "cmd-9",
            "status": "completed",
            "result": {"ok": True},
            "error": None,
            "result_file_id": None,
            "kind": tool,
        },
    )
    seen = _variables(monkeypatch, {_VARIABLE_ID: "s3cr3t-pw"})
    sent: dict = {}

    async def _dispatch(**kwargs):
        sent.update(kwargs)
        return "cmd-9", "pending"

    monkeypatch.setattr(computer_use, "dispatch_command", _dispatch)
    arguments = {"action": "type", "text": "{{var_id." + _VARIABLE_ID.upper() + "}}"}
    if tool == "browser_control":
        arguments["selector"] = "#password"
    result, frames = _call(owner, laptop, tool, **arguments)
    assert not result.is_error
    assert sent["payload"]["text"] == "s3cr3t-pw"
    assert seen["identity"][0] == owner
    # The card shows a length, never the value.
    assert frames[-1]["command"]["input"]["text_length"] == len("s3cr3t-pw")
    assert "s3cr3t-pw" not in json.dumps(frames)


def test_typed_text_naming_an_unreadable_variable_is_refused_and_nothing_dispatched(monkeypatch):
    owner = uuid.uuid4()
    laptop = _runner(owner, name="SAMUEL-LAPTOP")
    _wire(monkeypatch, [laptop])
    _variables(monkeypatch, {})
    sent: dict = {}

    async def _dispatch(**kwargs):
        sent.update(kwargs)
        return "cmd-9", "pending"

    monkeypatch.setattr(computer_use, "dispatch_command", _dispatch)
    result, _frames = _call(
        owner, laptop, "browser_control", action="type", selector="#password", text="{{var_id." + _VARIABLE_ID + "}}"
    )
    assert not sent
    assert "Nothing typed" in result.content and _VARIABLE_ID in result.content


def test_typed_text_without_a_placeholder_reads_no_variable(monkeypatch):
    owner = uuid.uuid4()
    laptop = _runner(owner, name="SAMUEL-LAPTOP")
    _wire(
        monkeypatch,
        [laptop],
        result={
            "id": "cmd-9",
            "status": "completed",
            "result": {"ok": True},
            "error": None,
            "result_file_id": None,
            "kind": "browser_control",
        },
    )
    seen = _variables(monkeypatch, {})
    result, _frames = _call(owner, laptop, "browser_control", action="type", selector="#login", text="plain")
    assert not result.is_error
    assert "identity" not in seen


def test_browser_control_sends_an_evaluation_and_a_frame_to_the_runner(monkeypatch):
    owner = uuid.uuid4()
    laptop = _runner(owner, name="SAMUEL-LAPTOP")
    _wire(
        monkeypatch,
        [laptop],
        result={
            "id": "cmd-10",
            "status": "completed",
            "result": {"action": "evaluate", "result": "Entra admin center", "truncated": False},
            "error": None,
            "result_file_id": None,
            "kind": "browser_control",
        },
    )
    sent: list[dict] = []

    async def _dispatch(**kwargs):
        sent.append(kwargs)
        return "cmd-10", "pending"

    monkeypatch.setattr(computer_use, "dispatch_command", _dispatch)
    result, _frames = _call(owner, laptop, "browser_control", action="evaluate", expression="document.title", timeout=5)
    assert not result.is_error
    assert sent[-1]["payload"] == {"action": "evaluate", "expression": "document.title", "timeout": 5}

    _call(owner, laptop, "browser_control", action="click", selector="#save", frame="iframe#blade")
    assert sent[-1]["payload"] == {"action": "click", "selector": "#save", "frame": "iframe#blade"}

    defs, _ = build_computer_use_tools(
        user_id=str(owner),
        is_admin=False,
        user_group_ids=[],
        agent_runner_ids=None,
        agent_runner_labels=None,
    )
    schema = {d.name: d for d in defs}["browser_control"].input_schema["properties"]
    assert "evaluate" in schema["action"]["enum"]
    assert {"expression", "timeout", "frame"} <= set(schema)


def test_a_fleet_machine_is_not_the_local_one(monkeypatch):
    owner = uuid.uuid4()
    server = _runner(owner, name="build-box")
    _wire(monkeypatch, [server], result={"status": "completed", "result": {"entries": []}, "result_file_id": None})
    _, frames = _call(owner, server, "list_dir", pinned=False, path="/srv")
    assert frames[-1]["command"]["local"] is False
    assert frames[-1]["command"]["runner_name"] == "build-box"


def test_an_unreachable_local_machine_is_a_card_naming_it(monkeypatch):
    owner = uuid.uuid4()
    laptop = _runner(owner, name="SAMUEL-LAPTOP", online=False)
    _wire(monkeypatch, [laptop])
    result, frames = _call(owner, laptop, "run_shell", command="ls")
    assert _statuses(frames) == ["unavailable"]
    card = frames[0]["command"]
    assert card["runner_name"] == "SAMUEL-LAPTOP" and card["local"] is True
    # A synthetic key: nothing reached a queue.
    assert card["command_id"].startswith("local-")
    assert "not connected" in result.content


def test_a_policy_refusal_is_a_card_with_the_reason(monkeypatch):
    owner = uuid.uuid4()
    laptop = _runner(owner)
    _wire(monkeypatch, [laptop], refusal="the 'run_shell' capability is not granted")
    result, frames = _call(owner, laptop, "run_shell", command="rm -rf /")
    assert _statuses(frames) == ["refused"]
    assert frames[0]["command"]["error"] == "the 'run_shell' capability is not granted"
    assert result.content.startswith("Refused by the runner's permission policy")


def test_a_busy_machine_is_a_card(monkeypatch):
    owner = uuid.uuid4()
    laptop = _runner(owner)
    _wire(monkeypatch, [laptop], dispatch_status="busy")
    result, frames = _call(owner, laptop, "read_file", path="C:\\a.txt")
    assert _statuses(frames) == ["busy"]
    assert "concurrent-command limit" in result.content


def test_a_machine_revoked_since_it_was_picked_is_a_card(monkeypatch):
    owner = uuid.uuid4()
    laptop = _runner(owner, name="SAMUEL-LAPTOP")
    _wire(monkeypatch, [laptop], dispatch_status="revoked")
    result, frames = _call(owner, laptop, "run_shell", command="ls")
    assert _statuses(frames) == ["unavailable"]
    assert "was revoked" in result.content


def test_a_tool_that_crashes_closes_its_card_as_failed(monkeypatch):
    owner = uuid.uuid4()
    laptop = _runner(owner)
    _wire(monkeypatch, [laptop], result_error=RuntimeError("redis down"))
    result, frames = _call(owner, laptop, "run_shell", command="ls")
    assert _statuses(frames) == ["running", "failed"]
    assert frames[-1]["command"]["error"] == "redis down"
    assert result.is_error


def _command_row(status: str, **fields) -> dict:
    return {
        "id": "cmd-9",
        "status": status,
        "result": None,
        "error": None,
        "result_file_id": None,
        "kind": "run_shell",
        **fields,
    }


def _give_up_to(monkeypatch, row: dict | None) -> list[tuple[str, str]]:
    """Record the tool's give-ups; the row comes back as ``row`` says."""
    calls: list[tuple[str, str]] = []

    async def _give_up(command_id, reason, **_kwargs):
        calls.append((command_id, reason))
        if row is not None and row["status"] == "timeout" and row["error"] is None:
            return {**row, "error": reason}
        return row

    monkeypatch.setattr(computer_use, "give_up_on_command", _give_up)
    return calls


def test_a_command_nobody_picked_up_is_cancelled_and_says_why(monkeypatch):
    owner = uuid.uuid4()
    laptop = _runner(owner, name="SAMUEL-LAPTOP")
    _wire(monkeypatch, [laptop], result=_command_row("pending"))
    calls = _give_up_to(monkeypatch, _command_row("timeout"))
    result, frames = _call(owner, laptop, "run_shell", command="ls")
    assert [command_id for command_id, _ in calls] == ["cmd-9"]
    assert _statuses(frames) == ["running", "timeout"]
    assert "never picked the command up" in result.content


def test_a_command_claimed_as_its_wait_gave_up_reports_its_result(monkeypatch):
    # The runner claimed and ran it between the wait's last read and the
    # give-up: the result that landed is reported, not a timeout.
    owner = uuid.uuid4()
    laptop = _runner(owner)
    _wire(monkeypatch, [laptop], result=_command_row("pending"))
    _give_up_to(
        monkeypatch,
        _command_row("completed", result={"exit_code": 0, "stdout": "ran after all\n", "stderr": ""}),
    )
    result, frames = _call(owner, laptop, "run_shell", command="ls")
    assert _statuses(frames) == ["running", "completed"]
    assert "ran after all" in result.content


def test_a_command_still_running_when_its_wait_ends_is_given_up_too(monkeypatch):
    # A runner holds it: the give-up is recorded so it is never sent again,
    # and the agent is not told to retry a command that may still finish.
    owner = uuid.uuid4()
    laptop = _runner(owner, name="SAMUEL-LAPTOP")
    _wire(monkeypatch, [laptop], result=_command_row("dispatched"))
    calls = _give_up_to(monkeypatch, _command_row("dispatched"))
    result, frames = _call(owner, laptop, "run_shell", command="ls")
    assert [command_id for command_id, _ in calls] == ["cmd-9"]
    assert _statuses(frames) == ["running", "timeout"]
    assert "will not be sent again" in result.content
    assert "try again" not in result.content


def test_a_turn_cancelled_right_after_dispatch_gives_the_command_up(monkeypatch):
    # The first await once the command exists is the card report: a cancel
    # landing there, before any result wait, still gives the command up.
    owner = uuid.uuid4()
    laptop = _runner(owner)
    _wire(monkeypatch, [laptop], result=_command_row("completed"))
    calls = _give_up_to(monkeypatch, _command_row("cancelled"))

    async def _cancelled(_self):
        raise asyncio.CancelledError

    monkeypatch.setattr(RunnerActivity, "report", _cancelled)
    with pytest.raises(asyncio.CancelledError):
        _call(owner, laptop, "run_shell", command="ls")
    assert calls == [("cmd-9", computer_use.TURN_ENDED_REASON)]


def test_a_turn_cancelled_while_awaiting_the_result_gives_the_command_up(monkeypatch):
    owner = uuid.uuid4()
    laptop = _runner(owner)
    _wire(monkeypatch, [laptop], result_error=asyncio.CancelledError())
    calls = _give_up_to(monkeypatch, _command_row("timeout"))
    with pytest.raises(asyncio.CancelledError):
        _call(owner, laptop, "run_shell", command="ls")
    assert calls == [("cmd-9", computer_use.TURN_ENDED_REASON)]


def test_list_runners_dispatches_nothing_and_reports_nothing(monkeypatch):
    owner = uuid.uuid4()
    laptop = _runner(owner)
    _wire(monkeypatch, [laptop])
    _, frames = _call(owner, laptop, "list_runners")
    assert frames == []


# ── The chat fold ────────────────────────────────────────────────────────────


def _frame(record: dict, event: str = RUNNER_COMMAND_FINISHED_EVENT) -> dict:
    return {"status": TOOL_PROGRESS_STATUS, "event": event, "tool": "run_shell", "tool_call_id": "tc", "command": record}


def _record(command_id: str, status: str = STATUS_COMPLETED, **extra) -> dict:
    return {
        "command_id": command_id,
        "kind": "run_shell",
        "status": status,
        "runner_id": "r",
        "runner_name": "laptop",
        "platform": "linux",
        "local": True,
        "started_at": "2026-09-26T07:00:00+00:00",
        "duration_ms": 12,
        "input": {"command": "ls"},
        "output": {"exit_code": 0, "stdout": "a", "stderr": ""},
        "error": None,
        **extra,
    }


def test_the_fold_keeps_first_report_order_and_upserts():
    log = RunnerActivityLog()
    assert log.observe(_frame(_record("a", STATUS_RUNNING), RUNNER_COMMAND_STARTED_EVENT))
    assert log.observe(_frame(_record("b", STATUS_RUNNING), RUNNER_COMMAND_STARTED_EVENT))
    assert log.observe(_frame(_record("a")))
    assert log.observe(_frame(_record("b")))
    snapshot = log.snapshot()
    assert [(r["command_id"], r["status"]) for r in snapshot] == [("a", "completed"), ("b", "completed")]


def test_the_fold_ignores_every_other_frame():
    log = RunnerActivityLog()
    assert not log.observe({"status": "tool_start", "tools": ["run_shell"]})
    assert not log.observe({"status": TOOL_PROGRESS_STATUS, "event": "presentation_saved", "revision": 2})
    # A runner frame with a malformed record is recognised and dropped.
    assert log.observe({"status": TOOL_PROGRESS_STATUS, "event": RUNNER_COMMAND_FINISHED_EVENT, "command": "nope"})
    assert len(log) == 0


def test_a_consulted_agents_commands_are_folded_through_the_wrappers():
    def consult(inner: dict, depth: int = 1) -> dict:
        for _ in range(depth):
            inner = {"status": "consult_progress", "agent_name": "Ops", "agent_id": "a-1", "inner": inner}
        return inner

    log = RunnerActivityLog()
    assert log.observe(consult(_frame(_record("a", STATUS_RUNNING), RUNNER_COMMAND_STARTED_EVENT)))
    # A consult of a consult: still this turn's command.
    assert log.observe(consult(_frame(_record("a")), depth=3))
    assert [(r["command_id"], r["status"]) for r in log.snapshot()] == [("a", "completed")]
    # The consult's own progress is not machine activity; nor is a wrapper
    # nested past any real handover chain.
    assert not log.observe(consult({"status": "tool_start", "tools": ["run_shell"]}))
    assert not log.observe(consult(_frame(_record("b")), depth=MAX_CONSULT_DEPTH + 1))
    assert len(log) == 1


def test_every_snapshot_closes_the_live_cards():
    # The chat persists at a segment boundary (before the round's tools run)
    # and at the final answer (after the last): no tool call of the turn is in
    # flight at either, so a card still live there never reported back - a
    # tool cancelled mid-command - and must not spin on a reload.
    log = RunnerActivityLog()
    log.observe(_frame(_record("a", "awaiting_approval"), RUNNER_COMMAND_STARTED_EVENT))
    log.observe(_frame(_record("b", STATUS_RUNNING), RUNNER_COMMAND_STARTED_EVENT))
    log.observe(_frame(_record("c")))
    assert [r["status"] for r in log.snapshot()] == [STATUS_INTERRUPTED, STATUS_INTERRUPTED, "completed"]
    log.reset()
    assert log.snapshot() == []


def test_the_fold_is_bounded_to_the_latest_records():
    log = RunnerActivityLog()
    for i in range(MAX_RUNNER_ACTIVITY_RECORDS + 7):
        log.observe(_frame(_record(f"c{i}")))
    snapshot = log.snapshot()
    assert len(snapshot) == MAX_RUNNER_ACTIVITY_RECORDS
    assert snapshot[-1]["command_id"] == f"c{MAX_RUNNER_ACTIVITY_RECORDS + 6}"


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "x",
        {"command_id": "", "kind": "run_shell", "status": "completed"},
        {"command_id": "a", "kind": "host_facts", "status": "completed"},
        {"command_id": "a", "kind": "run_shell", "status": "exploded"},
    ],
)
def test_malformed_records_are_dropped(raw):
    assert sanitize_record(raw) is None


def test_a_sanitised_record_keeps_only_known_keys_and_types():
    record = sanitize_record({**_record("a"), "local": "yes", "duration_ms": "12", "evil": "<script>", "input": "x"})
    assert record is not None
    assert "evil" not in record
    assert record["local"] is False
    assert record["duration_ms"] is None
    assert record["input"] == {}


def test_a_sanitised_stopped_record_keeps_its_stopper():
    stopper = {"user_id": "u-1", "name": "Ada"}
    assert sanitize_record(_record("a", "stopped", stopped_by=stopper))["stopped_by"] == stopper
    assert sanitize_record(_record("a", "stopped", stopped_by={"name": "Ada"}))["stopped_by"] is None
    # An older record has none; a record that was not stopped never shows one.
    assert sanitize_record(_record("a", "stopped"))["stopped_by"] is None
    assert sanitize_record(_record("a", stopped_by=stopper))["stopped_by"] is None


def test_a_persisted_list_is_served_sanitised():
    assert sanitize_activity_list(None) is None
    assert sanitize_activity_list([]) is None
    assert sanitize_activity_list([{"bad": True}]) is None
    served = sanitize_activity_list([_record("a"), {"bad": True}, _record("b")])
    assert [r["command_id"] for r in served] == ["a", "b"]


def test_the_message_response_carries_what_was_persisted():
    from app.api.v1.chat import _build_message_response

    message = SimpleNamespace(
        id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        role="assistant",
        content="Done.",
        channel="web",
        channel_message_id=None,
        sequence=3,
        created_at=datetime.now(UTC),
        llm_trace_id=None,
        metadata_json=json.dumps({RUNNER_ACTIVITY_META_KEY: [_record("a"), {"junk": 1}]}),
    )
    response = _build_message_response(message)
    assert [r["command_id"] for r in response.runner_activity] == ["a"]
    plain = SimpleNamespace(**{**message.__dict__, "metadata_json": None})
    assert _build_message_response(plain).runner_activity is None


# ── Wiring tripwires ─────────────────────────────────────────────────────────


def test_the_websocket_turn_folds_persists_and_resets_the_activity():
    source = (BACKEND_ROOT / "app" / "api" / "v1" / "chat.py").read_text(encoding="utf-8")
    assert "_turn_runner_activity = RunnerActivityLog()" in source
    on_event = source[source.index("    async def _on_event(event: dict) -> None:") :]
    on_event = on_event[: on_event.index("    async def _on_token(")]
    assert "_turn_runner_activity.observe(event)" in on_event
    collect = source[source.index("    def _collect_turn_meta(") : source.index("    def _reset_turn_tracking(")]
    assert "meta[RUNNER_ACTIVITY_META_KEY] = _turn_runner_activity.snapshot()" in collect
    reset = source[source.index("    def _reset_turn_tracking(") :]
    assert "_turn_runner_activity.reset()" in reset[:800]


def test_the_frontend_knows_every_record_status():
    source = (FRONTEND_ROOT / "src" / "lib" / "runner-activity.ts").read_text(encoding="utf-8")
    block = source[source.index("export const RUNNER_ACTIVITY_STATUSES") :]
    block = block[: block.index("] as const")]
    frontend = set(re.findall(r'"([a-z_]+)"', block))
    assert frontend == set(RECORD_STATUSES)
    live = source[source.index("export const LIVE_RUNNER_ACTIVITY_STATUSES") :]
    live = live[: live.index(";")]
    assert set(re.findall(r'"([a-z_]+)"', live)) == set(LIVE_STATUSES)


def test_the_frontend_listens_for_both_events_and_reads_the_persisted_key():
    lib = (FRONTEND_ROOT / "src" / "lib" / "runner-activity.ts").read_text(encoding="utf-8")
    assert f'"{RUNNER_COMMAND_STARTED_EVENT}"' in lib
    assert f'"{RUNNER_COMMAND_FINISHED_EVENT}"' in lib
    types = (FRONTEND_ROOT / "src" / "types" / "index.ts").read_text(encoding="utf-8")
    assert f"{RUNNER_ACTIVITY_META_KEY}?: RunnerActivity[] | null;" in types
    parts = (FRONTEND_ROOT / "src" / "components" / "chat-panel" / "MessageParts.tsx").read_text(encoding="utf-8")
    assert f"stored?.{RUNNER_ACTIVITY_META_KEY}" in parts


def test_the_module_never_imports_the_loop():
    # Display data only: the record builder must stay importable by the tools
    # without dragging the agent loop (and its import cost) in.
    source = Path(runner_activity.__file__).read_text(encoding="utf-8")
    assert "agent_loop" not in source
