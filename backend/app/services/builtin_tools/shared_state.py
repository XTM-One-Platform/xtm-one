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

"""Built-in shared-state tools (lifecycle + purge).

Gated behind the ``shared_state`` built-in tool id, these let an agent read and
write a generic, fleet-scoped ``key -> value`` store so the fleet shares "the
state of things" across runs instead of cold-starting:

* ``get_shared_state`` - exact fetch of the value for a key (with its status).
* ``set_shared_state`` - create-or-merge the live value (free-form JSON bag) with
  an optional append-only note; writing marks the entry active. ``append_json`` /
  ``remove_json`` change a list held under a top-level key on the server,
  so an agent never reads and rewrites a list another run may be changing. A write answers
  with a compact read-back: the value whole when small, otherwise its
  top-level keys and sizes, and only the latest note; the agent already knows
  what it wrote, so the full entry stays behind ``get_shared_state``.
  ``hold`` holds the entry as a lease for the run the tool runs in:
  while another live run holds it the call writes nothing and answers a
  ``held`` result (holder, pending_fix), not an error, since standing down is
  the expected outcome; taken over once that run ended,
  released by the platform's tick when its holder ends; reads show the holder
  and whether it is alive.
* ``list_shared_state`` - enumerate the keys (active only by default).
* ``resolve_shared_state`` - mark an entry resolved once the thing it tracks
  concludes (drops out of active reads, kept briefly for audit, then purged).
* ``delete_shared_state`` - purge an entry the fleet no longer needs.

The owning agent is responsible for keeping its shared state tidy: resolve when
done, purge when useless. Shared state is NOT a write-only log - a retention
sweep eventually clears resolved entries as a backstop, but the agent owns the
cleanup.

A company-managed entry is read by every agent that sees it and written only by
a company-managed agent (``services/company_managed_writes.py``); a personal
agent's write of one is refused with the reason and writes nothing. The agent
that owns a company-managed entry may narrow which agents read and write it:
the reads never reach an entry this agent may not read and mark the ones it
may not change.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.core.utils import format_exception_message, normalize_correlation_key, safe_uuid
from app.services.company_managed_writes import (
    CompanyManagedKeyHiddenError,
    CompanyManagedWriteRefusedError,
    agent_writer,
)
from app.services.coordination_scope import SHARED_STATE_SCOPE_SCHEMA
from app.tools.base import (
    CallableToolExecutor,
    ToolDefinition,
    ToolResult,
    current_workspace,
    workspace_ingest_requested,
)

from ._shared import logger
from .paging import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT, answer_budget, page_bounds, page_line, rows_that_fit

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# The compact read-back of a write. A value whose JSON fits under
# _WRITE_VALUE_INLINE_CHARS is shown whole; a larger one by its top-level keys
# and their sizes. Only the latest note is shown, cut at _WRITE_NOTE_CHARS.
_WRITE_VALUE_INLINE_CHARS = 600
_WRITE_VALUE_KEYS_SHOWN = 20
_WRITE_NOTE_CHARS = 500
# A top-level key is shown JSON-escaped (quotes, newlines and control characters
# stay escaped, as in the full rendering) and cut at this length.
_WRITE_KEY_CHARS = 80

# The read of a value. The default read shows the value's JSON up to
# _READ_VALUE_CHARS, then says how much is left and how to read it: a window
# (offset / max_chars) or one top-level key whole (path). A windowed read
# answers the value or the key alone, without the notes, in windows of
# _READ_WINDOW_DEFAULT_CHARS, at most _READ_WINDOW_MAX_CHARS.
_READ_VALUE_CHARS = 2000
_READ_WINDOW_DEFAULT_CHARS = 20_000
_READ_WINDOW_MAX_CHARS = 50_000
#: Keys a ``PREFIX*`` read searches (gap-evidence-overflow-scatter), read by pages of
#: _FAMILY_PAGE_KEYS so a family past one page is searched whole (gap-lookup-across-split-keys).
_FAMILY_MAX_KEYS = 500
_FAMILY_PAGE_KEYS = 100
# The default read of a large object value: its top-level keys, a
# small value inline (its JSON up to _READ_KEY_INLINE_CHARS), a larger one by
# its size; at most _READ_KEYS_SHOWN keys. Then the latest _READ_NOTES notes.
_READ_KEY_INLINE_CHARS = 120
_READ_KEYS_SHOWN = 40
_READ_NOTES = 5
# How many keys deep a where_json read with no path looks for lists, through
# objects only (dev:pr-pools keeps its lists at pools.<pool>.<list>: three).
_WHERE_MAX_DEPTH = 4
# group_by with fields: the ids a group names before "+N more".
_GROUP_IDS_SHOWN = 20

# Characters quoted on each side of the position a JSON argument broke at.
_JSON_EXCERPT_RADIUS = 40

# set_shared_state(from_workspace_path): the arguments a workspace JSON file may
# carry (a large write then never passes through the context), and its ceiling.
_WORKSPACE_JSON_ARGUMENTS = (
    "value_json",
    "append_json",
    "remove_json",
    "keep_last_json",
    "unique_by_json",
    "update_json",
)
_WORKSPACE_JSON_MAX_BYTES = 4 * 1024 * 1024


def json_argument_error(name: str, raw: str, exc: json.JSONDecodeError) -> str:
    """The refusal of a JSON argument that does not parse: its name, where, and the text there.

    Each argument is parsed on its own, so the name is always the argument that
    failed. The excerpt (about 40 characters each side, the break marked with
    >>> <<<) lets the caller fix a long argument in one retry instead of
    rebuilding the whole call.
    """
    pos = max(0, min(exc.pos, len(raw)))
    start = max(0, pos - _JSON_EXCERPT_RADIUS)
    end = min(len(raw), pos + _JSON_EXCERPT_RADIUS)
    before = ("..." if start else "") + raw[start:pos]
    after = raw[pos:end] + ("..." if end < len(raw) else "")
    return (
        f"Error: {name} is not valid JSON ({exc.msg} at char {exc.pos} of {len(raw)}): "
        f"{before}>>>{after[:1]}<<<{after[1:]}. Nothing was written."
    )


# Replacement records a write answers per list (at most this many shown).
_REPLACED_SHOWN = 5


def _replaced_item_lines(list_key: str, rows: list[dict[str, Any]]) -> list[str]:
    """One line per item a unique_by append replaced: what the stored item lost or changed.

    A run that read no match, then appended, learns here that another run wrote
    that item between its read and its write (the fields it dropped are named).
    """
    lines: list[str] = []
    for row in rows[:_REPLACED_SHOWN]:
        ident = json.dumps(row.get("identity") or {}, default=str)[:_WRITE_KEY_CHARS]
        lost = ", ".join(row.get("lost") or []) or "none"
        changed = ", ".join(row.get("changed") or []) or "none"
        lines.append(
            f"    - replaced {ident}: fields dropped {lost[: _WRITE_KEY_CHARS * 2]}; "
            f"fields changed {changed[: _WRITE_KEY_CHARS * 2]}"
        )
    if len(rows) > _REPLACED_SHOWN:
        lines.append(
            f"    - and {len(rows) - _REPLACED_SHOWN} more replaced in {json.dumps(list_key)[:_WRITE_KEY_CHARS]}"
        )
    return lines


def _is_note_only(
    parsed: dict[str, dict | None],
    *,
    unset_keys: list[str] | None,
    hold: str | None,
    label: str | None,
    owner_agent_id: str | None,
    lease: bool | None,
    work_item_id: str | None,
    expires_at: str | None,
    terminal_condition: str | None,
    retention: dict | None,
    archive_key: str | None,
) -> bool:
    """True when a set_shared_state call writes a note (or releases a hold) and nothing live.

    Such a call keeps the entry's status: a closing note on a resolved lease
    (``dev:branch-lock:<branch>``) must not reactivate it, which left leases
    to be resolved twice. A value, a list change, an unset, a take or any
    lifecycle field is a live write and still marks the entry active.
    """
    if any(value is not None for value in parsed.values()):
        return False
    if unset_keys or (hold or "").strip() not in ("", "release"):
        return False
    if (label or "").strip() or (owner_agent_id or "").strip() or (archive_key or "").strip():
        return False
    if lease is not None or retention is not None:
        return False
    return all(field is None for field in (work_item_id, expires_at, terminal_condition))


def build_shared_state_tools(
    db: AsyncSession,
    user_id: str,
    agent_id: str,
    *,
    is_company_managed: bool = False,
    company_managed_agent: bool = False,
    key_namespace: str | None = None,
    # Set together with a `workspace:<id>` namespace. The namespace
    # isolates the KEY; this column is what the partial unique index governs,
    # so a namespaced write with a NULL column would land under the per-user
    # index instead and two people in one workspace would each get a row.
    workspace_id: uuid.UUID | None = None,
    # The run the tools run in: what ``hold="take"`` stamps on an
    # entry. Without one, the background task around the turn, if any.
    run_id: str | uuid.UUID | None = None,
    # The person the turn or run acts for is an administrator: the agent reads
    # past every owner agent's coordination narrowing (#2161).
    acting_admin: bool = False,
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """Build the generic cross-run shared-state tools for an agent.

    ``is_company_managed`` makes the entries the tools create company-managed;
    ``company_managed_agent`` says the acting agent is company-managed, which
    is what lets it change a company-managed entry (a personal agent only
    reads one). ``agent_id`` is the acting agent, the one the entries' owner
    agents admit or keep out.

    ``key_namespace`` scopes every key the agent uses (flow runs pass one so
    a run's — or a flow's — state is isolated instead of colliding on raw
    keys). The prefix is applied and stripped HERE, transparently: the agent
    reads and writes clean keys and never learns the namespace exists, so a
    prompt can neither forget it nor escape it.
    """
    del db  # operations use their own sessions
    acting_user_id = user_id if user_id and user_id != "anonymous" else None
    writer = agent_writer(is_company_managed=company_managed_agent, agent_id=agent_id, acting_admin=acting_admin)
    # One entry per key for a company-managed agent, whatever the context:
    # a run whose assignment hides its runs is born personal
    # (``run.is_company_managed`` false), and creating the entry in that
    # run's personal scope would split one key into two rows the chat and
    # the run each keep reading. The scope of a new entry follows the agent.
    is_company_managed = bool(is_company_managed or company_managed_agent)
    _prefix = f"{key_namespace.strip()}:" if key_namespace and key_namespace.strip() else ""

    def _refused(exc: CompanyManagedWriteRefusedError, key: str) -> str:
        from app.services.shared_state import WorkspaceWriteRefusedError

        if isinstance(exc, CompanyManagedKeyHiddenError):
            return f"Error: {exc}, so nothing was written. Use another key."
        if isinstance(exc, WorkspaceWriteRefusedError):
            return f"Error: {exc}, so nothing was written."
        return (
            f"Error: {exc}. The entry `{key.strip()}` is shared by the fleet: "
            "this agent can read it but not change it, so nothing was written."
        )

    def _read_only(state: dict) -> bool:
        if "read_only" in state:
            return bool(state["read_only"])
        return bool(state.get("is_company_managed")) and not writer.may_write

    def _ns(key: str) -> str:
        return f"{_prefix}{key.strip()}"

    def _strip_ns(state: dict) -> dict:
        if _prefix and isinstance(state.get("key"), str):
            state = {**state, "key": state["key"].removeprefix(_prefix)}
        return state

    def _format_state(state: dict) -> str:
        status = state.get("status") or "active"
        status_label = status + (" [STALE]" if state.get("is_stale") else "")
        lines = [
            f"State **{state.get('label') or state['key']}** "
            f"(key: `{state['key']}`, status: {status_label})"
        ]
        if _read_only(state):
            lines.append("  - company-managed: read-only for this agent")
        lines.append(f"  - scope: {_scope_words(state)}")
        if state.get("owner_agent_id"):
            lines.append(f"  - owner agent: {state['owner_agent_id']}")
        lines.extend(_holder_lines(state))
        if state.get("resolved_at"):
            lines.append(f"  - resolved at: {state['resolved_at']}")
        lines.extend(_usage_lines(state))
        value = state.get("value")
        if value:
            dumped = json.dumps(value, default=str)
            if len(dumped) <= _READ_VALUE_CHARS:
                lines.append(f"  - value: {dumped}")
            elif isinstance(value, dict):
                # A large object: its keys, the small ones inline, the
                # others by size; `path` reads one whole, offset a window.
                lines.append(
                    f"  - value: {len(dumped)} chars, {len(value)} top-level key(s) "
                    "(small ones inline; `path` reads one key whole, `offset` / `max_chars` a window of the JSON):"
                )
                # The keys and small values are data another writer chose:
                # one quarantine block, as for a windowed read; the count of
                # keys not shown and how to read them stay outside it.
                from app.services.content_safety import wrap_untrusted

                lines.append(
                    wrap_untrusted(
                        "\n".join(_key_view(value)),
                        source=f"shared state value of {state['key']}",
                    )
                )
                if len(value) > _READ_KEYS_SHOWN:
                    lines.append(
                        f"    +{len(value) - _READ_KEYS_SHOWN} more key(s): offset=0 reads the JSON by window"
                    )
            else:
                lines.append(f"  - value: {dumped[:_READ_VALUE_CHARS]}")
                lines.append("    " + _cut_marker(value, dumped, _READ_VALUE_CHARS, path=""))
        notes = state.get("notes") or []
        if notes:
            shown = notes[-_READ_NOTES:]
            older = len(notes) - len(shown)
            more = f", the latest {len(shown)}; {older} older not shown" if older else ""
            lines.append(f"  - notes ({len(notes)}{more}, latest last):")
            for entry in shown:
                lines.append(f"    - [{entry.get('at', '')}] {entry.get('note', '')}")
        return "\n".join(lines)

    def _usage_lines(state: dict[str, Any]) -> list[str]:
        """The retention policy, the size against its cap and the 80% warning (#12035)."""
        usage = state.get("retention_usage")
        if not usage:
            return []
        cap = f" of {usage['max_chars']} ({usage['percent']}%)" if usage.get("max_chars") else ""
        lines = [f"  - retention usage: {usage['size_chars']} chars{cap}"]
        if usage.get("warning"):
            lines.append(f"  - RETENTION WARNING: {usage['warning']}")
        return lines

    def _scope_words(state: dict[str, object]) -> str:
        """The entry's scope and owner user, as the reads and the listing show them."""
        user = state.get("user_id")
        if state.get("workspace_id"):
            scope = f"workspace {state['workspace_id']}"
        elif state.get("is_company_managed"):
            scope = "company-managed"
        else:
            scope = "personal"
        return f"{scope}, owner user {user}" if user else scope

    def _holder_lines(state: dict) -> list[str]:
        """The run holding the entry and whether it lives, when held."""
        holder = state.get("held_by_run")
        if not state.get("held_by_run_id"):
            return []
        if not holder:
            return [f"  - held by run {state['held_by_run_id']}"]
        status = holder.get("status") or "no longer recorded"
        if holder.get("alive"):
            return [f"  - held by run {holder['id']} ({status}, alive)"]
        return [
            f"  - held by run {holder['id']}, which ENDED ({status}): "
            "hold='take' takes the entry over; the platform also releases it on its next tick"
        ]

    async def _with_holder(state: dict) -> dict:
        """*state* with ``held_by_run`` read live: id, kind, status, alive."""
        rid = safe_uuid(state.get("held_by_run_id") or "")
        if rid is None:
            return state
        try:
            from app.core.database import async_session
            from app.services.work_item import holding_runs

            async with async_session() as session:
                holder = (await holding_runs(session, [rid])).get(rid)
        except Exception as exc:  # the read stays useful without the holder's state
            logger.warning("Shared-state holder not read: %s", format_exception_message(exc))
            return state
        if holder is None:
            return state
        return {
            **state,
            "held_by_run": {
                "id": str(rid),
                "kind": holder.kind,
                "status": holder.status,
                "alive": holder.alive,
            },
        }

    def _key_view(value: dict) -> list[str]:
        """One line per top-level key: a small value inline, a larger one by its size."""
        out: list[str] = []
        keys = list(value)
        for k in keys[:_READ_KEYS_SHOWN]:
            item = json.dumps(value[k], default=str)
            if len(item) <= _READ_KEY_INLINE_CHARS:
                out.append(f"    {_shown_key(k)}: {item}")
            else:
                kind = f"list of {len(value[k])}" if isinstance(value[k], list) else type(value[k]).__name__
                out.append(f"    {_shown_key(k)} ({kind}, {len(item)} chars)")
        return out

    def _shown_key(key: object) -> str:
        """A value key as the agent reads it: JSON-escaped, never raw, bounded.

        A key is data another writer chose: printed raw, a newline or an
        instruction-shaped key would read as its own line of the tool answer.
        """
        escaped = json.dumps(str(key))
        if len(escaped) > _WRITE_KEY_CHARS:
            escaped = escaped[: _WRITE_KEY_CHARS - 4] + '..."'
        return escaped

    def _key_sizes(value: dict) -> str:
        keys = list(value)
        sizes = ", ".join(
            f"{_shown_key(k)} ({len(json.dumps(value[k], default=str))} chars)"
            for k in keys[:_WRITE_VALUE_KEYS_SHOWN]
        )
        more = len(keys) - _WRITE_VALUE_KEYS_SHOWN
        if more > 0:
            sizes += f", +{more} more"
        return sizes

    def _cut_marker(value: object, dumped: str, end: int, *, path: str) -> str:
        """Say where a value's JSON stopped and how to read the rest of it."""
        where = f"{_shown_key(path)} " if path else ""
        marker = (
            f"[value {where}cut at character {end} of {len(dumped)}: "
            f"{len(dumped) - end} more; get_shared_state with offset={end} reads on"
        )
        if not path and isinstance(value, dict):
            marker += f", or `path` set to one of these top-level keys reads it whole: {_key_sizes(value)}"
        return marker + "]"

    def _shape_items(items: list, shape: dict | None) -> tuple[list, str | None]:
        """Keep only ``shape['fields']`` of each object item, then the item page asked for.

        Answers the items and the line naming the page (items N to M of T, the
        next item_offset), or None when no page was asked. An item page counts
        items, not characters, so it never cuts an item and stays put while
        other runs append at the end of the list.
        """
        if not shape:
            return items, None
        fields = shape.get("fields")
        if fields:
            items = [
                {f: item[f] for f in fields if f in item} if isinstance(item, dict) else item
                for item in items
            ]
        start, limit = shape.get("item_offset"), shape.get("item_limit")
        if start is None and limit is None:
            return items, None
        start = start or 0
        total = len(items)
        end = total if limit is None else min(total, start + limit)
        line = f"  - items {min(start, total)} to {end} of {total}"
        if end < total:
            line += f"; item_offset={end} reads the next page"
        return items[start:end], line

    def _format_window(
        state: dict,
        path: str,
        offset: int,
        max_chars: int,
        where: list | None = None,
        shape: dict | None = None,
    ) -> str:
        """One window of the value's JSON, or of one top-level key's.

        A long-lived entry's list (an open-PR list, a register) cut at a fixed
        length cannot be rewritten whole by the agent that reads it; this read
        returns the part asked for, exactly, and says where it stops. The part
        is data another writer chose (it may carry text a run copied from an
        external source): it is returned inside an untrusted-content block, the
        header and the continuation marker outside it.
        """
        from app.services.content_safety import wrap_untrusted

        status = state.get("status") or "active"
        status_label = status + (" [STALE]" if state.get("is_stale") else "")
        lines = [
            f"State **{state.get('label') or state['key']}** "
            f"(key: `{state['key']}`, status: {status_label})"
        ]
        if _read_only(state):
            lines.append("  - company-managed: read-only for this agent")
        lines.append(f"  - scope: {_scope_words(state)}")
        value = state.get("value") or {}
        part, missing = _resolve_path(value, path)
        if where is not None and (not path or missing is not None):
            # The caller filters without knowing the layout (no path, or a
            # guessed one): the match runs over every list of the value, so a
            # wrong guess costs no second call.
            return _format_where_anywhere(state, value, path, offset, max_chars, where, lines, shape=shape)
        if path:
            if missing is not None:
                return f"Error: the value of `{state['key']}` {missing}."
            label = _path_label(value, path)
            if where is not None:
                # Only the items carrying the fields asked for, by the
                # match rule of remove_json, so a read selects what a remove drops.
                from app.services.shared_state import list_item_matches

                if isinstance(part, dict):
                    # An object path: every list directly under it is searched.
                    return _format_where_anywhere(
                        state, value, path, offset, max_chars, where, lines, under=part, shape=shape
                    )
                if not isinstance(part, list):
                    return (
                        f"Error: where_json filters a list, and {label} of "
                        f"`{state['key']}` is a {type(part).__name__}."
                    )
                total = len(part)
                part = [item for item in part if any(list_item_matches(item, m) for m in where)]
                lines.append(f"  - where_json: {len(part)} of {total} item(s) match")
                if not part:
                    return "\n".join(lines)
                label += " matching where_json"
            if shape:
                if not isinstance(part, list):
                    return (
                        f"Error: fields, item_offset and item_limit apply to a list, and {label} "
                        f"of `{state['key']}` is a {type(part).__name__}."
                    )
                part, page = _shape_items(part, shape)
                if page:
                    lines.append(page)
        else:
            if shape:
                return "Error: fields, item_offset and item_limit need `path` (a list) or `where_json`."
            part = value
            label = "value"
        dumped = json.dumps(part, default=str)
        if offset > len(dumped):
            return (
                f"Error: offset {offset} is past the end of the {label} "
                f"({len(dumped)} chars), so nothing was read."
            )
        end = min(len(dumped), offset + max_chars)
        lines.append(f"  - {label}, characters {offset} to {end} of {len(dumped)}:")
        lines.append(
            wrap_untrusted(dumped[offset:end], source=f"shared state value of {state['key']}")
        )
        if end < len(dumped):
            lines.append("    " + _cut_marker(part, dumped, end, path=path))
        return "\n".join(lines)

    def _group_value(item: object, field: str) -> tuple[bool, object]:
        """The value of a (dotted) field of a list item, and whether it is there."""
        current = item
        if isinstance(current, dict) and field in current:
            return True, current[field]
        for segment in field.split("."):
            if not isinstance(current, dict) or segment not in current:
                return False, None
            current = current[segment]
        return True, current

    def _group_items(
        state: dict[str, Any],
        path: str,
        where: list[dict[str, Any]] | None,
        group_by: str,
        fields: list[str] | None,
    ) -> tuple[str, list[dict[str, Any]] | None]:
        """Counts of a list's items per value of one field: (header, rows) or (error, None).

        where_json filters first; items without the field count in a row of
        their own (``missing: true``). With ``fields``, each row also names the
        first field's value of its items, at most ``_GROUP_IDS_SHOWN``.
        """
        from app.services.shared_state import list_item_matches

        value = state.get("value") or {}
        part, missing = _resolve_path(value, path)
        if missing is not None:
            return f"Error: the value of `{state['key']}` {missing}.", None
        label = _path_label(value, path)
        if not isinstance(part, list):
            return f"Error: group_by counts a list, and {label} of `{state['key']}` is a {type(part).__name__}.", None
        total = len(part)
        if where is not None:
            part = [item for item in part if any(list_item_matches(item, m) for m in where)]
        counts: dict[str, dict[str, Any]] = {}
        for item in part:
            present, found = _group_value(item, group_by)
            bucket = json.dumps(found, default=str, sort_keys=True) if present else "\x00missing"
            row = counts.setdefault(bucket, {"value": found, "count": 0} if present else {"missing": True, "count": 0})
            row["count"] += 1
            if fields:
                has_id, ident = _group_value(item, fields[0])
                ids = row.setdefault("ids", [])
                if has_id and len(ids) < _GROUP_IDS_SHOWN:
                    ids.append(ident)
        rows = sorted(counts.values(), key=lambda r: (-r["count"], "missing" in r, str(r.get("value"))))
        read_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        scope = f"{len(part)} of {total} item(s) matching where_json" if where is not None else f"{total} item(s)"
        header = f"  - group_by {group_by!r} over {label}: {scope}, {len(rows)} group(s), read {read_at}"
        return header, rows

    def _format_groups(state: dict[str, Any], header: str, rows: list[dict[str, Any]]) -> str:
        """One line per group, inside the untrusted block (the values are data)."""
        from app.services.content_safety import wrap_untrusted

        lines = [
            f"State **{state.get('label') or state['key']}** (key: `{state['key']}`, "
            f"status: {state.get('status') or 'active'})",
            header,
        ]
        body = []
        for row in rows:
            name = "(missing)" if row.get("missing") else json.dumps(row["value"], default=str, ensure_ascii=False)
            text = f"{name}: {row['count']}"
            if "ids" in row:
                shown = ", ".join(json.dumps(i, default=str, ensure_ascii=False) for i in row["ids"])
                more = row["count"] - len(row["ids"])
                text += f" [{shown}{f', +{more} more' if more > 0 else ''}]"
            body.append(text)
        if body:
            lines.append(wrap_untrusted("\n".join(body), source=f"shared state value of {state['key']}"))
        return "\n".join(lines)

    def _export_selection(
        state: dict[str, object], path: str, where: list[dict[str, object]] | None, shape: dict[str, object] | None
    ) -> str:
        """The part of the value a read selects, whole, as JSON, for ``to_workspace_path``.

        The same selection as the windowed read (``path``, ``where_json``,
        ``fields`` / ``item_offset`` / ``item_limit``) without the window: a
        value far larger than one answer lands in the workspace in one call
        and is analysed in code, never paged through the context. A layout-free
        ``where_json`` gives the matches grouped by list path, as the read does.
        """
        from app.services.shared_state import list_item_matches

        value = state.get("value") or {}
        part, missing = _resolve_path(value, path)
        if where is not None and (not path or missing is not None or isinstance(part, dict)):
            under = part if path and missing is None else value
            prefix = path if path and missing is None else ""
            groups: list[dict[str, object]] = []
            for depth in _where_candidates(under):
                for list_path, items in depth:
                    found = [i for i in items if any(list_item_matches(i, m) for m in where)]
                    if found:
                        if shape:
                            found, _page = _shape_items(found, {"fields": shape.get("fields")})
                        groups.append({"path": f"{prefix}.{list_path}" if prefix else list_path, "matches": found})
                if groups:
                    break
            return json.dumps(groups, default=str, ensure_ascii=False)
        if path and missing is not None:
            return f"Error: the value of `{state['key']}` {missing}."
        if where is not None:
            if not isinstance(part, list):
                return f"Error: where_json filters a list, and {_path_label(value, path)} is a {type(part).__name__}."
            part = [item for item in part if any(list_item_matches(item, m) for m in where)]
        if shape:
            if not isinstance(part, list):
                return "Error: fields, item_offset and item_limit apply to a list."
            part, _page = _shape_items(part, shape)
        return json.dumps(part, default=str, ensure_ascii=False)

    def _selected_items(state: dict[str, object], path: str, where: list[dict[str, object]]) -> list[object] | None:
        """The list items a where_json read selects, flat; ``None`` when it selects no list."""
        exported = _export_selection(state, path, where, None)
        if exported.startswith("Error:"):
            return None
        selected = json.loads(exported)
        if not isinstance(selected, list):
            return None
        part, missing = _resolve_path(state.get("value") or {}, path)
        if path and missing is None and isinstance(part, list):
            return selected
        # A layout-free read answers the matches grouped by list path.
        return [item for group in selected for item in group["matches"]]

    def _resolve_path(value: object, path: str) -> tuple[object, str | None]:
        """The part of a value a read `path` names, or why there is none.

        A top-level key is looked up first, whole, so a key that itself holds
        a dot reads as it always did; otherwise a dotted path is walked into
        nested objects segment by segment, the way set_shared_state writes it.
        The refusal names the first missing segment and the keys that exist at
        the nearest prefix.
        """
        if not path:
            return value, None
        if isinstance(value, dict) and path in value:
            return value[path], None
        segments = path.split(".") if "." in path else [path]
        current: object = value
        for i, segment in enumerate(segments):
            if isinstance(current, dict) and segment in current:
                current = current[segment]
                continue
            if isinstance(current, dict):
                keys = _key_sizes(current) if current else "none"
            elif i == 0:
                keys = "none"
            else:
                keys = f"none (it is a {type(current).__name__})"
            if i == 0:
                where = f"has no top-level key {_shown_key(segment)}"
                if len(segments) > 1:
                    where += f" (first missing segment of {_shown_key(path)})"
                return None, f"{where}. Its top-level keys: {keys}"
            prefix = ".".join(segments[:i])
            return None, (
                f"has no key {_shown_key(segment)} under {_shown_key(prefix)} "
                f"(first missing segment of {_shown_key(path)}). Keys under {_shown_key(prefix)}: {keys}"
            )
        return current, None

    def _path_label(value: object, path: str) -> str:
        """How a read part is named: value["k"], or value["a"]["b"] for a dotted path."""
        if isinstance(value, dict) and path in value:
            return f"value[{_shown_key(path)}]"
        return "value" + "".join(f"[{_shown_key(s)}]" for s in path.split("."))

    def _where_candidates(value: object) -> list[list[tuple[str, list]]]:
        """The lists of a value a layout-free where_json searches, by depth.

        Every list reached through objects only, down to ``_WHERE_MAX_DEPTH``
        keys, grouped by depth and named by their dotted path (``outer.inner``).
        Only the shallowest depth that holds a list and the one below it are
        kept: the search starts where the value's lists are (a value keeping
        its lists under an object of objects, ``pools.<pool>.open``, is
        searched at depth three), and goes one level deeper when nothing
        matches there. A list inside a list is never entered.
        """
        levels: list[list[tuple[str, list]]] = []
        frontier: list[tuple[str, dict]] = [("", value)] if isinstance(value, dict) else []
        for _ in range(_WHERE_MAX_DEPTH):
            found: list[tuple[str, list]] = []
            deeper: list[tuple[str, dict]] = []
            for prefix, obj in frontier:
                for k, v in obj.items():
                    name = f"{prefix}.{k}" if prefix else str(k)
                    if isinstance(v, list):
                        found.append((name, v))
                    elif isinstance(v, dict):
                        deeper.append((name, v))
            levels.append(found)
            frontier = deeper
            if not frontier:
                break
        start = next((i for i, level in enumerate(levels) if level), None)
        if start is None:
            return [[], []]
        kept = levels[start : start + 2]
        return kept + [[]] * (2 - len(kept))

    def _format_where_anywhere(
        state: dict,
        value: object,
        path: str,
        offset: int,
        max_chars: int,
        where: list,
        lines: list[str],
        under: dict | None = None,
        shape: dict | None = None,
    ) -> str:
        """A where_json read with no path, a path the value does not hold, or
        a path that reaches an object (``under``: its lists are searched as
        the value's are, one level then two, named ``path.inner``).

        The matching items come back grouped by the list they were found
        under (``[{"path": ..., "matches": [...]}]``) with the count per list.
        Nothing matching is a normal answer when no path was given (a dedupe
        read that finds nothing is the expected case, not a fault): 0 matches
        and the lists searched. A path the value does not hold stays an error.
        """
        from app.services.content_safety import wrap_untrusted
        from app.services.shared_state import list_item_matches

        if under is not None:
            missing = f"path {_shown_key(path)} is an object"
            candidates = [
                [(f"{path}.{inner}", items) for inner, items in depth]
                for depth in _where_candidates(under)
            ]
        else:
            missing = f"path {_shown_key(path)} not found" if path else "no path given"
            candidates = _where_candidates(value)
        searched: list[str] = []
        groups: list[dict] = []
        counts: list[str] = []
        for depth in candidates:
            for list_path, items in depth:
                searched.append(f"{_shown_key(list_path)} ({len(items)} item(s))")
                found = [i for i in items if any(list_item_matches(i, m) for m in where)]
                if found:
                    groups.append({"path": list_path, "matches": found})
                    counts.append(
                        f"  - where_json under {_shown_key(list_path)}: "
                        f"{len(found)} of {len(items)} item(s) match"
                    )
            if groups:
                break
        if not groups:
            if path and under is None:
                if not searched:
                    keys = _key_sizes(value) if isinstance(value, dict) and value else "none"
                    return (
                        f"Error: {missing}, and the value of `{state['key']}` holds no list "
                        f"for where_json to filter. Its top-level keys: {keys}."
                    )
                return (
                    f"Error: {missing}, and where_json matched no item in any list of "
                    f"`{state['key']}` (0 matches; lists searched: {', '.join(searched)})."
                )
            if not searched:
                # A value shape is not a fault on a read: 0 matches, and the
                # keys the caller can pass as `path` instead.
                if under is not None:
                    keys = _key_sizes(under) if under else "none"
                    lines.append(
                        f"  - where_json: 0 matches; {missing} of `{state['key']}` that "
                        f"holds no list for where_json to filter. Its keys: {keys}."
                    )
                else:
                    keys = _key_sizes(value) if isinstance(value, dict) and value else "none"
                    lines.append(
                        f"  - where_json: 0 matches; {missing}, and the value of "
                        f"`{state['key']}` holds no list for where_json to filter. "
                        f"Its top-level keys: {keys}."
                    )
                return "\n".join(lines)
            lines.append(
                f"  - where_json: 0 matches in any list (lists searched: {', '.join(searched)})"
            )
            if under is None and isinstance(value, dict) and not any(
                isinstance(v, list) for v in value.values()
            ):
                lines.append(f"  - the value's top-level keys: {_key_sizes(value)}")
            return "\n".join(lines)
        where_found = ", ".join(_shown_key(g["path"]) for g in groups)
        lines.append(f"  - {missing}; where_json matched under {where_found}")
        lines.extend(counts)
        if shape:
            # The page runs over the matches in path order, as they are shown.
            flat = [(g["path"], item) for g in groups for item in g["matches"]]
            kept, page = _shape_items([item for _, item in flat], shape)
            first = shape.get("item_offset") or 0
            paged: list[dict] = []
            for (list_path, _), item in zip(flat[first : first + len(kept)], kept, strict=True):
                if not paged or paged[-1]["path"] != list_path:
                    paged.append({"path": list_path, "matches": []})
                paged[-1]["matches"].append(item)
            groups = paged
            if page:
                lines.append(page)
        dumped = json.dumps(groups, default=str)
        if offset > len(dumped):
            return (
                f"Error: offset {offset} is past the end of the matches "
                f"({len(dumped)} chars), so nothing was read."
            )
        end = min(len(dumped), offset + max_chars)
        lines.append(f"  - matches by path, characters {offset} to {end} of {len(dumped)}:")
        lines.append(
            wrap_untrusted(dumped[offset:end], source=f"shared state value of {state['key']}")
        )
        if end < len(dumped):
            lines.append("    " + _cut_marker(groups, dumped, end, path=""))
        return "\n".join(lines)

    def _format_write(state: dict) -> str:
        """The compact read-back of a write: what the entry is now, not all of it.

        A long-lived entry (an open-PR list, a program with dozens of long notes)
        echoed whole costs 10 to 15 KB of context per write, for text the agent
        just sent. The header, the value (whole when small, else its top-level
        keys with sizes) and the latest note are enough to confirm the write.
        """
        status = state.get("status") or "active"
        status_label = status + (" [STALE]" if state.get("is_stale") else "")
        lines = [
            f"State **{state.get('label') or state['key']}** "
            f"(key: `{state['key']}`, status: {status_label})"
        ]
        if _read_only(state):
            lines.append("  - company-managed: read-only for this agent")
        lines.append(f"  - scope: {_scope_words(state)}")
        if state.get("owner_agent_id"):
            lines.append(f"  - owner agent: {state['owner_agent_id']}")
        if state.get("held_by_run_id"):
            lines.append(f"  - held by run {state['held_by_run_id']}")
        if state.get("resolved_at"):
            lines.append(f"  - resolved at: {state['resolved_at']}")
        if state.get("size_warning"):
            lines.append(f"  - SIZE WARNING: {state['size_warning']}")
        if state.get("retention"):
            policy = dict(state["retention"])
            for name in ("archive_key", "overflow_key"):
                if isinstance(policy.get(name), str):
                    policy[name] = policy[name].removeprefix(_prefix)
            lines.append(f"  - retention: {json.dumps(policy)[:_WRITE_KEY_CHARS * 4]}")
        if state.get("rolled"):
            target = state.get("rolled_to")
            counts = ", ".join(f"{name} {count}" for name, count in state["rolled"].items())
            where = f"to `{target.removeprefix(_prefix)}`" if target else "and dropped (no archive_key)"
            dropped = state.get("archive_dropped")
            tail = f"; the archive dropped its {dropped} oldest item(s) to fit" if dropped else ""
            lines.append(f"  - rolled by retention {where}: {counts}{tail}")
        if state.get("retention_rolled_by_age"):
            lines.append(
                "  - RETENTION WARNING: the policy has no archive_when, so the OLDEST items rolled "
                "whatever their state; set archive_when to roll terminal items only"
            )
        if state.get("retention_held_open") and state.get("retention_unmatched") is None:
            held = ", ".join(f"{n} {c}" for n, c in state["retention_held_open"].items())
            lines.append(
                f"  - RETENTION WARNING: open items kept past the cap ({held}): archive_when "
                "matches too few items to fit; remove_json terminal items or raise the cap"
            )
        if state.get("spilled"):
            target = str(state.get("spilled_to") or "").removeprefix(_prefix)
            counts = ", ".join(f"{name} {count}" for name, count in state["spilled"].items())
            lines.append(f"  - long fields moved by max_item_chars to `{target}` (item(s) per list): {counts}")
        if state.get("overflow_appended"):
            counts = ", ".join(
                f"`{name.removeprefix(_prefix)}` {count}" for name, count in state["overflow_appended"].items()
            )
            lines.append(
                f"  - update append on overflowed field(s) written through the pointer, item(s) added: {counts}"
            )
        if state.get("retention_unmatched") is not None:
            rule = json.dumps(state["retention_unmatched"])[: _WRITE_KEY_CHARS * 4]
            lines.append(
                f"  - RETENTION WARNING: archive_when {rule} matches no item while the value is over "
                "max_chars, so nothing can roll: fix the rule (a field value equal to it) or remove_json items"
            )
        lines.extend(_usage_lines(state))
        caps = state.get("size_caps")
        if caps:
            lines.append(
                f"  - size: {caps['size_chars']} chars (soft limit {caps['soft_limit_chars']}, "
                f"hard limit {caps['hard_limit_chars']})"
            )
        if "unset_keys" in state:
            removed = state["unset_keys"]
            lines.append(f"  - keys removed: {', '.join(removed)[:_WRITE_KEY_CHARS * 4] if removed else 'none (not present)'}")
        for list_key, counts in (state.get("list_changes") or {}).items():
            # A list changed under a write path is keyed "a.b.list": walk the
            # dotted path into the nested value, as a read with that path does.
            held, _missing = _resolve_path(state.get("value") or {}, list_key)
            size = len(held) if isinstance(held, list) else 0
            trimmed = (
                f"{counts['trimmed']} trimmed, " if counts.get("trimmed") is not None else ""
            )
            replaced = (
                f"{counts['replaced']} replaced, " if counts.get("replaced") is not None else ""
            )
            if counts.get("kept_open"):
                trimmed += f"{counts['kept_open']} open kept (archive_when), "
            if counts.get("folded") is not None:
                trimmed = f"{counts['folded']} folded, " + trimmed
            updated = (
                f"{counts['updated']} updated, " if counts.get("updated") is not None else ""
            )
            lines.append(
                f"  - list {json.dumps(list_key)[:_WRITE_KEY_CHARS]}: "
                f"{counts.get('appended', 0)} appended, {counts.get('removed', 0)} removed, "
                f"{updated}{replaced}{trimmed}{size} item(s) now"
            )
            lines.extend(_replaced_item_lines(list_key, (state.get("replaced_items") or {}).get(list_key) or []))
        value = state.get("value")
        if value:
            dumped = json.dumps(value, default=str)
            if len(dumped) <= _WRITE_VALUE_INLINE_CHARS or not isinstance(value, dict):
                lines.append(f"  - value: {dumped[:_WRITE_VALUE_INLINE_CHARS]}")
            else:
                lines.append(
                    f"  - value: {len(dumped)} chars, {len(value)} top-level key(s): "
                    f"{_key_sizes(value)}"
                )
        notes = state.get("notes") or []
        if notes:
            latest = notes[-1]
            text = str(latest.get("note", ""))
            if len(text) > _WRITE_NOTE_CHARS:
                text = text[:_WRITE_NOTE_CHARS] + f"... [{len(text) - _WRITE_NOTE_CHARS} more chars]"
            lines.append(f"  - notes: {len(notes)}, latest: [{latest.get('at', '')}] {text}")
        lines.append("  - get_shared_state reads the full entry (value and recent notes).")
        return "\n".join(lines)

    async def _resolve_overflow(shown: dict) -> tuple[dict, str]:
        """Replace the overflow pointers of the entry's value by the fields they name (#12320).

        Each overflow key the pointers name is read once, in the reader's
        scope (at most ``OVERFLOW_RESOLVE_MAX_KEYS``); a pointer whose key is
        hidden, missing or past that cap stays a pointer and is counted.
        """
        from app.services.shared_state import (
            OVERFLOW_RESOLVE_MAX_KEYS,
            get_state,
            overflow_pointer_keys,
            resolve_overflow_pointers,
        )

        keys = overflow_pointer_keys(shown.get("value"))
        if not keys:
            return shown, "  - overflow: no overflow:// pointer in the value"
        stored: dict[str, object] = {}
        for name in keys[:OVERFLOW_RESOLVE_MAX_KEYS]:
            try:
                found = await get_state(user_id=acting_user_id, key=name, reader=writer)
                if found is None and _prefix and not name.startswith(_prefix):
                    found = await get_state(user_id=acting_user_id, key=_ns(name), reader=writer)
            except Exception as exc:
                logger.warning("overflow key %s unreadable: %s", name, format_exception_message(exc))
                found = None
            if found is not None:
                stored[name] = found.get("value")
        value, resolved, missing = resolve_overflow_pointers(shown.get("value"), stored)
        line = f"  - overflow: {resolved} pointer(s) resolved from {len(stored)} key(s)"
        if missing:
            line += f", {missing} left as pointers (key, item or field not found"
            if len(keys) > OVERFLOW_RESOLVE_MAX_KEYS:
                line += f", or past the {OVERFLOW_RESOLVE_MAX_KEYS}-key cap"
            line += ")"
        return {**shown, "value": value}, line

    async def _read_family(
        family: str, path: str, where: list[Any] | None = None, fields: list[str] | None = None
    ) -> str:
        """The part `path` names in every active key starting with *family* (gap-evidence-overflow-scatter).

        A record split across sibling keys (``dev:harness-gaps:evidence-3`` ...
        ``-12``) is found in one call instead of one probe per key: each key
        where the path resolves is answered with its part, the others counted.
        With *where* (#1760) the part must be a list: only its items matching
        any match are answered, a key with none is counted, not shown; *fields*
        keeps those fields of each object item.
        """
        from app.services.content_safety import wrap_untrusted
        from app.services.shared_state import list_item_matches, list_states

        if not family.strip() or not path:
            return "Error: a key ending in * reads one `path` across the keys starting with it; give both."
        states: list[dict] = []
        try:
            while len(states) < _FAMILY_MAX_KEYS:
                page = await list_states(
                    user_id=acting_user_id,
                    status="active",
                    reader=writer,
                    key_prefix=_ns(family),
                    limit=min(_FAMILY_PAGE_KEYS, _FAMILY_MAX_KEYS - len(states)),
                    offset=len(states),
                )
                states.extend(page)
                if len(page) < _FAMILY_PAGE_KEYS:
                    break
        except Exception as exc:
            logger.error("get_shared_state family read failed: %s", format_exception_message(exc), exc_info=True)
            return "Error: failed to read the shared state. Please try again."
        hits: list[tuple[str, str]] = []
        found = matched = 0
        for state in states:
            part, missing = _resolve_path(state.get("value") or {}, path)
            if missing is not None:
                continue
            found += 1
            if where is not None or fields:
                if not isinstance(part, list):
                    continue
                if where is not None:
                    part = [item for item in part if any(list_item_matches(item, m) for m in where)]
                    if not part:
                        continue
                    matched += len(part)
                if fields:
                    part = [
                        {f: item[f] for f in fields if f in item} if isinstance(item, dict) else item for item in part
                    ]
            hits.append((_strip_ns(state)["key"], json.dumps(part, default=str)))
        lines = [f"Family `{family}*`: `{path}` found in {found} of {len(states)} active key(s)"]
        if where is not None:
            lines[0] += f"; where_json matched {matched} item(s) in {len(hits)} key(s)"
        if len(states) >= _FAMILY_MAX_KEYS:
            lines[0] += f" (first {_FAMILY_MAX_KEYS} keys only)"
        budget = _READ_WINDOW_DEFAULT_CHARS
        for name, dumped in hits:
            shown = dumped[: max(budget, 0)]
            budget -= len(shown)
            lines.append(f"  - `{name}` ({len(dumped)} chars):")
            lines.append(wrap_untrusted(shown, source=f"shared state value of {name}"))
            if len(shown) < len(dumped):
                lines.append(f"    [cut: get_shared_state(key='{name}', path='{path}') reads it whole]")
        return "\n".join(lines)

    async def _get_shared_state(
        key: str,
        path: str = "",
        offset: int | None = None,
        max_chars: int | None = None,
        where_json: str | None = None,
        fields: list | None = None,
        item_offset: int | None = None,
        item_limit: int | None = None,
        group_by: str | None = None,
        resolve_overflow: bool = False,
    ) -> str | ToolResult:
        from app.services.shared_state import get_state

        if resolve_overflow is not None and not isinstance(resolve_overflow, bool):
            return "Error: resolve_overflow must be true or false."

        if not (key or "").strip():
            return "Error: key is required."
        path = (path or "").strip() if isinstance(path, str) else ""
        try:
            start = int(offset) if offset is not None else 0
            size = int(max_chars) if max_chars is not None else 0
        except (TypeError, ValueError):
            return "Error: offset and max_chars must be whole numbers."
        if start < 0:
            return "Error: offset must be zero or more."
        if max_chars is not None and size < 1:
            return "Error: max_chars must be 1 or more."
        where: list | None = None
        if where_json is not None and str(where_json).strip():
            try:
                parsed = json.loads(where_json) if isinstance(where_json, str) else where_json
            except json.JSONDecodeError as exc:
                return json_argument_error("where_json", where_json, exc)
            except (TypeError, ValueError) as exc:
                return f"Error: where_json is not valid JSON ({exc})."
            where = parsed if isinstance(parsed, list) else [parsed]
            if not where or not all(isinstance(m, dict) and m for m in where):
                return (
                    "Error: where_json must be a non-empty JSON object of fields, "
                    "or a list of such objects (an item matching any of them is returned)."
                )
            from app.services.shared_state import list_match_error

            for m in where:
                if (error := list_match_error(m)) is not None:
                    return f"Error: where_json: {error}"
        if key.strip().endswith("*"):
            if fields is not None and (
                not isinstance(fields, list) or not all(isinstance(f, str) and f for f in fields)
            ):
                return "Error: fields must be a list of field names."
            return await _read_family(key.strip()[:-1], path, where, fields or None)
        shape: dict | None = None
        if fields or item_offset is not None or item_limit is not None:
            if fields is not None and (
                not isinstance(fields, list) or not all(isinstance(f, str) and f for f in fields)
            ):
                return "Error: fields must be a list of field names."
            try:
                first = int(item_offset) if item_offset is not None else None
                count = int(item_limit) if item_limit is not None else None
            except (TypeError, ValueError):
                return "Error: item_offset and item_limit must be whole numbers."
            if (first is not None and first < 0) or (count is not None and count < 1):
                return "Error: item_offset must be zero or more and item_limit 1 or more."
            shape = {"fields": fields or None, "item_offset": first, "item_limit": count}
        group = (group_by or "").strip() if isinstance(group_by, str) else ""
        if group and not path:
            return "Error: group_by needs `path`, the list whose items it counts."
        windowed = bool(path) or where is not None or offset is not None or max_chars is not None
        size = min(size if max_chars is not None else _READ_WINDOW_DEFAULT_CHARS, _READ_WINDOW_MAX_CHARS)
        try:
            state = await get_state(user_id=acting_user_id, key=_ns(key), reader=writer)
        except Exception as exc:
            logger.error("get_shared_state tool failed: %s", format_exception_message(exc), exc_info=True)
            return "Error: failed to read the shared state. Please try again."
        if state is None:
            if workspace_ingest_requested():
                # Nothing to write: a file holding this sentence would read as the value.
                return f"Error: no shared state found for key `{key}`, so nothing was written to the workspace."
            return ToolResult(
                tool_call_id="",
                content=(
                    f"No shared state found for key `{key}`. Use set_shared_state to "
                    "create it."
                ),
                structured={},
            )
        shown = await _with_holder(_strip_ns(state))
        overflow_line = ""
        if resolve_overflow:
            shown, overflow_line = await _resolve_overflow(shown)
        if group:
            header, rows = _group_items(shown, path, where, group, fields)
            if rows is None:
                return header
            if workspace_ingest_requested():
                return json.dumps(rows, default=str, ensure_ascii=False)
            return ToolResult(tool_call_id="", content=_format_groups(shown, header, rows), structured=rows)
        structured = json.loads(json.dumps(shown, default=str))
        if where is not None:
            # A gate check reads ``structured``: a where_json read carries its
            # selection and count, so ``$.match_count`` tells a gate whether the
            # list holds an item matching the event (#489).
            matches = _selected_items(shown, path, where)
            if matches is not None:
                structured["matches"] = matches
                structured["match_count"] = len(matches)
        if shape and not windowed:
            return "Error: fields, item_offset and item_limit need `path` (a list) or `where_json`."
        if workspace_ingest_requested():
            # Bound for the sandbox workspace (to_workspace_path): the selection
            # whole, as plain JSON, no window, no header, no notes.
            return _export_selection(shown, path, where, shape)
        out = _format_window(shown, path, start, size, where, shape) if windowed else _format_state(shown)
        if out.startswith("Error:"):
            return out
        if overflow_line:
            out = f"{overflow_line}\n{out}"
        # Tool-result grounding for the lifecycle: tell the agent when an entry is
        # concluded or has gone quiet so it acts on it instead of reusing stale state
        # (never on an entry it may only read).
        if _read_only(state):
            return ToolResult(tool_call_id="", content=out, structured=structured)
        if state.get("status") == "resolved":
            out += (
                "\n\nNote: this entry is RESOLVED. If the thing it tracks is live "
                "again, set_shared_state reactivates it; otherwise leave it - the "
                "retention sweep will purge it, or delete_shared_state removes it now."
            )
        elif state.get("is_stale"):
            out += (
                "\n\nNote: this entry is STALE (no update for a while). If it is "
                "still the source of truth, update it; if the thing it tracks is "
                "done, resolve_shared_state it; if it is useless, delete_shared_state it."
            )
        return ToolResult(tool_call_id="", content=out, structured=structured)

    async def _set_shared_state(
        key: str,
        value_json: str = "",
        label: str = "",
        note: str = "",
        owner_agent_id: str = "",
        replace: bool = False,
        append_json: str = "",
        remove_json: str = "",
        path: str = "",
        hold: str = "",
        keep_last_json: str = "",
        unique_by_json: str = "",
        update_json: str = "",
        archive_key: str = "",
        confirm_trim: bool = False,
        unset_keys: list[str] | None = None,
        from_workspace_path: str = "",
        lease: bool | None = None,
        work_item_id: str | None = None,
        expires_at: str | None = None,
        terminal_condition: str | None = None,
        retention_json: str = "",
        fold_json: str = "",
    ) -> str | ToolResult:
        from app.services.shared_state import SharedStateHeldError, upsert_state
        from app.services.work_item import current_claim_holder

        if not (key or "").strip():
            return "Error: key is required."
        if from_workspace_path:
            loaded_args = await _workspace_json_arguments(
                from_workspace_path,
                {
                    "value_json": value_json,
                    "append_json": append_json,
                    "remove_json": remove_json,
                    "keep_last_json": keep_last_json,
                    "unique_by_json": unique_by_json,
                    "update_json": update_json,
                },
            )
            # The file's arguments then go through the same checks as inline ones.
            if isinstance(loaded_args, str):
                return loaded_args
            value_json = loaded_args["value_json"]
            append_json = loaded_args["append_json"]
            remove_json = loaded_args["remove_json"]
            keep_last_json = loaded_args["keep_last_json"]
            unique_by_json = loaded_args["unique_by_json"]
            update_json = loaded_args["update_json"]
        if hold is not None and not isinstance(hold, str):
            return "Error: hold must be 'take' or 'release'. Nothing was written."
        if path is not None and not isinstance(path, str):
            return "Error: path must be a dot-separated string, e.g. 'slice_state.P2'. Nothing was written."
        parsed: dict[str, dict | None] = {}
        for name, raw in (
            ("value_json", value_json),
            ("append_json", append_json),
            ("remove_json", remove_json),
            ("keep_last_json", keep_last_json),
            ("unique_by_json", unique_by_json),
            ("update_json", update_json),
            ("fold_json", fold_json),
        ):
            parsed[name] = None
            if raw is None:
                continue
            if not isinstance(raw, str):
                return f"Error: {name} must be a string holding a JSON object. Nothing was written."
            if not raw.strip():
                continue
            try:
                loaded = json.loads(raw)
            except json.JSONDecodeError as exc:
                return json_argument_error(name, raw, exc)
            if not isinstance(loaded, dict) and not (name == "value_json" and (path or "").strip()):
                return f"Error: {name} must be a JSON object (any JSON value with path)."
            if name == "keep_last_json" and not all(
                isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in loaded.values()
            ):
                return (
                    "Error: keep_last_json must map each top-level key to an integer of 0 or more, "
                    'e.g. {"runs": 30}. Nothing was written.'
                )
            if name == "unique_by_json" and not all(
                (isinstance(v, str) and v)
                or (isinstance(v, list) and v and all(isinstance(f, str) and f for f in v))
                for v in loaded.values()
            ):
                return (
                    "Error: unique_by_json must map each top-level list key to a field name or a "
                    'non-empty list of field names, e.g. {"prs": "number"}. Nothing was written.'
                )
            if name in ("append_json", "remove_json") and not all(isinstance(v, list) for v in loaded.values()):
                return (
                    f"Error: {name} must map each top-level key to a list of items, "
                    'e.g. {"prs": [{"repo": "o/r", "number": 12}]}. Nothing was written.'
                )
            if name == "update_json" and not all(
                isinstance(v, dict) or (isinstance(v, list) and v and all(isinstance(p, dict) for p in v))
                for v in loaded.values()
            ):
                return (
                    "Error: update_json must map each top-level list key to a patch object (or a list "
                    'of them), e.g. {"gaps": {"match": {"slug": "x"}, "increment": {"count": 1}}}. '
                    "Nothing was written."
                )
            parsed[name] = loaded
        parsed_value = parsed["value_json"]
        path_list_ops = (path or "").strip() and any(
            parsed[name] for name in ("append_json", "remove_json", "keep_last_json", "update_json", "fold_json")
        )
        if (path or "").strip() and parsed_value is None and not path_list_ops:
            return (
                "Error: path needs value_json, the object to write under it, or list changes "
                "(append_json, remove_json, update_json, keep_last_json, fold_json) to apply to the "
                "lists of the object under it. Nothing was written."
            )
        if confirm_trim is not None and not isinstance(confirm_trim, bool):
            return "Error: confirm_trim must be true or false. Nothing was written."
        if lease is not None and not isinstance(lease, bool):
            return "Error: lease must be true or false. Nothing was written."
        for field_name, field_value in (
            ("work_item_id", work_item_id),
            ("expires_at", expires_at),
            ("terminal_condition", terminal_condition),
        ):
            if field_value is not None and not isinstance(field_value, str):
                return f"Error: {field_name} must be a string. Nothing was written."
        linked_item: str | None = None
        if work_item_id is not None:
            linked_item = _full_work_item_id(work_item_id)
            if linked_item.startswith("Error:"):
                return linked_item
        if unset_keys is not None and (
            not isinstance(unset_keys, list) or not all(isinstance(k, str) and k for k in unset_keys)
        ):
            return "Error: unset_keys must be a list of top-level key names. Nothing was written."
        retention: dict | None = None
        if retention_json is not None and not isinstance(retention_json, str):
            return "Error: retention_json must be a string holding a JSON object. Nothing was written."
        if (retention_json or "").strip():
            try:
                retention = json.loads(retention_json)
            except json.JSONDecodeError as exc:
                return json_argument_error("retention_json", retention_json, exc)
            if isinstance(retention, dict) and isinstance(retention.get("archive_key"), str):
                if normalize_correlation_key(retention["archive_key"]) == normalize_correlation_key(key):
                    return "Error: retention_json archive_key must name another key than key. Nothing was written."
                retention = {**retention, "archive_key": _ns(retention["archive_key"])}
            if isinstance(retention, dict) and isinstance(retention.get("overflow_key"), str):
                if normalize_correlation_key(retention["overflow_key"]) == normalize_correlation_key(key):
                    return "Error: retention_json overflow_key must name another key than key. Nothing was written."
                retention = {**retention, "overflow_key": _ns(retention["overflow_key"])}
        note_only = _is_note_only(
            parsed,
            unset_keys=unset_keys,
            hold=hold,
            label=label,
            owner_agent_id=owner_agent_id,
            lease=lease,
            work_item_id=work_item_id,
            expires_at=expires_at,
            terminal_condition=terminal_condition,
            retention=retention,
            archive_key=archive_key,
        )
        archive_line = ""
        expect_dropped: dict[str, list] | None = None
        if archive_key is not None and not isinstance(archive_key, str):
            return "Error: archive_key must be a shared state key. Nothing was written."
        if (archive_key or "").strip():
            target = archive_key.strip()
            value_path = (path or "").strip() or None
            drops = bool(parsed["remove_json"] or parsed["keep_last_json"])
            unsets = _has_unset(parsed["update_json"])
            if not (drops or unsets or unset_keys):
                return (
                    "Error: archive_key archives what this write drops: remove_json, keep_last_json, "
                    "update_json patches with 'unset', or unset_keys; this write drops nothing. "
                    "Nothing was written."
                )
            if drops:
                line, expect_dropped = await _archive_dropped(
                    key, target, parsed, guard_trim=not confirm_trim, path=value_path
                )
                if line.startswith("Error:"):
                    return line
                archive_line += line
            if unsets:
                line = await _archive_unset(key, target, parsed["update_json"], path=value_path)
                if line.startswith("Error:"):
                    return line
                archive_line += line
            if unset_keys:
                line = await _archive_unset_keys(key, target, unset_keys)
                if line.startswith("Error:"):
                    return line
                archive_line += line
        try:
            state = await upsert_state(
                user_id=acting_user_id,
                key=_ns(key),
                label=label or None,
                owner_agent_id=owner_agent_id or None,
                value=parsed_value,
                note=note or None,
                # Writing live state marks the entry active (reactivating it if it
                # had been resolved): a resolved entry should never keep receiving
                # writes while hidden from active reads. To conclude it, call
                # resolve_shared_state; to remove it, call delete_shared_state.
                # A note alone (or a hold release) is no live write: it keeps the
                # status, so a closing note on a released lease never reopens it.
                status=None if note_only else "active",
                acting_agent_id=agent_id,
                is_company_managed=is_company_managed,
                workspace_id=workspace_id,
                replace_value=replace,
                writer=writer,
                append=parsed["append_json"],
                remove=parsed["remove_json"],
                keep_last=parsed["keep_last_json"],
                unique_by=parsed["unique_by_json"],
                update=parsed["update_json"],
                fold=parsed["fold_json"],
                # A cap never drops more than the call appends unless the
                # caller confirms it: a first cap on a long ledger is a loss.
                guard_trim=not confirm_trim,
                # Nested objects merge key by key: a write to one
                # sub-key never drops a sibling another run wrote.
                deep=True,
                path=path or None,
                hold=(hold or "").strip() or None,
                run_id=current_claim_holder(run_id),
                unset_keys=unset_keys or None,
                expect_dropped=expect_dropped,
                lease=lease,
                linked_work_item_id=linked_item,
                expires_at=expires_at,
                terminal_condition=terminal_condition,
                retention=retention,
            )
        except CompanyManagedWriteRefusedError as exc:
            return _refused(exc, key)
        except SharedStateHeldError as exc:
            return _held(exc, key)
        except ValueError as exc:
            return f"Error: {exc}"
        except Exception as exc:
            logger.error("set_shared_state tool failed: %s", format_exception_message(exc), exc_info=True)
            return "Error: failed to save the shared state. Please try again."
        return "Saved shared state.\n" + archive_line + _format_write(_strip_ns(state))

    def _full_work_item_id(raw: str) -> str:
        """*raw* as a work item id: ``""`` clears the link, a full UUID links, else an Error line."""
        text = raw.strip()
        if not text or safe_uuid(text) is not None:
            return text
        return (
            f"Error: work_item_id {text!r} is not a full UUID; read it with get_work_item or "
            "list_work_items. Nothing was written."
        )

    async def _workspace_json_arguments(path: object, inline: dict[str, str]) -> dict[str, str] | str:
        """*inline* completed with the ``*_json`` arguments a workspace JSON file holds, or the refusal.

        The file is one JSON object whose keys are argument names of
        :data:`_WORKSPACE_JSON_ARGUMENTS` and whose values are the JSON the
        argument would carry (an object, or that JSON as a string). Read on
        the server through the run's workspace: its content never enters the
        context.
        """
        if not isinstance(path, str):
            return "Error: from_workspace_path must be a workspace path, e.g. work/x.json. Nothing was written."
        workspace = current_workspace()
        if workspace is None:
            return "Error: from_workspace_path needs the sandbox workspace of this run, and it has none. Nothing was written."
        try:
            _rel, _name, data = await workspace.read_file(path, tool="set_shared_state")
        except ValueError as exc:
            return f"Error: from_workspace_path refused: {exc} Nothing was written."
        if len(data) > _WORKSPACE_JSON_MAX_BYTES:
            return (
                f"Error: from_workspace_path holds {len(data):,} bytes; the ceiling is "
                f"{_WORKSPACE_JSON_MAX_BYTES:,}. Split the write. Nothing was written."
            )
        try:
            loaded = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return f"Error: from_workspace_path is not a UTF-8 JSON file ({exc}). Nothing was written."
        if not isinstance(loaded, dict) or not loaded:
            return (
                "Error: from_workspace_path must hold a JSON object of arguments, e.g. "
                '{"append_json": {"runs": [{"n": 1}]}}. Nothing was written.'
            )
        unknown = sorted(set(loaded) - set(_WORKSPACE_JSON_ARGUMENTS))
        if unknown:
            return (
                f"Error: from_workspace_path names {', '.join(unknown)}; it may hold "
                f"{', '.join(_WORKSPACE_JSON_ARGUMENTS)}. Pass any other argument inline beside "
                "from_workspace_path: replace=true inline writes the file's value_json whole. "
                "Nothing was written."
            )
        merged = dict(inline)
        for name, value in loaded.items():
            if isinstance(merged.get(name), str) and merged[name].strip():
                return f"Error: {name} is given both inline and in from_workspace_path. Nothing was written."
            merged[name] = value if isinstance(value, str) else json.dumps(value)
        return merged

    async def _archive_dropped(
        key: str, archive_key: str, parsed: dict[str, Any], *, guard_trim: bool, path: str | None = None
    ) -> tuple[str, dict[str, list[Any]] | None]:
        """Append what remove_json / keep_last_json drop to *archive_key*, before the write.

        The dropped items go whole to the list of the same name under the
        archive key; with *path* the lists are those of the object under it.
        Answers the line the write's answer opens with (or an
        ``Error:`` line, nothing written) and the items dropped, which the
        source write checks under its row lock: a list changed in between is
        refused there, so an item never leaves without its copy.
        """
        from app.services.shared_state import (
            apply_list_ops,
            get_state,
            parse_value_path,
            retention_protect,
            upsert_state,
            value_at_path,
        )
        from app.services.work_item import current_claim_holder

        if normalize_correlation_key(archive_key) == normalize_correlation_key(key):
            return "Error: archive_key must name another key than key. Nothing was written.", None
        try:
            source = await get_state(user_id=acting_user_id, key=_ns(key), reader=writer)
            if _is_archive((source or {}).get("value")):
                return (
                    f"Error: {key!r} is itself an archive; an archive is never archived into another "
                    "(#11696). Drop its oldest items with keep_last_json and confirm_trim, without "
                    "archive_key; the platform also caps it. Nothing was written.",
                    None,
                )
            dropped: dict[str, list] = {}
            source_value = dict((source or {}).get("value") or {})
            if path:
                held = value_at_path(source_value, parse_value_path(path))
                if held is not None and not isinstance(held, dict):
                    return f"Error: the value under {path!r} is not an object. Nothing was written.", None
                source_value = dict(held or {})
            apply_list_ops(
                source_value,
                append=parsed["append_json"],
                remove=parsed["remove_json"],
                keep_last=parsed["keep_last_json"],
                unique_by=parsed["unique_by_json"],
                update=parsed["update_json"],
                guard_trim=guard_trim,
                dropped=dropped,
                # The retention protects the top-level lists only (path writes never roll).
                protect=None if path else retention_protect((source or {}).get("retention"), source_value),
            )
        except ValueError as exc:
            return f"Error: {exc}", None
        count = sum(len(v) for v in dropped.values())
        if not count:
            return "Archived: nothing (the call drops no item).\n", dropped
        try:
            await upsert_state(
                user_id=acting_user_id,
                key=_ns(archive_key),
                value=None,
                status="active",
                acting_agent_id=agent_id,
                is_company_managed=is_company_managed,
                workspace_id=workspace_id,
                writer=writer,
                append=dropped,
                note=f"archived {count} item(s) dropped from {key}",
                run_id=current_claim_holder(run_id),
                archive=True,
            )
        except CompanyManagedWriteRefusedError as exc:
            return f"Error: the archive key {archive_key!r} refused the write ({exc}). Nothing was written.", None
        except ValueError as exc:
            return f"Error: archive {archive_key!r}: {exc}. Nothing was written.", None
        lists = ", ".join(f"{k} {len(v)}" for k, v in dropped.items())
        return f"Archived {count} item(s) whole to `{archive_key}` (lists: {lists}) before the write.\n", dropped

    def _has_unset(update: dict[str, Any] | None) -> bool:
        """True when an update_json carries at least one patch with ``unset``."""
        if not update:
            return False
        return any(
            (p.get("unset") if isinstance(p, dict) else None)
            for patches in update.values()
            for p in (patches if isinstance(patches, list) else [patches])
        )

    async def _archive_unset_keys(key: str, archive_key: str, unset_keys: list[str]) -> str:
        """Store the top-level fields *unset_keys* removes under *archive_key*, before the write.

        One archive object ``{source_key, unset_keys, archived_at, fields}`` in the
        archive's ``entries`` list; written first, so a failure leaves a copy, never a loss.
        """
        from app.services.shared_state import get_state, upsert_state
        from app.services.work_item import current_claim_holder

        if normalize_correlation_key(archive_key) == normalize_correlation_key(key):
            return "Error: archive_key must name another key than key. Nothing was written."
        source = await get_state(user_id=acting_user_id, key=_ns(key), reader=writer)
        value = (source or {}).get("value") or {}
        fields = {k: value[k] for k in dict.fromkeys(unset_keys) if isinstance(value, dict) and k in value}
        if not fields:
            return "Archived: nothing (no unset key is in the value).\n"
        entry = {
            "source_key": key,
            "unset_keys": list(fields),
            "archived_at": datetime.now(UTC).isoformat(),
            "fields": fields,
        }
        try:
            await upsert_state(
                user_id=acting_user_id,
                key=_ns(archive_key),
                value=None,
                status="active",
                acting_agent_id=agent_id,
                is_company_managed=is_company_managed,
                workspace_id=workspace_id,
                writer=writer,
                append={"entries": [entry]},
                note=f"archived {len(fields)} key(s) unset from {key}",
                run_id=current_claim_holder(run_id),
                archive=True,
            )
        except CompanyManagedWriteRefusedError as exc:
            return f"Error: the archive key {archive_key!r} refused the write ({exc}). Nothing was written."
        except ValueError as exc:
            return f"Error: archive {archive_key!r}: {exc}. Nothing was written."
        size = len(json.dumps(fields, default=str))
        return (
            f"Archived {len(fields)} key(s) ({', '.join(fields)}), {size} characters, to `{archive_key}` "
            "(list entries) before the write.\n"
        )

    async def _archive_unset(key: str, archive_key: str, update: dict[str, Any] | None, path: str | None = None) -> str:
        """Store what an update's unset removes under *archive_key*, before the write.

        Answers the line the write's answer opens with, or an ``Error:`` line
        (nothing written anywhere). The archive is appended first and the
        source written only after it, so a failure between the two leaves a
        copy, never a loss.
        """
        from app.services.shared_state import get_state, parse_value_path, unset_archive, upsert_state, value_at_path
        from app.services.work_item import current_claim_holder

        if normalize_correlation_key(archive_key) == normalize_correlation_key(key):
            return "Error: archive_key must name another key than key. Nothing was written."
        try:
            source = await get_state(user_id=acting_user_id, key=_ns(key), reader=writer)
            retention = (source or {}).get("retention") or {}
            held = (source or {}).get("value") or {}
            if path:
                held = value_at_path(held, parse_value_path(path))
                held = held if isinstance(held, dict) else {}
            archived = unset_archive(
                held,
                update,
                source_key=f"{key}#{path}" if path else key,
                at=datetime.now(UTC).isoformat(),
                item_key=retention.get("item_key") if isinstance(retention, dict) else None,
            )
        except ValueError as exc:
            return f"Error: {exc}"
        if not archived:
            return "Archived: nothing (no matched item held an unset field).\n"
        try:
            await upsert_state(
                user_id=acting_user_id,
                key=_ns(archive_key),
                value=None,
                status="active",
                acting_agent_id=agent_id,
                is_company_managed=is_company_managed,
                workspace_id=workspace_id,
                writer=writer,
                append={"entries": archived},
                note=f"archived {len(archived)} item(s) unset from {key}",
                run_id=current_claim_holder(run_id),
                archive=True,
            )
        except CompanyManagedWriteRefusedError as exc:
            return f"Error: the archive key {archive_key!r} refused the write ({exc}). Nothing was written."
        except ValueError as exc:
            return f"Error: archive {archive_key!r}: {exc}. Nothing was written."
        size = sum(len(json.dumps(a["fields"], default=str)) for a in archived)
        return (
            f"Archived {len(archived)} item(s), {size} characters, to `{archive_key}` "
            "(list entries) before the write.\n"
        )

    def _is_archive(value: object) -> bool:
        """True when *value* holds items the platform archived (an archive entry)."""
        from app.services.shared_state import ARCHIVE_STAMP

        if not isinstance(value, dict):
            return False
        return any(
            isinstance(item, dict) and ARCHIVE_STAMP in item
            for items in value.values()
            if isinstance(items, list)
            for item in items[-5:]
        )

    def _held(exc, key: str) -> ToolResult:
        """A lease another live run holds: the expected outcome, not an error.

        The caller stands down (or writes a pending_fix without ``hold``), so
        the answer is a plain result: no friction, no failure streak.
        """
        holder = str(exc.holder_run_id) if exc.holder_run_id else ""
        lines = [
            f"Held by live run {holder or '(unknown)'}; stand down or write a pending_fix. "
            f"`{key}` is held by run {holder or '(unknown)'}, still {exc.holder_status or 'alive'}: "
            "nothing was written."
        ]
        if exc.pending_fix is not None:
            lines.append(f"  - pending_fix: {json.dumps(exc.pending_fix, default=str)[:500]}")
        return ToolResult(
            tool_call_id="",
            content="\n".join(lines),
            structured={
                "held": True,
                "holder_run_id": holder or None,
                "holder_status": exc.holder_status,
                "pending_fix": exc.pending_fix,
            },
        )

    async def _list_shared_state(
        include_resolved: bool = False,
        stale_only: bool = False,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
        scope: str = "",
        key_prefix: str = "",
    ) -> str:
        from app.services import coordination_scope
        from app.services.shared_state import count_states, list_states

        # The caller's fleet or own entries, resolved as list_work_items does.
        try:
            resolved = await coordination_scope.resolve_coordination_scope(agent_id, acting_user_id, scope)
        except Exception as exc:
            logger.error("list_shared_state scope failed: %s", format_exception_message(exc), exc_info=True)
            return "Error: failed to resolve the scope of the listing. Please try again."
        if isinstance(resolved, str):
            return resolved
        scope_line = resolved.label("entry")

        # Active-only by default so resolved entries stop cluttering the run;
        # ``stale_only`` surfaces the active entries that have gone quiet so the
        # owner can resolve or purge them.
        status = None if (include_resolved or stale_only) else "active"
        size, start = page_bounds(limit, offset)
        filters = {
            "user_id": acting_user_id,
            "status": status,
            "stale_only": stale_only,
            "reader": writer,
            # The caller's key_prefix narrows inside its namespace, in the query,
            # so one family of keys (dev:green-claim:) answers without a page.
            "key_prefix": f"{_prefix}{key_prefix or ''}" or None,
            "scope": resolved.shared_state_clause(),
        }
        try:
            states = await list_states(**filters, limit=size, offset=start)
            # A short page is the end: its total is known without a COUNT.
            total = (
                await count_states(**filters)
                if len(states) >= size or (start and not states)
                else start + len(states)
            )
        except Exception as exc:
            logger.error("list_shared_state tool failed: %s", format_exception_message(exc), exc_info=True)
            return "Error: failed to list shared state. Please try again."
        if _prefix:
            # Namespaced scope: only this namespace's entries exist as far as
            # the agent is concerned, and they read as clean keys.
            states = [
                _strip_ns(s)
                for s in states
                if isinstance(s.get("key"), str) and s["key"].startswith(_prefix)
            ]
        if not states and not total:
            if stale_only:
                return f"No stale shared state entries - nothing to clean up. {scope_line}"
            which = "" if include_resolved else " active"
            return f"No{which} shared state entries found. {scope_line}"
        # Scope word for the header; append the separating space only when a word
        # is present so the line never collapses (e.g. "3 staleshared state
        # entries:") - computing the space here is what keeps it correct.
        which = "stale" if stale_only else ("" if include_resolved else "active")
        prefix = f"{which} " if which else ""
        # The loop cuts a result from its end to its cap: keep only the rows
        # the answer can carry, so the page line names what is delivered and
        # the next offset is the first row left out.
        # Two visible entries under one key (a company-managed row and a
        # personal copy) read as one to an agent: flag them.
        key_counts: dict[str, int] = {}
        for s in states:
            entry_key = str(s.get("key"))
            key_counts[entry_key] = key_counts.get(entry_key, 0) + 1

        def _row(s: dict) -> str:
            label = f" - {s['label']}" if s.get("label") else ""
            tags = [s.get("status") or "active"]
            if key_counts.get(str(s.get("key")), 0) > 1:
                tags.append("duplicate key")
            if s.get("is_stale"):
                tags.append("STALE")
            if s.get("over_soft_limit"):
                tags.append("OVERSIZE")
            if _read_only(s):
                tags.append("read-only")
            if s.get("is_lease"):
                tags.append("lease")
            size = s.get("size_chars")
            sized = f" {size} chars" if isinstance(size, int) else ""
            if sized and s.get("largest_list"):
                sized += f", longest list {s['largest_list']}"
            if isinstance(s.get("value_age_days"), int):
                sized += f", value unchanged {s['value_age_days']}d"
            ends = s.get("terminal_condition") or (
                f"work item {s['linked_work_item_id']} ends" if s.get("linked_work_item_id") else None
            )
            if s.get("expires_at"):
                ends = f"{ends}; " if ends else ""
                ends += f"expires {s['expires_at'][:16]}"
            sized += f"; ends: {ends}" if ends else "; ends: not set"
            sized += f"; scope: {_scope_words(s)}"
            if s.get("owner_agent_id"):
                sized += f"; owner agent {s['owner_agent_id']}"
            return f"- `{s['key']}`{label} [{', '.join(tags)}]{sized}"

        blocks = [_row(s) for s in states]
        kept = rows_that_fit(blocks, answer_budget())
        states = states[:kept]
        lines = [f"{page_line(start, kept, total, f'{prefix}shared state entries')} {scope_line}"]
        if states:
            lines.append(
                f"{len(states)} {prefix}shared state "
                f"entr{'y' if len(states) == 1 else 'ies'}:"
            )
        lines.extend(blocks[:kept])
        # The tidy-up nudge only counts entries this agent may resolve or delete.
        stale_count = sum(1 for s in states if s.get("is_stale") and not _read_only(s))
        if stale_count and not stale_only:
            lines.append(
                f"\n{stale_count} entr{'y' if stale_count == 1 else 'ies'} look stale. "
                "Resolve the ones whose thing is done (resolve_shared_state) and "
                "delete the useless ones (delete_shared_state) - you own keeping "
                "your shared state tidy."
            )
        return "\n".join(lines)

    async def _resolve_shared_state(key: str, note: str = "") -> str:
        from app.services.shared_state import resolve_state

        if not (key or "").strip():
            return "Error: key is required."
        try:
            state = await resolve_state(
                user_id=acting_user_id,
                key=_ns(key),
                note=note or None,
                acting_agent_id=agent_id,
                writer=writer,
            )
        except CompanyManagedWriteRefusedError as exc:
            return _refused(exc, key)
        except Exception as exc:
            logger.error("resolve_shared_state tool failed: %s", format_exception_message(exc), exc_info=True)
            return "Error: failed to resolve the shared state. Please try again."
        if state is None:
            return (
                f"No shared state found for key `{key}` to resolve "
                "(list_shared_state to see what exists)."
            )
        return (
            "Resolved shared state - it will drop out of active reads and be "
            "purged after the retention window (delete_shared_state to remove it "
            "now).\n" + _format_write(_strip_ns(state))
        )

    async def _delete_shared_state(key: str) -> str:
        from app.services.shared_state import delete_state

        if not (key or "").strip():
            return "Error: key is required."
        try:
            removed = await delete_state(
                user_id=acting_user_id, key=_ns(key), writer=writer
            )
        except CompanyManagedWriteRefusedError as exc:
            return _refused(exc, key)
        except Exception as exc:
            logger.error("delete_shared_state tool failed: %s", format_exception_message(exc), exc_info=True)
            return "Error: failed to delete the shared state. Please try again."
        if not removed:
            return (
                f"No shared state found for key `{key}` to delete (it may already "
                "be gone, or it is not visible to you)."
            )
        return f"Purged shared state `{key.strip()}`."

    executor = CallableToolExecutor()
    executor.register("get_shared_state", _get_shared_state)
    executor.register("set_shared_state", _set_shared_state)
    executor.register("list_shared_state", _list_shared_state)
    executor.register("resolve_shared_state", _resolve_shared_state)
    executor.register("delete_shared_state", _delete_shared_state)

    definitions = [
        ToolDefinition(
            name="get_shared_state",
            description=(
                "Read the shared, cross-run state under an exact key; call it at "
                "the start of a run to continue from the fleet's state. Shows the "
                "status (active / resolved, stale) and the latest 5 notes. A value "
                "over 2,000 characters is shown by its top-level keys and sizes: "
                "`path` reads one key whole, `offset` / `max_chars` a window of the "
                "JSON (no notes). `where_json` returns only the list items carrying "
                "its fields (the remove_json rule); without a held list `path` it "
                "searches every list under it, grouped by path. `fields` and "
                "`item_offset` / `item_limit` project and page those items; "
                "`group_by` counts them."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Exact key; PREFIX*: path in each key"},
                    "path": {"type": "string", "description": "A top-level key or dot-separated path (e.g. 'prs', 'pools.x') to read alone, whole up to max_chars"},
                    "offset": {"type": "integer", "description": "Character offset into the value's JSON (or the path's) to read from (default 0)"},
                    "max_chars": {"type": "integer", "description": "Characters to return in a windowed read (default 20,000, at most 50,000)"},
                    "where_json": {"type": "string", "description": "A JSON object of fields, or a list of them (any matches), e.g. {\"n\": 12}; a value may be an operator object: {\"starts_with\": \"P\"}, {\"exists\": false}"},
                    "fields": {"type": "array", "items": {"type": "string"}, "description": "Keep only these fields of each item"},
                    "item_offset": {"type": "integer", "description": "First item of the page (default 0)"},
                    "item_limit": {"type": "integer", "description": "Items in the page"},
                    "group_by": {
                        "type": "string",
                        "description": "Field (dotted ok) of the `path` list items: count per value, after where_json",
                    },
                    "resolve_overflow": {
                        "type": "boolean",
                        "description": "Replace overflow:// pointers by the fields they name",
                    },
                },
                "required": ["key"],
            },
            source="builtin:coordination",
            workspace_ingest=True,
        ),
        ToolDefinition(
            name="set_shared_state",
            description=(
                "Create or update the durable shared state of a key (marks it active). One "
                "stable key per thing, no date in it (put 'as of' in `note`); retire it with "
                "resolve_shared_state, never with a status in the value. value_json is merged "
                "recursively (objects key by key, lists and scalars replace; replace=true "
                "overwrites). path writes one sub-key alone under the entry's row lock. A list "
                "under a top-level key changes with append_json / remove_json / update_json / "
                "keep_last_json, applied on the server: never read and rewrite it. Name what ends "
                "the entry at creation. Values and lists are capped. Answers a compact read-back."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Exact state key (any caller-chosen identifier)"},
                    "value_json": {"type": "string", "description": "A JSON object to merge in (nested objects merge key by key; lists and scalars replace); with path, any JSON value"},
                    "path": {"type": "string", "description": "Dot-separated sub-key value_json is written under alone, e.g. 'slice_state.P2' (created when missing, siblings kept; replace=true overwrites it alone); list ops apply to its lists"},
                    "label": {"type": "string", "description": "Optional human-friendly label for the entry"},
                    "note": {"type": "string", "description": "Log note (store-stamped)"},
                    "owner_agent_id": {"type": "string", "description": "UUID of the agent responsible for this state (defaults to you on first create)"},
                    "replace": {"type": "boolean", "description": "Overwrite the whole value object (or, with path, that sub-key alone) instead of merging (default false)"},
                    "append_json": {"type": "string", "description": "Top-level keys to lists of items to append (list created when missing; an item present is not added twice), e.g. {\"prs\": [{\"number\": 12}]}"},
                    "remove_json": {"type": "string", "description": "Top-level keys to lists of matches: drops each item equal to a match or carrying all its fields (where_json operators). Before append_json"},
                    "keep_last_json": {"type": "string", "description": "List keys to a count N: after removals and appends, only the newest N items stay, e.g. {\"runs\": 30}. Trimming more than appended needs confirm_trim"},
                    "confirm_trim": {"type": "boolean", "description": "Let keep_last_json or an unarchived retention roll drop more items than appended"},
                    "unique_by_json": {"type": "string", "description": "List keys to identity field(s), e.g. {\"prs\": \"number\"}: an append replaces the item with the same values"},
                    "update_json": {"type": "string", "description": "Patch matched list items in place: {\"gaps\": {\"match\": {\"slug\": \"x\"}, \"set\": {}, \"increment\": {\"count\": 1}, \"append\": {\"runs\": []}, \"unset\": [\"f\"]}}; match may be a list (any matches; one matching nothing refuses all). Applied after the removals, before the appends"},
                    "fold_json": {"type": "string", "description": "List keys to identity field(s) or {by, sum}: duplicates merge in the first"},
                    "archive_key": {"type": "string", "description": "Key that first receives what remove_json / keep_last_json / an unset drop"},
                    "retention_json": {"type": "string", "description": "List policy per write: keep_last, max_chars, archive_key, archive_when, max_item_chars, overflow_key, keep_fields, roll_open; {} clears"},
                    "unset_keys": {"type": "array", "items": {"type": "string"}, "description": "Top-level keys to remove from the value"},
                    "hold": {"type": "string", "enum": ["take", "release"], "description": "Hold the entry as a lease (a branch lock): 'take' records this run as its holder (a live other holder answers held, not an error), taking it over once that run ended; 'release' frees it. The platform frees the hold of an ended run. Omit to keep the holder"},
                    "lease": {"type": "boolean", "description": "Resolved at hold release (default: created by take)"},
                    "work_item_id": {"type": "string", "description": "Work item UUID: resolved when it ends"},
                    "expires_at": {"type": "string", "description": "ISO-8601: resolved then"},
                    "terminal_condition": {"type": "string", "description": "What ends the entry"},
                },
                "required": ["key"],
            },
            source="builtin:coordination",
            # from_workspace_path is added at offer time, with a workspace only.
            workspace_read=True,
        ),
        ToolDefinition(
            name="list_shared_state",
            description=(
                "List shared-state entries visible to you (active only by "
                "default). Each entry shows its status, stale flag, value age and "
                "what ends it. "
                "Set stale_only=true to find active entries that have gone quiet "
                "(candidates to resolve or purge), or include_resolved=true to "
                "also see resolved entries. Paged, newest-updated first: "
                f"{DEFAULT_PAGE_LIMIT} entries per call by default (limit up to "
                f"{MAX_PAGE_LIMIT}); the first line gives the total and, when the "
                "page is cut, the offset of the next page."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "include_resolved": {"type": "boolean", "description": "Also list resolved entries (default false)"},
                    "stale_only": {"type": "boolean", "description": "Only active entries that have gone quiet past the staleness window"},
                    "key_prefix": {"type": "string", "description": "Only keys starting with it"},
                    "limit": {
                        "type": "integer",
                        "description": f"Entries per page (default {DEFAULT_PAGE_LIMIT}, max {MAX_PAGE_LIMIT})",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Entries to skip: the offset the previous page named (default 0)",
                    },
                    "scope": SHARED_STATE_SCOPE_SCHEMA,
                },
                "required": [],
            },
            source="builtin:coordination",
        ),
        ToolDefinition(
            name="resolve_shared_state",
            description=(
                "Mark a shared-state entry RESOLVED once the thing it tracks has "
                "concluded (e.g. the order shipped, the incident closed). This is "
                "the correct way to retire an entry - it drops out of active reads, "
                "keeps a short audit trail, then the retention sweep purges it. Do "
                "NOT signal done-ness by writing a status into the value. You own "
                "keeping your shared state tidy: resolve when the thing is done."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Exact state key to resolve"},
                    "note": {"type": "string", "description": "Optional closing note appended to the annotation log"},
                },
                "required": ["key"],
            },
            source="builtin:coordination",
        ),
        ToolDefinition(
            name="delete_shared_state",
            description=(
                "Permanently PURGE a shared-state entry the fleet no longer needs "
                "(e.g. a one-off or obsolete record). Use this - not a status in "
                "the value - to clean up: shared state is not a write-only log, and "
                "you are responsible for removing entries that are no longer useful "
                "so stale state never accumulates. Prefer resolve_shared_state when "
                "the entry still has audit value; delete when it is simply useless."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Exact state key to purge"},
                },
                "required": ["key"],
            },
            source="builtin:coordination",
        ),
    ]

    return definitions, executor
