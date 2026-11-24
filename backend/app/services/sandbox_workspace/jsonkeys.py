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

"""Add, change, rename, remove and read the top-level keys of JSON object files.

Pure: no I/O. ``sandbox_json_keys`` (``builtin_tools/sandbox_workspace.py``)
reads the files, calls :func:`change_keys` and writes what it answers in one
workspace commit. The case it serves is the i18n catalogs: four files of more
than a megabyte, one key added, renamed or dropped in each, where a find/replace
needs a unique anchor per file and a script rewrites the whole file.

Each file is written back in its own layout (indent, ASCII escaping, final
newline, as :mod:`jsonmerge` detects it); a file whose layout
:func:`json.dumps` does not reproduce byte for byte, a duplicate key or a value
that is not an object is refused, and nothing is written for any file.

A value set in a French catalog (a file named ``fr.json``, ``fr-FR.json``,
``fr_CA.json``...) is checked for French typography: a plain space before a
high punctuation mark that ends a word is reported per key, or replaced by the
no-break space the mark takes when the caller asks for it (``french_spacing``).

A TypeScript or JavaScript catalog (``.ts``, ``.js``... holding one object
literal, such as ``export const fr: Catalog = {...}``) is edited in place by
:mod:`tscatalog`: only the lines of the keys named change, and the values set
there are strings.

A gettext catalog (``.po`` / ``.pot``) is edited in place by :mod:`pocatalog`,
keyed by msgid (``msgctxt`` + U+0004 + msgid for an entry with a context): only
the entries named change, the values set are strings, a plural entry is read
and never set (#1284). ``fr.po`` and any ``.po`` under a ``fr`` locale
directory (``locales/fr/messages.po``) get the French typography check.

In a JSON file, a key of ``set``, ``remove`` or ``get`` that starts with ``/``
and is not itself a top-level key of the file is a JSON Pointer (RFC 6901) to a
nested value: ``/a/b`` is the member ``b`` of the object under ``a``, ``/list/0``
the first item of an array, ``/list/-`` (set only) a new last item. Every
parent must exist; ``place`` positions a new member among its siblings. This is
the nested edit a config or fixture file needs, which agents otherwise did
through a code_interpreter rewrite (#11483).
"""

from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass, field
from typing import Any

from app.services.sandbox_workspace import pocatalog, tscatalog
from app.services.sandbox_workspace.jsonmerge import MAX_JSON_MERGE_BYTES, _dump, _DuplicateKeyError, _layout, _pairs

#: Files one call changes at most.
MAX_FILES = 20
#: Keys one call names at most for one file (its own set, the ``*`` set, rename,
#: remove and get together), so a call over several catalogs is not refused
#: for their sum.
MAX_KEYS = 500
#: Characters of one value ``get`` shows before it is shortened.
GET_VALUE_CHARS = 300
#: What ``french_spacing`` accepts: report the plain spaces (default) or fix them.
FRENCH_SPACING_MODES = ("report", "fix")

#: A plain space between a word and a high punctuation mark that ends it: the
#: mark follows a letter, a digit, a currency sign, a closing quote, bracket or
#: ellipsis, and is followed by the end, a space, a closing brace, quote or
#: bracket, so "::1" or "otpauth:// ;" are left alone. The same reading as the
#: catalog gates that enforce French typography (U+202F before "?", "!", ";",
#: U+00A0 before ":").
_PLAIN_SPACE_BEFORE_MARK = re.compile(
    r"(?:(?<=[^\W_])|(?<=[$\u20ac%\u00bb\"'\u2019)\]}\u2026*.])) ([?!;:])(?=\Z|[\s}\"'\u00bb)\]])"
)
#: A French locale file name: fr.json, fr-FR.json, fr_CA.json.
_FRENCH_FILE = re.compile(r"fr(?:[-_][A-Za-z]{2,4})?\.(?:json|po)", re.IGNORECASE)
#: A French locale directory holding a gettext catalog: locales/fr/messages.po.
_FRENCH_DIR = re.compile(r"fr(?:[-_][A-Za-z]{2,4})?", re.IGNORECASE)


