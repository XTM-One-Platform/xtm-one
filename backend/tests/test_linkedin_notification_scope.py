"""The LinkedIn notification endpoint reaches only the admin app's own integrations.

``POST /api/v1/integrations/notifications/linkedin`` is the second URL of the
admin's LinkedIn OAuth app, next to ``/api/hooks/providers/linkedin``: both
answer LinkedIn's challenge and verify ``X-LI-Signature`` with the LinkedIn
OAuth default's client secret only. It used to hand every verified notification
to every enabled LinkedIn assignment of the instance, whoever owned it. These
tests pin the provider route's rule on it
(``_admin_app_integrations_by_assignment`` in ``webhooks.py``): the app (stored
client id = the OAuth default's), the agent owner's right to use the
integration, the pin over the agent's bindings, and the refusals.

Requests go through the real ASGI stack (``TestClient``); the session answers
each query by the table it reads, so the tests do not depend on query order.
The SQL behind the visibility rule runs against PostgreSQL in
``test_linkedin_notification_scope_postgres.py``.
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import logging
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import integration_notifications as notifications
from app.api.v1 import webhooks
from app.core.security import encrypt_value
from app.models.agent import Agent
from app.models.assignment import Assignment
from app.models.group import GroupMember
from app.models.integration import Integration
from app.models.oauth_default import OAuthDefault

URL = "/api/v1/integrations/notifications/linkedin"
SECRET = "linkedin-admin-app-secret-4385"
ADMIN_CLIENT_ID = "linkedin-admin-app-client-id-4385"
OWN_CLIENT_ID = "users-own-linkedin-app-client-id"
NOT_REACHED = {"status": "ok", "triggered": 0}
BATCH = {
    "type": "ORGANIZATION_SOCIAL_ACTION_NOTIFICATIONS",
    "notifications": [
        {
            "action": "COMMENT",
            "notificationId": 4385,
            "organizationalEntity": "urn:li:organization:1",
            "sourcePost": "urn:li:share:1",
            "decoratedGeneratedActivity": {"comment": {"text": "hi", "owner": "urn:li:person:1"}},
        }
    ],
}


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


def _signature(body: bytes, secret: str = SECRET) -> str:
    return "hmacsha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _oauth_default(*, client_id: str = ADMIN_CLIENT_ID, secret: str | None = SECRET, client_id_enc=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        provider="linkedin",
        client_id_enc=client_id_enc or encrypt_value(client_id),
        client_secret_enc=encrypt_value(secret) if secret is not None else None,
        extra_config_enc=None,
    )


@dataclass
class _World:
    oauth_default: object | None
    assignments: list = field(default_factory=list)
    agents: list = field(default_factory=list)
    integrations: list = field(default_factory=list)
    memberships: list = field(default_factory=list)
    visible: list = field(default_factory=list)
    queries: list = field(default_factory=list)
    committed: bool = False

    def integration(
        self,
        owner,
        *,
        client_id: str | None = ADMIN_CLIENT_ID,
        auth_type: str = "oauth",
        acknowledged: bool = False,
        provider: str = "linkedin",
        client_id_enc: str | None = None,
    ) -> uuid.UUID:
        """A row as ``select(*_integration_row_columns())`` returns it."""
        integration_id = uuid.uuid4()
        if client_id_enc is None and client_id is not None:
            client_id_enc = encrypt_value(client_id)
        self.integrations.append((integration_id, provider, owner, auth_type, acknowledged, client_id_enc, None))
        return integration_id

    def agent(self, owner, *bound, enabled: bool = True) -> SimpleNamespace:
        agent = SimpleNamespace(
            id=uuid.uuid4(),
            name="LinkedIn agent",
            enabled=enabled,
            user_id=owner,
            integration_bindings={"linkedin": [str(i) for i in bound]} if bound else {},
        )
        self.agents.append(agent)
        return agent

    def assignment(self, agent, **config) -> SimpleNamespace:
        wf = SimpleNamespace(
            id=uuid.uuid4(),
            name=f"On LinkedIn {len(self.assignments)}",
            agent_id=agent.id,
            enabled=True,
            trigger_type="integration_event",
            trigger_config={"provider": "linkedin", **config},
        )
        self.assignments.append(wf)
        return wf


class _Result:
    def __init__(self, *, scalar=None, scalars=(), rows=()):
        self._scalar = scalar
        self._scalars = list(scalars)
        self._rows = list(rows)

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._scalars))

    def all(self):
        return list(self._rows)


def _bound_ids(statement) -> set[uuid.UUID]:
    """The UUIDs a statement's WHERE clause binds (``== id`` or ``IN (ids)``)."""
    ids: set[uuid.UUID] = set()
    for value in statement.compile().params.values():
        values = value if isinstance(value, (list, tuple, set, frozenset)) else [value]
        ids.update(v for v in values if isinstance(v, uuid.UUID))
    return ids


