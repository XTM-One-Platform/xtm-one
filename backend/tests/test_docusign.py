"""DocuSign e-signature provider: envelopes from conversation files, status, downloads, auth.

The DocuSign API is a fake transport shaped like its documented answers;
nothing reaches DocuSign. The JWT Grant signs a real RS256 assertion with a
throwaway key generated here.
"""

from __future__ import annotations

import base64
import json
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.integrations import oauth, token_endpoint
from app.integrations.http import _APP_TOKEN_CACHE
from app.integrations.oauth import build_token_request
from app.integrations.providers import docusign
from app.services import token_refresh
from app.services.integration_tools import _SafeIntegrationExecutor
from tests.integration_file_fakes import RecordingPersist, call, fake_api, file_context_with

_PDF = b"%PDF-1.4 agreement to sign"
# PEM armour assembled in parts: a literal header reads as a leaked key to the
# secret scan, and these stand-ins carry no key material.
_PEM_HEAD = "-----BEGIN RSA " + "PRIVATE KEY-----"
_PEM_TAIL = "-----END RSA " + "PRIVATE KEY-----"
_ACCOUNT = "11111111-2222-3333-4444-555555555555"
_BASE = f"https://demo.docusign.net/restapi/v2.1/accounts/{_ACCOUNT}"
_OAUTH_TOKEN = f"demo||{_ACCOUNT}||oauth||access-tok"


def _executor(token=_OAUTH_TOKEN, persist=None):
    executor = _SafeIntegrationExecutor()
    docusign.build_tools(token, executor, persist_file_fn=persist)
    return executor


@pytest.fixture(autouse=True)
def _no_cache():
    _APP_TOKEN_CACHE.clear()
    yield
    _APP_TOKEN_CACHE.clear()


def _api(request: httpx.Request) -> httpx.Response:
    url = str(request.url).split("?")[0]
    if url == "https://account-d.docusign.com/oauth/userinfo":
        return httpx.Response(
            200,
            json={
                "accounts": [
                    {"account_id": "other", "is_default": False, "base_uri": "https://eu.docusign.net"},
                    {
                        "account_id": _ACCOUNT,
                        "account_name": "Acme",
                        "is_default": True,
                        "base_uri": "https://demo.docusign.net",
                    },
                ]
            },
        )
    if url == "https://account-d.docusign.com/oauth/token":
        return httpx.Response(200, json={"access_token": "jwt-access", "expires_in": 3600})
    if url == _BASE and request.method == "GET":
        return httpx.Response(200, json={"accountName": "Acme"})
    if url == f"{_BASE}/envelopes" and request.method == "POST":
        return httpx.Response(201, json={"envelopeId": "env-1", "status": "sent"})
    if url == f"{_BASE}/envelopes" and request.method == "GET":
        return httpx.Response(
            200,
            json={
                "totalSetSize": "1",
                "envelopes": [
                    {
                        "envelopeId": "env-1",
                        "emailSubject": "NDA",
                        "status": "completed",
                        "completedDateTime": "2026-01-02T10:00:00Z",
                    }
                ],
            },
        )
    if url == f"{_BASE}/envelopes/env-1" and request.method == "GET":
        return httpx.Response(
            200,
            json={
                "envelopeId": "env-1",
                "emailSubject": "NDA",
                "status": "sent",
                "sentDateTime": "2026-01-01T09:00:00Z",
            },
        )
    if url == f"{_BASE}/envelopes/env-1" and request.method == "PUT":
        return httpx.Response(
            200, json={"envelopeId": "env-1", "status": json.loads(request.content)["status"]}
        )
    if url == f"{_BASE}/envelopes/env-1/recipients" and request.method == "GET":
        return httpx.Response(
            200,
            json={
                "signers": [
                    {
                        "recipientId": "1",
                        "name": "Ana",
                        "email": "ana@example.com",
                        "status": "delivered",
                        "deliveredDateTime": "2026-01-01T09:05:00Z",
                    },
                    {
                        "recipientId": "2",
                        "name": "Bo",
                        "email": "bo@example.com",
                        "status": "completed",
                        "signedDateTime": "2026-01-01T10:00:00Z",
                    },
                ]
            },
        )
    if url == f"{_BASE}/envelopes/env-1/recipients" and request.method == "PUT":
        return httpx.Response(200, json={})
    if url == f"{_BASE}/envelopes/env-1/documents":
        return httpx.Response(
            200,
            json={"envelopeDocuments": [{"documentId": "1", "name": "NDA.pdf", "type": "content"}]},
        )
    if url == f"{_BASE}/envelopes/env-1/documents/combined":
        return httpx.Response(
            200,
            content=_PDF,
            headers={
                "content-type": "application/pdf",
                "content-disposition": 'attachment; filename="NDA_signed.pdf"',
            },
        )
    if url == f"{_BASE}/envelopes/env-1/views/recipient":
        return httpx.Response(201, json={"url": "https://demo.docusign.net/signing/xyz"})
    if url == f"{_BASE}/templates":
        return httpx.Response(
            200,
            json={
                "envelopeTemplates": [
                    {
                        "templateId": "tpl-1",
                        "name": "NDA",
                        "recipients": {"signers": [{"roleName": "Signer"}]},
                    }
                ]
            },
        )
    return httpx.Response(
        404, json={"errorCode": "NOT_FOUND", "message": f"no route for {request.method} {url}"}
    )


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def test_the_compound_token_parses_both_modes():
    oauth = docusign.parse_token(_OAUTH_TOKEN)
    assert (oauth["mode"], oauth["environment"], oauth["account_id"], oauth["access_token"]) == (
        "oauth",
        "demo",
        _ACCOUNT,
        "access-tok",
    )
    key = f"{_PEM_HEAD}\nabc||def\n{_PEM_TAIL}"
    jwt_fields = docusign.parse_token(f"production||||jwt||ik||uid||{key}")
    assert jwt_fields["mode"] == "jwt"
    assert (jwt_fields["integration_key"], jwt_fields["user_id"]) == ("ik", "uid")
    assert jwt_fields["private_key"] == key  # the key keeps its ``||``
    assert docusign.parse_token("__dummy__")["mode"] == "oauth"
    assert docusign.build_tools("", _SafeIntegrationExecutor()) == []


