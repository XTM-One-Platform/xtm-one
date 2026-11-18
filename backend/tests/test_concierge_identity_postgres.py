"""Platform Concierge level 1 (#8254): the identity and the settings against PostgreSQL.

* Settings > Agents and automations > Concierge saved and read back, audited, and ``concierge_active``
  answering the flag AND the administrator's switch.
* A run in scope is marked as the Concierge's for a fleet agent with no user
  or an admin user, never for a non-admin or an unknown one.

``integration``-marked; skips when PostgreSQL is unreachable.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.audit_log import AuditLog
from app.services.health.availability import concierge_active
from app.services.health.identity import concierge_run_scope, current_concierge, mark_concierge_run
from app.services.health.settings import ConciergeSettings, load_concierge_settings
from tests.concierge_support import World, concierge_database, configure

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    monkeypatch.setattr(settings, "enabled_features", "CONCIERGE")


async def test_settings_are_saved_audited_and_switch_the_concierge_on(monkeypatch) -> None:
    async with concierge_database() as sessions:
        world = await World().build(sessions)
        async with sessions() as db:
            assert await load_concierge_settings(db) == ConciergeSettings()
            assert await concierge_active(db) is False
        await configure(sessions, world.admin, enabled=True, scope_mode="selected", scope_agent_ids=[world.private.id])
        async with sessions() as db:
            stored = await load_concierge_settings(db)
            assert (stored.enabled, stored.scope_mode, stored.scope_agent_ids) == (True, "selected", [world.private.id])
            assert await concierge_active(db) is True
            rows = (
                await db.execute(select(AuditLog).where(AuditLog.entity_type == "health_settings"))
            ).scalars().all()
            assert [(r.action, r.user_id, r.details["after"]["enabled"]) for r in rows] == [
                ("configure", world.admin.id, True)
            ]


@pytest.mark.parametrize(("who", "acts"), [("nobody", True), ("admin", True), ("owner", False), ("unknown", False)])
async def test_a_fleet_run_is_marked_for_no_user_or_an_admin_only(who: str, acts: bool) -> None:
    async with concierge_database() as sessions:
        world = await World().build(sessions)
        user_id = {"nobody": None, "admin": world.admin.id, "owner": world.owner.id}.get(who, world.fleet.id)
        async with sessions() as db:
            fleet = await db.get(type(world.fleet), world.fleet.id)
            with concierge_run_scope():
                principal = await mark_concierge_run(db, fleet, user_id)
                assert (current_concierge() is not None) is acts
                assert (principal is not None) is acts
                if principal is not None:
                    assert principal.on_behalf_of_admin_id == (world.admin.id if who == "admin" else None)
            assert current_concierge() is None
            # Any other agent is never marked, whoever runs it.
            with concierge_run_scope():
                assert await mark_concierge_run(db, await db.get(type(world.company), world.company.id), None) is None
