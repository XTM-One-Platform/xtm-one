"""@mentioned agents answer in messaging channels too: the typed
``@key`` grammar, the label each channel's format uses, the deployment switch
that keeps them off unless turned on, the agent's own channel
settings, one agentic action per agent that answers, and the replies stored
as separate attributed messages."""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.channels.base import ChannelType
from app.services import mention_dispatch
from app.services.agent_chat import QUOTA_EXCEEDED_MESSAGE
from app.services.mention_dispatch import (
    ANSWERED_BY_KEY,
    MENTION_FAILED_KEY,
    MENTIONED_AGENT_HANDOVER_TYPES,
    MentionReply,
    MentionTarget,
    TurnMetering,
    agent_mention_key,
    compose_channel_reply,
    mentioned_agent_ids_from_text,
    parse_mention_tokens,
)
from app.services.output_format import CONTENT_FORMAT_META_KEY, SLACK_MRKDWN

A, B, C = (str(uuid.uuid4()) for _ in range(3))


def _target(agent_id, name, *, allowed=True):
    return MentionTarget(agent_id=agent_id, name=name, icon=None, agent=SimpleNamespace(id=agent_id, name=name))


def patch_chat_gate(monkeypatch, *, allowance: int | None = None) -> SimpleNamespace:
    """Run the real chat quota gate (``enforce_agentic_quota_ex``) on faked counters.

    ``charged`` lists the agent id each action was charged for (``None`` for
    the General Assistant) and ``executions`` counts the agentic executions
    recorded; once ``allowance`` actions are charged the quota is exhausted.
    """
    from app.services.quota import QuotaExceededError

    gate = SimpleNamespace(charged=[], executions=0)

    async def check(_db, _uid):
        if allowance is not None and len(gate.charged) >= allowance:
            raise QuotaExceededError("agentic_actions", allowance, len(gate.charged), "monthly")

    async def increment(_uid, period="monthly", db=None, agent_id=None, receipt_token=None):
        gate.charged.append(str(agent_id) if agent_id else None)

    async def record(_source):
        gate.executions += 1

    monkeypatch.setattr("app.services.quota.check_agentic_quota", check)
    monkeypatch.setattr("app.services.quota.increment_agentic_usage", increment)
    monkeypatch.setattr("app.services.agent_chat.record_agentic_execution", record)
    return gate


async def run_through_gate(db, user_id: str, agent_id, **kwargs) -> str | None:
    """What a chat run does before anything else: meet the quota gate with the
    metering its caller asked for. Returns the refusal, or None."""
    from app.services.agent_chat import enforce_agentic_quota_ex

    quota_out = kwargs.get("quota_out")
    refusal, _ = await enforce_agentic_quota_ex(
        db,
        user_id,
        meter_quota=kwargs.get("meter_quota", True),
        agent_id=str(agent_id) if agent_id else None,
        quota_out=quota_out if quota_out is not None else {},
    )
    return refusal


# ---------------------------------------------------------------------------
# Grammar and format
# ---------------------------------------------------------------------------


def test_tokens_follow_the_web_composer_grammar():
    text = "hi @Mail-Desk and @reviewer, mail me at bob@example.com, <@U0123> @mail-desk"
    assert parse_mention_tokens(text) == ["mail-desk", "reviewer", "u0123"]


def test_an_agent_is_mentioned_by_its_slug_else_its_hyphenated_name():
    assert agent_mention_key("mail-desk", "Mail Desk") == "mail-desk"
    assert agent_mention_key(None, "Deal  Desk Agent") == "deal-desk-agent"


@pytest.mark.parametrize(("channel", "marker"), [("slack", "*"), ("google_chat", "*"), ("teams", "**"), ("discord", "**")])
def test_the_combined_reply_labels_each_agent_in_the_channel_format(channel, marker):
    replies = [
        MentionReply(MentionTarget(agent_id="", name=None), "assistant says"),
        MentionReply(MentionTarget(agent_id=B, name="Beta"), "beta says"),
    ]
    assert compose_channel_reply(channel, replies) == f"assistant says\n\n{marker}Beta{marker}\nbeta says"


@pytest.mark.asyncio
async def test_typed_keys_resolve_to_visible_agents_in_mention_order():
    agents = [
        SimpleNamespace(id=uuid.UUID(A), slug="alpha", name="Alpha"),
        SimpleNamespace(id=uuid.UUID(B), slug=None, name="Beta Bot"),
    ]
    with patch.object(mention_dispatch, "visible_agents", AsyncMock(return_value=agents)):
        ids = await mentioned_agent_ids_from_text(
            MagicMock(), user_id=uuid.uuid4(), text="@beta-bot then @alpha, and @someone-else"
        )
    assert ids == [B, A]


@pytest.mark.asyncio
async def test_a_typed_key_is_read_in_the_users_library_for_an_admin_too():
    # A typed key is the channel's picker, and a picker is three-tier for an
    # admin as well: never a colleague's private agent.
    seen: dict = {}

    def fake_query(model, user_id, group_ids, **kwargs):
        seen.update(kwargs, group_ids=group_ids)
        return "library"

    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.scalar = AsyncMock(return_value="admin")
    with (
        patch("app.api.v1.helpers.user_visible_query", side_effect=fake_query),
        patch("app.api.v1.helpers.get_user_group_ids", AsyncMock(return_value=["g1"])),
    ):
        assert await mention_dispatch.visible_agents(db, uuid.uuid4()) == []
    assert seen == {"group_ids": ["g1"]}
    db.execute.assert_awaited_once_with("library")


@pytest.mark.asyncio
async def test_a_key_names_the_agent_that_may_answer_when_several_share_it():
    here = SimpleNamespace(id=uuid.UUID(B), slug="support", name="Support", ok=True)
    elsewhere = SimpleNamespace(id=uuid.UUID(C), slug="support", name="Support", ok=False)
    with patch.object(mention_dispatch, "visible_agents", AsyncMock(return_value=[elsewhere, here])):
        ids = await mentioned_agent_ids_from_text(
            MagicMock(), user_id=uuid.uuid4(), text="@support", admit=lambda a: a.ok
        )
    assert ids == [B]


# ---------------------------------------------------------------------------
# Through the router: once the deployment turns channel mentions on, a
# channel @mention answers only through the mentioned agent's own channel
# settings; any other token is plain text.
# ---------------------------------------------------------------------------

ROUTED_ID = uuid.uuid4()


