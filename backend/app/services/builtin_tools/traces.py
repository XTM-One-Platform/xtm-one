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

"""Built-in trace inspection tools for agent self-troubleshooting and supervision.

Three tools are exposed:
- ``inspect_my_traces``: read-only access to the current agent's traces.
- ``inspect_agent_traces``: read-only cross-agent access, gated by admin
  privileges or an explicit per-agent allow-list.
- ``get_trace_payload``: the companion fetch that turns any bounded preview
  from the two above into the complete stored payload, one page at a time.

The listing tools return *previews*, deliberately.  A trace payload is audit
material kept whole in the durable store (see
``services/tracing/payload_offload.py``), so a single span can carry megabytes
and one trace can carry dozens of spans — serializing all of it into a tool
result would blow the model's context to answer "which step failed?".  So the
listing orients: it names, times and excerpts every step, and every field it
had to hold back is listed in that step's ``payload_truncated`` so the agent
knows exactly what to fetch.  Nothing is unreachable; a diagnosis that needs
the full text of a step can always read it to the last character.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from sqlalchemy import select

from app.api.v1.helpers import agent_tool_admin_bypass, get_user_group_ids
from app.core.database import async_session
from app.core.utils import format_exception_message
from app.models.user import User
from app.services.leaked_reasoning import serialized_without_reasoning, without_reasoning
from app.services.trace_switches import trace_switches
from app.services.tracing.cost_calculator import cache_write_tokens, cached_input_tokens
from app.services.tracing.payload_offload import (
    is_offload_envelope,
    resolve_payload,
)
from app.services.tracing.run_rollup import (
    MAX_TOOLS,
    merge_segment_rollups,
    rollup_from_spans,
    rollup_percentiles,
    stored_rollup,
)
from app.services.tracing.store import build_trace_read_store
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._shared import logger
from .id_prefix import (
    MAX_CANDIDATES,
    id_prefix,
    id_prefix_clause,
    ids_by_prefix,
    resolved_or_refused,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


#: Name of the companion fetch tool.  Not a user-selectable builtin id — it
#: ships with whichever inspection tool is enabled, so the call sites that gate
#: the trace surface by tool name must let it through (``agent_chat`` and
#: ``assignment_engine``).  Exported so those filters name it from here rather
#: than repeating the literal.
TRACE_PAYLOAD_TOOL_NAME = "get_trace_payload"

#: The trace surfaces a turn wires, one per inspection function. They
#: are the functions of the ``observability`` group (``builtin_tool_aliases``):
#: ``runtime_builtin_tool_ids`` expands the group into the ones its scoping keeps,
#: and the former ids of the same name are its [DEPRECATED] aliases.
TRACE_SUITE_IDS: frozenset[str] = frozenset({"inspect_my_traces", "inspect_agent_traces"})

#: What Platform Health is told for another agent's traces: these tools apply no
#: consent rule, ``health_diagnosis`` does.
HEALTH_TRACES_REFUSED = "Error: Platform Health reads other agents' traces through its diagnosis tools only."


def _health_refuses_other_agents() -> bool:
    """Whether the work acts as Platform Health, or a sandbox copy's turn, which never
    reads another agent's traces here: neither the admin bypass nor a supervision
    allow-list (a copy keeps its source's) checks consent."""
    from app.services.health.identity import in_health_reach

    return in_health_reach()


async def may_read_agent_traces(
    db: AsyncSession,
    user: Any,
    agent: Any,
    user_group_ids: list[str] | None = None,
) -> bool:
    """Whether *user* may read *agent*'s traces: the rule for its chats.

    A trace holds the conversation it records, inputs and outputs, so an
    agent's traces follow ``helpers.can_view_agent_chats`` like the trace REST
    API: an admin, the owner, or a ``config_with_chats`` grantee, directly or
    through a group. Never an anonymous caller, and never the work of an A2A
    partner's task, whatever identity it runs as (``a2a.partner_scope``).
    *user_group_ids* are the user's groups when the caller already read them.
    """
    from app.a2a.partner_scope import in_partner_task
    from app.api.v1 import helpers

    if user is None or in_partner_task():
        return False
    if user_group_ids is None:
        user_group_ids = await helpers.get_user_group_ids(db, user.id)
    return helpers.can_view_agent_chats(user, agent, user_group_ids)


async def agents_whose_traces_user_may_read(
    db: AsyncSession,
    user: Any,
    agent_ids: Iterable[Any],
    user_group_ids: list[str] | None = None,
) -> set[str]:
    """The canonical ids among *agent_ids* whose traces *user* may read
    (:func:`may_read_agent_traces`). An id naming no agent is left out.

    A trace supervision allow-list names the agents another agent may inspect;
    what a caller reads through it is never wider than what that caller may
    read of each target on its own, so it is checked when the list is written
    and again when it is read.
    """
    from app.models.agent import Agent

    wanted = {canonical for canonical in (_canonical_agent_id(str(v)) for v in agent_ids if v) if canonical}
    if not wanted or user is None:
        return set()
    if user_group_ids is None:
        user_group_ids = await get_user_group_ids(db, user.id)
    rows = (await db.execute(select(Agent).where(Agent.id.in_([uuid.UUID(i) for i in wanted])))).scalars().all()
    return {str(agent.id) for agent in rows if await may_read_agent_traces(db, user, agent, user_group_ids)}


_MAX_LIMIT = 25
_DEFAULT_LIMIT = 10
_MAX_SPAN_TRACE_COUNT = 5
#: Spans listed per trace by default, and at most: a run of ninety tool calls
#: listed every span with its previews and answered over 100 KB when the
#: caller wanted the rollup. The rest is counted and paged with ``span_offset``.
_DEFAULT_MAX_SPANS = 30
_MAX_SPANS = 200

#: Payload fields addressable by ``get_trace_payload``.  ``tool_arguments``
#: exists on spans only; the rest exist on both traces and spans.
_PAYLOAD_FIELDS = ("input", "output", "metadata", "tool_arguments")
_SPAN_ONLY_FIELDS = ("tool_arguments",)

#: Per-field preview cap.  A preview exists to let the agent *choose* what to
#: read in full, not to carry the payload — the whole value is one
#: ``get_trace_payload`` call away, pageable to the end.
_PREVIEW_MAX_CHARS = 2_000
#: Floor for a single preview once the shared budget is nearly spent, so a late
#: span still shows something rather than nothing.
_PREVIEW_MIN_CHARS = 200
#: Ceiling on all previews of one response together.  Five traces of forty
#: spans at the per-field cap would otherwise serialize several hundred
#: thousand characters straight into the context.  60,000 put a run of ninety
#: tool calls past 100 KB; with the span cap the answer stays under 30 KB,
#: every preview being one get_trace_payload call from its whole value.
_RESPONSE_PREVIEW_BUDGET_CHARS = 12_000
#: Ceiling on the serialized spans of one response together, previews and the
#: fixed fields of every span included.  The span count alone did not bound the
#: answer: thirty spans of a run of 188 tool calls weighed 32,707 characters
#: (a failed tool span's error text alone 7,391), and the listing answered
#: 43,399.  A page stops at the span that would cross it and names the next
#: offset, so nothing is skipped (#10603).  18,000 leaves room under 30,000 for
#: the rollup (under 6,000), the trace's own previews and its headers.
_SPAN_LISTING_BUDGET_CHARS = 18_000
#: Cap on one span's ``status_message`` in a listing.  It sits outside the
#: preview budget (it is not a payload pane), so it is cut here, with a marker
#: naming where the whole error is read.
_STATUS_MESSAGE_MAX_CHARS = 400

#: One page of a full payload.  Bounded because a stored payload can be
#: megabytes and a tool result reaches the model verbatim; ``next_offset``
#: walks the rest.
_PAYLOAD_PAGE_DEFAULT_CHARS = 20_000
_PAYLOAD_PAGE_MAX_CHARS = 40_000

_ALLOWED_PERIODS = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "90d": timedelta(days=90),
}


#: What the trace switches of Settings > Usage and quotas > Observability leave out of
#: what these tools read: a run missing from a listing, or a null payload, is
#: then not stored rather than absent or empty.
_TRACE_STORAGE_OFF_NOTE = (
    "Trace storage is off in Settings > Usage and quotas > Observability: no trace is stored while it is off, "
    "so the runs since it was turned off are not listed."
)
_TRACE_PAYLOADS_OFF_NOTE = (
    "Store full payloads is off in Settings > Usage and quotas > Observability: the traces written while it is off "
    "keep no input, output or tool arguments, so a null one there was not stored - it does not "
    "mean the call was empty."
)


def _trace_switches_note() -> str | None:
    switches = trace_switches()
    if not switches.storage_enabled:
        return _TRACE_STORAGE_OFF_NOTE
    if not switches.store_payloads:
        return _TRACE_PAYLOADS_OFF_NOTE
    return None


def _parse_period(period: str | None) -> tuple[datetime | None, str | None]:
    value = (period or "7d").strip().lower()
    if value not in _ALLOWED_PERIODS:
        return None, (
            "Error: invalid period. Allowed values are 24h, 7d, 30d, 90d."
        )
    return datetime.now(UTC) - _ALLOWED_PERIODS[value], None


#: Traces read at most to answer a filter on the stored rollup (generations,
#: streamed first token), newest first: the rollup lives in the trace metadata,
#: so the filter runs on the rows read, never in the store's query.
_ROLLUP_FILTER_SCAN_MAX = 500
_ROLLUP_FILTER_SCAN_PAGE = 100


def _parse_instant(value: object, name: str) -> tuple[datetime | None, str | None]:
    """An ISO 8601 instant (a naive one read as UTC), ``None`` when unset, or the refusal."""
    text = str(value or "").strip()
    if not text:
        return None, None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None, f"Error: {name} is not an ISO 8601 time (e.g. 2026-11-06T11:00:00Z): {text!r}."
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)), None


def _optional_count(value: object) -> int | None:
    if not isinstance(value, int | str) or isinstance(value, bool) or value == "":
        return None
    try:
        return max(0, int(value))
    except ValueError:
        return None


def _rollup_filter(
    min_generations: object, max_generations: object, streamed_only: object
) -> Callable[[Any], bool] | None:
    """The predicate on a trace's stored rollup, or ``None`` when no rollup filter is set.

    A trace without a stored rollup (an older one, or still running) never
    matches a set filter: its figures are unknown, not zero.
    """
    low = _optional_count(min_generations)
    high = _optional_count(max_generations)
    streamed = streamed_only is True
    if low is None and high is None and not streamed:
        return None

    def keep(trace: Any) -> bool:
        rollup = stored_rollup(getattr(trace, "metadata_json", None))
        if rollup is None:
            return False
        if low is not None or high is not None:
            generations = rollup.get("generations")
            if isinstance(generations, bool) or not isinstance(generations, int | float):
                return False
            if low is not None and generations < low:
                return False
            if high is not None and generations > high:
                return False
        if streamed:
            # A chat turn's timeline, else any streamed run's first output.
            return rollup.get("turn_first_token_ms") is not None or rollup.get("first_token_ms") is not None
        return True

    return keep


async def _scan_rollup_filtered(
    trace_store: Any,
    list_kwargs: dict[str, Any],
    keep: Callable[[Any], bool],
    *,
    first: int,
    count: int,
    exclude_id: str | None,
) -> tuple[list[Any], int, bool]:
    """Read the newest traces page by page, keep the matching ones, answer one page of them.

    Returns the page, the number of matches found and whether the scan stopped
    at ``_ROLLUP_FILTER_SCAN_MAX`` with traces left unread.
    """
    matched: list[Any] = []
    read = 0
    capped = False
    while True:
        if read >= _ROLLUP_FILTER_SCAN_MAX:
            capped = True
            break
        rows, stored_total = await trace_store.list_traces(**list_kwargs, limit=_ROLLUP_FILTER_SCAN_PAGE, offset=read)
        if not rows:
            break
        read += len(rows)
        matched.extend(row for row in rows if str(row[0].id) != exclude_id and keep(row[0]))
        if read >= int(stored_total) or len(rows) < _ROLLUP_FILTER_SCAN_PAGE:
            break
    return matched[first : first + count], len(matched), capped


_INCLUDE_SPANS_DESCRIPTION = f"Spans and the per-run rollup; at most {_MAX_SPAN_TRACE_COUNT} traces."

#: The span window and the rollup-only form, shared by both listing tools.
_SPAN_WINDOW_PROPERTIES: dict[str, Any] = {
    "max_spans": {
        "type": "integer",
        "minimum": 1,
        "maximum": _MAX_SPANS,
        "default": _DEFAULT_MAX_SPANS,
        "description": "Spans listed per trace; the rollup counts all.",
    },
    "span_offset": {
        "type": "integer",
        "minimum": 0,
        "default": 0,
        "description": "First span listed: the previous spans_next_offset.",
    },
    "rollup_only": {
        "type": "boolean",
        "default": False,
        "description": "The rollup alone: no span list, no preview.",
    },
    "span_tool": {"type": "string", "description": "Span search: this tool's spans, compact"},
    "span_match": {"type": "string", "description": "Span search: output or arguments hold it"},
}


