# SPDX-License-Identifier: Apache-2.0

"""The builds this platform has run, and which one was deployed when.

``record_running_build`` writes the running build (the version the About dialog
shows and the commit it was built from) at startup, ``touch_running_build_if_due``
keeps its ``last_seen_at`` current from the scheduler tick. Nothing is ever
backfilled: the history starts at the first boot of a release that records it,
and every reader says so (``history_note``).

A build is the deployed one from its ``first_seen_at`` until the next build's
(``release_windows``). A rollback to an older build is not a new row - the
``(version, git_sha)`` pair already has one - so the windows follow the first
time each build was seen; ``last_seen_at`` tells a build still running from one
that was replaced.

A rollout is read off the same rows (``rollout``): the newest recorded build
booted, and the process answering is an older one - the rollout is in
progress -, or the previous build was still seen after the newest one's first
boot and recently - it may still be draining -, else the rollout is complete.
Nothing is known of a deploy before its first pod boots and records itself.

The first boot of a build raises the platform event ``platform.release.deployed``:
only the process whose upsert inserted the row publishes it, so the
pods of one rollout raise it once, and its event id is stable per build, so a
race between two publishers still starts one run per subscriber.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import case, literal, literal_column, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.utils import format_exception_message
from app.models.platform_build import PlatformBuild

logger = logging.getLogger(__name__)

#: How often the scheduler tick refreshes ``last_seen_at`` of the running build.
TOUCH_INTERVAL_SECONDS = 15 * 60

#: The bucket a time before the first recorded build falls in.
BEFORE_HISTORY_BUCKET = "before_release_history"

#: How many builds a reader takes (newest first), so a ``CASE`` over them stays small.
MAX_BUILDS = 200

_last_touch: float = 0.0


def running_build() -> tuple[str, str]:
    """``(version, git_sha)`` of this process: the About dialog's version and its commit."""
    return (settings.app_version or "0.0.0-dev")[:100], (settings.git_sha or "")[:64]


#: The short commit a development version names (``1.261104.0-dev.f3d4da9``).
_DEV_VERSION_SHA_RE = re.compile(r"-dev\.([0-9a-f]{7,40})$")


def version_git_sha(version: str) -> str:
    """The commit a ``-dev.<sha>`` *version* names, lower case; empty when it names none. Pure."""
    match = _DEV_VERSION_SHA_RE.search((version or "").strip().lower())
    return match.group(1) if match else ""


async def recorded_git_sha(version: str) -> str:
    """The commit ``platform_builds`` holds for *version*, written by a process that had one; empty when none."""
    from app.core.database import async_session

    async with async_session() as db:
        sha = await db.scalar(
            select(PlatformBuild.git_sha)
            .where(PlatformBuild.version == version, PlatformBuild.git_sha != "")
            .order_by(PlatformBuild.first_seen_at.desc())
            .limit(1)
        )
    return str(sha or "")


async def resolve_build_sha(version: str, git_sha: str | None) -> str:
    """The commit of the build *version*, lower case; empty when nothing names it.

    *git_sha* when given (the process's ``GIT_SHA`` or the release event's);
    else the commit recorded for the same version by another process of the
    build (a pod started without ``GIT_SHA`` answers ``running.git_sha: null``
    while the row its sibling recorded carries it); else the short commit a
    ``-dev.<sha>`` version names. Never raises.
    """
    if git_sha:
        return git_sha.strip().lower()
    recorded = ""
    if version:
        try:
            recorded = await recorded_git_sha(version)
        except Exception as exc:
            logger.info("The commit of build %s not read: %s", version, format_exception_message(exc))
    return (recorded or version_git_sha(version)).strip().lower()


#: The platform event the first boot of a build raises.
RELEASE_DEPLOYED_EVENT = "platform.release.deployed"


