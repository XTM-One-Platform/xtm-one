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

"""The public link of a presentation: anyone with the URL may view it.

Every other way into a deck goes through its grants (``deck_permission``), and
the deck's own address grants nothing. A public link is the one exception, on
purpose: a capability URL its managers hand to people who have no account
here - a customer, a board, a conference audience. This module owns it, and
both surfaces that change it call it: the Share dialog (``GET`` / ``POST`` /
``DELETE /presentations/{id}/public-link``) and the
``set_presentation_public_link`` built-in tool. Who may call is theirs to
check first: the deck's managers (owner, ``config`` grantee; an admin on the
REST surface), never a ``read_only`` grantee.

* **The token** is 256 bits (``secrets.token_urlsafe(32)``). A public request
  is looked up by its SHA-256 (``security.hash_token``, the invitation /
  password reset idiom), and the token is kept Fernet-encrypted (invariant 2)
  so a manager can copy the link again. One link per deck; ``regenerate``
  rewrites the token in place and the old URL stops answering at once;
  ``disable`` deletes the row.
* **The admin switch** ``presentation_public_links_enabled`` (Settings >
  Security, :func:`public_links_enabled`) is read on EVERY public request:
  off, every link answers like a token nobody issued, and none can be created.
  The rows are kept, so turning it back on restores the links nobody turned
  off.
* **What a link shows** is :func:`public_presentation_payload`: the deck as
  its audience sees it, resolved as its OWNER would read it (template palette,
  logo geometry, picture labels), whitelisted by ``PublicPresentationResponse``
  and minus the speaker notes. Nothing about who owns, edits or may open the
  deck. The files it may fetch are those :func:`public_asset_ids` lists - the
  same structural walk ``render-asset`` serves from - so a link is a
  capability over ONE deck's pictures, never a general file-read credential.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import secrets
import time
import uuid
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from cryptography.fernet import InvalidToken
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_value, encrypt_value, hash_token
from app.core.security_options import security_option
from app.models.presentation import Presentation, PresentationPublicLink

logger = logging.getLogger(__name__)

#: The web app page a public link opens (``PresentationPublic.tsx``).
PUBLIC_LINK_ROUTE = "/public/presentations"

#: ``secrets.token_urlsafe(32)``: 43 characters of the URL-safe alphabet.
TOKEN_BYTES = 32
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")

#: What one ``set_presentation_public_link`` call does.
PUBLIC_LINK_ACTIONS: tuple[str, ...] = ("enable", "regenerate", "disable")

#: The answer while the admin switch is off, for the REST surface and the tool.
PUBLIC_LINKS_OFF_DETAIL = (
    "Public links are turned off on this platform by an administrator "
    "(Settings > Access and security > Public presentation links)."
)

#: The audit ``details.public_link`` of each change.
OUTCOME_CREATED = "enabled"
OUTCOME_REGENERATED = "regenerated"
OUTCOME_KEPT = "kept"
OUTCOME_DISABLED = "disabled"
OUTCOME_PDF_ON = "pdf_download_enabled"
OUTCOME_PDF_OFF = "pdf_download_disabled"

#: What the PDF route answers while the link offers no download.
PDF_DOWNLOAD_OFF_DETAIL = "This presentation cannot be downloaded"

#: Downloads one client address may ask for per window: a room behind
#: one address downloading together, from the cache.
PUBLIC_PDF_DOWNLOADS_PER_ADDRESS = 60
#: Renders one public link may cause per window - downloads the cache could
#: not answer. A deck edited ten times in ten minutes is the most a person
#: does; a flood of renders is refused here, never queued.
PUBLIC_PDF_RENDERS_PER_LINK = 10
PUBLIC_PDF_WINDOW_SECONDS = 600
#: How long a rendered PDF answers for an unchanged deck, and how much one
#: worker keeps. A picture replaced in place under the same id is the one
#: change the fingerprint cannot see; it shows within this time.
PUBLIC_PDF_CACHE_SECONDS = 600
PUBLIC_PDF_CACHE_MAX_BYTES = 64 * 1024 * 1024
#: A larger PDF is served but not kept: it would evict every other deck.
PUBLIC_PDF_MAX_CACHED_BYTES = 16 * 1024 * 1024


def public_links_enabled() -> bool:
    """Whether public links answer and may be created (the admin switch)."""
    return bool(security_option("presentation_public_links_enabled"))


def mint_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def well_formed_token(token: object) -> bool:
    """Whether *token* has the shape :func:`mint_token` gives, checked before any lookup."""
    return isinstance(token, str) and bool(_TOKEN_RE.fullmatch(token))


def public_link_path(token: str) -> str:
    """The root-relative page address (the web app adds its base path)."""
    return f"{PUBLIC_LINK_ROUTE}/{token}"


def public_link_url(token: str) -> str:
    """The absolute address a person pastes into a message."""
    from app.services.email import platform_url

    return f"{platform_url()}{public_link_path(token)}"


def link_token(link: PresentationPublicLink) -> str | None:
    """The token of a stored link, or ``None`` when ``SECRET_KEY`` no longer decrypts it.

    The link keeps answering (it is looked up by hash), but its address can no
    longer be shown: regenerating it is the way to a link that can be copied.
    """
    try:
        return decrypt_value(link.token_enc)
    except (InvalidToken, ValueError):
        logger.warning("The public link of presentation %s can no longer be decrypted", link.presentation_id)
        return None


# ── Reads ─────────────────────────────────────────────────────────────────────


async def load_public_link(db: AsyncSession, presentation_id: uuid.UUID) -> PresentationPublicLink | None:
    result = await db.execute(
        select(PresentationPublicLink).where(PresentationPublicLink.presentation_id == presentation_id)
    )
    return result.scalar_one_or_none()


async def public_deck_ids(db: AsyncSession, presentation_ids: Iterable[uuid.UUID]) -> set[str]:
    """Which of *presentation_ids* a public link opens now: one query for a whole list.

    Empty while the admin switch is off - a kept link opens nothing, and a
    card must not call the deck public.
    """
    ids = list(presentation_ids)
    if not ids or not public_links_enabled():
        return set()
    rows = await db.execute(
        select(PresentationPublicLink.presentation_id).where(PresentationPublicLink.presentation_id.in_(ids))
    )
    return {str(deck_id) for deck_id in rows.scalars().all()}


async def resolve_public_link(
    db: AsyncSession, token: object
) -> tuple[Presentation, PresentationPublicLink] | None:
    """The deck *token* opens and its link, or ``None``: an unknown, regenerated or disabled link, or the switch off.

    Every refusal is the same ``None``, so a caller answers them alike and the
    answer never tells a dead link from one that never existed.
    """
    if not public_links_enabled() or not well_formed_token(token):
        return None
    result = await db.execute(
        select(Presentation, PresentationPublicLink)
        .join(PresentationPublicLink, PresentationPublicLink.presentation_id == Presentation.id)
        .where(PresentationPublicLink.token_hash == hash_token(str(token)))
    )
    row = result.first()
    return (row[0], row[1]) if row is not None else None


async def resolve_public_presentation(db: AsyncSession, token: object) -> Presentation | None:
    """:func:`resolve_public_link`, the deck alone."""
    resolved = await resolve_public_link(db, token)
    return resolved[0] if resolved is not None else None


# ── Writes ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PublicLinkChange:
    """What :func:`enable_public_link` did, and the link as it stands."""

    outcome: str  # OUTCOME_CREATED | OUTCOME_REGENERATED | OUTCOME_KEPT
    token: str | None
    created_at: datetime | None
    created_by_user_id: uuid.UUID | None
    #: Whether the link offers a PDF download: off on a new link, kept by a regenerate.
    allow_pdf_download: bool = False

    @property
    def changed(self) -> bool:
        return self.outcome != OUTCOME_KEPT


async def enable_public_link(
    db: AsyncSession,
    presentation_id: uuid.UUID,
    *,
    actor_id: uuid.UUID | None,
    regenerate: bool = False,
) -> PublicLinkChange:
    """Give the deck a public link, or hand back the one it has; ``regenerate`` issues a new token.

    Upserts on the unique ``presentation_id`` rather than read-then-write, so
    two managers enabling the link at once end with one link both are handed.
    The caller checked the switch and that *actor_id* may manage the deck;
    flushes nothing beyond the statements, commits nothing.
    """
    token = mint_token()
    now = datetime.now(UTC)
    fields = {
        "token_hash": hash_token(token),
        "token_enc": encrypt_value(token),
        "created_by_user_id": actor_id,
        "created_at": now,
    }
    if regenerate:
        # The option survives a new token: replacing a leaked link must not
        # silently change what its next holders are offered.
        rotated = await db.execute(
            update(PresentationPublicLink)
            .where(PresentationPublicLink.presentation_id == presentation_id)
            .values(**fields)
            .returning(PresentationPublicLink.allow_pdf_download)
        )
        kept_pdf = rotated.scalar_one_or_none()
        if kept_pdf is not None:
            return PublicLinkChange(OUTCOME_REGENERATED, token, now, actor_id, allow_pdf_download=bool(kept_pdf))
    inserted = await db.execute(
        pg_insert(PresentationPublicLink)
        .values(id=uuid.uuid4(), presentation_id=presentation_id, **fields)
        .on_conflict_do_nothing(index_elements=[PresentationPublicLink.presentation_id])
        .returning(PresentationPublicLink.id)
    )
    if inserted.scalar_one_or_none() is not None:
        # ROI of chat work (#10544): a public link is a use of the deck.
        from app.services.roi_chat_records import record_signal

        await record_signal(db, kind="presentation", deliverable_id=presentation_id, signal="public_link", at=now)
        return PublicLinkChange(OUTCOME_CREATED, token, now, actor_id)
    # Another manager's link landed first: it is the deck's link.
    existing = await load_public_link(db, presentation_id)
    if existing is None:  # pragma: no cover - disabled between the two statements
        return PublicLinkChange(OUTCOME_KEPT, None, None, None)
    return PublicLinkChange(
        OUTCOME_KEPT,
        link_token(existing),
        existing.created_at,
        existing.created_by_user_id,
        allow_pdf_download=bool(existing.allow_pdf_download),
    )


async def set_pdf_download(db: AsyncSession, presentation_id: uuid.UUID, allowed: bool) -> bool | None:
    """Let the visitors of the deck's public link download its PDF, or stop them.

    ``True`` when the option changed, ``False`` when it already was *allowed*,
    ``None`` when the deck has no public link (the option belongs to the
    link). The caller checked that the user may manage the deck - and the
    admin switch before turning it on; turning it off is never refused.
    Flushes, commits nothing.
    """
    link = await load_public_link(db, presentation_id)
    if link is None:
        return None
    if bool(link.allow_pdf_download) == allowed:
        return False
    link.allow_pdf_download = allowed
    await db.flush()
    return True


async def disable_public_link(db: AsyncSession, presentation_id: uuid.UUID) -> bool:
    """Delete the deck's public link: its URL stops answering at once. ``False`` when it had none."""
    removed = await db.execute(
        delete(PresentationPublicLink)
        .where(PresentationPublicLink.presentation_id == presentation_id)
        .returning(PresentationPublicLink.id)
    )
    return removed.scalar_one_or_none() is not None


