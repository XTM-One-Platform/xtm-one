"""Business SaaS integration files: link, download, inline, pipe.

HiBob employee documents, HubSpot files and record attachments, Ashby resumes
and candidate files, Perk invoice PDFs, Metabase question exports, Grafana
panel images, YouTrack issue attachments and Concord agreement documents.
Each provider API is a fake transport shaped like its documented answers;
nothing reaches a real provider.
"""

from __future__ import annotations

import contextlib
import json
import socket
from urllib.parse import parse_qs

import httpx
import pytest

from app.integrations import file_delivery
from app.integrations.providers import (
    ashby,
    concord,
    grafana,
    hibob,
    hubspot,
    metabase,
    perk,
    youtrack,
)
from app.services.integration_tools import _SafeIntegrationExecutor
from tests.integration_file_fakes import (
    HOSTILE_FILE_NAMES,
    RecordingPersist,
    assert_name_only_inside,
    call,
    fake_api,
    fenced_and_open,
    file_context_with,
)

_PDF = b"%PDF-1.4 signed agreement"
_PNG = b"\x89PNG\r\n\x1a\npanel"


def _executor(builder, token, persist=None, **kwargs):
    executor = _SafeIntegrationExecutor()
    builder(token, executor, persist_file_fn=persist, **kwargs)
    return executor


def _stored(result, persist: RecordingPersist, data: bytes) -> tuple[str, str, str]:
    """``(file_id, filename, content_type)`` of the stored file, after the common checks."""
    assert not result.is_error, result.content
    assert any(stored == data for _, stored, _ in persist.calls)
    (ref,) = result.files
    assert f"[[FILE:{ref.file_id}]]" in result.content
    assert f"file_id: {ref.file_id}" in result.content
    return ref.file_id, ref.filename, ref.content_type


def _path(request: httpx.Request) -> str:
    return request.url.path


# ---------------------------------------------------------------------------
# Shared download helpers
# ---------------------------------------------------------------------------


def test_a_content_disposition_names_the_file_rfc5987_first():
    assert file_delivery.content_disposition_filename('attachment; filename="q 1.csv"') == "q 1.csv"
    assert (
        file_delivery.content_disposition_filename(
            "attachment; filename=\"x.pdf\"; filename*=UTF-8''r%C3%A9sum%C3%A9.pdf"
        )
        == "r\u00e9sum\u00e9.pdf"
    )
    assert file_delivery.content_disposition_filename("attachment; filename*=ISO-8859-1''caf%E9.txt") == "caf\u00e9.txt"
    assert file_delivery.content_disposition_filename(None) == ""


#: Test host names and the address the fake resolver gives them.
_RESOLVED = {
    "storage.example": "52.216.0.10",
    "bob-docs.s3.amazonaws.com": "52.216.0.11",
    "internal.example": "10.1.2.3",
}


@pytest.fixture(autouse=True)
def _resolver(monkeypatch):
    """Resolve the test host names for the outbound URL policy, without DNS."""
    real = socket.getaddrinfo

    def getaddrinfo(host, *args, **kwargs):
        if host in _RESOLVED:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_RESOLVED[host], 0))]
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


async def _fetch(responder, url, **kwargs):
    with fake_api(responder) as sent:
        async with httpx.AsyncClient() as client:
            result = await file_delivery.fetch_file(
                client,
                "get",
                url,
                provider="Acme",
                action="download doc",
                max_bytes=1024,
                follow_redirects=True,
                **kwargs,
            )
    return result, sent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location",
    [
        "https://127.0.0.1/admin",
        "https://[::1]/admin",
        "https://169.254.169.254/latest/meta-data/",
        "https://10.0.0.5/internal.pdf",
        "https://internal.example/doc.pdf",
    ],
)
async def test_a_redirect_to_an_internal_address_is_not_followed(location):
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.provider.example":
            return httpx.Response(302, headers={"location": location})
        return httpx.Response(200, content=_PDF)

    result, sent = await _fetch(responder, "https://api.provider.example/doc")
    assert isinstance(result, str) and "which is not followed" in result, result
    assert [r.url.host for r in sent] == ["api.provider.example"]


@pytest.mark.asyncio
async def test_a_redirect_that_leaves_https_is_not_followed_even_on_the_same_host():
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.scheme == "https":
            return httpx.Response(302, headers={"location": "http://api.provider.example/doc"})
        return httpx.Response(200, content=_PDF)

    result, sent = await _fetch(responder, "https://api.provider.example/doc", headers={"X-API-KEY": "secret"})
    assert isinstance(result, str) and "it leaves HTTPS" in result
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_a_self_hosted_instance_on_a_private_address_keeps_its_same_origin_redirects():
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/export":
            return httpx.Response(302, headers={"location": "/download/export.csv"})
        return httpx.Response(200, content=_PDF)

    result, sent = await _fetch(responder, "https://10.0.0.5/api/export", headers={"X-Api-Key": "secret"})
    assert result.data == _PDF
    assert [r.headers["x-api-key"] for r in sent] == ["secret", "secret"]


@pytest.mark.asyncio
async def test_a_redirect_loop_stops_at_the_hop_limit():
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": f"/doc/{len(request.url.path)}"})

    result, sent = await _fetch(responder, "https://api.provider.example/doc")
    assert result == "Error: the Acme download was redirected more than 5 times."
    assert len(sent) == file_delivery._MAX_REDIRECTS + 1


@pytest.mark.asyncio
async def test_a_redirect_follows_its_location_without_the_first_query():
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.provider.example":
            return httpx.Response(302, headers={"location": "https://storage.example/doc.pdf?sig=1"})
        return httpx.Response(200, content=_PDF, headers={"content-type": "application/pdf"})

    with fake_api(responder) as sent:
        async with httpx.AsyncClient() as client:
            result = await file_delivery.fetch_file(
                client,
                "get",
                "https://api.provider.example/doc",
                provider="Acme",
                action="download doc",
                max_bytes=1024,
                params={"api_key": "secret"},
                follow_redirects=True,
            )
    assert result.data == _PDF
    first, storage = sent
    assert first.url.params["api_key"] == "secret"
    assert dict(storage.url.params) == {"sig": "1"}


