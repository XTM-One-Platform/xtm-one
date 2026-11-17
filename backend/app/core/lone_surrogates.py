# SPDX-License-Identifier: Apache-2.0

"""A lone UTF-16 surrogate, refused where it enters an assignment or a flow.

A JSON ``"\\ud800"`` escape decodes to a Python string holding a lone
surrogate. Stored in a PostgreSQL ``json`` column it is an escape every later
``::jsonb`` read refuses, and no JSON response can encode it: an assignment
created with one answered 500 after its commit, and every read of it failed
from then on. The engine now stores such a value as U+FFFD
(:func:`~app.core.utils.sanitize_json_strings`), which keeps the row
readable but changes what the caller sent - for a trigger, what it matches.

So a write of an assignment or flow definition refuses one where it enters,
naming where it is (:func:`lone_surrogate_error`), and the caller fixes what
they sent. A surrogate pair is a character and is accepted as it is: the JSON
decoder joins a ``"\\ud83d\\ude00"`` escape pair into the one character it
encodes, and a high surrogate directly followed by a low one is a pair
PostgreSQL decodes (``LONE_SURROGATE_RE``, the engine's own definition).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.core.utils import LONE_SURROGATE_RE, SURROGATE_RE


@dataclass(frozen=True, slots=True)
class LoneSurrogate:
    """Where a value holds a lone surrogate.

    *path* runs from the value's root (``""``, ``.filter.subject``, ``[2]``);
    *in_key* says the object key at *path* holds it, not the value there.
    """

    path: str
    code_point: int
    in_key: bool


def _lone_code_point(text: str) -> int | None:
    if text.isascii():
        return None
    found = LONE_SURROGATE_RE.search(text)
    return ord(found.group()) if found else None


def _member(key: object) -> str:
    """The path step naming *key*, ASCII whatever the key holds."""
    if isinstance(key, str) and key.isascii() and key.isidentifier():
        return f".{key}"
    if isinstance(key, str):
        return f"[{json.dumps(key)}]"
    return f"[{key!r}]"


def find_lone_surrogate(value: Any) -> LoneSurrogate | None:
    """The first lone surrogate of a JSON-like *value* in document order, else ``None``.

    Strings, object keys included, are searched through dicts, lists and
    tuples; anything else is left alone. Iterative, so a deeply nested value
    costs no recursion.
    """
    stack: list[tuple[Any, str, bool]] = [(value, "", False)]
    while stack:
        node, path, is_key = stack.pop()
        if isinstance(node, str):
            code_point = _lone_code_point(node)
            if code_point is not None:
                return LoneSurrogate(path, code_point, is_key)
        elif isinstance(node, dict):
            members: list[tuple[Any, str, bool]] = []
            for key, item in node.items():
                member_path = path + _member(key)
                members.append((key, member_path, True))
                members.append((item, member_path, False))
            stack.extend(reversed(members))
        elif isinstance(node, list | tuple):
            stack.extend((item, f"{path}[{index}]", False) for index, item in reversed(list(enumerate(node))))
    return None


def lone_surrogate_error(name: str, value: Any) -> str | None:
    """The refusal for a *value* holding a lone surrogate, named *name*, else ``None``.

    ASCII only: the message never carries the surrogate it reports. An empty
    *name* names the value by its inner path alone.
    """
    found = find_lone_surrogate(value)
    if found is None:
        return None
    where = f"{name}{found.path}".lstrip(".") or "the value"
    holder = f"a key of {where}" if found.in_key else where
    return (
        f"{holder} holds a lone UTF-16 surrogate (U+{found.code_point:04X}), half of a "
        "character that cannot be stored on its own. Send the whole character or remove it."
    )


def escape_surrogates(value: Any) -> Any:
    """*value* with every surrogate code point written as its ``\\uXXXX`` escape text.

    For a value echoed back to a client (a 422 names what was sent): a string
    holding a surrogate, paired or not, cannot be encoded as UTF-8, so the
    response failed as a 500. Copy-on-write through dicts, lists and tuples;
    a value with nothing to escape is returned as the same object.
    """
    if isinstance(value, str):
        if value.isascii() or not SURROGATE_RE.search(value):
            return value
        return value.encode("utf-8", "backslashreplace").decode("utf-8")
    if isinstance(value, dict):
        members = [(key, item, escape_surrogates(key), escape_surrogates(item)) for key, item in value.items()]
        if all(new_key is key and new_item is item for key, item, new_key, new_item in members):
            return value
        return {new_key: new_item for _, _, new_key, new_item in members}
    if isinstance(value, list | tuple):
        items = [escape_surrogates(item) for item in value]
        if all(new is old for new, old in zip(items, value, strict=True)):
            return value
        return items if isinstance(value, list) else tuple(items)
    return value
