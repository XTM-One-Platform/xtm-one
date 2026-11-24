import asyncio
import uuid
from collections.abc import Hashable
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql import visitors

import app.api.v1.helpers as api_helpers
import app.core.database as database
import app.services.builtin_tools as builtin_tools_module
from app.models.agent import Agent
from app.tools.base import ToolCall

# Single canonical reference to the agent-builder factory through the
# module alias above. We deliberately do NOT add a separate
# ``from app.services.builtin_tools import build_agent_builder_tool``
# because the dual ``import`` / ``import from`` pattern triggers a CodeQL
# `py/multiple-import` finding — keep one binding so monkeypatches and
# direct invocations resolve through the same namespace.
build_agent_builder_tool = builtin_tools_module.build_agent_builder_tool


@pytest.fixture(autouse=True)
def update_audit(monkeypatch):
    """Records the ``update_agent`` audit rows: these fakes have no audit
    table and their agents no slug, so the rows are read back in
    ``tests/test_agent_builder_versions_postgres.py`` and
    ``tests/test_agent_builder_update_audit.py`` (#251)."""
    import app.services.audit as audit_module

    entries: list[dict] = []

    async def _record(_db, **kwargs):
        entries.append(kwargs)

    monkeypatch.setattr(audit_module, "log_audit", _record)
    return entries


@pytest.fixture(autouse=True)
def version_history(monkeypatch):
    """Records how the builder calls the version history: these fakes have no
    version tables, so the rows are read back in
    ``tests/test_agent_builder_versions_postgres.py`` and the calls asserted
    here (``.records`` / ``.seeds``, the keyword arguments of each call)."""
    import app.services.agent_config_versioning as versioning

    spy = SimpleNamespace(records=[], seeds=[])

    async def _record(_db, **kwargs):
        spy.records.append(kwargs)
        return None, {}

    async def _seed(_db, **kwargs):
        spy.seeds.append(kwargs)

    monkeypatch.setattr(versioning, "record_agent_version_changes", _record)
    monkeypatch.setattr(versioning, "seed_initial_agent_versions", _seed)
    monkeypatch.setattr(versioning, "versioned_agent_values", lambda _agent: {})
    return spy

# ---------------------------------------------------------------------------
# Permission-gate helpers
#
# The agent-builder write tools route every mutation through
# ``_load_writable_agent_for_builder`` / ``_load_writable_assignment_for_builder``
# so the LLM is held to the same gates as the REST API (built-in agents are
# read-only in XTM One mode, company-managed entities require admin, group-
# shared entities require ``config``-tier permission).
#
# Tests that focus on *internal mechanics* (parallel sessions, commit-failure
# rollbacks, friendly error responses, etc.) must not also exercise the gates
# — otherwise a permission-related regression would mask the mechanic under
# test. The helpers below stub the gates to "always allow" so the mechanic
# tests can keep using lightweight ``SimpleNamespace`` fixtures, while
# dedicated gate tests (see ``test_agent_builder_permission_gates.py``-style
# additions at the bottom of this file) cover the security contract end-to-end.
# ---------------------------------------------------------------------------


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        # Mirror SQLAlchemy's ``Result.scalars().first()`` for call sites that
        # explicitly tolerate duplicate rows (e.g. the ``create_knowledge_base``
        # pre-check, which would otherwise raise ``MultipleResultsFound`` if
        # the user already has multiple KBs with the same name — the model
        # intentionally allows that).
        return _ScalarsIterator(self._value)

    def all(self):
        # Row-tuple results, as used by the skill-package binding
        # reconciliation. These doubles carry no packaged skill resources, so
        # there are never rows to return.
        return []


class _ScalarsIterator:
    def __init__(self, value):
        self._value = value

    def first(self):
        return self._value

    def all(self):
        # Mirror SQLAlchemy's ``Result.scalars().all()`` for call sites
        # such as ``get_user_group_ids`` that materialise the full row
        # set. Several fake sessions use ``_ScalarResult(None)`` to mean
        # "no matching row" — and SQLAlchemy returns ``[]`` (no rows), not
        # ``[None]`` (one row with a NULL scalar) in that case, so we have
        # to translate ``None`` to ``[]`` to keep stub semantics aligned.
        if self._value is None:
            return []
        if isinstance(self._value, (list, tuple)):
            return list(self._value)
        return [self._value]


class _SharedSession:
    async def execute(self, _statement):
        raise AssertionError("update_agent should not use the shared session execute()")

    async def commit(self):
        raise AssertionError("update_agent should not use the shared session commit()")


class _DedicatedToolSession:
    def __init__(
        self,
        created_sessions,
        agent_name: str,
        *,
        fail_on_commit: bool = False,
    ):
        self._created_sessions = created_sessions
        self._created_sessions.append(self)
        # Real Agent rows always carry ``is_company_managed``; update_agent reads
        # it to decide whether to re-validate company-managed bindings, so the
        # stub must expose it (default private/False = no extra validation).
        # ``knowledge_base_permissions`` + the bound real-model prune mirror
        # production so update_agent's
        # ``prune_orphaned_knowledge_base_permissions()`` call (now run on any
        # knowledge_base_ids/permissions change) behaves exactly as it would on a
        # real Agent row.
        # ``skill_ids`` / ``custom_tool_ids`` / ``knowledge_base_ids`` are read by
        # the skill-package binding reconciliation update_agent runs whenever one
        # of them changes, so the stub must expose all three (empty = nothing
        # bound, hence nothing to reconcile).
        self.agent = SimpleNamespace(
            name=agent_name,
            is_company_managed=False,
            knowledge_base_permissions=None,
            skill_ids=[],
            custom_tool_ids=[],
            knowledge_base_ids=[],
            integration_bindings=None,
            integration_template_bindings=None,
            integration_allowed_tools=None,
            integration_element_bindings=None,
        )
        self.agent.prune_orphaned_knowledge_base_permissions = (
            lambda a=self.agent: Agent.prune_orphaned_knowledge_base_permissions(a)
        )
        # update_agent now also calls ``prune_orphaned_integration_scopes()``
        # whenever an integration binding changes (parity with REST). The real
        # method reads the ``active_integration_providers`` property, which a
        # SimpleNamespace can't expose; these doubles carry no element/tool
        # scopes, so the prune is a no-op here. Real prune behaviour is covered
        # by the dedicated tests that use a real Agent row.
        self.agent.prune_orphaned_integration_scopes = lambda: None
        self.fail_on_commit = fail_on_commit
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.executed_statements = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        self.closed = True
        return False

    async def execute(self, statement, parameters=None):
        self.executed_statements.append(statement)
        return _ScalarResult(self.agent)

    async def commit(self):
        if self.fail_on_commit:
            raise RuntimeError("simulated commit failure")
        await asyncio.sleep(0.01)
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class _DedicatedAssignmentSession:
    def __init__(
        self,
        created_sessions,
        assignments_by_id: dict[uuid.UUID, SimpleNamespace],
        agents_by_id: dict[uuid.UUID, SimpleNamespace] | None = None,
        *,
        fail_on_commit: bool = False,
        commit_error: BaseException | None = None,
    ):
        self._created_sessions = created_sessions
        self._created_sessions.append(self)
        self._assignments_by_id = assignments_by_id
        self._agents_by_id = agents_by_id or {}
        self.fail_on_commit = fail_on_commit
        # When ``fail_on_commit`` is set, raise this exception (defaults to a
        # generic, NON-retryable RuntimeError). Tests simulating a transient
        # deadlock pass a deadlock-classified error so the retry path engages.
        self._commit_error = commit_error
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.executed_statements = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        self.closed = True
        return False

    async def execute(self, statement, parameters=None):
        self.executed_statements.append(statement)
        if "FROM assignment_runs" in str(statement):
            # The live-run check of ``delete_assignment``: these
            # doubles hold no run, so the assignment is never busy.
            return _ScalarResult(None)
        bind_values = _extract_bind_values(statement)
        for value in bind_values:
            # The visibility predicate binds arrays (per-user and group shares).
            if not isinstance(value, Hashable):
                continue
            if value in self._assignments_by_id:
                return _ScalarResult(self._assignments_by_id[value])
            if value in self._agents_by_id:
                return _ScalarResult(self._agents_by_id[value])
        return _ScalarResult(None)

    async def commit(self):
        if self.fail_on_commit:
            raise self._commit_error or RuntimeError("simulated commit failure")
        await asyncio.sleep(0.01)
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    async def delete(self, instance):
        # ``_delete_agent`` / ``_delete_assignment`` call ``await _db.delete(obj)``
        # to mark the row for removal before the commit. We just record the
        # call so the assertions can verify it happened — the failure case
        # then trips on the subsequent ``await _db.commit()`` raising.
        self.executed_statements.append(("delete", instance))

    async def get(self, _model, pk, **_kw):
        # The agent deletion re-reads the agent once it holds the lock.
        return self._agents_by_id.get(pk)

    def add(self, _instance):
        # The agent deletion's audit entry.
        return None

    async def flush(self):
        return None


class _CreateKBDedicatedSession:
    """Fake async session feeder for ``_create_knowledge_base`` tests.

    Lets each regression test pre-bake the sequence of `execute` results
    (``existing`` rows for the pre-check, then the optional re-check
    after an `IntegrityError`) and decide whether `commit()` should
    succeed, raise `IntegrityError`, or raise some other exception so
    we can exercise every branch of the hardening logic.

    ``execute()`` records every statement it receives in
    ``executed_statements`` so tests can use :func:`_extract_bind_values`
    to assert the WHERE clause actually filters by ``user_id`` and
    ``name`` — without that, a regression that silently drops the
    ``user_id`` filter (and returns another user's KB) would still pass.
    """

    def __init__(
        self,
        created_sessions,
        *,
        execute_results: list[object],
        fail_on_commit: bool = False,
        commit_exception: Exception | None = None,
    ):
        self._created_sessions = created_sessions
        self._created_sessions.append(self)
        self._execute_results = execute_results
        self.fail_on_commit = fail_on_commit
        self.commit_exception = commit_exception
        self.commits = 0
        self.rollbacks = 0
        self.added: list[object] = []
        self.closed = False
        self.executed_statements: list[object] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        self.closed = True
        return False

    async def execute(self, statement):
        self.executed_statements.append(statement)
        value = self._execute_results.pop(0) if self._execute_results else None
        return _ScalarResult(value)

    def add(self, instance):
        self.added.append(instance)

    async def commit(self):
        if self.commit_exception is not None:
            raise self.commit_exception
        if self.fail_on_commit:
            raise IntegrityError("insert", {}, Exception("duplicate key"))
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def _assert_kb_lookup_filters_by_user_and_name(
    statement, *, expected_user_id: uuid.UUID, expected_name: str
) -> None:
    """Assert a recorded `_CreateKBDedicatedSession.execute` statement
    filters the KB lookup by *both* ``user_id`` and ``name``.

    Without this check, a regression that dropped the ``user_id``
    predicate (and accidentally returned another user's KB on a
    name collision) would still pass the existing tests because the
    fake session blindly returns the next canned result.
    """
    bind_values = _extract_bind_values(statement)
    assert expected_user_id in bind_values, (
        f"KB lookup statement is missing the user_id={expected_user_id} "
        f"predicate; observed bind values: {bind_values}"
    )
    assert expected_name in bind_values, (
        f"KB lookup statement is missing the name={expected_name!r} "
        f"predicate; observed bind values: {bind_values}"
    )


def _assert_kb_lookup_is_deterministic(statement) -> None:
    """Assert a recorded KB lookup statement applies a deterministic
    ``ORDER BY created_at, id`` plus ``LIMIT 1``.

    Without this, ``.first()`` on an unordered query returns a
    non-deterministic row when the user has multiple KBs with the same
    ``(user_id, name)`` — which the schema explicitly allows — and the
    LLM can be steered to different KB IDs across calls. The fix at the
    call sites adds ``order_by(KnowledgeBase.created_at,
    KnowledgeBase.id).limit(1)``, and this helper pins it down so a
    regression to the unordered query would fail loudly.

    Inspects the **compiled** SQL (public SQLAlchemy API) rather than
    private underscored attributes (``_order_by_clauses`` / ``_limit``)
    so the assertion stays stable across SQLAlchemy version upgrades.
    """
    compiled_sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
    lower_sql = compiled_sql.lower()

    assert "limit 1" in lower_sql, (
        f"KB lookup statement must apply LIMIT 1 to avoid scanning all "
        f"duplicate rows; compiled SQL: {compiled_sql}"
    )

    assert "order by" in lower_sql, (
        f"KB lookup statement is missing an ORDER BY clause; "
        f"compiled SQL: {compiled_sql}"
    )
    order_by_section = lower_sql.split("order by", 1)[1].split("limit", 1)[0]
    assert "knowledge_bases.created_at" in order_by_section, (
        f"KB lookup statement is missing the ``created_at`` ORDER BY clause; "
        f"compiled SQL: {compiled_sql}"
    )
    assert "knowledge_bases.id" in order_by_section, (
        f"KB lookup statement is missing the ``id`` ORDER BY tie-breaker; "
        f"compiled SQL: {compiled_sql}"
    )


def _extract_bind_values(statement) -> list[object]:
    values: list[object] = []

    visitors.traverse(
        statement,
        {},
        {"bindparam": lambda bind_param: values.append(bind_param.value)},
    )
    return values


async def _writable_agent_from_session(_db, _uid, _agent_uuid, *, action="modify"):
    """Default writable-agent gate stub used by mechanic tests.

    Returns the dedicated session's ``.agent`` attribute so each parallel
    call gets back its own SimpleNamespace fixture without having to thread
    a dict of agents through the test setup. Records each invocation on the
    session so mechanic tests can still verify the gate was consulted with
    the expected ``(uid, agent_uuid, action)`` triple — preserving the
    spirit of the previous bind-value assertions even though the security
    check moved from a SQL WHERE clause into the helper function.
    """
    calls = getattr(_db, "writable_agent_calls", None)
    if calls is None:
        calls = []
        _db.writable_agent_calls = calls
    calls.append({"uid": _uid, "agent_uuid": _agent_uuid, "action": action})
    return _db.agent, None


async def _writable_assignment_from_session(
    _db, _uid, assignment_uuid, *, action="modify"
):
    """Default writable-assignment gate stub used by mechanic tests."""
    assignment = _db._assignments_by_id.get(assignment_uuid)
    if assignment is None:
        return None, None, f"Assignment `{assignment_uuid}` not found."
    agents_map = getattr(_db, "_agents_by_id", None) or {}
    agent = agents_map.get(getattr(assignment, "agent_id", None))
    return assignment, agent, None


@pytest.mark.asyncio
async def test_update_agent_uses_dedicated_session_for_parallel_calls(monkeypatch):
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    created_sessions = []

    def _session_factory():
        return _DedicatedToolSession(created_sessions, "Original Agent")

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(
        builtin_tools_module,
        "_load_writable_agent_for_builder",
        _writable_agent_from_session,
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)

    call_specs = [(str(uuid.uuid4()), f"Agent {i}") for i in range(4)]
    calls = [
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_agent",
            arguments={"agent_id": call_agent_id, "name": call_name},
        )
        for call_agent_id, call_name in call_specs
    ]

    results = await asyncio.gather(*(executor.execute_tool(call) for call in calls))

    assert len(results) == 4
    assert all(not result.is_error for result in results)
    assert {result.content for result in results} == {
        f"Agent **Agent {i}** updated: name." for i in range(4)
    }
    assert len(created_sessions) == 4
    assert sum(session.commits for session in created_sessions) == 4
    assert all(session.closed for session in created_sessions)
    assert {session.agent.name for session in created_sessions} == {
        f"Agent {i}" for i in range(4)
    }
    # Verify the permission gate was consulted exactly once per call with the
    # caller's user_id and the targeted agent_id. The security check moved
    # from a ``WHERE Agent.user_id = ?`` clause into the dedicated helper
    # (``_load_writable_agent_for_builder``), but the contract is the same:
    # every mutation must run through the gate with the calling user's id
    # and the requested agent id — a regression that dropped either input
    # would re-open the bypass the previous WHERE clause closed.
    expected_agent_ids = {uuid.UUID(agent_id) for agent_id, _ in call_specs}
    observed_agent_ids = set()
    observed_user_ids = set()
    observed_actions = set()
    for session in created_sessions:
        gate_calls = session.writable_agent_calls
        assert len(gate_calls) == 1, (
            "the update tool must check the write gate exactly once per call"
        )
        observed_agent_ids.add(gate_calls[0]["agent_uuid"])
        observed_user_ids.add(gate_calls[0]["uid"])
        observed_actions.add(gate_calls[0]["action"])
    assert observed_agent_ids == expected_agent_ids
    assert observed_user_ids == {uuid.UUID(user_id)}
    assert observed_actions == {"edit"}


@pytest.mark.asyncio
async def test_update_agent_returns_friendly_error_when_commit_fails(monkeypatch):
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    agent_id = str(uuid.uuid4())
    created_sessions = []

    def _session_factory():
        return _DedicatedToolSession(
            created_sessions,
            "Will Fail",
            fail_on_commit=True,
        )

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(
        builtin_tools_module,
        "_load_writable_agent_for_builder",
        _writable_agent_from_session,
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_agent",
            arguments={"agent_id": agent_id, "name": "New Name"},
        )
    )

    assert result.is_error
    assert (
        result.content == f"Failed to update agent `{agent_id}`. Please try again."
    )
    assert len(created_sessions) == 1
    assert created_sessions[0].commits == 0
    assert created_sessions[0].rollbacks == 1
    assert created_sessions[0].closed


@pytest.mark.asyncio
async def test_update_agent_no_fields_returns_without_commit(monkeypatch):
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    agent_id = str(uuid.uuid4())
    created_sessions = []

    def _session_factory():
        return _DedicatedToolSession(created_sessions, "Noop Agent")

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(
        builtin_tools_module,
        "_load_writable_agent_for_builder",
        _writable_agent_from_session,
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_agent",
            arguments={"agent_id": agent_id},
        )
    )

    assert not result.is_error
    assert result.content == "No fields to update. Provide at least one field."
    assert len(created_sessions) == 1
    assert created_sessions[0].commits == 0
    assert created_sessions[0].rollbacks == 0
    assert created_sessions[0].closed


@pytest.mark.asyncio
async def test_update_agent_rejects_malformed_integration_bindings(monkeypatch):
    """A malformed ``integration_bindings`` entry (missing ``:``) must be
    rejected with a clear error and must NOT overwrite the agent's existing
    bindings — a formatting slip should never silently wipe them.
    """
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    agent_id = str(uuid.uuid4())
    created_sessions = []
    existing = {"github": ["11111111-1111-1111-1111-111111111111"]}

    def _session_factory():
        session = _DedicatedToolSession(created_sessions, "Bindings Agent")
        session.agent.integration_bindings = dict(existing)
        return session

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(
        builtin_tools_module,
        "_load_writable_agent_for_builder",
        _writable_agent_from_session,
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_agent",
            arguments={
                "agent_id": agent_id,
                "integration_bindings": "github-without-a-colon",
            },
        )
    )

    assert not result.is_error
    assert "Invalid integration_bindings" in result.content
    # Existing bindings untouched and nothing committed.
    assert created_sessions[0].agent.integration_bindings == existing
    assert created_sessions[0].commits == 0


@pytest.mark.asyncio
async def test_update_agent_clears_bindings_with_sentinel(monkeypatch):
    """``none``/``clear`` sentinels must empty a resource-binding list and the
    integration map (an empty string means "leave unchanged", so clearing
    needs an explicit sentinel).
    """
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    agent_id = str(uuid.uuid4())
    created_sessions = []

    def _session_factory():
        session = _DedicatedToolSession(created_sessions, "Clear Agent")
        session.agent.knowledge_base_ids = ["kb-1", "kb-2"]
        session.agent.integration_bindings = {"github": ["x"]}
        return session

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(
        builtin_tools_module,
        "_load_writable_agent_for_builder",
        _writable_agent_from_session,
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_agent",
            arguments={
                "agent_id": agent_id,
                "knowledge_base_ids": "none",
                "integration_bindings": "clear",
            },
        )
    )

    assert not result.is_error
    assert created_sessions[0].agent.knowledge_base_ids == []
    assert created_sessions[0].agent.integration_bindings == {}
    assert created_sessions[0].commits == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changes_kwargs", "expected", "unexpected"),
    [
        # Plural agreement on the derived-resource report.
        (
            {"added_custom_tool_ids": ["t1", "t2"], "added_knowledge_base_ids": ["k1"]},
            ["+1 KB", "+2 custom tools"],
            ["custom tool,", "2 custom tool."],
        ),
        (
            {"added_custom_tool_ids": ["t1"]},
            ["+1 custom tool."],
            ["custom tools"],
        ),
        # A skip changes nothing, so it must still be reported on its own —
        # silently dropping a skill's KB is the surprise worth telling about.
        (
            {"skipped_knowledge_base_ids": ["k1", "k2"]},
            ["Not bound: 2 packaged KBs", "company-managed"],
            ["followed their skills"],
        ),
    ],
)
async def test_update_agent_reports_skill_package_resource_changes(
    monkeypatch, changes_kwargs, expected, unexpected
):
    """The tool result must tell the caller what the skill bindings dragged along."""
    from app.services import skill_package_bindings as skill_package_bindings_module
    from app.services.skill_package_bindings import SkillPackageBindingChanges

    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    agent_id = str(uuid.uuid4())
    skill_id = str(uuid.uuid4())
    created_sessions = []

    monkeypatch.setattr(
        database,
        "async_session",
        lambda: _DedicatedToolSession(created_sessions, "Packaged Skill Agent"),
    )
    monkeypatch.setattr(
        builtin_tools_module,
        "_load_writable_agent_for_builder",
        _writable_agent_from_session,
    )

    async def _fake_reconcile(_db, _agent):
        return SkillPackageBindingChanges(**changes_kwargs)

    monkeypatch.setattr(
        skill_package_bindings_module,
        "reconcile_agent_skill_package_bindings",
        _fake_reconcile,
    )
    # The fake session cannot resolve the skill for the personal-binding
    # visibility gate; that gate has its own dedicated tests.
    from app.services import agent_bindings

    async def _skip_visibility_gate(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        agent_bindings, "validate_personal_agent_bindings", _skip_visibility_gate
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_agent",
            arguments={"agent_id": agent_id, "skill_ids": skill_id},
        )
    )

    assert not result.is_error
    for fragment in expected:
        assert fragment in result.content
    for fragment in unexpected:
        assert fragment not in result.content


