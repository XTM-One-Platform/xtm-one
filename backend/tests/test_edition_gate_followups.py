"""Follow-ups to the background edition gate.

Each test here fails on the code without the follow-ups:

* A channel message the bot stays silent on while it is licensed - an
  anonymous sender it does not serve, the routed agent's own channel rules or
  only-mention setting, a message no agent or assistant takes - stays silent on
  Community Edition, and the router's own answer stays its own; a direct
  message or an @mention a model would answer still gets the licence sentence;
  a licence that could not be read never produces it.
* A flow held back by the gate consumes nothing: a due schedule keeps its
  window, a polled event goes back unseen with the provider cursor, and both
  fire once agentic AI is back; a state that could not be read records no skip.
* The residual findings: a scheduled assignment refused mid-tick gets its
  window back; the refused jobs of one workset subject share one retry; a task
  whose wake is refused is delivered like a task whose parent does not wake; a
  failed licence read after an edition change does not fall back to the old
  answer; a refusal is never a billing candidate; the webhook receivers let the
  refusal through as the 403 their senders are promised.
"""

from __future__ import annotations

import ast
import inspect
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import OperationalError

from app.api import deps
from app.api.v1 import platform as platform_api
from app.channels.base import ChannelType, InboundMessage
from app.services import agentic_gate, ai_state
from app.services.agentic_gate import LICENSE_REQUIRED_CODE, AgenticVerdict
from app.services.failure_causes import BILLABLE_CANDIDATE_CAUSES, FailureCause
from tests.test_background_edition_gate import (
    COMMUNITY,
    ENTERPRISE,
    _pin,
    _pin_unreadable,
    _Session,
)

# ── Channel bots: the licence refusal only where a model would answer ──────


class _PastTheGateError(Exception):
    """A step a refused message must never reach."""


def _slack(*, dm: bool = False, mentioned: bool = False) -> InboundMessage:
    metadata: dict = {"channel_type": "im" if dm else "channel"}
    if mentioned:
        metadata["bot_was_mentioned"] = True
    return InboundMessage(
        channel=ChannelType.SLACK,
        text="Did anyone look at the VPN alerts?",
        user_email="analyst@example.com",
        channel_user_id="U12345",
        channel_thread_id="D0123" if dm else "C0123:1700000000.000100",
        metadata=metadata,
    )


def _bot(**overrides) -> SimpleNamespace:
    values = {
        "id": uuid.uuid4(),
        "only_mention": False,
        "allow_anonymous": False,
        "allow_private": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _agent(**slack_rules) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="Support",
        enabled=True,
        user_id=None,
        channel_config={"slack": {"receive_enabled": True, **slack_rules}},
        available_channel_types=None,
    )


def _user() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), channel_agent_config=None)


def _patch_router(
    monkeypatch,
    *,
    bot,
    user,
    routed=None,
    bound=None,
    anonymous_agent=None,
    ga_allows: bool = False,
):
    """The router's collaborators, stubbed around the routing it decides on.

    ``routed``: the agent ``_resolve_channel_agent`` picks. ``bound``: the
    agent an existing conversation of this thread is bound to. The agent row
    ``_route_turn`` loads comes off the session. Every step past the licence
    decision raises, so a refused message proves it stored and ran nothing.
    """
    from app.channels import router

    agent_row = bound or routed or anonymous_agent
    session = _Session([agent_row] if agent_row is not None else [])
    monkeypatch.setattr(router, "async_session", lambda: session)
    monkeypatch.setattr(router, "_get_channel_bot", AsyncMock(return_value=bot))
    monkeypatch.setattr(router, "_bot_allows_receive", lambda _bot, _inbound: True)
    monkeypatch.setattr(router, "_any_agent_bypasses_whitelist", AsyncMock(return_value=False))
    monkeypatch.setattr(router, "_is_thread_active", AsyncMock(return_value=False))
    monkeypatch.setattr(router, "_is_thread_muted", AsyncMock(return_value=False))
    monkeypatch.setattr(router, "resolve_user_by_email", AsyncMock(return_value=user))
    monkeypatch.setattr(
        router, "resolve_user_by_channel_identity", AsyncMock(return_value=user)
    )
    monkeypatch.setattr(router, "link_channel_identity", AsyncMock())
    monkeypatch.setattr(
        router, "_resolve_channel_agent_anonymous", AsyncMock(return_value=anonymous_agent)
    )
    conv = SimpleNamespace(id=uuid.uuid4(), agent_id=bound.id) if bound else None
    monkeypatch.setattr(
        router, "_find_existing_external_conversation", AsyncMock(return_value=conv)
    )
    monkeypatch.setattr(router, "_find_bound_conversation", AsyncMock(return_value=None))
    monkeypatch.setattr(router, "get_conversation_history", AsyncMock(return_value=[]))
    monkeypatch.setattr(router, "_resolve_channel_agent", AsyncMock(return_value=routed))
    monkeypatch.setattr(router, "_ga_allows_channel", AsyncMock(return_value=ga_allows))
    for step in (
        "_resolve_channel_files",
        "_load_channel_backfill",
        "_compact_history",
        "_finalize_message",
    ):
        monkeypatch.setattr(router, step, AsyncMock(side_effect=_PastTheGateError))
    return session


