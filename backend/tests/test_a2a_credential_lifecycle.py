"""Who may mint an A2A partner credential, and when.

Two rules, both of which destroy a working configuration when absent:

* minting is OPERATOR-DRIVEN. A secret minted at create time is returned by
  no response — create answers with the integration, not the credential — so
  minting is driven by the operator from the Config tab, which is the only
  surface that can show the secret once.

* minting is INBOUND-ONLY. An outbound registration presents a credential the
  PARTNER issued; ``mint_credential`` writes its client id into
  ``extra_config``, which is exactly where the partner-issued one lives, so one
  click on a route gated by provider alone would silently replace a working
  outbound credential with a meaningless inbound one.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1 import integrations as integrations_module
from app.models.a2a import A2AIntegrationSettings


class _Db:
    async def execute(self, _statement):
        # The current client id read before the mint (#105): none yet.
        return SimpleNamespace(scalar_one_or_none=lambda: None)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None


def _integration(provider="oauth_a2a"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="Partner",
        provider=provider,
        is_company_managed=False,
        extra_config_enc=None,
    )


@pytest.fixture
def route(monkeypatch):
    """Call the regenerate route for real, with only its collaborators faked.

    Returns a callable taking the settings row to be found, and reporting
    whether ``mint_credential`` was reached.
    """
    minted: dict[str, bool] = {"called": False}

    async def _mint(_db, integration):
        minted["called"] = True
        return "a2a-new", "secret-shown-once"

    async def _groups(_db, _uid):
        return []

    async def _audit(*_a, **_kw):
        return None

    import app.a2a.providers as providers_module
    import app.integrations.providers.oauth_a2a as oauth_a2a_module
    from tests.a2a_chat_gate import stub_chat_gate

    stub_chat_gate(monkeypatch)
    monkeypatch.setattr(oauth_a2a_module, "mint_credential", _mint)
    monkeypatch.setattr(integrations_module, "get_user_group_ids", _groups)
    monkeypatch.setattr(integrations_module, "log_audit", _audit)
    monkeypatch.setattr(
        integrations_module, "require_admin_for_company_managed", lambda *_a, **_k: None
    )
    monkeypatch.setattr(integrations_module, "can_manage", lambda *_a, **_k: True)

    async def _run(settings_row, *, provider="oauth_a2a"):
        integration = _integration(provider)

        async def _get_integration(*_a, **_k):
            return integration

        async def _get_settings(_db, _id):
            return settings_row

        monkeypatch.setattr(
            integrations_module, "get_user_integration", _get_integration
        )
        monkeypatch.setattr(providers_module, "get_settings", _get_settings)
        minted["called"] = False
        result = await integrations_module.regenerate_a2a_credential(
            str(integration.id),
            SimpleNamespace(client=None, headers={}),
            user=SimpleNamespace(id=uuid.uuid4(), email="op@x", is_admin=True),
            db=_Db(),
        )
        return result, minted["called"]

    # Exposed so a test can examine the flag when the call raises and the
    # return value above is swallowed by ``pytest.raises``.
    _run.minted = minted
    return _run


def _row(direction: str):
    return A2AIntegrationSettings(integration_id=uuid.uuid4(), direction=direction)


class TestMintingIsInboundOnly:
    async def test_an_inbound_registration_mints(self, route):
        result, called = await route(_row("inbound"))
        assert called
        # The secret is returned HERE and nowhere else.
        assert result["client_secret"] == "secret-shown-once"

    async def test_an_outbound_registration_is_refused(self, route):
        with pytest.raises(HTTPException) as exc:
            await route(_row("outbound"))
        assert exc.value.status_code == 422
        assert "outbound" in str(exc.value.detail).lower()

    async def test_the_refusal_happens_before_anything_is_written(self, route):
        # The point of the gate. `mint_credential` overwrites
        # extra_config["client_id"] — the partner-issued one — so reaching it
        # at all on an outbound registration has already done the damage.
        with pytest.raises(HTTPException):
            await route(_row("outbound"))
        assert route.minted["called"] is False, (
            "the mint must not be reached on an outbound registration"
        )
        # And the probe is live, not vacuous: the same fixture does reach the
        # mint when the direction allows it.
        _, called = await route(_row("inbound"))
        assert called, "sanity: the fixture does reach the mint when allowed"

    async def test_a_registration_with_no_settings_row_is_refused(self, route):
        # Fail closed, like every other direction decision: no row means
        # neither direction, and minting would pick one on the operator's
        # behalf.
        with pytest.raises(HTTPException) as exc:
            await route(None)
        assert exc.value.status_code == 422

