"""Unit tests for Microsoft Teams message normalization (services/teams_format).

Pins the contract that agent-authored Markdown is normalized at every Teams
send boundary so reports render readably instead of as a wall of literal
``**``, ``##``, ``|``, ``[`` / ``]`` characters. The two surfaces differ:

* the Graph ``chatMessage`` HTML body (``markdown_to_teams_html`` /
  ``build_graph_chat_body``) renders bold, headings, lists, links and GFM
  tables as real ``<table>`` markup (Teams renders HTML tables); an
  agent-authored HTML body is passed through intact, only sanitized for safety;
* the Bot Framework ``text`` field (``markdown_to_teams_text``) renders neither
  Markdown nor HTML tables, so there headings downgrade to bold and tables
  become record lines, leaving the Bot-Framework-renderable Markdown intact.

Fenced code blocks are preserved verbatim on the text surface (pipe tables and
``#`` lines inside them are never rewritten).
"""

from __future__ import annotations

import html as _html
import re

from app.services.teams_format import (
    build_graph_chat_body,
    looks_like_escaped_html_teams,
    looks_like_html_teams,
    markdown_to_teams_html,
    markdown_to_teams_text,
)

# A representative "Daily Compliance Posture Digest" authored in Markdown:
# bold TL;DR, a heading, a GFM table and a clickable link with a query string.
DIGEST_MARKDOWN = """\
**TL;DR:** 3 of 5 frameworks are audit-ready.

## 1. Per-framework readiness

| Framework | Controls passing | Status |
| --- | --- | --- |
| SOC 2 | 95% | **Ready** |
| ISO 27001 | 88% | In progress |

Review the [compliance dashboard](https://drata.example.com/d?tab=controls&view=all).

- Action one
- Action two
"""


def _visible_text(html: str) -> str:
    """Return the text a reader sees (tags removed)."""
    return re.sub(r"<[^>]+>", "", html)


# ---------------------------------------------------------------------------
# HTML normalizer — Markdown -> Teams-safe HTML
# ---------------------------------------------------------------------------


def test_markdown_heading_bold_link_render_as_html():
    html = markdown_to_teams_html(DIGEST_MARKDOWN)

    assert "<h2>" in html  # heading rendered
    assert "<strong>" in html  # bold rendered
    assert '<a href="https://drata.example.com/d?tab=controls' in html
    assert "compliance dashboard" in html
    assert "<ul>" in html and "<li>" in html  # bullet list rendered


def test_gfm_table_renders_as_html_table():
    """Teams renders HTML tables, so a GFM table becomes a real <table>."""
    html = markdown_to_teams_html(DIGEST_MARKDOWN)

    assert "<table" in html.lower()
    assert "<th" in html.lower() and "<td" in html.lower()
    # Header labels and cell values survive.
    assert "Framework" in html and "SOC 2" in html
    # Markdown-generated tables get baseline inline borders (Teams ignores
    # <style> blocks, so the styling must be inline).
    assert "border-collapse:collapse" in html
    assert "border:1px solid" in html


def test_html_fenced_code_table_stays_code_while_plain_table_renders():
    """A pipe table inside a fenced code block is kept as literal code; a table
    outside the fence still renders as a real HTML table."""
    body = (
        "```\n| a | b |\n| --- | --- |\n| 1 | 2 |\n```\n\n"
        "| Framework | Status |\n| --- | --- |\n| SOC 2 | Ready |\n"
    )
    html = markdown_to_teams_html(body)
    # In-fence table preserved verbatim as code.
    assert "<pre>" in html and "<code>" in html
    assert "| a | b |" in html
    # Out-of-fence table rendered as real HTML markup.
    assert "<table" in html.lower()
    assert "<td" in html.lower()


def test_agent_authored_html_table_is_preserved():
    """The content_type='html' path must NOT strip the table the agent built.

    Regression guard for the case where the agent decides on its own to send
    rich HTML (verified to render in Teams) - we only sanitize, never convert
    or drop its table/structure.
    """
    agent_html = (
        "<h2>Licensing - Formatting Test</h2>"
        "<p><strong>Bold</strong> and <em>italic</em></p>"
        "<table><thead><tr><th>Tier</th><th>Status</th><th>Notes</th></tr></thead>"
        "<tbody><tr><td>Community</td><td>Active</td><td>Open-source core</td></tr>"
        "<tr><td>Enterprise</td><td>Active</td><td>Full XTM Suite</td></tr></tbody></table>"
        "<blockquote>This is a blockquote</blockquote>"
    )
    body = build_graph_chat_body(agent_html, content_type="html")
    content = body["content"]

    assert body["contentType"] == "html"
    # The table and every structural tag the agent relied on survive as real
    # markup (pre-fix, the <td>/<th> tags were stripped and the cells collapsed
    # into concatenated text).
    assert "<table" in content
    assert "<thead" in content and "<tbody" in content
    assert "<th>Tier</th>" in content and "<td>Community</td>" in content
    assert "<td>Full XTM Suite</td>" in content
    assert "<blockquote>" in content


def test_agent_authored_html_table_is_still_sanitized():
    """Pass-through HTML is still made safe (scripts / handlers stripped)."""
    agent_html = (
        "<table><tr><td>ok</td></tr></table>"
        "<script>alert('xss')</script>"
        '<a href="javascript:alert(1)">x</a>'
    )
    content = build_graph_chat_body(agent_html, content_type="html")["content"]
    assert "<table" in content  # legitimate structure kept
    assert "<script" not in content.lower()
    assert "alert('xss')" not in content
    assert "javascript:" not in content.lower()


def test_blocks_are_separated_with_br_for_breadth():
    """Teams strips CSS margins, so vertical spacing must be structural: a
    ``<br/>`` is inserted between consecutive top-level blocks (heading ->
    table -> paragraph -> list) so the report is not rendered flush."""
    html = markdown_to_teams_html(DIGEST_MARKDOWN)
    # The digest has 5 top-level blocks (p, h2, table, p, ul) -> 3 separators:
    # a list opens with a gap of its own in Teams, so it gets no <br/>.
    assert html.count("<br") >= 3
    assert re.search(r"</p>\s*<ul>", html), html


def test_block_spacing_does_not_touch_list_items_or_table_rows():
    """Only sibling *blocks* are spaced - nested list items and table rows stay
    tight (no <br/> injected directly before <li> or <tr>)."""
    html = markdown_to_teams_html(DIGEST_MARKDOWN)
    assert "<br/><li" not in html.replace("<br>", "<br/>")
    assert "<br/><tr" not in html.replace("<br>", "<br/>")
    assert "<br/><td" not in html.replace("<br>", "<br/>")


def test_single_block_gets_no_separator():
    """A one-block message has nothing to separate, so no stray <br/> is added
    between blocks (the intra-paragraph <br> from nl2br is unaffected)."""
    html = markdown_to_teams_html("Just one paragraph of text.")
    assert "<br" not in html


