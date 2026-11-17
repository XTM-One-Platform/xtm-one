# SPDX-License-Identifier: Apache-2.0

"""The single authorization contract for reading a ``FileAttachment``.

Extracted from ``app.api.v1.chat`` so a non-HTTP caller can enforce the same
rule. The download endpoint was the only consumer while files were only ever
read over HTTP; ``attach_template_asset`` now needs it too, because an agent
naming an arbitrary file id is exactly the case the contract exists for — a file
the acting user cannot download must not be launderable into a template
container that other people can read.

``app.api.v1.chat`` keeps its ``_authorize_file_access`` name as an alias, so
this is a move, not a second implementation: there is one place where "may this
user read these bytes?" is answered.

It is also the one place that decides what a refusal LOOKS like: a caller who
may not read a file gets exactly the answer given for an id nobody issued - the
same 404, the same body - so a file route never confirms which ids exist to
someone it would not show them to. :func:`load_readable_file` (and
:func:`load_readable_chat_image` for the legacy DB-stored images) is what a
route calls; it answers a malformed, an unknown and an unreadable id alike.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import Text, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import safe_uuid
from app.models.chat import Conversation
from app.models.user import User

#: What a route serving or acting on ONE stored file answers for a file id the
#: caller may not read - word for word its answer for an id nobody issued.
FILE_NOT_FOUND_DETAIL = "File not found"

#: The same answer for the legacy DB-stored chat images (``/chat/images/{id}``).
IMAGE_NOT_FOUND_DETAIL = "Image not found"


def file_not_found(detail: str = FILE_NOT_FOUND_DETAIL) -> HTTPException:
    """The unknown-file 404, which is also the answer for a file the caller may not read."""
    return HTTPException(status_code=404, detail=detail)

# Rows the deck-reference scan fetches per round-trip. A PAGE, not a cap: the
# scan walks every candidate the SQL pre-filter admits, a page at a time, and
# stops at the first deck that structurally names the file. The common case is
# one page holding one candidate; a UUID pasted into the prose of many decks
# costs its reader more pages, never a wrong answer.
PRESENTATION_SCAN_PAGE_SIZE = 25


async def authorize_file_access(
    file_att: object,
    user: User,
    db: AsyncSession,
    *,
    presentation_id: uuid.UUID | None = None,
) -> None:
    """Raise the unknown-file 404 unless *user* may read *file_att* (:func:`can_read_file`).

    The refusal is :func:`file_not_found`, never a 403: a 403 would confirm the
    id is real to a caller who may not read it. A caller that must word its own
    answer (a tool, a validator) asks :func:`can_read_file` and gives the same
    answer for a missing row and an unreadable one.
    """
    if not await can_read_file(file_att, user, db, presentation_id=presentation_id):
        raise file_not_found()


async def load_readable_file(
    db: AsyncSession,
    file_id: str | uuid.UUID | None,
    user: User,
    *,
    presentation_id: uuid.UUID | None = None,
):
    """The ``FileAttachment`` *file_id* names, iff *user* may read it.

    What a route serving or acting on one file calls: a malformed id, an id
    nobody issued and an id naming a row *user* may not read all raise the
    same :func:`file_not_found`, so the answer never tells them apart.
    """
    from app.models.file_attachment import FileAttachment

    file_uuid = file_id if isinstance(file_id, uuid.UUID) else safe_uuid(file_id)
    row = None
    if file_uuid is not None:
        row = (
            await db.execute(select(FileAttachment).where(FileAttachment.id == file_uuid))
        ).scalar_one_or_none()
    if row is None or not await can_read_file(row, user, db, presentation_id=presentation_id):
        raise file_not_found()
    return row


async def can_read_chat_image(image: object, user: User, db: AsyncSession) -> bool:
    """Whether *user* may read a legacy DB-stored ``ChatImage``.

    Its owner and admins; a row of a conversation under :func:`can_read_file`'s
    conversation rule - whoever may read its conversation, owner or anyone it
    is shared with, whoever made it. A row of no one and of no conversation is
    admins-only, like an orphan file.
    """
    from app.api.v1.chat import conversation_access

    owner_id = getattr(image, "user_id", None)
    if owner_id == user.id or user.role == "admin":
        return True
    conversation_id = getattr(image, "conversation_id", None)
    if conversation_id is None:
        return False
    conversation = (
        await db.execute(select(Conversation).where(Conversation.id == conversation_id))
    ).scalar_one_or_none()
    return conversation is not None and bool(await conversation_access(db, user, conversation))


async def load_readable_chat_image(db: AsyncSession, image_id: str | None, user: User):
    """The ``ChatImage`` *image_id* names, iff *user* may read it; one 404 otherwise."""
    from app.models.chat_image import ChatImage

    image_uuid = safe_uuid(image_id)
    image = None
    if image_uuid is not None:
        image = (
            await db.execute(select(ChatImage).where(ChatImage.id == image_uuid))
        ).scalar_one_or_none()
    if image is None or not await can_read_chat_image(image, user, db):
        raise file_not_found(IMAGE_NOT_FOUND_DETAIL)
    return image


async def can_read_file(
    file_att: object,
    user: User,
    db: AsyncSession,
    *,
    presentation_id: uuid.UUID | None = None,
) -> bool:
    """Whether *user* may read a ``FileAttachment`` row.

    Authorization paths (any one is sufficient):

    * **Owner / admin** — ``file_att.user_id == user.id`` or
      ``user.role == "admin"``.
    * **Conversation-scoped** — a file of a conversation is reachable if the
      requester may read that conversation: its owner, or anyone it is shared
      with, whoever uploaded or generated the file. An attachment is part of
      the transcript, as the message-scoped download says; a reader of a
      shared conversation downloads the owner's files through this id-scoped
      route too.
    * **Assignment-run-scoped fallback** — when ``file_att.user_id`` is
      ``NULL``, the file is reachable if the requester satisfies
      :func:`require_run_access` on the linked assignment run. Used by
      assignment-engine ``generate_file`` outputs and custom-tool
      ``$output_files`` when ``run.user_id`` is ``NULL`` (company-managed
      runs) — those legitimately can't carry a user_id but must still be
      gated to admins / run owners.
    * **Template asset** — a row whose ``source`` is ``template_asset`` is
      readable by everyone who can see its container.
    * **Shown by a readable deck** — a file a slide of a presentation the
      requester may READ names as ``file_id`` / ``*_file_id``. See
      :func:`_readable_presentation_names_file`.

    ``presentation_id`` is a HINT for that last path: the deck the request is
    FOR, when the caller knows it (the static render page behind the export and
    the post-save layout check fetches every picture of ONE deck). The hinted
    deck is tried first, by primary key under the same visibility predicate, so
    those callers never pay the text scan; a hint that does not pan out falls
    through to the scan and never refuses on its own.

    Files with ``user_id IS NULL`` AND no scope binding (orphan files —
    e.g. dropped FK from a deleted conversation / run) that no readable deck
    names are rejected so the legacy permissive ``NULL means everyone``
    semantics can never resurface.
    """
    # All imported inside the function. ``conversation_access`` in particular
    # lives in ``app.api.v1.chat``, which imports this module at module level to
    # alias ``_authorize_file_access`` — a top-level import would close that
    # cycle.
    from app.api.v1.chat import conversation_access
    from app.api.v1.helpers import get_user_group_ids, require_run_access
    from app.models.assignment import AssignmentRun

    if file_att.user_id == user.id or user.role == "admin":
        return True

    # The two remaining paths are visibility-scoped (template container, deck)
    # and share the requester's group memberships: looked up once, on demand.
    group_ids: list[str] | None = None

    async def _groups() -> list[str]:
        nonlocal group_ids
        if group_ids is None:
            group_ids = await get_user_group_ids(db, user.id)
        return group_ids

    async def _conversation_reader() -> bool:
        # Owner *or* anyone the conversation is shared with, whoever's file it
        # is. Without the share branch a read-only viewer would see the file
        # chip in the transcript and be refused on click: the chat panel
        # downloads every attachment through this id-scoped route.
        _conv_result = await db.execute(
            select(Conversation).where(Conversation.id == file_att.conversation_id)
        )
        _conv = _conv_result.scalar_one_or_none()
        return _conv is not None and bool(await conversation_access(db, user, _conv))

    if file_att.user_id is None:
        if file_att.conversation_id is not None and await _conversation_reader():
            return True
        if file_att.assignment_run_id is not None:
            _run_result = await db.execute(
                select(AssignmentRun).where(
                    AssignmentRun.id == file_att.assignment_run_id
                )
            )
            _run = _run_result.scalar_one_or_none()
            if _run is not None:
                # ``require_run_access`` raises 403/404 if the user can't
                # access the run; treat any raise as "no scope match" so
                # callers can't enumerate run existence via this
                # endpoint.
                try:
                    require_run_access(_run, user, action="download files from")
                    return True
                except HTTPException:
                    pass
    # Template-asset path — checked regardless of ``user_id``,
    # because a template asset is OWNED by whoever uploaded it but SHARED through
    # its container's visibility. Without this, a deck built from a
    # company-managed brand template renders every one of its images as a 403 for
    # everybody except the admin who uploaded them, and the failure is silent (a
    # slide image that won't load just doesn't appear).
    #
    # The grant is exactly the container's own three-tier visibility (own +
    # group-shared + company-managed): reading a template's logo is no more
    # privileged than reading the template metadata that references it, which
    # ``search_templates`` already returns to the same user.
    if getattr(file_att, "template_container_id", None) is not None:
        from app.api.v1.helpers import user_visible_filter
        from app.models.template_container import (
            TEMPLATE_ASSET_SOURCE,
            TemplateContainer,
        )

        # ``source`` is re-checked so only a row that really is a template asset
        # can take this path — a stray FK on some other attachment must not
        # inherit the container's visibility.
        if getattr(file_att, "source", None) == TEMPLATE_ASSET_SOURCE:
            _tpl_result = await db.execute(
                select(TemplateContainer.id).where(
                    TemplateContainer.id == file_att.template_container_id,
                    user_visible_filter(TemplateContainer, user.id, await _groups()),
                )
            )
            if _tpl_result.scalar_one_or_none() is not None:
                return True
    # Deck-reference path — checked regardless of ``user_id``. A picture
    # an agent generated for a slide is OWNED by the person whose agent produced
    # it and bound to THEIR conversation; sharing the deck shares the
    # spec, which only NAMES the file by id. Without this path every reader of a
    # shared deck who is not its owner got a 403 on every generated picture — in
    # the viewer, the chat card, the speaker view and the export alike, since
    # they all end here — and the failure was silent: a slide with a hole where
    # the image was.
    #
    # The grant is exactly the deck's own three-tier visibility (own + shared
    # with the user or one of their groups + company-managed): fetching a
    # picture a slide shows is no more privileged than reading the slide. It is
    # evaluated against the STORED spec at request time, so a deck shared before
    # this path existed is fixed by deploying it — no migration, no re-save by
    # its author. What keeps the grant honest is the other half of the file
    # contract: every write that can INTRODUCE a reference verifies it against
    # the writer (``verify_file_references``), so a foreign file id typed into a
    # deck is refused before it is ever stored.
    file_id = _file_uuid(file_att)
    if file_id is not None and await _readable_presentation_names_file(
        db, file_id, user, await _groups(), presentation_id=presentation_id
    ):
        return True
    # Someone's file of a conversation the requester may read (the owner-less
    # one was tried above): last, as a file a deck shows is more often asked for
    # by the deck's readers than by the conversation's. Never an internal one (a
    # checkpoint spill keeps a tool result whole): a reader gets what the
    # transcript shows, as the conversation's file list does.
    from app.llm.base import is_internal_file_source

    return (
        file_att.user_id is not None
        and getattr(file_att, "conversation_id", None) is not None
        and not is_internal_file_source(getattr(file_att, "source", None))
        and await _conversation_reader()
    )


def _file_uuid(file_att: object) -> uuid.UUID | None:
    """The row's id as a UUID, or ``None`` when it is not one (never referenced then)."""
    value = getattr(file_att, "id", None)
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


