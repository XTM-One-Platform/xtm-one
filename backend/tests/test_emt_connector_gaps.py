"""Connector gaps found building a delegated executive assistant.

* Microsoft Teams: a channel message posted by a workflow (an app, its text in
  an Adaptive Card), a system event or a device read back as author "Unknown"
  and an empty body - only ``from.user`` and the HTML body were read.
* Perk: cost centers live under ``/cost_centers``; the tool called
  ``/cost-centers`` and got "resource not found".
* LinkedIn: a Page (organization) token has no ``openid`` / ``profile`` scope,
  so the member-profile tool must say so instead of failing on
  ``/v2/userinfo``, and ``get_linkedin_organization`` without an id lists the
  Pages the token administers.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.integrations.providers import linkedin, microsoft, perk
from app.tools.base import CallableToolExecutor, ToolCall


class _Response:
    def __init__(self, payload: Any, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300

    def json(self) -> Any:
        return self._payload


async def _call(executor: CallableToolExecutor, name: str, **arguments):
    return await executor.execute_tool(ToolCall(id="call", name=name, arguments=arguments))


# ---------------------------------------------------------------------------
# Microsoft Teams
# ---------------------------------------------------------------------------

EMT_TEAM = "5b3c2a1d-6e7f-4a8b-9c0d-1e2f3a4b5c6d"
EMT_CHANNEL = "19:emt-channel@thread.tacv2"

WORKFLOW_CARD = {
    "type": "AdaptiveCard",
    "body": [
        {"type": "TextBlock", "text": "EMT weekly sync", "weight": "bolder"},
        {"type": "FactSet", "facts": [{"title": "Owner", "value": "Samuel"}]},
        {
            "type": "Container",
            "items": [
                {
                    "type": "RichTextBlock",
                    "inlines": [{"type": "TextRun", "text": "Decision: ship the release on Friday"}],
                }
            ],
        },
    ],
}


def _workflow_message(created: str) -> dict:
    """What Graph returns for a Workflows card post: no user, a placeholder body."""
    return {
        "id": "1759590900000",
        "messageType": "message",
        "createdDateTime": created,
        "from": {
            "user": None,
            "device": None,
            "application": {
                "id": "workflows-app",
                "displayName": "Workflows",
                "applicationIdentityType": "bot",
            },
        },
        "body": {"contentType": "html", "content": '<attachment id="card-1"></attachment>'},
        "attachments": [
            {
                "id": "card-1",
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": json.dumps(WORKFLOW_CARD),
            }
        ],
    }


def _system_message(created: str) -> dict:
    return {
        "id": "1759590960000",
        "messageType": "systemEventMessage",
        "createdDateTime": created,
        "from": None,
        "body": {"contentType": "html", "content": "<systemEventMessage/>"},
        "eventDetail": {
            "@odata.type": "#microsoft.graph.membersAddedEventMessageDetail",
            "members": [{"id": "u-2", "displayName": "Ada Lovelace"}],
            "initiator": {"user": {"id": "u-1", "displayName": "Samuel Hassine"}},
        },
    }


def test_a_workflow_card_message_names_the_app_and_reads_the_card():
    message = _workflow_message("2026-10-04T15:15:00Z")

    assert microsoft._teams._teams_message_author(message) == ("application", "Workflows")
    text = microsoft._teams._teams_message_text(message)
    assert "EMT weekly sync" in text
    assert "Owner: Samuel" in text
    assert "Decision: ship the release on Friday" in text


def test_a_system_event_names_its_initiator_and_the_event():
    message = _system_message("2026-10-04T15:16:00Z")

    assert microsoft._teams._teams_message_author(message) == ("system", "Samuel Hassine")
    text = microsoft._teams._teams_message_text(message)
    assert text.startswith("System event: members added")
    assert "Ada Lovelace" in text


def test_a_message_body_keeps_its_lines_and_decodes_its_entities():
    """A list stays one item per line and no entity reaches the agent."""
    message = {
        "from": {"user": {"displayName": "Ada"}},
        "body": {
            "contentType": "html",
            "content": "<p>Read live:</p><ul><li>one</li><li>two&nbsp;now</li></ul><p>R&amp;D</p>",
        },
    }

    assert microsoft._teams._teams_message_text(message) == "Read live:\n\n- one\n- two now\n\nR&D"


@pytest.mark.parametrize(
    ("message", "author", "text"),
    [
        (
            {"from": {"device": {"id": "d-1", "displayName": "Board room"}}, "body": {"content": "hello"}},
            ("device", "Board room"),
            "hello",
        ),
        (
            {"from": {"user": {"displayName": "Ada"}}, "body": {"content": ""}, "deletedDateTime": "2026-10-04T15:00:00Z"},
            ("user", "Ada"),
            "[message deleted]",
        ),
        (
            {
                "from": {"user": {"displayName": "Ada"}},
                "body": {"content": '<attachment id="q"></attachment> agreed'},
                "attachments": [
                    {
                        "id": "q",
                        "contentType": "messageReference",
                        "content": json.dumps(
                            {
                                "messagePreview": "Can we ship Friday?",
                                "messageSender": {"user": {"displayName": "Samuel"}},
                            }
                        ),
                    }
                ],
            },
            ("user", "Ada"),
            "agreed\n[quoting Samuel: Can we ship Friday?]",
        ),
        ({"body": {"content": ""}}, ("unknown", "Unknown sender"), "[no text content]"),
    ],
    ids=["device", "deleted", "quoted", "nothing"],
)
def test_every_kind_of_message_gets_an_author_type_and_a_text(message, author, text):
    assert microsoft._teams._teams_message_author(message) == author
    assert microsoft._teams._teams_message_text(message) == text


@pytest.mark.asyncio
async def test_the_emt_channel_message_returns_its_author_type_and_text(monkeypatch):
    """The reported read: the EMT channel, as the executive-assistant account."""
    recent = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    later = (datetime.now(UTC) - timedelta(minutes=50)).strftime("%Y-%m-%dT%H:%M:%SZ")
    urls: list[str] = []

    async def fake_safe_request(_client, _method, url, _provider, _op, **_kwargs):
        urls.append(url)
        if url.endswith("/messages"):
            return _Response({"value": [_workflow_message(recent), _system_message(later)]}), None
        return _Response({"value": []}), None

    monkeypatch.setattr(microsoft._common, "safe_request", fake_safe_request)
    executor = CallableToolExecutor()
    microsoft.build_tools(token="dummy", executor=executor)

    result = await _call(
        executor,
        "list_microsoft_channel_messages",
        team_id=EMT_TEAM,
        channel_id=EMT_CHANNEL,
        hours=24,
    )

    assert not result.is_error, result.content
    assert f"/teams/{EMT_TEAM}/channels/{EMT_CHANNEL}/messages" in urls[0]
    assert "Unknown" not in result.content
    assert "Workflows (application):" in result.content
    assert "EMT weekly sync" in result.content
    assert "Samuel Hassine (system, systemEventMessage):" in result.content
    assert "System event: members added" in result.content


def test_a_chat_message_from_a_bot_is_named_as_an_application():
    ((line,),) = microsoft._teams._format_chat_listing([("chat-1", [_workflow_message("2026-10-04T15:15:00Z")])])

    assert "Workflows (application):" in line
    assert "EMT weekly sync" in line


# ---------------------------------------------------------------------------
# Perk
# ---------------------------------------------------------------------------


def _perk(monkeypatch, payload: Any) -> tuple[CallableToolExecutor, list[dict]]:
    calls: list[dict] = []

    async def fake_safe_request(_client, method, url, _provider, _op, **kwargs):
        calls.append({"method": method, "url": url, "params": kwargs.get("params")})
        return _Response(payload), None

    monkeypatch.setattr(perk, "safe_request", fake_safe_request)
    executor = CallableToolExecutor()
    perk.build_tools("perk-key", executor)
    return executor, calls


@pytest.mark.asyncio
async def test_cost_centers_are_listed_from_the_cost_centers_endpoint(monkeypatch):
    executor, calls = _perk(
        monkeypatch,
        {
            "offset": 0,
            "limit": 50,
            "cost_centers": [
                {"id": "2", "name": "Marketing", "count_users": 3},
                {"id": "7", "name": "R&D", "count_users": 12, "erp_code": "CC-4210", "archived": True},
            ],
        },
    )

    result = await _call(executor, "list_perk_cost_centers")

    assert not result.is_error, result.content
    assert calls == [
        {
            "method": "get",
            "url": "https://api.travelperk.com/cost_centers",
            "params": {"limit": 50, "offset": 0},
        }
    ]
    assert "ID: 2 | Marketing | Users: 3" in result.content
    assert "ID: 7 | R&D | ERP code: CC-4210 | Users: 12 | archived" in result.content


@pytest.mark.asyncio
async def test_a_cost_center_is_read_from_its_cost_centers_path(monkeypatch):
    executor, calls = _perk(monkeypatch, {"id": "7", "name": "R&D", "users": []})

    result = await _call(executor, "read_perk_cost_center", cost_center_id="7")

    assert not result.is_error, result.content
    assert calls[0]["url"] == "https://api.travelperk.com/cost_centers/7"
    assert '"name": "R&D"' in result.content


@pytest.mark.asyncio
async def test_custom_fields_are_read_per_trip(monkeypatch):
    executor, calls = _perk(monkeypatch, {"trip_id": 1234, "custom_fields": []})

    refused = await _call(executor, "list_perk_custom_fields")
    assert "pass trip_id" in refused.content
    assert calls == []

    await _call(executor, "list_perk_custom_fields", trip_id="1234")
    assert calls[0]["url"] == "https://api.travelperk.com/trips/1234/custom-fields"


# ---------------------------------------------------------------------------
# LinkedIn
# ---------------------------------------------------------------------------


def _linkedin(monkeypatch, token: str, answers: dict[str, Any]) -> tuple[CallableToolExecutor, list[str]]:
    calls: list[str] = []

    async def fake_safe_request(_client, _method, url, _provider, _op, **_kwargs):
        calls.append(url)
        for suffix, payload in answers.items():
            if url.endswith(suffix):
                return _Response(payload), None
        return None, f"Error: unexpected call {url}"

    monkeypatch.setattr(linkedin, "safe_request", fake_safe_request)
    executor = CallableToolExecutor()
    linkedin.build_tools(token, executor)
    return executor, calls


@pytest.mark.asyncio
async def test_a_page_token_is_told_to_use_the_organization_tools(monkeypatch):
    executor, calls = _linkedin(monkeypatch, "tok||page||111", {})

    result = await _call(executor, "get_linkedin_profile")

    assert result.content == linkedin.PAGE_ACCOUNT_PROFILE_ERROR
    assert result.content.startswith("Error: organization token")
    assert result.is_error
    assert calls == [], "a Page token must never reach /v2/userinfo"


@pytest.mark.asyncio
async def test_a_personal_token_still_reads_its_profile(monkeypatch):
    executor, calls = _linkedin(
        monkeypatch, "tok||personal||", {"/v2/userinfo": {"name": "Ada", "email": "a@x", "sub": "s"}}
    )

    result = await _call(executor, "get_linkedin_profile")

    assert "**Ada**" in result.content
    assert calls == ["https://api.linkedin.com/v2/userinfo"]


@pytest.mark.asyncio
async def test_an_organization_without_id_lists_the_administered_pages(monkeypatch):
    executor, calls = _linkedin(
        monkeypatch,
        "tok||page||111",
        {
            "/rest/organizationAcls": {
                "elements": [
                    {"organization": "urn:li:organization:111", "role": "ADMINISTRATOR"},
                    {"organization": "urn:li:organization:222", "role": "ADMINISTRATOR"},
                ]
            },
            "/rest/organizations/111": {"id": 111, "localizedName": "Filigran"},
            "/rest/organizations/222": {"id": 222, "localizedName": "Filigran Labs"},
        },
    )

    result = await _call(executor, "get_linkedin_organization")

    assert "LinkedIn Pages this token administers (2)" in result.content
    assert "**Filigran** (the integration's configured Page)" in result.content
    assert "**Filigran Labs**" in result.content
    assert calls[0].endswith("/rest/organizationAcls")


@pytest.mark.asyncio
async def test_an_organization_by_id_is_read_directly(monkeypatch):
    executor, calls = _linkedin(
        monkeypatch, "tok||page||111", {"/rest/organizations/222": {"id": 222, "localizedName": "Filigran Labs"}}
    )

    result = await _call(executor, "get_linkedin_organization", org_id="222")

    assert "**Filigran Labs**" in result.content
    assert calls == ["https://api.linkedin.com/rest/organizations/222"]


class _ConnectivityClient:
    def __init__(self, response: _Response, seen: list[str]):
        self._response = response
        self._seen = seen

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get(self, url, **_kwargs):
        self._seen.append(url)
        return self._response


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("account_type", "payload", "url", "status", "message"),
    [
        (
            "page",
            {"elements": [{"organization": "urn:li:organization:111"}]},
            "https://api.linkedin.com/rest/organizationAcls",
            "ok",
            "organization token administering 1 LinkedIn Page",
        ),
        (
            "page",
            {"elements": []},
            "https://api.linkedin.com/rest/organizationAcls",
            "error",
            "administers no LinkedIn Page",
        ),
        (
            "personal",
            {"sub": "s"},
            "https://api.linkedin.com/v2/userinfo",
            "ok",
            "is connected and responding",
        ),
    ],
    ids=["page", "page-without-pages", "personal"],
)
async def test_the_connection_test_checks_a_page_token_on_its_pages(
    monkeypatch, account_type, payload, url, status, message
):
    from app.api.v1.integration_testing import test_provider_connectivity

    seen: list[str] = []
    monkeypatch.setattr(
        linkedin, "egress_client", lambda **_kw: _ConnectivityClient(_Response(payload), seen)
    )

    result = await test_provider_connectivity(
        "linkedin", "tok", {"account_type": account_type}, "LinkedIn"
    )

    assert seen == [url]
    assert result["status"] == status
    assert message in result["message"]


class _UnreadableResponse(_Response):
    def json(self) -> Any:
        raise ValueError("not JSON")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [_UnreadableResponse(None), _Response(["not", "an", "object"]), _Response({"elements": "x"})],
    ids=["not-json", "a-list", "elements-not-a-list"],
)
async def test_an_unreadable_pages_answer_is_an_error_never_a_raise(monkeypatch, response):
    """``test_provider_connectivity`` never raises: an odd body is a verdict."""
    from app.api.v1.integration_testing import test_provider_connectivity

    monkeypatch.setattr(linkedin, "egress_client", lambda **_kw: _ConnectivityClient(response, []))

    result = await test_provider_connectivity("linkedin", "tok", {"account_type": "page"}, "LinkedIn")

    assert result == {
        "status": "error",
        "message": "LinkedIn answered the Pages it administers with an unreadable body.",
    }
