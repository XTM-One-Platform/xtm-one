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

"""A shared-state entry that tracks one pull request ends with it.

Agents keep per-pull-request entries in shared state: a branch lease
(``dev:branch-lock:<owner/name>:<head branch>`` holding ``{"pr": 12, ...}``;
the old form ``dev:branch-lock:<head branch>`` is still read), a babysitting
record, a review round. The run that merges the pull request is meant to
resolve them, and often does not (it was cut, another run merged, the step was
forgotten), so they stay ``active`` after the pull request is gone and a sweep
has to find them by hand.

When the platform learns that a pull request closed (merged or not), it
resolves every ACTIVE entry whose value names that pull request
(:func:`entry_names_pull_request`):

* the number under ``pr``, ``pr_number`` or ``pull_request`` (an integer, a
  digit string, ``#12``, or an object with ``number``), and
* the same pull request, not one with the same number in another repository:
  the repository the value names (``repository`` / ``repo``, or the object's
  ``repo``) is the pull request's; with no repository named, the head branch of
  the pull request is the value's ``branch`` / ``head_branch`` or the last
  ``:`` segment of the key (``dev:branch-lock:fix/12-x`` names ``fix/12-x``).
  A repository-scoped key (``dev:branch-lock:acme/widgets:fix/12-x``,
  :func:`repository_in_key`) names its repository when the value names none,
  so two repositories with the same branch name never touch each other's lease.

An entry that names NO pull request number is matched by its branch alone
(#10780): a lease taken before the pull request was opened keeps
``{"repository": ..., "issue": ...}`` and its key is the head branch. It is
resolved when it names the closing's repository and its key's last ``:``
segment (or ``branch`` / ``head_branch``) is the closing's head branch. With
no repository named it is left alone: a branch name is not unique across
repositories.

The entry gets ``resolved`` with a dated note naming the pull request and how
it ended; its value, owner and hold are untouched, and any later write
reactivates it as before. A system write, like the hold release sweep
(``shared_state.release_ended_run_holds``): no writer, best-effort, never raises.

The same matcher keeps a work item from being dispatched while a LIVE run
holds a lease on one of its open pull requests
(:func:`live_pull_request_lease`): that run is pushing the fix, and a second
run would only read the lease and stand down (#10737).

A delivery that is not signed is never trusted with an entry's state
(:func:`resolve_from_unverified_delivery`): the pull request is read back from
the code host with the entry owner's credential, through the reader the
blocked-references fallback uses, and only a closing that read confirms
resolves anything.
"""

from __future__ import annotations

import dataclasses
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Text, cast, or_, select

from app.core.utils import format_exception_message, safe_uuid
from app.integrations.pull_request_events import PullRequestEvent

logger = logging.getLogger(__name__)

#: Value fields that name the pull request an entry tracks.
PR_FIELDS = ("pr", "pr_number", "pull_request")
#: Value fields that name its repository (``owner/name``).
REPO_FIELDS = ("repository", "repo")
#: Value fields that name its head branch.
BRANCH_FIELDS = ("branch", "head_branch")
#: Entries one closing may resolve (a pull request is normally named by a few).
MAX_ENTRIES_PER_EVENT = 50
#: Candidate rows read per closing, before the exact match in Python.
_CANDIDATE_ROWS = 200
#: An ``owner/name`` key segment (the repository of a scoped branch lease).
_REPO_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
#: An ``owner/name@<head sha>`` key segment (the repository of a per-head claim, #12040).
_REPO_AT_HEAD_SEGMENT = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@[0-9a-fA-F]{7,40}$")

