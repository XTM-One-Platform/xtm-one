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

"""Merge three versions of a JSON object file key by key.

The diff3 line merge (:func:`app.services.sandbox_workspace.rebase.merge_text`)
conflicts when two sides add keys on adjacent lines of the same object, the
usual case of the i18n catalogs: each branch appends its own keys, nobody
touches the other's, and every rebase or merge still stops on markers.
:func:`merge_json_objects` merges such files by key instead:

- a key changed on one side takes that side; changed on both sides to the
  same value, that value once;
- a key added on one side is placed after the key that precedes it on that
  side (after the keys the workspace added there: ours first, then theirs;
  with ``ref_first``, theirs first, then ours: merging a base branch into a
  pull request branch keeps the base's order, so the branch's diff against
  the base holds its own keys only, #1116);
- a key removed on one side and unchanged on the other is removed;
- an object changed on both sides is merged the same way, recursively.

``prefer`` (``"ours"`` or ``"theirs"``) settles a key the two sides changed
differently by taking that side's value (or its removal), every other key of
both sides still merged: the ``keep: branch|ref`` resolution of a JSON file,
whose line merge would take one side's whole tail hunk and drop the keys the
other side appended there.

It answers ``None``, and the caller keeps the line merge's answer, when the
merge is not certain: a side that is not a JSON object, a duplicate key, a
key changed differently on both sides (or removed on one and changed on the
other), or a layout :func:`json.dumps` does not reproduce byte for byte, so
the merged file is written exactly as the files were.
"""

from __future__ import annotations

import json
from typing import Any

#: Bytes of one side the merge parses at most; a larger file keeps the line merge's answer.
MAX_JSON_MERGE_BYTES = 8 * 1024 * 1024


class _KeyConflictError(Exception):
    """A key the two sides changed differently: the key merge does not apply."""


class _DuplicateKeyError(ValueError):
    """An object holding the same key twice: its order cannot be kept."""


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise _DuplicateKeyError(key)
        out[key] = value
    return out


def _parse(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text, object_pairs_hook=_pairs)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


_Layout = tuple[int | str | None, bool, bool]


def _dump(value: dict[str, Any], layout: _Layout) -> str:
    indent, ensure_ascii, newline = layout
    return json.dumps(value, indent=indent, ensure_ascii=ensure_ascii) + ("\n" if newline else "")


def _layout(text: str, value: dict[str, Any]) -> _Layout | None:
    """The :func:`json.dumps` arguments that write *value* back as *text* exactly, else ``None``."""
    newline = text.endswith("\n")
    second = text.split("\n", 2)[1] if text.count("\n") >= 2 else ""
    lead = second[: len(second) - len(second.lstrip(" \t"))]
    indents: list[int | str | None] = [lead if "\t" in lead else len(lead)] if lead else [None, 2]
    for indent in indents:
        for ensure_ascii in (False, True):
            layout = (indent, ensure_ascii, newline)
            if _dump(value, layout) == text:
                return layout
    return None


def _same(left: Any, right: Any) -> bool:
    """Equal as JSON (``1``, ``1.0`` and ``true`` are three values, as in the file)."""
    return json.dumps(left, ensure_ascii=False) == json.dumps(right, ensure_ascii=False)


_ABSENT = object()


def _value(base: Any, ours: Any, theirs: Any, prefer: str | None = None) -> Any:
    """The merged value of one key (``_ABSENT``: the key is dropped).

    A key the sides changed differently raises :class:`_KeyConflictError`, or takes the *prefer* side's value.
    """
    if ours is _ABSENT and theirs is _ABSENT:
        return _ABSENT
    if ours is not _ABSENT and theirs is not _ABSENT:
        if _same(ours, theirs):
            return ours
        if base is not _ABSENT and _same(base, ours):
            return theirs
        if base is not _ABSENT and _same(base, theirs):
            return ours
        inner = {} if base is _ABSENT else base
        if isinstance(inner, dict) and isinstance(ours, dict) and isinstance(theirs, dict):
            return _merge(inner, ours, theirs, prefer)
        return _preferred(ours, theirs, prefer)
    kept = ours if theirs is _ABSENT else theirs
    if base is _ABSENT:
        return kept
    if _same(base, kept):
        return _ABSENT
    return _preferred(ours, theirs, prefer)


def _preferred(ours: Any, theirs: Any, prefer: str | None) -> Any:
    """The *prefer* side of a key changed differently on both sides; raises :class:`_KeyConflictError` without one."""
    if prefer == "ours":
        return ours
    if prefer == "theirs":
        return theirs
    raise _KeyConflictError


def _merge(
    base: dict[str, Any], ours: dict[str, Any], theirs: dict[str, Any], prefer: str | None = None
) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key in (*ours, *(k for k in theirs if k not in ours), *(k for k in base if k not in ours and k not in theirs)):
        value = _value(base.get(key, _ABSENT), ours.get(key, _ABSENT), theirs.get(key, _ABSENT), prefer)
        if value is not _ABSENT:
            values[key] = value
    order = [key for key in ours if key in values]
    placed = set(order)
    previous: str | None = None
    for key in theirs:
        if key in values and key not in placed:
            # After its predecessor on the ref, and after the keys only the workspace
            # added there: both sides appending keeps ours first, then theirs (git's order).
            at = order.index(previous) + 1 if previous is not None else 0
            while at < len(order) and order[at] not in theirs:
                at += 1
            order.insert(at, key)
            placed.add(key)
        if key in placed:
            previous = key
    return {key: values[key] for key in order}