def _span_window(max_spans: object, span_offset: object) -> tuple[int, int]:
    """Spans shown per trace and the first one shown: defaults when unset or not integers, bounded."""
    try:
        count = int(max_spans) if max_spans is not None and not isinstance(max_spans, bool) else _DEFAULT_MAX_SPANS
    except (TypeError, ValueError):
        count = _DEFAULT_MAX_SPANS
    try:
        first = int(span_offset) if span_offset is not None and not isinstance(span_offset, bool) else 0
    except (TypeError, ValueError):
        first = 0
    return max(1, min(count, _MAX_SPANS)), max(0, first)


def _safe_limit(limit: int | None, *, include_spans: bool) -> int:
    raw = limit if isinstance(limit, int) else _DEFAULT_LIMIT
    bounded = max(1, min(raw, _MAX_LIMIT))
    if include_spans:
        return min(bounded, _MAX_SPAN_TRACE_COUNT)
    return bounded


async def _load_user_context(db: AsyncSession, user_id: str) -> tuple[User | None, list[str]]:
    uid = uuid.UUID(user_id)
    user_row = await db.execute(select(User).where(User.id == uid))
    user = user_row.scalar_one_or_none()
    if user is None:
        return None, []
    group_ids = await get_user_group_ids(db, uid)
    return user, group_ids


def _trace_viewer(user: User) -> SimpleNamespace:
    """*user* as the trace store's visibility reads them (``id``, ``is_admin``).

    The store skips its visibility for an admin reader; these tools take that
    bypass only through :func:`agent_tool_admin_bypass`.
    """
    return SimpleNamespace(id=user.id, is_admin=agent_tool_admin_bypass(user))


def _own_trace_reader(user: User) -> SimpleNamespace:
    """*user* reading the calling agent's OWN traces: the store's per-user visibility is skipped.

    An agent reads every earlier run of itself, including one whose
    trace the run's user cannot see - a company-managed or ``access_restricted``
    agent run on someone's behalf. Used only for a listing hard-scoped to
    ``agent_ids=[calling agent]`` and for a trace whose ``agent_id`` is checked
    against the calling agent before anything of it is served, so another
    agent's traces stay behind the supervision grants.
    """
    return SimpleNamespace(id=user.id, is_admin=True)


async def _resolve_trace_prefix(
    db: AsyncSession,
    prefix: str,
    value: str,
    *,
    viewer: Any,
    group_ids: list[str],
    own_agent_id: str | None,
) -> tuple[uuid.UUID | None, str | None]:
    """The trace an id *prefix* starts, among the traces ``get_trace_payload``
    may open: the ones *viewer* sees (``trace_visible_query``, the trace
    explorer's rule) and the calling agent's own. ``(id, None)`` for one,
    ``(None, None)`` for none, ``(None, refusal)`` naming them for several.

    Only the candidates are found here: the payload read that follows still
    runs every access check of a full id (supervision grants included).
    """
    from sqlalchemy.orm import load_only

    from app.models.llm_trace import LLMTrace
    from app.services.tracing.read_store import trace_visible_query

    found: dict[uuid.UUID, str] = {}
    visible = (
        trace_visible_query(viewer, group_ids)
        .where(id_prefix_clause(LLMTrace.id, prefix))
        .options(load_only(LLMTrace.id, LLMTrace.name))
        .order_by(LLMTrace.id)
        .limit(MAX_CANDIDATES + 1)
    )
    for trace in (await db.execute(visible)).scalars().all():
        found[trace.id] = trace.name or ""
    if own_agent_id is not None and len(found) <= MAX_CANDIDATES:
        own = (
            select(LLMTrace.id, LLMTrace.name)
            .where(id_prefix_clause(LLMTrace.id, prefix), LLMTrace.agent_id == uuid.UUID(own_agent_id))
            .order_by(LLMTrace.id)
            .limit(MAX_CANDIDATES + 1)
        )
        for row in (await db.execute(own)).all():
            found.setdefault(row[0], row[1] or "")
    candidates = sorted(found.items())
    resolved, refusal = resolved_or_refused(value, "traces", candidates)
    if refusal or not candidates:
        return None, refusal
    return uuid.UUID(str(resolved)), None


#: The root traces one run id answers at most (one per resumed attempt). Not
#: the candidate cap of an ambiguous id prefix (``MAX_CANDIDATES``): a run
#: resumed through a series of rollouts records more than five root traces,
#: and the newest, the one that holds the outcome, was the one cut.
RUN_ROOT_TRACES_MAX = 50


def _run_traces_refusal(value: str, candidates: list[tuple[Any, str]], *, listing_tool: str) -> str:
    """The refusal of a run id whose run has several root traces: how many,
    each one oldest first (past :data:`RUN_ROOT_TRACES_MAX`, the first ones and
    the newest), the newest named apart, and the listing call that answers them
    all in one read."""
    raw = (value or "").strip()
    count = len(candidates)
    shown = list(candidates[:RUN_ROOT_TRACES_MAX])
    if count > RUN_ROOT_TRACES_MAX:
        shown = [*shown[: RUN_ROOT_TRACES_MAX - 1], candidates[-1]]
        count_text = f"more than {RUN_ROOT_TRACES_MAX}"
    else:
        count_text = str(count)
    named = "; ".join(f"`{cid}` ({label})" if label else f"`{cid}`" for cid, label in shown)
    newest = shown[-1][0]
    return (
        f"Error: `{raw}` names a run with {count_text} root traces you can see (one per resumed attempt), "
        f"oldest first: {named}. The newest, `{newest}`, holds the outcome. Nothing was done: call again "
        f"with the full id of the one you mean, or list them all with {listing_tool}(trace_id={raw})."
    )


async def _own_trace_of_run(
    db: AsyncSession, value: str, *, own_agent_id: str | None
) -> tuple[uuid.UUID | None, str | None]:
    """The calling agent's trace of the run *value* names (a run id, or its 8+
    hex prefix): the id a work item shows as "held by run" or a run summary
    gives as run_id, which is not the trace's own id.

    ``(trace id, None)`` for one root trace of the run, ``(None, None)`` for
    none, ``(None, refusal)`` naming them when the run has several (each one
    can be passed as ``trace_id``). Only this agent's traces are searched, so
    it reads no wider than the self-only listing.
    """
    candidates = await _own_run_root_traces(db, value, own_agent_id=own_agent_id)
    if not candidates:
        return None, None
    if len(candidates) == 1:
        return uuid.UUID(str(candidates[0][0])), None
    return None, _run_traces_refusal(value, candidates, listing_tool="inspect_my_traces")


async def _own_run_root_traces(
    db: AsyncSession, value: str, *, own_agent_id: str | None
) -> list[tuple[Any, str]]:
    """``(trace id, name)`` of every root trace of the calling agent recorded by
    the run *value* names (a run id or its 8+ hex prefix), oldest first.

    A run resumed after a worker stop records one root trace per attempt; at
    most :data:`RUN_ROOT_TRACES_MAX` + 1 are read, so a caller can tell a run
    cut at the bound. Empty for none, for another agent's run and for a value
    that is no id.
    """
    if own_agent_id is None:
        return []
    from app.models.llm_trace import LLMTrace

    run_clause = _run_id_clause(value)
    if run_clause is None:
        return []
    rows = (
        await db.execute(
            select(LLMTrace.id, LLMTrace.name)
            .where(
                run_clause,
                LLMTrace.agent_id == uuid.UUID(own_agent_id),
                LLMTrace.parent_trace_id.is_(None),
            )
            .order_by(LLMTrace.started_at, LLMTrace.id)
            .limit(RUN_ROOT_TRACES_MAX + 1)
        )
    ).all()
    return [(row[0], row[1] or "") for row in rows]


def _run_id_clause(value: str) -> Any:
    """``llm_traces.assignment_run_id`` matching *value* (a run id or its 8+ hex
    prefix), or ``None`` for a value that is no id."""
    from app.models.llm_trace import LLMTrace

    raw = (value or "").strip()
    prefix = id_prefix(raw)
    if prefix is not None:
        return id_prefix_clause(LLMTrace.assignment_run_id, prefix)
    try:
        return LLMTrace.assignment_run_id == uuid.UUID(raw)
    except (TypeError, ValueError, AttributeError):
        return None


async def _supervised_run_root_traces(
    db: AsyncSession,
    value: str,
    *,
    viewer: Any,
    group_ids: list[str],
    agent_ids: list[str] | None,
) -> list[tuple[Any, str]]:
    """``(trace id, name)`` of every root trace of the run *value* names (a run
    id or its 8+ hex prefix) among the agents *agent_ids* (``None``: every
    agent), as *viewer* sees them (``trace_visible_query``), oldest first.

    The supervised twin of :func:`_own_run_root_traces`: a supervisor knows a
    run of an agent it supervises by the run id a work item names. Only the
    candidates are found here; the read that follows still runs every access
    check a trace id gets, so nothing is readable that a trace id would not open.
    """
    if agent_ids is not None and not agent_ids:
        return []
    from sqlalchemy.orm import load_only

    from app.models.llm_trace import LLMTrace
    from app.services.tracing.read_store import trace_visible_query

    run_clause = _run_id_clause(value)
    if run_clause is None:
        return []
    agent_clause = (
        LLMTrace.agent_id.is_not(None)
        if agent_ids is None
        else LLMTrace.agent_id.in_([uuid.UUID(agent) for agent in agent_ids])
    )
    query = (
        trace_visible_query(viewer, group_ids)
        .where(run_clause, agent_clause, LLMTrace.parent_trace_id.is_(None))
        .options(load_only(LLMTrace.id, LLMTrace.name))
        .order_by(LLMTrace.started_at, LLMTrace.id)
        .limit(RUN_ROOT_TRACES_MAX + 1)
    )
    return [(trace.id, trace.name or "") for trace in (await db.execute(query)).scalars().all()]


def _one_trace_of_run(value: str, candidates: list[tuple[Any, str]]) -> tuple[uuid.UUID | None, str | None]:
    """``(id, None)`` for a run with one root trace, ``(None, None)`` for none,
    ``(None, refusal)`` naming each trace of a run with several (a resumed run)."""
    if not candidates:
        return None, None
    if len(candidates) == 1:
        return uuid.UUID(str(candidates[0][0])), None
    return None, _run_traces_refusal(value, candidates, listing_tool="inspect_agent_traces")


def _run_page(candidates: list[tuple[Any, str]]) -> tuple[list[tuple[Any, str]], str]:
    """The root traces a run listing shows and its note: every one up to
    :data:`RUN_ROOT_TRACES_MAX`; past it the oldest ones and the newest (the
    attempt that holds the outcome), with a note saying the run was cut."""
    if len(candidates) <= RUN_ROOT_TRACES_MAX:
        return candidates, ""
    shown = [*candidates[: RUN_ROOT_TRACES_MAX - 1], candidates[-1]]
    return shown, (
        f"This run has more than {RUN_ROOT_TRACES_MAX} root traces: the oldest {RUN_ROOT_TRACES_MAX - 1} "
        "and the newest are listed."
    )


async def _scoped_traces_by_id(
    db: AsyncSession,
    value: str,
    *,
    reader: Any,
    group_ids: list[str],
    agent_ids: list[str] | None,
) -> tuple[list[Any], str]:
    """The stored traces ``inspect_agent_traces(trace_id=...)`` names: every root
    trace of a run (*value* a run id or its 8+ hex prefix), oldest first, else
    the one trace *value* names (UUID or 8+ hex prefix), within *agent_ids*
    (``None``: every agent) as *reader* sees them. ``([], refusal)`` for none
    or several; ``(traces, note)`` otherwise, the note saying when a run was
    cut at :data:`RUN_ROOT_TRACES_MAX`."""
    from app.models.llm_trace import LLMTrace

    not_found = "Error: trace not found, or not visible to you."
    store = build_trace_read_store()

    async def _read(trace_id: Any) -> Any:
        try:
            trace, _, _, _ = await store.get_trace_detail(
                db, user=reader, user_group_ids=group_ids, trace_id=uuid.UUID(str(trace_id))
            )
        except HTTPException:
            return None
        if trace is None or (agent_ids is not None and _canonical_agent_id(str(trace.agent_id or "")) not in agent_ids):
            return None
        return trace

    run_roots, run_note = _run_page(
        await _supervised_run_root_traces(db, value, viewer=reader, group_ids=group_ids, agent_ids=agent_ids)
    )
    run_traces = [trace for trace_id, _name in run_roots if (trace := await _read(trace_id)) is not None]
    if run_traces:
        # On success the second slot is the run's note (empty unless cut).
        return run_traces, run_note
    prefix = id_prefix(value)
    if prefix is None:
        try:
            resolved: Any = uuid.UUID((value or "").strip())
        except (TypeError, ValueError, AttributeError):
            return [], "Error: trace_id must be a valid UUID, or its first 8+ hexadecimal characters."
    else:
        from app.services.tracing.read_store import trace_visible_query

        query = trace_visible_query(reader, group_ids).where(id_prefix_clause(LLMTrace.id, prefix))
        if agent_ids is not None:
            query = query.where(LLMTrace.agent_id.in_([uuid.UUID(agent) for agent in agent_ids]))
        rows = (await db.execute(query.order_by(LLMTrace.id).limit(MAX_CANDIDATES + 1))).scalars().all()
        candidates = [(row.id, row.name or "") for row in rows]
        resolved, refusal = resolved_or_refused(value, "traces", candidates)
        if refusal:
            return [], refusal
        if not candidates:
            return [], not_found
    trace = await _read(resolved)
    return ([trace], "") if trace is not None else ([], not_found)


