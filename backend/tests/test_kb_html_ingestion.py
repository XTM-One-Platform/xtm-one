"""Rendered-text ingestion of HTML documents, a per-knowledge-base opt-in.

A knowledge base embeds an HTML document as its stored markup by default
(``html_ingestion = "source"``, byte-identical to before the setting existed).
``rendered`` embeds the page as ``attachment_extract.extract_html_document``
reads it instead - title, data summaries, page text, and none of the pointers
an attachment reading gives to conversation-file tools - while the stored
source stays what ``get_knowledge_document`` returns. The choice is made at
processing time by ``kb_embedding_text.resolve_embedding_text``, so changing
the setting re-embeds nothing until a document is processed again.
"""

from __future__ import annotations

import inspect
import json
import uuid
from types import SimpleNamespace
from typing import get_args
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import deps
from app.api.v1 import knowledge_bases as kb_api
from app.api.v1.schemas.knowledge_base import (
    CreateKnowledgeBaseRequest,
    HtmlIngestion,
    KBExportKnowledgeBase,
    KnowledgeBaseResponse,
    UpdateKnowledgeBaseRequest,
)
from app.core.database import get_db
from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument
from app.services import attachment_extract, document_processor, kb_embedding_text
from app.services.chunking import chunk_text, extract_text_from_file
from app.services.kb_embedding_text import (
    embedding_text_for,
    embedding_text_for_async,
    is_html_document,
    resolve_embedding_text,
    resolve_embedding_text_async,
)

SENTENCE = "The migration to the new cluster finishes in October."

PAGE = (
    "<!DOCTYPE html><html><head><title>Q3 plan</title>"
    "<style>.x{color:red;font-weight:700}</style></head><body>"
    '<svg viewBox="0 0 20 20"><path d="M0 0 L10 10 L20 0 Z M5 5 L15 15 Z"/></svg>'
    f"<p>{SENTENCE}</p>"
    '<script type="application/json">{"milestones": [{"name": "cutover", "date": "2026-10-12"}]}</script>'
    "</body></html>"
)

#: What only a reader of a conversation FILE can act on, or markup.
_NOT_INDEXED = (
    "query_file_json",
    "read_file_content",
    "code_interpreter",
    "Rendered from",
    "not shown",
    "<style",
    "<svg",
    "<script",
)


# ---------------------------------------------------------------------------
# The default is byte-identical to before the setting
# ---------------------------------------------------------------------------


def test_a_source_kb_embeds_exactly_what_extract_text_from_file_returns():
    stored = extract_text_from_file(PAGE.encode("utf-8"), "plan.html")

    # The extractor still keeps the SOURCE of an HTML document...
    assert stored == PAGE
    # ...and a ``source`` knowledge base embeds it unchanged.
    assert embedding_text_for(stored, "plan.html", "source") is stored


@pytest.mark.parametrize(
    ("content", "filename"),
    [
        ("Plain notes.\nSecond line.", "notes.txt"),
        ("# Runbook\n\n- step one\n- step two\n\n```html\n<div>x</div>\n```", "runbook.md"),
        ('{"rows": [{"id": 1, "body": "<p>quoted</p>"}]}', "export.json"),
        (PAGE, "plan.html"),
        (PAGE, "https://wiki.example.com/plan.html"),
    ],
)
@pytest.mark.parametrize("mode", [None, "source"])
def test_a_source_kb_chunks_every_document_exactly_as_before(content, filename, mode):
    """Every knowledge base that did not opt in - one created before the
    setting reads ``None`` until the default applies - chunks the stored
    content itself, so its chunks are the ones it had."""
    embedded = embedding_text_for(content, filename, mode)

    assert embedded is content
    assert chunk_text(embedded, chunk_size=512, chunk_overlap=128) == chunk_text(
        content, chunk_size=512, chunk_overlap=128
    )


@pytest.mark.parametrize("mode", [None, "", "SOURCE", "markdown"])
def test_an_unset_or_unknown_mode_reads_as_source(mode):
    assert embedding_text_for(PAGE, "plan.html", mode) is PAGE


# ---------------------------------------------------------------------------
# ``rendered``: the page text is embedded, the markup is not
# ---------------------------------------------------------------------------


def test_a_rendered_kb_embeds_the_page_text_and_its_data_not_the_markup():
    text = embedding_text_for(PAGE, "plan.html", "rendered")

    assert text.startswith("# Q3 plan")
    assert SENTENCE in text
    assert "cutover" in text  # the embedded data is summarised, not dropped
    for markup in ("<path", "M0 0 L10", "color:red", "<p>", "<!DOCTYPE"):
        assert markup not in text


