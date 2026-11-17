"""The Letsignit integration: the signature reader, the tools, the Test button.

Letsignit's public API answers ``GET /signature/{email}`` with the person's
signature (``content``) and its reply variant (``reply``); a 404 means it holds
none for the address, a 401 / 403 that the key is refused or lacks the
``api`` scope. Nothing here reaches the network: ``safe_request`` is replaced.
"""

from __future__ import annotations

import pytest

from app.integrations.connectivity import ProbeContext
from app.integrations.providers import letsignit
from app.integrations.providers.letsignit import (
    LETSIGNIT_API,
    LetsignitIntegration,
    build_letsignit_headers,
    build_tools,
    check_connectivity,
    read_letsignit_signature,
)
from app.services.email_signatures import EmailSignature, SignatureUnavailableError, signature_providers
from app.services.integration_tools import _SafeIntegrationExecutor

SIGNATURE = {
    "name": "Corporate 2026",
    "content": '<table><tr><td>Sam Example<br>CEO</td><td><img src="https://cdn/banner.png"></td></tr></table>',
    "reply": "<p>Sam Example - CEO</p>",
}


class _Resp:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _answer(monkeypatch, status: int = 200, payload=None) -> list[dict]:
    """Every request goes to *status* / *payload*; the calls are recorded."""
    calls: list[dict] = []

    async def _fake(client, method, url, provider, action, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        resp = _Resp(status, payload)
        if 200 <= status < 300:
            return resp, None
        return resp, f"Error: Letsignit answered {status} for {action}."

    monkeypatch.setattr(letsignit, "safe_request", _fake)
    return calls


def _tools():
    executor = _SafeIntegrationExecutor()
    definitions = build_tools("lsk_test", executor)
    return executor._handlers, {d.name: d for d in definitions}


def test_letsignit_is_a_signature_source_with_bearer_auth():
    assert "letsignit" in signature_providers()
    headers = build_letsignit_headers("  lsk  ")
    assert headers == {"Authorization": "Bearer lsk", "Accept": "application/json"}
    integ = LetsignitIntegration()
    assert integ.provider_name == "letsignit" and integ.auth_types == ["api_key"]


async def test_the_reader_returns_the_signature_and_its_reply_variant(monkeypatch):
    calls = _answer(monkeypatch, payload=SIGNATURE)
    signature = await read_letsignit_signature("lsk", "sam@example.com")
    assert signature == EmailSignature(html=SIGNATURE["content"], reply_html=SIGNATURE["reply"], name="Corporate 2026")
    assert calls[0]["url"] == f"{LETSIGNIT_API}/signature/sam@example.com"
    assert calls[0]["headers"]["Authorization"] == "Bearer lsk"


async def test_a_reply_variant_is_optional(monkeypatch):
    _answer(monkeypatch, payload={"name": "Plain", "content": "<p>Sam</p>", "reply": None})
    signature = await read_letsignit_signature("lsk", "sam@example.com")
    assert signature.for_reply(True) == "<p>Sam</p>" and signature.reply_html == ""


async def test_no_signature_for_the_address_is_none_not_a_failure(monkeypatch):
    _answer(monkeypatch, status=404)
    assert await read_letsignit_signature("lsk", "nobody@example.com") is None
    _answer(monkeypatch, payload={"name": None, "content": None, "reply": None})
    assert await read_letsignit_signature("lsk", "nobody@example.com") is None


@pytest.mark.parametrize(
    ("status", "words"),
    [(401, "refused the API key"), (403, "Public API ('api') scope"), (500, "HTTP 500")],
)
async def test_a_refusal_says_what_to_fix(monkeypatch, status, words):
    _answer(monkeypatch, status=status)
    with pytest.raises(SignatureUnavailableError, match=words.replace("(", r"\(").replace(")", r"\)")):
        await read_letsignit_signature("lsk", "sam@example.com")


async def test_the_reader_never_puts_something_else_than_an_address_in_the_path(monkeypatch):
    calls = _answer(monkeypatch, payload=SIGNATURE)
    for bad in ("../users", "sam@example.com/../x", "not an email", ""):
        with pytest.raises(SignatureUnavailableError):
            await read_letsignit_signature("lsk", bad)
    assert calls == []


async def test_the_signature_tool_reads_text_by_default_and_html_on_request(monkeypatch):
    _answer(monkeypatch, payload=SIGNATURE)
    handlers, _ = _tools()
    text = await handlers["get_letsignit_signature"](email="sam@example.com")
    assert "Signature 'Corporate 2026' of sam@example.com" in text
    assert "New emails (1 image(s)):" in text and "Sam Example\nCEO" in text
    assert "Replies (0 image(s)):" in text and "Sam Example - CEO" in text
    html = await handlers["get_letsignit_signature"](email="sam@example.com", format="html")
    assert SIGNATURE["content"] in html and SIGNATURE["reply"] in html


async def test_the_signature_tool_says_when_there_is_none(monkeypatch):
    _answer(monkeypatch, status=404)
    handlers, _ = _tools()
    assert await handlers["get_letsignit_signature"](email="nobody@example.com") == (
        "Letsignit holds no signature for nobody@example.com."
    )


async def test_the_user_tools_read_create_update_and_delete(monkeypatch):
    calls = _answer(
        monkeypatch,
        payload={
            "id": "u-1", "email": "sam@example.com", "type": "user", "picture_url": "", "roles": ["admin"],
            "variables": {"full_name": "Sam Example", "job_title": "CEO", "custom": {"pronouns": "he/him"}},
        },
    )
    handlers, _ = _tools()
    shown = await handlers["get_letsignit_user"](email="sam@example.com")
    assert "Letsignit user sam@example.com (id u-1)" in shown and "job_title: CEO" in shown
    assert "pronouns: he/him" in shown and "Roles: admin" in shown

    updated = await handlers["update_letsignit_user_variables"](
        email="sam@example.com", variables={"job_title": "Founder", "custom": {"pronouns": "they"}}
    )
    assert updated == "Updated custom, job_title of the Letsignit user sam@example.com."
    assert calls[-1]["method"] == "put" and calls[-1]["url"].endswith("/users/sam@example.com/variables")
    assert calls[-1]["json"] == {"variables": {"job_title": "Founder", "custom": {"pronouns": "they"}}}

    created = await handlers["create_letsignit_user"](email="new@example.com", variables={"full_name": "New"})
    assert created == "Created the Letsignit user new@example.com (id u-1)."
    assert calls[-1]["json"] == {"email": "new@example.com", "variables": {"full_name": "New"}}

    assert await handlers["delete_letsignit_user"](email="sam@example.com") == "Deleted the Letsignit user sam@example.com."
    assert calls[-1]["method"] == "delete"


async def test_variables_are_checked_before_any_request(monkeypatch):
    calls = _answer(monkeypatch, payload={})
    handlers, _ = _tools()
    unknown = await handlers["update_letsignit_user_variables"](email="sam@example.com", variables={"salary": "1"})
    assert unknown.startswith("Error: unknown variable(s) salary.") and "job_title" in unknown
    custom = await handlers["update_letsignit_user_variables"](email="sam@example.com", variables={"custom": {"a": 1}})
    assert custom.startswith("Error: 'custom' must be")
    empty = await handlers["update_letsignit_user_variables"](email="sam@example.com", variables={})
    assert empty == "Error: give at least one variable to set."
    assert await handlers["delete_letsignit_user"](email="x/../y") == "Error: 'x/../y' is not an email address."
    assert calls == []


async def test_an_existing_user_is_not_created_twice(monkeypatch):
    _answer(monkeypatch, status=409)
    handlers, _ = _tools()
    answer = await handlers["create_letsignit_user"](email="sam@example.com")
    assert "already exists" in answer


def test_tools_sort_reads_before_writes_before_the_destructive_one():
    _, tools = _tools()
    assert {name: d.priority for name, d in tools.items()} == {
        "get_letsignit_signature": 0,
        "get_letsignit_user": 0,
        "create_letsignit_user": 1,
        "update_letsignit_user_variables": 1,
        "delete_letsignit_user": 2,
    }
    assert "Permanent" in tools["delete_letsignit_user"].description


async def test_the_test_button_accepts_a_key_even_without_a_signature(monkeypatch):
    def ctx(key: str):
        return ProbeContext(provider="letsignit", name="Letsignit", token=key, extra={})

    _answer(monkeypatch, status=404)
    assert (await check_connectivity(ctx("lsk"))).status == "ok"
    _answer(monkeypatch, payload=SIGNATURE)
    assert (await check_connectivity(ctx("lsk"))).status == "ok"
    _answer(monkeypatch, status=401)
    refused = await check_connectivity(ctx("lsk"))
    assert refused.status == "error" and "refused the API key" in refused.message
    assert (await check_connectivity(ctx("  "))).status == "error"
