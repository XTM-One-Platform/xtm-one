# SPDX-License-Identifier: Apache-2.0

"""A ``json`` column value PostgreSQL stores but cannot parse.

``json_in`` only checks the syntax and keeps the text verbatim, so a ``\\u0000``
escape or a lone UTF-16 surrogate escape (``"\\ud800"``) is stored as sent, and
so is a number past ``numeric``'s range (``1e131072``), which only ``jsonb``
refuses.
Everything that makes PostgreSQL de-escape the document - a ``::jsonb`` cast,
``->``, ``->>``, ``json_each`` - then refuses the row, and the error aborts the
whole statement: one row takes the query down for every caller. ``json_typeof``
and ``json_array_length`` read the text without de-escaping it and never fail.

:func:`json_unparseable` is the exact test, in SQL, and
:func:`when_json_parseable` guards a predicate with it. A ``CASE`` rather than
an ``AND``: PostgreSQL evaluates the terms of an ``AND`` in any order, a
``CASE`` branch only when it is reached. A well-formed row - a valid surrogate
pair included - reaches the guarded predicate unchanged.

The engine's JSON serializer keeps new values out
(``core/utils.sanitize_json_strings``); the guard is for the rows written
before it and for any write that goes around the ORM.

A column with an index on ``(col::jsonb)`` cannot hold such a value - the index
refuses the write - and a guard would keep the planner off the index, so
:func:`jsonb_cast_indexed` tells the two apart. :func:`jsonb_has_key` and
:func:`jsonb_field` read a row the guard sets aside the way the operators
would have, from its value decoded in Python.
"""

from __future__ import annotations

from functools import cache
from typing import Any

from sqlalchemy import Column, Text, and_, case, cast, func, or_
from sqlalchemy.sql.elements import ColumnElement

#: A ``\\u0000`` escape or a UTF-16 surrogate escape: what PostgreSQL may refuse to de-escape.
REFUSED_ESCAPE_PATTERN = r"\\u(0000|[dD][89a-fA-F])"
#: A high surrogate escape immediately followed by a low one: a pair PostgreSQL decodes.
SURROGATE_PAIR_PATTERN = r"\\u[dD][89abAB][0-9a-fA-F]{2}\\u[dD][c-fC-F][0-9a-fA-F]{2}"
#: An escaped backslash, which starts no escape.
ESCAPED_BACKSLASH = "\\\\"
#: A number ``jsonb`` may refuse: ``numeric`` holds a weight of up to 131,071
#: digits and a scale of up to 16,383, so only an exponent of five digits or
#: more, or a run of a thousand digits, can take one past them. A superset -
#: a PostgreSQL regex cannot do the arithmetic (repetition stops at 255), and
#: this platform never writes such a number (Python prints a float's exponent
#: in three digits at most, and an integer in 4,300 digits at most). Anchored
#: where a number starts (the document, or after ``:`` ``,`` ``[``), so a hex
#: string such as a commit SHA never reads as one.
OUT_OF_RANGE_NUMBER_PATTERN = (
    r"(^|[:,\[])\s*-?([0-9]+(\.[0-9]+)?[eE][+-]?[0-9]{5}"
    r"|([0-9]+\.)?[0-9]{250}[0-9]{250}[0-9]{250}[0-9]{250})"
)


def json_unparseable(column: Any) -> ColumnElement[bool]:
    """Whether the stored text of *column* may fail a ``jsonb`` cast; NULL for NULL.

    Escapes, exactly: a backslash in valid JSON text belongs to exactly one
    escape and escapes pair up from the left, so once every escaped backslash
    is set aside what is left of a backslash starts a real escape. Set aside as
    ``_``, never dropped: dropping it could join a high and a low surrogate
    escape it separated into a pair. Valid pairs are set aside the same way,
    and the text is refused exactly when a ``\\u0000`` or a surrogate escape is
    left. The cheap search runs first; only a row holding such an escape pays
    the rest.

    Numbers, as a superset (``OUT_OF_RANGE_NUMBER_PATTERN``): a row holding a
    number ``numeric`` refuses always matches, and a row that matches but would
    cast is read as unreadable too - a value no write of this platform makes.
    """
    stored = cast(column, Text)
    real_escapes = func.replace(stored, ESCAPED_BACKSLASH, "_")
    unpaired = func.regexp_replace(real_escapes, SURROGATE_PAIR_PATTERN, "_", "g")
    return or_(
        and_(
            stored.regexp_match(REFUSED_ESCAPE_PATTERN),
            unpaired.regexp_match(REFUSED_ESCAPE_PATTERN),
        ),
        stored.regexp_match(OUT_OF_RANGE_NUMBER_PATTERN),
    )


def when_json_parseable(
    column: Any, condition: ColumnElement[Any], *, otherwise: bool | None = False
) -> ColumnElement[Any]:
    """*condition* on the rows whose *column* PostgreSQL can parse, *otherwise* (``None``: NULL) on the rest."""
    return case((json_unparseable(column), otherwise), else_=condition)


def jsonb_has_key(value: object, key: str) -> bool:
    """PostgreSQL's ``jsonb ? key`` on a decoded value: a top-level key, array element or the string itself."""
    if isinstance(value, dict):
        return key in value
    if isinstance(value, list):
        return key in (item for item in value if isinstance(item, str))
    return value == key


def jsonb_field(value: object, key: str) -> object:
    """PostgreSQL's ``jsonb -> key`` on a decoded value: the member of an object, else nothing."""
    return value.get(key) if isinstance(value, dict) else None


@cache
def _jsonb_cast_indexed(column: Column) -> bool:
    wanted = f"({column.name}::jsonb)"
    return any(
        len(index.expressions) == 1
        and "".join(str(index.expressions[0]).split()).lower() == wanted
        for index in column.table.indexes
    )


def jsonb_cast_indexed(column: Any) -> bool:
    """Whether an index on ``(<column>::jsonb)`` guarantees *column* holds only text PostgreSQL parses.

    PostgreSQL computes the index expression on every write of the row, so a
    value the cast refuses fails the write; a predicate on the cast then
    needs no guard, and keeps the index the guard would hide from the planner.
    """
    return _jsonb_cast_indexed(column.property.columns[0] if hasattr(column, "property") else column)
