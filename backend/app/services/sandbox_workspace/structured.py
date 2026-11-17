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

"""Parse a merged structured file (JSON, YAML, TOML) before it is written or reported.

A line merge knows nothing of syntax: two sides that each add a key at the
same place can meet at a hunk join without the comma between them, keep a
context line twice, or write the same key twice, the later value silently
winning (#11986). Every merge path of the workspace and of the GitHub tools
calls :func:`merge_defect` on what it is about to write: a defect makes the
file a conflict with its reason and line, never "merged cleanly".

A merge is judged only when both sides it started from parse: a file that
was already broken (a Helm template with ``{{ }}``, a JSON file with
comments) is not the merge's doing and is left to the line merge.

A resolved file a person or an agent wrote (``keep: both``, a merged
``content``) is also parsed as Python when its path is a ``.py`` file
(:func:`resolution_kind`, #12179): a hunk written twice or cut short is
refused with its line before it lands, never committed as a ``SyntaxError``.
"""

from __future__ import annotations

import ast
import json
import re
import tomllib
from typing import Any

import yaml

#: Bytes of one text the validator parses at most; a larger file is not judged.
MAX_STRUCTURED_BYTES = 8 * 1024 * 1024

JSON = "JSON"
YAML = "YAML"
TOML = "TOML"
#: Source checked for syntax only (no key semantics): resolved files, never a clean line merge.
PYTHON = "Python"

_SUFFIXES = {".json": JSON, ".yaml": YAML, ".yml": YAML, ".toml": TOML}
_SOURCE_SUFFIXES = {".py": PYTHON, ".pyi": PYTHON}


class _DuplicateKeyError(ValueError):
    def __init__(self, key: Any, line: int = 0) -> None:
        super().__init__(str(key))
        self.key = key
        self.line = line


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise _DuplicateKeyError(key)
        out[key] = value
    return out


class _UniqueKeyLoader(yaml.SafeLoader):
    """``yaml.SafeLoader`` refusing a mapping that holds the same key twice (PyYAML keeps the last one)."""


def _unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    seen: set[Any] = set()
    for key_node, _value_node in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            continue
        key = loader.construct_object(key_node, deep=True)
        try:
            duplicate = key in seen
        except TypeError:  # an unhashable key: PyYAML refuses it itself
            continue
        if duplicate:
            raise _DuplicateKeyError(key, key_node.start_mark.line + 1)
        seen.add(key)
    mapping: dict[Any, Any] = loader.construct_mapping(node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)

_TOML_LINE_RE = re.compile(r"at line (\d+)")


def structured_kind(path: str, *texts: str) -> str:
    """The format *path* holds (``JSON``, ``YAML``, ``TOML``), '' for plain text.

    Named by the extension; a file without one of those extensions is JSON
    when every non-empty text of *texts* is a JSON object or array (the
    line merge's former check).
    """
    lowered = path.lower()
    for suffix, kind in _SUFFIXES.items():
        if lowered.endswith(suffix):
            return kind
    shown = [text for text in texts if text.strip()]
    if shown and all(text.lstrip()[:1] in ("{", "[") and not parse_defect(JSON, text) for text in shown):
        return JSON
    return ""


def resolution_kind(path: str) -> str:
    """The format a resolved *path* is parsed as before it lands: a structured format, else ``Python``, else ''."""
    kind = structured_kind(path)
    if kind:
        return kind
    lowered = path.lower()
    return next((kind for suffix, kind in _SOURCE_SUFFIXES.items() if lowered.endswith(suffix)), "")


def _json_key_lines(text: str, key: str) -> str:
    needle = re.compile(re.escape(json.dumps(key, ensure_ascii=False)) + r"\s*:")
    lines = [text.count("\n", 0, match.start()) + 1 for match in needle.finditer(text)]
    if not lines:
        return ""
    shown = ", ".join(str(n) for n in lines[:4]) + (f" and {len(lines) - 4} more" if len(lines) > 4 else "")
    return f" (lines {shown})" if len(lines) > 1 else f" (line {shown})"


def parse_defect(kind: str, text: str) -> str:
    """Why *text* is not a valid *kind* file ('' when it is): the parse error or the duplicate key, with its line."""
    if not kind or len(text) > MAX_STRUCTURED_BYTES:
        return ""
    try:
        if kind == JSON:
            json.loads(text, object_pairs_hook=_pairs)
        elif kind == YAML:
            for _document in yaml.load_all(text, Loader=_UniqueKeyLoader):  # a SafeLoader subclass
                pass
        elif kind == TOML:
            tomllib.loads(text)
        elif kind == PYTHON:
            ast.parse(text)
    except _DuplicateKeyError as exc:
        where = f" (line {exc.line})" if exc.line else (_json_key_lines(text, exc.key) if kind == JSON else "")
        return f"key '{exc.key}' written twice in one object{where}"
    except json.JSONDecodeError as exc:
        return f"not valid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark is not None else ""
        problem = getattr(exc, "problem", None) or "parse error"
        return f"not valid YAML{where}: {problem}"
    except SyntaxError as exc:  # IndentationError and TabError included
        where = f" at line {exc.lineno}" + (f", column {exc.offset}" if exc.offset else "") if exc.lineno else ""
        return f"not valid Python{where}: {exc.msg}"
    except tomllib.TOMLDecodeError as exc:
        found = _TOML_LINE_RE.search(str(exc))
        where = f" at line {found.group(1)}" if found else ""
        return f"not valid TOML{where}: {str(exc).split(' (at ')[0]}"
    except (ValueError, RecursionError) as exc:
        return f"not valid {kind}: {exc}"
    return ""


def resolution_defect(path: str, resolved: str, *sides: str) -> str:
    """Why a *resolved* text breaks *path* ('' when sound or not judged): the parse error with its line.

    Judged only when *path* has a parser (:func:`resolution_kind`) and every non-empty text of *sides* (the
    versions the resolution started from) parses: a file already broken on one side is not the resolution's doing.
    """
    kind = resolution_kind(path)
    if not kind or any(side.strip() and parse_defect(kind, side) for side in sides):
        return ""
    return parse_defect(kind, resolved)


def merge_defect(path: str, ours: str, theirs: str, merged: str) -> tuple[str, str]:
    """``(kind, why)``: the format of *path* and why *merged* breaks it ('' when sound or not judged).

    *kind* is '' for plain text, and also when one side the merge started
    from does not parse (the breakage is not the merge's): the file is then
    neither validated nor refused.
    """
    kind = structured_kind(path, ours, theirs)
    if not kind or parse_defect(kind, ours) or parse_defect(kind, theirs):
        return "", ""
    return kind, parse_defect(kind, merged)
