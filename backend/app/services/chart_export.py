# SPDX-License-Identifier: Apache-2.0

"""The export rules of the native ``chart`` block, apart from PowerPoint.

Which chart kinds PowerPoint has a native chart type for, how a value written
by the viewer (``chart-format.ts``) is written by Excel (a number format code),
and what the export log says about a chart that had to stay a picture. Pure
Python, no ``python-pptx``: ``presentation_export.py`` injects
:data:`NATIVE_CHART_KINDS` and :data:`TWO_AXIS_PICTURE_SERIES_KINDS` into the
primitive extractor so the browser and the writer agree on which figure
becomes a chart, and ``pptx_writer.py`` turns the format codes into
``c:numFmt`` elements.

A chart that reads a second unit against a right-hand value axis is
written per axis: :func:`axis_view` is the chart as that axis reads it (its
series, its target, how it writes a number), and its ticks carry the unit
(``0.5x``, ``1x``) so each axis says what it reads.

The number formats MIRROR ``formatChartValue`` in
``frontend/src/components/presentation/blocks/chart-format.ts``: a ``currency``
amount is compact with the currency's sign (``€16.5M``, ``€527.1k``), a
``percent`` value IS the percentage (``86%``), a ``number`` label carries the
unit (``1,204 users``) and a large ``number`` axis writes compact ticks - as
do the labels of an axis chart whose axis does (``2.2M users``).

The decimals are the viewer's too: a chart's ``decimals`` (its
``secondary_decimals`` on the right-hand axis) writes every value with that
many; unset, a run of values - an axis' ticks, or its labels - takes the
decimals :func:`run_decimals` reads off it, as ``chartValueWriter`` does.
One format code serves a whole band of a series (or of an axis), so the one
thing Excel cannot mirror is the trailing zero the viewer drops: a band
writes as many decimals as its most precise value shows (``€17.0M`` next to
``€16.5M``).

A currency axis whose chart pins a ``unit_scale`` (``secondary_unit_scale``
on the right-hand axis) writes every amount in that one letter: the format code
is ONE section at that tier (``"€"#,##0.00,,"M"`` reads ``-250000`` as
``-€0.25M``), as :func:`pinned_tier` and ``pinnedTier`` decide alike.

A butterfly is a clustered bar chart whose left series is stored
negative: :func:`mirrored_number_format` writes its labels and its axis with
the code a bar chart would take, twice (``#,##0;#,##0``) - the second section
is the negative one, and it carries no sign, so PowerPoint writes the size the
viewer writes. Its labels add an empty zero section (``#,##0;#,##0;``): a
zero, which the viewer draws no bar and writes no value for, is labelled
with nothing.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal

#: Chart kinds exported as a native, editable PowerPoint chart. A ``combo`` is
#: one plot per way its series are drawn (bars, lines) in one chart.
NATIVE_CHART_KINDS: frozenset[str] = frozenset(
    {"bar", "stacked_bar", "line", "area", "pie", "donut", "combo", "waterfall", "butterfly"}
)
#: Native kinds whose left series PowerPoint holds negative: a
#: butterfly's two sides, one clustered bar chart, the sign kept out of every
#: label and tick by :func:`mirrored_number_format`.
MIRRORED_CHART_KINDS: frozenset[str] = frozenset({"butterfly"})
#: Native kinds drawn through ANOTHER PowerPoint chart type, said so in the
#: export log: a waterfall is a stacked column chart whose steps stand on an
#: invisible base series (PowerPoint's own waterfall is a `chartex` part
#: python-pptx cannot write, and that Keynote, Google Slides and LibreOffice
#: show as a blank box).
EMULATED_CHART_KINDS: frozenset[str] = frozenset({"waterfall"})
#: Chart kinds exported as a 2x picture of the viewer's figure, said so in the export log.
#: A map too: PowerPoint's filled map is a `chartex` part drawn from
#: Bing's geocoding when the file opens, which python-pptx cannot write and
#: no other app shows.
PICTURE_CHART_KINDS: frozenset[str] = frozenset({"gauge", "map"})
#: Every kind the spec accepts (pinned against ``presentation_spec.ChartKind`` by a test).
CHART_KINDS: frozenset[str] = NATIVE_CHART_KINDS | PICTURE_CHART_KINDS
#: How a series drawn on the right-hand value axis keeps a native kind a
#: picture: PowerPoint centres the bars of both value axes on their
#: category, one over the other, where the viewer sets them side by side.
TWO_AXIS_PICTURE_SERIES_KINDS: frozenset[str] = frozenset({"bar"})
#: The ``chart_reason`` the extractor tags such a picture with.
TWO_AXIS_BARS_REASON = "bars_on_two_axes"

#: Axis ticks of a ``number`` chart compact from this magnitude on (``AXIS_COMPACT_FROM``).
AXIS_COMPACT_FROM = 10_000
#: The most decimals a chart writes on its own (``MAX_AUTO_DECIMALS``): past
#: them two values differ by float noise, not by a figure.
MAX_AUTO_DECIMALS = 6

#: The text of a chart at each ``label_size``, in canvas px: the size
#: of its ticks, then the sizes its values may be set in, largest first, down
#: to the smallest one they are written upright in before they are turned on
#: their side. Mirrors ``CHART_TEXT_SCALES`` in ``chart-layout.ts`` (pinned by
#: a test). The writer sizes the chart from what the page MEASURED - the tick
#: font, the size each run of values was drawn in - and falls back on this
#: where the page published nothing.
CHART_TEXT_SCALES: dict[str, tuple[int, tuple[int, ...]]] = {
    "sm": (18, (18, 16, 14, 12, 10)),
    "md": (22, (22, 20, 18, 16, 14, 12)),
    "lg": (26, (26, 24, 22, 20, 18, 16, 14)),
}
_DEFAULT_LABEL_SIZE = "md"
#: The canvas px a ``value_label_size`` number may name: the smallest
#: size a text scale sets a value upright in, up to well past ``lg``'s ticks.
#: Mirrors ``VALUE_LABEL_SIZE_MIN`` / ``_MAX`` in ``chart-format.ts`` (pinned
#: by a test); ``presentation_spec.ValueLabelSize`` bounds the field with them.
VALUE_LABEL_SIZE_MIN_PX = 10
VALUE_LABEL_SIZE_MAX_PX = 32

# ISO codes written as their sign (``CURRENCY_SIGNS`` in chart-format.ts).
_CURRENCY_SIGNS: dict[str, str] = {
    "EUR": "\u20ac",
    "USD": "$",
    "GBP": "\u00a3",
    "JPY": "\u00a5",
    "CNY": "\u00a5",
    "INR": "\u20b9",
    "KRW": "\u20a9",
}
_DEFAULT_CURRENCY = "EUR"

# Compact tiers: the threshold, the thousands-scaling commas of a format code, the letter.
_COMPACT_TIERS: tuple[tuple[float, str, str], ...] = (
    (1e9, ",,,", "B"),
    (1e6, ",,", "M"),
    (1e3, ",", "k"),
)

_PICTURE_REASONS: dict[str, str] = {
    "gauge": "PowerPoint has no gauge chart",
    "map": "PowerPoint has no map chart that opens outside Office; its title, caption and legend are text",
    TWO_AXIS_BARS_REASON: "PowerPoint draws the bars of two value axes over each other",
}


def is_native_chart(kind: str) -> bool:
    """Whether a chart of ``kind`` is exported as an editable PowerPoint chart."""
    return kind in NATIVE_CHART_KINDS


def picture_note(slide_number: int, kind: str, reason: str | None = None) -> str:
    """The export-log line for a chart that stayed a picture (1-based slide
    number): ``reason`` is the extractor's ``chart_reason``, the kind itself
    when PowerPoint has no chart of that kind."""
    why = _PICTURE_REASONS.get(reason or kind, "PowerPoint has no native chart of that kind")
    return f"Slide {slide_number}: the {kind} chart is a picture of the rendered figure - {why}."


def series_axis(entry: Mapping[str, object]) -> str:
    """The value axis a payload series reads against: ``secondary`` or ``primary``."""
    return "secondary" if entry.get("axis") == "secondary" else "primary"


def drawn_kind(chart: Mapping[str, object], entry: Mapping[str, object]) -> str:
    """How a payload series is drawn - a combo series' own word, else the
    chart's kind (``drawnKind`` in ``ChartCanvas.tsx``)."""
    kind = chart.get("kind")
    if kind == "combo":
        return "line" if entry.get("kind") == "line" else "bar"
    if kind in ("line", "area"):
        return str(kind)
    return "bar"