@pytest.mark.asyncio
async def test_update_agent_rejects_non_uuid_binding_ids(monkeypatch):
    """Resource-binding ids must be validated as UUIDs before they are
    persisted, otherwise a non-UUID later blows up ``uuid.UUID(id_str)`` in
    REST helpers / the runtime.  A bad id is rejected without mutating or
    committing.
    """
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    agent_id = str(uuid.uuid4())
    created_sessions = []
    existing = ["11111111-1111-1111-1111-111111111111"]

    def _session_factory():
        session = _DedicatedToolSession(created_sessions, "Bindings Agent")
        session.agent.knowledge_base_ids = list(existing)
        return session

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(
        builtin_tools_module,
        "_load_writable_agent_for_builder",
        _writable_agent_from_session,
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_agent",
            arguments={"agent_id": agent_id, "knowledge_base_ids": "not-a-uuid"},
        )
    )

    assert not result.is_error
    assert "Invalid knowledge_base_ids" in result.content
    # Existing binding untouched and nothing committed.
    assert created_sessions[0].agent.knowledge_base_ids == existing
    assert created_sessions[0].commits == 0


@pytest.mark.asyncio
async def test_update_agent_rejects_non_uuid_integration_id(monkeypatch):
    """The integration_id half of an integration binding must be a valid UUID;
    a non-UUID is treated as malformed and rejected before mutating.
    """
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    agent_id = str(uuid.uuid4())
    created_sessions = []
    existing = {"github": ["11111111-1111-1111-1111-111111111111"]}

    def _session_factory():
        session = _DedicatedToolSession(created_sessions, "Bindings Agent")
        session.agent.integration_bindings = dict(existing)
        return session

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(
        builtin_tools_module,
        "_load_writable_agent_for_builder",
        _writable_agent_from_session,
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_agent",
            arguments={"agent_id": agent_id, "integration_bindings": "github:not-a-uuid"},
        )
    )

    assert not result.is_error
    assert "Invalid integration_bindings" in result.content
    assert created_sessions[0].agent.integration_bindings == existing
    assert created_sessions[0].commits == 0


@pytest.mark.asyncio
async def test_update_agent_canonicalises_integration_binding_provider_key(monkeypatch):
    """The provider key of an integration binding must be canonicalised to
    lower-case: downstream tool loading compares the key to
    ``Integration.provider`` (a lower-case catalog key) with an exact match, so
    a value like "GitHub:<uuid>" would otherwise persist a dead binding that
    never resolves.
    """
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    agent_id = str(uuid.uuid4())
    created_sessions = []
    integ_id = "11111111-1111-1111-1111-111111111111"

    def _session_factory():
        session = _DedicatedToolSession(created_sessions, "Bindings Agent")
        session.agent.integration_bindings = {}
        return session

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(
        builtin_tools_module,
        "_load_writable_agent_for_builder",
        _writable_agent_from_session,
    )
    # This test targets provider-key canonicalisation, not binding visibility.
    # Stub the REST visibility validator (it would query the shared session
    # double, which always returns the agent fixture) so the parse/normalize
    # path is exercised in isolation.
    import app.services.agent_bindings as _agent_bindings_module

    async def _ok_personal_bindings(*_a, **_k):
        return None

    monkeypatch.setattr(
        _agent_bindings_module, "validate_personal_agent_bindings", _ok_personal_bindings
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_agent",
            arguments={"agent_id": agent_id, "integration_bindings": f"GitHub:{integ_id}"},
        )
    )

    assert not result.is_error
    # Stored under the canonical lower-case provider key, not "GitHub".
    assert created_sessions[0].agent.integration_bindings == {"github": [integ_id]}
    assert created_sessions[0].commits == 1


@pytest.mark.asyncio
async def test_update_agent_output_format_is_stripped(monkeypatch, version_history):
    """A padded ``output_format`` ("  markdown  ") — a common LLM/UI formatting
    glitch — must be accepted and stored canonicalised, not rejected as invalid.
    """
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    agent_id = str(uuid.uuid4())
    created_sessions = []

    def _session_factory():
        return _DedicatedToolSession(created_sessions, "Fmt Agent")

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(
        builtin_tools_module,
        "_load_writable_agent_for_builder",
        _writable_agent_from_session,
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_agent",
            arguments={"agent_id": agent_id, "output_format": "  markdown  "},
        )
    )

    assert not result.is_error
    assert created_sessions[0].agent.output_format == "markdown"  # stripped
    assert created_sessions[0].commits == 1
    # Every value the write changed is versioned (no display-label filter),
    # with the agent builder named as where the edit came from.
    (record,) = version_history.records
    assert set(record) == {"agent", "before", "author_id", "commit_message", "record_baseline"}
    assert record["agent"] is created_sessions[0].agent
    assert record["commit_message"] == "Edited through the agent builder"
    assert record["record_baseline"] is True


@pytest.mark.asyncio
async def test_update_agent_whitespace_output_format_leaves_unchanged(monkeypatch):
    """A whitespace-only ``output_format`` must be a no-op (leave unchanged),
    not a rejected-as-invalid error and not a stored blank."""
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    agent_id = str(uuid.uuid4())
    created_sessions = []

    def _session_factory():
        return _DedicatedToolSession(created_sessions, "Fmt Agent")

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(
        builtin_tools_module,
        "_load_writable_agent_for_builder",
        _writable_agent_from_session,
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_agent",
            arguments={"agent_id": agent_id, "output_format": "   "},
        )
    )

    assert not result.is_error
    assert result.content == "No fields to update. Provide at least one field."
    assert created_sessions[0].commits == 0


@pytest.mark.asyncio
async def test_update_agent_whitespace_binding_args_leave_unchanged(monkeypatch):
    """Whitespace-only binding args must be treated as "leave unchanged" — never
    silently cleared. Only the explicit ``none``/``clear`` sentinel clears, so a
    stray whitespace value from an LLM/UI glitch can't wipe a binding.
    """
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    agent_id = str(uuid.uuid4())
    created_sessions = []
    kb_ids = ["11111111-1111-1111-1111-111111111111"]
    integ = {"github": ["22222222-2222-2222-2222-222222222222"]}

    def _session_factory():
        session = _DedicatedToolSession(created_sessions, "Whitespace Agent")
        session.agent.knowledge_base_ids = list(kb_ids)
        session.agent.integration_bindings = dict(integ)
        return session

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(
        builtin_tools_module,
        "_load_writable_agent_for_builder",
        _writable_agent_from_session,
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_agent",
            arguments={
                "agent_id": agent_id,
                "knowledge_base_ids": "   ",
                "integration_bindings": "  ",
            },
        )
    )

    assert not result.is_error
    # Whitespace = leave unchanged → no fields updated, nothing committed,
    # existing bindings untouched.
    assert result.content == "No fields to update. Provide at least one field."
    assert created_sessions[0].agent.knowledge_base_ids == kb_ids
    assert created_sessions[0].agent.integration_bindings == integ
    assert created_sessions[0].commits == 0


@pytest.mark.asyncio
async def test_update_agent_separator_only_binding_arg_is_rejected(monkeypatch):
    """A non-empty, non-sentinel id list that parses to zero UUIDs (e.g. ``","``
    or ``", ,"``) must be rejected — clearing requires the explicit
    ``none``/``clear`` sentinel, so stray separators can't silently wipe a
    binding. Nothing is mutated or committed.
    """
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    agent_id = str(uuid.uuid4())
    created_sessions = []
    kb_ids = ["11111111-1111-1111-1111-111111111111"]

    def _session_factory():
        session = _DedicatedToolSession(created_sessions, "Separator Agent")
        session.agent.knowledge_base_ids = list(kb_ids)
        return session

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(
        builtin_tools_module,
        "_load_writable_agent_for_builder",
        _writable_agent_from_session,
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_agent",
            arguments={"agent_id": agent_id, "knowledge_base_ids": ", ,"},
        )
    )

    assert not result.is_error
    assert "No valid knowledge_base_ids" in result.content
    # Existing binding untouched and nothing committed (no silent wipe).
    assert created_sessions[0].agent.knowledge_base_ids == kb_ids
    assert created_sessions[0].commits == 0


@pytest.mark.asyncio
async def test_update_agent_separator_only_integration_bindings_is_rejected(
    monkeypatch,
):
    """A non-empty, non-sentinel ``integration_bindings`` that parses to zero
    pairs (e.g. ``","``) must be rejected rather than silently wiping the map —
    clearing requires the explicit ``none``/``clear`` sentinel.
    """
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    agent_id = str(uuid.uuid4())
    created_sessions = []
    existing = {"github": ["11111111-1111-1111-1111-111111111111"]}

    def _session_factory():
        session = _DedicatedToolSession(created_sessions, "Separator Agent")
        session.agent.integration_bindings = dict(existing)
        return session

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(
        builtin_tools_module,
        "_load_writable_agent_for_builder",
        _writable_agent_from_session,
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_agent",
            arguments={"agent_id": agent_id, "integration_bindings": ", ,"},
        )
    )

    assert not result.is_error
    assert "No valid integration_bindings" in result.content
    # Existing bindings untouched and nothing committed.
    assert created_sessions[0].agent.integration_bindings == existing
    assert created_sessions[0].commits == 0


@pytest.mark.asyncio
async def test_update_agent_whitespace_llm_model_leaves_unchanged(monkeypatch):
    """A whitespace-only ``llm_model`` must be treated as "leave unchanged" — not
    persisted as the agent's model override (a bogus non-empty model would break
    downstream LLM routing). Clearing still requires the explicit ``none``
    sentinel.
    """
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    agent_id = str(uuid.uuid4())
    created_sessions = []

    def _session_factory():
        session = _DedicatedToolSession(created_sessions, "Model Agent")
        session.agent.llm_model = "gpt-4o"
        return session

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(
        builtin_tools_module,
        "_load_writable_agent_for_builder",
        _writable_agent_from_session,
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_agent",
            arguments={"agent_id": agent_id, "llm_model": "   "},
        )
    )

    assert not result.is_error
    assert result.content == "No fields to update. Provide at least one field."
    assert created_sessions[0].agent.llm_model == "gpt-4o"  # untouched
    assert created_sessions[0].commits == 0


async def _model_gate_allow(_db, _model, **_kwargs):
    """Stub for ``_validate_selectable_chat_model_for_builder`` — allow.

    Mechanic-focused tests stub the model-catalog gate to "always valid"
    so they keep using lightweight fake sessions; the gate itself has
    dedicated tests below and in ``test_model_selection_validation.py``.
    """
    return None


@pytest.mark.asyncio
async def test_update_agent_llm_model_is_canonicalised(monkeypatch):
    """A real ``llm_model`` value is stripped before persisting, so a padded
    value can't store an untrimmed model id that breaks routing; ``none`` still
    clears the override.
    """
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    agent_id = str(uuid.uuid4())
    created_sessions = []

    def _session_factory():
        session = _DedicatedToolSession(created_sessions, "Model Agent")
        session.agent.llm_model = None
        return session

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(
        builtin_tools_module,
        "_load_writable_agent_for_builder",
        _writable_agent_from_session,
    )
    monkeypatch.setattr(
        builtin_tools_module,
        "_validate_selectable_chat_model_for_builder",
        _model_gate_allow,
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_agent",
            arguments={"agent_id": agent_id, "llm_model": "  gpt-4o  "},
        )
    )

    assert not result.is_error
    assert "llm_model" in result.content
    assert created_sessions[0].agent.llm_model == "gpt-4o"  # stripped
    assert created_sessions[0].commits == 1


@pytest.mark.asyncio
async def test_update_agent_rejects_model_not_in_available_models(monkeypatch):
    """An ``llm_model`` outside the configured providers' curated
    ``available_models`` catalogs must be rejected with the gate's error
    message and must NOT mutate or commit the agent.

    The builder runs with LLM-supplied input, so a hallucinated or retired
    model id (e.g. a stale dated Anthropic snapshot) is a live risk:
    persisting one makes every subsequent call on the agent fail with a
    provider 404.
    """
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    agent_id = str(uuid.uuid4())
    created_sessions = []

    def _session_factory():
        session = _DedicatedToolSession(created_sessions, "Model Agent")
        session.agent.llm_model = None
        return session

    async def _model_gate_reject(_db, model, **_kwargs):
        return (
            f"Model '{model}' is not in any configured provider's available "
            "models, so it cannot be selected. Available models: "
            "claude-sonnet-4-6, claude-haiku-4-5-20251001"
        )

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(
        builtin_tools_module,
        "_load_writable_agent_for_builder",
        _writable_agent_from_session,
    )
    monkeypatch.setattr(
        builtin_tools_module,
        "_validate_selectable_chat_model_for_builder",
        _model_gate_reject,
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_agent",
            arguments={
                "agent_id": agent_id,
                "llm_model": "claude-sonnet-4-20250514",
            },
        )
    )

    assert "not in any configured provider's available models" in result.content
    # The valid catalog is surfaced so the LLM can self-correct.
    assert "claude-sonnet-4-6" in result.content
    assert created_sessions[0].agent.llm_model is None, (
        "a rejected model id must never be persisted on the agent"
    )
    assert created_sessions[0].commits == 0, (
        "a rejected update must not commit"
    )


@pytest.mark.asyncio
async def test_update_agent_none_sentinel_bypasses_model_gate(monkeypatch):
    """The explicit ``none`` sentinel clears the override and must NOT be
    routed through the model-catalog gate (it is not a model id)."""
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    agent_id = str(uuid.uuid4())
    created_sessions = []

    def _session_factory():
        session = _DedicatedToolSession(created_sessions, "Model Agent")
        session.agent.llm_model = "claude-sonnet-4-6"
        return session

    async def _exploding_gate(_db, model, **_kwargs):
        raise AssertionError(
            f"model gate must not be invoked for the 'none' sentinel, got {model!r}"
        )

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(
        builtin_tools_module,
        "_load_writable_agent_for_builder",
        _writable_agent_from_session,
    )
    monkeypatch.setattr(
        builtin_tools_module,
        "_validate_selectable_chat_model_for_builder",
        _exploding_gate,
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_agent",
            arguments={"agent_id": agent_id, "llm_model": "none"},
        )
    )

    assert not result.is_error
    assert created_sessions[0].agent.llm_model is None
    assert created_sessions[0].commits == 1


@pytest.mark.asyncio
async def test_update_agent_handles_null_freetext_and_binding_args(monkeypatch):
    """Tool-call args are forwarded without coercion, so a provider can send
    JSON ``null`` for any string field. ``null`` output_format / llm_model /
    integration_bindings / binding id-lists must be treated as "leave
    unchanged" — never raise AttributeError from ``None.strip()``. With no
    other field it's a clean no-op.
    """
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    agent_id = str(uuid.uuid4())
    created_sessions = []
    kb_ids = ["11111111-1111-1111-1111-111111111111"]
    integ = {"github": ["22222222-2222-2222-2222-222222222222"]}

    def _session_factory():
        session = _DedicatedToolSession(created_sessions, "Null Agent")
        session.agent.llm_model = "gpt-4o"
        session.agent.output_format = "markdown"
        session.agent.knowledge_base_ids = list(kb_ids)
        session.agent.integration_bindings = dict(integ)
        return session

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(
        builtin_tools_module,
        "_load_writable_agent_for_builder",
        _writable_agent_from_session,
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_agent",
            arguments={
                "agent_id": agent_id,
                "output_format": None,
                "llm_model": None,
                "integration_bindings": None,
                "knowledge_base_ids": None,
            },
        )
    )

    assert not result.is_error
    assert result.content == "No fields to update. Provide at least one field."
    # Everything untouched — null is "leave unchanged", never a wipe.
    assert created_sessions[0].agent.llm_model == "gpt-4o"
    assert created_sessions[0].agent.output_format == "markdown"
    assert created_sessions[0].agent.knowledge_base_ids == kb_ids
    assert created_sessions[0].agent.integration_bindings == integ
    assert created_sessions[0].commits == 0


@pytest.mark.asyncio
async def test_update_agent_sets_and_clears_max_iterations(monkeypatch):
    """update_agent surfaces max_iterations: a number sets the per-agent cap
    (clamped to the ceiling), and 'none'/'default'/'inherit' clears it back to
    NULL (= platform default). Each call uses a fresh dedicated session whose
    agent starts at 50, so the set and clear paths are both exercised."""
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    agent_id = str(uuid.uuid4())
    created_sessions = []

    def _session_factory():
        session = _DedicatedToolSession(created_sessions, "Iter Agent")
        session.agent.max_iterations = 50
        return session

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(
        builtin_tools_module,
        "_load_writable_agent_for_builder",
        _writable_agent_from_session,
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)

    set_result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_agent",
            arguments={"agent_id": agent_id, "max_iterations": "20"},
        )
    )
    assert not set_result.is_error
    assert created_sessions[0].agent.max_iterations == 20
    assert created_sessions[0].commits == 1

    clear_result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_agent",
            arguments={"agent_id": agent_id, "max_iterations": "none"},
        )
    )
    assert not clear_result.is_error
    assert created_sessions[1].agent.max_iterations is None
    assert created_sessions[1].commits == 1


@pytest.mark.asyncio
async def test_update_agent_accepts_native_int_max_iterations(monkeypatch):
    """Regression: the tool schema allows max_iterations as a native
    JSON integer and tool kwargs are forwarded verbatim, so a caller may pass
    ``{"max_iterations": 20}``. A native int must set the per-agent cap instead
    of raising AttributeError on the str-only ``.strip()`` path (mirrors
    update_assignment, which already handles int)."""
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    agent_id = str(uuid.uuid4())
    created_sessions = []

    def _session_factory():
        session = _DedicatedToolSession(created_sessions, "Iter Agent")
        session.agent.max_iterations = 50
        return session

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(
        builtin_tools_module,
        "_load_writable_agent_for_builder",
        _writable_agent_from_session,
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)

    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_agent",
            arguments={"agent_id": agent_id, "max_iterations": 20},  # native int
        )
    )
    assert not result.is_error
    assert created_sessions[0].agent.max_iterations == 20
    assert created_sessions[0].commits == 1


@pytest.mark.asyncio
async def test_update_agent_rejects_out_of_range_max_iterations(monkeypatch):
    """An out-of-range max_iterations is rejected with an actionable error and
    never committed."""
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    agent_id = str(uuid.uuid4())
    created_sessions = []

    def _session_factory():
        session = _DedicatedToolSession(created_sessions, "Iter Agent")
        session.agent.max_iterations = 30
        return session

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(
        builtin_tools_module,
        "_load_writable_agent_for_builder",
        _writable_agent_from_session,
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_agent",
            arguments={"agent_id": agent_id, "max_iterations": "0"},
        )
    )

    assert "max_iterations" in result.content
    assert created_sessions[0].agent.max_iterations == 30
    assert created_sessions[0].commits == 0


@pytest.mark.asyncio
async def test_add_assignment_invalid_cron_returns_single_prefixed_error(monkeypatch):
    """Regression: ``_add_assignment_to_agent`` must NOT double-prefix the
    cron error message.

    ``validate_cron_expression`` already raises
    ``ValueError("Invalid cron expression '...': ...")``. The previous
    return path wrapped it in another ``f"Invalid cron expression: {cron_err}"``
    so users saw ``Invalid cron expression: Invalid cron expression '...': ...``.
    The handler now surfaces ``str(cron_err)`` verbatim — this test pins
    that contract so a future caller that re-adds the prefix trips
    immediately.
    """
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    uid = uuid.UUID(user_id)
    agent_id = str(uuid.uuid4())
    created_sessions: list[_DedicatedToolSession] = []

    def _session_factory():
        s = _DedicatedToolSession(created_sessions, "Schedule Agent")
        # The handler accesses ``agent.user_id`` and ``agent.id`` later, but
        # the early-return on the bad cron fires before that — patch the
        # SimpleNamespace anyway so a regression that skips the early
        # return surfaces as a clean AttributeError instead of returning
        # a misleading "agent not found".
        s.agent = SimpleNamespace(id=uuid.UUID(agent_id), user_id=uid, name="Schedule Agent")
        return s

    monkeypatch.setattr(database, "async_session", _session_factory)

    async def _no_groups(*_args, **_kwargs):
        return []

    monkeypatch.setattr(api_helpers, "get_user_group_ids", _no_groups)

    # Stub the writable-agent gate so the test stays focused on the cron
    # error formatting — without this the gate-loader runs the real
    # ``_load_caller_user_for_builder`` against the SimpleNamespace
    # session fixture and trips on ``user.is_admin`` long before the
    # cron parser is reached. We hand back a permissive agent so the
    # handler proceeds straight to the cron validation we're testing.
    async def _fake_gate(_db, _uid, _agent_uuid, *, action="modify", assignment_write=False):
        return (
            SimpleNamespace(
                id=_agent_uuid,
                name="Schedule Agent",
                user_id=uid,
                is_company_managed=False,
            ),
            None,
        )

    monkeypatch.setattr(
        builtin_tools_module, "_load_writable_agent_for_builder", _fake_gate
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="create_assignment",
            arguments={
                "agent_id": agent_id,
                "name": "Schedule x",
                "prompt": "ignore",
                "trigger_type": "schedule",
                # The exact value from the prod log (4 fields instead of 5).
                "trigger_config": '{"cron": "*/15 * * 1"}',
            },
        )
    )

    assert not result.is_error
    body = result.content if isinstance(result.content, str) else str(result.content)
    # CONTRACT: exactly one occurrence of the canonical leading phrase.
    # A double-prefix regression would push this to >= 2.
    assert body.count("Invalid cron expression") == 1, (
        f"Expected exactly one 'Invalid cron expression' prefix, got {body!r}"
    )
    # Sanity: the offending value must be echoed so the LLM/operator
    # knows what to fix.
    assert "*/15 * * 1" in body
    assert "Expected trigger_config like" in body
    # And no assignment must have been committed (early-return path).
    assert created_sessions[0].commits == 0