@pytest.mark.asyncio
async def test_a_redirect_off_the_provider_origin_leaves_the_credentials_behind():
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/doc":
            return httpx.Response(302, headers={"location": "/moved/doc"})
        if request.url.path == "/moved/doc":
            return httpx.Response(302, headers={"location": "https://storage.example/doc.pdf?sig=1"})
        return httpx.Response(200, content=_PDF, headers={"content-type": "application/pdf"})

    with fake_api(responder) as sent:
        async with httpx.AsyncClient() as client:
            result = await file_delivery.fetch_file(
                client,
                "get",
                "https://api.provider.example/doc",
                provider="Acme",
                action="download doc",
                max_bytes=1024,
                headers={"X-API-KEY": "secret", "Authorization": "Bearer secret"},
                follow_redirects=True,
            )
    assert result.data == _PDF
    first, same_origin, storage = sent
    assert first.headers["x-api-key"] == same_origin.headers["x-api-key"] == "secret"
    assert storage.url.host == "storage.example"
    assert "x-api-key" not in storage.headers and "authorization" not in storage.headers


@pytest.mark.asyncio
async def test_past_the_origin_no_client_credential_follows_the_download():
    """An AutoRefreshClient token and a client's default headers stay on the provider."""
    from app.integrations.http import AutoRefreshClient

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.provider.example":
            return httpx.Response(302, headers={"location": "https://storage.example/rec.mp4?sig=1"})
        return httpx.Response(403, json={"message": "expired"})

    async def refresh():
        return "fresh-token"

    with fake_api(responder) as sent:
        async with AutoRefreshClient({"Authorization": "Bearer t"}, refresh_fn=refresh) as auto:
            refused = await file_delivery.fetch_file(
                auto, "get", "https://api.provider.example/rec", provider="Acme", action="download", max_bytes=1024,
                follow_redirects=True,
            )
        async with httpx.AsyncClient(headers={"X-API-KEY": "secret"}, follow_redirects=True) as plain:
            await file_delivery.fetch_file(
                plain, "get", "https://api.provider.example/rec", provider="Acme", action="download", max_bytes=1024
            )
    assert refused.status == 403
    provider_hops = [r for r in sent if r.url.host == "api.provider.example"]
    storage_hops = [r for r in sent if r.url.host == "storage.example"]
    assert provider_hops[0].headers["authorization"] == "Bearer t"
    assert provider_hops[1].headers["x-api-key"] == "secret"
    assert len(storage_hops) == 2
    assert all("authorization" not in r.headers and "x-api-key" not in r.headers for r in storage_hops)


@pytest.mark.asyncio
async def test_a_chain_that_returns_to_its_start_origin_is_checked_again(monkeypatch):
    checked: list[str] = []

    async def validate(url: str) -> str:
        checked.append(url)
        if url.startswith("https://api.provider.example"):
            raise ValueError("URL targets a private or non-routable address: 127.0.0.1")
        return url

    monkeypatch.setattr(file_delivery, "validate_outbound_url_async", validate)

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.provider.example":
            return httpx.Response(302, headers={"location": "https://bounce.example/doc"})
        return httpx.Response(302, headers={"location": "https://api.provider.example/doc?again=1"})

    with fake_api(responder) as sent:
        async with httpx.AsyncClient() as client:
            result = await file_delivery.fetch_file(
                client, "get", "https://api.provider.example/doc", provider="Acme", action="download",
                max_bytes=1024, follow_redirects=True,
            )
    assert isinstance(result, str) and "private or non-routable address" in result
    assert [r.url.host for r in sent] == ["api.provider.example", "bounce.example"]
    assert checked == ["https://bounce.example/doc", "https://api.provider.example/doc?again=1"]


@pytest.mark.asyncio
async def test_an_explicit_default_port_is_the_same_origin():
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/doc":
            return httpx.Response(302, headers={"location": "https://api.provider.example:443/moved"})
        return httpx.Response(200, content=_PDF, headers={"content-type": "application/pdf"})

    with fake_api(responder) as sent:
        async with httpx.AsyncClient() as client:
            result = await file_delivery.fetch_file(
                client, "get", "https://api.provider.example/doc", provider="Acme", action="download",
                max_bytes=1024, headers={"X-API-KEY": "secret"}, follow_redirects=True,
            )
    assert result.data == _PDF
    assert sent[1].headers["x-api-key"] == "secret"


@pytest.mark.asyncio
async def test_a_redirect_without_a_location_is_an_error_not_a_crash():
    with fake_api(lambda r: httpx.Response(302, json={"message": "moved"})) as sent:
        async with httpx.AsyncClient() as client:
            result = await file_delivery.fetch_file(
                client,
                "get",
                "https://api.provider.example/doc",
                provider="Acme",
                action="download doc",
                max_bytes=1024,
                follow_redirects=True,
            )
    assert isinstance(result, str) and result.startswith("Error")
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_an_invalid_download_url_is_an_error_not_a_crash():
    async with httpx.AsyncClient() as client:
        result = await file_delivery.fetch_file(
            client, "get", "http://[::1", provider="Acme", action="download a", max_bytes=1024
        )
    assert isinstance(result, str) and result.startswith("Error: Acme request failed")


def test_a_force_download_answer_is_typed_by_the_file_name():
    assert file_delivery.file_content_type("application/force-download;charset=UTF-8", "a.pdf") == "application/pdf"
    assert file_delivery.file_content_type("text/csv; charset=utf-8", "export") == "text/csv"
    assert file_delivery.file_content_type("", "blob") == "application/octet-stream"
    assert file_delivery.with_extension("Offer letter", "application/pdf") == "Offer letter.pdf"
    assert file_delivery.with_extension("offer.docx", "application/pdf") == "offer.docx"


