"""Provider webhook routing cache — regression tests.

Production symptom: GitHub webhook storms (many repositories delivering to
``/api/hooks/providers/github``) saturated the DB pool (~40 concurrent
handlers, 2 sessions + 3+N queries EACH, even when nothing matched) and the
uvicorn concurrency backstop shed hundreds of deliveries with 429 — which
GitHub does not retry.

The fix caches a short-TTL per-provider routing snapshot (OAuthDefault
signature secrets + assignment matchers) so:

* a NO-MATCH delivery performs signature verification + Redis dedup + pure
  CPU matching with ZERO DB sessions;
* a MATCHING delivery opens ONE bounded session and re-loads the matched
  agent/assignment rows fresh (rows disabled since the snapshot are
  skipped);
* the old per-agent assignment N+1 is replaced by one ``IN`` query in the
  cache loader;
* cache loads are single-flight.
"""

from __future__ import annotations

import json
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.api.v1 import webhooks
from app.api.v1.webhooks import (
    _AssignmentMatcher,
    _match_provider_assignments,
    _ProviderRouting,
)
from app.core.security import encrypt_value


class _Headers(dict):
    """Dict with .items()/.get — enough for the webhook header helpers."""


def _make_request(headers: dict | None = None, body: bytes = b"{}") -> SimpleNamespace:
    async def _body() -> bytes:
        return body

    return SimpleNamespace(
        headers=_Headers(headers or {}),
        body=_body,
        url="https://copilot.example/api/hooks/providers/github",
        method="POST",
        query_params={},
        client=SimpleNamespace(host="203.0.113.7"),
    )


def _routing(
    *,
    matchers: tuple[_AssignmentMatcher, ...] = (),
    has_agents: bool | None = None,
) -> _ProviderRouting:
    return _ProviderRouting(
        oauth_default_exists=False,
        extra_config_enc=None,
        client_secret_enc=None,
        has_agents=bool(matchers) if has_agents is None else has_agents,
        matchers=matchers,
    )


def _matcher(**overrides) -> _AssignmentMatcher:
    defaults = dict(
        agent_id=str(uuid.uuid4()),
        assignment_id=str(uuid.uuid4()),
        trigger_type="integration_event",
        provider="github",
        event="",
        path="",
        events_filter=(),
        # Reachable through one admin-app integration recording no
        # installation - what the loader keeps.
        integrations=((str(uuid.uuid4()), None),),
    )
    defaults.update(overrides)
    return _AssignmentMatcher(**defaults)


@pytest.fixture(autouse=True)
def _clean_cache():
    webhooks._invalidate_provider_routing_cache()
    yield
    webhooks._invalidate_provider_routing_cache()


# ---------------------------------------------------------------------------
# _match_provider_assignments — must mirror the old live-ORM filter semantics
# ---------------------------------------------------------------------------


def test_integration_event_matches_on_provider_and_event_prefix():
    routing = _routing(
        matchers=(
            _matcher(provider="github", event="issues"),
            _matcher(provider="slack", event=""),
            _matcher(provider="github", event="pull_request"),
        )
    )
    matched = _match_provider_assignments(routing, "github", "issues.closed")
    assert len(matched) == 1
    assert matched[0].event == "issues"


@pytest.mark.parametrize("action", ["published", "reported"])
def test_repository_advisory_actions_match_event_prefix(action):
    matcher = _matcher(provider="github", event="repository_advisory")
    matched = _match_provider_assignments(
        _routing(matchers=(matcher,)),
        "github",
        f"repository_advisory.{action}",
    )
    assert matched == [matcher]


def test_integration_event_empty_event_matches_everything():
    routing = _routing(matchers=(_matcher(provider="github", event=""),))
    assert _match_provider_assignments(routing, "github", "anything.at_all")