@pytest.mark.asyncio
async def test_get_assignment_uses_dedicated_session_for_parallel_calls(monkeypatch):
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    created_sessions = []
    assignment_specs = [(uuid.uuid4(), uuid.uuid4(), f"Assignment {i}") for i in range(4)]
    assignments = {
        assignment_id: SimpleNamespace(
            id=assignment_id,
            name=assignment_name,
            description=f"Description {i}",
            prompt=f"Prompt {i}",
            activation_prompt=f"Gate {i}" if i % 2 else None,
            trigger_type="manual",
            trigger_config={},
            delay=None,
            enabled=True,
            human_in_the_loop=bool(i % 2),
            ai_label=bool((i + 1) % 2),
            ai_label_prompt=None,
            human_in_the_loop_prompt=None,
            agent_id=agent_id,
        )
        for i, (assignment_id, agent_id, assignment_name) in enumerate(assignment_specs)
    }
    agents = {
        agent_id: SimpleNamespace(id=agent_id, name=f"Agent {i}")
        for i, (_assignment_id, agent_id, _assignment_name) in enumerate(assignment_specs)
    }

    async def _fake_get_user_group_ids(_db, _uid):
        return []

    def _session_factory():
        return _DedicatedAssignmentSession(created_sessions, assignments, agents)

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(api_helpers, "get_user_group_ids", _fake_get_user_group_ids)

    _, executor = build_agent_builder_tool(shared_db, user_id)
    calls = [
        ToolCall(
            id=str(uuid.uuid4()),
            name="get_assignment",
            arguments={"assignment_id": str(assignment_id)},
        )
        for assignment_id, _agent_id, _assignment_name in assignment_specs
    ]

    results = await asyncio.gather(*(executor.execute_tool(call) for call in calls))

    assert len(results) == 4
    assert all(not result.is_error for result in results)
    for i, result in enumerate(results):
        assert f"**Assignment {i}**" in result.content
        assert f"  • Agent: Agent {i}" in result.content
    assert len(created_sessions) == 4
    assert sum(session.commits for session in created_sessions) == 0
    assert all(session.closed for session in created_sessions)


@pytest.mark.asyncio
async def test_get_assignment_surfaces_gate_and_full_action_prompt(monkeypatch):
    """``get_assignment`` must surface the activation gate prompt as a distinct
    labelled field and the action prompt in FULL. The previous implementation
    truncated the action prompt to a 300-char preview and never showed the gate
    at all, so the builder could not read or reason about the gate."""
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    assignment_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    long_action_prompt = "ACTION:" + ("x" * 600)
    gate_prompt = "Is this a non-bot PR opened event? Answer YES or NO only."
    assignments = {
        assignment_id: SimpleNamespace(
            id=assignment_id,
            name="Gated assignment",
            description="desc",
            prompt=long_action_prompt,
            activation_prompt=gate_prompt,
            trigger_type="webhook",
            trigger_config={},
            delay=None,
            enabled=True,
            human_in_the_loop=False,
            ai_label=False,
            ai_label_prompt=None,
            human_in_the_loop_prompt=None,
            agent_id=agent_id,
        )
    }
    agents = {agent_id: SimpleNamespace(id=agent_id, name="Builder agent")}
    created_sessions = []

    async def _fake_get_user_group_ids(_db, _uid):
        return []

    def _session_factory():
        return _DedicatedAssignmentSession(created_sessions, assignments, agents)

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(api_helpers, "get_user_group_ids", _fake_get_user_group_ids)

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="get_assignment",
            arguments={"assignment_id": str(assignment_id)},
        )
    )

    assert not result.is_error
    # Gate prompt surfaced as its own labelled field, separate from the action.
    assert "Activation gate prompt" in result.content
    assert gate_prompt in result.content
    # Action prompt shown in full - no truncation, no ellipsis.
    assert "Action prompt" in result.content
    assert long_action_prompt in result.content
    assert "..." not in result.content


@pytest.mark.asyncio
async def test_get_assignment_renders_empty_action_prompt_as_empty(monkeypatch):
    """``prompt`` is NOT NULL, so an empty action prompt is a real persisted
    value. The read tool must render it as "(empty)" rather than "None" (which
    would misrepresent it as unset / same as a missing gate)."""
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    assignment_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    assignments = {
        assignment_id: SimpleNamespace(
            id=assignment_id,
            name="Empty prompt assignment",
            description="desc",
            prompt="",
            activation_prompt=None,
            trigger_type="manual",
            trigger_config={},
            delay=None,
            enabled=True,
            human_in_the_loop=False,
            ai_label=False,
            ai_label_prompt=None,
            human_in_the_loop_prompt=None,
            agent_id=agent_id,
        )
    }
    agents = {agent_id: SimpleNamespace(id=agent_id, name="Builder agent")}
    created_sessions = []

    async def _fake_get_user_group_ids(_db, _uid):
        return []

    def _session_factory():
        return _DedicatedAssignmentSession(created_sessions, assignments, agents)

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(api_helpers, "get_user_group_ids", _fake_get_user_group_ids)

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="get_assignment",
            arguments={"assignment_id": str(assignment_id)},
        )
    )

    assert not result.is_error
    assert "Action prompt" in result.content
    assert "(empty)" in result.content
    # Read-only inspection must never commit.
    assert sum(s.commits for s in created_sessions) == 0


@pytest.mark.asyncio
async def test_get_assignment_surfaces_all_settings(monkeypatch):
    """``get_assignment`` surfaces every execution setting and renders a NULL
    human_in_the_loop as 'Inherit from agent' - read/write parity for the full
    configuration surface the builder can now set."""
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    assignment_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    assignments = {
        assignment_id: SimpleNamespace(
            id=assignment_id,
            name="Configured",
            description="desc",
            prompt="do the thing",
            activation_prompt=None,
            trigger_type="manual",
            trigger_config={},
            delay=None,
            enabled=True,
            human_in_the_loop=None,
            ai_label=True,
            ai_label_prompt=None,
            human_in_the_loop_prompt=None,
            notify_on_complete=True,
            show_in_digest=True,
            dry_run=True,
            ignore_self_triggered_events=True,
            retry_on_failure=True,
            max_retries=5,
            max_iterations=20,
            timeout_seconds=600,
            agent_id=agent_id,
        )
    }
    agents = {agent_id: SimpleNamespace(id=agent_id, name="Builder agent", max_iterations=None)}
    created_sessions = []

    async def _fake_get_user_group_ids(_db, _uid):
        return []

    def _session_factory():
        return _DedicatedAssignmentSession(created_sessions, assignments, agents)

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(api_helpers, "get_user_group_ids", _fake_get_user_group_ids)

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="get_assignment",
            arguments={"assignment_id": str(assignment_id)},
        )
    )

    assert not result.is_error
    content = result.content
    assert "HITL: Inherit from agent" in content
    assert "Notify on complete: Yes" in content
    assert "Show in digest: Yes" in content
    assert "Dry run: Yes" in content
    assert "Ignore self-triggered events: Yes" in content
    assert "Retry on failure: Yes" in content
    assert "Max retries: 5" in content
    assert "Max iterations: 20" in content
    assert "Timeout (seconds): 600" in content
    # The budget the engine applies, and why (#1695).
    assert "Effective timeout: 600s (" in content
    assert sum(s.commits for s in created_sessions) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config", "expected", "warning"),
    [
        (
            {"cron": "0 7 * * *", "timezone": "Europe/Paris"},
            "Next run: 2026-10-31T07:00:00+01:00 (Europe/Paris), "
            "2026-10-31T06:00:00+00:00 UTC",
            None,
        ),
        (
            {"cron": "0 7 * * *", "timezone": "Mars/Olympus"},
            "Next run: 2026-10-31T07:00:00+00:00 (UTC), 2026-10-31T07:00:00+00:00 UTC",
            "Timezone: timezone 'Mars/Olympus' not recognised, UTC used",
        ),
    ],
)
async def test_get_assignment_shows_the_next_run_in_the_zone_and_utc(
    monkeypatch, config, expected, warning
):
    """A schedule's next fire is shown in ``trigger_config.timezone`` and UTC,
    and an unknown zone says it was read as UTC."""
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    assignment_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    assignments = {
        assignment_id: SimpleNamespace(
            id=assignment_id,
            name="Daily",
            description=None,
            prompt="p",
            activation_prompt=None,
            ai_label_prompt=None,
            human_in_the_loop_prompt=None,
            trigger_type="schedule",
            trigger_config=config,
            delay=None,
            enabled=True,
            last_triggered_at=datetime(2026, 10, 30, 7, 0, tzinfo=UTC),
            agent_id=agent_id,
        )
    }
    agents = {agent_id: SimpleNamespace(id=agent_id, name="Builder agent")}
    created_sessions = []

    async def _fake_get_user_group_ids(_db, _uid):
        return []

    def _session_factory():
        return _DedicatedAssignmentSession(created_sessions, assignments, agents)

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(api_helpers, "get_user_group_ids", _fake_get_user_group_ids)

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="get_assignment",
            arguments={"assignment_id": str(assignment_id)},
        )
    )

    assert not result.is_error
    assert expected in result.content
    if warning:
        assert warning in result.content
    else:
        assert "not recognised" not in result.content


@pytest.mark.asyncio
async def test_get_assignment_tolerates_partial_assignment_double(monkeypatch):
    """``get_assignment`` reads every setting through ``getattr`` defaults, so a
    partial assignment object missing the newer columns - and even
    ``human_in_the_loop`` / ``ai_label`` - renders defaults instead of raising
    AttributeError, the read-tool resilience the inline comment promises."""
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    assignment_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    # Deliberately omit human_in_the_loop, ai_label and all the newer
    # behavioural / limit columns to exercise the getattr fallbacks.
    assignments = {
        assignment_id: SimpleNamespace(
            id=assignment_id,
            name="Partial",
            description="desc",
            prompt="do the thing",
            activation_prompt=None,
            trigger_type="manual",
            trigger_config={},
            delay=None,
            enabled=True,
            ai_label_prompt=None,
            human_in_the_loop_prompt=None,
            agent_id=agent_id,
        )
    }
    agents = {agent_id: SimpleNamespace(id=agent_id, name="Builder agent")}
    created_sessions = []

    async def _fake_get_user_group_ids(_db, _uid):
        return []

    def _session_factory():
        return _DedicatedAssignmentSession(created_sessions, assignments, agents)

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(api_helpers, "get_user_group_ids", _fake_get_user_group_ids)

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="get_assignment",
            arguments={"assignment_id": str(assignment_id)},
        )
    )

    assert not result.is_error
    content = result.content
    # Missing HITL column -> inherit; missing ai_label -> default Yes.
    assert "HITL: Inherit from agent" in content
    assert "AI label: Yes" in content
    # Missing behavioural / limit columns fall back to their defaults.
    assert "Notify on complete: No" in content
    assert "Max retries: 3" in content
    # max_iterations is now inherit-able: a missing column on both the
    # assignment and the agent renders the inherit fallback, not a hardcoded 15.
    assert "Max iterations: Inherited (platform default)" in content
    assert "Timeout (seconds): 300" in content


@pytest.mark.asyncio
async def test_update_assignment_sets_activation_gate_prompt(monkeypatch):
    """``update_assignment`` can set the activation gate prompt, stored as-is so
    prompt-body formatting (including leading/trailing whitespace) is preserved,
    matching the action ``prompt`` handling and the rest of the update tool."""
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    uid = uuid.UUID(user_id)
    assignment_id = uuid.uuid4()
    gate = "  Should I run? Answer YES or NO.\n"
    assignments = {
        assignment_id: SimpleNamespace(
            id=assignment_id,
            name="Original",
            prompt="do the thing",
            activation_prompt=None,
            trigger_type="manual",
            trigger_config={},
            delay=None,
            enabled=True,
            user_id=uid,
        )
    }
    created_sessions = []

    def _session_factory():
        return _DedicatedAssignmentSession(created_sessions, assignments)

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(
        builtin_tools_module,
        "_load_writable_assignment_for_builder",
        _writable_assignment_from_session,
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_assignment",
            arguments={
                "assignment_id": str(assignment_id),
                "activation_prompt": gate,
            },
        )
    )

    assert not result.is_error
    assert "activation_prompt (gate)" in result.content
    assert assignments[assignment_id].activation_prompt == gate  # verbatim
    assert sum(s.commits for s in created_sessions) == 1


@pytest.mark.asyncio
async def test_update_assignment_clears_activation_gate_with_none(monkeypatch):
    """Passing the literal ``"none"`` clears the activation gate back to NULL so
    the assignment always runs (mirrors the ``delay`` clear convention)."""
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    uid = uuid.UUID(user_id)
    assignment_id = uuid.uuid4()
    assignments = {
        assignment_id: SimpleNamespace(
            id=assignment_id,
            name="Original",
            prompt="do the thing",
            activation_prompt="Existing gate?",
            trigger_type="manual",
            trigger_config={},
            delay=None,
            enabled=True,
            user_id=uid,
        )
    }
    created_sessions = []

    def _session_factory():
        return _DedicatedAssignmentSession(created_sessions, assignments)

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(
        builtin_tools_module,
        "_load_writable_assignment_for_builder",
        _writable_assignment_from_session,
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_assignment",
            arguments={
                "assignment_id": str(assignment_id),
                "activation_prompt": "none",
            },
        )
    )

    assert not result.is_error
    assert assignments[assignment_id].activation_prompt is None
    assert sum(s.commits for s in created_sessions) == 1


@pytest.mark.asyncio
async def test_update_assignment_uses_dedicated_session_for_parallel_calls(monkeypatch):
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    uid = uuid.UUID(user_id)
    created_sessions = []
    call_specs = [(uuid.uuid4(), f"Assignment {i}") for i in range(4)]
    assignments = {
        assignment_id: SimpleNamespace(
            id=assignment_id,
            name=f"Original {i}",
            prompt="",
            trigger_type="manual",
            trigger_config={},
            delay=None,
            enabled=True,
            user_id=uid,
        )
        for i, (assignment_id, _assignment_name) in enumerate(call_specs)
    }

    def _session_factory():
        return _DedicatedAssignmentSession(created_sessions, assignments)

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(
        builtin_tools_module,
        "_load_writable_assignment_for_builder",
        _writable_assignment_from_session,
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    calls = [
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_assignment",
            arguments={"assignment_id": str(assignment_id), "name": assignment_name},
        )
        for assignment_id, assignment_name in call_specs
    ]

    results = await asyncio.gather(*(executor.execute_tool(call) for call in calls))

    assert len(results) == 4
    assert all(not result.is_error for result in results)
    assert {result.content for result in results} == {
        f"Assignment **Assignment {i}** updated: name." for i in range(4)
    }
    assert len(created_sessions) == 4
    assert sum(session.commits for session in created_sessions) == 4
    assert all(session.closed for session in created_sessions)
    assert {assignment.name for assignment in assignments.values()} == {
        f"Assignment {i}" for i in range(4)
    }


@pytest.mark.asyncio
async def test_update_assignment_strips_whitespace_name(monkeypatch):
    """A padded assignment name is canonicalised to its stripped form, matching
    the rest of the builder's free-text handling."""
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    uid = uuid.UUID(user_id)
    assignment_id = uuid.uuid4()
    assignments = {
        assignment_id: SimpleNamespace(
            id=assignment_id,
            name="Original",
            prompt="do the thing",
            trigger_type="manual",
            trigger_config={},
            delay=None,
            enabled=True,
            user_id=uid,
        )
    }
    created_sessions = []

    def _session_factory():
        return _DedicatedAssignmentSession(created_sessions, assignments)

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(
        builtin_tools_module,
        "_load_writable_assignment_for_builder",
        _writable_assignment_from_session,
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_assignment",
            arguments={"assignment_id": str(assignment_id), "name": "  Renamed  "},
        )
    )

    assert not result.is_error
    assert assignments[assignment_id].name == "Renamed"  # stripped
    assert sum(s.commits for s in created_sessions) == 1


def _make_update_assignment_env(monkeypatch, assignment: SimpleNamespace):
    """Wire ``update_assignment`` to an always-allow gate over *assignment* and
    return ``(executor, user_id, created_sessions)``."""
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    assignments = {assignment.id: assignment}
    created_sessions: list[_DedicatedAssignmentSession] = []

    def _session_factory():
        return _DedicatedAssignmentSession(created_sessions, assignments)

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(
        builtin_tools_module,
        "_load_writable_assignment_for_builder",
        _writable_assignment_from_session,
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    return executor, user_id, created_sessions


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw,expected",
    [("true", True), ("false", False), ("inherit", None), ("INHERIT", None)],
)
async def test_update_assignment_sets_human_in_the_loop_toggle(
    monkeypatch, raw, expected
):
    """``update_assignment`` exposes the human-in-the-loop ON/OFF boolean (not
    just its prompt override). 'inherit' (any case) clears it back to NULL so
    it inherits from the parent agent. Previously the toggle was unsettable through
    the builder, so an orchestrator could read HITL but never flip it."""
    assignment = SimpleNamespace(
        id=uuid.uuid4(),
        name="Original",
        prompt="do the thing",
        trigger_type="manual",
        trigger_config={},
        delay=None,
        enabled=True,
        human_in_the_loop=True,
        user_id=uuid.uuid4(),
    )
    executor, _user_id, created_sessions = _make_update_assignment_env(
        monkeypatch, assignment
    )

    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_assignment",
            arguments={
                "assignment_id": str(assignment.id),
                "human_in_the_loop": raw,
            },
        )
    )

    assert not result.is_error
    assert "human_in_the_loop" in result.content
    assert assignment.human_in_the_loop is expected
    assert sum(s.commits for s in created_sessions) == 1


@pytest.mark.asyncio
async def test_update_assignment_sets_all_behavioural_settings(monkeypatch):
    """``update_assignment`` exposes the full behavioural configuration surface:
    every on/off toggle plus the safety/limit integers (matching the REST API's
    UpdateAssignmentRequest)."""
    assignment = SimpleNamespace(
        id=uuid.uuid4(),
        name="Original",
        prompt="do the thing",
        trigger_type="manual",
        trigger_config={},
        delay=None,
        enabled=True,
        human_in_the_loop=None,
        ai_label=True,
        notify_on_complete=False,
        show_in_digest=False,
        dry_run=False,
        ignore_self_triggered_events=False,
        retry_on_failure=False,
        max_retries=3,
        max_iterations=15,
        timeout_seconds=300,
        user_id=uuid.uuid4(),
    )
    executor, _user_id, created_sessions = _make_update_assignment_env(
        monkeypatch, assignment
    )

    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_assignment",
            arguments={
                "assignment_id": str(assignment.id),
                "ai_label": "false",
                "notify_on_complete": "true",
                "show_in_digest": "true",
                "dry_run": "true",
                "ignore_self_triggered_events": "true",
                "retry_on_failure": "true",
                "max_retries": "5",
                "max_iterations": "20",
                "timeout_seconds": "600",
            },
        )
    )

    assert not result.is_error
    assert assignment.ai_label is False
    assert assignment.notify_on_complete is True
    assert assignment.show_in_digest is True
    assert assignment.dry_run is True
    assert assignment.ignore_self_triggered_events is True
    assert assignment.retry_on_failure is True
    assert assignment.max_retries == 5
    assert assignment.max_iterations == 20
    assert assignment.timeout_seconds == 600
    assert sum(s.commits for s in created_sessions) == 1


@pytest.mark.asyncio
async def test_update_assignment_accepts_native_bool_and_int(monkeypatch):
    """Tool-call kwargs are forwarded verbatim, so update_assignment must honour
    a real JSON bool / int (not only their string forms) instead of silently
    skipping the field."""
    assignment = SimpleNamespace(
        id=uuid.uuid4(),
        name="Original",
        prompt="do the thing",
        trigger_type="manual",
        trigger_config={},
        delay=None,
        enabled=True,
        human_in_the_loop=None,
        ai_label=True,
        max_iterations=15,
        user_id=uuid.uuid4(),
    )
    executor, _user_id, created_sessions = _make_update_assignment_env(
        monkeypatch, assignment
    )

    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_assignment",
            arguments={
                "assignment_id": str(assignment.id),
                "enabled": False,  # native JSON bool (no AttributeError)
                "human_in_the_loop": True,  # native JSON bool
                "ai_label": False,  # native JSON bool
                "max_iterations": 20,  # native JSON int
            },
        )
    )

    assert not result.is_error
    assert assignment.enabled is False
    assert assignment.human_in_the_loop is True
    assert assignment.ai_label is False
    assert assignment.max_iterations == 20
    assert sum(s.commits for s in created_sessions) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("sentinel", ["none", "inherit", "default", "NONE"])
