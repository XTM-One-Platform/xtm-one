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

"""Edit the entries of a gettext ``.po`` catalog in place, keyed by their msgid.

Pure: no I/O. :mod:`jsonkeys` calls it for a ``.po`` / ``.pot`` file (the Lingui
catalogs ``front/src/locales/<locale>/messages.po``, half a megabyte each), so
``sandbox_json_keys`` adds, changes, renames, removes and reads messages the way it
does for JSON, without a script rewriting the whole file (#1284).

An entry is a block of lines between blank lines: its comments (``#``, ``#.``,
``#:``, ``#,``, ``#|``), an optional ``msgctxt``, the ``msgid``, an optional
``msgid_plural`` and its ``msgstr`` (or ``msgstr[N]``), each string continued on
``"..."`` lines. Its key is the msgid, prefixed by the msgctxt and U+0004 (the
gettext convention) when it has a context. Only the lines of the entries named
change: every other byte (the header, comments, obsolete ``#~`` entries, wrapping
and separators) is kept. A new entry is written after its anchor (or last) as
``msgid "..."`` / ``msgstr "..."``, one blank line apart. A plural entry is read
(its forms as an object), renamed or removed, never set. CRLF line endings or a
line that is not PO is refused. A msgid present twice (Lingui writes one entry
per source with an explicit id, ``#. js-lingui-explicit-id``, next to the same
msgid from a plain ``t``) leaves the file editable: only a call that names that
msgid is refused, with the lines of each copy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: File extensions read as a gettext catalog.
PO_EXTENSIONS = (".po", ".pot")
#: Between the msgctxt and the msgid in the key of an entry with a context.
CONTEXT_SEPARATOR = "\x04"

_KEYWORD = re.compile(r'(msgctxt|msgid_plural|msgid|msgstr(?:\[\d+\])?)[ \t]+"((?:[^"\\]|\\.)*)"[ \t]*')
_CONTINUATION = re.compile(r'[ \t]*"((?:[^"\\]|\\.)*)"[ \t]*')
_SEPARATOR = re.compile(r"\n(?:[ \t]*\n)+")
_DECODE = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "a": "\a", "b": "\b", "f": "\f", "v": "\v"}


class PoCatalogError(ValueError):
    """The file cannot be edited as a gettext catalog: nothing is written."""


def is_po_catalog(path: str) -> bool:
    """True for a file :mod:`jsonkeys` hands to this module (by extension)."""
    return path.lower().endswith(PO_EXTENSIONS)


def decode_string(body: str) -> str:
    """The value of the inside of a PO string (no quotes)."""
    out: list[str] = []
    i = 0
    while i < len(body):
        if body[i] == "\\" and i + 1 < len(body):
            out.append(_DECODE.get(body[i + 1], body[i + 1]))
            i += 2
        else:
            out.append(body[i])
            i += 1
    return "".join(out)


def encode_string(value: str) -> str:
    """*value* as a quoted PO string; typographic characters are kept as they are."""
    escaped = (
        value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
    )
    return f'"{escaped}"'


def _keyword_lines(keyword: str, value: str) -> list[str]:
    """*keyword* and *value* as PO lines: one line, or ``""`` then one line per line of a multi-line value."""
    parts = value.split("\n")
    if len(parts) == 1 or (len(parts) == 2 and not parts[1]):
        return [f"{keyword} {encode_string(value)}"]
    segments = [p + "\n" for p in parts[:-1]] + ([parts[-1]] if parts[-1] else [])
    return [f'{keyword} ""', *(encode_string(s) for s in segments)]


def split_key(key: str) -> tuple[str | None, str]:
    """``(msgctxt or None, msgid)`` of a key."""
    if CONTEXT_SEPARATOR in key:
        context, msgid = key.split(CONTEXT_SEPARATOR, 1)
        return context, msgid
    return None, key


@dataclass
class _Block:
    sep: str  # the blank lines before the block ("" for the first)
    lines: list[str]
    first_line: int  # 1-based line number of its first line, for errors
    key: str | None = None  # None: the header, a comment-only or an obsolete block
    plural: bool = False
    fields: dict[str, list[int]] = field(default_factory=dict)  # keyword -> [first line, last line + 1]
    strings: dict[str, list[str]] = field(default_factory=dict)  # keyword -> its raw string pieces

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def parse(self) -> None:
        self.key, self.plural, self.fields, self.strings = None, False, {}, {}
        if any(line.startswith("#~") for line in self.lines):
            return
        current: str | None = None
        for n, line in enumerate(self.lines):
            where = f"line {self.first_line + n}"
            if line.startswith("#"):
                if current is not None:
                    raise PoCatalogError(f"{where}: a comment after the message strings.")
                continue
            keyword = _KEYWORD.fullmatch(line)
            if keyword:
                current = keyword.group(1)
                if current in self.fields:
                    raise PoCatalogError(f"{where}: {current} twice in one entry.")
                self.fields[current] = [n, n + 1]
                self.strings[current] = [keyword.group(2)]
                continue
            continuation = _CONTINUATION.fullmatch(line)
            if continuation and current is not None:
                self.fields[current][1] = n + 1
                self.strings[current].append(continuation.group(1))
                continue
            raise PoCatalogError(f"{where}: {line[:60]!r} is not a PO line.")
        if not self.fields:
            return
        if "msgid" not in self.fields or not any(k.startswith("msgstr") for k in self.fields):
            raise PoCatalogError(f"line {self.first_line}: an entry needs a msgid and a msgstr.")
        msgid = self.string("msgid")
        context = self.string("msgctxt") if "msgctxt" in self.fields else None
        if context is None and msgid == "":
            return  # the header
        self.key = msgid if context is None else context + CONTEXT_SEPARATOR + msgid
        self.plural = "msgid_plural" in self.fields

    def string(self, keyword: str) -> str:
        return decode_string("".join(self.strings[keyword]))

    def replace(self, keyword: str, value: str) -> None:
        start, end = self.fields[keyword]
        self.lines[start:end] = _keyword_lines(keyword, value)
        self.parse()


class PoCatalog:
    """A ``.po`` file as an ordered set of entries keyed by msgid, edited block by block."""

    def __init__(self, text: str) -> None:
        if "\r" in text:
            raise PoCatalogError("uses CRLF line endings; only LF catalogs are edited.")
        stripped = text.lstrip("\n")
        self.prefix = text[: len(text) - len(stripped)]
        body = stripped.rstrip(" \t\n")
        self.suffix = stripped[len(body) :]
        if not body:
            raise PoCatalogError("holds no entry.")
        self.blocks: list[_Block] = []
        sep, pos = "", 0
        for match in [*_SEPARATOR.finditer(body), None]:
            end = match.start() if match else len(body)
            line = len(self.prefix) + body.count("\n", 0, pos) + 1
            block = _Block(sep=sep, lines=body[pos:end].split("\n"), first_line=line)
            block.parse()
            self.blocks.append(block)
            if match:
                sep, pos = match.group(0)[1:], match.end()
        lines: dict[str, list[int]] = {}
        for block in self.blocks:
            if block.key is not None:
                lines.setdefault(block.key, []).append(block.first_line)
        #: key -> first line of each copy, for the keys present more than once.
        self.duplicates: dict[str, list[int]] = {key: found for key, found in lines.items() if len(found) > 1}

    # The dict-like surface :mod:`jsonkeys` drives (the one of tscatalog.ScriptCatalog).

    def keys(self) -> list[str]:
        return list(dict.fromkeys(b.key for b in self.blocks if b.key is not None))

    def __contains__(self, key: object) -> bool:
        return any(b.key == key for b in self.blocks)

    def _index(self, key: str) -> int:
        if key in self.duplicates:
            where = ", ".join(str(n) for n in self.duplicates[key])
            raise PoCatalogError(
                f"holds the msgid {key!r} twice (lines {where}): edit that msgid by hand or remove a copy first."
            )
        return next(i for i, b in enumerate(self.blocks) if b.key == key)

    def value(self, key: str) -> str | dict[str, str]:
        block = self.blocks[self._index(key)]
        if block.plural:
            return {k: block.string(k) for k in block.fields if k.startswith("msgstr")}
        return block.string("msgstr")

    def set_value(self, key: str, value: str) -> None:
        block = self.blocks[self._index(key)]
        if block.plural or "msgstr" not in block.fields:
            raise PoCatalogError(f"{key!r} is a plural entry: its msgstr[N] forms are not set by this tool.")
        block.replace("msgstr", value)

    def rename(self, old: str, new: str) -> None:
        (old_context, _), (new_context, msgid) = split_key(old), split_key(new)
        if old_context != new_context:
            raise PoCatalogError(f"renaming {old!r} to {new!r} changes its msgctxt; remove and set instead.")
        block = self.blocks[self._index(old)]
        block.replace("msgid", msgid)

    def add(self, key: str, value: str, side: str | None = None, anchor: str | None = None) -> None:
        context, msgid = split_key(key)
        lines = [*(_keyword_lines("msgctxt", context) if context is not None else []), *_keyword_lines("msgid", msgid)]
        block = _Block(sep="\n", lines=[*lines, *_keyword_lines("msgstr", value)], first_line=0)
        block.parse()
        if anchor is None:
            self.blocks.append(block)
            return
        index = self._index(anchor) + (1 if side == "after" else 0)
        if index == 0:
            block.sep, self.blocks[0].sep = "", "\n"
        self.blocks.insert(index, block)

    def remove(self, key: str) -> None:
        index = self._index(key)
        self.blocks.pop(index)
        if index == 0 and self.blocks:
            self.blocks[0].sep = ""

    def render(self) -> str:
        return (
            self.prefix + "".join(("\n" + b.sep if n else "") + b.text for n, b in enumerate(self.blocks)) + self.suffix
        )
