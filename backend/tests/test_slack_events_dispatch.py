"""A verified Slack message reaches only the assignments connected to it, once.

``app/services/slack_events.py`` receives the integrations whose app signed a delivery
(see ``test_integration_notification_auth``) and decides which assignments the message
may run, and it claims the message in the event poller's own seen set so the poller
and a Slack retry never run it again. The database, Redis, Slack's ``auth.test`` and
the assignment engine are replaced by recording fakes: nothing leaves the process. The
SQL behind ``eligible_integration_ids`` (the poller's token gate, the template
resolution filter) runs against PostgreSQL in ``test_slack_events_scope_postgres``.
"""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import HTTPException

from app.core.security import encrypt_value
from app.services import slack_events
from app.services.ai_state import AI_DISABLED_CODE
from app.services.slack_events import (
    SlackIdentity,
    delivery_scope,
    integration_may_see,
    seen_set_key,
    slack_message_key,
)

TEAM_A, TEAM_B = "T-A", "T-B"
CHANNEL, TS = "C123", "1700000000.000100"


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._rows))

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return [(getattr(row, "id", row),) for row in self._rows]


class _Session:
    """Answers each ``select`` with the rows of the table it selects."""

    def __init__(self, tables: dict[str, list]):
        self.tables = tables
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, statement, *_a, **_k):
        table = statement.column_descriptions[0]["entity"].__tablename__
        return _Result(self.tables.get(table, []))

    async def get(self, model, key):
        """``AsyncSession.get``, which the trigger visibility check reads the acting user with."""
        return next((row for row in self.tables.get(model.__tablename__, []) if getattr(row, "id", None) == key), None)

    async def commit(self):
        self.commits += 1

    def in_transaction(self):
        return False


class _Redis:
    """Sorted sets, plain sets and strings: the seen set, its legacy form, the identity cache."""

    def __init__(self, *, broken: bool = False):
        self.sets: dict[str, dict[str, float]] = {}
        self.plain: dict[str, set] = {}
        self.values: dict[str, str] = {}
        self.broken = broken

    def members(self, key) -> set:
        return set(self.sets.get(key, {}))

    async def exists(self, key):
        if self.broken:
            raise ConnectionError("redis down")
        return int(key in self.values)

    async def sismember(self, key, member):
        if self.broken:
            raise ConnectionError("redis down")
        return member in self.plain.get(key, set())

    async def zremrangebyscore(self, key, _min, max_score):
        scores = self.sets.get(key, {})
        for member in [m for m, s in scores.items() if s <= max_score]:
            del scores[member]

    async def zadd(self, key, mapping, nx=False):
        if self.broken:
            raise ConnectionError("redis down")
        scores = self.sets.setdefault(key, {})
        added = 0
        for member, score in mapping.items():
            if nx and member in scores:
                continue
            scores[member] = score
            added += 1
        return added

    async def zrem(self, key, member):
        self.sets.get(key, {}).pop(member, None)
        return 1

    async def expire(self, *_a, **_k):
        return True

    async def get(self, key):
        if self.broken:
            raise ConnectionError("redis down")
        return self.values.get(key)

    async def set(self, key, value, nx=False, **_k):
        if self.broken or (nx and key in self.values):
            return None
        self.values[key] = value
        return True

    async def delete(self, key):
        self.values.pop(key, None)


def _integration(token: str, **fields):
    return SimpleNamespace(
        id=uuid.uuid4(),
        provider="slack",
        status="connected",
        access_token_enc=encrypt_value(token),
        **fields,
    )


def _agent(owner=None, **fields):
    fields.setdefault("integration_bindings", {})
    fields.setdefault("integration_template_bindings", [])
    return SimpleNamespace(id=uuid.uuid4(), enabled=True, user_id=owner or uuid.uuid4(), **fields)


def _assignment(agent, **config):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="Slack triage",
        agent_id=agent.id,
        run_as_user_id=None,
        user_id=agent.user_id,
        trigger_config={"provider": "slack", "event": "new_slack_message", **config},
    )


def _envelope(team: str = TEAM_A, *, user: str = "U-author", channel_type: str = "channel", authorizations=None):
    event = {"type": "message", "text": "hello", "user": user, "channel": CHANNEL, "ts": TS, "channel_type": channel_type}
    envelope = {"type": "event_callback", "team_id": team, "event": event}
    envelope["authorizations"] = (
        authorizations
        if authorizations is not None
        else [{"team_id": team, "user_id": "U-bot", "is_bot": True, "is_enterprise_install": False}]
    )
    return envelope, event


def _queued(status: str, run_id: str | None = None):
    from app.services.assignment_engine import QueuedTriggerResult

    return QueuedTriggerResult(status=status, run_id=run_id, queue_depth=1)


@pytest.fixture
def world(monkeypatch):
    """Wire the fakes; ``identities`` maps a token to who Slack says it is."""
    state = SimpleNamespace(
        tables={"integrations": [], "assignments": [], "agents": []},
        identities={},
        eligible=None,
        triggered=[],
        trigger_result="run",
        trigger_error=None,
        queue_result=_queued("queued", "run-queued"),
        queued=[],
        redis=_Redis(),
        eligible_calls=[],
    )
    session = _Session(state.tables)
    state.session = session
    monkeypatch.setattr(slack_events, "async_session", lambda: session)

    async def _redis():
        return state.redis

    monkeypatch.setattr(slack_events, "redis_client", _redis)

    async def _fetch(token, _http):
        return state.identities.get(token)

    monkeypatch.setattr(slack_events, "fetch_slack_identity", _fetch)

    async def _eligible(_db, acting, ids, *, template, company_automation=False):
        state.eligible_calls.append((acting, tuple(ids), template))
        ids = list(ids)
        if state.eligible is None:
            return set(ids)
        return {i for i in ids if i in state.eligible.get((acting, template), set())}

    monkeypatch.setattr(slack_events, "eligible_integration_ids", _eligible)

    async def _trigger(**kwargs):
        if state.trigger_error is not None:
            raise state.trigger_error
        state.triggered.append(kwargs)
        return state.trigger_result

    monkeypatch.setattr(slack_events, "trigger_assignment", _trigger)

    async def _queue(*_args, **kwargs):
        state.queued.append(kwargs)
        return state.queue_result

    # A busy assignment's message as the event queue answers it when on;
    # a test may set another result, or put the real helper back.
    monkeypatch.setattr(slack_events, "queue_busy_integration_event", _queue, raising=False)
    return state


async def _dispatch(world, envelope_event, integrations):
    envelope, event = envelope_event
    ids = frozenset(str(i.id) for i in integrations)
    return await slack_events.dispatch_slack_message(envelope, event, ids)


def _bind(agent, *integrations):
    agent.integration_bindings = {"slack": [str(i.id) for i in integrations]}


# ---------------------------------------------------------------------------
# Workspace and user scoping
# ---------------------------------------------------------------------------


async def test_an_event_from_workspace_a_never_reaches_an_assignment_bound_to_workspace_b(world):
    a, b = _integration("tok-a"), _integration("tok-b")
    world.identities = {"tok-a": SlackIdentity(TEAM_A, "U-a"), "tok-b": SlackIdentity(TEAM_B, "U-b")}
    agent_a, agent_b = _agent(), _agent()
    _bind(agent_a, a)
    _bind(agent_b, b)
    wf_a, wf_b = _assignment(agent_a), _assignment(agent_b)
    world.tables.update(integrations=[a, b], assignments=[wf_a, wf_b], agents=[agent_a, agent_b])

    # Both integrations run on the admin app, so the signature proves both of them.
    assert await _dispatch(world, _envelope(TEAM_A), [a, b]) == 1
    (call,) = world.triggered
    assert call["assignment"] is wf_a
    assert call["trigger_context"]["integration_id"] == str(a.id)
    assert world.session.commits == 1


