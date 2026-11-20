"""manage_opencti_connector update_config merges with the stored configuration.

OpenCTI rebuilds a managed connector's whole configuration on every
managedConnectorEdit: a key it does not receive keeps its stored value only when
it is a secret (format password), every other key goes back to its contract
default. So update_config reads the stored configuration first and re-sends
every non-secret setting the caller did not change; a secret the caller did not
pass is never sent (OpenCTI keeps it), and the answer names keys, never values.
"""

import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

import app.integrations.providers.opencti.connectors as connectors_mod
from app.integrations.providers.opencti import build_tools
from app.tools.base import CallableToolExecutor, ToolCall, ToolResult

TOKEN = "https://demo.opencti.io||test-token"
CIPHERTEXT = "Q" * 120
SECRET_PLAINTEXT = "plain-secret-value"

_CONTRACT = {
    "title": "Tenant importer",
    "config_schema": {
        "type": "object",
        "required": ["TENANT_ID", "CLIENT_ID", "CLIENT_SECRET"],
        "properties": {
            "TENANT_ID": {"type": "string"},
            "CLIENT_ID": {"type": "string"},
            "CLIENT_SECRET": {"type": "string", "format": "password"},
            "DURATION_PERIOD": {"type": "string", "default": "PT1H"},
            "IMPORT_START_DATE": {"type": "string"},
            "API_KEY_HEADER": {"type": "string"},
        },
    },
}
_STORED = [
    {"key": "TENANT_ID", "value": "tenant-1"},
    {"key": "CLIENT_ID", "value": "client-1"},
    {"key": "CLIENT_SECRET", "value": CIPHERTEXT},
    {"key": "DURATION_PERIOD", "value": "PT6H"},
    {"key": "API_KEY_HEADER", "value": "X-Api"},
    {"key": "CONNECTOR_ID", "value": "c1"},
    {"key": "CONNECTOR_NAME", "value": "Importer"},
    {"key": "HTTPS_PROXY_REJECT_UNAUTHORIZED", "value": "true"},
]


class _Fake:
    def __init__(self, connector: dict[str, Any] | None, *, refuse_definition: bool = False) -> None:
        self.connector = connector
        self.refuse_definition = refuse_definition
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def __call__(
        self, _client: Any, _base: Any, _headers: Any, query: str, variables: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], str | None]:
        self.calls.append((query, variables))
        if "managedConnectorEdit" in query:
            return {"managedConnectorEdit": {"id": "c1", "name": "Importer"}}, None
        if self.refuse_definition and "manager_contract_definition" in query:
            return {}, 'Cannot query field "manager_contract_definition" on type "Connector".'
        return {"connector": self.connector}, None

    def edits(self) -> list[dict[str, Any]]:
        return [v["input"] for q, v in self.calls if "managedConnectorEdit" in q and v]

    def sent(self) -> dict[str, str]:
        return {c["key"]: c["value"] for c in self.edits()[-1]["manager_contract_configuration"]}


def _connector(**over: Any) -> dict[str, Any]:
    node = {
        "id": "c1",
        "name": "Importer",
        "connector_user_id": "user-1",
        "manager_contract_definition": json.dumps(_CONTRACT),
        "manager_contract_configuration": _STORED,
    }
    node.update(over)
    return node


def _build(monkeypatch: pytest.MonkeyPatch, fake: _Fake) -> CallableToolExecutor:
    @contextlib.asynccontextmanager
    async def _client(*_args: Any, **_kwargs: Any) -> AsyncIterator[None]:
        yield None

    monkeypatch.setattr(connectors_mod, "_client", _client)
    monkeypatch.setattr(connectors_mod, "_graphql", fake)
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor, live_writes=True)
    return executor


async def _update(executor: CallableToolExecutor, config: dict[str, Any], **more: Any) -> ToolResult:
    arguments = {
        "action": "update_config",
        "connector_id": "c1",
        "name": "Importer",
        "config": json.dumps(config),
        **more,
    }
    return await executor.execute_tool(ToolCall(id="call-1", name="manage_opencti_connector", arguments=arguments))


