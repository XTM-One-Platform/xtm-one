"""The agent linked to the General Assistant answers in its place.

Settings > AI and models > General Assistant > "Link to Agent" said the General Assistant then
inherits the agent's model, persona, tools and configuration, and nothing read
the link at run time. These pin the one rule (``services/general_assistant_link``)
and the three places that hand the agent the turn, plus the defaults a new
General Assistant ships with and the migration that brings them to the
platforms that already have one.
"""

import contextlib
import importlib.util
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy.dialects import postgresql

from app.api.v1 import chat as chat_api
from app.api.v1.schemas.settings import GeneralAssistantConfigRequest
from app.api.v1.settings import update_general_assistant_config
from app.channels.base import ChannelType, InboundMessage
from app.models.general_assistant_config import (
    DEFAULT_GA_BUILTIN_TOOL_IDS,
    GENERAL_ASSISTANT_BUILTIN_TOOL_IDS,
)
from app.services import general_assistant_link
from app.services.general_assistant_link import general_assistant_stand_in, stand_in_refusal

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _agent(**overrides) -> SimpleNamespace:
    values = {
        "id": uuid.uuid4(),
        "name": "CTEM Assistant",
        "enabled": True,
        "disable_chat": False,
        "is_company_managed": True,
        "access_restricted": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FlushReachedError(Exception):
    """The settings handler's first write: every gate has run by then."""


class _Session:
    def __init__(self, *results):
        self._results = list(results)
        self.statements: list = []
        self.added: list = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _Result(self._results.pop(0))

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        raise _FlushReachedError

    async def commit(self):
        pass


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"is_company_managed": False}, "is not company-managed"),
        ({"access_restricted": True}, "is restricted to the people granted access"),
        ({"enabled": False}, "is disabled"),
        ({"disable_chat": True}, "has chat disabled"),
    ],
    ids=["private", "restricted", "disabled", "chat-disabled"],
)
def test_an_agent_not_every_user_can_chat_with_cannot_stand_in(overrides, reason):
    assert stand_in_refusal(_agent(**overrides)) == reason


@pytest.mark.parametrize("restricted", [False, None], ids=["unrestricted", "null"])
def test_an_enabled_company_managed_chat_agent_can_stand_in(restricted):
    assert stand_in_refusal(_agent(access_restricted=restricted)) is None


@pytest.mark.asyncio
async def test_no_link_is_no_stand_in_and_the_lookup_is_one_read():
    """Asked on every inbound channel message: the link and its agent in one join."""
    db = _Session(None)
    assert await general_assistant_stand_in(db) is None
    [statement] = db.statements
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "JOIN general_assistant_config ON general_assistant_config.linked_agent_id = agents.id" in sql


@pytest.mark.asyncio
async def test_a_link_meeting_the_rule_is_followed():
    agent = _agent()
    assert await general_assistant_stand_in(_Session(agent)) is agent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "agent",
    [_agent(is_company_managed=False), _agent(enabled=False), _agent(access_restricted=True)],
    ids=["made-private", "disabled", "restricted"],
)
async def test_a_link_that_stopped_meeting_the_rule_is_not_followed(agent):
    assert await general_assistant_stand_in(_Session(agent)) is None


# ---------------------------------------------------------------------------
# Who hands it the turn
# ---------------------------------------------------------------------------


def _stand_in(monkeypatch, agent, *, may_chat: bool = True):
    async def _lookup(_db):
        return agent

    async def _may_chat_with(_db, _agent, _user_id):
        return may_chat

    monkeypatch.setattr(general_assistant_link, "general_assistant_stand_in", _lookup)
    monkeypatch.setattr(general_assistant_link, "may_chat_with", _may_chat_with)


class _ConversationDB:
    def __init__(self):
        self.added: list = []

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass


@pytest.mark.asyncio
async def test_a_web_conversation_created_without_an_agent_is_bound_to_the_stand_in(monkeypatch):
    agent = _agent()
    _stand_in(monkeypatch, agent)
    db = _ConversationDB()

    # The fake session fills no server defaults: the response read fails after the insert.
    with contextlib.suppress(AttributeError):
        await chat_api.create_conversation(
            body=chat_api.ConversationCreate(),
            user=SimpleNamespace(id=uuid.uuid4(), is_admin=False),
            db=db,
            _chat_gate=None,
        )

    [conversation] = db.added
    assert conversation.agent_id == agent.id


