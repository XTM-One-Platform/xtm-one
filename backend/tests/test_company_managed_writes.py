"""Company-managed shared state and work items are written only by company-managed
writers.

Everyone who can use a company-managed agent reads what it shares; only a
company-managed agent (whoever it runs for), a company-managed flow, an admin on
the REST API or the platform's own run bookkeeping may write it. These tests pin
the rule in the two services (the funnel every surface goes through), in the
built-in tools an agent writes through, and in the wiring that decides which
writer an agent is. No DB: the services' sessions are replaced by a fake that
serves one row and records what would have been written.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import app.services.shared_state as shared_state_service
import app.services.work_item as wi_service
from app.services.builtin_tools import build_shared_state_tools, build_work_item_tools
from app.services.company_managed_writes import (
    PLATFORM_WRITER,
    CompanyManagedWriteRefusedError,
    agent_writer,
    flow_writer,
    person_writer,
)
from app.tools.base import ToolCall

PERSONAL_AGENT = agent_writer(is_company_managed=False)
COMPANY_AGENT = agent_writer(is_company_managed=True)
APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"


# ── Fakes ──────────────────────────────────────────────────────────────────────


class _RowResult:
    def __init__(self, row):
        self._row = row

    def scalars(self):
        return self

    def first(self):
        return self._row

    def all(self):
        return [self._row] if self._row is not None else []

    def scalar_one_or_none(self):
        return self._row


class _RowSession:
    """Every query answers *row*; adds, deletes and commits are recorded."""

    def __init__(self, row):
        self.row = row
        self.added: list = []
        self.deleted: list = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, _model, _ident):
        # The workspace tiers look the caller up first; an unknown user has
        # no workspace tier and executes nothing.
        return None

    async def execute(self, statement):
        return _RowResult(self.row)

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        pass

    async def refresh(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()


def _use_session(monkeypatch, row) -> _RowSession:
    session = _RowSession(row)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    return session


def _no_session(monkeypatch) -> None:
    def _boom():
        raise AssertionError("a refused write must not open a session")

    monkeypatch.setattr("app.core.database.async_session", _boom)


def _state(**over) -> SimpleNamespace:
    old = datetime.now(UTC) - timedelta(days=60)
    base = dict(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        is_company_managed=True,
        workspace_id=None,
        key="fleet-incident",
        label=None,
        owner_agent_id=None,
        status="active",
        value={"stage": "triage"},
        notes=[],
        resolved_at=None,
        created_at=old,
        updated_at=old,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _item(**over) -> SimpleNamespace:
    now = datetime.now(UTC)
    base = dict(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        is_company_managed=True,
        owner_agent_id=uuid.uuid4(),
        created_by_agent_id=None,
        title="Contain the incident",
        description=None,
        payload={},
        status="open",
        priority="high",
        correlation_key=None,
        due_at=None,
        claimed_by_agent_id=None,
        claimed_at=None,
        source="agent",
        result=None,
        last_activity_at=now,
        created_at=now,
        updated_at=now,
    )
    base.update(over)
    return SimpleNamespace(**base)


async def _call(executor, name, /, **arguments):
    return await executor.execute_tool(
        ToolCall(id=str(uuid.uuid4()), name=name, arguments=arguments)
    )


# ── The rule ───────────────────────────────────────────────────────────────────


def test_writers_and_their_refusals():
    assert person_writer(is_admin=True).may_write
    assert not person_writer(is_admin=False).may_write
    assert COMPANY_AGENT.may_write and not PERSONAL_AGENT.may_write
    assert flow_writer(is_company_managed=True).may_write
    assert not flow_writer(is_company_managed=False).may_write
    assert PLATFORM_WRITER.may_write
    # The REST wording is the one the routes always answered with.
    assert person_writer(is_admin=False).allowed == "admins"
    assert PERSONAL_AGENT.allowed == "company-managed agents"
    assert flow_writer(is_company_managed=False).allowed == "company-managed flows"


_WRITE_FUNCTIONS = {
    shared_state_service: (
        "upsert_state",
        "resolve_state",
        "resolve_state_by_id",
        "delete_state",
        "delete_state_by_id",
    ),
    wi_service: (
        "create_work_item",
        "update_work_item",
        "claim_work_item",
        "complete_work_item",
        # A person answers the decision an item waits for (#9437).
        "answer_work_item",
        # A person sends Re-check, It's solved or Do it to the item's agent (#774).
        "ask_work_item",
        # The inbox run's end: completes, keeps or parks the item.
        "settle_inbox_work_item",
        "requeue_work_item",
        "release_work_item_claim",
        "delete_work_item",
        "list_dispatchable_work_items",
    ),
}
# Reads, the REST bulk delete (scoped by ``_deletable_clause`` from the route's
# own ``is_admin``) and the retention sweeps.
_NOT_WRITER_GATED = {
    shared_state_service: {
        "get_state",
        "get_state_by_id",
        "list_states",
        "count_states",
        "count_states_by_owner",
        "bulk_delete_states",
        "purge_resolved_states",
        # The archive retention sweep of the scheduler (#10789): drops only the
        # items ``archive_key`` stamped, past the retention, in every entry.
        "purge_archived_items",
        # The scheduler clearing the holds of runs that ended: a sweep,
        # like ``work_item.release_ended_run_claims``.
        "release_ended_run_holds",
        # The hygiene sweeps of the scheduler (#11696): resolve entries whose
        # linked work item ended or whose date passed, compact oversize ones.
        "resolve_terminal_linked_states",
        "resolve_expired_states",
        "compact_oversize_states",
        "sweep_shared_state_lifecycle",
        "shared_state_hygiene_counts",
        "visible_shared_state_hygiene_counts",
        "report_shared_state_hygiene",
    },
    wi_service: {
        "get_work_item",
        # The id-prefix resolver of the built-in tools: a read under the
        # visibility clause of ``get_work_item``.
        "work_items_by_id_prefix",
        # The attachment resolver of read_file_content: the items visible
        # to the caller that attach a file, read under the same clause.
        "work_items_attaching",
        "list_work_items",
        # The graph view's blockers outside the filter (#968), read under
        # the visibility clause of ``list_work_items``.
        "list_visible_work_items_by_ids",
        "count_work_items",
        # One GROUP BY per field value over the same filters (#12105).
        "count_work_items_grouped",
        "count_work_items_by_owner",
        "count_work_items_by_status",
        "count_stale_work_items",
        # The dashboard notice: the caller's own items that wait for them.
        "work_items_waiting_for_person",
        # The bell's rows of the same population (#9437).
        "list_work_items_waiting_for_person",
        # The fleet health read: open items per owner agent.
        "open_work_item_counts",
        # What the views say will pick an item up: the inboxes and
        # the dispatch's own clauses, read on the caller's session.
        "load_assignment_inboxes",
        "accepted_by_inbox",
        "existing_correlation_keys",
        "bulk_delete_work_items",
        "reap_work_items",
        # The platform's claim bookkeeping: the holder's state read, the
        # release of the claims of runs that ended and of claims no live run
        # holds (sweeps, like the reaper) and the dispatcher naming the run it
        # just created as the holder of a claim ``claim_work_item`` already
        # gated with the inbox's writer.
        "holding_runs",
        "release_ended_run_claims",
        "release_orphaned_legacy_claims",
        "record_claim_run",
        # The inbox run's finalizer parking the item it holds and could not
        # reach (#11900): bound to its own claim, like record_claim_run.
        "park_unreachable_work_item",
        # The finalizer's read of whether the run's own tools reach the
        # item (#11943): the claim's admission, read only, no write.
        "reachable_by_run_tools",
        # The dialog's and the write routes' read of whether a person may
        # write an item they can read (#1845): two clauses read, no write.
        "person_write_access",
        # The detail read of whether a person may cancel (#1910): a read, no write.
        "person_may_cancel",
        # The scheduler reopening items whose blockers are done: a
        # sweep, like the reaper.
        "release_satisfied_dependencies",
        # The same sweep narrowed to the waiters of an item its caller's
        # gated write just finished (#11711).
        "release_dependants_of",
        # The atomic claim statement on its caller's session: ``claim_work_item``
        # gates it with its writer first, the scheduled-item dispatcher
        # is the platform claiming items no company-managed rule applies to.
        "claim_open_work_item",
        # Publishes the platform events of a write already committed
        # and gated by its caller; it reads the row and writes nothing.
        "publish_transition",
    },
}


@pytest.mark.parametrize("module", list(_WRITE_FUNCTIONS), ids=lambda m: m.__name__)
def test_every_write_function_requires_its_writer(module):
    """A write function takes ``writer`` keyword-only and without a default, so
    no caller can reach a company-managed row without saying who it is; a new
    public coroutine must be classified here."""
    for name in _WRITE_FUNCTIONS[module]:
        param = inspect.signature(getattr(module, name)).parameters.get("writer")
        assert param is not None, f"{name} takes no writer"
        assert param.kind is inspect.Parameter.KEYWORD_ONLY, name
        assert param.default is inspect.Parameter.empty, f"{name} defaults its writer"
    public = {
        name
        for name, fn in vars(module).items()
        if inspect.iscoroutinefunction(fn)
        and not name.startswith("_")
        and fn.__module__ == module.__name__
    }
    unclassified = public - set(_WRITE_FUNCTIONS[module]) - _NOT_WRITER_GATED[module]
    assert not unclassified, f"classify {sorted(unclassified)} in this test"


_AGENT_WRITER_BUILDERS = {
    "build_work_item_tools",
    "build_shared_state_tools",
    # The oversized knowledge-document offload creates a work item.
    "build_kb_document_read_tools",
}


@pytest.mark.parametrize(
    ("path", "builders"),
    [
        ("services/agent_chat.py", _AGENT_WRITER_BUILDERS),
        ("services/assignment_engine.py", _AGENT_WRITER_BUILDERS),
    ],
)
def test_agent_paths_write_as_the_agent_itself(path, builders):
    """Chat turns (every caller of run_agent_chat: channels, sub-agent tasks,
    flow agent steps) and assignment runs build the tools with the acting
    agent's OWN tier - never the run's visibility or the user's role."""
    tree = ast.parse((APP_DIR / path).read_text(encoding="utf-8"))
    seen = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in builders:
            continue
        seen.add(node.func.id)
        kwargs = {k.arg: ast.unparse(k.value) for k in node.keywords}
        assert kwargs.get("company_managed_agent") == "agent.is_company_managed", (
            f"{path}: {node.func.id} at line {node.lineno}"
        )
    assert seen == builders


