"""Built-in fleet seed (#8988) against a real PostgreSQL.

Pinned: the seed is idempotent (a second boot creates and changes nothing),
company-managed and ``is_builtin`` in a company-managed workspace, members
resolved by slug with parents, exposure and the orchestrator; a member whose
agent is not seeded is left out and its reports move up; copilot mode seeds
none and sweeps the built-in ones; a stale built-in fleet is swept and a user
fleet is never touched; the entry agent gets a handover to the orchestrator.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.agents.fleet_seed import SEED_DEFAULT_AUDIT_ACTION, seed_builtin_fleets
from app.models.agent import Agent
from app.models.audit_log import AuditLog
from app.models.fleet import FLEET_ROLE_CONNECTED, FLEET_ROLE_MEMBER, Fleet, FleetMember
from app.models.user import User
from app.models.workspace import Workspace
from app.services.workspaces import create_workspace
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_SPEC = {
    "slug": "ctem-test-fleet",
    "name": "Test fleet",
    "icon": "Crosshair",
    "orchestrator": "t-orch",
    "members": [
        {"agent": "t-orch", "role_label": "Orchestrator"},
        {"agent": "t-lead", "role_label": "Lead", "parent": "t-orch"},
        {"agent": "t-gated", "role_label": "Gated", "parent": "t-lead"},
        {"agent": "t-hunt", "role_label": "Hunt", "parent": "t-gated", "exposed": True},
    ],
    "connected": [{"agent": "t-friend", "collaboration_instruction": "Consulted"}],
    "entry_handovers": [{"from": "t-entry", "type": "delegate"}],
}


async def _setup(db) -> User:
    admin = User(
        id=uuid.uuid4(),
        email=f"bf-{uuid.uuid4().hex[:8]}@example.com",
        full_name="Admin",
        hashed_password="x",
        role="admin",
    )
    db.add(admin)
    await db.flush()
    # t-gated is not seeded: a feature-gated member of another deployment.
    for slug in ("t-orch", "t-lead", "t-hunt", "t-friend", "t-entry"):
        db.add(Agent(id=uuid.uuid4(), name=slug, slug=slug, user_id=admin.id, is_builtin=True, is_company_managed=True))
    await db.commit()
    return admin


async def _members(db, fleet: Fleet) -> dict[str, FleetMember]:
    rows = (
        await db.execute(
            select(FleetMember, Agent.slug)
            .join(Agent, Agent.id == FleetMember.agent_id)
            .where(FleetMember.fleet_id == fleet.id)
        )
    ).all()
    return {slug: row for row, slug in rows}


async def test_seed_is_idempotent_and_resolves_members():
    async with (
        scratch_database("bf_seed", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as s,
        s.sessions() as db,
    ):
        await _setup(db)
        assert await seed_builtin_fleets(db, platform_mode="xtm_one", fleets=[_SPEC]) == 1
        assert await seed_builtin_fleets(db, platform_mode="xtm_one", fleets=[_SPEC]) == 0
        fleets = (await db.execute(select(Fleet))).scalars().all()
        assert len(fleets) == 1
        fleet = fleets[0]
        assert fleet.is_builtin and fleet.is_company_managed and fleet.slug == "ctem-test-fleet"
        workspace = (await db.execute(select(Workspace).where(Workspace.id == fleet.workspace_id))).scalar_one()
        assert workspace.is_company_managed
        rows = await _members(db, fleet)
        assert set(rows) == {"t-orch", "t-lead", "t-hunt", "t-friend"}
        assert rows["t-friend"].role == FLEET_ROLE_CONNECTED
        assert rows["t-orch"].role == FLEET_ROLE_MEMBER and rows["t-orch"].is_exposed
        assert rows["t-orch"].parent_member_id is None
        assert rows["t-lead"].parent_member_id == rows["t-orch"].id
        # t-gated is absent: its report moves up to t-lead.
        assert rows["t-hunt"].parent_member_id == rows["t-lead"].id and rows["t-hunt"].is_exposed
        orch = (await db.execute(select(Agent).where(Agent.slug == "t-orch"))).scalar_one()
        assert fleet.orchestrator_agent_id == orch.id
        entry = (await db.execute(select(Agent).where(Agent.slug == "t-entry"))).scalar_one()
        assert str(orch.id) in [str(i) for i in entry.handover_agent_ids]
        assert entry.handover_types[str(orch.id)] == "delegate"
        # #11948: every home member holds a Consult handover to the orchestrator.
        for slug in ("t-lead", "t-hunt"):
            member = (await db.execute(select(Agent).where(Agent.slug == slug))).scalar_one()
            assert str(orch.id) in [str(i) for i in member.handover_agent_ids]
            assert member.handover_types[str(orch.id)] == "consult"
        friend = (await db.execute(select(Agent).where(Agent.slug == "t-friend"))).scalar_one()
        assert str(orch.id) not in [str(i) for i in friend.handover_agent_ids or []]
        assert not orch.handover_types or str(orch.id) not in orch.handover_types
        assert len((await db.execute(select(Workspace))).scalars().all()) == 1


async def test_copilot_seeds_none_and_stale_sweep_keeps_user_fleets():
    async with (
        scratch_database("bf_sweep", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as s,
        s.sessions() as db,
    ):
        admin = await _setup(db)
        workspace = await create_workspace(db, user_id=admin.id, name="Mine")
        db.add(Fleet(user_id=admin.id, slug="mine", name="Mine", workspace_id=workspace.id))
        await db.commit()
        assert await seed_builtin_fleets(db, platform_mode="copilot", fleets=[_SPEC]) == 0
        assert [f.slug for f in (await db.execute(select(Fleet))).scalars()] == ["mine"]
        assert await seed_builtin_fleets(db, platform_mode="dev", fleets=[_SPEC]) == 1
        # The release drops the entry: the built-in fleet is swept, the user's stays.
        assert await seed_builtin_fleets(db, platform_mode="dev", fleets=[]) == 0
        remaining = (await db.execute(select(Fleet))).scalars().all()
        assert [(f.slug, f.is_builtin) for f in remaining] == [("mine", False)]


async def _seed_rows(db, fleet: Fleet) -> list[AuditLog]:
    return list(
        (
            await db.execute(
                select(AuditLog).where(AuditLog.entity_id == fleet.id, AuditLog.action == SEED_DEFAULT_AUDIT_ACTION)
            )
        ).scalars()
    )


async def test_a_seeded_fleet_ships_at_low_recorded_as_the_seed_default_once():
    """#10005: fresh boot = Low, one system audit row, a second boot writes nothing; custom fleets keep full_gates."""
    async with (
        scratch_database("bf_level", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as s,
        s.sessions() as db,
    ):
        admin = await _setup(db)
        workspace = await create_workspace(db, user_id=admin.id, name="Mine")
        db.add(Fleet(user_id=admin.id, slug="mine", name="Mine", workspace_id=workspace.id))
        await db.commit()
        await seed_builtin_fleets(db, platform_mode="xtm_one", fleets=[_SPEC])
        await seed_builtin_fleets(db, platform_mode="xtm_one", fleets=[_SPEC])
        fleet = (await db.execute(select(Fleet).where(Fleet.is_builtin.is_(True)))).scalar_one()
        assert fleet.autonomy_level == "low"
        (row,) = await _seed_rows(db, fleet)
        assert row.user_email == "system:fleet-seed"
        assert row.details["from"] == "full_gates" and row.details["to"] == "low"
        mine = (await db.execute(select(Fleet).where(Fleet.slug == "mine"))).scalar_one()
        assert mine.autonomy_level == "full_gates"


async def test_a_level_a_person_set_survives_a_reboot():
    """#10005: an existing fleet a person moved (back) to full_gates is never moved to Low by the seed."""
    async with (
        scratch_database("bf_level_kept", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as s,
        s.sessions() as db,
    ):
        admin = await _setup(db)
        await seed_builtin_fleets(db, platform_mode="xtm_one", fleets=[_SPEC])
        fleet = (await db.execute(select(Fleet).where(Fleet.is_builtin.is_(True)))).scalar_one()
        fleet.autonomy_level = "full_gates"
        db.add(
            AuditLog(
                user_id=admin.id,
                user_email=admin.email,
                action="autonomy_level_change",
                entity_type="fleet",
                entity_id=fleet.id,
                entity_name=fleet.name,
                summary="Set by a person",
                details={"from": "low", "to": "full_gates"},
            )
        )
        await db.commit()
        await seed_builtin_fleets(db, platform_mode="xtm_one", fleets=[_SPEC])
        await db.refresh(fleet)
        assert fleet.autonomy_level == "full_gates"
        assert len(await _seed_rows(db, fleet)) == 1


_OLD_ROOT = {**_SPEC, "slug": "t-old-root", "name": "Old root"}
_OLD_DESK = {
    "slug": "t-old-desk",
    "name": "Old desk",
    "icon": "Crosshair",
    "orchestrator": "t-friend",
    "members": [{"agent": "t-friend", "role_label": "Orchestrator"}],
}
_MERGED = {
    **_SPEC,
    "slug": "t-loop",
    "name": "Loop",
    "previous_slugs": ["t-old-root"],
    "absorbs": ["t-old-desk"],
    "leads": ["t-friend"],
    "members": [*_SPEC["members"], {"agent": "t-friend", "role_label": "Desk", "parent": "t-orch"}],
    "connected": [],
}


async def test_an_upgrade_adopts_the_previous_row_folds_the_absorbed_fleet_and_keeps_its_pins():
    """CTEM-31 (#1048): the old root row is renamed (same id), the absorbed fleet is swept, its pins carried."""
    async with (
        scratch_database("bf_ctem31", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as s,
        s.sessions() as db,
    ):
        await _setup(db)
        await seed_builtin_fleets(db, platform_mode="xtm_one", fleets=[_OLD_ROOT, _OLD_DESK])
        rows = {f.slug: f for f in (await db.execute(select(Fleet))).scalars()}
        root_id = rows["t-old-root"].id
        rows["t-old-desk"].approval_pins = {"g.desk": "wait"}
        rows["t-old-root"].approval_pins = {"g.root": "wait"}
        await db.commit()
        await seed_builtin_fleets(db, platform_mode="xtm_one", fleets=[_MERGED])
        fleets = (await db.execute(select(Fleet))).scalars().all()
        assert [f.slug for f in fleets] == ["t-loop"]
        loop = fleets[0]
        assert loop.id == root_id and loop.name == "Loop"
        assert loop.approval_pins == {"g.desk": "wait", "g.root": "wait"}
        members = await _members(db, loop)
        assert members["t-friend"].role == FLEET_ROLE_MEMBER
        assert members["t-friend"].parent_member_id == members["t-orch"].id
        # A second boot changes nothing.
        assert await seed_builtin_fleets(db, platform_mode="xtm_one", fleets=[_MERGED]) == 0


async def test_an_existing_fleet_never_set_by_a_person_moves_to_low_at_the_next_boot():
    async with (
        scratch_database(
            "bf_level_upgrade", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
        ) as s,
        s.sessions() as db,
    ):
        await _setup(db)
        await seed_builtin_fleets(db, platform_mode="xtm_one", fleets=[_SPEC])
        fleet = (await db.execute(select(Fleet).where(Fleet.is_builtin.is_(True)))).scalar_one()
        # A fleet seeded before #10005: still at the stored default, no audit row.
        fleet.autonomy_level = "full_gates"
        for row in await _seed_rows(db, fleet):
            await db.delete(row)
        await db.commit()
        await seed_builtin_fleets(db, platform_mode="xtm_one", fleets=[_SPEC])
        await db.refresh(fleet)
        assert fleet.autonomy_level == "low"
        assert len(await _seed_rows(db, fleet)) == 1