class _Session:
    """``async_session()`` answering each query by the table it reads."""

    def __init__(self, world: _World):
        self._world = world

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, statement, *_args, **_kwargs):
        columns = statement.column_descriptions
        entity = columns[0]["entity"]
        world = self._world
        if entity is OAuthDefault:
            world.queries.append("oauth_default")
            return _Result(scalar=world.oauth_default)
        if entity is Assignment:
            world.queries.append("assignments")
            return _Result(scalars=world.assignments)
        if entity is Agent:
            world.queries.append("agents")
            wanted = _bound_ids(statement)
            agents = [a for a in world.agents if a.id in wanted]
            return _Result(scalar=agents[0] if len(agents) == 1 else None, scalars=agents)
        if entity is GroupMember:
            world.queries.append("memberships")
            return _Result(rows=world.memberships)
        if entity is Integration and len(columns) == 2:
            # ``_foreign_visible_by_owner``: (integration id, owner) per visible row.
            world.queries.append("visibility")
            return _Result(rows=world.visible)
        if entity is Integration:
            world.queries.append("integrations")
            return _Result(rows=world.integrations)
        raise AssertionError(f"unexpected query on {entity!r}")

    async def commit(self):
        self._world.committed = True


def _deliver(monkeypatch, world: _World, *, batch=None, sign_with: str | None = SECRET):
    """POST *batch* (``BATCH`` by default) and return the response and the triggered assignment ids."""
    triggered: list = []

    async def _trigger(**kwargs):
        triggered.append(kwargs["assignment"].id)
        return f"run-{len(triggered)}"

    monkeypatch.setattr(notifications, "async_session", lambda: _Session(world))
    monkeypatch.setattr(notifications, "trigger_assignment", _trigger)
    app = FastAPI()
    app.include_router(notifications.notification_router, prefix="/api/v1/integrations")
    body = json.dumps(BATCH if batch is None else batch).encode()
    headers = {"Content-Type": "application/json"}
    if sign_with is not None:
        headers["X-LI-Signature"] = _signature(body, sign_with)
    response = TestClient(app).post(URL, content=body, headers=headers)
    return response, triggered


# ---------------------------------------------------------------------------
# Which integrations a delivery reaches an assignment through
# ---------------------------------------------------------------------------


def test_the_owners_own_integration_on_the_admin_app_is_reached(monkeypatch):
    world = _World(_oauth_default())
    alice = uuid.uuid4()
    reached = world.assignment(world.agent(alice, world.integration(alice)))
    # A disabled agent stays out, as before.
    world.assignment(world.agent(alice, world.integration(alice), enabled=False))

    response, triggered = _deliver(monkeypatch, world)

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "triggered": 1}
    assert triggered == [reached.id]
    assert world.committed


def test_an_integration_on_the_users_own_app_never_hears_the_admin_app(monkeypatch):
    world = _World(_oauth_default())
    alice = uuid.uuid4()
    world.assignment(world.agent(alice, world.integration(alice, client_id=OWN_CLIENT_ID)))

    response, triggered = _deliver(monkeypatch, world)

    assert response.json() == NOT_REACHED
    assert triggered == []


def test_an_assignment_reached_through_no_integration_is_not_reached(monkeypatch):
    """The old fan-out handed every LinkedIn assignment the delivery, bound or not."""
    world = _World(_oauth_default())
    world.assignment(world.agent(uuid.uuid4()))

    response, triggered = _deliver(monkeypatch, world)

    assert response.json() == NOT_REACHED
    assert triggered == []


def test_another_users_private_integration_is_not_reached(monkeypatch):
    """Bound while it was shared, the share since withdrawn: even acknowledged,
    the token counts only while the agent's owner sees the integration."""
    world = _World(_oauth_default())
    alice, bob = uuid.uuid4(), uuid.uuid4()
    world.assignment(world.agent(alice, world.integration(bob, acknowledged=True)))

    response, triggered = _deliver(monkeypatch, world)

    assert response.json() == NOT_REACHED
    assert triggered == []
    assert "visibility" in world.queries, "the owner's visibility was not asked"


def test_a_visible_group_shared_non_oauth_integration_is_reached(monkeypatch):
    world = _World(_oauth_default())
    alice, bob = uuid.uuid4(), uuid.uuid4()
    shared = world.integration(bob, auth_type="api_key")
    reached = world.assignment(world.agent(alice, shared))
    world.memberships = [(alice, "team")]
    world.visible = [(shared, str(alice))]

    response, triggered = _deliver(monkeypatch, world)

    assert response.json() == {"status": "ok", "triggered": 1}
    assert triggered == [reached.id]


