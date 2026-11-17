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

"""In which page state a recording opened, and in which one a step missed its element.

A browser profile's saved session keeps what its last take left in the page:
its origin's ``localStorage`` and ``sessionStorage`` (a graph's layout, a
list's filters) and, read back from them, the parameters the application
writes into its address. A take that opens in that state finds another page
than the one its script was written on, and its steps time out naming
elements that have nothing to do with the cause. So:

* **What a take opened on** (:func:`opened_words`): the first line of its
  action log says the URL it opened, what its page state reset cleared
  (``capture.reset_page``) or why none ran, and the parameters the page wrote
  into its address itself - a state the reset does not reach.
* **Where a step missed its element** (:func:`page_state`,
  :func:`missed_words`): beside the closest names, the page's URL at that
  moment (its query included), its title, and how many elements of the step's
  role it showed - or matched the step's text or selector.
* **Found on an earlier take** (:func:`earlier_finds`,
  :func:`load_earlier_finds`): the same step - the same accessible name, text
  or selector (:func:`step_key`) - found on a recording of the same person
  with the same profile within :data:`EARLIER_HOURS`, and the URL it was found
  at: "the page is in another state".
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlparse

from app.core.utils import format_exception_message
from app.services.video_studio.spec import LOCATOR_ACTIONS, TakeStep, parse_take_script

logger = logging.getLogger(__name__)

#: How far back an earlier recording of the same profile is read for the steps it found, and how many of them.
EARLIER_HOURS = 24
EARLIER_TAKES = 20
#: The most visible matches read of a text or a selector, and of the names a cleared store lists.
_VISIBLE_READ = 10
_NAMES_SAID = 6


# ── The same step on another take ─────────────────────────────────────────


def step_key(step: TakeStep) -> str | None:
    """What a step that looks for an element names - its accessible name (whatever its role: a role's family matches
    it too), its text or its selector -, as an earlier take's step is matched with it; ``None`` for one that names
    no element."""
    if step.on_canvas or step.action not in LOCATOR_ACTIONS:
        return None
    if step.role:
        return f"name:{step.name.casefold()}" if step.name else None
    if step.selector:
        return f"selector:{step.selector}"
    if step.text and step.action != "type":
        return f"text:{step.text.casefold()}"
    return None


@dataclass(frozen=True)
class EarlierFind:
    """A recording that found a step's element (:func:`earlier_finds`): which, and at which URL."""

    take_id: str
    name: str
    url: str


def earlier_finds(takes: Iterable[Any]) -> dict[str, EarlierFind]:
    """The steps *takes* (ready recordings, newest first) found their element for, by :func:`step_key`: the newest
    take that did, and the URL its page was at when the step started."""
    found: dict[str, EarlierFind] = {}
    for take in takes:
        try:
            steps = parse_take_script(take.script or {}).steps
        except Exception as exc:  # a script stored before a field changed: its finds are not read
            logger.debug("Recording %s's script not read for its finds: %s", take.id, format_exception_message(exc))
            continue
        for event in take.events or []:
            index = event.get("step")
            if not event.get("ok", True) or not isinstance(index, int) or not 0 <= index < len(steps):
                continue
            key = step_key(steps[index])
            url = str(event.get("url_before") or event.get("url") or "")
            if key and url and key not in found:
                found[key] = EarlierFind(str(take.id), str(take.name or ""), url)
    return found


async def load_earlier_finds(db: Any, *, take_id: object, user_id: object, profile_id: object) -> dict[str, EarlierFind]:
    """:func:`earlier_finds` of the ready recordings *user_id* made with *profile_id* in the last
    :data:`EARLIER_HOURS`, *take_id* aside; empty without a profile or a person."""
    from sqlalchemy import select

    from app.models.video_studio import JOB_READY, VideoTake

    if profile_id is None or user_id is None:
        return {}
    since = datetime.now(UTC) - timedelta(hours=EARLIER_HOURS)
    rows = (
        await db.execute(
            select(VideoTake)
            .where(
                VideoTake.user_id == user_id,
                VideoTake.profile_id == profile_id,
                VideoTake.status == JOB_READY,
                VideoTake.id != (take_id if isinstance(take_id, uuid.UUID) else uuid.UUID(str(take_id))),
                VideoTake.finished_at >= since,
            )
            .order_by(VideoTake.finished_at.desc())
            .limit(EARLIER_TAKES)
        )
    ).scalars()
    return earlier_finds(rows.all())


# ── The page a step missed its element on ─────────────────────────────────