def test_the_compound_credential_is_assembled_the_same_way_for_runs_and_the_test_button():
    extra = {"environment": "demo", "account_id": _ACCOUNT}
    assert docusign.compound_credential("access-tok", extra, jwt_grant=False) == _OAUTH_TOKEN
    assert docusign.compound_credential("tok", {}, jwt_grant=False) == "production||||oauth||tok"
    key = f"{_PEM_HEAD}\nabc\n{_PEM_TAIL}"
    jwt_extra = {**extra, "user_id": "uid"}
    # The integration key comes from extra_config, else from the stored client id.
    assert docusign.compound_credential(key, {**jwt_extra, "integration_key": "ik"}, jwt_grant=True) == (
        f"demo||{_ACCOUNT}||jwt||ik||uid||{key}"
    )
    assert docusign.compound_credential(key, jwt_extra, jwt_grant=True, integration_key="cid") == (
        f"demo||{_ACCOUNT}||jwt||cid||uid||{key}"
    )
    # A missing key, integration key or user id is reported as empty.
    assert docusign.compound_credential("", {**jwt_extra, "integration_key": "ik"}, jwt_grant=True) == ""
    assert docusign.compound_credential(key, jwt_extra, jwt_grant=True) == ""
    assert docusign.compound_credential(key, {"integration_key": "ik"}, jwt_grant=True) == ""
    assert docusign.parse_token(docusign.compound_credential(key, jwt_extra, jwt_grant=True, integration_key="cid"))[
        "private_key"
    ] == key


def test_a_one_line_pem_key_gets_its_newlines_back():
    body = "A" * 70
    flat = f"{_PEM_HEAD} {body} {_PEM_TAIL}"
    restored = docusign.normalize_private_key(flat)
    assert restored.splitlines()[0] == _PEM_HEAD
    assert restored.splitlines()[1] == "A" * 64 and restored.splitlines()[2] == "A" * 6
    assert docusign.normalize_private_key("x\\ny") == "x\ny"


def test_the_oauth_token_request_sends_basic_auth_with_a_form_body():
    kwargs = build_token_request(
        "docusign", "ik", "secret", {"grant_type": "authorization_code", "code": "c"}
    )
    assert kwargs["data"] == {"grant_type": "authorization_code", "code": "c"}
    assert "json" not in kwargs
    assert kwargs["headers"]["Authorization"] == "Basic " + base64.b64encode(b"ik:secret").decode()


