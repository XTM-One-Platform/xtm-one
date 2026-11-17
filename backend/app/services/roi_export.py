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

"""The CSV and PDF exports of the ROI ledger's figures (issue #12077, slice L4c, design 12.8).

``GET /roi/export`` renders the answer of ``roi_ledger.figures`` (``Figures.as_dict``),
the one shape ``GET /roi`` answers under ``figures``: the export of a scope prints the
numbers the page reads for the same viewer and window, never a second computation
(invariant d). Every scope exports, ``mine`` included.

The rendering here is pure (no database): :func:`labels` reads the names of the
agents, fleets and units the figures name, and the assumptions behind them, once,
before the rendering. A masked cost reads :data:`HIDDEN_COST`, never 0. Every
breakdown exported sums to the hero, its residual and folded rows included, with the
names of design 12.4 ("No agent (general chat)", "No fleet", "No unit", "Other
agents and fleets").
"""

from __future__ import annotations

import csv
import io
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import roi, roi_figures, roi_report

#: What a masked cost reads in an export: never 0, never a blank that reads as nothing spent.
HIDDEN_COST = roi_report.HIDDEN_COST
METHODOLOGY = roi_report.METHODOLOGY

#: The names of the source rows and of the residual and folded rows (design 12.4 and 12.6).
SOURCE_NAMES: dict[str, str] = {
    roi_figures.CLASS_AUTOMATION: "Automations",
    roi_figures.CLASS_CHAT: "Chat (estimated)",
    roi_figures.CLASS_PLATFORM: "Other AI usage",
}
RESIDUAL_NAMES: dict[str, str] = {
    roi_figures.ROW_NO_AGENT: "No agent (general chat)",
    roi_figures.ROW_NO_FLEET: "No fleet",
    roi_figures.ROW_NO_UNIT: "No unit",
    roi_figures.ROW_NO_PERSON: "No person",
    roi_figures.ROW_OTHER: "Other agents and fleets",
}

#: The breakdowns exported, in their order, with their heading.
BREAKDOWNS: tuple[tuple[str, str], ...] = (
    ("by_source", "By source"),
    ("by_agent", "By agent"),
    ("by_fleet", "By fleet"),
    ("by_unit", "By unit of work"),
    ("by_week", "By week"),
    ("by_person", "By person"),
)

CSV_COLUMNS = (
    "section",
    "key",
    "name",
    "runs",
    "units",
    "chat_deliverables",
    "hours",
    "hours_low",
    "hours_high",
    "value",
    "value_low",
    "value_high",
    "ai_cost",
    "net_value",
    "roi",
    "payback_months",
    "currency",
)
#: The figures a masked cost hides (``None`` in ``Figures.as_dict``).
_COST_COLUMNS = frozenset({"ai_cost", "net_value", "roi", "payback_months"})
ASSUMPTION_COLUMNS = ("assumption", "kind", "name", "value", "label", "source")

#: The rows a breakdown shows in the PDF; the CSV carries every row.
PDF_TOP = 8


def export_filename(
    window: str,
    fmt: str,
    *,
    xtm_one: bool,
    scope: str = roi_figures.SCOPE_ORGANIZATION,
    scope_id: uuid.UUID | None = None,
) -> str:
    """The download name: the product's name in XTM One mode only, then the scope (none for the organization)."""
    prefix = "xtm-one-value" if xtm_one else "agents-value"
    scope = roi_figures.normalize_scope(scope)
    if scope == roi_figures.SCOPE_MINE:
        prefix += "-mine"
    elif scope_id is not None:
        prefix += f"-{scope}-{str(scope_id)[:8]}"
    return f"{prefix}-{window}.{fmt}"


def row_name(section: str, key: str, names: Mapping[str, str]) -> str:
    """The name of a breakdown row: its source, its residual or folded row, its object, its week."""
    if section == "by_source":
        return SOURCE_NAMES.get(key, key)
    if key in RESIDUAL_NAMES:
        return RESIDUAL_NAMES[key]
    if section == "by_week":
        return f"Week of {key}"
    return names.get(key, key)


