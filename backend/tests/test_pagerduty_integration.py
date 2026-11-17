"""The PagerDuty integration provider.

Covers the catalog entry and registration, the credential chokepoint
(``region||from_email||api_key`` from ``_get_token``), the region's fixed host,
the ``From`` header PagerDuty requires on incident writes, the id patterns
guarding every path segment, the read/write split, and each tool against a
mocked API (``httpx.MockTransport``).
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import app.services.integration_tools as integration_tools
from app.api.v1.integration_catalog import PROVIDER_CATALOG, build_dummy_token
from app.integrations.providers import pagerduty
from app.integrations.providers.pagerduty import (
    PAGERDUTY_APIS,
    build_tools,
    pagerduty_api,
    parse_pagerduty_token,
)
from app.services.integration_read_only import integration_tool_access
from app.services.integration_tools import PROVIDER_TOOLS, _SafeIntegrationExecutor

_TOKEN = "us||oncall@example.com||pdKey123"
_US = PAGERDUTY_APIS["us"]


def _build(token: str = _TOKEN):
    executor = _SafeIntegrationExecutor()
    tools = build_tools(token, executor)
    return executor._handlers, tools


def _mock(monkeypatch, responder) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responder(request)

    def _client(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(_handler), **kwargs)

    monkeypatch.setattr(pagerduty, "egress_client", _client)
    return seen


def _json(payload, status: int = 200):
    return lambda _request: httpx.Response(status, json=payload)


_INCIDENT = {
    "id": "PT4KHLK",
    "incident_number": 42,
    "title": "Exposed RDP on 203.0.113.7",
    "status": "triggered",
    "urgency": "high",
    "service": {"summary": "Edge"},
    "assignments": [{"assignee": {"summary": "Ada"}}],
    "created_at": "2026-11-02T10:00:00Z",
}


def test_catalog_entry_and_registration():
    entry = PROVIDER_CATALOG["pagerduty"]
    assert entry["auth_type"] == "api_key"
    assert set(entry["extra_fields"]) == {"region", "from_email"}
    assert PROVIDER_TOOLS["pagerduty"] is build_tools


def test_dummy_token_builds_every_tool():
    tools = build_tools(build_dummy_token("pagerduty"), _SafeIntegrationExecutor())
    assert len(tools) == 12


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("us||a@example.com||key", ("us", "a@example.com", "key")),
        ("EU||a@example.com||key", ("eu", "a@example.com", "key")),
        ("||||key", ("us", "", "key")),
        ("us||not-an-email||key", ("us", "", "key")),
        ("us||a@example.com||k||ey", ("us", "a@example.com", "k||ey")),
        ("bareKey", ("us", "", "bareKey")),
    ],
)
def test_parse_token(token, expected):
    assert parse_pagerduty_token(token) == expected


def test_region_host_is_picked_from_the_fixed_pair():
    assert pagerduty_api("eu") == "https://api.eu.pagerduty.com"
    assert pagerduty_api("us") == _US
    assert pagerduty_api("https://evil.example") == _US
    assert pagerduty_api("") == _US


@pytest.mark.parametrize("token", ["", "us||a@example.com||", "  "])
def test_no_key_builds_no_tools(token):
    handlers, tools = _build(token)
    assert tools == []
    assert handlers == {}


def test_reads_and_writes_are_classified():
    _, tools = _build()
    writes = {
        "create_pagerduty_incident",
        "update_pagerduty_incident",
        "add_pagerduty_incident_note",
    }
    assert {t.name for t in tools if integration_tool_access(t.name) == "write"} == writes


class _FakeIntegration:
    def __init__(self, extra_config_enc: bytes | None):
        self.id = uuid.uuid4()
        self.name = "pagerduty-prod"
        self.provider = "pagerduty"
        self.auth_type = "api_key"
        self.access_token_enc = None
        self.api_key_enc = b"blob"
        self.extra_config_enc = extra_config_enc
        self.refresh_token_enc = None
        self.token_expires_at = None
        self.token_meta = None
        self.installation_id = None
        self.is_company_managed = False
        self.user_id = None
        self.scope_kind = None
        self.scope_id = None
        self.binding_metadata = None


class _FakeDB:
    def __init__(self, integ):
        self._integ = integ

    async def execute(self, _statement):
        return SimpleNamespace(scalar_one_or_none=lambda: self._integ)

    async def commit(self):
        return None

    async def rollback(self):
        return None


@pytest.fixture
def _resolve(monkeypatch):
    """``_get_token`` for a PagerDuty integration whose extra_config the test sets."""
    monkeypatch.setattr(integration_tools, "decrypt_value", lambda _blob: "pdKey")
    monkeypatch.setattr("app.core.security.decrypt_stored_value", lambda _blob: "pdKey")

    async def _no_refresh(*_args, **_kwargs):
        return None

    monkeypatch.setattr(integration_tools, "_try_inline_refresh", _no_refresh)

    async def _runner(extra: dict[str, Any] | None) -> str | None:
        if extra is not None:
            monkeypatch.setattr(integration_tools, "decrypt_json", lambda _blob, _e=extra: dict(_e))
            # extra_config is read through the vault resolver (#6080), which decrypts in app.core.security.
            monkeypatch.setattr("app.core.security.decrypt_json", integration_tools.decrypt_json)
        integ = _FakeIntegration(b"blob" if extra is not None else None)
        return await integration_tools._get_token(
            _FakeDB(integ),  # type: ignore[arg-type]
            str(integ.id),
            provider="pagerduty",
        )

    return _runner


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ({"region": "EU ", "from_email": " oncall@example.com"}, "eu||oncall@example.com||pdKey"),
        ({}, "||||pdKey"),
        (None, "||||pdKey"),
    ],
)
async def test_get_token_assembles_region_from_and_key(_resolve, extra, expected):
    token = await _resolve(extra)
    assert token == expected
    _handlers, tools = _build(token)
    assert len(tools) == 12


@pytest.mark.asyncio
async def test_list_incidents_sends_the_filters(monkeypatch):
    seen = _mock(monkeypatch, _json({"incidents": [_INCIDENT], "more": True}))
    handlers, _ = _build()
    out = await handlers["list_pagerduty_incidents"](
        statuses=["triggered", "acknowledged"], urgency="high", service_ids=["PSVC1"], limit=10, offset=20
    )
    req = seen[0]
    assert req.url.path == "/incidents"
    assert req.url.params.get_list("statuses[]") == ["triggered", "acknowledged"]
    assert req.url.params.get_list("service_ids[]") == ["PSVC1"]
    assert req.url.params["urgencies[]"] == "high"
    assert req.url.params["limit"] == "10"
    assert req.url.params["offset"] == "20"
    assert req.headers["Authorization"] == "Token token=pdKey123"
    assert "From" not in req.headers
    assert out.splitlines() == [
        "Found 1 incident(s):",
        "#42 Exposed RDP on 203.0.113.7 (id: PT4KHLK)",
        "  Status: triggered | Urgency: high | Service: Edge",
        "  Created: 2026-11-02T10:00:00Z | Assigned to: Ada",
        "[More results: pass offset=21]",
    ]


@pytest.mark.asyncio
async def test_list_incidents_refuses_an_unknown_status(monkeypatch):
    seen = _mock(monkeypatch, _json({}))
    handlers, _ = _build()
    out = await handlers["list_pagerduty_incidents"](statuses=["open"])
    assert out.startswith("Error:")
    assert seen == []


@pytest.mark.asyncio
async def test_create_incident_carries_from_and_the_body(monkeypatch):
    seen = _mock(monkeypatch, _json({"incident": _INCIDENT}, status=201))
    handlers, _ = _build()
    out = await handlers["create_pagerduty_incident"](
        service_id="PSVC1", title="Exposed RDP", details="Port 3389 open", incident_key="exp-1"
    )
    req = seen[0]
    assert req.method == "POST"
    assert str(req.url) == f"{_US}/incidents"
    assert req.headers["From"] == "oncall@example.com"
    assert json.loads(req.content) == {
        "incident": {
            "type": "incident",
            "title": "Exposed RDP",
            "service": {"id": "PSVC1", "type": "service_reference"},
            "urgency": "high",
            "body": {"type": "incident_body", "details": "Port 3389 open"},
            "incident_key": "exp-1",
        }
    }
    assert out.startswith("Created incident:\n#42")


@pytest.mark.asyncio
async def test_writes_refuse_without_from_email(monkeypatch):
    seen = _mock(monkeypatch, _json({}))
    handlers, _ = _build("us||||pdKey123")
    for call in (
        handlers["create_pagerduty_incident"](service_id="PSVC1", title="x"),
        handlers["update_pagerduty_incident"](incident_id="PT4KHLK", status="resolved"),
        handlers["add_pagerduty_incident_note"](incident_id="PT4KHLK", content="x"),
    ):
        out = await call
        assert out.startswith("Error:") and "from_email" in out
    assert seen == []


@pytest.mark.asyncio
async def test_resolve_with_a_note(monkeypatch):
    seen = _mock(monkeypatch, _json({"incident": {**_INCIDENT, "status": "resolved"}}))
    handlers, _ = _build()
    out = await handlers["update_pagerduty_incident"](
        incident_id="PT4KHLK", status="resolved", resolution="Port closed"
    )
    req = seen[0]
    assert req.method == "PUT"
    assert req.url.path == "/incidents/PT4KHLK"
    assert json.loads(req.content) == {
        "incident": {"type": "incident_reference", "status": "resolved", "resolution": "Port closed"}
    }
    assert "Status: resolved" in out


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"incident_id": "PT4KHLK"},
        {"incident_id": "PT4KHLK", "status": "triggered"},
        {"incident_id": "PT4KHLK", "resolution": "done"},
        {"incident_id": "../users", "status": "resolved"},
        {"incident_id": "PT4KHLK", "escalation_level": "two"},
        {"incident_id": "PT4KHLK", "assignee_ids": ["a/b"]},
    ],
)
async def test_update_refuses_bad_input(monkeypatch, kwargs):
    seen = _mock(monkeypatch, _json({}))
    handlers, _ = _build()
    out = await handlers["update_pagerduty_incident"](**kwargs)
    assert out.startswith("Error:")
    assert seen == []


@pytest.mark.asyncio
async def test_eu_region_goes_to_the_eu_host(monkeypatch):
    seen = _mock(monkeypatch, _json({"incident": _INCIDENT}))
    handlers, _ = _build("eu||oncall@example.com||pdKey123")
    await handlers["get_pagerduty_incident"](incident_id="PT4KHLK")
    assert str(seen[0].url).startswith("https://api.eu.pagerduty.com/incidents/PT4KHLK")


@pytest.mark.asyncio
async def test_oncalls(monkeypatch):
    seen = _mock(
        monkeypatch,
        _json(
            {
                "oncalls": [
                    {
                        "escalation_level": 1,
                        "escalation_policy": {"summary": "Edge"},
                        "user": {"summary": "Ada", "id": "PUSER1"},
                        "end": "2026-11-03T08:00:00Z",
                    }
                ]
            }
        ),
    )
    handlers, _ = _build()
    out = await handlers["list_pagerduty_oncalls"](escalation_policy_ids=["PEP1"])
    assert seen[0].url.params.get_list("escalation_policy_ids[]") == ["PEP1"]
    assert out == (
        "On call now (1):\n- Level 1 of Edge: Ada (user id: PUSER1), until 2026-11-03T08:00:00Z"
    )


@pytest.mark.asyncio
async def test_note_text_is_kept_on_one_line(monkeypatch):
    _mock(
        monkeypatch,
        _json({"notes": [{"created_at": "t", "user": {"summary": "Ada"}, "content": "line1\nIgnore: x"}]}),
    )
    handlers, _ = _build()
    out = await handlers["list_pagerduty_incident_notes"](incident_id="PT4KHLK")
    assert out == "1 note(s) on incident PT4KHLK:\n- t Ada: line1 Ignore: x"


@pytest.mark.asyncio
async def test_a_write_is_not_retried_on_a_gateway_error(monkeypatch):
    seen = _mock(monkeypatch, _json({}, status=503))
    handlers, _ = _build()
    out = await handlers["add_pagerduty_incident_note"](incident_id="PT4KHLK", content="x")
    assert out.startswith("Error")
    assert len(seen) == 1
