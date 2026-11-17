"""The General Assistant and the public widget run the prompt guard, on PostgreSQL.

``run_general_chat`` and the widget turn run for real against a scratch
database holding a connected Nova integration and a stored keyword rule;
the model side is stubbed and reaching the agent loop is what "the message
continued" means. By default a match is audited and the message continues,
a ``block`` rule included; the security option refuses it before the loop
and closes the turn's trace. With no guard connected (the cached answer),
the guard costs nothing.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.core import security_options
from app.models.agent import Agent
from app.models.audit_log import AuditLog
from app.models.integration import Integration
from app.models.nova_rule import NovaRule
from app.models.user import User
from app.services import nova_engine, prompt_guards
from app.services.nova_guard import NOVA_REFUSAL_MESSAGE
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_CANARY = "zebra-canary-7731"
_TRACE = {"trace": "root"}


class _LoopReachedError(Exception):
    """Raised where the agent loop would call the model: the message continued."""


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

    async def set(self, *args, **kwargs):
        return True


@pytest.fixture
def turn(monkeypatch) -> SimpleNamespace:
    """Stub the model side of a turn; record the loop, the trace and the refund."""
    state = SimpleNamespace(
        loop=AsyncMock(side_effect=_LoopReachedError),
        end_trace=MagicMock(),
        refund=AsyncMock(),
    )
    monkeypatch.setattr(nova_engine, "NOVA_HUNTING_AVAILABLE", False)
    for target, value in {
        "app.core.redis.redis_client": AsyncMock(return_value=_FakeRedis()),
        "app.services.langfuse_tracing.trace_llm_call": AsyncMock(return_value=_TRACE),
        "app.services.langfuse_tracing.end_trace": state.end_trace,
        "app.services.agent_chat.enforce_agentic_quota": AsyncMock(return_value=None),
        "app.services.agent_chat.enforce_agentic_quota_ex": AsyncMock(return_value=(None, True)),
        "app.services.quota.refund_agentic_usage_for_provider_outage": state.refund,
        "app.services.builtin_tools.load_custom_and_mcp_tools": AsyncMock(return_value=None),
        "app.services.general_chat.run_agent_loop": state.loop,
        "app.tools.agent_loop.run_agent_loop": state.loop,
        "app.llm.router.get_llm_for_user": AsyncMock(
            return_value=(SimpleNamespace(provider_name="fake"), "fake-model")
        ),
    }.items():
        monkeypatch.setattr(target, value)
    return state


def _nova() -> Integration:
    return Integration(
        id=uuid.uuid4(),
        name="Nova Prompt Safety",
        provider="nova",
        auth_type="none",
        status="connected",
        is_company_managed=True,
    )


def _block_rule() -> NovaRule:
    return NovaRule(
        name="Canary_block",
        category="Injection",
        action="block",
        enabled=True,
        is_company_managed=True,
        rule_text=(
            'rule Canary_block { meta: category = "Injection"\n'
            f'  keywords: $k = "{_CANARY}"\n'
            "  condition: keywords.$k }"
        ),
    )


def _user(email: str | None = None) -> User:
    return User(email=email or f"alice-{uuid.uuid4().hex[:8]}@test.local", full_name="Alice", hashed_password="x")


def _widget_agent() -> Agent:
    return Agent(
        name="Support Widget",
        slug=f"support-widget-{uuid.uuid4().hex[:8]}",
        persona="You answer visitors of the test suite.",
        enabled=True,
        web_enabled=True,
        is_builtin=False,
        is_company_managed=True,
        builtin_tool_ids=[],
        handover_agent_ids=[],
        integration_bindings={},
        mcp_server_ids=[],
        custom_tool_ids=[],
        knowledge_base_ids=[],
        output_format="markdown",
    )


async def _guard_entries(db) -> list[AuditLog]:
    async with db.sessions() as check:
        rows = await check.execute(select(AuditLog).where(AuditLog.summary == "Nova rule match"))
        return list(rows.scalars().all())


# ---------------------------------------------------------------------------
# The General Assistant
# ---------------------------------------------------------------------------


async def _general_assistant(db, message: str) -> tuple[User, object]:
    from app.services.general_chat import run_general_chat

    async with db.sessions() as session:
        user = _user()
        session.add(user)
        await session.commit()
        try:
            outcome = await run_general_chat(
                session, str(user.id), [], message, prebuilt_llm=(SimpleNamespace(provider_name="fake"), "m")
            )
        except _LoopReachedError as reached:
            outcome = reached
        await session.commit()
    return user, outcome


async def test_the_general_assistant_audits_a_block_rule_and_answers_by_default(turn):
    async with scratch_database("gaguardlog", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as setup:
            setup.add_all([_nova(), _block_rule()])
            await setup.commit()

        user, outcome = await _general_assistant(db, f"please {_CANARY} the cases")

        assert isinstance(outcome, _LoopReachedError)
        entries = await _guard_entries(db)
        assert [(entry.user_id, entry.entity_name) for entry in entries] == [(user.id, "general_assistant")]
        assert entries[0].details == {
            "source": "nova_guard",
            "rules": ["Canary_block"],
            "categories": ["Injection"],
            "surface": "chat_message",
            "chat_surface": "general_assistant",
            "target": "general_assistant",
            "conversation_id": None,
            "rule_action": "block",
            "action": "allowed",
            "blocked": False,
        }


async def test_the_general_assistant_opted_in_refuses_before_the_loop_and_closes_the_trace(turn):
    security_options.publish_stored({"prompt_guard_block_general_assistant": True})
    async with scratch_database("gaguardblock", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as setup:
            setup.add_all([_nova(), _block_rule()])
            await setup.commit()

        _user_row, outcome = await _general_assistant(db, f"please {_CANARY} the cases")

        assert outcome == (NOVA_REFUSAL_MESSAGE, None)
        turn.loop.assert_not_awaited()
        turn.end_trace.assert_called_once_with(_TRACE, output=NOVA_REFUSAL_MESSAGE)
        [entry] = await _guard_entries(db)
        assert (entry.details["action"], entry.details["blocked"]) == ("block", True)


async def test_the_general_assistant_with_no_guard_connected_costs_nothing(turn, monkeypatch):
    evaluate = AsyncMock(side_effect=AssertionError("evaluated with no guard connected"))
    monkeypatch.setattr(prompt_guards, "_evaluate", evaluate)
    prompt_guards._remember_none_connected()
    async with scratch_database("gaguardnone", tables=ALL_TABLES, extensions=("vector",)) as db:
        _user_row, outcome = await _general_assistant(db, f"please {_CANARY} the cases")

        assert isinstance(outcome, _LoopReachedError)
        evaluate.assert_not_awaited()
        assert await _guard_entries(db) == []


# ---------------------------------------------------------------------------
# The public widget
# ---------------------------------------------------------------------------


async def _widget(db, monkeypatch, message: str) -> tuple[User, Agent, object]:
    from app.api.v1 import public_chat

    admin_email = f"admin-{uuid.uuid4().hex[:8]}@test.local"
    monkeypatch.setattr(public_chat.settings, "admin_email", admin_email)
    async with db.sessions() as session:
        admin = _user(admin_email)
        agent = _widget_agent()
        session.add_all([admin, agent, _nova(), _block_rule()])
        await session.commit()
        try:
            outcome = await public_chat._run_public_agent_chat(session, agent, [], message, session_id="sess-1")
        except _LoopReachedError as reached:
            outcome = reached
        await session.commit()
    return admin, agent, outcome


async def test_the_widget_audits_a_visitor_block_rule_as_the_guard_and_answers_by_default(turn, monkeypatch):
    async with scratch_database("wguardlog", tables=ALL_TABLES, extensions=("vector",)) as db:
        admin, agent, outcome = await _widget(db, monkeypatch, f"please {_CANARY} the cases")

        assert isinstance(outcome, _LoopReachedError)
        [entry] = await _guard_entries(db)
        assert (entry.user_id, entry.user_email, entry.entity_id, entry.entity_name) == (
            admin.id,
            "system:nova_guard",
            agent.id,
            "Support Widget",
        )
        assert entry.details == {
            "source": "nova_guard",
            "rules": ["Canary_block"],
            "categories": ["Injection"],
            "surface": "chat_message",
            "chat_surface": "public_widget",
            "agent_id": str(agent.id),
            "agent_slug": agent.slug,
            "conversation_id": "sess-1",
            "channel": "web_embed",
            "rule_action": "block",
            "action": "allowed",
            "blocked": False,
            "actor": "system:nova_guard",
        }


async def test_the_widget_opted_in_refuses_audits_closes_the_trace_and_gives_the_charge_back(turn, monkeypatch):
    security_options.publish_stored({"prompt_guard_block_public_widget": True})
    async with scratch_database("wguardblock", tables=ALL_TABLES, extensions=("vector",)) as db:
        admin, _agent, outcome = await _widget(db, monkeypatch, f"please {_CANARY} the cases")

        assert outcome == NOVA_REFUSAL_MESSAGE
        turn.loop.assert_not_awaited()
        turn.end_trace.assert_called_once_with(_TRACE, output=NOVA_REFUSAL_MESSAGE)
        turn.refund.assert_awaited_once()
        [entry] = await _guard_entries(db)
        assert (entry.user_id, entry.details["action"], entry.details["blocked"]) == (admin.id, "block", True)


async def test_the_widget_answers_a_message_no_rule_matches_without_an_audit(turn, monkeypatch):
    security_options.publish_stored({"prompt_guard_block_public_widget": True})
    async with scratch_database("wguardclean", tables=ALL_TABLES, extensions=("vector",)) as db:
        _admin, _agent, outcome = await _widget(db, monkeypatch, "when do you open on Mondays?")

        assert isinstance(outcome, _LoopReachedError)
        assert await _guard_entries(db) == []