def audit_details(outcome: str, *, via_agent: bool = False) -> dict[str, Any]:
    """The ``details`` of the one audit row a change writes; ``via: agent`` like ``share_presentation``'s."""
    details: dict[str, Any] = {"public_link": outcome, "sharing_changed": True}
    if via_agent:
        details["via"] = "agent"
    return details


def audit_summary(outcome: str, title: str) -> str:
    verb = {
        OUTCOME_CREATED: "Turned on the public link of",
        OUTCOME_REGENERATED: "Regenerated the public link of",
        OUTCOME_DISABLED: "Turned off the public link of",
        OUTCOME_PDF_ON: "Allowed the PDF download of the public link of",
        OUTCOME_PDF_OFF: "Stopped the PDF download of the public link of",
    }[outcome]
    return f"{verb} presentation '{title}'"


# ── The agent's words ─────────────────────────────────────────────────────────

_ACTION_ALIASES: dict[str, str] = {
    "enable": "enable",
    "on": "enable",
    "create": "enable",
    "get": "enable",
    "share": "enable",
    "publish": "enable",
    "regenerate": "regenerate",
    "rotate": "regenerate",
    "reset": "regenerate",
    "renew": "regenerate",
    "disable": "disable",
    "off": "disable",
    "revoke": "disable",
    "remove": "disable",
    "delete": "disable",
    "unshare": "disable",
    "unpublish": "disable",
}