async def test_an_assignment_whose_agent_is_bound_to_no_receiving_integration_is_not_reached(world):
    """Another user's assignment, bound to an integration that did not receive the event, never runs."""
    a, b = _integration("tok-a"), _integration("tok-b")
    world.identities = {"tok-a": SlackIdentity(TEAM_A, "U-a"), "tok-b": SlackIdentity(TEAM_B, "U-b")}
    mine, theirs = _agent(), _agent()
    _bind(mine, a)
    _bind(theirs, b)
    world.tables.update(integrations=[a, b], assignments=[_assignment(theirs), _assignment(mine)], agents=[mine, theirs])
    assert await _dispatch(world, _envelope(), [a]) == 1
    assert world.triggered[0]["agent"] is mine


async def test_an_assignment_connected_to_no_slack_integration_runs_as_before(world):
    """No app, workspace or user to scope it by: it receives an accepted delivery as on main."""
    a = _integration("tok-a")
    world.identities = {"tok-a": SlackIdentity(TEAM_A, "U-a")}
    unbound = _agent()
    wf = _assignment(unbound)
    world.tables.update(integrations=[a], assignments=[wf], agents=[unbound])
    assert await _dispatch(world, _envelope(), [a]) == 1
    assert world.triggered[0]["assignment"] is wf
    assert world.triggered[0]["trigger_context"]["integration_id"] is None


# ---------------------------------------------------------------------------
# An unverified delivery accepted by default fans out as on main
# ---------------------------------------------------------------------------


async def _compat(world, envelope_event, *, protected=()):
    envelope, event = envelope_event
    return await slack_events.dispatch_slack_message(
        envelope, event, frozenset(), compatible=True, protected_ids=frozenset(str(i.id) for i in protected)
    )


async def test_an_unverified_delivery_reaches_every_unprotected_assignment_without_asking_slack(world):
    """Baseline fan-out: another workspace, an unidentifiable token, a DM - all as on main."""
    a, b = _integration("tok-a"), _integration("tok-b")
    world.identities = {}  # auth.test would fail for both: it is never asked
    agent_a, agent_b, unbound = _agent(), _agent(), _agent()
    _bind(agent_a, a)
    _bind(agent_b, b)
    wfs = [_assignment(agent_a), _assignment(agent_b), _assignment(unbound)]
    world.tables.update(integrations=[a, b], assignments=wfs, agents=[agent_a, agent_b, unbound])
    assert await _compat(world, _envelope(TEAM_B, channel_type="im")) == 3
    assert [c["assignment"] for c in world.triggered] == wfs


async def test_an_unverified_delivery_never_reaches_an_assignment_protected_by_a_stored_secret(world):
    protected, open_ = _integration("tok-p"), _integration("tok-o")
    agent_p, agent_o, agent_mixed = _agent(), _agent(), _agent()
    _bind(agent_p, protected)
    _bind(agent_o, open_)
    _bind(agent_mixed, protected, open_)
    wf_p, wf_o, wf_mixed = _assignment(agent_p), _assignment(agent_o), _assignment(agent_mixed)
    pinned = _assignment(agent_o, integration_id=str(protected.id))
    world.tables.update(
        integrations=[protected, open_],
        assignments=[wf_p, wf_o, wf_mixed, pinned],
        agents=[agent_p, agent_o, agent_mixed],
    )
    assert await _compat(world, _envelope(), protected=[protected]) == 2
    assert [c["assignment"] for c in world.triggered] == [wf_o, wf_mixed]
    assert world.triggered[1]["trigger_context"]["integration_id"] == str(open_.id)


async def test_an_unverified_delivery_to_a_template_binding_follows_the_run_users_integrations(world):
    protected, open_ = _integration("tok-p"), _integration("tok-o")
    agent = _agent(integration_template_bindings=["slack"])
    wf = _assignment(agent)
    world.tables.update(integrations=[protected, open_], assignments=[wf], agents=[agent])
    world.eligible = {(agent.user_id, True): {str(protected.id)}}
    assert await _compat(world, _envelope(), protected=[protected]) == 0
    world.eligible = {(agent.user_id, True): {str(open_.id)}}
    assert await _compat(world, _envelope(), protected=[protected]) == 1


async def test_an_unverified_delivery_keeps_the_channel_filter_and_runs_once(world):
    agent = _agent()
    inside = _assignment(agent, filter={"channels": [CHANNEL]})
    outside = _assignment(agent, filter={"channels": ["C-other"]})
    world.tables.update(assignments=[inside, outside], agents=[agent])
    assert await _compat(world, _envelope()) == 1
    assert await _compat(world, _envelope()) == 0, "a Slack retry of the same message"
    assert [c["assignment"] for c in world.triggered] == [inside]


async def test_an_integration_the_signature_did_not_prove_is_never_reached(world):
    a = _integration("tok-a")
    world.identities = {"tok-a": SlackIdentity(TEAM_A, "U-a")}
    agent = _agent()
    _bind(agent, a)
    world.tables.update(integrations=[a], assignments=[_assignment(agent)], agents=[agent])
    assert await slack_events.dispatch_slack_message(*_envelope(), frozenset()) == 0
    assert world.triggered == []


@pytest.mark.parametrize("channel_type", ["group", "im", "mpim", None])
async def test_a_message_outside_a_public_channel_reaches_only_its_author_or_the_authorized_user(world, channel_type):
    author, other, authorized = _integration("tok-author"), _integration("tok-other"), _integration("tok-auth")
    world.identities = {
        "tok-author": SlackIdentity(TEAM_A, "U-author"),
        "tok-other": SlackIdentity(TEAM_A, "U-other"),
        "tok-auth": SlackIdentity(TEAM_A, "U-auth"),
    }
    agents = [_agent(), _agent(), _agent()]
    for agent, integ in zip(agents, (author, other, authorized), strict=True):
        _bind(agent, integ)
    world.tables.update(
        integrations=[author, other, authorized],
        assignments=[_assignment(a) for a in agents],
        agents=agents,
    )
    envelope = _envelope(
        channel_type=channel_type,
        authorizations=[{"team_id": TEAM_A, "user_id": "U-auth", "is_bot": False}],
    )
    assert await _dispatch(world, envelope, [author, other, authorized]) == 2
    assert {c["agent"].id for c in world.triggered} == {agents[0].id, agents[2].id}


def test_the_delivery_scope_reads_the_authorizations_first():
    scope = delivery_scope(
        {
            "team_id": "T-envelope",
            "authorizations": [
                {"team_id": TEAM_A, "user_id": "U-1", "is_bot": False},
                {"team_id": None, "enterprise_id": "E-1", "is_enterprise_install": True, "user_id": "B-1", "is_bot": True},
                "not-an-object",
            ],
        }
    )
    assert scope.teams == frozenset({TEAM_A})
    assert scope.enterprises == frozenset({"E-1"})
    assert scope.users == frozenset({"U-1"})
    assert delivery_scope({"team_id": TEAM_B}).teams == frozenset({TEAM_B})


def test_an_org_wide_install_reaches_the_workspaces_of_its_org_only_for_public_channels():
    scope = delivery_scope({"authorizations": [{"enterprise_id": "E-1", "is_enterprise_install": True, "is_bot": True}]})
    in_org = SlackIdentity("T-x", "U-x", "E-1")
    assert integration_may_see(in_org, scope, {"channel_type": "channel"})
    assert not integration_may_see(in_org, scope, {"channel_type": "im", "user": "U-y"})
    assert not integration_may_see(SlackIdentity("T-x", "U-x", "E-2"), scope, {"channel_type": "channel"})


