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

"""Presentation API — agent-generated slide decks.

Decks are AUTHORED by the ``generate_presentation`` built-in tool: an agent
produces a validated ``PresentationSpec`` and the frontend fetches it here to
render the interactive ``@revealjs/react`` deck behind a ``[[PRESENTATION:<id>]]``
chat marker, or on the standalone ``/presentations/:id`` viewer.

What this surface adds on top of reading:

* **Sharing.** A deck carries the platform's three-tier visibility columns, so
  it is listed and fetched through ``user_visible_filter`` like any shareable
  entity — own + user-shared + group-shared + company-managed. A ``config``
  grant lets a colleague edit the deck (here AND through their own agent); a
  ``read_only`` grant lets them present, export and duplicate it.
* **Light manual controls.** ``PATCH`` merges title / theme / transition /
  aspect ratio / chrome / the full slide list into the stored spec and runs it
  through ``validate_spec`` — the SAME validator the tool uses — so the UI can
  never introduce a deck shape the renderer would reject. There is deliberately
  no raw ``spec`` field and no ``POST`` that creates a deck from JSON: decks are
  born from the validated tool path, and edited here only as validated pieces.
  ``expected_revision`` makes a content edit conditional (409 when a
  collaborator or an agent got there first).
* **Duplicate / delete**, both audited.
* **Public link**. ``/{id}/public-link`` lets the deck's managers turn
  on, copy, regenerate and turn off the one link that opens the deck for
  anyone without an account (``services/presentation_public_links``); every
  reader is told through ``has_public_link``. ``PATCH`` sets what it offers
  besides the slides: the PDF download.

Visibility semantics: a deck whose owner was deleted (``user_id`` NULL) stays
readable for admins only, as before.
"""

from __future__ import annotations

import json
import logging
import uuid

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.api.deps import get_current_user, require_ai_enabled
from app.api.v1.helpers import (
    get_user_group_ids,
    parse_path_id,
    parse_uuid,
    user_visible_filter,
)
from app.api.v1.schemas.presentation import (
    PresentationClickerCommandRequest,
    PresentationClickerHand,
    PresentationClickerHolder,
    PresentationClickerResponse,
    PresentationCommentCreate,
    PresentationCommentDraftRequest,
    PresentationCommentDraftResponse,
    PresentationCommentResponse,
    PresentationCommentUpdate,
    PresentationDuplicateRequest,
    PresentationExportRequest,
    PresentationImportReport,
    PresentationImportResponse,
    PresentationLayoutCheckResponse,
    PresentationLibraryPage,
    PresentationPresenceEntry,
    PresentationPresenceHeartbeat,
    PresentationPresenceResponse,
    PresentationPublicLinkRequest,
    PresentationPublicLinkResponse,
    PresentationPublicLinkUpdate,
    PresentationResponse,
    PresentationSlideLayoutRequest,
    PresentationSlideLayoutResponse,
    PresentationSummaryResponse,
    PresentationUpdate,
    PresentationVersionResponse,
)
from app.core.database import get_db, release_db_connection_before_loop
from app.core.rate_limit import enforce_rate_limit
from app.core.utils import build_content_disposition, format_exception_message, safe_uuid
from app.llm.router import get_llm_for_user
from app.models.presentation import (
    VERSION_SOURCE_MANUAL,
    Presentation,
    PresentationComment,
    PresentationVersion,
)
from app.models.user import User
from app.services.audit import log_audit
from app.services.branding import get_cached_branding
from app.services.library_filters import LibraryVisibility, library_filters
from app.services.presentation_clicker import (
    CLICKER_COMMANDS_PER_MINUTE,
    CLICKER_EVENT_STATE,
    CLICKER_TTL_SECONDS,
    ClickerUnavailableError,
    clicker_frame,
    hand_clicker,
    keep_clicker,
    publish_to_user,
    read_clicker,
    release_clicker,
)
from app.services.presentation_comment_drafts import CommentDraftError, draft_comment
from app.services.presentation_comments import (
    CommentError,
    can_manage_comment,
    count_open_comments,
    create_comment,
    delete_comment,
    get_comment,
    list_comments,
    serialize_comment,
    set_comment_resolved,
    slide_ids_of,
    unknown_slide_error,
    update_comment_body,
)
from app.services.presentation_export import (
    EXPORT_FORMATS,
    PresentationRenderError,
    RenderClaims,
    RenderTokenError,
    decode_render_token,
    enforce_export_rate_limit,
    export_presentation,
    referenced_file_ids,
)
from app.services.presentation_layout import (
    check_presentation_layout,
    layout_check_enabled,
)
from app.services.presentation_layout_switch import LayoutSwitchError, switch_slide_layout
from app.services.presentation_package import (
    MAX_PACKAGE_BYTES,
    PresentationPackageError,
    build_presentation_package,
    import_presentation_package,
    package_filename,
    parse_presentation_package,
)
from app.services.presentation_pictures import load_quote_picture_labels, quote_picture_ids
from app.services.presentation_presence import (
    PRESENCE_BEATS_PER_MINUTE,
    PRESENCE_HEARTBEAT_SECONDS,
    PRESENCE_TTL_SECONDS,
    heartbeat_presence,
    leave_presence,
    list_presence,
)
from app.services.presentation_public_links import (
    OUTCOME_DISABLED,
    OUTCOME_PDF_OFF,
    OUTCOME_PDF_ON,
    PUBLIC_LINKS_OFF_DETAIL,
    audit_details,
    audit_summary,
    disable_public_link,
    enable_public_link,
    link_token,
    load_public_link,
    public_deck_ids,
    public_link_path,
    public_link_url,
    public_links_enabled,
    set_pdf_download,
)
from app.services.presentation_sharing import SHARING_FIELDS, apply_sharing
from app.services.presentation_spec import (
    DEFAULT_SLIDE_CAP,
    DOCUMENT_ASPECT_RATIOS,
    PRESENTATION_KINDS,
    SHARED_HEROES,
    PresentationSpecError,
    normalize_theme,
    slide_theme_overrides,
    validate_spec,
)
from app.services.presentation_studio import studio_turn_active
from app.services.presentation_versions import (
    changed_spec_fields,
    get_presentation_version,
    list_presentation_versions,
    resolve_user_names,
    restore_presentation_version,
    serialize_presentation_version,
    summarize_manual_edit,
)
from app.services.presentations import (
    deck_heroes,
    deck_permission,
    duplicate_presentation,
    effective_slide_cap,
    fit_slide_heroes,
    fit_slide_themes_to_schemes,
    fit_theme_to_schemes,
    introduced_slide_heroes,
    introduced_slide_themes,
    load_logo_asset_meta,
    load_visible_template_facts,
    remove_presentation,
    resolve_template_names,
    resolve_template_palette,
    resolve_template_theme,
    save_presentation,
    serialize_presentation,
    theme_fits_schemes,
    unsupported_slide_hero_message,
    unsupported_slide_heroes,
    unsupported_slide_theme_message,
    unsupported_slide_themes,
    unsupported_theme_message,
)
from app.services.visibility import SegmentCounts, Visibility, resolve_visibility, segment_counts

logger = logging.getLogger(__name__)
router = APIRouter()

#: Cap on one page of the list endpoint. A deck list is a navigation aid, not a dataset.
MAX_LIST_RESULTS = 200
#: A page of the library unless the caller asks another size: eight rows of its three-column grid.
LIST_PAGE_SIZE = 24

# The orders the library's data table offers (``GET /presentations?sort=``).
_LIST_SORT_COLUMNS = {
    "updated_at": Presentation.updated_at,
    "created_at": Presentation.created_at,
    "title": func.lower(Presentation.title),
    "slide_count": Presentation.slide_count,
}

RENDER_TOKEN_QUERY_PARAM = "render_token"  # noqa: S105 - a parameter name, not a secret

#: What the export endpoint says when the headless browser is unavailable —
#: the operator's problem (Chromium not installed, render URL unreachable),
#: never the deck's.
EXPORT_UNAVAILABLE_DETAIL = (
    "Presentation export is unavailable: the platform's headless browser could "
    "not render the deck. Check PRESENTATION_RENDER_BASE_URL and that Chromium "
    "is installed for Playwright."
)

#: Response header carrying the export log as a JSON array of sentences
#: - what the exported file could not carry the way the viewer shows
#: it, a gauge chart written as a picture for instance. The export
#: menu reads it to say so; the download itself is unaffected.
EXPORT_NOTES_HEADER = "X-Export-Notes"

#: Spec keys ``PATCH`` may rewrite, mapped from the request field of the same
#: name. ``slides`` is the whole list on purpose: reorder / delete / edit is
#: "send the new list", which is also exactly what the agent does.
_CONTENT_FIELDS = ("title", "theme", "transition", "aspect_ratio", "chrome", "slides")

#: Deck-level keys the spec defaults when absent — an explicit ``null`` in the
#: request resets one of these to its default. ``title`` and ``slides`` have no
#: default, so ``null`` there keeps the stored value.
_RESETTABLE_FIELDS = frozenset({"theme", "transition", "aspect_ratio", "chrome"})

# ── Loading + permissions ─────────────────────────────────────────────────────


