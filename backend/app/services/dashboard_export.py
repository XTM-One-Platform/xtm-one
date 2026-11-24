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

"""Custom dashboard export: a dashboard as a PDF or a PNG, or one visualization as a PNG, rendered server-side.

The deck export's headless Chromium (``presentation_export.open_render_page``:
its export slots, launch options, same-origin network policy and timeouts)
loads the dashboard's static render page (``/dashboards/{id}/static``,
``frontend/src/pages/DashboardStaticRender.tsx``): the dashboard as its
viewers see it, no app chrome, the chosen tabs one under the other or one
visualization alone. The page says when every widget has read its data
(``dashboardReady``).

The PDF holds a picture of each tab - a tab taller than ``PAGE_HEIGHT`` over
several pages - rather than a print of the
page: the grid places its widgets in pixels from the width it measured, and
Chrome's print pass lays the page out again at another width, which shrank
and cut the grid.

The page holds no session. It is handed a render token confined like the
deck's (``presentation_export.mint_confined_render_token``): scope
``dashboard_render``, bound to ONE dashboard, three minutes, refused by
``get_current_user``, accepted only by the render routes of
``api/v1/custom_dashboards.py`` (``/render``, ``/render/widgets/.../data``,
``/render/data-sources/.../data``, ``/render/assets/...``), which apply the
ordinary read rule to the token's user: the export reads every source with
the access of the person who asked for it, as the page reads for them.
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
import re
import uuid
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlencode

from app.services.presentation_export import (
    PDF_CONTENT_TYPE,
    ExportedFile,
    decode_confined_render_token,
    export_filename,
    mint_confined_render_token,
    open_render_page,
    render_base_url,
)

logger = logging.getLogger(__name__)

#: ``scope`` claim of a dashboard's render token: a deck's or a report's token is not one.
DASHBOARD_RENDER_TOKEN_SCOPE = "dashboard_render"  # noqa: S105 - a claim value, not a secret

PNG_CONTENT_TYPE = "image/png"

#: The viewport the dashboard is laid out in: a wide screen, as most people read it.
DASHBOARD_RENDER_WIDTH = 1440
DASHBOARD_RENDER_HEIGHT = 900

#: A PDF page is never taller than this (CSS pixels): a taller tab goes on over the next pages, in equal slices.
PAGE_HEIGHT = 2_400

#: A PDF holds at most this many pages; a dashboard taller than that is cut.
MAX_PAGES = 40

#: Pictures are drawn at twice the pixels, sharp on a slide, in a mail or zoomed in a PDF.
SCALE = 2

ExportFormat = Literal["pdf", "png"]


class DashboardRenderError(RuntimeError):
    """The headless render did not produce the dashboard (message is user-readable)."""


@dataclass(frozen=True, slots=True)
class DashboardRenderClaims:
    dashboard_id: uuid.UUID
    user_id: uuid.UUID


def mint_dashboard_render_token(dashboard_id: uuid.UUID, user_id: uuid.UUID) -> str:
    """Mint the confined token the dashboard's static render page reads with."""
    return mint_confined_render_token(
        scope=DASHBOARD_RENDER_TOKEN_SCOPE, claim="dashboard_id", subject_id=dashboard_id, user_id=user_id
    )


def decode_dashboard_render_token(token: str) -> DashboardRenderClaims:
    """Validate a dashboard render token and return what it is bound to (``RenderTokenError`` otherwise)."""
    dashboard_id, user_id = decode_confined_render_token(token, scope=DASHBOARD_RENDER_TOKEN_SCOPE, claim="dashboard_id")
    return DashboardRenderClaims(dashboard_id=dashboard_id, user_id=user_id)


def static_dashboard_url(
    dashboard_id: uuid.UUID, token: str, *, layout_ids: list[str] | None = None, visualization_id: str | None = None
) -> str:
    query: list[tuple[str, str]] = [("render_token", token)]
    query += [("tab", layout_id) for layout_id in layout_ids or []]
    if visualization_id:
        query.append(("viz", visualization_id))
    return f"{render_base_url()}/dashboards/{dashboard_id}/static?" + urlencode(query)


async def export_dashboard(
    *,
    dashboard_id: uuid.UUID,
    user_id: uuid.UUID,
    title: str,
    export_format: ExportFormat,
    layout_ids: list[str] | None = None,
    visualization_id: str | None = None,
) -> ExportedFile:
    """Render a dashboard (its chosen tabs, or one visualization) to a PDF or a PNG.

    The caller has already authorized *user_id* on the dashboard and checked
    that the tabs and the visualization exist; this only turns the page into
    bytes. The render token is minted for that user, so every source is read
    with THEIR access.
    """
    url = static_dashboard_url(
        dashboard_id,
        mint_dashboard_render_token(dashboard_id, user_id),
        layout_ids=layout_ids,
        visualization_id=visualization_id,
    )
    async with open_render_page(
        url,
        width=DASHBOARD_RENDER_WIDTH,
        height=DASHBOARD_RENDER_HEIGHT,
        device_scale_factor=SCALE,
        signal="dashboard",
        error=DashboardRenderError,
    ) as page:
        if export_format == "png":
            data = await page.locator("[data-export-root]").screenshot(type="png")
            return ExportedFile(export_filename(title, "png", fallback="dashboard"), PNG_CONTENT_TYPE, data)
        # Each tab from the top of the page, in equal slices of at most a page (no sliver on a last page).
        boxes = await page.evaluate(
            "() => [...document.querySelectorAll('[data-export-page]')].map((el) => {"
            " const r = el.getBoundingClientRect();"
            " return [r.left + window.scrollX, r.top + window.scrollY, r.width, r.height]; })"
        )
        pictures: list[bytes] = []
        for x, y, width, height in boxes:
            slices = max(1, math.ceil(height / PAGE_HEIGHT))
            for index in range(slices):
                if len(pictures) >= MAX_PAGES:
                    break
                clip = {"x": x, "y": y + index * height / slices, "width": width, "height": height / slices}
                pictures.append(await page.screenshot(type="png", clip=clip, full_page=True))
    if not pictures:
        raise DashboardRenderError("The dashboard has nothing to export.")
    data = await asyncio.to_thread(pdf_of_pictures, pictures)
    return ExportedFile(export_filename(title, "pdf", fallback="dashboard"), PDF_CONTENT_TYPE, data)


