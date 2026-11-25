"""#2386: Defender tools say why Microsoft Entra ID refused a token.

A missing admin consent on one audience (Defender for Endpoint) used to answer
the generic "re-authorize" text, which cannot fix it; the connection test
checked only the Graph audience and passed.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.api.v1 import integration_testing as it
from app.integrations import entra_token_errors as ete
from app.integrations.granted_scopes import missing_tool_scopes
from app.integrations.providers import defender as df
from app.tools.base import CallableToolExecutor, ToolCall


class _Acquirer:
    """A delegated token acquirer: Graph works, MDE fails with *failure*."""

    def __init__(self, failure: ete.EntraTokenFailure | None):
        self._failure = failure

    async def __call__(self, scope: str) -> str | None:
        return "graph-token" if scope == df._GRAPH_SCOPE else None

    def failure(self, scope: str) -> ete.EntraTokenFailure | None:
        return self._failure if scope == df._MDE_SCOPE else None

    async def forget(self, scope: str) -> None:
        return None


class _Resp:
    status_code = 200

    def json(self) -> dict[str, Any]:
        return {"Results": [], "Schema": []}


async def _ok_request(_client, method, url, provider, action, **kwargs):
    return _Resp(), None


async def _call(executor: CallableToolExecutor, name: str, **arguments: Any) -> str:
    result = await executor.execute_tool(ToolCall(id="c", name=name, arguments=arguments))
    return result.content if isinstance(result.content, str) else str(result.content)


def _executor(monkeypatch, acquirer: _Acquirer) -> CallableToolExecutor:
    monkeypatch.setattr(df, "safe_request", _ok_request)
    executor = CallableToolExecutor()
    df.build_tools(token="", executor=executor, token_acquirer=acquirer)
    return executor


def test_classify_consent_grant_and_client():
    consent = ete.classify(
        400,
        '{"error":"invalid_grant","error_description":"AADSTS65001: The user or administrator has not consented",'
        '"error_codes":[65001]}',
    )
    assert consent.kind == "consent" and consent.aadsts == "65001"
    assert ete.classify(400, {"error": "invalid_scope"}).kind == "consent"
    grant = ete.classify(400, {"error": "invalid_grant", "error_description": "AADSTS700082: expired"})
    assert grant.kind == "grant" and grant.aadsts == "700082"
    assert ete.classify(401, {"error": "invalid_client"}).kind == "client"
    assert "AADSTS65001" in consent.code()


@pytest.mark.asyncio
async def test_consent_missing_on_mde_names_admin_consent_and_hunting_still_works(monkeypatch):
    failure = ete.classify(400, {"error": "invalid_grant", "error_codes": [65001]})
    executor = _executor(monkeypatch, _Acquirer(failure))

    indicators = await _call(executor, "list_defender_indicators")
    assert "admin consent" in indicators
    assert "Defender for Endpoint" in indicators
    assert "will not help" in indicators
    assert "Re-authorize this integration" not in indicators

    hunting = await _call(executor, "run_defender_hunting_query", query="DeviceEvents | take 1")
    assert not hunting.startswith("Error")


@pytest.mark.asyncio
async def test_expired_refresh_token_still_asks_to_reauthorize(monkeypatch):
    failure = ete.classify(400, {"error": "invalid_grant", "error_codes": [700082]})
    executor = _executor(monkeypatch, _Acquirer(failure))

    answer = await _call(executor, "list_defender_indicators")
    assert "Re-authorize this integration" in answer
    assert "admin consent" not in answer


def test_app_only_wrong_credentials_name_tenant_client_and_secret():
    failure = ete.classify(401, {"error": "invalid_client", "error_codes": [7000215]})
    message = df.token_failure_message(df._MDE_SCOPE, failure, delegated=False)
    assert "tenant ID" in message and "client ID" in message and "client secret" in message


def test_messages_never_echo_a_secret():
    failure = ete.classify(400, '{"error":"invalid_grant","error_description":"client_secret=s3cr3t AADSTS65001"}')
    message = df.token_failure_message(df._MDE_SCOPE, failure, delegated=True)
    assert "s3cr3t" not in message


@pytest.mark.asyncio
async def test_connection_test_reports_mde_failure_while_graph_passes(monkeypatch):
    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            return _Resp()

    monkeypatch.setattr(it, "egress_client", lambda **_k: _Client())

    async def _redeem(holder: str, scope: str):
        return None, ete.classify(400, {"error": "invalid_scope"})

    import app.services.integration_tools as tools

    monkeypatch.setattr(tools, "redeem_azure_delegated_audience", _redeem)

    result = await it._defender_delegated_test(("https://graph.example/incidents", {}), "holder-1", "Defender")
    assert result["status"] == "error"
    assert "Microsoft Graph: OK" in result["message"]
    assert "Defender for Endpoint:" in result["message"]
    assert "admin consent" in result["message"]


def test_missing_tool_permissions_name_the_tools():
    entry = [
        {"any_of": ["CustomDetection.ReadWrite.All"], "tools": ["create_defender_detection_rule"]},
        {
            "any_of": ["CustomDetection.Read.All", "CustomDetection.ReadWrite.All"],
            "tools": ["list_defender_detection_rules"],
        },
    ]
    lacking = missing_tool_scopes(entry, "SecurityIncident.ReadWrite.All CustomDetection.Read.All")
    assert lacking == ["CustomDetection.ReadWrite.All (create_defender_detection_rule)"]
    assert missing_tool_scopes(entry, None) == []


def test_token_permissions_reads_scp_and_roles():
    import base64
    import json

    body = base64.urlsafe_b64encode(json.dumps({"scp": "A.Read B.Write", "roles": ["C.All"]}).encode()).decode()
    assert ete.token_permissions(f"h.{body.rstrip('=')}.s") == {"a.read", "b.write", "c.all"}
    assert ete.token_permissions("opaque") is None
