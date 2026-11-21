"""An assignment with HITL on pauses on ``request_human_input`` and resumes, on a real PostgreSQL (#842, S1).

The loop calls the real tool through the run's executor: the question the
person reads carries the options, and the resumed run reads the option chosen
or the person's own words as the tool result. The old call shape (question
and context alone) pauses with the same question as before.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

import app.models  # noqa: F401 - registers every table on Base.metadata
from tests.scratch_database import ALL_TABLES, scratch_database
from tests.test_secret_variable_redaction_postgres import _stub_the_run_surroundings

pytestmark = pytest.mark.integration


def _seed() -> list[Any]:
    from app.models.agent import Agent
    from app.models.assignment import Assignment, AssignmentRun
    from app.models.user import User

    owner = User(
        id=uuid.uuid4(),
        email=f"owner-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        full_name="Owner",
        role="user",
    )
    agent = Agent(
        id=uuid.uuid4(),
        name="Mail Helper",
        slug=f"mail-helper-{uuid.uuid4().hex[:6]}",
        enabled=True,
        user_id=owner.id,
        is_builtin=False,
        is_company_managed=False,
        persona="You send mail.",
        integration_bindings={},
        builtin_tool_ids=[],
        handover_agent_ids=[],
        output_format="markdown",
    )
    assignment = Assignment(
        id=uuid.uuid4(),
        agent_id=agent.id,
        user_id=owner.id,
        is_company_managed=False,
        name="Send the weekly note",
        trigger_type="manual",
        trigger_config={},
        prompt="Send the weekly note.",
        enabled=True,
        dry_run=False,
        human_in_the_loop=True,
        timeout_seconds=300,
        retry_on_failure=False,
        max_retries=0,
        notify_on_complete=False,
        execution_count=0,
    )
    run = AssignmentRun(
        id=uuid.uuid4(),
        assignment_id=assignment.id,
        agent_id=agent.id,
        user_id=owner.id,
        is_company_managed=False,
        status="pending",
        trigger_payload={},
        trigger_context={"type": "manual"},
        retry_count=0,
    )
    return [owner, agent, assignment, run]


OPTIONS = [
    {"key": "A", "label": "Sales", "effect": "Sends from the sales mailbox"},
    {"key": "B", "label": "Support", "effect": "Sends from the support mailbox"},
]


@pytest.mark.parametrize(
    ("arguments", "answer", "question", "tool_result"),
    [
        (
            {"question": "Which mailbox should I use?", "options": OPTIONS, "recommendation": "A"},
            "b",
            "Which mailbox should I use?\n\nOptions:\n- A. Sales (recommended): Sends from the sales mailbox\n"
            "- B. Support: Sends from the support mailbox",
            "Human chose option B (Support)",
        ),
        (
            {"question": "Which mailbox should I use?", "options": OPTIONS},
            "Use the shared one",
            None,
            "Human response: Use the shared one",
        ),
        (
            {"question": "Which mailbox should I use?", "context": "Two are configured."},
            "Sales",
            "Which mailbox should I use?\n\nContext: Two are configured.",
            "Human response: Sales",
        ),
    ],
    ids=["option", "free-text-over-options", "old-call-shape"],
)
async def test_hitl_pauses_and_resumes(
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, Any],
    answer: str,
    question: str | None,
    tool_result: str,
) -> None:
    from app.llm.base import ChatMessage
    from app.models.assignment import AssignmentRun
    from app.services import assignment_engine
    from app.tools.base import HumanInputRequestedError, ToolCall

    seen: list[list[ChatMessage]] = []
    refused: list[str] = []

    async def _loop(**kwargs: Any) -> SimpleNamespace:
        messages = kwargs["messages"]
        seen.append(list(messages))
        if len(seen) == 1:
            executor = kwargs["executor"]
            # An unreadable question is refused before the pause: the agent asks again.
            bad = ToolCall(id="call-0", name="request_human_input", arguments={"question": "CARD SENT 12:43Z?"})
            refused.append((await executor.execute_tool(bad)).content)
            call = ToolCall(id="call-1", name="request_human_input", arguments=arguments)
            try:
                await executor.execute_tool(call)
            except HumanInputRequestedError as pause:
                pause.tool_call_id = call.id
                pause.conversation = [*messages, ChatMessage(role="assistant", content="", tool_calls=[call])]
                raise
            pytest.fail("request_human_input did not pause the run")
        messages.append(ChatMessage(role="assistant", content="Sent."))
        return SimpleNamespace(content="Sent.", usage={}, model="model-x", stop_reason="end_turn")

    _stub_the_run_surroundings(monkeypatch, _loop)  # type: ignore[no-untyped-call]

    async with scratch_database(
        "hitloptions", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db:
        rows = _seed()
        async with db.sessions() as seed:
            for row in rows:
                seed.add(row)
                await seed.flush()
            await seed.commit()
        run_id = rows[-1].id

        await assignment_engine.execute_assignment_run(str(run_id))
        assert seen, "the loop never ran"
        assert refused and "nothing was asked" in refused[0] and "ask.question is read by a person" in refused[0]
        async with db.sessions() as respond:
            paused = (await respond.execute(select(AssignmentRun).where(AssignmentRun.id == run_id))).scalar_one()
            assert paused.status == "awaiting_human", paused.error
            if question is not None:
                assert paused.human_input_question == question
            assert "Which mailbox should I use?" in paused.human_input_question
            paused.human_input_response = answer
            await respond.commit()

        await assignment_engine.execute_assignment_run(str(run_id))
        assert len(seen) == 2
        results = [m for m in seen[1] if m.role == "tool" and m.name == "request_human_input"]
        assert results and results[-1].content == tool_result
        async with db.sessions() as check:
            done = (await check.execute(select(AssignmentRun).where(AssignmentRun.id == run_id))).scalar_one()
        assert done.status == "completed", done.error
        assert done.human_input_question is None
