"""Authentication options of a spec: every OAuth2 flow, OpenID Connect, OAuth metadata, mutual TLS."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.services.openapi_import import discovery as discovery_module
from app.services.openapi_import.discovery import discovery_urls, fetch_discovery
from app.services.openapi_import.document import parse_spec_document
from app.services.openapi_import.operations import analyze_spec, auth_url_base
from app.services.web_fetch import FetchedBytes

FIXTURES = Path(__file__).parent / "openapi_fixtures"
AUTH_FLOWS = (FIXTURES / "auth_flows_v32.yaml").read_text()

OIDC_URL = "https://login.acme.test/.well-known/openid-configuration"
META_URL = "https://login.acme.test/.well-known/oauth-authorization-server"
OIDC_METADATA = {
    "issuer": "https://login.acme.test",
    "authorization_endpoint": "https://login.acme.test/oidc/authorize",
    "token_endpoint": "https://login.acme.test/oidc/token",
    "device_authorization_endpoint": "https://login.acme.test/oidc/device",
    "grant_types_supported": [
        "authorization_code",
        "refresh_token",
        "client_credentials",
        "urn:ietf:params:oauth:grant-type:device_code",
    ],
    "scopes_supported": ["openid", "profile", "email", "offline_access"],
}
OAUTH_METADATA = {
    "issuer": "https://login.acme.test",
    "authorization_endpoint": "https://login.acme.test/as/authorize",
    "token_endpoint": "https://login.acme.test/as/token",
}


def _analyze(text: str = AUTH_FLOWS, discovery: dict | None = None):
    return analyze_spec(parse_spec_document(text), discovery=discovery)


def _options(analysis):
    return {o.id: o for o in analysis.auth_options}


def test_every_oauth2_flow_is_an_option():
    options = _options(_analyze())
    implicit = options["oauth:implicit"]
    assert (implicit.kind, implicit.integration_auth_type, implicit.runtime_auth()) == (
        "oauth2_implicit",
        "oauth",
        {"type": "bearer"},
    )
    assert implicit.authorize_url == "https://login.acme.test/authorize"
    assert implicit.token_url is None
    password = options["oauth:password"]
    assert (password.kind, password.integration_auth_type) == ("oauth2_password", "oauth_password")
    assert password.token_url == "https://login.acme.test/token"
    device = options["oauth:device"]
    assert (device.kind, device.integration_auth_type) == ("oauth2_device", "oauth")
    assert device.device_authorization_url == "https://login.acme.test/device"
    assert device.token_url == "https://login.acme.test/token"
    assert device.scopes == {"accounts:read": "Read accounts"}


def test_legacy_flows_are_never_the_default_while_another_option_works():
    # The top-level requirement names only the oauth scheme: its device flow
    # wins over its implicit and password flows.
    assert _analyze().default_auth_option == "oauth:device"
    only_legacy = AUTH_FLOWS.replace(
        """        deviceAuthorization:
          deviceAuthorizationUrl: https://login.acme.test/device
          tokenUrl: https://login.acme.test/token
          scopes:
            accounts:read: Read accounts
""",
        "",
    )
    assert _analyze(only_legacy).default_auth_option == "oauth:implicit"


def test_swagger2_implicit_and_password_flows():
    spec = {
        "swagger": "2.0",
        "info": {"title": "Legacy", "version": "1"},
        "host": "legacy.test",
        "paths": {},
        "securityDefinitions": {
            "implicit": {
                "type": "oauth2",
                "flow": "implicit",
                "authorizationUrl": "https://legacy.test/oauth/authorize",
                "scopes": {"read": "Read"},
            },
            "password": {"type": "oauth2", "flow": "password", "tokenUrl": "/oauth/token", "scopes": {}},
        },
    }
    options = _options(_analyze(json.dumps(spec)))
    assert options["implicit:implicit"].kind == "oauth2_implicit"
    assert options["password:password"].token_url == "https://legacy.test/oauth/token"


def test_openid_connect_without_its_discovery_document_says_why():
    oidc = _options(_analyze())["oidc"]
    assert not oidc.supported
    assert "could not be read" in oidc.unsupported_reason
    failed = _options(_analyze(discovery={OIDC_URL: {"error": "the server answered HTTP 404"}}))["oidc"]
    assert failed.unsupported_reason.endswith("the server answered HTTP 404")


def test_openid_connect_becomes_the_flows_its_provider_supports():
    options = _options(_analyze(discovery={OIDC_URL: OIDC_METADATA}))
    code = options["oidc:authorization_code"]
    assert code.kind == "oauth2_authorization_code"
    assert (code.authorize_url, code.token_url) == (
        "https://login.acme.test/oidc/authorize",
        "https://login.acme.test/oidc/token",
    )
    # OpenID Connect asks for openid (and a refresh token when it can), not
    # for every scope the provider knows.
    assert code.default_scopes == ["openid", "offline_access"]
    assert set(code.scopes) == {"openid", "profile", "email", "offline_access"}
    assert options["oidc:device"].device_authorization_url == "https://login.acme.test/oidc/device"
    assert options["oidc:client_credentials"].integration_auth_type == "client_credentials"
    assert "oidc:password" not in options  # not in grant_types_supported
    assert code.label.startswith("OpenID Connect")


def test_oauth_metadata_url_fills_a_scheme_without_flows():
    options = _options(_analyze(discovery={META_URL: OAUTH_METADATA}))
    # RFC 8414: an omitted grant_types_supported means code and implicit.
    code = options["meta:authorization_code"]
    assert (code.authorize_url, code.token_url) == (
        "https://login.acme.test/as/authorize",
        "https://login.acme.test/as/token",
    )
    assert code.default_scopes is None
    assert "meta:client_credentials" not in options
    assert not _options(_analyze())["meta"].supported


def test_oauth_metadata_fills_the_endpoints_a_flow_leaves_out():
    spec = AUTH_FLOWS.replace(
        """    meta:
      type: oauth2
      oauth2MetadataUrl: https://login.acme.test/.well-known/oauth-authorization-server