def test_agent_authored_html_is_not_respaced():
    """Pass-through HTML keeps the agent's own layout - we never inject spacing
    into HTML the agent crafted itself (only sanitize it)."""
    agent_html = "<p>one</p><p>two</p><p>three</p>"
    content = build_graph_chat_body(agent_html, content_type="html")["content"]
    assert "<br" not in content
    assert "<p>one</p><p>two</p><p>three</p>" in content


def test_no_literal_markdown_characters_in_visible_html():
    """No literal |, *, #, [, ] in the visible (rendered) text."""
    visible = _visible_text(markdown_to_teams_html(DIGEST_MARKDOWN))
    for ch in ("|", "*", "#", "[", "]"):
        assert ch not in visible, f"literal {ch!r} leaked into visible text: {visible!r}"
    assert "](" not in visible


def test_links_preserved_exactly_including_query_string():
    html = markdown_to_teams_html(DIGEST_MARKDOWN)
    assert "https://drata.example.com/d?tab=controls&view=all" in html.replace(
        "&amp;", "&"
    )


def test_existing_html_passes_through_without_double_conversion():
    body = "<p>Hello <strong>team</strong></p><ul><li>one</li></ul>"
    html = markdown_to_teams_html(body)
    assert "&lt;p&gt;" not in html
    assert "<p>" in html and "<ul>" in html


def test_double_conversion_is_idempotent():
    once = markdown_to_teams_html(DIGEST_MARKDOWN)
    twice = markdown_to_teams_html(once)
    assert "&lt;h2" not in twice
    assert "<h2>" in twice
    assert "<table" in twice  # the table survives a second pass
    assert '<a href="https://drata.example.com/d?tab=controls' in twice


def test_looks_like_html_detects_block_tags_only():
    assert looks_like_html_teams("<ul><li>x</li></ul>")
    assert looks_like_html_teams("<p>hello</p>")
    assert not looks_like_html_teams("hello **world** with a <br> and <b>bold</b>")


def test_plain_text_becomes_valid_html_with_line_breaks():
    html = markdown_to_teams_html("Line one\nLine two")
    assert "<p>" in html
    assert "<br" in html
    assert "Line one" in html and "Line two" in html


def _lines_after_breaks(html: str) -> list[str]:
    """Return the raw text that opens each line: after a <br> or a block start."""
    return re.findall(r"(?:<br\s*/?>|<p>|<li>|<h\d>)([^<]*)", html)


def test_line_after_a_soft_break_starts_flush_left():
    """A sign-off then a name on the next line: no space, newline or &nbsp;
    between the <br> and the name, which Teams shows as an indent (#8532)."""
    html = markdown_to_teams_html("Kind regards,\nCEO Office")
    assert "<br />CEO Office" in html or "<br/>CEO Office" in html or "<br>CEO Office" in html
    assert "\n" not in html
    assert "&nbsp;CEO" not in html and "\xa0CEO" not in html


def test_paragraph_after_a_blank_line_starts_flush_left():
    html = markdown_to_teams_html("Kind regards,\n\nCEO Office")
    assert "<p>CEO Office</p>" in html
    assert "\n" not in html
    assert " CEO Office" not in html


def test_every_line_of_a_multi_line_body_starts_at_column_zero():
    body = "Hello Sam,\n\nThe report is ready.\n  Indented by mistake.\n\nBien cordialement,\n\nCEO Office"
    html = markdown_to_teams_html(body)
    for line in _lines_after_breaks(html):
        assert line == line.lstrip(" \t\r\n\xa0"), f"leading whitespace in {line!r}: {html!r}"
    assert "Indented by mistake." in html


def test_graph_chat_body_line_breaks_carry_no_leading_space():
    """The Graph chatMessage body (chat, channel post, channel reply) is flush."""
    for content_type in (None, "text", "html"):
        content = build_graph_chat_body("Kind regards,\nCEO Office", content_type)["content"]
        assert re.search(r"<br\s*/?>CEO Office", content), content


def test_agent_html_written_over_several_lines_starts_each_line_flush():
    agent_html = "<p>\n  Kind regards,<br>\n  CEO Office\n</p>"
    content = build_graph_chat_body(agent_html, content_type="html")["content"]
    assert "<p>Kind regards,<br>CEO Office" in content or "<p>Kind regards,<br/>CEO Office" in content, content


def test_line_start_strip_keeps_inline_spaces_lists_bold_links_and_code():
    body = (
        "**Bold** and *italic* with a [link](https://example.com/a?b=1)\n\n"
        "- one **two**\n- three\n\n"
        "```\n    indented code\n```"
    )
    html = markdown_to_teams_html(body)
    assert "<strong>Bold</strong> and <em>italic</em> with a " in html
    assert '<a href="https://example.com/a?b=1"' in html
    assert "<li>one <strong>two</strong></li>" in html and "<li>three</li>" in html
    assert "    indented code" in html  # code keeps its whitespace


def test_intentionally_empty_line_keeps_its_nbsp():
    content = build_graph_chat_body("<p>a</p><p>&nbsp;</p><p>b</p>", content_type="html")["content"]
    assert "<p>&nbsp;</p>" in content or "<p>\xa0</p>" in content


def test_empty_body_returns_empty_string():
    assert markdown_to_teams_html("") == ""
    assert markdown_to_teams_html("   \n  ") == ""


# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------


def test_script_and_event_handlers_are_stripped():
    body = (
        "Hello <script>alert('xss')</script>"
        '<img src=x onerror="alert(1)">'
        '<a href="javascript:alert(2)">click</a>'
    )
    html = markdown_to_teams_html(body)
    assert "<script" not in html.lower()
    assert "alert('xss')" not in html
    assert "onerror" not in html.lower()
    assert "javascript:" not in html.lower()


def test_style_blocks_are_removed_with_content():
    body = "<p>visible</p><style>.x{color:red}</style>"
    html = markdown_to_teams_html(body)
    assert "<style" not in html.lower()
    assert "color:red" not in html


# ---------------------------------------------------------------------------
# Text normalizer — Markdown -> Bot Framework text
# ---------------------------------------------------------------------------


def test_text_table_becomes_record_lines():
    text = markdown_to_teams_text(DIGEST_MARKDOWN)
    # The table row delimiter and pipes are gone, replaced by record bullets.
    assert "|" not in text
    assert "---" not in text
    assert "**Framework:** SOC 2" in text
    assert "**Status:** **Ready**" in text


def test_text_headings_downgraded_to_bold():
    text = markdown_to_teams_text("## Daily digest\n\nbody")
    assert "## " not in text
    assert "**Daily digest**" in text


