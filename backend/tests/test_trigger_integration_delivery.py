"""Integration events and a run whose acting user cannot see the integration.

The event poller, the flow trigger poller and the Microsoft Graph notification
read an integration's events through its stored credentials, behind the
personal OAuth token gate, and hand them to a run. The run's acting
user is checked against the integration (three-tier visibility, admins see
every one): the events are delivered whether or not they can see it - nothing
is withheld at run time, and there is no strict mode - with a warning once an
hour and a Security Audit finding when they cannot. An owner-less
company-managed assignment keeps the OAuth gate alone.

``FakeSession`` runs each query's real WHERE clause against in-memory rows, so
these tests exercise the visibility predicate the code builds, not a canned
answer. The acting user is a NON-admin unless a test says otherwise.
"""

from __future__ import annotations

import contextlib
import json
import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.agent import Agent
from app.models.assignment import Assignment
from app.models.integration import Integration
from app.models.user import User
from app.services import trigger_integration_access as access
from tests.integration_credential_fakes import FakeSession, make_integration

OWNER = uuid.uuid4()
COLLEAGUE = uuid.uuid4()
RUN_AS = uuid.uuid4()
OWNER_GROUP = str(uuid.uuid4())
OTHER_GROUP = str(uuid.uuid4())

#: ``{case: (integration overrides, seen)}``, seen from ``OWNER`` (a member of
#: ``OWNER_GROUP``). Every integration holds a token the OAuth gate lets the
#: owner use, so only visibility differs.
CASES: dict[str, tuple[dict, bool]] = {
    "own": ({"user_id": OWNER}, True),
    "group_share_visible": (
        {"user_id": COLLEAGUE, "shared_group_ids": [OWNER_GROUP], "oauth_share_acknowledged": True},
        True,
    ),
    "group_share_visible_restricted": (
        {
            "user_id": COLLEAGUE,
            "shared_group_ids": [OWNER_GROUP],
            "oauth_share_acknowledged": True,
            "access_restricted": True,
        },
        True,
    ),
    "user_grant": (
        {"user_id": COLLEAGUE, "shared_user_ids": [str(OWNER)], "oauth_share_acknowledged": True},
        True,
    ),
    "company_managed": (
        {"user_id": None, "is_company_managed": True, "oauth_share_acknowledged": True},
        True,
    ),
    "hidden_group_share": (
        {"user_id": COLLEAGUE, "shared_group_ids": [OTHER_GROUP], "oauth_share_acknowledged": True},
        False,
    ),
    "hidden_restricted_company": (
        {
            "user_id": None,
            "is_company_managed": True,
            "oauth_share_acknowledged": True,
            "access_restricted": True,
        },
        False,
    ),
    "hidden_api_key": ({"user_id": COLLEAGUE, "auth_type": "api_key"}, False),
}
PARAMS = [pytest.param(case, seen, id=case) for case, (_, seen) in CASES.items()]


@pytest.fixture(autouse=True)
def _fresh_unseen_log():
    access._last_unseen_log.clear()
    yield
    access._last_unseen_log.clear()


@pytest.fixture(autouse=True)
def _groups(monkeypatch):
    memberships = {OWNER: [OWNER_GROUP], COLLEAGUE: [OTHER_GROUP], RUN_AS: []}

    async def _group_ids(_db, user_id):
        return list(memberships.get(user_id, []))

    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", _group_ids)
    return memberships


def _user(user_id: uuid.UUID, *, role: str = "user") -> User:
    return User(id=user_id, email=f"{user_id}@test.local", full_name="U", hashed_password="x", role=role)


def _users(*, admin: uuid.UUID | None = None) -> list[User]:
    return [
        _user(uid, role="admin" if uid == admin else "user") for uid in (OWNER, COLLEAGUE, RUN_AS)
    ]


def _integration(case: str) -> Integration:
    return make_integration(provider="microsoft", **CASES[case][0])


def _unseen_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == access.__name__]


# ---------------------------------------------------------------------------
# The event poller (assignments)
# ---------------------------------------------------------------------------