async def test_update_assignment_max_iterations_inherit_clears(monkeypatch, sentinel):
    """The 'none'/'inherit'/'default' sentinel (case-insensitive) clears the
    assignment's max_iterations override back to NULL so it inherits (agent
    override -> platform default)."""
    assignment = SimpleNamespace(
        id=uuid.uuid4(),
        name="Original",
        prompt="do the thing",
        trigger_type="manual",
        trigger_config={},
        delay=None,
        enabled=True,
        human_in_the_loop=None,
        ai_label=True,
        max_iterations=40,  # an existing explicit override
        user_id=uuid.uuid4(),
    )
    executor, _user_id, created_sessions = _make_update_assignment_env(
        monkeypatch, assignment
    )

    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_assignment",
            arguments={
                "assignment_id": str(assignment.id),
                "max_iterations": sentinel,
            },
        )
    )

    assert not result.is_error
    assert assignment.max_iterations is None
    assert sum(s.commits for s in created_sessions) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments,needle",
    [
        ({"ai_label": "maybe"}, "ai_label"),
        ({"ai_label": 1}, "ai_label"),
        ({"enabled": "maybe"}, "enabled"),
        ({"human_in_the_loop": "sometimes"}, "human_in_the_loop"),
        ({"max_iterations": "0"}, "max_iterations"),
        ({"timeout_seconds": "-1"}, "timeout_seconds"),
        ({"max_retries": "abc"}, "max_retries"),
    ],
)
async def test_update_assignment_rejects_invalid_setting(
    monkeypatch, arguments, needle
):
    """Invalid booleans/integers return an actionable error and never commit."""
    assignment = SimpleNamespace(
        id=uuid.uuid4(),
        name="Original",
        prompt="do the thing",
        trigger_type="manual",
        trigger_config={},
        delay=None,
        enabled=True,
        human_in_the_loop=True,
        ai_label=True,
        max_retries=3,
        max_iterations=15,
        timeout_seconds=300,
        user_id=uuid.uuid4(),
    )
    executor, _user_id, created_sessions = _make_update_assignment_env(
        monkeypatch, assignment
    )

    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_assignment",
            arguments={"assignment_id": str(assignment.id), **arguments},
        )
    )

    assert needle in result.content
    assert sum(s.commits for s in created_sessions) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments,needle",
    [
        ({"delay": 1}, "delay"),
        ({"activation_prompt": True}, "activation_prompt"),
        ({"description": 1}, "description"),
        ({"ai_label_prompt": True}, "ai_label_prompt"),
        ({"name": 1}, "name"),
    ],
)
async def test_update_assignment_rejects_non_string_field(
    monkeypatch, arguments, needle
):
    """update_assignment type-guards its string fields before any .strip()/.lower():
    a native non-string (tool kwargs forwarded verbatim) returns an actionable
    '... must be a string.' instead of crashing with AttributeError, and never
    commits."""
    assignment = SimpleNamespace(
        id=uuid.uuid4(),
        name="Original",
        prompt="do the thing",
        trigger_type="manual",
        trigger_config={},
        delay=None,
        enabled=True,
        user_id=uuid.uuid4(),
    )
    executor, _user_id, created_sessions = _make_update_assignment_env(
        monkeypatch, assignment
    )

    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_assignment",
            arguments={"assignment_id": str(assignment.id), **arguments},
        )
    )

    assert needle in result.content
    assert "must be a string" in result.content
    assert sum(s.commits for s in created_sessions) == 0


@pytest.mark.asyncio
async def test_update_assignment_normalizes_delay(monkeypatch):
    """update_assignment strips the delay so a padded clear sentinel ('  none  ')
    clears it to NULL and a real value is stored stripped."""
    assignment = SimpleNamespace(
        id=uuid.uuid4(),
        name="Original",
        prompt="do the thing",
        trigger_type="manual",
        trigger_config={},
        delay="5m",
        enabled=True,
        user_id=uuid.uuid4(),
    )
    executor, _user_id, _created_sessions = _make_update_assignment_env(
        monkeypatch, assignment
    )

    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_assignment",
            arguments={"assignment_id": str(assignment.id), "delay": "  none  "},
        )
    )
    assert not result.is_error
    assert assignment.delay is None

    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_assignment",
            arguments={"assignment_id": str(assignment.id), "delay": "  1h  "},
        )
    )
    assert not result.is_error
    assert assignment.delay == "1h"


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["null", "[]", "3", '"manual"'])
async def test_update_assignment_refuses_a_trigger_config_that_is_not_an_object(
    monkeypatch, raw
):
    """``trigger_config`` is a NOT NULL object column. ``"null"`` is valid
    JSON for ``None``, which SQLAlchemy writes as the JSON literal ``null``: the
    commit succeeds and every later read of the assignment fails to serialize.
    Refused like ``create_assignment`` refuses it, and never committed."""
    assignment = SimpleNamespace(
        id=uuid.uuid4(),
        name="Original",
        prompt="do the thing",
        trigger_type="manual",
        trigger_config={"source": "kept"},
        delay=None,
        enabled=True,
        user_id=uuid.uuid4(),
    )
    executor, _user_id, created_sessions = _make_update_assignment_env(
        monkeypatch, assignment
    )

    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_assignment",
            arguments={"assignment_id": str(assignment.id), "trigger_config": raw},
        )
    )

    assert "must decode to a JSON object" in result.content
    assert assignment.trigger_config == {"source": "kept"}
    assert sum(s.commits for s in created_sessions) == 0


def test_parse_trigger_config_arg_accepts_only_a_json_object():
    """The one decoder behind both assignment tools' ``trigger_config``."""
    from app.services.builtin_tools.agent_builder import _parse_trigger_config_arg

    assert _parse_trigger_config_arg('{"cron": "0 9 * * 1"}') == (
        {"cron": "0 9 * * 1"},
        None,
    )
    config, error = _parse_trigger_config_arg("null")
    assert config is None
    assert "must decode to a JSON object" in error and "NoneType" in error
    config, error = _parse_trigger_config_arg("{not json")
    assert config is None
    assert "must be a valid JSON string" in error


def _stored_assignment(trigger_type: str, trigger_config: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="Nightly digest",
        prompt="do the thing",
        trigger_type=trigger_type,
        trigger_config=trigger_config,
        delay=None,
        enabled=True,
        user_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored_type", "stored_config", "arguments", "expected"),
    [
        # A new cron on a stored schedule.
        ("schedule", {"cron": "0 9 * * 1"}, {"trigger_config": '{"cron": "*/15 * * 1"}'},
         "Invalid cron expression '*/15 * * 1'"),
        ("schedule", {"cron": "0 9 * * 1"}, {"trigger_config": '{"cron": ""}'},
         "non-empty 'cron' expression in trigger_config, got ''"),
        ("schedule", {"cron": "0 9 * * 1"}, {"trigger_config": '{"cron": 42}'}, "got 42"),
        # A new config without a cron: the scheduler would never run it.
        ("schedule", {"cron": "0 9 * * 1"}, {"trigger_config": '{"timezone": "UTC"}'},
         "non-empty 'cron' expression in trigger_config."),
        # Switching to schedule with a bad config in the same call.
        ("manual", {}, {"trigger_type": "schedule", "trigger_config": '{"cron": "nope"}'},
         "Invalid cron expression 'nope'"),
        # Switching to schedule keeps the stored config, which is checked too.
        ("manual", {"cron": "*/15 * * 1"}, {"trigger_type": "schedule"},
         "Invalid cron expression '*/15 * * 1'"),
        ("manual", {}, {"trigger_type": "schedule"}, "non-empty 'cron' expression"),
    ],
)
async def test_update_assignment_refuses_a_schedule_the_scheduler_cannot_run(
    monkeypatch, stored_type, stored_config, arguments, expected
):
    """The REST update and ``create_assignment`` refuse these; the tool used to
    answer "updated" and leave the scheduler to pause (or silently skip) the
    assignment. Refused on the trigger as it will be saved, and never committed."""
    assignment = _stored_assignment(stored_type, stored_config)
    executor, _user_id, created_sessions = _make_update_assignment_env(monkeypatch, assignment)

    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_assignment",
            arguments={"assignment_id": str(assignment.id), "name": "Renamed", **arguments},
        )
    )

    assert expected in result.content
    assert "Expected trigger_config like" in result.content
    assert result.content.endswith("Nothing was saved.")
    assert (assignment.trigger_type, assignment.trigger_config) == (stored_type, stored_config)
    assert sum(s.commits for s in created_sessions) == 0


@pytest.mark.asyncio
async def test_update_assignment_refusal_never_echoes_a_stored_webhook_secret(monkeypatch):
    """Switching a webhook to schedule checks its stored config; the refusal names
    the cron, never the rest of the config."""
    assignment = _stored_assignment("webhook", {"path": "hook", "webhook_secret": "s3cret-value"})
    executor, _user_id, created_sessions = _make_update_assignment_env(monkeypatch, assignment)

    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_assignment",
            arguments={"assignment_id": str(assignment.id), "trigger_type": "schedule"},
        )
    )

    assert "non-empty 'cron' expression" in result.content
    assert "s3cret-value" not in result.content
    assert assignment.trigger_type == "webhook"
    assert sum(s.commits for s in created_sessions) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored_type", "stored_config", "arguments", "saved_type", "saved_cron"),
    [
        ("manual", {}, {"trigger_type": "schedule", "trigger_config": '{"cron": "0 9 * * 1-5"}'},
         "schedule", "0 9 * * 1-5"),
        ("schedule", {"cron": "0 9 * * 1"}, {"trigger_config": '{"cron": "30 6 * * *"}'},
         "schedule", "30 6 * * *"),
        # Switching to schedule alone is fine when the stored cron is valid.
        ("manual", {"cron": "0 9 * * 1"}, {"trigger_type": "schedule"}, "schedule", "0 9 * * 1"),
    ],
)
async def test_update_assignment_saves_a_schedule_the_scheduler_can_run(
    monkeypatch, stored_type, stored_config, arguments, saved_type, saved_cron
):
    assignment = _stored_assignment(stored_type, stored_config)
    executor, _user_id, created_sessions = _make_update_assignment_env(monkeypatch, assignment)

    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_assignment",
            arguments={"assignment_id": str(assignment.id), **arguments},
        )
    )

    assert result.content.startswith("Assignment **Nightly digest** updated:")
    assert assignment.trigger_type == saved_type
    assert assignment.trigger_config["cron"] == saved_cron
    assert sum(s.commits for s in created_sessions) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored_type", "stored_config", "arguments"),
    [
        # Leaving schedule is never blocked by the cron it leaves behind.
        ("schedule", {"cron": "*/15 * * 1"}, {"trigger_type": "manual"}),
        ("webhook", {"path": "hook"}, {"trigger_config": '{"path": "other"}'}),
        # The cron key of a non-schedule config is not read by the scheduler.
        ("manual", {}, {"trigger_config": '{"cron": "nope"}'}),
        # Unrelated edits of a legacy schedule whose cron is broken still work:
        # pausing it is how it gets fixed.
        ("schedule", {"cron": "*/15 * * 1"}, {"enabled": "false"}),
        ("schedule", {"cron": "*/15 * * 1"}, {"name": "Renamed"}),
    ],
)
async def test_update_assignment_leaves_non_schedule_and_unrelated_edits_alone(
    monkeypatch, stored_type, stored_config, arguments
):
    assignment = _stored_assignment(stored_type, stored_config)
    executor, _user_id, created_sessions = _make_update_assignment_env(monkeypatch, assignment)

    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_assignment",
            arguments={"assignment_id": str(assignment.id), **arguments},
        )
    )

    assert " updated: " in result.content, result.content
    assert sum(s.commits for s in created_sessions) == 1


def test_parse_optional_bool_arg_error_names_the_declared_inherit_value():
    """The tri-state takes ONE declared inherit value (``inherit``, any case):
    the error names it, and a guessed synonym is refused with that guidance
    instead of being read as a word (architecture invariant 18, #5303)."""
    from app.services.builtin_tools.agent_builder import (
        INHERIT_VALUE,
        _parse_optional_bool_arg,
    )

    for spelled in (INHERIT_VALUE, INHERIT_VALUE.upper(), f" {INHERIT_VALUE} "):
        assert _parse_optional_bool_arg(spelled, allow_inherit=True) == (None, None, True)

    for guess in ("maybe", "default", "none"):
        _value, error, provided = _parse_optional_bool_arg(guess, allow_inherit=True)
        assert provided is True
        assert _value is None
        assert f"'{INHERIT_VALUE}'" in error, error

    # Without allow_inherit the message stays restricted to the booleans.
    _v, plain_error, _p = _parse_optional_bool_arg("maybe")
    assert plain_error == "must be 'true' or 'false'"


def test_optional_arg_parsers_accept_native_json_types():
    """Tool kwargs are forwarded verbatim, so the parsers accept a real JSON
    bool / int (provided=True) while still treating None / blank as "leave
    unchanged" and rejecting bool / float for integer fields."""
    from app.services.builtin_tools.agent_builder import (
        _parse_optional_bool_arg,
        _parse_optional_int_arg,
    )

    assert _parse_optional_bool_arg(True) == (True, None, True)
    assert _parse_optional_bool_arg(False) == (False, None, True)
    assert _parse_optional_bool_arg(None) == (None, None, False)

    # A non-bool / non-string type (e.g. an int) is an explicit invalid value:
    # an actionable error (provided=True), never a silent skip.
    _bv, bool_type_err, bool_provided = _parse_optional_bool_arg(1)
    assert bool_provided is True and bool_type_err and "true" in bool_type_err

    assert _parse_optional_int_arg(20, "max_iterations", minimum=1, maximum=100) == (
        20,
        None,
        True,
    )
    assert _parse_optional_int_arg(None, "max_iterations") == (None, None, False)
    assert _parse_optional_int_arg("", "max_iterations") == (None, None, False)

    # A bool is not a valid integer limit even though bool subclasses int.
    _v, bool_err, provided = _parse_optional_int_arg(True, "max_iterations")
    assert provided is True and bool_err and "integer" in bool_err

    # A float (or any other non-string, non-int type) is rejected too.
    _v2, float_err, prov2 = _parse_optional_int_arg(20.5, "timeout_seconds")
    assert prov2 is True and float_err and "integer" in float_err

    # An out-of-range native int is reported, not silently skipped.
    _v3, range_err, prov3 = _parse_optional_int_arg(
        0, "max_iterations", minimum=1, maximum=100
    )
    assert prov3 is True and range_err and "at least 1" in range_err


def test_update_assignment_schema_documents_native_types():
    """The update_assignment schema declares the native JSON types the handler
    now accepts (boolean for the toggles, integer for the limits) alongside the
    string forms, so strict clients / validators don't reject valid inputs."""
    tools, _executor = build_agent_builder_tool(_SharedSession(), str(uuid.uuid4()))
    props = next(t for t in tools if t.name == "update_assignment").input_schema[
        "properties"
    ]

    for field in (
        "enabled",
        "human_in_the_loop",
        "ai_label",
        "notify_on_complete",
        "show_in_digest",
        "dry_run",
        "ignore_self_triggered_events",
        "retry_on_failure",
    ):
        types = props[field]["type"]
        assert "string" in types and "boolean" in types, (
            f"{field} must accept string + boolean: {types!r}"
        )

    for field in ("max_retries", "max_iterations", "timeout_seconds"):
        types = props[field]["type"]
        assert "string" in types and "integer" in types, (
            f"{field} must accept string + integer: {types!r}"
        )


def test_update_assignment_schema_documents_the_inherit_value():
    """The ``update_assignment`` tool schema documents the inherit value the
    parser accepts (generated from the same constant, so they cannot drift)."""
    from app.services.builtin_tools.agent_builder import INHERIT_VALUE

    tools, _executor = build_agent_builder_tool(_SharedSession(), str(uuid.uuid4()))
    update_tool = next(t for t in tools if t.name == "update_assignment")
    hitl_desc = update_tool.input_schema["properties"]["human_in_the_loop"][
        "description"
    ]
    assert f"'{INHERIT_VALUE}'" in hitl_desc, hitl_desc


@pytest.mark.asyncio
async def test_update_assignment_whitespace_prompt_leaves_unchanged(monkeypatch):
    """A whitespace-only ``prompt`` must be "leave unchanged" (not persisted as a
    blank instruction); with no other field it is a no-op with no commit."""
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    uid = uuid.UUID(user_id)
    assignment_id = uuid.uuid4()
    assignments = {
        assignment_id: SimpleNamespace(
            id=assignment_id,
            name="Original",
            prompt="do the thing",
            trigger_type="manual",
            trigger_config={},
            delay=None,
            enabled=True,
            user_id=uid,
        )
    }
    created_sessions = []

    def _session_factory():
        return _DedicatedAssignmentSession(created_sessions, assignments)

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(
        builtin_tools_module,
        "_load_writable_assignment_for_builder",
        _writable_assignment_from_session,
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_assignment",
            arguments={"assignment_id": str(assignment_id), "prompt": "   "},
        )
    )

    assert not result.is_error
    assert result.content == "No fields to update. Provide at least one field."
    assert assignments[assignment_id].prompt == "do the thing"  # untouched
    assert sum(s.commits for s in created_sessions) == 0


@pytest.mark.asyncio
async def test_update_assignment_commit_failure_returns_friendly_error(monkeypatch):
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    uid = uuid.UUID(user_id)
    assignment_id = uuid.uuid4()
    created_sessions = []
    assignments = {
        assignment_id: SimpleNamespace(
            id=assignment_id,
            name="Original",
            prompt="",
            trigger_type="manual",
            trigger_config={},
            delay=None,
            enabled=True,
            user_id=uid,
        )
    }

    def _session_factory():
        return _DedicatedAssignmentSession(
            created_sessions,
            assignments,
            fail_on_commit=True,
        )

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(
        builtin_tools_module,
        "_load_writable_assignment_for_builder",
        _writable_assignment_from_session,
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_assignment",
            arguments={"assignment_id": str(assignment_id), "name": "Updated"},
        )
    )

    assert result.is_error
    assert (
        result.content
        == f"Failed to update assignment `{assignment_id}`. Please try again."
    )
    assert len(created_sessions) == 1
    assert created_sessions[0].commits == 0
    assert created_sessions[0].rollbacks == 1
    assert created_sessions[0].closed


@pytest.mark.asyncio
async def test_delete_assignment_commit_failure_returns_friendly_error(monkeypatch):
    """``_delete_assignment`` mirrors the ``update`` handlers: a failed
    commit must (a) be caught, (b) trigger ``await _db.rollback()`` so the
    half-applied delete cannot leak into the pooled connection, and
    (c) surface a generic retry message to the tool caller instead of
    leaking the raw exception text.
    """
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    uid = uuid.UUID(user_id)
    assignment_id = uuid.uuid4()
    created_sessions = []
    assignments = {
        assignment_id: SimpleNamespace(
            id=assignment_id,
            name="Doomed",
            user_id=uid,
        )
    }

    def _session_factory():
        return _DedicatedAssignmentSession(
            created_sessions,
            assignments,
            fail_on_commit=True,
        )

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(
        builtin_tools_module,
        "_load_writable_assignment_for_builder",
        _writable_assignment_from_session,
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="delete_assignment",
            arguments={"assignment_id": str(assignment_id)},
        )
    )

    assert result.is_error
    assert (
        result.content
        == f"Failed to delete assignment `{assignment_id}`. Please try again."
    )
    assert len(created_sessions) == 1
    session = created_sessions[0]
    assert session.commits == 0
    assert session.rollbacks == 1
    assert session.closed
    # The DELETE was issued via ``await _db.delete(...)`` before commit
    # failed — verify by inspecting the recorded operations.
    assert any(
        op[0] == "delete" for op in session.executed_statements if isinstance(op, tuple)
    )


def _deletable_agent(agent_id: uuid.UUID, name: str, uid: uuid.UUID) -> SimpleNamespace:
    """An agent as the shared deletion reads it: no knowledge base of its own, and what its tombstone keeps."""
    return SimpleNamespace(
        id=agent_id,
        name=name,
        user_id=uid,
        slug=name.lower().replace(" ", "-"),
        icon="Bot",
        is_company_managed=False,
        knowledge_base_ids=[],
    )


def _stub_deletion(monkeypatch, uid: uuid.UUID) -> None:
    """The caller the deletion is audited as (the gate loaded it too), for an
    agent with no run left: these tests drive the agent's own transaction, the
    run batches before it are pinned in ``test_agent_deletion*.py``."""
    from app.services import agent_deletion

    async def _caller(_db, _uid):
        return SimpleNamespace(id=uid, email="builder@example.com")

    async def _no_runs(*_a, **_k):
        return None

    monkeypatch.setattr(builtin_tools_module, "_load_caller_user_for_builder", _caller)
    monkeypatch.setattr(agent_deletion, "_delete_runs_in_batches", _no_runs)


@pytest.mark.asyncio
async def test_delete_agent_commit_failure_returns_friendly_error(monkeypatch):
    """``_delete_agent`` issues a bulk ``DELETE FROM assignments WHERE
    agent_id = ?`` and then ``await _db.delete(agent)`` before the commit.
    A failing commit must rollback both operations and surface a generic
    retry message — leaving the agent + its assignments in a consistent
    state for the next attempt.
    """
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    uid = uuid.UUID(user_id)
    agent_id = uuid.uuid4()
    created_sessions = []
    agents = {agent_id: _deletable_agent(agent_id, "Doomed Agent", uid)}
    _stub_deletion(monkeypatch, uid)

    def _session_factory():
        return _DedicatedAssignmentSession(
            created_sessions,
            assignments_by_id={},
            agents_by_id=agents,
            fail_on_commit=True,
        )

    monkeypatch.setattr(database, "async_session", _session_factory)

    # The delete_agent gate stub mirrors the parallel-update one but pulls the
    # target agent from the dedicated session's ``agents_by_id`` map so the
    # commit-failure assertions can run against the same SimpleNamespace
    # fixture the test wired up.
    async def _gate_from_agents_map(_db, _uid, agent_uuid, *, action="modify"):
        return _db._agents_by_id.get(agent_uuid), None

    monkeypatch.setattr(
        builtin_tools_module,
        "_load_writable_agent_for_builder",
        _gate_from_agents_map,
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="delete_agent",
            arguments={"agent_id": str(agent_id)},
        )
    )

    assert result.is_error
    assert (
        result.content
        == f"Failed to delete agent `{agent_id}`. Please try again."
    )
    assert len(created_sessions) == 1
    session = created_sessions[0]
    assert session.commits == 0
    assert session.rollbacks == 1
    assert session.closed
    # Both the bulk Assignment DELETE (via ``execute(sa_delete(...))``)
    # and the ``await _db.delete(agent)`` should have been recorded
    # before the commit raised.
    assert any(
        op[0] == "delete" for op in session.executed_statements if isinstance(op, tuple)
    )


class _FakeOrig:
    """Stand-in for an asyncpg driver exception carrying a SQLSTATE."""

    def __init__(self, pgcode: str):
        self.pgcode = pgcode


def _deadlock_error(message: str = "deadlock detected") -> Exception:
    """Build an exception ``is_retryable_db_error`` treats as retryable.

    Mirrors a real ``DBAPIError`` wrapping ``asyncpg.DeadlockDetectedError``:
    the SQLSTATE (40P01) rides on ``exc.orig.pgcode``.
    """
    exc = Exception(message)
    exc.orig = _FakeOrig("40P01")  # type: ignore[attr-defined]
    return exc


def _is_advisory_lock_stmt(stmt) -> bool:
    return "pg_advisory_xact_lock" in str(stmt)


