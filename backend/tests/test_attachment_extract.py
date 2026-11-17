"""Server-side reading of data-bearing attachments.

A 559 KB self-contained HTML plan reached the model as raw source - styles,
``<svg>`` geometry and the data its scripts embed - so the inlined text hit the
fallback cap on markup and the model paged through the file until the turn
looped. ``services/attachment_extract`` is the ONE reader every surface shares:
the rendered page with its embedded data (JSON scripts AND JavaScript literals)
summarised first, JSON / JSON Lines summaries, a CSV digest, a JSONPath subset,
all bounded in time and memory on hostile input. The adversarial tests below
pin each quadratic / exponential / unbounded shape the two reviews found; their
time bounds leave orders of magnitude of headroom over the fixed code and are
far below what the old code took.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from types import SimpleNamespace

import pytest

from app.llm.base import FileRef
from app.services import attachment_extract as ax
from app.services.builtin_tools import build_generate_file_tool
from app.tools.base import FileContext, ToolCall


@pytest.fixture(autouse=True)
def _fresh_render_cache():
    ax.clear_render_cache()
    yield
    ax.clear_render_cache()


def _cpu_per_call(fn, arg, *, at_least: float = 0.1) -> float:
    # CPU time of this thread, over calls enough to read it: Windows counts it in 15.6 ms ticks.
    calls, start = 0, time.thread_time()
    while True:
        fn(arg)
        calls += 1
        spent = time.thread_time() - start
        if spent >= at_least:
            return spent / calls


def _growth(fn, make, n: int, *, parts: int = 8, rounds: int = 3) -> float:
    """What ``fn(make(n))`` costs over the same input in *parts* pieces: about 1 for
    work linear in the input, about *parts* for quadratic work.

    Never a budget in seconds: the runner sets those - a slow shard, or collections
    over a long-lived worker's heap, multiplied one by 12 on CI. The two sides run
    the same input on the same runner, interleaved, the cheapest round of each kept.
    """
    large, small = make(n), make(n // parts)
    best_large = best_small = float("inf")
    for _ in range(rounds):
        best_large = min(best_large, _cpu_per_call(fn, large))
        best_small = min(best_small, _cpu_per_call(fn, small))
    return best_large / (parts * best_small)


#: Linear work reads about 1 on :func:`_growth`, quadratic work about 8.
_LINEAR = 3.0


_PLAN = {
    "plan": {
        "region": "North America",
        "fy": "FY2027",
        "targets": [{"name": f"Target {i}", "arr": 1000 * i, "coordinates": [[i, i + 1]] * 30} for i in range(40)],
        "geometry": {"type": "Polygon", "coordinates": [[[x, x * 2] for x in range(500)]]},
        "notes": "n" * 5000,
    }
}
_SVG = (
    "<svg viewBox='0 0 100 100'><title>Chart title</title>"
    + "".join(f"<path d='M {i} {i} L {i + 1} {i + 3} Z'/>" for i in range(2000))
    + "</svg>"
)
_HTML = (
    "<!doctype html><html><head><title>NA Plan FY27</title>"
    "<style>body{color:red}</style>"
    f'<script type="application/json" id="plan-data">{json.dumps(_PLAN)}</script>'
    '<script type="application/ld+json">{"@type": "Article", "name": "Plan"}</script>'
    "<script>window.x = 1; function f() { return 2; }</script></head>"
    "<body><h1>North America Plan</h1>"
    "<p>Tim proposes <b>3 pods</b> and a 40% growth.<br>Second line.</p>"
    "<h2>Objectives</h2><ul><li>Hire 4 AEs</li><li>Open Austin office</li></ul>"
    "<table><tr><th>Quarter</th><th>ARR</th></tr><tr><td>Q1</td><td>1.2M</td></tr>"
    "<tr><td>Q2</td><td>1.5M</td></tr></table>"
    f"{_SVG}<canvas id='c'></canvas><div>Closing remarks.</div></body></html>"
)


def _rows(n: int) -> list[dict]:
    return [
        {
            "name": f"Account {i}",
            "segment": ["Federal", "State", "City", "Enterprise"][i % 4],
            "acv": 62_000 + (i * 137) % 360_000,
            "flag": i % 3 == 0,
        }
        for i in range(n)
    ]


def _coverage_page(rows: list[dict]) -> str:
    """The shape: the data lives in JavaScript literals, not JSON scripts."""
    items = ", ".join(f"{{id: {i}, label: 'item {i}', note: \"it's // not a comment\", }}" for i in range(40))
    return f"""<!DOCTYPE html><html><head><title>Coverage Model</title>