def _poller(monkeypatch, *, integrations, agent, assignments, users):
    from app.services import event_poller
    from tests.agentic_gate_stub import stub_agentic_gate

    stub_agentic_gate(monkeypatch)
    polled: list[str] = []

    async def _poll(_provider, _event, _token, wf, *_a, **_k):
        polled.append(str(wf.id))
        return []

    redis = AsyncMock()
    redis.get.return_value = None
    monkeypatch.setattr(event_poller, "redis_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(event_poller, "decrypt_value", lambda value: f"plain:{value}")
    monkeypatch.setattr(event_poller, "_poll_provider", _poll)
    monkeypatch.setattr("app.core.database.release_db_connection_before_loop", AsyncMock())
    db = FakeSession(
        {
            Assignment: list(assignments),
            Agent: [agent],
            Integration: list(integrations),
            User: list(users),
        }
    )
    return event_poller, db, polled


def _agent(owner=OWNER, **extra):
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=owner,
        enabled=True,
        is_company_managed=owner is None,
        integration_bindings=extra.pop("integration_bindings", {}),
        **extra,
    )


def _assignment(agent, *, integration_id=None, user_id=OWNER, run_as_user_id=None):
    config = {"provider": "microsoft", "event": "new_email"}
    if integration_id is not None:
        config["integration_id"] = str(integration_id)
    return SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent.id,
        name="Triage",
        trigger_type="integration_event",
        enabled=True,
        user_id=user_id,
        run_as_user_id=run_as_user_id,
        is_company_managed=agent.is_company_managed,
        trigger_config=config,
    )


@pytest.mark.parametrize(("case", "seen"), PARAMS)
async def test_worked_on_main_the_poller_delivers_a_pinned_integration_whoever_sees_it(
    monkeypatch, caplog, case, seen
):
    integration = _integration(case)
    agent = _agent()
    wf = _assignment(agent, integration_id=integration.id)
    event_poller, db, polled = _poller(
        monkeypatch, integrations=[integration], agent=agent, assignments=[wf], users=_users()
    )

    with caplog.at_level(logging.WARNING, logger=access.__name__):
        await event_poller.poll_integration_events(db)

    assert polled == [str(wf.id)]
    lines = _unseen_lines(caplog)
    if seen:
        assert lines == []
    else:
        assert len(lines) == 1
        assert "delivered" in lines[0] and str(wf.id) in lines[0] and str(integration.id) in lines[0]


@pytest.mark.parametrize(("case", "seen"), PARAMS)
async def test_worked_on_main_the_poller_delivers_the_agent_bound_fallback_whoever_sees_it(
    monkeypatch, caplog, case, seen
):
    integration = _integration(case)
    agent = _agent(integration_bindings={"microsoft": [str(integration.id)]})
    wf = _assignment(agent)
    event_poller, db, polled = _poller(
        monkeypatch, integrations=[integration], agent=agent, assignments=[wf], users=_users()
    )

    with caplog.at_level(logging.WARNING, logger=access.__name__):
        await event_poller.poll_integration_events(db)

    assert polled == [str(wf.id)]
    assert len(_unseen_lines(caplog)) == (0 if seen else 1)


async def test_an_admin_acting_user_sees_every_integration(monkeypatch, caplog):
    hidden = _integration("hidden_group_share")
    agent = _agent()
    wf = _assignment(agent, integration_id=hidden.id)
    event_poller, db, polled = _poller(
        monkeypatch, integrations=[hidden], agent=agent, assignments=[wf], users=_users(admin=OWNER)
    )

    with caplog.at_level(logging.WARNING, logger=access.__name__):
        await event_poller.poll_integration_events(db)

    assert polled == [str(wf.id)]
    assert _unseen_lines(caplog) == []


