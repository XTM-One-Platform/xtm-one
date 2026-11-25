"""The manual-trigger gate shared by REST and the agent tools.

``POST /assignments/{id}/trigger``, the agent-builder ``trigger_assignment``
tool and the work-items ``delegate_to_assignment`` tool all start a run AS
their caller, so they answer through ONE gate in ``api/v1/helpers.py``:
``manual_trigger_refusal`` (enabled, company-managed admin-only unless per
user) and ``manual_trigger_overlap`` (the active-run guard). Per-tier
behaviour through the real routes and tools is pinned against PostgreSQL in
``test_assignment_visibility_postgres.py``.
"""

from __future__ import annotations

import ast
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api.v1 import helpers
from app.api.v1.helpers import (
    find_user_assignment,
    manual_trigger_overlap,
    manual_trigger_refusal,
)


def _assignment(**overrides):
    fields = {
        "id": uuid.uuid4(),
        "enabled": True,
        "is_company_managed": False,
        "run_as_all_users": False,
        "hide_runs": None,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _agent(**overrides):
    fields = {"enabled": True, "hide_runs": False}
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _reason(assignment, agent, *, is_admin):
    refusal = manual_trigger_refusal(assignment, agent, is_admin=is_admin)
    return None if refusal is None else refusal.reason


def test_the_gate_answers_in_rest_order_with_rest_status_codes():
    assert _reason(_assignment(enabled=False), None, is_admin=True) == "assignment_disabled"
    assert _reason(_assignment(), None, is_admin=True) == "agent_missing"
    assert _reason(_assignment(), _agent(enabled=False), is_admin=True) == "agent_disabled"
    refusal = manual_trigger_refusal(
        _assignment(is_company_managed=True), _agent(), is_admin=False
    )
    assert (refusal.status_code, refusal.detail) == (
        403,
        "Only admins can trigger company-managed assignments",
    )


@pytest.mark.parametrize(
    ("assignment", "agent", "allowed"),
    [
        (_assignment(), _agent(), True),
        (_assignment(is_company_managed=True), _agent(), False),
        (_assignment(is_company_managed=True, hide_runs=True), _agent(), True),
        (_assignment(is_company_managed=True, run_as_all_users=True), _agent(), True),
        (_assignment(is_company_managed=True), _agent(hide_runs=True), True),
        (
            _assignment(is_company_managed=True, hide_runs=False),
            _agent(hide_runs=True),
            False,
        ),
    ],
    ids=["personal", "company", "hidden-runs", "fan-out", "inherited", "overridden"],
)
def test_a_company_managed_assignment_is_admin_only_unless_it_runs_per_user(
    assignment, agent, allowed
):
    assert (_reason(assignment, agent, is_admin=False) is None) is allowed
    assert _reason(assignment, agent, is_admin=True) is None


def test_the_action_is_worded_by_the_caller():
    refusal = manual_trigger_refusal(
        _assignment(is_company_managed=True), _agent(), is_admin=False, action="delegate to"
    )
    assert refusal.detail == "Only admins can delegate to company-managed assignments"


@pytest.mark.asyncio
async def test_the_overlap_guard_of_a_per_user_assignment_is_the_caller_s_run(monkeypatch):
    calls = []
    active = SimpleNamespace(
        id=uuid.uuid4(),
        status="running",
        created_at=datetime(2026, 10, 20, 19, 50, tzinfo=UTC),
        started_at=datetime(2026, 10, 20, 19, 53, 12, tzinfo=UTC),
    )

    async def _find_active_run(_db, assignment_id, *, user_id=None):
        calls.append(user_id)
        return active

    monkeypatch.setattr("app.services.assignment_engine.find_active_run", _find_active_run)
    uid = uuid.uuid4()

    per_user = await manual_trigger_overlap(None, _assignment(hide_runs=True), _agent(), uid)
    whole = await manual_trigger_overlap(None, _assignment(), _agent(), uid)

    assert (per_user.status_code, per_user.reason) == (409, "own_run_active")
    assert (whole.status_code, whole.reason) == (409, "run_active")
    assert calls == [uid, None]


@pytest.mark.asyncio
async def test_the_overlap_refusal_names_the_run_in_flight(monkeypatch):
    """A refused second trigger says which run holds the assignment, so the
    caller waits on it instead of guessing; no run, no refusal."""
    run_id = uuid.uuid4()
    active = SimpleNamespace(
        id=run_id,
        status="running",
        created_at=datetime(2026, 10, 20, 19, 50, tzinfo=UTC),
        started_at=datetime(2026, 10, 20, 19, 53, 12, tzinfo=UTC),
    )

    async def _find_active_run(_db, assignment_id, *, user_id=None):
        return active

    monkeypatch.setattr("app.services.assignment_engine.find_active_run", _find_active_run)
    refusal = await manual_trigger_overlap(None, _assignment(), _agent(), uuid.uuid4())

    assert refusal.active_run.id == str(run_id)
    assert refusal.active_run.status == "running"
    assert refusal.active_run.describe() == (
        f"Run `{run_id}` is running, started 2026-10-20 19:53:12 UTC."
    )
    queued = helpers.ActiveRun(id="r1", status="pending", created_at=active.created_at, started_at=None)
    assert queued.describe() == "Run `r1` is pending, created 2026-10-20 19:50:00 UTC."

    async def _none(_db, assignment_id, *, user_id=None):
        return None

    monkeypatch.setattr("app.services.assignment_engine.find_active_run", _none)
    assert await manual_trigger_overlap(None, _assignment(), _agent(), uuid.uuid4()) is None


def test_both_tools_word_the_run_in_flight():
    """The builder's trigger and the delegation both hand the caller the run's
    id, status and start, and say to wait on it."""
    from app.services.builtin_tools.agent_builder import _trigger_refusal_message
    from app.services.builtin_tools.work_items import _delegate_refusal_message

    started = datetime(2026, 10, 20, 19, 53, 12, tzinfo=UTC)
    active = helpers.ActiveRun(id="run-1", status="running", created_at=started, started_at=started)
    refusal = helpers.manual_trigger_skipped(None, active_run=active)
    named = SimpleNamespace(name="Eng recap")

    for text in (
        _trigger_refusal_message(refusal, named, named),
        _delegate_refusal_message(refusal, named),
    ):
        assert "Run `run-1` is running, started 2026-10-20 19:53:12 UTC." in text
        assert "Wait for that run" in text

    bare = _trigger_refusal_message(helpers.manual_trigger_skipped(None), named, named)
    assert "already has an active run" in bare
    assert "Run `" not in bare


@pytest.mark.asyncio
async def test_the_engine_s_atomic_guard_can_be_scoped_to_one_user(monkeypatch):
    """``trigger_assignment(skip_if_active=True, active_run_user_id=...)`` checks
    the user's own run under the per-assignment advisory lock, so a per-user
    delegation keeps an atomic guard; without the scope it guards the whole
    assignment, as every existing caller expects."""
    from app.services import assignment_engine

    checks = []
    locks = []

    async def _no_ai_gate(_assignment):
        return None

    async def _lock(_db, assignment_id):
        locks.append(assignment_id)

    async def _has_active_run(_db, assignment_id, *, user_id=None):
        checks.append(user_id)
        return True

    class _DB:
        async def commit(self):
            return None

    monkeypatch.setattr(assignment_engine, "_refuse_if_ai_disabled", _no_ai_gate)
    monkeypatch.setattr(assignment_engine, "_acquire_trigger_overlap_lock", _lock)
    monkeypatch.setattr(assignment_engine, "has_active_run", _has_active_run)
    # Ownerless: the funnel's owner-deactivation backstop looks nothing up.
    assignment = SimpleNamespace(
        id=uuid.uuid4(),
        name="Report",
        enabled=True,
        user_id=None,
        is_company_managed=False,
    )
    agent = SimpleNamespace(name="Agent", enabled=True)
    uid = uuid.uuid4()

    scoped = await assignment_engine.trigger_assignment(
        assignment, agent, db=_DB(), skip_if_active=True, active_run_user_id=uid
    )
    whole = await assignment_engine.trigger_assignment(
        assignment, agent, db=_DB(), skip_if_active=True
    )

    assert scoped is None and whole is None
    assert checks == [uid, None]
    assert locks == [assignment.id, assignment.id]


@pytest.mark.asyncio
async def test_a_caller_with_no_account_is_looked_up_without_groups(monkeypatch):
    async def _no_groups(*_a):
        raise AssertionError("no account, no groups")

    monkeypatch.setattr(helpers, "get_user_group_ids", _no_groups)

    class _DB:
        async def execute(self, _stmt):
            return SimpleNamespace(scalar_one_or_none=lambda: None)

    assert await find_user_assignment(_DB(), None, str(uuid.uuid4())) is None


class _CapturingDB:
    def __init__(self):
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        return SimpleNamespace(scalar_one_or_none=lambda: None)


@pytest.mark.asyncio
@pytest.mark.parametrize("is_admin", [True, False], ids=["admin", "member"])
async def test_an_admin_reaches_every_assignment(monkeypatch, is_admin):
    """An admin is looked up with no visibility check and no group lookup;
    anyone else through the parent agent's three tiers, groups and all."""
    from sqlalchemy.dialects import postgresql

    groups = []

    async def _groups(_db, uid):
        groups.append(uid)
        return []

    monkeypatch.setattr(helpers, "get_user_group_ids", _groups)
    db = _CapturingDB()
    uid = uuid.uuid4()

    await find_user_assignment(db, uid, str(uuid.uuid4()), is_admin=is_admin)

    where = str(
        db.statements[0].whereclause.compile(dialect=postgresql.dialect())
    ).lower()
    assert ("from agents" in where) is (not is_admin)
    assert groups == ([] if is_admin else [uid])


_APP = Path(__file__).resolve().parents[1] / "app"
_TRIGGER_CALLERS = {
    "api/v1/assignments.py": "trigger_assignment_manual",
    "services/builtin_tools/agent_builder.py": "_trigger_assignment_tool",
    "services/builtin_tools/work_items.py": "_run_delegate_to_assignment",
}


def _names_used_in(path: Path, function: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == function:
            return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
    raise AssertionError(f"{function} not found in {path}")


def _calls(tree: ast.AST, name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == name)
            or (isinstance(node.func, ast.Attribute) and node.func.attr == name)
        )
    ]