async def test_an_integration_slack_cannot_identify_receives_nothing(world):
    a = _integration("tok-revoked")
    agent = _agent()
    _bind(agent, a)
    world.tables.update(integrations=[a], assignments=[_assignment(agent)], agents=[agent])
    assert await _dispatch(world, _envelope(), [a]) == 0


async def test_the_identity_is_resolved_once_per_token(world, monkeypatch):
    calls: list[str] = []

    async def _fetch(token, _http):
        calls.append(token)
        return SlackIdentity(TEAM_A, "U-a")

    monkeypatch.setattr(slack_events, "fetch_slack_identity", _fetch)
    a = _integration("tok-a")
    agent = _agent()
    _bind(agent, a)
    world.tables.update(integrations=[a], assignments=[_assignment(agent)], agents=[agent])
    await _dispatch(world, _envelope(), [a])
    world.redis.sets.clear()
    await _dispatch(world, _envelope(), [a])
    assert calls == ["tok-a"]


# ---------------------------------------------------------------------------
# Which assignments are connected to an integration
# ---------------------------------------------------------------------------


async def test_a_pinned_integration_wins_over_the_agent_bindings(world):
    a, other = _integration("tok-a"), _integration("tok-other")
    world.identities = {"tok-a": SlackIdentity(TEAM_A, "U-a"), "tok-other": SlackIdentity(TEAM_A, "U-o")}
    agent = _agent()
    _bind(agent, a, other)
    world.tables.update(
        integrations=[a], assignments=[_assignment(agent, integration_id=str(other.id))], agents=[agent]
    )
    assert await _dispatch(world, _envelope(), [a]) == 0


async def test_a_binding_spelled_another_way_still_reaches_its_integration(world):
    """A valid UUID in another spelling is the same integration, as it is for the poller's SQL."""
    a = _integration("tok-a")
    world.identities = {"tok-a": SlackIdentity(TEAM_A, "U-a")}
    bound, pinned = _agent(), _agent()
    bound.integration_bindings = {"slack": [str(a.id).upper()]}
    world.tables.update(
        integrations=[a],
        assignments=[_assignment(bound), _assignment(pinned, integration_id=a.id.hex)],
        agents=[bound, pinned],
    )
    assert await _dispatch(world, _envelope(), [a]) == 2
    assert {c["trigger_context"]["integration_id"] for c in world.triggered} == {str(a.id)}


async def test_a_bound_integration_the_acting_user_may_not_use_is_not_reached(world):
    """The three-tier + OAuth-share rule, applied for the agent owner (never an admin bypass)."""
    a = _integration("tok-a")
    world.identities = {"tok-a": SlackIdentity(TEAM_A, "U-a")}
    agent = _agent()
    _bind(agent, a)
    world.tables.update(integrations=[a], assignments=[_assignment(agent)], agents=[agent])
    world.eligible = {}
    assert await _dispatch(world, _envelope(), [a]) == 0
    assert world.eligible_calls == [(agent.user_id, (str(a.id),), False)]
    world.eligible = {(agent.user_id, False): {str(a.id)}}
    assert await _dispatch(world, _envelope(), [a]) == 1


async def test_a_template_binding_reaches_the_run_users_eligible_integration(world):
    a = _integration("tok-a")
    world.identities = {"tok-a": SlackIdentity(TEAM_A, "U-a")}
    run_user = uuid.uuid4()
    agent = _agent(integration_template_bindings=["slack"])
    wf = _assignment(agent)
    wf.run_as_user_id = run_user
    world.tables.update(integrations=[a], assignments=[wf], agents=[agent])
    world.eligible = {(run_user, True): {str(a.id)}}
    assert await _dispatch(world, _envelope(), [a]) == 1
    world.redis.sets.clear()
    world.eligible = {}
    assert await _dispatch(world, _envelope(), [a]) == 0


@pytest.mark.parametrize(
    ("config", "reached"),
    [
        pytest.param({}, True, id="no-filter"),
        pytest.param({"filter": {"channels": [CHANNEL]}}, True, id="channels"),
        pytest.param({"filter": {"channels": ["C-other"]}}, False, id="channels-other"),
        pytest.param({"filter": {"scope": {"slack_channel": ["C-other"]}, "channels": [CHANNEL]}}, False, id="scope-wins"),
        pytest.param({"filter": {"scope": {"slack_channel": [CHANNEL]}}}, True, id="scope"),
    ],
)
async def test_the_channel_filter_is_read_as_the_poller_reads_it(world, config, reached):
    a = _integration("tok-a")
    world.identities = {"tok-a": SlackIdentity(TEAM_A, "U-a")}
    agent = _agent()
    _bind(agent, a)
    world.tables.update(integrations=[a], assignments=[_assignment(agent, **config)], agents=[agent])
    assert await _dispatch(world, _envelope(), [a]) == (1 if reached else 0)


async def test_a_disabled_agent_or_another_trigger_is_not_reached(world):
    a = _integration("tok-a")
    world.identities = {"tok-a": SlackIdentity(TEAM_A, "U-a")}
    disabled, other = _agent(), _agent()
    disabled.enabled = False
    _bind(disabled, a)
    _bind(other, a)
    email = _assignment(other)
    email.trigger_config = {"provider": "microsoft", "event": "new_email"}
    world.tables.update(integrations=[a], assignments=[_assignment(disabled), email], agents=[disabled, other])
    assert await _dispatch(world, _envelope(), [a]) == 0


# ---------------------------------------------------------------------------
# Exactly once: real time, retries, shared channels, the poller
# ---------------------------------------------------------------------------


def _one_assignment(world, *integrations):
    world.identities = {"tok-a": SlackIdentity(TEAM_A, "U-a"), "tok-b": SlackIdentity(TEAM_B, "U-b")}
    agent = _agent()
    _bind(agent, *integrations)
    wf = _assignment(agent)
    world.tables.update(integrations=list(integrations), assignments=[wf], agents=[agent])
    return agent, wf


async def test_a_slack_retry_runs_the_assignment_once(world):
    a = _integration("tok-a")
    _, wf = _one_assignment(world, a)
    assert await _dispatch(world, _envelope(), [a]) == 1
    assert await _dispatch(world, _envelope(), [a]) == 0
    assert len(world.triggered) == 1
    assert world.redis.members(seen_set_key(wf.id)) == {slack_message_key(CHANNEL, TS)}


async def test_a_shared_channel_message_seen_through_two_integrations_runs_once(world):
    a, b = _integration("tok-a"), _integration("tok-b")
    _one_assignment(world, a, b)
    envelope = _envelope(authorizations=[{"team_id": TEAM_A, "is_bot": True}, {"team_id": TEAM_B, "is_bot": True}])
    assert await _dispatch(world, envelope, [a, b]) == 1
    assert await _dispatch(world, envelope, [a, b]) == 0


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param("ai-off", id="ai-unavailable"),
        pytest.param("error", id="error"),
    ],
)
async def test_a_message_no_run_was_created_for_is_given_back_to_the_poller(world, outcome):
    a = _integration("tok-a")
    _, wf = _one_assignment(world, a)
    if outcome == "ai-off":
        world.trigger_error = HTTPException(status_code=403, detail={"code": AI_DISABLED_CODE, "message": "off"})
    else:
        world.trigger_error = RuntimeError("database blip")
    if outcome == "error":
        with pytest.raises(RuntimeError):
            await _dispatch(world, _envelope(), [a])
    else:
        assert await _dispatch(world, _envelope(), [a]) == 0
    assert world.redis.members(seen_set_key(wf.id)) == set()


async def test_a_redis_outage_never_loses_the_message(world):
    a = _integration("tok-a")
    _one_assignment(world, a)
    world.redis = _Redis(broken=True)
    assert await _dispatch(world, _envelope(), [a]) == 1