async def test_worked_on_main_an_ownerless_company_managed_assignment_keeps_the_oauth_gate_alone(
    monkeypatch,
):
    """No "run as" user and no owner: no acting user to check, exactly the
    rule it had before - the OAuth gate (acknowledged or non-OAuth)."""
    restricted = _integration("hidden_restricted_company")
    agent = _agent(owner=None)
    wf = _assignment(agent, integration_id=restricted.id, user_id=None)
    event_poller, db, polled = _poller(
        monkeypatch, integrations=[restricted], agent=agent, assignments=[wf], users=_users()
    )

    await event_poller.poll_integration_events(db)

    assert polled == [str(wf.id)]
    integration_reads = [
        stmt for stmt in db.statements if stmt.column_descriptions[0]["entity"] is Integration
    ]
    assert len(integration_reads) == 1, "no acting user: no visibility query"


@pytest.mark.parametrize("granted", [False, True], ids=["not_granted", "granted"])
async def test_worked_on_main_a_run_as_user_receives_the_events_and_is_flagged_unless_they_see_it(
    monkeypatch, caplog, granted
):
    """A company-managed assignment running as a chosen user receives the
    events either way; the user not seeing the integration is flagged."""
    overrides = {"shared_user_ids": [str(RUN_AS)]} if granted else {}
    integration = make_integration(
        provider="microsoft",
        user_id=None,
        is_company_managed=True,
        oauth_share_acknowledged=True,
        access_restricted=True,
        **overrides,
    )
    agent = _agent(owner=None)
    wf = _assignment(agent, integration_id=integration.id, user_id=None, run_as_user_id=RUN_AS)
    event_poller, db, polled = _poller(
        monkeypatch, integrations=[integration], agent=agent, assignments=[wf], users=_users()
    )

    with caplog.at_level(logging.WARNING, logger=access.__name__):
        await event_poller.poll_integration_events(db)

    assert polled == [str(wf.id)]
    lines = _unseen_lines(caplog)
    assert len(lines) == (0 if granted else 1)
    assert all(str(RUN_AS) in line for line in lines)


async def test_an_assignment_its_acting_user_cannot_see_is_named_once_an_hour(monkeypatch, caplog):
    hidden = _integration("hidden_api_key")
    agent = _agent()
    first = _assignment(agent, integration_id=hidden.id)
    second = _assignment(agent, integration_id=hidden.id)
    event_poller, db, polled = _poller(
        monkeypatch,
        integrations=[hidden],
        agent=agent,
        assignments=[first, second],
        users=_users(),
    )

    with caplog.at_level(logging.WARNING, logger=access.__name__):
        await event_poller.poll_integration_events(db)
        await event_poller.poll_integration_events(db)

    assert sorted(polled) == sorted([str(first.id), str(second.id)] * 2), "delivered on every tick"
    lines = _unseen_lines(caplog)
    assert len(lines) == 2, lines
    assert any(str(first.id) in line for line in lines)
    assert any(str(second.id) in line for line in lines)
    assert all(str(hidden.id) in line for line in lines)
    assert all("enc-access-token" not in line for line in lines)


async def test_one_visibility_lookup_per_user_and_integration_in_a_tick(monkeypatch):
    """Several assignments of one user on one mailbox cost one lookup."""
    shared = _integration("group_share_visible")
    agent = _agent()
    assignments = [_assignment(agent, integration_id=shared.id) for _ in range(3)]
    event_poller, db, polled = _poller(
        monkeypatch, integrations=[shared], agent=agent, assignments=assignments, users=_users()
    )

    await event_poller.poll_integration_events(db)

    assert sorted(polled) == sorted(str(wf.id) for wf in assignments)
    integration_reads = [
        stmt for stmt in db.statements if stmt.column_descriptions[0]["entity"] is Integration
    ]
    # One OAuth-gated load per assignment, one visibility lookup in all.
    assert len(integration_reads) == len(assignments) + 1


