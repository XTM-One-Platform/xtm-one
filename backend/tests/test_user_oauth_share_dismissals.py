"""Tests for the server-side dismissal of the OAuth-share dashboard reminder
(``users.oauth_share_dismissed_integration_ids``).

The "N of your integrations share your personal OAuth credentials"
reminder is dismissible, and the dismissal lives on the user, next to
``favorite_agent_ids``, with two server-side rules this file pins:

* ``PUT /settings/preferences`` keeps only ids naming an OAuth integration the
  caller OWNS whose token is currently acknowledged as shared -- the exact
  condition under which the dashboard shows the reminder -- and rejects junk
  (a non-UUID entry, a list above the cap) with a 422. It must also stay an
  explicitly-present-key update (``model_fields_set``) so clearing the last
  dismissal -- an empty list, which is falsy -- is not mistaken for "field
  omitted" and silently dropped.
* ``PUT /integrations/{id}`` drops the integration from its OWNER's list the
  moment ``oauth_share_acknowledged`` flips back to False, so sharing it again
  later brings the reminder back -- the semantics a client-side prune
  once implemented, now owned by the server.

Mirrors the repo's fake-session convention: the backend suite has no real-DB
harness, so the endpoint coroutine and the helpers are driven directly.
"""

from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.api.v1.integrations as integrations_api
import app.api.v1.settings as settings_api
from app.api.v1.schemas.settings import UserPreferencesRequest
from app.models.integration import Integration

OWNER_ID = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
ADMIN_ID = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
INTEG_A = "11111111-1111-4111-8111-111111111111"
INTEG_B = "22222222-2222-4222-8222-222222222222"


# ---------------------------------------------------------------------------
# PUT /settings/preferences
# ---------------------------------------------------------------------------


class _IdResult:
    """Stands in for the ``select(Integration.id)`` probe's result."""

    def __init__(self, ids):
        self._ids = ids

    def scalars(self):
        return self

    def all(self):
        return list(self._ids)


class _FakeDB:
    """Answers the ownership probe with a fixed set of "shared" integration ids.

    The only statement the endpoint issues for this field is the
    ``Integration.id IN (...) AND user_id = ... AND auth_type = 'oauth' AND
    oauth_share_acknowledged IS true`` probe; the asked ids are read back off
    it so a test can assert what was checked, and the rendered SQL is kept so
    a test can assert the predicates that make this an OWNER-only list.
    """

    def __init__(self, *, shared_ids=None):
        # ``None`` = every id asked about passes (the common case); a list
        # pins exactly which ones do.
        self._shared = shared_ids
        self.asked = None
        self.sql = None
        self.params = None

    async def flush(self):
        return None

    async def commit(self):
        return None

    async def execute(self, statement):
        compiled = statement.compile()
        self.sql = str(compiled)
        self.params = dict(compiled.params)
        # The IN may render as one expanding parameter (a sequence) or as one
        # parameter per value; flatten both shapes. The owner id travels as a
        # UUID parameter too and is not one of the asked ids.
        asked = []
        for param in compiled.params.values():
            values = param if isinstance(param, (list, tuple)) else [param]
            asked.extend(
                v for v in values if isinstance(v, uuid.UUID) and v != OWNER_ID
            )
        self.asked = asked
        if self._shared is None:
            return _IdResult(asked)
        return _IdResult([i for i in asked if i in self._shared])


