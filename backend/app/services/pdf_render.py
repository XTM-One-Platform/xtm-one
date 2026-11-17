# SPDX-License-Identifier: Apache-2.0

"""Shared HTML / Markdown → PDF rendering.

Single source of truth for turning agent-authored Markdown / HTML into a
clean, printable A4 PDF via the headless Chromium that Playwright already
ships for the ``browser`` builtin tool. Used by:

* ``services/builtin_tools/media.py`` — the ``generate_file`` tool
  (downloadable PDF artifacts in chat / assignments).
* ``integrations/providers/opencti.py`` — ``upload_opencti_file`` renders a
  real PDF server-side before attaching it to an OpenCTI entity, so agents
  can author summaries in Markdown and attach a proper PDF without any
  manual browser export step.

Why Playwright and not WeasyPrint / wkhtmltopdf:

* Playwright is **already a backend dependency** for the ``browser`` builtin
  tool (``services/browser_tool.py``) and the ``playwright install chromium``
  step runs in every Docker image build — adding WeasyPrint / wkhtmltopdf
  would mean a new C toolchain (Cairo + Pango) just to get HTML → PDF, which
  is the thing Chromium already does perfectly.
* Each render uses a fresh ``new_context()`` so PDF generation is isolated
  from any in-flight agent browser sessions.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Default page size + margins for Playwright PDF rendering — single source
# of truth so the tool builder + tests + future channel exporters all agree
# on the page geometry.
PDF_RENDER_FORMAT = "A4"
PDF_RENDER_MARGIN = "12mm"

# Minimal CSS sheet wrapped around plain / markdown content when rendering
# to PDF.  Uses a system font stack so the rendered PDF is consistent
# regardless of which fonts the Playwright Chromium image happens to ship
# with (the standard Filigran Geologica / IBM Plex fonts are not bundled
# in the headless browser image).  Keep the styles conservative — tables,
# code blocks, blockquotes — so a typical "report from the agent" looks
# clean without us having to maintain a full theme.
PDF_DEFAULT_STYLESHEET = """
:root { color-scheme: light; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
               "Helvetica Neue", Arial, sans-serif;
  font-size: 11pt;
  line-height: 1.55;
  color: #1f2933;
  margin: 0;
  -webkit-print-color-adjust: exact;
  print-color-adjust: exact;
}
h1, h2, h3, h4, h5, h6 {
  font-weight: 600;
  line-height: 1.25;
  margin-top: 1.3em;
  margin-bottom: 0.45em;
  color: #0b1f33;
  page-break-after: avoid;
}
h1 {
  font-size: 22pt;
  margin-top: 0;
  border-bottom: 2px solid #0072ce;
  padding-bottom: 0.25em;
}
h2 {
  font-size: 16pt;
  border-bottom: 1px solid #e2e6ea;
  padding-bottom: 0.2em;
}
h3 { font-size: 13pt; }
h4 { font-size: 11.5pt; }
p { margin: 0.6em 0; }
ul, ol { margin: 0.6em 0; padding-left: 1.6em; }
li { margin: 0.25em 0; }
li > p { margin: 0.2em 0; }
strong { color: #0b1f33; }
pre {
  background: #f6f8fa;
  border: 1px solid #e2e6ea;
  padding: 0.8em 1em;
  border-radius: 6px;
  font-family: "SF Mono", Menlo, Consolas, monospace;
  font-size: 9.5pt;
  line-height: 1.45;
  overflow-x: auto;
  white-space: pre-wrap;
  word-wrap: break-word;
  page-break-inside: avoid;
}
code {
  background: #eef1f4;
  padding: 0.12em 0.35em;
  border-radius: 3px;
  font-family: "SF Mono", Menlo, Consolas, monospace;
  font-size: 9.5pt;
}
pre > code { background: transparent; padding: 0; }
blockquote {
  margin: 0.9em 0;
  padding: 0.4em 1em;
  border-left: 3px solid #0072ce;
  background: #f6f9fc;
  color: #3b4a59;
}
table {
  border-collapse: collapse;
  width: 100%;
  margin: 1em 0;
  font-size: 10pt;
  page-break-inside: avoid;
}
th, td {
  border: 1px solid #dfe3e8;
  padding: 0.5em 0.75em;
  text-align: left;
  vertical-align: top;
}
thead th {
  background: #0b1f33;
  color: #ffffff;
  font-weight: 600;
  border-color: #0b1f33;
}
tbody tr:nth-child(even) { background: #f6f8fa; }
a { color: #0072ce; text-decoration: none; }
a:hover { text-decoration: underline; }
img { max-width: 100%; height: auto; }
hr { border: none; border-top: 1px solid #e2e6ea; margin: 1.6em 0; }
.rendered-plaintext {
  white-space: pre-wrap;
  word-wrap: break-word;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
               "Helvetica Neue", Arial, sans-serif;
  background: transparent;
  border: none;
  padding: 0;
  font-size: 11pt;
}
"""


def looks_like_html(text: str) -> bool:
    """Cheap structural sniff for HTML — used to decide whether to wrap
    plain / Markdown content in a default ``<html><body>`` shell before
    handing it to Playwright.  Matches the most common entry points
    that real-world agent output uses (``<!DOCTYPE html>``,
    ``<html …>``, or just ``<body>`` / ``<article>``) without pulling in
    a full HTML parser dependency just for this check.
    """
    stripped = text.lstrip().lower()
    return (
        stripped.startswith("<!doctype html")
        or stripped.startswith("<html")
        or stripped.startswith("<body")
        or stripped.startswith("<article")
    )


def render_markdown_to_html(content: str) -> str:
    """Render Markdown to an HTML fragment for PDF / HTML file generation.

    Agents author their reports in Markdown (headings, GitHub-style pipe
    tables, lists, fenced code blocks).  Rendering that Markdown to real
    HTML — instead of dumping it verbatim into a ``<pre>`` block — is what
    turns the generated PDF from a wall of literal ``#`` and ``|``
    characters into an actual formatted document.

    Extensions enabled:

    * ``tables``      — GitHub-style pipe tables (ubiquitous in the
                        report-style output agents produce).
    * ``fenced_code`` — ```` ``` ```` fenced code blocks.
    * ``sane_lists``  — predictable ordered / unordered list nesting.
    * ``nl2br``       — single newlines become ``<br>`` so intentional line
                        breaks survive (chat-style Markdown rarely uses the
                        two-trailing-spaces hard-break convention).

    Block-level raw HTML embedded in the Markdown is passed through, so an
    agent can still hand-author richer markup inline.  Tags are intentionally
    not stripped here because the downstream renderer (:func:`render_text_to_pdf`)
    runs the HTML in an isolated headless Chromium with **JavaScript disabled
    and all external network requests blocked**, so passed-through ``<script>``
    never executes and embedded external resources cannot be fetched.
    """
    import markdown as _markdown

    return _markdown.markdown(
        content,
        extensions=["tables", "fenced_code", "sane_lists", "nl2br"],
        output_format="html",
    )


def wrap_content_as_html(content: str, title: str | None = None) -> str:
    """Wrap arbitrary text/markdown as a styled, printable HTML document.

    The wrapper:

    * Sets ``charset=utf-8`` and the standard ``viewport`` meta so the
      headless Chromium renderer treats the document as a UTF-8 web
      page (otherwise non-ASCII characters render as mojibake when the
      ``Content-Type`` is inferred from the filename rather than the
      HTML head).
    * Injects ``PDF_DEFAULT_STYLESHEET`` so any reasonable
      Markdown-derived HTML — headings, lists, tables, code blocks,
      blockquotes — renders cleanly without requiring the agent to
      author its own CSS.
    * Renders Markdown content to HTML (via :func:`render_markdown_to_html`)
      so agent reports become properly formatted documents.  Falls back to a
      ``<pre>`` block only if Markdown rendering raises, so a file is always
      produced.
    """
    import html as _html

    safe_title = _html.escape(title or "Generated report")
    if looks_like_html(content):
        body_html = content
    else:
        try:
            body_html = render_markdown_to_html(content)
        except Exception:
            logger.warning(
                "Markdown rendering failed; falling back to <pre> block",
                exc_info=True,
            )
            body_html = f'<pre class="rendered-plaintext">{_html.escape(content)}</pre>'

    return (
        "<!DOCTYPE html>"
        "<html lang=\"en\">"
        "<head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{safe_title}</title>"
        f"<style>{PDF_DEFAULT_STYLESHEET}</style>"
        "</head>"
        f"<body>{body_html}</body>"
        "</html>"
    )


async def render_text_to_pdf(content: str, *, title: str | None = None) -> bytes:
    """Render text / HTML / Markdown content to a PDF byte string via Playwright.

    Each render uses a fresh ``new_context()`` so the PDF generation is
    isolated from any in-flight agent browser sessions — this runs on every
    request regardless of whether the agent is currently driving the browser
    tool.

    The content is agent/user-influenced, so the renderer is locked down to
    prevent server-side request forgery (SSRF) and hangs on slow resources:

    * **JavaScript is disabled** (``java_script_enabled=False``) so embedded
      ``<script>`` never executes.
    * **Every request whose URL is not an in-memory scheme (``data:`` /
      ``about:`` / ``blob:``) is aborted**, so an external
      ``<img src="http://internal-host/…">`` / ``<link>`` / ``<iframe>`` cannot
      reach the network (inline ``data:`` images still render).
    * The page only waits for ``domcontentloaded`` (not the full ``load``
      event) so a blocked/slow sub-resource cannot stall the render.

    Failure modes (missing chromium binary, render timeout, malformed HTML)
    raise a real exception that callers catch and surface as a friendly error
    so the agent can fall back to plain HTML / Markdown.
    """
    return await _render_html_to_pdf(wrap_content_as_html(content, title=title), margin=PDF_RENDER_MARGIN)


async def render_document_to_pdf(document: str, *, first_page_only: bool = False) -> bytes:
    """Render a complete HTML document (its own ``<style>`` and ``@page`` rules) to PDF.

    For a server-built layout such as the fleet datasheet (#9768 S4): the
    document is not wrapped, the page margins are the document's (``@page``),
    and *first_page_only* prints page 1 alone, so a layout that overflows is
    cut instead of growing a second page. Same lockdown as
    :func:`render_text_to_pdf`: JavaScript off, every network request aborted.
    """
    return await _render_html_to_pdf(document, margin="0", page_ranges="1" if first_page_only else "")


async def _render_html_to_pdf(html: str, *, margin: str, page_ranges: str = "") -> bytes:
    """The locked-down Playwright render shared by :func:`render_text_to_pdf` and :func:`render_document_to_pdf`."""
    from playwright.async_api import Route, async_playwright

    from app.services.headless_render import RENDER_CHROMIUM_ARGS, RENDER_SERVICE_WORKERS

    async def _block_external(route: Route) -> None:
        # Allow only in-memory schemes (data: inline content, about: the
        # document itself, blob: in-memory objects) — abort every real network
        # request to neutralise SSRF / external fetches.
        url = route.request.url
        if url.startswith(("data:", "about:", "blob:")):
            await route.continue_()
        else:
            await route.abort()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=list(RENDER_CHROMIUM_ARGS))
        try:
            context = await browser.new_context(
                java_script_enabled=False, service_workers=RENDER_SERVICE_WORKERS
            )
            try:
                await context.route("**/*", _block_external)
                page = await context.new_page()
                await page.set_content(html, wait_until="domcontentloaded")
                pdf_bytes: bytes = await page.pdf(
                    format=PDF_RENDER_FORMAT,
                    margin={"top": margin, "bottom": margin, "left": margin, "right": margin},
                    print_background=True,
                    page_ranges=page_ranges,
                )
                return pdf_bytes
            finally:
                await context.close()
        finally:
            await browser.close()
