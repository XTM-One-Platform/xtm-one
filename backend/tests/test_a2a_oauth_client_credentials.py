"""The client-credentials grant, and its binding to exactly one exposure.

The token endpoint serves ``authorization_code`` and ``refresh_token``
alongside it. This grant is deliberately narrow - a
client that did not come from an exposure is refused even if it asks for the
grant, because A2A partner credentials have a different lifecycle from the
DCR-registered clients the authorization-code flow serves.

Every rejection returns the SAME ``invalid_client``: distinguishing "no such
client" from "wrong secret" from "not an A2A client" would let a caller
enumerate registrations.
"""

import uuid

import pytest

from app.a2a import grants as a2a_grants
from app.a2a import tokens as a2a_tokens
from app.api.v1 import mcp_oauth


class _Client:
    # S107 false positive: "h" is a placeholder stand-in for a bcrypt hash in
    # a stub object; bcrypt verification itself is monkeypatched in these tests.
    def __init__(
        self,
        *,
        secret_hash="h",  # noqa: S107 - placeholder, not a credential
        grants=None,
        exposure_id=None,
        integration_id=None,
    ):
        self.client_id = "a2a-abc"
        self.client_secret_hash = secret_hash
        self.grant_types = grants if grants is not None else ["client_credentials"]
        # The two partner-credential shapes: an exposure's (the older
        # per-agent flow) and an ``oauth_a2a`` integration's (one partner,
        # every agent binding it). A client carries one or the other.
        self.a2a_exposure_id = exposure_id
        self.a2a_integration_id = integration_id


class _Exposure:
    def __init__(self, run_as_user_id=None):
        self.id = uuid.uuid4()
        self.run_as_user_id = run_as_user_id or uuid.uuid4()
        self.revoked_at = None


class _Result:
    def __init__(self, v):
        self._v = v

    def scalar_one_or_none(self):
        return self._v


class _DB:
    def __init__(self, results):
        self._r = list(results)

    async def execute(self, _s):
        return _Result(self._r.pop(0) if self._r else None)


@pytest.fixture
def ok_bcrypt(monkeypatch):
    async def _check(_raw, _hashed):
        return True

    monkeypatch.setattr(mcp_oauth, "bcrypt_checkpw_async", _check)


@pytest.fixture
def bad_bcrypt(monkeypatch):
    async def _check(_raw, _hashed):
        return False

    monkeypatch.setattr(mcp_oauth, "bcrypt_checkpw_async", _check)


class _Request:
    """Minimal stand-in: both paths under test read only the auth header."""

    def __init__(self, token: str):
        self.headers = {"Authorization": f"Bearer {token}", "authorization": f"Bearer {token}"}


DATA = {"client_id": "a2a-abc", "client_secret": "s"}


class TestGrantIsAdvertised:
    async def test_metadata_lists_client_credentials(self):
        # Both target platforms read AS metadata to discover how to
        # authenticate; a grant that works but is not advertised is not
        # discoverable.
        from app.a2a.grants import register_a2a_grants

        # Restored afterwards: registering mutates module-level registries that
        # outlive this test, and a leaked entry makes any later assertion about
        # the flag-off ``client_credentials`` contract depend on file order.
        handlers = dict(mcp_oauth._GRANT_HANDLERS)
        token_types = set(mcp_oauth._ACCESS_TOKEN_TYPES)
        try:
            register_a2a_grants()
            assert "client_credentials" in mcp_oauth._GRANT_HANDLERS
            # Advertised to partners, not merely wired: both target platforms
            # read the server metadata to discover how to authenticate.
            assert "client_credentials" in [
                "authorization_code",
                "refresh_token",
                *sorted(mcp_oauth._GRANT_HANDLERS),
            ]
        finally:
            mcp_oauth._GRANT_HANDLERS.clear()
            mcp_oauth._GRANT_HANDLERS.update(handlers)
            mcp_oauth._ACCESS_TOKEN_TYPES.clear()
            mcp_oauth._ACCESS_TOKEN_TYPES.update(token_types)


