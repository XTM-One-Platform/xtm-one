"""Microsoft Graph subscriptions deliver integration events within seconds.

An enabled Microsoft integration-event assignment is subscribed when the
platform has a public https URL: created with a random clientState the
notification endpoint verifies, renewed before it expires, retired when the
assignment no longer wants it. While its record lives the poller skips the
assignment; a refused, removed or lapsed subscription drops the record and the
poller reads on from the last event notified, the shared seen set running each
event once. Graph, Redis and the database are replaced by fakes.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.models.agent import Agent
from app.models.assignment import Assignment
from app.models.user import User
from app.services import event_poller
from app.services import graph_event_subscriptions as subs
from app.services.integration_notification_auth import graph_subscription_key
from tests.integration_credential_fakes import FakeSession

OWNER = uuid.uuid4()


class _Redis:
    def __init__(self):
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.sets: dict[str, set] = {}
        self.hashes: dict[str, dict] = {}

    async def get(self, key):
        return self.values.get(key)

    async def mget(self, keys):
        return [self.values.get(key) for key in keys]

    async def set(self, key, value, ex=None, nx=False, **_k):
        if nx and key in self.values:
            return None
        self.values[key] = value
        if ex:
            self.ttls[key] = ex
        return True

    async def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)
            self.ttls.pop(key, None)

    async def expire(self, key, ttl):
        self.ttls[key] = ttl
        return True

    async def sadd(self, key, *members):
        self.sets.setdefault(key, set()).update(members)

    async def srem(self, key, *members):
        self.sets.setdefault(key, set()).difference_update(members)

    async def smembers(self, key):
        return set(self.sets.get(key, set()))

    async def hmget(self, key, fields):
        return [self.hashes.get(key, {}).get(f) for f in fields]

    async def hset(self, key, mapping):
        self.hashes.setdefault(key, {}).update(mapping)


class _Resp:
    def __init__(self, status_code: int, data: dict | None = None):
        self.status_code = status_code
        self._data = data or {}
        self.text = json.dumps(self._data)

    def json(self):
        return self._data


class _Graph:
    """Graph's subscription API and resource reads."""

    def __init__(self, *, create_status: int = 201, renew_status: int = 200, resources: dict | None = None):
        self.create_status = create_status
        self.renew_status = renew_status
        self.resources = resources or {}
        self.calls: list[tuple[str, str, dict | None]] = []
        self._next = 0

    async def request(self, method, url, headers=None, json=None, timeout=None):
        self.calls.append((method, url, json))
        if method == "POST":
            self._next += 1
            return _Resp(self.create_status, {"id": f"sub-{self._next}"} if self.create_status == 201 else {"error": "no"})
        if method == "PATCH":
            return _Resp(self.renew_status)
        return _Resp(204)

    async def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append(("GET", url, params))
        for key, data in self.resources.items():
            if url.endswith(key):
                return _Resp(200, data)
        return _Resp(404)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


@pytest.fixture
def _public(monkeypatch):
    monkeypatch.setattr(settings, "base_url", "https://xtm.example.com")
    monkeypatch.setattr(settings, "integration_event_graph_subscriptions_enabled", True)


def _assignment(event="new_email", filter_config=None, **extra):
    config = {"provider": "microsoft", "event": event, "event_matching": "exact"}
    if filter_config is not None:
        config["filter"] = filter_config
    return SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=extra.pop("agent_id", uuid.uuid4()),
        enabled=True,
        trigger_type="integration_event",
        trigger_config=config,
        user_id=OWNER,
        is_company_managed=False,
        name="Inbox",
        **extra,
    )


def _db(assignments, agents):
    owner = User(id=OWNER, email="owner@test.local", full_name="O", hashed_password="x", role="user")
    return FakeSession({Assignment: list(assignments), Agent: list(agents), User: [owner]})


def _agent(agent_id):
    return SimpleNamespace(id=agent_id, user_id=OWNER, enabled=True, integration_bindings={"microsoft": [str(uuid.uuid4())]})


@pytest.fixture
def _graph_env(monkeypatch, _public):
    redis = _Redis()
    graph = _Graph()

    async def _redis_client():
        return redis

    async def _token(_db, _wf, _agent):
        return "tok", None

    monkeypatch.setattr("app.core.redis.redis_client", _redis_client)
    monkeypatch.setattr(subs, "graph_token_for", _token)
    monkeypatch.setattr(subs, "egress_client", lambda: graph)
    return redis, graph