@pytest.mark.asyncio
async def test_without_a_stand_in_a_web_conversation_stays_the_general_assistants(monkeypatch):
    _stand_in(monkeypatch, None)
    db = _ConversationDB()

    with contextlib.suppress(AttributeError):
        await chat_api.create_conversation(
            body=chat_api.ConversationCreate(),
            user=SimpleNamespace(id=uuid.uuid4(), is_admin=False),
            db=db,
            _chat_gate=None,
        )

    [conversation] = db.added
    assert conversation.agent_id is None


async def _route_dm(monkeypatch, stand_in):
    from app.channels import router

    _stand_in(monkeypatch, stand_in)
    inbound = InboundMessage(
        channel=ChannelType.SLACK,
        text="Create an agent that watches our CVE feed",
        user_email="analyst@example.com",
        channel_user_id="U12345",
        channel_thread_id="D0123",
        metadata={"channel_type": "im"},
    )
    return await router._route_turn(
        object(),
        inbound,
        target_agent_id=None,
        channel_bot=None,
        is_profile_override=False,
        is_anonymous=False,
        is_dm=True,
        user=SimpleNamespace(id=uuid.uuid4()),
    )


@pytest.mark.asyncio
async def test_a_channel_message_for_the_general_assistant_is_answered_by_the_stand_in(monkeypatch):
    agent = _agent()
    route = await _route_dm(monkeypatch, agent)
    assert route.agent is agent
    assert route.general_assistant is False


@pytest.mark.asyncio
async def test_without_a_stand_in_the_general_assistant_answers_a_channel_message(monkeypatch):
    route = await _route_dm(monkeypatch, None)
    assert route.agent is None
    assert route.general_assistant is True


# ---------------------------------------------------------------------------
# One conversation, one answering agent, wherever it is continued
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("agent_id", "channel", "follows"),
    [
        (None, "slack", True),
        (None, "teams", True),
        (None, "web_embed", True),
        (None, "web", False),
        (None, None, False),
        (uuid.uuid4(), "slack", False),
    ],
    ids=["slack", "teams", "web-embed", "web", "unset-is-web", "bound"],
)
def test_an_unbound_conversation_started_outside_the_web_chat_follows_the_link(agent_id, channel, follows):
    assert general_assistant_link.follows_the_link(agent_id, channel) is follows


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_id", "channel", "may_chat", "answers"),
    [
        (None, "slack", True, True),
        (None, "web", True, False),
        (uuid.uuid4(), "slack", True, False),
        (None, "slack", False, False),
    ],
    ids=["slack-conversation", "web-conversation", "bound-conversation", "user-may-not-chat"],
)
async def test_the_turn_of_a_conversation_is_the_stand_ins_only_where_it_follows_the_link(
    monkeypatch, agent_id, channel, may_chat, answers
):
    agent = _agent()
    _stand_in(monkeypatch, agent, may_chat=may_chat)
    found = await general_assistant_link.turn_stand_in(
        object(), agent_id=agent_id, conversation_channel=channel, user_id=uuid.uuid4()
    )
    assert found is (agent if answers else None)


@pytest.mark.asyncio
async def test_a_turn_with_no_user_or_no_link_is_the_general_assistants(monkeypatch):
    _stand_in(monkeypatch, _agent())
    assert (
        await general_assistant_link.turn_stand_in(object(), agent_id=None, conversation_channel="slack", user_id=None)
        is None
    )
    _stand_in(monkeypatch, None)
    assert (
        await general_assistant_link.turn_stand_in(
            object(), agent_id=None, conversation_channel="slack", user_id=uuid.uuid4()
        )
        is None
    )


