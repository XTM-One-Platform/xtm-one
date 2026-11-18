"""The Flow Lab's access rule against a real PostgreSQL (#8260).

One rule on every path (``services/concierge/flow_lab_access``), checked on the
rows, by id and by exact name:

* a person: the flow's owner, a ``config`` grantee (directly or through a
  group) and, for a company-managed flow, an administrator; never a
  ``read_only`` grantee, a revoked share, another user or a sandbox copy;
* the Concierge: what level 1's supervision scope admits, content only where
  ``consent.content_readable`` admits the run - asserted on the call site;
  only the sandbox copies the Flow Lab authored;
* a sandbox graph's reach: what its source flow or basis agent names.
"""

from __future__ import annotations

import uuid

import pytest

from app.models.agent import Agent
from app.models.flow import Flow
from app.models.flow_run import FlowRun
from app.services.health import flow_lab_access as access
from tests.test_health_flow_lab_postgres import _database, _runs, _seed

pytestmark = pytest.mark.integration


async def _caller(db, user_id) -> access.FlowLabCaller:
    caller = await access.resolve_caller(db, user_id)
    assert caller is not None and caller.is_concierge is False
    return caller


@pytest.mark.parametrize(
    ("who", "reaches"),
    [
        ("owner", True),
        ("editor", True),
        ("member", True),
        ("admin", True),
        ("reader", False),
        ("stranger", False),
    ],
)
async def test_a_person_reaches_a_flow_they_may_manage(who, reaches):
    async with _database() as sessions:
        seeded = await _seed(sessions)
        async with sessions() as db:
            caller = await _caller(db, seeded["users"][who])
            by_id = await access.resolve_flow(db, caller, str(seeded["flow"]))
            by_name = await access.resolve_flow(db, caller, "triage FLOW")
        assert (by_id is not None) is reaches
        assert (by_name is not None) is reaches


async def test_a_revoked_share_reaches_nothing_at_once():
    async with _database() as sessions:
        seeded = await _seed(sessions)
        editor = seeded["users"]["editor"]
        async with sessions() as db:
            assert await access.resolve_flow(db, await _caller(db, editor), str(seeded["flow"])) is not None
            flow = await db.get(Flow, seeded["flow"])
            flow.shared_user_ids = []
            flow.shared_user_permissions = {}
            await db.commit()
        async with sessions() as db:
            assert await access.resolve_flow(db, await _caller(db, editor), str(seeded["flow"])) is None


@pytest.mark.parametrize(("who", "reaches"), [("admin", True), ("owner", False), ("editor", False)])
async def test_a_company_managed_flow_is_for_administrators(who, reaches):
    async with _database() as sessions:
        seeded = await _seed(sessions)
        async with sessions() as db:
            caller = await _caller(db, seeded["users"][who])
            assert (await access.resolve_flow(db, caller, str(seeded["company"])) is not None) is reaches


async def test_a_person_never_reaches_a_sandbox_copy():
    async with _database() as sessions:
        seeded = await _seed(sessions)
        async with sessions() as db:
            admin = await _caller(db, seeded["users"]["admin"])
            assert await access.resolve_flow(db, admin, str(seeded["sandbox"])) is None
            assert await access.resolve_sandbox_flow(db, admin, str(seeded["sandbox"])) is None


@pytest.mark.parametrize(
    ("who", "reaches"),
    [("owner", True), ("editor", True), ("admin", True), ("reader", False), ("stranger", False)],
)
async def test_a_person_mines_the_chats_of_an_agent_they_may_manage(who, reaches):
    async with _database() as sessions:
        seeded = await _seed(sessions)
        async with sessions() as db:
            caller = await _caller(db, seeded["users"][who])
            assert (await access.resolve_agent(db, caller, str(seeded["agent"])) is not None) is reaches


@pytest.mark.parametrize(
    ("who", "reaches"),
    [("owner", True), ("editor", True), ("admin", True), ("reader", False), ("stranger", False)],
)
async def test_a_person_reaches_an_assignment_they_may_modify(who, reaches):
    async with _database() as sessions:
        seeded = await _seed(sessions)
        async with sessions() as db:
            caller = await _caller(db, seeded["users"][who])
            assert (await access.resolve_assignment(db, caller, "Nightly triage") is not None) is reaches


async def test_an_ambiguous_name_resolves_to_nothing():
    async with _database() as sessions:
        seeded = await _seed(sessions)
        async with sessions() as db:
            db.add(Flow(id=uuid.uuid4(), user_id=seeded["users"]["owner"], name="Triage flow", graph={}))
            await db.commit()
        async with sessions() as db:
            caller = await _caller(db, seeded["users"]["owner"])
            assert await access.resolve_flow(db, caller, "Triage flow") is None
            assert await access.resolve_flow(db, caller, str(seeded["flow"])) is not None


