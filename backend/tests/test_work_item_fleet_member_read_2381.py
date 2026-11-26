"""A company-managed fleet member reads its own fleet's workspace items (#2381).

A company-managed assignment run acts with no person (``_resolve_run_file_owner_id``
keeps file stamping personless), so its work-item tools carry no person's
workspace tier. An item another member filed in the fleet's coordination
workspace (a CTEM gate owned by the Loop Orchestrator) matched no read tier:
``get_work_item`` answered not found and ``list_work_items`` did not list it.

These tests pin:

* the new read tier: workspaces of live fleets the agent is a member of
  (home or connected) or orchestrates; nothing for a person, a personal agent
  or a writer without an agent (no cross-fleet leak, no person widened);
* ``get_work_item`` and ``list_work_items`` select through it on an agent read;
* writes keep their rules: ``_writable_clause`` does not carry it;
* ``scope all`` accepts a run acting for an administrator with no person in
  ``user_id``, and still refuses a non-admin one;
* the owner-agent and handed-to tiers are still there.

No database: SQL is compiled and read, sessions are queued fakes.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

import app.services.work_item as wi_service
from app.models.work_item import WorkItem
from app.services import coordination_scope
from app.services.company_managed_writes import agent_writer, person_writer

AGENT = uuid.uuid4()
CM_AGENT = agent_writer(is_company_managed=True, agent_id=AGENT)
CM_ADMIN_RUN = agent_writer(is_company_managed=True, agent_id=AGENT, acting_admin=True)


def _compiled(clause):
    return select(WorkItem.id).where(clause).compile(dialect=postgresql.dialect())


def _sql(clause) -> str:
    return str(_compiled(clause)).lower()


# -- The tier itself --------------------------------------------------------


def test_the_tier_reads_the_workspaces_of_the_live_fleets_the_agent_works_in():
    clause = wi_service._fleet_member_read_clause(CM_AGENT)
    sql = _sql(clause)
    assert "work_items.workspace_id is not null" in sql
    assert "work_items.workspace_id in (select fleets.workspace_id" in sql
    assert "fleets.is_archived is false" in sql
    # Member (any role: home or connected) or orchestrator.
    assert "fleet_members.agent_id" in sql and "fleets.orchestrator_agent_id" in sql
    assert "fleet_members.role" not in sql
    assert AGENT in _compiled(clause).params.values()


def test_the_tier_names_no_other_agent_than_the_reader():
    # The workspace set is computed from the reader alone: an item filed in
    # the workspace of a fleet the reader is not in matches no fleet row.
    params = [
        v for v in _compiled(wi_service._fleet_member_read_clause(CM_AGENT)).params.values() if isinstance(v, uuid.UUID)
    ]
    assert params and set(params) == {AGENT}


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
    assert _sql(wi_service._fleet_member_read_clause(actor)).endswith("where false")


# Other tiers read fleets too: the tier is swapped for a marker no other
# tier writes, so presence and absence are the tier's own.
MARKER = "2381 = 2381"


@pytest.fixture
def marked(monkeypatch):
    from sqlalchemy import false, literal_column

    def _marker(actor):
        if actor is None or actor.agent_id is None:
            return false()
        return literal_column("2381") == literal_column("2381")

    monkeypatch.setattr(wi_service, "_fleet_member_read_clause", _marker)


def test_the_agent_read_carries_the_tier_and_a_person_read_does_not(marked):
    assert MARKER in _sql(wi_service._visible_clause(None, [], CM_AGENT))
    assert MARKER not in _sql(wi_service._visible_clause(uuid.uuid4(), []))


def test_writes_keep_their_rules(marked):
    assert MARKER not in _sql(wi_service._writable_clause(None, [], CM_AGENT))
    assert MARKER not in _sql(wi_service._writable_clause(uuid.uuid4(), [], CM_AGENT))


def test_the_owner_agent_and_handed_to_tiers_are_still_read():
    sql = _sql(wi_service._visible_clause(None, [], CM_AGENT))
    # #11900 owner-agent tier and #858 handed-to tier.
    assert "fleet_members.role" in sql
    assert "work_items.user_id is not null" in sql


# -- The reads select through it --------------------------------------------


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

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, _model, _ident):
        return None

    async def execute(self, statement):
        self.statements.append(statement)
        return self._results.pop(0) if self._results else _Result()


def _statement_sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect())).lower()


@pytest.mark.asyncio
async def test_get_work_item_of_a_company_managed_run_selects_its_fleet_workspaces(monkeypatch, marked):
    session = _Session([_Result(one=None)])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    assert await wi_service.get_work_item(str(uuid.uuid4()), user_id=None, reader=CM_AGENT) is None
    assert MARKER in _statement_sql(session.statements[0])


@pytest.mark.asyncio
async def test_list_work_items_of_a_company_managed_run_selects_its_fleet_workspaces(monkeypatch, marked):
    session = _Session([_Result(rows=[])])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    assert await wi_service.list_work_items(user_id=None, reader=CM_AGENT, search="ctem:remediation:") == []
    assert any(MARKER in _statement_sql(s) for s in session.statements)


@pytest.mark.asyncio
async def test_a_person_listing_is_unchanged(monkeypatch, marked):
    session = _Session([_Result(rows=[])])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    await wi_service.list_work_items(user_id=None)
    assert not any(MARKER in _statement_sql(s) for s in session.statements)


# -- scope all for a run acting for an administrator ------------------------


FLEET = uuid.uuid4()


@pytest.fixture
def in_a_fleet(monkeypatch):
    async def _home(_aid):
        return FLEET, "CTEM", (AGENT,), uuid.uuid4()

    async def _not_admin(_uid):
        return False

    monkeypatch.setattr(coordination_scope, "read_home_fleet", _home)
    monkeypatch.setattr(coordination_scope, "user_is_admin", _not_admin)


@pytest.mark.asyncio
async def test_scope_all_admits_a_company_managed_run_acting_for_an_administrator(in_a_fleet):
    resolved = await coordination_scope.resolve_coordination_scope(str(AGENT), None, "all", acting_admin=True)
    assert isinstance(resolved, coordination_scope.CoordinationScope)
    assert resolved.name == coordination_scope.SCOPE_ALL


@pytest.mark.asyncio
async def test_scope_all_still_refuses_a_run_not_acting_for_an_administrator(in_a_fleet):
    refused = await coordination_scope.resolve_coordination_scope(str(AGENT), None, "all")
    assert isinstance(refused, str) and "administrator" in refused


def test_the_admin_run_writer_carries_the_flag():
    assert CM_ADMIN_RUN.acting_admin and not CM_AGENT.acting_admin