@pytest.mark.asyncio
async def test_a_download_over_the_cap_is_refused_before_it_is_held():
    cap = 1024 * 1024

    async def chunks():
        for _ in range(3):
            yield b"x" * (cap // 2)

    declared = lambda r: httpx.Response(200, content=b"x" * (cap + 1))  # noqa: E731
    streamed = lambda r: httpx.Response(200, content=chunks())  # noqa: E731
    for responder in (declared, streamed):
        with fake_api(responder):
            async with httpx.AsyncClient() as client:
                result = await file_delivery.fetch_file(
                    client, "get", "https://files.example/a", provider="Acme", action="download a", max_bytes=cap
                )
        assert result == "Error: the Acme file is over the 1 MB download limit."


@pytest.mark.asyncio
async def test_a_failed_download_answers_the_provider_error():
    with fake_api(lambda r: httpx.Response(403, json={"message": "forbidden"})):
        async with httpx.AsyncClient() as client:
            result = await file_delivery.fetch_file(
                client, "get", "https://files.example/a", provider="Acme", action="download a", max_bytes=1024
            )
    assert isinstance(result, str) and result.startswith("Error")


# ---------------------------------------------------------------------------
# HiBob employee documents
# ---------------------------------------------------------------------------

_HIBOB_DOCS = "https://api.hibob.com/v1/docs/people/e1"
_HIBOB_LINK = "https://app.hibob.com/api/docs/download/77"
_HIBOB_S3 = "https://bob-docs.s3.amazonaws.com/contract.pdf"


def _hibob(request: httpx.Request) -> httpx.Response:
    url = str(request.url).split("?")[0]
    if url == _HIBOB_DOCS and request.method == "GET":
        return httpx.Response(
            200,
            json={
                "documents": [
                    {"documentName": "Offer letter", "downloadLink": _HIBOB_LINK},
                    {"documentName": "contract.pdf", "downloadLink": f"{_HIBOB_S3}?X-Amz-Signature=sig"},
                ]
            },
        )
    if url == _HIBOB_LINK:
        return httpx.Response(200, content=_PDF, headers={"content-type": "application/pdf"})
    if url == _HIBOB_S3:
        return httpx.Response(200, content=_PDF, headers={"content-type": "application/octet-stream"})
    if url in (f"{_HIBOB_DOCS}/shared/upload", f"{_HIBOB_DOCS}/folders/f9/upload"):
        return httpx.Response(200, json={"id": 501})
    if url == f"{_HIBOB_DOCS}/shared":
        return httpx.Response(200, json={"id": 502, "name": "Policy"})
    return httpx.Response(404, json={})


@pytest.mark.asyncio
async def test_hibob_documents_are_listed_with_their_download_links():
    executor = _executor(hibob.build_tools, "svc||key")
    with fake_api(_hibob):
        result = await call(executor, "list_hibob_employee_documents", employee_id="e1")
    assert "Offer letter | #1" in result.content
    assert f"Download URL: {_HIBOB_LINK}" in result.content
    assert "contract.pdf | #2" in result.content


@pytest.mark.asyncio
async def test_hibob_downloads_a_document_by_name_with_its_credential_on_hibob_only():
    persist = RecordingPersist()
    executor = _executor(hibob.build_tools, "svc||key", persist)
    with fake_api(_hibob) as sent:
        by_name = await call(executor, "download_hibob_document", employee_id="e1", document="offer letter")
        by_position = await call(executor, "download_hibob_document", employee_id="e1", document="#2")
    _, name, content_type = _stored(by_name, persist, _PDF)
    assert (name, content_type) == ("Offer letter.pdf", "application/pdf")
    assert f"Download URL: {_HIBOB_LINK}" in by_name.content
    _, name, content_type = _stored(by_position, persist, _PDF)
    assert (name, content_type) == ("contract.pdf", "application/pdf")
    hibob_fetch = next(r for r in sent if r.url.host == "app.hibob.com")
    storage_fetch = next(r for r in sent if r.url.host == "bob-docs.s3.amazonaws.com")
    assert hibob_fetch.headers["authorization"].startswith("Basic ")
    assert "authorization" not in storage_fetch.headers


@pytest.mark.asyncio
async def test_hibob_a_download_link_redirected_to_storage_leaves_the_credential_on_hibob():
    """A hibob.com link answered with a redirect to a storage host."""

    def responder(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _HIBOB_LINK:
            return httpx.Response(302, headers={"location": f"{_HIBOB_S3}?X-Amz-Signature=sig"})
        return _hibob(request)

    persist = RecordingPersist()
    executor = _executor(hibob.build_tools, "svc||key", persist)
    with fake_api(responder) as sent:
        result = await call(executor, "download_hibob_document", employee_id="e1", document="offer letter")
    _stored(result, persist, _PDF)
    link_fetch, storage_fetch = [r for r in sent if r.url.path != "/v1/docs/people/e1"]
    assert link_fetch.url.host == "app.hibob.com"
    assert link_fetch.headers["authorization"].startswith("Basic ")
    assert storage_fetch.url.host == "bob-docs.s3.amazonaws.com"
    assert "authorization" not in storage_fetch.headers


@pytest.mark.asyncio
async def test_hibob_a_download_link_redirected_to_an_internal_address_is_refused():
    def responder(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _HIBOB_LINK:
            return httpx.Response(302, headers={"location": "https://169.254.169.254/latest/meta-data/"})
        return _hibob(request)

    executor = _executor(hibob.build_tools, "svc||key", RecordingPersist())
    with fake_api(responder) as sent:
        result = await call(executor, "download_hibob_document", employee_id="e1", document="offer letter")
    assert "which is not followed" in result.content
    assert all(r.url.host != "169.254.169.254" for r in sent)


@pytest.mark.asyncio
async def test_hibob_an_unknown_document_names_the_ones_there_are():
    executor = _executor(hibob.build_tools, "svc||key")
    with fake_api(_hibob):
        result = await call(executor, "download_hibob_document", employee_id="e1", document="payslip")
    assert result.is_error
    assert "Offer letter, contract.pdf" in result.content


@pytest.mark.asyncio
async def test_hibob_uploads_any_file_of_the_run_to_a_folder():
    executor = _executor(hibob.build_tools, "svc||key")
    ctx, file_id = file_context_with(_PDF, "signed.pdf", "application/pdf")
    executor.file_context = ctx
    with fake_api(_hibob) as sent:
        shared = await call(executor, "upload_hibob_document", employee_id="e1", file_id=file_id, document_name="Signed offer")
        custom = await call(
            executor, "upload_hibob_document", employee_id="e1", file_id=file_id, folder_type="custom", folder_id="f9"
        )
        missing = await call(executor, "upload_hibob_document", employee_id="e1", file_id=file_id, folder_type="custom")
    assert not shared.is_error, shared.content
    assert "Doc ID: 501" in shared.content
    assert not custom.is_error, custom.content
    assert missing.is_error and "folder_id is required" in missing.content
    uploads = [r for r in sent if r.method == "POST"]
    assert [r.url.path for r in uploads] == ["/v1/docs/people/e1/shared/upload", "/v1/docs/people/e1/folders/f9/upload"]
    assert b'name="file"; filename="Signed offer.pdf"' in uploads[0].content
    assert _PDF in uploads[0].content
    assert uploads[0].headers["content-type"].startswith("multipart/form-data")


@pytest.mark.asyncio
async def test_hibob_a_url_upload_sends_the_documented_body():
    executor = _executor(hibob.build_tools, "svc||key")
    with fake_api(_hibob) as sent:
        result = await call(
            executor, "upload_hibob_document_url", employee_id="e1", url="https://files.example/policies/Policy%202026.pdf"
        )
    assert "Doc ID: 502" in result.content
    assert json.loads(sent[0].content) == {
        "documentName": "Policy 2026.pdf",
        "documentUrl": "https://files.example/policies/Policy%202026.pdf",
    }


# ---------------------------------------------------------------------------
# HubSpot files and record attachments
# ---------------------------------------------------------------------------

_HS_SIGNED = "https://cdn.hubspotusercontent.example/signed/deck.pdf?Expires=1&Signature=s"
_HS_DECK = {
    "id": "7001",
    "name": "deck",
    "extension": "pdf",
    "size": len(_PDF),
    "type": "DOCUMENT",
    "access": "PRIVATE",
    "url": "https://12345.fs1.hubspotusercontent-na1.net/hubfs/12345/deck.pdf",
}


class _Unpaced:
    @contextlib.asynccontextmanager
    async def slot(self, *args, **kwargs):
        yield


@pytest.fixture
def unpaced_hubspot(monkeypatch):
    """HubSpot's shared account limiter paces requests at 4/s; not in a unit test."""
    monkeypatch.setattr(hubspot, "get_shared_rate_limiter", lambda *a, **k: _Unpaced())


def _hubspot(request: httpx.Request) -> httpx.Response:
    path = _path(request)
    if request.url.host == "cdn.hubspotusercontent.example":
        return httpx.Response(200, content=_PDF, headers={"content-type": "application/pdf"})
    if path == "/crm/v4/objects/deals/9/associations/notes":
        return httpx.Response(200, json={"results": [{"toObjectId": 301, "associationTypes": []}]})
    if path.startswith("/crm/v4/objects/deals/9/associations/"):
        return httpx.Response(200, json={"results": []})
    if path == "/crm/v3/objects/notes/batch/read":
        body = json.loads(request.content)
        assert "hs_attachment_ids" in body["properties"]
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "301",
                        "properties": {
                            "hs_attachment_ids": "7001;7002",
                            "hs_note_body": "<p>Signed <b>deck</b></p>",
                            "hs_timestamp": "2026-09-01T10:00:00Z",
                        },
                    }
                ]
            },
        )
    if path == "/files/v3/files/7001":
        return httpx.Response(200, json=_HS_DECK)
    if path == "/files/v3/files/7002":
        return httpx.Response(200, json={"id": "7002", "name": "logo", "extension": "png", "size": 10, "type": "IMG"})
    if path == "/files/v3/files/7001/signed-url":
        return httpx.Response(200, json={"url": _HS_SIGNED, "expiresAt": "2026-09-29T18:00:00Z", "name": "deck"})
    if path == "/files/v3/files/search":
        return httpx.Response(200, json={"results": [_HS_DECK], "paging": {"next": {"after": "abc"}}})
    if path == "/files/v3/files" and request.method == "POST":
        return httpx.Response(201, json={"id": "8001", "name": "report", "extension": "pdf", "access": "PRIVATE"})
    if path == "/crm/v3/objects/notes" and request.method == "POST":
        return httpx.Response(201, json={"id": "401"})
    if path == "/crm/v4/objects/notes/401/associations/default/deals/9" and request.method == "PUT":
        return httpx.Response(200, json={})
    return httpx.Response(404, json={"message": "not found"})