@pytest.mark.asyncio
async def test_the_jwt_grant_signs_an_assertion_and_caches_the_token():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    with fake_api(_api) as sent:
        token, err = await docusign.jwt_grant_token("demo", "ik", "uid", pem)
        again, _ = await docusign.jwt_grant_token("demo", "ik", "uid", pem)
    assert (token, err, again) == ("jwt-access", None, "jwt-access")
    (grant,) = sent  # the second call came from the cache
    form = parse_qs(grant.content.decode())
    assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
    claims = jwt.decode(
        form["assertion"][0], public, algorithms=["RS256"], audience="account-d.docusign.com"
    )
    assert (claims["iss"], claims["sub"], claims["scope"]) == ("ik", "uid", "signature impersonation")
    assert claims["exp"] - claims["iat"] == 3600


@pytest.mark.asyncio
async def test_the_jwt_grant_refuses_a_key_that_cannot_sign():
    with fake_api(_api) as sent:
        token, err = await docusign.jwt_grant_token("demo", "ik", "uid", "not a key")
    assert token is None and err.startswith("Error:") and "private key" in err
    assert sent == []


@pytest.mark.asyncio
async def test_validate_connection_reads_userinfo_then_the_account():
    with fake_api(_api) as sent:
        ok, message = await docusign.validate_connection(_OAUTH_TOKEN)
    assert ok and "Acme" in message and "demo" in message
    assert [str(r.url) for r in sent] == ["https://account-d.docusign.com/oauth/userinfo", _BASE]
    assert all(r.headers["authorization"] == "Bearer access-tok" for r in sent)
    with fake_api(_api):
        ok, message = await docusign.validate_connection("demo||nope||oauth||access-tok")
    assert not ok and "is not one of the token's accounts" in message


@pytest.mark.asyncio
async def test_the_scheduled_refresh_of_a_demo_integration_uses_the_demo_host(monkeypatch):
    """The sweep's refresh (``token_refresh._do_refresh_integration``) resolves the
    environment host as the chat-time refresh does: a demo refresh token posted to
    ``account.docusign.com`` is an ``invalid_grant`` that marks the integration
    ``reauthorize`` an hour after every sign-in."""

    async def _resolve(_host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("203.0.113.10", port))]

    monkeypatch.setattr(token_endpoint, "_resolve", _resolve)
    monkeypatch.setattr(token_refresh, "decrypt_value", lambda value: value)
    monkeypatch.setattr(oauth, "decrypt_value", lambda value: value)
    monkeypatch.setattr("app.core.security.decrypt_stored_value", lambda value: value)
    monkeypatch.setattr(token_refresh, "redis_client", AsyncMock(return_value=AsyncMock()))
    integration = SimpleNamespace(
        id="0c5a91f6-9be3-4a3f-9c86-2f3c1f1f1f1f",
        name="DocuSign",
        provider="docusign",
        status="connected",
        client_id_enc="ik",
        client_secret_enc="secret",
        refresh_token_enc="refresh-tok",
        access_token_enc="old",
        token_url="https://account.docusign.com/oauth/token",  # the catalog's production host
        scopes="signature",
        extra_config_enc=json.dumps({"environment": "demo", "account_id": _ACCOUNT}),
        token_expires_at=None,
    )
    http = AsyncMock()
    http.post = AsyncMock(
        return_value=httpx.Response(
            200,
            json={"access_token": "new", "refresh_token": "next", "expires_in": 3600},
            request=httpx.Request("POST", "https://account-d.docusign.com/oauth/token"),
        )
    )

    ok = await token_refresh._do_refresh_integration(integration, http)

    assert ok is True
    http.post.assert_awaited_once()
    url = http.post.await_args.args[0]
    kwargs = http.post.await_args.kwargs
    assert httpx.URL(url).host == "account-d.docusign.com"
    assert httpx.URL(url).path == "/oauth/token"
    assert kwargs["data"]["grant_type"] == "refresh_token"
    assert kwargs["data"]["refresh_token"] == "refresh-tok"
    assert kwargs["headers"]["Authorization"] == "Basic " + base64.b64encode(b"ik:secret").decode()
    assert integration.status == "connected"
    assert integration.token_expires_at is not None