async def test_a_claim_whose_ttl_could_not_be_refreshed_is_still_given_back(world):
    """The member is in the set once SADD succeeded: a failed EXPIRE must not strand it there."""

    class _NoExpire(_Redis):
        async def expire(self, *_a, **_k):
            raise ConnectionError("redis blip")

    a = _integration("tok-a")
    _, wf = _one_assignment(world, a)
    world.redis = _NoExpire()
    world.trigger_error = HTTPException(status_code=403, detail={"code": AI_DISABLED_CODE, "message": "off"})
    assert await _dispatch(world, _envelope(), [a]) == 0
    assert world.redis.members(seen_set_key(wf.id)) == set()
    world.trigger_error = None
    world.trigger_result = "run"
    assert await _dispatch(world, _envelope(), [a]) == 1
    assert world.redis.members(seen_set_key(wf.id)) == {slack_message_key(CHANNEL, TS)}


async def test_the_poller_skips_a_message_already_delivered_in_real_time(world, monkeypatch):
    """The real poll loop, the same Redis, the same message."""
    from app.services import agentic_gate, event_poller

    a = _integration("tok-a", auth_type="oauth", user_id=None, oauth_share_acknowledged=False)
    agent, wf = _one_assignment(world, a)
    assert await _dispatch(world, _envelope(), [a]) == 1

    async def _redis():
        return world.redis

    async def _no_refusal(_db):
        return None

    async def _poll(_provider, _event, _token, _wf, _redis_, _seen, _http, **_kw):
        return [{"message_id": slack_message_key(CHANNEL, TS), "text": "hello"}]

    polled: list[dict] = []

    async def _trigger(*_args, **kwargs):
        polled.append(kwargs)
        return _queued("ok", "run")

    monkeypatch.setattr(event_poller, "redis_client", _redis)
    monkeypatch.setattr(agentic_gate, "agentic_refusal", _no_refusal)
    monkeypatch.setattr(event_poller, "_poll_provider", _poll)
    monkeypatch.setattr(event_poller, "trigger_or_queue_assignment", _trigger)
    monkeypatch.setattr(event_poller, "full_queue_assignment_ids", AsyncMock(return_value=set()))
    session = _Session({"assignments": [wf], "agents": [agent], "integrations": [a]})
    assert await event_poller.poll_integration_events(session) == 0
    assert polled == []


async def test_no_pooled_connection_is_held_while_slack_says_who_a_token_is(world, monkeypatch):
    a = _integration("tok-a")
    _one_assignment(world, a)
    session = world.session
    open_transaction = {"value": False}
    open_during_lookup: list[bool] = []
    execute = session.execute

    async def _execute(statement, *args, **kwargs):
        open_transaction["value"] = True
        return await execute(statement, *args, **kwargs)

    async def _commit():
        open_transaction["value"] = False
        session.commits += 1

    async def _fetch(_token, _http):
        open_during_lookup.append(open_transaction["value"])
        return SlackIdentity(TEAM_A, "U-a")

    monkeypatch.setattr(session, "execute", _execute)
    monkeypatch.setattr(session, "commit", _commit)
    monkeypatch.setattr(session, "in_transaction", lambda: open_transaction["value"])
    monkeypatch.setattr(slack_events, "fetch_slack_identity", _fetch)
    assert await _dispatch(world, _envelope(), [a]) == 1
    assert open_during_lookup == [False]


def test_the_poller_and_the_events_endpoint_name_a_message_the_same_way():
    from app.services import event_poller

    assert event_poller.event_seen_set_key is seen_set_key
    assert event_poller.slack_message_key is slack_message_key
    assert slack_message_key(CHANNEL, TS) == f"{CHANNEL}:{TS}"
    assert seen_set_key("abc") == "event_seen:abc"


# ---------------------------------------------------------------------------
# Own messages: the poller's rule on both paths
# ---------------------------------------------------------------------------


def test_the_events_endpoint_and_the_poller_skip_messages_by_one_rule():
    from app.api.v1 import integration_notifications
    from app.services import event_poller

    assert integration_notifications.slack_message_skipped is slack_events.slack_message_skipped
    assert event_poller.slack_message_skipped is slack_events.slack_message_skipped


@pytest.mark.parametrize(
    ("message", "skipped"),
    [
        pytest.param({"type": "message", "user": "U-a", "text": "mine"}, False, id="own-message"),
        pytest.param({"type": "message", "user": "U-b", "text": "theirs"}, False, id="someone-else"),
        pytest.param({"type": "message", "subtype": "bot_message", "bot_id": "B1"}, True, id="bot"),
        pytest.param({"type": "message", "subtype": "message_changed"}, True, id="edit"),
    ],
)
def test_a_message_its_integration_user_wrote_is_treated_as_the_poller_treats_it(message, skipped):
    """The poller does not skip the integration user's own messages, so real time does not either."""
    assert slack_events.slack_message_skipped(message) is skipped


async def test_an_own_message_reaches_its_assignment_in_real_time_as_the_poller_would(world):
    a = _integration("tok-a")
    _one_assignment(world, a)
    envelope, event = _envelope(user="U-a")
    assert await slack_events.dispatch_slack_message(envelope, event, frozenset({str(a.id)})) == 1


# ---------------------------------------------------------------------------
# The seen set is bounded by the claim window
# ---------------------------------------------------------------------------


async def test_a_claim_older_than_the_window_is_trimmed_and_a_recent_one_holds():
    redis = _Redis()
    wf = uuid.uuid4()
    start = 1_000_000.0
    assert await slack_events.claim_seen(redis, wf, "C1:1", now=start) is True
    assert await slack_events.claim_seen(redis, wf, "C1:1", now=start + 60) is False, "a Slack retry"
    assert await slack_events.claim_seen(redis, wf, "C1:1", now=start + 3600) is False, "the next poll"
    later = start + slack_events.SEEN_SET_TTL_SECONDS + 1
    assert await slack_events.claim_seen(redis, wf, "C1:2", now=later) is True
    assert redis.members(seen_set_key(wf)) == {"C1:2"}, "the set holds only the window"


async def test_a_claim_in_the_legacy_set_still_counts_until_it_expires():
    """Messages claimed before the upgrade are not run a second time by it."""
    redis = _Redis()
    wf = uuid.uuid4()
    redis.plain[slack_events.legacy_seen_set_key(wf)] = {"C1:1"}
    assert await slack_events.claim_seen(redis, wf, "C1:1") is False
    assert await slack_events.claim_seen(redis, wf, "C1:2") is True


async def test_the_poller_claims_in_the_bounded_set(world, monkeypatch):
    from app.services import agentic_gate, event_poller

    a = _integration("tok-a", auth_type="oauth", user_id=None, oauth_share_acknowledged=False)
    agent, wf = _one_assignment(world, a)

    async def _redis():
        return world.redis

    async def _no_refusal(_db):
        return None

    async def _poll(*_a, **_kw):
        return [{"message_id": slack_message_key(CHANNEL, TS)}]

    async def _run(*_args, **_kwargs):
        return _queued("ok", "run")

    monkeypatch.setattr(event_poller, "redis_client", _redis)
    monkeypatch.setattr(agentic_gate, "agentic_refusal", _no_refusal)
    monkeypatch.setattr(event_poller, "_poll_provider", _poll)
    monkeypatch.setattr(event_poller, "trigger_or_queue_assignment", _run)
    monkeypatch.setattr(event_poller, "full_queue_assignment_ids", AsyncMock(return_value=set()))
    session = _Session({"assignments": [wf], "agents": [agent], "integrations": [a]})
    await event_poller.poll_integration_events(session)
    assert world.redis.members(seen_set_key(wf.id)) == {slack_message_key(CHANNEL, TS)}
    assert slack_events.legacy_seen_set_key(wf.id) not in world.redis.plain


