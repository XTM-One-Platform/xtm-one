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

"""Work items never wait on themselves through other items or pull requests (#606).

The wait graph of the open work items has two kinds of node, an item and a
pull request (``owner/name#12``), and three kinds of edge:

* item -> item: an id of its ``blocked_on``;
* item -> pull request: an open ``blocked_on_refs`` entry of kind
  ``pull_request``, or a ``deployment`` not deployed yet (its merge comes first);
* pull request -> item: an open item that gates that pull request's merge: a
  review or verdict item about it (:func:`~app.services.work_item_blocked_refs.own_pull_request`
  of its key, ``ux:<repo>-<n>`` or ``dev:ux-verdict-<n>-comment``), or an item
  naming it in ``payload.gates_pull_requests: [{repo, number}]``.

A cycle is a wait nothing can ever release: a fix item F waits on pull request
P, P merges only on the verdict of review item R, R waits on F. Three things
use the graph:

* :func:`refuse_wait_cycle` refuses the ``create_work_item`` /
  ``update_work_item`` write that would close a cycle, the cycle spelled out;
* :func:`release_wait_cycles` (the autonomous tick) breaks a cycle that exists
  anyway (rows written before this check, racing writes): the item of the cycle
  blocked most recently goes back to ``open`` with a note, and one
  ``work_item.wait_cycle`` platform event is raised per cycle;
* :func:`find_cycles` and :func:`stalled_chains` feed the ``wait_cycle`` and
  ``blocked_chain_stalled`` fleet health findings; :func:`person_wait_chains`
  lists apart the chains that end on a person's open decision (#2326): waiting
  on a person before the due date is the expected state under human gates, not
  a stall.

The graph rules are pure; only the loaders read the database.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import String, Text, cast, or_, select

from app.core.utils import format_exception_message
from app.models.work_item import WORK_ITEM_OPEN_STATUSES, WorkItem

logger = logging.getLogger(__name__)

#: Items read into one graph at most (write-time check and sweep).
MAX_GRAPH_ITEMS = 2000
#: Cycles released by one sweep at most.
MAX_RELEASES_PER_SWEEP = 20
#: A blocked chain whose items are all this quiet, none held by a live run, is stalled.
STALLED_AFTER = timedelta(hours=2)

#: The fix both refusals and notes propose.
CYCLE_ADVICE = (
    "Model a review gate as: the review item blocks on the fix item, the merge waits on the review; "
    "a fix item never waits on the merge of the pull request it fixes. Or drop one of these waits."
)


@dataclass
class WaitNode:
    """One open work item as the wait graph reads it."""

    id: str
    status: str
    blocked_on: list[str] = field(default_factory=list)
    refs: list[dict[str, Any]] = field(default_factory=list)
    gates: list[tuple[str | None, str | None, int]] = field(default_factory=list)
    last_activity_at: datetime | None = None
    held: bool = False
    #: A person's decision gate (#2326): ``needs_human`` with a card they can read.
    needs_human: bool = False
    card_ok: bool = False
    answered: bool = False
    due_at: datetime | None = None
    created_at: datetime | None = None
    #: Who the decision waits for: a user id, ``group:<ids>`` or ``administrators``.
    recipient: str | None = None


def pr_key(repo: str, number: int) -> str:
    return f"{repo.lower()}#{int(number)}"


def _gates(correlation_key: object, title: object, payload: object) -> list[tuple[str | None, str | None, int]]:
    """The pull requests whose merge *this* item gates: ``(repo, short name, number)``. Pure."""
    from app.services.work_item_blocked_refs import own_pull_request

    out: list[tuple[str | None, str | None, int]] = []
    own = own_pull_request(correlation_key, title)
    if own is not None:
        out.append((own.get("repo"), own.get("name"), int(own["number"])))
    named = payload.get("gates_pull_requests") if isinstance(payload, dict) else None
    for entry in named if isinstance(named, list) else []:
        if not isinstance(entry, dict):
            continue
        repo, number = entry.get("repo"), entry.get("number")
        if isinstance(number, str) and number.strip().lstrip("#").isdigit():
            number = int(number.strip().lstrip("#"))
        if isinstance(repo, str) and repo.count("/") == 1 and isinstance(number, int) and number > 0:
            out.append((repo.strip().lower(), None, number))
    return out


def node_of(
    item_id: object,
    *,
    status: str | None,
    blocked_on: object,
    blocked_on_refs: object,
    correlation_key: object,
    title: object,
    payload: object,
    last_activity_at: datetime | None = None,
    held: bool = False,
    needs_human: object = None,
    ask: object = None,
    due_at: datetime | None = None,
    created_at: datetime | None = None,
    recipient: str | None = None,
) -> WaitNode:
    """The :class:`WaitNode` of an item, from its fields as they land. Pure."""
    from app.services.work_item_ask_backfill import card_unreadable
    from app.services.work_item_blocked_refs import open_refs

    human = needs_human is True

    waits = [str(b) for b in (blocked_on if isinstance(blocked_on, list | tuple) else []) if b]
    refs = [
        r
        for r in open_refs(blocked_on_refs)
        if r.get("number")
        and (r.get("kind") == "pull_request" or (r.get("kind") == "deployment" and not r.get("merge_sha")))
    ]
    return WaitNode(
        id=str(item_id),
        status=str(status or "open"),
        blocked_on=waits,
        refs=refs,
        gates=_gates(correlation_key, title, payload),
        last_activity_at=last_activity_at,
        held=held,
        needs_human=human,
        card_ok=human and not card_unreadable(ask),
        answered=_answered(payload, ask),
        due_at=due_at,
        created_at=created_at,
        recipient=recipient,
    )


def _answered(payload: object, ask: object) -> bool:
    """Whether a person answered the item (``payload.answer``), a keep-waiting answer aside. Pure."""
    from app.services.decision_ask import keeps_waiting

    answer = payload.get("answer") if isinstance(payload, dict) else None
    if not isinstance(answer, dict):
        return False
    key = str(answer.get("option") or "").casefold()
    options = ask.get("options") if isinstance(ask, dict) else None
    chosen = next(
        (o for o in options or [] if isinstance(o, dict) and str(o.get("key", "")).casefold() == key and key),
        None,
    )
    return not keeps_waiting(chosen)


def _gates_pr(node: WaitNode, repo: str, number: int) -> bool:
    for g_repo, g_name, g_number in node.gates:
        if g_number != number:
            continue
        if g_repo is not None and g_repo == repo:
            return True
        if g_repo is None and g_name is not None and repo.split("/")[-1] == g_name:
            return True
    return False


def successors(node: WaitNode, nodes: dict[str, WaitNode]) -> list[tuple[str, str]]:
    """The ``(via, item id)`` pairs *node* waits on, one hop through a pull request. Pure.

    *via* is ``""`` for a direct ``blocked_on`` and ``owner/name#12`` for a wait on
    that pull request's merge gated by the item. Only open items count.
    """
    if node.status != "blocked":
        return []
    out: list[tuple[str, str]] = [("", b) for b in node.blocked_on if b in nodes]
    for ref in node.refs:
        repo, number = str(ref.get("repo") or "").lower(), int(ref["number"])
        for other in nodes.values():
            if other.id != node.id and _gates_pr(other, repo, number):
                out.append((pr_key(repo, number), other.id))
    return out


def cycle_through(start: str, nodes: dict[str, WaitNode]) -> list[tuple[str, str]] | None:
    """The cycle back to *start*, as ``[(via, item id), ...]`` ending on *start*; ``None`` when none. Pure."""
    if start not in nodes:
        return None
    stack: list[tuple[str, list[tuple[str, str]]]] = [(start, [])]
    seen: set[str] = set()
    while stack:
        current, path = stack.pop()
        for via, nxt in successors(nodes[current], nodes):
            step = [*path, (via, nxt)]
            if nxt == start:
                return step
            if nxt not in seen:
                seen.add(nxt)
                stack.append((nxt, step))
    return None


def find_cycles(nodes: dict[str, WaitNode]) -> list[list[tuple[str, str]]]:
    """Every distinct cycle of *nodes*, one per set of items, in a stable order. Pure."""
    found: dict[frozenset[str], list[tuple[str, str]]] = {}
    for item_id in sorted(nodes):
        if nodes[item_id].status != "blocked":
            continue
        cycle = cycle_through(item_id, nodes)
        if cycle is None:
            continue
        members = frozenset(i for _, i in cycle)
        found.setdefault(members, cycle)
    return [found[k] for k in sorted(found, key=lambda s: sorted(s))]


def cycle_id(cycle: Sequence[tuple[str, str]]) -> str:
    """Stable id of a cycle, whatever item it is read from."""
    members = ",".join(sorted(i for _, i in cycle))
    return hashlib.sha256(members.encode()).hexdigest()[:16]


def describe_cycle(start: str, cycle: Sequence[tuple[str, str]], labels: dict[str, str] | None = None) -> str:
    """``A waits for owner/name#12, which waits for B, which waits for A``. Pure."""
    labels = labels or {}

    def name(item_id: str) -> str:
        return labels.get(item_id) or item_id

    parts = []
    for index, (via, nxt) in enumerate(cycle):
        verb = "waits for" if index == 0 else "which waits for"
        if via:
            parts.append(f"{verb} {via}, which waits for the item gating it, {name(nxt)}")
        else:
            parts.append(f"{verb} {name(nxt)}")
    return f"{name(start)} " + ", ".join(parts)


