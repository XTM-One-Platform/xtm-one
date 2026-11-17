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

"""Pydantic schemas for the presentation API routes."""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field

from app.services.presentation_spec import DEFAULT_SLIDE_CAP, SHARED_HEROES


class _PresentationSharing(BaseModel):
    """The three-tier visibility fields every shareable entity exposes.

    ``permission`` is the caller's own tier (``owner`` / ``config`` /
    ``read_only``) and ``can_manage`` its boolean form — computed server-side by
    ``presentations.deck_permission`` so the UI never re-derives the rule.
    """

    user_id: str | None = None
    owner_name: str | None = None
    is_company_managed: bool = False
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool = False
    permission: str | None = None
    can_manage: bool = False
    #: A public link opens the deck for anyone, without an account.
    #: Every reader is told; only a manager reads the link itself
    #: (``GET /{id}/public-link``). ``False`` while the admin switch is off.
    has_public_link: bool = False


class PresentationResponse(_PresentationSharing):
    """A deck as consumed by the frontend ``@revealjs/react`` renderer.

    ``spec`` is the validated ``PresentationSpec`` (see
    ``app.services.presentation_spec``) — the frontend still type-guards it
    before rendering, but the backend is the authority on its shape.
    ``theme_config`` carries the whitelisted brand overrides resolved from the
    deck's linked template container for the deck's SAVED theme, empty
    when there is none. ``theme_configs`` is the same palette per colour scheme
    (``dark`` / ``light``): the editor previews a theme switch from it
    on the client, before anything is saved, so the deck turns light the moment
    ``light`` is picked rather than after the save round-trip.

    ``supported_schemes`` is the list the theme picker may offer: both
    schemes for a free deck, only what the linked template can dress otherwise
    (a template supports the scheme its palette is designed for, plus the other
    one when it ships a ``theme_variants`` palette for it). ``template_name``
    labels the linked template, ``None`` for a free deck or one whose template
    the reader may not see.

    ``template_chrome`` is the header / footer pattern that template
    declares (``footer``, ``header``, ``variables``), resolved live on every
    read like the palette: the renderer fills it from the deck's title, its
    opening slide's subtitle, ``chrome.event_label`` and ``chrome.fields``.
    ``None`` for a free deck, an invisible template or a template declaring
    none — the footer then reads the deck's own event label and footer text.

    ``supported_layouts`` is what the editor's per-slide layout picker
    may move a slide to: the linked template's advertised catalog restricted to
    the platform's ``SLIDE_LAYOUTS``, the whole catalog for a free deck (or a
    template the reader may not see). Never empty. The retired v1 layouts
    are never in it, and no served slide wears one: the spec is
    upgraded on read.
    """

    id: str
    title: str
    #: The colour scheme the deck is read in - ``dark`` or ``light``. A
    #: deck stored under the retired ``filigran`` preset is served as ``dark``.
    theme: str
    #: The deck's format (``16:9`` / ``4:3`` / ``A4`` / ``letter``), lifted out
    #: of the spec, and the family it belongs to — ``deck`` (presented: slides,
    #: fullscreen, speaker view) or ``page`` (read: stacked portrait pages,
    #: zoom, print). The frontend picks the viewer from ``kind``.
    aspect_ratio: str = "16:9"
    kind: str = "deck"
    slide_count: int
    #: Bumped on every in-place edit — lets the renderer distinguish "same deck,
    #: new slides" (reconcile) from a different deck (remount).
    revision: int
    conversation_id: str | None = None
    assignment_run_id: str | None = None
    template_container_id: str | None = None
    template_name: str | None = None
    supported_schemes: list[str] = ["dark", "light"]
    #: The template's footer / header pattern and its variables —
    #: ``TemplateChrome.as_dict()``; ``None`` when nothing dresses the footer.
    template_chrome: dict | None = None
    #: The template's design tokens - ``TemplateDesign.as_dict()``;
    #: ``None`` when it declares none (the renderer's own look).
    template_design: dict | None = None
    #: The layouts a slide may be moved to from the editor, catalog order.
    supported_layouts: list[str] = [
        "title",
        "section",
        "agenda",
        "content",
        "roadmap",
        "takeaways",
        "dashboard",
        "closing",
    ]
    #: How many slides the deck may hold: its template's ``max_slides``
    #: (``extra_metadata``), the platform default for a free deck or a template
    #: that sets none. The editor's "Add slide" and Duplicate stop here.
    max_slides: int = DEFAULT_SLIDE_CAP
    #: The covers the hero picker may offer, catalog order: the shared
    #: heroes, plus the template's own (``extra_metadata.heroes``). A cover in
    #: ``spec`` naming another hero is served on the template's default for it.
    supported_heroes: list[str] = list(SHARED_HEROES)
    spec: dict
    theme_config: dict[str, str] = {}
    theme_configs: dict[str, dict[str, str]] = {}
    #: The stored geometry of the logos the deck draws, keyed by the
    #: file id as the spec spells it: ``{w, h, trim: [x0, y0, x1, y1], ink?}``.
    #: The renderer crops each logo to its mark with it; a logo absent here is
    #: drawn as its file.
    asset_meta: dict[str, dict] = {}
    #: What a quote picture no visible credit names is announced as:
    #: ``{file_id: {"description", "filename"}}`` for the quote pictures the
    #: spec references, as far as the reader may be told (``load_picture_facts``).
    picture_labels: dict[str, dict[str, str | None]] = {}
    #: Collaboration facts: who saved the current revision and how
    #: many review threads are still open.
    updated_by_user_id: str | None = None
    updated_by_name: str | None = None
    open_comment_count: int = 0
    created_at: str | None = None
    updated_at: str | None = None


