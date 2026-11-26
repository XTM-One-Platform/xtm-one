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

"""The graph of work items and what they wait for (Tasks > Work Items, Graph view).

One call answers the nodes and edges of the items the list's filters match,
read with the list's own visibility (``work_item.list_work_items``), so the
graph never shows an item the list would not:

* **item nodes**: the matching items, plus the visible items they wait for
  that the filter left out (``outside_filter``: a done or cancelled blocker,
  an item of another owner), read in one more query, so a chain never ends on
  an arrow to nothing. A blocker the caller cannot see stays out, and its
  edge with it.
* **ref nodes**: each ``blocked_on_refs`` entry (pull request, issue,
  deployment, workflow run) once, with its stored state; the link is the one
  an item's ``payload.pull_requests`` records, or the workflow run's own.
* **person nodes**: the person a ``needs_human`` item waits for.
* **edges**: ``blocked_on`` and ``ref`` and ``person`` go from the waiter to
  what it waits for; ``program`` from a program root to its child
  (``program:<slug>/...`` keys and ``payload.program_key``).

:func:`build_graph` is pure; :func:`read_graph` is the one read.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from app.services.work_item_programs import PROGRAM_KEY_PREFIX, is_program_root, program_key

#: Items one graph draws: the largest page ``list_work_items`` serves.
MAX_GRAPH_ITEMS = 500
#: Statuses that never release a waiter.
TERMINAL_STATUSES = frozenset({"done", "cancelled"})


def _ref_id(ref: dict[str, Any]) -> str:
    """A stable node id for a reference: the same reference twice is one node."""
    kind = ref.get("kind") or "pull_request"
    repo = str(ref.get("repo") or "").lower()
    if kind == "workflow_run":
        return f"ref:{kind}:{repo}:{ref.get('workflow') or ''}"
    if kind == "a2a_task":
        return f"ref:{kind}:{ref.get('task') or ''}"
    if kind == "tag":
        return f"ref:{kind}:{repo}:{ref.get('pattern') or ''}@{str(ref.get('carries_sha') or '')[:12]}"
    tail = ref.get("number") if ref.get("number") is not None else str(ref.get("sha") or "")[:12]
    return f"ref:{kind}:{repo}#{tail}"


def _ref_label(ref: dict[str, Any]) -> str:
    repo = str(ref.get("repo") or "")
    if ref.get("kind") == "workflow_run":
        return f"{repo}: {ref.get('workflow') or ''}"
    if ref.get("kind") == "a2a_task":
        return f"A2A task {ref.get('task') or ''}"
    if ref.get("kind") == "tag":
        return f"{repo}: tag {ref.get('pattern') or '*'}"
    if ref.get("number") is not None:
        return f"{repo}#{ref['number']}"
    if ref.get("sha"):
        return f"{repo}@{str(ref['sha'])[:7]}"
    return repo


def _ref_satisfied(ref: dict[str, Any]) -> bool:
    """As ``work_item_blocked_refs._satisfied`` reads a reference."""
    state = ref.get("state")
    if ref.get("kind") == "deployment":
        return state == "deployed"
    if ref.get("kind") == "workflow_run":
        return state == "succeeded"
    if ref.get("kind") == "a2a_task":
        return state in {"completed", "ended"}
    if ref.get("kind") == "tag":
        return state == "created"
    return state in {"merged", "closed"}


def _pull_request_urls(items: Iterable[dict[str, Any]]) -> dict[tuple[str, int], str]:
    """The links the items' ``payload.pull_requests`` record, by (repo, number)."""
    urls: dict[tuple[str, int], str] = {}
    for item in items:
        payload = item.get("payload")
        prs = payload.get("pull_requests") if isinstance(payload, dict) else None
        for pr in prs if isinstance(prs, list) else []:
            if not isinstance(pr, dict):
                continue
            repo, number, url = pr.get("repo") or pr.get("repository"), pr.get("number"), pr.get("url")
            if isinstance(repo, str) and isinstance(number, int) and isinstance(url, str) and url:
                urls.setdefault((repo.lower(), number), url)
    return urls


def _program_root_of(item: dict[str, Any]) -> str | None:
    """The program root key *item* is a child of, ``None`` when none (a root is no child)."""
    key = str(item.get("correlation_key") or "")
    if key.startswith(PROGRAM_KEY_PREFIX) and "/" in key:
        root = program_key(key.split("/", 1)[0])
        if root:
            return root
    payload = item.get("payload")
    named = payload.get("program_key") if isinstance(payload, dict) else None
    if isinstance(named, str):
        root = program_key(named)
        if root and root != key:
            return root
    return None


def _ask_view(ask: Any) -> dict[str, Any] | None:
    if not isinstance(ask, dict) or not ask.get("question"):
        return None
    options = [
        {"key": str(o.get("key") or ""), "label": str(o.get("label") or "")}
        for o in ask.get("options") or []
        if isinstance(o, dict)
    ]
    return {"question": str(ask["question"]), "options": options, "recommendation": ask.get("recommendation")}


