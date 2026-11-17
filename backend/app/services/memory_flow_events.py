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

"""Durable memory writes on the flow event bus.

Persistent memory is already searchable by agents; what it could not do was
*wake* anything. An agent recorded ``Status: remediation_needed`` and the
assignment that should act on it waited for the next scheduled reconciliation.
This module closes that gap the same way ``runner_flow_events`` closed it for
runner lifecycle: at the single durable-write chokepoint
(:func:`app.services.persistent_memory.persist_memory`), emit a typed
:class:`~app.models.flow_event.FlowEvent` so any flow carrying an ``On event``
trigger bound to it reacts within a reconciler tick.

One builtin, company-owned event type is seeded at startup (create-if-absent
by name, exactly like the runner types):

* ``Memory persisted`` — one durable memory entry was written.

The payload is deliberately a *summary*, not the memory: ids, classification,
the parsed status token and a short preview. A listener that needs the whole
entry reads it back by ``memory_id``.

**Loop safety** has three layers, because a flow that writes memory is the
obvious way to build an infinite trigger cycle:

1. ``exclude_flow_id`` — a flow never re-triggers itself from a memory its own
   run wrote. This is the direct self-trigger case and it is prevented, not
   merely bounded.
2. ``chain_depth`` — a run started by a memory event carries the depth
   forward through :data:`_memory_chain`, so an A → B → A cycle dies at
   ``EVENT_CHAIN_MAX_DEPTH``.
3. ``debounce_seconds`` on the trigger node — a rate limit on the listener,
   for the write-storm case (fact reconciliation persists in a loop).

Layers 1 and 2 ride a ContextVar set by ``FlowWalker.run_burst``, so they
cover writes made *inside* a flow run — including from an agent node's tool
call, which inherits the context. They also survive the one process boundary
this feature routinely crosses: ``action.trigger_assignment`` freezes the
chain onto the run's ``trigger_context`` and the assignment orchestration
adapter rebinds it in the worker, the same way
``_register_completion_emissions`` carries a delegation across the hop. That
matters because the context does not merely weaken across a hop — it resets
``chain_depth`` to 0, so the cap would never be approached and the cycle
would not be bounded at all, only rate-limited by an optional layer 3.

A write from some other process with no chain handed to it still emits at
depth 0. Layer 3 is the bound there, and it is opt-in.

**Keeping the table bounded.** ``flow_events`` is append-only, and memory
writes are by far its largest producer, so this module carries half the
answer: :func:`_delete_superseded` drops an entry's older emissions as it is
rewritten, which collapses the repeat-writer case (a consolidated memory, an
operational block moving through its statuses) to one row per live entry. It
cannot bound the other case — every distinct fact emits a row nothing will
supersede — so the age sweep in
:func:`app.services.flow_events.purge_old_flow_events` is the real ceiling.

Every emitter here is fail-open: recording a memory must never depend on the
flow bus having a good day.
"""

from __future__ import annotations

import logging
import re
import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import delete, select

from app.models.flow_event import FlowEvent

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.agent_memory_entry import AgentMemoryEntry
    from app.models.flow_event import FlowEventType

logger = logging.getLogger(__name__)

MEMORY_EVENT_PERSISTED = "Memory persisted"


class MemoryStatus(StrEnum):
    """The operational statuses a memory block declares in a ``Status:`` line.

    Only these are surfaced as ``status``: an open-ended token would let any
    line starting with "Status:" (including one an agent quoted from a scanned
    document) become a routing key that flows filter on.
    """

    PLANNED = "planned"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    BLOCKED = "blocked"
    FAILED_VERIFICATION = "failed_verification"
    REMEDIATION_NEEDED = "remediation_needed"
    COMPLETED = "completed"
    REMEDIATED = "remediated"
    CANCELLED = "cancelled"


# Field declarations follow the FlowEventType shape ({name, type, required}).
# Everything is a string: event fields are rendered into templates
# (``{{t.fields.status}}``) far more often than they are arithmetic, and ids
# have no numeric meaning.
MEMORY_EVENT_TYPE_SPECS: dict[str, dict] = {
    MEMORY_EVENT_PERSISTED: {
        "name": MEMORY_EVENT_PERSISTED,
        "fields": [
            {"name": "memory_id", "type": "string", "required": True},
            {"name": "agent_id", "type": "string", "required": True},
            {"name": "user_id", "type": "string", "required": False},
            {"name": "memory_type", "type": "string", "required": True},
            {"name": "source", "type": "string", "required": True},
            {"name": "status", "type": "string", "required": False},
            {"name": "content_preview", "type": "string", "required": False},
            {"name": "created_at", "type": "string", "required": False},
        ],
    },
}

