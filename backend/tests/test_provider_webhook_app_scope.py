"""The admin app's provider-level webhook reaches only its own integrations.

``POST /api/hooks/providers/{provider}`` is the webhook of the admin's OAuth
app. It used to hand every delivery to every enabled agent bound to ANY
integration of the provider - an integration on a user's own app, one the
agent's owner may not use, or one recording other GitHub App installations
all received the admin app's events. These tests pin the scoping: the app
(stored client id = the OAuth default's), the agent owner's right to use the
integration, the GitHub App installation, and the fresh re-check on
the match path. Every owner here is an ordinary user: this route has no admin
bypass, the scoping is by integration and owner.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import sqlalchemy as sa
from fastapi import HTTPException

from app.api.v1 import webhooks
from app.api.v1.webhooks import (
    _AssignmentMatcher,
    _event_installation_id,
    _match_provider_assignments,
    _ProviderRouting,
    _recorded_installations,
)
from app.core.security import encrypt_value

ADMIN_CLIENT_ID = "admin-app-client-id-4299"
OWN_CLIENT_ID = "users-own-app-client-id"



@pytest.fixture(autouse=True)
def _strict_admin_app_scoping(monkeypatch):
    """These tests pin the admin-app narrowing, which INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION turns on.

    The default (off) keeps what the route delivered before, see the tests named ``*_by_default``.
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "integration_notifications_require_verification", True)


def _default_mode(monkeypatch):
    from app.core.config import settings
    from app.services import integration_notification_auth

    integration_notification_auth._log_state.clear()

    monkeypatch.setattr(settings, "integration_notifications_require_verification", False)


@pytest.fixture(autouse=True)
def _clean_cache():
    webhooks._invalidate_provider_routing_cache()
    webhooks._unreadable_integrations_warned.clear()
    yield
    webhooks._invalidate_provider_routing_cache()
    webhooks._unreadable_integrations_warned.clear()


def _admin_default():
    return SimpleNamespace(
        id=uuid.uuid4(),
        client_id_enc=encrypt_value(ADMIN_CLIENT_ID),
        client_secret_enc=None,
        extra_config_enc=None,
    )


def _row(
    integration_id,
    owner_id,
    *,
    client_id=ADMIN_CLIENT_ID,
    provider="github",
    auth_type="oauth",
    share_acknowledged=False,
    extra=None,
):
    return (
        integration_id,
        provider,
        owner_id,
        auth_type,
        share_acknowledged,
        encrypt_value(client_id) if client_id else None,
        encrypt_value(json.dumps(extra)) if extra is not None else None,
    )


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
    def __init__(self, results):
        self._results = list(results)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, _stmt):
        return self._results.pop(0)

    async def commit(self):
        return None


async def _load(
    agents, assignment_rows, integration_rows, *, oauth_default="admin", visibility=None
):
    """``visibility`` scripts the two queries ``_foreign_visible_by_owner``
    issues for a foreign candidate: ``(memberships, visible (id, owner) rows)``."""
    od = _admin_default() if oauth_default == "admin" else oauth_default
    results = [
        _Result(scalar_one=od),
        # ``(id, owner, bindings, unreadable)``: every scripted agent is readable.
        _Result(rows=[(*agent, False) for agent in agents]),
        _Result(rows=assignment_rows),
        _Result(rows=integration_rows),
    ]
    if visibility is not None:
        memberships, visible = visibility
        results += [_Result(rows=memberships), _Result(rows=visible)]
    session = _Session(results)
    with patch.object(webhooks, "async_session", new=lambda: session):
        routing = await webhooks._load_provider_routing("github")
    if visibility is not None:
        assert session._results == [], "the visibility queries did not run"
    return routing


def _issue_assignment(agent_id, **config):
    return (
        uuid.uuid4(),
        agent_id,
        "integration_event",
        {"provider": "github", "event": "issues", **config},
    )


def _reachable(routing) -> list[str]:
    """The agents the admin app reaches under the strict rule.

    The snapshot keeps every matcher whatever the mode - the option can be switched
    at any time - with the integrations the rule allows it: none means unreachable,
    and the strict match skips it.
    """
    kept = sorted(m.agent_id for m in routing.matchers if m.integrations)
    matched = sorted(m.agent_id for m in _match_provider_assignments(routing, "github", "issues.opened"))
    assert matched == kept
    return kept


