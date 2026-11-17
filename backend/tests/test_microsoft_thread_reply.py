"""Recipients of a reply to a forwarded thread.

The forwarded copy names its forwarder as sender; the reply goes to the
participants of the ORIGINAL message, read from the header block the forward
quotes, the sending mailbox left out.
"""

from __future__ import annotations

import pytest

from app.integrations.providers.microsoft._thread_reply import (
    THREAD_IN_REPLY_TO_HEADER,
    THREAD_REFERENCES_HEADER,
    drop_forward_header,
    forward_thread_headers,
    forwarded_header,
    graph_recipients,
    held_thread,
    is_forward,
    parse_addresses,
    quote_boundaries,
    reply_subject,
    thread_recipients,
    with_extra_headers,
    with_recipient_headers,
    with_thread_headers,
)

OUTLOOK_FORWARD = (
    '<html><body><div>Can you answer them for me?</div>'
    '<div id="divRplyFwdMsg"><hr style="display:inline-block">'
    '<font face="Calibri"><b>From:</b> Alice Martin &lt;alice@example.org&gt;<br>'
    "<b>Sent:</b> Sunday, November 1, 2026 2:30 PM<br>"
    "<b>To:</b> Sam &lt;sam@example.com&gt;; Bob &lt;bob@example.org&gt;<br>"
    "<b>Cc:</b> Carol &lt;carol@example.net&gt;; assistant@example.com<br>"
    "<b>Subject:</b> Introduction</font></div><div>Hello Sam</div></body></html>"
)
GMAIL_FORWARD = (
    "<div>FYI</div><div>---------- Forwarded message ---------<br>"
    "De : Alice &lt;alice@example.org&gt;<br>Date: dim. 1 nov. 2026<br>"
    "Objet : Intro<br>À : sam@example.com<br></div><div>Bonjour</div>"
)


def test_the_outlook_forward_header_names_the_original_participants():
    header = forwarded_header(OUTLOOK_FORWARD)
    assert header is not None
    assert header.sender == ["alice@example.org"]
    assert header.to == ["sam@example.com", "bob@example.org"]
    assert header.cc == ["carol@example.net", "assistant@example.com"]
    assert header.subject == "Introduction"


def test_a_reply_goes_to_the_original_sender_its_participants_in_cc_without_the_sending_mailbox():
    header = forwarded_header(OUTLOOK_FORWARD)
    assert header is not None
    to, cc = thread_recipients(header, own_address="Assistant@Example.com")
    assert to == ["alice@example.org"]
    assert cc == ["sam@example.com", "bob@example.org", "carol@example.net"]


def test_a_french_gmail_forward_is_read_too():
    header = forwarded_header(GMAIL_FORWARD)
    assert header is not None
    assert header.sender == ["alice@example.org"]
    assert header.subject == "Intro"


def test_the_header_of_a_forward_is_skipped_for_the_original_one():
    html = (
        "<div>Please answer</div><hr><div><b>From:</b> Sam &lt;sam@example.com&gt;<br><b>Sent:</b> Sunday<br>"
        "<b>To:</b> assistant@example.com<br><b>Subject:</b> FW: Introduction</div><div>Please answer</div>"
        + OUTLOOK_FORWARD
    )
    header = forwarded_header(html)
    assert header is not None and header.sender == ["alice@example.org"]


def test_a_message_without_a_quoted_header_has_none():
    assert forwarded_header("<p>From: the start of the week we ship.</p>") is None
    assert forwarded_header("<p>Hello</p>") is None


@pytest.mark.parametrize(
    ("subject", "forward"),
    [("FW: Intro", True), ("Fwd: Intro", True), ("TR : Intro", True), ("RE: Intro", False), ("Intro", False)],
)
def test_is_forward(subject, forward):
    assert is_forward(subject) is forward


@pytest.mark.parametrize(
    ("subject", "expected"),
    [("FW: Intro", "RE: Intro"), ("RE: FW: Intro", "RE: Intro"), ("TR: RE: Intro", "RE: Intro"), ("Intro", "RE: Intro")],
)
def test_reply_subject(subject, expected):
    assert reply_subject(subject) == expected