# ── Shared state: the service gate ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_personal_agent_cannot_merge_into_a_company_managed_entry(monkeypatch):
    """An upsert of a key the caller has no private row for resolves to the
    visible company-managed row: refused, nothing written, and no private copy
    forked under the same key."""
    row = _state()
    session = _use_session(monkeypatch, row)

    with pytest.raises(
        CompanyManagedWriteRefusedError,
        match="Only company-managed agents can modify company-managed shared state",
    ):
        await shared_state_service.upsert_state(
            user_id=str(uuid.uuid4()),
            key="fleet-incident",
            value={"stage": "closed"},
            writer=PERSONAL_AGENT,
        )
    assert row.value == {"stage": "triage"}
    assert session.added == [] and session.committed is False


@pytest.mark.asyncio
async def test_a_company_managed_agent_merges_into_a_company_managed_entry(monkeypatch):
    row = _state()
    session = _use_session(monkeypatch, row)

    out = await shared_state_service.upsert_state(
        user_id=None,  # an anonymous channel user on a company-managed agent
        key="fleet-incident",
        value={"stage": "contained"},
        is_company_managed=True,
        writer=COMPANY_AGENT,
    )
    assert out["value"] == {"stage": "contained"}
    assert session.committed is True


@pytest.mark.asyncio
async def test_a_personal_agent_writes_its_own_private_entry(monkeypatch):
    row = _state(is_company_managed=False)
    session = _use_session(monkeypatch, row)

    await shared_state_service.upsert_state(
        user_id=str(row.user_id),
        key="fleet-incident",
        value={"stage": "mine"},
        writer=PERSONAL_AGENT,
    )
    assert row.value == {"stage": "mine"}
    assert session.committed is True


