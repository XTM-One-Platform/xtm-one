"""Three targets, three credential flows, one property they all share.

FR-010 / FR-045. All three mint a SHORT-LIVED bearer from a stored
non-interactive credential; none stores a bearer directly. Keeping the flows
in one module is what makes that checkable by reading, and this file is what
makes it checkable by running.

The three-way failure split is the other half: a credential that cannot mint
(durable), a partner that cannot be reached (transient), and a partner that
rejects a token we DID mint (durable, different reason). Collapsing any two
would send an operator to fix the wrong thing.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from app.a2a.client import credentials
from app.a2a.client.credentials import CredentialError
from tests.a2a_client_stubs import resolve_public


def _integration(provider, **kw):
    """A minimal integration stand-in; ``kw`` overrides any default."""
    fields = {
        "id": "00000000-0000-0000-0000-000000000001",
        "provider": provider,
        "client_id_enc": None,
        "client_secret_enc": None,
        "extra_config_enc": None,
        "token_url": None,
    }
    fields.update(kw)
    return SimpleNamespace(**fields)


def _returns(config):
    """A stand-in for the async ``credentials._config`` answering *config*."""

    async def _config(_integration):
        return config

    return _config


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _token_ok(_request):
    return httpx.Response(200, json={"access_token": "minted", "expires_in": 3600})


class TestSharedProperties:
    def test_every_supported_target_has_a_flow(self):
        from app.a2a.providers import ALL_A2A_PROVIDERS

        assert set(credentials._FLOWS) == set(ALL_A2A_PROVIDERS)

    @pytest.mark.asyncio
    async def test_an_unsupported_provider_is_a_durable_credential_error(self):
        with pytest.raises(CredentialError):
            await credentials.mint_token(_integration("github"), _client(_token_ok))

    def test_credential_errors_are_durable_never_transient(self):
        # A credential that cannot mint will not mint next run either, so it
        # must never reach the breaker's counter.
        assert CredentialError.classification.is_durable


@pytest.fixture(autouse=True)
def _mp(request, monkeypatch):
    """Expose ``monkeypatch`` to the class-based tests below."""
    request.instance.monkeypatch = monkeypatch if request.instance else None


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    resolve_public(monkeypatch)
    credentials._TOKENS.clear()


class TestOAuthClientCredentials:
    @pytest.mark.asyncio
    async def test_it_exchanges_id_and_secret_for_a_bearer(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["body"] = request.content.decode()
            return _token_ok(request)

        integration = _integration(
            "oauth_a2a", token_url="https://partner.example/token"
        )
        # Config path rather than encrypted columns, so the test does not
        # depend on the Fernet key. ``monkeypatch``, not a reload: reloading
        # the module rebinds ``CredentialError``, and ``pytest.raises`` then
        # stops matching the class the test imported.
        self.monkeypatch.setattr(credentials, "_config", _returns({"client_id": "cid", "client_secret": "csec"}))
        headers = await credentials.outbound_headers(integration, _client(handler))
        assert headers == {"Authorization": "Bearer minted"}
        assert "grant_type=client_credentials" in seen["body"]
        assert seen["url"] == "https://partner.example/token"

    @pytest.mark.asyncio
    async def test_missing_pieces_are_a_credential_error_not_a_request(self):
        called = False

        def handler(request):
            nonlocal called
            called = True
            return _token_ok(request)

        with pytest.raises(CredentialError, match="required"):
            await credentials.mint_token(_integration("oauth_a2a"), _client(handler))
        # Refusing before the request matters: a half-configured integration
        # must not send anything to a partner.
        assert called is False


class TestTheTokenEndpointIsGuarded:
    """A token URL is operator-supplied and the POST to it carries the secret:
    the same request-forgery surface as a card URL."""

    @pytest.mark.asyncio
    async def test_a_forbidden_token_url_is_refused_before_any_request(self):
        called = False

        def handler(request):
            nonlocal called
            called = True
            return _token_ok(request)

        integration = _integration("oauth_a2a", token_url="https://169.254.169.254/token")
        self.monkeypatch.setattr(credentials, "_config", _returns({"client_id": "cid", "client_secret": "csec"}))
        # A credential error: the endpoint they typed is what the operator fixes.
        with pytest.raises(CredentialError, match="destination"):
            await credentials.mint_token(integration, _client(handler))
        assert called is False

    @pytest.mark.asyncio
    async def test_a_token_url_that_resolves_inward_is_refused(self):
        from app.a2a.client import destinations

        async def _inward(_host, _port):
            return [(None, None, None, "", ("10.0.0.5", 443))]

        self.monkeypatch.setattr(destinations, "_resolve", _inward)
        called = False

        def handler(request):
            nonlocal called
            called = True
            return _token_ok(request)

        integration = _integration("oauth_a2a", token_url="https://partner.example/token")
        self.monkeypatch.setattr(credentials, "_config", _returns({"client_id": "cid", "client_secret": "csec"}))
        with pytest.raises(CredentialError, match="destination"):
            await credentials.mint_token(integration, _client(handler))
        assert called is False

    @pytest.mark.asyncio
    async def test_a_plain_http_token_url_is_refused(self):
        integration = _integration("oauth_a2a", token_url="http://partner.example/token")
        self.monkeypatch.setattr(credentials, "_config", _returns({"client_id": "cid", "client_secret": "csec"}))
        with pytest.raises(CredentialError, match="https"):
            await credentials.mint_token(integration, _client(_token_ok))

    @pytest.mark.asyncio
    async def test_a_key_s_token_uri_is_guarded_the_same_way(self):
        called = False

        def handler(request):
            nonlocal called
            called = True
            return _token_ok(request)

        integration = _integration("google_a2a")
        self.monkeypatch.setattr(
            credentials,
            "_config",
            _returns(
                {
                    "service_account_json": {
                        "client_email": "sa@p.iam.gserviceaccount.com",
                        "private_key": "not-a-key",
                        "token_uri": "https://127.0.0.1/token",
                    }
                }
            ),
        )
        with pytest.raises(CredentialError, match="destination"):
            await credentials.mint_token(integration, _client(handler))
        assert called is False


class TestTheBearerIsReused:
    """A mint is a network round trip per call otherwise."""

    def _rig(self, **integration_kw):
        mints = []

        def handler(request):
            mints.append(request)
            return _token_ok(request)

        integration = _integration(
            "oauth_a2a", token_url="https://partner.example/token", **integration_kw
        )
        self.monkeypatch.setattr(credentials, "_config", _returns({"client_id": "cid", "client_secret": "csec"}))
        return integration, _client(handler), mints

    @pytest.mark.asyncio
    async def test_two_calls_mint_once(self):
        integration, client, mints = self._rig()
        await credentials.outbound_headers(integration, client)
        await credentials.outbound_headers(integration, client)
        assert len(mints) == 1

    @pytest.mark.asyncio
    async def test_a_rotated_secret_is_a_miss(self):
        integration, client, mints = self._rig(client_secret_enc="enc-1")
        await credentials.outbound_headers(integration, client)
        integration.client_secret_enc = "enc-2"
        await credentials.outbound_headers(integration, client)
        assert len(mints) == 2

    @pytest.mark.asyncio
    async def test_a_token_near_expiry_is_re_minted(self):
        integration, client, mints = self._rig()
        await credentials.outbound_headers(integration, client)
        for token in credentials._TOKENS.values():
            token.__dict__["expires_at"] = 0.0
        await credentials.outbound_headers(integration, client)
        assert len(mints) == 2

    @pytest.mark.asyncio
    async def test_fresh_bypasses_the_cache(self):
        integration, client, mints = self._rig()
        await credentials.outbound_headers(integration, client)
        await credentials.outbound_headers(integration, client, fresh=True)
        assert len(mints) == 2

    @pytest.mark.asyncio
    async def test_a_rejected_token_is_forgotten(self):
        # A token revoked at the partner's end would otherwise be presented
        # again until it expired.
        integration, client, mints = self._rig()
        await credentials.outbound_headers(integration, client)
        credentials.forget_token(integration)
        await credentials.outbound_headers(integration, client)
        assert len(mints) == 2


class TestTokenResponseHandling:
    def test_an_unreadable_lifetime_does_not_refuse_the_token(self):
        minted = credentials._token_from_response(
            httpx.Response(200, json={"access_token": "t", "expires_in": "soon"})
        )
        assert minted.value == "t"

    def test_a_non_json_response_is_a_credential_error(self):
        with pytest.raises(CredentialError, match="did not return JSON"):
            credentials._token_from_response(httpx.Response(200, content=b"<html>"))

    def test_a_200_with_no_token_is_a_credential_error(self):
        # An endpoint answering 200 with no token is a misconfiguration that
        # would otherwise surface later as an unexplained partner 401 — much
        # further from its cause.
        with pytest.raises(CredentialError, match="no access_token"):
            credentials._token_from_response(
                httpx.Response(200, json={"token_type": "Bearer"})
            )


class TestGoogleServiceAccount:
    @pytest.mark.asyncio
    async def test_a_malformed_key_is_refused_before_any_request(self):
        called = False

        def handler(request):
            nonlocal called
            called = True
            return _token_ok(request)

        integration = _integration("google_a2a")
        self.monkeypatch.setattr(credentials, "_config", _returns({"service_account_json": "{not json"}))
        with pytest.raises(CredentialError, match="not valid JSON"):
            await credentials.mint_token(integration, _client(handler))
        assert called is False

    @pytest.mark.asyncio
    async def test_a_key_without_a_private_key_is_refused(self):
        integration = _integration("google_a2a")
        self.monkeypatch.setattr(
            credentials,
            "_config",
            _returns({"service_account_json": json.dumps({"client_email": "x@y.iam"})}),
        )
        with pytest.raises(CredentialError, match="private_key"):
            await credentials.mint_token(integration, _client(_token_ok))


class TestUnreadableCredentials:
    @pytest.mark.asyncio
    async def test_an_unreadable_config_reads_as_absent_not_as_an_error(self):
        # Principle II: a credential that cannot be READ is the same operator
        # problem as one that is missing, and surfacing the decryption error
        # verbatim could put ciphertext in a message.
        integration = _integration("oauth_a2a", extra_config_enc="not-fernet")
        assert await credentials._config(integration) == {}
        assert credentials.configured_card_urls(integration) == []


class TestVaultHeldCredentials:
    """#6080: a credential of the extra config held in a vault is read from it."""

    @pytest.mark.asyncio
    async def test_a_referenced_client_secret_is_resolved_before_the_exchange(self):
        from app.core.security import encrypt_value

        seen = {}

        def handler(request):
            seen["body"] = request.content.decode()
            return _token_ok(request)

        integration = _integration(
            "oauth_a2a",
            token_url="https://partner.example/token",
            extra_config_enc=encrypt_value(
                json.dumps({"client_id": "cid", "client_secret": "vault://partner/a2a#secret"})
            ),
        )

        async def _resolve(value, _owner):
            return "resolved-secret" if value == "vault://partner/a2a#secret" else value

        from app.services import vault

        self.monkeypatch.setattr(vault, "resolve_secret_text", _resolve)
        await credentials.mint_token(integration, _client(handler))
        assert "client_secret=resolved-secret" in seen["body"]
        assert "vault" not in seen["body"]

    @pytest.mark.asyncio
    async def test_an_unavailable_referenced_secret_is_a_credential_error(self):
        from app.core.security import encrypt_value
        from app.services import vault

        called = False

        def handler(request):
            nonlocal called
            called = True
            return _token_ok(request)

        integration = _integration(
            "oauth_a2a",
            token_url="https://partner.example/token",
            extra_config_enc=encrypt_value(
                json.dumps({"client_id": "cid", "client_secret": "vault://partner/a2a#secret"})
            ),
        )

        async def _refuse(_value, _owner):
            raise vault.SecretUnavailableError("not_found")

        self.monkeypatch.setattr(vault, "resolve_secret_text", _refuse)
        with pytest.raises(CredentialError, match="could not be read") as exc:
            await credentials.mint_token(integration, _client(handler))
        assert "partner/a2a" not in str(exc.value)
        assert called is False

    def test_the_card_urls_are_read_as_stored(self):
        from app.core.security import encrypt_value

        integration = _integration(
            "oauth_a2a",
            extra_config_enc=encrypt_value(
                json.dumps({"card_urls": "https://a/c.json", "client_secret": "vault://partner/a2a#secret"})
            ),
        )
        assert credentials.configured_card_urls(integration) == ["https://a/c.json"]