# ---------------------------------------------------------------------------
# A message an agent posted never runs an assignment in real time
# ---------------------------------------------------------------------------


async def test_an_assignments_own_post_does_not_run_it_but_the_persons_own_message_does(world):
    from app.services.agent_posts import agent_post_key

    a = _integration("tok-a")
    _agent, wf = _one_assignment(world, a)
    world.redis.values[agent_post_key("slack", CHANNEL, TS)] = str(wf.id)
    assert await _dispatch(world, _envelope(user="U-a"), [a]) == 0
    assert world.triggered == []
    envelope, event = _envelope(user="U-a")
    event["ts"] = "1700000000.000999"
    assert await slack_events.dispatch_slack_message(envelope, event, frozenset({str(a.id)})) == 1


async def test_a_post_another_assignment_made_still_runs_this_one(world):
    """A digest posted where another assignment listens triggers it, as on main."""
    from app.services.agent_posts import agent_post_key

    a = _integration("tok-a")
    _one_assignment(world, a)
    world.redis.values[agent_post_key("slack", CHANNEL, TS)] = str(uuid.uuid4())
    assert await _dispatch(world, _envelope(user="U-a"), [a]) == 1


# ---------------------------------------------------------------------------
# A busy assignment's events: queued with INTEGRATION_EVENT_QUEUE_ENABLED on;
# off by default, a pushed one is skipped as before and a
# polled one is fetched again once the assignment is idle
# ---------------------------------------------------------------------------


async def test_a_busy_assignment_queues_the_message_and_keeps_the_claim(world):
    a = _integration("tok-a")
    _, wf = _one_assignment(world, a)
    world.trigger_result = None
    assert await _dispatch(world, _envelope(), [a]) == 1
    (queued,) = world.queued
    assert queued["trigger_context"]["message_id"] == TS
    assert queued["on_full"] == "persist", "a pushed event has no poller to fetch it again"
    assert world.redis.members(seen_set_key(wf.id)) == {slack_message_key(CHANNEL, TS)}, "the poller must not run it again"


async def test_an_idle_assignment_never_touches_the_queue(world):
    a = _integration("tok-a")
    _one_assignment(world, a)
    assert await _dispatch(world, _envelope(), [a]) == 1
    assert world.queued == []


def _poller_world(world, monkeypatch, *, queue_result, messages, runs=(), full=False):
    """The poller over one assignment whose provider returns *messages* on every poll.

    ``trigger_or_queue_assignment`` starts a run for the ids in ``world.poll_runs``,
    one call at a time, then answers *queue_result* (a free slot, then the queue);
    ``world.poll_dispatched`` records every message it accepted, in order,
    and ``queue`` the calls answered from *queue_result*. *full* puts the
    assignment's delivery queue at its cap before the poll.
    """
    from app.services import agentic_gate, event_poller

    a = _integration("tok-a", auth_type="oauth", user_id=None, oauth_share_acknowledged=False)
    agent, wf = _one_assignment(world, a)
    world.polls = 0
    world.poll_runs = list(runs)
    world.poll_dispatched = []

    async def _redis():
        return world.redis

    async def _no_refusal(_db):
        return None

    async def _poll(_provider, _event, _token, wf_, redis_, *_a, **_kw):
        world.polls += 1
        await redis_.set(f"event_cursor:{wf_.id}", "after-this-poll")
        return [{"message_id": m} for m in messages]

    queue = AsyncMock(return_value=queue_result)

    async def _trigger_or_queue(*args, **kwargs):
        if world.poll_runs:
            result = _queued("ok", world.poll_runs.pop(0))
        else:
            result = await queue(*args, **kwargs)
        if result.status != "queue_full":
            world.poll_dispatched.append(kwargs["trigger_context"]["message_id"])
        return result

    world.full_queue_reads = 0

    async def _full_queue(_db, ids):
        world.full_queue_reads += 1
        return {wf.id} if full else set()

    monkeypatch.setattr(event_poller, "redis_client", _redis)
    monkeypatch.setattr(agentic_gate, "agentic_refusal", _no_refusal)
    monkeypatch.setattr(event_poller, "_poll_provider", _poll)
    monkeypatch.setattr(event_poller, "trigger_or_queue_assignment", _trigger_or_queue)
    monkeypatch.setattr(event_poller, "full_queue_assignment_ids", _full_queue)
    tables = {"assignments": [wf], "agents": [agent], "integrations": [a]}
    return event_poller, _Session(tables), wf, queue


@pytest.mark.parametrize("event_queue", [False, True])
async def test_the_poller_queues_the_events_it_cannot_start_whatever_the_event_queue_setting(
    world, monkeypatch, event_queue
):
    """A polled event starts in a free slot or waits in the queue: with a
    cursor to fall back on, it is never turned away by the overlap guard, so
    INTEGRATION_EVENT_QUEUE_ENABLED - which governs pushed events - does not apply."""
    from app.services import assignment_engine as ae

    monkeypatch.setattr(ae.settings, "integration_event_queue_enabled", event_queue)
    poller, session, wf, queue = _poller_world(
        world, monkeypatch, queue_result=_queued("queued", "run-q"), messages=["C1:1", "C1:2"]
    )
    world.redis.values[f"event_cursor:{wf.id}"] = "before-this-poll"
    assert await poller.poll_integration_events(session) == 2
    assert [c.kwargs["overflow"] for c in queue.await_args_list] == ["reject", "reject"]
    assert all("queue" not in c.kwargs for c in queue.await_args_list), "a polled event is always queued"
    assert world.redis.members(seen_set_key(wf.id)) == {"C1:1", "C1:2"}
    assert world.redis.values[f"event_cursor:{wf.id}"] == "after-this-poll"
    assert f"event_poll:{wf.id}" in world.redis.values


async def test_a_full_queue_gives_the_polled_event_and_the_cursor_back(world, monkeypatch):
    poller, session, wf, queue = _poller_world(
        world, monkeypatch, queue_result=_queued("queue_full"), messages=["C1:1", "C1:2"]
    )
    world.redis.values[f"event_cursor:{wf.id}"] = "before-this-poll"
    assert await poller.poll_integration_events(session) == 0
    assert queue.await_count == 1, "the batch stops at the first event the queue cannot take"
    assert world.redis.members(seen_set_key(wf.id)) == set()
    assert world.redis.values[f"event_cursor:{wf.id}"] == "before-this-poll"
    assert f"event_poll:{wf.id}" not in world.redis.values, "polled again on the next tick"


def _engine_queue_calls(monkeypatch, *, event_queue: bool) -> list[str]:
    """The real busy-event helpers over an assignment whose every slot is taken,
    with only the engine's queue write recorded."""
    from app.services import assignment_engine as ae

    calls: list[str] = []

    async def _queue(*_args, overflow=None, queue=True, **_kwargs):
        if not queue:
            # Every slot taken and nothing may be queued: the skip stands.
            return ae.QueuedTriggerResult(status="dropped")
        calls.append(overflow)
        return ae.QueuedTriggerResult(status="queued", run_id="run-queued", queue_depth=1)

    monkeypatch.setattr(ae, "trigger_or_queue_assignment", _queue)
    monkeypatch.setattr(ae.settings, "integration_event_queue_enabled", event_queue)
    return calls