@pytest.mark.asyncio
async def test_creating_a_company_managed_entry_needs_a_company_managed_writer(monkeypatch):
    _no_session(monkeypatch)
    with pytest.raises(
        CompanyManagedWriteRefusedError,
        match="Only admins can create company-managed shared state",
    ):
        await shared_state_service.upsert_state(
            user_id=str(uuid.uuid4()),
            key="fleet-incident",
            is_company_managed=True,
            writer=person_writer(is_admin=False),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("function", "arguments", "action"),
    [
        ("resolve_state", {"key": "fleet-incident"}, "resolve"),
        ("resolve_state_by_id", {"state_id": str(uuid.uuid4())}, "resolve"),
        ("delete_state", {"key": "fleet-incident"}, "delete"),
        ("delete_state_by_id", {"state_id": str(uuid.uuid4())}, "delete"),
    ],
)
async def test_a_personal_agent_cannot_resolve_or_delete_a_company_managed_entry(
    monkeypatch, function, arguments, action
):
    row = _state()
    session = _use_session(monkeypatch, row)

    with pytest.raises(
        CompanyManagedWriteRefusedError,
        match=f"Only company-managed agents can {action} company-managed shared state",
    ):
        await getattr(shared_state_service, function)(
            user_id=str(row.user_id), writer=PERSONAL_AGENT, **arguments
        )
    assert row.status == "active"
    assert session.deleted == [] and session.committed is False


