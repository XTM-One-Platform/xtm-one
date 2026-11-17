"""The mandatory-transfer routing call is in its turn's stored trace.

With two or more mandatory-transfer targets, ``run_agent_chat`` asks a model
which one gets the turn once its loop has returned. The turn's root trace was
closed before that call, so its generation was never written and the trace's
token total left it out. The real ``run_agent_chat`` runs here against
PostgreSQL, with the agent loop and the routing model stubbed and the trace
store's queue recording what ``end_trace`` submits.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.llm.base import ChatResponse
from app.services import langfuse_tracing as lft
from tests.scratch_database import ALL_TABLES, scratch_database
from tests.test_prompt_guard_chat_surfaces_postgres import _FakeRedis, _user
from tests.test_prompt_guard_session_postgres import _agent, _stub_turn

pytestmark = pytest.mark.integration

_LOOP_USAGE = {"input_tokens": 1_000, "output_tokens": 200}
_ROUTING_USAGE = {"prompt_tokens": 280, "completion_tokens": 2, "total_tokens": 282}
_ANSWER = "The refund was approved and the customer was told."
# Read before ``_stub_turn`` stands a mock in for it.
_TRACE_LLM_CALL = lft.trace_llm_call


class _RoutingLLM:
    provider_name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages=None, **kwargs) -> ChatResponse:
        self.calls += 1
        return ChatResponse(content="2", model="router-model", stop_reason="end_turn", usage=dict(_ROUTING_USAGE))


@pytest.fixture
def turn(monkeypatch) -> SimpleNamespace:
    """The loop answers, the routing model picks the second target, traces are recorded."""
    submitted: list[tuple[object, list]] = []
    routing = _RoutingLLM()

    async def _loop(**_kwargs):
        return ChatResponse(content=_ANSWER, model="agent-model", stop_reason="end_turn", usage=dict(_LOOP_USAGE))

    _stub_turn(monkeypatch)
    for target, value in {
        "app.core.redis.redis_client": AsyncMock(return_value=_FakeRedis()),
        # The real trace open, local-only: Langfuse is not configured.
        "app.services.langfuse_tracing.trace_llm_call": _TRACE_LLM_CALL,
        "app.services.langfuse_tracing.get_langfuse_client": AsyncMock(return_value=None),
        "app.services.tracing.dispatcher.submit_trace_for_flush": lambda trace, spans: submitted.append(
            (trace, list(spans))
        ),
        "app.services.agent_chat.run_agent_loop": _loop,
        "app.services.agent_chat.enforce_agentic_quota_ex": AsyncMock(return_value=(None, True)),
        "app.llm.router.get_llm_for_user": AsyncMock(return_value=(routing, "router-model")),
    }.items():
        monkeypatch.setattr(target, value)
    return SimpleNamespace(submitted=submitted, routing=routing)


async def test_the_routing_call_is_in_the_source_turns_stored_trace(turn):
    from app.services.agent_chat import run_agent_chat

    async with (
        scratch_database("mtroute", tables=ALL_TABLES, extensions=("vector",)) as db,
        db.sessions() as session,
    ):
            user = _user()
            session.add(user)
            await session.flush()
            billing, refunds = _agent(user), _agent(user)
            session.add_all([billing, refunds])
            await session.flush()
            source = _agent(user)
            source.handover_agent_ids = [str(billing.id), str(refunds.id)]
            source.handover_types = {str(billing.id): "mandatory_transfer", str(refunds.id): "mandatory_transfer"}
            source.handover_conditions = {
                str(billing.id): "the answer is about an invoice",
                str(refunds.id): "the answer is about a refund",
            }
            session.add(source)
            await session.commit()
            source_id, refunds_id = source.id, refunds.id

            _reply, final_agent_id = await run_agent_chat(
                session,
                str(user.id),
                source,
                [],
                "Where is my refund?",
                prebuilt_llm=(SimpleNamespace(provider_name="fake"), "agent-model"),
            )
            await session.commit()

    assert turn.routing.calls == 1
    assert final_agent_id == str(refunds_id)
    [(trace, spans)] = [(t, s) for t, s in turn.submitted if t.agent_id == source_id]
    [routing_span] = [span for span in spans if span.name == "routing_classifier"]
    assert (routing_span.model, routing_span.input_tokens, routing_span.output_tokens) == ("router-model", 280, 2)
    # The trace's totals count the loop's calls and the routing call.
    assert (trace.input_tokens, trace.output_tokens, trace.total_tokens) == (1_280, 202, 1_482)
    assert [part["model"] for part in trace.usage_details[lft.USAGE_PARTS_KEY]] == ["agent-model", "router-model"]
    assert (trace.status, trace.output) == ("ok", _ANSWER)
    # The target's turn is traced on its own, under the source's trace.
    [(target_trace, _target_spans)] = [(t, s) for t, s in turn.submitted if t.agent_id == refunds_id]
    assert target_trace.parent_trace_id == trace.id
