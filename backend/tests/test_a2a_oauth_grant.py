"""The partner grant issues tokens for BOTH credential shapes.

An ``oauth_a2a`` integration's credential carries ``a2a_integration_id`` and
no ``a2a_exposure_id`` — the shape that exists because one partner
relationship now reaches every agent that binds it. The grant refused
exactly that shape when it landed, so the partner could never mint a token
and every exchange answered 401 with nothing in the log to explain it.

The invariant both shapes share: a credential stays mintable only while it
addresses a surface that still answers, resolved through the same
``_resolvable_exposures`` filter the task surface uses.
"""

import json
import uuid

import pytest

from app.a2a import grants


class _Client:
    def __init__(self, *, exposure_id=None, integration_id=None):
        self.client_id = "a2a-test"
        self.a2a_exposure_id = exposure_id
        self.a2a_integration_id = integration_id


class _Exposure:
    def __init__(self):
        self.id = uuid.uuid4()
        self.run_as_user_id = uuid.uuid4()


@pytest.fixture
def grant(monkeypatch):
    """Authenticate the client successfully; each test scripts the rest."""
    state = {"client": None, "reachable": [], "exposure": None, "owner": None}

    async def _auth(_db, _cid, _secret, grant_type=None):
        return state["client"]

    async def _reachable(_db, _iid, _provider):
        return state["reachable"]

    async def _one(_db, _eid):
        return state["exposure"]

    async def _owner(_db, _iid):
        return state["owner"]

    import app.api.v1.mcp_oauth as oauth_mod
    from app.a2a import exposure as expo_mod

    monkeypatch.setattr(oauth_mod, "authenticate_client", _auth)
    monkeypatch.setattr(
        expo_mod, "resolvable_exposures_for_integration", _reachable
    )
    monkeypatch.setattr(expo_mod, "get_resolvable_exposure", _one)
    monkeypatch.setattr(grants, "_integration_owner", _owner)
    return state


async def _exchange():
    return await grants.handle_client_credentials(
        {"client_id": "a2a-test", "client_secret": "s"}, None
    )


def _claims(resp):
    import jwt as pyjwt

    from app.core.config import settings

    body = json.loads(resp.body)
    return pyjwt.decode(
        body["access_token"],
        settings.secret_key,
        algorithms=[settings.jwt_algorithm],
    )


class TestIntegrationScopedCredential:
    async def test_mints_while_any_bound_agent_is_live(self, grant):
        """One credential reaches every agent binding the integration, so it
        stays mintable while ANY of them resolves."""
        integration_id = uuid.uuid4()
        owner = uuid.uuid4()
        grant["client"] = _Client(integration_id=integration_id)
        grant["reachable"] = [_Exposure()]
        grant["owner"] = owner

        resp = await _exchange()
        assert resp.status_code == 200
        claims = _claims(resp)
        # Confinement claims travel together or the token reopens a surface.
        assert claims["type"] == "a2a_client"
        assert claims["scope"] == "a2a:task"
        # ``sub`` is attribution — the human owning the partner relationship.
        # Execution runs as each exposure's OWN run-as user, never this.
        assert claims["sub"] == str(owner)

    async def test_company_managed_integration_falls_back_to_a_run_as_user(
        self, grant
    ):
        """A company-managed integration has no owner, and ``sub`` must still
        be a real user rather than an empty claim."""
        exposure = _Exposure()
        grant["client"] = _Client(integration_id=uuid.uuid4())
        grant["reachable"] = [exposure]
        grant["owner"] = None

        claims = _claims(await _exchange())
        assert claims["sub"] == str(exposure.run_as_user_id)

    async def test_refused_when_it_reaches_no_live_exposure(self, grant):
        """Unbound from every agent (or every run-as user deactivated): the
        credential must stop being mintable at the same moment the surface
        stops answering."""
        grant["client"] = _Client(integration_id=uuid.uuid4())
        grant["reachable"] = []

        resp = await _exchange()
        assert resp.status_code == 401
        assert json.loads(resp.body)["error"] == "invalid_client"


class TestExposureScopedCredential:
    async def test_legacy_shape_still_mints(self, grant):
        """Credentials minted by the older per-agent flow are still in
        partners' hands; this grant must keep honouring them."""
        exposure = _Exposure()
        grant["client"] = _Client(exposure_id=exposure.id)
        grant["exposure"] = exposure

        claims = _claims(await _exchange())
        assert claims["sub"] == str(exposure.run_as_user_id)

    async def test_revoked_exposure_stops_minting(self, grant):
        grant["client"] = _Client(exposure_id=uuid.uuid4())
        grant["exposure"] = None
        assert (await _exchange()).status_code == 401


class TestNonPartnerClient:
    async def test_a_dcr_client_is_refused(self, grant):
        """A partner-registered MCP client asking for this grant is refused:
        different lifecycle, and it addresses no exposure at all."""
        grant["client"] = _Client()
        resp = await _exchange()
        assert resp.status_code == 401
        assert json.loads(resp.body)["error"] == "invalid_client"

    async def test_missing_credentials_are_a_bad_request(self, grant):
        resp = await grants.handle_client_credentials({}, None)
        assert resp.status_code == 400
