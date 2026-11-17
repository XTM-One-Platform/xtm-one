"""XTM One Desktop - a chat turn sent from the machine the user sits at.

Pure tests (no database, no Redis) over the three halves of the feature:

* the desktop context - the client's ``desktop_runner_id`` resolved against
  the caller's runner visibility, fail-open to ``None``;
* the ``computer_use`` pin - the local machine re-admitted past the agent's
  selector and chosen as the default target, never silently replaced by
  another machine of the fleet when it is offline;
* the inline approval - a held shell command offered to the user through a
  ``tool_progress`` frame and waited for, with the rejected / undecided /
  approved outcomes read back into the tool result.

Plus the wiring tripwires: both chat loops take the context, both chat
surfaces (REST + WebSocket) read the client's id, and the migration that
seeds the Desktop enrollment preset is chained and lock-safe.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models.runner import Runner
from app.services.builtin_tools import computer_use
from app.services.builtin_tools.computer_use import (
    DESKTOP_APPROVAL_WAIT_SECONDS,
    approval_summary,
    await_approval_decision,
    build_computer_use_tools,
    filter_eligible_runners,
    select_target_runner,
)
from app.services.desktop_session import (
    DESKTOP_TOOL_IDS,
    DesktopContext,
    ensure_desktop_tools,
    load_desktop_context,
    parse_desktop_runner_id,
)
from app.tools.tool_events import (
    RUNNER_APPROVAL_REQUIRED_EVENT,
    RUNNER_APPROVAL_RESOLVED_EVENT,
    bind_tool_event_sink,
    reset_tool_event_sink,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = BACKEND_ROOT.parent / "frontend"


# ── Fakes ────────────────────────────────────────────────────────────────────


class _Result:
    def __init__(self, one=None, rows=None):
        self._one = one
        self._rows = rows or []

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._one


class _Savepoint:
    """The CORE savepoint (``connection().begin_nested()``) the loader isolates
    its lookup in; records whether it was released (clean exit) or rolled back."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        self._session.savepoints_opened += 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is None:
            self._session.savepoints_released += 1
        else:
            self._session.savepoints_rolled_back += 1
        return False


class _FakeConnection:
    def __init__(self, session):
        self._session = session

    def begin_nested(self):
        return _Savepoint(self._session)


class _NoAutoflush:
    """``session.no_autoflush`` as a context manager; counts its uses."""

    def __init__(self, session):
        self._session = session

    def __enter__(self):
        self._session.no_autoflush_entered += 1
        return self

    def __exit__(self, *exc):
        return False


class _FakeSession:
    """The slice of ``AsyncSession`` the loader touches: ``execute``, and the
    isolation surface (``connection()`` for a Core savepoint, ``no_autoflush``)
    - the SESSION-level ``begin_nested`` is deliberately absent, since the
    loader must not use it (it flushes the caller's pending state)."""

    def __init__(self, results=None, *, raise_on_execute: Exception | None = None):
        self._results = list(results or [])
        self._raise = raise_on_execute
        self.statements: list = []
        self.savepoints_opened = 0
        self.savepoints_released = 0
        self.savepoints_rolled_back = 0
        self.no_autoflush_entered = 0
        self.no_autoflush = _NoAutoflush(self)

    async def connection(self):
        return _FakeConnection(self)

    async def execute(self, statement, *a, **k):
        if self._raise is not None:
            raise self._raise
        self.statements.append(statement)
        return self._results.pop(0) if self._results else _Result()


def _runner(
    owner: uuid.UUID | None,
    *,
    name: str = "laptop",
    online: bool = True,
    labels: list[str] | None = None,
    **columns,
) -> Runner:
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
        labels=labels or [],
        is_active=True,
        status="online" if online else "offline",
        last_seen_at=seen,
        **columns,
    )


def _load(session, **kwargs):
    return asyncio.run(load_desktop_context(session, **kwargs))


# ── Tool guarantee ───────────────────────────────────────────────────────────


def test_desktop_tools_are_added_without_removing_anything():
    resolved = ensure_desktop_tools(["web_fetch", "code_interpreter"])
    assert resolved[:2] == ["web_fetch", "code_interpreter"]
    assert set(DESKTOP_TOOL_IDS) <= set(resolved)
    assert DESKTOP_TOOL_IDS == ("computer_use",)


