"""Platform Concierge level 1 (#8254): the one consent rule against PostgreSQL.

* The predicate, the SQL filter and the snapshot give the same answer on one
  matrix of targets, users and scopes, read live (no cache).
* An audited content read names ``system:concierge``; a refused one writes
  nothing.

``integration``-marked; skips when PostgreSQL is unreachable.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.core.config import settings
from app.models.agent import Agent
from app.models.audit_log import AuditLog
from app.models.flow import Flow
from app.models.user import User
from app.services.concierge import consent
from app.services.concierge.consent import ConsentDeniedError
from app.services.concierge.identity import acting_as_concierge, concierge_principal_for
from tests.concierge_support import World, concierge_database, configure, make_agent

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    monkeypatch.setattr(settings, "enabled_features", "CONCIERGE")


# ── The consent rule: predicate, SQL filter and snapshot agree ────────────


@pytest.mark.parametrize("scope", ["all", "selected", "disabled"])
async def test_the_predicate_the_filter_and_the_snapshot_agree(scope: str) -> None:
    async with concierge_database() as sessions:
        world = await World().build(sessions)
        async with sessions() as db:
            opted_in = make_agent(world.owner, "opted-in", health_enrolled=True)
            other_flow = Flow(id=uuid.uuid4(), user_id=world.owner.id, name="other", graph={})
            db.add_all([opted_in, other_flow])
            (await db.get(Agent, world.fleet.id)).health_enrolled = True
            (await db.get(Flow, world.flow.id)).health_enrolled = True
            (await db.get(User, world.revoked.id)).health_content_opt_out = True
            await db.commit()
        await configure(
            sessions,
            world.admin,
            enabled=scope != "disabled",
            scope_mode="selected" if scope == "selected" else "all",
            scope_agent_ids=[opted_in.id, world.private.id, world.fleet.id],
            scope_flow_ids=[world.flow.id],
        )
        agents = [opted_in.id, world.private.id, world.fleet.id, uuid.uuid4(), None]
        flows = [world.flow.id, other_flow.id, None]
        users = [world.reader.id, world.revoked.id, None]
        rows = [(a, f, u) for a in agents for f in flows for u in users]
        table = sa.values(
            sa.column("agent_id", PG_UUID(as_uuid=True)),
            sa.column("flow_id", PG_UUID(as_uuid=True)),
            sa.column("user_id", PG_UUID(as_uuid=True)),
            name="rows",
        ).data(rows)
        async with sessions() as db:
            clause = await consent.consent_filter(
                db, agent_id_col=table.c.agent_id, flow_id_col=table.c.flow_id, user_id_cols=[table.c.user_id]
            )
            by_sql = {tuple(r) for r in (await db.execute(select(table).where(clause))).all()}
            snapshot = await consent.load_consent_scope(db)
            by_python = {
                r for r in rows if await consent.content_readable(db, agent_id=r[0], flow_id=r[1], user_id=r[2])
            }
            by_snapshot = {r for r in rows if snapshot.allows_content(agent_id=r[0], flow_id=r[1], user_ids=[r[2]])}
        expected = (
            set()
            if scope == "disabled"
            else {
                (opted_in.id, world.flow.id, world.reader.id),
                (opted_in.id, world.flow.id, None),
                (opted_in.id, None, world.reader.id),
                (opted_in.id, None, None),
                (None, world.flow.id, world.reader.id),
                (None, world.flow.id, None),
            }
        )
        assert by_sql == expected
        assert by_python == expected
        assert by_snapshot == expected


async def test_a_revocation_applies_to_the_next_read() -> None:
    async with concierge_database() as sessions:
        world = await World().build(sessions)
        await configure(sessions, world.admin, enabled=True)
        async with sessions() as db:
            (await db.get(Agent, world.private.id)).health_enrolled = True
            await db.commit()
            assert await consent.content_readable(db, agent_id=world.private.id, user_id=world.reader.id) is True
            assert await consent.content_readable_agent_ids(db) == frozenset({world.private.id})
            assert await consent.in_supervision_scope(db, "agent", world.private.id) is True
            assert await consent.in_supervision_scope(db, "agent", world.fleet.id) is False
            assert await consent.in_supervision_scope(db, "flow", world.flow.id) is True
        async with sessions() as other:
            (await other.get(User, world.reader.id)).health_content_opt_out = True
            await other.commit()
        async with sessions() as db:
            assert await consent.content_readable(db, agent_id=world.private.id, user_id=world.reader.id) is False
            assert await consent.opted_out_user_ids(db) == frozenset({world.reader.id})
            # Metrics need no opt-in and survive an opt-out.
            assert await consent.in_supervision_scope(db, "agent", world.private.id) is True


async def test_an_audited_read_names_the_concierge_and_a_refused_one_writes_nothing() -> None:
    async with concierge_database() as sessions:
        world = await World().build(sessions)
        await configure(sessions, world.admin, enabled=True)
        trace_id = uuid.uuid4()
        async with sessions() as db:
            (await db.get(Agent, world.private.id)).health_enrolled = True
            (await db.get(User, world.revoked.id)).health_content_opt_out = True
            await db.commit()
            principal = concierge_principal_for(await db.get(Agent, world.fleet.id), None)
            with acting_as_concierge(principal):
                await consent.require_content_read(
                    db, kind="trace", ref_id=trace_id, agent_id=world.private.id, user_id=world.reader.id
                )
                await consent.audit_content_read(db, kind="memory", ref_id="lane:agent", agent_id=world.private.id)
                with pytest.raises(ConsentDeniedError) as denied:
                    await consent.require_content_read(
                        db, kind="trace", ref_id=uuid.uuid4(), agent_id=world.private.id, user_id=world.revoked.id
                    )
            await db.commit()
            assert str(denied.value) == consent.DENIED_MESSAGE
            rows = (
                await db.execute(
                    select(AuditLog)
                    .where(AuditLog.action == "concierge_content_read")
                    .order_by(AuditLog.entity_type)
                )
            ).scalars().all()
        assert [(r.user_email, r.entity_type, r.user_id) for r in rows] == [
            ("system:concierge", "memory", world.owner.id),
            ("system:concierge", "trace", world.reader.id),
        ]
        assert rows[1].entity_id == trace_id
        assert rows[1].details["via_agent_id"] == str(world.fleet.id)
        assert rows[0].details["ref"] == "lane:agent"

