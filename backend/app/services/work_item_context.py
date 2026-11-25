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

"""What a work item means to the person reading it.

A work item row carries ids: the agent that owns it, the one that created it,
the one holding it, the person it acts for. Read alone they answer none of the
questions a person opening Operations > Work Items asks, so this module is the
one place those questions are answered, for one viewer:

* **who** - the owner, creator and claimer by name, or as an agent the viewer
  cannot open (never a name the viewer may not see), and the person whose
  chat, run or flow created it;
* **why you see it** - your own, company-managed (the whole fleet sees it), or
  filed in a workspace you can see;
* **where it came from** - the chat, assignment run or flow run it was created
  in (``WorkItem.conversation_id`` / ``origin_run_id`` / ``origin_flow_run_id``),
  linked only when the viewer may open it;
* **what will act on it** - the inboxes whose pickup rule takes it
  (``work_item.accepted_by_inbox``, the dispatch's own clauses), or the state
  that keeps it from being picked up, and when the retention sweeps will
  cancel or delete it.

Reads only. The REST routes add the result as ``context``; the agent tools
word their pickup line from :func:`pickup_inboxes`.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import select

from app.core.config import settings
from app.core.utils import format_exception_message, safe_uuid
from app.models.user import User
from app.models.work_item import WORK_ITEM_TERMINAL_STATUSES
from app.services.work_item import WorkItemInbox
from app.services.work_item_blocked_refs import open_refs
from app.services.work_item_fleet_impact import effective_fleet_impact, impacted_agent_ids
from app.services.work_item_pull_requests import pull_requests_waiting_for_ci, waiting_for_ci

logger = logging.getLogger(__name__)

#: Every pickup state a view words (``context.pickup.state``).
PICKUP_STATES: tuple[str, ...] = (
    "scheduled",  # the platform runs it at its time
    "finished",  # done / cancelled
    "in_progress",  # claimed or in progress: an agent holds it
    "blocked",  # nothing takes a blocked item until it is put back in the queue
    "blocked_on_items",  # waits for other items; reopens once they are done
    "waiting_for_refs",  # waits for pull requests, issues, deployments or workflow runs; released by itself
    "waiting_for_person",  # waits for a person's answer to its decision card; the answer reopens it
    "waiting_for_ci",  # open or blocked on a pull request whose CI runs: its end dispatches it
    "unassigned",  # open with no owner agent: nothing routes it
    "automatic",  # open, due, and an inbox takes it on the next tick
    "waiting_until_due",  # an inbox takes it once its due date arrives
    "filtered_out",  # the owner has inboxes, none of them takes this item
    "manual",  # the owner has no inbox: it waits for someone to act
)


def _uuid(value: object) -> uuid.UUID | None:
    if value is None or isinstance(value, uuid.UUID):
        return value
    return safe_uuid(str(value))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _origin_run_id(item: dict) -> uuid.UUID | None:
    """The assignment run an item was created in.

    Items the Action Register materialised in an older release carry it in their
    payload only; that copy is read for them alone.
    """
    run_id = _uuid(item.get("origin_run_id"))
    if run_id is None and item.get("source") == "action_register":
        run_id = _uuid((item.get("payload") or {}).get("source_run_id"))
    return run_id


async def pickup_inboxes(
    db, items: Sequence[dict]
) -> dict[uuid.UUID, list[tuple[WorkItemInbox, bool]]]:
    """For each owned, unfinished, unscheduled item: the inboxes draining its
    owner's queue, each with whether it takes the item.

    Both kinds of inbox (``work_item.load_assignment_inboxes``,
    ``flow_triggers.load_flow_work_item_inboxes``), filtered to the owners
    present, then one ``accepted_by_inbox`` query per inbox.
    """
    from app.services.flow_triggers import load_flow_work_item_inboxes
    from app.services.work_item import accepted_by_inbox, load_assignment_inboxes

    candidates = [
        item
        for item in items
        if item.get("owner_agent_id")
        and not item.get("scheduled")
        and item.get("status") not in WORK_ITEM_TERMINAL_STATUSES
    ]
    if not candidates:
        return {}
    owners = {_uuid(item["owner_agent_id"]) for item in candidates}
    inboxes = [
        inbox
        for inbox in [*await load_assignment_inboxes(db), *await load_flow_work_item_inboxes(db)]
        if _uuid(inbox.owner_agent_id) in owners
    ]
    out: dict[uuid.UUID, list[tuple[WorkItemInbox, bool]]] = {}
    for inbox in inboxes:
        owner = _uuid(inbox.owner_agent_id)
        ids = [
            item_id
            for item in candidates
            if _uuid(item["owner_agent_id"]) == owner and (item_id := _uuid(item["id"]))
        ]
        accepted = await accepted_by_inbox(db, inbox, ids)
        for item_id in ids:
            out.setdefault(item_id, []).append((inbox, item_id in accepted))
    return out


def claim_holder_ended(item: dict) -> bool:
    """Whether *item*'s claim is held by a run read as no longer alive.

    Such a claim is released on the scheduler's next tick: no view says an
    agent is working on it or names who holds it.
    """
    holder = item.get("claimed_by_run")
    return isinstance(holder, dict) and holder.get("alive") is False


def pickup_state(
    item: dict, inboxes: Sequence[tuple[WorkItemInbox, bool]], now: datetime
) -> str:
    """Which of :data:`PICKUP_STATES` *item* is in, given its owner's inboxes."""
    status = item.get("status")
    if item.get("scheduled"):
        return "scheduled"
    if status in WORK_ITEM_TERMINAL_STATUSES:
        return "finished"
    if status in ("claimed", "in_progress") and not claim_holder_ended(item):
        return "in_progress"
    if status == "blocked" and item.get("needs_human"):
        # A person's answer reopens it: no blocker released before it does (#861).
        return "waiting_for_person"
    if status == "blocked" and item.get("blocked_on"):
        # Its blockers decide: the end of a CI never releases it.
        return "blocked_on_items"
    if status in ("open", "blocked") and item.get("owner_agent_id") and waiting_for_ci(item) is not None:
        # The end of the CI dispatches it to its owner: not "not picked up".
        return "waiting_for_ci"
    if status == "blocked" and item.get("owner_agent_id") and open_refs(item.get("blocked_on_refs")):
        # The platform releases it once each reference is merged, closed,
        # deployed or succeeded, and hands it to its owner: not "not picked up" (#861).
        return "waiting_for_refs"
    if status == "blocked":
        return "blocked"
    # Open, or held by a run that ended (released on the next tick): what
    # picks it up next.
    if not item.get("owner_agent_id"):
        return "unassigned"
    if any(accepts for _inbox, accepts in inboxes):
        # ``not_before`` defers the pickup; ``due_at`` is the deadline only (#8753).
        start = _parse(item.get("not_before"))
        return "waiting_until_due" if start and start > now else "automatic"
    return "filtered_out" if inboxes else "manual"