def test_desktop_tools_union_is_idempotent_and_handles_none():
    once = ensure_desktop_tools(None)
    assert once == list(DESKTOP_TOOL_IDS)
    assert ensure_desktop_tools(once) == once
    # An explicit empty picker ("disable every tool") still gets the suite:
    # the Desktop is what makes the agent able to act on the machine.
    assert ensure_desktop_tools([]) == list(DESKTOP_TOOL_IDS)


# ── Context loading ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [None, "", "   ", "not-a-uuid", 42, True, ["b0b4d2a8-0000-4000-8000-000000000000"]],
)
def test_malformed_desktop_id_is_not_a_desktop_turn(value):
    assert parse_desktop_runner_id(value) is None


def test_well_formed_desktop_id_parses_with_surrounding_whitespace():
    rid = uuid.uuid4()
    assert parse_desktop_runner_id(f"  {rid}  ") == rid


def test_no_desktop_id_means_no_context_and_no_query():
    session = _FakeSession()
    assert _load(session, user_id=uuid.uuid4(), runner_id=None) is None
    assert _load(session, user_id=uuid.uuid4(), runner_id="garbage") is None
    assert session.statements == []


def test_an_invisible_runner_yields_no_context():
    # The visibility filter runs in SQL: another user's private machine (or a
    # revoked / deleted one) comes back as no row.
    session = _FakeSession(results=[_Result(rows=[]), _Result(one=None)])
    assert _load(session, user_id=uuid.uuid4(), runner_id=str(uuid.uuid4())) is None


def test_a_visible_runner_becomes_the_desktop_context():
    owner = uuid.uuid4()
    machine = _runner(owner)
    session = _FakeSession(results=[_Result(rows=[]), _Result(one=machine)])
    ctx = _load(session, user_id=owner, runner_id=str(machine.id))
    assert isinstance(ctx, DesktopContext)
    assert ctx.runner is machine
    assert ctx.runner_id == str(machine.id)
    # Group memberships, the runner row, then the caller's role (for the
    # approval decision - an unknown role reads as not an admin).
    assert len(session.statements) == 3
    assert ctx.approver_is_admin is False


def test_the_callers_real_role_rides_the_context_for_approvals_only():
    """Targeting never uses admin-wide visibility, but deciding a held command
    on a company-managed machine is an admin's power: the caller's role is
    read once and carried on the context, separately."""
    admin = uuid.uuid4()
    kiosk = _runner(None, is_company_managed=True)
    session = _FakeSession(results=[_Result(one=kiosk), _Result(one="admin")])
    ctx = _load(session, user_id=admin, runner_id=str(kiosk.id), user_group_ids=[])
    assert ctx is not None and ctx.approver_is_admin is True
    sql = re.sub(r"\s+", " ", str(session.statements[1]))
    assert "users.role" in sql and "users.id" in sql
    # The runner query itself is still the plain three-tier one (no admin bypass).
    runner_sql = re.sub(r"\s+", " ", str(session.statements[0]))
    assert "runners.user_id" in runner_sql and "runners.is_company_managed" in runner_sql


def test_the_runner_query_is_scoped_and_keeps_revoked_machines_out():
    owner = uuid.uuid4()
    machine = _runner(owner)
    session = _FakeSession(results=[_Result(rows=[]), _Result(one=machine)])
    _load(session, user_id=owner, runner_id=str(machine.id))
    sql = re.sub(r"\s+", " ", str(session.statements[1]))
    where = sql[sql.upper().index(" WHERE ") :]
    # Three-tier visibility predicate + the active tombstone + the id itself.
    assert "runners.user_id" in where
    assert "runners.is_company_managed" in where
    assert "runners.is_active" in where
    assert "runners.id" in where


def test_group_ids_are_reused_when_the_caller_already_has_them():
    owner = uuid.uuid4()
    machine = _runner(owner)
    session = _FakeSession(results=[_Result(one=machine)])
    ctx = _load(session, user_id=owner, runner_id=str(machine.id), user_group_ids=[])
    assert ctx is not None
    # No group lookup: the runner row, then the caller's role.
    assert len(session.statements) == 2
    assert "group_members" not in str(session.statements[0]).lower()


def test_a_lookup_failure_fails_open_to_a_plain_chat():
    """A failed statement aborts the transaction it ran in: the lookup runs in
    a CORE savepoint under ``no_autoflush`` (the ``ai_state`` recipe - the
    session's own ``begin_nested`` would flush and then expire the caller's
    pending state), so the failure rolls back to it and the caller's session is
    left usable for the chat (or login) queries that follow."""
    session = _FakeSession(raise_on_execute=RuntimeError("db down"))
    assert _load(session, user_id=uuid.uuid4(), runner_id=str(uuid.uuid4())) is None
    assert session.savepoints_opened == 1 and session.savepoints_rolled_back == 1
    assert session.no_autoflush_entered == 1