@pytest.mark.parametrize("event_queue", [False, True])
async def test_the_events_endpoint_queues_a_busy_assignments_message_only_with_the_event_queue_on(
    world, monkeypatch, event_queue
):
    """Off (the default), nothing is queued and the claim goes back to the poller."""
    from app.services import assignment_engine as ae

    monkeypatch.setattr(slack_events, "queue_busy_integration_event", ae.queue_busy_integration_event)
    calls = _engine_queue_calls(monkeypatch, event_queue=event_queue)
    a = _integration("tok-a")
    _, wf = _one_assignment(world, a)
    world.trigger_result = None
    assert await _dispatch(world, _envelope(), [a]) == (1 if event_queue else 0)
    assert calls == (["persist"] if event_queue else [])
    claimed = {slack_message_key(CHANNEL, TS)} if event_queue else set()
    assert world.redis.members(seen_set_key(wf.id)) == claimed


async def test_every_email_of_a_burst_is_dispatched_in_the_tick_that_polls_it(world, monkeypatch):
    """Several new events in one poll: the first starts a run, the
    others wait in the queue for a slot - all in one tick, where the poller used
    to start one and give the rest back to a tick a minute later. None is lost
    and none runs twice."""
    poller, session, wf, queue = _poller_world(
        world,
        monkeypatch,
        queue_result=_queued("queued", "run-q"),
        messages=["C1:1", "C1:2", "C1:3"],
        runs=["run-1"],
    )
    world.redis.values[f"event_cursor:{wf.id}"] = "before-this-poll"

    assert await poller.poll_integration_events(session) == 3
    assert world.poll_dispatched == ["C1:1", "C1:2", "C1:3"]
    assert queue.await_count == 2
    assert world.redis.members(seen_set_key(wf.id)) == {"C1:1", "C1:2", "C1:3"}
    assert world.redis.values[f"event_cursor:{wf.id}"] == "after-this-poll"
    assert f"event_poll:{wf.id}" in world.redis.values, "the poll ran everything it fetched"

    # The next tick is inside the poll interval: nothing is fetched twice.
    assert await poller.poll_integration_events(session) == 0
    assert world.polls == 1


class _Mailbox:
    """Graph ``/me/messages`` over a fixed mailbox, honouring ``$filter``, ``$orderby`` and ``$top``."""

    def __init__(self, count: int):
        self.mails = [
            {
                "id": f"mail-{n:03d}",
                "subject": f"mail {n}",
                "from": {"emailAddress": {"address": "client@x.example", "name": "Client"}},
                "receivedDateTime": f"2026-09-29T{8 + n // 60:02d}:{n % 60:02d}:00Z",
            }
            for n in range(1, count + 1)
        ]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get(self, url, **_k):
        query = parse_qs(urlsplit(url).query)
        mails = self.mails
        if "$filter" in query:
            after = query["$filter"][0].rsplit(" ", 1)[1]
            mails = [m for m in mails if m["receivedDateTime"] > after]
        newest_first = query["$orderby"][0].endswith("desc")
        mails = sorted(mails, key=lambda m: m["receivedDateTime"], reverse=newest_first)[: int(query["$top"][0])]
        return SimpleNamespace(status_code=200, json=lambda: {"value": mails}, text="", headers={})


def _mailbox_world(world, monkeypatch, count: int):
    from app.services import event_poller

    provider = event_poller._poll_provider
    poller, session, wf, _queue = _poller_world(
        world, monkeypatch, queue_result=_queued("queued", "run-q"), messages=[]
    )
    monkeypatch.setattr(poller, "_poll_provider", provider)
    (integration,) = world.tables["integrations"]
    integration.provider = "microsoft"
    wf.trigger_config = {"provider": "microsoft", "event": "new_email", "integration_id": str(integration.id)}
    mailbox = _Mailbox(count)
    monkeypatch.setattr(poller.httpx, "AsyncClient", lambda *a, **k: mailbox)
    world.redis.values[f"event_cursor:{wf.id}"] = "2026-09-29T07:00:00Z"
    return poller, session, wf, mailbox


async def test_a_mailbox_backlog_larger_than_a_page_runs_every_email_once_in_order(world, monkeypatch, caplog):
    """A backlog of more emails than one page is read page after
    page, oldest first, in the same tick - never one page per poll interval - and
    every email is dispatched once, in the order it arrived."""
    from app.services import event_poller

    poller, session, wf, mailbox = _mailbox_world(world, monkeypatch, event_poller._MICROSOFT_MAIL_PAGE + 5)

    with caplog.at_level(logging.INFO, logger=poller.__name__):
        assert await poller.poll_integration_events(session) == len(mailbox.mails)

    assert world.poll_dispatched == [m["id"] for m in mailbox.mails]
    assert world.redis.values[f"event_cursor:{wf.id}"] == mailbox.mails[-1]["receivedDateTime"]
    assert f"event_poll:{wf.id}" in world.redis.values, "caught up: the poll is stamped"
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert sum("more new events than one page" in r.getMessage() for r in caplog.records) == 1


async def test_a_backlog_past_the_pages_of_one_tick_is_read_on_the_next_tick_not_a_poll_interval_later(
    world, monkeypatch
):
    """The 5-minute gaps: a poll that stopped on a page with more behind it
    was stamped, so the rest waited ``POLL_INTERVALS``. It is left unstamped now,
    and the next scheduler tick reads on from its cursor."""
    from app.services import event_poller

    page = event_poller._MICROSOFT_MAIL_PAGE
    pages = event_poller._MAX_PAGES_PER_POLL
    poller, session, wf, mailbox = _mailbox_world(world, monkeypatch, page * pages + 3)

    assert await poller.poll_integration_events(session) == page * pages
    assert f"event_poll:{wf.id}" not in world.redis.values, "more to read: polled again on the next tick"
    assert f"{event_poller.POLL_READ_KEY_PREFIX}{wf.id}" in world.redis.values, "the pages read are shown (#1718)"
    assert await poller.poll_integration_events(session) == 3
    assert f"event_poll:{wf.id}" in world.redis.values
    assert world.poll_dispatched == [m["id"] for m in mailbox.mails]


async def test_a_tick_past_its_time_budget_reads_no_further_page(world, monkeypatch):
    """A slow provider must not push a tick past the scheduler's interval:
    past the budget the poll stops after the page it read, unstamped, and the next
    tick reads on from its cursor - nothing is skipped or read twice."""
    from app.services import event_poller

    page = event_poller._MICROSOFT_MAIL_PAGE
    poller, session, wf, mailbox = _mailbox_world(world, monkeypatch, page * 2 + 3)
    monkeypatch.setattr(poller, "_POLL_TICK_BUDGET_SECONDS", 0.0)

    assert await poller.poll_integration_events(session) == page
    assert f"event_poll:{wf.id}" not in world.redis.values, "cut short: polled again on the next tick"
    assert await poller.poll_integration_events(session) == page
    assert await poller.poll_integration_events(session) == 3
    assert f"event_poll:{wf.id}" in world.redis.values
    assert world.poll_dispatched == [m["id"] for m in mailbox.mails]


async def test_an_assignment_whose_queue_is_full_is_left_unpolled_and_unstamped(world, monkeypatch):
    """Every event it read would be handed back: the provider keeps them for the
    first tick that finds room in the queue."""
    poller, session, wf, _queue = _poller_world(
        world, monkeypatch, queue_result=_queued("queued", "run-q"), messages=["C1:1"], full=True
    )
    assert await poller.poll_integration_events(session) == 0
    assert world.polls == 0
    assert f"event_poll:{wf.id}" not in world.redis.values