async def _own_traces_of_run(
    db: AsyncSession, value: str, *, user: User, group_ids: list[str], own_agent_id: str | None
) -> tuple[list[Any], str]:
    """Every stored root trace of the calling agent's run *value*, oldest first,
    and the note of a run cut at :data:`RUN_ROOT_TRACES_MAX`: the answer to a
    run id whose run has several traces (one per resumed attempt), where a
    single trace would leave segments out."""
    store = build_trace_read_store()
    traces: list[Any] = []
    roots, note = _run_page(await _own_run_root_traces(db, value, own_agent_id=own_agent_id))
    for trace_id, _name in roots:
        try:
            trace, _, _, _ = await store.get_trace_detail(
                db, user=_own_trace_reader(user), user_group_ids=group_ids, trace_id=uuid.UUID(str(trace_id))
            )
        except HTTPException:
            continue
        if trace is not None and _canonical_agent_id(str(trace.agent_id)) == own_agent_id:
            traces.append(trace)
    return traces, (note if traces else "")


async def _own_trace_by_id(
    db: AsyncSession,
    value: str,
    *,
    user: User,
    group_ids: list[str],
    own_agent_id: str | None,
) -> tuple[Any, str]:
    """The calling agent's stored trace named by *value* (a UUID or its 8+ hex prefix).

    ``(trace, "")`` when it is one of the calling agent's own traces, else
    ``(None, refusal)``: another agent's trace answers "not found" exactly like
    a missing one, so this reads no wider than the self-only listing does.
    Supervised reads stay with ``inspect_agent_traces`` and ``get_trace_payload``.
    """
    not_found = "Error: trace not found among this agent's traces."
    if own_agent_id is None:
        return None, not_found
    prefix = id_prefix(value)
    if prefix is not None:
        from app.models.llm_trace import LLMTrace

        candidates = [
            (row[0], row[1] or "")
            for row in (
                await db.execute(
                    select(LLMTrace.id, LLMTrace.name)
                    .where(id_prefix_clause(LLMTrace.id, prefix), LLMTrace.agent_id == uuid.UUID(own_agent_id))
                    .order_by(LLMTrace.id)
                    .limit(MAX_CANDIDATES + 1)
                )
            ).all()
        ]
        resolved, refusal = resolved_or_refused(value, "traces", candidates)
        if refusal:
            return None, refusal
        if not candidates:
            # Not a trace id: maybe the id of the run that wrote the trace.
            by_run, run_refusal = await _own_trace_of_run(db, value, own_agent_id=own_agent_id)
            if run_refusal:
                return None, run_refusal
            if by_run is None:
                return None, await _unfinished_background_task_note(db, value, own_agent_id) or not_found
            resolved = by_run
        parsed = uuid.UUID(str(resolved))
    else:
        try:
            parsed = uuid.UUID(value)
        except (TypeError, ValueError, AttributeError):
            return None, "Error: trace_id must be a valid UUID, or its first 8+ hexadecimal characters."
    store = build_trace_read_store()
    try:
        trace, _, _, _ = await store.get_trace_detail(
            db, user=_own_trace_reader(user), user_group_ids=group_ids, trace_id=parsed
        )
    except HTTPException:
        trace = None
    if trace is None and prefix is None:
        # A full id that is no trace of this agent: maybe a run id.
        by_run, run_refusal = await _own_trace_of_run(db, value, own_agent_id=own_agent_id)
        if run_refusal:
            return None, run_refusal
        if by_run is not None:
            try:
                trace, _, _, _ = await store.get_trace_detail(
                    db, user=_own_trace_reader(user), user_group_ids=group_ids, trace_id=by_run
                )
            except HTTPException:
                trace = None
    if trace is None or _canonical_agent_id(trace.agent_id) != own_agent_id:
        return None, await _unfinished_background_task_note(db, value, own_agent_id) or not_found
    return trace, ""


#: Background task states whose trace is not stored yet: a turn writes its
#: trace when it ends (see ``app.services.tracing.background_run``).
_UNFINISHED_TASK_STATES = ("pending", "running")


async def _unfinished_background_task_note(db: AsyncSession, value: str, own_agent_id: str | None) -> str:
    """The answer to the id of a background task this agent spawned or runs
    that has not ended yet (a full id or its 8+ hex prefix), else ``""``.

    Its trace is written when its turn ends, so "trace not found" read as
    "this run never happened"; the answer names the task's state and the
    tools that follow it instead. Only tasks this agent spawned or executes
    are matched, so it reads no wider than the self-only listing.
    """
    if own_agent_id is None:
        return ""
    from sqlalchemy import or_

    from app.models.sub_agent_task import SubAgentTask

    raw = (value or "").strip()
    prefix = id_prefix(raw)
    if prefix is not None:
        id_clause = id_prefix_clause(SubAgentTask.id, prefix)
    else:
        try:
            id_clause = SubAgentTask.id == uuid.UUID(raw)
        except (TypeError, ValueError, AttributeError):
            return ""
    me = uuid.UUID(own_agent_id)
    try:
        rows = (
            await db.execute(
                select(SubAgentTask.id, SubAgentTask.status, SubAgentTask.started_at)
                .where(
                    id_clause,
                    or_(SubAgentTask.parent_agent_id == me, SubAgentTask.target_agent_id == me),
                    SubAgentTask.status.in_(_UNFINISHED_TASK_STATES),
                )
                .limit(2)
            )
        ).all()
    except Exception:
        logger.debug("inspect_my_traces: background task lookup failed", exc_info=True)
        return ""
    if len(rows) != 1:
        return ""
    task_id, status, started_at = rows[0]
    since = f" since {started_at.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}" if started_at else ""
    return (
        f"Background task {task_id} is {status}{since}: its trace is stored when its turn ends, "
        "so there is none to read yet. Follow it with check_task_status, then read its trace "
        "with this tool once it has ended."
    )


def _canonical_agent_id(value: str | None) -> str | None:
    try:
        return str(uuid.UUID(str(value))) if value else None
    except (TypeError, ValueError, AttributeError):
        return None


#: A read of other agents' traces is audited once per reader, per identical
#: read, per window. An agent paging one payload, or listing the same
#: agents again, wrote rows that differed by their timestamp alone - the page
#: offset and the listing's filters were never recorded - so the trail keeps the
#: first and says in ``window_seconds`` that repeats inside the window are folded.
TRACE_READ_AUDIT_WINDOW_SECONDS = 15 * 60


def trace_read_audit_key(
    user_id: object, entity_id: object, details: dict[str, Any], *, action: str = "inspect_traces"
) -> str:
    """Redis marker of one reader's audited window on one read (the row's own content).

    Any other audited read (*action*, the platform insights audit-log read)
    hashes its action in, so two kinds of read never share a window.
    """
    parts: list[Any] = [str(entity_id), details] if action == "inspect_traces" else [action, str(entity_id), details]
    content = json.dumps(parts, sort_keys=True, default=str)
    digest = hashlib.sha256(content.encode()).hexdigest()[:32]
    return f"trace:inspect:audit:{user_id}:{digest}"


async def audit_read_once(
    db: AsyncSession,
    user: User,
    *,
    action: str,
    entity_type: str,
    entity_id: str | None,
    summary: str,
    details: dict[str, Any],
) -> None:
    """Record a read in the audit log unless the same row was written in the window.

    The runner-history oversight pattern: the marker is written only
    AFTER the row is committed, so a failed write never covers its retry; two
    first reads racing through the unmarked window may both audit - one row too
    many, never one too few - and a Redis failure audits anyway.
    """
    from app.core.redis import redis_client
    from app.services.audit import log_audit

    details = {**details, "window_seconds": TRACE_READ_AUDIT_WINDOW_SECONDS}
    key = trace_read_audit_key(user.id, entity_id, details, action=action)
    redis = None
    try:
        redis = await redis_client()
        if await redis.exists(key):
            return
    except Exception as exc:
        redis = None
        logger.warning(
            "Trace read audit marker unavailable (auditing anyway) for user=%s: %s",
            user.id,
            format_exception_message(exc),
        )
    await log_audit(
        db,
        user=user,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        summary=summary,
        details=details,
    )
    await db.commit()
    if redis is None:
        return
    try:
        await redis.set(key, "1", ex=TRACE_READ_AUDIT_WINDOW_SECONDS)
    except Exception as exc:
        logger.warning(
            "Trace read audit marker not written for user=%s: %s",
            user.id,
            format_exception_message(exc),
        )


async def _audit_trace_read(
    db: AsyncSession, user: User, *, entity_id: str | None, summary: str, details: dict[str, Any]
) -> None:
    """Record an ``inspect_traces`` read unless the same row was written in the window."""
    await audit_read_once(
        db,
        user,
        action="inspect_traces",
        entity_type="agent",
        entity_id=entity_id,
        summary=summary,
        details=details,
    )


def _truncation_marker(hidden: int) -> str:
    """The truncation marker used across the trace layer, so one grep finds all.

    Mirrors ``tracing.payload_offload._truncate_marker`` and
    ``langfuse_tracing._truncate``.  The character count is load-bearing here:
    it is how the agent knows how much ``get_trace_payload`` still holds for it.
    """
    return f"… [truncated {hidden} chars]"


def _serialize_payload(value: Any, *, include_reasoning: bool = False) -> str | None:
    """Render a payload as the text the agent will read.

    One rendering for previews and for paged reads alike.  Pages used to be
    pretty-printed, which broke the arithmetic the previews hand out: the
    ``[truncated N chars]`` count is an offset into *this* text, and it pointed
    into a differently-indented string.  Compact also keeps the page count down
    for a reader that gains nothing from the whitespace.

    The model's reasoning is left out unless asked for: Claude declines a prompt
    that carries it back (#12356). Filtering here keeps every offset consistent.
    """
    if value is None:
        return None
    if not include_reasoning:
        value = without_reasoning(value)
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _envelope_excerpt(envelope: dict, *, include_reasoning: bool = False) -> str | None:
    """The excerpt an offloaded payload's envelope kept: serialized JSON, cut short."""
    text = _serialize_payload(envelope.get("preview"), include_reasoning=True)
    if text is None or include_reasoning:
        return text
    return serialized_without_reasoning(text)


def _payload_field(row: Any, field: str) -> Any:
    """Read one payload field off a trace or span row.

    ``metadata`` is ``metadata_json`` on both models; the others match their
    attribute names.  Missing attributes read as ``None`` so a row shape that
    lacks a field (a trace has no ``tool_arguments``) degrades to "absent"
    rather than raising.
    """
    if field == "metadata":
        return getattr(row, "metadata_json", None)
    return getattr(row, field, None)


class _PreviewBudget:
    """Shared character allowance for every preview in one tool response.

    Spent in the order previews are built — traces, then their spans in
    execution order — so the earliest steps, which is where a run usually goes
    wrong, keep the most room.  Exhaustion is never silent: a field that was
    cut lands in ``payload_truncated`` either way, and that list is what tells
    the agent to call ``get_trace_payload``.

    Once the budget is gone, a field gets *no* preview rather than a floor's
    worth.  The floor used to apply unconditionally, which meant it multiplied
    by the number of spans and the "ceiling on all previews of one response"
    was not one: ``get_trace_spans`` is unlimited (``_MAX_SPAN_TRACE_COUNT``
    bounds traces, not spans), so four fields × 200 chars × 200 spans put the
    real bound near a quarter of a million characters.  The ceiling now holds at
    ``total`` plus at most one field's overshoot, whatever the span count.
    """

    def __init__(self, total: int = _RESPONSE_PREVIEW_BUDGET_CHARS) -> None:
        self._remaining = max(0, total)
        #: Whether anything at all was held back, so the response only carries
        #: the "fetch the rest with…" note when there is a rest to fetch.
        self.truncated_any = False
        #: Payload fields that had content but got no preview at all, so the
        #: response can say how many rather than leaving a null field looking
        #: like an empty payload.
        self.previews_omitted = 0

    def take(self, text: str) -> tuple[str | None, bool]:
        if self._remaining <= 0:
            self.mark_truncated()
            self.previews_omitted += 1
            return None, True
        allowance = min(_PREVIEW_MAX_CHARS, max(_PREVIEW_MIN_CHARS, self._remaining))
        if len(text) <= allowance:
            self._remaining = max(0, self._remaining - len(text))
            return text, False
        self._remaining = max(0, self._remaining - allowance)
        self.mark_truncated()
        return text[:allowance] + _truncation_marker(len(text) - allowance), True

    def mark_truncated(self) -> None:
        """Record that something was held back, whatever the reason."""
        self.truncated_any = True

    def checkpoint(self) -> tuple[int, bool, int]:
        """The budget's state, so a span built and then left off the page gives its share back."""
        return self._remaining, self.truncated_any, self.previews_omitted

    def restore(self, state: tuple[int, bool, int]) -> None:
        self._remaining, self.truncated_any, self.previews_omitted = state