def refusal_message(start: str, cycle: Sequence[tuple[str, str]], labels: dict[str, str] | None = None) -> str:
    return (
        f"This wait closes a cycle: {describe_cycle(start, cycle, labels)}. Nothing would ever release them. "
        + CYCLE_ADVICE
    )


def stalled_chains(nodes: dict[str, WaitNode], now: datetime, *, in_cycles: Iterable[str] = ()) -> list[list[str]]:
    """The chains of 2+ blocked items waiting on each other, all quiet for :data:`STALLED_AFTER`, none held. Pure.

    A chain is a connected group of the wait graph between blocked items; items
    already in a cycle are left out (the cycle is its own finding). A chain whose
    every end waits on a person's open decision (:func:`waits_on_person`) is not
    stalled, however quiet (#2326): :func:`person_wait_chains` lists it apart. A
    decision past its due date, answered without releasing its chain, or on a card
    nobody can read still stalls the chain.
    """
    return [
        group
        for group in _chains(nodes, in_cycles)
        if not _ends_on_person(group, nodes, now) and all(_quiet(nodes[i], now) for i in group)
    ]


def person_wait_chains(nodes: dict[str, WaitNode], now: datetime, *, in_cycles: Iterable[str] = ()) -> list[list[str]]:
    """The chains of 2+ blocked items whose every end waits on a person's open decision (#2326). Pure."""
    return [group for group in _chains(nodes, in_cycles) if _ends_on_person(group, nodes, now)]


