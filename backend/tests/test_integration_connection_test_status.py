"""The result of an integration's last connectivity test (#11837).

A typed API key is stored, never checked, so it reads "untested" until
``POST /integrations/{id}/test`` runs; the test then stores its result, which
the page follows. A person's own account on a per-user integration says
nothing about the integration's credential and leaves it alone.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.v1 import integrations as integrations_api
from app.api.v1.integrations import (
    CONNECTION_TEST_UNTESTED,
    connection_test_outcome,
    typed_credential_untested,
)
from tests.integration_credential_fakes import FakeSession, make_integration


@pytest.mark.parametrize("auth_type", ["api_key", "bot_token"])
def test_a_typed_secret_is_untested(auth_type):
    assert typed_credential_untested(auth_type, api_key="k", client_secret=None)
    assert typed_credential_untested(auth_type, api_key=None, client_secret="s")


@pytest.mark.parametrize("auth_type", ["oauth", "none", "platform_managed", "client_credentials"])
def test_other_auth_types_are_not_marked(auth_type):
    assert not typed_credential_untested(auth_type, api_key="k", client_secret="s")


def test_no_secret_given_marks_nothing():
    assert not typed_credential_untested("api_key", api_key=None, client_secret=None)
    assert not typed_credential_untested("api_key", api_key="", client_secret="")


@pytest.mark.parametrize(("status", "stored"), [("ok", "ok"), ("info", "ok"), ("error", "error"), (None, "error")])
def test_the_outcome_is_ok_or_error(status, stored):
    assert connection_test_outcome(status) == stored


def _wire(monkeypatch, integration, *, result, holder=None):
    monkeypatch.setattr(integrations_api, "get_user_group_ids", AsyncMock(return_value=[]))
    monkeypatch.setattr(integrations_api, "get_user_integration", AsyncMock(return_value=integration))
    monkeypatch.setattr(integrations_api, "usable_credential_holder", AsyncMock(return_value=holder or integration))
    monkeypatch.setattr(integrations_api, "_test_integration", AsyncMock(return_value=result))


def _user():
    return SimpleNamespace(id="u1", is_admin=True)


@pytest.mark.parametrize(
    ("result", "stored"),
    [({"status": "ok", "message": "fine"}, "ok"), ({"status": "error", "message": "401"}, "error")],
)
async def test_the_test_stores_its_result(monkeypatch, result, stored):
    integration = make_integration(auth_type="api_key")
    integration.connection_test_status = CONNECTION_TEST_UNTESTED
    _wire(monkeypatch, integration, result=result)

    answer = await integrations_api.test_integration(integration_id=str(integration.id), user=_user(), db=FakeSession())

    assert answer == result
    assert integration.connection_test_status == stored


async def test_a_persons_own_account_leaves_the_integration_alone(monkeypatch):
    integration = make_integration(auth_type="api_key")
    integration.connection_test_status = CONNECTION_TEST_UNTESTED
    own = SimpleNamespace(is_user_credential=True)
    _wire(monkeypatch, integration, result={"status": "error", "message": "401"}, holder=own)

    await integrations_api.test_integration(integration_id=str(integration.id), user=_user(), db=FakeSession())

    assert integration.connection_test_status == CONNECTION_TEST_UNTESTED


def test_a_users_integration_row_carries_what_its_status_reads():
    """The user's Objects page shows the same status as the detail page (#11843 UX B1)."""
    from app.api.v1.users import _user_integration_item

    typed = make_integration(provider="shodan", auth_type="api_key", access_token_enc=None, api_key_enc="enc-key")
    typed.connection_test_status = CONNECTION_TEST_UNTESTED
    row = _user_integration_item(typed)
    assert row.status == "connected"
    assert row.connection_test_status == CONNECTION_TEST_UNTESTED
    assert row.has_access_token is False

    oauth = make_integration()
    row = _user_integration_item(oauth)
    assert row.has_access_token is True
    assert row.connection_test_status is None