# ---------------------------------------------------------------------------
# The loader: which assignments the admin app can reach at all
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_integration_on_the_users_own_app_never_hears_the_admin_app():
    admin_owner, own_owner = uuid.uuid4(), uuid.uuid4()
    admin_agent, own_agent = uuid.uuid4(), uuid.uuid4()
    admin_integ, own_integ = uuid.uuid4(), uuid.uuid4()
    routing = await _load(
        [
            (admin_agent, admin_owner, {"github": [str(admin_integ)]}),
            (own_agent, own_owner, {"github": [str(own_integ)]}),
        ],
        [_issue_assignment(admin_agent), _issue_assignment(own_agent)],
        [
            _row(admin_integ, admin_owner),
            _row(own_integ, own_owner, client_id=OWN_CLIENT_ID),
        ],
    )

    assert _reachable(routing) == [str(admin_agent)]
    (admin_matcher,) = [m for m in routing.matchers if m.agent_id == str(admin_agent)]
    assert admin_matcher.integrations == ((str(admin_integ), None),)
    # The delivery itself: only the admin-app agent is matched.
    matched = _match_provider_assignments(routing, "github", "issues.opened")
    assert [m.agent_id for m in matched] == [str(admin_agent)]


@pytest.mark.asyncio
async def test_an_integration_without_client_credentials_is_not_on_the_admin_app():
    owner, agent, integ = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    routing = await _load(
        [(agent, owner, {"github": [str(integ)]})],
        [_issue_assignment(agent)],
        [_row(integ, owner, client_id=None)],
    )
    assert _reachable(routing) == []


@pytest.mark.asyncio
async def test_an_agent_owner_cannot_hear_through_someone_elses_personal_token():
    """Even visible to the agent owner (group-shared): a personal OAuth token
    without the owner's acknowledgement is not theirs to hear through."""
    integ_owner, agent_owner = uuid.uuid4(), uuid.uuid4()
    agent, integ = uuid.uuid4(), uuid.uuid4()
    visible = AsyncMock(return_value={agent_owner: {integ}})
    with patch.object(webhooks, "_foreign_visible_by_owner", new=visible):
        routing = await _load(
            [(agent, agent_owner, {"github": [str(integ)]})],
            [_issue_assignment(agent)],
            [_row(integ, integ_owner)],
        )
    assert _reachable(routing) == []


@pytest.mark.asyncio
async def test_a_share_acknowledged_token_may_be_heard_by_another_owner_who_sees_it():
    integ_owner, agent_owner = uuid.uuid4(), uuid.uuid4()
    agent, integ = uuid.uuid4(), uuid.uuid4()
    visible = AsyncMock(return_value={agent_owner: {integ}})
    with patch.object(webhooks, "_foreign_visible_by_owner", new=visible):
        routing = await _load(
            [(agent, agent_owner, {"github": [str(integ)]})],
            [_issue_assignment(agent)],
            [_row(integ, integ_owner, share_acknowledged=True)],
        )
    assert _reachable(routing) == [str(agent)]
    assert visible.await_args.args[1] == {agent_owner: {str(integ)}}


@pytest.mark.asyncio
async def test_an_acknowledged_share_no_longer_visible_to_the_owner_is_not_heard():
    """Removed from the group that exposed it: the binding survives, the
    acknowledgement is still set, the delivery must not."""
    integ_owner, agent_owner = uuid.uuid4(), uuid.uuid4()
    agent, integ = uuid.uuid4(), uuid.uuid4()
    with patch.object(webhooks, "_foreign_visible_by_owner", new=AsyncMock(return_value={})):
        routing = await _load(
            [(agent, agent_owner, {"github": [str(integ)]})],
            [_issue_assignment(agent)],
            [_row(integ, integ_owner, share_acknowledged=True)],
        )
    assert _reachable(routing) == []


