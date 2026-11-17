"""Teams private-channel Graph notifications prove their subscription.

``POST /api/channels/teams/graph-notifications`` compared a notification's
``clientState`` with the one stored in the Redis record of its channel's
subscription - but with no record at all it fell back to ``copilot_{team_id}``,
the value subscriptions were created with before 2026-07-02, so anyone who knew
a team id could make the platform fetch and act on channel messages.

A notification proves its subscription with a record, the record's subscription
id, and the clientState that record says was issued: a random one stored
Fernet-encrypted, a random one stored in plaintext by earlier releases, or -
only for a record written before random values existed - the legacy derivable
one. By default a notification earlier releases acted on is still acted on,
with a rate-limited warning and a Security Audit finding; with "Require
verified Teams channel notifications" on (strict), only a proven one is, and the
renewal pass replaces the subscriptions whose clientState is derivable or can
no longer be read. Either way the pass encrypts the plaintext records.

The Graph fake keeps the rules Microsoft documents, because the migration
depends on them: one subscription per app and channel (a second is refused), a
clientState fixed at creation and never listed, 404 for a subscription that no
longer exists. Notifications go through the real route; Redis, Graph and the
Graph credentials are recording fakes, so nothing leaves the process.
"""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI

import app.core.config as config_module
from app.channels import base as channel_base
from app.channels import teams, teams_graph_verdict, teams_graph_worker
from app.core import security_options
from app.core.parse_offload import WEBHOOK_THREAD_NAME_PREFIX
from app.core.security import decrypt_value, encrypt_value
from app.services import integration_notification_auth

TEAM = "team-1"
CHANNEL = "19:channel@thread.tacv2"
SUB_KEY = f"teams:priv_ch_sub:{TEAM}:{CHANNEL}"
LOCK_KEY = f"teams:priv_ch_sub_lock:{TEAM}:{CHANNEL}"
THREADS_KEY = f"teams:priv_threads:{TEAM}:{CHANNEL}"
RESOURCE = f"teams('{TEAM}')/channels('{CHANNEL}')/messages('m-1')"
GRAPH_RESOURCE = f"/teams/{TEAM}/channels/{CHANNEL}/messages"
TENANT = "0e5f5c1e-3a52-4c2f-9d0e-6a1b2c3d4e5f"
OTHER_TENANT = "7b1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f"
URL = "/api/channels/teams/graph-notifications"
LIFECYCLE_PARAMS = {"team": TEAM, "channel": CHANNEL}

# Captured before a test replaces ``httpx.AsyncClient`` with the Graph fake:
# the route itself is always driven through the real client.
_ASYNC_CLIENT = httpx.AsyncClient


class _Redis:
    """The part of the Redis client the Teams Graph code uses."""

    def __init__(self):
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int | None] = {}
        self.sets: dict[str, set] = {}
        self.zsets: dict[str, dict] = {}
        self.hashes: dict[str, dict] = {}
        self.calls: list[str] = []

    async def get(self, key):
        self.calls.append(f"get {key}")
        return self.values.get(key)

    async def mget(self, keys):
        self.calls.append(f"mget {len(keys)}")
        return [self.values.get(key) for key in keys]

    async def hincrby(self, key, field, amount):
        fields = self.hashes.setdefault(key, {})
        fields[field] = str(int(fields.get(field, 0)) + amount)

    async def hset(self, key, mapping):
        self.hashes.setdefault(key, {}).update(mapping)

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def set(self, key, value, ex=None, nx=False):
        self.calls.append(f"set {key}")
        if nx and key in self.values:
            return None
        self.values[key] = value
        self.ttls[key] = ex
        return True

    async def ttl(self, key):
        if key not in self.values:
            return -2
        return self.ttls.get(key) or -1

    async def delete(self, *keys):
        for key in keys:
            self.calls.append(f"delete {key}")
            self.values.pop(key, None)
            self.ttls.pop(key, None)
            self.zsets.pop(key, None)

    async def exists(self, key):
        return int(key in self.values or key in self.zsets)

    async def expire(self, key, _seconds):
        return int(key in self.values or key in self.zsets or key in self.hashes)

    async def eval(self, script, numkeys, key, token):
        """The one script the code runs: compare-and-delete, atomic by construction here."""
        self.calls.append(f"eval {key}")
        assert "DEL" in script and numkeys == 1
        if self.values.get(key) == token:
            self.values.pop(key, None)
            self.ttls.pop(key, None)
            return 1
        return 0

    async def sadd(self, key, member):
        self.sets.setdefault(key, set()).add(member)

    async def sismember(self, key, member):
        return int(member in self.sets.get(key, set()))

    async def smismember(self, key, members):
        self.calls.append(f"smismember {len(members)}")
        return [int(member in self.sets.get(key, set())) for member in members]

    async def srem(self, key, member):
        self.sets.setdefault(key, set()).discard(member)

    async def smembers(self, key):
        return set(self.sets.get(key, set()))

    async def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)

    async def zrangebyscore(self, key, low, high):
        low = float("-inf") if low == "-inf" else float(low)
        members = self.zsets.get(key, {}).items()
        return [m for m, score in sorted(members, key=lambda kv: kv[1]) if low <= score <= float(high)]

    async def zcard(self, key):
        return len(self.zsets.get(key, {}))

    async def zrem(self, key, member):
        self.zsets.get(key, {}).pop(member, None)


class _Graph:
    """Graph's ``/subscriptions`` API behind an ``httpx.AsyncClient`` stand-in.

    It keeps Graph's documented rules: one subscription per app and channel (a
    second is refused with 409), 404 for a subscription that does not exist,
    and the clientState a subscription was created with. It also serves the
    channel's messages (one message, and a thread's replies newest first).
    """

    def __init__(self):
        self.subscriptions: dict[str, dict] = {}
        self.calls: list[tuple] = []
        # A method answered with this status instead of its normal outcome.
        self.status: dict[str, int] = {}
        # Runs before a POST is answered: another worker acting meanwhile.
        self.before_post = None
        # Runs once a DELETE removed a subscription: a user posting meanwhile.
        self.after_delete = None
        # The first POST answers only after this many event-loop turns.
        self.slow_first_post = 0
        # Every GET answers only after this many event-loop turns.
        self.slow_get = 0
        self.created = 0
        self.messages: dict[str, dict] = {}
        self.replies: dict[str, list[str]] = {}

    def post_reply(self, root: str, reply_id: str, created: datetime) -> None:
        self.messages[reply_id] = {
            "id": reply_id,
            "replyToId": root,
            "createdDateTime": created.isoformat().replace("+00:00", "Z"),
            "body": {"contentType": "text", "content": f"text of {reply_id}"},
        }
        self.replies.setdefault(root, []).append(reply_id)

    def add(self, sub_id: str, client_state: str, resource: str = GRAPH_RESOURCE, lifecycle: bool = False) -> None:
        """A subscription made before the test: by an earlier release, or with *lifecycle* by this one."""
        self.subscriptions[sub_id] = {"id": sub_id, "resource": resource, "clientState": client_state}
        if lifecycle:
            self.subscriptions[sub_id]["lifecycleNotificationUrl"] = f"https://xtm.test{URL}?team={TEAM}"

    def methods(self) -> list[str]:
        return [call[0] for call in self.calls]

    def client(self, *_args, **_kwargs):
        graph = self

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            async def post(self, url, headers=None, json=None, timeout=None):
                first = graph.created == 0 and not any(call[0] == "POST" for call in graph.calls)
                answer = graph._post(url, json)
                for _ in range(graph.slow_first_post if first else 0):
                    await asyncio.sleep(0)
                return answer

            async def patch(self, url, headers=None, json=None, timeout=None):
                return graph._patch(url, json)

            async def delete(self, url, headers=None, timeout=None):
                return graph._delete(url)

            async def get(self, url, headers=None, params=None, timeout=None):
                for _ in range(graph.slow_get):
                    await asyncio.sleep(0)
                return graph._get(url, params)

        return _Client()

    @staticmethod
    def _answer(status: int, body: dict | None = None):
        return SimpleNamespace(status_code=status, json=lambda: body or {}, text="")

    @staticmethod
    def _id(url: str) -> str:
        return url.rsplit("/", 1)[-1]

    def _post(self, url, body):
        self.calls.append(("POST", url, body))
        if self.before_post:
            self.before_post()
        if "POST" in self.status:
            return self._answer(self.status["POST"])
        wanted = body["resource"].strip("/").lower()
        if any(sub["resource"].strip("/").lower() == wanted for sub in self.subscriptions.values()):
            return self._answer(409)
        self.created += 1
        sub_id = f"sub-new-{self.created}"
        self.subscriptions[sub_id] = {"id": sub_id, **body}
        return self._answer(201, {"id": sub_id})

    def _patch(self, url, body):
        self.calls.append(("PATCH", url, body))
        if "PATCH" in self.status:
            return self._answer(self.status["PATCH"])
        return self._answer(200 if self._id(url) in self.subscriptions else 404)

    def _delete(self, url):
        self.calls.append(("DELETE", url, None))
        if "DELETE" in self.status:
            return self._answer(self.status["DELETE"])
        deleted = self.subscriptions.pop(self._id(url), None)
        if deleted and self.after_delete:
            self.after_delete()
        return self._answer(204 if deleted else 404)

    def _get(self, url, params=None):
        self.calls.append(("GET", url, params))
        channel_messages = f"{teams.GRAPH_API}/teams/{TEAM}/channels/{CHANNEL}/messages/"
        if url.startswith(channel_messages) and url.endswith("/replies"):
            if "REPLIES" in self.status:
                return self._answer(self.status["REPLIES"])
            root = url[len(channel_messages) :].split("/", 1)[0]
            newest_first = sorted(
                (self.messages[r] for r in self.replies.get(root, [])),
                key=lambda m: m["createdDateTime"],
                reverse=True,
            )
            return self._answer(200, {"value": newest_first})
        if url.startswith(channel_messages):
            path = url[len(channel_messages) :]
            root, _, reply = path.partition("/replies/")
            # A reply is read under its thread's root, as Graph serves it.
            if reply:
                message = self.messages.get(reply) if reply in self.replies.get(root, []) else None
            else:
                message = self.messages.get(path)
            if "MESSAGE" in self.status:
                return self._answer(self.status["MESSAGE"])
            return self._answer(200, message) if message else self._answer(404)
        if url != f"{teams.GRAPH_API}/subscriptions":
            return self._answer(404)
        # Graph never lists a clientState ("not returned for security purposes").
        listed = [
            {
                "id": sub["id"],
                "resource": sub["resource"],
                "clientState": None,
                "lifecycleNotificationUrl": sub.get("lifecycleNotificationUrl"),
            }
            for sub in self.subscriptions.values()
        ]
        return self._answer(200, {"value": listed})


@pytest.fixture(autouse=True)
def _fresh_channel_tasks():
    channel_base._channel_processing_sem = None
    channel_base._channel_tasks.clear()
    yield
    channel_base._channel_processing_sem = None
    channel_base._channel_tasks.clear()


@pytest.fixture(autouse=True)
def _default_mode(monkeypatch):
    """Nothing set in Settings > Access and security, the variable unset: the default of every install."""
    security_options.reset_snapshot()
    monkeypatch.setattr(config_module.settings, "integration_notifications_require_verification", False)
    integration_notification_auth._log_state.clear()
    yield
    security_options.reset_snapshot()
    integration_notification_auth._log_state.clear()


@pytest.fixture
def strict(monkeypatch):
    """ "Require verified integration notifications" on: it covers this endpoint too."""
    monkeypatch.setattr(config_module.settings, "integration_notifications_require_verification", True)


@pytest.fixture
def redis(monkeypatch):
    fake = _Redis()

    async def _client():
        return fake

    monkeypatch.setattr("app.core.redis.redis_client", _client)
    return fake


@pytest.fixture
def reads(monkeypatch):
    """Every attempt to read the message with the platform's Graph credentials."""
    calls: list[str] = []

    async def _creds():
        calls.append("graph-credentials")
        return None

    monkeypatch.setattr(teams, "_get_teams_graph_creds", _creds)
    return calls