def _cell(value: Any) -> Any:
    """A CSV cell a spreadsheet never reads as a formula."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return "" if value is None else value


def _csv_line(
    section: str, key: str, name: str, totals: Mapping[str, Any], *, currency: str, masked: bool
) -> list[Any]:
    cells: list[Any] = [section, key, name]
    for column in CSV_COLUMNS[3:-1]:
        source = "deliverables" if column == "chat_deliverables" else column
        cells.append(HIDDEN_COST if masked and column in _COST_COLUMNS else totals.get(source))
    cells.append(currency)
    return [_cell(c) for c in cells]


def figures_csv(
    figures: Mapping[str, Any],
    *,
    names: Mapping[str, str],
    assumptions: Sequence[tuple[str, str, str, str, str]],
) -> str:
    """The figures as CSV: the hero (``total``), then one line per row of every breakdown.

    Each section sums to the ``total`` line. After the figures, a blank line, the ``since``
    line when the window starts before every fact names its person, the ``methodology`` line,
    a blank line and the ``assumptions`` section.
    """
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    masked = bool(figures.get("cost_masked"))
    currency = str(figures.get("currency") or "")
    scope_key = str(figures.get("id") or figures.get("scope") or "")
    scope_name = names.get(scope_key, str(figures.get("scope") or ""))
    writer.writerow(_csv_line("total", scope_key, scope_name, figures["totals"], currency=currency, masked=masked))
    for section, _heading in BREAKDOWNS:
        for row in figures.get(section) or ():
            key = str(row["key"])
            writer.writerow(
                _csv_line(section, key, row_name(section, key, names), row, currency=currency, masked=masked)
            )
    writer.writerow([])
    if figures.get("since"):
        writer.writerow(["since", figures["since"]])
    writer.writerow(["methodology", METHODOLOGY])
    writer.writerow([])
    writer.writerow(ASSUMPTION_COLUMNS)
    for row in assumptions:
        writer.writerow([_cell(v) for v in ("assumption", *row)])
    return out.getvalue()


def _md(text: str) -> str:
    """Text safe inside a Markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ").replace("<", "&lt;").replace(">", "&gt;")


def _money(amount: float | None, currency: str, *, masked: bool = False) -> str:
    if masked:
        return HIDDEN_COST
    return "n/a" if amount is None else f"{amount:,.2f} {currency}"


def _ratio(ratio: float | None, *, masked: bool) -> str:
    if masked:
        return HIDDEN_COST
    return "n/a" if ratio is None else f"{ratio:g}x"


def _period(window: str, window_first: str | None) -> str:
    if window_first is None:
        return "Since the start"
    if window == roi_report.WINDOW_QUARTER:
        return f"This quarter (since {window_first})"
    return f"The last {window.removesuffix('d')} days (since {window_first})"


def figures_markdown(
    figures: Mapping[str, Any],
    *,
    names: Mapping[str, str],
    assumptions: Sequence[tuple[str, str, str, str, str]],
    window: str,
    title: str,
    generated_at: datetime | None = None,
) -> str:
    """The value report of the figures as Markdown (the PDF source): the hero, the breakdowns, the method."""
    masked = bool(figures.get("cost_masked"))
    currency = str(figures.get("currency") or "")
    totals = figures["totals"]
    scope = str(figures.get("scope") or "")
    when = (generated_at or datetime.now(UTC)).strftime("%Y-%m-%d")
    lines = [f"# {title}", ""]
    if scope == roi_figures.SCOPE_MINE:
        lines += ["**Your own runs and chats**", ""]
    elif figures.get("id"):
        lines += [f"**{scope.capitalize()}: {_md(names.get(str(figures['id']), str(figures['id'])))}**", ""]
    lines += [f"_{_period(window, figures.get('window_first'))}, generated {when}. Figures estimated._", ""]
    if figures.get("since"):
        lines += [f"_Attributed to a person since {figures['since']}; earlier facts are counted, not attributed._", ""]
    lines += [
        "| Hours saved | Value | AI cost | Net value | ROI |",
        "|---|---|---|---|---|",
        f"| {totals['hours']:g} h | {_money(totals['value'], currency)} "
        f"| {_money(totals['ai_cost'], currency, masked=masked)} "
        f"| {_money(totals['net_value'], currency, masked=masked)} | {_ratio(totals['roi'], masked=masked)} |",
        "",
        f"{totals['runs']} runs produced {totals['units']} units of work; "
        f"{totals['deliverables']} chat deliverables counted.",
        "",
    ]
    for section, heading in BREAKDOWNS:
        if section == "by_week":
            continue  # the CSV carries the weeks; the one-page report the totals
        rows = list(figures.get(section) or ())
        if not rows:
            continue
        lines += [f"## {heading}", "", "| Name | Hours saved | Value | AI cost | Net value |", "|---|---|---|---|---|"]
        lines += [
            f"| {_md(row_name(section, str(r['key']), names))} | {r['hours']:g} | {_money(r['value'], currency)} "
            f"| {_money(r['ai_cost'], currency, masked=masked)} | {_money(r['net_value'], currency, masked=masked)} |"
            for r in rows[:PDF_TOP]
        ]
        if len(rows) > PDF_TOP:
            lines.append(f"\n_{len(rows) - PDF_TOP} more rows in the CSV export._")
        lines.append("")
    lines += [
        '<div style="page-break-before: always"></div>',
        "",
        "## How these figures are computed",
        "",
        "- Value = units x (manual minutes - review minutes) x hourly rate, "
        f"plus the estimated time of chat deliverables: {_money(totals['value'], currency)}",
    ]
    if masked:
        lines += [f"- Net value = value - AI cost: {HIDDEN_COST}", f"- ROI = net value / AI cost: {HIDDEN_COST}"]
    else:
        lines += [
            f"- Net value = value - AI cost = {_money(totals['value'], currency)} - "
            f"{_money(totals['ai_cost'], currency)} = {_money(totals['net_value'], currency)}",
            f"- ROI = net value / AI cost = {_ratio(totals['roi'], masked=False)}",
        ]
    lines.append("")
    if assumptions:
        lines += ["## Assumptions", "", "| Input | Value | Label | Source |", "|---|---|---|---|"]
        lines += [
            f"| {_md(name)} | {_md(value)} | {_md(label)} | {_md(source) or '-'} |"
            for _kind, name, value, label, source in assumptions
        ]
        lines.append("")
    lines += [f"_{METHODOLOGY}_", ""]
    return "\n".join(lines).rstrip() + "\n"