# ---------------------------------------------------------------------------
# What a trigger subscribes to, and when the platform can subscribe at all
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("event", "filter_config", "resources"),
    [
        # No folder: every folder, as the poll reads (a mail a rule files
        # outside the inbox is notified too).
        ("new_email", None, (("me/messages", "created"),)),
        ("new_email", {"from": {"allow": ["ovh.com"]}}, (("me/messages", "created"),)),
        ("new_email", {"folder": "archive"}, (("me/mailFolders('archive')/messages", "created"),)),
        ("email_flag_changed", None, (("me/messages", "updated"),)),
        ("calendar_event_changed", None, (("me/events", "created,updated,deleted"),)),
        (
            "new_teams_reply",
            {"scope": {"teams_channel": ["t1:19:c1@thread.tacv2", "t2:19:c2@thread.tacv2"]}},
            (
                ("teams/t1/channels/19:c1@thread.tacv2/messages", "created"),
                ("teams/t2/channels/19:c2@thread.tacv2/messages", "created"),
            ),
        ),
        ("new_teams_chat_message", {"chat_ids": ["19:abc@thread.v2"]}, (("chats/19:abc@thread.v2/messages", "created"),)),
    ],
)
def test_each_event_subscribes_to_its_resource(event, filter_config, resources):
    config = {"event": event, **({"filter": filter_config} if filter_config else {})}
    plan = subs.subscription_plan(config)
    assert plan.reason is None and plan.resources == resources


@pytest.mark.parametrize("event", ["new_teams_message", "new_teams_chat_message", "new_slack_message"])
def test_a_trigger_naming_no_target_stays_polled_and_says_why(event):
    plan = subs.subscription_plan({"event": event})
    assert plan.resources == () and plan.reason


@pytest.mark.parametrize(
    ("base_url", "enabled", "reachable"),
    [
        ("https://xtm.example.com", True, True),
        ("https://xtm.example.com/xtm-one", True, True),
        ("https://xtm.example.com", False, False),
        ("http://xtm.example.com", True, False),
        ("https://localhost:8100", True, False),
        ("https://10.1.2.3", True, False),
        ("https://xtm.corp.local", True, False),
    ],
)
def test_only_a_public_https_url_receives_notifications(monkeypatch, base_url, enabled, reachable):
    monkeypatch.setattr(settings, "base_url", base_url)
    monkeypatch.setattr(settings, "integration_event_graph_subscriptions_enabled", enabled)
    url = subs.public_notification_url()
    assert (url is not None) is reachable
    if reachable:
        assert url == f"{base_url}/api/v1/integrations/notifications/microsoft"


# ---------------------------------------------------------------------------
# Create, renew, lapse, retire
# ---------------------------------------------------------------------------


async def test_a_mail_assignment_is_subscribed_and_the_poller_skips_it(_graph_env):
    redis, graph = _graph_env
    wf = _assignment()
    counts = await subs.maintain_graph_event_subscriptions(_db([wf], [_agent(wf.agent_id)]), force=True)

    assert counts["created"] == 1
    (post,) = [call for call in graph.calls if call[0] == "POST"]
    body = post[2]
    assert body["resource"] == "me/messages"
    assert body["changeType"] == "created"
    assert body["notificationUrl"].endswith("/api/v1/integrations/notifications/microsoft")
    assert body["lifecycleNotificationUrl"] == body["notificationUrl"]
    # The clientState the endpoint verifies is recorded, never in the clear.
    stored = json.loads(redis.values[graph_subscription_key("sub-1")])
    assert stored["assignment_id"] == str(wf.id) and body["clientState"] not in stored["client_state"]
    record = json.loads(redis.values[f"graph_sub_wf:{wf.id}"])
    assert [s["id"] for s in record["subscriptions"]] == ["sub-1"]
    # A lapse polls on from here, never the mailbox's latest page.
    assert redis.values[f"event_cursor:{wf.id}"]
    mode = await subs.delivery_mode(redis, wf)
    assert mode["mode"] == "subscription" and mode["subscriptions"] == 1


async def test_a_pass_is_rate_limited_unless_forced(_graph_env):
    _redis, graph = _graph_env
    wf = _assignment()
    db = _db([wf], [_agent(wf.agent_id)])
    await subs.maintain_graph_event_subscriptions(db)
    await subs.maintain_graph_event_subscriptions(db)
    assert len([c for c in graph.calls if c[0] == "POST"]) == 1


