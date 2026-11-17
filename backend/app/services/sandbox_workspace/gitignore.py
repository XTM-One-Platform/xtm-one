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

"""Which untracked files of a checkout its ``.gitignore`` files exclude, read as git reads them.

A build or an install in a checkout writes files the repository never
tracks (``frontend/dist``, ``*.tsbuildinfo``): without this, the checkout's
change set (``sandbox_checkout_status``, ``sandbox_checkout_diff``,
``create_github_commit(from_checkout=...)``) listed each of them as added.
:class:`GitIgnore` holds the rules of every ``.gitignore`` of the checkout,
the root one first and the deeper ones after, so the last matching rule wins
as in git: a pattern without a slash matches at any depth below its file, a
pattern with one is anchored to its file's directory, a trailing ``/``
matches a directory only, ``!`` re-includes, ``*`` / ``?`` / ``[...]`` stay
inside one segment and ``**`` crosses segments. A file under an excluded
directory is excluded whatever a later ``!`` says, as git cannot re-include
it. Only untracked files are ever asked about: a tracked file stays a change
whatever ``.gitignore`` says (``checkout.changes_since``).
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable
from dataclasses import dataclass

#: The file name git reads its exclusion rules from, in any directory of a tree.
GITIGNORE_NAME = ".gitignore"
#: ``.gitignore`` files read per checkout at most (the shallowest first), and bytes read per file.
MAX_GITIGNORE_FILES = 200
MAX_GITIGNORE_BYTES = 256 * 1024


@dataclass(frozen=True)
class _Rule:
    #: The directory of the ``.gitignore`` holding the rule, relative to the checkout root ("" at the root).
    base: str
    regex: re.Pattern[str]
    negated: bool
    dir_only: bool


def _translate(glob: str) -> str:
    """The regular expression of one gitignore glob (no anchor, no trailing slash)."""
    out: list[str] = []
    i, n = 0, len(glob)
    while i < n:
        char = glob[i]
        if char == "*":
            if glob.startswith("**", i):
                j = i + 2
                if j < n and glob[j] == "/":
                    out.append("(?:.*/)?")
                    i = j + 1
                    continue
                out.append(".*")
                i = j
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            end = glob.find("]", i + 2)
            if end == -1:
                out.append(re.escape(char))
            else:
                body = glob[i + 1 : end]
                if body[:1] in ("!", "^"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = end + 1
                continue
        elif char == "\\" and i + 1 < n:
            out.append(re.escape(glob[i + 1]))
            i += 2
            continue
        else:
            out.append(re.escape(char))
        i += 1
    return "".join(out)


def _rule(line: str, base: str) -> _Rule | None:
    """The rule one line of a ``.gitignore`` at *base* states, or ``None`` (blank, comment, bare slash)."""
    text = line.rstrip("\r\n")
    if not text or text.startswith("#"):
        return None
    stripped = text.rstrip(" ")
    if stripped.endswith("\\") and len(stripped) < len(text):
        stripped += " "
    text = stripped
    negated = text.startswith("!")
    if negated or text.startswith(("\\#", "\\!")):
        text = text[1:]
    dir_only = text.endswith("/")
    text = text.rstrip("/")
    if not text:
        return None
    anchored = "/" in text
    text = text.lstrip("/")
    try:
        regex = re.compile(("^" if anchored else "^(?:.*/)?") + _translate(text) + "$")
    except re.error:
        return None
    return _Rule(base=base, regex=regex, negated=negated, dir_only=dir_only)


class GitIgnore:
    """The exclusion rules of a checkout's ``.gitignore`` files; :meth:`ignores` asks about one path."""

    def __init__(self, files: Iterable[tuple[str, bytes]] = ()) -> None:
        """*files* are ``(repository path of a .gitignore, its bytes)``, in any order."""
        ordered = sorted(files, key=lambda item: (item[0].count("/"), item[0]))
        rules: list[_Rule] = []
        for path, data in ordered[:MAX_GITIGNORE_FILES]:
            if posixpath.basename(path) != GITIGNORE_NAME:
                continue
            base = posixpath.dirname(path)
            text = data[:MAX_GITIGNORE_BYTES].decode("utf-8", errors="replace")
            rules.extend(r for r in (_rule(line, base) for line in text.splitlines()) if r is not None)
        self._rules = tuple(rules)

    def __bool__(self) -> bool:
        return bool(self._rules)

    def _verdict(self, path: str, *, is_dir: bool) -> bool:
        excluded = False
        for rule in self._rules:
            if rule.dir_only and not is_dir:
                continue
            if rule.base:
                if not path.startswith(rule.base + "/"):
                    continue
                relative = path[len(rule.base) + 1 :]
            else:
                relative = path
            if rule.regex.match(relative):
                excluded = not rule.negated
        return excluded

    def ignores(self, repo_path: str) -> bool:
        """Whether git would leave the untracked file *repo_path* (relative to the checkout root) out."""
        if not self._rules:
            return False
        parts = repo_path.split("/")
        for depth in range(1, len(parts)):
            if self._verdict("/".join(parts[:depth]), is_dir=True):
                return True
        return self._verdict(repo_path, is_dir=False)


def gitignore_paths(repo_paths: Iterable[str]) -> list[str]:
    """The ``.gitignore`` files among *repo_paths*, the shallowest first, bounded by :data:`MAX_GITIGNORE_FILES`."""
    found = [p for p in repo_paths if posixpath.basename(p) == GITIGNORE_NAME]
    found.sort(key=lambda p: (p.count("/"), p))
    return found[:MAX_GITIGNORE_FILES]