<style>{".x{color:red} " * 200}</style>
<script src="https://cdn.example/chart.js"></script>
<script>
// the page's data
const ACCOUNTS = {json.dumps(rows)};
var CONFIG = {{ theme: 'dark', weights: {{industry: 30}} }};
/* rows */ window.__ROWS__ = [ {items} ];
const FROZEN = Object.freeze({json.dumps([{"k": i, "v": "x" * 10} for i in range(30)])});
const PARSED = JSON.parse('{json.dumps([{"k": i, "v": f"value {i}"} for i in range(30)])}');
function render() {{ document.getElementById('t').innerHTML = ACCOUNTS.length; }}
</script>
<script type="application/json" id="assumptions">{{"ramp_months": 9, "acv": {{"federal": [420000, 285000, 180000]}}}}</script>
</head><body>
<h1>Americas Coverage Model</h1>
<p>Directional model. <b>$9.43M</b> FY27 new ARR is a <i>ceiling</i>.</p>
<table><caption>ACV grid</caption>
<thead><tr><th>Segment</th><th>Large</th><th>Small</th></tr></thead>
<tbody><tr><td>Federal</td><td>420k</td><td>180k</td></tr></tbody></table>
<div id="t"></div><div hidden>never shown</div>
</body></html>"""


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_html_is_rendered_as_reading_text_not_source():
    text = ax.render_html_attachment(_HTML, filename="plan.html")
    assert text.startswith("# NA Plan FY27\n\n[Rendered from ")
    page = text.split("## Page text\n\n", 1)[1]
    assert page.startswith("# North America Plan")
    assert "Tim proposes 3 pods and a 40% growth.\nSecond line." in page
    assert "## Objectives\n\n- Hire 4 AEs\n- Open Austin office" in page
    assert "| Quarter | ARR |\n| Q1 | 1.2M |\n| Q2 | 1.5M |" in page
    assert "Closing remarks." in page
    # Geometry, styles and code never reach the reader ...
    assert "<path" not in text and "M 0 0 L" not in text
    assert "color:red" not in text and "function f()" not in text
    # ... and the strip is accounted for in one line, never silent.
    accounting = text.split("\n\n")[1]
    assert "1 <svg>" in accounting and "1 <style>" in accounting and "1 <canvas>" in accounting
    assert 'read_file_content(mode="source")' in accounting
    assert len(text) < 16_000 < len(_HTML)


def test_the_embedded_data_comes_before_the_page_text_so_a_cap_never_cuts_it():
    text = ax.render_html_attachment(_HTML)
    assert text.index("## Embedded data") < text.index("## Page text")
    assert "query_file_json(file_id, path, block=N)" in text.split("## Page text")[0]
    # The capped text any caller shows keeps the data section and the pointer.
    capped = text[:4_000]
    assert "## Embedded data" in capped and "query_file_json" in capped


def test_a_page_that_omits_the_head_end_tag_still_renders():
    """Html.parser nested the body inside ``<head>``, the
    renderer dropped ``<head>`` wholesale, and a valid HTML5 page rendered EMPTY."""
    page = "<!doctype html><html><head><title>T</title><meta charset=utf-8><body><p>Hello world</p></body></html>"
    assert ax.render_html_attachment(page) == "# T\n\nHello world"


def test_only_a_title_outside_svg_is_the_page_title():
    page = "<html><body>" + _SVG + "<p>Body text</p></body></html>"
    rendered = ax.render_html_attachment(page)
    assert not rendered.startswith("# Chart title")
    assert "Chart title" not in rendered


def test_table_cells_escape_the_pipe_and_empty_rows_and_trailing_cells_go():
    page = (
        "<table><tr><th>Segment</th><th>Note</th><th></th></tr>"
        "<tr><td>Federal | US</td><td>ok</td><td> </td></tr><tr><td></td><td></td></tr></table>"
    )
    rendered = ax.render_html_attachment(page)
    assert rendered == "| Segment | Note |\n| Federal \\| US | ok |"


def test_only_the_hidden_attribute_hides_and_until_found_content_stays():
    page = '<div hidden>never shown</div><div hidden="until-found">revealable</div><p aria-hidden="true">kept</p>'
    rendered = ax.render_html_attachment(page)
    assert "never shown" not in rendered
    assert "revealable" in rendered and "kept" in rendered
    assert "1 hidden element" in rendered


def test_text_outside_the_body_is_read_too():
    rendered = ax.render_html_attachment("<html><body><p>inside</p></body></html><p>after the body</p>")
    assert "inside" in rendered and "after the body" in rendered


def test_nested_lists_indent_and_a_nested_table_flows_into_its_cell():
    page = (
        "<ul><li>top<ul><li>nested</li></ul></li></ul>"
        "<table><tr><td>a</td><td>b<table><tr><td>inner</td></tr></table></td></tr></table>"
        "<pre>line one\n  line two</pre>"
    )
    rendered = ax.render_html_attachment(page)
    assert "- top\n  - nested" in rendered
    assert "| a | b inner |" in rendered
    assert "line one\n  line two" in rendered


# ---------------------------------------------------------------------------
# Embedded data: JSON scripts and JavaScript literals
# ---------------------------------------------------------------------------


def test_json_scripts_are_summarised_shape_first():
    doc = ax.extract_html_document(_HTML)
    assert [b.kind for b in doc.json_blocks] == ["application/json", "application/ld+json"]
    assert doc.json_blocks[0].element_id == "plan-data"
    assert doc.json_blocks[0].data["plan"]["region"] == "North America"
    text = ax.render_html_attachment(_HTML)
    assert "2 data blocks found in the page's scripts" in text
    assert 'Block 1: application/json id="plan-data"' in text
    # The geometry is folded to its shape, the long string cut, the array counted.
    assert "[<500 x 2 numeric rows>]" in text
    assert "more chars>" in text
    assert "<28 more items>" in text


def test_data_embedded_as_javascript_literals_is_found_and_normalised():
    """The capability an earlier extractor lacked: ``const X = [...]``, ``window.X =
    [...]`` written as JavaScript (bare keys, single quotes, a trailing comma,
    ``//`` inside a string), ``Object.freeze([...])`` and ``JSON.parse('...')``."""
    doc = ax.extract_html_document(_coverage_page(_rows(1_375)))
    labels = [b.label for b in doc.json_blocks]
    by_name = {b.element_id: b for b in doc.json_blocks}
    assert {"ACCOUNTS", "window.__ROWS__", "FROZEN", "PARSED (JSON.parse)", "assumptions"} <= set(by_name)
    assert "CONFIG" not in by_name, "a small configuration object is not data"
    assert len(by_name["ACCOUNTS"].data) == 1_375
    assert by_name["window.__ROWS__"].data[3] == {"id": 3, "label": "item 3", "note": "it's // not a comment"}
    assert len(by_name["PARSED (JSON.parse)"].data) == 30
    # Numbered in document order - the order query_file_json uses.
    assert labels[0].startswith("Block 1: JavaScript literal ACCOUNTS")
    assert labels[-1].startswith('Block 5: application/json id="assumptions"')


def test_records_are_summarised_with_their_field_statistics():
    text = ax.render_html_attachment(_coverage_page(_rows(1_375)))
    section = text.split("## Page text")[0]
    assert "// 1,375 records" in section
    assert "fields: name, segment, acv, flag" in section
    assert 'segment: 4 distinct: "Federal" (344)' in section
    assert "acv: number x1375: min 62,000" in section and "sum " in section
    assert "flag: boolean: true 459, false 916" in section
    assert '{"name": "Account 0", "segment": "Federal", "acv": 62000, "flag": true}' in section
    # The largest block leads the section; the numbering stays document order.
    assert section.index("### Block 1: JavaScript literal ACCOUNTS") < section.index("### Block 5:")


def test_a_literal_that_does_not_parse_is_counted_not_digested():
    script = "const BAD = [" + ", ".join(f"fn({i}) + x" for i in range(200)) + "];"
    doc = ax.extract_html_document(f"<html><body><script>{script}</script><p>hi</p></body></html>")
    assert doc.json_blocks == []
    assert len(doc.unparsed_literal_chars) == 1
    rendered = ax.render_html_attachment(f"<html><body><script>{script}</script><p>hi</p></body></html>")
    assert "did not parse as data and are omitted" in rendered and "hi" in rendered


def test_a_bare_script_literal_is_one_block():
    body = json.dumps({"rows": [{"a": i} for i in range(100)], "meta": {"v": 2}})
    doc = ax.extract_html_document(f"<html><body><script>{body}</script></body></html>")
    assert len(doc.json_blocks) == 1 and doc.json_blocks[0].data["meta"] == {"v": 2}


def test_the_embedded_data_section_and_each_block_are_bounded():
    wide = [{f"field_{j}": f"value {i} {j}" for j in range(80)} for i in range(500)]
    page = (
        "<html><body>"
        + "".join(f"<script>const W{k} = {json.dumps(wide)};</script>" for k in range(6))
        + "</body></html>"
    )
    text = ax.render_html_attachment(page)
    section = text.split("## Page text")[0]
    assert len(section) <= ax.EMBEDDED_DATA_SECTION_MAX_CHARS + 2_000
    assert "not summarised here" in section
    block = ax.summarize_json(wide, max_chars=2_000)
    assert len(block) <= 2_000 + 120 and "summary cut at 2,000 chars" in block


# ---------------------------------------------------------------------------
# Adversarial input: every shape the two reviews measured, now linear
# ---------------------------------------------------------------------------


def test_nesting_depth_costs_linear_time():
    """Bs4 ancestor walks on insert / decompose made the
    renderer quadratic in depth (20k nested divs: 4.9 s, so 80k ~ 80 s). One
    streaming pass keeps no tree and walks no ancestors."""
    def nested(depth: int) -> str:
        return "<html><body>" + "<div>" * depth + "deep text" + "</div>" * depth + "</body></html>"

    def cells(count: int) -> str:
        return "<table><tr>" + "<td>x" * count + "</tr></table>"  # 4k unclosed td took 0.28 s, quadratic

    assert ax.render_html_attachment(nested(80_000)) == "deep text"
    assert _growth(ax.render_html_attachment, nested, 80_000) < _LINEAR
    assert ax.render_html_attachment(cells(64_000)).count("| x") == 64_000
    assert _growth(ax.render_html_attachment, cells, 64_000) < _LINEAR


def test_a_row_of_empty_cells_is_not_exponential():
    """``(\\s*\\|\\s*)+$`` doubled per empty cell (22 cells
    1.33 s; 40 cells would be days). Trailing empty cells are popped, not regexed."""

    def row(empty: int) -> str:
        return "<table><tr><td>a</td>" + "<td> </td>" * empty + "</tr></table>"

    assert ax.render_html_attachment(row(60)) == "| a |"
    assert _growth(ax.render_html_attachment, row, 4_000) < _LINEAR


def test_long_identifier_runs_in_a_script_are_scanned_once():
    """The assignment anchor regex restarted inside every
    identifier run (80k chars: 67 s). The scanner reads an identifier as one token."""
    for script in (lambda n: "var " + "a" * n + " ; b = 1;", lambda n: "0x" + "f" * n):
        assert _growth(ax.render_html_attachment, lambda n, s=script: f"<script>{s(n)}</script><p>x</p>", 320_000) < _LINEAR


def test_unbalanced_anchors_never_rescan_the_script():
    """Each unbalanced ``x=[`` rescanned to the end of the
    script (8,000 anchors in 24 KB: 5.9 s, quadratic). Brackets are counted
    in one pass, and a literal is parsed from its exact slice."""
    for anchor in ("x=[", "x=[1,"):
        assert _growth(ax.render_html_attachment, lambda n, a=anchor: f"<script>{a * n}</script>", 128_000) < _LINEAR


def test_many_json_parse_candidates_are_linear():
    """``_inside()`` made ``JSON.parse`` candidates
    quadratic (4,000: 6 s) and every candidate was parsed twice."""

    def page(count: int) -> str:
        return "<script>" + "".join(f"a{i}=JSON.parse('{'q' * 300}');" for i in range(count)) + "</script>"

    assert "did not parse as data" in ax.render_html_attachment(page(10_000))
    # 10,000 candidates stay under the parse budget, which would cut a larger page short.
    assert ax._JS_NORMALISE_MAX_CHARS > 10_000 * 300
    assert _growth(ax.render_html_attachment, page, 10_000) < _LINEAR


def test_a_deeply_nested_literal_or_json_script_never_raises():
    """A 5,000-deep page / 20k-deep literal raised
    RecursionError and the page fell back to raw source."""
    deep_literal = "x = " + "[" * 20_000 + "]" * 20_000 + ";"
    out = ax.render_html_attachment(f"<script>{deep_literal}</script><p>page text</p>")
    assert "page text" in out
    deep_json = '<script type="application/json">' + "[" * 20_000 + "]" * 20_000 + "</script><p>ok</p>"
    out = ax.render_html_attachment(deep_json)
    # Whether ``json.loads`` gets through 20,000 levels depends on the C
    # stack (3.14 guards it with a RecursionError): a block it parses is
    # summarised with its depth folded, one it cannot is counted as skipped.
    assert out.endswith("## Page text\n\nok")
    assert "nested too deep" in out or "skipped" in out
    assert ax.summarize_json(json.loads("[" * 900 + "]" * 900)).count("nested too deep") == 1


def test_huge_numbers_never_overflow_the_statistics():
    """``_is_number`` / ``math.fsum`` raised OverflowError
    on a 400-digit int or ``[1e308, 1e308]`` and killed the whole render."""
    rows = [{"big": 10**400, "f": 1e308, "name": f"r{i}"} for i in range(20)]
    out = ax.render_html_attachment(f"<script>const R = {json.dumps(rows)};</script><p>t</p>")
    assert "sum overflow" in out and "t" in out
    assert ax.summarize_json([10**400] * 20) == "[<20 numbers, 1.000e+400 .. 1.000e+400>]"
    digest = ax.digest_csv_text("a,b\n" + "\n".join(f"{1e308},{i}" for i in range(50)))
    assert "a: number x50: min 1.000e+308, max 1.000e+308, sum overflow" in digest


def test_the_summary_is_rendered_against_its_budget_not_whole_then_cut(monkeypatch):
    """``summarize_json`` rendered the whole tree (512k
    leaves: 5.6 s, 70 MB) before cutting. The emission stops at the budget."""
    calls = {"n": 0}
    original = ax._render_scalar

    def _counting(value, *, key):
        calls["n"] += 1
        return original(value, key=key)

    monkeypatch.setattr(ax, "_render_scalar", _counting)
    tree = {f"k{i}": {f"j{j}": [f"v{x}" for x in range(64)] for j in range(80)} for i in range(100)}
    summary = ax.summarize_json(tree, max_chars=20_000)
    assert "summary cut at 20,000 chars" in summary
    assert calls["n"] < 20_000, "the walk stopped with the output, not after 512,000 leaves"


def test_a_query_result_stops_serialising_at_its_bound(monkeypatch):
    """``json.dumps(indent=2)`` serialised the whole match
    before cutting (``$`` on 100k rows: 1.55 s). The encoder is consumed chunk
    by chunk and stopped at the bound."""
    chunks = {"n": 0}

    class _Counting(json.JSONEncoder):
        def iterencode(self, o, _one_shot=False):
            for chunk in super().iterencode(o, _one_shot):
                chunks["n"] += 1
                yield chunk

    monkeypatch.setattr(ax.json, "JSONEncoder", _Counting)
    text, complete = ax.dump_json_bounded(list(range(500_000)), 30_000)
    assert complete is False and len(text) == 30_000
    assert chunks["n"] < 20_000, "stopped at the bound, not after 500,000 values"


def test_a_page_is_rendered_once_per_content_not_once_per_turn(monkeypatch):
    """The inline fallback re-reads every attachment of the history on every
    turn: the rendering is memoised per content hash."""
    calls = {"n": 0}
    original = ax._render

    def _counting(*args):
        calls["n"] += 1
        return original(*args)

    monkeypatch.setattr(ax, "_render", _counting)
    data = _HTML.encode()
    first = ax.extract_attachment_text(data, "plan.html", "text/html")
    for _ in range(5):
        assert ax.extract_attachment_text(data, "plan.html", "text/html") == first
    assert calls["n"] == 1
    ax.extract_attachment_text(data + b" ", "plan.html", "text/html")
    assert calls["n"] == 2, "other bytes, another rendering"


# ---------------------------------------------------------------------------
# JSON / JSON Lines / CSV
# ---------------------------------------------------------------------------


def test_summarize_json_is_bounded_and_shape_preserving():
    summary = ax.summarize_json(_PLAN, max_chars=1_500)
    assert len(summary) <= 1_500 + 120
    assert summary.lstrip().startswith("{")
    assert '"region": "North America"' in summary
    assert "summary cut at 1,500 chars" in summary and "query_file_json" in summary
    assert ax.summarize_json({"a": [1, 2], "b": None, "c": True}) == '{\n  "a": [1, 2],\n  "b": null,\n  "c": true\n}'


def test_numeric_and_geo_payloads_fold_to_their_shape():
    assert ax.summarize_json(list(range(50))) == "[<50 numbers, 0 .. 49>]"
    assert ax.summarize_json([[1, 2]] * 20) == "[<20 x 2 numeric rows>]"
    folded = ax.summarize_json({"d": "M 10 20 L 30 40 " * 20, "label": "x" * 100})
    assert '"d": "<string, 320 chars: M 10 20 L 30 40 M 10 20 ...>"' in folded
    assert '"label": "' + "x" * 100 + '"' in folded
    assert ax.summarize_json([1, 2, 3]) == "[1, 2, 3]"


def test_json_lines_are_read_line_by_line():
    """``.jsonl`` went to a single
    ``json.loads`` and always yielded nothing."""
    lines = [json.dumps({"id": i, "team": ["a", "b"][i % 2], "score": i}) for i in range(2_000)]
    text = "\n".join([*lines[:5], "{broken", *lines[5:]])
    rendered = ax.extract_attachment_text(text.encode(), "events.jsonl", None)
    assert rendered.startswith("[JSON Lines document,")
    assert "2,001 lines, 1 not valid JSON (line 6)" in rendered
    assert "// 2,001 items: 2,000 records, 1 invalid line" in rendered and "team: 2 distinct" in rendered
    blocks = ax.load_attachment_json(text.encode(), "events.ndjson", None)
    assert len(blocks) == 1 and len(blocks[0].data) == 2_001 and str(blocks[0].data[5]) == "<invalid line 6>"
    out, is_error = ax.run_json_query(text.encode(), "events.jsonl", None, "$[3].score", None)
    assert not is_error and out.endswith("3")


def test_the_csv_digest_reads_header_rows_and_numbers():
    text = "name;segment;acv\n" + "\n".join(f"Acc {i};{['Fed', 'State'][i % 2]};{1_000 + i}" for i in range(300))
    out = ax.digest_csv_text(text)
    assert "CSV: 300 data rows, 3 columns, delimiter ';'" in out
    assert "segment: 2 distinct:" in out
    assert "acv: number x300: min 1,000, max 1,299" in out
    assert '{"name":"Acc 0","segment":"Fed","acv":1000}' in out


def test_csv_cells_coerce_grouped_numbers_and_refuse_garbage():
    assert ax._coerce_cell("1,234,567.5") == 1_234_567.5
    assert ax._coerce_cell("42") == 42
    assert ax._coerce_cell("  ") is None
    assert ax._coerce_cell("N/A") == "N/A"
    assert ax._coerce_cell("9" * 400) == "9" * 400, "an over-long cell stays text"
    assert ax.digest_csv_text("") is None


def test_a_large_csv_leads_with_the_digest_of_the_whole_file():
    text = "name,segment,acv\n" + "\n".join(f"Acc {i},{['Fed', 'State'][i % 2]},{1_000 + i}" for i in range(6_000))
    rendered = ax.extract_attachment_text(text.encode(), "accounts.csv", "text/csv")
    assert rendered.startswith("[CSV document,")
    assert "CSV: 6,000 data rows, 3 columns" in rendered
    assert "(statistics over the first 5,000 of 6,000 rows)" in rendered
    assert rendered.index("CSV: 6,000 data rows") < rendered.index("## File text")
    assert rendered.endswith(text), "the whole file follows the digest"
    small = "name,acv\nAcc 1,1000\n"
    assert ax.extract_attachment_text(small.encode(), "s.csv", "text/csv") == small
    tsv = "a\tb\n" + "\n".join(f"{i}\t{i * 2}" for i in range(5_000))
    assert "delimiter '\\t'" in ax.extract_attachment_text(tsv.encode(), "t.tsv", None)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("$.plan.region", ["North America"]),
        ("plan.fy", ["FY2027"]),
        ("$.plan.targets[0].name", ["Target 0"]),
        ("$.plan.targets[-1].name", ["Target 39"]),
        ("$.plan.targets[1:3].arr", [1000, 2000]),
        ("$..region", ["North America"]),
        ("$.plan['fy']", ["FY2027"]),
        ('$["plan"]["fy"]', ["FY2027"]),
        ("$.plan.missing", []),
        ("$", [_PLAN]),
    ],
)
def test_query_json_subset(path, expected):
    assert ax.query_json(_PLAN, path) == expected


def test_query_json_wildcards_bounds_and_refusals():
    names = ax.query_json(_PLAN, "$.plan.targets[*].name")
    assert names[:2] == ["Target 0", "Target 1"] and len(names) == 40
    assert len(ax.query_json({"k": list(range(1000))}, "$.k[*]")) == ax.QUERY_MAX_RESULTS
    assert len(ax.query_json(_PLAN, "$..*", max_results=5)) == 5
    with pytest.raises(ax.JsonPathError) as exc:
        ax.query_json(_PLAN, "$.plan.targets[?(@.arr > 1)]")
    assert "unsupported path syntax" in str(exc.value) and "..key" in str(exc.value)


def test_a_recursive_descent_stops_walking_once_it_holds_its_answer(monkeypatch):
    calls = {"n": 0}
    original = ax._descendants

    def _counting(value):
        for node in original(value):
            calls["n"] += 1
            yield node

    monkeypatch.setattr(ax, "_descendants", _counting)
    huge = {"rows": [{"id": i, "tags": ["a", "b"]} for i in range(50_000)]}
    assert len(ax.query_json(huge, "$..*", max_results=10)) == 10
    assert calls["n"] <= 41


def test_run_json_query_validates_block_and_path_up_front():
    data = _HTML.encode()
    assert ax.run_json_query(data, "plan.html", "text/html", "$", True) == (
        "Error: block must be a block number (1, 2, ...), not true / false.",
        True,
    )
    out, is_error = ax.run_json_query(data, "plan.html", "text/html", "$", 7)
    assert is_error and "block 7 does not exist" in out and "Block 2: application/ld+json" in out
    out, is_error = ax.run_json_query(data, "plan.html", "text/html", "$[?(@)]", None)
    assert is_error and "unsupported path syntax" in out
    out, is_error = ax.run_json_query(b"just words", "notes.txt", "text/plain", "$.a", None)
    assert is_error and "carries no JSON to query" in out


