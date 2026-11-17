"""The Mattermost integration provider.

Covers the catalog entry and registration, the shared instance-URL rule
(``operator_base_url``: http(s), no credentials, query or fragment; required
here), the id pattern guarding every path segment, the read/write split, and
each tool against a mocked API (``httpx.MockTransport``).
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.api.v1.integration_catalog import PROVIDER_CATALOG, build_dummy_token
from app.integrations.http import operator_base_url
from app.integrations.providers import mattermost
from app.integrations.providers.mattermost import build_tools, mattermost_api
from app.services.integration_read_only import integration_tool_access
from app.services.integration_tools import PROVIDER_TOOLS, _SafeIntegrationExecutor

_BASE = "https://chat.example.com"
_API = f"{_BASE}/api/v4"
_TOKEN = f"{_BASE}||mmTok"
_CHANNEL = "c" * 26
_USER = "u" * 26
_POST = "p" * 26
_TEAM = "t" * 26


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

    monkeypatch.setattr(mattermost, "egress_client", _client)
    return seen


def _json(payload, status: int = 200):
    return lambda _request: httpx.Response(status, json=payload)


def test_catalog_entry_and_registration():
    entry = PROVIDER_CATALOG["mattermost"]
    assert entry["auth_type"] == "api_key"
    assert set(entry["extra_fields"]) == {"base_url"}
    assert entry["integration_category"] == "communication"
    assert PROVIDER_TOOLS["mattermost"] is build_tools


def test_dummy_token_builds_every_tool():
    assert len(build_tools(build_dummy_token("mattermost"), _SafeIntegrationExecutor())) == 19


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://chat.example.com/", "https://chat.example.com"),
        ("https://chat.example.com/api/v4", "https://chat.example.com"),
        ("http://10.1.2.3:8065", "http://10.1.2.3:8065"),
        ("https://[::1]:8065/mm", "https://[::1]:8065/mm"),
        ("https://u:p@chat.example.com", None),
        ("https://chat.example.com/?a=1", None),
        ("https://chat.example.com/#x", None),
        ("javascript:alert(1)", None),
        ("chat.example.com", None),
        ("", None),
    ],
)
def test_operator_base_url(raw, expected):
    assert operator_base_url(raw, strip_suffix="/api/v4") == expected


@pytest.mark.parametrize("token", ["", "||mmTok", f"{_BASE}||", "ftp://x||mmTok", "https://a:b@x.com||mmTok"])
def test_unusable_token_builds_no_tools(token):
    handlers, tools = _build(token)
    assert tools == []
    assert handlers == {}


def test_api_root():
    assert mattermost_api(_BASE) == _API
    assert mattermost_api("") is None


def test_reads_and_writes_are_classified():
    _, tools = _build()
    writes = {
        "post_mattermost_message",
        "post_mattermost_ephemeral_message",
        "delete_mattermost_post",
        "add_mattermost_reaction",
        "remove_mattermost_reaction",
        "create_mattermost_channel",
        "archive_mattermost_channel",
        "restore_mattermost_channel",
        "add_mattermost_channel_member",
        "deactivate_mattermost_user",
        "invite_mattermost_users",
    }
    assert {t.name for t in tools if integration_tool_access(t.name) == "write"} == writes


@pytest.mark.asyncio
async def test_post_message_in_a_thread(monkeypatch):
    seen = _mock(monkeypatch, _json({"id": _POST}, status=201))
    handlers, _ = _build()
    out = await handlers["post_mattermost_message"](channel_id=_CHANNEL, message="Critical: RDP exposed", root_id=_POST)
    req = seen[0]
    assert req.method == "POST"
    assert str(req.url) == f"{_API}/posts"
    assert req.headers["Authorization"] == "Bearer mmTok"
    assert json.loads(req.content) == {"channel_id": _CHANNEL, "message": "Critical: RDP exposed", "root_id": _POST}
    assert out == f"Posted message {_POST}."


@pytest.mark.asyncio
async def test_long_message_is_cut_and_said(monkeypatch):
    seen = _mock(monkeypatch, _json({"id": _POST}))
    handlers, _ = _build()
    out = await handlers["post_mattermost_message"](channel_id=_CHANNEL, message="x" * 20_000)
    assert len(json.loads(seen[0].content)["message"]) == 16_383
    assert out.endswith("(cut to 16383 characters).")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"channel_id": "../users/me", "message": "x"},
        {"channel_id": "C" * 26, "message": "x"},
        {"channel_id": _CHANNEL, "message": "   "},
        {"channel_id": _CHANNEL, "message": "x", "root_id": "nope"},
    ],
)
async def test_post_refuses_bad_input(monkeypatch, kwargs):
    seen = _mock(monkeypatch, _json({}))
    handlers, _ = _build()
    out = await handlers["post_mattermost_message"](**kwargs)
    assert out.startswith("Error:")
    assert seen == []


@pytest.mark.asyncio
async def test_channel_posts_follow_the_order(monkeypatch):
    other = "q" * 26
    seen = _mock(
        monkeypatch,
        _json(
            {
                "order": [_POST, other],
                "posts": {
                    _POST: {"create_at": 2, "user_id": _USER, "message": "new\nline"},
                    other: {"create_at": 1, "user_id": _USER, "message": "old", "root_id": _POST},
                },
            }
        ),
    )
    handlers, _ = _build()
    out = await handlers["list_mattermost_channel_posts"](channel_id=_CHANNEL, limit=2)
    assert seen[0].url.params["per_page"] == "2"
    assert out.splitlines() == [
        "Messages, newest first (2; create_at is in ms since epoch):",
        f"- 2 user {_USER} post {_POST}: new line",
        f"- 1 user {_USER} post {other} (reply to {_POST}): old",
        "[Older messages: pass page=1]",
    ]


@pytest.mark.asyncio
async def test_add_reaction_reads_the_token_user_first(monkeypatch):
    def _respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v4/users/me":
            return httpx.Response(200, json={"id": _USER})
        return httpx.Response(201, json={})

    seen = _mock(monkeypatch, _respond)
    handlers, _ = _build()
    out = await handlers["add_mattermost_reaction"](post_id=_POST, emoji_name=":white_check_mark:")
    assert [r.url.path for r in seen] == ["/api/v4/users/me", "/api/v4/reactions"]
    assert json.loads(seen[1].content) == {"user_id": _USER, "post_id": _POST, "emoji_name": "white_check_mark"}
    assert out == f"Reacted :white_check_mark: to post {_POST}."


@pytest.mark.asyncio
async def test_reaction_emoji_cannot_reach_another_path(monkeypatch):
    seen = _mock(monkeypatch, _json({"id": _USER}))
    handlers, _ = _build()
    out = await handlers["remove_mattermost_reaction"](post_id=_POST, emoji_name="../../users")
    assert out.startswith("Error:")
    assert seen == []


@pytest.mark.asyncio
async def test_search_channels_posts_the_term(monkeypatch):
    seen = _mock(monkeypatch, _json([{"id": _CHANNEL, "name": "soc", "display_name": "SOC", "type": "O"}]))
    handlers, _ = _build()
    out = await handlers["search_mattermost_channels"](team_id=_TEAM, term="soc")
    assert seen[0].method == "POST"
    assert seen[0].url.path == f"/api/v4/teams/{_TEAM}/channels/search"
    assert out == f'Channels (1):\n- ~soc "SOC" (id: {_CHANNEL}, public)'


@pytest.mark.asyncio
async def test_create_channel_checks_the_handle(monkeypatch):
    seen = _mock(monkeypatch, _json({}))
    handlers, _ = _build()
    out = await handlers["create_mattermost_channel"](team_id=_TEAM, name="Vuln Alerts", display_name="Vuln alerts")
    assert out.startswith("Error:")
    assert seen == []


@pytest.mark.asyncio
async def test_get_user_by_email(monkeypatch):
    seen = _mock(monkeypatch, _json({"id": _USER, "username": "ada", "email": "ada@example.com"}))
    handlers, _ = _build()
    out = await handlers["get_mattermost_user"](email="ada@example.com")
    assert seen[0].url.path == "/api/v4/users/email/ada@example.com"
    assert out == f"@ada (id: {_USER}), ada@example.com"


@pytest.mark.asyncio
async def test_invite_refuses_a_bad_address(monkeypatch):
    seen = _mock(monkeypatch, _json({}))
    handlers, _ = _build()
    out = await handlers["invite_mattermost_users"](team_id=_TEAM, emails=["ada@example.com", "x/../y"])
    assert out.startswith("Error:")
    assert seen == []


@pytest.mark.asyncio
async def test_a_write_is_not_retried_on_a_gateway_error(monkeypatch):
    seen = _mock(monkeypatch, _json({}, status=502))
    handlers, _ = _build()
    out = await handlers["post_mattermost_message"](channel_id=_CHANNEL, message="x")
    assert out.startswith("Error")
    assert len(seen) == 1