def waits_on_person(node: WaitNode, now: datetime) -> bool:
    """An open decision of a person: needs_human, a card they can read, not answered, not past due. Pure."""
    if node.status != "blocked" or not node.needs_human or not node.card_ok or node.answered:
        return False
    due = node.due_at
    return due is None or (due if due.tzinfo else due.replace(tzinfo=UTC)) > now


def chain_ends(group: Iterable[str], nodes: dict[str, WaitNode]) -> list[str]:
    """The items of *group* that wait on no other item of it: where the chain is held. Pure."""
    members = set(group)
    return sorted(i for i in members if not any(nxt in members for _, nxt in successors(nodes[i], nodes)))


def _ends_on_person(group: Sequence[str], nodes: dict[str, WaitNode], now: datetime) -> bool:
    ends = chain_ends(group, nodes)
    return bool(ends) and all(waits_on_person(nodes[i], now) for i in ends)


def _chains(nodes: dict[str, WaitNode], in_cycles: Iterable[str]) -> list[list[str]]:
    """The connected groups of 2+ blocked items of the wait graph, cycles left out. Pure."""
    excluded = set(in_cycles)
    blocked = {i: n for i, n in nodes.items() if n.status == "blocked" and i not in excluded}
    links: dict[str, set[str]] = {i: set() for i in blocked}
    for item_id, node in blocked.items():
        for _, nxt in successors(node, nodes):
            if nxt in blocked:
                links[item_id].add(nxt)
                links[nxt].add(item_id)
    out: list[list[str]] = []
    seen: set[str] = set()
    for item_id in sorted(blocked):
        if item_id in seen or not links[item_id]:
            continue
        group, todo = set(), [item_id]
        while todo:
            current = todo.pop()
            if current in group:
                continue
            group.add(current)
            todo.extend(links[current] - group)
        seen |= group
        if len(group) >= 2:
            out.append(sorted(group))
    return out


