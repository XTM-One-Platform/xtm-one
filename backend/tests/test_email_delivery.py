"""What an email send tells its caller.

The headers every message carries, a subject that can never break a header,
and how a failure is classified: a refusal only a 5xx makes permanent, a link
broken while the message was on its way reported as uncertain, never retried
by a caller that must not email anyone twice.
"""

from __future__ import annotations

import smtplib

import pytest

from app.core.config import settings
from app.services import email as email_service
from app.services.email import (
    AUTOMATED_EMAIL_HEADERS,
    EmailDeliveryError,
    deliver_email,
    email_paragraph,
    email_paragraph_html,
)


def _message(**overrides):
    kwargs = {"to": "ann@acme.test", "subject": "Hello", "html": "<p>Hi</p>", "text": "Hi", "headers": None}
    kwargs.update(overrides)
    return email_service._build_message(**kwargs)


# --- The message --------------------------------------------------------------------


def test_every_message_is_marked_automated():
    msg = _message()
    for name, value in AUTOMATED_EMAIL_HEADERS.items():
        assert msg.get_all(name) == [value]


def test_a_callers_headers_are_added_on_top_of_the_defaults():
    msg = _message(headers={"X-Campaign": "catalog", "Auto-Submitted": "auto-replied"})
    assert msg["X-Campaign"] == "catalog"
    assert msg.get_all("Auto-Submitted") == ["auto-replied"]
    assert msg["X-Auto-Response-Suppress"] == "All"


def test_a_subject_is_folded_to_one_line():
    msg = _message(subject="Claude Sonnet 5.5 on Acme\r\nBcc: someone@evil.test\n  now")
    assert msg["Subject"] == "Claude Sonnet 5.5 on Acme Bcc: someone@evil.test now"
    assert msg["Bcc"] is None


# --- Classifying a failure ----------------------------------------------------------


@pytest.fixture
def smtp(monkeypatch):
    monkeypatch.setattr(settings, "smtp_hostname", "smtp.acme.test")
    monkeypatch.setattr(settings, "smtp_use_tls", False)
    monkeypatch.setattr(settings, "smtp_starttls", False)
    monkeypatch.setattr(settings, "smtp_username", "")


def _refused(*codes: int) -> smtplib.SMTPRecipientsRefused:
    return smtplib.SMTPRecipientsRefused({f"r{i}@acme.test": (code, b"no") for i, code in enumerate(codes)})


@pytest.mark.parametrize(
    ("codes", "permanent"),
    [((550,), True), ((550, 553), True), ((450,), False), ((550, 452), False), ((), False)],
)
def test_only_a_refusal_every_recipient_gave_with_a_5xx_is_permanent(codes, permanent):
    assert email_service._refusal_is_permanent(_refused(*codes)) is permanent


@pytest.mark.parametrize(
    ("reply", "names_the_address"),
    [
        (b"4.1.2 <emma@test.com>: Recipient address rejected: Domain not found", True),
        (b"5.1.1 <ann@acme.test>: Recipient address rejected: User unknown", True),
        ("450 4.1.1 No such user", True),
        (b"4.2.2 Mailbox full", True),
        (b"5.2.1 Mailbox disabled", True),
        (b"4.2.0 Mailbox status unknown", True),
        (b"5.1.10 Recipient address has null MX", True),
        # A policy or a sending limit, the sender's own address, no verdict on the address.
        (b"4.7.1 Greylisted", False),
        # The message or the server, not the mailbox: too large, a mailing list expansion problem.
        (b"5.2.3 Message too large", False),
        (b"4.2.4 Mailing list expansion problem", False),
        (b"4.1.8 <noreply@acme.test>: Sender address rejected: Domain not found", False),
        (b"4.1.0 Address status unknown", False),
        (b"Recipient address rejected: Domain not found", False),
        (b"no", False),
    ],
)
def test_a_refusal_names_the_address_only_by_its_enhanced_status_code(reply, names_the_address):
    assert email_service.reply_names_the_address(reply) is names_the_address


def _refused_with(*replies: bytes) -> smtplib.SMTPRecipientsRefused:
    return smtplib.SMTPRecipientsRefused({f"r{i}@acme.test": (450, reply) for i, reply in enumerate(replies)})


class _FakeSMTP:
    raises: BaseException | None = None

    def __init__(self, *args, **kwargs) -> None:
        pass

    def ehlo(self) -> None:
        pass

    def send_message(self, msg) -> None:
        if self.raises is not None:
            raise self.raises

    def quit(self) -> None:
        pass