def test_the_unseen_line_is_rate_limited_per_target(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(access.time, "monotonic", lambda: clock[0])
    kwargs = {"integration_id": "i", "acting_user_id": "u"}

    assert access.log_unseen_trigger_integration("assignment", "a", **kwargs) is True
    assert access.log_unseen_trigger_integration("assignment", "a", **kwargs) is False
    assert access.log_unseen_trigger_integration("assignment", "b", **kwargs) is True
    clock[0] += access.UNSEEN_LOG_INTERVAL_SECONDS
    assert access.log_unseen_trigger_integration("assignment", "a", **kwargs) is True


def test_there_is_no_strict_mode_to_withhold_the_events():
    """The maintainer decision of 2026-09-27: nothing is withheld at
    run time, and no setting brings a withholding mode back."""
    from app.core.config import Settings

    assert not [name for name in Settings.model_fields if "trigger_integration" in name]
    assert not hasattr(access, "strict_trigger_visibility")


def test_the_acting_user_is_the_run_as_user_then_the_owner_then_the_agent_owner():
    agent = SimpleNamespace(user_id=COLLEAGUE)
    assert access.assignment_acting_user_id(
        SimpleNamespace(run_as_user_id=RUN_AS, user_id=OWNER), agent
    ) == RUN_AS
    assert access.assignment_acting_user_id(
        SimpleNamespace(run_as_user_id=None, user_id=OWNER), agent
    ) == OWNER
    assert access.assignment_acting_user_id(
        SimpleNamespace(run_as_user_id=None, user_id=None), agent
    ) == COLLEAGUE
    assert access.assignment_acting_user_id(
        SimpleNamespace(run_as_user_id=None, user_id=None), SimpleNamespace(user_id=None)
    ) is None


def test_the_trigger_integration_is_the_pinned_one_else_the_first_binding():
    agent = SimpleNamespace(integration_bindings={"microsoft": ["bound-1", "bound-2"]})
    assert access.trigger_integration_id(
        {"provider": "microsoft", "integration_id": "pinned"}, agent
    ) == "pinned"
    assert access.trigger_integration_id({"provider": "microsoft"}, agent) == "bound-1"
    assert access.trigger_integration_id({"provider": "slack"}, agent) is None
    assert access.trigger_integration_id(None, agent) is None


# ---------------------------------------------------------------------------
# The flow trigger poller (the same rule, applied to flows)
# ---------------------------------------------------------------------------


def _flow_poller(monkeypatch, *, integration, owner=OWNER, users=None):
    from app.services import flow_triggers

    flow = SimpleNamespace(
        id=uuid.uuid4(), name="Mailbox flow", user_id=owner, graph={}, status="active"
    )
    node = {
        "id": "trigger-1",
        "kind": "trigger.integration_event",
        "config": {"integration_id": str(integration.id), "event_type": "new_email"},
    }
    polled: list[str] = []

    async def _poll(_provider, _event, _token, target, *_a, **_k):
        polled.append(target.id)
        return []

    redis = AsyncMock()
    redis.get.return_value = None
    monkeypatch.setattr(
        flow_triggers, "_active_flows_with_trigger", AsyncMock(return_value=[(flow, [node])])
    )
    monkeypatch.setattr("app.core.redis.redis_client", AsyncMock(return_value=redis))
    monkeypatch.setattr("app.core.security.decrypt_value", lambda value: f"plain:{value}")
    monkeypatch.setattr("app.services.event_poller._poll_provider", _poll)
    monkeypatch.setattr("app.core.database.release_db_connection_before_loop", AsyncMock())
    db = FakeSession({Integration: [integration], User: users if users is not None else _users()})
    return flow_triggers, db, polled, flow


@pytest.mark.parametrize(("case", "seen"), PARAMS)
async def test_worked_on_main_the_flow_poller_delivers_whoever_sees_the_integration(
    monkeypatch, caplog, case, seen
):
    flow_triggers, db, polled, flow = _flow_poller(monkeypatch, integration=_integration(case))

    with caplog.at_level(logging.WARNING, logger=access.__name__):
        await flow_triggers.poll_flow_integration_events(db)

    assert polled == [f"flow:{flow.id}:trigger-1"]
    assert len(_unseen_lines(caplog)) == (0 if seen else 1)


async def test_a_flow_node_its_owner_cannot_see_is_named_once_an_hour(monkeypatch, caplog):
    flow_triggers, db, polled, flow = _flow_poller(
        monkeypatch, integration=_integration("hidden_group_share")
    )

    with caplog.at_level(logging.WARNING, logger=access.__name__):
        await flow_triggers.poll_flow_integration_events(db)
        await flow_triggers.poll_flow_integration_events(db)

    assert polled == [f"flow:{flow.id}:trigger-1"] * 2
    lines = _unseen_lines(caplog)
    assert len(lines) == 1 and str(flow.id) in lines[0] and "trigger-1" in lines[0]


# ---------------------------------------------------------------------------
# Microsoft Graph change notification
# ---------------------------------------------------------------------------


async def _graph_delivery(monkeypatch, integration: Integration, *, users=None, bound_id=None):
    from app.api.v1 import integration_notifications as notifications

    agent = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=OWNER,
        enabled=True,
        integration_bindings={"microsoft": [str(bound_id or integration.id)]},
    )
    assignment = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent.id,
        enabled=True,
        name="Triage",
        user_id=OWNER,
        run_as_user_id=None,
        is_company_managed=False,
    )
    db = FakeSession(
        {
            Assignment: [assignment],
            Agent: [agent],
            Integration: [integration],
            User: users if users is not None else _users(),
        }
    )

    @contextlib.asynccontextmanager
    async def _session():
        yield db

    redis = AsyncMock()
    # The record of the subscription as main reads it: the bare assignment id (a
    # legacy record, served as before by default).
    redis.mget.return_value = [str(assignment.id)]
    enriched_with: list[str] = []

    async def _enrich(_event, _notification, *, token, **_context):
        enriched_with.append(token)
        return {}, ""

    trigger = AsyncMock(return_value="run-1")
    monkeypatch.setattr(notifications, "async_session", _session)
    monkeypatch.setattr(notifications, "redis_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(notifications, "decrypt_value", lambda value: f"plain:{value}")
    monkeypatch.setattr(notifications, "notified_event", _enrich)
    monkeypatch.setattr(notifications, "trigger_assignment", trigger)
    body = json.dumps(
        {"value": [{"resource": "me/messages/1", "subscriptionId": "sub-1", "changeType": "created"}]}
    ).encode()

    async def _stream():
        yield body

    # The raw body is read, bounded, before it is parsed.
    request = SimpleNamespace(query_params={}, headers={"content-length": str(len(body))}, stream=_stream)
    result = await notifications.microsoft_graph_notification(request)
    return result, trigger, enriched_with


@pytest.mark.parametrize(("case", "seen"), PARAMS)
async def test_worked_on_main_a_graph_notification_is_delivered_whoever_sees_the_integration(
    monkeypatch, caplog, case, seen
):
    with caplog.at_level(logging.WARNING, logger=access.__name__):
        result, trigger, enriched_with = await _graph_delivery(monkeypatch, _integration(case))

    assert result == {"status": "ok", "processed": 1}
    assert enriched_with == ["plain:enc-access-token"]
    trigger.assert_awaited_once()
    assert len(_unseen_lines(caplog)) == (0 if seen else 1)


@pytest.mark.parametrize(
    ("overrides", "seen"),
    [
        # A colleague's OAuth integration shared with another group, token not
        # shared: hidden from the owner - delivered, flagged, not read.
        ({"user_id": COLLEAGUE, "shared_group_ids": [OTHER_GROUP]}, False),
        # Shared with the owner's group, token not shared: visible - delivered
        # as before, without reading the resource.
        ({"user_id": COLLEAGUE, "shared_group_ids": [OWNER_GROUP]}, True),
    ],
    ids=["hidden", "visible"],
)
async def test_a_graph_notification_reads_the_resource_only_with_a_usable_token(
    monkeypatch, caplog, overrides, seen
):
    integration = make_integration(provider="microsoft", **overrides)

    with caplog.at_level(logging.WARNING, logger=access.__name__):
        result, trigger, enriched_with = await _graph_delivery(monkeypatch, integration)

    assert enriched_with == [], "the token is not usable: nothing is read with it"
    assert result == {"status": "ok", "processed": 1}
    trigger.assert_awaited_once()
    assert len(_unseen_lines(caplog)) == (0 if seen else 1)


async def test_worked_on_main_a_graph_notification_bound_to_nothing_is_delivered(monkeypatch, caplog):
    """A binding that names no integration hides nothing: delivered, unread, unflagged."""
    unrelated = make_integration(provider="microsoft", user_id=COLLEAGUE)
    with caplog.at_level(logging.WARNING, logger=access.__name__):
        result, trigger, enriched_with = await _graph_delivery(
            monkeypatch, unrelated, bound_id=uuid.uuid4()
        )

    assert result == {"status": "ok", "processed": 1}
    assert enriched_with == []
    trigger.assert_awaited_once()
    assert _unseen_lines(caplog) == []


# ---------------------------------------------------------------------------
# The security audit
# ---------------------------------------------------------------------------


def _reports(monkeypatch, *, assignments, flows):
    monkeypatch.setattr(access, "assignment_trigger_visibility", AsyncMock(return_value=assignments))
    monkeypatch.setattr(access, "flow_trigger_visibility", AsyncMock(return_value=flows))


async def test_the_security_audit_warns_about_the_assignments_and_flows_their_user_cannot_see(
    monkeypatch,
):
    from app.api.v1 import security_audit

    wf_id, flow_id = uuid.uuid4(), uuid.uuid4()
    _reports(
        monkeypatch,
        assignments=access.TriggerVisibilityReport(checked=3, unseen=[(wf_id, "Mailbox triage")]),
        flows=access.TriggerVisibilityReport(checked=1, unseen=[(flow_id, "Mailbox flow")]),
    )
    findings: list = []

    await security_audit._check_trigger_integration_delivery(object(), findings)

    assert [(f.id, f.category, f.severity) for f in findings] == [
        ("access.assignment_trigger_integration_not_visible", "access", "warning"),
        ("access.flow_trigger_integration_not_visible", "access", "warning"),
    ]
    assert f"Mailbox triage ({wf_id})" in findings[0].description
    assert f"Mailbox flow ({flow_id})" in findings[1].description
    assert all("still reach" in f.description for f in findings)
    assert all(f.recommendation and "STRICT" not in f.recommendation for f in findings)


async def test_the_security_audit_names_the_first_fifty_and_counts_the_rest(monkeypatch):
    from app.api.v1 import security_audit

    unseen = [(uuid.uuid4(), f"Assignment {i:03}") for i in range(53)]
    _reports(
        monkeypatch,
        assignments=access.TriggerVisibilityReport(checked=53, unseen=unseen),
        flows=access.TriggerVisibilityReport(checked=0, unseen=[]),
    )
    findings: list = []

    await security_audit._check_trigger_integration_delivery(object(), findings)

    [finding] = findings
    assert finding.title.startswith("53 integration-event assignment(s)")
    assert "Assignment 049" in finding.description and "Assignment 050" not in finding.description
    assert "and 3 more" in finding.description


async def test_the_security_audit_passes_when_every_trigger_in_use_is_seen(monkeypatch):
    from app.api.v1 import security_audit

    _reports(
        monkeypatch,
        assignments=access.TriggerVisibilityReport(checked=2, unseen=[]),
        flows=access.TriggerVisibilityReport(checked=1, unseen=[]),
    )
    findings: list = []

    await security_audit._check_trigger_integration_delivery(object(), findings)

    assert [(f.id, f.category, f.severity) for f in findings] == [
        ("access.trigger_integrations_visible", "access", "pass")
    ]
    assert "2 assignment(s) and 1 active flow(s)" in findings[0].description


async def test_the_security_audit_says_nothing_without_an_integration_trigger(monkeypatch):
    from app.api.v1 import security_audit

    _reports(
        monkeypatch,
        assignments=access.TriggerVisibilityReport(checked=0, unseen=[]),
        flows=access.TriggerVisibilityReport(checked=0, unseen=[]),
    )
    findings: list = []

    await security_audit._check_trigger_integration_delivery(object(), findings)

    assert findings == []
