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

"""Presentation export: vector PDF, slide PNGs and the DOM measured for PPTX.

The on-screen deck is a React + reveal.js render of a ``PresentationSpec`` on a
fixed 1920x1080 canvas (1440x1080 for 4:3; 1920x2715 / 1920x2485 for the A4 /
letter portrait pages). An export that re-implemented that
layout server-side would drift from the screen the moment a stylesheet changed,
so every export goes through the SAME renderer instead: the platform's own
headless Chromium (already shipped for the ``browser`` tool and the
``generate_file`` PDF path) loads the deck's **static render page**
(``/presentations/{id}/static``, ``frontend/src/pages/PresentationStaticRender.tsx``),
which lays every slide out as one unscaled canvas, one under the other, with
no reveal.js runtime, no transitions and no controls. From that page:

* **PDF** — ``page.pdf()``: one page per slide (each ``.deck-static-slide``
  carries ``break-after: page``; the paper is set here to 13.333x7.5in /
  10x7.5in like PowerPoint, with the print scale that maps the 1080px canvas
  onto it), text stays vector and selectable, fonts are the deck's self-hosted
  ones.
* **Slide PNGs** — an element screenshot per slide, for the pixel-perfect PPTX
  variant (``pptx_writer.build_image_pptx``).
* **Measured primitives** — a DOM walk (``EXTRACT_PRIMITIVES_JS``) that returns,
  per slide, the rectangles, text runs and images Chromium actually painted,
  with their canvas coordinates, fonts and colours; ``pptx_writer.build_native_pptx``
  turns them into editable PowerPoint shapes placed exactly where the browser
  placed them, and the visuals a shape cannot express (icons, diagrams, logos,
  gradients) are captured as PNGs from the same page. A native ``chart`` block
  travels as DATA: the figure publishes its series, categories, the
  value axis it settled on and its resolved colours (``data-deck-chart`` +
  ``.deck-chart-facts``), and the writer rebuilds it as an editable
  PowerPoint chart (``pptx_writer._add_chart``), a right-hand value axis
  included, and a waterfall as a stacked column emulation;
  what PowerPoint cannot draw (a gauge, a map, bars read against two value
  axes) is captured at 2x, and every stand-in is said so in the export log
  (``ExportedFile.notes``). In the PDF a chart is the same inline SVG
  Chromium printed: vector, in the viewer's colours and fonts.

Authentication of the headless page
-----------------------------------
The page runs in a browser that holds no session. It is handed a **render
token**: a confined JWT (``scope = presentation_render``, bound to ONE deck,
three minutes) that ``get_current_user`` refuses on sight (``is_confined_token``)
and that only the two render routes in ``api/v1/presentations.py`` accept —
the deck's spec, and the files that spec references (and nothing else, see
:func:`referenced_file_ids`). It travels in the query string because ``<img>``
cannot send a header; it never leaves the server process, and a leaked one
buys three minutes of read access to one deck's own images.

The PDF a public link offers needs no render token at all: its page
(``/public/presentations/{token}/static``, :func:`render_public_deck_pdf`)
reads the deck and its pictures through the PUBLIC routes with the link's own
token, so it renders exactly what a visitor of the link sees and borrows no
account's visibility.

Network policy: the browser context aborts every request that is not for the
render origin itself (or ``data:`` / ``blob:``), so a deck cannot make the
exporter fetch anything external — fonts, scripts and Mermaid are all bundled
with the frontend.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlencode, urlsplit

import jwt

from app.core.config import settings
from app.core.security import JWT_CLOCK_SKEW_LEEWAY
from app.core.utils import format_exception_message, is_valid_uuid
from app.services.chart_export import (
    MIRRORED_CHART_KINDS,
    NATIVE_CHART_KINDS,
    TWO_AXIS_BARS_REASON,
    TWO_AXIS_PICTURE_SERIES_KINDS,
)
from app.services.presentation_pictures import is_file_reference_key
from app.services.presentation_spec import CANVAS_SIZES_PX, PAGE_SIZES_IN

if TYPE_CHECKING:
    from playwright.async_api import Page

logger = logging.getLogger(__name__)

#: ``scope`` claim of the render token. Its presence is what keeps the token
#: out of ``get_current_user`` (``is_confined_token`` rejects on presence).
RENDER_TOKEN_SCOPE = "presentation_render"  # noqa: S105 - a claim value, not a secret
RENDER_TOKEN_TTL = timedelta(minutes=3)

#: Export formats accepted by the REST endpoint and the ``export_presentation``
#: tool. ``pptx`` is the pixel-perfect variant (one full-bleed PNG per slide),
#: ``pptx_native`` the editable one (shapes measured from the render).
EXPORT_FORMATS: tuple[str, ...] = ("pdf", "pptx", "pptx_native")

PDF_CONTENT_TYPE = "application/pdf"
PPTX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
EXPORT_CONTENT_TYPES: dict[str, str] = {
    "pdf": PDF_CONTENT_TYPE,
    "pptx": PPTX_CONTENT_TYPE,
    "pptx_native": PPTX_CONTENT_TYPE,
}
EXPORT_EXTENSIONS: dict[str, str] = {
    "pdf": "pdf",
    # A dashboard's or a visualization's picture (``dashboard_export``).
    "png": "png",
    "pptx": "pptx",
    "pptx_native": "pptx",
}

#: The authoring canvas of every format, in CSS pixels — one definition in
#: ``presentation_spec`` (mirrored by ``geometry.ts``), so the renderer's
#: viewport, the PPTX writer's slide and the frontend's canvas can never drift.
CANVAS_SIZES: dict[str, tuple[int, int]] = CANVAS_SIZES_PX

#: PDF paper per format — PowerPoint's own slide sizes for 16:9 / 4:3 (so a
#: PDF page and a PPTX slide of the same deck have identical dimensions), real
#: ISO A4 and US Letter for the portrait pages. Mirrored by ``PDF_PAGE_SIZE``
#: in ``geometry.ts``: the static render page ALSO declares it as its
#: ``@page`` size, because Chromium keeps the orientation of the document's own
#: ``@page`` rule even when ``page.pdf()`` is handed an explicit landscape
#: paper (``prefer_css_page_size=False`` only drops the dimensions), and the
#: app ships a global portrait ``@page { size: A4 }`` for reports.
PDF_PAGE_SIZES_IN: dict[str, tuple[str, str]] = {
    ratio: (f"{width:g}in", f"{height:g}in") for ratio, (width, height) in PAGE_SIZES_IN.items()
}
#: Chromium prints at 96 CSS px/in; the print scale maps the canvas height onto
#: the page height (a 1080px slide canvas onto a 7.5in = 720px page, a 2715px
#: A4 canvas onto an 11.69in = 1122px page). For the slide formats it makes 1
#: canvas px = 0.5pt in the PDF exactly as in the PPTX.
PDF_PRINT_SCALES: dict[str, float] = {
    ratio: (PAGE_SIZES_IN[ratio][1] * 96) / CANVAS_SIZES_PX[ratio][1] for ratio in CANVAS_SIZES_PX
}
PDF_PRINT_SCALE = PDF_PRINT_SCALES["16:9"]
#: Device pixels per canvas pixel for the pictures of an EDITABLE PPTX:
#: the visuals left as pictures there (icons, diagrams, a gauge or a map chart)
#: sit next to crisp shapes and text, so they are captured at 2x.
NATIVE_PPTX_IMAGE_SCALE = 2

#: Port the unified image listens on (``EXPOSE 4000`` in the root Dockerfile).
UNIFIED_IMAGE_PORT = 4000
_FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend_dist"

NAVIGATION_TIMEOUT_MS = 30_000
#: The static page flags itself ready once the spec, the fonts, every image and
#: every Mermaid diagram have settled. A deck with many diagrams needs seconds,
#: not minutes; past this the render is failed rather than left hanging.
READY_TIMEOUT_MS = 60_000
#: One element screenshot of the editable PPTX capture loop (a picture, a hero
#: backdrop): the page is already rendered and settled, so a shot that has not
#: returned in this time is a detached or zero-size element, and the loop moves
#: on to the next picture (the exception is logged and skipped) rather than
#: sitting the whole default 30 s on every one of them.
CAPTURE_TIMEOUT_MS = 15_000
#: Renders launch a Chromium each; two at once is plenty for an interactive
#: export and keeps a burst of clicks from exhausting the worker's memory.
MAX_CONCURRENT_RENDERS = 2
#: Exports one user may request per minute from the export routes, decks and
#: reports together: a request waiting for one of the render slots
#: holds a request slot of its worker, so one account must not fill a worker
#: with queued exports.
EXPORTS_PER_USER_PER_MINUTE = 6
#: Renders the PUBLIC links of a worker may run at once: one of the
#: :data:`MAX_CONCURRENT_RENDERS`, taken before it, so a burst of anonymous
#: downloads never holds the slot an authenticated export waits for.
MAX_CONCURRENT_PUBLIC_RENDERS = 1
#: Attempts :func:`render_deck` makes when the BROWSER fails (a launch that
#: fails, a crash, a render page that refuses the connection in the first
#: seconds after a rollout), and the pause before each retry. A failure the
#: deck itself reports (:class:`PresentationRenderError`) is never retried,
#: and the render slot is released during the pause.
RENDER_ATTEMPTS = 3
RENDER_RETRY_BACKOFF_S: tuple[float, ...] = (1.0, 3.0)

_render_slots: dict[int, asyncio.Semaphore] = {}
_public_render_slots: dict[int, asyncio.Semaphore] = {}


def _loop_slot(slots: dict[int, asyncio.Semaphore], size: int) -> asyncio.Semaphore:
    """Per-event-loop semaphore (an asyncio primitive must not cross loops)."""
    loop = asyncio.get_running_loop()
    key = id(loop)
    slot = slots.get(key)
    if slot is None:
        slot = asyncio.Semaphore(size)
        slots.clear()
        slots[key] = slot
    return slot


def _render_slot() -> asyncio.Semaphore:
    return _loop_slot(_render_slots, MAX_CONCURRENT_RENDERS)


def _public_render_slot() -> asyncio.Semaphore:
    return _loop_slot(_public_render_slots, MAX_CONCURRENT_PUBLIC_RENDERS)


async def enforce_export_rate_limit(user_id: uuid.UUID) -> None:
    """The per-user budget of the export routes (HTTP 429 past it), shared by decks and reports."""
    from app.core.rate_limit import enforce_rate_limit

    await enforce_rate_limit(
        None, "render-export", limit=EXPORTS_PER_USER_PER_MINUTE, window_seconds=60, bucket=str(user_id)
    )


class RenderTokenError(ValueError):
    """The bearer is not a valid render token for the deck or report it names."""


class PresentationRenderError(RuntimeError):
    """The headless render did not produce a deck (message is user-readable)."""


class HeadlessBrowserUnavailableError(RuntimeError):
    """The headless browser failed, not the deck: its cause, whether a later call can work.

    ``str()`` is one user-readable sentence naming the cause and saying
    whether to retry; :attr:`retryable` is False for a host that cannot run
    the browser at all (Chromium not installed).
    """

    def __init__(self, cause: str, *, retryable: bool, attempts: int = 1) -> None:
        self.cause = cause
        self.retryable = retryable
        self.attempts = attempts
        tried = f" after {attempts} attempts" if attempts > 1 else ""
        advice = "a later call may work" if retryable else "retrying will not help until the deployment is fixed"
        super().__init__(f"the platform's headless browser failed{tried}: {cause}; {advice}")


#: Chromium network errors that mean the render page did not answer YET (a
#: platform restarting, a rollout): retried, and named with the origin.
_TRANSIENT_NET_ERRORS: tuple[str, ...] = (
    "ERR_CONNECTION_REFUSED",
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_CLOSED",
    "ERR_CONNECTION_TIMED_OUT",
    "ERR_EMPTY_RESPONSE",
    "ERR_ADDRESS_UNREACHABLE",
    "ERR_NAME_NOT_RESOLVED",
)
#: Messages of a browser that died under the render.
_CRASH_MARKERS: tuple[str, ...] = (
    "has been closed",
    "Target closed",
    "crashed",
    "Browser closed",
    "Connection closed",
)


def classify_browser_failure(exc: BaseException, *, origin: str) -> HeadlessBrowserUnavailableError:
    """Name what failed in a headless render that raised *exc* (not a deck failure).

    *origin* is the render page's scheme and host (never the URL: it carries
    the render token). The cause is read from Playwright's message: Chromium
    missing on the host (not retryable), the render page unreachable (its
    network error and origin), the page not answering in time, the browser
    crashing; anything else is named by its type and first line.
    """
    message = str(exc) or type(exc).__name__
    first_line = message.strip().splitlines()[0][:200] if message.strip() else type(exc).__name__
    if "Executable doesn't exist" in message or "missing dependencies" in message:
        return HeadlessBrowserUnavailableError("Chromium is not installed for Playwright on this host", retryable=False)
    for code in _TRANSIENT_NET_ERRORS:
        if code in message:
            return HeadlessBrowserUnavailableError(
                f"the render page at {origin} could not be reached (net::{code}); "
                "check PRESENTATION_RENDER_BASE_URL on the process that runs the tool",
                retryable=True,
            )
    if type(exc).__name__ == "TimeoutError" and "goto" in message:
        return HeadlessBrowserUnavailableError(
            f"the render page at {origin} did not answer within {NAVIGATION_TIMEOUT_MS // 1000} seconds",
            retryable=True,
        )
    if any(marker in message for marker in _CRASH_MARKERS):
        return HeadlessBrowserUnavailableError("the browser closed or crashed during the render", retryable=True)
    return HeadlessBrowserUnavailableError(f"{type(exc).__name__}: {first_line}", retryable=True)


@dataclass(frozen=True, slots=True)
class RenderClaims:
    presentation_id: uuid.UUID
    user_id: uuid.UUID


# ── Render token ──────────────────────────────────────────────────────────────


def mint_confined_render_token(*, scope: str, claim: str, subject_id: uuid.UUID, user_id: uuid.UUID) -> str:
    """Mint a confined token a static render page authenticates with.

    Deliberately NOT built with ``create_access_token``: this must never be a
    session. ``scope`` marks it confined (``get_current_user`` refuses it) and
    names what it renders; *claim* binds it to ONE subject (a deck, a report)
    so the render routes can refuse it for any other. Shared by the deck
    export and the report export, each with its own scope.
    """
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "scope": scope,
        claim: str(subject_id),
        "iat": int(now.timestamp()),
        "exp": now + RENDER_TOKEN_TTL,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_confined_render_token(token: str, *, scope: str, claim: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Validate a render token of *scope* and return ``(subject id, user id)``.

    Raises :class:`RenderTokenError` for anything that is not a live render
    token of that scope — including a perfectly valid *session* JWT, which
    carries no ``scope``: the two credential kinds are disjoint in both
    directions, and a deck's token is not a report's.
    """
    try:
        payload = jwt.decode(
            token, settings.secret_key, algorithms=[settings.jwt_algorithm], leeway=JWT_CLOCK_SKEW_LEEWAY
        )
    except jwt.PyJWTError as exc:
        raise RenderTokenError("invalid or expired render token") from exc
    if payload.get("scope") != scope:
        raise RenderTokenError("not a render token")
    try:
        return uuid.UUID(str(payload[claim])), uuid.UUID(str(payload["sub"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise RenderTokenError("malformed render token") from exc


def mint_render_token(presentation_id: uuid.UUID, user_id: uuid.UUID) -> str:
    """Mint the confined token the deck's static render page authenticates with."""
    return mint_confined_render_token(
        scope=RENDER_TOKEN_SCOPE, claim="presentation_id", subject_id=presentation_id, user_id=user_id
    )


def decode_render_token(token: str) -> RenderClaims:
    """Validate a deck render token and return what it is bound to."""
    presentation_id, user_id = decode_confined_render_token(token, scope=RENDER_TOKEN_SCOPE, claim="presentation_id")
    return RenderClaims(presentation_id=presentation_id, user_id=user_id)


# ── What a deck may fetch ─────────────────────────────────────────────────────


def referenced_file_ids(spec: dict | None, theme_config: dict | None = None) -> frozenset[str]:
    """Every platform file id a deck's render can legitimately request.

    Walks the spec for any ``*_file_id`` / ``file_id`` key (slide backgrounds,
    column images, ``image`` blocks, speaker photos — and whatever a future
    layout adds, without this list needing to know) plus the logo keys of the
    resolved theme. The render-asset route serves a file iff its id is in
    this set: the render token is a capability over ONE deck's own images,
    not a general file-read credential.
    """
    found: set[str] = set()

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str) and is_file_reference_key(key) and is_valid_uuid(value):
                    found.add(str(uuid.UUID(value)))
                else:
                    _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(spec or {})
    for key, value in (theme_config or {}).items():
        if key.endswith("_file_id") and isinstance(value, str) and is_valid_uuid(value):
            found.add(str(uuid.UUID(value)))
    return frozenset(found)


# ── Where the page lives ──────────────────────────────────────────────────────


def render_base_url() -> str:
    """Origin the headless browser loads the static render page from.

    Explicit ``PRESENTATION_RENDER_BASE_URL`` wins. Otherwise the unified image
    (which serves the built frontend itself) uses its own loopback address —
    no trip through the ingress, no dependency on the pod resolving its public
    hostname — and a split setup (Vite dev server + API) falls back to
    ``FRONTEND_URL``.

    Whatever the origin, the page lives under the platform's URL path prefix
    (``BASE_PATH``): the SPA's router carries the prefix as its
    ``basename`` and renders nothing for a location outside it, so a render URL
    without the prefix is a deck that never reports ready. An explicit value
    is an ORIGIN by contract and gets the prefix appended; one that already
    carries a path (a public URL spelled with the prefix) is used as is.
    """
    configured = (settings.presentation_render_base_url or "").strip().rstrip("/")
    if configured:
        if settings.base_path and not urlsplit(configured).path.strip("/"):
            return f"{configured}{settings.base_path}"
        return configured
    if _FRONTEND_DIST.is_dir():
        # The loopback listener accepts the SPA route with or without the
        # URL path prefix (``BASE_PATH``); spelled with it so the page
        # is loaded exactly the way a browser would load it.
        return f"http://127.0.0.1:{UNIFIED_IMAGE_PORT}{settings.base_path}"
    return (settings.frontend_url or settings.base_url).rstrip("/")


def static_render_url(presentation_id: uuid.UUID, token: str) -> str:
    return f"{render_base_url()}/presentations/{presentation_id}/static?" + urlencode({"render_token": token})


def public_static_render_url(public_token: str) -> str:
    """The static render page of a deck AS ITS PUBLIC LINK SHOWS IT.

    The page reads the deck and its pictures through the public routes with
    the link's own token - what any visitor holding it reads - so the PDF a
    visitor downloads can carry nothing the public page does not show, and
    no account's visibility is borrowed to render it.
    """
    return f"{render_base_url()}/public/presentations/{quote(public_token, safe='')}/static"


# ── Measured DOM → primitives ─────────────────────────────────────────────────

#: Runs in the static render page and returns, for every ``.deck-static-slide``,
#: the painted primitives in paint order:
#:
#: * ``rect``  — an element with a visible background colour or border (cards,
#:   tiles, table cells, dividers): position, fill, line, corner radius.
#: * ``text``  — a block whose content is text and inline markup only: its box,
#:   padding (→ text-frame insets), alignment, and one run per style change
#:   (font family / size / weight / italic / colour), ``\n`` for ``<br>``.
#: * ``image`` — an ``<img>``, an inline ``<svg>`` (Lucide icon, Mermaid
#:   diagram), an element painting a background image or gradient text: its
#:   box plus a ``data-export-id`` the exporter screenshots. Not descended into.
#:   A ``chart`` block PowerPoint cannot draw is one of these, tagged
#:   ``chart: <kind>`` and ``chart_reason`` (the kind itself for a gauge or a
#:   map, ``chart_export.TWO_AXIS_BARS_REASON`` for bars read against two
#:   value axes) so the export log can name it.
#: * ``chart`` — a native ``chart`` block PowerPoint can draw: the
#:   chart as data (``chart``, the figure's ``data-deck-chart`` payload), the
#:   boxes of the canvas, the legend, the plot's inner grid and the target
#:   lines, the value axes the viewer settled on (``axis``, and
#:   ``secondary_axis`` for a right-hand one), the value labels as
#:   written (``labels``; ``values_alternate`` when a line's or an area's
#:   alternate over and under their points; ``values_fit``, each
#:   value's ``lift`` and the ``bar_spacing`` of bars that gave way, or of a
#:   group of upright bars kept tight, when the values past the bars
#:   were fitted; a thin segment's value past its
#:   stack, role ``annotation``, with its ``leaders``' legs and
#:   ``annotation_size``), the resolved series /
#:   slice colours and the fonts and colours of the drawing (``style``, each
#:   value axis' tick colour included, the tick size ``label_size`` set and
#:   the legend's own size). The writer rebuilds it as an editable chart
#:   (``pptx_writer._add_chart``). Not descended into, except for its caption
#:   and a donut's centre reading, which are text.
#: * An ``architecture`` stays ONE picture of its drawing, but a zone's chips
#: are ``rect`` + ``text`` items at the positions the page draws
#:   them, one ``group`` per zone and a ``subgroup`` per chip; the picture is
#:   tagged ``lifted`` and captured without them (``data-export-lifted``).
#: * ``oval`` / ``connector`` - the shapes of a native ``org_chart``
#:   (``data-deck-org-chart``): an org chart is written as the SVG the layout
#:   engine drew, never as a picture - ``rect`` and ``oval`` shapes (``dash``
#:   ``dash`` / ``dot`` for an open or an external seat), ``text`` items, and
#:   one ``connector`` per polyline the engine published (``points``, slide px).
#:   Every item carries the chart's ``group`` and its seat's ``subgroup``, so
#:   the writer makes the chart one group and each seat a group inside it.
#:
#: Coordinates are canvas pixels relative to the slide's top-left corner
#: (the slide is rendered unscaled, so 1px here is 1px of the 1920x1080
#: canvas); an ``image``'s box is the one its screenshot covers, the
#: element's rounded out to whole pixels. Alpha is kept on colours; the writer
#: blends it against the slide background because a PPTX solid fill has no
#: alpha channel.
#:
#: Two DOM facts this has to get right, because the collateral blocks lean on
#: them everywhere (check-list rows, badge pills, toned table columns, icon
#: buttons):
#:
#: * An inline ``<svg>`` lives in the SVG namespace, so its ``tagName`` is the
#:   lowercase ``"svg"`` — an uppercase comparison never matches and every
#:   Lucide icon and Mermaid diagram silently vanishes from the export.
#: * A ``color-mix()`` tint (the ``*-soft`` tones) is resolved by Chromium to
#:   ``color(srgb r g b / a)`` with 0..1 channels, not to ``rgb()``. Anything
#:   else exotic goes through a 1x1 canvas, which normalises every CSS colour
#:   the browser can paint.
#:
#: An icon sitting in the text flow (``<li><svg/>Unlimited assets</li>``) is
#: an *atom*: it is captured as its own picture and the text box is framed on
#: the text nodes only, so the picture and the text frame never overlap.
#:
#: ``__NATIVE_CHART_KINDS__``, ``__MIRRORED_CHART_KINDS__``,
#: ``__TWO_AXIS_PICTURE_SERIES_KINDS__`` and ``__TWO_AXIS_BARS_REASON__`` are
#: filled from ``chart_export`` below, so the browser and the writer can never
#: disagree on which charts become charts and which stay pictures, nor on
#: which keep the viewer's legend as text (a butterfly's).
_EXTRACT_PRIMITIVES_TEMPLATE = r"""
() => {
  const INLINE = new Set(["SPAN", "STRONG", "EM", "B", "I", "CODE", "A", "BR", "SUB", "SUP", "U", "S", "MARK", "SMALL", "ABBR", "TIME"]);
  const SVG_NS = "http://www.w3.org/2000/svg";
  const isAtom = (el) => el.namespaceURI === SVG_NS || el.tagName === "IMG" || el.tagName === "CANVAS" || el.tagName === "VIDEO";
  const px = (value) => parseFloat(value) || 0;
  const chan = (part) => (part.endsWith("%") ? parseFloat(part) / 100 : parseFloat(part));
  const colorCanvas = document.createElement("canvas");
  colorCanvas.width = 1; colorCanvas.height = 1;
  const colorCtx = colorCanvas.getContext("2d", { willReadFrequently: true });
  const colorCache = new Map();
  const parseColor = (value) => {
    if (!value || value === "none" || value === "transparent") return null;
    if (colorCache.has(value)) return colorCache.get(value);
    let out = null;
    let m = /^rgba?\(([^)]+)\)$/.exec(value);
    if (m) {
      const parts = m[1].split(/[\s,\/]+/).filter(Boolean);
      const a = parts.length > 3 ? chan(parts[3]) : 1;
      if (a > 0) out = { r: parseFloat(parts[0]), g: parseFloat(parts[1]), b: parseFloat(parts[2]), a };
    } else if ((m = /^color\(srgb\s+([^)]+)\)$/.exec(value))) {
      const parts = m[1].split(/[\s\/]+/).filter(Boolean);
      const a = parts.length > 3 ? chan(parts[3]) : 1;
      if (a > 0) out = { r: Math.round(chan(parts[0]) * 255), g: Math.round(chan(parts[1]) * 255), b: Math.round(chan(parts[2]) * 255), a };
    } else if (colorCtx) {
      colorCtx.clearRect(0, 0, 1, 1);
      colorCtx.fillStyle = "#010203";
      colorCtx.fillStyle = value;
      colorCtx.fillRect(0, 0, 1, 1);
      const [r, g, b, a] = colorCtx.getImageData(0, 0, 1, 1).data;
      if (a > 0) out = { r, g, b, a: a / 255 };
    }
    colorCache.set(value, out);
    return out;
  };
  const textColor = (cs) => {
    const fill = cs.webkitTextFillColor;
    const useFill = fill && fill !== "rgba(0, 0, 0, 0)" && cs.webkitBackgroundClip !== "text";
    return parseColor(useFill ? fill : cs.color);
  };
  // Outlined text (-webkit-text-stroke, the ghost section number) is a run
  // outline in PowerPoint — kept as editable text, not screenshotted, because
  // an element screenshot would also bake in whatever overlaps it.
  const textStroke = (cs) => {
    const width = px(cs.webkitTextStrokeWidth);
    if (!(width > 0)) return null;
    const color = parseColor(cs.webkitTextStrokeColor);
    return color ? { ...color, width } : null;
  };
  // Text-like: every child is inline markup or an atom (an icon in the flow).
  // An inline TAG laid out as a box (a pill span with display:inline-flex, a
  // button anchor with display:flex) is a box, not markup: it keeps its own
  // background and gets its own text frame.
  const isMarkup = (el) => INLINE.has(el.tagName) && (getComputedStyle(el).display === "inline" || el.tagName === "BR");
  const textLike = (el) => {
    for (const child of el.children) {
      if (isAtom(child)) continue;
      if (!isMarkup(child)) return false;
      if (!textLike(child)) return false;
    }
    return true;
  };
  const collectAtoms = (el, atoms) => {
    for (const child of el.children) {
      if (isAtom(child)) atoms.push(child);
      else collectAtoms(child, atoms);
    }
  };
  // CSS `text-transform` is a rendering effect the DOM text does not carry:
  // apply it to the run, or an uppercase eyebrow ("CHALLENGES") exports in
  // the sentence case it was typed in.
  const transformText = (text, transform) => {
    if (transform === "uppercase") return text.toUpperCase();
    if (transform === "lowercase") return text.toLowerCase();
    if (transform === "capitalize") return text.replace(/(^|\s)(\S)/g, (m, lead, ch) => lead + ch.toUpperCase());
    return text;
  };
  const collectRuns = (el, inherited, runs, k) => {
    for (const node of el.childNodes) {
      if (node.nodeType === 3) {
        const text = transformText(node.textContent.replace(/\s+/g, " "), inherited.transform);
        if (text) runs.push({ ...inherited, text });
      } else if (node.nodeType === 1) {
        if (isAtom(node)) continue;
        if (node.tagName === "BR") { runs.push({ ...inherited, text: "\n" }); continue; }
        const cs = getComputedStyle(node);
        if (cs.display === "none" || cs.visibility === "hidden") continue;
        collectRuns(node, baseStyle(cs, k), runs, k);
      }
    }
  };
  // Shrink-to-fit (`fit.ts`): a body taller than its slide is scaled
  // down by the ratio its `.deck-fit` host publishes as `data-fit`. Bounding
  // rects already include that transform; computed styles do not, so every
  // length read from a computed style (font size, line height, padding,
  // letter spacing, border widths, corner radius) is multiplied by the ratio
  // of the host the element sits in - or the PPTX shows 40pt text in a box
  // measured for 30pt.
  const fitOf = (el) => {
    const host = el.closest(".deck-fit[data-fit]");
    const k = host ? parseFloat(host.dataset.fit) : 1;
    return k > 0 && k < 1 ? k : 1;
  };
  // Union of the client rects of *nodes* (text nodes; atoms excluded).
  const rectUnion = (nodes) => {
    const range = document.createRange();
    let union = null;
    for (const node of nodes) {
      if (!node.textContent.trim()) continue;
      range.selectNodeContents(node);
      for (const r of range.getClientRects()) {
        if (r.width <= 0 || r.height <= 0) continue;
        union = union
          ? { left: Math.min(union.left, r.left), top: Math.min(union.top, r.top), right: Math.max(union.right, r.right), bottom: Math.max(union.bottom, r.bottom) }
          : { left: r.left, top: r.top, right: r.right, bottom: r.bottom };
      }
    }
    return union ? { left: union.left, top: union.top, width: union.right - union.left, height: union.bottom - union.top } : null;
  };
  const textNodesOf = (el) => {
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    const nodes = [];
    for (let node = walker.nextNode(); node; node = walker.nextNode()) nodes.push(node);
    return nodes;
  };
  const baseStyle = (cs, k) => ({
    font: cs.fontFamily, size: px(cs.fontSize) * k, weight: parseInt(cs.fontWeight, 10) || 400,
    italic: cs.fontStyle === "italic", color: textColor(cs), stroke: textStroke(cs),
    underline: (cs.textDecorationLine || "").includes("underline"), mono: /mono/i.test(cs.fontFamily),
    transform: cs.textTransform,
  });
  // Decorations drawn by pseudo-elements - a slide title's gradient rule, a
  // card's beam and bloom, a callout's bar, a list's bullet, an eyebrow's
  // corner marks - are no DOM nodes, so the walk below never met them and the
  // editable PPTX dropped every one. Each `::before` / `::after` that
  // paints (an empty `content` with a fill, a gradient or a border) gets a
  // STAND-IN: a span at the slide root, laid exactly over it with its
  // computed style, hidden on the page (`[data-export-pseudo]` in the
  // stylesheet - the pseudo-element keeps drawing the screen and the PDF) and
  // written from its host, under the host's content when it is painted under
  // it. A text `content` (a counter, a separator glyph) is not a shape and is
  // left alone.
  const PSEUDOS = ["before", "after"];
  const UNCOPIED = new Set(["content", "counter-increment", "counter-reset", "counter-set", "quotes", "visibility"]);
  const paints = (cs) =>
    (cs.backgroundImage && cs.backgroundImage !== "none") ||
    !!parseColor(cs.backgroundColor) ||
    ["Top", "Right", "Bottom", "Left"].some(
      (side) => px(cs[`border${side}Width`]) > 0 && cs[`border${side}Style`] !== "none" && !!parseColor(cs[`border${side}Color`]),
    );
  const standIns = new Map();
  const materializePseudos = (slide) => {
    const found = [];
    for (const host of slide.querySelectorAll("*")) {
      if (isAtom(host) || host.hasAttribute("data-export-pseudo")) continue;
      for (const which of PSEUDOS) {
        const cs = getComputedStyle(host, `::${which}`);
        if (cs.content !== '""' || cs.display === "none" || cs.visibility === "hidden" || px(cs.opacity) === 0 || !paints(cs)) continue;
        const style = [];
        for (let i = 0; i < cs.length; i += 1) {
          if (!UNCOPIED.has(cs[i])) style.push([cs[i], cs.getPropertyValue(cs[i])]);
        }
        const positioned = cs.position === "absolute" || cs.position === "fixed";
        // Painted under the host's content: a `::before`, or a positioned
        // layer with a negative z-index (a card's light).
        const behind = which === "before" || (positioned && parseInt(cs.zIndex, 10) < 0);
        found.push({ host, which, style, behind });
      }
    }
    if (!found.length) return;
    // Where each one is drawn: a probe with its style takes its place while
    // the pseudo-element is switched off (`data-export-measure-*`), all of a
    // slide's at once - one layout - and leaves right after.
    for (const entry of found) {
      const probe = document.createElement("span");
      for (const [prop, value] of entry.style) probe.style.setProperty(prop, value);
      entry.host.setAttribute(`data-export-measure-${entry.which}`, "");
      if (entry.which === "before") entry.host.insertBefore(probe, entry.host.firstChild);
      else entry.host.appendChild(probe);
      entry.probe = probe;
    }
    for (const entry of found) entry.rect = entry.probe.getBoundingClientRect();
    for (const entry of found) {
      entry.probe.remove();
      entry.host.removeAttribute(`data-export-measure-${entry.which}`);
    }
    const origin = slide.getBoundingClientRect();
    for (const entry of found) {
      const { rect } = entry;
      if (!(rect.width > 0.5 && rect.height > 0.5)) continue;
      const span = document.createElement("span");
      for (const [prop, value] of entry.style) span.style.setProperty(prop, value);
      // Out of every flow, at the box the pseudo-element was drawn in.
      Object.assign(span.style, {
        position: "absolute", left: `${rect.left - origin.left - slide.clientLeft}px`, top: `${rect.top - origin.top - slide.clientTop}px`,
        right: "auto", bottom: "auto", width: `${rect.width}px`, height: `${rect.height}px`, margin: "0",
        minWidth: "0", minHeight: "0", maxWidth: "none", maxHeight: "none", boxSizing: "border-box",
        transform: "none", zIndex: "auto", flex: "none",
      });
      span.setAttribute("data-export-pseudo", entry.which);
      span.setAttribute("aria-hidden", "true");
      slide.appendChild(span);
      if (!standIns.has(entry.host)) standIns.set(entry.host, []);
      standIns.get(entry.host).push({ span, behind: entry.behind });
    }
  };
  let seq = 0;
  const slides = Array.from(document.querySelectorAll(".deck-static-slide"));
  return slides.map((slide, index) => {
    materializePseudos(slide);
    const base = slide.getBoundingClientRect();
    const slideStyle = getComputedStyle(slide);
    const items = [];
    const box = (rect) => ({ x: rect.left - base.left, y: rect.top - base.top, w: rect.width, h: rect.height });
    // What an element screenshot covers: Playwright crops the element's
    // document box rounded OUT to whole CSS pixels (`enclosingIntRect`). A
    // picture takes that box, so the writer lays the capture where the page
    // painted it instead of squeezing the rounding into the element's own box
    // - which moved a connector drawn from card to card a pixel off the card
    // above it.
    const captureBox = (rect) => {
      const left = Math.floor(rect.left + window.scrollX + 1e-3);
      const top = Math.floor(rect.top + window.scrollY + 1e-3);
      const right = Math.ceil(rect.right + window.scrollX - 1e-3);
      const bottom = Math.ceil(rect.bottom + window.scrollY - 1e-3);
      return { x: left - window.scrollX - base.left, y: top - window.scrollY - base.top, w: right - left, h: bottom - top };
    };
    const isInside = (rect) => rect.width > 0.5 && rect.height > 0.5 && rect.right > base.left && rect.left < base.right && rect.bottom > base.top && rect.top < base.bottom;
    // A render group (`data-export-group`, a logo wall card): every
    // primitive drawn inside one carries its id, and the writer makes the run
    // one PowerPoint group shape. The id is the element's, per slide.
    const groups = new Map();
    const groupOf = (el) => {
      const host = el && el.closest ? el.closest("[data-export-group]") : null;
      if (!host) return undefined;
      if (!groups.has(host)) groups.set(host, `g${index}-${groups.size}`);
      return groups.get(host);
    };
    const emitImage = (el, rect, opacity) => {
      if (!isInside(rect)) return null;
      const id = `x${index}-${seq++}`;
      el.setAttribute("data-export-id", id);
      const item = { kind: "image", id, ...captureBox(rect), opacity, group: groupOf(el) };
      // A hero backdrop is painted UNDER the slide's text: its
      // screenshot must not bake that text in, or the PPTX carries the
      // headline twice (once in the picture, once as editable runs). The
      // capture loop isolates it (`data-export-isolate` on the slide) first.
      // A picture INSIDE a backdrop is the backdrop's drawing (the tiles of a
      // mosaic cover, an SVG in a wrapper that paints nothing).
      if (el.closest("[data-export-backdrop]")) item.backdrop = true;
      // A picture drawn edge to edge between two boxes (`data-export-alone`:
      // the connector of a `challenge_answer`) is captured alone on a
      // transparent page, like a stand-in: its crop rounds out to the pixel
      // and would carry the border of the box it touches.
      if (el.hasAttribute("data-export-alone")) {
        item.backdrop = true;
        item.alpha = true;
      }
      // The picture's alt text travels to the PPTX as its description.
      if (el.tagName === "IMG" && (el.getAttribute("alt") || "").trim()) item.alt = el.getAttribute("alt").trim();
      // A video block's poster: the editable PPTX embeds the movie behind it.
      const video = el.getAttribute("data-export-video");
      if (video) item.video = video;
      items.push(item);
      return item;
    };
    // An element's own box - fill, border, corner radius - as one shape, when
    // it paints one. Returns whether it did.
    const emitRect = (el, cs, rect, opacity, k) => {
      const fill = parseColor(cs.backgroundColor);
      // Each side on its own: a card is a thin grey frame with a thick toned
      // top rule, and painting the whole frame in the tone would misrepresent
      // it. The colour of a side that is not drawn is meaningless (`border: 0`
      // resets it to currentColor), so an undrawn side carries none.
      const sides = {};
      for (const side of ["top", "right", "bottom", "left"]) {
        const cap = side[0].toUpperCase() + side.slice(1);
        const width = px(cs[`border${cap}Width`]) * k;
        sides[side] = { width, color: width > 0 ? parseColor(cs[`border${cap}Color`]) : null };
      }
      const drawn = Object.values(sides).filter((side) => side.width > 0 && side.color);
      const lineWidth = drawn.length ? Math.max(...drawn.map((side) => side.width)) : 0;
      const line = drawn.length ? drawn[0].color : null;
      // A dashed or dotted frame (a configuration card's "available, not
      // used" chip) keeps its dash as PowerPoint's preset.
      const borderStyle = cs.borderTopStyle;
      const dash = borderStyle === "dashed" ? "dash" : borderStyle === "dotted" ? "dot" : null;
      const sameColor = (a, b) => !!a && !!b && a.r === b.r && a.g === b.g && a.b === b.b && a.a === b.a;
      // Uniform = one shape outline: every side drawn, same width, same colour.
      const isUniformBorder = drawn.length === 4 && drawn.every((side) => side.width === lineWidth && sameColor(side.color, line));
      if (!fill && !line) return false;
      items.push({
        kind: "rect", ...box(rect), opacity, group: groupOf(el),
        fill: fill || null,
        line: isUniformBorder ? { ...line, width: lineWidth } : null,
        edges: !isUniformBorder && line
          ? {
              top: sides.top.width, right: sides.right.width, bottom: sides.bottom.width, left: sides.left.width,
              color: line,
              colors: { top: sides.top.color, right: sides.right.color, bottom: sides.bottom.color, left: sides.left.color },
            }
          : null,
        dash: isUniformBorder ? dash : null,
        radius: Math.max(px(cs.borderTopLeftRadius), px(cs.borderTopRightRadius), px(cs.borderBottomLeftRadius), px(cs.borderBottomRightRadius)) * k,
      });
      return true;
    };
    // The two stops and the angle of a `linear-gradient(ANGLEdeg, COLOR1, COLOR2)`
    // background as computed (colours resolved to rgb/rgba), or null for any
    // other image - a radial, three stops, a url().
    const gradientOf = (cs) => {
      const image = cs.backgroundImage || "";
      if (!image.startsWith("linear-gradient(")) return null;
      const colors = image.match(/rgba?\([^)]*\)/g) || [];
      if (colors.length !== 2) return null;
      const stops = colors.map(parseColor);
      if (!stops.every(Boolean)) return null;
      const angle = /linear-gradient\(\s*(-?[\d.]+)deg/.exec(image);
      return { stops, angle: angle ? parseFloat(angle[1]) : 180 };
    };
    // A native chart. The figure carries the chart AS DATA on
    // `data-deck-chart` and its drawing publishes, in `.deck-chart-facts`, the
    // facts only a laid-out chart knows (the axes it settled on, whether the
    // values fit). A chart PowerPoint can draw becomes ONE `chart`
    // primitive over the legend and the canvas: the writer rebuilds it as an
    // editable chart with a legend of its own, so the HTML legend is not
    // exported as text. One it cannot draw keeps its legend as text and
    // swatches and its drawing as a picture flagged with the kind and the
    // reason, which the export log names: a kind with no PowerPoint chart, or
    // bars read against the right-hand axis, which PowerPoint would
    // draw over the left-hand axis' bars. The title (in the figure's
    // head with the legend), the caption and the donut's centre reading are
    // ordinary text either way: the title is visited BEFORE the chart, and
    // the chart's frame is the legend's and the canvas' boxes alone, never
    // the head's, so PowerPoint's chart sits under the title's text box as the
    // panel title it is styled after. The chart is never measured off the
    // figure's own rect: inside a dashboard panel it is `display: contents`
    // and has none. A framed figure paints a card, exported as a
    // plain shape under the chart, and is the surface the chart sits on.
    const NATIVE_CHARTS = new Set(__NATIVE_CHART_KINDS__);
    const MIRRORED_CHARTS = new Set(__MIRRORED_CHART_KINDS__);
    const TWO_AXIS_PICTURE_SERIES = new Set(__TWO_AXIS_PICTURE_SERIES_KINDS__);
    const pictureReason = (chart) => {
      if (!NATIVE_CHARTS.has(chart.kind)) return chart.kind;
      const series = Array.isArray(chart.series) ? chart.series : [];
      const pictured = series.some((entry) => entry && entry.axis === "secondary" && TWO_AXIS_PICTURE_SERIES.has(entry.kind || chart.kind));
      return pictured ? __TWO_AXIS_BARS_REASON__ : null;
    };
    const probe = document.createElement("span");
    probe.setAttribute("aria-hidden", "true");
    const resolveColor = (host, expression) => {
      if (!expression) return null;
      probe.style.color = expression;
      host.appendChild(probe);
      const color = parseColor(getComputedStyle(probe).color);
      probe.remove();
      return color;
    };
    const paintOf = (host, selector, property) => {
      const el = host.querySelector(selector);
      return el ? parseColor(getComputedStyle(el)[property]) : null;
    };
    const chartOf = (figure) => {
      try { return JSON.parse(figure.dataset.deckChart); } catch (e) { return null; }
    };
    const unionRects = (rects) => {
      const drawn = rects.filter((r) => r.width > 0 && r.height > 0);
      if (!drawn.length) return null;
      const u = drawn.reduce(
        (acc, r) => ({ left: Math.min(acc.left, r.left), top: Math.min(acc.top, r.top), right: Math.max(acc.right, r.right), bottom: Math.max(acc.bottom, r.bottom) }),
        { left: Infinity, top: Infinity, right: -Infinity, bottom: -Infinity },
      );
      return { ...u, width: u.right - u.left, height: u.bottom - u.top };
    };
    const emitChart = (figure, opacity) => {
      const chart = chartOf(figure);
      const canvas = figure.querySelector(".deck-chart-canvas");
      if (!chart || !canvas) return false;
      const legend = figure.querySelector(".deck-chart-legend");
      const title = figure.querySelector(".deck-chart-head > .deck-chart-title");
      const caption = figure.querySelector("figcaption");
      const canvasRect = canvas.getBoundingClientRect();
      const ccs = getComputedStyle(canvas);
      const fcs = getComputedStyle(figure);
      const figureRect = figure.getBoundingClientRect();
      const framed = fcs.display !== "contents" && isInside(figureRect)
        && emitRect(figure, fcs, figureRect, opacity, fitOf(figure));
      if (title) visit(title);
      const reason = pictureReason(chart);
      if (reason) {
        if (legend) visit(legend);
        const picture = emitImage(canvas, canvasRect, opacity * px(ccs.opacity));
        if (picture) {
          picture.chart = chart.kind;
          picture.chart_reason = reason;
        }
        if (caption) visit(caption);
        return true;
      }
      // A butterfly's legend is the viewer's text and swatches: each
      // entry over its side and the side's total under it, a placement
      // PowerPoint's legend cannot take - so the chart gets no legend of its own.
      const ownLegend = MIRRORED_CHARTS.has(chart.kind) ? null : legend;
      if (legend && !ownLegend) visit(legend);
      // The legend box is where its ENTRIES are (the list itself spans the
      // figure), without the target's entry: PowerPoint's legend lists series
      // only, so the target is named by its line instead.
      const legendItems = ownLegend ? Array.from(ownLegend.querySelectorAll(".deck-chart-legend-item:not(.deck-chart-legend-target)")) : [];
      const legendRect = ownLegend
        ? unionRects(legendItems.map((el) => el.getBoundingClientRect())) || ownLegend.getBoundingClientRect()
        : null;
      // How many rows the legend wrapped into: a narrow panel stacks a long legend.
      const legendRows = new Set(legendItems.map((el) => Math.round(el.getBoundingClientRect().top))).size || 1;
      const hasLegend = !!legendRect && legendRect.width > 0 && legendRect.height > 0;
      const frame = hasLegend
        ? { left: Math.min(canvasRect.left, legendRect.left), top: Math.min(canvasRect.top, legendRect.top), right: Math.max(canvasRect.right, legendRect.right), bottom: Math.max(canvasRect.bottom, legendRect.bottom) }
        : { left: canvasRect.left, top: canvasRect.top, right: canvasRect.right, bottom: canvasRect.bottom };
      const frameRect = { ...frame, width: frame.right - frame.left, height: frame.bottom - frame.top };
      if (isInside(frameRect)) {
        const k = fitOf(figure);
        const facts = figure.querySelector(".deck-chart-facts");
        // The inner plot: the grid of an axis chart, the disc of a pie (the
        // union of its sectors - never its outside labels), so PowerPoint
        // draws the plot where the viewer drew it.
        const grid = canvas.querySelector(".recharts-cartesian-grid");
        const innerRect = grid
          ? grid.getBoundingClientRect()
          : unionRects(Array.from(canvas.querySelectorAll(".recharts-pie-sector")).map((el) => el.getBoundingClientRect()));
        // The chart's text colour is the category axis' (a value axis of a
        // two-unit chart wears its series' colour, read apart below). A pie
        // has no axis: its text is the chart's size (`--deck-chart-text`, its
        // values carrying their own), inked as a value written outside
        // its slice - never as one inside, which is inked from its slice.
        const axisTick = canvas.querySelector(".deck-chart-tick-category")
          || canvas.querySelector(".recharts-cartesian-axis-tick-value");
        const tick = axisTick || canvas.querySelector('text:not([data-role="segment"])');
        const tcs = tick ? getComputedStyle(tick) : ccs;
        const chartTextPx = axisTick ? 0 : px(ccs.getPropertyValue("--deck-chart-text"));
        // A pie's leader from its rim to a value outside it, end to
        // end in slide px: it runs at the slice's angle, which a box cannot say.
        const endsOf = (el) => {
          const matrix = el.getScreenCTM();
          const at = (x, y) => {
            const point = new DOMPoint(x, y).matrixTransform(matrix);
            return [point.x - base.left, point.y - base.top];
          };
          const [x1, y1] = at(el.x1.baseVal.value, el.y1.baseVal.value);
          const [x2, y2] = at(el.x2.baseVal.value, el.y2.baseVal.value);
          return { x1, y1, x2, y2, w: Math.max(1, px(getComputedStyle(el).strokeWidth) || 1) };
        };
        const legendName = legend ? legend.querySelector(".deck-chart-legend-name") : null;
        const surface = framed ? figure : canvas.closest(".deck-panel") || slide;
        const target = canvas.querySelector('.deck-chart-target:not([data-value-axis="secondary"])');
        const secondaryTarget = canvas.querySelector('.deck-chart-target[data-value-axis="secondary"]');
        // An SVG line's box has no thickness (its geometry, not its stroke),
        // so the target line is given its stroke width, centred on the line.
        const strokeBox = (el) => {
          const rect = el.getBoundingClientRect();
          const stroke = Math.max(1, px(getComputedStyle(el).strokeWidth) || 1);
          const w = Math.max(rect.width, stroke);
          const h = Math.max(rect.height, stroke);
          return { x: rect.left - base.left - (w - rect.width) / 2, y: rect.top - base.top - (h - rect.height) / 2, w, h };
        };
        // The values as the viewer wrote them, where it wrote them: a
        // stack's totals have no PowerPoint data label and are placed as text;
        // its segment values say which segments the viewer found room in, and
        // become PowerPoint's own labels on those segments only; a thin
        // segment's value written past its stack is text too, set in
        // its series' colour, its leader's legs measured below. A combo
        // line's value says where it sits against its point. The
        // anchor says which end of the text the viewer pinned: a
        // horizontal chart's total starts at its bar's end and reads rightward.
        // A fitted value past a bar says how far past its place it
        // sits (`lift`, in page px like its box).
        const labels = Array.from(canvas.querySelectorAll(".deck-chart-value"))
          .map((el) => ({
            text: el.textContent.replace(/\s+/g, " ").trim(), ...box(el.getBoundingClientRect()),
            role: el.dataset.role || null,
            series: el.dataset.series !== undefined ? Number(el.dataset.series) : null,
            category: el.dataset.category !== undefined ? Number(el.dataset.category) : null,
            rotate: el.dataset.rotate === "true",
            placement: el.dataset.placement || null,
            anchor: el.getAttribute("text-anchor") || null,
            lift: el.dataset.lift !== undefined ? Number(el.dataset.lift) * k : null,
          }))
          .filter((label) => label.text && label.w > 0 && label.h > 0);
        const factPx = (name) => {
          const value = facts ? parseFloat(facts.dataset[name]) : NaN;
          return Number.isFinite(value) ? value * k : null;
        };
        items.push({
          kind: "chart", ...box(frameRect), opacity,
          chart,
          plot: box(canvasRect),
          legend: hasLegend ? box(legendRect) : null,
          legend_rows: legendRows,
          inner: innerRect ? box(innerRect) : null,
          target_line: target ? strokeBox(target) : null,
          // A waterfall's dashed connectors, from one step's level to the next.
          connectors: Array.from(canvas.querySelectorAll(".deck-chart-connector")).map(strokeBox),
          // The legs of each leader from a thin segment to its value past the stack.
          leaders: Array.from(canvas.querySelectorAll(".deck-chart-leader"))
            .map((el) => ({ ...strokeBox(el), series: Number(el.dataset.series) })),
          secondary_target_line: secondaryTarget ? strokeBox(secondaryTarget) : null,
          labels,
          colors: (chart.series || []).map((entry) => resolveColor(canvas, entry.color)),
          slice_colors: (chart.slice_colors || []).map((color) => resolveColor(canvas, color)),
          step_colors: (chart.steps || []).map((step) => resolveColor(canvas, step.color)),
          axis: facts && facts.dataset.min !== undefined
            ? { min: parseFloat(facts.dataset.min), max: parseFloat(facts.dataset.max), step: parseFloat(facts.dataset.step) }
            : null,
          secondary_axis: facts && facts.dataset.secondaryMin !== undefined
            ? { min: parseFloat(facts.dataset.secondaryMin), max: parseFloat(facts.dataset.secondaryMax), step: parseFloat(facts.dataset.secondaryStep) }
            : null,
          values: facts ? facts.dataset.values === "true" : !!chart.values,
          label_size: factPx("labelSize"),
          label_rotate: !!facts && facts.dataset.labelRotate === "true",
          // A line's or an area's values alternate over and under their points.
          values_alternate: !!facts && facts.dataset.valuesAlternate === "true",
          // The values past the bars were fitted, and the bars gave way to them.
          values_fit: facts && facts.dataset.valuesFit ? facts.dataset.valuesFit : null,
          bar_spacing: facts && facts.dataset.barWidth !== undefined
            ? { width: parseFloat(facts.dataset.barWidth), gap: parseFloat(facts.dataset.barGap), category_gap: parseFloat(facts.dataset.categoryGap) }
            : null,
          segment_size: factPx("segmentSize"),
          annotation_size: factPx("annotationSize"),
          point_size: factPx("pointSize"),
          // Where a pie's or a donut's values are written: outside
          // with a leader, or inside the slices.
          values_placement: facts && facts.dataset.valuesPlacement ? facts.dataset.valuesPlacement : null,
          slice_leaders: Array.from(canvas.querySelectorAll(".deck-chart-slice-leader")).map(endsOf),
          style: {
            font: tcs.fontFamily, size: (chartTextPx || px(tcs.fontSize)) * k, fit: k,
            tick: tick ? parseColor(tcs.fill) : null,
            primary_tick: paintOf(canvas, ".deck-chart-tick-primary", "fill"),
            secondary_tick: paintOf(canvas, ".deck-chart-tick-secondary", "fill"),
            // A segment's value is inked from its segment, a value past a
            // stack from its series: the label paint is another's.
            label: paintOf(canvas, '.deck-chart-value:not([data-role="segment"], [data-role="annotation"])', "fill"),
            grid: paintOf(canvas, ".recharts-cartesian-grid line", "stroke"),
            axis: paintOf(canvas, ".recharts-cartesian-axis-line", "stroke"),
            target: paintOf(canvas, '.deck-chart-target:not([data-value-axis="secondary"])', "stroke"),
            secondary_target: paintOf(canvas, '.deck-chart-target[data-value-axis="secondary"]', "stroke"),
            connector: paintOf(canvas, ".deck-chart-connector", "stroke"),
            leader: paintOf(canvas, ".deck-chart-slice-leader", "stroke"),
            legend: legendName ? parseColor(getComputedStyle(legendName).color) : tick ? parseColor(tcs.fill) : null,
            legend_size: legendName ? px(getComputedStyle(legendName).fontSize) * k : null,
            background: parseColor(getComputedStyle(surface).backgroundColor),
          },
        });
      }
      for (const centre of canvas.querySelectorAll(".deck-chart-center")) visit(centre);
      if (caption) visit(caption);
      return true;
    };
    // A native org chart. The figure is the layout engine's geometry drawn as
    // SVG (`blocks/OrgChart.tsx`), so it is written as what it is drawn with,
    // never as a picture: every seat ONE group of native shapes (its card a
    // rounded rectangle, its circle an oval of its size, its tile, its badge
    // and chips, its words as text), every line connectors (`data-points`, the
    // engine's own polylines), the whole chart one group, the zones and the
    // legend in it. A photo or an icon is a picture captured alone on a
    // transparent page. Positions are the page's, mapped through each
    // element's own transform: no second layout.
    let orgSeq = 0;
    const emitOrgChart = (figure, opacity) => {
      const svg = figure.querySelector("svg.deck-org-svg");
      if (!svg) return false;
      const chart = `o${index}-${orgSeq++}`;
      const scaleOf = (el) => {
        const m = el.getScreenCTM();
        return m ? Math.hypot(m.a, m.b) : 1;
      };
      const toSlide = (el, x, y) => {
        const p = new DOMPoint(x, y).matrixTransform(el.getScreenCTM());
        return { x: p.x - base.left, y: p.y - base.top };
      };
      // A dashed outline as PowerPoint's preset: dots when the dash is about the stroke's width.
      const dashOf = (cs, width) => {
        if (!cs.strokeDasharray || cs.strokeDasharray === "none") return null;
        const first = px(cs.strokeDasharray.split(/[\s,]+/)[0]);
        return first <= Math.max(3.5, width * 1.5) ? "dot" : "dash";
      };
      const lineOf = (el) => {
        const cs = getComputedStyle(el);
        const color = parseColor(cs.stroke);
        const width = px(cs.strokeWidth) * scaleOf(el);
        return color && width > 0 ? { line: { ...color, width }, dash: dashOf(cs, px(cs.strokeWidth)) } : { line: null, dash: null };
      };
      const shapeItem = (el, kind, sub) => {
        const cs = getComputedStyle(el);
        const r = el.getBoundingClientRect();
        if (!isInside(r)) return;
        const { line, dash } = lineOf(el);
        const fill = parseColor(cs.fill);
        if (!fill && !line) return;
        const radius = kind === "rect" ? px(el.getAttribute("rx")) * scaleOf(el) : 0;
        items.push({ kind, ...box(r), opacity: opacity * px(cs.opacity), fill: fill || null, line, edges: null, radius, dash, group: chart, subgroup: sub });
      };
      const textItem = (el, sub) => {
        const cs = getComputedStyle(el);
        const text = el.textContent.replace(/\s+/g, " ").trim();
        const r = el.getBoundingClientRect();
        if (!text || !isInside(r)) return;
        const size = px(el.getAttribute("font-size") || cs.fontSize) * scaleOf(el);
        const anchor = el.getAttribute("text-anchor") || "start";
        const weight = parseInt(el.getAttribute("font-weight") || cs.fontWeight, 10) || 400;
        items.push({
          kind: "text", ...box(r), opacity: opacity * px(cs.opacity), group: chart, subgroup: sub,
          runs: [{ text, font: cs.fontFamily, size, weight, italic: false, color: parseColor(cs.fill), stroke: null, underline: false, mono: false }],
          align: anchor === "middle" ? "center" : anchor === "end" ? "right" : "left",
          lineHeight: size * 1.2, padding: { top: 0, right: 0, bottom: 0, left: 0 }, valign: "middle", letterSpacing: 0,
        });
      };
      const pictureItem = (el, sub) => {
        const item = emitImage(el, el.getBoundingClientRect(), opacity);
        if (!item) return;
        item.backdrop = true;
        item.alpha = true;
        item.group = chart;
        item.subgroup = sub;
      };
      const connectorItems = (path) => {
        const { line, dash } = lineOf(path);
        if (!line) return;
        for (const run of (path.getAttribute("data-points") || "").split(";")) {
          const points = run.trim().split(/\s+/).filter(Boolean).map((pair) => {
            const [x, y] = pair.split(",").map(Number);
            return toSlide(path, x, y);
          });
          if (points.length >= 2) items.push({ kind: "connector", points, line, dash, opacity, group: chart, x: points[0].x, y: points[0].y, w: 0, h: 0 });
        }
      };
      // Every piece in the order the page paints it: zones, lines, seats, dotted lines, legend.
      let legendSeq = 0;
      const walk = (el, sub) => {
        for (const child of el.children) {
          const cs = getComputedStyle(child);
          if (cs.display === "none" || cs.visibility === "hidden" || px(cs.opacity) === 0) continue;
          if (child.tagName === "defs" || child.tagName === "clipPath" || child.classList.contains("deck-org-glow")) continue;
          if (child.classList.contains("deck-org-node")) {
            walk(child, `${chart}:${child.getAttribute("data-org-node")}`);
          } else if (child.classList.contains("deck-org-legend-item")) {
            walk(child, `${chart}:legend:${legendSeq++}`);
          } else if (child.classList.contains("deck-org-link")) {
            connectorItems(child);
          } else if (child.tagName === "svg" || child.tagName === "image") {
            pictureItem(child, sub);
          } else if (child.tagName === "text") {
            textItem(child, sub);
          } else if (child.tagName === "rect") {
            shapeItem(child, "rect", sub);
          } else if (child.tagName === "circle") {
            shapeItem(child, "oval", sub);
          } else if (child.tagName === "line") {
            const x1 = px(child.getAttribute("x1")), y1 = px(child.getAttribute("y1"));
            const x2 = px(child.getAttribute("x2")), y2 = px(child.getAttribute("y2"));
            const { line, dash } = lineOf(child);
            const points = [toSlide(child, x1, y1), toSlide(child, x2, y2)];
            if (line) items.push({ kind: "connector", points, line, dash, opacity, group: chart, subgroup: sub, x: points[0].x, y: points[0].y, w: 0, h: 0 });
          } else if (child.tagName === "g") {
            walk(child, sub);
          }
        }
      };
      walk(svg, undefined);
      const caption = figure.querySelector(":scope > figcaption");
      if (caption) visit(caption);
      return true;
    };
    // A native architecture is ONE picture of its drawing, except its zones'
    // chips: each is written as a rounded rectangle and its text where
    // the page draws it, one group per zone, and lifted out of the picture
    // (`data-export-lifted`, hidden while the capture runs) so the row stays
    // editable and is never drawn twice. A chip's tint is composited over the
    // zone's panel first: the writer blends a translucent fill against the
    // slide alone. A drawing with no zone chips takes the generic path.
    const over = (top, under) => {
      if (!top || !under || top.a >= 1) return top;
      const a = top.a + under.a * (1 - top.a);
      const mix = (t, u) => Math.round((t * top.a + u * under.a * (1 - top.a)) / a);
      return { r: mix(top.r, under.r), g: mix(top.g, under.g), b: mix(top.b, under.b), a };
    };
    let archSeq = 0;
    const emitArchitecture = (figure, opacity) => {
      const svg = figure.querySelector("svg.deck-architecture-svg");
      const shown = (el) => {
        const ecs = getComputedStyle(el);
        return ecs.display !== "none" && ecs.visibility !== "hidden" && px(ecs.opacity) !== 0;
      };
      const chips = svg ? Array.from(svg.querySelectorAll(".deck-arch-zone-chip")).filter(shown) : [];
      if (!chips.length) return false;
      const picture = emitImage(svg, svg.getBoundingClientRect(), opacity * px(getComputedStyle(svg).opacity));
      if (picture) picture.lifted = true;
      const figureId = `a${index}-${archSeq++}`;
      const zones = Array.from(svg.querySelectorAll(".deck-arch-zone"));
      chips.forEach((chip, position) => {
        chip.setAttribute("data-export-lifted", "");
        const pill = chip.querySelector("rect");
        const label = chip.querySelector("text");
        if (!pill || !label) return;
        const r = pill.getBoundingClientRect();
        if (!isInside(r)) return;
        const matrix = pill.getScreenCTM();
        const scale = matrix ? Math.hypot(matrix.a, matrix.b) : 1;
        const zone = chip.closest(".deck-arch-zone");
        const panel = zone && zone.querySelector(":scope > .deck-arch-zone-panel");
        const under = panel ? parseColor(getComputedStyle(panel).fill) : null;
        const group = `${figureId}:z${zones.indexOf(zone)}`;
        const subgroup = `${group}:${position}`;
        const pcs = getComputedStyle(pill);
        const stroke = parseColor(pcs.stroke);
        const strokeWidth = px(pcs.strokeWidth) * scale;
        const chipOpacity = opacity * px(getComputedStyle(chip).opacity);
        items.push({
          kind: "rect", ...box(r), opacity: chipOpacity, group, subgroup,
          fill: over(parseColor(pcs.fill), under),
          line: stroke && strokeWidth > 0 ? { ...over(stroke, under), width: strokeWidth } : null,
          edges: null, radius: px(pill.getAttribute("rx")) * scale,
        });
        const size = px(label.getAttribute("font-size") || getComputedStyle(label).fontSize) * scale;
        const runs = Array.from(label.querySelectorAll("tspan")).map((tspan) => {
          const tcs = getComputedStyle(tspan);
          return {
            text: tspan.textContent.replace(/\s+/g, " "), font: tcs.fontFamily, size,
            weight: parseInt(tcs.fontWeight, 10) || 400, italic: tcs.fontStyle === "italic",
            color: parseColor(tcs.fill), stroke: null, underline: false, mono: /mono/i.test(tcs.fontFamily),
          };
        }).filter((run) => run.text);
        if (runs.length) runs[0].text = runs[0].text.trimStart();
        // The text frame is the pill, centred both ways: where the page centres its line.
        items.push({
          kind: "text", ...box(r), opacity: chipOpacity, group, subgroup, runs,
          align: "center", valign: "middle", lineHeight: size * 1.2,
          padding: { top: 0, right: 0, bottom: 0, left: 0 }, letterSpacing: 0,
        });
      });
      const caption = figure.querySelector(":scope > figcaption");
      if (caption) visit(caption);
      return true;
    };
    // A pseudo-element's stand-in, written for its host: a fill or a border
    // is a shape, a gradient (a title's rule, a card's beam and bloom) a
    // picture captured alone on a transparent page (`alpha`), so it lays over
    // the host's own fill with no text baked in.
    const emitStandIn = (host, { span }, k) => {
      const cs = getComputedStyle(span);
      const rect = span.getBoundingClientRect();
      const opacity = px(cs.opacity);
      const before = items.length;
      if (cs.backgroundImage && cs.backgroundImage !== "none") {
        const item = emitImage(span, rect, opacity);
        if (item) {
          item.backdrop = true;
          item.alpha = true;
        }
      } else if (isInside(rect)) {
        emitRect(span, cs, rect, opacity, k);
      }
      for (const item of items.slice(before)) item.group = groupOf(host);
    };
    const emitStandIns = (host, behind, k) => {
      for (const standIn of standIns.get(host) || []) {
        if (standIn.behind === behind) emitStandIn(host, standIn, k);
      }
    };
    const visit = (el) => {
      if (el.tagName === "SCRIPT" || el.tagName === "STYLE" || el.tagName === "NOSCRIPT") return;
      const cs = getComputedStyle(el);
      if (cs.display === "none" || cs.visibility === "hidden" || px(cs.opacity) === 0) return;
      if (el.dataset && el.dataset.deckChart !== undefined && emitChart(el, px(cs.opacity))) return;
      if (el.dataset && el.dataset.deckOrgChart !== undefined && emitOrgChart(el, px(cs.opacity))) return;
      if (el.classList && el.classList.contains("deck-architecture") && emitArchitecture(el, px(cs.opacity))) return;
      const rect = el.getBoundingClientRect();
      const inside = isInside(rect);
      const opacity = px(cs.opacity);
      const k = fitOf(el);
      // `data-export-gradient` (a formula's result card): a box whose fill
      // is a two-stop linear gradient is ONE native shape with a gradient
      // fill, its text visited as text - never a picture with the words
      // baked in, which is what a gradient background reads as below.
      if (el.dataset && el.dataset.exportGradient !== undefined && inside) {
        const gradient = gradientOf(cs);
        if (gradient) {
          items.push({
            kind: "rect", ...box(rect), opacity, group: groupOf(el), fill: null, line: null, edges: null, gradient,
            radius: Math.max(px(cs.borderTopLeftRadius), px(cs.borderTopRightRadius)) * k,
          });
          emitStandIns(el, true, k);
          visitContent(el, cs, rect, inside, opacity, k);
          emitStandIns(el, false, k);
          return;
        }
      }
      // `data-export-picture` (a logo mark): the element's box is ONE
      // picture - the cropped, inked mark - never the image under it, whose
      // own box runs past the crop. Its pseudo-elements are in that picture.
      const isImageLike =
        isAtom(el) ||
        el.hasAttribute("data-export-picture") ||
        el.hasAttribute("data-export-video") ||
        (cs.backgroundImage && cs.backgroundImage !== "none") ||
        (cs.webkitBackgroundClip === "text" && el.textContent.trim());
      if (isImageLike) {
        emitImage(el, rect, opacity);
        return;
      }
      if (inside && el !== slide) emitRect(el, cs, rect, opacity, k);
      emitStandIns(el, true, k);
      visitContent(el, cs, rect, inside, opacity, k);
      emitStandIns(el, false, k);
    };
    const visitContent = (el, cs, rect, inside, opacity, k) => {
      if (inside && el !== slide) {
        const pushText = (allRuns, frame, padding, valign) => {
          // A run with neither a colour nor an outline is transparent text:
          // invisible, so not exported.
          const runs = allRuns.filter((run) => run.color || run.stroke || run.text === "\n");
          if (!runs.some((run) => run.text.trim()) || !frame || frame.w <= 0 || frame.h <= 0) return;
          const item = {
            kind: "text", ...frame, opacity, runs, group: groupOf(el),
            align: cs.textAlign, lineHeight: (cs.lineHeight === "normal" ? px(cs.fontSize) * 1.2 : px(cs.lineHeight)) * k,
            padding, valign,
            letterSpacing: cs.letterSpacing === "normal" ? 0 : px(cs.letterSpacing) * k,
          };
          // Text set on its side (`writing-mode: vertical-*`, a vertical axis
          // title), turned again by a rotate transform: the measured box is
          // the turned one, so the reading angle travels with it (`rotate`,
          // degrees clockwise) and the writer lays the box out upright and
          // turns the shape.
          if (/^(vertical|sideways)/.test(cs.writingMode || "")) {
            const m = /^matrix\(([^,]+),\s*([^,]+)/.exec(cs.transform || "");
            const turn = m ? Math.round((Math.atan2(parseFloat(m[2]), parseFloat(m[1])) * 180) / Math.PI) : 0;
            const base = cs.writingMode === "sideways-lr" ? 270 : 90;
            item.rotate = (((base + turn) % 360) + 360) % 360;
          }
          items.push(item);
        };
        const noPadding = { top: 0, right: 0, bottom: 0, left: 0 };
        if (textLike(el) && el.textContent.trim()) {
          const atoms = [];
          collectAtoms(el, atoms);
          for (const atom of atoms) {
            const acs = getComputedStyle(atom);
            if (acs.display === "none" || acs.visibility === "hidden" || px(acs.opacity) === 0) continue;
            emitImage(atom, atom.getBoundingClientRect(), opacity * px(acs.opacity));
          }
          const runs = [];
          collectRuns(el, baseStyle(cs, k), runs, k);
          let frame = box(rect);
          let padding = { top: px(cs.paddingTop) * k, right: px(cs.paddingRight) * k, bottom: px(cs.paddingBottom) * k, left: px(cs.paddingLeft) * k };
          let valign = cs.display.includes("flex") && cs.alignItems === "center" ? "middle" : "top";
          if (atoms.length) {
            // The icon shares the line with the text: frame the text nodes
            // alone so the picture and the text frame do not overlap.
            const union = rectUnion(textNodesOf(el));
            if (union) { frame = box(union); padding = noPadding; valign = "middle"; }
          }
          pushText(runs, frame, padding, valign);
          return;
        }
        // A box mixing bare text with block children (a KPI value next to its
        // unit tile, a heading with an inline-flex pill): frame the bare text
        // nodes on their own, then descend for the children.
        const loose = Array.from(el.childNodes).filter((node) => node.nodeType === 3 && node.textContent.trim());
        if (loose.length) {
          const runs = [];
          const style = baseStyle(cs, k);
          for (const node of loose) {
            const text = transformText(node.textContent.replace(/\s+/g, " "), style.transform);
            if (text) runs.push({ ...style, text });
          }
          const union = rectUnion(loose);
          if (union) pushText(runs, box(union), noPadding, "middle");
        }
      }
      for (const child of el.children) visit(child);
    };
    visit(slide);
    return { index, w: base.width, h: base.height, background: parseColor(slideStyle.backgroundColor), items };
  });
}
"""

EXTRACT_PRIMITIVES_JS = (
    _EXTRACT_PRIMITIVES_TEMPLATE.replace("__NATIVE_CHART_KINDS__", json.dumps(sorted(NATIVE_CHART_KINDS)))
    .replace("__MIRRORED_CHART_KINDS__", json.dumps(sorted(MIRRORED_CHART_KINDS)))
    .replace("__TWO_AXIS_PICTURE_SERIES_KINDS__", json.dumps(sorted(TWO_AXIS_PICTURE_SERIES_KINDS)))
    .replace("__TWO_AXIS_BARS_REASON__", json.dumps(TWO_AXIS_BARS_REASON))
)

#: Runs in the static render page and returns, for every ``.deck-static-slide``,
#: the boxes of its slide links (``[text](#/<slide id>)``, drawn by
#: ``inline-markup.tsx`` as ``a[data-slide-link-index]``): one box per LINE
#: box of the link (``getClientRects``, a label that wraps is two boxes), in
#: canvas px - the static page is unscaled - clipped to the slide, with the
#: 0-based index of the slide it jumps to (``target``). A web or e-mail link
#: (``a[data-link-href]``) is measured the same way with its address
#: (``href``). A link whose target is not a slide of the deck is drawn without
#: either attribute, so it is not measured. Both PPTX writers lay an invisible
#: click target over each box (``pptx_writer.add_slide_link_hotspots``); the
#: PDF needs none, Chromium writes the page's own ``#deck-slide-<n>`` anchors
#: as internal links and an address as a URI link.
SLIDE_LINK_BOXES_JS = """
() => Array.from(document.querySelectorAll(".deck-static-slide")).map((slide) => {
  const origin = slide.getBoundingClientRect();
  const boxes = [];
  for (const anchor of slide.querySelectorAll("a[data-slide-link-index], a[data-link-href]")) {
    const href = anchor.getAttribute("data-link-href");
    const target = Number.parseInt(anchor.getAttribute("data-slide-link-index") || "", 10);
    const goal = href ? { href } : Number.isInteger(target) && target >= 0 ? { target } : null;
    if (!goal) continue;
    for (const rect of anchor.getClientRects()) {
      const left = Math.max(rect.left, origin.left);
      const top = Math.max(rect.top, origin.top);
      const right = Math.min(rect.right, origin.right);
      const bottom = Math.min(rect.bottom, origin.bottom);
      if (right - left < 1 || bottom - top < 1) continue;
      boxes.push({ x: left - origin.left, y: top - origin.top, w: right - left, h: bottom - top, ...goal });
    }
  }
  return boxes;
})
"""

#: Runs in the static render page and measures, for every ``.deck-static-slide``,
#: what the audience would see go wrong — the raw observations the
#: layout audit (``presentation_layout.py``) turns into findings for the agent:
#:
#: * ``fit``      — the body was shrunk by ``fit.ts`` to keep it on the slide:
#:   ``scale`` is the ratio applied (0.8 = the content needed 25% more height);
#:   ``cut`` is what a body held at the floor (``FIT_MIN``) still loses to the
#:   host's clip, in canvas px (0 when the scaled body fits).
#: * ``size``     — a block that sets its size (``data-fit-hold``, a
#:   ``metrics_grid``) drawn below it: ``fit.ts`` holds such a block at
#:   its size and shrinks the rest of the body, and lets go
#:   (``data-fit-released`` on the host) only when the body would still
#:   overflow at the floor. One per held block of a released host, with the
#:   block's type, its ``size`` and the ratio it is drawn at (``scale``).
#: * ``clipped``  — a box that clips its content (``overflow: hidden``, the slide
#:   frame itself) whose TEXT extends past its padding edge: rows of a table
#:   that are cut, a value that does not fit its tile, a footer ellipsized. The
#:   fit host is not reported here - its cut rides the ``fit`` observation.
#: * ``nowrap``   — text that refuses to wrap (a pill, a badge) and runs past its
#:   own box, over whatever sits next to it.
#: * ``overflow`` — an element with text or an image painted past the canvas
#:   edge, which the slide's own clip hides from the room (one per slide, the
#:   worst).
#: * ``sparse``   — the other direction, ``content`` slides only: the
#:   share of the body's ROOM left EMPTY below its last block (``unused``,
#:   0-1), with the gap in canvas px (``empty``) and the slide height
#:   (``slide``). The room is the ``.deck-fit`` host (the space between the
#:   header and the footer block), never the run's own box: a
#:   ``fill`` block or a ``distribute`` run stops at a readable gap
#:   (``spread.ts`` caps its ``max-height``) and its box is no longer the
#:   room. Measured on the blocks' bottom edge - each column's blocks on the
#:   columns form, whose columns stretch - never on a scroll height. Not
#:   measured when the slide placed its run on purpose (``vertical_align``
#:   center / end WITHOUT ``distribute`` - the switch wins over the placement
#:   in the renderer, so it wins here); ``distribute`` no longer silences it,
#:   because a capped spread can leave the floor bare. Graded against
#:   ``SPARSE_ABOVE`` by the audit, which also drops it from a slide that
#:   carries an excess finding.
#: * ``logo``     - one per ``logo_grid`` / ``logo_wall``: the height
#:   of its smallest logo mark (``height``, canvas px as painted) and the
#:   share of its emptiest logo field its logos cover (``coverage``, 0-1:
#:   the widest row's span times the rows' packed height - each row's
#:   tallest cell plus one gap - over the field's box, or over the card for
#:   a grid that does not ``fill`` and ends a ``fill`` callout, flagged
#:   ``room: "callout"``). A mark is ONE
#:   picture (``data-export-picture``): the image cropped inside it is never
#:   measured on its own. An ``architecture`` node logo sized by
#:   ``logo_height`` that is painted under its step (a wordmark its column
#:   caps) is one too, with ``block: "architecture"``, the ``height`` painted
#:   and the ``step`` asked (the renderer's ``data-logo-step``), both in
#:   canvas px. And one per slide with a measurable logo: the lowest WCAG
#:   ``contrast`` ratio of a logo against the surface painted under it (the
#:   template's mark ink, or the mean of the image's opaque pixels), with the
#:   ``block`` that places it and the node's label when an architecture does.
#: * ``lines``    — the line count of a ``statement`` quote's text at the size it
#:   is drawn in, one per statement, graded against
#:   ``STATEMENT_MAX_LINES`` by the audit: a statement past it fits, but is a
#:   paragraph where one breath was meant.
#: * ``wrapped``  - a card title or a name (``card-title``, ``feature-title``,
#:   ``persona-name``, ``tier-name``, ``profile-name``) drawn on more than one
#:   line, with its line count (``lines``); graded against
#:   ``CARD_TITLE_MAX_LINES`` by the audit (advisory).
#: * ``columns``  — a ``columns`` block of four the renderer wrapped 2 x 2
#:   because its widest column's content (``need``) is wider than the column it
#:   gets side by side (``room``), both in canvas px.
#: * ``chart``    — one per chart canvas: its height as painted (``height``,
#:   canvas px, a shrunk body's charts shrunk with it), its kind, whether it
#:   fills (``fill``: ``fill`` or ``size: full``) or sits in a dashboard panel
#:   (``panel``), and its caption - or the heading right above it - to name
#:   it. Graded against ``CHART_MIN_HEIGHT_PX`` by the audit; a canvas with no
#:   room at all is 0px high, a legend and a caption over no plot.
#: * ``values``   — a ``chart`` that writes fewer of its values than its block
#:   asked for, one per such chart: its position among the slide's
#:   charts in reading order (``chart``, 1-based), its kind and shape, how many
#:   values it was asked for and wrote, and the reason and room its drawing
#:   published on ``.deck-chart-facts`` (``chart-layout.ts`` decides them).
#: * ``shrunk``   — a pie or a donut whose values are set under the size its
#:   ``label_size`` asks: the size they are set at, the size asked,
#:   the radius the disc would keep with them outside at it and the least it
#:   keeps, where they went (``placement``) and the legend's side
#:   (``chart-slices.ts`` decides them).
#:
#: * ``org_chart`` — one per org chart: the layout engine's own account of its
#:   picture (``facts``, from ``data-org-facts``: the scale the room draws it
#:   at and the smallest type it then shows, a refusal, the widest level when
#:   it is wider than the room, labels cut inside a word, team totals the tree
#:   does not add up to, planned levels with no period, how many seats are
#:   drawn and whether anything folds) and what the page PAINTED
#:   (``painted``: lines through a seat they do not join, one manager's cards
#:   at different heights, names and roles whose glyphs run past the width
#:   they were fitted to, a circle's label over another seat), the chart's
#:   position among the slide's org charts (``chart``) and its caption.
#: * ``arch_row`` — an ``architecture`` grid row of two or more framed cards
#:   (the cards whose shapes share a vertical centre): its ``row`` counted top
#:   to bottom among such rows, its ``cards`` and the ``amount`` between the
#:   tallest and the shortest card, in canvas px; ``unsized`` counts its cards
#:   with a logo and no ``logo_height``, ``tiles`` its cards with a tiled icon.
#: * ``unsettled`` — an ``architecture`` figure whose room kept changing
#:   (the slide's fit and the drawing's ratio feeding back into each other):
#:   the renderer froze it at its last room (``data-layout-unsettled``,
#:   #11580); its position among the slide's architectures (``architecture``).
#: * ``map_overlap`` — a ``positioning_map`` whose labels (item names,
#:   quadrant labels, axis end labels) are drawn over another label or a mark,
#:   counted by the renderer (``data-map-overlaps``) and measured on the
#:   drawn boxes, the larger of the two: how many (``amount``), the
#:   first names concerned (``labels``) and the map's position among the
#:   slide's maps (``map``). A frozen map is ``unsettled`` with ``map``.
#: * ``zone_chips`` — an ``architecture`` zone whose row of chips does
#:   not fit its head row even one size down: the canvas px it lacks
#:   (``amount``, the renderer's ``data-chips-overflow``), the zone's index in
#:   its block (``zone``) and its title (``label``), the figure's position
#:   among the slide's architectures (``architecture``) and the chip count.
#:
#: Cuts are measured on GLYPHS (the union of every text node's client rects,
#: accumulated on each ancestor), not on element boxes: a box's
#: ``scrollHeight`` also grows with decorative pseudo-elements deliberately
#: positioned outside it, which would report every banner as truncated.
#: Decorations flagged ``aria-hidden`` (the ghost section number that bleeds
#: past the edge by design, icon strips, arrows) are skipped for the same
#: reason; the chrome footer (and the header a template may declare),
#: aria-hidden as well, get their own explicit check because THEIR ellipsis is
#: a real cut of what the deck settings say the footer reads.
LAYOUT_AUDIT_JS = r"""
() => {
  // A couple of canvas pixels past an edge is anti-aliasing; more is a cut.
  const EDGE_TOLERANCE = 2;
  // Glyph boxes overshoot a tight line box by a few pixels; a cut needs more.
  const CLIP_TOLERANCE = 6;
  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT"]);
  const SVG_NS = "http://www.w3.org/2000/svg";
  // Scaffolding a block is BUILT of, never the block itself: a finding inside a
  // dashboard panel names the `panel` (or the table / chart it holds), one in
  // a chart's legend, canvas or centre reading names the `chart`, one in a
  // profile card's row names the `profile-line`, one in a quote's
  // credit row or mark names the `quote`, one in a logo row or a
  // callout's head or body names the logo block or the `callout`.
  const STRUCTURAL = /^deck-(fit|fit-content|slide|slide-body|slide-header|blocks|columns|columns-slide|column|static-slide|canvas|dashboard|dashboard-tiles|dashboard-panels|panel-body|chart-(head|title|canvas|legend|legend-item|legend-target|legend-name|legend-total|swatch|swatch-line|value|center|center-value|gauge|gauge-arc|gauge-value|gauge-max|facts|target|connector|leader|slice-leader|spine|tick-category|tick-primary|tick-secondary|tooltip|tooltip-title|tooltip-rows|tooltip-row|tooltip-name|tooltip-value|error|map|countries|country|country-hover|marker|tip|ramp|ramp-bar|ramp-value|classes)|profile-(head|identity|photo|portrait|initials|lines|line-text|tile|logo)|logo-(field|row|cell|mark|ink|wall-groups|group|group-head|group-icon)|callout-(head|body)|quote-(rule|glyph|open|close|credit|author|role|org|photo|portrait|logo|logo-image|figures)|org-(stage|svg|detail|refused))$/;
  // The one-line run of a `profiles` line: ellipsized by design, its
  // cut is measured explicitly below (scrollWidth, like the chrome bars), so
  // the generic clip pass skips it instead of reporting the same cut twice.
  const PROFILE_LINE_TEXT = "deck-profile-line-text";
  // The person's name and title are one line each too, measured the same way.
  const PROFILE_ONE_LINE = [[PROFILE_LINE_TEXT, "profile-line"], ["deck-profile-name", "profile-name"], ["deck-profile-title", "profile-title"]];
  const profileOneLine = (el) => PROFILE_ONE_LINE.some(([cls]) => el.classList.contains(cls));
  // The labels a card is named by, read on one line (`wrapped`).
  const WRAPPED_TITLE_SELECTOR = ".deck-card-title, .deck-feature-title, .deck-persona-name, .deck-tier-name, .deck-profile-name";
  const excerpt = (text) => (text || "").replace(/\s+/g, " ").trim().slice(0, 80);
  const hidden = (cs) => cs.display === "none" || cs.visibility === "hidden" || parseFloat(cs.opacity) === 0;
  // Name the element by the nearest deck block it belongs to ("callout",
  // "card", "table-wrap"...), which is what the agent can find in its spec.
  const blockOf = (el) => {
    for (let node = el; node && node.classList; node = node.parentElement) {
      if (node.classList.contains("deck-slide")) return "slide";
      const cls = Array.from(node.classList).find((c) => c.startsWith("deck-") && !STRUCTURAL.test(c));
      if (cls) return cls.slice(5);
    }
    return el.tagName.toLowerCase();
  };
  const range = document.createRange();
  const slides = Array.from(document.querySelectorAll(".deck-static-slide"));
  return slides.map((slide, index) => {
    const base = slide.getBoundingClientRect();
    const frame = slide.querySelector(".deck-slide");
    const findings = [];
    // Glyph extents, accumulated on every ancestor up to the slide. Decorative
    // text (the ghost section number, aria-hidden by design) is left out here
    // too, or its glyphs past the frame's edge would read as the frame's cut.
    const extents = new Map();
    const walker = document.createTreeWalker(slide, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (!node.textContent.trim() || !node.parentElement) continue;
      if (node.parentElement.closest("[aria-hidden='true']")) continue;
      if (getComputedStyle(node.parentElement).visibility === "hidden") continue;
      range.selectNodeContents(node);
      for (const r of range.getClientRects()) {
        if (r.width <= 0 || r.height <= 0) continue;
        for (let el = node.parentElement; el; el = el.parentElement) {
          const ext = extents.get(el);
          if (ext) {
            ext.left = Math.min(ext.left, r.left); ext.top = Math.min(ext.top, r.top);
            ext.right = Math.max(ext.right, r.right); ext.bottom = Math.max(ext.bottom, r.bottom);
          } else {
            extents.set(el, { left: r.left, top: r.top, right: r.right, bottom: r.bottom });
          }
          if (el === slide) break;
        }
      }
    }
    // The shrunk bodies. `fit.ts` stops at its floor and the host clips the
    // rest, so the cut below the host's padding edge is reported WITH the
    // ratio: one cause, one observation (the host is skipped in the pass below).
    for (const host of slide.querySelectorAll(".deck-fit[data-fit]")) {
      const scale = parseFloat(host.dataset.fit);
      if (!(scale > 0 && scale < 1)) continue;
      const ext = extents.get(host);
      const hr = host.getBoundingClientRect();
      const bottom = hr.bottom - (parseFloat(getComputedStyle(host).borderBottomWidth) || 0);
      const cut = ext ? ext.bottom - bottom : 0;
      findings.push({ kind: "fit", scale, cut: cut > CLIP_TOLERANCE ? Math.round(cut) : 0 });
      // A block that sets its size is held at it while the rest shrinks;
      // a host that had to let go draws it at the body's ratio.
      if (!host.hasAttribute("data-fit-released")) continue;
      for (const held of host.querySelectorAll("[data-fit-hold]")) {
        findings.push({ kind: "size", block: held.dataset.fitHold, size: held.dataset.size || "", scale });
      }
    }
    // The first text node of `el` painted past the box's clipping edges.
    const cutText = (el, bottom, right) => {
      const inner = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
      for (let node = inner.nextNode(); node; node = inner.nextNode()) {
        if (!node.textContent.trim() || !node.parentElement) continue;
        if (node.parentElement.closest("[aria-hidden='true']")) continue;
        range.selectNodeContents(node);
        for (const r of range.getClientRects()) {
          if (r.bottom - bottom > CLIP_TOLERANCE || r.right - right > CLIP_TOLERANCE) return node.textContent;
        }
      }
      return el.textContent;
    };
    let worst = null;
    for (const el of slide.querySelectorAll("*")) {
      if (SKIP.has(el.tagName) || el.closest("[aria-hidden='true']")) continue;
      // A logo mark is ONE picture: its image runs past the crop by
      // the file's transparent padding, which is never painted.
      const picture = el.hasAttribute("data-export-picture");
      if (!picture && el.closest("[data-export-picture]")) continue;
      const cs = getComputedStyle(el);
      if (hidden(cs)) continue;
      const r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) continue;
      const ext = extents.get(el);
      const visual = !!ext || picture || el.tagName === "IMG" || el.namespaceURI === SVG_NS;
      if (visual) {
        const over = { bottom: r.bottom - base.bottom, top: base.top - r.top, right: r.right - base.right, left: base.left - r.left };
        const side = Object.keys(over).reduce((a, b) => (over[a] >= over[b] ? a : b));
        if (over[side] > EDGE_TOLERANCE && (!worst || over[side] > worst.amount)) {
          worst = { kind: "overflow", side, amount: Math.round(over[side]), block: blockOf(el), text: excerpt(el.textContent) };
        }
      }
      // A fit host's cut is already on its `fit` observation above; a profile
      // line's, name's or title's ellipsis is measured on its own below.
      if (!ext || el.classList.contains("deck-fit") || profileOneLine(el)) continue;
      const clipsY = cs.overflowY === "hidden" || cs.overflowY === "clip";
      const clipsX = cs.overflowX === "hidden" || cs.overflowX === "clip";
      if (clipsY || clipsX) {
        const bottom = r.bottom - (parseFloat(cs.borderBottomWidth) || 0);
        const right = r.right - (parseFloat(cs.borderRightWidth) || 0);
        const cutY = clipsY ? ext.bottom - bottom : 0;
        const cutX = clipsX ? ext.right - right : 0;
        if (cutY > CLIP_TOLERANCE || cutX > CLIP_TOLERANCE) {
          findings.push({
            kind: "clipped", side: cutY >= cutX ? "bottom" : "right", amount: Math.round(Math.max(cutY, cutX)),
            block: blockOf(el), text: excerpt(cutText(el, bottom, right)),
          });
        }
      } else if (cs.whiteSpace === "nowrap" && ext.right - r.right > EDGE_TOLERANCE) {
        findings.push({ kind: "nowrap", side: "right", amount: Math.round(ext.right - r.right), block: blockOf(el), text: excerpt(el.textContent) });
      }
    }
    if (worst) findings.push(worst);
    // The footer, and the header a template may declare: both are one
    // ellipsized line, so a cut is a real loss of what the deck settings say.
    for (const [selector, block] of [[".deck-chrome-footer", "chrome-footer"], [".deck-chrome-header", "chrome-header"]]) {
      const bar = slide.querySelector(selector);
      if (bar && bar.scrollWidth - bar.clientWidth > EDGE_TOLERANCE) {
        findings.push({ kind: "clipped", side: "right", amount: bar.scrollWidth - bar.clientWidth, block, text: excerpt(bar.textContent) });
      }
    }
    // A `profiles` line is ONE line by design: the run shrinks a type
    // step, then the ellipsis cuts it - and a cut fact is a fact the room does
    // not read, so every truncated line is a finding on its `profile-line`.
    // The name and the title follow the same rule: a card's identity is one
    // line each, so a cut name or role is named on `profile-name` / `profile-title`.
    for (const [cls, block] of PROFILE_ONE_LINE) {
      for (const run of slide.querySelectorAll("." + cls)) {
        if (hidden(getComputedStyle(run))) continue;
        const cut = run.scrollWidth - run.clientWidth;
        if (cut > EDGE_TOLERANCE) {
          findings.push({ kind: "clipped", side: "right", amount: cut, block, text: excerpt(run.textContent) });
        }
      }
    }
    // Logos, one observation per logo block: the height of its
    // smallest mark as the room sees it, and how much of its emptiest logo
    // field (a grid's, a wall card's) its logos cover - the widest row's span
    // times the height of the rows packed together (each row's pitch: its
    // tallest cell and one gap), so rows spread thin over a tall box read as
    // the tiny logos lost in a big box they are. A lone logo is placed, not
    // lost: a row of one is centred by design and says nothing about the
    // width, and a field of one is not measured. Raw; the audit grades both.
    // A grid that does not `fill` hugs its own rows, so its field cannot see
    // the room around it; when it ENDS a `fill` callout, the card's height
    // under it is room those logos were given, and they are measured against
    // it. Anywhere else the room below belongs to the next block, or
    // to the slide, whose `sparse` measure already reads it.
    const lastShown = (parent) => {
      let last = null;
      for (const child of parent.children) if (!hidden(getComputedStyle(child))) last = child;
      return last;
    };
    const calloutRoom = (root) => {
      if (!root.classList.contains("deck-logo-grid") || root.getAttribute("data-fill") === "true") return null;
      const blocks = root.parentElement;
      if (!blocks || !blocks.classList.contains("deck-blocks") || lastShown(blocks) !== root) return null;
      const body = blocks.parentElement;
      if (!body || !body.classList.contains("deck-callout-body") || lastShown(body) !== blocks) return null;
      const callout = body.parentElement;
      if (!callout || !callout.classList.contains("deck-callout") || callout.getAttribute("data-fill") !== "true") return null;
      return { bottom: body.getBoundingClientRect().bottom - (parseFloat(getComputedStyle(body).paddingBottom) || 0) };
    };
    for (const root of slide.querySelectorAll(".deck-logo-grid, .deck-logo-wall")) {
      if (hidden(getComputedStyle(root))) continue;
      const room = calloutRoom(root);
      const rootRect = root.getBoundingClientRect();
      let smallest = null;
      let coverage = null;
      for (const field of root.querySelectorAll(".deck-logo-field")) {
        const fr = field.getBoundingClientRect();
        if (fr.width <= 0 || fr.height <= 0) continue;
        const height = room ? Math.max(fr.height, room.bottom - fr.top - (rootRect.bottom - fr.bottom)) : fr.height;
        let span = 0;
        let packed = 0;
        let rows = 0;
        let logos = 0;
        for (const row of field.querySelectorAll(":scope > .deck-logo-row")) {
          let left = Infinity, right = -Infinity, tallest = 0, cells = 0;
          for (const cell of row.querySelectorAll(":scope > .deck-logo-cell")) {
            const cr = cell.getBoundingClientRect();
            if (cr.width <= 0 || cr.height <= 0) continue;
            left = Math.min(left, cr.left);
            right = Math.max(right, cr.right);
            tallest = Math.max(tallest, cr.height);
            cells += 1;
          }
          if (!tallest) continue;
          if (cells > 1) span = Math.max(span, right - left);
          packed += tallest;
          rows += 1;
          logos += cells;
        }
        for (const mark of field.querySelectorAll(".deck-logo-mark")) {
          const mr = mark.getBoundingClientRect();
          if (mr.width <= 0 || mr.height <= 0) continue;
          if (!smallest || mr.height < smallest.height) {
            const img = mark.querySelector("img");
            smallest = { height: mr.height, text: img ? img.alt : "" };
          }
        }
        if (rows && logos > 1) {
          packed += rows * (parseFloat(getComputedStyle(field).rowGap) || 0);
          const width = span > 0 ? Math.min(1, span / fr.width) : 1;
          const share = width * Math.min(1, packed / height);
          coverage = coverage === null ? share : Math.min(coverage, share);
        }
      }
      if (smallest) {
        findings.push({
          kind: "logo", block: root.classList.contains("deck-logo-wall") ? "logo-wall" : "logo-grid",
          height: Math.round(smallest.height * 10) / 10, coverage: Math.round(coverage * 1000) / 1000,
          text: excerpt(smallest.text), room: room ? "callout" : undefined,
        });
      }
    }
    // An architecture node logo sized by `logo_height` and painted under its
    // step: the column caps a wide wordmark so the drawing keeps its labels
    // readable. The smallest such mark per figure, against its own step.
    // The height is the one the renderer drew (`data-logo-drawn`, drawing
    // units) times the scale the mark is painted at on the canvas, never the
    // group's box: the mark is cropped to its ink by a nested <svg> viewBox,
    // and a bounding box ignores that crop, so it reads the whole file's
    // height (a wordmark on a tall transparent margin then never reads as
    // short). The scale is the mark's screen CTM: it carries the viewBox's
    // `meet` (the smaller of the two axis ratios) and any fit transform,
    // where the figure's width over its viewBox width overstates it when the
    // svg box is wider than the drawing and lifts a few px of shortfall over
    // the threshold. Short means drawn under the step by half a pixel or
    // more: the finding reports the rounded height, so 53 under 56 is named.
    for (const figure of slide.querySelectorAll(".deck-architecture")) {
      if (hidden(getComputedStyle(figure))) continue;
      let short = null;
      for (const mark of figure.querySelectorAll("[data-logo-step]")) {
        const step = Number(mark.dataset.logoStep) || 0;
        const ctm = typeof mark.getScreenCTM === "function" ? mark.getScreenCTM() : null;
        let scale = ctm ? Math.hypot(ctm.b, ctm.d) : 0;
        if (!(scale > 0)) {
          const svg = mark.ownerSVGElement;
          const box = svg && svg.viewBox && svg.viewBox.baseVal;
          const sr = svg ? svg.getBoundingClientRect() : null;
          scale = box && box.width > 0 && box.height > 0 && sr && sr.width > 0
            ? Math.min(sr.width / box.width, sr.height / box.height) : 0;
        }
        const drawn = Number(mark.dataset.logoDrawn) || 0;
        const height = drawn > 0 && scale > 0 ? drawn * scale : mark.getBoundingClientRect().height;
        if (!(step > 0) || height <= 0 || height >= step - 0.5) continue;
        if (!short || height / step < short.height / short.step) {
          const node = mark.closest(".deck-arch-node");
          short = { height, step, text: node ? node.getAttribute("aria-label") || "" : "" };
        }
      }
      if (short) {
        findings.push({
          kind: "logo", block: "architecture", height: Math.round(short.height * 10) / 10,
          step: short.step, text: excerpt(short.text),
        });
      }
    }
    // Logo contrast: a mark painted in nearly the colour of what is under it
    // (a white one-colour mark on a pale light slide) is there and unseen.
    // The colour painted is the ink for a mark drawn in the template's mark
    // ink (`data-brand-ink`: the box's fill or background), else the mean of
    // the image's opaque pixels read off a small canvas (a file the browser
    // will not let the page read, or a mark tinted by a CSS filter, is not
    // measured). The surface is the node's or zone's shape fill in an
    // architecture, else the nearest painted ancestor, translucent layers
    // composited down to the first opaque one. The lowest WCAG ratio of the
    // slide is reported; the audit grades it against `LOGO_MIN_CONTRAST`.
    {
      const probe = document.createElement("canvas");
      probe.width = 1; probe.height = 1;
      const pctx = probe.getContext("2d", { willReadFrequently: true });
      const rgba = (value) => {
        // A paint server (`url(#gradient)`) is no single colour: not measured.
        if (!pctx || !value || value === "none" || value === "transparent" || value.startsWith("url(")) return null;
        pctx.clearRect(0, 0, 1, 1);
        pctx.fillStyle = "#010203";
        pctx.fillStyle = value;
        pctx.fillRect(0, 0, 1, 1);
        const [r, g, b, a] = pctx.getImageData(0, 0, 1, 1).data;
        return a > 0 ? { r, g, b, a: a / 255 } : null;
      };
      const over = (top, under) => ({
        r: top.r * top.a + under.r * (1 - top.a), g: top.g * top.a + under.g * (1 - top.a),
        b: top.b * top.a + under.b * (1 - top.a), a: 1,
      });
      const flatten = (layers) => {
        if (!layers.length) return null;
        let out = layers[layers.length - 1];
        for (let i = layers.length - 2; i >= 0; i -= 1) out = over(layers[i], out);
        return out.a >= 0.99 ? out : null;
      };
      const surfaceOf = (el) => {
        const layers = [];
        const shapes = [];
        const node = el.closest(".deck-arch-node");
        if (node) shapes.push(node.querySelector(".deck-arch-node-shape"));
        const zone = el.closest(".deck-arch-zone");
        if (zone) shapes.push(zone.querySelector(".deck-arch-zone-panel"));
        for (const shape of shapes) {
          const fill = shape ? rgba(getComputedStyle(shape).fill) : null;
          if (!fill) continue;
          layers.push(fill);
          if (fill.a >= 0.99) return flatten(layers);
        }
        let html = el.namespaceURI === SVG_NS ? el.closest("svg") : el.parentElement;
        while (html && html.namespaceURI === SVG_NS) html = html.parentElement;
        for (let up = html; up; up = up.parentElement) {
          const fill = rgba(getComputedStyle(up).backgroundColor);
          if (fill) {
            layers.push(fill);
            if (fill.a >= 0.99) return flatten(layers);
          }
          if (up === slide) break;
        }
        return null;
      };
      // Its own canvas per image: a file from another origin taints the canvas
      // it is drawn on for good, and the colour reads above share theirs.
      const meanInk = (source) => {
        try {
          const sample = document.createElement("canvas");
          sample.width = 24; sample.height = 24;
          const sctx = sample.getContext("2d", { willReadFrequently: true });
          sctx.drawImage(source, 0, 0, 24, 24);
          const data = sctx.getImageData(0, 0, 24, 24).data;
          let r = 0, g = 0, b = 0, w = 0;
          for (let i = 0; i < data.length; i += 4) {
            const a = data[i + 3] / 255;
            if (a < 0.5) continue;
            r += data[i] * a; g += data[i + 1] * a; b += data[i + 2] * a; w += a;
          }
          return w > 0 ? { r: r / w, g: g / w, b: b / w, a: 1 } : null;
        } catch {
          return null;
        }
      };
      const luminance = (c) => {
        const lin = (v) => {
          const s = v / 255;
          return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
        };
        return 0.2126 * lin(c.r) + 0.7152 * lin(c.g) + 0.0722 * lin(c.b);
      };
      const LOGO_MARKS = [
        "rect.deck-arch-logo-ink", "image.deck-arch-logo", "img[data-brand-ink='true']",
        "img.deck-row-logo-img", "img.deck-profile-logo", "img.deck-logo",
      ].join(", ");
      let faint = null;
      for (const mark of slide.querySelectorAll(LOGO_MARKS)) {
        if (mark.closest("[aria-hidden='true']")) continue;
        const cs = getComputedStyle(mark);
        if (hidden(cs)) continue;
        const r = mark.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) continue;
        const inked = mark.getAttribute("data-brand-ink") === "true";
        let ink = null;
        if (inked) ink = rgba(mark.namespaceURI === SVG_NS ? cs.fill : cs.backgroundColor);
        else if (cs.filter === "none" && !(mark.closest("[style*='--logo-filter']"))) ink = meanInk(mark);
        const surface = ink ? surfaceOf(mark) : null;
        if (!ink || !surface) continue;
        const [hi, lo] = [luminance(ink), luminance(surface)].sort((a, b) => b - a);
        const ratio = (hi + 0.05) / (lo + 0.05);
        if (!faint || ratio < faint.ratio) {
          const node = mark.closest(".deck-arch-node, .deck-arch-zone");
          faint = { ratio, block: blockOf(mark), text: node ? node.getAttribute("aria-label") || "" : "" };
        }
      }
      if (faint) {
        findings.push({
          kind: "logo", block: faint.block, contrast: Math.round(faint.ratio * 100) / 100, text: excerpt(faint.text),
        });
      }
    }
    // A `columns` block of four that could not keep them side by side:
    // the renderer measured its widest column's content wider than a quarter
    // of the block and wrapped it 2 x 2 (`columns-fit.ts`). Both widths are
    // the renderer's own, in canvas px as the body would be drawn unshrunk.
    for (const block of slide.querySelectorAll(".deck-columns[data-wrapped]")) {
      if (hidden(getComputedStyle(block))) continue;
      findings.push({
        kind: "columns", count: Number(block.dataset.count) || 0, per_row: Number(block.dataset.wrapped) || 0,
        need: Number(block.dataset.columnNeed) || 0, room: Number(block.dataset.columnRoom) || 0, block: "columns",
      });
    }
    // The values a chart was asked to write and could not: its drawing
    // counts them on `.deck-chart-facts` with the reason, and a value missing
    // from a board deck is a finding, never a silent drop. Values it had to
    // turn on their side are reported too, with the room they lacked
    // upright, and a pie's values set under the size asked, with the
    // radius the disc would have kept for them at it. A chart is named by its
    // position among the slide's charts, the way the agent reads the slide.
    // The fit its values were drawn under (`value_label_fit`)
    // says which room was tried.
    Array.from(slide.querySelectorAll(".deck-chart")).forEach((figure, position) => {
      const facts = figure.querySelector(".deck-chart-facts");
      const canvas = figure.querySelector(".deck-chart-canvas");
      if (!facts || !canvas || hidden(getComputedStyle(canvas))) return;
      const asked = Number(facts.dataset.valuesAsked) || 0;
      const written = Number(facts.dataset.valuesWritten) || 0;
      const rotated = facts.dataset.labelRotate === "true";
      const shrunk = facts.dataset.shrunkSize !== undefined;
      if (!(asked > written) && !rotated && !shrunk) return;
      let shape = {};
      try { shape = JSON.parse(figure.dataset.deckChart || "{}"); } catch (e) { shape = {}; }
      // The size the values are tried at first, when the chart names one apart
      // from its axis text: the remedies then name it, not label_size.
      // Where a pie's values went and the side its legend takes: the
      // remedies name a legend under the chart only where it stands beside it.
      const chart = {
        block: "chart", chart: position + 1, chart_kind: figure.dataset.kind || "",
        series: Array.isArray(shape.series) ? shape.series.length : 0,
        categories: Array.isArray(shape.categories) ? shape.categories.length : 0,
        value_label_size: shape.value_label_size ?? null,
        placement: facts.dataset.valuesPlacement || null,
        legend_position: figure.dataset.legendPosition || null,
      };
      if (asked > written) {
        // Segments of a stack still hidden carry what more room would
        // do - the drawing's own plan at the tallest canvas and across a full
        // row - and the series they belong to, named for the remedy.
        const named = Array.isArray(shape.series) ? shape.series : [];
        const hiddenSeries = (facts.dataset.valuesSeries || "").split(",").filter(Boolean)
          .map((index) => String((named[Number(index)] || {}).name || ""))
          .filter(Boolean);
        findings.push({
          kind: "values", ...chart, asked, written, reason: facts.dataset.valuesReason || "",
          room: Number(facts.dataset.valuesRoom) || 0, need: Number(facts.dataset.valuesNeed) || 0,
          share: Number(facts.dataset.valuesShare) || 0, reach: Number(facts.dataset.valuesReach) || 0,
          taller: facts.dataset.valuesTaller === "true", wider: facts.dataset.valuesWider === "true",
          hidden_series: hiddenSeries, horizontal: shape.horizontal === true, panel: !!figure.closest(".deck-panel"),
          fit: facts.dataset.valuesFit || "",
          // A map's countries beyond its region, by code.
          region: facts.dataset.region || "",
          countries: (facts.dataset.valuesCountries || "").split(",").filter(Boolean),
        });
      }
      if (rotated) {
        findings.push({
          kind: "rotated", ...chart, label_size: figure.dataset.labelSize || "md",
          size: Number(facts.dataset.labelSize) || 0, upright: Number(facts.dataset.uprightSize) || 0,
          room: Number(facts.dataset.uprightRoom) || 0, need: Number(facts.dataset.uprightNeed) || 0,
        });
      }
      if (shrunk) {
        findings.push({
          kind: "shrunk", ...chart, label_size: figure.dataset.labelSize || "md",
          size: Number(facts.dataset.labelSize) || 0, asked_size: Number(facts.dataset.shrunkSize) || 0,
          room: Number(facts.dataset.shrunkRoom) || 0, need: Number(facts.dataset.shrunkNeed) || 0,
        });
      }
    });
    // A statement quote is read in one breath: the line count of every
    // statement's text at the size it is drawn in - a `hero` already stepped
    // down if it had to - reported raw; the audit grades it. Layout metrics,
    // so the canvas scale and a shrunk body change nothing about the count.
    for (const quote of slide.querySelectorAll(".deck-quote[data-variant='statement']")) {
      const text = quote.querySelector(":scope > blockquote");
      if (!text) continue;
      const tcs = getComputedStyle(text);
      const lineHeight = parseFloat(tcs.lineHeight);
      if (hidden(tcs) || !(lineHeight > 0)) continue;
      findings.push({
        kind: "lines", lines: Math.round(text.clientHeight / lineHeight), size: quote.dataset.size || "md",
        stepped: quote.dataset.quoteFit === "step",
        block: "quote", text: excerpt(text.textContent),
      });
    }
    // A card title or a name is a label, read on one line: the line count of
    // each one at the size it is drawn in, reported raw; the audit grades it
    // (`wrapped`). Layout metrics, so the canvas scale changes nothing.
    for (const title of slide.querySelectorAll(WRAPPED_TITLE_SELECTOR)) {
      const tcs = getComputedStyle(title);
      const lineHeight = parseFloat(tcs.lineHeight);
      if (hidden(tcs) || !(lineHeight > 0) || !title.clientHeight) continue;
      const lines = Math.round(title.clientHeight / lineHeight);
      if (lines > 1) {
        const named = [...title.classList].find((c) => c.startsWith("deck-"));
        const block = named ? named.slice(5) : "card-title";
        findings.push({ kind: "wrapped", lines, block, text: excerpt(title.textContent) });
      }
    }
    // A slide, panel or chart title and a chart legend are read on one line
    // (`wrap_title`, `wrap_legend`). A title's lines are counted from its
    // text's line boxes - a grid row can stretch the element, never its
    // lines - and a legend's rows from its entries' tops; a legend placed
    // `right` is a column by design and is not counted. Raw; the audit grades.
    const rowsOf = (rects) => {
      const tops = [];
      for (const r of rects) {
        if (r.width <= 0 || r.height <= 0) continue;
        if (!tops.some((top) => Math.abs(top - r.top) < r.height / 2)) tops.push(r.top);
      }
      return tops.length;
    };
    const panels = Array.from(slide.querySelectorAll(".deck-panel"));
    const charts = Array.from(slide.querySelectorAll(".deck-chart"));
    const placeOf = (el) => {
      const panel = el.closest(".deck-panel");
      const figure = el.closest(".deck-chart");
      const named = panel ? panel.querySelector(":scope > .deck-panel-title") : null;
      const own = figure ? figure.querySelector(".deck-chart-head > .deck-chart-title") : null;
      return {
        panel: panel ? panels.indexOf(panel) + 1 : 0, chart: figure ? charts.indexOf(figure) + 1 : 0,
        name: excerpt((named || own || {}).textContent || ""),
      };
    };
    const oneLineTitles = [
      ...Array.from(slide.querySelectorAll(".deck-slide-header > h2"), (el) => [el, "slide"]),
      ...Array.from(slide.querySelectorAll(".deck-panel-title"), (el) => [el, "panel"]),
      ...Array.from(slide.querySelectorAll(".deck-chart-head > .deck-chart-title"), (el) => [el, "chart"]),
    ];
    for (const [title, owner] of oneLineTitles) {
      if (hidden(getComputedStyle(title)) || !title.getClientRects().length) continue;
      range.selectNodeContents(title);
      const lines = rowsOf(range.getClientRects());
      if (lines > 1) {
        const { panel, chart } = placeOf(title);
        findings.push({ kind: "wrap_title", owner, lines, panel, chart, text: excerpt(title.textContent) });
      }
    }
    for (const legend of slide.querySelectorAll(".deck-chart-legend")) {
      const figure = legend.closest(".deck-chart");
      if (!figure || figure.dataset.legendPosition === "right" || hidden(getComputedStyle(legend))) continue;
      const entries = Array.from(legend.querySelectorAll(".deck-chart-legend-item")).filter((el) => el.getClientRects().length);
      const rows = rowsOf(entries.map((el) => el.getBoundingClientRect()));
      if (rows > 1) {
        const { panel, chart, name } = placeOf(legend);
        findings.push({ kind: "wrap_legend", rows, series: entries.length, panel, chart, text: name });
      }
    }
    // Every chart canvas, at the height the room sees it (a shrunk body
    // shrinks its charts), reported raw; the audit grades it. A canvas with no
    // room at all is 0px high: a legend and a caption over no plot. Named by
    // its own title (drawn in its frame), else its caption, else the
    // heading right above it, which is how a column used to title its chart.
    for (const canvas of slide.querySelectorAll(".deck-chart-canvas")) {
      const figure = canvas.closest(".deck-chart");
      if (!figure || !canvas.getClientRects().length || hidden(getComputedStyle(canvas))) continue;
      const title = figure.querySelector(".deck-chart-head > .deck-chart-title");
      const caption = figure.querySelector(":scope > figcaption");
      const above = figure.previousElementSibling;
      const name = title && !hidden(getComputedStyle(title)) ? title.textContent
        : caption ? caption.textContent : (above && above.classList.contains("deck-heading") ? above.textContent : "");
      findings.push({
        kind: "chart", height: Math.round(canvas.getBoundingClientRect().height * 10) / 10,
        chart: figure.dataset.kind || "", fill: figure.dataset.fill === "true" || figure.dataset.size === "full",
        panel: !!figure.closest(".deck-panel"), block: "chart", text: excerpt(name),
      });
    }
    // Org charts, one observation each: the layout engine's account of its own
    // picture (`data-org-facts`: the fit, a refusal, a level wider than the
    // room, a word cut, counts that do not add up, an undated planned level)
    // and what the page actually PAINTED - a line through a seat it does not
    // join, one manager's cards at different heights, a name or a role whose
    // glyphs run past the width it was fitted to, a circle's label over
    // another seat. Measured in the drawing's own units or in client px
    // against client px, so the canvas scale and a shrunk body change
    // nothing. Raw; the audit grades.
    const segmentHits = (a, b, r) => {
      let t0 = 0;
      let t1 = 1;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const clip = (p, q) => {
        if (p === 0) return q >= 0;
        const t = q / p;
        if (p < 0) { if (t > t1) return false; if (t > t0) t0 = t; }
        else { if (t < t0) return false; if (t < t1) t1 = t; }
        return true;
      };
      return clip(-dx, a.x - r.left) && clip(dx, r.right - a.x) && clip(-dy, a.y - r.top) && clip(dy, r.bottom - a.y) && t0 <= t1;
    };
    Array.from(slide.querySelectorAll(".deck-org-chart")).forEach((figure, position) => {
      let facts = {};
      try { facts = JSON.parse(figure.dataset.orgFacts || "{}"); } catch (e) { facts = {}; }
      const painted = { crossings: [], unequal: [], overflow: [], overlaps: [] };
      const svg = figure.querySelector("svg.deck-org-svg");
      if (svg && svg.getClientRects().length && !hidden(getComputedStyle(svg))) {
        const seats = Array.from(svg.querySelectorAll(".deck-org-node")).map((g) => {
          const shape = g.querySelector(".deck-org-shape");
          const r = shape ? shape.getBoundingClientRect() : g.getBoundingClientRect();
          return { el: g, id: g.dataset.orgNode, parent: g.dataset.parent || "", shape: g.dataset.shape, left: r.left, top: r.top, right: r.right, bottom: r.bottom };
        });
        for (const path of svg.querySelectorAll(".deck-org-link")) {
          const ends = new Set([path.dataset.from, ...(path.dataset.to || "").split(" ")]);
          const matrix = path.getScreenCTM();
          if (!matrix) continue;
          const runs = (path.getAttribute("data-points") || "").split(";").map((run) => run.trim().split(/\s+/).filter(Boolean).map((pair) => {
            const [x, y] = pair.split(",").map(Number);
            return new DOMPoint(x, y).matrixTransform(matrix);
          }));
          for (const seat of seats) {
            if (ends.has(seat.id)) continue;
            const inner = { left: seat.left + 2, top: seat.top + 2, right: seat.right - 2, bottom: seat.bottom - 2 };
            if (runs.some((run) => run.slice(1).some((p, i) => segmentHits(run[i], p, inner)))) {
              painted.crossings.push(`${path.dataset.from}|${seat.id}`);
            }
          }
        }
        const byParent = new Map();
        for (const seat of seats) {
          if (seat.shape !== "card" || !seat.parent) continue;
          byParent.set(seat.parent, [...(byParent.get(seat.parent) || []), seat.bottom - seat.top]);
        }
        for (const [parent, heights] of byParent) {
          if (heights.length > 1 && Math.max(...heights) - Math.min(...heights) > 1) painted.unequal.push(parent);
        }
        for (const text of svg.querySelectorAll(".deck-org-node text[data-max-w]")) {
          const limit = parseFloat(text.dataset.maxW);
          const drawnWidth = text.getComputedTextLength();
          if (limit > 0 && drawnWidth > limit * 1.02 + 2) {
            painted.overflow.push(`${text.closest(".deck-org-node").dataset.orgNode}|${excerpt(text.textContent)}`);
          }
        }
        for (const seat of seats) {
          if (seat.shape !== "circle") continue;
          for (const text of seat.el.querySelectorAll(":scope > .deck-org-name, :scope > .deck-org-role")) {
            const r = text.getBoundingClientRect();
            const outside = r.top >= seat.bottom - 1 || r.bottom <= seat.top + 1 || r.left >= seat.right - 1 || r.right <= seat.left + 1;
            if (!outside) continue;
            for (const other of seats) {
              if (other === seat) continue;
              if (r.left < other.right - 1 && other.left < r.right - 1 && r.top < other.bottom - 1 && other.top < r.bottom - 1) {
                painted.overlaps.push(`${seat.id}|${other.id}`);
              }
            }
          }
        }
      }
      const caption = figure.querySelector(":scope > figcaption");
      findings.push({ kind: "org_chart", chart: position + 1, block: "org-chart", text: excerpt(caption ? caption.textContent : ""), facts, painted });
    });
    // An architecture zone's chips are one row on its head row, never
    // wrapped nor cut: the renderer publishes the canvas px the row lacks at
    // its smaller size (`data-chips-overflow`). One observation per such zone,
    // named by its place among the figure's zones and its title.
    Array.from(slide.querySelectorAll(".deck-architecture")).forEach((figure, position) => {
      // A figure whose room kept moving was frozen by the renderer
      // (`data-layout-unsettled`, #11580): named, never a hang.
      if (figure.dataset.layoutUnsettled === "true" && !hidden(getComputedStyle(figure))) {
        findings.push({ kind: "unsettled", block: "architecture", architecture: position + 1 });
      }
      Array.from(figure.querySelectorAll(".deck-arch-zone")).forEach((zone, index) => {
        const overflow = Number(zone.dataset.chipsOverflow) || 0;
        if (!(overflow > 0) || hidden(getComputedStyle(zone))) return;
        const title = zone.querySelector(".deck-arch-zone-title");
        findings.push({
          kind: "zone_chips", block: "architecture", architecture: position + 1, zone: index,
          label: excerpt(title ? title.textContent : ""), amount: overflow,
          chips: zone.querySelectorAll(".deck-arch-zone-chip").length,
        });
      });
    });
    // A positioning map's labels that found no free side: the renderer
    // counts them on `data-map-overlaps` (and marks each `data-overlap`).
    // The drawn boxes are measured too - every item name, quadrant label and
    // axis end label against each other and against every mark - so a
    // collision the layout did not foresee is still reported.
    Array.from(slide.querySelectorAll(".deck-positioning-map")).forEach((figure, position) => {
      if (figure.dataset.layoutUnsettled === "true" && !hidden(getComputedStyle(figure))) {
        findings.push({ kind: "unsettled", block: "positioning_map", map: position + 1 });
      }
      if (hidden(getComputedStyle(figure))) return;
      const texts = Array.from(figure.querySelectorAll(".deck-map-label, .deck-map-quadrant-label, .deck-map-end"));
      const marks = Array.from(figure.querySelectorAll(".deck-map-mark"));
      const box = (el) => el.getBoundingClientRect();
      const hit = (a, b) => Math.min(a.right, b.right) - Math.max(a.left, b.left) > 1
        && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 1;
      const name = (el) => excerpt((el.querySelector(".deck-map-name") || el).textContent);
      const crowded = new Set();
      texts.forEach((text, i) => {
        const r = box(text);
        if (!(r.width > 0 && r.height > 0)) return;
        const item = text.closest(".deck-map-item");
        const own = item ? item.querySelector(".deck-map-mark") : null;
        if (texts.some((other, j) => j !== i && hit(r, box(other)))
          || marks.some((mark) => mark !== own && hit(r, box(mark)))) crowded.add(text);
      });
      Array.from(figure.querySelectorAll('.deck-map-label[data-overlap="true"], .deck-map-quadrant-label[data-overlap="true"]'))
        .forEach((text) => crowded.add(text));
      const overlaps = Math.max(Number(figure.dataset.mapOverlaps) || 0, crowded.size);
      if (!(overlaps > 0)) return;
      const labels = Array.from(crowded).map(name).filter(Boolean).slice(0, 4);
      findings.push({ kind: "map_overlap", block: "positioning_map", map: position + 1, amount: overlaps, labels });
    });
    // The framed cards of one architecture row: the renderer centres every
    // node on its grid row, so the cards whose shapes share a vertical centre
    // (within a pixel) are one row. One observation per row of two or more
    // cards, numbered top to bottom, the spread between its tallest and
    // shortest card in canvas px.
    Array.from(slide.querySelectorAll(".deck-architecture")).forEach((figure, position) => {
      if (hidden(getComputedStyle(figure))) return;
      const rows = [];
      for (const shape of figure.querySelectorAll('.deck-arch-node[data-shape="card"] > rect.deck-arch-node-shape')) {
        const r = shape.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) continue;
        const centre = r.top + r.height / 2;
        let seen = rows.find((row) => Math.abs(row.centre - centre) <= 1);
        if (!seen) {
          seen = { centre, low: Infinity, high: 0, cards: 0, unsized: 0, tiles: 0 };
          rows.push(seen);
        }
        seen.low = Math.min(seen.low, r.height);
        seen.high = Math.max(seen.high, r.height);
        seen.cards += 1;
        // What the card carries: a tiled icon, or a logo with no `logo_height`
        // (a sized logo draws in `.deck-arch-logo-sized`), so the finding can
        // name the remedy that actually evens the row.
        const node = shape.closest(".deck-arch-node");
        if (node && node.querySelector(".deck-arch-node-tile")) seen.tiles += 1;
        else if (node && !node.querySelector(".deck-arch-logo-sized") && node.querySelector(".deck-arch-logo, .deck-arch-logo-pending")) seen.unsized += 1;
      }
      rows.sort((a, b) => a.centre - b.centre).forEach((seen, index) => {
        if (seen.cards < 2) return;
        findings.push({
          kind: "arch_row", block: "architecture", architecture: position + 1, row: index + 1,
          amount: Math.round((seen.high - seen.low) * 10) / 10, cards: seen.cards,
          unsized: seen.unsized, tiles: seen.tiles,
        });
      });
    });
    // A `fill` table or a map in a dashboard panel: the share of the panel's
    // room under the figure's top it leaves empty (a table's rows stop short
    // of the panel's floor; a map is drawn short of its canvas, or its canvas
    // short of the panel). The panel's title and a table's caption are above
    // the start and never counted. The grade is the audit's.
    Array.from(slide.querySelectorAll(".deck-panel")).forEach((panel, index) => {
      const pr = panel.getBoundingClientRect();
      const floor = pr.bottom - (parseFloat(getComputedStyle(panel).paddingBottom) || 0);
      const measure = (block, start, drawn) => {
        const sr = start && start.getBoundingClientRect();
        const dr = drawn && drawn.getBoundingClientRect();
        if (!sr || !dr || !(dr.height > 0) || !(floor - sr.top > 0)) return;
        const empty = Math.max(0, dr.top - sr.top) + Math.max(0, floor - dr.bottom);
        findings.push({ kind: "panel_fill", block, panel: index + 1, unused: empty / (floor - sr.top), empty: Math.round(empty) });
      };
      const table = panel.querySelector('.deck-table-block[data-fill="true"] .deck-table');
      if (table) measure("table", table, table);
      const canvas = panel.querySelector('.deck-chart[data-kind="map"] .deck-chart-canvas');
      if (canvas) measure("map", canvas, canvas.querySelector(".deck-chart-countries"));
    });
    // The other direction: a content body that leaves its bottom bare.
    // The ROOM is the `.deck-fit` host - it fills the body and shrinks to it,
    // so its box is always the space available (the run inside it is
    // no longer that space - a `fill` block or a `distribute` run stops at a
    // readable gap and its box ends where its cap does). Where the content
    // ENDS is the bottom edge of the blocks: the run's children on the
    // single-flow form, each column's blocks on the columns form (the columns
    // themselves stretch to the room under `distribute`) - never a
    // `scrollHeight`, which reads a stretched box and hides the gap. A slide
    // that placed its run on purpose is left alone: `vertical_align` center /
    // end moves the run off the top - unless the slide also sets `distribute`,
    // which wins over the placement in the renderer (the run is spread from
    // the top, as the schema says) and so must win here too, or a centred
    // slide that distributes could stop at its cap with the floor bare and
    // never be told. `distribute` itself silences nothing: its spread is
    // capped, so the floor it used to reach can now be bare, and that is
    // exactly what the agent must hear. The share is reported raw; the grade
    // is the audit's.
    if (frame && frame.dataset.layout === "content") {
      const host = slide.querySelector(".deck-fit-content > .deck-blocks-slide, .deck-fit-content > .deck-columns-slide");
      const room = host && host.closest(".deck-fit");
      if (host && room) {
        const rr = room.getBoundingClientRect();
        const leaves = host.classList.contains("deck-columns-slide")
          ? host.querySelectorAll(":scope > .deck-column > .deck-blocks > *")
          : host.children;
        let last = rr.top;
        let drawn = 0;
        for (const leaf of leaves) {
          const cr = leaf.getBoundingClientRect();
          if (cr.width > 0 && cr.height > 0) {
            last = Math.max(last, cr.bottom);
            drawn += 1;
          }
        }
        // Balance: where each column's blocks END (the columns form). Columns
        // that end far apart read as one finished column beside an unfinished
        // one; the audit grades the spread against the room's height.
        const ends = [];
        if (host.classList.contains("deck-columns-slide")) {
          for (const column of host.querySelectorAll(":scope > .deck-column")) {
            let end = null;
            for (const leaf of column.querySelectorAll(":scope > .deck-blocks > *")) {
              const cr = leaf.getBoundingClientRect();
              if (cr.width > 0 && cr.height > 0) end = Math.max(end ?? cr.bottom, cr.bottom);
            }
            ends.push(end === null ? null : Math.round(end - rr.top));
          }
        }
        const placed = host.dataset.distribute !== "true"
          && (host.dataset.valign === "center" || host.dataset.valign === "end");
        if (!placed && rr.height > 0) {
          const empty = Math.max(0, rr.bottom - last);
          findings.push({ kind: "sparse", unused: empty / rr.height, empty: Math.round(empty), slide: Math.round(base.height), room: Math.round(rr.height), blocks: drawn, ends });
        }
      }
    }
    return { index, slide_id: slide.dataset.slideId || null, layout: (frame && frame.dataset.layout) || null, findings };
  });
}
"""

#: The revision the static page rendered (``data-deck-revision`` on ``.deck-root``,
#: written with the slides from the SAME response of ``render-spec``). The page
#: loads the deck's current row, not the caller's snapshot: this is how a check
#: knows the deck it measured is the deck it was asked about.
RENDERED_REVISION_JS = r"""
() => {
  const root = document.querySelector(".deck-root");
  const raw = root ? root.dataset.deckRevision : "";
  const revision = Number.parseInt(raw || "", 10);
  return Number.isFinite(revision) && revision >= 0 ? revision : null;
}
"""


@dataclass(slots=True)
class RenderedDeck:
    """What one headless render produced (only the requested parts are set)."""

    slide_count: int
    pdf: bytes | None = None
    #: One PNG per slide, in deck order (``want_slide_images``) — or one per
    #: REQUESTED slide, in the order asked for, when ``slide_indices`` narrows
    #: the capture.
    slide_images: list[bytes] = field(default_factory=list)
    #: Per slide, the measured primitives (``want_primitives``).
    primitives: list[dict] = field(default_factory=list)
    #: ``data-export-id`` → PNG, for every ``image`` primitive - at
    #: ``image_scale`` device pixels per canvas pixel, so a chart PowerPoint
    #: cannot draw (a gauge, a map, bars on two value axes) is pasted at 2x
    #: like the slide pictures.
    captures: dict[str, bytes] = field(default_factory=dict)
    #: Per slide, the raw layout observations (``want_layout``).
    layout: list[dict] = field(default_factory=list)
    #: Per slide, the boxes of its slide links and the slide each jumps to
    #: (``want_slide_images`` / ``want_primitives``: :data:`SLIDE_LINK_BOXES_JS`).
    slide_links: list[list[dict]] = field(default_factory=list)
    #: The deck revision the page rendered (``want_layout`` /
    #: ``want_slide_images``): the page loads the CURRENT row, so the caller
    #: checks it against the one it holds.
    revision: int | None = None


#: The attribute the capture loop sets on a ``.deck-static-slide`` while one of
#: its hero backdrops is screenshotted. The stylesheet answers it by
#: hiding the slide's content and chrome and showing the backdrop alone
#: (``presentation.css``, "Export capture isolation"), so the picture carries
#: the wash, the watermark and the arcs - never the headline drawn over them,
#: which the PPTX writes as editable runs.
EXPORT_ISOLATE_ATTR = "data-export-isolate"

#: The attribute ``render_deck(hide_chrome=True)`` sets on every slide; the
#: stylesheet answers it by hiding the chrome ("Export capture isolation").
EXPORT_NO_CHROME_ATTR = "data-export-no-chrome"
_HIDE_CHROME_JS = r"""
(attr) => document.querySelectorAll(".deck-static-slide").forEach((slide) => slide.setAttribute(attr, ""))
"""

#: Sets / clears :data:`EXPORT_ISOLATE_ATTR` on the slide that holds the
#: element with the given ``data-export-id``. Returns whether it found it.
#: *alpha* (a pseudo-element's stand-in) also marks the element as the
#: one being captured and the page as transparent: the stylesheet then shows
#: that element alone - no other backdrop, no paper - so the picture is its
#: paint over nothing and lays over the host's own fill in the PPTX.
_ISOLATE_BACKDROP_JS = r"""
([captureId, isolate, alpha]) => {
  const el = document.querySelector(`[data-export-id="${captureId}"]`);
  const slide = el && el.closest(".deck-static-slide");
  if (!slide) return false;
  if (isolate) {
    slide.setAttribute("data-export-isolate", captureId);
    if (alpha) {
      el.setAttribute("data-export-capturing", "");
      document.documentElement.setAttribute("data-export-alpha", "");
    }
  } else {
    slide.removeAttribute("data-export-isolate");
    el.removeAttribute("data-export-capturing");
    document.documentElement.removeAttribute("data-export-alpha");
  }
  return true;
}
"""


async def _capture_backdrop(page, capture_id: str, *, alpha: bool = False) -> bytes:
    """Screenshot a hero backdrop with the slide's content hidden around it.

    An element screenshot is a crop of the page, so the text painted OVER the
    backdrop would be baked into it - and then written a second time as
    editable runs. The slide is put in its isolation state for the one shot
    (``visibility: hidden`` on everything but the backdrop; the canvas keeps
    its paper, which the backdrop's wash is meant to sit on) and restored
    right after, whatever the screenshot did. An *alpha* capture (a
    pseudo-element's stand-in: a card's light, a title's rule) drops the
    paper too and keeps the transparency, so the picture lays over whatever
    fill its host has.
    """
    isolated = await page.evaluate(_ISOLATE_BACKDROP_JS, [capture_id, True, alpha])
    options: dict[str, Any] = {"type": "png", "timeout": CAPTURE_TIMEOUT_MS}
    if alpha:
        options["omit_background"] = True
    try:
        return await page.locator(f'[data-export-id="{capture_id}"]').screenshot(**options)
    finally:
        if isolated:
            await page.evaluate(_ISOLATE_BACKDROP_JS, [capture_id, False, alpha])


#: Sets / clears ``data-export-lifting`` on the picture with the given
#: ``data-export-id``: the stylesheet then hides the pieces the extractor wrote
#: as native shapes over it (``data-export-lifted``: an architecture's zone
#: chips). Returns whether it found the picture.
_LIFT_JS = r"""
([captureId, lift]) => {
  const el = document.querySelector(`[data-export-id="${captureId}"]`);
  if (!el) return false;
  if (lift) el.setAttribute("data-export-lifting", "");
  else el.removeAttribute("data-export-lifting");
  return true;
}
"""


async def _capture_lifted(page, capture_id: str) -> bytes:
    """Screenshot a picture without the pieces written as native shapes over it.

    The PPTX lays the lifted pieces (a zone's chips) over the picture as
    editable shapes; captured with them, the picture would carry each one a
    second time under its shape. Hidden for the one shot and shown again
    right after, whatever the screenshot did.
    """
    lifted = await page.evaluate(_LIFT_JS, [capture_id, True])
    try:
        return await page.locator(f'[data-export-id="{capture_id}"]').screenshot(
            type="png", timeout=CAPTURE_TIMEOUT_MS
        )
    finally:
        if lifted:
            await page.evaluate(_LIFT_JS, [capture_id, False])


async def render_deck(
    presentation_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    aspect_ratio: str,
    want_pdf: bool = False,
    want_slide_images: bool = False,
    want_primitives: bool = False,
    want_layout: bool = False,
    image_scale: int = 1,
    slide_indices: Sequence[int] | None = None,
    hide_chrome: bool = False,
) -> RenderedDeck:
    """Render the deck (:func:`_render_deck_once`), retrying a BROWSER failure.

    A failure of the browser rather than of the deck (a launch that fails, a
    crash, the render page refusing the connection while the platform
    restarts) is retried up to :data:`RENDER_ATTEMPTS` times with the pauses of
    :data:`RENDER_RETRY_BACKOFF_S`, the render slot released in between; one
    that persists, or that no retry can cure (Chromium not installed), raises
    :class:`HeadlessBrowserUnavailableError` naming its cause.
    :class:`PresentationRenderError` (the deck's own failure) is raised at
    once, never retried.
    """
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(render_base_url()))
    failure: HeadlessBrowserUnavailableError | None = None
    for attempt in range(1, RENDER_ATTEMPTS + 1):
        try:
            return await _render_deck_once(
                presentation_id,
                user_id,
                aspect_ratio=aspect_ratio,
                want_pdf=want_pdf,
                want_slide_images=want_slide_images,
                want_primitives=want_primitives,
                want_layout=want_layout,
                image_scale=image_scale,
                slide_indices=slide_indices,
                hide_chrome=hide_chrome,
            )
        except (PresentationRenderError, ValueError):
            raise
        except Exception as exc:
            failure = classify_browser_failure(exc, origin=origin)
            logger.warning(
                "Presentation render of %s: browser failure on attempt %d/%d (%s): %s",
                presentation_id,
                attempt,
                RENDER_ATTEMPTS,
                failure.cause,
                format_exception_message(exc),
            )
            if not failure.retryable or attempt == RENDER_ATTEMPTS:
                raise HeadlessBrowserUnavailableError(
                    failure.cause, retryable=failure.retryable, attempts=attempt
                ) from exc
            await asyncio.sleep(RENDER_RETRY_BACKOFF_S[min(attempt - 1, len(RENDER_RETRY_BACKOFF_S) - 1)])
    raise AssertionError("unreachable")  # pragma: no cover - the loop returns or raises


async def _render_deck_once(
    presentation_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    aspect_ratio: str,
    want_pdf: bool = False,
    want_slide_images: bool = False,
    want_primitives: bool = False,
    want_layout: bool = False,
    image_scale: int = 1,
    slide_indices: Sequence[int] | None = None,
    hide_chrome: bool = False,
) -> RenderedDeck:
    """Load the static render page in headless Chromium and export from it.

    *hide_chrome* draws the slides without the deck's chrome - footer, logo,
    slide number, header and emblem - for a slide shown inside something
    else (a video scene): :data:`EXPORT_NO_CHROME_ATTR`.

    *slide_indices* narrows ``want_slide_images`` to those 0-based slides, in
    the order given: ``slide_images`` then holds one PNG per REQUESTED slide
    rather than one per deck slide, and the caller maps them back from the
    indices it passed. It exists for the review capture
    (``capture_presentation_slides``), which looks at three slides of a deck
    that may hold sixty — screenshotting all sixty would hold the render slot
    and the memory for images nobody looks at. ``None`` (the export path) keeps
    the whole deck, in deck order, which is what the PPTX writers index into.

    Raises :class:`PresentationRenderError` with a user-readable message when
    the page reports a failure (deck not found, an asset that never loaded) or
    never becomes ready in time; anything else (no Chromium, browser crash)
    propagates to :func:`render_deck`, which classifies and retries it.
    """
    width, height = CANVAS_SIZES.get(aspect_ratio, CANVAS_SIZES["16:9"])
    token = mint_render_token(presentation_id, user_id)
    url = static_render_url(presentation_id, token)

    result = RenderedDeck(slide_count=0)
    async with open_render_page(
        url,
        width=width,
        height=height,
        device_scale_factor=max(1, min(int(image_scale), 3)),
        signal="deck",
        error=PresentationRenderError,
    ) as page:
        slides = page.locator(".deck-static-slide")
        result.slide_count = await slides.count()
        if result.slide_count == 0:
            raise PresentationRenderError("The deck rendered no slides.")
        if hide_chrome:
            await page.evaluate(_HIDE_CHROME_JS, EXPORT_NO_CHROME_ATTR)
        if want_layout or want_slide_images:
            result.revision = await page.evaluate(RENDERED_REVISION_JS)

        if want_pdf:
            result.pdf = await _print_pdf(page, aspect_ratio)
        if want_slide_images:
            wanted = (
                [i for i in slide_indices if 0 <= i < result.slide_count]
                if slide_indices is not None
                else range(result.slide_count)
            )
            for index in wanted:
                result.slide_images.append(await slides.nth(index).screenshot(type="png"))
        if want_slide_images or want_primitives:
            result.slide_links = await page.evaluate(SLIDE_LINK_BOXES_JS)
        if want_primitives:
            result.primitives = await page.evaluate(EXTRACT_PRIMITIVES_JS)
            for slide in result.primitives:
                for item in slide.get("items", []):
                    if item.get("kind") != "image":
                        continue
                    capture_id = str(item.get("id") or "")
                    if not capture_id:
                        continue
                    try:
                        if item.get("backdrop"):
                            result.captures[capture_id] = await _capture_backdrop(
                                page, capture_id, alpha=bool(item.get("alpha"))
                            )
                            continue
                        if item.get("lifted"):
                            result.captures[capture_id] = await _capture_lifted(page, capture_id)
                            continue
                        result.captures[capture_id] = await page.locator(
                            f'[data-export-id="{capture_id}"]'
                        ).screenshot(type="png", timeout=CAPTURE_TIMEOUT_MS)
                    except Exception as exc:
                        # One un-capturable visual (0-size after layout,
                        # detached by a re-render) must not sink the deck.
                        logger.debug(
                            "Presentation export: capture %s skipped: %s",
                            capture_id,
                            format_exception_message(exc),
                        )
        if want_layout:
            result.layout = await page.evaluate(LAYOUT_AUDIT_JS)
    return result


async def _print_pdf(page: Page, aspect_ratio: str) -> bytes:
    """The vector PDF of a settled static render page: one page per slide, on the format's paper.

    Its translucent gradients (a hero's wash, a card's bloom) are rewritten as
    plain axial / radial shadings (:func:`pdf_shading.make_shadings_viewer_safe`):
    Chromium prints them as PostScript calculator functions that some PDF
    viewers paint purple (issue #10727).
    """
    from app.services.pdf_shading import make_shadings_viewer_safe

    ratio = aspect_ratio if aspect_ratio in PDF_PAGE_SIZES_IN else "16:9"
    page_width, page_height = PDF_PAGE_SIZES_IN[ratio]
    printed = await page.pdf(
        width=page_width,
        height=page_height,
        scale=PDF_PRINT_SCALES[ratio],
        print_background=True,
        margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
    )
    return await asyncio.to_thread(make_shadings_viewer_safe, printed)


async def render_public_deck_pdf(public_token: str, *, aspect_ratio: str) -> bytes:
    """The PDF of a deck as its public link shows it.

    The same page, paper and print scale as the authenticated PDF export,
    loaded from :func:`public_static_render_url`: the slides without their
    speaker notes, the template resolved as the public page resolves it.
    Holds the worker's one public render slot (:data:`MAX_CONCURRENT_PUBLIC_RENDERS`)
    before one of the shared ones. The caller checked the link and that it
    offers a download; a link turned off mid-render fails the page, which
    raises :class:`PresentationRenderError` like any page that cannot render.
    """
    width, height = CANVAS_SIZES.get(aspect_ratio, CANVAS_SIZES["16:9"])
    async with (
        _public_render_slot(),
        open_render_page(
            public_static_render_url(public_token),
            width=width,
            height=height,
            signal="deck",
            error=PresentationRenderError,
        ) as page,
    ):
        if await page.locator(".deck-static-slide").count() == 0:
            raise PresentationRenderError("The deck rendered no slides.")
        return await _print_pdf(page, aspect_ratio)


#: The ready / error flags a static render page may set on
#: ``document.documentElement.dataset`` - ``<signal>Ready`` and
#: ``<signal>Error`` - one pair per page that renders for an export.
RENDER_PAGE_SIGNALS: tuple[str, ...] = ("deck", "report", "figure", "dashboard")


@asynccontextmanager
async def open_render_page(
    url: str,
    *,
    width: int,
    height: int,
    device_scale_factor: float = 1,
    signal: str,
    error: type[Exception],
    answer: Callable[[Any], Awaitable[bool]] | None = None,
) -> AsyncIterator[Page]:
    """A static render page, loaded and settled, in the export's locked-down headless Chromium.

    The ONE browser session behind every export (the deck's here, the
    report's in ``report_export``, a video's figure in
    ``video_studio.figures``): one of the :data:`MAX_CONCURRENT_RENDERS`
    export slots for its whole life, the content-rendering launch and context
    options (``headless_render``), every request that leaves the render
    origin aborted, the page loaded within :data:`NAVIGATION_TIMEOUT_MS` and
    handed over once it sets ``<signal>Ready`` - or *error* raised with its
    ``<signal>Error`` message, or when it has set neither within
    :data:`READY_TIMEOUT_MS`. *answer* sees every request first and returns
    ``True`` for one it fulfilled itself (the figure page's data, served by
    the process that renders it).
    """
    from playwright.async_api import Route, async_playwright
    from playwright.async_api import TimeoutError as PlaywrightTimeout

    from app.services.headless_render import RENDER_CHROMIUM_ARGS, RENDER_SERVICE_WORKERS

    if signal not in RENDER_PAGE_SIGNALS:
        raise ValueError(f"unknown render page signal {signal!r}")
    parts = urlsplit(url)
    allowed_origin = f"{parts.scheme}://{parts.netloc}"

    async def _same_origin_only(route: Route) -> None:
        request_url = route.request.url
        if answer is not None and request_url.startswith(allowed_origin + "/") and await answer(route):
            return
        if (
            request_url.startswith(("data:", "blob:", "about:"))
            or request_url.startswith(allowed_origin + "/")
            or request_url == allowed_origin
        ):
            await route.continue_()
        else:
            await route.abort()

    async with _render_slot(), async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=list(RENDER_CHROMIUM_ARGS))
        try:
            context = await browser.new_context(
                viewport={"width": width, "height": height},
                device_scale_factor=device_scale_factor,
                service_workers=RENDER_SERVICE_WORKERS,
            )
            try:
                await context.route("**/*", _same_origin_only)
                page = await context.new_page()
                await page.goto(url, wait_until="load", timeout=NAVIGATION_TIMEOUT_MS)
                try:
                    await page.wait_for_function(
                        f"() => document.documentElement.dataset.{signal}Ready === 'true'"
                        f" || !!document.documentElement.dataset.{signal}Error",
                        timeout=READY_TIMEOUT_MS,
                    )
                except PlaywrightTimeout as exc:
                    raise error(f"The {signal} did not finish rendering in time.") from exc
                failure = await page.evaluate(f"() => document.documentElement.dataset.{signal}Error || ''")
                if failure:
                    raise error(str(failure))
                yield page
            finally:
                await context.close()
        finally:
            await browser.close()