def has_secondary_axis(chart: Mapping[str, object]) -> bool:
    """Whether a chart payload reads a series against the right-hand axis."""
    series = chart.get("series")
    return isinstance(series, list) and any(
        isinstance(entry, Mapping) and series_axis(entry) == "secondary" for entry in series
    )


def axis_view(chart: Mapping[str, object], axis: str = "primary") -> dict[str, object]:
    """The chart payload as ONE of its value axes reads it (``axisView`` in
    ``chart-format.ts``): the series against that axis, its target, and how it
    writes a number - the chart's ``format`` / ``unit`` / ``decimals`` /
    ``unit_scale`` for the left-hand axis, the ``secondary_`` ones for the
    right-hand one. A chart with one axis is its own view, so every format
    rule written for one axis (the compact labels of ``label_number_format``
    included) reads either axis of a two-unit chart unchanged."""
    if not has_secondary_axis(chart):
        return dict(chart)
    series = [entry for entry in chart.get("series") or [] if isinstance(entry, Mapping)]
    own = [entry for entry in series if series_axis(entry) == axis]
    if axis == "secondary" and chart.get("secondary_format"):
        reading = {
            "format": chart.get("secondary_format"),
            "unit": chart.get("secondary_unit"),
            "decimals": chart.get("secondary_decimals"),
            "unit_scale": chart.get("secondary_unit_scale"),
        }
    else:
        reading = {
            "format": chart.get("format"),
            "unit": chart.get("unit"),
            "decimals": chart.get("decimals"),
            "unit_scale": chart.get("unit_scale"),
        }
    target = chart.get("secondary_target") if axis == "secondary" else chart.get("target")
    return {**chart, **reading, "series": own, "target": target}