@pytest.mark.asyncio
async def test_hubspot_lists_a_record_attachments_through_its_engagements(unpaced_hubspot):
    executor = _executor(hubspot.build_tools, "pat-token")
    with fake_api(_hubspot):
        result = await call(executor, "list_hubspot_attachments", object_type="deals", record_id="9")
    assert not result.is_error, result.content
    assert "Attachments on deals 9 (2 file(s))" in result.content
    assert "note 301 'Signed deck'" in result.content
    assert "deck.pdf (file id: 7001)" in result.content
    assert f"URL: {_HS_DECK['url']}" in result.content
    assert "logo.png (file id: 7002)" in result.content


@pytest.mark.asyncio
async def test_hubspot_downloads_a_file_through_its_signed_url_without_the_token(unpaced_hubspot):
    persist = RecordingPersist()
    executor = _executor(hubspot.build_tools, "pat-token", persist)
    with fake_api(_hubspot) as sent:
        result = await call(executor, "download_hubspot_file", file_id="7001")
    _, name, content_type = _stored(result, persist, _PDF)
    assert (name, content_type) == ("deck.pdf", "application/pdf")
    assert f"Download URL: {_HS_SIGNED}" in result.content
    assert f"File URL: {_HS_DECK['url']}" in result.content
    cdn = next(r for r in sent if r.url.host == "cdn.hubspotusercontent.example")
    assert "authorization" not in cdn.headers


@pytest.mark.asyncio
async def test_hubspot_attaches_a_file_of_the_run_to_a_record_through_a_note(unpaced_hubspot):
    executor = _executor(hubspot.build_tools, "pat-token")
    ctx, file_id = file_context_with(_PDF, "report.pdf", "application/pdf")
    executor.file_context = ctx
    with fake_api(_hubspot) as sent:
        result = await call(
            executor, "upload_hubspot_attachment", object_type="deals", record_id="9", file_id=file_id, note="Q3 report"
        )
    assert not result.is_error, result.content
    inside, outside = fenced_and_open(result.content)
    assert outside == f"Attached file {file_id} to deals 9 through note 401 (file id: 8001).\n"
    assert "Name: report.pdf" in inside.splitlines()
    upload, note, association = sent
    assert upload.url.path == "/files/v3/files"
    assert _PDF in upload.content
    assert b'{"access": "PRIVATE"}' in upload.content
    assert b"/agent-uploads" in upload.content
    assert json.loads(note.content)["properties"]["hs_attachment_ids"] == "8001"
    assert json.loads(note.content)["properties"]["hs_note_body"] == "Q3 report"
    assert association.method == "PUT"