# ── Export entry point ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ExportedFile:
    filename: str
    content_type: str
    data: bytes
    #: The export log: what the file could not carry as the deck has
    #: it - a chart PowerPoint has no chart type for, pasted as a picture. One
    #: sentence per event, for the person (the agent's tool result, the server
    #: log); empty when the export was faithful.
    notes: tuple[str, ...] = ()


def export_filename(title: str, export_format: str, *, fallback: str = "presentation") -> str:
    """``<slugified title>.<ext>`` — the download name and the FileAttachment name.

    *fallback* is the stem of a title with no usable character (``report``
    for a report's export).
    """
    import re
    import unicodedata

    from app.core.utils import slugify

    # ``slugify`` falls back to "agent" for a title with no usable character;
    # an export wants its own fallback, so test for one before delegating.
    ascii_title = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode("ascii")
    stem = slugify(ascii_title)[:80] if re.search(r"\w", ascii_title) else fallback
    return f"{stem}.{EXPORT_EXTENSIONS[export_format]}"


async def _load_deck_movies(
    primitives: list[dict[str, Any]],
    user_id: uuid.UUID,
    presentation_id: uuid.UUID,
    export_log: list[str],
) -> dict[str, tuple[bytes, str]]:
    """The bytes of every video block the editable PPTX embeds as a movie.

    A video block's poster is measured with the file id of its MP4
    (``data-export-video``); each one the exporting user may read through the
    deck (the render page's own rule) and that is no larger than
    ``MAX_EMBEDDED_MOVIE_BYTES`` is loaded. A larger or unreadable one keeps
    its poster picture and is named in the export log.
    """
    from sqlalchemy import select

    from app.core.database import async_session
    from app.models.file_attachment import FileAttachment
    from app.models.user import User
    from app.services.file_access import can_read_file
    from app.services.file_storage import FileNotFoundInStorageError, file_storage
    from app.services.pptx_writer import MAX_EMBEDDED_MOVIE_BYTES

    wanted: set[uuid.UUID] = set()
    for slide in primitives or []:
        for item in slide.get("items") or []:
            video = item.get("video") if isinstance(item, dict) else None
            if isinstance(video, str) and is_valid_uuid(video):
                wanted.add(uuid.UUID(video))
    if not wanted:
        return {}
    movies: dict[str, tuple[bytes, str]] = {}
    async with async_session() as db:
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if user is None:
            return {}
        rows = (await db.execute(select(FileAttachment).where(FileAttachment.id.in_(wanted)))).scalars().all()
        for row in rows:
            if not await can_read_file(row, user, db, presentation_id=presentation_id):
                continue
            if (row.size or 0) > MAX_EMBEDDED_MOVIE_BYTES:
                export_log.append(
                    f"Video {row.filename} is {row.size // (1024 * 1024)} MB, over the "
                    f"{MAX_EMBEDDED_MOVIE_BYTES // (1024 * 1024)} MB a deck embeds: kept as its poster"
                )
                continue
            try:
                data = await file_storage.download(row.s3_object_name)
            except FileNotFoundInStorageError:
                continue
            movies[str(row.id)] = (data, row.content_type or "video/mp4")
    return movies


