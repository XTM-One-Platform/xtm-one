"""A company-managed assignment run with no user keeps its tier.

A caller with no account gets the company-wide tier: company-managed rows
minus those narrowed to an audience. The run of a company-managed assignment
that has no user - a scheduled, webhook or integration-event trigger and no
"run as" user - is not such a caller: it is the organisation's own
automation, and it keeps every company-managed row, ``access_restricted``
ones included, exactly as before. The assignment engine records that
provenance from the run and assignment rows it executes; nothing a request
carries can set it.
"""

from __future__ import annotations

import ast
import inspect
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql
from starlette.datastructures import Headers

from app.api.v1 import helpers
from app.api.v1.helpers import (
    assignment_visible_filter,
    caller_visible_filter,
    company_managed_run_scoped,
    company_wide_visible_filter,
    in_company_managed_run,
    mark_company_managed_run,
    user_visible_filter,
)
from app.models.knowledge_base import KnowledgeBase
from app.models.runner import Runner
from tests import app_sources
from tests.agentic_gate_stub import stub_agentic_gate


@pytest.fixture(autouse=True)
def _agentic_ai_available(monkeypatch):
    """A run asks the background gate at its start; these tests are
    about what the run reads once agentic AI may run."""
    stub_agentic_gate(monkeypatch)


def _sql(clause) -> str:
    return str(clause.compile(dialect=postgresql.dialect())).lower()


def _render(clause) -> tuple[str, dict]:
    compiled = clause.compile(dialect=postgresql.dialect())
    return str(compiled), compiled.params


OWNERLESS_COMPANY_RUN = (
    SimpleNamespace(is_company_managed=True),
    SimpleNamespace(user_id=None),
)


async def _in_run(assignment, run, read):
    @company_managed_run_scoped
    async def _execute():
        marked = mark_company_managed_run(assignment, run)
        return marked, read()

    return await _execute()


@pytest.mark.asyncio
async def test_an_ownerless_company_managed_run_keeps_every_company_managed_row():
    marked, sql = await _in_run(
        *OWNERLESS_COMPANY_RUN, lambda: _sql(caller_visible_filter(Runner, None))
    )

    assert marked is True
    assert sql == "runners.is_company_managed is true"


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", [None, "anonymous", "system", ""])
async def test_every_no_account_shape_reads_the_run_tier_inside_the_run(user_id):
    _, sql = await _in_run(
        *OWNERLESS_COMPANY_RUN,
        lambda: _render(caller_visible_filter(KnowledgeBase, user_id, [])),
    )

    assert sql == _render(KnowledgeBase.is_company_managed.is_(True))


@pytest.mark.asyncio
async def test_a_signed_in_caller_keeps_its_own_tiers_inside_the_run():
    user_id = uuid.uuid4()
    groups = [str(uuid.uuid4())]

    _, rendered = await _in_run(
        *OWNERLESS_COMPANY_RUN,
        lambda: _render(caller_visible_filter(KnowledgeBase, user_id, groups)),
    )

    assert rendered == _render(user_visible_filter(KnowledgeBase, user_id, groups))


@pytest.mark.asyncio
async def test_the_assignment_lookup_stays_company_wide_inside_the_run():
    """``delegate_to_assignment`` read ``assignment_visible_filter(None)`` on
    main already: the run keeps that answer too."""
    _, rendered = await _in_run(
        *OWNERLESS_COMPANY_RUN, lambda: _render(assignment_visible_filter(None))
    )

    assert rendered == _render(assignment_visible_filter(None))
    assert "agents.access_restricted is not true" in rendered[0].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("assignment", "run"),
    [
        (SimpleNamespace(is_company_managed=True), SimpleNamespace(user_id=uuid.uuid4())),
        (SimpleNamespace(is_company_managed=False), SimpleNamespace(user_id=None)),
        (SimpleNamespace(is_company_managed=None), SimpleNamespace(user_id=None)),
        (SimpleNamespace(), SimpleNamespace(user_id=None)),
        (SimpleNamespace(is_company_managed=True), SimpleNamespace()),
    ],
    ids=["run-as-user", "personal", "unset-flag", "no-flag", "no-user-column"],
)
async def test_any_other_run_reads_like_a_caller_with_no_account(assignment, run):
    marked, rendered = await _in_run(
        assignment, run, lambda: _render(caller_visible_filter(Runner, None))
    )

    assert marked is False
    assert rendered == _render(company_wide_visible_filter(Runner))


def test_outside_a_run_the_mark_is_refused_and_nothing_widens():
    """An anonymous channel sender and the public widget never execute inside
    the engine's run scope, so there is nothing for them to mark."""
    assert mark_company_managed_run(*OWNERLESS_COMPANY_RUN) is False
    assert in_company_managed_run() is False
    assert _render(caller_visible_filter(Runner, "anonymous")) == _render(
        company_wide_visible_filter(Runner)
    )