def test_a_successful_lookup_releases_its_savepoint():
    owner = uuid.uuid4()
    laptop = _runner(owner)
    session = _FakeSession(results=[_Result(one=laptop)])
    ctx = _load(session, user_id=owner, runner_id=str(laptop.id), user_group_ids=[])
    assert ctx is not None and ctx.runner is laptop
    assert session.savepoints_opened == 1 and session.savepoints_released == 1
    assert session.no_autoflush_entered == 1


def test_the_loader_never_uses_the_sessions_own_savepoint():
    """Pinned: ``Session.begin_nested`` flushes the caller's pending entities and
    expires the dirty ones when the read fails - the one thing an optional,
    fail-open lookup must never do to the transaction it was handed."""
    source = (BACKEND_ROOT / "app" / "services" / "desktop_session.py").read_text(encoding="utf-8")
    assert "db.begin_nested()" not in source
    assert "connection()" in source and "no_autoflush" in source


def test_a_minimal_session_is_read_plainly():
    """A session-like object without the isolation surface (several callers'
    tests pass one) is read plainly instead of failing on an attribute."""
    owner = uuid.uuid4()
    laptop = _runner(owner)

    class _Plain:
        def __init__(self):
            self.calls = 0

        async def execute(self, statement, *a, **k):
            self.calls += 1
            return _Result(one=laptop) if self.calls == 1 else _Result(one="admin")

    ctx = _load(_Plain(), user_id=owner, runner_id=str(laptop.id), user_group_ids=[])
    assert ctx is not None and ctx.runner is laptop and ctx.approver_is_admin is True


# ── The computer_use pin ─────────────────────────────────────────────────────


def test_local_machine_is_readmitted_past_the_agent_selector_when_visible():
    owner = uuid.uuid4()
    audit_box = _runner(owner, name="audit-vm", labels=["audit"])
    laptop = _runner(owner, name="laptop")
    eligible = filter_eligible_runners(
        [audit_box, laptop],
        agent_runner_ids=None,
        agent_runner_labels=["audit"],
        assignment_runner_ids=None,
        local_runner_id=str(laptop.id),
    )
    assert [r.name for r in eligible] == ["audit-vm", "laptop"]


def test_local_machine_is_never_added_from_outside_the_visible_rows():
    owner = uuid.uuid4()
    audit_box = _runner(owner, name="audit-vm")
    stranger = _runner(uuid.uuid4(), name="someone-elses-laptop")
    eligible = filter_eligible_runners(
        [audit_box],  # the stranger's machine did not pass visibility
        agent_runner_ids=None,
        agent_runner_labels=None,
        assignment_runner_ids=None,
        local_runner_id=str(stranger.id),
    )
    assert [r.name for r in eligible] == ["audit-vm"]


def test_without_a_pin_the_selector_rules_are_unchanged():
    owner = uuid.uuid4()
    audit_box = _runner(owner, name="audit-vm", labels=["audit"])
    laptop = _runner(owner, name="laptop")
    eligible = filter_eligible_runners(
        [audit_box, laptop],
        agent_runner_ids=None,
        agent_runner_labels=["audit"],
        assignment_runner_ids=None,
    )
    assert [r.name for r in eligible] == ["audit-vm"]
    refined = filter_eligible_runners(
        [audit_box, laptop],
        agent_runner_ids=None,
        agent_runner_labels=None,
        assignment_runner_ids=[],
    )
    assert refined == []


def test_local_machine_is_the_default_target_even_with_a_fleet_online():
    owner = uuid.uuid4()
    server = _runner(owner, name="server")
    laptop = _runner(owner, name="laptop")
    chosen = select_target_runner([server, laptop], None, local_runner_id=str(laptop.id))
    assert chosen is laptop


def test_an_offline_local_machine_is_a_refusal_not_a_silent_fallback():
    owner = uuid.uuid4()
    server = _runner(owner, name="server")
    laptop = _runner(owner, name="laptop", online=False)
    outcome = select_target_runner([server, laptop], None, local_runner_id=str(laptop.id))
    assert isinstance(outcome, str)
    assert "laptop" in outcome
    assert "not connected" in outcome
    # The way out is explicit: name another machine.
    assert "runner_id" in outcome