@pytest.fixture
def graph(monkeypatch):
    fake = _Graph()
    monkeypatch.setattr(teams.httpx, "AsyncClient", fake.client)

    async def _graph_creds():
        return "token", "app-id", "tenant-1"

    async def _bf_creds():
        return None

    monkeypatch.setattr(teams, "_get_teams_graph_creds", _graph_creds)
    monkeypatch.setattr(teams, "_get_teams_creds", _bf_creds)
    return fake


@asynccontextmanager
async def _route_client():
    app = FastAPI()
    app.include_router(teams.router, prefix="/api/channels")
    async with _ASYNC_CLIENT(transport=httpx.ASGITransport(app=app), base_url="https://xtm.test") as http:
        yield http


def _decide_every_body(mode: str, monkeypatch) -> None:
    """Where the route decides every body: on the event loop, in the worker
    process, or on the thread pool a worker that cannot start falls back to."""
    off_loop_from = teams.GRAPH_NOTIFICATION_MAX_BODY_BYTES + 1 if mode == "inline" else 0
    monkeypatch.setattr(teams, "GRAPH_NOTIFICATION_OFF_LOOP_BYTES", off_loop_from)
    if mode == "thread":

        async def _no_worker():
            return None

        monkeypatch.setattr(teams_graph_worker, "_ready_worker", _no_worker)


@pytest.fixture(params=["inline", "process", "thread"], ids=["inline", "process", "thread"])
async def client(request, monkeypatch):
    """The real route: every test using it runs once per place a body can be
    decided - the same answers all three ways."""
    _decide_every_body(request.param, monkeypatch)
    async with _route_client() as http:
        yield http


@pytest.fixture(params=["inline", "thread"], ids=["inline", "thread"])
async def in_process_client(request, monkeypatch):
    """The real route, deciding in this process: for a test that watches the
    decision's own steps, which a worker process runs out of its sight."""
    _decide_every_body(request.param, monkeypatch)
    async with _route_client() as http:
        yield http


async def _deliver(client, body, params=None) -> httpx.Response:
    """POST *body* to the real route, then let the tasks it spawned finish."""
    response = await client.post(URL, params=params, json=body)
    while channel_base._channel_tasks:
        await asyncio.gather(*list(channel_base._channel_tasks))
    return response


def _record(**fields) -> str:
    record = {
        "subscription_id": "sub-1",
        "team_id": TEAM,
        "channel_id": CHANNEL,
        "tenant_id": "tenant-1",
        "expires_at": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
    }
    record.update(fields)
    return json.dumps(record)


def _notification(client_state, subscription_id="sub-1", message="m-1", **fields) -> dict:
    notification = {
        "changeType": "created",
        "resource": f"teams('{TEAM}')/channels('{CHANNEL}')/messages('{message}')",
        "subscriptionId": subscription_id,
        **fields,
    }
    if client_state is not None:
        notification["clientState"] = client_state
    return notification


def _lifecycle(event: str, client_state: str, subscription_id: str = "sub-1") -> dict:
    return {
        "lifecycleEvent": event,
        "subscriptionId": subscription_id,
        "clientState": client_state,
        "subscriptionExpirationDateTime": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "tenantId": TENANT,
    }


def _fetched(graph: _Graph) -> list[str]:
    """The messages the platform fetched with its Graph credentials - the ones it acted on."""
    return [
        url.rsplit("/", 1)[-1]
        for method, url, _body in graph.calls
        if method == "GET" and "/messages/" in url and not url.endswith("/replies")
    ]


# ---------------------------------------------------------------------------
# The route: handshake, refusals and malformed bodies
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("params", [{}, LIFECYCLE_PARAMS], ids=["notification-url", "lifecycle-url"])
async def test_the_validation_handshake_echoes_the_token_as_plain_text(client, redis, reads, params):
    response = await client.post(URL, params={**params, "validationToken": "a token & <b>"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == "a token & <b>"
    assert redis.calls == [] and reads == []


async def test_without_a_record_the_legacy_value_is_refused_when_verification_is_required(
    client, redis, reads, strict
):
    """The forgery: a team id is all it took."""
    response = await _deliver(client, {"value": [_notification(f"copilot_{TEAM}")]})
    assert response.status_code == 202
    assert reads == []


async def test_by_default_what_earlier_releases_acted_on_is_acted_on_and_reported(client, redis, reads, caplog):
    """No record: earlier releases compared the clientState with the legacy value. It still works, said once."""
    await _deliver(client, {"value": [_notification(f"copilot_{TEAM}")]})
    await _deliver(client, {"value": [_notification(f"copilot_{TEAM}", message="m-2")]})
    assert reads == ["graph-credentials", "graph-credentials"]
    warnings = [r.getMessage() for r in caplog.records if "without proof of its subscription" in r.getMessage()]
    assert len(warnings) == 1  # rate-limited: once per reason and process every 15 minutes
    assert "INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION=true" in warnings[0]
    assert TEAM not in warnings[0]  # neither the team nor the payload
    assert f"copilot_{TEAM}" not in warnings[0]  # never the payload
    marker = redis.hashes[teams.UNVERIFIED_NOTIFICATIONS_KEY]
    assert marker["count"] == "2" and marker["last_reason"] == "no_record"
    # Anything else is refused, as it always was.
    await _deliver(client, {"value": [_notification("anything-else", message="m-3")]})
    assert reads == ["graph-credentials", "graph-credentials"]


async def test_an_encrypted_client_state_is_accepted_when_it_matches(client, redis, reads):
    redis.values[SUB_KEY] = _record(client_state_enc=encrypt_value("random-state"))
    await _deliver(client, {"value": [_notification("random-state")]})
    assert reads == ["graph-credentials"]


@pytest.mark.parametrize(
    ("client_state", "subscription_id"),
    [
        pytest.param("not-the-state", "sub-1", id="wrong-state"),
        pytest.param(None, "sub-1", id="no-state"),
        pytest.param("", "sub-1", id="empty-state"),
        pytest.param(f"copilot_{TEAM}", "sub-1", id="legacy-value-on-a-random-record"),
        pytest.param("random-state\u00e9", "sub-1", id="non-ascii"),
    ],
)
async def test_a_notification_that_does_not_prove_its_subscription_is_refused(
    client, redis, reads, client_state, subscription_id
):
    redis.values[SUB_KEY] = _record(client_state_enc=encrypt_value("random-state"))
    response = await _deliver(client, {"value": [_notification(client_state, subscription_id)]})
    assert response.status_code == 202
    assert reads == []


@pytest.mark.parametrize(
    ("client_state", "subscription_id", "fields"),
    [
        pytest.param("random-state", "sub-other", {}, id="another-subscription"),
        pytest.param("random-state", None, {}, id="no-subscription-id"),
        pytest.param("random-state", "sub-1", {"tenantId": OTHER_TENANT}, id="another-tenant"),
    ],
)
async def test_the_clientstate_alone_is_acted_on_by_default_and_refused_when_verification_is_required(
    client, redis, reads, monkeypatch, client_state, subscription_id, fields
):
    """Earlier releases compared the clientState only, never the subscription id or the tenant."""
    redis.values[SUB_KEY] = _record(tenant_id=TENANT, client_state_enc=encrypt_value("random-state"))
    await _deliver(client, {"value": [_notification(client_state, subscription_id, **fields)]})
    assert reads == ["graph-credentials"]
    assert redis.hashes[teams.UNVERIFIED_NOTIFICATIONS_KEY]["last_reason"] == "subscription_not_matched"

    monkeypatch.setattr(config_module.settings, "integration_notifications_require_verification", True)
    await _deliver(client, {"value": [_notification(client_state, subscription_id, message="m-2", **fields)]})
    assert reads == ["graph-credentials"]


async def test_a_plaintext_client_state_from_an_earlier_release_is_still_accepted(client, redis, reads):
    redis.values[SUB_KEY] = _record(client_state="random-state")
    await _deliver(client, {"value": [_notification("random-state")]})
    assert reads == ["graph-credentials"]


async def test_a_legacy_record_accepts_the_value_it_was_issued_and_nothing_else(client, redis, reads, strict):
    """Written before 2026-07-02: no clientState key, created with ``copilot_{team_id}``."""
    redis.values[SUB_KEY] = _record()
    await _deliver(client, {"value": [_notification(f"copilot_{TEAM}")]})
    assert reads == ["graph-credentials"]
    await _deliver(client, {"value": [_notification(f"copilot_{TEAM}", "sub-forged", message="m-2")]})
    await _deliver(client, {"value": [_notification("anything-else", message="m-3")]})
    assert reads == ["graph-credentials"]


async def test_by_default_a_legacy_record_acts_on_the_legacy_value_under_any_subscription_id(client, redis, reads):
    redis.values[SUB_KEY] = _record()
    await _deliver(client, {"value": [_notification(f"copilot_{TEAM}", "sub-forged")]})
    assert reads == ["graph-credentials"]
    assert redis.hashes[teams.UNVERIFIED_NOTIFICATIONS_KEY]["last_reason"] == "derivable_client_state"
    await _deliver(client, {"value": [_notification("anything-else", message="m-3")]})
    assert reads == ["graph-credentials"]


_UNPROVABLE_RECORDS = [
    pytest.param(_record(client_state=""), id="empty-plaintext"),
    pytest.param(_record(client_state_enc=""), id="empty-encrypted"),
    pytest.param(_record(client_state_enc="not-a-fernet-token"), id="undecryptable"),
    pytest.param(_record(client_state=["x"]), id="not-a-string"),
    pytest.param("not json", id="unreadable-record"),
    pytest.param(json.dumps(["x"]), id="not-an-object"),
    pytest.param(_record(subscription_id=None), id="no-subscription-id"),
]


@pytest.mark.parametrize("raw", _UNPROVABLE_RECORDS)
async def test_a_record_that_cannot_say_what_was_issued_authenticates_nothing(client, redis, reads, strict, raw):
    redis.values[SUB_KEY] = raw
    await _deliver(client, {"value": [_notification(f"copilot_{TEAM}"), _notification("", message="m-2")]})
    assert reads == []


@pytest.mark.parametrize(
    ("raw", "acted_on"),
    [
        # Earlier releases fell back to the legacy value for all of these...
        pytest.param(_record(client_state=""), ["m-1"], id="empty-plaintext"),
        pytest.param(_record(client_state_enc=""), ["m-1"], id="empty-encrypted"),
        pytest.param("not json", ["m-1"], id="unreadable-record"),
        pytest.param(json.dumps(["x"]), ["m-1"], id="not-an-object"),
        pytest.param(_record(subscription_id=None), ["m-1"], id="no-subscription-id"),
        # ...could compare a non-text value with nothing...
        pytest.param(_record(client_state=["x"]), [], id="not-a-string"),
        # ...and read a random value this release encrypts: a SECRET_KEY change leaves
        # the recorded subscription id, which a genuine notification always carries.
        pytest.param(_record(client_state_enc="not-a-fernet-token"), ["m-1", "m-2"], id="undecryptable"),
    ],
)
async def test_by_default_a_record_that_cannot_prove_acts_on_what_earlier_releases_did(
    client, redis, graph, raw, acted_on
):
    redis.values[SUB_KEY] = raw
    graph.messages.update({m: {"id": m, "body": {"content": ""}} for m in ("m-1", "m-2")})
    await _deliver(client, {"value": [_notification(f"copilot_{TEAM}"), _notification("", message="m-2")]})
    assert _fetched(graph) == acted_on
    # Another subscription id proves nothing, whatever the record holds.
    await _deliver(client, {"value": [_notification("anything", "sub-other", message="m-3")]})
    assert _fetched(graph) == acted_on


async def test_a_notification_from_another_tenant_is_refused(client, redis, reads, strict):
    redis.values[SUB_KEY] = _record(tenant_id=TENANT, client_state_enc=encrypt_value("random-state"))
    await _deliver(client, {"value": [_notification("random-state", tenantId=OTHER_TENANT)]})
    assert reads == []
    await _deliver(client, {"value": [_notification("random-state", tenantId=TENANT.upper())]})
    assert reads == ["graph-credentials"]


async def test_a_tenant_recorded_by_name_is_not_compared(client, redis, reads):
    """``tenant_id`` is whatever the bot was configured with - possibly a domain name."""
    redis.values[SUB_KEY] = _record(tenant_id="contoso.onmicrosoft.com", client_state_enc=encrypt_value("random-state"))
    await _deliver(client, {"value": [_notification("random-state", tenantId=TENANT)]})
    assert reads == ["graph-credentials"]


async def test_the_same_message_notified_twice_is_processed_once(client, redis, graph, routed):
    """Graph delivers at least once; a proven notification for a message already seen is dropped."""
    redis.values[SUB_KEY] = _record(client_state_enc=encrypt_value("random-state"))
    graph.messages.update({m: {"id": m, "replyToId": "root-1", "body": {"content": m}} for m in ("m-1", "m-2")})
    await _deliver(client, {"value": [_notification("random-state")]})
    await _deliver(client, {"value": [_notification("random-state")]})
    await _deliver(client, {"value": [_notification("random-state", message="m-2")]})
    assert routed == ["m-1", "m-2"]


async def test_a_refused_notification_does_not_mark_its_message_as_seen(client, redis, reads):
    """Otherwise a forger could pre-empt the genuine notification of a message."""
    redis.values[SUB_KEY] = _record(client_state_enc=encrypt_value("random-state"))
    await _deliver(client, {"value": [_notification("forged")]})
    await _deliver(client, {"value": [_notification("random-state")]})
    assert reads == ["graph-credentials"]


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"[]", id="list"),
        pytest.param(b'"x"', id="string"),
        pytest.param(b'{"value": "abc"}', id="value-a-string"),
        pytest.param(b'{"value": {"a": 1}}', id="value-an-object"),
        pytest.param(b'{"value": [1, "x", null, []]}', id="items-not-objects"),
        pytest.param(b'{"value": [{"changeType": "created", "resource": 5}]}', id="resource-not-a-string"),
    ],
)
async def test_a_malformed_body_is_acknowledged_without_an_error(client, redis, reads, body):
    """A 5xx would only make Graph retry for four hours; nothing here is actionable."""
    response = await client.post(URL, content=body, headers={"content-type": "application/json"})
    while channel_base._channel_tasks:
        await asyncio.gather(*list(channel_base._channel_tasks))
    assert response.status_code in (200, 202)
    assert reads == []