def _agent(name, *, enabled=True, disable_chat=False, channel="slack", **cfg):
    """An agent; ``cfg`` is its ``channel_config`` entry for ``channel`` (``none``: no config)."""
    config = None if cfg.get("none") else {channel: {"receive_enabled": True, "publish_enabled": True, **cfg}}
    return SimpleNamespace(
        id=ROUTED_ID if name == "Alpha" else uuid.uuid4(),
        name=name,
        slug=name.lower(),
        icon=None,
        enabled=enabled,
        disable_chat=disable_chat,
        channel_config=config,
        available_channel_types=None,
        is_company_managed=True,
    )


class _Result:
    """The routed agent for a lookup by id, every agent for the mention lookup."""

    def __init__(self, routed, agents):
        self._routed = routed
        self._agents = agents

    def scalar_one_or_none(self):
        return self._routed

    def one_or_none(self):
        return (self._routed.name, None)

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._agents))

    def all(self):
        return [(a.id, a.slug, a.name) for a in self._agents]


class _RouterDb:
    """The one session ``handle_message`` and each mentioned run open."""

    def __init__(self, agents):
        self._by_id = {a.id: a for a in agents}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def execute(self, *_args, **_kwargs):
        return _Result(self._by_id[ROUTED_ID], self._by_id.values())

    async def scalar(self, *_args, **_kwargs):
        return "user"

    async def get(self, _model, agent_id):
        return self._by_id.get(agent_id)

    async def commit(self):
        return None

    async def rollback(self):
        return None


def _slack(text, *, channel_type="channel", bot_mentioned=True):
    from app.channels.base import InboundMessage

    return InboundMessage(
        channel=ChannelType.SLACK,
        channel_user_id="U1",
        channel_thread_id="C1:1700000000.000100",
        user_email="bob@example.com",
        text=text,
        metadata={"channel_type": channel_type, "bot_was_mentioned": bot_mentioned},
    )


def _switch(monkeypatch, enabled: bool) -> None:
    """Set ``CHANNEL_AGENT_MENTIONS_ENABLED`` for one test."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "channel_agent_mentions_enabled", enabled)


async def _route(
    monkeypatch,
    inbound,
    mentioned_agents,
    *,
    bot=None,
    identified=True,
    bot_whitelist_passes=True,
    mentions_enabled: bool | None = True,
    run=None,
    stand_in: bool = False,
    history=None,
    general_assistant: str | None = None,
):
    """Run ``handle_message`` end to end: Alpha is the routed agent.

    ``mentions_enabled`` sets the deployment switch (``None``: left as the
    deployment has it); ``run`` replaces the ``run_agent_chat`` fake.
    ``stand_in``: no agent receives on the channel, so the General Assistant
    would answer, and Alpha is the agent linked to it. ``history``:
    the thread's earlier messages, in an unbound conversation that exists.
    ``general_assistant``: no agent receives on the channel and none stands
    in, so the General Assistant answers, with this text.
    """
    from fastapi import HTTPException

    from app.channels import base as channel_base
    from app.channels import router
    from tests.agentic_gate_stub import stub_agentic_gate

    if mentions_enabled is not None:
        _switch(monkeypatch, mentions_enabled)
    # Off the whitelist, routing returns an agent that bypasses it, or none.
    alpha = _agent(
        "Alpha", channel=inbound.channel.value, **({} if bot_whitelist_passes else {"bypass_whitelist": True})
    )
    agents = [alpha, *mentioned_agents]
    db = _RouterDb(agents)
    user = SimpleNamespace(id=uuid.uuid4(), channel_agent_config=None) if identified else None
    bot = bot or SimpleNamespace(
        id=uuid.uuid4(),
        only_mention=False,
        allow_anonymous=True,
        allow_private=True,
        allowed_receive_channels=None,
        is_company_managed=True,
    )

    async def get_user_agent(_db, _user_id, agent_id, *_a, **_k):
        for agent in agents:
            if str(agent.id) == str(agent_id):
                return agent
        raise HTTPException(status_code=404)

    async def run_agent_chat(_db, _user_id, agent, *_args, **kwargs):
        if kwargs.get("quota_out") is not None:
            kwargs["quota_out"].update({"actions_consumed": 1, "reason": "charged"})
        return (f"{agent.name.lower()} says", None)

    async def keep_text(_db, text, *_a, **_k):
        return text

    run = AsyncMock(side_effect=run or run_agent_chat)
    finalize = AsyncMock(return_value=None)
    stub_agentic_gate(monkeypatch)
    monkeypatch.setattr(router, "async_session", lambda: db)
    # Each mentioned agent runs on a session of its own.
    monkeypatch.setattr("app.core.database.async_session", lambda: db)
    monkeypatch.setattr(router, "_get_channel_bot", AsyncMock(return_value=bot))
    monkeypatch.setattr(router, "_bot_allows_receive", lambda _b, _i: bot_whitelist_passes)
    monkeypatch.setattr(router, "_any_agent_bypasses_whitelist", AsyncMock(return_value=True))
    monkeypatch.setattr(router, "_is_thread_active", AsyncMock(return_value=False))
    monkeypatch.setattr(router, "_is_thread_muted", AsyncMock(return_value=False))
    monkeypatch.setattr(router, "resolve_user_by_email", AsyncMock(return_value=user))
    monkeypatch.setattr(router, "resolve_user_by_channel_identity", AsyncMock(return_value=None))
    monkeypatch.setattr(router, "link_channel_identity", AsyncMock())
    conversation = None
    if history is not None:
        from app.tools.base import FileContext

        conversation = SimpleNamespace(id=uuid.uuid4(), agent_id=None)
        monkeypatch.setattr(router, "get_conversation_history", AsyncMock(return_value=list(history)))
        monkeypatch.setattr(
            "app.services.agent_chat.load_conversation_files", AsyncMock(return_value=FileContext())
        )
        monkeypatch.setattr(router, "_compact_history", AsyncMock(side_effect=lambda _db, h, **_k: h))
    monkeypatch.setattr(router, "_find_existing_external_conversation", AsyncMock(return_value=conversation))
    monkeypatch.setattr(router, "_find_bound_conversation", AsyncMock(return_value=None))
    if stand_in or general_assistant is not None:
        monkeypatch.setattr(router, "_resolve_channel_agent", AsyncMock(return_value=None))
        monkeypatch.setattr(router, "_ga_allows_channel", AsyncMock(return_value=True))
        monkeypatch.setattr(
            "app.services.general_assistant_link.general_assistant_stand_in",
            AsyncMock(return_value=alpha if stand_in else None),
        )
        monkeypatch.setattr(
            "app.services.general_chat.run_general_chat",
            AsyncMock(return_value=(general_assistant or "", None)),
        )
    else:
        monkeypatch.setattr(router, "_resolve_channel_agent", AsyncMock(return_value=alpha))
    monkeypatch.setattr(router, "_resolve_channel_agent_anonymous", AsyncMock(return_value=alpha))
    monkeypatch.setattr(router, "_resolve_channel_files", AsyncMock(return_value=[]))
    monkeypatch.setattr(router, "_load_channel_backfill", AsyncMock(return_value=None))
    monkeypatch.setattr(router, "_load_llm_trace_id", AsyncMock(return_value=None))
    monkeypatch.setattr(router, "_finalize_message", finalize)
    monkeypatch.setattr("app.api.v1.helpers.get_user_agent", get_user_agent)
    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.api.v1.platform.is_agentic_ai_licensed", AsyncMock(return_value=True))
    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", run)
    for name in (
        "resolve_channel_file_markers",
        "resolve_channel_presentation_markers",
        "resolve_channel_report_markers",
    ):
        monkeypatch.setattr(channel_base, name, keep_text)
    monkeypatch.setattr(channel_base, "channel_image_scope", AsyncMock(return_value=set()))
    monkeypatch.setattr(channel_base, "resolve_channel_images", lambda text, _url, **_kw: (text, []))

    out = await router.handle_message(inbound)
    return out, run, finalize


def _answered_alone(out, run, finalize):
    """What main does: the routed agent answers, metered as a plain message."""
    assert out.text == "alpha says"
    assert [call.args[2].name for call in run.await_args_list] == ["Alpha"]
    kwargs = run.await_args.kwargs
    # Through the chat gate exactly as on main: nothing about mentions.
    assert kwargs.get("meter_quota", True) is True
    assert kwargs.get("quota_out") is None
    assert "failure_refund_out" not in kwargs
    assert "allowed_handover_types" not in kwargs
    # No agent answers after it, so it is told of none.
    assert kwargs["agents_after"] == []
    assert finalize.await_args.kwargs["mention_replies"] == []
    # Stored as on main: no attribution on the reply; written in Slack's
    # mrkdwn on Slack, and marked so.
    platform = (kwargs.get("context") or {}).get("platform")
    expected = {CONTENT_FORMAT_META_KEY: SLACK_MRKDWN} if platform == "slack" else None
    assert finalize.await_args.kwargs.get("assistant_metadata") == expected


def _gated_run(*, consults: dict | None = None):
    """A ``run_agent_chat`` fake that meets the real quota gate before it runs.

    ``consults`` maps an agent name to the agent it consults mid-run: a nested
    handover, run with ``meter_quota=False`` as ``consult_agent`` does.
    """

    async def run(db, user_id, agent, _history, _text, **kwargs):
        refusal = await run_through_gate(db, user_id, agent.id, **kwargs)
        if refusal:
            return (refusal, None)
        consulted = (consults or {}).get(agent.name)
        if consulted is not None:
            await run(db, user_id, consulted, [], "a question", meter_quota=False)
        return (f"{agent.name.lower()} says", None)

    return run


# ---------------------------------------------------------------------------
# The deployment switch: off unless turned on, and off a channel
# message is answered by its routed agent alone, exactly as on main
# ---------------------------------------------------------------------------

MESSAGING_CHANNELS = [
    ChannelType.SLACK,
    ChannelType.TEAMS,
    ChannelType.WHATSAPP,
    ChannelType.TELEGRAM,
    ChannelType.DISCORD,
    ChannelType.GOOGLE_CHAT,
]

#: A group conversation on each messaging channel: its thread id and metadata.
_GROUP_CONVERSATION = {
    ChannelType.SLACK: ("C1:1700000000.000100", {"channel_type": "channel"}),
    ChannelType.TEAMS: ("19:general@thread.tacv2", {"conversation_type": "channel"}),
    ChannelType.WHATSAPP: ("120363000000000000@g.us", {"chat_type": "group"}),
    ChannelType.TELEGRAM: ("-1001234567890", {"chat_type": "group"}),
    ChannelType.DISCORD: ("987654321098765432", {"is_dm": False}),
    ChannelType.GOOGLE_CHAT: ("spaces/AAAAxyz", {"space_type": "SPACE"}),
}


def _group_message(channel: ChannelType, text: str):
    """An identified user's message in a group conversation, the bot mentioned."""
    from app.channels.base import InboundMessage

    thread_id, metadata = _GROUP_CONVERSATION[channel]
    return InboundMessage(
        channel=channel,
        channel_user_id="U1",
        channel_thread_id=thread_id,
        user_email="bob@example.com",
        text=text,
        metadata={**metadata, "bot_was_mentioned": True},
    )