class JsonKeysError(ValueError):
    """The request cannot apply: nothing is written."""


@dataclass
class FileChange:
    path: str
    text: str
    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    renamed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    absent: list[str] = field(default_factory=list)
    values: dict[str, Any] = field(default_factory=dict)
    #: Keys set with a plain space before a French high punctuation mark: ``(key, marks)``.
    spacing: list[tuple[str, str]] = field(default_factory=list)
    #: True when those spaces were replaced by the no-break space each mark takes.
    spacing_fixed: bool = False

    @property
    def modified(self) -> bool:
        return bool(self.added or self.changed or self.renamed or self.removed)

    def line(self) -> str:
        parts = [
            f"{label} {len(keys)} ({_names(keys)})"
            for label, keys in (
                ("added", self.added),
                ("changed", self.changed),
                ("renamed", self.renamed),
                ("removed", self.removed),
                ("absent", self.absent),
            )
            if keys
        ]
        return f"{self.path}: " + ("; ".join(parts) if parts else "unchanged")

    def spacing_line(self) -> str:
        """The French spacing found in the values set, or empty when there is none."""
        if not self.spacing:
            return ""
        keys = ", ".join(f"{json.dumps(k, ensure_ascii=False)} before {m}" for k, m in self.spacing[:5])
        more = f", +{len(self.spacing) - 5} more" if len(self.spacing) > 5 else ""
        if self.spacing_fixed:
            return f"  French spacing fixed (U+202F before ? ! ;, U+00A0 before :): {keys}{more}"
        return (
            f"  WARNING French spacing: a plain space before a high punctuation mark in {keys}{more}; "
            'the catalog gate refuses it. Pass french_spacing="fix" to write the no-break space it takes.'
        )


def is_french_catalog(path: str) -> bool:
    """True for a French locale file (``fr.json``, ``fr-FR.json``, ``fr.po``, ``fr/messages.po``)."""
    if _FRENCH_FILE.fullmatch(posixpath.basename(path)):
        return True
    return pocatalog.is_po_catalog(path) and bool(_FRENCH_DIR.fullmatch(posixpath.basename(posixpath.dirname(path))))


def _no_break_space_for(mark: str) -> str:
    return "\u00a0" if mark == ":" else "\u202f"


def french_spacing_marks(text: str) -> str:
    """The marks of *text* preceded by a plain space, in order (``""`` when none)."""
    return "".join(m.group(1) for m in _PLAIN_SPACE_BEFORE_MARK.finditer(text))


def fix_french_spacing(text: str) -> str:
    """*text* with each plain space before a mark replaced by the no-break space it takes."""
    return _PLAIN_SPACE_BEFORE_MARK.sub(lambda m: _no_break_space_for(m.group(1)) + m.group(1), text)


def _spaced(value: Any, fix: bool) -> tuple[Any, str]:
    """*value* (fixed when *fix*) and the marks found in its strings, nested ones included."""
    if isinstance(value, str):
        marks = french_spacing_marks(value)
        return (fix_french_spacing(value) if fix and marks else value), marks
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        found = ""
        for k, v in value.items():
            out[k], marks = _spaced(v, fix)
            found += marks
        return out, found
    if isinstance(value, list):
        pairs = [_spaced(v, fix) for v in value]
        return [v for v, _ in pairs], "".join(m for _, m in pairs)
    return value, ""


def _names(keys: list[str], shown: int = 5) -> str:
    head = ", ".join(json.dumps(k, ensure_ascii=False) for k in keys[:shown])
    return head + (f", +{len(keys) - shown} more" if len(keys) > shown else "")


def _shown(value: Any) -> str:
    text = str(value) if isinstance(value, tscatalog.Expression) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= GET_VALUE_CHARS else text[:GET_VALUE_CHARS] + f"... [{len(text)} chars]"


