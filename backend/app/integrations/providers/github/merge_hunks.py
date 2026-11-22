"""The line merge of a conflicting ``merge_github_branch``, on the server.

GitHub's Merges API refuses a merge whose two sides changed the same paths,
and names nothing. :func:`line_merge` reads the merge base, the branch and
the ref of each path changed on both sides and merges them line by line with
the diff3 merge ``sandbox_checkout_rebase`` uses
(:func:`app.services.sandbox_workspace.rebase.merge_text`):

- a path whose changes do not overlap merges cleanly: its merged text becomes
  the resolution the caller fills in, so the agent resolves nothing for it;
  so does a JSON object file whose keys merge cleanly although its lines
  overlap (:mod:`app.services.sandbox_workspace.jsonmerge`, the i18n catalogs);
- a path with overlapping hunks is a conflict: :func:`describe_conflicts`
  names each hunk with its line range on both sides and both texts, so the
  agent writes a resolution for those hunks only;
- a binary changed on both sides, a delete against a change, or a path that
  is not a plain file is a conflict with its reason: it is resolved whole.

Nothing is written here and no file reaches the sandbox: the blobs are read
with the integration's credential on the server and only the bounded hunks
reach the model. The read budget applies per path: each path's three
versions are read, merged and released before the next one, so a path over
``MAX_MERGE_READ_BYTES`` is a conflict with its reason (resolved whole) while
the others still merge. Only a tree GitHub does not list in full, a failed
read, or reads past ``MAX_LINE_MERGE_TOTAL_BYTES`` in all answer the reason,
and the caller falls back to the list of paths.

The merge of each path runs in a worker thread (difflib is quadratic on a
large file and would block the event loop, so the worker lost its lease and
the call never answered, #51), within ``LINE_MERGE_SECONDS`` in all: a path
not merged in time, and every path after it, is a conflict with its reason,
so the call always answers with what it merged.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

from app.services.sandbox_workspace import structured
from app.services.sandbox_workspace.jsonmerge import merge_json_objects, removed_keys
from app.services.sandbox_workspace.reading import is_text
from app.services.sandbox_workspace.rebase import ConflictRegion, _added, merge_text

from .contents import _MAX_COMMIT_CONTENT_BYTES, _MODE_EXECUTABLE, _MODE_FILE
from .merge_resolution import _MAX_READ_BYTES, _read_blob, _read_side, _Side

#: Paths changed on both sides the line merge reads at most; more falls back to the list of paths.
MAX_MERGED_PATHS = 40
#: Bytes of one path's three versions the line merge reads; a path over it is a conflict resolved whole.
MAX_MERGE_READ_BYTES = 3 * _MAX_COMMIT_CONTENT_BYTES
#: Bytes of blobs one line merge reads in all, path after path (the resolver's own read budget).
MAX_LINE_MERGE_TOTAL_BYTES = _MAX_READ_BYTES
#: Seconds one line merge spends merging paths in all; a path past it is a conflict resolved whole (#51).
LINE_MERGE_SECONDS = 60.0
#: Lines of one side of one hunk shown in the answer; the rest are counted.
MAX_HUNK_LINES = 40
#: Characters of one shown line; the rest are counted.
MAX_LINE_CHARS = 300
#: Characters of the whole hunk description before it is cut.
MAX_DESCRIPTION_CHARS = 20_000


@dataclass(frozen=True)
class PathMerge:
    """The line merge of one path: the merged text when clean, else the reason or the hunks."""

    path: str
    merged: str | None = None
    reason: str = ""
    ours: tuple[str, ...] = ()
    theirs: tuple[str, ...] = ()
    regions: tuple[ConflictRegion, ...] = ()
    union: str | None = None
    #: The merge with every overlapping hunk taken from the branch (``keep: branch``) or from the ref
    #: (``keep: ref``), every non-overlapping hunk of both sides kept; None for a path resolved whole.
    keep_ours: str | None = None
    keep_theirs: str | None = None
    #: The structured format both sides parse as (``JSON``, ``YAML``, ``TOML``, see :mod:`structured`): a merged
    #: text of this path must parse too, with no key written twice; '' for plain text.
    structured_kind: str = ""
    #: The lines both sides added against the base (the meaningful ones): written once in a merge.
    added_both: tuple[str, ...] = ()
    #: How often each side holds each of those lines: a merge holds a line at most as often as one side.
    side_counts: tuple[tuple[str, int], ...] = ()
    #: The keys of a JSON object file the merge base and the ref hold that the branch removed: a merge removes them
    #: without a conflict, so the answer names them for a check (a stale copy of a catalog loses keys that way, #1095).
    removed_keys: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return self.merged is not None

    def defect(self, text: str) -> str:
        """Why *text*, a merged version of this path, would break it; '' when it is sound."""
        return merged_text_defect(self.structured_kind, self.added_both, dict(self.side_counts), text)

    def parse_defect(self, text: str) -> str:
        """Why *text* does not parse as the format both sides hold ('' when sound or plain text): the union check."""
        return merged_text_defect(self.structured_kind, (), {}, text)


#: A line worth checking for a duplicate: three word characters at least (a lone brace or blank line repeats legitimately).
_MEANINGFUL_RE = re.compile(r"\w{3}")


def merged_text_defect(kind: str, added_both: tuple[str, ...], side_counts: dict[str, int], text: str) -> str:
    """Why a merged *text* breaks its path: a *kind* file both sides held valid that no longer parses or writes a
    key twice, or a both-sides addition twice."""
    broken = structured.parse_defect(kind, text)
    if broken:
        return f"both sides are valid {kind} but the merged text is not: {broken}"
    if added_both:
        counts = Counter(line.strip() for line in text.splitlines())
        twice = [line for line in added_both if counts[line] > side_counts.get(line, 1)]
        if twice:
            shown = twice[0] if len(twice[0]) <= 120 else twice[0][:120] + "..."
            more = f" and {len(twice) - 1} more" if len(twice) > 1 else ""
            return f"a line both sides added is written twice: '{shown}'{more}"
    return ""


def _structured_sides(path: str, ours_text: str, theirs_text: str) -> str:
    """The structured format both sides of *path* parse as, '' when plain text or one side is already broken."""
    if not ours_text.strip():
        return ""
    kind = structured.structured_kind(path, ours_text, theirs_text)
    if not kind or structured.parse_defect(kind, ours_text) or structured.parse_defect(kind, theirs_text):
        return ""
    return kind


def _checks(path: str, base_text: str, ours_text: str, theirs_text: str) -> dict[str, Any]:
    """What a merged text of these three versions is checked against (:meth:`PathMerge.defect`)."""
    base_lines = base_text.splitlines(keepends=True)
    ours_added = {line.strip() for line in _added(base_lines, ours_text.splitlines(keepends=True))}
    theirs_added = {line.strip() for line in _added(base_lines, theirs_text.splitlines(keepends=True))}
    # A line the base already holds is a recurring line of the file (a dict key every provider block opens
    # with): each side adding it in its own block is two additions, not one written twice.
    base_held = {line.strip() for line in base_lines}
    both = tuple(
        sorted(line for line in ours_added & theirs_added if _MEANINGFUL_RE.search(line) and line not in base_held)
    )
    ours_counts = Counter(line.strip() for line in ours_text.splitlines())
    theirs_counts = Counter(line.strip() for line in theirs_text.splitlines())
    kind = _structured_sides(path, ours_text, theirs_text)
    return {
        "structured_kind": kind,
        "added_both": both,
        "side_counts": tuple((line, max(ours_counts[line], theirs_counts[line])) for line in both),
        "removed_keys": tuple(removed_keys(base_text, ours_text, theirs_text)) if kind == structured.JSON else (),
    }


def _text(data: bytes | None) -> str | None:
    """*data* as text, ``''`` for an absent side, ``None`` for a binary."""
    if data is None:
        return ""
    if not is_text(data):
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def merge_path(
    path: str, base: bytes | None, ours: bytes | None, theirs: bytes | None, *, ref_first: bool = False
) -> PathMerge:
    """The three-way merge of one path from its three versions (``None``: absent on that side).

    *ref_first*: a JSON object file merged by key keeps the order of *theirs* (the ref merged into the branch),
    the branch's own keys after it, so the branch's diff against the ref holds its own keys only (#1116).
    """
    if ours is None or theirs is None:
        gone, kept = ("the branch", "the ref") if ours is None else ("the ref", "the branch")
        return PathMerge(path, reason=f"removed on {gone}, changed on {kept}: keep one side whole")
    texts = (_text(base), _text(ours), _text(theirs))
    if any(text is None for text in texts):
        return PathMerge(path, reason="a binary file changed on both sides: keep one side whole")
    base_text, ours_text, theirs_text = (text or "" for text in texts)
    checks = _checks(path, base_text, ours_text, theirs_text)
    merged = merge_text(base_text, ours_text, theirs_text)
    defect = ""
    if not merged.conflicts:
        defect = merged_text_defect(
            checks["structured_kind"], checks["added_both"], dict(checks["side_counts"]), merged.text
        )
        if not defect:
            return PathMerge(path, merged=merged.text, **checks)
    by_key = merge_json_objects(base_text, ours_text, theirs_text, ref_first=ref_first)
    if by_key is not None and not structured.parse_defect(checks["structured_kind"], by_key):
        return PathMerge(path, merged=by_key, **checks)
    regions = merged.regions
    if defect:
        # A textual merge without an overlap that still breaks the path (an addition both sides made in
        # different places, JSON that no longer parses) is a conflict resolved by hand, the reason named.
        return PathMerge(
            path,
            reason=f"the line merge has no overlap but {defect}: give a merged 'content' or keep one side",
            **checks,
        )
    union = merge_text(base_text, ours_text, theirs_text, both=True).text
    # A JSON object file (an i18n catalog) settles a keep by key: the line merge's preferred hunk would take one
    # side's whole tail and drop the keys the other side appended there; the key merge keeps them.
    keep_ours = merge_json_objects(base_text, ours_text, theirs_text, prefer="ours", ref_first=ref_first)
    if keep_ours is None:
        keep_ours = merge_text(base_text, ours_text, theirs_text, prefer="ours").text
    keep_theirs = merge_json_objects(base_text, ours_text, theirs_text, prefer="theirs", ref_first=ref_first)
    if keep_theirs is None:
        keep_theirs = merge_text(base_text, ours_text, theirs_text, prefer="theirs").text
    return PathMerge(
        path,
        reason="added on both sides" if base is None else "",
        ours=tuple(ours_text.splitlines()),
        theirs=tuple(theirs_text.splitlines()),
        regions=regions,
        union=union,
        keep_ours=keep_ours,
        keep_theirs=keep_theirs,
        **checks,
    )


@dataclass(frozen=True)
class LineMerge:
    """Every path changed on both sides, merged: the clean ones and the conflicting ones."""

    merges: dict[str, PathMerge]

    @property
    def clean(self) -> list[str]:
        return sorted(path for path, merge in self.merges.items() if merge.clean)

    @property
    def conflicting(self) -> list[str]:
        return sorted(path for path, merge in self.merges.items() if not merge.clean)

    @property
    def removed(self) -> list[str]:
        """``path: key, key`` for every JSON path whose branch removed keys the base and the ref hold (#1095)."""
        return [
            f"{path}: {_keys_shown(merge.removed_keys)}"
            for path, merge in sorted(self.merges.items())
            if merge.removed_keys
        ]

    @property
    def validated(self) -> list[str]:
        """``path (FORMAT)`` for every clean structured path: parsed after the merge, no duplicate key (#11986)."""
        return sorted(
            f"{path} ({merge.structured_kind})"
            for path, merge in self.merges.items()
            if merge.clean and merge.structured_kind
        )


async def line_merge(
    client: Any,
    headers: dict[str, str],
    repo: str,
    repo_url: str,
    merge_base: str,
    branch: str,
    head: str,
    paths: list[str],
) -> LineMerge | str:
    """Read the three versions of *paths* and merge each line by line; the reason when it cannot run.

    *head* is merged into *branch*: a JSON object file merged by key keeps *head*'s key order (#1116).
    """
    if len(paths) > MAX_MERGED_PATHS:
        return f"{len(paths)} paths changed on both sides, over the {MAX_MERGED_PATHS} the line merge reads"
    sides: list[_Side] = []
    for ref in (merge_base, branch, head):
        side = await _read_side(client, headers, repo, repo_url, ref)
        if isinstance(side, str):
            return side.removeprefix("Error: ")
        sides.append(side)
    merges: dict[str, PathMerge] = {}
    total = 0
    deadline = time.monotonic() + LINE_MERGE_SECONDS
    for path in paths:
        if time.monotonic() >= deadline:
            merges[path] = PathMerge(path, reason=_OVER_TIME_BUDGET)
            continue
        entries = [side.entries.get(path) for side in sides]
        # An executable file on every side merges like a plain one: the signed commit keeps its mode.
        # A path deleted on one side keeps no mode to carry, so an executable one is still resolved whole.
        modes = {entry.mode for entry in entries if entry is not None}
        plain = modes == {_MODE_FILE} or (modes == {_MODE_EXECUTABLE} and None not in entries[1:])
        odd = (
            None
            if plain
            else next((entry for entry in entries if entry is not None and entry.mode != _MODE_FILE), None)
        )
        if odd is not None:
            merges[path] = PathMerge(path, reason=f"not a plain file (mode {odd.mode}): keep one side whole")
            continue
        listed = sum(entry.size for entry in entries if entry is not None)
        if listed > MAX_MERGE_READ_BYTES:
            merges[path] = PathMerge(path, reason=_over_path_budget(listed))
            continue
        total += listed
        if total > MAX_LINE_MERGE_TOTAL_BYTES:
            return f"the versions of the paths are over the {MAX_LINE_MERGE_TOTAL_BYTES} bytes one line merge reads"
        # One path at a time: its blobs are released once it is merged, so memory is bound per path.
        blobs: dict[str, bytes] = {}
        for entry in entries:
            if entry is None or entry.sha in blobs:
                continue
            data = await _read_blob(client, headers, repo, repo_url, entry.sha, path)
            if isinstance(data, str):
                return data.removeprefix("Error: ")
            blobs[entry.sha] = data
        read = sum(len(data) for data in blobs.values())
        if read > MAX_MERGE_READ_BYTES:
            merges[path] = PathMerge(path, reason=_over_path_budget(read))
            continue
        base, ours, theirs = (None if entry is None else blobs[entry.sha] for entry in entries)
        remaining = deadline - time.monotonic()
        try:
            # Off the event loop; a thread past the budget finishes on its own and its result is dropped.
            merges[path] = await asyncio.wait_for(
                asyncio.to_thread(merge_path, path, base, ours, theirs, ref_first=True),
                timeout=max(remaining, 0.001),
            )
        except TimeoutError:
            merges[path] = PathMerge(path, reason=_OVER_TIME_BUDGET)
    return LineMerge(merges)


#: Keys of one path named in an answer; the rest are counted.
MAX_REMOVED_KEYS_SHOWN = 20


def _keys_shown(keys: tuple[str, ...]) -> str:
    shown = ", ".join(keys[:MAX_REMOVED_KEYS_SHOWN])
    more = len(keys) - MAX_REMOVED_KEYS_SHOWN
    return shown + (f" and {more} more" if more > 0 else "")


def removed_lines(merged: LineMerge | None) -> list[str]:
    """The answer lines naming the JSON keys the branch removed, or none (#1095)."""
    if merged is None or not merged.removed:
        return []
    return [
        f"Keys the branch removed that the merge base and the ref still hold ({len(merged.removed)} path(s)); "
        "the merge removes them too: restore any the branch dropped by mistake (a stale copy of the file):",
        *(f"- {line}" for line in merged.removed),
    ]


_OVER_TIME_BUDGET = (
    "not merged within the time budget of the line merge: keep one side whole or give a merged 'content' or "
    "'workspace_path'"
)


def _over_path_budget(size: int) -> str:
    return (
        f"its three versions add up to {size} bytes, over the {MAX_MERGE_READ_BYTES} the line merge reads for one "
        "path: keep one side whole or give a merged 'content' or 'workspace_path'"
    )


def _shown(lines: tuple[str, ...], start: int, end: int) -> list[str]:
    shown = []
    for line in lines[start : min(end, start + MAX_HUNK_LINES)]:
        cut = len(line) - MAX_LINE_CHARS
        shown.append(f"    | {line[:MAX_LINE_CHARS]}" + (f" ... [+{cut} chars]" if cut > 0 else ""))
    if end - start > MAX_HUNK_LINES:
        shown.append(f"    | ... and {end - start - MAX_HUNK_LINES} more line(s)")
    if not shown:
        shown.append("    (no line: this side removed them)")
    return shown


def hunk_ranges(merge: PathMerge, branch: str, head: str) -> list[str]:
    """Each overlapping hunk of *merge* as its line range on both sides, for an answer naming how it was settled."""
    return [
        f"'{branch}' {_range(region.ours_start, region.ours_end)} / '{head}' {_range(region.theirs_start, region.theirs_end)}"
        for region in merge.regions
    ]


def _range(start: int, end: int) -> str:
    if end <= start:
        return f"nothing, after line {start}"
    return f"line {start + 1}" if end - start == 1 else f"lines {start + 1}-{end}"


def describe_conflicts(merges: list[PathMerge], branch: str, head: str) -> str:
    """Each conflicting path with its hunks: the line range on both sides and both texts, bounded."""
    lines: list[str] = []
    for merge in merges:
        if not merge.regions:
            lines.append(f"- {merge.path}: {merge.reason}.")
            continue
        count = len(merge.regions)
        why = f" ({merge.reason})" if merge.reason else ""
        lines.append(f"- {merge.path}: {count} overlapping hunk{'s' if count != 1 else ''}{why}")
        for number, region in enumerate(merge.regions, 1):
            lines.append(
                f"  {number}. '{branch}' {_range(region.ours_start, region.ours_end)}, "
                f"'{head}' {_range(region.theirs_start, region.theirs_end)}:"
            )
            lines.append(f"    '{branch}':")
            lines += _shown(merge.ours, region.ours_start, region.ours_end)
            lines.append(f"    '{head}':")
            lines += _shown(merge.theirs, region.theirs_start, region.theirs_end)
    text = "\n".join(lines)
    if len(text) > MAX_DESCRIPTION_CHARS:
        text = text[:MAX_DESCRIPTION_CHARS] + f"\n... [hunks cut: {len(text) - MAX_DESCRIPTION_CHARS} more characters]"
    return text