async def _readable_presentation_names_file(
    db: AsyncSession,
    file_id: uuid.UUID,
    user: User,
    group_ids: list[str],
    *,
    presentation_id: uuid.UUID | None = None,
) -> bool:
    """Does a presentation *user* may read name *file_id* on one of its slides?

    Two steps, because ``presentations.spec`` is a plain ``json`` column with no
    index on the ids buried in it:

    1. SQL narrows the decks to those the requester may READ
       (``user_visible_filter`` — the same predicate behind the library list, so
       an invisible deck grants nothing) whose spec TEXT contains the id, in
       either spelling a UUID may take. The text match is a pre-filter, nothing
       more: it is case-insensitive because a stored id may be upper-cased, and
       it would also match an id pasted into a slide's prose.
    2. Python confirms STRUCTURALLY (``spec_references_file``: a ``file_id`` /
       ``*_file_id`` field on a slide — the exact walk ``verify_file_references``
       vouched for when the reference was written). A mention grants nothing.

    The scan is EXHAUSTIVE and paged: candidates are read
    ``PRESENTATION_SCAN_PAGE_SIZE`` at a time in id order (keyset, so a deck
    saved mid-scan cannot shift a page) and the walk stops at the first deck
    that names the file — the answer never depends on which candidates happen
    to be the most recent, and a file shown by one deck among many that merely
    mention it is still granted. When the caller names the deck the request is
    for (``presentation_id``), that deck is tried first by primary key under the
    same predicate; only a miss falls through to the scan.

    Only the CURRENT spec is consulted: a version snapshot that still names a
    picture the deck has since dropped does not keep it readable for the deck's
    readers — that walk would multiply the scan by the retained history, and the
    history panel's preview of such a slide shows the owner the image and the
    reader a hole, which is a bounded, documented gap. The same rule means a
    ``config`` grantee restoring a revision that brings such a picture back is
    refused when they cannot read it (the restore verifies what it re-introduces
    against the restorer); the owner can.

    The grant follows the DECK, not the picture's owner: nothing here consults
    ``file_attachments``, so a picture a third party generated is served to the
    deck's readers exactly like the owner's own (pinned by
    ``test_the_grant_follows_the_deck_not_the_picture_s_owner``). The residual
    this leaves for rows the REST editor or a restore stored unverified
    is recorded in ``dev-docs/presentations.md`` ("What the grant
    trusts"); one thing narrows it further: the only in-band surface that ever
    handed a non-admin the id of a file they could not read was a deck - or a
    retained revision of one - they could read at the time, which this path now
    grants by design, so such a write added nothing the same writer could not
    have added through the verified tool while that deck was visible.

    Cost: the pre-filter is a sequential ``ILIKE`` pass over the TEXT of every
    deck the requester may read, per request that reaches this path - per
    picture, per page load, for every reader who is neither owner nor admin and
    whose caller has no deck to hint (the browser then holds a picture written
    once for a day, ``Cache-Control: private, max-age=86400``; a template asset,
    replaceable in place, is revalidated on every use and costs this pass each
    time its container's visibility does not already grant it - see
    ``app.services.file_responses``). A few dozen visible decks of a few tens of
    KB is milliseconds; the pass grows linearly with both, and at ten thousand
    visible decks of 200 KB it is a 2 GB read per picture and no longer
    acceptable. The answer at that scale is the reverse-reference table named
    in the dev-doc, not a positive cache inside this contract:
    ``verify_file_references`` calls the same function, so a cached "yes" would
    let a writer re-introduce a picture whose deck was un-shared seconds earlier
    for the cache's whole lifetime.
    """
    from app.api.v1.helpers import user_visible_filter
    from app.models.presentation import Presentation
    from app.services.presentations import spec_references_file

    visible = user_visible_filter(Presentation, user.id, group_ids)
    if presentation_id is not None:
        hinted = await db.execute(
            select(Presentation.spec).where(Presentation.id == presentation_id, visible)
        )
        spec = hinted.scalar_one_or_none()
        if spec is not None and spec_references_file(spec, file_id):
            return True

    spec_text = cast(Presentation.spec, Text)
    mentions = or_(spec_text.ilike(f"%{file_id}%"), spec_text.ilike(f"%{file_id.hex}%"))
    after: uuid.UUID | None = None
    while True:
        page = select(Presentation.id, Presentation.spec).where(visible, mentions)
        if after is not None:
            page = page.where(Presentation.id > after)
        rows = (
            await db.execute(page.order_by(Presentation.id).limit(PRESENTATION_SCAN_PAGE_SIZE))
        ).all()
        for deck_id, spec in rows:
            if spec_references_file(spec, file_id):
                return True
            after = deck_id
        if len(rows) < PRESENTATION_SCAN_PAGE_SIZE:
            return False


__all__ = [
    "FILE_NOT_FOUND_DETAIL",
    "IMAGE_NOT_FOUND_DETAIL",
    "PRESENTATION_SCAN_PAGE_SIZE",
    "authorize_file_access",
    "can_read_chat_image",
    "can_read_file",
    "file_not_found",
    "load_readable_chat_image",
    "load_readable_file",
]