@pytest.mark.asyncio
async def test_someone_elses_non_oauth_integration_must_be_visible_too():
    integ_owner, agent_owner = uuid.uuid4(), uuid.uuid4()
    agent, integ = uuid.uuid4(), uuid.uuid4()
    routing = await _load(
        [(agent, agent_owner, {"github": [str(integ)]})],
        [_issue_assignment(agent)],
        [_row(integ, integ_owner, auth_type="api_key")],
        visibility=([], []),
    )
    assert _reachable(routing) == []


@pytest.mark.asyncio
async def test_foreign_visibility_uses_the_owners_groups_and_no_admin_bypass(monkeypatch):
    from app.api.v1 import helpers

    owner, own_integ, foreign_integ = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    calls: dict = {}

    def _filter(model, user_id, group_ids, **kwargs):
        calls["filter"] = (model, user_id, group_ids, kwargs)
        return sa.true()

    monkeypatch.setattr(helpers, "user_visible_filter", _filter)
    rows = {
        own_integ: webhooks._IntegrationRow(*_row(own_integ, owner)),
        foreign_integ: webhooks._IntegrationRow(
            *_row(foreign_integ, uuid.uuid4(), share_acknowledged=True)
        ),
    }
    session = _Session(
        [
            _Result(rows=[(owner, "group-1")]),  # the owner's memberships
            _Result(rows=[(foreign_integ, str(owner))]),
        ]
    )
    visible = await webhooks._foreign_visible_by_owner(
        session, {owner: (str(own_integ), str(foreign_integ))}, rows
    )
    assert visible == {owner: {foreign_integ}}
    assert calls["filter"][1:3] == (owner, ["group-1"])
    assert "is_admin" not in calls["filter"][3]


@pytest.mark.asyncio
async def test_foreign_visibility_queries_nothing_when_no_foreign_candidate_could_be_usable():
    """Own candidates, and someone else's unacknowledged OAuth token (never
    usable, visible or not), need no visibility read."""
    owner, integ, personal = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    rows = {
        integ: webhooks._IntegrationRow(*_row(integ, owner)),
        personal: webhooks._IntegrationRow(*_row(personal, uuid.uuid4())),
    }
    session = _Session([])  # any execute would pop from an empty list
    assert (
        await webhooks._foreign_visible_by_owner(
            session, {owner: (str(integ), str(personal)), None: (str(personal),)}, rows
        )
        == {}
    )


@pytest.mark.asyncio
async def test_foreign_visibility_is_read_per_batch_of_owners_never_per_owner(monkeypatch):
    monkeypatch.setattr(webhooks, "_VISIBILITY_OWNERS_PER_QUERY", 2)
    integ = uuid.uuid4()
    rows = {integ: webhooks._IntegrationRow(*_row(integ, uuid.uuid4(), share_acknowledged=True))}
    owners = {uuid.uuid4(): (str(integ),) for _ in range(3)}
    session = _Session([_Result(rows=[]) for _ in range(4)])
    assert await webhooks._foreign_visible_by_owner(session, owners, rows) == {}
    assert session._results == [], "two batches, memberships + visibility each"


@pytest.mark.asyncio
async def test_a_pinned_integration_decides_over_the_agents_other_bindings():
    owner, agent = uuid.uuid4(), uuid.uuid4()
    admin_integ, own_integ = uuid.uuid4(), uuid.uuid4()
    routing = await _load(
        [(agent, owner, {"github": [str(admin_integ), str(own_integ)]})],
        [_issue_assignment(agent, integration_id=str(own_integ))],
        [
            _row(admin_integ, owner),
            _row(own_integ, owner, client_id=OWN_CLIENT_ID),
        ],
    )
    assert _reachable(routing) == []


@pytest.mark.asyncio
async def test_without_an_admin_app_nothing_is_reachable():
    """No OAuth default: no integration is on the admin app, so an unsigned
    POST to this route (nothing to verify it against) triggers nobody."""
    owner, agent, integ = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    session = _Session(
        [
            _Result(scalar_one=None),
            _Result(rows=[(agent, owner, {"github": [str(integ)]}, False)]),
            _Result(rows=[_issue_assignment(agent)]),
        ]
    )
    with patch.object(webhooks, "async_session", new=lambda: session):
        routing = await webhooks._load_provider_routing("github")
    assert _reachable(routing) == []
    assert routing.has_agents is True