def _is_dml_stmt(stmt) -> bool:
    """True for a row-mutating statement - the ops that take row locks and can
    deadlock. The session mock records ``_db.delete(obj)`` as a
    ``("delete", obj)`` tuple; SQL DELETE/UPDATE/INSERT arrive as statements.
    """
    if isinstance(stmt, tuple):
        return bool(stmt) and stmt[0] == "delete"
    return str(stmt).lstrip().upper().startswith(("DELETE", "UPDATE", "INSERT"))


def _assert_lock_before_dml(executed_statements) -> None:
    """The advisory serialization lock must be taken before the first row-locking
    DML. Harmless reads (the permission gate / lookups) may legitimately precede
    it, so assert ordering relative to the first DML rather than that the lock is
    literally the first statement (which would be brittle - the real delete flow
    SELECTs the agent for the permission check before taking the lock).
    """
    lock_idx = next(
        (i for i, s in enumerate(executed_statements) if _is_advisory_lock_stmt(s)),
        None,
    )
    assert lock_idx is not None, "advisory lock was never acquired"
    dml_idx = next(
        (i for i, s in enumerate(executed_statements) if _is_dml_stmt(s)), None
    )
    assert dml_idx is not None, "expected at least one DELETE/DML statement"
    assert lock_idx < dml_idx, (
        f"advisory lock (idx {lock_idx}) must be acquired before the first DML "
        f"(idx {dml_idx})"
    )


async def _instant_sleep(_delay):
    return None


@pytest.mark.asyncio
async def test_delete_agent_retries_on_deadlock_then_succeeds(monkeypatch):
    """A transient deadlock (40P01) on the first commit must be retried on a
    fresh session and ultimately succeed.

    The LLM commonly emits several ``delete_agent`` calls in parallel and the
    agent loop runs them concurrently, each on its own session; deleting agents
    that reference one another deadlocks at the DB. The advisory serialization
    lock plus this retry guarantee an agent is ALWAYS deletable regardless of
    concurrency.
    """
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    uid = uuid.UUID(user_id)
    agent_id = uuid.uuid4()
    created_sessions: list[_DedicatedAssignmentSession] = []
    agents = {agent_id: _deletable_agent(agent_id, "Resilient Agent", uid)}
    _stub_deletion(monkeypatch, uid)

    # First attempt's session deadlocks on commit; the retry's session commits.
    def _session_factory():
        first = len(created_sessions) == 0
        return _DedicatedAssignmentSession(
            created_sessions,
            assignments_by_id={},
            agents_by_id=agents,
            fail_on_commit=first,
            commit_error=_deadlock_error() if first else None,
        )

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(api_helpers.asyncio, "sleep", _instant_sleep)

    async def _gate_from_agents_map(_db, _uid, agent_uuid, *, action="modify"):
        return _db._agents_by_id.get(agent_uuid), None

    monkeypatch.setattr(
        builtin_tools_module,
        "_load_writable_agent_for_builder",
        _gate_from_agents_map,
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="delete_agent",
            arguments={"agent_id": str(agent_id)},
        )
    )

    assert not result.is_error
    assert result.content == (
        "Agent **Resilient Agent** and all its assignments have been deleted."
    )
    # Two sessions: the deadlocked attempt + the successful retry.
    assert len(created_sessions) == 2
    assert created_sessions[0].rollbacks == 1
    assert created_sessions[0].commits == 0
    assert created_sessions[1].commits == 1
    # Every attempt acquires the advisory serialization lock BEFORE any DELETE.
    for sess in created_sessions:
        _assert_lock_before_dml(sess.executed_statements)


@pytest.mark.asyncio
async def test_delete_agent_acquires_serialization_lock(monkeypatch):
    """A normal (non-deadlocking) delete must still take the advisory lock so
    concurrent agent deletes serialize and can never deadlock."""
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    uid = uuid.UUID(user_id)
    agent_id = uuid.uuid4()
    created_sessions: list[_DedicatedAssignmentSession] = []
    agents = {agent_id: _deletable_agent(agent_id, "Lone Agent", uid)}
    _stub_deletion(monkeypatch, uid)

    def _session_factory():
        return _DedicatedAssignmentSession(
            created_sessions, assignments_by_id={}, agents_by_id=agents
        )

    monkeypatch.setattr(database, "async_session", _session_factory)

    async def _gate_from_agents_map(_db, _uid, agent_uuid, *, action="modify"):
        return _db._agents_by_id.get(agent_uuid), None

    monkeypatch.setattr(
        builtin_tools_module,
        "_load_writable_agent_for_builder",
        _gate_from_agents_map,
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="delete_agent",
            arguments={"agent_id": str(agent_id)},
        )
    )

    assert not result.is_error
    assert len(created_sessions) == 1
    session = created_sessions[0]
    assert session.commits == 1
    # The serialization lock is acquired before any row-locking DML.
    _assert_lock_before_dml(session.executed_statements)


def _is_a2a_task_lock_stmt(stmt) -> bool:
    if isinstance(stmt, tuple):
        return False
    from sqlalchemy.dialects import postgresql

    sql = str(stmt.compile(dialect=postgresql.dialect()))
    return sql.startswith("SELECT a2a_tasks.id") and "FOR NO KEY UPDATE" in sql


@pytest.mark.asyncio
async def test_delete_agent_locks_its_a2a_tasks_before_any_write(monkeypatch):
    """The A2A tasks the deletion cascades onto are locked between the
    serialization lock and the first write: a cancel, a claim and the
    terminal write take a task row before its run row, and a deletion writing
    the run first deadlocked with them."""
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    uid = uuid.UUID(user_id)
    agent_id = uuid.uuid4()
    created_sessions: list[_DedicatedAssignmentSession] = []
    agents = {agent_id: _deletable_agent(agent_id, "Exposed Agent", uid)}
    _stub_deletion(monkeypatch, uid)

    def _session_factory():
        return _DedicatedAssignmentSession(
            created_sessions, assignments_by_id={}, agents_by_id=agents
        )

    monkeypatch.setattr(database, "async_session", _session_factory)

    async def _gate_from_agents_map(_db, _uid, agent_uuid, *, action="modify"):
        return _db._agents_by_id.get(agent_uuid), None

    monkeypatch.setattr(
        builtin_tools_module,
        "_load_writable_agent_for_builder",
        _gate_from_agents_map,
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="delete_agent",
            arguments={"agent_id": str(agent_id)},
        )
    )

    assert not result.is_error
    statements = created_sessions[0].executed_statements
    serialized = next(i for i, s in enumerate(statements) if _is_advisory_lock_stmt(s))
    tasks = next((i for i, s in enumerate(statements) if _is_a2a_task_lock_stmt(s)), None)
    written = next(i for i, s in enumerate(statements) if _is_dml_stmt(s))
    assert tasks is not None, "the agent's A2A tasks were never locked"
    assert serialized < tasks < written
    # And again in the statement before the cascading DELETE of the agent, so a
    # task created since the first lock is held first too.
    agent_deleted = next(
        i for i, s in enumerate(statements) if isinstance(s, tuple) and s[0] == "delete"
    )
    assert _is_a2a_task_lock_stmt(statements[agent_deleted - 1]), statements


def test_is_retryable_db_error_classifies_deadlocks():
    """40P01 / 40001 SQLSTATEs and the textual fallback are retryable; other
    errors are not."""
    assert api_helpers.is_retryable_db_error(_deadlock_error())

    serialization = Exception("could not serialize access")
    serialization.orig = _FakeOrig("40001")  # type: ignore[attr-defined]
    assert api_helpers.is_retryable_db_error(serialization)

    # Textual fallback when no SQLSTATE is available on the exception.
    assert api_helpers.is_retryable_db_error(Exception("deadlock detected"))
    assert api_helpers.is_retryable_db_error(Exception("serialization failure"))

    # Non-retryable errors must NOT be retried.
    assert not api_helpers.is_retryable_db_error(RuntimeError("boom"))
    assert not api_helpers.is_retryable_db_error(ValueError("bad id"))


@pytest.mark.asyncio
async def test_run_with_deadlock_retry_retries_then_succeeds():
    calls = {"n": 0}

    async def _op():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _deadlock_error()
        return "ok"

    result = await api_helpers.run_with_deadlock_retry(
        _op, max_retries=5, base_delay=0.0
    )
    assert result == "ok"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_run_with_deadlock_retry_propagates_non_retryable():
    async def _op():
        raise ValueError("permanent")

    with pytest.raises(ValueError, match="permanent"):
        await api_helpers.run_with_deadlock_retry(_op, max_retries=5, base_delay=0.0)


@pytest.mark.asyncio
async def test_run_with_deadlock_retry_reraises_after_exhausting():
    calls = {"n": 0}

    async def _op():
        calls["n"] += 1
        raise _deadlock_error("deadlock detected")

    with pytest.raises(Exception, match="deadlock"):
        await api_helpers.run_with_deadlock_retry(_op, max_retries=3, base_delay=0.0)
    # Exactly ``max_retries`` attempts were made before giving up.
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_trigger_assignment_returns_generic_error_on_engine_failure(
    monkeypatch,
):
    """Regression: ``_trigger_assignment_tool`` must NOT echo the raw
    exception string back to the LLM caller — internal exception
    text can leak DB constraint names, connection-string hints, or
    upstream provider error payloads that contain sensitive
    identifiers.  The sibling handlers (``_update_*`` / ``_delete_*``)
    all return a generic 'please retry' message; the trigger handler
    must do the same.

    Pre-fix the handler did ``return f\"Failed to trigger assignment:
    {e!s}\"`` which leaked the full exception repr.  Post-fix it
    returns a stable, redaction-safe message that the LLM can parse
    consistently.
    """
    import app.services.assignment_engine as assignment_engine_module

    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    uid = uuid.UUID(user_id)
    assignment_id = uuid.uuid4()
    agent_id = uuid.uuid4()

    assignments = {
        assignment_id: SimpleNamespace(
            id=assignment_id,
            name="Doomed Assignment",
            user_id=uid,
            is_company_managed=False,
            agent_id=agent_id,
            enabled=True,
        )
    }
    agents = {
        agent_id: SimpleNamespace(
            id=agent_id,
            name="Doomed Agent",
            user_id=uid,
            enabled=True,
        )
    }
    created_sessions: list[_DedicatedAssignmentSession] = []

    def _session_factory():
        return _DedicatedAssignmentSession(
            created_sessions,
            assignments_by_id=assignments,
            agents_by_id=agents,
        )

    monkeypatch.setattr(database, "async_session", _session_factory)

    # ``_trigger_assignment_tool`` consults the caller's User row to gate
    # company-managed assignments (admin-only).  This test is about a
    # *user-owned* assignment so the gate must pass — stub the loader to
    # return any non-admin user.
    async def _fake_caller_user(_db, _uid):
        return SimpleNamespace(
            id=uid,
            role="user",
            is_admin=False,
        )

    monkeypatch.setattr(
        builtin_tools_module,
        "_load_caller_user_for_builder",
        _fake_caller_user,
    )

    # The handler calls ``get_user_group_ids`` before the lookups —
    # stub it to a fixed empty list so the test focuses on the
    # error-message contract, not membership resolution.
    async def _no_groups(*_args, **_kwargs):
        return []

    monkeypatch.setattr(api_helpers, "get_user_group_ids", _no_groups)
    # ``user_visible_filter`` returns a SQL expression that participates
    # in the agent lookup ``WHERE`` clause.  The fake session ignores
    # WHERE conditions and returns by bind value lookup, so the
    # default helper is safe to leave in place.

    # The handler's active-run guard queries by assignment_id — the fake
    # session's bind-value lookup would return the assignment row and
    # falsely report an active run, so stub the guard to pass.
    async def _no_active_run(_db, _assignment_id, **_kwargs):
        return None

    monkeypatch.setattr(
        assignment_engine_module, "find_active_run", _no_active_run
    )

    # The actual leak source — replace the real engine call with one
    # that raises a sensitive-looking exception.  The message
    # deliberately contains a fake DB constraint name + a fake
    # connection-string fragment to prove the new contract truly
    # redacts it before returning to the caller.
    sensitive_exc_msg = (
        "constraint 'assignments_agent_id_fkey' violated; "
        "host=internal-pg.internal.example.com user=copilot_app"
    )

    async def _exploding_trigger(*_args, **_kwargs):
        raise RuntimeError(sensitive_exc_msg)

    monkeypatch.setattr(
        assignment_engine_module, "trigger_assignment", _exploding_trigger
    )
    # Some import sites grab the symbol lazily inside the handler;
    # patching both the source module AND any cached re-export in
    # ``builtin_tools`` would belt-and-braces this — we only need
    # the source-module patch since the handler does a fresh
    # ``from app.services.assignment_engine import trigger_assignment``
    # at call time.

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="trigger_assignment",
            arguments={"assignment_id": str(assignment_id)},
        )
    )

    assert result.is_error, (
        "the tool should return cleanly with the error string as the "
        "tool result body, not raise into the executor"
    )

    body = result.content if isinstance(result.content, str) else str(result.content)
    # CONTRACT: the friendly message must include the human-readable
    # assignment name so the LLM/operator knows WHICH assignment
    # failed, and a 'please retry' suggestion that mirrors the
    # sibling handlers.
    assert "Failed to trigger assignment **Doomed Assignment**" in body, (
        f"expected the structured friendly error; got {body!r}"
    )
    assert "retry" in body.lower(), (
        f"the friendly error should suggest retrying; got {body!r}"
    )
    # CRITICAL: the raw exception string MUST NOT be in the body.
    # If a regression accidentally re-introduces ``f\"... {e!s}\"``,
    # the sensitive constraint name AND the fake host would surface.
    assert "assignments_agent_id_fkey" not in body, (
        "the raw exception repr leaked DB constraint names back to "
        f"the caller; body was {body!r}"
    )
    assert "internal-pg.internal.example.com" not in body, (
        "the raw exception repr leaked a connection-string fragment "
        f"back to the caller; body was {body!r}"
    )

    # CRITICAL: the dedicated session MUST have been rolled back
    # before being returned to the pool.  ``assignment_engine.
    # trigger_assignment`` performs writes/commits on the passed
    # session and does NOT roll back on its own failure path, so a
    # handler that just swallows the exception would leave the
    # session in a failed-transaction state and poison the pooled
    # connection.  Mirrors the rollback assertions on the sibling
    # ``_update_*`` / ``_delete_*`` commit-failure tests.
    assert len(created_sessions) == 1
    session = created_sessions[0]
    assert session.rollbacks == 1, (
        "the dedicated session must be rolled back on engine failure "
        "to avoid poisoning the pooled connection"
    )
    assert session.closed

    # Silence the unused-variable warning while keeping the leak-text
    # close to the assertion for readability.
    _ = sensitive_exc_msg


@pytest.mark.asyncio
async def test_trigger_assignment_refuses_when_active_run_exists(monkeypatch):
    """``_trigger_assignment_tool`` mirrors the REST API's
    ``trigger_assignment_manual`` active-run guard (HTTP 409): a
    chat-initiated trigger must not stack a second run while one is
    already pending, delayed, running, or awaiting human input.  The
    handler consults the shared ``find_active_run`` helper and returns a
    friendly refusal WITHOUT calling the engine, naming the run in flight
    so the caller can wait on it.
    """
    import app.services.assignment_engine as assignment_engine_module

    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    uid = uuid.UUID(user_id)
    assignment_id = uuid.uuid4()
    agent_id = uuid.uuid4()

    assignments = {
        assignment_id: SimpleNamespace(
            id=assignment_id,
            name="Busy Assignment",
            user_id=uid,
            is_company_managed=False,
            agent_id=agent_id,
            enabled=True,
        )
    }
    agents = {
        agent_id: SimpleNamespace(
            id=agent_id,
            name="Busy Agent",
            user_id=uid,
            enabled=True,
        )
    }
    created_sessions: list[_DedicatedAssignmentSession] = []

    def _session_factory():
        return _DedicatedAssignmentSession(
            created_sessions,
            assignments_by_id=assignments,
            agents_by_id=agents,
        )

    monkeypatch.setattr(database, "async_session", _session_factory)

    async def _no_groups(*_args, **_kwargs):
        return []

    monkeypatch.setattr(api_helpers, "get_user_group_ids", _no_groups)

    active_id = uuid.uuid4()

    async def _active_run(_db, _assignment_id, **_kwargs):
        return SimpleNamespace(
            id=active_id, status="running", created_at=None, started_at=None
        )

    monkeypatch.setattr(assignment_engine_module, "find_active_run", _active_run)

    async def _must_not_trigger(*_args, **_kwargs):
        raise AssertionError(
            "trigger_assignment must not be called when an active run exists"
        )

    monkeypatch.setattr(
        assignment_engine_module, "trigger_assignment", _must_not_trigger
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="trigger_assignment",
            arguments={"assignment_id": str(assignment_id)},
        )
    )

    assert not result.is_error
    body = result.content if isinstance(result.content, str) else str(result.content)
    assert "Busy Assignment" in body
    assert "already has an active run" in body
    assert f"Run `{active_id}` is running." in body
    assert "Wait for that run" in body


# ---------------------------------------------------------------------------
# create_knowledge_base — duplicate / IntegrityError / rollback hardening
#
# ``_create_knowledge_base`` runs inside its own dedicated session
# (``_builder_session()``) so that, even when the agent-builder tool is
# invoked alongside other concurrent DB activity, a duplicate-key path
# cannot poison the shared session and cause cascading
# ``PendingRollbackError`` failures in unrelated tools downstream.  The
# tests below pin down each branch of the hardening contract.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_knowledge_base_returns_existing_without_commit(monkeypatch):
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    user_uuid = uuid.UUID(user_id)
    created_sessions = []
    existing_kb = SimpleNamespace(id=uuid.uuid4(), name="Ops KB")

    def _session_factory():
        return _CreateKBDedicatedSession(
            created_sessions,
            execute_results=[existing_kb],
        )

    monkeypatch.setattr(database, "async_session", _session_factory)

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="create_knowledge_base",
            arguments={"name": "Ops KB", "description": "desc"},
        )
    )

    assert not result.is_error
    assert "already exists" in result.content
    assert str(existing_kb.id) in result.content
    assert len(created_sessions) == 1
    assert created_sessions[0].commits == 0
    assert created_sessions[0].rollbacks == 0
    assert created_sessions[0].added == []
    assert created_sessions[0].closed
    # The pre-check statement must filter by *both* the calling user's
    # id and the provided name — guards against a regression that
    # silently returns another user's KB on a name collision.
    assert len(created_sessions[0].executed_statements) == 1
    _assert_kb_lookup_filters_by_user_and_name(
        created_sessions[0].executed_statements[0],
        expected_user_id=user_uuid,
        expected_name="Ops KB",
    )
    # The lookup must be deterministic — ``ORDER BY created_at, id``
    # + ``LIMIT 1`` so multiple matching rows don't steer the LLM to a
    # different KB id across calls.
    _assert_kb_lookup_is_deterministic(
        created_sessions[0].executed_statements[0],
    )


@pytest.mark.asyncio
async def test_create_knowledge_base_rolls_back_on_integrity_error(monkeypatch):
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    user_uuid = uuid.UUID(user_id)
    created_sessions = []
    existing_kb = SimpleNamespace(id=uuid.uuid4(), name="Ops KB")

    def _session_factory():
        return _CreateKBDedicatedSession(
            created_sessions,
            execute_results=[None, existing_kb],
            fail_on_commit=True,
        )

    monkeypatch.setattr(database, "async_session", _session_factory)

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="create_knowledge_base",
            arguments={"name": "Ops KB", "description": "desc"},
        )
    )

    assert not result.is_error
    assert "already exists" in result.content
    assert str(existing_kb.id) in result.content
    assert len(created_sessions) == 1
    assert created_sessions[0].commits == 0
    assert created_sessions[0].rollbacks == 1
    assert len(created_sessions[0].added) == 1
    assert created_sessions[0].closed
    # Both the pre-check AND the race-window re-check after the
    # IntegrityError must filter by ``user_id`` and ``name`` — neither
    # branch is allowed to leak another user's KB — *and* both must
    # apply the same deterministic ORDER BY + LIMIT so duplicate rows
    # don't make the recovery branch return a different KB id each
    # time.
    assert len(created_sessions[0].executed_statements) == 2
    for stmt in created_sessions[0].executed_statements:
        _assert_kb_lookup_filters_by_user_and_name(
            stmt,
            expected_user_id=user_uuid,
            expected_name="Ops KB",
        )
        _assert_kb_lookup_is_deterministic(stmt)


@pytest.mark.asyncio
async def test_create_knowledge_base_happy_path_commits_once(monkeypatch):
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    user_uuid = uuid.UUID(user_id)
    created_sessions = []

    def _session_factory():
        return _CreateKBDedicatedSession(
            created_sessions,
            execute_results=[None],
        )

    monkeypatch.setattr(database, "async_session", _session_factory)

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="create_knowledge_base",
            arguments={"name": "Ops KB", "description": "desc"},
        )
    )

    assert not result.is_error
    assert "created successfully" in result.content
    assert len(created_sessions) == 1
    assert created_sessions[0].commits == 1
    assert created_sessions[0].rollbacks == 0
    assert len(created_sessions[0].added) == 1
    assert created_sessions[0].closed
    # Pre-check must have filtered by (user_id, name) and applied the
    # deterministic ORDER BY + LIMIT so the chosen "already exists" row
    # is stable across calls when duplicates are present.
    assert len(created_sessions[0].executed_statements) == 1
    _assert_kb_lookup_filters_by_user_and_name(
        created_sessions[0].executed_statements[0],
        expected_user_id=user_uuid,
        expected_name="Ops KB",
    )
    _assert_kb_lookup_is_deterministic(
        created_sessions[0].executed_statements[0],
    )
    # The created KB row must be owned by the calling user (and not by
    # whatever NULL/admin user the agent runs as).
    assert created_sessions[0].added[0].user_id == user_uuid
    assert created_sessions[0].added[0].name == "Ops KB"


