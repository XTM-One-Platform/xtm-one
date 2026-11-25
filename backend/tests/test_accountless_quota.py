"""Agentic runs with no account behind them count against the platform-wide
licence limits.

The licence sells platform-wide agentic capacity: its daily / monthly / yearly
ceilings in XTM One mode, the ``platform_limit`` of Settings > Usage and quotas > Quotas in Copilot
mode. A run with no account behind it - an external-channel sender without an
account (the router runs it as ``"anonymous"``), a public widget visitor, a
``/mcp/global`` call made with the platform MCP key (``"mcp-global"``), an
assignment run no user can be attributed to - used to be neither checked
against those limits nor counted in them. It now goes through the one agentic
gate like any request and, having no user, group or per-user limit, is checked
against and charged to the platform-wide tiers alone.

Pinned for every entry point, on both platform modes: the run is charged to the
global counters, refused at the ceiling in words for a reader with no quota of
their own, refused while the quota store is unreachable in XTM One mode,
credited back on those counters when it never reached a model, and never writes
a per-user counter. The execution telemetry counts it once.
"""

from __future__ import annotations

import contextlib
import fnmatch
import inspect
import json
import re
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core import config
from app.services import quota as quota_module
from app.services.agent_chat import QUOTA_STORE_OUTAGE_KEY, enforce_agentic_quota_ex
from app.services.failure_causes import FailureCause
from app.services.quota import (
    _USAGE_PREFIX,
    ACCOUNTLESS_QUOTA_EXCEEDED_MESSAGE,
    ACCOUNTLESS_QUOTA_UNAVAILABLE_MESSAGE,
    QUOTA_UNAVAILABLE_MESSAGE,
    REFUND_OUTCOME_NO_ACCOUNT,
    QuotaExceededError,
    _current_period_key,
    _global_usage_key,
    _usage_key,
    check_agentic_quota,
    get_platform_quota_monitoring,
    increment_agentic_usage,
    refund_agentic_usage,
    refund_agentic_usage_by_receipt,
    refund_agentic_usage_for_failure,
    refund_agentic_usage_for_provider_outage,
)
from tests.agentic_gate_stub import stub_agentic_gate
from tests.quota_redis_scripts import RedisHashes, run_quota_script
from tests.test_fail_modes import _FakeSession, _StopAfterQuotaGate

#: The one platform-wide ceiling of the platforms below.
LIMIT = 3

DB = SimpleNamespace(name="db-sentinel")


class _FakeRedis(RedisHashes):
    """Counters in a dict, the quota Lua scripts through their Python twins."""

    def __init__(self) -> None:
        self.store: dict = {}
        self.hashes: dict = {}

    async def get(self, key):
        value = self.store.get(key)
        return None if value is None else str(value).encode()

    async def incr(self, key):
        self.store[key] = int(self.store.get(key, 0)) + 1
        return self.store[key]

    async def incrby(self, key, amount):
        self.store[key] = int(self.store.get(key, 0)) + amount
        return self.store[key]

    async def decr(self, key):
        self.store[key] = int(self.store.get(key, 0)) - 1
        return self.store[key]

    async def expire(self, key, ttl):
        return True

    async def set(self, key, value, ex=None, **kwargs):
        self.store[key] = value
        return True

    async def getdel(self, key):
        value = self.store.pop(key, None)
        return None if value is None else str(value).encode()

    async def delete(self, *keys):
        return sum(self.store.pop(key, None) is not None for key in keys)

    async def eval(self, script, numkeys, *keys_and_args):
        return run_quota_script(self.store, self.hashes, script, keys_and_args)

    async def scan_iter(self, match=None, count=None):
        for key in list(self.store):
            if match is None or fnmatch.fnmatchcase(key, match):
                yield key


@contextlib.asynccontextmanager
async def _private_session():
    """The refund helpers' own session: period resolution is patched below."""
    yield SimpleNamespace()


def _per_user_counters(redis: _FakeRedis) -> list[str]:
    """Every usage counter that belongs to somebody - none may appear."""
    return [
        key
        for key in redis.store
        if key.startswith(_USAGE_PREFIX) and not key.startswith(f"{_USAGE_PREFIX}_global:")
    ]


def _executions(redis: _FakeRedis) -> int:
    return sum(int(value) for key, value in redis.store.items() if key.startswith("agentic:exec:"))


def _count(redis: _FakeRedis, key: str) -> int:
    return int(redis.store.get(key, 0))


def _use_store(monkeypatch, redis) -> None:
    """One store behind the quota, the execution telemetry and the chat's
    activity counter."""
    for target in (
        "app.services.quota.redis_client",
        "app.services.agentic_executions.redis_client",
        "app.core.redis.redis_client",
    ):
        monkeypatch.setattr(target, AsyncMock(return_value=redis))
    monkeypatch.setattr("app.core.database.async_session", _private_session)