def test_text_heading_is_surrounded_by_blank_lines():
    """On the Bot Framework text surface a blank line is the spacing primitive,
    so a downgraded heading gets one above and below for section breadth."""
    text = markdown_to_teams_text("Intro line\n## Section\nBody line")
    assert "Intro line\n\n**Section**\n\nBody line" in text


def test_text_heading_spacing_is_not_doubled():
    """Existing blank lines around a heading are reused, never doubled."""
    text = markdown_to_teams_text("## Section\n\nBody")
    assert "\n\n\n" not in text
    assert "**Section**\n\nBody" in text


def test_text_heading_at_start_has_no_leading_blank():
    """A heading that opens the message is not padded with a leading blank."""
    text = markdown_to_teams_text("## Section\n\nBody")
    assert not text.startswith("\n")


def test_text_preserves_bold_italic_lists_and_links():
    body = "Hello **bold** and *italic*\n\n- one\n- two\n\n[link](https://x.example/q?a=1&b=2)"
    text = markdown_to_teams_text(body)
    assert "**bold**" in text
    assert "*italic*" in text
    assert "- one" in text and "- two" in text
    # Bot Framework renders Markdown links natively — leave them intact.
    assert "[link](https://x.example/q?a=1&b=2)" in text


def test_text_empty_body():
    assert markdown_to_teams_text("") == ""
    assert markdown_to_teams_text("   ") == "   "


def test_text_escaped_pipe_in_cell_is_preserved():
    body = "| Name | Note |\n| --- | --- |\n| A | x \\| y |"
    text = markdown_to_teams_text(body)
    # The escaped pipe survives as a literal pipe inside the value, but the
    # table-structure pipes are gone (converted to a record line).
    assert "**Name:** A" in text
    assert "x | y" in text


def test_text_leaves_fenced_code_block_untouched():
    """Tables / headings inside a fenced code block are preserved verbatim."""
    body = (
        "## Real heading\n\n"
        "```\n"
        "# not a heading\n"
        "| a | b |\n"
        "| --- | --- |\n"
        "| 1 | 2 |\n"
        "```\n\n"
        "| Framework | Status |\n"
        "| --- | --- |\n"
        "| SOC 2 | Ready |\n"
    )
    text = markdown_to_teams_text(body)
    # Heading + table OUTSIDE the fence are still normalized.
    assert "**Real heading**" in text
    assert "**Framework:** SOC 2" in text
    # Content INSIDE the fence is preserved verbatim (not rewritten).
    assert "# not a heading" in text
    assert "**not a heading**" not in text
    assert "| a | b |" in text
    assert "**a:** 1" not in text


def test_text_tilde_fence_is_respected():
    """``~~~`` fences are honored just like backtick fences."""
    body = "~~~\n## inside\n| x | y |\n| --- | --- |\n| 1 | 2 |\n~~~"
    text = markdown_to_teams_text(body)
    assert "## inside" in text
    assert "| x | y |" in text
    assert "**inside**" not in text


# ---------------------------------------------------------------------------
# Microsoft Graph chat body builder — always HTML
# ---------------------------------------------------------------------------


def test_build_graph_chat_body_always_html_even_for_text_content_type():
    body = build_graph_chat_body("**bold** and a heading\n\n## H2", content_type="text")
    assert body["contentType"] == "html"
    assert "<strong>bold</strong>" in body["content"]
    assert "<h2>" in body["content"]


def test_build_graph_chat_body_html_hint_passes_through():
    raw = "<p>already <strong>html</strong></p>"
    body = build_graph_chat_body(raw, content_type="html")
    assert body["contentType"] == "html"
    assert "<p>already <strong>html</strong></p>" in body["content"]


def test_build_graph_chat_body_none_content_type_autodetects():
    body = build_graph_chat_body("# Heading\n\nsome text")
    assert body["contentType"] == "html"
    assert "<h1>" in body["content"]


def test_build_graph_chat_body_table_has_no_pipes_in_visible_text():
    body = build_graph_chat_body(DIGEST_MARKDOWN)
    visible = _visible_text(body["content"])
    assert "|" not in visible


# ---------------------------------------------------------------------------
# Regression: a Markdown body mislabelled content_type="html" must still be
# converted, never shipped raw (Teams showed literal ##, **, |, \n otherwise).
# ---------------------------------------------------------------------------


def test_markdown_body_with_html_content_type_is_converted_not_raw():
    """The exact failure that broke the daily digest: the agent authored
    Markdown but passed content_type='html'. The body must be rendered to HTML,
    not dumped raw with literal ## / ** / | characters."""
    body = build_graph_chat_body(DIGEST_MARKDOWN, content_type="html")
    content = body["content"]

    assert body["contentType"] == "html"
    assert "<h2>" in content  # heading converted (not literal ##)
    assert "<strong>" in content  # bold converted (not literal **)
    assert "<table" in content.lower()  # GFM table converted (not literal |)
    visible = _visible_text(content)
    for ch in ("#", "*", "|"):
        assert ch not in visible, f"literal {ch!r} leaked into Teams output"
    assert "](" not in visible  # links converted too


def test_assume_html_true_does_not_force_passthrough_for_markdown():
    """assume_html=True is advisory: a Markdown body is still converted."""
    html = markdown_to_teams_html("## Heading\n\n- a\n- b", assume_html=True)
    assert "<h2>" in html
    assert "<ul>" in html and "<li>" in html
    assert "##" not in _visible_text(html)


def test_real_html_with_html_content_type_still_passes_through():
    """The genuine agent-authored-HTML case is unaffected: real HTML (block
    tags present) is still preserved intact, only sanitized."""
    agent_html = (
        "<h2>Report</h2><p><strong>Bold</strong></p>"
        "<table><tr><td>cell</td></tr></table>"
    )
    content = build_graph_chat_body(agent_html, content_type="html")["content"]
    assert "<h2>Report</h2>" in content
    assert "<td>cell</td>" in content
    # Preserved verbatim - not re-converted, so no spacing <br/> is injected.
    assert "<br" not in content


def test_markdown_with_fenced_html_block_is_not_passthrough():
    """HTML auto-detection is fence-aware: a Markdown report that embeds an HTML
    snippet inside a fenced code block must still be treated as Markdown. The
    surrounding Markdown is converted (heading / bold) and the fenced <table> is
    escaped and rendered as code - never detected as HTML, shipped raw, or
    re-rendered as a live table."""
    body = (
        "## Example report\n\n"
        "Here is the HTML we generate:\n\n"
        "```html\n"
        "<table><tr><td>cell</td></tr></table>\n"
        "```\n\n"
        "End of **report**.\n"
    )
    # The only block-level tag sits inside the fence, so detection must NOT
    # classify the body as HTML.
    assert not looks_like_html_teams(body)

    # Even mislabelled content_type="html", the body is converted, not raw.
    html = markdown_to_teams_html(body, assume_html=True)
    assert "<h2>" in html  # heading converted (not literal ##)
    assert "<strong>report</strong>" in html  # bold converted (not literal **)
    # The fenced snippet is escaped and rendered as code...
    assert "<pre>" in html and "<code" in html
    assert "&lt;table" in html  # the <table> is escaped (shown as code)
    # ...so no LIVE <table> element leaked from the fenced snippet.
    assert "<table" not in html.lower()