@pytest.mark.asyncio
async def test_a_web_conversation_never_reads_the_link(monkeypatch):
    async def _never(_db):
        raise AssertionError("the link was read for a web conversation")

    monkeypatch.setattr(general_assistant_link, "general_assistant_stand_in", _never)
    assert (
        await general_assistant_link.turn_stand_in(
            object(), agent_id=None, conversation_channel="web", user_id=uuid.uuid4()
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [{"enabled": False}, {"disable_chat": True}], ids=["disabled", "chat-off"])
async def test_nobody_may_chat_with_a_disabled_or_chat_off_agent_and_nothing_is_read(overrides):
    db = _Session()
    assert await general_assistant_link.may_chat_with(db, _agent(**overrides), uuid.uuid4()) is False
    assert db.statements == []


@pytest.mark.asyncio
@pytest.mark.parametrize("visible", [True, False], ids=["visible", "not-visible"])
async def test_whether_a_user_may_chat_with_an_agent_is_the_three_tier_rule(monkeypatch, visible):
    agent = _agent()
    groups: list = []

    async def _groups(_db, user_id):
        groups.append(user_id)
        return ["g-1"]

    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", _groups)
    user_id = uuid.uuid4()
    db = _Session(agent.id if visible else None)
    assert await general_assistant_link.may_chat_with(db, agent, str(user_id)) is visible
    assert groups == [user_id]
    [statement] = db.statements
    sql = str(statement.compile(dialect=postgresql.dialect()))
    # Own, shared and company-managed rows, the restricted ones withdrawn.
    assert "agents.is_company_managed IS true" in sql and "agents.access_restricted IS NOT true" in sql
    assert "agents.shared_user_ids" in sql and "agents.shared_group_ids" in sql


@pytest.mark.asyncio
async def test_a_page_of_conversations_reads_the_link_once_and_asks_no_owner(monkeypatch):
    """A shared page lists many owners: none is asked on their own (no N+1),
    since a link that qualifies reaches every user."""
    agent = _agent()
    reads: list = []

    async def _lookup(_db):
        reads.append(1)
        return agent

    async def _never(*_args):
        raise AssertionError("an owner was asked on their own")

    monkeypatch.setattr(general_assistant_link, "general_assistant_stand_in", _lookup)
    monkeypatch.setattr(general_assistant_link, "may_chat_with", _never)

    def conv(channel, *, user_id=None, agent_id=None):
        return SimpleNamespace(id=uuid.uuid4(), channel=channel, user_id=user_id, agent_id=agent_id)

    owners = [uuid.uuid4() for _ in range(3)]
    slack, teams, web = conv("slack", user_id=owners[0]), conv("teams", user_id=owners[1]), conv("web", user_id=owners[2])
    bound = conv("slack", user_id=owners[2], agent_id=uuid.uuid4())
    anonymous = conv("slack")

    found = await general_assistant_link.conversation_stand_ins(object(), [slack, teams, web, bound, anonymous])
    assert found == {slack.id: agent, teams.id: agent}
    assert len(reads) == 1


@pytest.mark.asyncio
async def test_a_page_with_no_conversation_following_the_link_reads_nothing(monkeypatch):
    async def _never(_db):
        raise AssertionError("the link was read")

    monkeypatch.setattr(general_assistant_link, "general_assistant_stand_in", _never)
    web = SimpleNamespace(id=uuid.uuid4(), channel="web", user_id=uuid.uuid4(), agent_id=None)
    assert await general_assistant_link.conversation_stand_ins(object(), [web]) == {}


@pytest.mark.asyncio
async def test_a_web_conversation_continued_in_slack_keeps_the_general_assistant_there(monkeypatch):
    """As on the web and in its wake: who answers is the conversation's, not the surface's."""
    from app.channels import router

    _stand_in(monkeypatch, _agent())
    inbound = InboundMessage(
        channel=ChannelType.SLACK,
        text="and the figures?",
        user_email="analyst@example.com",
        channel_user_id="U12345",
        channel_thread_id="D0123:1700000000.000100",
        metadata={"channel_type": "im"},
    )
    route = await router._route_turn(
        object(),
        inbound,
        target_agent_id=None,
        channel_bot=None,
        is_profile_override=False,
        is_anonymous=False,
        is_dm=True,
        user=SimpleNamespace(id=uuid.uuid4()),
        conversation_channel="web",
    )
    assert route.agent is None and route.general_assistant is True


@pytest.mark.asyncio
async def test_a_channel_user_who_may_not_chat_with_the_stand_in_gets_the_general_assistant(monkeypatch):
    from app.channels import router

    _stand_in(monkeypatch, _agent(), may_chat=False)
    inbound = InboundMessage(
        channel=ChannelType.SLACK,
        text="hello",
        user_email="analyst@example.com",
        channel_user_id="U12345",
        channel_thread_id="D0123",
        metadata={"channel_type": "im"},
    )
    route = await router._route_turn(
        object(),
        inbound,
        target_agent_id=None,
        channel_bot=None,
        is_profile_override=False,
        is_anonymous=False,
        is_dm=True,
        user=SimpleNamespace(id=uuid.uuid4()),
    )
    assert route.agent is None and route.general_assistant is True


# ---------------------------------------------------------------------------
# The settings API
# ---------------------------------------------------------------------------


@pytest.fixture
def platform_settings(monkeypatch):
    async def _fake_platform_settings(_db):
        return SimpleNamespace(
            embedding_provider=None,
            embedding_provider_id=None,
            embedding_model=None,
            embedding_dimensions=None,
        )

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _fake_platform_settings,
    )


