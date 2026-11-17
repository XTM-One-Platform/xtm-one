"""The export rules of the native ``chart`` block, apart from PowerPoint.

``chart_export.py`` decides which chart kinds become editable PowerPoint
charts, how the viewer's value formatting is written as an Excel number format
code, and what the export log says about a chart that stayed a picture. These
are pinned here so the browser extractor, the PPTX writer and the spec can
never disagree on a kind, and so a format code keeps reading what the viewer
wrote (``€16.5M``, ``86%``, ``1,204 users``).
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
from typing import get_args

import pytest

from app.services.chart_export import (
    AXIS_COMPACT_FROM,
    CHART_KINDS,
    CHART_TEXT_SCALES,
    EMULATED_CHART_KINDS,
    MAX_AUTO_DECIMALS,
    MIRRORED_CHART_KINDS,
    NATIVE_CHART_KINDS,
    PICTURE_CHART_KINDS,
    TWO_AXIS_BARS_REASON,
    TWO_AXIS_PICTURE_SERIES_KINDS,
    VALUE_LABEL_SIZE_MAX_PX,
    VALUE_LABEL_SIZE_MIN_PX,
    axis_number_format,
    axis_view,
    chart_tick_px,
    chart_value_px,
    compact_format,
    compact_tier,
    currency_sign,
    drawn_kind,
    emulation_note,
    has_secondary_axis,
    is_native_chart,
    label_number_format,
    mirrored_number_format,
    picture_note,
    pinned_tier,
    run_decimals,
    series_axis,
    series_values,
    spine_note,
    view_values,
    with_currency,
    with_unit,
)
from app.services.presentation_spec import (
    SECONDARY_AXIS_CHART_KINDS,
    ChartKind,
    ChartUnitScale,
    SeriesKind,
    Size,
)

# ── Kinds ─────────────────────────────────────────────────────────────────────


def test_every_spec_chart_kind_is_either_native_or_a_picture():
    """A kind the spec accepts must be classified, or the writer would meet a
    ``chart`` primitive it has no chart type for (or paste a picture of a kind
    PowerPoint could have edited) without the log saying so."""
    assert set(get_args(ChartKind)) == CHART_KINDS
    assert NATIVE_CHART_KINDS | PICTURE_CHART_KINDS == CHART_KINDS
    assert not (NATIVE_CHART_KINDS & PICTURE_CHART_KINDS)
    # An emulated kind is an editable chart, drawn through another type.
    assert EMULATED_CHART_KINDS <= NATIVE_CHART_KINDS


@pytest.mark.parametrize("kind", sorted(NATIVE_CHART_KINDS))
def test_every_powerpoint_chart_kind_is_native(kind):
    """A combo is native too: its bars and lines are plots of one chart."""
    assert is_native_chart(kind)


def test_a_waterfall_is_an_editable_emulation_and_the_note_says_so():
    """PowerPoint's own waterfall is a `chartex` part python-pptx cannot write
    (and Keynote, Google Slides and LibreOffice draw as a blank box): the
    bridge is an editable stacked column chart, and the log says it is one."""
    assert sorted(EMULATED_CHART_KINDS) == ["waterfall"]
    note = emulation_note(3, "waterfall")
    assert note == (
        "Slide 3: the waterfall chart is an editable stacked bar chart, each step standing on an "
        "invisible base series - PowerPoint's own waterfall chart cannot be written from here."
    )
    # The log rides an HTTP header: ASCII, one sentence per line.
    assert note.isascii() and "\n" not in note


@pytest.mark.parametrize("kind", sorted(PICTURE_CHART_KINDS))
def test_a_gauge_and_a_map_stay_a_picture(kind):
    assert not is_native_chart(kind)
    assert sorted(PICTURE_CHART_KINDS) == ["gauge", "map"]


def test_only_bars_on_the_right_hand_axis_keep_a_native_kind_a_picture():
    """PowerPoint centres the bars of two value axes on their category, one
    over the other; a line on the right-hand axis is drawn as the viewer
    draws it. The set names series kinds, and only kinds a secondary series
    can take on a chart that allows one."""
    assert {"bar"} == TWO_AXIS_PICTURE_SERIES_KINDS
    assert set(get_args(SeriesKind)) >= TWO_AXIS_PICTURE_SERIES_KINDS
    assert SECONDARY_AXIS_CHART_KINDS <= NATIVE_CHART_KINDS


def test_the_picture_note_names_the_slide_the_kind_and_the_reason():
    note = picture_note(8, "gauge")
    assert note == "Slide 8: the gauge chart is a picture of the rendered figure - PowerPoint has no gauge chart."
    bars = picture_note(9, "bar", TWO_AXIS_BARS_REASON)
    assert bars == (
        "Slide 9: the bar chart is a picture of the rendered figure - "
        "PowerPoint draws the bars of two value axes over each other."
    )
    # A kind with no reason of its own still gets a complete sentence.
    other = picture_note(2, "sunburst")
    assert other.startswith("Slide 2: the sunburst chart is a picture") and other.endswith(".")
    # A map is a picture beside the text the extractor writes for it.
    world = picture_note(4, "map", "map")
    assert world == (
        "Slide 4: the map chart is a picture of the rendered figure - PowerPoint has no map chart that "
        "opens outside Office; its title, caption and legend are text."
    )
    # The log is shown to people through headers and tool results: ASCII-safe
    # apart from nothing, and one sentence per line.
    for text in (note, bars, other, world):
        assert "\n" not in text
        assert text.isascii()


# ── Two value axes ────────────────────────────────────────────────────


def test_a_payload_series_reads_against_its_axis_and_is_drawn_its_way():
    assert series_axis({"axis": "secondary"}) == "secondary"
    assert series_axis({}) == series_axis({"axis": "primary"}) == series_axis({"axis": "sideways"}) == "primary"
    assert drawn_kind({"kind": "combo"}, {"kind": "line"}) == "line"
    assert drawn_kind({"kind": "combo"}, {"kind": "bar"}) == drawn_kind({"kind": "combo"}, {}) == "bar"
    assert drawn_kind({"kind": "line"}, {}) == "line"
    assert drawn_kind({"kind": "area"}, {}) == "area"
    assert drawn_kind({"kind": "stacked_bar"}, {"kind": "line"}) == "bar"
    assert has_secondary_axis({"series": [{"name": "A"}, {"name": "B", "axis": "secondary"}]})
    assert not has_secondary_axis({"series": [{"name": "A"}]})
    assert not has_secondary_axis({"series": "nope"})


def test_each_axis_reads_its_own_series_target_and_format():
    """``axis_view`` is the chart as one axis reads it, so the compact-label
    rule (``value_extent``) and every format reads either axis unchanged."""
    chart = {
        "kind": "combo",
        "format": "number",
        "unit": None,
        "target": 30,
        "secondary_format": "currency",
        "secondary_unit": "EUR",
        "secondary_target": 250_000,
        "series": [
            {"name": "New logos", "values": [24, 56], "kind": "bar"},
            {"name": "ASP", "values": [240_000, 540_000], "kind": "line", "axis": "secondary"},
        ],
    }
    left, right = axis_view(chart), axis_view(chart, "secondary")
    assert (left["format"], left["unit"], left["target"]) == ("number", None, 30)
    assert [entry["name"] for entry in left["series"]] == ["New logos"]
    assert (right["format"], right["unit"], right["target"]) == ("currency", "EUR", 250_000)
    assert [entry["name"] for entry in right["series"]] == ["ASP"]
    # The count's labels stay whole: its own axis never reaches the compact threshold.
    assert label_number_format(left, [24, 56]) == "#,##0"
    # A chart with one axis is its own view.
    single = {"format": "percent", "unit": "pts", "series": [{"name": "A", "values": [1]}]}
    assert axis_view(single, "secondary") == single


def test_a_two_unit_axis_keeps_its_unit_on_its_ticks():
    """Each axis of a two-unit chart says which unit it reads, as the viewer
    writes it (``formatChartValue(..., { axisUnit: true })``)."""
    ratio = {"format": "number", "unit": "x"}
    code = axis_number_format(ratio, [0, 0.5, 1, 1.5, 2], with_unit=True)
    assert code == '#,##0.0"x"'
    assert _excel(code, 1.5) == "1.5x"
    assert axis_number_format(ratio, [0, 0.5, 1, 1.5, 2]) == "#,##0.0"
    # A large axis compacts and keeps the unit after the letter.
    counts = {"format": "number", "unit": "logos"}
    compact = axis_number_format(counts, [0, 25_000, 50_000], with_unit=True)
    assert _excel(compact, 50_000) == "50k logos"
    # Currency and percent axes already say their unit.
    assert axis_number_format({"format": "percent"}, [0, 50, 100], with_unit=True) == '#,##0"%"'
    assert axis_number_format({"format": "currency", "unit": "EUR"}, [0, 300_000], with_unit=True).count('"\u20ac"') == 2


def test_series_values_can_be_read_for_the_series_of_one_plot():
    chart = {"series": [{"values": [1, 2]}, "noise", {"values": [30, 40]}, {"values": [500]}]}
    # Positions count the series objects only, the writer's plotted series.
    assert series_values(chart, [1]) == [30.0, 40.0]
    assert series_values(chart, [0, 2]) == [1.0, 2.0, 500.0]
    assert series_values(chart, []) == []


# ── Currency signs ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("unit", "sign"),
    [
        (None, "\u20ac"),
        ("", "\u20ac"),
        ("EUR", "\u20ac"),
        ("usd", "$"),
        ("GBP", "\u00a3"),
        ("JPY", "\u00a5"),
        ("$", "$"),
        ("CHF", "CHF"),
    ],
)
def test_a_currency_unit_is_written_as_its_sign(unit, sign):
    assert currency_sign(unit) == sign


def test_an_amount_takes_its_currency_or_unit_the_way_the_viewer_writes_it():
    """The rules `formatChartValue` writes a label by, for the text the
    backend writes itself (a waterfall's refusal names its figures)."""
    assert with_currency("16.5M", None) == "\u20ac16.5M"
    assert with_currency("16.5M", "USD") == "$16.5M"
    assert with_currency("16.5M", "CHF") == "CHF 16.5M"
    assert with_unit("1,204", "users") == "1,204 users"
    assert with_unit("48", "h") == "48h"
    assert with_unit("12", None) == "12"
    assert compact_tier(19_200_000) == (1e6, "M")
    assert compact_tier(-400_000) == (1e3, "k")
    assert compact_tier(2_500_000_000) == (1e9, "B")
    assert compact_tier(850) == (1.0, "")


# ── Compact formats ───────────────────────────────────────────────────────────


def _excel(code: str, value: float) -> str:
    """A small reading of the format codes this module writes, enough to check
    what Excel shows for a value: the first section whose condition holds, the
    thousands-scaling commas, the decimal digits, the quoted literals."""
    for section in code.split(";"):
        condition = re.match(r"^\[>=(\d+)\](.*)$", section)
        body = section
        if condition:
            if abs(value) < float(condition.group(1)):
                continue
            body = condition.group(2)
        number = re.search(r"#,##0(?:\.(0+))?(,*)", body)
        assert number, body
        scaled = abs(value) / (1000 ** len(number.group(2)))
        # Excel rounds the decimal it shows half away from zero, not the binary value.
        places = Decimal(1).scaleb(-len(number.group(1) or ""))
        digits = f"{Decimal(repr(scaled)).quantize(places, rounding=ROUND_HALF_UP):,f}"
        rendered = body[: number.start()] + digits + body[number.end() :]
        return ("-" if value < 0 else "") + re.sub(r'"([^"]*)"', r"\1", rendered)
    raise AssertionError(f"no section of {code!r} accepts {value}")


def test_compact_currency_reads_as_the_viewer_writes_it():
    code = compact_format([11_200_000, 16_500_000, 527_100, 850], prefix='"\u20ac"')
    assert code == '[>=1000000]"\u20ac"#,##0.0,,"M";[>=1000]"\u20ac"#,##0.0,"k";"\u20ac"#,##0'
    assert _excel(code, 16_500_000) == "\u20ac16.5M"
    assert _excel(code, 527_100) == "\u20ac527.1k"
    assert _excel(code, 850) == "\u20ac850"


def test_a_band_writes_a_decimal_only_where_one_of_its_values_needs_one():
    # Every million-band value is whole: no ``.0`` on the ticks (``€30M``).
    whole = compact_format([0, 10_000_000, 20_000_000, 30_000_000], prefix='"\u20ac"')
    assert whole.startswith('[>=1000000]"\u20ac"#,##0,,"M"')
    assert _excel(whole, 30_000_000) == "\u20ac30M"
    # One value with a decimal makes the whole band carry it (``€17.0M`` next
    # to ``€16.5M`` is the one thing Excel cannot avoid).
    mixed = compact_format([16_500_000, 17_000_000], prefix='"\u20ac"')
    assert _excel(mixed, 17_000_000) == "\u20ac17.0M"


def test_small_values_are_plain_numbers_with_a_thousands_separator():
    assert compact_format([120, 470, 999]) == "#,##0"
    assert compact_format([1.5, 2]) == "#,##0.0"
    assert _excel(compact_format([120, 470]), 470) == "470"


def test_the_two_reached_tiers_are_conditions_and_the_third_is_the_plain_section():
    code = compact_format([2_500_000_000, 750_000_000, 4_000_000, 12_000])
    sections = code.split(";")
    assert len(sections) == 3
    assert sections[0].startswith("[>=1000000000]") and sections[0].endswith('"B"')
    assert sections[1].startswith("[>=1000000]") and sections[1].endswith('"M"')
    # Excel allows two conditions: the thousands are the unconditional rest.
    assert not sections[2].startswith("[") and sections[2].endswith('"k"')
    assert _excel(code, 2_500_000_000) == "2.5B"
    assert _excel(code, 12_000) == "12k"


# ── Label and axis formats ────────────────────────────────────────────────────


def test_percent_labels_and_axes_write_the_value_as_a_percentage():
    """A quoted ``%`` is a literal: Excel writes the value as it is, never times 100."""
    chart = {"format": "percent"}
    assert label_number_format(chart, [71, 86]) == '#,##0"%"'
    assert axis_number_format(chart, [0, 25, 50, 75, 100]) == '#,##0"%"'
    assert _excel(label_number_format(chart, [86]), 86) == "86%"
    # As precise as the viewer: 12.345 reads 12.3%, not every digit Excel keeps.
    assert _excel(label_number_format(chart, [12.345, 64]), 12.345) == "12.3%"


def test_number_labels_carry_the_unit_and_axes_drop_it():
    chart = {"format": "number", "unit": "users"}
    assert label_number_format(chart, [120, 1204]) == '#,##0" users"'
    assert _excel(label_number_format(chart, [1204]), 1204) == "1,204 users"
    assert axis_number_format(chart, [0, 500, 1000, 1500]) == "#,##0"
    # A one-letter unit sticks to the number, as ``48h`` in the viewer.
    hours = {"unit": "h"}
    assert label_number_format(hours, [12, 48]) == '#,##0"h"'


def test_a_large_number_axis_writes_compact_ticks():
    chart = {"format": "number"}
    assert axis_number_format(chart, [0, 5_000, 10_000, 15_000]) == '[>=1000]#,##0,"k";#,##0'
    assert _excel(axis_number_format(chart, [0, 5_000, 10_000, 15_000]), 15_000) == "15k"
    # Below the compaction threshold the ticks stay plain, however many digits.
    below = [0, AXIS_COMPACT_FROM / 2]
    assert axis_number_format(chart, below) == "#,##0"


def test_currency_formats_take_the_units_sign():
    chart = {"format": "currency", "unit": "USD"}
    # Reaching the millions means the thousands tier is reached too: it is a
    # band of its own (empty here, hence whole), before the plain rest.
    code = label_number_format(chart, [12_400_000, 1_500_000])
    assert code == '[>=1000000]"$"#,##0.0,,"M";[>=1000]"$"#,##0,"k";"$"#,##0'
    assert _excel(code, 12_400_000) == "$12.4M"
    # A code with no sign of its own is written out with a space.
    swiss = {"format": "currency", "unit": "CHF"}
    assert label_number_format(swiss, [950]) == '"CHF "#,##0'


# ── Decimals ──────────────────────────────────────────────────────────

#: The burn multiple of slide 3 of the Q3 2026 quarterly review.
BURN_MULTIPLE = [0.96, 1.04, 0.47, 1.57, 1.18, 1.56, 1.27]


def _burn_and_net_new_arr(**extra) -> dict:
    """The payload of slide 3: net new ARR and burn as euro bars, the burn
    multiple as a line against the right-hand axis (``chartExportPayload``)."""
    return {
        "kind": "combo",
        "format": "currency",
        "unit": "EUR",
        "decimals": None,
        "secondary_format": "number",
        "secondary_unit": "x",
        "secondary_decimals": None,
        "series": [
            {"name": "Net new ARR", "kind": "bar", "values": [1_460_000, 1_950_000, 4_100_000, 1_600_000]},
            {"name": "Burn", "kind": "bar", "values": [1_400_000, 1_990_000, 1_700_000, 2_980_000]},
            {"name": "Burn multiple", "kind": "line", "axis": "secondary", "values": BURN_MULTIPLE},
        ],
        **extra,
    }


def test_the_right_hand_axis_writes_the_decimals_the_chart_sets():
    """With ``secondary_decimals: 2`` the
    PPTX labels and the right-hand ticks read like the KPI tile (1.27x)."""
    right = axis_view(_burn_and_net_new_arr(secondary_decimals=2), "secondary")
    labels = label_number_format(right, BURN_MULTIPLE)
    assert labels == '#,##0.00"x"'
    assert [_excel(labels, value) for value in BURN_MULTIPLE] == [
        "0.96x",
        "1.04x",
        "0.47x",
        "1.57x",
        "1.18x",
        "1.56x",
        "1.27x",
    ]
    # Every tick takes them too, a round one included.
    ticks = axis_number_format(right, [0, 0.5, 1, 1.5, 2], with_unit=True)
    assert [_excel(ticks, tick) for tick in (0, 1.5)] == ["0.00x", "1.50x"]
    # The left-hand axis keeps its own reading.
    left = axis_view(_burn_and_net_new_arr(secondary_decimals=2), "primary")
    # Its bars run 1.95M and 1.99M, which both read 2.0M at one decimal as
    # Intl rounds a tie: the axis takes a second one.
    assert _excel(label_number_format(left, [1_460_000]), 1_460_000) == "\u20ac1.46M"


def test_a_currency_writes_the_decimals_of_its_compact_amount():
    chart = {"format": "currency", "unit": "EUR", "decimals": 2}
    code = label_number_format(chart, [19_140_000, 527_100, 850])
    assert [_excel(code, value) for value in (19_140_000, 527_100, 850)] == [
        "\u20ac19.14M",
        "\u20ac527.10k",
        "\u20ac850.00",
    ]
    assert _excel(label_number_format({**chart, "decimals": 0}, [19_140_000]), 19_140_000) == "\u20ac19M"


def test_a_percent_or_a_number_writes_the_decimals_the_chart_sets():
    assert _excel(label_number_format({"format": "percent", "decimals": 1}, [86]), 86) == "86.0%"
    assert label_number_format({"format": "number", "unit": "h", "decimals": 3}, [4]) == '#,##0.000"h"'
    assert axis_number_format({"format": "number", "decimals": 1}, [0, 10, 20]) == "#,##0.0"


def test_without_decimals_the_burn_multiple_keeps_two_significant_digits_and_every_value_its_own_label():
    """Unset, the line is written the way the viewer writes it: 0.47 needs two
    decimals for two significant digits, so the run takes two and 1.57 / 1.56
    never both read 1.5x."""
    right = axis_view(_burn_and_net_new_arr(), "secondary")
    assert run_decimals(BURN_MULTIPLE, compact=False) == 2
    labels = label_number_format(right, BURN_MULTIPLE)
    assert len({_excel(labels, value) for value in BURN_MULTIPLE}) == len(BURN_MULTIPLE)
    assert _excel(labels, 1.27) == "1.27x"


@pytest.mark.parametrize(
    ("values", "compact", "decimals"),
    [
        # Two significant digits under 10, one decimal at most from 10 on.
        ([1.4, 1.6, 2], False, 1),
        ([0.96, 1.5], False, 2),
        ([0.053, 0.08], False, 3),
        ([12.4, 527.1], False, 1),
        # One more while two different values would read the same.
        ([1.57, 1.56, 1.27], False, 2),
        ([12.31, 12.34], False, 2),
        ([16_510_000, 16_540_000], True, 2),
        # Float noise is not a difference: 0.1 + 0.2 reads 0.3, no decimal added for it.
        ([0.3, 0.1 + 0.2], False, 2),
        # A value compact in another tier reads apart from its letter alone.
        ([999_960, 1_000_000], True, 1),
    ],
)
def test_a_run_of_values_takes_the_decimals_that_read_it(values, compact, decimals):
    assert run_decimals(values, compact=compact) == decimals


@pytest.mark.parametrize(
    ("value", "written"),
    [(1.005, "1.01"), (2.675, "2.68"), (0.125, "0.13"), (1.015, "1.02")],
)
def test_a_tie_rounds_the_way_the_viewer_rounds_it(value, written):
    """A tie rounds half away from zero on the shortest decimal that reads the
    float back, as Intl.NumberFormat does, not on its binary value
    (Decimal(1.005) is 1.00499999...)."""
    assert _excel(label_number_format({"decimals": 2}, [value]), value) == written


def test_a_run_with_a_tie_takes_the_decimals_the_viewer_takes():
    """1.005 and 1.01 read the same at two decimals in the viewer (both 1.01),
    so the run takes a third, as chartValueWriter does."""
    assert run_decimals([1.005, 1.01], compact=False) == 3


def test_every_plot_reading_one_axis_writes_its_decimals():
    """A combo's bars and line on one axis are two plots, but one run: the
    bars write 12.25 next to the line's 0.53, as the viewer writes them."""
    chart = {
        "kind": "combo",
        "format": "number",
        "series": [
            {"name": "Deals", "kind": "bar", "values": [12.25, 13.5]},
            {"name": "Win rate", "kind": "line", "values": [0.53, 0.8]},
        ],
    }
    assert view_values(chart) == [12.25, 13.5, 0.53, 0.8]
    assert _excel(label_number_format(chart, [12.25, 13.5]), 12.25) == "12.25"


def test_a_stack_reads_its_totals_and_its_target_in_its_run():
    chart = {"stacked": True, "target": 7, "series": [{"values": [1, -2, 3]}, {"values": [4, 5, -6]}]}
    assert view_values(chart) == [1, -2, 3, 4, 5, -6, 5, 0, 5, -2, 3, -6, 7]


def test_series_values_flatten_the_plotted_numbers_and_read_garbage_as_zero():
    chart = {
        "series": [
            {"name": "A", "values": [1, 2.5, "x", True, None]},
            "not a series",
            {"name": "B", "values": "no"},
            {"name": "C", "values": [3]},
        ]
    }
    assert series_values(chart) == [1.0, 2.5, 0.0, 0.0, 0.0, 3.0]
    assert series_values({}) == []
    assert series_values({"series": "nope"}) == []


# ── Unit scale ────────────────────────────────────────────────────────

#: Two rises, a fall under a million, the ending.
BRIDGE = [2_500_000, 1_250_000, -250_000, 3_500_000]
EURO = "\u20ac"


def _pinned(scale: str, **extra) -> dict:
    return {"kind": "waterfall", "format": "currency", "unit": "EUR", "unit_scale": scale, **extra}


def test_a_pinned_scale_writes_every_amount_in_its_one_letter():
    """With ``unit_scale: "M"`` and two
    decimals the bridge reads in millions only - the fall under a million
    too - in one format section PowerPoint applies to the sign itself. The
    viewer adds the ``+`` of a rise (``waterfallLabel``)."""
    chart = _pinned("M", decimals=2)
    labels = label_number_format(chart, BRIDGE)
    assert labels == f'"{EURO}"#,##0.00,,"M"'
    assert [_excel(labels, value) for value in BRIDGE] == [
        f"{EURO}2.50M",
        f"{EURO}1.25M",
        f"-{EURO}0.25M",
        f"{EURO}3.50M",
    ]
    ticks = axis_number_format(chart, [0, 1_000_000, 2_000_000, 3_000_000, 4_000_000])
    assert ticks == labels
    assert _excel(ticks, 0) == f"{EURO}0.00M"


def test_a_stacked_bar_reads_its_segments_in_the_pinned_letter():
    """``EUR 0.5M`` beside ``EUR 1.2M``, never ``EUR 480k``: a stack's segment
    labels and its axis take the chart's scale and decimals."""
    chart = {**_pinned("M", decimals=1), "kind": "stacked_bar", "stacked": True}
    code = label_number_format(chart, [480_000, 1_200_000])
    assert [_excel(code, value) for value in (480_000, 1_200_000)] == [f"{EURO}0.5M", f"{EURO}1.2M"]


def test_an_amount_that_rounds_to_zero_in_the_letter_keeps_the_decimals():
    code = label_number_format(_pinned("M", decimals=2), [1_000, 2_500_000])
    assert _excel(code, 1_000) == f"{EURO}0.00M"
    billions = label_number_format(_pinned("B", decimals=1), [40_000_000, 2_500_000_000])
    assert [_excel(billions, value) for value in (40_000_000, 2_500_000_000)] == [f"{EURO}0.0B", f"{EURO}2.5B"]


def test_without_decimals_a_pinned_run_reads_its_amounts_at_that_letter():
    """Unset decimals keep the viewer's rule, applied to the pinned amounts:
    0.48M needs two decimals for two significant digits."""
    values = [480_000, 1_200_000]
    series = [{"name": "ARR", "values": values}]
    assert run_decimals(values, compact=True, pinned=pinned_tier(_pinned("M"))) == 2
    code = label_number_format(_pinned("M", series=series), values)
    assert _excel(code, 480_000) == f"{EURO}0.48M"
    thousands = label_number_format(_pinned("k", series=series), values)
    assert [_excel(thousands, value) for value in values] == [f"{EURO}480k", f"{EURO}1,200k"]


@pytest.mark.parametrize(
    "chart",
    [
        {"kind": "pie", "format": "currency", "unit_scale": "M"},
        {"kind": "donut", "format": "currency", "unit_scale": "M"},
        {"kind": "gauge", "format": "currency", "unit_scale": "M"},
        {"kind": "bar", "format": "number", "unit_scale": "M"},
        {"kind": "bar", "format": "percent", "unit_scale": "M"},
        {"kind": "bar", "format": "currency", "unit_scale": "auto"},
        {"kind": "bar", "format": "currency", "unit_scale": None},
    ],
)
def test_a_scale_the_chart_ignores_leaves_its_formats_as_they_were(chart):
    """Byte-identical where the scale does not apply: a pie, a donut, a
    gauge, a number, a percent, ``auto`` and unset write today's codes."""
    without = {key: value for key, value in chart.items() if key != "unit_scale"}
    values = [480_000, 1_200_000]
    assert pinned_tier(chart) is None
    assert label_number_format(chart, values) == label_number_format(without, values)
    assert axis_number_format(chart, [0, 1_000_000]) == axis_number_format(without, [0, 1_000_000])


def test_each_axis_reads_its_own_unit_scale():
    """The right-hand axis follows ``secondary_unit_scale`` and
    ``secondary_decimals``; the left-hand one keeps its own reading."""
    chart = {
        "kind": "combo",
        "format": "currency",
        "unit": "EUR",
        "unit_scale": "M",
        "decimals": 1,
        "secondary_format": "currency",
        "secondary_unit": "USD",
        "secondary_unit_scale": "k",
        "secondary_decimals": 0,
        "series": [
            {"name": "ARR", "kind": "bar", "values": [480_000, 1_200_000]},
            {"name": "ASP", "kind": "line", "axis": "secondary", "values": [1_250_000, 98_000]},
        ],
    }
    left, right = axis_view(chart, "primary"), axis_view(chart, "secondary")
    assert (left["unit_scale"], right["unit_scale"]) == ("M", "k")
    assert _excel(label_number_format(left, [480_000]), 480_000) == f"{EURO}0.5M"
    assert [_excel(label_number_format(right, [1_250_000, 98_000]), v) for v in (1_250_000, 98_000)] == [
        "$1,250k",
        "$98k",
    ]
    assert axis_number_format(right, [0, 500_000, 1_000_000, 1_500_000], with_unit=True) == '"$"#,##0,"k"'


def test_the_unit_scales_are_the_compact_tiers_on_both_sides():
    """The spec's letters, the writer's tiers and the viewer's steps are one
    table: a letter the spec accepts is a tier both sides write."""
    from pathlib import Path

    letters = {tier[2] for tier in (pinned_tier(_pinned(scale)) for scale in ("k", "M", "B")) if tier}
    assert set(get_args(ChartUnitScale)) == {"auto", *letters} == {"auto", "k", "M", "B"}
    blocks = Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "presentation" / "blocks"
    source = (blocks / "chart-format.ts").read_text(encoding="utf-8")
    for limit, letter in (("1e9", "B"), ("1e6", "M"), ("1e3", "k")):
        assert f'[{limit}, "{letter}"]' in source, letter
    assert "COMPACT_STEPS.find(([, letter]) => letter === block.unit_scale)" in source


def test_the_format_rules_mirror_the_viewers_formatter():
    """The viewer's ``chart-format.ts`` is the source the codes mirror: the
    currency signs and the axis compaction threshold are the same on both sides."""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2]
        / "frontend"
        / "src"
        / "components"
        / "presentation"
        / "blocks"
        / "chart-format.ts"
    ).read_text(encoding="utf-8")
    assert f"export const AXIS_COMPACT_FROM = {AXIS_COMPACT_FROM:_};" in source
    assert f"export const MAX_AUTO_DECIMALS = {MAX_AUTO_DECIMALS};" in source
    for code, sign in (("EUR", "\u20ac"), ("USD", "$"), ("GBP", "\u00a3")):
        assert re.search(rf'{code}:\s*"{re.escape(sign)}"', source), f"{code} sign differs from the viewer"
    # A waterfall's bridge is checked by the validator and drawn by the viewer
    # (its gap steps): the two hold a total to the same slack.
    from app.services import presentation_spec

    assert f"export const DEFAULT_WATERFALL_TOLERANCE = {presentation_spec.DEFAULT_WATERFALL_TOLERANCE};" in source
    slack = re.search(r"const BRIDGE_FLOAT_SLACK = ([\d.e+-]+);", source)
    assert slack and float(slack.group(1)) == presentation_spec._BRIDGE_FLOAT_SLACK
    assert "Math.abs(total - running) > (tolerance / 100) * moved + slack" in source