def test_webhook_trigger_matches_on_path_and_events_filter():
    routing = _routing(
        matchers=(
            _matcher(
                trigger_type="webhook",
                provider=None,
                path="github",
                events_filter=("issues.closed", "push"),
            ),
            _matcher(trigger_type="webhook", provider=None, path="gitlab"),
        )
    )
    assert _match_provider_assignments(routing, "github", "issues.closed")
    assert _match_provider_assignments(routing, "github", "push")
    # Prefix match is allowed (same as the old inline logic).
    assert _match_provider_assignments(routing, "github", "push.branch")
    assert not _match_provider_assignments(routing, "github", "release.published")


def test_webhook_trigger_empty_events_filter_matches_everything():
    routing = _routing(matchers=(_matcher(trigger_type="webhook", provider=None, path="github"),))
    assert _match_provider_assignments(routing, "github", "whatever")


# ---------------------------------------------------------------------------
# _get_provider_routing — TTL cache with single-flight loads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_routing_cache_loads_once_within_ttl():
    load_mock = AsyncMock(return_value=_routing())
    with patch.object(webhooks, "_load_provider_routing", load_mock):
        r1 = await webhooks._get_provider_routing("github")
        r2 = await webhooks._get_provider_routing("github")
    assert r1 is r2
    load_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_routing_cache_reloads_after_ttl():
    load_mock = AsyncMock(return_value=_routing())
    with patch.object(webhooks, "_load_provider_routing", load_mock):
        await webhooks._get_provider_routing("github")
        # Age the cache entry past the TTL.
        ts, routing = webhooks._provider_routing_cache["github"]
        webhooks._provider_routing_cache["github"] = (
            ts - webhooks._PROVIDER_ROUTING_TTL_SECONDS - 1,
            routing,
        )
        await webhooks._get_provider_routing("github")
    assert load_mock.await_count == 2


@pytest.mark.asyncio
async def test_routing_cache_is_bounded():
    """The provider path segment is attacker-controlled — the cache must not
    grow without limit."""
    load_mock = AsyncMock(return_value=_routing())
    with patch.object(webhooks, "_load_provider_routing", load_mock):
        for i in range(webhooks._PROVIDER_ROUTING_MAX_ENTRIES + 5):
            await webhooks._get_provider_routing(f"bogus-{i}")
    assert len(webhooks._provider_routing_cache) <= webhooks._PROVIDER_ROUTING_MAX_ENTRIES


@pytest.mark.asyncio
async def test_overflow_evicts_oldest_entry_not_hot_providers():
    """Overflow must evict only the OLDEST entry — an attacker churning bogus
    provider paths must not blow away hot providers' snapshots (which would
    force DB reloads for legitimate traffic)."""
    # A hot, recently-refreshed provider...
    webhooks._provider_routing_cache["github"] = (time.monotonic(), _routing())
    # ...and a full cache of older bogus entries.
    stale = time.monotonic() - 10.0
    for i in range(webhooks._PROVIDER_ROUTING_MAX_ENTRIES - 1):
        webhooks._provider_routing_cache[f"bogus-{i}"] = (stale, _routing())

    load_mock = AsyncMock(return_value=_routing())
    with patch.object(webhooks, "_load_provider_routing", load_mock):
        await webhooks._get_provider_routing("bogus-new")

    assert "github" in webhooks._provider_routing_cache
    assert "bogus-new" in webhooks._provider_routing_cache
    assert len(webhooks._provider_routing_cache) <= webhooks._PROVIDER_ROUTING_MAX_ENTRIES


# ---------------------------------------------------------------------------
# receive_provider_webhook — the no-match fast path must open NO DB session
# ---------------------------------------------------------------------------


def _forbidden_session_factory():
    raise AssertionError("no-match provider webhook deliveries must not open a DB session")


def _patch_dedup(is_duplicate: bool = False):
    """Patch the Redis dedup layer with pass-through fakes."""
    return (
        patch.object(
            webhooks,
            "_claim_webhook_dedup_slot",
            new=AsyncMock(return_value=(is_duplicate, None, "dedup-key")),
        ),
        patch.object(
            webhooks,
            "_finalize_dedup",
            new=AsyncMock(side_effect=lambda _key, response: response),
        ),
        patch.object(
            webhooks,
            "_release_webhook_dedup_slot",
            new=AsyncMock(return_value=None),
        ),
    )


