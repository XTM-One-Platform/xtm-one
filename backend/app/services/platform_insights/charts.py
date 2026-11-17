# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""``render_chart``: tidy rows into the chat's chart block.

The rows are the ``{bucket, dimension, value}`` rows the analytics, objects and
adoption tools return. Buckets become the categories and dimensions the series
when both are there; one of them alone makes the categories of a single series.
The block is then validated by the chat chart contract
(``chat_charts.validate_chart``, the presentation ``chart`` block), so a chart
the tool accepts is one every surface draws.

Nothing is invented: a null value (a masked cost) refuses the chart, a missing
cell is a zero only when the caller says the values are counts or sums
(``missing_values``), and folding the smallest categories into "Other" is the
caller's explicit choice, for sums and counts only.
"""

from __future__ import annotations

from typing import Any

from app.services.chat_charts import CHAT_CHART_KINDS, ChartBlockError, validate_chart
from app.services.platform_insights.scope import InsightRefusedError

#: The presentation chart's bounds (``presentation_spec``), said in refusals.
MAX_CATEGORIES = 16
MAX_SERIES = 6
CATEGORY_CHARS = 24
SERIES_CHARS = 40
MAX_INPUT_ROWS = 2000


def _label(value: Any, limit: int, taken: set[str]) -> str:
    text = str(value).strip() or "-"
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    base, index = text, 2
    while text in taken:
        suffix = f" {index}"
        text = base[: limit - len(suffix)] + suffix
        index += 1
    taken.add(text)
    return text


def build_chart(args: dict[str, Any]) -> dict[str, Any]:
    """The validated chart block for *args*, or :class:`InsightRefusedError` saying why not."""
    rows = args.get("rows")
    if not isinstance(rows, list) or not rows:
        raise InsightRefusedError("Error: rows must be the non-empty 'rows' list a query tool returned.")
    if len(rows) > MAX_INPUT_ROWS:
        raise InsightRefusedError(f"Error: at most {MAX_INPUT_ROWS} rows; group the query more coarsely.")
    kind = str(args.get("kind") or "bar").strip().lower()
    if kind not in CHAT_CHART_KINDS:
        raise InsightRefusedError(f"Error: kind must be one of {', '.join(CHAT_CHART_KINDS)}.")
    missing = str(args.get("missing_values") or "zero").strip().lower()
    if missing not in {"zero", "error"}:
        raise InsightRefusedError("Error: missing_values must be zero or error.")

    cells: dict[tuple[Any, Any], float] = {}
    buckets: list[Any] = []
    dimensions: list[Any] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or "value" not in row:
            raise InsightRefusedError(f"Error: rows[{index}] has no value; pass the rows as the query returned them.")
        value = row.get("value")
        if value is None:
            raise InsightRefusedError(
                f"Error: rows[{index}] has no value (a masked cost, or a figure the platform does not have). "
                "A chart would show it as zero: say it in words instead."
            )
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise InsightRefusedError(f"Error: rows[{index}].value is not a number.") from None
        bucket, dimension = row.get("bucket"), row.get("dimension")
        if bucket not in buckets:
            buckets.append(bucket)
        if dimension not in dimensions:
            dimensions.append(dimension)
        cells[(bucket, dimension)] = cells.get((bucket, dimension), 0.0) + number

    by_bucket = any(bucket is not None for bucket in buckets)
    by_dimension = any(dimension is not None for dimension in dimensions)
    if not by_bucket and not by_dimension:
        raise InsightRefusedError("Error: one total is not a chart; state the figure in words.")
    series_name = str(args.get("series_name") or "Value")
    if by_bucket and by_dimension:
        categories, series_keys = buckets, dimensions
        lookup = lambda category, key: cells.get((category, key))  # noqa: E731
    elif by_bucket:
        categories, series_keys = buckets, [None]
        lookup = lambda category, _key: cells.get((category, None))  # noqa: E731
    else:
        categories, series_keys = dimensions, [None]
        lookup = lambda category, _key: cells.get((None, category))  # noqa: E731

    if args.get("fold_others") and len(series_keys) == 1 and len(categories) > MAX_CATEGORIES:
        ranked = sorted(categories, key=lambda category: -(lookup(category, None) or 0.0))
        kept, folded = ranked[: MAX_CATEGORIES - 1], ranked[MAX_CATEGORIES - 1 :]
        other = sum(lookup(category, None) or 0.0 for category in folded)
        kept_set = set(kept)
        categories = [category for category in categories if category in kept_set]
        values_by_category = {category: lookup(category, None) for category in categories}
        other_label = f"Other ({len(folded)})"
        values_by_category[other_label] = other
        categories = [*categories, other_label]
        lookup = lambda category, _key: values_by_category.get(category)  # noqa: E731
    if len(categories) > MAX_CATEGORIES:
        raise InsightRefusedError(
            f"Error: {len(categories)} categories; a chart holds at most {MAX_CATEGORIES}. Query with a coarser "
            "bucket (week or month), filter, or set fold_others for a count or a sum."
        )
    if len(series_keys) > MAX_SERIES:
        raise InsightRefusedError(
            f"Error: {len(series_keys)} series; a chart holds at most {MAX_SERIES}. Filter the query, or drop split_by."
        )

    taken: set[str] = set()
    category_labels = [_label(category, CATEGORY_CHARS, taken) for category in categories]
    series = []
    series_taken: set[str] = set()
    for key in series_keys:
        values = []
        for category in categories:
            value = lookup(category, key)
            if value is None:
                if missing == "error":
                    raise InsightRefusedError(
                        f"Error: no value for {key or series_name!r} at {category!r}. Use missing_values zero only "
                        "for counts and sums, where no row means none."
                    )
                value = 0.0
            values.append(value)
        series.append({"name": _label(series_name if key is None else key, SERIES_CHARS, series_taken), "values": values})

    raw: dict[str, Any] = {"kind": kind, "categories": category_labels, "series": series}
    for field in ("title", "caption", "unit"):
        if args.get(field):
            raw[field] = str(args[field])
    if args.get("format"):
        raw["format"] = str(args["format"]).strip().lower()
    if args.get("decimals") is not None:
        raw["decimals"] = args["decimals"]
    try:
        return validate_chart(raw)
    except ChartBlockError as exc:
        raise InsightRefusedError(f"Error: the chart was refused: {exc}") from None