def _preview(value: Any, budget: _PreviewBudget) -> tuple[str | None, bool]:
    """Serialize a payload field to a budget-bounded preview.

    An offloaded payload's row value is a *pointer*, not the payload: rendering
    it as-is would show the envelope's own fields (size, digest, excerpt) while
    presenting them as the value, and an envelope whose excerpt happens to be
    short would come back unflagged — read as complete, when the bytes are in
    object storage and only ``get_trace_payload`` reaches them.  So an envelope
    yields the excerpt it captured and is always reported as truncated.
    """
    if is_offload_envelope(value):
        text = _envelope_excerpt(value)
        if text is None:
            budget.mark_truncated()
            return None, True
        bounded, _ = budget.take(text)
        budget.mark_truncated()
        return bounded, True
    text = _serialize_payload(value)
    if text is None:
        return None, False
    return budget.take(text)


def _trace_summary(trace, *, budget: _PreviewBudget | None = None) -> dict:
    """Summarize one trace.

    ``budget`` is passed only in the span-inclusive (deep-dive) mode: a listing
    of 25 traces is for picking one, and payload previews there would spend the
    whole response budget before reaching the spans that explain anything.
    """
    summary = {
        "id": str(trace.id),
        "agent_id": str(trace.agent_id) if trace.agent_id else None,
        "source": trace.source,
        "name": trace.name,
        "model": trace.model,
        "status": trace.status,
        "started_at": trace.started_at.isoformat() if trace.started_at else None,
        "ended_at": trace.ended_at.isoformat() if trace.ended_at else None,
        "duration_ms": trace.duration_ms,
        "input_tokens": trace.input_tokens,
        "output_tokens": trace.output_tokens,
        "total_tokens": trace.total_tokens,
        "total_cost": float(trace.total_cost) if trace.total_cost is not None else None,
        "error_message": trace.error_message,
    }
    conversation_id = getattr(trace, "conversation_id", None)
    if conversation_id:
        summary["conversation_id"] = str(conversation_id)
    run_id = getattr(trace, "assignment_run_id", None)
    if run_id:
        summary["run_id"] = str(run_id)
    # The rollup stored when the run closed: every mode carries it, so
    # a listing says which run was slow or heavy without a span read.
    # The per-tool table stays for the span-inclusive mode: a listing of 25
    # runs keeps to the figures and the five largest results.
    rollup = stored_rollup(getattr(trace, "metadata_json", None))
    if rollup is not None:
        summary["rollup"] = {key: value for key, value in rollup.items() if key != "tools"}
    if budget is not None:
        summary.update(_payload_previews(trace, budget, fields=("input", "output")))
    return summary


def _payload_previews(
    row: Any,
    budget: _PreviewBudget,
    *,
    fields: tuple[str, ...],
) -> dict:
    """Preview each of ``fields`` and name the ones that were cut.

    The ``payload_truncated`` list is the contract with the agent: every field
    it names is readable in full through ``get_trace_payload``, so a bounded
    preview is a pointer rather than a dead end.
    """
    previews: dict[str, Any] = {}
    truncated: list[str] = []
    for field in fields:
        text, was_truncated = _preview(_payload_field(row, field), budget)
        previews[f"{field}_preview"] = text
        if was_truncated:
            truncated.append(field)
    previews["payload_truncated"] = truncated
    return previews


def _bounded_status_message(span) -> str | None:
    """The span's status message, cut at ``_STATUS_MESSAGE_MAX_CHARS`` with a pointer to the whole text.

    A failing tool span stores its error as its output too, so the marker names
    the ``output`` field of that span.
    """
    message = span.status_message
    if not isinstance(message, str) or len(message) <= _STATUS_MESSAGE_MAX_CHARS:
        return message
    hidden = len(message) - _STATUS_MESSAGE_MAX_CHARS
    return (
        message[:_STATUS_MESSAGE_MAX_CHARS]
        + _truncation_marker(hidden)
        + f"; the whole error: {TRACE_PAYLOAD_TOOL_NAME}(trace_id={span.trace_id}, span_id={span.id}, field=output)"
    )


def _span_summary(span, budget: _PreviewBudget | None = None) -> dict:
    summary = {
        "id": str(span.id),
        "trace_id": str(span.trace_id),
        "type": span.type,
        "name": span.name,
        "status_message": _bounded_status_message(span),
        "started_at": span.started_at.isoformat() if span.started_at else None,
        "ended_at": span.ended_at.isoformat() if span.ended_at else None,
        "duration_ms": span.duration_ms,
        "input_tokens": span.input_tokens,
        "output_tokens": span.output_tokens,
        "total_cost": float(span.total_cost) if span.total_cost is not None else None,
        "tool_call_id": span.tool_call_id,
    }
    if span.type == "GENERATION":
        summary.update(_generation_figures(span))
    previews = _payload_previews(
        span,
        budget if budget is not None else _PreviewBudget(),
        # Order matters: the budget is spent left to right, and a step is
        # usually diagnosed from what it produced and what it was asked, before
        # its metadata.  ``input`` carries the messages and the assembled
        # prompt; ``output`` carries the completion (reasoning left out on read).
        fields=("output", "input", "tool_arguments", "metadata"),
    )
    # ``tool_arguments`` keeps its historical key: it is the call's arguments,
    # not a payload pane, and callers read it by that name.
    summary["tool_arguments"] = previews.pop("tool_arguments_preview")
    summary.update(previews)
    return summary


def _generation_figures(span) -> dict:
    """Time to first token and the prompt-cache split of one model call.

    The writer stores both on every generation span (``time_to_first_token``,
    and the cache counts inside ``usage_details``, read with the same keys as
    pricing); the rollup only sums them per run. Listed per span, two
    calls of one turn compare directly: a first-call cache miss shows as
    ``cache_write_tokens`` with no ``cache_read_tokens``. ``ttft_ms`` is null
    for a call that did not stream.
    """
    usage = getattr(span, "usage_details", None)
    usage = usage if isinstance(usage, dict) else {}
    ttft = getattr(span, "time_to_first_token", None)
    return {
        "ttft_ms": round(float(ttft), 1) if isinstance(ttft, int | float) and not isinstance(ttft, bool) else None,
        "cache_read_tokens": cached_input_tokens(usage),
        "cache_write_tokens": cache_write_tokens(usage),
    }


def _span_page(
    spans: list[Any],
    budget: _PreviewBudget,
    *,
    count: int,
    first: int,
    listing_budget: list[int] | None = None,
) -> dict:
    """One page of a trace's spans: *count* of them from *first*, the total and the next offset.

    The rollup is computed from every span before this cut, so a capped list
    never changes a figure; ``spans_next_offset`` is null on the last page.

    The page also stops at the span whose serialized summary would carry the
    listing past ``_SPAN_LISTING_BUDGET_CHARS`` (``listing_budget``, shared by
    the traces of one answer): that span gives its preview share back and
    ``spans_next_offset`` names it, so the next call starts there and no span
    is skipped. The first span of a page is always shown.
    """
    total = len(spans)
    remaining = listing_budget if listing_budget is not None else [_SPAN_LISTING_BUDGET_CHARS]
    shown: list[dict] = []
    cut_by_size = False
    for span in spans[first : first + count]:
        state = budget.checkpoint()
        summary = _span_summary(span, budget)
        size = len(json.dumps(summary, ensure_ascii=False))
        if shown and size > remaining[0]:
            budget.restore(state)
            cut_by_size = True
            break
        remaining[0] -= size
        shown.append(summary)
    end = first + len(shown)
    out: dict[str, Any] = {
        "spans": shown,
        "spans_total": total,
        "spans_offset": first,
        "spans_next_offset": end if end < total else None,
    }
    if end < total or first:
        out["spans_note"] = (
            f"Spans {first + 1 if shown else first}-{end} of {total} shown"
            + (" (the page stopped at the listing's size budget)" if cut_by_size else "")
            + (f"; {total - end} more: pass span_offset={end}" if end < total else "")
            + ". rollup_only=true answers the rollup alone."
        )
    return out


#: Characters a span search shows of each matching span's arguments and output.
_MATCH_ARGUMENTS_CHARS = 300
_MATCH_OUTPUT_CHARS = 400
#: Characters of the output shown before the first match, the rest after it.
_MATCH_LEAD_CHARS = 100


def _span_search(span_tool: object, span_match: object) -> tuple[str, str] | None:
    """The span search asked for: (tool name, lower-case text), or None when neither is given."""
    tool = span_tool.strip() if isinstance(span_tool, str) else ""
    text = span_match.strip().lower() if isinstance(span_match, str) else ""
    if not tool and not text:
        return None
    return tool, text


def _bare_tool_name(name: object) -> str:
    """A tool span's name without the tracer's ``tool:`` prefix, lower case.

    The tracer stores a tool span as ``tool:<name>`` while every caller of the
    span search names the tool itself: both forms match the same spans.
    """
    text = name.strip().lower() if isinstance(name, str) else ""
    return text.removeprefix("tool:")


#: Tool names a span search lists when its span_tool names no span of the trace.
_MAX_LISTED_TOOLS = 20


def _searchable_text(value: Any) -> str:
    """The text a span search reads: the payload, or an offloaded payload's stored excerpt."""
    if is_offload_envelope(value):
        return _envelope_excerpt(value) or ""
    return _serialize_payload(value) or ""


def _excerpt(text: str, needle: str, limit: int) -> str:
    """*limit* characters of *text* around the first *needle* (its head without one), cut marked."""
    at = text.lower().find(needle) if needle else -1
    start = max(0, at - _MATCH_LEAD_CHARS) if at > 0 else 0
    window = text[start : start + limit]
    if start:
        window = "…" + window
    hidden = len(text) - start - limit
    return window + (_truncation_marker(hidden) if hidden > 0 else "")


def _matching_spans(spans: list[Any], search: tuple[str, str]) -> list[dict]:
    """The TOOL spans of a trace named *tool* whose output or arguments hold *text*, compact.

    A run of fifty iterations was searched for two tool outputs by paging the
    whole span listing eight times (206,000 characters, the rollup and the
    trace's input preview repeated on every page). Each match carries its span
    id, the call's arguments and the output around the first hit, so
    ``get_trace_payload`` reads the whole of the one that matters. An
    offloaded payload is searched in its stored excerpt only.
    """
    tool, needle = search
    wanted = _bare_tool_name(tool)
    found: list[dict] = []
    for span in spans:
        if span.type != "TOOL" or (wanted and _bare_tool_name(span.name) != wanted):
            continue
        output = _searchable_text(_payload_field(span, "output"))
        arguments = _searchable_text(_payload_field(span, "tool_arguments"))
        if needle and needle not in output.lower() and needle not in arguments.lower():
            continue
        found.append(
            {
                "span_id": str(span.id),
                "tool": (span.name or "").removeprefix("tool:"),
                "started_at": span.started_at.isoformat() if span.started_at else None,
                "failed": _span_failed(span),
                "tool_arguments": _excerpt(arguments, "", _MATCH_ARGUMENTS_CHARS),
                "output": _excerpt(output, needle, _MATCH_OUTPUT_CHARS),
            }
        )
    return found


def _match_page(spans: list[Any], search: tuple[str, str], *, count: int, first: int) -> dict:
    """One page of a trace's matching spans, paged with ``span_offset`` over the matches."""
    found = _matching_spans(spans, search)
    shown = found[first : first + count]
    end = first + len(shown)
    page: dict[str, Any] = {
        "span_matches": shown,
        "span_matches_total": len(found),
        "spans_searched": len(spans),
        "spans_next_offset": end if end < len(found) else None,
    }
    wanted = _bare_tool_name(search[0])
    if wanted and not found:
        # A tool the trace never called is not "no match": name the tools it did call.
        called = sorted({_bare_tool_name(s.name) for s in spans if s.type == "TOOL" and s.name})
        if wanted not in called:
            listed = ", ".join(called[:_MAX_LISTED_TOOLS]) or "none"
            more = len(called) - _MAX_LISTED_TOOLS
            tail = f" (+{more} more)" if more > 0 else ""
            page["span_tool_note"] = f"No span of tool {wanted} in this trace; tools in it: {listed}{tail}"
    return page


#: How many of a run's biggest tool results the rollup names.
_ROLLUP_LARGEST_RESULTS = 5


def _payload_chars(value: Any) -> int:
    """Size of a payload as stored, in characters (reasoning not filtered out: a size needs no copy).

    An offloaded payload's row value is an envelope whose own size says nothing:
    its ``size_bytes`` is the stored payload's size, the closest measure there
    is without downloading it (bytes, not characters: equal for ASCII JSON).
    """
    if is_offload_envelope(value):
        try:
            return max(0, int(value.get("size_bytes") or 0))
        except (TypeError, ValueError):
            return 0
    text = _serialize_payload(value, include_reasoning=True)
    return len(text) if text is not None else 0


def _span_failed(span: Any) -> bool:
    return (getattr(span, "level", None) or "").upper() == "ERROR"