class PresentationSummaryResponse(_PresentationSharing):
    """List-view shape: everything but the (potentially large) spec."""

    id: str
    title: str
    theme: str
    aspect_ratio: str = "16:9"
    kind: str = "deck"
    slide_count: int
    revision: int
    conversation_id: str | None = None
    assignment_run_id: str | None = None
    template_container_id: str | None = None
    #: Name of the template the deck wears, as the reader sees it;
    #: ``None`` for a free deck or a template the reader cannot see.
    template_name: str | None = None
    updated_by_user_id: str | None = None
    updated_by_name: str | None = None
    open_comment_count: int = 0
    created_at: str | None = None
    updated_at: str | None = None


class PresentationLibraryPage(BaseModel):
    """A page of the library: its decks, and how many match in all."""

    items: list[PresentationSummaryResponse]
    total: int


# ── Public link ──────────────────────────────────────────────────────


class PresentationPublicLinkRequest(BaseModel):
    """``POST /{id}/public-link``: turn the link on, or issue it a new token."""

    regenerate: bool = False


class PresentationPublicLinkUpdate(BaseModel):
    """``PATCH /{id}/public-link``: what the link offers besides the slides."""

    allow_pdf_download: bool


class PresentationPublicLinkResponse(BaseModel):
    """A deck's public link, as its managers read it.

    ``available`` is the admin switch: off, a kept link (``enabled``) opens
    nothing and none can be created. ``path`` is the web app page, without the
    base path (the client adds its own); ``url`` the absolute address. Both
    are ``None`` while the link is off, and when ``SECRET_KEY`` no longer
    decrypts the stored token (regenerate it to copy a link again).
    ``allow_pdf_download``: its visitors may also download the PDF.
    """

    available: bool
    enabled: bool
    path: str | None = None
    url: str | None = None
    created_at: str | None = None
    created_by_name: str | None = None
    allow_pdf_download: bool = False


class PublicPresentationResponse(BaseModel):
    """A deck as a public link shows it: what a renderer needs, nothing else.

    A whitelist, on purpose: who owns, edited or may open the deck, its
    conversation and its comments are not part of it, and a field added to
    ``serialize_presentation`` later stays out until it is listed here. The
    spec carries no speaker notes (``presentation_public_links.public_spec``).
    """

    id: str
    title: str
    theme: str
    aspect_ratio: str = "16:9"
    kind: str = "deck"
    slide_count: int
    revision: int
    spec: dict
    theme_config: dict[str, str] = {}
    theme_configs: dict[str, dict[str, str]] = {}
    supported_schemes: list[str] = ["dark", "light"]
    template_chrome: dict | None = None
    template_design: dict | None = None
    asset_meta: dict[str, dict] = {}
    picture_labels: dict[str, dict[str, str | None]] = {}
    updated_at: str | None = None
    #: The link offers the PDF of these slides: the page shows its download.
    pdf_download: bool = False


# ── Collaboration ────────────────────────────────────────────────────