def test_channel_mentions_stay_off_unless_the_deployment_turns_them_on():
    from app.core.config import Settings

    assert Settings.model_fields["channel_agent_mentions_enabled"].default is False


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", MESSAGING_CHANNELS, ids=lambda c: c.value)
async def test_worked_on_main_a_message_naming_another_agent_of_the_channel_gets_the_routed_reply_only(
    monkeypatch, channel
):
    """Nothing configured: Beta receives and publishes on this channel, set up
    for routing, and the message names it - the routed agent still answers
    alone, charged one action, as on main."""
    from app.core.config import settings

    assert getattr(settings, "channel_agent_mentions_enabled", False) is False
    gate = patch_chat_gate(monkeypatch)
    out, run, finalize = await _route(
        monkeypatch,
        _group_message(channel, "@beta what do you think?"),
        [_agent("Beta", channel=channel.value)],
        mentions_enabled=None,
        run=_gated_run(),
    )
    _answered_alone(out, run, finalize)
    assert gate.charged == [str(ROUTED_ID)]


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", MESSAGING_CHANNELS, ids=lambda c: c.value)
async def test_turned_on_a_mention_of_an_agent_set_up_on_the_channel_answers(monkeypatch, channel):
    out, run, _ = await _route(
        monkeypatch,
        _group_message(channel, "@beta what do you think?"),
        [_agent("Beta", channel=channel.value)],
    )
    bold = "**" if channel in (ChannelType.TEAMS, ChannelType.DISCORD) else "*"
    assert out.text == f"{bold}Alpha{bold}\nalpha says\n\n{bold}Beta{bold}\nbeta says"
    assert [call.args[2].name for call in run.await_args_list] == ["Alpha", "Beta"]


@pytest.mark.asyncio
async def test_the_switch_is_read_before_any_mention_lookup(monkeypatch):
    from app.channels import router

    _switch(monkeypatch, False)
    lookup = AsyncMock(return_value=[B])
    monkeypatch.setattr(mention_dispatch, "mentioned_agent_ids_from_text", lookup)
    mentioned = await router._read_channel_mentions(
        MagicMock(),
        _slack("@beta can you help?"),
        SimpleNamespace(id=uuid.uuid4()),
        channel_bot=None,
        bot_whitelist_passed=True,
        is_external_channel=True,
        is_anonymous=False,
    )
    assert mentioned == []
    lookup.assert_not_awaited()