async def _upsert_running_build(db: AsyncSession, *, now: datetime) -> dict | None:
    """Upsert the running build; the ``platform.release.deployed`` payload when this call inserted it."""
    version, git_sha = running_build()
    statement = (
        pg_insert(PlatformBuild)
        .values(version=version, git_sha=git_sha, first_seen_at=now, last_seen_at=now)
        .on_conflict_do_update(
            constraint="uq_platform_builds_version_sha",
            set_={"last_seen_at": now},
        )
        # ``xmax = 0`` on the returned row: this statement inserted it (a
        # conflict update stamps xmax). The unique constraint picks one inserter.
        .returning(PlatformBuild.first_seen_at, literal_column("(xmax = 0)").label("inserted"))
    )
    row = (await db.execute(statement)).one_or_none()
    deployed: dict | None = None
    if row is not None and row.inserted:
        previous = (
            await db.execute(
                select(PlatformBuild.version, PlatformBuild.git_sha)
                .where((PlatformBuild.version != version) | (PlatformBuild.git_sha != git_sha))
                .order_by(PlatformBuild.first_seen_at.desc())
                .limit(1)
            )
        ).one_or_none()
        deployed = release_deployed_data(
            version=version,
            git_sha=git_sha,
            first_seen_at=_utc(row.first_seen_at),
            previous_version=previous.version if previous else None,
            previous_git_sha=previous.git_sha if previous else None,
        )
    await db.commit()
    return deployed


def release_deployed_data(
    *,
    version: str,
    git_sha: str,
    first_seen_at: datetime,
    previous_version: str | None,
    previous_git_sha: str | None,
) -> dict:
    """The payload of ``platform.release.deployed``.

    A build belongs to no one and its version is shown to every user (the About
    dialog): the event is platform-wide (``platform_events.PLATFORM_WIDE_EVENTS``),
    heard by every subscriber whatever its scope, and touches no workspace.
    """
    return {
        "version": version,
        "git_sha": git_sha or None,
        "previous_version": previous_version,
        "previous_git_sha": previous_git_sha or None,
        "first_seen_at": first_seen_at.isoformat(),
        "release": release_label(version, git_sha),
        "object_type": "platform_release",
        "object_id": release_label(version, git_sha),
        "participant_agent_ids": [],
    }


def release_event_id(version: str, git_sha: str) -> str:
    """The stable event id of one build's ``platform.release.deployed`` (one run per subscriber).

    A digest of the build, 40 characters: a watch keeps at most 64 of an event id.
    """
    digest = hashlib.sha256(f"{version}\n{git_sha}".encode()).hexdigest()[:32]
    return f"release-{digest}"


async def record_running_build(db: AsyncSession) -> None:
    """Record the build this process booted with; never raises (a startup step).

    The first boot of a build publishes ``platform.release.deployed`` after the commit.
    """
    try:
        deployed = await _upsert_running_build(db, now=datetime.now(UTC))
    except Exception as exc:
        await db.rollback()
        logger.warning(
            "Could not record the running build in platform_builds: %s", format_exception_message(exc)
        )
        return
    if deployed is None:
        return
    from app.services.platform_events import publish

    await publish(
        RELEASE_DEPLOYED_EVENT,
        deployed,
        event_id=release_event_id(deployed["version"], deployed["git_sha"] or ""),
    )
    # The work items blocked on the deployment of a change this build carries.
    from app.services.work_item_blocked_refs import schedule_release_check

    schedule_release_check(deployed)
    # The owners of agents whose allowlist leaves out a built-in tool this build adds (#12192).
    from app.services.builtin_tool_release_notice import schedule_release_notice

    schedule_release_notice(deployed)
    # The decision cards parked before readable asks, rewritten for their reader once (#776).
    from app.services.work_item_ask_backfill import schedule_readable_ask_backfill

    schedule_readable_ask_backfill(deployed)


async def touch_running_build_if_due() -> None:
    """Refresh ``last_seen_at`` of the running build, at most every ``TOUCH_INTERVAL_SECONDS``."""
    global _last_touch
    now = time.monotonic()
    if _last_touch and now - _last_touch < TOUCH_INTERVAL_SECONDS:
        return
    _last_touch = now
    from app.core.database import async_session

    async with async_session() as db:
        await record_running_build(db)
    # A pass the boot call could not run (agentic AI not yet allowed, no
    # routing model) is asked again here until one process runs it.
    from app.services.work_item_ask_backfill import schedule_if_due

    version, git_sha = running_build()
    schedule_if_due({"version": version, "git_sha": git_sha or None})


@dataclass(frozen=True)
class ReleaseWindow:
    """One deployed build and the span it was the deployed one."""

    label: str
    version: str
    git_sha: str
    first_seen_at: datetime
    last_seen_at: datetime
    #: The next build's first boot, ``None`` for the newest one.
    until: datetime | None

    def payload(self) -> dict:
        return {
            "release": self.label,
            "version": self.version,
            "git_sha": self.git_sha or None,
            "first_seen_at": self.first_seen_at.isoformat(),
            "last_seen_at": self.last_seen_at.isoformat(),
            "deployed_until": self.until.isoformat() if self.until else None,
        }