def _user(**overrides):
    base = dict(
        id=OWNER_ID,
        email="owner@example.com",
        full_name="Owner",
        title=None,
        preferred_llm_model=None,
        favorite_agent_ids=None,
        oauth_share_dismissed_integration_ids=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


async def _update(monkeypatch, user, body: UserPreferencesRequest, db=None):
    async def _no_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(settings_api, "log_audit", _no_audit)
    return await settings_api.update_preferences(
        body=body,
        request=SimpleNamespace(headers={}, client=None),
        user=user,
        db=db or _FakeDB(),
    )


@pytest.mark.asyncio
async def test_dismissals_are_stored_deduplicated_in_order(monkeypatch):
    """Closing the banner sends every visible id at once; a repeat must not
    leave a second entry."""
    user = _user()
    await _update(
        monkeypatch,
        user,
        UserPreferencesRequest(
            oauth_share_dismissed_integration_ids=[INTEG_A, INTEG_B, INTEG_A]
        ),
    )
    assert user.oauth_share_dismissed_integration_ids == [INTEG_A, INTEG_B]


@pytest.mark.asyncio
async def test_only_own_acknowledged_shared_oauth_integrations_are_kept(monkeypatch):
    """The probe is what keeps the list honest: another user's integration, a
    deleted or unshared one, a non-OAuth row -- none of them can be muted."""
    user = _user()
    alive = uuid.UUID(INTEG_A)
    foreign = uuid.UUID(INTEG_B)
    db = _FakeDB(shared_ids=[alive])
    await _update(
        monkeypatch,
        user,
        UserPreferencesRequest(
            oauth_share_dismissed_integration_ids=[str(foreign), str(alive)]
        ),
        db=db,
    )
    assert user.oauth_share_dismissed_integration_ids == [str(alive)]
    # Both ids really went through the probe -- the assertion above is the
    # filter at work, not an empty query returning nothing.
    assert set(db.asked) == {alive, foreign}
    # ...and the probe is scoped to the caller's own, currently shared OAuth
    # rows: these three predicates are the whole point of the column.
    assert "integrations.user_id = " in db.sql
    assert "integrations.auth_type = " in db.sql
    assert "integrations.oauth_share_acknowledged IS" in db.sql
    assert OWNER_ID in db.params.values()
    assert "oauth" in db.params.values()


@pytest.mark.asyncio
async def test_clearing_the_last_dismissal_persists_the_empty_list(monkeypatch):
    user = _user(oauth_share_dismissed_integration_ids=[INTEG_A])
    await _update(
        monkeypatch,
        user,
        UserPreferencesRequest(oauth_share_dismissed_integration_ids=[]),
    )
    assert user.oauth_share_dismissed_integration_ids == []


@pytest.mark.asyncio
async def test_selection_above_the_cap_is_rejected(monkeypatch):
    """Unbounded growth would bloat the users row and every /auth/me payload."""
    user = _user()
    too_many = [
        str(uuid.uuid4()) for _ in range(settings_api.MAX_OAUTH_SHARE_DISMISSALS + 1)
    ]
    with pytest.raises(HTTPException) as excinfo:
        await _update(
            monkeypatch,
            user,
            UserPreferencesRequest(oauth_share_dismissed_integration_ids=too_many),
        )
    assert excinfo.value.status_code == 422
    assert user.oauth_share_dismissed_integration_ids is None  # nothing written


@pytest.mark.asyncio
async def test_non_uuid_id_is_rejected(monkeypatch):
    user = _user()
    with pytest.raises(HTTPException) as excinfo:
        await _update(
            monkeypatch,
            user,
            UserPreferencesRequest(oauth_share_dismissed_integration_ids=["not-a-uuid"]),
        )
    assert excinfo.value.status_code == 422
    assert user.oauth_share_dismissed_integration_ids is None


@pytest.mark.asyncio
async def test_omitting_the_field_leaves_dismissals_untouched(monkeypatch):
    """Every other preference update (theme, language, favorites, ...) must
    not wipe them."""
    kept = [INTEG_A]
    user = _user(oauth_share_dismissed_integration_ids=list(kept))
    await _update(monkeypatch, user, UserPreferencesRequest(title="CTO"))
    assert user.oauth_share_dismissed_integration_ids == kept


# ---------------------------------------------------------------------------
# PUT /integrations/{id}: unsharing forgets the dismissal
# ---------------------------------------------------------------------------


class _FakeSession:
    """Only ``get`` is needed: the helper loads the owner when the actor is
    somebody else (an admin editing a member's integration)."""

    def __init__(self, users=None):
        self._users = users or {}
        self.get_calls = []

    async def get(self, model, pk):
        self.get_calls.append((model, pk))
        return self._users.get(pk)


def _integration(owner_id=OWNER_ID):
    return Integration(
        id=uuid.UUID(INTEG_A),
        auth_type="oauth",
        provider="github",
        name="GitHub",
        user_id=owner_id,
    )


@pytest.mark.asyncio
async def test_unsharing_forgets_the_owners_dismissal():
    """The common case: the owner turns sharing off themselves."""
    owner = SimpleNamespace(
        id=OWNER_ID, oauth_share_dismissed_integration_ids=[INTEG_A, INTEG_B]
    )
    db = _FakeSession()

    await integrations_api._forget_oauth_share_dismissal(
        db, _integration(), actor=owner
    )

    assert owner.oauth_share_dismissed_integration_ids == [INTEG_B]
    assert db.get_calls == []  # the actor IS the owner: no lookup


@pytest.mark.asyncio
async def test_an_admin_unsharing_loads_the_owner():
    """An admin editing a member's integration must clean the MEMBER's list,
    not their own."""
    from app.models.user import User

    owner = SimpleNamespace(
        id=OWNER_ID, oauth_share_dismissed_integration_ids=[INTEG_A]
    )
    admin = SimpleNamespace(
        id=ADMIN_ID, oauth_share_dismissed_integration_ids=[INTEG_A]
    )
    db = _FakeSession(users={OWNER_ID: owner})

    await integrations_api._forget_oauth_share_dismissal(
        db, _integration(), actor=admin
    )

    assert owner.oauth_share_dismissed_integration_ids == []
    assert admin.oauth_share_dismissed_integration_ids == [INTEG_A]
    assert db.get_calls == [(User, OWNER_ID)]


@pytest.mark.asyncio
async def test_forget_is_a_no_op_when_nothing_was_dismissed():
    """An unrelated unshare must not dirty the owner's row (or replace the
    list with an equal copy, which SQLAlchemy would still write)."""
    kept = [INTEG_B]
    owner = SimpleNamespace(id=OWNER_ID, oauth_share_dismissed_integration_ids=kept)

    await integrations_api._forget_oauth_share_dismissal(
        _FakeSession(), _integration(), actor=owner
    )

    assert owner.oauth_share_dismissed_integration_ids is kept

    # NULL column (nothing ever dismissed) is left NULL, not turned into [].
    fresh = SimpleNamespace(id=OWNER_ID, oauth_share_dismissed_integration_ids=None)
    await integrations_api._forget_oauth_share_dismissal(
        _FakeSession(), _integration(), actor=fresh
    )
    assert fresh.oauth_share_dismissed_integration_ids is None


@pytest.mark.asyncio
async def test_forget_skips_an_ownerless_integration():
    """A legacy company-managed row with no owner has nobody's list to clean."""
    db = _FakeSession()
    actor = SimpleNamespace(id=ADMIN_ID, oauth_share_dismissed_integration_ids=[INTEG_A])

    await integrations_api._forget_oauth_share_dismissal(
        db, _integration(owner_id=None), actor=actor
    )

    assert db.get_calls == []
    assert actor.oauth_share_dismissed_integration_ids == [INTEG_A]


def test_update_integration_forgets_the_dismissal_after_the_share_consent():
    """Wiring tripwire: the endpoint compares the acknowledgement before and
    after the share choice (``apply_share_choice``, which applies
    ``apply_oauth_share_consent``) and forgets the dismissal BEFORE the
    owner transfer, so it is the owner who dismissed whose list is cleaned.
    The endpoint itself is too wide to drive through a fake session."""
    source = inspect.getsource(integrations_api.update_integration)
    read_at = source.index("was_oauth_share_acknowledged = integration.oauth_share_acknowledged")
    guard_at = source.index("apply_share_choice(")
    forget_at = source.index("_forget_oauth_share_dismissal(")
    transfer_at = source.index("apply_owner_transfer(")
    assert read_at < guard_at < forget_at < transfer_at
    # The read happens before the auth-type switch, which is the other place
    # the acknowledgement is reset.
    assert read_at < source.index("integration.oauth_share_acknowledged = False")


def test_delete_integration_forgets_the_dismissal_before_the_row_goes():
    """Wiring tripwire for the other exit: a deleted integration's id must not
    linger in the owner's list, where it would count against the cap of every
    later dismissal until a write happened to filter it out."""
    source = inspect.getsource(integrations_api.delete_integration)
    assert source.index("_forget_oauth_share_dismissal(") < source.index(
        "db.delete(integration)"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