def _keyword(call: ast.Call, name: str):
    return next((kw.value for kw in call.keywords if kw.arg == name), None)


_TOOL_MODULES = [
    *sorted((_APP / "services" / "builtin_tools").rglob("*.py")),
    _APP / "api" / "v1" / "mcp_xtm.py",
]
# The assignment tools, reads and writes.
_ASSIGNMENT_TOOL_FUNCTIONS = {
    "services/builtin_tools/agent_builder.py": (
        "_list_assignments",
        "_get_assignment",
        "_trigger_assignment_tool",
        "_load_writable_assignment_for_builder",
        "_update_assignment",
        "_delete_assignment",
        "_add_assignment_to_agent",
    ),
    "services/builtin_tools/work_items.py": ("_run_delegate_to_assignment",),
}


def _async_function(tree: ast.AST, name: str) -> ast.AsyncFunctionDef:
    return next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == name)


def _is_bypass_helper(node) -> bool:
    return isinstance(node, ast.Call) and (
        (isinstance(node.func, ast.Name) and node.func.id == "agent_tool_admin_bypass")
        or (isinstance(node.func, ast.Attribute) and node.func.attr == "agent_tool_admin_bypass")
    )


def _guarded_by_bypass_helper(function: ast.AsyncFunctionDef) -> set[int]:
    """Ids of the calls under an ``if`` whose test asks ``agent_tool_admin_bypass``."""
    guarded: set[int] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.If) and any(_is_bypass_helper(n) for n in ast.walk(node.test)):
            for statement in node.body:
                guarded.update(id(n) for n in ast.walk(statement) if isinstance(n, ast.Call))
    return guarded