def _quiet(node: WaitNode, now: datetime) -> bool:
    """Quiet for :data:`STALLED_AFTER` and held by no live run. Pure."""
    stamp = node.last_activity_at
    if stamp is None or node.held:
        return False
    return (stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)) <= now - STALLED_AFTER


# -- The loaders --------------------------------------------------------------------


def _graph_clause() -> tuple[Any, ...]:
    """SQL: the open items that can be a node with an edge (blocked, or gating a pull request)."""
    key = cast(WorkItem.correlation_key, String)
    return (
        WorkItem.status.in_(WORK_ITEM_OPEN_STATUSES),
        WorkItem.action_mode.is_(None),
        or_(
            WorkItem.status == "blocked",
            key.like("ux:%"),
            key.like("dev:ux-verdict-%"),
            cast(WorkItem.payload, Text).like('%"gates_pull_requests"%'),
        ),
    )


def _recipient(item: Any) -> str | None:
    """Who a ``needs_human`` item waits for, as :mod:`app.services.work_item_recipient` reads it."""
    if getattr(item, "needs_human", None) is not True:
        return None
    groups = getattr(item, "waits_for_group_ids", None)
    if isinstance(groups, list) and groups:
        return "group:" + ",".join(str(g) for g in groups)
    person = getattr(item, "waits_for_user_id", None) or getattr(item, "user_id", None)
    return str(person) if person else "administrators"


def _row_node(item: Any, *, held: bool = False) -> WaitNode:
    return node_of(
        item.id,
        status=item.status,
        blocked_on=item.blocked_on,
        blocked_on_refs=item.blocked_on_refs,
        correlation_key=item.correlation_key,
        title=item.title,
        payload=item.payload,
        last_activity_at=item.last_activity_at,
        held=held,
        needs_human=getattr(item, "needs_human", None),
        ask=getattr(item, "ask", None),
        due_at=getattr(item, "due_at", None),
        created_at=getattr(item, "created_at", None),
        recipient=_recipient(item),
    )


async def load_nodes(db: Any, *, scope: Any = None) -> dict[str, WaitNode]:
    """The wait graph's nodes under *scope* (an extra SQL clause), at most :data:`MAX_GRAPH_ITEMS`."""
    from app.services.work_item import _holder_alive_clause

    clauses = [*_graph_clause()]
    if scope is not None:
        clauses.append(scope)
    held = _holder_alive_clause(WorkItem.claimed_by_run_id)  # type: ignore[no-untyped-call]
    rows = (
        await db.execute(
            select(WorkItem, held.label("held"))
            .where(*clauses)
            .order_by(WorkItem.last_activity_at.desc())
            .limit(MAX_GRAPH_ITEMS)
        )
    ).all()
    return {str(item.id): _row_node(item, held=bool(alive)) for item, alive in rows}


def _owner_scope(user_id: Any, workspace_id: Any) -> Any:
    """SQL: the items a cycle with this item can run through (same person or same workspace)."""
    clauses = []
    if user_id is not None:
        clauses.append(WorkItem.user_id == user_id)
    if workspace_id is not None:
        clauses.append(WorkItem.workspace_id == workspace_id)
    return or_(*clauses) if clauses else None


async def refuse_wait_cycle(
    db: Any,
    *,
    item_id: object,
    status: str | None,
    blocked_on: object,
    blocked_on_refs: object,
    correlation_key: object,
    title: object,
    payload: object,
    user_id: Any,
    workspace_id: Any,
) -> None:
    """Raise ``ValueError`` when the item, as it would land, closes a wait cycle.

    *item_id* is ``None`` for an item not created yet: a new item can still close a
    cycle through the pull request it gates (a review created blocked on the fix
    that waits on its pull request). *status* is the status the item lands in.
    """
    if status != "blocked" or not (blocked_on or blocked_on_refs):
        # An item that waits on nothing has no edge out: no cycle runs through it.
        return
    scope = _owner_scope(user_id, workspace_id)
    if scope is None:
        return
    nodes = await load_nodes(db, scope=scope)
    own = str(item_id) if item_id is not None else f"new-{uuid.uuid4()}"
    nodes[own] = node_of(
        own,
        status=status,
        blocked_on=blocked_on,
        blocked_on_refs=blocked_on_refs,
        correlation_key=correlation_key,
        title=title,
        payload=payload,
    )
    cycle = cycle_through(own, nodes)
    if cycle is None:
        return
    labels = {own: "this item"} if item_id is None else {own: f"this item ({own})"}
    raise ValueError(refusal_message(own, cycle, labels))