async def new_item_pickup_state(item: dict) -> str | None:
    """The pickup state of an item an agent just created, for the tool's result.

    ``None`` when it cannot be read: the item exists either way, and the tool
    then says what would pick it up rather than what will.
    """
    from app.core.database import async_session

    try:
        async with async_session() as db:
            entries = (await pickup_inboxes(db, [item])).get(_uuid(item.get("id")), [])
    except Exception as exc:
        logger.warning(
            "Could not read the pickup of work item %s: %s",
            item.get("id"),
            format_exception_message(exc),
        )
        return None
    return pickup_state(item, entries, datetime.now(UTC))


def _retention(item: dict, state: str, windows: dict[str, int] | None = None) -> tuple[str | None, str | None]:
    """``(auto_cancel_at, purge_at)``: when ``work_item.reap_work_items`` will
    cancel an open item nobody picks up, or delete a finished one.

    *windows* are the effective Settings retentions
    (services/coordination_retention.py); ``None`` reads the environment."""
    if state in ("unassigned", "automatic", "waiting_until_due", "filtered_out", "manual"):
        days = (
            windows["work_item_abandoned_days"] if windows else getattr(settings, "work_item_abandoned_after_days", 14)
        )
        anchor = _parse(item.get("last_activity_at")) or _parse(item.get("created_at"))
        if not days or days <= 0 or anchor is None:
            return None, None
        at = anchor + timedelta(days=days)
        later = [d for d in (_parse(item.get("due_at")), _parse(item.get("not_before"))) if d]
        return _iso(max([at, *later])), None
    if state == "finished":
        days = (
            windows["work_item_resolved_days"]
            if windows
            else getattr(settings, "work_item_resolved_retention_days", 90)
        )
        anchor = _parse(item.get("updated_at"))
        if not days or days <= 0 or anchor is None:
            return None, None
        return None, _iso(anchor + timedelta(days=days))
    return None, None