async def test_a_subscription_close_to_expiry_is_renewed(_graph_env):
    redis, graph = _graph_env
    wf = _assignment()
    db = _db([wf], [_agent(wf.agent_id)])
    await subs.maintain_graph_event_subscriptions(db, force=True)
    record = json.loads(redis.values[f"graph_sub_wf:{wf.id}"])
    record["subscriptions"][0]["expires_at"] = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    redis.values[f"graph_sub_wf:{wf.id}"] = json.dumps(record)

    counts = await subs.maintain_graph_event_subscriptions(db, force=True)

    assert counts["renewed"] == 1
    (patch,) = [call for call in graph.calls if call[0] == "PATCH"]
    assert patch[1].endswith("/subscriptions/sub-1")
    renewed = json.loads(redis.values[f"graph_sub_wf:{wf.id}"])
    assert datetime.fromisoformat(renewed["subscriptions"][0]["expires_at"]) > datetime.now(UTC) + timedelta(days=2)


async def test_a_subscription_graph_no_longer_holds_hands_the_assignment_back_to_polling(_graph_env):
    redis, graph = _graph_env
    wf = _assignment()
    db = _db([wf], [_agent(wf.agent_id)])
    await subs.maintain_graph_event_subscriptions(db, force=True)
    redis.values[f"graph_sub_renew:{wf.id}"] = "1"
    graph.renew_status = 404

    await subs.maintain_graph_event_subscriptions(db, force=True)

    assert f"graph_sub_wf:{wf.id}" not in redis.values
    assert graph_subscription_key("sub-1") not in redis.values
    assert (await subs.delivery_mode(redis, wf))["mode"] == "poll"


async def test_a_subscription_graph_refuses_leaves_the_assignment_polled_with_the_reason(_graph_env):
    redis, graph = _graph_env
    graph.create_status = 403
    wf = _assignment()
    counts = await subs.maintain_graph_event_subscriptions(_db([wf], [_agent(wf.agent_id)]), force=True)

    assert counts["failed"] == 1
    assert f"graph_sub_wf:{wf.id}" not in redis.values
    mode = await subs.delivery_mode(redis, wf)
    assert mode["mode"] == "poll" and "not created" in mode["reason"] and mode["interval_seconds"] == 300


async def test_one_channel_refused_creates_none_of_the_others(_graph_env):
    redis, graph = _graph_env
    wf = _assignment("new_teams_reply", {"channels": ["t1:c1", "t1:c2"]})
    calls = {"n": 0}
    original = graph.request

    async def _second_refused(method, url, **kwargs):
        if method == "POST":
            calls["n"] += 1
            if calls["n"] == 2:
                return _Resp(409, {"error": "exists"})
        return await original(method, url, **kwargs)

    graph.request = _second_refused
    await subs.maintain_graph_event_subscriptions(_db([wf], [_agent(wf.agent_id)]), force=True)

    assert f"graph_sub_wf:{wf.id}" not in redis.values
    assert [c for c in graph.calls if c[0] == "DELETE"], "the first channel's subscription is deleted"


async def test_switching_subscriptions_off_retires_every_subscription(_graph_env, monkeypatch):
    redis, graph = _graph_env
    wf = _assignment()
    db = _db([wf], [_agent(wf.agent_id)])
    await subs.maintain_graph_event_subscriptions(db, force=True)
    monkeypatch.setattr(settings, "integration_event_graph_subscriptions_enabled", False)

    async def _token_of(_db, _assignment_id):
        return "tok", None

    monkeypatch.setattr(subs, "_token_of_assignment", _token_of)
    counts = await subs.maintain_graph_event_subscriptions(db, force=True)

    assert counts["retired"] == 1
    assert next(c for c in graph.calls if c[0] == "DELETE")[1].endswith("/subscriptions/sub-1")
    assert f"graph_sub_wf:{wf.id}" not in redis.values
    mode = await subs.delivery_mode(redis, wf)
    assert mode["mode"] == "poll" and "switched off" in mode["reason"]


async def test_a_retargeted_trigger_gets_new_subscriptions(_graph_env):
    _redis, graph = _graph_env
    wf = _assignment()
    db = _db([wf], [_agent(wf.agent_id)])
    await subs.maintain_graph_event_subscriptions(db, force=True)
    wf.trigger_config = {**wf.trigger_config, "filter": {"folder": "archive"}}

    await subs.maintain_graph_event_subscriptions(db, force=True)

    posts = [c[2]["resource"] for c in graph.calls if c[0] == "POST"]
    assert posts == ["me/messages", "me/mailFolders('archive')/messages"]
    assert [c for c in graph.calls if c[0] == "DELETE"]


