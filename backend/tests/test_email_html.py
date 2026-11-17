"""Unit tests for email body normalization (services/email_html).

Pins the contract that every email body delivered through the platform's
email-send tools is well-formed, sanitized HTML — regardless of whether the
agent authored Markdown, HTML or plain text — so reports render in Outlook
instead of showing literal ``|``, ``*``, ``[`` / ``]`` and ``(`` characters.
"""

from __future__ import annotations

import re

import pytest
from bs4 import BeautifulSoup

from app.services.email_html import (
    DEFAULT_COMPOSE_STYLE,
    EMAIL_BLOCK_SPACING_STYLE,
    EMAIL_TABLE_STYLE,
    _plaintext_fallback,
    build_graph_email_body,
    compose_declarations,
    looks_like_html_email,
    markdown_to_email_html,
)

SPACED = EMAIL_BLOCK_SPACING_STYLE
COMPOSE = DEFAULT_COMPOSE_STYLE
#: The compose style of a heading: it keeps its own size.
COMPOSE_HEADING = "font-family:Aptos,Calibri,Arial,sans-serif;color:rgb(0,0,0)"
#: The setting that keeps the generated markup theme-neutral: the structure tests read it bare.
NEUTRAL = "none"

# A representative P1 alert report (incident #8942) authored in Markdown:
# a GFM table + bold + clickable links to Defender / Grafana.
P1_ALERT_MARKDOWN = """\
**P1 - Critical alert** for incident #8942.

| Severity | Asset | Status |
| --- | --- | --- |
| **Critical** | web-prod-01 | Open |
| High | db-prod-02 | Mitigating |

Investigate in [Microsoft Defender](https://security.microsoft.com/incidents/8942)
and review the [Grafana dashboard](https://grafana.example.com/d/abc?from=now-1h&to=now).
"""


def _visible_text(html: str) -> str:
    """Return the text that a reader sees (tags + attributes removed)."""
    return re.sub(r"<[^>]+>", "", html)


# ---------------------------------------------------------------------------
# Acceptance criterion: Markdown table + links + bold -> real HTML
# ---------------------------------------------------------------------------


def test_markdown_table_links_bold_render_as_html():
    html = markdown_to_email_html(P1_ALERT_MARKDOWN)

    # A real bordered table with inline styles (Outlook ignores <style> blocks).
    assert "<table" in html
    assert "border-collapse:collapse" in html
    assert "<th" in html and "<td" in html
    assert "border:1px solid #d0d0d0" in html

    # Bold text became <strong>, links became clickable anchors.
    assert "<strong>" in html
    assert '<a href="https://security.microsoft.com/incidents/8942"' in html
    assert "Microsoft Defender" in html


def test_no_literal_markdown_characters_in_visible_text():
    """No literal |, *, [, ] characters and no "](" Markdown-link artifact in the
    visible text. A bare "(" can legitimately appear in prose (and in href URL
    query strings), so it is deliberately not asserted absent on its own."""
    html = markdown_to_email_html(P1_ALERT_MARKDOWN)
    visible = _visible_text(html)
    for ch in ("|", "*", "[", "]"):
        assert ch not in visible, f"literal {ch!r} leaked into visible text: {visible!r}"
    # The only parentheses left belong to the URL query string inside href=,
    # never the Markdown link syntax — so visible text has no "](" artifact.
    assert "](" not in visible


def test_links_preserved_exactly_including_query_string():
    """Defender / Grafana URLs (incl. query strings) must not be mangled."""
    html = markdown_to_email_html(P1_ALERT_MARKDOWN)
    assert "https://grafana.example.com/d/abc?from=now-1h&to=now" in html.replace("&amp;", "&")
    assert "https://security.microsoft.com/incidents/8942" in html


# ---------------------------------------------------------------------------
# Idempotency & content-type detection
# ---------------------------------------------------------------------------


def test_existing_html_passes_through_without_double_conversion():
    body = "<p>Hello <strong>team</strong></p><table><tr><th>A</th><td>1</td></tr></table>"
    html = markdown_to_email_html(body)
    # No escaped tags (would indicate the HTML was treated as Markdown text).
    assert "&lt;p&gt;" not in html
    assert "<p " in html and "<table" in html


