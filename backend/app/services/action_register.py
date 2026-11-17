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

"""Action Register parsing -> live tracked work items.

The fleet formatting standard emits a "Action Register" markdown table in run
output (owner + status + due). On its own that text dies at end-of-run and
nobody picks it up. This module parses that table and materialises each row that
names a KNOWN owning agent into a durable :class:`WorkItem`
(``source="action_register"``) so genuine, owned follow-ups become trackable
objects the dashboard can stale-flag.

Rows with a blank or unresolvable owner are intentionally NOT materialised. Work
items are an agent-to-agent queue, not a human to-do list, so an owner-less row
(a human approver, an informational / optional note, or the run's own recurring
assignment) would only ever become an orphan that nobody picks up - the exact
"unassigned, stale, nobody owns it" item this materialiser must never create.
Such follow-ups stay in the run text; an agent that wants one tracked creates it
explicitly, with an owner, via ``create_work_item``.

Pure parsing (``parse_action_register``) is side-effect free and unit-tested;
``materialize_action_register`` persists the owned rows (best-effort; owner
resolved by agent name, slug, or id) and is called from the assignment engine
after a run completes.
"""

from __future__ import annotations

import logging
import re
import string
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.company_managed_writes import CompanyManagedWriter

logger = logging.getLogger(__name__)

# Header aliases -> canonical field. Matched case-insensitively after stripping
# markdown emphasis / punctuation from the header cell.
_TITLE_HEADERS = {"action", "task", "item", "follow-up", "followup", "todo", "description", "next step", "next steps"}
_OWNER_HEADERS = {"owner", "assignee", "agent", "responsible", "owner agent"}
_STATUS_HEADERS = {"status", "state"}
_DUE_HEADERS = {"due", "due date", "deadline", "by", "eta"}
_PRIORITY_HEADERS = {"priority", "severity", "sev"}

_STATUS_MAP = {
    "open": "open",
    "todo": "open",
    "to do": "open",
    "not started": "open",
    "new": "open",
    "pending": "open",
    "claimed": "claimed",
    "assigned": "claimed",
    "in progress": "in_progress",
    "in-progress": "in_progress",
    "wip": "in_progress",
    "doing": "in_progress",
    "active": "in_progress",
    "blocked": "blocked",
    "on hold": "blocked",
    "waiting": "blocked",
    "done": "done",
    "complete": "done",
    "completed": "done",
    "closed": "done",
    "resolved": "done",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "wontfix": "cancelled",
}
_PRIORITY_MAP = {
    "low": "low",
    "p3": "low",
    "p4": "low",
    "medium": "medium",
    "med": "medium",
    "normal": "medium",
    "p2": "medium",
    "high": "high",
    "p1": "high",
    "important": "high",
    "urgent": "urgent",
    "critical": "urgent",
    "p0": "urgent",
    "sev1": "urgent",
}

_ACTION_REGISTER_RE = re.compile(r"action\s*register", re.IGNORECASE)


def _clean_cell(cell: str) -> str:
    """Strip markdown emphasis / code / links to plain text for a table cell."""
    text = cell.strip()
    # [label](url) -> label
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    # strip surrounding **, *, `, _
    text = text.strip("*`_ ").strip()
    return text


def _split_row(line: str) -> list[str]:
    """Split a markdown table row into trimmed cells (drop edge separators)."""
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    return [c.strip() for c in inner.split("|")]


def _is_separator_row(line: str) -> bool:
    cells = _split_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c.strip()) for c in cells if c.strip() != "")


def _map_header(cell: str) -> str | None:
    # Strip surrounding punctuation (e.g. "Owner:", "(Due)", "Status.") on top
    # of the markdown emphasis ``_clean_cell`` removes, so a decorated header
    # still maps. Only the ends are stripped, so internal hyphens / spaces are
    # kept and multi-word aliases ("follow-up", "next step") still match.
    key = _clean_cell(cell).lower().strip(string.punctuation + string.whitespace)
    if key in _TITLE_HEADERS:
        return "title"
    if key in _OWNER_HEADERS:
        return "owner"
    if key in _STATUS_HEADERS:
        return "status"
    if key in _DUE_HEADERS:
        return "due"
    if key in _PRIORITY_HEADERS:
        return "priority"
    return None