# ---------------------------------------------------------------------------
# Metering: every agent that answers is one agentic action, charged
# before it runs through the chat gate; a handover inside a run is not
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_agent_answering_a_channel_message_is_charged_one_action(monkeypatch):
    """Alpha routed, Beta and Gamma named: three runs, three actions and three
    executions; the agent Beta consults mid-run rides Beta's action."""
    gate = patch_chat_gate(monkeypatch)
    beta, gamma, delta = _agent("Beta"), _agent("Gamma"), _agent("Delta")
    out, run, _ = await _route(
        monkeypatch,
        _slack("@beta and @gamma, what do you think?"),
        [beta, gamma],
        run=_gated_run(consults={"Beta": delta}),
    )
    assert out.text == "*Alpha*\nalpha says\n\n*Beta*\nbeta says\n\n*Gamma*\ngamma says"
    assert gate.charged == [str(ROUTED_ID), str(beta.id), str(gamma.id)]
    assert gate.executions == 3
    # Each run applies its own credit-back, as any chat turn does.
    assert all("failure_refund_out" not in call.kwargs for call in run.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [pytest.param("hello there", id="no-mention"), pytest.param("@alpha hello there", id="routed-agent-only")],
)
async def test_a_channel_message_one_agent_answers_is_charged_as_on_main(monkeypatch, text):
    gate = patch_chat_gate(monkeypatch)
    out, run, finalize = await _route(monkeypatch, _slack(text), [_agent("Beta")], run=_gated_run())
    _answered_alone(out, run, finalize)
    assert gate.charged == [str(ROUTED_ID)]
    assert gate.executions == 1


# ---------------------------------------------------------------------------
# The agent standing in for the General Assistant leaves the
# conversation unbound, and the turn is still its own
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_stand_in_named_in_the_message_it_answers_answers_once(monkeypatch):
    gate = patch_chat_gate(monkeypatch)
    out, run, finalize = await _route(
        monkeypatch, _slack("@alpha hello there"), [_agent("Beta")], run=_gated_run(), stand_in=True
    )
    _answered_alone(out, run, finalize)
    assert gate.charged == [str(ROUTED_ID)]
    # Unbound, so the next message is routed again; the activity names it.
    assert finalize.await_args.kwargs["target_agent_id"] is None
    assert finalize.await_args.kwargs["answering_agent_id"] == ROUTED_ID


@pytest.mark.asyncio
async def test_the_stand_ins_reply_is_its_own_before_a_mentioned_agents(monkeypatch):
    gate = patch_chat_gate(monkeypatch)
    beta = _agent("Beta")
    out, run, finalize = await _route(
        monkeypatch, _slack("@alpha and @beta, what do you think?"), [beta], run=_gated_run(), stand_in=True
    )
    assert out.text == "*Alpha*\nalpha says\n\n*Beta*\nbeta says"
    assert [call.args[2].name for call in run.await_args_list] == ["Alpha", "Beta"]
    assert gate.charged == [str(ROUTED_ID), str(beta.id)]
    assert finalize.await_args.kwargs["assistant_metadata"][ANSWERED_BY_KEY]["agent_name"] == "Alpha"


@pytest.mark.asyncio
async def test_the_thread_is_compacted_with_the_stand_ins_memory_settings(monkeypatch):
    from app.channels import router
    from app.llm.base import ChatMessage

    earlier = [ChatMessage(role="user", content="earlier"), ChatMessage(role="assistant", content="answer")]
    await _route(monkeypatch, _slack("hello there"), [], stand_in=True, history=earlier)
    assert router._compact_history.await_args.kwargs["agent_id"] == ROUTED_ID


# ---------------------------------------------------------------------------
# The markup a stored reply is written in: an agent's run on Slack
# writes Slack's mrkdwn and its reply is marked so, for the web to convert it;
# every other reply is the Markdown the web renders
# ---------------------------------------------------------------------------


@pytest.fixture
def finished(monkeypatch):
    """The ``done`` frames ``ChannelTurn.finish`` was asked for."""
    from app.services.channel_continuation import ChannelTurn

    calls: list[dict] = []
    monkeypatch.setattr(ChannelTurn, "finish", lambda _self, **kwargs: calls.append(kwargs))
    return calls


@pytest.mark.asyncio
async def test_an_agents_slack_reply_is_stored_marked_as_mrkdwn_and_its_mentions_too(monkeypatch, finished):
    _out, _run, finalize = await _route(monkeypatch, _slack("@beta what do you think?"), [_agent("Beta")])
    kwargs = finalize.await_args.kwargs
    assert kwargs["assistant_metadata"][CONTENT_FORMAT_META_KEY] == SLACK_MRKDWN
    assert kwargs["assistant_metadata"][ANSWERED_BY_KEY]["agent_name"] == "Alpha"
    assert [r.metadata().get(CONTENT_FORMAT_META_KEY) for r in kwargs["mention_replies"]] == [SLACK_MRKDWN]
    assert finished[-1]["content_format"] == SLACK_MRKDWN


@pytest.mark.asyncio
async def test_a_note_written_in_place_of_a_mentioned_agent_is_not_marked(monkeypatch):
    async def run(_db, _user_id, agent, *_args, **_kwargs):
        if agent.name == "Beta":
            raise RuntimeError("the model is down")
        return ("alpha says", None)

    _out, _run, finalize = await _route(monkeypatch, _slack("@beta what do you think?"), [_agent("Beta")], run=run)
    (note,) = finalize.await_args.kwargs["mention_replies"]
    assert note.failed
    assert CONTENT_FORMAT_META_KEY not in note.metadata()


@pytest.mark.asyncio
async def test_a_teams_reply_is_markdown_and_stored_unmarked(monkeypatch, finished):
    _out, _run, finalize = await _route(
        monkeypatch,
        _group_message(ChannelType.TEAMS, "@beta what do you think?"),
        [_agent("Beta", channel="teams")],
    )
    kwargs = finalize.await_args.kwargs
    assert CONTENT_FORMAT_META_KEY not in (kwargs["assistant_metadata"] or {})
    assert [CONTENT_FORMAT_META_KEY in r.metadata() for r in kwargs["mention_replies"]] == [False]
    assert finished[-1]["content_format"] is None


