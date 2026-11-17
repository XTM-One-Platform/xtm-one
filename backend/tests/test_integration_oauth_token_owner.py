"""Record whose OAuth token an integration holds when it changes hands.

The personal OAuth token rule reads ``Integration.user_id`` as
the owner of the stored token. An admin's owner transfer changes ``user_id``
and keeps the token, so the new owner held someone else's connected account
with nothing saying so. ``oauth_token_owner_id`` now records that person, the
integration page says so, and the owner's own authorization clears it.
Nothing is refused: the integration keeps working as before.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.models.integration import Integration
from app.services.integration_credentials import (
    apply_oauth_share_consent,
    may_consent_to_share,
    oauth_token_owner_differs,
    record_token_owner_on_transfer,
)
from app.services.integration_state import replace_oauth_client_id
from tests.integration_credential_fakes import COLLEAGUE_ID, OWNER_ID, make_integration
from tests.test_integration_oauth_callback_owner import _callback, _user  # noqa: F401
from tests.test_integration_oauth_sharing_guard import (
    _build_client,
    _connected_oauth,
    _FakeDb,
    _make_integration,
    _Result,
)

_THIRD = uuid.uuid4()


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def test_a_transfer_records_the_previous_owner_as_the_token_s_owner():
    integration = make_integration(user_id=COLLEAGUE_ID)

    assert record_token_owner_on_transfer(integration, OWNER_ID) is True
    assert integration.oauth_token_owner_id == OWNER_ID
    assert oauth_token_owner_differs(integration) is True


def test_a_second_transfer_keeps_who_really_connected_it():
    integration = make_integration(user_id=_THIRD, oauth_token_owner_id=OWNER_ID)

    record_token_owner_on_transfer(integration, COLLEAGUE_ID)

    assert integration.oauth_token_owner_id == OWNER_ID


def test_a_transfer_back_to_whoever_connected_it_clears_the_record():
    integration = make_integration(user_id=OWNER_ID, oauth_token_owner_id=OWNER_ID)

    assert record_token_owner_on_transfer(integration, COLLEAGUE_ID) is False
    assert integration.oauth_token_owner_id is None
    assert oauth_token_owner_differs(integration) is False


def test_a_row_holding_no_oauth_token_records_nothing():
    no_token = make_integration(user_id=COLLEAGUE_ID, access_token_enc=None)
    api_key = make_integration(user_id=COLLEAGUE_ID, auth_type="api_key", api_key_enc="k")

    for integration in (no_token, api_key):
        assert record_token_owner_on_transfer(integration, OWNER_ID) is False
        assert integration.oauth_token_owner_id is None


def test_an_ownerless_previous_owner_records_nothing():
    integration = make_integration(user_id=COLLEAGUE_ID, is_company_managed=True)

    assert record_token_owner_on_transfer(integration, None) is False
    assert integration.oauth_token_owner_id is None


def test_a_removed_token_says_nothing_about_its_owner():
    integration = make_integration(
        user_id=COLLEAGUE_ID, oauth_token_owner_id=OWNER_ID, access_token_enc=None
    )

    assert oauth_token_owner_differs(integration) is False


def test_the_share_opt_out_removes_the_token_and_its_record():
    integration = make_integration(
        user_id=COLLEAGUE_ID,
        oauth_token_owner_id=OWNER_ID,
        shared_group_ids=[str(uuid.uuid4())],
        oauth_share_acknowledged=True,
    )

    apply_oauth_share_consent(integration, share_credentials=False, sharing_changed=True)

    assert integration.access_token_enc is None
    assert integration.oauth_token_owner_id is None


def test_a_new_oauth_app_removes_the_token_and_its_record():
    integration = make_integration(
        user_id=COLLEAGUE_ID,
        oauth_token_owner_id=OWNER_ID,
        shared_group_ids=[str(uuid.uuid4())],
    )

    assert replace_oauth_client_id(integration, "the-owner-s-own-app") is True

    assert integration.access_token_enc is None
    assert integration.oauth_token_owner_id is None
    assert may_consent_to_share(integration, OWNER_ID, is_admin=False) is False
    assert may_consent_to_share(integration, COLLEAGUE_ID, is_admin=False) is True


# ---------------------------------------------------------------------------
# The owner transfer route
# ---------------------------------------------------------------------------


class _TransferDb(_FakeDb):
    """The sharing-guard fake, plus the user lookups an owner transfer makes."""

    def __init__(self, names: dict[uuid.UUID, str]):
        super().__init__()
        self.names = names

    async def get(self, _model, key):
        if key in self.names:
            return SimpleNamespace(
                id=key, is_active=True, full_name=self.names[key], email=f"{key}@example.com"
            )
        return None

    async def execute(self, stmt):
        if "users.full_name" in str(stmt):
            wanted = [v for v in stmt.compile().params.values() if isinstance(v, uuid.UUID)]
            return _Result(rows=[self.names[w] for w in wanted if w in self.names])
        return await super().execute(stmt)


def _admin() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        full_name="Admin",
        email="admin@example.com",
        role="admin",
        is_admin=True,
        oauth_share_dismissed_integration_ids=None,
    )


def test_transferring_a_connected_oauth_integration_says_whose_account_it_uses(monkeypatch):
    previous, new_owner = uuid.uuid4(), uuid.uuid4()
    db = _TransferDb({previous: "Alice", new_owner: "Bob"})
    integration = _connected_oauth(previous)
    db.integrations[str(integration.id)] = integration

    with _build_client(db, _admin(), monkeypatch) as client:
        resp = client.put(
            f"/api/v1/integrations/{integration.id}",
            json={"id": str(integration.id), "user_id": str(new_owner)},
        )

    assert resp.status_code == 200, resp.text
    stored = db.integrations[str(integration.id)]
    assert stored.user_id == new_owner
    assert stored.oauth_token_owner_id == previous
    # Nothing is refused or removed: the integration keeps working.
    assert stored.access_token_enc == "secret-token"
    assert stored.status == "connected"
    body = resp.json()
    assert body["oauth_connected_by_other"] is True
    assert body["oauth_token_owner_name"] == "Alice"
    assert body["owner_name"] == "Bob"


def test_transferring_an_api_key_integration_flags_nothing(monkeypatch):
    previous, new_owner = uuid.uuid4(), uuid.uuid4()
    db = _TransferDb({previous: "Alice", new_owner: "Bob"})
    integration = _make_integration(user_id=previous, api_key_enc="k")
    db.integrations[str(integration.id)] = integration

    with _build_client(db, _admin(), monkeypatch) as client:
        resp = client.put(
            f"/api/v1/integrations/{integration.id}",
            json={"id": str(integration.id), "user_id": str(new_owner)},
        )

    assert resp.status_code == 200, resp.text
    assert db.integrations[str(integration.id)].oauth_token_owner_id is None
    assert resp.json()["oauth_connected_by_other"] is False
    assert resp.json()["oauth_token_owner_name"] is None


def test_an_edit_without_a_transfer_leaves_the_record_alone(monkeypatch):
    previous, owner = uuid.uuid4(), uuid.uuid4()
    db = _TransferDb({previous: "Alice", owner: "Bob"})
    integration = _connected_oauth(owner, oauth_token_owner_id=previous)
    db.integrations[str(integration.id)] = integration

    with _build_client(db, _admin(), monkeypatch) as client:
        resp = client.put(
            f"/api/v1/integrations/{integration.id}",
            json={"id": str(integration.id), "name": "Renamed"},
        )

    assert resp.status_code == 200, resp.text
    assert db.integrations[str(integration.id)].oauth_token_owner_id == previous
    assert resp.json()["oauth_connected_by_other"] is True


# ---------------------------------------------------------------------------
# The owner's own authorization clears it
# ---------------------------------------------------------------------------


async def test_the_owner_reconnecting_makes_the_token_their_own(_callback):  # noqa: F811
    integration = make_integration(user_id=OWNER_ID, oauth_token_owner_id=COLLEAGUE_ID)
    assert isinstance(integration, Integration)

    await _callback(integration, _user(OWNER_ID, is_admin=False))

    assert integration.access_token_enc == "enc:new-token"
    assert integration.oauth_token_owner_id is None