# ── Text scale ────────────────────────────────────────────────────────


def test_the_text_scales_mirror_the_viewers_and_the_stylesheets():
    """``label_size`` sets a chart's ticks and the sizes its values may take:
    the planner (``chart-layout.ts``), the stylesheet that sets every glyph of
    the canvas and the writer's fallback read one table, one entry per size
    the spec accepts."""
    from pathlib import Path

    presentation = Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "presentation"
    source = (presentation / "blocks" / "chart-layout.ts").read_text(encoding="utf-8")
    entry = r"(\w+): \{ tick: (\d+), values: \[([\d, ]+)\], segmentFloor: \d+ \}"
    declared = {
        name: (int(tick), tuple(int(v) for v in values.split(",")))
        for name, tick, values in re.findall(entry, source)
    }
    assert declared == CHART_TEXT_SCALES
    assert set(CHART_TEXT_SCALES) == set(get_args(Size))
    css = (presentation / "presentation.css").read_text(encoding="utf-8")
    assert f".deck-root .deck-chart {{ --deck-chart-text: {CHART_TEXT_SCALES['md'][0]}px; }}" in css
    for name in ("sm", "lg"):
        tick = CHART_TEXT_SCALES[name][0]
        assert f'.deck-root .deck-chart[data-label-size="{name}"] {{ --deck-chart-text: {tick}px; }}' in css, name
    assert f"font-size: var(--deck-chart-text, {CHART_TEXT_SCALES['md'][0]}px);" in css