@pytest.fixture(params=["xtm_one", "copilot"])
def platform(request, monkeypatch):
    """A platform whose one platform-wide ceiling is ``LIMIT`` agentic runs.

    The license's daily quota in XTM One mode, ``platform_limit`` per month in
    Copilot mode - the tier each mode enforces for everyone. Returns the store
    and the counter that ceiling is read from.
    """
    redis = _FakeRedis()
    _use_store(monkeypatch, redis)
    monkeypatch.setattr(config.settings, "platform_mode", request.param)
    if request.param == "xtm_one":
        monkeypatch.setattr(
            quota_module, "get_xtm_agentic_ceilings", AsyncMock(return_value={"daily": LIMIT})
        )
        period = "daily"
    else:
        monkeypatch.setattr(
            quota_module,
            "_load_platform_wide_config",
            AsyncMock(return_value={"platform_limit": LIMIT, "period": "monthly"}),
        )
        period = "monthly"
    return SimpleNamespace(
        mode=request.param,
        redis=redis,
        ceiling_key=_global_usage_key("agentic", _current_period_key(period)),
    )


@pytest.fixture
def xtm_one_store_down(monkeypatch):
    """XTM One mode with ``QUOTA_FAIL_MODE=open`` set - which it ignores -
    and a quota store that cannot be reached."""
    redis = _FakeRedis()
    _use_store(monkeypatch, redis)
    monkeypatch.setattr(config.settings, "platform_mode", "xtm_one")
    monkeypatch.setattr(config.settings, "quota_fail_mode", "open")
    monkeypatch.setattr(config.settings, "_environment_fields", frozenset({"quota_fail_mode"}))
    monkeypatch.setattr(
        quota_module, "redis_client", AsyncMock(side_effect=ConnectionError("quota store unreachable"))
    )
    return redis


# ---------------------------------------------------------------------------
# The quota service, for a run with no account (``user_id=None``)
# ---------------------------------------------------------------------------


async def test_a_charge_with_no_account_lands_on_the_platform_wide_counters_only(platform):
    await increment_agentic_usage(None, db=DB, agent_id=uuid.uuid4())

    assert _count(platform.redis, platform.ceiling_key) == 1
    assert _per_user_counters(platform.redis) == []


async def test_the_platform_ceiling_refuses_a_run_with_no_account(platform):
    platform.redis.store[platform.ceiling_key] = LIMIT - 1
    await check_agentic_quota(DB, None)

    platform.redis.store[platform.ceiling_key] = LIMIT
    with pytest.raises(QuotaExceededError):
        await check_agentic_quota(DB, None)


async def test_a_refund_with_no_account_reverses_its_charge_and_never_goes_below_zero(platform):
    await increment_agentic_usage(None, db=DB)
    await increment_agentic_usage(None, db=DB)
    await refund_agentic_usage(None, db=DB)
    assert _count(platform.redis, platform.ceiling_key) == 1

    await refund_agentic_usage(None, db=DB)
    await refund_agentic_usage(None, db=DB)
    global_counters = [key for key in platform.redis.store if key.startswith(f"{_USAGE_PREFIX}_global:")]
    assert global_counters
    assert all(_count(platform.redis, key) == 0 for key in global_counters)
    assert _per_user_counters(platform.redis) == []


async def test_the_failure_pool_never_applies_to_a_run_with_no_account(platform, monkeypatch):
    """The pool is a per-user allowance: the failure keeps its charge."""
    monkeypatch.setattr(quota_module.settings, "agent_failure_quota_refund_enabled", True)
    monkeypatch.setattr(quota_module.settings, "agent_failure_quota_pool", 5)
    await increment_agentic_usage(None, db=DB)
    outcome: dict = {}

    refunded = await refund_agentic_usage_for_failure(None, uuid.uuid4(), db=DB, outcome_out=outcome)

    assert refunded is False
    assert outcome == {"outcome": REFUND_OUTCOME_NO_ACCOUNT}
    assert _count(platform.redis, platform.ceiling_key) == 1
    assert not [key for key in platform.redis.store if "agentic_failure_pool" in key]


async def test_a_run_with_no_account_that_reached_no_model_gets_its_charge_back(platform):
    agent_id = uuid.uuid4()
    await increment_agentic_usage(None, db=DB, agent_id=agent_id)

    assert await refund_agentic_usage_for_provider_outage(None, agent_id, db=DB) is True

    assert _count(platform.redis, platform.ceiling_key) == 0
    # Not tallied under anybody: the card counts its credits per user.
    assert _per_user_counters(platform.redis) == []


async def test_a_receipt_of_a_charge_with_no_account_is_redeemed_once(platform):
    agent_id = uuid.uuid4()
    await increment_agentic_usage(None, db=DB, agent_id=agent_id, receipt_token="sub_agent_task:t1")
    assert json.loads(platform.redis.store["quota:receipt:sub_agent_task:t1"])["user_id"] is None

    assert await refund_agentic_usage_by_receipt("sub_agent_task:t1", db=DB) is True
    assert await refund_agentic_usage_by_receipt("sub_agent_task:t1", db=DB) is False

    assert _count(platform.redis, platform.ceiling_key) == 0