#: ``(id, user_id, key, value)`` of a candidate entry.
Row = tuple[uuid.UUID, Any, str, Any]


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_number(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        text = value.strip().lstrip("#")
        if text.isdigit() and int(text) > 0:
            return int(text)
    return None


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def pull_request_named(value: object) -> tuple[int, str | None] | None:
    """``(number, repository or None)`` of the pull request *value* names; ``None`` when none. Pure."""
    if not isinstance(value, dict):
        return None
    repo = next((r for r in (_text(value.get(f)) for f in REPO_FIELDS) if r), None)
    for field in PR_FIELDS:
        raw = value.get(field)
        if isinstance(raw, dict):
            number = _as_number(raw.get("number"))
            nested = _text(raw.get("repo")) or _text(raw.get("repository"))
            if number:
                return number, (nested or repo)
            continue
        number = _as_number(raw)
        if number:
            return number, repo
    return None


def repository_in_key(key: str) -> str | None:
    """The ``owner/name`` a repository-scoped key names before its branch, ``None`` for an old-form key. Pure.

    A branch lease is keyed ``dev:branch-lock:<owner/name>:<branch>``: a branch
    name is not unique across repositories (``chore/chatbot-3.29.0`` exists on
    several), so the repository is part of the key. A git ref never holds a
    ``:``, so the branch is the key's last ``:`` segment and the repository the
    one before it when it reads ``owner/name``. The old form
    ``dev:branch-lock:<branch>`` names none and is still honoured by its branch.

    A per-head claim ``dev:green-claim:<owner/name>@<head sha>`` (the run that
    merges a green head holds it) names its repository before the ``@``: a live
    holder keeps the item undispatched like a branch lease (#12040).
    """
    parts = (key or "").split(":")
    if len(parts) < 3:
        return None
    at_head = _REPO_AT_HEAD_SEGMENT.match(parts[-1].strip())
    if at_head:
        return at_head.group(1)
    candidate = parts[-2].strip()
    return candidate if _REPO_SEGMENT.match(candidate) else None


def _repository_named(key: str, value: object, named: str | None = None) -> str | None:
    """The repository an entry names: the pull request's own, the value's, else its key's."""
    if named:
        return named
    if isinstance(value, dict):
        from_value = next((r for r in (_text(value.get(f)) for f in REPO_FIELDS) if r), None)
        if from_value:
            return from_value
    return repository_in_key(key)


def entry_names_pull_request(key: str, value: object, *, repo: str, number: int, head_branch: str | None) -> bool:
    """Whether the entry *key* / *value* tracks pull request *repo* ``#`` *number*. Pure.

    The number must match, then the repository the value names (or, with none
    in the value, the repository a scoped key ``...:<owner/name>:<branch>``
    names), or with none named at all the head branch (a value field or the
    key's last ``:`` segment).
    """
    named = pull_request_named(value)
    if named is None:
        return _names_branch_only(key, value, repo=repo, head_branch=head_branch)
    if named[0] != number:
        return False
    named_repo = _repository_named(key, value, named[1])
    if named_repo:
        return named_repo.lower() == (repo or "").lower()
    if not head_branch or not isinstance(value, dict):
        return False
    return head_branch in _branches(key, value)


def _branches(key: str, value: dict[str, Any]) -> set[str]:
    """The branches an entry names: its ``branch`` / ``head_branch`` fields and its key's last ``:`` segment."""
    branches = {b for b in (_text(value.get(f)) for f in BRANCH_FIELDS) if b}
    tail = (key or "").rsplit(":", 1)[-1].strip()
    if tail:
        branches.add(tail)
    return branches


def _names_branch_only(key: str, value: object, *, repo: str, head_branch: str | None) -> bool:
    """An entry naming no pull request number tracks the head branch of *repo* it names. Pure."""
    if not head_branch or not isinstance(value, dict):
        return False
    named_repo = _repository_named(key, value)
    if not named_repo or named_repo.lower() != (repo or "").lower():
        return False
    return head_branch in _branches(key, value)


def _like_literal(text: str) -> str:
    """*text* escaped for a ``LIKE`` pattern with ``\\`` as the escape character."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def resolved_note(event: PullRequestEvent, number: int, now: datetime) -> str:
    """The note of an entry resolved because its pull request closed. Pure."""
    from app.services.work_item_blocked_refs import closing_stamp

    how = "merged" if event.merged else "closed unmerged"
    sha = f" ({event.merge_sha[:8]})" if event.merged and event.merge_sha else ""
    return (
        f"resolved at {_stamp(now)}: pull request {event.repo}#{number} {how}{sha} at "
        f"{closing_stamp(event, now)}; the entry tracks it (a later write reactivates it)"
    )


async def _candidates(db: Any, number: int, head_branch: str | None = None) -> list[Row]:
    """Active entries whose value text holds *number*, or whose key ends with or value holds *head_branch*.

    ``(id, user_id, key, value)``; the exact match is :func:`entry_names_pull_request`.
    """
    from app.models.shared_state import SHARED_STATE_ACTIVE_STATUS, SharedState

    text = cast(SharedState.value, Text)
    terms = [text.like(f"%{number}%")]
    if head_branch:
        branch = _like_literal(head_branch)
        terms += [SharedState.key.like(f"%:{branch}", escape="\\"), text.like(f"%{branch}%", escape="\\")]
    match = or_(*terms)
    rows = await db.execute(
        select(SharedState.id, SharedState.user_id, SharedState.key, SharedState.value)
        .where(SharedState.status == SHARED_STATE_ACTIVE_STATUS, match)
        .order_by(SharedState.updated_at.desc())
        .limit(_CANDIDATE_ROWS)
    )
    return [(row[0], row[1], row[2], row[3]) for row in rows.all()]


def _matching(rows: list[Row], event: PullRequestEvent, number: int) -> list[Row]:
    return [
        row
        for row in rows
        if entry_names_pull_request(row[2], row[3], repo=event.repo, number=number, head_branch=event.head_branch)
    ][:MAX_ENTRIES_PER_EVENT]


async def _resolve_entry(state_id: uuid.UUID, event: PullRequestEvent, number: int, now: datetime) -> bool:
    """Resolve one entry on its locked row when it still tracks the pull request and is active."""
    from app.core.database import async_session
    from app.models.shared_state import (
        SHARED_STATE_ACTIVE_STATUS,
        SHARED_STATE_RESOLVED_STATUS,
        SharedState,
    )
    from app.services.shared_state import _MAX_NOTES

    async with async_session() as db:
        state = (
            await db.execute(select(SharedState).where(SharedState.id == state_id).with_for_update())
        ).scalar_one_or_none()
        if state is None or state.status != SHARED_STATE_ACTIVE_STATUS:
            return False
        if not entry_names_pull_request(
            state.key, state.value, repo=event.repo, number=number, head_branch=event.head_branch
        ):
            return False  # rewritten meanwhile
        state.status = SHARED_STATE_RESOLVED_STATUS
        state.resolved_at = now
        notes = list(state.notes or [])
        notes.append({"at": now.isoformat(), "agent_id": None, "note": resolved_note(event, number, now)[:2000]})
        state.notes = notes[-_MAX_NOTES:]
        await db.commit()
        return True


async def _resolve_rows(rows: list[Row], event: PullRequestEvent, number: int) -> int:
    resolved = 0
    for state_id, _user_id, key, _value in rows:
        try:
            if await _resolve_entry(state_id, event, number, _now()):
                resolved += 1
        except Exception as exc:
            logger.warning(
                "Shared state %s not resolved on %s#%s: %s", key, event.repo, number, format_exception_message(exc)
            )
    return resolved


def leases_naming(rows: list[tuple[str, Any, Any]], bindings: list[dict]) -> list[tuple[str, Any, dict]]:
    """``(key, holder run id, binding)`` of each held entry of *rows* naming one of *bindings*. Pure.

    *rows* are ``(key, value, held_by_run_id)``; *bindings* the open pull
    requests of a work item (``{repo, number, head_branch?}``).
    """
    found: list[tuple[str, Any, dict]] = []
    for key, value, run_id in rows:
        if run_id is None:
            continue
        for binding in bindings:
            if entry_names_pull_request(
                key, value, repo=binding["repo"], number=binding["number"], head_branch=binding.get("head_branch")
            ):
                found.append((key, run_id, binding))
                break
    return found


async def _held_rows(user_id: Any) -> list[tuple[str, Any, Any]]:
    """``(key, value, held_by_run_id)`` of the active held entries *user_id* or the company owns."""
    from sqlalchemy import or_

    from app.core.database import async_session
    from app.models.shared_state import SHARED_STATE_ACTIVE_STATUS, SharedState

    owner = safe_uuid(user_id) if user_id else None
    scope = SharedState.is_company_managed.is_(True)
    if owner is not None:
        scope = or_(SharedState.user_id == owner, scope)
    async with async_session() as db:
        rows = await db.execute(
            select(SharedState.key, SharedState.value, SharedState.held_by_run_id)
            .where(
                SharedState.status == SHARED_STATE_ACTIVE_STATUS,
                SharedState.held_by_run_id.is_not(None),
                scope,
            )
            .order_by(SharedState.updated_at.desc())
            .limit(_CANDIDATE_ROWS)
        )
        return [(row[0], row[1], row[2]) for row in rows.all()]


async def _alive(run_ids: list[Any]) -> set[Any]:
    from app.core.database import async_session
    from app.services.work_item import holding_runs

    async with async_session() as db:
        holders = await holding_runs(db, run_ids)
    return {rid for rid, holder in holders.items() if holder.alive}


async def live_pull_request_lease(payload: object, *, user_id: Any) -> dict | None:
    """The lease a LIVE run holds on one of the open pull requests a work item's *payload* names.

    ``{"key", "run_id", "repo", "number"}`` of the first such entry (a
    shared-state entry held with ``hold="take"`` whose value names the pull
    request, :func:`entry_names_pull_request`), ``None`` when there is none.
    An item naming no open pull request reads nothing. Never raises: a failed
    read answers ``None`` and the item is dispatched as before.
    """
    from app.services.work_item_pull_requests import pull_request_bindings

    bindings = [b for b in pull_request_bindings(payload) if b.get("state", "open") == "open"]
    if not bindings:
        return None
    try:
        leases = leases_naming(await _held_rows(user_id), bindings)
        if not leases:
            return None
        alive = await _alive([run_id for _, run_id, _ in leases])
    except Exception as exc:
        logger.warning("Pull request leases unreadable: %s", format_exception_message(exc))
        return None
    for key, run_id, binding in leases:
        if run_id in alive:
            return {"key": key, "run_id": str(run_id), "repo": binding["repo"], "number": binding["number"]}
    return None


def entry_names_branch(key: str, value: object, *, repo: str, branch: str) -> bool:
    """Whether the entry *key* / *value* tracks *branch* of *repo*, pull request number or not. Pure.

    The repository the value names (``repository`` / ``repo``, or the pull
    request object's ``repo``), else the repository a scoped key names; the
    branch a ``branch`` / ``head_branch`` field or the key's last ``:``
    segment names. With no repository named it is left alone: a branch name is
    not unique across repositories.
    """
    if not branch or not isinstance(value, dict):
        return False
    named = pull_request_named(value)
    named_repo = _repository_named(key, value, named[1] if named else None)
    if not named_repo or named_repo.lower() != (repo or "").lower():
        return False
    return branch in _branches(key, value)


async def live_branch_lease(repo: str, branch: str, *, user_id: Any, run_id: Any) -> dict | None:
    """The lease ANOTHER live run holds on *branch* of *repo*, before a write moves that branch.

    ``{"key", "run_id"}`` of the first active entry held with ``hold="take"``
    by a run other than *run_id* whose value names the branch
    (:func:`entry_names_branch`) while that run is alive; ``None`` otherwise.
    The write tools read it so a run with no item binding (an event run)
    stands down instead of pushing under the holder (the dispatch check,
    :func:`live_pull_request_lease`, only keeps an ITEM undispatched). Never
    raises: a failed read answers ``None`` and the write goes ahead as before.
    """
    repo = (repo or "").strip()
    branch = (branch or "").strip()
    if not repo or not branch:
        return None
    caller = str(run_id).strip().lower() if run_id else ""
    try:
        held = [
            (key, holder)
            for key, value, holder in await _held_rows(user_id)
            if holder is not None
            and str(holder).lower() != caller
            and entry_names_branch(key, value, repo=repo, branch=branch)
        ]
        if not held:
            return None
        alive = await _alive([holder for _, holder in held])
    except Exception as exc:
        logger.warning("Branch leases of %s:%s unreadable: %s", repo, branch, format_exception_message(exc))
        return None
    for key, holder in held:
        if holder in alive:
            return {"key": key, "run_id": str(holder)}
    return None


def branch_lease_refusal(repo: str, branch: str, lease: dict) -> str:
    """What a write tool answers instead of moving a branch another live run leases. Pure."""
    return (
        f"Nothing written: branch '{branch}' of {repo} is leased by run {lease['run_id']} "
        f"(shared state '{lease['key']}', held with hold='take'), which is still running and pushes "
        "there. This is not an error: stand down, or write again once that run has ended or released "
        "the lease."
    )


async def resolve_states_of_closed_pull_request(event: PullRequestEvent) -> int:
    """Resolve the active entries tracking the pull request a TRUSTED closing names.

    How many entries were resolved. Never raises.
    """
    from app.core.database import async_session

    if event.kind != "closed" or not event.repo:
        return 0
    resolved = 0
    for number in event.pr_numbers:
        try:
            async with async_session() as db:
                rows = _matching(await _candidates(db, number, event.head_branch), event, number)
        except Exception as exc:
            logger.warning("Shared state of %s#%s unreadable: %s", event.repo, number, format_exception_message(exc))
            continue
        resolved += await _resolve_rows(rows, event, number)
    if resolved:
        logger.info("Pull request %s closed: %d shared state entr(ies) resolved", event.repo, resolved)
    return resolved


async def resolve_from_unverified_delivery(event: PullRequestEvent) -> int:
    """Resolve the entries an UNSIGNED closing names, once the code host confirms it.

    The pull request is read back with the credential of an owner of a
    matching entry; the confirmed closing (how it ended, the merge commit, the
    time) is applied, the delivery's head branch kept only to match the
    entries. Nothing is resolved while the read says open or fails. Never raises.
    """
    from app.core.database import async_session
    from app.services.work_item_pull_requests import read_pull_request_closings

    if event.kind != "closed" or not event.repo:
        return 0
    resolved = 0
    for number in event.pr_numbers:
        try:
            async with async_session() as db:
                rows = _matching(await _candidates(db, number, event.head_branch), event, number)
        except Exception as exc:
            logger.warning("Shared state of %s#%s unreadable: %s", event.repo, number, format_exception_message(exc))
            continue
        owners = list(dict.fromkeys(row[1] for row in rows if row[1] is not None))
        confirmed: PullRequestEvent | None = None
        for user_id in owners[:3]:
            closings = await read_pull_request_closings([{"repo": event.repo, "number": number}], user_id)
            confirmed = next((c for c in closings if c.kind == "closed"), None)
            if confirmed is not None:
                break
        if confirmed is None:
            continue
        applied = dataclasses.replace(confirmed, repo=event.repo, head_branch=event.head_branch)
        resolved += await _resolve_rows(rows, applied, number)
    return resolved