def _run_rollup(trace: Any, spans: list[Any]) -> dict:
    """Per-run figures computed from every span, not from the bounded previews.

    A run measured span by span costs the reader a count over a response the
    preview budget has already cut, and the size of a tool result is shown
    nowhere: the rollup answers "how many iterations, which tools, what did it
    cost, which results were the biggest" in one object, and each of the
    largest results carries its span id so ``get_trace_payload`` reads it.
    """
    iterations = sum(1 for s in spans if s.type == "ITERATION")
    generations = [s for s in spans if s.type == "GENERATION"]
    tool_spans = [s for s in spans if s.type == "TOOL"]

    input_tokens = sum(s.input_tokens or 0 for s in generations)
    output_tokens = sum(s.output_tokens or 0 for s in generations)
    costs = [float(s.total_cost) for s in generations if s.total_cost is not None]
    if not input_tokens and not output_tokens:
        # Spans recorded without usage: the trace totals are the only figure.
        input_tokens = trace.input_tokens or 0
        output_tokens = trace.output_tokens or 0
    total_cost: float | None = sum(costs) if costs else None
    if total_cost is None and trace.total_cost is not None:
        total_cost = float(trace.total_cost)

    tools: dict[str, dict[str, int]] = {}
    results: list[dict[str, Any]] = []
    tool_errors = 0
    for span in tool_spans:
        chars = _payload_chars(_payload_field(span, "output"))
        failed = _span_failed(span)
        tool_errors += int(failed)
        entry = tools.setdefault(span.name, {"calls": 0, "errors": 0, "result_chars": 0})
        entry["calls"] += 1
        entry["errors"] += int(failed)
        entry["result_chars"] += chars
        results.append({"span_id": str(span.id), "tool": span.name, "result_chars": chars})

    results.sort(key=lambda item: item["result_chars"], reverse=True)
    # The per-tool table keeps the MAX_TOOLS heaviest tools, as the rollup a run
    # stores at its close does: a run of 36 distinct tools put rollup_only at
    # the edge of 6,000 characters (#10603). The totals still count every tool.
    ranked_tools = sorted(tools.items(), key=lambda kv: (-kv[1]["result_chars"], kv[0]))
    return {
        # A run recorded before iteration spans existed still counts its turns
        # by model calls.
        "iterations": iterations or len(generations),
        "generations": len(generations),
        "tool_calls": len(tool_spans),
        "tool_errors": tool_errors,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_cost": total_cost,
        "tool_result_chars": sum(item["result_chars"] for item in results),
        "tools": dict(ranked_tools[:MAX_TOOLS]),
        "tools_omitted": max(0, len(ranked_tools) - MAX_TOOLS),
        "largest_tool_results": [r for r in results[:_ROLLUP_LARGEST_RESULTS] if r["result_chars"]],
    }


async def _agents_gone(db: AsyncSession, agent_ids: set[str]) -> set[str]:
    """The ids of *agent_ids* no agent row bears any more: deleted agents.

    A deleted agent's traces stay until the retention purges them; who may
    read them is the trace store's visibility (their own user, the agent's
    owner when it was deleted). Fails closed: a lookup that fails names none,
    so it never widens a read.
    """
    from app.models.agent import Agent

    wanted = {a for a in agent_ids if a}
    if not wanted:
        return set()
    try:
        alive = (
            await db.execute(select(Agent.id).where(Agent.id.in_([uuid.UUID(a) for a in wanted])))
        ).scalars().all()
    except Exception as exc:
        logger.warning("Could not tell deleted agents apart: %s", format_exception_message(exc))
        return set()
    return wanted - {str(a) for a in alive}


async def _deleted_agent_trace_is_callers(db: AsyncSession, user_id: object, trace: Any) -> bool:
    """Whether *trace* is a deleted agent's that is the caller's own to
    read: their run of it, or an agent they owned when it was deleted. Fails
    closed."""
    from app.models.agent_tombstone import AgentTombstone

    agent_id = _canonical_agent_id(getattr(trace, "agent_id", None))
    if agent_id is None or agent_id not in await _agents_gone(db, {agent_id}):
        return False
    if str(getattr(trace, "user_id", None) or "") == str(user_id):
        return True
    try:
        owner = (
            await db.execute(select(AgentTombstone.user_id).where(AgentTombstone.agent_id == uuid.UUID(agent_id)))
        ).scalar_one_or_none()
    except Exception as exc:
        logger.warning("Could not read a deleted agent's owner: %s", format_exception_message(exc))
        return False
    return owner is not None and str(owner) == str(user_id)


async def _annotate_agents(db: AsyncSession, traces: list[dict], trace_rows: list) -> None:
    """Name each listed trace's agent, and say when it was deleted."""
    for item, row in zip(traces, trace_rows, strict=True):
        name = getattr(row, "agent_name", None)
        if name:
            item["agent_name"] = name
    gone = await _agents_gone(db, {t["agent_id"] for t in traces if t.get("agent_id")})
    for item in traces:
        if item.get("agent_id") in gone:
            item["agent_deleted"] = True


class SupervisionDeniedError(Exception):
    """The caller may not run the requested trace listing.

    An exception rather than a sentinel return: the refusal and the widest
    possible success value (``None`` — "no agent filter", every trace the
    caller can see) would otherwise share one return type, and a caller that
    forgot to check would turn a denial into an instance-wide listing. Being
    unignorable is the point.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _supervision_scope(
    *,
    admin_bypass: bool,
    target_uuid: str | None,
    supervision_allowlist: set[str],
    own_agent_id: str | None = None,
) -> list[str] | None:
    """Resolve which agents a supervision listing may cover.

    Returns the ``agent_ids`` filter to apply — ``None`` meaning "no agent
    filter", every trace the caller can see. Raises :class:`SupervisionDeniedError`
    when the caller may not run the listing at all.

    Omitting the target is the cross-agent listing, and it mirrors the REST
    rule in ``_require_readable_observability_scope``: unscoped is an admin
    surface.  A non-admin keeps the one scoped form available to it — the
    explicit per-agent allow-list, which names its targets — rather than
    widening to everything the caller happens to be able to read.
    *admin_bypass* is :func:`agent_tool_admin_bypass` of the caller. Naming
    the calling agent itself (*own_agent_id*) needs no grant: it is the
    self-inspection listing.
    """
    if target_uuid is not None:
        if target_uuid == own_agent_id:
            return [target_uuid]
        if not admin_bypass and target_uuid not in supervision_allowlist:
            raise SupervisionDeniedError(
                "Error: you are not allowed to inspect traces for this agent. "
                "Only admins (not while AGENT_TOOLS_STRICT_ADMIN_ACCESS is set) or agents "
                "with an explicit supervision allow-list can use this target."
            )
        return [target_uuid]

    if admin_bypass:
        return None

    if supervision_allowlist:
        return sorted(supervision_allowlist)

    raise SupervisionDeniedError(
        "Error: listing traces across agents requires admin privileges (not while "
        "AGENT_TOOLS_STRICT_ADMIN_ACCESS is set) or an explicit supervision "
        "allow-list. Pass a target_agent_id instead."
    )


def _own_live_run(agent_id: str | None) -> dict | None:
    """The root context of the trace the calling run is recording, when it is *agent_id*'s.

    A trace reaches the store only when its turn ends, so the run calling
    ``inspect_my_traces`` is never in a listing. Its root context holds
    the spans ended so far. ``None`` outside a traced turn, and for a turn of
    another agent (a context inherited from a delegating run is not this run).
    """
    own = _canonical_agent_id(agent_id)
    if own is None:
        return None
    try:
        from app.services.langfuse_tracing import current_local_trace_root

        root = current_local_trace_root()
    except Exception as exc:
        logger.debug("inspect_my_traces: no current trace context: %s", format_exception_message(exc))
        return None
    if not isinstance(root, dict) or root.get("local_trace_id") is None:
        return None
    if _canonical_agent_id(str(root.get("local_agent_id") or "")) != own:
        return None
    return root


def _names_own_run(own_run: dict | None, value: str) -> bool:
    """Whether *value* (a full id or its 8+ hex prefix) names the calling run:
    its trace id, or the run id a work item shows as "held by run"."""
    raw = (value or "").strip().lower()
    if own_run is None or len(raw) < 8:
        return False
    for key in ("local_trace_id", "local_assignment_run_id"):
        own = own_run.get(key)
        if own and str(own).lower().startswith(raw):
            return True
    return False


#: How many earlier segments of a resumed run the live rollup folds in.
_MAX_EARLIER_SEGMENTS = 50


async def _earlier_segment_rollups(
    db: AsyncSession, trace_store: Any, *, user: User, user_group_ids: list[str], root: dict
) -> list[dict]:
    """The stored rollups of the calling run's earlier segments, oldest first.

    An assignment run resumed after a worker stop records one trace per
    attempt, all carrying the run's id: the attempt handed back closed its
    trace with the figures of its segment. Read with the caller's visibility;
    a read that fails answers none, so the live figures stay the segment's own.
    """
    run_id = root.get("local_assignment_run_id")
    if not run_id:
        return []
    own_id = str(root.get("local_trace_id"))
    try:
        rows, _total = await trace_store.list_traces(
            db=db,
            user=user,
            user_group_ids=user_group_ids,
            search=None,
            conversation_id=None,
            source=None,
            agent_id=None,
            model=None,
            status_filter=None,
            level=None,
            date_from=None,
            date_to=None,
            cursor=None,
            cursor_id=None,
            limit=_MAX_EARLIER_SEGMENTS,
            assignment_run_id=run_id if isinstance(run_id, uuid.UUID) else uuid.UUID(str(run_id)),
        )
    except Exception as exc:
        logger.warning("inspect_my_traces: earlier segments unread: %s", format_exception_message(exc))
        return []
    segments: list[tuple[str, dict]] = []
    for row in rows:
        trace = row[0]
        if str(trace.id) == own_id:
            continue
        rollup = stored_rollup(getattr(trace, "metadata_json", None))
        if rollup is not None:
            started = getattr(trace, "started_at", None)
            segments.append((started.isoformat() if isinstance(started, datetime) else "", rollup))
    segments.sort(key=lambda item: item[0])
    return [rollup for _started, rollup in segments]


async def _recorded_resumes(db: AsyncSession, root: dict) -> int | None:
    """How many resumes the calling run's own record lists, or ``None`` when unread.

    ``assignment_run_resume.prepare_resume`` writes one entry per resume on the
    run's ``trigger_context``, whatever ended the attempt before. A segment
    ended by a hand-back stores its trace; one lost with its worker (taken
    back after the worker said it was stopping, a crash, a lapsed lease)
    cannot, since its spans lived in that worker's memory. The record is the
    count; the stored traces are only the figures.
    """
    run_id = root.get("local_assignment_run_id")
    if not run_id:
        return None
    try:
        from app.models.assignment import AssignmentRun
        from app.services.background_checkpoint import resumes_of

        key = run_id if isinstance(run_id, uuid.UUID) else uuid.UUID(str(run_id))
        context = (
            await db.execute(select(AssignmentRun.trigger_context).where(AssignmentRun.id == key))
        ).scalar_one_or_none()
    except Exception as exc:
        logger.debug("inspect_my_traces: run resumes unread: %s", format_exception_message(exc))
        return None
    return len(resumes_of(context if isinstance(context, dict) else None))


async def _run_frictions(db: AsyncSession, root: dict) -> dict | None:
    """The calling assignment run's friction ledger summary, or ``None`` (none, a chat turn, unread)."""
    run_id = root.get("local_assignment_run_id")
    if not run_id:
        return None
    from app.services.frictions import run_friction_summary

    return await run_friction_summary(db, run_id)


async def _annotate_frictions(db: AsyncSession, traces: list[dict], trace_rows: list) -> None:
    """Add ``rollup.frictions`` to each listed assignment run that recorded a friction.

    One grouped read of the ledger for the whole page; a chat turn, a run
    with no friction or an unreadable ledger is left unchanged. Every segment
    of a resumed run carries the run's figures (the ledger is per run).
    """
    run_ids = {
        str(item["id"]): getattr(row[0], "assignment_run_id", None)
        for item, row in zip(traces, trace_rows, strict=True)
    }
    wanted = [run_id for run_id in run_ids.values() if run_id is not None]
    if not wanted:
        return
    from app.services.frictions import run_friction_summaries

    summaries = await run_friction_summaries(db, wanted)
    for item in traces:
        summary = summaries.get(str(run_ids.get(str(item["id"]))))
        if summary:
            rollup = item.get("rollup")
            item["rollup"] = {**(rollup if isinstance(rollup, dict) else {}), "frictions": summary}