async def test_polled_events_without_an_id_run_or_queue_and_move_the_cursor(world, monkeypatch):
    """Without an id nothing tells the next poll which events of this one already
    ran, so the cursor moves past them; each still starts or waits in the queue."""
    poller, session, wf, _queue = _poller_world(
        world, monkeypatch, queue_result=_queued("queued", "run-q"), messages=["", ""], runs=["run-1"]
    )
    world.redis.values[f"event_cursor:{wf.id}"] = "before-this-poll"
    assert await poller.poll_integration_events(session) == 2
    assert world.poll_dispatched == [None, None]
    assert world.redis.values[f"event_cursor:{wf.id}"] == "after-this-poll"
    assert f"event_poll:{wf.id}" in world.redis.values


async def test_a_batch_in_which_an_event_without_an_id_ran_is_never_rewound(world, monkeypatch, caplog):
    """An event without an id ran first, then the queue
    cannot take an identified one. Rewinding the cursor would fetch - and run - the
    id-less one again, since nothing in the seen set says it ran: the batch keeps its
    cursor instead."""
    poller, session, wf, _queue = _poller_world(
        world,
        monkeypatch,
        queue_result=_queued("queue_full"),
        messages=["", "C1:2"],
        runs=["run-1"],
    )
    world.redis.values[f"event_cursor:{wf.id}"] = "before-this-poll"
    with caplog.at_level(logging.WARNING, logger=poller.__name__):
        assert await poller.poll_integration_events(session) == 1
    assert world.poll_dispatched == [None]
    assert world.redis.values[f"event_cursor:{wf.id}"] == "after-this-poll", "a rewind runs the id-less event twice"
    assert "event without an id" in caplog.text, "the events not run are said, not dropped silently"


async def test_the_providers_cursor_reaches_redis_only_once_the_batch_ran(world, monkeypatch):
    """The provider moves the cursor while it fetches. Written
    then, a worker killed before the batch ran would lose all of it."""
    poller, session, wf, _queue = _poller_world(
        world, monkeypatch, queue_result=None, messages=["C1:1", "C1:2"], runs=["r1", "r2"]
    )
    key = f"event_cursor:{wf.id}"
    world.redis.values[key] = "before-this-poll"
    stored: list[str | None] = []
    fetch, dispatch = poller._poll_provider, poller.trigger_or_queue_assignment

    async def _fetch_and_look(*args, **kwargs):
        events = await fetch(*args, **kwargs)
        stored.append(world.redis.values.get(key))
        return events

    async def _dispatch_and_look(*args, **kwargs):
        stored.append(world.redis.values.get(key))
        return await dispatch(*args, **kwargs)

    monkeypatch.setattr(poller, "_poll_provider", _fetch_and_look)
    monkeypatch.setattr(poller, "trigger_or_queue_assignment", _dispatch_and_look)
    assert await poller.poll_integration_events(session) == 2
    assert stored == ["before-this-poll"] * 3, "fetched, then each of the two runs"
    assert world.redis.values[key] == "after-this-poll"


async def test_a_worker_killed_after_an_id_less_event_ran_never_runs_it_again(world, monkeypatch):
    """Nothing in the seen set can skip an event without an id,
    so a batch holding one keeps its cursor from the start: a kill after it ran does not
    fetch it again."""
    poller, session, wf, _queue = _poller_world(
        world, monkeypatch, queue_result=None, messages=["", "C1:2"], runs=["r1", "r2"]
    )
    world.redis.values[f"event_cursor:{wf.id}"] = "before-this-poll"
    dispatch = poller.trigger_or_queue_assignment

    async def _killed_after_the_first_run(*args, **kwargs):
        await dispatch(*args, **kwargs)
        raise _WorkerKilled("killed")

    monkeypatch.setattr(poller, "trigger_or_queue_assignment", _killed_after_the_first_run)
    with pytest.raises(_WorkerKilled):
        await poller.poll_integration_events(session)
    assert world.redis.values[f"event_cursor:{wf.id}"] == "after-this-poll"


class _WorkerKilled(BaseException):
    """A worker killed mid-batch (a deploy): not an ``Exception``, nothing catches it."""


@pytest.mark.parametrize(
    "failure",
    [_WorkerKilled, RuntimeError],
    ids=["worker_killed", "unexpected_error"],
)
async def test_a_batch_cut_short_is_fetched_again_and_runs_each_event_once(world, monkeypatch, failure):
    """The provider moved the cursor past the whole batch before any of it ran. A
    worker killed (or an error raised) after the second event's run leaves the cursor where
    it was, so the next poll fetches the batch again: the third event runs, the first two
    are skipped by the seen set."""
    poller, session, wf, _queue = _poller_world(
        world, monkeypatch, queue_result=None, messages=["C1:1", "C1:2", "C1:3"], runs=["r1", "r2", "r3"]
    )
    world.redis.values[f"event_cursor:{wf.id}"] = "before-this-poll"
    dispatch = poller.trigger_or_queue_assignment

    async def _killed_after_the_second_run(*args, **kwargs):
        result = await dispatch(*args, **kwargs)
        if result.run_id == "r2" and not world.restarted:
            raise failure("killed")
        return result

    world.restarted = False
    monkeypatch.setattr(poller, "trigger_or_queue_assignment", _killed_after_the_second_run)
    if failure is _WorkerKilled:
        with pytest.raises(_WorkerKilled):
            await poller.poll_integration_events(session)
    else:
        await poller.poll_integration_events(session)
    assert world.redis.values[f"event_cursor:{wf.id}"] == "before-this-poll"
    assert f"event_poll:{wf.id}" not in world.redis.values

    world.restarted = True
    assert await poller.poll_integration_events(session) == 1
    assert world.poll_dispatched == ["C1:1", "C1:2", "C1:3"]
    assert world.redis.values[f"event_cursor:{wf.id}"] == "after-this-poll"


async def test_the_full_queue_check_is_one_read_per_tick_whatever_the_number_of_assignments(world, monkeypatch):
    """One batched read of the queue depths, not one per assignment."""
    poller, session, wf, _queue = _poller_world(
        world, monkeypatch, queue_result=_queued("queued", "run-q"), messages=["C1:1"], runs=["run-1"], full=True
    )
    others = [SimpleNamespace(**{**vars(wf), "id": uuid.uuid4()}) for _ in range(3)]
    session.tables["assignments"].extend(others)
    await poller.poll_integration_events(session)
    assert world.full_queue_reads == 1
    assert world.polls == 3, "the assignment with a full queue is not polled, the others are"


# The engine: with the integration event queue on, no drop policy drops
# a busy event; off (the default), the overlap guard's skip stands.


def _engine_queue(monkeypatch, *, depth: int, max_depth: int = 2, event_queue: bool = True):
    from app.services import assignment_engine as ae

    created: list[dict] = []
    contexts: list[dict | None] = []

    async def _nothing(*_a, **_k):
        return None

    async def _busy(*_a, **_k):
        # Every slot taken.
        return ae._SlotUsage(active=ae.assignment_dispatch.MAX_CONCURRENCY_CAP, busy_keys=frozenset())

    async def _depth(*_a, **_k):
        return depth

    async def _create(_db, _assignment, _agent, payload, context):
        created.append(payload)
        contexts.append(context)
        return "run-queued"

    monkeypatch.setattr(ae, "_refuse_if_ai_disabled", _nothing)
    monkeypatch.setattr(ae, "_acquire_trigger_overlap_lock", _nothing)
    monkeypatch.setattr(ae, "_slot_usage", _busy)
    monkeypatch.setattr(ae, "count_queued_runs", _depth)
    monkeypatch.setattr(ae, "_create_queued_run", _create)
    monkeypatch.setattr(ae, "_evict_oldest_queued_run", AsyncMock(side_effect=AssertionError("evicted")))
    monkeypatch.setattr(ae.settings, "assignment_queue_max_depth", max_depth)
    monkeypatch.setattr(ae.settings, "integration_event_queue_enabled", event_queue)
    db = SimpleNamespace(commit=AsyncMock())
    agent = SimpleNamespace(id=uuid.uuid4(), name="A", enabled=True)
    # Ownerless, so the owner-deactivation rule exempts it without a query.
    # Bound to its agent, as a real assignment is: the dedup key is scoped to it.
    wf = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent.id,
        name="WF",
        enabled=True,
        is_company_managed=False,
        user_id=None,
        trigger_config={},
    )
    db.contexts = contexts
    return ae, wf, agent, db, created