def _mapping(name: str, value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise JsonKeysError(f"{name} must be an object.")
    return value


def _keys(name: str, value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(k, str) for k in value):
        raise JsonKeysError(f"{name} must be a list of keys.")
    return value


def _placements(value: Any, sets: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """*place* checked: new key to ``("after" | "before", anchor key)``."""
    places = _mapping("place", value)
    set_names = {key for values in sets.values() if isinstance(values, dict) for key in values}
    out: dict[str, tuple[str, str]] = {}
    for key, spec in places.items():
        if key not in set_names:
            raise JsonKeysError(f"place names {json.dumps(key, ensure_ascii=False)}, which set does not give.")
        if not isinstance(spec, dict) or len(spec) != 1 or next(iter(spec)) not in ("after", "before"):
            raise JsonKeysError(f'place[{key}] must be {{"after": key}} or {{"before": key}}.')
        side, anchor = next(iter(spec.items()))
        if not isinstance(anchor, str) or not anchor or anchor == key:
            raise JsonKeysError(f"place[{key}].{side} must be another key (a non-empty string).")
        out[key] = (side, anchor)
    return out


def is_pointer(key: str, value: dict[str, Any]) -> bool:
    """True when *key* reads as a JSON Pointer in *value*: it starts with ``/`` and is no top-level key of it."""
    return key.startswith("/") and key not in value


def _tokens(pointer: str) -> list[str]:
    return [t.replace("~1", "/").replace("~0", "~") for t in pointer[1:].split("/")]


def _index(container: list[Any], token: str, pointer: str, *, append: bool = False) -> int:
    """The array index *token* names in *container* (``-`` or the length when *append*)."""
    if append and token in ("-", str(len(container))):
        return len(container)
    if not token.isdigit() or (len(token) > 1 and token.startswith("0")) or int(token) >= len(container):
        raise JsonKeysError(f"{pointer}: {json.dumps(token)} is not an item of an array of {len(container)}.")
    return int(token)


def _parent(value: Any, pointer: str) -> tuple[Any, str]:
    """The container that holds the last token of *pointer*, and that token; every parent must exist."""
    tokens = _tokens(pointer)
    node = value
    for depth, token in enumerate(tokens[:-1]):
        where = "/" + "/".join(tokens[: depth + 1])
        if isinstance(node, dict):
            if token not in node:
                raise JsonKeysError(f"{pointer}: {where} does not exist (a parent is never created).")
            node = node[token]
        elif isinstance(node, list):
            node = node[_index(node, token, pointer)]
        else:
            raise JsonKeysError(f"{pointer}: {where} is not an object or an array.")
    if not isinstance(node, (dict, list)):
        raise JsonKeysError(f"{pointer}: its parent is not an object or an array.")
    return node, tokens[-1]


def _read_pointer(value: Any, pointer: str) -> Any:
    """The value at *pointer*, or ``_MISSING``."""
    try:
        parent, token = _parent(value, pointer)
        if isinstance(parent, dict):
            return parent.get(token, _MISSING)
        return parent[_index(parent, token, pointer)]
    except JsonKeysError:
        return _MISSING


def _set_pointer(
    value: dict[str, Any], pointer: str, new_value: Any, place: tuple[str, str] | None, change: FileChange
) -> None:
    """Set the nested value at *pointer* in place, recorded on *change* as added or changed."""
    parent, token = _parent(value, pointer)
    if isinstance(parent, list):
        at = _index(parent, token, pointer, append=True)
        if at == len(parent):
            parent.append(new_value)
            change.added.append(pointer)
        elif json.dumps(parent[at], ensure_ascii=False) != json.dumps(new_value, ensure_ascii=False):
            parent[at] = new_value
            change.changed.append(pointer)
        return
    if token in parent:
        if json.dumps(parent[token], ensure_ascii=False) != json.dumps(new_value, ensure_ascii=False):
            change.changed.append(pointer)
        parent[token] = new_value
        return
    change.added.append(pointer)
    if place is None:
        parent[token] = new_value
        return
    side, anchor = place
    if anchor not in parent:
        raise JsonKeysError(
            f"{pointer}: its object has no member {json.dumps(anchor, ensure_ascii=False)} to place it {side}."
        )
    rebuilt = _inserted(parent, token, new_value, side, anchor)
    parent.clear()
    parent.update(rebuilt)


def _remove_pointer(value: dict[str, Any], pointer: str, change: FileChange) -> None:
    """Drop the nested value at *pointer*; reported absent when it is not there."""
    try:
        parent, token = _parent(value, pointer)
        if isinstance(parent, list):
            del parent[_index(parent, token, pointer)]
        elif token in parent:
            del parent[token]
        else:
            change.absent.append(pointer)
            return
    except JsonKeysError:
        change.absent.append(pointer)
        return
    change.removed.append(pointer)


def _inserted(value: dict[str, Any], key: str, new_value: Any, side: str, anchor: str) -> dict[str, Any]:
    """*value* with *key* added right after or before *anchor*."""
    out: dict[str, Any] = {}
    for k, v in value.items():
        if k == anchor and side == "before":
            out[key] = new_value
        out[k] = v
        if k == anchor and side == "after":
            out[key] = new_value
    return out


def change_keys(
    texts: dict[str, str],
    *,
    set_keys: dict[str, Any] | None = None,
    rename: dict[str, Any] | None = None,
    remove: list[str] | None = None,
    get: list[str] | None = None,
    place: dict[str, Any] | None = None,
    french_spacing: str | None = None,
) -> list[FileChange]:
    """Every file of *texts* (path to text) with the changes applied, in the order given.

    *set_keys* maps a path (``"*"``: every file) to the keys and values to set:
    an existing key keeps its place, a new one goes last unless *place* maps it
    to ``{"after": key}`` or ``{"before": key}`` (the anchor must be in the file
    by then, else the call is refused). *rename* maps an old key to a new one,
    in place, in every file; a set of the new key in the same call gives it a
    new value per file, still in place. *remove* drops keys from every
    file (one a file lacks is reported absent). *get* reads keys (after the
    changes). Order of application: rename, set, remove. A value set in a
    French catalog (:func:`is_french_catalog`) with a plain space before a high
    punctuation mark is listed in ``spacing``; *french_spacing* ``"fix"``
    writes the no-break space the mark takes instead. Raises
    :class:`JsonKeysError` when anything cannot apply.
    """
    if french_spacing not in (None, *FRENCH_SPACING_MODES):
        raise JsonKeysError('french_spacing must be "report" or "fix".')
    fix_spacing = french_spacing == "fix"
    sets = _mapping("set", set_keys)
    renames = _mapping("rename", rename)
    removes = _keys("remove", remove)
    gets = _keys("get", get)
    places = _placements(place, sets)
    if not texts:
        raise JsonKeysError("name at least one file in paths.")
    if len(texts) > MAX_FILES:
        raise JsonKeysError(f"{len(texts)} files named, at most {MAX_FILES} per call.")
    if not (sets or renames or removes or gets):
        raise JsonKeysError("nothing to do: give set, rename, remove or get.")
    unknown = sorted(p for p in sets if p != "*" and p not in texts)
    if unknown:
        raise JsonKeysError(f"set names {', '.join(unknown)}, not in paths.")
    for path, values in sets.items():
        if not isinstance(values, dict):
            raise JsonKeysError(f"set[{path}] must be an object of keys and values.")
    for old, new in renames.items():
        if not isinstance(new, str) or not new:
            raise JsonKeysError(f"rename[{old}] must be the new key (a non-empty string).")
    if len(set(renames.values())) != len(renames):
        raise JsonKeysError("rename gives two keys the same new name.")
    shared = len(sets.get("*", {})) + len(renames) + len(removes) + len(gets)
    heaviest = max(texts, key=lambda p: len(sets.get(p, {})))
    named = shared + len(sets.get(heaviest, {}))
    if named > MAX_KEYS:
        raise JsonKeysError(
            f"{named} keys named for {heaviest}, at most {MAX_KEYS} per file: "
            "split that file's keys over several calls. Nothing was written."
        )

    out: list[FileChange] = []
    for path, text in texts.items():
        if len(text) > MAX_JSON_MERGE_BYTES:
            raise JsonKeysError(f"{path} is over {MAX_JSON_MERGE_BYTES // (1024 * 1024)} MB.")
        if tscatalog.is_script_catalog(path):
            out.append(_change_script(path, text, sets, renames, removes, gets, places))
            continue
        if pocatalog.is_po_catalog(path):
            out.append(_change_po(path, text, sets, renames, removes, gets, places, fix_spacing))
            continue
        try:
            value = json.loads(text, object_pairs_hook=_pairs)
        except _DuplicateKeyError as exc:
            raise JsonKeysError(f"{path} holds the key {exc} twice.") from None
        except (ValueError, RecursionError):
            value = None
        if not isinstance(value, dict):
            raise JsonKeysError(f"{path} is not a JSON object.")
        layout = _layout(text, value)
        if layout is None:
            raise JsonKeysError(
                f"{path}: its layout cannot be written back byte for byte (indent or escaping is mixed)."
            )
        change = FileChange(path=path, text=text)
        french = is_french_catalog(path)
        change.spacing_fixed = french and fix_spacing
        for old, new in renames.items():
            if old not in value:
                raise JsonKeysError(f"{path} has no key {json.dumps(old, ensure_ascii=False)} to rename.")
            if new in value and new != old:
                raise JsonKeysError(f"{path} already has the key {json.dumps(new, ensure_ascii=False)}.")
            if new != old:
                value = {(new if k == old else k): v for k, v in value.items()}
                change.renamed.append(f"{old} -> {new}")
        for values in (sets.get("*", {}), sets.get(path, {})):
            for key, new_value in values.items():
                if french:
                    new_value, marks = _spaced(new_value, fix_spacing)
                    if marks:
                        change.spacing.append((key, " ".join(dict.fromkeys(marks))))
                if is_pointer(key, value):
                    _set_pointer(value, key, new_value, places.get(key), change)
                    continue
                if key not in value:
                    change.added.append(key)
                    if key in places:
                        side, anchor = places[key]
                        if anchor not in value:
                            raise JsonKeysError(
                                f"{path} has no key {json.dumps(anchor, ensure_ascii=False)} to place "
                                f"{json.dumps(key, ensure_ascii=False)} {side}."
                            )
                        value = _inserted(value, key, new_value, side, anchor)
                        continue
                elif json.dumps(value[key], ensure_ascii=False) != json.dumps(new_value, ensure_ascii=False):
                    change.changed.append(key)
                value[key] = new_value
        for key in removes:
            if is_pointer(key, value):
                _remove_pointer(value, key, change)
            elif key in value:
                del value[key]
                change.removed.append(key)
            else:
                change.absent.append(key)
        for key in gets:
            change.values[key] = _read_pointer(value, key) if is_pointer(key, value) else value.get(key, _MISSING)
        if change.modified:
            change.text = _dump(value, layout)
        out.append(change)
    return out


def _change_script(
    path: str,
    text: str,
    sets: dict[str, Any],
    renames: dict[str, Any],
    removes: list[str],
    gets: list[str],
    places: dict[str, tuple[str, str]],
) -> FileChange:
    """The :func:`change_keys` steps on a TypeScript or JavaScript catalog, edited line by line."""
    try:
        catalog = tscatalog.ScriptCatalog(text)
    except tscatalog.ScriptCatalogError as exc:
        raise JsonKeysError(f"{path} {exc}") from None
    except (ValueError, IndexError):
        raise JsonKeysError(f"{path} holds a string escape that cannot be read.") from None
    change = FileChange(path=path, text=text)
    for old, new in renames.items():
        if old not in catalog:
            raise JsonKeysError(f"{path} has no key {json.dumps(old, ensure_ascii=False)} to rename.")
        if new in catalog and new != old:
            raise JsonKeysError(f"{path} already has the key {json.dumps(new, ensure_ascii=False)}.")
        if new != old:
            catalog.rename(old, new)
            change.renamed.append(f"{old} -> {new}")
    for values in (sets.get("*", {}), sets.get(path, {})):
        for key, new_value in values.items():
            if not isinstance(new_value, str):
                raise JsonKeysError(
                    f"{path} is a script catalog: the value of {json.dumps(key, ensure_ascii=False)} must be a string."
                )
            if key not in catalog:
                change.added.append(key)
                side, anchor = places.get(key, (None, None))
                if anchor is not None and anchor not in catalog:
                    raise JsonKeysError(
                        f"{path} has no key {json.dumps(anchor, ensure_ascii=False)} to place "
                        f"{json.dumps(key, ensure_ascii=False)} {side}."
                    )
                catalog.add(key, new_value, side, anchor)
            elif catalog.value(key) != new_value:
                change.changed.append(key)
                catalog.set_value(key, new_value)
    for key in removes:
        if key in catalog:
            catalog.remove(key)
            change.removed.append(key)
        else:
            change.absent.append(key)
    for key in gets:
        change.values[key] = catalog.value(key) if key in catalog else _MISSING
    if change.modified:
        change.text = catalog.render()
    return change


def _change_po(
    path: str,
    text: str,
    sets: dict[str, Any],
    renames: dict[str, Any],
    removes: list[str],
    gets: list[str],
    places: dict[str, tuple[str, str]],
    fix_spacing: bool,
) -> FileChange:
    """The :func:`change_keys` steps on a gettext catalog, edited entry by entry."""
    try:
        catalog = pocatalog.PoCatalog(text)
    except pocatalog.PoCatalogError as exc:
        raise JsonKeysError(f"{path} {exc}") from None
    change = FileChange(path=path, text=text)
    french = is_french_catalog(path)
    change.spacing_fixed = french and fix_spacing
    try:
        for old, new in renames.items():
            if old not in catalog:
                raise JsonKeysError(f"{path} has no msgid {json.dumps(old, ensure_ascii=False)} to rename.")
            if new in catalog and new != old:
                raise JsonKeysError(f"{path} already has the msgid {json.dumps(new, ensure_ascii=False)}.")
            if new != old:
                catalog.rename(old, new)
                change.renamed.append(f"{old} -> {new}")
        for values in (sets.get("*", {}), sets.get(path, {})):
            for key, new_value in values.items():
                if not isinstance(new_value, str):
                    raise JsonKeysError(
                        f"{path} is a gettext catalog: the msgstr of {json.dumps(key, ensure_ascii=False)} "
                        "must be a string."
                    )
                if french:
                    new_value, marks = _spaced(new_value, fix_spacing)
                    if marks:
                        change.spacing.append((key, " ".join(dict.fromkeys(marks))))
                if key not in catalog:
                    change.added.append(key)
                    side, anchor = places.get(key, (None, None))
                    if anchor is not None and anchor not in catalog:
                        raise JsonKeysError(
                            f"{path} has no msgid {json.dumps(anchor, ensure_ascii=False)} to place "
                            f"{json.dumps(key, ensure_ascii=False)} {side}."
                        )
                    catalog.add(key, new_value, side, anchor)
                elif catalog.value(key) != new_value:
                    catalog.set_value(key, new_value)
                    change.changed.append(key)
        for key in removes:
            if key in catalog:
                catalog.remove(key)
                change.removed.append(key)
            else:
                change.absent.append(key)
        for key in gets:
            change.values[key] = catalog.value(key) if key in catalog else _MISSING
    except pocatalog.PoCatalogError as exc:
        raise JsonKeysError(f"{path}: {exc}") from None
    if change.modified:
        change.text = catalog.render()
    return change


class _Missing:
    def __repr__(self) -> str:
        return "(absent)"


_MISSING = _Missing()


def describe_values(change: FileChange) -> list[str]:
    """One line per key ``get`` read in *change*."""
    return [
        f"  {json.dumps(key, ensure_ascii=False)}: " + ("(absent)" if value is _MISSING else _shown(value))
        for key, value in change.values.items()
    ]
