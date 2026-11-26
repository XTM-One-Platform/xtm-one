"""XTM One's registration with the XTM Hub: the rules that need no database.

The Hub check (what is sent, how each answer and each failure reads), the
version and Hub URL the registration is bound to, the request body the API
validates (422 before anything is sent), and the status each answer leads to.
The database half is ``test_xtm_hub_registration_postgres.py``.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.api.v1.xtm_hub_registration import RegisterRequest, XtmHubRegistrationStatusResponse
from app.core import config
from app.core.config import settings
from app.integrations.providers import xtm_hub as hub
from app.models.xtm_hub_registration import (
    STATUS_LOST_CONNECTIVITY,
    STATUS_NEEDS_REREGISTRATION,
    STATUS_REGISTERED,
)
from app.services import xtm_hub_registration as registration

_PLATFORM = uuid.UUID("6a3c2a8e-1f2b-4c3d-9e8f-0a1b2c3d4e5f")
# Computed, so no literal looks like a credential to a secret scanner.
_TOKEN = str(uuid.uuid5(uuid.NAMESPACE_URL, "test:xtm-hub-platform-token"))


@pytest.mark.parametrize(
    ("app_version", "expected"),
    [
        ("1.260513.0", "1.260513.0"),
        ("1.260513.0-lts.1", "1.260513.0"),
        ("1.260513.0-dev.0123456", "1.260513.0"),
        ("0.0.0-dev", "0.0.0"),
        ("01.02.03", "1.2.3"),
        ("", "0.0.0"),
        ("not-a-version", "0.0.0"),
    ],
)
def test_the_registration_sends_the_version_core(monkeypatch, app_version, expected):
    monkeypatch.setattr(settings, "app_version", app_version)
    assert registration.registration_platform_version() == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://hub.example.com/", "https://hub.example.com"),
        (None, hub.xtm_hub_default_url()),
        ("", hub.xtm_hub_default_url()),
        ("http://localhost:4002", "http://localhost:4002"),
        ("http://127.0.0.1:4002/", "http://127.0.0.1:4002"),
        ("http://[::1]:4002", "http://[::1]:4002"),
        ("http://hub.example.com", ""),
        ("http://10.0.0.5", ""),
        ("hub.example.com", ""),
        ("https://user:pw@hub.example.com", ""),
        ("ftp://hub.example.com", ""),
    ],
    ids=lambda v: repr(v),
)
def test_the_token_only_travels_over_https_or_to_this_machine(raw, expected):
    assert registration.registration_hub_url(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://hub.example.com/", "https://hub.example.com"),
        ("  https://staging.hub.example.com  ", "https://staging.hub.example.com"),
        ("http://localhost:4002", "http://localhost:4002"),
        ("", "https://hub.filigran.io"),
    ],
    ids=lambda v: repr(v),
)
def test_the_default_hub_url_is_read_from_the_environment(raw, expected):
    assert config.Settings(xtm_hub_default_url=raw).xtm_hub_default_url == expected


@pytest.mark.parametrize(
    "raw",
    ["hub.example.com", "ftp://hub.example.com", "https://user:pw@hub.example.com", "https://hub.example.com/?a=1"],
)
def test_a_malformed_default_hub_url_refuses_to_start(raw):
    with pytest.raises(ValidationError, match="XTM_HUB_DEFAULT_URL"):
        config.Settings(xtm_hub_default_url=raw)


def test_every_reader_of_the_default_hub_follows_the_setting(monkeypatch):
    monkeypatch.setattr(settings, "xtm_hub_default_url", "https://staging.hub.example.com")

    assert hub.xtm_hub_default_url() == "https://staging.hub.example.com"
    assert hub.xtm_hub_base_url("") == "https://staging.hub.example.com"
    assert registration.registration_hub_url(None) == "https://staging.hub.example.com"
    assert XtmHubRegistrationStatusResponse(status="unregistered", hub_url=None).default_hub_url == (
        "https://staging.hub.example.com"
    )


class _Hub:
    def __init__(self, data=None, err=None):
        self.data, self.err, self.calls = data, err, []

    async def __call__(self, base_url, query, variables, action, **credentials):
        self.calls.append((base_url, query, variables, action))
        self.credentials = credentials
        return self.data, self.err


@pytest.mark.parametrize("contract", ["EE", "CE"])
@pytest.mark.asyncio
async def test_the_check_sends_this_platform_identity_its_token_and_its_contract(monkeypatch, contract):
    fake = _Hub(data={"refreshPlatformRegistrationConnectivityStatus": {"status": "active"}})
    monkeypatch.setattr(hub, "xtm_hub_graphql", fake)
    monkeypatch.setattr(settings, "app_version", "1.260930.0-dev.abcdef1")

    result = await registration.check_registration(
        "https://hub.example.com", registration.PlatformCredential(_PLATFORM, token=_TOKEN), contract
    )

    assert result == "active"
    assert len(fake.calls) == 1
    # Before enrollment, the token travels in the input as it always has: no header credential.
    assert fake.credentials == {}
    base_url, query, variables, _action = fake.calls[0]
    assert base_url == "https://hub.example.com"
    assert query == registration.REFRESH_MUTATION
    assert variables == {
        "input": {
            "platformId": str(_PLATFORM),
            "token": _TOKEN,
            "platformVersion": "1.260930.0",
            "platformIdentifier": "xtmone",
            "platformContract": contract,
        }
    }


@pytest.mark.parametrize(("edition", "expected"), [("enterprise", "EE"), ("community", "CE")])
@pytest.mark.asyncio
async def test_the_contract_is_the_edition_the_platform_reports(monkeypatch, edition, expected):
    from app.api.v1 import platform

    async def deployment_edition(_db):
        return edition

    monkeypatch.setattr(platform, "get_deployment_edition", deployment_edition)
    assert await registration.registration_contract(object()) == expected


@pytest.mark.parametrize(
    ("data", "err", "expected"),
    [
        ({"refreshPlatformRegistrationConnectivityStatus": {"status": "inactive"}}, None, "inactive"),
        ({"refreshPlatformRegistrationConnectivityStatus": {"status": "not_found"}}, None, "not_found"),
        (None, hub._UNREACHABLE, "unreachable"),
        (None, hub._UNAVAILABLE, "unreachable"),
        (None, hub._NOT_A_HUB, "refused"),
        (None, hub._REFUSED, "refused"),
        ({"refreshPlatformRegistrationConnectivityStatus": {"status": "maybe"}}, None, "refused"),
        ({"refreshPlatformRegistrationConnectivityStatus": None}, None, "refused"),
        ({}, None, "refused"),
    ],
    ids=["inactive", "not-found", "unreachable", "unavailable", "not-a-hub", "graphql-error", "unknown-status", "null", "empty"],
)
@pytest.mark.asyncio
async def test_each_hub_answer_reads_as_one_check_result(monkeypatch, data, err, expected):
    monkeypatch.setattr(hub, "xtm_hub_graphql", _Hub(data=data, err=err))
    credential = registration.PlatformCredential(_PLATFORM, token=_TOKEN)
    assert await registration.check_registration("https://hub.example.com", credential, "CE") == expected


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ("active", STATUS_REGISTERED),
        ("inactive", STATUS_LOST_CONNECTIVITY),
        ("unreachable", STATUS_LOST_CONNECTIVITY),
        ("refused", STATUS_LOST_CONNECTIVITY),
        ("not_found", None),
        ("key_mismatch", STATUS_NEEDS_REREGISTRATION),
        ("token_path_closed", STATUS_NEEDS_REREGISTRATION),
    ],
)
def test_each_check_result_leads_to_one_status(result, expected):
    assert registration.status_after(result) == expected


def test_a_valid_registration_body_is_normalised():
    body = RegisterRequest(hub_url=" https://hub.example.com/ ", token=f" {_TOKEN.upper()} ")
    assert body.hub_url == "https://hub.example.com"
    assert body.token == _TOKEN


@pytest.mark.parametrize(
    ("hub_url", "token", "field"),
    [
        ("https://hub.example.com", "not-a-token", "token"),
        ("https://hub.example.com", "", "token"),
        ("https://hub.example.com", "x" * 65, "token"),
        ("http://hub.example.com", _TOKEN, "hub_url"),
        ("javascript:alert(1)", _TOKEN, "hub_url"),
        ("", _TOKEN, "hub_url"),
    ],
    ids=["not-a-uuid", "empty-token", "long-token", "plain-http", "script", "empty-url"],
)
def test_an_invalid_registration_body_is_refused_before_anything_is_sent(hub_url, token, field):
    with pytest.raises(ValidationError) as caught:
        RegisterRequest(hub_url=hub_url, token=token)
    assert [error["loc"][0] for error in caught.value.errors()] == [field]


@pytest.mark.parametrize(
    ("license_type", "validated", "expected"),
    [("trial", True, "trial"), ("standard", True, "standard"), ("trial", False, "CE")],
    ids=["trial", "other-type-as-is", "no-valid-licence"],
)
@pytest.mark.asyncio
async def test_an_automatic_registration_declares_the_licence_type_as_is(
    monkeypatch, license_type, validated, expected
):
    from types import SimpleNamespace

    from app.api.v1 import platform_settings_helpers
    from app.services import xtm_license

    async def platform_settings(_db):
        return None

    monkeypatch.setattr(platform_settings_helpers, "get_platform_settings", platform_settings)
    monkeypatch.setattr(
        xtm_license,
        "resolve_xtm_license",
        lambda _ps: SimpleNamespace(license_type=license_type, license_validated=validated),
    )
    assert await registration.auto_register_contract(object()) == expected