@pytest.mark.asyncio
async def test_an_integration_on_the_admin_app_keeps_its_delivery_whatever_it_recorded():
    """No regression for the admin app: an integration created before
    ``uses_admin_oauth_credentials`` existed (NULL) carries the admin's client
    id and is reached exactly as before - a row that recorded nothing is
    judged by its credentials (``integration_state.uses_admin_oauth_app``)."""
    owner, agent, integ = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    webhook_assignment = (uuid.uuid4(), agent, "webhook", {"path": "github"})
    routing = await _load(
        [(agent, owner, {"github": [str(integ)]})],
        [_issue_assignment(agent), webhook_assignment],
        [_row(integ, owner)],
    )
    assert len(routing.matchers) == 2
    assert len(_match_provider_assignments(routing, "github", "issues.opened")) == 2


@pytest.mark.asyncio
async def test_the_recorded_choice_decides_the_app_whatever_the_client_id_says():
    """An integration created on the admin app is on it, whatever client id it
    stores; one whose owner typed the admin's client id recorded its own app."""
    owner = uuid.uuid4()
    created, typed = uuid.uuid4(), uuid.uuid4()
    agent_created, agent_typed = uuid.uuid4(), uuid.uuid4()
    routing = await _load(
        [(agent_created, owner, {"github": [str(created)]}), (agent_typed, owner, {"github": [str(typed)]})],
        [_issue_assignment(agent_created), _issue_assignment(agent_typed)],
        [(*_row(created, owner, client_id="another-client-id"), True), (*_row(typed, owner), False)],
    )
    assert _reachable(routing) == [str(agent_created)]


# ---------------------------------------------------------------------------
# GitHub App installations
# ---------------------------------------------------------------------------


def test_recorded_installations_reads_both_shapes():
    enc = encrypt_value(json.dumps({"installations": [{"id": 11}, {"id": "12"}]}))
    assert _recorded_installations("github", enc) == frozenset({"11", "12"})
    legacy = encrypt_value(json.dumps({"installation_id": 13}))
    assert _recorded_installations("github", legacy) == frozenset({"13"})
    assert _recorded_installations("github", encrypt_value(json.dumps({}))) is None
    assert _recorded_installations("github", None) is None
    assert _recorded_installations("linkedin", enc) is None


def test_event_installation_id_reads_the_github_payload():
    assert _event_installation_id("github", {"installation": {"id": 42}}) == "42"
    assert _event_installation_id("github", {"action": "opened"}) is None
    assert _event_installation_id("linkedin", {"installation": {"id": 42}}) is None


def _routing_with(*integrations):
    matcher = _AssignmentMatcher(
        agent_id=str(uuid.uuid4()),
        assignment_id=str(uuid.uuid4()),
        trigger_type="integration_event",
        provider="github",
        event="issues",
        path="",
        events_filter=(),
        integrations=integrations,
    )
    return _ProviderRouting(
        oauth_default_exists=True,
        extra_config_enc=None,
        client_secret_enc=None,
        has_agents=True,
        matchers=(matcher,),
    )


def test_a_delivery_of_another_installation_is_not_handed_over():
    routing = _routing_with(("integ", frozenset({"111"})))
    assert _match_provider_assignments(routing, "github", "issues.opened", "222") == []
    assert len(_match_provider_assignments(routing, "github", "issues.opened", "111")) == 1


def test_an_integration_recording_no_installation_keeps_every_delivery():
    routing = _routing_with(("integ", None))
    assert len(_match_provider_assignments(routing, "github", "issues.opened", "222")) == 1
    assert len(_match_provider_assignments(routing, "github", "issues.opened", None)) == 1


def test_a_matcher_with_no_integration_is_never_reached():
    assert _match_provider_assignments(_routing_with(), "github", "issues.opened") == []


@pytest.mark.asyncio
async def test_the_loader_carries_the_recorded_installations():
    owner, agent, integ = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    routing = await _load(
        [(agent, owner, {"github": [str(integ)]})],
        [_issue_assignment(agent)],
        [_row(integ, owner, extra={"installations": [{"id": 111, "account_login": "org-a"}]})],
    )
    assert routing.matchers[0].integrations == ((str(integ), frozenset({"111"})),)
    assert _match_provider_assignments(routing, "github", "issues.opened", "222") == []