def test_an_oauth_integration_shared_without_acknowledgement_is_not_reached(monkeypatch):
    """Visible to the agent's owner, but the OAuth token is personal to whoever
    connected it until they acknowledge the share."""
    world = _World(_oauth_default())
    alice, bob = uuid.uuid4(), uuid.uuid4()
    shared = world.integration(bob, acknowledged=False)
    world.assignment(world.agent(alice, shared))
    world.memberships = [(alice, "team")]
    world.visible = [(shared, str(alice))]

    response, triggered = _deliver(monkeypatch, world)

    assert response.json() == NOT_REACHED
    assert triggered == []


def test_a_share_acknowledged_integration_the_owner_still_sees_is_reached(monkeypatch):
    world = _World(_oauth_default())
    alice, bob = uuid.uuid4(), uuid.uuid4()
    shared = world.integration(bob, acknowledged=True)
    reached = world.assignment(world.agent(alice, shared))
    world.memberships = [(alice, "team")]
    world.visible = [(shared, str(alice))]

    response, triggered = _deliver(monkeypatch, world)

    assert response.json() == {"status": "ok", "triggered": 1}
    assert triggered == [reached.id]


def test_the_pinned_integration_decides_over_the_agents_bindings(monkeypatch):
    world = _World(_oauth_default())
    alice = uuid.uuid4()
    admin_app = world.integration(alice)
    own_app = world.integration(alice, client_id=OWN_CLIENT_ID)
    # Bound to the admin app, pinned to the user's own app: not reached.
    world.assignment(world.agent(alice, admin_app), integration_id=str(own_app))
    # Bound to the user's own app, pinned to the admin app: reached.
    reached = world.assignment(world.agent(alice, own_app), integration_id=str(admin_app))

    response, triggered = _deliver(monkeypatch, world)

    assert response.json() == {"status": "ok", "triggered": 1}
    assert triggered == [reached.id]


def test_an_integration_of_another_provider_is_not_a_linkedin_one(monkeypatch):
    world = _World(_oauth_default())
    alice = uuid.uuid4()
    github = world.integration(alice, provider="github")
    world.assignment(world.agent(alice), integration_id=str(github))

    response, triggered = _deliver(monkeypatch, world)

    assert response.json() == NOT_REACHED
    assert triggered == []


def test_an_agent_with_no_owner_keeps_the_pollers_rule(monkeypatch):
    """A company-managed agent has no user to check visibility against: it hears
    through an integration that is not OAuth or whose token is share-acknowledged."""
    world = _World(_oauth_default())
    acknowledged = world.assignment(world.agent(None, world.integration(None, acknowledged=True)))
    non_oauth = world.assignment(world.agent(None, world.integration(None, auth_type="api_key")))
    world.assignment(world.agent(None, world.integration(None, acknowledged=False)))

    response, triggered = _deliver(monkeypatch, world)

    assert response.json() == {"status": "ok", "triggered": 2}
    assert sorted(triggered) == sorted([acknowledged.id, non_oauth.id])


def test_the_event_filter_is_unchanged(monkeypatch):
    world = _World(_oauth_default())
    alice = uuid.uuid4()
    integ = world.integration(alice)
    agent = world.agent(alice, integ)
    catch_all = world.assignment(agent, event="linkedin_social_action")
    comments = world.assignment(agent, event="linkedin_comment")
    no_event = world.assignment(agent)
    world.assignment(agent, event="linkedin_like")

    response, triggered = _deliver(monkeypatch, world)

    assert response.json() == {"status": "ok", "triggered": 3}
    assert triggered == [catch_all.id, comments.id, no_event.id]


# ---------------------------------------------------------------------------
# No app, no delivery; and nothing is read before the signature is checked
# ---------------------------------------------------------------------------


def test_without_an_oauth_default_nobody_is_reached_and_nothing_is_looked_up(monkeypatch):
    world = _World(None)
    alice = uuid.uuid4()
    world.assignment(world.agent(alice, world.integration(alice)))

    response, triggered = _deliver(monkeypatch, world, sign_with=None)

    assert response.status_code == 200
    assert response.json() == NOT_REACHED
    assert triggered == []
    assert world.queries == ["oauth_default"]


def test_an_unverified_delivery_reaches_nobody_when_no_secret_is_configured(monkeypatch):
    """Without a client secret nothing proves the sender came from the admin app,
    so there is no app to hand the delivery to - and LinkedIn cannot register
    this URL anyway, since the challenge is answered with that secret."""
    world = _World(_oauth_default(secret=None))
    alice = uuid.uuid4()
    world.assignment(world.agent(alice, world.integration(alice)))

    response, triggered = _deliver(monkeypatch, world, sign_with=None)

    assert response.json() == NOT_REACHED
    assert triggered == []
    assert world.queries == ["oauth_default"]