async def _load_visible(
    db: AsyncSession,
    user: User,
    presentation_id: str | uuid.UUID,
    *,
    for_update: bool = False,
    without_spec: bool = False,
) -> tuple[Presentation, list[str], str]:
    """Load a deck the caller may READ plus their permission tier, or 404.

    The visibility predicate runs in SQL: a deck the user cannot see is
    indistinguishable from one that does not exist. Admins can open every deck
    by id (``is_admin``), as on every other entity DETAIL route — that is how a
    deck found in Settings > Objects opens — while the library LIST stays
    three-tier for them too (:func:`list_presentations`).

    ``for_update`` locks the row for the rest of the transaction, like the
    agent's write path (``load_visible_presentation``): a write that
    checks ``expected_revision`` and saves on top of it must not race another
    save of the same deck. ``without_spec`` leaves the spec - the one
    heavy column - in the database, for a route that only asks who may read
    the deck (the presence beat, every few seconds while someone edits); a
    read of it then raises instead of loading it behind the caller's back.
    """
    deck_id = (
        presentation_id
        if isinstance(presentation_id, uuid.UUID)
        else parse_path_id(presentation_id, not_found_detail="Presentation not found")
    )
    group_ids = await get_user_group_ids(db, user.id)
    query = select(Presentation).where(
        Presentation.id == deck_id,
        user_visible_filter(Presentation, user.id, group_ids, is_admin=user.is_admin),
    )
    if for_update:
        query = query.with_for_update(of=Presentation)
    if without_spec:
        query = query.options(defer(Presentation.spec, raiseload=True))
    result = await db.execute(query)
    presentation = result.scalar_one_or_none()
    if presentation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Presentation not found")
    tier = deck_permission(presentation, user_id=user.id, user_group_ids=group_ids, is_admin=user.is_admin)
    if tier is None:
        # The SQL predicate admits exactly the callers ``deck_permission``
        # recognises, so this is unreachable in production — kept as a
        # fail-closed belt so a drift between the two can never widen access.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    return presentation, group_ids, tier


async def _load_manageable(
    db: AsyncSession, user: User, presentation_id: str, *, action: str, for_update: bool = False
) -> tuple[Presentation, list[str]]:
    """Load a deck the caller may EDIT (owner, ``config`` grantee, admin), or 403."""
    presentation, group_ids, tier = await _load_visible(db, user, presentation_id, for_update=for_update)
    if tier not in ("owner", "config"):
        detail = (
            f"Only admins can {action} a published presentation"
            if presentation.is_company_managed
            else f"You can view this presentation but not {action} it"
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)
    return presentation, group_ids


async def _response(
    db: AsyncSession,
    presentation: Presentation,
    *,
    user: User,
    tier: str | None,
) -> dict:
    names = await resolve_user_names(db, [presentation.user_id, presentation.updated_by_user_id])
    # One container load for both colour schemes: `theme_config` is the palette
    # of the SAVED theme (what a reader renders), `theme_configs` the palette per
    # scheme so the editor can preview a theme switch before saving -
    # and, from the same load, the schemes the template supports plus its name,
    # so the theme picker offers only what the template can dress.
    palette = await resolve_template_palette(
        db, presentation.template_container_id, user_id=user.id, is_admin=user.is_admin
    )
    open_counts = await count_open_comments(db, [presentation.id])
    # The names of the quote pictures the spec references: the alt text
    # of a picture no visible credit names, on screen and on the export page.
    picture_labels = (
        await load_quote_picture_labels(
            db,
            presentation.spec,
            user_id=user.id,
            group_ids=await get_user_group_ids(db, user.id),
            is_admin=user.is_admin,
        )
        if quote_picture_ids(presentation.spec)
        else {}
    )
    return serialize_presentation(
        presentation,
        theme_config=palette.theme_for(presentation.theme),
        theme_configs=palette.themes,
        owner_name=names.get(str(presentation.user_id)),
        permission=tier,
        include_spec=True,
        updated_by_name=names.get(str(presentation.updated_by_user_id or "")),
        open_comment_count=open_counts.get(str(presentation.id), 0),
        supported_schemes=palette.supported_schemes,
        template_name=palette.name,
        # The footer / header pattern the template draws: live like
        # the palette, so a pattern edit on the template restyles every deck.
        template_chrome=palette.chrome,
        # The template's design tokens, live like the chrome.
        template_design=palette.design,
        # The layouts the slide picker may offer, from the same load.
        supported_layouts=palette.supported_layouts,
        # How many slides the deck may hold, from the same load.
        slide_cap=palette.slide_cap,
        # The covers the template offers: a cover naming another hero
        # is served on the template's own default for it.
        supported_heroes=palette.heroes,
        hero_defaults=palette.hero_defaults,
        # The trim box of every logo the deck draws: the renderer
        # sizes the mark, not the file's transparent padding.
        asset_meta=await load_logo_asset_meta(db, presentation.spec),
        picture_labels=picture_labels,
        # Every reader is told a public link opens the deck.
        has_public_link=str(presentation.id) in await public_deck_ids(db, [presentation.id]),
    )


def _user_display_name(user: User) -> str | None:
    return (getattr(user, "full_name", None) or getattr(user, "email", None) or "").strip() or None


# ── Read ──────────────────────────────────────────────────────────────────────