@pytest.mark.asyncio
async def test_hubspot_searches_the_file_manager(unpaced_hubspot):
    executor = _executor(hubspot.build_tools, "pat-token")
    with fake_api(_hubspot) as sent:
        result = await call(executor, "search_hubspot_files", name="deck", extension=".pdf")
    assert "deck.pdf (file id: 7001)" in result.content
    assert 'after="abc"' in result.content
    assert sent[0].url.params["extension"] == "pdf"


def test_hubspot_file_manager_tools_are_withdrawn_under_an_object_type_scope():
    executor = _SafeIntegrationExecutor()
    names = {d.name for d in hubspot.build_tools("pat-token", executor, object_type_scope=frozenset({"deal"}))}
    assert not {"search_hubspot_files", "upload_hubspot_file"} & names
    assert {"list_hubspot_attachments", "download_hubspot_file", "upload_hubspot_attachment"} <= names


@pytest.mark.asyncio
async def test_hubspot_under_a_scope_a_file_comes_only_through_an_allowed_record(unpaced_hubspot):
    persist = RecordingPersist()
    executor = _SafeIntegrationExecutor()
    hubspot.build_tools("pat-token", executor, persist_file_fn=persist, object_type_scope=frozenset({"deal"}))
    with fake_api(_hubspot) as sent:
        unanchored = await call(executor, "download_hubspot_file", file_id="7001")
        other_type = await call(executor, "list_hubspot_attachments", object_type="contacts", record_id="1")
        assert sent == []
        not_attached = await call(executor, "download_hubspot_file", file_id="9999", object_type="deals", record_id="9")
        assert not any(r.url.path.startswith("/files/") for r in sent)
        attached = await call(executor, "download_hubspot_file", file_id="7001", object_type="deal", record_id="9")
    assert unanchored.is_error and "pass object_type" in unanchored.content
    assert other_type.is_error and "'contacts' is outside the Scope(s)" in other_type.content
    assert not_attached.is_error and "file 9999 is not attached to deals 9" in not_attached.content
    _stored(attached, persist, _PDF)


# ---------------------------------------------------------------------------
# Ashby resumes and candidate files
# ---------------------------------------------------------------------------

_ASHBY_S3 = "https://s3.amazonaws.com/ashby-files/resume.pdf"
_ASHBY_CANDIDATE = {
    "id": "c1",
    "name": "Ada Lovelace",
    "profileUrl": "https://app.ashbyhq.com/candidates/c1",
    "resumeFileHandle": {"id": "f-r", "name": "ada-resume.pdf", "handle": "h-resume"},
    "fileHandles": [{"id": "f-p", "name": "portfolio.pdf", "handle": "h-portfolio"}],
}


def _ashby(request: httpx.Request) -> httpx.Response:
    path = _path(request)
    if request.url.host == "s3.amazonaws.com":
        return httpx.Response(200, content=_PDF, headers={"content-type": "binary/octet-stream"})
    if path == "/candidate.info":
        return httpx.Response(200, json={"success": True, "results": _ASHBY_CANDIDATE})
    if path == "/file.info":
        handle = json.loads(request.content)["fileHandle"]
        return httpx.Response(200, json={"success": True, "results": {"url": f"{_ASHBY_S3}?h={handle}"}})
    if path == "/candidate.uploadResume":
        return httpx.Response(200, json={"success": True, "results": _ASHBY_CANDIDATE})
    if path == "/candidate.uploadFile":
        return httpx.Response(200, json={"success": False, "errors": [{"message": "File type not allowed"}]})
    return httpx.Response(404, json={})


@pytest.mark.asyncio
async def test_ashby_a_candidate_read_names_the_resume_and_files():
    executor = _executor(ashby.build_tools, "ashby-key")
    with fake_api(_ashby):
        result = await call(executor, "get_ashby_candidate", candidate_id="c1")
    assert "Resume: ada-resume.pdf (file id: f-r)" in result.content
    assert "Files: portfolio.pdf (file id: f-p)" in result.content
    assert "Profile: https://app.ashbyhq.com/candidates/c1" in result.content


@pytest.mark.asyncio
async def test_ashby_downloads_the_resume_or_a_file_through_file_info():
    persist = RecordingPersist()
    executor = _executor(ashby.build_tools, "ashby-key", persist)
    with fake_api(_ashby) as sent:
        resume = await call(executor, "download_ashby_candidate_file", candidate_id="c1")
        portfolio = await call(executor, "download_ashby_candidate_file", candidate_id="c1", file_id="f-p")
    _, name, content_type = _stored(resume, persist, _PDF)
    assert (name, content_type) == ("ada-resume.pdf", "application/pdf")
    assert f"Download URL: {_ASHBY_S3}?h=h-resume" in resume.content
    _, name, _ = _stored(portfolio, persist, _PDF)
    assert name == "portfolio.pdf"
    handles = [json.loads(r.content)["fileHandle"] for r in sent if r.url.path == "/file.info"]
    assert handles == ["h-resume", "h-portfolio"]
    assert all("authorization" not in r.headers for r in sent if r.url.host == "s3.amazonaws.com")


@pytest.mark.asyncio
async def test_ashby_uploads_a_file_of_the_run_as_the_resume_and_reports_a_refusal():
    executor = _executor(ashby.build_tools, "ashby-key")
    ctx, file_id = file_context_with(_PDF, "cv.pdf", "application/pdf")
    executor.file_context = ctx
    with fake_api(_ashby) as sent:
        resume = await call(executor, "upload_ashby_candidate_file", candidate_id="c1", file_id=file_id, as_resume=True)
        other = await call(executor, "upload_ashby_candidate_file", candidate_id="c1", file_id=file_id)
    assert not resume.is_error, resume.content
    assert "as the resume of candidate c1" in resume.content
    assert other.is_error and "File type not allowed" in other.content
    upload = sent[0]
    assert upload.url.path == "/candidate.uploadResume"
    assert b'name="resume"; filename="cv.pdf"' in upload.content
    assert b'name="candidateId"' in upload.content and b"c1" in upload.content
    assert b'name="file"; filename="cv.pdf"' in sent[1].content


# ---------------------------------------------------------------------------
# Perk invoice PDFs
# ---------------------------------------------------------------------------

_PERK_PDF = "https://api.travelperk.com/invoices/INV-01-9/pdf"


