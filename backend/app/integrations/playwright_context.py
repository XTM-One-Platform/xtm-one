# SPDX-License-Identifier: Apache-2.0

"""The page a failed Playwright test saw, read from its ``error-context.md``.

Pure: no I/O. On a failure Playwright writes ``<outputDir>/<test dir>/error-context.md``
(its default ``outputDir`` is ``test-results``) and prints its path in the log as
``Error Context: test-results/<test dir>/error-context.md``. The log itself only
carries the assertion (``expect(locator).toBeVisible() failed``); the reason a
reader needs (an alert, an error banner, the message a page printed) is in the
file's ARIA snapshot, a YAML fence: under a ``# Page snapshot`` heading in
older Playwright releases, as the second fence of ``# Error details`` (after
the error and its call log) in current ones (#11277 retest, xtm-one #11270).

This module finds the paths a log names and reduces a snapshot to the lines
that carry text, the landmark chrome (navigation, banner, contentinfo,
complementary) and the element references left out, bounded.
"""

from __future__ import annotations

import re

#: Tests whose page snapshot one answer shows at most.
MAX_CONTEXTS = 3
#: Snapshot lines kept per test.
MAX_SNAPSHOT_LINES = 40
#: Characters of one test's snapshot.
MAX_SNAPSHOT_CHARS = 2_500
#: Characters of one snapshot line.
MAX_SNAPSHOT_LINE_CHARS = 300

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
#: ``Error Context: test-results/<dir>/error-context.md`` as Playwright's list and line reporters print it.
_ERROR_CONTEXT = re.compile(r"Error Context:\s+(?P<path>\S*?(?P<dir>[^/\s]+)/error-context\.md)")
_SNAPSHOT_HEADING = re.compile(r"^#{1,6}\s+Page snapshot\s*$", re.IGNORECASE)
_HEADING = re.compile(r"^#{1,6}\s+\S")
_FENCE = re.compile(r"^\s*```")
#: The opening of a YAML fence (```yaml / ```yml), the form an ARIA snapshot is written in.
_YAML_FENCE = re.compile(r"^\s*```\s*ya?ml\s*$", re.IGNORECASE)
#: ``- role "name" [attr] [ref=e12]: text`` (YAML list item of an ARIA snapshot).
_ITEM = re.compile(r"^(?P<indent>\s*)-\s+(?P<body>.*)$")
_ROLE = re.compile(r"^(?P<role>[a-z][\w-]*)")
_ATTRS = re.compile(r"\s*\[(?:ref|cursor|active|level|checked|expanded|pressed|selected|disabled)(?:=[^\]]*)?\]")
#: Landmarks every page of an application repeats: they never explain a failure.
_CHROME_ROLES = frozenset({"navigation", "banner", "contentinfo", "complementary"})
#: A line that is only structure: a role with no name and no text.
_BARE = re.compile(r"^[a-z][\w-]*:?$")


def error_context_paths(log: str) -> list[tuple[str, str]]:
    """The ``(path as printed, test directory)`` of every ``error-context.md`` the log names, in log order, once each."""
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for match in _ERROR_CONTEXT.finditer(_ANSI.sub("", log)):
        directory = match.group("dir")
        if directory in seen:
            continue
        seen.add(directory)
        out.append((match.group("path"), directory))
    return out


def _aria_fence_block(lines: list[str]) -> list[str] | None:
    """The first YAML fence whose first line is an ARIA list item (``- main``, ``- generic [ref=e1]:``), fence removed."""
    for i, line in enumerate(lines):
        if not _YAML_FENCE.match(line):
            continue
        block: list[str] = []
        for inner in lines[i + 1 :]:
            if _FENCE.match(inner):
                break
            block.append(inner)
        first = next((ln for ln in block if ln.strip()), "")
        item = _ITEM.match(first)
        if item and _ROLE.match(item.group("body")):
            return block
    return None


def _snapshot_block(markdown: str) -> list[str] | None:
    """The lines of the page snapshot, its code fence removed; ``None`` when the file holds none.

    The ``# Page snapshot`` section when the file has one, else the first
    YAML fence that is an ARIA snapshot, wherever it sits.
    """
    lines = markdown.splitlines()
    start = next((i for i, ln in enumerate(lines) if _SNAPSHOT_HEADING.match(ln.strip())), None)
    if start is None:
        return _aria_fence_block(lines)
    block: list[str] = []
    in_fence = False
    for line in lines[start + 1 :]:
        if _FENCE.match(line):
            if in_fence:
                break
            in_fence = True
            continue
        if not in_fence and _HEADING.match(line):
            break
        block.append(line)
    return block


def has_page_snapshot(markdown: str) -> bool:
    """Whether the file holds a page snapshot at all (an empty reduction then means: only chrome)."""
    return _snapshot_block(markdown) is not None


def snapshot_lines(markdown: str) -> list[str]:
    """The text-bearing lines of the page snapshot, chrome landmarks and element references left out.

    Bounded by :data:`MAX_SNAPSHOT_LINES` and :data:`MAX_SNAPSHOT_CHARS`; a
    cut ends with a marker. Empty when the file has no page snapshot.
    """
    kept: list[str] = []
    skip_deeper_than: int | None = None
    for raw in _snapshot_block(markdown) or []:
        item = _ITEM.match(raw)
        if not item:
            continue
        indent = len(item.group("indent"))
        if skip_deeper_than is not None:
            if indent > skip_deeper_than:
                continue
            skip_deeper_than = None
        body = _ATTRS.sub("", item.group("body")).strip()
        role = _ROLE.match(body)
        if role and role.group("role") in _CHROME_ROLES:
            skip_deeper_than = indent
            continue
        if not body or _BARE.match(body):
            continue
        if len(body) > MAX_SNAPSHOT_LINE_CHARS:
            body = body[: MAX_SNAPSHOT_LINE_CHARS - 3] + "..."
        kept.append(f"{' ' * min(indent, 12)}- {body}")
    out: list[str] = []
    used = 0
    for line in kept:
        if len(out) >= MAX_SNAPSHOT_LINES or used + len(line) + 1 > MAX_SNAPSHOT_CHARS:
            out.append(f"[snapshot clipped: {len(out)} of {len(kept)} text line(s) shown]")
            break
        out.append(line)
        used += len(line) + 1
    return out


def format_contexts(contexts: list[tuple[str, list[str] | None]], sources: dict[str, str] | None = None) -> str:
    """The section a job log answer appends: one block per test directory and its snapshot lines.

    ``None`` lines: the file holds no page snapshot (a Playwright layout this
    reader does not know); the block then names the file and the artifact in
    *sources* that holds it, never "no text outside the page chrome", which
    is said only of a snapshot made of chrome alone.
    """
    parts = ["--- page snapshot of each failed Playwright test (error-context.md, chrome left out) ---"]
    for directory, lines in contexts:
        parts.append(f"\n[{directory}]")
        if lines is None:
            source = (sources or {}).get(directory)
            member = f"{directory}/error-context.md"
            where = f"download artifact {source} member {member}" if source else f"read {member}"
            parts.append(f"(no page snapshot found in {member}; {where})")
        else:
            parts.extend(lines or ["(no text outside the page chrome)"])
    return "\n".join(parts)
