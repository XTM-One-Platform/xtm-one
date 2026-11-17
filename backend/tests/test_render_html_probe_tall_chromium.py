"""The probe of ``render_html_to_image`` covers a figure taller than the canvas cap, in a real Chromium.

The canvas is at most ``MAX_CANVAS_PX`` high. A board taller than that (a long
table mockup) used to be probed on its first 4096 px only, so a defect lower
down never surfaced and the reviewer cut the HTML into slices by hand. The PNG
keeps the cap; the probe measures the whole document and reports a table cell
whose text wraps to more lines than its column (``WRAP``).

Skipped where Playwright's Chromium is not installed; the ``Browser tool (Chromium)``
CI job installs it and sets ``REQUIRE_CHROMIUM=1``, which turns the skip into a failure.
"""

from __future__ import annotations

import os

import pytest

from app.services.builtin_tools.html_render import MAX_CANVAS_PX, format_coverage, format_probe, render_html

pytestmark = pytest.mark.chromium

WIDTH = 800
ROW_PX = 40
ROWS = 200  # 200 rows of 40 px: an 8000 px board
WRAPPED_ROW = 150  # about y = 6000


def _board(rows: int, wrapped_row: int | None) -> str:
    cells = []
    for i in range(rows):
        name = "A name that wraps to two lines" if i == wrapped_row else f"Row {i}"
        cells.append(f'<tr><td class="name">{name}</td><td class="status">Ready</td></tr>')
    return (
        '<table style="border-collapse:collapse;font:14px/20px sans-serif;table-layout:fixed;width:400px">'
        '<colgroup><col style="width:200px"><col style="width:200px"></colgroup>'
        "<tbody>" + "".join(cells) + "</tbody></table>"
        f"<style>td{{padding:10px 8px;height:{ROW_PX - 20}px;vertical-align:top}}</style>"
    )


async def _render(html: str, height: int) -> tuple[bytes, dict]:
    try:
        return await render_html(html, width=WIDTH, height=height, scale=1, font_css="")
    except Exception as exc:
        text = str(exc)
        if "Executable doesn't exist" not in text and "playwright install" not in text.lower():
            raise
        if os.environ.get("REQUIRE_CHROMIUM") == "1":
            pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
        pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")


async def test_a_wrapped_cell_at_6000_px_of_an_8000_px_board_is_reported():
    png, probe = await _render(_board(ROWS, WRAPPED_ROW), MAX_CANVAS_PX)

    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert probe["measured"]["height"] >= ROWS * ROW_PX
    assert [item["el"] for item in probe["wrap"]] == ['td.name "A name that wraps to two lines"']
    assert probe["wrap"][0]["lines"] == 2
    assert probe["wrap"][0]["column"] == 1
    assert probe["wrap"][0]["top"] > MAX_CANVAS_PX
    # Nothing below the cap is reported as outside: the document is the area.
    assert probe["out"] == []
    text, total = format_probe(probe, width=WIDTH, height=MAX_CANVAS_PX)
    assert total == 1
    assert text.startswith('WRAP td.name "A name that wraps to two lines": text runs to 2 lines')
    assert "the probe measured all" in format_coverage(probe, height=MAX_CANVAS_PX)


async def test_a_board_under_the_cap_is_probed_on_its_canvas_as_before():
    # 60 rows on a 1080 px canvas: the rows below the canvas are OUT findings
    # as they always were, and no whole-document measure is taken.
    _png, probe = await _render(_board(60, None), 1080)

    assert "measured" not in probe
    assert probe["wrap"] == []
    assert probe["out"], "content past a canvas under the cap is still reported OUT"
    assert format_coverage(probe, height=1080) == ""


def _expanded_table(wrapped: bool) -> str:
    head = "".join(f"<th>Col {i}</th>" for i in range(6))
    rows = []
    for r in range(5):
        first = "A name long enough to wrap to two lines" if wrapped and r == 3 else f"Row {r}"
        rows.append(
            "<tr>" + f'<td class="name">{first}</td>' + "".join(f"<td>v{r}{c}</td>" for c in range(5)) + "</tr>"
        )
        if r == 1:
            rows.append(
                '<tr class="unfold"><td colspan="6"><p>An expanded row explains the request in a paragraph '
                "long enough to run over several lines of the panel, then shows two cards.</p>"
                '<div class="card">First card<br>with two lines</div><div class="card">Second card<br>with two lines</div>'
                "</td></tr>"
            )
    return (
        '<table style="border-collapse:collapse;font:14px/20px sans-serif;table-layout:fixed;width:720px">'
        f"<thead><tr>{head}</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
        "<style>td,th{padding:8px;vertical-align:top;width:120px}.card{border:1px solid #ccc;margin:4px 0}</style>"
    )


async def test_an_expanded_colspan_row_is_not_a_wrap_finding():
    # The unfold row of a data table (td colspan=6 holding a paragraph and
    # cards) runs to many lines by design: it is not compared with column 0.
    _png, probe = await _render(_expanded_table(wrapped=False), 900)

    assert probe["wrap"] == []


async def test_a_wrapped_cell_beside_an_expanded_row_is_still_reported():
    _png, probe = await _render(_expanded_table(wrapped=True), 900)

    assert [item["el"] for item in probe["wrap"]] == ['td.name "A name long enough to wrap to two lines"']
    assert probe["wrap"][0]["column"] == 1


async def test_a_short_figure_on_a_canvas_at_the_cap_keeps_its_canvas():
    _png, probe = await _render(_board(10, 4), MAX_CANVAS_PX)

    assert "measured" not in probe
    assert [item["lines"] for item in probe["wrap"]] == [2]
