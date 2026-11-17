"""An agent chat or General Assistant turn with integration tools carries the untrusted-content policy.

Integration and bundle tools answer in ``<<UNTRUSTED-...>>`` blocks, so a turn
that carries them gets the policy that reads those markers, with or without
``web_fetch``. ``run_agent_chat`` and ``run_general_chat`` run against
PostgreSQL with the model side and the integration tool builder stubbed; the
system prompt is read where the turn measures it (agent chat), where it stops
(agent chat, after the late missing-integration warning) or where the agent
loop receives it (General Assistant).
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.agent import Agent
from app.models.integration import Integration
from app.models.user import User
from app.services.content_safety import UNTRUSTED_CONTENT_POLICY
from app.services.prompt_guard import build_security_policy
from tests.scratch_database import ALL_TABLES, scratch_database
from tests.test_memory_quarantine_chat_postgres import (
    _LLM,
    _PERSONA,
    _StopTurnError,
    _stub_turn,
    _system_prompt,
)

pytestmark = pytest.mark.integration

_BUNDLE_ID = "00000000-0000-4000-8000-0000000b0d1e"
_ANCHOR = build_security_policy(include_identity=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_fields", "policy"),
    [
        ({"integration_bindings": {"bundle_acme": [_BUNDLE_ID]}}, True),
        ({"integration_bindings": {}, "builtin_tool_ids": ["search_knowledge"]}, False),
    ],
    ids=["bundle-tool-no-web-fetch", "no-integration"],
)
async def test_an_agent_chat_turn_with_a_bundle_tool_gets_the_policy(monkeypatch, agent_fields, policy):
    prompt = await _system_prompt(monkeypatch, memory=False, agent_fields=agent_fields)

    if policy:
        assert prompt.count(UNTRUSTED_CONTENT_POLICY) == 1
        assert prompt.endswith(UNTRUSTED_CONTENT_POLICY + "\n" + _ANCHOR)
    else:
        assert UNTRUSTED_CONTENT_POLICY not in prompt
        assert prompt.endswith("\n" + _ANCHOR)


def _final_prompt(excinfo) -> str:
    """``system_prompt`` of ``run_agent_chat`` where the turn stopped."""
    tb = excinfo.tb
    while tb is not None:
        if tb.tb_frame.f_code.co_name == "run_agent_chat":
            return tb.tb_frame.f_locals["system_prompt"]
        tb = tb.tb_next
    raise AssertionError("run_agent_chat is not on the stopped turn's stack")


@pytest.mark.asyncio
async def test_a_template_binding_left_unconnected_keeps_the_policy_next_to_the_anchor(monkeypatch):
    """A template binding brings the policy; the late "NOT connected" warning
    goes before the policy, which stays immediately before the anchor."""
    from app.services.agent_chat import run_agent_chat

    async with (
        scratch_database("untrustedpolicy", tables=ALL_TABLES, extensions=("vector",)) as db,
        db.sessions() as session,
    ):
        user = User(email=f"alice-{uuid.uuid4().hex[:8]}@test.local", full_name="Alice", hashed_password="x")
        session.add(user)
        await session.flush()
        agent = Agent(
            name="Hunt Desk",
            slug=f"hunt-desk-{uuid.uuid4().hex[:8]}",
            persona=_PERSONA,
            enabled=True,
            user_id=user.id,
            is_builtin=False,
            is_company_managed=False,
            builtin_tool_ids=[],
            handover_agent_ids=[],
            integration_bindings={},
            integration_template_bindings=["opencti"],
            mcp_server_ids=[],
            custom_tool_ids=[],
            knowledge_base_ids=[],
            output_format="markdown",
            memory_short_term_enabled=False,
            memory_medium_term_enabled=False,
            memory_persistent_enabled=False,
            memory_user_enabled=False,
        )
        session.add(agent)
        await session.commit()
        _stub_turn(monkeypatch)
        with pytest.raises(_StopTurnError) as excinfo:
            await run_agent_chat(
                session, str(user.id), agent, [], "Any new campaign?", meter_quota=False, prebuilt_llm=_LLM
            )

    prompt = _final_prompt(excinfo)
    assert prompt.count(UNTRUSTED_CONTENT_POLICY) == 1
    assert prompt.endswith(UNTRUSTED_CONTENT_POLICY + "\n" + _ANCHOR)
    assert "required integrations are NOT connected" in prompt
    assert prompt.index("required integrations are NOT connected") < prompt.index(UNTRUSTED_CONTENT_POLICY)


class _LoopReachedError(Exception):
    """Raised where the agent loop would call the model."""


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


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [True, False], ids=["bundle-tool-no-web-fetch", "no-integration"])
async def test_a_general_assistant_turn_with_a_bundle_tool_gets_the_policy(monkeypatch, bound):
    from app.services.general_chat import run_general_chat

    loop = AsyncMock(side_effect=_LoopReachedError)
    integrations = AsyncMock(return_value=([], None))
    for target, value in {
        "app.core.redis.redis_client": AsyncMock(return_value=_FakeRedis()),
        "app.services.langfuse_tracing.trace_llm_call": AsyncMock(return_value=None),
        "app.services.agent_chat.enforce_agentic_quota": AsyncMock(return_value=None),
        "app.services.agent_chat.enforce_agentic_quota_ex": AsyncMock(return_value=(None, True)),
        "app.services.builtin_tools.load_custom_and_mcp_tools": AsyncMock(return_value=None),
        "app.services.integration_tools.build_integration_tools": integrations,
        "app.services.general_chat.run_agent_loop": loop,
        "app.tools.agent_loop.run_agent_loop": loop,
    }.items():
        monkeypatch.setattr(target, value)

    async with (
        scratch_database("untrustedpolicyga", tables=ALL_TABLES, extensions=("vector",)) as db,
        db.sessions() as session,
    ):
        user = User(email=f"alice-{uuid.uuid4().hex[:8]}@test.local", full_name="Alice", hashed_password="x")
        session.add(user)
        await session.flush()
        bundle = Integration(
            provider="bundle_acme",
            name="Acme",
            auth_type="api_key",
            status="connected",
            user_id=user.id,
            is_company_managed=False,
        )
        session.add(bundle)
        await session.flush()
        user.ga_builtin_tool_ids = []
        user.ga_integration_bindings = {"bundle_acme": [str(bundle.id)]} if bound else {}
        await session.commit()
        with pytest.raises(_LoopReachedError):
            await run_general_chat(session, str(user.id), [], "What changed?", prebuilt_llm=_LLM)

    assert integrations.await_count == (1 if bound else 0)
    system = loop.await_args.kwargs["messages"][0]
    assert system.role == "system"
    anchor = build_security_policy(include_identity=True)
    if bound:
        assert system.content.count(UNTRUSTED_CONTENT_POLICY) == 1
        assert system.content.endswith(UNTRUSTED_CONTENT_POLICY + "\n" + anchor)
    else:
        assert UNTRUSTED_CONTENT_POLICY not in system.content
        assert system.content.endswith("\n" + anchor)