# An exact status LINE, not a substring: `Status: planned` on its own line.
# Anchored both ends so prose that merely mentions a status ("the Status:
# planned entries are stale") does not classify the whole memory.
#
# ``\r`` is in both character classes because of CRLF: under ``re.MULTILINE``
# ``$`` matches BEFORE the ``\n``, which leaves the ``\r`` sitting between the
# token and the anchor. Memory routinely arrives from pasted documents and
# Windows-authored sources, and without this such an entry parses to no status
# at all — the listener then simply never fires, which is the worst failure
# mode here because nothing errors and the flow just looks broken.
_STATUS_LINE = re.compile(
    r"^[ \t\r]*status[ \t]*:[ \t]*([a-z][a-z0-9_]*)[ \t\r]*$",
    re.IGNORECASE | re.MULTILINE,
)

# How much of the memory rides along. Enough to route on and to read in the
# run's trigger payload; not so much that the event table becomes a second
# copy of the memory store.
_PREVIEW_CHARS = 280


# ── Chain context ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MemoryChainContext:
    """What the running flow contributes to a memory emission it causes."""

    flow_id: uuid.UUID | None
    chain_depth: int


_memory_chain: ContextVar[MemoryChainContext | None] = ContextVar(
    "memory_event_chain", default=None
)


def set_memory_chain_context(
    flow_id: uuid.UUID | None, chain_depth: int
) -> Token:
    """Bind the running flow to memory writes made under it. Returns the reset token."""
    return _memory_chain.set(
        MemoryChainContext(flow_id=flow_id, chain_depth=max(0, int(chain_depth)))
    )


def reset_memory_chain_context(token: Token) -> None:
    _memory_chain.reset(token)


def current_memory_chain() -> MemoryChainContext | None:
    return _memory_chain.get()


# ── Parsing ─────────────────────────────────────────────────────────────────


def extract_status_token(content: str | None) -> str:
    """The memory's operational status, or ``""`` when it declares none.

    The LAST matching line wins: an operational block that is rewritten in
    place appends its new state, and the newest one is the one a listener
    should route on.
    """
    if not content:
        return ""
    last = ""
    for match in _STATUS_LINE.finditer(content):
        token = match.group(1).lower()
        if token in MemoryStatus:
            last = token
    return last


def content_preview(content: str | None) -> str:
    """A short, single-line view of the memory for the event payload."""
    if not content:
        return ""
    collapsed = " ".join(content.split())
    if len(collapsed) <= _PREVIEW_CHARS:
        return collapsed
    return collapsed[:_PREVIEW_CHARS].rstrip() + "…"


# ── Seeding ─────────────────────────────────────────────────────────────────


async def ensure_memory_event_types(db: AsyncSession) -> None:
    """Create-if-absent the builtin memory event types (company-owned)."""
    from app.models.flow_event import FlowEventType

    for spec in MEMORY_EVENT_TYPE_SPECS.values():
        existing = await db.scalar(
            select(FlowEventType.id).where(
                FlowEventType.name == spec["name"],
                FlowEventType.user_id.is_(None),
            )
        )
        if existing is not None:
            continue
        db.add(
            FlowEventType(
                user_id=None,
                name=spec["name"],
                description=(
                    "Builtin memory event — emitted by the platform after a "
                    "durable memory write."
                ),
                fields=spec["fields"],
                is_company_managed=True,
            )
        )
    await db.flush()


async def _load_type(db: AsyncSession, name: str) -> FlowEventType | None:
    from app.models.flow_event import FlowEventType

    return (
        await db.execute(
            select(FlowEventType).where(
                FlowEventType.name == name, FlowEventType.user_id.is_(None)
            )
        )
    ).scalar_one_or_none()


# ── Emission ────────────────────────────────────────────────────────────────


def memory_event_payload(entry: AgentMemoryEntry) -> dict[str, str]:
    """The raw (pre-coercion) field mapping for one persisted entry."""
    return {
        "memory_id": str(entry.id),
        "agent_id": str(entry.agent_id) if entry.agent_id else "",
        "user_id": str(entry.user_id) if entry.user_id else "",
        "memory_type": entry.memory_type or "",
        "source": entry.source or "",
        "status": extract_status_token(entry.content),
        "content_preview": content_preview(entry.content),
        "created_at": entry.created_at.isoformat() if entry.created_at else "",
    }


