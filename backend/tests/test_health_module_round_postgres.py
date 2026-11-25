"""The daily module round with zero open findings (#101 S15d-2, dev-docs/platform-health.md 13.9 (l)).

XTM One mode, Platform Health on, an OpenCTI, an OpenAEV and an OpenCRQ registered through
their integrations and no open finding: one trigger pass starts one checker cycle per
registration with no administrator action, each run reads its own module's tools and
records a check report with every check of its playbook. The model and the integrations
are stubbed: the run is replaced by the calls a checker makes, the report write is the
real tool.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.core.config import settings
from app.models.agent import Agent
from app.models.assignment import AssignmentRun
from app.models.health import HealthCycle
from app.models.health_check import STATUS_OPEN, HealthCheckFinding
from app.models.health_module_analyst import HealthModuleCheckReport
from app.models.integration import Integration
from app.models.platform_registration import PlatformRegistration
from app.services import assignment_engine
from app.services.builtin_tools import health_modules
from app.services.health import budget, fleet_seed, module_analyst
from app.services.health.access import Reader
from app.services.health.cycles import CYCLE_CONTEXT_KEY
from app.services.health.identity import CHECKER_ROLE_BY_PROVIDER, HealthPrincipal
from tests.health_support import configure, health_database, make_user

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 22, 6, 0, tzinfo=UTC)


def _playbook_checks(provider: str) -> list[str]:
    """The check ids of the seeded playbook of *provider*, in table order."""
    from app.services.health.check_reports import module_playbook

    text = module_playbook(provider)
    return re.findall(r"^\| `([a-z0-9_]+)` \|", text, flags=re.MULTILINE)


@pytest.fixture
def xtm_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "platform_mode", "xtm_one")

    async def _no_gate(_db: Any) -> None:
        return None

    # The licence and AI gate is pinned by its own tests; here the round itself.
    monkeypatch.setattr(budget, "_gate", _no_gate)


async def _register(db: Any, provider: str, owner: uuid.UUID) -> uuid.UUID:
    integration = Integration(id=uuid.uuid4(), provider=provider, name=f"{provider}-lab", user_id=owner)
    db.add(integration)
    await db.flush()
    registration = PlatformRegistration(
        id=uuid.uuid4(),
        platform_identifier=provider,
        platform_url=f"https://{provider}.example.com",
        platform_title=provider,
        platform_version="1.0.0",
        status="active",
        integration_id=integration.id,
    )
    db.add(registration)
    await db.flush()
    return registration.id


@pytest.mark.asyncio
async def test_the_daily_round_checks_every_module_with_zero_open_findings(
    xtm_one: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs: list[dict[str, Any]] = []

    async def _checker_run(assignment, agent, payload=None, db=None, trigger_context=None, **_kw):
        """What the checker's run does, model stubbed: its module's reads, then its report."""
        run = AssignmentRun(
            assignment_id=assignment.id,
            agent_id=agent.id,
            user_id=assignment.user_id,
            status="running",
            trigger_payload=payload,
            trigger_context=trigger_context,
        )
        db.add(run)
        await db.commit()
        runs.append({"agent_role": agent.health_role, "payload": payload, "context": trigger_context})
        return str(run.id)

    monkeypatch.setattr(assignment_engine, "trigger_assignment", _checker_run)

    admin = make_user("admin", admin=True)
    async with health_database() as sessions:
        async with sessions() as db:
            db.add(admin)
            await db.commit()
            await fleet_seed.seed_health_fleet(db)
            registrations = {p: await _register(db, p, admin.id) for p in module_analyst.MODULE_PROVIDERS}
            await db.commit()
        await configure(sessions, admin, enabled=True)

        async with sessions() as db:
            started = await module_analyst.run_module_triggers(db, now=NOW)

        # Each run reads only its module's tools, then records every check of its playbook.
        provider_by_registration = {str(rid): p for p, rid in registrations.items()}
        read_tools: dict[str, list[str]] = {}
        for run in runs:
            provider = provider_by_registration[run["payload"]["target_id"]]
            tools = list(module_analyst.MODULE_CHECKER_TOOLS[provider])
            assert module_analyst.filter_module_tools(provider, tools) == tools
            read_tools[provider] = tools
            async with sessions() as db:
                agent = (await db.execute(select(Agent).where(Agent.health_role == run["agent_role"]))).scalar_one()
                reader = Reader(
                    user=None,
                    principal=HealthPrincipal(agent_id=agent.id, role=run["agent_role"]),
                    scope=type("Scope", (), {"opted_out_user_ids": frozenset()})(),
                    cycle_id=uuid.UUID(run["context"][CYCLE_CONTEXT_KEY]),
                )
                checks = [
                    {"id": check, "verdict": "pass", "evidence": [{"tool": tools[0], "values": {"ok": True}}]}
                    for check in _playbook_checks(provider)
                ]
                await health_modules._record_module_check_report(db, reader, checks=checks)

        async with sessions() as db:
            cycles = (await db.execute(select(HealthCycle).where(HealthCycle.target_type == "module"))).scalars().all()
            reports = (await db.execute(select(HealthModuleCheckReport))).scalars().all()
            open_findings = (
                (await db.execute(select(HealthCheckFinding).where(HealthCheckFinding.status == STATUS_OPEN)))
                .scalars()
                .all()
            )

    assert started == 3
    assert {c.target_id for c in cycles} == set(registrations.values())
    assert {c.trigger for c in cycles} == {"schedule"}
    assert sorted(r["agent_role"] for r in runs) == sorted(CHECKER_ROLE_BY_PROVIDER.values())
    assert {r["payload"]["module_trigger"] for r in runs} == {"round"}
    assert set(read_tools) == set(module_analyst.MODULE_PROVIDERS)
    by_registration = {r.registration_id: r for r in reports}
    assert set(by_registration) == set(registrations.values())
    for provider, registration_id in registrations.items():
        report = by_registration[registration_id]
        expected = _playbook_checks(provider)
        assert report.module == provider
        assert [c["id"] for c in report.checks] == expected
        assert (report.passed, report.warned, report.failed, report.not_run) == (len(expected), 0, 0, 0)
    assert open_findings == []


@pytest.mark.asyncio
async def test_a_second_pass_the_same_day_starts_no_round(xtm_one: None, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def _checker_run(assignment, agent, payload=None, db=None, trigger_context=None, **_kw):
        run = AssignmentRun(
            assignment_id=assignment.id, agent_id=agent.id, user_id=assignment.user_id, status="completed"
        )
        db.add(run)
        await db.commit()
        calls.append(payload["target_id"])
        return str(run.id)

    monkeypatch.setattr(assignment_engine, "trigger_assignment", _checker_run)
    admin = make_user("admin", admin=True)
    async with health_database() as sessions:
        async with sessions() as db:
            db.add(admin)
            await db.commit()
            await fleet_seed.seed_health_fleet(db)
            for provider in module_analyst.MODULE_PROVIDERS:
                await _register(db, provider, admin.id)
            await db.commit()
        await configure(sessions, admin, enabled=True)
        async with sessions() as db:
            first = await module_analyst.run_module_triggers(db, now=NOW)
            await db.execute(HealthCycle.__table__.update().values(status="completed", closed_at=NOW))
            await db.commit()
            later = NOW + module_analyst.PASS_CADENCE * 2
            second = await module_analyst.run_module_triggers(db, now=later)

    assert (first, second) == (3, 0)
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_the_fleet_data_flow_draws_each_checker_reading_its_modules_integration(xtm_one: None) -> None:
    """#101 S15f-1, section 13.8: the checkers' inputs are the module integrations, drawn read."""
    from sqlalchemy import true

    from app.models.fleet import Fleet
    from app.services.fleet_flow import EDGE_READ, build_fleet_flow
    from app.services.health.read_only import HEALTH_FLEET_SLUG

    admin = make_user("admin", admin=True)
    async with health_database() as sessions, sessions() as db:
        db.add(admin)
        await db.commit()
        await fleet_seed.seed_health_fleet(db)
        registrations = {p: await _register(db, p, admin.id) for p in module_analyst.MODULE_PROVIDERS}
        await db.commit()
        reads = await module_analyst.checker_reads(db)
        fleet = (await db.execute(select(Fleet).where(Fleet.slug == HEALTH_FLEET_SLUG))).scalar_one()
        flow = await build_fleet_flow(db, fleet, visible_agent_clause=true(), visible_integration_clause=true())
        checkers = {
            agent.health_role: agent.id
            for agent in (
                await db.execute(select(Agent).where(Agent.health_role.in_(CHECKER_ROLE_BY_PROVIDER.values())))
            ).scalars()
        }
        integrations = {
            p: (await db.get(PlatformRegistration, rid)).integration_id for p, rid in registrations.items()
        }

    assert {role: [p for p, _ in pairs] for role, pairs in reads.items()} == {
        CHECKER_ROLE_BY_PROVIDER[p]: [p] for p in module_analyst.MODULE_PROVIDERS
    }
    edges = {(e.source, e.target, e.kind) for e in flow.edges}
    for provider, role in CHECKER_ROLE_BY_PROVIDER.items():
        node = f"output:integration:{provider}:{integrations[provider]}"
        assert (f"agent:{checkers[role]}", node, EDGE_READ) in edges
    # Read only: no checker draws a write edge.
    assert not [e for e in flow.edges if e.source in {f"agent:{i}" for i in checkers.values()} and e.kind != EDGE_READ]


