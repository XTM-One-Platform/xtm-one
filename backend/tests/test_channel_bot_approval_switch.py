"""A channel bot's own approval switch.

A bot contributes tools that send, edit and delete messages in somebody's
Slack, Teams or Discord, and it had no per-connection switch — only the
platform-wide whitelist, which decides for every bot at once. This is the
counterpart of ``integrations.requires_approval``, with one difference that
matters: it is keyed by **bot**, not by provider, because two Slack bots can
disagree and the switch is shown per configured bot.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services.hitl_whitelist import resolve_exempt_sources


class _DB:
    """Answers the two selects in ``resolve_exempt_sources``, in order."""

    def __init__(self, ungated_integrations=(), ungated_bots=()):
        self._answers = [
            [(p, f"{p}-id") for p in ungated_integrations],
            # ``(provider, id)``: the source carries both, because the gate keys
            # on the bot while every other consumer reads the provider.
            [(prov, b) for prov, b in ungated_bots],
        ]

    async def execute(self, _stmt):
        rows = self._answers.pop(0)
        return SimpleNamespace(all=lambda: rows)


@pytest.fixture(autouse=True)
def _no_platforms(monkeypatch):
    import app.services.hitl_whitelist as svc

    async def _none(_db):
        return frozenset(), frozenset()

    monkeypatch.setattr(svc, "platform_approval_by_provider", _none)


@pytest.mark.asyncio
async def test_a_bot_left_gated_exempts_nothing():
    sources = await resolve_exempt_sources(_DB())

    assert sources == frozenset()


@pytest.mark.asyncio
async def test_switching_a_bot_off_exempts_that_bot_only():
    """Keyed by bot id, not provider: the other Slack bot keeps asking."""
    ungated, other = uuid.uuid4(), uuid.uuid4()

    sources = await resolve_exempt_sources(
        _DB(ungated_bots=(("slack_bot", ungated),))
    )

    assert f"channel_bot:slack_bot:{ungated}" in sources
    assert f"channel_bot:slack_bot:{other}" not in sources


@pytest.mark.asyncio
async def test_bots_and_integrations_are_independent():
    bot = uuid.uuid4()

    sources = await resolve_exempt_sources(
        _DB(ungated_integrations=("notion",), ungated_bots=(("discord_bot", bot),))
    )

    assert sources == frozenset(
        {"integration:notion:notion-id", f"channel_bot:discord_bot:{bot}"}
    )


@pytest.mark.asyncio
async def test_an_unreadable_switch_keeps_gating():
    """Fails closed, like every other read in this module: not knowing a switch
    was turned off must never be treated as it having been."""

    class _Boom:
        async def execute(self, _stmt):
            raise RuntimeError("no")

        async def rollback(self):
            return None

    assert await resolve_exempt_sources(_Boom()) == frozenset()


@pytest.mark.asyncio
async def test_the_switch_also_stops_the_conversation_pausing():
    """The half that makes a gated tool usable through a bot at all.

    Exempting the bot's own tools is not enough: an agent asked in Discord to
    update an OpenCTI entity calls a tool the bot does not provide, and this
    surface cannot collect a decision — the router answers with a refusal and
    the turn ends. So a bot switched off runs the whole turn uninterrupted,
    which is what ``run_agent_chat(hitl_off_outcome=...)`` carries.

    Asserted on the router's own source rather than by driving a live Discord
    turn: what needs pinning is that the override is passed at all, and from
    the bot's flag.
    """
    import inspect

    from app.channels import router
    from app.services.agent_chat import run_agent_chat

    assert "hitl_off_outcome" in inspect.signature(run_agent_chat).parameters, (
        "the chat entry point must accept the override the router passes"
    )
    source = inspect.getsource(router.handle_message)
    assert "hitl_off_outcome=" in source, "the router must pass it"
    assert "requires_approval" in source, "and derive it from the bot's switch"


def test_a_bot_uninterrupted_call_is_not_recorded_as_an_ungated_agent():
    """FR-020 asks why a call ran, and the answer has to name the right switch.

    ``auto_run_hitl_off`` means the agent does not gate. An agent that gates on
    the web, on its scheduled runs, and through every other bot is not that —
    so a turn let through by one trusted bot gets its own outcome, exactly as
    the platform switch and the user's allow-all get theirs.
    """
    from app.tools.approval import (
        OUTCOME_BOT_UNINTERRUPTED,
        OUTCOME_HITL_OFF,
        resolve_log_outcome,
    )

    assert OUTCOME_BOT_UNINTERRUPTED != OUTCOME_HITL_OFF

    agent_ungated = resolve_log_outcome(
        "send_discord_bot_message", "call_1", outcomes={}, hitl_enabled=False
    )
    via_bot = resolve_log_outcome(
        "update_opencti_field",
        "call_2",
        outcomes={},
        hitl_enabled=False,
        hitl_off_outcome=OUTCOME_BOT_UNINTERRUPTED,
    )

    assert agent_ungated == OUTCOME_HITL_OFF
    assert via_bot == OUTCOME_BOT_UNINTERRUPTED


def test_a_decision_the_gate_made_still_wins_over_the_bot_reason():
    """The reason only explains calls that ran with no gate installed; anything
    the gate itself decided keeps its own outcome."""
    from app.tools.approval import OUTCOME_BOT_UNINTERRUPTED, resolve_log_outcome

    assert (
        resolve_log_outcome(
            "x",
            "call_3",
            outcomes={"call_3": "paused_for_approval"},
            hitl_enabled=False,
            hitl_off_outcome=OUTCOME_BOT_UNINTERRUPTED,
        )
        == "paused_for_approval"
    )


def test_the_source_carries_the_provider_as_well_as_the_bot():
    """Both halves are load-bearing, and stamping the id alone broke three.

    The gate keys its per-bot switch on the id, because two bots of one provider
    can disagree. The activity trail, the loop's dynamic-tool accounting and the
    evaluation engine all read the *provider* out of the source — and a source
    of ``channel_bot:<id>`` alone silently dropped bot calls out of all three,
    in a change whose stated goal was to give them attribution.
    """
    import inspect

    from app.services import integration_tools

    source = inspect.getsource(integration_tools.load_channel_bot_tools) if hasattr(
        integration_tools, "load_channel_bot_tools"
    ) else inspect.getsource(integration_tools)
    assert 'f"channel_bot:{provider}:{channel_bot_id}"' in source

    from app.tools.agent_loop import _DYNAMIC_TOOL_SOURCE_PREFIXES

    assert "channel_bot:" in _DYNAMIC_TOOL_SOURCE_PREFIXES


@pytest.mark.asyncio
async def test_the_switch_read_is_the_bot_the_agent_is_bound_to():
    """``_get_channel_bot`` picks *a* connected bot of the provider, ordered by
    ``is_company_managed`` — which every channel bot is, so the pick is
    arbitrary among several. The agent's tools come from
    ``channel_config[<channel>].channel_bot_id``.

    Since this override disables approval for the whole turn, reading the
    arbitrary one meant switching off bot A could run bot B's conversations
    unattended, and switching off the intended bot could do nothing.
    """
    from types import SimpleNamespace

    from app.channels.router import _bot_governing_turn

    bound = SimpleNamespace(id=uuid.uuid4(), requires_approval=False)
    arbitrary = SimpleNamespace(id=uuid.uuid4(), requires_approval=True)
    channel = SimpleNamespace(value="slack")
    agent = SimpleNamespace(
        channel_config={"slack": {"channel_bot_id": str(bound.id)}}
    )

    class _DBOne:
        async def scalar(self, _stmt):
            return bound

    chosen = await _bot_governing_turn(_DBOne(), agent, channel, arbitrary)
    assert chosen is bound

    # No bot named for this channel: the provider pick stands, which is the
    # pre-existing behaviour on a single-bot deployment.
    bare = SimpleNamespace(channel_config={})
    assert await _bot_governing_turn(_DBOne(), bare, channel, arbitrary) is arbitrary