# ---------------------------------------------------------------------------
# The match path re-checks on fresh rows
# ---------------------------------------------------------------------------


def _patch_dedup():
    return (
        patch.object(
            webhooks, "_claim_webhook_dedup_slot", AsyncMock(return_value=(False, None, None))
        ),
        patch.object(webhooks, "_finalize_dedup", AsyncMock(side_effect=lambda _k, r: r)),
        patch.object(webhooks, "_release_webhook_dedup_slot", AsyncMock()),
    )


def _request(body: bytes):
    async def _body() -> bytes:
        return body

    return SimpleNamespace(
        headers={"X-GitHub-Event": "issues"},
        body=_body,
        url="https://copilot.example/api/hooks/providers/github",
        method="POST",
        query_params={},
        client=SimpleNamespace(host="203.0.113.7"),
    )


async def _deliver(
    fresh_integration_rows,
    *,
    installation_id=None,
    fresh_bindings="same",
    fresh_pin=None,
    extra_results=None,
):
    """``extra_results(integration id, owner)`` scripts what the match path
    reads after the integrations (the ``_foreign_visible_by_owner`` queries)."""
    owner, agent_id, wf_id, integ = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    bindings = {"github": [str(integ)]} if fresh_bindings == "same" else fresh_bindings
    agent = SimpleNamespace(
        id=agent_id, name="Repo Agent", enabled=True, user_id=owner, integration_bindings=bindings
    )
    config = {"provider": "github", "event": "issues"}
    if fresh_pin is not None:
        config["integration_id"] = fresh_pin
    wf = SimpleNamespace(
        id=wf_id,
        agent_id=agent_id,
        name="On issue",
        enabled=True,
        trigger_type="integration_event",
        trigger_config=config,
    )
    webhooks._provider_routing_cache["github"] = (
        time.monotonic(),
        _routing_with_ids(agent_id, wf_id, integ),
    )
    session = _Session(
        [
            _Result(scalar_one=_admin_default()),
            _Result(scalars_all=[agent]),
            _Result(scalars_all=[wf]),
            _Result(rows=[r(integ, owner) for r in fresh_integration_rows]),
            *(extra_results(integ, owner) if extra_results else ()),
        ]
    )
    payload = {"action": "opened"}
    if installation_id:
        payload["installation"] = {"id": installation_id}
    trigger = AsyncMock(return_value="run-1")
    p1, p2, p3 = _patch_dedup()
    with (
        p1,
        p2,
        p3,
        patch.object(webhooks, "async_session", new=lambda: session),
        patch.object(webhooks, "trigger_assignment", new=trigger),
    ):
        response = await webhooks.receive_provider_webhook(
            "github", _request(json.dumps(payload).encode())
        )
    return response, trigger


def _routing_with_ids(agent_id, wf_id, integ):
    matcher = _AssignmentMatcher(
        agent_id=str(agent_id),
        assignment_id=str(wf_id),
        trigger_type="integration_event",
        provider="github",
        event="issues",
        path="",
        events_filter=(),
        integrations=((str(integ), None),),
    )
    return _ProviderRouting(
        oauth_default_exists=True,
        extra_config_enc=None,
        client_secret_enc=None,
        has_agents=True,
        matchers=(matcher,),
    )


@pytest.mark.asyncio
async def test_a_client_id_replaced_since_the_snapshot_stops_the_delivery_now():
    response, trigger = await _deliver(
        [lambda i, o: _row(i, o, client_id=OWN_CLIENT_ID)]
    )
    assert response["count"] == 0
    trigger.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_installation_recorded_since_the_snapshot_is_honoured_now():
    response, trigger = await _deliver(
        [lambda i, o: _row(i, o, extra={"installations": [{"id": 111}]})],
        installation_id=222,
    )
    assert response["count"] == 0
    trigger.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_binding_removed_since_the_snapshot_stops_the_delivery_now():
    response, trigger = await _deliver([lambda i, o: _row(i, o)], fresh_bindings={})
    assert response["count"] == 0
    trigger.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_pin_moved_to_another_integration_since_the_snapshot_stops_the_delivery_now():
    response, trigger = await _deliver(
        [lambda i, o: _row(i, o)], fresh_pin=str(uuid.uuid4())
    )
    assert response["count"] == 0
    trigger.assert_not_awaited()