# The assignment writes: a three-tier lookup first, the admin bypass only when
# it misses and ``agent_tool_admin_bypass`` allows it.
_WRITE_LOADER = ("services/builtin_tools/agent_builder.py", "_load_writable_assignment_for_builder")


def test_every_agent_tool_takes_the_admin_bypass_through_one_helper():
    """An agent tool reaches the assignments its caller reaches through the
    REST routes, every one for an admin, unless
    ``AGENT_TOOLS_STRICT_ADMIN_ACCESS``: each ``find_user_assignment``
    and ``assignment_visible_filter`` call of the assignment tools passes
    ``is_admin=agent_tool_admin_bypass(...)``, the write loader alone looks
    three-tier first and then passes ``True`` under an ``if`` that asks that
    helper, and no tool uses the HTTP wrapper ``get_user_assignment``."""
    offenders = []
    found = 0
    write_lookups = {"three_tier": 0, "bypass": 0}
    for path in _TOOL_MODULES:
        rel = path.relative_to(_APP).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        loader = _async_function(tree, _WRITE_LOADER[1]) if rel == _WRITE_LOADER[0] else None
        loader_calls = {id(n) for n in ast.walk(loader)} if loader is not None else set()
        guarded = _guarded_by_bypass_helper(loader) if loader is not None else set()
        for call in _calls(tree, "find_user_assignment"):
            found += 1
            flag = _keyword(call, "is_admin")
            if _is_bypass_helper(flag):
                continue
            if id(call) in loader_calls and flag is None:
                write_lookups["three_tier"] += 1
            elif id(call) in guarded and isinstance(flag, ast.Constant) and flag.value is True:
                write_lookups["bypass"] += 1
            else:
                offenders.append(f"{rel}:{call.lineno}: find_user_assignment bypass")
        for call in _calls(tree, "get_user_assignment"):
            offenders.append(f"{rel}:{call.lineno}: get_user_assignment (REST wrapper)")
        for function in _ASSIGNMENT_TOOL_FUNCTIONS.get(rel, ()):
            node = _async_function(tree, function)
            for call in _calls(node, "assignment_visible_filter"):
                if not _is_bypass_helper(_keyword(call, "is_admin")):
                    offenders.append(f"{rel}:{call.lineno}: {function} is_admin")
            for call in _calls(node, "_load_writable_agent_for_builder"):
                flag = _keyword(call, "assignment_write")
                if not (isinstance(flag, ast.Constant) and flag.value is True):
                    offenders.append(f"{rel}:{call.lineno}: {function} agent load")
    assert found >= 4, "the assignment tools no longer use find_user_assignment"
    assert write_lookups == {"three_tier": 1, "bypass": 1}, write_lookups
    assert not offenders, offenders

    # The agent an assignment is added to follows the same order.
    builder = ast.parse((_APP / _WRITE_LOADER[0]).read_text(encoding="utf-8"))
    agent_loader = _async_function(builder, "_load_writable_agent_for_builder")
    assert any(_is_bypass_helper(n) for n in ast.walk(agent_loader))