@pytest.mark.asyncio
async def test_no_agents_bound_delivery_touches_no_db():
    body = json.dumps({"action": "opened"}).encode()
    request = _make_request({"X-GitHub-Event": "issues"}, body=body)

    webhooks._provider_routing_cache["github"] = (
        time.monotonic(),
        _routing(has_agents=False),
    )

    p1, p2, p3 = _patch_dedup()
    with (
        p1,
        p2,
        p3,
        patch.object(webhooks, "async_session", new=_forbidden_session_factory),
    ):
        response = await webhooks.receive_provider_webhook("github", request)

    assert response == {"status": "ok", "triggered": [], "count": 0}


@pytest.mark.asyncio
async def test_no_matching_assignment_delivery_touches_no_db():
    body = json.dumps({"action": "published"}).encode()
    request = _make_request({"X-GitHub-Event": "release"}, body=body)

    webhooks._provider_routing_cache["github"] = (
        time.monotonic(),
        _routing(matchers=(_matcher(provider="github", event="issues"),)),
    )

    p1, p2, p3 = _patch_dedup()
    with (
        p1,
        p2,
        p3,
        patch.object(webhooks, "async_session", new=_forbidden_session_factory),
    ):
        response = await webhooks.receive_provider_webhook("github", request)

    assert response == {"status": "ok", "triggered": [], "count": 0}


# ---------------------------------------------------------------------------
# receive_provider_webhook — rotated-secret refresh-and-retry (pre-claim gate)
# ---------------------------------------------------------------------------


def _secret_routing(enc: str) -> _ProviderRouting:
    return _ProviderRouting(
        oauth_default_exists=True,
        extra_config_enc=enc,
        client_secret_enc=None,
        has_agents=False,
        matchers=(),
    )


@pytest.mark.asyncio
async def test_rotated_secret_refreshes_cache_and_retries():
    """A delivery signed with a freshly ROTATED secret must not be dropped
    for the whole TTL: the 401 against the cached (stale) secret triggers one
    rate-limited cache refresh, and verification is retried against the
    fresh secrets."""
    from fastapi import HTTPException

    body = json.dumps({"action": "opened"}).encode()
    request = _make_request({"X-GitHub-Event": "issues"}, body=body)

    # Stale-but-not-expired snapshot carrying the OLD secret (older than the
    # signature-retry min age, younger than the TTL).
    webhooks._provider_routing_cache["github"] = (
        time.monotonic() - 5.0,
        _secret_routing("old-enc"),
    )

    def _verify(body_, enc, request_, *, context=""):
        if enc == "old-enc":
            raise HTTPException(status_code=401, detail="Invalid signature")

    load_mock = AsyncMock(return_value=_secret_routing("new-enc"))
    p1, p2, p3 = _patch_dedup()
    with (
        p1,
        p2,
        p3,
        patch.object(webhooks, "_try_verify_webhook_signature", new=_verify),
        patch.object(webhooks, "_load_provider_routing", load_mock),
    ):
        response = await webhooks.receive_provider_webhook("github", request)

    assert response == {"status": "ok", "triggered": [], "count": 0}
    load_mock.assert_awaited_once_with("github")
    # The refreshed snapshot replaced the stale one in the cache.
    assert webhooks._provider_routing_cache["github"][1].extra_config_enc == "new-enc"