@pytest.mark.asyncio
async def test_create_knowledge_base_rolls_back_on_generic_commit_error(
    monkeypatch, caplog
):
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    user_uuid = uuid.UUID(user_id)
    created_sessions = []

    def _session_factory():
        return _CreateKBDedicatedSession(
            created_sessions,
            execute_results=[None],
            commit_exception=RuntimeError("simulated generic commit failure"),
        )

    monkeypatch.setattr(database, "async_session", _session_factory)

    _, executor = build_agent_builder_tool(shared_db, user_id)
    with caplog.at_level("ERROR"):
        result = await executor.execute_tool(
            ToolCall(
                id=str(uuid.uuid4()),
                name="create_knowledge_base",
                arguments={"name": "Ops KB", "description": "desc"},
            )
        )

    assert result.is_error
    assert result.content == "Failed to create knowledge base. Please try again."
    assert len(created_sessions) == 1
    assert created_sessions[0].commits == 0
    assert created_sessions[0].rollbacks == 1
    assert len(created_sessions[0].added) == 1
    assert created_sessions[0].closed
    assert "Agent builder create_knowledge_base failed" in caplog.text
    # Even on the generic-failure path the pre-check must have filtered
    # by (user_id, name) before allocating the new row.
    assert len(created_sessions[0].executed_statements) == 1
    _assert_kb_lookup_filters_by_user_and_name(
        created_sessions[0].executed_statements[0],
        expected_user_id=user_uuid,
        expected_name="Ops KB",
    )


@pytest.mark.asyncio
async def test_create_knowledge_base_returns_friendly_error_when_pre_check_raises(
    monkeypatch, caplog
):
    """Regression: a transient DB error during the *pre-check* must
    still surface the friendly tool message instead of leaking the raw
    exception string back to the LLM (the pre-check used to sit outside
    the try/except)."""
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    created_sessions = []

    class _PreCheckExplodesSession(_CreateKBDedicatedSession):
        async def execute(self, statement):
            self.executed_statements.append(statement)
            raise RuntimeError("simulated transient DB error during pre-check")

    def _session_factory():
        return _PreCheckExplodesSession(
            created_sessions,
            execute_results=[],
        )

    monkeypatch.setattr(database, "async_session", _session_factory)

    _, executor = build_agent_builder_tool(shared_db, user_id)
    with caplog.at_level("ERROR"):
        result = await executor.execute_tool(
            ToolCall(
                id=str(uuid.uuid4()),
                name="create_knowledge_base",
                arguments={"name": "Ops KB", "description": "desc"},
            )
        )

    assert result.is_error
    assert result.content == "Failed to create knowledge base. Please try again."
    assert len(created_sessions) == 1
    # No commit happened, but the outer guard still attempted a
    # rollback and the session still closed cleanly.
    assert created_sessions[0].commits == 0
    assert created_sessions[0].rollbacks == 1
    assert created_sessions[0].added == []
    assert created_sessions[0].closed
    assert "Agent builder create_knowledge_base failed" in caplog.text


@pytest.mark.asyncio
async def test_create_knowledge_base_tolerates_pre_existing_duplicate_rows(
    monkeypatch,
):
    """Regression: the pre-check must tolerate pre-existing duplicates.

    ``(user_id, name)`` is intentionally **not** unique on
    ``KnowledgeBase`` — the comment block above the pre-check states this
    explicitly. The earlier implementation used
    ``existing_result.scalar_one_or_none()`` which raises
    :class:`sqlalchemy.exc.MultipleResultsFound` when the user already
    has multiple KBs with the same name, turning a usable
    "knowledge base already exists" response into the generic
    ``"Failed to create knowledge base. Please try again."`` error.
    Switching to ``existing_result.scalars().first()`` returns one of
    the duplicates and lets the LLM continue.
    """
    from sqlalchemy.exc import MultipleResultsFound

    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    created_sessions = []
    existing_kb = SimpleNamespace(id=uuid.uuid4(), name="Ops KB")

    class _DuplicateRowsResult:
        """Emulates a SQLAlchemy ``Result`` that has > 1 row.

        ``scalar_one_or_none()`` raises (the *previous* behaviour) while
        ``scalars().first()`` returns one of them (the *fixed* behaviour).
        """

        def scalar_one_or_none(self):
            raise MultipleResultsFound(
                "Multiple rows were found for one() — fix must not call this"
            )

        def scalars(self):
            return _ScalarsIterator(existing_kb)

    class _DuplicateRowsSession(_CreateKBDedicatedSession):
        async def execute(self, statement):
            self.executed_statements.append(statement)
            return _DuplicateRowsResult()

    def _session_factory():
        return _DuplicateRowsSession(
            created_sessions,
            execute_results=[],
        )

    monkeypatch.setattr(database, "async_session", _session_factory)

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="create_knowledge_base",
            arguments={"name": "Ops KB", "description": "desc"},
        )
    )

    # The tool must return the existing-KB message — *not* the generic
    # "Failed to create knowledge base" string the previous
    # ``scalar_one_or_none``-based code would have produced via the
    # outer exception guard.
    assert not result.is_error
    assert "already exists" in result.content
    assert str(existing_kb.id) in result.content
    assert "Failed to create knowledge base" not in result.content
    assert len(created_sessions) == 1
    # Pre-check only — no insert, no commit, no rollback.
    assert created_sessions[0].commits == 0
    assert created_sessions[0].rollbacks == 0
    assert created_sessions[0].added == []
    assert created_sessions[0].closed


@pytest.mark.asyncio
async def test_create_knowledge_base_returns_friendly_error_on_session_acquisition_failure(
    monkeypatch, caplog
):
    """Regression: a failure during session acquisition itself
    (``async with _builder_session().__aenter__`` raising — pool
    exhaustion, connection refused, driver bug) must surface the
    friendly tool message rather than leak the raw exception string
    back to the LLM through ``CallableToolExecutor``.

    The in-session try/except can't catch ``__aenter__`` failures
    because ``_db`` was never bound, so the implementation wraps the
    entire ``async with`` in an outer ``try / except Exception`` for
    this exact case. This test drives that outer guard.
    """
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())

    class _SessionAcquisitionError(RuntimeError):
        """Marker exception emulating an ``async with`` __aenter__ failure."""

    def _session_factory():
        # Mimic the worst case: the session-context-manager factory
        # itself raises *before* we can enter the ``async with`` block.
        # SQLAlchemy can raise here under pool exhaustion or when the
        # underlying engine has been disposed.
        raise _SessionAcquisitionError(
            "simulated pool exhaustion during session acquisition"
        )

    monkeypatch.setattr(database, "async_session", _session_factory)

    _, executor = build_agent_builder_tool(shared_db, user_id)
    with caplog.at_level("ERROR"):
        result = await executor.execute_tool(
            ToolCall(
                id=str(uuid.uuid4()),
                name="create_knowledge_base",
                arguments={"name": "Ops KB", "description": "desc"},
            )
        )

    # Without the outer guard the raw ``_SessionAcquisitionError``
    # string would surface to the LLM via ``CallableToolExecutor``.
    assert result.is_error
    assert result.content == "Failed to create knowledge base. Please try again."
    assert "simulated pool exhaustion" not in result.content
    # The dedicated outer log message helps operators tell session
    # acquisition failures apart from in-session DB errors.
    assert (
        "Agent builder create_knowledge_base failed to acquire session"
        in caplog.text
    )
    # And critically, the teardown-specific log line must NOT fire on the
    # acquisition path — it would otherwise mislead on-call into looking
    # at the wrong layer (close-on-exit instead of pool / connectivity).
    assert (
        "Agent builder create_knowledge_base session teardown failed"
        not in caplog.text
    )


@pytest.mark.asyncio
async def test_create_knowledge_base_returns_friendly_error_on_session_teardown_failure(
    monkeypatch, caplog
):
    """Regression: a failure during session **teardown**
    (``async with _builder_session().__aexit__`` raising — close-on-exit
    errors, driver-level disconnects after the in-session work ran) must
    surface the friendly tool message *and* log the teardown-specific
    line so on-call can tell it apart from session-acquisition failures.

    The outer ``except Exception`` guard catches both ``__aenter__`` and
    ``__aexit__`` failures, but a single shared log message would
    mislead on-call about which layer broke. The implementation uses a
    ``session_acquired`` flag to distinguish the two cases.
    """
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    user_uuid = uuid.UUID(user_id)
    created_sessions = []
    existing_kb = SimpleNamespace(id=uuid.uuid4(), name="Ops KB")

    class _TeardownExplodesSession(_CreateKBDedicatedSession):
        """Session whose body runs cleanly but ``__aexit__`` raises.

        Emulates a driver-level close failure that happens *after* the
        in-session block already produced a result. The outer guard
        must convert that to the friendly message and the
        teardown-specific log line must fire — not the acquisition one.
        """

        async def __aexit__(self, _exc_type, _exc, _tb):
            self.closed = True
            raise RuntimeError("simulated driver close failure during teardown")

    def _session_factory():
        # Pre-check finds an existing KB so the body returns cleanly
        # before teardown runs. This isolates the teardown branch — the
        # in-session try/except is never invoked.
        return _TeardownExplodesSession(
            created_sessions,
            execute_results=[existing_kb],
        )

    monkeypatch.setattr(database, "async_session", _session_factory)

    _, executor = build_agent_builder_tool(shared_db, user_id)
    with caplog.at_level("ERROR"):
        result = await executor.execute_tool(
            ToolCall(
                id=str(uuid.uuid4()),
                name="create_knowledge_base",
                arguments={"name": "Ops KB", "description": "desc"},
            )
        )

    # Even though the body returned the existing-KB message, ``__aexit__``
    # raising replaces the return value with the exception which the
    # outer guard converts to the friendly failure message — the LLM
    # never sees the raw close-error string.
    assert result.is_error
    assert result.content == "Failed to create knowledge base. Please try again."
    assert "simulated driver close failure" not in result.content
    assert len(created_sessions) == 1
    # The body did run: the pre-check filtered by (user_id, name) before
    # the teardown blew up.
    assert len(created_sessions[0].executed_statements) == 1
    _assert_kb_lookup_filters_by_user_and_name(
        created_sessions[0].executed_statements[0],
        expected_user_id=user_uuid,
        expected_name="Ops KB",
    )
    # Teardown-specific log line must fire — not the acquisition line.
    assert (
        "Agent builder create_knowledge_base session teardown failed"
        in caplog.text
    )
    assert (
        "Agent builder create_knowledge_base failed to acquire session"
        not in caplog.text
    )


# ---------------------------------------------------------------------------
# Permission gates — built-in agents, company-managed agents, group-shared
# entities. These tests pin down the security contract: the
# agent-builder tool must enforce the same gates as the REST API so the LLM
# cannot bypass UI-level restrictions (e.g. attaching an assignment to a
# read-only built-in agent like the XTM One Assistant).
# ---------------------------------------------------------------------------


def _patch_writable_gate_with_error(monkeypatch, *, error_message: str):
    """Force the writable-agent gate to deny the operation."""

    async def _fake(_db, _uid, _agent_uuid, *, action="modify", assignment_write=False):
        return None, error_message

    monkeypatch.setattr(
        builtin_tools_module, "_load_writable_agent_for_builder", _fake
    )


def _patch_writable_assignment_gate_with_error(
    monkeypatch, *, error_message: str
):
    async def _fake(_db, _uid, _assignment_uuid, *, action="modify"):
        return None, None, error_message

    monkeypatch.setattr(
        builtin_tools_module,
        "_load_writable_assignment_for_builder",
        _fake,
    )


@pytest.mark.asyncio
async def test_add_assignment_rejects_when_gate_returns_error(monkeypatch):
    """Regression: the ``create_assignment`` tool must surface the gate's
    refusal verbatim instead of silently creating an assignment on a
    read-only built-in/company-managed agent. A visibility check alone would
    let the LLM attach assignments to agents the UI marks read-only.
    """
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    created_sessions: list[_DedicatedToolSession] = []

    def _session_factory():
        return _DedicatedToolSession(created_sessions, "Built-in Agent")

    monkeypatch.setattr(database, "async_session", _session_factory)
    _patch_writable_gate_with_error(
        monkeypatch,
        error_message="Cannot add an assignment to built-in agents.",
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="create_assignment",
            arguments={
                "agent_id": str(uuid.uuid4()),
                "name": "Sneaky assignment",
                "prompt": "should never run",
            },
        )
    )

    assert not result.is_error
    assert "Cannot add an assignment to built-in agents" in result.content
    # No commit should have happened — the gate refusal must abort before
    # any writes hit the dedicated session.
    assert len(created_sessions) == 1
    assert created_sessions[0].commits == 0


@pytest.mark.asyncio
async def test_update_agent_rejects_when_gate_returns_error(monkeypatch):
    """Same security contract as ``test_add_assignment_rejects_*`` but
    for ``update_agent``: when the gate denies (built-in, company-managed
    without admin role, or read-only group share), the tool must NOT
    mutate the agent or commit.
    """
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    created_sessions: list[_DedicatedToolSession] = []

    def _session_factory():
        return _DedicatedToolSession(created_sessions, "Company-Managed Agent")

    monkeypatch.setattr(database, "async_session", _session_factory)
    _patch_writable_gate_with_error(
        monkeypatch,
        error_message="Only admins can edit company-managed agents.",
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_agent",
            arguments={"agent_id": str(uuid.uuid4()), "name": "Hijacked"},
        )
    )

    assert not result.is_error
    assert "Only admins can edit company-managed agents" in result.content
    assert len(created_sessions) == 1
    assert created_sessions[0].commits == 0
    # And the in-memory agent fixture must NOT have been mutated — if the
    # gate refusal is honoured the per-session ``agent.name`` stays at
    # the original value.
    assert created_sessions[0].agent.name == "Company-Managed Agent"


@pytest.mark.asyncio
async def test_delete_agent_rejects_when_gate_returns_error(monkeypatch):
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    agent_id = uuid.uuid4()
    created_sessions: list[_DedicatedAssignmentSession] = []
    agents = {
        agent_id: SimpleNamespace(
            id=agent_id,
            name="Read-only Agent",
            user_id=uuid.UUID(user_id),
        )
    }

    def _session_factory():
        return _DedicatedAssignmentSession(
            created_sessions,
            assignments_by_id={},
            agents_by_id=agents,
        )

    monkeypatch.setattr(database, "async_session", _session_factory)
    _patch_writable_gate_with_error(
        monkeypatch,
        error_message="Cannot delete built-in agents.",
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="delete_agent",
            arguments={"agent_id": str(agent_id)},
        )
    )

    assert not result.is_error
    assert "Cannot delete built-in agents" in result.content
    assert len(created_sessions) == 1
    # No DELETE statements should have been issued — the gate refusal
    # must short-circuit before any cascade query runs.
    delete_ops = [
        op
        for op in created_sessions[0].executed_statements
        if isinstance(op, tuple) and op[0] == "delete"
    ]
    assert delete_ops == []
    assert created_sessions[0].commits == 0


@pytest.mark.asyncio
async def test_update_assignment_rejects_when_gate_returns_error(monkeypatch):
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    assignment_id = uuid.uuid4()
    created_sessions: list[_DedicatedAssignmentSession] = []
    assignments = {
        assignment_id: SimpleNamespace(
            id=assignment_id,
            name="Original",
            prompt="",
            trigger_type="manual",
            trigger_config={},
            delay=None,
            enabled=True,
            user_id=None,  # company-managed assignment
        )
    }

    def _session_factory():
        return _DedicatedAssignmentSession(created_sessions, assignments)

    monkeypatch.setattr(database, "async_session", _session_factory)
    _patch_writable_assignment_gate_with_error(
        monkeypatch,
        error_message="Only admins can edit company-managed assignments.",
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_assignment",
            arguments={
                "assignment_id": str(assignment_id),
                "name": "Tampered",
            },
        )
    )

    assert not result.is_error
    assert "Only admins can edit company-managed assignments" in result.content
    assert len(created_sessions) == 1
    assert created_sessions[0].commits == 0
    # The in-memory assignment name must NOT have been mutated.
    assert assignments[assignment_id].name == "Original"


@pytest.mark.asyncio
async def test_delete_assignment_refuses_while_a_run_is_in_flight(monkeypatch):
    """``assignment_runs.assignment_id`` cascades, so deleting an
    assignment with a live run deleted that run's record (a run deleting its
    own inbox ended "no longer recorded"). The tool refuses, names the runs,
    issues no DELETE and commits nothing."""
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    assignment_id = uuid.uuid4()
    created_sessions: list[_DedicatedAssignmentSession] = []
    assignments = {
        assignment_id: SimpleNamespace(id=assignment_id, name="Inbox", user_id=None)
    }
    live_runs = [uuid.uuid4() for _ in range(7)]
    asked: list[uuid.UUID] = []

    async def _live(_db, aid):
        asked.append(aid)
        return live_runs[:6]

    def _session_factory():
        return _DedicatedAssignmentSession(created_sessions, assignments)

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(
        builtin_tools_module,
        "_load_writable_assignment_for_builder",
        _writable_assignment_from_session,
    )
    monkeypatch.setattr(builtin_tools_module, "_active_runs_blocking_delete", _live)

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="delete_assignment",
            arguments={"assignment_id": str(assignment_id)},
        )
    )

    assert asked == [assignment_id]
    assert "was not deleted" in result.content
    for run_id in live_runs[:5]:
        assert str(run_id) in result.content
    assert str(live_runs[5]) not in result.content
    assert "and more" in result.content
    assert "after_run=true" in result.content
    session = created_sessions[0]
    assert not [
        op for op in session.executed_statements if isinstance(op, tuple) and op[0] == "delete"
    ]
    assert session.commits == 0
    assert session.rollbacks == 1


@pytest.mark.asyncio
async def test_delete_assignment_runs_when_no_run_is_in_flight(monkeypatch):
    """With no live run the delete goes through as before."""
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    assignment_id = uuid.uuid4()
    created_sessions: list[_DedicatedAssignmentSession] = []
    assignments = {
        assignment_id: SimpleNamespace(id=assignment_id, name="Done", user_id=None)
    }

    async def _none(_db, _aid):
        return []

    def _session_factory():
        return _DedicatedAssignmentSession(created_sessions, assignments)

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(
        builtin_tools_module,
        "_load_writable_assignment_for_builder",
        _writable_assignment_from_session,
    )
    monkeypatch.setattr(builtin_tools_module, "_active_runs_blocking_delete", _none)

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="delete_assignment",
            arguments={"assignment_id": str(assignment_id)},
        )
    )

    assert "has been deleted" in result.content
    session = created_sessions[0]
    assert [op for op in session.executed_statements if isinstance(op, tuple)] == [
        ("delete", assignments[assignment_id])
    ]
    assert session.commits == 1


@pytest.mark.asyncio
async def test_delete_assignment_after_run_pauses_it_for_retirement(monkeypatch):
    """``after_run=true`` with a run in flight (the caller's own): no DELETE,
    the assignment is paused with ``retire_after_run`` and committed, and the
    answer says the deletion is scheduled at the end of that run."""
    from app.services.assignment_retire import RETIRE_AFTER_RUN_REASON

    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    assignment_id = uuid.uuid4()
    created_sessions: list[_DedicatedAssignmentSession] = []
    assignments = {
        assignment_id: SimpleNamespace(
            id=assignment_id, name="One-off", user_id=None, enabled=True
        )
    }
    own_run = uuid.uuid4()

    async def _live(_db, _aid):
        return [own_run]

    def _session_factory():
        return _DedicatedAssignmentSession(created_sessions, assignments)

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(
        builtin_tools_module,
        "_load_writable_assignment_for_builder",
        _writable_assignment_from_session,
    )
    monkeypatch.setattr(builtin_tools_module, "_active_runs_blocking_delete", _live)

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="delete_assignment",
            arguments={"assignment_id": str(assignment_id), "after_run": True},
        )
    )

    assert "scheduled at the end" in result.content
    assert str(own_run) in result.content
    stored = assignments[assignment_id]
    assert stored.enabled is False
    assert stored.paused_reason == RETIRE_AFTER_RUN_REASON
    session = created_sessions[0]
    assert not [
        op for op in session.executed_statements if isinstance(op, tuple) and op[0] == "delete"
    ]
    assert session.commits == 1


@pytest.mark.asyncio
async def test_delete_assignment_after_run_deletes_now_when_nothing_runs(monkeypatch):
    """``after_run=true`` with no run in flight is a plain delete."""
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    assignment_id = uuid.uuid4()
    created_sessions: list[_DedicatedAssignmentSession] = []
    assignments = {
        assignment_id: SimpleNamespace(id=assignment_id, name="Done", user_id=None)
    }

    async def _none(_db, _aid):
        return []

    def _session_factory():
        return _DedicatedAssignmentSession(created_sessions, assignments)

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(
        builtin_tools_module,
        "_load_writable_assignment_for_builder",
        _writable_assignment_from_session,
    )
    monkeypatch.setattr(builtin_tools_module, "_active_runs_blocking_delete", _none)

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="delete_assignment",
            arguments={"assignment_id": str(assignment_id), "after_run": "true"},
        )
    )

    assert "has been deleted" in result.content
    assert [
        op for op in created_sessions[0].executed_statements if isinstance(op, tuple)
    ] == [("delete", assignments[assignment_id])]


@pytest.mark.asyncio
async def test_delete_assignment_rejects_a_non_boolean_after_run():
    _, executor = build_agent_builder_tool(_SharedSession(), str(uuid.uuid4()))
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="delete_assignment",
            arguments={"assignment_id": str(uuid.uuid4()), "after_run": "later"},
        )
    )
    assert "after_run must be 'true' or 'false'" in result.content


@pytest.mark.asyncio
async def test_active_runs_blocking_delete_reads_the_active_statuses_of_that_assignment():
    """The check binds the assignment id and every active status of
    the engine, so it matches the overlap guard's notion of a live run."""
    from app.services.assignment_engine import ACTIVE_RUN_STATUSES
    from app.services.builtin_tools import agent_builder as agent_builder_module

    assignment_id = uuid.uuid4()
    live = [uuid.uuid4()]
    seen = []

    class _Session:
        async def execute(self, statement):
            seen.append(statement)
            return _ScalarResult(live)

    runs = await agent_builder_module._active_runs_blocking_delete(_Session(), assignment_id)

    assert runs == live
    binds = _extract_bind_values(seen[0])
    assert assignment_id in binds
    for status in ACTIVE_RUN_STATUSES:
        assert status in binds or any(
            isinstance(v, (list, tuple)) and status in v for v in binds
        )