# The permission gates of the agent tools, by module and function, with the
# number of reads of the caller's ROLE each makes: company-managed creates and
# toggles, admin-only agent settings (channel routing, catalog), the
# manual-trigger admin-only rule, the admin-only evaluation tools, a flow's
# creator-or-admin edit, the consent to share an OAuth integration's account,
# the role a vault provider lets write a reference to it, or re-point an entity keeping
# one. ``skills.py`` reads the agent OWNER's role to
# re-verify the skills bound to the agent, which REST bound for an admin with
# the bypass - configuration, not a lookup of the tool's own.
_ROLE_GATES = {
    ("services/builtin_tools/agent_builder.py", "_apply_catalog_access"): 1,
    # Binding the administrator-only organization_context group on a copy (#11594).
    ("services/builtin_tools/agent_builder.py", "_clone_agent"): 2,
    ("services/builtin_tools/agent_builder.py", "_create_agent_row"): 1,
    ("services/builtin_tools/agent_builder.py", "_create_custom_tool"): 1,
    ("services/builtin_tools/agent_builder.py", "_create_integration"): 1,
    ("services/builtin_tools/agent_builder.py", "_create_knowledge_base"): 1,
    ("services/builtin_tools/agent_builder.py", "_create_mcp_server"): 1,
    ("services/builtin_tools/agent_builder.py", "_create_prompt"): 1,
    ("services/builtin_tools/agent_builder.py", "_create_skill"): 1,
    ("services/builtin_tools/agent_builder.py", "_create_variable"): 1,
    ("services/builtin_tools/agent_builder.py", "_trigger_assignment_tool"): 1,
    # The seventh: turning a binding to Live writes needs the authority to edit
    # the integration unless the caller is an admin, the REST rule (#11962).
    ("services/builtin_tools/agent_builder.py", "_update_agent"): 7,
    # The second: only an admin changes the company-managed status of an
    # integration, the REST update's rule (#12380).
    ("services/builtin_tools/agent_builder.py", "_update_integration"): 2,
    ("services/builtin_tools/agent_builder.py", "_update_knowledge_base"): 1,
    # delete_ctem_cases is for an administrator only, as DELETE /ctem-cases/{id}
    # and POST /ctem-cases/bulk-delete (#1723): a permission gate on the role.
    ("services/builtin_tools/ctem_cases.py", "_delete"): 1,
    # The CTEM repairs are for an administrator only, as their REST routes (#1778):
    # _admin re-reads the acting person's role at the call.
    ("services/builtin_tools/ctem_repair.py", "_admin"): 1,
    # The CTEM read tools report the acting person's role in their scope block
    # (#1359): is_admin is echoed, and the case view's rule (admin flag alone,
    # ctem_cases_view.signal_members) is named; the visibility of signals still
    # goes through agent_tool_admin_bypass.
    ("services/builtin_tools/ctem_signals.py", "reader_scope"): 1,
    ("services/builtin_tools/evaluations.py", "_compare_evaluation_runs"): 1,
    ("services/builtin_tools/evaluations.py", "_get_evaluation"): 1,
    ("services/builtin_tools/evaluations.py", "_get_evaluation_run_values"): 1,
    ("services/builtin_tools/evaluations.py", "_get_placebo_comparison"): 1,
    ("services/builtin_tools/evaluations.py", "_list_evaluation_runs"): 1,
    ("services/builtin_tools/evaluations.py", "_list_evaluations"): 1,
    ("services/builtin_tools/evaluations.py", "_trigger_evaluation"): 1,
    # Company-managed fleet creation is admin-only, like the builder's creates;
    # a built-in fleet's seeded automations are paused or resumed by an admin only;
    # update_fleet sets the instructions position for an admin only (#10255);
    # share_fleet_workspace is for an administrator or the workspace owner (#24).
    ("services/builtin_tools/fleets.py", "_action"): 4,
    # update_fleet raises the autonomy level for an administrator's chat agent
    # only, on confirmation (#10439): a permission gate on the caller's role.
    ("services/builtin_tools/fleets.py", "_check_autonomy_raise"): 1,
    # list_module_findings: read by the Platform Health Module Analyst or an
    # administrator only, a permission gate on the role (#101, S14a-2b).
    ("services/builtin_tools/health_modules.py", "_may_read"): 1,
    # get_platform_health shows the alert e-mail settings to an administrator only,
    # as the admin-only health settings route; the page itself is read through the
    # page's own role-scoped reads (#2199): a permission gate on the role.
    ("services/builtin_tools/health_orchestrator.py", "get_platform_health"): 1,
    # unbind_agent_from_intent: an administrator removes the global binding, anyone
    # else only their own personal override: a permission gate on the role.
    ("services/builtin_tools/intent_bindings.py", "_unbind_agent_from_intent"): 1,
    # update_organization_context re-reads the acting person's role at the call (#11594).
    ("services/builtin_tools/organization_context_write.py", "_update"): 1,
    # The row and import tools of the same group read the role again through one helper (#11888).
    ("services/builtin_tools/organization_context_write.py", "_admin"): 1,
    # The platform AI settings tools are administrators only: a
    # permission gate on the caller's role, not a visibility bypass.
    ("services/builtin_tools/platform_admin.py", "_admin"): 1,
    # The ROI unit catalog tools are administrators only, as their REST routes.
    ("services/builtin_tools/roi.py", "_require_admin"): 1,
    ("services/builtin_tools/skills.py", "build_skill_access_context"): 1,
    ("services/builtin_tools/skills.py", "_visible_skill_rows"): 1,
    # The user administration tools are administrators only: _admin re-reads the
    # acting person's role at the call; deactivation_refusal refuses the last
    # active administrator (the TARGET's role, the REST rule) (#250).
    ("services/builtin_tools/user_admin.py", "_admin"): 1,
    ("services/builtin_tools/user_admin.py", "deactivation_refusal"): 2,
    ("services/builtin_tools/work_items.py", "_run_delegate_to_assignment"): 1,
    # Company-managed creation is admin-only, like the builder's other creates.
    ("services/builtin_tools/workspaces.py", "_create_workspace"): 1,
    # Changing an existing fleet's or workspace's company-managed flag is admin-only,
    # as PUT /fleets/{id} and PUT /workspaces/{id} decide (#398).
    ("services/builtin_tools/fleets.py", "_set_fleet_company_managed"): 1,
    ("services/builtin_tools/workspaces.py", "_set_workspace_company_managed"): 1,
}
# The lookups that pass ``is_admin=True``: each is reached only once
# ``agent_tool_admin_bypass`` allowed it (the assignment write loader, the
# ``scope="all"`` agent listing and its name resolution).
_ADMIN_SCOPE_LOOKUPS = {
    ("services/builtin_tools/agent_builder.py", "_load_writable_assignment_for_builder"),
    ("services/builtin_tools/agent_builder.py", "_list_agents"),
    ("services/builtin_tools/agent_builder.py", "_detail_visible_clause"),
}
# The reads that pass ``is_admin=True`` to skip the trace store's per-user
# visibility for the CALLING AGENT's own rows: not the caller's role -
# any user's agent reads its own traces - and each is scoped to
# ``agent_ids=[calling agent]`` or checked against it before anything is served.
_OWN_AGENT_SCOPE_LOOKUPS = {
    ("services/builtin_tools/traces.py", "_own_trace_reader"),
}
# REST helpers the agent tools call that take the admin bypass from the
# caller's role unless told otherwise.
_ROLE_DEFAULTING_HELPERS = ("catalog_sources", "get_visible_run", "resolve_target", "may_read_cycle")


