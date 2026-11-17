"""The XTM Hub import bundle (``scripts/build_xtm_hub_bundle.py``).

Built, parsed by the import rules, imported through the real route, and its
tools registered from the stored definition and called: the request that
leaves is the bundle's fixed GraphQL query with the model's variables.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

from app.integrations import base_url_policy
from app.integrations.providers import xtm_hub as hub
from app.services import web_fetch
from app.services.declarative_http_executor import validate_execution_spec
from app.services.integration_bundle_manager import parse_integration_bundle_zip
from app.services.integration_tools import _register_custom_declared_tools
from app.tools.base import CallableToolExecutor, ToolCall
from scripts import build_xtm_hub_bundle as bundle
from tests.test_integration_bundles_api import bundles_client  # noqa: F401 - fixture

_STAMP = "2026-10-02T00:00:00+00:00"
_TOOLS = [
    "list_xtm_hub_catalogs",
    "search_xtm_hub_resources",
    "get_xtm_hub_resource",
    "list_xtm_hub_newest_resources",
    "list_xtm_hub_most_deployed_resources",
    "list_xtm_hub_use_cases",
]


def test_the_bundle_satisfies_the_import_rules():
    parsed = parse_integration_bundle_zip(bundle.build_bundle(_STAMP))
    meta = parsed.metadata
    assert (meta.format, meta.version, meta.provider, meta.slug) == (
        "filigran-integration-v1",
        bundle.BUNDLE_VERSION,
        "xtm-hub",
        "xtm-hub",
    )
    assert (meta.auth_type, meta.author, meta.exported_at) == ("none", "Filigran", _STAMP)
    assert parsed.icon_bytes == bundle.LOGO.read_bytes()
    assert [(s.key, s.type, s.required) for s in meta.settings] == [("base_url", "string", True)]
    assert (meta.test_connection.mode, meta.test_connection.method, meta.test_connection.path) == (
        "http",
        "GET",
        "/en/cybersecurity-solutions",
    )
    instance = parsed.integration_instance
    assert (instance.name, instance.auth_type, instance.extra_config, instance.is_company_managed) == (
        "XTM Hub",
        "none",
        {"base_url": "https://hub.filigran.io"},
        False,
    )
    assert [t["name"] for t in parsed.metadata_raw["tools"]] == _TOOLS


def test_every_tool_posts_a_fixed_query_and_only_the_variables_are_free():
    for index, tool in enumerate(bundle.bundle_tools()):
        execution = tool["execution"]
        assert validate_execution_spec(execution, tool_index=index) == []
        assert (execution["method"], execution["path"], execution["auth"]) == (
            "POST",
            "/graphql-api",
            {"type": "none"},
        )
        mapping = execution["body_mapping"]
        assert set(mapping["query"]) == {"value"}
        assert mapping["query"]["value"].lstrip().startswith("query ")
        assert "mutation" not in mapping["query"]["value"]
        assert set(mapping) <= {"query", "variables"}
        assert set(tool["input_schema"]["properties"]) <= {"variables"}
        # Hub text reaches the model as untrusted data, read bounded.
        assert (execution["untrusted_response"], execution["max_response_bytes"]) == (
            True,
            hub._MAX_RESPONSE_BYTES,
        )


def test_the_bundle_selects_what_the_native_provider_selects():
    queries = {t["name"]: t["execution"]["body_mapping"]["query"]["value"] for t in bundle.bundle_tools()}
    assert queries["list_xtm_hub_catalogs"] == hub.CATALOGS_QUERY
    assert hub.DETAIL_FIELDS in queries["get_xtm_hub_resource"]
    for name in (
        "search_xtm_hub_resources",
        "list_xtm_hub_newest_resources",
        "list_xtm_hub_most_deployed_resources",
    ):
        assert hub.SUMMARY_FIELDS in queries[name], name


def test_import_creates_the_custom_provider_and_a_connected_instance(bundles_client, monkeypatch):  # noqa: F811
    async def _public(host, port):
        return [(web_fetch.socket.AF_INET, web_fetch.socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    # The instance's base URL is held to the base URL policy on import.
    monkeypatch.setattr(base_url_policy, "_resolve", _public)
    client, db = bundles_client
    resp = client.post(
        "/api/v1/integrations/admin/import",
        files={"bundle": ("xtm-hub.zip", bundle.build_bundle(_STAMP), "application/zip")},
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert (payload["provider"], payload["version"], payload["upgraded"]) == (
        "custom:xtm-hub",
        bundle.BUNDLE_VERSION,
        False,
    )
    created = db.integrations[payload["integration_id"]]
    assert (created.provider, created.auth_type, created.status, created.name) == (
        "custom:xtm-hub",
        "none",
        "connected",
        "XTM Hub",
    )
    stored = db.custom_defs["custom:xtm-hub"].metadata_json
    assert "integration_instance" not in stored
    assert [t["name"] for t in stored["tools"]] == _TOOLS


@pytest.mark.asyncio
async def test_an_imported_tool_sends_its_constant_query_whatever_the_model_passes(monkeypatch):
    # The base URL policy resolves the host before the call.
    monkeypatch.setattr(
        web_fetch.socket,
        "getaddrinfo",
        lambda *a, **k: [(web_fetch.socket.AF_INET, web_fetch.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
    )
    metadata = bundle.bundle_metadata(_STAMP)
    executor = CallableToolExecutor()
    definitions = _register_custom_declared_tools(
        executor,
        provider="custom:xtm-hub",
        integration_id="00000000-0000-0000-0000-000000000001",
        metadata_json=metadata,
        integration_token="__no_auth__",
        integration_extra_config={"base_url": "https://hub.filigran.io/"},
    )
    assert [d.name for d in definitions] == _TOOLS

    variables = {"serviceInstanceId": "sid-1", "slug": "opencti-custom-dashboards", "search": "ransomware"}
    answer = {"data": {"publicDocuments": {"totalCount": 0, "pageInfo": {}, "edges": []}}}
    sent: list[httpx.Request] = []
    seen_kwargs: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=httpx.ByteStream(json.dumps(answer).encode()),
        )

    real_client = httpx.AsyncClient

    def factory(**kwargs):
        seen_kwargs.append(kwargs)
        return real_client(**{**kwargs, "transport": httpx.MockTransport(handler)})

    with patch("app.services.declarative_http_executor.egress_client", factory):
        result = await executor.execute_tool(
            ToolCall(
                id="c1",
                name="search_xtm_hub_resources",
                arguments={"variables": variables, "query": "mutation { contactUs(message: \"x\") { success } }"},
            )
        )

    assert not result.is_error
    assert len(sent) == 1
    request = sent[0]
    assert (request.method, str(request.url)) == ("POST", "https://hub.filigran.io/graphql-api")
    assert json.loads(request.content) == {"query": bundle.SEARCH_QUERY, "variables": variables}
    assert "authorization" not in request.headers
    assert request.headers["accept-encoding"] == "identity"
    assert seen_kwargs[0]["follow_redirects"] is False
    assert seen_kwargs[0]["trust_env"] is False
    # Pinned to the address the base URL policy checked.
    assert type(seen_kwargs[0]["transport"]) is web_fetch.GuardedTransport
    # The Hub's answer reaches the model inside an untrusted-content block.
    lines = result.content.splitlines()
    assert lines[0].startswith("Untrusted external content follows")
    assert lines[-1].startswith("<<END-UNTRUSTED-")
    assert json.dumps(answer, ensure_ascii=False) in result.content


def test_the_command_writes_the_zip(tmp_path):
    target = tmp_path / "hub.zip"
    assert bundle.main(["-o", str(target)]) == 0
    assert parse_integration_bundle_zip(target.read_bytes()).metadata.provider == "xtm-hub"