@pytest.mark.asyncio
async def test_the_seeded_fleet_delegates_to_each_checker_and_each_holds_its_playbook(xtm_one: None) -> None:
    """#101 S15f-2, 13.9 (n): the orchestrator's delegate handovers, the checkers' skills, the graph edges."""
    from sqlalchemy import true

    from app.agents.seed import seed_builtin_skills
    from app.models.agent_skill import AgentSkill
    from app.models.fleet import Fleet
    from app.services.fleet_flow import EDGE_HANDOVER, build_fleet_flow
    from app.services.health.read_only import HEALTH_FLEET_SLUG

    admin = make_user("admin", admin=True)
    async with health_database() as sessions, sessions() as db:
        db.add(admin)
        await db.commit()
        await seed_builtin_skills(db, platform_mode="xtm_one")
        await db.commit()
        await fleet_seed.seed_health_fleet(db)
        rows = (await db.execute(select(Agent).where(Agent.health_role.is_not(None)))).scalars()
        agents = {a.health_role: a for a in rows}
        skills = {
            str(skill.id): skill.tags
            for skill in (await db.execute(select(AgentSkill).where(AgentSkill.is_company_managed.is_(True)))).scalars()
        }
        fleet = (await db.execute(select(Fleet).where(Fleet.slug == HEALTH_FLEET_SLUG))).scalar_one()
        flow = await build_fleet_flow(db, fleet, visible_agent_clause=true(), visible_integration_clause=true())

    orchestrator = agents["orchestrator"]
    edges = {(e.source, e.target, e.kind, e.type) for e in flow.edges}
    for provider, role in CHECKER_ROLE_BY_PROVIDER.items():
        checker = agents[role]
        assert str(checker.id) in [str(i) for i in orchestrator.handover_agent_ids or []], role
        assert (orchestrator.handover_types or {}).get(str(checker.id)) == "delegate", role
        assert (f"agent:{orchestrator.id}", f"agent:{checker.id}", EDGE_HANDOVER, "delegate") in edges, role
        held = [skills[str(i)] for i in checker.skill_ids or [] if str(i) in skills]
        assert any("slug:health-module-checks" in (tags or []) for tags in held), (provider, role, held)
