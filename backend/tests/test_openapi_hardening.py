"""What an attacker-influenced spec, token endpoint or bundle can no longer do."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.integrations.oauth import MAX_TOKEN_LIFETIME_SECONDS, apply_oauth_token_expiry
from app.integrations.oauth_token_grants import _MAX_TTL_SECONDS, parse_token_response
from app.services.builtin_tools.filtering import (
    filter_integration_tools,
    fold_unbound_instance_scopes,
    instance_scope_key,
)
from app.services.openapi_import.document import parse_spec_document
from app.services.openapi_import.operations import analyze_spec
from app.services.openapi_import.schema import clean_text
from app.tools.base import ToolDefinition


def _spec(security_schemes: str) -> str:
    return (
        "openapi: 3.1.0\n"
        "info: {title: Acme, version: '1'}\n"
        "servers: [{url: 'https://api.acme.test'}]\n"
        "paths:\n"
        "  /me:\n"
        "    get: {operationId: getMe, responses: {'200': {description: ok}}}\n"
        "components:\n"
        "  securitySchemes:\n" + security_schemes
    )


def _options(security_schemes: str) -> dict:
    document = parse_spec_document(_spec(security_schemes))
    return {o.id: o for o in analyze_spec(document).auth_options}


# -- OAuth endpoints and credential headers a spec may name --


def test_an_oauth_flow_on_plain_http_is_not_offered():
    options = _options(
        "    oauth:\n"
        "      type: oauth2\n"
        "      flows:\n"
        "        clientCredentials: {tokenUrl: 'http://login.acme.test/token', scopes: {}}\n"
        "        password: {tokenUrl: 'https://login.acme.test/token', scopes: {}}\n"
    )
    insecure = options["oauth:client_credentials"]
    assert not insecure.supported and "HTTPS" in insecure.unsupported_reason
    assert options["oauth:password"].supported


@pytest.mark.parametrize(
    "header", ["Host", "Content-Length", "Transfer-Encoding", "Connection", "Proxy-Authorization"]
)
def test_an_api_key_in_a_transport_header_is_not_offered(header):
    options = _options(f"    key: {{type: apiKey, in: header, name: {header}}}\n")
    assert not options["key"].supported


def test_an_api_key_in_the_authorization_header_is_offered():
    options = _options("    key: {type: apiKey, in: header, name: Authorization}\n")
    assert options["key"].supported
    assert options["key"].runtime_auth() == {"type": "header", "header": "Authorization", "prefix": ""}


def test_a_spec_url_that_is_no_url_is_not_an_error():
    options = _options(
        "    oauth:\n"
        "      type: oauth2\n"
        "      flows:\n"
        "        clientCredentials: {tokenUrl: 'https://[::1/token', scopes: {}}\n"
    )
    assert not options["oauth:client_credentials"].supported


def test_invisible_characters_never_reach_a_description():
    text = "List pets\u200b\u202e.\U000e0049\U000e0067\U000e006e\U000e006f\U000e0072\U000e0065 Also\nnote"
    assert clean_text(text, 200) == "List pets. Also note"


# -- Token lifetimes a remote endpoint states --


def _token_response(expires_in: object) -> httpx.Response:
    return httpx.Response(200, json={"access_token": "t", "token_type": "Bearer", "expires_in": expires_in})


@pytest.mark.parametrize(
    ("expires_in", "expected"),
    [(3600, 3600), (1e18, _MAX_TTL_SECONDS), ("NaN", 300), ("1e400", 300), (-5, 0), ("9" * 400, 300)],
)
def test_a_cached_token_lifetime_is_bounded(expires_in, expected):
    _token, ttl = parse_token_response(_token_response(expires_in))
    assert math.isfinite(ttl) and ttl == expected


@pytest.mark.parametrize("expires_in", [10**15, "9" * 30, "9" * 5000, float("inf"), "abc"])
def test_a_stored_token_expiry_never_overflows(expires_in):
    target = SimpleNamespace(token_expires_at=None)
    apply_oauth_token_expiry(target, {"expires_in": expires_in, "refresh_token": "rt"})
    assert target.token_expires_at <= datetime.now(UTC) + timedelta(seconds=MAX_TOKEN_LIFETIME_SECONDS + 5)


# -- Tool scopes of integrations that are each their own API --


def _tool(name: str, entity_id: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=name,
        input_schema={"type": "object", "properties": {}},
        source="integration:openapi",
        entity_id=entity_id,
    )


_TOOLS = [
    _tool("pets_list", "petstore"),
    _tool("pets_delete", "petstore"),
    _tool("erp_invoices", "erp"),
    _tool("erp_orders", "erp"),
]


def _names(allowed: dict) -> set[str]:
    return {t.name for t in filter_integration_tools(_TOOLS, allowed)}


def test_a_provider_wide_list_only_restricts_the_integration_it_names():
    assert _names({"openapi": ["pets_list"]}) == {"pets_list", "erp_invoices", "erp_orders"}


def test_an_empty_provider_wide_list_restricts_every_integration():
    assert _names({"openapi": []}) == set()


def test_an_instance_list_restricts_its_integration():
    assert _names({instance_scope_key("openapi", "erp"): ["erp_orders"]}) == {
        "pets_list",
        "pets_delete",
        "erp_orders",
    }


def test_an_imported_instance_scope_keeps_its_restriction():
    """The source install's integration id means nothing here: folded, not dropped."""
    folded = fold_unbound_instance_scopes(
        {"openapi@1f0e-source": ["pets_list"], "github": ["list_issues"]}, set()
    )
    assert folded == {"openapi": ["pets_list"], "github": ["list_issues"]}
    assert _names(folded) == {"pets_list", "erp_invoices", "erp_orders"}


def test_a_bound_instance_scope_is_kept_as_it_is():
    allowed = {"openapi@erp": ["erp_orders"]}
    assert fold_unbound_instance_scopes(allowed, {"erp"}) == allowed


def test_only_empty_imported_scopes_fold_into_an_empty_list():
    assert fold_unbound_instance_scopes({"openapi@a": [], "openapi@b": []}, set()) == {"openapi": []}