def pdf_of_pictures(pictures: list[bytes]) -> bytes:
    """One PDF page per picture, each page the picture's size at the screen's scale (96 CSS pixels an inch)."""
    from PIL import Image

    pages = [Image.open(io.BytesIO(picture)).convert("RGB") for picture in pictures]
    out = io.BytesIO()
    pages[0].save(out, format="PDF", save_all=True, append_images=pages[1:], resolution=96 * SCALE)
    return out.getvalue()


# ── Sending an export by email ────────────────────────────────────────────────
#
# A scheduled export is an assignment on an agent (invariant 1): its run calls
# the ``send_dashboard_export`` tool, which sends through the platform's email.
# The recipients are people with an account who may open the dashboard - never
# an address the platform does not know, so a run cannot carry a dashboard out
# - and each receives the export rendered with THEIR access, what they would
# see opening it.

#: The most people one send reaches (each is a render of their own).
MAX_RECIPIENTS = 10
MAX_SUBJECT_CHARS = 200
MAX_MESSAGE_CHARS = 2_000

_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}$")


@dataclass
class SendReport:
    """What one send did: who received the export, and who did not and why (fixed codes)."""

    sent: list[str]
    not_sent: dict[str, str]


async def send_dashboard_export(
    *,
    dashboard_id: uuid.UUID,
    sender_id: uuid.UUID,
    recipients: list[str],
    export_format: ExportFormat = "pdf",
    subject: str | None = None,
    message: str | None = None,
    layout_ids: list[str] | None = None,
) -> SendReport:
    """Email the dashboard, rendered for each recipient with their own access, to the people listed.

    *sender_id* must be able to read the dashboard through their own tiers
    (``LookupError`` otherwise; an admin's reach does not count).
    A recipient is an address of an active account that may read it; any other
    is reported (``no_account``, ``no_access``, ``invalid``), never sent to.
    Raises ``RuntimeError`` when the platform cannot send email.
    """
    from sqlalchemy import func, select

    from app.core.database import async_session
    from app.models.user import User
    from app.services.custom_dashboards import load_dashboard_for
    from app.services.email import (
        EmailAttachment,
        email_button,
        email_heading,
        email_paragraph,
        is_email_configured,
        platform_url,
        render_email,
        send_email,
    )

    if not is_email_configured():
        raise RuntimeError("email is not configured on this platform")
    wanted: list[str] = []
    report = SendReport(sent=[], not_sent={})
    for raw in recipients:
        address = str(raw).strip().lower()
        if not _EMAIL_RE.match(address):
            report.not_sent[address[:120] or "(empty)"] = "invalid"
        elif address not in wanted:
            wanted.append(address)
    for address in wanted[MAX_RECIPIENTS:]:
        report.not_sent[address] = "too_many"
    wanted = wanted[:MAX_RECIPIENTS]

    readers: list[tuple[uuid.UUID, str]] = []
    async with async_session() as db:
        sender = await db.get(User, sender_id)
        # The sender's own tiers: an agent's run sends what its owner may open, never through an admin's reach.
        found = await load_dashboard_for(db, dashboard_id, user_id=sender_id)
        if sender is None or found is None:
            raise LookupError("no dashboard the sender may read")
        dashboard = found[0]
        title, sender_name = dashboard.title, sender.full_name or sender.email
        rows = (
            await db.execute(select(User).where(func.lower(User.email).in_(wanted), User.is_active.is_(True)))
        ).scalars().all()
        by_email = {row.email.lower(): row for row in rows}
        for address in wanted:
            person = by_email.get(address)
            if person is None:
                report.not_sent[address] = "no_account"
            elif await load_dashboard_for(db, dashboard_id, user_id=person.id, is_admin=person.is_admin) is None:
                report.not_sent[address] = "no_access"
            else:
                readers.append((person.id, address))

    heading = " ".join((subject or title).split())[:MAX_SUBJECT_CHARS] or title
    link = f"{platform_url()}/dashboards/{dashboard_id}"
    body = email_heading(heading)
    if message and message.strip():
        body += email_paragraph(message.strip()[:MAX_MESSAGE_CHARS])
    body += email_paragraph(f"The attached file shows “{title}” as you may read it, sent by {sender_name}.", small=True, muted=True)
    body += email_button("Open the dashboard", link)
    html = render_email(title=heading, preheader=f"{title}: the dashboard, attached", body_html=body)
    text = f"{heading}\n\n{(message or '').strip()[:MAX_MESSAGE_CHARS]}\n\nOpen the dashboard: {link}\n"
    for reader_id, address in readers:
        try:
            exported = await export_dashboard(
                dashboard_id=dashboard_id, user_id=reader_id, title=title, export_format=export_format, layout_ids=layout_ids
            )
        except Exception:
            logger.warning("Dashboard %s could not be rendered for a recipient", dashboard_id, exc_info=True)
            report.not_sent[address] = "render_failed"
            continue
        attachment = EmailAttachment(exported.filename, exported.content_type, exported.data)
        if await send_email(to=address, subject=heading, html=html, text=text, attachments=[attachment]):
            report.sent.append(address)
        else:
            report.not_sent[address] = "send_failed"
    return report
