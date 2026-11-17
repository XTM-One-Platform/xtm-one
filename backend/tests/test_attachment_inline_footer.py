"""What the model is shown of an attachment its provider cannot take natively.

The provider fallback inlines an attachment capped at 80,000 characters. One
whose plain extraction fits the cap is inlined as that extraction, whole, as it
always was. Past the cap an HTML file used to be a prefix of its raw source and
the footer named the size and nothing else - the model's way to the rest was
``read_file_content`` in 50k windows, and the 25 Sep 2026 turn paged a 558k
model for 97 iterations. Now such a file is rendered: the embedded data is
summarised FIRST, a large CSV leads with a digest of the whole file, and the
footer names the cut and gives the ways forward with an offset that means the
same characters in the rendered view and the exact path of the sandbox mount.
"""

from __future__ import annotations

import json

import pytest

from app.llm import file_resolver
from app.llm.base import FileRef
from app.services import attachment_extract as ax
from app.services.builtin_tools import build_code_interpreter_tool
from app.services.chunking import extract_text_from_file
from app.services.code_interpreter import mount_path_hint
from app.tools.base import offering_tools


@pytest.fixture(autouse=True)
def _fresh_render_cache():
    ax.clear_render_cache()
    yield
    ax.clear_render_cache()


def _big_html(rows: int = 3_000) -> bytes:
    data = json.dumps([{"name": f"Acc {i}", "segment": ["Fed", "State"][i % 2], "acv": 1000 + i} for i in range(rows)])
    return (
        "<html><head><title>Model</title><style>" + ".a{color:red} " * 6_000 + "</style>"
        f"<script>const ROWS = {data}; function draw() {{}}</script></head>"
        "<body><h1>Coverage</h1><p>Directional model.</p>"
        + "".join(f"<p>Narrative paragraph {i} about the plan.</p>" for i in range(2_500))
        + "</body></html>"
    ).encode("utf-8")


@pytest.mark.asyncio
async def test_a_large_html_attachment_leads_with_its_data_and_names_the_way_forward():
    ref = FileRef.from_bytes(_big_html(), "Model TD.html", "text/html")
    # A turn that offers the code interpreter (nothing else names it).
    with offering_tools(build_code_interpreter_tool()[0]):
        text = (await file_resolver._extract_fallback(ref)).text
    assert text.startswith(f"{file_resolver.file_label(ref)}\n\n# Model")
    # The digest of the embedded data survives the cap because it comes first.
    assert "// 3,000 records" in text
    assert "acv: number x3000: min 1,000, max 3,999" in text
    assert "# Coverage" in text
    assert "color:red" not in text and "const ROWS" not in text
    # The cut is named, and so are the two ways forward - and NOT paging.
    assert "[... truncated: the first 80,000 of" in text
    assert "do not page through the rest" in text
    assert f'read_file_content(file_id="{ref.file_id}", mode="extract_structured", offset=80000)' in text
    assert f"code_interpreter, where it is mounted at {mount_path_hint(ref.filename, ref.file_id)}" in text
    assert "total characters]" not in text, "the old footer is gone"


@pytest.mark.asyncio
async def test_a_large_csv_attachment_gets_a_whole_file_digest_ahead_of_its_text():
    rows = "\n".join(f"Acc {i},{['Fed', 'State'][i % 2]},{1000 + i}" for i in range(6_000))
    ref = FileRef.from_bytes(("name,segment,acv\n" + rows).encode(), "accounts.csv", "text/csv")
    text = (await file_resolver._extract_fallback(ref)).text
    # The whole file WAS read (6,000 rows), and the header says the column
    # statistics come from a sample of it.
    assert "a digest of the whole file (6,000 rows; column statistics over the first 5,000)" in text
    assert "CSV: 6,000 data rows, 3 columns" in text
    assert "acv: number x5000: min 1,000, max 5,999" in text
    assert "(statistics over the first 5,000 of 6,000 rows)" in text
    assert text.index("CSV: 6,000 data rows") < text.index("name,segment,acv\nAcc 0,Fed,1000")
    footer = text.rsplit("[... truncated", 1)[1]
    assert f'read_file_content(file_id="{ref.file_id}", mode="extract_structured", offset=80000)' in footer