async def _link(db: _Session, agent_id: str):
    return await update_general_assistant_config(
        body=GeneralAssistantConfigRequest(linked_agent_id=agent_id),
        request=SimpleNamespace(),
        background_tasks=BackgroundTasks(),
        user=SimpleNamespace(id="admin", email="admin@filigran.io"),
        db=db,
    )


@pytest.mark.asyncio
async def test_linking_an_agent_not_every_user_can_chat_with_is_refused(platform_settings):
    private = _agent(name="My private agent", is_company_managed=False)
    config = SimpleNamespace(linked_agent_id=None)

    with pytest.raises(HTTPException) as exc:
        await _link(_Session(config, private), str(private.id))

    assert exc.value.status_code == 422
    assert '"My private agent" is not company-managed' in exc.value.detail
    assert config.linked_agent_id is None
    assert not hasattr(config, "linked_agent_configured")


@pytest.mark.asyncio
async def test_linking_an_agent_meeting_the_rule_is_saved(platform_settings):
    agent = _agent()
    config = SimpleNamespace(linked_agent_id=None)

    with pytest.raises(_FlushReachedError):
        await _link(_Session(config, agent), str(agent.id))

    assert config.linked_agent_id == agent.id
    # Boot and the AI toggle keep it from now on.
    assert config.linked_agent_configured is True


@pytest.mark.asyncio
async def test_choosing_no_agent_is_an_admin_choice_too(platform_settings):
    """NULL is also "never configured", which boot fills with the CTEM
    Assistant: the flag is what keeps "no agent" across restarts."""
    linked = _agent()
    config = SimpleNamespace(linked_agent_id=linked.id, linked_agent_configured=False)
    db = _Session(config)

    with pytest.raises(_FlushReachedError):
        await _link(db, None)

    assert config.linked_agent_id is None
    assert config.linked_agent_configured is True
    assert len(db.statements) == 1


@pytest.mark.asyncio
async def test_a_stored_link_sent_back_is_kept_without_the_rule(platform_settings):
    """Like a stored handover target: an unrelated save never fails on it."""
    disabled = _agent(enabled=False)
    config = SimpleNamespace(linked_agent_id=disabled.id)

    with pytest.raises(_FlushReachedError):
        await _link(_Session(config, disabled), str(disabled.id))

    assert config.linked_agent_id == disabled.id


@pytest.mark.asyncio
async def test_a_malformed_link_is_an_unknown_agent(platform_settings):
    db = _Session(SimpleNamespace(linked_agent_id=None))

    with pytest.raises(HTTPException) as exc:
        await _link(db, "not-a-uuid")

    assert exc.value.status_code == 404
    assert len(db.statements) == 1


# ---------------------------------------------------------------------------
# What a General Assistant ships with, on new and existing platforms
# ---------------------------------------------------------------------------


def _migration():
    path = BACKEND_ROOT / "alembic" / "versions" / "g5209gadefault_add_ga_default_platform_builder.py"
    spec = importlib.util.spec_from_file_location("g5209gadefault", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_new_general_assistant_can_create_agents_and_read_a_link():
    assert {"agent_builder", "web_fetch"} <= set(DEFAULT_GA_BUILTIN_TOOL_IDS)
    assert set(DEFAULT_GA_BUILTIN_TOOL_IDS) <= set(GENERAL_ASSISTANT_BUILTIN_TOOL_IDS)


def test_the_migration_adds_exactly_what_the_defaults_gained():
    assert set(_migration().ADDED_TOOL_IDS) <= set(DEFAULT_GA_BUILTIN_TOOL_IDS)


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ('["generate_file"]', ["generate_file", "agent_builder", "web_fetch"]),
        ('["web_fetch", "browser"]', ["web_fetch", "browser", "agent_builder"]),
        ("[]", ["agent_builder", "web_fetch"]),
        (None, ["agent_builder", "web_fetch"]),
        ("null", ["agent_builder", "web_fetch"]),
    ],
    ids=["missing-both", "missing-one", "empty", "sql-null", "json-null"],
)
def test_the_migration_appends_the_missing_tools_and_keeps_the_rest(stored, expected):
    assert json.loads(_migration().with_added_tools(stored)) == expected


@pytest.mark.parametrize(
    "stored",
    ['["agent_builder", "web_fetch"]', '{"not": "a list"}', "not json"],
    ids=["both-present", "not-a-list", "unreadable"],
)
def test_the_migration_writes_nothing_it_does_not_need_to(stored):
    assert _migration().with_added_tools(stored) is None