def test_the_tallest_chart_canvas_is_the_stylesheets_size_lg_for_the_planner_and_the_layout_check():
    """The drawing plans a stack hiding values at the tallest canvas a
    slide gives a chart, and the layout check recommends more height only when
    that plan writes them all and names that height - the canvas of ``size:
    lg`` in the stylesheet, one number in all three places."""
    from pathlib import Path

    from app.services.presentation_layout import CHART_TALLEST_CANVAS_PX

    presentation = Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "presentation"
    source = (presentation / "blocks" / "chart-layout.ts").read_text(encoding="utf-8")
    css = (presentation / "presentation.css").read_text(encoding="utf-8")
    assert f"export const TALLEST_CHART_CANVAS = {CHART_TALLEST_CANVAS_PX};" in source
    assert f'.deck-root .deck-chart[data-size="lg"] {{ --deck-chart-h: {CHART_TALLEST_CANVAS_PX}px; }}' in css


def test_a_payload_ticks_in_the_size_its_label_size_names():
    assert chart_tick_px({"label_size": "sm"}) == 18
    assert chart_tick_px({"label_size": "lg"}) == 26
    # A payload from an older page, or one that names no size, reads at md.
    assert chart_tick_px({}) == chart_tick_px({"label_size": "xl"}) == chart_tick_px({"label_size": 3}) == 22