def test_a_subject_ending_with_a_non_breaking_space_gets_no_entity():
    # Gmail threads can end their subject with U+00A0.
    assert reply_subject("FW: Re: Intro to Filigran\u00a0") == "RE: Intro to Filigran"
    assert reply_subject("\u00a0Intro & co") == "RE: Intro & co"


def test_the_quoted_header_is_read_as_text_without_entities():
    html = (
        "<div><b>From:</b> Alice &lt;alice@example.org&gt;<br><b>Sent:</b> Sunday<br>"
        "<b>To:</b> sam@example.com<br><b>Subject:</b> Re: Intro to Cognisys- Filigran&nbsp;&amp; co&nbsp;</div>"
    )
    header = forwarded_header(html)
    assert header is not None
    assert header.sender == ["alice@example.org"]
    assert header.subject == "Re: Intro to Cognisys- Filigran & co"
    assert "&" not in reply_subject(header.subject).replace(" & ", "")


def test_the_forward_header_and_comment_can_be_dropped_from_the_quote():
    html = (
        '<body><div>Mine</div><hr><div id="divRplyFwdMsg">From: Sam<br>Sent: Sunday</div><div>FYI</div>'
        '<hr><div id="divRplyFwdMsg">From: Alice<br>Subject: Intro</div><div>Original</div></body>'
    )
    dropped = drop_forward_header(html)
    assert "From: Sam" not in dropped and "FYI" not in dropped
    assert dropped.index("Mine") < dropped.index("<hr>") < dropped.index("From: Alice") < dropped.index("Original")
    single = '<body><hr><div id="divRplyFwdMsg">From: Alice<br>Sent: Sunday</div></body>'
    assert drop_forward_header(single) == single


# A createReply draft (Prefer html) on a copy forwarded by forward_microsoft_email from a
# plain-text original, as Exchange answers it: the forward's own header is the
# reply's quote header; the original's is the underscore line + header lines Exchange
# wrote when it turned the text original into HTML. No divRplyFwdMsg on the original.
FORWARDED_TEXT_REPLY = (
    '<html><head><meta http-equiv="Content-Type" content="text/html; charset=utf-8"></head>'
    '<body dir="ltr"><div id="appendonsend"></div>'
    '<hr style="display:inline-block;width:98%" tabindex="-1">'
    '<div dir="ltr"><font face="Calibri, sans-serif" color="#000000" style="font-size:11pt">'
    "<b>From:</b> Sam Example &lt;sam@example.com&gt;<br><b>Sent:</b> Sunday, November 1, 2026 11:32:14 PM<br>"
    "<b>To:</b> Executive Assistant &lt;assistant@example.com&gt;<br>"
    "<b>Subject:</b> FW: Test thread</font><div>&nbsp;</div></div>"
    '<div><div style="font-family:Aptos">Can you handle the scheduling?</div>'
    '<div id="Signature"><img src="cid:sig-aaaaaaaaaaaaaaaaaaaa@xtm-one" alt="">'
    '<img src="cid:sig-bbbbbbbbbbbbbbbbbbbb@xtm-one"><p>Sam Example, CEO</p>'
    "<p>____________________</p><p>Book a meeting</p></div>"
    '<div class="x_PlainText">________________________________________<br>'
    "From: Alice Sender &lt;alice@example.org&gt;<br>Sent: Sunday, November 1, 2026 9:22:23 PM<br>"
    "To: Sam Example &lt;sam@example.com&gt;<br>Subject: Test thread<br><br>"
    "Hello Sam,<br><br>This is a test message.<br><br>Best,<br>Alice</div></div></body></html>"
)


