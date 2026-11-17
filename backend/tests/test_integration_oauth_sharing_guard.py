"""Sharing an OAuth integration shares its connected account.

An OAuth-authenticated integration's token is personal to whoever connected
it. Sharing it - a group, a per-user grant, company-managed status, or a
switch to OAuth on an integration that is already shared - hands that account
to everyone it is shared with: ``update_integration`` / ``create_integration``
keep the stored token and record the owner's consent
(``oauth_share_acknowledged``) by default. ``keep_oauth_credentials_on_share:
false`` sent explicitly is the opt-out: the integration stays shared, its
token is removed and someone must reconnect it. An edit that does not opt out
never removes a token; un-sharing makes the integration personal again.

These tests hit the real FastAPI router with a minimal in-memory DB stub -
same pattern as ``test_integration_bundles_api.py``'s ``bundles_client``,
scoped down to just the ``Integration`` create / update paths.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import deps
from app.api.deps import require_agentic_ai
from app.api.v1 import integrations as integrations_api
from app.core.database import get_db
from app.models.integration import Integration
from app.services.integration_credentials import oauth_credentials_usable


@dataclass
class _Result:
    rows: list | None = None

    def scalar_one_or_none(self):
        rows = self.rows or []
        return rows[0] if rows else None

    def scalars(self):
        return self

    def all(self):
        return list(self.rows or [])


#: A group the shares name: group grants are canonical UUIDs.
_GROUP = "6f1c1f0e-5d55-4a8a-9f3e-0c1b2a3d4e5f"


def _named_ids(params: dict) -> list:
    """The ids an ``IN`` lookup names, from its compiled parameters."""
    ids: list = []
    for key, value in params.items():
        if key.split("_")[0] == "id":
            ids.extend(value if isinstance(value, (list, tuple)) else [value])
    return ids


class _FakeDb:
    def __init__(self):
        self.integrations: dict[str, Integration] = {}
        #: Users a per-user grant may name (``normalize_user_grants``).
        self.active_user_ids: set[str] = set()
        self.added: list = []

    async def flush(self):
        return None

    async def commit(self):
        return None

    def add(self, obj):
        self.added.append(obj)
        if isinstance(obj, Integration):
            # What the database fills in on INSERT.
            now = datetime.now(UTC)
            for attr, value in (
                ("id", uuid.uuid4()),
                ("created_at", now),
                ("updated_at", now),
                ("is_builtin", False),
            ):
                if getattr(obj, attr, None) is None:
                    setattr(obj, attr, value)
            self.integrations[str(obj.id)] = obj

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0].get("entity")
        params = stmt.compile().params

        if entity is Integration:
            id_values = [v for k, v in params.items() if k.split("_")[0] == "id"]
            if id_values:
                rows = [
                    self.integrations[str(v)]
                    for v in id_values
                    if str(v) in self.integrations
                ]
                return _Result(rows=rows)
            return _Result(rows=list(self.integrations.values()))

        if "users.is_active" in str(stmt):
            # The per-user grant validation: (id, is_active) for each grantee.
            return _Result(rows=[(uid, True) for uid in sorted(self.active_user_ids)])

        if "FROM groups" in str(stmt):
            # The group grant validation (``normalize_group_grants``): every
            # group a share names exists here.
            return _Result(rows=[(str(gid),) for gid in _named_ids(params)])

        # GroupMember lookups (get_user_group_ids) - this admin user belongs
        # to no groups; empty is fine since is_admin bypasses visibility.
        return _Result(rows=[])


def _build_client(fake_db, fake_user, monkeypatch, *, group_ids: list[str] | None = None):
    """Build a TestClient for a given (possibly non-admin) actor."""
    return TestClient(_build_app(fake_db, fake_user, monkeypatch, group_ids=group_ids))


def _build_app(fake_db, fake_user, monkeypatch, *, group_ids: list[str] | None = None):
    """The integrations router, acting as *fake_user* on *fake_db*.

    ``group_ids`` stubs ``get_user_group_ids`` directly - the fake DB's
    Integration-only ``execute`` doesn't model GroupMember rows, so this is
    simpler than teaching it a second query shape just for this actor's
    group memberships.
    """

    async def _get_db():
        yield fake_db

    async def _audit(*_a, **_k):
        return None

    monkeypatch.setattr(integrations_api, "log_audit", _audit)
    if group_ids is not None:
        async def _group_ids(*_a, **_k):
            return group_ids

        monkeypatch.setattr(integrations_api, "get_user_group_ids", _group_ids)

    app = FastAPI()
    app.include_router(integrations_api.router, prefix="/api/v1/integrations")
    app.dependency_overrides[deps.get_current_user] = lambda: fake_user
    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[require_agentic_ai] = lambda: None
    app.dependency_overrides[deps.require_agentic_ai_licensed] = lambda: None
    return app


@pytest.fixture
def oauth_guard_client(monkeypatch):
    fake_db = _FakeDb()
    fake_user = SimpleNamespace(
        id=uuid.uuid4(),
        full_name="Admin",
        role="admin",
        is_admin=True,
        oauth_share_dismissed_integration_ids=None,
    )
    with _build_client(fake_db, fake_user, monkeypatch) as client:
        yield client, fake_db, fake_user


def _make_integration(**overrides) -> Integration:
    now = datetime.now(UTC)
    defaults = dict(
        id=uuid.uuid4(),
        provider="github",
        auth_type="api_key",
        name="GitHub",
        is_company_managed=False,
        is_builtin=False,
        shared_group_ids=None,
        shared_group_permissions=None,
        tags=[],
        status="connected",
        client_id_enc=None,
        access_token_enc=None,
        refresh_token_enc=None,
        oauth_share_acknowledged=False,
        created_at=now,
        updated_at=now,
    )
    defaults.update(overrides)
    return Integration(**defaults)


def _connected_oauth(user_id, **overrides) -> Integration:
    """A personal OAuth integration holding its owner's token."""
    return _make_integration(
        user_id=user_id,
        auth_type="oauth",
        access_token_enc="secret-token",
        refresh_token_enc="secret-refresh",
        **overrides,
    )


