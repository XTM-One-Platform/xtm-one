"""``act_on_page`` reaches the agent loop only when it can work, against real PostgreSQL.

Offered on a turn whose widget declared it answers page actions while an
administrator has set a decision model - never otherwise (FR-003, FR-005).

``integration``-marked and skips when PostgreSQL is unreachable.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.llm.base import ChatResponse
from app.models.chat import Conversation
from app.models.llm_config import LLMProviderConfig
from app.models.platform_settings import PlatformSettings
from app.models.user import User
from app.services.builtin_tools.page_actions import ACT_ON_PAGE_TOOL
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


async def test_act_on_page_offered_only_when_declared_and_configured(monkeypatch):
    from app.services import nova_engine
    from app.services.agent_chat import run_agent_chat
    from tests.test_prompt_guard_chat_surfaces_postgres import _FakeRedis
    from tests.test_prompt_guard_session_postgres import _agent, _stub_turn

    loop = AsyncMock(return_value=ChatResponse(content="done", model="fake-model"))
    llm = (SimpleNamespace(provider_name="fake"), "fake-model")
    monkeypatch.setattr(nova_engine, "NOVA_HUNTING_AVAILABLE", False)
    _stub_turn(monkeypatch)
    for target, value in {
        "app.core.redis.redis_client": AsyncMock(return_value=_FakeRedis()),
        "app.services.agent_chat.run_agent_loop": loop,
        "app.services.agent_chat.enforce_agentic_quota_ex": AsyncMock(return_value=(None, True)),
    }.items():
        monkeypatch.setattr(target, value)

    async with scratch_database(
        "actonpage", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db, db.sessions() as session:
        user = User(id=uuid.uuid4(), email=f"a-{uuid.uuid4().hex[:6]}@example.com", full_name="A", hashed_password="x")
        session.add(user)
        await session.flush()
        conv = Conversation(id=uuid.uuid4(), user_id=user.id, title="t", channel="api")
        settings = PlatformSettings(id=uuid.uuid4())
        agent = _agent(user)
        session.add_all([conv, settings, agent])
        await session.commit()

        async def offered(**kw) -> bool:
            await run_agent_chat(
                session, str(user.id), agent, [], "open the reports",
                conversation_id=str(conv.id), prebuilt_llm=llm, **kw,
            )
            return ACT_ON_PAGE_TOOL in {t.name for t in loop.await_args.kwargs["tools"] or []}

        # Declared, but no decision model: nothing could decide.
        assert not await offered(page_actions=True)

        settings.decision_model = "clef-flash"
        await session.commit()
        assert await offered(page_actions=True)
        # A client that did not declare it would never answer a frame.
        assert not await offered()

        # A pinned decision provider the user may not use would refuse
        # every step: the tool is not offered at all.
        restricted = LLMProviderConfig(
            id=uuid.uuid4(), provider="ollama", api_key_enc="x", access_restricted=True, shared_user_ids=[]
        )
        session.add(restricted)
        settings.decision_provider_id = restricted.id
        await session.commit()
        assert not await offered(page_actions=True)

        restricted.shared_user_ids = [str(user.id)]
        await session.commit()
        assert await offered(page_actions=True)
