"""The Zendesk integration provider.

Covers the catalog entry and registration, the credential chokepoint
(``subdomain||email||api_token``), the host built from the subdomain only, the
Basic ``<email>/token`` header, the read/write split, and each tool against a
mocked API (``httpx.MockTransport``).
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from app.api.v1.integration_catalog import PROVIDER_CATALOG, build_dummy_token
from app.integrations.providers import zendesk
from app.integrations.providers.zendesk import (
    build_tools,
    build_zendesk_headers,
    parse_zendesk_token,
    zendesk_subdomain,
)
from app.services.integration_read_only import integration_tool_access
from app.services.integration_tools import PROVIDER_TOOLS, _SafeIntegrationExecutor

_TOKEN = "acme||agent@example.com||zdTok"
_API = "https://acme.zendesk.com/api/v2"


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

    monkeypatch.setattr(zendesk, "egress_client", _client)
    return seen


def _json(payload, status: int = 200):
    return lambda _request: httpx.Response(status, json=payload)


_TICKET = {
    "id": 101,
    "subject": "Patch exposed Exchange",
    "status": "open",
    "priority": "urgent",
    "type": "task",
    "assignee_id": 7,
    "requester_id": 9,
    "tags": ["exposure", "cve_2024_21410"],
}


def test_catalog_entry_and_registration():
    entry = PROVIDER_CATALOG["zendesk"]
    assert entry["auth_type"] == "api_key"
    assert set(entry["extra_fields"]) == {"subdomain", "email"}
    assert PROVIDER_TOOLS["zendesk"] is build_tools


def test_dummy_token_builds_every_tool():
    assert len(build_tools(build_dummy_token("zendesk"), _SafeIntegrationExecutor())) == 20


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("acme", "acme"),
        ("ACME.zendesk.com", "acme"),
        ("https://acme.zendesk.com/agent/tickets", "acme"),
        ("acme.evil.com", None),
        ("evil.com/x.zendesk.com", None),
        ("-acme", None),
        ("", None),
    ],
)
def test_subdomain(raw, expected):
    assert zendesk_subdomain(raw) == expected


@pytest.mark.parametrize(
    "token",
    ["", "acme||||tok", "||agent@example.com||tok", "acme||agent@example.com||", "evil.com||agent@example.com||tok"],
)
def test_incomplete_token_builds_no_tools(token):
    handlers, tools = _build(token)
    assert tools == []
    assert handlers == {}


def test_token_keeps_pipes_in_the_secret():
    assert parse_zendesk_token("acme||a@example.com||t||k") == ("acme", "a@example.com", "t||k")


def test_basic_header_is_email_slash_token():
    header = build_zendesk_headers("agent@example.com", "zdTok")["Authorization"]
    assert base64.b64decode(header.removeprefix("Basic ")) == b"agent@example.com/token:zdTok"


def test_reads_and_writes_are_classified():
    _, tools = _build()
    writes = {
        "create_zendesk_ticket",
        "update_zendesk_ticket",
        "delete_zendesk_ticket",
        "restore_zendesk_ticket",
        "create_zendesk_user",
        "update_zendesk_user",
        "delete_zendesk_user",
        "create_zendesk_organization",
        "update_zendesk_organization",
        "delete_zendesk_organization",
    }
    assert {t.name for t in tools if integration_tool_access(t.name) == "write"} == writes


@pytest.mark.asyncio
async def test_search_tickets_builds_the_query(monkeypatch):
    seen = _mock(monkeypatch, _json({"results": [_TICKET], "count": 3, "next_page": "x"}))
    handlers, _ = _build()
    out = await handlers["search_zendesk_tickets"](query="exchange", status="open", tags=["exposure"], limit=1)
    req = seen[0]
    assert str(req.url).startswith(f"{_API}/search.json?")
    assert req.url.params["query"] == "type:ticket status:open tags:exposure exchange"
    assert req.url.params["per_page"] == "1"
    assert req.headers["Authorization"] == build_zendesk_headers("agent@example.com", "zdTok")["Authorization"]
    assert out.splitlines() == [
        "Found 3 ticket(s), showing 1:",
        "#101 Patch exposed Exchange [open]",
        "  Priority: urgent | Type: task | Assignee id: 7 | Requester id: 9",
        "  Tags: exposure, cve_2024_21410",
        "[More results: pass page=2]",
    ]


@pytest.mark.asyncio
async def test_search_refuses_an_unknown_status(monkeypatch):
    seen = _mock(monkeypatch, _json({}))
    handlers, _ = _build()
    out = await handlers["search_zendesk_tickets"](status="done")
    assert out.startswith("Error:")
    assert seen == []


@pytest.mark.asyncio
async def test_create_ticket_body(monkeypatch):
    seen = _mock(monkeypatch, _json({"ticket": _TICKET}, status=201))
    handlers, _ = _build()
    out = await handlers["create_zendesk_ticket"](
        subject="Patch exposed Exchange", description="CVE-2024-21410 on mail.example.com",
        priority="urgent", tags=["exposure", "cve 2024"], external_id="finding-9",
    )
    assert seen[0].method == "POST"
    assert str(seen[0].url) == f"{_API}/tickets.json"
    assert json.loads(seen[0].content) == {
        "ticket": {
            "subject": "Patch exposed Exchange",
            "comment": {"body": "CVE-2024-21410 on mail.example.com"},
            "priority": "urgent",
            "tags": ["exposure", "cve_2024"],
            "external_id": "finding-9",
        }
    }
    assert out.startswith("Created ticket:\n#101")


@pytest.mark.asyncio
async def test_update_comment_is_internal_by_default(monkeypatch):
    seen = _mock(monkeypatch, _json({"ticket": {**_TICKET, "status": "solved"}}))
    handlers, _ = _build()
    await handlers["update_zendesk_ticket"](ticket_id=101, status="solved", comment="Patched")
    assert json.loads(seen[0].content) == {
        "ticket": {"status": "solved", "comment": {"body": "Patched", "public": False}}
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"ticket_id": 101},
        {"ticket_id": 101, "status": "closed"},
        {"ticket_id": "../users", "status": "open"},
        {"ticket_id": 0, "status": "open"},
        {"ticket_id": 101, "priority": "p1"},
        {"ticket_id": 101, "assignee_id": "bob"},
    ],
)
async def test_update_refuses_bad_input(monkeypatch, kwargs):
    seen = _mock(monkeypatch, _json({}))
    handlers, _ = _build()
    out = await handlers["update_zendesk_ticket"](**kwargs)
    assert out.startswith("Error:")
    assert seen == []


@pytest.mark.asyncio
async def test_get_ticket_with_comments(monkeypatch):
    def _respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/comments.json"):
            return httpx.Response(
                200,
                json={"comments": [{"created_at": "t", "author_id": 9, "public": False, "plain_body": "a\nb"}]},
            )
        return httpx.Response(200, json={"ticket": {**_TICKET, "description": "Found by scan"}})

    seen = _mock(monkeypatch, _respond)
    handlers, _ = _build()
    out = await handlers["get_zendesk_ticket"](ticket_id=101)
    assert [r.url.path for r in seen] == ["/api/v2/tickets/101.json", "/api/v2/tickets/101/comments.json"]
    assert "  Description:\nFound by scan" in out
    assert out.endswith("Comments on #101, newest first (1):\n- t author 9 (internal): a b")


@pytest.mark.asyncio
async def test_restore_ticket(monkeypatch):
    seen = _mock(monkeypatch, _json({}))
    handlers, _ = _build()
    out = await handlers["restore_zendesk_ticket"](ticket_id=101)
    assert seen[0].method == "PUT"
    assert seen[0].url.path == "/api/v2/deleted_tickets/101/restore.json"
    assert out == "Restored ticket 101."


@pytest.mark.asyncio
async def test_search_users(monkeypatch):
    seen = _mock(
        monkeypatch,
        _json({"users": [{"id": 9, "name": "Ada", "email": "ada@example.com", "role": "agent", "organization_id": 3}]}),
    )
    handlers, _ = _build()
    out = await handlers["search_zendesk_users"](query="ada")
    assert seen[0].url.path == "/api/v2/users/search.json"
    assert out == "Users (1):\n- Ada (id: 9): ada@example.com, role agent, organization id 3"


@pytest.mark.asyncio
async def test_create_user_refuses_a_bad_email(monkeypatch):
    seen = _mock(monkeypatch, _json({}))
    handlers, _ = _build()
    out = await handlers["create_zendesk_user"](name="Ada", email="not-an-email")
    assert out.startswith("Error:")
    assert seen == []


@pytest.mark.asyncio
async def test_a_write_is_not_retried_on_a_gateway_error(monkeypatch):
    seen = _mock(monkeypatch, _json({}, status=503))
    handlers, _ = _build()
    out = await handlers["delete_zendesk_ticket"](ticket_id=101)
    assert out.startswith("Error")
    assert len(seen) == 1