def _live_run_summary(
    root: dict,
    *,
    include_spans: bool,
    earlier: list[dict] | None = None,
    recorded_resumes: int | None = None,
    frictions: dict | None = None,
) -> dict:
    """The calling run as a listing item: what its root context holds now, ``in_progress``.

    Its rollup is computed from the spans ended so far, with the function the
    close stores, so the figures read the same once the run is listed. A run
    resumed after a worker stop adds the stored rollups of its *earlier*
    segments: the figures are the run's totals, ``resumes`` says how many.
    *recorded_resumes* (the run's own record) wins over the stored traces when
    it says more: ``untraced_segments`` names the segments lost with their
    worker, whose figures are missing from the totals.
    """
    started_at = root.get("local_started_at")
    now = datetime.now(UTC)
    wall_ms = (now - started_at).total_seconds() * 1000.0 if isinstance(started_at, datetime) else None
    spans = root.get("local_spans")
    rollup = rollup_from_spans(list(spans) if isinstance(spans, list) else [], wall_ms=wall_ms)
    untraced = 0
    if earlier or recorded_resumes:
        rollup = merge_segment_rollups(rollup, earlier or [])
        if recorded_resumes is not None and recorded_resumes > rollup["resumes"]:
            untraced = recorded_resumes - rollup["resumes"]
            rollup["resumes"] = recorded_resumes
            rollup["segments"] = recorded_resumes + 1
            rollup["untraced_segments"] = untraced
    if frictions:
        # The run's friction ledger (services/frictions.py): what it met so
        # far and how much of it has no disposition yet.
        rollup["frictions"] = frictions
    resumes = rollup.get("resumes") or 0
    if not include_spans:
        rollup = {key: value for key, value in rollup.items() if key != "tools"}
    agent = root.get("local_agent_id")
    run_id = root.get("local_assignment_run_id")
    assignment_id = root.get("local_assignment_id")
    return {
        "id": str(root.get("local_trace_id")),
        # The run's own ids beside its trace id: get_run reads run_id.
        "run_id": str(run_id) if run_id else None,
        "assignment_id": str(assignment_id) if assignment_id else None,
        "agent_id": str(agent) if agent else None,
        "source": root.get("local_source"),
        "name": root.get("local_name"),
        "model": root.get("local_model"),
        "status": "in_progress",
        "in_progress": True,
        "started_at": started_at.isoformat() if isinstance(started_at, datetime) else None,
        "ended_at": None,
        "rollup": rollup,
        "note": (
            "This run, still in progress: figures from the steps ended so far"
            + (
                f", summed over its {resumes + 1 - untraced} segments with a stored trace "
                f"(resumed {resumes} time(s))"
                if resumes
                else ""
            )
            + (
                f"; {untraced} earlier segment(s) were lost with their worker and left no trace, "
                "so their figures are missing from these totals"
                if untraced
                else ""
            )
            + ". Its trace is stored when the run ends; its payloads are readable then."
        ),
    }


async def _fetch_traces(
    db: AsyncSession,
    *,
    user: User,
    user_group_ids: list[str],
    query: str,
    period: str,
    status_filter: str,
    source: str,
    model: str,
    limit: int,
    agent_ids: list[str] | None,
    include_spans: bool,
    own_run: dict | None = None,
    max_spans: object = None,
    span_offset: object = None,
    rollup_only: bool = False,
    only_trace: Any = None,
    run_note: str = "",
    started_from: object = None,
    started_to: object = None,
    min_generations: object = None,
    max_generations: object = None,
    streamed_only: object = False,
    offset: object = 0,
    span_tool: object = "",
    span_match: object = "",
) -> str:
    date_from, period_err = _parse_period(period)
    if period_err:
        return period_err
    window_from, window_err = _parse_instant(started_from, "started_from")
    if window_err:
        return window_err
    window_to, window_err = _parse_instant(started_to, "started_to")
    if window_err:
        return window_err
    if window_from is not None:
        # An explicit window replaces the period bound.
        date_from = window_from
    date_to = window_to or datetime.now(UTC)
    if date_from is not None and date_to < date_from:
        return "Error: started_to is before started_from."
    first_row = _optional_count(offset) or 0
    keep = _rollup_filter(min_generations, max_generations, streamed_only)
    scan_capped = False

    # The rollup alone: every figure the span-inclusive mode computes (the
    # per-tool table included), no span list and no payload preview. A listener
    # reading twenty numbers paid the whole span list for them.
    rollup_only = rollup_only is True
    # A span search answers the matching tool spans alone: no rollup, no
    # preview of the trace, nothing repeated when its matches are paged.
    search = _span_search(span_tool, span_match)
    if search is not None:
        rollup_only = False
        include_spans = True
    list_spans = include_spans and not rollup_only
    include_spans = include_spans or rollup_only
    span_count, first_span = _span_window(max_spans, span_offset)
    wanted = _safe_limit(limit, include_spans=include_spans)
    # The calling run comes first and counts in the limit: limit=1 is this run.
    listed_limit = wanted - 1 if own_run is not None else wanted
    own_id = str(own_run.get("local_trace_id")) if own_run is not None else None
    trace_store = build_trace_read_store()
    try:
        if only_trace is not None:
            # One trace named by id, or the traces of one run (already read and
            # access-checked by the caller): no listing, the same rollup and span page.
            named = only_trace if isinstance(only_trace, list) else [only_trace]
            trace_rows, total = [(trace,) for trace in named], len(named)
        else:
            list_kwargs: dict[str, Any] = {
                "db": db,
                "user": user,
                "user_group_ids": user_group_ids,
                "search": (query or "").strip() or None,
                "conversation_id": None,
                "source": [source.strip()] if (source or "").strip() else None,
                "agent_id": agent_ids,
                "model": [model.strip()] if (model or "").strip() else None,
                "status_filter": (status_filter or "").strip() or None,
                "level": None,
                "date_from": date_from,
                "date_to": date_to,
                "cursor": None,
                "cursor_id": None,
            }
            if keep is not None:
                # The rollup is in the metadata: filter the rows read, newest first.
                trace_rows, total, scan_capped = await _scan_rollup_filtered(
                    trace_store,
                    list_kwargs,
                    keep,
                    first=first_row,
                    count=max(0, listed_limit),
                    exclude_id=own_id,
                )
            else:
                if first_row:
                    list_kwargs["offset"] = first_row
                trace_rows, total = await trace_store.list_traces(
                    **list_kwargs,
                    # One row more than shown, so the calling run's row (once stored) never shortens the page.
                    limit=max(1, listed_limit + (1 if own_run is not None else 0)),
                )
    except Exception as exc:
        logger.error("trace tool list_traces failed: %s", format_exception_message(exc), exc_info=True)
        return "Error: unable to read traces right now."

    if own_run is not None:
        trace_rows = [row for row in trace_rows if str(row[0].id) != own_id][: max(0, listed_limit)]

    # One budget for the whole response: previews compete with each other, not
    # with a per-field cap that multiplies by the number of spans.
    budget = _PreviewBudget()
    # The spans of every trace of the answer share one size budget, as the
    # previews share theirs.
    listing_budget = [_SPAN_LISTING_BUDGET_CHARS]
    traces = [
        _trace_summary(row[0], budget=budget if list_spans and search is None else None)
        for row in trace_rows
    ]
    if search is not None:
        for item in traces:
            item.pop("rollup", None)
    await _annotate_agents(db, traces, trace_rows)

    if include_spans and traces:
        for row in trace_rows:
            trace = row[0]
            spans_read = True
            try:
                spans = await trace_store.get_trace_spans(
                    db=db,
                    user=user,
                    user_group_ids=user_group_ids,
                    trace_id=trace.id,
                )
            except Exception as exc:
                logger.warning(
                    "trace tool get_trace_spans failed for trace %s: %s",
                    trace.id,
                    format_exception_message(exc),
                )
                spans = []
                spans_read = False
            for item in traces:
                if search is not None and item["id"] == str(trace.id):
                    item.update(_match_page(spans, search, count=span_count, first=first_span))
                    if not spans_read:
                        item["spans_note"] = "The spans of this trace could not be read."
                    break
                if item["id"] == str(trace.id):
                    stored = stored_rollup(getattr(trace, "metadata_json", None))
                    if not spans_read and stored is not None:
                        # A failed span read is not an empty run: keep what
                        # the run stored when it closed.
                        item["rollup"] = stored
                        if list_spans:
                            item["spans"] = []
                        break
                    item["rollup"] = _run_rollup(trace, spans)
                    if stored is not None:
                        # Timings exist only in the stored rollup: keep them.
                        item["rollup"] = {**stored, **item["rollup"]}
                    if list_spans:
                        item.update(
                            _span_page(
                                spans,
                                budget,
                                count=span_count,
                                first=first_span,
                                listing_budget=listing_budget,
                            )
                        )
                    break

    # The cost is the trace's own, priced by the sink after the close. The
    # calling run is left out: its figures are partial until it ends.
    stored_rollups = [
        {**item["rollup"], "total_cost": item.get("total_cost")}
        for item in traces
        if isinstance(item.get("rollup"), dict)
    ]
    if search is None:
        await _annotate_frictions(db, traces, trace_rows)
    if own_run is not None:
        earlier = await _earlier_segment_rollups(
            db, trace_store, user=user, user_group_ids=user_group_ids, root=own_run
        )
        recorded = await _recorded_resumes(db, own_run)
        frictions = await _run_frictions(db, own_run)
        traces.insert(
            0,
            _live_run_summary(
                own_run,
                include_spans=include_spans,
                earlier=earlier,
                recorded_resumes=recorded,
                frictions=frictions,
            ),
        )
    payload: dict[str, Any] = {
        "count": len(traces),
        "total": int(total),
        "period": period,
        "include_spans": list_spans,
        "traces": traces,
    }
    if window_from is not None or window_to is not None:
        payload["started_from"] = date_from.isoformat() if date_from else None
        payload["started_to"] = date_to.isoformat()
    if run_note:
        payload["run_note"] = run_note
    listed = len(trace_rows)
    if only_trace is None and first_row + listed < int(total):
        payload["next_offset"] = first_row + listed
    if keep is not None:
        payload["filtered_on_rollup"] = True
        if scan_capped:
            payload["filter_note"] = (
                f"The rollup filter read the newest {_ROLLUP_FILTER_SCAN_MAX} traces of the window only: "
                "narrow started_from / started_to to reach older ones."
            )
    if rollup_only:
        payload["rollup_only"] = True
    if search is not None:
        payload["span_search"] = {"tool": search[0] or None, "match": search[1] or None}
    if stored_rollups:
        # p50 and p90 over the runs listed, never an average alone.
        payload["rollup_percentiles"] = rollup_percentiles(stored_rollups)
    if budget.truncated_any:
        # Said once, in the response rather than only in the tool description:
        # the preview is a pointer, and this is the pointer's target.
        note = (
            "Previews above are bounded. Every field named in a "
            "'payload_truncated' list is readable in full with "
            f"{TRACE_PAYLOAD_TOOL_NAME}(trace_id=..., span_id=..., field=...) — "
            "page through it with the returned 'next_offset'."
        )
        if budget.previews_omitted:
            # A null preview must not read as an empty payload: say how many
            # fields the budget could not show at all, so a span that looks
            # bare is understood as unshown rather than as having nothing.
            note += (
                f" The preview budget ran out before {budget.previews_omitted} "
                "field(s) with content could be shown at all: their preview is "
                "null while they are listed in 'payload_truncated', so read them "
                "directly rather than treating them as empty."
            )
        payload["payload_note"] = note
    switches_note = _trace_switches_note()
    if switches_note:
        payload["trace_store_note"] = switches_note
    return json.dumps(payload, ensure_ascii=False)


#: Rendered text of the most recently resolved *offloaded* payload, so walking
#: ``next_offset`` does not re-download the object once per page.  An offloaded
#: payload is by definition above the offload threshold and can be far larger,
#: and ``resolve_payload`` downloads all of it every call — so a 50 MB payload
#: read to the end used to cost one full download and one fresh render per page.
#:
#: One slot, deliberately.  Paging is sequential over a single payload, so a
#: single entry gives the walk a near-perfect hit rate while bounding what is
#: held in memory to one payload rather than a growing map of them.
_RESOLVED_CACHE_TTL_SECONDS = 120.0
#: Above this the render is not cached at all: a walk over something this large
#: re-downloads rather than pinning it in the worker for the whole TTL.
_RESOLVED_CACHE_MAX_CHARS = 8_000_000
_resolved_payload_cache: dict[str, Any] = {}


def _resolved_cache_key(
    trace_id: str, span_id: str | None, field: str, raw: dict, include_reasoning: bool = False
) -> str | None:
    """Identity of one offloaded payload's bytes, or ``None`` if unidentifiable.

    Includes the digest, so the key names the exact bytes rather than a row that
    could in principle be rewritten.
    """
    digest = raw.get("sha256") or raw.get("object_name")
    if not digest:
        return None
    return f"{trace_id}:{span_id or '-'}:{field}:{digest}:{'r' if include_reasoning else '-'}"


def _cached_render(key: str) -> str | None:
    if _resolved_payload_cache.get("key") != key:
        return None
    if _resolved_payload_cache.get("expires", 0.0) <= time.monotonic():
        _resolved_payload_cache.clear()
        return None
    return _resolved_payload_cache.get("text")


def _store_render(key: str, text: str) -> None:
    if len(text) > _RESOLVED_CACHE_MAX_CHARS:
        return
    _resolved_payload_cache.clear()
    _resolved_payload_cache.update(
        key=key,
        text=text,
        expires=time.monotonic() + _RESOLVED_CACHE_TTL_SECONDS,
    )


