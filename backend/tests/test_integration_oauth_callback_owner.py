"""The stored OAuth token always belongs to the integration's ``user_id``.

The personal OAuth token rule (``integration_credentials``) trusts
``Integration.user_id`` as the owner of ``access_token_enc``. Two ways used
to break that: an admin re-authorizing a colleague's company-managed
integration left the admin's token under the colleague's name (still carrying
the colleague's share consent), and anyone who could see a personal integration
could start its authorization. The state-less GitHub App install callback
stores no token at all: ``test_github_app_install_callback_owner.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import app.api.oauth_callbacks as callbacks
import app.api.v1.integrations as integrations_api
from app.models.integration import Integration
from app.models.user import User
from tests.integration_credential_fakes import (
    COLLEAGUE_ID,
    OWNER_ID,
    FakeSession,
    make_integration,
)


def _user(user_id, *, is_admin: bool) -> User:
    return User(
        id=user_id,
        email=f"{user_id}@example.com",
        full_name="u",
        role="admin" if is_admin else "user",
        oauth_share_dismissed_integration_ids=[],
    )


class _Session(FakeSession):
    """FakeSession plus ``get`` for the users the callback loads."""

    def __init__(self, integrations, users):
        super().__init__({Integration: list(integrations)})
        self.users = {u.id: u for u in users}

    async def get(self, _model, key):
        return self.users.get(key)


# ---------------------------------------------------------------------------
# Who may start an authorization
# ---------------------------------------------------------------------------


@pytest.fixture
def _authorize(monkeypatch):
    build_url = AsyncMock(return_value="https://provider.example/authorize")
    monkeypatch.setattr("app.integrations.oauth.build_authorize_url_for_integration", build_url)
    monkeypatch.setattr(integrations_api, "get_user_group_ids", AsyncMock(return_value=[]))

    async def _start(integration, user):
        monkeypatch.setattr(
            integrations_api, "get_user_integration", AsyncMock(return_value=integration)
        )
        return await integrations_api.start_oauth(
            integration_id=str(integration.id), user=user, db=SimpleNamespace(flush=AsyncMock())
        )

    return _start, build_url


@pytest.mark.parametrize(
    ("overrides", "is_admin"),
    [
        pytest.param({}, True, id="admin_on_a_colleague_s_private_integration"),
        pytest.param({"shared_group_ids": ["g"]}, False, id="group_member"),
    ],
)
async def test_only_the_owner_authorizes_a_personal_integration(_authorize, overrides, is_admin):
    start, build_url = _authorize
    integration = make_integration(client_id_enc="enc-client", **overrides)

    with pytest.raises(HTTPException) as exc:
        await start(integration, _user(COLLEAGUE_ID, is_admin=is_admin))

    assert exc.value.status_code == 403
    build_url.assert_not_awaited()


async def test_the_owner_authorizes_their_personal_integration(_authorize):
    start, _ = _authorize
    integration = make_integration(client_id_enc="enc-client")

    result = await start(integration, _user(OWNER_ID, is_admin=False))

    assert result == {"authorize_url": "https://provider.example/authorize"}


async def test_any_admin_may_authorize_a_company_managed_integration(_authorize):
    start, _ = _authorize
    integration = make_integration(client_id_enc="enc-client", is_company_managed=True)

    result = await start(integration, _user(COLLEAGUE_ID, is_admin=True))

    assert result["authorize_url"]


# ---------------------------------------------------------------------------
# The callback records whose token it stores
# ---------------------------------------------------------------------------


@pytest.fixture
def _callback(monkeypatch):
    audits: list[dict] = []

    async def _audit(_db, **kwargs):
        audits.append(kwargs)

    monkeypatch.setattr(callbacks, "log_audit", _audit)
    monkeypatch.setattr(callbacks, "encrypt_value", lambda value: f"enc:{value}")
    monkeypatch.setattr(
        callbacks,
        "exchange_code_for_tokens_with_integration",
        AsyncMock(return_value={"access_token": "new-token", "refresh_token": "new-refresh"}),
    )

    async def _run(integration, authorizing_user, *users):
        db = _Session([integration], [authorizing_user, *users])
        state = {
            "integration_id": str(integration.id),
            "user_id": str(authorizing_user.id),
            "provider": integration.provider,
        }
        await callbacks._handle_integration_callback(state, "code", db)
        return audits

    return _run


async def test_a_company_managed_reauthorization_moves_the_credential_and_its_consent(
    _callback,
):
    integration = make_integration(is_company_managed=True, oauth_share_acknowledged=True)
    previous_owner = _user(OWNER_ID, is_admin=True)
    previous_owner.oauth_share_dismissed_integration_ids = [str(integration.id)]
    admin = _user(COLLEAGUE_ID, is_admin=True)

    audits = await _callback(integration, admin, previous_owner)

    assert integration.user_id == COLLEAGUE_ID
    assert integration.access_token_enc == "enc:new-token"
    # The previous owner's consent covered their token; the flag now records
    # the admin's own, given by authorizing the company-managed integration.
    assert integration.oauth_share_acknowledged is True
    assert previous_owner.oauth_share_dismissed_integration_ids == []
    (audit,) = [a for a in audits if a["action"] == "update"]
    assert audit["user"] is admin
    assert audit["details"] == {
        "previous_owner_id": str(OWNER_ID),
        "new_owner_id": str(COLLEAGUE_ID),
        "previous_owner_consent_dropped": True,
    }


async def test_the_owner_reauthorizing_keeps_ownership_and_consent(_callback):
    integration = make_integration(is_company_managed=True, oauth_share_acknowledged=True)
    owner = _user(OWNER_ID, is_admin=True)

    audits = await _callback(integration, owner)

    assert integration.user_id == OWNER_ID
    assert integration.oauth_share_acknowledged is True
    assert integration.access_token_enc == "enc:new-token"
    # Only the connect entry (test_integration_granted_scopes.py), no owner change.
    assert [a["action"] for a in audits] == ["connect"]


async def test_an_ownerless_company_managed_integration_gets_its_authorizer_as_owner(_callback):
    integration = make_integration(user_id=None, is_company_managed=True)
    admin = _user(COLLEAGUE_ID, is_admin=True)

    audits = await _callback(integration, admin)

    assert integration.user_id == COLLEAGUE_ID
    assert audits[0]["details"]["previous_owner_id"] is None


def _no_refresh_token(monkeypatch):
    monkeypatch.setattr(
        callbacks,
        "exchange_code_for_tokens_with_integration",
        AsyncMock(return_value={"access_token": "new-token"}),
    )


async def test_a_reauthorization_that_moves_the_credential_drops_the_old_refresh_token(
    _callback, monkeypatch
):
    """A provider that omits the refresh token must not leave the previous
    owner's one behind: refreshing it would restore their access."""
    _no_refresh_token(monkeypatch)
    integration = make_integration(is_company_managed=True, refresh_token_enc="enc:old-refresh")
    admin = _user(COLLEAGUE_ID, is_admin=True)

    await _callback(integration, admin, _user(OWNER_ID, is_admin=True))

    assert integration.user_id == COLLEAGUE_ID
    assert integration.access_token_enc == "enc:new-token"
    assert integration.refresh_token_enc is None