def parse_action_register(text: str) -> list[dict]:
    """Parse the "Action Register" markdown table(s) from run output text.

    Returns a list of ``{title, owner, status, priority, due}`` dicts (title is
    always present; the others are ``None`` when absent). Returns ``[]`` when no
    Action Register section / table is found.
    """
    if not text or not _ACTION_REGISTER_RE.search(text):
        return []

    lines = text.splitlines()
    # Find the first line that mentions the Action Register, then scan forward
    # for the first markdown table after it.
    start = next(
        (i for i, ln in enumerate(lines) if _ACTION_REGISTER_RE.search(ln)), None
    )
    if start is None:
        return []

    rows: list[dict] = []
    i = start + 1
    n = len(lines)
    while i < n:
        line = lines[i]
        if "|" not in line:
            # Allow a few blank/prose lines between the heading and the table.
            if line.strip() == "" and not rows:
                i += 1
                continue
            if rows:
                break  # table ended
            i += 1
            # Give up if we wander too far past the heading with no table.
            if i - start > 12:
                break
            continue

        header_cells = _split_row(line)
        col_map = {idx: _map_header(c) for idx, c in enumerate(header_cells)}
        if "title" not in col_map.values():
            # Not the register table - keep scanning.
            i += 1
            continue
        # Next non-empty line should be the separator.
        j = i + 1
        if j < n and _is_separator_row(lines[j]):
            j += 1
        # Consume data rows.
        while j < n and "|" in lines[j] and not _is_separator_row(lines[j]):
            data = _split_row(lines[j])
            row: dict[str, str | None] = {
                "title": None,
                "owner": None,
                "status": None,
                "priority": None,
                "due": None,
            }
            for idx, field in col_map.items():
                if field and idx < len(data):
                    row[field] = _clean_cell(data[idx]) or None
            if row["title"]:
                row["status"] = _normalize_status(row["status"])
                row["priority"] = _normalize_priority(row["priority"])
                rows.append(row)
            j += 1
        break

    return rows


def _normalize_status(value: str | None) -> str:
    if not value:
        return "open"
    return _STATUS_MAP.get(value.strip().lower(), "open")


def _normalize_priority(value: str | None) -> str | None:
    if not value:
        return None
    return _PRIORITY_MAP.get(value.strip().lower())


async def materialize_action_register(
    *,
    text: str,
    user_id: str | None,
    is_company_managed: bool,
    correlation_prefix: str,
    created_by_agent_id: str | None = None,
    source_run_id: str | None = None,
    writer: CompanyManagedWriter,
) -> int:
    """Parse *text* and persist each OWNED Action Register row as a WorkItem.

    *writer* is the run's agent: only a company-managed one creates
    company-managed items.

    Owners are resolved by agent name, slug, or id among the acting user's
    visible agents. A row whose owner is blank or does not resolve to a known
    agent is skipped - never minted as an orphan, unassigned item - because work
    items are an agent-to-agent queue, not a human to-do list. Materialised
    items use ``source="action_register"`` (tracking objects, surfaced +
    stale-flagged in the dashboard, picked up via the agentic workflow). A
    deterministic ``correlation_key`` per row makes re-runs idempotent at the
    application level. Returns the number of items created.
    """
    rows = parse_action_register(text)
    if not rows:
        return 0

    from app.core.utils import normalize_correlation_key, slugify
    from app.services.work_item import (
        WorkItemOrigin,
        create_work_item,
        existing_correlation_keys,
    )

    owner_lookup = await _resolve_agent_names(user_id)

    # Build candidate rows (skip blank titles + already-terminal rows) and their
    # deterministic correlation keys first, then fetch all existing keys in a
    # SINGLE query so re-runs stay idempotent without one lookup per row (N+1).
    candidates: list[tuple[dict, str, str, str]] = []
    for idx, row in enumerate(rows):
        title = (row.get("title") or "").strip()
        if not title:
            continue
        status = row.get("status") or "open"
        if status in ("done", "cancelled"):
            # A row already marked done/cancelled is not an open follow-up.
            continue
        # Normalize here so the candidate key, the batch existence query, and the
        # eventual create all use the identical stored value (keeps re-run
        # idempotency intact even when the prefix + slug would exceed 200 chars).
        corr = normalize_correlation_key(
            f"{correlation_prefix}:{slugify(title)[:60] or idx}"
        )
        if not corr:
            continue
        candidates.append((row, title, status, corr))

    already = await existing_correlation_keys(
        user_id=user_id, keys=[corr for _, _, _, corr in candidates]
    )

    # Set of real agent ids (from the name/slug lookup) so an owner cell that is
    # a literal agent UUID still resolves. Used by ``_resolve_owner``.
    valid_ids = set(owner_lookup.values())

    created = 0
    skipped_unowned = 0
    seen: set[str] = set()
    for row, title, status, corr in candidates:
        # Idempotency: skip a key that already exists (prior run) or repeats
        # within this same register batch.
        if corr in already or corr in seen:
            continue
        # Only materialise rows that name a KNOWN owning agent. A blank or
        # unresolvable owner (human approver, informational/optional note, or the
        # run's own recurring assignment) would just become an orphan nobody
        # picks up, so skip it rather than minting an unassigned work item. The
        # ``seen`` guard is added only AFTER this check so a later owned row with
        # the same title still materialises.
        owner_id = _resolve_owner(row.get("owner"), owner_lookup, valid_ids)
        if owner_id is None:
            skipped_unowned += 1
            continue
        seen.add(corr)
        try:
            await create_work_item(
                user_id=user_id,
                title=title,
                # Provenance + content so a surfaced item is never a bare title
                # the human can't explain: a real description plus the
                # originating run id and register owner carried in ``payload``
                # (the UI links back to the source run).
                description=(
                    "Auto-tracked follow-up parsed from the agent's Action "
                    "Register output. See the originating run for full context."
                ),
                owner_agent_id=owner_id,
                created_by_agent_id=created_by_agent_id,
                priority=row.get("priority") or "medium",
                status=status if status in ("open", "claimed", "in_progress", "blocked") else "open",
                correlation_key=corr,
                due_at=row.get("due"),
                payload={
                    "source": "action_register",
                    "source_run_id": source_run_id,
                    "register_owner": row.get("owner"),
                },
                source="action_register",
                is_company_managed=is_company_managed,
                origin=WorkItemOrigin(run_id=source_run_id),
                writer=writer,
            )
            created += 1
        except Exception as exc:  # pragma: no cover - best-effort materialization
            logger.warning("Failed to materialize action-register row %r: %s", title, exc)

    if created or skipped_unowned:
        logger.info(
            "Action Register %s: materialized %d owned work item(s), skipped %d "
            "unowned row(s) (no known owning agent)",
            correlation_prefix,
            created,
            skipped_unowned,
        )
    return created