def _silent(out) -> bool:
    return out.suppress is True and out.text == ""


@pytest.mark.asyncio
async def test_a_group_message_from_an_anonymous_sender_the_bot_does_not_serve_stays_silent(
    monkeypatch,
):
    from app.channels import router

    _pin(monkeypatch, **COMMUNITY)
    _patch_router(monkeypatch, bot=_bot(allow_anonymous=False), user=None)

    assert _silent(await router.handle_message(_slack()))


@pytest.mark.asyncio
async def test_a_group_message_the_routed_agent_does_not_take_stays_silent(monkeypatch):
    """The agent's own receive whitelist: this channel is not one of its."""
    from app.channels import router

    _pin(monkeypatch, **COMMUNITY)
    restricted = _agent(receive_channels=[{"id": "C0999"}])
    _patch_router(monkeypatch, bot=_bot(), user=_user(), bound=restricted)

    assert _silent(await router.handle_message(_slack()))


@pytest.mark.asyncio
async def test_a_group_message_without_a_mention_stays_silent_under_the_agents_only_mention(
    monkeypatch,
):
    """The bot listens to everything; the agent answers only an @mention."""
    from app.channels import router

    _pin(monkeypatch, **COMMUNITY)
    mention_only = _agent(only_mention=True)
    _patch_router(monkeypatch, bot=_bot(only_mention=False), user=_user(), bound=mention_only)

    assert _silent(await router.handle_message(_slack()))


@pytest.mark.asyncio
async def test_a_group_message_no_agent_or_assistant_takes_stays_silent(monkeypatch):
    from app.channels import router

    _pin(monkeypatch, **COMMUNITY)
    _patch_router(monkeypatch, bot=_bot(), user=_user(), routed=None, ga_allows=False)

    assert _silent(await router.handle_message(_slack()))


@pytest.mark.asyncio
async def test_the_routers_own_answer_stays_its_own_on_community_edition(monkeypatch):
    """An anonymous direct message no agent serves is told so, licensed or not:
    the licence sentence replaces a model's answer, never a routing answer."""
    from app.channels import router

    _pin(monkeypatch, **COMMUNITY)
    _patch_router(
        monkeypatch, bot=_bot(allow_anonymous=True), user=None, anonymous_agent=None
    )

    out = await router.handle_message(_slack(dm=True))

    assert out.suppress is False
    assert out.text.startswith("I couldn't find a suitable agent")
    assert deps.licence_required_detail() not in out.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "inbound", [_slack(dm=True), _slack(mentioned=True)], ids=["direct", "mention"]
)
async def test_a_message_a_model_would_answer_gets_the_licence_sentence(monkeypatch, inbound):
    """Unchanged where it applies today: a direct message or an @mention the
    routed agent answers. Nothing is stored and nothing past routing runs."""
    from app.channels import router

    _pin(monkeypatch, **COMMUNITY)
    session = _patch_router(monkeypatch, bot=_bot(), user=_user(), routed=_agent())

    out = await router.handle_message(inbound)

    assert out.text == deps.chat_unavailable_detail()
    assert deps.licence_required_detail() in out.text
    assert out.suppress is False
    assert session.commits == 0 and session.added == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "inbound", [_slack(dm=True), _slack(mentioned=True)], ids=["direct", "mention"]
)
async def test_an_unreadable_licence_never_produces_the_licence_sentence(monkeypatch, inbound):
    """A cold process during a database failover: not a refusal. A direct
    message is told the AI service is temporarily unavailable, a group channel
    (an @mention included) gets silence
    (``test_channel_unreadable_ai_state``)."""
    from app.channels import router

    _pin_unreadable(monkeypatch, half="licence")
    _patch_router(monkeypatch, bot=_bot(), user=_user(), routed=_agent())

    out = await router.handle_message(inbound)

    if router._is_private_conversation(inbound):
        assert out.text == deps.AI_TEMPORARILY_UNAVAILABLE_DETAIL
    else:
        assert out.suppress is True and out.text == ""
    assert deps.licence_required_detail() not in out.text