def test_a_rendered_kb_names_no_attachment_tool_even_where_a_data_summary_is_cut(monkeypatch):
    """A search hit is served to agents that cannot open a KB document with
    ``query_file_json`` or ``read_file_content``: the attachment reading's
    pointers, its account of what was left out and its hint on a cut summary
    must not be indexed."""
    monkeypatch.setattr("app.services.attachment_extract.JSON_BLOCK_SUMMARY_MAX_CHARS", 300)
    records = json.dumps({f"field_{i}": f"value {i}" for i in range(80)})
    page = PAGE.replace("</body>", f'<script type="application/json">{records}</script></body>')

    text = embedding_text_for(page, "plan.html", "rendered")

    assert "summary cut at" in text  # the second block's summary did hit its bound
    for leaked in _NOT_INDEXED:
        assert leaked not in text


def test_a_search_for_a_sentence_matches_and_a_search_for_markup_does_not():
    """What a search can hit is the chunk text, so the property is pinned on
    the chunks the processor would embed."""
    rendered_chunks = chunk_text(embedding_text_for(PAGE, "plan.html", "rendered"), chunk_size=512, chunk_overlap=128)
    source_chunks = chunk_text(embedding_text_for(PAGE, "plan.html", "source"), chunk_size=512, chunk_overlap=128)

    assert any(SENTENCE in chunk for chunk in rendered_chunks)
    assert not any("<path" in chunk or "M0 0 L10" in chunk for chunk in rendered_chunks)
    assert any("<path" in chunk for chunk in source_chunks)


def test_text_that_is_not_html_is_embedded_as_is_even_in_a_rendered_kb():
    """A crawl or a sync source often stores Markdown under an ``.html`` URL;
    it is never re-rendered."""
    markdown = "# Plan\n\nThe cutover is on **12 October**; see the runbook for details."

    assert embedding_text_for(markdown, "https://wiki.example.com/plan.html", "rendered") is markdown
    assert embedding_text_for(markdown, "plan.md", "rendered") is markdown


@pytest.mark.parametrize(
    "filename",
    ["emails.json", "export.jsonl", "rows.csv", "guide.md", "notes.txt", "notes.text", "template.js", "build.py"],
)
def test_a_data_text_or_code_file_quoting_html_is_never_rendered(filename):
    """Data, text and code files keep their source whatever they quote near
    their start: a JSON export of e-mails, a Markdown page about HTML, a
    script that builds markup."""
    quoting = f'{{"body": "<!DOCTYPE html><html><body><p>{SENTENCE}</p></body></html>"}}'

    assert not is_html_document(quoting, filename)
    assert embedding_text_for(quoting, filename, "rendered") is quoting
    # Under an HTML file name a fragment is a page; text quoting one still is not.
    assert is_html_document(FRAGMENT, "page.html")
    assert not is_html_document(quoting, "page.html")


def test_text_that_quotes_a_page_is_not_a_page_whatever_its_name():
    """Without a file type to go by, the content decides, and a page STARTS as
    one: a note showing a page's markup, or a JSON value holding a page, is
    text. Rendered, the note would lose the tags it quotes and have its lines
    folded into one paragraph."""
    note = (
        "Checklist for the landing page\n\n"
        "1. Start every page with the doctype:\n\n"
        '```html\n<!DOCTYPE html>\n<html lang="en">\n<body>...</body>\n</html>\n```\n\n'
        "2. Keep the hero image under 200 KB."
    )
    quoting = f'{{"body": "<!DOCTYPE html><html><body><p>{SENTENCE}</p></body></html>"}}'

    for text in (note, quoting):
        assert not is_html_document(text, "Landing page checklist")
        assert embedding_text_for(text, "Landing page checklist", "rendered") is text


@pytest.mark.parametrize(
    "prologue",
    [
        "",
        "\ufeff",
        "\n\n  ",
        "<!-- saved from url=(0014)about:internet -->\n",
        '<?xml version="1.0" encoding="UTF-8"?>\n',
    ],
)
def test_a_page_is_recognised_after_what_may_come_before_its_first_tag(prologue):
    page = prologue + PAGE

    assert is_html_document(page, "Q3 plan")
    assert SENTENCE in embedding_text_for(page, "Q3 plan", "rendered")


@pytest.mark.parametrize(
    "filename",
    [
        "https://wiki.example.com/export.json?download=1",
        "https://wiki.example.com/readme.md#install",
        # urlsplit refuses this host: the name is read as written, never a crash.
        "https://[wiki.example.com/export.json",
    ],
)
def test_a_whole_page_under_a_data_or_text_url_name_is_never_rendered(filename):
    assert not is_html_document(PAGE, filename)
    assert embedding_text_for(PAGE, filename, "rendered") is PAGE


