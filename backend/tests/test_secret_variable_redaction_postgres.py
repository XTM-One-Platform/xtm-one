"""A run that pauses with a secret variable in context, then resumes, on a real PostgreSQL.

The persona and the prompt resolve a secret variable, and the model echoes it
in its answer and in a tool call. While the run waits for a human, what is
stored holds the variable's marker only; the resumed run gives the model and
the approved tool call the real value back.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text, update

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.core.redaction import SECRET_MARKER_RE, known_secrets_scope, remember_secret_value
from app.core.security import encrypt_value
from tests.agentic_gate_stub import stub_agentic_gate
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

SECRET = "ghp_RunSecretValue1234567890"


def _seed(variable_id: uuid.UUID):
    from app.models.agent import Agent
    from app.models.assignment import Assignment, AssignmentRun
    from app.models.user import User
    from app.models.variable import Variable

    owner = User(
        id=uuid.uuid4(),
        email=f"owner-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        full_name="Owner",
        role="user",
    )
    variable = Variable(
        id=variable_id,
        user_id=owner.id,
        is_company_managed=False,
        name="github_token",
        is_secret=True,
        value_enc=encrypt_value(SECRET),
    )
    agent = Agent(
        id=uuid.uuid4(),
        name="Repo Watcher",
        slug=f"repo-watcher-{uuid.uuid4().hex[:6]}",
        enabled=True,
        user_id=owner.id,
        is_builtin=False,
        is_company_managed=False,
        persona=f"You watch repositories. Authenticate with {{{{var_id.{variable_id}}}}}.",
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
        name="Check the repositories",
        trigger_type="manual",
        trigger_config={},
        prompt=f"List the open pull requests with the token {{{{var_id.{variable_id}}}}}.",
        enabled=True,
        dry_run=False,
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
    return owner, variable, agent, assignment, run


def _stub_the_run_surroundings(monkeypatch, loop):
    """Everything around the loop that reaches beyond the database: the model, quotas, tracing, tool loaders."""
    from app.core.config import settings
    from app.tools.wait_policy import WaitPolicy

    llm = SimpleNamespace(provider_name="anthropic", default_model="model-x", last_usage=None)
    for target, value in {
        "app.services.quota.check_agentic_quota": AsyncMock(),
        "app.services.quota.increment_agentic_usage": AsyncMock(),
        "app.services.quota.refund_agentic_usage_for_failure": AsyncMock(return_value=True),
        "app.services.quota.refund_agentic_usage_for_provider_outage": AsyncMock(return_value=True),
        "app.services.assignment_engine.record_agentic_execution": AsyncMock(),
        "app.services.assignment_engine._publish_run_status": AsyncMock(),
        "app.services.assignment_engine.event_bus": SimpleNamespace(emit=AsyncMock()),
        "app.llm.router.get_llm_for_user": AsyncMock(return_value=(llm, "model-x")),
        "app.services.langfuse_tracing.trace_llm_call": AsyncMock(return_value=None),
        "app.services.assignment_engine._load_memory_context": AsyncMock(return_value=("", "")),
        "app.services.builtin_tools.load_kb_names": AsyncMock(return_value={}),
        "app.services.builtin_tools.load_write_locked_kb_ids": AsyncMock(return_value=set()),
        "app.services.assignment_engine.load_authorized_trigger_files": AsyncMock(return_value=[]),
        "app.services.builtin_tools.load_mcp_tools": AsyncMock(return_value=None),
        "app.services.builtin_tools.load_custom_tools": AsyncMock(return_value=None),
        "app.services.integration_tools.build_integration_tools": AsyncMock(return_value=([], None)),
        "app.services.assignment_engine.resolve_run_max_iterations": AsyncMock(return_value=10),
        "app.services.assignment_engine.resolve_run_wait_policy": AsyncMock(return_value=WaitPolicy()),
        "app.tools.run_budget.build_run_budget_for_run": AsyncMock(return_value=None),
        "app.tools.agent_loop.run_agent_loop": loop,
        "app.services.tool_tracking.track_tool_usage": AsyncMock(),
        "app.services.assignment_engine._track_assignment_auth_failures": AsyncMock(),
        "app.services.assignment_engine._track_stale_reference_failure": AsyncMock(),
        "app.services.integration_tools.cancel_run_partner_tasks": AsyncMock(),
        "app.services.builtin_tools.cleanup_mcp_manager": AsyncMock(),
        "app.services.assignment_engine.create_execution": AsyncMock(),
    }.items():
        monkeypatch.setattr(target, value)
    monkeypatch.setattr(settings, "agent_self_fanout_enabled", False)
    stub_agentic_gate(monkeypatch)


async def _stored_run_text(db, run_id: uuid.UUID) -> str:
    async with db.sessions() as check:
        row = (
            await check.execute(
                text(
                    "SELECT coalesce(messages::text, '') || coalesce(pending_approvals::text, '') "
                    "|| coalesce(result_summary, '') FROM assignment_runs WHERE id = :id"
                ),
                {"id": run_id},
            )
        ).scalar_one()
    return row


@pytest.mark.parametrize("pause", ["human_input", "approval"])
async def test_a_paused_run_stores_markers_and_resumes_with_the_value(monkeypatch, pause):
    from app.llm.base import ChatMessage
    from app.models.assignment import AssignmentRun
    from app.services import assignment_engine
    from app.tools.base import HumanInputRequestedError, ToolCall

    variable_id = uuid.uuid4()
    seen: list[list[ChatMessage]] = []
    approved: list[list[dict]] = []
    echo = f"I will call the API with {SECRET} once you confirm."

    async def _loop(**kwargs):
        messages = kwargs["messages"]
        seen.append(list(messages))
        if len(seen) == 1:
            call = ToolCall(
                id="call-1",
                name="request_human_input" if pause == "human_input" else "http_request",
                arguments={"question": "Confirm?", "authorization": f"Bearer {SECRET}"},
            )
            conversation = [*messages, ChatMessage(role="assistant", content=echo, tool_calls=[call])]
            proposals = (
                [{"tool_call_id": "call-1", "tool_name": "http_request", "arguments": call.arguments}]
                if pause == "approval"
                else None
            )
            raise HumanInputRequestedError("Confirm?", "call-1", conversation, proposals)
        messages.append(ChatMessage(role="assistant", content=f"Done, {SECRET} worked."))
        return SimpleNamespace(content=f"Done, {SECRET} worked.", usage={}, model="model-x", stop_reason="end_turn")

    async def _apply(*, proposals, messages, **_kwargs):
        approved.append(proposals)
        for proposal in proposals:
            messages.append(
                ChatMessage(role="tool", content="200 OK", tool_call_id=proposal["tool_call_id"], name="http_request")
            )

    _stub_the_run_surroundings(monkeypatch, _loop)
    monkeypatch.setattr(assignment_engine, "_apply_approval_decisions", _apply)

    async with scratch_database(
        "secretvarpause", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db:
        rows = _seed(variable_id)
        async with db.sessions() as seed:
            # One flush per level: no relationship orders these inserts.
            for level in (rows[:1], rows[1:3], rows[3:4], rows[4:]):
                seed.add_all(level)
                await seed.flush()
            await seed.commit()
        run_id = rows[4].id

        # The run pauses: the model had the value, the database holds markers only.
        await assignment_engine.execute_assignment_run(str(run_id))
        if not seen:
            async with db.sessions() as check:
                ended = (await check.execute(select(AssignmentRun).where(AssignmentRun.id == run_id))).scalar_one()
            pytest.fail(f"the loop never ran: {ended.status} {ended.error}")
        first = seen[0]
        assert any(SECRET in (message.content or "") for message in first), "the model reads the value"
        stored = await _stored_run_text(db, run_id)
        assert SECRET not in stored
        assert {match.group(1) for match in SECRET_MARKER_RE.finditer(stored)} == {str(variable_id)}

        async with db.sessions() as answer:
            paused = (await answer.execute(select(AssignmentRun).where(AssignmentRun.id == run_id))).scalar_one()
            assert paused.status == "awaiting_human"
            if pause == "approval":
                paused.approval_decisions = {"call-1": {"decision": "approve"}}
            else:
                paused.human_input_response = "Yes, go ahead."
            await answer.commit()

        # The run resumes: the model and the approved call get the value back.
        await assignment_engine.execute_assignment_run(str(run_id))
        resumed = seen[1]
        assert any(SECRET in (message.content or "") for message in resumed if message.role in ("system", "user"))
        assistant = next(message for message in resumed if message.role == "assistant" and message.tool_calls)
        assert assistant.content == echo
        assert assistant.tool_calls[0].arguments["authorization"] == f"Bearer {SECRET}"
        if pause == "approval":
            assert approved and approved[0][0]["arguments"]["authorization"] == f"Bearer {SECRET}"

        stored = await _stored_run_text(db, run_id)
        assert SECRET not in stored and "[[SECRET_VARIABLE:" in stored

        # A Core write on asyncpg's positional parameters: the content column it sets
        # carries the marker; the status it sets and the WHERE values are left alone.
        with known_secrets_scope(run_id):
            remember_secret_value(variable_id, SECRET)
            async with db.sessions() as core:
                written = await core.execute(
                    update(AssignmentRun)
                    .where(AssignmentRun.id == run_id, AssignmentRun.status == "completed")
                    .values(error=f"echoed {SECRET}", status="completed")
                )
                await core.commit()
        assert written.rowcount == 1
        async with db.sessions() as check:
            status, error = (
                await check.execute(select(AssignmentRun.status, AssignmentRun.error).where(AssignmentRun.id == run_id))
            ).one()
        assert status == "completed"
        assert SECRET not in error and "[[SECRET_VARIABLE:" in error


async def test_a_batched_core_insert_stores_markers_in_every_row():
    """asyncpg sends ``insert(...).returning(...)`` of several rows as one statement with flat parameters."""
    from sqlalchemy import insert

    from app.models.activity import Activity
    from app.models.user import User

    variable_id = uuid.uuid4()
    async with scratch_database(
        "secretvarbatch", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db:
        owner = User(
            id=uuid.uuid4(), email=f"owner-{uuid.uuid4().hex[:8]}@example.com", hashed_password="x",
            full_name="Owner", role="user",
        )
        async with db.sessions() as seed:
            seed.add(owner)
            await seed.commit()
        with known_secrets_scope(uuid.uuid4()):
            remember_secret_value(variable_id, SECRET)
            async with db.sessions() as core:
                await core.execute(
                    insert(Activity).returning(Activity.id),
                    [
                        {"user_id": owner.id, "activity_type": "notice", "title": "Run", "summary": f"echoed {SECRET} {n}"}
                        for n in range(3)
                    ],
                )
                await core.commit()
        async with db.sessions() as check:
            summaries = (await check.execute(select(Activity.summary))).scalars().all()
    assert len(summaries) == 3
    assert all(SECRET not in summary and SECRET_MARKER_RE.search(summary) for summary in summaries)