class PresentationVersionResponse(BaseModel):
    """One entry of a deck's revision history.

    ``spec`` is only present on the single-version read (the preview); the
    list carries headlines only. ``source`` is ``agent`` / ``manual`` /
    ``restore`` / ``duplicate``; a ``restore`` names the revision it brought
    back in ``restored_from_revision``. An edit session - one person's
    run of autosaves - began at ``created_at`` and was last saved at
    ``updated_at``, which is ``None`` on every other entry.
    """

    id: str
    presentation_id: str
    revision: int
    title: str
    slide_count: int
    source: str
    summary: str | None = None
    restored_from_revision: int | None = None
    author_id: str | None = None
    author_name: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    spec: dict | None = None


class PresentationCommentResponse(BaseModel):
    """A review note. ``slide_position`` / ``slide_title`` are resolved against
    the CURRENT spec on every read; ``detached`` means the anchored slide is
    gone. ``can_manage`` is the caller's right to edit / resolve / delete it
    (author or deck manager), computed server-side."""

    id: str
    presentation_id: str
    slide_id: str | None = None
    slide_position: int | None = None
    slide_title: str | None = None
    detached: bool = False
    parent_id: str | None = None
    author_id: str | None = None
    author_name: str | None = None
    body: str
    resolved: bool = False
    resolved_at: str | None = None
    resolved_by_name: str | None = None
    can_manage: bool = False
    created_at: str | None = None
    updated_at: str | None = None


class PresentationCommentCreate(BaseModel):
    """``POST /presentations/{id}/comments`` — a note on a slide, the deck, or a reply.

    ``slide_id`` is the slide's STABLE id from the spec (never a position);
    omitted = a comment on the deck as a whole. ``parent_id`` makes it a reply
    and the slide is then inherited from the thread.
    """

    body: str = Field(min_length=1, max_length=4000)
    slide_id: str | None = Field(default=None, max_length=64)
    parent_id: str | None = None


class PresentationCommentUpdate(BaseModel):
    """``PATCH`` — edit the text and / or resolve (``true``) / reopen (``false``)."""

    body: str | None = Field(default=None, min_length=1, max_length=4000)
    resolved: bool | None = None


class PresentationCommentDraftRequest(BaseModel):
    """``POST /presentations/{id}/comments/draft`` — "Generate with AI" in the composer.

    ``brief`` is what the note should say; ``slide_id`` / ``parent_id`` are the
    anchor the composer is on (the slide's STABLE id, or the thread being
    answered — a reply inherits its thread's slide); ``current_text`` is the
    draft already typed, kept and improved rather than replaced. The slide's
    content is read server-side from the deck, never trusted from the client.
    """

    brief: str = Field(min_length=2, max_length=2000)
    slide_id: str | None = Field(default=None, max_length=64)
    parent_id: str | None = None
    current_text: str | None = Field(default=None, max_length=4000)


class PresentationCommentDraftResponse(BaseModel):
    """The drafted note — nothing is stored until the person posts it."""

    body: str
    #: The reply was still cut at its output limit after one retry with twice
    #: the room: the note is returned as drafted, for the reviewer to
    #: finish before posting it.
    truncated: bool = False


class PresentationPresenceHeartbeat(BaseModel):
    """``POST /presentations/{id}/presence`` — what the caller is doing right now.

    ``editing`` means "I am changing this deck in the manual editor" — a save
    is coming or just landed (the editor autosaves). An open panel or
    studio rail with nothing happening in it is not editing. Whether
    the caller's assistant is working on the deck is not claimed here: the
    route reads it off the chat turn marker itself.

    While the editor is open, the beat also says WHERE the caller works: the
    stable id of the slide on their stage and the spec path of the block they
    have selected - drawn on the others' stages. Both are shapes the
    editor writes and nothing else (an id of the server's alphabet, a dotted
    path of names and indexes), so neither carries free text.
    """

    editing: bool = False
    slide_index: int | None = Field(default=None, ge=0, le=10_000)
    slide_id: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    block: str | None = Field(default=None, max_length=200, pattern=r"^[a-z_]+(\.[a-z0-9_]+)*$")


PresentationPresenceActivity = Literal["viewing", "editing", "assistant"]