@pytest.mark.asyncio
async def test_a_large_json_attachment_is_a_structured_summary():
    payload = {"accounts": [{"id": i, "acv": 500 + i} for i in range(5_000)], "version": 3}
    ref = FileRef.from_bytes(json.dumps(payload).encode(), "model.json", "application/json")
    text = (await file_resolver._extract_fallback(ref)).text
    assert "[JSON document," in text and "object with 2 keys" in text
    assert "// 5,000 records" in text
    assert "truncated" not in text, "the summary fits the cap"


@pytest.mark.asyncio
async def test_an_html_attachment_is_recognised_by_its_mime_type_alone():
    ref = FileRef.from_bytes(_big_html(), "download.bin", "application/xhtml+xml")
    text = (await file_resolver._extract_fallback(ref)).text
    assert "// 3,000 records" in text
    assert "color:red" not in text
    assert 'mode="extract_structured"' in text


@pytest.mark.asyncio
async def test_an_html_attachment_that_fits_the_cap_is_inlined_as_its_source():
    """A page whose source fits the fallback cap reaches the model as that
    source, whole - the markup a question about the page's code needs - as
    it always did; only a page the cap would cut is rendered."""
    page = (
        "<html><head><title>Invoice</title><style>.total{color:#0015A8}</style></head>"
        "<body><h1>Invoice</h1><script>const ROWS = [1, 2, 3];</script><p>Total due.</p></body></html>"
    )
    ref = FileRef.from_bytes(page.encode(), "invoice.html", "text/html")
    text = (await file_resolver._extract_fallback(ref)).text
    assert text == f"{file_resolver.file_label(ref)}\n\n{extract_text_from_file(page.encode(), 'invoice.html')}"
    assert "<style>" in text and "## Embedded data" not in text


@pytest.mark.asyncio
async def test_a_json_or_csv_attachment_that_fits_the_cap_is_inlined_verbatim():
    """Between the reader's 40,000-character summary bound and the 80,000-character
    fallback cap a JSON file is inlined verbatim and a CSV whole, with no
    digest taking the room of its last rows."""
    payload = json.dumps({"accounts": [{"id": i, "acv": 500 + i} for i in range(2_000)]}).encode()
    assert ax.JSON_VERBATIM_MAX_CHARS < len(payload) < file_resolver._MAX_FALLBACK_CHARS
    ref = FileRef.from_bytes(payload, "m.json", "application/json")
    text = (await file_resolver._extract_fallback(ref)).text
    assert text == f"{file_resolver.file_label(ref)}\n\n{extract_text_from_file(payload, 'm.json')}"
    assert "[JSON document," not in text
    rows = ("name,segment,acv\n" + "".join(f"Acc {i},{['Fed', 'State'][i % 2]},{1000 + i}\n" for i in range(2_500))).encode()
    assert ax.CSV_VERBATIM_MAX_CHARS < len(rows) < file_resolver._MAX_FALLBACK_CHARS
    ref = FileRef.from_bytes(rows, "a.csv", "text/csv")
    text = (await file_resolver._extract_fallback(ref)).text
    assert text == f"{file_resolver.file_label(ref)}\n\n{extract_text_from_file(rows, 'a.csv')}"
    assert "[CSV document," not in text


def test_the_plain_text_an_attachment_is_inlined_as_is_read_once_per_content(monkeypatch):
    """The inline fallback resolves the history's attachments on every
    provider request: the plain extraction it inlines is memoised like every
    other reading."""
    from app.services import chunking

    calls: list[bytes] = []

    def _extract(content: bytes, filename: str) -> str:
        calls.append(content)
        return extract_text_from_file(content, filename)

    monkeypatch.setattr(chunking, "extract_text_from_file", _extract)
    page = b"<html><body><p>Invoice total due.</p></body></html>"
    first = ax.extract_inline_attachment_text(page, "invoice.html", "text/html", False, 80_000)
    again = ax.extract_inline_attachment_text(page, "invoice.html", "text/html", False, 80_000)
    assert first == again == extract_text_from_file(page, "invoice.html")
    assert calls == [page]


