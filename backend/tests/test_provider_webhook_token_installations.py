"""The admin GitHub App's webhook and the installations each token sees.

Under "Require verified integration notifications" a GitHub App event reaches
an admin-app integration recording no installation only when its own token sees
the event's installation. These tests drive ``receive_provider_webhook`` with a
scripted session: what a delivery reads and triggers in both modes, that it
never waits on GitHub, and that everything else is exactly what it was.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.api.v1 import webhooks
from app.api.v1.webhooks import _AssignmentMatcher, _ProviderRouting
from app.core.config import settings
from app.core.security import encrypt_value
from app.services import github_token_installations as gti

ADMIN_CLIENT_ID = "admin-app-client-id-4359"


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.commands: list[str] = []

    async def mget(self, keys):
        self.commands.append("mget")
        return [self.values.get(key) for key in keys]

    async def set(self, key, value, *, ex=None):
        self.commands.append("set")
        self.values[key] = value
        return True


class FakeGitHub:
    def __init__(self) -> None:
        self.answers: dict[str, gti.TokenListing] = {}
        self.calls: list[str] = []
        self.gate: asyncio.Event | None = None

    async def __call__(self, token: str) -> gti.TokenListing:
        self.calls.append(token)
        if self.gate is not None:
            await self.gate.wait()
        return self.answers[token]


@pytest.fixture(autouse=True)
def _clean_cache():
    webhooks._invalidate_provider_routing_cache()
    yield
    webhooks._invalidate_provider_routing_cache()


@pytest.fixture
def strict(monkeypatch):
    monkeypatch.setattr(settings, "integration_notifications_require_verification", True)


@pytest.fixture
def default_mode(monkeypatch):
    from app.services import integration_notification_auth

    integration_notification_auth._log_state.clear()
    monkeypatch.setattr(settings, "integration_notifications_require_verification", False)


@pytest.fixture
def redis(monkeypatch):
    fake = FakeRedis()

    async def _store():
        return fake

    monkeypatch.setattr(gti, "redis_store", _store)
    return fake


@pytest.fixture
def github(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(gti, "fetch_installations", fake)
    return fake


class _Result:
    def __init__(self, *, scalar_one=None, rows=None, scalars_all=None):
        self._scalar_one = scalar_one
        self._rows = rows or []
        self._scalars_all = scalars_all or []

    def scalar_one_or_none(self):
        return self._scalar_one

    def all(self):
        return list(self._rows)

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._scalars_all))


class _Session:
    """Answers each query with the next scripted result; an unscripted query fails the test."""

    def __init__(self, results):
        self._results = list(results)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, _stmt):
        return self._results.pop(0)

    def begin_nested(self):
        return self

    async def commit(self):
        return None


class World:
    """One owner, one agent bound to one admin-app GitHub integration, one ``issues`` assignment."""

    def __init__(self, *, connected: bool = True, extra: dict | None = None) -> None:
        self.owner, self.agent_id, self.wf_id, self.integ = (uuid.uuid4() for _ in range(4))
        self.client_id_enc = encrypt_value(ADMIN_CLIENT_ID)
        self.token_enc = encrypt_value("gho-alice") if connected else None
        self.extra_enc = encrypt_value(json.dumps(extra)) if extra is not None else None

    def credentials_row(self):
        return (self.integ, "Alice's GitHub", "oauth", self.client_id_enc, self.token_enc, None, self.extra_enc)

    def candidate(self) -> gti.TokenCandidate:
        return gti.token_candidate(*self.credentials_row())

    def integration_row(self):
        return (self.integ, "github", self.owner, "oauth", False, self.client_id_enc, self.extra_enc)

    def routing(self) -> _ProviderRouting:
        matcher = _AssignmentMatcher(
            agent_id=str(self.agent_id),
            assignment_id=str(self.wf_id),
            trigger_type="integration_event",
            provider="github",
            event="issues",
            path="",
            events_filter=(),
            integrations=((str(self.integ), None),),
        )
        return _ProviderRouting(
            oauth_default_exists=True,
            extra_config_enc=None,
            client_secret_enc=None,
            has_agents=True,
            matchers=(matcher,),
        )

    def seed(self, redis: FakeRedis, ids, *, at: float | None = None) -> None:
        redis.values[f"github:token_installations:{self.integ}"] = json.dumps(
            {"fp": self.candidate().fingerprint, "at": time.time() if at is None else at,
             "ids": sorted(ids), "complete": True}
        )


def _request(event: str, payload: dict):
    body = json.dumps(payload).encode()

    async def _body() -> bytes:
        return body

    return SimpleNamespace(
        headers={"X-GitHub-Event": event},
        body=_body,
        url="https://copilot.example/api/hooks/providers/github",
        method="POST",
        query_params={},
        client=SimpleNamespace(host="203.0.113.7"),
    )


async def _deliver(world: World, installation_id=None, *, reads_credentials: bool, event="issues",
                   routing=None):
    """One delivery through the match path; *reads_credentials* scripts the token check's query."""
    agent = SimpleNamespace(
        id=world.agent_id, name="Repo Agent", enabled=True, user_id=world.owner,
        integration_bindings={"github": [str(world.integ)]},
    )
    wf = SimpleNamespace(
        id=world.wf_id, agent_id=world.agent_id, name="On issue", enabled=True,
        trigger_type="integration_event", trigger_config={"provider": "github", "event": "issues"},
    )
    webhooks._provider_routing_cache["github"] = (time.monotonic(), routing or world.routing())
    od = SimpleNamespace(id=uuid.uuid4(), client_id_enc=world.client_id_enc, client_secret_enc=None, extra_config_enc=None)
    session = _Session(
        [
            _Result(scalar_one=od),
            _Result(scalars_all=[agent]),
            _Result(scalars_all=[wf]),
            _Result(rows=[world.integration_row()]),
            *([_Result(rows=[world.credentials_row()])] if reads_credentials else []),
        ]
    )
    payload = {"action": "opened"}
    if installation_id is not None:
        payload["installation"] = {"id": installation_id}
    trigger = AsyncMock(return_value="run-1")
    with (
        patch.object(webhooks, "_claim_webhook_dedup_slot", AsyncMock(return_value=(False, None, None))),
        patch.object(webhooks, "_finalize_dedup", AsyncMock(side_effect=lambda _k, r: r)),
        patch.object(webhooks, "_release_webhook_dedup_slot", AsyncMock()),
        patch.object(webhooks, "async_session", new=lambda: session),
        patch.object(webhooks, "trigger_assignment", new=trigger),
    ):
        response = await webhooks.receive_provider_webhook("github", _request(event, payload))
    assert session._results == [], "a scripted query was not made"
    return response, trigger