@pytest.mark.asyncio
async def test_the_mark_ends_with_the_run_even_when_it_raises():
    @company_managed_run_scoped
    async def _failing_run():
        assert mark_company_managed_run(*OWNERLESS_COMPANY_RUN) is True
        raise RuntimeError("the run failed")

    with pytest.raises(RuntimeError):
        await _failing_run()

    assert helpers._NO_ACCOUNT_PROVENANCE.get() is None
    assert in_company_managed_run() is False


@pytest.mark.parametrize("reason", [helpers.MCP_GLOBAL_KEY, helpers.OWNERLESS_EVALUATION])
def test_the_platform_key_and_an_ownerless_evaluation_keep_mains_tier(reason):
    with helpers.keeping_company_managed_tier(reason):
        assert helpers.no_account_provenance() == reason
        assert _render(caller_visible_filter(Runner, "mcp-global")) == _render(
            Runner.is_company_managed.is_(True)
        )
        assert _render(caller_visible_filter(Runner, "anonymous")) == _render(
            Runner.is_company_managed.is_(True)
        )
        user_id = uuid.uuid4()
        assert _render(caller_visible_filter(Runner, str(user_id), [])) == _render(
            user_visible_filter(Runner, user_id, [])
        )
        assert "agents.access_restricted is not true" in _sql(assignment_visible_filter(None))
    assert helpers._NO_ACCOUNT_PROVENANCE.get() is None
    assert _render(caller_visible_filter(Runner, "mcp-global")) == _render(
        company_wide_visible_filter(Runner)
    )


@pytest.mark.parametrize("reason", ["", helpers.COMPANY_MANAGED_RUN, "anything"])
def test_only_the_platform_key_and_ownerless_evaluations_open_a_scope_by_hand(reason):
    """A company-managed run is only ever marked by the engine, from its rows."""
    with pytest.raises(ValueError), helpers.keeping_company_managed_tier(reason):
        pass
    assert helpers._NO_ACCOUNT_PROVENANCE.get() is None


def test_the_scope_ends_with_the_call_even_when_it_raises():
    with pytest.raises(RuntimeError), helpers.keeping_company_managed_tier(helpers.MCP_GLOBAL_KEY):
        raise RuntimeError("the call failed")
    assert helpers.no_account_provenance() is None


def test_the_engine_opens_the_scope_and_marks_the_run_it_loaded():
    from app.services import assignment_engine

    assert inspect.unwrap(assignment_engine.execute_assignment_run) is not (
        assignment_engine.execute_assignment_run
    )
    tree = ast.parse(inspect.getsource(assignment_engine.execute_assignment_run))
    fn = tree.body[0]
    assert any(
        isinstance(d, ast.Name) and d.id == "company_managed_run_scoped"
        for d in fn.decorator_list
    )
    marks = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "mark_company_managed_run"
    ]
    assert len(marks) == 1
    assert [a.id for a in marks[0].args] == ["assignment", "run"]


def _company_run_objects(*, run_as_user: uuid.UUID | None):
    from tests.test_assignment_stale_reference import _engine_objects

    run, assignment, agent = _engine_objects()
    agent.is_company_managed = True
    agent.user_id = None
    assignment.is_company_managed = True
    assignment.user_id = None
    assignment.run_as_user_id = run_as_user
    run.is_company_managed = True
    run.user_id = run_as_user
    return run, assignment, agent


def _observer(seen: dict):
    def _observe():
        seen["active"] = in_company_managed_run()
        seen["runners"] = _sql(caller_visible_filter(Runner, None))

    return _observe


@pytest.mark.asyncio
async def test_the_engine_marks_an_ownerless_scheduled_company_managed_run(monkeypatch):
    from tests.test_assignment_stale_reference import _drive_run

    seen: dict = {}
    await _drive_run(
        monkeypatch,
        report_integration_id=None,
        engine_objects=_company_run_objects(run_as_user=None),
        on_loop=_observer(seen),
    )

    assert seen == {"active": True, "runners": "runners.is_company_managed is true"}
    assert helpers._NO_ACCOUNT_PROVENANCE.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["run-as-user", "personal"])
async def test_the_engine_leaves_any_other_run_unmarked(monkeypatch, shape):
    from tests.test_assignment_stale_reference import _drive_run, _engine_objects

    objects = (
        _company_run_objects(run_as_user=uuid.uuid4())
        if shape == "run-as-user"
        else _engine_objects()
    )
    seen: dict = {}
    await _drive_run(
        monkeypatch,
        report_integration_id=None,
        engine_objects=objects,
        on_loop=_observer(seen),
    )

    assert seen["active"] is False
    assert seen["runners"] == _sql(company_wide_visible_filter(Runner))