# ── The values' own size ──────────────────────────────────────────────


def test_a_payload_tries_its_values_at_the_size_its_value_label_size_names_and_its_ticks_stay():
    """A text scale's first value size, or the number of px named; a payload
    that names none - or one the spec would refuse - says so (``None``), and
    the writer then sizes the values like the ticks, as before."""
    assert chart_value_px({"label_size": "md", "value_label_size": 20}) == 20
    assert chart_tick_px({"label_size": "md", "value_label_size": 20}) == 22
    for name, (_tick, values) in CHART_TEXT_SCALES.items():
        assert chart_value_px({"label_size": "lg", "value_label_size": name}) == values[0]
    assert chart_value_px({"value_label_size": VALUE_LABEL_SIZE_MIN_PX}) == VALUE_LABEL_SIZE_MIN_PX
    assert chart_value_px({"value_label_size": VALUE_LABEL_SIZE_MAX_PX}) == VALUE_LABEL_SIZE_MAX_PX
    for unnamed in ({}, {"label_size": "sm"}, {"value_label_size": None}):
        assert chart_value_px(unnamed) is None
    for refused in ("xl", 9, 33, 20.5, True, "20"):
        assert chart_value_px({"value_label_size": refused}) is None, refused


def test_the_value_size_domain_and_ladder_mirror_the_viewers():
    """The bounds a ``value_label_size`` number may take are one pair in the
    planner, the spec and the writer; the ladder the planner derives from it
    steps as the text scales do - 2px at a time, 8px deep, segments 6px under
    - which the layout check's remedy relies on (``_smaller_value_label_size``)."""
    from itertools import pairwise
    from pathlib import Path

    from pydantic import TypeAdapter, ValidationError

    from app.services import presentation_layout
    from app.services.presentation_spec import ValueLabelSize

    presentation = Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "presentation"
    fmt = (presentation / "blocks" / "chart-format.ts").read_text(encoding="utf-8")
    layout = (presentation / "blocks" / "chart-layout.ts").read_text(encoding="utf-8")
    assert f"export const VALUE_LABEL_SIZE_MIN = {VALUE_LABEL_SIZE_MIN_PX};" in fmt
    assert f"export const VALUE_LABEL_SIZE_MAX = {VALUE_LABEL_SIZE_MAX_PX};" in fmt
    assert f"const VALUE_LADDER_STEP = {presentation_layout._VALUE_LADDER_STEP_PX};" in layout
    assert "const VALUE_LADDER_REACH = 8;" in layout
    assert "const VALUE_SEGMENT_REACH = 6;" in layout
    # Every text scale's own ladder steps alike, which a number's ladder copies.
    for _tick, values in CHART_TEXT_SCALES.values():
        assert all(a - b == presentation_layout._VALUE_LADDER_STEP_PX for a, b in pairwise(values))
    adapter = TypeAdapter(ValueLabelSize)
    for accepted in (*CHART_TEXT_SCALES, VALUE_LABEL_SIZE_MIN_PX, VALUE_LABEL_SIZE_MAX_PX):
        assert adapter.validate_python(accepted) == accepted
    for refused in ("xl", VALUE_LABEL_SIZE_MIN_PX - 1, VALUE_LABEL_SIZE_MAX_PX + 1):
        with pytest.raises(ValidationError):
            adapter.validate_python(refused)