def test_double_conversion_is_idempotent():
    once = markdown_to_email_html(P1_ALERT_MARKDOWN)
    twice = markdown_to_email_html(once)
    # Second pass detects HTML and only re-sanitizes — table + link survive,
    # nothing gets escaped into literal &lt;table&gt; text.
    assert "&lt;table" not in twice
    assert "<table" in twice
    assert '<a href="https://security.microsoft.com/incidents/8942"' in twice


def test_looks_like_html_email_detects_block_tags_only():
    assert looks_like_html_email("<table><tr><td>x</td></tr></table>")
    assert looks_like_html_email("<p>hello</p>")
    # Inline-only markup is NOT treated as HTML (still Markdown).
    assert not looks_like_html_email("hello **world** with a <br> and <b>bold</b>")


# ---------------------------------------------------------------------------
# Plain text -> valid HTML
# ---------------------------------------------------------------------------


def test_plain_text_becomes_valid_html_with_line_breaks():
    html = markdown_to_email_html("Line one\nLine two")
    assert "<p " in html
    assert "<br" in html
    assert "Line one" in html and "Line two" in html


def test_empty_body_returns_empty_string():
    assert markdown_to_email_html("") == ""
    assert markdown_to_email_html("   \n  ") == ""


# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------


def test_script_and_event_handlers_are_stripped():
    body = 'Hello <script>alert(\'xss\')</script><img src=x onerror="alert(1)"><a href="javascript:alert(2)">click</a>'
    html = markdown_to_email_html(body)
    assert "<script" not in html.lower()
    assert "alert('xss')" not in html  # content of <script> removed too
    assert "onerror" not in html.lower()
    assert "javascript:" not in html.lower()


def test_style_blocks_are_removed_with_content():
    body = "<p>visible</p><style>.x{color:red}</style>"
    html = markdown_to_email_html(body)
    assert "<style" not in html.lower()
    assert "color:red" not in html


# ---------------------------------------------------------------------------
# Microsoft Graph body builder — always HTML
# ---------------------------------------------------------------------------


def test_build_graph_email_body_always_html_even_for_text_content_type():
    body = build_graph_email_body("**bold** and a | table | row", content_type="text")
    assert body["contentType"] == "HTML"
    # Markdown was rendered even though the caller declared content_type="text".
    assert "<strong>bold</strong>" in body["content"]


def test_build_graph_email_body_html_hint_passes_through():
    raw = "<p>already <strong>html</strong></p>"
    body = build_graph_email_body(raw, content_type="html")
    assert body["contentType"] == "HTML"
    assert (
        f'<p class="elementToProof" style="{SPACED}"><span style="{COMPOSE}">already <strong>html</strong></span></p>'
        in body["content"]
    )


def test_build_graph_email_body_none_content_type_autodetects():
    body = build_graph_email_body("# Heading\n\nsome text")
    assert body["contentType"] == "HTML"
    assert "<h1 " in body["content"]


# ---------------------------------------------------------------------------
# Lists written as people write mail: no blank line around them
# ---------------------------------------------------------------------------


def test_a_list_right_under_a_line_of_text_is_a_real_list():
    html = markdown_to_email_html(
        "Hello,\n\nHere are the slots:\n- Monday 10:00\n- **Tuesday** 14:00\nBest,\nSam", compose_style=NEUTRAL
    )
    assert f'<p style="{SPACED}">Here are the slots:</p>' in html
    assert f'<ul style="{SPACED}">\n<li>Monday 10:00</li>\n<li><strong>Tuesday</strong> 14:00</li>\n</ul>' in html
    # The sign-off right under the last item is its own paragraph, not part of the item.
    assert f'<p style="{SPACED}">Best,<br/>\nSam</p>' in html
    assert "- Monday" not in _visible_text(html)


def test_a_numbered_list_right_under_text_is_an_ordered_list():
    html = markdown_to_email_html("Steps:\n1. Open the report\n2. Check the table", compose_style=NEUTRAL)
    assert "<ol " in html and "<li>Open the report</li>" in html and "<li>Check the table</li>" in html