# ---------------------------------------------------------------------------
# Decided before anything is queued, from a bounded body
# ---------------------------------------------------------------------------

OTHER_CHANNEL = "19:other@thread.tacv2"


@pytest.fixture
def spawned(monkeypatch):
    """Every task the route queues."""
    names: list[str] = []
    real = teams.spawn_channel_task

    def _spawn(coro, *, name=None):
        names.append(name)
        return real(coro, name=name)

    monkeypatch.setattr(teams, "spawn_channel_task", _spawn)
    return names


def _on_other_channel(notification: dict, message: str = "m-x") -> dict:
    return {**notification, "resource": f"teams('{TEAM}')/channels('{OTHER_CHANNEL}')/messages('{message}')"}


@pytest.mark.parametrize("mode", ["default", "strict"])
async def test_a_forged_batch_queues_nothing_and_reads_each_channel_once(
    client, redis, reads, spawned, monkeypatch, mode
):
    """The semaphore bounds the tasks running, not the tasks queued: a refused entry must never become one."""
    if mode == "strict":
        monkeypatch.setattr(config_module.settings, "integration_notifications_require_verification", True)
    redis.values[SUB_KEY] = _record(client_state_enc=encrypt_value("random-state"))
    forged = [_notification("forged", message=f"m-{i}") for i in range(2000)]
    forged += [_on_other_channel(_notification("forged"))] * 50
    forged += [_lifecycle("reauthorizationRequired", "forged")] * 50
    response = await client.post(URL, params=LIFECYCLE_PARAMS, json={"value": forged})
    assert response.status_code == 202
    assert spawned == [] and reads == []
    # One round trip for every channel the batch names, whatever its size.
    assert [call for call in redis.calls if call.startswith(("get ", "mget "))] == ["mget 2"]


@pytest.mark.parametrize(
    ("mode", "client_state"),
    [
        # The random value of a record that was lost: refused by default too.
        pytest.param("default", "random-state-of-the-lost-record", id="default"),
        # What earlier releases acted on, refused once verification is required.
        pytest.param("strict", f"copilot_{TEAM}", id="strict"),
    ],
)
async def test_a_refused_batch_on_a_channel_that_lost_its_record_marks_the_gap_once(
    client, redis, reads, spawned, monkeypatch, mode, client_state
):
    """Refused in the route, it must still say the monitored channel may be missing notifications."""
    if mode == "strict":
        monkeypatch.setattr(config_module.settings, "integration_notifications_require_verification", True)
    redis.sets["teams:priv_ch_subs"] = {f"{TEAM}:{CHANNEL}"}
    forged = [_notification(client_state, message=f"m-{i}") for i in range(500)]
    forged += [_on_other_channel(_notification("forged"))] * 20
    response = await client.post(URL, json={"value": forged})
    assert response.status_code == 202
    assert spawned == [] and reads == []
    assert f"teams:priv_ch_gap:{TEAM}:{CHANNEL}" in redis.values
    assert f"teams:priv_ch_gap:{TEAM}:{OTHER_CHANNEL}" not in redis.values
    assert [c for c in redis.calls if c.startswith(("get ", "mget ", "smismember "))] == ["mget 2", "smismember 2"]


async def test_by_default_what_earlier_releases_acted_on_is_queued_and_reported_once(client, redis, reads, spawned):
    """Decided in the route, reported by the task alone: the audit counts it once."""
    await _deliver(client, {"value": [_notification(f"copilot_{TEAM}")]})
    assert spawned == ["teams-graph-notification"]
    assert reads == ["graph-credentials"]
    assert redis.hashes[teams.UNVERIFIED_NOTIFICATIONS_KEY]["count"] == "1"


async def test_the_accepted_entries_of_a_mixed_batch_are_the_only_ones_queued(client, redis, reads, spawned):
    redis.values[SUB_KEY] = _record(client_state_enc=encrypt_value("random-state"))
    batch = [_notification("forged", message="m-forged"), _notification("random-state", message="m-real")]
    batch += [{"changeType": "updated", "resource": RESOURCE}, {"lifecycleEvent": "missed"}]
    response = await _deliver(client, {"value": batch})
    assert response.status_code == 202
    assert spawned == ["teams-graph-notification"]
    assert reads == ["graph-credentials"]


async def test_a_proven_lifecycle_notification_is_still_queued(client, redis, graph, spawned):
    soon = (datetime.now(UTC) + timedelta(hours=20)).isoformat()
    redis.values[SUB_KEY] = _record(tenant_id=TENANT, client_state_enc=encrypt_value("random-state"), expires_at=soon)
    graph.add("sub-1", "random-state")
    body = {"value": [_lifecycle("reauthorizationRequired", "random-state")]}
    await _deliver(client, body, params=LIFECYCLE_PARAMS)
    assert spawned == ["teams-graph-lifecycle-notification"]
    assert graph.methods() == ["PATCH"]


async def test_a_replaced_subscription_s_lifecycle_notification_is_not_queued(client, redis, reads, spawned):
    """Its change notifications are still accepted for a while; it has nothing left to renew or recreate."""
    grace = {
        "id": "sub-old",
        "client_state_enc": encrypt_value("old-state"),
        "accept_until": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }
    redis.values[SUB_KEY] = _record(
        tenant_id=TENANT, client_state_enc=encrypt_value("random-state"), replaced_subscriptions=[grace]
    )
    lifecycle = _lifecycle("subscriptionRemoved", "old-state", "sub-old")
    await _deliver(client, {"value": [lifecycle]}, params=LIFECYCLE_PARAMS)
    assert spawned == []
    await _deliver(client, {"value": [_notification("old-state", "sub-old", tenantId=TENANT)]})
    assert spawned == ["teams-graph-notification"] and reads == ["graph-credentials"]


async def test_a_message_named_twice_in_one_batch_is_queued_and_read_once(client, redis, graph, routed, spawned):
    """Each task would read the message from Graph before its once-per-message mark."""
    redis.values[SUB_KEY] = _record(client_state_enc=encrypt_value("random-state"))
    graph.messages["m-1"] = {"id": "m-1", "replyToId": "root-1", "body": {"content": "m-1"}}
    graph.post_reply("root-1", "r-1", datetime.now(UTC))
    reply = _reply_notification_of("random-state", "r-1")
    batch = [_notification("random-state")] * 3 + [reply, reply]
    await _deliver(client, {"value": batch})
    assert spawned == ["teams-graph-notification"] * 2
    assert sorted(_fetched(graph)) == ["m-1", "r-1"]
    assert sorted(routed) == ["m-1", "r-1"]


async def test_by_default_an_unverified_message_named_many_times_is_queued_once(client, redis, reads, spawned):
    await _deliver(client, {"value": [_notification(f"copilot_{TEAM}")] * 500})
    assert spawned == ["teams-graph-notification"] and reads == ["graph-credentials"]
    assert redis.hashes[teams.UNVERIFIED_NOTIFICATIONS_KEY]["count"] == "1"


async def test_the_channels_a_batch_names_are_read_in_chunks(client, redis, reads, spawned):
    channels = [f"19:c{i}@thread.tacv2" for i in range(1200)]
    batch = [
        {**_notification("forged"), "resource": f"teams('{TEAM}')/channels('{channel}')/messages('m')"}
        for channel in channels
    ]
    response = await client.post(URL, json={"value": batch})
    assert response.status_code == 202 and spawned == []
    chunk = integration_notification_auth.GRAPH_RECORD_FETCH_CHUNK
    assert [c for c in redis.calls if c.startswith("mget ")] == [f"mget {chunk}", f"mget {chunk}", "mget 200"]


def _gap_writes_fail(redis) -> None:
    real = redis.set

    async def _set(key, value, ex=None, nx=False):
        if key.startswith("teams:priv_ch_gap:"):
            raise ConnectionError("redis is down")
        return await real(key, value, ex=ex, nx=nx)

    redis.set = _set


async def test_a_gap_that_cannot_be_recorded_is_answered_503_and_queues_nothing(client, redis, reads, spawned):
    """Acknowledged with no task and no gap, a genuine notification refused for a lost record would be lost."""
    redis.sets["teams:priv_ch_subs"] = {f"{TEAM}:{CHANNEL}"}
    redis.values[teams._subscription_key(TEAM, OTHER_CHANNEL)] = _record(client_state_enc=encrypt_value("other-state"))
    _gap_writes_fail(redis)
    batch = [_notification("random-state-of-the-lost-record"), _on_other_channel(_notification("other-state"))]
    response = await client.post(URL, json={"value": batch})
    assert response.status_code == 503
    assert spawned == [] and reads == []
    assert GAP_KEY not in redis.values


async def test_the_task_still_marks_the_gap_best_effort(redis):
    """Only the route needs the marker before answering: a task that finds the record gone keeps going."""
    redis.sets["teams:priv_ch_subs"] = {f"{TEAM}:{CHANNEL}"}
    _gap_writes_fail(redis)
    assert await teams._graph_notification_accepted(_notification("random-state-of-the-lost-record"), TEAM, CHANNEL) is False


async def test_each_channel_s_client_state_is_decrypted_once_per_batch(
    in_process_client, redis, reads, spawned, monkeypatch
):
    """The record state, the accepted pairs and the compatible value all come from one decryption."""
    client = in_process_client
    channels = [CHANNEL, OTHER_CHANNEL, "19:third@thread.tacv2"]
    for channel in channels:
        redis.values[teams._subscription_key(TEAM, channel)] = _record(client_state_enc=encrypt_value("random-state"))
    decrypted: list[str] = []
    real = teams_graph_verdict.decrypt_value

    def _counting(value):
        decrypted.append(value)
        return real(value)

    monkeypatch.setattr(teams_graph_verdict, "decrypt_value", _counting)
    batch = [
        {**_notification("forged"), "resource": f"teams('{TEAM}')/channels('{channel}')/messages('m-{i}')"}
        for channel in channels
        for i in range(40)
    ]
    response = await client.post(URL, json={"value": batch})
    assert response.status_code == 202 and spawned == []
    assert len(decrypted) == len(channels)


async def test_a_batch_redis_cannot_decide_is_answered_503_and_queues_nothing(client, redis, reads, spawned, caplog):
    """Nothing is queued yet, so Graph's redelivery is what keeps a genuine notification."""
    redis.values[SUB_KEY] = _record(client_state_enc=encrypt_value("random-state"))

    async def _down(_keys):
        raise ConnectionError("redis is down")

    redis.mget = _down
    response = await client.post(URL, json={"value": [_notification("random-state")]})
    assert response.status_code == 503
    assert spawned == [] and reads == []
    warnings = [r.getMessage() for r in caplog.records if "answered 503" in r.getMessage()]
    assert len(warnings) == 1 and "random-state" not in warnings[0]