# ---------------------------------------------------------------------------
# Lifecycle notifications
# ---------------------------------------------------------------------------


async def test_lifecycle_notifications_renew_resume_polling_or_catch_up():
    redis = _Redis()
    aid = str(uuid.uuid4())
    redis.values[f"graph_sub_wf:{aid}"] = json.dumps({"subscriptions": [{"id": "s1", "expires_at": "2099-01-01T00:00:00+00:00"}]})
    redis.values[graph_subscription_key("s1")] = "{}"

    assert await subs.handle_lifecycle_notification(redis, aid, {"lifecycleEvent": "reauthorizationRequired"}) == "renewal requested"
    assert redis.values[f"graph_sub_renew:{aid}"] == "1"
    assert await subs.handle_lifecycle_notification(redis, aid, {"lifecycleEvent": "missed"}) == "catch-up poll requested"
    assert redis.values[f"graph_sub_catchup:{aid}"] == "1"
    assert await subs.handle_lifecycle_notification(redis, aid, {"lifecycleEvent": "subscriptionRemoved", "subscriptionId": "s1"}) == "polling resumed"
    assert f"graph_sub_wf:{aid}" not in redis.values and graph_subscription_key("s1") not in redis.values


# ---------------------------------------------------------------------------
# The poller and a subscribed assignment
# ---------------------------------------------------------------------------


def _poller(monkeypatch, wf, redis):
    from tests.agentic_gate_stub import stub_agentic_gate

    stub_agentic_gate(monkeypatch)
    polled: list[str] = []

    async def _poll(_provider, _event, _token, polled_wf, *_a, **_k):
        polled.append(str(polled_wf.id))
        return []

    async def _redis_client():
        return redis

    async def _no_release(*_a, **_k):
        return None

    async def _visible(*_a, **_k):
        return True

    monkeypatch.setattr(event_poller, "redis_client", _redis_client)
    monkeypatch.setattr(event_poller, "_poll_provider", _poll)
    monkeypatch.setattr(event_poller, "decrypt_value", lambda value: f"plain:{value}")
    monkeypatch.setattr("app.core.database.release_db_connection_before_loop", _no_release)
    monkeypatch.setattr(event_poller.TriggerIntegrationVisibility, "sees", _visible)
    from tests.integration_credential_fakes import make_integration

    integration = make_integration(provider="microsoft", user_id=OWNER)
    agent = SimpleNamespace(
        id=wf.agent_id, user_id=OWNER, enabled=True, is_company_managed=False, integration_bindings={"microsoft": [str(integration.id)]}
    )
    owner = User(id=OWNER, email="owner@test.local", full_name="O", hashed_password="x", role="user")
    db = FakeSession({Assignment: [wf], Agent: [agent], User: [owner], event_poller.Integration: [integration]})
    return db, polled


async def test_a_subscribed_assignment_is_not_polled_until_graph_says_it_missed_events(monkeypatch):
    redis = _Redis()
    wf = _assignment()
    db, polled = _poller(monkeypatch, wf, redis)
    redis.values[f"graph_sub_wf:{wf.id}"] = json.dumps({"subscriptions": []})

    await event_poller.poll_integration_events(db)
    assert polled == []

    redis.values[f"graph_sub_catchup:{wf.id}"] = "1"
    redis.values[f"event_poll:{wf.id}"] = str(datetime.now(UTC).timestamp())
    await event_poller.poll_integration_events(db)
    assert polled == [str(wf.id)], "a catch-up poll runs at once, whatever the interval"
    assert f"graph_sub_catchup:{wf.id}" not in redis.values


async def test_the_last_poll_shown_follows_the_pages_read_while_a_backlog_is_still_read():
    """#1718: the interval stamp is written once a poll caught up, so a backlog read
    over several ticks showed the end of the previous catch-up as its last poll."""
    redis = _Redis()
    wf = _assignment()
    now = datetime.now(UTC).timestamp()
    redis.values[f"event_poll:{wf.id}"] = str(now - 3600)
    redis.values[f"{event_poller.POLL_READ_KEY_PREFIX}{wf.id}"] = str(now)

    mode = await subs.delivery_mode(redis, wf)
    assert mode["last_poll_at"] == datetime.fromtimestamp(now, UTC).isoformat()
    assert mode["catching_up"] is True

    redis.values[f"event_poll:{wf.id}"] = str(now)
    mode = await subs.delivery_mode(redis, wf)
    assert mode["last_poll_at"] == datetime.fromtimestamp(now, UTC).isoformat()
    assert "catching_up" not in mode, "caught up: both stamps agree"