@pytest.mark.asyncio
async def test_the_general_assistants_slack_reply_is_markdown_and_stored_unmarked(monkeypatch, finished):
    """The General Assistant writes Markdown; its Slack adapter converts it
    only on its way out, so the web renders the stored text as it is."""
    out, run, finalize = await _route(
        monkeypatch, _slack("hello there"), [], general_assistant="The fix is *tentative*."
    )
    assert out.text == "The fix is *tentative*."
    run.assert_not_awaited()
    assert finalize.await_args.kwargs["assistant_text"] == "The fix is *tentative*."
    assert not finalize.await_args.kwargs.get("assistant_metadata")
    assert finished[-1]["content_format"] is None


@pytest.mark.asyncio
async def test_a_mentioned_agent_the_quota_refuses_says_so_and_nobody_after_it_is_asked(monkeypatch):
    """Two actions left: Alpha and Beta are charged, Gamma's charge is refused
    with the chat gate's own sentence, and Delta is not asked."""
    gate = patch_chat_gate(monkeypatch, allowance=2)
    beta, gamma, delta = _agent("Beta"), _agent("Gamma"), _agent("Delta")
    out, run, _ = await _route(
        monkeypatch,
        _slack("@beta @gamma @delta what do you think?"),
        [beta, gamma, delta],
        run=_gated_run(),
    )
    assert out.text == f"*Alpha*\nalpha says\n\n*Beta*\nbeta says\n\n*Gamma*\n{QUOTA_EXCEEDED_MESSAGE}"
    assert [call.args[2].name for call in run.await_args_list] == ["Alpha", "Beta", "Gamma"]
    assert gate.charged == [str(ROUTED_ID), str(beta.id)]


@pytest.mark.asyncio
async def test_each_channel_agent_but_the_last_reads_the_agents_answering_after_it(monkeypatch):
    """As on the web chat: the routed agent reads that Beta then Gamma
    answer after it, Beta that Gamma does, Gamma nothing."""
    out, run, _ = await _route(
        monkeypatch, _slack("@beta and @gamma, what do you think?"), [_agent("Beta"), _agent("Gamma")]
    )

    assert out.text == "*Alpha*\nalpha says\n\n*Beta*\nbeta says\n\n*Gamma*\ngamma says"
    assert [(call.args[2].name, [t.name for t in call.kwargs["agents_after"]]) for call in run.await_args_list] == [
        ("Alpha", ["Beta", "Gamma"]),
        ("Beta", ["Gamma"]),
        ("Gamma", []),
    ]


@pytest.mark.asyncio
async def test_the_general_assistant_reads_the_agents_answering_after_it_on_a_channel(monkeypatch):
    from app.services import general_chat

    await _route(monkeypatch, _slack("@beta what do you think?"), [_agent("Beta")], general_assistant="ga says")

    assert [t.name for t in general_chat.run_general_chat.await_args.kwargs["agents_after"]] == ["Beta"]


@pytest.mark.asyncio
async def test_a_failed_line_up_lookup_leaves_the_routed_agent_without_one(monkeypatch):
    from app.channels import router

    lookup = AsyncMock(side_effect=RuntimeError("database gone"))
    monkeypatch.setattr(mention_dispatch, "resolve_mention_targets", lookup)

    after = await router._channel_mention_targets(
        MagicMock(), SimpleNamespace(id=uuid.uuid4()), [B], ROUTED_ID, "en"
    )

    assert after == []
    lookup.assert_awaited_once()


def _silent_alpha():
    """Alpha leaves the message to the agents the user named: ``[NO_RESPONSE]``."""

    async def run(_db, _user_id, agent, *_args, **kwargs):
        if kwargs.get("quota_out") is not None:
            kwargs["quota_out"].update({"actions_consumed": 1, "reason": "charged"})
        return ("[NO_RESPONSE]" if agent.name == "Alpha" else f"{agent.name.lower()} says", None)

    return run


@pytest.mark.asyncio
async def test_a_routed_agent_that_leaves_the_message_to_the_named_agents_does_not_silence_them(monkeypatch):
    """Told Beta answers after it, Alpha may have nothing to add -
    Beta still answers, alone in the message, and only its reply is stored."""
    beta = _agent("Beta")
    out, run, finalize = await _route(monkeypatch, _slack("@beta what do you think?"), [beta], run=_silent_alpha())

    assert out.suppress is False
    assert out.text == "*Beta*\nbeta says"
    assert [call.args[2].name for call in run.await_args_list] == ["Alpha", "Beta"]
    # Beta did not read Alpha's silence as a reply.
    assert [m.content for m in run.await_args_list[1].args[3]] == []
    stored = finalize.await_args.kwargs
    assert (stored["suppress"], stored["store_reply"], stored["assistant_text"]) == (False, False, "beta says")
    assert [r.text for r in stored["mention_replies"]] == ["beta says"]
    # The activity entry names the agent whose reply it shows, not the silent one.
    assert stored["answering_agent_id"] == beta.id


@pytest.mark.asyncio
async def test_when_the_routed_agent_and_a_named_agent_stay_silent_the_next_named_agent_answers_alone(monkeypatch):
    """Alpha and Beta both answer ``[NO_RESPONSE]``, Gamma answers -
    the message holds Gamma's section only, only its reply is stored, and the
    activity entry names it."""

    async def run(_db, _user_id, agent, *_args, **kwargs):
        if kwargs.get("quota_out") is not None:
            kwargs["quota_out"].update({"actions_consumed": 1, "reason": "charged"})
        return ("gamma says" if agent.name == "Gamma" else "[NO_RESPONSE]", None)

    gamma = _agent("Gamma")
    out, run_mock, finalize = await _route(
        monkeypatch, _slack("@beta @gamma what do you think?"), [_agent("Beta"), gamma], run=run
    )

    assert out.suppress is False
    assert out.text == "*Gamma*\ngamma says"
    assert [call.args[2].name for call in run_mock.await_args_list] == ["Alpha", "Beta", "Gamma"]
    stored = finalize.await_args.kwargs
    assert (stored["suppress"], stored["store_reply"], stored["assistant_text"]) == (False, False, "gamma says")
    assert [(r.target.agent_id, r.text) for r in stored["mention_replies"]] == [(str(gamma.id), "gamma says")]
    assert stored["answering_agent_id"] == gamma.id


@pytest.mark.asyncio
async def test_a_routed_agent_silent_with_no_agent_named_stays_silent(monkeypatch):
    out, run, finalize = await _route(
        monkeypatch, _slack("what do you think?"), [_agent("Beta")], run=_silent_alpha()
    )

    assert out.suppress is True
    assert [call.args[2].name for call in run.await_args_list] == ["Alpha"]
    stored = finalize.await_args.kwargs
    assert (stored["suppress"], stored["store_reply"], stored["mention_replies"]) == (True, True, [])


