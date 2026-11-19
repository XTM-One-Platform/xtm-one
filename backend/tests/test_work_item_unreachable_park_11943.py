"""An item its owner's inbox run cannot read costs at most one run (#11943).

The inbox selects through the assignment owner's writable tier, while the run
it starts reads and writes its item through its own tools, which act with no
person on a company-managed run. Before this change an item only the first
admitted was dispatched again and again (each run answering "not found"),
until the release cap parked it. These tests pin:

* the inbox records the identity its run's tools act with;
* dispatch selection adds that identity's tier (selection == claim admission),
  and nothing changes when the identities match or the agent has no tools;
* the reach check is the claim's admission.

No database: SQL is compiled and read, sessions are queued fakes.
"""

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

import app.services.work_item as wi_service
from app.services.company_managed_writes import agent_writer

AGENT = uuid.uuid4()
OWNER = uuid.uuid4()
CM_AGENT = agent_writer(is_company_managed=True, agent_id=AGENT)


def _assignment(*, company: bool, run_as=None, hide=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="inbox",
        agent_id=AGENT,
        user_id=OWNER,
        run_as_user_id=run_as,
        is_company_managed=company,
        hide_runs=hide,
        run_as_all_users=False,
        trigger_config={},
    )


def _agent(*, company: bool, tools=("coordination",)):
    return SimpleNamespace(
        id=AGENT,
        enabled=True,
        is_company_managed=company,
        builtin_tool_ids=list(tools),
        builtin_allowed_tools=None,
        hide_runs=False,
        health_role=None,
    )


# -- The identity the run's tools act with --------------------------------


def test_a_company_managed_run_reads_its_item_with_no_person():
    inbox = wi_service.assignment_inbox(_assignment(company=True), _agent(company=True))
    assert inbox.user_id == str(OWNER)
    assert inbox.run_user_id is None and inbox.run_reads_items is True


def test_a_hidden_run_reads_with_its_run_as_person():
    run_as = uuid.uuid4()
    inbox = wi_service.assignment_inbox(_assignment(company=True, run_as=run_as, hide=True), _agent(company=True))
    assert inbox.run_user_id == str(run_as)


def test_a_personal_run_reads_with_its_owner():
    inbox = wi_service.assignment_inbox(_assignment(company=False), _agent(company=False))
    assert inbox.run_user_id == str(OWNER)


def test_an_agent_without_the_work_item_tools_is_not_narrowed():
    inbox = wi_service.assignment_inbox(_assignment(company=True), _agent(company=True, tools=("web",)))
    assert inbox.run_reads_items is False


# -- Dispatch selection == what the run may claim -------------------------


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
    def __init__(self, results=()):
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


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect())).lower()


@pytest.fixture
def _no_tiers(monkeypatch):
    async def _tiers(_db, _user_id):
        return [], []

    monkeypatch.setattr(wi_service, "_workspace_tiers", _tiers)


@pytest.mark.asyncio
async def test_the_dispatch_adds_the_run_s_own_tier(monkeypatch, _no_tiers):
    narrowed = _Session([_Result(rows=[])])
    monkeypatch.setattr("app.core.database.async_session", lambda: narrowed)
    await wi_service.list_dispatchable_work_items(
        owner_agent_id=str(AGENT), user_id=str(OWNER), writer=CM_AGENT, run_user_id=None, run_reads_items=True
    )
    plain = _Session([_Result(rows=[])])
    monkeypatch.setattr("app.core.database.async_session", lambda: plain)
    await wi_service.list_dispatchable_work_items(owner_agent_id=str(AGENT), user_id=str(OWNER), writer=CM_AGENT)
    narrowed_sql, plain_sql = _sql(narrowed.statements[0]), _sql(plain.statements[0])
    # The run's tier (no person) is ANDed on top of the owner's: one more
    # writable clause, so the item must pass both.
    assert narrowed_sql != plain_sql
    assert narrowed_sql.count("fleets.is_archived is false") > plain_sql.count("fleets.is_archived is false")


@pytest.mark.asyncio
async def test_the_same_identity_or_no_tools_narrows_nothing(_no_tiers):
    db = _Session()
    same = await wi_service._run_tools_clause(
        db, inbox_user_id=str(OWNER), run_user_id=str(OWNER), run_reads_items=True, writer=CM_AGENT
    )
    no_tools = await wi_service._run_tools_clause(
        db, inbox_user_id=str(OWNER), run_user_id=None, run_reads_items=False, writer=CM_AGENT
    )
    assert same is None and no_tools is None


@pytest.mark.asyncio
async def test_accepted_by_inbox_says_what_the_dispatch_takes(_no_tiers):
    inbox = SimpleNamespace(
        owner_agent_id=str(AGENT),
        user_id=str(OWNER),
        writer=CM_AGENT,
        correlation_prefix=None,
        priority_min=None,
        run_user_id=None,
        run_reads_items=True,
    )
    narrowed = _Session([_Result(rows=[])])
    await wi_service.accepted_by_inbox(narrowed, inbox, [uuid.uuid4()])
    inbox.run_reads_items = False
    plain = _Session([_Result(rows=[])])
    await wi_service.accepted_by_inbox(plain, inbox, [uuid.uuid4()])
    assert _sql(narrowed.statements[0]).count("fleets.is_archived is false") > _sql(plain.statements[0]).count(
        "fleets.is_archived is false"
    )


# -- The reach check the finalizer parks on ------------------------------


@pytest.mark.asyncio
async def test_an_item_outside_the_run_s_tier_is_unreachable(monkeypatch, _no_tiers):
    monkeypatch.setattr("app.core.database.async_session", lambda: _Session([_Result(one=None)]))
    assert await wi_service.reachable_by_run_tools(str(uuid.uuid4()), user_id=None, writer=CM_AGENT) is False


@pytest.mark.asyncio
async def test_an_item_in_the_run_s_tier_is_reachable(monkeypatch, _no_tiers):
    item = SimpleNamespace(id=uuid.uuid4(), created_by_agent_id=None)
    monkeypatch.setattr("app.core.database.async_session", lambda: _Session([_Result(one=item)]))

    async def _policies(_db, _reader, _items):
        return {}

    monkeypatch.setattr(wi_service, "_policies_for", _policies)
    monkeypatch.setattr(wi_service, "access_of", lambda _p, _c: None)
    monkeypatch.setattr(wi_service, "_admits", lambda *_a: True)
    assert await wi_service.reachable_by_run_tools(str(item.id), user_id=None, writer=CM_AGENT) is True