async def _payload_page(
    raw: Any,
    *,
    field: str,
    trace_id: str,
    span_id: str | None,
    offset: int,
    max_chars: int,
    include_reasoning: bool = False,
) -> str:
    """Return one page of a payload field, resolved to its complete value.

    Resolution first: an oversized payload lives in object storage and the row
    holds only a pointer (see ``tracing.payload_offload``), so reading the row
    would hand back the envelope's own excerpt while calling it the full value.

    The page is a slice of the *rendered text*, not of the structure, which is
    what makes ``next_offset`` able to walk a payload of any size without ever
    returning something the agent cannot parse the boundaries of.

    Only reached once the caller has been authorized for this trace, which is
    what makes the render cache safe to consult here.
    """
    envelope = is_offload_envelope(raw)
    unavailable = False
    error: str | None = None
    text: str | None = None

    if envelope and (raw.get("offload_failed") or not raw.get("object_name")):
        # Nothing to fetch — report the gap rather than passing the captured
        # excerpt off as the whole payload.
        unavailable = True
        error = str(raw.get("error") or "payload was not stored")
        text = _envelope_excerpt(raw, include_reasoning=include_reasoning)
    else:
        # Only offloaded payloads are worth caching: an inline one came back with
        # the row the caller had to read anyway, so there is no download to save.
        cache_key = (
            _resolved_cache_key(trace_id, span_id, field, raw, include_reasoning)
            if envelope
            else None
        )
        text = _cached_render(cache_key) if cache_key else None
        if text is None:
            try:
                value = await resolve_payload(raw)
            except Exception as exc:
                logger.warning(
                    "get_trace_payload could not read the stored payload for "
                    "trace %s field %s: %s",
                    trace_id,
                    field,
                    format_exception_message(exc),
                )
                return (
                    "Error: this payload is held in object storage and could not be "
                    "read right now. Retry, or report that object storage is unreachable."
                )
            text = _serialize_payload(value, include_reasoning=include_reasoning)
            if cache_key and text is not None:
                _store_render(cache_key, text)
    result: dict[str, Any] = {
        "trace_id": trace_id,
        "span_id": span_id,
        "field": field,
        "offloaded": envelope,
    }
    if envelope:
        result["size_bytes"] = raw.get("size_bytes")
    if unavailable:
        result["unavailable"] = True
        result["error"] = error

    if text is None:
        result["total_chars"] = 0
        result["content"] = None
        if field != "metadata" and not trace_switches().store_payloads:
            result["trace_store_note"] = _TRACE_PAYLOADS_OFF_NOTE
        return json.dumps(result, ensure_ascii=False)

    bounded_max = max(1_000, min(int(max_chars), _PAYLOAD_PAGE_MAX_CHARS))
    total = len(text)
    start = min(max(0, int(offset)), total)
    end = min(total, start + bounded_max)
    result.update(
        {
            "total_chars": total,
            "offset": start,
            "returned_chars": end - start,
            "next_offset": end if end < total else None,
            "content": text[start:end],
        }
    )
    return json.dumps(result, ensure_ascii=False)


