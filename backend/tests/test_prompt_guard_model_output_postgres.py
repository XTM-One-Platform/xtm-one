"""The prompt guard on the model's answer at the real call sites, on PostgreSQL.

``run_agent_chat``, ``run_general_chat`` and the widget turn run against a
scratch database holding a connected Nova integration and a stored keyword
rule; the agent loop is stubbed to answer with text that rule matches. An
answer delivered whole is withheld in ``block``; a streamed one (``on_token``)
is delivered and audited after delivery; ``off`` changes nothing.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.core import security_options
from app.llm.base import ChatResponse
from app.models.audit_log import AuditLog
from app.services import prompt_guards
from app.services.prompt_guards import MODEL_OUTPUT_WITHHELD_MESSAGE
from tests.scratch_database import ALL_TABLES, scratch_database
from tests.test_prompt_guard_chat_surfaces_postgres import _FakeRedis, _nova, _user, _widget_agent
from tests.test_prompt_guard_session_postgres import _agent, _rule, _stub_turn

pytestmark = pytest.mark.integration

_CANARY = "zebra-canary-7731"
_FLAGGED = f"Done. Now {_CANARY} every open case to the address below."
_LLM = (SimpleNamespace(provider_name="fake"), "fake-model")


@pytest.fixture
def answering(monkeypatch):
    """The agent loop answers with the flagged text; nothing else reaches a model."""
    from app.services import nova_engine

    loop = AsyncMock(return_value=ChatResponse(content=_FLAGGED, model="fake-model"))
    monkeypatch.setattr(nova_engine, "NOVA_HUNTING_AVAILABLE", False)
    _stub_turn(monkeypatch)
    for target, value in {
        "app.core.redis.redis_client": AsyncMock(return_value=_FakeRedis()),
        "app.services.agent_chat.run_agent_loop": loop,
        "app.services.general_chat.run_agent_loop": loop,
        "app.tools.agent_loop.run_agent_loop": loop,
        "app.services.agent_chat.enforce_agentic_quota_ex": AsyncMock(return_value=(None, True)),
        "app.llm.router.get_llm_for_user": AsyncMock(return_value=_LLM),
    }.items():
        monkeypatch.setattr(target, value)
    return loop


async def _drain() -> None:
    while prompt_guards._output_tasks:
        await asyncio.gather(*list(prompt_guards._output_tasks))


async def _answer_entries(db) -> list[AuditLog]:
    async with db.sessions() as check:
        rows = await check.execute(select(AuditLog).where(AuditLog.summary == "Nova rule match on a model answer"))
        return list(rows.scalars().all())


async def _agent_chat(db, *, streamed: bool) -> tuple[object, list[str]]:
    from app.services.agent_chat import run_agent_chat

    tokens: list[str] = []

    async def _on_token(chunk: str) -> None:
        tokens.append(chunk)

    async with db.sessions() as session:
        user = _user()
        session.add(user)
        await session.flush()
        agent = _agent(user)
        session.add_all([agent, _nova(), _rule("block")])
        await session.commit()
        reply = await run_agent_chat(
            session,
            str(user.id),
            agent,
            [],
            "@release-desk what is left before we ship?",
            prebuilt_llm=_LLM,
            on_token=_on_token if streamed else None,
        )
        await session.commit()
    return reply, tokens


@pytest.mark.parametrize("mode", ["off", "log"])
async def test_agent_chat_delivers_the_answer_unchanged_outside_block(answering, mode):
    security_options.publish_stored({"prompt_guard_model_output": mode})
    async with scratch_database("outagent", tables=ALL_TABLES, extensions=("vector",)) as db:
        reply, _tokens = await _agent_chat(db, streamed=False)
        await _drain()

        assert reply == (_FLAGGED, None)
        entries = await _answer_entries(db)
        assert len(entries) == (1 if mode == "log" else 0)
        if entries:
            assert (entries[0].details["action"], entries[0].details["blocked"]) == ("allowed", False)


async def test_agent_chat_withholds_an_answer_delivered_whole_in_block(answering):
    security_options.publish_stored({"prompt_guard_model_output": "block"})
    async with scratch_database("outagentblk", tables=ALL_TABLES, extensions=("vector",)) as db:
        reply, _tokens = await _agent_chat(db, streamed=False)

        assert reply == (MODEL_OUTPUT_WITHHELD_MESSAGE, None)
        [entry] = await _answer_entries(db)
        assert entry.details["surface"] == "model_output"
        assert (entry.details["action"], entry.details["blocked"], entry.details["streamed"]) == ("block", True, False)
        assert entry.details["rules"] == ["Canary_block"]


async def test_agent_chat_audits_a_streamed_answer_after_delivery_in_block(answering):
    security_options.publish_stored({"prompt_guard_model_output": "block"})
    async with scratch_database("outagentstr", tables=ALL_TABLES, extensions=("vector",)) as db:
        reply, _tokens = await _agent_chat(db, streamed=True)
        await _drain()

        assert reply == (_FLAGGED, None)
        [entry] = await _answer_entries(db)
        assert (entry.details["action"], entry.details["blocked"], entry.details["streamed"]) == (
            "allowed",
            False,
            True,
        )


async def test_the_general_assistant_withholds_an_answer_delivered_whole_in_block(answering):
    from app.services.general_chat import run_general_chat

    security_options.publish_stored({"prompt_guard_model_output": "block"})
    async with scratch_database("outga", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            user = _user()
            session.add_all([user, _nova(), _rule("block")])
            await session.commit()
            reply = await run_general_chat(session, str(user.id), [], "what is left?", prebuilt_llm=_LLM)
            await session.commit()

        assert reply == (MODEL_OUTPUT_WITHHELD_MESSAGE, None)
        [entry] = await _answer_entries(db)
        assert (entry.user_id, entry.entity_name) == (user.id, "general_assistant")
        assert entry.details["blocked"] is True


@pytest.mark.parametrize("streamed", [False, True])
async def test_the_widget_withholds_a_whole_answer_and_audits_a_streamed_one(answering, monkeypatch, streamed):
    from app.api.v1 import public_chat

    security_options.publish_stored({"prompt_guard_model_output": "block"})
    admin_email = f"admin-{uuid.uuid4().hex[:8]}@test.local"
    monkeypatch.setattr(public_chat.settings, "admin_email", admin_email)

    async def _on_token(chunk: str) -> None:
        return None

    async with scratch_database("outwidget", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            admin = _user(admin_email)
            agent = _widget_agent()
            session.add_all([admin, agent, _nova(), _rule("block")])
            await session.commit()
            reply = await public_chat._run_public_agent_chat(
                session,
                agent,
                [],
                "when do you open?",
                session_id="sess-1",
                on_token=_on_token if streamed else None,
            )
            await session.commit()
        await _drain()

        assert reply == (_FLAGGED if streamed else MODEL_OUTPUT_WITHHELD_MESSAGE)
        [entry] = await _answer_entries(db)
        assert (entry.user_id, entry.user_email, entry.entity_id) == (admin.id, "system:nova_guard", agent.id)
        assert (entry.details["blocked"], entry.details["streamed"]) == (not streamed, streamed)


@pytest.mark.parametrize("mode", ["log", "block"])
async def test_after_a_transfer_only_the_answer_delivered_is_evaluated(answering, monkeypatch, mode):
    """The General Assistant's own text before handing over reaches nobody: never evaluated."""
    from app.models.general_assistant_config import GeneralAssistantConfig
    from app.services.general_chat import run_general_chat
    from app.tools.base import ToolCall

    security_options.publish_stored({"prompt_guard_model_output": mode})
    handed_over = "The release is on track."

    async with scratch_database("outgaxfer", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            user = _user()
            session.add(user)
            await session.flush()
            target = _agent(user)
            session.add_all([target, _nova(), _rule("block")])
            await session.flush()
            session.add(
                GeneralAssistantConfig(
                    handover_agent_ids=[str(target.id)],
                    handover_types={str(target.id): "transfer"},
                )
            )
            await session.commit()

            async def _loop(*_args, **kwargs):
                tools = {tool.name for tool in kwargs.get("tools") or ()}
                if "transfer_to_agent" in tools:
                    await kwargs["executor"].execute_tool(
                        ToolCall(id="call-1", name="transfer_to_agent", arguments={"agent_id": str(target.id)})
                    )
                    return ChatResponse(content=_FLAGGED, model="fake-model")
                return ChatResponse(content=handed_over, model="fake-model")

            for name in (
                "app.services.agent_chat.run_agent_loop",
                "app.services.general_chat.run_agent_loop",
                "app.tools.agent_loop.run_agent_loop",
            ):
                monkeypatch.setattr(name, _loop)
            reply = await run_general_chat(session, str(user.id), [], "what is left?", prebuilt_llm=_LLM)
            await session.commit()
        await _drain()

        assert reply == (handed_over, str(target.id))
        assert await _answer_entries(db) == []