def _perk(request: httpx.Request) -> httpx.Response:
    path = _path(request)
    if path == "/invoices":
        return httpx.Response(
            200,
            json={
                "invoices": [
                    {
                        "serial_number": "INV-01-9",
                        "status": "paid",
                        "total": "120.00",
                        "currency": "EUR",
                        "issuing_date": "2026-09-01",
                        "pdf": _PERK_PDF,
                    }
                ],
                "total": 1,
            },
        )
    if path == "/invoices/INV-01-9/pdf":
        return httpx.Response(200, content=_PDF, headers={"content-type": "application/pdf"})
    return httpx.Response(404, json={})


@pytest.mark.asyncio
async def test_perk_invoices_carry_their_pdf_and_the_pdf_is_stored():
    persist = RecordingPersist()
    executor = _executor(perk.build_tools, "perk-key", persist)
    with fake_api(_perk) as sent:
        listing = await call(executor, "list_perk_invoices")
        pdf = await call(executor, "download_perk_invoice_pdf", serial_number="#INV-01-9")
    assert f"PDF: {_PERK_PDF}" in listing.content
    _, name, content_type = _stored(pdf, persist, _PDF)
    assert (name, content_type) == ("INV-01-9.pdf", "application/pdf")
    assert sent[-1].headers["authorization"] == "ApiKey perk-key"
    assert sent[-1].headers["api-version"] == "1"


# ---------------------------------------------------------------------------
# Metabase question exports
# ---------------------------------------------------------------------------


def _metabase(request: httpx.Request) -> httpx.Response:
    path = _path(request)
    if path == "/api/card/42/query/csv":
        return httpx.Response(
            200,
            content=b"region,total\nEU,12\n",
            headers={"content-type": "text/csv", "content-disposition": 'attachment; filename="sales_2026.csv"'},
        )
    if path == "/api/card/43/query/xlsx":
        return httpx.Response(202, json={"status": "failed", "error": "Column 'x' not found"})
    return httpx.Response(404, json={})


@pytest.mark.asyncio
async def test_metabase_exports_a_question_as_a_stored_file():
    persist = RecordingPersist()
    executor = _executor(metabase.build_tools, "https://mb.example||mb-key", persist)
    params = '[{"type":"category","value":"EU","target":["variable",["template-tag","region"]]}]'
    with fake_api(_metabase) as sent:
        result = await call(executor, "export_metabase_question", card_id=42, parameters=params)
    _, name, content_type = _stored(result, persist, b"region,total\nEU,12\n")
    assert (name, content_type) == ("sales_2026.csv", "text/csv")
    assert "Web URL: https://mb.example/question/42" in result.content
    (export,) = sent
    assert export.headers["content-type"] == "application/x-www-form-urlencoded"
    assert export.headers["x-api-key"] == "mb-key"
    form = parse_qs(export.content.decode())
    assert form["format_rows"] == ["false"]
    assert json.loads(form["parameters"][0])[0]["value"] == "EU"


@pytest.mark.asyncio
async def test_metabase_a_failed_export_is_an_error_not_a_file():
    persist = RecordingPersist()
    executor = _executor(metabase.build_tools, "https://mb.example||mb-key", persist)
    with fake_api(_metabase):
        failed = await call(executor, "export_metabase_question", card_id=43, export_format="xlsx")
        bad = await call(executor, "export_metabase_question", card_id=42, export_format="pdf")
    assert failed.is_error and "Column 'x' not found" in failed.content
    assert bad.is_error and "csv, xlsx, json" in bad.content
    assert persist.calls == []


# ---------------------------------------------------------------------------
# Grafana panel images
# ---------------------------------------------------------------------------


def _grafana(renderer: bool):
    def responder(request: httpx.Request) -> httpx.Response:
        path = _path(request)
        if path == "/api/frontend/settings":
            return httpx.Response(200, json={"rendererAvailable": renderer})
        if path == "/api/dashboards/uid/abc":
            return httpx.Response(
                200,
                json={
                    "dashboard": {
                        "title": "API health",
                        "panels": [{"type": "row", "panels": [{"id": 2, "title": "Latency p95", "type": "timeseries"}]}],
                    },
                    "meta": {"slug": "api-health"},
                },
            )
        if path == "/render/d-solo/abc/api-health":
            return httpx.Response(200, content=_PNG, headers={"content-type": "image/png"})
        return httpx.Response(404, json={})

    return responder


@pytest.mark.asyncio
async def test_grafana_renders_a_panel_as_a_stored_image():
    persist = RecordingPersist()
    executor = _executor(grafana.build_tools, "https://grafana.example||glsa_key", persist)
    with fake_api(_grafana(renderer=True)) as sent:
        result = await call(
            executor, "export_grafana_panel_image", dashboard_uid="abc", panel_id=2, from_time="now-24h", variables="env=prod"
        )
    _, name, content_type = _stored(result, persist, _PNG)
    assert (name, content_type) == ("API-health-Latency-p95.png", "image/png")
    render = next(r for r in sent if r.url.path.startswith("/render/"))
    assert render.url.params["panelId"] == "2"
    assert render.url.params["from"] == "now-24h"
    assert render.url.params["var-env"] == "prod"
    assert render.headers["authorization"] == "Bearer glsa_key"
    assert "Web URL: https://grafana.example/d/abc/api-health?viewPanel=2" in result.content


@pytest.mark.asyncio
async def test_grafana_without_an_image_renderer_says_so_and_renders_nothing():
    persist = RecordingPersist()
    executor = _executor(grafana.build_tools, "https://grafana.example||glsa_key", persist)
    with fake_api(_grafana(renderer=False)) as sent:
        result = await call(executor, "export_grafana_panel_image", dashboard_uid="abc", panel_id=2)
    assert result.is_error
    assert "no image renderer" in result.content
    assert "https://grafana.com/docs/grafana/latest/setup-grafana/image-rendering/" in result.content
    assert not any(r.url.path.startswith("/render/") for r in sent)
    assert persist.calls == []


@pytest.mark.asyncio
async def test_grafana_an_unknown_panel_names_the_panels_there_are():
    executor = _executor(grafana.build_tools, "https://grafana.example||glsa_key")
    with fake_api(_grafana(renderer=True)):
        result = await call(executor, "export_grafana_panel_image", dashboard_uid="abc", panel_id=9)
    assert result.is_error and "[2] Latency p95" in result.content


