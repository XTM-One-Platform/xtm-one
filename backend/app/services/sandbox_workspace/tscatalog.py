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

"""Edit the top-level keys of a TypeScript or JavaScript object-literal catalog in place.

Pure: no I/O. :mod:`jsonkeys` calls it for a ``.ts`` / ``.js`` file (the
``@filigran/chatbot`` catalogs are ``export const fr: Catalog = { 'Key': 'Valeur', ... };``),
so ``sandbox_json_keys`` adds, renames, removes and reads their keys the way it
does for JSON, without a script rewriting the file.

The file holds ONE object literal assigned or exported (``= {``, ``export
default {``), one entry per line. Only the lines of the keys named change: an
entry left alone keeps its bytes (quotes, comments, escapes), a new entry is
written with the file's indent, quote character, key quoting (a bare
identifier when the file writes some keys bare) and line ending, and the commas
follow the file's trailing-comma style. A spread, a computed or shorthand key,
two object literals, an entry sharing a line with another or a duplicate key is
refused. Values set are strings; a value that is not a single string literal is
read back as its source text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: File extensions read as a script catalog.
SCRIPT_EXTENSIONS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")

_IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_NUMBER = re.compile(r"\d[\d_]*(?:\.\d+)?")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
_OPEN = "{[("
_CLOSE = "}])"


class ScriptCatalogError(ValueError):
    """The file cannot be edited as a catalog: nothing is written."""


@dataclass(frozen=True)
class Expression:
    """A value that is not a single string literal: its source text."""

    source: str

    def __str__(self) -> str:
        return f"(expression) {self.source}"


def is_script_catalog(path: str) -> bool:
    """True for a file :mod:`jsonkeys` hands to this module (by extension)."""
    return path.lower().endswith(SCRIPT_EXTENSIONS)


def _skip(text: str, i: int) -> int | None:
    """The index after the string, template or comment starting at *i*; None when there is none."""
    c = text[i]
    if c in "'\"":
        j = i + 1
        while j < len(text):
            if text[j] == "\\":
                j += 2
                continue
            if text[j] == c:
                return j + 1
            if text[j] == "\n":
                break
            j += 1
        raise ScriptCatalogError("a string literal is not closed on its line.")
    if c == "`":
        j = i + 1
        while j < len(text):
            if text[j] == "\\":
                j += 2
                continue
            if text[j] == "`":
                return j + 1
            if text.startswith("${", j):
                j = _close(text, j + 1)
                continue
            j += 1
        raise ScriptCatalogError("a template literal is not closed.")
    if text.startswith("//", i):
        end = text.find("\n", i)
        return len(text) if end < 0 else end
    if text.startswith("/*", i):
        end = text.find("*/", i + 2)
        if end < 0:
            raise ScriptCatalogError("a block comment is not closed.")
        return end + 2
    return None


def _close(text: str, i: int) -> int:
    """The index after the bracket that closes the one at *i*."""
    depth = 0
    j = i
    while j < len(text):
        skipped = _skip(text, j)
        if skipped is not None:
            j = skipped
            continue
        if text[j] in _OPEN:
            depth += 1
        elif text[j] in _CLOSE:
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    raise ScriptCatalogError("a bracket is not closed.")


def _previous_token(text: str, i: int) -> str:
    """The significant text right before *i*: the last character, or the last word."""
    j = i - 1
    while j >= 0 and text[j].isspace():
        j -= 1
    if j < 0:
        return ""
    if text[j].isalnum() or text[j] in "_$":
        start = j
        while start > 0 and (text[start - 1].isalnum() or text[start - 1] in "_$"):
            start -= 1
        return text[start : j + 1]
    return text[j]


def _object_span(text: str) -> tuple[int, int]:
    """Start and end (after ``}``) of the one object literal the file assigns or exports."""
    found: list[tuple[int, int]] = []
    i = 0
    while i < len(text):
        skipped = _skip(text, i)
        if skipped is not None:
            i = skipped
            continue
        c = text[i]
        if c in _OPEN:
            end = _close(text, i)
            if c == "{" and _previous_token(text, i) in ("=", "default", "return"):
                found.append((i, end))
            i = end
            continue
        i += 1
    if len(found) != 1:
        raise ScriptCatalogError(
            f"holds {len(found)} object literals assigned or exported; a catalog holds exactly one."
        )
    return found[0]


def decode_string(literal: str) -> str:
    """The value of a JS string literal (quotes included in *literal*)."""
    body = literal[1:-1]
    out: list[str] = []
    i = 0
    while i < len(body):
        c = body[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        n = body[i + 1]
        if n in _ESCAPES and not (n == "0" and i + 2 < len(body) and body[i + 2].isdigit()):
            out.append(_ESCAPES[n])
            i += 2
        elif n == "x":
            out.append(chr(int(body[i + 2 : i + 4], 16)))
            i += 4
        elif n == "u" and body[i + 2 : i + 3] == "{":
            end = body.index("}", i)
            out.append(chr(int(body[i + 3 : end], 16)))
            i = end + 1
        elif n == "u":
            out.append(chr(int(body[i + 2 : i + 6], 16)))
            i += 6
        elif n == "\r" and body[i + 2 : i + 3] == "\n":
            i += 3
        elif n in "\n\u2028\u2029":
            i += 2
        else:
            out.append(n)
            i += 2
    return "".join(out)


def encode_string(value: str, quote: str) -> str:
    """*value* as a JS string literal in *quote*; typographic characters are kept as they are."""
    escaped = (
        value.replace("\\", "\\\\")
        .replace(quote, "\\" + quote)
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    return quote + escaped + quote


@dataclass
class _Entry:
    lead: str  # comment and blank lines before the entry's own line
    body: str  # from the entry's line start to the end of the line it ends on, newline included
    key: str
    key_start: int  # offsets in body
    key_end: int
    value_start: int
    value_end: int
    comma: int | None  # offset in body of the comma after the value

    def value(self) -> str | Expression:
        source = self.body[self.value_start : self.value_end]
        if len(source) >= 2 and source[0] in "'\"" and source[-1] == source[0]:
            try:
                if _skip(source, 0) == len(source):
                    return decode_string(source)
            except (ScriptCatalogError, ValueError, IndexError):
                pass
        return Expression(source)

    def replace(self, start: int, end: int, new: str) -> None:
        """Replace ``body[start:end]`` (a non-empty span); the offsets at or past *end* move with it."""
        delta = len(new) - (end - start)
        self.body = self.body[:start] + new + self.body[end:]
        for name in ("key_start", "key_end", "value_start", "value_end", "comma"):
            offset = getattr(self, name)
            if offset is not None and offset >= end:
                setattr(self, name, offset + delta)


class ScriptCatalog:
    """The entries of a script catalog, edited by key; :meth:`render` writes the file back."""

    def __init__(self, text: str) -> None:
        start, end = _object_span(text)
        self.newline = "\r\n" if "\r\n" in text else "\n"
        open_line = text.find("\n", start)
        if open_line < 0 or open_line >= end:
            raise ScriptCatalogError("its object literal is on one line; a catalog holds one entry per line.")
        if text[start + 1 : open_line].strip():
            raise ScriptCatalogError(
                "an entry shares the line of the opening brace; a catalog holds one entry per line."
            )
        self.prefix = text[: open_line + 1]
        self.entries: list[_Entry] = []
        cursor = open_line + 1
        i = cursor
        close = end - 1
        while True:
            while i < close and (text[i].isspace() or text[i] == ","):
                i += 1
            skipped = _skip(text, i) if i < close else None
            if skipped is not None and text[i] in "/":
                i = skipped
                continue
            if i >= close:
                break
            entry, i = self._entry(text, cursor, i, close)
            self.entries.append(entry)
            cursor = i
        if not self.entries:
            raise ScriptCatalogError("its object literal has no entry to take the layout from.")
        self.suffix = text[cursor:]
        first = self.entries[0]
        self.indent = first.body[: len(first.body) - len(first.body.lstrip(" \t"))]
        self.trailing_comma = self.entries[-1].comma is not None
        self.bare_keys = any(e.body[e.key_start] not in "'\"" for e in self.entries)
        self.quote = next(
            (e.body[e.value_start] for e in self.entries if e.body[e.value_start] in "'\""),
            next((e.body[e.key_start] for e in self.entries if e.body[e.key_start] in "'\""), "'"),
        )
        seen: set[str] = set()
        for entry in self.entries:
            if entry.key in seen:
                raise ScriptCatalogError(f"holds the key {entry.key!r} twice.")
            seen.add(entry.key)

    def _entry(self, text: str, cursor: int, i: int, close: int) -> tuple[_Entry, int]:
        line_start = text.rfind("\n", 0, i) + 1
        if line_start < cursor or text[line_start:i].strip():
            raise ScriptCatalogError(
                f"line {text.count(chr(10), 0, i) + 1} holds two entries; a catalog holds one entry per line."
            )
        c = text[i]
        if text.startswith("...", i) or c == "[":
            raise ScriptCatalogError(
                f"line {text.count(chr(10), 0, i) + 1}: a spread or computed key is not a catalog key."
            )
        if c in "'\"":
            key_end = _skip(text, i) or i
            key = decode_string(text[i:key_end])
        else:
            match = _IDENTIFIER.match(text, i) or _NUMBER.match(text, i)
            if not match:
                raise ScriptCatalogError(f"line {text.count(chr(10), 0, i) + 1}: an entry has no key.")
            key_end = match.end()
            key = match.group(0)
        j = key_end
        while j < close and text[j] in " \t":
            j += 1
        if j >= close or text[j] != ":":
            raise ScriptCatalogError(
                f"line {text.count(chr(10), 0, i) + 1}: {key!r} is a shorthand or method, not a key: value entry."
            )
        j += 1
        while j < close and text[j] in " \t\r\n":
            j += 1
        value_start = j
        value_end = j
        while j < close:
            skipped = _skip(text, j)
            if skipped is not None:
                if text[j] == "/":  # a comment after the value ends it
                    break
                j = value_end = skipped
                continue
            if text[j] in _OPEN:
                j = value_end = _close(text, j)
                continue
            if text[j] == "," or text[j] == "\n":
                break
            if not text[j].isspace():
                value_end = j + 1
            j += 1
        if value_end == value_start:
            raise ScriptCatalogError(f"line {text.count(chr(10), 0, i) + 1}: {key!r} has no value.")
        rest = j
        while rest < close and text[rest] in " \t\r\n" and text[rest] != "\n":
            rest += 1
        comma = rest if rest < close and text[rest] == "," else None
        line_end = text.find("\n", comma if comma is not None else value_end)
        if line_end < 0 or line_end >= close:
            raise ScriptCatalogError(
                f"line {text.count(chr(10), 0, i) + 1}: the closing brace shares the line of the last entry."
            )
        nxt = text[(comma + 1 if comma is not None else value_end) : line_end]
        if nxt.strip() and not nxt.strip().startswith(("//", "/*")):
            raise ScriptCatalogError(
                f"line {text.count(chr(10), 0, i) + 1} holds two entries; a catalog holds one entry per line."
            )
        body = text[line_start : line_end + 1]
        entry = _Entry(
            lead=text[cursor:line_start],
            body=body,
            key=key,
            key_start=i - line_start,
            key_end=key_end - line_start,
            value_start=value_start - line_start,
            value_end=value_end - line_start,
            comma=None if comma is None else comma - line_start,
        )
        return entry, line_end + 1

    # The dict-like surface :mod:`jsonkeys` drives.

    def keys(self) -> list[str]:
        return [e.key for e in self.entries]

    def __contains__(self, key: object) -> bool:
        return any(e.key == key for e in self.entries)

    def _find(self, key: str) -> _Entry:
        return next(e for e in self.entries if e.key == key)

    def value(self, key: str) -> str | Expression:
        return self._find(key).value()

    def _key_text(self, key: str) -> str:
        if self.bare_keys and _IDENTIFIER.fullmatch(key):
            return key
        return encode_string(key, self.quote)

    def rename(self, old: str, new: str) -> None:
        entry = self._find(old)
        entry.replace(entry.key_start, entry.key_end, self._key_text(new))
        entry.key = new

    def set_value(self, key: str, value: str) -> None:
        entry = self._find(key)
        entry.replace(entry.value_start, entry.value_end, encode_string(value, self.quote))

    def add(self, key: str, value: str, side: str | None = None, anchor: str | None = None) -> None:
        text = f"{self.indent}{self._key_text(key)}: "
        literal = encode_string(value, self.quote)
        body = f"{text}{literal},{self.newline}"
        entry = _Entry(
            lead="",
            body=body,
            key=key,
            key_start=len(self.indent),
            key_end=len(self.indent) + len(self._key_text(key)),
            value_start=len(text),
            value_end=len(text) + len(literal),
            comma=len(text) + len(literal),
        )
        if anchor is None:
            self.entries.append(entry)
            return
        index = self.keys().index(anchor)
        self.entries.insert(index + (1 if side == "after" else 0), entry)

    def remove(self, key: str) -> None:
        index = self.keys().index(key)
        lead = self.entries.pop(index).lead
        if lead:
            if index < len(self.entries):
                self.entries[index].lead = lead + self.entries[index].lead
            else:
                self.suffix = lead + self.suffix

    def render(self) -> str:
        last = len(self.entries) - 1
        for n, entry in enumerate(self.entries):
            wants = n < last or self.trailing_comma
            if wants and entry.comma is None:
                entry.body = entry.body[: entry.value_end] + "," + entry.body[entry.value_end :]
                entry.comma = entry.value_end
            elif not wants and entry.comma is not None:
                entry.replace(entry.comma, entry.comma + 1, "")
                entry.comma = None
        return self.prefix + "".join(e.lead + e.body for e in self.entries) + self.suffix
