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

"""Built-in report tools — agent-generated executive reports.

Gated behind the ``generate_report`` built-in tool id:

* ``generate_report`` — create a report from a structured ``ReportSpec``, or
  rewrite an existing one in place (revise a section, re-score the verdict,
  append a source) by passing its ``report_id``.
* ``get_report`` — read a report's current spec back, so an agent can revise a
  report it produced in an earlier turn without having the JSON in context.
* ``export_report`` — render the report to a PDF file of the conversation or
  run, through the headless Chromium the decks export with
  (``services/report_export.py``). Exporting a report the same turn (or run)
  already exported, with neither the report nor its template changed since,
  hands back that file.

What the agent may and may not author
------------------------------------
The agent produces **structured JSON, never a page of HTML**: every block picks
one of the controlled types in ``app.services.report_spec``, and each maps to a
fixed React component whose markup and CSS a human wrote. The only markup an
agent can emit is a paragraph-level ``rich_text`` fragment, rejected here if it
carries script-capable constructs and sanitized with DOMPurify again at render
time.

This is not a limitation to work around — it is the feature. A brief that *looks*
like bespoke CSS is structurally a dozen repeated components with data in them,
so the agent fills values and the stylesheet owns the design. Letting a template
carry raw ``<style>`` instead would hand any agent an XSS primitive into the app
shell, because ``template_authoring`` is itself an agent-facing tool.

Persistence is a ``Report`` row rather than a ``FileAttachment``: a report keeps a
stable id across turns so the ``[[REPORT:<id>]]`` marker already sitting in the
conversation stays valid when the report is revised, and the frontend reconciles
the block list instead of mounting a second document.

Like every other tool closure, database work happens on a **dedicated short-lived
session** (never the caller's ``db``): tool calls run mid agentic loop, after the
caller released its pooled connection, and flushing on that session would pin a
connection across every remaining LLM call.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from app.core.utils import format_exception_message, safe_uuid
from app.services.report_spec import (
    MAX_BLOCKS,
    REPORT_BLOCKS,
    REPORT_CONFIDENCE_LEVELS,
    REPORT_RISK_LEVELS,
    REPORT_THEMES,
    ReportSpecError,
)
from app.tools.base import CallableToolExecutor, ToolDefinition, ToolResult

from ._shared import TurnExport, export_result, logger

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

#: Budget for the spec JSON returned by ``get_report``. A 40-block report
#: serializes to a few tens of KB; truncating keeps one oversized report from
#: eating the whole context window, and the agent can still regenerate from the
#: summary above it.
_SPEC_READBACK_BUDGET = 20000


async def _resolve_report_id(db: AsyncSession, value: str, user_uuid: uuid.UUID) -> tuple[uuid.UUID | None, str | None]:
    """*value*, an id prefix (8+ hexadecimal characters), as the id of the one
    report of *user_uuid* it starts (``builtin_tools/id_prefix.py``).

    ``(None, refusal)`` when it starts several of the user's reports, with the
    candidates named; ``(None, None)`` when it starts none, so the tool answers
    its usual "not found". Only the caller's own reports are looked up, the
    rule :func:`app.services.reports.load_owned_report` applies.
    """
    from app.models.report import Report

    from .id_prefix import resolve_id_prefix

    full, refusal = await resolve_id_prefix(
        db, Report, value, [Report.user_id == user_uuid], noun="reports", label=Report.title
    )
    if refusal:
        return None, refusal
    return safe_uuid(str(full).strip()), None


def _summarize(spec: dict) -> str:
    """One line per block: index, type and heading — the agent's edit map.

    Returned after every create/update so the model can see the block order it
    just produced (and address a block by index on the next revision) without
    re-reading the whole spec.
    """
    lines = []
    for idx, block in enumerate(spec.get("blocks") or []):
        if not isinstance(block, dict):
            continue
        title = block.get("title") or block.get("summary") or "(untitled)"
        lines.append(f"  {idx + 1}. [{block.get('block')}] {title}")
    return "\n".join(lines)


def build_report_tools(
    db: AsyncSession,
    user_id: str,
    conversation_id: str | None = None,
    *,
    assignment_run_id: str | None = None,
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """Build the report generation, read-back and export tools for an agent."""
    del db  # every operation opens its own short-lived session
    # The PDFs this turn's (or run's) exports stored, by report, revision and
    # resolved template.
    exported_files: dict[tuple[uuid.UUID, int, str], TurnExport] = {}

    async def _generate_report(
        title: str,
        header: object,
        blocks: object,
        theme: str = "light",
        footer: str | None = None,
        template_id: str | None = None,
        report_id: str | None = None,
        **_kw: object,
    ) -> ToolResult:
        from app.core.database import async_session
        from app.services.reports import load_owned_report, save_report

        # LLMs occasionally hand a JSON *string* to an object- or array-typed
        # parameter. Parsing it here turns a guaranteed failure into a working
        # call; a non-JSON string still lands in the normal validation error.
        for name, value in (("header", header), ("blocks", blocks)):
            if not isinstance(value, str):
                continue
            try:
                parsed = json.loads(value)
            except ValueError:
                return ToolResult(
                    tool_call_id="",
                    content=(
                        f"{name} must be JSON, not a plain string. 'header' is an "
                        "object with a 'title'; 'blocks' is an array of block "
                        f"objects, each with a 'block' key (one of: "
                        f"{', '.join(REPORT_BLOCKS)})."
                    ),
                    is_error=True,
                )
            if name == "header":
                header = parsed
            else:
                blocks = parsed

        user_uuid = safe_uuid(user_id)
        if user_uuid is None:
            # Anonymous external-channel callers have no user row to own the
            # report, and a report is only reachable through the authenticated
            # read endpoint — so there would be nothing to render.
            return ToolResult(
                tool_call_id="",
                content=(
                    "Reports can only be generated for a signed-in user. Produce "
                    "the content as a document via generate_file instead."
                ),
                is_error=True,
            )

        from .id_prefix import id_prefix

        target_id = None
        target_prefix = None
        if report_id:
            target_id = safe_uuid(str(report_id).strip())
            if target_id is None:
                target_prefix = id_prefix(str(report_id))
            if target_id is None and target_prefix is None:
                return ToolResult(
                    tool_call_id="",
                    content=(
                        "report_id must be the UUID of a report you generated "
                        "earlier (omit it to create a new report)."
                    ),
                    is_error=True,
                )

        template_uuid = None
        if template_id:
            template_uuid = safe_uuid(str(template_id).strip())
            if template_uuid is None:
                return ToolResult(
                    tool_call_id="",
                    content=(
                        "template_id must be the UUID of a template container "
                        "(use search_templates to find one, or omit it)."
                    ),
                    is_error=True,
                )

        spec_payload = {
            "title": title,
            "theme": theme or "light",
            "header": header,
            "blocks": blocks,
            "footer": footer,
        }

        try:
            async with async_session() as own_db:
                existing = None
                if target_prefix is not None:
                    target_id, refusal = await _resolve_report_id(own_db, target_prefix, user_uuid)
                    if refusal:
                        return ToolResult(tool_call_id="", content=refusal, is_error=True)
                    if target_id is None:
                        return ToolResult(
                            tool_call_id="",
                            content=(
                                f"No report `{report_id}` of yours was found. Omit report_id to create a new report."
                            ),
                            is_error=True,
                        )
                if target_id is not None:
                    existing = await load_owned_report(
                        own_db, target_id, user_id=user_uuid
                    )
                    if existing is None:
                        return ToolResult(
                            tool_call_id="",
                            content=(
                                f"No report `{report_id}` of yours was found. Omit "
                                "report_id to create a new report."
                            ),
                            is_error=True,
                        )
                report = await save_report(
                    own_db,
                    user_id=user_uuid,
                    spec_payload=spec_payload,
                    conversation_id=safe_uuid(conversation_id),
                    assignment_run_id=safe_uuid(assignment_run_id),
                    template_container_id=template_uuid,
                    report=existing,
                )
                doc_id = str(report.id)
                revision = report.revision
                spec = report.spec or {}
                await own_db.commit()
        except ReportSpecError as exc:
            # Validation failure: the report was NOT written. The message names
            # the offending field(s), so return it verbatim.
            return ToolResult(tool_call_id="", content=str(exc), is_error=True)
        except Exception as exc:
            logger.error(
                "generate_report failed: %s",
                format_exception_message(exc),
                exc_info=True,
            )
            return ToolResult(
                tool_call_id="",
                content="Error: failed to save the report. Please try again.",
                is_error=True,
            )

        verb = "Updated" if revision > 1 else "Created"
        return ToolResult(
            tool_call_id="",
            content=(
                f"{verb} report **{spec.get('title')}** "
                f"({len(spec.get('blocks') or [])} blocks, theme: "
                f"{spec.get('theme')}, revision {revision}).\n"
                f"{_summarize(spec)}\n\n"
                f"report_id: `{doc_id}` — pass it back as report_id to revise this "
                "same report (the rendered document updates in place).\n"
                f"[[REPORT:{doc_id}]]"
            ),
            # For a reader without a model: the flow's Create report node.
            structured={"report_id": doc_id, "revision": revision},
        )

    async def _get_report(report_id: str, **_kw: object) -> str:
        from app.core.database import async_session
        from app.services.reports import load_owned_report

        from .id_prefix import id_prefix

        doc_id = safe_uuid((report_id or "").strip())
        prefix = id_prefix(report_id) if doc_id is None else None
        user_uuid = safe_uuid(user_id)
        if doc_id is None and prefix is None:
            return (
                "Error: report_id must be a valid UUID, or the first 8 or more "
                "characters of the id of one of your reports."
            )
        if user_uuid is None:
            return "Error: reports are only available to signed-in users."

        try:
            async with async_session() as own_db:
                if prefix is not None:
                    doc_id, refusal = await _resolve_report_id(own_db, prefix, user_uuid)
                    if refusal:
                        return refusal
                    if doc_id is None:
                        return f"No report `{report_id}` of yours was found."
                report = await load_owned_report(own_db, doc_id, user_id=user_uuid)
                if report is None:
                    return f"No report `{report_id}` of yours was found."
                spec = report.spec or {}
                revision = report.revision
        except Exception as exc:
            logger.error(
                "get_report failed: %s", format_exception_message(exc), exc_info=True
            )
            return "Error: failed to read the report. Please try again."

        payload = json.dumps(spec, ensure_ascii=False, separators=(",", ":"))
        if len(payload) > _SPEC_READBACK_BUDGET:
            payload = (
                payload[:_SPEC_READBACK_BUDGET]
                + f"… (truncated, {len(payload)} chars total)"
            )
        return (
            f"Report **{spec.get('title')}** (revision {revision}, "
            f"{len(spec.get('blocks') or [])} blocks):\n"
            f"{_summarize(spec)}\n\n"
            f"Current spec:\n{payload}"
        )

    async def _export_report(report_id: str, **_kw: object) -> ToolResult:
        from app.core.database import async_session
        from app.llm.base import FileRef
        from app.services.report_export import ReportRenderError, export_report
        from app.services.reports import load_owned_report, resolve_report_export_stamp

        from ._shared import _persist_generated_file
        from .id_prefix import id_prefix

        doc_id = safe_uuid((report_id or "").strip())
        prefix = id_prefix(report_id) if doc_id is None else None
        user_uuid = safe_uuid(user_id)
        if doc_id is None and prefix is None:
            return ToolResult(
                tool_call_id="",
                content=(
                    "report_id must be a valid UUID, or the first 8 or more "
                    "characters of the id of one of your reports."
                ),
                is_error=True,
            )
        if user_uuid is None:
            return ToolResult(
                tool_call_id="", content="Reports are only available to signed-in users.", is_error=True
            )

        try:
            async with async_session() as own_db:
                # The reports an agent reads are its user's own, as for
                # get_report: an agent tool takes no admin bypass.
                if prefix is not None:
                    doc_id, refusal = await _resolve_report_id(own_db, prefix, user_uuid)
                    if refusal:
                        return ToolResult(tool_call_id="", content=refusal, is_error=True)
                    if doc_id is None:
                        return ToolResult(
                            tool_call_id="",
                            content=f"No report `{report_id}` of yours was found.",
                            is_error=True,
                        )
                report = await load_owned_report(own_db, doc_id, user_id=user_uuid)
                if report is None:
                    return ToolResult(
                        tool_call_id="",
                        content=f"No report `{report_id}` of yours was found.",
                        is_error=True,
                    )
                title = report.title
                # The render dresses the report in its template as it is now:
                # an edited template restyles it without a new revision, so
                # the template's stamp is part of the key.
                template = await resolve_report_export_stamp(
                    own_db,
                    report.template_container_id,
                    user_id=user_uuid,
                    report_theme=report.theme,
                )
                key = (doc_id, report.revision, template)
            earlier = exported_files.get(key)
            if earlier is not None:
                return export_result(title, "PDF", earlier, again=True)
            # The render happens OUTSIDE the session: it takes seconds and must
            # not hold a pooled connection while Chromium paints.
            exported = await export_report(report_id=doc_id, user_id=user_uuid, title=title)
            # ROI of chat work (#10544, S2b): an export is a use of the report.
            from app.services.roi_chat_records import record_signal_detached

            await record_signal_detached(kind="report", deliverable_id=doc_id, signal="exported")
            file_att = await _persist_generated_file(
                user_id=user_id,
                filename=exported.filename,
                raw_bytes=exported.data,
                content_type=exported.content_type,
                conversation_id=conversation_id,
                assignment_run_id=assignment_run_id,
            )
        except ReportRenderError as exc:
            return ToolResult(
                tool_call_id="",
                content=f"The report could not be exported: {exc}",
                is_error=True,
            )
        except Exception as exc:
            logger.error(
                "export_report failed: %s",
                format_exception_message(exc),
                exc_info=True,
            )
            return ToolResult(
                tool_call_id="",
                content=(
                    "Error: the report could not be exported (the platform's "
                    "headless browser is unavailable). The user can still export "
                    "it from the report's toolbar or open it at "
                    f"/reports/{doc_id}."
                ),
                is_error=True,
            )

        stored = TurnExport(
            file_ref=FileRef.from_attachment(file_att), filename=exported.filename, size=len(exported.data)
        )
        exported_files[key] = stored
        return export_result(title, "PDF", stored)

    executor = CallableToolExecutor()
    executor.register("generate_report", _generate_report)
    executor.register("get_report", _get_report)
    executor.register("export_report", _export_report)

    # Per-block field documentation. Written out rather than generated from
    # ``block_catalog()`` because the model needs the *shape* of the nested
    # objects (a risk_table row differs from an assessment_table row), not just
    # the key names — and the validator rejects anything not described here.
    block_help = (
        "Blocks (each object needs exactly one 'block' plus that block's own "
        "keys — unknown keys are rejected; every block also accepts 'title' (the "
        "section heading) and 'number' (the section number shown beside it, e.g. "
        '"01" or "R" — a string, and it also builds the table of contents):\n'
        "- kpi_row: items (1-6 of {value, label, note}) — the figures strip\n"
        "- prose: lead (rich_text, opening paragraph), rich_text, "
        "alert {label, rich_text}\n"
        "- decisions: items (1-6 of {title, text|rich_text, label, "
        f"confidence: {'|'.join(REPORT_CONFIDENCE_LEVELS)}, confidence_label}}) — "
        "what you are asking the reader to decide, or a set of analytic items "
        "(mechanisms, drivers) each with its own confidence\n"
        "- chain: steps (1-8 of {title, text}), note — a numbered attack/process "
        "chain with arrows\n"
        "- assessment_table: rows (1-24 of {statement, "
        f"confidence: {'|'.join(REPORT_CONFIDENCE_LEVELS)}, confidence_label, "
        "nature, appraisal}) — findings with the KIND of evidence behind them "
        "('public decision', 'local report', 'allegation', 'official "
        "attribution' — free text, use your own taxonomy) and how much confidence\n"
        "- scenarios: items (1-6 of {title, text, impact})\n"
        "- risk_table: rows (1-12 of {situation, "
        f"level: {'|'.join(REPORT_RISK_LEVELS)}, posture, level_label}}) — grades "
        "ONE situation per row\n"
        "- matrix: axis_label, column_labels ([str], 1-6), rows (1-8 of {label, "
        f"cells: [{{label, level: {'|'.join(REPORT_RISK_LEVELS)}}}]}}), note — a "
        "DOUBLE-ENTRY grid grading every combination of two variables (travel "
        "context x device profile, likelihood x impact). Each row needs exactly "
        "one cell per column label. Use this instead of risk_table whenever the "
        "grading depends on two things at once — flattening a 4x4 matrix into a "
        "table loses 12 of its 16 judgements\n"
        "- callout: rich_text, score, score_label — the highlighted position, "
        "with a headline figure\n"
        "- timeline: phases (1-8 of {label, headline, items: [str]}) — a phased "
        "action plan\n"
        "- checklist: items ([str]), columns (1 or 2)\n"
        "- fill_in_box: fields (1-12 of {label, value}) — omit 'value' to leave a "
        "blank rule for the reader to complete — plus notes ([str]) as status "
        "chips\n"
        "- annex: summary (required, the clickable line), items ([str]), "
        "rich_text, table {headers, rows}, note, open (bool)\n"
        "- sources: intro, items (1-30 of {label, url, note, ref}) — 'ref' is a "
        "citation handle like \"S01\" or \"S04 · FIELD REPORT\" so body text can "
        "point at an entry — plus limits "
        "(rich_text), open (bool)"
    )

    definitions = [
        ToolDefinition(
            name="generate_report",
            description=(
                "Build a STRUCTURED, printable report the user can read in the web "
                "app and export to PDF. Use it whenever the user asks for a "
                "report, a brief, an executive summary, a readout for management "
                "or a formatted assessment — not generate_file, which produces a "
                "static file, and not generate_presentation, which produces "
                "slides.\n"
                "You author structured blocks, NOT HTML: pick a block type per "
                "section from the catalog below and fill its values. The layout, "
                "the palette and the print stylesheet are the renderer's; you "
                "cannot and need not style anything. Free-form paragraph markup is "
                "allowed only inside 'rich_text' fields (p, strong, em, ul/ol/li, "
                "a, code, br, h3/h4, table); scripts, iframes, forms, <style> and "
                "event-handler attributes are rejected.\n"
                f"{block_help}\n"
                "Put the as-of date, classification and audience in header.tags, "
                "and the analytic bottom line in header.verdict.\n"
                "EDITING: to revise a report you already created, call this tool "
                "again with report_id set and the FULL new block list — the "
                "document the user is looking at updates in place. Use get_report "
                "first if you no longer have the blocks in context.\n"
                "Include the returned [[REPORT:...]] marker verbatim in your reply "
                "so the report renders at the right position."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": (
                            "Report title, shown on the chat card and in the "
                            "browser tab."
                        ),
                    },
                    "header": {
                        "type": "object",
                        "description": (
                            "The masthead: {title (required), eyebrow, subtitle, "
                            "tags: [str] (as-of date, classification, audience), "
                            "verdict: {grade, label, rich_text}}."
                        ),
                        "properties": {
                            "title": {"type": "string"},
                            "eyebrow": {
                                "type": "string",
                                "description": "Small uppercase kicker above the title.",
                            },
                            "subtitle": {"type": "string"},
                            "tags": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Framing chips: as-of date, classification, "
                                    "audience, analytic confidence."
                                ),
                            },
                            "verdict": {
                                "type": "object",
                                "description": (
                                    "The bottom line: {grade (short, shown large), "
                                    "label, rich_text}."
                                ),
                            },
                        },
                        "required": ["title"],
                    },
                    "blocks": {
                        "type": "array",
                        "description": (
                            "Ordered sections. Each item is an object with a "
                            "'block' key plus that block's fields "
                            f"(max {MAX_BLOCKS} blocks)."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "block": {
                                    "type": "string",
                                    "enum": list(REPORT_BLOCKS),
                                    "description": (
                                        "Which controlled block renders this "
                                        "section."
                                    ),
                                },
                                "title": {
                                    "type": "string",
                                    "description": "Section heading (all blocks).",
                                },
                                "number": {
                                    "type": "string",
                                    "description": (
                                        "Section number beside the heading, e.g. "
                                        '"01", "R", "A.2" (all blocks). Also '
                                        "builds the table of contents."
                                    ),
                                },
                                "items": {
                                    "type": "array",
                                    "description": (
                                        "kpi_row / decisions / scenarios: objects. "
                                        "checklist / annex: strings. sources: "
                                        "{label, url, note, ref}."
                                    ),
                                },
                                "rows": {
                                    "type": "array",
                                    "description": (
                                        "assessment_table: {statement, confidence, "
                                        "confidence_label, nature, appraisal}. "
                                        "risk_table: {situation, level, posture, "
                                        "level_label}. matrix: {label, cells: "
                                        "[{label, level}]} — one cell per column "
                                        "label."
                                    ),
                                    "items": {"type": "object"},
                                },
                                "column_labels": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": (
                                        "matrix: the horizontal axis (1-6 "
                                        "headers). Every row needs exactly this "
                                        "many cells."
                                    ),
                                },
                                "axis_label": {
                                    "type": "string",
                                    "description": (
                                        "matrix: top-left corner label naming what "
                                        'the two axes are, e.g. "Profile / device".'
                                    ),
                                },
                                "steps": {
                                    "type": "array",
                                    "description": "chain: {title, text}.",
                                    "items": {"type": "object"},
                                },
                                "phases": {
                                    "type": "array",
                                    "description": (
                                        "timeline: {label, headline, items: [str]}."
                                    ),
                                    "items": {"type": "object"},
                                },
                                "fields": {
                                    "type": "array",
                                    "description": (
                                        "fill_in_box: {label, value} — omit 'value' "
                                        "for a blank rule."
                                    ),
                                    "items": {"type": "object"},
                                },
                                "lead": {
                                    "type": "string",
                                    "description": (
                                        "prose: opening paragraph, rendered larger "
                                        "(sanitized HTML)."
                                    ),
                                },
                                "rich_text": {
                                    "type": "string",
                                    "description": (
                                        "Optional sanitized HTML paragraph block "
                                        "(no script/iframe/form/style/on* "
                                        "attributes)."
                                    ),
                                },
                                "alert": {
                                    "type": "object",
                                    "description": (
                                        "prose: called-out caveat {label, "
                                        "rich_text}."
                                    ),
                                },
                                "summary": {
                                    "type": "string",
                                    "description": (
                                        "annex: the clickable summary line."
                                    ),
                                },
                                "table": {
                                    "type": "object",
                                    "description": (
                                        "annex: {headers: [str], rows: [[str]]} — "
                                        "every row needs one cell per header."
                                    ),
                                },
                                "score": {
                                    "type": "string",
                                    "description": (
                                        "callout: headline figure, e.g. '4/5'."
                                    ),
                                },
                                "score_label": {
                                    "type": "string",
                                    "description": "callout: caption under the score.",
                                },
                                "note": {
                                    "type": "string",
                                    "description": (
                                        "chain / annex / matrix: caveat line under "
                                        "the block."
                                    ),
                                },
                                "notes": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "fill_in_box: status chips.",
                                },
                                "intro": {
                                    "type": "string",
                                    "description": "sources: method paragraph.",
                                },
                                "limits": {
                                    "type": "string",
                                    "description": (
                                        "sources: closing note on limits "
                                        "(sanitized HTML)."
                                    ),
                                },
                                "columns": {
                                    "type": "integer",
                                    "enum": [1, 2],
                                    "description": (
                                        "checklist: print in 1 or 2 columns "
                                        "(default 2)."
                                    ),
                                },
                                "open": {
                                    "type": "boolean",
                                    "description": (
                                        "annex / sources: start expanded on screen "
                                        "(every one prints open)."
                                    ),
                                },
                            },
                            "required": ["block"],
                        },
                    },
                    "theme": {
                        "type": "string",
                        "enum": list(REPORT_THEMES),
                        "description": (
                            "Visual theme (default: light — a report is meant to "
                            "be printed)."
                        ),
                        "default": "light",
                    },
                    "footer": {
                        "type": "string",
                        "description": (
                            "Single line at the foot of every printed page."
                        ),
                    },
                    "template_id": {
                        "type": "string",
                        "description": (
                            "Template id (search_templates); omit it to get the "
                            "platform's brand."
                        ),
                    },
                    "report_id": {
                        "type": "string",
                        "description": (
                            "Set ONLY to revise a report you already created: the "
                            "report with this id is rewritten in place with the "
                            "blocks you pass. Omit to create a new report."
                        ),
                    },
                },
                "required": ["title", "header", "blocks"],
            },
            source="builtin:generate_report",
        ),
        ToolDefinition(
            name="get_report",
            description=(
                "Read back the full block spec of a report you generated earlier, "
                "so you can revise, add or reorder its sections in a later turn. "
                "Returns the report's JSON spec; edit it and pass the result to "
                "generate_report with the same report_id."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "report_id": {
                        "type": "string",
                        "description": "UUID returned by generate_report.",
                    }
                },
                "required": ["report_id"],
            },
            source="builtin:generate_report",
        ),
        ToolDefinition(
            name="export_report",
            description=(
                "Export a report you generated to a PDF file, rendered "
                "server-side exactly as it prints from the report's viewer (A4, "
                "every annex open). Only when the user asks for a file — the "
                "report itself is already viewable and exportable from its "
                "toolbar. Include the returned [[FILE:...]] marker verbatim."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "report_id": {
                        "type": "string",
                        "description": "UUID returned by generate_report.",
                    }
                },
                "required": ["report_id"],
            },
            source="builtin:generate_report",
        ),
    ]

    return definitions, executor