# ---------------------------------------------------------------------------
# YouTrack issue attachments
# ---------------------------------------------------------------------------

_LOG = b"2026-09-01 ERROR leak detected\n"
_YT_ATTACHMENT = {
    "id": "7-2",
    "name": "trace.log",
    "size": len(_LOG),
    "mimeType": "text/plain",
    "url": "/api/files/7-2?sign=abc&updated=1",
    "created": 1554209839634,
    "author": {"login": "ana", "fullName": "Ana"},
}
_YT_URL = "https://acme.youtrack.cloud/api/files/7-2?sign=abc&updated=1"


def _youtrack(request: httpx.Request) -> httpx.Response:
    path = _path(request)
    if path == "/api/issues/PROJ-1" and request.method == "GET":
        return httpx.Response(
            200,
            json={
                "idReadable": "PROJ-1",
                "summary": "Leak",
                "attachments": [_YT_ATTACHMENT, {"id": "7-3", "name": "old.png", "removed": True}],
            },
        )
    if path == "/api/issues/PROJ-1/comments":
        return httpx.Response(200, json=[])
    if path == "/api/issues/PROJ-1/attachments" and request.method == "GET":
        return httpx.Response(200, json=[_YT_ATTACHMENT])
    if path == "/api/issues/PROJ-1/attachments" and request.method == "POST":
        return httpx.Response(200, json=[{"id": "7-9", "name": "report.pdf", "url": "/api/files/7-9?sign=x", "size": len(_PDF)}])
    if path == "/api/files/7-2":
        return httpx.Response(200, content=_LOG, headers={"content-type": "application/octet-stream"})
    return httpx.Response(404, json={})


@pytest.mark.asyncio
async def test_youtrack_an_issue_read_lists_its_attachments_with_their_links():
    executor = _executor(youtrack.build_tools, "https://acme.youtrack.cloud||perm-token")
    with fake_api(_youtrack) as sent:
        issue = await call(executor, "get_youtrack_issue", issue_id="PROJ-1")
        listing = await call(executor, "list_youtrack_attachments", issue_id="PROJ-1")
    for result in (issue, listing):
        assert "trace.log (id: 7-2)" in result.content
        assert f"Download URL: {_YT_URL}" in result.content
        assert "old.png" not in result.content
    assert "attachments(" in sent[0].url.params["fields"]


@pytest.mark.asyncio
async def test_youtrack_downloads_and_uploads_attachments():
    persist = RecordingPersist()
    executor = _executor(youtrack.build_tools, "https://acme.youtrack.cloud||perm-token", persist)
    ctx, file_id = file_context_with(_PDF, "report.pdf", "application/pdf")
    executor.file_context = ctx
    with fake_api(_youtrack) as sent:
        download = await call(executor, "download_youtrack_attachment", issue_id="PROJ-1", attachment_id="7-2")
        upload = await call(executor, "upload_youtrack_attachment", issue_id="PROJ-1", file_id=file_id)
    _, name, content_type = _stored(download, persist, _LOG)
    assert (name, content_type) == ("trace.log", "text/plain")
    fetch = next(r for r in sent if r.url.path == "/api/files/7-2")
    assert fetch.headers["authorization"] == "Bearer perm-token"
    assert not upload.is_error, upload.content
    assert "report.pdf (id: 7-9)" in upload.content
    post = next(r for r in sent if r.method == "POST")
    assert _PDF in post.content
    assert post.headers["content-type"].startswith("multipart/form-data")


# ---------------------------------------------------------------------------
# Concord agreement documents
# ---------------------------------------------------------------------------

_CONCORD = "https://api.concordnow.com/api/rest/1/organizations/12/agreements/ag-1"


def _concord(request: httpx.Request) -> httpx.Response:
    url = str(request.url).split("?")[0]
    if url == f"{_CONCORD}/versions":
        return httpx.Response(
            200,
            json=[{"id": 3, "version": 2, "displayVersion": "2.0", "type": "PDF", "date": 1456763982000, "user": {"name": "Ana"}}],
        )
    if url == f"{_CONCORD}/attachments" and request.method == "GET":
        return httpx.Response(
            200,
            json={"attachments": [{"id": "a1", "name": "Signed copy", "fileName": "signed.pdf", "description": "", "url": "https://secure.concordnow.com/a1"}]},
        )
    if url == f"{_CONCORD}/attachments" and request.method == "POST":
        return httpx.Response(201, json={"id": "a2"})
    if url == f"{_CONCORD}.pdf":
        return httpx.Response(
            200, content=_PDF, headers={"content-type": "application/pdf", "content-disposition": 'attachment; filename="MSA.pdf"'}
        )
    if url == f"{_CONCORD}/attachments/a1":
        return httpx.Response(200, content=_PDF, headers={"content-type": "application/force-download;charset=UTF-8"})
    if url == f"{_CONCORD}/versions/upload":
        return httpx.Response(200, json={"version": 3})
    return httpx.Response(404, json={})


@pytest.mark.asyncio
async def test_concord_lists_the_document_versions_and_attachments_with_links():
    executor = _executor(concord.build_tools, "12||concord-key")
    with fake_api(_concord):
        result = await call(executor, "list_concord_agreement_files", organization_id=12, agreement_uid="ag-1")
    assert f"PDF: {_CONCORD}.pdf" in result.content
    assert f"Word: {_CONCORD}.docx" in result.content
    assert "v2.0 (document id: 3) | PDF | 2016-02-29" in result.content
    assert "Signed copy (id: a1) | signed.pdf" in result.content
    assert f"Download URL: {_CONCORD}/attachments/a1" in result.content


@pytest.mark.asyncio
async def test_concord_downloads_the_agreement_and_an_attachment():
    persist = RecordingPersist()
    executor = _executor(concord.build_tools, "12||concord-key", persist)
    with fake_api(_concord) as sent:
        document = await call(executor, "download_concord_agreement", organization_id=12, agreement_uid="ag-1")
        attachment = await call(
            executor, "download_concord_attachment", organization_id=12, agreement_uid="ag-1", attachment_id="a1"
        )
    _, name, _ = _stored(document, persist, _PDF)
    assert name == "MSA.pdf"
    _, name, content_type = _stored(attachment, persist, _PDF)
    assert (name, content_type) == ("signed.pdf", "application/pdf")
    assert all(r.headers["x-api-key"] == "concord-key" for r in sent)