@pytest.mark.asyncio
async def test_an_unreadable_licence_keeps_a_silent_message_silent(monkeypatch):
    from app.channels import router

    _pin_unreadable(monkeypatch, half="licence")
    _patch_router(monkeypatch, bot=_bot(allow_anonymous=False), user=None)

    assert _silent(await router.handle_message(_slack()))


@pytest.mark.asyncio
async def test_a_routed_message_on_enterprise_edition_goes_on_to_the_model(monkeypatch):
    from app.channels import router

    _pin(monkeypatch, **ENTERPRISE)
    _patch_router(monkeypatch, bot=_bot(), user=_user(), routed=_agent())

    with pytest.raises(_PastTheGateError):
        await router.handle_message(_slack(dm=True))


def test_the_router_decides_step_six_once_for_both_paths():
    """One routing decision for the model path and the licence refusal, so the
    two cannot disagree on where the bot answers."""
    from app.channels import router

    source = inspect.getsource(router.handle_message)
    assert source.count("await _route_turn(") == 1
    route = source.index("await _route_turn(")
    licence = source.index("if licence_refused:\n")
    files = source.index("await _resolve_channel_files(")
    assert route < licence < files, "decided before any file, backfill or compaction work"


# ── Flow triggers: held back, never consumed ───────────────────────────────


class _FakeRedis:
    def __init__(self, values: dict | None = None):
        self.values: dict = dict(values or {})
        self.sets: dict[str, set] = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, ex=None):
        self.values[key] = value

    async def delete(self, key):
        self.values.pop(key, None)

    async def sadd(self, key, member):
        members = self.sets.setdefault(key, set())
        if member in members:
            return 0
        members.add(member)
        return 1

    async def srem(self, key, member):
        self.sets.get(key, set()).discard(member)

    async def expire(self, key, _seconds):
        return True


def _flow(trigger: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="Mail triage",
        version_number=2,
        user_id=uuid.uuid4(),
        graph={"nodes": [trigger, {"id": "a1", "kind": "agent", "config": {}}], "edges": []},
        is_company_managed=False,
        shared_group_ids=None,
        shared_user_ids=None,
        access_restricted=False,
        last_triggered_at=None,
    )


def _patch_flow_triggers(monkeypatch, flow, node):
    """The flow and its trigger node; the owner sees the trigger integration
    (pinned by ``test_trigger_integration_delivery``)."""
    from app.services import flow_triggers

    monkeypatch.setattr(
        flow_triggers, "_active_flows_with_trigger", AsyncMock(return_value=[(flow, [node])])
    )
    monkeypatch.setattr(
        "app.services.trigger_integration_access.TriggerIntegrationVisibility.sees",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(flow_triggers, "has_active_run", AsyncMock(return_value=False))
    # A webhook's admission (advisory lock + active-run count) is its own query.
    monkeypatch.setattr(flow_triggers, "webhook_run_admitted", AsyncMock(return_value=True))
    created = AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4()))
    monkeypatch.setattr(flow_triggers, "create_flow_run", created)
    monkeypatch.setattr(
        "app.services.orchestration_engine.notify_orchestration_wake", AsyncMock()
    )
    own = _Session()
    monkeypatch.setattr(flow_triggers, "async_session", lambda: own)
    return created, own


