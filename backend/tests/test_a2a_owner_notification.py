"""Notification when an agent becomes externally reachable.

This is the mitigation for a deliberately accepted risk: full parity with
existing management authority means a config-tier group member can expose
another user's agent, which places that OWNER's data boundary on an external
surface. The authority was not narrowed; the affected users are told instead
(FR-005). If this stops firing, the risk becomes silent.

It fires from the flush hook rather than from an endpoint, and that is the
point of testing it here. Exposure follows an integration binding, so a
notification wired to an expose endpoint — as this once was — would protect
nobody once that endpoint no longer exists.
"""

import uuid
from contextlib import nullcontext

from sqlalchemy import inspect as sa_inspect

from app.a2a import hooks
from app.a2a import notifications as notif
from app.models.activity import Activity
from app.models.agent import Agent
from tests.a2a_session_doubles import TxnSurface


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return self._rows


class _ScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _Scalars(self._rows)


class _DirectionRows:
    """What ``a2a.providers.directions_for`` expects: (id, direction) pairs."""

    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _LiveExposure:
    """The one attribute the sync path reads to tell live from just-revoked."""

    revoked_at = None


class _Session(TxnSurface):
    """Sync session double — the listener runs in ``before_flush``."""

    no_autoflush = nullcontext()

    def __init__(self, live=(), inbound_ids=()):
        self._live = list(live)
        self.added = []
        # Which bound integrations face inward. Exposure — and therefore the
        # owner notice these tests assert — follows an INBOUND binding only,
        # so a test that wants a notice must say the binding is inbound.
        self._inbound_ids = list(inbound_ids)

    def execute(self, _stmt):
        if "a2a_integration_settings" in str(_stmt):
            return _DirectionRows([(i, "inbound") for i in self._inbound_ids])
        return _ScalarResult(self._live)

    def add(self, obj):
        self.added.append(obj)


def _bind(agent_id=None, owner_id=None, provider="oauth_a2a", integration_id=None):
    """A persistent agent whose A2A bindings just changed.

    Returns the agent; pass the same ``integration_id`` to ``_Session`` so the
    binding resolves as inbound and the exposure — hence the notice — happens.
    """
    agent = Agent(name="Threat Triage", enabled=True)
    agent.id = agent_id or uuid.uuid4()
    agent.user_id = owner_id or uuid.uuid4()
    agent.integration_bindings = {}
    sa_inspect(agent)._commit_all({})
    agent.integration_bindings = {provider: [str(integration_id or uuid.uuid4())]}
    return agent


def _activities(session):
    return [
        obj
        for obj in session.added
        if getattr(obj, "activity_type", None) == notif.ACTIVITY_EXPOSED
    ]


def test_binding_an_a2a_integration_notifies_the_owner():
    owner_id, agent_id = uuid.uuid4(), uuid.uuid4()
    integration_id = uuid.uuid4()
    session = _Session(inbound_ids=[integration_id])
    hooks._sync_implicit_exposure(
        session, _bind(agent_id, owner_id, integration_id=integration_id)
    )

    entries = _activities(session)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.user_id == owner_id
    assert entry.agent_id == agent_id
    assert entry.agent_name == "Threat Triage"
    assert "Threat Triage" in entry.title
    assert "data boundary" in entry.summary.lower()


def test_the_run_as_user_is_told_too_when_it_differs():
    """On a company-managed agent the identity external work executes as is
    frequently not the stored owner, and it is that person's reach a partner
    inherits — so they are the one who most needs to know."""
    agent = _bind()
    agent.is_company_managed = True
    run_as = uuid.uuid4()
    session = _Session()
    for values in hooks._exposed_notice_values(agent, run_as):
        session.add(Activity(**values))

    recipients = {e.user_id for e in _activities(session)}
    assert recipients == {agent.user_id, run_as}


def test_one_notice_when_owner_and_run_as_are_the_same_person():
    """The ordinary personally-owned case: told once, not twice."""
    agent = _bind()
    session = _Session()
    for values in hooks._exposed_notice_values(agent, agent.user_id):
        session.add(Activity(**values))
    assert len(_activities(session)) == 1


def test_unbinding_raises_no_exposure_notice():
    agent = Agent(name="Threat Triage", enabled=True)
    agent.id = uuid.uuid4()
    agent.user_id = uuid.uuid4()
    agent.integration_bindings = {"oauth_a2a": [str(uuid.uuid4())]}
    sa_inspect(agent)._commit_all({})
    agent.integration_bindings = {"oauth_a2a": []}

    session = _Session()
    hooks._sync_implicit_exposure(session, agent)
    assert _activities(session) == []


def test_an_already_exposed_agent_is_not_re_announced():
    """Binding a SECOND partner to an already-reachable agent changes nothing
    about the boundary, so re-notifying would train the owner to ignore the
    notice that matters."""
    integration_id = uuid.uuid4()
    agent = _bind(integration_id=integration_id)
    session = _Session(live=[_LiveExposure()], inbound_ids=[integration_id])
    hooks._sync_implicit_exposure(session, agent)
    assert _activities(session) == []