def test_markdown_with_inline_html_code_span_is_not_passthrough():
    """HTML detection ignores inline code spans too: a Markdown body that names
    a block-level tag inside backticks (e.g. `<table>`) is still Markdown - the
    tag is escaped and rendered as inline code, never passed through as live
    HTML."""
    body = "Use the `<table>` tag to lay out **rows** of data."
    # The only block tag sits inside an inline code span, so detection must NOT
    # classify the body as HTML.
    assert not looks_like_html_teams(body)

    # Even mislabelled content_type="html", the body is converted, not raw.
    html = markdown_to_teams_html(body, assume_html=True)
    assert "<strong>rows</strong>" in html  # surrounding Markdown converted
    assert "<code" in html and "&lt;table" in html  # tag escaped as code
    assert "<table" not in html.lower()  # no live table leaked


def test_looks_like_html_ignores_inline_and_fenced_block_tags():
    """Direct detector checks: a real block tag is HTML; the same tag inside an
    inline code span or a fenced block is not."""
    assert looks_like_html_teams("<table><tr><td>x</td></tr></table>")
    assert not looks_like_html_teams("inline `<div>` reference only")
    assert not looks_like_html_teams("```\n<p>fenced</p>\n```")
    # A real leading block tag still wins even if inline code follows.
    assert looks_like_html_teams("<p>see `<span>` below</p>")


# ---------------------------------------------------------------------------
# Regression: entity-encoded HTML (the ~5% Graph-send failure)
# ---------------------------------------------------------------------------
# Agents stuffing HTML into a JSON tool argument sometimes entity-encode the
# tags. Live-tag detection misses `&lt;p&gt;...`, Markdown wraps the entities,
# and Teams renders `<p>` / `<b>` / `<ul>` as visible characters.

# Screenshot-shaped body: concatenated <p>s, <b> (not <strong>), <ul>/<li>,
# and a bare ampersand in "Q&A" - the exact shape that leaked as raw tags.
_INCIDENT_HTML = (
    "<p>Hello everyone,</p>"
    "<p>We have identified a <b>security incident</b> affecting production "
    "and the CS team is coordinating the response.</p>"
    "<p><b>What we know:</b></p>"
    "<ul><li>Impact is contained</li><li>Investigation is ongoing</li></ul>"
    "<p>A Q&A session will follow this announcement.</p>"
)


def test_live_html_incident_post_still_sends_real_tags():
    """The 95% path: live HTML is still shipped as live tags, not entities."""
    html = markdown_to_teams_html(_INCIDENT_HTML)
    assert "<p>" in html
    assert "<ul>" in html and "<li>" in html
    assert "<b>" in html or "<strong>" in html
    assert "&lt;p&gt;" not in html
    visible = _visible_text(html)
    assert "<p>" not in visible
    assert "Hello everyone," in visible
    assert "security incident" in visible


def test_entity_encoded_html_is_decoded_to_live_tags():
    """The 5% path: html.escape(agent HTML) must still render, not leak tags."""
    escaped = _html.escape(_INCIDENT_HTML)
    assert "&lt;p&gt;" in escaped
    assert not looks_like_html_teams(escaped)
    assert looks_like_escaped_html_teams(escaped)

    html = markdown_to_teams_html(escaped)
    assert "<p>" in html
    assert "<ul>" in html and "<li>" in html
    assert "<b>" in html or "<strong>" in html
    assert "&lt;p&gt;" not in html
    assert "&lt;b&gt;" not in html
    assert "&lt;ul&gt;" not in html
    visible = _visible_text(html)
    assert "<p>" not in visible
    assert "&lt;" not in visible
    assert "Hello everyone," in visible
    assert "security incident" in visible
    # Bare "&" in "Q&A" is well-formed as &amp; in the HTML source so Graph
    # does not fall back to contentType=text.
    assert "Q&amp;A" in html or "Q&A" in html


def test_double_encoded_html_is_decoded_to_live_tags():
    """`&amp;lt;p&amp;gt;...` (escaped twice) still becomes live HTML."""
    double = _html.escape(_html.escape(_INCIDENT_HTML))
    assert "&amp;lt;p&amp;gt;" in double
    assert looks_like_escaped_html_teams(double)

    html = markdown_to_teams_html(double)
    assert "<p>" in html
    assert "&lt;p&gt;" not in html
    assert "&amp;lt;" not in html
    visible = _visible_text(html)
    assert "<p>" not in visible
    assert "Hello everyone," in visible


def test_encoded_html_inside_inline_code_is_not_decoded():
    """Markdown that *mentions* `&lt;p&gt;hello&lt;/p&gt;` in backticks stays code."""
    body = "Use `&lt;p&gt;hello&lt;/p&gt;` to wrap a paragraph."
    assert not looks_like_html_teams(body)
    assert not looks_like_escaped_html_teams(body)

    html = markdown_to_teams_html(body)
    assert "<code" in html
    # Markdown escapes the already-encoded tags one more time inside <code>.
    assert "&amp;lt;p" in html
    assert "<p>hello</p>" not in html
    # The only live <p> is the Markdown wrapper around the sentence, not a
    # decoded copy of the mentioned tags.
    assert html.count("<p>") <= 1


def test_encoded_html_inside_fenced_code_is_not_decoded():
    """Fenced escaped HTML stays a code sample, never live markup."""
    body = (
        "Example markup:\n\n"
        "```html\n"
        "&lt;p&gt;Hello everyone,&lt;/p&gt;\n"
        "&lt;ul&gt;&lt;li&gt;item&lt;/li&gt;&lt;/ul&gt;\n"
        "```\n"
    )
    assert not looks_like_html_teams(body)
    assert not looks_like_escaped_html_teams(body)

    html = markdown_to_teams_html(body)
    assert "<pre>" in html and "<code" in html
    # The fenced snippet is not promoted to a live list.
    assert "<ul>" not in html.lower()
    assert "<li>" not in html.lower()


def test_entity_encoded_html_via_graph_body_helper():
    """The Graph send helper (content_type=html) still decodes escaped tags."""
    escaped = _html.escape(_INCIDENT_HTML)
    body = build_graph_chat_body(escaped, content_type="html")
    assert body["contentType"] == "html"
    content = body["content"]
    assert "<p>" in content
    assert "&lt;p&gt;" not in content
    assert "<p>" not in _visible_text(content)