def _youngest(cycle: Sequence[tuple[str, str]], nodes: dict[str, WaitNode]) -> str:
    """The item of *cycle* blocked most recently (its last activity), ties by id."""
    members = sorted({i for _, i in cycle})
    floor = datetime.min.replace(tzinfo=UTC)

    def when(item_id: str) -> datetime:
        stamp = nodes[item_id].last_activity_at
        if stamp is None:
            return floor
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)

    return max(members, key=lambda i: (when(i), i))


def release_note(cycle: Sequence[tuple[str, str]], released: str, now: datetime) -> str:
    return (
        f"released from a wait cycle: {describe_cycle(released, _rotate(cycle, released))}. "
        f"Its waits (blocked_on, blocked_on_refs) were dropped so the cycle can move; {CYCLE_ADVICE} "
        f"Released at {now.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )


def _rotate(cycle: Sequence[tuple[str, str]], start: str) -> list[tuple[str, str]]:
    """*cycle* read from *start*: the steps after it, ending on it."""
    steps = list(cycle)
    for index, (_, item_id) in enumerate(steps):
        if item_id == start:
            return steps[index + 1 :] + steps[: index + 1]
    return steps


async def release_wait_cycles(now: datetime | None = None) -> int:
    """Break every wait cycle among the open items: one release, one note, one event per cycle.

    The item of the cycle blocked most recently goes back to ``open`` (unclaimed,
    its waits dropped, the note on top of its description), and
    ``work_item.wait_cycle`` is raised with the cycle's items and references.
    Never raises.
    """
    from app.core.database import async_session

    now = now or datetime.now(UTC)
    released: list[tuple[str, str, list[tuple[str, str]]]] = []
    try:
        async with async_session() as db:
            nodes = await load_nodes(db)
            cycles = find_cycles(nodes)[:MAX_RELEASES_PER_SWEEP]
            for cycle in cycles:
                target = _youngest(cycle, nodes)
                item = (
                    await db.execute(
                        select(WorkItem).where(WorkItem.id == uuid.UUID(target)).with_for_update(skip_locked=True)
                    )
                ).scalar_one_or_none()
                if item is None or item.status != "blocked":
                    continue
                note = release_note(cycle, target, now)
                previous = item.description or ""
                item.description = f"{note}\n\n{previous}" if previous else note
                item.claim_notes = [*(item.claim_notes or []), {"at": now.isoformat(), "text": note}]
                item.status = "open"
                item.claimed_by_agent_id = None
                item.claimed_by_run_id = None
                item.claimed_at = None
                item.blocked_on = None
                item.blocked_on_refs = None
                item.blocked_reason = None
                item.needs_human = None
                item.last_activity_at = now
                released.append((target, note, cycle))
            await db.commit()
    except Exception as exc:
        logger.warning("Work item wait cycles not released: %s", format_exception_message(exc))
        return 0
    for target, note, cycle in released:
        await _publish_cycle(target, note, cycle, nodes)
    if released:
        logger.info("Released %d work item wait cycle(s)", len(released))
    return len(released)


async def _publish_cycle(target: str, note: str, cycle: Sequence[tuple[str, str]], nodes: dict[str, WaitNode]) -> None:
    """``work_item.status_changed`` of the released item and one ``work_item.wait_cycle``. Best-effort."""
    from app.core.database import async_session
    from app.services.platform_events import publish, work_item_data
    from app.services.work_item import publish_transition

    try:
        await publish_transition(uuid.UUID(target), previous_status="blocked", note=note)
        async with async_session() as db:
            item = await db.get(WorkItem, uuid.UUID(target))
        if item is None:
            return
        data = work_item_data(item, previous_status="blocked", note=note)
        data["cycle"] = {
            "id": cycle_id(cycle),
            "items": sorted({i for _, i in cycle}),
            "references": sorted({via for via, _ in cycle if via}),
            "released": target,
        }
        await publish("work_item.wait_cycle", data, event_id=f"work_item.wait_cycle:{cycle_id(cycle)}:{target}")
    except Exception as exc:  # best-effort: the release already landed
        logger.warning("work_item.wait_cycle for %s not published: %s", target, format_exception_message(exc))