async def test_a_name_others_share_still_resolves_to_the_one_flow_within_reach():
    async with _database() as sessions:
        seeded = await _seed(sessions)
        stranger = seeded["users"]["stranger"]
        async with sessions() as db:
            # Lower ids than any random one: read first, never within the owner's reach.
            db.add_all(
                Flow(id=uuid.UUID(int=i + 1), user_id=stranger, name="Triage flow", graph={}) for i in range(6)
            )
            await db.commit()
        async with sessions() as db:
            caller = await _caller(db, seeded["users"]["owner"])
            found = await access.resolve_flow(db, caller, "Triage flow")
            assert found is not None and found.id == seeded["flow"]


@pytest.fixture
def concierge(monkeypatch):
    """The Concierge acting, level 1's consent recorded at its call site."""
    from app.services.health import consent
    from app.services.health.identity import HealthPrincipal, acting_as_health_principal

    calls: dict[str, list] = {"scope": [], "content": [], "audit": []}
    state = {"in_scope": True, "readable": True}

    async def in_supervision_scope(_db, target_type, target_id):
        calls["scope"].append((target_type, target_id))
        return state["in_scope"]

    async def content_readable(_db, *, agent_id=None, flow_id=None, user_id=None, user_ids=()):
        calls["content"].append({"agent_id": agent_id, "flow_id": flow_id, "user_id": user_id})
        return state["readable"]

    from app.services.health import identity

    monkeypatch.setattr(identity, "health_enabled", lambda: True)
    monkeypatch.setattr(consent, "in_supervision_scope", in_supervision_scope)
    monkeypatch.setattr(consent, "content_readable", content_readable)

    async def audit_content_read(_db, **kwargs):
        calls["audit"].append(kwargs)

    monkeypatch.setattr(consent, "audit_content_read", audit_content_read)
    principal = HealthPrincipal(agent_id=uuid.uuid4(), role="flow_lab", on_behalf_of_admin_id=None)
    state["agent_id"] = str(principal.agent_id)
    # The principal is a ContextVar: it is entered in the test's own task.
    return calls, state, lambda: acting_as_health_principal(principal)


async def test_the_concierge_reaches_what_its_scope_admits(concierge):
    calls, state, acting = concierge
    async with _database() as sessions:
        seeded = await _seed(sessions)
        with acting():
            async with sessions() as db:
                caller = await access.resolve_caller(db, None, agent_id=state["agent_id"])
                assert caller.is_concierge
                assert await access.resolve_flow(db, caller, str(seeded["flow"])) is not None
                assert await access.resolve_sandbox_flow(db, caller, str(seeded["sandbox"])) is not None
                # A proposal's copy is what was approved: no tool reaches it.
                assert await access.resolve_sandbox_flow(db, caller, str(seeded["proposal_copy"])) is None
                state["in_scope"] = False
                assert await access.resolve_flow(db, caller, str(seeded["flow"])) is None
                assert await access.resolve_assignment(db, caller, str(seeded["assignment"])) is None
                assert (await access.resolve_agent(db, caller, "Triage agent")) is None
        assert calls["scope"] == [
            ("flow", seeded["flow"]),
            ("flow", seeded["flow"]),
            ("agent", seeded["agent"]),
            ("agent", seeded["agent"]),
        ]


async def test_the_concierge_reads_flow_run_content_only_where_consent_admits_it(concierge):
    calls, state, acting = concierge
    async with _database() as sessions:
        seeded = await _seed(sessions)
        runs = await _runs(sessions, seeded)
        with acting():
            async with sessions() as db:
                caller = await access.resolve_caller(db, None, agent_id=state["agent_id"])
                flow = await db.get(Flow, seeded["flow"])
                run = await db.get(FlowRun, runs["other"])
                assert await access.flow_run_content_allowed(db, caller, flow, run) is True
                await access.audit_flow_run_read(db, caller, flow, run)
                state["readable"] = False
                assert await access.resolve_run(db, caller, "flow_run", str(run.id)) is None
        assert calls["content"] == [
            {"agent_id": None, "flow_id": seeded["flow"], "user_id": seeded["users"]["stranger"]},
            {"agent_id": None, "flow_id": seeded["flow"], "user_id": seeded["users"]["stranger"]},
        ]
        assert calls["audit"] == [
            {"kind": "flow_run", "ref_id": runs["other"], "flow_id": seeded["flow"], "user_id": seeded["users"]["stranger"]}
        ]


async def test_a_prototypes_reach_is_what_its_agent_already_names():
    async with _database() as sessions:
        seeded = await _seed(sessions)
        skill_id, flow_node_id = str(uuid.uuid4()), str(uuid.uuid4())
        async with sessions() as db:
            agent = await db.get(Agent, seeded["agent"])
            agent.skill_ids = [skill_id]
            flow = await db.get(Flow, seeded["flow"])
            flow.graph = {"nodes": [{"id": "n", "kind": "integration", "config": {"integration_id": flow_node_id}}]}
            await db.commit()
        async with sessions() as db:
            by_agent = await access.sandbox_reach(db, source_flow=None, basis_agent_id=seeded["agent"])
            by_flow = await access.sandbox_reach(db, source_flow=await db.get(Flow, seeded["flow"]), basis_agent_id=None)
        assert {str(seeded["agent"]), skill_id} <= by_agent
        assert flow_node_id not in by_agent
        assert by_flow == {flow_node_id}