def _reads_the_admin_role(node: ast.AST) -> bool:
    if isinstance(node, ast.Attribute):
        return node.attr == "is_admin" and isinstance(node.ctx, ast.Load)
    if isinstance(node, ast.Call):
        return (
            isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "is_admin"
        )
    if isinstance(node, ast.Compare):
        return any(isinstance(c, ast.Constant) and c.value == "admin" for c in node.comparators)
    return False


def _enclosing_function(parents: dict, node: ast.AST) -> str:
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
    return "<module>"


def test_no_agent_tool_takes_the_admin_bypass_past_the_helper():
    """``AGENT_TOOLS_STRICT_ADMIN_ACCESS`` keeps EVERY agent tool of an admin to
    the admin's three tiers: what a tool can see - an agent, a run, a
    trace, a flow, an evaluation, a knowledge base, a binding - takes the admin
    bypass only through ``agent_tool_admin_bypass()``. The caller's role is read
    directly only in the permission gates listed in ``_ROLE_GATES``; an
    ``is_admin=True`` lookup sits behind the helper (``_ADMIN_SCOPE_LOOKUPS``)
    or reads the calling agent's own rows (``_OWN_AGENT_SCOPE_LOOKUPS``);
    a REST helper that defaults to the role is handed the helper's answer. A new
    read of the role in a tool module is a bypass until it is classified here."""
    reads: dict[tuple[str, str], int] = {}
    offenders = []
    for path in _TOOL_MODULES:
        rel = path.relative_to(_APP).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            if _reads_the_admin_role(node):
                key = (rel, _enclosing_function(parents, node))
                reads[key] = reads.get(key, 0) + 1
            if (
                isinstance(node, ast.keyword)
                and node.arg == "is_admin"
                and isinstance(node.value, ast.Constant)
                and node.value.value is True
                and (rel, _enclosing_function(parents, node)) not in _ADMIN_SCOPE_LOOKUPS | _OWN_AGENT_SCOPE_LOOKUPS
            ):
                offenders.append(f"{rel}:{node.value.lineno}: is_admin=True")
        for name in _ROLE_DEFAULTING_HELPERS:
            for call in _calls(tree, name):
                if not _is_bypass_helper(_keyword(call, "is_admin")):
                    offenders.append(f"{rel}:{call.lineno}: {name} without agent_tool_admin_bypass()")
    assert not offenders, offenders
    assert reads == _ROLE_GATES, {
        "unlisted": {k: v for k, v in reads.items() if _ROLE_GATES.get(k) != v},
        "gone": {k: v for k, v in _ROLE_GATES.items() if reads.get(k) != v},
    }