# ---------------------------------------------------------------------------
# The global MCP endpoint's platform key and owner-less evaluations
# ---------------------------------------------------------------------------


def _mcp_gates(monkeypatch, seen: list):
    from app.api.v1 import mcp_endpoint

    async def _dispatch(body, agents, db, user_id):
        seen.append(helpers.no_account_provenance())
        return {"jsonrpc": "2.0", "id": body.id, "result": {}}

    async def _allowed(*_args, **_kwargs):
        return True

    monkeypatch.setattr("app.services.ai_state.is_ai_enabled", _allowed)
    monkeypatch.setattr("app.api.v1.platform.is_chat_licensed", _allowed)
    monkeypatch.setattr(mcp_endpoint, "_dispatch_mcp", _dispatch)
    return mcp_endpoint


class _Request:
    headers = Headers()

    async def json(self):
        return {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


async def _no_agents(*_args, **_kwargs):
    return []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("caller", "expected"),
    [("key", helpers.MCP_GLOBAL_KEY), ("oauth", None)],
)
async def test_only_the_platform_key_opens_the_scope_on_the_global_endpoint(
    monkeypatch, caller, expected
):
    seen: list = []
    mcp_endpoint = _mcp_gates(monkeypatch, seen)
    identity = mcp_endpoint.GLOBAL_KEY_CALLER if caller == "key" else str(uuid.uuid4())

    async def _authenticated(_request, _db):
        return SimpleNamespace(mcp_exposed_agent_ids=[]), identity

    monkeypatch.setattr(mcp_endpoint, "_authenticate_global_endpoint", _authenticated)
    monkeypatch.setattr(mcp_endpoint, "_get_global_agents", _no_agents)

    await mcp_endpoint.mcp_global_endpoint(_Request(), db=SimpleNamespace(commit=AsyncMock()))

    assert seen == [expected]
    assert helpers._NO_ACCOUNT_PROVENANCE.get() is None


@pytest.mark.asyncio
async def test_the_per_user_endpoint_never_opens_the_scope(monkeypatch):
    seen: list = []
    mcp_endpoint = _mcp_gates(monkeypatch, seen)

    async def _authenticated(_request, _db, _resource_path):
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(mcp_endpoint, "authenticate_user", _authenticated)
    monkeypatch.setattr(mcp_endpoint, "_get_user_agents", _no_agents)

    await mcp_endpoint.mcp_user_endpoint(_Request(), db=SimpleNamespace(commit=AsyncMock()))

    assert seen == [None]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("evaluation", "expected"),
    [
        (SimpleNamespace(id=uuid.uuid4(), user_id=None), helpers.OWNERLESS_EVALUATION),
        (SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4()), None),
        (None, None),
    ],
    ids=["ownerless", "owned", "no-row"],
)
async def test_only_an_ownerless_evaluation_row_opens_the_scope(monkeypatch, evaluation, expected):
    from app.services import evaluation_engine

    seen: list = []

    async def _chat(*_args, **_kwargs):
        seen.append(helpers.no_account_provenance())
        return "out", None

    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", _chat)

    await evaluation_engine._execute_agent_full_loop(
        None,
        SimpleNamespace(id=uuid.uuid4(), name="a"),
        "model-x",
        "prompt",
        evaluation=evaluation,
    )

    assert seen == [expected]
    assert helpers._NO_ACCOUNT_PROVENANCE.get() is None


_APP_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1] / "app"
# Who may establish that a read with no account keeps main's tier.
_PROVENANCE_WRITERS = {
    "keeping_company_managed_tier": {"api/v1/mcp_endpoint.py", "services/evaluation_engine.py"},
    "mark_company_managed_run": {"services/assignment_engine.py"},
    "company_managed_run_scoped": {"services/assignment_engine.py"},
    "_NO_ACCOUNT_PROVENANCE": set(),
}


def test_only_the_code_that_established_the_provenance_writes_it():
    """An anonymous channel sender, the public widget, ``/mcp/user`` or any
    other path must not be able to open the scope: no module but these calls
    the writers (``helpers.py`` defines them)."""
    found: dict[str, set[str]] = {name: set() for name in _PROVENANCE_WRITERS}
    for path in app_sources.python_files(_APP_ROOT):
        rel = path.relative_to(_APP_ROOT).as_posix()
        if rel == "api/v1/helpers.py":
            continue
        for node in app_sources.walk(app_sources.tree(path)):
            name = (
                node.id
                if isinstance(node, ast.Name)
                else node.attr
                if isinstance(node, ast.Attribute)
                else None
            )
            if name in found:
                found[name].add(rel)

    assert found == _PROVENANCE_WRITERS