def test_an_explicit_runner_id_still_wins_over_the_pin():
    owner = uuid.uuid4()
    server = _runner(owner, name="server")
    laptop = _runner(owner, name="laptop")
    chosen = select_target_runner(
        [server, laptop], str(server.id), local_runner_id=str(laptop.id)
    )
    assert chosen is server


def test_fleet_rule_without_a_pin_is_unchanged():
    owner = uuid.uuid4()
    a = _runner(owner, name="a")
    b = _runner(owner, name="b")
    only = select_target_runner([a], None)
    assert only is a
    several = select_target_runner([a, b], None)
    assert isinstance(several, str) and "Multiple runners are online" in several
    nothing = select_target_runner([], None)
    assert isinstance(nothing, str) and "XTM One Desktop" in nothing


def test_tool_descriptions_name_the_local_machine_and_list_marks_it():
    owner = uuid.uuid4()
    laptop = _runner(owner, name="SAMUEL-LAPTOP")
    defs, _ = build_computer_use_tools(
        user_id=str(owner),
        is_admin=False,
        user_group_ids=[],
        agent_runner_ids=None,
        agent_runner_labels=None,
        local_runner=laptop,
        interactive_approval=True,
    )
    by_name = {d.name: d for d in defs}
    prop = by_name["run_shell"].input_schema["properties"]["runner_id"]
    assert "SAMUEL-LAPTOP" in prop["description"]
    assert "XTM One Desktop" in prop["description"]
    assert "local: true" in by_name["list_runners"].description
    # Without a pin the descriptions are the fleet ones - no machine named.
    plain, _ = build_computer_use_tools(
        user_id=str(owner),
        is_admin=False,
        user_group_ids=[],
        agent_runner_ids=None,
        agent_runner_labels=None,
    )
    plain_prop = {d.name: d for d in plain}["run_shell"].input_schema["properties"]["runner_id"]
    assert "SAMUEL-LAPTOP" not in plain_prop["description"]


# ── Inline approval ──────────────────────────────────────────────────────────


def test_approval_summary_names_what_the_command_would_do():
    assert approval_summary("run_shell", {"command": "git status", "cwd": "C:/repo"}) == (
        "git status  (in C:/repo)"
    )
    assert approval_summary("run_shell", {"command": "ls"}) == "ls"
    assert approval_summary("write_file", {"path": "/home/sam/notes.md"}) == "/home/sam/notes.md"
    assert approval_summary("browser_control", {"action": "navigate", "url": "https://a"}) == (
        "navigate https://a"
    )
    assert approval_summary("browser_control", {"action": "select_tab", "tab_id": "T1"}) == (
        "select_tab T1"
    )
    assert approval_summary("browser_control", {"action": "list_tabs"}) == "list_tabs"
    assert approval_summary("screenshot", None) == "screenshot"


def test_await_approval_decision_returns_as_soon_as_the_row_leaves_the_queue(monkeypatch):
    statuses = iter(["pending_approval", "pending_approval", "pending"])

    async def _status(_cid):
        return next(statuses)

    monkeypatch.setattr(computer_use, "load_command_status", _status)
    monkeypatch.setattr(computer_use, "_APPROVAL_POLL_SECONDS", 0.001)
    assert asyncio.run(await_approval_decision("cmd", timeout=5)) == "pending"


def test_await_approval_decision_times_out_to_pending_approval(monkeypatch):
    async def _status(_cid):
        return "pending_approval"

    monkeypatch.setattr(computer_use, "load_command_status", _status)
    monkeypatch.setattr(computer_use, "_APPROVAL_POLL_SECONDS", 0.001)
    assert asyncio.run(await_approval_decision("cmd", timeout=0.01)) == "pending_approval"


def test_await_approval_decision_reports_a_missing_row(monkeypatch):
    async def _status(_cid):
        return None

    monkeypatch.setattr(computer_use, "load_command_status", _status)
    assert asyncio.run(await_approval_decision("cmd", timeout=1)) == "missing"


def test_approval_wait_fits_inside_the_loop_tool_timeout():
    # Approval, then the runner's whole report window from the delivery (its
    # ceiling and its upload retries), then what the tool keeps to answer, must
    # fit one tool call: a command approved at the last moment still has its
    # result reported, never cut by the loop.
    from app.services.builtin_tools.computer_use import TOOL_ANSWER_RESERVE_SECONDS
    from app.services.runner_transport import runner_report_window_seconds
    from app.tools.agent_loop import TOOL_EXECUTION_TIMEOUT_SECONDS

    assert (
        DESKTOP_APPROVAL_WAIT_SECONDS
        + runner_report_window_seconds()
        + TOOL_ANSWER_RESERVE_SECONDS
        < TOOL_EXECUTION_TIMEOUT_SECONDS
    )


