"""What an agent of a multi-agent turn reads of the agents after it, on PostgreSQL.

``run_agent_chat`` and ``run_general_chat`` run for real against a scratch
database with the agent loop stubbed: the agents handed to them as
``agents_after`` reach the model as a line-up on the message the agent
answers - after the user's words and any reference brief - never in the
system prompt, and a transfer hands them on to the agent that answers in its
place, which never reads itself among them. A turn without them sends the
message as it always did.

``integration``-marked and skips when PostgreSQL is unreachable.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.llm.base import ChatResponse
from app.models.general_assistant_config import GeneralAssistantConfig
from app.services import nova_engine
from app.services.mention_dispatch import MentionTarget
from app.tools.base import ToolCall
from tests.scratch_database import ALL_TABLES, scratch_database
from tests.test_prompt_guard_chat_surfaces_postgres import _FakeRedis, _user
from tests.test_prompt_guard_session_postgres import _agent, _stub_turn

pytestmark = pytest.mark.integration

_LLM = (SimpleNamespace(provider_name="fake"), "fake-model")
_LOOPS = (
    "app.services.agent_chat.run_agent_loop",
    "app.services.general_chat.run_agent_loop",
    "app.tools.agent_loop.run_agent_loop",
)
BETA = MentionTarget(agent_id="b", name="Beta", agent=SimpleNamespace(id="b", name="Beta", slug="beta"))


def _lineup(entries: str) -> str:
    return (
        "[Agents answering after you in this turn]\n"
        f"The user's message also addresses {entries}. After your reply, each of them answers it "
        "in a reply of its own, in this order, reading the replies given before its own in this turn. "
        "Give your own part only: the message "
        "already reaches them, so there is nothing to forward or to ask the user to relay."
    )


LINEUP = _lineup('"Beta" (@beta)')


@pytest.fixture
def loop(monkeypatch) -> AsyncMock:
    """The agent loop answers at once; every call it gets is recorded."""
    loop = AsyncMock(return_value=ChatResponse(content="done", model="fake-model"))
    monkeypatch.setattr(nova_engine, "NOVA_HUNTING_AVAILABLE", False)
    _stub_turn(monkeypatch)
    for target, value in {
        "app.core.redis.redis_client": AsyncMock(return_value=_FakeRedis()),
        "app.services.agent_chat.enforce_agentic_quota_ex": AsyncMock(return_value=(None, True)),
        "app.llm.router.get_llm_for_user": AsyncMock(return_value=_LLM),
        **dict.fromkeys(_LOOPS, loop),
    }.items():
        monkeypatch.setattr(target, value)
    return loop


@asynccontextmanager
async def _session(name: str):
    async with scratch_database(name, tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        yield session


def _sent(loop_kwargs: dict) -> tuple[str, str]:
    """Every system message the loop got, and the message the agent answers."""
    messages = loop_kwargs["messages"]
    system = "\n".join(m.content or "" for m in messages if m.role == "system")
    return system, [m for m in messages if m.role == "user"][-1].content


def _count(loop_kwargs: dict, text: str) -> int:
    return sum(1 for m in loop_kwargs["messages"] if text in (m.content or ""))


@pytest.mark.parametrize(
    ("brief", "after", "sent"),
    [
        pytest.param(None, None, "what do you think?", id="neither"),
        pytest.param(None, [], "what do you think?", id="nobody-after"),
        pytest.param(None, [BETA], f"what do you think?\n\n{LINEUP}", id="line-up"),
        pytest.param("THE BRIEF", [BETA], f"what do you think?\n\nTHE BRIEF\n\n{LINEUP}", id="both"),
    ],
)
async def test_the_agent_reads_the_line_up_on_the_message_it_answers(loop, brief, after, sent):
    from app.services.agent_chat import run_agent_chat

    async with _session("lineupagent") as session:
        user = _user()
        session.add(user)
        await session.flush()
        agent = _agent(user)
        session.add(agent)
        await session.commit()

        await run_agent_chat(
            session,
            str(user.id),
            agent,
            [],
            "what do you think?",
            prebuilt_llm=_LLM,
            reference_brief=brief,
            agents_after=after,
        )

    kwargs = loop.await_args.kwargs
    system, message = _sent(kwargs)
    assert message == sent
    assert "Agents answering after you" not in system
    assert _count(kwargs, LINEUP) == (1 if after else 0)


@pytest.mark.parametrize("after", [None, [BETA]], ids=["without", "with"])
async def test_the_general_assistant_reads_the_line_up_on_the_message_it_answers(loop, after):
    from app.services.general_chat import run_general_chat

    async with _session("lineupga") as session:
        user = _user()
        session.add(user)
        await session.commit()

        await run_general_chat(
            session, str(user.id), [], "what do you think?", prebuilt_llm=_LLM, agents_after=after
        )

    kwargs = loop.await_args.kwargs
    system, message = _sent(kwargs)
    assert message == (f"what do you think?\n\n{LINEUP}" if after else "what do you think?")
    assert "Agents answering after you" not in system
    assert _count(kwargs, LINEUP) == (1 if after else 0)


@pytest.mark.parametrize("target_mentioned", [False, True], ids=["another-agent", "a-mentioned-agent"])
async def test_the_agent_a_transfer_hands_the_message_to_reads_the_agents_after_it(
    monkeypatch, loop, target_mentioned
):
    """The General Assistant transfers the message: the agent answering in its
    place reads the agents after it - never itself, when the user had also
    mentioned it (it answers here and is not asked again)."""
    from app.services.general_chat import run_general_chat

    sent: list[str] = []

    async with _session("lineupxfer") as session:
        user = _user()
        session.add(user)
        await session.flush()
        target = _agent(user)
        session.add(target)
        await session.flush()
        session.add(
            GeneralAssistantConfig(
                handover_agent_ids=[str(target.id)],
                handover_types={str(target.id): "transfer"},
            )
        )
        await session.commit()
        after = [BETA]
        if target_mentioned:
            after = [MentionTarget(agent_id=str(target.id), name="Release Desk", agent=target), BETA]

        async def _loop(*_args, **kwargs):
            system, message = _sent(kwargs)
            assert "Agents answering after you" not in system
            sent.append(message)
            if "transfer_to_agent" in {tool.name for tool in kwargs.get("tools") or ()}:
                await kwargs["executor"].execute_tool(
                    ToolCall(id="call-1", name="transfer_to_agent", arguments={"agent_id": str(target.id)})
                )
                return ChatResponse(content="handing over", model="fake-model")
            return ChatResponse(content="the target answers", model="fake-model")

        for name in _LOOPS:
            monkeypatch.setattr(name, _loop)
        reply = await run_general_chat(
            session, str(user.id), [], "what do you think?", prebuilt_llm=_LLM, agents_after=after
        )

    assert reply == ("the target answers", str(target.id))
    general_reads = _lineup(f'"Release Desk" (@{target.slug}), "Beta" (@beta)') if target_mentioned else LINEUP
    assert sent == [f"what do you think?\n\n{general_reads}", f"what do you think?\n\n{LINEUP}"]