@pytest.mark.parametrize("policy", ["reject", "drop_newest", "drop_oldest"])
@pytest.mark.parametrize("queue_enabled", [True, False])
async def test_a_pushed_event_is_queued_past_a_full_queue_whatever_the_settings(monkeypatch, policy, queue_enabled, caplog):
    ae, wf, agent, db, created = _engine_queue(monkeypatch, depth=2)
    monkeypatch.setattr(ae.settings, "assignment_queue_overflow_policy", policy)
    monkeypatch.setattr(ae.settings, "assignment_queue_enabled", queue_enabled)
    with caplog.at_level("WARNING"):
        run_id = await ae.queue_busy_pushed_event(wf, agent, {"m": 1}, db=db, trigger_context={})
    assert run_id == "run-queued"
    assert created == [{"m": 1}]
    assert any("past the cap" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("policy", ["reject", "drop_newest", "drop_oldest"])
@pytest.mark.parametrize("queue_enabled", [True, False])
async def test_a_polled_event_meets_a_full_queue_with_a_hand_back_never_a_drop(monkeypatch, policy, queue_enabled):
    ae, wf, agent, db, created = _engine_queue(monkeypatch, depth=2)
    monkeypatch.setattr(ae.settings, "assignment_queue_overflow_policy", policy)
    monkeypatch.setattr(ae.settings, "assignment_queue_enabled", queue_enabled)
    result = await ae.queue_busy_integration_event(wf, agent, {"m": 1}, db=db, on_full="hand_back")
    assert (result.status, result.run_id, created) == ("queue_full", None, [])


async def test_a_busy_event_with_room_is_queued(monkeypatch):
    ae, wf, agent, db, created = _engine_queue(monkeypatch, depth=0)
    monkeypatch.setattr(ae.settings, "assignment_queue_enabled", False)
    result = await ae.queue_busy_integration_event(wf, agent, {"m": 1}, db=db, on_full="hand_back")
    assert (result.status, result.run_id, created) == ("queued", "run-queued", [{"m": 1}])


def test_a_busy_integration_event_is_skipped_by_default():
    from app.core.config import Settings

    assert Settings.model_fields["integration_event_queue_enabled"].default is False


@pytest.mark.parametrize("depth", [0, 2])
async def test_with_the_event_queue_off_a_busy_event_is_skipped_and_nothing_is_queued(monkeypatch, depth):
    ae, wf, agent, db, created = _engine_queue(monkeypatch, depth=depth, event_queue=False)
    polled = await ae.queue_busy_integration_event(wf, agent, {"m": 1}, db=db, on_full="hand_back")
    pushed = await ae.queue_busy_pushed_event(wf, agent, {"m": 2}, db=db, trigger_context={})
    assert (polled.status, polled.run_id, pushed, created) == ("dropped", None, None, [])
    # The slots were read under the advisory lock, released by the commit.
    assert db.commit.await_count == 2


async def test_with_the_event_queue_off_a_busy_event_still_starts_in_a_free_slot(monkeypatch):
    """The overlap guard turned it away, but the assignment has room for another
    run: it starts, whatever INTEGRATION_EVENT_QUEUE_ENABLED says."""
    ae, wf, agent, db, created = _engine_queue(monkeypatch, depth=0, event_queue=False)
    wf.trigger_config = {"max_concurrent_runs": 2}

    async def _one_in_flight(*_a, **_k):
        return ae._SlotUsage(active=1, busy_keys=frozenset())

    started: list[dict] = []

    async def _trigger(*, payload, **_kwargs):
        started.append(payload)
        return "run-started"

    monkeypatch.setattr(ae, "_slot_usage", _one_in_flight)
    monkeypatch.setattr(ae, "trigger_assignment", _trigger)
    run_id = await ae.queue_busy_pushed_event(wf, agent, {"m": 1}, db=db, trigger_context={})
    assert (run_id, started, created) == ("run-started", [{"m": 1}], [])


async def test_the_webhook_receiver_keeps_its_own_overflow_policy(monkeypatch):
    """The per-assignment webhook passes no override: its settings still apply."""
    ae, wf, agent, db, created = _engine_queue(monkeypatch, depth=2)
    monkeypatch.setattr(ae.settings, "assignment_queue_overflow_policy", "reject")
    result = await ae.trigger_or_queue_assignment(wf, agent, {"m": 1}, db=db)
    assert (result.status, created) == ("queue_full", [])


# Every pushed-event receiver queues a busy assignment's delivery.

_PUSHED_RECEIVERS = [
    ("app/api/v1/webhooks.py", "receive_provider_webhook"),
    ("app/api/v1/webhooks.py", "_handle_hubspot_webhook"),
    ("app/api/v1/webhooks.py", "receive_integration_webhook"),
    ("app/api/v1/webhooks.py", "_handle_hubspot_assignment_webhook"),
    ("app/api/v1/webhooks.py", "receive_assignment_webhook"),
    ("app/api/v1/webhooks.py", "receive_webhook"),
    ("app/api/v1/integration_notifications.py", "microsoft_graph_notification"),
    ("app/api/v1/integration_notifications.py", "linkedin_social_action_notification"),
]


@pytest.mark.parametrize(("module", "function"), _PUSHED_RECEIVERS, ids=[f for _m, f in _PUSHED_RECEIVERS])
def test_every_pushed_receiver_queues_what_the_overlap_guard_turns_away(module, function):
    """A ``trigger_assignment(skip_if_active=True)`` answering ``None`` is followed, in the
    same function, by ``queue_busy_pushed_event`` - never by a bare drop."""
    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / module).read_text(encoding="utf-8")
    tree = ast.parse(source)
    (fn,) = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == function]
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    guarded = [
        c for c in calls
        if c.func.id == "trigger_assignment"
        and any(k.arg == "skip_if_active" and isinstance(k.value, ast.Constant) and k.value.value is True for k in c.keywords)
    ]
    queued = [c for c in calls if c.func.id == "queue_busy_pushed_event"]
    assert guarded, "the receiver still triggers through the overlap guard"
    assert len(queued) == len(guarded), f"{function}: {len(guarded)} guarded trigger(s), {len(queued)} queue call(s)"
    for trigger in guarded:
        assert any(q.lineno > trigger.lineno for q in queued)



@pytest.mark.parametrize("queue_enabled", [True, False])
async def test_a_queued_integration_event_is_marked_so_the_drain_promotes_it(monkeypatch, queue_enabled):
    ae, wf, agent, db, _created = _engine_queue(monkeypatch, depth=0)
    monkeypatch.setattr(ae.settings, "assignment_queue_enabled", queue_enabled)
    await ae.queue_busy_pushed_event(wf, agent, {"m": 1}, db=db, trigger_context={"type": "integration_event"})
    (context,) = db.contexts
    assert context == {"type": "integration_event", ae.QUEUE_ORIGIN_KEY: ae.INTEGRATION_EVENT_QUEUE_ORIGIN}


async def test_a_queued_webhook_delivery_is_not_marked(monkeypatch):
    ae, wf, agent, db, _created = _engine_queue(monkeypatch, depth=0)
    await ae.trigger_or_queue_assignment(wf, agent, {"m": 1}, db=db, trigger_context={"type": "webhook"})
    assert db.contexts == [{"type": "webhook"}]
