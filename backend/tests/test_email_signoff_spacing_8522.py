"""The signature follows the sign-off line directly (#8522).

Every paragraph of an email keeps its 12pt bottom margin (#7512), except
the block right above the signature: a signature service's block brings its
own top spacing, and the two stacked into a blank line under "Kind regards,".
"""

from __future__ import annotations

import re

import pytest

from app.integrations.providers.microsoft import mail
from app.services.email_html import (
    EMAIL_BLOCK_SPACING_STYLE,
    close_before_signature,
    markdown_to_email_html,
)

SIG = '<div id="Signature"><table><tr><td height="15"></td></tr><tr><td>Sam</td></tr></table></div>'
SPACED = f'style="{EMAIL_BLOCK_SPACING_STYLE}"'


def _style_of(html: str, text: str) -> str:
    """The ``style`` attribute of the paragraph holding *text* (its compose style span, if any, skipped)."""
    match = re.search(r"<p\b([^>]*)>(?:<span\b[^>]*>)?" + re.escape(text), html)
    assert match is not None, text
    style = re.search(r'\bstyle="[^"]*"', match.group(1))
    return style.group(0) if style else ""


def test_the_sign_off_of_a_markdown_body_has_no_bottom_margin_and_the_others_keep_theirs():
    body = markdown_to_email_html("Hello Bob,\n\nThe report is attached.\n\nKind regards,")
    signed = mail.sign_above_quote(body, SIG)
    assert "margin-bottom:0" in _style_of(signed, "Kind regards,")
    assert "margin-bottom:12pt" not in _style_of(signed, "Kind regards,")
    assert SPACED in _style_of(signed, "Hello Bob,")
    assert SPACED in _style_of(signed, "The report is attached.")
    # The signature right after the sign-off: no blank paragraph between them.
    assert re.search(r"Kind regards,(?:</span>)?</p>\s*<div id=\"Signature\">", signed)


def test_an_author_margin_shorthand_on_the_sign_off_is_overridden_at_the_bottom_only():
    body = '<p style="margin:0 0 12px">Hi,</p><p style="margin:0 0 12px">Bien à toi,</p>'
    signed = mail.sign_above_quote(markdown_to_email_html(body, assume_html=True), SIG)
    assert _style_of(signed, "Bien à toi,").strip() == 'style="margin:0 0 12px;margin-bottom:0"'
    assert _style_of(signed, "Hi,").strip() == 'style="margin:0 0 12px"'


def test_a_list_that_ends_the_body_and_its_last_item_paragraph_lose_the_bottom_margin():
    body = markdown_to_email_html("Points:\n\n- one\n- two")
    closed = close_before_signature(body)
    assert re.search(r'<ul style="[^"]*margin-bottom:0"', closed)
    assert SPACED in _style_of(closed, "Points:")


def test_the_signature_goes_above_a_quote_and_the_quote_is_untouched():
    quote = f"<blockquote {SPACED}><p {SPACED}>Old</p></blockquote>"
    html = f"<html><body><p {SPACED}>Hello</p><p {SPACED}>Kind regards,</p>{quote}</body></html>"
    signed = mail.sign_above_quote(html, SIG)
    assert "margin-bottom:0" in _style_of(signed, "Kind regards,")
    assert signed.endswith(f"{quote}</body></html>")
    assert signed.index("Kind regards,") < signed.index('id="Signature"') < signed.index("Old")


def test_a_reply_draft_signed_at_send_closes_its_own_part_only():
    own = f"<html><head><style>p {{margin:0}}</style></head><body><div><p {SPACED}>Thanks,</p></div>"
    thread = f'<hr><div id="divRplyFwdMsg"><b>From:</b> Alice</div><p {SPACED}>Earlier</p></body></html>'
    signed = mail._sign_draft_body(own + thread, SIG)
    assert "margin-bottom:0" in _style_of(signed, "Thanks,")
    assert SPACED in _style_of(signed, "Earlier")
    assert signed.index("Thanks,") < signed.index('id="Signature"') < signed.index("<hr>")


@pytest.mark.parametrize("html", ["", "plain text", "<div>no block</div>"])
def test_a_body_without_a_spaced_block_is_returned_as_is(html):
    assert close_before_signature(html) == html


def test_only_the_opening_tags_change_and_crlf_bodies_are_located_right():
    html = f"<div>\r\n<p {SPACED}>One</p>\r\n<p {SPACED}>Two</p>\r\n</div>\r\n"
    closed = close_before_signature(html)
    assert closed == html.replace(f"<p {SPACED}>Two", '<p style="margin-top:0;margin-bottom:0">Two')


def test_a_block_followed_by_text_keeps_its_margin():
    html = f"<p {SPACED}>One</p>Sam"
    assert close_before_signature(html) == html
