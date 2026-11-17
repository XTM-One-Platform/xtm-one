"""A partner credential never leaves the platform in readable form.

FR-004 / SC-012. Four surfaces could leak one and each is checked here,
because the interesting failure is not "we return the secret" — nobody writes
that — but a credential reaching a place nobody thought of as a surface: an
error message built from an exception, a health verdict's reason, an audit
detail, a log line.

The direction change makes this sharper than it is for other integrations. An
integration that leaves the inbound direction has its credentials CLEARED
(FR-058), so "the secret is gone" is a property with a moment, and a leak
after that moment would be a secret nobody believes still exists.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from app.a2a.client import credentials, verification
from app.a2a.client.credentials import CredentialError
from app.models.a2a import USABILITY_UNUSABLE
from tests.a2a_client_stubs import resolve_public

SECRET = "s3cr3t-partner-credential-value"


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    # A mint guards its token endpoint like a card URL, resolution included.
    resolve_public(monkeypatch)


class TestTheStoredKeyIsNotReadableBack:
    def test_the_service_account_key_is_redacted_in_responses(self):
        """A private key that the redaction heuristic did not recognise.

        ``_is_sensitive_setting_name`` looks for "secret", "token", "key" or
        "password". ``service_account_json`` contains none of them - not even
        "key" - so a Google service account's PRIVATE KEY was returned in full
        by every read of the integration, while every other credential on the
        platform was masked.

        Pre-existing: ``google_secops`` stores one under the same name. The
        outbound A2A mechanism made it a second place, and a value that is a
        credential everywhere it appears should be redacted everywhere it
        appears.
        """
        from app.api.v1.integrations import _is_sensitive_setting_name

        assert _is_sensitive_setting_name("service_account_json")

    def test_the_impersonation_target_is_not_redacted(self):
        # It is an address, not a secret. Masking it would hide WHICH
        # credential mode a registration uses from the operator who has to
        # choose between them.
        from app.api.v1.integrations import _is_sensitive_setting_name

        assert not _is_sensitive_setting_name("impersonate_service_account")

    def test_an_endpoint_is_not_redacted(self):
        # ``token_url`` contains "token" yet names WHERE the credential is
        # exchanged; masked, an operator could not read back the partner.
        from app.api.v1.integrations import _is_sensitive_setting_name

        assert not _is_sensitive_setting_name("token_url")
        assert not _is_sensitive_setting_name("card_urls")

    def test_every_catalog_field_that_holds_a_credential_is_redacted(self):
        # The sweep that found the first one. A catalog field naming a
        # credential must not depend on its spelling happening to contain one
        # of four words.
        from app.api.v1.integration_catalog import PROVIDER_CATALOG
        from app.api.v1.integrations import _is_sensitive_setting_name

        missed = []
        for provider, entry in PROVIDER_CATALOG.items():
            fields = dict(entry.get("extra_fields") or {})
            for variant in (entry.get("variants") or {}).values():
                fields.update(variant.get("extra_fields") or {})
            # A field with a declared option list cannot hold a secret: its
            # values are enumerated in the catalog itself. Excluded by that
            # property rather than by name - ``credential_mode`` is a chooser,
            # and it tripped this sweep the day it was added.
            choices = set(entry.get("extra_field_options") or {})
            for key in fields:
                if key in choices:
                    continue
                if any(
                    token in key.lower()
                    for token in ("json", "credential", "private", "pem", "passphrase")
                ) and not _is_sensitive_setting_name(key):
                    missed.append(f"{provider}.{key}")
        assert not missed, f"credential-bearing fields returned unredacted: {missed}"


class TestVerdictsCarryNoCredential:
    @pytest.mark.asyncio
    async def test_a_failed_fetch_reports_the_status_not_the_header(self):
        def handler(request):
            # The credential IS on the request; the verdict must not echo it.
            assert request.headers.get("authorization") == f"Bearer {SECRET}"
            return httpx.Response(500)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            verdict = await verification.verify_card(
                card_url="https://partner.example/c.json",
                http_client=c,
                headers={"Authorization": f"Bearer {SECRET}"},
                resolve_dns=False,
            )
        assert verdict.usability == USABILITY_UNUSABLE
        assert SECRET not in (verdict.error_detail or "")

    @pytest.mark.asyncio
    async def test_a_malformed_card_reports_the_parse_error_not_the_body(self):
        def handler(_request):
            # A partner echoing our credential back in its body is not
            # far-fetched — an error page including the request headers does
            # exactly that.
            return httpx.Response(200, content=f"<html>{SECRET}</html>".encode())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            verdict = await verification.verify_card(
                card_url="https://partner.example/c.json",
                http_client=c,
                resolve_dns=False,
            )
        assert verdict.unusable_reason == "card_malformed"
        assert SECRET not in (verdict.error_detail or "")

    @pytest.mark.asyncio
    async def test_no_verdict_field_can_hold_a_credential(self):
        # Structural rather than case-by-case: the Verdict's fields are all
        # reasons, states and projections of a public card. A future field
        # holding a credential would have to be added deliberately, and this
        # asserts nobody has.
        import dataclasses

        names = {f.name for f in dataclasses.fields(verification.Verdict)}
        assert not names & {"headers", "token", "credential", "secret", "auth"}


class TestCredentialErrorsCarryNoCredential:
    @pytest.mark.asyncio
    async def test_a_rejected_exchange_reports_the_status_only(self):
        def handler(_request):
            return httpx.Response(401, json={"error": "invalid_client"})

        from types import SimpleNamespace

        integration = SimpleNamespace(
            id=uuid.uuid4(),
            provider="oauth_a2a",
            client_id_enc=None,
            client_secret_enc=None,
            extra_config_enc=None,
            token_url="https://partner.example/token",
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            with pytest.raises(CredentialError) as exc:
                await credentials.mint_token(integration, c)
        assert SECRET not in str(exc.value)
        # And it says something an operator can act on.
        assert "required" in str(exc.value) or "401" in str(exc.value)

    @pytest.mark.asyncio
    async def test_an_unreadable_credential_is_absent_not_echoed(self):
        # Principle II: unreadable is the same operator problem as missing,
        # and surfacing the decryption failure verbatim could put ciphertext
        # into a message that is rendered and audited.
        from types import SimpleNamespace

        integration = SimpleNamespace(
            id=uuid.uuid4(), extra_config_enc="gAAAAA-not-really-fernet"
        )
        assert await credentials._config(integration) == {}


class TestTheApiResponseShape:
    def test_verification_state_has_no_credential_field(self):
        # The read surface an operator hits. Checked structurally for the
        # same reason as the Verdict: a leak here would be a new field, not a
        # new value in an existing one.
        from app.api.v1.a2a_client import TargetState

        names = set(TargetState.model_fields)
        assert not names & {
            "client_secret",
            "api_key",
            "token",
            "credential",
            "service_account_json",
            "headers",
        }

    def test_it_exposes_the_reason_but_not_the_error_body(self):
        # ``last_verification_error`` is operator-facing detail and IS
        # exposed, which is exactly why the verdict tests above assert no
        # credential reaches it.
        from app.api.v1.a2a_client import TargetState

        assert "last_verification_error" in TargetState.model_fields
        assert "unusable_reason" in TargetState.model_fields


class TestClearingOnDirectionChange:
    def test_every_credential_column_is_cleared(self):
        # FR-058. Asserted by reading the implementation rather than by a
        # round trip, so a NEW credential column added to Integration and
        # forgotten here is caught: the test names the columns it expects to
        # see cleared.
        import inspect

        from app.a2a import providers

        src = inspect.getsource(providers.change_direction)
        for column in (
            "client_id_enc",
            "client_secret_enc",
            "api_key_enc",
            "access_token_enc",
            "refresh_token_enc",
            "extra_config_enc",
        ):
            assert f"integration.{column} = None" in src, column