def chart_tick_px(chart: Mapping[str, object]) -> int:
    """The canvas-px size a chart payload's ticks are set in, from its ``label_size``."""
    size = chart.get("label_size")
    scale = CHART_TEXT_SCALES.get(size) if isinstance(size, str) else None
    return (scale or CHART_TEXT_SCALES[_DEFAULT_LABEL_SIZE])[0]


def chart_value_px(chart: Mapping[str, object]) -> int | None:
    """The canvas-px size a chart payload's values are tried at first, from its
    ``value_label_size``: a text scale's first value size, or the
    number of px it names. ``None`` when it names none - the values then read
    at the ticks' size, as they always did."""
    size = chart.get("value_label_size")
    if isinstance(size, str) and size in CHART_TEXT_SCALES:
        return CHART_TEXT_SCALES[size][1][0]
    if isinstance(size, int) and not isinstance(size, bool):
        return size if VALUE_LABEL_SIZE_MIN_PX <= size <= VALUE_LABEL_SIZE_MAX_PX else None
    return None


def emulation_note(slide_number: int, kind: str) -> str:
    """The export-log line for a chart drawn through another chart type (1-based slide number)."""
    return (
        f"Slide {slide_number}: the {kind} chart is an editable stacked bar chart, each step standing on an "
        f"invisible base series - PowerPoint's own {kind} chart cannot be written from here."
    )


def spine_note(slide_number: int, kind: str) -> str:
    """The export-log line for a butterfly whose categories the viewer wrote on
    its central spine (1-based slide number): PowerPoint's bar chart writes
    them on its left axis instead."""
    return (
        f"Slide {slide_number}: the {kind} chart's categories are on its left axis - PowerPoint's bar chart has no "
        "column between its two sides to write them in."
    )


