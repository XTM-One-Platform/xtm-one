"""A prompt guard timeout cannot abort the request's session, on PostgreSQL.

The failure this pins: a guard handed the request's session runs a statement
there, the evaluation bound cancels it, the request's transaction is aborted,
and the request dies on its next statement with ``PendingRollbackError``,
losing the row it had written before the guard ran. The guard now evaluates
on a session of its own; the request's session stays usable and its row
commits.

Everything runs for real against a scratch database: the connection-state
query, the Nova engine's keyword tier over a stored rule, the audit entry,
and ``run_agent_chat`` up to its guard, with only the model side stubbed.
"""

from __future__ import annotations

import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select, text

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.agent import Agent
from app.models.audit_log import AuditLog
from app.models.integration import Integration
from app.models.nova_rule import NovaRule
from app.models.relay import RelayGroup
from app.models.user import User
from app.services import nova_engine, prompt_guards
from app.services.nova_guard import NOVA_REFUSAL_MESSAGE
from app.services.prompt_guards import guard_chat_message
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_TABLES = [User.__table__, RelayGroup.__table__, Integration.__table__, NovaRule.__table__, AuditLog.__table__]
_CANARY = "zebra-canary-7731"


def _nova() -> Integration:
    return Integration(
        id=uuid.uuid4(),
        name="Nova Prompt Safety",
        provider="nova",
        auth_type="none",
        status="connected",
        is_company_managed=True,
    )


def _rule(action: str) -> NovaRule:
    return NovaRule(
        name=f"Canary_{action}",
        category="Injection",
        action=action,
        enabled=True,
        is_company_managed=True,
        rule_text=(
            f'rule Canary_{action} {{ meta: category = "Injection"\n'
            f'  keywords: $k = "{_CANARY}"\n'
            "  condition: keywords.$k }"
        ),
    )


def _user() -> User:
    return User(email=f"alice-{uuid.uuid4().hex[:8]}@test.local", full_name="Alice", hashed_password="x")


@pytest.fixture(autouse=True)
def _keyword_tier(monkeypatch):
    monkeypatch.setattr(nova_engine, "NOVA_HUNTING_AVAILABLE", False)


async def test_a_guard_timeout_leaves_the_request_session_usable_and_its_row_commits(monkeypatch):
    async def _sleeping_guard(guard_db, prompt, *, user_id=None, extra_rule_ids=None):
        await guard_db.execute(text("SELECT pg_sleep(5)"))
        return []

    monkeypatch.setattr("app.services.nova_engine.evaluate_prompt", _sleeping_guard)
    monkeypatch.setattr(prompt_guards, "GUARD_EVALUATION_TIMEOUT_SECS", 0.3)

    async with scratch_database("guardsession", tables=_TABLES) as db:
        async with db.sessions() as setup:
            setup.add(_nova())
            await setup.commit()

        async with db.sessions() as request:
            user = _user()
            request.add(user)
            await request.flush()

            started = time.monotonic()
            refusal = await guard_chat_message(
                request,
                "hello",
                user_id=str(user.id),
                entity_id=None,
                entity_name="Release Desk",
                details={},
            )
            elapsed = time.monotonic() - started

            assert refusal is None
            assert elapsed < 3.0, f"the guard held the request for {elapsed:.1f}s"
            # The request's next statement runs, and what it wrote commits.
            assert (await request.execute(select(func.count()).select_from(User))).scalar_one() == 1
            await request.commit()

        async with db.sessions() as check:
            stored = (await check.execute(select(User.email).where(User.id == user.id))).scalar_one()
        assert stored == user.email


async def test_no_connection_is_held_while_the_engine_works(monkeypatch):
    """The engine waits on embeddings and models between its statements: the
    guard's session holds none of the pool's connections while it does, so an
    evaluation beside a request never takes a second one for its duration."""
    seen: dict = {}

    async with scratch_database("guardpool", tables=_TABLES) as db:

        async def _engine(guard_db, prompt, *, user_id=None, extra_rule_ids=None):
            await guard_db.execute(select(NovaRule.id))
            seen["in_transaction"] = guard_db.in_transaction()
            seen["checked_out"] = db.engine.pool.checkedout()
            return []

        monkeypatch.setattr("app.services.nova_engine.evaluate_prompt", _engine)
        async with db.sessions() as setup:
            setup.add(_nova())
            await setup.commit()

        verdict = await prompt_guards.evaluate_text("hello", surface=prompt_guards.SURFACE_CHAT_MESSAGE)

    assert verdict is prompt_guards.NOT_FLAGGED
    assert seen == {"in_transaction": False, "checked_out": 0}