_EVENT_NODE = {
    "id": "t1",
    "kind": "trigger.integration_event",
    "config": {"integration_id": str(uuid.uuid4()), "event_type": "new_email"},
}


def _patch_poll(monkeypatch, redis: _FakeRedis) -> None:
    monkeypatch.setattr("app.core.redis.redis_client", AsyncMock(return_value=redis))
    monkeypatch.setattr("app.core.security.decrypt_value", lambda _value: "token")
    monkeypatch.setattr(
        "app.core.database.release_db_connection_before_loop", AsyncMock()
    )

    async def _poll(_provider, _event_type, _token, target, redis_, _seen, _http):
        # Every provider poller advances the cursor before returning.
        redis_.values[f"event_cursor:{target.id}"] = "2026-09-27T01:00:00Z"
        return [{"message_id": "m-1", "subject": "VPN"}, {"message_id": "m-2"}]

    monkeypatch.setattr("app.services.event_poller._poll_provider", _poll)


def _integration() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), provider="microsoft", access_token_enc="enc")


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor_before", [None, "2026-09-26T09:00:00Z"])
async def test_an_event_held_back_is_not_consumed_and_fires_once_ai_is_back(
    monkeypatch, cursor_before
):
    from app.services import flow_triggers

    flow = _flow(_EVENT_NODE)
    target = f"flow:{flow.id}:{_EVENT_NODE['id']}"
    cursor_key = f"event_cursor:{target}"
    redis = _FakeRedis({cursor_key: cursor_before} if cursor_before else {})
    created, own = _patch_flow_triggers(monkeypatch, flow, _EVENT_NODE)
    _patch_poll(monkeypatch, redis)

    _pin(monkeypatch, **COMMUNITY)
    triggered = await flow_triggers.poll_flow_integration_events(
        _Session([_integration(), None, None])
    )

    assert triggered == 0
    created.assert_not_awaited()
    assert redis.sets.get(f"event_seen_ids:{target}", set()) == set(), "nothing consumed"
    assert redis.values.get(cursor_key) == cursor_before, "the cursor goes back"
    (skipped,) = own.added
    assert skipped.status == "skipped"

    # Agentic AI is back: the next poll dispatches the held event.
    _pin(monkeypatch, **ENTERPRISE)
    redis.values.pop(f"event_poll:{target}")
    triggered = await flow_triggers.poll_flow_integration_events(_Session([_integration()]))

    assert triggered == 2
    first = created.await_args_list[0].kwargs
    assert first["trigger_payload"]["payload"]["message_id"] == "m-1"
    assert redis.sets[f"event_seen_ids:{target}"] == {"m-1", "m-2"}


@pytest.mark.asyncio
@pytest.mark.parametrize("half", ["licence", "switch"])
async def test_an_unreadable_state_records_no_skip_and_consumes_no_event(monkeypatch, half):
    """Either half of the gate unreadable: the licence (a cold process during a
    failover) or the AI switch (``ai_state`` fails closed on a read error)."""
    from app.services import flow_triggers

    flow = _flow(_EVENT_NODE)
    target = f"flow:{flow.id}:{_EVENT_NODE['id']}"
    redis = _FakeRedis()
    created, own = _patch_flow_triggers(monkeypatch, flow, _EVENT_NODE)
    _patch_poll(monkeypatch, redis)

    _pin_unreadable(monkeypatch, half=half)
    triggered = await flow_triggers.poll_flow_integration_events(
        _Session([_integration(), None, None])
    )

    assert triggered == 0
    created.assert_not_awaited()
    assert own.added == [], "an unreadable state is not 'not licensed'"
    assert redis.sets.get(f"event_seen_ids:{target}", set()) == set()


_SCHEDULE_NODE = {"id": "s1", "kind": "trigger.schedule", "config": {"cron": "* * * * *"}}