async def _attributed_user_id(db: AsyncSession, entry: AgentMemoryEntry):
    """Who this emission runs as: the memory's scope owner, else its agent's.

    A user-scoped memory attributes to that user. An agent-level one
    (``user_id IS NULL`` — the tradecraft and assignment memories) has no
    scope owner, and passing that ``None`` straight through was wrong: global
    dispatch matches ``Flow.user_id == :u OR is_company_managed``, so an
    ownerless emission reached every company-managed flow platform-wide and,
    because ``run_user_id = user_id or flow.user_id`` fell through, started
    the run under that flow's CREATOR — their quotas, their integration
    bindings — for a memory that was never theirs. Company-managed flows do
    not reliably have ``user_id IS NULL``: ``toggle_company_managed_list_field``
    preserves the creator, which is what made the fallback reachable.

    Resolving the agent's owner keeps the run under an identity the write can
    actually be traced to. When even that is absent (a company-managed agent),
    the emission stays ownerless on purpose — the event row is still written
    for the audit trail and for parked waiters, and ``emit_flow_event``'s own
    ownerless guard decides what may start.
    """
    if entry.user_id is not None:
        return entry.user_id
    if entry.agent_id is None:
        return None
    from app.models.agent import Agent

    return await db.scalar(select(Agent.user_id).where(Agent.id == entry.agent_id))


async def emit_memory_persisted(db: AsyncSession, entry: AgentMemoryEntry) -> None:
    """Emit ``Memory persisted`` for a durable write (global scope).

    The caller owns the transaction, mirroring :func:`emit_flow_event`'s
    contract — the event row, and any listener runs it starts, commit with the
    memory entry itself, so a rolled-back write can never leave a live event
    pointing at a memory that does not exist.

    No orchestration wake is sent: the reconciler's own poll
    (``reconciler_interval_seconds``, 2s) picks the listener runs up, and that
    is already three orders of magnitude better than the scheduled
    reconciliation this replaces. Nudging from here would mean publishing a
    wake for rows the reconciler cannot see yet, since the commit is the
    caller's to make.
    """
    event_type = await _load_type(db, MEMORY_EVENT_PERSISTED)
    if event_type is None:
        return

    from app.services.flow_events import (
        EVENT_SCOPE_GLOBAL,
        coerce_event_fields,
        emit_flow_event,
    )

    # The type is admin-editable: coerce against its CURRENT declaration so
    # listeners never receive fields the signature does not promise.
    payload, errors = coerce_event_fields(
        list(event_type.fields or []), memory_event_payload(entry)
    )
    if errors:
        logger.warning(
            "Memory event skipped — payload no longer matches the edited "
            "'%s' declaration: %s",
            event_type.name,
            errors,
        )
        return

    chain = current_memory_chain()
    result = await emit_flow_event(
        db,
        event_type=event_type,
        # See ``_attributed_user_id``: the memory's scope owner, else the
        # owner of the agent that wrote it. Never a bare None for an
        # agent-level memory, which used to run under a company-managed
        # flow's creator.
        user_id=await _attributed_user_id(db, entry),
        flow_id=chain.flow_id if chain else None,
        run_id=None,
        scope=EVENT_SCOPE_GLOBAL,
        payload=payload,
        # A write made inside a flow run inherits that run's depth, so a
        # memory-triggered flow that writes memory again converges on the cap
        # instead of cycling forever.
        chain_depth=(chain.chain_depth + 1) if chain else 0,
        # Layer 1: the writing flow never hears its own write.
        exclude_flow_id=chain.flow_id if chain else None,
    )
    await _delete_superseded(db, event_type.id, result.event)


async def _delete_superseded(
    db: AsyncSession, event_type_id: uuid.UUID, newest: FlowEvent
) -> int:
    """Drop this memory's older emissions now that a newer one exists.

    One memory entry emits again every time it is rewritten — the consolidated
    upsert is the same row over and over, and an operational block moves
    ``planned → running → completed`` in place. Once the newer emission is
    stored, the older ones describe a state that no longer exists: they cannot
    start a run (dispatch already happened, synchronously, at their own emit)
    and nothing reads them back except the audit view and parked waiters.

    Safe for a parked ``util.wait_event`` **because the newest row always
    survives**: a waiter polls for the first emission after it parked, so
    removing a superseded row moves it onto the current state rather than
    stranding it. Deleting the whole family would be the unsafe version, and
    is exactly what the ``id !=`` guard prevents.

    Concurrency is safe in the conservative direction: two transactions
    writing the same memory cannot see each other's uncommitted row, so
    neither deletes the other and the sweep keeps too much, never too little.
    """
    memory_id = (newest.payload or {}).get("memory_id")
    if not memory_id:
        return 0
    result = await db.execute(
        delete(FlowEvent).where(
            FlowEvent.event_type_id == event_type_id,
            FlowEvent.id != newest.id,
            FlowEvent.created_at <= newest.created_at,
            # The indexed expression — see ``ix_flow_events_type_memory_id``.
            FlowEvent.payload["memory_id"].astext == str(memory_id),
        )
    )
    return result.rowcount or 0