def _item_node(item: dict[str, Any], *, outside_filter: bool) -> dict[str, Any]:
    context = item.get("context") or {}
    owner = context.get("owner_agent") or None
    return {
        "id": item["id"],
        "kind": "item",
        "title": item.get("title") or "",
        "status": item.get("status"),
        "priority": item.get("priority"),
        "correlation_key": item.get("correlation_key"),
        "owner_agent": (
            {"id": owner.get("id"), "name": owner.get("name"), "icon": owner.get("icon")} if owner else None
        ),
        "needs_human": bool(item.get("needs_human")),
        "ask": _ask_view(item.get("ask")) if item.get("needs_human") else None,
        "is_program_root": is_program_root(item),
        "outside_filter": outside_filter,
        # What the Waiting for me filter reads (work_item `_waits_for_person_clause`),
        # so the view marks the nodes that wait for the reader as the list rows do.
        "user_id": item.get("user_id"),
        "waits_for_user_id": item.get("waits_for_user_id"),
        "owner_agent_id": item.get("owner_agent_id"),
        "due_at": item.get("due_at"),
        "scheduled": bool(item.get("scheduled")),
    }


def missing_blocker_ids(items: Sequence[dict[str, Any]]) -> list[str]:
    """The ids the items wait for that are not among them, each once, in order. Pure."""
    present = {i["id"] for i in items}
    out: list[str] = []
    for item in items:
        for blocker in item.get("blocked_on") or []:
            if isinstance(blocker, str) and blocker not in present and blocker not in out:
                out.append(blocker)
    return out


def build_graph(
    items: Sequence[dict[str, Any]],
    blockers: Sequence[dict[str, Any]] = (),
    *,
    truncated: bool = False,
) -> dict[str, Any]:
    """Nodes and edges of *items* (the filter's) and *blockers* (outside it). Pure.

    Every item dict is ``serialize_work_item``'s, with its ``context`` when it
    has one (the owner agent's name). An edge to an id that is in neither list
    (a blocker the caller cannot see) is left out.
    """
    nodes: list[dict[str, Any]] = [_item_node(i, outside_filter=False) for i in items]
    in_filter = {i["id"] for i in items}
    nodes += [_item_node(b, outside_filter=True) for b in blockers if b["id"] not in in_filter]
    known = {n["id"] for n in nodes}
    every = [*items, *blockers]
    urls = _pull_request_urls(every)
    edges: list[dict[str, Any]] = []
    seen_edges: set[str] = set()
    refs: dict[str, dict[str, Any]] = {}
    people: dict[str, dict[str, Any]] = {}

    def edge(source: str, target: str, kind: str) -> None:
        eid = f"{kind}:{source}->{target}"
        if eid not in seen_edges:
            seen_edges.add(eid)
            edges.append({"id": eid, "source": source, "target": target, "kind": kind})

    roots = {i.get("correlation_key"): i["id"] for i in every if i.get("correlation_key")}
    for item in every:
        iid = item["id"]
        if iid not in in_filter:
            # A blocker outside the filter is drawn, not expanded: its own
            # waits belong to another chain.
            continue
        for blocker in item.get("blocked_on") or []:
            if blocker in known:
                edge(iid, blocker, "blocked_on")
        for ref in item.get("blocked_on_refs") or []:
            if not isinstance(ref, dict):
                continue
            rid = _ref_id(ref)
            if rid not in refs:
                number = ref.get("number")
                url = ref.get("run_url") if ref.get("kind") == "workflow_run" else None
                if url is None and isinstance(number, int):
                    url = urls.get((str(ref.get("repo") or "").lower(), number))
                refs[rid] = {
                    "id": rid,
                    "kind": "ref",
                    "ref_kind": ref.get("kind") or "pull_request",
                    "label": _ref_label(ref),
                    "repo": ref.get("repo"),
                    "number": number,
                    "state": ref.get("state"),
                    "satisfied": _ref_satisfied(ref),
                    "url": url,
                }
            edge(iid, rid, "ref")
        if item.get("needs_human") and item.get("status") not in TERMINAL_STATUSES:
            person = (item.get("context") or {}).get("person") or {}
            pid_raw = item.get("waits_for_user_id") or item.get("user_id")
            if pid_raw:
                pid = f"person:{pid_raw}"
                if pid not in people:
                    named = person.get("id") == str(pid_raw)
                    people[pid] = {
                        "id": pid,
                        "kind": "person",
                        "name": person.get("name") if named else None,
                        "is_you": bool(person.get("is_you")) if named else False,
                    }
                edge(iid, pid, "person")
        root = _program_root_of(item)
        if root and roots.get(root) in known and roots[root] != iid:
            edge(roots[root], iid, "program")

    return {
        "nodes": [*nodes, *refs.values(), *people.values()],
        "edges": edges,
        "truncated": truncated,
    }


async def read_graph(*, user_id: str | None, viewer: Any, filters: dict[str, Any]) -> dict[str, Any]:
    """The graph of the items *filters* match for *user_id*: three reads, no N+1.

    *filters* are ``work_item.list_work_items``' keyword filters (the list's
    route builds them); the graph reads the first :data:`MAX_GRAPH_ITEMS`.
    """
    from app.services import work_item as work_item_service
    from app.services import work_item_context

    items = await work_item_service.list_work_items(user_id=user_id, limit=MAX_GRAPH_ITEMS, offset=0, **filters)
    blockers = await work_item_service.list_visible_work_items_by_ids(missing_blocker_ids(items), user_id=user_id)
    described = await work_item_context.describe_work_items([*items, *blockers], viewer=viewer)
    by_id = {d["id"]: d for d in described}
    return build_graph(
        [by_id.get(i["id"], i) for i in items],
        [by_id.get(b["id"], b) for b in blockers],
        truncated=len(items) >= MAX_GRAPH_ITEMS,
    )