# ---------------------------------------------------------------------------
# Decoding and the entry point
# ---------------------------------------------------------------------------


def test_a_non_utf8_export_is_decoded_not_garbled():
    latin = "<html><body><h1>Plan Amérique du Nord</h1><p>Résumé.</p></body></html>".encode("cp1252")
    assert ax.decode_attachment_text(latin).startswith("<html><body><h1>Plan Amérique")
    declared = '<meta charset="iso-8859-15"><p>Coût: 5 €</p>'.encode("iso-8859-15")
    # A page's own <meta> is read for HTML only (a JSON field can carry that string).
    assert "5 €" in ax.decode_attachment_text(declared, html=True)
    assert ax.decode_attachment_text(b"\xef\xbb\xbfhello") == "hello"
    rendered = ax.extract_attachment_text(latin, "plan-fr.html", "text/html")
    assert rendered == "# Plan Amérique du Nord\n\nRésumé."


def test_extract_attachment_text_dispatches_by_kind():
    assert ax.extract_attachment_text(_HTML.encode(), "plan.html", "text/html").startswith("# NA Plan FY27")
    small = json.dumps({"a": 1}).encode()
    assert ax.extract_attachment_text(small, "a.json", "application/json") == small.decode()
    huge = json.dumps({"rows": [{"id": i, "v": "x" * 50} for i in range(2000)]}).encode()
    summary = ax.extract_attachment_text(huge, "rows.json", "application/json")
    assert summary.startswith("[JSON document,") and "query_file_json" in summary and len(summary) < len(huge) // 4
    # MIME-only kinds are recognised too.
    assert ax.attachment_kind("payload.bin", "application/vnd.api+json") == "json"
    assert ax.attachment_kind("x.bin", "application/xhtml+xml") == "html"
    assert ax.attachment_kind("x", "application/x-ndjson") == "jsonl"
    assert ax.attachment_kind("notes.md", "text/markdown") is None
    assert ax.extract_attachment_text(b"hello world", "note.txt", "text/plain") == "hello world"