# ---------------------------------------------------------------------------
# The one gate: ``agent_chat.enforce_agentic_quota_ex``
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("user_id", [None, "", "anonymous", "mcp-global"])
async def test_the_gate_charges_a_request_with_no_account_to_the_platform_wide_tiers(platform, user_id):
    out: dict = {}

    refusal, metered = await enforce_agentic_quota_ex(DB, user_id, agent_id=str(uuid.uuid4()), quota_out=out)

    assert (refusal, metered) == (None, True)
    assert out == {
        "actions_consumed": 1,
        "reason": "charged_no_account",
        # No config of its own to resolve: the default bucket.
        "period": _current_period_key("monthly"),
    }
    assert _count(platform.redis, platform.ceiling_key) == 1
    assert _per_user_counters(platform.redis) == []
    assert _executions(platform.redis) == 1


async def test_an_account_is_still_charged_to_its_own_counter_too(platform, monkeypatch):
    monkeypatch.setattr(quota_module, "resolve_quota_config", AsyncMock(return_value=None))
    user_id = uuid.uuid4()
    out: dict = {}

    refusal, metered = await enforce_agentic_quota_ex(DB, str(user_id), agent_id=str(uuid.uuid4()), quota_out=out)

    assert (refusal, metered) == (None, True)
    assert out == {"actions_consumed": 1, "reason": "charged", "period": _current_period_key("monthly")}
    assert _count(platform.redis, platform.ceiling_key) == 1
    assert _count(platform.redis, _usage_key(user_id, "agentic", None, _current_period_key("monthly"))) == 1


async def test_the_gate_refuses_a_request_with_no_account_at_the_ceiling_in_words_for_its_reader(platform):
    platform.redis.store[platform.ceiling_key] = LIMIT
    out: dict = {}

    refusal, metered = await enforce_agentic_quota_ex(DB, "anonymous", agent_id=str(uuid.uuid4()), quota_out=out)

    assert (refusal, metered) == (ACCOUNTLESS_QUOTA_EXCEEDED_MESSAGE, False)
    assert out == {"actions_consumed": 0, "reason": "quota_exceeded"}
    assert _count(platform.redis, platform.ceiling_key) == LIMIT
    # A person on a channel or the widget has no quota, licence or store of
    # their own to be told about.
    for internal in ("quota", "licen", "store", "backend", "administrator"):
        assert internal not in refusal.lower()


async def test_xtm_one_refuses_a_request_with_no_account_while_the_quota_store_is_unreachable(
    xtm_one_store_down,
):
    out: dict = {}

    refusal, metered = await enforce_agentic_quota_ex(DB, "mcp-global", agent_id=str(uuid.uuid4()), quota_out=out)

    assert (refusal, metered) == (ACCOUNTLESS_QUOTA_UNAVAILABLE_MESSAGE, False)
    assert out == {"actions_consumed": 0, "reason": "quota_unavailable", QUOTA_STORE_OUTAGE_KEY: True}
    for internal in ("quota", "licen", "store", "backend"):
        assert internal not in refusal.lower()


@pytest.mark.parametrize(("fail_mode", "expected"), [("open", None), ("closed", ACCOUNTLESS_QUOTA_UNAVAILABLE_MESSAGE)])
async def test_copilot_mode_applies_its_own_fail_mode_to_a_request_with_no_account(monkeypatch, fail_mode, expected):
    _use_store(monkeypatch, _FakeRedis())
    monkeypatch.setattr(config.settings, "platform_mode", "copilot")
    monkeypatch.setattr(config.settings, "quota_fail_mode", fail_mode)
    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(side_effect=ConnectionError("down")))

    refusal, metered = await enforce_agentic_quota_ex(DB, "anonymous", agent_id=str(uuid.uuid4()))

    assert (refusal, metered) == (expected, False)


# ---------------------------------------------------------------------------
# External channels (anonymous senders) and /mcp/global (the platform key)
# ---------------------------------------------------------------------------