@pytest.mark.asyncio
async def test_when_every_agent_stays_silent_the_message_gets_no_reply(monkeypatch):
    async def run(_db, _user_id, agent, *_args, **kwargs):
        if kwargs.get("quota_out") is not None:
            kwargs["quota_out"].update({"actions_consumed": 1, "reason": "charged"})
        return ("[NO_RESPONSE]", None)

    out, _run, finalize = await _route(monkeypatch, _slack("@beta what do you think?"), [_agent("Beta")], run=run)

    assert out.suppress is True
    assert finalize.await_args.kwargs["mention_replies"] == []


@pytest.mark.asyncio
async def test_only_the_named_agents_replies_are_stored_when_the_routed_agent_was_silent():
    from app.channels import router

    beta_message = SimpleNamespace(id=uuid.uuid4())
    stored = AsyncMock(return_value=beta_message)
    db = MagicMock()
    db.commit = AsyncMock()
    out: dict = {}
    beta = MentionReply(MentionTarget(agent_id=B, name="Beta"), "beta says")
    with patch.object(router, "store_message", stored):
        await router._finalize_message(
            db,
            _inbound(),
            user=None,
            conv=SimpleNamespace(id=uuid.uuid4()),
            target_agent_id=uuid.UUID(A),
            is_anonymous=False,
            is_external_channel=False,
            suppress=False,
            assistant_text="beta says",
            mention_replies=[beta],
            store_reply=False,
            stored_out=out,
        )

    assert [c.kwargs["content"] for c in stored.await_args_list] == ["beta says"]
    assert out == {"assistant_message_id": str(beta_message.id)}


@pytest.mark.asyncio
async def test_a_mention_of_an_agent_enabled_on_the_channel_answers(monkeypatch):
    out, run, finalize = await _route(monkeypatch, _slack("@beta what do you think?"), [_agent("Beta")])

    assert out.text == "*Alpha*\nalpha says\n\n*Beta*\nbeta says"
    assert [call.args[2].name for call in run.await_args_list] == ["Alpha", "Beta"]
    assert [r.text for r in finalize.await_args.kwargs["mention_replies"]] == ["beta says"]
    # The routed agent's reply is the turn's answer: the activity entry names it.
    assert finalize.await_args.kwargs["answering_agent_id"] == ROUTED_ID


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "beta",
    [
        pytest.param(dict(none=True), id="no-channel-config"),
        pytest.param(dict(receive_enabled=False), id="receive-off"),
        pytest.param(dict(receive_channels=[{"id": "C999"}]), id="other-channel"),
        pytest.param(dict(publish_enabled=False), id="publish-off"),
        pytest.param(dict(only_mention=True, bot_mentioned=False), id="agent-only-mention"),
        pytest.param(dict(enabled=False), id="disabled"),
        pytest.param(dict(disable_chat=True), id="disable-chat"),
    ],
)
async def test_a_mention_of_an_agent_that_cannot_answer_there_is_plain_text(monkeypatch, beta):
    bot_mentioned = beta.pop("bot_mentioned", True)
    flags = {k: beta.pop(k) for k in ("enabled", "disable_chat") if k in beta}
    out, run, finalize = await _route(
        monkeypatch,
        _slack("@beta what do you think?", bot_mentioned=bot_mentioned),
        [_agent("Beta", **flags, **beta)],
    )
    _answered_alone(out, run, finalize)


@pytest.mark.asyncio
async def test_a_mention_the_bot_whitelist_refuses_is_plain_text(monkeypatch):
    """The bot whitelist failed and only Alpha bypasses it: Beta cannot answer."""
    out, run, finalize = await _route(
        monkeypatch,
        _slack("@beta what do you think?"),
        [_agent("Beta")],
        bot_whitelist_passes=False,
    )
    # Alpha is the conversation's agent here: it answers as on main.
    _answered_alone(out, run, finalize)


@pytest.mark.asyncio
async def test_a_mention_in_a_dm_the_bot_keeps_private_off_is_plain_text(monkeypatch):
    """Beta listens on #support only; the bot's allow_private=False overrides its own."""
    bot = SimpleNamespace(
        id=uuid.uuid4(),
        only_mention=False,
        allow_anonymous=True,
        allow_private=False,
        allowed_receive_channels=None,
        is_company_managed=True,
    )
    out, run, finalize = await _route(
        monkeypatch,
        _slack("@beta what do you think?", channel_type="im"),
        [_agent("Beta", receive_channels=[{"id": "C999"}], allow_private=True)],
        bot=bot,
    )
    _answered_alone(out, run, finalize)


@pytest.mark.asyncio
async def test_a_token_naming_no_agent_is_plain_text(monkeypatch):
    out, run, finalize = await _route(monkeypatch, _slack("ping @sales and @bob"), [_agent("Beta")])
    _answered_alone(out, run, finalize)


@pytest.mark.asyncio
async def test_an_anonymous_senders_mention_is_plain_text(monkeypatch):
    """The anonymous routing answers with the one agent it picks, as on main."""
    out, run, finalize = await _route(
        monkeypatch,
        _slack("@beta what do you think?"),
        [_agent("Beta", allow_anonymous=True)],
        identified=False,
    )
    assert out.text == "alpha says"
    assert [call.args[2].name for call in run.await_args_list] == ["Alpha"]
    assert run.await_args.args[1] == "anonymous"
    assert run.await_args.kwargs["agents_after"] == []
    assert finalize.await_args.kwargs["mention_replies"] == []


@pytest.mark.asyncio
async def test_a_message_without_tokens_runs_no_query():
    db = MagicMock()
    db.execute = AsyncMock()
    assert await mentioned_agent_ids_from_text(db, user_id=uuid.uuid4(), text="no mention here") == []
    db.execute.assert_not_called()


# ---------------------------------------------------------------------------
# The router helper
# ---------------------------------------------------------------------------


def _inbound(text="@beta what do you think?"):
    from app.channels.base import InboundMessage

    return InboundMessage(
        channel=ChannelType.SLACK,
        channel_user_id="U1",
        channel_thread_id="T1",
        text=text,
    )


OWN_SESSION = object()


def _charged() -> TurnMetering:
    """The routed agent's run met the quota gate and was charged."""
    return TurnMetering(quota={"actions_consumed": 1, "reason": "charged"})