# ---------------------------------------------------------------------------
# Autolink - bare URLs and backtick-wrapped URLs become clickable <a>
# ---------------------------------------------------------------------------

_NOTION_URL = (
    "https://app.notion.com/p/Communication-Customers-Communication-Hub-"
    "3cd8fce17f2a811ca9cfe8db5c3cee89"
)


def test_bare_https_url_becomes_anchor():
    html = markdown_to_teams_html(f"See {_NOTION_URL}")
    assert f'<a href="{_NOTION_URL}"' in html
    assert "<code" not in html
    assert _NOTION_URL in _visible_text(html)


def test_backtick_wrapped_url_is_a_link_not_code():
    """The screenshot failure: agents wrap the URL in backticks, Markdown
    emits <code>, and Teams renders unclickable monospace text."""
    html = markdown_to_teams_html(f"Hub: `{_NOTION_URL}`")
    assert f'<a href="{_NOTION_URL}"' in html
    assert "<code" not in html
    visible = _visible_text(html)
    assert _NOTION_URL in visible


def test_existing_markdown_link_keeps_label():
    html = markdown_to_teams_html(
        "Review the [compliance dashboard](https://drata.example.com/d?tab=controls)."
    )
    assert '<a href="https://drata.example.com/d?tab=controls"' in html
    assert "compliance dashboard" in html
    # Destination is not rewritten as the link text.
    assert html.count("https://drata.example.com/d?tab=controls") == 1


def test_trailing_period_is_not_part_of_href():
    html = markdown_to_teams_html("See https://example.com.")
    assert '<a href="https://example.com"' in html
    assert 'href="https://example.com."' not in html
    assert _visible_text(html).endswith(".")


def test_balanced_parentheses_in_url_are_kept():
    url = "https://en.wikipedia.org/wiki/Foo_(bar)"
    html = markdown_to_teams_html(f"See {url}")
    assert f'<a href="{url}"' in html


def test_fenced_code_url_stays_code():
    body = "Example:\n\n```\nhttps://example.com/secret\n```\n"
    html = markdown_to_teams_html(body)
    assert "<pre>" in html and "<code" in html
    assert '<a href="https://example.com/secret"' not in html


def test_non_url_inline_code_stays_code():
    html = markdown_to_teams_html("Use `pip install foo` to install.")
    assert "<code" in html
    assert "pip install foo" in html
    assert "<a " not in html


def test_html_passthrough_autolinks_bare_url():
    html = markdown_to_teams_html(f"<p>See {_NOTION_URL}</p>")
    assert f'<a href="{_NOTION_URL}"' in html


def test_html_passthrough_unwraps_code_url():
    html = markdown_to_teams_html(f"<p>Hub: <code>{_NOTION_URL}</code></p>")
    assert f'<a href="{_NOTION_URL}"' in html
    assert "<code" not in html


def test_bot_framework_text_unwraps_backtick_url():
    text = markdown_to_teams_text(f"Hub: `{_NOTION_URL}`")
    assert f"[{_NOTION_URL}]({_NOTION_URL})" in text
    assert f"`{_NOTION_URL}`" not in text


def test_bot_framework_text_autolinks_bare_url():
    text = markdown_to_teams_text(f"See {_NOTION_URL}")
    assert f"[{_NOTION_URL}]({_NOTION_URL})" in text


# ---------------------------------------------------------------------------
# Regression: mixed escaped-HTML wrapper + escaped code sample. Detection ignores code constructs, so the decode pass must too:
# a full-body html.unescape() promoted a fenced/inline code sample's escaped
# tags to live HTML once the surrounding document was decoded.
# ---------------------------------------------------------------------------


def test_mixed_escaped_wrapper_and_fenced_sample_keeps_fence_encoded():
    """Escaped wrapper outside code decodes; the fenced sample does not."""
    body = (
        "&lt;p&gt;Here is the template we use:&lt;/p&gt;\n"
        "```html\n"
        "&lt;p&gt;Hello everyone,&lt;/p&gt;\n"
        "&lt;ul&gt;&lt;li&gt;item&lt;/li&gt;&lt;/ul&gt;\n"
        "```\n"
        "&lt;p&gt;Use it for announcements.&lt;/p&gt;"
    )
    assert looks_like_escaped_html_teams(body)

    html = markdown_to_teams_html(body)
    # The document wrapper is live HTML again.
    assert "<p>Here is the template we use:</p>" in html
    assert "<p>Use it for announcements.</p>" in html
    # The fenced sample's tags were NOT promoted to live markup.
    assert "<ul>" not in html
    assert "<li>" not in html
    assert "<p>Hello everyone,</p>" not in html
    # The sample still shows its tags as visible characters (entities in the
    # HTML source, which Teams renders as literal < > text).
    assert "&lt;p&gt;Hello everyone,&lt;/p&gt;" in html
    assert "&lt;ul&gt;&lt;li&gt;item&lt;/li&gt;&lt;/ul&gt;" in html


def test_mixed_escaped_wrapper_and_inline_code_keeps_span_encoded():
    """An inline-code mention of escaped tags survives the decode pass."""
    body = (
        "&lt;p&gt;Wrap paragraphs explicitly.&lt;/p&gt; "
        "Mention `&lt;b&gt;bold&lt;/b&gt;` in backticks."
    )
    assert looks_like_escaped_html_teams(body)

    html = markdown_to_teams_html(body)
    assert "<p>Wrap paragraphs explicitly.</p>" in html
    # The backticked sample is not decoded to a live <b>.
    assert "<b>bold</b>" not in html
    assert "&lt;b&gt;bold&lt;/b&gt;" in html


def test_mixed_double_encoded_wrapper_keeps_fenced_sample_encoded():
    """Two decode passes still never touch a fenced code sample."""
    body = (
        _html.escape("&lt;p&gt;Header paragraph.&lt;/p&gt;")
        + "\n```\n&lt;p&gt;sample&lt;/p&gt;\n```\n"
    )
    assert looks_like_escaped_html_teams(body)

    html = markdown_to_teams_html(body)
    assert "<p>Header paragraph.</p>" in html
    assert "<p>sample</p>" not in html
    assert "&lt;p&gt;sample&lt;/p&gt;" in html


# ---------------------------------------------------------------------------
# Block layout: lists, headings, tables and entities (both surfaces)
# ---------------------------------------------------------------------------

#: A reply the way models write it: a list straight under its sentence, a list
#: run on the colon's line, a table under a paragraph, entities.
_TIGHT_REPLY = (
    "## Status\n"
    "Read live:\n"
    "- item one\n"
    "- item two\n"
    "Next steps:\n"
    "1. first\n"
    "2. second\n"
    "| Area | Owner |\n"
    "|---|---|\n"
    "| R&amp;D | Ops |\n"
    "Summary: - alpha - beta - gamma\n"
    "Done&nbsp;today."
)