# ── The butterfly ─────────────────────────────────────────────────────


def _excel_mirrored(code: str, value: float) -> str:
    """What Excel shows for ``value`` under a code of two or three sections
    with no condition: the first section for a positive number (and a zero
    when there is no third), the second for a negative one - which Excel
    writes WITHOUT a sign of its own - and the third for a zero, an empty
    one writing nothing."""
    sections = code.split(";")
    assert len(sections) in (2, 3) and not any(section.startswith("[") for section in sections), code
    if value == 0 and len(sections) == 3:
        return _excel(sections[2], 0.0) if sections[2] else ""
    return _excel(sections[1] if value < 0 else sections[0], abs(value))


def test_a_butterfly_is_one_native_clustered_bar_chart_never_an_emulation_nor_a_picture():
    from app.services.presentation_spec import MIRRORED_CHART_KINDS as SPEC_MIRRORED

    assert MIRRORED_CHART_KINDS == SPEC_MIRRORED == {"butterfly"}
    assert MIRRORED_CHART_KINDS <= NATIVE_CHART_KINDS
    assert not MIRRORED_CHART_KINDS & (EMULATED_CHART_KINDS | PICTURE_CHART_KINDS)


def test_a_butterfly_writes_its_left_side_stored_negative_as_the_size_the_viewer_writes():
    """The left series is stored negative in PowerPoint; its labels and ticks
    read the size the viewer writes - the chart's own code as its positive
    and its negative section, the ``0;0`` of the issue."""
    pyramid = {
        "kind": "butterfly",
        "format": "number",
        "unit": None,
        "series": [{"values": [0, 2, 10, 40, 80, 30, 15]}, {"values": [5, 20, 18, 6, 0, 0, 0]}],
    }
    labels = mirrored_number_format(pyramid, [-0.0, -2, -10, -40, -80, -30, -15, 5, 20, 18, 6, 0, 0, 0])
    assert labels == "#,##0;#,##0;"
    assert [_excel_mirrored(labels, value) for value in (-80, -2, 20)] == ["80", "2", "20"]
    axis = mirrored_number_format(pyramid, [-80, -60, -40, -20, 0, 20, 40, 60, 80], axis=True)
    assert axis == "#,##0;#,##0"
    assert _excel_mirrored(axis, -60) == "60"
    # A unit rides the labels, a percent its sign - never a minus.
    people = mirrored_number_format({**pyramid, "unit": "people"}, [-80, 20])
    assert _excel_mirrored(people, -80) == "80 people"
    assert mirrored_number_format({**pyramid, "unit": "people"}, [-80, 20], axis=True) == "#,##0;#,##0"
    shares = mirrored_number_format({**pyramid, "format": "percent"}, [-42.5, 12])
    assert _excel_mirrored(shares, -42.5) == "42.5%"