def test_a_file_too_long_to_inline_is_read_once_past_the_memo_entry_bound(monkeypatch):
    """The memo keeps no entry over a quarter of its budget
    (4,000,000 characters) and an upload may be 40 MB, so the plain text of
    such a file was extracted again on every provider request only to learn
    it is too long to inline. The decision needs its length, which is kept."""
    from app.services import chunking

    calls: list[str] = []

    def _extract(content: bytes, filename: str) -> str:
        calls.append(filename)
        return extract_text_from_file(content, filename)

    monkeypatch.setattr(chunking, "extract_text_from_file", _extract)
    payload = json.dumps([{"id": i, "note": "x" * 200} for i in range(20_000)]).encode()
    assert len(payload) > ax._RENDER_CACHE_MAX_CHARS // 4, "past what the memo keeps as one entry"
    first = ax.extract_inline_attachment_text(payload, "m.json", "application/json", False, 80_000)
    again = ax.extract_inline_attachment_text(payload, "m.json", "application/json", False, 80_000)
    assert first == again and first.startswith("[JSON document,")
    assert calls == ["m.json"]


def test_the_plain_text_of_a_file_too_long_to_inline_leaves_the_memo_to_its_rendering(monkeypatch):
    """That text is never inlined, yet it was kept whole in the
    memo next to the rendering - two entries where the rendering alone had
    one - so a conversation holding a few large files pushed its own
    renderings out and every provider request rendered all of them again (an
    HTML page costs about a second per megabyte of markup). The memo budget is
    scaled down with the files."""
    monkeypatch.setattr(ax, "_RENDER_CACHE_MAX_CHARS", 40_000)
    rendered: list[bytes] = []
    real_render = ax._render

    def _render(kind, data, *args):
        rendered.append(data)
        return real_render(kind, data, *args)

    monkeypatch.setattr(ax, "_render", _render)
    files = [("name,acv\n" + "".join(f"Acc {n}-{i},{1000 + i}\n" for i in range(500))).encode() for n in range(3)]
    assert all(len(data) < ax._RENDER_CACHE_MAX_CHARS // 4 for data in files), "each rendering fits one entry"
    for _request in range(2):
        for data in files:
            text = ax.extract_inline_attachment_text(data, "a.csv", "text/csv", False, 1_000)
            assert text == data.decode()
    assert len(rendered) == 3, "each file rendered once, on the first request"


def test_a_file_too_long_to_inline_is_hashed_once_per_request_and_shares_its_rendering(monkeypatch):
    """The rendering's memo key reuses the digest the plain key took: a 40 MB
    upload is hashed once per provider request, as it was when only its
    rendering was looked up, and ``read_file_content(mode="extract_structured")``
    still finds the rendering the inline path memoised."""
    page = _big_html()
    rendered: list[bytes] = []
    real_render = ax._render

    def _render(kind, data, *args):
        rendered.append(data)
        return real_render(kind, data, *args)

    monkeypatch.setattr(ax, "_render", _render)
    inline = ax.extract_inline_attachment_text(page, "Model TD.html", "text/html", False, 80_000)
    assert inline.startswith("# Model")
    hashed: list[int] = []
    monkeypatch.setattr(ax, "hashlib", _CountingHashlib(hashed))
    assert ax.extract_inline_attachment_text(page, "Model TD.html", "text/html", False, 80_000) == inline
    assert hashed == [len(page)], "one digest per request"
    assert ax.extract_attachment_text(page, "Model TD.html", "text/html", False) == inline
    assert rendered == [page], "one rendering for both views"


class _CountingHashlib:
    """``hashlib`` whose BLAKE2b calls are recorded by the size they digest."""

    def __init__(self, calls: list[int]) -> None:
        import hashlib

        self._hashlib = hashlib
        self._calls = calls

    def __getattr__(self, name):
        return getattr(self._hashlib, name)

    def blake2b(self, data=b"", **kwargs):
        self._calls.append(len(data))
        return self._hashlib.blake2b(data, **kwargs)


def _big_csv_rows(rows: int = 220_000) -> bytes:
    return ("name,segment,acv\n" + "".join(f"Acc {i},{['Fed', 'State'][i % 2]},{1000 + i}\n" for i in range(rows))).encode()


@pytest.mark.asyncio
async def test_a_csv_past_the_memo_entry_bound_is_rendered_once_across_requests(monkeypatch):
    """A CSV rendering is its digest followed by the whole file text,
    so past the memo's 4,000,000-character entry bound it was decoded,
    sniffed and digested again on every provider request - to inline its
    first 80,000 characters. Those characters and the total are kept."""
    data = _big_csv_rows()
    rendered: list[int] = []
    real_render = ax._render

    def _render(kind, raw, *args):
        rendered.append(len(raw))
        return real_render(kind, raw, *args)

    monkeypatch.setattr(ax, "_render", _render)
    ref = FileRef.from_bytes(data, "accounts.csv", "text/csv")
    first = (await file_resolver._extract_fallback(ref)).text
    whole = ax.extract_attachment_text(data, "accounts.csv", "text/csv", False)
    assert len(whole) > ax._RENDER_CACHE_MAX_CHARS // 4, "past what the memo keeps as one entry"
    assert first.startswith(f"{file_resolver.file_label(ref)}\n\n{whole[: file_resolver._MAX_FALLBACK_CHARS]}\n\n")
    assert f"the first 80,000 of {len(whole):,} characters" in first
    rendered.clear()
    hashed: list[int] = []
    monkeypatch.setattr(ax, "hashlib", _CountingHashlib(hashed))
    assert (await file_resolver._extract_fallback(ref)).text == first
    assert rendered == [], "the second request renders nothing"
    assert hashed == [len(data)], "one digest per request"
    assert ax._inline_heads.weight <= file_resolver._MAX_FALLBACK_CHARS, "only what is inlined is kept"


@pytest.mark.asyncio
async def test_a_text_file_past_the_memo_entry_bound_is_extracted_once_across_requests(monkeypatch):
    """A non-structured file (a log, a text export) whose text passes
    the memo's entry bound was extracted again on every provider request to
    inline its first 80,000 characters."""
    from app.services import chunking

    data = "".join(f"line {i:07d} of the export log\n" for i in range(150_000)).encode()
    assert len(data) > ax._RENDER_CACHE_MAX_CHARS // 4
    calls: list[str] = []

    def _extract(content: bytes, filename: str) -> str:
        calls.append(filename)
        return extract_text_from_file(content, filename)

    monkeypatch.setattr(chunking, "extract_text_from_file", _extract)
    ref = FileRef.from_bytes(data, "export.txt", "text/plain")
    first = (await file_resolver._extract_fallback(ref)).text
    assert first.startswith(f"{file_resolver.file_label(ref)}\n\n{data.decode()[: file_resolver._MAX_FALLBACK_CHARS]}\n\n")
    assert f"the first 80,000 of {len(data):,} characters" in first
    hashed: list[int] = []
    monkeypatch.setattr(ax, "hashlib", _CountingHashlib(hashed))
    assert (await file_resolver._extract_fallback(ref)).text == first
    assert calls == ["export.txt"], "extracted once"
    assert hashed == [len(data)], "one digest per request"


def test_a_remembered_cut_is_the_whole_text_cut_at_each_cap():
    """The upload message (50,000 characters) and the fallback (80,000) keep
    their own cut of a large text; each is the whole text's prefix, with its
    lengths, whether computed or remembered."""
    data = "".join(f"line {i:07d} of the export log\n" for i in range(150_000)).encode()
    whole = data.decode()
    for _request in range(2):
        for limit in (50_000, 80_000):
            inline = ax.inline_attachment_text(data, "export.txt", "text/plain", False, limit)
            assert inline == ax.InlineText(whole[:limit], len(whole), len(whole.strip()))


def test_each_inline_cap_decides_for_itself_whatever_the_memo_holds():
    """The upload message (50,000 characters) and the inline fallback (80,000)
    read one memo: a file between the two caps is rendered under the smaller
    and inlined as its source under the larger, in either order."""
    page = ("<html><body>" + "<p>One line item of the invoice.</p>" * 40 + "</body></html>").encode()
    source = extract_text_from_file(page, "invoice.html")
    small, large = len(source) - 1, len(source)
    for limit, as_source in ((small, False), (large, True), (small, False), (large, True)):
        text = ax.extract_inline_attachment_text(page, "invoice.html", "text/html", False, limit)
        assert (text == source) is as_source, limit


def test_the_inline_rule_decides_from_the_plain_extraction_its_caller_holds(monkeypatch):
    """The upload message extracts a file for its own text
    and its knowledge-base copy, then the inline rule extracted it again to
    decide whether to render it. Given that extraction, the rule reads the
    file no second time, answers what it answers without it, and the memo
    learns it for the requests that follow (the 80,000-character fallback)."""
    from app.services import chunking

    small = ("<html><body>" + "<p>One line item of the invoice.</p>" * 40 + "</body></html>").encode()
    files = (("small.html", small), ("large.html", _big_html()))
    expected = {name: ax.inline_attachment_text(data, name, "text/html", False, 50_000) for name, data in files}
    ax.clear_render_cache()
    calls: list[str] = []

    def _extract(content: bytes, filename: str) -> str:
        calls.append(filename)
        return extract_text_from_file(content, filename)

    monkeypatch.setattr(chunking, "extract_text_from_file", _extract)
    for name, data in files:
        plain = extract_text_from_file(data, name)
        assert ax.inline_attachment_text(data, name, "text/html", False, 50_000, plain) == expected[name]
    for name, data in files:
        ax.inline_attachment_text(data, name, "text/html", False, 80_000)
    assert calls == [], "neither file is extracted again"


@pytest.mark.parametrize(("limit_delta", "as_source"), [(0, True), (-1, False)])
def test_the_inline_limit_includes_a_text_of_exactly_that_length(limit_delta, as_source):
    page = ("<html><body>" + "<p>One line item of the invoice.</p>" * 40 + "</body></html>").encode()
    source = extract_text_from_file(page, "invoice.html")
    text = ax.extract_inline_attachment_text(page, "invoice.html", "text/html", False, len(source) + limit_delta)
    assert (text == source) is as_source
    assert ("<p>" in text) is as_source


def _zip_archive() -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("rows.csv", "name,acv\n" + "Acc,1000\n" * 500)
    return buffer.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "content_type"),
    [("data.json", "application/json"), ("data.csv", "text/csv"), ("page.html", "text/html")],
)
async def test_an_archive_under_a_structured_name_is_inlined_as_its_size_stub(filename, content_type, monkeypatch):
    """The plain extraction reads a zip container as nothing, on
    purpose - its compressed bytes decoded are noise - and the released version
    inlined such a file as its size stub, as the upload message still does. The
    inline rule rendered it instead, and a rendering decodes the bytes it is
    given: every provider request carried the archive as text, NUL characters
    included. The empty extraction is what is inlined, and it is not read again."""
    from app.services import chunking

    data = _zip_archive()
    assert extract_text_from_file(data, filename) == ""
    calls: list[str] = []

    def _extract(content: bytes, name: str) -> str:
        calls.append(name)
        return extract_text_from_file(content, name)

    monkeypatch.setattr(chunking, "extract_text_from_file", _extract)
    ref = FileRef.from_bytes(data, filename, content_type)
    stub = file_resolver.file_label(ref, f" ({filename.rsplit('.', 1)[-1].upper()}, {len(data):,} bytes)")
    for _request in range(2):
        assert (await file_resolver._extract_fallback(ref)).text == stub
    assert calls == [filename], "the empty extraction is remembered"


