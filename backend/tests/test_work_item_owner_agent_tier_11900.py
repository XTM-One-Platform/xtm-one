"""The owner agent of a company-managed item filed in its fleet workspace (#11900).

A company-managed agent acting for a person files the item it hands to
another company-managed agent in the hosting fleet's workspace, with the
person as ``user_id``. The assignee's run acts with no person, so before
this change no tier admitted the item to it while the dispatcher (reading
through the assignment owner's tiers) kept handing it out. These tests pin:

* the owner-agent tier: company-managed, filed, owner or claimer, workspace
  of a live fleet the agent is a HOME member of; nothing for a person, a
  personal agent or a writer without an agent;
* dispatch selection and claim admission carry the same tier (one gate);
* the unfiled company tier and the user_id NULL case are unchanged;
* an item the inbox run can neither settle nor release is parked, once.

No database: SQL is compiled and read, sessions are queued fakes.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

import app.services.work_item as wi_service
from app.models.work_item import WorkItem
from app.services.company_managed_writes import agent_writer, person_writer


def _sql(clause) -> str:
    return str(select(WorkItem.id).where(clause).compile(dialect=postgresql.dialect())).lower()


def _params(clause) -> dict:
    return select(WorkItem.id).where(clause).compile(dialect=postgresql.dialect()).params


AGENT = uuid.uuid4()
CM_AGENT = agent_writer(is_company_managed=True, agent_id=AGENT)


# -- The tier itself --------------------------------------------------------


def test_the_owner_agent_tier_is_scoped_to_its_home_fleet_workspaces():
    clause = wi_service._owner_agent_clause(CM_AGENT)
    sql = _sql(clause)
    assert "work_items.is_company_managed is true" in sql
    assert "work_items.workspace_id is not null" in sql
    # The workspace must be the coordination workspace of a fleet the agent is
    # a HOME member of, the fleet not archived.
    assert "fleets.workspace_id" in sql and "fleets.is_archived is false" in sql
    assert "fleet_members.role" in sql and "fleets.is_archived is false" in sql
    # Owner or current claimer only.
    assert "work_items.owner_agent_id" in sql and "work_items.claimed_by_agent_id" in sql
    params = _params(clause).values()
    assert AGENT in params and "member" in params


@pytest.mark.parametrize(
    "actor",
    [
        None,
        person_writer(is_admin=True),  # a person: no agent
        agent_writer(is_company_managed=False, agent_id=uuid.uuid4()),  # personal agent
        agent_writer(is_company_managed=True),  # no acting agent
    ],
)
def test_nobody_but_a_company_managed_agent_gets_the_tier(actor):
    assert _sql(wi_service._owner_agent_clause(actor)).endswith("where false")


def test_a_person_s_read_and_write_are_unchanged():
    uid = uuid.uuid4()
    assert _sql(wi_service._writable_clause(uid, [])) == _sql(wi_service._writable_clause(uid, [], None))
    # The tier's own marker: only it filters on archived fleets.
    assert "is_archived" not in _sql(wi_service._writable_clause(uid, []))
    assert "is_archived" not in _sql(wi_service._visible_clause(uid, []))


def test_the_unfiled_company_tier_still_admits_a_user_id_null_item():
    # The earlier fix (#34d23bd): a company-managed, unfiled item with no person
    # is writable by an agent path with no user. Kept, the new tier only adds.
    sql = _sql(wi_service._writable_clause(None, [], CM_AGENT))
    assert "work_items.is_company_managed is true and work_items.workspace_id is null" in sql
    assert "fleets.is_archived is false" in sql


def test_the_agent_s_own_read_admits_what_it_may_claim():
    assert "fleets.is_archived is false" in _sql(wi_service._visible_clause(None, [], CM_AGENT))


# -- One gate: dispatch selection == claim admission ------------------------


class _Result:
    def __init__(self, rows=None, one=None):
        self._rows, self._one = rows or [], one

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._one


class _Session:
    def __init__(self, results):
        self._results = list(results)
        self.statements: list = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, _model, _ident):
        return None  # no workspace tier for the run's (absent) person

    async def execute(self, statement):
        self.statements.append(statement)
        return self._results.pop(0) if self._results else _Result()

    async def commit(self):
        self.committed = True


def _statement_sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect())).lower()


@pytest.mark.asyncio
async def test_dispatch_and_claim_select_through_the_same_owner_agent_tier(monkeypatch):
    dispatch = _Session([_Result(rows=[])])
    monkeypatch.setattr("app.core.database.async_session", lambda: dispatch)
    await wi_service.list_dispatchable_work_items(owner_agent_id=str(AGENT), user_id=None, writer=CM_AGENT)
    claim = _Session([_Result(one=None)])
    monkeypatch.setattr("app.core.database.async_session", lambda: claim)
    out = await wi_service.claim_work_item(str(uuid.uuid4()), agent_id=str(AGENT), user_id=None, writer=CM_AGENT)
    assert out == {"claimed": False, "reason": "not found"}
    dispatch_sql = _statement_sql(dispatch.statements[0])
    claim_sql = _statement_sql(claim.statements[0])
    for sql in (dispatch_sql, claim_sql):
        assert "fleets.is_archived is false" in sql and "work_items.workspace_id is not null" in sql


@pytest.mark.asyncio
async def test_the_inbox_writer_is_the_tier_of_accepted_by_inbox():
    inbox = SimpleNamespace(
        owner_agent_id=str(AGENT),
        user_id=None,
        writer=CM_AGENT,
        correlation_prefix=None,
        priority_min=None,
    )
    db = _Session([_Result(rows=[])])
    await wi_service.accepted_by_inbox(db, inbox, [uuid.uuid4()])
    assert "fleets.is_archived is false" in _statement_sql(db.statements[0])


# -- Safety net: an unreachable item is parked once -------------------------


@pytest.mark.asyncio
async def test_an_item_the_run_cannot_reach_is_parked_with_its_reason(monkeypatch):
    run_id = uuid.uuid4()
    item = SimpleNamespace(
        id=uuid.uuid4(),
        status="claimed",
        blocked_reason=None,
        claimed_by_agent_id=AGENT,
        claimed_by_run_id=run_id,
        claimed_at=datetime.now(UTC),
        last_activity_at=None,
        claim_notes=None,
    )
    session = _Session([_Result(one=item)])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    published = []

    async def _publish(wid, *, previous_status, note=None):
        published.append((wid, previous_status))

    monkeypatch.setattr(wi_service, "publish_transition", _publish)

    parked = await wi_service.park_unreachable_work_item(
        str(item.id), agent_id=AGENT, run_id=run_id, reason="not found"
    )
    assert parked is True and session.committed
    assert item.status == "blocked"
    assert str(run_id) in item.blocked_reason and "not dispatched again" in item.blocked_reason
    assert item.claimed_by_agent_id is None and item.claimed_by_run_id is None
    assert item.claim_notes and str(run_id) in item.claim_notes[-1]["text"]
    assert published == [(item.id, "claimed")]
    # The claim is the authority: only this agent's claim in this run, still held.
    sql = _statement_sql(session.statements[0])
    assert "claimed_by_agent_id" in sql and "claimed_by_run_id" in sql and "for update" in sql


@pytest.mark.asyncio
async def test_an_item_no_longer_held_by_the_run_is_left_alone(monkeypatch):
    session = _Session([_Result(one=None)])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    parked = await wi_service.park_unreachable_work_item(
        str(uuid.uuid4()), agent_id=AGENT, run_id=uuid.uuid4(), reason="not found"
    )
    assert parked is False and not session.committed