class PresentationPresenceEntry(BaseModel):
    user_id: str
    name: str | None = None
    #: What this viewer is doing to the deck: ``viewing`` (open, idle),
    #: ``editing`` (unsaved manual edits), ``assistant`` (an agent turn in
    #: flight in one of their conversations bound to the deck, server-verified).
    activity: PresentationPresenceActivity = "viewing"
    #: ``activity != "viewing"`` — kept as the boolean the sort and badge key on.
    editing: bool = False
    slide_index: int | None = None
    #: Where they work while their editor is open: the slide's stable
    #: id and the block selected on it.
    slide_id: str | None = None
    block: str | None = None
    last_seen_at: float
    is_self: bool = False


class PresentationPresenceResponse(BaseModel):
    """The live room plus the cadence the client should keep.

    ``revision`` is the deck's current revision: an editor holding an
    older one reads the deck again at once, so a collaborator's save reaches
    it within one beat rather than at the next deck poll.
    """

    viewers: list[PresentationPresenceEntry]
    heartbeat_seconds: int
    ttl_seconds: int
    revision: int


class PresentationClickerHand(BaseModel):
    """``PUT /presentations/{id}/clicker`` — hand the clicker to someone.

    The person must already be able to read the deck; the caller becomes the
    presenter the commands are relayed to (xtm-one #10552, slice B).
    """

    user_id: uuid.UUID


class PresentationClickerCommandRequest(BaseModel):
    """``POST /presentations/{id}/clicker/command`` — the holder drives."""

    command: Literal["prev", "next", "goto"]
    #: The slide to go to (0-based), for ``goto`` only.
    index: int | None = Field(default=None, ge=0, le=10_000)


class PresentationClickerHolder(BaseModel):
    user_id: str
    name: str | None = None


class PresentationClickerResponse(BaseModel):
    """Who presents and who holds the clicker, as the caller sees it."""

    presenter_id: str | None = None
    holder: PresentationClickerHolder | None = None
    handed_at: float | None = None
    is_presenter: bool = False
    is_holder: bool = False
    #: The slide the presenter is on (0-based), read off their presence beat,
    #: so the holder sees where the talk is.
    slide_index: int | None = None
    ttl_seconds: int


class PresentationUpdate(BaseModel):
    """``PATCH /presentations/{id}`` — the light manual controls.

    Two families of fields, both optional (a field left out is untouched):

    * **Deck content** — ``title``, ``theme``, ``transition``, ``aspect_ratio``,
      ``chrome`` and the FULL ``slides`` list (reorder / delete / edit a field or
      the speaker notes is "send the new list"). They are merged into the stored
      spec and re-validated by ``validate_spec`` exactly like an agent's call, so
      the UI can never introduce a deck shape the renderer would choke on.
      Content edits bump ``revision``; ``expected_revision`` makes the write
      conditional (409 when a collaborator — or the agent — got there first).
    * **Sharing** — the platform's standard fields. They never bump the
      revision: who may see a deck is not a change to the deck.

    There is deliberately no raw ``spec`` field: the spec is only ever written
    as validated pieces, never replaced wholesale from the client.
    """

    title: str | None = Field(default=None, min_length=1, max_length=300)
    #: ``dark`` or ``light``. Under a template, only a scheme the template
    #: supports (``supported_schemes`` on the deck response) - 422 otherwise.
    theme: str | None = None
    transition: str | None = None
    aspect_ratio: str | None = None
    chrome: dict | None = None
    slides: list[dict] | None = None
    #: ``None`` unlinks the template (renderer theme only, both schemes); a UUID
    #: re-points it - the template must be visible to the caller and be a
    #: presentation template, and a theme it cannot dress is snapped to the
    #: scheme it was designed for. Distinguished from "not sent" via
    #: ``model_fields_set``.
    template_container_id: str | None = None
    expected_revision: int | None = Field(default=None, ge=1)

    is_company_managed: bool | None = None
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool | None = None


class PresentationDuplicateRequest(BaseModel):
    """``POST /presentations/{id}/duplicate`` — optional title for the copy."""

    title: str | None = Field(default=None, min_length=1, max_length=300)


class PresentationExportRequest(BaseModel):
    format: str = Field(
        default="pdf",
        description="pdf | pptx (pixel-perfect images) | pptx_native (editable shapes)",
    )