@dataclass(frozen=True)
class PageState:
    """The page when a step missed its element (:func:`page_state`)."""

    url: str
    title: str
    #: The elements of the step's role the page showed (``role``), or those matching its text or selector and how many
    #: of them were visible; ``None`` when the page could not be read.
    looked_for: str | None = None
    count: int | None = None
    visible: int | None = None

    def as_dict(self) -> dict:
        said: dict[str, Any] = {"url": self.url, "title": self.title}
        if self.count is not None:
            said["candidates"] = {"of": self.looked_for, "count": self.count, **({"visible": self.visible} if self.visible is not None else {})}
        return said

    def words(self) -> str:
        """``The page was at https://.../graph?layoutMode=radial ("Report"), showing 3 elements of role button``."""
        said = f"The page was at {self.url or 'nothing'}" + (f' ("{self.title}")' if self.title else "")
        if self.count is None:
            return said
        if self.looked_for and self.looked_for.startswith("role "):
            return said + f", showing {self.count} element{'s' if self.count != 1 else ''} of {self.looked_for}"
        if not self.count:
            return said + f", where nothing matches its {self.looked_for}"
        shown = "none of them shown" if not self.visible else f"{self.visible} shown"
        return said + f", where {self.count} element{'s' if self.count != 1 else ''} match its {self.looked_for} ({shown})"


async def _visible(locator: Any, count: int) -> int:
    shown = 0
    for i in range(min(count, _VISIBLE_READ)):
        try:
            shown += bool(await locator.nth(i).is_visible())
        except Exception as exc:  # detached meanwhile: not shown
            logger.debug("Match %d of a missed step not read: %s", i, format_exception_message(exc))
    return shown


async def page_state(page: Any, step: TakeStep) -> PageState:
    """The page a step that looks for an element missed it on: its URL, its title, the elements of the step's role it
    showed - or those matching its text or its selector, and how many of them were visible."""
    title = ""
    try:
        title = str(await page.title())[:200]
    except Exception as exc:  # navigating: read as untitled
        logger.debug("Page title not read: %s", format_exception_message(exc))
    url = str(page.url or "")
    try:
        if step.role:
            return PageState(url, title, f"role {step.role}", await page.get_by_role(step.role).count())
        if step.selector:
            locator, what = page.locator(step.selector), "selector"
        elif step.text and step.action != "type":
            locator, what = page.get_by_text(step.text, exact=False), "text"
        else:
            return PageState(url, title)
        count = await locator.count()
        return PageState(url, title, what, count, await _visible(locator, count) if count else 0)
    except Exception as exc:  # a page navigating or closing: its URL and title say what they can
        logger.debug("Candidates of a missed step not read: %s", format_exception_message(exc))
        return PageState(url, title)


def missed_words(state: PageState | None, earlier: EarlierFind | None) -> str:
    """What a step that missed its element says of its page: :meth:`PageState.words`, and where an earlier
    recording found it - "found on take X at URL Y; this take is at URL Z: the page is in another state"."""
    if state is None:
        return ""
    said = state.words()
    if earlier is not None:
        named = f' ("{earlier.name}")' if earlier.name else ""
        if earlier.url == state.url:
            said += (
                f". It was found on take {earlier.take_id}{named} at this same URL: the page is in another state "
                "(its storage, or the application's own data)"
            )
        else:
            said += (
                f". It was found on take {earlier.take_id}{named} at {earlier.url}; this take is at {state.url}: the "
                "page is in another state"
            )
    return said


# ── What a take opened on ─────────────────────────────────────────────────


def added_query(asked: str, url: str) -> list[list[str]]:
    """The query parameters *url* carries that *asked* did not (``[[name, value], ...]``): what the page wrote into its
    address itself as it opened."""
    try:
        given = set(parse_qsl(urlparse(asked).query, keep_blank_values=True))
        held = parse_qsl(urlparse(url).query, keep_blank_values=True)
    except ValueError:
        return []
    return [[name[:40], value[:120]] for name, value in held if (name, value) not in given][:8]


def _names(keys: list[str]) -> str:
    shown = ", ".join(keys[:_NAMES_SAID])
    return shown + (f" +{len(keys) - _NAMES_SAID}" if len(keys) > _NAMES_SAID else "")