def test_an_admin_app_without_a_client_id_reaches_nobody(monkeypatch):
    world = _World(_oauth_default(client_id=""))
    alice = uuid.uuid4()
    world.assignment(world.agent(alice, world.integration(alice)))

    response, triggered = _deliver(monkeypatch, world)

    assert response.json() == NOT_REACHED
    assert triggered == []


def test_the_signature_is_checked_before_any_lookup(monkeypatch):
    world = _World(_oauth_default())
    alice = uuid.uuid4()
    world.assignment(world.agent(alice, world.integration(alice)))

    response, triggered = _deliver(monkeypatch, world, sign_with="not-the-secret")

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid LinkedIn signature"
    assert triggered == []
    assert world.queries == ["oauth_default"]


# ---------------------------------------------------------------------------
# Unreadable stored credentials are surfaced, never read as "nothing matched"
# ---------------------------------------------------------------------------


def test_an_unreadable_admin_client_id_is_a_503_before_anything_is_triggered(monkeypatch, caplog):
    world = _World(_oauth_default(client_id_enc="not-a-fernet-token"))
    alice = uuid.uuid4()
    world.assignment(world.agent(alice, world.integration(alice)))

    with caplog.at_level(logging.ERROR):
        response, triggered = _deliver(monkeypatch, world)

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "300"
    assert triggered == []
    assert not world.committed
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert "client id cannot be decrypted" in errors[0].getMessage()
    assert "not-a-fernet-token" not in caplog.text


def test_an_integration_with_an_unreadable_client_id_is_skipped_with_one_warning(monkeypatch, caplog):
    monkeypatch.setattr(webhooks, "_unreadable_integrations_warned", set())
    world = _World(_oauth_default())
    alice = uuid.uuid4()
    unreadable = world.integration(alice, client_id_enc="not-a-fernet-token")
    world.assignment(world.agent(alice, unreadable))

    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            response, triggered = _deliver(monkeypatch, world)
            assert response.json() == NOT_REACHED
            assert triggered == []

    warnings = [r for r in caplog.records if str(unreadable) in r.getMessage()]
    assert len(warnings) == 1
    assert "client id cannot be decrypted" in warnings[0].getMessage()
    assert "not-a-fernet-token" not in caplog.text


# ---------------------------------------------------------------------------
# One rule for every URL of the admin app
# ---------------------------------------------------------------------------


def test_the_notification_route_and_the_provider_route_share_one_rule():
    """Never a second spelling of the rule: both routes hand a delivery over
    through the same function."""
    shared = webhooks._admin_app_integrations_or_report
    assert notifications._admin_app_integrations_or_report is shared
    for route in (notifications.linkedin_social_action_notification, webhooks.receive_provider_webhook):
        assert "await _admin_app_integrations_or_report(" in inspect.getsource(route)
    route_source = inspect.getsource(notifications.linkedin_social_action_notification)
    for helper in ("_admin_app_integrations(", "_foreign_visible_by_owner(", "_assignment_candidate_ids("):
        assert helper not in route_source


# ---------------------------------------------------------------------------
# The default: every LinkedIn assignment keeps receiving what it received before
# ---------------------------------------------------------------------------


def test_by_default_an_unsigned_delivery_without_a_client_secret_still_triggers(monkeypatch, caplog):
    _default_mode(monkeypatch)
    world = _World(_oauth_default(secret=None))
    alice = uuid.uuid4()
    world.assignment(world.agent(alice, world.integration(alice)))
    with caplog.at_level(logging.WARNING):
        response, triggered = _deliver(monkeypatch, world, sign_with=None)
    assert response.status_code == 200
    assert len(triggered) == 1
    assert any("accepted without a verified signature" in r.getMessage() for r in caplog.records)


def test_by_default_an_assignment_reached_through_no_integration_is_still_reached(monkeypatch, caplog):
    _default_mode(monkeypatch)
    world = _World(_oauth_default())
    world.assignment(world.agent(uuid.uuid4()))
    with caplog.at_level(logging.WARNING):
        response, triggered = _deliver(monkeypatch, world)
    assert response.status_code == 200
    assert len(triggered) == 1
    assert any("no integration on the admin LinkedIn app" in r.getMessage() for r in caplog.records)


def test_by_default_a_wrong_signature_is_still_refused(monkeypatch):
    _default_mode(monkeypatch)
    world = _World(_oauth_default())
    world.assignment(world.agent(uuid.uuid4()))
    response, triggered = _deliver(monkeypatch, world, sign_with="not-the-secret")
    assert response.status_code == 401
    assert triggered == []