@pytest.mark.asyncio
async def test_a_schedule_held_back_keeps_its_window_and_catches_up_once_ai_is_back(
    monkeypatch,
):
    from app.services import flow_triggers

    flow = _flow(_SCHEDULE_NODE)
    created, own = _patch_flow_triggers(monkeypatch, flow, _SCHEDULE_NODE)

    _pin(monkeypatch, **COMMUNITY)
    held_db = _Session([None, None])
    assert await flow_triggers.process_scheduled_flows(held_db) == 0

    assert flow.last_triggered_at is None, "the window stays open"
    assert held_db.commits == 0
    created.assert_not_awaited()
    (skipped,) = own.added
    assert skipped.status == "skipped"

    _pin(monkeypatch, **ENTERPRISE)
    back_db = _Session()
    assert await flow_triggers.process_scheduled_flows(back_db) == 1

    created.assert_awaited_once()
    assert flow.last_triggered_at is not None
    assert back_db.commits == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("half", ["licence", "switch"])
async def test_an_unreadable_state_keeps_a_schedule_window_and_records_no_skip(
    monkeypatch, half
):
    from app.services import flow_triggers

    flow = _flow(_SCHEDULE_NODE)
    created, own = _patch_flow_triggers(monkeypatch, flow, _SCHEDULE_NODE)

    _pin_unreadable(monkeypatch, half=half)
    db = _Session()
    assert await flow_triggers.process_scheduled_flows(db) == 0

    assert flow.last_triggered_at is None, "the window stays open"
    assert db.commits == 0
    created.assert_not_awaited()
    assert own.added == [], "an unreadable state is not 'not licensed'"


@pytest.mark.asyncio
@pytest.mark.parametrize("half", ["licence", "switch"])
async def test_an_unreadable_state_leaves_a_webhook_for_its_sender_to_retry(monkeypatch, half):
    """``None`` is the 429 the sender retries; nothing is recorded."""
    from app.services import flow_triggers

    node = {"id": "w1", "kind": "trigger.webhook", "config": {"path": "alerts"}}
    flow = _flow(node)
    created, own = _patch_flow_triggers(monkeypatch, flow, node)

    _pin_unreadable(monkeypatch, half=half)
    run_id = await flow_triggers.trigger_flow_from_webhook(
        _Session([None, None]), flow, node, payload={}, headers={}
    )

    assert run_id is None
    created.assert_not_awaited()
    assert own.added == []


@pytest.mark.asyncio
@pytest.mark.parametrize("half", ["licence", "switch"])
async def test_an_unreadable_state_claims_no_work_item_and_records_no_skip(
    monkeypatch, half
):
    """Asked before the claim: the item stays on the queue for every worker."""
    from app.services import flow_triggers

    node = {
        "id": "q1",
        "kind": "trigger.work_item",
        "config": {"owner_agent_id": str(uuid.uuid4())},
    }
    flow = _flow(node)
    created, own = _patch_flow_triggers(monkeypatch, flow, node)
    listed = AsyncMock(return_value=[{"id": str(uuid.uuid4())}])
    claim = AsyncMock(return_value={"claimed": True})
    monkeypatch.setattr("app.services.work_item.list_dispatchable_work_items", listed)
    monkeypatch.setattr("app.services.work_item.claim_work_item", claim)

    _pin_unreadable(monkeypatch, half=half)
    assert await flow_triggers.process_work_item_flows(_Session()) == 0

    listed.assert_not_awaited()
    claim.assert_not_awaited()
    created.assert_not_awaited()
    assert own.added == []


# ── Scheduled assignments: a mid-tick refusal gives the window back ────────


