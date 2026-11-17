"""A consulted peer runs on its configuration as it is at call time.

The Orchestrator's turn loaded the Clerk when it started; the Clerk's persona
is saved while the turn runs. The consult that follows reads the Clerk's row
again, so the nested turn's system prompt carries the new persona, and every
trace records the configuration revision (``updated_at``) its turn ran with.

These drive the real ``run_agent_chat`` of both agents; only the model loop is
scripted, and the session serves a stale copy to the turn's peer query and the
live row to the primary-key read a consult makes.
"""

from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.services.agent_chat as agent_chat_mod
from app.llm.base import ChatResponse
from app.models.agent import Agent
from app.services.builtin_tools import agent_config_revision, live_consult_target
from app.tools.base import ToolCall
from tests.test_agent_chat_evaluation import _caller_trace
from tests.test_agent_chat_smoke import _FakeRedis
from tests.test_handover_consult_chain import _IDENTITY, _ChainDB, _desk, _wire

_USER = str(uuid.uuid4())
_CONVERSATION = str(uuid.uuid4())
_P1 = "You are the Clerk, first edition: answer in one word."
_P2 = "You are the Clerk, second edition: answer with the figure and its source."


class _LiveDB(_ChainDB):
    """The peer query serves the copies loaded at turn start; ``get`` serves the live rows."""

    def __init__(self, loaded: list, live: dict) -> None:
        super().__init__(loaded, [])
        self._live = live

    async def get(self, model, key):
        if model is not Agent:
            return None
        return self._live.get(str(key))


async def _consulting_turn(orchestrator, loaded: list, live: dict) -> dict:
    """Run the Orchestrator's turn, which consults the Clerk once.

    Returns the system prompt each agent's loop was given and the metadata of
    each trace opened, by agent name.
    """
    systems: dict[str, str] = {}
    traces: dict[str, dict] = {}
    real_run_agent_chat = agent_chat_mod.run_agent_chat

    async def _trace(_db, **kwargs):
        traces[kwargs.get("agent_name") or kwargs.get("name")] = dict(kwargs.get("metadata") or {})
        return _caller_trace()

    async def _loop(**kwargs):
        # The identity reminder is its own system message, placed after the
        # history (not in the system prompt): read the name from whichever
        # system message carries it, as ``test_handover_consult_chain`` does.
        system = [m.content for m in kwargs["messages"] if m.role == "system"]
        names = [match.group(1) for text in system if (match := _IDENTITY.search(text))]
        assert names, f"no system message of the turn carries the identity reminder: {system}"
        name = names[0]
        systems[name] = "\n".join(system)
        if name == "Orchestrator":
            call = ToolCall(id="o-0", name="consult_agent", arguments={"agent_id": "clerk", "question": "What is X?"})
            result = await kwargs["executor"].execute_tool(call)
            return ChatResponse(content=str(getattr(result, "content", result)), model="fake-model", stop_reason="end_turn")
        return ChatResponse(content="X is 42.", model="fake-model", stop_reason="end_turn")

    with (
        patch("app.services.quota.check_agentic_quota", new=AsyncMock()),
        patch("app.services.quota.increment_agentic_usage", new=AsyncMock()),
        patch("app.core.redis.redis_client", new=AsyncMock(return_value=_FakeRedis())),
        patch("app.services.langfuse_tracing.trace_llm_call", new=AsyncMock(side_effect=_trace)),
        patch("app.services.langfuse_tracing.end_trace", new=MagicMock()),
        patch("app.llm.router.get_llm_for_user", new=AsyncMock(return_value=(SimpleNamespace(provider_name="fake"), "fake-model"))),
        patch("app.core.database.async_session", new=lambda: _LiveDB(loaded, live)),
        patch("app.services.prompt_guards.guard_chat_message", new=AsyncMock(return_value=None)),
        patch("app.services.prompt_guards.guard_model_output", new=AsyncMock(side_effect=lambda text, **_k: text)),
        patch.object(agent_chat_mod, "run_agent_loop", new=_loop),
    ):
        text, _ = await real_run_agent_chat(
            _LiveDB(loaded, live),  # type: ignore[arg-type]
            _USER,
            orchestrator,  # type: ignore[arg-type]
            [],
            "What is X?",
            conversation_id=_CONVERSATION,
        )
    return {"text": text, "systems": systems, "traces": traces}


def _clerk_pair():
    """The Clerk as the turn loaded it (P1) and its live row, saved later (P2)."""
    loaded = _desk("Clerk")
    loaded.persona = _P1
    loaded.updated_at = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
    live = copy.copy(loaded)
    live.persona = _P2
    live.updated_at = loaded.updated_at + timedelta(minutes=4)
    return loaded, live