async def test_refusals_are_logged_as_a_count_rate_limited_never_the_payload(client, redis, spawned, caplog):
    redis.values[SUB_KEY] = _record(client_state_enc=encrypt_value("random-state"))
    for _ in range(3):
        await client.post(URL, json={"value": [_notification("forged-state")] * 7 + [{"changeType": "updated"}]})
    refusals = [r.getMessage() for r in caplog.records if "refused on" in r.getMessage()]
    assert len(refusals) == 1  # at most once every 15 minutes, the POSTs held back counted
    assert "7 of the 8" in refusals[0]
    assert "forged-state" not in refusals[0] and TEAM not in refusals[0] and CHANNEL not in refusals[0]
    assert spawned == []


def _batch_of(size_bytes: int) -> bytes:
    """A syntactically valid notification batch of exactly *size_bytes*."""
    head, tail = b'{"value": [], "pad": "', b'"}'
    return head + b"x" * (size_bytes - len(head) - len(tail)) + tail


async def test_a_body_past_the_bound_is_refused_with_413_before_anything_is_read(client, redis, reads, spawned):
    response = await client.post(
        URL,
        content=_batch_of(teams.GRAPH_NOTIFICATION_MAX_BODY_BYTES + 1),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413
    assert redis.calls == [] and spawned == [] and reads == []


@pytest.mark.parametrize(
    "declared",
    [
        pytest.param(b"9" * 5000, id="past-the-int-digit-limit"),
        pytest.param(str(4 * 1024 * 1024 + 1).encode(), id="one-over"),
        # RFC 9110 optional whitespace around the value.
        pytest.param(f" {4 * 1024 * 1024 + 1}\t".encode(), id="one-over-padded"),
    ],
)
async def test_a_declared_length_past_the_bound_is_a_413_whatever_its_digits(redis, reads, spawned, declared):
    """``int()`` refuses a digit string longer than 4300 characters: that must not become a 500."""
    app = FastAPI()
    app.include_router(teams.router, prefix="/api/channels")
    sent: list[dict] = []

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": URL,
        "raw_path": URL.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json"), (b"content-length", declared)],
        "client": ("127.0.0.1", 1),
        "server": ("xtm.test", 443),
    }
    await app(scope, _receive, _send)
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 413
    assert redis.calls == [] and spawned == []


async def test_a_streamed_body_without_a_length_is_cut_off_at_the_bound(client, redis, reads, spawned):
    async def _chunks():
        chunk = b"x" * 65536
        sent = 0
        while sent <= teams.GRAPH_NOTIFICATION_MAX_BODY_BYTES:
            sent += len(chunk)
            yield chunk

    response = await client.post(URL, content=_chunks(), headers={"content-type": "application/json"})
    assert response.status_code == 413
    assert redis.calls == [] and spawned == []


async def test_a_large_genuine_batch_fits_the_bound(client, redis, reads, spawned):
    """Graph documents no batch limit; basic notifications are about 1 KB, a thousand of them fit several times."""
    redis.values[SUB_KEY] = _record(client_state_enc=encrypt_value("random-state"))
    batch = [
        _notification(
            "random-state",
            message=f"m-{i}",
            tenantId=TENANT,
            subscriptionExpirationDateTime=datetime.now(UTC).isoformat(),
            resourceData={"id": f"m-{i}", "@odata.type": "#Microsoft.Graph.chatMessage", "@odata.id": RESOURCE},
        )
        for i in range(1000)
    ]
    body = json.dumps({"value": batch}).encode()
    assert len(body) < teams.GRAPH_NOTIFICATION_MAX_BODY_BYTES
    response = await client.post(URL, content=body, headers={"content-type": "application/json"})
    assert response.status_code == 202
    assert len(spawned) == 1000
    while channel_base._channel_tasks:
        await asyncio.gather(*list(channel_base._channel_tasks))
    assert len(reads) == 1000


def _forged_batch_of(size_bytes: int) -> bytes:
    """Forged notifications for the recorded channel, as many as fit in *size_bytes*."""
    entries: list[bytes] = []
    size = len(b'{"value": []}')
    while True:
        entry = json.dumps(_notification("forged", message=f"m-{len(entries)}", tenantId=TENANT)).encode()
        if size + len(entry) + 2 > size_bytes:
            return b'{"value": [' + b", ".join(entries) + b"]}"
        entries.append(entry)
        size += len(entry) + 2


def _empty_objects_of(size_bytes: int) -> bytes:
    """``{"value": [{}, {}, ...]}``: the most objects a body of *size_bytes* can make the parser allocate."""
    head, tail = b'{"value":[', b"]}"
    count = (size_bytes - len(head) - len(tail) + 1) // 3
    return head + b",".join([b"{}"] * count) + tail


def _record_threads(monkeypatch) -> dict[str, list[str]]:
    """The thread each step of the decision ran on, by step (this process only)."""
    threads: dict[str, list[str]] = {}
    for name in ("summarize_post", "decide_post"):
        real = getattr(teams, name)

        def _recording(*args, _name=name, _real=real):
            threads.setdefault(_name, []).append(threading.current_thread().name)
            return _real(*args)

        monkeypatch.setattr(teams, name, _recording)
    return threads


@pytest.mark.parametrize(
    ("entries", "off_loop"),
    [
        pytest.param(1, False, id="one-notification-inline"),
        pytest.param(500, True, id="past-the-threshold-in-the-worker"),
    ],
)
async def test_only_a_large_post_is_decided_in_the_worker_process(
    redis, reads, spawned, monkeypatch, entries, off_loop
):
    """The hop costs more than deciding a small batch; a large one is decided in the worker process."""
    redis.values[SUB_KEY] = _record(client_state_enc=encrypt_value("random-state"))
    body = json.dumps({"value": [_notification("forged", message=f"m-{i}") for i in range(entries)]}).encode()
    assert (len(body) >= teams.GRAPH_NOTIFICATION_OFF_LOOP_BYTES) is off_loop
    sent: list[str] = []
    real = teams.run_decision

    async def _sending(fn, *args):
        sent.append(fn.__name__)
        return await real(fn, *args)

    monkeypatch.setattr(teams, "run_decision", _sending)
    async with _route_client() as http:
        response = await http.post(URL, content=body, headers={"content-type": "application/json"})
    assert response.status_code == 202 and spawned == [] and reads == []
    assert sent == (["summarize_post", "decide_post"] if off_loop else [])


@pytest.mark.parametrize(
    "make_body",
    [
        pytest.param(_forged_batch_of, id="forged-notifications"),
        pytest.param(_empty_objects_of, id="empty-objects"),
    ],
)
async def test_in_the_worker_process_a_4_mib_post_never_stalls_the_event_loop(
    redis, reads, spawned, caplog, make_body
):
    """The parse and the decision hold another interpreter's GIL: the loop's
    longest stall stays well under what deciding the POST here costs, which is
    the stall the loop took when it decided it inline.

    Both are read on this process's CPU clock: a thread of it holding the GIL
    spends it, as the loop deciding inline does, while the scheduler parking the
    whole process on a loaded machine - the worker and other test processes
    sharing its cores - spends none of it."""
    redis.values[SUB_KEY] = _record(tenant_id=TENANT, client_state_enc=encrypt_value("random-state"))
    body = make_body(teams.GRAPH_NOTIFICATION_MAX_BODY_BYTES)
    assert teams.GRAPH_NOTIFICATION_MAX_BODY_BYTES - 1024 < len(body) <= teams.GRAPH_NOTIFICATION_MAX_BODY_BYTES
    started = time.process_time()
    entries, channel_keys, _candidates, _ignored = teams_graph_verdict.summarize_post(body, None, None)
    if channel_keys:
        teams_graph_verdict.decide_post(body, None, None, {SUB_KEY: redis.values[SUB_KEY]}, False)
    inline = time.process_time() - started
    gc.collect()
    # The worker's start (spawn, imports) is not what is measured.
    with caplog.at_level(logging.WARNING, logger=teams_graph_worker.__name__):
        worker_pid = await teams_graph_worker.run_decision(os.getpid)
    assert worker_pid != os.getpid(), f"no worker process: {caplog.text!r}"
    gaps: list[float] = []

    async def _tick():
        last = time.process_time()
        while True:
            await asyncio.sleep(0)
            now = time.process_time()
            gaps.append(now - last)
            last = now

    ticker = asyncio.create_task(_tick())
    try:
        with caplog.at_level(logging.INFO):
            async with _route_client() as http:
                response = await http.post(URL, content=body, headers={"content-type": "application/json"})
    finally:
        ticker.cancel()
    assert response.status_code == 202 and spawned == [] and reads == []
    assert max(gaps) < inline / 4, f"longest stall {max(gaps) * 1000:.0f} ms of CPU, deciding here {inline * 1000:.0f} ms"
    decided = [r.getMessage() for r in caplog.records if " in one POST " in r.getMessage()]
    assert [f"{entries} of the {entries}" in message for message in decided] == [True]


async def test_on_the_thread_fallback_a_4_mib_forged_batch_is_decided_while_the_loop_keeps_running(
    redis, reads, spawned, monkeypatch, caplog
):
    """With no worker process the webhook thread pool decides: the per-entry
    decision lets the loop in, the C-level parse does not."""
    _decide_every_body("thread", monkeypatch)
    redis.values[SUB_KEY] = _record(tenant_id=TENANT, client_state_enc=encrypt_value("random-state"))
    body = _forged_batch_of(teams.GRAPH_NOTIFICATION_MAX_BODY_BYTES)
    entries = body.count(b'"changeType"')
    threads = _record_threads(monkeypatch)
    ticks = 0

    async def _tick():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0)

    decided: list[tuple[int, int]] = []
    recording = teams_graph_verdict._decide_candidates

    def _deciding(*args):
        before = ticks
        result = recording(*args)
        decided.append((before, ticks))
        return result

    monkeypatch.setattr(teams_graph_verdict, "_decide_candidates", _deciding)
    ticker = asyncio.create_task(_tick())
    try:
        async with _route_client() as http:
            response = await http.post(URL, content=body, headers={"content-type": "application/json"})
    finally:
        ticker.cancel()
    assert response.status_code == 202 and spawned == [] and reads == []
    [(before, after)] = decided
    assert after > before, "the event loop did not run while the batch was decided"
    assert set(threads) == {"summarize_post", "decide_post"}
    assert all(name.startswith(WEBHOOK_THREAD_NAME_PREFIX) for ran_on in threads.values() for name in ran_on)
    [refusal] = [r.getMessage() for r in caplog.records if "refused on" in r.getMessage()]
    assert f"{entries} of the {entries} in one POST" in refusal
    assert [call for call in redis.calls if call.startswith(("get ", "mget "))] == ["mget 1"]


async def test_a_decision_that_fails_is_answered_503_and_queues_nothing(
    in_process_client, redis, reads, spawned, monkeypatch
):
    """Raised on the event loop or on the thread pool, the answer is the same
    (``test_teams_graph_worker.py`` checks what the worker process raises)."""
    client = in_process_client
    redis.values[SUB_KEY] = _record(client_state_enc=encrypt_value("random-state"))

    def _broken(*_args, **_kwargs):
        raise RuntimeError("verdict failed")

    monkeypatch.setattr(teams_graph_verdict, "_notification_verdict", _broken)
    response = await client.post(URL, json={"value": [_notification("random-state")]})
    assert response.status_code == 503
    assert spawned == [] and reads == []


# ---------------------------------------------------------------------------
# How subscriptions are created
# ---------------------------------------------------------------------------


async def test_a_new_subscription_subscribes_to_lifecycle_notifications_and_stores_its_client_state_encrypted(
    redis, graph
):
    sub_id = await teams._ensure_channel_subscription(TEAM, CHANNEL, TENANT, "token")
    assert sub_id == "sub-new-1"
    (method, _url, body) = graph.calls[0]
    assert method == "POST"
    issued = body["clientState"]
    assert issued != f"copilot_{TEAM}" and 32 <= len(issued) <= 128
    # Graph refuses a Teams subscription longer than an hour without one; the
    # query names the channel, which a lifecycle notification does not carry.
    lifecycle = urlparse(body["lifecycleNotificationUrl"])
    assert lifecycle.path == URL
    assert parse_qs(lifecycle.query) == {"team": [TEAM], "channel": [CHANNEL]}
    assert urlparse(body["notificationUrl"]).path == URL
    raw = redis.values[SUB_KEY]
    assert issued not in raw
    record = json.loads(raw)
    assert "client_state" not in record
    assert decrypt_value(record["client_state_enc"]) == issued
    assert record["tenant_id"] == TENANT