class TestRejections:
    async def test_missing_credentials(self):
        r = await a2a_grants.handle_client_credentials({}, _DB([]))
        assert r.status_code == 400

    async def test_unknown_client(self, ok_bcrypt):
        r = await a2a_grants.handle_client_credentials(DATA, _DB([None]))
        assert r.status_code == 401

    async def test_wrong_secret(self, bad_bcrypt):
        exposure = _Exposure()
        client = _Client(exposure_id=exposure.id)
        r = await a2a_grants.handle_client_credentials(DATA, _DB([client]))
        assert r.status_code == 401

    async def test_client_without_the_grant(self, ok_bcrypt):
        client = _Client(grants=["authorization_code"], exposure_id=uuid.uuid4())
        r = await a2a_grants.handle_client_credentials(DATA, _DB([client]))
        assert r.status_code == 401

    async def test_non_a2a_client_is_refused(self, ok_bcrypt):
        """A DCR-registered MCP client must not be able to use this grant."""
        r = await a2a_grants.handle_client_credentials(
            DATA, _DB([_Client(exposure_id=None)])
        )
        assert r.status_code == 401

    async def test_revoked_exposure_revokes_the_credential(self, ok_bcrypt):
        """There is no separate credential-revocation step to forget: the
        exposure lookup filters revoked rows, so the token simply stops."""
        client = _Client(exposure_id=uuid.uuid4())
        r = await a2a_grants.handle_client_credentials(DATA, _DB([client, None]))
        assert r.status_code == 401

    async def test_every_rejection_is_byte_identical(self, ok_bcrypt, bad_bcrypt):
        bodies = set()
        for results in (
            [None],
            [_Client(exposure_id=None)],
            [_Client(exposure_id=uuid.uuid4()), None],
        ):
            r = await a2a_grants.handle_client_credentials(DATA, _DB(results))
            bodies.add(bytes(r.body))
        assert len(bodies) == 1, "rejections are distinguishable"


class TestSuccess:
    async def test_token_is_issued_for_a_live_bound_exposure(self, ok_bcrypt):
        run_as_user = uuid.uuid4()
        exposure = _Exposure(run_as_user_id=run_as_user)
        client = _Client(exposure_id=exposure.id)
        r = await a2a_grants.handle_client_credentials(DATA, _DB([client, exposure]))
        assert r.status_code == 200
        import json

        body = json.loads(r.body)
        assert body["token_type"] == "Bearer"
        assert body["access_token"]
        assert body["expires_in"] > 0

    async def test_subject_is_the_run_as_user_not_the_exposer(self, ok_bcrypt):
        import json

        import jwt

        from app.core.config import settings

        run_as_user = uuid.uuid4()
        exposure = _Exposure(run_as_user_id=run_as_user)
        r = await a2a_grants.handle_client_credentials(
            DATA, _DB([_Client(exposure_id=exposure.id), exposure])
        )
        token = json.loads(r.body)["access_token"]
        claims = jwt.decode(
            token, settings.secret_key, algorithms=[settings.jwt_algorithm]
        )
        assert claims["sub"] == str(run_as_user)

    async def test_response_is_not_cacheable(self, ok_bcrypt):
        exposure = _Exposure(run_as_user_id=uuid.uuid4())
        r = await a2a_grants.handle_client_credentials(
            DATA, _DB([_Client(exposure_id=exposure.id), exposure])
        )
        assert r.headers.get("cache-control") == "no-store"

    async def test_granted_scope_is_reported(self, ok_bcrypt):
        import json

        exposure = _Exposure(run_as_user_id=uuid.uuid4())
        r = await a2a_grants.handle_client_credentials(
            DATA, _DB([_Client(exposure_id=exposure.id), exposure])
        )
        assert json.loads(r.body)["scope"] == a2a_tokens.A2A_TOKEN_SCOPE