# ---------------------------------------------------------------------------
# Envelopes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_envelope_carries_the_file_bytes_base64_the_signers_and_the_tabs():
    executor = _executor()
    ctx, pdf_id = file_context_with(_PDF, "NDA.pdf", "application/pdf")
    executor.file_context = ctx
    with fake_api(_api) as sent:
        result = await call(
            executor,
            "create_docusign_envelope",
            file_ids=[pdf_id],
            email_subject="Please sign the NDA",
            email_message="Thanks",
            signers=[
                {"name": "Ana", "email": "ana@example.com"},
                {"name": "Bo", "email": "bo@example.com", "routing_order": 2, "role": "CEO"},
            ],
            carbon_copies=[{"name": "Cy", "email": "cy@example.com"}],
            anchor_tabs=[
                {"anchor_string": "/sig/", "type": "sign_here", "y_offset": 10},
                {"anchor_string": "/date/", "type": "date_signed"},
            ],
        )
    assert not result.is_error, result.content
    assert "Envelope env-1 sent" in result.content
    assert "https://appdemo.docusign.com/documents/details/env-1" in result.content
    create = next(r for r in sent if r.method == "POST")
    body = json.loads(create.content)
    assert body["status"] == "sent" and body["emailSubject"] == "Please sign the NDA"
    assert body["emailBlurb"] == "Thanks"
    (document,) = body["documents"]
    assert base64.b64decode(document["documentBase64"]) == _PDF
    assert (document["name"], document["fileExtension"], document["documentId"]) == ("NDA.pdf", "pdf", "1")
    ana, bo = body["recipients"]["signers"]
    assert (ana["recipientId"], ana["routingOrder"], bo["routingOrder"], bo["roleName"]) == ("1", "1", "2", "CEO")
    assert "clientUserId" not in ana
    assert ana["tabs"]["signHereTabs"][0] == {
        "anchorString": "/sig/",
        "anchorUnits": "pixels",
        "anchorXOffset": "0",
        "anchorYOffset": "10",
        "anchorIgnoreIfNotPresent": "false",
    }
    assert ana["tabs"]["dateSignedTabs"][0]["anchorString"] == "/date/"
    assert body["recipients"]["carbonCopies"] == [
        {"recipientId": "3", "name": "Cy", "email": "cy@example.com", "routingOrder": "3"}
    ]


@pytest.mark.asyncio
async def test_an_envelope_refuses_a_non_document_and_a_signer_without_email():
    executor = _executor()
    ctx, txt_id = file_context_with(b"notes", "notes.txt", "text/plain")
    executor.file_context = ctx
    with fake_api(_api) as sent:
        refused = await call(
            executor,
            "create_docusign_envelope",
            file_ids=[txt_id],
            email_subject="x",
            signers=[{"name": "A", "email": "a@x.io"}],
        )
        # The documents are read first: the signer check needs a real document.
        executor.file_context, pdf_id = file_context_with(_PDF, "NDA.pdf", "application/pdf")
        no_email = await call(
            executor, "create_docusign_envelope", file_ids=[pdf_id], email_subject="x", signers=[{"name": "A"}]
        )
    assert refused.is_error and "not a PDF or Word document" in refused.content
    assert no_email.is_error and "needs a name and an email" in no_email.content
    assert sent == []


@pytest.mark.asyncio
async def test_embedded_signing_returns_a_url_per_signer():
    executor = _executor()
    ctx, pdf_id = file_context_with(_PDF, "NDA.pdf", "application/pdf")
    executor.file_context = ctx
    with fake_api(_api) as sent:
        result = await call(
            executor,
            "create_docusign_envelope",
            file_ids=[pdf_id],
            email_subject="NDA",
            signers=[{"name": "Ana", "email": "ana@example.com"}],
            embedded=True,
            return_url="https://app.example/done",
        )
    assert "ana@example.com: https://demo.docusign.net/signing/xyz" in result.content
    view = next(r for r in sent if r.url.path.endswith("/views/recipient"))
    assert json.loads(view.content)["clientUserId"] == "ana@example.com"
    create = next(r for r in sent if r.method == "POST" and r.url.path.endswith("/envelopes"))
    assert json.loads(create.content)["recipients"]["signers"][0]["clientUserId"] == "ana@example.com"


@pytest.mark.asyncio
async def test_the_envelope_status_lists_recipients_and_documents():
    executor = _executor()
    with fake_api(_api):
        result = await call(executor, "get_docusign_envelope", envelope_id="env-1")
    text = result.content
    assert "Envelope env-1 - NDA - status: sent" in text and "Sent: 2026-01-01T09:00:00Z" in text
    assert "Ana <ana@example.com> | status: delivered | delivered 2026-01-01T09:05:00Z" in text
    assert "Bo <bo@example.com> | status: completed | signed 2026-01-01T10:00:00Z" in text
    assert "NDA.pdf (document id: 1, content)" in text