@pytest.mark.asyncio
async def test_a_company_managed_agent_resolves_and_deletes_a_company_managed_entry(
    monkeypatch,
):
    row = _state()
    session = _use_session(monkeypatch, row)
    out = await shared_state_service.resolve_state(
        user_id=None, key="fleet-incident", writer=COMPANY_AGENT
    )
    assert out["status"] == "resolved"

    assert await shared_state_service.delete_state(
        user_id=None, key="fleet-incident", writer=COMPANY_AGENT
    )
    assert session.deleted == [row]


@pytest.mark.asyncio
async def test_the_platform_clears_the_decision_signal_of_the_run_it_drives(monkeypatch):
    row = _state(key="autonomous_run:r1:decision")
    session = _use_session(monkeypatch, row)
    assert await shared_state_service.delete_state(
        user_id=str(row.user_id), key=row.key, writer=PLATFORM_WRITER
    )
    assert session.deleted == [row]


# ── Shared state: the tools ────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments", "action"),
    [
        ("set_shared_state", {"value_json": '{"stage": "closed"}'}, "modify"),
        ("resolve_shared_state", {}, "resolve"),
        ("delete_shared_state", {}, "delete"),
    ],
)
async def test_the_tools_of_a_personal_agent_refuse_a_company_managed_entry(
    monkeypatch, tool, arguments, action
):
    row = _state()
    session = _use_session(monkeypatch, row)
    _, executor = build_shared_state_tools(None, str(row.user_id), str(uuid.uuid4()))

    result = await _call(executor, tool, key="fleet-incident", **arguments)

    assert result.is_error
    assert (
        f"Only company-managed agents can {action} company-managed shared state"
        in result.content
    )
    assert "nothing was written" in result.content
    assert "try again" not in result.content.lower()
    assert session.committed is False and session.deleted == []


@pytest.mark.asyncio
async def test_the_tools_of_a_company_managed_agent_write_a_company_managed_entry(
    monkeypatch,
):
    row = _state()
    session = _use_session(monkeypatch, row)
    _, executor = build_shared_state_tools(
        None,
        str(uuid.uuid4()),
        str(uuid.uuid4()),
        is_company_managed=True,
        company_managed_agent=True,
    )

    result = await _call(
        executor, "set_shared_state", key="fleet-incident", value_json='{"stage": "x"}'
    )
    assert not result.is_error
    assert "Saved shared state" in result.content
    assert "read-only" not in result.content
    assert session.committed is True


@pytest.mark.asyncio
async def test_a_personal_agent_reads_a_company_managed_entry_as_read_only(monkeypatch):
    """The read says so up front and gives no tidy-up hint it could not follow;
    a company-managed agent keeps the hint."""
    row = _state()  # quiet for 60 days: stale
    _use_session(monkeypatch, row)

    _, personal = build_shared_state_tools(None, str(row.user_id), str(uuid.uuid4()))
    read = await _call(personal, "get_shared_state", key="fleet-incident")
    assert "read-only for this agent" in read.content
    assert "Note: this entry is STALE" not in read.content

    listed = await _call(personal, "list_shared_state")
    assert "read-only" in listed.content
    assert "look stale" not in listed.content

    _, company = build_shared_state_tools(
        None, str(row.user_id), str(uuid.uuid4()), company_managed_agent=True
    )
    read = await _call(company, "get_shared_state", key="fleet-incident")
    assert "read-only" not in read.content
    assert "Note: this entry is STALE" in read.content


# ── Work items: the service gate ───────────────────────────────────────────────