def test_a_broken_renderer_falls_back_to_the_plain_extraction(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("parser down")

    monkeypatch.setattr(ax, "render_html_attachment", _boom)
    out = ax.extract_attachment_text(b"<p>x</p>", "a.html", "text/html")
    # ... and says so: the reader is told it gets the raw file, not the
    # rendering every tool description promises.
    assert out.startswith("[The HTML rendering of this file failed: what follows is its plain text extraction")
    assert out.endswith("\n\n<p>x</p>")


def test_knowledge_base_ingestion_is_unchanged():
    """Decision of the reconciliation: KB ingestion keeps indexing the SOURCE
    (rendering it would silently change every HTML document's embedding on
    its next re-embed); an opt-in is a follow-up."""
    from app.services.chunking import extract_text_from_file

    assert extract_text_from_file(_HTML.encode(), "plan.html") == _HTML


# ---------------------------------------------------------------------------
# The surfaces: inline fallback, read_file_content, query_file_json
# ---------------------------------------------------------------------------


def _build(monkeypatch, *refs: FileRef):
    async def _fake_persist(**kwargs):
        return SimpleNamespace(
            id=uuid.uuid4(),
            filename=kwargs["filename"],
            content_type=kwargs["content_type"],
            size=len(kwargs["raw_bytes"]),
            s3_object_name=f"chat/conv/{kwargs['filename']}",
            source=kwargs["source"],
        )

    monkeypatch.setattr("app.services.builtin_tools._persist_generated_file", _fake_persist)
    _defs, executor = build_generate_file_tool(db=None, user_id=str(uuid.uuid4()), conversation_id=str(uuid.uuid4()))
    ctx = FileContext()
    for ref in refs:
        ctx.add(ref)
    executor.file_context = ctx
    return executor


async def _call(executor, name: str, **arguments):
    return await executor.execute_tool(ToolCall(id=f"call-{name}", name=name, arguments=arguments))


@pytest.mark.asyncio
async def test_the_inline_fallback_and_the_rendered_view_serve_the_same_characters(monkeypatch):
    """The footer's offset is an offset into the text ``read_file_content(
    mode="extract_structured")`` serves - including for a non-UTF-8 page."""
    from app.llm import file_resolver
    from app.services.builtin_tools import build_code_interpreter_tool
    from app.tools.base import offering_tools

    rows = _rows(4_000)
    narrative = "".join(f"<p>Paragraf {i} über den Plan - Résumé.</p>" for i in range(4_000))
    page = f"<html><head><title>Modèle</title></head><body><script>const ROWS = {json.dumps(rows)};</script>{narrative}</body></html>"
    raw = page.encode("cp1252")
    ref = FileRef.from_bytes(raw, "Modèle TD.html", "text/html")
    # One turn, one surface: the inline text and the rendered view it
    # points at are read under the same one.
    with offering_tools(build_code_interpreter_tool()[0]):
        resolved = await file_resolver._extract_fallback(ref)
        body = resolved.text.split("\n\n", 1)[1]
        inline, footer = body.rsplit("\n\n[... truncated: ", 1)
        assert f'read_file_content(file_id="{ref.file_id}", mode="extract_structured", offset={len(inline)})' in footer
        from app.services.code_interpreter import mount_path_hint

        assert f"code_interpreter, where it is mounted at {mount_path_hint(ref.filename, ref.file_id)}" in footer
        executor = _build(monkeypatch, ref)
        first = await _call(
            executor, "read_file_content", file_id=ref.file_id, mode="extract_structured", max_chars=len(inline)
        )
        assert first.content.split(", verbatim slice):\n", 1)[1].split("\n\n[TRUNCATED", 1)[0] == inline
        nxt = await _call(
            executor, "read_file_content", file_id=ref.file_id, mode="extract_structured", offset=len(inline), max_chars=200
        )
    assert f"(characters {len(inline)}-" in nxt.content
    assert (
        ax.extract_attachment_text(raw, ref.filename, ref.content_type, True)[len(inline) : len(inline) + 200]
        in nxt.content
    )


@pytest.mark.asyncio
async def test_read_file_content_source_and_rendered_views(monkeypatch):
    ref = FileRef.from_bytes(_HTML.encode(), "plan.html", "text/html")
    executor = _build(monkeypatch, ref)
    source = await _call(executor, "read_file_content", file_id=ref.file_id, max_chars=200)
    assert source.is_error is False
    assert "editable source text" in source.content and 'mode="extract_structured"' in source.content
    assert "<!doctype html>" in source.content and "[TRUNCATED -" in source.content
    rendered = await _call(executor, "read_file_content", file_id=ref.file_id, mode="extract_structured")
    assert rendered.is_error is False
    assert "rendered for reading" in rendered.content and "## Embedded data" in rendered.content
    assert "<path" not in rendered.content and "Content (complete," in rendered.content
    unknown = await _call(executor, "read_file_content", file_id=ref.file_id, mode="pixels")
    assert unknown.is_error is True and "unknown mode" in unknown.content


@pytest.mark.asyncio
async def test_a_mime_only_json_file_is_rendered_in_the_structured_mode(monkeypatch):
    """``application/vnd.api+json`` under ``.bin`` is JSON by
    its MIME type but not "editable", and the structured branch sat inside
    ``if editable:`` - the mode fell through to the plain extraction."""
    payload = json.dumps({"data": [{"id": i, "type": "item", "v": "x" * 40} for i in range(2_000)]}).encode()
    ref = FileRef.from_bytes(payload, "payload.bin", "application/vnd.api+json")
    executor = _build(monkeypatch, ref)
    result = await _call(executor, "read_file_content", file_id=ref.file_id, mode="extract_structured")
    assert result.is_error is False
    assert "rendered for reading" in result.content and "[JSON document," in result.content


@pytest.mark.asyncio
async def test_every_rendering_surface_runs_off_the_event_loop(monkeypatch):
    """``query_file_json`` parsed, queried and formatted on
    the event loop. Every surface goes through the parse executor."""
    from app.core import parse_offload
    from app.llm import file_resolver

    seen: list[str] = []
    original = parse_offload.run_parse

    async def _recording(fn, /, *args):
        seen.append(getattr(fn, "__name__", repr(fn)))
        return await original(fn, *args)

    monkeypatch.setattr(parse_offload, "run_parse", _recording)
    ref = FileRef.from_bytes(_HTML.encode(), "plan.html", "text/html")
    executor = _build(monkeypatch, ref)
    await _call(executor, "read_file_content", file_id=ref.file_id, mode="extract_structured")
    await _call(executor, "query_file_json", file_id=ref.file_id, path="$.plan.region", block=1)
    await file_resolver._extract_fallback(ref)
    assert seen.count("extract_attachment_text") == 1
    assert "inline_attachment_text" in seen, "the inline fallback runs off the event loop too"
    assert "run_json_query" in seen


@pytest.mark.asyncio
async def test_query_file_json_reads_one_value_of_the_embedded_data(monkeypatch):
    ref = FileRef.from_bytes(_HTML.encode(), "plan.html", "text/html")
    coverage = FileRef.from_bytes(_coverage_page(_rows(50)).encode(), "coverage.html", "text/html")
    executor = _build(monkeypatch, ref, coverage)
    names = await _call(executor, "query_file_json", file_id=ref.file_id, path="$.plan.targets[*].name", block=1)
    assert names.is_error is False
    assert "40 values match" in names.content and '"Target 39"' in names.content
    structure = await _call(executor, "query_file_json", file_id=ref.file_id)
    assert "2 data blocks - pass block=N" in structure.content and '"@type": "Article"' in structure.content
    everywhere = await _call(executor, "query_file_json", file_id=ref.file_id, path="$..name")
    assert "[Block 2: application/ld+json" in everywhere.content and '"Plan"' in everywhere.content
    # A JavaScript literal is a block like any other.
    acv = await _call(executor, "query_file_json", file_id=coverage.file_id, path="$[0].acv", block=1)
    assert acv.is_error is False and "62000" in acv.content
    bad = await _call(executor, "query_file_json", file_id=ref.file_id, path="$", block=True)
    assert bad.is_error is True and "not true / false" in bad.content
    missing = await _call(executor, "query_file_json", file_id="nope", path="$")
    assert missing.is_error is True and "no file 'nope'" in missing.content


@pytest.mark.asyncio
async def test_query_file_json_on_a_file_without_json_says_so(monkeypatch):
    plain = FileRef.from_bytes(b"just words", "notes.txt", "text/plain")
    image = FileRef.from_bytes(b"\x89PNG\r\n\x1a\n", "pic.png", "image/png")
    executor = _build(monkeypatch, plain, image)
    result = await _call(executor, "query_file_json", file_id=plain.file_id, path="$.a")
    assert result.is_error is True and "carries no JSON to query" in result.content
    result = await _call(executor, "query_file_json", file_id=image.file_id, path="$.a")
    assert result.is_error is True and "is an image" in result.content


def test_the_render_cache_is_thread_safe_under_the_parse_pool():
    data = _HTML.encode()

    async def _many():
        return await asyncio.gather(*[ax.extract_attachment_text_async(data, "p.html", "text/html") for _ in range(16)])

    results = asyncio.run(_many())
    assert len(set(results)) == 1


# ---------------------------------------------------------------------------
# Honest decoding, cache keys and partial reads
# ---------------------------------------------------------------------------


def test_a_declared_charset_wins_over_the_utf8_guess():
    """Strict UTF-8 ran BEFORE the declared charset. BOM-less
    UTF-16 is valid UTF-8 (its ASCII half is NUL bytes), so a page the MIME
    type declared UTF-16 came back as NUL-riddled markup; and a legacy page
    whose accented bytes happen to form UTF-8 ignored its own <meta>."""
    utf16 = "<p>Plan \u00e9t\u00e9</p>".encode("utf-16-le")
    assert ax.decode_attachment_text(utf16, "text/html; charset=utf-16le") == "<p>Plan \u00e9t\u00e9</p>"
    assert ax.extract_attachment_text(utf16, "plan.html", "text/html; charset=utf-16le") == "Plan \u00e9t\u00e9"
    # C3 A9 is valid UTF-8 ("e acute") AND two Latin-1 characters: the page says which.
    legacy = "<html><head><meta charset='iso-8859-1'></head><body><p>\u00c3\u00a9</p></body></html>".encode("latin-1")
    assert ax.decode_attachment_text(legacy, "text/html", html=True).endswith("<p>\u00c3\u00a9</p></body></html>")
    assert ax.extract_attachment_text(legacy, "legacy.html", "text/html") == "\u00c3\u00a9"
    # The http-equiv spelling is a declaration too, and the transport outranks the page.
    equiv = '<meta http-equiv="Content-Type" content="text/html; charset=windows-1251"><p>\u043f\u043b\u0430\u043d</p>'
    assert "\u043f\u043b\u0430\u043d" in ax.decode_attachment_text(equiv.encode("cp1251"), "text/html", html=True)
    # Byte E9 is "short i" in windows-1251 and "e acute" in Latin-1: the MIME type decides.
    both = "<meta charset='iso-8859-1'><p>\u0439</p>".encode("cp1251")
    assert ax.decode_attachment_text(both, "text/html; charset=windows-1251", html=True).endswith("<p>\u0439</p>")


def test_a_meta_charset_inside_a_json_or_csv_file_declares_nothing():
    """A JSON / CSV file that merely CONTAINS markup (a scraped page in a
    field) must not have its decoding picked by it."""
    payload = json.dumps({"html": "<meta charset='iso-8859-1'>", "name": "\u00e9t\u00e9"}, ensure_ascii=False).encode()
    assert ax.decode_attachment_text(payload, "application/json") == payload.decode("utf-8")
    assert json.loads(ax.extract_attachment_text(payload, "page.json", "application/json"))["name"] == "\u00e9t\u00e9"


@pytest.mark.parametrize("label", ["base64", "zlib", "rot13", "hex", "utf-7", "unicode_escape", "no-such-codec"])
def test_a_declared_charset_no_browser_decodes_with_is_ignored(label):
    text = "<p>caf\u00e9 +ADw-b+AD4- \\u0041</p>"
    assert ax.decode_attachment_text(text.encode("utf-8"), f"text/html; charset={label}", html=True) == text


def test_the_rendering_cache_keys_on_the_charset_and_the_extension():
    """The key held the bare MIME type and no extension, so
    identical bytes declared in another charset, or uploaded as .tsv then
    .csv, were served the first upload's rendering."""
    page = "<p>hello</p>".encode("utf-16-le")
    undeclared = ax.extract_attachment_text(page, "p.html", "text/html")
    declared = ax.extract_attachment_text(page, "p.html", "text/html; charset=utf-16le")
    assert declared == "hello" and undeclared != declared
    table = ("a\tb;c\n" + "\n".join(f"{i}\tx;{i}" for i in range(6_000))).encode()
    as_tsv = ax.extract_attachment_text(table, "t.tsv", None)
    as_csv = ax.extract_attachment_text(table, "t.csv", None)
    assert "delimiter '\\t'" in as_tsv and "delimiter ';'" in as_csv


def test_a_query_over_a_capped_json_lines_file_says_what_it_did_not_search(monkeypatch):
    """Past JSONL_MAX_RECORDS the query path dropped the
    unread count, and a value in the unread lines came back as a definitive
    "No value matches"."""
    monkeypatch.setattr(ax, "JSONL_MAX_RECORDS", 100)
    lines = [json.dumps({"id": i, "team": "late" if i == 150 else "early"}) for i in range(200)]
    raw = "\n".join(lines).encode()
    out, is_error = ax.run_json_query(raw, "events.jsonl", None, "$[*].missing", None)
    assert not is_error
    assert "[Partial block: only the first 100 of its 200 lines were parsed - the last 100 are not in these records" in out
    assert out.index("[Partial block:") < out.index("No value matches")
    out, _ = ax.run_json_query(raw, "events.jsonl", None, "$..team", None)
    assert '"late"' not in out and "[Partial block:" in out
    # A file read whole says nothing of the kind.
    whole, _ = ax.run_json_query("\n".join(lines[:50]).encode(), "events.jsonl", None, "$[0].id", None)
    assert "Partial" not in whole


def test_data_blocks_past_the_cap_are_counted_in_the_section_and_the_query(monkeypatch):
    monkeypatch.setattr(ax, "MAX_EMBEDDED_BLOCKS", 2)
    scripts = "".join(f'<script type="application/json">{json.dumps({"k": i})}</script>' for i in range(5))
    page = f"<html><body>{scripts}<p>t</p></body></html>"
    assert "3 more data blocks past the first 2 were not read" in ax.render_html_attachment(page)
    out, is_error = ax.run_json_query(page.encode(), "p.html", "text/html", "$.k", None)
    assert not is_error
    assert out.startswith("[Partial: 3 more data blocks past the first 2 were not read - a value there")


def test_a_json_lines_record_carrying_a_line_separator_stays_one_record():
    """``str.splitlines()`` split U+2028 / U+2029 / U+0085, which JSON allows raw
    inside a string: the record became two invalid lines and vanished."""
    records = [{"id": 1, "note": "a\u2028b"}, {"id": 2, "note": "c\u0085d"}, {"id": 3, "note": "e\u2029f"}]
    text = "\r\n".join(json.dumps(r, ensure_ascii=False) for r in records)
    (block,) = ax.load_attachment_json(text.encode(), "notes.jsonl", None)
    assert block.data == records and block.partial is None


def _jsonl(count: int, *, invalid: tuple[int, ...] = (), blank: tuple[int, ...] = (), pad: int = 0) -> str:
    """*count* lines, line N holding ``{"line": N, ...}`` - except the lines in
    *invalid*, which are not valid JSON, and the lines in *blank*."""
    lines = []
    for number in range(1, count + 1):
        if number in invalid:
            lines.append(f'{{"line": {number}, broken')
        elif number in blank:
            lines.append("")
        else:
            lines.append(json.dumps({"line": number, "team": "ab"[number % 2], "score": number, "pad": "x" * pad}))
    return "\n".join(lines)


def _slot(value):
    """A slot as the tests read it: a placeholder's marker, else its record's line."""
    return str(value) if isinstance(value, ax.JsonlPlaceholder) else value["line"]


def _payload(out: str, header: str):
    assert header in out, out[:600]
    return json.loads(out.split(header + "\n", 1)[1])


@pytest.mark.asyncio
async def test_an_invalid_json_lines_line_keeps_its_slot(monkeypatch):
    """An invalid line was dropped, so every later index moved - after
    an invalid line 12, ``$[20]`` read the record on line 22. Every line of the
    file keeps its slot: ``$[i]`` is line ``i + 1``, on the tool surface too."""
    raw = _jsonl(30, invalid=(12,)).encode()
    out, is_error = ax.run_json_query(raw, "events.jsonl", None, "$[20].line", None)
    assert not is_error and out.endswith("1 value matches '$[20].line':\n21")
    out, _ = ax.run_json_query(raw, "events.jsonl", None, "$[11]", None)
    assert out.endswith(
        "1 item matches '$[11]': line 12 of the file, which is not valid JSON (Expecting property name enclosed "
        "in double quotes at column 14) - it holds no value."
    )
    (block,) = ax.load_attachment_json(raw, "events.jsonl", None)
    assert [_slot(v) for v in block.data] == ["<invalid line 12>" if n == 12 else n for n in range(1, 31)]
    assert block.partial == "line 12 is not valid JSON (its slot holds <invalid line 12>)"
    assert "30 lines, $[i] = line i+1 of the file; 1 not valid JSON" in block.label
    ref = FileRef.from_bytes(raw, "events.jsonl", "application/x-ndjson")
    executor = _build(monkeypatch, ref)
    result = await _call(executor, "query_file_json", file_id=ref.file_id, path="$[20].line")
    assert result.is_error is False and result.content.endswith("1 value matches '$[20].line':\n21")


@pytest.mark.parametrize("invalid", [(1,), (15,), (30,), (1, 2, 15, 29, 30)])
def test_invalid_lines_first_in_the_middle_or_last_keep_the_numbering(invalid):
    raw = _jsonl(30, invalid=invalid).encode()
    (block,) = ax.load_attachment_json(raw, "events.jsonl", None)
    assert [_slot(v) for v in block.data] == [f"<invalid line {n}>" if n in invalid else n for n in range(1, 31)]
    for number in range(1, 31):
        out, _ = ax.run_json_query(raw, "events.jsonl", None, f"$[{number - 1}]", None)
        if number in invalid:
            assert f"1 item matches '$[{number - 1}]': line {number} of the file, which is not valid JSON" in out
        else:
            assert out.endswith(f"1 value matches '$[{number - 1}]':\n" + json.dumps(block.data[number - 1], indent=2))
    last, _ = ax.run_json_query(raw, "events.jsonl", None, "$[-1]", None)
    assert ("1 item matches '$[-1]': line 30 of the file, which is not valid JSON" in last) == (30 in invalid)
    if len(invalid) == 1:
        assert block.partial == f"line {invalid[0]} is not valid JSON (its slot holds <invalid line {invalid[0]}>)"
    else:
        assert block.partial == "5 lines are not valid JSON (lines 1, 2, 15, 29, 30 - each slot holds <invalid line N>)"


def test_a_blank_json_lines_line_keeps_its_slot_and_the_final_newline_is_none():
    """A blank line is a line (JSON Lines forbids it, ndjson tolerates it): it
    holds ``<blank line N>`` so the lines after it keep their numbers - a
    leading one too, which the verbatim rendering keeps. The newline ending
    the file, and the blank lines after the last record, are not lines."""
    lines = ["", json.dumps({"line": 2}), json.dumps({"line": 3}), "   ", json.dumps({"line": 5})]
    raw = ("\n".join(lines) + "\n\n\n").encode()
    (block,) = ax.load_attachment_json(raw, "rows.ndjson", None)
    assert [_slot(v) for v in block.data] == ["<blank line 1>", 2, 3, "<blank line 4>", 5]
    assert block.partial is None, "a blank line holds nothing a query could miss"
    assert "5 lines, $[i] = line i+1 of the file; 2 blank" in block.label
    out, _ = ax.run_json_query(raw, "rows.ndjson", None, "$[3]", None)
    assert out.endswith("1 item matches '$[3]': line 4 of the file, which is blank - it holds no value.")
    out, _ = ax.run_json_query(raw, "rows.ndjson", None, "$[1].line", None)
    assert out.endswith("1 value matches '$[1].line':\n2")
    assert ax.extract_attachment_text(raw, "rows.ndjson", None).split("\n") == lines


def test_slices_wildcards_and_descents_render_a_placeholder_in_its_slot():
    """A placeholder is never data: ``[*].field`` and ``..key`` never match it,
    and where a query lists the slots themselves (a slice, ``[*]``, ``..*``) it
    stands in its place, counted apart from the values."""
    raw = _jsonl(30, invalid=(13,), blank=(20,)).encode()
    window, _ = ax.run_json_query(raw, "events.jsonl", None, "$[10:15]", None)
    got = _payload(
        window,
        "5 items match '$[10:15]' (4 values and 1 line holding none, shown in its slot as \"<invalid line N>\"):",
    )
    assert [v if isinstance(v, str) else v["line"] for v in got] == [11, 12, "<invalid line 13>", 14, 15]
    everything, _ = ax.run_json_query(raw, "events.jsonl", None, "$[*]", None)
    got = _payload(
        everything,
        "30 items match '$[*]' (28 values and 2 lines holding none, shown in their slots as "
        '"<invalid line N>" or "<blank line N>"):',
    )
    assert [v if isinstance(v, str) else v["line"] for v in got] == [
        {13: "<invalid line 13>", 20: "<blank line 20>"}.get(n, n) for n in range(1, 31)
    ]
    records = [n for n in range(1, 31) if n not in (13, 20)]
    lines, _ = ax.run_json_query(raw, "events.jsonl", None, "$[*].line", None)
    assert _payload(lines, "28 values match '$[*].line':") == records
    scores, _ = ax.run_json_query(raw, "events.jsonl", None, "$..score", None)
    assert _payload(scores, "28 values match '$..score':") == records
    leaves, _ = ax.run_json_query(raw, "events.jsonl", None, "$..*", None)
    got = _payload(
        leaves,
        "142 items match '$..*' (140 values and 2 lines holding none, shown in their slots as "
        '"<invalid line N>" or "<blank line N>"):',
    )
    assert "<invalid line 13>" in got and "<blank line 20>" in got
    capped, _ = ax.run_json_query(_jsonl(300, invalid=(5,)).encode(), "events.jsonl", None, "$[*]", None)
    got = _payload(
        capped,
        "More than 200 items match '$[*]' (the first 200: 199 values and 1 line holding none, shown in "
        'its slot as "<invalid line N>"):',
    )
    assert len(got) == 200 and got[4] == "<invalid line 5>" and got[199]["line"] == 200


def test_a_slice_or_wildcard_selecting_one_placeholder_names_the_line():
    """A slice or a wildcard whose one match is a placeholder
    read "'$[*]' is line 1" - the path is not the line, it matched it."""
    raw = _jsonl(30, invalid=(12,)).encode()
    out, _ = ax.run_json_query(raw, "events.jsonl", None, "$[11:12]", None)
    assert "1 item matches '$[11:12]': line 12 of the file, which is not valid JSON (Expecting property" in out
    alone = b'{"id": 1, oops'
    out, _ = ax.run_json_query(alone, "one.jsonl", None, "$[*]", None)
    assert "1 item matches '$[*]': line 1 of the file, which is not valid JSON (" in out
    out, _ = ax.run_json_query(alone, "one.jsonl", None, "$..*", None)
    assert "1 item matches '$..*': line 1 of the file, which is not valid JSON (" in out


def test_a_file_of_placeholders_or_scalars_still_counts_its_lines():
    """The count line was a records-only feature, so a file
    of invalid lines only - or of scalars with an invalid line past the head -
    was summarised without saying how many of its lines hold nothing."""
    broken = "\n".join(f'{{"line": {n}, broken {"x" * 20}' for n in range(1, 2_001))
    rendered = ax.extract_attachment_text(broken.encode(), "bad.jsonl", None)
    listed = "lines 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, ... (the first 10 of 2,000)"
    assert f"2,000 lines, 2,000 not valid JSON ({listed})" in rendered
    assert "\n  // 2,000 items: 0 records, 2,000 invalid lines\n  <invalid line 1>,\n" in rendered
    scalars = "\n".join("oops" if n == 500 else str(n * 1_000) for n in range(1, 10_001))
    rendered = ax.extract_attachment_text(scalars.encode(), "numbers.jsonl", None)
    assert "\n  // 10,000 items: 9,999 records, 1 invalid line\n  1000,\n  2000,\n" in rendered
    structure, _ = ax.run_json_query(("\n" * 20 + "7").encode(), "rows.ndjson", None, None, None)
    assert "// 21 items: 1 record, 20 blank lines" in structure and "<blank line 1>" in structure


def test_the_field_statistics_and_the_counts_are_the_records_alone():
    """A placeholder is no row: the statistics - numbers, distinct values, min
    / max / sum, nulls - of a file with invalid and blank lines are those of
    its records, and the count line tells the lines apart."""
    records = [{"id": i, "team": "ab"[i % 2], "score": i, "note": None if i % 5 == 0 else "n"} for i in range(2_000)]
    clean = [json.dumps(r) for r in records]
    broken = [*clean[:3], "{broken", "", *clean[3:1_000], "nope", *clean[1_000:]]

    def statistics(text: str) -> list[str]:
        return [line for line in text.split("\n") if line.startswith(("  //   ", "  // fields:"))]

    whole = ax.extract_attachment_text("\n".join(clean).encode(), "events.jsonl", None)
    with_gaps = ax.extract_attachment_text("\n".join(broken).encode(), "events.jsonl", None)
    assert statistics(with_gaps) == statistics(whole) and len(statistics(whole)) == 5
    assert '  //   note: 1 distinct: "n" (1600); null 400' in statistics(with_gaps)
    assert "\n  // 2,000 records\n" in whole
    assert "\n  // 2,003 items: 2,000 records, 2 invalid lines, 1 blank line\n" in with_gaps
    assert "\n  <invalid line 4>,\n  <blank line 5>,\n" in with_gaps
    header = with_gaps.split("\n", 1)[0]
    assert "2,003 lines, 2 not valid JSON (lines 4, 1003), 1 blank (line 5)" in header
    kept = "(a line holding no value keeps its slot as <invalid line N> / <blank line N>)"
    assert f"$[i] being line i+1 of the file {kept}" in header


def test_the_rendering_numbers_the_lines_as_the_query_does():
    """The summary's head, its line report and the query name the same slot
    for the same line."""
    raw = _jsonl(2_000, invalid=(3, 1_500), blank=(7,), pad=10).encode()
    rendered = ax.extract_attachment_text(raw, "events.jsonl", None)
    assert "2,000 lines, 2 not valid JSON (lines 3, 1500), 1 blank (line 7)" in rendered.split("\n", 1)[0]
    head = [line.strip().rstrip(",") for line in rendered.split("\n") if line.startswith(("  {", "  <"))]
    assert len(head) == 12 and head[2] == "<invalid line 3>" and head[6] == "<blank line 7>"
    for index, item in enumerate(head):
        out, _ = ax.run_json_query(raw, "events.jsonl", None, f"$[{index}]", None)
        if item.startswith("<"):
            assert f"1 item matches '$[{index}]': line {index + 1} of the file, which is" in out
        else:
            assert out.endswith(f"1 value matches '$[{index}]':\n" + json.dumps(json.loads(item), indent=2))
    out, _ = ax.run_json_query(raw, "events.jsonl", None, "$[1499]", None)
    assert "1 item matches '$[1499]': line 1500 of the file, which is not valid JSON" in out


def test_the_caps_count_lines_with_their_placeholders(monkeypatch):
    """``JSONL_MAX_RECORDS`` and ``JSON_PARSE_MAX_CHARS`` bound the LINES held -
    a placeholder takes its slot like a record - and every line past them, a
    placeholder's included, is reported as not read."""
    monkeypatch.setattr(ax, "JSONL_MAX_RECORDS", 100)
    raw = _jsonl(200, invalid=(10, 150), blank=(50, 160)).encode()
    (block,) = ax.load_attachment_json(raw, "events.jsonl", None)
    assert len(block.data) == 100 and _slot(block.data[99]) == 100
    assert _slot(block.data[9]) == "<invalid line 10>" and _slot(block.data[49]) == "<blank line 50>"
    assert block.partial == (
        "only the first 100 of its 200 lines were parsed - the last 100 are not in these records; "
        "line 10 is not valid JSON (its slot holds <invalid line 10>)"
    )
    out, _ = ax.run_json_query(raw, "events.jsonl", None, "$[99].line", None)
    assert out.endswith("1 value matches '$[99].line':\n100")
    monkeypatch.setattr(ax, "JSONL_MAX_RECORDS", 50_000)
    monkeypatch.setattr(ax, "JSON_PARSE_MAX_CHARS", 2_000)
    text = _jsonl(100, invalid=(3,), blank=(4,))
    lines = ax._parse_jsonl(text)
    assert len(lines) + lines.unread == lines.lines == 100 and lines.invalid == 1 and lines.blank == 1
    physical = text.split("\n")
    parsed = sum(len(line.strip()) for line in physical[: len(lines)])
    cut = len(physical[len(lines)].strip())
    assert parsed <= 2_000 < parsed + cut, "the invalid line's text counts, and the cut is exact"


def test_a_file_opening_on_lines_holding_no_value_is_still_an_array_of_records():
    """The records-or-values decision sampled the first
    5,000 SLOTS, so a file opening on 5,000 invalid lines was summarised with
    its count line and nothing else - no field statistics, no record. It
    reads the values the statistics are computed over, and a head holding
    placeholders only names the first line holding a value, with its path."""
    lines = [f'{{"line": {n}, broken' for n in range(1, 5_001)]
    lines += [json.dumps({"line": n, "team": "ab"[n % 2]}) for n in range(5_001, 6_001)]
    raw = "\n".join(lines).encode()
    first = '// the first line holding a value is line 5001, $[5000]: {"line": 5001, "team": "b"}'
    rendered = ax.extract_attachment_text(raw, "events.jsonl", None)
    assert (
        "\n  // 6,000 items: 1,000 records, 5,000 invalid lines\n  // fields: line, team\n"
        "  //   line: number x1000: min 5,001, max 6,000, sum 5,500,500, mean 5,500.50\n"
        '  //   team: 2 distinct: "b" (500), "a" (500)\n'
        f"  {first}\n  <invalid line 1>,\n"
    ) in rendered
    structure, is_error = ax.run_json_query(raw, "events.jsonl", None, None, None)
    assert not is_error and "\n  // fields: line, team\n" in structure and first in structure
    out, _ = ax.run_json_query(raw, "events.jsonl", None, "$[5000]", None)
    assert out.endswith("1 value matches '$[5000]':\n" + json.dumps({"line": 5001, "team": "b"}, indent=2))
    one = [f'{{"line": {n}, broken' for n in range(1, 10_001)]
    one[6_999] = json.dumps({"line": 7_000})
    rendered = ax.extract_attachment_text("\n".join(one).encode(), "events.jsonl", None)
    assert (
        "\n  // 10,000 items: 1 record, 9,999 invalid lines\n  // fields: line\n"
        "  //   line: number x1: min 7,000, max 7,000, sum 7,000, mean 7,000\n"
        '  // the first line holding a value is line 7000, $[6999]: {"line": 7000}\n'
    ) in rendered
    structure, _ = ax.run_json_query(("\n" * 20 + "7").encode(), "rows.ndjson", None, None, None)
    assert "\n  // the first line holding a value is line 21, $[20]: 7\n  <blank line 1>,\n" in structure
    wide = ["{broken"] * 12 + [json.dumps({"note": "z" * 5_000})] + ["{broken"] * 6_000
    rendered = ax.extract_attachment_text("\n".join(wide).encode(), "wide.jsonl", None)
    (example,) = [line for line in rendered.split("\n") if "the first line holding a value" in line]
    assert example.startswith('  // the first line holding a value is line 13, $[12]: {"note": "zzz')
    assert example.endswith("zzz...") and len(example) < 500, "the example is cut at its bound"
    head_holds_one = ax.extract_attachment_text(_jsonl(2_000, invalid=(1, 2)).encode(), "events.jsonl", None)
    assert "the first line holding a value" not in head_holds_one, "a record in the head is its own example"


def test_a_summarised_file_of_twelve_lines_or_fewer_still_counts_its_placeholders():
    """The count line waited for more lines than the head
    shows, so a summarised file of 12 lines or fewer - one record past the
    verbatim bound and an invalid line - never said how many lines hold
    nothing. A file's lines holding placeholders always carry it."""
    text = json.dumps({"blob": "y" * 41_000}) + "\n{broken"
    rendered = ax.extract_attachment_text(text.encode(), "t.jsonl", None)
    assert rendered.startswith("[JSON Lines document,")
    assert rendered.split("\n", 1)[1].startswith("[\n  // 2 items: 1 record, 1 invalid line\n  {\n")
    assert rendered.endswith("\n  <invalid line 2>\n]")
    structure, _ = ax.run_json_query(text.encode(), "t.jsonl", None, None, None)
    assert "\n  // 2 items: 1 record, 1 invalid line\n" in structure
    clean = ax.extract_attachment_text(json.dumps({"blob": "y" * 41_000}).encode(), "t.jsonl", None)
    assert "// " not in clean, "a file holding no placeholder has no count line to give"


def test_the_statistics_read_the_rows_the_records_decision_read():
    """The statistics skip a placeholder, so past leading ones their sample
    ran further than the 5,000 slots the decision had read: a value there
    that is no record (a scalar line) reached ``for key in row``, the
    rendering fell back to the plain text and ``query_file_json`` without a
    path raised. The two read the same rows - 5,000 values that are not all
    records are no array of records, whatever holds the slots before them."""
    lines = [f'{{"line": {n}, broken' for n in range(1, 101)]
    lines += [json.dumps({"line": n}) for n in range(101, 5_001)]
    lines += ["5", *(json.dumps({"line": n}) for n in range(5_002, 5_201))]
    raw = "\n".join(lines).encode()
    summary = (
        "\n  // 5,200 items: 5,100 records, 100 invalid lines\n"
        '  // the first line holding a value is line 101, $[100]: {"line": 101}\n  <invalid line 1>,\n'
    )
    rendered = ax.extract_attachment_text(raw, "events.jsonl", None)
    assert rendered.startswith("[JSON Lines document,") and summary in rendered
    structure, is_error = ax.run_json_query(raw, "events.jsonl", None, None, None)
    assert not is_error and summary in structure
    assert "// fields:" not in rendered + structure


def test_the_csv_digest_says_how_much_of_the_file_it_read(monkeypatch):
    """The header promised a digest of the WHOLE file while
    the digest stopped at CSV_MAX_ROWS rows, and a row the csv module refuses
    ended it silently."""
    monkeypatch.setattr(ax, "CSV_MAX_ROWS", 1_000)
    text = "name,acv\n" + "\n".join(f"Account {i},{i}" for i in range(4_000))
    rendered = ax.extract_attachment_text(text.encode(), "a.csv", "text/csv")
    header = rendered.split("\n", 1)[0]
    assert "whole" not in header.lower()
    assert "a digest of its first 1,000 rows only (the digest stops at 1,000 rows)" in header
    assert "CSV: at least 1,000 data rows (the digest stops at 1,000 rows), 2 columns" in rendered
    big_field = "x" * 200_000
    broken = "a,b\n" + "\n".join(f"{i},v" for i in range(10)) + f'\n11,"{big_field}"\n' + "12,v\n"
    digest = ax.digest_csv_text(broken)
    assert "CSV: at least 10 data rows (parsing stopped after data row 10: field larger than field limit" in digest


def test_the_csv_digest_holds_only_its_statistics_sample(monkeypatch):
    """Every row used to become a dict before the statistics sampled 5,000 of
    them: a 40 MB export of 50,000 rows x 200 columns is ten million cells
    held at once."""
    coerced = {"n": 0}
    original = ax._coerce_cell

    def _counting(cell):
        coerced["n"] += 1
        return original(cell)

    monkeypatch.setattr(ax, "_coerce_cell", _counting)
    header = ",".join(f"c{j}" for j in range(100))
    text = header + "\n" + "\n".join(",".join(str(i) for _ in range(100)) for i in range(6_000))
    digest = ax.digest_csv_text(text)
    assert "CSV: 6,000 data rows, 100 columns" in digest
    assert "(statistics for the first 64 of 100 columns)" in digest
    assert "fields: c0, c1," in digest and "... (100 fields)" in digest
    assert "(statistics over the first 5,000 of 6,000 rows)" in digest
    assert coerced["n"] <= ax.STATS_SAMPLE_ROWS * 64


def test_a_large_json_file_that_does_not_parse_says_so():
    broken = ('{"k": "' + "x" * 50_000).encode()
    out = ax.extract_attachment_text(broken, "dump.json", "application/json")
    assert out.startswith("[JSON document, 50,007 chars that could not be read as JSON - not valid JSON (")
    assert out.endswith("x" * 100)


def test_a_page_that_never_closes_its_title_keeps_its_text():
    """The 3.14 tokenizer reads <title> as RCDATA up to </title>: without one the
    whole page was the title's raw text and rendered EMPTY, silently."""
    page = "<html><head><title>Plan FY27<body><h1>Coverage</h1><p>Three pods.</p><title>x<p>y</p></body></html>"
    out = ax.render_html_attachment(page)
    assert out.startswith("# Plan FY27\n\n[Rendered from ")
    assert "its <title> is never closed (a browser shows such a page blank)" in out
    assert "# Coverage" in out and "Three pods." in out
    # Only the FIRST <title> is RCDATA: a later one - or one inside <svg>,
    # which a browser parses as markup - never swallows the rest of the page.
    assert "<p>" not in out and out.endswith("x\ny")
    assert ax.render_html_attachment("<title>A</title><p>one</p><title>B<p>two</p>") == "# A\n\none\n\nB\ntwo"
    svg = ax.render_html_attachment("<p>before</p><svg><title>Chart<path d='M0'/></svg><p>after</p>")
    assert svg.endswith("before\n\nafter") and "unclosed" not in svg


def test_the_page_after_an_unclosed_title_is_read_from_its_source():
    """What an unclosed ``<title>`` swallowed is read again from the SOURCE,
    not from the RCDATA the tokenizer already decoded: decoded twice, escaped
    markup became markup - a code sample turned into a JSON block the page
    never carried and ``&amp;lt;`` into ``<``. The page reads as it does with
    the title closed."""
    body = (
        '<p>1 &amp;lt; 2 and &lt;script type="application/json"&gt;{"a": 1}&lt;/script&gt; end</p>'
        "<p>&amp;#65; &#66;</p>"
    )
    doc = ax.extract_html_document("<title>T &amp; &#67;" + body)
    closed = ax.extract_html_document("<title>T &amp; &#67;</title>" + body)
    assert doc.title == closed.title == "T & C" and doc.title_unclosed
    assert doc.text == closed.text
    assert '1 &lt; 2 and <script type="application/json">{"a": 1}</script> end' in doc.text
    assert "&#65; B" in doc.text and doc.json_blocks == closed.json_blocks == []


def test_the_title_read_before_an_end_tag_the_tokenizer_cannot_finish_is_kept():
    """``</title`` with no ``>`` after it is an end tag the tokenizer never
    finishes: it has handed the title's text over, decoded, and keeps only the
    tag - so that text is the title's head (re-read from what the tokenizer
    still held, the title started at the tag and came out empty), each
    reference in it decoded once."""
    assert ax.render_html_attachment("<title>Plan</title more").startswith("# Plan\n")
    page = f'<title>A &#{"0" * 5_000}66; &amp;lt; C</title x="<p>body</p>'
    doc = ax.extract_html_document(page)
    assert doc.title == "A B &lt; C" and doc.title_unclosed and doc.text == ""


def test_an_unclosed_title_ends_at_its_first_tag_not_at_a_bare_less_than():
    """A ``<`` that opens no markup (followed by a space or a digit) is text to
    the tokenizer, so it does not end an unclosed title: the title runs to the
    first tag - with no tag after it, to the end, as a browser reads it."""
    doc = ax.extract_html_document("<title>1 < 2 <3 <p>x</p>")
    assert doc.title == "1 < 2 <3" and doc.text == "x"
    assert ax.extract_html_document("<title>a <3 b").title == "a <3 b"
    # ``</`` opens an end tag only with a letter after it: ``</ 2`` is text.
    doc = ax.extract_html_document("<title>Budget </ 2 <p>x</p>")
    assert doc.title == "Budget </ 2" and doc.text == "x"
    assert ax.extract_html_document("<title>Budget </ 2").title == "Budget </ 2"


def test_the_unclosed_title_recovery_rests_on_the_tokenizers_rcdata_state():
    """The recovery reads what the stdlib tokenizer still holds when a page
    ends inside its first ``<title>`` (RCDATA), pinned here so a Python
    release that changes it fails this test rather than the pages: without an
    end tag nothing is emitted and the rest of the page is kept as the source
    writes it; before an end tag it cannot finish, the title's text is
    emitted, decoded, and only the tag is kept; ``close()`` hands what was
    kept over DECODED - fed again as markup, it was decoded twice."""

    class Recorder(ax.HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.data: list[str] = []

        def handle_data(self, data: str) -> None:
            self.data.append(data)

    rest = 'T &amp; <p>1 &amp;lt; 2</p><script type="application/json">{"v": "&#65;"}</script>'
    parser = Recorder()
    parser.feed("<title>" + rest)
    assert parser.cdata_elem == "title" and parser.rawdata == rest and parser.data == []
    parser.close()
    assert parser.data == ['T & <p>1 &lt; 2</p><script type="application/json">{"v": "A"}</script>']
    parser = Recorder()
    parser.feed("<title>Plan &amp; co</title more")
    assert parser.cdata_elem == "title" and parser.rawdata == "</title more" and parser.data == ["Plan & co"]


def test_a_page_that_ends_inside_an_unclosed_element_says_so():
    out = ax.render_html_attachment("<p>Before.</p><svg><path d='M0 0'/><p>Swallowed text.</p>")
    assert "the page ends inside an unclosed <svg>, so the 15 characters of text from it to the end are not shown" in out
    out = ax.render_html_attachment("<p>Before.</p><script>var x = 1;<p>After.</p>")
    assert "the page ends inside an unclosed <script>" in out and "are read as script, not shown" in out
    assert "unclosed" not in ax.render_html_attachment("<p>a</p><svg><path/></svg><div hidden>h</div><p>b</p>")


def test_a_huge_page_is_read_in_part_and_says_so(monkeypatch):
    """The stdlib tokenizer costs ~0.5-1 s per MB of hostile markup, and a
    rendering past the cache's entry bound was redone on every turn: a 40 MB
    upload was tens of seconds of the parse pool per turn. The page is read up
    to HTML_RENDER_MAX_CHARS and its text kept up to HTML_TEXT_MAX_CHARS -
    each cut named in the accounting line and in a query's partial note."""
    monkeypatch.setattr(ax, "HTML_RENDER_MAX_CHARS", 2_000)
    monkeypatch.setattr(ax, "HTML_TEXT_MAX_CHARS", 300)
    scripts = '<script type="application/json">{"early": 1}</script>'
    late = '<script type="application/json">{"late": 2}</script>'
    page = "<html><body>" + scripts + "".join(f"<p>Paragraph {i} text.</p>" for i in range(200)) + late + "</body></html>"
    out = ax.render_html_attachment(page)
    assert f"only the first 2,000 chars were read - the last {len(page) - 2_000:,} (text and data alike)" in out
    assert "the page text stops after 300 chars (" in out
    body = out.split("## Page text\n\n", 1)[1]
    assert len(body) < 400 and "Paragraph 0 text." in body
    query, is_error = ax.run_json_query(page.encode(), "p.html", "text/html", "$.late", None)
    assert not is_error and query.startswith(f"[Partial: the last {len(page) - 2_000:,} chars of the page were not read")
    assert "No value matches" in query


def test_a_slice_costs_the_values_read_not_a_copy_of_the_array():
    import tracemalloc

    big = {"k": list(range(2_000_000))}
    tracemalloc.start()
    try:
        out = ax.query_json(big, "$.k[0:2000000]")
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(out) == ax.QUERY_MAX_RESULTS and out[:3] == [0, 1, 2]
    assert peak < 1_000_000, f"the slice copied the array ({peak:,} bytes)"
    assert ax.query_json({"k": [0, 1, 2, 3]}, "$.k[-2:]") == [2, 3]


def test_a_query_section_cut_at_the_output_budget_says_so(monkeypatch):
    monkeypatch.setattr(ax, "QUERY_RESULT_MAX_CHARS", 900)
    key = "k" * 300
    raw = json.dumps({key: {f"field_{j}": "v" * 200 for j in range(80)}}).encode()
    out, is_error = ax.run_json_query(raw, "big.json", "application/json", f"$.{key}", None)
    assert not is_error
    assert out.endswith("... [cut here: this call's output budget is spent - narrow the path or pass block=N]")
    assert len(out) <= 900


# ---------------------------------------------------------------------------
# Hostile and sloppy inputs
# ---------------------------------------------------------------------------


def test_a_lone_surrogate_escape_never_reaches_the_rendering():
    """``json.loads`` turns ``"\\ud83d"`` (half an emoji, what a UTF-16
    slicer leaves) into a lone surrogate, and a str carrying one cannot be
    encoded as UTF-8 - the upload, the LLM request and the database write
    would all raise. Every rendering and query result is encodable."""
    records = [{"id": i, "label": "cut \\ud83d here", "note": "x" * 60} for i in range(600)]
    raw = ("[" + ",".join(json.dumps(r).replace("\\\\ud83d", "\\ud83d") for r in records) + "]").encode()
    assert len(raw) > ax.JSON_VERBATIM_MAX_CHARS
    out = ax.extract_attachment_text(raw, "rows.json", "application/json")
    out.encode("utf-8")
    assert "\ufffd" in out
    query, is_error = ax.run_json_query(raw, "rows.json", "application/json", "$[0].label", None)
    assert not is_error
    query.encode("utf-8")
    page = '<script type="application/json">{"title": "half \\ud83d emoji"}</script><p>body</p>'
    rendered = ax.extract_attachment_text(page.encode(), "p.html", "text/html")
    rendered.encode("utf-8")
    assert "half \ufffd emoji" in rendered


def test_a_query_follows_every_record_a_step_reaches():
    """A step keeps no working set of its own: with 2,000 records where only
    record 1,500 carries ``b``, ``$[*].b`` finds it instead of saying "No
    value matches", and a result stopped at its cap says "more than"."""
    data = [{"a": i} for i in range(2_000)]
    data[1_500]["b"] = "the one"
    assert ax.query_json(data, "$[*].b") == ["the one"]
    rows = [{"i": i} for i in range(801)]
    rows[800]["target"] = "last"
    assert ax.query_json({"rows": rows}, "$.rows[*].target") == ["last"]
    outcome = ax.run_query(data, "$[*]")
    assert len(outcome.matches) == ax.QUERY_MAX_RESULTS and outcome.more and not outcome.exhausted
    raw = json.dumps(data).encode()
    out, is_error = ax.run_json_query(raw, "rows.json", "application/json", "$[*].b", None)
    assert not is_error and "1 value matches '$[*].b'" in out and '"the one"' in out
    out, _ = ax.run_json_query(raw, "rows.json", "application/json", "$[*].a", None)
    assert f"More than {ax.QUERY_MAX_RESULTS} values match '$[*].a'" in out


def test_a_query_past_its_visit_budget_says_it_searched_only_part():
    """``$..a..zzz`` walks the subtree under every ``a`` - quadratic on a
    nested document. One visit budget bounds every query, and a search it
    cut short is named as partial, never a definitive "No value matches"."""
    nested: dict = {"a": {}}
    node = nested["a"]
    for _ in range(300):
        node["a"] = {"v": 1}
        node = node["a"]
    outcome = ax.run_query(nested, "$..a..zzz", visit_budget=5_000)
    assert outcome.exhausted and outcome.matches == [] and outcome.visit_budget == 5_000
    text = ax.format_query_result(
        outcome.matches, path="$..a..zzz", exhausted=True, visit_budget=outcome.visit_budget
    )
    assert text.startswith("[Partial search: the visit budget (5,000 values) ran out")
    assert text.endswith("No value matches '$..a..zzz' in the part searched.")
    assert not ax.run_query(nested, "$.a.v").exhausted


def test_one_query_file_json_call_spends_one_visit_budget_across_its_blocks(monkeypatch):
    """With ``block`` omitted every block is queried - from ONE budget, so a
    file of 64 blocks never buys 64. A block reached after the budget ran
    out says it was not searched; a key-only path still answers."""
    monkeypatch.setattr(ax, "QUERY_VISIT_BUDGET", 50)
    block = '<script type="application/json">' + json.dumps({"k": 7, "rows": list(range(40))}) + "</script>"
    page = (block * 3).encode()
    out, is_error = ax.run_json_query(page, "p.html", "text/html", "$.rows[*]", None)
    assert not is_error
    first, second, third = out.split("\n\n[Block ")[1:4]
    assert "40 values match '$.rows[*]'" in first and "Partial search" not in first
    assert "[Partial search: the visit budget (50 values) ran out" in second and "10 values match" in second
    assert "not searched: this call's visit budget (50 values) was spent on the blocks above" in third
    keys, _ = ax.run_json_query(page, "p.html", "text/html", "$.k", None)
    assert keys.count("1 value matches '$.k'") == 3 and "Partial search" not in keys


def test_a_long_reference_inside_a_script_is_script_data_and_stays():
    """A reference inside a <script> is never decoded, so a JSON value
    ``"&#123456789;"`` is data and queries back byte for byte - on a page
    whose text carries a reference ``int()`` refuses as well."""
    page = '<script type="application/json">{"value": "&#123456789;"}</script><p>ok &#12345678901; done</p>'
    query, is_error = ax.run_json_query(page.encode(), "p.html", "text/html", "$.value", None)
    assert not is_error and '"&#123456789;"' in query
    out = ax.render_html_attachment(page)
    assert "ok \ufffd done" in out and "&#123456789;" in out
    refused = page.replace("</p>", " &#" + "7" * 5_000 + "; end</p>")
    query, is_error = ax.run_json_query(refused.encode(), "p.html", "text/html", "$.value", None)
    assert not is_error and '"&#123456789;"' in query
    assert "done \ufffd end" in ax.render_html_attachment(refused)
    # Even a reference past the tokenizer's limit is data inside a raw-text
    # element: only where references are decoded is it read as its value.
    huge = "&#" + "5" * 5_000 + ";"
    mixed = (
        f'<script type="application/json">{{"v": "{huge}"}}</script>'
        f"<style>.x {{ content: '{huge}' }}</style><p>text &#{'7' * 5_000}; end</p>"
    )
    query, is_error = ax.run_json_query(mixed.encode(), "p.html", "text/html", "$.v", None)
    assert not is_error and f'"{huge}"' in query
    assert "text \ufffd end" in ax.render_html_attachment(mixed)


def test_a_script_tag_the_tokenizer_does_not_read_as_one_exposes_no_raw_text():
    """``<script>`` inside a comment, an attribute value, the ``<title>`` or a
    CDATA section is no element for the tokenizer, and raw text is where the
    tokenizer itself says it is: the page after it reads, and the script
    data further on keeps a refused reference verbatim (a scan of the source
    that took these for script openings sent the page to a last pass that
    replaced every such reference, script data included)."""
    huge = "&#" + "5" * 5_000 + ";"
    script = f'<script type="application/json">{{"v": "{huge}"}}</script>'
    fools = ("<!-- <script> -->", '<p title="<script>">x</p>', "<title>T <script> T</title>", "<![CDATA[ <script> ]]>")
    for fool in fools:
        page = f"{fool}<p>x &#{'8' * 5_000}; y</p>{script}"
        out = ax.extract_attachment_text(page.encode(), "p.html", "text/html")
        assert "rendering of this file failed" not in out and "x \ufffd y" in out, fool
        query, _ = ax.run_json_query(page.encode(), "p.html", "text/html", "$.v", None)
        assert f'"{huge}"' in query, fool


def test_raw_text_keeps_even_a_reference_int_refuses_byte_for_byte():
    """A script body and ``<plaintext>`` are raw text the tokenizer never
    decodes, so every reference there is data, kept byte for byte - one
    ``int()`` refuses and a padded one included (shortened to ``&#65;`` it
    would be other data) - on a page whose text holds a refused one too
    (read again with them replaced, the script value came back as U+FFFD)."""
    refused = "&#" + "7" * 5_000 + ";"
    in_raw = "&#" + "5" * 5_000 + ";"
    padded = "&#" + "0" * 700 + "65;"
    page = (
        f'<script type="application/json">{{"value": "{in_raw}", "padded": "{padded}"}}</script>'
        f"<p>a {refused} b</p><plaintext>raw {in_raw} end"
    )
    query, is_error = ax.run_json_query(page.encode(), "p.html", "text/html", "$.value", None)
    assert not is_error and f'"{in_raw}"' in query
    query, _ = ax.run_json_query(page.encode(), "p.html", "text/html", "$.padded", None)
    assert f'"{padded}"' in query
    out = ax.render_html_attachment(page)
    assert "a \ufffd b" in out and f"raw {in_raw} end" in out


def test_a_page_carrying_a_hold_delimiter_still_holds_its_references():
    """A held reference becomes its index between two noncharacters the page
    does not carry, so a page carrying U+FDD0 or U+FDD1 - marker-shaped text
    included - is held with another pair: its own text reads as it is, a text
    reference decodes, and script and ``<plaintext>`` data keep the reference
    byte for byte. Only a page carrying 65 of the 66 noncharacters falls back
    to short forms in place
    (``test_a_page_carrying_nearly_every_noncharacter_falls_back_to_short_forms``)."""
    zeros = "0" * 700
    for delimiter in ("\ufdd0", "\ufdd1"):
        own = f"\ufdd00\ufdd1 {delimiter}"
        page = (
            f"<p>own {own} x &#{zeros}65; y &#{'9' * 5_000}; z</p>"
            f'<script type="application/json">{{"v": "&#{zeros}66;"}}</script>'
            f"<plaintext>raw &#{zeros}67; end"
        )
        out = ax.render_html_attachment(page)
        assert f"own {own} x A y \ufffd z" in out and f"raw &#{zeros}67; end" in out, ascii(delimiter)
        query, is_error = ax.run_json_query(page.encode(), "p.html", "text/html", "$.v", None)
        assert not is_error and f'"&#{zeros}66;"' in query, ascii(delimiter)


def test_a_reference_int_refuses_reads_as_a_browser_reads_it():
    """Held out of the tokenizer, a reference of any length decodes by the
    stdlib's own rules - leading zeros dropped, U+FFFD past the last code
    point, ``&#128;`` as the euro sign - in text, RCDATA and attribute
    values alike, with or without its ``;``. It read as U+FFFD whatever it
    meant."""
    zeros = "0" * 5_000
    too_large = "&#" + "9" * 5_000 + ";"
    page = (
        f"<title>T &#{zeros}65; T</title>"
        f'<script type="application/json" id="k{too_large}">{{"v": 1}}</script>'
        f"<p>x &#{zeros}65; y {too_large} z &#{zeros}66 w &#{zeros}; v &#{zeros}128; u</p>"
        f"<textarea>ta &#{zeros}65; ta</textarea>"
    )
    out = ax.render_html_attachment(page)
    assert out.startswith("# T A T")
    assert "x A y \ufffd z B w \ufffd v \u20ac u" in out and "ta A ta" in out
    query, _ = ax.run_json_query(page.encode(), "p.html", "text/html", None, None)
    assert 'id="k\ufffd"' in query


def test_a_reference_reads_the_same_whatever_digit_limit_the_process_set():
    """The digit limit is a process setting (``sys.set_int_max_str_digits``):
    lowered to 640 (the floor), left alone or lifted, a page reads the same -
    a padded reference is its character, an out-of-range one U+FFFD - and a
    long digit run never reaches ``int()``, which with the limit lifted spent
    seconds converting one."""
    import sys

    page = "<p>a &#" + "9" * 700 + "; b &#" + "0" * 598 + "65; c &#" + "0" * 700 + "66; d</p>"
    def run(digits: int) -> str:
        return "<p>&#" + "7" * digits + ";</p>"

    previous = sys.get_int_max_str_digits()
    try:
        for limit in (640, previous, 0):
            sys.set_int_max_str_digits(limit)
            assert ax.render_html_attachment(page).endswith("a \ufffd b A c B d"), limit
        assert ax.render_html_attachment(run(4_000_000)) == "\ufffd"
        growth = _growth(ax.render_html_attachment, run, 4_000_000)
    finally:
        sys.set_int_max_str_digits(previous)
    assert growth < _LINEAR


def test_a_page_with_a_refused_reference_is_tokenized_once(monkeypatch):
    """The refused reference is held out before the tokenizer, never met by
    it, so the page is read in ONE pass: a retry after the tokenizer refused
    it read the page twice (an 8 MB page refused at its end took twice the
    tokenizer time)."""
    fed: list[int] = []
    feed = ax.HTMLParser.feed

    def counting_feed(self, data):
        fed.append(len(data))
        return feed(self, data)

    monkeypatch.setattr(ax.HTMLParser, "feed", counting_feed)
    page = "<p>x</p>" * 100 + "<p>&#" + "7" * 5_000 + ";</p>"
    assert ax.render_html_attachment(page).endswith("\ufffd")
    assert len(fed) == 1


def test_an_unclosed_title_whose_text_decodes_into_a_refused_reference_still_renders():
    """What an unclosed ``<title>`` swallowed was decoded as its RCDATA and
    read again as markup, so ``&amp;#`` and 5,000 digits became a reference
    the second read refused: the rendering fell back to the plain text and
    ``query_file_json`` raised. The second read is of the source, where it is
    the text ``&#`` and digits."""
    page = "<title>T <p>x &amp;#" + "5" * 5_000 + "; y</p>"
    out = ax.extract_attachment_text(page.encode(), "p.html", "text/html")
    assert "rendering of this file failed" not in out and "its <title> is never closed" in out
    assert f"x &#{'5' * 5_000}; y" in out
    query, is_error = ax.run_json_query(page.encode(), "p.html", "text/html", "$", None)
    assert is_error and "carries no JSON to query" in query


#: The 66 Unicode noncharacters, the candidates for a held reference's delimiters.
_NONCHARACTERS = [chr(code) for code in range(0xFDD0, 0xFDF0)] + [
    chr(plane << 16 | low) for plane in range(17) for low in (0xFFFE, 0xFFFF)
]


def test_a_page_carrying_noncharacters_still_holds_its_references():
    """A held reference is its index between two noncharacters the page does
    NOT carry, so one it does carry - U+FDD0, U+FDD1, a forged index, all 32
    of U+FDD0..U+FDEF - is page text like any other: kept as written, never
    read as a held index, and the page's script data and ``<plaintext>`` keep
    every reference byte for byte. One U+FDD0 anywhere used to send the page
    to shortening every long reference in place, script data included."""
    huge = "&#" + "5" * 5_000 + ";"
    padded = "&#" + "0" * 5_000 + "65;"
    for carried in ("\ufdd0", "\ufdd1", "\ufdd00\ufdd1", "".join(_NONCHARACTERS[:32])):
        page = (
            f"<p>x {carried} {padded} y</p><!-- {carried} -->"
            f'<script type="application/json">{{"v": "{huge}", "p": "{padded}", "c": "{carried}"}}</script>'
            f"<plaintext>raw {padded} {carried} end"
        )
        out = ax.render_html_attachment(page)
        assert f"x {carried} A y" in out and f"raw {padded} {carried} end" in out, repr(carried)
        for path, value in (("$.v", huge), ("$.p", padded), ("$.c", carried)):
            query, is_error = ax.run_json_query(page.encode(), "p.html", "text/html", path, None)
            assert not is_error and f'"{value}"' in query, (repr(carried), path)


def test_a_page_carrying_nearly_every_noncharacter_falls_back_to_short_forms():
    """With fewer than two of the 66 noncharacters absent from the page there
    is no pair to delimit a held index with: each long reference is shortened
    in place instead - the same value where the tokenizer decodes, its raw
    text shortened - and the page still reads, no held index confused with
    its text. Two absent are enough to hold."""
    padded = "&#" + "0" * 5_000 + "65;"
    for carried, raw in (
        (_NONCHARACTERS, "&#65;"),
        (_NONCHARACTERS[1:], "&#65;"),
        (_NONCHARACTERS[2:], padded),
    ):
        text = "".join(carried)
        page = f'<p>x {padded} y</p><!--{text}--><script type="application/json">{{"p": "{padded}"}}</script>'
        out = ax.render_html_attachment(page + f"<plaintext>raw {padded} end")
        assert "rendering of this file failed" not in out and "x A y" in out, len(carried)
        assert f"raw {raw} end" in out, len(carried)
        query, is_error = ax.run_json_query(page.encode(), "p.html", "text/html", "$.p", None)
        assert not is_error and f'"{raw}"' in query, len(carried)


def test_a_padded_page_renders_exactly_like_its_short_form():
    """A held reference is put back where the tokenizer hands its text over,
    BEFORE any rule of the reader, so its value meets the same rules as the
    short form's: a table cell escapes the ``|`` it decodes to, the title
    stays one line above the rendering's own note, whitespace folds, a
    ``<pre>`` line breaks - put back after the reader, a padded ``&#124;``
    forged a column and a padded ``&#10;`` a line of the page's choosing."""
    template = (
        "<title>Plan{nl}## Injected</title>"
        "<table><tr><td>a{pipe}b</td><td>c</td></tr></table>"
        "<h2>x{sp}{sp}y{sp}</h2><p>x{sp}{sp}y{sp}</p><ul><li>one{nl}two</li></ul><pre>one{nl}two</pre>"
    )
    short = template.format(nl="&#10;", pipe="&#124;", sp="&#32;")
    pad = "0" * 5_000
    padded = template.format(nl=f"&#{pad}10;", pipe=f"&#{pad}124;", sp=f"&#{pad}32;")
    rendered = ax.render_html_attachment(short)
    assert rendered.startswith("# Plan ## Injected\n") and "| a\\|b | c |" in rendered
    assert "## x y\n" in rendered and rendered.endswith("- one two\n\none\ntwo")
    assert ax.render_html_attachment(padded) == rendered


@pytest.mark.parametrize(
    "template",
    [
        "<p>a {r} b {rn}bc</p>",
        "<title>T {r} T</title><textarea>t {r} t</textarea>",
        '<script type="application/json" id="i{r}" data-x={r}>{{"v": "{r}"}}</script>',
        '<script>const ROWS = [{{"v": "{r}", "pad": "' + "x" * 400 + '"}}];</script>',
        "<style>p {{ content: '{r}' }}</style><xmp>{r}</xmp><noscript>{r}</noscript><p>t</p>",
        "<p>t</p><plaintext>raw {r} end",
        "<plaintext/>parsed {r}<script/>parsed {r}",
        "<!-- {r} --><!DOCTYPE html {r}><?pi {r}?><![CDATA[ {r} ]]>"
        "<svg><![CDATA[ {r} ]]><title>{r}</title></svg><math>{r}</math><p>t</p>",
        '<div hidden="{u}ntil-found">shown</div><div hidden={u}ntil-found>shown too</div>',
        "<title>A {r} <p>x {r} y</p>",
        '<p>t</p><script type="application/json">{{"v": "{r}"}}',
    ],
)
def test_a_padded_reference_reads_like_its_short_form_in_every_context(template):
    """Held or not, a reference reads by the tokenizer's own state: its value
    where the tokenizer decodes (text, RCDATA, attribute values, a
    ``<plaintext/>`` or ``<script/>`` the tokenizer does not open), itself
    where it does not (a script body, ``<plaintext>``), nothing where the
    reader shows nothing (comments, declarations, CDATA sections, skipped
    subtrees) - exactly what its short form reads, padding aside."""

    def read(pad: str) -> str:
        page = template.format(r=f"&#{pad}65;", rn=f"&#{pad}65", u=f"&#{pad}117;")
        doc = ax.extract_html_document(page)
        blocks = [json.dumps(block.data) for block in doc.json_blocks]
        seen = [doc.title, doc.text, blocks, doc.stripped, doc.title_unclosed, doc.unclosed and doc.unclosed[0]]
        return json.dumps(seen).replace("&#" + pad, "&#")

    assert read("0" * 5_000) == read("")


def test_a_script_id_is_inert_text_in_its_block_label():
    """A script's ``id`` is page content the block label quotes: a line break
    in it (``&#10;``, padded or not, or a literal newline), a separator or a
    format character ended the platform's ``### Block`` line and started a
    line of the page's choosing inside ``## Embedded data``. It reads as one
    quoted line, its quotes escaped, its length bounded."""
    pad = "0" * 5_000
    for brk in ("&#10;", f"&#{pad}10;", "\n", "&#13;", "&#8232;", "\x85"):
        page = f'<script type="application/json" id="x{brk}## Forged &quot;q&quot;&#8238;">{{"v": 1}}</script><p>t</p>'
        out = ax.render_html_attachment(page)
        assert not [line for line in out.splitlines() if line.startswith("## Forged")], ascii(brk)
        assert '### Block 1: application/json id="x ## Forged \\"q\\"" (8 chars' in out, ascii(brk)
    long_id = "a" * 500
    page = f'<script type="application/json" id="{long_id}">{{"v": 1}}</script>'
    label = next(line for line in ax.render_html_attachment(page).splitlines() if line.startswith("### Block 1"))
    assert len(label) < 200 and label.count("a") < 100 and '..." (' in label
    # Every format character (Unicode category Cf) is left out, not only the
    # usual bidi / zero-width ranges: an Arabic letter mark reorders the label,
    # a word joiner or a soft hyphen hides in it.
    for control in ("\u061c", "\u2060", "\u2064", "\u00ad", "\u180e", "\u206a", "\ufff9", "\U000e0001"):
        page = f'<script type="application/json" id="x{control}y">{{"v": 1}}</script>'
        label = next(line for line in ax.render_html_attachment(page).splitlines() if line.startswith("### Block 1"))
        assert '### Block 1: application/json id="x y" (' in label, ascii(control)


def test_a_json_file_is_read_as_utf8_whatever_charset_it_declares():
    """RFC 8259 makes JSON UTF-8 and gives its media type no charset a
    compliant reader honours: a UTF-8 file served as ``charset=iso-8859-1``
    reads as its characters, not as mojibake. A page still follows its
    declaration."""
    raw = '{"city": "Montr\u00e9al", "pad": "' + "x" * 50 + '"}'
    data = raw.encode("utf-8")
    out = ax.extract_attachment_text(data, "c.json", "application/json; charset=iso-8859-1")
    assert "Montr\u00e9al" in out and "Montr\u00c3" not in out
    query, _ = ax.run_json_query(data, "c.json", "application/json; charset=iso-8859-1", "$.city", None)
    assert '"Montr\u00e9al"' in query
    assert ax.decode_attachment_text("caf\u00e9".encode("cp1252"), "application/json", utf8_first=True) == "caf\u00e9"


def test_an_endless_character_reference_does_not_break_the_page():
    """``&#`` and 5,000 digits: the stdlib tokenizer converts it with
    ``int()``, which refuses more than 4,300 digits, so the whole rendering
    fell back to the raw source and a query raised. It reads as U+FFFD, what
    the tokenizer makes of any out-of-range reference."""
    page = "<title>T</title><p>before &#" + "1" * 5_000 + "; after</p><p>next &#x" + "f" * 5_000 + "; end</p>"
    out = ax.render_html_attachment(page)
    assert "before \ufffd after" in out and "next \ufffd end" in out
    assert "rendering of this file failed" not in ax.extract_attachment_text(page.encode(), "p.html", "text/html")
    query, is_error = ax.run_json_query(page.encode(), "p.html", "text/html", "$", None)
    assert is_error and "carries no JSON to query" in query
    assert "A" in ax.render_html_attachment("<p>&#0000065;</p>")


def test_a_json_text_past_the_parse_cap_is_reported_not_parsed(monkeypatch):
    """Parsed JSON weighs up to ~25 times its text: past the cap a document is
    shown as its text with the reason, a data block is skipped with it, and
    the JSON Lines records stop there with the rest counted as unread."""
    monkeypatch.setattr(ax, "JSON_PARSE_MAX_CHARS", 1_000)
    monkeypatch.setattr(ax, "JSON_VERBATIM_MAX_CHARS", 100)
    doc = json.dumps([{"id": i, "v": "x" * 20} for i in range(100)])
    out = ax.render_json_attachment(doc)
    assert out.startswith(f"[JSON document, {len(doc):,} chars not summarised - too large to parse here")
    query, is_error = ax.run_json_query(doc.encode(), "d.json", "application/json", "$[0]", None)
    assert not is_error and "skipped: too large to parse here" in query
    lines = "\n".join(json.dumps({"id": i, "v": "y" * 20}) for i in range(100))
    records = ax._parse_jsonl(lines)
    assert records.lines == 100 and records.invalid == 0 and records.unread == 100 - len(records)
    assert 0 < len(records) < 40
    assert "not read" in ax.render_jsonl_attachment(lines)


def test_a_huge_title_cannot_push_the_page_out_of_the_rendering():
    """The title heads the rendering: 90,000 characters of it would push the
    text and the data past the inline cap."""
    page = "<title>" + "T" * 90_000 + '</title><script type="application/json">{"k": 1}</script><p>Body text.</p>'
    out = ax.render_html_attachment(page)
    first = out.split("\n", 1)[0]
    assert first.startswith("# TTT") and first.endswith("...") and len(first) <= 2 + 300
    assert "Body text." in out and "## Embedded data" in out and len(out) < 5_000


def test_script_past_the_scan_budget_is_named_even_when_no_block_was_found(monkeypatch):
    """No block in the part scanned is not "no data": a literal past the
    budget would otherwise vanish without a word."""
    monkeypatch.setattr(ax, "_JS_SCAN_MAX_CHARS", 1_000)
    page = "<script>" + "var x = 1;\n" * 500 + "</script><p>Body.</p>"
    out = ax.render_html_attachment(page)
    assert "## Embedded data\nNo data block found in the part of the page's scripts that was scanned" in out
    assert "chars past the scan budget were not read" in out


def test_a_repeated_csv_header_keeps_both_columns():
    """Two ``amount`` columns would key into one field of every row, the
    second silently overwriting the first."""
    text = "amount,amount,amount_2,amount\n" + "\n".join(f"{i},{i * 10},{i * 100},{i * 1000}" for i in range(1, 30))
    digest = ax.digest_csv_text(text) or ""
    assert "fields: amount, amount_3, amount_2, amount_4" in digest
    assert "amount: number x29: min 1, max 29" in digest
    assert "amount_3: number x29: min 10, max 290" in digest
    assert "amount_2: number x29: min 100, max 2,900" in digest
    wide = ",".join(["same"] * 5_000) + "\n" + ",".join(["1"] * 5_000)
    assert "5000 columns" in (ax.digest_csv_text(wide) or "")


def test_the_sampling_note_survives_a_cut_digest():
    """The note that the statistics cover a sample comes first, where no cut
    of the digest reaches it."""
    text = "name,score\n" + "\n".join(f"row{i},{i}" for i in range(6_000))
    digest = ax.digest_csv_text(text, max_chars=160) or ""
    assert "(statistics over the first 5,000 of 6,000 rows)" in digest
    assert digest.endswith("... (digest truncated)")


def test_a_json_parse_string_keeps_an_astral_character_written_as_two_escapes():
    """A JavaScript string is UTF-16: ``'\\ud83d\\ude00'`` is ONE character
    (what an ASCII-only minifier writes for an emoji). Decoded escape by
    escape it became two lone surrogates, read as two U+FFFD; a lone half
    still reads as U+FFFD."""
    payload = '{"e":"\\ud83d\\ude00","lone":"\\ud83d!","pad":"' + "x" * 400 + '"}'
    page = ("<script>const DATA = JSON.parse('" + payload + "');</script><p>body</p>").encode()
    query, is_error = ax.run_json_query(page, "p.html", "text/html", "$.e", None)
    assert not is_error and '"\U0001f600"' in query
    query, _ = ax.run_json_query(page, "p.html", "text/html", "$.lone", None)
    assert '"\ufffd!"' in query
    ax.extract_attachment_text(page, "p.html", "text/html").encode("utf-8")


def test_a_renamed_csv_column_never_takes_the_name_of_another_column():
    """A repeat is renamed around every name the file already uses: a real
    ``amount_2`` column keeps its name and its values, a blank header its
    position unless the file uses that name, and a cell past the header is
    keyed on the same terms instead of overwriting a column of that name."""
    assert ax._unique_columns(["amount", "amount", "amount_2"]) == ["amount", "amount_3", "amount_2"]
    assert ax._unique_columns(["", " ", "column_2"]) == ["column_1", "column_2_2", "column_2"]
    text = "amount,amount,amount_2\n" + "\n".join(f"{i},{i * 10},{i * 100}" for i in range(1, 30))
    digest = ax.digest_csv_text(text) or ""
    assert "amount_2: number x29: min 100, max 2,900" in digest
    assert "amount_3: number x29: min 10, max 290" in digest
    wider_rows = "a,column_3\n" + "\n".join(f"{i},{i * 10},{i * 100}" for i in range(1, 30))
    digest = ax.digest_csv_text(wider_rows) or ""
    assert "column_3: number x29: min 10, max 290" in digest
    assert "column_3_2: number x29: min 100, max 2,900" in digest
    def header(repeats: int) -> list[str]:
        return [f"a_{k}" for k in range(2, repeats + 2)] + ["a"] * repeats

    assert len(set(ax._unique_columns(header(50_000)))) == 100_000
    assert _growth(ax._unique_columns, header, 50_000) < _LINEAR


def test_a_utf16_or_utf32_json_without_a_bom_is_read_as_its_characters():
    """UTF-8 first is for bytes that ARE UTF-8: a JSON text starts with an
    ASCII character and never holds a NUL byte in UTF-8 (a control character
    must be escaped), so UTF-16 / UTF-32 without a BOM - valid UTF-8 as well
    when its text is ASCII - shows a NUL in its first two bytes, and
    ``json.detect_encoding`` reads which one it is (RFC 4627 s3, what
    ``json.loads`` does with bytes): declared or not, and whatever the
    declaration (``charset=utf-16`` alone decodes little-endian in Python).
    A UTF-8 file with a stray NUL past its start stays UTF-8."""
    doc = json.dumps({"city": "Paris", "pad": "x" * 50})
    for codec, declared in (
        ("utf-16-le", "; charset=utf-16le"),
        ("utf-16-le", ""),
        ("utf-16-be", "; charset=utf-16"),
        ("utf-32-le", ""),
        ("utf-32-be", "; charset=utf-32"),
    ):
        raw = doc.encode(codec)
        query, is_error = ax.run_json_query(raw, "c.json", "application/json" + declared, "$.city", None)
        assert not is_error and '"Paris"' in query, (codec, declared)
        assert ax.decode_attachment_text(raw, "application/json" + declared, utf8_first=True) == doc
    stray = '{"city": "Montr\u00e9al"}\x00'.encode()
    decoded = ax.decode_attachment_text(stray, "application/json; charset=iso-8859-1", utf8_first=True)
    assert decoded.startswith('{"city": "Montr\u00e9al"}')
    # Any JSON text: one character (two or four bytes), leading whitespace, a
    # second character past ASCII (RFC 4627's table assumed two ASCII ones),
    # an astral character (a surrogate pair in UTF-16) - read alike by the
    # decoding and the inline rendering; the query reads the pair as ONE.
    texts = ("1", "[]", '"\u00e9t\u00e9"', '"\u4e2d\u6587"', ' \n {"city": "Montr\u00e9al", "e": "\U0001f600"}')
    for text in texts:
        for codec in ("utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"):
            raw = text.encode(codec)
            assert ax.decode_attachment_text(raw, "application/json", utf8_first=True) == text, (text, codec)
            assert ax.extract_attachment_text(raw, "c.json", "application/json") == text.strip(), (text, codec)
    query, is_error = ax.run_json_query(texts[-1].encode("utf-16-be"), "c.json", None, "$.e", None)
    assert not is_error and '"\U0001f600"' in query
    # A UTF-8 text too short for a byte order, or with a NUL past its first two
    # bytes, stays UTF-8 whatever it declares.
    for raw in (b"1", b"[]", b'""', b'{"a": "\x00"}', b"[1,\x002]", b"ab\x00"):
        assert ax.decode_attachment_text(raw, "application/json; charset=utf-16", utf8_first=True) == raw.decode()
    # A malformed text is read, never raised on: a lone surrogate or a cut last
    # unit is U+FFFD (never a lone surrogate, which no client can encode), and
    # what is then not JSON says so.
    lone = '{"a": "?"}'.encode("utf-16-le").replace("?".encode("utf-16-le"), b"\x3d\xd8")
    assert ax.decode_attachment_text(lone, "application/json", utf8_first=True) == '{"a": "\ufffd"}'
    cut = '{"a": "b"}'.encode("utf-16-be")[:-1]
    assert ax.decode_attachment_text(cut, "application/json", utf8_first=True) == '{"a": "b"\ufffd'
    query, _ = ax.run_json_query(cut, "c.json", None, "$.a", None)
    assert "not valid JSON" in query
    # JSON Lines bytes are JSON texts, one per line: the same rule.
    lines = '{"city": "Paris"}\n{"city": "Montr\u00e9al"}'
    for codec in ("utf-16-le", "utf-16-be", "utf-32-be"):
        raw = lines.encode(codec)
        assert ax.extract_attachment_text(raw, "c.jsonl", None) == lines, codec
        query, is_error = ax.run_json_query(raw, "c.jsonl", None, "$[1].city", None)
        assert not is_error and '"Montr\u00e9al"' in query, codec


def test_a_key_of_non_ascii_digits_is_a_key_not_an_index():
    """``str.isdigit`` accepts ``\u00b2``, which ``int`` refuses: over an array
    it raised out of the query instead of matching nothing. An index is
    ASCII digits in the bracket forms too, where ``\\d`` took any Unicode
    decimal digit."""
    assert ax.query_json([1, 2, 3], "$['\u00b2']") == []
    assert ax.query_json({"\u00b2": "squared"}, "$['\u00b2']") == ["squared"]
    assert ax.query_json([1, 2, 3], "$['1']") == [2]
    for path in ("$[\u0663]", "$[\u0661:\u0663]", "$[-\u0661]"):
        with pytest.raises(ax.JsonPathError):
            ax.query_json([1, 2, 3, 4], path)
    assert ax.query_json([1, 2, 3, 4], "$[1:3]") == [2, 3] and ax.query_json([1, 2, 3, 4], "$[-1]") == [4]
    # Every look-alike in every form an index takes - Arabic-Indic, full-width
    # and Devanagari digits are Unicode decimals ``\d`` took, a superscript is
    # not - refused with the supported forms named on the tool's path too (a
    # JSON file, the records of a JSON Lines file); quoted, the same
    # characters are an object's key, never an index.
    records = b'{"k": 1}\n{"k": 2}\n{"k": 3}\n{"k": 4}\n'
    for digit in ("\u0663", "\uff13", "\u0969", "\u00b2"):
        for path in (f"$[{digit}]", f"$[-{digit}]", f"$[{digit}:]", f"$[:{digit}]", f"$.a[{digit}].k"):
            with pytest.raises(ax.JsonPathError, match="unsupported path syntax"):
                ax.query_json([1, 2, 3, 4], path)
            for raw, name in ((b"[1, 2, 3, 4]", "a.json"), (records, "a.jsonl")):
                out, is_error = ax.run_json_query(raw, name, None, path, None)
                assert is_error and "Supported: $.key, ['key'], [0], [-1], [1:3]" in out, (path, name)
        for quoted in (f"$['{digit}']", f'$["{digit}"]'):
            assert ax.query_json([1, 2, 3, 4], quoted) == [] and ax.query_json({digit: "k"}, quoted) == ["k"]
    out, is_error = ax.run_json_query(records, "a.jsonl", None, "$[3].k", None)
    assert not is_error and out.endswith("\n4")