async def test_a_reused_record_puts_its_channel_back_in_the_renewal_pass(redis, graph):
    """The pass only visits the channels in the set: one missing there would never be migrated."""
    redis.values[SUB_KEY] = _record()
    assert await teams._ensure_channel_subscription(TEAM, CHANNEL, "tenant-1", "token") == "sub-1"
    assert graph.calls == []
    assert redis.sets["teams:priv_ch_subs"] == {f"{TEAM}:{CHANNEL}"}


async def test_a_subscription_nobody_recorded_is_deleted_so_the_channel_can_have_one(redis, graph, strict):
    """No record, yet Graph refuses a new subscription: an orphan holds the channel's only slot."""
    graph.add("sub-orphan", f"copilot_{TEAM}")
    sub_id = await teams._ensure_channel_subscription(TEAM, CHANNEL, "tenant-1", "token")
    assert sub_id == "sub-new-1"
    assert graph.methods() == ["POST", "GET", "DELETE", "POST"]
    assert graph.calls[2][1].endswith("/subscriptions/sub-orphan")
    assert list(graph.subscriptions) == ["sub-new-1"]
    assert json.loads(redis.values[SUB_KEY])["subscription_id"] == "sub-new-1"


async def test_by_default_an_orphan_an_earlier_release_created_is_kept(redis, graph):
    """It may still be the channel's working subscription (its notifications are acted on as before):
    deleting it would silence the channel if Graph then refused the new one."""
    graph.add("sub-orphan", f"copilot_{TEAM}")
    assert await teams._ensure_channel_subscription(TEAM, CHANNEL, "tenant-1", "token") is None
    assert graph.methods() == ["POST", "GET"]
    assert list(graph.subscriptions) == ["sub-orphan"]
    assert SUB_KEY not in redis.values


async def test_by_default_an_orphan_this_release_created_is_deleted(redis, graph):
    """It has a lifecycle URL, and its clientState was in the lost record: nothing it sends is acted on."""
    graph.add("sub-orphan", "lost-random-state", lifecycle=True)
    assert await teams._ensure_channel_subscription(TEAM, CHANNEL, "tenant-1", "token") == "sub-new-1"
    assert graph.methods() == ["POST", "GET", "DELETE", "POST"]
    assert list(graph.subscriptions) == ["sub-new-1"]


async def test_a_subscription_another_worker_just_recorded_is_adopted_not_deleted(redis, graph):
    graph.add("sub-other", "their-state")

    def _other_worker_records_it():
        redis.values[SUB_KEY] = _record(subscription_id="sub-other", client_state_enc=encrypt_value("their-state"))

    graph.before_post = _other_worker_records_it
    sub_id = await teams._ensure_channel_subscription(TEAM, CHANNEL, "tenant-1", "token")
    assert sub_id == "sub-other"
    assert graph.methods() == ["POST"]
    assert list(graph.subscriptions) == ["sub-other"]
    assert json.loads(redis.values[SUB_KEY])["subscription_id"] == "sub-other"


# ---------------------------------------------------------------------------
# The renewal pass: migration, renewal, recovery
# ---------------------------------------------------------------------------


def _renewal_setup(redis, record: str | None) -> None:
    if record is not None:
        redis.values[SUB_KEY] = record
        redis.ttls[SUB_KEY] = 3600
    redis.sets["teams:priv_ch_subs"] = {f"{TEAM}:{CHANNEL}"}
    redis.zsets[THREADS_KEY] = {"thread-1": datetime.now(UTC).timestamp()}


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({}, id="legacy"),
        pytest.param(
            {"client_state_enc": Fernet(Fernet.generate_key()).encrypt(b"random-state").decode()},
            id="encrypted-under-another-secret-key",
        ),
    ],
)
async def test_by_default_a_subscription_that_cannot_prove_is_kept_and_renewed(redis, graph, fields):
    """Deleting it first (Graph allows one per channel) would silence the channel if Graph then refused
    the new one: by default it is renewed as before."""
    graph.add("sub-1", f"copilot_{TEAM}")
    soon = (datetime.now(UTC) + timedelta(hours=6)).isoformat()
    _renewal_setup(redis, _record(expires_at=soon, **fields))
    await teams.renew_private_channel_subscriptions()
    assert graph.methods() == ["PATCH"]
    record = json.loads(redis.values[SUB_KEY])
    assert record["subscription_id"] == "sub-1"
    assert datetime.fromisoformat(record["expires_at"]) > datetime.now(UTC) + timedelta(days=2)


async def test_the_renewal_pass_replaces_a_legacy_subscription_by_deleting_it_first(redis, graph, strict):
    """Graph allows one subscription per app and channel: creating the new one first is refused."""
    graph.add("sub-1", f"copilot_{TEAM}")
    _renewal_setup(redis, _record())
    await teams.renew_private_channel_subscriptions()
    assert graph.methods() == ["DELETE", "POST"]
    assert graph.calls[0][1].endswith("/subscriptions/sub-1")
    assert list(graph.subscriptions) == ["sub-new-1"]
    record = json.loads(redis.values[SUB_KEY])
    assert record["subscription_id"] == "sub-new-1"
    assert decrypt_value(record["client_state_enc"]) == graph.subscriptions["sub-new-1"]["clientState"]


async def test_the_replaced_legacy_subscription_is_accepted_only_for_a_grace_period(client, redis, graph, strict):
    """Its notifications on their way, and Graph's retries of those it could not deliver, are not lost."""
    graph.add("sub-1", f"copilot_{TEAM}")
    _renewal_setup(redis, _record())
    await teams.renew_private_channel_subscriptions()
    new_state = graph.subscriptions["sub-new-1"]["clientState"]
    graph.calls.clear()

    await _deliver(client, {"value": [_notification(new_state, "sub-new-1", message="m-new")]})
    await _deliver(client, {"value": [_notification(f"copilot_{TEAM}", "sub-1", message="m-in-flight")]})
    assert _fetched(graph) == ["m-new", "m-in-flight"]

    # Graph retries a delivery that failed (the platform was restarting) for up to four hours.
    [entry] = json.loads(redis.values[SUB_KEY])["replaced_subscriptions"]
    grace = datetime.fromisoformat(entry["accept_until"]) - datetime.now(UTC)
    assert timedelta(hours=3, minutes=59) < grace <= timedelta(hours=4)

    record = json.loads(redis.values[SUB_KEY])
    for entry in record["replaced_subscriptions"]:
        entry["accept_until"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    redis.values[SUB_KEY] = json.dumps(record)
    await _deliver(client, {"value": [_notification(f"copilot_{TEAM}", "sub-1", message="m-late")]})
    assert _fetched(graph) == ["m-new", "m-in-flight"]


async def test_a_legacy_subscription_that_cannot_be_deleted_is_kept_and_renewed(client, redis, reads, graph, strict):
    """No second subscription can exist while it does: it keeps notifying, the next pass tries again."""
    graph.add("sub-1", f"copilot_{TEAM}")
    graph.status["DELETE"] = 500
    legacy = _record(expires_at=(datetime.now(UTC) + timedelta(hours=6)).isoformat())
    _renewal_setup(redis, legacy)
    await teams.renew_private_channel_subscriptions()
    assert graph.methods() == ["DELETE", "PATCH"]
    record = json.loads(redis.values[SUB_KEY])
    assert record["subscription_id"] == "sub-1"
    assert "client_state_enc" not in record and "client_state" not in record
    assert datetime.fromisoformat(record["expires_at"]) > datetime.now(UTC) + timedelta(days=2)


async def test_when_the_replacement_cannot_be_created_the_next_pass_creates_it(redis, graph, strict):
    graph.add("sub-1", f"copilot_{TEAM}")
    graph.status["POST"] = 500
    _renewal_setup(redis, _record())
    await teams.renew_private_channel_subscriptions()
    assert graph.methods() == ["DELETE", "POST"]
    # The deleted subscription notifies no more: its record would only vouch for forgeries.
    assert SUB_KEY not in redis.values

    del graph.status["POST"]
    graph.calls.clear()
    await teams.renew_private_channel_subscriptions()
    assert graph.methods() == ["POST"]
    assert json.loads(redis.values[SUB_KEY])["subscription_id"] == "sub-new-1"


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param(
            {"client_state_enc": Fernet(Fernet.generate_key()).encrypt(b"random-state").decode()},
            id="encrypted-under-another-secret-key",
        ),
        pytest.param({"client_state": ""}, id="empty-plaintext"),
        pytest.param({"client_state_enc": ""}, id="empty-encrypted"),
    ],
)
async def test_a_subscription_whose_record_can_prove_nothing_is_replaced(redis, graph, strict, fields):
    """After a SECRET_KEY change every notification would be refused while renewals kept it alive."""
    graph.add("sub-1", "random-state")
    _renewal_setup(redis, _record(**fields))
    await teams.renew_private_channel_subscriptions()
    assert graph.methods() == ["DELETE", "POST"]
    record = json.loads(redis.values[SUB_KEY])
    assert record["subscription_id"] == "sub-new-1"
    assert decrypt_value(record["client_state_enc"]) == graph.subscriptions["sub-new-1"]["clientState"]
    assert "replaced_subscriptions" not in record


async def test_the_renewal_pass_encrypts_a_plaintext_client_state_in_place(redis, graph):
    """Graph cannot change a clientState, so the value stays: only its storage changes."""
    expires_at = datetime.now(UTC) + timedelta(days=2)
    graph.add("sub-1", "random-state")
    _renewal_setup(redis, _record(client_state="random-state", expires_at=expires_at.isoformat()))
    await teams.renew_private_channel_subscriptions()
    assert graph.calls == []
    record = json.loads(redis.values[SUB_KEY])
    assert "client_state" not in record
    assert decrypt_value(record["client_state_enc"]) == "random-state"
    assert record["subscription_id"] == "sub-1"
    # The record lives exactly as long as the subscription it describes.
    remaining = (expires_at - datetime.now(UTC)).total_seconds()
    assert abs(redis.ttls[SUB_KEY] - remaining) < 60


async def test_a_renewal_answered_404_recreates_the_subscription(redis, graph):
    """Graph removed it (expired, deleted): a PATCH can never succeed again."""
    soon = (datetime.now(UTC) + timedelta(hours=6)).isoformat()
    _renewal_setup(redis, _record(client_state_enc=encrypt_value("random-state"), expires_at=soon))
    await teams.renew_private_channel_subscriptions()
    assert graph.methods() == ["PATCH", "POST"]
    assert json.loads(redis.values[SUB_KEY])["subscription_id"] == "sub-new-1"


async def test_a_renewal_that_fails_transiently_keeps_the_subscription_and_its_record(redis, graph):
    """Recreating it could only be refused while it exists, and its notifications would be lost."""
    soon = (datetime.now(UTC) + timedelta(hours=6)).isoformat()
    kept = _record(client_state_enc=encrypt_value("random-state"), expires_at=soon)
    graph.add("sub-1", "random-state")
    graph.status["PATCH"] = 503
    _renewal_setup(redis, kept)
    await teams.renew_private_channel_subscriptions()
    assert graph.methods() == ["PATCH"]
    assert redis.values[SUB_KEY] == kept


async def test_a_missing_record_is_recreated_even_when_its_subscription_is_still_live(redis, graph, strict):
    graph.add("sub-orphan", f"copilot_{TEAM}")
    _renewal_setup(redis, None)
    await teams.renew_private_channel_subscriptions()
    assert graph.methods() == ["POST", "GET", "DELETE", "POST"]
    assert json.loads(redis.values[SUB_KEY])["subscription_id"] == "sub-new-1"