@contextlib.contextmanager
def _capture_tool_events():
    frames: list[dict] = []

    async def _sink(frame):
        frames.append(frame)

    binding = bind_tool_event_sink(_sink, tool_name="run_shell")
    try:
        yield frames
    finally:
        reset_tool_event_sink(binding)


_APPROVAL_EVENTS = (RUNNER_APPROVAL_REQUIRED_EVENT, RUNNER_APPROVAL_RESOLVED_EVENT)


def _approvals(frames: list[dict]) -> list[dict]:
    """The approval-card frames among everything the tool reported (the
    machine-activity frames ride the same channel)."""
    return [f for f in frames if f["event"] in _APPROVAL_EVENTS]


def _activity_statuses(frames: list[dict]) -> list[str]:
    return [f["command"]["status"] for f in frames if f["event"] not in _APPROVAL_EVENTS]


class _FakeSessionFactory:
    """Stand-in for ``async_session`` yielding a session that lists ``rows``."""

    def __init__(self, rows):
        self.rows = rows

    def __call__(self):
        return _FakeSessionContext(self.rows)


class _FakeSessionContext:
    def __init__(self, rows):
        self.rows = rows

    async def __aenter__(self):
        return _FakeSession(results=[_Result(rows=self.rows)])

    async def __aexit__(self, *exc):
        return False


def _held_command_setup(monkeypatch, laptop: Runner, *, decision: str):
    """Wire a run_shell that the runner policy holds for approval."""
    monkeypatch.setattr(computer_use, "async_session", _FakeSessionFactory([laptop]))
    monkeypatch.setattr(computer_use, "authorize_command", lambda *_a, **_k: None)
    monkeypatch.setattr(computer_use, "command_requires_approval", lambda *_a, **_k: True)

    dispatched: list[dict] = []

    async def _dispatch(**kwargs):
        dispatched.append(kwargs)
        return "cmd-1", "pending_approval"

    async def _decision(_cid, _timeout):
        return decision

    awaited: list[str] = []

    async def _await_result(cid, timeout=None, *, not_after=None):
        awaited.append(cid)
        return {
            "id": cid,
            "status": "completed",
            "result": {"exit_code": 0, "stdout": "ok", "stderr": ""},
            "error": None,
            "result_file_id": None,
            "kind": "run_shell",
        }

    monkeypatch.setattr(computer_use, "dispatch_command", _dispatch)
    monkeypatch.setattr(computer_use, "await_approval_decision", _decision)
    monkeypatch.setattr(computer_use, "await_result", _await_result)
    return dispatched, awaited


def _run_shell(defs_exec, **kwargs) -> str:
    """Drive ``run_shell`` through the executor's public path, as the loop does."""
    from app.tools.base import ToolCall

    _, executor = defs_exec
    call = ToolCall(id="tc-1", name="run_shell", arguments=kwargs)
    result = asyncio.run(executor.execute_tool(call))
    return result.content


def _build(owner: uuid.UUID, laptop: Runner, *, interactive: bool, approver_is_admin: bool = False):
    return build_computer_use_tools(
        user_id=str(owner),
        is_admin=False,
        user_group_ids=[],
        agent_runner_ids=None,
        agent_runner_labels=None,
        conversation_id=str(uuid.uuid4()),
        local_runner=laptop if interactive else None,
        interactive_approval=interactive,
        approver_is_admin=approver_is_admin,
    )


def test_unattended_runs_keep_the_queue_and_return_behaviour(monkeypatch):
    owner = uuid.uuid4()
    laptop = _runner(owner)
    dispatched, awaited = _held_command_setup(monkeypatch, laptop, decision="pending")
    with _capture_tool_events() as frames:
        out = _run_shell(_build(owner, laptop, interactive=False), command="ls")
    assert "queued for human approval" in out
    assert "Desktop page" in out
    assert awaited == []  # nothing to wait on: the tool returned at once
    assert _approvals(frames) == []  # no approval card outside a live desktop chat
    assert _activity_statuses(frames) == ["not_approved"]
    assert dispatched[0]["requires_approval"] is True