def build_trace_tools(
    db: AsyncSession,
    user_id: str,
    agent_id: str | None,
    *,
    supervision_target_agent_ids: list[str] | None = None,
    allow_cross_agent: bool = False,
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """Build trace troubleshooting tools for the current agent context.

    ``agent_id`` is the *calling* agent, and may be ``None`` when no agent run
    sits behind the call — the ``/mcp/xtm`` server builds these tools for a plain
    authenticated user.  Only ``inspect_my_traces`` needs it, and it refuses
    rather than guessing; callers with no agent simply do not expose it.

    ``allow_cross_agent`` states whether this agent was granted the supervision
    surface (``inspect_agent_traces``), and gates the caller's *ambient* admin
    privilege in ``get_trace_payload``.  It defaults to ``False`` because that
    is the safe reading of an unknown grant: without it, an agent given only
    ``inspect_my_traces`` — whose listing is hard-scoped to its own
    ``agent_id`` even for an admin — could still read any trace payload in the
    instance while running in an admin's session, given a trace UUID from
    anywhere.  The per-agent allow-list is unaffected: that is explicit
    configuration naming specific targets, not ambient privilege.
    """

    # ``db`` can be in a long-lived transaction during loop execution; use
    # dedicated short sessions for tool calls.
    del db

    supervision_allowlist: set[str] = set()
    for value in supervision_target_agent_ids or []:
        if not value:
            continue
        try:
            supervision_allowlist.add(str(uuid.UUID(value)))
        except (TypeError, ValueError, AttributeError):
            logger.warning(
                "Ignoring invalid trace supervision target agent id: %r",
                value,
            )
            continue
    own_agent_id = _canonical_agent_id(agent_id)

    async def _readable_allowlist(_db: AsyncSession, user: User, group_ids: list[str]) -> set[str]:
        """The other agents of the allow-list whose traces this caller may read
        (:func:`agents_whose_traces_user_may_read`): the list says which agents
        this one may supervise, the caller's right to each one's chats decides."""
        others = supervision_allowlist - {own_agent_id}
        if not others:
            return set()
        return await agents_whose_traces_user_may_read(_db, user, others, group_ids)

    async def _supervised_agent_ids(_db: AsyncSession, user: User, group_ids: list[str]) -> list[str] | None:
        """The other agents whose runs this caller may look up by run id: the
        readable allow-list, or every agent (``None``) for an admin granted the
        supervision surface; ``[]`` for none."""
        if allow_cross_agent and agent_tool_admin_bypass(user):
            return None
        return sorted(await _readable_allowlist(_db, user, group_ids))

    async def _supervised_trace_of_run(
        _db: AsyncSession, user: User, group_ids: list[str], value: str
    ) -> tuple[uuid.UUID | None, str | None]:
        """The root trace of a supervised agent's run *value* names, or the
        refusal naming each trace of a resumed run; ``(None, None)`` without a
        grant, so no other agent's run is even searched."""
        if _health_refuses_other_agents():
            return None, None
        scope = await _supervised_agent_ids(_db, user, group_ids)
        if scope == []:
            return None, None
        candidates = await _supervised_run_root_traces(
            _db, value, viewer=_trace_viewer(user), group_ids=group_ids, agent_ids=scope
        )
        return _one_trace_of_run(value, candidates)

    async def _inspect_my_traces(
        query: str = "",
        period: str = "7d",
        status_filter: str = "",
        source: str = "",
        model: str = "",
        limit: int = _DEFAULT_LIMIT,
        include_spans: bool = True,
        max_spans: int = _DEFAULT_MAX_SPANS,
        span_offset: int = 0,
        rollup_only: bool = False,
        trace_id: str = "",
        started_from: str = "",
        started_to: str = "",
        min_generations: int | None = None,
        max_generations: int | None = None,
        streamed_only: bool = False,
        offset: int = 0,
        span_tool: str = "",
        span_match: str = "",
    ) -> str:
        if not user_id or user_id == "anonymous":
            return "Error: inspect_my_traces requires an authenticated user context."

        if not agent_id:
            return (
                "Error: inspect_my_traces is only available inside an agent run. "
                "Use inspect_agent_traces to inspect a named agent."
            )

        try:
            uuid.UUID(agent_id)
            async with async_session() as _db:
                user, group_ids = await _load_user_context(_db, user_id)
                if user is None:
                    return "Error: calling user not found."
                fetch_kwargs: dict[str, Any] = {}
                own_run = _own_live_run(agent_id)
                raw_trace_id = (trace_id or "").strip()
                if raw_trace_id:
                    if _names_own_run(own_run, raw_trace_id):
                        # This very run, by its trace id or its run id: not
                        # stored yet, its live summary (earlier segments folded
                        # in) is the answer.
                        limit = 1
                        fetch_kwargs["own_run"] = own_run
                    elif (
                        run_of := await _own_traces_of_run(
                            _db, raw_trace_id, user=user, group_ids=group_ids, own_agent_id=own_agent_id
                        )
                    )[0]:
                        # A run id of this agent: its trace, or every trace of
                        # a run resumed after a worker stop (one per attempt),
                        # oldest first, rather than a refusal.
                        fetch_kwargs["only_trace"], fetch_kwargs["run_note"] = run_of
                    else:
                        trace, refusal = await _own_trace_by_id(
                            _db, raw_trace_id, user=user, group_ids=group_ids, own_agent_id=own_agent_id
                        )
                        if trace is None:
                            return refusal
                        fetch_kwargs["only_trace"] = trace
                elif own_run is not None:
                    fetch_kwargs["own_run"] = own_run
                return await _fetch_traces(
                    _db,
                    user=_own_trace_reader(user),
                    user_group_ids=group_ids,
                    query=query,
                    period=period,
                    status_filter=status_filter,
                    source=source,
                    model=model,
                    limit=limit,
                    agent_ids=[agent_id],
                    include_spans=include_spans,
                    max_spans=max_spans,
                    span_offset=span_offset,
                    rollup_only=rollup_only,
                    started_from=started_from,
                    started_to=started_to,
                    min_generations=min_generations,
                    max_generations=max_generations,
                    streamed_only=streamed_only,
                    offset=offset,
                    span_tool=span_tool,
                    span_match=span_match,
                    **fetch_kwargs,
                )
        except Exception as exc:
            logger.error("inspect_my_traces failed: %s", format_exception_message(exc), exc_info=True)
            return "Error: failed to inspect this agent's traces."

    async def _inspect_agent_traces(
        target_agent_id: str = "",
        query: str = "",
        period: str = "7d",
        status_filter: str = "",
        source: str = "",
        model: str = "",
        limit: int = _DEFAULT_LIMIT,
        include_spans: bool = True,
        max_spans: int = _DEFAULT_MAX_SPANS,
        span_offset: int = 0,
        rollup_only: bool = False,
        trace_id: str = "",
        span_tool: str = "",
        span_match: str = "",
        started_from: str = "",
        started_to: str = "",
        offset: int = 0,
    ) -> str:
        if not user_id or user_id == "anonymous":
            return "Error: inspect_agent_traces requires an authenticated user context."

        raw_target = (target_agent_id or "").strip()
        target_uuid: str | None = None
        if raw_target:
            try:
                target_uuid = str(uuid.UUID(raw_target))
            except Exception:
                return "Error: target_agent_id must be a valid UUID."

        try:
            async with async_session() as _db:
                user, group_ids = await _load_user_context(_db, user_id)
                if user is None:
                    return "Error: calling user not found."

                try:
                    agent_ids = _supervision_scope(
                        admin_bypass=agent_tool_admin_bypass(user),
                        target_uuid=target_uuid,
                        supervision_allowlist=(
                            set()
                            if own_agent_id is not None and target_uuid == own_agent_id
                            else await _readable_allowlist(_db, user, group_ids)
                        ),
                        own_agent_id=own_agent_id,
                    )
                except SupervisionDeniedError as denied:
                    # A deleted agent supervises nobody's agent any more: its
                    # traces are read as the caller may see them (their own
                    # runs of it, an agent they owned), never wider.
                    if target_uuid is None or target_uuid not in await _agents_gone(_db, {target_uuid}):
                        return denied.message
                    agent_ids = [target_uuid]
                if agent_ids != [own_agent_id] and _health_refuses_other_agents():
                    return HEALTH_TRACES_REFUSED
                reader = (
                    _own_trace_reader(user)
                    if own_agent_id is not None and agent_ids == [own_agent_id]
                    else _trace_viewer(user)
                )
                fetch_kwargs: dict[str, Any] = {}
                if (trace_id or "").strip():
                    # One trace, or every root trace of one run, inside the
                    # same scope as the listing: found before the audit write,
                    # so a lookup that finds nothing leaves no row.
                    named, refusal = await _scoped_traces_by_id(
                        _db, trace_id, reader=reader, group_ids=group_ids, agent_ids=agent_ids
                    )
                    if not named:
                        return refusal
                    fetch_kwargs["only_trace"] = named
                    # On success the second slot is a cut run's note.
                    fetch_kwargs["run_note"] = refusal

                await _audit_trace_read(
                    _db,
                    user,
                    entity_id=target_uuid,
                    summary=(
                        f"Inspected traces for agent {target_uuid}"
                        if target_uuid
                        else "Inspected traces across agents"
                    ),
                    details={
                        "calling_agent_id": agent_id,
                        "target_agent_ids": agent_ids,
                    },
                )

                return await _fetch_traces(
                    _db,
                    user=reader,
                    user_group_ids=group_ids,
                    query=query,
                    period=period,
                    status_filter=status_filter,
                    source=source,
                    model=model,
                    limit=limit,
                    agent_ids=agent_ids,
                    include_spans=include_spans,
                    max_spans=max_spans,
                    span_offset=span_offset,
                    rollup_only=rollup_only,
                    span_tool=span_tool,
                    span_match=span_match,
                    # The same window as inspect_my_traces: it narrows the time, never the scope.
                    started_from=started_from,
                    started_to=started_to,
                    offset=offset,
                    **fetch_kwargs,
                )
        except Exception as exc:
            logger.error("inspect_agent_traces failed: %s", format_exception_message(exc), exc_info=True)
            return "Error: failed to inspect target agent traces."

    async def _get_trace_payload(
        trace_id: str,
        field: str = "output",
        span_id: str = "",
        offset: int = 0,
        max_chars: int = _PAYLOAD_PAGE_DEFAULT_CHARS,
        include_reasoning: bool = False,
    ) -> str:
        if not user_id or user_id == "anonymous":
            return f"Error: {TRACE_PAYLOAD_TOOL_NAME} requires an authenticated user context."

        checked_field = (field or "output").strip().lower()
        if checked_field not in _PAYLOAD_FIELDS:
            return f"Error: field must be one of {', '.join(_PAYLOAD_FIELDS)}."

        # An id prefix (8+ hexadecimal characters) of a trace, or of a span of
        # the trace, stands for the full id it starts: resolved below, inside
        # the traces the caller may read and the spans of the trace it reads.
        trace_prefix = id_prefix(trace_id)
        parsed_trace_id: uuid.UUID | None = None
        if trace_prefix is None:
            try:
                parsed_trace_id = uuid.UUID((trace_id or "").strip())
            except (TypeError, ValueError, AttributeError):
                return (
                    "Error: trace_id must be a valid UUID, or its first 8+ "
                    "hexadecimal characters."
                )

        # The synthetic trace-root span the trace UI shows as ``t-<trace_id>``
        # carries the trace's own payloads, so it resolves against the trace row
        # — accepted here so an id copied from the inspector just works.
        raw_span_id = (span_id or "").strip()
        if raw_span_id.startswith("t-") and (
            raw_span_id[2:] == str(parsed_trace_id) or raw_span_id[2:] == (trace_id or "").strip()
        ):
            raw_span_id = ""
        span_prefix = id_prefix(raw_span_id) if raw_span_id else None
        parsed_span_id: uuid.UUID | None = None
        if raw_span_id and span_prefix is None:
            try:
                parsed_span_id = uuid.UUID(raw_span_id)
            except (TypeError, ValueError, AttributeError):
                return (
                    "Error: span_id must be a valid UUID (or its first 8+ "
                    "hexadecimal characters), or omitted to read the "
                    "trace's own payload."
                )

        if checked_field in _SPAN_ONLY_FIELDS and parsed_span_id is None and span_prefix is None:
            return (
                f"Error: {checked_field} exists on a span, not on the trace — "
                "pass the span_id of the step you want."
            )

        # Coerced up here with every other argument, not down at the slice: a
        # model that sends ``offset: null`` would otherwise fail *after* the ACL
        # check, the span fetch and — for a cross-agent read — an audit write and
        # its commit, leaving an audit row for a read that never happened, plus
        # an ERROR-level stack trace, in place of a one-line argument error.
        try:
            checked_offset = max(0, int(offset if offset is not None else 0))
            checked_max_chars = int(
                max_chars if max_chars is not None else _PAYLOAD_PAGE_DEFAULT_CHARS
            )
        except (TypeError, ValueError):
            return "Error: offset and max_chars must be integers."

        try:
            async with async_session() as _db:
                user, group_ids = await _load_user_context(_db, user_id)
                if user is None:
                    return "Error: calling user not found."
                viewer = _trace_viewer(user)

                if trace_prefix is not None:
                    resolved, refusal = await _resolve_trace_prefix(
                        _db, trace_prefix, trace_id, viewer=viewer, group_ids=group_ids, own_agent_id=own_agent_id
                    )
                    if refusal:
                        return refusal
                    if resolved is None:
                        # Not a trace id: maybe the id of a run of this agent.
                        resolved, refusal = await _own_trace_of_run(_db, trace_id, own_agent_id=own_agent_id)
                        if refusal:
                            return refusal
                    if resolved is None:
                        # Or of a run of an agent this one supervises.
                        resolved, refusal = await _supervised_trace_of_run(_db, user, group_ids, trace_id)
                        if refusal:
                            return refusal
                    if resolved is None:
                        return "Error: trace not found, or not visible to you."
                    parsed_trace_id = resolved

                trace_store = build_trace_read_store()
                try:
                    trace, _, _, _ = await trace_store.get_trace_detail(
                        _db,
                        user=viewer,
                        user_group_ids=group_ids,
                        trace_id=parsed_trace_id,
                    )
                except HTTPException:
                    # Not visible to the user, but maybe a run of this very
                    # agent: its own traces are always readable. Anything else
                    # gets the one answer, "not found" and "not yours" alike,
                    # as on the HTTP API.
                    trace = None
                    if own_agent_id is not None:
                        own_reader = _own_trace_reader(user)
                        if trace_prefix is None:
                            # A full id that is no visible trace: maybe the id
                            # of a run of this agent, whose trace is read.
                            by_run, refusal = await _own_trace_of_run(
                                _db, trace_id, own_agent_id=own_agent_id
                            )
                            if refusal:
                                return refusal
                            if by_run is not None:
                                parsed_trace_id = by_run
                        try:
                            trace, _, _, _ = await trace_store.get_trace_detail(
                                _db,
                                user=own_reader,
                                user_group_ids=group_ids,
                                trace_id=parsed_trace_id,
                            )
                        except HTTPException:
                            trace = None
                        if trace is not None and _canonical_agent_id(trace.agent_id) != own_agent_id:
                            trace = None
                        if trace is not None:
                            viewer = own_reader
                    if trace is None and trace_prefix is None:
                        # Or the id of a run of an agent this one supervises,
                        # read as any trace id is: the checks below still apply.
                        by_supervised, refusal = await _supervised_trace_of_run(_db, user, group_ids, trace_id)
                        if refusal:
                            return refusal
                        if by_supervised is not None:
                            parsed_trace_id = by_supervised
                            try:
                                trace, _, _, _ = await trace_store.get_trace_detail(
                                    _db,
                                    user=viewer,
                                    user_group_ids=group_ids,
                                    trace_id=parsed_trace_id,
                                )
                            except HTTPException:
                                trace = None
                    if trace is None:
                        return "Error: trace not found, or not visible to you."

                # Own traces always.  Another agent's only through an explicit
                # grant: the per-agent supervision allow-list, or the caller's
                # admin privilege *when the supervision surface was granted*.
                # The admin branch is deliberately not ambient — this tool ships
                # with the self-only listing too, and that listing is hard-scoped
                # to this agent_id even for an admin, so honouring admin here
                # regardless would let a self-inspection-only agent read any
                # trace in the instance out of an admin's session.
                trace_agent_id = str(trace.agent_id) if trace.agent_id else None
                is_own_trace = trace_agent_id == agent_id or (
                    own_agent_id is not None and _canonical_agent_id(trace_agent_id) == own_agent_id
                )
                may_read_other_agent = allow_cross_agent and agent_tool_admin_bypass(user)
                if not is_own_trace and not may_read_other_agent:
                    may_read_other_agent = _canonical_agent_id(trace_agent_id) in await _readable_allowlist(
                        _db, user, group_ids
                    )
                if not is_own_trace and not may_read_other_agent and allow_cross_agent:
                    # A deleted agent's trace: the caller's run of it, or
                    # an agent they owned - never through an admin bypass.
                    may_read_other_agent = await _deleted_agent_trace_is_callers(_db, user.id, trace)
                if not is_own_trace and _health_refuses_other_agents():
                    return HEALTH_TRACES_REFUSED
                if not is_own_trace and not may_read_other_agent:
                    return (
                        "Error: you are not allowed to read payloads for this trace. "
                        "It belongs to another agent, and you are not configured to "
                        "supervise it."
                    )

                if span_prefix is not None:
                    # Only now, the trace's access checked: its spans alone.
                    from app.models.llm_trace import LLMSpan

                    candidates = await ids_by_prefix(
                        _db,
                        LLMSpan,
                        span_prefix,
                        [LLMSpan.trace_id == parsed_trace_id],
                    )
                    resolved_span, refusal = resolved_or_refused(raw_span_id, "spans of this trace", candidates)
                    if refusal:
                        return refusal
                    if not candidates:
                        return "Error: no span with that id in this trace."
                    parsed_span_id = uuid.UUID(str(resolved_span))

                source: Any = trace
                if parsed_span_id is not None:
                    try:
                        # One row, not the whole trace: span payloads are stored
                        # whole, so loading every span to serve one field would
                        # pull megabytes.
                        source = await trace_store.get_trace_span(
                            _db,
                            user=viewer,
                            user_group_ids=group_ids,
                            trace_id=parsed_trace_id,
                            span_id=parsed_span_id,
                        )
                    except HTTPException:
                        return "Error: no span with that id in this trace."

                if not is_own_trace:
                    await _audit_trace_read(
                        _db,
                        user,
                        entity_id=trace_agent_id or str(parsed_trace_id),
                        summary=(
                            f"Read {checked_field} payload of trace {parsed_trace_id}"
                        ),
                        details={
                            "calling_agent_id": agent_id,
                            "trace_id": str(parsed_trace_id),
                            "span_id": str(parsed_span_id) if parsed_span_id else None,
                            "field": checked_field,
                            "include_reasoning": include_reasoning is True,
                        },
                    )

                return await _payload_page(
                    _payload_field(source, checked_field),
                    field=checked_field,
                    trace_id=str(parsed_trace_id),
                    span_id=str(parsed_span_id) if parsed_span_id else None,
                    offset=checked_offset,
                    max_chars=checked_max_chars,
                    include_reasoning=include_reasoning is True,
                )
        except Exception as exc:
            logger.error(
                "%s failed: %s",
                TRACE_PAYLOAD_TOOL_NAME,
                format_exception_message(exc),
                exc_info=True,
            )
            return "Error: failed to read the trace payload."

    definitions = [
        ToolDefinition(
            name="inspect_my_traces",
            source="builtin:observability",
            # "inspect" is no verb the name classifier reads; the tool only reads traces.
            read_only=True,
            description=(
                "This agent's traces, newest first; in a run the first is that run, its "
                "rollup so far, counted in limit. Previews are bounded: "
                f"{TRACE_PAYLOAD_TOOL_NAME} reads one whole."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "period": {
                        "type": "string",
                        "enum": ["24h", "7d", "30d", "90d"],
                        "default": "7d",
                    },
                    "status_filter": {"type": "string", "enum": ["", "ok", "error"]},
                    "source": {"type": "string"},
                    "model": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_LIMIT, "default": _DEFAULT_LIMIT},
                    "include_spans": {
                        "type": "boolean",
                        "default": True,
                        "description": _INCLUDE_SPANS_DESCRIPTION,
                    },
                    **_SPAN_WINDOW_PROPERTIES,
                    "trace_id": {
                        "type": "string",
                        "description": "One trace (UUID, 8+ hex prefix or run id), however old.",
                    },
                    "started_from": {"type": "string", "description": "ISO; replaces period"},
                    "started_to": {"type": "string"},
                    "min_generations": {"type": "integer"},
                    "max_generations": {"type": "integer"},
                    "streamed_only": {"type": "boolean"},
                    "offset": {"type": "integer", "description": "next_offset"},
                },
                "additionalProperties": False,
            },
        ),
        ToolDefinition(
            name="inspect_agent_traces",
            source="builtin:observability",
            description=(
                "Traces of another agent when supervision is allowed (admin or "
                "allow-list); no target_agent_id lists every supervised agent's. "
                "Previews are bounded: "
                f"{TRACE_PAYLOAD_TOOL_NAME} reads one whole."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "target_agent_id": {
                        "type": "string",
                        "description": (
                            "Target agent UUID, a deleted agent's included. Omit to list "
                            "traces across every supervised agent (admins: the whole instance)."
                        ),
                    },
                    "query": {"type": "string"},
                    "period": {
                        "type": "string",
                        "enum": ["24h", "7d", "30d", "90d"],
                        "default": "7d",
                    },
                    "status_filter": {"type": "string", "enum": ["", "ok", "error"]},
                    "source": {"type": "string"},
                    "model": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_LIMIT, "default": _DEFAULT_LIMIT},
                    "include_spans": {
                        "type": "boolean",
                        "default": True,
                        "description": _INCLUDE_SPANS_DESCRIPTION,
                    },
                    **_SPAN_WINDOW_PROPERTIES,
                    "trace_id": {"type": "string", "description": "One trace (UUID, 8+ hex prefix) or run id."},
                    "started_from": {"type": "string"},
                    "started_to": {"type": "string"},
                    "offset": {"type": "integer"},
                },
                "additionalProperties": False,
            },
        ),
        ToolDefinition(
            name=TRACE_PAYLOAD_TOOL_NAME,
            # Comes with either trace function of the group.
            source="builtin:observability",
            description=(
                "Read one trace or span payload field in full, when a preview ended in "
                "'[truncated N chars]' or a step is in 'payload_truncated'. Pages carry "
                "'total_chars' and 'next_offset': offset=PREVIEW_LENGTH resumes the "
                "preview; page on until next_offset is null. span_id reads one step, "
                "none the trace's own payload. A span's 'output' is the model's answer, "
                "its 'input' the messages sent; reasoning reads '[reasoning omitted]' "
                "unless include_reasoning (page from 0). Read-only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "trace_id": {
                        "type": "string",
                        "description": "Trace UUID from inspect_*_traces, its 8+ hex prefix, or its run id",
                    },
                    "field": {
                        "type": "string",
                        "enum": list(_PAYLOAD_FIELDS),
                        "default": "output",
                        "description": ("Which payload to read. 'tool_arguments' requires span_id."),
                    },
                    "span_id": {
                        "type": "string",
                        "description": "Span UUID or 8+ hex prefix: one step of the run",
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 0,
                        "description": "Character offset to start from; use the previous call's next_offset.",
                    },
                    "max_chars": {
                        "type": "integer",
                        "minimum": 1000,
                        "maximum": _PAYLOAD_PAGE_MAX_CHARS,
                        "default": _PAYLOAD_PAGE_DEFAULT_CHARS,
                        "description": "Characters to return in this page.",
                    },
                    "include_reasoning": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Return the model's reasoning. Claude may decline a call carrying it."
                        ),
                    },
                },
                "required": ["trace_id"],
                "additionalProperties": False,
            },
        ),
    ]

    executor = CallableToolExecutor()
    executor.register("inspect_my_traces", _inspect_my_traces)
    executor.register("inspect_agent_traces", _inspect_agent_traces)
    executor.register(TRACE_PAYLOAD_TOOL_NAME, _get_trace_payload)
    return definitions, executor