def test_an_unreadable_config_matches_no_named_installation():
    for enc in ("not-a-fernet-token", encrypt_value(json.dumps(["x"]))):
        recorded = _recorded_installations("github", enc)
        assert recorded == frozenset()
        routing = _routing_with(("integ", recorded))
        assert _match_provider_assignments(routing, "github", "issues.opened", "111") == []
        # A delivery naming no installation is not narrowed by it.
        assert len(_match_provider_assignments(routing, "github", "issues.opened", None)) == 1
    malformed = encrypt_value(json.dumps({"installations": "111"}))
    assert _recorded_installations("github", malformed) == frozenset()


@pytest.mark.asyncio
async def test_an_admin_app_integration_still_triggers_on_the_match_path():
    # It records no installation, so the match path reads its token; with
    # no answer known for that token yet, it is reached as before.
    response, trigger = await _deliver(
        [lambda i, o: _row(i, o)],
        installation_id=222,
        extra_results=lambda i, _o: [
            _Result(rows=[(i, "GitHub", "oauth", None, encrypt_value("gho-token"), None, None)])
        ],
    )
    assert response["count"] == 1
    trigger.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_share_the_owner_lost_since_the_snapshot_stops_the_delivery_now():
    integ_owner = uuid.uuid4()
    response, trigger = await _deliver(
        [lambda i, _o: _row(i, integ_owner, share_acknowledged=True)],
        extra_results=lambda _i, _o: [_Result(rows=[]), _Result(rows=[])],
    )
    assert response["count"] == 0
    trigger.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_share_the_owner_still_sees_keeps_the_delivery():
    integ_owner = uuid.uuid4()
    response, trigger = await _deliver(
        [lambda i, _o: _row(i, integ_owner, share_acknowledged=True)],
        extra_results=lambda i, o: [
            _Result(rows=[(o, "group-a")]),
            _Result(rows=[(i, str(o))]),
        ],
    )
    assert response["count"] == 1
    trigger.assert_awaited_once()


# ---------------------------------------------------------------------------
# Unreadable stored credentials are surfaced, never read as "nothing matched"
# ---------------------------------------------------------------------------


def _unreadable_default():
    return SimpleNamespace(
        id=uuid.uuid4(), client_id_enc="not-a-fernet-token", client_secret_enc=None, extra_config_enc=None
    )


@pytest.mark.asyncio
async def test_an_unreadable_admin_client_id_is_flagged_and_logged(caplog):
    owner, agent, integ = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with caplog.at_level(logging.ERROR, logger=webhooks.__name__):
        routing = await _load(
            [(agent, owner, {"github": [str(integ)]})],
            [_issue_assignment(agent)],
            [_row(integ, owner)],
            oauth_default=_unreadable_default(),
        )
    assert routing.admin_app_unreadable is True
    assert _reachable(routing) == []
    assert "client id cannot be decrypted" in caplog.text
    assert "not-a-fernet-token" not in caplog.text