@pytest.mark.parametrize(
    ("filename", "renders"),
    [
        ("https://example.com/export.json?download=1", False),
        ("https://example.com/notes.md#top", False),
        ("https://example.com/page.html?download=1", True),
        ("https://example.com/page.htm#section-2", True),
    ],
)
def test_a_url_file_name_is_typed_by_its_path(filename, renders):
    """A crawl or a sync source names a document after its URL: the type is
    read from the path, never from a query string or a fragment."""
    quoting = f'{{"body": "<!DOCTYPE html><html><body><p>{SENTENCE}</p></body></html>"}}'
    content = FRAGMENT if renders else quoting

    assert is_html_document(content, filename) is renders
    # A title that merely contains a "?" is not a URL: its name is read as written.
    assert not is_html_document(quoting, "Why? guide.md")


def test_every_source_text_format_but_html_is_a_file_type_never_rendered():
    """The formats ``read_file_content`` treats as source text are the ones a
    rendering knowledge base leaves as they are, HTML itself excepted."""
    from app.services.builtin_tools.media import _TEXT_LIKE_EXTENSIONS

    source_text = {ext.lstrip(".") for ext in _TEXT_LIKE_EXTENSIONS} - {"html", "htm", "xhtml"}

    assert source_text <= kb_embedding_text._NOT_HTML_EXTENSIONS
    assert not {"html", "htm", "xhtml"} & kb_embedding_text._NOT_HTML_EXTENSIONS


@pytest.mark.parametrize("filename", ["https://docs.example.com/guide.html", "docs/guide.html", "guide.htm"])
def test_converted_text_quoting_tags_under_an_html_name_is_never_rendered(filename):
    """A crawl or a sync source stores the Markdown it converted a page to
    under the page's ``.html`` URL: it starts with text, and the tags it quotes
    (a code sample, a key) must survive, not be read as markup."""
    markdown = (
        "# Install guide\n\nAdd the widget to your page:\n\n"
        '```html\n<div class="widget" data-id="42"></div>\n```\n\n'
        "Then press <kbd>Ctrl</kbd>+<kbd>S</kbd> to save.\n"
    )

    assert not is_html_document(markdown, filename)
    assert embedding_text_for(markdown, filename, "rendered") is markdown
    # A fragment starts with a tag, after the same prologue a page may have.
    assert is_html_document("\n<!-- partial -->\n" + FRAGMENT, filename)


def test_an_html_fragment_is_rendered_only_when_the_file_is_named_as_html():
    fragment = f"<div><p>{SENTENCE}</p><p>Owner: platform team, reviewed weekly.</p></div>"

    assert is_html_document(fragment, "notes.html")
    assert not is_html_document(fragment, "notes.md")
    assert "<div>" not in embedding_text_for(fragment, "notes.html", "rendered")
    assert embedding_text_for(fragment, "notes.md", "rendered") is fragment