async def test_by_default_a_missing_record_waits_for_an_earlier_release_s_subscription_to_expire(
    client, redis, graph
):
    """Its notifications are acted on meanwhile, as before; the pass creates one once Graph has room."""
    graph.add("sub-orphan", f"copilot_{TEAM}")
    graph.messages["m-1"] = {"id": "m-1", "body": {"content": ""}}
    _renewal_setup(redis, None)
    await teams.renew_private_channel_subscriptions()
    assert graph.methods() == ["POST", "GET"]
    assert list(graph.subscriptions) == ["sub-orphan"]
    await _deliver(client, {"value": [_notification(f"copilot_{TEAM}", "sub-orphan")]})
    assert _fetched(graph) == ["m-1"]

    del graph.subscriptions["sub-orphan"]  # expired
    graph.calls.clear()
    await teams.renew_private_channel_subscriptions()
    assert graph.methods() == ["POST"]
    assert json.loads(redis.values[SUB_KEY])["subscription_id"] == "sub-new-1"


async def test_a_channel_another_worker_is_maintaining_is_left_alone(redis, graph):
    """Two passes can overlap (the scheduler marker lasts 55 s): only one touches a channel at a time."""
    graph.add("sub-1", f"copilot_{TEAM}")
    legacy = _record()
    _renewal_setup(redis, legacy)
    redis.values[LOCK_KEY] = "another-worker"
    await teams.renew_private_channel_subscriptions()
    assert graph.calls == []
    assert redis.values[SUB_KEY] == legacy
    assert redis.values[LOCK_KEY] == "another-worker"


async def test_the_lock_is_released_after_the_pass(redis, graph):
    graph.add("sub-1", f"copilot_{TEAM}")
    _renewal_setup(redis, _record())
    await teams.renew_private_channel_subscriptions()
    assert LOCK_KEY not in redis.values


async def test_when_no_thread_is_left_the_subscription_and_its_record_are_deleted(redis, graph):
    graph.add("sub-1", "random-state")
    _renewal_setup(redis, _record(client_state_enc=encrypt_value("random-state")))
    redis.zsets[THREADS_KEY] = {}
    await teams.renew_private_channel_subscriptions()
    assert graph.methods() == ["DELETE"]
    assert graph.subscriptions == {}
    assert SUB_KEY not in redis.values
    assert redis.sets["teams:priv_ch_subs"] == set()


# ---------------------------------------------------------------------------
# Lifecycle notifications
# ---------------------------------------------------------------------------


async def test_a_reauthorization_challenge_renews_the_subscription(client, redis, graph):
    """Ignored, Graph eventually pauses the subscription's notifications."""
    soon = (datetime.now(UTC) + timedelta(hours=20)).isoformat()
    redis.values[SUB_KEY] = _record(tenant_id=TENANT, client_state_enc=encrypt_value("random-state"), expires_at=soon)
    graph.add("sub-1", "random-state")
    body = {"value": [_lifecycle("reauthorizationRequired", "random-state")]}
    response = await _deliver(client, body, params=LIFECYCLE_PARAMS)
    assert response.status_code == 202
    assert graph.methods() == ["PATCH"]
    assert graph.calls[0][1].endswith("/subscriptions/sub-1")
    record = json.loads(redis.values[SUB_KEY])
    assert datetime.fromisoformat(record["expires_at"]) > datetime.now(UTC) + timedelta(days=2)
    # Challenges repeat every few minutes: one renewal answers them all for a while.
    await _deliver(client, body, params=LIFECYCLE_PARAMS)
    assert graph.methods() == ["PATCH"]


async def test_a_removed_subscription_is_recreated(client, redis, graph):
    redis.values[SUB_KEY] = _record(tenant_id=TENANT, client_state_enc=encrypt_value("random-state"))
    body = {"value": [_lifecycle("subscriptionRemoved", "random-state")]}
    await _deliver(client, body, params=LIFECYCLE_PARAMS)
    assert graph.methods() == ["POST"]
    record = json.loads(redis.values[SUB_KEY])
    assert record["subscription_id"] == "sub-new-1"
    assert decrypt_value(record["client_state_enc"]) == graph.subscriptions["sub-new-1"]["clientState"]


@pytest.mark.parametrize(
    ("notification", "params"),
    [
        pytest.param(_lifecycle("subscriptionRemoved", "forged"), LIFECYCLE_PARAMS, id="wrong-state"),
        pytest.param(_lifecycle("subscriptionRemoved", "random-state", "sub-other"), LIFECYCLE_PARAMS, id="other-id"),
        pytest.param(_lifecycle("subscriptionRemoved", "random-state"), {}, id="no-channel-in-url"),
        pytest.param(_lifecycle("reauthorizationRequired", "forged"), LIFECYCLE_PARAMS, id="forged-challenge"),
        pytest.param(_lifecycle("missed", "random-state"), LIFECYCLE_PARAMS, id="missed"),
    ],
)
async def test_a_lifecycle_notification_that_proves_nothing_actionable_changes_nothing(
    client, redis, graph, notification, params
):
    kept = _record(tenant_id=TENANT, client_state_enc=encrypt_value("random-state"))
    redis.values[SUB_KEY] = kept
    graph.add("sub-1", "random-state")
    response = await _deliver(client, {"value": [notification]}, params=params)
    assert response.status_code == 202
    assert graph.calls == []
    assert redis.values[SUB_KEY] == kept


# ---------------------------------------------------------------------------
# Thread replies
# ---------------------------------------------------------------------------

REPLY_PATH = f"/teams/{TEAM}/channels/{CHANNEL}/messages/root-1/replies/reply-1"
THREAD_KEY = f"{CHANNEL};messageid=root-1"


@pytest.fixture
def followed(monkeypatch, redis, graph):
    """What reaches the channel router from a monitored thread."""
    redis.values[f"active_thread:teams:{THREAD_KEY}"] = "1"
    handled: list = []

    async def _handle(inbound):
        handled.append(inbound)
        return SimpleNamespace(suppress=True)

    @asynccontextmanager
    async def _no_typing(_channel):
        yield

    async def _not_muted(*_args):
        return False

    async def _bf_creds():
        return "bf-token", "bot-app"

    monkeypatch.setattr(teams, "handle_message", _handle)
    monkeypatch.setattr(teams, "typing_keepalive", _no_typing)
    monkeypatch.setattr("app.channels.router._is_thread_muted", _not_muted)
    monkeypatch.setattr(teams, "_get_teams_creds", _bf_creds)
    return handled


def _accepting(redis, proof: str) -> str:
    """The clientState a notification carries to be acted on: *proof* ``proven`` or, by default, ``unverified``."""
    if proof == "proven":
        redis.values[SUB_KEY] = _record(client_state_enc=encrypt_value("random-state"))
        return "random-state"
    return f"copilot_{TEAM}"  # no record: what earlier releases acted on


def _message(message_id: str, reply_to: str | None, text: str, sender: dict | None = None) -> dict:
    return {
        "id": message_id,
        "replyToId": reply_to,
        "from": sender or {"user": {"id": "user-1", "email": "user@example.com", "displayName": "User"}},
        "body": {"contentType": "text", "content": text},
    }


def _post_reply(graph: _Graph, reply_id: str, reply_to: str | None, text: str, sender: dict | None = None) -> None:
    """A reply Graph serves under ``root-1``, saying it replies to *reply_to*."""
    graph.post_reply("root-1", reply_id, datetime.now(UTC))
    graph.messages[reply_id] = _message(reply_id, reply_to, text, sender)


def _reply_notification_of(client_state: str, reply_id: str, root: str = "root-1") -> dict:
    resource = f"teams('{TEAM}')/channels('{CHANNEL}')/messages('{root}')/replies('{reply_id}')"
    return {**_notification(client_state), "resource": resource}


@pytest.mark.parametrize("proof", ["proven", "unverified"])
@pytest.mark.parametrize(
    "resource",
    [
        pytest.param(f"teams('{TEAM}')/channels('{CHANNEL}')/messages('root-1')/replies('reply-1')", id="parenthesized"),
        pytest.param(f"teams/{TEAM}/channels/{CHANNEL}/messages/root-1/replies/reply-1", id="slash"),
    ],
)
async def test_a_thread_reply_is_fetched_as_the_reply_and_routed_to_its_thread(
    client, redis, graph, followed, resource, proof
):
    """Graph names a reply ``messages('{root}')/replies('{reply}')``: reading the root skipped every follow-up."""
    _post_reply(graph, "reply-1", "root-1", "a follow-up")
    await _deliver(client, {"value": [{**_notification(_accepting(redis, proof)), "resource": resource}]})
    assert _fetched(graph) == ["reply-1"]
    assert next(url for method, url, _body in graph.calls if method == "GET") == f"{teams.GRAPH_API}{REPLY_PATH}"
    (inbound,) = followed
    assert inbound.channel_thread_id == THREAD_KEY
    assert inbound.channel_message_id == "reply-1"
    assert inbound.text == "a follow-up"
    assert inbound.metadata["reply_to_id"] == "root-1"


async def test_with_verification_required_a_reply_needs_the_proof_a_root_post_needs(
    client, redis, graph, followed, strict
):
    _post_reply(graph, "reply-1", "root-1", "a follow-up")
    await _deliver(client, {"value": [_reply_notification_of(_accepting(redis, "unverified"), "reply-1")]})
    assert graph.calls == [] and followed == []
    assert not [key for key in redis.values if key.startswith("teams:graph_msg_seen:")]
    await _deliver(client, {"value": [_reply_notification_of(_accepting(redis, "proven"), "reply-1")]})
    assert [inbound.channel_message_id for inbound in followed] == ["reply-1"]


async def test_two_replies_in_one_thread_are_two_messages(client, redis, graph, followed):
    """Processed once per message id: read as their root, the second reply was dropped as already seen."""
    _post_reply(graph, "reply-1", "root-1", "first")
    _post_reply(graph, "reply-2", "root-1", "second")
    state = _accepting(redis, "proven")
    for reply_id in ("reply-1", "reply-2"):
        await _deliver(client, {"value": [_reply_notification_of(state, reply_id)]})
    assert [inbound.channel_message_id for inbound in followed] == ["reply-1", "reply-2"]


async def test_a_reply_graph_could_not_return_is_processed_on_its_next_delivery(client, redis, graph, followed):
    """Marked only once read: a transient Graph failure does not burn the genuine message."""
    _post_reply(graph, "reply-1", "root-1", "a follow-up")
    state = _accepting(redis, "proven")
    graph.status["MESSAGE"] = 503
    await _deliver(client, {"value": [_reply_notification_of(state, "reply-1")]})
    assert followed == []
    del graph.status["MESSAGE"]
    await _deliver(client, {"value": [_reply_notification_of(state, "reply-1")]})
    assert [inbound.channel_message_id for inbound in followed] == ["reply-1"]


async def test_a_notification_naming_a_reply_under_another_root_does_not_block_the_real_one(
    client, redis, graph, followed
):
    """Acted on by default without proof: a wrong root reads nothing and marks nothing."""
    _post_reply(graph, "reply-1", "root-1", "a follow-up")
    state = _accepting(redis, "unverified")
    await _deliver(client, {"value": [_reply_notification_of(state, "reply-1", root="root-9")]})
    assert followed == []
    await _deliver(client, {"value": [_reply_notification_of(state, "reply-1")]})
    assert [inbound.channel_message_id for inbound in followed] == ["reply-1"]


async def test_concurrent_deliveries_of_one_reply_are_answered_once(client, redis, graph, followed):
    """Both read the reply before either marks it: the mark is still taken once.

    Two POSTs: within one batch, a message named twice is queued once.
    """
    _post_reply(graph, "reply-1", "root-1", "a follow-up")
    graph.slow_get = 5
    notification = _reply_notification_of(_accepting(redis, "proven"), "reply-1")
    await asyncio.gather(*(client.post(URL, json={"value": [dict(notification)]}) for _ in range(2)))
    await _drain()
    assert _fetched(graph).count("reply-1") == 2
    assert [inbound.channel_message_id for inbound in followed] == ["reply-1"]


@pytest.mark.parametrize("reply_to", [None, "root-2"], ids=["no-reply-to", "another-thread"])
async def test_a_reply_graph_does_not_place_in_the_notified_thread_is_skipped(
    client, redis, graph, followed, caplog, reply_to
):
    """The thread comes from Teams' own answer, never from the notification's resource."""
    redis.values[f"active_thread:teams:{CHANNEL};messageid=root-2"] = "1"
    _post_reply(graph, "reply-1", reply_to, "a follow-up")
    state = _accepting(redis, "proven")
    for _ in range(2):
        await _deliver(client, {"value": [_reply_notification_of(state, "reply-1")]})
    assert _fetched(graph)[:1] == ["reply-1"]
    assert followed == []
    skipped = [r for r in caplog.records if "does not reply to the thread" in r.getMessage()]
    assert len(skipped) == 1  # said once


