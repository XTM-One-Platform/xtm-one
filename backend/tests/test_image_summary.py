"""Tests for the server-side visual summary of a raster image.

The incident behind the module: a diagram published to Notion with the
OpenCRQ mark rendered cyan (``#0FBCFF``) instead of Filigran blue, after a
geometry probe reported "clean" and the screenshot's pixels had been pruned
from the agent's context. Every test here is a synthetic version of that
picture - white paper, brand-coloured cards, a small mark - and asks whether
the summary names the mark's colour without pixels.
"""

from __future__ import annotations

import colorsys
import io
import random

import pytest
from PIL import Image, ImageDraw

from app.core.color import delta_e, format_hex, parse_hex_color
from app.services import image_summary
from app.services.image_summary import (
    COLOR_MATCH_DELTA_E,
    GATE_SEPARATION_DELTA_E,
    MAX_CHECK_COLORS,
    MAX_SUMMARY_COLORS,
    SATURATED_COLOR_MIN_SHARE,
    ColorShare,
    ImageSummary,
    check_color_expectations,
    conflicting_gate_colors,
    format_visual_summary,
    looks_like_raster_image,
    parse_color_list,
    run_color_check,
    summarize_image,
    summarize_image_bytes,
)

BLUE = "#0015A8"
PURPLE = "#7B2CBF"
CYAN = "#0FBCFF"


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _diagram(*, mark: str | None = CYAN, mark_size: int = 40) -> Image.Image:
    """White 1600x900 paper, a blue and a purple card, grey ink, one small mark."""
    image = Image.new("RGB", (1600, 900), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle([100, 100, 499, 299], fill=BLUE)  # 400x200 = 5.56 %
    draw.rectangle([600, 100, 899, 249], fill=PURPLE)  # 300x150 = 3.13 %
    draw.rectangle([100, 500, 1399, 519], fill="#333333")  # grey bar
    draw.rectangle([100, 600, 1399, 609], fill="#1A1F36")  # dark navy "text"
    if mark:
        draw.rectangle(
            [1500, 820, 1500 + mark_size - 1, 820 + mark_size - 1], fill=mark
        )
    return image


def _hexes(summary: ImageSummary) -> list[str]:
    return [color.hex for color in summary.colors]


# ── What the summary reads out of a picture ─────────────────────────────


def test_the_summary_lists_the_brand_colours_and_the_stray_mark_and_ignores_the_ink():
    summary = summarize_image_bytes(_png(_diagram()))

    assert summary is not None
    assert (summary.width, summary.height) == (1600, 900)
    # The paper is the dominant colour, exact.
    assert summary.dominant is not None
    assert summary.dominant.hex == "#FFFFFF"
    assert 0.85 < summary.dominant.share < 0.92
    # Largest share first; the grey bar and the near-black navy are ink, not colour.
    assert _hexes(summary) == [BLUE, PURPLE, CYAN]
    blue, purple, cyan = summary.colors
    assert abs(blue.share - 0.0556) < 0.004
    assert abs(purple.share - 0.0313) < 0.004
    # 40x40 on 1600x900 = 0.11 %: well over the threshold.
    assert 0.0008 < cyan.share < 0.0016


def test_a_thirty_pixel_mark_is_still_listed():
    """The threshold is set for a small logo mark: 30x30 on 1600x900 is 0.06 %."""
    summary = summarize_image_bytes(_png(_diagram(mark_size=30)))
    assert summary is not None
    assert CYAN in _hexes(summary)
    assert summary.colors[-1].share >= SATURATED_COLOR_MIN_SHARE


def test_without_the_mark_the_cyan_is_not_there():
    summary = summarize_image_bytes(_png(_diagram(mark=None)))
    assert summary is not None
    assert _hexes(summary) == [BLUE, PURPLE]


def test_transparent_pixels_are_ignored_and_reported():
    logo = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
    ImageDraw.Draw(logo).ellipse([100, 100, 300, 300], fill=CYAN)
    summary = summarize_image_bytes(_png(logo))

    assert summary is not None
    # Shares are of the OPAQUE pixels: the disc is the whole visible image.
    assert summary.dominant is not None and summary.dominant.hex == CYAN
    assert summary.colors[0].share > 0.99
    assert 0.75 < summary.transparent_share < 0.85
    assert "transparent 8" in format_visual_summary(summary)


def test_a_gradient_folds_into_one_colour():
    """The steps of a gradient are one colour to the eye and one entry here."""
    image = Image.new("RGB", (800, 400), "white")
    draw = ImageDraw.Draw(image)
    for row in range(100, 300):
        shade = 0xA8 + (row - 100) // 20  # #0015A8 -> #0015B1 over 200 rows
        draw.line([(100, row), (700, row)], fill=(0, 0x15, shade))
    summary = summarize_image_bytes(_png(image))

    assert summary is not None
    assert len(summary.colors) == 1
    assert delta_e(summary.colors[0].rgb, parse_hex_color(BLUE)) < COLOR_MATCH_DELTA_E


def test_jpeg_artefacts_do_not_hide_a_forbidden_colour():
    buffer = io.BytesIO()
    _diagram().save(buffer, format="JPEG", quality=80)
    summary = summarize_image_bytes(buffer.getvalue())

    assert summary is not None
    problems = check_color_expectations(
        summary, expect=[parse_hex_color(BLUE)], forbid=[parse_hex_color(CYAN)]
    )
    assert len(problems) == 1
    assert problems[0].startswith("forbidden colour #0FBCFF is present (")
    # The listed value drifted a little; the check matched it by distance and
    # names both the colour asked about and the one found.
    assert "0.1" in problems[0]


def test_bytes_that_are_not_an_image_yield_no_summary():
    assert summarize_image_bytes(b"%PDF-1.4 fake") is None
    assert summarize_image_bytes(b"") is None
    assert summarize_image_bytes(b"\x89PNG\r\n\x1a\n truncated") is None
    assert format_visual_summary(None) == (
        "Visual summary: unavailable (the image could not be decoded, or is over the size cap)."
    )


def test_an_image_over_the_pixel_cap_is_not_decoded(monkeypatch):
    monkeypatch.setattr(image_summary, "MAX_SUMMARY_PIXELS", 100)
    assert summarize_image_bytes(_png(Image.new("RGB", (20, 20), "white"))) is None


def test_a_colourful_photograph_stays_bounded():
    """Thousands of saturated buckets cost the merge bound, never the whole list,
    and the summary LINE prints at most MAX_SUMMARY_COLORS of them."""
    rng = random.Random(3714)  # noqa: S311 - deterministic test noise, not a secret
    noise = Image.new("RGB", (600, 400))
    noise.putdata(
        [(rng.randrange(256), rng.randrange(256), rng.randrange(256)) for _ in range(600 * 400)]
    )
    summary = summarize_image_bytes(_png(noise))
    assert summary is not None
    assert len(summary.colors) <= image_summary._MAX_DISTINCT_COLORS
    assert all(color.share >= SATURATED_COLOR_MIN_SHARE for color in summary.colors)
    line = format_visual_summary(summary)
    assert line.count("#") <= MAX_SUMMARY_COLORS + 1  # the dominant colour, then the list
    if len(summary.colors) > MAX_SUMMARY_COLORS:
        assert f"(+{len(summary.colors) - MAX_SUMMARY_COLORS} more)" in line


def _banded(*, bands: int, mark: bool) -> Image.Image:
    """``bands`` distinct saturated stripes, each well over the threshold, plus
    (optionally) a cyan mark smaller than every stripe - the entry a capped
    list would drop first."""
    image = Image.new("RGB", (1600, 900), "white")
    draw = ImageDraw.Draw(image)
    for index in range(bands):
        rgb = tuple(round(channel * 255) for channel in colorsys.hsv_to_rgb(index / bands, 0.9, 0.85))
        draw.rectangle([100 + index * 80, 100, 100 + index * 80 + 59, 499], fill=rgb)
    if mark:
        draw.rectangle([1500, 820, 1539, 859], fill=CYAN)
    return image


def test_the_gate_reads_every_colour_over_the_threshold_not_the_printed_dozen():
    """Fourteen brand stripes push the printed list past MAX_SUMMARY_COLORS; the
    smallest colour - the stray mark - must still be found by forbid_colors."""
    summary = summarize_image_bytes(_png(_banded(bands=14, mark=True)))
    assert summary is not None
    assert not summary.truncated
    assert len(summary.colors) > MAX_SUMMARY_COLORS
    assert summary.colors[-1].hex == CYAN  # the smallest share, past the printed dozen
    line = format_visual_summary(summary)
    assert CYAN not in line and "more)" in line
    problems = check_color_expectations(summary, expect=[], forbid=[parse_hex_color(CYAN)])
    assert len(problems) == 1 and problems[0].startswith("forbidden colour #0FBCFF is present")
    # ... and an expected colour past the dozen is found as well.
    assert check_color_expectations(summary, expect=[parse_hex_color(CYAN)], forbid=[]) == []


def test_a_sampled_summary_refuses_a_forbid_check_instead_of_passing_it():
    """When not every pixel was counted, absence is not provable: forbid_colors
    refuses, expect_colors says why the count is short. (A hand-built summary
    without buckets is read off its list.)"""
    summary = ImageSummary(
        width=800,
        height=600,
        dominant=ColorShare((255, 255, 255), 0.5),
        colors=(ColorShare((0x00, 0x15, 0xA8), 0.2),),
        sampled=True,
    )
    assert not summary.complete
    assert "sampled, not every pixel counted" in format_visual_summary(summary)
    problems = check_color_expectations(summary, expect=[], forbid=[parse_hex_color(CYAN)])
    assert problems == [
        "forbidden colour #0FBCFF cannot be ruled out (the image was too large or too "
        "colourful to count every pixel and was sampled, so its absence is not verifiable)"
    ]
    # A forbidden colour that IS there is still reported as present, not as unverifiable.
    problems = check_color_expectations(summary, expect=[], forbid=[parse_hex_color(BLUE)])
    assert problems == ["forbidden colour #0015A8 is present (exactly, 20.0% of the image)"]
    problems = check_color_expectations(summary, expect=[parse_hex_color(CYAN)], forbid=[])
    assert len(problems) == 1 and problems[0].endswith(
        "present: #0015A8 20.0%; the image was too large or too colourful to count every "
        "pixel and was sampled)"
    )


def test_a_truncated_line_does_not_blind_the_gate():
    """The distinct-colour cap shortens the LINE (`truncated`); the gate reads
    the buckets and is untouched by it - the summary stays `complete`."""
    summary = ImageSummary(
        width=800,
        height=600,
        dominant=ColorShare((255, 255, 255), 0.5),
        colors=(ColorShare((0x00, 0x15, 0xA8), 0.2),),
        truncated=True,
        saturated=(ColorShare((0x00, 0x15, 0xA8), 0.2), ColorShare((0x0F, 0xBC, 0xFF), 0.001)),
    )
    assert summary.complete
    assert "(incomplete:" in format_visual_summary(summary)
    problems = check_color_expectations(summary, expect=[], forbid=[parse_hex_color(CYAN)])
    assert problems == ["forbidden colour #0FBCFF is present (exactly, 0.10% of the image)"]
    assert check_color_expectations(summary, expect=[], forbid=[(255, 0, 0)]) == []


def test_the_merge_bound_marks_the_summary_truncated_when_it_drops_a_colour(monkeypatch):
    monkeypatch.setattr(image_summary, "_MAX_DISTINCT_COLORS", 3)
    summary = summarize_image_bytes(_png(_banded(bands=6, mark=False)))
    assert summary is not None
    assert summary.truncated
    assert len(summary.colors) == 3


def test_discarded_buckets_are_judged_together_not_one_by_one(monkeypatch):
    """Once the cap is full, three nearby cyan shades - each under the threshold,
    none kept, so none can fold into another - are discarded one by one; together
    they are a colour over the threshold, so the LINE must say incomplete. The
    gate reads the buckets themselves and finds the colour regardless."""
    monkeypatch.setattr(image_summary, "_MAX_DISTINCT_COLORS", 1)
    image = Image.new("RGB", (1600, 900), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle([100, 100, 899, 499], fill=BLUE)  # the one kept colour
    # Three 24x24 marks (0.04 % each, 0.12 % together) in three cyan shades
    # that fall into three different RGB buckets yet sit within a few dE of
    # each other (so, uncapped, they fold into one colour).
    for index, shade in enumerate(("#0FBCFF", "#18C0FF", "#22C4FF")):
        left = 1400 + index * 40
        draw.rectangle([left, 820, left + 23, 843], fill=shade)
    summary = summarize_image_bytes(_png(image))
    assert summary is not None
    assert [color.hex for color in summary.colors] == [BLUE]
    assert summary.truncated and summary.complete
    assert "(incomplete:" in format_visual_summary(summary)
    problems = check_color_expectations(summary, expect=[], forbid=[parse_hex_color(CYAN)])
    assert problems == ["forbidden colour #0FBCFF is present (exactly, 0.12% of the image)"]

    # Without the cap the three shades fold into one listed colour over the
    # threshold, and the same forbid check finds it.
    monkeypatch.setattr(image_summary, "_MAX_DISTINCT_COLORS", 64)
    complete = summarize_image_bytes(_png(image))
    assert complete is not None and not complete.truncated
    assert len(complete.colors) == 2
    problems = check_color_expectations(complete, expect=[], forbid=[parse_hex_color(CYAN)])
    assert len(problems) == 1 and "is present" in problems[0]


def test_every_bucket_folds_into_a_kept_colour_whatever_its_rank(monkeypatch):
    """A kept colour one sample under the threshold is completed by a tiny
    nearby bucket the cap could not keep: the small bucket is FOLDED, not
    discarded, so the colour is listed and forbid_colors finds it - however
    many other colours rank above it."""
    monkeypatch.setattr(image_summary, "_MAX_DISTINCT_COLORS", 2)
    image = Image.new("RGB", (1600, 900), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle([100, 100, 899, 499], fill=BLUE)  # kept #1
    # Cyan mark just UNDER the threshold on its own (25x25 = 0.043 %) ...
    draw.rectangle([1500, 820, 1524, 844], fill=CYAN)  # kept #2 (the cap)
    # ... plus a sliver in a neighbouring bucket that completes it (0.014 %),
    # and a purple speck the cap cannot keep and cannot fold anywhere.
    draw.rectangle([1500, 850, 1524, 857], fill="#18C0FF")
    draw.rectangle([1550, 850, 1559, 859], fill=PURPLE)
    summary = summarize_image_bytes(_png(image))
    assert summary is not None
    assert [color.hex for color in summary.colors] == [BLUE, CYAN]
    assert summary.colors[-1].share >= SATURATED_COLOR_MIN_SHARE
    assert not summary.truncated  # the purple speck alone cannot reach the threshold
    problems = check_color_expectations(summary, expect=[], forbid=[parse_hex_color(CYAN)])
    assert len(problems) == 1 and "is present" in problems[0]


def test_a_negligible_discard_does_not_mark_the_summary_incomplete(monkeypatch):
    """A single stray pixel past the cap cannot be a colour over the threshold:
    the list stays complete and forbid_colors keeps its verdict."""
    monkeypatch.setattr(image_summary, "_MAX_DISTINCT_COLORS", 1)
    image = Image.new("RGB", (1600, 900), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle([100, 100, 899, 499], fill=BLUE)
    draw.point((1500, 850), fill=CYAN)
    summary = summarize_image_bytes(_png(image))
    assert summary is not None
    assert not summary.truncated
    assert check_color_expectations(summary, expect=[], forbid=[parse_hex_color(CYAN)]) == []


def _capped_with_scattered_mark(*, mark_size: int) -> bytes:
    """Three big stripes and one mark drawn as four quadrants of slightly
    different cyans - what JPEG drift or anti-aliasing does to a flat fill -
    so the mark lands in four buckets, each UNDER the listing threshold at
    ``mark_size`` 40 while together they are over it."""
    image = Image.new("RGB", (1600, 900), "white")
    draw = ImageDraw.Draw(image)
    for index in range(3):
        rgb = tuple(round(channel * 255) for channel in colorsys.hsv_to_rgb(index / 3, 0.9, 0.85))
        draw.rectangle([100 + index * 300, 100, 100 + index * 300 + 199, 499], fill=rgb)
    half = mark_size // 2
    for quadrant, rgb in enumerate(
        [(0x0F, 0xBC, 0xFF), (0x0F, 0xCC, 0xFF), (0x1F, 0xBC, 0xEF), (0x0F, 0xAC, 0xFF)]
    ):
        x = 1500 + (quadrant % 2) * half
        y = 820 + (quadrant // 2) * half
        draw.rectangle([x, y, x + half - 1, y + half - 1], fill=rgb)
    return _png(image)


def test_the_cap_shortens_the_line_but_never_blinds_the_gate(monkeypatch):
    """A drifting mark's four pieces, each under the threshold and too far
    apart to fold into one another, are discarded once the cap is full: the
    LINE says incomplete. The gate sums the buckets within tolerance - three
    of the four - and finds the mark anyway. A speck's pieces cannot reach the
    threshold even added up: not truncated, and forbid passes."""
    monkeypatch.setattr(image_summary, "_MAX_DISTINCT_COLORS", 3)
    summary = summarize_image_bytes(_capped_with_scattered_mark(mark_size=40))
    assert summary is not None
    assert CYAN not in _hexes(summary)
    assert summary.truncated and summary.complete
    problems = check_color_expectations(summary, expect=[], forbid=[parse_hex_color(CYAN)])
    assert problems == ["forbidden colour #0FBCFF is present (exactly, 0.08% of the image)"]
    summary = summarize_image_bytes(_capped_with_scattered_mark(mark_size=8))
    assert summary is not None
    assert not summary.truncated and summary.complete
    assert check_color_expectations(summary, expect=[], forbid=[parse_hex_color(CYAN)]) == []


# ── Every pixel counted, and the gate reads the buckets ─────────────────


def test_every_pixel_is_counted_so_a_one_pixel_line_is_found():
    """A one-pixel vertical stripe covers 0.0625 % of
    a 1600x900 image - over the threshold - and a NEAREST subsample skipped
    every other source column, so the forbidden stripe passed the gate with
    the summary marked complete. Every pixel is counted now: the stripe is
    found whichever column it stands in."""
    for x in (1, 2, 4, 5, 7):
        image = Image.new("RGB", (1600, 900), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle([100, 100, 499, 299], fill=BLUE)
        draw.line([(x, 0), (x, 899)], fill=CYAN)
        summary = summarize_image_bytes(_png(image))
        assert summary is not None
        assert summary.complete and not summary.sampled
        assert CYAN in _hexes(summary), x
        problems = check_color_expectations(summary, expect=[], forbid=[parse_hex_color(CYAN)])
        assert problems == ["forbidden colour #0FBCFF is present (exactly, 0.06% of the image)"], x


def test_the_gate_sums_the_exact_colours_within_tolerance_not_the_merged_list():
    """MERGE_DELTA_E (10) is narrower than
    COLOR_MATCH_DELTA_E (12), so two halves of a drifted mark 11.9 apart -
    each under the threshold, each within tolerance of the forbidden colour -
    stay two list entries the threshold drops. Read as exact colours and
    summed they are the mark."""
    forbidden = parse_hex_color(CYAN)
    half_a, half_b = (0x0F, 0xBC, 0xFF), (0x0F, 0xCC, 0xFF)
    assert delta_e(forbidden, half_b) <= COLOR_MATCH_DELTA_E
    assert delta_e(half_a, half_b) > image_summary.MERGE_DELTA_E
    image = Image.new("RGB", (1600, 900), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle([100, 100, 499, 299], fill=BLUE)
    draw.rectangle([1500, 820, 1519, 849], fill=half_a)  # 600 px = 0.042 %
    draw.rectangle([1530, 820, 1549, 849], fill=half_b)  # 600 px = 0.042 %
    summary = summarize_image_bytes(_png(image))
    assert summary is not None
    assert _hexes(summary) == [BLUE]  # neither half is listed on its own
    assert {color.hex for color in summary.saturated} == {BLUE, "#0FBCFF", "#0FCCFF"}
    assert all(color.lab is not None for color in summary.saturated)
    problems = check_color_expectations(summary, expect=[], forbid=[forbidden])
    assert problems == ["forbidden colour #0FBCFF is present (exactly, 0.08% of the image)"]
    # ... and an expected colour split the same way is present too.
    assert check_color_expectations(summary, expect=[forbidden], forbid=[]) == []


def test_a_colour_folded_into_an_out_of_tolerance_neighbour_still_counts():
    """The other shape of the same hole: the within-tolerance half is FOLDED
    into a larger neighbour (< 10 from it) that is itself out of tolerance of
    the forbidden colour (19 from it), so the merged list names only the
    neighbour. The exact colours still hold the half."""
    forbidden = parse_hex_color(CYAN)
    half, neighbour = (0x0F, 0xCC, 0xFF), (0x08, 0xD4, 0xFC)
    assert delta_e(forbidden, half) <= COLOR_MATCH_DELTA_E
    assert delta_e(half, neighbour) < image_summary.MERGE_DELTA_E
    assert delta_e(forbidden, neighbour) > COLOR_MATCH_DELTA_E
    image = Image.new("RGB", (1600, 900), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle([100, 100, 499, 299], fill=BLUE)
    draw.rectangle([1400, 700, 1499, 799], fill=neighbour)  # 10000 px, the larger one
    draw.rectangle([1500, 820, 1529, 849], fill=half)  # 900 px = 0.0625 %
    summary = summarize_image_bytes(_png(image))
    assert summary is not None
    assert _hexes(summary) == [BLUE, "#08D4FC"]  # the half folded into its neighbour
    problems = check_color_expectations(summary, expect=[], forbid=[forbidden])
    assert problems == ["forbidden colour #0FBCFF is present (#0FCCFF, 0.06% of the image)"]


def test_a_small_mark_sharing_a_bucket_with_a_large_colour_is_not_lost_in_the_mean():
    """CodeRabbit's second case: a 16-cube bucket holds checkable colours up
    to 25.8 dE apart. #002F40 and #0F204F share one; with the navy filling a
    card and the teal a small mark, the bucket's MEAN is the navy - 25 from
    the teal - and a gate reading bucket means passed the forbidden teal.
    The gate reads exact colours, so the mark is found at its own share."""
    forbidden, neighbour = (0x00, 0x2F, 0x40), (0x0F, 0x20, 0x4F)
    assert [c // 16 for c in forbidden] == [c // 16 for c in neighbour]  # one bucket
    assert delta_e(forbidden, neighbour) > 2 * COLOR_MATCH_DELTA_E
    image = Image.new("RGB", (1600, 900), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle([100, 100, 499, 299], fill=neighbour)  # 80000 px
    draw.rectangle([1500, 820, 1539, 859], fill=forbidden)  # 1600 px = 0.11 %
    summary = summarize_image_bytes(_png(image))
    assert summary is not None
    assert _hexes(summary) == ["#0F204F"]  # the line shows the bucket, named by the navy
    problems = check_color_expectations(summary, expect=[], forbid=[forbidden])
    assert problems == ["forbidden colour #002F40 is present (exactly, 0.11% of the image)"]
    # ... and the navy, though it dominates the same bucket, is not "the teal".
    assert check_color_expectations(summary, expect=[], forbid=[(0x0F, 0x20, 0x4F)]) == [
        "forbidden colour #0F204F is present (exactly, 5.6% of the image)"
    ]


def test_an_image_too_colourful_to_count_exactly_is_sampled_and_says_so(monkeypatch):
    """Past _MAX_EXACT_COLORS the exact count is abandoned for the NEAREST
    subsample: the summary is `sampled`, the line says so, and a forbid check
    refuses on it - a feature thinner than the sample step may be missed."""
    monkeypatch.setattr(image_summary, "_MAX_EXACT_COLORS", 3)
    summary = summarize_image_bytes(_png(_diagram()))
    assert summary is not None
    assert summary.sampled and not summary.complete
    assert "sampled, not every pixel counted" in format_visual_summary(summary)
    # Presence is still reported; only absence is refused.
    problems = check_color_expectations(summary, expect=[], forbid=[parse_hex_color(CYAN)])
    assert problems == ["forbidden colour #0FBCFF is present (exactly, 0.11% of the image)"]
    problems = check_color_expectations(summary, expect=[], forbid=[(255, 0, 0)])
    assert problems == [
        "forbidden colour #FF0000 cannot be ruled out (the image was too large or too "
        "colourful to count every pixel and was sampled, so its absence is not verifiable)"
    ]
    # ... and the same for an image over the exact-count pixel cap.
    monkeypatch.setattr(image_summary, "_MAX_EXACT_COLORS", 1 << 17)
    monkeypatch.setattr(image_summary, "_MAX_EXACT_PIXELS", 1000)
    summary = summarize_image_bytes(_png(_diagram()))
    assert summary is not None and summary.sampled


def test_a_small_image_too_colourful_for_the_exact_pass_is_still_counted_whole(monkeypatch):
    """The fallback of an image within SUMMARY_SAMPLE_LONG_EDGE counts every
    pixel (its 'sample' is the whole image), so it is NOT sampled: no false
    notice, and - the two saturated colours fitting the palette - a forbid
    check keeps its verdict."""
    monkeypatch.setattr(image_summary, "_MAX_EXACT_COLORS", 2)
    small = _diagram(mark=None).resize((640, 360), Image.NEAREST)
    summary = summarize_image_bytes(_png(small))
    assert summary is not None
    assert not summary.sampled and not summary.palette_cut and summary.complete
    assert "sampled" not in format_visual_summary(summary)
    assert check_color_expectations(summary, expect=[], forbid=[parse_hex_color(CYAN)]) == []


def test_a_whole_image_count_that_could_not_keep_every_colour_is_not_complete(monkeypatch):
    """A small image with more distinct saturated
    colours than the palette holds is counted whole (not sampled), yet the
    rarest colours are cut - and a mark painted as 1600 one-pixel shades is
    exactly what sits in that tail. The line still shows its bucket; the gate
    would have reported it absent. Such a count is not `complete`: the line
    says the check is limited, and a forbid check refuses."""
    monkeypatch.setattr(image_summary, "_MAX_EXACT_COLORS", 2000)
    rng = random.Random(1)  # noqa: S311 - deterministic test noise, not a secret
    image = Image.new("RGB", (600, 400), "white")
    pixels = image.load()
    # 3000 distinct greens, ~40 pixels each: they fill the palette first.
    greens = [(rng.randrange(0, 40), rng.randrange(150, 256), rng.randrange(0, 40)) for _ in range(3000)]
    index = 0
    for y in range(200, 400):
        for x in range(0, 600):
            pixels[x, y] = greens[(index // 40) % 3000]
            index += 1
    # The forbidden mark: 40x40 = 0.67 %, every pixel its own shade of cyan.
    shades = [(15 + a, 188 + b, 253 + c) for a in range(24) for b in range(24) for c in range(3)]
    for offset, (y, x) in enumerate((y, x) for y in range(150, 190) for x in range(500, 540)):
        pixels[x, y] = shades[offset]
    summary = summarize_image_bytes(_png(image))
    assert summary is not None
    assert not summary.sampled and summary.palette_cut and not summary.complete
    assert len(summary.saturated) == 2000
    assert "colour check limited to the 2000 most frequent colours" in format_visual_summary(summary)
    problems = check_color_expectations(summary, expect=[], forbid=[parse_hex_color(CYAN)])
    assert problems == [
        "forbidden colour #0FBCFF cannot be ruled out (the image has more distinct saturated "
        "colours than the check can compare, so only the 2000 most frequent were kept, so its "
        "absence is not verifiable)"
    ]
    # A colour that IS among the kept ones is still verified as present.
    assert check_color_expectations(summary, expect=[greens[0]], forbid=[]) == []


def _animated_gif(*, later_frame_mark: str | None) -> bytes:
    """Two frames: the first is the clean diagram, the second carries the mark."""
    first = Image.new("RGB", (400, 300), "white")
    ImageDraw.Draw(first).rectangle([20, 20, 120, 120], fill=BLUE)
    second = first.copy()
    if later_frame_mark:
        ImageDraw.Draw(second).rectangle([300, 200, 380, 280], fill=later_frame_mark)
    buffer = io.BytesIO()
    first.save(buffer, format="GIF", save_all=True, append_images=[second], duration=100, loop=0)
    return buffer.getvalue()


def test_an_animation_is_read_at_its_first_frame_and_says_so():
    """A forbidden colour in the second frame is invisible to a first-frame
    read: the summary names the frame count, and forbid refuses on it rather
    than passing on the one frame it saw. Presence in the first frame is
    still presence, so expect is verified on it."""
    summary = summarize_image_bytes(_animated_gif(later_frame_mark=CYAN))
    assert summary is not None
    assert summary.frames == 2 and not summary.complete and not summary.truncated
    assert _hexes(summary) == [BLUE]
    line = format_visual_summary(summary)
    assert "400x300 px; animated, only the first of 2 frames read; dominant" in line
    problems = check_color_expectations(summary, expect=[], forbid=[parse_hex_color(CYAN)])
    assert problems == [
        "forbidden colour #0FBCFF cannot be ruled out (only the first of its 2 frames was "
        "read, so its absence is not verifiable)"
    ]
    assert check_color_expectations(summary, expect=[parse_hex_color(BLUE)], forbid=[]) == []
    problems = check_color_expectations(summary, expect=[parse_hex_color(CYAN)], forbid=[])
    assert len(problems) == 1 and problems[0].endswith("only the first of its 2 frames was read)")

    # A still image reports one frame and no such caveat.
    still = summarize_image_bytes(_png(_diagram()))
    assert still is not None and still.frames == 1 and still.complete
    assert "frames" not in format_visual_summary(still)


def test_every_incompleteness_reason_is_named_together():
    summary = ImageSummary(
        width=800,
        height=600,
        dominant=ColorShare((255, 255, 255), 0.5),
        colors=(ColorShare((0x00, 0x15, 0xA8), 0.2),),
        sampled=True,
        palette_cut=True,
        frames=3,
    )
    assert not summary.complete
    problems = check_color_expectations(summary, expect=[], forbid=[parse_hex_color(CYAN)])
    assert problems == [
        "forbidden colour #0FBCFF cannot be ruled out (the image was too large or too "
        "colourful to count every pixel and was sampled and the image has more distinct "
        f"saturated colours than the check can compare, so only the {image_summary._MAX_EXACT_COLORS} "
        "most frequent were kept and only the first of its 3 frames was read, so its absence "
        "is not verifiable)"
    ]


def test_complete_folds_in_every_flag_a_negative_check_relies_on():
    """`complete` is the ONE predicate the gate reads for 'absence is provable':
    every flag that can make a negative verdict wrong turns it off."""
    base = dict(width=10, height=10, dominant=None, colors=())
    assert ImageSummary(**base).complete
    assert not ImageSummary(**base, sampled=True).complete
    assert not ImageSummary(**base, palette_cut=True).complete
    assert not ImageSummary(**base, frames=2).complete
    # The line's own cap is about what is printed, never about the gate.
    assert ImageSummary(**base, truncated=True).complete


def test_a_container_whose_frames_cannot_be_counted_is_not_summarised(monkeypatch):
    """Frame 0 of a file that could not be read to its end is not a summary of
    the file: the gate then refuses as undecodable instead of passing on it."""
    from PIL import GifImagePlugin

    def _broken(self):
        raise OSError("frame table unreadable")

    monkeypatch.setattr(GifImagePlugin.GifImageFile, "n_frames", property(_broken))
    assert summarize_image_bytes(_animated_gif(later_frame_mark=CYAN)) is None


@pytest.mark.parametrize(
    ("data", "content_type", "expected"),
    [
        (b"\x89PNG\r\n\x1a\n...", "", True),
        (b"\xff\xd8\xff...", "", True),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "", True),
        (b"GIF89a", "application/octet-stream", True),
        (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml", False),
        (b"%PDF-1.4", "application/pdf", False),
        (b"not magic", "image/png", True),
        (b"", "image/png", False),
    ],
)
def test_looks_like_raster_image_reads_magic_first_and_the_declared_type_second(
    data, content_type, expected
):
    assert looks_like_raster_image(data, content_type) is expected


# ── The one summary line ────────────────────────────────────────────────


def test_format_visual_summary_is_one_stable_line():
    summary = ImageSummary(
        width=1600,
        height=900,
        dominant=ColorShare((255, 255, 255), 0.812),
        colors=(
            ColorShare((0x7B, 0x2C, 0xBF), 0.023),
            ColorShare((0x00, 0x15, 0xA8), 0.011),
            ColorShare((0x0F, 0xBC, 0xFF), 0.004),
        ),
    )
    assert format_visual_summary(summary) == (
        "Visual summary: 1600x900 px; dominant #FFFFFF 81.2%; "
        "saturated colours (>= 0.05% coverage): #7B2CBF 2.3%, #0015A8 1.1%, #0FBCFF 0.40%."
    )
    assert format_visual_summary(ImageSummary(10, 10, ColorShare((0, 0, 0), 1.0), ())) == (
        "Visual summary: 10x10 px; dominant #000000 100.0%; "
        "saturated colours (>= 0.05% coverage): none."
    )


def test_format_hex_is_uppercase_and_clamped():
    assert format_hex((15, 188, 255)) == "#0FBCFF"
    assert format_hex((300, -4, 0)) == "#FF0000"


# ── The expect / forbid gate ────────────────────────────────────────────


def test_parse_color_list_accepts_a_list_or_one_string_and_drops_duplicates():
    assert parse_color_list(["#0015A8", "7B2CBF", "#0015a8"], what="expect_colors") == (
        [(0, 0x15, 0xA8), (0x7B, 0x2C, 0xBF)],
        None,
    )
    assert parse_color_list("#0FBCFF, ff0000;  #0FBCFF", what="forbid_colors") == (
        [(0x0F, 0xBC, 0xFF), (0xFF, 0, 0)],
        None,
    )
    assert parse_color_list(None, what="expect_colors") == ([], None)
    assert parse_color_list("", what="expect_colors") == ([], None)
    assert parse_color_list([], what="expect_colors") == ([], None)


@pytest.mark.parametrize("value", [[""], ["   "], "   ", [BLUE, ""], [" , ; "]])
def test_a_supplied_gate_with_a_blank_entry_is_refused_not_read_as_no_gate(value):
    """`forbid_colors=[""]` tokenised to nothing and
    became an empty gate that let the upload through. Only an OMITTED gate
    (None / "" / []) means no check; a supplied blank entry is malformed and
    refused, naming the argument."""
    colors, err = parse_color_list(value, what="forbid_colors")
    assert colors == []
    assert err == (
        "Error: forbid_colors has a blank entry; drop it, or write a hex colour like #0015A8 "
        "(omit the argument entirely for no check)."
    )


def test_parse_color_list_names_the_entry_it_cannot_read():
    colors, err = parse_color_list(["#0015A8", "blue"], what="forbid_colors")
    assert colors == []
    assert err == (
        "Error: forbid_colors entry 'blue' is not a 6-digit hex colour (write it like #0015A8)."
    )
    _, err = parse_color_list(["#FFF"], what="expect_colors")
    assert "'#FFF' is not a 6-digit hex colour" in err
    _, err = parse_color_list([42], what="expect_colors")
    assert "got a int in the list" in err
    _, err = parse_color_list({"hex": "#FFFFFF"}, what="expect_colors")
    assert "got dict" in err
    too_many = [f"#FF{index * 15:02X}00" for index in range(MAX_CHECK_COLORS + 1)]
    _, err = parse_color_list(too_many, what="expect_colors")
    assert f"{MAX_CHECK_COLORS} is the cap" in err


@pytest.mark.parametrize("entry", ["#333333", "#FFFFFF", "#1A1F36", "#E6F0FF", "#000000"])
def test_parse_color_list_refuses_a_colour_the_check_can_never_see(entry):
    """A grey, white, near-black or pale entry would never be on the saturated
    list: forbidding it would always pass and expecting it would always fail,
    both silently. It is refused up front instead, naming the entry."""
    colors, err = parse_color_list([BLUE, entry], what="forbid_colors")
    assert colors == []
    assert err.startswith(f"Error: forbid_colors entry '{entry}' is a grey, white, near-black")
    assert "SATURATED colours only" in err


def test_gate_colours_whose_match_regions_overlap_are_refused_up_front():
    """Colours #0015A8 and #0037C8 are 12.6 apart - past a
    single match radius - yet #0018AB is within 12 of BOTH, so a card in that
    colour satisfied the expected colour and convicted the forbidden one at
    once. CIE76 is Euclidean, so one pixel within the radius of both puts the
    pair within twice the radius: that is the separation the check demands."""
    expected, forbidden, observed = (
        parse_hex_color("#0015A8"),
        parse_hex_color("#0037C8"),
        parse_hex_color("#0018AB"),
    )
    assert COLOR_MATCH_DELTA_E < delta_e(expected, forbidden) <= GATE_SEPARATION_DELTA_E
    assert delta_e(observed, expected) <= COLOR_MATCH_DELTA_E
    assert delta_e(observed, forbidden) <= COLOR_MATCH_DELTA_E
    # The contradiction the check exists to prevent: one summary, one colour,
    # "expected present" AND "forbidden present" from the same pixels.
    summary = ImageSummary(10, 10, None, (), saturated=(ColorShare(observed, 0.5),))
    assert check_color_expectations(summary, expect=[expected], forbid=[]) == []
    assert check_color_expectations(summary, expect=[], forbid=[forbidden]) != []
    err = conflicting_gate_colors([expected], [forbidden])
    assert err is not None
    assert err.startswith("Error: expected #0015A8 and forbidden #0037C8 are too close")
    assert f"at least {GATE_SEPARATION_DELTA_E:g} apart" in err
    # Past twice the radius no colour can count for both, and the pair is accepted.
    far = parse_hex_color(CYAN)
    assert delta_e(expected, far) > GATE_SEPARATION_DELTA_E
    assert conflicting_gate_colors([expected], [far]) is None
    # The same hex on both sides keeps its own wording.
    assert "listed in BOTH" in conflicting_gate_colors([expected], [expected])


def test_check_color_expectations_names_the_colour_and_its_share():
    summary = summarize_image_bytes(_png(_diagram()))
    assert summary is not None

    # A forbidden colour present: the colour asked about, then what was found.
    problems = check_color_expectations(summary, expect=[], forbid=[parse_hex_color(CYAN)])
    assert problems == ["forbidden colour #0FBCFF is present (exactly, 0.11% of the image)"]

    # An expected colour absent: what IS there is listed so the agent sees why.
    problems = check_color_expectations(summary, expect=[(255, 0, 0)], forbid=[])
    assert len(problems) == 1
    assert problems[0].startswith("expected colour #FF0000 is absent (")
    assert "#0015A8 5.6%" in problems[0] and "#0FBCFF 0.11%" in problems[0]

    # The pass: brand colours present, the wrong one absent.
    clean = summarize_image_bytes(_png(_diagram(mark=None)))
    assert clean is not None
    assert (
        check_color_expectations(
            clean,
            expect=[parse_hex_color(BLUE), parse_hex_color(PURPLE)],
            forbid=[parse_hex_color(CYAN)],
        )
        == []
    )


def test_a_colour_under_the_coverage_threshold_does_not_count_as_present():
    """A 10x10 speck is below the listing threshold, so forbidding it is a pass
    and expecting it is a miss - the threshold is one number for both."""
    summary = summarize_image_bytes(_png(_diagram(mark_size=10)))
    assert summary is not None
    assert CYAN not in _hexes(summary)
    assert check_color_expectations(summary, expect=[], forbid=[parse_hex_color(CYAN)]) == []
    assert len(check_color_expectations(summary, expect=[parse_hex_color(CYAN)], forbid=[])) == 1


# ── Off-loop execution ──────────────────────────────────────────────────


async def test_summarize_image_runs_on_the_parse_executor_and_never_raises(monkeypatch):
    calls: list = []
    data = _png(_diagram())

    async def _fake_run_parse(fn, *args):
        calls.append(fn)
        return fn(*args)

    monkeypatch.setattr("app.core.parse_offload.run_parse", _fake_run_parse)
    summary = await summarize_image(data)
    assert calls == [summarize_image_bytes]
    assert summary is not None and CYAN in _hexes(summary)

    # The gate rides the executor too (up to 4096 buckets per colour) ...
    calls.clear()
    problems = await run_color_check(summary, expect=[], forbid=[parse_hex_color(CYAN)])
    assert len(calls) == 1 and calls[0].func is check_color_expectations
    assert problems == ["forbidden colour #0FBCFF is present (exactly, 0.11% of the image)"]

    async def _broken_run_parse(fn, *args):
        raise RuntimeError("executor gone")

    monkeypatch.setattr("app.core.parse_offload.run_parse", _broken_run_parse)
    assert await summarize_image(data) is None
    # ... but unlike the summary, a gate that could not run must not pass: it raises.
    with pytest.raises(RuntimeError):
        await run_color_check(summary, expect=[], forbid=[parse_hex_color(CYAN)])


# ── Blank or painted (gap screenshot-blank-warning-light-page) ─────────


def _light_page(*, ink: str | None) -> Image.Image:
    """A #FAFAFA 1440x900 page, two skeleton bars a few shades off it, and optionally grey text lines."""
    image = Image.new("RGB", (1440, 900), "#FAFAFA")
    draw = ImageDraw.Draw(image)
    draw.rectangle([100, 100, 699, 129], fill="#EEEEEE")  # loading skeleton
    draw.rectangle([100, 160, 499, 179], fill="#EEEEEE")
    if ink:
        for top in range(300, 420, 24):  # five lines of a 404 message, about 2% of the page
            draw.rectangle([500, top, 1039, top + 11], fill=ink)
    return image


def test_a_light_page_of_grey_text_is_painted_not_blank():
    summary = summarize_image_bytes(_png(_light_page(ink="#5F6368")))
    assert summary is not None and summary.dominant is not None
    assert summary.dominant.share > image_summary.BLANK_DOMINANT_SHARE
    assert not summary.colors  # no saturated colour: the old rule flagged it
    assert summary.ink_share > image_summary.BLANK_MAX_INK_SHARE
    assert not image_summary.looks_blank(summary)


def test_a_background_with_only_a_skeleton_is_still_blank():
    summary = summarize_image_bytes(_png(_light_page(ink=None)))
    assert summary is not None
    assert summary.ink_share == 0.0
    assert image_summary.looks_blank(summary)