def _chat_agent(**overrides) -> SimpleNamespace:
    """An agent whose ``skip_llm`` path answers with the message itself."""
    defaults = dict(
        id=uuid.uuid4(),
        name="Public Agent",
        slug="public-agent",
        is_builtin=False,
        skip_llm=True,
        prepare_tool_ref=None,
        finalize_tool_ref=None,
        llm_model=None,
        llm_provider=None,
        llm_provider_id=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


async def _anonymous_channel_turn(agent) -> str:
    """The turn ``channels/router.py`` runs for a sender with no account."""
    from app.services.agent_chat import run_agent_chat

    text, _ = await run_agent_chat(db=DB, user_id="anonymous", agent=agent, history=[], user_message="ping")
    return text


async def _mcp_global_call(agent) -> str:
    """What ``/mcp/global`` runs for a call made with the platform MCP key."""
    from app.api.v1.mcp_endpoint import GLOBAL_KEY_CALLER, _execute_agent_tool

    return await _execute_agent_tool(DB, GLOBAL_KEY_CALLER, agent, "ping")


_CHAT_ENTRY_POINTS = pytest.mark.parametrize(
    "run_turn", [_anonymous_channel_turn, _mcp_global_call], ids=["anonymous-channel", "mcp-global"]
)


def test_the_entry_points_hand_the_gate_their_synthetic_identity():
    """What the two helpers above stand for, read off the entry points."""
    from app.api.v1 import mcp_endpoint
    from app.channels import router
    from app.core.utils import safe_uuid

    assert 'effective_user_id = str(user.id) if user else "anonymous"' in inspect.getsource(router)
    assert "return ps, GLOBAL_KEY_CALLER" in inspect.getsource(mcp_endpoint._authenticate_global_endpoint)
    assert safe_uuid(mcp_endpoint.GLOBAL_KEY_CALLER) is None


@pytest.mark.parametrize("user_id", [None, "", "anonymous", "mcp-global"])
def test_the_quota_gate_and_the_read_tier_agree_on_who_has_no_account(monkeypatch, user_id):
    """One reading of "no account": the callers ``caller_visible_filter``
    gives the company-wide read tier are the ones the quota gate
    charges to the platform-wide tiers (``safe_uuid`` of the id is None)."""
    from app.api.v1 import helpers
    from app.core.utils import safe_uuid
    from app.models.agent import Agent

    company_wide = object()
    monkeypatch.setattr(helpers, "company_wide_visible_filter", lambda _model: company_wide)

    assert helpers.caller_visible_filter(Agent, user_id) is company_wide
    assert safe_uuid(user_id) is None


@_CHAT_ENTRY_POINTS
async def test_a_chat_turn_with_no_account_is_charged_to_the_platform_wide_tiers(platform, run_turn):
    assert await run_turn(_chat_agent()) == "ping"

    assert _count(platform.redis, platform.ceiling_key) == 1
    assert _per_user_counters(platform.redis) == []
    assert _executions(platform.redis) == 1


@_CHAT_ENTRY_POINTS
async def test_a_chat_turn_with_no_account_is_refused_at_the_ceiling(platform, run_turn):
    platform.redis.store[platform.ceiling_key] = LIMIT

    assert await run_turn(_chat_agent()) == ACCOUNTLESS_QUOTA_EXCEEDED_MESSAGE

    assert _count(platform.redis, platform.ceiling_key) == LIMIT
    assert _per_user_counters(platform.redis) == []


@_CHAT_ENTRY_POINTS
async def test_xtm_one_refuses_a_chat_turn_with_no_account_while_the_store_is_down(xtm_one_store_down, run_turn):
    assert await run_turn(_chat_agent()) == ACCOUNTLESS_QUOTA_UNAVAILABLE_MESSAGE


@_CHAT_ENTRY_POINTS
async def test_a_chat_turn_with_no_account_that_reached_no_model_gets_its_charge_back(
    platform, monkeypatch, run_turn
):
    monkeypatch.setattr(
        "app.llm.router.get_llm_for_user",
        AsyncMock(side_effect=ValueError("No LLM provider is configured")),
    )

    with pytest.raises(ValueError, match="No LLM provider"):
        await run_turn(_chat_agent(skip_llm=False))

    assert _count(platform.redis, platform.ceiling_key) == 0
    assert _per_user_counters(platform.redis) == []


# ---------------------------------------------------------------------------
# External channels: what the router does with a refused anonymous turn
# ---------------------------------------------------------------------------


class _RouterSession:
    """The session ``handle_message`` opens: the bound agent is its one read."""

    def __init__(self, agent) -> None:
        self._agent = agent

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, *_args, **_kwargs):
        return SimpleNamespace(scalar_one_or_none=lambda: self._agent)

    async def commit(self) -> None:
        return None


def _slack_message(*, dm: bool = False, mentioned: bool = False, email: str | None = None):
    from app.channels.base import ChannelType, InboundMessage

    metadata: dict = {"channel_type": "im" if dm else "channel"}
    if mentioned:
        metadata["bot_was_mentioned"] = True
    return InboundMessage(
        channel=ChannelType.SLACK,
        text="How do I rotate my API key?",
        user_email=email,
        channel_user_id="U12345",
        channel_thread_id="C0123:1700000000.000100",
        metadata=metadata,
    )