@pytest.mark.asyncio
async def test_delete_assignment_rejects_when_gate_returns_error(monkeypatch):
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    assignment_id = uuid.uuid4()
    created_sessions: list[_DedicatedAssignmentSession] = []
    assignments = {
        assignment_id: SimpleNamespace(
            id=assignment_id,
            name="Doomed",
            user_id=None,
        )
    }

    def _session_factory():
        return _DedicatedAssignmentSession(created_sessions, assignments)

    monkeypatch.setattr(database, "async_session", _session_factory)
    _patch_writable_assignment_gate_with_error(
        monkeypatch,
        error_message=(
            "Cannot delete assignments on built-in agents."
        ),
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="delete_assignment",
            arguments={"assignment_id": str(assignment_id)},
        )
    )

    assert not result.is_error
    assert "Cannot delete assignments on built-in agents" in result.content
    assert len(created_sessions) == 1
    # The gate refusal must short-circuit before any DELETE is issued.
    delete_ops = [
        op
        for op in created_sessions[0].executed_statements
        if isinstance(op, tuple) and op[0] == "delete"
    ]
    assert delete_ops == []
    assert created_sessions[0].commits == 0


@pytest.mark.asyncio
async def test_trigger_assignment_blocks_company_managed_for_non_admin(
    monkeypatch,
):
    """Regression: non-admins must NOT be able to trigger company-managed
    assignments (``user_id IS NULL``) through the agent builder tool —
    same gate the REST API's ``trigger_assignment_manual`` enforces.
    """
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    uid = uuid.UUID(user_id)
    assignment_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    created_sessions: list[_DedicatedAssignmentSession] = []

    assignments = {
        assignment_id: SimpleNamespace(
            id=assignment_id,
            name="Company-Managed Assignment",
            user_id=None,  # company-managed
            is_company_managed=True,
            agent_id=agent_id,
            enabled=True,
        )
    }
    agents = {
        agent_id: SimpleNamespace(
            id=agent_id,
            name="Company Agent",
            user_id=None,
            enabled=True,
        )
    }

    def _session_factory():
        return _DedicatedAssignmentSession(
            created_sessions,
            assignments_by_id=assignments,
            agents_by_id=agents,
        )

    monkeypatch.setattr(database, "async_session", _session_factory)

    async def _no_groups(*_args, **_kwargs):
        return []

    monkeypatch.setattr(api_helpers, "get_user_group_ids", _no_groups)

    # Non-admin caller must be blocked.
    async def _fake_caller_user(_db, _uid):
        return SimpleNamespace(id=uid, role="user", is_admin=False)

    monkeypatch.setattr(
        builtin_tools_module,
        "_load_caller_user_for_builder",
        _fake_caller_user,
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="trigger_assignment",
            arguments={"assignment_id": str(assignment_id)},
        )
    )

    assert not result.is_error
    assert "Only admins can trigger company-managed assignments" in result.content


@pytest.mark.asyncio
async def test_trigger_assignment_allows_company_managed_for_admin(monkeypatch):
    """Admin callers MUST still be able to trigger company-managed
    assignments — the gate is admin-only, not anonymous-only.
    """
    import app.services.assignment_engine as assignment_engine_module

    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    uid = uuid.UUID(user_id)
    assignment_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    created_sessions: list[_DedicatedAssignmentSession] = []

    assignments = {
        assignment_id: SimpleNamespace(
            id=assignment_id,
            name="Company-Managed Assignment",
            user_id=None,
            is_company_managed=True,
            agent_id=agent_id,
            enabled=True,
        )
    }
    agents = {
        agent_id: SimpleNamespace(
            id=agent_id,
            name="Company Agent",
            user_id=None,
            enabled=True,
        )
    }

    def _session_factory():
        return _DedicatedAssignmentSession(
            created_sessions,
            assignments_by_id=assignments,
            agents_by_id=agents,
        )

    monkeypatch.setattr(database, "async_session", _session_factory)

    async def _fake_admin_caller(_db, _uid):
        return SimpleNamespace(id=uid, role="admin", is_admin=True)

    monkeypatch.setattr(
        builtin_tools_module,
        "_load_caller_user_for_builder",
        _fake_admin_caller,
    )

    async def _no_groups(*_args, **_kwargs):
        return []

    monkeypatch.setattr(api_helpers, "get_user_group_ids", _no_groups)

    # The handler's active-run guard queries by assignment_id — the fake
    # session's bind-value lookup would return the assignment row and
    # falsely report an active run, so stub the guard to pass.
    async def _no_active_run(_db, _assignment_id, **_kwargs):
        return None

    monkeypatch.setattr(
        assignment_engine_module, "find_active_run", _no_active_run
    )

    triggered_run_id = uuid.uuid4()

    async def _ok_trigger(*_args, **_kwargs):
        return triggered_run_id

    monkeypatch.setattr(
        assignment_engine_module, "trigger_assignment", _ok_trigger
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="trigger_assignment",
            arguments={"assignment_id": str(assignment_id)},
        )
    )

    assert not result.is_error
    assert "triggered successfully" in result.content
    assert str(triggered_run_id) in result.content


@pytest.mark.asyncio
async def test_writable_agent_error_built_in_in_xtm_one_mode(monkeypatch):
    """Unit test for the shared ``writable_agent_error`` helper.

    Built-in agents must be read-only when ``settings.is_xtm_one`` is
    truthy — even for admins. This is the single source of truth used by
    both the REST API (``assert_writable_agent``) and the agent builder
    tool (via ``_load_writable_agent_for_builder``), so a regression here
    would simultaneously re-open both bypass paths.
    """
    from app.api.v1.helpers import writable_agent_error
    from app.core.config import settings as core_settings

    monkeypatch.setattr(core_settings, "platform_mode", "xtm_one")

    admin = SimpleNamespace(id=uuid.uuid4(), role="admin", is_admin=True)
    builtin_agent = SimpleNamespace(
        id=uuid.uuid4(),
        is_builtin=True,
        is_company_managed=True,
        user_id=None,
        shared_group_ids=None,
        shared_group_permissions=None,
    )
    err = writable_agent_error(builtin_agent, admin, [], action="edit")
    assert err is not None
    # New active-voice phrasing (``Cannot <action> built-in agents.``)
    # replaces the old ``Built-in agents cannot be {action}d.`` template,
    # which produced ``editd`` / ``add an assignment tod`` for verb
    # phrases that don't pluralise by appending ``d``. Asserting the
    # exact rendered string here pins the contract so a future revert
    # of the template fails loudly.
    assert err == "Cannot edit built-in agents."


@pytest.mark.asyncio
async def test_writable_agent_error_company_managed_non_admin():
    """Company-managed agents must be read-only for non-admins regardless
    of platform mode — this is the gate the agent-builder tool was
    silently bypassing.
    """
    from app.api.v1.helpers import writable_agent_error

    non_admin = SimpleNamespace(id=uuid.uuid4(), role="user", is_admin=False)
    cm_agent = SimpleNamespace(
        id=uuid.uuid4(),
        is_builtin=False,
        is_company_managed=True,
        user_id=None,
        shared_group_ids=None,
        shared_group_permissions=None,
    )
    err = writable_agent_error(cm_agent, non_admin, [], action="edit")
    assert err is not None
    assert "Only admins" in err
    assert "company-managed" in err


@pytest.mark.asyncio
async def test_writable_agent_error_owner_allowed():
    """The owner of a private agent must always be allowed to mutate it."""
    from app.api.v1.helpers import writable_agent_error

    owner = SimpleNamespace(id=uuid.uuid4(), role="user", is_admin=False)
    private_agent = SimpleNamespace(
        id=uuid.uuid4(),
        is_builtin=False,
        is_company_managed=False,
        user_id=owner.id,
        shared_group_ids=None,
        shared_group_permissions=None,
    )
    assert writable_agent_error(private_agent, owner, [], action="edit") is None


@pytest.mark.asyncio
async def test_writable_assignment_error_company_managed_non_admin():
    """Mirrors ``writable_agent_error`` but for assignments — non-admins
    must NOT be able to edit/delete company-managed (``user_id IS NULL``)
    assignments, matching the REST API's ``update_assignment`` /
    ``delete_assignment`` gate.
    """
    from app.api.v1.helpers import writable_assignment_error

    non_admin = SimpleNamespace(id=uuid.uuid4(), role="user", is_admin=False)
    cm_assignment = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=None,
        is_company_managed=True,
    )
    cm_agent = SimpleNamespace(
        id=uuid.uuid4(),
        is_builtin=False,
        is_company_managed=True,
        user_id=None,
        shared_group_ids=None,
        shared_group_permissions=None,
    )
    err = writable_assignment_error(
        cm_assignment, cm_agent, non_admin, [], action="edit"
    )
    assert err is not None
    assert "Only admins" in err
    assert "company-managed assignments" in err


# ---------------------------------------------------------------------------
# Visibility leak guards — the loaders must not surface a permission-denied
# response for entities the caller cannot see in the first place. Returning
# a different message for "visible but read-only" vs "exists but private to
# another user" would let an attacker probe for the existence of agents /
# assignments owned by other users.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_writable_agent_loader_hides_invisible_agent_as_not_found(
    monkeypatch,
):
    """The agent loader must apply the three-tier visibility filter on
    the SELECT so callers can never distinguish "the agent doesn't exist"
    from "the agent exists but is private to another user". Both must
    surface as ``Agent <id> not found.`` — the visibility filter
    (``own + group-shared + company-managed``, with admin bypass) is what
    enforces this. Without the filter the loader would happily fetch any
    row by ID and then return the permission-denied gate string,
    confirming the row's existence to the LLM and any chat user driving
    the tool. This regression test fails if a future refactor removes
    the ``user_visible_filter`` from the SELECT.
    """
    uid = uuid.uuid4()
    agent_id = uuid.uuid4()

    # The fake session's ``execute`` returns ``self.agent`` for every
    # SELECT regardless of WHERE — setting ``agent`` to ``None`` emulates
    # the desired behaviour where the visibility filter eliminates the
    # row before SQLAlchemy materialises it. The recorded statements
    # let the assertion block below verify the predicate was applied.
    sess = _DedicatedToolSession([], "Unseen Agent")
    sess.agent = None  # type: ignore[assignment]

    # User exists (admin=False, so the visibility filter applies).
    async def _fake_caller_user(_db, _uid):
        return SimpleNamespace(id=uid, role="user", is_admin=False)

    monkeypatch.setattr(
        builtin_tools_module,
        "_load_caller_user_for_builder",
        _fake_caller_user,
    )

    async def _no_groups(*_args, **_kwargs):
        return []

    monkeypatch.setattr(api_helpers, "get_user_group_ids", _no_groups)

    agent_row, err = await builtin_tools_module._load_writable_agent_for_builder(
        sess,
        uid,
        agent_id,
        action="edit",
    )

    assert agent_row is None
    assert err is not None
    # CRITICAL: the message must be the not-found shape, NOT the
    # permission-denied shape. A regression that drops
    # ``user_visible_filter`` would return ``"Only admins can edit
    # company-managed agents."`` or similar — that's the existence leak.
    assert "not found" in err.lower(), (
        f"Loader must hide invisible agents behind a not-found "
        f"response, not surface a permission-denied gate message; "
        f"got {err!r}"
    )
    assert "permission" not in err.lower(), (
        "Permission-denied responses leak the existence of invisible "
        f"agents — must be hidden as not-found; got {err!r}"
    )

    # And the SELECT statement must carry the visibility predicate — a
    # regression that issues a plain ``WHERE id = ?`` SELECT without the
    # filter would still pass the assertions above when the row genuinely
    # doesn't exist, but would leak existence in production. Look at the
    # compiled SQL for the visibility-filter shape (own / company-managed
    # / OR-merged conditions).
    assert sess.executed_statements, "loader must have issued a SELECT"
    compiled_sql = str(
        sess.executed_statements[0].compile(compile_kwargs={"literal_binds": True})
    ).lower()
    # ``user_visible_filter`` for non-admin produces an OR clause covering
    # ``user_id = <uid>`` and ``is_company_managed IS true`` at minimum.
    assert "is_company_managed" in compiled_sql, (
        "SELECT must include the is_company_managed clause of "
        "user_visible_filter; otherwise the visibility check has been "
        f"dropped. Compiled SQL: {compiled_sql}"
    )


@pytest.mark.asyncio
async def test_writable_assignment_loader_hides_invisible_assignment_as_not_found(
    monkeypatch,
):
    """Same existence-hiding contract as the agent loader: an assignment
    that exists but is invisible to the caller must surface as
    ``Assignment <id> not found.``, not as the permission-denied gate
    string. The loader applies ``assignment_visible_filter`` (the predicate
    ``get_user_assignment`` uses: the parent agent is visible to the caller)
    so a regression that drops it would fail this test.
    """
    uid = uuid.uuid4()
    assignment_id = uuid.uuid4()

    sess = _DedicatedAssignmentSession(
        [],
        assignments_by_id={},  # No assignment visible to this user.
        agents_by_id={},
    )

    async def _fake_caller_user(_db, _uid):
        return SimpleNamespace(id=uid, role="user", is_admin=False)

    monkeypatch.setattr(
        builtin_tools_module,
        "_load_caller_user_for_builder",
        _fake_caller_user,
    )

    async def _no_groups(*_args, **_kwargs):
        return []

    monkeypatch.setattr(api_helpers, "get_user_group_ids", _no_groups)

    (
        assignment_row,
        agent_row,
        err,
    ) = await builtin_tools_module._load_writable_assignment_for_builder(
        sess,
        uid,
        assignment_id,
        action="edit",
    )

    assert assignment_row is None
    assert agent_row is None
    assert err is not None
    # CRITICAL: must be the not-found shape — never the permission-denied
    # gate message that would otherwise confirm existence.
    assert "not found" in err.lower(), (
        f"Loader must hide invisible assignments behind a not-found "
        f"response, not surface a permission-denied gate message; "
        f"got {err!r}"
    )
    assert "permission" not in err.lower(), (
        "Permission-denied responses leak the existence of invisible "
        f"assignments — must be hidden as not-found; got {err!r}"
    )
    # And the SELECT must carry the assignment visibility predicate
    # (matching ``get_user_assignment``: the parent agent's visibility).
    assert sess.executed_statements, "loader must have issued a SELECT"
    compiled_sql = str(
        sess.executed_statements[0].compile(compile_kwargs={"literal_binds": True})
    ).lower()
    # If a regression drops the visibility predicate the WHERE clause
    # collapses to just ``assignments.id = ?``.
    assert "from agents" in compiled_sql and "access_restricted" in compiled_sql, (
        "SELECT must restrict the assignment to one whose parent agent is "
        f"visible to the caller; compiled SQL: {compiled_sql}"
    )


@pytest.mark.asyncio
async def test_writable_agent_loader_admin_bypasses_visibility_filter(
    monkeypatch,
):
    """Admins must be able to load any agent through the builder tool —
    the visibility filter only applies to non-admins. Without the admin
    bypass, an admin chat session trying to edit a private agent (e.g.
    troubleshooting another user's setup) would get a misleading
    "not found" response.
    """
    uid = uuid.uuid4()
    agent_id = uuid.uuid4()

    # Mock session that returns the agent for any SELECT regardless of
    # the WHERE clause — emulating the admin-bypass path where the
    # visibility filter degenerates to ``TRUE``.
    sess = _DedicatedToolSession([], "Other User's Agent")
    sess.agent = SimpleNamespace(
        id=agent_id,
        name="Other User's Agent",
        is_builtin=False,
        is_company_managed=False,
        user_id=uuid.uuid4(),  # Owned by someone else.
        shared_group_ids=None,
        shared_group_permissions=None,
    )

    async def _fake_admin_caller(_db, _uid):
        return SimpleNamespace(id=uid, role="admin", is_admin=True)

    monkeypatch.setattr(
        builtin_tools_module,
        "_load_caller_user_for_builder",
        _fake_admin_caller,
    )

    async def _no_groups(*_args, **_kwargs):
        return []

    monkeypatch.setattr(api_helpers, "get_user_group_ids", _no_groups)

    agent_row, err = await builtin_tools_module._load_writable_agent_for_builder(
        sess,
        uid,
        agent_id,
        action="edit",
    )

    assert err is None, (
        f"Admin must be allowed to edit any visible agent; got err={err!r}"
    )
    assert agent_row is not None
    assert agent_row.id == agent_id


# ── Assignment ownership tracks parent agent owner ───────────────────────────


class _AddAssignmentSession:
    """Captures the ``Assignment`` row added by ``_add_assignment_to_agent``.

    Production code only calls ``add()`` + ``commit()`` after the writable-
    agent gate passes; no SELECTs run inside this code path (the agent is
    handed back by the gate). The fake therefore only needs to record the
    ORM object the tool tried to persist so the test can assert its
    ``user_id`` matches the agent owner — not the caller — even when the
    caller is a group-shared ``config``-tier member or an admin operating
    on someone else's private agent.
    """

    def __init__(self, created_sessions):
        self._created_sessions = created_sessions
        self._created_sessions.append(self)
        self.added: list[object] = []
        self.commits = 0
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        self.closed = True
        return False

    async def execute(self, _statement):  # pragma: no cover - unused in this path
        return _ScalarResult(None)

    def add(self, instance):
        self.added.append(instance)

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_add_assignment_owner_tracks_parent_agent_owner_not_caller(monkeypatch):
    """Regression: ``Assignment.user_id`` MUST track the parent agent's
    owner, not the caller's UID.

    Why this matters: ``Assignment.user_id`` is the owner the manage-tier
    checks read (``webhook_secret_visible`` hands the delivery secret to the
    assignment's owner). When a group-shared ``config``-tier member or an
    admin uses the builder tool to create an assignment on someone else's
    agent and ``user_id`` is bound to the caller, the caller becomes the
    owner of a row on an agent that is not theirs: downgraded to
    ``read_only`` on that agent, they would still receive its delivery
    secret. Binding to ``agent.user_id`` instead means the assignment always
    lives with the agent it's attached to.
    """
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())  # caller
    caller_uuid = uuid.UUID(user_id)
    agent_owner_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    created_sessions: list[_AddAssignmentSession] = []

    def _session_factory():
        return _AddAssignmentSession(created_sessions)

    monkeypatch.setattr(database, "async_session", _session_factory)

    fake_agent = SimpleNamespace(
        id=agent_id,
        name="Group-Shared Agent",
        is_company_managed=False,
        user_id=agent_owner_id,
    )

    async def _fake_gate(_db, _uid, _agent_uuid, *, action="modify", assignment_write=False):
        return fake_agent, None

    monkeypatch.setattr(
        builtin_tools_module, "_load_writable_agent_for_builder", _fake_gate
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="create_assignment",
            arguments={
                "agent_id": str(agent_id),
                "name": "Owner-visible assignment",
                "prompt": "do the thing",
                "trigger_type": "manual",
            },
        )
    )

    assert not result.is_error
    assert len(created_sessions) == 1
    session = created_sessions[0]
    assert session.commits == 1, "successful create must commit exactly once"
    assert len(session.added) == 1, (
        f"exactly one Assignment must be added; got {session.added}"
    )
    persisted = session.added[0]
    assert persisted.agent_id == agent_id
    assert persisted.user_id == agent_owner_id, (
        "Assignment.user_id must track the parent agent's owner; binding to "
        f"the caller ({caller_uuid}) would make them the owner of a row on "
        "someone else's agent. "
        f"got={persisted.user_id} expected={agent_owner_id}"
    )


def _add_assignment_gate_setup(monkeypatch, created_sessions):
    """Wire up ``create_assignment`` with an always-allow agent gate and a
    capturing session; returns the executor + agent id."""
    agent_id = uuid.uuid4()
    fake_agent = SimpleNamespace(
        id=agent_id,
        name="Agent",
        is_company_managed=False,
        user_id=uuid.uuid4(),
    )

    def _session_factory():
        return _AddAssignmentSession(created_sessions)

    async def _fake_gate(_db, _uid, _agent_uuid, *, action="modify", assignment_write=False):
        return fake_agent, None

    monkeypatch.setattr(database, "async_session", _session_factory)
    monkeypatch.setattr(
        builtin_tools_module, "_load_writable_agent_for_builder", _fake_gate
    )
    _, executor = build_agent_builder_tool(_SharedSession(), str(uuid.uuid4()))
    return executor, agent_id


@pytest.mark.asyncio
async def test_add_assignment_preserves_activation_gate_formatting(monkeypatch):
    """``create_assignment`` stores the activation gate verbatim (no trimming),
    matching ``update_assignment`` so create and update persist identical bytes
    for the same input and intentional leading/trailing whitespace survives."""
    created_sessions: list[_AddAssignmentSession] = []
    executor, agent_id = _add_assignment_gate_setup(monkeypatch, created_sessions)
    gate = "  Should I run? Answer YES or NO.\n"

    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="create_assignment",
            arguments={
                "agent_id": str(agent_id),
                "name": "Gated assignment",
                "prompt": "do the thing",
                "activation_prompt": gate,
                "trigger_type": "manual",
            },
        )
    )

    assert not result.is_error
    assert created_sessions[0].added[0].activation_prompt == gate  # verbatim


@pytest.mark.asyncio
async def test_add_assignment_sets_full_configuration(monkeypatch):
    """``create_assignment`` persists every behavioural setting, not just the
    handful it used to expose - it previously dropped ai_label, the prompt
    overrides, notifications, digest, dry-run, loop-protection, retries and the
    iteration / timeout limits."""
    created_sessions: list[_AddAssignmentSession] = []
    executor, agent_id = _add_assignment_gate_setup(monkeypatch, created_sessions)

    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="create_assignment",
            arguments={
                "agent_id": str(agent_id),
                "name": "Fully configured",
                "prompt": "do the thing",
                "trigger_type": "manual",
                "human_in_the_loop": False,
                "human_in_the_loop_prompt": "Custom HITL",
                "ai_label": False,
                "ai_label_prompt": "Custom AI label",
                "notify_on_complete": True,
                "show_in_digest": True,
                "dry_run": True,
                "ignore_self_triggered_events": True,
                "retry_on_failure": True,
                "max_retries": 7,
                "max_iterations": 25,
                "timeout_seconds": 900,
            },
        )
    )

    assert not result.is_error
    persisted = created_sessions[0].added[0]
    assert persisted.human_in_the_loop is False
    assert persisted.human_in_the_loop_prompt == "Custom HITL"
    assert persisted.ai_label is False
    assert persisted.ai_label_prompt == "Custom AI label"
    assert persisted.notify_on_complete is True
    assert persisted.show_in_digest is True
    assert persisted.dry_run is True
    assert persisted.ignore_self_triggered_events is True
    assert persisted.retry_on_failure is True
    assert persisted.max_retries == 7
    assert persisted.max_iterations == 25
    assert persisted.timeout_seconds == 900