async def _answer(
    targets,
    *,
    runs,
    allowed=True,
    first_reply="alpha says",
    metering=None,
    first_failed=False,
    bot_whitelist_passed=True,
    allows_channel=None,
    run=None,
    file_context=None,
):
    from app.channels import router

    run_agent_chat = run or AsyncMock(side_effect=[(text, None) for text in runs])

    async def own_session(agent, run):
        return await run(OWN_SESSION, agent)

    with (
        patch.object(mention_dispatch, "run_on_own_session", own_session),
        patch.object(mention_dispatch, "resolve_mention_targets", AsyncMock(return_value=targets)),
        patch.object(
            mention_dispatch,
            "conversation_agent_target",
            AsyncMock(return_value=MentionTarget(agent_id=A, name="Alpha")),
        ),
        patch("app.services.agent_chat.run_agent_chat", run_agent_chat),
        patch.object(
            router,
            "_answers_channel_mention",
            allows_channel or MagicMock(return_value=allowed),
        ),
    ):
        result = await router._answer_channel_mentions(
            MagicMock(),
            _inbound(),
            user=SimpleNamespace(id=uuid.uuid4()),
            channel_bot=None,
            answering_agent_id=uuid.UUID(A),
            first_reply=first_reply,
            history=[],
            message_context={"platform": "slack", "bot_was_mentioned": False},
            channel_files=None,
            conversation_id=None,
            mentioned=[t.agent_id for t in targets],
            metering=metering if metering is not None else _charged(),
            bot_whitelist_passed=bot_whitelist_passed,
            first_failed=first_failed,
            file_context=file_context,
        )
    return result, run_agent_chat


def _ref(file_id: str):
    from app.llm.base import FileRef

    return FileRef(file_id=file_id, filename=f"{file_id}.csv", content_type="text/csv", size=1, s3_object_name=file_id)


@pytest.mark.asyncio
async def test_a_mentioned_agent_works_in_the_turns_files_and_its_reply_keeps_what_it_produced():
    from app.tools.base import FileContext

    context = FileContext()
    context.add(_ref("routed-file"))  # the routed agent's, produced before

    async def beta(*_a, **kwargs):
        assert kwargs["file_context"] is context
        kwargs["file_context"].add(_ref("beta-file"))
        return ("beta says", None)

    (replies, _, _), _ = await _answer(
        [_target(B, "Beta")], runs=[], run=AsyncMock(side_effect=beta), file_context=context
    )
    assert [r.file_ids for r in replies] == [["beta-file"]]


@pytest.mark.asyncio
async def test_a_mentioned_agent_answers_after_the_conversation_agent_in_one_message():
    (replies, first_meta, text), run = await _answer([_target(B, "Beta")], runs=["beta says"])

    assert [(r.target.name, r.text) for r in replies] == [("Beta", "beta says")]
    assert first_meta[ANSWERED_BY_KEY]["agent_name"] == "Alpha"
    assert text == "*Alpha*\nalpha says\n\n*Beta*\nbeta says"
    kwargs = run.await_args.kwargs
    # Its own agentic action, through the chat gate, and no transfer.
    assert kwargs.get("meter_quota", True) is True
    assert kwargs["quota_out"] == {} and "failure_refund_out" not in kwargs
    assert kwargs["allowed_handover_types"] == MENTIONED_AGENT_HANDOVER_TYPES
    # The user addressed the agent by name: not a silent group-channel message.
    assert kwargs["context"]["bot_was_mentioned"] is True
    # It read the conversation agent's reply, labelled.
    assert run.await_args.args[3][-1].content == "[Reply from Alpha]\n\nalpha says"
    # It ran on a session of its own, never the channel request's.
    assert run.await_args.args[0] is OWN_SESSION


@pytest.mark.asyncio
async def test_an_agent_that_can_no_longer_answer_there_is_dropped_without_a_note():
    """Re-read after the routed agent ran: plain text, like any other token."""
    (replies, meta, text), run = await _answer([_target(B, "Beta")], runs=[], allowed=False)
    assert (replies, meta, text) == ([], None, None)
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_per_message_limit_still_leaves_its_note():
    capped = MentionTarget(agent_id=C, name="Gamma", refusal="Gamma did not answer: at most 5.", over_limit=True)
    (replies, _, _), _ = await _answer([_target(B, "Beta"), capped], runs=["beta says"])
    assert [(r.target.name, r.failed) for r in replies] == [("Beta", False), ("Gamma", True)]


@pytest.mark.asyncio
async def test_an_agent_choosing_silence_adds_nothing():
    (replies, meta, text), _ = await _answer([_target(B, "Beta")], runs=["[NO_RESPONSE]"])
    assert (replies, meta, text) == ([], None, None)


@pytest.mark.asyncio
async def test_no_other_agent_mentioned_changes_nothing():
    (replies, meta, text), run = await _answer([], runs=[])
    assert (replies, meta, text) == ([], None, None)
    run.assert_not_awaited()


# ---------------------------------------------------------------------------
# Who may answer, and on which charge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_mentioned_agent_is_held_to_the_bots_receive_whitelist():
    allows = MagicMock(return_value=True)
    await _answer([_target(B, "Beta")], runs=["beta says"], bot_whitelist_passed=False, allows_channel=allows)
    assert allows.call_args.kwargs["bot_whitelist_passed"] is False


def test_an_agent_without_bypass_whitelist_is_refused_where_the_bot_whitelist_failed():
    from app.channels import router

    def agent(**slack):
        return SimpleNamespace(channel_config={"slack": {"receive_enabled": True, **slack}}, available_channel_types=None)

    inbound = _inbound()
    assert router._agent_allows_channel(agent(), ChannelType.SLACK, inbound, None, bot_whitelist_passed=False) is False
    assert (
        router._agent_allows_channel(
            agent(bypass_whitelist=True), ChannelType.SLACK, inbound, None, bot_whitelist_passed=False
        )
        is True
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["quota_exceeded", "quota_unavailable"])
async def test_a_message_the_quota_refused_asks_no_mentioned_agent(reason):
    metering = TurnMetering(quota={"actions_consumed": 0, "reason": reason})
    (replies, meta, text), run = await _answer([_target(B, "Beta")], runs=["beta says"], metering=metering)
    assert (replies, meta, text) == ([], None, None)
    run.assert_not_awaited()


def _metering_run(*, reason="charged"):
    """A ``run_agent_chat`` that meets the quota gate when it is metered."""

    async def run(*args, **kwargs):
        if kwargs.get("meter_quota", True):
            kwargs["quota_out"].update({"actions_consumed": int(reason == "charged"), "reason": reason})
            if reason != "charged":
                return ("You have reached your agentic execution quota.", None)
        return (f"{args[2].name} says", None)

    return AsyncMock(side_effect=run)