async def export_presentation(
    *,
    presentation_id: uuid.UUID,
    user_id: uuid.UUID,
    title: str,
    spec: dict,
    export_format: str,
) -> ExportedFile:
    """Render a deck and package it in *export_format* (see ``EXPORT_FORMATS``).

    The caller has already authorized *user_id* on the deck; this only turns a
    deck into bytes. The user id is what the render token is minted for, so the
    static page renders with THAT user's visibility (template theme, assets).
    """
    if export_format not in EXPORT_FORMATS:
        raise ValueError(f"unsupported export format {export_format!r}")
    from app.services import pptx_writer

    aspect_ratio = str(spec.get("aspect_ratio") or "16:9")
    slides = spec.get("slides") or []
    notes = [(slide.get("notes") if isinstance(slide, dict) else None) for slide in slides]
    # The deck's transition plays in PowerPoint too, in both variants.
    transition = str(spec.get("transition") or "slide")
    filename = export_filename(title, export_format)
    content_type = EXPORT_CONTENT_TYPES[export_format]

    if export_format == "pdf":
        rendered = await render_deck(presentation_id, user_id, aspect_ratio=aspect_ratio, want_pdf=True)
        assert rendered.pdf is not None  # noqa: S101 - want_pdf contract
        return ExportedFile(filename, content_type, rendered.pdf)

    if export_format == "pptx":
        rendered = await render_deck(
            presentation_id,
            user_id,
            aspect_ratio=aspect_ratio,
            want_slide_images=True,
        )
        data = pptx_writer.build_image_pptx(
            rendered.slide_images,
            aspect_ratio=aspect_ratio,
            notes=notes,
            title=title,
            transition=transition,
            slide_links=rendered.slide_links,
        )
        return ExportedFile(filename, content_type, data)

    # 2x captures: the only pictures left in an editable deck are the visuals
    # that have no shape equivalent (icons, diagrams, the gauge and map charts), and
    # at 1x they were the one soft thing on a crisp slide.
    rendered = await render_deck(
        presentation_id,
        user_id,
        aspect_ratio=aspect_ratio,
        want_primitives=True,
        image_scale=NATIVE_PPTX_IMAGE_SCALE,
    )
    export_log: list[str] = []
    movies = await _load_deck_movies(rendered.primitives, user_id, presentation_id, export_log)
    data = pptx_writer.build_native_pptx(
        rendered.primitives,
        captures=rendered.captures,
        aspect_ratio=aspect_ratio,
        notes=notes,
        title=title,
        export_log=export_log,
        transition=transition,
        slide_links=rendered.slide_links,
        movies=movies,
    )
    for note in export_log:
        logger.info("Presentation export %s (%s): %s", presentation_id, export_format, note)
    return ExportedFile(filename, content_type, data, notes=tuple(export_log))