def test_a_butterfly_labels_a_zero_with_nothing_and_its_axis_keeps_its_zero():
    """A zero is a rung one side does not have - the viewer draws no
    bar and writes no value for it - so the labels' code ends on an empty
    zero section and PowerPoint labels it with nothing, on either side (the
    left zero is stored 0, never -0). The axis keeps two sections: its 0 tick
    is a tick. A value that is not exactly zero is still written."""
    pyramid = {
        "kind": "butterfly",
        "format": "number",
        "unit": None,
        "series": [{"values": [0, 2, 10, 40, 80, 30, 15]}, {"values": [5, 20, 18, 6, 0, 0, 0]}],
    }
    labels = mirrored_number_format(pyramid, [0, -2, -10, -40, -80, -30, -15, 5, 20, 18, 6, 0, 0, 0])
    assert labels.split(";")[2] == ""
    assert [_excel_mirrored(labels, value) for value in (0, -0.0, 0.0)] == ["", "", ""]
    axis = mirrored_number_format(pyramid, [-80, -40, 0, 40, 80], axis=True)
    assert axis.count(";") == 1 and _excel_mirrored(axis, 0) == "0"
    # Every format a butterfly writes in: a unit, a percent, an amount in one tier.
    for chart, values in (
        ({**pyramid, "unit": "people"}, [-80, 20, 0]),
        ({**pyramid, "format": "percent"}, [-42.5, 12, 0]),
        ({**pyramid, "format": "currency", "unit": "EUR"}, [-16_800_000, 480_000, 0]),
    ):
        code = mirrored_number_format(chart, values)
        assert code.count(";") == 2 and code.endswith(";"), code
        assert _excel_mirrored(code, 0) == "", code
        assert _excel_mirrored(code, values[1]) not in ("", "0"), code
    # Every other kind keeps its code: a bar chart's has one section, so PowerPoint still labels its 0.
    assert ";" not in label_number_format({"kind": "bar", "format": "number", "series": [{"values": [12, 0, 18]}]}, [12, 0, 18])