@pytest.mark.asyncio
async def test_a_delivery_the_admin_app_cannot_route_is_refused_before_the_dedup_slot():
    """503 before the dedup slot: nothing is remembered as handled, so the
    provider shows a failed delivery that can be retried once it is fixed."""
    webhooks._provider_routing_cache["github"] = (
        time.monotonic(),
        _ProviderRouting(
            oauth_default_exists=True,
            extra_config_enc=None,
            client_secret_enc=None,
            has_agents=True,
            matchers=(),
            admin_app_unreadable=True,
        ),
    )
    claim = AsyncMock(side_effect=AssertionError("dedup slot claimed"))
    with (
        patch.object(webhooks, "_claim_webhook_dedup_slot", claim),
        pytest.raises(HTTPException) as refused,
    ):
        await webhooks.receive_provider_webhook("github", _request(b'{"action": "opened"}'))
    assert refused.value.status_code == 503
    assert refused.value.headers == {"Retry-After": "300"}
    claim.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_admin_client_id_unreadable_on_the_fresh_row_refuses_and_frees_the_slot():
    owner, agent_id, wf_id, integ = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    webhooks._provider_routing_cache["github"] = (
        time.monotonic(),
        _routing_with_ids(agent_id, wf_id, integ),
    )
    agent = SimpleNamespace(
        id=agent_id,
        name="Repo Agent",
        enabled=True,
        user_id=owner,
        integration_bindings={"github": [str(integ)]},
    )
    wf = SimpleNamespace(
        id=wf_id,
        agent_id=agent_id,
        name="On issue",
        enabled=True,
        trigger_type="integration_event",
        trigger_config={"provider": "github", "event": "issues"},
    )
    session = _Session(
        [
            _Result(scalar_one=_unreadable_default()),
            _Result(scalars_all=[agent]),
            _Result(scalars_all=[wf]),
        ]
    )
    release = AsyncMock()
    trigger = AsyncMock(return_value="run-1")
    with (
        patch.object(
            webhooks, "_claim_webhook_dedup_slot", AsyncMock(return_value=(False, None, "k"))
        ),
        patch.object(webhooks, "_release_webhook_dedup_slot", release),
        patch.object(webhooks, "async_session", new=lambda: session),
        patch.object(webhooks, "trigger_assignment", new=trigger),
        pytest.raises(HTTPException) as refused,
    ):
        await webhooks.receive_provider_webhook("github", _request(b'{"action": "opened"}'))
    assert refused.value.status_code == 503
    release.assert_awaited_once_with("k")
    trigger.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_integration_with_an_unreadable_client_id_is_named_once(caplog):
    owner, agent, integ = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    row = (integ, "github", owner, "oauth", False, "not-a-fernet-token", None)
    with caplog.at_level(logging.WARNING, logger=webhooks.__name__):
        for _ in range(2):
            routing = await _load(
                [(agent, owner, {"github": [str(integ)]})], [_issue_assignment(agent)], [row]
            )
            assert _reachable(routing) == []
    warnings = [r for r in caplog.records if str(integ) in r.getMessage()]
    assert len(warnings) == 1
    assert "client id cannot be decrypted" in warnings[0].getMessage()
    assert "not-a-fernet-token" not in caplog.text


@pytest.mark.asyncio
async def test_an_integration_with_an_unreadable_config_is_named_once(caplog):
    owner, agent, integ = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    row = (integ, "github", owner, "oauth", False, encrypt_value(ADMIN_CLIENT_ID), "garbled")
    with caplog.at_level(logging.WARNING, logger=webhooks.__name__):
        for _ in range(2):
            routing = await _load(
                [(agent, owner, {"github": [str(integ)]})], [_issue_assignment(agent)], [row]
            )
            # Still reached by a delivery naming no installation.
            assert routing.matchers[0].integrations == ((str(integ), frozenset()),)
    warnings = [r for r in caplog.records if str(integ) in r.getMessage()]
    assert len(warnings) == 1
    assert "configuration cannot be decrypted" in warnings[0].getMessage()


# ---------------------------------------------------------------------------
# The default: what the route reached keeps receiving it
# ---------------------------------------------------------------------------


def test_by_default_a_matcher_with_no_integration_is_still_reached(monkeypatch):
    _default_mode(monkeypatch)
    assert len(_match_provider_assignments(_routing_with(), "github", "issues.opened")) == 1


def test_by_default_a_delivery_of_another_installation_is_still_handed_over(monkeypatch):
    _default_mode(monkeypatch)
    routing = _routing_with(("integ", frozenset({"111"})))
    assert len(_match_provider_assignments(routing, "github", "issues.opened", "222")) == 1


