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

"""Presentation version history — snapshot on save, list, restore.

Every ``save_presentation`` records the spec it just wrote as a
``PresentationVersion`` numbered by the deck's own ``revision``, so the
history panel, the editor's 409 conflict message and the agent's tool result
all speak about the same number. Modelled on ``flow_versioning``:

* **Append-only.** A restore does not rewind the counter; it saves the chosen
  version's spec as a NEW revision (``source = "restore"``), so a restore is
  itself undoable and "what happened to this deck" is a straight line.
* **Fixed retention.** ``PRESENTATION_VERSION_RETENTION`` newest entries are
  kept per deck; older ones are pruned on every write. Fixed rather than
  configurable for the same reason flows are: an agent rewriting a deck ten
  times in one conversation must not evict the version the presenter wants
  back, and there is no operator reason to tune it.
* **Never raises into the save.** Recording a version is bookkeeping around
  the deck write; a failure to snapshot is logged and the deck save still
  lands (the deck is the deliverable, the history is the safety net).
* **Edit sessions**. The slide editor autosaves every few seconds; one
  entry per save would evict the 30 kept revisions within minutes of typing.
  A run of manual saves by one person is ONE entry, moved forward to each new
  revision (:func:`open_edit_session`): open while saves come less than
  ``MANUAL_EDIT_SESSION_IDLE_SECONDS`` apart and for at most
  ``MANUAL_EDIT_SESSION_MAX_SECONDS``, closed by an entry of any other kind
  (an agent edit, a restore, a template change), so those stay points the
  history can go back to. Its summary says what changed since the entry under
  it. The revision numbers in the list skip what a session folded in.

Restoring goes through :func:`app.services.presentations.save_presentation`
so the spec is re-validated exactly like an agent's call — a version saved
under an older validator that would no longer pass is refused with the same
field-addressed error, never written back blindly.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.presentation import (
    PRESENTATION_VERSION_RETENTION,
    VERSION_SOURCE_AGENT,
    VERSION_SOURCE_MANUAL,
    VERSION_SOURCE_RESTORE,
    VERSION_SOURCES,
    Presentation,
    PresentationVersion,
)
from app.models.user import User

logger = logging.getLogger(__name__)

_SUMMARY_MAX = 500

#: An edit session stays open while one person's manual saves come less than
#: this far apart: a pause longer than a coffee break is a new entry.
MANUAL_EDIT_SESSION_IDLE_SECONDS = 5 * 60
#: ...and for at most this long from its first save: an afternoon of editing
#: keeps a point to go back to every half hour.
MANUAL_EDIT_SESSION_MAX_SECONDS = 30 * 60
#: How many of the newest entries are read to find the person's open session:
#: the other editors' sessions sit above it while several people edit at once.
_EDIT_SESSION_LOOKBACK = 6


def normalize_summary(summary: str | None) -> str | None:
    """One trimmed line, capped at the column width; ``None`` when empty."""
    if not summary or not isinstance(summary, str):
        return None
    text = " ".join(summary.split())
    if not text:
        return None
    return text[:_SUMMARY_MAX]


def changed_spec_fields(previous: Mapping[str, object] | None, current: Mapping[str, object]) -> list[str]:
    """Top-level keys whose value differs between two *validated* specs.

    The editor PATCHes the whole spec on every save, so "which fields were
    sent" would read ``slides, theme, title, transition`` for a one-word title
    change. Both sides come out of ``validate_spec`` (the stored spec IS its
    output), so a plain top-level comparison names exactly what changed —
    and an identical spec yields ``[]``, which the route turns into a no-op
    rather than a revision bump with an empty history line.
    """
    before = dict(previous or {})
    keys = set(before) | set(current)
    return sorted(key for key in keys if before.get(key) != current.get(key))


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _longest_increasing_run(values: Sequence[int]) -> set[int]:
    """Indexes of one longest strictly increasing subsequence of *values*.

    The slides that KEPT their relative order between two revisions; every
    other common slide is the one that moved (the minimal set, so one slide
    dragged to the top is one move, not one move per slide it passed).
    """
    if not values:
        return set()
    tails: list[int] = []  # index into values of the tail of each length
    previous: list[int] = [-1] * len(values)
    for index, value in enumerate(values):
        lo, hi = 0, len(tails)
        while lo < hi:
            mid = (lo + hi) // 2
            if values[tails[mid]] < value:
                lo = mid + 1
            else:
                hi = mid
        if lo > 0:
            previous[index] = tails[lo - 1]
        if lo == len(tails):
            tails.append(index)
        else:
            tails[lo] = index
    kept: set[int] = set()
    cursor = tails[-1]
    while cursor != -1:
        kept.add(cursor)
        cursor = previous[cursor]
    return kept


def _as_stored_today(spec: Mapping[str, object]) -> Mapping[str, object]:
    """*spec* as ``validate_spec`` would write it today, its slide ids as stored.

    The stored spec of an older deck is not today's validator output: a v1
    slide is upgraded on read (``SpecJSON``) without the defaults the models
    materialise, a v1-era row spells an absent subtitle as ``""``, a
    field added since has no key. Compared raw against a fresh validation of
    the same slides, every one of them reads as edited - so the baseline is
    read through the validator first. The ids stay as stored: a slide with
    none is matched by position (``_assign_slide_ids`` would mint one), and
    a spec today's validator refuses is compared raw.
    """
    from app.services.presentation_spec import MAX_SLIDE_CAP, PresentationSpecError, validate_spec

    try:
        # A read, not a write: a stored deck is normalised whatever its cap
        # reads today, so a long deck's history still names its edits.
        data = validate_spec(dict(spec), slide_cap=MAX_SLIDE_CAP)
    except PresentationSpecError:
        return spec
    stored = [s for s in (spec.get("slides") or []) if isinstance(s, Mapping)]
    slides = [s for s in (data.get("slides") or []) if isinstance(s, dict)]
    if len(slides) != len(stored):
        return spec
    for raw, slide in zip(stored, slides, strict=True):
        if raw.get("id"):
            slide["id"] = raw["id"]
        else:
            slide.pop("id", None)
    return data


def describe_slide_changes(previous: Mapping[str, object], current: Mapping[str, object]) -> list[str]:
    """The slide-level account of a manual save, by stable slide id.

    ``["Edited slides 3, 7", "Inserted slide 4", "Deleted slide 5", "Moved
    slide 4 to 3"]`` - the same wording as an agent edit's ``summarize_patch``,
    so the history reads the same whoever made the change. Slides are matched
    by ``id``; positions are 1-based in the revision the change is read from -
    the new one for an edit, an insert or a move's destination, the old one for
    a delete and a move's origin.

    A stored slide with NO id (a deck written before ``validate_spec`` assigned
    one on every save) is matched by POSITION: the save it is compared with
    gave every slide a fresh id, so matching those by id would read the whole
    deck as deleted and re-inserted. The id itself is never an edit - it is the
    match key, not slide content. The stored side is read as today's validator
    would store it (:func:`_as_stored_today`), so an older deck's slides the
    person never touched are not reported as edited for the defaults and the
    upgrades a re-validation spells out.
    """
    from app.services.presentation_spec import unit_noun

    previous = _as_stored_today(previous)
    before = [dict(s) for s in (previous.get("slides") or []) if isinstance(s, Mapping)]
    after = [dict(s) for s in (current.get("slides") or []) if isinstance(s, Mapping)]

    def content(slide: Mapping[str, object]) -> str:
        return _canonical({k: v for k, v in slide.items() if k != "id"})

    def key(slide: Mapping[str, object], index: int) -> str:
        return str(slide.get("id") or f"#{index}")

    before_at = {key(s, i): (i, content(s)) for i, s in enumerate(before)}

    def after_key(slide: Mapping[str, object], index: int) -> str:
        slide_id = slide.get("id")
        if slide_id and str(slide_id) in before_at:
            return str(slide_id)
        if f"#{index}" in before_at:
            return f"#{index}"  # the id-less stored slide at this position
        return str(slide_id or f"#{index}")

    after_at = {after_key(s, i): (i, content(s)) for i, s in enumerate(after)}
    after_ids = [after_key(s, i) for i, s in enumerate(after)]
    before_ids = [key(s, i) for i, s in enumerate(before)]

    def unit(count: int) -> str:
        return unit_noun(dict(current), count)

    changed = [i + 1 for i, sid in enumerate(after_ids) if sid in before_at and before_at[sid][1] != after_at[sid][1]]
    inserted = [i + 1 for i, sid in enumerate(after_ids) if sid not in before_at]
    deleted = [i + 1 for i, sid in enumerate(before_ids) if sid not in after_at]
    common = [sid for sid in after_ids if sid in before_at]
    kept = _longest_increasing_run([before_at[sid][0] for sid in common])
    moved = [(before_at[sid][0] + 1, after_at[sid][0] + 1) for k, sid in enumerate(common) if k not in kept]

    parts: list[str] = []
    if changed:
        parts.append(f"Edited {unit(len(changed))} {', '.join(str(n) for n in changed)}")
    if inserted:
        parts.append(f"Inserted {unit(len(inserted))} {', '.join(str(n) for n in inserted)}")
    if deleted:
        parts.append(f"Deleted {unit(len(deleted))} {', '.join(str(n) for n in deleted)}")
    if moved:
        parts.append("Moved " + ", ".join(f"{unit(1)} {old} to {new}" for old, new in moved))
    return parts


def summarize_manual_edit(
    changed_fields: Sequence[str],
    *,
    previous: Mapping[str, object] | None = None,
    current: Mapping[str, object] | None = None,
) -> str | None:
    """Human summary for a manual PATCH from the spec fields it changed.

    ``["slides", "theme"]`` → ``"Edited slides, theme"``. Stable ordering so
    two identical edits read identically in the history. With both specs
    given, the ``slides`` part is spelled out by slide
    (:func:`describe_slide_changes`) - a person's save names what an agent's
    does: ``"Edited slides 3, 7; Inserted slide 4; Changed theme"``.
    """
    names = sorted({str(f) for f in changed_fields if f})
    if not names:
        return None
    if "slides" in names and previous is not None and current is not None:
        parts = describe_slide_changes(previous, current)
        others = [name for name in names if name != "slides"]
        if others:
            parts.append("Changed " + ", ".join(others))
        if parts:
            return normalize_summary("; ".join(parts))
    return normalize_summary("Edited " + ", ".join(names))


def _aware(moment: datetime) -> datetime:
    """A stored timestamp as an aware UTC datetime (SQLite and tests hand back naive ones)."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


async def open_edit_session(
    db: AsyncSession,
    presentation: Presentation,
    *,
    author_id: uuid.UUID | str | None,
    now: datetime | None = None,
) -> PresentationVersion | None:
    """The history entry a manual save by *author_id* continues, or ``None``.

    The person's newest edit session (an entry with ``updated_at`` set), still
    open: its last save less than ``MANUAL_EDIT_SESSION_IDLE_SECONDS`` ago and
    its first less than ``MANUAL_EDIT_SESSION_MAX_SECONDS`` ago, with no entry
    of another kind written after it - an agent edit, a restore, a template
    change stays a point the history can go back to. Other people's sessions
    above it (two people editing at once) do not close it: each person's run
    of saves is one entry. Read among the ``_EDIT_SESSION_LOOKBACK`` newest.
    """
    author = _coerce_uuid(author_id)
    if author is None or presentation.id is None:
        return None
    rows = (
        (
            await db.execute(
                select(PresentationVersion)
                .where(PresentationVersion.presentation_id == presentation.id)
                .order_by(PresentationVersion.revision.desc())
                .limit(_EDIT_SESSION_LOOKBACK)
            )
        )
        .scalars()
        .all()
    )
    moment = now or datetime.now(UTC)
    for row in rows:
        if row.source != VERSION_SOURCE_MANUAL or row.updated_at is None:
            return None
        if row.author_id != author:
            continue
        idle = (moment - _aware(row.updated_at)).total_seconds()
        span = (moment - _aware(row.created_at)).total_seconds() if row.created_at else None
        if idle >= MANUAL_EDIT_SESSION_IDLE_SECONDS or span is None or span >= MANUAL_EDIT_SESSION_MAX_SECONDS:
            return None
        return row
    return None


async def _entry_under(
    db: AsyncSession, presentation_id: uuid.UUID, *, below: int, besides: uuid.UUID
) -> PresentationVersion | None:
    """The newest entry older than revision *below*, other than *besides*: the one a moved session sits on."""
    return (
        (
            await db.execute(
                select(PresentationVersion)
                .where(
                    PresentationVersion.presentation_id == presentation_id,
                    PresentationVersion.revision < below,
                    PresentationVersion.id != besides,
                )
                .order_by(PresentationVersion.revision.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


async def record_presentation_version(
    db: AsyncSession,
    presentation: Presentation,
    *,
    source: str,
    author_id: uuid.UUID | str | None = None,
    summary: str | None = None,
    restored_from_revision: int | None = None,
    edit_session: PresentationVersion | None = None,
    opens_edit_session: bool = False,
) -> PresentationVersion | None:
    """Snapshot *presentation*'s current spec under its current revision.

    Idempotent per ``(presentation, revision)``: a retry of the same save does
    not produce a duplicate entry (the unique constraint would refuse it, so
    the existing row is returned instead). Prunes past the retention.

    *edit_session* (:func:`open_edit_session`) is the entry this manual
    save continues: it is MOVED to the new revision - its spec, title and
    slide count replaced, ``updated_at`` stamped, its summary recomputed
    against the entry it now sits on - instead of a row being added.
    *opens_edit_session* makes a new entry one a later save may continue.

    Runs in the SAME transaction as the deck write and is allowed to fail it:
    a revision with no snapshot would break the one promise the history makes,
    and a failed flush leaves the session unusable for the outer commit anyway.
    """
    if source not in VERSION_SOURCES:
        raise ValueError(f"unknown version source {source!r}")
    existing = (
        await db.execute(
            select(PresentationVersion).where(
                PresentationVersion.presentation_id == presentation.id,
                PresentationVersion.revision == presentation.revision,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    moment = datetime.now(UTC)
    current = dict(presentation.spec or {})
    if edit_session is not None:
        if edit_session.presentation_id != presentation.id:
            raise ValueError("edit session does not belong to this presentation")
        under = await _entry_under(db, presentation.id, below=presentation.revision, besides=edit_session.id)
        if under is not None:
            changed = changed_spec_fields(under.spec, current)
            summary = summarize_manual_edit(changed, previous=under.spec, current=current) or summary
        edit_session.revision = presentation.revision
        edit_session.title = presentation.title
        edit_session.slide_count = presentation.slide_count
        edit_session.spec = current
        edit_session.summary = normalize_summary(summary)
        edit_session.updated_at = moment
        await db.flush()
        return edit_session
    version = PresentationVersion(
        presentation_id=presentation.id,
        revision=presentation.revision,
        title=presentation.title,
        slide_count=presentation.slide_count,
        spec=current,
        source=source,
        summary=normalize_summary(summary),
        restored_from_revision=restored_from_revision,
        author_id=_coerce_uuid(author_id),
        created_at=moment,
        updated_at=moment if opens_edit_session else None,
    )
    db.add(version)
    await db.flush()
    pruned = await _enforce_retention(db, presentation.id)
    if pruned:
        logger.debug(
            "presentation %s: pruned %d version(s) past retention",
            presentation.id,
            pruned,
        )
    return version


async def _enforce_retention(db: AsyncSession, presentation_id: uuid.UUID) -> int:
    """Delete everything older than the newest ``PRESENTATION_VERSION_RETENTION``."""
    total = (
        await db.execute(
            select(func.count())
            .select_from(PresentationVersion)
            .where(PresentationVersion.presentation_id == presentation_id)
        )
    ).scalar_one_or_none()
    excess = int(total or 0) - PRESENTATION_VERSION_RETENTION
    if excess <= 0:
        return 0
    doomed = (
        select(PresentationVersion.id)
        .where(PresentationVersion.presentation_id == presentation_id)
        .order_by(PresentationVersion.revision.asc())
        .limit(excess)
    )
    result = await db.execute(delete(PresentationVersion).where(PresentationVersion.id.in_(doomed)))
    return int(result.rowcount or 0)


async def list_presentation_versions(db: AsyncSession, presentation_id: uuid.UUID) -> list[PresentationVersion]:
    """Newest first."""
    result = await db.execute(
        select(PresentationVersion)
        .where(PresentationVersion.presentation_id == presentation_id)
        .order_by(PresentationVersion.revision.desc())
    )
    return list(result.scalars().all())


async def get_presentation_version(
    db: AsyncSession, presentation_id: uuid.UUID, revision: int
) -> PresentationVersion | None:
    return (
        await db.execute(
            select(PresentationVersion).where(
                PresentationVersion.presentation_id == presentation_id,
                PresentationVersion.revision == revision,
            )
        )
    ).scalar_one_or_none()


async def restore_presentation_version(
    db: AsyncSession,
    presentation: Presentation,
    version: PresentationVersion,
    *,
    user_id: uuid.UUID | str | None,
) -> Presentation:
    """Save *version*'s spec as a NEW revision of *presentation*.

    Goes through ``save_presentation`` so the spec is re-validated, file
    references re-checked and the new revision itself snapshotted (as a
    ``restore`` entry pointing at the revision it brought back).

    The re-check is scoped to what the restore brings BACK: a file the
    old revision named and the current deck no longer does is verified against
    the person restoring, because the deck's readers will be able to fetch it
    again; what the current deck already shows is left alone. That is why
    *user_id* is required here despite its optional type: without an actor no
    reference can be vouched for. A picture the old revision named whose file
    no longer exists at all (a removed built-in template's asset) is not
    a reason to refuse: the revision comes back without it, and the history
    line counts the pictures left empty.
    """
    from app.services.presentation_pictures import slide_file_ids
    from app.services.presentation_spec import MAX_SLIDE_CAP
    from app.services.presentations import save_presentation

    if version.presentation_id != presentation.id:
        raise ValueError("version does not belong to this presentation")
    if user_id is None:
        raise ValueError("restoring a revision needs the acting user")
    return await save_presentation(
        db,
        user_id=presentation.user_id,
        spec_payload=dict(version.spec or {}),
        presentation=presentation,
        verify_files_for=user_id,
        verify_baseline=presentation.spec if isinstance(presentation.spec, dict) else {},
        verify_carried=slide_file_ids(version.spec),
        author_id=user_id,
        change_source=VERSION_SOURCE_RESTORE,
        change_summary=f"Restored revision {version.revision}",
        restored_from_revision=version.revision,
        # A revision that was once saved comes back whatever the deck's cap
        # reads today: held to the ceiling only, like a duplicate.
        slide_cap=MAX_SLIDE_CAP,
    )


async def resolve_user_names(db: AsyncSession, user_ids: Sequence[uuid.UUID | str | None]) -> dict[str, str]:
    """``{str(user_id): full_name}`` for every distinct non-null id.

    Shared by the version, comment and presentation serializers so an author
    is named the same way everywhere a deck's history is shown.
    """
    wanted: set[uuid.UUID] = set()
    for value in user_ids:
        coerced = _coerce_uuid(value)
        if coerced is not None:
            wanted.add(coerced)
    if not wanted:
        return {}
    result = await db.execute(select(User.id, User.full_name, User.email).where(User.id.in_(wanted)))
    return {str(uid): (name or email or "") for uid, name, email in result.all()}


def serialize_presentation_version(
    version: PresentationVersion,
    *,
    author_name: str | None = None,
    include_spec: bool = False,
) -> dict:
    payload = {
        "id": str(version.id),
        "presentation_id": str(version.presentation_id),
        "revision": version.revision,
        "title": version.title,
        "slide_count": version.slide_count,
        "source": version.source,
        "summary": version.summary,
        "restored_from_revision": version.restored_from_revision,
        "author_id": str(version.author_id) if version.author_id else None,
        "author_name": author_name,
        "created_at": version.created_at.isoformat() if version.created_at else None,
        "updated_at": version.updated_at.isoformat() if version.updated_at else None,
    }
    if include_spec:
        payload["spec"] = version.spec or {}
    return payload


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
    "MANUAL_EDIT_SESSION_IDLE_SECONDS",
    "MANUAL_EDIT_SESSION_MAX_SECONDS",
    "PRESENTATION_VERSION_RETENTION",
    "VERSION_SOURCE_AGENT",
    "VERSION_SOURCE_MANUAL",
    "VERSION_SOURCE_RESTORE",
    "changed_spec_fields",
    "describe_slide_changes",
    "get_presentation_version",
    "list_presentation_versions",
    "normalize_summary",
    "open_edit_session",
    "record_presentation_version",
    "resolve_user_names",
    "restore_presentation_version",
    "serialize_presentation_version",
    "summarize_manual_edit",
]