@pytest.mark.asyncio
async def test_a_small_attachment_is_inlined_whole_without_a_footer():
    ref = FileRef.from_bytes(b"name,acv\nAcc 1,1000\nAcc 2,2000\n", "small.csv", "text/csv")
    resolved = await file_resolver._extract_fallback(ref)
    assert resolved.text == f"{file_resolver.file_label(ref)}\n\nname,acv\nAcc 1,1000\nAcc 2,2000\n"


def test_a_plain_document_footer_names_no_mode():
    footer = ax.inline_truncation_footer(
        file_id="f1",
        filename="report.pdf",
        content_type="application/pdf",
        shown=80_000,
        total=90_000,
        code_interpreter=True,
    )
    assert 'read_file_content(file_id="f1", offset=80000)' in footer
    assert "mode=" not in footer


def test_the_footer_names_the_exact_mounted_path_for_a_sanitised_name():
    footer = ax.inline_truncation_footer(
        file_id="0f3c9a1b-2222-4333-8444-555566667777",
        filename="plans/Q3:final.csv",
        content_type="text/csv",
        shown=80_000,
        total=90_000,
        code_interpreter=True,
    )
    assert "attachments/Q3_final-0f3c9a1b222243338444555566667777.csv" in footer


def test_the_footer_is_one_actionable_paragraph():
    footer = ax.inline_truncation_footer(
        file_id="f1",
        filename="plan.html",
        content_type="text/html",
        shown=80_000,
        total=213_456,
        code_interpreter=True,
    )
    assert footer.startswith("[... truncated: the first 80,000 of 213,456 characters are shown.")
    assert footer.endswith(f"{mount_path_hint('plan.html', 'f1')}.]")
    assert "Answer from what is shown and say what you could not read" in footer
    assert len(footer) < 500, "attachment text, kept tight"
