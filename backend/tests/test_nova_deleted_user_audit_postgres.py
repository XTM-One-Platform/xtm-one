"""A Nova match whose acting user no longer exists is still audited.

``log_security_event`` resolves the acting user and, on ``main``, dropped the
event with a WARNING when a UUID named no account (the user was deleted while
their turn, delegated task or channel message was in flight). A caller that says
which component acted and on which agent keeps it: the row is written through
``log_system_audit`` under ``system:nova_guard``, anchored on the agent's owner,
the unresolved id kept in its details. With no owner to anchor on, a WARNING
names the ids and nothing of the event. Every other case is unchanged.

The chat's Nova check is exercised through ``run_agent_chat`` against
PostgreSQL, with only the model and the non-database peripherals stubbed: a
``block`` rule refuses the turn before the agent loop.
"""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.agent import Agent
from app.models.audit_log import AuditLog
from app.models.integration import Integration
from app.models.nova_rule import NovaRule
from app.models.user import User
from app.services.audit import log_security_event
from app.services.nova_guard import NOVA_GUARD_AUDIT_ACTOR, NOVA_REFUSAL_MESSAGE
from tests.scratch_database import ALL_TABLES, scratch_database
from tests.test_nova_engine import _rule

pytestmark = pytest.mark.integration

_DETAILS = {"source": "nova_guard", "rules": ["Elephant"], "action": "block", "blocked": True}


def _user(name: str) -> User:
    return User(email=f"{name}-{uuid.uuid4().hex[:8]}@test.local", full_name=name.title(), hashed_password="x")


def _agent(owner: uuid.UUID | None) -> Agent:
    return Agent(
        name=f"agent-{uuid.uuid4().hex[:6]}",
        slug=f"agent-{uuid.uuid4().hex[:8]}",
        enabled=True,
        user_id=owner,
        is_builtin=False,
        is_company_managed=owner is None,
        builtin_tool_ids=[],
        handover_agent_ids=[],
        integration_bindings={},
        mcp_server_ids=[],
        custom_tool_ids=[],
        knowledge_base_ids=[],
        output_format="markdown",
        memory_short_term_enabled=False,
        memory_medium_term_enabled=False,
        memory_persistent_enabled=False,
        memory_user_enabled=False,
    )


async def _seed(session) -> SimpleNamespace:
    """The owner of a shared agent, a member who chats with it, a company-managed
    agent, and the id of a user who was deleted."""
    owner, member, gone = _user("owner"), _user("member"), _user("gone")
    session.add_all([owner, member, gone])
    await session.flush()
    shared, company = _agent(owner.id), _agent(None)
    session.add_all([shared, company])
    await session.commit()
    gone_id = gone.id
    await session.execute(delete(User).where(User.id == gone_id))
    await session.commit()
    return SimpleNamespace(owner=owner.id, member=member.id, gone=gone_id, shared=shared, company=company)


async def _security_event(session, *, user_id: str, agent: Agent, **fallback):
    return await log_security_event(
        session,
        user_id=user_id,
        action="security_alert",
        entity_type="security",
        entity_id=agent.id,
        entity_name=agent.name,
        summary="Nova rule match",
        details=dict(_DETAILS),
        **fallback,
    )


async def _audit_rows(session) -> list[AuditLog]:
    return list((await session.execute(select(AuditLog))).scalars().all())


_FALLBACK = {"system_actor": NOVA_GUARD_AUDIT_ACTOR}