@pytest.mark.asyncio
async def test_by_default_an_integration_on_the_users_own_app_still_hears_the_admin_app(monkeypatch):
    _default_mode(monkeypatch)
    admin_owner, own_owner = uuid.uuid4(), uuid.uuid4()
    admin_agent, own_agent = uuid.uuid4(), uuid.uuid4()
    admin_integ, own_integ = uuid.uuid4(), uuid.uuid4()
    routing = await _load(
        [
            (admin_agent, admin_owner, {"github": [str(admin_integ)]}),
            (own_agent, own_owner, {"github": [str(own_integ)]}),
        ],
        [_issue_assignment(admin_agent), _issue_assignment(own_agent)],
        [
            _row(admin_integ, admin_owner),
            _row(own_integ, own_owner, client_id=OWN_CLIENT_ID),
        ],
    )
    matched = _match_provider_assignments(routing, "github", "issues.opened")
    assert {m.agent_id for m in matched} == {str(admin_agent), str(own_agent)}


@pytest.mark.asyncio
async def test_a_snapshot_serves_whichever_mode_is_on_when_the_delivery_arrives(monkeypatch):
    """The option is a Settings > Access and security switch: flipping it applies to the next delivery,
    not once the 30-second routing snapshot expires."""
    from app.core.config import settings

    _default_mode(monkeypatch)
    admin_owner, own_owner = uuid.uuid4(), uuid.uuid4()
    admin_agent, own_agent = uuid.uuid4(), uuid.uuid4()
    admin_integ, own_integ = uuid.uuid4(), uuid.uuid4()
    routing = await _load(
        [
            (admin_agent, admin_owner, {"github": [str(admin_integ)]}),
            (own_agent, own_owner, {"github": [str(own_integ)]}),
        ],
        [_issue_assignment(admin_agent), _issue_assignment(own_agent)],
        [_row(admin_integ, admin_owner), _row(own_integ, own_owner, client_id=OWN_CLIENT_ID)],
    )
    monkeypatch.setattr(settings, "integration_notifications_require_verification", True)
    assert [m.agent_id for m in _match_provider_assignments(routing, "github", "issues.opened")] == [str(admin_agent)]
    monkeypatch.setattr(settings, "integration_notifications_require_verification", False)
    assert len(_match_provider_assignments(routing, "github", "issues.opened")) == 2


@pytest.mark.asyncio
async def test_by_default_a_scoping_failure_never_costs_the_routing(monkeypatch, caplog):
    _default_mode(monkeypatch)
    owner, agent, integ = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    monkeypatch.setattr(webhooks, "_scope_matchers", AsyncMock(side_effect=RuntimeError("boom")))
    with caplog.at_level(logging.WARNING, logger=webhooks.__name__):
        routing = await _load([(agent, owner, {"github": [str(integ)]})], [_issue_assignment(agent)], [])
    assert len(_match_provider_assignments(routing, "github", "issues.opened")) == 1
    assert "handed out as before" in caplog.text


@pytest.mark.asyncio
async def test_in_strict_mode_a_scoping_failure_fails_the_routing(monkeypatch):
    owner, agent, integ = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    monkeypatch.setattr(webhooks, "_scope_matchers", AsyncMock(side_effect=RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        await _load([(agent, owner, {"github": [str(integ)]})], [_issue_assignment(agent)], [])


class _NestedSession(_Session):
    def __init__(self, results):
        super().__init__(results)
        self.savepoints = 0

    def begin_nested(self):
        session = self

        class _Savepoint:
            async def __aenter__(self):
                session.savepoints += 1

            async def __aexit__(self, *_exc):
                return False

        return _Savepoint()


@pytest.mark.asyncio
async def test_by_default_the_report_runs_in_a_savepoint_and_never_fails_the_delivery(monkeypatch):
    _default_mode(monkeypatch)
    failing = AsyncMock(side_effect=RuntimeError("visibility query failed"))
    monkeypatch.setattr(webhooks, "_admin_app_integrations_by_assignment", failing)
    session = _NestedSession([])
    assert await webhooks._admin_app_integrations_or_report(session, "github", _admin_default(), []) == {}
    assert session.savepoints == 1


@pytest.mark.asyncio
async def test_in_strict_mode_the_rule_is_applied_on_the_delivery_session(monkeypatch):
    failing = AsyncMock(side_effect=RuntimeError("visibility query failed"))
    monkeypatch.setattr(webhooks, "_admin_app_integrations_by_assignment", failing)
    session = _NestedSession([])
    with pytest.raises(RuntimeError):
        await webhooks._admin_app_integrations_or_report(session, "github", _admin_default(), [])
    assert session.savepoints == 0