def test_a_rendering_error_embeds_the_source_rather_than_failing(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr("app.services.attachment_extract.extract_html_document", _boom)

    assert embedding_text_for(PAGE, "plan.html", "rendered") is PAGE


def test_a_page_without_text_or_data_embeds_the_source_not_the_placeholder():
    """The attachment rendering writes a placeholder line for an empty page;
    the choice is made on the parsed page, so that line is never embedded."""
    empty = "<!DOCTYPE html><html><head><style>p{margin:0}</style></head><body><div></div></body></html>"
    broken_data = '<!DOCTYPE html><html><body><script type="application/json">{not json</script></body></html>'

    assert embedding_text_for(empty, "blank.html", "rendered") is empty
    assert embedding_text_for(broken_data, "blank.html", "rendered") is broken_data


def test_a_page_holding_only_data_is_still_rendered():
    data_only = (
        '<!DOCTYPE html><html><body><script type="application/json">'
        '{"milestones": [{"name": "cutover", "date": "2026-10-12"}]}</script></body></html>'
    )

    text = embedding_text_for(data_only, "data.html", "rendered")

    assert "cutover" in text and '<script type="application/json">' not in text
    assert "(the page has no visible text)" not in text


#: A page whose data holds half a UTF-16 pair: six ASCII characters in the
#: stored source (``\\ud83d``), one lone surrogate once the JSON is parsed.
SURROGATE_PAGE = PAGE.replace(
    "</body>",
    '<script type="application/json">{"owner": "platform \\ud83d team"}</script>'
    + '<script type="application/json">'
    + json.dumps([{"status": "clo\ud83dsed", "n": i} for i in range(40)])
    + "</script></body>",
)


def test_the_rendered_reading_is_text_the_database_can_store():
    """The embedding call and the insert of every chunk refuse a string holding
    a lone surrogate, so the reading carries U+FFFD in its place, like every
    attachment rendering - a source that stored fine must not stop indexing
    once the knowledge base renders it."""
    SURROGATE_PAGE.encode("utf-8")

    resolved = resolve_embedding_text(SURROGATE_PAGE, "plan.html", "rendered")

    assert resolved.html_note == "as its rendered page text"
    resolved.text.encode("utf-8")
    assert "platform \ufffd team" in resolved.text


@pytest.mark.parametrize(
    ("bound", "value"),
    [("HTML_RENDER_MAX_CHARS", 120), ("HTML_TEXT_MAX_CHARS", 20)],
)
def test_a_page_the_reader_cannot_read_whole_embeds_its_source(monkeypatch, bound, value):
    """Past the reader's bounds the rest of a page would silently leave the
    index: such a page keeps its source, never indexes less than it."""
    page = PAGE.replace("</body>", "<p>A second paragraph, past the bound.</p></body>")
    monkeypatch.setattr(f"app.services.attachment_extract.{bound}", value)

    resolved = resolve_embedding_text(page, "plan.html", "rendered")

    assert resolved.text is page
    assert resolved.html_note == "from its HTML source: the page is too large to render whole"


def _recording_parser(monkeypatch) -> list[str]:
    """Record every page the reader parses, and parse it as it would."""
    parsed: list[str] = []
    real = attachment_extract.extract_html_document

    def _parse(html):
        parsed.append(html)
        return real(html)

    monkeypatch.setattr(attachment_extract, "extract_html_document", _parse)
    return parsed


def test_a_page_past_the_read_bound_keeps_its_source_without_being_parsed(monkeypatch):
    """Such a page keeps its source whatever it holds, so its length decides:
    tokenizing the part the reader reads only to discard it would be the whole
    cost of the page - seconds of the parse pool for 8 MB of markup."""
    parsed = _recording_parser(monkeypatch)
    monkeypatch.setattr(attachment_extract, "HTML_RENDER_MAX_CHARS", len(PAGE) - 1)

    resolved = resolve_embedding_text(PAGE, "plan.html", "rendered")

    assert resolved.text is PAGE
    assert resolved.html_note == "from its HTML source: the page is too large to render whole"
    assert parsed == []


@pytest.mark.asyncio
async def test_the_async_form_does_not_queue_a_page_past_the_read_bound(monkeypatch):
    calls = []

    async def _run_parse(fn, *args):
        calls.append(fn)
        return fn(*args)

    monkeypatch.setattr("app.core.parse_offload.run_parse", _run_parse)
    monkeypatch.setattr(attachment_extract, "HTML_RENDER_MAX_CHARS", len(PAGE) - 1)

    resolved = await resolve_embedding_text_async(PAGE, "plan.html", "rendered")

    assert resolved.text is PAGE
    assert resolved.html_note == "from its HTML source: the page is too large to render whole"
    assert calls == []


def test_a_page_is_rendered_once_per_content(monkeypatch):
    """The workset derives what a rendering knowledge base embedded on every
    search and every chunk read: the rendered reading is memoised per content,
    whatever name it is read under, and a changed page is rendered afresh."""
    parsed = _recording_parser(monkeypatch)

    first = resolve_embedding_text(PAGE, "plan.html", "rendered")
    again = resolve_embedding_text(PAGE, "Q3 plan", "rendered")
    changed = resolve_embedding_text(PAGE.replace("October", "November"), "plan.html", "rendered")

    assert again == first
    assert first.html_note == "as its rendered page text" and SENTENCE in first.text
    assert "November" in changed.text
    assert len(parsed) == 2


def test_data_past_the_reader_bounds_does_not_cost_the_page_its_rendering(monkeypatch):
    """The data is a summary within the reader's bounds, like the rows a
    summary does not show; only a page whose TEXT could not be kept whole
    falls back to its source."""
    monkeypatch.setattr("app.services.attachment_extract.MAX_EMBEDDED_BLOCKS", 1)
    page = PAGE.replace("</body>", '<script type="application/json">{"owner": "platform team"}</script></body>')

    resolved = resolve_embedding_text(page, "plan.html", "rendered")

    assert resolved.html_note == "as its rendered page text"
    assert SENTENCE in resolved.text and "cutover" in resolved.text
    assert "platform team" not in resolved.text


def test_the_resolution_says_which_reading_it_embedded(monkeypatch):
    empty = "<!DOCTYPE html><html><body></body></html>"

    assert resolve_embedding_text(PAGE, "plan.html", "source").html_note is None
    assert resolve_embedding_text("plain notes", "notes.txt", "rendered").html_note is None
    assert resolve_embedding_text(PAGE, "plan.html", "rendered").html_note == "as its rendered page text"
    assert (
        resolve_embedding_text(empty, "blank.html", "rendered").html_note
        == "from its HTML source: the page has no visible text or data"
    )

    def _boom(*_args, **_kwargs):
        raise RuntimeError("parser exploded")

    # The page is memoised by now: an error is what a page not rendered yet meets.
    attachment_extract.clear_render_cache()
    monkeypatch.setattr("app.services.attachment_extract.extract_html_document", _boom)
    assert (
        resolve_embedding_text(PAGE, "plan.html", "rendered").html_note
        == "from its HTML source: the page could not be rendered"
    )


@pytest.mark.asyncio
async def test_the_async_form_renders_off_loop_and_skips_the_executor_for_source(monkeypatch):
    calls = []

    async def _run_parse(fn, *args):
        calls.append(fn)
        return fn(*args)

    monkeypatch.setattr("app.core.parse_offload.run_parse", _run_parse)

    assert await embedding_text_for_async(PAGE, "plan.html", "source") is PAGE
    assert calls == []
    rendered = await embedding_text_for_async(PAGE, "plan.html", "rendered")
    assert SENTENCE in rendered and "<path" not in rendered
    assert calls == [resolve_embedding_text]


# ---------------------------------------------------------------------------
# One choice point for every place that chunks a knowledge-base document
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("module", "function"),
    [
        ("app.services.document_processor", "_process_document_impl"),
        ("app.services.builtin_tools.kb", "build_kb_store_tool"),
        ("app.services.workset_read", "chunks_for_document"),
    ],
)
def test_every_kb_chunking_path_goes_through_the_resolution(module, function):
    """Upload, crawl, sync, update and reprocess all end in the document
    processor; ``store_knowledge`` chunks inline; the workset re-chunks a
    stored document to recover chunk ordinals. A path that chunked ``content``
    directly would embed (or address) something else than the others."""
    import importlib

    src = inspect.getsource(getattr(importlib.import_module(module), function))

    assert "embedding_text_for_async(" in src or "resolve_embedding_text_async(" in src


