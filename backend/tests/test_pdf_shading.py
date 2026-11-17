"""Viewer-safe gradients in a Chromium-printed PDF (issue #10727).

The calculator programs below are the ones Chromium printed for the deck's
``gradient`` hero (a linear wash and a radial bloom with translucent stops),
captured on main ``b4cdd9f1``. The last tests print real CSS gradients in
Chromium and check the rewritten PDF holds no FunctionType 4 and paints
the same pixels under pdfium; they skip where Playwright's Chromium is not
installed (``REQUIRE_CHROMIUM=1`` turns the skip into a failure).
"""

from __future__ import annotations

import io
import os
import re
import zlib

import pytest

from app.services import pdf_shading
from app.services.pdf_shading import (
    CalculatorError,
    evaluate_calculator,
    linear_pieces,
    make_shadings_viewer_safe,
    parse_calculator,
)

#: The hero's linear wash: x alone, two stops then the end colour, alpha last.
AXIAL = b"""{pop
dup 0 le {pop 0 .1475 .1663 .2 0} if
dup dup 0 gt exch 1 le and {
dup .47999999 le {
0 sub dup .00000064174333 mul 0 add exch dup -.30718946 mul .1475 add exch dup -.34640527 mul .1663 add exch -.41666666 mul .2 add
}{
.47999999 sub dup .031674337 mul exch dup .152337804 mul exch dup .28959277 mul exch .38461536 mul
} ifelse
0} if
0 gt {.0165 .0792 .1506 .2} if
dup abs 0.00001 lt{ pop pop pop pop 0 0 0 }{ dup 3 1 roll div 4 1 roll dup 3 1 roll div 4 1 roll div 3 1 roll} ifelse
}"""

#: The hero's radial bloom: the distance to the origin, transparent past 0.72.
RADIAL = b"""{dup mul exch dup mul add sqrt
dup 0 le {pop .0453 .2178 .4141 .55 0} if
dup dup 0 gt exch 1 le and {
dup .72000003 le {
0 sub dup -.062908754 mul .0453 add exch dup -.30255982 mul .2178 add exch dup -.57516342 mul .4141 add exch -.7638889 mul .55 add
}{
.72000003 sub pop 0 0 0 0
} ifelse
0} if
0 gt {0 0 0 0} if
dup abs 0.00001 lt{ pop pop pop pop 0 0 0 }{ dup 3 1 roll div 4 1 roll dup 3 1 roll div 4 1 roll div 3 1 roll} ifelse
}"""


def test_the_radial_bloom_evaluates_to_its_unpremultiplied_blue():
    program = parse_calculator(RADIAL)

    centre = evaluate_calculator(program, (0.0, 0.0), 3)

    # .0453 .2178 .4141 at alpha .55: #1565C0 (21, 101, 192)
    assert [round(v * 255) for v in centre] == [21, 101, 192]
    assert evaluate_calculator(program, (0.6, 0.8), 3) == (0.0, 0.0, 0.0)


def test_roll_and_index_follow_postscript():
    stack_program = parse_calculator(b"{.1 .2 .3 3 1 roll}")
    assert evaluate_calculator(stack_program, (), 3) == pytest.approx((0.3, 0.1, 0.2))
    assert evaluate_calculator(parse_calculator(b"{.1 .2 1 index}"), (), 3) == pytest.approx((0.1, 0.2, 0.1))


def test_an_unknown_operator_is_refused():
    with pytest.raises(CalculatorError):
        evaluate_calculator(parse_calculator(b"{1 2 frobnicate}"), (), 1)


@pytest.mark.parametrize(("source", "kind"), [(AXIAL, "axial"), (RADIAL, "radial")])
def test_the_hero_programs_are_recognised(source, kind):
    found = pdf_shading._profile(parse_calculator(source), 3, (-1.6, 1.4, -1.3, 2.0))

    assert found is not None
    assert found[0] == kind


def test_a_program_of_both_coordinates_is_left_alone():
    program = parse_calculator(b"{mul dup dup}")

    assert pdf_shading._profile(program, 3, (0.0, 1.0, 0.0, 1.0)) is None


def test_linear_pieces_cut_at_the_stops():
    values = [(min(i, 50) / 50, 0.0, 0.0) for i in range(101)]

    assert linear_pieces(values) == [0, 50, 100]


def test_a_pdf_without_calculator_functions_comes_back_unchanged():
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    out = io.BytesIO()
    writer.write(out)

    pdf = out.getvalue()

    assert make_shadings_viewer_safe(pdf) is pdf


