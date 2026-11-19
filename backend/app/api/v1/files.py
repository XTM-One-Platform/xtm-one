# SPDX-License-Identifier: Apache-2.0

"""Files — the cross-context repository behind Work > Files.

Every ``FileAttachment`` a user can *download* today is scattered behind the
context that produced it: the files panel of one conversation, the attachments
card of one assignment run, a dataset, a tool test. This router is the single
list over all of them — "what did my agents and I put in files, anywhere?" —
filterable by where the file lives (``context``), how it got there
(``origin``), and its name.

Scope is deliberately **the download contract, not the admin one**. The list
predicate is derived from the two download endpoints' authorization
(``authorize_file_access`` for ``/chat/files/{id}/download`` and the transcript
rule of ``download_message_file``), so a row that is listed is a row that can
be fetched — never a chip that 403s on click. Two consequences worth stating:

* **No ambient admin read of other people's conversation files.** An admin
  reaches another user's conversation files only through the audited
  break-glass gate on the conversation itself. A cross-conversation
  list that admitted admins would be that gate's bypass, so here an admin sees
  conversation-bound files exactly as any user does: their own conversations
  and the ones shared with them. Admins do see the outputs of company-managed
  runs, because ``require_run_access`` grants them that on the run itself.
* **Template assets are excluded.** They are a design resource managed from
  their template (Agent Studio > Templates), not something an agent produced,
  and a fresh install seeds two hundred of them — listing those here would
  bury every real file under brand logos.

Deletion is narrower than reading: the file's owner, the owner of the
conversation it lives in, or whoever ``require_run_access`` admits on the run
it was produced by. A shared viewer reads, never deletes. The S3 object goes
with the row (through ``delete_file_attachments``, the one cascade helper), and
a message or deck that still references the id degrades to the existing
"file no longer available" state rather than breaking.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.v1.chat import (
    LINK_ACCESS_ORG_READ,
    _conversation_file_origin,
    _listed_file_predicate,
    _origin_sql_predicate,
)
from app.api.v1.helpers import parse_path_id, parse_uuid, require_run_access
from app.core.database import get_db
from app.models.agent import Agent
from app.models.assignment import Assignment, AssignmentRun
from app.models.chat import Conversation
from app.models.conversation_share import ConversationShare
from app.models.file_attachment import FileAttachment
from app.models.group import GroupMember
from app.models.template_container import TEMPLATE_ASSET_SOURCE
from app.models.user import User
from app.services.audit import log_audit
from app.services.chat_messages import attachment_download_url
from app.services.visibility import SegmentCounts, Visibility, resolve_visibility, segment_counts

logger = logging.getLogger(__name__)

router = APIRouter()

DEFAULT_FILES_PAGE_SIZE = 50
MAX_FILES_PAGE_SIZE = 200

#: Where a file lives. ``conversation`` and ``run`` are the two bindings the
#: model carries; ``other`` is every unbound row — dataset uploads, tool-test
#: inputs, flow-run uploads, channel attachments that never reached a
#: conversation. Surfaced rather than hidden, same rule as the origin
#: ``other`` bucket in ``chat.py``: an unclassified row must stay visible.
FileContext = Literal["conversation", "run", "other"]
FileOrigin = Literal["uploaded", "generated", "other"]
FileOwnership = Literal["mine", "shared"]


class FileItem(BaseModel):
    id: str
    filename: str
    content_type: str
    size: int
    created_at: str
    #: Raw ``FileAttachment.source`` — the UI labels the ``other`` context by
    #: it ("Dataset", "Tool test", "Flow", "Channel").
    source: str
    origin: FileOrigin
    context: FileContext
    conversation_id: str | None = None
    conversation_title: str | None = None
    assignment_run_id: str | None = None
    #: The assignment's name, or the agent's when the run had no assignment
    #: (manual / follow-up / sub-agent runs).
    run_label: str | None = None
    agent_name: str | None = None
    download_url: str
    can_delete: bool
    #: The tombstone (#396): the bytes were deleted (``deleted_reason``:
    #: retention | owner_deleted | admin); the row stays so the page shows it.
    deleted_at: str | None = None
    deleted_reason: str | None = None
    #: When the file goes if nobody uses it again (its category's retention);
    #: ``None`` when it is kept for good or already deleted.
    expires_at: str | None = None


class FilesResponse(BaseModel):
    files: list[FileItem]
    total: int


def file_context(row: FileAttachment) -> FileContext:
    """Bucket a row by the binding it carries — conversation first.

    A generated file is bound to exactly one of the two (see
    ``persist_generated_file``); the precedence only matters for a row that
    somehow carries both, and the transcript is then the place a person would
    look for it.
    """
    if row.conversation_id is not None:
        return "conversation"
    if row.assignment_run_id is not None:
        return "run"
    return "other"


def context_sql_predicate(context: FileContext | None):
    """SQL twin of :func:`file_context` — ``None`` means no filter.

    Kept in lockstep so filtering and labelling partition the set the same way:
    ``conversation + run + other == all`` must hold, or a row would be labelled
    one thing and filtered as another.
    """
    if context is None:
        return None
    if context == "conversation":
        return FileAttachment.conversation_id.is_not(None)
    if context == "run":
        return and_(
            FileAttachment.conversation_id.is_(None),
            FileAttachment.assignment_run_id.is_not(None),
        )
    return and_(
        FileAttachment.conversation_id.is_(None),
        FileAttachment.assignment_run_id.is_(None),
    )


def readable_conversation_ids(user_id: uuid.UUID):
    """Conversations whose transcript — hence whose files — *user_id* may read.

    Mirrors ``chat.conversation_access`` (owner, named share to the user or one
    of their groups, org-wide link) as one subquery, so the list is scoped by
    the same rule the message-scoped download enforces. Group membership is a
    correlated subquery rather than a pre-fetched list, as in
    ``_has_conversation_share``.
    """
    member_group_ids = select(GroupMember.group_id).where(GroupMember.user_id == user_id)
    shared_conversation_ids = select(ConversationShare.conversation_id).where(
        or_(
            ConversationShare.shared_with_user_id == user_id,
            ConversationShare.shared_with_group_id.in_(member_group_ids),
        )
    )
    return select(Conversation.id).where(
        or_(
            Conversation.user_id == user_id,
            Conversation.link_access == LINK_ACCESS_ORG_READ,
            Conversation.id.in_(shared_conversation_ids),
        )
    )


def readable_run_ids(user: User):
    """Runs whose files this list shows *user* — never wider than ``require_run_access``.

    Deliberately NOT ``run_visible_filter``: that predicate lets every user
    *see* company-managed runs in Operations, while ``require_run_access``
    only lets admins download from them. Listing by the wider rule would show
    non-admins files they cannot fetch. An admin may act on every run, but this
    is a library list: it keeps their own runs and the company-managed ones,
    and a colleague's run files are reached from that run.
    """
    if user.role == "admin":
        return select(AssignmentRun.id).where(
            or_(
                AssignmentRun.user_id == user.id,
                AssignmentRun.is_company_managed.is_(True),
            )
        )
    return select(AssignmentRun.id).where(
        AssignmentRun.user_id == user.id,
        AssignmentRun.is_company_managed.is_not(True),
    )


def readable_files_filter(user: User):
    """Every ``FileAttachment`` row *user* may download, as one predicate.

    Derived branch by branch from ``authorize_file_access`` and
    ``download_message_file``:

    * own rows (``user_id == me``) — the owner branch;
    * rows in a conversation I may read — a shared viewer downloads through the
      message-scoped URL when ``message_id`` is set, and through the id-scoped
      one only when ``user_id`` is NULL (the conversation fallback), so a
      non-admin's branch requires one of the two. An admin's id-scoped download
      admits any row, so the extra condition is dropped for them;
    * rows produced by a run I may access — id-scoped, conversation-less,
      ``user_id`` NULL for a non-admin (the run fallback), any row for an admin.

    Template assets are excluded outright (see the module docstring).
    """
    conversation_bound = FileAttachment.conversation_id.in_(readable_conversation_ids(user.id))
    run_bound = FileAttachment.assignment_run_id.in_(readable_run_ids(user))
    if user.role == "admin":
        conversation_branch = conversation_bound
        run_branch = run_bound
    else:
        conversation_branch = and_(
            conversation_bound,
            or_(
                FileAttachment.message_id.is_not(None),
                FileAttachment.user_id.is_(None),
            ),
        )
        run_branch = and_(FileAttachment.user_id.is_(None), run_bound)
    return and_(
        or_(
            FileAttachment.source.is_(None),
            FileAttachment.source != TEMPLATE_ASSET_SOURCE,
        ),
        or_(FileAttachment.user_id == user.id, conversation_branch, run_branch),
    )


def mine_files_filter(user_id: uuid.UUID):
    """Rows attributed to me: uploaded by me, in my conversation, or from my run.

    Every term is NULL-guarded so the predicate is TRUE or FALSE, never NULL:
    a run's file has no ``user_id`` and no ``conversation_id``, and an
    unguarded ``user_id = me OR conversation_id IN (...)`` evaluated to NULL on
    it, so ``NOT mine`` was NULL too and the row fell in no segment (All
    counted it, Mine, Shared and Company did not).
    """
    return or_(
        and_(FileAttachment.user_id.is_not(None), FileAttachment.user_id == user_id),
        and_(
            FileAttachment.conversation_id.is_not(None),
            FileAttachment.conversation_id.in_(select(Conversation.id).where(Conversation.user_id == user_id)),
        ),
        and_(
            FileAttachment.assignment_run_id.is_not(None),
            FileAttachment.assignment_run_id.in_(
                select(AssignmentRun.id).where(AssignmentRun.user_id == user_id)
            ),
        ),
    )


def company_files_filter(user_id: uuid.UUID | None = None) -> ColumnElement[bool]:
    """Rows produced by a company-managed run: the Company segment of the list.

    With *user_id*, a run of an agent whose activity is hidden from that
    non-admin (#11624) is left out, like :func:`run_visible_filter`.
    """
    from app.services.activity_visibility import agent_activity_shown

    runs = select(AssignmentRun.id).where(AssignmentRun.is_company_managed.is_(True))
    if user_id is not None:
        runs = runs.where(agent_activity_shown(AssignmentRun.agent_id, user_id))
    return and_(
        FileAttachment.assignment_run_id.is_not(None),
        FileAttachment.assignment_run_id.in_(runs),
    )


def can_delete_file(
    row: FileAttachment,
    user: User,
    *,
    conversation_owner_id: uuid.UUID | None,
    run: AssignmentRun | None,
) -> bool:
    """Whether *user* may delete *row* — narrower than reading, on purpose.

    Owner of the file; owner of the conversation it lives in; or whoever
    ``require_run_access`` admits on the run that produced it (the run owner,
    or an admin). A shared viewer of a conversation reads its files and deletes
    none of them, and an admin gets no ambient delete over another user's
    conversation — the same line the list draws for reading.
    """
    if row.source == TEMPLATE_ASSET_SOURCE:
        return False
    if row.user_id is not None and row.user_id == user.id:
        return True
    if row.conversation_id is not None and conversation_owner_id is not None and conversation_owner_id == user.id:
        return True
    if row.assignment_run_id is not None and run is not None:
        try:
            require_run_access(run, user, action="delete files from")
            return True
        except HTTPException:
            return False
    return False


def download_url_for(row: FileAttachment, user: User) -> str:
    """The download URL the requester can actually use for *row*.

    Same rule as ``list_conversation_files``: a conversation-bound row with a
    ``message_id`` gets the message-scoped URL (authorized by transcript
    access — the path a shared viewer needs), everything else the id-scoped one.
    An admin who is not the conversation's owner is not on the message-scoped
    path's owner/share list unless shared, but the id-scoped endpoint's admin
    branch admits them, so they get that URL instead.
    """
    if row.conversation_id is not None and row.message_id is not None and user.role != "admin":
        return attachment_download_url(
            str(row.id),
            conversation_id=str(row.conversation_id),
            message_id=str(row.message_id),
        )
    return attachment_download_url(str(row.id))


async def _load_context_labels(
    db: AsyncSession, rows: list[FileAttachment]
) -> tuple[
    dict[uuid.UUID, tuple[str, uuid.UUID | None]], dict[uuid.UUID, tuple[AssignmentRun, str | None, str | None]]
]:
    """Titles and owners for every conversation / run the page references.

    Two set-based lookups for the page rather than one per row.
    """
    conv_ids = {r.conversation_id for r in rows if r.conversation_id is not None}
    run_ids = {r.assignment_run_id for r in rows if r.assignment_run_id is not None}

    conversations: dict[uuid.UUID, tuple[str, uuid.UUID | None]] = {}
    if conv_ids:
        result = await db.execute(
            select(Conversation.id, Conversation.title, Conversation.user_id).where(Conversation.id.in_(conv_ids))
        )
        for conv_id, title, owner_id in result.all():
            conversations[conv_id] = (title, owner_id)

    runs: dict[uuid.UUID, tuple[AssignmentRun, str | None, str | None]] = {}
    if run_ids:
        result = await db.execute(
            select(AssignmentRun, Assignment.name, Agent.name)
            .outerjoin(Assignment, Assignment.id == AssignmentRun.assignment_id)
            .outerjoin(Agent, Agent.id == AssignmentRun.agent_id)
            .where(AssignmentRun.id.in_(run_ids))
        )
        for run, assignment_name, agent_name in result.all():
            runs[run.id] = (run, assignment_name, agent_name)
    return conversations, runs


def _serialize(
    row: FileAttachment,
    user: User,
    conversations: dict[uuid.UUID, tuple[str, uuid.UUID | None]],
    runs: dict[uuid.UUID, tuple[AssignmentRun, str | None, str | None]],
    rules: Mapping[str, Any] | None = None,
) -> FileItem:
    from app.services.file_retention import category_of, expires_at

    conv = conversations.get(row.conversation_id) if row.conversation_id else None
    category = category_of(row.source)
    rule = rules.get(category.key) if (rules is not None and category is not None) else None
    expiry = expires_at(row, rule.days, category.basis) if (rule is not None and category is not None) else None
    run_entry = runs.get(row.assignment_run_id) if row.assignment_run_id else None
    run = run_entry[0] if run_entry else None
    assignment_name = run_entry[1] if run_entry else None
    agent_name = run_entry[2] if run_entry else None
    return FileItem(
        id=str(row.id),
        filename=row.filename,
        content_type=row.content_type or "application/octet-stream",
        size=row.size or 0,
        created_at=row.created_at.isoformat(),
        source=row.source or "",
        origin=_conversation_file_origin(row.source),
        context=file_context(row),
        conversation_id=str(row.conversation_id) if row.conversation_id else None,
        conversation_title=conv[0] if conv else None,
        assignment_run_id=str(row.assignment_run_id) if row.assignment_run_id else None,
        run_label=(assignment_name or agent_name) if run_entry else None,
        agent_name=agent_name,
        download_url=download_url_for(row, user),
        can_delete=can_delete_file(
            row,
            user,
            conversation_owner_id=conv[1] if conv else None,
            run=run,
        ),
        deleted_at=row.deleted_at.isoformat() if getattr(row, "deleted_at", None) else None,
        deleted_reason=getattr(row, "deleted_reason", None),
        expires_at=expiry.isoformat() if expiry else None,
    )


@router.get("", response_model=FilesResponse)
async def list_files(
    limit: int = Query(default=DEFAULT_FILES_PAGE_SIZE, ge=1, le=MAX_FILES_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    search: str | None = Query(default=None, max_length=200),
    context: FileContext | None = Query(default=None, description="Where the file lives (omit for everywhere)."),
    origin: FileOrigin | None = Query(default=None, description="How the file got there (omit for any)."),
    ownership: FileOwnership | None = Query(
        default=None,
        description=(
            "[DEPRECATED] use `vis`. `mine` = uploaded by me, in my conversations or from my runs; "
            "`shared` = everything else I can read. Omit for both."
        ),
    ),
    vis: str | None = Query(
        default=None,
        description=(
            "`mine` | `shared` (not mine, not from a company-managed run) | "
            "`company` (from a company-managed run, not mine) | `all`. Wins over `ownership`."
        ),
    ),
    conversation_id: str | None = Query(default=None),
    assignment_run_id: str | None = Query(default=None),
    status: Literal["live", "deleted", "all"] = Query(
        default="all",
        description="`live`: files whose content is kept; `deleted`: tombstones (#396); `all`: both.",
    ),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Every file the requester can download, newest first, across contexts.

    ``total`` is the size of the *filtered* set. The two id filters narrow to
    one context (a deep link from a conversation or a run); they are applied
    on top of the readability predicate, never instead of it, so naming a
    conversation you cannot read returns an empty page rather than its files.
    """
    where = _file_filters(
        user,
        segment=resolve_visibility(vis, ownership, default="all"),
        # The [DEPRECATED] `ownership=shared` meant every file not mine; the
        # Shared segment leaves the company-managed run files to Company.
        shared_excludes_company=vis is not None and bool(vis.strip()),
        search=search,
        context=context,
        origin=origin,
        conversation_id=conversation_id,
        assignment_run_id=assignment_run_id,
    )
    # Called directly (a test, another route), the Query default is not resolved: all.
    if status == "live":
        where = [*where, FileAttachment.deleted_at.is_(None)]
    elif status == "deleted":
        where = [*where, FileAttachment.deleted_at.is_not(None)]

    total = (await db.execute(select(func.count()).select_from(FileAttachment).where(*where))).scalar_one()

    # ``id`` as the tiebreaker, as in ``list_conversation_files``: an agent
    # writing several files in one turn collides on ``created_at``.
    result = await db.execute(
        select(FileAttachment)
        .where(*where)
        .order_by(FileAttachment.created_at.desc(), FileAttachment.id.desc())
        .offset(offset)
        .limit(limit)
    )
    page = list(result.scalars().all())
    conversations, runs = await _load_context_labels(db, page)
    from app.services.file_retention import category_of, load_policy

    # The retention rules give the "Expires" column; read only when a row of the page has a category.
    rules = await load_policy(db) if any(category_of(row.source) for row in page) else None
    return FilesResponse(
        files=[_serialize(row, user, conversations, runs, rules) for row in page],
        total=total,
    )


def _file_filters(
    user: User,
    *,
    segment: Visibility,
    shared_excludes_company: bool,
    search: str | None,
    context: FileContext | None,
    origin: FileOrigin | None,
    conversation_id: str | None,
    assignment_run_id: str | None,
) -> list:
    """The predicates of the file list: the list and its segment counts read the same ones."""
    where = [readable_files_filter(user), _listed_file_predicate()]
    if search and search.strip():
        where.append(FileAttachment.filename.ilike(f"%{search.strip()}%"))
    context_predicate = context_sql_predicate(context)
    if context_predicate is not None:
        where.append(context_predicate)
    origin_predicate = _origin_sql_predicate(origin)
    if origin_predicate is not None:
        where.append(origin_predicate)
    if segment == "mine":
        where.append(mine_files_filter(user.id))
    elif segment == "company":
        where.append(~mine_files_filter(user.id))
        where.append(company_files_filter(user.id))
    elif segment == "shared":
        where.append(~mine_files_filter(user.id))
        if shared_excludes_company:
            where.append(~company_files_filter())
    if conversation_id:
        where.append(FileAttachment.conversation_id == parse_uuid(conversation_id, label="conversation ID"))
    if assignment_run_id:
        where.append(FileAttachment.assignment_run_id == parse_uuid(assignment_run_id, label="run ID"))
    return where


@router.get("/segment-counts", response_model=SegmentCounts)
async def file_segment_counts(
    search: str | None = Query(default=None, max_length=200),
    context: FileContext | None = Query(default=None),
    origin: FileOrigin | None = Query(default=None),
    conversation_id: str | None = Query(default=None),
    assignment_run_id: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SegmentCounts:
    """How many files each visibility segment holds under the list's other filters."""

    async def count(segment: Visibility) -> int:
        where = _file_filters(
            user,
            segment=segment,
            shared_excludes_company=True,
            search=search,
            context=context,
            origin=origin,
            conversation_id=conversation_id,
            assignment_run_id=assignment_run_id,
        )
        return (await db.execute(select(func.count()).select_from(FileAttachment).where(*where))).scalar_one()

    return await segment_counts(count)


@router.delete("/{file_id}", status_code=204)
async def delete_file(
    file_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete one file — its row and its S3 object.

    Refused with the unknown-file 404 (never 403 — the id must not be an
    oracle for files the requester cannot see) unless :func:`can_delete_file`
    admits the requester. A template asset the requester may read is refused
    with 409 and pointed at its template, since it is managed there; one they
    may not read gets the 404 like any other file.
    """
    from app.services.file_access import can_read_file, file_not_found
    from app.services.file_cleanup import delete_file_attachments

    file_uuid = parse_path_id(file_id, not_found_detail="File not found")
    row = (
        await db.execute(select(FileAttachment).where(FileAttachment.id == file_uuid, readable_files_filter(user)))
    ).scalar_one_or_none()
    if row is None:
        # Distinguish only for template assets the requester could read: they
        # are excluded from the readable set, so look once more to give the
        # useful answer instead of a misleading 404.
        asset = (
            await db.execute(
                select(FileAttachment).where(
                    FileAttachment.id == file_uuid,
                    FileAttachment.source == TEMPLATE_ASSET_SOURCE,
                )
            )
        ).scalar_one_or_none()
        if asset is not None and await can_read_file(asset, user, db):
            raise HTTPException(
                status_code=409,
                detail="Template assets are managed from their template",
            )
        raise file_not_found()

    conversations, runs = await _load_context_labels(db, [row])
    conv = conversations.get(row.conversation_id) if row.conversation_id else None
    run_entry = runs.get(row.assignment_run_id) if row.assignment_run_id else None
    if not can_delete_file(
        row,
        user,
        conversation_owner_id=conv[1] if conv else None,
        run=run_entry[0] if run_entry else None,
    ):
        raise file_not_found()

    filename = row.filename
    await delete_file_attachments(db, id=row.id)
    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="file",
        entity_id=file_uuid,
        entity_name=filename,
        summary=f"Deleted file '{filename}'",
    )
    await db.commit()
    return None


__all__ = [
    "can_delete_file",
    "context_sql_predicate",
    "download_url_for",
    "file_context",
    "mine_files_filter",
    "readable_files_filter",
    "router",
]