@pytest.mark.asyncio
async def test_concord_uploads_a_new_version_and_an_attachment_from_a_file_id():
    executor = _executor(concord.build_tools, "12||concord-key")
    ctx, pdf_id = file_context_with(_PDF, "MSA v3.pdf", "application/pdf")
    notes_ctx, notes_id = file_context_with(b"notes", "notes.txt", "text/plain")
    executor.file_context = ctx
    with fake_api(_concord) as sent:
        version = await call(executor, "upload_concord_agreement_version", organization_id=12, agreement_uid="ag-1", file_id=pdf_id)
        signed = await call(
            executor,
            "upload_concord_attachment",
            organization_id=12,
            agreement_uid="ag-1",
            file_id=pdf_id,
            is_signed_contract=True,
        )
    assert "(version 3)" in version.content
    assert "(attachment id: a2)" in signed.content
    version_post, attachment_post = sent
    assert version_post.url.params["uploadMode"] == "PDF"
    assert b'name="file"; filename="MSA v3.pdf"' in version_post.content
    assert b'name="isSignedContract"' in attachment_post.content and b"true" in attachment_post.content
    executor.file_context = notes_ctx
    with fake_api(_concord) as sent:
        refused = await call(executor, "upload_concord_agreement_version", organization_id=12, agreement_uid="ag-1", file_id=notes_id)
    assert refused.is_error and "not a PDF or Word document" in refused.content
    assert sent == []


# ---------------------------------------------------------------------------
# An upload answer names the file by its file_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("hostile", list(HOSTILE_FILE_NAMES))
async def test_hibob_upload_names_the_file_by_its_id(hostile):
    name = HOSTILE_FILE_NAMES[hostile]
    executor = _executor(hibob.build_tools, "svc||key")
    ctx, file_id = file_context_with(_PDF, name, "application/pdf")
    executor.file_context = ctx
    with fake_api(_hibob):
        result = await call(executor, "upload_hibob_document", employee_id="e1", file_id=file_id)
    assert not result.is_error, result.content
    outside = assert_name_only_inside(result.content, name)
    assert outside == f"Uploaded file {file_id} to the shared folder of employee e1. Doc ID: 501\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("hostile", list(HOSTILE_FILE_NAMES))
async def test_hubspot_attachment_names_the_file_by_its_id(unpaced_hubspot, hostile):
    name = HOSTILE_FILE_NAMES[hostile]

    def responder(request: httpx.Request) -> httpx.Response:
        if _path(request) == "/files/v3/files" and request.method == "POST":
            return httpx.Response(201, json={"id": "8001", "name": name, "access": "PRIVATE"})
        return _hubspot(request)

    executor = _executor(hubspot.build_tools, "pat-token")
    ctx, file_id = file_context_with(_PDF, name, "application/pdf")
    executor.file_context = ctx
    with fake_api(responder):
        result = await call(executor, "upload_hubspot_attachment", object_type="deals", record_id="9", file_id=file_id)
    assert not result.is_error, result.content
    outside = assert_name_only_inside(result.content, name)
    assert outside == f"Attached file {file_id} to deals 9 through note 401 (file id: 8001).\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("hostile", list(HOSTILE_FILE_NAMES))
async def test_ashby_upload_names_the_file_by_its_id(hostile):
    name = HOSTILE_FILE_NAMES[hostile]
    executor = _executor(ashby.build_tools, "ashby-key")
    ctx, file_id = file_context_with(_PDF, name, "application/pdf")
    executor.file_context = ctx
    with fake_api(_ashby):
        result = await call(executor, "upload_ashby_candidate_file", candidate_id="c1", file_id=file_id, as_resume=True)
    assert not result.is_error, result.content
    outside = assert_name_only_inside(result.content, name)
    assert outside == f"Uploaded file {file_id} as the resume of candidate c1.\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("hostile", list(HOSTILE_FILE_NAMES))
async def test_youtrack_upload_names_the_file_by_its_id(hostile):
    name = HOSTILE_FILE_NAMES[hostile]

    def responder(request: httpx.Request) -> httpx.Response:
        if _path(request) == "/api/issues/PROJ-1/attachments" and request.method == "POST":
            return httpx.Response(200, json=[{"id": "7-9", "name": name, "url": "/api/files/7-9?sign=x"}])
        return _youtrack(request)

    executor = _executor(youtrack.build_tools, "https://acme.youtrack.cloud||perm-token")
    ctx, file_id = file_context_with(_PDF, name, "application/pdf")
    executor.file_context = ctx
    with fake_api(responder):
        result = await call(executor, "upload_youtrack_attachment", issue_id="PROJ-1", file_id=file_id)
    assert not result.is_error, result.content
    outside = assert_name_only_inside(result.content, name)
    assert outside == f"Attached file {file_id} to PROJ-1.\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("hostile", list(HOSTILE_FILE_NAMES))
async def test_concord_uploads_name_the_file_by_its_id(hostile):
    name = HOSTILE_FILE_NAMES[hostile]
    executor = _executor(concord.build_tools, "12||concord-key")
    ctx, pdf_id = file_context_with(_PDF, name, "application/pdf")
    notes_ctx, notes_id = file_context_with(b"notes", name, "text/plain")
    executor.file_context = ctx
    with fake_api(_concord):
        version = await call(executor, "upload_concord_agreement_version", organization_id=12, agreement_uid="ag-1", file_id=pdf_id)
        attached = await call(executor, "upload_concord_attachment", organization_id=12, agreement_uid="ag-1", file_id=pdf_id)
    executor.file_context = notes_ctx
    with fake_api(_concord):
        refused = await call(executor, "upload_concord_agreement_version", organization_id=12, agreement_uid="ag-1", file_id=notes_id)
    assert not version.is_error, version.content
    assert assert_name_only_inside(version.content, name) == (
        f"Uploaded file {pdf_id} as a new version of agreement ag-1 (version 3).\n"
    )
    assert assert_name_only_inside(attached.content, name) == (
        f"Attached file {pdf_id} to agreement ag-1 (attachment id: a2).\n"
    )
    assert refused.is_error
    assert assert_name_only_inside(refused.content, name) == (
        f"Error: file {notes_id} is not a PDF or Word document - "
        "Concord creates a version only from a PDF or a Word file.\n"
    )