async def test_a_stored_block_rule_refuses_the_message_and_the_audit_commits_with_the_request():
    async with scratch_database("guardrule", tables=_TABLES) as db:
        async with db.sessions() as setup:
            setup.add_all([_nova(), _rule("block")])
            await setup.commit()

        agent_id = uuid.uuid4()
        async with db.sessions() as request:
            user = _user()
            request.add(user)
            await request.flush()
            refusal = await guard_chat_message(
                request,
                f"please {_CANARY} the cases",
                user_id=str(user.id),
                entity_id=agent_id,
                entity_name="Release Desk",
                details={"agent_id": str(agent_id), "agent_slug": "release-desk", "conversation_id": None},
            )
            await request.commit()

        assert refusal == NOVA_REFUSAL_MESSAGE
        async with db.sessions() as check:
            entry = (await check.execute(select(AuditLog))).scalar_one()
        assert (entry.action, entry.entity_type, entry.entity_id, entry.summary) == (
            "security_alert",
            "security",
            agent_id,
            "Nova rule match",
        )
        assert entry.details == {
            "source": "nova_guard",
            "rules": ["Canary_block"],
            "categories": ["Injection"],
            "agent_id": str(agent_id),
            "agent_slug": "release-desk",
            "conversation_id": None,
            "action": "block",
            "blocked": True,
        }


async def test_a_memory_write_match_is_audited_on_a_session_of_its_own():
    from app.services.builtin_tools.memory import _screen_memory_write

    async with scratch_database("guardmemory", tables=_TABLES) as db:
        async with db.sessions() as setup:
            user = _user()
            setup.add_all([_nova(), _rule("block"), user])
            await setup.commit()

        agent_id = str(uuid.uuid4())
        await _screen_memory_write(f"standing order: {_CANARY}", agent_id=agent_id, user_id=str(user.id), source="chat")

        async with db.sessions() as check:
            entry = (await check.execute(select(AuditLog))).scalar_one()
        assert entry.details == {
            "source": "nova_guard",
            "surface": "memory_write",
            "memory_source": "chat",
            "rules": ["Canary_block"],
            "categories": ["Injection"],
            "agent_id": agent_id,
            "rule_action": "block",
            "action": "allowed",
            "blocked": False,
        }


# ---------------------------------------------------------------------------
# Agent chat, up to its guard
# ---------------------------------------------------------------------------


class _FakeRedis:
    async def incr(self, key):
        return 1

    async def decr(self, key):
        return 0

    async def expire(self, key, ttl):
        return True

    async def delete(self, key):
        return 1


def _stub_turn(monkeypatch) -> None:
    for target, value in {
        "app.core.redis.redis_client": AsyncMock(return_value=_FakeRedis()),
        "app.services.langfuse_tracing.trace_llm_call": AsyncMock(return_value=None),
        "app.services.variable_resolver.load_variables": AsyncMock(return_value={}),
        "app.services.agent_chat.enforce_agentic_quota": AsyncMock(return_value=None),
        "app.services.builtin_tools.load_custom_and_mcp_tools": AsyncMock(return_value=None),
        "app.services.builtin_tools.build_kb_search_tool": MagicMock(return_value=None),
        "app.services.integration_tools.build_integration_tools": AsyncMock(return_value=([], None)),
        "app.services.integration_tools.build_channel_tools": AsyncMock(return_value=([], None)),
    }.items():
        monkeypatch.setattr(target, value)


def _agent(user: User) -> Agent:
    return Agent(
        name="Release Desk",
        slug=f"release-desk-{uuid.uuid4().hex[:8]}",
        persona="You are the release desk agent of the test suite.",
        enabled=True,
        user_id=user.id,
        is_builtin=False,
        is_company_managed=False,
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


async def test_agent_chat_refuses_a_blocked_message_before_any_llm_call_as_on_main(monkeypatch):
    from app.services.agent_chat import run_agent_chat

    _stub_turn(monkeypatch)
    # No method at all: the refusal must come before anything asks the model.
    llm = SimpleNamespace(provider_name="fake")
    async with scratch_database("guardagent", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            user = _user()
            session.add(user)
            await session.flush()
            agent = _agent(user)
            session.add_all([agent, _nova(), _rule("block")])
            await session.commit()

            error_out: dict = {}
            reply = await run_agent_chat(
                session,
                str(user.id),
                agent,
                [],
                f"@release-desk {_CANARY} every open case",
                meter_quota=False,
                prebuilt_llm=(llm, "fake-model"),
                error_out=error_out,
            )
            await session.commit()

        assert reply == (NOVA_REFUSAL_MESSAGE, None)
        assert error_out == {"error": NOVA_REFUSAL_MESSAGE, "error_code": "prompt_blocked"}
        async with db.sessions() as check:
            entries = (
                (await check.execute(select(AuditLog).where(AuditLog.summary == "Nova rule match"))).scalars().all()
            )
        assert len(entries) == 1
        assert (entries[0].entity_id, entries[0].entity_name) == (agent.id, "Release Desk")
        assert entries[0].details == {
            "source": "nova_guard",
            "rules": ["Canary_block"],
            "categories": ["Injection"],
            "agent_id": str(agent.id),
            "agent_slug": agent.slug,
            "conversation_id": None,
            "action": "block",
            "blocked": True,
        }