@router.get("", response_model=PresentationLibraryPage)
async def list_presentations(
    conversation_id: str | None = Query(default=None),
    search: str | None = Query(default=None, max_length=200, description="Words of the title, template or owner."),
    vis: str | None = Query(
        default=None,
        description="Visibility segment: `mine`, `shared`, `company` or `all` (default `all`). See services/visibility.",
    ),
    visibility: LibraryVisibility | None = Query(
        default=None,
        deprecated=True,
        description="[DEPRECATED] alias of `vis`, read when `vis` is absent.",
    ),
    mine: bool = Query(
        default=False,
        description="Only decks the caller owns (default: everything visible to them).",
    ),
    kind: str | None = Query(
        default=None,
        description="`deck` (16:9 / 4:3 slides) or `page` (A4 / letter portrait pages).",
    ),
    limit: int = Query(default=LIST_PAGE_SIZE, ge=1, le=MAX_LIST_RESULTS),
    offset: int = Query(default=0, ge=0),
    sort: str = Query(
        default="updated_at",
        description="Order by `updated_at` (default), `created_at`, `title` or `slide_count`.",
    ),
    direction: str = Query(default="desc", description="`asc` or `desc` (default); the newest deck breaks ties."),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The decks visible to the caller, a page at a time, the last changed first.

    Visibility is the standard three-tier predicate (own + shared with them +
    company-managed), so the library page shows a colleague's deck the moment
    it is shared. It is the same predicate for an admin: like every
    other library list (agents, prompts, knowledge bases, ...), the LIST never
    takes the admin bypass — the ``Mine`` / ``Shared`` / ``Company`` pills
    cannot account for a stranger's private deck, and the admin's own work
    would drown under everyone else's. An admin finds other users' decks in
    Settings > Objects and opens them through ``GET /{id}``, which keeps the
    bypass (see :func:`_load_visible`). ``visibility`` and ``search`` are the
    library rule the Videos tab shares (``services/library_filters``: the
    pills, then every word in the title, the owner's name or the template's
    name); ``mine`` narrows to the caller's own decks, published ones
    included; ``kind`` keeps one family (slide decks or portrait pages, see
    ``presentation_kind``). ``total`` counts every deck the filters match, so
    the page bar reaches the last one. ``sort`` and ``direction`` order the
    whole match before the page is cut (the library's data table sorts on
    the server); an unknown value is a 422.
    """
    order_column = _LIST_SORT_COLUMNS.get(sort)
    if order_column is None or direction not in ("asc", "desc"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"sort must be one of {sorted(_LIST_SORT_COLUMNS)} and direction asc or desc",
        )
    order = order_column.asc() if direction == "asc" else order_column.desc()
    # The newest deck breaks ties; a sort on ``updated_at`` already orders by it.
    order_by = [order] if sort == "updated_at" else [order, Presentation.updated_at.desc()]
    order_by.append(Presentation.id.desc())
    group_ids = await get_user_group_ids(db, user.id)
    where = _deck_filters(
        user,
        group_ids,
        segment=resolve_visibility(vis, visibility),
        search=search,
        mine=mine,
        kind=kind,
        conversation_id=conversation_id,
    )
    total = (await db.execute(select(func.count()).select_from(Presentation).where(*where))).scalar_one()
    result = await db.execute(
        select(Presentation)
        .where(*where)
        .order_by(*order_by)
        .offset(offset)
        .limit(limit)
    )
    decks = list(result.scalars().all())
    # One batch for owners AND last editors, one for open review threads, one
    # for the templates the decks wear — the card shows all of it
    # without a per-row query.
    names = await resolve_user_names(db, [deck.user_id for deck in decks] + [deck.updated_by_user_id for deck in decks])
    open_counts = await count_open_comments(db, [deck.id for deck in decks])
    template_names = await resolve_template_names(
        db,
        [deck.template_container_id for deck in decks],
        user_id=user.id,
        user_group_ids=group_ids,
    )
    public = await public_deck_ids(db, [deck.id for deck in decks])
    items = [
        PresentationSummaryResponse(
            **serialize_presentation(
                deck,
                owner_name=names.get(str(deck.user_id)),
                permission=deck_permission(deck, user_id=user.id, user_group_ids=group_ids, is_admin=user.is_admin)
                or ("owner" if user.is_admin else "read_only"),
                include_spec=False,
                updated_by_name=names.get(str(deck.updated_by_user_id or "")),
                open_comment_count=open_counts.get(str(deck.id), 0),
                template_name=template_names.get(str(deck.template_container_id or "")),
                has_public_link=str(deck.id) in public,
            )
        )
        for deck in decks
    ]
    return PresentationLibraryPage(items=items, total=total)


def _deck_filters(
    user: User,
    group_ids: list,
    *,
    segment: LibraryVisibility,
    search: str | None,
    mine: bool,
    kind: str | None,
    conversation_id: str | None,
) -> list:
    """The predicates of the deck library: the list and its segment counts read the same ones."""
    if kind is not None and kind not in PRESENTATION_KINDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"kind must be one of {', '.join(PRESENTATION_KINDS)}",
        )
    where = library_filters(Presentation, user, group_ids, search=search, visibility=segment)
    if mine:
        where.append(Presentation.user_id == user.id)
    if kind:
        # The format lives in the spec (validated + normalized on every write,
        # so the stored spelling is canonical); a spec that names none is 16:9.
        aspect = Presentation.spec["aspect_ratio"].as_string()
        is_page = aspect.in_(DOCUMENT_ASPECT_RATIOS)
        where.append(is_page if kind == "page" else or_(aspect.is_(None), ~is_page))
    if conversation_id:
        where.append(Presentation.conversation_id == parse_uuid(conversation_id, label="conversation ID"))
    return where


@router.get("/segment-counts", response_model=SegmentCounts)
async def presentation_segment_counts(
    conversation_id: str | None = Query(default=None),
    search: str | None = Query(default=None, max_length=200),
    mine: bool = Query(default=False),
    kind: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SegmentCounts:
    """How many decks each visibility segment holds under the list's other filters.

    Declared before ``GET /{presentation_id}`` so the literal wins the match.
    """
    group_ids = await get_user_group_ids(db, user.id)

    async def count(segment: Visibility) -> int:
        where = _deck_filters(
            user, group_ids, segment=segment, search=search, mine=mine, kind=kind, conversation_id=conversation_id
        )
        return (await db.execute(select(func.count()).select_from(Presentation).where(*where))).scalar_one()

    return await segment_counts(count)


# ── Portable packages ─────────────────────────────────────────────────
#
# A deck lives on one install. ``GET /{id}/package`` writes it out as a single
# JSON document — spec, template by slug / name, every referenced file with its
# bytes embedded — and ``POST /import`` rebuilds it on another install (or the
# same one, from a backup). The import is the ONE way a deck is created from
# JSON, and it does not bypass the tool path's guarantees: the spec goes through
# ``validate_spec`` and ``verify_file_references`` inside ``save_presentation``
# exactly like an agent-authored one, after every file id has been rewritten to
# a file that exists HERE. See ``app.services.presentation_package``.


@router.post(
    "/import",
    response_model=PresentationImportResponse,
    status_code=status.HTTP_201_CREATED,
)
async def import_presentation_endpoint(
    request: Request,
    file: UploadFile = File(
        ...,
        description="A presentation package (.presentation.json) or a bare PresentationSpec.",
    ),
    title: str | None = Form(default=None, max_length=300),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a deck owned by the caller from an exported package.

    Matching before copying: a referenced file that this platform already
    holds — a seeded brand asset, an asset of an identically named template —
    is re-used, and only what is missing is created from the embedded bytes
    (owned by the importer, sanitized like any upload). A reference that can be
    neither matched nor copied fails the import naming the file, because
    silently dropping it would change the deck. The template is resolved by
    slug, then name, then the install's default — reported in
    ``import_report`` so the library page can say what happened.
    """
    # Bounded read, the same idiom as the template import: one byte past the
    # cap is enough to decide, without holding an oversized upload in memory.
    raw = await file.read(MAX_PACKAGE_BYTES + 1)
    if len(raw) > MAX_PACKAGE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"The package exceeds the maximum size of {MAX_PACKAGE_BYTES // (1024 * 1024)} MB.",
        )
    try:
        package = parse_presentation_package(raw)
    except PresentationPackageError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    try:
        presentation, report = await import_presentation_package(db, package, user=user, title=title)
    except PresentationPackageError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except PresentationSpecError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"The packaged deck does not validate on this platform:\n{exc.human_details()}",
        ) from exc

    await log_audit(
        db,
        user=user,
        action="create",
        entity_type="presentation",
        entity_id=presentation.id,
        entity_name=presentation.title,
        summary=f"Imported presentation '{presentation.title}' from a package",
        details={
            "slide_count": presentation.slide_count,
            "source_platform": package.source_platform,
            **report.as_dict(),
        },
        request=request,
    )
    await db.commit()
    await db.refresh(presentation)
    payload = await _response(db, presentation, user=user, tier="owner")
    return PresentationImportResponse(**payload, import_report=PresentationImportReport(**report.as_dict()))