@pytest.mark.asyncio
async def test_a_consult_runs_the_peer_on_the_persona_saved_since_the_turn_started():
    orchestrator = _desk("Orchestrator")
    clerk_loaded, clerk_live = _clerk_pair()
    _wire(orchestrator, consult=[clerk_loaded])

    turn = await _consulting_turn(orchestrator, [orchestrator, clerk_loaded], {str(clerk_live.id): clerk_live})

    assert _P2 in turn["systems"]["Clerk"], "the consulted turn carries the persona saved since"
    assert _P1 not in turn["systems"]["Clerk"]
    assert "X is 42." in turn["text"]


@pytest.mark.asyncio
async def test_a_persona_saved_between_two_consults_reaches_the_second():
    orchestrator = _desk("Orchestrator")
    clerk_loaded, clerk_live = _clerk_pair()
    _wire(orchestrator, consult=[clerk_loaded])
    live = {str(clerk_live.id): copy.copy(clerk_loaded)}

    first = await _consulting_turn(orchestrator, [orchestrator, clerk_loaded], live)
    assert _P1 in first["systems"]["Clerk"]

    live[str(clerk_live.id)] = clerk_live  # update_agent saved P2
    second = await _consulting_turn(orchestrator, [orchestrator, clerk_loaded], live)
    assert _P2 in second["systems"]["Clerk"] and _P1 not in second["systems"]["Clerk"]


@pytest.mark.asyncio
async def test_every_trace_records_the_configuration_revision_its_turn_ran_with():
    orchestrator = _desk("Orchestrator")
    orchestrator.updated_at = datetime(2026, 10, 30, 11, 0, tzinfo=UTC)
    clerk_loaded, clerk_live = _clerk_pair()
    _wire(orchestrator, consult=[clerk_loaded])

    turn = await _consulting_turn(orchestrator, [orchestrator, clerk_loaded], {str(clerk_live.id): clerk_live})

    assert turn["traces"]["Orchestrator"]["agent_config_revision"] == "2026-10-30T11:00:00+00:00"
    assert turn["traces"]["Clerk"]["agent_config_revision"] == clerk_live.updated_at.isoformat()
    assert turn["traces"]["Clerk"]["agent_config_revision"] != clerk_loaded.updated_at.isoformat()


@pytest.mark.asyncio
async def test_a_consult_keeps_the_loaded_row_when_the_live_read_finds_nothing():
    orchestrator = _desk("Orchestrator")
    clerk_loaded, _clerk_live = _clerk_pair()
    _wire(orchestrator, consult=[clerk_loaded])

    turn = await _consulting_turn(orchestrator, [orchestrator, clerk_loaded], {})

    assert _P1 in turn["systems"]["Clerk"]
    assert "X is 42." in turn["text"]


@pytest.mark.asyncio
async def test_live_consult_target_never_fails_a_consult():
    loaded = SimpleNamespace(id=uuid.uuid4(), persona=_P1)

    class _Broken:
        async def get(self, *_a, **_k):
            raise RuntimeError("connection lost")

    class _Found:
        def __init__(self, row):
            self.row = row

        async def get(self, model, key):
            assert model is Agent and key == loaded.id
            return self.row

    assert await live_consult_target(_Broken(), loaded) is loaded  # type: ignore[arg-type]
    assert await live_consult_target(_Found(None), loaded) is loaded  # type: ignore[arg-type]
    fresh = SimpleNamespace(id=loaded.id, persona=_P2)
    assert await live_consult_target(_Found(fresh), loaded) is fresh  # type: ignore[arg-type]


def test_agent_config_revision_is_the_rows_updated_at():
    when = datetime(2026, 10, 30, 14, 23, 5, tzinfo=UTC)
    assert agent_config_revision(SimpleNamespace(updated_at=when)) == "2026-10-30T14:23:05+00:00"
    assert agent_config_revision(SimpleNamespace(updated_at="rev-7")) == "rev-7"
    assert agent_config_revision(SimpleNamespace()) is None
    assert agent_config_revision(SimpleNamespace(updated_at=None)) is None


def test_both_consult_paths_read_the_live_row_before_the_nested_turn():
    from pathlib import Path

    for module in ("agent_chat.py", "assignment_engine.py"):
        source = (Path(agent_chat_mod.__file__).parent / module).read_text(encoding="utf-8")
        consult = source.split("async def _consult_agent(", 1)[1].split('_ho_exec.register("consult_agent"', 1)[0]
        assert "target = await live_consult_target(" in consult, module
        nested = "_run_helper_turn(" if module == "agent_chat.py" else "run_agent_chat("
        assert consult.index("live_consult_target(") < consult.index(nested), module