def is_build(window: ReleaseWindow, version: str, git_sha: str) -> bool:
    """Whether *window* is the build ``(version, git_sha)``. Pure.

    A side that carries no commit (an image built without ``GIT_SHA``, a worker
    before #10718) matches on the version alone: it is the same build as the
    row its sibling process recorded with the commit, never an older one.
    """
    if window.version != version:
        return False
    return not git_sha or not window.git_sha or window.git_sha == git_sha


def release_label(version: str, git_sha: str) -> str:
    """What a release is called in a result: its version and short commit."""
    return f"{version} ({git_sha[:7]})" if git_sha else version


async def release_windows(db: AsyncSession) -> list[ReleaseWindow]:
    """Every recorded build, oldest first, each with the span it was deployed."""
    rows = (
        await db.execute(
            select(PlatformBuild).order_by(PlatformBuild.first_seen_at.desc()).limit(MAX_BUILDS)
        )
    ).scalars().all()
    builds = sorted(rows, key=lambda row: row.first_seen_at)
    windows: list[ReleaseWindow] = []
    for index, build in enumerate(builds):
        following = builds[index + 1].first_seen_at if index + 1 < len(builds) else None
        windows.append(
            ReleaseWindow(
                label=release_label(build.version, build.git_sha),
                version=build.version,
                git_sha=build.git_sha,
                first_seen_at=_utc(build.first_seen_at),
                last_seen_at=_utc(build.last_seen_at),
                until=_utc(following) if following else None,
            )
        )
    return windows


def release_window_slice(
    windows: list[ReleaseWindow], *, last_n: int | None = None, since: datetime | None = None
) -> list[ReleaseWindow]:
    """The releases a windowed read serves, NEWEST first: *windows* oldest first (:func:`release_windows`), kept
    when first seen at or after *since*, then the newest *last_n* of them. Neither given: every window, oldest
    first, unchanged."""
    if last_n is None and since is None:
        return list(windows)
    kept = [window for window in reversed(windows) if since is None or window.first_seen_at >= since]
    return kept[:last_n] if last_n is not None else kept


def history_note(windows: list[ReleaseWindow], window_start: datetime | None = None) -> str:
    """The sentence every release-cut result carries about where the history starts."""
    if not windows:
        return (
            "No build is recorded yet: the release history starts at the first boot of a release "
            "that records it, and nothing before it is backfilled."
        )
    start = windows[0].first_seen_at
    note = (
        f"The release history starts at {start.isoformat()} (the first recorded boot); "
        "nothing before it is backfilled."
    )
    if window_start is not None and window_start < start:
        note += (
            f" This window starts before it: what falls before that boot is counted under "
            f"'{BEFORE_HISTORY_BUCKET}', not under a release."
        )
    return note


# ── The rollout, read off the recorded builds ───────────────────────────────

ROLLOUT_UNKNOWN = "unknown"
ROLLOUT_IN_PROGRESS = "in_progress"
ROLLOUT_DRAINING = "draining"
ROLLOUT_COMPLETE = "complete"
ROLLOUT_ROLLED_BACK = "rolled_back"

#: How long after its last sighting a build still seen after a newer one's first boot counts as draining: one
#: ``last_seen_at`` refresh (``TOUCH_INTERVAL_SECONDS``) and a minute of slack.
DRAIN_GRACE = timedelta(seconds=TOUCH_INTERVAL_SECONDS + 60)


