import uuid

import pytest

from app.models.agent import Agent
from app.models.assignment import Assignment
from app.services.assignment_helpers import _build_system_message
from app.services.prompt_guard import SAFETY_SYSTEM_PROMPT


class _FakeScalarResult:
    def scalar_one_or_none(self):
        return None


class _FakeDB:
    def __init__(self) -> None:
        self.execute_calls = 0

    async def execute(self, *args, **kwargs):
        self.execute_calls += 1
        return _FakeScalarResult()


@pytest.mark.asyncio
async def test_assignment_system_message_includes_safety_prompt():
    agent = Agent(name="Webhook agent", persona="You are a helpful automation agent.")
    assignment = Assignment(
        agent_id=uuid.uuid4(),
        name="Webhook assignment",
        trigger_type="webhook",
        trigger_config={},
        prompt="Process the webhook payload",
    )

    system_message = await _build_system_message(
        db=None,
        agent=agent,
        assignment=assignment,
        user_id=None,
        user_memory_text="",
        agent_memory_text="",
        element_bindings={},
    )

    assert SAFETY_SYSTEM_PROMPT in system_message


@pytest.mark.asyncio
async def test_assignment_system_message_includes_safety_prompt_with_user_context():
    agent = Agent(name="Webhook agent", persona="You are a helpful automation agent.")
    assignment = Assignment(
        agent_id=uuid.uuid4(),
        name="Webhook assignment",
        trigger_type="webhook",
        trigger_config={},
        prompt="Process the webhook payload",
    )
    db = _FakeDB()

    system_message = await _build_system_message(
        db=db,
        agent=agent,
        assignment=assignment,
        user_id=uuid.uuid4(),
        user_memory_text="",
        agent_memory_text="",
        element_bindings={},
    )

    assert SAFETY_SYSTEM_PROMPT in system_message
    assert db.execute_calls == 1


@pytest.mark.asyncio
async def test_assignment_system_message_places_safety_after_guardrails_and_last():
    agent = Agent(
        name="Webhook agent",
        persona="You are a helpful automation agent.",
        guardrails="Never execute destructive actions.",
    )
    assignment = Assignment(
        agent_id=uuid.uuid4(),
        name="Webhook assignment",
        trigger_type="webhook",
        trigger_config={},
        prompt="Process the webhook payload",
    )

    system_message = await _build_system_message(
        db=None,
        agent=agent,
        assignment=assignment,
        user_id=None,
        user_memory_text="",
        agent_memory_text="",
        element_bindings={},
    )

    guardrails_fragment = "Guardrails:\nNever execute destructive actions."
    assert guardrails_fragment in system_message
    assert system_message.index(guardrails_fragment) < system_message.index(
        SAFETY_SYSTEM_PROMPT
    )
    assert system_message.rstrip().endswith(SAFETY_SYSTEM_PROMPT.strip())