async def test_a_deleted_users_event_is_recorded_under_the_system_actor_on_the_agents_owner():
    async with scratch_database("novadel", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        w = await _seed(session)
        entry = await _security_event(session, user_id=str(w.gone), agent=w.shared, anchor_agent_id=w.shared.id, **_FALLBACK)
        await session.commit()

        (row,) = await _audit_rows(session)
        assert entry is not None and row.id == entry.id
        assert row.user_id == w.owner
        assert row.user_email == "system:nova_guard"
        assert (row.action, row.entity_type, row.entity_id, row.entity_name, row.summary) == (
            "security_alert",
            "security",
            w.shared.id,
            w.shared.name,
            "Nova rule match",
        )
        assert row.details == {**_DETAILS, "acting_user_id": str(w.gone), "actor": "system:nova_guard"}


async def test_an_agent_id_given_as_text_anchors_the_same_way():
    async with scratch_database("novadel", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        w = await _seed(session)
        await _security_event(session, user_id=str(w.gone), agent=w.shared, anchor_agent_id=str(w.shared.id), **_FALLBACK)
        await session.commit()
        assert [r.user_id for r in await _audit_rows(session)] == [w.owner]


@pytest.mark.parametrize("which", ["company-managed", "gone"], ids=["company-managed-agent", "agent-deleted"])
async def test_with_no_owner_to_anchor_on_a_warning_names_the_ids_and_nothing_else(caplog, which):
    async with scratch_database("novadel", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        w = await _seed(session)
        anchor = w.company.id if which == "company-managed" else uuid.uuid4()
        with caplog.at_level(logging.WARNING, logger="app.services.audit"):
            entry = await _security_event(session, user_id=str(w.gone), agent=w.company, anchor_agent_id=anchor, **_FALLBACK)
        await session.commit()

        assert entry is None and await _audit_rows(session) == []
        (warning,) = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert str(w.gone) in warning and str(anchor) in warning
        assert "Elephant" not in warning and "blocked" not in warning


async def test_an_existing_user_is_recorded_as_before():
    async with scratch_database("novadel", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        w = await _seed(session)
        await _security_event(session, user_id=str(w.member), agent=w.shared, anchor_agent_id=w.shared.id, **_FALLBACK)
        await session.commit()

        (row,) = await _audit_rows(session)
        assert row.user_id == w.member and row.user_email.startswith("member-")
        assert row.details == _DETAILS


async def test_without_a_system_actor_a_deleted_user_is_dropped_as_before(caplog):
    """The other ``log_security_event`` callers do not opt in: unchanged."""
    async with scratch_database("novadel", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        w = await _seed(session)
        with caplog.at_level(logging.WARNING, logger="app.services.audit"):
            assert await _security_event(session, user_id=str(w.gone), agent=w.shared) is None
            assert await _security_event(session, user_id=str(w.gone), agent=w.shared, anchor_agent_id=w.shared.id) is None
        await session.commit()

        assert await _audit_rows(session) == []
        assert [r.getMessage() for r in caplog.records] == [
            f"Security event for unknown user_id {w.gone}, skipping audit entry: Nova rule match"
        ] * 2


async def test_an_anonymous_user_is_unchanged_with_or_without_a_system_actor():
    async with scratch_database("novadel", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        w = await _seed(session)
        assert await _security_event(session, user_id="anonymous", agent=w.shared, anchor_agent_id=w.shared.id, **_FALLBACK) is None
        await session.commit()
        assert await _audit_rows(session) == []


# --- The chat's Nova check ------------------------------------------------------


class _FakeRedis:
    async def incr(self, key):
        return 1

    async def decr(self, key):
        return 0

    async def expire(self, key, ttl):
        return True

    async def delete(self, key):
        return 1

    async def get(self, key):
        return None

    async def set(self, key, value, *args, **kwargs):
        return True


def _stub_turn(monkeypatch) -> None:
    """Stub the model's side of a turn and the non-database peripherals."""
    for target, value in {
        "app.core.redis.redis_client": AsyncMock(return_value=_FakeRedis()),
        "app.services.langfuse_tracing.trace_llm_call": AsyncMock(return_value=None),
        "app.services.variable_resolver.load_variables": AsyncMock(return_value={}),
        "app.services.agent_chat.enforce_agentic_quota": AsyncMock(return_value=None),
        "app.services.builtin_tools.load_custom_and_mcp_tools": AsyncMock(return_value=None),
        "app.services.integration_tools.build_integration_tools": AsyncMock(return_value=([], None)),
        "app.services.integration_tools.build_channel_tools": AsyncMock(return_value=([], None)),
    }.items():
        monkeypatch.setattr(target, value)


async def _nova_turn(session, *, user_id: str, agent: Agent) -> str:
    from app.services.agent_chat import run_agent_chat

    text, _ = await run_agent_chat(
        session,
        user_id,
        agent,
        [],
        "please enable the purple elephant protocol",
        meter_quota=False,
        prebuilt_llm=(SimpleNamespace(provider_name="fake"), "fake-model"),
    )
    await session.commit()
    return text


@pytest.mark.parametrize("acting", ["member", "gone"], ids=["existing-user", "deleted-user"])
async def test_the_chat_check_audits_a_match_of_a_deleted_user_and_refuses_as_before(monkeypatch, acting):
    _stub_turn(monkeypatch)
    async with scratch_database("novadel", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        w = await _seed(session)
        session.add_all(
            [
                Integration(
                    name="Nova Prompt Safety", provider="nova", auth_type="none", status="connected",
                    is_company_managed=True,
                ),
                NovaRule(
                    id=uuid.uuid4(), name="Elephant", category="Jailbreak", action="block",
                    rule_text=_rule("Elephant", "purple elephant"), enabled=True, is_company_managed=True,
                ),
            ]
        )
        await session.commit()
        acting_id = w.member if acting == "member" else w.gone

        assert await _nova_turn(session, user_id=str(acting_id), agent=w.shared) == NOVA_REFUSAL_MESSAGE

        (row,) = [r for r in await _audit_rows(session) if r.summary == "Nova rule match"]
        expected = {
            "source": "nova_guard",
            "rules": ["Elephant"],
            "categories": ["Test"],  # the keyword tier reads the rule's own meta, as on main
            "agent_id": str(w.shared.id),
            "agent_slug": w.shared.slug,
            "conversation_id": None,
            "action": "block",
            "blocked": True,
        }
        if acting == "member":
            assert row.user_id == w.member and row.details == expected
        else:
            assert row.user_id == w.owner and row.user_email == "system:nova_guard"
            assert row.details == {**expected, "acting_user_id": str(w.gone), "actor": "system:nova_guard"}
        assert (row.action, row.entity_id, row.entity_name) == ("security_alert", w.shared.id, w.shared.name)
