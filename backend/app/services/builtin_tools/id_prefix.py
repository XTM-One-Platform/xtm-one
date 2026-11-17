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

"""An unambiguous id prefix taken where a built-in tool expects a UUID.

Agents, skills, notes and people name objects by the first characters of
their id (``48b4e177``). A built-in tool that takes an object id accepts such
a prefix: a full UUID is taken as it always was; a hexadecimal prefix of
:data:`ID_PREFIX_MIN_CHARS` or more characters (dashes allowed where a UUID
has them) is looked up among the objects of that type the caller may see,
with the tool's own visibility clause, so the lookup never widens what the
caller reads. One match stands for the full id; none leaves the value as it
was, so the tool answers its usual "not found"; several are refused with the
candidates named so the model picks one. REST, webhook and UI ids are not
concerned: only the tools call this.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select

#: The shortest prefix taken for an id: the 8 characters ids are quoted by.
ID_PREFIX_MIN_CHARS = 8
#: Candidates an ambiguous prefix names; one more is read to know there are more.
MAX_CANDIDATES = 5

_HEX = frozenset("0123456789abcdef")
#: Where the dashes of a UUID's text sit, counted in hexadecimal digits.
_GROUP_ENDS = (8, 12, 16, 20, 32)


def _dashed(hexed: str) -> str:
    """*hexed* (dashes removed) written as the start of a UUID's text."""
    parts: list[str] = []
    start = 0
    for end in _GROUP_ENDS:
        if start >= len(hexed):
            break
        parts.append(hexed[start:end])
        start = end
    return "-".join(parts)


def id_prefix(value: object) -> str | None:
    """The start of a UUID's text *value* stands for, lower-case and dashed.

    ``None`` when *value* is a full UUID (taken as it is), is not a string,
    is shorter than :data:`ID_PREFIX_MIN_CHARS` hexadecimal digits, holds a
    character that is not one, or puts a dash where a UUID has none.
    """
    if not isinstance(value, str):
        return None
    raw = value.strip().lower()
    hexed = raw.replace("-", "")
    if not ID_PREFIX_MIN_CHARS <= len(hexed) < 32 or not set(hexed) <= _HEX:
        return None
    canonical = _dashed(hexed)
    if "-" in raw and raw != canonical:
        return None
    return canonical


def prefix_bounds(prefix: str) -> tuple[uuid.UUID, uuid.UUID]:
    """The lowest and the highest UUID whose text starts with *prefix* (as
    :func:`id_prefix` writes it)."""
    hexed = prefix.replace("-", "")
    return uuid.UUID(hexed.ljust(32, "0")), uuid.UUID(hexed.ljust(32, "f"))


def id_prefix_clause(column: Any, prefix: str) -> Any:
    """*column* (a UUID column) holding an id that starts with *prefix*.

    A range on the UUID itself, not a match on its text: the primary key's
    index serves it, so a prefix looked up among millions of runs, traces or
    spans reads only the rows it names. A UUID orders by its bytes, which is
    the order of its hexadecimal text."""
    low, high = prefix_bounds(prefix)
    return column.between(low, high)


async def ids_by_prefix(
    db: Any,
    model: Any,
    prefix: str,
    clauses: Sequence[Any] = (),
    *,
    label: Any = None,
) -> list[tuple[uuid.UUID, str]]:
    """The ``(id, label)`` of the *model* rows whose id starts with *prefix*
    and that pass every one of *clauses* (the tool's visibility), at most
    :data:`MAX_CANDIDATES` + 1, by id."""
    label_column = label if label is not None else getattr(model, "name", None)
    columns = [model.id] + ([label_column] if label_column is not None else [])
    stmt = (
        select(*columns)
        .where(id_prefix_clause(model.id, prefix), *clauses)
        .order_by(model.id)
        .limit(MAX_CANDIDATES + 1)
    )
    rows = (await db.execute(stmt)).all()
    return [(row[0], str(row[1]) if len(row) > 1 and row[1] is not None else "") for row in rows]


def ambiguous_text(value: str, noun: str, candidates: Sequence[tuple[object, str]]) -> str:
    """The refusal of a prefix *value* that starts the id of several *noun*."""
    shown = list(candidates)[:MAX_CANDIDATES]
    more = " and more" if len(candidates) > MAX_CANDIDATES else ""
    named = "; ".join(f"`{cid}` ({label})" if label else f"`{cid}`" for cid, label in shown)
    count = f"{len(candidates)}" if not more else f"more than {MAX_CANDIDATES}"
    return (
        f"Error: `{value.strip()}` is the start of the id of {count} {noun} you can see: {named}{more}. "
        "Nothing was done: call again with the full id of the one you mean."
    )


def unknown_prefix_text(value: str, noun: str) -> str:
    """The refusal of a prefix *value* that starts the id of no *noun* the
    caller can see: an argument that writes or filters is never dropped or
    widened when its prefix resolves nothing."""
    return (
        f"Error: no {noun} you can see has an id starting with `{value.strip()}`. "
        "Nothing was done: call again with the full id, or a prefix of one you can see."
    )


async def distinct_ids_by_prefix(
    db: Any, column: Any, prefix: str, clauses: Sequence[Any] = ()
) -> list[tuple[Any, str]]:
    """The distinct values of *column* (a UUID column, not necessarily a key)
    that start with *prefix* in the rows passing *clauses*, at most
    :data:`MAX_CANDIDATES` + 1, as ``(id, "")`` candidates."""
    stmt = (
        select(column)
        .where(id_prefix_clause(column, prefix), *clauses)
        .distinct()
        .order_by(column)
        .limit(MAX_CANDIDATES + 1)
    )
    return [(row[0], "") for row in (await db.execute(stmt)).all()]


def resolved_or_refused(
    value: Any, noun: str, candidates: Sequence[tuple[object, str]]
) -> tuple[Any, str | None]:
    """``(full id, None)`` for one candidate, ``(value, None)`` for none (the
    tool answers its usual not found), ``(value, refusal)`` for several."""
    if len(candidates) == 1:
        return str(candidates[0][0]), None
    if not candidates:
        return value, None
    return value, ambiguous_text(str(value), noun, candidates)


async def resolve_id_prefix(
    db: Any,
    model: Any,
    value: Any,
    clauses: Sequence[Any] = (),
    *,
    noun: str,
    label: Any = None,
) -> tuple[Any, str | None]:
    """*value* with an id prefix replaced by the one full id it stands for.

    A full UUID or anything that is not an id prefix comes back unchanged and
    reads nothing. See :func:`resolved_or_refused` for the answers.
    """
    prefix = id_prefix(value)
    if prefix is None:
        return value, None
    candidates = await ids_by_prefix(db, model, prefix, clauses, label=label)
    return resolved_or_refused(value, noun, candidates)


__all__ = [
    "ID_PREFIX_MIN_CHARS",
    "MAX_CANDIDATES",
    "ambiguous_text",
    "distinct_ids_by_prefix",
    "id_prefix",
    "id_prefix_clause",
    "ids_by_prefix",
    "prefix_bounds",
    "resolve_id_prefix",
    "resolved_or_refused",
    "unknown_prefix_text",
]