class _Result:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _NoAutoflush:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _ProcessorSession:
    """The four statements of a successful ``_process_document_impl`` run: the
    document, its knowledge base, the re-lock and the swap's delete."""

    def __init__(self, doc, kb):
        self._results = [_Result(doc), _Result(kb), _Result(doc.id), _Result()]
        self.added: list = []
        self.no_autoflush = _NoAutoflush()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, _statement):
        return self._results.pop(0)

    async def commit(self):
        pass

    async def rollback(self):
        pass

    def add(self, value):
        self.added.append(value)

    def expunge(self, _value):
        pass

    async def get(self, _model, _key):
        # The person the document is embedded for (``bind_llm_caller``): none here.
        return None


async def _process(monkeypatch, html_ingestion, content=PAGE):
    kb = KnowledgeBase(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="KB",
        chunk_size=512,
        chunk_overlap=128,
        html_ingestion=html_ingestion,
    )
    doc = KnowledgeDocument(
        id=uuid.uuid4(),
        knowledge_base_id=kb.id,
        filename="plan.html",
        source_type="upload",
        content=content,
        status="pending",
    )
    session = _ProcessorSession(doc, kb)
    provider = SimpleNamespace(
        embed=AsyncMock(return_value=[0.1, 0.2, 0.3]), provider_name="test", embedding_dimensions=None
    )
    activity = AsyncMock()
    monkeypatch.setattr(document_processor, "async_session", lambda: session)
    monkeypatch.setattr(document_processor, "log_kb_activity", activity)
    monkeypatch.setattr(document_processor, "_recount_kb_stats", AsyncMock())
    monkeypatch.setattr(
        "app.llm.router.get_embedding_provider",
        AsyncMock(return_value=(provider, "embed-model", str(uuid.uuid4()))),
    )

    chunks = await document_processor._process_document_impl(str(doc.id))

    assert chunks > 0 and doc.status == "ready"
    assert activity.await_args.args[2] == "embedding_completed"
    embedded = "\n".join(record.content for record in session.added)
    return embedded, chunks, activity.await_args.kwargs["details"]


@pytest.mark.asyncio
async def test_the_processor_embeds_the_rendered_page_and_says_so(monkeypatch):
    embedded, chunks, details = await _process(monkeypatch, "rendered")

    assert SENTENCE in embedded and "<path" not in embedded
    assert details == f"Embedded 'plan.html' — {chunks} chunk(s), as its rendered page text"


