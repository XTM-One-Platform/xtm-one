"""Regression tests for the HackerOne integration provider.

Pins the headline report-PDF download behaviour (the injected persist callback
is used when available; graceful fallback to an inline ``FileRef`` when
persistence is unavailable or fails), the program-handle requirement on report
listing, and the consolidated ``_manage`` action/id validation.
"""

from __future__ import annotations

import pytest

from app.integrations.providers import hackerone
from app.integrations.providers.hackerone import build_tools
from app.llm.base import FileRef
from app.services.integration_tools import _SafeIntegrationExecutor
from app.tools.base import FileContext, ToolResult

# api_username||api_token (HTTP Basic compound token)
_TOKEN = "tok-id||tok-value"


class _FakeResp:
    def __init__(
        self,
        status_code: int = 200,
        content: bytes = b"%PDF-1.4 fake",
        headers: dict | None = None,
    ):
        self.status_code = status_code
        self._content = content
        self.headers = headers or {}

    @property
    def content(self) -> bytes:
        return self._content

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def is_redirect(self) -> bool:
        return self.status_code in (301, 302, 303, 307, 308)


class _FakeClient:
    """Async-context httpx.AsyncClient stand-in that serves queued responses
    (one per GET) and records each GET's ``(url, headers)`` plus the remaining
    kwargs so tests can assert on them (e.g. that a redirect follow drops the
    Authorization header, or that the PDF tool sends ``pdf_type``)."""

    def __init__(self, responses: list[_FakeResp]):
        self._responses = list(responses)
        self.calls: list[tuple[str, dict | None]] = []
        self.kwargs: list[dict] = []

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get(self, url, headers: dict | None = None, **kwargs) -> _FakeResp:
        self.calls.append((str(url), headers))
        self.kwargs.append(kwargs)
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]