@pytest.mark.parametrize(
    "error",
    [
        smtplib.SMTPServerDisconnected("Connection unexpectedly closed"),
        TimeoutError("timed out"),
        ConnectionResetError(),
    ],
)
def test_a_link_broken_while_the_message_was_sent_is_uncertain(smtp, monkeypatch, error):
    monkeypatch.setattr(_FakeSMTP, "raises", error)
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    with pytest.raises(email_service._UncertainDeliveryError):
        email_service._send_sync(_message())


@pytest.mark.parametrize("error", [_refused(550), smtplib.SMTPDataError(451, b"try again")])
def test_a_reply_the_server_gave_is_not_uncertain(smtp, monkeypatch, error):
    monkeypatch.setattr(_FakeSMTP, "raises", error)
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    with pytest.raises(type(error)):
        email_service._send_sync(_message())


async def _delivery_error(monkeypatch, raised: BaseException | None = None, **message) -> EmailDeliveryError:
    def fake_send(_msg) -> None:
        if raised is not None:
            raise raised

    monkeypatch.setattr(email_service, "_send_sync", fake_send)
    kwargs = {"to": "ann@acme.test", "subject": "Hello", "html": "<p>Hi</p>", **message}
    with pytest.raises(EmailDeliveryError) as info:
        await deliver_email(**kwargs)
    return info.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raised", "permanent", "uncertain", "recipient_refused"),
    [
        (_refused(550), True, False, True),
        # The address refused for now: another one may still go through.
        (_refused(450), False, False, True),
        (email_service._UncertainDeliveryError("timed out"), False, True, False),
        (ConnectionRefusedError("refused"), False, False, False),
        (smtplib.SMTPAuthenticationError(535, b"bad credentials"), False, False, False),
    ],
)
async def test_deliver_email_says_whether_a_failure_can_pass_later(
    smtp, monkeypatch, raised, permanent, uncertain, recipient_refused
):
    error = await _delivery_error(monkeypatch, raised)
    assert (error.permanent, error.uncertain, error.recipient_refused) == (permanent, uncertain, recipient_refused)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raised", "address_specific"),
    [
        (_refused_with(b"4.1.2 Domain not found"), True),
        (_refused_with(b"4.1.2 Domain not found", b"4.2.2 Mailbox full"), True),
        # One recipient refused for another reason: the refusal may be the server's.
        (_refused_with(b"4.1.2 Domain not found", b"4.7.1 Too many recipients"), False),
        (_refused(450), False),
        (smtplib.SMTPDataError(451, b"4.1.2 try again"), False),
    ],
)
async def test_deliver_email_says_whether_the_server_refused_the_address_itself(
    smtp, monkeypatch, raised, address_specific
):
    error = await _delivery_error(monkeypatch, raised)
    assert error.address_specific is address_specific


@pytest.mark.asyncio
async def test_an_address_that_cannot_be_a_header_is_permanent(smtp, monkeypatch):
    error = await _delivery_error(monkeypatch, to="ann@acme.test\r\nBcc: someone@evil.test")
    assert (error.permanent, error.uncertain) == (True, False)


@pytest.mark.asyncio
async def test_without_smtp_nothing_is_sent_and_it_may_pass_later(monkeypatch):
    monkeypatch.setattr(settings, "smtp_hostname", "")
    error = await _delivery_error(monkeypatch, AssertionError("never sent"))
    assert (error.permanent, error.uncertain) == (False, False)


# --- The layout ---------------------------------------------------------------------


def test_a_paragraph_of_text_is_escaped_and_one_of_markup_is_not():
    assert "&lt;b&gt;Acme &amp; Co&lt;/b&gt;" in email_paragraph("<b>Acme & Co</b>")
    assert "<b>" not in email_paragraph("<b>Acme & Co</b>")
    assert "<b>Acme &amp; Co</b>" in email_paragraph_html("<b>Acme &amp; Co</b>")


def test_a_file_travels_as_an_attachment_with_a_one_line_name():
    attachment = email_service.EmailAttachment("Ops\r\nweek.pdf", "application/pdf", b"%PDF-1.7")
    msg = _message(attachments=[attachment])
    files = list(msg.iter_attachments())
    assert [(part.get_content_type(), part.get_filename(), part.get_content()) for part in files] == [
        ("application/pdf", "Ops week.pdf", b"%PDF-1.7")
    ]
    # The text and the HTML stay the body.
    assert msg.get_body(preferencelist=("html",)).get_content().strip() == "<p>Hi</p>"


def test_files_over_the_bound_are_refused_before_sending():
    big = email_service.EmailAttachment("big.png", "image/png", b"x" * (email_service.MAX_ATTACHMENT_BYTES + 1))
    with pytest.raises(ValueError):
        _message(attachments=[big])
