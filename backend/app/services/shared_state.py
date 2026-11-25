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

"""Shared-state service - generic cross-run state store.

A SharedState entry is a durable, fleet-scoped ``key -> value`` record so agents
share "the state of things" across runs instead of cold-starting. It is domain
agnostic: the key is any caller-chosen identifier and the value is a free-form
JSON bag (the shared-blackboard pattern).

``upsert_state`` is the workhorse: it creates the entry on first touch and merges
on subsequent touches (shallow-merge of ``value``, optional ``note`` append), so
an agent never has to branch on "exists / not". It also appends to and removes
from a list held under a top-level key (``append`` / ``remove``) against
the value as stored, under a row lock, so two runs keeping one list never drop
each other's items by rewriting it whole.

A company-managed entry is read by everyone and written only by a
company-managed writer (``services/company_managed_writes.py``): every write
function takes the ``writer`` and refuses before anything is written. The agent
that owns a company-managed entry may narrow which agents read and write it
(its ``coordination_access``).

An entry filed into a workspace (``workspace_id``) is governed by that
workspace instead: everyone who can see the workspace reads it, and writing it
needs the workspace's `config` tier (``services/workspaces.py``,
``workspace_scope_tiers``).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, false, func, not_, or_, select, true
from sqlalchemy import case as sa_case
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.utils import normalize_correlation_key, safe_uuid
from app.models.shared_state import (
    SHARED_STATE_ACTIVE_STATUS,
    SHARED_STATE_RESOLVED_STATUS,
    SHARED_STATE_STATUSES,
    SharedState,
)
from app.services.company_managed_writes import (
    DEFAULT_ACCESS,
    READ,
    WRITE,
    CompanyManagedKeyHiddenError,
    CompanyManagedWriter,
    CompanyManagedWriteRefusedError,
    CoordinationAccess,
    access_of,
    coordination_admits,
    load_coordination_access,
    refuse_company_managed_write,
    refuse_restricted_write,
    restricted_governors,
)
from app.services.ownership_scope import ownership_clause

logger = logging.getLogger(__name__)

_SUBJECT = "shared state"
# Who governs a company-managed entry, as a refusal names it.
_GOVERNOR = "its owner agent"


class WorkspaceWriteRefusedError(CompanyManagedWriteRefusedError):
    """A write of a workspace's shared state by a caller who may only read it.

    A subclass so every surface that already answers a refused write (the
    REST 403, the tools' explicit refusal) answers this one too.
    """


_WORKSPACE_WRITE_REFUSED = (
    "You have read-only access to this workspace's shared state"
)


def _narrowed_clause(actor: CompanyManagedWriter, mode: str):
    """Company-managed entries whose owner agent keeps *actor* out: the SQL
    mirror of :func:`_admits`."""
    return and_(
        SharedState.is_company_managed.is_(True),
        SharedState.owner_agent_id.is_not(None),
        SharedState.owner_agent_id.in_(restricted_governors(actor, mode=mode)),
    )


def _narrows(actor: CompanyManagedWriter | None) -> bool:
    return actor is not None and actor.agent_id is not None


async def _governor_access(
    db, actor: CompanyManagedWriter, state: SharedState
) -> CoordinationAccess:
    """The narrowing the entry's owner agent set, when an agent acts on a
    company-managed entry."""
    governor = state.owner_agent_id
    if actor.agent_id is None or not state.is_company_managed or governor is None:
        return DEFAULT_ACCESS
    return access_of(await load_coordination_access(db, [governor]), governor)


def _admits(
    access: CoordinationAccess, actor: CompanyManagedWriter, state: SharedState, mode: str
) -> bool:
    return not state.is_company_managed or coordination_admits(
        access, actor, mode=mode, governor_id=state.owner_agent_id
    )


async def _gate_write(
    db, writer: CompanyManagedWriter, state: SharedState, action: str
) -> CoordinationAccess | None:
    """Decide a write of a visible *state*: ``None`` when its owner agent hides
    it from this agent (the caller answers "not found"), the owner's narrowing
    when the write may go ahead; raises when it is refused."""
    access = await _governor_access(db, writer, state)
    if not _admits(access, writer, state, READ):
        return None
    refuse_company_managed_write(
        writer,
        is_company_managed=bool(state.is_company_managed),
        action=action,
        subject=_SUBJECT,
    )
    if state.is_company_managed:
        refuse_restricted_write(
            access,
            writer,
            governor_id=state.owner_agent_id,
            action=action,
            subject=_SUBJECT,
            governor_label=_GOVERNOR,
        )
    return access


def _read_only(
    actor: CompanyManagedWriter, state: SharedState, access: CoordinationAccess
) -> bool:
    if not state.is_company_managed:
        return False
    return not actor.may_write or not _admits(access, actor, state, WRITE)


_HIDDEN_KEY = (
    "This key is already used by company-managed shared state that this agent "
    "cannot access"
)


async def _key_held_out_of_sight(db, uid: uuid.UUID | None, key: str) -> bool:
    """Whether the user scope's unique ``(user_id, key)`` slot is taken by an
    entry the agent's lookup could not reach (its owner agent keeps it out)."""
    if uid is None:
        return False
    return (
        await db.execute(
            select(SharedState.id).where(
                SharedState.user_id == uid,
                SharedState.key == key,
                SharedState.workspace_id.is_(None),
            )
        )
    ).first() is not None


def _key_workspace(key: str) -> uuid.UUID | None:
    """The workspace a ``workspace:<id>:...`` key names, or None.

    The namespace and the typed column must agree: a namespaced key
    with a NULL column falls under the per-user index instead of the
    workspace one, so two people in one workspace would each get their own
    row, and a caller could skip the workspace write gate by leaving the
    column out.
    """
    from app.services.workspaces import workspace_namespace

    prefix = workspace_namespace("")
    if not key.startswith(prefix):
        return None
    ref, sep, _tail = key[len(prefix):].partition(":")
    return safe_uuid(ref) if sep else None


# Cap the append-only notes log so a long-lived entry can't grow unbounded.
_MAX_NOTES = 200

# The stamp the platform puts on an object item it archives (``archive_key``):
# the archive retention sweep drops platform-archived items older than
# ``shared_state_archive_retention_days`` and never touches an item without it,
# so a caller's own ``archived_at`` field is never read as one.
ARCHIVE_STAMP = "_archived_at"


def value_size(value: object) -> int:
    """Characters of *value* serialised as JSON: the unit of the size limits."""
    if not value:
        return 0
    return len(json.dumps(value, default=str))


def largest_keys(value: object, count: int = 3) -> list[tuple[str, int, int | None]]:
    """The *count* largest top-level keys of *value*: (key, characters, items when a list)."""
    if not isinstance(value, dict):
        return []
    sizes = [
        (str(k), value_size(v), len(v) if isinstance(v, list) else None) for k, v in value.items()
    ]
    return sorted(sizes, key=lambda s: s[1], reverse=True)[:count]


def _keys_line(value: object) -> str:
    return ", ".join(
        f"{k} {chars} chars" + (f" ({items} items)" if items is not None else "")
        for k, chars, items in largest_keys(value)
    )


def size_limit_refusal(old_size: int, new_value: object, kept_open: dict[str, int] | None = None) -> str | None:
    """Why a write leaving *new_value* is refused, or None when it may land.

    The hard limit (``shared_state_value_hard_limit_chars``, 0 disables) refuses
    a value over it unless the write shrinks the value, so an entry already
    over the limit can always be compacted. *kept_open* maps a list to the open
    items a ``keep_last_json`` of the same write could not trim because the
    retention's ``archive_when`` protects them: the refusal then names them
    instead of advising the ``keep_last_json`` the caller already passed.
    """
    hard = settings.shared_state_value_hard_limit_chars
    if not hard or hard <= 0:
        return None
    new_size = value_size(new_value)
    if new_size <= hard or new_size < old_size:
        return None
    head = (
        f"the value would be {new_size} characters, over the hard limit of {hard} "
        f"(largest keys: {_keys_line(new_value)}); nothing was written. "
    )
    held = {name: count for name, count in (kept_open or {}).items() if count}
    if held:
        lists = ", ".join(f"{count} open item(s) of {name!r}" for name, count in held.items())
        return head + (
            f"keep_last_json trimmed only terminal items: it kept {lists}, which the "
            "retention's archive_when protects from any trim. Shrink it in the same write "
            "another way: move items to a terminal state archive_when matches, "
            "remove_json them (archive_key keeps a copy), or shorten their fields"
        )
    return head + (
        "Shrink it in the same write: keep_last_json with archive_key on the longest "
        "list, remove_json for terminal items, or unset_keys"
    )


def _list_lengths(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {str(k): len(v) for k, v in value.items() if isinstance(v, list)}


def list_cap_refusal(old_value: object, new_value: object) -> str | None:
    """Why a write leaving a list over ``shared_state_list_max_items`` is refused, or None.

    Only a list that grows past the cap is refused, so a list already over it
    can always be shortened (#11696).
    """
    cap = settings.shared_state_list_max_items
    if not cap or cap <= 0:
        return None
    before = _list_lengths(old_value)
    over = [
        (name, count) for name, count in _list_lengths(new_value).items() if count > cap and count > before.get(name, 0)
    ]
    if not over:
        return None
    lists = ", ".join(f"{name} {count} items" for name, count in over)
    return (
        f"the list would pass the cap of {cap} items ({lists}); nothing was written. "
        "Bound it in the same write: keep_last_json (with archive_key to keep a copy), "
        "remove_json for terminal items, or split the list into another key"
    )


def size_warning(value: object) -> str | None:
    """The read-back warning of a value over the soft limit, or None."""
    soft = settings.shared_state_value_soft_limit_chars
    if not soft or soft <= 0:
        return None
    size = value_size(value)
    if size <= soft:
        return None
    return (
        f"the value is {size} characters, over the soft limit of {soft} (largest keys: "
        f"{_keys_line(value)}). Bound the longest list: keep_last_json with archive_key "
        "(archived items are purged after the archive retention), or remove_json the "
        "terminal items"
    )


def _trim_to_hard_limit(value: dict, appended: dict[str, int]) -> int:
    """Drop the oldest items an archive already held until *value* fits the hard limit.

    Used on an archive write, so an archive never outgrows the limit. Every
    list of the archive is a candidate, not only the appended ones (an archive
    also holds the ``entries`` of unset archives), oldest :data:`ARCHIVE_STAMP`
    first, an unstamped item before any stamped one. The last ``appended[name]``
    items of each list are the ones this write archives: they are never dropped,
    whatever the size (#10813: a trim through the appended list lost the items
    the caller asked to keep). Answers the count dropped; raises ``ValueError``
    when the archive cannot fit without dropping what this write archives, so
    the archive write, and the write of the source behind it, are refused.
    """
    trimmed = _trim_archive_lists(value, appended)
    hard = settings.shared_state_value_hard_limit_chars
    if not hard or hard <= 0:
        return trimmed
    excess = value_size(value) - hard
    if excess <= 0:
        return trimmed
    candidates: list[tuple[str, str, int]] = []
    for name, items in value.items():
        if not isinstance(items, list):
            continue
        kept = min(len(items), max(0, appended.get(name, 0)))
        for index, item in enumerate(items[: len(items) - kept]):
            stamp = item.get(ARCHIVE_STAMP) if isinstance(item, dict) else None
            candidates.append((stamp if isinstance(stamp, str) else "", name, index))
    candidates.sort()
    cut: dict[str, set[int]] = {}
    for _stamp, name, index in candidates:
        if excess <= 0:
            break
        excess -= value_size(value[name][index]) + 2
        cut.setdefault(name, set()).add(index)
    if excess > 0:
        raise ValueError(
            "the archive cannot hold these items under the hard limit of "
            f"{hard} characters even with every older archived item dropped; "
            "nothing was written: archive fewer items per write, or to a new archive key"
        )
    for name, indexes in cut.items():
        value[name] = [item for i, item in enumerate(value[name]) if i not in indexes]
    return trimmed + sum(len(indexes) for indexes in cut.values())


def _trim_archive_lists(value: dict[str, object], appended: dict[str, int]) -> int:
    """Drop the oldest items of each archive list past ``shared_state_list_max_items``.

    An archive is bounded in items as well as in size (#11696): the items this
    write archives are kept, the oldest ones before them go first.
    """
    cap = settings.shared_state_list_max_items
    if not cap or cap <= 0:
        return 0
    dropped = 0
    for name, items in list(value.items()):
        if not isinstance(items, list) or len(items) <= cap:
            continue
        keep = max(cap, min(len(items), appended.get(name, 0)))
        dropped += len(items) - keep
        value[name] = items[len(items) - keep :]
    return dropped


def compact_value(value: dict[str, object]) -> tuple[dict[str, object], int]:
    """*value* cut to the hard limit and the list cap, oldest items first; the count dropped.

    The tick's compaction of an entry already over a limit (#11696): every
    top-level list is cut to ``shared_state_list_max_items`` from its start,
    then the oldest items go until the value fits the hard limit: the items
    nearest the start of their list first (a list is appended at its end),
    a platform-archived item by its stamp among them. Scalars and objects are
    never touched; a value whose lists cannot pay for the excess keeps them
    empty and is answered as it stands.
    """
    out = dict(value)
    dropped = _trim_archive_lists(out, {})
    hard = settings.shared_state_value_hard_limit_chars
    if not hard or hard <= 0:
        return out, dropped
    excess = value_size(out) - hard
    if excess <= 0:
        return out, dropped
    candidates: list[tuple[int, str, str, int]] = []
    for name, items in out.items():
        if not isinstance(items, list):
            continue
        for index, item in enumerate(items):
            stamp = item.get(ARCHIVE_STAMP) if isinstance(item, dict) else None
            candidates.append((index, stamp if isinstance(stamp, str) else "", name, value_size(item) + 2))
    candidates.sort()
    cut: dict[str, int] = {}
    for _index, _stamp, name, size in candidates:
        if excess <= 0:
            break
        excess -= size
        cut[name] = cut.get(name, 0) + 1
    for name, count in cut.items():
        out[name] = out[name][count:]
        dropped += count
    return out, dropped


_RETENTION_FIELDS = (
    "keep_last",
    "max_chars",
    "archive_key",
    "lists",
    "archive_when",
    "max_item_chars",
    "overflow_key",
    "overflow_fields",
    "item_key",
    "keep_fields",
    "roll_open",
)

# What a field moved to the overflow key of a retention holds instead (#12177).
OVERFLOW_POINTER = "overflow://"

# The share of a retention cap past which the reads and the read-back warn (#12035).
RETENTION_WARN_RATIO = 0.8


def retention_shape(policy: object) -> dict:
    """Check a retention policy (#11839); ``{}`` clears the policy of the entry.

    ``keep_last`` (items per list) and ``max_chars`` (characters of the value)
    bound the lists, at least one of them; ``archive_key`` receives what rolls
    out (without it the rolled items are dropped); ``lists`` names the
    top-level lists it governs (default: every top-level list).
    ``archive_when`` (#12035) is the terminal predicate: a match object, or a
    list of them (any matches), in the grammar of ``where_json`` and
    ``remove_json`` (:func:`list_item_matches`). With it only the items it
    matches ever roll, the oldest first; an open item is never moved.
    ``max_item_chars`` (#12177) bounds one item of a governed list: an item
    over it is refused, or, with ``overflow_key``, its longest fields move to
    that key (:func:`spill_items`), keyed by the list and the item's
    ``item_key`` field (default ``id``); ``overflow_fields`` names fields
    moved from every item whatever its size. ``keep_fields`` (#754) lets the
    open items themselves fit ``max_chars``: after the roll, their longest
    fields move to ``overflow_key`` (:func:`compact_open_items`); the named
    fields, the identifying field and the fields ``archive_when`` reads stay.
    ``roll_open`` (#1760, needs ``archive_key``) bounds a list of open items:
    once the terminal items cannot bring a list or the value under its bound,
    the oldest open items roll into the archive too (a copy, never a drop).
    """
    if policy is None or policy == {}:
        return {}
    if not isinstance(policy, dict):
        raise ValueError("retention must be an object, e.g. {\"keep_last\": 200, \"archive_key\": \"k-archive\"}")
    unknown = sorted(set(policy) - set(_RETENTION_FIELDS))
    if unknown:
        raise ValueError(f"retention takes {', '.join(_RETENTION_FIELDS)}; not {', '.join(unknown)}")
    out: dict = {}
    for name in ("keep_last", "max_chars", "max_item_chars"):
        count = policy.get(name)
        if count is None:
            continue
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError(f"retention {name} must be an integer of 1 or more")
        out[name] = count
    overflow_fields = policy.get("overflow_fields")
    if overflow_fields is not None:
        if (
            not isinstance(overflow_fields, list)
            or not overflow_fields
            or not all(isinstance(n, str) and n for n in overflow_fields)
        ):
            raise ValueError("retention overflow_fields must be a non-empty list of field names")
        out["overflow_fields"] = list(dict.fromkeys(overflow_fields))
    if not out:
        raise ValueError("retention needs keep_last, max_chars, max_item_chars or overflow_fields (or {} to clear it)")
    overflow_key = policy.get("overflow_key")
    if overflow_key is not None:
        if not isinstance(overflow_key, str) or not normalize_correlation_key(overflow_key):
            raise ValueError("retention overflow_key must be a shared state key")
        out["overflow_key"] = normalize_correlation_key(overflow_key)
    elif "overflow_fields" in out:
        raise ValueError("retention overflow_fields needs an overflow_key to move the fields to")
    keep_fields = policy.get("keep_fields")
    if keep_fields is not None:
        if (
            not isinstance(keep_fields, list)
            or not keep_fields
            or not all(isinstance(n, str) and n.strip() for n in keep_fields)
        ):
            raise ValueError("retention keep_fields must be a non-empty list of field names")
        if "overflow_key" not in out or "max_chars" not in out:
            raise ValueError(
                "retention keep_fields needs an overflow_key and a max_chars: the other fields of the open "
                "items move to the overflow key until the value fits max_chars"
            )
        out["keep_fields"] = list(dict.fromkeys(n.strip() for n in keep_fields))
    item_key = policy.get("item_key")
    if item_key is not None:
        if not isinstance(item_key, str) or not item_key.strip():
            raise ValueError("retention item_key must be the name of the field identifying an item")
        out["item_key"] = item_key.strip()
    archive_key = policy.get("archive_key")
    if archive_key is not None:
        if not isinstance(archive_key, str) or not normalize_correlation_key(archive_key):
            raise ValueError("retention archive_key must be a shared state key")
        out["archive_key"] = normalize_correlation_key(archive_key)
    lists = policy.get("lists")
    if lists is not None:
        if not isinstance(lists, list) or not lists or not all(isinstance(n, str) and n for n in lists):
            raise ValueError("retention lists must be a non-empty list of top-level list keys")
        out["lists"] = list(dict.fromkeys(lists))
    archive_when = policy.get("archive_when")
    if archive_when is not None:
        matches = archive_when if isinstance(archive_when, list) else [archive_when]
        if not matches or not all(isinstance(m, dict) and m for m in matches):
            raise ValueError(
                "retention archive_when must be a match object or a list of them, "
                'e.g. {"state": {"in": ["merged", "closed"]}}'
            )
        for match in matches:
            if (error := list_match_error(match)) is not None:
                raise ValueError(f"retention archive_when: {error}")
        out["archive_when"] = archive_when
    roll_open = policy.get("roll_open")
    if roll_open is not None:
        if not isinstance(roll_open, bool):
            raise ValueError("retention roll_open must be true or false")
        if roll_open and "archive_key" not in out:
            raise ValueError(
                "retention roll_open needs an archive_key: the open items it rolls are kept there, never dropped"
            )
        if roll_open:
            out["roll_open"] = True
    return out


def retention_protect(policy: dict[str, Any] | None, value: object) -> dict[str, object]:
    """The lists of *value* an ``archive_when`` retention protects, to their predicate (#12035)."""
    if not policy or policy.get("archive_when") is None:
        return {}
    names = policy.get("lists") or (list(value) if isinstance(value, dict) else [])
    return {name: policy["archive_when"] for name in names}


def is_terminal_item(item: object, archive_when: object) -> bool:
    """Whether a retention may move *item*: no predicate moves any item (by age)."""
    if archive_when is None:
        return True
    matches = archive_when if isinstance(archive_when, list) else [archive_when]
    return any(list_item_matches(item, m) for m in matches)


def retention_usage(value: object, policy: dict[str, Any] | None) -> dict[str, object] | None:
    """The value's size against its retention caps, and the warning past 80% (#12035).

    Answers ``None`` without a policy; else ``size_chars`` and ``max_chars``
    (with ``percent``) and ``warning`` when the value or a governed list is at
    ``RETENTION_WARN_RATIO`` of its cap or past it.
    """
    if not policy or not isinstance(value, dict):
        return None
    size = value_size(value)
    out: dict[str, object] = {"size_chars": size}
    warnings: list[str] = []
    cap = policy.get("max_chars")
    if isinstance(cap, int) and cap > 0:
        percent = round(100 * size / cap)
        out["max_chars"] = cap
        out["percent"] = percent
        if size >= RETENTION_WARN_RATIO * cap:
            warnings.append(f"{size} of {cap} characters ({percent}%)")
    keep = policy.get("keep_last")
    if isinstance(keep, int) and keep > 0:
        governed = policy.get("lists")
        for name in governed if isinstance(governed, list) else list(value):
            items = value.get(name)
            if isinstance(items, list) and len(items) >= RETENTION_WARN_RATIO * keep:
                warnings.append(f"{name} {len(items)} of {keep} items")
    if warnings:
        tail = (
            "items matching archive_when roll first, open items stay"
            if policy.get("archive_when") is not None
            else "the OLDEST items roll whatever their state: set archive_when to roll terminal items only"
        )
        out["warning"] = f"near or at the retention cap: {'; '.join(warnings)}; {tail}"
    return out


def _is_overflow_pointer(field_value: object) -> bool:
    return isinstance(field_value, str) and field_value.startswith(OVERFLOW_POINTER)


def parse_overflow_pointer(pointer: object, list_name: str) -> tuple[str, list[str]] | None:
    """The overflow key and the path under it that a pointer of *list_name* names (#12301).

    A pointer reads ``overflow://KEY/LIST/ITEM#FIELD`` (:func:`spill_items`)
    and its field is stored at ``LIST/ITEM/FIELD`` of ``KEY``. The key may hold
    slashes, so the list name the item was found in splits it. ``None`` when
    *pointer* is not such a pointer.
    """
    if not _is_overflow_pointer(pointer):
        return None
    body, sep, field = str(pointer)[len(OVERFLOW_POINTER) :].rpartition("#")
    marker = f"/{list_name}/"
    at = body.rfind(marker)
    if not sep or not field or at <= 0:
        return None
    target, item = body[:at], body[at + len(marker) :]
    if not item:
        return None
    return target, [list_name, item, field]


def append_at_path(value: dict[str, Any], path: Sequence[str], items: list[Any]) -> tuple[dict[str, Any], int]:
    """A copy of *value* with the *items* absent from the list under *path* appended, and their count (#12301).

    The objects along *path* and the list at its end are created when missing;
    anything else along it is refused with :class:`ValueError`, *value* left
    untouched.
    """
    head, rest = path[0], path[1:]
    out = dict(value)
    held = out.get(head)
    if rest:
        if held is None:
            held = {}
        if not isinstance(held, dict):
            raise ValueError(
                f"the value under {head!r} is not an object, so the path cannot go through it; nothing was written"
            )
        out[head], added = append_at_path(held, rest, items)
        return out, added
    if held is None:
        held = []
    if not isinstance(held, list):
        raise ValueError(f"the value under {head!r} is not a list, so it cannot be appended to; nothing was written")
    current = list(held)
    added = 0
    for entry in items:
        if entry not in current:
            current.append(entry)
            added += 1
    out[head] = current
    return out, added


def _spillable(field_value: object) -> bool:
    """Whether a field may move to the overflow key by size: text and containers, never a scalar (#1760).

    A number, a boolean or null is what an ``increment`` adds to and a
    ``where_json`` compares: as a pointer both would fail on the item.
    """
    return isinstance(field_value, (str, list, dict))


def spill_items(value: dict[str, Any], policy: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    """Bound each item of the policy's lists (in place); answer the fields moved, per list and item (#12177).

    An object item over ``max_item_chars`` moves its longest fields, one by
    one, until it fits; ``overflow_fields`` move from every item. Each moved
    field is replaced by a pointer (``overflow://KEY/LIST/ITEM#FIELD``) and
    answered under its list and the item's ``item_key`` value, for the caller
    to write to ``overflow_key``. A field already holding a pointer and the
    identifying field never move. Without ``overflow_key`` an item over the
    cap is refused (``ValueError``, nothing written), as is an item to spill
    that has no identifying value to key its overflow by. By size, a field
    moves only when it is text or a container longer than its pointer: a
    number, a boolean, a ``keep_fields`` field and a field ``archive_when``
    reads stay, so increments, filters and the terminal rule keep working.
    """
    cap = policy.get("max_item_chars")
    target = policy.get("overflow_key")
    forced = (policy.get("overflow_fields") or []) if target else []
    if not cap and not forced:
        return {}
    id_field = policy.get("item_key") or "id"
    # What filters, increments and the terminal rule read stays on the item (#1760).
    stay = set(policy.get("keep_fields") or ()) | _archive_rule_fields(policy.get("archive_when"))
    names = [n for n in (policy.get("lists") or list(value)) if isinstance(value.get(n), list)]
    spilled: dict[str, dict[str, dict[str, Any]]] = {}
    for name in names:
        items = list(value[name])
        changed = False
        for index, item in enumerate(items):
            size = value_size(item)
            over = isinstance(cap, int) and size > cap
            if not isinstance(item, dict):
                if over:
                    raise ValueError(
                        f"item {index} of list {name!r} is {size} characters, over the retention "
                        f"max_item_chars of {cap}, and is not an object whose fields could move; "
                        "nothing was written"
                    )
                continue
            movable = [f for f in item if f != id_field and not _is_overflow_pointer(item[f])]
            wanted = [f for f in forced if f in movable]
            if not over and not wanted:
                continue
            if not target:
                raise ValueError(
                    f"item {index} of list {name!r} is {size} characters, over the retention "
                    f"max_item_chars of {cap}: shorten it, or set overflow_key in the retention "
                    "to move its longest fields there; nothing was written"
                )
            identity = item.get(id_field)
            if isinstance(identity, bool) or not isinstance(identity, (str, int)) or str(identity) == "":
                raise ValueError(
                    f"item {index} of list {name!r} has no {id_field!r} value to key its overflow "
                    "by: give it one, or set item_key in the retention; nothing was written"
                )
            new_item = dict(item)
            moved: dict[str, Any] = {}
            for field in wanted:
                moved[field] = new_item[field]
                new_item[field] = f"{OVERFLOW_POINTER}{target}/{name}/{identity}#{field}"
            if cap:
                pointer_size = len(json.dumps(f"{OVERFLOW_POINTER}{target}/{name}/{identity}#"))
                rest = sorted(
                    (
                        f
                        for f in movable
                        if f not in moved
                        and f not in stay
                        and _spillable(new_item[f])
                        and value_size(new_item[f]) > pointer_size + len(f)
                    ),
                    key=lambda f: value_size(new_item[f]),
                    reverse=True,
                )
                for field in rest:
                    if value_size(new_item) <= cap:
                        break
                    moved[field] = new_item[field]
                    new_item[field] = f"{OVERFLOW_POINTER}{target}/{name}/{identity}#{field}"
            if moved:
                items[index] = new_item
                changed = True
                spilled.setdefault(name, {}).setdefault(str(identity), {}).update(moved)
        if changed:
            value[name] = items
    return spilled


def _archive_rule_fields(archive_when: object) -> set[str]:
    """The top-level item fields an ``archive_when`` predicate reads (#754)."""
    matches = archive_when if isinstance(archive_when, list) else [archive_when]
    return {str(field).split(".", 1)[0] for m in matches if isinstance(m, dict) for field in m}


def compact_open_items(value: dict[str, Any], policy: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    """Move the longest fields of the open items to the overflow key until the value fits (#754).

    Runs after :func:`roll_lists`, only with ``keep_fields``: while the value
    is over ``max_chars`` (or the hard limit, whichever is lower), the
    largest field of any item of a governed list moves to ``overflow_key``,
    leaving an ``overflow://`` pointer, the largest first and only while it
    saves characters. The identifying field, the ``keep_fields``, the fields
    ``archive_when`` reads and a field already holding a pointer never move,
    so filters and the terminal rule still read the item. Answers the moved
    fields as :func:`spill_items` does, for the caller to write.
    """
    keep = policy.get("keep_fields")
    target = policy.get("overflow_key")
    if not keep or not target:
        return {}
    limits = [c for c in (policy.get("max_chars"), settings.shared_state_value_hard_limit_chars) if c and c > 0]
    if not limits:
        return {}
    excess = value_size(value) - min(limits)
    if excess <= 0:
        return {}
    id_field = policy.get("item_key") or "id"
    stay = set(keep) | {id_field} | _archive_rule_fields(policy.get("archive_when"))
    names = [n for n in (policy.get("lists") or list(value)) if isinstance(value.get(n), list)]
    candidates: list[tuple[int, str, int, str]] = []
    for name in names:
        for index, item in enumerate(value[name]):
            if not isinstance(item, dict):
                continue
            identity = item.get(id_field)
            if isinstance(identity, bool) or not isinstance(identity, (str, int)) or str(identity) == "":
                continue
            for field, field_value in item.items():
                if field in stay or _is_overflow_pointer(field_value):
                    continue
                pointer = f"{OVERFLOW_POINTER}{target}/{name}/{identity}#{field}"
                saving = value_size(field_value) - len(json.dumps(pointer))
                if saving > 0:
                    candidates.append((saving, name, index, field))
    candidates.sort(key=lambda c: c[0], reverse=True)
    spilled: dict[str, dict[str, dict[str, Any]]] = {}
    for saving, name, index, field in candidates:
        if excess <= 0:
            break
        items = value[name]
        item = dict(items[index])
        identity = str(item[id_field])
        spilled.setdefault(name, {}).setdefault(identity, {})[field] = item[field]
        item[field] = f"{OVERFLOW_POINTER}{target}/{name}/{identity}#{field}"
        items[index] = item
        excess -= saving
    return spilled


# At most this many overflow keys are read to resolve the pointers of one read.
OVERFLOW_RESOLVE_MAX_KEYS = 10


def overflow_part_key(base: str, part: int) -> str:
    """The key of overflow part *part* of *base*: the base itself, then ``BASE:part-N``."""
    return base if part <= 1 else f"{base}:part-{part}"


def repoint_spilled(
    value: dict[str, Any],
    spilled: dict[str, dict[str, dict[str, Any]]],
    old_key: str,
    new_key: str,
    id_field: str,
) -> int:
    """Point the fields of *spilled* at *new_key* instead of *old_key* in *value* (in place); the count.

    Used when an overflow part is full and the spill rolls to the next part:
    only the pointers this write made (they name *old_key*) change.
    """
    count = 0
    for name, items in spilled.items():
        held = value.get(name)
        if not isinstance(held, list):
            continue
        for index, item in enumerate(held):
            if not isinstance(item, dict) or str(item.get(id_field)) not in items:
                continue
            identity = str(item.get(id_field))
            new_item = dict(item)
            for field in items[identity]:
                if new_item.get(field) == f"{OVERFLOW_POINTER}{old_key}/{name}/{identity}#{field}":
                    new_item[field] = f"{OVERFLOW_POINTER}{new_key}/{name}/{identity}#{field}"
                    count += 1
            held[index] = new_item
    return count


def split_overflow_pointer(field_value: object) -> tuple[str, str, str, str] | None:
    """The (key, list, item, field) an ``overflow://KEY/LIST/ITEM#FIELD`` pointer names, or None.

    Unlike :func:`parse_overflow_pointer`, the list is not known: the list and
    the item are the last two path segments, so a key holding a ``/`` still
    parses; a value that is not a well-formed pointer answers None.
    """
    if not _is_overflow_pointer(field_value):
        return None
    body, sep, field = str(field_value)[len(OVERFLOW_POINTER):].rpartition("#")
    if not sep or not field:
        return None
    parts = body.rsplit("/", 2)
    if len(parts) != 3 or not all(parts):
        return None
    key, name, item = parts
    return key, name, item, field


def overflow_pointer_keys(value: object) -> list[str]:
    """The overflow keys the pointers anywhere in *value* name, in first-seen order."""
    keys: list[str] = []

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            for child in node.values():
                _walk(child)
        elif isinstance(node, list):
            for child in node:
                _walk(child)
        else:
            parsed = split_overflow_pointer(node)
            if parsed is not None and parsed[0] not in keys:
                keys.append(parsed[0])

    _walk(value)
    return keys


def resolve_overflow_pointers(value: object, overflow: dict[str, object]) -> tuple[object, int, int]:
    """Answer a copy of *value* with each overflow pointer replaced by the field it names.

    *overflow* maps an overflow key to its stored value, which holds the moved
    fields under ``{list: {item: {field: value}}}`` (:func:`spill_items`). A
    pointer whose key, list, item or field is absent stays as it is. Answers
    the copy, the count of pointers resolved and the count left unresolved.
    """
    counts = [0, 0]

    def _walk(node: object) -> object:
        if isinstance(node, dict):
            return {k: _walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_walk(v) for v in node]
        parsed = split_overflow_pointer(node)
        if parsed is None:
            return node
        key, name, item, field = parsed
        stored = overflow.get(key)
        fields = stored.get(name, {}).get(item) if isinstance(stored, dict) and isinstance(stored.get(name), dict) else None
        if isinstance(fields, dict) and field in fields:
            counts[0] += 1
            return fields[field]
        counts[1] += 1
        return node

    return _walk(value), counts[0], counts[1]


def _fold_shape(fold: dict | None) -> dict[str, dict[str, tuple[str, ...]]]:
    """Check a ``fold`` argument: list keys to identity field(s), or to ``{"by": ..., "sum": [...]}``."""
    if not fold:
        return {}
    if not isinstance(fold, dict):
        raise ValueError("fold must be an object of top-level list keys to the identifying field(s)")
    out: dict[str, dict[str, tuple[str, ...]]] = {}
    for key, spec in fold.items():
        if not isinstance(key, str) or not key:
            raise ValueError("fold keys must be non-empty strings")
        by = spec.get("by") if isinstance(spec, dict) else spec
        total = (spec.get("sum") or []) if isinstance(spec, dict) else []
        if isinstance(spec, dict) and set(spec) - {"by", "sum"}:
            raise ValueError(f"fold[{key!r}] takes only 'by' and 'sum'")
        names = [by] if isinstance(by, str) else by
        if not isinstance(names, (list, tuple)) or not names or not all(isinstance(n, str) and n for n in names):
            raise ValueError(f"fold[{key!r}] must name the identifying field(s): a field name or a list of them")
        # A tuple is the shape answered here: checking it again (upsert_state
        # then apply_list_ops) is a no-op.
        if not isinstance(total, (list, tuple)) or not all(isinstance(n, str) and n for n in total):
            raise ValueError(f"fold[{key!r}].sum must be a list of field names")
        out[key] = {"by": tuple(names), "sum": tuple(total)}
    return out


def _fold_into(base: dict, other: dict, sums: tuple[str, ...]) -> dict:
    """Merge *other* into a copy of *base*: lists unioned, *sums* added, missing fields taken."""
    out = dict(base)
    for field, theirs in other.items():
        if field not in out or out[field] is None:
            out[field] = theirs
            continue
        ours = out[field]
        if field in sums and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in (ours, theirs)
        ):
            out[field] = ours + theirs
        elif isinstance(ours, list) and isinstance(theirs, list):
            out[field] = ours + [v for v in theirs if v not in ours]
    return out


def fold_list_items(items: list, by: tuple[str, ...], sums: tuple[str, ...] = ()) -> tuple[list, int]:
    """Fold every object item sharing the values of *by* into the first of them; answer the list and the count folded.

    The first item keeps its position; a later one is merged into it
    (:func:`_fold_into`) and dropped. Items without every *by* field, and
    non-object items, are kept as they are.
    """
    out: list = []
    first: dict[str, int] = {}
    folded = 0
    for item in items:
        identity = _identity(item, by)
        if identity is None:
            out.append(item)
            continue
        marker = json.dumps(identity, sort_keys=True, default=str)
        if marker in first:
            out[first[marker]] = _fold_into(out[first[marker]], item, sums)
            folded += 1
            continue
        first[marker] = len(out)
        out.append(item)
    return out, folded


def unmatched_archive_rule(value: object, policy: dict[str, Any] | None) -> object | None:
    """The ``archive_when`` of *policy* when it matches no item while the value is over ``max_chars`` (#12177)."""
    if not policy or not isinstance(value, dict):
        return None
    rule: object = policy.get("archive_when")
    cap = policy.get("max_chars")
    if policy.get("roll_open"):
        # The open items roll too: a rule matching nothing blocks no roll (#1760).
        return None
    if rule is None or not isinstance(cap, int) or value_size(value) <= cap:
        return None
    names = [n for n in (policy.get("lists") or list(value)) if isinstance(value.get(n), list)]
    if any(is_terminal_item(item, rule) for name in names for item in value[name]):
        return None
    return rule


def size_caps(value: object) -> dict[str, int]:
    """The value's size against the platform's soft and hard limits (#12177)."""
    return {
        "size_chars": value_size(value),
        "soft_limit_chars": int(settings.shared_state_value_soft_limit_chars or 0),
        "hard_limit_chars": int(settings.shared_state_value_hard_limit_chars or 0),
    }


def _drop_indexes(items: list[Any], indexes: set[int]) -> list[Any]:
    return [item for i, item in enumerate(items) if i not in indexes]


def _refuse_unarchived_roll(rolled: dict[str, list], appended: dict[str, int]) -> None:
    """Refuse a retention roll that drops, with no ``archive_key``, more items than the call appends.

    Without an archive the rolled items are gone: a policy set on a list
    already over its bound would drop its oldest items in one write, as
    ``keep_last_json`` would without ``confirm_trim``. Nothing is written.
    """
    for name, items in rolled.items():
        added = appended.get(name, 0)
        if len(items) > added:
            raise ValueError(
                f"retention would roll {len(items)} item(s) out of {name!r} with no archive_key while "
                f"this call appends {added}: they would be dropped. Set archive_key in the policy, "
                "or pass confirm_trim=true to drop them; nothing was written"
            )


def roll_lists(
    value: dict[str, Any],
    policy: dict[str, Any],
    appended: dict[str, int] | None = None,
    held: dict[str, int] | None = None,
) -> dict[str, list[Any]]:
    """Roll the oldest movable items of the policy's lists out of *value* (in place); answer them per list.

    Each governed list keeps at most ``keep_last`` items (and never more than
    ``shared_state_list_max_items``); then, while the value passes ``max_chars``
    or the hard limit, whichever is lower, the oldest items go, the items
    nearest the start of their list first. The last ``appended[name]`` items
    of a list are the ones this write appends: they are never rolled out, so
    a write that cannot fit even so is still refused by the size limit.
    With ``archive_when`` (#12035) only the items it matches are movable: an
    open item is never rolled, and ``held`` receives, per list, the count of
    open items kept past a bound because terminal items alone could not
    bring the list or the value under it.
    """
    appended = appended or {}
    archive_when = policy.get("archive_when")
    roll_open = bool(policy.get("roll_open")) and archive_when is not None
    names = [
        n for n in (policy.get("lists") or list(value)) if isinstance(value.get(n), list)
    ]

    def movable(name: str) -> list[int]:
        # With roll_open (#1760) the terminal items come first, then the
        # oldest open ones: an open list is bounded, its items archived.
        items = value[name]
        tail = min(len(items), appended.get(name, 0))
        terminal = [i for i in range(len(items) - tail) if is_terminal_item(items[i], archive_when)]
        if not roll_open:
            return terminal
        done = set(terminal)
        return terminal + [i for i in range(len(items) - tail) if i not in done]

    rolled: dict[str, list[Any]] = {}
    bounds = [
        b for b in (policy.get("keep_last"), settings.shared_state_list_max_items) if b and b > 0
    ]
    if bounds:
        bound = min(bounds)
        for name in names:
            items = value[name]
            excess_items = len(items) - max(bound, min(len(items), appended.get(name, 0)))
            if excess_items <= 0:
                continue
            picked = movable(name)[:excess_items]
            if picked:
                rolled[name] = [items[i] for i in picked]
                value[name] = _drop_indexes(items, set(picked))
            if held is not None and len(picked) < excess_items:
                held[name] = held.get(name, 0) + excess_items - len(picked)
    limits = [
        c for c in (policy.get("max_chars"), settings.shared_state_value_hard_limit_chars) if c and c > 0
    ]
    if not limits:
        return rolled
    excess = value_size(value) - min(limits)
    if excess <= 0:
        return rolled
    candidates = (
        sorted((rank, index, name) for name in names for rank, index in enumerate(movable(name)))
        if roll_open
        else sorted((0, index, name) for name in names for index in movable(name))
    )
    cut: dict[str, set[int]] = {}
    for _rank, index, name in candidates:
        if excess <= 0:
            break
        excess -= value_size(value[name][index]) + 2
        cut.setdefault(name, set()).add(index)
    for name, indexes in cut.items():
        rolled.setdefault(name, []).extend(value[name][i] for i in sorted(indexes))
        value[name] = _drop_indexes(value[name], indexes)
    if excess > 0 and held is not None and archive_when is not None:
        for name in names:
            tail = min(len(value[name]), appended.get(name, 0))
            still_open = sum(
                1 for item in value[name][: len(value[name]) - tail] if not is_terminal_item(item, archive_when)
            )
            if still_open:
                held[name] = max(held.get(name, 0), still_open)
    return rolled


def fit_archive(value: dict, appended: dict[str, int]) -> int:
    """Fit an archive receiving rolled items under the hard limit; answer the count dropped.

    The oldest items the archive already held go first (:func:`_trim_to_hard_limit`);
    when the rolled items alone do not fit, the oldest of them go too: an
    archive at its cap drops its oldest entries, it never refuses a roll.
    """
    try:
        return _trim_to_hard_limit(value, appended)
    except ValueError:
        pass
    dropped = 0
    for name, items in list(value.items()):
        if not isinstance(items, list):
            continue
        keep = min(len(items), appended.get(name, 0))
        dropped += len(items) - keep
        value[name] = items[len(items) - keep :]
    hard = settings.shared_state_value_hard_limit_chars
    while hard and hard > 0 and value_size(value) > hard:
        lists = [n for n, items in value.items() if isinstance(items, list) and items]
        if not lists:
            break
        longest = max(lists, key=lambda n: value_size(value[n]))
        value[longest] = value[longest][1:]
        dropped += 1
    return dropped


# An upsert whose row is deleted between its lookup and its lock looks the key
# up again (and creates it); bounded so a delete storm cannot spin it.
_UPSERT_ATTEMPTS = 3


def _list_ops_shape(ops: dict | None, name: str) -> dict[str, list]:
    """Check an ``append`` / ``remove`` argument: top-level keys to lists."""
    if not ops:
        return {}
    if not isinstance(ops, dict):
        raise ValueError(f"{name} must be an object of top-level keys to lists")
    for key, items in ops.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"{name} keys must be non-empty strings")
        if not isinstance(items, list):
            raise ValueError(f"{name}[{key!r}] must be a list of items")
    return ops


def list_item_matches(item: object, match: object) -> bool:
    """A list item match: equal, or an object whose fields include the match's.

    The rule of ``remove``, of ``update``'s ``match`` and of the ``where`` read
    of ``get_shared_state``, so a read selects exactly what a remove drops. A
    field's value may be an operator object of the event filter grammar
    (``{"starts_with": "PASS"}``, ``{"exists": false}``); any other value is
    equality on a present field.
    """
    if isinstance(match, dict) and match:
        if not isinstance(item, dict):
            return False
        from app.services.integration_event_filters import field_matches

        return all(field_matches(item, k, v) for k, v in match.items())
    return item == match


def list_match_error(match: object) -> str | None:
    """Why an object *match* cannot be evaluated (a bad operator object); ``None`` when it can."""
    if not isinstance(match, dict):
        return None
    from app.services.integration_event_filters import match_value_error

    for key, value in match.items():
        if (error := match_value_error(str(key), value)) is not None:
            return error
    return None


def _keep_last_shape(keep_last: dict | None) -> dict[str, int]:
    """Check a ``keep_last`` argument: top-level keys to a count of 0 or more."""
    if not keep_last:
        return {}
    if not isinstance(keep_last, dict):
        raise ValueError("keep_last must be an object of top-level keys to counts")
    for key, count in keep_last.items():
        if not isinstance(key, str) or not key:
            raise ValueError("keep_last keys must be non-empty strings")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(f"keep_last[{key!r}] must be an integer of 0 or more")
    return keep_last


def _unique_by_shape(unique_by: dict | None) -> dict[str, tuple[str, ...]]:
    """Check a ``unique_by`` argument: top-level list keys to one field name or a list of them."""
    if not unique_by:
        return {}
    if not isinstance(unique_by, dict):
        raise ValueError("unique_by must be an object of top-level list keys to a field name or a list of field names")
    out: dict[str, tuple[str, ...]] = {}
    for key, fields in unique_by.items():
        if not isinstance(key, str) or not key:
            raise ValueError("unique_by keys must be non-empty strings")
        # A tuple is the shape this function answers: checking an already
        # checked argument again (upsert_state then apply_list_ops) is a no-op.
        names = [fields] if isinstance(fields, str) else fields
        if not isinstance(names, (list, tuple)) or not names or not all(isinstance(n, str) and n for n in names):
            raise ValueError(f"unique_by[{key!r}] must be a field name or a non-empty list of field names")
        out[key] = tuple(names)
    return out


_UPDATE_OPS = ("set", "increment", "append", "unset")


def _update_shape(update: dict | None) -> dict[str, list[dict]]:
    """Check an ``update`` argument: top-level list keys to a patch or a list of patches.

    A patch is ``{"match": {...}, "set": {...}, "increment": {...}, "append": {...},
    "unset": [...]}``:
    ``match`` is a non-empty object (the rule of ``remove``) or a non-empty
    list of them (an item matching any is patched; each must match one item),
    and at least one of the operations is given, each an object of field names
    (``unset``: a list of field names). An increment is a number; an append is
    a list of items; an unset field is removed from the matched item.
    """
    if not update:
        return {}
    if not isinstance(update, dict):
        raise ValueError("update must be an object of top-level list keys to patches")
    out: dict[str, list[dict]] = {}
    for key, patches in update.items():
        if not isinstance(key, str) or not key:
            raise ValueError("update keys must be non-empty strings")
        listed = [patches] if isinstance(patches, dict) else patches
        if not isinstance(listed, list) or not listed:
            raise ValueError(f"update[{key!r}] must be a patch object or a non-empty list of them")
        for patch in listed:
            if not isinstance(patch, dict):
                raise ValueError(f"update[{key!r}] must be a patch object or a non-empty list of them")
            unknown = set(patch) - {"match", *_UPDATE_OPS}
            if unknown:
                raise ValueError(f"update[{key!r}] has unknown field(s): {', '.join(sorted(unknown))}")
            match = patch.get("match")
            matches = match if isinstance(match, list) else [match]
            if not matches or not all(isinstance(m, dict) and m for m in matches):
                raise ValueError(
                    f"update[{key!r}] needs a non-empty 'match' object or a non-empty list of them"
                )
            if not any(patch.get(op) for op in _UPDATE_OPS):
                raise ValueError(
                    f"update[{key!r}] needs at least one of 'set', 'increment', 'append', 'unset'"
                )
            unset = patch.get("unset")
            if unset is not None and (
                not isinstance(unset, list) or not all(isinstance(f, str) and f for f in unset)
            ):
                raise ValueError(f"update[{key!r}].unset must be a list of field names")
            for op in _UPDATE_OPS:
                fields = patch.get(op)
                if fields is None or op == "unset":
                    continue
                if not isinstance(fields, dict) or not all(isinstance(f, str) and f for f in fields):
                    raise ValueError(f"update[{key!r}].{op} must be an object of field names")
            for field, step in (patch.get("increment") or {}).items():
                if isinstance(step, bool) or not isinstance(step, (int, float)):
                    raise ValueError(f"update[{key!r}].increment[{field!r}] must be a number")
            for field, items in (patch.get("append") or {}).items():
                if not isinstance(items, list):
                    raise ValueError(f"update[{key!r}].append[{field!r}] must be a list of items")
        out[key] = listed
    return out


def _patch_item(
    item: dict[str, Any],
    patch: dict[str, Any],
    key: str,
    overflow_appends: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A copy of *item* with *patch*'s unset, set, increment and append applied.

    An append on a field a retention moved to its overflow key (an
    ``overflow://`` pointer, :func:`spill_items`) keeps the pointer and goes to
    *overflow_appends* (the pointer, the list, the field, the items), for the
    write to append them to the list stored there (#12301).
    """
    out = dict(item)
    for field in patch.get("unset") or ():
        out.pop(field, None)
    out.update(patch.get("set") or {})
    for field, step in (patch.get("increment") or {}).items():
        current = out.get(field, 0)
        if current is None:
            current = 0
        if isinstance(current, bool) or not isinstance(current, (int, float)):
            raise ValueError(
                f"{key!r}: the field {field!r} of a matched item is not a number, so it "
                "cannot be incremented; nothing was written"
            )
        out[field] = current + step
    for field, items in (patch.get("append") or {}).items():
        current = out.get(field)
        if _is_overflow_pointer(current):
            if overflow_appends is not None:
                overflow_appends.append({"pointer": current, "list": key, "field": field, "items": list(items)})
            continue
        if current is None:
            current = []
        if not isinstance(current, list):
            raise ValueError(
                f"{key!r}: the field {field!r} of a matched item is not a list, so it "
                "cannot be appended to; nothing was written"
            )
        current = list(current)
        for entry in items:
            if entry not in current:
                current.append(entry)
        out[field] = current
    return out


#: Fields that identify an item on their own, kept in an unset archive's
#: ``match`` whatever the patch matched on (a bulk unset matched on a status).
_UNSET_ARCHIVE_IDENTITY_FIELDS = ("id", "key", "slug")


def unset_archive(
    value: dict,
    update: dict | None,
    *,
    source_key: str,
    at: str,
    item_key: str | None = None,
) -> list[dict]:
    """The archive objects an ``update``'s ``unset`` would remove from *value*.

    One object per matched item that holds at least one of the unset fields:
    ``{source_key, list, match, archived_at, fields}``, ``match`` being the
    item's values of the patch's match fields plus its own identity (the
    retention's *item_key*, ``id``, ``key`` or ``slug`` when it holds them),
    so an item unset by a bulk match such as ``{"state": "done"}`` is still
    found in the archive by its key, and ``fields`` the values the unset
    drops. The caller appends them to an archive key before it writes the
    update, so a field leaves the live list only once
    its text is stored elsewhere. Nothing to unset answers an empty list.
    """
    archived: list[dict] = []
    for key, patches in _update_shape(update).items():
        items = value.get(key) if isinstance(value, dict) else None
        if not isinstance(items, list):
            continue
        for patch in patches:
            fields = patch.get("unset") or []
            if not fields:
                continue
            matches = patch["match"] if isinstance(patch["match"], list) else [patch["match"]]
            for item in items:
                if not isinstance(item, dict) or not any(list_item_matches(item, m) for m in matches):
                    continue
                removed = {f: item[f] for f in fields if f in item}
                if not removed:
                    continue
                identity = {f: item[f] for m in matches for f in m if f in item}
                for f in (item_key, *_UNSET_ARCHIVE_IDENTITY_FIELDS):
                    if f and f in item and f not in identity:
                        identity[f] = item[f]
                archived.append(
                    {
                        "source_key": source_key,
                        "list": key,
                        "match": identity,
                        "archived_at": at,
                        "fields": removed,
                    }
                )
    return archived


def _replacement_record(
    old: object, new: dict[str, Any], fields: tuple[str, ...] | None, identity: list[Any] | None
) -> dict[str, Any]:
    """What a ``unique_by`` replacement overwrote: the identity, the fields lost and changed."""
    old_fields = old if isinstance(old, dict) else {}
    return {
        "identity": dict(zip(fields or (), identity or [], strict=False)),
        "lost": sorted(k for k in old_fields if k not in new),
        "changed": sorted(k for k in old_fields if k in new and old_fields[k] != new[k]),
    }


def _identity(item: object, fields: tuple[str, ...] | None) -> list | None:
    """The values of *fields* in an object item, or None when it lacks one (or no fields)."""
    if not fields or not isinstance(item, dict) or not all(f in item for f in fields):
        return None
    return [item[f] for f in fields]


def apply_list_ops(
    value: dict,
    append: dict | None = None,
    remove: dict | None = None,
    keep_last: dict | None = None,
    unique_by: dict | None = None,
    update: dict | None = None,
    guard_trim: bool = False,
    dropped: dict[str, list] | None = None,
    protect: dict[str, object] | None = None,
    overflow_appends: list[dict[str, Any]] | None = None,
    fold: dict | None = None,
    replaced_items: dict[str, list[dict[str, Any]]] | None = None,
) -> tuple[dict, dict[str, dict[str, int]]]:
    """Apply list removals, field updates, appends, folds then trims to *value*; answer it and the counts.

    ``remove`` drops every item of the list under each key that equals a match,
    or, for an object match, every object item carrying all the match's fields.
    ``append`` adds each item not already in the list (equality), creating the
    list when the key is missing. ``unique_by`` names, per list, the field(s)
    that identify an object item: an appended object carrying them replaces in
    place every existing object with the same values (the first keeps its
    position, further copies are dropped and counted as removed), counted as
    ``replaced`` when it changed something, so a re-run append never leaves a
    near-duplicate. ``update`` patches, after the removals and before the
    appends, every object item carrying all the fields of a patch's ``match``
    (the rule of ``remove``): ``unset`` drops fields, ``set`` replaces fields, ``increment`` adds to
    numeric ones (a missing field starts at 0), ``append`` adds items absent
    from a list field (created when missing); counted as ``updated``. A patch
    matching no item is refused, so a typo in a match never passes as a write.
    ``keep_last`` then keeps only the
    newest N items of each named list (the end of the list; the oldest, at its
    front, are dropped and counted as ``trimmed``), so a rolling ledger is
    capped without reading it. With ``guard_trim`` a trim larger than the
    items the call appended to that list (a cap applied for the first time to
    a list already over it) is refused with :class:`ValueError` naming the
    count it would drop: a rolling ledger trims at most what it appends.
    A key holding anything but a list is refused
    with :class:`ValueError` and *value* is left untouched. A ``dropped``
    dict receives, per list, the items ``remove`` and ``keep_last`` dropped
    (whole, in list order), what an ``archive_key`` stores before the write.
    ``protect`` (#12035) maps a list to the ``archive_when`` predicate of the
    entry's retention (:func:`retention_protect`): ``keep_last`` then drops
    only the oldest items it matches and keeps the others, counted as
    ``kept_open``, never trimming an open item.
    ``overflow_appends`` receives the update appends on a field a retention
    moved to its overflow key (:func:`_patch_item`, #12301); without it they
    are left out, the pointer kept.
    ``fold`` (#12320) names, per list, the identifying field(s) (``"id"``, or
    ``{"by": "id", "sum": ["count"]}``): after the appends, every object item
    sharing those values is merged into the first one (:func:`fold_list_items`),
    counted as ``folded``, so duplicates an older write left are merged on the
    server instead of read, merged by hand and rewritten.
    ``replaced_items`` receives, per list, one record per item a ``unique_by``
    append replaced with a different one: its identity, the fields the stored
    item had that the appended one lacks (``lost``) and the fields whose value
    changed (``changed``), so a caller that read no match and appended a new
    item sees that it overwrote one another run wrote since its read.
    """
    append = _list_ops_shape(append, "append")
    remove = _list_ops_shape(remove, "remove")
    keep_last = _keep_last_shape(keep_last)
    unique_by = _unique_by_shape(unique_by)
    update = _update_shape(update)
    fold = _fold_shape(fold)
    out = dict(value)
    changes: dict[str, dict[str, int]] = {}
    named = [*remove, *(k for k in append if k not in remove)]
    named += [k for k in (*update, *fold, *keep_last) if k not in named]
    for key in named:
        # A key that is present holds a list, or the change is refused: JSON
        # null included, so a field a caller set to null is never overwritten.
        if key in out and not isinstance(out[key], list):
            raise ValueError(
                f"the value under {key!r} is not a list, so it cannot be appended "
                "to or removed from; nothing was written"
            )
    for name, ops in (("remove", remove), ("update", update)):
        for key, entries in ops.items():
            for entry in entries:
                found = entry["match"] if name == "update" else entry
                for m in found if isinstance(found, list) else [found]:
                    if (error := list_match_error(m)) is not None:
                        raise ValueError(f"{name}[{key!r}]: {error} Nothing was written.")
    for key, matches in remove.items():
        current = list(out.get(key) or [])
        kept = [item for item in current if not any(list_item_matches(item, m) for m in matches)]
        changes.setdefault(key, {"appended": 0, "removed": 0})["removed"] = len(current) - len(kept)
        if dropped is not None and len(kept) < len(current):
            dropped.setdefault(key, []).extend(
                item for item in current if any(list_item_matches(item, m) for m in matches)
            )
        if key in out:
            out[key] = kept
    for key, patches in update.items():
        current = list(out.get(key) or [])
        updated = 0
        for patch in patches:
            matches = patch["match"] if isinstance(patch["match"], list) else [patch["match"]]
            for m in matches:
                if not any(list_item_matches(item, m) for item in current):
                    raise ValueError(
                        f"update[{key!r}]: no item of the list matches {json.dumps(m, default=str)[:200]}; "
                        "nothing was written"
                    )
            hits = [i for i, item in enumerate(current) if any(list_item_matches(item, m) for m in matches)]
            for i in hits:
                patched = _patch_item(current[i], patch, key, overflow_appends)
                if patched != current[i]:
                    updated += 1
                current[i] = patched
        out[key] = current
        changes.setdefault(key, {"appended": 0, "removed": 0})["updated"] = updated
    for key, items in append.items():
        current = list(out.get(key) or [])
        counts = changes.setdefault(key, {"appended": 0, "removed": 0})
        fields = unique_by.get(key)
        added = replaced = 0
        for item in items:
            identity = _identity(item, fields)
            same = (
                [i for i, cur in enumerate(current) if _identity(cur, fields) == identity]
                if identity is not None
                else []
            )
            if same:
                if current[same[0]] != item or len(same) > 1:
                    replaced += 1
                    if replaced_items is not None:
                        replaced_items.setdefault(key, []).append(
                            _replacement_record(current[same[0]], item, fields, identity)
                        )
                current[same[0]] = item
                for i in reversed(same[1:]):
                    del current[i]
                counts["removed"] += len(same) - 1
            elif item not in current:
                current.append(item)
                added += 1
        out[key] = current
        counts["appended"] = added
        if fields:
            counts["replaced"] = replaced
    for key, spec in fold.items():
        counts = changes.setdefault(key, {"appended": 0, "removed": 0})
        if key not in out:
            counts["folded"] = 0
            continue
        out[key], counts["folded"] = fold_list_items(list(out[key]), spec["by"], spec["sum"])
    for key, count in keep_last.items():
        counts = changes.setdefault(key, {"appended": 0, "removed": 0})
        current = out.get(key)
        trimmed = max(len(current) - count, 0) if isinstance(current, list) else 0
        cut: set[int] | None = None
        if trimmed and protect and key in protect:
            cut = set([i for i, item in enumerate(current) if is_terminal_item(item, protect[key])][:trimmed])
            counts["kept_open"] = trimmed - len(cut)
            trimmed = len(cut)
        if guard_trim and trimmed > counts["appended"]:
            raise ValueError(
                f"keep_last[{key!r}] would drop {trimmed} of {len(current)} item(s) while this "
                f"call appends {counts['appended']}: a cap applied to a list already over it. "
                "Pass confirm_trim=true to drop them; nothing was written"
            )
        if trimmed:
            if cut is None:
                cut = set(range(trimmed))
            if dropped is not None:
                dropped.setdefault(key, []).extend(current[i] for i in sorted(cut))
            out[key] = _drop_indexes(current, cut)
        counts["trimmed"] = trimmed
    return out, changes


def deep_merge(base: dict, patch: dict) -> dict:
    """Merge *patch* into a copy of *base* key by key, at every depth.

    An object under a key both sides hold as objects merges recursively; any
    other value (a list, a scalar, null) replaces what *base* held. Neither
    input is mutated.
    """
    out = dict(base)
    for key, val in patch.items():
        held = out.get(key)
        if isinstance(val, dict) and isinstance(held, dict):
            out[key] = deep_merge(held, val)
        else:
            out[key] = val
    return out


def parse_value_path(path: str | None) -> list[str]:
    """Split a dot-separated value path (``slice_state.P2``) into its keys.

    An empty or missing path answers ``[]`` (the whole value); an empty segment
    (``a..b``, a leading or trailing dot) is refused with :class:`ValueError`.
    """
    if path is None or not path.strip():
        return []
    parts = path.strip().split(".")
    if any(not p.strip() for p in parts):
        raise ValueError("path must be dot-separated non-empty keys, e.g. 'slice_state.P2'")
    return [p.strip() for p in parts]


def value_at_path(value: dict[str, Any], path: list[str]) -> Any:
    """Answer what *value* holds under *path*, or ``None`` when a key along it is missing.

    A key along the path that holds anything but an object answers ``None`` for
    a deeper path; the caller tells a missing object from a scalar at the end.
    """
    held: Any = value
    for part in path:
        if not isinstance(held, dict):
            return None
        held = held.get(part)
    return held


def write_at_path(value: dict, path: list[str], patch: dict, *, replace: bool = False) -> dict:
    """Answer a copy of *value* with *patch* written under *path* alone.

    The objects along *path* are created when missing; the sub-key at its end
    is deep-merged with *patch* (:func:`deep_merge`), or overwritten when
    *replace* (or when it holds anything but an object). Every sibling key is
    kept. A key along the path that holds anything but an object is refused
    with :class:`ValueError`, and *value* is left untouched.
    """
    if not path:
        if not isinstance(patch, dict):
            raise ValueError("a value written without a path must be an object")
        return dict(patch) if replace else deep_merge(value, patch)
    head, rest = path[0], path[1:]
    out = dict(value)
    held = out.get(head)
    if rest:
        if held is None:
            held = {}
        if not isinstance(held, dict):
            raise ValueError(
                f"the value under {head!r} is not an object, so the path cannot go "
                "through it; nothing was written"
            )
        out[head] = write_at_path(held, rest, patch, replace=replace)
    elif not isinstance(patch, dict):
        # A scalar or a list under a path is written there as it is.
        out[head] = patch
    elif replace or not isinstance(held, dict):
        out[head] = dict(patch)
    else:
        out[head] = deep_merge(held, patch)
    return out


def _stale_after() -> timedelta | None:
    """The active-entry staleness window, or ``None`` when staleness is disabled.

    An ``active`` entry untouched for longer than this is *derived* stale (never
    stored) and surfaced so its owner resolves or purges it. ``<= 0`` disables the
    signal entirely (admins who want shared state to never look stale)."""
    days = getattr(settings, "shared_state_stale_after_days", 14)
    if not days or days <= 0:
        return None
    return timedelta(days=days)


def _aware(at: datetime | None) -> datetime | None:
    if at is None:
        return None
    return at.replace(tzinfo=UTC) if at.tzinfo is None else at


def value_changed_at(state: SharedState) -> datetime | None:
    """When the entry's VALUE last changed: ``value_updated_at``, else its last write.

    A note bumps ``updated_at`` and never this (#11696), so an entry kept warm
    by notes alone ages like one nobody touches. An entry not written since
    the column exists reads its last write, as before.
    """
    return (
        _aware(getattr(state, "value_updated_at", None))
        or _aware(getattr(state, "updated_at", None))
        or _aware(getattr(state, "created_at", None))
    )


def is_shared_state_stale(state: SharedState, now: datetime | None = None) -> bool:
    """True when an ACTIVE entry's value has not changed for the staleness window.

    Also true past its ``expires_at`` (#11696). Resolved entries are never stale
    (they are on their way out via the retention sweep). Mirrors WorkItem's
    derived ``is_stale``: computed at read time, never persisted. Notes do not
    count as a change: the window runs from :func:`value_changed_at`."""
    if state.status != SHARED_STATE_ACTIVE_STATUS:
        return False
    now = now or datetime.now(UTC)
    expires = _aware(getattr(state, "expires_at", None))
    if expires is not None and expires <= now:
        return True
    window = _stale_after()
    if window is None:
        return False
    last = value_changed_at(state)
    if last is None:
        return False
    return now - last > window


def _str_or_none(value) -> str | None:
    return str(value) if value else None


def _lifecycle_uuid(raw: object, name: str) -> uuid.UUID | str | None:
    """A lifecycle id argument: ``None`` keeps, ``""`` clears, else a UUID (ValueError)."""
    if raw is None:
        return None
    if isinstance(raw, uuid.UUID):
        return raw
    text = str(raw).strip()
    if not text:
        return ""
    parsed = safe_uuid(text)
    if parsed is None:
        raise ValueError(f"{name} must be a full UUID; nothing was written")
    return parsed


def _lifecycle_datetime(raw: object) -> datetime | str | None:
    """An ``expires_at`` argument: ``None`` keeps, ``""`` clears, else an aware datetime."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return _aware(raw)
    text = str(raw).strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(
            "expires_at must be an ISO-8601 date or date and time, e.g. 2026-12-01T09:00:00Z; nothing was written"
        ) from None
    return _aware(parsed)


def _iso_or_none(at: datetime | None) -> str | None:
    return at.isoformat() if isinstance(at, datetime) else None


def _age_days(at: datetime | None, now: datetime | None) -> int | None:
    if at is None:
        return None
    return max(0, ((now or datetime.now(UTC)) - at).days)


# ── Holding an entry as a lease ─────────────────────────────────────
# ``hold="take"`` stamps the entry with the run the write runs in
# (``held_by_run_id``), ``hold="release"`` clears it. Another run's take or
# release is refused while that run is alive; once it ended (any terminal
# status, dead-lettered, or no longer recorded) a take takes the entry over and
# the tick's sweep (:func:`release_ended_run_holds`) releases it, each with a
# dated note naming the previous holder. Liveness is the work-item claims' one
# (``app.services.work_item.holding_runs``), so a lease and a claim held by the
# same run end together.
SHARED_STATE_HOLD_ACTIONS: tuple[str, ...] = ("take", "release")


class SharedStateHeldError(ValueError):
    """A take or release of an entry another live run holds: nothing written.

    Carries the holder (*holder_run_id*, *holder_status*) and the
    ``pending_fix`` the entry's value holds, so the tool answers the held
    lease as a result (the caller stands down) rather than as an error.
    """

    def __init__(
        self,
        message: str,
        *,
        holder_run_id: object = None,
        holder_status: str | None = None,
        pending_fix: object = None,
    ) -> None:
        super().__init__(message)
        self.holder_run_id = holder_run_id
        self.holder_status = holder_status
        self.pending_fix = pending_fix


def _holder_label(holder) -> str:
    if holder is None or holder.status is None:
        return "no longer recorded"
    kind = "background task" if holder.kind == "background_task" else "run"
    return f"{kind} {holder.status}"


def _hold_note(action: str, run_id, previous, holder, now: datetime) -> str:
    at = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if action == "released":
        return f"hold released at {at}: holder run {run_id} ended ({_holder_label(holder)})"
    return (
        f"hold taken over at {at} by run {run_id}: previous holder run {previous} "
        f"ended ({_holder_label(holder)})"
    )


def serialize_state(state: SharedState, *, now: datetime | None = None) -> dict:
    """Serialize a shared-state entry to a JSON-safe dict (with derived ``is_stale``).

    ``size_chars`` is the value's JSON length, ``largest_list`` the item count
    of its longest top-level list, ``over_soft_limit`` whether the size passes
    ``shared_state_value_soft_limit_chars``: the size flag next to the stale one.
    """
    value = state.value or {}
    size = value_size(value)
    soft = settings.shared_state_value_soft_limit_chars
    lists = [len(v) for v in value.values() if isinstance(v, list)] if isinstance(value, dict) else []
    return {
        "id": str(state.id),
        "user_id": str(state.user_id) if state.user_id else None,
        "is_company_managed": state.is_company_managed,
        "workspace_id": str(state.workspace_id) if state.workspace_id else None,
        "key": state.key,
        "label": state.label,
        "owner_agent_id": str(state.owner_agent_id) if state.owner_agent_id else None,
        "held_by_run_id": _str_or_none(getattr(state, "held_by_run_id", None)),
        "is_lease": bool(getattr(state, "is_lease", None)),
        "linked_work_item_id": _str_or_none(getattr(state, "linked_work_item_id", None)),
        "expires_at": _iso_or_none(getattr(state, "expires_at", None)),
        "terminal_condition": getattr(state, "terminal_condition", None),
        "retention": getattr(state, "retention", None) or None,
        "retention_usage": retention_usage(value, getattr(state, "retention", None)),
        "value_updated_at": _iso_or_none(value_changed_at(state)),
        "value_age_days": _age_days(value_changed_at(state), now),
        "status": state.status,
        "is_stale": is_shared_state_stale(state, now),
        "value": value,
        "size_chars": size,
        "largest_list": max(lists, default=0),
        "over_soft_limit": bool(soft and soft > 0 and size > soft),
        "notes": state.notes or [],
        "resolved_at": state.resolved_at.isoformat() if state.resolved_at else None,
        "created_at": state.created_at.isoformat() if state.created_at else None,
        "updated_at": state.updated_at.isoformat() if state.updated_at else None,
    }


def _visible_clause(
    user_uuid: uuid.UUID | None,
    workspace_ids: Sequence[uuid.UUID] | None = None,
):
    """Visibility: own + company-managed + workspace-shared.

    The workspace tier is what makes a workspace a SHARED container: a
    row an agent wrote in a workspace is visible to everyone who can see that
    workspace, not only to the person whose turn happened to write it. Without
    it the partial unique index would key rows by workspace while the read path
    still filtered by user, so two people in one workspace would collide on
    writes and see nothing.

    Own rows and company-managed rows count among the UNFILED ones only. A row
    filed into a workspace is governed by the workspace tier: an author who
    has lost access to the workspace loses the row with it, and a
    company-managed agent's row filed there is not readable by the whole fleet.

    *workspace_ids* is the readable tier for a read, the writable tier for the
    lookup of a write. An empty list adds nothing (an empty ``IN`` renders
    false), so a caller with no workspaces sees no workspace-scoped rows.
    """
    unfiled = SharedState.workspace_id.is_(None)
    # Activity hidden from non-admins (#11624): a company-wide or workspace
    # row of a hidden agent, or filed in a hidden fleet's workspace, leaves
    # the reader's tiers; own rows never do. A read with no caller is unchanged.
    shown = true()
    if user_uuid is not None:
        from app.services.activity_visibility import (
            agents_activity_shown,
            workspace_activity_shown,
        )

        shown = and_(
            agents_activity_shown(user_uuid, SharedState.owner_agent_id),
            workspace_activity_shown(SharedState.workspace_id, user_uuid),
        )
    tiers = [and_(SharedState.is_company_managed.is_(True), unfiled, shown)]
    if user_uuid is not None:
        tiers.append(and_(SharedState.user_id == user_uuid, unfiled))
    if workspace_ids:
        tiers.append(and_(SharedState.workspace_id.in_(list(workspace_ids)), shown))
    return or_(*tiers)


#: The read visibility under its public name, for a counter outside this
#: module (a workspace's "Holds:" line) that must agree with the lists.
shared_state_visible_clause = _visible_clause


def _deletable_clause(
    user_uuid: uuid.UUID | None,
    is_admin: bool,
    writable_workspace_ids: Sequence[uuid.UUID] | None = None,
):
    """Rows the caller may delete (single source of truth for bulk delete).

    Mirrors the single-delete gate: a user can always delete their OWN private
    unfiled rows; company-managed rows are admin-only on the REST API, so a
    non-admin's deletable scope excludes them entirely - including one that
    carries the caller's ``user_id`` (whoever was chatting with the
    company-managed agent that created it).

    A workspace-scoped row needs the `config` tier on its workspace, NOT mere
    visibility: a `read_only` grantee who could delete
    would be removing state other agents are acting on.
    """
    if is_admin:
        return _visible_clause(user_uuid, writable_workspace_ids)
    tiers = []
    if user_uuid is not None:
        tiers.append(
            and_(SharedState.user_id == user_uuid, SharedState.workspace_id.is_(None))
        )
    if writable_workspace_ids:
        tiers.append(SharedState.workspace_id.in_(list(writable_workspace_ids)))
    if not tiers:
        return false()
    return and_(SharedState.is_company_managed.is_not(True), or_(*tiers))


async def _workspace_tiers(
    db, user_id: str | None
) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    """``(readable, writable)`` workspace ids for this caller, once per operation.

    Read follows the workspace's visibility; write requires the `config` tier:
    a `read_only` grantee who could write would be
    injecting state other agents then act on.
    """
    from app.services.workspaces import workspace_scope_tiers

    return await workspace_scope_tiers(db, user_id)


def _search_clause(search: str | None):
    """ILIKE match on the key or label, or ``None`` when no search given."""
    if not (search and search.strip()):
        return None
    term = f"%{search.strip()}%"
    return or_(SharedState.key.ilike(term), SharedState.label.ilike(term))


def _ownership_clause(ownership: str | None, user_uuid: uuid.UUID | None):
    """The Operations Mine | Shared | Company split of the visible entries.

    Mine = written for the caller (``user_id``), or owned by an agent the
    caller owns; Company = company-managed and not mine; Shared = the rest.
    ``None`` when *ownership* is omitted or ``all``.
    """
    return ownership_clause(
        ownership,
        user_uuid=user_uuid,
        user_column=SharedState.user_id,
        agent_columns=(SharedState.owner_agent_id,),
        company_column=SharedState.is_company_managed,
    )


def _status_clause(status: str | None):
    """Exact status filter, or ``None`` for an unset / unrecognised status."""
    if status and status in SHARED_STATE_STATUSES:
        return SharedState.status == status
    return None


def _stale_clause(now: datetime):
    """SQL mirror of :func:`is_shared_state_stale` - active + no update for the
    staleness window. ``None`` when staleness is disabled (no rows are stale)."""
    window = _stale_after()
    if window is None:
        return None
    changed = func.coalesce(SharedState.value_updated_at, SharedState.updated_at, SharedState.created_at)
    return and_(
        SharedState.status == SHARED_STATE_ACTIVE_STATUS,
        or_(
            changed < now - window,
            and_(SharedState.expires_at.is_not(None), SharedState.expires_at <= now),
        ),
    )


def _visible_state_query(
    uid: uuid.UUID | None,
    key: str,
    workspace_ids: Sequence[uuid.UUID] | None = None,
    reader: CompanyManagedWriter | None = None,
):
    """Deterministic single-row lookup by key in the visible scope.

    A workspace-scoped read arrives with the namespaced key
    (``workspace:<id>:<key>``), so the namespace narrows it to one workspace
    before the tier list is consulted: the tier AUTHORISES the row, the
    namespace SELECTS it.

    An agent *reader* never resolves a key to an entry whose owner agent keeps
    it out: a fleet that narrowed its entries owns its keys, so another fleet
    using the same key reaches its own entry (or none).

    The visible scope spans the caller's OWN unfiled row, every unfiled
    company-managed row and every row of a workspace in *workspace_ids*, so a
    single key can resolve to several visible rows: a company-managed row
    usually keeps its creator's ``user_id`` but may have a NULL ``user_id`` when
    an anonymous caller created it, and different creators can each hold a
    company-managed row with the same key. Prefer the caller's own entry, then
    the most-recently-updated, and ``LIMIT 1`` so a key lookup can never raise
    ``MultipleResultsFound``.
    """
    stmt = select(SharedState).where(
        SharedState.key == key,
        _visible_clause(uid, workspace_ids),
    )
    if _narrows(reader):
        stmt = stmt.where(not_(_narrowed_clause(reader, READ)))
    if uid is None:
        # Anonymous scope sees only company-managed rows and has no "own" row, so
        # the own-first tiebreak is meaningless here: ``SharedState.user_id == uid``
        # would render as ``user_id IS NULL`` and wrongly float an
        # anonymously-created row above a newer, identified-creator company-managed
        # row for the same key. Order by recency alone.
        return stmt.order_by(SharedState.updated_at.desc()).limit(1)
    # ``case`` yields 0 for the caller's own rows and 1 otherwise (never NULL), so
    # the sort is total and own-first regardless of the DB's NULL-ordering default.
    own_first = sa_case((SharedState.user_id == uid, 0), else_=1)
    if reader is not None and reader.may_write and reader.agent_id is not None:
        # A company-managed agent keeps ONE entry per key whatever its run
        # context: a personal copy left by a hidden run must not shadow the
        # fleet's company-managed entry in the context that wrote it.
        managed_first = sa_case((SharedState.is_company_managed.is_(True), 0), else_=1)
        return stmt.order_by(managed_first, own_first, SharedState.updated_at.desc()).limit(1)
    return stmt.order_by(own_first, SharedState.updated_at.desc()).limit(1)


async def get_state(
    *, user_id: str | None, key: str, reader: CompanyManagedWriter | None = None
) -> dict | None:
    """Fetch a shared-state entry by its exact key (visible scope).

    *reader* is the agent reading (its tools): an entry whose owner agent keeps
    it out is not found, and the entry says whether that agent may change it
    (``read_only``). A person's read passes none.
    """
    from app.core.database import async_session

    key = normalize_correlation_key(key)
    if not key:
        return None
    uid = safe_uuid(user_id) if user_id else None
    async with async_session() as db:
        readable, _writable = await _workspace_tiers(db, user_id)
        state = (
            await db.execute(_visible_state_query(uid, key, readable, reader))
        ).scalars().first()
        if state is None:
            return None
        if reader is None:
            return serialize_state(state)
        access = await _governor_access(db, reader, state)
    return {**serialize_state(state), "read_only": _read_only(reader, state, access)}


async def get_state_by_id(*, user_id: str | None, state_id: str) -> dict | None:
    """Fetch one visible shared-state entry by its UUID (for the REST surface)."""
    from app.core.database import async_session

    sid = safe_uuid(state_id)
    if sid is None:
        return None
    uid = safe_uuid(user_id) if user_id else None
    async with async_session() as db:
        readable, _writable = await _workspace_tiers(db, user_id)
        state = (
            await db.execute(
                select(SharedState).where(
                    SharedState.id == sid, _visible_clause(uid, readable)
                )
            )
        ).scalar_one_or_none()
    return serialize_state(state) if state else None


LIST_SORTS = ("key", "owner", "updated", "size")


def _list_order(sort: str | None, direction: str | None):
    """The ORDER BY of a ``sort`` the Operations table asked for, or None (the default order).

    ``owner`` orders by the owner agent's name (entries with no owner last),
    ``size`` by the length of the value's JSON text (``serialize_state``'s
    ``size_chars`` up to whitespace). An unknown sort is the default order.
    """
    if sort not in LIST_SORTS:
        return None
    from sqlalchemy import Text, cast

    from app.models.agent import Agent

    descending = direction != "asc"
    if sort == "key":
        column = SharedState.key
    elif sort == "updated":
        column = SharedState.updated_at
    elif sort == "size":
        column = func.length(cast(SharedState.value, Text))
    else:
        column = (
            select(func.lower(Agent.name)).where(Agent.id == SharedState.owner_agent_id).scalar_subquery()
        )
    primary = column.desc().nulls_last() if descending else column.asc().nulls_last()
    tie = SharedState.id.desc() if descending else SharedState.id.asc()
    return (primary, tie)


async def list_states(
    *,
    user_id: str | None,
    search: str | None = None,
    status: str | None = None,
    stale_only: bool = False,
    limit: int = 100,
    offset: int = 0,
    reader: CompanyManagedWriter | None = None,
    key_prefix: str | None = None,
    ownership: str | None = None,
    scope=None,
    sort: str | None = None,
    direction: str | None = None,
) -> list[dict]:
    """List visible shared-state entries.

    ``key_prefix`` keeps one namespace's entries, applied before the page is
    cut so a namespaced tool never loses its own entries behind newer ones.
    ``search`` matches the key or label (ILIKE). ``status`` filters to ``active``
    or ``resolved`` (unset = all). ``stale_only`` returns only active entries that
    have gone quiet past the staleness window, ordered most-neglected first so the
    owner sees the worst offenders to resolve / purge; otherwise the list is
    newest-updated first. Paired with :func:`count_states` for pagination.

    *reader* is the agent listing (its tools): the company-managed entries whose
    owner agent keeps it out are left out in SQL, and every entry says whether
    that agent may change it (``read_only``). A person's listing passes none.

    ``ownership`` (``mine`` / ``shared``, omitted = both) is the Operations
    split of the visible entries (:func:`_ownership_clause`); the tools never
    pass it. ``scope`` is one more clause from the route (the workspace of the
    Operations tab, ``fleet_views.workspace_scope_clause``).

    ``sort`` (one of :data:`LIST_SORTS`: ``key``, ``owner``, ``updated``,
    ``size``) with ``direction`` (``asc`` / ``desc``) orders the page for the
    Operations table; omitted keeps the default order above. ``id`` breaks
    every tie so offset pages never overlap.
    """
    from app.core.database import async_session

    uid = safe_uuid(user_id) if user_id else None
    now = datetime.now(UTC)
    order = _list_order(sort, direction)
    # Resolved BEFORE the session opens: staleness disabled means nothing is
    # stale, and the caller gets an empty page without a database round-trip.
    stale = _stale_clause(now) if stale_only else None
    if stale_only and stale is None:
        return []
    async with async_session() as db:
        readable, _writable = await _workspace_tiers(db, user_id)
        query = select(SharedState).where(_visible_clause(uid, readable))
        if _narrows(reader):
            query = query.where(not_(_narrowed_clause(reader, READ)))
        for clause in (
            _search_clause(search),
            _status_clause(status),
            _ownership_clause(ownership, uid),
            scope,
        ):
            if clause is not None:
                query = query.where(clause)
        if key_prefix:
            query = query.where(SharedState.key.startswith(key_prefix, autoescape=True))
        if stale is not None:
            query = query.where(stale)
        if order is not None:
            query = query.order_by(*order)
        elif stale is not None:
            query = query.order_by(SharedState.updated_at.asc(), SharedState.id.asc())
        else:
            # ``id`` breaks ties so offset paging never skips nor repeats.
            query = query.order_by(SharedState.updated_at.desc(), SharedState.id.desc())
        query = query.offset(max(offset, 0)).limit(min(max(limit, 1), 500))
        states = (await db.execute(query)).scalars().all()
        if reader is None:
            return [serialize_state(s, now=now) for s in states]
        policies = (
            await load_coordination_access(
                db,
                {s.owner_agent_id for s in states if s.is_company_managed and s.owner_agent_id},
            )
            if _narrows(reader)
            else {}
        )
    return [
        {
            **serialize_state(s, now=now),
            "read_only": _read_only(reader, s, access_of(policies, s.owner_agent_id)),
        }
        for s in states
    ]


async def count_states(
    *,
    user_id: str | None,
    search: str | None = None,
    status: str | None = None,
    stale_only: bool = False,
    is_admin: bool = False,
    deletable_only: bool = False,
    ownership: str | None = None,
    scope=None,
    reader: CompanyManagedWriter | None = None,
    key_prefix: str | None = None,
) -> int:
    """Count shared-state entries matching the filters (single COUNT(*)).

    *reader* and ``key_prefix`` narrow exactly as on :func:`list_states`, so
    the tool's "showing X-Y of Z" total counts what its pages can return.

    Same ``search`` / ``status`` / ``stale_only`` semantics as :func:`list_states`.
    By default counts the *visible* scope - the "Showing X-Y of Z" pagination
    total. Pass ``deletable_only=True`` to count only rows the caller may bulk
    delete (:func:`_deletable_clause`), so the "select all N matching" UI
    promises exactly what :func:`bulk_delete_states` will remove (for a
    non-admin that excludes the company-managed entries they can see but not
    delete). Sharing ``_deletable_clause`` keeps count and bulk-delete in
    lockstep.
    """
    from app.core.database import async_session

    uid = safe_uuid(user_id) if user_id else None
    now = datetime.now(UTC)
    stale = _stale_clause(now) if stale_only else None
    if stale_only and stale is None:
        return 0
    async with async_session() as db:
        readable, writable = await _workspace_tiers(db, user_id)
        visible_scope = (
            _deletable_clause(uid, is_admin, writable)
            if deletable_only
            else _visible_clause(uid, readable)
        )
        query = select(func.count()).select_from(SharedState).where(visible_scope)
        if _narrows(reader):
            query = query.where(not_(_narrowed_clause(reader, READ)))
        if key_prefix:
            query = query.where(SharedState.key.startswith(key_prefix, autoescape=True))
        for clause in (
            _search_clause(search),
            _status_clause(status),
            _ownership_clause(ownership, uid),
            scope,
        ):
            if clause is not None:
                query = query.where(clause)
        if stale is not None:
            query = query.where(stale)
        count = (await db.execute(query)).scalar_one()
    return int(count or 0)


async def count_states_by_owner(
    *,
    user_id: str | None,
    search: str | None = None,
    status: str | None = None,
    stale_only: bool = False,
    ownership: str | None = None,
    scope=None,
) -> list[tuple[str | None, int]]:
    """``(owner_agent_id, count)`` of the visible entries matching the filters.

    The list's filters minus the owner: what the Operations owner-agent filter
    offers, each owner with the number of entries it would show, ``None`` for
    the entries no agent owns. Largest first. Same visibility, ``search``,
    ``status``, ``stale_only``, ``ownership`` and ``scope`` as
    :func:`count_states`, so each number is the total the list shows once that
    owner is picked.
    """
    from app.core.database import async_session

    uid = safe_uuid(user_id) if user_id else None
    now = datetime.now(UTC)
    stale = _stale_clause(now) if stale_only else None
    if stale_only and stale is None:
        return []
    async with async_session() as db:
        readable, _writable = await _workspace_tiers(db, user_id)
        total = func.count().label("total")
        query = (
            select(SharedState.owner_agent_id, total)
            .where(_visible_clause(uid, readable))
        )
        for clause in (
            _search_clause(search),
            _status_clause(status),
            _ownership_clause(ownership, uid),
            scope,
            stale,
        ):
            if clause is not None:
                query = query.where(clause)
        query = query.group_by(SharedState.owner_agent_id).order_by(total.desc())
        rows = (await db.execute(query)).all()
    return [(str(owner) if owner else None, int(count)) for owner, count in rows]


async def bulk_delete_states(
    *,
    user_id: str | None,
    is_admin: bool = False,
    ids: list[str] | None = None,
    all_matching_filters: bool = False,
    search: str | None = None,
    status: str | None = None,
    stale_only: bool = False,
    ownership: str | None = None,
) -> int:
    """Delete multiple shared-state entries at once. Returns the number deleted.

    Two modes mirror the work-item / assignment-run bulk delete: explicit
    ``ids``, or ``all_matching_filters`` (same ``search`` / ``status`` /
    ``stale_only`` filters as the list, so "select all N matching" removes exactly
    what the filtered list shows). The deletable scope is the single-delete gate:
    own private rows always, rows of a workspace the caller may write, plus
    company-managed rows only for admins (:func:`_deletable_clause`).
    """
    from app.core.database import async_session

    uid = safe_uuid(user_id) if user_id else None
    now = datetime.now(UTC)

    async with async_session() as db:
        _readable, writable = await _workspace_tiers(db, user_id)
        deletable = _deletable_clause(uid, is_admin, writable)
        if ids:
            valid = [sid for sid in (safe_uuid(i) for i in ids) if sid]
            if not valid:
                return 0
            query = select(SharedState.id).where(SharedState.id.in_(valid), deletable)
        elif all_matching_filters:
            query = select(SharedState.id).where(deletable)
            for clause in (
                _search_clause(search),
                _status_clause(status),
                _ownership_clause(ownership, uid),
            ):
                if clause is not None:
                    query = query.where(clause)
            if stale_only:
                stale = _stale_clause(now)
                if stale is None:
                    return 0
                query = query.where(stale)
        else:
            return 0

        target_ids = (await db.execute(query)).scalars().all()
        if not target_ids:
            return 0
        # Re-apply ``deletable`` to every DELETE (not just the SELECT that chose
        # the ids) so the permission gate is enforced by the delete itself: a row
        # that turned company-managed or changed owner between the SELECT and the
        # DELETE can't be removed by a caller who may no longer delete it (closes
        # the TOCTOU window). Sum the real per-batch rowcounts so the reported
        # total reflects what was actually removed, not merely what was targeted.
        deleted = 0
        for start in range(0, len(target_ids), 1000):
            batch = target_ids[start : start + 1000]
            result = await db.execute(
                delete(SharedState).where(SharedState.id.in_(batch), deletable)
            )
            deleted += result.rowcount or 0
        await db.commit()

    return deleted


async def upsert_state(
    *,
    user_id: str | None,
    key: str,
    label: str | None = None,
    owner_agent_id: str | None = None,
    value: dict | None = None,
    note: str | None = None,
    status: str | None = None,
    acting_agent_id: str | None = None,
    is_company_managed: bool = False,
    replace_value: bool = False,
    writer: CompanyManagedWriter,
    workspace_id: uuid.UUID | None = None,
    append: dict | None = None,
    remove: dict | None = None,
    keep_last: dict | None = None,
    unique_by: dict | None = None,
    update: dict | None = None,
    fold: dict | None = None,
    guard_trim: bool = False,
    deep: bool = False,
    path: str | None = None,
    hold: str | None = None,
    run_id: str | uuid.UUID | None = None,
    unset_keys: Sequence[str] | None = None,
    expect_dropped: dict[str, list] | None = None,
    archive: bool = False,
    lease: bool | None = None,
    linked_work_item_id: str | uuid.UUID | None = None,
    expires_at: datetime | str | None = None,
    terminal_condition: str | None = None,
    retention: dict | None = None,
    archive_fit: bool = False,
    path_appends: Sequence[tuple[Sequence[str], list[Any]]] | None = None,
) -> dict:
    """Create or update the shared-state entry for ``key`` (idempotent).

    Overflowed fields (#12301): an ``update`` append on a field the entry's
    retention moved to its overflow key (an ``overflow://`` pointer) is
    written to the list stored there, before the source commits, and the
    answer carries ``overflow_appended`` (per pointer, the items added).
    ``path_appends`` (pairs of a path and items) is that write: each item
    absent from the list under the path is appended (:func:`append_at_path`),
    the objects and the list along it created when missing.

    Retention (#11839): ``retention`` stores a policy on the entry
    (:func:`retention_shape`; ``{}`` clears it, ``None`` keeps it). On every
    write that changes the value, the policy rolls the oldest items of its
    lists out (:func:`roll_lists`) and appends them to its ``archive_key``
    first, so an append on a list at its cap lands instead of being refused;
    the answer carries ``rolled`` (per list, the count), ``rolled_to`` and
    ``archive_dropped``. ``archive_fit`` marks that archive write: an archive
    at its cap drops its oldest entries, the rolled ones included, instead of
    refusing (:func:`fit_archive`).

    Lifecycle (#11696): ``lease`` marks the entry a lease (default: an entry
    CREATED by ``hold="take"`` is one); a lease entry is resolved when its
    hold is released, here or by :func:`release_ended_run_holds`.
    ``linked_work_item_id`` names the work item the entry tracks (resolved by
    :func:`resolve_terminal_linked_states` once it is done or cancelled),
    ``expires_at`` the date the tick resolves it, ``terminal_condition`` what
    ends it in words; an empty string clears each, ``None`` keeps it. A write
    that changes the value stamps ``value_updated_at``; a note alone does not.
    A write leaving a top-level list longer than ``shared_state_list_max_items``
    and longer than before is refused (:func:`list_cap_refusal`).

    Size limits: a write that leaves the value over
    ``shared_state_value_hard_limit_chars`` (JSON characters) and larger than
    before is refused (:func:`size_limit_refusal`); a value over the soft limit
    is written and the answer carries ``size_warning``. ``archive`` marks an
    archive write (``archive_key``): every appended object item is stamped
    :data:`ARCHIVE_STAMP` for the archive retention sweep, and the oldest items
    of the appended lists go first when the archive would pass the hard limit
    (``archive_trimmed`` in the answer).

    ``hold`` is ``"take"`` or ``"release"``: the entry is held as a
    lease by *run_id*, the run the write runs in (``held_by_run_id``), or
    released. A take or release of an entry another run holds is refused with
    :class:`SharedStateHeldError` while that run is alive (nothing written);
    once it ended, a take takes the entry over with a dated note naming the
    previous holder. A take needs *run_id*. A write without ``hold`` never
    changes the holder.

    ``value`` is shallow-merged into the existing bag unless ``replace_value``;
    with ``deep`` nested objects merge key by key at every depth
    (:func:`deep_merge`), so two writers of sibling sub-keys both survive.
    ``path`` (dot-separated) writes ``value`` under that sub-key alone
    (:func:`write_at_path`): deep-merged there, or overwritten there when
    ``replace_value``; the objects along it are created when missing. Both
    apply to the value as stored, under the row lock below.
    ``append`` / ``remove`` then change lists held under top-level keys
    (:func:`apply_list_ops`) against the value as stored; the answer carries
    ``list_changes`` (per key, items appended and removed); ``keep_last``
    then keeps only the newest N items of each named list (``trimmed``
    in ``list_changes``); ``guard_trim`` refuses a trim larger than what the
    call appends to that list (:func:`apply_list_ops`). ``unset_keys`` removes
    those top-level keys from the value (a missing one is ignored; the answer
    names the removed ones in ``unset_keys``). ``expect_dropped`` is what the
    caller archived from the value it read: the write is refused when the
    removals and trims, under the row lock, would drop anything else, so an
    item never leaves the list without its copy. ``unique_by`` makes an append replace the object
    item carrying the same identifying field(s) instead of adding a second
    one (``replaced`` in ``list_changes``). ``update`` patches fields of the
    object items a match selects (set, increment, append; ``updated`` in
    ``list_changes``), so a counter moves without resending its item. The row an update
    reads is locked (``SELECT ... FOR UPDATE``) until the commit, so concurrent
    merges and list changes serialise instead of overwriting each other. The
    lock is taken only once the write is authorised (``_refuse_modify``), so a
    caller that may only read the row never holds it.
    ``note`` appends one entry to the append-only notes log. ``status`` moves the
    entry through its lifecycle (``active`` / ``resolved``): resolving stamps
    ``resolved_at``; (re)activating clears it. On first create the owner defaults
    to the acting agent (``acting_agent_id``) when none is given, so every entry
    has a responsible janitor. Concurrent first-touches are reconciled via the
    unique index (insert -> retry as update on conflict).

    ``workspace_id`` files the entry into a workspace. A key in a
    workspace's namespace (``workspace:<id>:...``) names its workspace itself,
    and the two must agree. Writing there needs the workspace's `config` tier,
    checked against the argument AND against the row actually written.

    Raises :class:`CompanyManagedWriteRefusedError` when *writer* may not create a
    company-managed entry or the key resolves to a company-managed entry it may
    not modify - never a private copy under the same key, which would split
    what two readers see - and when an agent other than the owner agent of an
    entry its owner narrowed would hand it to another owner.
    :class:`CompanyManagedKeyHiddenError` when the key is held, in the same
    user scope, by an entry whose owner agent keeps this agent out.
    :class:`WorkspaceWriteRefusedError` when the caller may only read the
    workspace the entry is filed into.
    """
    from app.core.database import async_session

    refuse_company_managed_write(
        writer, is_company_managed=is_company_managed, action="create", subject=_SUBJECT
    )
    key = normalize_correlation_key(key)
    if not key:
        raise ValueError("key is required")
    key_workspace = _key_workspace(key)
    if workspace_id is None:
        workspace_id = key_workspace
    elif key_workspace != workspace_id:
        raise ValueError(
            "a workspace entry's key must be in that workspace's namespace"
        )
    uid = safe_uuid(user_id) if user_id else None
    now = datetime.now(UTC)
    normalized_status = status if status in SHARED_STATE_STATUSES else None
    append = _list_ops_shape(append, "append")
    if archive and append:
        stamp = now.isoformat()
        append = {
            name: [{**item, ARCHIVE_STAMP: stamp} if isinstance(item, dict) else item for item in items]
            for name, items in append.items()
        }
    archive_trimmed: list[int] = []
    policy_set = None if retention is None else retention_shape(retention)
    if policy_set and policy_set.get("archive_key") == key:
        raise ValueError("retention archive_key must name another key than the entry's own")
    if policy_set and policy_set.get("overflow_key") == key:
        raise ValueError("retention overflow_key must name another key than the entry's own")
    # The fields the policy's max_item_chars moved out of the items, per list and item (#12177).
    spilled_out: dict[str, dict[str, dict[str, Any]]] = {}
    spilled_answer: dict[str, Any] = {}
    # What the policy rolls out of the lists in this write, per list.
    rolled_out: dict[str, list] = {}
    rolled_answer: dict = {}
    # Open items the policy's archive_when kept past a bound, per list (#12035).
    held_open: dict[str, int] = {}
    remove = _list_ops_shape(remove, "remove")
    keep_last = _keep_last_shape(keep_last)
    unique_by = _unique_by_shape(unique_by)
    update = _update_shape(update)
    fold = _fold_shape(fold)
    value_path = parse_value_path(path)
    path_list_ops = bool(value_path) and bool(append or remove or keep_last or update or fold)
    if value_path and value is None and not path_list_ops:
        raise ValueError("path needs a value to write under it, or list changes to apply under it")
    hold_action = (hold or "").strip().lower() or None
    if hold_action is not None and hold_action not in SHARED_STATE_HOLD_ACTIONS:
        raise ValueError("hold must be 'take' or 'release'")
    hold_run = (
        run_id if isinstance(run_id, uuid.UUID) else safe_uuid(str(run_id)) if run_id else None
    )
    if hold_action == "take" and hold_run is None:
        raise ValueError(
            "hold='take' needs a run to hold the entry: it works inside an "
            "assignment run or a background task only"
        )
    linked_item = _lifecycle_uuid(linked_work_item_id, "linked_work_item_id")
    expires = _lifecycle_datetime(expires_at)
    condition = None if terminal_condition is None else (terminal_condition.strip()[:500] or "")
    list_changes: dict[str, dict[str, int]] = {}
    replaced_items: dict[str, list[dict[str, Any]]] = {}
    unset = [k for k in dict.fromkeys(unset_keys or []) if isinstance(k, str) and k]
    unset_done: list[str] = []
    # Dated notes the hold check writes with the change (a takeover).
    hold_notes: list[str] = []
    # Update appends on a field moved to the overflow key, and their answer (#12301).
    overflow_appends: list[dict[str, Any]] = []
    overflow_answer: dict[str, Any] = {}
    path_appended: list[int] = []

    async def _check_hold(db, state: SharedState) -> None:
        """Refuse a take or release of an entry a live other run holds."""
        hold_notes.clear()
        current = getattr(state, "held_by_run_id", None)
        _refuse_take_of_a_record(state)
        if hold_action is None or current is None or current == hold_run:
            return
        from app.services.work_item import holding_runs

        holder = (await holding_runs(db, [current])).get(current)
        if holder is not None and holder.alive:
            value_now = getattr(state, "value", None)
            raise SharedStateHeldError(
                f"the shared state {key!r} is held by run {current}, which is still "
                f"{holder.status}; nothing was written",
                holder_run_id=current,
                holder_status=holder.status,
                pending_fix=value_now.get("pending_fix") if isinstance(value_now, dict) else None,
            )
        if hold_action == "take":
            hold_notes.append(_hold_note("taken", hold_run, current, holder, now))
        else:
            hold_notes.append(_hold_note("released", current, None, holder, now))

    def _refuse_take_of_a_record(state: SharedState) -> None:
        """Refuse a take of an active entry kept for another purpose (#2208).

        An entry that is not a lease, has no holder and is still active was
        written for something else (notes, a merge record): a take would stamp
        a holder on it and merge the lease value into the other purpose's, and
        its release would leave it active with both mixed. Passing ``lease``
        explicitly says the caller means that entry.
        """
        if hold_action != "take" or lease is not None:
            return
        if getattr(state, "id", None) is None or getattr(state, "held_by_run_id", None) is not None:
            return
        if getattr(state, "is_lease", None) or getattr(state, "status", None) == SHARED_STATE_RESOLVED_STATUS:
            return
        raise ValueError(
            f"{key!r} is a shared state entry, not a lease: it is active, has no holder and was "
            "not created by a take, so a take would mix the lease into what it records. Take a "
            "key of its own, or pass lease=true (or lease=false) to hold this entry on purpose; "
            "nothing was written"
        )

    def _apply(state: SharedState) -> None:
        # The value first: a refused list change raises before anything else
        # of the row is touched, and the caller never commits.
        new_value: dict | None = None
        if value is not None:
            if value_path:
                new_value = write_at_path(
                    dict(state.value or {}), value_path, value, replace=replace_value
                )
            elif replace_value:
                new_value = dict(value)
            elif deep:
                new_value = deep_merge(dict(state.value or {}), value)
            else:
                new_value = dict(state.value or {})
                new_value.update(value)
        policy = policy_set if policy_set is not None else (getattr(state, "retention", None) or {})
        overflow_appends.clear()
        replaced_items.clear()
        if path_list_ops:
            # The list changes apply to the lists of the object under the path,
            # under the row lock: two runs changing one nested list never drop
            # each other's change (a read then a path write of the whole object did).
            base_value = new_value if new_value is not None else dict(state.value or {})
            held = value_at_path(base_value, value_path)
            if held is not None and not isinstance(held, dict):
                raise ValueError(
                    f"the value under {'.'.join(value_path)!r} is not an object, so its lists "
                    "cannot be changed; nothing was written"
                )
            nested_dropped: dict[str, list[Any]] = {}
            nested, changes = apply_list_ops(
                dict(held or {}),
                append=append,
                remove=remove,
                keep_last=keep_last,
                unique_by=unique_by,
                update=update,
                fold=fold,
                guard_trim=guard_trim,
                dropped=nested_dropped,
                replaced_items=replaced_items,
            )
            if expect_dropped is not None and nested_dropped != expect_dropped:
                raise ValueError(
                    "the list changed between the archive and the write: the items it "
                    "would drop now are not the ones archived; the archive holds a copy, "
                    "the list was not written: read it and write again"
                )
            new_value = write_at_path(base_value, value_path, nested, replace=True)
            list_changes.clear()
            list_changes.update({f"{'.'.join(value_path)}.{name}": counts for name, counts in changes.items()})
            nested_replaced = {f"{'.'.join(value_path)}.{name}": rows for name, rows in replaced_items.items()}
            replaced_items.clear()
            replaced_items.update(nested_replaced)
        elif append or remove or keep_last or update or fold:
            dropped: dict[str, list] = {}
            base_value = new_value if new_value is not None else dict(state.value or {})
            new_value, changes = apply_list_ops(
                base_value,
                append=append,
                remove=remove,
                keep_last=keep_last,
                unique_by=unique_by,
                update=update,
                fold=fold,
                guard_trim=guard_trim,
                dropped=dropped,
                protect=retention_protect(policy, base_value),
                overflow_appends=overflow_appends,
                replaced_items=replaced_items,
            )
            if expect_dropped is not None and dropped != expect_dropped:
                raise ValueError(
                    "the list changed between the archive and the write: the items it "
                    "would drop now are not the ones archived; the archive holds a copy, "
                    "the list was not written: read it and write again"
                )
            list_changes.clear()
            list_changes.update(changes)
        path_appended.clear()
        if path_appends:
            base = new_value if new_value is not None else dict(state.value or {})
            added_total = 0
            for at, items in path_appends:
                base, added = append_at_path(base, list(at), list(items))
                added_total += added
            new_value = base
            path_appended.append(added_total)
        unset_done.clear()
        if unset:
            base = new_value if new_value is not None else dict(state.value or {})
            unset_done.extend(k for k in unset if k in base)
            if unset_done:
                new_value = {k: v for k, v in base.items() if k not in unset_done}
        archive_trimmed.clear()
        rolled_out.clear()
        held_open.clear()
        spilled_out.clear()
        if policy and not archive and (new_value is not None or policy_set):
            if new_value is None:
                new_value = dict(state.value or {})
            # Bound each item first: a setting of the policy compacts every item
            # already stored, a later write the items it brings (#12177).
            spilled_out.update(spill_items(new_value, policy))
            appended = {
                name: (list_changes.get(name) or {}).get("appended", 0) for name in (append or {}) if not path_list_ops
            }
            rolled_out.update(roll_lists(new_value, policy, appended, held_open))
            if guard_trim and not policy.get("archive_key"):
                _refuse_unarchived_roll(rolled_out, appended)
            compacted = compact_open_items(new_value, policy)
            if compacted:
                for list_name, moved_items in compacted.items():
                    for identity, fields in moved_items.items():
                        spilled_out.setdefault(list_name, {}).setdefault(identity, {}).update(fields)
                if value_size(new_value) <= min(
                    c for c in (policy.get("max_chars"), settings.shared_state_value_hard_limit_chars) if c and c > 0
                ):
                    held_open.clear()
        if new_value is not None:
            if archive and append:
                appended_counts = {name: len(items) for name, items in append.items()}
                archive_trimmed.append(
                    fit_archive(new_value, appended_counts)
                    if archive_fit
                    else _trim_to_hard_limit(new_value, appended_counts)
                )
            refusal = size_limit_refusal(
                value_size(state.value),
                new_value,
                {name: c.get("kept_open", 0) for name, c in list_changes.items()},
            )
            if refusal:
                raise ValueError(refusal)
            if not archive:
                refusal = list_cap_refusal(state.value, new_value)
                if refusal:
                    raise ValueError(refusal)
        if label is not None:
            state.label = label.strip()[:2000] or None
        if owner_agent_id is not None:
            state.owner_agent_id = (
                safe_uuid(owner_agent_id) if owner_agent_id else None
            )
        if normalized_status is not None:
            state.status = normalized_status
            state.resolved_at = (
                now if normalized_status == SHARED_STATE_RESOLVED_STATUS else None
            )
        if new_value is not None:
            if new_value != (state.value or {}) or getattr(state, "value_updated_at", None) is None:
                state.value_updated_at = now
            state.value = new_value
        if lease is not None:
            state.is_lease = bool(lease)
        elif hold_action == "take" and getattr(state, "id", None) is None:
            # Created by its first take: the entry exists to hold the lease.
            state.is_lease = True
        if linked_item is not None:
            state.linked_work_item_id = linked_item if isinstance(linked_item, uuid.UUID) else None
        if expires is not None:
            state.expires_at = expires if isinstance(expires, datetime) else None
        if condition is not None:
            state.terminal_condition = condition or None
        if policy_set is not None:
            state.retention = policy_set or None
        if hold_action == "take":
            state.held_by_run_id = hold_run
        elif hold_action == "release":
            state.held_by_run_id = None
            if getattr(state, "is_lease", None) and normalized_status != SHARED_STATE_RESOLVED_STATUS:
                # A lease ends with its hold (#11696): retired, not left active.
                state.status = SHARED_STATE_RESOLVED_STATUS
                state.resolved_at = now
                hold_notes.append("lease released: entry resolved")
        entries = [
            {"at": now.isoformat(), "agent_id": acting_agent_id or None, "note": text[:2000]}
            for text in (*hold_notes, *([note] if note else []))
        ]
        if entries:
            log = list(state.notes or [])
            log.extend(entries)
            state.notes = log[-_MAX_NOTES:]

    def _refuse_workspace_write(state_workspace: uuid.UUID | None, writable) -> None:
        if state_workspace is not None and state_workspace not in writable:
            raise WorkspaceWriteRefusedError(_WORKSPACE_WRITE_REFUSED)

    async def _refuse_modify(db, state: SharedState, writable) -> None:
        # The row decides, not the argument: the lookup reads the READ tier,
        # so a row found there may still be one this caller may only read.
        _refuse_workspace_write(state.workspace_id, writable)
        access = await _gate_write(db, writer, state, "modify")
        if access is None:
            raise CompanyManagedKeyHiddenError(_HIDDEN_KEY)
        if (
            owner_agent_id is not None
            and state.is_company_managed
            and writer.agent_id is not None
            and access.restricted
            and str(writer.agent_id) != str(state.owner_agent_id)
            and (safe_uuid(owner_agent_id) if owner_agent_id else None)
            != state.owner_agent_id
        ):
            raise CompanyManagedWriteRefusedError(
                "Only its owner agent can hand over this company-managed shared state"
            )

    async def _refuse_foreign_owner(db) -> None:
        # A new company-managed entry an agent names another owner for is
        # governed by that owner from the start: only an agent the owner
        # admits as a writer may create it there.
        owner = safe_uuid(owner_agent_id) if owner_agent_id else safe_uuid(acting_agent_id)
        if not is_company_managed or writer.agent_id is None or owner in (None, writer.agent_id):
            return
        refuse_restricted_write(
            access_of(await load_coordination_access(db, [owner]), owner),
            writer,
            governor_id=owner,
            action="create",
            subject=_SUBJECT,
            governor_label="that owner agent",
        )

    def _answer(state: SharedState) -> dict:
        out = serialize_state(state)
        if list_changes:
            out["list_changes"] = dict(list_changes)
        if replaced_items:
            out["replaced_items"] = {k: list(v) for k, v in replaced_items.items()}
        if unset:
            out["unset_keys"] = list(unset_done)
        warning = size_warning(state.value)
        if warning:
            out["size_warning"] = warning
        if archive_trimmed and archive_trimmed[0]:
            out["archive_trimmed"] = archive_trimmed[0]
        out.update(rolled_answer)
        out.update(spilled_answer)
        out.update(overflow_answer)
        if path_appended:
            out["path_appended"] = path_appended[0]
        policy = getattr(state, "retention", None) or {}
        if held_open:
            out["retention_held_open"] = dict(held_open)
        if rolled_out and policy.get("archive_when") is None:
            out["retention_rolled_by_age"] = True
        # A rule that rolled items in this very write matched: what is left
        # over the cap is open items, which retention_held_open says.
        unmatched = None if rolled_out else unmatched_archive_rule(state.value, policy)
        if unmatched is not None:
            out["retention_unmatched"] = unmatched
        out["size_caps"] = size_caps(state.value)
        return out

    async def _write_overflow(state: SharedState) -> None:
        """Write the fields the policy moved out of the items to its overflow key, before the source commits (#12177).

        The overflow commits first, as the archive does: a source write that
        fails after it leaves a copy, never a loss. The fields land deep-merged
        under ``{list: {item: {field: value}}}``, so a later spill of the same
        item adds to it.
        """
        spilled_answer.clear()
        if not spilled_out:
            return
        policy = getattr(state, "retention", None) or {}
        base = policy.get("overflow_key")
        if not base:
            return
        count = sum(len(fields) for items in spilled_out.values() for fields in items.values())
        # A part at the hard limit refuses the spill: it rolls to the next
        # part (BASE:part-2, ...) and the pointers this write made are
        # re-pointed before the source commits, up to the read's key cap.
        target = base
        for part in range(1, OVERFLOW_RESOLVE_MAX_KEYS + 1):
            target = overflow_part_key(base, part)
            try:
                await upsert_state(
                    user_id=user_id,
                    key=target,
                    status=SHARED_STATE_ACTIVE_STATUS,
                    acting_agent_id=acting_agent_id,
                    is_company_managed=bool(state.is_company_managed),
                    workspace_id=state.workspace_id,
                    writer=writer,
                    value={name: dict(items) for name, items in spilled_out.items()},
                    deep=True,
                    note=f"moved {count} field(s) out of {key} by its retention",
                    run_id=run_id,
                )
                break
            except ValueError as exc:
                if "hard limit" not in str(exc) or part == OVERFLOW_RESOLVE_MAX_KEYS:
                    raise
        if target != base:
            moved = dict(state.value or {})
            repoint_spilled(moved, spilled_out, base, target, policy.get("item_key") or "id")
            state.value = moved
            spilled_answer["overflow_rolled_to"] = target
        spilled_answer["spilled"] = {name: len(items) for name, items in spilled_out.items()}
        spilled_answer["spilled_to"] = target

    async def _write_overflow_appends(state: SharedState) -> None:
        """Append the update's items to the overflow lists its pointers name, before the source commits (#12301).

        Grouped per overflow key, one write each under that key's row lock; a
        refused write raises and the source is never committed. A pointer that
        names no list of this entry's lists (hand-written) is refused.
        """
        overflow_answer.clear()
        if not overflow_appends:
            return
        per_key: dict[str, list[tuple[list[str], list[Any]]]] = {}
        for entry in overflow_appends:
            parsed = parse_overflow_pointer(entry["pointer"], entry["list"])
            if parsed is None or normalize_correlation_key(parsed[0]) == key:
                raise ValueError(
                    f"{entry['list']!r}: the field {entry['field']!r} of a matched item holds "
                    f"{str(entry['pointer'])[:200]!r}, not a list nor an overflow pointer of this "
                    "entry, so it cannot be appended to; nothing was written"
                )
            target, at = parsed
            per_key.setdefault(target, []).append((at, entry["items"]))
        appended: dict[str, int] = {}
        for target, pairs in per_key.items():
            written = await upsert_state(
                user_id=user_id,
                key=target,
                status=SHARED_STATE_ACTIVE_STATUS,
                acting_agent_id=acting_agent_id,
                is_company_managed=bool(state.is_company_managed),
                workspace_id=state.workspace_id,
                writer=writer,
                path_appends=pairs,
                note=f"appended to {len(pairs)} overflowed field(s) of {key} by update",
                run_id=run_id,
            )
            appended[target] = int(written.get("path_appended") or 0)
        overflow_answer["overflow_appended"] = appended

    async def _archive_rolled(state: SharedState) -> None:
        """Append what the policy rolled out to its archive key, before the source commits.

        The archive commits first: a source write that fails after it leaves a
        copy, never a loss. Without an ``archive_key`` the rolled items are
        dropped and counted. An archive at its cap drops its oldest entries.
        """
        rolled_answer.clear()
        if not rolled_out:
            return
        policy = getattr(state, "retention", None) or {}
        target = policy.get("archive_key")
        rolled_answer["rolled"] = {name: len(items) for name, items in rolled_out.items()}
        if not target:
            rolled_answer["rolled_to"] = None
            return
        count = sum(len(items) for items in rolled_out.values())
        archived = await upsert_state(
            user_id=user_id,
            key=target,
            status=SHARED_STATE_ACTIVE_STATUS,
            acting_agent_id=acting_agent_id,
            is_company_managed=bool(state.is_company_managed),
            workspace_id=state.workspace_id,
            writer=writer,
            append={name: list(items) for name, items in rolled_out.items()},
            note=f"rolled {count} item(s) out of {key} by its retention policy",
            run_id=run_id,
            archive=True,
            archive_fit=True,
        )
        rolled_answer["rolled_to"] = target
        if archived.get("archive_trimmed"):
            rolled_answer["archive_dropped"] = archived["archive_trimmed"]

    async def _lock_authorised(
        db: AsyncSession, state: SharedState, writable: Sequence[uuid.UUID]
    ) -> SharedState | None:
        # Authorise on the row as read, then re-read it FOR UPDATE: the row
        # stays locked to the commit, so a concurrent write waits and then
        # merges into what this one stored instead of both merging into the
        # same old value. Authorised again on the locked row, which a
        # concurrent write may have handed over meanwhile.
        await _refuse_modify(db, state, writable)
        locked = (
            await db.execute(
                select(SharedState)
                .where(SharedState.id == state.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars().first()
        if locked is None:
            # Deleted by a concurrent write between the read and the lock: the
            # caller goes back to the lookup, which then creates the entry.
            return None
        await _refuse_modify(db, locked, writable)
        return locked

    # The scope (owner, company-managed, workspace) of a row deleted between
    # the lookup and the lock: a retry may only write that same scope.
    vanished_scope: tuple | None = None

    def _scope_of(owner: uuid.UUID | None, managed: bool, workspace: uuid.UUID | None) -> tuple:
        # A company-managed entry belongs to the fleet whoever filed it.
        return (bool(managed), workspace, None if managed else owner)

    def _scope(row: SharedState) -> tuple:
        return _scope_of(row.user_id, bool(row.is_company_managed), row.workspace_id)

    def _lost(row: SharedState) -> None:
        nonlocal vanished_scope
        vanished_scope = _scope(row)

    def _refuse_scope_switch(row: SharedState) -> None:
        # The vanished row is gone for good, so any row a retry finds under the
        # key is another physical entry (another creator's, another scope):
        # never redirect the write to it. Only a create in the same scope may
        # follow a delete.
        if vanished_scope is not None:
            raise ValueError(
                f"the shared state {key!r} this write was updating was deleted "
                "meanwhile, and the entry now found under that key is another "
                "one; nothing was written: read it and write again"
            )

    async def _upsert_once(
        db: AsyncSession, readable: Sequence[uuid.UUID], writable: Sequence[uuid.UUID]
    ) -> dict | None:
        hold_notes.clear()  # a retry re-checks the hold on the row it finds
        state = (
            await db.execute(_visible_state_query(uid, key, readable, writer))
        ).scalars().first()
        if state is not None:
            _refuse_scope_switch(state)
        if state is None:
            # Creating a row with no owning user, not company-managed and not
            # filed into a workspace would be invisible to everyone and
            # effectively undeletable via the API/UI. That orphan only arises
            # for an anonymous caller on a non-company-managed agent - reject
            # it rather than persist dead state. (An anonymous caller updating
            # an EXISTING visible company-managed row is fine and is handled
            # above, where ``state`` is found.)
            if uid is None and not is_company_managed and workspace_id is None:
                raise ValueError(
                    "shared state must belong to a user or be company-managed; "
                    "an anonymous, non-company-managed entry would be invisible "
                    "to everyone"
                )
            # "Resolve never creates": refuse to materialize a brand-new entry
            # straight into a terminal (resolved) status. This closes the race
            # where a human-UI / REST "resolve" (key + status=resolved) lands
            # after the row was deleted or purged and would otherwise create a
            # phantom resolved entry (empty value, no real history). Resolving is
            # only meaningful for an entry that already exists - the dedicated
            # resolve_state / resolve_state_by_id paths enforce the same rule.
            if normalized_status == SHARED_STATE_RESOLVED_STATUS:
                raise ValueError(
                    "cannot create a new shared-state entry directly as "
                    "resolved; resolve an existing entry instead"
                )
            if (
                vanished_scope is not None
                and _scope_of(uid, bool(is_company_managed), workspace_id) != vanished_scope
            ):
                raise ValueError(
                    f"the shared state {key!r} this write was updating was deleted "
                    "meanwhile, and recreating it here would file it in another "
                    "scope (owner, company-managed flag or workspace); nothing was "
                    "written: read it and write again"
                )
            await _refuse_foreign_owner(db)
            state = SharedState(
                user_id=uid,
                is_company_managed=bool(is_company_managed),
                key=key,
                label=(label or None),
                value={},
                notes=[],
                workspace_id=workspace_id,
            )
            _apply(state)
            # Crystal-clear ownership: a brand-new entry defaults its owner to the
            # creating agent so there is always an agent responsible for resolving
            # and purging it later. An explicit owner_agent_id (set in _apply) wins.
            if state.owner_agent_id is None and acting_agent_id:
                state.owner_agent_id = safe_uuid(acting_agent_id)
            await _write_overflow(state)
            await _write_overflow_appends(state)
            await _archive_rolled(state)
            db.add(state)
            try:
                await db.commit()
            except IntegrityError:
                # A concurrent first-touch won the unique constraint - reload
                # and apply our changes as an update instead.
                await db.rollback()
                state = (
                    await db.execute(_visible_state_query(uid, key, readable, writer))
                ).scalars().first()
                if state is None:
                    if _narrows(writer) and await _key_held_out_of_sight(db, uid, key):
                        raise CompanyManagedKeyHiddenError(_HIDDEN_KEY) from None
                    raise
                _refuse_scope_switch(state)
                locked = await _lock_authorised(db, state, writable)
                if locked is None:
                    _lost(state)
                    return None
                state = locked
                await _check_hold(db, state)
                _apply(state)
                await _write_overflow(state)
                await _write_overflow_appends(state)
                await _archive_rolled(state)
                await db.commit()
            await db.refresh(state)
            return _answer(state)

        locked = await _lock_authorised(db, state, writable)
        if locked is None:
            _lost(state)
            return None
        await _check_hold(db, locked)
        _apply(locked)
        await _write_overflow(locked)
        await _write_overflow_appends(locked)
        await _archive_rolled(locked)
        await db.commit()
        await db.refresh(locked)
        return _answer(locked)

    async with async_session() as db:
        for _attempt in range(_UPSERT_ATTEMPTS):
            # Read on every attempt: a workspace archived or a grant revoked
            # during a delete race is honoured by the retry.
            readable, writable = await _workspace_tiers(db, user_id)
            # Filing a new row needs the write tier: a create has no row yet for
            # the check in `_refuse_modify` to read.
            _refuse_workspace_write(workspace_id, writable)
            answer = await _upsert_once(db, readable, writable)
            if answer is not None:
                return answer
            # The row was deleted between the lookup and the lock:
            # look the key up again, which now creates the entry.
            await db.rollback()
        raise ValueError(
            f"the shared state {key!r} was deleted by concurrent writes on every "
            f"one of {_UPSERT_ATTEMPTS} attempts; nothing was written"
        )


async def resolve_state(
    *,
    user_id: str | None,
    key: str,
    note: str | None = None,
    acting_agent_id: str | None = None,
    writer: CompanyManagedWriter,
) -> dict | None:
    """Mark an existing visible entry ``resolved`` (terminal) with an optional note.

    Returns the serialized entry, or ``None`` when no entry the caller may write
    exists for the key (resolve never *creates* an entry - that would be
    meaningless). Stamps ``resolved_at`` so the retention sweep can eventually
    purge it. Raises :class:`CompanyManagedWriteRefusedError` for a
    company-managed entry *writer* may only read.
    """
    from app.core.database import async_session

    key = normalize_correlation_key(key)
    if not key:
        return None
    uid = safe_uuid(user_id) if user_id else None
    now = datetime.now(UTC)
    async with async_session() as db:
        # The WRITABLE workspace tier: a `read_only` grantee resolving state
        # other agents are acting on is the same escalation as writing it.
        _readable, writable = await _workspace_tiers(db, user_id)
        state = (
            await db.execute(_visible_state_query(uid, key, writable, writer))
        ).scalars().first()
        if state is None or await _gate_write(db, writer, state, "resolve") is None:
            return None
        state.status = SHARED_STATE_RESOLVED_STATUS
        state.resolved_at = now
        if note:
            entry = {
                "at": now.isoformat(),
                "agent_id": acting_agent_id or None,
                "note": note[:2000],
            }
            log = list(state.notes or [])
            log.append(entry)
            state.notes = log[-_MAX_NOTES:]
        await db.commit()
        await db.refresh(state)
        return serialize_state(state, now=now)


async def resolve_state_by_id(
    *,
    user_id: str | None,
    state_id: str,
    note: str | None = None,
    acting_agent_id: str | None = None,
    writer: CompanyManagedWriter,
) -> dict | None:
    """Mark a specific visible entry (by its UUID) ``resolved`` (terminal).

    The id-addressed companion to :func:`resolve_state`, for the human REST/UI
    surface: the UI resolves the exact row it is showing, so it must target that
    row by id rather than by key. Several visible rows can share one key (the
    caller's own row plus any company-managed rows), and the key lookup applies
    own-first / most-recent tie-breaks - so a key-addressed resolve could flip a
    *different* row than the one the user clicked. Returns the serialized entry,
    or ``None`` when no entry the caller may write has that id (resolve never
    *creates*). Raises :class:`CompanyManagedWriteRefusedError` for a
    company-managed entry *writer* may only read.
    """
    from app.core.database import async_session

    sid = safe_uuid(state_id)
    if sid is None:
        return None
    uid = safe_uuid(user_id) if user_id else None
    now = datetime.now(UTC)
    async with async_session() as db:
        _readable, writable = await _workspace_tiers(db, user_id)
        state = (
            await db.execute(
                select(SharedState).where(
                    SharedState.id == sid, _visible_clause(uid, writable)
                )
            )
        ).scalar_one_or_none()
        if state is None or await _gate_write(db, writer, state, "resolve") is None:
            return None
        state.status = SHARED_STATE_RESOLVED_STATUS
        state.resolved_at = now
        if note:
            entry = {
                "at": now.isoformat(),
                "agent_id": acting_agent_id or None,
                "note": note[:2000],
            }
            log = list(state.notes or [])
            log.append(entry)
            state.notes = log[-_MAX_NOTES:]
        await db.commit()
        await db.refresh(state)
        return serialize_state(state, now=now)


async def release_ended_run_holds(
    *, limit: int = 500, run_ids: Sequence | None = None
) -> int:
    """Release every shared-state hold whose holding run has ended.

    The lease twin of ``work_item.release_ended_run_claims``, run on the same
    scheduler tick and from the same paths that end a run: an entry held by a
    run (or background task) that reached a terminal status, was
    dead-lettered, or is no longer recorded has ``held_by_run_id`` cleared and
    a dated note naming the run and how it ended. Value and owner are
    untouched; a lease entry (``is_lease``) is resolved with it (#11696), any
    other keeps its status. The write re-applies the predicate (the holder unchanged and
    still not alive), so an entry taken over between the read and the write is
    never touched. A system write: it runs with no writer. *run_ids* scopes the
    sweep to those holders; an empty or unreadable list releases nothing.
    Returns how many entries were released.
    """
    from sqlalchemy import update

    from app.core.database import async_session
    from app.services.work_item import _holder_alive_clause, holding_runs

    now = datetime.now(UTC)
    held = SharedState.held_by_run_id.is_not(None)
    if run_ids is not None:
        scoped = {safe_uuid(str(rid)) for rid in run_ids if rid is not None}
        scoped.discard(None)
        if not scoped:
            return 0
        held = and_(held, SharedState.held_by_run_id.in_(sorted(scoped, key=str)))
    released = 0
    async with async_session() as db:
        rows = (
            await db.execute(
                select(SharedState.id, SharedState.held_by_run_id, SharedState.notes, SharedState.is_lease)
                .where(held, not_(_holder_alive_clause(SharedState.held_by_run_id)))
                .order_by(SharedState.updated_at.asc())
                .limit(max(int(limit), 1))
            )
        ).all()
        if not rows:
            return 0
        holders = await holding_runs(db, [row[1] for row in rows])
        for state_id, run_id, notes, is_lease in (tuple(row) + (None,) * (4 - len(row)) for row in rows):
            holder = holders.get(run_id)
            if holder is not None and holder.alive:  # came back between the reads
                continue
            text = _hold_note("released", run_id, None, holder, now)
            changes: dict[str, object] = {}
            if is_lease:
                text += "; lease entry resolved"
                changes = {"status": SHARED_STATE_RESOLVED_STATUS, "resolved_at": now}
            entry = {"at": now.isoformat(), "agent_id": None, "note": text}
            res = await db.execute(
                update(SharedState)
                .where(
                    SharedState.id == state_id,
                    SharedState.held_by_run_id == run_id,
                    not_(_holder_alive_clause(SharedState.held_by_run_id)),
                )
                .values(held_by_run_id=None, notes=[*(notes or []), entry][-_MAX_NOTES:], **changes)
            )
            if res.rowcount:
                released += res.rowcount
                logger.info(
                    "Shared state %s hold released: its holding run %s ended (%s)",
                    state_id,
                    run_id,
                    _holder_label(holder),
                )
        await db.commit()
    return released


async def purge_resolved_states(*, older_than: timedelta, limit: int = 500) -> int:
    """Delete RESOLVED entries whose last update is older than ``older_than``.

    The fleet-wide retention backstop so resolved shared state never lives forever
    even if the owning agent forgets to purge it (the agent-driven path remains
    the primary one). Capped per call and oldest-resolved first, like the other
    autonomous cleanup sweeps. ACTIVE entries are NEVER touched - a long-lived
    source of truth may legitimately update rarely.

    The window counts from ``updated_at`` (#2070), the entry's last update, as
    the Settings retention says: a resolved entry still written to (a note, a
    re-resolve) is kept for the whole window after that write.
    """
    from app.core.database import async_session

    cutoff = datetime.now(UTC) - older_than
    anchor = SharedState.updated_at
    async with async_session() as db:
        target_ids = (
            await db.execute(
                select(SharedState.id)
                .where(
                    SharedState.status == SHARED_STATE_RESOLVED_STATUS,
                    anchor < cutoff,
                )
                .order_by(anchor.asc())
                .limit(min(max(limit, 1), 5000))
            )
        ).scalars().all()
        if not target_ids:
            return 0
        # Re-apply BOTH retention predicates to the DELETE itself (not just the
        # SELECT that chose the ids), so the DELETE - not the now-stale id list -
        # decides what is purged. Between the SELECT and the DELETE an entry can,
        # in its own transaction, be (a) reactivated by upsert_state (status flips
        # to active) or (b) re-resolved with a fresh note (updated_at bumped back
        # within the retention window). Re-checking
        # status == resolved AND anchor < cutoff means neither an active entry nor
        # a freshly re-resolved (no-longer-stale) one is ever purged - fully
        # closing the TOCTOU window and keeping "only resolved entries past
        # retention are purged" true under concurrency (mirrors the
        # bulk_delete_states guard).
        result = await db.execute(
            delete(SharedState).where(
                SharedState.id.in_(target_ids),
                SharedState.status == SHARED_STATE_RESOLVED_STATUS,
                anchor < cutoff,
            )
        )
        await db.commit()
    return result.rowcount or 0


def drop_archived_before(value: dict, cutoff: datetime) -> tuple[dict, int]:
    """*value* without the platform-archived object items stamped before *cutoff*.

    Only items carrying :data:`ARCHIVE_STAMP` in a top-level list are read; an
    unparseable stamp is kept. Answers the new value and the count dropped.
    """
    dropped = 0
    out = dict(value)
    for name, items in value.items():
        if not isinstance(items, list):
            continue
        kept = []
        for item in items:
            stamp = item.get(ARCHIVE_STAMP) if isinstance(item, dict) else None
            try:
                at = datetime.fromisoformat(stamp) if isinstance(stamp, str) else None
            except ValueError:
                at = None
            if at is not None and at.tzinfo is None:
                at = at.replace(tzinfo=UTC)
            if at is not None and at < cutoff:
                dropped += 1
                continue
            kept.append(item)
        if len(kept) != len(items):
            out[name] = kept
    return out, dropped


async def purge_archived_items(*, older_than: timedelta, limit: int = 200) -> int:
    """Drop platform-archived list items older than ``older_than`` from every entry.

    The retention of ``archive_key``: an archive is an active entry, which
    :func:`purge_resolved_states` never touches, so its stamped items are
    purged here, oldest entries first, at most *limit* entries per call, each
    under its row lock. Items without the stamp are never touched. Answers the
    number of items dropped.
    """
    from sqlalchemy import String, cast

    from app.core.database import async_session

    cutoff = datetime.now(UTC) - older_than
    dropped_total = 0
    async with async_session() as db:
        ids = (
            await db.execute(
                select(SharedState.id)
                .where(cast(SharedState.value, String).contains(f'"{ARCHIVE_STAMP}"'))
                .order_by(SharedState.updated_at.asc())
                .limit(min(max(limit, 1), 1000))
            )
        ).scalars().all()
        for state_id in ids:
            state = (
                await db.execute(select(SharedState).where(SharedState.id == state_id).with_for_update())
            ).scalar_one_or_none()
            if state is None or not isinstance(state.value, dict):
                continue
            new_value, dropped = drop_archived_before(state.value, cutoff)
            if dropped:
                state.value = new_value
                dropped_total += dropped
        await db.commit()
    return dropped_total


def _system_note(now: datetime, text: str) -> dict[str, object]:
    return {"at": now.isoformat(), "agent_id": None, "note": text[:2000]}


async def resolve_terminal_linked_states(*, limit: int = 500) -> int:
    """Resolve every ACTIVE entry whose linked work item is done, cancelled or gone (#11696).

    A system write of the scheduler tick, like :func:`release_ended_run_holds`:
    no writer, a dated note naming the item and how it ended, value and owner
    untouched. The update re-applies ``status == active`` and the same link,
    so an entry re-linked or resolved meanwhile is never touched. Answers how
    many entries were resolved.
    """
    from sqlalchemy import update

    from app.core.database import async_session
    from app.models.work_item import WORK_ITEM_TERMINAL_STATUSES, WorkItem

    now = datetime.now(UTC)
    resolved = 0
    async with async_session() as db:
        rows = (
            await db.execute(
                select(SharedState.id, SharedState.linked_work_item_id, SharedState.notes)
                .where(
                    SharedState.status == SHARED_STATE_ACTIVE_STATUS,
                    SharedState.linked_work_item_id.is_not(None),
                )
                .order_by(SharedState.updated_at.asc())
                .limit(max(int(limit), 1))
            )
        ).all()
        if not rows:
            return 0
        item_ids = {row[1] for row in rows}
        found = await db.execute(select(WorkItem.id, WorkItem.status).where(WorkItem.id.in_(item_ids)))
        statuses: dict[uuid.UUID, str] = {row[0]: row[1] for row in found.all()}
        for state_id, item_id, notes in rows:
            status = statuses.get(item_id)
            if status is not None and status not in WORK_ITEM_TERMINAL_STATUSES:
                continue
            how = status or "no longer recorded"
            entry = _system_note(now, f"resolved: its linked work item {item_id} is {how}")
            res = await db.execute(
                update(SharedState)
                .where(
                    SharedState.id == state_id,
                    SharedState.status == SHARED_STATE_ACTIVE_STATUS,
                    SharedState.linked_work_item_id == item_id,
                )
                .values(
                    status=SHARED_STATE_RESOLVED_STATUS,
                    resolved_at=now,
                    notes=[*(notes or []), entry][-_MAX_NOTES:],
                )
            )
            resolved += getattr(res, "rowcount", 0) or 0
        await db.commit()
    return resolved


async def resolve_expired_states(*, limit: int = 500) -> int:
    """Resolve every ACTIVE entry past its ``expires_at`` (#11696), with a dated note.

    A system write of the scheduler tick; the update re-applies the predicate,
    so an entry whose date moved meanwhile is never touched.
    """
    from sqlalchemy import update

    from app.core.database import async_session

    now = datetime.now(UTC)
    expired = and_(
        SharedState.status == SHARED_STATE_ACTIVE_STATUS,
        SharedState.expires_at.is_not(None),
        SharedState.expires_at <= now,
    )
    resolved = 0
    async with async_session() as db:
        rows = (
            await db.execute(
                select(SharedState.id, SharedState.expires_at, SharedState.notes)
                .where(expired)
                .order_by(SharedState.expires_at.asc())
                .limit(max(int(limit), 1))
            )
        ).all()
        for state_id, expires, notes in rows:
            at = _iso_or_none(_aware(expires))
            entry = _system_note(now, f"resolved: expired at {at}")
            res = await db.execute(
                update(SharedState)
                .where(SharedState.id == state_id, expired)
                .values(
                    status=SHARED_STATE_RESOLVED_STATUS,
                    resolved_at=now,
                    notes=[*(notes or []), entry][-_MAX_NOTES:],
                )
            )
            resolved += getattr(res, "rowcount", 0) or 0
        await db.commit()
    return resolved


async def compact_oversize_states(*, limit: int = 100) -> int:
    """Cut every entry over the hard limit or the list cap back under them (#11696).

    The backstop of the write-time limits for entries written before them:
    :func:`compact_value` drops the oldest list items, under the row lock,
    with a dated note giving the count. Every status is compacted (a resolved
    entry keeps its audit trail, within the limits). Answers the number of
    items dropped.
    """
    from sqlalchemy import String, cast

    from app.core.database import async_session

    hard = settings.shared_state_value_hard_limit_chars
    cap = settings.shared_state_list_max_items
    if (not hard or hard <= 0) and (not cap or cap <= 0):
        return 0
    # A cheap SQL pre-filter: a value whose JSON text is under the list cap's
    # floor and the hard limit cannot be over either, so only long texts load.
    floor = min(x for x in (hard, cap * 2 if cap and cap > 0 else 0) if x and x > 0)
    now = datetime.now(UTC)
    dropped_total = 0
    async with async_session() as db:
        ids = (
            (
                await db.execute(
                    select(SharedState.id)
                    .where(func.length(cast(SharedState.value, String)) > floor)
                    .order_by(SharedState.updated_at.asc())
                    .limit(min(max(limit, 1), 1000))
                )
            )
            .scalars()
            .all()
        )
        for state_id in ids:
            state = (
                await db.execute(select(SharedState).where(SharedState.id == state_id).with_for_update())
            ).scalar_one_or_none()
            if state is None or not isinstance(state.value, dict):
                continue
            before = value_size(state.value)
            new_value, dropped = compact_value(state.value)
            if not dropped:
                continue
            state.value = new_value
            state.notes = [
                *(state.notes or []),
                _system_note(
                    now,
                    f"compacted by the platform: {dropped} oldest list item(s) dropped, "
                    f"{before} -> {value_size(new_value)} characters (hard limit {hard}, "
                    f"list cap {cap})",
                ),
            ][-_MAX_NOTES:]
            dropped_total += dropped
            logger.info("Shared state %s compacted: %d item(s) dropped", state_id, dropped)
        await db.commit()
    return dropped_total


# ── Hygiene counts per fleet (#11696 follow-up) ───────────────────────────
# What GET /fleets/{id}/health shows and what the hourly sweep reports to each
# fleet's orchestrator. ``orphaned_leases`` and ``terminal_linked`` are what the
# sweep fixes by itself (a count above 0 between two ticks only); ``oversize``
# and ``stale`` need an agent's decision and are the ones reported.
SHARED_STATE_HYGIENE_FIELDS: tuple[str, ...] = ("oversize", "stale", "orphaned_leases", "terminal_linked")
SHARED_STATE_HYGIENE_REPORTED: tuple[str, ...] = ("oversize", "stale")
SHARED_STATE_HYGIENE_KEY_PREFIX = "shared-state-hygiene:"


def _hygiene_count_columns(now: datetime) -> list:
    """One ``SUM(CASE ...)`` per field of :data:`SHARED_STATE_HYGIENE_FIELDS`."""
    from sqlalchemy import String, cast, exists

    from app.models.work_item import WORK_ITEM_TERMINAL_STATUSES, WorkItem
    from app.services.work_item import _holder_alive_clause

    soft = settings.shared_state_value_soft_limit_chars
    active = SharedState.status == SHARED_STATE_ACTIVE_STATUS
    oversize = and_(active, func.length(cast(SharedState.value, String)) > soft) if soft and soft > 0 else false()
    stale = _stale_clause(now)
    orphaned = and_(SharedState.held_by_run_id.is_not(None), not_(_holder_alive_clause(SharedState.held_by_run_id)))
    live_item = exists(
        select(WorkItem.id).where(
            WorkItem.id == SharedState.linked_work_item_id,
            WorkItem.status.not_in(WORK_ITEM_TERMINAL_STATUSES),
        )
    )
    terminal_linked = and_(active, SharedState.linked_work_item_id.is_not(None), not_(live_item))
    clauses = (oversize, stale if stale is not None else false(), orphaned, terminal_linked)
    return [
        func.coalesce(func.sum(sa_case((clause, 1), else_=0)), 0).label(name)
        for name, clause in zip(SHARED_STATE_HYGIENE_FIELDS, clauses, strict=True)
    ]


async def shared_state_hygiene_counts(db: AsyncSession, scope, *, now: datetime | None = None) -> dict[str, int]:
    """``{oversize, stale, orphaned_leases, terminal_linked}`` of the entries *scope* selects.

    One aggregate query. *scope* is a SQL clause on :class:`SharedState` (a
    fleet's population, ``coordination_scope.fleet_shared_states_clause``,
    optionally narrowed to what a caller sees); ``oversize`` is over the soft
    limit, the size flag the listing shows.
    """
    row = (await db.execute(select(*_hygiene_count_columns(now or datetime.now(UTC))).where(scope))).first()
    values = tuple(row) if row is not None else ()
    return {name: int(values[i] or 0) if i < len(values) else 0 for i, name in enumerate(SHARED_STATE_HYGIENE_FIELDS)}


async def visible_shared_state_hygiene_counts(db: AsyncSession, *, user_id: str | None, scope) -> dict[str, int]:
    """The hygiene counts of *scope* limited to what *user_id* may read (the fleet health strip)."""
    uid = safe_uuid(user_id) if user_id else None
    readable, _writable = await _workspace_tiers(db, user_id)
    return await shared_state_hygiene_counts(db, and_(_visible_clause(uid, readable), scope))


def hygiene_report_description(fleet_name: str, counts: dict[str, int]) -> str:
    """The body of the item the sweep files on a fleet's orchestrator."""
    soft = settings.shared_state_value_soft_limit_chars
    window = settings.shared_state_stale_after_days
    return (
        f"Shared-state hygiene of the fleet '{fleet_name}', counted by the platform's hourly sweep.\n"
        f"- Oversize (value over {soft} characters): {counts.get('oversize', 0)}. Bound the longest list "
        "(keep_last_json with archive_key, or remove_json the terminal items) or split the entry.\n"
        f"- Stale (value unchanged for {window} days, or past its expires_at): {counts.get('stale', 0)}. "
        "Resolve what has concluded, or link it to its work item (work_item_id) or a date (expires_at).\n"
        "Orphaned leases and entries whose linked work item ended are resolved by the platform itself.\n"
        "list_shared_state(stale_only=true) lists the stale ones; the size flag shows on each entry. "
        "This item is refreshed, never duplicated, while it stays open."
    )


async def report_shared_state_hygiene(*, now: datetime | None = None) -> int:
    """File ONE work item per fleet on its orchestrator for what the sweep cannot fix (#11696).

    Every fleet (seeded or custom, not archived) with an enabled orchestrator
    and at least one oversize or stale entry gets an item keyed
    ``shared-state-hygiene:<fleet id>``, deduplicated on that key: while one is
    open the sweep files nothing new. A refusal for one fleet (its workspace
    no longer writable) is logged and the others still run. Answers how many
    items were filed.
    """
    from app.core.database import async_session
    from app.models.agent import Agent
    from app.models.fleet import Fleet
    from app.services.company_managed_writes import PLATFORM_WRITER
    from app.services.coordination_scope import fleet_shared_states_clause
    from app.services.work_item import create_work_item

    now = now or datetime.now(UTC)
    async with async_session() as db:
        fleets = (
            await db.execute(
                select(Fleet.id, Fleet.name, Fleet.user_id, Fleet.workspace_id, Fleet.orchestrator_agent_id)
                .join(Agent, Agent.id == Fleet.orchestrator_agent_id)
                .where(Fleet.is_archived.is_(False), Agent.enabled.is_(True))
            )
        ).all()
        counted = []
        for fleet_id, name, owner, workspace_id, orchestrator in fleets:
            counts = await shared_state_hygiene_counts(db, fleet_shared_states_clause(fleet_id), now=now)
            if any(counts.get(f, 0) for f in SHARED_STATE_HYGIENE_REPORTED):
                counted.append((fleet_id, name, owner, workspace_id, orchestrator, counts))
    filed = 0
    for fleet_id, name, owner, workspace_id, orchestrator, counts in counted:
        try:
            item = await create_work_item(
                user_id=str(owner) if owner else None,
                is_company_managed=owner is None,
                title=f"Shared-state hygiene: {counts['oversize']} oversize, {counts['stale']} stale",
                description=hygiene_report_description(name, counts),
                owner_agent_id=str(orchestrator),
                priority="low",
                correlation_key=f"{SHARED_STATE_HYGIENE_KEY_PREFIX}{fleet_id}",
                workspace_id=workspace_id,
                payload={"shared_state_hygiene": {"fleet_id": str(fleet_id), **counts}},
                dedupe=True,
                dedupe_by="key",
                writer=PLATFORM_WRITER,
            )
        except Exception as exc:  # one fleet's refusal never stops the others
            logger.warning("Shared-state hygiene report for fleet %s not filed: %s", fleet_id, exc)
            continue
        if not item.get("deduplicated"):
            filed += 1
    return filed


async def sweep_shared_state_lifecycle() -> dict[str, int]:
    """The hygiene step of the scheduler tick (#11696), each part best-effort.

    Resolves the entries whose linked work item ended and those past their
    ``expires_at``, then compacts the oversize ones, then files one item per
    fleet on its orchestrator for what is left (:func:`report_shared_state_hygiene`).
    Released leases are resolved by :func:`release_ended_run_holds` on the
    same tick. Answers the count of each part.
    """
    out: dict[str, int] = {}
    for name, step in (
        ("linked_resolved", resolve_terminal_linked_states),
        ("expired_resolved", resolve_expired_states),
        ("items_compacted", compact_oversize_states),
        ("orchestrator_items", report_shared_state_hygiene),
    ):
        try:
            out[name] = await step()
        except Exception as exc:
            logger.error("Shared-state hygiene %s failed: %s", name, exc, exc_info=True)
            out[name] = 0
    return out


async def delete_state(
    *, user_id: str | None, key: str, writer: CompanyManagedWriter
) -> bool:
    """Delete a shared-state entry the caller may write, by key. True when removed.

    Raises :class:`CompanyManagedWriteRefusedError` for a company-managed entry
    *writer* may only read.
    """
    from app.core.database import async_session

    key = normalize_correlation_key(key)
    if not key:
        return False
    uid = safe_uuid(user_id) if user_id else None
    async with async_session() as db:
        # The WRITABLE workspace tier: removing work is the same escalation as
        # writing it.
        _readable, writable = await _workspace_tiers(db, user_id)
        state = (
            await db.execute(_visible_state_query(uid, key, writable, writer))
        ).scalars().first()
        if state is None or await _gate_write(db, writer, state, "delete") is None:
            return False
        await db.delete(state)
        await db.commit()
    return True


async def delete_state_by_id(
    *, user_id: str | None, state_id: str, writer: CompanyManagedWriter
) -> bool:
    """Delete a shared-state entry the caller may write, by its UUID.

    Raises :class:`CompanyManagedWriteRefusedError` for a company-managed entry
    *writer* may only read.
    """
    from app.core.database import async_session

    sid = safe_uuid(state_id)
    if sid is None:
        return False
    uid = safe_uuid(user_id) if user_id else None
    async with async_session() as db:
        _readable, writable = await _workspace_tiers(db, user_id)
        state = (
            await db.execute(
                select(SharedState).where(
                    SharedState.id == sid, _visible_clause(uid, writable)
                )
            )
        ).scalar_one_or_none()
        if state is None or await _gate_write(db, writer, state, "delete") is None:
            return False
        await db.delete(state)
        await db.commit()
    return True