_OTHER_BOT = {"application": {"id": "other-bot", "displayName": "Other bot", "applicationIdentityType": "bot"}}


async def test_a_reply_another_bot_posted_is_not_answered(client, redis, graph, followed):
    """Two bots monitoring one private thread must not answer each other for as long as it is monitored."""
    _post_reply(graph, "reply-1", "root-1", "an automated post", sender=_OTHER_BOT)
    _post_reply(graph, "reply-2", "root-1", "a person's follow-up")
    state = _accepting(redis, "proven")
    for reply_id in ("reply-1", "reply-2"):
        await _deliver(client, {"value": [_reply_notification_of(state, reply_id)]})
    assert _fetched(graph)[:2] == ["reply-1", "reply-2"]
    assert [inbound.channel_message_id for inbound in followed] == ["reply-2"]
    # The catch-up hands what it reads back to the same path.
    automated = _message("reply-3", "root-1", "another automated post", sender=_OTHER_BOT)
    await teams._route_graph_channel_message(automated, TEAM, CHANNEL, "reply-3", "token", "app-id", "tenant-1")
    assert [inbound.channel_message_id for inbound in followed] == ["reply-2"]


async def test_a_root_level_channel_post_is_still_not_a_thread_follow_up(client, redis, graph, followed):
    graph.messages["m-9"] = _message("m-9", None, "a new post")
    await _deliver(client, {"value": [_notification(_accepting(redis, "proven"), message="m-9")]})
    assert _fetched(graph) == ["m-9"]
    assert followed == []


@pytest.mark.parametrize(
    "resource",
    [
        f"teams('{TEAM}')/channels('{CHANNEL}')/messages('root-1')/replies('reply-1')/hostedContents('x')",
        f"teams('{TEAM}')/channels('{CHANNEL}')/messages('root-1')/replies",
        f"teams/{TEAM}/channels/{CHANNEL}/messages/root-1/replies",
        f"teams/{TEAM}/channels/{CHANNEL}/messages/root-1/replies/reply-1/more",
        f"teams/{TEAM}/channels/{CHANNEL}/messages/root-1/",
        f"teams/{TEAM}/channels/{CHANNEL}/messages/root-1/replies/reply-1/",
        f"//teams/{TEAM}/channels/{CHANNEL}/messages/root-1",
        f"teams//channels/{CHANNEL}/messages/root-1",
        f"teams/{TEAM}/channels/{CHANNEL}/messages//replies/reply-1",
        f"teams/{TEAM}/channels/{CHANNEL}/messages/root-1/replies/",
    ],
)
def test_a_resource_that_names_no_single_message_is_not_parsed(resource):
    assert teams._parse_graph_resource(resource) == ("", "", "", None)


@pytest.mark.parametrize("prefix", ["", "/"], ids=["relative", "leading-slash"])
def test_a_resource_may_start_with_one_slash(prefix):
    reply = f"{prefix}teams/{TEAM}/channels/{CHANNEL}/messages/root-1/replies/reply-1"
    assert teams._parse_graph_resource(reply) == (TEAM, CHANNEL, "root-1", "reply-1")
    quoted = f"{prefix}teams('{TEAM}')/channels('{CHANNEL}')/messages('m-1')"
    assert teams._parse_graph_resource(quoted) == (TEAM, CHANNEL, "m-1", None)


@pytest.mark.parametrize("unsafe", ["a/b", "..", ".", "a?b", "a#b", "a%2Fb", "a b", "a\\b", "a\nb"])
@pytest.mark.parametrize("position", range(4), ids=["team", "channel", "message", "reply"])
def test_an_id_that_could_leave_its_path_segment_is_not_parsed(position, unsafe):
    """Every id lands in a Graph URL (httpx collapses ``..``) and in the once-per-message key."""
    ids = [TEAM, CHANNEL, "root-1", "reply-1"]
    ids[position] = unsafe
    quoted = f"teams('{ids[0]}')/channels('{ids[1]}')/messages('{ids[2]}')/replies('{ids[3]}')"
    assert teams._parse_graph_resource(quoted) == ("", "", "", None)
    slashed = f"teams/{ids[0]}/channels/{ids[1]}/messages/{ids[2]}/replies/{ids[3]}"
    assert teams._parse_graph_resource(slashed) == ("", "", "", None)


@pytest.mark.parametrize(
    "channel", ["19:2a3b4c5d6e7f80910a1b2c3d4e5f6071@thread.tacv2", "19:abc_DEF-012@thread.skype"]
)
def test_real_teams_ids_are_parsed(channel):
    team = "fbe2bf47-16c8-47cf-b4a5-4b9b187c508b"
    reply = f"teams('{team}')/channels('{channel}')/messages('1612289765949')/replies('1612289802572')"
    assert teams._parse_graph_resource(reply) == (team, channel, "1612289765949", "1612289802572")
    root = f"teams/{team}/channels/{channel}/messages/1612289765949"
    assert teams._parse_graph_resource(root) == (team, channel, "1612289765949", None)


async def test_an_accepted_notification_whose_ids_leave_their_segment_reads_nothing(client, redis, graph, followed):
    """Acted on by default without proof: the reply segment must not walk the read out of the thread."""
    graph.messages["m-9"] = _message("m-9", None, "an unrelated post")
    _post_reply(graph, "reply-1", "root-1", "a follow-up")
    state = _accepting(redis, "unverified")
    for reply in ("../../m-9", "reply-1#again", "reply-1?x=1"):
        await _deliver(client, {"value": [_reply_notification_of(state, reply)]})
    assert graph.calls == [] and followed == []
    assert not [key for key in redis.values if key.startswith("teams:graph_msg_seen:")]


# ---------------------------------------------------------------------------
# Concurrency: the channel lock
# ---------------------------------------------------------------------------


async def test_the_channel_lock_is_released_in_one_atomic_step(redis, graph):
    """A GET then a DEL could delete the lock of a worker that took it over in between."""
    graph.add("sub-1", f"copilot_{TEAM}")
    _renewal_setup(redis, _record())
    await teams.renew_private_channel_subscriptions()
    lock_calls = [call for call in redis.calls if call.endswith(LOCK_KEY)]
    assert lock_calls == [f"set {LOCK_KEY}", f"eval {LOCK_KEY}"]
    assert LOCK_KEY not in redis.values


async def test_a_lock_another_worker_took_over_is_not_released(redis):
    async with teams._channel_subscription_lock(redis, TEAM, CHANNEL) as held:
        assert held
        redis.values[LOCK_KEY] = "the-next-holder"  # ours expired meanwhile
    assert redis.values[LOCK_KEY] == "the-next-holder"


async def test_two_setups_at_once_create_one_subscription_and_delete_none(redis, graph):
    """Two mentions answered together: the second must not read the first's fresh subscription as an orphan."""
    graph.slow_first_post = 20
    first, second = await asyncio.gather(
        teams._ensure_channel_subscription(TEAM, CHANNEL, "tenant-1", "token"),
        teams._ensure_channel_subscription(TEAM, CHANNEL, "tenant-1", "token"),
    )
    assert first == second == "sub-new-1"
    assert "DELETE" not in graph.methods()
    assert list(graph.subscriptions) == ["sub-new-1"]
    assert json.loads(redis.values[SUB_KEY])["subscription_id"] == "sub-new-1"


async def test_a_subscription_that_cannot_be_deleted_keeps_its_record_when_no_thread_is_left(redis, graph):
    """Dropping the record would leave a live subscription nobody tracks until it expires."""
    graph.add("sub-1", "random-state")
    kept = _record(client_state_enc=encrypt_value("random-state"))
    _renewal_setup(redis, kept)
    redis.zsets[THREADS_KEY] = {}
    graph.status["DELETE"] = 503
    await teams.renew_private_channel_subscriptions()
    assert redis.values[SUB_KEY] == kept
    assert redis.sets["teams:priv_ch_subs"] == {f"{TEAM}:{CHANNEL}"}

    del graph.status["DELETE"]
    await teams.renew_private_channel_subscriptions()
    assert graph.subscriptions == {}
    assert SUB_KEY not in redis.values
    assert redis.sets["teams:priv_ch_subs"] == set()


# ---------------------------------------------------------------------------
# Catching up on what no subscription notified
# ---------------------------------------------------------------------------

ROOT = "root-1"
GAP_KEY = f"teams:priv_ch_gap:{TEAM}:{CHANNEL}"


@pytest.fixture
def routed(monkeypatch):
    """The channel messages handed to the thread follow-up path, in order."""
    seen: list[str] = []

    async def _route(msg, team_id, channel_id, message_id, *_args):
        assert (team_id, channel_id) == (TEAM, CHANNEL)
        seen.append(message_id)

    monkeypatch.setattr(teams, "_route_graph_channel_message", _route)
    return seen


async def _drain() -> None:
    while channel_base._channel_tasks:
        await asyncio.gather(*list(channel_base._channel_tasks))


def _monitored(redis, record: str | None) -> None:
    _renewal_setup(redis, record)
    redis.zsets[THREADS_KEY] = {f"{CHANNEL};messageid={ROOT}": datetime.now(UTC).timestamp()}


def _reply_notification(client_state, subscription_id, reply_id) -> dict:
    return _notification(client_state, subscription_id, message=reply_id, tenantId=TENANT)


async def test_a_reply_posted_while_a_legacy_subscription_is_replaced_is_caught_up_once(
    client, redis, graph, routed, strict
):
    """Graph allows one subscription per channel: between the DELETE and the POST nothing is notified."""
    now = datetime.now(UTC)
    graph.add("sub-1", f"copilot_{TEAM}")
    graph.post_reply(ROOT, "r-old", now - timedelta(hours=1))
    graph.post_reply(ROOT, "r-before", now - timedelta(seconds=30))
    graph.after_delete = lambda: graph.post_reply(ROOT, "r-gap", datetime.now(UTC))
    _monitored(redis, _record(tenant_id=TENANT))

    await teams.renew_private_channel_subscriptions()
    await _drain()
    # Only what the gap swallowed: older replies were notified by the old subscription.
    assert routed == ["r-gap"]
    assert GAP_KEY not in redis.values

    new_state = graph.subscriptions["sub-new-1"]["clientState"]
    # A late notification of the same reply is not processed a second time...
    await _deliver(client, {"value": [_reply_notification(new_state, "sub-new-1", "r-gap")]})
    # ...while the old subscription's last delivery, within its grace period, still is.
    await _deliver(client, {"value": [_reply_notification(f"copilot_{TEAM}", "sub-1", "r-before")]})
    assert routed == ["r-gap", "r-before"]


async def test_a_reply_notified_before_the_catch_up_is_not_processed_again(client, redis, graph, routed):
    now = datetime.now(UTC)
    redis.values[SUB_KEY] = _record(tenant_id=TENANT, client_state_enc=encrypt_value("random-state"))
    _monitored(redis, redis.values[SUB_KEY])
    graph.post_reply(ROOT, "r-notified", now - timedelta(seconds=20))
    await _deliver(client, {"value": [_reply_notification("random-state", "sub-1", "r-notified")]})
    assert routed == ["r-notified"]

    # Graph then removes the subscription; one more reply lands before the new one exists.
    graph.post_reply(ROOT, "r-unnotified", now - timedelta(seconds=10))
    await _deliver(
        client, {"value": [_lifecycle("subscriptionRemoved", "random-state")]}, params=LIFECYCLE_PARAMS
    )
    await _drain()
    assert routed == ["r-notified", "r-unnotified"]
    assert json.loads(redis.values[SUB_KEY])["subscription_id"] == "sub-new-1"
    assert GAP_KEY not in redis.values