@pytest.mark.asyncio
async def test_a_scheduled_assignment_refused_mid_tick_keeps_its_window(monkeypatch):
    """The tick asked and AI was available; the edition changed before the
    trigger. Refused before any run exists, the window goes back - and the
    assignments left are not tried one by one."""
    from app.services import autonomous

    previous = datetime(2026, 9, 1, tzinfo=UTC)
    agent = SimpleNamespace(id=uuid.uuid4(), name="Analyst", enabled=True, is_builtin=False)

    def _scheduled(name: str) -> SimpleNamespace:
        return SimpleNamespace(
            id=uuid.uuid4(),
            name=name,
            agent_id=agent.id,
            trigger_config={"cron": "* * * * *"},
            last_triggered_at=previous,
            run_as_all_users=False,
            is_company_managed=False,
        )

    first, second = _scheduled("Morning brief"), _scheduled("Evening brief")
    refusal = HTTPException(
        403, detail={"code": LICENSE_REQUIRED_CODE, "message": "Enterprise Edition required."}
    )
    trigger = AsyncMock(side_effect=refusal)
    monkeypatch.setattr(autonomous, "trigger_assignment", trigger)
    monkeypatch.setattr(autonomous, "has_active_run", AsyncMock(return_value=False))
    db = _Session([[first, second], [agent]])

    assert await autonomous._process_scheduled_assignments(db) == 0

    assert first.last_triggered_at == previous
    assert second.last_triggered_at == previous
    assert trigger.await_count == 1


# ── Workset jobs: one retry per subject ────────────────────────────────────


@pytest.mark.asyncio
async def test_the_refused_jobs_of_one_subject_share_one_retry(monkeypatch):
    """SAQ keeps one job per key queued, scheduled or running: two clustering
    passes of one workset refused together ask for the same retry key, so they
    become one retry chain; a retry refused again asks for a later one."""
    from app import worker

    enqueue = AsyncMock()
    monkeypatch.setattr(worker, "queue", SimpleNamespace(enqueue=enqueue))
    interval = worker._AI_UNAVAILABLE_RETRY_SECONDS
    clock = SimpleNamespace(now=float(interval * 3_000_000))
    monkeypatch.setattr(worker, "time", SimpleNamespace(time=lambda: clock.now))

    async def _ask() -> None:
        assert await worker._ask_again_later(
            "workset_cluster_job", "workset-cluster-ws-1", timeout=600, workset_id="ws-1"
        )

    await _ask()
    clock.now += 42
    await _ask()
    clock.now += interval
    await _ask()

    first, second, later = (call.args[0] for call in enqueue.await_args_list)
    assert first.key == second.key, "refused together: one retry"
    assert later.key != first.key, "a retry asks for a later slot, never its own key"
    assert first.key.startswith("workset-cluster-ws-1-")
    assert first.scheduled == int(interval * 3_000_000) + interval
    assert first.kwargs == {"workset_id": "ws-1"}
    assert first.timeout == 600


# ── A refused wake: delivered like a task whose parent does not wake ───────


def _task(status: str = "completed") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        status=status,
        conversation_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        target_agent_id=uuid.uuid4(),
        metadata_json={},
        result="Three new exposures, all on the VPN edge." if status == "completed" else None,
        error=None if status == "completed" else "Timed out.",
    )


def _conversation(task) -> SimpleNamespace:
    return SimpleNamespace(
        id=task.conversation_id,
        user_id=task.user_id,
        agent_id=uuid.uuid4(),
        channel="web",
        channel_thread_id=None,
        tool_overrides_json=None,
    )


async def _patch_refused_wake(monkeypatch, task, *, disable_chat: bool = False):
    """The wake's session, then the delivery's own read of the task and its
    target agent; the notice itself is ``sub_agent._deliver_task_result``.

    The licence is read (and cached) first: its read opens a session of its
    own, which would otherwise take one of these."""
    from app.services import parent_wake

    await agentic_gate.is_licensed()
    target = SimpleNamespace(id=task.target_agent_id, disable_chat=disable_chat)
    sessions = iter([_Session([task, _conversation(task)]), _Session([task, target])])
    monkeypatch.setattr("app.core.database.async_session", lambda: next(sessions))
    store = AsyncMock()
    monkeypatch.setattr("app.channels.router.store_message", store)
    acquire = AsyncMock(return_value=(None, True))
    monkeypatch.setattr("app.services.chat_turn_guard.acquire_or_fold", acquire)
    monkeypatch.setattr("app.services.sub_agent.publish_task_event", AsyncMock())
    monkeypatch.setattr(parent_wake, "load_result_file_refs", AsyncMock(return_value=[]))
    deliver = AsyncMock()
    monkeypatch.setattr("app.services.sub_agent._deliver_task_result", deliver)
    return store, acquire, deliver