def test_html_keeps_one_item_per_list_entry_and_every_block_apart():
    html = markdown_to_teams_html(_TIGHT_REPLY)

    assert "<h2>Status</h2>" in html
    assert "<p>Read live:</p>" in html
    assert re.search(r"<ul>\s*<li>item one</li>\s*<li>item two</li>\s*</ul>", html)
    assert re.search(r"<ol>\s*<li>first</li>\s*<li>second</li>\s*</ol>", html)
    assert re.search(r"<ul>\s*<li>alpha</li>\s*<li>beta</li>\s*<li>gamma</li>\s*</ul>", html)
    assert "<table" in html and ">R&amp;D</td>" in html
    visible = _html.unescape(_visible_text(html))
    assert "- item" not in visible and "- alpha" not in visible
    assert "&nbsp;" not in html and "&amp;amp;" not in html and "&amp;nbsp;" not in html
    assert "Done today." in visible.replace("\xa0", " ")


def test_text_puts_a_blank_line_before_each_list_and_one_item_per_line():
    text = markdown_to_teams_text(_TIGHT_REPLY)
    lines = text.split("\n")

    for first_item in ("- item one", "1. first", "- alpha"):
        idx = lines.index(first_item)
        assert lines[idx - 1] == "", f"no blank line before {first_item!r}: {text!r}"
    assert lines[lines.index("- item one") + 1] == "- item two"
    assert lines[lines.index("1. first") + 1] == "2. second"
    assert lines[lines.index("- alpha") + 1 : lines.index("- alpha") + 3] == ["- beta", "- gamma"]
    assert "**Status**" in lines
    assert "- **Area:** R&D, **Owner:** Ops" in lines
    assert "&nbsp;" not in text and "&amp;" not in text
    assert "Done today." in text


def test_block_layout_leaves_code_and_plain_dashes_alone():
    body = "Range: 5 - 10\n\n```\n- not a list\na&nbsp;b\n```\n\nUse `&amp;` as is."
    text = markdown_to_teams_text(body)

    assert "Range: 5 - 10" in text
    assert "```\n- not a list\na&nbsp;b\n```" in text
    assert "`&amp;`" in text
    html = markdown_to_teams_html(body)
    assert "<li>" not in html


def test_block_layout_is_idempotent():
    once = markdown_to_teams_text(_TIGHT_REPLY)
    assert markdown_to_teams_text(once) == once


# ---------------------------------------------------------------------------
# Bold lead-ins followed by lists, with or without a stray tag
# ---------------------------------------------------------------------------

_BOLD_LEAD_REPLY = (
    "Short answer: one sentence.\n"
    "**Heading one**\n"
    "- item a\n"
    "- item b\n"
    "**Heading two**\n"
    "- item c"
)


def _assert_bold_lead_html(html: str) -> None:
    assert "<strong>Heading one</strong>" in html
    assert "<strong>Heading two</strong>" in html
    assert re.search(r"<ul>\s*<li>item a</li>\s*<li>item b</li>\s*</ul>", html), html
    assert re.search(r"<ul>\s*<li>item c</li>\s*</ul>", html), html
    visible = _html.unescape(_visible_text(html))
    assert "**" not in visible and "- item" not in visible, visible


def test_text_bold_lead_lines_and_lists_are_kept_apart():
    lines = markdown_to_teams_text(_BOLD_LEAD_REPLY).split("\n")

    assert lines[0] == "Short answer: one sentence."
    for lead, first_item in (("**Heading one**", "- item a"), ("**Heading two**", "- item c")):
        idx = lines.index(first_item)
        assert lines[idx - 1] == "", f"no blank line before {first_item!r}: {lines!r}"
        assert lines[idx - 2] == lead
    assert lines[lines.index("- item a") + 1] == "- item b"
    assert lines[lines.index("**Heading two**") - 1] == ""


def test_html_bold_lead_lines_and_lists_render_as_lists():
    _assert_bold_lead_html(markdown_to_teams_html(_BOLD_LEAD_REPLY))


_STRAY_TAG_SENTENCE = (
    "Short answer: lists in emails are real <ul> and <li> elements, "
    "not dashes, and every paragraph is a <p>."
)


def test_html_bold_lead_lines_and_lists_keep_tags_named_in_prose_as_text():
    # The tags named in the sentence were
    # swallowed as raw HTML, so their words disappeared.
    body = _BOLD_LEAD_REPLY.replace("Short answer: one sentence.", _STRAY_TAG_SENTENCE)

    for html in (
        markdown_to_teams_html(body),
        markdown_to_teams_html(body, assume_html=True),
        build_graph_chat_body(body, "html")["content"],
    ):
        assert "&lt;ul&gt; and &lt;li&gt; elements" in html, html
        assert "every paragraph is a &lt;p&gt;." in html, html
        assert re.search(r"<ul>\s*<li>item a</li>\s*<li>item b</li>\s*</ul>", html), html
        assert re.search(r"<ul>\s*<li>item c</li>\s*</ul>", html), html
        assert html.count("<li>") == 3, html

    text = markdown_to_teams_text(body)
    assert "&lt;ul&gt; and &lt;li&gt; elements" in text, text
    assert "every paragraph is a &lt;p&gt;." in text, text
    assert "<li>" not in text and "<p>" not in text, text
    # A tag inside a code span stays code on the text surface too.
    assert markdown_to_teams_text("Use `<li>` here.") == "Use `<li>` here."


def test_list_right_after_a_bold_line_on_every_surface():
    body = "**Why earlier emails looked fine**\n- one\n- two"

    text = markdown_to_teams_text(body)
    assert text.split("\n") == ["**Why earlier emails looked fine**", "", "- one", "- two"]
    for html in (
        markdown_to_teams_html(body),
        markdown_to_teams_html(body, assume_html=True),
        build_graph_chat_body(body, "text")["content"],
        build_graph_chat_body(body, "html")["content"],
    ):
        assert re.search(r"<ul>\s*<li>one</li>\s*<li>two</li>\s*</ul>", html), html


def test_markdown_naming_a_block_tag_in_prose_is_not_sent_raw():
    # A tag named outside backticks used to flip the body to the HTML
    # pass-through: newlines collapsed and Teams showed one paragraph.
    body = _BOLD_LEAD_REPLY + "\n**Note**\n- the <p> wrapper is gone"

    for content_type in ("text", "html", None):
        _assert_bold_lead_html(build_graph_chat_body(body, content_type)["content"])
    _assert_bold_lead_html(markdown_to_teams_html(body, assume_html=True))


_PROSE_TAG_TAIL = (
    "**Why earlier emails looked fine**\n\n"
    "- New messages: no stylesheet comes with the quoted mail.\n"
    "- Internal messages: same thing.\n\n"
    "**Why it broke today**\n\n"
    "- Both replies quoted a message written in Outlook.\n"
    "- Lists had already shown a weaker version of this."
)