def cleared_words(cleared: dict | None) -> str:
    """``cleared 5 localStorage keys (layoutMode, graph, ...) and 2 sessionStorage keys (...); kept auth`` - what a
    page state reset (``browser.open_cleared``) removed; cookies are never touched."""
    cleared = cleared or {}
    if cleared.get("unread"):
        return "its storage cleared (the page did not say what it held)"
    parts = []
    for key, store in (("local_storage", "localStorage"), ("session_storage", "sessionStorage")):
        keys = [str(name) for name in cleared.get(key) or []]
        if keys:
            parts.append(f"{len(keys)} {store} key{'s' if len(keys) != 1 else ''} ({_names(keys)})")
    databases = [str(name) for name in cleared.get("indexed_db") or []]
    if databases:
        parts.append(f"{len(databases)} IndexedDB database{'s' if len(databases) != 1 else ''} ({_names(databases)})")
    said = "cleared " + " and ".join(parts) if parts else "nothing to clear in its storage"
    if kept := [str(name) for name in cleared.get("kept") or []]:
        said += f"; kept {_names(kept)} (the profile's keep_storage_keys)"
    return said + "; cookies untouched"


#: Where a take's capture keeps the build of the worker that recorded it, and of the one that finished it after a
#: relaunch (#11407).
BUILD_KEY = "build"
RESUMED_BUILD_KEY = "resumed_build"


def build_record() -> dict[str, str]:
    """The build this worker runs: ``{"version": "1.261112.0-dev.0fed327", "commit": "0fed327"}`` (the process's
    ``GIT_SHA``, else the commit its development version names); no ``commit`` when neither says one."""
    from app.services.platform_builds import running_build, version_git_sha

    version, sha = running_build()
    commit = (sha or version_git_sha(version)).strip().lower()[:7]
    return {"version": version, "commit": commit} if commit else {"version": version}


def _build_said(record: Any) -> str:
    if not isinstance(record, dict) or not record.get("version"):
        return ""
    commit = record.get("commit")
    return f"build {record['version']}" + (f" ({commit})" if commit else "")


def build_words(captured: dict[str, Any] | None) -> str:
    """``build 1.261112.0-dev.0fed327 (0fed327)``: the build that recorded a take (``capture.build``); empty for a take
    recorded before it was kept."""
    return _build_said((captured or {}).get(BUILD_KEY)) if isinstance(captured, dict) else ""


def resumed_build_words(captured: dict[str, Any] | None) -> str:
    """``finished on build ... (...) after a relaunch``: the build of the worker that finished a take from its stored
    keyframes (``capture.resumed_build``); empty for a take finished by the worker that recorded it."""
    said = _build_said((captured or {}).get(RESUMED_BUILD_KEY)) if isinstance(captured, dict) else ""
    return f"finished on {said} after a relaunch" if said else ""


def opened_words(
    opened: dict | None,
    fallback: str | None = None,
    waited: str | None = None,
    worker: dict[str, Any] | None = None,
    *,
    build: str | None = None,
    resumed: str = "",
) -> str:
    """The first line of a take's action log: the URL it opened on, its page state reset - what it cleared, or
    why none ran -, the parameters the page wrote into its address itself, the size or rate a worker short of the
    take's fps recorded it at instead (*fallback*, ``capture_rate.CapturePlan.fallback``), and how long it waited in
    the recording lane for its worker's load (*waited*, ``recording_lane.Wait.waited_words``). It starts with the
    worker class and GPU model that recorded it (*worker*, ``worker_class.words``), after the build that did (*build*,
    :func:`build_words`; ``""`` ends it with "build not recorded", ``None`` says nothing) and, after a relaunch, the build that
    finished it (*resumed*, :func:`resumed_build_words`)."""
    from app.services.video_studio.worker_class import words as worker_words

    if not isinstance(opened, dict) or not opened.get("url"):
        return ""
    said = f"opened on {opened['url']}"
    on = worker_words(worker)
    if build:
        said = f"recorded on {build}" + (f" by {on}" if on else "") + (f", {resumed}" if resumed else "") + f", {said}"
    else:
        if on:
            said = f"recorded on {on}, {said}"
        if resumed:
            said = f"{resumed}, {said}"
    if isinstance(opened.get("cleared"), dict):
        how = "start_url opened as given" if (opened.get("reset") or {}).get("url_query", True) else "the page reloaded where it stood"
        said += f" - page state reset before step 0: {cleared_words(opened['cleared'])}; {how}"
    elif opened.get("carried") is False:
        said += " - a new browser, no saved session carrying a page state"
    else:
        said += " - reset_state false: in the page state the profile's saved session carried from the takes before"
    if added := opened.get("added_query"):
        written = ", ".join(f"{name}={value}" for name, value in added)
        said += f"; the page wrote {written} into its address itself (a state its storage reset does not reach)"
    if fallback:
        said += f"; {fallback}"
    if waited:
        said += f"; {waited}"
    if build == "":
        # Recorded before its build was kept (#11407).
        said += "; build not recorded"
    return said
