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

"""Slide-anchored review comments on a presentation.

A comment is anchored to a slide's STABLE id (``PresentationSpec.slides[].id``,
assigned server-side and preserved across edits), never to its position: a
reorder or a deletion must not move a reviewer's note onto another slide. A
comment whose slide no longer exists is kept and reported as detached.

Who may do what — the one place these rules live, shared by the REST routes
and the agent-facing formatter:

* **comment** — anyone who can READ the deck (``deck_permission`` is not
  ``None``). Reviewing is what a read-only grantee is for.
* **resolve / reopen** — the comment's author, or anyone who can MANAGE the
  deck (``owner`` / ``config``): the owner decides when a note is addressed.
* **edit / delete** — the author, or a manager.

The agent side MIRRORS these rules rather than relaxing them: the
``list_presentation_comments`` / ``add_presentation_comment`` /
``resolve_presentation_comment`` built-in tools run as the calling user, so an
agent comments wherever its user may comment and resolves only what its user
may resolve — the same ``can_manage_comment`` check the ``PATCH`` route makes.
Closing a note is still a deliberate act: the tool is the LAST step of
"implement this comment and close it", taken when the user asked for it and
after the change is saved, and every agent write is audited with
``via: agent``. ``get_presentation`` keeps surfacing the OPEN notes, now with
the comment ids the resolve tool takes, so the assistant never guesses which
note it closed.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.presentation import (
    COMMENT_MAX_CHARS,
    Presentation,
    PresentationComment,
)
from app.services.presentation_spec import unit_noun, whole_noun
from app.services.presentations import DeckPermission

MANAGE_TIERS: frozenset[str] = frozenset({"owner", "config"})

#: Replies printed under one thread by ``format_comments_for_agent`` — the most
#: recent ones, where a review thread stands; the earlier ones are counted.
#: ``limit`` bounds the roots, this bounds what each root drags along.
MAX_REPLIES_PER_THREAD = 8
#: Ceiling on the whole digest: past it the remaining threads are counted, not
#: printed, so one long-lived review cannot fill the model's context.
MAX_DIGEST_CHARS = 20_000


class CommentError(ValueError):
    """A comment write the caller got wrong — surfaced as a 400 / 403."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def unknown_slide_error(spec: dict | None, slide_id: str) -> CommentError:
    """The 404 for an anchor naming no slide of *spec* — one wording for the
    REST routes and the agent tools, in the presentation's own nouns, with the
    count so the caller can fall back to a position."""
    ids = slide_ids_of(spec)
    return CommentError(
        f"{unit_noun(spec, 1).capitalize()} {slide_id!r} is not part of this {whole_noun(spec)}, "
        f"which has {len(ids)} {unit_noun(spec, len(ids))}.",
        status_code=404,
    )


def normalize_body(body: str | None) -> str:
    """Trim; refuse empty and oversized bodies with a precise message."""
    text = (body or "").strip()
    if not text:
        raise CommentError("A comment needs some text.")
    if len(text) > COMMENT_MAX_CHARS:
        raise CommentError(f"A comment is at most {COMMENT_MAX_CHARS} characters (got {len(text)}).")
    return text


def slide_ids_of(spec: dict | None) -> list[str]:
    """The stable ids of *spec*'s slides, in deck order."""
    slides = (spec or {}).get("slides") or []
    ids: list[str] = []
    for slide in slides:
        if isinstance(slide, dict):
            sid = slide.get("id")
            if isinstance(sid, str) and sid:
                ids.append(sid)
    return ids


def slide_position(spec: dict | None, slide_id: str | None) -> int | None:
    """1-based position of *slide_id* in *spec*, ``None`` when detached / whole-deck."""
    if not slide_id:
        return None
    ids = slide_ids_of(spec)
    try:
        return ids.index(slide_id) + 1
    except ValueError:
        return None


def slide_title_of(spec: dict | None, slide_id: str | None) -> str | None:
    if not slide_id:
        return None
    for slide in (spec or {}).get("slides") or []:
        if isinstance(slide, dict) and slide.get("id") == slide_id:
            title = slide.get("title") or slide.get("heading") or slide.get("quote")
            return str(title) if title else None
    return None


def can_manage_comment(
    comment: PresentationComment,
    *,
    user_id: uuid.UUID | str | None,
    permission: DeckPermission | None,
) -> bool:
    """Author or deck manager."""
    if permission in MANAGE_TIERS:
        return True
    if user_id is None or comment.author_id is None:
        return False
    return str(comment.author_id) == str(user_id)