def test_desktop_chat_offers_the_command_inline_and_runs_it_once_approved(monkeypatch):
    owner = uuid.uuid4()
    laptop = _runner(owner, name="laptop")
    _dispatched, awaited = _held_command_setup(monkeypatch, laptop, decision="pending")
    with _capture_tool_events() as frames:
        out = _run_shell(_build(owner, laptop, interactive=True), command="git status")
    # The card, then the resolution - both keyed on WHAT happened.
    approvals = _approvals(frames)
    assert [f["event"] for f in approvals] == [
        RUNNER_APPROVAL_REQUIRED_EVENT,
        RUNNER_APPROVAL_RESOLVED_EVENT,
    ]
    card = approvals[0]
    assert card["command_id"] == "cmd-1"
    assert card["runner_name"] == "laptop"
    assert card["kind"] == "run_shell"
    assert card["summary"] == "git status"
    assert card["expires_in_seconds"] == DESKTOP_APPROVAL_WAIT_SECONDS
    assert approvals[1]["status"] == "pending"
    # Approved: the command ran and its output is in THIS turn.
    assert awaited == ["cmd-1"]
    assert "exit code: 0" in out and "ok" in out
    # The transcript card followed it: held, running, done - one command id.
    assert _activity_statuses(frames) == ["awaiting_approval", "running", "completed"]
    assert {f["command"]["command_id"] for f in frames if "command" in f} == {"cmd-1"}


def test_a_rejected_command_tells_the_agent_not_to_retry(monkeypatch):
    owner = uuid.uuid4()
    laptop = _runner(owner, name="laptop")
    _dispatched, awaited = _held_command_setup(monkeypatch, laptop, decision="cancelled")
    with _capture_tool_events() as frames:
        out = _run_shell(_build(owner, laptop, interactive=True), command="rm -rf build")
    assert _approvals(frames)[-1]["status"] == "cancelled"
    assert awaited == []
    assert "REJECTED" in out
    assert "rm -rf build" in out
    assert "Do not retry" in out
    assert _activity_statuses(frames) == ["awaiting_approval", "rejected"]


def test_an_undecided_command_stays_queued_and_is_reported_as_waiting(monkeypatch):
    owner = uuid.uuid4()
    laptop = _runner(owner, name="laptop")
    _dispatched, awaited = _held_command_setup(monkeypatch, laptop, decision="pending_approval")
    with _capture_tool_events() as frames:
        out = _run_shell(_build(owner, laptop, interactive=True), command="npm test")
    assert _approvals(frames)[-1]["status"] == "pending_approval"
    assert awaited == []
    assert "not approved" in out
    assert "Do not retry" in out
    assert _activity_statuses(frames) == ["awaiting_approval", "not_approved"]


def test_inline_approval_is_not_offered_when_the_user_cannot_decide(monkeypatch):
    """The card's buttons call approve / reject, which are ``can_manage``-gated:
    a company-managed machine is decided by admins only. Offering the card to
    a non-admin sitting at one would wait out the window on buttons that all
    answer 403 - so the tool falls back to queue-and-return, like an
    unattended run, and never emits the frame."""
    user = uuid.uuid4()
    kiosk = _runner(None, name="kiosk", is_company_managed=True)
    _dispatched, awaited = _held_command_setup(monkeypatch, kiosk, decision="pending")
    with _capture_tool_events() as frames:
        out = _run_shell(_build(user, kiosk, interactive=True), command="ls")
    assert _approvals(frames) == []
    assert awaited == []
    assert "queued for human approval" in out


def test_an_admin_on_a_company_managed_machine_does_get_the_card(monkeypatch):
    """The one role that may decide on a company-managed machine: the caller's
    REAL role (`approver_is_admin`) drives the card, while targeting keeps its
    non-admin visibility - the two are separate questions."""
    admin = uuid.uuid4()
    kiosk = _runner(None, name="kiosk", is_company_managed=True)
    _dispatched, awaited = _held_command_setup(monkeypatch, kiosk, decision="pending")
    with _capture_tool_events() as frames:
        out = _run_shell(_build(admin, kiosk, interactive=True, approver_is_admin=True), command="ls")
    assert [f["event"] for f in _approvals(frames)] == [
        RUNNER_APPROVAL_REQUIRED_EVENT,
        RUNNER_APPROVAL_RESOLVED_EVENT,
    ]
    assert awaited == ["cmd-1"]
    assert "exit code: 0" in out