class PresentationLayoutFinding(BaseModel):
    """One thing the audience would see go wrong on one slide."""

    slide_index: int
    slide_number: int
    slide_id: str | None = None
    layout: str | None = None
    title: str = ""
    kind: str = Field(
        description=(
            "fit | clipped | nowrap | overflow | sparse | logo | lines | columns | chart | values | rotated | shrunk | size"
            " | org_scale | org_width | org_label | org_siblings | org_crossing | org_bubble | org_counts"
            " | org_crowded | org_undated | org_hint | unsettled | map_overlap | zone_chips | arch_row | uneven | floating | top_heavy | wrapped"
            " | wrap_title | wrap_legend | unfilled"
        )
    )
    severity: str = Field(description="dense | overloaded | advisory")
    message: str
    #: ``fit``: the ratio the body was shrunk to (0.8 = needed 25% more height);
    #: ``size``: the ratio a block that sets its size was drawn at.
    scale: float | None = None
    amount: int | None = Field(
        default=None,
        description=(
            "fit: the excess height in percent; sparse: the empty share of the slide in percent; "
            "logo: the smallest logo's height in canvas px when dense, the share of their box the logos "
            "cover in percent when advisory, the faintest logo's contrast ratio times 100 when it names a contrast; "
            "lines: the statement's line count; wrapped: the card title's line count; "
            "wrap_title: the slide, panel or chart title's line count; wrap_legend: the legend's row count; "
            "top_heavy and floating: the bare share of the slide in percent; "
            "unfilled: the bare share of the slide (a body) or of the panel (a fill table or map) in percent; "
            "columns: the width the widest column needs in "
            "canvas px; chart: the height the chart is drawn in canvas px; "
            "values: how many of a chart's values are not written; "
            "rotated: the size in canvas px a chart's values are turned on their side in; "
            "org_scale and org_crowded: the seats the org chart draws; org_counts: how many of its teams' "
            "counts do not add up; the other org_* kinds: none; "
            "the others: the cut in canvas px"
        ),
    )


class PresentationLayoutCheckResponse(BaseModel):
    """``POST /presentations/{id}/layout-check`` — the rendered deck, measured.

    ``skipped`` names the reason when the check did not run to completion (the
    render timed out, the headless browser is unavailable, the deck changed
    while it rendered); the findings are then empty and mean nothing. ``unit``
    is the noun the messages use (``slide`` / ``page``, from the deck's kind);
    ``revision`` is the one the render measured, when the page published it.
    """

    slide_count: int
    skipped: str | None = None
    unit: Literal["slide", "page"] = "slide"
    revision: int | None = None
    findings: list[PresentationLayoutFinding] = Field(default_factory=list)


class PresentationSlideLayoutRequest(BaseModel):
    """``POST /presentations/{id}/slide-layout`` — move one slide to another layout.

    ``slide`` is the slide as the editor holds it (the stored one, or the
    panel's unsaved draft of it — the conversion is stateless and writes
    nothing); ``layout`` the destination, one of the deck's
    ``supported_layouts`` or any other current layout. The stored deck is only
    read for the permission check.
    """

    slide: dict = Field(description="the slide to convert, as a PresentationSpec slide object")
    layout: str = Field(min_length=1, max_length=40, description="the target layout id")


class PresentationSlideLayoutResponse(BaseModel):
    """The converted slide plus an honest account of the move.

    ``slide`` validates under the target layout and keeps the source's ``id``,
    ``notes`` and background. ``kept`` names the fields carried over as they
    were, ``derived`` what the target needed and the conversion built from the
    source (a title from the first heading, agenda items from the bullets of a
    list, a default title the layout requires), ``dropped`` what the target
    has no room for — each a short, human-readable sentence the panel shows
    in a toast, so a person knows what to check on the converted slide.
    """

    slide: dict
    kept: list[str] = Field(default_factory=list)
    derived: list[str] = Field(default_factory=list)
    dropped: list[str] = Field(default_factory=list)


class PresentationImportReport(BaseModel):
    """What ``POST /presentations/import`` did with the package.

    ``template`` is ``matched`` (the packaged template exists on this
    platform), ``default`` (it does not — the install's default template was
    used instead) or ``none``. ``assets_matched`` counts references re-pointed
    at an asset this platform already held (a seeded brand logo), and
    ``assets_copied`` those served from the bytes embedded in the package.
    ``warnings`` are human-readable and safe to show as-is.
    """

    assets_total: int = 0
    assets_matched: int = 0
    assets_copied: int = 0
    template: str = "none"
    template_name: str | None = None
    warnings: list[str] = []


class PresentationImportResponse(PresentationResponse):
    """The imported deck plus the report — one round-trip for the library page."""

    import_report: PresentationImportReport