async def test_a_lapsed_subscription_polls_on_from_the_last_event_notified(monkeypatch):
    redis = _Redis()
    wf = _assignment()
    db, polled = _poller(monkeypatch, wf, redis)
    await event_poller.seed_event_cursor(redis, wf.id)
    await event_poller.advance_event_cursor(redis, wf.id, "2099-01-01T10:00:00Z")
    await event_poller.advance_event_cursor(redis, wf.id, "2099-01-01T09:00:00Z")
    assert redis.values[f"event_cursor:{wf.id}"] == "2099-01-01T10:00:00Z", "the cursor never moves back"

    await event_poller.poll_integration_events(db)
    assert polled == [str(wf.id)]


# ---------------------------------------------------------------------------
# Notifications: from the resource to the payload the poller builds
# ---------------------------------------------------------------------------


def test_channel_and_chat_resources_are_parsed():
    assert subs.parse_channel_resource("teams('t1')/channels('19:c@thread.tacv2')/messages('100')") == (
        "t1",
        "19:c@thread.tacv2",
        "100",
        "",
    )
    assert subs.parse_channel_resource("teams('t1')/channels('c')/messages('100')/replies('101')")[3] == "101"
    assert subs.parse_chat_resource("chats('19:abc@thread.v2')/messages('7')") == ("19:abc@thread.v2", "7")


async def _notified(event, notification, graph, wf=None, redis=None):
    wf = wf or _assignment(event)
    return await subs.notified_event(
        event, notification, wf=wf, token="tok", redis=redis or _Redis(), http=graph, integration_id=uuid.uuid4()
    )


async def test_a_notified_mail_carries_the_poller_s_payload():
    graph = _Graph(
        resources={
            "Users/u/Messages/m1": {
                "id": "m1",
                "from": {"emailAddress": {"address": "ceo@acme.example", "name": "CEO"}},
                "subject": "Board",
                "receivedDateTime": "2026-10-29T08:00:00Z",
                "conversationId": "c1",
            }
        }
    )
    payload, instant = await _notified("new_email", {"resource": "Users/u/Messages/m1", "changeType": "created"}, graph)
    assert payload["message_id"] == "m1" and payload["from"] == "ceo@acme.example" and payload["event"] == "new_email"
    assert instant == "2026-10-29T08:00:00Z"


async def test_a_reply_in_a_thread_triggers_new_teams_reply_with_its_parent():
    reply = {
        "id": "101",
        "messageType": "message",
        "replyToId": "100",
        "createdDateTime": "2026-10-29T08:01:00Z",
        "body": {"content": "Agreed"},
        "from": {"user": {"displayName": "Alice", "id": "u-1"}},
    }
    graph = _Graph(resources={"messages/100/replies/101": reply})
    resource = "teams('t1')/channels('c1')/messages('100')/replies('101')"
    payload, instant = await _notified("new_teams_reply", {"resource": resource}, graph)
    assert payload["event"] == "new_teams_reply"
    assert (payload["message_id"], payload["parent_message_id"]) == ("101", "100")
    assert (payload["team_id"], payload["channel_id"], payload["from_name"]) == ("t1", "c1", "Alice")
    assert instant == "2026-10-29T08:01:00Z"
    # The root-message trigger is not told of a reply, nor the reply trigger of a root.
    assert (await _notified("new_teams_message", {"resource": resource}, graph))[0] is None
    assert (await _notified("new_teams_reply", {"resource": "teams('t1')/channels('c1')/messages('100')"}, graph))[0] is None


async def test_a_chat_message_notification():
    msg = {"id": "7", "messageType": "message", "createdDateTime": "2026-10-29T08:02:00Z", "body": {"content": "hi"}, "chatType": "oneOnOne"}
    graph = _Graph(resources={"chats/19:abc@thread.v2/messages/7": msg})
    payload, _ = await _notified("new_teams_chat_message", {"resource": "chats('19:abc@thread.v2')/messages('7')"}, graph)
    assert (payload["chat_id"], payload["message_id"], payload["chat_type"]) == ("19:abc@thread.v2", "7", "oneOnOne")


