"""An agent can be kept from spawning background copies of itself.

Self-fan-out (``spawn_background_task(agent_id="self")``) stays on by default;
``Agent.allow_self_fanout`` turns it off for one agent. The platform switch
``AGENT_SELF_FANOUT_ENABLED`` still wins, a NULL reads as on, and only the self
target goes: the agent's peer handovers are untouched.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from app.api.v1.schemas.agent import AgentResponse, CreateAgentRequest, UpdateAgentRequest
from app.models.agent_copy import (
    AGENT_BUNDLE_PORTABLE_FIELDS,
    AGENT_CATALOG_CLONE_FIELDS,
    AGENT_COPY_FIELDS,
    PUBLIC_DOWNLOAD_AGENT_FIELDS,
)
from app.services import agent_chat
from app.services.builtin_tools import self_fanout_enabled
from tests import test_handover_reachable_peers as harness
from tests.agentic_gate_stub import stub_agentic_gate


@pytest.fixture(autouse=True)
def _agentic_ai_available(monkeypatch):
    stub_agentic_gate(monkeypatch)


def _agent_allows(monkeypatch, value) -> None:
    """Every agent the harness builds carries ``allow_self_fanout=value``."""
    built = harness._objects

    def _objects(*args, **kwargs):
        run, assignment, agent = built(*args, **kwargs)
        agent.allow_self_fanout = value
        return run, assignment, agent

    monkeypatch.setattr(harness, "_objects", _objects)


# ---------------------------------------------------------------------------
# The one helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("platform", "agent_value", "expected"),
    [
        (True, True, True),
        (True, None, True),
        (True, False, False),
        (False, True, False),
        (False, None, False),
        (False, False, False),
    ],
)
def test_the_agent_and_the_platform_both_have_to_allow_it(monkeypatch, platform, agent_value, expected):
    from app.core.config import settings

    monkeypatch.setattr(settings, "agent_self_fanout_enabled", platform)
    assert self_fanout_enabled(SimpleNamespace(allow_self_fanout=agent_value)) is expected


def test_an_agent_with_no_such_attribute_reads_as_on(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "agent_self_fanout_enabled", True)
    assert self_fanout_enabled(SimpleNamespace()) is True


# ---------------------------------------------------------------------------
# An assignment run, end to end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_agent_with_the_option_off_gets_no_copy_of_itself(monkeypatch):
    _agent_allows(monkeypatch, False)
    consulted = harness._peer("Reviewer")
    seen = await harness._run_turn(monkeypatch, [consulted], {str(consulted.id): "consult"}, self_fanout=True)

    assert "spawn_background_task" not in seen["tools"]
    assert "Parallel self-execution" not in seen["system"]
    assert "consult_agent" in seen["tools"]


@pytest.mark.asyncio
async def test_the_option_off_keeps_the_delegate_peers(monkeypatch):
    _agent_allows(monkeypatch, False)
    delegated = harness._peer("Worker")
    seen = await harness._run_turn(
        monkeypatch,
        [delegated],
        {str(delegated.id): "delegate"},
        self_fanout=True,
        calls=[("spawn_background_task", {"agent_id": "self", "task": "Take half of it."})],
    )

    assert "spawn_background_task" in seen["tools"]
    assert set(harness._listed_peers(seen["system"])) == {str(delegated.id)}
    assert "Parallel self-execution" not in seen["system"]
    # The refusal names the switch that is off.
    assert seen["results"] == [
        "Agent self is not available for background tasks: running a copy of "
        "yourself is turned off for this agent (allow_self_fanout) or for the "
        "platform (AGENT_SELF_FANOUT_ENABLED)."
    ]
    seen["peer_runs"].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_value", [True, None], ids=["on", "unset"])
async def test_an_agent_that_does_not_turn_it_off_keeps_it(monkeypatch, agent_value):
    _agent_allows(monkeypatch, agent_value)
    consulted = harness._peer("Reviewer")
    seen = await harness._run_turn(monkeypatch, [consulted], {str(consulted.id): "consult"}, self_fanout=True)

    assert "spawn_background_task" in seen["tools"]
    assert "Parallel self-execution" in seen["system"]


@pytest.mark.asyncio
async def test_the_platform_switch_wins_over_the_agent(monkeypatch):
    _agent_allows(monkeypatch, True)
    consulted = harness._peer("Reviewer")
    seen = await harness._run_turn(monkeypatch, [consulted], {str(consulted.id): "consult"}, self_fanout=False)

    assert "spawn_background_task" not in seen["tools"]
    assert "Parallel self-execution" not in seen["system"]


# ---------------------------------------------------------------------------
# Chat reads the same helper
# ---------------------------------------------------------------------------


def test_chat_decides_self_fan_out_through_the_helper():
    source = inspect.getsource(agent_chat)
    start = source.index("_self_fanout_allowed = (")
    block = source[start : source.index("\n        )", start)]
    assert "self_fanout_enabled(agent)" in block
    assert "settings.agent_self_fanout_enabled" not in block


# ---------------------------------------------------------------------------
# The option is part of the agent
# ---------------------------------------------------------------------------


def test_the_api_serves_and_accepts_the_option():
    assert AgentResponse.model_fields["allow_self_fanout"].default is True
    assert CreateAgentRequest.model_fields["allow_self_fanout"].default is True
    assert UpdateAgentRequest.model_fields["allow_self_fanout"].default is None
    assert UpdateAgentRequest(allow_self_fanout=False).model_dump(exclude_unset=True) == {"allow_self_fanout": False}


@pytest.mark.parametrize(
    "manifest",
    [AGENT_COPY_FIELDS, AGENT_BUNDLE_PORTABLE_FIELDS, AGENT_CATALOG_CLONE_FIELDS, PUBLIC_DOWNLOAD_AGENT_FIELDS],
    ids=["duplicate", "bundle", "catalog_clone", "public_download"],
)
def test_every_copy_of_the_agent_carries_it(manifest):
    # It narrows what the agent may do: a copy that dropped it would turn
    # self-fan-out back on.
    assert "allow_self_fanout" in manifest


def test_the_agent_builder_can_change_it():
    from app.services.builtin_tools import agent_builder

    source = inspect.getsource(agent_builder)
    assert '"allow_self_fanout": {' in source
    assert '("allow_self_fanout", allow_self_fanout)' in source
