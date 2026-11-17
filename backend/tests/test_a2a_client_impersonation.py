"""The keyless Google credential mode, and the refusals that make it usable.

Why this mode exists at all: ``google_a2a`` outbound signs a JWT assertion
with a service-account key, and
``constraints/iam.disableServiceAccountKeyCreation`` is Google's own
recommended baseline. Under it a key cannot be created — not misconfigured,
unconfigurable — so a partner with an ordinary security posture could not be
reached outbound by any configuration at all.

The property under test is the one that makes the mode trustworthy rather than
merely present: **each failure names the party who can fix it**. The platform
having no Google identity, the partner not having granted
``serviceAccountTokenCreator``, and an operator filling in both modes are three
different people's problems, and a single "cannot mint a token" sends all three
to the wrong place.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import httpx
import pytest

from app.a2a.client import credentials
from app.a2a.client.credentials import CredentialError

TARGET = "agent-caller@their-project.iam.gserviceaccount.com"
IAM_URL = (
    "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
    f"{TARGET}:generateAccessToken"
)
CLOUD_PLATFORM = "https://www.googleapis.com/auth/cloud-platform"


def _integration(**config):
    """An outbound Google registration whose stored config is *config*."""

    async def _config(_integration):
        return dict(config)

    credentials._config = _config
    return SimpleNamespace(
        id="i1",
        provider="google_a2a",
        extra_config_enc=b"",
        client_id_enc=None,
        client_secret_enc=None,
        token_url=None,
    )


@pytest.fixture(autouse=True)
def _restore_the_config_reader():
    """``_integration`` replaces the config reader; put it back afterwards.

    Restore it after each test so the fake config cannot leak into other
    test files.
    """
    original = credentials._config
    yield
    credentials._config = original


@pytest.fixture(autouse=True)
def _forget_the_ambient_identity():
    """Each test resolves the platform's identity for itself.

    Module state on purpose in the product — the identity is a fact about the
    deployment, and resolving it per delegation would be a metadata round trip
    per delegation — so it has to be reset here or the first test's answer
    would be every test's answer.
    """
    credentials._ambient_credentials = None
    yield
    credentials._ambient_credentials = None


class _StubAdc:
    """A resolved Google credential, as ``google.auth.default`` returns one."""

    def __init__(self):
        self.token = "ya29.the-platforms-own-token"
        self.valid = True
        self.refreshes = 0

    def refresh(self, _request):
        self.refreshes += 1
        self.valid = True


@pytest.fixture
def platform_identity(monkeypatch):
    """The platform runs with a Google identity.

    Patched at ``google.auth.default`` rather than at
    ``_ambient_google_token``: the resolution, the caching and the off-loop
    hop are part of what this file is for, so stubbing above them would leave
    the interesting half untested.
    """
    import google.auth

    stub = _StubAdc()
    calls = {"n": 0}

    def _default(scopes=None):
        calls["n"] += 1
        assert scopes == [CLOUD_PLATFORM], scopes
        return stub, "the-platforms-project"

    monkeypatch.setattr(google.auth, "default", _default)
    return SimpleNamespace(stub=stub, calls=calls)


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestTheExchange:
    @pytest.mark.asyncio
    async def test_it_borrows_the_platform_identity_to_become_the_target(
        self, platform_identity
    ):
        seen: dict = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "accessToken": "ya29.a-token-as-the-partner",
                    "expireTime": "2099-01-01T00:00:00Z",
                },
            )

        async with _client(handler) as client:
            minted = await credentials.mint_token(
                _integration(impersonate_service_account=TARGET), client
            )

        assert minted.value == "ya29.a-token-as-the-partner"
        assert seen["url"] == IAM_URL
        # The PLATFORM's token authorises the exchange; the partner's identity
        # is the subject of it. Nothing the partner holds is presented.
        assert seen["auth"] == "Bearer ya29.the-platforms-own-token"
        assert seen["body"] == {"scope": [CLOUD_PLATFORM], "lifetime": "3600s"}
        # And the header handed downstream is an ordinary bearer, so nothing
        # past this module can tell the two modes apart (FR-045).
        assert minted.headers() == {"Authorization": "Bearer ya29.a-token-as-the-partner"}

    @pytest.mark.asyncio
    async def test_the_expiry_comes_from_the_reply_not_from_a_guess(
        self, platform_identity
    ):
        # proto-JSON names: ``accessToken`` and an RFC 3339 ``expireTime``,
        # where the OAuth reader looks for ``access_token``/``expires_in``.
        # Reusing that reader would find no token at all.
        def handler(_request):
            return httpx.Response(
                200,
                json={"accessToken": "t", "expireTime": "2030-06-01T12:00:00Z"},
            )

        async with _client(handler) as client:
            minted = await credentials.mint_token(
                _integration(impersonate_service_account=TARGET), client
            )
        # 2030-06-01T12:00:00Z, i.e. read from the reply rather than assumed.
        assert minted.expires_at > time.time() + 3600 * 24

    @pytest.mark.asyncio
    async def test_an_unreadable_expiry_assumes_the_api_default(
        self, platform_identity
    ):
        def handler(_request):
            return httpx.Response(
                200, json={"accessToken": "t", "expireTime": "not-a-timestamp"}
            )

        async with _client(handler) as client:
            minted = await credentials.mint_token(
                _integration(impersonate_service_account=TARGET), client
            )
        assert 3500 < minted.expires_at - time.time() <= 3600


    @pytest.mark.asyncio
    async def test_the_platform_identity_is_resolved_once_and_reused(
        self, platform_identity
    ):
        # Module state on purpose: the identity is a fact about the deployment,
        # so resolving it per delegation would be a metadata round trip per
        # delegation. google-auth refreshes the credential itself, so nothing
        # here has to know when it expires.
        integration = _integration(impersonate_service_account=TARGET)

        def handler(_request):
            return httpx.Response(200, json={"accessToken": "t"})

        async with _client(handler) as client:
            await credentials.mint_token(integration, client)
            await credentials.mint_token(integration, client)

        assert platform_identity.calls["n"] == 1
        assert platform_identity.stub.refreshes == 0

    @pytest.mark.asyncio
    async def test_an_expired_platform_identity_is_refreshed_not_re_resolved(
        self, platform_identity
    ):
        platform_identity.stub.valid = False

        async with _client(
            lambda _r: httpx.Response(200, json={"accessToken": "t"})
        ) as client:
            await credentials.mint_token(
                _integration(impersonate_service_account=TARGET), client
            )

        assert platform_identity.stub.refreshes == 1


class TestTheSelectedModeDecides:
    """An explicit choice beats inference, and that is the point of the choice.

    The form asks which mode this registration uses and hides the other field.
    Without honouring the answer, a value left behind by a previous mode would
    still decide - which is exactly the ambiguity the dropdown exists to remove.
    """

    @pytest.mark.asyncio
    async def test_mode_key_uses_the_key_even_with_a_target_left_behind(self):
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            NoEncryption,
            PrivateFormat,
        )

        pem = (
            rsa.generate_private_key(public_exponent=65537, key_size=2048)
            .private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
            .decode()
        )
        seen: dict = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})

        async with _client(handler) as client:
            await credentials.mint_token(
                _integration(
                    credential_mode="key",
                    service_account_json={
                        "client_email": "signer@p.iam.gserviceaccount.com",
                        "private_key": pem,
                    },
                    impersonate_service_account=TARGET,
                ),
                client,
            )
        # Google's TOKEN endpoint, not the impersonation exchange.
        assert seen["url"] == "https://oauth2.googleapis.com/token"

    @pytest.mark.asyncio
    async def test_mode_impersonation_ignores_a_key_left_behind(
        self, platform_identity
    ):
        seen: dict = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"accessToken": "t"})

        async with _client(handler) as client:
            await credentials.mint_token(
                _integration(
                    credential_mode="impersonation",
                    impersonate_service_account=TARGET,
                    service_account_json={"private_key": "x", "client_email": "y"},
                ),
                client,
            )
        assert seen["url"] == IAM_URL

    @pytest.mark.asyncio
    async def test_a_mode_whose_field_is_empty_says_which_is_missing(self):
        async with _client(lambda _r: httpx.Response(200)) as client:
            with pytest.raises(CredentialError) as caught:
                await credentials.mint_token(
                    _integration(credential_mode="key"), client
                )
            assert "no key JSON is configured" in str(caught.value)
            with pytest.raises(CredentialError) as caught:
                await credentials.mint_token(
                    _integration(credential_mode="impersonation"), client
                )
            assert "no service account to impersonate" in str(caught.value)

    @pytest.mark.asyncio
    async def test_a_registration_predating_the_choice_still_works(
        self, platform_identity
    ):
        # No mode recorded: inferred from what is filled in, exactly as before.
        async with _client(
            lambda _r: httpx.Response(200, json={"accessToken": "t"})
        ) as client:
            minted = await credentials.mint_token(
                _integration(impersonate_service_account=TARGET), client
            )
        assert minted.value == "t"


class TestEachRefusalNamesWhoCanFixIt:
    @pytest.mark.asyncio
    async def test_no_platform_identity_says_so_rather_than_blaming_the_fields(
        self, monkeypatch
    ):
        # The most consequential message here: the fix is not on the
        # integration at all, and an operator told "cannot mint a token" would
        # re-type a credential that was never the problem. Raised from where
        # it really would be — ADC resolution, which is what fails on a
        # deployment with no Google identity.
        import google.auth
        from google.auth.exceptions import DefaultCredentialsError

        def _no_adc(scopes=None):
            raise DefaultCredentialsError("no metadata server, no key file")

        monkeypatch.setattr(google.auth, "default", _no_adc)

        async with _client(lambda _r: httpx.Response(200)) as client:
            with pytest.raises(CredentialError) as caught:
                await credentials.mint_token(
                    _integration(impersonate_service_account=TARGET), client
                )
        message = str(caught.value)
        assert "no Google identity" in message
        assert "Application Default Credentials" in message

    @pytest.mark.asyncio
    async def test_a_missing_grant_names_the_role_and_whose_end_it_is_at(
        self, platform_identity
    ):
        def handler(_request):
            return httpx.Response(403, json={"error": {"message": "denied"}})

        async with _client(handler) as client:
            with pytest.raises(CredentialError) as caught:
                await credentials.mint_token(
                    _integration(impersonate_service_account=TARGET), client
                )
        message = str(caught.value)
        # Google's own words first: several causes share a 403 - the missing
        # permission, a disabled API, a user credential with no quota project -
        # and only the party that refused knows which.
        assert "denied" in message
        assert "roles/iam.serviceAccountTokenCreator" in message
        assert "iamcredentials API" in message
        assert "partner's end" in message

    @pytest.mark.asyncio
    async def test_an_unknown_account_is_its_own_answer(self, platform_identity):
        async with _client(lambda _r: httpx.Response(404)) as client:
            with pytest.raises(CredentialError) as caught:
                await credentials.mint_token(
                    _integration(impersonate_service_account=TARGET), client
                )
        assert TARGET in str(caught.value)

    @pytest.mark.asyncio
    async def test_a_reply_with_no_token_is_refused(self, platform_identity):
        async with _client(lambda _r: httpx.Response(200, json={})) as client:
            with pytest.raises(CredentialError) as caught:
                await credentials.mint_token(
                    _integration(impersonate_service_account=TARGET), client
                )
        assert "accessToken" in str(caught.value)

    @pytest.mark.asyncio
    async def test_both_modes_at_once_is_refused_rather_than_ranked(self):
        # An operator who filled in a key and then switched to impersonation
        # would otherwise keep minting under the key while believing
        # otherwise — and the partner's audit log would name an identity
        # nobody expected to see there.
        async with _client(lambda _r: httpx.Response(200)) as client:
            with pytest.raises(CredentialError) as caught:
                await credentials.mint_token(
                    _integration(
                        impersonate_service_account=TARGET,
                        service_account_json={"private_key": "x", "client_email": "y"},
                    ),
                    client,
                )
        assert "not both" in str(caught.value)

    @pytest.mark.asyncio
    async def test_neither_mode_says_what_either_would_need(self):
        async with _client(lambda _r: httpx.Response(200)) as client:
            with pytest.raises(CredentialError) as caught:
                await credentials.mint_token(_integration(), client)
        message = str(caught.value)
        assert "key JSON" in message and "impersonate" in message

    @pytest.mark.parametrize(
        "target",
        ["not-an-email", "has space@project.iam.gserviceaccount.com", "@project.com", "a@b"],
    )
    @pytest.mark.asyncio
    async def test_an_address_no_account_could_have_is_refused_before_the_call(
        self, platform_identity, target
    ):
        called = {"n": 0}

        def handler(_request):
            called["n"] += 1
            return httpx.Response(200, json={"accessToken": "t"})

        async with _client(handler) as client:
            with pytest.raises(CredentialError):
                await credentials.mint_token(
                    _integration(impersonate_service_account=target), client
                )
        assert called["n"] == 0


class TestTheKeyModeIsUntouched:
    @pytest.mark.asyncio
    async def test_a_key_still_signs_its_own_assertion(self):
        # The mode that shipped, unchanged: a registration configured with a
        # key must not notice that a second mode exists.
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            NoEncryption,
            PrivateFormat,
        )

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
        ).decode()

        seen: dict = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["body"] = request.content.decode()
            return httpx.Response(
                200, json={"access_token": "ya29.from-the-assertion", "expires_in": 3600}
            )

        async with _client(handler) as client:
            minted = await credentials.mint_token(
                _integration(
                    service_account_json={
                        "client_email": "signer@p.iam.gserviceaccount.com",
                        "private_key": pem,
                    }
                ),
                client,
            )
        assert minted.value == "ya29.from-the-assertion"
        assert seen["url"] == "https://oauth2.googleapis.com/token"
        assert "urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer" in seen["body"]