@router.get("/{presentation_id}/package")
async def export_presentation_package_endpoint(
    presentation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Download the deck as a portable JSON package (``<title>.presentation.json``).

    Same visibility as reading the deck. The package embeds the bytes of every
    referenced file the CALLER may download — a file the exporter cannot read
    travels as a reference only (filename, hash, owning template). Reading a deck grants the pictures its slides show, so a reader's package
    embeds them: the package carries exactly what its exporter could already
    fetch picture by picture through ``/chat/files/{id}/download``, never more.
    """
    presentation, _group_ids, _tier = await _load_visible(db, user, presentation_id)
    bundle = await build_presentation_package(db, presentation, user=user, platform_name=get_cached_branding().name)
    return JSONResponse(
        content=bundle,
        headers={
            "Content-Disposition": build_content_disposition(package_filename(presentation)),
            "Cache-Control": "private, no-store",
        },
    )


@router.get("/{presentation_id}", response_model=PresentationResponse)
async def get_presentation(
    presentation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return one deck's spec plus its resolved template theme overrides.

    Theme overrides are resolved against the *reader's* visibility: a template
    that has since been un-shared contributes nothing rather than leaking its
    brand config (the deck still renders with its built-in theme).
    """
    presentation, _group_ids, tier = await _load_visible(db, user, presentation_id)
    return PresentationResponse(**await _response(db, presentation, user=user, tier=tier))


# ── Write: light manual controls + sharing ────────────────────────────


@router.patch("/{presentation_id}", response_model=PresentationResponse)
async def update_presentation(
    presentation_id: str,
    body: PresentationUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Edit a deck's content (validated pieces) and / or its sharing.

    Content fields are merged into the stored spec and run through the same
    ``validate_spec`` the agent tool uses — a 422 names the offending slide and
    field, worded for the person in the editor (``Slide 4 (title): subtitle -
    ...``) rather than for the tool loop. A content edit bumps ``revision``; when
    ``expected_revision`` is given and no longer matches, nothing is written and
    the response is a 409 carrying ``current_revision`` so the client can
    merge the newer revision into its draft and save again. Sharing
    changes never bump the revision.

    The editor autosaves: a content save continues the person's open
    edit session - one history entry moved forward, never one per save
    (``open_edit_session``) - and only the save that OPENS a session is
    audited; the session's every save is in the history entry it moves. A
    template change is an entry of its own and is always audited, like a
    sharing change.
    """
    # Locked: the autosave of two editors sends saves of one deck
    # seconds apart, and two carrying the same ``expected_revision`` must
    # meet one 200 and one 409 - never both pass the check and both write.
    presentation, group_ids = await _load_manageable(db, user, presentation_id, action="edit", for_update=True)
    fields_set = body.model_fields_set
    continued_sessions: list[PresentationVersion] = []
    content_saved = False

    content_changes = {key: getattr(body, key) for key in _CONTENT_FIELDS if key in fields_set}
    template_named = "template_container_id" in fields_set
    template_uuid: uuid.UUID | None = None
    if template_named and body.template_container_id is not None:
        template_uuid = parse_uuid(body.template_container_id, label="template container ID")
    # A template change is measured against the ROW, like every other field:
    # re-sending the template the deck already wears (or ``null`` on a free
    # deck) is not a change - no revision bump, no "Applied template" history
    # line, no re-validation of a link that already holds. ``null`` on a
    # linked deck unlinks it; a different id re-links it.
    relink_template = template_named and template_uuid != presentation.template_container_id

    if (
        (content_changes or relink_template)
        and body.expected_revision is not None
        and body.expected_revision != presentation.revision
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": (
                    "The presentation changed since you loaded it "
                    f"(revision {presentation.revision}, you edited revision "
                    f"{body.expected_revision}). Reload and apply your change again."
                ),
                "current_revision": presentation.revision,
            },
        )

    if content_changes or relink_template:
        merged = dict(presentation.spec or {})
        for key, value in content_changes.items():
            if value is None:
                if key in _RESETTABLE_FIELDS:
                    merged.pop(key, None)
                continue
            merged[key] = value
        # The template decides which colour schemes the deck may be read in.
        # Linking one snaps a theme it cannot dress to the scheme it
        # was designed for (the link is the intent; the template wins), and so
        # does a save that merely carries a stored theme the template never
        # supported (an older deck) - the deck comes out branded instead of
        # degraded. CHANGING the theme to one the deck's template cannot dress
        # is refused: the picker never offers such a theme, so the request is
        # a stale client, and silently degrading the brand is what this rule
        # exists to end. The template must be visible to the editor (a typed
        # id, not one from search_templates) and be a presentation template -
        # an asset library has no palette to apply.
        effective_template = template_uuid if relink_template else presentation.template_container_id
        facts = None
        # A slide's OWN theme is held to the template like the deck
        # theme: the facts are read whenever a save carries slides that name
        # one, so the editor's Theme picker is refused the same scheme the
        # deck picker is.
        slides_carry_themes = "slides" in content_changes and bool(slide_theme_overrides(merged))
        # A template's own cover is held to the template that offers
        # it, so a save carrying one reads the template too.
        slides_carry_template_heroes = "slides" in content_changes and bool(
            unsupported_slide_heroes(merged, SHARED_HEROES)
        )
        needs_facts = (
            relink_template or "theme" in content_changes or slides_carry_themes or slides_carry_template_heroes
        )
        # How many slides the deck may hold is the template's to say,
        # so a save that GROWS the deck reads the template for its cap - one
        # load, shared with the theme rules above when they need it too. A
        # save that does not grow the deck is admitted at the deck's size
        # (``effective_slide_cap``), so it reads nothing; an invisible
        # template reads as the default, as it reads as free everywhere else.
        stored_slides = (presentation.spec or {}).get("slides") or []
        grows_deck = len(merged.get("slides") or []) > len(stored_slides)
        slide_cap = DEFAULT_SLIDE_CAP
        if effective_template is not None and (needs_facts or grows_deck):
            loaded = await load_visible_template_facts(
                db, effective_template, user_id=user.id, is_admin=user.is_admin
            )
            if loaded is not None:
                slide_cap = loaded.slide_cap
            if needs_facts:
                facts = loaded
            if facts is None and relink_template:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")
            # A theme change on a deck whose STORED template the editor cannot
            # see (un-shared since the deck was made, or a collaborator's
            # private template) leaves ``facts`` None and is accepted for any
            # scheme - deliberately. The read path renders that deck
            # as a free deck (``resolve_template_palette`` contributes nothing
            # for an invisible template: no palette, no name, and
            # ``supported_schemes`` serialises as both), so the picker this
            # editor sees offers both schemes; refusing one of them here would
            # contradict the very response the editor is looking at. The link
            # is kept as stored, and the invisible template's schemes are never
            # read on the editor's behalf. An admin sees every template, on the
            # read path and here alike, so the rule holds for them unchanged.
            # Pinned by ``test_patch_theme_is_free_when_the_stored_template_is_invisible``.
            if facts is not None and relink_template and not facts.is_presentation_template:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=(
                        f"'{facts.name}' is not a presentation template (kind: "
                        f"{facts.kind or 'unknown'}), so it cannot dress a deck."
                    ),
                )
        if facts is not None and not theme_fits_schemes(merged.get("theme"), facts.supported_schemes):
            requested_theme = content_changes.get("theme")
            theme_changed = requested_theme is not None and normalize_theme(requested_theme) != normalize_theme(
                presentation.theme
            )
            if theme_changed and not relink_template:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=unsupported_theme_message(
                        requested_theme,
                        template_name=facts.name,
                        supported_schemes=facts.supported_schemes,
                    ),
                )
            merged["theme"] = fit_theme_to_schemes(merged.get("theme"), facts.supported_schemes)
        if facts is not None:
            # The slides' own themes. A save that SETS one the deck's
            # template cannot dress is refused with the slide and the field
            # (the picker never offers it, so this is a stale client); a
            # template change snaps every override it cannot dress, the way
            # it snaps the deck - the template wins, the slides follow.
            offending = unsupported_slide_themes(merged, facts.supported_schemes)
            if offending and not relink_template:
                # Only what this save INTRODUCES is refused, compared by the
                # slide's stable id (a stored override the template never
                # supported survives a reorder or a title edit; a slide
                # replaced at the same position is a new one).
                introduced = introduced_slide_themes(presentation.spec, merged, offending)
                if introduced:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                        detail=unsupported_slide_theme_message(
                            introduced,
                            spec=merged,
                            template_name=facts.name,
                            supported_schemes=facts.supported_schemes,
                        ),
                    )
            if offending and relink_template:
                merged["slides"] = [dict(slide) if isinstance(slide, dict) else slide for slide in merged.get("slides") or []]
                fit_slide_themes_to_schemes(merged, facts.supported_schemes)
        if relink_template or slides_carry_template_heroes:
            # The covers' heroes, by the slide-theme rule above: a
            # hero this save SETS that the deck's template does not offer is
            # refused (the picker never offers it); a template change gives
            # every cover it does not dress the template's own default.
            heroes, hero_defaults = deck_heroes(facts)
            offending_heroes = unsupported_slide_heroes(merged, heroes)
            if offending_heroes and not relink_template:
                introduced_heroes = introduced_slide_heroes(presentation.spec, merged, offending_heroes)
                if introduced_heroes:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                        detail=unsupported_slide_hero_message(
                            introduced_heroes,
                            spec=merged,
                            template_name=facts.name if facts is not None else None,
                            heroes=heroes,
                        ),
                    )
            if offending_heroes and relink_template:
                merged["slides"] = list(merged.get("slides") or [])
                fit_slide_heroes(merged, heroes, hero_defaults)
        try:
            validated = validate_spec(merged, slide_cap=effective_slide_cap(slide_cap, presentation.spec))
        except PresentationSpecError as exc:
            # A person is behind this route: slides numbered from 1, no "call
            # the tool again", no catalog dump.
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=exc.human_message()) from exc
        # The editor sends the whole spec on every save; the history line must
        # name what actually changed, not what was sent — and a save that
        # changes nothing is a no-op, not a revision bump. A template re-link
        # alone still goes through ``save_presentation`` so the revision bumps:
        # the deck LOOKS different afterwards, and a collaborator's stale edit
        # must be told so.
        changed = changed_spec_fields(presentation.spec, validated)
        if changed or relink_template:
            touched = changed + (["template"] if relink_template else [])
            try:
                # Every file reference this save INTRODUCES is checked against
                # the person saving: a deck's readers may fetch what its
                # slides name, so a foreign file id written here would be served
                # to them - and to the writer. References the deck already
                # carries were vouched for when they were written and are left
                # alone (the stored spec is the baseline), so a picture deleted
                # since never blocks an unrelated edit.
                await save_presentation(
                    db,
                    user_id=presentation.user_id,
                    spec_payload=validated,
                    template_container_id=template_uuid,
                    presentation=presentation,
                    unlink_template=relink_template and template_uuid is None,
                    verify_files_for=user,
                    verify_baseline=presentation.spec,
                    author_id=user.id,
                    change_source=VERSION_SOURCE_MANUAL,
                    change_summary=summarize_manual_edit(touched, previous=presentation.spec, current=validated),
                    slide_cap=slide_cap,
                    # A template change is an entry of its own.
                    edit_session=not relink_template,
                    continued_sessions=continued_sessions,
                )
            except PresentationSpecError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=exc.human_message()
                ) from exc
            content_saved = True

    sharing_touched = bool(SHARING_FIELDS & fields_set)
    if sharing_touched:
        # Only whoever may manage the deck reached this point; publishing it
        # (company-managed) is additionally admin-only inside the write. The
        # share_presentation tool goes through the same write.
        await apply_sharing(db, user, presentation, body)

    # What was written is audited: a save that opened an edit session, a
    # template change, a sharing change. An autosave inside an open session is
    # in that session's history entry (the save that opened it was audited),
    # and a save the server found unchanged wrote nothing.
    if (content_saved and not continued_sessions) or sharing_touched:
        await log_audit(
            db,
            user=user,
            action="update",
            entity_type="presentation",
            entity_id=presentation.id,
            entity_name=presentation.title,
            summary=f"Updated presentation '{presentation.title}'",
            details={
                "content_fields": sorted(content_changes),
                "template_changed": relink_template,
                "sharing_changed": sharing_touched,
                "revision": presentation.revision,
                "is_company_managed": presentation.is_company_managed,
            },
            request=request,
        )
    await db.commit()
    await db.refresh(presentation)
    # Recompute the tier from the row as written rather than re-running the
    # visibility query: a ``config`` grantee who just removed their own grant
    # gets an honest ``permission: null`` response, not a 404 on a write that
    # succeeded.
    tier = deck_permission(presentation, user_id=user.id, user_group_ids=group_ids, is_admin=user.is_admin)
    return PresentationResponse(**await _response(db, presentation, user=user, tier=tier))


@router.delete("/{presentation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_presentation(
    presentation_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a deck (owner, ``config`` grantee, or admin).

    The ``[[PRESENTATION:<id>]]`` marker already sitting in a conversation then
    renders as "no longer available", which is the honest state — the chat
    message itself is not rewritten.
    """
    presentation, _group_ids = await _load_manageable(db, user, presentation_id, action="delete")
    await remove_presentation(db, presentation, actor=user, request=request)
    await db.commit()


@router.post(
    "/{presentation_id}/duplicate",
    response_model=PresentationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def duplicate_presentation_endpoint(
    presentation_id: str,
    request: Request,
    body: PresentationDuplicateRequest | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Copy a deck the caller can read into a new deck they own.

    Reading is enough: a ``read_only`` grantee who wants their own variant of
    a shared deck forks it instead of asking for edit rights, and a published
    (company-managed) deck is the natural starting point for the next one.
    """
    from app.core.utils import fit_copy_name

    source, _group_ids, _tier = await _load_visible(db, user, presentation_id)
    try:
        copy = await duplicate_presentation(
            db,
            source,
            user_id=user.id,
            title=(
                body.title
                if body and body.title
                else fit_copy_name(source.title, Presentation.__table__.c.title, prefix="Copy of ")
            ),
        )
    except PresentationSpecError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"The source deck no longer validates and cannot be copied:\n{exc.human_details()}",
        ) from exc
    await log_audit(
        db,
        user=user,
        action="create",
        entity_type="presentation",
        entity_id=copy.id,
        entity_name=copy.title,
        summary=f"Duplicated presentation '{source.title}'",
        details={"source_id": str(source.id), "slide_count": copy.slide_count},
        request=request,
    )
    await db.commit()
    await db.refresh(copy)
    return PresentationResponse(**await _response(db, copy, user=user, tier="owner"))


# ── Public link ──────────────────────────────────────────────────────
#
# The Share dialog's side of ``services/presentation_public_links`` (the
# ``set_presentation_public_link`` tool is the agent's): the deck's managers
# read, turn on, regenerate and turn off the link that opens it for anyone.
# What the link then serves, without an account, is
# ``api/v1/presentation_public.py``.


async def _public_link_response(
    db: AsyncSession,
    *,
    token: str | None,
    created_at,
    created_by_user_id: uuid.UUID | None,
    allow_pdf_download: bool,
) -> PresentationPublicLinkResponse:
    names = await resolve_user_names(db, [created_by_user_id]) if created_by_user_id else {}
    return PresentationPublicLinkResponse(
        available=public_links_enabled(),
        enabled=True,
        path=public_link_path(token) if token else None,
        url=public_link_url(token) if token else None,
        created_at=created_at.isoformat() if created_at else None,
        created_by_name=names.get(str(created_by_user_id)),
        allow_pdf_download=allow_pdf_download,
    )


async def _stored_link_response(db: AsyncSession, link) -> PresentationPublicLinkResponse:
    return await _public_link_response(
        db,
        token=link_token(link),
        created_at=link.created_at,
        created_by_user_id=link.created_by_user_id,
        allow_pdf_download=bool(link.allow_pdf_download),
    )


@router.get("/{presentation_id}/public-link", response_model=PresentationPublicLinkResponse)
async def get_presentation_public_link(
    presentation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The deck's public link, for whoever may share the deck (owner, editor, admin)."""
    presentation, _group_ids = await _load_manageable(db, user, presentation_id, action="share")
    link = await load_public_link(db, presentation.id)
    if link is None:
        return PresentationPublicLinkResponse(available=public_links_enabled(), enabled=False)
    return await _stored_link_response(db, link)


@router.post("/{presentation_id}/public-link", response_model=PresentationPublicLinkResponse)
async def enable_presentation_public_link(
    presentation_id: str,
    request: Request,
    body: PresentationPublicLinkRequest | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Turn the deck's public link on, or hand back the one it has.

    ``regenerate`` issues a new token: the previous URL stops answering at
    once. Refused (403) while the admin switch is off. One audit row per call
    that changed the link.
    """
    presentation, _group_ids = await _load_manageable(db, user, presentation_id, action="share")
    if not public_links_enabled():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=PUBLIC_LINKS_OFF_DETAIL)
    change = await enable_public_link(
        db, presentation.id, actor_id=user.id, regenerate=bool(body and body.regenerate)
    )
    if change.changed:
        await log_audit(
            db,
            user=user,
            action="update",
            entity_type="presentation",
            entity_id=presentation.id,
            entity_name=presentation.title,
            summary=audit_summary(change.outcome, presentation.title),
            details=audit_details(change.outcome),
            request=request,
        )
    await db.commit()
    return await _public_link_response(
        db,
        token=change.token,
        created_at=change.created_at,
        created_by_user_id=change.created_by_user_id,
        allow_pdf_download=change.allow_pdf_download,
    )


@router.patch("/{presentation_id}/public-link", response_model=PresentationPublicLinkResponse)
async def update_presentation_public_link(
    presentation_id: str,
    body: PresentationPublicLinkUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """What the deck's public link offers besides the slides: its PDF download.

    404 for a deck with no link (the option belongs to the link). Turning the
    download on is refused (403) while the admin switch is off; turning it
    off never is. One audit row when the option changed.
    """
    presentation, _group_ids = await _load_manageable(db, user, presentation_id, action="share")
    if body.allow_pdf_download and not public_links_enabled():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=PUBLIC_LINKS_OFF_DETAIL)
    changed = await set_pdf_download(db, presentation.id, body.allow_pdf_download)
    if changed is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This presentation has no public link")
    if changed:
        outcome = OUTCOME_PDF_ON if body.allow_pdf_download else OUTCOME_PDF_OFF
        await log_audit(
            db,
            user=user,
            action="update",
            entity_type="presentation",
            entity_id=presentation.id,
            entity_name=presentation.title,
            summary=audit_summary(outcome, presentation.title),
            details=audit_details(outcome),
            request=request,
        )
    await db.commit()
    link = await load_public_link(db, presentation.id)
    if link is None:  # pragma: no cover - turned off between the commit and the read
        return PresentationPublicLinkResponse(available=public_links_enabled(), enabled=False)
    return await _stored_link_response(db, link)


@router.delete("/{presentation_id}/public-link", status_code=status.HTTP_204_NO_CONTENT)
async def disable_presentation_public_link(
    presentation_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Turn the deck's public link off: its URL stops answering at once.

    Allowed while the admin switch is off too - taking a link down is never
    refused. A deck with no link answers 204 and writes nothing.
    """
    presentation, _group_ids = await _load_manageable(db, user, presentation_id, action="share")
    if await disable_public_link(db, presentation.id):
        await log_audit(
            db,
            user=user,
            action="update",
            entity_type="presentation",
            entity_id=presentation.id,
            entity_name=presentation.title,
            summary=audit_summary(OUTCOME_DISABLED, presentation.title),
            details=audit_details(OUTCOME_DISABLED),
            request=request,
        )
    await db.commit()


# ── Collaboration: versions ──────────────────────────────────────────


@router.get("/{presentation_id}/versions", response_model=list[PresentationVersionResponse])
async def list_presentation_versions_endpoint(
    presentation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The deck's revision history, newest first — headlines only, no specs.

    Reading the deck is enough to read its history: a reviewer asking "what
    changed since yesterday?" is exactly who this is for.
    """
    presentation, _group_ids, _tier = await _load_visible(db, user, presentation_id)
    versions = await list_presentation_versions(db, presentation.id)
    names = await resolve_user_names(db, [v.author_id for v in versions])
    return [
        PresentationVersionResponse(
            **serialize_presentation_version(v, author_name=names.get(str(v.author_id or "")), include_spec=False)
        )
        for v in versions
    ]


@router.get(
    "/{presentation_id}/versions/{revision}",
    response_model=PresentationVersionResponse,
)
async def get_presentation_version_endpoint(
    presentation_id: str,
    revision: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """One version WITH its spec — what the history panel previews."""
    presentation, _group_ids, _tier = await _load_visible(db, user, presentation_id)
    version = await get_presentation_version(db, presentation.id, revision)
    if version is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found")
    names = await resolve_user_names(db, [version.author_id])
    return PresentationVersionResponse(
        **serialize_presentation_version(
            version, author_name=names.get(str(version.author_id or "")), include_spec=True
        )
    )


@router.post(
    "/{presentation_id}/versions/{revision}/restore",
    response_model=PresentationResponse,
)
async def restore_presentation_version_endpoint(
    presentation_id: str,
    revision: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Bring a past revision back as a NEW revision (never a rewind).

    Managers only — a restore is an edit. The restored spec is re-validated on
    the way in, so a version that no longer passes the current validator is a
    422 naming the field rather than a silent write of a deck the renderer
    cannot draw.
    """
    # Locked like the PATCH: a restore writes the next revision too.
    presentation, group_ids = await _load_manageable(db, user, presentation_id, action="restore", for_update=True)
    version = await get_presentation_version(db, presentation.id, revision)
    if version is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found")
    if version.revision == presentation.revision:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Revision {revision} is already the current one.",
        )
    try:
        await restore_presentation_version(db, presentation, version, user_id=user.id)
    except PresentationSpecError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Revision {revision} no longer validates and cannot be restored:\n{exc.human_details()}",
        ) from exc
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="presentation",
        entity_id=presentation.id,
        entity_name=presentation.title,
        summary=f"Restored revision {revision} of presentation '{presentation.title}'",
        details={"restored_from_revision": revision, "revision": presentation.revision},
        request=request,
    )
    await db.commit()
    await db.refresh(presentation)
    tier = deck_permission(presentation, user_id=user.id, user_group_ids=group_ids, is_admin=user.is_admin)
    return PresentationResponse(**await _response(db, presentation, user=user, tier=tier))


# ── Collaboration: review comments ───────────────────────────────────


async def _comments_payload(
    db: AsyncSession, presentation: Presentation, *, user: User, tier: str | None
) -> list[dict]:
    comments = await list_comments(db, presentation.id)
    names = await resolve_user_names(
        db,
        [c.author_id for c in comments] + [c.resolved_by_user_id for c in comments],
    )
    return [
        serialize_comment(c, spec=presentation.spec, names=names, user_id=user.id, permission=tier) for c in comments
    ]


@router.get("/{presentation_id}/comments", response_model=list[PresentationCommentResponse])
async def list_presentation_comments_endpoint(
    presentation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Every comment on the deck, oldest first; threads are rebuilt client-side.

    ``slide_position`` is resolved against the CURRENT spec on every read, so a
    reorder moves the number and a deleted slide shows the note as detached.
    """
    presentation, _group_ids, tier = await _load_visible(db, user, presentation_id)
    return [PresentationCommentResponse(**c) for c in await _comments_payload(db, presentation, user=user, tier=tier)]


@router.post(
    "/{presentation_id}/comments",
    response_model=PresentationCommentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_presentation_comment_endpoint(
    presentation_id: str,
    body: PresentationCommentCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Leave a review note on a slide (or on the whole deck), or reply to one.

    Anyone who can READ the deck may comment — a read-only grant is a
    reviewer's grant. The slide must exist; a reply inherits its thread's slide.
    """
    presentation, _group_ids, tier = await _load_visible(db, user, presentation_id)
    parent_uuid = parse_uuid(body.parent_id, label="parent comment ID") if body.parent_id else None
    try:
        comment = await create_comment(
            db,
            presentation,
            author_id=user.id,
            body=body.body,
            slide_id=body.slide_id,
            parent_id=parent_uuid,
        )
    except CommentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    await log_audit(
        db,
        user=user,
        action="create",
        entity_type="presentation_comment",
        entity_id=comment.id,
        entity_name=presentation.title,
        summary=f"Commented on presentation '{presentation.title}'",
        details={
            "presentation_id": str(presentation.id),
            "slide_id": comment.slide_id,
            "reply": comment.parent_id is not None,
        },
        request=request,
    )
    await db.commit()
    await db.refresh(comment)
    return PresentationCommentResponse(
        **serialize_comment(
            comment,
            spec=presentation.spec,
            names={str(user.id): _user_display_name(user) or ""},
            user_id=user.id,
            permission=tier,
        )
    )


@router.post("/{presentation_id}/comments/draft", response_model=PresentationCommentDraftResponse)
async def draft_presentation_comment_endpoint(
    presentation_id: str,
    body: PresentationCommentDraftRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Draft a review note with the caller's model — "Generate with AI" in the composer.

    Anyone who can READ the deck may ask for a draft, as they may post one. The
    slide under review, its neighbours and the thread being answered are read
    from the deck here; the client only says what the note should convey.
    Nothing is stored: the draft goes back to the textarea, and posting it is
    the ordinary ``POST /comments`` under the person's own name.
    """
    presentation, _group_ids, _tier = await _load_visible(db, user, presentation_id)
    anchor = (body.slide_id or "").strip() or None
    thread: list[tuple[str, str]] = []
    if body.parent_id:
        parent = await get_comment(db, presentation.id, parse_uuid(body.parent_id, label="parent comment ID"))
        if parent is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comment not found")
        root_id = parent.parent_id or parent.id
        members = [c for c in await list_comments(db, presentation.id) if c.id == root_id or c.parent_id == root_id]
        names = await resolve_user_names(db, [c.author_id for c in members])
        thread = [(names.get(str(c.author_id) or "", ""), c.body) for c in members]
        anchor = next((c.slide_id for c in members if c.id == root_id), anchor)
    elif anchor is not None and anchor not in slide_ids_of(presentation.spec):
        exc = unknown_slide_error(presentation.spec, anchor)
        raise HTTPException(status_code=exc.status_code, detail=str(exc))

    llm, model = await get_llm_for_user(db, str(user.id))
    try:
        draft = await draft_comment(
            llm,
            model,
            spec=presentation.spec,
            deck_title=presentation.title,
            slide_id=anchor,
            brief=body.brief,
            current_text=body.current_text,
            thread=thread,
        )
    except CommentDraftError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return PresentationCommentDraftResponse(body=draft.body, truncated=draft.truncated)


async def _load_comment_for_write(
    db: AsyncSession, user: User, presentation_id: str, comment_id: str
) -> tuple[Presentation, PresentationComment, str | None]:
    presentation, _group_ids, tier = await _load_visible(db, user, presentation_id)
    # Row-locked until this request's commit: two resolves racing on one
    # thread stamp it once (the second reads it resolved and changes nothing).
    comment = await get_comment(db, presentation.id, parse_path_id(comment_id, not_found_detail="Comment not found"), for_update=True)
    if comment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comment not found")
    if not can_manage_comment(comment, user_id=user.id, permission=tier):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the comment's author or whoever manages the presentation can do that",
        )
    return presentation, comment, tier


@router.patch(
    "/{presentation_id}/comments/{comment_id}",
    response_model=PresentationCommentResponse,
)
async def update_presentation_comment_endpoint(
    presentation_id: str,
    comment_id: str,
    body: PresentationCommentUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Edit a comment's text and / or resolve or reopen its thread.

    Author or deck manager — the same ``can_manage_comment`` gate the agent's
    ``resolve_presentation_comment`` tool runs as the calling user, so
    "implement this note and close it" ends on exactly the notes the person
    asking could have closed by hand.

    Resolving is idempotent: a thread already in the requested state is left
    as it is — no new ``resolved_at`` / ``resolved_by_user_id`` stamp, no
    audit row — and comes back unchanged.
    """
    presentation, comment, tier = await _load_comment_for_write(db, user, presentation_id, comment_id)
    fields_set = body.model_fields_set
    changed: list[str] = []
    try:
        if "body" in fields_set and body.body is not None:
            await update_comment_body(db, comment, body=body.body)
            changed.append("body")
        if (
            "resolved" in fields_set
            and body.resolved is not None
            and await set_comment_resolved(db, comment, resolved=body.resolved, user_id=user.id)
        ):
            changed.append("resolved")
    except CommentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    if changed:
        await log_audit(
            db,
            user=user,
            action="update",
            entity_type="presentation_comment",
            entity_id=comment.id,
            entity_name=presentation.title,
            summary=(
                f"{'Resolved' if body.resolved else 'Reopened'} a comment on '{presentation.title}'"
                if "resolved" in changed
                else f"Edited a comment on '{presentation.title}'"
            ),
            details={"presentation_id": str(presentation.id), "fields": changed},
            request=request,
        )
    await db.commit()
    await db.refresh(comment)
    names = await resolve_user_names(db, [comment.author_id, comment.resolved_by_user_id])
    return PresentationCommentResponse(
        **serialize_comment(comment, spec=presentation.spec, names=names, user_id=user.id, permission=tier)
    )


@router.delete("/{presentation_id}/comments/{comment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_presentation_comment_endpoint(
    presentation_id: str,
    comment_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a comment (author or deck manager); deleting a root takes its replies."""
    presentation, comment, _tier = await _load_comment_for_write(db, user, presentation_id, comment_id)
    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="presentation_comment",
        entity_id=comment.id,
        entity_name=presentation.title,
        summary=f"Deleted a comment on '{presentation.title}'",
        details={"presentation_id": str(presentation.id), "slide_id": comment.slide_id},
        request=request,
    )
    await delete_comment(db, comment)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Collaboration: presence ──────────────────────────────────────────


@router.post("/{presentation_id}/presence", response_model=PresentationPresenceResponse)
async def presentation_presence_heartbeat(
    presentation_id: str,
    body: PresentationPresenceHeartbeat,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Say "I have this deck open" and get back who else does.

    Redis-backed and ephemeral (``presentation_presence``): the viewer calls
    this every ``heartbeat_seconds`` and the entry expires after three missed
    beats. Fail-open — an unreachable Redis returns an empty room, never an
    error, because presence is a courtesy, not a lock.

    The ``assistant`` half of "is editing" is the SERVER's reading, never the
    client's claim: an agent turn in flight in one of the caller's
    conversations bound to this deck, read off the chat turn marker. A client
    whose in-flight state got stuck therefore cannot keep the badge on - the
    marker goes when the turn does, and the next beat says so.

    An editor beats every few seconds, so a beat reads no spec and is
    budgeted per person and deck (``PRESENCE_BEATS_PER_MINUTE``), fail-open
    like the rest of presence: past the budget, or with Redis down, the room
    just is not refreshed.
    """
    deck_id = parse_path_id(presentation_id, not_found_detail="Presentation not found")
    await enforce_rate_limit(
        None,
        "presentation_presence",
        limit=PRESENCE_BEATS_PER_MINUTE,
        window_seconds=60,
        bucket=f"{user.id}:{deck_id}",
        fail_closed=False,
        quiet=True,
    )
    presentation, _group_ids, _tier = await _load_visible(db, user, deck_id, without_spec=True)
    assistant = await studio_turn_active(db, presentation_id=presentation.id, user_id=user.id)
    room = await heartbeat_presence(
        presentation.id,
        user_id=user.id,
        name=_user_display_name(user),
        editing=bool(body.editing),
        assistant=assistant,
        slide_index=body.slide_index,
        slide_id=body.slide_id,
        block=body.block,
    )
    # The deck's revision travels with the room: an editor holding an
    # older one reads the deck again and merges the newer revision in.
    return PresentationPresenceResponse(
        viewers=[PresentationPresenceEntry(**entry) for entry in room],
        heartbeat_seconds=PRESENCE_HEARTBEAT_SECONDS,
        ttl_seconds=PRESENCE_TTL_SECONDS,
        revision=presentation.revision,
    )


@router.delete("/{presentation_id}/presence", status_code=status.HTTP_204_NO_CONTENT)
async def presentation_presence_leave(
    presentation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Leave the room now rather than after the TTL (viewer closed)."""
    presentation, _group_ids, _tier = await _load_visible(db, user, presentation_id, without_spec=True)
    await leave_presence(presentation.id, user_id=user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Collaboration: hand the clicker (#10552 slice B) ──────────────────────


_CLICKER_UNAVAILABLE = "The clicker cannot be handed right now, try again in a moment"


async def _clicker_response(presentation_id: uuid.UUID, user: User, entry: dict | None) -> PresentationClickerResponse:
    """The clicker as *user* sees it, with the slide the presenter is on."""
    if not entry:
        return PresentationClickerResponse(ttl_seconds=CLICKER_TTL_SECONDS)
    presenter_id = entry["presenter_id"]
    slide_index = None
    for viewer in await list_presence(presentation_id):
        if viewer["user_id"] == presenter_id:
            slide_index = viewer.get("slide_index")
            break
    return PresentationClickerResponse(
        presenter_id=presenter_id,
        holder=PresentationClickerHolder(user_id=entry["holder_id"], name=entry.get("holder_name")),
        handed_at=entry.get("handed_at"),
        is_presenter=presenter_id == str(user.id),
        is_holder=entry["holder_id"] == str(user.id),
        slide_index=slide_index,
        ttl_seconds=CLICKER_TTL_SECONDS,
    )


@router.get("/{presentation_id}/clicker", response_model=PresentationClickerResponse)
async def get_presentation_clicker(
    presentation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Who presents the deck and who holds its clicker.

    The holder's view reads this every few seconds: the read keeps the clicker
    alive, so a holder whose tab died stops holding it after
    ``CLICKER_TTL_SECONDS`` and control falls back to the presenter.
    """
    presentation, _group_ids, _tier = await _load_visible(db, user, presentation_id, without_spec=True)
    entry = await read_clicker(presentation.id)
    if entry and entry["holder_id"] == str(user.id):
        await keep_clicker(presentation.id)
    return await _clicker_response(presentation.id, user, entry)


@router.put("/{presentation_id}/clicker", response_model=PresentationClickerResponse)
async def hand_presentation_clicker(
    presentation_id: str,
    body: PresentationClickerHand,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Hand the clicker to a person who can already read the deck.

    The caller becomes the presenter the holder's commands reach. One clicker
    at a time: handing it again replaces the holder. Someone who cannot read
    the deck is refused exactly like an unknown person (404), and a public-link
    viewer has no account to hand it to.
    """
    presentation, _group_ids, _tier = await _load_visible(db, user, presentation_id, without_spec=True)
    if body.user_id == user.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You already hold your own clicker")
    holder = (await db.execute(select(User).where(User.id == body.user_id))).scalar_one_or_none()
    holder_tier = None
    if holder is not None and holder.is_active:
        holder_tier = deck_permission(
            presentation,
            user_id=holder.id,
            user_group_ids=await get_user_group_ids(db, holder.id),
            is_admin=holder.is_admin,
        )
    if holder is None or holder_tier is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This person cannot open the deck")
    holder_name = _user_display_name(holder)
    try:
        entry = await hand_clicker(presentation.id, presenter_id=user.id, holder_id=holder.id, holder_name=holder_name)
    except ClickerUnavailableError:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=_CLICKER_UNAVAILABLE) from None
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="presentation",
        entity_id=presentation.id,
        entity_name=presentation.title,
        summary=f"Handed the clicker of '{presentation.title}' to {holder_name or holder.id}",
        details={"clicker": "handed", "holder_id": str(holder.id)},
        request=request,
    )
    await db.commit()
    # The co-presenter's one in-app notice: their app shows a toast on it.
    try:
        await publish_to_user(
            holder.id,
            {
                "type": CLICKER_EVENT_STATE,
                "presentation_id": str(presentation.id),
                "title": presentation.title,
                "state": "handed",
                "presenter_name": _user_display_name(user),
            },
        )
    except ClickerUnavailableError:
        logger.debug("clicker handed but the notice to %s was not sent", holder.id)
    return await _clicker_response(presentation.id, user, entry)


@router.delete("/{presentation_id}/clicker", status_code=status.HTTP_204_NO_CONTENT)
async def release_presentation_clicker(
    presentation_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Take the clicker back (presenter) or give it back (holder)."""
    presentation, _group_ids, _tier = await _load_visible(db, user, presentation_id, without_spec=True)
    entry = await read_clicker(presentation.id)
    if not entry:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    me = str(user.id)
    if me not in (entry["presenter_id"], entry["holder_id"]):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Only the presenter or the holder can release the clicker"
        )
    try:
        await release_clicker(presentation.id)
    except ClickerUnavailableError:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=_CLICKER_UNAVAILABLE) from None
    taken_back = me == entry["presenter_id"]
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="presentation",
        entity_id=presentation.id,
        entity_name=presentation.title,
        summary=(
            f"Took back the clicker of '{presentation.title}'"
            if taken_back
            else f"Gave back the clicker of '{presentation.title}'"
        ),
        details={"clicker": "taken_back" if taken_back else "given_back", "holder_id": entry["holder_id"]},
        request=request,
    )
    await db.commit()
    other = entry["holder_id"] if taken_back else entry["presenter_id"]
    try:
        await publish_to_user(
            other,
            {"type": CLICKER_EVENT_STATE, "presentation_id": str(presentation.id), "state": "released"},
        )
    except ClickerUnavailableError:
        logger.debug("clicker released but the notice to %s was not sent", other)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{presentation_id}/clicker/command", status_code=status.HTTP_204_NO_CONTENT)
async def presentation_clicker_command(
    presentation_id: str,
    body: PresentationClickerCommandRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The holder moves the presenter's deck: next, previous or a slide.

    Relayed to the presenter's windows over their activity stream; anyone
    but the current holder is refused (409), so a clicker taken back stops
    driving at once. Budgeted per person and deck.
    """
    deck_id = parse_path_id(presentation_id, not_found_detail="Presentation not found")
    await enforce_rate_limit(
        None,
        "presentation_clicker",
        limit=CLICKER_COMMANDS_PER_MINUTE,
        window_seconds=60,
        bucket=f"{user.id}:{deck_id}",
    )
    presentation, _group_ids, _tier = await _load_visible(db, user, deck_id, without_spec=True)
    if body.command == "goto" and body.index is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="goto needs a slide index")
    entry = await read_clicker(presentation.id)
    if not entry or entry["holder_id"] != str(user.id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="You do not hold the clicker of this deck")
    try:
        await publish_to_user(
            entry["presenter_id"],
            clicker_frame(presentation.id, command=body.command, index=body.index, sender_id=user.id),
        )
    except ClickerUnavailableError:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=_CLICKER_UNAVAILABLE) from None
    await keep_clicker(presentation.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Export ────────────────────────────────────────────────────────────


@router.post("/{presentation_id}/export")
async def export_presentation_endpoint(
    presentation_id: str,
    body: PresentationExportRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Render the deck server-side and return it as a PDF or PPTX download.

    Same visibility as reading the deck. The bytes are streamed back rather
    than stored: an interactive export is a download, not an artifact of the
    conversation (the ``export_presentation`` tool is the path that persists
    one as a ``FileAttachment`` the agent can hand over). Counted against the
    user's export budget, shared with the report export, and no
    pooled connection is held while the render waits for its slot and runs.
    """
    export_format = (body.format or "pdf").strip().lower()
    if export_format not in EXPORT_FORMATS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"format must be one of: {', '.join(EXPORT_FORMATS)}",
        )
    await enforce_export_rate_limit(user.id)
    presentation, _group_ids, _tier = await _load_visible(db, user, presentation_id)
    await release_db_connection_before_loop(db)

    try:
        exported = await export_presentation(
            presentation_id=presentation.id,
            user_id=user.id,
            title=presentation.title,
            spec=presentation.spec or {},
            export_format=export_format,
        )
    except PresentationRenderError as exc:
        logger.warning(
            "Presentation export failed: deck=%s format=%s: %s",
            presentation.id,
            export_format,
            exc,
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(
            "Presentation export crashed: deck=%s format=%s: %s",
            presentation.id,
            export_format,
            format_exception_message(exc),
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=EXPORT_UNAVAILABLE_DETAIL,
        ) from exc

    # ROI of chat work (#10544, S2b): an export is a use of the deck.
    from app.services.roi_chat_records import record_signal_detached

    await record_signal_detached(kind="presentation", deliverable_id=presentation.id, signal="exported")
    headers = {
        "Content-Disposition": build_content_disposition(exported.filename),
        "Cache-Control": "private, no-store",
    }
    if exported.notes:
        # What the file could not carry as the viewer shows it (a gauge
        # chart exported as a picture). The notes are ASCII sentences
        # built by the exporter, never user text, so they travel as a header.
        headers[EXPORT_NOTES_HEADER] = json.dumps(list(exported.notes))
    return Response(
        content=exported.data,
        media_type=exported.content_type,
        headers=headers,
    )


@router.post("/{presentation_id}/layout-check", response_model=PresentationLayoutCheckResponse)
async def layout_check_presentation_endpoint(
    presentation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Render the deck headless and report what the audience would see go wrong.

    For whoever may EDIT the deck, like ``get_presentation``'s check:
    every finding is something to trim or split, a read-only reader could act
    on none of them, and each check takes a headless render slot the exports
    share. Nothing is written all the same, and the render runs as the caller.
    The agent gets the same report on every save through
    ``generate_presentation``; this route is for the UI and for a human
    checking a deck they did not just generate.

    A check that could not run (render timed out, no headless browser on this
    deployment, a save landing while the deck rendered) is a 200 with
    ``skipped`` set, not an error: the answer "we could not measure it" is a
    valid answer and the UI shows it as such. The report names the revision it
    measured, which is what the panel marks stale against.
    """
    if not layout_check_enabled():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The presentation layout check is disabled on this deployment.",
        )
    presentation, _group_ids = await _load_manageable(db, user, presentation_id, action="edit")
    report = await check_presentation_layout(
        presentation.id,
        user.id,
        presentation.spec or {},
        expected_revision=presentation.revision,
    )
    return report.as_dict()


@router.post("/{presentation_id}/slide-layout", response_model=PresentationSlideLayoutResponse)
async def switch_slide_layout_endpoint(
    presentation_id: str,
    body: PresentationSlideLayoutRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Rebuild ONE slide as another layout, deterministically.

    The editor panel's per-slide layout picker calls this with the slide as
    the panel holds it (its unsaved draft, not the stored spec) and the
    target layout, and receives the converted slide plus an account of what
    was kept, derived and dropped. Nothing is written: the slide comes back
    to the draft, the person sees it on the stage, and the save is the
    existing PATCH with its ``expected_revision`` - so a conversion that is
    not what they wanted costs a Cancel, never a revision.

    Gated on the manage tier all the same: a read-only viewer has no draft to
    put the result into, and answering them would describe an edit the UI
    never offers. Any layout of the catalog is accepted whatever the deck's
    template advertises - the template's list is a recommendation the picker
    orders by, not a rule; a retired v1 layout is refused as a target,
    and a draft slide still wearing one is upgraded to the current catalog
    before it is converted. A slide that cannot honestly become the target (a
    title slide asked to be an agenda has nothing to list) is a 422 with the
    reason in the person's words, not a slide padded with placeholders.
    """
    await _load_manageable(db, user, presentation_id, action="edit")
    try:
        outcome = switch_slide_layout(body.slide, body.layout)
    except LayoutSwitchError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    return PresentationSlideLayoutResponse(**outcome.as_dict())


# ── Static render routes — render-token only ──────────────────────────
#
# These two routes are what the headless browser behind the export calls. They
# accept ONLY a render token (a confined JWT bound to one deck, see
# ``presentation_export.mint_render_token``); a session JWT is refused here just
# as a render token is refused by ``get_current_user``. The token rides in the
# query string because ``<img src>`` cannot send a header.


def _render_claims(request: Request, presentation_id: str) -> RenderClaims:
    token = request.query_params.get(RENDER_TOKEN_QUERY_PARAM, "")
    if not token:
        authorization = request.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Render token required")
    try:
        claims = decode_render_token(token)
    except RenderTokenError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid render token") from exc
    deck_id = safe_uuid(presentation_id)
    if deck_id is None or claims.presentation_id != deck_id:
        # Bound to another deck: a token is a capability over ONE deck.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    return claims


async def _render_user(db: AsyncSession, claims: RenderClaims) -> User:
    result = await db.execute(select(User).where(User.id == claims.user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid render token")
    return user


@router.get("/{presentation_id}/render-spec", response_model=PresentationResponse)
async def render_spec(
    presentation_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """The deck as the static render page consumes it (render token only)."""
    claims = _render_claims(request, presentation_id)
    user = await _render_user(db, claims)
    presentation, _group_ids, tier = await _load_visible(db, user, claims.presentation_id)
    return PresentationResponse(**await _response(db, presentation, user=user, tier=tier))


@router.get("/{presentation_id}/render-polls")
async def render_polls(
    presentation_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """The live polls of the deck as the export snapshots them (render token only, #10157).

    The same state the slide reads from ``/polls`` (short link, QR, results,
    voters, the quiz answer only once revealed by the block), so a PDF or PPTX
    export draws the link and the results at the time of the export instead
    of a ``/v/...`` placeholder and empty bars.
    """
    from app.services.presentation_polls import deck_poll_states

    claims = _render_claims(request, presentation_id)
    user = await _render_user(db, claims)
    presentation, _group_ids, _tier = await _load_visible(db, user, claims.presentation_id)
    polls = await deck_poll_states(db, presentation)
    await db.commit()
    return {"can_control": False, "polls": polls}


@router.get("/{presentation_id}/render-asset/{file_id}")
async def render_asset(
    presentation_id: str,
    file_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """One file the deck references, for the static render page (render token only).

    Serves a file iff (a) the deck's spec or resolved theme names it — so the
    token cannot be turned into a general file-read credential — AND (b) the
    token's user may read it under the ordinary ``authorize_file_access`` rule,
    the same gate the interactive viewer goes through. Either refusal is the
    unknown-file 404, like an id nobody issued. Answered through the
    FileAttachment byte contract, never stored: the token rides in the
    query string and every export reads the bytes as they are now.
    """
    from app.services.file_access import file_not_found, load_readable_file
    from app.services.file_responses import file_response_headers
    from app.services.file_storage import FileNotFoundInStorageError, file_storage

    claims = _render_claims(request, presentation_id)
    user = await _render_user(db, claims)
    presentation, _group_ids, _tier = await _load_visible(db, user, claims.presentation_id)
    theme_config = await resolve_template_theme(
        db,
        presentation.template_container_id,
        user_id=user.id,
        deck_theme=presentation.theme,
        is_admin=user.is_admin,
    )
    asset_id = parse_path_id(file_id, not_found_detail="File not found")
    if str(asset_id) not in referenced_file_ids(presentation.spec, theme_config):
        raise file_not_found()
    # The deck is the hint: a reader who is not the owner of a slide's
    # picture is granted it because THIS deck shows it, and the page fetches
    # every picture of the deck, so the contract resolves the hinted deck by
    # primary key instead of scanning the reader's decks once per picture.
    file_att = await load_readable_file(db, asset_id, user, presentation_id=presentation.id)
    try:
        data = await file_storage.download(file_att.s3_object_name)
    except FileNotFoundInStorageError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File no longer available") from None
    return Response(
        content=data,
        media_type=file_att.content_type,
        headers=file_response_headers(file_att, store=False),
    )


# ── Live polls (xtm-one #10157) ──────────────────────────────────────────────
# The deck's ``poll`` blocks: every reader of the deck sees their live results
# (the slide polls this every 2 s); only an editor drives them.


class PresentationPollControl(BaseModel):
    action: str = Field(..., description="open | next | lock | unlock | reveal | hide | reset")
    question_id: str | None = Field(default=None, max_length=32)


@router.get("/{presentation_id}/polls")
async def get_presentation_polls(
    presentation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The live state and results of every poll of the deck, for anyone who can read it."""
    from app.services.presentation_polls import deck_poll_states

    presentation, _group_ids, tier = await _load_visible(db, user, presentation_id)
    polls = await deck_poll_states(db, presentation)
    await db.commit()
    return {"can_control": tier in ("owner", "config"), "polls": polls}


@router.post("/{presentation_id}/polls/{poll_id}/control")
async def control_presentation_poll(
    presentation_id: str,
    poll_id: str,
    body: PresentationPollControl,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Open a question, lock, reveal or reset a poll (owner or editor).

    A poll is controlled while the deck is shown to an audience: the deck was
    presented, a use of a deck built in a conversation (#10544, S2c).
    """
    from app.services.presentation_polls import control_poll
    from app.services.roi_chat import KIND_PRESENTATION, SIGNAL_PRESENTED
    from app.services.roi_chat_records import record_signal

    presentation, _group_ids = await _load_manageable(db, user, presentation_id, action="edit")
    try:
        state = await control_poll(db, presentation, poll_id, body.action, body.question_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    await record_signal(db, kind=KIND_PRESENTATION, deliverable_id=presentation.id, signal=SIGNAL_PRESENTED)
    await db.commit()
    return state
