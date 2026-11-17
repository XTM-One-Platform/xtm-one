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

"""Programs: a status computed live from work items and pull requests (#10833).

A program is a work item, its root: the item whose correlation key is
``program:<slug>`` (no ``/`` after the slug). Its payload may describe it::

    payload.program = {
        "owner": "who answers for it",
        "target_date": "2026-12-01",
        "exit_criterion": "what done means",
        "next_owed_to_user": "the next step a person is owed, if any",
    }

Its children are the items whose correlation key starts with
``program:<slug>/`` and the items of any key whose ``payload.program_key`` is
``program:<slug>`` (a ``dev:`` or ``ux:`` item naming the program it serves).
Creating the root is the whole registration: nothing else lists a program.

The status is computed at call time and never stored: every child is read
from the work items, every pull request a child names (``payload.pull_requests``
and the pull request references of ``blocked_on_refs``) from its code host
through the readers the work-item waits already use
(``integrations/reference_state``). Each line carries the time it was read and
the id of the object it came from. A shared-state register or an agent's
memory is never read: a status copied by hand drifts, this one cannot.

A ``next_owed_to_user`` with no open child that waits for a person and has a
due date is flagged: a step owed to a person is a dated work item, never prose.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.core.utils import normalize_correlation_key, safe_uuid
from app.models.work_item import WORK_ITEM_OPEN_STATUSES, WORK_ITEM_TERMINAL_STATUSES

#: The correlation key prefix of a program root.
PROGRAM_KEY_PREFIX = "program:"
#: Children one status reads (the largest page ``list_work_items`` serves).
MAX_CHILDREN = 500
#: Programs one listing covers.
MAX_PROGRAMS = 100
#: Pull requests one status reads live: the cap of the readers it calls
#: (``work_item_pull_requests.MAX_PULL_REQUESTS``).
MAX_PULL_REQUESTS = 10
#: The fields of ``payload.program`` a status shows.
PROGRAM_FIELDS = ("owner", "target_date", "exit_criterion", "next_owed_to_user")


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def program_key(value: str) -> str | None:
    """The root key *value* names (``program:<slug>`` or ``<slug>``), ``None`` when none. Pure."""
    key = normalize_correlation_key(value or "")
    if not key:
        return None
    if not key.startswith(PROGRAM_KEY_PREFIX):
        key = PROGRAM_KEY_PREFIX + key
    slug = key[len(PROGRAM_KEY_PREFIX) :]
    if not slug or "/" in slug:
        return None
    return key


def with_program_key(payload: dict[str, Any] | None, key: str | None) -> dict[str, Any]:
    """A copy of *payload* whose ``program_key`` is *key*, dropped when *key* is empty. Pure.

    The other payload keys are kept as they are: linking an item to a program
    never touches its correlation key, so the inbox that routes it by that key
    still does (#10852).
    """
    out = dict(payload or {})
    if key:
        out["program_key"] = key
    else:
        out.pop("program_key", None)
    return out


def is_program_root(item: dict[str, Any]) -> bool:
    """Whether *item* is a program root: its key is ``program:<slug>``. Pure."""
    key = item.get("correlation_key") or ""
    return key.startswith(PROGRAM_KEY_PREFIX) and program_key(key) == key


def program_meta(item: dict[str, Any]) -> dict[str, Any]:
    """What ``payload.program`` of the root *item* says, its known fields only. Pure."""
    raw_payload = item.get("payload")
    payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
    raw_meta = payload.get("program")
    meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
    out: dict[str, Any] = {}
    for name in PROGRAM_FIELDS:
        value = meta.get(name)
        if isinstance(value, str) and value.strip():
            out[name] = value.strip()
    if "owner" not in out and item.get("owner_agent_id"):
        out["owner"] = item["owner_agent_id"]
    if "target_date" not in out and item.get("due_at"):
        out["target_date"] = item["due_at"]
    return out


def linked_pull_requests(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every pull request *items* name, once, with the ids of the items naming it. Pure.

    Read from ``payload.pull_requests`` and from the ``pull_request`` and
    ``deployment`` references of ``blocked_on_refs`` that carry a number.
    """
    from app.services.work_item_pull_requests import pull_request_bindings

    found: dict[tuple[str, int], dict[str, Any]] = {}
    for item in items:
        named: list[tuple[str, int]] = []
        for binding in pull_request_bindings(item.get("payload")):
            named.append((binding["repo"], binding["number"]))
        for ref in item.get("blocked_on_refs") or []:
            if not isinstance(ref, dict) or ref.get("kind", "pull_request") not in ("pull_request", "deployment"):
                continue
            repo, number = ref.get("repo"), ref.get("number")
            if isinstance(repo, str) and repo.count("/") == 1 and isinstance(number, int) and number > 0:
                named.append((repo.strip().lower(), number))
        for key in named:
            entry = found.setdefault(key, {"repo": key[0], "number": key[1], "item_ids": []})
            if item["id"] not in entry["item_ids"]:
                entry["item_ids"].append(item["id"])
    return list(found.values())


def _age_hours(since: str | None, now: datetime) -> float | None:
    if not since:
        return None
    try:
        then = datetime.fromisoformat(since)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return round((now - then).total_seconds() / 3600, 1)


def _waits_for_person(item: dict[str, Any]) -> bool:
    return bool(item.get("needs_human")) or bool(item.get("ask"))


def _child_line(item: dict[str, Any], read_at: str) -> dict[str, Any]:
    return {
        "id": item["id"],
        "title": item.get("title"),
        "correlation_key": item.get("correlation_key"),
        "status": item.get("status"),
        "owner_agent_id": item.get("owner_agent_id"),
        "blocked_on": list(item.get("blocked_on") or []),
        "blocked_on_refs": list(item.get("blocked_on_refs") or []),
        "needs_human": bool(item.get("needs_human")),
        "due_at": item.get("due_at"),
        "completed_at": item.get("completed_at"),
        "read_at": read_at,
    }


def compute_program_status(
    root: dict[str, Any],
    children: list[dict[str, Any]],
    pull_requests: list[dict[str, Any]],
    *,
    now: datetime,
) -> dict[str, Any]:
    """The status of the program *root*, from what was read at *now*. Pure.

    *pull_requests* are :func:`linked_pull_requests` entries with what the code
    host answered: ``state`` (``open`` / ``merged`` / ``closed`` / ``unread``),
    and for an open one ``head_sha``, ``draft`` and ``ci`` (``success`` /
    ``failure`` / ``None`` while it runs), each with its ``read_at``.
    """
    read_at = _stamp(now)
    meta = program_meta(root)
    counts: dict[str, int] = {}
    for child in children:
        counts[child.get("status") or "unknown"] = counts.get(child.get("status") or "unknown", 0) + 1
    open_children = [c for c in children if c.get("status") in WORK_ITEM_OPEN_STATUSES]
    next_steps = [
        _child_line(c, read_at)
        for c in open_children
        if not c.get("blocked_on") and not c.get("blocked_on_refs") and c.get("status") != "blocked"
    ]
    owed = [
        {**_child_line(c, read_at), "age_hours": _age_hours(c.get("created_at"), now), "ask": c.get("ask")}
        for c in open_children
        if _waits_for_person(c)
    ]
    flags: list[str] = []
    if meta.get("next_owed_to_user") and not any(o.get("due_at") for o in owed):
        flags.append(
            "next_owed_to_user has no dated work item: create one waiting for the person "
            "(needs_human or ask, with due_at) under the program"
        )
    finished = root.get("status") in WORK_ITEM_TERMINAL_STATUSES
    if not finished and children and not open_children:
        flags.append("every child is finished but the root is still open: check the exit criterion")
    return {
        "program": root.get("correlation_key"),
        "root": {
            "id": root["id"],
            "title": root.get("title"),
            "status": root.get("status"),
            "read_at": read_at,
            **meta,
        },
        "read_at": read_at,
        "children_by_status": counts,
        "children": [_child_line(c, read_at) for c in children],
        "pull_requests": pull_requests,
        "next_steps": next_steps,
        "owed_to_user": owed,
        "flags": flags,
    }


async def read_pull_requests(entries: list[dict[str, Any]], user_id: Any) -> list[dict[str, Any]]:
    """*entries* with their state read live from the code host now. Never raises.

    A merged or closed pull request answers its closing (merge SHA, when);
    an open one its head, draft flag and CI verdict; one no reader answers
    for is ``unread``, never guessed.
    """
    from app.services.work_item_pull_requests import read_live_ci, read_pull_request_closings

    entries = entries[:MAX_PULL_REQUESTS]
    closings = {(e.repo, n): e for e in await read_pull_request_closings(entries, user_id) for n in e.pr_numbers}
    open_ones = [e for e in entries if (e["repo"], e["number"]) not in closings]
    live = await read_live_ci(open_ones, user_id) if open_ones else {}
    read_at = _stamp(_now())
    out: list[dict[str, Any]] = []
    for entry in entries:
        key = (entry["repo"], entry["number"])
        line = {**entry, "read_at": read_at}
        closing = closings.get(key)
        if closing is not None:
            line.update(
                state="merged" if closing.merged else "closed",
                merge_sha=closing.merge_sha,
                closed_at=closing.closed_at,
                url=closing.url,
            )
        elif key in live:
            state = live[key]
            line.update(
                state="open",
                head_sha=state.get("head_sha"),
                draft=state.get("draft"),
                ci=state.get("conclusion"),
                run_url=state.get("run_url"),
            )
        else:
            line["state"] = "unread"
        out.append(line)
    return out


async def find_program_root(
    program: str, *, user_id: str | None, reader: Any = None, scope: Any = None
) -> dict[str, Any] | None:
    """The visible root *program* names: its key, its slug, or its item id."""
    from app.services.work_item import get_work_item, list_work_items

    text = (program or "").strip()
    if safe_uuid(text) is not None:
        item = await get_work_item(text, user_id=user_id, reader=reader)
        return item if item is not None and is_program_root(item) else None
    key = program_key(text)
    if key is None:
        return None
    roots = await list_work_items(user_id=user_id, correlation_key=key, reader=reader, scope=scope, limit=20)
    roots = [r for r in roots if is_program_root(r)]
    if not roots:
        return None
    # The open root wins over a finished one with the same key.
    roots.sort(key=lambda r: r.get("status") in WORK_ITEM_TERMINAL_STATUSES)
    return roots[0]


async def program_children(
    root: dict[str, Any], *, user_id: str | None, reader: Any = None, scope: Any = None
) -> list[dict[str, Any]]:
    """Every visible child of *root*, read now."""
    from app.services.work_item import list_work_items

    return await list_work_items(
        user_id=user_id,
        program_key=root["correlation_key"],
        reader=reader,
        scope=scope,
        limit=MAX_CHILDREN,
        sort="updated_desc",
    )


async def get_program_status(
    program: str, *, user_id: str | None, reader: Any = None, scope: Any = None
) -> dict[str, Any] | None:
    """The live status of *program*, ``None`` when no visible root carries it."""
    root = await find_program_root(program, user_id=user_id, reader=reader, scope=scope)
    if root is None:
        return None
    children = await program_children(root, user_id=user_id, reader=reader, scope=scope)
    prs = await read_pull_requests(linked_pull_requests([root, *children]), user_id)
    return compute_program_status(root, children, prs, now=_now())


async def list_programs(
    *, user_id: str | None, open_only: bool = True, reader: Any = None, scope: Any = None
) -> list[dict[str, Any]]:
    """Every visible program root with its children counted by status, read now.

    No pull request is read here: :func:`get_program_status` reads them for one.
    """
    from app.services.work_item import list_work_items

    roots = await list_work_items(
        user_id=user_id,
        correlation_key_prefix=PROGRAM_KEY_PREFIX,
        open_only=open_only,
        reader=reader,
        scope=scope,
        limit=MAX_CHILDREN,
    )
    out: list[dict[str, Any]] = []
    for root in [r for r in roots if is_program_root(r)][:MAX_PROGRAMS]:
        children = await program_children(root, user_id=user_id, reader=reader, scope=scope)
        status = compute_program_status(root, children, [], now=_now())
        out.append(
            {
                "program": status["program"],
                "root": status["root"],
                "children_by_status": status["children_by_status"],
                "next_steps": len(status["next_steps"]),
                "owed_to_user": len(status["owed_to_user"]),
                "flags": status["flags"],
                "read_at": status["read_at"],
            }
        )
    return out