@pytest.mark.asyncio
async def test_add_assignment_hitl_defaults_to_inherit(monkeypatch):
    """Omitting human_in_the_loop on create persists None (inherit from the
    agent), matching the tri-state REST CreateAssignmentRequest contract and the
    read/write parity update_assignment already has - not a forced True."""
    created_sessions: list[_AddAssignmentSession] = []
    executor, agent_id = _add_assignment_gate_setup(monkeypatch, created_sessions)

    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="create_assignment",
            arguments={
                "agent_id": str(agent_id),
                "name": "Inheriting HITL",
                "prompt": "do the thing",
                "trigger_type": "manual",
            },
        )
    )

    assert not result.is_error
    assert "Inherit from agent" in result.content
    persisted = created_sessions[0].added[0]
    assert persisted.human_in_the_loop is None


@pytest.mark.asyncio
async def test_add_assignment_max_iterations_defaults_to_inherit(monkeypatch):
    """Omitting max_iterations on create persists None (inherit: the assignment
    falls back to the agent's override, then the platform default) - it is not
    forced to a number and does not silently override the agent."""
    created_sessions: list[_AddAssignmentSession] = []
    executor, agent_id = _add_assignment_gate_setup(monkeypatch, created_sessions)

    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="create_assignment",
            arguments={
                "agent_id": str(agent_id),
                "name": "Inheriting iterations",
                "prompt": "do the thing",
                "trigger_type": "manual",
            },
        )
    )

    assert not result.is_error
    persisted = created_sessions[0].added[0]
    assert persisted.max_iterations is None


@pytest.mark.asyncio
async def test_add_assignment_max_iterations_overrides_agent(monkeypatch):
    """A real max_iterations on create is persisted as an explicit per-run
    override (clamped to the shared ceiling)."""
    created_sessions: list[_AddAssignmentSession] = []
    executor, agent_id = _add_assignment_gate_setup(monkeypatch, created_sessions)

    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="create_assignment",
            arguments={
                "agent_id": str(agent_id),
                "name": "Capped run",
                "prompt": "do the thing",
                "trigger_type": "manual",
                "max_iterations": 40,
            },
        )
    )

    assert not result.is_error
    persisted = created_sessions[0].added[0]
    assert persisted.max_iterations == 40


@pytest.mark.asyncio
async def test_add_assignment_none_prompt_overrides_clear_to_null(monkeypatch):
    """The 'none' sentinel (case-insensitive) on the create-path prompt overrides
    stores NULL so they fall back to the platform default - matching
    update_assignment and the documented schema behaviour."""
    created_sessions: list[_AddAssignmentSession] = []
    executor, agent_id = _add_assignment_gate_setup(monkeypatch, created_sessions)

    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="create_assignment",
            arguments={
                "agent_id": str(agent_id),
                "name": "Defaulted prompts",
                "prompt": "do the thing",
                "trigger_type": "manual",
                "human_in_the_loop_prompt": "NONE",
                "ai_label_prompt": "none",
            },
        )
    )

    assert not result.is_error
    persisted = created_sessions[0].added[0]
    assert persisted.human_in_the_loop_prompt is None
    assert persisted.ai_label_prompt is None


@pytest.mark.asyncio
async def test_add_assignment_rejects_out_of_range_limit(monkeypatch):
    """The safety/limit integers are range-guarded on create too, so the LLM
    can't persist a runaway iteration cap."""
    created_sessions: list[_AddAssignmentSession] = []
    executor, agent_id = _add_assignment_gate_setup(monkeypatch, created_sessions)

    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="create_assignment",
            arguments={
                "agent_id": str(agent_id),
                "name": "Bad limit",
                "prompt": "do the thing",
                "trigger_type": "manual",
                "max_iterations": 0,
            },
        )
    )

    assert "max_iterations" in result.content
    assert not created_sessions[0].added


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments,needle",
    [
        ({"ai_label": "false"}, "ai_label"),
        ({"enabled": "false"}, "enabled"),
        ({"notify_on_complete": "true"}, "notify_on_complete"),
        ({"dry_run": 1}, "dry_run"),
        ({"human_in_the_loop": "false"}, "human_in_the_loop"),
    ],
)
async def test_add_assignment_rejects_non_boolean_setting(
    monkeypatch, arguments, needle
):
    """create_assignment validates the boolean execution settings: a stringified
    "true"/"false" (truthy in Python) or other non-bool is rejected with an
    actionable error and never persisted, instead of writing a wrong value."""
    created_sessions: list[_AddAssignmentSession] = []
    executor, agent_id = _add_assignment_gate_setup(monkeypatch, created_sessions)

    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="create_assignment",
            arguments={
                "agent_id": str(agent_id),
                "name": "Bad bool",
                "prompt": "do the thing",
                "trigger_type": "manual",
                **arguments,
            },
        )
    )

    assert needle in result.content
    # Actionable guidance, and nothing persisted.
    assert "true" in result.content
    assert not created_sessions[0].added


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments,needle",
    [
        ({"activation_prompt": True}, "activation_prompt"),
        ({"description": 1}, "description"),
        ({"human_in_the_loop_prompt": 1}, "human_in_the_loop_prompt"),
        ({"ai_label_prompt": True}, "ai_label_prompt"),
        ({"delay": 1}, "delay"),
    ],
)
async def test_add_assignment_rejects_non_string_field(monkeypatch, arguments, needle):
    """create_assignment type-guards its string fields before any .strip(): a
    native non-string (tool kwargs are forwarded verbatim) returns an actionable
    '... must be a string.' error instead of crashing with the generic failure."""
    created_sessions: list[_AddAssignmentSession] = []
    executor, agent_id = _add_assignment_gate_setup(monkeypatch, created_sessions)

    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="create_assignment",
            arguments={
                "agent_id": str(agent_id),
                "name": "Bad string",
                "prompt": "do the thing",
                "trigger_type": "manual",
                **arguments,
            },
        )
    )

    assert needle in result.content
    assert "must be a string" in result.content
    # Failed fast before opening a session / writing anything.
    assert all(not s.added for s in created_sessions)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "delay_value,expected",
    [
        ("  none  ", None),
        ("   ", None),
        ("none", None),
        ("", None),
        ("  10m  ", "10m"),
    ],
)
async def test_add_assignment_normalizes_delay(monkeypatch, delay_value, expected):
    """create_assignment normalises delay like the other free-text fields: strip,
    and a blank / (padded) 'none' becomes NULL rather than persisting confusing
    whitespace or a literal 'none'."""
    created_sessions: list[_AddAssignmentSession] = []
    executor, agent_id = _add_assignment_gate_setup(monkeypatch, created_sessions)

    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="create_assignment",
            arguments={
                "agent_id": str(agent_id),
                "name": "Delayed",
                "prompt": "do the thing",
                "trigger_type": "manual",
                "delay": delay_value,
            },
        )
    )

    assert not result.is_error
    assert created_sessions[0].added[0].delay == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("gate_value", ["", "   ", "none", "  NONE  "])
async def test_add_assignment_blank_or_none_activation_gate_is_no_gate(
    monkeypatch, gate_value
):
    """An empty/whitespace gate, or the literal ``"none"``, means no gate (the
    activation prompt is persisted as NULL so the assignment always runs)."""
    created_sessions: list[_AddAssignmentSession] = []
    executor, agent_id = _add_assignment_gate_setup(monkeypatch, created_sessions)

    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="create_assignment",
            arguments={
                "agent_id": str(agent_id),
                "name": "Ungated assignment",
                "prompt": "do the thing",
                "activation_prompt": gate_value,
                "trigger_type": "manual",
            },
        )
    )

    assert not result.is_error
    assert created_sessions[0].added[0].activation_prompt is None


@pytest.mark.asyncio
async def test_add_assignment_company_managed_keeps_null_user_id(monkeypatch):
    """Sanity: company-managed parent agents still produce assignments
    with ``user_id=None`` (company-managed visibility). Pins the other
    branch of the ownership ternary alongside the owner-tracks-parent
    regression above.
    """
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    agent_id = uuid.uuid4()
    created_sessions: list[_AddAssignmentSession] = []

    def _session_factory():
        return _AddAssignmentSession(created_sessions)

    monkeypatch.setattr(database, "async_session", _session_factory)

    fake_agent = SimpleNamespace(
        id=agent_id,
        name="Company-Managed Agent",
        is_company_managed=True,
        user_id=None,
    )

    async def _fake_gate(_db, _uid, _agent_uuid, *, action="modify", assignment_write=False):
        return fake_agent, None

    monkeypatch.setattr(
        builtin_tools_module, "_load_writable_agent_for_builder", _fake_gate
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="create_assignment",
            arguments={
                "agent_id": str(agent_id),
                "name": "Company assignment",
                "prompt": "do the thing",
                "trigger_type": "manual",
            },
        )
    )

    assert not result.is_error
    assert len(created_sessions[0].added) == 1
    persisted = created_sessions[0].added[0]
    assert persisted.user_id is None, (
        "Company-managed parent agent must produce a company-managed "
        f"assignment (user_id=None); got {persisted.user_id}"
    )


# ── Built-in KB protection on add_kb_document ────────────────────────────────


class _BuiltinKBSession:
    """Returns a built-in KB on the second SELECT, captures any mutation.

    The production code must SELECT the KB with the visibility filter
    applied (so non-visible KBs surface as not-found, mirroring
    ``get_user_knowledge_base``) and then short-circuit on
    ``kb.is_builtin`` BEFORE running ``can_manage`` or
    ``_db.add(KnowledgeDocument(...))`` / ``_db.commit()`` /
    ``enqueue_document_processing(...)``. The fake therefore records
    every interaction (executes / adds / commits) so the test can assert
    none of the mutating ones happen after the SELECT that surfaced the
    built-in KB.

    Returns ``[]`` for the group-ids SELECT (first call) and the KB row
    on subsequent SELECTs so the production code's
    ``get_user_group_ids`` + KB query path both succeed.
    """

    def __init__(self, kb):
        self._kb = kb
        self.added: list[object] = []
        self.commits = 0
        self.executes = 0
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        self.closed = True
        return False

    async def execute(self, _statement):
        self.executes += 1
        # The first call inside _add_kb_document is
        # get_user_group_ids → returns an empty list of group ids.
        # The second is the KB SELECT → returns the built-in KB.
        if self.executes == 1:
            return _ScalarResult([])
        return _ScalarResult(self._kb)

    def add(self, instance):
        self.added.append(instance)

    async def commit(self):
        self.commits += 1


class _InvisibleKBSession:
    """Simulates ``user_visible_filter`` excluding the requested KB.

    Returns ``[]`` for the first SELECT (``get_user_group_ids``) and
    ``None`` for the second SELECT (the visibility-filtered KB query),
    matching what the production code sees when the caller asks for a
    KB they cannot see.
    """

    def __init__(self):
        self.added: list[object] = []
        self.commits = 0
        self.executes = 0
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        self.closed = True
        return False

    async def execute(self, _statement):
        self.executes += 1
        if self.executes == 1:
            return _ScalarResult([])
        return _ScalarResult(None)

    def add(self, instance):
        self.added.append(instance)

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_add_kb_document_refuses_built_in_kb(monkeypatch):
    """Built-in KBs are seeded at startup and immutable for everyone per
    the ``KnowledgeBase`` model contract (model docstring line 70-72).
    The builder tool must refuse before any ``can_manage`` check,
    document insert, ``document_count`` bump, ``_db.commit()``, or
    background ``enqueue_document_processing()`` runs — those would all
    silently violate the "cannot be modified" guarantee.

    Caller lookup *is* allowed to run before the built-in check: the
    visibility filter on the KB SELECT needs ``caller.is_admin`` to
    apply the admin bypass correctly, and without it admin users would
    incorrectly see private KBs of other users as "not found". The
    tripwires that matter for the immutability contract — no insert,
    no commit, no enqueue, ``document_count`` unchanged — are still
    asserted below.
    """
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    kb_id = uuid.uuid4()

    builtin_kb = SimpleNamespace(
        id=kb_id,
        name="Built-in Skills",
        is_builtin=True,
        is_company_managed=True,  # most built-in KBs are also company-managed
        user_id=None,
        shared_group_ids=None,
        shared_group_permissions=None,
        document_count=42,
    )

    session = _BuiltinKBSession(builtin_kb)
    monkeypatch.setattr(database, "async_session", lambda: session)

    async def _fake_caller(*_args, **_kwargs):
        return SimpleNamespace(id=uuid.UUID(user_id), is_admin=True, role="admin")

    monkeypatch.setattr(
        builtin_tools_module,
        "_load_caller_user_for_builder",
        _fake_caller,
    )

    enqueued: list[str] = []

    async def _trip_enqueue(doc_id, **_kwargs):
        enqueued.append(doc_id)

    monkeypatch.setattr(
        "app.services.kb_helpers.enqueue_document_processing",
        _trip_enqueue,
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="add_kb_document",
            arguments={
                "knowledge_base_id": str(kb_id),
                "title": "Sneaky doc",
                "content": "should never persist",
            },
        )
    )

    assert not result.is_error
    assert "built-in knowledge bases" in result.content.lower(), (
        f"expected built-in refusal, got: {result.content}"
    )
    # No document inserted, no commit, no enqueue — and the KB's
    # existing document_count must NOT be bumped.
    assert session.added == []
    assert session.commits == 0
    assert enqueued == []
    assert builtin_kb.document_count == 42


@pytest.mark.asyncio
async def test_add_kb_document_hides_invisible_kb_as_not_found(monkeypatch):
    """Regression: ``_add_kb_document`` previously loaded the KB by ID
    with no visibility filter, then ran the built-in / company-managed
    gates.  A caller could probe for the existence of any KB UUID and
    the response (``"Cannot add documents to built-in ..."`` /
    ``"Only admins can add documents ..."``) would leak the existence
    of KBs they cannot see — same bug class fixed for agents and
    assignments.

    The loader must now apply ``user_visible_filter(KnowledgeBase, ...)``
    so non-visible KBs surface as the generic ``"not found or you don't
    have access."`` response, matching the REST API's
    ``get_user_knowledge_base`` 404 contract.

    Tripwires also assert no document insert, no commit, no enqueue
    runs on the rejection path so a regression that drops the filter
    AND triggers a mutation fails loudly on more than one axis.
    """
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    kb_id = uuid.uuid4()

    session = _InvisibleKBSession()
    monkeypatch.setattr(database, "async_session", lambda: session)

    async def _fake_caller(*_args, **_kwargs):
        # Non-admin so user_visible_filter is NOT bypassed.
        return SimpleNamespace(id=uuid.UUID(user_id), is_admin=False, role="user")

    monkeypatch.setattr(
        builtin_tools_module,
        "_load_caller_user_for_builder",
        _fake_caller,
    )

    enqueued: list[str] = []

    async def _trip_enqueue(doc_id, **_kwargs):
        enqueued.append(doc_id)

    monkeypatch.setattr(
        "app.services.kb_helpers.enqueue_document_processing",
        _trip_enqueue,
    )

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="add_kb_document",
            arguments={
                "knowledge_base_id": str(kb_id),
                "title": "Probe",
                "content": "should never persist",
            },
        )
    )

    assert not result.is_error
    lowered = result.content.lower()
    assert "not found" in lowered, (
        f"expected the generic not-found response that hides existence, "
        f"got: {result.content}"
    )
    # The response must NOT distinguish "exists but you can't see it"
    # from "does not exist" — i.e. no leak of built-in / company-managed
    # / permission-denied vocabulary specific to the invisible KB.
    assert "built-in" not in lowered, (
        "must not leak that the KB exists and is built-in"
    )
    assert "company-managed" not in lowered, (
        "must not leak that the KB exists and is company-managed"
    )
    assert "permission" not in lowered, (
        "must not leak that the KB exists with a permission-specific error"
    )
    assert session.added == []
    assert session.commits == 0
    assert enqueued == []


# ---------------------------------------------------------------------------
# Invalid-UUID input hardening across the whole agent-builder surface.
#
# Every tool that accepts an id used to call ``uuid.UUID(value)`` inline, so a
# hallucinated/truncated id raised an uncaught ``ValueError`` (or, in
# ``create_assignment``, got swallowed into a misleading "Failed to add
# assignment" with an error-level stack trace). The fix validates the id once,
# *before* opening a DB session, and returns an actionable correction.
# ---------------------------------------------------------------------------

_INVALID_ID = "not-a-uuid"

_ID_TOOL_CASES = [
    ("get_agent", {"agent_id": _INVALID_ID}, "agent_id"),
    ("update_agent", {"agent_id": _INVALID_ID, "name": "X"}, "agent_id"),
    ("delete_agent", {"agent_id": _INVALID_ID}, "agent_id"),
    (
        "create_assignment",
        {"agent_id": _INVALID_ID, "name": "N", "prompt": "P"},
        "agent_id",
    ),
    ("list_assignments", {"agent_id": _INVALID_ID}, "agent_id"),
    ("get_assignment", {"assignment_id": _INVALID_ID}, "assignment_id"),
    ("update_assignment", {"assignment_id": _INVALID_ID, "name": "X"}, "assignment_id"),
    ("delete_assignment", {"assignment_id": _INVALID_ID}, "assignment_id"),
    ("trigger_assignment", {"assignment_id": _INVALID_ID}, "assignment_id"),
    (
        "add_kb_document",
        {"knowledge_base_id": _INVALID_ID, "title": "T", "content": "C"},
        "knowledge_base_id",
    ),
    ("get_custom_tool", {"tool_id": _INVALID_ID}, "tool_id"),
]


def test_validate_id_arg_accepts_valid_and_rejects_invalid():
    valid = str(uuid.uuid4())
    parsed, err = builtin_tools_module._validate_id_arg(valid, "agent_id")
    assert err is None
    assert str(parsed) == valid

    parsed, err = builtin_tools_module._validate_id_arg("nope", "agent_id")
    assert parsed is None
    assert "agent_id must be a valid UUID" in err
    assert "nope" in err

    # Empty string must be rejected too — ``safe_uuid`` short-circuits before
    # ``uuid.UUID`` so there is no AttributeError leak.
    parsed, err = builtin_tools_module._validate_id_arg("", "tool_id")
    assert parsed is None
    assert "tool_id must be a valid UUID" in err

    # ``None`` must be rejected without leaking an exception.
    parsed, err = builtin_tools_module._validate_id_arg(None, "agent_id")
    assert parsed is None
    assert "agent_id must be a valid UUID" in err

    # Non-string JSON/LLM values (e.g. bytes, int, dict) must also be treated
    # as invalid rather than raising a TypeError out of ``uuid.UUID`` — tool
    # arguments are untrusted, so the guard must not crash on them.
    for bad in (b"550e8400-e29b-41d4-a716-446655440000", 1234, {"x": "y"}, ["id"]):
        parsed, err = builtin_tools_module._validate_id_arg(bad, "assignment_id")
        assert parsed is None
        assert "assignment_id must be a valid UUID" in err


def test_validate_id_arg_bounds_huge_value_display():
    """A hallucinated huge value must not inflate the error string: the echoed
    value is bounded (long strings sliced + ``…``; large containers shown as a
    ``<type>`` placeholder, never serialized)."""
    big = "x" * 10_000
    _, err = builtin_tools_module._validate_id_arg(big, "agent_id")
    assert err is not None and err.endswith("…")
    # Bounded well under the raw input — only ~budget chars are echoed.
    assert len(err) < 300
    assert big not in err

    # Large containers/objects are shown as a type placeholder, not walked.
    _, err = builtin_tools_module._validate_id_arg(list(range(10_000)), "agent_id")
    assert "<list>" in err
    assert len(err) < 300
    _, err = builtin_tools_module._validate_id_arg(
        {str(i): i for i in range(5_000)}, "agent_id"
    )
    assert "<dict>" in err
    assert len(err) < 300


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name, arguments, arg_label", _ID_TOOL_CASES)
async def test_agent_builder_id_tools_reject_invalid_uuid(
    monkeypatch, tool_name, arguments, arg_label
):
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())
    created_sessions: list = []

    def _session_factory():
        return _DedicatedToolSession(created_sessions, "unused")

    monkeypatch.setattr(database, "async_session", _session_factory)

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(id=str(uuid.uuid4()), name=tool_name, arguments=arguments)
    )

    assert not result.is_error
    assert f"{arg_label} must be a valid UUID" in result.content
    assert "550e8400-e29b-41d4-a716-446655440000" in result.content
    assert repr(_INVALID_ID) in result.content
    assert created_sessions == [], (
        f"{tool_name} must reject an invalid {arg_label} before opening a DB session"
    )


@pytest.mark.asyncio
async def test_list_assignments_allows_empty_agent_id(monkeypatch):
    """``agent_id`` is optional for list_assignments — an empty value must not
    be rejected by the UUID guard (it means "across all visible agents")."""
    shared_db = _SharedSession()
    user_id = str(uuid.uuid4())

    class _EmptyResult:
        def fetchall(self):
            return []

        def scalars(self):
            return _ScalarsIterator(None)

        def scalar_one_or_none(self):
            return None

    class _EmptyRowsSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def execute(self, _statement):
            return _EmptyResult()

    monkeypatch.setattr(database, "async_session", lambda: _EmptyRowsSession())

    _, executor = build_agent_builder_tool(shared_db, user_id)
    result = await executor.execute_tool(
        ToolCall(id=str(uuid.uuid4()), name="list_assignments", arguments={})
    )

    assert not result.is_error
    assert "must be a valid UUID" not in result.content
    assert result.content == "No assignments found."