@pytest.fixture
def channel_turn(monkeypatch):
    """``handle_message`` down to the agent turn, everything around it stubbed.

    ``reply`` is what the turn hands back - its text and, for a refusal, the
    ``error_out`` code the gate writes - and ``user`` the sender's account.
    """
    from app.channels import router
    from app.tools.base import FileContext

    agent = SimpleNamespace(id=uuid.uuid4(), name="Community Agent", enabled=True, channel_config={})
    bot = SimpleNamespace(
        id=uuid.uuid4(),
        only_mention=False,
        allow_anonymous=True,
        allow_private=True,
        allowed_receive_channels=None,
    )
    turn = SimpleNamespace(
        reply=("Rotate it from your profile.", None),
        user=None,
        stored=[],
        conversations=[],
        title=AsyncMock(return_value="Rotating an API key"),
    )

    async def _run_agent_chat(_db, _user_id, _agent, _history, _message, **kwargs):
        text, code = turn.reply
        error_out = kwargs.get("error_out")
        if code is not None and error_out is not None:
            error_out.update(error=text, error_code=code)
        return text, None

    async def _conversation(*_args, **_kwargs):
        conversation = SimpleNamespace(id=uuid.uuid4(), title="New conversation", agent_id=None)
        turn.conversations.append(conversation)
        return conversation

    async def _store_message(_db, _conversation_id, role, content, **_kwargs):
        turn.stored.append((role, content))
        return SimpleNamespace(id=uuid.uuid4())

    async def _resolve_user(*_args, **_kwargs):
        return turn.user

    async def _unchanged(_db, text, *_args, **_kwargs):
        return text

    stub_agentic_gate(monkeypatch)
    monkeypatch.setattr(router, "async_session", lambda: _RouterSession(agent))
    monkeypatch.setattr(router, "_get_channel_bot", AsyncMock(return_value=bot))
    for name in (
        "_is_thread_active",
        "_is_thread_muted",
        "_mark_thread_active",
        "link_channel_identity",
        "_log_channel_activity",
    ):
        monkeypatch.setattr(router, name, AsyncMock(return_value=False))
    monkeypatch.setattr(router, "resolve_user_by_email", _resolve_user)
    monkeypatch.setattr(router, "resolve_user_by_channel_identity", _resolve_user)
    monkeypatch.setattr(router, "_resolve_channel_agent_anonymous", AsyncMock(return_value=agent))
    monkeypatch.setattr(router, "_resolve_channel_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr(router, "_find_existing_external_conversation", AsyncMock(return_value=None))
    monkeypatch.setattr(router, "_resolve_channel_files", AsyncMock(return_value=[]))
    monkeypatch.setattr(router, "_load_channel_backfill", AsyncMock(return_value=None))
    monkeypatch.setattr(router, "_agent_allows_channel", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(router, "_agent_can_publish", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(router, "_find_or_create_anonymous_conversation", _conversation)
    monkeypatch.setattr(router, "find_or_create_conversation", _conversation)
    monkeypatch.setattr(router, "store_message", _store_message)
    monkeypatch.setattr("app.services.agent_chat.load_conversation_files", AsyncMock(return_value=FileContext()))
    turn.run = AsyncMock(side_effect=_run_agent_chat)
    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", turn.run)
    monkeypatch.setattr("app.llm.router.get_llm_for_user", AsyncMock(return_value=(object(), "model")))
    monkeypatch.setattr("app.services.conversation_memory.generate_conversation_title", turn.title)
    for name in (
        "resolve_channel_file_markers",
        "resolve_channel_presentation_markers",
        "resolve_channel_report_markers",
    ):
        monkeypatch.setattr(f"app.channels.base.{name}", _unchanged)
    monkeypatch.setattr("app.channels.base.channel_image_scope", AsyncMock(return_value=set()))
    return turn


@pytest.mark.parametrize(
    ("code", "text"),
    [
        ("quota_exceeded", ACCOUNTLESS_QUOTA_EXCEEDED_MESSAGE),
        ("quota_unavailable", ACCOUNTLESS_QUOTA_UNAVAILABLE_MESSAGE),
    ],
)
@pytest.mark.parametrize(
    ("dm", "mentioned"),
    [(True, False), (False, True), (False, False)],
    ids=["direct-message", "mention", "group-channel"],
)
async def test_a_refused_anonymous_turn_is_answered_where_a_model_would_have_and_gets_no_title(
    channel_turn, code, text, dm, mentioned
):
    """Where the licence refusal is answered: wherever a model would
    have answered. The turn reached no model, and neither does its new
    conversation's title."""
    from app.channels import router

    channel_turn.reply = (text, code)
    message = _slack_message(dm=dm, mentioned=mentioned)

    out = await router.handle_message(message)

    assert out.text == text
    assert not out.suppress
    assert channel_turn.stored == [("user", message.text), ("assistant", text)]
    channel_turn.title.assert_not_awaited()
    assert channel_turn.conversations[0].title == message.text


async def test_where_the_router_stays_silent_no_model_runs_so_nothing_is_refused(channel_turn, monkeypatch):
    """The gate only runs inside the agent turn, so a refusal can only be
    answered where the route runs a model - the licence refusal's places."""
    from app.channels import router

    monkeypatch.setattr(router, "_agent_can_publish", lambda *_args, **_kwargs: False)
    channel_turn.reply = (ACCOUNTLESS_QUOTA_EXCEEDED_MESSAGE, "quota_exceeded")

    out = await router.handle_message(_slack_message())

    assert out.suppress is True
    channel_turn.run.assert_not_awaited()
    assert channel_turn.stored == []


async def test_an_answered_anonymous_turn_still_gets_its_generated_title(channel_turn):
    from app.channels import router

    out = await router.handle_message(_slack_message(dm=True))

    assert out.text == "Rotate it from your profile."
    channel_turn.title.assert_awaited_once()
    assert channel_turn.conversations[0].title == "Rotating an API key"


async def test_an_account_refused_in_a_group_channel_is_answered_as_before(channel_turn):
    """An account's own quota refusal is unchanged: posted, and titled."""
    from app.channels import router
    from app.services.agent_chat import QUOTA_EXCEEDED_MESSAGE

    channel_turn.user = SimpleNamespace(id=uuid.uuid4(), channel_agent_config=None)
    channel_turn.reply = (QUOTA_EXCEEDED_MESSAGE, "quota_exceeded")

    out = await router.handle_message(_slack_message(email="member@example.com"))

    assert out.text == QUOTA_EXCEEDED_MESSAGE
    assert not out.suppress
    channel_turn.title.assert_awaited_once()


# ---------------------------------------------------------------------------
# The public chat widget
# ---------------------------------------------------------------------------


def _widget(monkeypatch):
    from tests.test_public_chat_hardening import _patch_chat_pipeline

    return _patch_chat_pipeline(monkeypatch, stub_quota=False)


async def _widget_turn(**kwargs) -> str:
    from app.api.v1.public_chat import _run_public_agent_chat
    from tests.test_public_chat_hardening import _FakeDB, _make_public_agent

    return await _run_public_agent_chat(_FakeDB({}), _make_public_agent(), [], "hello", **kwargs)


def _compaction(monkeypatch) -> AsyncMock:
    """The conversation memory management, which can call the LLM."""
    compact = AsyncMock(side_effect=lambda llm, history, **kwargs: history)
    monkeypatch.setattr("app.services.conversation_memory.manage_conversation_memory", compact)
    return compact


def _loop_raising(monkeypatch, exc: Exception) -> None:
    async def _loop(**_kwargs):
        raise exc

    monkeypatch.setattr("app.tools.agent_loop.run_agent_loop", _loop)


async def test_a_widget_turn_is_charged_to_the_platform_wide_tiers(platform, monkeypatch):
    recorder = _widget(monkeypatch)

    assert await _widget_turn() == "loop response"

    assert recorder["loop_kwargs"] is not None
    assert _count(platform.redis, platform.ceiling_key) == 1
    assert _per_user_counters(platform.redis) == []
    assert _executions(platform.redis) == 1


async def test_a_widget_turn_is_refused_at_the_ceiling_before_any_model_runs(platform, monkeypatch):
    recorder = _widget(monkeypatch)
    platform.redis.store[platform.ceiling_key] = LIMIT

    assert await _widget_turn() == ACCOUNTLESS_QUOTA_EXCEEDED_MESSAGE

    assert recorder["loop_kwargs"] is None
    assert _count(platform.redis, platform.ceiling_key) == LIMIT


async def test_xtm_one_refuses_a_widget_turn_while_the_store_is_down(xtm_one_store_down, monkeypatch):
    recorder = _widget(monkeypatch)
    compact = _compaction(monkeypatch)

    assert await _widget_turn(compact_conversation_id="conv-1") == ACCOUNTLESS_QUOTA_UNAVAILABLE_MESSAGE

    assert recorder["loop_kwargs"] is None
    compact.assert_not_awaited()


async def test_a_widget_turn_refused_at_the_ceiling_never_compacts_its_history(platform, monkeypatch):
    """A compaction cycle calls the LLM, so it runs behind the gate only."""
    recorder = _widget(monkeypatch)
    compact = _compaction(monkeypatch)
    platform.redis.store[platform.ceiling_key] = LIMIT

    assert await _widget_turn(compact_conversation_id="conv-1") == ACCOUNTLESS_QUOTA_EXCEEDED_MESSAGE

    compact.assert_not_awaited()
    assert recorder["loop_kwargs"] is None


async def test_a_widget_turn_let_through_compacts_its_conversation_history(platform, monkeypatch):
    _widget(monkeypatch)
    compact = _compaction(monkeypatch)

    assert await _widget_turn(compact_conversation_id="conv-1") == "loop response"

    compact.assert_awaited_once()
    assert compact.await_args.kwargs["conversation_id"] == "conv-1"
    assert _count(platform.redis, platform.ceiling_key) == 1


def test_the_compactor_sees_the_provider_it_would_have_seen():
    """The recorder is transparent: the compactor's structured-output choice
    reads ``isinstance`` and ``provider_name`` off what it is handed."""
    from app.api.v1.public_chat import _AnswerRecorder
    from app.llm.openai_compat import OpenAICompatibleProvider
    from app.services.structured_output import build_native_structured_output

    provider = object.__new__(OpenAICompatibleProvider)
    provider.provider_name = "openrouter"
    recorder = _AnswerRecorder(provider)

    assert isinstance(recorder, OpenAICompatibleProvider)
    assert recorder.provider_name == "openrouter"
    schema = {"type": "object", "properties": {"summary": {"type": "string"}}}
    assert build_native_structured_output(recorder, schema) == build_native_structured_output(provider, schema)
    assert build_native_structured_output(recorder, schema) is not None


@pytest.mark.parametrize(
    ("messages", "outage", "answered"),
    [("due", False, True), ("due", True, False), ("short", False, False)],
    ids=["summarised", "compactor-unreachable", "no-cycle-due"],
)
async def test_the_real_compactor_says_whether_a_model_answered_it(monkeypatch, messages, outage, answered):
    import app.services.conversation_memory as cm
    from app.api.v1.public_chat import _compact_public_history
    from tests.test_conversation_compaction_cadence import _CompactorStubLLM, _make_history
    from tests.test_public_chat_hardening import _FakeDB, _make_public_agent

    llm = _CompactorStubLLM()
    if outage:
        llm.chat = AsyncMock(side_effect=ConnectionError("Connection refused"))
    monkeypatch.setattr("app.llm.router.get_llm_for_user", AsyncMock(return_value=(llm, "stub-model")))
    monkeypatch.setattr(cm, "_persist_compaction_state", AsyncMock())
    count = cm.SUMMARIZE_THRESHOLD + 2 if messages == "due" else cm.SUMMARIZE_THRESHOLD - 2
    history = _make_history(count)

    compacted, reached = await _compact_public_history(_FakeDB({}), _make_public_agent(), history, "conv-1")

    assert reached is answered
    if answered:
        assert cm._is_compaction_message(compacted[0])


@pytest.mark.parametrize(("summarised", "charged"), [(True, 1), (False, 0)])
async def test_a_compaction_the_model_answered_keeps_the_charge_of_a_turn_that_then_fails(
    platform, monkeypatch, summarised, charged
):
    """The compactor reached a model for this turn when a call answered: a
    provider failure afterwards is no longer "reached no model"."""
    _widget(monkeypatch)
    llm = SimpleNamespace(chat=AsyncMock(return_value=SimpleNamespace(content="summary")))
    monkeypatch.setattr("app.llm.router.get_llm_for_user", AsyncMock(return_value=(llm, "model")))

    async def _compact(compactor, history, **kwargs):
        if summarised:
            await compactor.chat(messages=[])
        return history

    monkeypatch.setattr("app.services.conversation_memory.manage_conversation_memory", _compact)
    _loop_raising(monkeypatch, ConnectionError("Connection refused"))

    with pytest.raises(ConnectionError):
        await _widget_turn(compact_conversation_id="conv-1")

    assert _count(platform.redis, platform.ceiling_key) == charged


async def test_a_widget_turn_with_no_provider_configured_gets_its_charge_back(platform, monkeypatch):
    _widget(monkeypatch)
    monkeypatch.setattr("app.llm.router.get_llm_for_user", AsyncMock(side_effect=ValueError("no provider")))

    assert await _widget_turn() == "No LLM provider is configured. Please contact the administrator."

    assert _count(platform.redis, platform.ceiling_key) == 0


async def test_a_widget_turn_whose_provider_cannot_be_reached_gets_its_charge_back(platform, monkeypatch):
    _widget(monkeypatch)
    _loop_raising(monkeypatch, ConnectionError("Connection refused"))

    with pytest.raises(ConnectionError):
        await _widget_turn()

    assert _count(platform.redis, platform.ceiling_key) == 0
    assert _per_user_counters(platform.redis) == []


async def test_a_widget_turn_that_failed_before_its_loop_gets_its_charge_back(platform, monkeypatch):
    _widget(monkeypatch)
    monkeypatch.setattr(
        "app.services.langfuse_tracing.trace_llm_call", AsyncMock(side_effect=RuntimeError("setup blew up"))
    )

    with pytest.raises(RuntimeError, match="setup blew up"):
        await _widget_turn()

    assert _count(platform.redis, platform.ceiling_key) == 0


async def test_a_widget_turn_that_failed_after_reaching_a_model_keeps_its_charge(platform, monkeypatch):
    """No user, so no failure pool to forgive it: the charge stands."""
    _widget(monkeypatch)
    _loop_raising(monkeypatch, RuntimeError("the tool crashed"))

    with pytest.raises(RuntimeError, match="the tool crashed"):
        await _widget_turn()

    assert _count(platform.redis, platform.ceiling_key) == 1
    assert _per_user_counters(platform.redis) == []


# ---------------------------------------------------------------------------
# Assignment runs no user can be attributed to
# ---------------------------------------------------------------------------


def _accountless_run():
    """A legacy company-managed row: no run user, no agent owner."""
    run = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=None,
        assignment_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        status="pending",
        error=None,
        started_at=None,
        is_company_managed=True,
        trigger_context=None,
        trigger_payload=None,
    )
    assignment = SimpleNamespace(
        enabled=True,
        name="Legacy company assignment",
        is_company_managed=True,
        user_id=None,
        trigger_config={},
    )
    agent = SimpleNamespace(id=run.agent_id, enabled=True, user_id=None, is_builtin=False)
    return run, assignment, agent


def _engine(monkeypatch, run, assignment, agent):
    from app.services import assignment_engine

    session = _FakeSession([run, assignment, agent, run.trigger_context])
    monkeypatch.setattr(assignment_engine, "async_session", lambda: session)
    stub_agentic_gate(monkeypatch)
    return assignment_engine


async def test_an_assignment_run_with_no_user_is_charged_to_the_platform_wide_tiers(platform, monkeypatch):
    run, assignment, agent = _accountless_run()
    engine = _engine(monkeypatch, run, assignment, agent)

    async def _stop(*_args, **_kwargs):
        raise _StopAfterQuotaGate

    monkeypatch.setattr(engine, "_publish_run_status", _stop)

    with pytest.raises(_StopAfterQuotaGate):
        await engine.execute_assignment_run(str(run.id))

    assert _count(platform.redis, platform.ceiling_key) == 1
    assert _per_user_counters(platform.redis) == []
    assert _executions(platform.redis) == 1
    # The durable charge the dead-letter path reads: no user behind it.
    assert run.trigger_context["quota_metered"] is True
    assert run.trigger_context["quota_metered_user_id"] is None


async def test_an_assignment_run_with_no_user_is_refused_at_the_ceiling(platform, monkeypatch):
    run, assignment, agent = _accountless_run()
    engine = _engine(monkeypatch, run, assignment, agent)
    platform.redis.store[platform.ceiling_key] = LIMIT

    failed_events: list[str] = []

    async def _record_failed(_run, error):
        failed_events.append(error)

    monkeypatch.setattr(engine, "_publish_run_failed", _record_failed)

    await engine.execute_assignment_run(str(run.id))

    # Refused before it executed: skipped, and no run.failed event.
    assert run.status == "skipped"
    assert run.error.startswith("Skipped: quota exceeded:")
    assert run.failure_cause == FailureCause.PLATFORM_QUOTA_EXCEEDED.value
    assert failed_events == []
    assert _count(platform.redis, platform.ceiling_key) == LIMIT


async def test_xtm_one_fails_an_assignment_run_with_no_user_while_the_store_is_down(
    xtm_one_store_down, monkeypatch
):
    run, assignment, agent = _accountless_run()
    engine = _engine(monkeypatch, run, assignment, agent)

    async def _must_not_publish(*_args, **_kwargs):
        raise AssertionError("an XTM One run must not execute unmetered")

    monkeypatch.setattr(engine, "_publish_run_status", _must_not_publish)

    await engine.execute_assignment_run(str(run.id))

    assert run.status == "failed"
    assert run.error == QUOTA_UNAVAILABLE_MESSAGE


@pytest.mark.parametrize(
    ("trigger_context", "charged"),
    [
        # The engine writes the user as ``None`` for a charge with no account.
        ({"quota_metered": True, "quota_metered_user_id": None}, 0),
        # A record without the key, or with a user that does not parse, says
        # nothing about whose charge it was: nothing is given back.
        ({"quota_metered": True}, 1),
        ({"quota_metered": True, "quota_metered_user_id": "not-a-uuid"}, 1),
    ],
)
async def test_an_abandoned_assignment_run_with_no_user_gets_its_charge_back(platform, trigger_context, charged):
    from app.services.orchestration_adapters.assignment import AssignmentAdapter

    run = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        status="running",
        error=None,
        trigger_context=trigger_context,
    )
    await increment_agentic_usage(None, db=DB, agent_id=run.agent_id)
    execution = SimpleNamespace(
        payload={"run_id": str(run.id)},
        last_error=None,
        state="dead_letter",
        attempt_count=3,
        max_attempts=3,
        deadline_at=None,
    )

    await AssignmentAdapter().on_dead_letter(SimpleNamespace(db=_FakeSession([run])), execution)

    assert run.status == "failed"
    assert _count(platform.redis, platform.ceiling_key) == charged


def test_the_assignment_failure_handler_hands_a_run_with_no_user_to_the_same_helpers():
    """The in-process failure handler sits behind a live loop, so it is pinned
    at source level like its neighbours in ``test_agent_failure_quota_pool``:
    it is gated on the charge alone, never on there being a user, and passes
    the run's identity - ``None`` here - to both helpers, which the tests above
    drive with ``None``."""
    from app.services import assignment_engine

    src = inspect.getsource(assignment_engine.execute_assignment_run)
    assert '_charged = bool(_quota_trace_meta.get("quota_actions_consumed"))' in src
    gate_at = src.index("if _owns_charge:")
    for helper in ("refund_agentic_usage_for_provider_outage", "refund_agentic_usage_for_failure"):
        call = re.compile(rf"await {helper}\(\s*effective_user_id,")
        assert call.search(src, gate_at), f"{helper} must receive the run's own identity"


# ---------------------------------------------------------------------------
# Settings > Usage and quotas > Quotas: platform-wide usage includes runs with no account
# ---------------------------------------------------------------------------


async def test_the_platform_wide_monitoring_row_counts_runs_with_no_account(monkeypatch):
    from tests.test_quota_usage_display import _FakeEmptyResult

    redis = _FakeRedis()
    _use_store(monkeypatch, redis)
    monkeypatch.setattr(config.settings, "platform_mode", "copilot")
    monkeypatch.setattr(
        quota_module,
        "_load_global_quota_config",
        AsyncMock(return_value={"agentic": {"platform_limit": 10, "default": 5, "period": "monthly"}}),
    )
    month = _current_period_key("monthly")
    user_id = uuid.uuid4()
    # Two runs of one user, three with no account: the platform counter holds five.
    redis.store[_usage_key(user_id, "agentic", None, month)] = 2
    redis.store[_global_usage_key("agentic", month)] = 5
    db = SimpleNamespace(execute=AsyncMock(return_value=_FakeEmptyResult()))

    result = await get_platform_quota_monitoring(db)

    rows = [row for row in result["quotas"] if row["type"] == "agentic"]
    platform_row = next(row for row in rows if row["is_global"])
    per_user_row = next(row for row in rows if not row["is_global"])
    assert platform_row["current_usage"] == 5
    # Never attributed to anybody: the per-user figures are the user's alone.
    assert per_user_row["current_usage"] == 2
    assert platform_row["active_users"] == per_user_row["active_users"] == 1

    # A store that lost the platform counter still reports the users' own usage.
    del redis.store[_global_usage_key("agentic", month)]
    result = await get_platform_quota_monitoring(db)
    platform_row = next(row for row in result["quotas"] if row["type"] == "agentic" and row["is_global"])
    assert platform_row["current_usage"] == 2
