"""The recipients ``get_microsoft_email`` prints: To, CC and BCC always, Reply-To when set.

A sent item or a draft carries its Bcc; a read that does not select it
looked the same as a message without one, so every line is written and an
empty one reads ``(none)``.
"""

from app.integrations.providers.microsoft.mail import format_recipient_lines


def _people(*addresses: str) -> list[dict]:
    return [{"emailAddress": {"name": "", "address": a}} for a in addresses]


def test_a_sent_item_with_a_bcc_names_it():
    lines = format_recipient_lines(
        {
            "toRecipients": _people("a@example.org"),
            "ccRecipients": _people("b@example.org", "c@example.org"),
            "bccRecipients": _people("hidden@example.org"),
        }
    )
    assert lines.splitlines() == [
        "To: a@example.org",
        "CC: b@example.org, c@example.org",
        "BCC: hidden@example.org",
    ]


def test_a_message_without_bcc_says_none():
    lines = format_recipient_lines({"toRecipients": _people("a@example.org")})
    assert "CC: (none)" in lines.splitlines()
    assert "BCC: (none)" in lines.splitlines()
    assert "Reply-To" not in lines


def test_reply_to_is_named_when_set():
    lines = format_recipient_lines({"toRecipients": _people("a@example.org"), "replyTo": _people("r@example.org")})
    assert lines.splitlines()[-1] == "Reply-To: r@example.org"


def test_the_read_selects_bcc_and_reply_to():
    import inspect

    from app.integrations.providers.microsoft import mail

    source = inspect.getsource(mail.register)
    assert "toRecipients,ccRecipients,bccRecipients,replyTo," in source