def _resolve_owner(
    owner_cell: str | None,
    owner_lookup: dict[str, str],
    valid_ids: set[str],
) -> str | None:
    """Resolve an Action Register owner cell to a known agent id, or ``None``.

    Tries the lower-cased display name / slug first, then a literal agent UUID
    (some runs emit the owner's id directly). Returns ``None`` when the cell is
    blank or matches no visible agent - the caller then skips the row instead of
    minting an orphan, unassigned work item.
    """
    from app.core.utils import safe_uuid

    raw = (owner_cell or "").strip()
    if not raw:
        return None
    by_label = owner_lookup.get(raw.lower())
    if by_label is not None:
        return by_label
    maybe_uuid = safe_uuid(raw)
    if maybe_uuid is not None and str(maybe_uuid) in valid_ids:
        return str(maybe_uuid)
    return None


async def _resolve_agent_names(user_id: str | None) -> dict[str, str]:
    """Map lower-cased agent name AND slug -> agent id among the acting user's
    VISIBLE agents (own + group-shared + company-managed), matching the rest of
    the product's visibility so a shared agent owner resolves; an anonymous
    caller resolves only company-managed (fleet) agents.

    Both the display name and the slug are indexed so an Action Register that
    refers to the owner either way still resolves to a real agent (and the row
    is materialised rather than dropped as unowned).
    """
    from sqlalchemy import select

    from app.api.v1.helpers import get_user_group_ids, user_visible_filter
    from app.core.database import async_session
    from app.core.utils import safe_uuid
    from app.models.agent import Agent

    uid = safe_uuid(user_id) if user_id else None
    async with async_session() as db:
        if uid is None:
            clause = Agent.is_company_managed.is_(True)
        else:
            group_ids = await get_user_group_ids(db, uid)
            clause = user_visible_filter(Agent, uid, group_ids)
        rows = (
            await db.execute(select(Agent.id, Agent.name, Agent.slug).where(clause))
        ).all()
    # Index by both name and slug (lower-cased). Last-wins on a duplicate label
    # is acceptable for a soft owner resolution.
    lookup: dict[str, str] = {}
    for aid, name, slug in rows:
        if name and str(name).strip():
            lookup[str(name).strip().lower()] = str(aid)
        if slug and str(slug).strip():
            lookup[str(slug).strip().lower()] = str(aid)
    return lookup