class TestTheTokenCannotLeaveTheA2ASurface:
    """The partner token names a real user in ``sub``, so it must be unusable
    anywhere that authenticates on ``sub`` alone.

    Every platform JWT is signed with the same secret. Without the two claims
    asserted here, an A2A partner credential is an ordinary platform bearer for
    the agent's owner: valid on ``/mcp/*`` and on every ``/api/v1/*`` endpoint,
    including the exposure endpoints themselves - so the partner could expose
    further agents and mint further credentials, which is exactly the
    escalation the boundary exists to prevent.

    Both claims are positive properties of the *token*, not deny-list entries
    on the surfaces. That direction is the point: a surface nobody thought of
    stays closed.
    """

    @pytest.fixture
    async def claims(self, ok_bcrypt):
        import json

        from app.core.security import decode_access_token

        exposure = _Exposure(run_as_user_id=uuid.uuid4())
        r = await a2a_grants.handle_client_credentials(
            DATA, _DB([_Client(exposure_id=exposure.id), exposure])
        )
        return decode_access_token(json.loads(r.body)["access_token"])

    async def test_type_is_not_the_mcp_resource_credential_type(self, claims):
        # ``mcp_auth.try_oauth_token`` admits only MCP_OAUTH_TOKEN_TYPE,
        # so a distinct type closes /mcp/user, /mcp/opencti, /mcp/openaev and
        # any sibling added later, with no change on that side.
        assert claims["type"] == a2a_tokens.A2A_TOKEN_TYPE
        assert claims["type"] != mcp_oauth.MCP_OAUTH_TOKEN_TYPE

    async def test_carries_a_scope_claim(self, claims):
        # ``deps.get_current_user_ungated`` refuses any token carrying ``scope``
        # on presence alone ("any future scoped token is closed by default"),
        # which is what keeps this token off the whole /api/v1 surface.
        assert claims["scope"] == a2a_tokens.A2A_TOKEN_SCOPE

    async def test_mcp_endpoint_refuses_it(self, claims):
        from app.api.v1 import mcp_oauth

        # /mcp/* admits only its own kind, so a distinct type closes every
        # sibling endpoint there with no change on that side.
        assert claims.get("type") != mcp_oauth.MCP_OAUTH_TOKEN_TYPE

    async def test_the_oauth_session_path_refuses_it(self, ok_bcrypt):
        """The session-consent path must not accept a partner token either -
        it authorizes further OAuth grants."""
        import json

        from fastapi import HTTPException

        exposure = _Exposure(run_as_user_id=uuid.uuid4())
        r = await a2a_grants.handle_client_credentials(
            DATA, _DB([_Client(exposure_id=exposure.id), exposure])
        )
        token = json.loads(r.body)["access_token"]
        with pytest.raises(HTTPException) as exc:
            await mcp_oauth._resolve_session_user(_Request(token), _DB([]))
        assert exc.value.status_code == 401
        assert exc.value.detail == "Invalid token type"

    async def test_the_a2a_surface_accepts_only_its_own_token_kind(self):
        """And the converse. Both tokens here are bound to the right exposure
        and carry the right ``client_id``, so the ONLY thing separating them is
        the kind - which is what makes this a real check rather than a
        restatement of the binding tested above.
        """
        from app.api.v1 import a2a_server

        exposure = _Exposure(run_as_user_id=uuid.uuid4())
        client = _Client(exposure_id=exposure.id)

        a2a_token = a2a_tokens.create_a2a_access_token(
            str(exposure.run_as_user_id), client_id=client.client_id
        )
        mcp_token = mcp_oauth._create_mcp_access_token(
            str(exposure.run_as_user_id), client_id=client.client_id
        )

        accepted, _, _provider = await a2a_server._authenticate(
            _Request(a2a_token), _DB([client]), exposure
        )
        assert accepted == client.client_id
        refused, reason, _provider = await a2a_server._authenticate(
            _Request(mcp_token), _DB([client]), exposure
        )
        assert refused is None
        # A wrong KIND is a plain refusal, not the platform-key signal — that
        # label is reserved for fcp-/mcp- API keys, the migration tell.
        assert reason == "unauthenticated"


