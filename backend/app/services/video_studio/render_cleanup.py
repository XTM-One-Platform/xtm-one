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

"""Deleting a video's renders on request: ``delete_video_renders`` and the Studio's "Clean up renders".

A video keeps every render it was given until the retention removes it
(``render_retention``): 4K finals a 1080p cut superseded, failed and cancelled
attempts, dozens of scene previews. Its author cleans them up in one call,
choosing what goes (:class:`Ask`):

* ``render_ids``: exactly those renders;
* ``select`` (:data:`SELECTORS`): ``all_but_latest_ready`` (the default) keeps
  the newest ready render of each quality the video has - of ``keep_quality``
  alone when given - and deletes every other render, scene previews included;
  ``failed_and_cancelled``; ``previews``; ``all``, the newest ready renders
  included, only with ``confirm``;
* ``before``: only the renders made before that time;
* ``dry_run``: the same answer, nothing deleted.

:func:`plan` decides each render - pure, so a dry run and the deletion answer
the same list - and refuses, naming it, a render still queued or running
(cancel it first), the render the video's public link plays unless
``confirm``, and, on a video the caller may only watch, a render someone else
asked for. A render kept by hand stays under a selector (named, or ``all``
with ``confirm``, deletes it); a cancelled render whose replacement is still
drawing stays while that one takes its finished segments over
(``render_segments.handed_over``).

:func:`run` applies a plan under the video's row lock: the rows and their files
through ``render_retention.delete`` (the one deletion of renders), the video's
current render moved to its newest remaining ready render when it went. The
caller audits (:func:`audit_details`), commits, then ``render_retention.purge``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.models.video_studio import (
    JOB_CANCELLED,
    JOB_FAILED,
    JOB_PENDING,
    JOB_RUNNING,
    JOB_TERMINAL_STATUSES,
    VideoProject,
    VideoRender,
)
from app.services.video_studio import render_retention
from app.services.video_studio.render_retention import Deletion, Standing

#: What a cleanup selects, the first the default.
SELECTORS = ("all_but_latest_ready", "failed_and_cancelled", "previews", "all")
DEFAULT_SELECTOR = SELECTORS[0]

ACTION_DELETE = "delete"
ACTION_KEEP = "keep"
ACTION_REFUSE = "refuse"

#: Why a render asked for is not deleted, as the answer says it (the Studio translates the code).
REASONS = {
    "in_progress": "still queued or running: cancel it first",
    "not_yours": "someone else asked for it, and this video is shared with you to watch only",
    "public_link": "the video's public link plays it: deleted only with confirm",
    "handing_over": "the render that replaced it is still drawing and takes its finished frames: delete it once that one ends",
    "latest_ready": "the newest ready render at its quality: what the video keeps",
    "kept_by_hand": "kept by hand: name it, or select all with confirm, to delete it",
    "busy": "being changed right now (another cleanup or the retention): ask again",
}

#: What ``confirm`` let go, as the audit records it.
CONFIRMED_PUBLIC_LINK = "public_link"
CONFIRMED_LATEST_READY = "latest_ready"
CONFIRMED_KEPT = "kept_by_hand"


class CleanupError(ValueError):
    """A request that cannot be planned: answered as it is, nothing deleted."""


def _as_bool(value: object) -> bool:
    return value is True or str(value).strip().lower() == "true"


def _id_list(value: object) -> tuple[str, ...] | None:
    """The render ids asked for: a list, a JSON list, or ids separated by commas; ``None`` when none was given."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        text = value.strip()
        try:
            value = json.loads(text) if text.startswith("[") else text.split(",")
        except ValueError:
            value = text.strip("[]").split(",")
    if not isinstance(value, list | tuple):
        raise CleanupError("render_ids is a list of render ids.")
    return tuple(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


def _when(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        when = datetime.fromisoformat(str(value).strip())
    except ValueError:
        raise CleanupError(f"before is a date or time in ISO 8601 (2026-10-20 or 2026-10-20T08:00:00Z), not {value!r}.") from None
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Ask:
    """What a cleanup asks for (:meth:`parse` checks it)."""

    render_ids: tuple[str, ...] | None = None
    select: str | None = None
    keep_quality: str | None = None
    before: datetime | None = None
    dry_run: bool = False
    confirm: bool = False

    @property
    def selector(self) -> str | None:
        """The selector applied: ``None`` for named renders."""
        return None if self.render_ids is not None else (self.select or DEFAULT_SELECTOR)

    @classmethod
    def parse(
        cls,
        *,
        render_ids: object = None,
        select: object = None,
        keep_quality: object = None,
        before: object = None,
        dry_run: object = False,
        confirm: object = False,
    ) -> Ask:
        from app.services.video_studio.spec import QUALITY_PRESETS

        ids = _id_list(render_ids)
        chosen = str(select).strip().lower() if select not in (None, "") else None
        if ids is not None and chosen:
            raise CleanupError("Give render_ids or select, not both.")
        if ids == ():
            raise CleanupError("render_ids names no render: give at least one id, or a select.")
        if chosen is not None and chosen not in SELECTORS:
            raise CleanupError(f"select is one of {', '.join(SELECTORS)}.")
        quality = str(keep_quality).strip().lower() if keep_quality not in (None, "") else None
        if quality is not None:
            if quality not in QUALITY_PRESETS:
                raise CleanupError(f"keep_quality is one of {', '.join(QUALITY_PRESETS)}.")
            if ids is not None or (chosen or DEFAULT_SELECTOR) != DEFAULT_SELECTOR:
                raise CleanupError(f"keep_quality goes with select {DEFAULT_SELECTOR} only.")
        confirmed = _as_bool(confirm)
        if chosen == "all" and not confirmed:
            raise CleanupError(
                "select all also deletes the newest ready render of every quality, leaving the video nothing to play: "
                "only with confirm: true, once the user asked for exactly that."
            )
        return cls(ids, chosen, quality, _when(before), _as_bool(dry_run), confirmed)


@dataclass
class Item:
    """One render a cleanup considered, and what it does with it."""

    render: VideoRender
    action: str
    reason: str | None = None
    size_bytes: int = 0
    #: What ``confirm`` let go to delete it (:data:`CONFIRMED_PUBLIC_LINK`...).
    confirmed: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        render = self.render
        out: dict[str, Any] = {
            "render_id": str(render.id),
            "quality": render.quality,
            "revision": render.revision,
            "status": render.status,
            "size_bytes": self.size_bytes,
            "action": self.action,
        }
        if render.scene_id:
            out["scene_id"] = render.scene_id
        if self.reason:
            out["reason"] = self.reason
            out["why"] = REASONS[self.reason]
        return out


def _summary(render: VideoRender | None, size: int | None = None) -> dict[str, Any] | None:
    if render is None:
        return None
    out: dict[str, Any] = {"render_id": str(render.id), "quality": render.quality, "revision": render.revision}
    if render.width and render.height:
        out["size"] = f"{render.width}x{render.height}"
    if size is not None:
        out["size_bytes"] = size
    return out


@dataclass
class Plan:
    """What a cleanup does with each render of the video, and what the video plays afterwards."""

    items: list[Item]
    #: Ids asked for that are not renders of this video.
    unknown: list[str]
    renders: list[VideoRender]
    standing: Standing
    sizes: dict[uuid.UUID, int]
    #: The render the video plays once the deletions are done (its current render, or the next when it goes).
    current_after: VideoRender | None = None
    current_moves: bool = False

    @property
    def deleting(self) -> list[Item]:
        return [item for item in self.items if item.action == ACTION_DELETE]

    @property
    def bytes(self) -> int:
        return sum(item.size_bytes for item in self.deleting)

    @property
    def confirmed(self) -> list[str]:
        return list(dict.fromkeys(reason for item in self.deleting for reason in item.confirmed))

    def settle(self) -> None:
        """What the video plays once the planned deletions are done."""
        gone = {item.render.id for item in self.deleting}
        current = self.standing.current_id
        by_id = {render.id: render for render in self.renders}
        self.current_moves = current is not None and current in gone
        if self.current_moves:
            self.current_after = render_retention.newest_playable(self.renders, excluding=gone)
        else:
            self.current_after = by_id.get(current) if current is not None else None

    def serves(self) -> dict[str, Any]:
        """What the video serves once the deletions are done: its current render and its newest ready per quality."""
        gone = {item.render.id for item in self.deleting}
        left = [render for render in self.renders if render.id not in gone]
        by_id = {render.id: render for render in left}
        latest = render_retention.latest_ready_of(left)
        return {
            "current": _summary(self.current_after, self.sizes.get(self.current_after.id) if self.current_after else None),
            "current_changed": self.current_moves,
            "public_link": self.standing.public_link,
            "latest_ready": {
                quality: _summary(by_id[render_id], self.sizes.get(render_id)) for quality, render_id in sorted(latest.items())
            },
        }


def _newest_first(renders: Iterable[VideoRender]) -> list[VideoRender]:
    floor = datetime.min.replace(tzinfo=UTC)
    return sorted(renders, key=lambda r: (r.created_at or floor, str(r.id)), reverse=True)


def _selected(render: VideoRender, selector: str) -> bool:
    if selector == "failed_and_cancelled":
        return render.status in (JOB_FAILED, JOB_CANCELLED)
    if selector == "previews":
        return render.scene_id is not None
    return True


def handing_over(renders: Iterable[VideoRender]) -> set[uuid.UUID]:
    """The cancelled whole renders whose replacement at their quality is still queued or running: it takes their
    finished segments (``render_segments.handed_over``), so they stay until it ends."""
    rows = list(renders)
    drawing = {render.id: render for render in rows if render.status in (JOB_PENDING, JOB_RUNNING)}
    return {
        render.id
        for render in rows
        if render.status == JOB_CANCELLED
        and render.scene_id is None
        and (successor := drawing.get(render.superseded_by_id)) is not None
        and successor.quality == render.quality
    }


def _decide(
    render: VideoRender,
    ask: Ask,
    standing: Standing,
    *,
    user_id: uuid.UUID,
    can_manage: bool,
    protected: set[uuid.UUID],
    handed: set[uuid.UUID],
) -> Item:
    if render.status not in JOB_TERMINAL_STATUSES:
        return Item(render, ACTION_REFUSE, "in_progress")
    if not can_manage and render.user_id != user_id:
        return Item(render, ACTION_REFUSE, "not_yours")
    if render.id in handed:
        return Item(render, ACTION_KEEP, "handing_over")
    if render.id in protected:
        return Item(render, ACTION_KEEP, "latest_ready")
    named = ask.render_ids is not None
    everything = ask.selector == "all"
    confirmed: list[str] = []
    if render.keep and not named:
        if not everything:
            return Item(render, ACTION_KEEP, "kept_by_hand")
        confirmed.append(CONFIRMED_KEPT)
    if everything and standing.is_latest_ready(render):
        confirmed.append(CONFIRMED_LATEST_READY)
    if render.id == standing.current_id and standing.public_link:
        if not ask.confirm:
            return Item(render, ACTION_REFUSE, "public_link")
        confirmed.append(CONFIRMED_PUBLIC_LINK)
    return Item(render, ACTION_DELETE, confirmed=confirmed)


def plan(
    renders: Iterable[VideoRender],
    ask: Ask,
    *,
    standing: Standing,
    user_id: uuid.UUID,
    can_manage: bool,
    sizes: dict[uuid.UUID, int] | None = None,
) -> Plan:
    """What a cleanup *ask* does with each render of a video (*renders*: all of them), as *user_id* who may manage
    the video or not. Raises :class:`CleanupError` for a ``keep_quality`` the video has no ready render at."""
    rows = _newest_first(renders)
    sizes = sizes or {}
    by_id = {str(render.id): render for render in rows}
    unknown: list[str] = []
    if ask.render_ids is not None:
        scope = [by_id[rid] for rid in ask.render_ids if rid in by_id]
        unknown = [rid for rid in ask.render_ids if rid not in by_id]
    else:
        scope = [render for render in rows if _selected(render, ask.selector or DEFAULT_SELECTOR)]
    if ask.before is not None:
        scope = [render for render in scope if render.created_at is not None and render.created_at < ask.before]
    protected: set[uuid.UUID] = set()
    if ask.selector == DEFAULT_SELECTOR:
        if ask.keep_quality is not None:
            kept = standing.latest_ready.get(ask.keep_quality)
            if kept is None:
                have = ", ".join(sorted(standing.latest_ready)) or "none"
                raise CleanupError(
                    f"The video has no ready render at {ask.keep_quality} to keep (its ready qualities: {have}): keeping "
                    "it would leave nothing to play. Name a quality it has, or leave keep_quality out."
                )
            protected = {kept}
        else:
            protected = set(standing.latest_ready.values())
    handed = handing_over(rows)
    items = []
    for render in scope:
        item = _decide(render, ask, standing, user_id=user_id, can_manage=can_manage, protected=protected, handed=handed)
        item.size_bytes = sizes.get(render.id, 0)
        items.append(item)
    planned = Plan(items, unknown, rows, standing, sizes)
    planned.settle()
    return planned


@dataclass
class Outcome:
    """What a cleanup did (or, a dry run, would do)."""

    project_id: uuid.UUID
    title: str
    ask: Ask
    plan: Plan
    deletion: Deletion = field(default_factory=Deletion)

    @property
    def deleted(self) -> int:
        return 0 if self.ask.dry_run else len(self.deletion.render_ids)

    @property
    def bytes_freed(self) -> int:
        return 0 if self.ask.dry_run else self.deletion.bytes

    def as_dict(self) -> dict[str, Any]:
        """The answer of the tool and of ``POST /projects/{id}/renders/cleanup``: the same for a dry run."""
        totals = (
            {"renders": len(self.plan.deleting), "bytes": self.plan.bytes}
            if self.ask.dry_run
            else {"renders": self.deleted, "bytes": self.bytes_freed}
        )
        return {
            "video_id": str(self.project_id),
            "dry_run": self.ask.dry_run,
            "select": self.ask.selector,
            "keep_quality": self.ask.keep_quality,
            "renders": [item.as_dict() for item in self.plan.items],
            "unknown_render_ids": self.plan.unknown,
            "would_free" if self.ask.dry_run else "freed": totals,
            "serves": self.plan.serves(),
        }


async def run(db: AsyncSession, project: VideoProject, user: User, ask: Ask) -> Outcome:
    """Plan *ask* on *project* (which *user* can open) and, unless a dry run, apply it under the video's row lock.

    Not committed: the caller audits, commits, then ``render_retention.purge(outcome.deletion)``. A render someone
    is changing as it runs (locked) is left, said ``busy``. A dry run takes no lock.
    """
    from app.services.video_studio.projects import can_manage_project

    fresh = {"populate_existing": True}
    if not ask.dry_run:
        await db.execute(select(VideoProject.id).where(VideoProject.id == project.id).with_for_update())
    await db.refresh(project)
    renders = list(
        (await db.execute(select(VideoRender).where(VideoRender.project_id == project.id).execution_options(**fresh))).scalars().all()
    )
    policy = await render_retention.load_policy(db)
    held = render_retention.standing_of(
        project, renders, public_link=await render_retention.has_public_link(db, project.id), policy=policy
    )
    planned = plan(
        renders,
        ask,
        standing=held,
        user_id=user.id,
        can_manage=await can_manage_project(db, project, user),
        sizes=await render_retention.measure(db, renders),
    )
    outcome = Outcome(project.id, project.title, ask, planned)
    if ask.dry_run or not planned.deleting:
        return outcome
    ids = [item.render.id for item in planned.deleting]
    locked = await db.execute(
        select(VideoRender)
        .where(VideoRender.id.in_(ids), VideoRender.status.in_(tuple(JOB_TERMINAL_STATUSES)))
        .with_for_update(skip_locked=True)
        .execution_options(**fresh)
    )
    rows = {render.id: render for render in locked.scalars().all()}
    for item in planned.deleting:
        if item.render.id not in rows:
            item.action, item.reason, item.confirmed = ACTION_KEEP, "busy", []
    planned.settle()
    outcome.deletion = await render_retention.delete(db, [rows[item.render.id] for item in planned.deleting])
    if planned.current_moves:
        project.latest_render_id = planned.current_after.id if planned.current_after is not None else None
    return outcome


def audit_details(outcome: Outcome, *, via_agent: bool = False) -> dict[str, Any]:
    """The ``details`` of the audit row of a cleanup that deleted renders, ``confirmed`` when ``confirm`` let some go
    (as ``render_video`` records ``forced``)."""
    ask = outcome.ask
    details: dict[str, Any] = {
        "render_ids": outcome.deletion.render_ids,
        "renders": outcome.deleted,
        "bytes_freed": outcome.bytes_freed,
        "select": ask.selector,
    }
    if ask.keep_quality:
        details["keep_quality"] = ask.keep_quality
    if ask.before:
        details["before"] = ask.before.isoformat()
    confirmed = outcome.plan.confirmed
    if confirmed:
        details["confirmed"] = True
        details["confirmed_for"] = confirmed
    if outcome.plan.current_moves:
        details["current_render_id"] = str(outcome.plan.current_after.id) if outcome.plan.current_after else None
    if via_agent:
        details["via"] = "agent"
    return details


def audit_summary(outcome: Outcome) -> str:
    noun = "render" if outcome.deleted == 1 else "renders"
    confirmed = outcome.plan.confirmed
    past = f" past their protection ({', '.join(confirmed)})" if confirmed else ""
    return f"Deleted {outcome.deleted} {noun} of video {outcome.title}{past}"


def _ready(render: VideoRender) -> str:
    return f"`{render.id}` ({render.quality} rev {render.revision})"


def answer_text(outcome: Outcome) -> str:
    """What ``delete_video_renders`` answers: what went and what stayed, what to do about a refusal, what the video
    plays now, then the answer as JSON."""
    from app.services.video_studio.resources import format_bytes

    plan_, ask = outcome.plan, outcome.ask
    title = outcome.title
    if ask.dry_run:
        head = (
            f"Dry run - nothing was deleted: {len(plan_.deleting)} render(s) of **{title}** would go, freeing "
            f"{format_bytes(plan_.bytes)}. Call again without dry_run to delete them."
        )
    elif outcome.deleted:
        head = (
            f"Deleted {outcome.deleted} render(s) of **{title}**: {format_bytes(outcome.bytes_freed)} freed - their MP4s, "
            "posters, captions, frames, insert crops and the segments kept for a resume. Files saved in the conversation "
            "(save_video_frame) stay."
        )
    else:
        head = f"Nothing was deleted from **{title}**."
    lines = [head]
    kept = [item for item in plan_.items if item.action == ACTION_KEEP]
    if kept:
        lines.append(
            "Kept: " + "; ".join(f"render {_ready(item.render)}: {REASONS[item.reason or 'busy']}" for item in kept[:12])
            + (f"; and {len(kept) - 12} more" if len(kept) > 12 else "")
            + "."
        )
    for item in plan_.items:
        if item.action != ACTION_REFUSE:
            continue
        rid = str(item.render.id)
        if item.reason == "in_progress":
            lines.append(
                f"Refused render {_ready(item.render)}: it is still {item.render.status} - cancel_video_job(render_ids="
                f"['{rid}']) first if the user wants it gone, then delete it."
            )
        elif item.reason == "public_link":
            after = render_retention.newest_playable(
                plan_.renders, excluding={*(i.render.id for i in plan_.deleting), item.render.id}
            )
            then = f"render {_ready(after)}" if after is not None else "nothing (no ready render would be left)"
            lines.append(
                f"Refused render {_ready(item.render)}: the video's public link plays it. delete_video_renders with "
                f"render_ids=['{rid}'] and confirm: true deletes it, once the user agrees - the link then plays {then}."
            )
        else:
            lines.append(f"Refused render {_ready(item.render)}: {REASONS[item.reason or 'not_yours']}.")
    if plan_.unknown:
        lines.append("Not renders of this video: " + ", ".join(f"`{rid}`" for rid in plan_.unknown) + ".")
    serves = plan_.serves()
    current = plan_.current_after
    verb = "would play" if ask.dry_run else "plays"
    if current is None:
        lines.append(f"The video {verb} nothing now: no ready render is left (render_video makes one).")
    else:
        moved = " (it was moved from the render deleted)" if plan_.current_moves else ""
        link = " - and so does its public link" if plan_.standing.public_link else ""
        lines.append(f"The video {verb} render {_ready(current)}{moved}{link}.")
    if serves["latest_ready"]:
        lines.append(
            "Newest ready render per quality: "
            + ", ".join(
                f"{quality} rev {entry['revision']} `{entry['render_id']}` ({format_bytes(entry.get('size_bytes') or 0)})"
                for quality, entry in serves["latest_ready"].items()
            )
            + "."
        )
    return "\n".join(lines) + "\n```json\n" + json.dumps(outcome.as_dict(), indent=1) + "\n```"