class _Lookup:
    """Everything the items of one page reference, read once, as *viewer* may see it."""

    def __init__(self, viewer: User) -> None:
        self.viewer = viewer
        self.agents: dict[uuid.UUID, tuple[str, str | None]] = {}
        self.visible_agents: set[uuid.UUID] = set()
        self.people: dict[uuid.UUID, str | None] = {}
        self.workspaces: dict[uuid.UUID, str] = {}
        self.conversations: dict[uuid.UUID, tuple[str | None, uuid.UUID | None]] = {}
        self.runs: dict[uuid.UUID, tuple[str | None, str | None]] = {}
        self.visible_runs: set[uuid.UUID] = set()
        self.flow_runs: dict[uuid.UUID, tuple[str | None, str | None, uuid.UUID | None]] = {}
        self.visible_flow_runs: set[uuid.UUID] = set()
        self.visible_assignments: set[uuid.UUID] = set()
        self.visible_flows: set[uuid.UUID] = set()
        self.pickup: dict[uuid.UUID, list[tuple[WorkItemInbox, bool]]] = {}
        # The items others wait for, as the viewer may see them.
        self.blockers: dict[str, tuple[str, str]] = {}
        # The effective retention windows of Settings (#2070), read once per page.
        self.retention: dict[str, int] | None = None

    async def load(self, db, items: Sequence[dict], *, extra_agent_ids: Iterable = ()) -> _Lookup:
        from app.api.v1.helpers import (
            assignment_visible_filter,
            company_wide_visible_filter,
            get_user_group_ids,
            run_visible_filter,
            user_visible_filter,
        )
        from app.models.agent import Agent
        from app.models.assignment import Assignment, AssignmentRun
        from app.models.chat import Conversation
        from app.models.flow import Flow
        from app.models.flow_run import FlowRun
        from app.models.workspace import Workspace
        from app.services.flow_access import get_visible_run

        viewer = self.viewer
        admin = bool(viewer.is_admin)
        group_ids = await get_user_group_ids(db, viewer.id)

        def ids(*keys: str) -> set[uuid.UUID]:
            found = {_uuid(item.get(key)) for item in items for key in keys}
            found.discard(None)
            return found  # type: ignore[return-value]

        agent_ids = ids("owner_agent_id", "created_by_agent_id", "claimed_by_agent_id")
        # The agents an item's completion is routed to, named like the others.
        for item in items:
            agent_ids.update(a for a in (_uuid(t) for t in impacted_agent_ids(item)) if a)
        agent_ids.update(a for a in (_uuid(x) for x in extra_agent_ids) if a)
        if agent_ids:
            rows = await db.execute(
                select(Agent.id, Agent.name, Agent.icon).where(Agent.id.in_(agent_ids))
            )
            self.agents = {row[0]: (row[1], row[2]) for row in rows.all()}
            if admin:
                self.visible_agents = set(self.agents)
            else:
                visible = await db.execute(
                    select(Agent.id).where(
                        Agent.id.in_(agent_ids),
                        user_visible_filter(Agent, viewer.id, group_ids),
                    )
                )
                self.visible_agents = set(visible.scalars().all())

        people = ids("user_id")
        if people:
            rows = await db.execute(select(User.id, User.full_name).where(User.id.in_(people)))
            self.people = {row[0]: row[1] for row in rows.all()}

        workspaces = ids("workspace_id")
        if workspaces:
            rows = await db.execute(
                select(Workspace.id, Workspace.name).where(Workspace.id.in_(workspaces))
            )
            self.workspaces = {row[0]: row[1] for row in rows.all()}

        conversations = ids("conversation_id")
        if conversations:
            rows = await db.execute(
                select(Conversation.id, Conversation.title, Conversation.user_id).where(
                    Conversation.id.in_(conversations)
                )
            )
            self.conversations = {row[0]: (row[1], row[2]) for row in rows.all()}

        runs = {run for run in (_origin_run_id(item) for item in items) if run}
        if runs:
            rows = await db.execute(
                select(AssignmentRun.id, Assignment.name, AssignmentRun.status)
                .outerjoin(Assignment, Assignment.id == AssignmentRun.assignment_id)
                .where(AssignmentRun.id.in_(runs))
            )
            self.runs = {row[0]: (row[1], row[2]) for row in rows.all()}
            if admin:
                self.visible_runs = set(self.runs)
            else:
                visible = await db.execute(
                    select(AssignmentRun.id).where(
                        AssignmentRun.id.in_(runs), run_visible_filter(viewer.id)
                    )
                )
                self.visible_runs = set(visible.scalars().all())

        flow_runs = ids("origin_flow_run_id")
        if flow_runs:
            rows = await db.execute(
                select(FlowRun.id, Flow.name, FlowRun.status, FlowRun.flow_id)
                .outerjoin(Flow, Flow.id == FlowRun.flow_id)
                .where(FlowRun.id.in_(flow_runs))
            )
            self.flow_runs = {row[0]: (row[1], row[2], row[3]) for row in rows.all()}
            for run_id in self.flow_runs:
                try:
                    await get_visible_run(db, viewer, str(run_id))
                except HTTPException:
                    continue
                self.visible_flow_runs.add(run_id)

        await self._load_blockers(db, items)
        self.pickup = await pickup_inboxes(db, items)
        inboxes = {inbox for entries in self.pickup.values() for inbox, _ in entries}
        assignment_ids = {i.id for i in inboxes if i.kind == "assignment"}
        flow_ids = {i.id for i in inboxes if i.kind == "flow"}
        if admin:
            self.visible_assignments, self.visible_flows = assignment_ids, flow_ids
        else:
            if assignment_ids:
                visible = await db.execute(
                    select(Assignment.id).where(
                        Assignment.id.in_(assignment_ids),
                        assignment_visible_filter(viewer.id, group_ids),
                    )
                )
                self.visible_assignments = set(visible.scalars().all())
            if flow_ids:
                visible_flow = (
                    user_visible_filter(Flow, viewer.id, group_ids)
                    if viewer.id
                    else company_wide_visible_filter(Flow)
                )
                visible = await db.execute(
                    select(Flow.id).where(Flow.id.in_(flow_ids), visible_flow)
                )
                self.visible_flows = set(visible.scalars().all())
        return self

    async def _load_blockers(self, db, items: Sequence[dict]) -> None:
        """Title and status of every item a page's items wait for, when visible."""
        from app.models.work_item import WorkItem
        from app.services.work_item import _visible_clause, _workspace_tiers

        wanted = {_uuid(b) for item in items for b in item.get("blocked_on") or []}
        wanted.discard(None)
        if not wanted:
            return
        readable, _ = await _workspace_tiers(db, str(self.viewer.id) if self.viewer.id else None)
        rows = await db.execute(
            select(WorkItem.id, WorkItem.title, WorkItem.status).where(
                WorkItem.id.in_(wanted), _visible_clause(self.viewer.id, readable)
            )
        )
        self.blockers = {str(row[0]): (row[1], row[2]) for row in rows.all()}

    def blocked_on(self, item: dict) -> list[dict]:
        """The items *item* waits for: linked by title, or unnamed when not visible."""
        out = []
        for blocker in item.get("blocked_on") or []:
            found = self.blockers.get(str(blocker))
            out.append(
                {
                    "id": str(blocker) if found else None,
                    "title": found[0] if found else None,
                    "status": found[1] if found else None,
                    "accessible": found is not None,
                }
            )
        return out

    def agent(self, agent_id: object, *, named: bool = False) -> dict[str, object] | None:
        aid = _uuid(agent_id)
        if aid is None:
            return None
        if aid not in self.visible_agents:
            # Named only to a viewer who may open it; one that was deleted
            # since has no row at all (its id is set NULL with it). *named*:
            # the OWNER agent of an item the viewer sees keeps its name,
            # read-only, never its id or a link (#858): who holds the work is
            # part of the item, not of the agent.
            if named and aid in self.agents:
                name, icon = self.agents[aid]
                return {"id": None, "name": name, "icon": icon, "accessible": False}
            return {"id": None, "name": None, "icon": None, "accessible": False}
        name, icon = self.agents[aid]
        return {"id": str(aid), "name": name, "icon": icon, "accessible": True}

    def context(self, item: dict, now: datetime) -> dict:
        viewer_id = self.viewer.id
        person_id = _uuid(item.get("user_id"))
        workspace_id = _uuid(item.get("workspace_id"))
        # The tiers of ``work_item._visible_clause``: a filed item is read
        # through its workspace, an unfiled one is company-managed or the
        # viewer's own.
        if workspace_id is not None:
            visibility = {
                "reason": "workspace",
                "workspace": {"id": str(workspace_id), "name": self.workspaces.get(workspace_id)},
            }
        elif item.get("is_company_managed"):
            visibility = {"reason": "company_managed", "workspace": None}
        else:
            visibility = {"reason": "own", "workspace": None}
        return {
            "owner_agent": self.agent(item.get("owner_agent_id"), named=True),
            "created_by_agent": self.agent(item.get("created_by_agent_id")),
            "claimed_by_agent": (
                None
                if claim_holder_ended(item)
                else self.agent(item.get("claimed_by_agent_id"))
            ),
            "person": (
                {
                    "id": str(person_id),
                    "name": self.people.get(person_id),
                    "is_you": person_id == viewer_id,
                }
                if person_id is not None
                else None
            ),
            "visibility": visibility,
            "origin": self._origin(item),
            "pickup": self._pickup(item, now),
            "blocked_on": self.blocked_on(item),
            "fleet_impact": self._fleet_impact(item),
        }

    def _fleet_impact(self, item: dict) -> dict | None:
        """Who the item's completion is routed to, each agent as the viewer may see it."""
        impact = effective_fleet_impact(item)
        if not impact:
            return None
        return {
            "agents": [self.agent(t) for t in impact["targets"]],
            "kind": impact.get("kind"),
            "instructions": impact.get("instructions"),
            "inferred": bool(impact.get("inferred")),
        }

    def _origin(self, item: dict) -> dict:
        conversation = None
        conversation_hidden = False
        cid = _uuid(item.get("conversation_id"))
        if cid is not None and cid in self.conversations:
            title, owner = self.conversations[cid]
            if owner is not None and owner == self.viewer.id:
                conversation = {"id": str(cid), "title": title}
            else:
                conversation_hidden = True
        run = None
        rid = _origin_run_id(item)
        if rid is not None and rid in self.runs:
            name, status = self.runs[rid]
            accessible = rid in self.visible_runs
            run = {
                "id": str(rid) if accessible else None,
                "name": name if accessible else None,
                "status": status if accessible else None,
                "accessible": accessible,
            }
        flow_run = None
        fid = _uuid(item.get("origin_flow_run_id"))
        if fid is not None and fid in self.flow_runs:
            name, status, flow_id = self.flow_runs[fid]
            accessible = fid in self.visible_flow_runs
            flow_run = {
                "id": str(fid) if accessible else None,
                "flow_id": str(flow_id) if accessible and flow_id else None,
                "name": name if accessible else None,
                "status": status if accessible else None,
                "accessible": accessible,
            }
        if flow_run is not None or item.get("source") == "flow":
            kind = "flow"
        elif run is not None:
            kind = "run"
        elif conversation is not None or conversation_hidden:
            kind = "chat"
        elif item.get("source") == "manual":
            kind = "manual"
        else:
            kind = "unknown"
        return {
            "kind": kind,
            "source": item.get("source"),
            "conversation": conversation,
            "conversation_hidden": conversation_hidden,
            "run": run,
            "flow_run": flow_run,
        }

    def _pickup(self, item: dict, now: datetime) -> dict:
        entries = self.pickup.get(_uuid(item.get("id")), [])
        state = pickup_state(item, entries, now)
        auto_cancel_at, purge_at = _retention(item, state, self.retention)
        due = _parse(item.get("not_before"))
        return {
            "state": state,
            "inboxes": [
                {
                    "kind": inbox.kind,
                    "id": str(inbox.id) if self._inbox_visible(inbox) else None,
                    "name": inbox.name if self._inbox_visible(inbox) else None,
                    "accessible": self._inbox_visible(inbox),
                    "accepts": accepts,
                    "priority_min": inbox.priority_min,
                    "correlation_prefix": inbox.correlation_prefix,
                }
                for inbox, accepts in entries
            ],
            "available_at": _iso(due) if state == "waiting_until_due" else None,
            "pull_request": waiting_for_ci(item) if state == "waiting_for_ci" else None,
            # Every pull request whose CI it waits for: the badge names them all.
            "pull_requests": pull_requests_waiting_for_ci(item) if state == "waiting_for_ci" else [],
            "auto_cancel_at": auto_cancel_at,
            "purge_at": purge_at,
            "dispatch_attempts": int((item.get("payload") or {}).get("_dispatch_attempts") or 0),
            "max_dispatch_attempts": max(
                int(getattr(settings, "work_item_max_dispatch_attempts", 5) or 0), 1
            ),
        }

    def _inbox_visible(self, inbox: WorkItemInbox) -> bool:
        pool = self.visible_assignments if inbox.kind == "assignment" else self.visible_flows
        return inbox.id in pool