""",
        """    meta:
      type: oauth2
      oauth2MetadataUrl: https://login.acme.test/.well-known/oauth-authorization-server
      flows:
        clientCredentials:
          scopes:
            accounts:read: Read accounts
""",
    )
    option = _options(_analyze(spec, discovery={META_URL: OAUTH_METADATA}))["meta:client_credentials"]
    assert option.supported and option.token_url == "https://login.acme.test/as/token"


def test_mutual_tls_is_an_option_whose_credential_is_the_certificate():
    mtls = _options(_analyze())["mtls"]
    assert (mtls.supported, mtls.kind, mtls.integration_auth_type) == (True, "mutual_tls", "none")
    assert mtls.runtime_auth() == {"type": "none"}
    assert mtls.description == "A client certificate issued by Acme."


def test_a_deprecated_scheme_is_labelled_and_never_preferred():
    options = _options(_analyze())
    legacy = options["legacy"]
    assert legacy.deprecated and legacy.label.endswith("(deprecated)")
    no_requirement = AUTH_FLOWS.replace("security:\n  - oauth: [accounts:read]\n", "", 1)
    assert _analyze(no_requirement).default_auth_option == "oauth:device"


def test_stored_options_round_trip_the_new_fields():
    from app.services.openapi_import.security import AuthOption

    device = _options(_analyze(discovery={OIDC_URL: OIDC_METADATA}))["oidc:device"]
    again = AuthOption.from_dict(json.loads(json.dumps(device.to_dict())))
    assert again == device
    # A stored option without these fields still reads.
    legacy_row = {k: v for k, v in device.to_dict().items() if k not in ("default_scopes", "deprecated", "device_authorization_url")}
    assert AuthOption.from_dict(legacy_row).default_scopes is None


# ── Discovery fetches ────────────────────────────────────────────────────────


def test_discovery_urls_come_from_openid_and_metadata_schemes():
    document = parse_spec_document(AUTH_FLOWS)
    assert discovery_urls(document, auth_url_base(document)) == [(OIDC_URL, "openid"), (META_URL, "oauth")]


async def test_fetch_discovery_tries_the_well_known_path_of_an_issuer(monkeypatch):
    fetched: list[str] = []

    async def fake_fetch(url, **kwargs):
        fetched.append(url)
        if url == "https://id.acme.test/.well-known/openid-configuration":
            return FetchedBytes(url=url, content=json.dumps(OIDC_METADATA).encode(), content_type="application/json", status_code=200)
        if url == "https://broken.acme.test/.well-known/openid-configuration":
            return FetchedBytes(url=url, content=b"<html>", content_type="text/html", status_code=200)
        request = httpx.Request("GET", url)
        raise httpx.HTTPStatusError("nope", request=request, response=httpx.Response(404, request=request))

    monkeypatch.setattr("app.services.web_fetch.fetch_url_bytes", fake_fetch)
    spec = AUTH_FLOWS.replace(OIDC_URL, "https://id.acme.test").replace(
        META_URL, "https://broken.acme.test/.well-known/openid-configuration"
    )
    document = parse_spec_document(spec)
    found = await fetch_discovery(document, auth_url_base(document))
    assert found["https://id.acme.test"]["token_endpoint"] == "https://login.acme.test/oidc/token"
    assert found["https://broken.acme.test/.well-known/openid-configuration"] == {"error": "it is not a JSON document"}
    assert fetched == [
        "https://id.acme.test",
        "https://id.acme.test/.well-known/openid-configuration",
        "https://broken.acme.test/.well-known/openid-configuration",
    ]


async def test_swagger2_has_no_discovery():
    document = parse_spec_document(json.dumps({"swagger": "2.0", "info": {"title": "x", "version": "1"}, "paths": {}}))
    assert await fetch_discovery(document, None) == {}


@pytest.mark.parametrize("count", [7])
def test_discovery_is_bounded(count):
    schemes = {
        f"s{i}": {"type": "openIdConnect", "openIdConnectUrl": f"https://idp{i}.test/.well-known/openid-configuration"}
        for i in range(count)
    }
    spec = {"openapi": "3.1.0", "info": {"title": "x", "version": "1"}, "paths": {}, "components": {"securitySchemes": schemes}}
    document = parse_spec_document(json.dumps(spec))
    assert len(discovery_urls(document, None)) == discovery_module._MAX_DOCUMENTS