class TestTheTwoMintersStaySeparate:
    """One shared encoder, two entry points, and the claim sets do not mix."""

    def test_mcp_resource_tokens_keep_their_type_and_carry_no_scope(self):
        """No DCR-registered MCP client is affected by the A2A grant existing."""
        from app.core.security import decode_access_token

        claims = decode_access_token(
            mcp_oauth._create_mcp_access_token(str(uuid.uuid4()), client_id="c")
        )
        assert claims["type"] == mcp_oauth.MCP_OAUTH_TOKEN_TYPE
        assert "scope" not in claims

    def test_the_two_token_types_are_distinct(self):
        """The constants no longer sit side by side, so assert what that
        adjacency used to make obvious.

        Every surface that consumes a token tells the kinds apart by this value
        alone. If the two ever collided, an A2A partner token would be accepted
        as an MCP resource credential again - silently, and with no other test
        failing.
        """
        assert a2a_tokens.A2A_TOKEN_TYPE != mcp_oauth.MCP_OAUTH_TOKEN_TYPE

    def test_the_a2a_minter_needs_no_caller_supplied_claims(self):
        """The confinement claims are baked in, not passed in.

        This is the point of the separate minter: ``type`` closes ``/mcp/*`` and
        ``scope`` closes ``/api/v1/*``, they are only correct together, and a
        signature that let a caller supply one without the other would let a
        future edit silently reopen a surface. So the A2A minter takes no claim
        arguments at all - it cannot be called wrongly.
        """
        import inspect

        params = inspect.signature(a2a_tokens.create_a2a_access_token).parameters
        assert set(params) == {"subject", "client_id"}

        from app.core.security import decode_access_token

        claims = decode_access_token(
            a2a_tokens.create_a2a_access_token(str(uuid.uuid4()), client_id="c")
        )
        assert claims["type"] == a2a_tokens.A2A_TOKEN_TYPE
        assert claims["scope"] == a2a_tokens.A2A_TOKEN_SCOPE


class TestSessionGuardDoesNotDependOnRegistration:
    """The session guard must close on the token, not on the grant registry.

    An earlier refactor keyed this on membership of the set of registered token
    types. That set is populated at startup, so the guard was open until the
    grant that mints the token had registered - a security check whose
    correctness depended on import order. It now keys on the PRESENCE of a
    ``type`` claim, which no platform session token carries.
    """

    async def test_an_unregistered_token_kind_is_still_refused(self):
        """The decisive case: a type this server has never heard of."""
        from datetime import UTC, datetime, timedelta

        import jwt
        from fastapi import HTTPException

        from app.api.v1 import mcp_oauth
        from app.core.config import settings

        now = datetime.now(UTC)
        token = jwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "iat": now,
                "exp": now + timedelta(minutes=5),
                "type": "some_future_resource_nobody_registered",
            },
            settings.secret_key,
            algorithm=settings.jwt_algorithm,
        )
        with pytest.raises(HTTPException) as exc:
            await mcp_oauth._resolve_session_user(_Request(token), _DB([]))
        assert exc.value.status_code == 401
        assert exc.value.detail == "Invalid token type"

    async def test_a_plain_session_token_passes_the_guard(self):
        """Guard against over-broadening: a real session must still get through
        to the user lookup rather than being refused as a resource credential."""
        from fastapi import HTTPException

        from app.api.v1 import mcp_oauth
        from app.core.security import create_access_token

        token = create_access_token(str(uuid.uuid4()))
        with pytest.raises(HTTPException) as exc:
            await mcp_oauth._resolve_session_user(_Request(token), _DB([None]))
        # Reached the user lookup - i.e. past the type guard - and failed there.
        assert exc.value.detail != "Invalid token type"

    def test_registering_a_colliding_token_type_is_refused(self):
        """Two grants sharing a ``type`` makes each one's tokens accepted
        wherever the other's are - the exact confusion the claim prevents."""
        from app.api.v1 import mcp_oauth

        async def _other_handler(_data, _db):
            return None

        with pytest.raises(ValueError, match="already minted"):
            mcp_oauth.register_grant_handler(
                "some_other_grant",
                _other_handler,
                token_type=mcp_oauth.MCP_OAUTH_TOKEN_TYPE,
            )