def test_the_rest_wrapper_forwards_the_admin_role():
    """``get_user_assignment`` - the REST detail routes - forwards its
    ``is_admin`` to the one lookup, whose signature has no second knob."""
    import inspect

    tree = ast.parse((_APP / "api" / "v1" / "helpers.py").read_text(encoding="utf-8"))
    wrapper = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "get_user_assignment"
    )
    (call,) = _calls(wrapper, "find_user_assignment")
    flag = _keyword(call, "is_admin")
    assert isinstance(flag, ast.Name) and flag.id == "is_admin"
    assert "allow_admin_bypass" not in inspect.signature(find_user_assignment).parameters


@pytest.mark.parametrize("relative", sorted(_TRIGGER_CALLERS))
def test_every_manual_trigger_goes_through_the_one_gate(relative):
    """REST and both agent tools call the shared gate instead of re-spelling
    the admin / per-user / overlap rules (the copies drifted)."""
    used = _names_used_in(_APP / relative, _TRIGGER_CALLERS[relative])
    assert {
        "manual_trigger_refusal",
        "manual_trigger_overlap",
        "manual_trigger_run_scope",
        "manual_trigger_skipped",
    } <= used
    assert "resolve_hide_runs" not in used


@pytest.mark.parametrize("relative", sorted(_TRIGGER_CALLERS))
def test_every_manual_trigger_asks_the_engine_for_the_atomic_guard(relative):
    """The overlap check alone is check-then-act; each manual trigger then
    calls ``trigger_assignment(skip_if_active=True, active_run_user_id=...)``
    so the engine's advisory lock makes it atomic."""
    tree = ast.parse((_APP / relative).read_text(encoding="utf-8"))
    function = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == _TRIGGER_CALLERS[relative]
    )
    triggers = [
        c
        for c in ast.walk(function)
        if isinstance(c, ast.Call)
        and any(kw.arg == "triggered_by_user_id" for kw in c.keywords)
    ]
    assert len(triggers) == 1, relative
    skip = _keyword(triggers[0], "skip_if_active")
    assert isinstance(skip, ast.Constant) and skip.value is True
    assert _keyword(triggers[0], "active_run_user_id") is not None


def test_a_skipped_trigger_is_refused_like_the_overlap_check():
    uid = uuid.uuid4()
    assert helpers.manual_trigger_skipped(uid).reason == "own_run_active"
    assert helpers.manual_trigger_skipped(None).reason == "run_active"
    assert helpers.manual_trigger_run_scope(_assignment(hide_runs=True), _agent(), uid) == uid
    assert helpers.manual_trigger_run_scope(_assignment(), _agent(), uid) is None