#: What the UI sends after its consent notice: sharing is an explicit choice.
_SHARE = {"keep_oauth_credentials_on_share": True}


def _assert_shares_the_token(integ: Integration) -> None:
    assert integ.access_token_enc == "secret-token"
    assert integ.refresh_token_enc == "secret-refresh"
    assert integ.status == "connected"
    assert integ.oauth_share_acknowledged is True


def _assert_withholds_the_token(integ: Integration) -> None:
    """Shared without the connected account: the owner keeps their token
    and their connection, nobody else may use it, and everyone else
    authorizes with their own account (``credential_mode = "per_user"``)."""
    assert integ.access_token_enc == "secret-token"
    assert integ.refresh_token_enc == "secret-refresh"
    assert integ.oauth_share_acknowledged is False
    assert integ.credential_mode == "per_user"
    assert not oauth_credentials_usable(integ, uuid.uuid4())


# ---------------------------------------------------------------------------
# Sharing keeps the token and records the consent, whatever the kind of share
# ---------------------------------------------------------------------------


def test_sharing_with_a_group_shares_the_connected_account(oauth_guard_client):
    client, db, user = oauth_guard_client
    integ = _connected_oauth(user.id)
    db.integrations[str(integ.id)] = integ

    resp = client.put(
        f"/api/v1/integrations/{integ.id}",
        json={"id": str(integ.id), "shared_group_ids": [str(uuid.uuid4())], **_SHARE},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["oauth_share_acknowledged"] is True
    assert db.integrations[str(integ.id)].shared_group_ids
    _assert_shares_the_token(db.integrations[str(integ.id)])


def test_sharing_with_a_user_shares_the_connected_account(oauth_guard_client):
    client, db, user = oauth_guard_client
    grantee = str(uuid.uuid4())
    db.active_user_ids.add(grantee)
    integ = _connected_oauth(user.id)
    db.integrations[str(integ.id)] = integ

    resp = client.put(
        f"/api/v1/integrations/{integ.id}",
        json={"id": str(integ.id), "shared_user_ids": [grantee], **_SHARE},
    )

    assert resp.status_code == 200, resp.text
    assert db.integrations[str(integ.id)].shared_user_ids == [grantee]
    _assert_shares_the_token(db.integrations[str(integ.id)])


def test_making_it_company_managed_shares_the_connected_account(oauth_guard_client):
    client, db, user = oauth_guard_client
    integ = _connected_oauth(user.id)
    db.integrations[str(integ.id)] = integ

    resp = client.put(
        f"/api/v1/integrations/{integ.id}",
        json={"id": str(integ.id), "is_company_managed": True, **_SHARE},
    )

    assert resp.status_code == 200, resp.text
    assert db.integrations[str(integ.id)].is_company_managed is True
    _assert_shares_the_token(db.integrations[str(integ.id)])


def test_shared_group_permissions_alone_shares_the_connected_account(oauth_guard_client):
    """A partial update touching only shared_group_permissions still settles
    the consent against the integration's effective (already shared) state."""
    client, db, user = oauth_guard_client
    integ = _connected_oauth(user.id, shared_group_ids=[_GROUP])
    db.integrations[str(integ.id)] = integ

    resp = client.put(
        f"/api/v1/integrations/{integ.id}",
        json={
            "id": str(integ.id),
            "shared_group_permissions": {_GROUP: "read_only"},
            **_SHARE,
        },
    )

    assert resp.status_code == 200, resp.text
    assert db.integrations[str(integ.id)].shared_group_permissions
    _assert_shares_the_token(db.integrations[str(integ.id)])


def test_switching_auth_type_to_oauth_while_company_managed_shares_the_token(
    oauth_guard_client,
):
    client, db, user = oauth_guard_client
    integ = _make_integration(
        user_id=user.id,
        is_company_managed=True,
        access_token_enc="secret-token",
        refresh_token_enc="secret-refresh",
    )
    db.integrations[str(integ.id)] = integ

    resp = client.put(
        f"/api/v1/integrations/{integ.id}",
        json={"id": str(integ.id), "auth_type": "oauth", **_SHARE},
    )

    assert resp.status_code == 200, resp.text
    updated = db.integrations[str(integ.id)]
    assert updated.auth_type == "oauth"
    assert updated.is_company_managed is True
    _assert_shares_the_token(updated)


def test_switching_away_from_oauth_forgets_who_connected_the_token(oauth_guard_client):
    """The token goes with the switch, and so does the record of who connected
    it: nothing is left to report or to carry into a later OAuth."""
    client, db, user = oauth_guard_client
    integ = _connected_oauth(
        user.id, shared_group_ids=["g"], oauth_share_acknowledged=True,
        oauth_token_owner_id=uuid.uuid4(),
    )
    db.integrations[str(integ.id)] = integ

    resp = client.put(
        f"/api/v1/integrations/{integ.id}",
        json={"id": str(integ.id), "auth_type": "api_key"},
    )

    assert resp.status_code == 200, resp.text
    updated = db.integrations[str(integ.id)]
    assert updated.access_token_enc is None
    assert updated.oauth_token_owner_id is None


def test_worked_on_main_the_explicit_opt_in_shares_the_token(oauth_guard_client):
    """A client sending ``true`` (the opt-in, what the UI sends after its
    consent notice) shares the connected account."""
    client, db, user = oauth_guard_client
    integ = _connected_oauth(user.id)
    db.integrations[str(integ.id)] = integ

    resp = client.put(
        f"/api/v1/integrations/{integ.id}",
        json={
            "id": str(integ.id),
            "shared_group_ids": [str(uuid.uuid4())],
            "keep_oauth_credentials_on_share": True,
        },
    )

    assert resp.status_code == 200, resp.text
    _assert_shares_the_token(db.integrations[str(integ.id)])


@pytest.mark.parametrize(
    "extra",
    [{}, {"keep_oauth_credentials_on_share": None}],
    ids=["omitted", "explicit_null"],
)
@pytest.mark.parametrize(
    "sharing",
    [
        {"shared_group_ids": [_GROUP]},
        {"shared_user_ids": ["u-1"]},
        {"is_company_managed": True},
    ],
    ids=["group", "person", "company_managed"],
)
def test_worked_on_main_a_share_that_makes_no_choice_keeps_the_token_private(
    oauth_guard_client, sharing, extra
):
    """An API client that shares an OAuth integration and
    says nothing about the connected account gets the baseline meaning - the
    token is not shared (it is removed, as a share always did)."""
    client, db, user = oauth_guard_client
    if "shared_user_ids" in sharing:
        grantee = str(uuid.uuid4())
        db.active_user_ids.add(grantee)
        sharing = {"shared_user_ids": [grantee]}
    integ = _connected_oauth(user.id)
    db.integrations[str(integ.id)] = integ

    resp = client.put(
        f"/api/v1/integrations/{integ.id}",
        json={"id": str(integ.id), **sharing, **extra},
    )

    assert resp.status_code == 200, resp.text
    _assert_withholds_the_token(db.integrations[str(integ.id)])


def test_an_omitted_choice_keeps_an_account_already_shared(oauth_guard_client):
    """Widening the share of an account whose consent is recorded keeps it
    shared, as on main: only an explicit ``false`` withdraws it."""
    client, db, user = oauth_guard_client
    integ = _connected_oauth(
        user.id, shared_group_ids=[str(uuid.uuid4())], oauth_share_acknowledged=True
    )
    db.integrations[str(integ.id)] = integ

    resp = client.put(
        f"/api/v1/integrations/{integ.id}",
        json={"id": str(integ.id), "shared_group_ids": [str(uuid.uuid4()), str(uuid.uuid4())]},
    )

    assert resp.status_code == 200, resp.text
    _assert_shares_the_token(db.integrations[str(integ.id)])


@pytest.fixture
def _creatable(monkeypatch):
    """What ``create_integration`` reads before it writes, stubbed."""
    import app.api.v1.platform_settings_helpers as platform_settings_helpers

    async def _catalog_entry(_db, _provider):
        return {"auth_type": "oauth", "supported_auth_types": ["oauth"]}

    async def _platform_settings(_db):
        return SimpleNamespace(disabled_integration_providers=[])

    monkeypatch.setattr(integrations_api, "_get_provider_catalog_entry", _catalog_entry)
    monkeypatch.setattr(platform_settings_helpers, "get_platform_settings", _platform_settings)


@pytest.mark.parametrize(
    ("sharing", "choice", "acknowledged"),
    [
        pytest.param({"is_company_managed": True}, True, True, id="company_managed"),
        pytest.param({"shared_group_ids": [_GROUP]}, True, True, id="group_share"),
        pytest.param({"is_company_managed": True}, None, False, id="company_managed_no_choice"),
        pytest.param({"shared_group_ids": [_GROUP]}, None, False, id="group_share_no_choice"),
        pytest.param({"is_company_managed": True}, False, False, id="opted_out"),
        pytest.param({}, None, False, id="personal"),
    ],
)
def test_creating_an_oauth_integration_settles_the_consent(
    oauth_guard_client, _creatable, sharing, choice, acknowledged
):
    client, db, _user = oauth_guard_client

    resp = client.post(
        "/api/v1/integrations",
        json={
            "provider": "github",
            "name": "GitHub",
            "client_id": "client-id",
            "client_secret": "client-secret",
            "keep_oauth_credentials_on_share": choice,
            **sharing,
        },
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["oauth_share_acknowledged"] is acknowledged
    assert body["status"] == "configured"
    assert db.integrations[body["id"]].oauth_share_acknowledged is acknowledged


# ---------------------------------------------------------------------------
# The opt-out: shared, without the owner's token
# ---------------------------------------------------------------------------


def test_the_opt_out_shares_the_integration_without_the_token(oauth_guard_client):
    client, db, user = oauth_guard_client
    integ = _connected_oauth(user.id)
    db.integrations[str(integ.id)] = integ

    resp = client.put(
        f"/api/v1/integrations/{integ.id}",
        json={
            "id": str(integ.id),
            "shared_group_ids": [str(uuid.uuid4())],
            "keep_oauth_credentials_on_share": False,
        },
    )

    assert resp.status_code == 200, resp.text
    updated = db.integrations[str(integ.id)]
    assert updated.shared_group_ids
    _assert_withholds_the_token(updated)
    # The owner's connection stays: the integration says it is shared without it.
    assert resp.json()["has_access_token"] is True
    assert resp.json()["credential_mode"] == "per_user"


def test_the_opt_out_alone_withholds_an_already_shared_token(oauth_guard_client):
    """Turning the opt-out on without touching any sharing field still
    withholds the account: the choice is honoured on its own."""
    client, db, user = oauth_guard_client
    integ = _connected_oauth(
        user.id, shared_group_ids=[str(uuid.uuid4())], oauth_share_acknowledged=True
    )
    db.integrations[str(integ.id)] = integ

    resp = client.put(
        f"/api/v1/integrations/{integ.id}",
        json={"id": str(integ.id), "keep_oauth_credentials_on_share": False},
    )

    assert resp.status_code == 200, resp.text
    _assert_withholds_the_token(db.integrations[str(integ.id)])


def test_the_opt_out_when_switching_to_oauth_on_a_company_managed_integration(
    oauth_guard_client,
):
    client, db, user = oauth_guard_client
    integ = _make_integration(
        user_id=user.id,
        is_company_managed=True,
        access_token_enc="secret-token",
        refresh_token_enc="secret-refresh",
    )
    db.integrations[str(integ.id)] = integ

    resp = client.put(
        f"/api/v1/integrations/{integ.id}",
        json={
            "id": str(integ.id),
            "auth_type": "oauth",
            "keep_oauth_credentials_on_share": False,
        },
    )

    assert resp.status_code == 200, resp.text
    _assert_withholds_the_token(db.integrations[str(integ.id)])


def test_an_opted_out_integration_stays_opted_out_on_an_unrelated_edit(oauth_guard_client):
    """A shared integration whose owner opted out holds no token: a rename
    neither records a consent nobody gave nor brings anything back."""
    client, db, user = oauth_guard_client
    integ = _make_integration(
        user_id=user.id,
        auth_type="oauth",
        shared_group_ids=[str(uuid.uuid4())],
        status="disconnected",
        oauth_share_acknowledged=False,
    )
    db.integrations[str(integ.id)] = integ

    resp = client.put(
        f"/api/v1/integrations/{integ.id}",
        json={"id": str(integ.id), "name": "GitHub (renamed)"},
    )

    assert resp.status_code == 200, resp.text
    updated = db.integrations[str(integ.id)]
    assert updated.name == "GitHub (renamed)"
    assert updated.oauth_share_acknowledged is False
    assert updated.access_token_enc is None


# ---------------------------------------------------------------------------
# Edits that do not opt out never remove a token
# ---------------------------------------------------------------------------


def test_a_legacy_unacknowledged_shared_row_is_left_as_it_is_on_an_unrelated_edit(
    oauth_guard_client,
):
    """A shared row holding a token nobody recorded consent for (shared before
    the consent existed, or connected through a path that skips it) used to be
    stripped by the next edit, even a rename. A rename neither shares it
    (only an explicit ``true`` does) nor strips it."""
    client, db, user = oauth_guard_client
    integ = _connected_oauth(
        user.id, shared_group_ids=[str(uuid.uuid4())], oauth_share_acknowledged=False
    )
    db.integrations[str(integ.id)] = integ

    resp = client.put(
        f"/api/v1/integrations/{integ.id}",
        json={"id": str(integ.id), "name": "GitHub (renamed)"},
    )

    assert resp.status_code == 200, resp.text
    updated = db.integrations[str(integ.id)]
    assert updated.name == "GitHub (renamed)"
    assert updated.access_token_enc == "secret-token"
    assert updated.refresh_token_enc == "secret-refresh"
    assert updated.oauth_share_acknowledged is False


def test_acknowledged_shared_row_survives_unrelated_edit(oauth_guard_client):
    client, db, user = oauth_guard_client
    integ = _connected_oauth(
        user.id, shared_group_ids=[str(uuid.uuid4())], oauth_share_acknowledged=True
    )
    db.integrations[str(integ.id)] = integ

    resp = client.put(
        f"/api/v1/integrations/{integ.id}",
        json={"id": str(integ.id), "name": "GitHub (renamed)"},
    )

    assert resp.status_code == 200, resp.text
    updated = db.integrations[str(integ.id)]
    assert updated.name == "GitHub (renamed)"
    _assert_shares_the_token(updated)


def test_a_config_tier_member_s_edit_never_removes_the_owner_s_token(monkeypatch):
    """A config-tier group member edits a shared integration like any editor:
    on an integration whose connected account is already shared, an edit
    echoing ``true`` is accepted and keeps the token. Turning that consent on
    is the account holder's alone (``test_share_consent_gate.py``)."""
    owner = SimpleNamespace(id=uuid.uuid4(), full_name="Owner", role="user", is_admin=False)
    editor = SimpleNamespace(
        id=uuid.uuid4(),
        full_name="Config Editor",
        role="user",
        is_admin=False,
        oauth_share_dismissed_integration_ids=None,
    )
    group_id = str(uuid.uuid4())

    fake_db = _FakeDb()
    integ = _connected_oauth(
        owner.id,
        shared_group_ids=[group_id],
        shared_group_permissions={group_id: "config"},
        oauth_share_acknowledged=True,
    )
    fake_db.integrations[str(integ.id)] = integ

    with _build_client(fake_db, editor, monkeypatch, group_ids=[group_id]) as client:
        resp = client.put(
            f"/api/v1/integrations/{integ.id}",
            json={
                "id": str(integ.id),
                "name": "Renamed by config-tier editor",
                "keep_oauth_credentials_on_share": True,
            },
        )

    assert resp.status_code == 200, resp.text
    updated = fake_db.integrations[str(integ.id)]
    assert updated.name == "Renamed by config-tier editor"
    _assert_shares_the_token(updated)


# ---------------------------------------------------------------------------
# Un-sharing makes the integration personal again
# ---------------------------------------------------------------------------


def test_unsharing_makes_the_integration_personal_again(oauth_guard_client):
    """The token stays - it is the owner's own again - and the consent is gone,
    so sharing it later asks again."""
    client, db, user = oauth_guard_client
    integ = _connected_oauth(
        user.id, shared_group_ids=[str(uuid.uuid4())], oauth_share_acknowledged=True
    )
    db.integrations[str(integ.id)] = integ

    resp = client.put(
        f"/api/v1/integrations/{integ.id}",
        json={"id": str(integ.id), "shared_group_ids": None},
    )

    assert resp.status_code == 200, resp.text
    updated = db.integrations[str(integ.id)]
    assert not updated.shared_group_ids
    assert updated.oauth_share_acknowledged is False
    assert updated.access_token_enc == "secret-token"


def test_auth_type_switch_to_oauth_when_disabling_company_managed_together(
    oauth_guard_client,
):
    """A single request that both disables company-managed AND switches to
    oauth ends up private, so nothing is removed or acknowledged."""
    client, db, user = oauth_guard_client
    integ = _make_integration(user_id=user.id, is_company_managed=True)
    db.integrations[str(integ.id)] = integ

    resp = client.put(
        f"/api/v1/integrations/{integ.id}",
        json={
            "id": str(integ.id),
            "auth_type": "oauth",
            "is_company_managed": False,
        },
    )

    assert resp.status_code == 200, resp.text
    updated = db.integrations[str(integ.id)]
    assert updated.auth_type == "oauth"
    assert updated.is_company_managed is False
    assert updated.oauth_share_acknowledged is False