def currency_sign(unit: str | None) -> str:
    """The sign a currency ``unit`` is written with: a symbol as is, a known code as its symbol."""
    code = (unit or _DEFAULT_CURRENCY).strip() or _DEFAULT_CURRENCY
    return _CURRENCY_SIGNS.get(code.upper(), code)


def _is_symbolic(sign: str) -> bool:
    return not any(ch.isalnum() for ch in sign)


def with_currency(amount: str, unit: str | None) -> str:
    """A written amount with the currency of ``unit``: a symbol glued to it
    (``€16.5M``), a code with a space (``CHF 16.5M``)."""
    sign = currency_sign(unit)
    return f"{sign}{amount}" if _is_symbolic(sign) else f"{sign} {amount}"


def with_unit(amount: str, unit: str | None) -> str:
    """A written number with the unit of a ``number`` chart: one letter glued
    to it (``48h``), a word with a space (``1,204 users``)."""
    text = (unit or "").strip()
    if not text:
        return amount
    return f"{amount}{text}" if len(text) == 1 else f"{amount} {text}"


def compact_tier(magnitude: float) -> tuple[float, str]:
    """The divisor and letter a compact amount of ``magnitude`` is written in
    (``(1e6, "M")`` for 16.5M), ``(1.0, "")`` below a thousand."""
    for limit, _commas, letter in _COMPACT_TIERS:
        if abs(magnitude) >= limit:
            return limit, letter
    return 1.0, ""


def pinned_tier(chart: Mapping[str, object]) -> tuple[float, str, str] | None:
    """The one compact tier - threshold, scaling commas, letter - every amount
    of a chart (or one axis view of it) is written in, when it pins one
    (``unit_scale``; ``pinnedTier`` in chart-format.ts): a currency on
    an axis chart only, ``None`` for ``auto``, a pie, a donut, a gauge, a
    number or a percent."""
    if chart.get("format") != "currency" or chart.get("kind") in _SLICE_KINDS:
        return None
    scale = chart.get("unit_scale")
    return next((tier for tier in _COMPACT_TIERS if tier[2] == scale), None)


def _literal(text: str) -> str:
    """A literal run of a format code: quoted, with no quote of its own inside."""
    return '"' + text.replace('"', "") + '"'


def _currency_prefix(unit: str | None) -> str:
    sign = currency_sign(unit)
    return _literal(sign) if _is_symbolic(sign) else _literal(sign + " ")


def _rounded(amount: float, digits: int) -> str:
    """``|amount|`` at ``digits`` decimals, half away from zero on its shortest
    round-trip decimal, trailing zeros dropped - what ``Intl.NumberFormat``
    writes: ``1.005`` reads ``1.01``, not the ``1.00`` its exact binary
    value ``1.00499999...`` would round to."""
    exact = Decimal(repr(abs(amount))).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
    text = f"{exact:f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _scale(value: float, compact: bool, pinned: tuple[float, str, str] | None) -> tuple[float, str]:
    """The divisor and letter ``value`` is written with: the pinned tier's, its own tier's when compact, none else."""
    if pinned is not None:
        return pinned[0], pinned[2]
    return compact_tier(value) if compact else (1.0, "")


def _written(value: float, compact: bool, digits: int, pinned: tuple[float, str, str] | None = None) -> str:
    """How ``value`` reads at ``digits`` decimals, compact or not: its sign,
    amount and tier letter (``-1.57``, ``19.14M``) - what tells two labels apart."""
    divisor, letter = _scale(value, compact, pinned)
    return ("-" if value < 0 else "") + _rounded(value / divisor, digits) + letter


def _own_decimals(amount: float) -> int:
    """The decimals that give ``amount`` two significant digits under 10, one from 10 on (``ownDecimals``)."""
    magnitude = abs(amount)
    if not magnitude > 0 or magnitude >= 10 or not math.isfinite(magnitude):
        return 1
    return min(MAX_AUTO_DECIMALS, 1 - math.floor(math.log10(magnitude)))


