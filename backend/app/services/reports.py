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

"""Report persistence + serialization.

Single place that turns a validated ``ReportSpec`` into a ``Report`` row and back
into the JSON the frontend report renderer consumes. Shared by the agent-facing
``generate_report`` tool and the REST read endpoint so the two cannot drift on
visibility, revisioning or the resolved palette.

The palette resolution is where a template's palette is consumed: when a report links a
``TemplateContainer``, its ``theme_config`` is read back — visibility-checked,
then filtered through a whitelist — and returned alongside the spec. Everything
else about a report lives in its own ``spec``, so deleting a template never
breaks a report built from it.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.report import Report
from app.services.presentations import BRAND_THEME_CONFIG_KEYS
from app.services.report_spec import validate_spec

#: Theme keys a ``TemplateContainer.theme_config`` may contribute to a report.
#:
#: The brand six (:data:`app.services.presentations.BRAND_THEME_CONFIG_KEYS`)
#: plus the surfaces and semantic colours a document needs and a slide does not:
#: a report is paper-on-desk (``paper`` behind ``card`` behind ``line``) and it
#: grades things (``critical`` / ``elevated`` / ``moderate`` / ``controlled``
#: drive the risk badges and confidence columns). The deck-only keys
#: (gradient, tones, mono font, logo file ids) are deliberately NOT inherited: the
#: report renderer has no CSS variable for them, and a forwarded key nothing
#: reads is a promise the template author cannot see being broken.
#:
#: Extending the whitelist is the *whole* mechanism by which a template restyles
#: a report: the frontend maps each key to one CSS custom property, and anything
#: not listed here is dropped rather than forwarded. The value guard behind
#: ``sanitize_theme_config`` (``theme_value_problem``) is unchanged and
#: unchangeable from here, so a longer whitelist buys more brand control without
#: buying a way to author CSS or to make a reader's browser load a URL.
REPORT_THEME_CONFIG_KEYS: tuple[str, ...] = (
    *BRAND_THEME_CONFIG_KEYS,
    "paper",
    "card",
    "line",
    "critical",
    "elevated",
    "moderate",
    "controlled",
)


def serialize_report(
    report: Report,
    *,
    theme_config: dict[str, str] | None = None,
) -> dict:
    """Serialize a report to the JSON-safe payload the renderer consumes."""
    return {
        "id": str(report.id),
        "title": report.title,
        "theme": report.theme,
        "block_count": report.block_count,
        "revision": report.revision,
        "conversation_id": (
            str(report.conversation_id) if report.conversation_id else None
        ),
        "assignment_run_id": (
            str(report.assignment_run_id) if report.assignment_run_id else None
        ),
        "template_container_id": (
            str(report.template_container_id) if report.template_container_id else None
        ),
        "spec": report.spec or {},
        "theme_config": theme_config or {},
        "created_at": (report.created_at.isoformat() if report.created_at else None),
        "updated_at": (report.updated_at.isoformat() if report.updated_at else None),
    }


async def resolve_report_theme(
    db: AsyncSession,
    container_id: uuid.UUID | None,
    *,
    user_id: uuid.UUID | None,
) -> dict[str, str]:
    """Return the whitelisted ``theme_config`` of a report's linked template.

    Thin wrapper over the deck resolver with the report whitelist: the visibility
    logic is shared on purpose (a second copy could fall behind and start leaking
    an un-shared container's brand config), only the key set differs.
    """
    from app.services.presentations import resolve_template_theme

    return await resolve_template_theme(
        db, container_id, user_id=user_id, keys=REPORT_THEME_CONFIG_KEYS
    )


async def resolve_report_export_stamp(
    db: AsyncSession,
    container_id: uuid.UUID | None,
    *,
    user_id: uuid.UUID | None,
) -> str:
    """What a report's export depends on besides its revision: its template's
    render inputs and asset versions (see ``presentations.resolve_template_export_stamp``)."""
    from app.services.presentations import resolve_template_export_stamp

    return await resolve_template_export_stamp(
        db, container_id, user_id=user_id, keys=REPORT_THEME_CONFIG_KEYS
    )


async def save_report(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    spec_payload: object,
    conversation_id: uuid.UUID | None = None,
    assignment_run_id: uuid.UUID | None = None,
    template_container_id: uuid.UUID | None = None,
    report: Report | None = None,
) -> Report:
    """Validate *spec_payload* and create — or update in place — a report.

    Passing an existing *report* rewrites its spec and bumps ``revision`` instead
    of inserting a second row: that is what keeps the ``[[REPORT:<id>]]`` marker
    already sitting in the conversation valid when the agent revises a section,
    so the frontend reconciles the block list rather than mounting a second
    document.

    Raises ``ReportSpecError`` (from :func:`validate_spec`) if the payload is
    malformed — the caller surfaces it verbatim as a tool error.
    """
    spec = validate_spec(spec_payload)
    title = str(spec["title"])
    theme = str(spec.get("theme") or "light")
    block_count = len(spec.get("blocks") or [])

    if report is None:
        report = Report(
            # Assigned here rather than left to the column default so the id is
            # readable before the INSERT lands — the tool has to put it in the
            # ``[[REPORT:<id>]]`` marker it returns.
            id=uuid.uuid4(),
            user_id=user_id,
            conversation_id=conversation_id,
            assignment_run_id=assignment_run_id,
            template_container_id=template_container_id,
            title=title,
            theme=theme,
            block_count=block_count,
            revision=1,
            spec=spec,
        )
        db.add(report)
    else:
        report.title = title
        report.theme = theme
        report.block_count = block_count
        report.spec = spec
        report.revision = (report.revision or 1) + 1
        # An edit may re-point the report at a different template, but omitting
        # the argument must not silently unlink the one already set.
        if template_container_id is not None:
            report.template_container_id = template_container_id
        if conversation_id is not None and report.conversation_id is None:
            report.conversation_id = conversation_id

    await db.flush()
    # ROI of chat work (#10544): a report built in a conversation is one record, refreshed by every round.
    from app.services.roi_chat_records import record_deliverable, report_pages

    await record_deliverable(
        db,
        kind="report",
        deliverable_id=report.id,
        conversation_id=report.conversation_id,
        user_id=report.user_id,
        title=title,
        size=report_pages(block_count),
    )
    return report


async def load_owned_report(
    db: AsyncSession,
    report_id: uuid.UUID,
    *,
    user_id: uuid.UUID | None,
) -> Report | None:
    """Load a report the acting user may edit (their own), or None.

    Editing is owner-only on purpose: ``generate_report`` runs on a user's behalf
    inside a conversation, so an agent must never be able to rewrite a report
    belonging to somebody else — not even for an admin operator, whose agent
    would then be able to mutate other users' artifacts from a chat.
    """
    if user_id is None:
        return None
    result = await db.execute(
        select(Report).where(Report.id == report_id, Report.user_id == user_id)
    )
    return result.scalar_one_or_none()
