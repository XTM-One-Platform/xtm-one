"""Tests for the 1Password integration provider.

Covers tool introspection (17 tools, read/write/destructive priorities),
the 6-part compound token parsing (Connect + Events + SCIM surfaces with
the US-cloud Events default), the bearer header + URL normalisation
helpers (the TOTP engine itself is ``app.core.totp``, tested in
test_browser_profile_totp.py), per-subsystem configuration guards, the Connect
item flows (search filters, concealed-field masking, OTP retrieval with
server-code preference and local fallback, item create/update/delete,
file reads, activity log pagination), the Events Reporting cursor
protocol, and the SCIM directory + provisioning tools. Every API call
routes through the shared ``safe_request`` helper, which is monkeypatched
here so no network is touched.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from app.integrations.providers import onepassword as op_mod
from app.integrations.providers.onepassword import (
    EVENTS_DEFAULT_BASE,
    OnePasswordIntegration,
    build_onepassword_headers,
    build_tools,
    onepassword_api_base,
    parse_onepassword_token,
    scim_api_base,
)
from app.tools.base import CallableToolExecutor, ToolCall

_CONNECT_URL = "https://connect.example.com"
_EVENTS_URL = "https://events.example.com"
_SCIM_URL = "https://scim.example.com"

# 26-char lowercase base32 ids, the shape the Connect API uses.
_VID = "abcdefghijklmnopqrstuvwxy2"
_IID = "itemitemitemitemitemitemit"


def _token(
    connect: bool = True, events: bool = False, scim: bool = False
) -> str:
    return "||".join(
        [
            _CONNECT_URL if connect else "",
            "ctok" if connect else "",
            _EVENTS_URL if events else "",
            "etok" if events else "",
            _SCIM_URL if scim else "",
            "stok" if scim else "",
        ]
    )


_FULL_TOKEN = _token(connect=True, events=True, scim=True)


class _StubResponse:
    def __init__(
        self,
        payload: Any = None,
        *,
        headers: dict[str, str] | None = None,
        status_code: int = 200,
        content: bytes = b"",
    ):
        self._payload = payload
        self.headers = headers or {}
        self.status_code = status_code
        self.content = content

    def json(self) -> Any:
        return self._payload


class _NonJsonResponse:
    """A 2xx response whose body is not JSON (login page, proxy error...)."""

    def json(self) -> Any:
        raise ValueError("Expecting value: line 1 column 1 (char 0)")


class _Recorder:
    """Fake ``safe_request`` that routes by (method, url) and records calls."""

    def __init__(self, router):
        self._router = router
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, _client, method, url, _provider, action, **kwargs):
        self.calls.append({"method": method, "url": url, "action": action, **kwargs})
        payload = self._router(method, url)
        if isinstance(payload, _StubResponse):
            return payload, None
        if payload is _NonJsonResponse:
            return _NonJsonResponse(), None
        return _StubResponse(payload), None


def _build(
    monkeypatch, router, token: str = _FULL_TOKEN
) -> tuple[CallableToolExecutor, _Recorder]:
    recorder = _Recorder(router)
    monkeypatch.setattr(op_mod, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=token, executor=executor)
    return executor, recorder


# ---------------------------------------------------------------------------
# Introspection + helpers
# ---------------------------------------------------------------------------

_ALL_TOOLS = {
    "list_onepassword_vaults",
    "search_onepassword_items",
    "get_onepassword_item",
    "get_onepassword_otp",
    "reveal_onepassword_field",
    "list_onepassword_files",
    "read_onepassword_file",
    "download_onepassword_file",
    "create_onepassword_item",
    "update_onepassword_item",
    "delete_onepassword_item",
    "get_onepassword_activity",
    "list_onepassword_signin_attempts",
    "list_onepassword_item_usages",
    "list_onepassword_audit_events",
    "list_onepassword_users",
    "list_onepassword_groups",
    "manage_onepassword_provisioning",
}


def test_build_tools_returns_definitions_with_dummy_token():
    # The catalog introspects with the 2-part URL dummy - all 18 tools must
    # register from it (the missing Events/SCIM surfaces guard at call time).
    executor = CallableToolExecutor()
    tools = build_tools(token="https://dummy.example.com||__dummy__", executor=executor)
    assert {t.name for t in tools} == _ALL_TOOLS
    assert len(tools) == 18


def test_tool_priorities_read_write_destructive():
    executor = CallableToolExecutor()
    tools = build_tools(token=_FULL_TOKEN, executor=executor)
    by_name = {t.name: t for t in tools}
    # reads sort first, writes next, destructive ops last
    assert by_name["list_onepassword_vaults"].priority == 0
    assert by_name["get_onepassword_otp"].priority == 0
    assert by_name["reveal_onepassword_field"].priority == 0
    assert by_name["get_onepassword_activity"].priority == 0
    assert by_name["list_onepassword_signin_attempts"].priority == 0
    assert by_name["list_onepassword_users"].priority == 0
    assert by_name["create_onepassword_item"].priority == 1
    assert by_name["update_onepassword_item"].priority == 1
    assert by_name["delete_onepassword_item"].priority == 2
    assert by_name["manage_onepassword_provisioning"].priority == 2


def test_build_tools_empty_when_nothing_configured():
    executor = CallableToolExecutor()
    assert build_tools(token="||".join([""] * 6), executor=executor) == []
    # A whitespace-only Connect key alone must be treated as absent too.
    assert (
        build_tools(
            token="||".join([_CONNECT_URL, "   ", "", "", "", ""]),
            executor=executor,
        )
        == []
    )


def test_build_tools_registers_everything_with_partial_config():
    # Events-only and SCIM-only configurations still expose the full tool
    # surface - the other subsystems return configuration guidance at call
    # time instead of vanishing from scoping UIs.
    executor = CallableToolExecutor()
    tools = build_tools(
        token=_token(connect=False, events=True), executor=executor
    )
    assert {t.name for t in tools} == _ALL_TOOLS


def test_headers_use_bearer_prefix():
    headers = build_onepassword_headers("abc")
    assert headers["Authorization"] == "Bearer abc"
    assert headers["Accept"] == "application/json"
    assert headers["Content-Type"] == "application/json"
    assert build_onepassword_headers("  abc  ")["Authorization"] == "Bearer abc"


def test_build_headers_omits_authorization_when_blank():
    for blank in ("", "   ", None):
        headers = build_onepassword_headers(blank)
        assert "Authorization" not in headers
        assert headers["Accept"] == "application/json"


def test_api_base_normalisation():
    assert onepassword_api_base("https://connect.example.com/") == _CONNECT_URL
    assert onepassword_api_base("  https://connect.example.com  ") == _CONNECT_URL
    assert onepassword_api_base("") == ""


def test_scim_api_base_normalisation():
    # A bare bridge root gains the SCIM 2.0 path...
    assert scim_api_base("https://scim.example.com") == (
        "https://scim.example.com/scim/v2"
    )
    assert scim_api_base("https://scim.example.com/") == (
        "https://scim.example.com/scim/v2"
    )
    # ...while an explicit prefix is respected as-is.
    assert scim_api_base("https://scim.example.com/scim/v2/") == (
        "https://scim.example.com/scim/v2"
    )
    assert scim_api_base("https://scim.example.com/scim") == (
        "https://scim.example.com/scim"
    )
    assert scim_api_base("") == ""


def test_parse_onepassword_token_full_compound():
    parsed = parse_onepassword_token(_FULL_TOKEN)
    assert parsed == (
        _CONNECT_URL,
        "ctok",
        _EVENTS_URL,
        "etok",
        f"{_SCIM_URL}/scim/v2",
        "stok",
    )


def test_parse_onepassword_token_defaults_events_host():
    # An events token without an explicit URL falls back to the US cloud.
    token = "||".join(["", "", "", "etok", "", ""])
    parsed = parse_onepassword_token(token)
    assert parsed[2] == EVENTS_DEFAULT_BASE
    assert parsed[3] == "etok"
    # ...but no events token means no default host either.
    parsed = parse_onepassword_token("||".join([""] * 6))
    assert parsed[2] == ""


# ---------------------------------------------------------------------------
# Integration class (validate_connection / get_user_info / connectivity)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_connection_requires_json_array(monkeypatch):
    recorder = _Recorder(lambda m, u: [{"id": _VID}])
    monkeypatch.setattr(op_mod, "safe_request", recorder)
    integration = OnePasswordIntegration()
    assert await integration.validate_connection(_FULL_TOKEN) is True
    call = recorder.calls[0]
    assert call["url"] == f"{_CONNECT_URL}/v1/vaults"
    assert call["headers"]["Authorization"] == "Bearer ctok"


@pytest.mark.asyncio
async def test_validate_connection_rejects_login_pages(monkeypatch):
    # A 2xx HTML login/proxy page must not count as connected.
    recorder = _Recorder(lambda m, u: _NonJsonResponse)
    monkeypatch.setattr(op_mod, "safe_request", recorder)
    integration = OnePasswordIntegration()
    assert await integration.validate_connection(_FULL_TOKEN) is False


@pytest.mark.asyncio
async def test_validate_connection_false_when_unconfigured(monkeypatch):
    async def _boom(*_a, **_k):  # pragma: no cover - must never fire
        raise AssertionError("no HTTP call expected")

    monkeypatch.setattr(op_mod, "safe_request", _boom)
    integration = OnePasswordIntegration()
    assert await integration.validate_connection("||".join([""] * 6)) is False
    assert await integration.validate_connection(_token(connect=False)) is False


@pytest.mark.asyncio
async def test_get_user_info_guards_misconfigured_credentials():
    integration = OnePasswordIntegration()
    info = await integration.get_user_info("||".join(["", "ctok", "", "", "", ""]))
    assert "base URL is not configured" in info["error"]
    info = await integration.get_user_info(
        "||".join([_CONNECT_URL, "   ", "", "", "", ""])
    )
    assert "access token is not configured" in info["error"]


@pytest.mark.asyncio
async def test_get_user_info_reports_vaults_and_version(monkeypatch):
    def router(method, url):
        if url.endswith("/v1/vaults"):
            return [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        return {"name": "1Password Connect API", "version": "1.7.3"}

    recorder = _Recorder(router)
    monkeypatch.setattr(op_mod, "safe_request", recorder)
    integration = OnePasswordIntegration()
    info = await integration.get_user_info(_FULL_TOKEN)
    assert info["name"] == "1Password Connect"
    assert info["vaults"] == 3
    assert info["version"] == "1.7.3"


@pytest.mark.asyncio
async def test_describe_connectivity_aggregates_all_surfaces(monkeypatch):
    def router(method, url):
        if url.endswith("/v1/vaults"):
            return [{"id": "a"}, {"id": "b"}]
        if url.endswith("/api/v2/auth/introspect"):
            return {"features": ["signinattempts", "auditevents"]}
        if url.endswith("/Users"):
            return {"totalResults": 5, "Resources": []}
        raise AssertionError(f"unexpected URL {url}")

    recorder = _Recorder(router)
    monkeypatch.setattr(op_mod, "safe_request", recorder)
    integration = OnePasswordIntegration()
    ok, message = await integration.describe_connectivity(_FULL_TOKEN)
    assert ok is True
    assert "Connect server OK (2 vaults in token scope)" in message
    assert "Events API OK (features: signinattempts, auditevents)" in message
    assert "SCIM bridge OK (5 users)" in message
    # The SCIM probe goes to the normalised /scim/v2 base.
    scim_call = next(c for c in recorder.calls if c["url"].endswith("/Users"))
    assert scim_call["url"] == f"{_SCIM_URL}/scim/v2/Users"


@pytest.mark.asyncio
async def test_describe_connectivity_flags_missing_connect(monkeypatch):
    def router(method, url):
        return {"features": []}

    recorder = _Recorder(router)
    monkeypatch.setattr(op_mod, "safe_request", recorder)
    integration = OnePasswordIntegration()
    ok, message = await integration.describe_connectivity(
        _token(connect=False, events=True)
    )
    assert ok is False
    assert "Connect server: not configured" in message
    assert "Events API OK" in message


# ---------------------------------------------------------------------------
# Per-subsystem configuration guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unconfigured_subsystems_guide_instead_of_failing(monkeypatch):
    # Connect-only credentials: Events + SCIM tools return setup guidance.
    executor, _ = _build(monkeypatch, lambda m, u: [], token=_token())
    result = await executor.execute_tool(
        ToolCall(id="g1", name="list_onepassword_signin_attempts", arguments={})
    )
    assert "Events Reporting API is not configured" in result.content
    result = await executor.execute_tool(
        ToolCall(id="g2", name="list_onepassword_users", arguments={})
    )
    assert "SCIM bridge is not configured" in result.content
    # Events-only credentials: Connect tools return setup guidance.
    executor, _ = _build(
        monkeypatch, lambda m, u: [], token=_token(connect=False, events=True)
    )
    result = await executor.execute_tool(
        ToolCall(id="g3", name="list_onepassword_vaults", arguments={})
    )
    assert "Connect server is not configured" in result.content


# ---------------------------------------------------------------------------
# Connect: vaults + items
# ---------------------------------------------------------------------------


def _vault_payload() -> dict:
    return {
        "id": _VID,
        "name": "Engineering",
        "type": "USER_CREATED",
        "items": 12,
    }


@pytest.mark.asyncio
async def test_list_vaults_formats_and_filters(monkeypatch):
    def router(method, url):
        assert url.endswith("/v1/vaults")
        return [
            _vault_payload(),
            {"id": "b" * 26, "name": "Marketing", "type": "USER_CREATED"},
        ]

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="v1", name="list_onepassword_vaults", arguments={"name": "eng"})
    )
    assert not result.is_error
    assert "'Engineering'" in result.content
    assert "Marketing" not in result.content
    assert f"id={_VID}" in result.content
    assert recorder.calls[0]["headers"]["Authorization"] == "Bearer ctok"


@pytest.mark.asyncio
async def test_search_items_filters_client_side(monkeypatch):
    items = [
        {
            "id": _IID,
            "title": "GitHub API token",
            "category": "API_CREDENTIAL",
            "tags": ["ci"],
            "favorite": True,
            "updatedAt": "2026-08-01T10:00:00Z",
        },
        {
            "id": "j" * 26,
            "title": "GitHub login",
            "category": "LOGIN",
            "updatedAt": "2026-07-01T10:00:00Z",
        },
    ]

    def router(method, url):
        if url.endswith("/v1/vaults"):
            return [_vault_payload()]
        if url.endswith(f"/v1/vaults/{_VID}/items"):
            return items
        raise AssertionError(f"unexpected URL {url}")

    executor, _ = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="s1",
            name="search_onepassword_items",
            arguments={"keyword": "github", "category": "API_CREDENTIAL"},
        )
    )
    assert not result.is_error
    assert "GitHub API token" in result.content
    assert "GitHub login" not in result.content
    assert f"id={_IID}" in result.content
    assert "vault='Engineering'" in result.content


@pytest.mark.asyncio
async def test_get_item_always_masks_concealed_fields(monkeypatch):
    full_item = {
        "id": _IID,
        "title": "Prod DB",
        "category": "DATABASE",
        "vault": {"id": _VID},
        "fields": [
            {
                "id": "username",
                "label": "username",
                "type": "STRING",
                "purpose": "USERNAME",
                "value": "dbadmin",
            },
            {
                "id": "password",
                "label": "password",
                "type": "CONCEALED",
                "purpose": "PASSWORD",
                "value": "s3cret-pass",
            },
            {
                "id": "totp1",
                "label": "one-time password",
                "type": "OTP",
                "value": "otpauth://totp/x?secret=JBSWY3DPEHPK3PXP",
            },
        ],
    }

    def router(method, url):
        if url.endswith(f"/v1/vaults/{_VID}"):
            return _vault_payload()
        if url.endswith(f"/v1/vaults/{_VID}/items/{_IID}"):
            return full_item
        raise AssertionError(f"unexpected URL {url}")

    executor, _ = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="i1",
            name="get_onepassword_item",
            arguments={"item": _IID, "vault": _VID},
        )
    )
    assert not result.is_error
    assert "dbadmin" in result.content
    assert "s3cret-pass" not in result.content
    assert "JBSWY3DPEHPK3PXP" not in result.content
    assert "[concealed - use reveal_onepassword_field]" in result.content
    assert "[one-time password - use get_onepassword_otp]" in result.content
    # The read-classified detail tool exposes NO reveal flag - the
    # write-classified reveal_onepassword_field is the only reveal path.
    tools = build_tools(token=_FULL_TOKEN, executor=CallableToolExecutor())
    schema = next(
        t for t in tools if t.name == "get_onepassword_item"
    ).input_schema
    assert "reveal_secrets" not in schema["properties"]


def _otp_router(field: dict):
    def router(method, url):
        if url.endswith(f"/v1/vaults/{_VID}"):
            return _vault_payload()
        if url.endswith(f"/v1/vaults/{_VID}/items/{_IID}"):
            return {
                "id": _IID,
                "title": "GitHub",
                "category": "LOGIN",
                "fields": [field],
            }
        raise AssertionError(f"unexpected URL {url}")

    return router


@pytest.mark.asyncio
async def test_get_otp_prefers_server_computed_code(monkeypatch):
    executor, _ = _build(
        monkeypatch,
        _otp_router(
            {
                "id": "totp1",
                "label": "one-time password",
                "type": "OTP",
                "value": "otpauth://totp/x?secret=JBSWY3DPEHPK3PXP",
                "totp": "123456",
            }
        ),
    )
    result = await executor.execute_tool(
        ToolCall(
            id="o1",
            name="get_onepassword_otp",
            arguments={"item": _IID, "vault": _VID},
        )
    )
    assert not result.is_error
    assert "One-time password for 'GitHub'" in result.content
    assert "123456" in result.content
    assert "30s period, 6 digits, SHA1" in result.content


@pytest.mark.asyncio
async def test_get_otp_computes_locally_from_otpauth_uri(monkeypatch):
    # No server-computed code -> local RFC 6238 fallback from the stored
    # secret, honouring the URI's period/digits/algorithm parameters.
    executor, _ = _build(
        monkeypatch,
        _otp_router(
            {
                "id": "totp1",
                "label": "one-time password",
                "type": "OTP",
                "value": (
                    "otpauth://totp/GitHub:alice?secret=JBSWY3DPEHPK3PXP"
                    "&digits=8&period=60"
                ),
            }
        ),
    )
    result = await executor.execute_tool(
        ToolCall(
            id="o2",
            name="get_onepassword_otp",
            arguments={"item": _IID, "vault": _VID},
        )
    )
    assert not result.is_error
    code = re.search(r"\[field: one-time password\]: (\d+)", result.content)
    assert code is not None
    assert len(code.group(1)) == 8
    assert "60s period, 8 digits, SHA1" in result.content


@pytest.mark.asyncio
async def test_get_otp_errors_without_otp_field(monkeypatch):
    executor, _ = _build(
        monkeypatch,
        _otp_router(
            {
                "id": "password",
                "label": "password",
                "type": "CONCEALED",
                "value": "x",
            }
        ),
    )
    result = await executor.execute_tool(
        ToolCall(
            id="o3",
            name="get_onepassword_otp",
            arguments={"item": _IID, "vault": _VID},
        )
    )
    assert "has no one-time password (OTP) field" in result.content
    assert "'password'" in result.content  # lists the fields that DO exist


@pytest.mark.asyncio
async def test_get_otp_errors_when_value_hidden(monkeypatch):
    # An OTP field with neither a computed code nor a readable secret means
    # the token cannot read the value - explain instead of crashing.
    executor, _ = _build(
        monkeypatch,
        _otp_router({"id": "totp1", "label": "otp", "type": "OTP", "value": ""}),
    )
    result = await executor.execute_tool(
        ToolCall(
            id="o4",
            name="get_onepassword_otp",
            arguments={"item": _IID, "vault": _VID},
        )
    )
    assert "exposes neither a computed code nor its secret" in result.content


@pytest.mark.asyncio
async def test_reveal_field_returns_value_with_care_notice(monkeypatch):
    def router(method, url):
        if url.endswith(f"/v1/vaults/{_VID}"):
            return _vault_payload()
        if url.endswith(f"/v1/vaults/{_VID}/items/{_IID}"):
            return {
                "id": _IID,
                "title": "Prod DB",
                "fields": [
                    {
                        "id": "password",
                        "label": "password",
                        "type": "CONCEALED",
                        "value": "s3cret-pass",
                    }
                ],
            }
        raise AssertionError(f"unexpected URL {url}")

    executor, _ = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="r1",
            name="reveal_onepassword_field",
            arguments={"item": _IID, "vault": _VID, "field": "password"},
        )
    )
    assert not result.is_error
    assert "s3cret-pass" in result.content
    assert "Handle with care" in result.content


@pytest.mark.asyncio
async def test_reveal_field_refuses_otp_seed(monkeypatch):
    # The permanent TOTP seed is 2FA-bypass material - the reveal tool
    # refuses OTP fields; only get_onepassword_otp surfaces the (ephemeral)
    # current code.
    def router(method, url):
        if url.endswith(f"/v1/vaults/{_VID}"):
            return _vault_payload()
        if url.endswith(f"/v1/vaults/{_VID}/items/{_IID}"):
            return {
                "id": _IID,
                "title": "GitHub",
                "fields": [
                    {
                        "id": "totp1",
                        "label": "one-time password",
                        "type": "OTP",
                        "value": "otpauth://totp/x?secret=JBSWY3DPEHPK3PXP",
                    }
                ],
            }
        raise AssertionError(f"unexpected URL {url}")

    executor, _ = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="r3",
            name="reveal_onepassword_field",
            arguments={"item": _IID, "vault": _VID, "field": "one-time password"},
        )
    )
    assert "JBSWY3DPEHPK3PXP" not in result.content
    assert "never revealed" in result.content
    assert "get_onepassword_otp" in result.content


@pytest.mark.asyncio
async def test_reveal_field_unknown_field_lists_available(monkeypatch):
    def router(method, url):
        if url.endswith(f"/v1/vaults/{_VID}"):
            return _vault_payload()
        return {
            "id": _IID,
            "title": "Prod DB",
            "fields": [{"id": "username", "label": "username", "type": "STRING"}],
        }

    executor, _ = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="r2",
            name="reveal_onepassword_field",
            arguments={"item": _IID, "vault": _VID, "field": "nope"},
        )
    )
    assert "no field matching 'nope'" in result.content
    assert "'username'" in result.content


# ---------------------------------------------------------------------------
# Connect: files
# ---------------------------------------------------------------------------


def _file_router(content: bytes):
    def router(method, url):
        if url.endswith(f"/v1/vaults/{_VID}"):
            return _vault_payload()
        if url.endswith(f"/v1/vaults/{_VID}/items/{_IID}"):
            return {"id": _IID, "title": "Server"}
        if url.endswith("/files"):
            return [{"id": "f1", "name": "recovery-codes.txt", "size": len(content)}]
        if url.endswith("/files/f1/content"):
            return _StubResponse(content=content)
        raise AssertionError(f"unexpected URL {url}")

    return router


@pytest.mark.asyncio
async def test_read_file_returns_text_content(monkeypatch):
    executor, recorder = _build(monkeypatch, _file_router(b"code-1\ncode-2\n"))
    result = await executor.execute_tool(
        ToolCall(
            id="f1",
            name="read_onepassword_file",
            arguments={"item": _IID, "vault": _VID, "file": "recovery-codes.txt"},
        )
    )
    assert not result.is_error
    assert "File 'recovery-codes.txt'" in result.content
    assert "code-1" in result.content
    # The content endpoint is called with the bearer header only.
    content_call = recorder.calls[-1]
    assert content_call["url"].endswith("/files/f1/content")
    assert content_call["headers"]["Authorization"] == "Bearer ctok"


@pytest.mark.asyncio
async def test_read_file_refuses_binary_content(monkeypatch):
    executor, _ = _build(monkeypatch, _file_router(b"\x00\x01\x02binary"))
    result = await executor.execute_tool(
        ToolCall(
            id="f2",
            name="read_onepassword_file",
            arguments={"item": _IID, "vault": _VID, "file": "recovery-codes.txt"},
        )
    )
    assert "looks binary" in result.content


# ---------------------------------------------------------------------------
# Connect: item lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_item_builds_full_body(monkeypatch):
    def router(method, url):
        if url.endswith(f"/v1/vaults/{_VID}"):
            return _vault_payload()
        if url.endswith(f"/v1/vaults/{_VID}/items"):
            return {
                "id": _IID,
                "title": "CI deploy key",
                "category": "API_CREDENTIAL",
            }
        raise AssertionError(f"unexpected URL {url}")

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="create_onepassword_item",
            arguments={
                "vault": _VID,
                "title": "CI deploy key",
                "category": "API_CREDENTIAL",
                "username": "ci-bot",
                "generate_password": True,
                "password_length": 40,
                "url": "https://ci.example.com",
                "notes": "Rotates quarterly",
                "otp_secret": "otpauth://totp/CI?secret=JBSWY3DPEHPK3PXP",
                "tags": "ci, automation",
            },
        )
    )
    assert not result.is_error
    assert "Created item 'CI deploy key'" in result.content
    assert "get_onepassword_otp returns the current code" in result.content
    call = recorder.calls[-1]
    assert call["method"] == "post"
    body = call["json"]
    assert body["vault"] == {"id": _VID}
    assert body["title"] == "CI deploy key"
    assert body["category"] == "API_CREDENTIAL"
    assert body["tags"] == ["ci", "automation"]
    assert body["urls"] == [{"href": "https://ci.example.com", "primary": True}]
    by_purpose = {f.get("purpose"): f for f in body["fields"] if f.get("purpose")}
    assert by_purpose["USERNAME"]["value"] == "ci-bot"
    assert by_purpose["PASSWORD"]["generate"] is True
    assert by_purpose["PASSWORD"]["recipe"]["length"] == 40
    assert by_purpose["NOTES"]["value"] == "Rotates quarterly"
    otp_fields = [f for f in body["fields"] if f.get("type") == "OTP"]
    assert len(otp_fields) == 1
    assert otp_fields[0]["value"].startswith("otpauth://totp/CI")


@pytest.mark.asyncio
async def test_create_item_input_validation(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="c2",
            name="create_onepassword_item",
            arguments={
                "vault": _VID,
                "title": "X",
                "password": "abc",
                "generate_password": True,
            },
        )
    )
    assert "either password or generate_password" in result.content
    result = await executor.execute_tool(
        ToolCall(
            id="c3",
            name="create_onepassword_item",
            arguments={"vault": _VID, "title": "X", "category": "NOT_A_THING"},
        )
    )
    assert "unknown category 'NOT_A_THING'" in result.content
    result = await executor.execute_tool(
        ToolCall(
            id="c4",
            name="create_onepassword_item",
            arguments={
                "vault": _VID,
                "title": "X",
                "otp_secret": "otpauth://hotp/x?secret=JBSWY3DPEHPK3PXP",
            },
        )
    )
    assert "invalid otp_secret" in result.content
    # Validation fires before any HTTP call.
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_update_item_composes_json_patch_ops(monkeypatch):
    full_item = {
        "id": _IID,
        "title": "Old name",
        "fields": [
            {"id": "password", "label": "password", "type": "CONCEALED"},
            {"id": "fld1", "label": "api key", "type": "STRING"},
        ],
    }

    def router(method, url):
        if url.endswith(f"/v1/vaults/{_VID}"):
            return _vault_payload()
        if url.endswith(f"/v1/vaults/{_VID}/items/{_IID}"):
            if method == "patch":
                return {"id": _IID, "title": "New name", "version": 7}
            return full_item
        raise AssertionError(f"unexpected URL {url}")

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="u1",
            name="update_onepassword_item",
            arguments={
                "item": _IID,
                "vault": _VID,
                "new_title": "New name",
                "set_fields": [{"field": "password", "value": "n3w"}],
                "add_fields": [{"label": "env", "value": "prod"}],
                "remove_field": "api key",
            },
        )
    )
    assert not result.is_error
    assert "Updated item 'New name'" in result.content
    assert "version 7" in result.content
    patch_call = next(c for c in recorder.calls if c["method"] == "patch")
    ops = patch_call["json"]
    assert {"op": "replace", "path": "/title", "value": "New name"} in ops
    assert {"op": "replace", "path": "/fields/password/value", "value": "n3w"} in ops
    assert {
        "op": "add",
        "path": "/fields",
        "value": {"label": "env", "type": "STRING", "value": "prod"},
    } in ops
    assert {"op": "remove", "path": "/fields/fld1"} in ops


@pytest.mark.asyncio
async def test_update_item_requires_a_change(monkeypatch):
    def router(method, url):
        if url.endswith(f"/v1/vaults/{_VID}"):
            return _vault_payload()
        return {"id": _IID, "title": "X", "fields": []}

    executor, _ = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="u2",
            name="update_onepassword_item",
            arguments={"item": _IID, "vault": _VID},
        )
    )
    assert "nothing to update" in result.content


@pytest.mark.asyncio
async def test_update_item_validates_totp_replacement(monkeypatch):
    def router(method, url):
        if url.endswith(f"/v1/vaults/{_VID}"):
            return _vault_payload()
        return {
            "id": _IID,
            "title": "X",
            "fields": [{"id": "totp1", "label": "otp", "type": "OTP"}],
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="u3",
            name="update_onepassword_item",
            arguments={
                "item": _IID,
                "vault": _VID,
                "set_fields": [{"field": "otp", "value": "!!!not-base32!!!"}],
            },
        )
    )
    assert "invalid TOTP secret" in result.content
    assert not any(c["method"] == "patch" for c in recorder.calls)


@pytest.mark.asyncio
async def test_delete_item_is_labelled_permanent(monkeypatch):
    def router(method, url):
        if url.endswith(f"/v1/vaults/{_VID}"):
            return _vault_payload()
        if method == "delete":
            return _StubResponse(status_code=204)
        return {"id": _IID, "title": "Stale cred"}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="d1",
            name="delete_onepassword_item",
            arguments={"item": _IID, "vault": _VID},
        )
    )
    assert not result.is_error
    assert "Deleted item 'Stale cred'" in result.content
    assert "permanent" in result.content
    delete_call = next(c for c in recorder.calls if c["method"] == "delete")
    assert delete_call["url"].endswith(f"/v1/vaults/{_VID}/items/{_IID}")


@pytest.mark.asyncio
async def test_activity_reads_total_from_content_range(monkeypatch):
    entries = [
        {
            "timestamp": "2026-08-13T09:00:00Z",
            "action": "READ",
            "result": "SUCCESS",
            "resource": {"type": "ITEM", "item": {"id": _IID}},
            "actor": {"account": "team.1password.com", "requestIp": "10.0.0.9"},
        },
        {
            "timestamp": "2026-08-13T08:00:00Z",
            "action": "CREATE",
            "result": "SUCCESS",
            "resource": {"type": "ITEM", "item": {"id": "j" * 26}},
            "actor": {},
        },
    ]

    def router(method, url):
        assert url.endswith("/v1/activity")
        return _StubResponse(entries, headers={"Content-Range": "0-2/57"})

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="a1", name="get_onepassword_activity", arguments={"limit": 2})
    )
    assert not result.is_error
    assert "API request" in result.content
    assert "READ" in result.content
    assert recorder.calls[0]["params"] == {"limit": 2, "offset": 0}
    # 2 of 57 shown -> pagination hint with the next offset
    assert "offset" in result.content


# ---------------------------------------------------------------------------
# Events Reporting API
# ---------------------------------------------------------------------------


def _events_executor(monkeypatch, payload):
    return _build(
        monkeypatch,
        lambda m, u: payload,
        token=_token(connect=False, events=True),
    )


@pytest.mark.asyncio
async def test_signin_attempts_builds_reset_cursor_body(monkeypatch):
    payload = {
        "items": [
            {
                "timestamp": "2026-08-13T07:12:00Z",
                "category": "credentials_failed",
                "type": "password_secret_bad",
                "target_user": {"email": "alice@example.com", "name": "Alice"},
                "client": {"app_name": "1Password Browser", "ip_address": "1.2.3.4"},
                "location": {"city": "Paris", "country": "FR"},
            }
        ],
        "cursor": "cur-abc",
        "has_more": True,
    }
    executor, recorder = _events_executor(monkeypatch, payload)
    result = await executor.execute_tool(
        ToolCall(
            id="e1",
            name="list_onepassword_signin_attempts",
            arguments={"since_hours": 4, "limit": 100},
        )
    )
    assert not result.is_error
    assert "credentials_failed/password_secret_bad" in result.content
    assert "Alice <alice@example.com>" in result.content
    assert "cur-abc" in result.content  # has_more -> cursor pagination hint
    call = recorder.calls[0]
    assert call["url"] == f"{_EVENTS_URL}/api/v2/signinattempts"
    assert call["method"] == "post"
    assert call["json"]["limit"] == 100
    assert "start_time" in call["json"]
    assert call["headers"]["Authorization"] == "Bearer etok"


@pytest.mark.asyncio
async def test_signin_attempts_cursor_resumes_stream(monkeypatch):
    payload = {"items": [], "cursor": "cur-next", "has_more": False}
    executor, recorder = _events_executor(monkeypatch, payload)
    result = await executor.execute_tool(
        ToolCall(
            id="e2",
            name="list_onepassword_signin_attempts",
            arguments={"cursor": "cur-abc"},
        )
    )
    assert not result.is_error
    # A cursor request must carry ONLY the cursor (Events API contract).
    assert recorder.calls[0]["json"] == {"cursor": "cur-abc"}
    assert "No sign-in attempts in the window." in result.content


@pytest.mark.asyncio
async def test_signin_attempts_rejects_unknown_category(monkeypatch):
    executor, recorder = _events_executor(monkeypatch, {"items": []})
    result = await executor.execute_tool(
        ToolCall(
            id="e3",
            name="list_onepassword_signin_attempts",
            arguments={"category": "bogus"},
        )
    )
    assert "unknown category 'bogus'" in result.content
    assert "mfa_failed" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_signin_attempts_category_filter_is_client_side(monkeypatch):
    payload = {
        "items": [
            {"timestamp": "2026-08-13T07:00:00Z", "category": "success", "type": "mfa_ok"},
            {
                "timestamp": "2026-08-13T07:01:00Z",
                "category": "mfa_failed",
                "type": "totp_bad",
                "target_user": {"email": "bob@example.com"},
            },
        ],
        "cursor": "c",
        "has_more": False,
    }
    executor, _ = _events_executor(monkeypatch, payload)
    result = await executor.execute_tool(
        ToolCall(
            id="e4",
            name="list_onepassword_signin_attempts",
            arguments={"category": "mfa_failed"},
        )
    )
    assert "mfa_failed/totp_bad" in result.content
    assert "success/mfa_ok" not in result.content
    assert "applied client-side" in result.content


@pytest.mark.asyncio
async def test_item_usages_lists_events(monkeypatch):
    payload = {
        "items": [
            {
                "timestamp": "2026-08-13T06:00:00Z",
                "action": "secure-copy",
                "user": {"email": "alice@example.com"},
                "item_uuid": _IID,
                "vault_uuid": _VID,
            }
        ],
        "cursor": "c1",
        "has_more": False,
    }
    executor, recorder = _events_executor(monkeypatch, payload)
    result = await executor.execute_tool(
        ToolCall(id="e5", name="list_onepassword_item_usages", arguments={})
    )
    assert not result.is_error
    assert "item usage event" in result.content
    assert recorder.calls[0]["url"] == f"{_EVENTS_URL}/api/v2/itemusages"


@pytest.mark.asyncio
async def test_audit_events_lists_events(monkeypatch):
    payload = {
        "items": [
            {
                "timestamp": "2026-08-13T05:00:00Z",
                "action": "grant",
                "object_type": "uva",
                "actor_details": {"email": "admin@example.com"},
            }
        ],
        "cursor": "c2",
        "has_more": False,
    }
    executor, recorder = _events_executor(monkeypatch, payload)
    result = await executor.execute_tool(
        ToolCall(id="e6", name="list_onepassword_audit_events", arguments={})
    )
    assert not result.is_error
    assert "audit event" in result.content
    assert recorder.calls[0]["url"] == f"{_EVENTS_URL}/api/v2/auditevents"


# ---------------------------------------------------------------------------
# SCIM bridge (directory + provisioning)
# ---------------------------------------------------------------------------

_SCIM_USERS = {
    "totalResults": 2,
    "Resources": [
        {
            "id": "u1",
            "userName": "alice@example.com",
            "name": {"givenName": "Alice", "familyName": "Doe"},
            "active": True,
        },
        {
            "id": "u2",
            "userName": "bob@example.com",
            "name": {"formatted": "Bob Ray"},
            "active": False,
        },
    ],
}

_SCIM_GROUPS = {
    "totalResults": 1,
    "Resources": [
        {
            "id": "g1",
            "displayName": "Engineering",
            "members": [{"value": "u1", "display": "Alice Doe"}],
        }
    ],
}


def _scim_executor(monkeypatch, extra_router=None):
    def router(method, url):
        if extra_router is not None:
            handled = extra_router(method, url)
            if handled is not None:
                return handled
        if url.endswith("/Users") and method == "get":
            return _SCIM_USERS
        if url.endswith("/Groups") and method == "get":
            return _SCIM_GROUPS
        raise AssertionError(f"unexpected URL {method} {url}")

    return _build(monkeypatch, router, token=_token(connect=False, scim=True))


@pytest.mark.asyncio
async def test_list_users_pages_and_marks_suspended(monkeypatch):
    executor, recorder = _scim_executor(monkeypatch)
    result = await executor.execute_tool(
        ToolCall(id="s1", name="list_onepassword_users", arguments={})
    )
    assert not result.is_error
    assert "Alice Doe <alice@example.com> id=u1 active" in result.content
    assert "Bob Ray <bob@example.com> id=u2 SUSPENDED" in result.content
    call = recorder.calls[0]
    assert call["url"] == f"{_SCIM_URL}/scim/v2/Users"
    assert call["params"] == {"startIndex": 1, "count": 100}
    assert call["headers"]["Authorization"] == "Bearer stok"


@pytest.mark.asyncio
async def test_list_users_filters(monkeypatch):
    executor, _ = _scim_executor(monkeypatch)
    result = await executor.execute_tool(
        ToolCall(
            id="s2",
            name="list_onepassword_users",
            arguments={"query": "alice", "active_only": True},
        )
    )
    assert "alice@example.com" in result.content
    assert "bob@example.com" not in result.content


@pytest.mark.asyncio
async def test_list_groups_shows_member_counts(monkeypatch):
    executor, _ = _scim_executor(monkeypatch)
    result = await executor.execute_tool(
        ToolCall(id="s3", name="list_onepassword_groups", arguments={})
    )
    assert "'Engineering' id=g1 members=1" in result.content


@pytest.mark.asyncio
async def test_provisioning_rejects_unknown_action(monkeypatch):
    executor, recorder = _scim_executor(monkeypatch)
    result = await executor.execute_tool(
        ToolCall(
            id="p0",
            name="manage_onepassword_provisioning",
            arguments={"action": "obliterate_user", "user": "u1"},
        )
    )
    assert "unknown action 'obliterate_user'" in result.content
    assert "deactivate_user" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_provisioning_create_user_builds_scim_body(monkeypatch):
    def extra(method, url):
        if method == "post" and url.endswith("/Users"):
            return {"id": "u9", "userName": "carol@example.com"}
        return None

    executor, recorder = _scim_executor(monkeypatch, extra)
    result = await executor.execute_tool(
        ToolCall(
            id="p1",
            name="manage_onepassword_provisioning",
            arguments={
                "action": "create_user",
                "email": "carol@example.com",
                "given_name": "Carol",
                "family_name": "Ng",
            },
        )
    )
    assert not result.is_error
    assert "Invited/provisioned user carol@example.com (id u9)" in result.content
    body = recorder.calls[-1]["json"]
    assert body["userName"] == "carol@example.com"
    assert body["emails"] == [{"value": "carol@example.com", "primary": True}]
    assert body["active"] is True
    assert body["name"] == {"givenName": "Carol", "familyName": "Ng"}
    assert body["schemas"] == ["urn:ietf:params:scim:schemas:core:2.0:User"]


@pytest.mark.asyncio
async def test_provisioning_create_user_requires_email(monkeypatch):
    executor, recorder = _scim_executor(monkeypatch)
    result = await executor.execute_tool(
        ToolCall(
            id="p2",
            name="manage_onepassword_provisioning",
            arguments={"action": "create_user", "email": "not-an-email"},
        )
    )
    assert "requires an email" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_provisioning_deactivate_resolves_email_and_patches(monkeypatch):
    def extra(method, url):
        if method == "patch" and url.endswith("/Users/u1"):
            return {"id": "u1", "active": False}
        return None

    executor, recorder = _scim_executor(monkeypatch, extra)
    result = await executor.execute_tool(
        ToolCall(
            id="p3",
            name="manage_onepassword_provisioning",
            arguments={"action": "deactivate_user", "user": "alice@example.com"},
        )
    )
    assert not result.is_error
    assert "Suspended user alice@example.com" in result.content
    assert "suspends rather than deletes" in result.content
    patch_call = next(c for c in recorder.calls if c["method"] == "patch")
    assert patch_call["json"]["Operations"] == [
        {"op": "replace", "value": {"active": False}}
    ]
    assert patch_call["json"]["schemas"] == [
        "urn:ietf:params:scim:api:messages:2.0:PatchOp"
    ]


@pytest.mark.asyncio
async def test_provisioning_group_membership_ops(monkeypatch):
    def extra(method, url):
        if method == "patch" and url.endswith("/Groups/g1"):
            return _StubResponse(status_code=204)
        return None

    executor, recorder = _scim_executor(monkeypatch, extra)
    result = await executor.execute_tool(
        ToolCall(
            id="p4",
            name="manage_onepassword_provisioning",
            arguments={
                "action": "add_group_member",
                "group": "Engineering",
                "user": "alice@example.com",
            },
        )
    )
    assert not result.is_error
    assert "Added alice@example.com to group 'Engineering'." in result.content
    patch_call = next(c for c in recorder.calls if c["method"] == "patch")
    assert patch_call["json"]["Operations"] == [
        {"op": "add", "path": "members", "value": [{"value": "u1"}]}
    ]
    result = await executor.execute_tool(
        ToolCall(
            id="p5",
            name="manage_onepassword_provisioning",
            arguments={
                "action": "remove_group_member",
                "group": "Engineering",
                "user": "alice@example.com",
            },
        )
    )
    assert "Removed alice@example.com from group 'Engineering'." in result.content
    remove_call = [c for c in recorder.calls if c["method"] == "patch"][-1]
    assert remove_call["json"]["Operations"] == [
        {"op": "remove", "path": 'members[value eq "u1"]'}
    ]


@pytest.mark.asyncio
async def test_provisioning_delete_group_is_permanent(monkeypatch):
    def extra(method, url):
        if method == "delete" and url.endswith("/Groups/g1"):
            return _StubResponse(status_code=204)
        return None

    executor, recorder = _scim_executor(monkeypatch, extra)
    result = await executor.execute_tool(
        ToolCall(
            id="p6",
            name="manage_onepassword_provisioning",
            arguments={"action": "delete_group", "group": "Engineering"},
        )
    )
    assert not result.is_error
    assert "Deleted group 'Engineering'" in result.content
    assert "permanent" in result.content
    delete_call = next(c for c in recorder.calls if c["method"] == "delete")
    assert delete_call["url"] == f"{_SCIM_URL}/scim/v2/Groups/g1"


@pytest.mark.asyncio
async def test_provisioning_ambiguous_user_asks_for_id(monkeypatch):
    dupes = {
        "totalResults": 2,
        "Resources": [
            {"id": "u1", "userName": "dup@example.com", "active": True},
            {"id": "u3", "userName": "DUP@example.com", "active": True},
        ],
    }

    def router(method, url):
        if url.endswith("/Users"):
            return dupes
        raise AssertionError(f"unexpected URL {url}")

    executor, _ = _build(monkeypatch, router, token=_token(connect=False, scim=True))
    result = await executor.execute_tool(
        ToolCall(
            id="p7",
            name="manage_onepassword_provisioning",
            arguments={"action": "deactivate_user", "user": "dup@example.com"},
        )
    )
    assert "matches several users" in result.content
    assert "use the id" in result.content