def _assert_prose_tag_tail(html: str) -> None:
    # The paragraphs, bold lead-ins and lists after the sentence keep their
    # own structure: nothing is folded into one list item.
    assert "<strong>Why earlier emails looked fine</strong>" in html, html
    assert "<strong>Why it broke today</strong>" in html, html
    assert "**" not in html, html
    assert re.search(
        r"<ul>\s*<li>New messages: no stylesheet comes with the quoted mail\.</li>\s*"
        r"<li>Internal messages: same thing\.</li>\s*</ul>",
        html,
    ), html
    assert re.search(r"</ul>\s*(?:<br/?>)?\s*<p><strong>Why it broke today", html), html
    assert html.count("<li>") == 4, html
    assert not html.rstrip().endswith("</li>"), html


def test_inline_code_dash_then_bold_leads_and_lists_keep_their_structure():
    # The reported message: an inline code span holding "- " inside a
    # sentence, then blank-line separated bold lead-ins and "- " lists.
    body = (
        "The integration turns it into bare HTML: paragraphs with no margin, "
        "lists as plain `- ` with no spacing. Whether that renders well depends "
        "on the thread.\n\n" + _PROSE_TAG_TAIL
    )

    for html in (
        markdown_to_teams_html(body),
        build_graph_chat_body(body, "html")["content"],
    ):
        assert "lists as plain <code>-</code> with no spacing." in html, html
        _assert_prose_tag_tail(html)
    text = markdown_to_teams_text(body).split("\n")
    assert text[0].endswith("on the thread.") and "`- `" in text[0]
    assert text[text.index("**Why earlier emails looked fine**") + 2].startswith("- New messages")


def test_block_tag_named_in_prose_does_not_swallow_the_rest_of_the_message():
    # One "<li>" and no "</li>": left live, nh3 closed it at the very end and
    # Teams showed every later paragraph inside one bullet.
    body = (
        "Lists as plain <li> with no spacing. Whether that renders well depends "
        "on the thread.\n\n" + _PROSE_TAG_TAIL
    )

    for html in (
        markdown_to_teams_html(body),
        markdown_to_teams_html(body, assume_html=True),
        build_graph_chat_body(body, "text")["content"],
        build_graph_chat_body(body, "html")["content"],
    ):
        assert html.startswith("<p>Lists as plain &lt;li&gt; with no spacing."), html
        _assert_prose_tag_tail(html)


def test_block_tag_named_in_plain_prose_is_converted_not_passed_through():
    body = (
        "Lists as plain <li> with no spacing.\n\n"
        "Why earlier emails looked fine: no stylesheet came with the quoted mail."
    )

    html = markdown_to_teams_html(body, assume_html=True)
    assert html.startswith("<p>Lists as plain &lt;li&gt; with no spacing.</p>"), html
    assert "<p>Why earlier emails looked fine:" in html, html
    assert "</li>" not in html, html


def test_balanced_tags_and_implicit_html_documents_stay_live():
    # Balanced markup is real HTML; a body opening with a tag keeps its
    # pass-through even when it leaves closing tags implicit.
    assert "<ul><li>a</li><li>b</li></ul>" in markdown_to_teams_html(
        "<p>Intro</p><ul><li>a</li><li>b</li></ul>"
    )
    implicit = markdown_to_teams_html("<p>one<p>two")
    assert "&lt;p&gt;" not in implicit and "one" in implicit and "two" in implicit
    # A tag named in a code span stays code.
    assert "<code>&lt;li&gt;</code>" in markdown_to_teams_html("Use `<li>` here.\n\n- a\n- b")


def test_real_html_with_a_bold_paragraph_still_passes_through():
    body = "<p><b>Heading</b></p>\n<ul><li>a</li><li>b</li></ul>"

    html = markdown_to_teams_html(body, assume_html=True)
    assert "<ul><li>a</li><li>b</li></ul>" in html
    assert "<br" not in html


# ---------------------------------------------------------------------------
# Nested lists: section labels become bold lines over flat lists
# ---------------------------------------------------------------------------

_NESTED_LABEL_REPLY = (
    "Where it stands\n"
    "- Done:\n"
    "  - first fix merged\n"
    "  - second fix merged\n"
    "- In progress:\n"
    "  - review open\n"
    "- Next:\n"
    "    - retest\n"
    "    - report"
)


def test_text_turns_nested_label_bullets_into_bold_lines_over_flat_lists():
    lines = markdown_to_teams_text(_NESTED_LABEL_REPLY).split("\n")

    assert lines == [
        "Where it stands",
        "",
        "**Done:**",
        "",
        "- first fix merged",
        "- second fix merged",
        "",
        "**In progress:**",
        "",
        "- review open",
        "",
        "**Next:**",
        "",
        "- retest",
        "- report",
    ]
    assert not any(line.startswith((" ", "\t")) for line in lines), lines


def test_html_of_nested_label_bullets_has_no_nested_list_on_any_surface():
    for html in (
        markdown_to_teams_html(_NESTED_LABEL_REPLY),
        build_graph_chat_body(_NESTED_LABEL_REPLY, "text")["content"],
        build_graph_chat_body(_NESTED_LABEL_REPLY, "html")["content"],
    ):
        for label in ("Done:", "In progress:", "Next:"):
            assert f"<strong>{label}</strong>" in html, html
            assert f"<li>{label}" not in html and f"<strong>{label}</strong></li>" not in html, html
        assert re.search(r"<ul>\s*<li>first fix merged</li>\s*<li>second fix merged</li>\s*</ul>", html), html
        assert re.search(r"<ul>\s*<li>retest</li>\s*<li>report</li>\s*</ul>", html), html
        assert not re.search(r"<li>(?:(?!</li>).)*<[uo]l>", html, re.S), html
        assert html.count("<ul>") == 3, html


def test_a_label_item_followed_by_siblings_and_a_parent_with_text_children():
    body = "Plan:\n\n- Ship:\n- tests first\n- **Bold parent**\n  1. step one\n  2. step two\n- plain item"
    lines = markdown_to_teams_text(body).split("\n")

    # A parent with no trailing colon is not a label: it stays a bullet, its
    # ordered children folded under it with their numbers.
    assert lines == [
        "Plan:",
        "",
        "**Ship:**",
        "",
        "- tests first",
        "- **Bold parent**",
        "  \u21b3 1. step one",
        "  \u21b3 2. step two",
        "- plain item",
    ]


# ---------------------------------------------------------------------------
# Only short labels become bold lines; a section never swallows the next item
# ---------------------------------------------------------------------------

_LONG_PARENT = "The migration is slower than planned because the index rebuild runs twice"

_SECTION_THEN_TOP_LEVEL = (
    "Status\n- Left before close:\n  - retest the export\n  - merge the docs\n- Why it's slow\n- Who owns it"
)