def run_decimals(
    values: Iterable[float], *, compact: bool, pinned: tuple[float, str, str] | None = None
) -> int:
    """The decimals a run of values is written with when its chart sets none -
    the values one axis writes together, its ticks or its labels
    (``chartValueWriter`` in chart-format.ts): those its smallest amount
    under 10 needs for two significant digits, one otherwise, and one more
    while two different values would read the same (``1.57x`` and ``1.56x``,
    never ``1.5x`` twice), up to :data:`MAX_AUTO_DECIMALS`. The amounts are
    read at the ``pinned`` tier when the chart pins one (:func:`pinned_tier`)."""
    run = [value for value in values if math.isfinite(value)]
    exact = [_written(value, compact, MAX_AUTO_DECIMALS, pinned) for value in run]

    def collides(digits: int) -> bool:
        seen: dict[str, str] = {}
        for value, precise in zip(run, exact, strict=True):
            if seen.setdefault(_written(value, compact, digits, pinned), precise) != precise:
                return True
        return False

    amounts = [value / _scale(value, compact, pinned)[0] for value in run]
    digits = max([1, *(_own_decimals(amount) for amount in amounts)])
    while digits < MAX_AUTO_DECIMALS and collides(digits):
        digits += 1
    return digits


def _fixed_decimals(chart: Mapping[str, object]) -> int | None:
    """The decimals a chart (an axis view of it) writes every value with, ``None`` when it sets none."""
    value = chart.get("decimals")
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_AUTO_DECIMALS:
        return None
    return value


def _precision(chart: Mapping[str, object], run: Iterable[float], *, compact: bool) -> tuple[int, bool]:
    """How many decimals a run of the chart is written with, and whether every value shows them all."""
    fixed = _fixed_decimals(chart)
    if fixed is not None:
        return fixed, True
    return run_decimals(run, compact=compact, pinned=pinned_tier(chart)), False


def _digits(values: Iterable[float], divisor: float, precision: tuple[int, bool]) -> str:
    """The digits of one band: the chart's decimals, or as many as its most precise value shows."""
    digits, fixed = precision
    if not fixed:
        digits = max((len(_rounded(v / divisor, digits).partition(".")[2]) for v in values), default=0)
    return "#,##0." + "0" * digits if digits else "#,##0"


def compact_format(
    values: Sequence[float],
    *,
    prefix: str = "",
    suffix: str = "",
    precision: tuple[int, bool] | None = None,
    pinned: tuple[float, str, str] | None = None,
) -> str:
    """A format code writing ``values`` compact: the two tiers the data reaches as
    conditional sections, the tier below as the plain section (Excel allows two
    conditions), each band with the decimals of ``precision`` (by default the
    run of ``values`` itself, :func:`run_decimals`).

    ``[>=1000000]"€"#,##0.0,,"M";[>=1000]"€"#,##0.0,"k";"€"#,##0`` reads
    ``16500000`` as ``€16.5M``, ``527100`` as ``€527.1k`` and ``850`` as ``€850``.

    A ``pinned`` tier (:func:`pinned_tier`) is the one section every value is
    written in: ``"€"#,##0.00,,"M"`` reads ``-250000`` as ``-€0.25M``.
    """
    if precision is None:
        precision = (run_decimals(values, compact=True, pinned=pinned), False)
    if pinned is not None:
        limit, commas, letter = pinned
        return f"{prefix}{_digits(values, limit, precision)}{commas}{_literal(letter)}{suffix}"
    magnitude = max((abs(v) for v in values), default=0.0)
    reached = [tier for tier in _COMPACT_TIERS if magnitude >= tier[0]]
    if not reached:
        return f"{prefix}{_digits(values, 1, precision)}{suffix}"
    sections: list[str] = []
    upper = math.inf
    for limit, commas, letter in reached[:2]:
        band = [v for v in values if limit <= abs(v) < upper]
        sections.append(f"[>={int(limit)}]{prefix}{_digits(band, limit, precision)}{commas}{_literal(letter)}{suffix}")
        upper = limit
    rest = [v for v in values if abs(v) < upper]
    if len(reached) > 2:
        limit, commas, letter = reached[2]
        sections.append(f"{prefix}{_digits(rest, limit, precision)}{commas}{_literal(letter)}{suffix}")
    else:
        sections.append(f"{prefix}{_digits(rest, 1, precision)}{suffix}")
    return ";".join(sections)