@pytest.mark.asyncio
async def test_the_processor_stores_rendered_chunks_the_database_accepts(monkeypatch):
    embedded, chunks, details = await _process(monkeypatch, "rendered", content=SURROGATE_PAGE)

    embedded.encode("utf-8")
    assert "platform \ufffd team" in embedded
    assert details == f"Embedded 'plan.html' — {chunks} chunk(s), as its rendered page text"


@pytest.mark.asyncio
async def test_the_processor_activity_of_a_source_kb_is_unchanged(monkeypatch):
    embedded, chunks, details = await _process(monkeypatch, "source")

    assert "<path" in embedded
    assert details == f"Embedded 'plan.html' — {chunks} chunk(s)"


class _WorksetDb:
    """The document row, then the stored chunk texts, as ``chunks_for_document`` reads them.

    The row ends with the knowledge base's ``describe_figures`` and the
    document's ``figure_reading``: off and none for an HTML page."""

    def __init__(self, row, embedded):
        self.row = row
        self.embedded = embedded

    async def execute(self, statement):
        if "embeddings" in str(statement):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(self.embedded)))
        return SimpleNamespace(first=lambda: self.row)


def _embeddable(chunks):
    from app.services.workset_read import _MIN_EMBEDDED_CHUNK_CHARS

    return [c for c in chunks if len(c.strip()) >= _MIN_EMBEDDED_CHUNK_CHARS]


def _chunks(mode):
    return chunk_text(embedding_text_for(PAGE, "plan.html", mode), chunk_size=512, chunk_overlap=128)


@pytest.mark.asyncio
@pytest.mark.parametrize(("current", "embedded_under"), [("rendered", "rendered"), ("source", "source")])
async def test_workset_addresses_the_chunks_of_the_current_mode(current, embedded_under):
    from app.services.workset_read import chunks_for_document

    db = _WorksetDb((PAGE, "ready", 512, 128, "plan.html", current, False, None), _embeddable(_chunks(embedded_under)))

    resolved = await chunks_for_document(db, uuid.uuid4())

    assert resolved.exact is True
    assert resolved.chunks == _chunks(current)


@pytest.mark.asyncio
@pytest.mark.parametrize(("current", "embedded_under"), [("source", "rendered"), ("rendered", "source")])
async def test_workset_follows_the_stored_representation_after_a_toggle(current, embedded_under):
    """Toggling re-embeds nothing, so a document embedded under the other mode
    must still map its search hits to ordinals."""
    from app.services.workset_read import chunks_for_document

    db = _WorksetDb((PAGE, "ready", 512, 128, "plan.html", current, False, None), _embeddable(_chunks(embedded_under)))

    resolved = await chunks_for_document(db, uuid.uuid4())

    assert resolved.exact is True
    assert resolved.chunks == _chunks(embedded_under)


@pytest.mark.asyncio
async def test_workset_is_inexact_when_neither_representation_matches():
    from app.services.workset_read import chunks_for_document

    db = _WorksetDb((PAGE, "ready", 512, 128, "plan.html", "rendered", False, None), ["an unrelated chunk of stored text"])

    resolved = await chunks_for_document(db, uuid.uuid4())

    assert resolved.exact is False
    assert resolved.chunks == _chunks("rendered")


@pytest.mark.asyncio
async def test_workset_does_not_render_a_page_whose_stored_chunks_are_cut_from_its_source(monkeypatch):
    """A ``source`` knowledge base (every one that existed before the setting)
    whose HTML document was embedded only in part - a chunk the provider
    failed on - no longer matches its chunk list, but the chunks it holds were
    cut from the source: the rendered reading cannot match them, so the page
    is not rendered to find out, and the addressing stays inexact."""
    from app.services.workset_read import chunks_for_document

    source_chunks = chunk_text(PAGE, chunk_size=64, chunk_overlap=0)
    assert len(_embeddable(source_chunks)) >= 2
    parsed = _recording_parser(monkeypatch)
    db = _WorksetDb((PAGE, "ready", 64, 0, "plan.html", "source", False, None), _embeddable(source_chunks)[1:])

    resolved = await chunks_for_document(db, uuid.uuid4())

    assert resolved.exact is False
    assert resolved.chunks == source_chunks
    assert parsed == []


def test_the_model_defaults_to_source():
    column = KnowledgeBase.__table__.c.html_ingestion
    assert column.default.arg == "source"
    assert column.nullable is False
    assert "source" in str(column.server_default.arg)


# ---------------------------------------------------------------------------
# API: schemas, export, update
# ---------------------------------------------------------------------------


