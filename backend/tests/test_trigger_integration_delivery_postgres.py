"""The real pollers deliver an integration's events whether or not the run's
acting user can see it, and flag exactly those who cannot, on PostgreSQL.

``test_trigger_integration_delivery.py`` drives every path with an in-memory
session; here the real ``poll_integration_events``, ``poll_flow_integration_events``
and the security audit's listing run against real rows, so the visibility
predicate - admin short-circuit, per-user and group grants,
``access_restricted`` - is PostgreSQL's own answer. Every owner is a NON-admin
unless named otherwise. Only the provider call is stubbed: it records which
assignment or flow node was polled and returns no event. Same scratch-database
recipe as ``test_agent_duplicate_bindings_postgres.py``.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.agent import Agent
from app.models.assignment import Assignment
from app.models.flow import FLOW_STATUS_ACTIVE, Flow
from app.models.group import Group, GroupMember
from app.models.integration import Integration
from app.models.user import User
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database(
        "trigint",
        tables=ALL_TABLES,
        extensions=("vector",),
        bind_app_sessions=False,
    ) as db:
        yield db.sessions


def _user(name: str, *, role: str = "user") -> User:
    return User(
        email=f"{name}-{uuid.uuid4().hex[:8]}@test.local",
        full_name=name.title(),
        hashed_password="x",
        role=role,
    )


def _integration(name: str, **kw) -> Integration:
    fields = {
        "provider": "microsoft",
        "name": name,
        "auth_type": "oauth",
        "status": "connected",
        "access_token_enc": "sealed-token",
    }
    fields.update(kw)
    return Integration(**fields)


@dataclass
class _World:
    #: Assignment ids whose acting user sees the integration, or that have no
    #: acting user (the OAuth gate alone), and those whose acting user does not.
    seen: set[str] = field(default_factory=set)
    unseen: set[str] = field(default_factory=set)
    #: Assignments with no acting user: never checked for visibility.
    ownerless: set[str] = field(default_factory=set)
    names: dict[str, str] = field(default_factory=dict)
    flow_seen: set[str] = field(default_factory=set)
    flow_unseen: set[str] = field(default_factory=set)


async def _seed(sessions) -> _World:
    world = _World()
    async with sessions() as session:
        owner, colleague, run_as, admin = (
            _user("owner"),
            _user("colleague"),
            _user("runas"),
            _user("admin", role="admin"),
        )
        session.add_all([owner, colleague, run_as, admin])
        await session.flush()
        owner_group = Group(name=f"owners-{uuid.uuid4().hex[:8]}")
        other_group = Group(name=f"others-{uuid.uuid4().hex[:8]}")
        session.add_all([owner_group, other_group])
        await session.flush()
        session.add_all(
            [
                GroupMember(group_id=owner_group.id, user_id=owner.id),
                GroupMember(group_id=other_group.id, user_id=colleague.id),
            ]
        )
        ack = {"oauth_share_acknowledged": True}
        rows = {
            "own": (_integration("Own mailbox", user_id=owner.id), True),
            "group_visible": (
                _integration(
                    "Team mailbox", user_id=colleague.id, shared_group_ids=[str(owner_group.id)], **ack
                ),
                True,
            ),
            "group_visible_restricted": (
                _integration(
                    "Restricted team mailbox",
                    user_id=colleague.id,
                    shared_group_ids=[str(owner_group.id)],
                    access_restricted=True,
                    **ack,
                ),
                True,
            ),
            "user_grant": (
                _integration(
                    "Granted mailbox", user_id=colleague.id, shared_user_ids=[str(owner.id)], **ack
                ),
                True,
            ),
            "company": (
                _integration("Company mailbox", user_id=None, is_company_managed=True, **ack),
                True,
            ),
            "hidden_group": (
                _integration(
                    "Other team mailbox",
                    user_id=colleague.id,
                    shared_group_ids=[str(other_group.id)],
                    **ack,
                ),
                False,
            ),
            "hidden_restricted_company": (
                _integration(
                    "Board mailbox",
                    user_id=None,
                    is_company_managed=True,
                    access_restricted=True,
                    **ack,
                ),
                False,
            ),
            "hidden_api_key": (
                _integration("Colleague key", user_id=colleague.id, auth_type="api_key"),
                False,
            ),
        }
        session.add_all([row for row, _ in rows.values()])
        await session.flush()
        ids = {key: row.id for key, (row, _) in rows.items()}

        personal = Agent(name="Mail desk", user_id=owner.id)
        fallback = Agent(
            name="Fallback desk",
            user_id=owner.id,
            integration_bindings={"microsoft": [str(ids["hidden_group"])]},
        )
        company = Agent(name="Company desk", user_id=None, is_company_managed=True)
        admins = Agent(name="Admin desk", user_id=admin.id)
        session.add_all([personal, fallback, company, admins])
        await session.flush()

        def _assignment(name, agent, integration_key=None, **kw):
            config = {"provider": "microsoft", "event": "new_email"}
            if integration_key is not None:
                config["integration_id"] = str(ids[integration_key])
            wf = Assignment(
                agent_id=agent.id,
                name=name,
                trigger_type="integration_event",
                trigger_config=config,
                prompt="Triage it",
                enabled=True,
                **kw,
            )
            session.add(wf)
            return wf

        created: list[tuple[Assignment, bool]] = []
        for key, (_row, seen) in rows.items():
            created.append((_assignment(f"Pinned {key}", personal, key, user_id=owner.id), seen))
        created.append((_assignment("Fallback hidden", fallback, user_id=owner.id), False))
        ownerless = _assignment(
            "Ownerless restricted", company, "hidden_restricted_company", is_company_managed=True
        )
        created.append((ownerless, True))
        created.append(
            (
                _assignment(
                    "Run-as restricted",
                    company,
                    "hidden_restricted_company",
                    is_company_managed=True,
                    run_as_user_id=run_as.id,
                ),
                False,
            )
        )
        created.append(
            (
                _assignment(
                    "Run-as company",
                    company,
                    "company",
                    is_company_managed=True,
                    run_as_user_id=run_as.id,
                ),
                True,
            )
        )
        created.append((_assignment("Admin pinned hidden", admins, "hidden_group", user_id=admin.id), True))
        await session.flush()
        for wf, seen in created:
            (world.seen if seen else world.unseen).add(str(wf.id))
            world.names[str(wf.id)] = wf.name
        world.ownerless.add(str(ownerless.id))

        def _flow(name, integration_key):
            return Flow(
                name=name,
                user_id=owner.id,
                status=FLOW_STATUS_ACTIVE,
                graph={
                    "nodes": [
                        {
                            "id": "trigger-1",
                            "kind": "trigger.integration_event",
                            "config": {
                                "integration_id": str(ids[integration_key]),
                                "event_type": "new_email",
                            },
                        }
                    ],
                    "edges": [],
                },
            )

        visible_flow, hidden_flow = _flow("Own flow", "own"), _flow("Hidden flow", "hidden_group")
        session.add_all([visible_flow, hidden_flow])
        await session.flush()
        world.flow_seen.add(f"flow:{visible_flow.id}:trigger-1")
        world.flow_unseen.add(f"flow:{hidden_flow.id}:trigger-1")
        world.names[str(visible_flow.id)] = visible_flow.name
        world.names[str(hidden_flow.id)] = hidden_flow.name
        await session.commit()
    return world


def _patch_providers(monkeypatch) -> list[str]:
    from app.services import event_poller
    from tests.agentic_gate_stub import stub_agentic_gate

    stub_agentic_gate(monkeypatch)
    polled: list[str] = []

    async def _poll(_provider, _event, _token, target, *_a, **_k):
        polled.append(str(target.id))
        return []

    redis = AsyncMock()
    redis.get.return_value = None
    monkeypatch.setattr(event_poller, "redis_client", AsyncMock(return_value=redis))
    monkeypatch.setattr("app.core.redis.redis_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(event_poller, "decrypt_value", lambda value: f"plain:{value}")
    monkeypatch.setattr("app.core.security.decrypt_value", lambda value: f"plain:{value}")
    monkeypatch.setattr(event_poller, "_poll_provider", _poll)
    return polled


@pytest.fixture(autouse=True)
def _fresh_unseen_log():
    from app.services import trigger_integration_access as access

    access._last_unseen_log.clear()
    yield
    access._last_unseen_log.clear()


def _unseen_lines(caplog) -> list[str]:
    from app.services import trigger_integration_access as access

    return [r.getMessage() for r in caplog.records if r.name == access.__name__]


@pytest.mark.asyncio
async def test_worked_on_main_the_event_poller_delivers_whoever_sees_the_integration(
    monkeypatch, caplog
):
    from app.services import trigger_integration_access as access
    from app.services.event_poller import poll_integration_events

    async with _temp_database() as sessions:
        world = await _seed(sessions)
        polled = _patch_providers(monkeypatch)
        with caplog.at_level(logging.WARNING, logger=access.__name__):
            async with sessions() as session:
                await poll_integration_events(session)

    everyone = world.seen | world.unseen
    assert set(polled) == everyone, {
        "missing": sorted(world.names[i] for i in everyone - set(polled)),
        "unexpected": sorted(world.names[i] for i in set(polled) - everyone),
    }
    flagged = {wf_id for wf_id in everyone if any(wf_id in line for line in _unseen_lines(caplog))}
    assert flagged == world.unseen, {
        "missing": sorted(world.names[i] for i in world.unseen - flagged),
        "unexpected": sorted(world.names[i] for i in flagged - world.unseen),
    }


@pytest.mark.asyncio
async def test_worked_on_main_the_flow_poller_delivers_whoever_sees_the_integration(
    monkeypatch, caplog
):
    from app.services import trigger_integration_access as access
    from app.services.flow_triggers import poll_flow_integration_events

    async with _temp_database() as sessions:
        world = await _seed(sessions)
        polled = _patch_providers(monkeypatch)
        with caplog.at_level(logging.WARNING, logger=access.__name__):
            async with sessions() as session:
                await poll_flow_integration_events(session)

    assert set(polled) == world.flow_seen | world.flow_unseen
    [line] = _unseen_lines(caplog)
    [hidden_target] = world.flow_unseen
    assert hidden_target.split(":")[1] in line


@pytest.mark.asyncio
async def test_the_security_audit_warns_about_exactly_the_triggers_their_user_cannot_see():
    from app.api.v1 import security_audit
    from app.services.trigger_integration_access import (
        assignment_trigger_visibility,
        flow_trigger_visibility,
    )

    async with _temp_database() as sessions:
        world = await _seed(sessions)
        async with sessions() as session:
            assignments = await assignment_trigger_visibility(session)
            flows = await flow_trigger_visibility(session)
            findings: list = []
            await security_audit._check_trigger_integration_delivery(session, findings)

    assert {str(row_id) for row_id, _name in assignments.unseen} == world.unseen
    assert assignments.checked == len((world.seen | world.unseen) - world.ownerless)
    assert {name for _id, name in flows.unseen} == {"Hidden flow"}
    assert flows.checked == 2
    assert [(f.id, f.severity) for f in findings] == [
        ("access.assignment_trigger_integration_not_visible", "warning"),
        ("access.flow_trigger_integration_not_visible", "warning"),
    ]
    for wf_id in world.unseen:
        assert f"{world.names[wf_id]} ({wf_id})" in findings[0].description