async def test_the_owner_replacing_a_transferred_token_drops_the_old_refresh_token(
    _callback, monkeypatch
):
    _no_refresh_token(monkeypatch)
    integration = make_integration(
        refresh_token_enc="enc:old-refresh", oauth_token_owner_id=COLLEAGUE_ID
    )

    await _callback(integration, _user(OWNER_ID, is_admin=False))

    assert integration.oauth_token_owner_id is None
    assert integration.refresh_token_enc is None


@pytest.mark.parametrize("company_managed", [False, True], ids=["personal", "company"])
async def test_worked_on_main_the_owner_reauthorizing_keeps_the_refresh_token(
    _callback, monkeypatch, company_managed
):
    """Providers such as Google return a refresh token on the first consent
    only: the owner re-authorizing keeps the one already stored."""
    _no_refresh_token(monkeypatch)
    integration = make_integration(
        is_company_managed=company_managed, refresh_token_enc="enc:old-refresh"
    )

    await _callback(integration, _user(OWNER_ID, is_admin=True))

    assert integration.access_token_enc == "enc:new-token"
    assert integration.refresh_token_enc == "enc:old-refresh"


async def test_worked_on_main_an_ownerless_row_keeps_its_refresh_token(_callback, monkeypatch):
    """Nothing says whose refresh token an owner-less company-managed row
    holds, so a reconnect that sends none keeps it, as on main."""
    _no_refresh_token(monkeypatch)
    integration = make_integration(
        user_id=None, is_company_managed=True, refresh_token_enc="enc:old-refresh"
    )

    await _callback(integration, _user(COLLEAGUE_ID, is_admin=True))

    assert integration.user_id == COLLEAGUE_ID
    assert integration.refresh_token_enc == "enc:old-refresh"


async def test_the_token_s_own_connector_re_authorizing_keeps_their_refresh_token(
    _callback, monkeypatch
):
    """An admin reconnecting the company-managed token they connected
    themselves (recorded as its connector) keeps their own refresh token."""
    _no_refresh_token(monkeypatch)
    integration = make_integration(
        is_company_managed=True,
        oauth_token_owner_id=COLLEAGUE_ID,
        refresh_token_enc="enc:old-refresh",
    )

    await _callback(integration, _user(COLLEAGUE_ID, is_admin=True), _user(OWNER_ID, is_admin=True))

    assert integration.refresh_token_enc == "enc:old-refresh"


async def test_a_new_refresh_token_always_replaces_the_stored_one(_callback):
    integration = make_integration(is_company_managed=True, refresh_token_enc="enc:old-refresh")

    await _callback(integration, _user(COLLEAGUE_ID, is_admin=True), _user(OWNER_ID, is_admin=True))

    assert integration.refresh_token_enc == "enc:new-refresh"


async def test_the_callback_never_writes_a_token_onto_a_colleague_s_personal_integration(
    _callback,
):
    integration = make_integration(access_token_enc="their-token")
    admin = _user(COLLEAGUE_ID, is_admin=True)

    with pytest.raises(HTTPException) as exc:
        await _callback(integration, admin)

    assert exc.value.status_code == 404
    assert integration.access_token_enc == "their-token"
    assert integration.user_id == OWNER_ID