@pytest.mark.asyncio
async def test_a_routed_agent_that_never_ran_is_not_charged_and_each_mentioned_agent_is():
    metering = TurnMetering()
    run = _metering_run()
    (replies, _, _), _ = await _answer(
        [_target(B, "Beta"), _target(C, "Gamma")], runs=[], metering=metering, first_failed=True, run=run
    )
    assert [r.text for r in replies] == ["Beta says", "Gamma says"]
    first, second = (call.kwargs for call in run.await_args_list)
    # Each mentioned agent is its own action, reported on its own outcome.
    assert first["quota_out"] == second["quota_out"] == {"actions_consumed": 1, "reason": "charged"}
    assert first["quota_out"] is not second["quota_out"]
    assert metering.quota == {}


@pytest.mark.asyncio
async def test_a_refused_charge_asks_nobody_after_it():
    metering = TurnMetering()
    run = _metering_run(reason="quota_exceeded")
    (replies, _, text), _ = await _answer(
        [_target(B, "Beta"), _target(C, "Gamma")], runs=[], metering=metering, first_failed=True, run=run
    )
    assert [(r.target.name, r.text, r.failed) for r in replies] == [
        ("Beta", "You have reached your agentic execution quota.", True)
    ]
    assert run.await_count == 1
    assert "Gamma" not in text


@pytest.mark.asyncio
async def test_a_failed_routed_agent_is_a_note_the_mentioned_agents_do_not_read():
    error = "I encountered an error while processing your message. Please try again."
    (replies, first_meta, _), run = await _answer(
        [_target(B, "Beta")], runs=["beta says"], first_reply=error, first_failed=True
    )
    assert first_meta[MENTION_FAILED_KEY] is True
    # Beta read the thread, not the error as Alpha's words.
    assert run.await_args.args[3] == []
    assert replies[0].text == "beta says"


@pytest.mark.asyncio
async def test_a_failing_mentioned_agent_leaves_a_note_and_the_next_one_answers_on_its_own_charge():
    """A failed run credits its own action back inside ``run_agent_chat``, as
    any chat turn does; nothing is deferred to the rest of the message."""
    run = AsyncMock(side_effect=[RuntimeError("provider down"), ("gamma says", None)])
    (replies, _, _), _ = await _answer([_target(B, "Beta"), _target(C, "Gamma")], runs=[], run=run)
    assert [(r.target.name, r.failed) for r in replies] == [("Beta", True), ("Gamma", False)]
    for call in run.await_args_list:
        assert call.kwargs.get("meter_quota", True) is True
        assert "failure_refund_out" not in call.kwargs


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_reply_is_stored_as_its_own_attributed_message():
    from app.channels import router

    stored = AsyncMock()
    db = MagicMock()
    db.commit = AsyncMock()
    conv = SimpleNamespace(id=uuid.uuid4())
    beta_trace = uuid.uuid4()
    beta = MentionReply(MentionTarget(agent_id=B, name="Beta"), "beta says", trace={"llm_trace_id": str(beta_trace)})
    with patch.object(router, "store_message", stored):
        await router._finalize_message(
            db,
            _inbound(),
            user=None,
            conv=conv,
            target_agent_id=uuid.UUID(A),
            is_anonymous=False,
            is_external_channel=False,
            suppress=False,
            assistant_text="alpha says",
            assistant_metadata={ANSWERED_BY_KEY: {"agent_id": A, "agent_name": "Alpha"}},
            mention_replies=[beta],
        )

    calls = [(c.kwargs["content"], json.loads(c.kwargs["metadata_json"])) for c in stored.await_args_list]
    assert calls == [
        ("alpha says", {ANSWERED_BY_KEY: {"agent_id": A, "agent_name": "Alpha"}}),
        ("beta says", {ANSWERED_BY_KEY: {"agent_id": B, "agent_name": "Beta"}}),
    ]
    # Each reply carries the trace of the run that wrote it.
    assert stored.await_args_list[1].kwargs["llm_trace_id"] == beta_trace


@pytest.mark.asyncio
async def test_each_reply_shows_the_files_its_own_run_produced():
    from app.channels import router

    alpha_msg, beta_msg = SimpleNamespace(id=uuid.uuid4()), SimpleNamespace(id=uuid.uuid4())
    stored = AsyncMock(side_effect=[alpha_msg, beta_msg])
    attach = AsyncMock(return_value=[])
    db = MagicMock()
    db.commit = AsyncMock()
    beta = MentionReply(MentionTarget(agent_id=B, name="Beta"), "beta says", file_ids=["beta-file"])
    with patch.object(router, "store_message", stored), patch.object(router, "_attach_file_ids_to_message", attach):
        await router._finalize_message(
            db,
            _inbound(),
            user=None,
            conv=SimpleNamespace(id=uuid.uuid4()),
            target_agent_id=uuid.UUID(A),
            is_anonymous=False,
            is_external_channel=False,
            suppress=False,
            assistant_text="alpha says",
            turn_files=[_ref("alpha-file")],
            mention_replies=[beta],
        )
    assert [(c.args[2], c.args[3]) for c in attach.await_args_list] == [
        (alpha_msg, ["alpha-file"]),
        (beta_msg, ["beta-file"]),
    ]


@pytest.mark.asyncio
async def test_the_one_channel_message_links_the_files_each_agent_produced(monkeypatch):
    alpha_file, beta_file = str(uuid.uuid4()), str(uuid.uuid4())

    async def run(_db, _uid, agent, *_a, **kwargs):
        if kwargs.get("quota_out") is not None:
            kwargs["quota_out"].update({"actions_consumed": 1, "reason": "charged"})
        kwargs["file_context"].add(_ref(alpha_file if agent.name == "Alpha" else beta_file))
        return (f"{agent.name.lower()} says", None)

    out, _, finalize = await _route(monkeypatch, _slack("@beta what do you think?"), [_agent("Beta")], run=run)

    assert out.text == f"*Alpha*\nalpha says\n\n*Beta*\nbeta says\n\n[[FILE:{alpha_file}]]\n[[FILE:{beta_file}]]"
    kwargs = finalize.await_args.kwargs
    assert [r.file_id for r in kwargs["turn_files"]] == [alpha_file]
    assert [r.file_ids for r in kwargs["mention_replies"]] == [[beta_file]]


@pytest.mark.asyncio
async def test_a_reply_without_mentions_is_stored_as_before():
    from app.channels import router

    stored = AsyncMock()
    db = MagicMock()
    db.commit = AsyncMock()
    with patch.object(router, "store_message", stored):
        await router._finalize_message(
            db,
            _inbound(),
            user=None,
            conv=SimpleNamespace(id=uuid.uuid4()),
            target_agent_id=None,
            is_anonymous=False,
            is_external_channel=False,
            suppress=False,
            assistant_text="hello",
        )
    assert stored.await_count == 1
    assert stored.await_args.kwargs["metadata_json"] is None