@pytest.mark.asyncio
async def test_invalid_signature_storm_cannot_force_reloads():
    """When the cached snapshot is FRESH (younger than the retry min age), a
    signature failure must NOT force a DB reload — an attacker sending junk
    signatures can't turn the cache back into a per-delivery load."""
    from fastapi import HTTPException

    body = json.dumps({"action": "opened"}).encode()
    request = _make_request({"X-GitHub-Event": "issues"}, body=body)

    webhooks._provider_routing_cache["github"] = (
        time.monotonic(),  # just written — inside the retry min age
        _secret_routing("current-enc"),
    )

    def _verify(body_, enc, request_, *, context=""):
        raise HTTPException(status_code=401, detail="Invalid signature")

    load_mock = AsyncMock(return_value=_secret_routing("current-enc"))
    p1, p2, p3 = _patch_dedup()
    with (
        p1,
        p2,
        p3,
        patch.object(webhooks, "_try_verify_webhook_signature", new=_verify),
        patch.object(webhooks, "_load_provider_routing", load_mock),
        pytest.raises(HTTPException) as exc_info,
    ):
        await webhooks.receive_provider_webhook("github", request)

    assert exc_info.value.status_code == 401
    load_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# receive_provider_webhook — the match path triggers with FRESH rows
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, scalars_all=None, scalar_one=None):
        self._scalars_all = scalars_all or []
        self._scalar_one = scalar_one

    def scalars(self):
        return SimpleNamespace(all=lambda: self._scalars_all)

    def scalar_one_or_none(self):
        return self._scalar_one


ADMIN_CLIENT_ID = "admin-app-client-id"


def _admin_default():
    return SimpleNamespace(
        id=uuid.uuid4(),
        client_id_enc=encrypt_value(ADMIN_CLIENT_ID),
        client_secret_enc=None,
        extra_config_enc=None,
    )


def _integration_row(integration_id, owner_id, *, client_id=ADMIN_CLIENT_ID, provider="github"):
    """A row as ``select(*_integration_row_columns())`` returns it."""
    return (
        integration_id,
        provider,
        owner_id,
        "oauth",
        False,
        encrypt_value(client_id),
        None,
    )


class _FakeRowsResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _FakeMatchSession:
    """Scripted session for the match path: OAuthDefault → agents →
    assignments → the matched integrations, re-read fresh."""

    def __init__(self, agents, assignments, integration_rows=(), *, oauth_default=None):
        self._responses = [
            _FakeResult(scalar_one=oauth_default or _admin_default()),
            _FakeResult(scalars_all=agents),
            _FakeResult(scalars_all=assignments),
            _FakeRowsResult(integration_rows),
        ]
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, _stmt):
        return self._responses.pop(0)

    async def commit(self):
        self.committed = True


def _fresh_row(wf_id, agent_id, trigger_type="integration_event", trigger_config=None):
    return SimpleNamespace(
        id=wf_id,
        agent_id=agent_id,
        name="On issue closed",
        enabled=True,
        trigger_type=trigger_type,
        trigger_config=(
            {"provider": "github", "event": "issues"}
            if trigger_config is None
            else trigger_config
        ),
    )


@pytest.mark.asyncio
async def test_matching_delivery_triggers_with_fresh_rows():
    agent_id = uuid.uuid4()
    wf_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    integration_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        name="Repo Agent",
        enabled=True,
        user_id=owner_id,
        integration_bindings={"github": [str(integration_id)]},
    )
    wf = _fresh_row(wf_id, agent_id)

    body = json.dumps({"action": "closed"}).encode()
    request = _make_request({"X-GitHub-Event": "issues"}, body=body)

    webhooks._provider_routing_cache["github"] = (
        time.monotonic(),
        _routing(
            matchers=(
                _matcher(
                    agent_id=str(agent_id),
                    assignment_id=str(wf_id),
                    provider="github",
                    event="issues",
                    integrations=((str(integration_id), None),),
                ),
            )
        ),
    )

    session = _FakeMatchSession(
        [agent], [wf], [_integration_row(integration_id, owner_id)]
    )
    trigger_mock = AsyncMock(return_value="run-123")
    p1, p2, p3 = _patch_dedup()
    with (
        p1,
        p2,
        p3,
        patch.object(webhooks, "async_session", new=lambda: session),
        patch.object(webhooks, "trigger_assignment", new=trigger_mock),
    ):
        response = await webhooks.receive_provider_webhook("github", request)

    assert response["count"] == 1
    assert response["triggered"][0]["run_id"] == "run-123"
    assert session.committed is True
    trigger_mock.assert_awaited_once()
    assert trigger_mock.await_args.kwargs["assignment"] is wf
    assert trigger_mock.await_args.kwargs["agent"] is agent