def test_a_nested_item_and_a_fenced_block_are_left_as_written():
    nested = markdown_to_email_html("Plan:\n- Ship\n    - tests first\n- Retest", compose_style=NEUTRAL)
    assert nested.count("<ul") == 2 and "<li>tests first</li>" in nested
    # Only the outer list is spaced: the nested one keeps its place inside its item.
    assert nested.count(SPACED) == 2 and "<ul>" in nested
    fenced = markdown_to_email_html("Run:\n```\nline\n- not a list\n```", compose_style=NEUTRAL)
    assert "<li>" not in fenced and "- not a list" in fenced
    # A fence of the other kind inside a block is code, not its end: what follows the block still parses.
    mixed = markdown_to_email_html("Run:\n\n```\n~~~\n- not a list\n```\nThen:\n- a real item", compose_style=NEUTRAL)
    assert "- not a list" in mixed and "<li>a real item</li>" in mixed and mixed.count("<li>") == 1


def test_a_dash_inside_a_sentence_is_not_a_list():
    html = markdown_to_email_html("We met on Monday - and again on Friday.")
    assert "<li>" not in html


_NO_THEME_OVERRIDE = re.compile(r"color\s*:|background|font-family|font-size|<font\b", re.IGNORECASE)


def test_with_the_compose_style_set_to_none_a_markdown_body_carries_no_colour_and_no_font():
    """An integration that sets ``none`` keeps the theme-neutral markup: text follows the client's theme."""
    html = build_graph_email_body(P1_ALERT_MARKDOWN, "markdown", compose_style=NEUTRAL)["content"]
    assert "<table" in html and "<th" in html and "<a " in html
    assert not _NO_THEME_OVERRIDE.search(html)
    assert "elementToProof" not in html


# ---------------------------------------------------------------------------
# Outlook's native compose style: dark mode inverts it like the quoted thread
# ---------------------------------------------------------------------------


def _texts_without_compose_colour(html: str) -> list[str]:
    """The visible text of *html* that no element around it gives a colour."""
    soup = BeautifulSoup(html, "html.parser")
    return [
        str(text).strip()
        for text in soup.find_all(string=True)
        if str(text).strip()
        and not any("color:" in str(parent.get("style") or "") for parent in text.parents if parent.name)
    ]


def test_a_text_body_is_written_as_outlook_composes_it():
    """Grey text in Outlook dark mode: every paragraph now carries Aptos 11pt black, as a message composed in Outlook."""
    html = build_graph_email_body("Dear Paul,\n\nSentence one.\n\nKind regards,", "text")["content"]
    assert html == (
        f'<p class="elementToProof" style="{SPACED}"><span style="{COMPOSE}">Dear Paul,</span></p>\n'
        f'<p class="elementToProof" style="{SPACED}"><span style="{COMPOSE}">Sentence one.</span></p>\n'
        f'<p class="elementToProof" style="{SPACED}"><span style="{COMPOSE}">Kind regards,</span></p>'
    )


def test_every_text_of_a_markdown_body_carries_the_compose_colour():
    """Paragraphs, list items, cells, headings, links: nothing is left for dark mode to render grey."""
    html = build_graph_email_body(P1_ALERT_MARKDOWN + "\n\n- one\n- two\n\n> quoted", "markdown")["content"]
    assert _texts_without_compose_colour(html) == []
    assert f'<li><span style="{COMPOSE}">one</span></li>' in html
    assert f'<span style="{COMPOSE}"><strong>P1 - Critical alert</strong>' in html


def test_a_heading_keeps_its_size_and_code_its_font():
    html = build_graph_email_body("# Title\n\n```\nx = 1\n```", "markdown")["content"]
    assert f'<span style="{COMPOSE_HEADING}">Title</span></h1>' in html
    assert '<span style="font-size:11pt;color:rgb(0,0,0)"><code>x = 1' in html


def test_an_html_body_gets_only_what_its_author_left_unset():
    """An author's colour, font or size is theirs, on the element or a container around it."""
    body = (
        '<p style="color:#666">Grey on purpose</p>'
        '<div style="font-family:Segoe UI;font-size:14px;color:#1f2937"><p>Inside</p></div>'
        "<p>Bare</p>"
    )
    html = build_graph_email_body(body, "html")["content"]
    assert (
        '<p class="elementToProof" style="color:#666;margin-top:0;margin-bottom:12pt">'
        '<span style="font-family:Aptos,Calibri,Arial,sans-serif;font-size:11pt">Grey on purpose</span></p>'
    ) in html
    assert f'<p style="{SPACED}">Inside</p>' in html
    assert f'<p class="elementToProof" style="{SPACED}"><span style="{COMPOSE}">Bare</span></p>' in html