def test_the_api_accepts_only_the_two_modes():
    assert CreateKnowledgeBaseRequest(name="kb").html_ingestion == "source"
    assert UpdateKnowledgeBaseRequest().html_ingestion is None
    assert UpdateKnowledgeBaseRequest(html_ingestion="rendered").html_ingestion == "rendered"
    with pytest.raises(ValidationError):
        UpdateKnowledgeBaseRequest(html_ingestion="markdown")
    assert set(get_args(HtmlIngestion)) == set(kb_embedding_text.HTML_INGESTION_MODES)


def test_a_package_exported_before_the_setting_imports_as_source():
    legacy = KBExportKnowledgeBase.model_validate({"name": "kb", "chunk_size": 512, "chunk_overlap": 128})

    assert legacy.html_ingestion == "source"


USER_ID = uuid.uuid4()
KB_ID = uuid.uuid4()


def _kb(html_ingestion="source", **overrides):
    fields = {
        "id": KB_ID,
        "user_id": USER_ID,
        "name": "Plans",
        "description": None,
        "tags": [],
        "is_builtin": False,
        "is_system_managed": False,
        "is_read_only": False,
        "is_company_managed": False,
        "html_ingestion": html_ingestion,
        "embedding_model": "text-embedding-3-large",
        "document_count": 4,
        "shared_group_ids": None,
        "shared_group_permissions": None,
        "shared_user_ids": None,
        "shared_user_permissions": None,
        "access_restricted": False,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


class _Db:
    def __init__(self, events):
        self.events = events

    async def execute(self, _statement):
        return SimpleNamespace(scalar_one_or_none=lambda: "Owner")

    async def commit(self):
        self.events.append("commit")


def _client(monkeypatch, kb, audits, enqueue, *, group_ids=(), manage_gate=False, events=None):
    """The update route over fakes. ``manage_gate`` keeps the real manage
    check (company-managed: admin only; a share: config tier); ``events``
    records the audit, the commits and the response in the order they run."""
    events = [] if events is None else events

    async def _noop(*_args, **_kwargs):
        return None

    async def _get_kb(*_args, **_kwargs):
        return kb

    async def _groups(*_args, **_kwargs):
        return list(group_ids)

    async def _audit(_db, **kwargs):
        events.append("audit")
        audits.append(kwargs)

    async def _response(kb_obj, _db, **_kwargs):
        events.append("response")
        return KnowledgeBaseResponse(
            id=str(kb_obj.id),
            name=kb_obj.name,
            embedding_model=kb_obj.embedding_model,
            html_ingestion=kb_obj.html_ingestion,
            document_count=kb_obj.document_count,
            chunk_count=0,
            created_at="2026-09-27T00:00:00Z",
            updated_at="2026-09-27T00:00:00Z",
        )

    monkeypatch.setattr(kb_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(kb_api, "get_user_knowledge_base", _get_kb)
    monkeypatch.setattr(kb_api, "assert_knowledge_base_not_package_origin", _noop)
    if not manage_gate:
        monkeypatch.setattr(kb_api, "_assert_kb_manageable", _noop)
    monkeypatch.setattr(kb_api, "handle_company_managed_toggle", _noop)
    monkeypatch.setattr(kb_api, "apply_user_share_fields", _noop)
    monkeypatch.setattr(kb_api, "apply_owner_transfer", _noop)
    monkeypatch.setattr(kb_api, "log_audit", _audit)
    monkeypatch.setattr(kb_api, "_kb_to_response", _response)
    monkeypatch.setattr(kb_api, "enqueue_document_processing", enqueue)
    monkeypatch.setattr(kb_api, "enqueue_documents", enqueue)
    monkeypatch.setattr(kb_api, "enqueue_reembedding", enqueue)

    app = FastAPI()
    app.include_router(kb_api.router, prefix="/api/v1/knowledge-bases")
    app.dependency_overrides[deps.get_current_user] = lambda: SimpleNamespace(
        id=USER_ID, is_admin=False, role="user", full_name="Owner"
    )

    async def _get_db():
        yield _Db(events)

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[deps.require_ai_enabled] = lambda: None
    return TestClient(app)


def test_switching_to_rendered_is_saved_and_audited_and_re_embeds_nothing(monkeypatch):
    kb = _kb()
    audits: list[dict] = []
    events: list[str] = []
    enqueue = AsyncMock(return_value=True)

    with _client(monkeypatch, kb, audits, enqueue, events=events) as client:
        resp = client.put(f"/api/v1/knowledge-bases/{KB_ID}", json={"html_ingestion": "rendered"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["html_ingestion"] == "rendered"
    assert kb.html_ingestion == "rendered"
    assert audits and audits[-1]["details"]["html_ingestion"] == "rendered"
    enqueue.assert_not_awaited()
    # Committed before the answer, not on ``get_db`` teardown: a refetch or a
    # Re-embed right after must read the new setting.
    assert events[:3] == ["audit", "commit", "response"]


def test_an_update_that_omits_the_setting_leaves_it_alone(monkeypatch):
    kb = _kb("rendered")

    with _client(monkeypatch, kb, [], AsyncMock()) as client:
        resp = client.put(f"/api/v1/knowledge-bases/{KB_ID}", json={"name": "Plans v2"})

    assert resp.status_code == 200, resp.text
    assert kb.html_ingestion == "rendered"


def test_an_unknown_mode_is_refused(monkeypatch):
    kb = _kb()

    with _client(monkeypatch, kb, [], AsyncMock()) as client:
        resp = client.put(f"/api/v1/knowledge-bases/{KB_ID}", json={"html_ingestion": "markdown"})

    assert resp.status_code == 422
    assert kb.html_ingestion == "source"


class _DocDb:
    async def execute(self, _statement):
        return None

    async def commit(self):
        pass

    async def refresh(self, _obj):
        pass


FRAGMENT = f"<div><p>{SENTENCE}</p></div>"


@pytest.mark.parametrize(
    ("mode", "content", "old_name", "new_name", "re_embeds"),
    [
        # The name decides whether a fragment reads as a page: both directions re-embed.
        ("rendered", FRAGMENT, "notes.txt", "notes.html", True),
        ("rendered", FRAGMENT, "notes.html", "notes.txt", True),
        # A rename that keeps the reading leaves the vectors alone...
        ("rendered", PAGE, "plan.html", "plan-v2.html", False),
        # ...as does any rename in a knowledge base that embeds the source.
        ("source", FRAGMENT, "notes.txt", "notes.html", False),
    ],
)
def test_a_rename_re_embeds_only_when_it_changes_how_a_rendering_kb_reads_the_document(
    monkeypatch, mode, content, old_name, new_name, re_embeds
):
    from datetime import UTC, datetime

    kb = _kb(mode, chunk_count=2)
    doc = SimpleNamespace(
        id=uuid.uuid4(),
        knowledge_base_id=KB_ID,
        filename=old_name,
        source_type="text",
        source_url=None,
        status="ready",
        chunk_count=2,
        error_message=None,
        content=content,
        content_hash="hash",
        embedding_retry_count=0,
        embedding_next_retry_at=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    enqueue = AsyncMock(return_value=True)

    async def _noop(*_args, **_kwargs):
        return None

    async def _get_kb(*_args, **_kwargs):
        return kb

    async def _get_doc(*_args, **_kwargs):
        return doc

    monkeypatch.setattr(kb_api, "get_user_knowledge_base", _get_kb)
    monkeypatch.setattr(kb_api, "assert_writable_knowledge_base", _noop)
    monkeypatch.setattr(kb_api, "_get_kb_document_or_404", _get_doc)
    monkeypatch.setattr(kb_api, "log_kb_activity", _noop)
    monkeypatch.setattr(kb_api, "enqueue_document_processing", enqueue)
    app = FastAPI()
    app.include_router(kb_api.router, prefix="/api/v1/knowledge-bases")
    app.dependency_overrides[deps.get_current_user] = lambda: SimpleNamespace(
        id=USER_ID, is_admin=False, role="user", full_name="Owner"
    )

    async def _get_db():
        yield _DocDb()

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[deps.require_ai_enabled] = lambda: None

    with TestClient(app) as client:
        resp = client.put(
            f"/api/v1/knowledge-bases/{KB_ID}/documents/{doc.id}",
            json={"content": content, "filename": new_name},
        )

    assert resp.status_code == 200, resp.text
    assert doc.filename == new_name
    assert (enqueue.await_count == 1) is re_embeds
    assert doc.status == ("pending" if re_embeds else "ready")


@pytest.mark.parametrize(
    ("overrides", "group_ids"),
    [
        # Company-managed: admins only.
        ({"is_company_managed": True, "user_id": None}, ()),
        # Shared with the caller's group at the read-only tier.
        (
            {"user_id": uuid.uuid4(), "shared_group_ids": ["g1"], "shared_group_permissions": {"g1": "read_only"}},
            ("g1",),
        ),
    ],
)
def test_a_caller_who_cannot_manage_the_kb_cannot_switch_it(monkeypatch, overrides, group_ids):
    kb = _kb(**overrides)
    audits: list[dict] = []

    with _client(monkeypatch, kb, audits, AsyncMock(), group_ids=group_ids, manage_gate=True) as client:
        resp = client.put(f"/api/v1/knowledge-bases/{KB_ID}", json={"html_ingestion": "rendered"})

    assert resp.status_code == 403, resp.text
    assert kb.html_ingestion == "source"
    assert audits == []