def _conflicting(base: Any, ours: Any, theirs: Any, prefix: str, out: list[str]) -> None:
    keys = [*ours, *(k for k in theirs if k not in ours), *(k for k in base if k not in ours and k not in theirs)]
    for key in keys:
        b, o, t = base.get(key, _ABSENT), ours.get(key, _ABSENT), theirs.get(key, _ABSENT)
        try:
            _value(b, o, t)
        except _KeyConflictError:
            name = f"{prefix}{key}"
            inner = {} if b is _ABSENT else b
            if isinstance(inner, dict) and isinstance(o, dict) and isinstance(t, dict):
                _conflicting(inner, o, t, f"{name}.", out)
            else:
                out.append(name)


def conflicting_keys(base: str, ours: str, theirs: str) -> list[str]:
    """The keys (dotted for a nested one) *ours* and *theirs* changed differently from *base*.

    A key changed to two values, or removed on one side and changed on the other. Empty when a side is
    not a JSON object or holds a key twice (the parse check names that instead).
    """
    if any(len(text) > MAX_JSON_MERGE_BYTES for text in (base, ours, theirs)):
        return []
    try:
        sides = [
            {} if not text.strip() and index == 0 else _parse(text) for index, text in enumerate((base, ours, theirs))
        ]
    except _DuplicateKeyError:
        return []
    base_obj, ours_obj, theirs_obj = sides
    if base_obj is None or ours_obj is None or theirs_obj is None:
        return []
    out: list[str] = []
    _conflicting(base_obj, ours_obj, theirs_obj, "", out)
    return out


def _removed(base: dict[str, Any], ours: dict[str, Any], theirs: dict[str, Any], prefix: str, out: list[str]) -> None:
    for key, value in base.items():
        if key not in theirs:
            continue
        name = f"{prefix}{key}"
        if key not in ours:
            out.append(name)
        elif isinstance(value, dict) and isinstance(ours[key], dict) and isinstance(theirs[key], dict):
            _removed(value, ours[key], theirs[key], f"{name}.", out)


def removed_keys(base: str, ours: str, theirs: str) -> list[str]:
    """The keys (dotted for a nested one) *base* and *theirs* hold that *ours* removed.

    A three-way merge removes them without a conflict, as it should when the removal was meant; a branch that
    wrote a stale copy of the file removes them too, so a merge answer names them for a check (#1095). Empty when a
    side is not a JSON object or holds a key twice.
    """
    if any(len(text) > MAX_JSON_MERGE_BYTES for text in (base, ours, theirs)) or not base.strip():
        return []
    try:
        sides = [_parse(text) for text in (base, ours, theirs)]
    except _DuplicateKeyError:
        return []
    base_obj, ours_obj, theirs_obj = sides
    if base_obj is None or ours_obj is None or theirs_obj is None:
        return []
    out: list[str] = []
    _removed(base_obj, ours_obj, theirs_obj, "", out)
    return out


def _dropped(sides: list[dict[str, Any]], resolved: dict[str, Any], prefix: str, out: list[str]) -> None:
    for key in sides[0]:
        if not all(key in side for side in sides[1:]):
            continue
        name = f"{prefix}{key}"
        if key not in resolved:
            out.append(name)
        elif isinstance(resolved[key], dict) and all(isinstance(side[key], dict) for side in sides):
            _dropped([side[key] for side in sides], resolved[key], f"{name}.", out)


def dropped_keys(resolved: str, *sides: str) -> list[str]:
    """The keys (dotted for a nested one) every one of *sides* holds that *resolved* does not.

    A merge of two sides never removes a key both of them hold: a resolution written by hand from a stale
    copy of a catalog does, silently (#1311: a merge of main lost 18 catalog keys that way). Empty when a text is not a
    JSON object, holds a key twice or is over the size the merge parses.
    """
    texts = (resolved, *sides)
    if not sides or any(len(text) > MAX_JSON_MERGE_BYTES for text in texts):
        return []
    try:
        parsed = [_parse(text) for text in texts]
    except _DuplicateKeyError:
        return []
    if any(value is None for value in parsed):
        return []
    out: list[str] = []
    _dropped([value or {} for value in parsed[1:]], parsed[0] or {}, "", out)
    return out


def merge_json_objects(
    base: str, ours: str, theirs: str, prefer: str | None = None, *, ref_first: bool = False
) -> str | None:
    """*ours* and *theirs* merged from *base* key by key, written in their layout; ``None`` when not certain.

    *base* empty is a file added on both sides (an empty object as base). *prefer* (``"ours"`` or ``"theirs"``)
    takes that side's value for a key changed differently on both sides instead of answering ``None``.
    *ref_first* orders the keys by *theirs* (the ref merged in), *ours*'s own keys placed after their
    predecessor there: the values are the same either way, only the order of keys both sides added differs.
    """
    texts = (base, ours, theirs)
    if any(len(text) > MAX_JSON_MERGE_BYTES for text in texts):
        return None
    if not ours.lstrip().startswith("{") or not theirs.lstrip().startswith("{"):
        return None
    try:
        sides = [{} if not text.strip() and index == 0 else _parse(text) for index, text in enumerate(texts)]
    except _DuplicateKeyError:
        return None
    base_obj, ours_obj, theirs_obj = sides
    if base_obj is None or ours_obj is None or theirs_obj is None:
        return None
    layout = _layout(ours, ours_obj)
    if layout is None or _layout(theirs, theirs_obj) != layout:
        return None
    if base.strip() and _layout(base, base_obj) != layout:
        return None
    try:
        if ref_first:
            # The same merge with the sides swapped: theirs gives the order, the preferred side stays the same.
            swapped = {"ours": "theirs", "theirs": "ours"}.get(prefer or "")
            merged = _merge(base_obj, theirs_obj, ours_obj, swapped)
        else:
            merged = _merge(base_obj, ours_obj, theirs_obj, prefer)
    except _KeyConflictError:
        return None
    return _dump(merged, layout)