async def test_one_new_key_keeps_every_stored_setting_and_the_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Fake(_connector())
    executor = _build(monkeypatch, fake)
    result = await _update(executor, {"IMPORT_START_DATE": "2026-01-01"})
    assert not result.is_error, result.content
    assert fake.sent() == {
        "TENANT_ID": "tenant-1",
        "CLIENT_ID": "client-1",
        "DURATION_PERIOD": "PT6H",
        "API_KEY_HEADER": "X-Api",
        "IMPORT_START_DATE": "2026-01-01",
    }
    assert fake.edits()[-1]["connector_user_id"] == "user-1"
    assert "changed: IMPORT_START_DATE" in result.content
    assert "kept as stored: TENANT_ID, CLIENT_ID, DURATION_PERIOD, API_KEY_HEADER" in result.content
    assert "secret preserved: CLIENT_SECRET" in result.content
    assert CIPHERTEXT not in result.content


async def test_a_required_secret_opencti_does_not_serve_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    # OpenCTI filters encrypted values out of Connector.manager_contract_configuration.
    served = [item for item in _STORED if item["key"] != "CLIENT_SECRET"]
    fake = _Fake(_connector(manager_contract_configuration=served))
    executor = _build(monkeypatch, fake)
    result = await _update(executor, {"IMPORT_START_DATE": "2026-01-01"})
    assert not result.is_error, result.content
    assert "CLIENT_SECRET" not in fake.sent()
    assert "secret preserved: CLIENT_SECRET" in result.content


async def test_a_new_secret_value_is_sent_and_never_echoed(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Fake(_connector())
    executor = _build(monkeypatch, fake)
    result = await _update(executor, {"CLIENT_SECRET": SECRET_PLAINTEXT})
    assert not result.is_error, result.content
    assert fake.sent()["CLIENT_SECRET"] == SECRET_PLAINTEXT
    assert "changed: CLIENT_SECRET" in result.content
    assert "secret preserved" not in result.content
    assert SECRET_PLAINTEXT not in result.content


async def test_replace_sends_config_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Fake(_connector())
    executor = _build(monkeypatch, fake)
    result = await _update(executor, {"TENANT_ID": "tenant-2"}, replace=True)
    assert not result.is_error, result.content
    assert fake.sent() == {"TENANT_ID": "tenant-2"}
    assert "Replaced the configuration" in result.content
    assert "reset to the contract default: CLIENT_ID, DURATION_PERIOD, API_KEY_HEADER" in result.content
    assert "secret preserved: CLIENT_SECRET" in result.content


async def test_an_older_opencti_without_the_contract_still_merges(monkeypatch: pytest.MonkeyPatch) -> None:
    node = _connector()
    del node["manager_contract_definition"]
    fake = _Fake(node, refuse_definition=True)
    executor = _build(monkeypatch, fake)
    result = await _update(executor, {"IMPORT_START_DATE": "2026-01-01"})
    assert not result.is_error, result.content
    sent = fake.sent()
    assert "CLIENT_SECRET" not in sent
    assert "CONNECTOR_ID" not in sent and "HTTPS_PROXY_REJECT_UNAUTHORIZED" not in sent
    assert sent["DURATION_PERIOD"] == "PT6H"
    assert "secret preserved: CLIENT_SECRET" in result.content


@pytest.mark.parametrize("placeholder", ["<encrypted>", "<redacted (secret-like key)>"])
async def test_a_get_placeholder_is_refused_and_nothing_is_sent(
    monkeypatch: pytest.MonkeyPatch, placeholder: str
) -> None:
    fake = _Fake(_connector())
    executor = _build(monkeypatch, fake)
    result = await _update(executor, {"CLIENT_SECRET": placeholder})
    assert "nothing was written" in result.content
    assert "CLIENT_SECRET" in result.content
    assert fake.calls == []


async def test_an_unknown_connector_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Fake(None)
    executor = _build(monkeypatch, fake)
    result = await _update(executor, {"TENANT_ID": "x"})
    assert "not found" in result.content
    assert fake.edits() == []