async def test_a_cancelled_calendar_event_triggers_with_its_id_and_change_kind():
    event = {
        "id": "ev-1",
        "subject": "Board meeting",
        "isCancelled": True,
        "createdDateTime": "2026-10-01T08:00:00Z",
        "lastModifiedDateTime": "2026-10-29T08:03:00Z",
        "start": {"dateTime": "2026-11-05T09:00:00", "timeZone": "UTC"},
        "end": {"dateTime": "2026-11-05T10:00:00", "timeZone": "UTC"},
    }
    graph = _Graph(resources={"Users/u/Events/ev-1": event})
    payload, instant = await _notified("calendar_event_changed", {"resource": "Users/u/Events/ev-1", "changeType": "updated"}, graph)
    assert (payload["event_id"], payload["change_kind"], payload["is_cancelled"]) == ("ev-1", "cancelled", True)
    assert instant == "2026-10-29T08:03:00Z"
    deleted, _ = await _notified(
        "calendar_event_changed",
        {"resource": "Users/u/Events/ev-2", "changeType": "deleted", "resourceData": {"id": "ev-2"}},
        graph,
    )
    assert (deleted["event_id"], deleted["change_kind"]) == ("ev-2", "cancelled")


async def test_a_notified_mail_a_rule_filed_outside_the_inbox_triggers():
    """``me/messages`` names the mail by id wherever a rule moved it."""
    graph = _Graph(
        resources={
            "Users/u/Messages/m2": {
                "id": "m2",
                "from": {"emailAddress": {"address": "customer-service@ovh.com"}},
                "subject": "Case assigned",
                "receivedDateTime": "2026-11-06T09:25:08Z",
                "parentFolderId": "user-folder",
            }
        }
    )
    wf = _assignment("new_email", {"from": {"allow": ["ovh.com"]}})
    payload, _ = await _notified(
        "new_email", {"resource": "Users/u/Messages/m2", "changeType": "created"}, graph, wf=wf
    )
    assert payload["from"] == "customer-service@ovh.com"
    from app.services.integration_event_filters import event_filter_reason

    assert event_filter_reason("new_email", payload, wf.trigger_config["filter"]) is None


async def test_a_draft_saved_in_the_mailbox_is_no_new_email():
    graph = _Graph(resources={"Users/u/Messages/d1": {"id": "d1", "isDraft": True, "subject": "Re: Board", "from": {}}})
    payload, _ = await _notified("new_email", {"resource": "Users/u/Messages/d1", "changeType": "created"}, graph)
    assert payload is None


async def test_a_resource_graph_will_not_read_back_is_read_by_the_next_poll():
    redis = _Redis()
    wf = _assignment()
    payload, _ = await _notified("new_email", {"resource": "Users/u/Messages/gone"}, _Graph(), wf=wf, redis=redis)
    assert payload is None
    assert redis.values[f"graph_sub_catchup:{wf.id}"] == "1", "the poller reads it, never lost"


async def test_a_mail_update_triggers_email_flag_changed_only_when_the_flag_changed():
    redis = _Redis()
    wf = _assignment("email_flag_changed")
    mail = {
        "id": "m9",
        "subject": "Contract",
        "from": {"emailAddress": {"address": "legal@acme.example"}},
        "flag": {"flagStatus": "flagged"},
        "lastModifiedDateTime": "2026-10-29T08:04:00Z",
    }
    graph = _Graph(resources={"Users/u/Messages/m9": mail})
    note = {"resource": "Users/u/Messages/m9", "changeType": "updated"}

    first, _ = await _notified("email_flag_changed", note, graph, wf=wf, redis=redis)
    assert (first["mail_id"], first["flag_status"], first["previous_flag_status"]) == ("m9", "flagged", "")
    # A second update (read, moved) that leaves the flag alone is no event.
    assert (await _notified("email_flag_changed", note, graph, wf=wf, redis=redis))[0] is None
    mail["flag"] = {"flagStatus": "complete"}
    mail["lastModifiedDateTime"] = "2026-10-29T09:00:00Z"
    third, _ = await _notified("email_flag_changed", note, graph, wf=wf, redis=redis)
    assert (third["flag_status"], third["previous_flag_status"]) == ("complete", "flagged")
    assert third["message_id"] != first["message_id"], "each change runs once, not each mail"