def _unit_suffix(unit: str | None) -> str:
    text = (unit or "").strip()
    if not text:
        return ""
    return _literal(text) if len(text) == 1 else _literal(" " + text)


def label_number_format(chart: Mapping[str, object], values: Sequence[float]) -> str:
    """The format code of the value labels of a chart (or of one axis view of
    it), for the ``values`` of the plot they label.

    A ``number`` axis chart whose axis reaches ``AXIS_COMPACT_FROM`` writes
    its labels compact, unit kept (``labelSurface`` in chart-format.ts); a
    pie or a donut keeps every digit. The decimals are the chart's, else the
    run of every value the view writes (:func:`view_values`), so two plots
    reading one axis write the same ones.
    """
    fmt = chart.get("format") or "number"
    unit = chart.get("unit")
    unit_text = unit if isinstance(unit, str) else None
    compact = fmt == "currency" or (
        fmt == "number" and chart.get("kind") not in _SLICE_KINDS and value_extent(chart) >= AXIS_COMPACT_FROM
    )
    precision = _precision(chart, view_values(chart), compact=compact)
    if fmt == "percent":
        return f'{_digits(values, 1, precision)}"%"'
    if fmt == "currency":
        return compact_format(
            values, prefix=_currency_prefix(unit_text), precision=precision, pinned=pinned_tier(chart)
        )
    if compact:
        return compact_format(values, suffix=_unit_suffix(unit_text), precision=precision)
    return f"{_digits(values, 1, precision)}{_unit_suffix(unit_text)}"


def view_values(chart: Mapping[str, object]) -> list[float]:
    """Every value a chart payload (or one axis view of it) writes on its
    figure: its series' values, a stack's totals each way, its target - the
    run its labels take their decimals from (``writtenValues`` in
    chart-format.ts; a waterfall's and a donut's labels ride as the text the
    viewer wrote)."""
    series = chart.get("series")
    entries = [entry for entry in series if isinstance(entry, Mapping)] if isinstance(series, list) else []
    rows = [series_values({"series": [entry]}) for entry in entries]
    values = [value for row in rows for value in row]
    if chart.get("stacked"):
        for index in range(max((len(row) for row in rows), default=0)):
            column = [row[index] for row in rows if index < len(row)]
            values += [sum(v for v in column if v > 0), sum(v for v in column if v < 0)]
    target = chart.get("target")
    if isinstance(target, (int, float)) and not isinstance(target, bool):
        values.append(float(target))
    return values


#: The kinds that plot shares of a whole rather than values against an axis.
_SLICE_KINDS: frozenset[str] = frozenset({"pie", "donut", "gauge"})


def value_extent(chart: Mapping[str, object]) -> float:
    """The largest magnitude the value axis of a chart payload reaches, above or below zero.

    Mirrors ``axisExtent`` in chart-format.ts: a stacked chart reaches its
    tallest stack in either direction, any other its furthest value, and a
    target is part of the ceiling.
    """
    series = [
        [
            float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0
            for v in entry.get("values") or []
        ]
        for entry in chart.get("series") or []
        if isinstance(entry, Mapping)
    ]
    stacked = bool(chart.get("stacked"))
    ceiling = 0.0
    floor = 0.0
    for index in range(max((len(values) for values in series), default=0)):
        column = [values[index] if index < len(values) else 0.0 for values in series]
        if stacked:
            ceiling = max(ceiling, sum(v for v in column if v > 0))
            floor = min(floor, sum(v for v in column if v < 0))
        else:
            ceiling = max(ceiling, *column, 0.0)
            floor = min(floor, *column, 0.0)
    target = chart.get("target")
    if isinstance(target, (int, float)) and not isinstance(target, bool):
        ceiling = max(ceiling, float(target))
    return max(abs(ceiling), abs(floor))