def test_a_reply_on_a_forward_of_a_text_original_drops_the_note_on_the_underscore_layout():
    """Row C: the original's header has no divRplyFwdMsg; the note is still cut."""
    assert len(quote_boundaries(FORWARDED_TEXT_REPLY)) == 2
    cut = drop_forward_header(FORWARDED_TEXT_REPLY)
    for gone in ("FW: Test thread", "Can you handle the scheduling?", "Sam Example, CEO", "Book a meeting", "cid:sig-"):
        assert gone not in cut
    quote = cut[cut.index("________________________________________") :]
    # The quote opens on the original sender's header, as Exchange wrote it, untouched.
    assert quote.startswith("________________________________________<br>From: Alice Sender &lt;alice@example.org&gt;")
    assert FORWARDED_TEXT_REPLY.endswith(quote)
    assert cut.startswith(FORWARDED_TEXT_REPLY[: FORWARDED_TEXT_REPLY.index("<hr")])


@pytest.mark.parametrize(
    "opening",
    [
        '<div id="x_divRplyFwdMsg" dir="ltr"><b>From:</b> Alice<br><b>Sent:</b> Sunday</div>',
        '<hr><div><b>De :</b> Alice<br><b>Envoyé :</b> dimanche</div>',
        "<div>________________________________<br>Von: Alice<br>Betreff: Test</div>",
    ],
    ids=["x-prefixed-id", "hr-french", "underscores-german"],
)
def test_each_native_layout_of_the_original_header_is_a_boundary(opening):
    html = (
        '<body><hr><div id="divRplyFwdMsg"><b>From:</b> Sam<br><b>To:</b> assistant</div><div>Note</div>'
        f"{opening}<div>Original</div></body>"
    )
    cut = drop_forward_header(html)
    assert "Note" not in cut and "From:</b> Sam" not in cut and "Original" in cut


def test_a_rule_or_underscores_without_a_header_under_them_open_nothing():
    html = (
        '<body><hr><div id="divRplyFwdMsg"><b>From:</b> Alice<br><b>Sent:</b> Sunday</div>'
        "<div>Hello</div><hr><div>Best</div><div>____________________</div><div>From: the start we ship</div></body>"
    )
    assert len(quote_boundaries(html)) == 1
    assert drop_forward_header(html) == html


def test_a_divider_and_from_prose_in_a_forward_note_never_open_a_false_boundary():
    """Aikido: the note's own divider, a "From:" line of prose, a later Subject: label."""
    html = (
        '<body><hr><div id="divRplyFwdMsg"><b>From:</b> Sam<br><b>Sent:</b> Sunday</div>'
        "<div>Private note</div><div>________________________________</div>"
        "<div>From: the start we agreed on the plan</div><div>Still private</div>"
        "<div>Subject: pricing, keep internal</div>"
        "<div>________________________________<br>From: Alice<br>Sent: Sunday<br>Subject: Intro<br><br>Original</div>"
        "</body>"
    )
    assert len(quote_boundaries(html)) == 2
    cut = drop_forward_header(html)
    for gone in ("Private note", "the start we agreed", "Still private", "pricing"):
        assert gone not in cut
    assert cut.index("From: Alice") < cut.index("Original")


def test_the_recipients_set_on_a_draft_are_the_ones_its_mime_names():
    mime = (
        b"From: assistant@example.com\r\nTo: Old <old@example.org>\r\nCc: stale@example.org,\r\n"
        b" folded@example.org\r\nSubject: RE: Intro\r\n\r\nbody Cc: kept"
    )
    addressed = with_recipient_headers(mime, to=["alice@example.org"], cc=["sam@example.com", "bob@example.org"])
    head, _, body = addressed.partition(b"\r\n\r\n")
    assert b"To: alice@example.org" in head and b"Cc: sam@example.com, bob@example.org" in head
    assert b"old@example.org" not in head and b"stale" not in head and b"folded" not in head
    assert body == b"body Cc: kept"
    cleared = with_recipient_headers(mime, to=["alice@example.org"], cc=[])
    assert b"\r\nCc:" not in cleared.partition(b"\r\n\r\n")[0] and b"folded" not in cleared