class _FakeJsonResp:
    """Minimal JSON response for the ``_api`` path (routes through safe_request)."""

    def __init__(self, payload: object, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.headers: dict = {}

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> object:
        return self._payload


class _FakeApiClient:
    """httpx.AsyncClient stand-in for the JSON ``_api`` path.

    ``safe_request`` dispatches through ``request(method, url, **kwargs)``, so
    this records every dispatched request. The strict ``get``/``delete`` verbs
    raise on a ``json=`` kwarg exactly like real httpx — a guard that fails the
    regression if the dispatch ever reverts to ``getattr(client, method)``.
    """

    def __init__(self, payload: object):
        self._payload = payload
        self.requests: list[tuple[str, str, dict]] = []

    async def __aenter__(self) -> _FakeApiClient:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def request(self, method, url, **kwargs) -> _FakeJsonResp:
        self.requests.append((str(method), str(url), kwargs))
        return _FakeJsonResp(self._payload)

    async def get(self, url, **kwargs) -> _FakeJsonResp:
        if "json" in kwargs:
            raise TypeError(
                "get() got an unexpected keyword argument 'json'"
            )
        return _FakeJsonResp(self._payload)

    async def delete(self, url, **kwargs) -> _FakeJsonResp:
        if "json" in kwargs:
            raise TypeError(
                "delete() got an unexpected keyword argument 'json'"
            )
        return _FakeJsonResp(self._payload)


def _patch_api_client(monkeypatch, payload: object) -> dict:
    """Patch ``httpx.AsyncClient`` for the JSON ``_api`` path."""
    holder: dict = {}

    def _factory(**kw):
        holder["client"] = _FakeApiClient(payload)
        return holder["client"]

    monkeypatch.setattr(hackerone.httpx, "AsyncClient", _factory)
    return holder


def _patch_pdf_response(monkeypatch, responses) -> dict:
    """Patch ``httpx.AsyncClient`` for the PDF path.

    ``responses`` is a single ``_FakeResp`` or a list served in order. Returns a
    holder whose ``client`` key is the created ``_FakeClient`` so tests can
    inspect ``.calls``.
    """
    if isinstance(responses, _FakeResp):
        responses = [responses]
    holder: dict = {}

    def _factory(**kw):
        holder["client"] = _FakeClient(responses)
        return holder["client"]

    monkeypatch.setattr(hackerone.httpx, "AsyncClient", _factory)
    return holder


def _handlers(persist_file_fn=None) -> dict:
    """Build the provider tools and return the registered handler map."""
    executor = _SafeIntegrationExecutor()
    build_tools(_TOKEN, executor, persist_file_fn=persist_file_fn)
    return executor._handlers


# ---------------------------------------------------------------------------
# Report PDF download — persist + fallback paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pdf_download_uses_persist_callback(monkeypatch):
    """When a persist callback is injected, the PDF becomes a download card."""
    _patch_pdf_response(monkeypatch, _FakeResp(content=b"%PDF-1.4 hello"))
    captured: dict = {}

    async def _persist(filename, raw_bytes, content_type):
        captured.update(filename=filename, size=len(raw_bytes), ct=content_type)
        return FileRef.from_bytes(
            data=raw_bytes,
            filename=filename,
            content_type=content_type,
            file_id="persisted-1",
        )

    handler = _handlers(persist_file_fn=_persist)["download_hackerone_report_pdf"]
    fc = FileContext()
    result = await handler(report_id="42", file_context=fc)

    assert isinstance(result, ToolResult)
    assert "[[FILE:persisted-1]]" in result.content
    assert result.files and result.files[0].file_id == "persisted-1"
    # Added to the shared file context so the loop can surface the download.
    assert fc.get("persisted-1") is not None
    assert captured["filename"] == "hackerone-report-42.pdf"
    assert captured["ct"] == "application/pdf"


@pytest.mark.asyncio
async def test_pdf_download_falls_back_when_persist_returns_none(monkeypatch):
    """Persist failure must degrade to an inline FileRef (no download card)."""
    _patch_pdf_response(monkeypatch, _FakeResp(content=b"%PDF-1.4 fallback"))

    async def _persist(filename, raw_bytes, content_type):
        return None  # persistence unavailable / failed

    handler = _handlers(persist_file_fn=_persist)["download_hackerone_report_pdf"]
    result = await handler(report_id="7", file_context=None)

    assert isinstance(result, ToolResult)
    assert "[[FILE:" not in result.content
    assert "attached to this conversation" in result.content
    assert result.files and result.files[0].filename == "hackerone-report-7.pdf"
    # Inline fallback carries the bytes so the LLM can still read the PDF.
    assert result.files[0].inline_data == b"%PDF-1.4 fallback"


@pytest.mark.asyncio
async def test_pdf_download_without_persist_fn_is_inline(monkeypatch):
    """No persist callback (older callers / channel-bot mode) -> inline FileRef."""
    _patch_pdf_response(monkeypatch, _FakeResp(content=b"%PDF data"))

    handler = _handlers(persist_file_fn=None)["download_hackerone_report_pdf"]
    result = await handler(report_id="9", file_context=None)

    assert isinstance(result, ToolResult)
    assert "[[FILE:" not in result.content
    assert result.files and result.files[0].inline_data == b"%PDF data"


@pytest.mark.asyncio
async def test_pdf_download_404_returns_actionable_message(monkeypatch):
    """A 404 must surface a clear message rather than a file."""
    _patch_pdf_response(monkeypatch, _FakeResp(status_code=404, content=b""))

    handler = _handlers()["download_hackerone_report_pdf"]
    result = await handler(report_id="404", file_context=None)

    assert isinstance(result, str)
    assert "not found" in result.lower()


@pytest.mark.asyncio
async def test_pdf_download_does_not_forward_credentials_on_redirect(monkeypatch):
    """A 3xx to a pre-signed CDN URL must be followed WITHOUT the Basic-auth
    header, so the HackerOne token never reaches a third-party host."""
    holder = _patch_pdf_response(
        monkeypatch,
        [
            _FakeResp(
                status_code=302,
                content=b"",
                headers={"location": "https://cdn.example.com/signed/report.pdf"},
            ),
            _FakeResp(status_code=200, content=b"%PDF-1.4 redirected"),
        ],
    )
    handler = _handlers()["download_hackerone_report_pdf"]
    result = await handler(report_id="42", file_context=None)

    assert isinstance(result, ToolResult)
    assert result.files and result.files[0].inline_data == b"%PDF-1.4 redirected"

    calls = holder["client"].calls
    assert len(calls) == 2
    # First hop to HackerOne carries Basic auth ...
    assert "Authorization" in (calls[0][1] or {})
    # ... the redirect follow targets the CDN and drops the credential.
    assert calls[1][0] == "https://cdn.example.com/signed/report.pdf"
    assert "Authorization" not in (calls[1][1] or {})


@pytest.mark.asyncio
async def test_pdf_download_empty_body_returns_message(monkeypatch):
    """A 200 with an empty body must not be surfaced as a zero-byte download."""
    _patch_pdf_response(monkeypatch, _FakeResp(status_code=200, content=b""))

    handler = _handlers()["download_hackerone_report_pdf"]
    result = await handler(report_id="5", file_context=None)

    assert isinstance(result, str)
    assert "empty pdf" in result.lower()


# ---------------------------------------------------------------------------
# Report listing — program handle requirement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pdf_download_rejects_traversal_report_id():
    """The PDF tool builds its own URL, so it must reject path-altering ids."""
    handler = _handlers()["download_hackerone_report_pdf"]
    for bad in ("../99", "1/2", ".."):
        result = await handler(report_id=bad, file_context=None)
        assert isinstance(result, str)
        assert "report_id must be" in result


@pytest.mark.asyncio
async def test_dedicated_tool_path_traversal_blocked_by_api_guard():
    """A traversal id in a dedicated tool is rejected before any HTTP call."""
    handler = _handlers()["get_hackerone_report"]
    result = await handler(report_id="../../me")
    assert "traversal" in result.lower()


@pytest.mark.asyncio
async def test_manage_rejects_id_with_path_separator():
    handler = _handlers()["manage_hackerone_report"]
    result = await handler(
        report_id="123/escalate", action="update_title", attributes={"title": "x"}
    )
    assert "invalid 'report_id'" in result
    assert "separators or traversal" in result


@pytest.mark.asyncio
async def test_list_reports_requires_program_handle():
    """Listing reports without a program handle returns guidance, not an API call."""
    handler = _handlers()["list_hackerone_reports"]
    result = await handler()
    assert "program handle is required" in result.lower()


# ---------------------------------------------------------------------------
# Consolidated manager — action / id validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manage_report_rejects_unknown_action():
    handler = _handlers()["manage_hackerone_report"]
    result = await handler(report_id="1", action="does_not_exist")
    assert "unknown action" in result.lower()
    # Lists the supported actions to help the LLM self-correct.
    assert "update_title" in result


@pytest.mark.asyncio
async def test_manage_program_requires_id_for_action():
    """A path placeholder with an empty id is rejected before any HTTP call."""
    handler = _handlers()["manage_hackerone_program"]
    result = await handler(program_id="", action="update_policy")
    assert "requires 'program_id'" in result


# ---------------------------------------------------------------------------
# JSON request dispatch — the GET-with-json crash + bodied DELETE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_tool_does_not_send_json_body(monkeypatch):
    """Regression: GET tools must NOT forward a ``json`` body.

    Forwarding ``json=None`` to ``httpx.AsyncClient.get`` raised
    ``AsyncClient.get() got an unexpected keyword argument 'json'`` and broke
    every HackerOne read tool. The request must dispatch as a bodyless GET.
    """
    holder = _patch_api_client(
        monkeypatch,
        {"data": [{"id": "1", "type": "program", "attributes": {"handle": "acme"}}]},
    )
    handler = _handlers()["list_hackerone_programs"]
    result = await handler()

    assert "acme" in result  # rendered the program list, no error string
    assert len(holder["client"].requests) == 1
    method, url, kwargs = holder["client"].requests[0]
    assert method == "GET"
    assert url.endswith("/me/programs")
    assert "json" not in kwargs  # the bug: a body must never ride on a GET


@pytest.mark.asyncio
async def test_bodied_delete_sends_json(monkeypatch):
    """A DELETE that carries a body (cancel-disclosure) must send the JSON body.

    ``httpx.AsyncClient.delete`` cannot carry ``json``; routing through
    ``request()`` lets the body ride on the DELETE verb.
    """
    holder = _patch_api_client(monkeypatch, {"data": {"id": "1", "type": "report"}})
    handler = _handlers()["manage_hackerone_report"]
    result = await handler(
        report_id="1", action="cancel_disclosure", attributes={"message": "stop"}
    )

    assert "error" not in result.lower()
    method, url, kwargs = holder["client"].requests[0]
    assert method == "DELETE"
    assert url.endswith("/reports/1/disclosure_requests")
    assert kwargs.get("json") == {"data": {"attributes": {"message": "stop"}}}


# ---------------------------------------------------------------------------
# Report PDF — pdf_type query param + permissive Accept (the HTTP 406 fix)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pdf_download_sends_pdf_type_and_permissive_accept(monkeypatch):
    """The PDF request must send ``pdf_type`` and a permissive Accept (no 406)."""
    holder = _patch_pdf_response(monkeypatch, _FakeResp(content=b"%PDF-1.4 full"))
    handler = _handlers()["download_hackerone_report_pdf"]
    result = await handler(report_id="42", pdf_type="full", file_context=None)

    assert isinstance(result, ToolResult)
    client = holder["client"]
    url, headers = client.calls[0]
    assert url.endswith("/reports/42/pdf")
    # Permissive Accept — a strict ``application/pdf`` is what triggered HTTP 406.
    assert headers.get("Accept") == "application/pdf, */*"
    assert "application/json" not in (headers.get("Accept") or "")
    assert client.kwargs[0].get("params") == {"pdf_type": "full"}


@pytest.mark.asyncio
async def test_pdf_download_rejects_invalid_pdf_type():
    handler = _handlers()["download_hackerone_report_pdf"]
    result = await handler(report_id="1", pdf_type="bogus", file_context=None)
    assert isinstance(result, str)
    assert "pdf_type must be" in result


@pytest.mark.asyncio
async def test_pdf_download_406_returns_actionable_message(monkeypatch):
    """A residual 406 must surface a clear, actionable message."""
    _patch_pdf_response(monkeypatch, _FakeResp(status_code=406, content=b""))
    handler = _handlers()["download_hackerone_report_pdf"]
    result = await handler(report_id="42", pdf_type="full", file_context=None)
    assert isinstance(result, str)
    assert "406" in result and "pdf_type='reporter'" in result


# ---------------------------------------------------------------------------
# Analytics — API-required parameters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analytics_requires_org_and_dates():
    """start_at, end_at and organization_id are required by the API."""
    handler = _handlers()["get_hackerone_analytics"]
    result = await handler(key="submissions")
    assert "requires" in result.lower()
    assert "organization_id" in result
    assert "start_at" in result and "end_at" in result


@pytest.mark.asyncio
async def test_analytics_rejects_invalid_interval():
    handler = _handlers()["get_hackerone_analytics"]
    result = await handler(
        key="submissions",
        start_at="2025-01-01",
        end_at="2025-02-01",
        organization_id="1",
        interval="day",
    )
    assert "interval must be" in result


@pytest.mark.asyncio
async def test_analytics_sends_required_params(monkeypatch):
    holder = _patch_api_client(monkeypatch, {"data": []})
    handler = _handlers()["get_hackerone_analytics"]
    result = await handler(
        key="submissions",
        start_at="2025-01-01",
        end_at="2025-02-01",
        organization_id="org-1",
        interval="month",
    )
    assert "Analytics result" in result
    _method, url, kwargs = holder["client"].requests[0]
    assert url.endswith("/analytics")
    params = kwargs.get("params") or {}
    assert params.get("organization_id") == "org-1"
    assert params.get("start_at") == "2025-01-01"
    assert params.get("end_at") == "2025-02-01"
    assert params.get("interval") == "month"