def test_a_butterfly_of_amounts_reads_them_in_one_tier_since_a_band_has_no_negative_section():
    """Excel gives a code of conditional bands no negative section: amounts
    reaching two tiers are written in the larger one, as a unit scale pins it,
    with the decimals that tell them apart; one tier keeps its own code."""
    money = {
        "kind": "butterfly",
        "format": "currency",
        "unit": "EUR",
        "series": [{"values": [16_800_000, 480_000]}, {"values": [1_200_000, 0]}],
    }
    labels = mirrored_number_format(money, [-16_800_000, -480_000, 1_200_000, 0])
    assert "[" not in labels and labels.count(";") == 2
    assert _excel_mirrored(labels, -16_800_000) == "\u20ac16.80M"
    assert _excel_mirrored(labels, -480_000) == "\u20ac0.48M"
    assert _excel_mirrored(labels, 1_200_000) == "\u20ac1.20M"
    ticks = mirrored_number_format(money, [-20_000_000, -10_000_000, 0, 10_000_000, 20_000_000], axis=True)
    assert _excel_mirrored(ticks, -20_000_000) == "\u20ac20M"
    # The chart's own decimals hold; a pinned unit scale is the tier already.
    assert _excel_mirrored(mirrored_number_format({**money, "decimals": 1}, [-16_800_000, -480_000]), -480_000) == "\u20ac0.5M"
    pinned = mirrored_number_format({**money, "unit_scale": "M", "decimals": 2}, [-16_800_000, -480_000])
    assert _excel_mirrored(pinned, -480_000) == "\u20ac0.48M"
    # Large counts compact the same way, with the decimals two significant digits of the smallest need.
    crowds = {"kind": "butterfly", "format": "number", "unit": "users", "series": [{"values": [2_500_000]}, {"values": [18_000]}]}
    code = mirrored_number_format(crowds, [-2_500_000, 18_000])
    assert _excel_mirrored(code, -2_500_000) == "2.500M users" and _excel_mirrored(code, 18_000) == "0.018M users"


def test_the_spine_note_says_where_powerpoint_writes_a_butterfly_s_categories():
    note = spine_note(4, "butterfly")
    assert note == (
        "Slide 4: the butterfly chart's categories are on its left axis - PowerPoint's bar chart has no column "
        "between its two sides to write them in."
    )
    assert note.isascii() and "\n" not in note


def test_the_writer_s_category_column_mirrors_the_viewer_s_glyph_estimate():
    """The column a butterfly's categories take on PowerPoint's left axis is
    the viewer's own estimate of a tick label (``textWidth``)."""
    from pathlib import Path

    from app.services import pptx_writer

    presentation = Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "presentation"
    layout = (presentation / "blocks" / "chart-layout.ts").read_text(encoding="utf-8")
    assert f"const GLYPH_EM = {pptx_writer._TICK_GLYPH_EM};" in layout
    assert f"export const AXIS_PADDING = {pptx_writer._CATEGORY_AXIS_PADDING_PX};" in layout