def test_a_paragraph_keeps_only_outlooks_class():
    html = build_graph_email_body('<p class="x elementToProof">a</p><p class="hostile">b</p>', "html")["content"]
    assert "hostile" not in html and " x " not in html and html.count('class="elementToProof"') == 2


def test_the_compose_style_is_set_per_integration_and_filtered_as_an_author_style():
    html = build_graph_email_body("Hi", "text", compose_style="font-family:Arial;color:#111;position:fixed")["content"]
    assert (
        html == f'<p class="elementToProof" style="{SPACED}"><span style="font-family:Arial;color:#111">Hi</span></p>'
    )
    assert compose_declarations(None) == compose_declarations("  ") == compose_declarations(COMPOSE)
    assert compose_declarations("None") == {}
    assert compose_declarations("background:url(x)") == {}


def test_the_compose_style_is_idempotent():
    once = markdown_to_email_html("Dear Paul,\n\n- one\n\n**Bold** [link](https://example.com)")
    assert markdown_to_email_html(once, assume_html=True) == once


def test_an_html_body_keeps_the_colours_and_fonts_its_author_wrote_and_drops_font_tags():
    """The author's inline styles are theirs: the platform adds no colour, it removes none it was given."""
    body = (
        '<p style="color:#666;font-family:Aptos;font-size:11pt;text-align:center">Hi</p>'
        '<font color="gray" face="Arial">old</font>'
        '<table style="background:#f3f3f3"><tr><td style="border:1px solid #ccc;background-color:#eee">x</td></tr></table>'
    )
    html = build_graph_email_body(body, "html")["content"]
    assert (
        '<p style="color:#666;font-family:Aptos;font-size:11pt;text-align:center;margin-top:0;margin-bottom:12pt">'
        in html
    )
    assert '<table style="background:#f3f3f3;margin-top:0;margin-bottom:12pt">' in html
    assert '<td style="border:1px solid #ccc;background-color:#eee">' in html
    assert "<font" not in html and "old" in html


# ---------------------------------------------------------------------------
# Author inline styles kept, unsafe declarations dropped
# ---------------------------------------------------------------------------


def test_a_heading_with_its_own_style_is_sent_with_exactly_that_style():
    """A heading's own margin is kept as written: no block spacing is added on top of it."""
    html = build_graph_email_body('<h2 style="font-size:16px;margin:22px 0 8px">Title</h2><p>Text</p>', "html")[
        "content"
    ]
    assert html == (
        f'<h2 style="font-size:16px;margin:22px 0 8px"><span style="{COMPOSE_HEADING}">Title</span></h2>'
        f'<p class="elementToProof" style="margin-top:0;margin-bottom:12pt"><span style="{COMPOSE}">Text</span></p>'
    )


def test_a_rating_box_keeps_its_background_and_left_bar_and_a_container_its_font():
    body = (
        '<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;line-height:1.45;color:#1f2937;max-width:720px">'
        '<div style="background:#fef2f2;border-left:4px solid #dc2626;padding:12px 16px;margin:0 0 22px">Box</div>'
        '<ul style="margin:0;padding-left:20px"><li>a</li></ul>'
        '<table><tr style="background:#f3f4f6"><td>x</td></tr></table>'
        '<p>Up <span style="color:#16a34a">+4%</span></p>'
        "</div>"
    )
    html = build_graph_email_body(body, "html")["content"]
    assert (
        '<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;line-height:1.45;color:#1f2937;max-width:720px">'
        in html
    )
    assert '<div style="background:#fef2f2;border-left:4px solid #dc2626;padding:12px 16px;margin:0 0 22px">' in html
    assert '<ul style="margin:0;padding-left:20px">' in html
    assert '<tr style="background:#f3f4f6">' in html
    assert '<span style="color:#16a34a">' in html