def axis_number_format(chart: Mapping[str, object], ticks: Sequence[float], *, with_unit: bool = False) -> str:
    """The format code of a value axis, from the ticks it writes: an axis
    drops the unit, and a ``number`` axis reaching ``AXIS_COMPACT_FROM``
    writes every tick compact (``13k / 6.5k / 0``). One of the two axes of a
    two-unit chart keeps its unit (``with_unit``), as the viewer writes it.
    The decimals are the chart's, else the run of the ticks (``axisTick``)."""
    fmt = chart.get("format") or "number"
    unit = chart.get("unit")
    unit_text = unit if isinstance(unit, str) else None
    compact = fmt == "currency" or (fmt == "number" and max((abs(t) for t in ticks), default=0.0) >= AXIS_COMPACT_FROM)
    precision = _precision(chart, ticks, compact=compact)
    if fmt == "percent":
        return f'{_digits(ticks, 1, precision)}"%"'
    if fmt == "currency":
        return compact_format(
            ticks, prefix=_currency_prefix(unit_text), precision=precision, pinned=pinned_tier(chart)
        )
    suffix = _unit_suffix(unit_text) if with_unit else ""
    if compact:
        return compact_format(ticks, suffix=suffix, precision=precision)
    return f"{_digits(ticks, 1, precision)}{suffix}"


def mirrored_number_format(chart: Mapping[str, object], values: Sequence[float], *, axis: bool = False) -> str:
    """The format code of a butterfly's labels, or of its value axis
    (``axis``), for the ``values`` they write: the code a bar chart
    writes the same sizes with, as both its positive and its negative section,
    so the left side - stored negative - reads as the size the viewer writes:
    ``#,##0;#,##0`` reads ``-80`` as ``80``. A label's code ends on an empty
    zero section (``#,##0;#,##0;``): a zero is a rung that side does not
    have, which the viewer writes no value for, while the axis keeps its 0.

    Excel gives a code of conditional bands (a compact amount reaching two
    tiers, ``[>=1000000]...;[>=1000]...``) no negative section, so such a code
    is written at the one tier of the largest size instead (``"€"#,##0.0,,"M"``),
    the tier a ``unit_scale`` would pin."""
    sizes = [abs(value) for value in values]
    code = axis_number_format(chart, sizes) if axis else label_number_format(chart, sizes)
    if ";" in code:
        code = _one_tier_format(chart, sizes, axis=axis)
    return f"{code};{code}" if axis else f"{code};{code};"


def _one_tier_format(chart: Mapping[str, object], sizes: Sequence[float], *, axis: bool) -> str:
    """A compact format code writing every one of ``sizes`` at the tier of the
    largest, with the decimals that tier needs (the chart's when it sets them)."""
    fmt = chart.get("format") or "number"
    unit = chart.get("unit")
    unit_text = unit if isinstance(unit, str) else None
    magnitude = max(sizes, default=0.0)
    tier = next((tier for tier in _COMPACT_TIERS if magnitude >= tier[0]), _COMPACT_TIERS[-1])
    run = sizes if axis else [abs(value) for value in view_values(chart)]
    fixed = _fixed_decimals(chart)
    precision = (fixed, True) if fixed is not None else (run_decimals(run, compact=True, pinned=tier), False)
    prefix = _currency_prefix(unit_text) if fmt == "currency" else ""
    suffix = "" if fmt == "currency" or axis else _unit_suffix(unit_text)
    return compact_format(sizes, prefix=prefix, suffix=suffix, precision=precision, pinned=tier)


def series_values(chart: Mapping[str, object], indices: Iterable[int] | None = None) -> list[float]:
    """Every plotted value of a chart payload, as floats (a malformed entry
    reads as 0) - of the series at ``indices`` only when given, a position
    among the payload's series objects (the writer's plotted series)."""
    out: list[float] = []
    series = chart.get("series")
    if not isinstance(series, list):
        return out
    wanted = None if indices is None else set(indices)
    entries = [entry for entry in series if isinstance(entry, Mapping)]
    for position, entry in enumerate(entries):
        if wanted is not None and position not in wanted:
            continue
        values = entry.get("values")
        if not isinstance(values, list):
            continue
        for value in values:
            out.append(float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0)
    return out