def _soft_masked_pdf() -> bytes:
    """One page: a red square under a soft mask whose ``/TR`` is Chromium's ``{1 exch sub}``.

    No gradient anywhere: the mask group is a grey square, so the calculator
    function is the only FunctionType 4 of the file (issue #10748).
    """
    from pypdf import PdfWriter
    from pypdf.generic import (
        ArrayObject,
        BooleanObject,
        DecodedStreamObject,
        DictionaryObject,
        FloatObject,
        NameObject,
        NumberObject,
    )

    writer = PdfWriter()
    page = writer.add_blank_page(100, 100)

    transfer = DecodedStreamObject()
    transfer.set_data(b"{1 exch sub}")
    transfer.update(
        {
            NameObject("/FunctionType"): NumberObject(4),
            NameObject("/Domain"): ArrayObject([NumberObject(0), NumberObject(1)]),
            NameObject("/Range"): ArrayObject([NumberObject(0), NumberObject(1)]),
        }
    )
    group = DecodedStreamObject()
    group.set_data(b"0.3 g 10 10 80 80 re f")
    group.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Form"),
            NameObject("/BBox"): ArrayObject(NumberObject(v) for v in (0, 0, 100, 100)),
            NameObject("/Group"): DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Group"),
                    NameObject("/S"): NameObject("/Transparency"),
                    NameObject("/I"): BooleanObject(True),
                }
            ),
        }
    )
    transfer_ref = writer._add_object(transfer)
    state = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/ExtGState"),
            NameObject("/SMask"): DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Mask"),
                    NameObject("/S"): NameObject("/Alpha"),
                    NameObject("/G"): writer._add_object(group),
                    NameObject("/TR"): transfer_ref,
                }
            ),
            NameObject("/ca"): FloatObject(1),
        }
    )
    content = DecodedStreamObject()
    content.set_data(b"/G0 gs 1 0 0 rg 0 0 100 100 re f")
    page[NameObject("/Contents")] = writer._add_object(content)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/ExtGState"): DictionaryObject({NameObject("/G0"): writer._add_object(state)})}
    )
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_a_soft_mask_transfer_function_is_rewritten_and_paints_the_same():
    printed = _soft_masked_pdf()
    assert _calculator_functions(printed) == 1

    safe = make_shadings_viewer_safe(printed)

    assert _calculator_functions(safe) == 0
    assert re.search(rb"/FunctionType\s*2", safe)
    before, after = _pixels(printed), _pixels(safe)
    assert before.size == after.size
    # The mask is not a no-op: inside the grey square the red is cut by the inverted alpha.
    assert before.getpixel((50, 50)) != before.getpixel((5, 5))
    worst = max(
        abs(a - b)
        for pa, pb in zip(before.getdata(), after.getdata(), strict=True)
        for a, b in zip(pa, pb, strict=True)
    )
    assert worst <= 3


def test_a_stepped_transfer_function_becomes_a_stitching_of_linear_pieces():
    from pypdf.generic import ArrayObject, DecodedStreamObject, NameObject, NumberObject

    function = DecodedStreamObject()
    function.set_data(b"{dup .5 lt {2 mul} {pop 1} ifelse}")
    function.update(
        {
            NameObject("/FunctionType"): NumberObject(4),
            NameObject("/Domain"): ArrayObject([NumberObject(0), NumberObject(1)]),
            NameObject("/Range"): ArrayObject([NumberObject(0), NumberObject(1)]),
        }
    )

    sampled = pdf_shading._sampled_function(function)

    assert sampled["/FunctionType"] == 3
    assert [float(b) for b in sampled["/Bounds"]] == [0.5]
    assert [float(v) for v in sampled["/Functions"][0]["/C1"]] == [1.0]


# ── Real Chromium ──────────────────────────────────────────────────────────

HERO = """<html><body style='margin:0;background:#0b1020'>
<div style='width:960px;height:540px;background-color:#0b1020;background-image:radial-gradient(ellipse 62% 78% at 100% 100%, color-mix(in srgb, #1565c0 55%, transparent), transparent 72%), linear-gradient(135deg, color-mix(in srgb, #00bcd4 20%, transparent) 0%, transparent 48%, color-mix(in srgb, #1565c0 20%, transparent) 100%)'></div>
<div style='width:300px;height:100px;background:radial-gradient(circle,rgba(0,27,218,.5),transparent)'></div>
</body></html>"""


def _calculator_functions(pdf: bytes) -> int:
    blobs = [pdf]
    for match in re.finditer(rb"stream\r?\n(.*?)endstream", pdf, re.S):
        try:
            blobs.append(zlib.decompress(match.group(1)))
        except zlib.error:
            continue
    return sum(len(re.findall(rb"/FunctionType\s*4", blob)) for blob in blobs)


def _print(html: str) -> bytes:
    import asyncio

    try:
        from playwright.async_api import async_playwright
    except ImportError:  # pragma: no cover - Playwright is a dev dependency
        pytest.skip("Playwright is not installed")

    async def run() -> bytes:
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch()
            except Exception as exc:
                if "Executable doesn't exist" not in str(exc) and "playwright install" not in str(exc).lower():
                    raise
                if os.environ.get("REQUIRE_CHROMIUM") == "1":
                    pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
                pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")
            try:
                page = await browser.new_page()
                await page.set_content(html)
                return await page.pdf(print_background=True, width="10in", height="7in")
            finally:
                await browser.close()

    return asyncio.run(run())


def _pixels(pdf: bytes):
    import pypdfium2

    document = pypdfium2.PdfDocument(pdf)
    try:
        return document[0].render(scale=1).to_pil().convert("RGB")
    finally:
        document.close()


@pytest.mark.chromium
def test_a_printed_translucent_gradient_becomes_viewer_safe_and_paints_the_same():
    printed = _print(HERO)
    assert _calculator_functions(printed) > 0, "Chromium no longer prints calculator functions: drop the rewrite"

    safe = make_shadings_viewer_safe(printed)

    assert _calculator_functions(safe) == 0
    before, after = _pixels(printed), _pixels(safe)
    assert before.size == after.size
    worst = max(
        abs(a - b)
        for pa, pb in zip(before.getdata(), after.getdata(), strict=True)
        for a, b in zip(pa, pb, strict=True)
    )
    assert worst <= 3