async def _post_unmatched(event: str, payload: dict) -> dict:
    """A delivery on the no-agents fast path: no database session at all."""
    no_agents = _ProviderRouting(
        oauth_default_exists=True, extra_config_enc=None, client_secret_enc=None,
        has_agents=False, matchers=(),
    )
    webhooks._provider_routing_cache["github"] = (time.monotonic(), no_agents)
    with (
        patch.object(webhooks, "_claim_webhook_dedup_slot", AsyncMock(return_value=(False, None, None))),
        patch.object(webhooks, "_finalize_dedup", AsyncMock(side_effect=lambda _k, r: r)),
        patch.object(webhooks, "async_session", new=lambda: pytest.fail("no session on the fast path")),
    ):
        return await webhooks.receive_provider_webhook("github", _request(event, payload))


def _delivered(trigger: AsyncMock) -> dict:
    """What the assignment was triggered with, the delivery time left out."""
    trigger.assert_awaited_once()
    kwargs = dict(trigger.await_args.kwargs)
    context = dict(kwargs.pop("trigger_context"))
    context.pop("triggered_at")
    return {
        "assignment": kwargs["assignment"].id,
        "agent": kwargs["agent"].id,
        "payload": kwargs["payload"],
        "context": context,
        "skip_if_active": kwargs["skip_if_active"],
    }


# ---------------------------------------------------------------------------
# Strict mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_token_that_sees_the_installation_is_delivered_exactly_as_before(
    strict, redis, github, monkeypatch
):
    world = World()
    world.seed(redis, {"111", "222"})
    response, trigger = await _deliver(world, 222, reads_credentials=True)
    assert response["count"] == 1
    checked = _delivered(trigger)

    # The same delivery with the check left out, as main ran it.
    async def _unchecked(*_args, **_kwargs):
        return None

    monkeypatch.setattr(webhooks, "_token_installation_reach", _unchecked)
    _response, baseline = await _deliver(world, 222, reads_credentials=False)
    assert checked == _delivered(baseline)
    await gti.settle()
    assert github.calls == []