async def figures_pdf(
    figures: Mapping[str, Any],
    *,
    names: Mapping[str, str],
    assumptions: Sequence[tuple[str, str, str, str, str]],
    window: str,
    title: str,
) -> bytes:
    """The value report as PDF, rendered by the platform's headless Chromium."""
    from app.services.pdf_render import render_text_to_pdf

    text = figures_markdown(figures, names=names, assumptions=assumptions, window=window, title=title)
    return await render_text_to_pdf(text, title=title)


def assumption_facts(units: Iterable[str], *, pricing: roi_report.Pricing) -> list[dict[str, Any]]:
    """The inputs behind the units of the figures, in the shape of ``roi.compute_roi``'s ``assumption_facts``."""
    used = sorted(u for u in set(units) if u in pricing.baselines)
    facts: list[dict[str, Any]] = []
    for unit in used:
        b = pricing.baselines[unit]
        facts.append(
            {
                "kind": "unit",
                "unit": unit,
                "name": b.name,
                "manual_min": b.manual_min_likely,
                "manual_min_low": b.manual_min_low,
                "manual_min_high": b.manual_min_high,
                "review_min": b.review_min,
                "label": roi.LABEL_OVERRIDE if unit in pricing.overridden else b.minutes_label,
                "source": b.minutes_source,
            }
        )
    for role in sorted({pricing.baselines[u].rate_role for u in used}):
        rate = (pricing.rates.get(role) or {}).get(pricing.currency)
        if rate is not None:
            facts.append(
                {
                    "kind": "rate",
                    "role": role,
                    "amount": rate.amount,
                    "currency": pricing.currency,
                    "label": rate.label,
                    "source": rate.source,
                }
            )
    return facts


async def labels(
    db: AsyncSession, figures: Mapping[str, Any]
) -> tuple[dict[str, str], list[tuple[str, str, str, str, str]]]:
    """The names of the agents, fleets and units *figures* name, and the assumptions behind its units.

    One read of each table; an id whose object is gone keeps its id as its name.
    """
    from app.models.agent import Agent
    from app.models.fleet import Fleet

    def ids(section: str) -> set[uuid.UUID]:
        found: set[uuid.UUID] = set()
        for row in figures.get(section) or ():
            try:
                found.add(uuid.UUID(str(row["key"])))
            except ValueError:
                continue  # a residual or folded row
        return found

    agent_ids, fleet_ids = ids("by_agent"), ids("by_fleet")
    scope_id = figures.get("id")
    if scope_id and figures.get("scope") == roi_figures.SCOPE_AGENT:
        agent_ids.add(uuid.UUID(str(scope_id)))
    elif scope_id and figures.get("scope") == roi_figures.SCOPE_FLEET:
        fleet_ids.add(uuid.UUID(str(scope_id)))
    names: dict[str, str] = {}
    if agent_ids:
        rows = (await db.execute(select(Agent.id, Agent.name).where(Agent.id.in_(agent_ids)))).all()
        names.update({str(i): str(n) for i, n in rows})
    if fleet_ids:
        rows = (await db.execute(select(Fleet.id, Fleet.name).where(Fleet.id.in_(fleet_ids)))).all()
        names.update({str(i): str(n) for i, n in rows})
    pricing = await roi_report.load_pricing(db, figures.get("currency") or None)
    units = [str(r["key"]) for r in figures.get("by_unit") or () if r["key"] not in RESIDUAL_NAMES]
    for unit in units:
        if unit in pricing.baselines:
            names[unit] = pricing.baselines[unit].name
    return names, roi_report.assumption_rows_of(assumption_facts(units, pricing=pricing))