@pytest.mark.asyncio
async def test_rows_disabled_since_snapshot_are_skipped():
    """The cache may be up to TTL seconds stale — an assignment disabled
    after the snapshot must be silently skipped, not triggered."""
    agent_id = uuid.uuid4()
    wf_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        name="Repo Agent",
        enabled=True,
        user_id=uuid.uuid4(),
        integration_bindings={"github": [str(uuid.uuid4())]},
    )

    body = json.dumps({"action": "closed"}).encode()
    request = _make_request({"X-GitHub-Event": "issues"}, body=body)

    webhooks._provider_routing_cache["github"] = (
        time.monotonic(),
        _routing(
            matchers=(
                _matcher(
                    agent_id=str(agent_id),
                    assignment_id=str(wf_id),
                    provider="github",
                    event="issues",
                ),
            )
        ),
    )

    # Fresh reload returns the agent but NOT the (now disabled) assignment.
    session = _FakeMatchSession([agent], [])
    trigger_mock = AsyncMock(return_value="run-123")
    p1, p2, p3 = _patch_dedup()
    with (
        p1,
        p2,
        p3,
        patch.object(webhooks, "async_session", new=lambda: session),
        patch.object(webhooks, "trigger_assignment", new=trigger_mock),
    ):
        response = await webhooks.receive_provider_webhook("github", request)

    assert response == {"status": "ok", "triggered": [], "count": 0}
    trigger_mock.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "trigger_type,trigger_config",
    [
        # Edited to another event, moved to another provider, made manual,
        # or left with a selection that names no event.
        ("integration_event", {"provider": "github", "event": "pull_request"}),
        ("integration_event", {"provider": "slack", "event": "issues"}),
        ("manual", {}),
        ("integration_event", {"provider": "github", "event": ["issues"]}),
    ],
)
async def test_rows_reconfigured_since_snapshot_are_skipped(trigger_type, trigger_config):
    """Nothing refreshes the snapshot when a trigger is edited, so a delivery
    the stale snapshot still matches must not run a fresh row that no longer
    selects it."""
    agent_id = uuid.uuid4()
    wf_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, name="Repo Agent", enabled=True)
    wf = _fresh_row(wf_id, agent_id, trigger_type, trigger_config)

    body = json.dumps({"action": "closed"}).encode()
    request = _make_request({"X-GitHub-Event": "issues"}, body=body)

    webhooks._provider_routing_cache["github"] = (
        time.monotonic(),
        _routing(
            matchers=(
                _matcher(
                    agent_id=str(agent_id),
                    assignment_id=str(wf_id),
                    provider="github",
                    event="issues",
                ),
            )
        ),
    )

    session = _FakeMatchSession([agent], [wf])
    trigger_mock = AsyncMock(return_value="run-123")
    p1, p2, p3 = _patch_dedup()
    with (
        p1,
        p2,
        p3,
        patch.object(webhooks, "async_session", new=lambda: session),
        patch.object(webhooks, "trigger_assignment", new=trigger_mock),
    ):
        response = await webhooks.receive_provider_webhook("github", request)

    assert response == {"status": "ok", "triggered": [], "count": 0}
    trigger_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# Loader — never-matching assignments are dropped at load time
# ---------------------------------------------------------------------------


class _LoaderResult:
    def __init__(self, *, scalar_one=None, rows=None):
        self._scalar_one = scalar_one
        self._rows = rows or []

    def scalar_one_or_none(self):
        return self._scalar_one

    def all(self):
        return self._rows