@pytest.mark.parametrize(
    "declaration",
    [
        "background:url(https://tracker.example/p.gif)",
        "background-image:url(x)",
        "width:expression(alert(1))",
        "color:red;behavior:url(x.htc)",
        "position:absolute",
        "background:javascript:alert(1)",
        "font-family:\\75rl(x)",
        "color:/* */red",
        "background:url(data:image/png;base64,AAAA)",
    ],
)
def test_an_unsafe_declaration_is_dropped_and_the_safe_ones_are_kept(declaration):
    html = build_graph_email_body(f'<p style="padding:4px;{declaration}">x</p>', "html")["content"]
    style = BeautifulSoup(html, "html.parser").p["style"]
    assert style.startswith("padding:4px")
    for needle in ("url(", "expression(", "behavior", "position", "javascript", "\\", "/*", "base64"):
        assert needle not in style.lower()


def test_a_style_left_empty_by_the_filter_is_removed():
    html = build_graph_email_body('<div style="position:fixed;top:0">x</div>', "html", compose_style=NEUTRAL)["content"]
    assert html == "<div>x</div>"


# ---------------------------------------------------------------------------
# Paragraph spacing that survives Outlook's head stylesheet
# ---------------------------------------------------------------------------


def _unspaced_blocks(html: str) -> list[str]:
    """The block tags of *html* outside a list item or a cell that carry no inline margin."""
    soup = BeautifulSoup(html, "html.parser")
    found = soup.find_all(["p", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "table"])
    return [
        str(tag)[:60]
        for tag in found
        if tag.find_parent(["li", "td", "th"]) is None and "margin" not in str(tag.get("style") or "")
    ]


def test_a_text_reply_gets_a_blank_line_under_each_paragraph():
    """Five blocks: greeting, sentence, a two-line slot block, the close; Outlook's p {margin:0} cannot glue them."""
    html = build_graph_email_body(
        "Dear Paul,\n\nSentence one.\n\nSlot 1\nSlot 2\n\nKind regards,", "text", compose_style=NEUTRAL
    )["content"]
    assert html == (
        f'<p style="{SPACED}">Dear Paul,</p>\n'
        f'<p style="{SPACED}">Sentence one.</p>\n'
        f'<p style="{SPACED}">Slot 1<br/>\nSlot 2</p>\n'
        f'<p style="{SPACED}">Kind regards,</p>'
    )
    assert _unspaced_blocks(html) == []


def test_a_markdown_list_is_spaced_before_and_after():
    html = build_graph_email_body("Hello,\n\nSlots:\n- Monday\n- Tuesday\n\nBest,", "markdown", compose_style=NEUTRAL)[
        "content"
    ]
    assert f'<ul style="{SPACED}">' in html and f'<p style="{SPACED}">Slots:</p>' in html
    assert "<li>Monday</li>" in html
    assert _unspaced_blocks(html) == []


def test_headings_quotes_code_and_tables_are_spaced_but_their_cells_are_not():
    html = markdown_to_email_html(P1_ALERT_MARKDOWN + "\n\n> quoted\n\n```\ncode\n```")
    assert _unspaced_blocks(html) == []
    assert f'<table style="{EMAIL_TABLE_STYLE};{SPACED}">' in html
    assert '<td style="border:1px solid #d0d0d0;padding:6px 10px;text-align:left">' in html


def test_caller_html_keeps_its_own_margins_byte_identical():
    body = (
        '<p style="margin:0 0 6pt 0">One</p>'
        '<p style="margin-bottom:0">Two</p>'
        '<ul style="margin-top:4pt;margin-bottom:4pt"><li>x</li></ul>'
    )
    assert build_graph_email_body(body, "html", compose_style=NEUTRAL)["content"] == body


def test_bare_caller_paragraphs_get_the_spacing_and_keep_their_layout():
    html = build_graph_email_body('<p style="text-align:center">Hi</p><p>There</p>', "html", compose_style=NEUTRAL)[
        "content"
    ]
    assert html == f'<p style="text-align:center;{SPACED}">Hi</p><p style="{SPACED}">There</p>'


def test_spacing_is_idempotent():
    once = markdown_to_email_html("Dear Paul,\n\nSentence one.")
    assert markdown_to_email_html(once) == once
    assert once.count(SPACED) == 2


def test_the_escaped_fallback_is_spaced_too():
    assert _plaintext_fallback("a\nb") == f'<p style="{SPACED}">a<br>b</p>'