async def _wake(task, terminal_status: str) -> None:
    from app.services import parent_wake

    await parent_wake._wake_parent_conversation(
        task_id=str(task.id),
        conversation_id=str(task.conversation_id),
        user_id=str(task.user_id),
        agent_name="Exposure analyst",
        terminal_status=terminal_status,
    )


@pytest.mark.asyncio
async def test_a_task_whose_wake_is_refused_gets_the_completion_notice(monkeypatch):
    """No turn carried the task's files: the notice does, as attachments a
    later turn rehydrates - the delivery a parent that does not wake gets."""
    _pin(monkeypatch, **COMMUNITY)
    task = _task()
    store, acquire, deliver = await _patch_refused_wake(monkeypatch, task)

    await _wake(task, "completed")

    store.assert_awaited_once()  # the hidden wake text, kept in the history
    acquire.assert_not_awaited()
    deliver.assert_awaited_once_with(
        task_id=str(task.id),
        conversation_id=str(task.conversation_id),
        agent_name="Exposure analyst",
        result_text=task.result,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "disable_chat"), [("failed", False), ("completed", True)], ids=["failed", "no_chat"]
)
async def test_a_refused_wake_delivers_only_where_a_non_waking_parent_would(
    monkeypatch, status, disable_chat
):
    """A failed task never gets a completion notice, nor does a task of an
    agent chat is disabled for - the non-waking parent's own conditions."""
    _pin(monkeypatch, **COMMUNITY)
    task = _task(status)
    _store, _acquire, deliver = await _patch_refused_wake(monkeypatch, task, disable_chat=disable_chat)

    await _wake(task, status)

    deliver.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_wake_on_enterprise_edition_gets_no_completion_notice(monkeypatch):
    _pin(monkeypatch, **ENTERPRISE)
    task = _task()
    _store, acquire, deliver = await _patch_refused_wake(monkeypatch, task)

    await _wake(task, "completed")

    acquire.assert_awaited_once()
    deliver.assert_not_awaited()


# ── The licence fallback does not outlive an edition change ────────────────


@pytest.mark.asyncio
async def test_a_failed_read_after_an_edition_change_does_not_fall_back_to_the_old_answer(
    monkeypatch,
):
    state = {"fail": False}

    async def _licence(_db):
        if state["fail"]:
            raise OperationalError("SELECT 1", {}, Exception("database failover"))
        return True

    async def _ai(_db):
        return True

    monkeypatch.setattr(platform_api, "is_agentic_ai_licensed", _licence)
    monkeypatch.setattr(ai_state, "is_ai_enabled", _ai)
    ai_state.prime_cache(True)

    assert await agentic_gate.is_licensed() is True
    agentic_gate.invalidate_cache()  # e.g. the Filigran license was just removed
    state["fail"] = True

    assert await agentic_gate.is_licensed() is False
    verdict = await agentic_gate.agentic_verdict(object())
    assert verdict == AgenticVerdict(LICENSE_REQUIRED_CODE, transient=True)

    state["fail"] = False
    assert await agentic_gate.is_licensed() is True


# ── Refusals are never billed; webhook senders get the 403 ─────────────────


def test_a_refusal_is_never_a_billing_candidate():
    """The platform declined to run before any quota was touched: there is
    nothing a billing decision could be about."""
    assert FailureCause.AI_DISABLED not in BILLABLE_CANDIDATE_CAUSES
    assert FailureCause.LICENSE_REQUIRED not in BILLABLE_CANDIDATE_CAUSES


def test_the_webhook_receivers_let_the_refusal_through_as_a_403():
    """Code and docstring agree: every receiver in ``webhooks.py`` answers its
    sender the 403 (the contract documented for webhook senders); only the
    change-notification receivers catch the refusal to keep answering 200."""
    from app.api.v1 import integration_notifications, webhooks
    from app.services import assignment_engine

    def _names(module) -> set[str]:
        tree = ast.parse(inspect.getsource(module))
        return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }

    assert "refusal_code" not in _names(webhooks)
    assert "refusal_code" in _names(integration_notifications)
    doc = inspect.getdoc(assignment_engine._refuse_if_ai_disabled) or ""
    assert "integration_notifications.py" in doc
    assert "answers its sender that 403" in doc