def test_the_bcc_set_on_a_draft_travels_in_its_mime():
    mime = b"To: old@example.org\r\nBcc: stale@example.org\r\nSubject: S\r\n\r\nbody"
    addressed = with_recipient_headers(mime, to=["a@example.org"], cc=[], bcc=["hidden@example.org"])
    head = addressed.partition(b"\r\n\r\n")[0]
    assert b"Bcc: hidden@example.org" in head and b"stale" not in head
    cleared = with_recipient_headers(mime, to=["a@example.org"], cc=[], bcc=[])
    assert b"Bcc:" not in cleared.partition(b"\r\n\r\n")[0]
    unknown = with_recipient_headers(mime, to=["a@example.org"], cc=[])
    assert b"Bcc: stale@example.org" in unknown.partition(b"\r\n\r\n")[0]


def test_the_thread_a_forward_comes_from_is_stamped_then_read_back():
    stamped = forward_thread_headers("<orig@mail.example.org>", "<root@x.org> <mid@x.org>")
    assert stamped == [
        {"name": THREAD_IN_REPLY_TO_HEADER, "value": "<orig@mail.example.org>"},
        {"name": THREAD_REFERENCES_HEADER, "value": "<root@x.org> <mid@x.org> <orig@mail.example.org>"},
    ]
    upper = [{"name": h["name"].upper(), "value": h["value"]} for h in stamped]
    assert held_thread(upper, "FW: Intro") == (
        "<orig@mail.example.org>",
        "<root@x.org> <mid@x.org> <orig@mail.example.org>",
    )
    # A forward without the stamp: its own References name the thread.
    assert held_thread([{"name": "References", "value": "<a@x.org> <b@x.org>"}], "Fwd: Intro") == (
        "<b@x.org>",
        "<a@x.org> <b@x.org>",
    )
    assert held_thread([{"name": "References", "value": "<a@x.org>"}], "RE: Intro") is None


def test_the_mime_of_a_reply_gets_the_thread_headers_its_body_untouched():
    mime = (
        b"Subject: RE: Intro\r\nIn-Reply-To: <fwd@ex.com>\r\nReferences: <fwd@ex.com>\r\n <older@ex.com>\r\n"
        b"To: alice@example.org\r\n\r\nIn-Reply-To: body line\r\n"
    )
    out = with_thread_headers(mime, in_reply_to="<orig@x.org>", references="<root@x.org> <orig@x.org>")
    head, body = out.split(b"\r\n\r\n", 1)
    assert b"fwd@ex.com" not in head and b"older@ex.com" not in head
    assert b"In-Reply-To: <orig@x.org>" in head and b"References: <root@x.org> <orig@x.org>" in head
    assert b"To: alice@example.org" in head
    assert body == b"In-Reply-To: body line\r\n"


def test_custom_headers_are_added_to_the_mime_head_only_once_its_body_untouched():
    mime = (
        b"Subject: FW: Intro\r\nX-Xtm-Thread-References: <stale@ex.com>\r\n <older@ex.com>\r\n"
        b"In-Reply-To: <fwd@ex.com>\r\n\r\nx-xtm-thread-references: body line\r\n"
    )
    headers = [
        {"name": "x-xtm-thread-in-reply-to", "value": "<orig@x.org>"},
        {"name": "x-xtm-thread-references", "value": "<root@x.org>\r\n <orig@x.org>"},
    ]
    head, body = with_extra_headers(mime, headers).split(b"\r\n\r\n", 1)
    assert b"stale@ex.com" not in head and b"older@ex.com" not in head
    assert b"x-xtm-thread-in-reply-to: <orig@x.org>" in head
    assert b"x-xtm-thread-references: <root@x.org> <orig@x.org>" in head
    assert b"In-Reply-To: <fwd@ex.com>" in head and b"Subject: FW: Intro" in head
    assert body == b"x-xtm-thread-references: body line\r\n"
    assert with_extra_headers(mime, []) == mime


def test_addresses_are_parsed_from_a_string_or_a_list():
    assert parse_addresses("a@x.io, b@x.io;c@x.io ,") == ["a@x.io", "b@x.io", "c@x.io"]
    assert parse_addresses(["a@x.io", " "]) == ["a@x.io"]
    assert parse_addresses(None) == []
    assert graph_recipients(["a@x.io"]) == [{"emailAddress": {"address": "a@x.io"}}]