#: What every result that hands out a link says, so the agent words it right.
_AUDIENCE_NOTE = (
    "Anyone with this link can view the presentation without signing in - no account, no grant. "
    "It shows the slides only: no speaker notes, comments or editing, and every later edit shows up "
    "on it. Give the user the link exactly as written; it works until it is turned off "
    "(action 'disable') or replaced (action 'regenerate')."
)


def normalize_public_link_action(value: object) -> str | None:
    """One of :data:`PUBLIC_LINK_ACTIONS` (``enable`` when unset), or ``None``."""
    if value is None or not str(value).strip():
        return "enable"
    return _ACTION_ALIASES.get(re.sub(r"[\s-]+", "_", str(value).strip().lower()))


_FLAG_TRUE = frozenset({"true", "yes", "on", "1", "allow", "allowed"})
_FLAG_FALSE = frozenset({"false", "no", "off", "0", "deny", "denied", "none"})


def normalize_download_flag(value: object, *, field: str) -> bool | None:
    """A public link's download argument as a choice, ``None`` when unset (leave the option as it is).

    The one reader of the deck's ``pdf_download`` and the video's ``download``.
    A boolean is the schema's form; ``"true"`` / ``"off"`` and the
    like are read too. Anything else raises :class:`ValueError` naming
    *field*: an access change made on a guess is the failure these tools must
    not have.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _FLAG_TRUE:
        return True
    if text in _FLAG_FALSE:
        return False
    raise ValueError(f"{field} must be true or false, not {value!r}")


def _pdf_sentence(allow_pdf_download: bool, *, changed: bool) -> str:
    if allow_pdf_download:
        lead = "Visitors can now also download it as a PDF" if changed else "Visitors can also download it as a PDF"
        return f"{lead} (the slides, without speaker notes)."
    lead = "Visitors can no longer download it" if changed else "Visitors cannot download it"
    return f"{lead}: pdf_download=true lets them download a PDF of the slides."


def format_public_link_result(
    title: str,
    outcome: str,
    token: str | None,
    *,
    allow_pdf_download: bool = False,
    pdf_changed: bool = False,
) -> str:
    """What one ``set_presentation_public_link`` call did, worded for the user."""
    if outcome == OUTCOME_DISABLED:
        return (
            f"Public link of **{title}** turned off: the link no longer opens it, for anyone. "
            "The people it is shared with on the platform keep their access."
        )
    if token is None:
        return (
            f"**{title}** has a public link, but its address can no longer be read (the platform's "
            "encryption key changed). Call again with action 'regenerate' to issue a new link."
        )
    url = public_link_url(token)
    head = {
        OUTCOME_CREATED: f"Public link turned on for **{title}**: {url}",
        OUTCOME_REGENERATED: (
            f"New public link for **{title}**: {url}\n"
            "The previous link no longer works: whoever opens it is told the presentation is not available."
        ),
        OUTCOME_KEPT: f"**{title}** already has a public link, unchanged: {url}",
    }[outcome]
    return f"{head}\n{_AUDIENCE_NOTE}\n{_pdf_sentence(allow_pdf_download, changed=pdf_changed)}"


def public_link_status_line(*, has_link: bool, allow_pdf_download: bool = False) -> str:
    """The ``Public link:`` line of ``get_presentation``; empty for a deck with none (the default)."""
    if not has_link:
        return ""
    if not public_links_enabled():
        return (
            "Public link: kept but not working - public links are turned off on this platform by an "
            "administrator, so it opens nothing until they turn them back on."
        )
    download = " and download it as a PDF" if allow_pdf_download else " (no PDF download)"
    return (
        f"Public link: on - anyone with the link can view it without signing in{download}. "
        "set_presentation_public_link returns its address (action 'enable') or turns it off ('disable')."
    )


# ── What the link shows ───────────────────────────────────────────────────────


def public_spec(spec: object) -> dict:
    """The spec an audience sees: every slide without its speaker notes."""
    if not isinstance(spec, dict):
        return {}
    slides = spec.get("slides")
    if not isinstance(slides, list):
        return dict(spec)
    return {
        **spec,
        "slides": [
            {key: value for key, value in slide.items() if key != "notes"} if isinstance(slide, dict) else slide
            for slide in slides
        ],
    }


async def public_asset_ids(db: AsyncSession, presentation: Presentation) -> frozenset[str]:
    """Every file a public viewer of *presentation* may fetch.

    The files the shown spec names, plus the logos of the template palettes
    the deck may wear - every scheme, since a slide may override the deck's
    theme - resolved as the owner reads them, like the payload.
    """
    from app.services.presentation_export import referenced_file_ids
    from app.services.presentations import resolve_template_palette

    palette = await resolve_template_palette(db, presentation.template_container_id, user_id=presentation.user_id)
    found = set(referenced_file_ids(public_spec(presentation.spec)))
    for theme in palette.themes.values():
        found |= referenced_file_ids(None, theme)
    return frozenset(found)


async def public_presentation_payload(
    db: AsyncSession,
    presentation: Presentation,
    link: PresentationPublicLink | None = None,
) -> dict:
    """The deck as a public link shows it, for ``PublicPresentationResponse``.

    ``pdf_download`` says whether *link* offers the PDF, so the page
    shows its download button only then.

    Rendered as its owner reads it - a template the owner can no longer see,
    or a deck whose owner was deleted, renders in the built-in look, the same
    answer the owner's own viewer gives - and served through a whitelist: the
    response model keeps the fields a renderer needs and drops the rest of
    ``serialize_presentation`` (owner, editors, grants, conversation,
    comments). Quote pictures keep the description they are announced by,
    never a filename.
    """
    from app.services.presentation_pictures import load_quote_picture_labels, quote_picture_ids
    from app.services.presentations import (
        load_logo_asset_meta,
        resolve_template_palette,
        serialize_presentation,
    )

    spec = public_spec(presentation.spec)
    palette = await resolve_template_palette(db, presentation.template_container_id, user_id=presentation.user_id)
    labels: dict[str, dict[str, str | None]] = {}
    if quote_picture_ids(spec):
        found = await load_quote_picture_labels(db, spec, user_id=presentation.user_id)
        labels = {
            file_id: {"description": label["description"], "filename": None}
            for file_id, label in found.items()
            if label.get("description")
        }
    payload = serialize_presentation(
        presentation,
        theme_config=palette.theme_for(presentation.theme),
        theme_configs=palette.themes,
        include_spec=True,
        supported_schemes=palette.supported_schemes,
        template_chrome=palette.chrome,
        template_design=palette.design,
        asset_meta=await load_logo_asset_meta(db, spec),
        picture_labels=labels,
        supported_heroes=palette.heroes,
        hero_defaults=palette.hero_defaults,
    )
    payload["spec"] = public_spec(payload.get("spec"))
    payload["pdf_download"] = bool(link is not None and link.allow_pdf_download)
    return payload


# ── The PDF a link may offer ──────────────────────────────────────────
#
# Anyone holding the link may ask, and a render costs a headless browser for
# seconds, so the render is the thing bounded: the PDF of an unchanged deck is
# rendered ONCE per worker and kept for PUBLIC_PDF_CACHE_SECONDS (keyed by the
# fingerprint of everything the page shows, so an edit is a new key, never a
# stale file); concurrent asks for the same deck wait for that one render;
# renders a link may cause are budgeted (PUBLIC_PDF_RENDERS_PER_LINK) and run
# in the worker's one public render slot (``presentation_export``).


@dataclass
class _CachedPdf:
    data: bytes
    expires_at: float


_pdf_cache: OrderedDict[str, _CachedPdf] = OrderedDict()
_pdf_cache_bytes = 0
_pdf_flights: dict[tuple[int, str], asyncio.Lock] = {}


def _pdf_cache_get(key: str) -> bytes | None:
    global _pdf_cache_bytes
    entry = _pdf_cache.get(key)
    if entry is None:
        return None
    if entry.expires_at <= time.monotonic():
        _pdf_cache.pop(key, None)
        _pdf_cache_bytes -= len(entry.data)
        return None
    _pdf_cache.move_to_end(key)
    return entry.data


def _pdf_cache_put(key: str, data: bytes) -> None:
    global _pdf_cache_bytes
    if len(data) > PUBLIC_PDF_MAX_CACHED_BYTES:
        return
    previous = _pdf_cache.pop(key, None)
    if previous is not None:
        _pdf_cache_bytes -= len(previous.data)
    _pdf_cache[key] = _CachedPdf(data, time.monotonic() + PUBLIC_PDF_CACHE_SECONDS)
    _pdf_cache_bytes += len(data)
    while _pdf_cache_bytes > PUBLIC_PDF_CACHE_MAX_BYTES and _pdf_cache:
        _key, evicted = _pdf_cache.popitem(last=False)
        _pdf_cache_bytes -= len(evicted.data)


def clear_pdf_cache() -> None:
    """Forget every rendered PDF of this worker (tests)."""
    global _pdf_cache_bytes
    _pdf_cache.clear()
    _pdf_cache_bytes = 0
    _pdf_flights.clear()


def _pdf_flight(key: str) -> asyncio.Lock:
    """The lock concurrent asks for one fingerprint wait on (per event loop: a lock must not cross loops)."""
    loop_key = (id(asyncio.get_running_loop()), key)
    lock = _pdf_flights.get(loop_key)
    if lock is None:
        lock = asyncio.Lock()
        _pdf_flights[loop_key] = lock
    return lock


def pdf_fingerprint(payload: dict) -> str:
    """The key of a public PDF: every field the page draws from, so any visible change is a new key."""
    shown = {key: value for key, value in payload.items() if key != "pdf_download"}
    return hashlib.sha256(json.dumps(shown, sort_keys=True, default=str).encode()).hexdigest()


@dataclass(frozen=True)
class PublicPdf:
    filename: str
    data: bytes
    #: Served from this worker's cache, no render.
    cached: bool


async def public_pdf(
    db: AsyncSession,
    presentation: Presentation,
    link: PresentationPublicLink,
    token: str,
) -> PublicPdf:
    """The PDF *link* offers of *presentation*, from the cache or one render.

    The caller resolved the link from *token* and checked that it offers a
    download. Releases the pooled connection before it waits: a render holds
    the request for seconds. Raises the 429 of the link's render budget,
    :class:`~app.services.presentation_export.PresentationRenderError` when
    the page cannot render, and whatever a missing headless browser raises.
    """
    from app.core.database import release_db_connection_before_loop
    from app.core.rate_limit import enforce_rate_limit
    from app.services.presentation_export import export_filename, render_public_deck_pdf

    payload = await public_presentation_payload(db, presentation, link)
    key = pdf_fingerprint(payload)
    filename = export_filename(presentation.title, "pdf")
    aspect_ratio = str(payload.get("aspect_ratio") or "16:9")
    link_id = str(link.id)
    await release_db_connection_before_loop(db)

    cached = _pdf_cache_get(key)
    if cached is not None:
        return PublicPdf(filename, cached, cached=True)
    lock = _pdf_flight(key)
    try:
        async with lock:
            cached = _pdf_cache_get(key)
            if cached is not None:
                return PublicPdf(filename, cached, cached=True)
            await enforce_rate_limit(
                None,
                "presentation_public_pdf_render",
                limit=PUBLIC_PDF_RENDERS_PER_LINK,
                window_seconds=PUBLIC_PDF_WINDOW_SECONDS,
                bucket=link_id,
            )
            data = await render_public_deck_pdf(token, aspect_ratio=aspect_ratio)
            _pdf_cache_put(key, data)
    finally:
        if not lock.locked():
            _pdf_flights.pop((id(asyncio.get_running_loop()), key), None)
    return PublicPdf(filename, data, cached=False)
