"""A turn whose answer goes to an agent acts for its user but does not talk to them, on real PostgreSQL.

A consult, a background task and an A2A partner's task run as a user without
that user being the one the model addresses. Presented with "Assisting Alice",
the model answers Alice: a question it needs answered lands in its reply
instead of with the agent that asked (an A2A task then never reaches
``input-required``). The identity line says whom the turn acts for instead,
the way an assignment's does.
"""

from __future__ import annotations

import pytest

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.a2a import tasks as a2a_tasks
from app.models.agent import Agent
from app.models.user import User
from tests.scratch_database import ALL_TABLES, scratch_database
from tests.test_memory_quarantine_chat_postgres import _LLM, _StopTurnError, _stub_turn

pytestmark = pytest.mark.integration

_PERSONA = "You are the partner desk agent of the test suite."


async def _system_prompt(monkeypatch, handover_context: dict | None) -> str:
    """The system prompt of one turn Alice's agent runs, handed *handover_context*."""
    from app.services.agent_chat import run_agent_chat

    async with (
        scratch_database("agent_facing_identity", tables=ALL_TABLES, extensions=("vector",)) as db,
        db.sessions() as session,
    ):
        user = User(email="alice@test.local", full_name="Alice Owner", hashed_password="x")
        session.add(user)
        await session.flush()
        agent = Agent(
            name="Partner Desk", slug="partner-desk", persona=_PERSONA, enabled=True, user_id=user.id,
            is_builtin=False, is_company_managed=False, builtin_tool_ids=[], handover_agent_ids=[],
            integration_bindings={}, mcp_server_ids=[], custom_tool_ids=[], knowledge_base_ids=[],
            output_format="markdown", memory_short_term_enabled=False, memory_medium_term_enabled=False,
            memory_persistent_enabled=False, memory_user_enabled=False,
        )
        session.add(agent)
        await session.commit()
        measured = _stub_turn(monkeypatch)
        with pytest.raises(_StopTurnError):
            await run_agent_chat(
                session, str(user.id), agent, [], "Which primary asset is mine?",
                handover_context=handover_context, meter_quota=False, prebuilt_llm=_LLM,
            )
    (prompt,) = [text for text in measured if isinstance(text, str) and text.startswith(_PERSONA)]
    return prompt


async def test_a_chat_turn_assists_its_user(monkeypatch):
    prompt = await _system_prompt(monkeypatch, None)
    assert "Assisting Alice Owner" in prompt


async def test_a_transfer_hands_the_turn_a_live_chat_with_its_user(monkeypatch):
    prompt = await _system_prompt(monkeypatch, {"type": "transfer", "source_agent": "Router"})
    assert "Assisting Alice Owner" in prompt


@pytest.mark.parametrize("ho_type", ["consult", "sub_agent_task"])
async def test_a_turn_answering_an_agent_acts_for_its_user(monkeypatch, ho_type):
    prompt = await _system_prompt(monkeypatch, {"type": ho_type, "source_agent": "Router"})
    assert "Acting on behalf of Alice Owner" in prompt
    assert "Assisting Alice Owner" not in prompt


async def test_an_a2a_task_is_a_consult_by_the_partner_acting_for_the_run_as_user(monkeypatch):
    prompt = await _system_prompt(monkeypatch, dict(a2a_tasks._PARTNER_HANDOVER_CONTEXT))
    assert "Consulted by an external partner agent over A2A" in prompt
    assert "Acting on behalf of Alice Owner" in prompt
    assert "Assisting Alice Owner" not in prompt
