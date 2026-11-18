"""The Quality Analyst's findings and the one access rule of its tools, on PostgreSQL (#8259).

Pinned here:

* the Concierge reads an agent of its scope and writes only with the role the
  tool needs; it reads nothing of an agent out of scope;
* a revoked enrolment takes the agent out of the supervision scope: its
  findings are no longer listed to Platform Health at the next read;
* a person reads the findings of an agent they manage - owner, admin,
  ``config`` and ``config_with_chats`` grantees, a group grant - its content
  only with the chats right, and nothing as ``read_only``, after a revoked
  share or on a company-managed agent they do not administer; a person never
  writes, admin included.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest
from sqlalchemy import func, select

import app.core.database as database
from app.models.agent import Agent
from app.models.group import Group, GroupMember
from app.models.health_finding import ConciergeFinding
from app.models.platform_settings import PlatformSettings
from app.models.user import User
from app.services.builtin_tools.health_experiments import build_concierge_experiment_tools
from app.services.builtin_tools.health_quality import build_concierge_diagnosis_tools
from app.services.health import availability
from app.services.health.access import NOT_IN_REACH, WRITE_REFUSED
from app.services.health.findings import Evidence, record_finding
from app.services.health.identity import ConciergePrincipal, acting_as_concierge, current_concierge
from app.tools.base import ToolCall
from tests.test_provisioning_postgres import _temp_database

pytestmark = pytest.mark.integration


def _user(tag: str, role: str = "user") -> User:
    return User(
        id=uuid.uuid4(),
        email=f"cq-{tag}-{uuid.uuid4().hex[:8]}@example.com",
        full_name=f"CQ {tag}",
        hashed_password="x",
        role=role,
    )


@dataclass
class World:
    admin: User
    owner: User
    config: User
    chats: User
    read_only: User
    member: User
    revoked: User
    stranger: User
    agent: Agent
    company_agent: Agent
    outside_agent: Agent
    analyst: Agent
    experimenter: Agent
    finding_id: uuid.UUID
    company_finding_id: uuid.UUID


async def _seed(sessions) -> World:
    async with sessions() as db:
        users = {tag: _user(tag) for tag in ("owner", "config", "chats", "read_only", "member", "revoked", "stranger")}
        admin = _user("admin", role="admin")
        db.add_all([admin, *users.values()])
        await db.flush()
        group = Group(name=f"cq-{uuid.uuid4().hex[:8]}")
        db.add(group)
        await db.flush()
        db.add(GroupMember(group_id=group.id, user_id=users["member"].id))

        agent = Agent(id=uuid.uuid4(), name="Billing agent", user_id=users["owner"].id, persona="You bill.")
        agent.health_enrolled = True
        agent.shared_user_ids = [str(users["config"].id), str(users["chats"].id), str(users["read_only"].id)]
        agent.shared_user_permissions = {
            str(users["config"].id): "config",
            str(users["chats"].id): "config_with_chats",
            str(users["read_only"].id): "read_only",
        }
        agent.shared_group_ids = [str(group.id)]
        agent.shared_group_permissions = {str(group.id): "config"}
        company_agent = Agent(
            id=uuid.uuid4(), name="Company agent", user_id=admin.id, is_company_managed=True, persona="You help."
        )
        company_agent.health_enrolled = True
        outside_agent = Agent(id=uuid.uuid4(), name="Outside agent", user_id=users["owner"].id, persona="x")
        analyst = Agent(
            id=uuid.uuid4(), name="Quality Analyst", user_id=admin.id, is_company_managed=True, persona="x"
        )
        analyst.health_role = "quality_analyst"
        experimenter = Agent(
            id=uuid.uuid4(), name="Experimenter", user_id=admin.id, is_company_managed=True, persona="x"
        )
        experimenter.health_role = "experimenter"
        db.add_all([agent, company_agent, outside_agent, analyst, experimenter])
        await db.flush()
        db.add(
            PlatformSettings(
                health_settings={
                    "enabled": True,
                    "scope_mode": "selected",
                    "scope_agent_ids": [str(agent.id), str(company_agent.id)],
                }
            )
        )
        trace_id = str(uuid.uuid4())
        recorded = await record_finding(
            db,
            target_type="agent",
            target_id=agent.id,
            pattern_kind="tool_errors",
            pattern_key="search_knowledge",
            hypothesis="The search tool times out on long queries.",
            suggested_experiment="Shorten the queries the persona asks for.",
            evidence=Evidence(trace_ids=(trace_id,), counts={"errors": 7}),
            content_evidence=True,
            author_agent_id=analyst.id,
        )
        company = await record_finding(
            db,
            target_type="agent",
            target_id=company_agent.id,
            pattern_kind="cost_drift",
            pattern_key="",
            hypothesis="Cost per run up a third.",
            suggested_experiment="",
            evidence=Evidence(counts={"drift_pct": 33.0}),
            content_evidence=False,
            author_agent_id=analyst.id,
        )
        await db.commit()
        return World(
            admin=admin,
            owner=users["owner"],
            config=users["config"],
            chats=users["chats"],
            read_only=users["read_only"],
            member=users["member"],
            revoked=users["revoked"],
            stranger=users["stranger"],
            agent=agent,
            company_agent=company_agent,
            outside_agent=outside_agent,
            analyst=analyst,
            experimenter=experimenter,
            finding_id=recorded.finding.id,
            company_finding_id=company.finding.id,
        )


@asynccontextmanager
async def _world(monkeypatch, prefix: str) -> AsyncIterator[tuple[World, object]]:
    async with _temp_database(prefix) as sessions:
        monkeypatch.setattr(database, "async_session", sessions)
        yield await _seed(sessions), sessions


async def _call(user_id: str, name: str, **arguments) -> str:
    builder = build_concierge_diagnosis_tools if name in {
        "get_quality_signals",
        "list_findings",
        "record_finding",
        "draft_variant",
    } else build_concierge_experiment_tools
    # The calling agent is the bound principal's own, as in its Concierge run.
    principal = current_concierge()
    _defs, executor = builder(user_id, agent_id=str(principal.agent_id) if principal else None)
    result = await executor.execute_tool(ToolCall(id="c1", name=name, arguments=arguments))
    return result.content or ""


def _parsed(text: str) -> dict:
    """The JSON of a tool result, inside its untrusted-data block or not."""
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start : end + 1])


def _as(agent: Agent, role: str):
    return acting_as_concierge(ConciergePrincipal(agent_id=agent.id, role=role))


@pytest.mark.asyncio
async def test_the_concierge_reads_its_scope_and_writes_with_its_role(monkeypatch):
    async with _world(monkeypatch, "cq_concierge") as (world, _sessions):
        with _as(world.analyst, "quality_analyst"):
            listed = _parsed(await _call("", "list_findings", target_id=str(world.agent.id)))
            assert [f["id"] for f in listed["findings"]] == [str(world.finding_id)]
            assert listed["findings"][0]["hypothesis"] == "The search tool times out on long queries."

            out_of_scope = await _call("", "list_findings", target_id=str(world.outside_agent.id))
            assert _parsed(out_of_scope) == {"count": 0, "findings": []}
            refused = await _call(
                "",
                "record_finding",
                target_type="agent",
                target_id=str(world.outside_agent.id),
                pattern_kind="latency",
                hypothesis="Slow.",
            )
            assert refused == NOT_IN_REACH

            recorded = json.loads(
                await _call(
                    "",
                    "record_finding",
                    target_type="agent",
                    target_id=str(world.agent.id),
                    pattern_kind="tool_errors",
                    pattern_key="search_knowledge",
                    hypothesis="Same pattern, next cycle.",
                    evidence={"rating_ids": [str(uuid.uuid4())], "trace_ids": ["not-a-uuid"]},
                )
            )
            assert recorded["finding_id"] == str(world.finding_id)
            assert recorded["new"] is False
            assert recorded["evidence_refs_dropped"] == 1

        with _as(world.experimenter, "experimenter"):
            assert (
                await _call("", "record_finding", target_type="agent", target_id=str(world.agent.id), pattern_kind="latency", hypothesis="x")
                == WRITE_REFUSED
            )
        with _as(world.analyst, "quality_analyst"):
            assert await _call("", "run_experiment", experiment_id=str(uuid.uuid4())) == WRITE_REFUSED
            assert await _call("", "propose_from_experiment", experiment_id=str(uuid.uuid4())) == WRITE_REFUSED


@pytest.mark.asyncio
async def test_a_revoked_enrolment_hides_the_findings_at_the_next_read(monkeypatch):
    async with _world(monkeypatch, "cq_revoked") as (world, sessions):
        async with sessions() as db:
            agent = await db.get(Agent, world.agent.id)
            agent.health_enrolled = False
            await db.commit()
        with _as(world.analyst, "quality_analyst"):
            listed = _parsed(await _call("", "list_findings", target_id=str(world.agent.id)))
        # Enrolled targets only (consent.py): an unenrolled agent is out of scope, metrics and content alike.
        assert (listed["count"], listed["findings"]) == (0, [])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("who", "sees", "content"),
    [
        ("owner", True, True),
        ("admin", True, True),
        ("config", True, False),
        ("chats", True, True),
        ("member", True, False),
        ("read_only", False, False),
        ("revoked", False, False),
        ("stranger", False, False),
    ],
)
async def test_a_person_reads_the_findings_of_an_agent_they_manage(monkeypatch, who, sees, content):
    async with _world(monkeypatch, f"cq_person_{who}") as (world, sessions):
        if who == "revoked":
            async with sessions() as db:
                # A config grant, then revoked: the next read sees nothing.
                agent = await db.get(Agent, world.agent.id)
                agent.shared_user_ids = [*agent.shared_user_ids, str(world.revoked.id)]
                agent.shared_user_permissions = {**agent.shared_user_permissions, str(world.revoked.id): "config"}
                await db.commit()
                assert _parsed(await _call(str(world.revoked.id), "list_findings", target_id=str(world.agent.id)))["count"] == 1
                agent.shared_user_ids = [u for u in agent.shared_user_ids if u != str(world.revoked.id)]
                agent.shared_user_permissions = {
                    k: v for k, v in agent.shared_user_permissions.items() if k != str(world.revoked.id)
                }
                await db.commit()
        user = getattr(world, who)
        listed = _parsed(await _call(str(user.id), "list_findings", target_id=str(world.agent.id)))
        if not sees:
            assert listed == {"count": 0, "findings": []}
            return
        (finding,) = listed["findings"]
        assert finding["id"] == str(world.finding_id)
        assert bool(finding.get("redacted")) is (not content)
        assert (finding["hypothesis"] != "") is content


@pytest.mark.asyncio
@pytest.mark.parametrize(("who", "sees"), [("admin", True), ("owner", False), ("config", False)])
async def test_a_company_managed_agent_s_findings_are_its_administrators(monkeypatch, who, sees):
    async with _world(monkeypatch, f"cq_company_{who}") as (world, _sessions):
        user = getattr(world, who)
        listed = _parsed(await _call(str(user.id), "list_findings", target_id=str(world.company_agent.id)))
        assert [f["id"] for f in listed["findings"]] == ([str(world.company_finding_id)] if sees else [])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("record_finding", {"target_type": "agent", "pattern_kind": "latency", "hypothesis": "x"}),
        ("draft_variant", {"change_type": "persona", "text": "y", "summary": "s"}),
        ("run_experiment", {}),
        ("propose_from_experiment", {}),
    ],
)
async def test_a_person_never_writes_admin_included(monkeypatch, name, arguments):
    async with _world(monkeypatch, f"cq_write_{name}") as (world, sessions):
        for user in (world.admin, world.owner):
            args = dict(arguments)
            if name == "record_finding":
                args["target_id"] = str(world.agent.id)
            elif name == "draft_variant":
                args |= {"finding_id": str(world.finding_id), "target_id": str(world.agent.id)}
            else:
                args["experiment_id"] = str(uuid.uuid4())
            assert await _call(str(user.id), name, **args) == WRITE_REFUSED
        async with sessions() as db:
            assert await db.scalar(select(func.count()).select_from(ConciergeFinding)) == 2


@pytest.mark.asyncio
async def test_the_tools_answer_off_while_the_capability_is_off(monkeypatch):
    async with _world(monkeypatch, "cq_off") as (world, _sessions):
        monkeypatch.setattr(availability, "concierge_enabled", lambda: False)
        assert (
            await _call(str(world.admin.id), "list_findings", target_id=str(world.agent.id))
            == "Error: the Concierge is not enabled on this platform."
        )


async def _drafted(sessions, world: World) -> uuid.UUID:
    """A persona experiment drafted for the seeded finding, as the Quality Analyst would."""
    from app.services.health import experiments, text_kinds  # noqa: F401 - registers the text kinds

    async with sessions() as db:
        finding = await db.get(ConciergeFinding, world.finding_id)
        experiment, _ = await experiments.draft(
            db,
            finding=finding,
            change_type="persona",
            target_id=world.agent.id,
            payload={"text": "You bill. Keep each search query short.", "summary": "From the failed searches."},
            author_agent_id=world.analyst.id,
        )
        await db.commit()
        return experiment.id


async def _cycle_run(sessions, world: World) -> tuple[uuid.UUID, uuid.UUID]:
    """An assignment run of the Experimenter that a Concierge cycle started: ``(run, cycle)``."""
    from app.models.assignment import AssignmentRun
    from app.services.health.memory_steward_deps import CYCLE_KEY

    cycle = uuid.uuid4()
    async with sessions() as db:
        run = AssignmentRun(
            agent_id=world.experimenter.id, user_id=world.admin.id, status="running", trigger_context={CYCLE_KEY: str(cycle)}
        )
        db.add(run)
        await db.commit()
        return run.id, cycle


@pytest.mark.asyncio
async def test_a_concierge_run_s_experiment_is_sized_on_its_cycle(monkeypatch):
    from app.services.health import experiments
    from app.services.health.access import load_reader

    async with _world(monkeypatch, "cq_cycle") as (world, sessions):
        experiment_id = await _drafted(sessions, world)
        run_id, cycle = await _cycle_run(sessions, world)
        seen: list[dict] = []

        async def _run(_db, **kwargs):
            seen.append(kwargs)
            return kwargs["experiment"]

        monkeypatch.setattr(experiments, "run", _run)
        _defs, executor = build_concierge_experiment_tools(
            "", run_id=str(run_id), agent_id=str(world.experimenter.id)
        )
        with _as(world.experimenter, "experimenter"):
            await executor.execute_tool(ToolCall(id="c1", name="run_experiment", arguments={"experiment_id": str(experiment_id)}))
            async with sessions() as db:
                reader = await load_reader(db, "", run_id="not-a-run", agent_id=str(world.experimenter.id))
                assert (reader.is_concierge, reader.cycle_id) == (True, None)
        (call,) = seen
        assert call["cycle_id"] == cycle
        assert call["experiment"].id == experiment_id
        async with sessions() as db:
            # A person is never the Concierge: no cycle is read for them.
            assert (await load_reader(db, str(world.owner.id), run_id=str(run_id))).cycle_id is None


@pytest.mark.asyncio
async def test_only_the_experimenter_settles_an_experiment_it_reads(monkeypatch):
    from app.services.health import experiments

    async with _world(monkeypatch, "cq_settle") as (world, sessions):
        experiment_id = await _drafted(sessions, world)
        settled: list[uuid.UUID] = []

        async def _settle(_db, experiment):
            settled.append(experiment.id)
            return experiment

        monkeypatch.setattr(experiments, "settle", _settle)
        with _as(world.analyst, "quality_analyst"):
            read = _parsed(await _call("", "get_experiment", experiment_id=str(experiment_id)))
        assert (read["id"], settled) == (str(experiment_id), [])
        with _as(world.experimenter, "experimenter"):
            await _call("", "get_experiment", experiment_id=str(experiment_id))
        assert settled == [experiment_id]


@pytest.mark.asyncio
@pytest.mark.parametrize(("who", "content"), [("chats", True), ("config", False)])
async def test_an_experiment_s_summary_needs_content_access(monkeypatch, who, content):
    async with _world(monkeypatch, f"cq_exp_{who}") as (world, sessions):
        experiment_id = await _drafted(sessions, world)
        user = getattr(world, who)
        listed = _parsed(await _call(str(user.id), "list_experiments", finding_id=str(world.finding_id)))
        read = _parsed(await _call(str(user.id), "get_experiment", experiment_id=str(experiment_id)))
    (served,) = listed["experiments"]
    for payload in (served, read):
        assert payload["variant_summary"] == ("From the failed searches." if content else "")
        assert payload.get("redacted", False) is (not content)


@pytest.mark.asyncio
async def test_a_finding_recorded_with_content_access_is_content_whatever_it_cites(monkeypatch):
    async with _world(monkeypatch, "cq_content_flag") as (world, sessions):
        with _as(world.analyst, "quality_analyst"):
            recorded = json.loads(
                await _call(
                    "",
                    "record_finding",
                    target_type="agent",
                    target_id=str(world.agent.id),
                    pattern_kind="latency",
                    hypothesis="Users say the answers come late.",
                    evidence={"counts": {"p90_s": 41}},
                )
            )
        async with sessions() as db:
            row = await db.get(ConciergeFinding, uuid.UUID(recorded["finding_id"]))
            assert (row.content_evidence, row.evidence["trace_ids"], row.evidence["rating_ids"]) == (True, [], [])


@pytest.mark.asyncio
async def test_an_agent_run_inline_by_the_concierge_reads_as_its_runner(monkeypatch):
    async with _world(monkeypatch, "cq_inline") as (world, _sessions):
        # A sandbox copy's turn inside the Experimenter's run: the principal is still bound.
        with _as(world.experimenter, "experimenter"):
            for user_id, answer in (("", "Error: this tool needs an authenticated caller."), (str(world.stranger.id), None)):
                _defs, executor = build_concierge_diagnosis_tools(user_id, agent_id=str(world.agent.id))
                listed = (await executor.execute_tool(ToolCall(id="c1", name="list_findings", arguments={}))).content
                assert listed == (answer or "Error: name the agent or assignment whose findings to list (target_id).")
                signals = (
                    await executor.execute_tool(
                        ToolCall(id="c2", name="get_quality_signals", arguments={"agent_id": str(world.agent.id)})
                    )
                ).content
                assert signals == (answer or NOT_IN_REACH)
