"""Regression tests for the Amazon SES provider's suppression-list lookup.

Pins the fix where ``get_ses_suppressed_destination`` must opt into
``safe_request``'s ``expected_statuses`` contract via ``_signed_request`` (the
inner ``_api`` helper does not accept that kwarg — passing it raised a
``TypeError`` at runtime) and branch on ``resp.status_code == 404`` *before* the
error string (which ``safe_request`` still returns for an expected status).
"""

from __future__ import annotations

import pytest

from app.integrations.providers import ses
from app.integrations.providers.ses import build_tools
from app.services.integration_tools import _SafeIntegrationExecutor

# region||access_key_id||secret_access_key||configuration_set
_TOKEN = "us-east-1||AKIA_TEST||secret_test||"


class _FakeResp:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


def _handlers(token: str = _TOKEN) -> dict:
    executor = _SafeIntegrationExecutor()
    build_tools(token, executor)
    return executor._handlers


@pytest.mark.asyncio
async def test_get_suppressed_404_reports_not_suppressed(monkeypatch):
    captured: dict = {}

    async def _fake_signed_request(**kwargs):
        captured.update(kwargs)
        # safe_request returns BOTH the response and an error string for an
        # opted-in expected status — the tool must branch on the status first.
        return _FakeResp(404), "Amazon SES error: 404 Not Found"

    monkeypatch.setattr(ses, "_signed_request", _fake_signed_request)

    handler = _handlers()["get_ses_suppressed_destination"]
    result = await handler(email_address="nobody@example.com")

    assert result == "nobody@example.com is not on the suppression list."
    # The tool opted into the expected-status contract for 404.
    assert captured.get("expected_statuses") == {404}


@pytest.mark.asyncio
async def test_get_suppressed_200_reports_details(monkeypatch):
    async def _fake_signed_request(**kwargs):
        return (
            _FakeResp(
                200,
                {
                    "SuppressedDestination": {
                        "EmailAddress": "bounce@example.com",
                        "Reason": "BOUNCE",
                        "LastUpdateTime": "2024-01-02T03:04:05Z",
                    }
                },
            ),
            None,
        )

    monkeypatch.setattr(ses, "_signed_request", _fake_signed_request)

    handler = _handlers()["get_ses_suppressed_destination"]
    result = await handler(email_address="bounce@example.com")

    assert "bounce@example.com is suppressed" in result
    assert "BOUNCE" in result


@pytest.mark.asyncio
async def test_get_suppressed_real_error_is_returned(monkeypatch):
    async def _fake_signed_request(**kwargs):
        return _FakeResp(403), "Amazon SES error: 403 Forbidden"

    monkeypatch.setattr(ses, "_signed_request", _fake_signed_request)

    handler = _handlers()["get_ses_suppressed_destination"]
    result = await handler(email_address="x@example.com")

    # Non-404 failure falls through to the error string, not the benign message.
    assert result == "Amazon SES error: 403 Forbidden"


@pytest.mark.asyncio
async def test_get_suppressed_missing_credentials_short_circuits(monkeypatch):
    async def _must_not_call(**kwargs):
        raise AssertionError("_signed_request must not run without credentials")

    monkeypatch.setattr(ses, "_signed_request", _must_not_call)

    # Empty access_key_id + secret in the compound token.
    handler = _handlers("us-east-1||||")["get_ses_suppressed_destination"]
    result = await handler(email_address="x@example.com")

    assert "credentials are missing" in result.lower()


# ---------------------------------------------------------------------------
# _split_addresses robustness
# ---------------------------------------------------------------------------


def test_split_addresses_coerces_non_strings_and_normalises():
    # A list with a non-string item must not raise (LLM tool calls vary).
    assert ses._split_addresses(["a@ex.com", 123]) == ["a@ex.com", "123"]
    # Comma/semicolon-separated strings split and trim.
    assert ses._split_addresses(" a@ex.com ; b@ex.com ") == ["a@ex.com", "b@ex.com"]
    # Falsy / empty inputs yield an empty list.
    assert ses._split_addresses(None) == []
    assert ses._split_addresses("") == []
    assert ses._split_addresses(["", "  "]) == []


# ---------------------------------------------------------------------------
# Request-payload shape smoke tests (capture the signed body)
# ---------------------------------------------------------------------------


def _capture(monkeypatch, payload: dict | None = None) -> dict:
    """Patch _signed_request to capture the call kwargs and return a 200."""
    captured: dict = {}

    async def _fake_signed_request(**kwargs):
        captured.update(kwargs)
        return _FakeResp(200, payload or {}), None

    monkeypatch.setattr(ses, "_signed_request", _fake_signed_request)
    return captured


@pytest.mark.asyncio
async def test_send_email_builds_expected_payload(monkeypatch):
    captured = _capture(monkeypatch, {"MessageId": "msg-1"})
    handler = _handlers()["send_ses_email"]
    result = await handler(
        from_address="Sender <s@ex.com>",
        to="a@ex.com, b@ex.com",
        subject="Hello",
        text_body="Plain",
        html_body="<p>Rich</p>",
        cc="c@ex.com",
        reply_to="r@ex.com",
    )

    assert "msg-1" in result
    assert captured["method"] == "post"
    assert captured["path"].endswith("/outbound-emails")
    body = captured["body"]
    assert body["FromEmailAddress"] == "Sender <s@ex.com>"
    assert body["Destination"]["ToAddresses"] == ["a@ex.com", "b@ex.com"]
    assert body["Destination"]["CcAddresses"] == ["c@ex.com"]
    assert body["Content"]["Simple"]["Subject"]["Data"] == "Hello"
    assert body["Content"]["Simple"]["Body"]["Text"]["Data"] == "Plain"
    assert body["Content"]["Simple"]["Body"]["Html"]["Data"] == "<p>Rich</p>"
    assert body["ReplyToAddresses"] == ["r@ex.com"]