class _LoaderSession:
    """Scripted session for _load_provider_routing: OAuthDefault → agents
    (id, owner, bindings, unreadable) → assignment column tuples → candidate integrations.

    ``agents`` is a list of ``(agent id, owner id, integration_bindings)``, all readable.
    """

    def __init__(self, agents, assignment_rows, integration_rows=(), *, oauth_default=None):
        self._results = [
            _LoaderResult(scalar_one=oauth_default),
            _LoaderResult(rows=[(*agent, False) for agent in agents]),
            _LoaderResult(rows=assignment_rows),
            _LoaderResult(rows=list(integration_rows)),
        ]
        self.executed = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, _stmt):
        self.executed += 1
        return self._results.pop(0)


@pytest.mark.asyncio
async def test_loader_drops_never_matching_assignments():
    """Assignments whose provider/path equality can never hold for this
    snapshot's provider must not inflate the cache or the storm-path scan;
    event/events filters stay runtime (they depend on the delivery)."""
    agent_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    integration_id = uuid.uuid4()
    rows = [
        # (id, agent_id, trigger_type, trigger_config)
        (uuid.uuid4(), agent_id, "integration_event", {"provider": "github", "event": "issues"}),
        (uuid.uuid4(), agent_id, "integration_event", {"provider": "slack"}),
        (uuid.uuid4(), agent_id, "webhook", {"path": "github"}),
        (uuid.uuid4(), agent_id, "webhook", {"path": "gitlab"}),
    ]
    session = _LoaderSession(
        [(agent_id, owner_id, {"github": [str(integration_id)]})],
        rows,
        [_integration_row(integration_id, owner_id)],
        oauth_default=_admin_default(),
    )
    with patch.object(webhooks, "async_session", new=lambda: session):
        routing = await webhooks._load_provider_routing("github")

    kept = {(m.trigger_type, m.provider, m.path) for m in routing.matchers}
    assert kept == {
        ("integration_event", "github", ""),
        ("webhook", None, "github"),
    }
    # The event prefix filter survives on the kept matcher (runtime concern).
    assert any(m.event == "issues" for m in routing.matchers)


# ---------------------------------------------------------------------------
# Loader shape — the N+1 must stay dead
# ---------------------------------------------------------------------------


def test_loader_uses_single_in_query_for_assignments():
    """The cache loader must fetch all bound agents' assignments with one
    ``IN`` query — the old handler issued one query per agent."""
    import inspect
    import re

    loader = inspect.getsource(webhooks._load_provider_routing)
    scoping = inspect.getsource(webhooks._scope_matchers)
    bound = inspect.getsource(webhooks._agents_bound_to_provider)
    assert "Assignment.agent_id.in_(" in loader
    assert "await _scope_matchers(" in loader
    assert "await _agents_bound_to_provider(" in loader
    # Count actual awaited execute calls (comments/strings can't skew it).
    assert "Integration.id.in_(" in scoping
    assert len(re.findall(r"await db\.execute\(", loader + scoping + bound)) == 4, (
        "loader must issue exactly 4 queries: OAuthDefault, agents, "
        "assignments IN, candidate integrations IN"
    )
    # Plus, only when an agent is bound to an integration its owner does not
    # own, the owners' current visibility of it: two queries per batch of
    # owners, called once - never a query per owner.
    assert len(re.findall(r"await _foreign_visible_by_owner\(", loader + scoping)) == 1
    visibility = inspect.getsource(webhooks._foreign_visible_by_owner)
    assert len(re.findall(r"await db\.execute\(", visibility)) == 2
    assert "union_all(" in visibility
    assert "_VISIBILITY_OWNERS_PER_QUERY" in visibility


def test_handler_has_no_per_agent_assignment_query():
    import inspect

    source = inspect.getsource(webhooks.receive_provider_webhook)
    assert "Assignment.agent_id == agent.id" not in source, (
        "the per-agent assignment N+1 must not return to the handler"
    )