async def _retention_windows(db) -> dict[str, int] | None:
    """The effective Settings retentions on *db*; ``None`` (the environment) when unreadable."""
    from app.api.v1.platform_settings_helpers import peek_platform_settings
    from app.services.coordination_retention import coordination_retention_days

    try:
        ps = await peek_platform_settings(db)
    except Exception as exc:
        logger.warning("Could not read the coordination retention: %s", format_exception_message(exc))
        return None
    return coordination_retention_days(getattr(ps, "coordination_retention", None) if ps else None)


async def describe_work_items(items: Sequence[dict], *, viewer: User) -> list[dict]:
    """*items* (serialized) with the ``context`` block *viewer* reads."""
    from app.core.database import async_session

    if not items:
        return []
    async with async_session() as db:
        lookup = await _Lookup(viewer).load(db, items)
        lookup.retention = await _retention_windows(db)
    now = datetime.now(UTC)
    return [{**item, "context": lookup.context(item, now)} for item in items]


async def describe_owners(
    counts: Sequence[tuple[str | None, int]], *, viewer: User
) -> list[dict]:
    """``work_item.count_work_items_by_owner`` rows, each owner as *viewer* may see it.

    The owners *viewer* cannot open are counted together, unnamed: an agent
    filter can only offer what it can name.
    """
    from app.core.database import async_session

    async with async_session() as db:
        lookup = await _Lookup(viewer).load(
            db, [], extra_agent_ids=[owner for owner, _ in counts if owner]
        )
    out: list[dict] = []
    hidden = 0
    for owner, count in counts:
        if owner is None:
            out.append({"agent": None, "count": count})
            continue
        ref = lookup.agent(owner)
        if ref is None or not ref["accessible"]:
            hidden += count
            continue
        out.append({"agent": ref, "count": count})
    if hidden:
        out.append(
            {"agent": {"id": None, "name": None, "icon": None, "accessible": False}, "count": hidden}
        )
    return out
