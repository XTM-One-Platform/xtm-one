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

"""Report export: a report as a PDF file, rendered server-side.

A report is printed by its own stylesheet - the ``@media print`` block
of ``frontend/src/components/report/report.css`` (A4, 12 mm margins, every
annex open, the screen-only table of contents hidden) - and that print used to
happen only in the reader's browser (``/reports/:id?print``), so a report had
no stored file: an agent could not attach it anywhere and the user got no
download chip. This prints the SAME document the same way, on the server: the
deck export's headless Chromium (``presentation_export.open_render_page`` -
its export slots, launch options, same-origin network policy and timeouts)
loads the report's static render page (``/reports/{id}/static``,
``frontend/src/pages/ReportStaticRender.tsx``: ``ReportView`` in its print
variant, no app chrome) and ``page.pdf()`` paginates it by the report's own
``@page`` rule.

The page holds no session. It is handed a render token confined like the
deck's (``presentation_export.mint_confined_render_token``): scope
``report_render``, bound to ONE report, three minutes, refused by
``get_current_user``, accepted only by ``GET /reports/{id}/render-spec``,
which applies the ordinary read rule to the token's user. A report
references no file, so there is no asset route.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
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

#: ``scope`` claim of a report's render token: a deck's token is not one.
REPORT_RENDER_TOKEN_SCOPE = "report_render"  # noqa: S105 - a claim value, not a secret

#: The viewport the page is laid out in before it is printed: an A4 sheet in
#: CSS pixels (96 per inch). The print layout itself is the paper's.
REPORT_RENDER_VIEWPORT = (794, 1123)


class ReportRenderError(RuntimeError):
    """The headless render did not produce the report (message is user-readable)."""


@dataclass(frozen=True, slots=True)
class ReportRenderClaims:
    report_id: uuid.UUID
    user_id: uuid.UUID


def mint_report_render_token(report_id: uuid.UUID, user_id: uuid.UUID) -> str:
    """Mint the confined token the report's static render page authenticates with."""
    return mint_confined_render_token(
        scope=REPORT_RENDER_TOKEN_SCOPE, claim="report_id", subject_id=report_id, user_id=user_id
    )


def decode_report_render_token(token: str) -> ReportRenderClaims:
    """Validate a report render token and return what it is bound to (``RenderTokenError`` otherwise)."""
    report_id, user_id = decode_confined_render_token(token, scope=REPORT_RENDER_TOKEN_SCOPE, claim="report_id")
    return ReportRenderClaims(report_id=report_id, user_id=user_id)


def static_report_url(report_id: uuid.UUID, token: str) -> str:
    return f"{render_base_url()}/reports/{report_id}/static?" + urlencode({"render_token": token})


async def export_report(*, report_id: uuid.UUID, user_id: uuid.UUID, title: str) -> ExportedFile:
    """Render a report to a PDF.

    The caller has already authorized *user_id* on the report; this only
    turns a report into bytes. The render token is minted for that user, so
    the page renders with THEIR visibility of the linked template's palette,
    exactly as the report reads in the app for them.
    """
    width, height = REPORT_RENDER_VIEWPORT
    url = static_report_url(report_id, mint_report_render_token(report_id, user_id))
    async with open_render_page(url, width=width, height=height, signal="report", error=ReportRenderError) as page:
        # Background graphics off, as in a print dialog by default: the report's
        # surfaces print through their ``print-color-adjust: exact``, and the
        # app's own page background, which would otherwise fill every page
        # around them, does not.
        data = await page.pdf(prefer_css_page_size=True)
    return ExportedFile(export_filename(title, "pdf", fallback="report"), PDF_CONTENT_TYPE, data)