@dataclass(frozen=True)
class Rollout:
    """Where the deployment of the newest recorded build stands, as the recorded builds and the process answering
    tell it (:func:`rollout`)."""

    state: str
    #: The newest recorded build; ``None`` when none is recorded.
    newest: ReleaseWindow | None
    #: The build of the process answering (:func:`release_label` of :func:`running_build`).
    answered_by: str
    #: The build deployed before the newest, when one is recorded.
    previous: ReleaseWindow | None = None

    def words(self) -> str:
        """The sentence a result carries: ``rollout in progress: 1.1.0 (bbbbbbb), started 2026-10-01T09:00:00+00:00;
        this answer comes from 1.0.0 (aaaaaaa)``."""
        if self.state == ROLLOUT_UNKNOWN or self.newest is None:
            return "No build is recorded yet: whether a rollout is in progress is not known."
        started = self.newest.first_seen_at.isoformat()
        if self.state == ROLLOUT_IN_PROGRESS:
            return (
                f"rollout in progress: {self.newest.label}, started {started} (its first pod booted); this answer "
                f"comes from {self.answered_by}, still serving"
            )
        if self.state == ROLLOUT_DRAINING and self.previous is not None:
            return (
                f"rollout of {self.newest.label} started {started}; the previous build {self.previous.label} was "
                f"still seen at {self.previous.last_seen_at.isoformat()} and may still be draining"
            )
        if self.state == ROLLOUT_ROLLED_BACK:
            return (
                f"rolled back: {self.newest.label}, first seen {started}, was last seen "
                f"{self.newest.last_seen_at.isoformat()}; this answer comes from {self.answered_by}, the deployed build"
            )
        return f"rollout complete: {self.newest.label} is the deployed build since {started}"

    def payload(self) -> dict:
        return {
            "state": self.state,
            "release": self.newest.label if self.newest else None,
            "version": self.newest.version if self.newest else None,
            "git_sha": (self.newest.git_sha or None) if self.newest else None,
            "started_at": self.newest.first_seen_at.isoformat() if self.newest else None,
            "answered_by": self.answered_by,
            "previous_release": self.previous.label if self.previous else None,
            "previous_last_seen_at": self.previous.last_seen_at.isoformat() if self.previous else None,
            "words": self.words(),
        }


def rollout(
    windows: list[ReleaseWindow], running: tuple[str, str] | None = None, *, now: datetime | None = None
) -> Rollout:
    """Where the rollout of the newest recorded build stands: *windows* oldest first (:func:`release_windows`),
    *running* the ``(version, git_sha)`` of the process answering (:func:`running_build` by default).

    ``in_progress`` when the newest recorded build is not the one answering and
    was seen within :data:`DRAIN_GRACE` of *now*: a newer pod booted and
    recorded itself while this one still serves. ``rolled_back`` when it is not
    the one answering and has not been seen for longer: its pods are gone and an
    older build serves (a rollback is not a new row). ``draining`` when it is
    the one answering, but the previous build was still seen after its first
    boot and within :data:`DRAIN_GRACE` of *now*: its pods may still be serving.
    ``complete`` otherwise; ``unknown`` with nothing recorded.
    """
    version, git_sha = running if running is not None else running_build()
    answered_by = release_label(version, git_sha)
    if not windows:
        return Rollout(ROLLOUT_UNKNOWN, None, answered_by)
    newest = windows[-1]
    previous = windows[-2] if len(windows) > 1 else None
    moment = now or datetime.now(UTC)
    if not is_build(newest, version, git_sha):
        state = ROLLOUT_IN_PROGRESS if moment - newest.last_seen_at < DRAIN_GRACE else ROLLOUT_ROLLED_BACK
        return Rollout(state, newest, answered_by, previous)
    if (
        previous is not None
        and previous.last_seen_at > newest.first_seen_at
        and moment - previous.last_seen_at < DRAIN_GRACE
    ):
        return Rollout(ROLLOUT_DRAINING, newest, answered_by, previous)
    return Rollout(ROLLOUT_COMPLETE, newest, answered_by, previous)


def release_order(label: object, windows: list[ReleaseWindow]) -> int:
    """Where a release bucket stands in the history: ``BEFORE_HISTORY_BUCKET`` first, then oldest first."""
    for index, window in enumerate(windows):
        if window.label == label:
            return index
    return -1


def release_case(column, windows: list[ReleaseWindow]):
    """SQL: the release label *column* (a timestamp) falls in, ``BEFORE_HISTORY_BUCKET`` before the first."""
    if not windows:
        return literal(BEFORE_HISTORY_BUCKET)
    whens = [(column >= window.first_seen_at, literal(window.label)) for window in reversed(windows)]
    return case(*whens, else_=literal(BEFORE_HISTORY_BUCKET))


def release_of_day(day: date, windows: list[ReleaseWindow]) -> str:
    """The release a UTC day of analytics counts for: the newest build first seen by the end of that day.

    Analytics are day-grained (the rollup has no finer row), so a day on which a
    new build was first seen counts for that build.
    """
    end_of_day = datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(days=1)
    label = BEFORE_HISTORY_BUCKET
    for window in windows:
        if window.first_seen_at < end_of_day:
            label = window.label
        else:
            break
    return label


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