_WORK_ITEM_WRITES = [
    ("update_work_item", {"status": "done"}, "update"),
    ("claim_work_item", {"agent_id": str(uuid.uuid4())}, "claim"),
    ("complete_work_item", {"result": "closed"}, "complete"),
    ("delete_work_item", {}, "delete"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("function", "arguments", "action"), _WORK_ITEM_WRITES)
async def test_a_personal_agent_cannot_write_or_take_over_a_company_managed_item(
    monkeypatch, function, arguments, action
):
    row = _item()
    session = _use_session(monkeypatch, row)

    with pytest.raises(
        CompanyManagedWriteRefusedError,
        match=f"Only company-managed agents can {action} company-managed work items",
    ):
        await getattr(wi_service, function)(
            str(row.id), user_id=str(row.user_id), writer=PERSONAL_AGENT, **arguments
        )
    assert row.status == "open" and row.claimed_by_agent_id is None
    assert session.committed is False and session.deleted == []


@pytest.mark.asyncio
async def test_a_company_managed_agent_claims_and_completes_a_company_managed_item(
    monkeypatch,
):
    row = _item()
    session = _use_session(monkeypatch, row)
    agent_id = str(uuid.uuid4())

    claim = await wi_service.claim_work_item(
        str(row.id), agent_id=agent_id, user_id=None, writer=COMPANY_AGENT
    )
    assert claim["claimed"] is True
    done = await wi_service.complete_work_item(
        str(row.id), user_id=None, result="contained", writer=COMPANY_AGENT
    )
    assert done["status"] == "done"
    assert session.committed is True


async def _no_cycle(*_a: object, **_kw: object) -> None:
    return None


@pytest.mark.asyncio
async def test_private_items_are_never_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _item(is_company_managed=False)
    _use_session(monkeypatch, row)
    monkeypatch.setattr("app.services.work_item_wait_cycles.refuse_wait_cycle", _no_cycle)
    out = await wi_service.update_work_item(
        str(row.id),
        user_id=str(row.user_id),
        status="blocked",
        blocked_on_refs=[{"repo": "o/r", "kind": "pull_request", "number": 7}],
        writer=PERSONAL_AGENT,
    )
    assert out["status"] == "blocked"


@pytest.mark.asyncio
async def test_creating_a_company_managed_item_needs_a_company_managed_writer(monkeypatch):
    _no_session(monkeypatch)
    with pytest.raises(
        CompanyManagedWriteRefusedError,
        match="Only company-managed flows can create company-managed work items",
    ):
        await wi_service.create_work_item(
            user_id=str(uuid.uuid4()),
            title="Fleet follow-up",
            is_company_managed=True,
            writer=flow_writer(is_company_managed=False),
        )


# ── Work items: the tools ──────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments", "action"),
    [
        ("update_work_item", {"status": "done"}, "update"),
        ("claim_work_item", {}, "claim"),
        ("complete_work_item", {"result": "closed"}, "complete"),
    ],
)
async def test_the_tools_of_a_personal_agent_refuse_a_company_managed_item(
    monkeypatch, tool, arguments, action
):
    row = _item()
    session = _use_session(monkeypatch, row)
    _, executor = build_work_item_tools(None, str(row.user_id), str(uuid.uuid4()))

    result = await _call(executor, tool, work_item_id=str(row.id), **arguments)

    assert result.is_error
    assert (
        f"Only company-managed agents can {action} company-managed work items"
        in result.content
    )
    assert "nothing was written" in result.content
    assert "Another agent likely already picked it up" not in result.content
    assert session.committed is False


@pytest.mark.asyncio
async def test_the_tools_of_a_company_managed_agent_take_over_a_company_managed_item(
    monkeypatch,
):
    row = _item()
    _use_session(monkeypatch, row)
    _, executor = build_work_item_tools(
        None,
        "anonymous",
        str(uuid.uuid4()),
        is_company_managed=True,
        company_managed_agent=True,
    )
    result = await _call(executor, "claim_work_item", work_item_id=str(row.id))
    assert not result.is_error
    assert "Claimed work item" in result.content


@pytest.mark.asyncio
async def test_a_personal_agent_lists_company_managed_items_as_read_only(monkeypatch):
    row = _item()
    _use_session(monkeypatch, row)

    _, personal = build_work_item_tools(None, str(row.user_id), str(uuid.uuid4()))
    listed = await _call(personal, "list_work_items")
    assert "company-managed, read-only for this agent" in listed.content

    _, company = build_work_item_tools(
        None, str(row.user_id), str(uuid.uuid4()), company_managed_agent=True
    )
    listed = await _call(company, "list_work_items")
    assert "read-only" not in listed.content