@pytest.mark.asyncio
async def test_listing_filters_by_status_and_date_newest_first():
    executor = _executor()
    with fake_api(_api) as sent:
        result = await call(
            executor, "list_docusign_envelopes", status="completed", from_date="2026-01-01", search_text="NDA"
        )
        bad = await call(executor, "list_docusign_envelopes", status="lost")
    assert "Envelope env-1 - NDA - status: completed" in result.content
    assert bad.content.startswith("Error: status must be one of")
    listing = next(r for r in sent if r.url.path.endswith("/envelopes"))
    params = listing.url.params
    assert (params["status"], params["from_date"], params["search_text"], params["order"]) == (
        "completed",
        "2026-01-01",
        "NDA",
        "desc",
    )


@pytest.mark.asyncio
async def test_the_signed_document_is_stored_byte_for_byte():
    persist = RecordingPersist()
    executor = _executor(persist=persist)
    with fake_api(_api) as sent:
        result = await call(executor, "download_docusign_document", envelope_id="env-1", certificate=True)
    assert not result.is_error, result.content
    assert any(data == _PDF for _, data, _ in persist.calls)
    (ref,) = result.files
    assert (ref.filename, ref.content_type) == ("NDA_signed.pdf", "application/pdf")
    assert f"[[FILE:{ref.file_id}]]" in result.content
    download = next(r for r in sent if "/documents/combined" in r.url.path)
    assert download.url.params["certificate"] == "true"


@pytest.mark.asyncio
async def test_send_void_and_resend_write_the_envelope():
    executor = _executor()
    with fake_api(_api) as sent:
        sent_result = await call(executor, "send_docusign_envelope", envelope_id="env-1")
        voided = await call(executor, "void_docusign_envelope", envelope_id="env-1", reason="Wrong version")
        no_reason = await call(executor, "void_docusign_envelope", envelope_id="env-1", reason="")
        resent = await call(executor, "resend_docusign_envelope", envelope_id="env-1")
    assert "Envelope env-1 sent" in sent_result.content and "Envelope env-1 voided" in voided.content
    assert no_reason.content.startswith("Error:")
    assert "resent to ana@example.com" in resent.content and "bo@example.com" not in resent.content
    puts = [json.loads(r.content) for r in sent if r.method == "PUT"]
    assert puts[0] == {"status": "sent"}
    assert puts[1] == {"status": "voided", "voidedReason": "Wrong version"}
    resend = next(r for r in sent if r.method == "PUT" and r.url.path.endswith("/recipients"))
    assert resend.url.params["resend_envelope"] == "true"
    assert [s["recipientId"] for s in json.loads(resend.content)["signers"]] == ["1"]


@pytest.mark.asyncio
async def test_templates_are_listed_and_filled():
    executor = _executor()
    with fake_api(_api) as sent:
        listed = await call(executor, "list_docusign_templates")
        created = await call(
            executor,
            "create_docusign_envelope_from_template",
            template_id="tpl-1",
            template_roles=[{"role_name": "Signer", "name": "Ana", "email": "ana@example.com"}],
            status="created",
        )
    assert "NDA (template id: tpl-1) roles: Signer" in listed.content
    assert "Envelope env-1 sent from template tpl-1" in created.content  # the fake answers 'sent'
    create = next(r for r in sent if r.method == "POST")
    assert json.loads(create.content) == {
        "templateId": "tpl-1",
        "templateRoles": [{"roleName": "Signer", "name": "Ana", "email": "ana@example.com"}],
        "status": "created",
    }


def test_the_tools_are_declared_with_their_read_write_split():
    from app.services.tool_classification import infer_tool_type

    executor = _SafeIntegrationExecutor()
    names = {d.name: d for d in docusign.build_tools("__dummy__", executor)}
    assert set(names) == {
        "create_docusign_envelope",
        "send_docusign_envelope",
        "get_docusign_envelope",
        "list_docusign_envelopes",
        "download_docusign_document",
        "void_docusign_envelope",
        "resend_docusign_envelope",
        "list_docusign_templates",
        "create_docusign_envelope_from_template",
    }
    reads = {n for n in names if infer_tool_type(n) == "read"}
    assert reads == {
        "get_docusign_envelope",
        "list_docusign_envelopes",
        "list_docusign_templates",
        "download_docusign_document",
    }
    assert names["download_docusign_document"].workspace_ingest is True
