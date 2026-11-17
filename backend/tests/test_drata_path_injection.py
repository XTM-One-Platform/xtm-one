"""Path-injection guards for Drata tools that interpolate a user-supplied
identifier into the request URL path.

Two tool parameters accept a free-form string that is placed directly into the
Drata API path:

  * ``list_drata_users(email=...)``  -> ``GET /users/email:<address>``
  * ``get_drata_personnel(personnel_id=...)`` -> ``GET /personnel/<id-or-email>``

Both must be normalized and validated through ``_common._user_ref`` before the
URL is built, so a value containing ``/`` / ``?`` / ``#`` / whitespace / other
reserved characters cannot inject into the request path. These tests pin that
a valid value reaches the documented path form and that an injection attempt
returns the friendly error WITHOUT issuing any HTTP request.
"""

from __future__ import annotations

import pytest

from app.integrations.providers import drata
from app.integrations.providers.drata import _common as drata_common
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://public-api.drata.com/public/v2||test-token"

# Values that must be rejected before any request is made. Each contains a
# character that would alter the request path/query/fragment if interpolated.
_INJECTION_VALUES = [
    "foo@acme.com/../../company",
    "foo@acme.com?expand[]=secret",
    "foo@acme.com#frag",
    "foo@acme.com/documents",
    "../company",
    "not an email",
    "12 34",
]


class _FakeResp:
    def __init__(self, payload: dict | None = None) -> None:
        self._payload = payload if payload is not None else {"id": 1}
        self.content = b"{}"

    def json(self) -> dict:
        return self._payload


def _patch_capture(monkeypatch, *, payload: dict | None = None) -> dict:
    captured: dict = {}

    async def _fake_safe_request(
        client, method, url, provider, action, *, expected_statuses=None, **kwargs
    ):
        captured["url"] = url
        captured["method"] = method
        return _FakeResp(payload=payload), None

    monkeypatch.setattr(drata_common, "safe_request", _fake_safe_request)
    return captured


def _build_executor() -> CallableToolExecutor:
    executor = CallableToolExecutor()
    drata.build_tools(_TOKEN, executor)
    return executor


async def _call(executor, name, args):
    return await executor.execute_tool(ToolCall(id="c1", name=name, arguments=args))


# ---------------------------------------------------------------------------
# list_drata_users — email fast-path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_users_valid_email_uses_email_ref(monkeypatch):
    captured = _patch_capture(
        monkeypatch, payload={"id": 1, "firstName": "A", "email": "a@acme.com"}
    )
    executor = _build_executor()
    await _call(executor, "list_drata_users", {"email": "A@Acme.com"})
    # Built via _user_ref -> documented ``email:<address>`` path form (the
    # email body is preserved; only the ``email:`` prefix is normalized).
    assert captured["url"].endswith("/users/email:A@Acme.com")


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", _INJECTION_VALUES)
async def test_list_users_rejects_path_injection(monkeypatch, bad):
    captured = _patch_capture(monkeypatch)
    executor = _build_executor()
    result = await _call(executor, "list_drata_users", {"email": bad})
    assert result.is_error is True  # a guarded rejection, not a raise
    assert "not a valid email" in result.content.lower()
    # No request must have been issued for an unsafe value.
    assert "url" not in captured


# ---------------------------------------------------------------------------
# get_drata_personnel — numeric ID or email:<address>
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_personnel_numeric_id(monkeypatch):
    captured = _patch_capture(monkeypatch, payload={"id": 42})
    executor = _build_executor()
    await _call(executor, "get_drata_personnel", {"personnel_id": "42"})
    assert captured["url"].endswith("/personnel/42")


@pytest.mark.asyncio
async def test_get_personnel_email_ref(monkeypatch):
    captured = _patch_capture(monkeypatch, payload={"id": 42})
    executor = _build_executor()
    await _call(
        executor, "get_drata_personnel", {"personnel_id": "leaver@acme.com"}
    )
    assert captured["url"].endswith("/personnel/email:leaver@acme.com")


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", _INJECTION_VALUES)
async def test_get_personnel_rejects_path_injection(monkeypatch, bad):
    captured = _patch_capture(monkeypatch)
    executor = _build_executor()
    result = await _call(executor, "get_drata_personnel", {"personnel_id": bad})
    assert result.is_error is True  # a guarded rejection, not a raise
    assert "personnel_id is required" in result.content
    # No request must have been issued for an unsafe value.
    assert "url" not in captured