def test_an_explicit_target_the_user_cannot_manage_is_queued_not_offered_inline(monkeypatch):
    """The eligibility is decided per SELECTED target, not per local machine:
    a desktop turn that names another runner by id - visible to the user but
    not theirs to manage - queues the command instead of showing a card whose
    buttons would all answer 403."""
    owner, colleague = uuid.uuid4(), uuid.uuid4()
    laptop = _runner(owner, name="laptop")
    shared_box = _runner(colleague, name="shared-box")
    _dispatched, awaited = _held_command_setup(monkeypatch, laptop, decision="pending")
    monkeypatch.setattr(computer_use, "async_session", _FakeSessionFactory([laptop, shared_box]))
    with _capture_tool_events() as frames:
        out = _run_shell(_build(owner, laptop, interactive=True), command="ls", runner_id=str(shared_box.id))
    assert _approvals(frames) == []
    assert awaited == []
    assert "queued for human approval" in out


def test_can_manage_and_can_manage_as_are_one_rule():
    """``can_manage_as`` is what the tool builder consults (it carries ids, not
    a User row); it must never drift from ``can_manage``."""
    from app.api.v1.helpers import can_manage, can_manage_as

    owner, stranger = uuid.uuid4(), uuid.uuid4()
    private = _runner(owner)
    kiosk = _runner(None, is_company_managed=True)
    for entity in (private, kiosk):
        for uid, role in ((owner, "user"), (stranger, "user"), (stranger, "admin")):
            user = SimpleNamespace(id=uid, role=role)
            assert can_manage(user, entity, []) == can_manage_as(uid, role == "admin", entity, [])
    assert can_manage_as(owner, False, private, []) is True
    assert can_manage_as(stranger, False, private, []) is False
    assert can_manage_as(stranger, False, kiosk, []) is False
    assert can_manage_as(stranger, True, kiosk, []) is True


def test_a_cancelled_turn_abandons_the_held_command(monkeypatch):
    """Stop, a wall-clock deadline or the tool ceiling can cancel the wait under
    the card. The command was offered WITH this chat's context: it is cancelled
    on the row (guarded - a decision that already landed stands) and the chat
    hears the card is over, instead of a silent later run from the Desktop
    page. The cancellation itself still propagates."""
    owner = uuid.uuid4()
    laptop = _runner(owner, name="laptop")
    _held_command_setup(monkeypatch, laptop, decision="pending")

    async def _interrupted(_cid, _timeout):
        raise asyncio.CancelledError()

    cancelled: list[tuple[str, str]] = []

    async def _cancel(command_id, reason):
        cancelled.append((command_id, reason))
        return True

    monkeypatch.setattr(computer_use, "await_approval_decision", _interrupted)
    monkeypatch.setattr(computer_use, "cancel_held_command", _cancel)
    with _capture_tool_events() as frames, pytest.raises(asyncio.CancelledError):
        _run_shell(_build(owner, laptop, interactive=True), command="make deploy")
    assert cancelled == [("cmd-1", computer_use.APPROVAL_ABANDONED_REASON)]
    approvals = _approvals(frames)
    assert [f["event"] for f in approvals] == [
        RUNNER_APPROVAL_REQUIRED_EVENT,
        RUNNER_APPROVAL_RESOLVED_EVENT,
    ]
    assert approvals[-1]["status"] == computer_use.APPROVAL_ABANDONED_STATUS
    # The cancellation propagates untouched: the card stays live and the
    # chat closes it as interrupted when the turn ends.
    assert _activity_statuses(frames) == ["awaiting_approval"]


def test_list_runners_marks_the_local_machine(monkeypatch):
    owner = uuid.uuid4()
    laptop = _runner(owner, name="laptop")
    server = _runner(owner, name="server")
    monkeypatch.setattr(computer_use, "async_session", _FakeSessionFactory([server, laptop]))
    _, executor = _build(owner, laptop, interactive=True)
    from app.tools.base import ToolCall

    result = asyncio.run(
        executor.execute_tool(ToolCall(id="tc", name="list_runners", arguments={}))
    )
    listed = {r["name"]: r for r in json.loads(result.content)}
    assert listed["laptop"]["local"] is True
    assert listed["server"]["local"] is False