@pytest.mark.asyncio
async def test_a_token_that_does_not_see_the_installation_is_not_delivered(strict, redis, github):
    world = World()
    world.seed(redis, {"111"})
    response, trigger = await _deliver(world, 222, reads_credentials=True)
    assert response == {"status": "ok", "triggered": [], "count": 0}
    trigger.assert_not_awaited()


@pytest.mark.asyncio
async def test_with_no_answer_yet_it_is_delivered_as_before_without_waiting_on_github(strict, redis, github):
    world = World()
    github.answers["gho-alice"] = gti.TokenListing(frozenset({"111"}))
    github.gate = asyncio.Event()

    response, trigger = await _deliver(world, 222, reads_credentials=True)
    # Answered while GitHub is still being asked.
    assert response["count"] == 1
    trigger.assert_awaited_once()
    assert not github.gate.is_set()

    github.gate.set()
    await gti.settle()
    assert github.calls == ["gho-alice"]
    response, trigger = await _deliver(world, 222, reads_credentials=True)
    assert response["count"] == 0
    response, trigger = await _deliver(world, 111, reads_credentials=True)
    assert response["count"] == 1


@pytest.mark.asyncio
async def test_an_integration_with_no_token_is_not_delivered(strict, redis, github):
    world = World(connected=False)
    response, trigger = await _deliver(world, 222, reads_credentials=True)
    assert response["count"] == 0
    trigger.assert_not_awaited()
    assert redis.commands == []


@pytest.mark.asyncio
async def test_an_integration_recording_installations_is_decided_by_them_alone(strict, redis, github):
    world = World(extra={"installations": [{"id": 222}]})
    response, _trigger = await _deliver(world, 222, reads_credentials=False)
    assert response["count"] == 1
    response, _trigger = await _deliver(world, 333, reads_credentials=False)
    assert response["count"] == 0
    assert redis.commands == []


@pytest.mark.asyncio
async def test_an_event_naming_no_installation_is_not_checked(strict, redis, github):
    world = World()
    world.seed(redis, {"111"})
    response, _trigger = await _deliver(world, None, reads_credentials=False)
    assert response["count"] == 1
    assert redis.commands == []


@pytest.mark.asyncio
async def test_an_organisation_that_installs_the_app_later_is_heard_at_once(strict, redis, github):
    """GitHub announces an installation before any of its events: the answer
    read before it no longer decides for that installation."""
    world = World()
    world.seed(redis, {"111"}, at=time.time() - 5)
    github.answers["gho-alice"] = gti.TokenListing(frozenset({"111", "333"}))

    await _post_unmatched("installation", {"action": "created", "installation": {"id": 333}})

    response, _trigger = await _deliver(world, 333, reads_credentials=True)
    assert response["count"] == 1
    await gti.settle()
    assert github.calls == ["gho-alice"]


# ---------------------------------------------------------------------------
# Default mode: nothing changes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_by_default_nothing_is_read_and_the_event_is_delivered_as_before(default_mode, redis, github):
    world = World()
    world.seed(redis, {"111"})
    response, trigger = await _deliver(world, 222, reads_credentials=False)
    assert response["count"] == 1
    trigger.assert_awaited_once()
    no_token = World(connected=False)
    response, _trigger = await _deliver(no_token, 222, reads_credentials=False)
    assert response["count"] == 1
    await gti.settle()
    assert redis.commands == []
    assert github.calls == []


@pytest.mark.asyncio
async def test_by_default_a_visibility_event_marks_nothing(default_mode, redis, github):
    response = await _post_unmatched("installation", {"action": "created", "installation": {"id": 333}})
    assert response == {"status": "ok", "triggered": [], "count": 0}
    assert redis.commands == []


@pytest.mark.asyncio
async def test_in_strict_mode_a_visibility_event_marks_its_installation_and_nothing_else(strict, redis, github):
    response = await _post_unmatched("member", {"action": "added", "installation": {"id": 333}})
    assert response == {"status": "ok", "triggered": [], "count": 0}
    assert "github:installation_visibility_changed:333" in redis.values
    await _post_unmatched("issues", {"action": "opened", "installation": {"id": 444}})
    await _post_unmatched("member", {"action": "added"})
    assert list(redis.values) == ["github:installation_visibility_changed:333"]
