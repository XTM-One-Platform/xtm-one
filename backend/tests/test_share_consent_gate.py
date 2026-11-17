"""Only the account holder or an admin consents to sharing a personal OAuth token.

``oauth_share_acknowledged`` stands for the token owner's consent: once it is
on, everyone the integration is shared with acts with the connected account.
The integration's owner, the person the stored token was issued to
(``oauth_token_owner_id``) and admins may turn it on - by sharing, by an edit
of a shared row holding a token, or by asking explicitly. A ``config`` grantee
edits the integration as before, but never turns the consent on for someone
else: an explicit request is refused with who may give it, an implicit one
(a share, a rename) saves and leaves the consent off.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import app.core.database as database
import app.services.builtin_tools as builtin_tools_module
from app.services.integration_credentials import (
    SHARE_CONSENT_REFUSED_DETAIL,
    ShareConsentRefusedError,
    apply_oauth_share_consent,
    may_consent_to_share,
)
from tests.test_agent_builder_crud_tools import (
    _call,
    _session_factory,
    _stub_manageable_gate,
)
from tests.test_integration_oauth_sharing_guard import (
    _build_client,
    _connected_oauth,
)
from tests.test_integration_oauth_sharing_guard import _FakeDb as _RestDb

OWNER = uuid.uuid4()
TOKEN_OWNER = uuid.uuid4()
EDITOR = uuid.uuid4()
GROUP = str(uuid.uuid4())
OTHER_GROUP = str(uuid.uuid4())


def _row(**overrides) -> SimpleNamespace:
    fields = {
        "auth_type": "oauth",
        "user_id": OWNER,
        "oauth_token_owner_id": None,
        "is_company_managed": False,
        "shared_group_ids": [GROUP],
        "shared_user_ids": None,
        "access_token_enc": "enc-token",
        "refresh_token_enc": "enc-refresh",
        "token_expires_at": None,
        "client_id_enc": "enc-client",
        "status": "connected",
        "oauth_share_acknowledged": False,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


# ---------------------------------------------------------------------------
# Who may consent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("actor", "is_admin", "expected"),
    [
        pytest.param(OWNER, False, True, id="owner"),
        pytest.param(TOKEN_OWNER, False, True, id="recorded_token_owner"),
        pytest.param(EDITOR, True, True, id="admin"),
        pytest.param(EDITOR, False, False, id="config_grantee"),
        pytest.param(None, False, False, id="nobody"),
    ],
)
def test_who_may_consent(actor, is_admin, expected):
    row = _row(oauth_token_owner_id=TOKEN_OWNER)
    assert may_consent_to_share(row, actor, is_admin=is_admin) is expected


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("share_credentials", "sharing_changed"),
    [(None, True), (None, False)],
    ids=["a_share", "an_edit_of_a_shared_row_holding_a_token"],
)
def test_a_grantee_s_implicit_write_never_turns_the_consent_on(
    share_credentials, sharing_changed
):
    row = _row()
    apply_oauth_share_consent(
        row,
        share_credentials=share_credentials,
        sharing_changed=sharing_changed,
        may_consent=False,
    )
    assert row.oauth_share_acknowledged is False
    assert row.access_token_enc == "enc-token"


def test_a_grantee_s_explicit_consent_is_refused_with_who_may_give_it():
    row = _row()
    with pytest.raises(ShareConsentRefusedError) as exc:
        apply_oauth_share_consent(
            row, share_credentials=True, sharing_changed=False, may_consent=False
        )
    assert str(exc.value) == SHARE_CONSENT_REFUSED_DETAIL
    assert "owner" in SHARE_CONSENT_REFUSED_DETAIL and "admin" in SHARE_CONSENT_REFUSED_DETAIL
    assert row.oauth_share_acknowledged is False


def test_worked_on_main_a_grantee_keeps_an_existing_consent_and_may_opt_out():
    """A consent already given stays; the opt-out and un-sharing apply as today."""
    acked = _row(oauth_share_acknowledged=True)
    apply_oauth_share_consent(acked, share_credentials=True, sharing_changed=True, may_consent=False)
    assert acked.oauth_share_acknowledged is True

    opted_out = _row(oauth_share_acknowledged=True)
    apply_oauth_share_consent(
        opted_out, share_credentials=False, sharing_changed=False, may_consent=False
    )
    assert opted_out.oauth_share_acknowledged is False
    # The opt-out keeps the owner's own account, never lent.
    assert opted_out.credential_mode == "per_user"

    unshared = _row(shared_group_ids=None, oauth_share_acknowledged=True)
    apply_oauth_share_consent(unshared, share_credentials=None, sharing_changed=True, may_consent=False)
    assert unshared.oauth_share_acknowledged is False


@pytest.mark.parametrize("share_credentials", [None, True])
def test_worked_on_main_the_account_holder_consents_as_today(share_credentials):
    row = _row()
    apply_oauth_share_consent(row, share_credentials=share_credentials, sharing_changed=True)
    assert row.oauth_share_acknowledged is True


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


def _editor():
    return SimpleNamespace(
        id=EDITOR,
        full_name="Config Editor",
        role="user",
        is_admin=False,
        oauth_share_dismissed_integration_ids=None,
    )


def _shared_row(**overrides):
    return _connected_oauth(
        OWNER,
        shared_group_ids=[GROUP],
        shared_group_permissions={GROUP: "config"},
        **overrides,
    )


def _put(monkeypatch, actor, integ, payload):
    fake_db = _RestDb()
    fake_db.integrations[str(integ.id)] = integ
    with _build_client(fake_db, actor, monkeypatch, group_ids=[GROUP]) as client:
        return client.put(
            f"/api/v1/integrations/{integ.id}", json={"id": str(integ.id), **payload}
        )


def test_rest_a_grantee_asking_for_the_consent_is_refused(monkeypatch):
    integ = _shared_row()
    resp = _put(monkeypatch, _editor(), integ, {"keep_oauth_credentials_on_share": True})
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == SHARE_CONSENT_REFUSED_DETAIL
    assert integ.oauth_share_acknowledged is False


@pytest.mark.parametrize(
    ("payload", "token"),
    [
        pytest.param({"name": "Renamed"}, "secret-token", id="rename"),
        # A share that makes no choice keeps the baseline meaning: the unconsented account is not shared - it stays its
        # owner's, and everyone else authorizes their own.
        pytest.param(
            {"shared_group_ids": [GROUP, OTHER_GROUP]}, "secret-token", id="share_with_another_group"
        ),
    ],
)
def test_rest_a_grantee_s_edit_saves_without_the_consent(monkeypatch, payload, token):
    integ = _shared_row()
    resp = _put(monkeypatch, _editor(), integ, payload)
    assert resp.status_code == 200, resp.text
    assert integ.oauth_share_acknowledged is False
    assert integ.access_token_enc == token


@pytest.mark.parametrize("who", ["owner", "token_owner", "admin"])
def test_worked_on_main_the_account_holder_s_edit_records_the_consent(monkeypatch, who):
    actor = SimpleNamespace(
        id={"owner": OWNER, "token_owner": TOKEN_OWNER, "admin": EDITOR}[who],
        full_name="Actor",
        role="admin" if who == "admin" else "user",
        is_admin=who == "admin",
        oauth_share_dismissed_integration_ids=None,
    )
    integ = _shared_row(oauth_token_owner_id=TOKEN_OWNER)
    resp = _put(monkeypatch, actor, integ, {"name": "Renamed", "keep_oauth_credentials_on_share": True})
    assert resp.status_code == 200, resp.text
    assert integ.oauth_share_acknowledged is True


# ---------------------------------------------------------------------------
# The agent-builder integration tool
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("caller", "expected"),
    [pytest.param(EDITOR, False, id="grantee"), pytest.param(OWNER, True, id="owner")],
)
async def test_a_builder_edit_consents_only_for_the_account_holder(monkeypatch, caller, expected):
    integration = SimpleNamespace(
        id=uuid.uuid4(),
        name="Acme",
        provider="acme",
        auth_type="oauth",
        status="connected",
        user_id=OWNER,
        oauth_token_owner_id=None,
        is_company_managed=False,
        shared_group_ids=[GROUP],
        shared_user_ids=None,
        access_token_enc="ENC(tok)",
        refresh_token_enc="ENC(tok)",
        oauth_share_acknowledged=False,
    )
    _stub_manageable_gate(monkeypatch, entity=integration)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))

    _, executor = builtin_tools_module.build_agent_builder_tool(SimpleNamespace(), str(caller))
    result = await _call(
        executor, "update_integration", integration_id=str(integration.id), name="Acme (renamed)"
    )

    assert not result.is_error, result.content
    assert integration.name == "Acme (renamed)"
    assert integration.access_token_enc == "ENC(tok)"
    assert integration.oauth_share_acknowledged is expected