@pytest.mark.asyncio
async def test_send_email_requires_a_recipient(monkeypatch):
    # Validation happens before any request is signed.
    async def _must_not_call(**kwargs):
        raise AssertionError("must validate before signing")

    monkeypatch.setattr(ses, "_signed_request", _must_not_call)
    handler = _handlers()["send_ses_email"]
    result = await handler(from_address="s@ex.com", to="", subject="Hi", text_body="B")
    assert "recipient" in result.lower()


@pytest.mark.asyncio
async def test_templated_email_passes_json_string_template_data_through(monkeypatch):
    # _as_template_data passes a pre-serialised JSON string verbatim (the
    # dict | str runtime contract behind the object-typed schema).
    captured = _capture(monkeypatch, {"MessageId": "msg-2"})
    handler = _handlers()["send_ses_templated_email"]
    await handler(
        from_address="s@ex.com",
        to="a@ex.com",
        template_name="welcome",
        template_data='{"name":"Bob"}',
    )
    tmpl = captured["body"]["Content"]["Template"]
    assert tmpl["TemplateName"] == "welcome"
    assert tmpl["TemplateData"] == '{"name":"Bob"}'


@pytest.mark.asyncio
async def test_bulk_email_builds_entries_and_summarises(monkeypatch):
    captured = _capture(
        monkeypatch,
        {"BulkEmailEntryResults": [{"Status": "SUCCESS"}, {"Status": "SUCCESS"}]},
    )
    handler = _handlers()["send_ses_bulk_email"]
    result = await handler(
        from_address="s@ex.com",
        template_name="promo",
        recipients=[
            {"to": "a@ex.com"},
            {"to": "b@ex.com", "replacement_data": {"code": "X1"}},
        ],
    )

    assert "2/2" in result
    body = captured["body"]
    assert body["DefaultContent"]["Template"]["TemplateName"] == "promo"
    entries = body["BulkEmailEntries"]
    assert len(entries) == 2
    assert entries[0]["Destination"]["ToAddresses"] == ["a@ex.com"]
    # A dict replacement is coerced to a JSON string by _as_template_data.
    assert (
        entries[1]["ReplacementEmailContent"]["ReplacementTemplate"][
            "ReplacementTemplateData"
        ]
        == '{"code": "X1"}'
    )


@pytest.mark.asyncio
async def test_create_template_builds_expected_payload(monkeypatch):
    captured = _capture(monkeypatch, {})
    handler = _handlers()["create_ses_template"]
    result = await handler(
        template_name="welcome",
        subject="Hi {{name}}",
        html_body="<p>{{name}}</p>",
    )

    assert "created" in result.lower()
    assert captured["method"] == "post"
    assert captured["path"].endswith("/templates")
    body = captured["body"]
    assert body["TemplateName"] == "welcome"
    assert body["TemplateContent"]["Subject"] == "Hi {{name}}"
    assert body["TemplateContent"]["Html"] == "<p>{{name}}</p>"


@pytest.mark.asyncio
async def test_create_template_requires_name(monkeypatch):
    async def _must_not_call(**kwargs):
        raise AssertionError("must validate template_name before signing")

    monkeypatch.setattr(ses, "_signed_request", _must_not_call)
    handler = _handlers()["create_ses_template"]
    result = await handler(template_name="", subject="Hi", text_body="B")
    assert "template_name is required" in result


@pytest.mark.asyncio
async def test_bulk_email_denominator_is_attempted_entries(monkeypatch):
    # SES returns fewer results than attempted entries -> the summary stays
    # meaningful by reporting out of the number of entries we sent.
    _capture(
        monkeypatch,
        {"BulkEmailEntryResults": [{"Status": "SUCCESS"}]},
    )
    handler = _handlers()["send_ses_bulk_email"]
    result = await handler(
        from_address="s@ex.com",
        template_name="promo",
        recipients=[{"to": "a@ex.com"}, {"to": "b@ex.com"}, {"to": "c@ex.com"}],
    )
    assert "1/3 accepted" in result


@pytest.mark.asyncio
async def test_send_email_applies_default_configuration_set(monkeypatch):
    # The 4th compound-token field is the default configuration set; it is
    # applied when the call omits an explicit configuration_set override.
    captured = _capture(monkeypatch, {"MessageId": "msg-3"})
    handler = _handlers("us-east-1||AKIA_TEST||secret_test||default-cfg")[
        "send_ses_email"
    ]
    await handler(
        from_address="s@ex.com", to="a@ex.com", subject="Hi", text_body="B"
    )
    assert captured["body"]["ConfigurationSetName"] == "default-cfg"


@pytest.mark.asyncio
async def test_bulk_email_rejects_recipients_without_address(monkeypatch):
    # Every recipient lacks a usable address, so the tool returns an error
    # before signing anything (no empty BulkEmailEntries reach SES).
    async def _must_not_call(**kwargs):
        raise AssertionError("must reject empty recipients before signing")

    monkeypatch.setattr(ses, "_signed_request", _must_not_call)
    handler = _handlers()["send_ses_bulk_email"]
    result = await handler(
        from_address="s@ex.com",
        template_name="promo",
        recipients=[{"replacement_data": {"code": "X1"}}, {"to": ""}],
    )
    assert "no valid recipients" in result.lower()