@pytest.mark.parametrize("live_first", [True, False], ids=["live-then-caught-up", "caught-up-then-live"])
async def test_a_reply_notified_under_its_root_and_caught_up_is_answered_once(client, redis, graph, routed, live_first):
    """Graph names the reply under its root, the catch-up reads it back by its own id: one mark for both."""
    now = datetime.now(UTC)
    _monitored(redis, _record(tenant_id=TENANT, client_state_enc=encrypt_value("random-state")))
    graph.post_reply(ROOT, "r-both", now - timedelta(seconds=20))
    resource = f"teams('{TEAM}')/channels('{CHANNEL}')/messages('{ROOT}')/replies('r-both')"
    live = {**_reply_notification("random-state", "sub-1", "r-both"), "resource": resource}

    async def _catch_up():
        redis.values[GAP_KEY] = (now - timedelta(minutes=1)).isoformat()
        await teams._catch_up_channel(redis, TEAM, CHANNEL, "token")
        await _drain()

    if live_first:
        await _deliver(client, {"value": [live]})
        await _catch_up()
    else:
        await _catch_up()
        await _deliver(client, {"value": [live]})
    assert routed == ["r-both"]


async def test_a_reply_refused_for_a_lost_record_is_caught_up_by_the_first_pass_after_startup(
    client, redis, graph, routed, monkeypatch, strict
):
    """A refused notification is never trusted: the reply is read back from Graph once the channel is repaired."""
    from app.services import autonomous

    graph.add("sub-orphan", f"copilot_{TEAM}")
    _monitored(redis, None)
    graph.post_reply(ROOT, "r-refused", datetime.now(UTC))
    await _deliver(client, {"value": [_reply_notification(f"copilot_{TEAM}", "sub-orphan", "r-refused")]})
    assert routed == [] and _fetched(graph) == []
    assert GAP_KEY in redis.values

    monkeypatch.setattr(autonomous, "_teams_subscription_tick_counter", autonomous._TEAMS_SUBSCRIPTION_INTERVAL)
    await autonomous._run_teams_subscription_maintenance()
    await _drain()
    assert routed == ["r-refused"]
    assert json.loads(redis.values[SUB_KEY])["subscription_id"] == "sub-new-1"
    assert list(graph.subscriptions) == ["sub-new-1"]
    # The next tick is an ordinary one: the pass runs again an hour later.
    graph.calls.clear()
    await autonomous._run_teams_subscription_maintenance()
    assert graph.calls == []


def test_every_process_runs_the_pass_on_the_first_tick_it_wins():
    """Seeded at its interval: a restart never waits an hour (N hours with N web processes) to repair a channel."""
    import ast
    import inspect

    from app.services import autonomous

    seeds = {
        node.targets[0].id: node.value
        for node in ast.parse(inspect.getsource(autonomous)).body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
    }
    seed = seeds["_teams_subscription_tick_counter"]
    assert isinstance(seed, ast.Name) and seed.id == "_TEAMS_SUBSCRIPTION_INTERVAL"


async def test_a_forged_notification_on_a_healthy_channel_marks_nothing(client, redis, graph, routed):
    _monitored(redis, _record(tenant_id=TENANT, client_state_enc=encrypt_value("random-state")))
    await _deliver(client, {"value": [_reply_notification("forged", "sub-1", "r-x")]})
    assert GAP_KEY not in redis.values


async def test_a_subscription_graph_removed_silently_is_caught_up_as_far_as_the_lookback(redis, graph, routed):
    """A renewal answered 404: when it vanished is unknown, so the catch-up reads its full look-back."""
    now = datetime.now(UTC)
    soon = (now + timedelta(hours=6)).isoformat()
    _monitored(redis, _record(client_state_enc=encrypt_value("random-state"), expires_at=soon))
    graph.post_reply(ROOT, "r-too-old", now - timedelta(hours=5))
    graph.post_reply(ROOT, "r-missed", now - timedelta(hours=1))
    await teams.renew_private_channel_subscriptions()
    await _drain()
    assert graph.methods()[:2] == ["PATCH", "POST"]
    assert routed == ["r-missed"]


async def test_a_catch_up_graph_could_not_answer_is_finished_by_the_next_pass(redis, graph, routed, strict):
    graph.add("sub-1", f"copilot_{TEAM}")
    graph.after_delete = lambda: graph.post_reply(ROOT, "r-gap", datetime.now(UTC))
    graph.status["REPLIES"] = 503
    _monitored(redis, _record())
    await teams.renew_private_channel_subscriptions()
    await _drain()
    assert routed == []
    assert GAP_KEY in redis.values

    del graph.status["REPLIES"]
    await teams.renew_private_channel_subscriptions()
    await _drain()
    assert routed == ["r-gap"]
    assert GAP_KEY not in redis.values


async def test_by_default_a_reply_refused_like_before_is_caught_up_once_graph_has_room(client, redis, graph, routed):
    """A lost record and an earlier release's random subscription: its notifications were refused
    before too. Once it expires and a new one is created, what it could not deliver is read back."""
    graph.add("sub-orphan", "random-state-of-the-lost-record")
    _monitored(redis, None)
    graph.post_reply(ROOT, "r-refused", datetime.now(UTC))
    await _deliver(client, {"value": [_reply_notification("random-state-of-the-lost-record", "sub-orphan", "r-refused")]})
    assert routed == [] and GAP_KEY in redis.values

    await teams.renew_private_channel_subscriptions()
    await _drain()
    assert routed == [] and list(graph.subscriptions) == ["sub-orphan"]  # kept while it lives

    del graph.subscriptions["sub-orphan"]  # expired
    await teams.renew_private_channel_subscriptions()
    await _drain()
    assert routed == ["r-refused"]
    assert GAP_KEY not in redis.values


async def test_by_default_a_reply_posted_while_an_unusable_orphan_is_replaced_is_caught_up(redis, graph, routed):
    """Deleting the orphan and creating the new subscription leave a window nothing notifies."""
    graph.add("sub-orphan", "lost-random-state", lifecycle=True)
    graph.after_delete = lambda: graph.post_reply(ROOT, "r-window", datetime.now(UTC))
    _monitored(redis, None)
    await teams.renew_private_channel_subscriptions()
    await _drain()
    assert graph.methods()[:4] == ["POST", "GET", "DELETE", "POST"]
    assert routed == ["r-window"]
    assert GAP_KEY not in redis.values


async def test_a_gap_is_caught_up_in_the_pass_that_renews_the_subscription(redis, graph, routed):
    """Not an hour later, once the renewed subscription has more than 12 hours left."""
    now = datetime.now(UTC)
    soon = (now + timedelta(hours=6)).isoformat()
    graph.add("sub-1", "random-state")
    _monitored(redis, _record(client_state_enc=encrypt_value("random-state"), expires_at=soon))
    graph.post_reply(ROOT, "r-held-back", now - timedelta(seconds=30))
    redis.values[GAP_KEY] = (now - timedelta(minutes=1)).isoformat()
    await teams.renew_private_channel_subscriptions()
    await _drain()
    assert graph.methods()[0] == "PATCH"
    assert routed == ["r-held-back"]
    assert GAP_KEY not in redis.values


async def test_a_gap_waits_while_the_renewal_fails(redis, graph, routed):
    """The subscription may not notify: reading back now would drop the marker before it does."""
    now = datetime.now(UTC)
    soon = (now + timedelta(hours=6)).isoformat()
    graph.add("sub-1", "random-state")
    graph.status["PATCH"] = 503
    _monitored(redis, _record(client_state_enc=encrypt_value("random-state"), expires_at=soon))
    graph.post_reply(ROOT, "r-held-back", now - timedelta(seconds=30))
    redis.values[GAP_KEY] = (now - timedelta(minutes=1)).isoformat()
    await teams.renew_private_channel_subscriptions()
    await _drain()
    assert graph.methods() == ["PATCH"]
    assert routed == [] and GAP_KEY in redis.values


async def test_a_replies_page_in_another_order_does_not_end_the_read(redis, graph, routed):
    """Graph lists replies newest first; an older reply ends the read only on a page in that order."""
    now = datetime.now(UTC)
    pages = [
        [("r-old", now - timedelta(hours=2)), ("r-new-1", now - timedelta(seconds=40))],  # oldest first
        [("r-new-2", now - timedelta(seconds=20))],
    ]
    base = f"{teams.GRAPH_API}/teams/{TEAM}/channels/{CHANNEL}/messages/{ROOT}/replies"

    class _Pages:
        async def get(self, url, headers=None, params=None, timeout=None):
            index = 0 if url == base else int(url.rsplit("=", 1)[-1])
            body = {"value": [{"id": i, "createdDateTime": t.isoformat()} for i, t in pages[index]]}
            if index + 1 < len(pages):
                body["@odata.nextLink"] = f"{base}?$skiptoken={index + 1}"
            return SimpleNamespace(status_code=200, json=lambda: body, text="")

    since = now - timedelta(minutes=1)
    deadline = asyncio.get_running_loop().time() + 30
    replies, complete = await teams._replies_since(_Pages(), "token", TEAM, CHANNEL, ROOT, since, deadline)
    assert [reply["id"] for reply in replies] == ["r-new-1", "r-new-2"]
    assert complete


async def test_a_system_message_read_back_is_skipped_without_an_error(redis, caplog, monkeypatch):
    """A member added, a pin: ``"from": null`` on the Graph copy."""

    async def _not_muted(*_args):
        return False

    monkeypatch.setattr("app.channels.router._is_thread_muted", _not_muted)
    msg = {"id": "r-system", "replyToId": ROOT, "from": None, "messageType": "systemEventMessage", "body": {}}
    await teams._route_graph_channel_message(msg, TEAM, CHANNEL, "r-system", "token", "app-id", "tenant-1")
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


# ---------------------------------------------------------------------------
# The Security Audit
# ---------------------------------------------------------------------------


class _TeamsBotsDb:
    """The one query the check makes: how many Teams bots are configured."""

    def __init__(self, count: int):
        self.count = count

    async def execute(self, _statement):
        return SimpleNamespace(scalar=lambda: self.count)


def _audit_channels(redis) -> None:
    channels = {
        "team-legacy:c": _record(),
        "team-key:c": _record(client_state_enc=Fernet(Fernet.generate_key()).encrypt(b"x").decode()),
        "team-missing:c": None,
        "team-current:c": _record(client_state_enc=encrypt_value("random-state")),
    }
    redis.sets["teams:priv_ch_subs"] = set(channels)
    for key, raw in channels.items():
        if raw is not None:
            redis.values[f"teams:priv_ch_sub:{key}"] = raw


async def _audit(redis) -> list:
    from app.api.v1 import security_audit

    findings: list = []
    await security_audit._check_teams_graph_notifications(_TeamsBotsDb(1), findings)
    return findings


async def test_the_audit_says_nothing_without_a_teams_bot(redis):
    from app.api.v1 import security_audit

    findings: list = []
    await security_audit._check_teams_graph_notifications(_TeamsBotsDb(0), findings)
    assert findings == []


async def test_the_audit_warns_by_default_and_counts_what_cannot_be_proven(client, redis, reads):
    from app.core.security_options import where_to_set

    _audit_channels(redis)
    await _deliver(client, {"value": [_notification(f"copilot_{TEAM}")]})
    [finding] = await _audit(redis)
    assert (finding.id, finding.severity, finding.category) == (
        "channels.teams_graph_notifications_unverified",
        "warning",
        "channels",
    )
    assert finding.control == "integrations.notifications_require_verification"
    assert "4 private or shared channel(s) are monitored" in finding.description
    assert "1 subscription(s) still use the derivable clientState" in finding.description
    assert "1 record(s) cannot be read or decrypted" in finding.description
    assert "1 channel(s) have no subscription record" in finding.description
    assert "1 notification(s) were acted on without proving their subscription" in finding.description
    assert where_to_set("integration_notifications_require_verification", "true") in finding.recommendation


async def test_the_audit_passes_when_verification_is_required(redis, strict):
    _audit_channels(redis)
    [finding] = await _audit(redis)
    assert (finding.id, finding.severity) == ("channels.teams_graph_notifications_verified", "pass")
    assert "No notification was acted on without proving its subscription" in finding.description


async def test_the_audit_still_reports_when_redis_does_not_answer(monkeypatch):
    async def _unreachable():
        raise ConnectionError("redis is down")

    monkeypatch.setattr("app.core.redis.redis_client", _unreachable)
    from app.api.v1 import security_audit

    findings: list = []
    await security_audit._check_teams_graph_notifications(_TeamsBotsDb(1), findings)
    [finding] = findings
    assert finding.severity == "warning"
    assert "could not be read" in finding.description