def test_list_runners_reports_the_effective_capabilities(monkeypatch):
    """A full_control runner grants everything: it never lists ``[]``.

    Agents read an empty list as "no permission" and refused a runner whose
    every command ran; the list says what ``authorize_command`` decides.
    """
    from app.models.runner import RUNNER_CAPABILITIES
    from app.services.runners import authorize_command

    owner = uuid.uuid4()
    laptop = _runner(owner, name="laptop")
    laptop.permission_mode = "full_control"
    laptop.capabilities = []
    server = _runner(owner, name="server")
    server.capabilities = ["file_ops", "unknown_cap", "run_shell"]
    monkeypatch.setattr(computer_use, "async_session", _FakeSessionFactory([server, laptop]))
    _, executor = _build(owner, laptop, interactive=True)
    from app.tools.base import ToolCall

    result = asyncio.run(
        executor.execute_tool(ToolCall(id="tc", name="list_runners", arguments={}))
    )
    listed = {r["name"]: r for r in json.loads(result.content)}
    assert listed["laptop"]["capabilities"] == list(RUNNER_CAPABILITIES)
    assert listed["server"]["capabilities"] == ["run_shell", "file_ops"]
    for kind in ("screenshot", "computer_action", "run_shell", "read_file", "browser_control"):
        assert authorize_command(laptop, kind, {"command": "echo hi", "path": "notes.txt"}) is None


# ── Wiring tripwires ─────────────────────────────────────────────────────────


def test_both_chat_loops_take_the_desktop_context():
    from app.services.agent_chat import run_agent_chat
    from app.services.general_chat import run_general_chat

    for fn in (run_agent_chat, run_general_chat):
        params = inspect.signature(fn).parameters
        assert "desktop" in params, fn.__name__
        assert params["desktop"].default is None


def test_both_chat_surfaces_read_the_client_desktop_id():
    source = (BACKEND_ROOT / "app" / "api" / "v1" / "chat.py").read_text(encoding="utf-8")
    # REST: the request model carries it and the turn resolves it.
    assert "desktop_runner_id: str | None = None" in source
    assert "runner_id=body.desktop_runner_id" in source
    # WebSocket: the frame carries it and the turn resolves it.
    assert 'data.get("desktop_runner_id")' in source
    # Every loop receives what was resolved, on both surfaces: the agent, the
    # General Assistant and an @mentioned agent.
    assert source.count("desktop=_desktop,") == 3
    assert source.count("desktop=_turn_desktop,") == 3


def test_general_assistant_builds_the_computer_use_suite():
    source = (BACKEND_ROOT / "app" / "services" / "general_chat.py").read_text(encoding="utf-8")
    assert "build_computer_use_tools(" in source
    # Never with the admin bypass: targeting is an execution power.
    block = source[source.index("build_computer_use_tools(") :]
    assert "is_admin=False" in block[:800]


def test_the_pin_grounds_the_model_in_tool_descriptions_not_the_system_prompt():
    # Invariant 15: no desktop block in either system prompt assembly.
    for name in ("agent_chat.py", "general_chat.py"):
        source = (BACKEND_ROOT / "app" / "services" / name).read_text(encoding="utf-8")
        assert "format_desktop_prompt" not in source
        assert "[XTM One Desktop]" not in source


def test_enrollment_key_seeds_require_approval_end_to_end():
    from app.api.v1.runners import EnrollmentKeyCreate

    assert EnrollmentKeyCreate(name="k").default_require_approval is False
    assert EnrollmentKeyCreate(name="k", default_require_approval=True).default_require_approval
    source = (BACKEND_ROOT / "app" / "api" / "v1" / "runners.py").read_text(encoding="utf-8")
    # Stamped on the key at creation and onto the machine at enrollment.
    assert "default_require_approval=body.default_require_approval" in source
    assert "require_approval=bool(key.default_require_approval)" in source
    # Serialised so the Desktop page can show which preset a key carries.
    from app.services.runners import serialize_enrollment_key

    key = SimpleNamespace(
        id=uuid.uuid4(),
        name="desktop",
        key_prefix="renr-xxxx",
        max_uses=1,
        use_count=0,
        default_labels=[],
        default_capabilities=["run_shell"],
        default_permission_mode="restricted",
        default_require_approval=True,
        enroll_as_company_managed=False,
        is_active=True,
        expires_at=None,
        last_used_at=None,
        created_at=datetime.now(UTC),
    )
    assert serialize_enrollment_key(key)["default_require_approval"] is True


def test_migration_is_lock_safe_and_chained_to_the_previous_head():
    path = (
        BACKEND_ROOT
        / "alembic"
        / "versions"
        / "a7f3run0004_add_enrollment_key_default_require_approval.py"
    )
    source = path.read_text(encoding="utf-8")
    assert 'down_revision: str | None = "f1sha2dedup01"' in source
    assert "terminate_lock_holders(conn, _TABLE)" in source
    assert "set_lock_timeout(conn)" in source
    assert "ADD COLUMN IF NOT EXISTS" in source
    assert "DEFAULT false" in source