async def list_comments(db: AsyncSession, presentation_id: uuid.UUID) -> list[PresentationComment]:
    """Every comment on the deck, oldest first (threads are rebuilt client-side)."""
    result = await db.execute(
        select(PresentationComment)
        .where(PresentationComment.presentation_id == presentation_id)
        .order_by(PresentationComment.created_at.asc())
    )
    return list(result.scalars().all())


async def get_comment(
    db: AsyncSession,
    presentation_id: uuid.UUID,
    comment_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> PresentationComment | None:
    """One comment of *presentation_id*, or ``None``.

    ``for_update`` takes the row lock a write to the comment needs — both
    resolve paths (the tool and the REST ``PATCH``) pass it, so two first-time
    resolves racing on one thread cannot both read it open and both stamp it
    (one closing, two audit rows): the loser waits on the winner's commit,
    reads the row as it now is, and ``set_comment_resolved`` makes its request
    the no-op it would have been a second later. Held until the caller's
    commit or rollback; a plain read never takes it.
    """
    stmt = select(PresentationComment).where(
        PresentationComment.presentation_id == presentation_id,
        PresentationComment.id == comment_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def create_comment(
    db: AsyncSession,
    presentation: Presentation,
    *,
    author_id: uuid.UUID | str | None,
    body: str,
    slide_id: str | None = None,
    parent_id: uuid.UUID | None = None,
) -> PresentationComment:
    """Add a comment (or a reply) to *presentation*.

    ``slide_id`` must name a slide the deck currently has — a comment on a
    slide that does not exist would be born detached. A reply inherits its
    root's slide and cannot itself be replied to (flat threads).
    """
    text = normalize_body(body)
    anchor = (slide_id or "").strip() or None
    parent: PresentationComment | None = None
    if parent_id is not None:
        parent = await get_comment(db, presentation.id, parent_id)
        if parent is None:
            raise CommentError("The comment you are replying to no longer exists.", status_code=404)
        if parent.parent_id is not None:
            raise CommentError("Replies cannot be nested; reply to the thread instead.")
        anchor = parent.slide_id
    elif anchor is not None and anchor not in slide_ids_of(presentation.spec):
        raise unknown_slide_error(presentation.spec, anchor)

    # One clock read for both stamps: the column defaults are two separate
    # `now()` calls, microseconds apart, and "edited" is `updated_at >
    # created_at` — a fresh comment must not be born edited.
    now = datetime.now(UTC)
    comment = PresentationComment(
        presentation_id=presentation.id,
        slide_id=anchor,
        parent_id=parent.id if parent is not None else None,
        author_id=_coerce_uuid(author_id),
        body=text,
        created_at=now,
        updated_at=now,
    )
    db.add(comment)
    await db.flush()
    return comment


async def update_comment_body(db: AsyncSession, comment: PresentationComment, *, body: str) -> PresentationComment:
    comment.body = normalize_body(body)
    await db.flush()
    return comment


async def set_comment_resolved(
    db: AsyncSession,
    comment: PresentationComment,
    *,
    resolved: bool,
    user_id: uuid.UUID | str | None,
) -> bool:
    """Resolve or reopen a THREAD — replies follow their root.

    Idempotent: ``True`` when the state changed, ``False`` when the thread was
    already where the caller wants it. A second "resolve" must not re-stamp
    ``resolved_at`` / ``resolved_by_user_id`` — that would move the closing
    onto whoever repeated it — and the callers audit only a real change.
    """
    if comment.parent_id is not None:
        raise CommentError("Resolve the thread, not a reply.")
    if resolved == (comment.resolved_at is not None):
        return False
    if resolved:
        comment.resolved_at = datetime.now(UTC)
        comment.resolved_by_user_id = _coerce_uuid(user_id)
    else:
        comment.resolved_at = None
        comment.resolved_by_user_id = None
    await db.flush()
    return True


async def delete_comment(db: AsyncSession, comment: PresentationComment) -> None:
    """Delete a comment; deleting a root cascades to its replies (FK)."""
    await db.delete(comment)
    await db.flush()


async def count_open_comments(db: AsyncSession, presentation_ids: Iterable[uuid.UUID]) -> dict[str, int]:
    """``{presentation_id: open root comments}`` for the list view badge.

    Only ROOT comments count — a thread with five replies is one open note.
    """
    ids = list({pid for pid in presentation_ids if pid is not None})
    if not ids:
        return {}
    result = await db.execute(
        select(PresentationComment.presentation_id, func.count())
        .where(
            PresentationComment.presentation_id.in_(ids),
            PresentationComment.parent_id.is_(None),
            PresentationComment.resolved_at.is_(None),
        )
        .group_by(PresentationComment.presentation_id)
    )
    return {str(pid): int(n) for pid, n in result.all()}


def serialize_comment(
    comment: PresentationComment,
    *,
    spec: dict | None,
    names: dict[str, str] | None = None,
    user_id: uuid.UUID | str | None = None,
    permission: DeckPermission | None = None,
) -> dict:
    names = names or {}
    author = str(comment.author_id) if comment.author_id else None
    resolver = str(comment.resolved_by_user_id) if comment.resolved_by_user_id else None
    return {
        "id": str(comment.id),
        "presentation_id": str(comment.presentation_id),
        "slide_id": comment.slide_id,
        # Resolved against the CURRENT spec on every read: a reorder moves the
        # number, a deletion turns it into ``None`` (detached).
        "slide_position": slide_position(spec, comment.slide_id),
        "slide_title": slide_title_of(spec, comment.slide_id),
        "detached": bool(comment.slide_id) and slide_position(spec, comment.slide_id) is None,
        "parent_id": str(comment.parent_id) if comment.parent_id else None,
        "author_id": author,
        "author_name": names.get(author or "") or None,
        "body": comment.body,
        "resolved": comment.resolved_at is not None,
        "resolved_at": comment.resolved_at.isoformat() if comment.resolved_at else None,
        "resolved_by_name": names.get(resolver or "") or None,
        "can_manage": can_manage_comment(comment, user_id=user_id, permission=permission),
        "created_at": comment.created_at.isoformat() if comment.created_at else None,
        "updated_at": comment.updated_at.isoformat() if comment.updated_at else None,
    }


#: What ``format_comments_for_agent`` may be asked to list.
COMMENT_STATUSES: tuple[str, ...] = ("open", "resolved", "all")

_STATUS_HEADERS: dict[str, str] = {
    # The open block doubles as the agent's work list: the id at the head of
    # each line is what ``resolve_presentation_comment`` takes, and the
    # condition for calling it is stated where the ids are — a tool result
    # the model reads when it matters, not a system-prompt clause it may skip.
    "open": (
        "Open review comments ({n}) — address them in your edit and say which "
        "you handled. Close one with resolve_presentation_comment(comment_id) "
        "only once the change is saved and the user asked for it:"
    ),
    "resolved": "Resolved review comments ({n}):",
    "all": "Review comments ({n}, open and resolved):",
}


def resolve_slide_anchor(
    spec: dict | None,
    *,
    slide_id: str | None = None,
    slide_position: int | None = None,
) -> str | None:
    """The stable slide id an agent-supplied anchor names; ``None`` = whole deck.

    The agent addresses slides by their 1-based position in the outline it
    reads (``summarize_slides``), while the store anchors by STABLE id; this is
    the one place that translation lives. An explicit ``slide_id`` wins, a
    position outside the deck is a precise error rather than a detached note.
    """
    unit, whole = unit_noun(spec, 1), whole_noun(spec)
    explicit = (slide_id or "").strip() or None
    if explicit is not None:
        if explicit not in slide_ids_of(spec):
            raise unknown_slide_error(spec, explicit)
        return explicit
    if slide_position is None:
        return None
    ids = slide_ids_of(spec)
    try:
        position = int(slide_position)
    except (TypeError, ValueError) as exc:
        raise CommentError(f"slide_position must be a whole number (1 = the first {unit}).") from exc
    if position < 1 or position > len(ids):
        raise CommentError(
            f"slide_position {position} is out of range: this {whole} has {len(ids)} "
            f"{unit_noun(spec, len(ids))} (1-based)."
        )
    return ids[position - 1]


def format_comments_for_agent(
    comments: Sequence[PresentationComment],
    *,
    spec: dict | None,
    names: dict[str, str] | None = None,
    status: str = "open",
    slide_id: str | None = None,
    limit: int = 30,
    max_replies: int = MAX_REPLIES_PER_THREAD,
    max_chars: int = MAX_DIGEST_CHARS,
) -> str:
    """The review notes as a block the assistant can act on — ids included.

    ``status`` picks open / resolved / all root threads; ``slide_id`` narrows
    to one slide. Grouped by slide in deck order (whole-deck notes first,
    detached last), each root with its replies indented, every root and reply
    prefixed with its comment id — the handle ``resolve_presentation_comment``
    and ``add_presentation_comment(reply_to=...)`` take, so the agent never
    has to describe a note to close it. Empty string when nothing matches, so
    the caller appends it only when there is something to say.

    Bounded three ways, each bound reported as a count rather than a silent
    cut: ``limit`` roots, ``max_replies`` (the most recent) under each root,
    and ``max_chars`` over the whole block — so a hundred threads or one
    long-lived one cannot flood the tool result. A cut takes the TAIL of the
    order above (the last slides' threads, then the detached ones), so what
    survives is the part of the deck the agent reads first; the count in the
    trailing line tells it to read the rest one slide at a time. The labels
    follow the presentation's kind (``unit_noun``): a page is never called a
    slide.
    """
    if status not in COMMENT_STATUSES:
        raise CommentError(f"status must be one of {', '.join(COMMENT_STATUSES)} (got {status!r}).")
    names = names or {}
    roots = [c for c in comments if c.parent_id is None]
    if status == "open":
        roots = [c for c in roots if c.resolved_at is None]
    elif status == "resolved":
        roots = [c for c in roots if c.resolved_at is not None]
    if slide_id:
        roots = [c for c in roots if c.slide_id == slide_id]
    if not roots:
        return ""
    replies_by_root: dict[uuid.UUID, list[PresentationComment]] = {}
    for c in comments:
        if c.parent_id is not None:
            replies_by_root.setdefault(c.parent_id, []).append(c)

    order = slide_ids_of(spec)
    rank = {sid: i for i, sid in enumerate(order)}

    def sort_key(c: PresentationComment) -> tuple[int, int, str]:
        if not c.slide_id:
            return (0, 0, c.created_at.isoformat() if c.created_at else "")
        if c.slide_id in rank:
            return (1, rank[c.slide_id], c.created_at.isoformat() if c.created_at else "")
        return (2, 0, c.created_at.isoformat() if c.created_at else "")

    roots.sort(key=sort_key)
    unit, whole = unit_noun(spec, 1), whole_noun(spec)
    lines = [_STATUS_HEADERS[status].format(n=len(roots))]
    size = len(lines[0])
    for shown, root in enumerate(roots):
        # The budget check comes after at least one thread: a digest that
        # names only a count would leave the agent nothing to act on.
        if shown >= limit or (shown and size > max_chars):
            lines.append(f"… and {len(roots) - shown} more.")
            break
        who = names.get(str(root.author_id) or "") or "A reviewer"
        if not root.slide_id:
            where = f"Whole {whole}"
        else:
            pos = slide_position(spec, root.slide_id)
            title = slide_title_of(spec, root.slide_id)
            if pos is None:
                where = f"Detached ({unit} {root.slide_id!r} was removed)"
            else:
                where = f"{unit.capitalize()} {pos}" + (f" ({title})" if title else "") + f" [id: {root.slide_id}]"
        state = ""
        if root.resolved_at is not None:
            resolver = names.get(str(root.resolved_by_user_id or "") or "")
            state = f" (resolved by {resolver})" if resolver else " (resolved)"
        entry = [f"- [{root.id}] {where} — {who}: {_one_line(root.body)}{state}"]
        replies = replies_by_root.get(root.id, [])
        hidden = max(0, len(replies) - max_replies)
        if hidden:
            entry.append(f"    - ({hidden} earlier {'reply' if hidden == 1 else 'replies'} not shown)")
        for reply in replies[hidden:]:
            r_who = names.get(str(reply.author_id) or "") or "Someone"
            entry.append(f"    - [{reply.id}] reply from {r_who}: {_one_line(reply.body)}")
        lines.extend(entry)
        size += sum(len(line) + 1 for line in entry)
    return "\n".join(lines)


def format_open_comments_for_agent(
    comments: Sequence[PresentationComment],
    *,
    spec: dict | None,
    names: dict[str, str] | None = None,
    limit: int = 30,
) -> str:
    """The OPEN review notes — what ``get_presentation`` appends to a deck read."""
    return format_comments_for_agent(comments, spec=spec, names=names, status="open", limit=limit)


def _one_line(text: str, limit: int = 400) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _coerce_uuid(value: uuid.UUID | str | None) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


__all__ = [
    "COMMENT_MAX_CHARS",
    "COMMENT_STATUSES",
    "MAX_DIGEST_CHARS",
    "MAX_REPLIES_PER_THREAD",
    "CommentError",
    "can_manage_comment",
    "count_open_comments",
    "create_comment",
    "delete_comment",
    "format_comments_for_agent",
    "format_open_comments_for_agent",
    "get_comment",
    "list_comments",
    "normalize_body",
    "resolve_slide_anchor",
    "serialize_comment",
    "set_comment_resolved",
    "slide_ids_of",
    "slide_position",
    "slide_title_of",
    "unknown_slide_error",
    "update_comment_body",
    "whole_noun",
]