def test_a_short_label_with_children_becomes_a_bold_line_over_a_flat_list():
    lines = markdown_to_teams_text("- Done:\n  - a\n  - b").split("\n")

    assert lines == ["**Done:**", "", "- a", "- b"]


def test_a_long_sentence_parent_stays_a_bullet_with_its_children_folded_under_it():
    body = f"- {_LONG_PARENT}\n  - the first pass\n  - the second pass\n- next item"
    text = markdown_to_teams_text(body)

    assert "**" not in text, text
    assert text.split("\n") == [
        f"- {_LONG_PARENT}",
        "  \u21b3 the first pass",
        "  \u21b3 the second pass",
        "- next item",
    ]
    for html in (markdown_to_teams_html(body), build_graph_chat_body(body, "html")["content"]):
        assert "<strong>" not in html, html
        assert html.count("<ul>") == 1, html
        assert re.search(
            rf"<li>{_LONG_PARENT}<br/?>\s*\u21b3 the first pass<br/?>\s*\u21b3 the second pass</li>", html
        ), html
        assert not re.search(r"<li>(?:(?!</li>).)*<[uo]l>", html, re.S), html


def test_a_long_label_with_a_colon_stays_a_bullet():
    parent = "These are the steps the release still needs before Friday:"
    text = markdown_to_teams_text(f"- {parent}\n  - tag\n  - publish")

    assert text.split("\n") == [f"- {parent}", "  \u21b3 tag", "  \u21b3 publish"]


def test_a_top_level_bullet_after_a_labelled_section_starts_a_list_of_its_own():
    lines = markdown_to_teams_text(_SECTION_THEN_TOP_LEVEL).split("\n")

    # A blank line and another bullet character: CommonMark opens a new list,
    # so the item never reads as one more item of the section above.
    assert lines == [
        "Status",
        "",
        "**Left before close:**",
        "",
        "- retest the export",
        "- merge the docs",
        "",
        "* Why it's slow",
        "* Who owns it",
    ]
    for html in (
        markdown_to_teams_html(_SECTION_THEN_TOP_LEVEL),
        build_graph_chat_body(_SECTION_THEN_TOP_LEVEL, "text")["content"],
        build_graph_chat_body(_SECTION_THEN_TOP_LEVEL, "html")["content"],
    ):
        assert re.search(r"<ul>\s*<li>retest the export</li>\s*<li>merge the docs</li>\s*</ul>", html), html
        assert re.search(r"<ul>\s*<li>Why it's slow</li>\s*<li>Who owns it</li>\s*</ul>", html), html
        assert "<!--" not in html and "list break" not in html, html
        assert "<p>" not in html.split("<strong>Left before close:</strong>")[1], html


def test_a_top_level_label_after_a_section_is_a_bold_line_of_its_own():
    body = "- Done:\n  - a\n- Next:\n  - b"
    assert markdown_to_teams_text(body).split("\n") == ["**Done:**", "", "- a", "", "**Next:**", "", "- b"]


def test_numbered_lists_get_the_same_labels_folding_and_section_break():
    labelled = "1. Done:\n   1. a\n   2. b\n2. Why it's slow"
    assert markdown_to_teams_text(labelled).split("\n") == ["**Done:**", "", "1. a", "2. b", "", "2) Why it's slow"]
    html = markdown_to_teams_html(labelled)
    assert re.search(r"<ol>\s*<li>a</li>\s*<li>b</li>\s*</ol>", html), html
    assert re.search(r"<ol( start=\"2\")?>\s*<li>Why it's slow</li>\s*</ol>", html), html

    long_parent = f"1. {_LONG_PARENT}\n   1. first\n   2. second\n2. next"
    assert markdown_to_teams_text(long_parent).split("\n") == [
        f"1. {_LONG_PARENT}",
        "  \u21b3 1. first",
        "  \u21b3 2. second",
        "2. next",
    ]


def test_plain_lists_with_no_nesting_are_untouched():
    for body in ("- a\n- b\n- c", "1. one\n2. two", f"- {_LONG_PARENT}\n- Done: shipped"):
        assert markdown_to_teams_text(body) == body


def test_the_new_flattening_is_idempotent_on_the_text_surface():
    for body in (_SECTION_THEN_TOP_LEVEL, f"- {_LONG_PARENT}\n  - a\n- b"):
        once = markdown_to_teams_text(body)
        assert markdown_to_teams_text(once) == once


def test_flat_lists_and_code_are_not_rewritten_and_the_flattening_is_idempotent():
    flat = "Read live:\n\n- **Area:** R&D\n- item two"
    assert markdown_to_teams_text(flat).split("\n") == ["Read live:", "", "- **Area:** R&D", "- item two"]
    fenced = "```\n- Done:\n  - kept\n```"
    assert markdown_to_teams_text(fenced) == fenced
    once = markdown_to_teams_text(_NESTED_LABEL_REPLY)
    assert markdown_to_teams_text(once) == once


# A headline line over a list: one standard gap on both surfaces.

_HEADLINE_OVER_LIST = "\u2705 The fix is live.\n\n- one\n- two\n- three\n- four"


def test_a_paragraph_over_a_list_gets_one_gap_on_the_html_surface():
    html = build_graph_chat_body(_HEADLINE_OVER_LIST)["content"]
    # Teams opens a list with its own gap: a <br/> before it doubled it.
    assert re.search(r"</p>\s*<ul>", html), html
    assert "<br" not in html, html
    assert html.count("<li>") == 4


def test_a_bold_label_over_a_list_gets_one_gap_and_paragraphs_stay_spaced():
    html = markdown_to_teams_html("Intro.\n\n- Done:\n  - x\n  - y\n- Left:\n  - z")
    assert re.search(r"<p><strong>Done:</strong></p>\s*<ul>", html), html
    assert re.search(r"<p><strong>Left:</strong></p>\s*<ul>", html), html
    # A paragraph after a paragraph or a list still gets its <br/>.
    assert re.search(r"<p>Intro\.</p><br/><p><strong>Done:", html), html
    assert re.search(r"</ul><br/><p><strong>Left:", html), html
    assert "<p>one</p><br/><p>two</p>" in markdown_to_teams_html("one\n\ntwo")


def test_consecutive_blank_lines_collapse_to_one_on_the_text_surface():
    assert markdown_to_teams_text("Headline.\n\n\n- a\n- b") == "Headline.\n\n- a\n- b"
    assert markdown_to_teams_text(_HEADLINE_OVER_LIST) == _HEADLINE_OVER_LIST
    assert markdown_to_teams_text("one\n\n\n\ntwo") == "one\n\ntwo"
    # Blank lines inside fenced code are code: kept as written.
    fenced = "```\na\n\n\nb\n```"
    assert markdown_to_teams_text(fenced) == fenced
