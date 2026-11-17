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

"""The chart a chat message carries: one block, one contract, every surface.

A chart in an answer is the native ``chart`` block ``generate_presentation``
already validates (``presentation_spec.ChartBlock``: kind, categories, series,
unit, format, title, caption), limited to the kinds the chat draws
(``CHAT_CHART_KINDS``). The message stores it in its own text, as a fenced block
of JSON in the ``xtm-chart`` language, so every surface that reads a message -
the web chat, the conversation export, a channel, a later turn's history - finds
it where the answer placed it, with no side table:

    ```xtm-chart
    {"type":"chart","kind":"line","categories":[...],"series":[...]}
    ```

The model never writes that block. ``render_chart`` validates the rows into one
(``validate_chart``), keeps it in the turn's chart sink and answers with a
marker, ``[[CHART:<id>]]``; the answer's deterministic post-processing
(``inject_charts``, like ``code_interpreter.inject_code_images``) puts the block
where the marker stands, or after the answer when the model left it out, so the
numbers shown are the validated ones. ``extract_charts`` reads them back
(``MessageResponse.charts``), and ``charts_as_tables`` turns each into a
Markdown table for the PDF and Markdown exports and the channels that draw no
chart.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

#: The fence language of a chart block. Hyphenated on purpose: no Markdown
#: highlighter claims it, so a block that fails validation still reads as code.
CHART_FENCE_LANGUAGE = "xtm-chart"

#: The ``ChartBlock`` kinds the chat draws (the frontend's ``ChatChart``).
CHAT_CHART_KINDS: tuple[str, ...] = ("bar", "stacked_bar", "line", "area", "pie", "donut")

CHART_MARKER_RE = re.compile(r"\[\[CHART:([0-9a-f]{12})\]\]", re.IGNORECASE)
CHART_FENCE_RE = re.compile(r"```" + re.escape(CHART_FENCE_LANGUAGE) + r"[ \t]*\r?\n(.*?)\r?\n```", re.DOTALL)


class ChartBlockError(ValueError):
    """Rows or a block the chat cannot draw, with the reason the tool returns."""


@dataclass(frozen=True)
class ChatChart:
    """One validated chart of a turn, the id its marker names."""

    id: str
    block: dict[str, Any]

    @property
    def marker(self) -> str:
        return f"[[CHART:{self.id}]]"


def _validation_message(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors()[:5]:
        location = ".".join(str(part) for part in error.get("loc", ()) if part != "chart")
        message = str(error.get("msg", "")).removeprefix("Value error, ")
        parts.append(f"{location}: {message}" if location else message)
    return "; ".join(parts)


def validate_chart(raw: dict[str, Any]) -> dict[str, Any]:
    """*raw* as the chart block a message stores, or :class:`ChartBlockError`.

    The presentation contract decides (bounds, one value per category, a pie's
    one series, ...); the stored form drops every field left at its default so
    the block stays the size of its data.
    """
    from app.services.presentation_spec import ChartBlock

    if not isinstance(raw, dict):
        raise ChartBlockError("a chart is an object")
    kind = raw.get("kind")
    if kind not in CHAT_CHART_KINDS:
        raise ChartBlockError(f"kind must be one of {', '.join(CHAT_CHART_KINDS)}, not {kind!r}")
    try:
        block = ChartBlock.model_validate({**raw, "type": "chart"})
    except ValidationError as exc:
        raise ChartBlockError(_validation_message(exc)) from None
    return block.model_dump(mode="json", exclude_defaults=True, exclude_none=True) | {"type": "chart"}


def new_chart(block: dict[str, Any]) -> ChatChart:
    return ChatChart(id=uuid.uuid4().hex[:12], block=block)


def chart_fence(block: dict[str, Any]) -> str:
    """The text a message stores for *block*."""
    payload = json.dumps(block, ensure_ascii=False, separators=(",", ":"))
    return f"```{CHART_FENCE_LANGUAGE}\n{payload}\n```"


def inject_charts(text: str, charts: list[ChatChart]) -> str:
    """*text* with every chart of *charts* placed: at its marker, else after the answer.

    *charts* is drained in place (``code_images`` in ``segment_finalize``), so a
    chart placed in a mid-turn segment is not placed again in the final answer.
    A marker naming no chart of this turn (one already placed, or made up) is
    removed: a marker is never shown as text.
    """
    if not charts and not (text and "[[CHART:" in text.upper()):
        return text
    pending = {chart.id: chart for chart in charts}
    placed: set[str] = set()

    def _place(match: re.Match[str]) -> str:
        chart_id = match.group(1).lower()
        chart = pending.get(chart_id)
        if chart is None or chart_id in placed:
            return ""
        placed.add(chart_id)
        return "\n\n" + chart_fence(chart.block) + "\n\n"

    result = CHART_MARKER_RE.sub(_place, text or "")
    trailing = [chart_fence(chart.block) for chart in charts if chart.id not in placed]
    del charts[:]
    result = re.sub(r"\n{3,}", "\n\n", result).strip()
    if trailing:
        result = "\n\n".join([result, *trailing]).strip()
    return result


def extract_charts(text: str | None) -> list[dict[str, Any]]:
    """The chart blocks *text* carries, in order; a block that no longer validates is skipped."""
    if not text or CHART_FENCE_LANGUAGE not in text:
        return []
    charts: list[dict[str, Any]] = []
    for match in CHART_FENCE_RE.finditer(text):
        try:
            charts.append(validate_chart(json.loads(match.group(1))))
        except (ValueError, TypeError):
            continue
    return charts


def _format_value(value: float, block: dict[str, Any]) -> str:
    from app.services.chart_export import with_currency, with_unit

    fmt = block.get("format", "number")
    decimals = block.get("decimals")
    if decimals is None:
        decimals = 0 if float(value).is_integer() else 2
    amount = f"{value:,.{decimals}f}"
    if fmt == "percent":
        return f"{amount}%"
    if fmt == "currency":
        return with_currency(amount, block.get("unit"))
    return with_unit(amount, block.get("unit"))


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def chart_table(block: dict[str, Any]) -> str:
    """*block* as a Markdown table: one row per category, one column per series."""
    categories = block.get("categories") or []
    series = block.get("series") or []
    lines: list[str] = []
    if block.get("title"):
        lines.append(f"**{_cell(block['title'])}**")
        lines.append("")
    lines.append("| | " + " | ".join(_cell(item.get("name", "")) for item in series) + " |")
    lines.append("|" + "---|" * (len(series) + 1))
    for index, category in enumerate(categories):
        cells = []
        for item in series:
            values = item.get("values") or []
            cells.append(_format_value(values[index], block) if index < len(values) else "")
        lines.append(f"| {_cell(category)} | " + " | ".join(cells) + " |")
    if block.get("caption"):
        lines.append("")
        lines.append(f"_{_cell(block['caption'])}_")
    return "\n".join(lines)


def charts_as_tables(text: str | None) -> str:
    """*text* with every chart block written as its table (exports, channels)."""
    if not text or CHART_FENCE_LANGUAGE not in text:
        return text or ""

    def _table(match: re.Match[str]) -> str:
        try:
            return chart_table(validate_chart(json.loads(match.group(1))))
        except (ValueError, TypeError):
            return match.group(0)

    return CHART_FENCE_RE.sub(_table, text)
