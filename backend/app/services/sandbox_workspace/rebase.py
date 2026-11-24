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

"""Rebase a materialized checkout onto a new ref with a three-way merge.

A checkout written by ``materialize_repository`` has no ``.git``: when the
branch it came from moves, checking it out again overwrites the agent's
edits. :func:`plan` and :func:`merge_text` are the ``git rebase`` of a
checkout, computed on the server from three trees: the files as recorded
(the base: the record's sha256), the workspace (ours) and the tree at the
new ref (theirs).

The base is the merge base of the recorded commit and the ref when the
provider names it: a checkout on a feature branch records the
branch's own files, and a file the branch adds is then an add of the
workspace side (kept, listed), not a file the ref deleted.

Per file (:func:`plan`): a side that did not change takes the other; a file
changed on both sides to the same bytes is taken once; text changed on both
sides is merged line by line (:func:`merge_text`), overlapping hunks written
between ``<<<<<<<`` / ``=======`` / ``>>>>>>>`` markers, as git writes them,
unless the three versions are JSON objects whose keys merge cleanly
(:mod:`app.services.sandbox_workspace.jsonmerge`, the i18n catalogs);
a clean line merge of plain text that writes a block more often than
either side holds it (:func:`duplicated_block`, #1171) keeps the
workspace side and is reported as a conflict;
a binary changed on both sides, and a delete against a change, keep the
workspace side and are reported as conflicts. The record then moves to the
new ref (:func:`rebased_record`), so ``sandbox_checkout_status`` and
``create_github_commit(from_checkout=...)`` show only the agent's own
changes on top of it.
"""

from __future__ import annotations

import dataclasses
import difflib
import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from app.services.sandbox_workspace import structured
from app.services.sandbox_workspace.checkout import CheckoutRecord, RecordedFile
from app.services.sandbox_workspace.jsonmerge import conflicting_keys, merge_json_objects
from app.services.sandbox_workspace.reading import is_text

#: The labels of the conflict markers: the workspace side, then the ref's.
OURS_LABEL = "workspace"
THEIRS_LABEL = "ref"


@dataclass(frozen=True)
class ConflictRegion:
    """Where one conflicting hunk sits in each side: 0-based line indexes, the end excluded."""

    ours_start: int
    ours_end: int
    theirs_start: int
    theirs_end: int


@dataclass(frozen=True)
class TextMerge:
    """The merged text, the number of conflicting hunks written with markers, and where each sits in the two sides."""

    text: str
    conflicts: int = 0
    regions: tuple[ConflictRegion, ...] = ()


def _hunks(base: list[str], side: list[str]) -> list[tuple[int, int, int, int]]:
    """The changed regions of *side* against *base*: ``(base_start, base_end, side_start, side_end)``."""
    matcher = difflib.SequenceMatcher(None, base, side, autojunk=False)
    return [(i1, i2, j1, j2) for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != "equal"]


def _added(base: list[str], side: list[str]) -> list[str]:
    """The lines *side* inserted or rewrote against *base*, in order (the lines it kept are left out)."""
    matcher = difflib.SequenceMatcher(None, base, side, autojunk=False)
    return [line for tag, _i1, _i2, j1, j2 in matcher.get_opcodes() if tag in ("insert", "replace") for line in side[j1:j2]]


def _subsumes(base: list[str], wide: list[str], narrow: list[str]) -> bool:
    """Whether *wide*'s change of *base* carries every change *narrow* made to it (#12377).

    True when each base line *narrow* removed or rewrote is gone from *wide* too, and the lines
    *narrow* inserted or rewrote are written by *wide*, in the same order. The case of a
    workspace that kept the lines a commit made from it already pushed and added more next to
    them: the ref's hunk is inside the workspace's, so the workspace's side is the merge, not a
    conflict of the workspace against its own lines.
    """
    if not narrow and not wide:
        return True

    def removed(side: list[str], tags: tuple[str, ...] = ("delete", "replace")) -> set[int]:
        matcher = difflib.SequenceMatcher(None, base, side, autojunk=False)
        return {i for tag, i1, i2, _j1, _j2 in matcher.get_opcodes() if tag in tags for i in range(i1, i2)}

    if not removed(narrow) <= removed(wide):
        return False
    wanted = _added(base, narrow)
    if not wanted:
        # A side that only deleted lines is carried only by a side that deleted them too, never by one
        # that rewrote them: a rewrite against a delete stays a conflict.
        return removed(narrow) <= removed(wide, ("delete",))
    written = iter(_added(base, wide))
    return all(any(line == got for got in written) for line in wanted)


def _union(ours: list[str], theirs: list[str]) -> list[str]:
    """Both sides of one overlapping hunk without markers: our whole hunk, then theirs whole (git's union merge).

    Nothing is deduplicated or dropped (#12179): a blank line, a closing brace or a ``with`` body both
    hunks hold is written in each, so neither side's hunk is ever cut short. A line written twice is for
    the caller to settle; a resolved file that no longer parses is refused before it lands
    (:func:`app.services.sandbox_workspace.structured.resolution_defect`).
    """
    out = [*ours, *theirs]
    # A side's last line without a newline is never glued to the next one.
    return [line if line.endswith(("\n", "\r")) else line + "\n" for line in out[:-1]] + out[-1:]


def _with_newline(lines: list[str]) -> list[str]:
    if lines and not lines[-1].endswith(("\n", "\r")):
        return [*lines[:-1], lines[-1] + "\n"]
    return lines


#: The line opening a conflict hunk the rebase writes, and the line closing it.
MARKER_OPEN = "<<<<<<< "
MARKER_CLOSE = ">>>>>>> "


def has_conflict_markers(data: bytes) -> bool:
    """Whether *data* holds an unsettled conflict hunk: a line opening one and a later line closing it."""
    opening, closing = MARKER_OPEN.encode(), MARKER_CLOSE.encode()
    if opening not in data:
        return False
    opened = False
    for line in data.splitlines():
        if line.startswith(opening):
            opened = True
        elif opened and line.startswith(closing):
            return True
    return False


def _is_crlf(text: str) -> bool:
    lf = text.count("\n")
    return lf > 0 and text.count("\r\n") == lf


def merge_text(
    base: str,
    ours: str,
    theirs: str,
    *,
    ours_label: str = OURS_LABEL,
    theirs_label: str = THEIRS_LABEL,
    both: bool = False,
    lead: str = "",
    prefer: str = "",
) -> TextMerge:
    """The three-way merge of *ours* and *theirs* from *base*, line by line (diff3).

    The changes of each side against *base* are grouped where their base
    ranges overlap or touch (git conflicts on adjacent edits too). A group
    changed by one side takes that side; changed by both to the same lines,
    those lines once; otherwise both versions between conflict markers, or,
    with *both*, our whole hunk then theirs whole, without markers (git's
    union merge, nothing deduplicated).

    *lead* (``"theirs"`` or ``"ours"``) writes an overlapping hunk without
    markers as the leading side's lines, then only the lines the other side
    added against *base* in that hunk: a line the workspace added next to a
    line the ref rewrote becomes the ref's line, then the workspace's line,
    never the old line twice.

    *prefer* (``"ours"`` or ``"theirs"``) writes an overlapping hunk as that
    side's lines alone, without markers: the ``keep`` resolution of a merge,
    which settles the overlapping hunks only and keeps every non-overlapping
    hunk of both sides.

    When the three texts do not share one line-ending style (a CRLF side
    against an LF one), the merge runs on LF endings, so a change of endings
    alone is never a conflict, and the result takes *ours*' style back.
    """
    styles = {_is_crlf(text) for text in (base, ours, theirs) if "\n" in text}
    if len(styles) > 1:
        merged = merge_text(
            base.replace("\r\n", "\n"),
            ours.replace("\r\n", "\n"),
            theirs.replace("\r\n", "\n"),
            ours_label=ours_label,
            theirs_label=theirs_label,
            both=both,
            lead=lead,
            prefer=prefer,
        )
        if not _is_crlf(ours):
            return merged
        return TextMerge(merged.text.replace("\n", "\r\n"), merged.conflicts, merged.regions)
    if ours == theirs or base == theirs:
        return TextMerge(ours)
    if base == ours:
        return TextMerge(theirs)
    b = base.splitlines(keepends=True)
    sides = (ours.splitlines(keepends=True), theirs.splitlines(keepends=True))
    changes = sorted(
        [(hunk, 0) for hunk in _hunks(b, sides[0])] + [(hunk, 1) for hunk in _hunks(b, sides[1])],
        key=lambda item: (item[0][0], item[0][1], item[1]),
    )
    out: list[str] = []
    conflicts = 0
    spans: list[ConflictRegion] = []
    pos = 0
    i = 0
    while i < len(changes):
        group = [changes[i]]
        lo, hi = changes[i][0][0], changes[i][0][1]
        i += 1
        while i < len(changes) and changes[i][0][0] <= hi:
            hi = max(hi, changes[i][0][1])
            group.append(changes[i])
            i += 1
        out.extend(b[pos:lo])
        regions: list[list[str] | None] = []
        bounds: list[tuple[int, int]] = []
        for index, lines in enumerate(sides):
            mine = [hunk for hunk, side in group if side == index]
            if not mine:
                regions.append(None)
                bounds.append((0, 0))
                continue
            first, last = mine[0], mine[-1]
            start, end = first[2] - (first[0] - lo), last[3] + (hi - last[1])
            regions.append(lines[start:end])
            bounds.append((start, end))
        ours_region, theirs_region = regions
        if ours_region is None:
            out.extend(theirs_region or [])
        elif theirs_region is None or ours_region == theirs_region:
            out.extend(ours_region)
        elif _subsumes(b[lo:hi], ours_region, theirs_region):
            # The workspace's hunk is the ref's plus more lines (its own pushed lines kept, more added).
            out.extend(ours_region)
        elif _subsumes(b[lo:hi], theirs_region, ours_region):
            out.extend(theirs_region)
        else:
            conflicts += 1
            spans.append(ConflictRegion(bounds[0][0], bounds[0][1], bounds[1][0], bounds[1][1]))
            out = _with_newline(out) if out and pos < lo else out
            if prefer in ("ours", "theirs"):
                kept = ours_region if prefer == "ours" else theirs_region
                out.extend(_with_newline(kept) if hi < len(b) else kept)
            elif lead in ("ours", "theirs"):
                leading, other = (theirs_region, ours_region) if lead == "theirs" else (ours_region, theirs_region)
                added = _added(b[lo:hi], other)
                out.extend(_with_newline(leading) if added else leading)
                out.extend(_with_newline(added) if added and hi < len(b) else added)
            elif both:
                united = _union(ours_region, theirs_region)
                out.extend(_with_newline(united) if hi < len(b) else united)
            else:
                out.append(f"<<<<<<< {ours_label}\n")
                out.extend(_with_newline(ours_region))
                out.append("=======\n")
                out.extend(_with_newline(theirs_region))
                out.append(f">>>>>>> {theirs_label}\n")
        pos = hi
    out.extend(b[pos:])
    return TextMerge("".join(out), conflicts, tuple(spans))


class Step(StrEnum):
    """What the rebase does to one file of the checkout."""

    #: Nothing: unchanged on both sides, or the ref did not change what the workspace changed.
    KEEP = "keep"
    #: The workspace did not change it: the ref's version is written (or added).
    TAKE = "take"
    #: The workspace did not change it and the ref deleted it: it is removed.
    REMOVE = "remove"
    #: Changed on both sides: merged by :func:`merge_file` (needs the base and the workspace bytes).
    MERGE = "merge"
    #: A delete against a change, or an add on both sides of a binary: the workspace side stays, reported.
    CONFLICT = "conflict"


@dataclass(frozen=True)
class FileStep:
    path: str
    step: Step
    #: Why a CONFLICT is one (said in the answer); empty otherwise.
    reason: str = ""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def base_files_of(
    record: CheckoutRecord, base_files: Mapping[str, bytes], *, checked_out: CheckoutRecord | None = None
) -> dict[str, RecordedFile]:
    """The record's files with the merge base's in place of those under its sub_paths.

    *checked_out* is the record before the ref widened its sub_paths: a path only the widened
    scope covers was never in the workspace, so it gets no base entry; with one, :func:`plan`
    would read it as a file the workspace removed and drop the ref's file instead of taking it.
    """
    before = checked_out or record

    def new_in_scope(path: str) -> bool:
        return record.covers(path) and not before.covers(path) and not before.tracks(path)

    return {
        **{path: entry for path, entry in record.files.items() if not record.covers(path)},
        **{
            path: RecordedFile(sha256=hashlib.sha256(data).hexdigest(), size=len(data))
            for path, data in base_files.items()
            if not new_in_scope(path)
        },
    }


def plan(
    record: CheckoutRecord,
    ours: Mapping[str, str],
    theirs: Mapping[str, bytes],
    *,
    in_scope: Callable[[str], bool] | None = None,
    checked_out: Mapping[str, RecordedFile] | None = None,
) -> list[FileStep]:
    """The step of every file of the base, the workspace and the ref, sorted by path.

    *ours* maps each repository-relative file the checkout tracks in the
    workspace to its sha256; *theirs* is the tree at the new ref (same
    ``sub_paths``), repository-relative path to bytes. *in_scope* says
    which paths the tree at the ref can hold: a file outside it (committed
    from the checkout outside its ``sub_paths``) is left as it is, never
    removed for being absent from the ref's tree. Only the files that need
    something are listed: a KEEP is listed only when the workspace changed
    the file, so the answer can name what stayed the agent's.

    *checked_out* maps each file to what the checkout wrote (the record
    before :func:`base_files_of` put the merge base in its place). A file the
    ref holds exactly as it was checked out brought nothing the workspace
    lacks: the workspace side stays (KEEP), never a merge against the base.
    That is the squash of the branch the checkout is on (#779): a file the
    branch added is absent from the merge base, the squash on the ref carries
    it as the branch had it, and a merge from an empty base would conflict on
    the whole file the workspace edited since.
    """
    steps: list[FileStep] = []
    for path in sorted(set(record.files) | set(ours) | set(theirs)):
        if path not in theirs and in_scope is not None and not in_scope(path):
            continue
        base = record.files.get(path)
        mine = ours.get(path)
        new = theirs.get(path)
        new_sha = _sha(new) if new is not None else None
        base_sha = base.sha256 if base is not None else None
        if mine == base_sha:
            # The workspace did not touch it: follow the ref.
            if new_sha == base_sha:
                continue
            steps.append(FileStep(path, Step.TAKE if new is not None else Step.REMOVE))
            continue
        # The workspace changed it (modified, added or removed).
        at_checkout = checked_out.get(path) if checked_out is not None else None
        if new_sha in (base_sha, mine) or (
            new_sha is not None and at_checkout is not None and new_sha == at_checkout.sha256
        ):
            steps.append(FileStep(path, Step.KEEP))
        elif mine is None:
            steps.append(FileStep(path, Step.CONFLICT, "removed in the workspace, changed on the ref"))
        elif new is None:
            steps.append(FileStep(path, Step.CONFLICT, "changed in the workspace, removed on the ref"))
        else:
            steps.append(FileStep(path, Step.MERGE))
    return steps


def stale_branch_sides(
    recorded: Mapping[str, RecordedFile],
    ours: Mapping[str, str],
    branch_tree: Mapping[str, bytes],
    *,
    in_scope: Callable[[str], bool],
) -> tuple[dict[str, bytes], list[str]]:
    """The workspace files that are older than the branch commit the record names (#1029).

    A ``from_checkout`` commit moves the record to the branch commit but keeps
    the hash of every file it did not write, so the workspace still holds them
    as first checked out. When the branch gained commits the checkout never
    held (main merged into it on the server, a split commit set), those bytes
    are not the branch's: taken as the workspace side of a rebase, a file the
    branch never changed read as "changed in the workspace" and was kept over
    the ref, and a file both sides changed lost what the branch had gained.

    *recorded* maps each path to the hash the record holds, *ours* the
    workspace hashes, *branch_tree* the tree at the record's commit (read at
    the paths *in_scope* says it can hold). Returns ``(path -> the branch's
    bytes, paths the branch removed)`` for every file the workspace did not
    edit since the record (its hash is still the recorded one) whose branch
    version differs. An edited file is left alone: its edit is the workspace side.
    """
    refreshed: dict[str, bytes] = {}
    gone: list[str] = []
    for path, entry in recorded.items():
        if ours.get(path) != entry.sha256:
            continue
        data = branch_tree.get(path)
        if data is None:
            if in_scope(path):
                gone.append(path)
            continue
        if _sha(data) != entry.sha256:
            refreshed[path] = data
    return refreshed, sorted(gone)


@dataclass(frozen=True)
class FileMerge:
    """The bytes to write for a MERGE step, and whether they hold conflict markers or kept the workspace side."""

    data: bytes
    conflicts: int = 0
    #: Set when the merge could not run (a binary): the workspace bytes are kept.
    reason: str = ""
    #: The structured format the written bytes were parsed as (``JSON``, ``YAML``, ``TOML``), '' when not judged.
    validated: str = ""
    #: Why the markers were written where a merge without markers was asked for (said in the answer).
    detail: str = ""


class OnConflict(StrEnum):
    """How a rebase writes a hunk both sides changed differently."""

    #: Both versions between conflict markers, settled by hand (git's default).
    MARKERS = "markers"
    #: The ref's lines, then the lines the workspace added in the hunk, no marker (a line added next to a
    #: line the ref rewrote).
    REF_THEN_WORKSPACE = "ref-then-workspace"
    #: The workspace's lines, then the lines the ref added in the hunk, no marker.
    WORKSPACE_THEN_REF = "workspace-then-ref"
    #: The ref's lines alone, no marker: the workspace's side of the overlapping hunk is dropped; every
    #: hunk only one side changed is still merged.
    REF = "ref"
    #: The workspace's lines alone, no marker; every hunk only one side changed is still merged.
    WORKSPACE = "workspace"


#: Consecutive substantive lines a run must hold before a copy of it written by the merge is reported (#1171).
DUPLICATE_RUN_LINES = 3


def _substantive(line: str) -> bool:
    """A line that says something: not blank, and holding a letter or a digit (a lone brace or ``)`` is not one)."""
    return any(char.isalnum() for char in line)


def _runs(lines: list[str]) -> dict[tuple[str, ...], list[int]]:
    """Each run of :data:`DUPLICATE_RUN_LINES` consecutive substantive lines, with the 0-based index of each start."""
    size = DUPLICATE_RUN_LINES
    found: dict[tuple[str, ...], list[int]] = {}
    for start in range(len(lines) - size + 1):
        window = tuple(line.rstrip() for line in lines[start : start + size])
        if all(_substantive(line) for line in window):
            found.setdefault(window, []).append(start)
    return found


def duplicated_block(ours: str, theirs: str, merged: str) -> int:
    """The 1-based line where *merged* writes a block more often than either side holds it, 0 when none (#1171).

    A line merge joins hunks that do not overlap: when the workspace and the ref each add the same block
    at different places (a branch carrying a change the ref also squashed), both copies are written and
    the merge reads clean. A run of :data:`DUPLICATE_RUN_LINES` substantive lines found more times in
    *merged* than in *ours* and in *theirs* is that copy; the line answered is the start of the first
    extra one. A run neither side holds is not a copy: it is where two merged hunks meet.
    """
    held = [_runs(text.splitlines()) for text in (ours, theirs)]
    first = 0
    for window, starts in _runs(merged.splitlines()).items():
        most = max(len(side.get(window, ())) for side in held)
        if most and len(starts) > most:
            line = starts[most] + 1
            first = line if not first else min(first, line)
    return first


def _keys_note(kind: str, texts: list[str]) -> str:
    if kind != structured.JSON:
        return ""
    keys = conflicting_keys(*texts)
    if not keys:
        return ""
    shown = ", ".join(f"'{key}'" for key in keys[:MAX_LISTED])
    more = f" and {len(keys) - MAX_LISTED} more" if len(keys) > MAX_LISTED else ""
    return f"; changed differently on both sides: {shown}{more}"


def merge_file(
    base: bytes | None,
    ours: bytes,
    theirs: bytes,
    *,
    on_conflict: OnConflict = OnConflict.MARKERS,
    path: str = "",
) -> FileMerge:
    """The three-way merge of one file's bytes; *base* is ``None`` for a file added on both sides.

    Text is merged by :func:`merge_text` on its UTF-8 decoding; a binary, or
    a file that does not decode, keeps the workspace bytes and is reported.
    With *on_conflict* other than MARKERS, an overlapping hunk is written as
    the leading side's lines then the other side's added lines (REF and
    WORKSPACE: that side's lines alone), without markers, and counts as no
    conflict.

    A structured file (:mod:`.structured`: JSON, YAML, TOML by *path*'s
    extension, JSON by content) whose two sides parse is parsed again once
    merged (#11986): a clean line merge that no longer parses or writes a key
    twice is merged key by key when that is certain, else the workspace bytes
    are kept and the reason named; a marker-free merge (*on_conflict*) that
    breaks it is written with markers instead, the keys both sides changed
    named in :attr:`FileMerge.detail`. "Merged cleanly" is never a broken file.
    """
    sides = [base or b"", ours, theirs]
    if not all(is_text(data) for data in sides if data):
        if on_conflict is OnConflict.REF:
            return FileMerge(theirs)
        if on_conflict is OnConflict.WORKSPACE:
            return FileMerge(ours)
        return FileMerge(ours, 1, "binary changed on both sides: the workspace version is kept")
    try:
        texts = [data.decode("utf-8") for data in sides]
    except UnicodeDecodeError:
        return FileMerge(ours, 1, "not UTF-8 text: the workspace version is kept")
    _base_text, ours_text, theirs_text = texts

    def judged(text: str) -> tuple[str, str]:
        return structured.merge_defect(path, ours_text, theirs_text, text)

    merged = merge_text(*texts)
    if not merged.conflicts:
        kind, defect = judged(merged.text)
        # A structured file is judged by its parser (a key written twice) and merged key by key below.
        twice = 0 if kind or defect else duplicated_block(ours_text, theirs_text, merged.text)
        if twice:
            return FileMerge(
                ours,
                1,
                f"the line merge has no overlap but writes a block twice (the copy starts at line {twice} of the "
                "merge): the workspace version is kept",
            )
        if not defect:
            return FileMerge(merged.text.encode("utf-8"), validated=kind)
    by_key = merge_json_objects(*texts)
    if by_key is not None:
        kind, defect_by_key = judged(by_key)
        if not defect_by_key:
            return FileMerge(by_key.encode("utf-8"), validated=kind)
    if not merged.conflicts:
        # A line merge without an overlap that breaks the file: neither side's lines can be trusted to join.
        why = f"the line merge has no overlap but the merged {kind} file is broken ({defect}){_keys_note(kind, texts)}"
        return FileMerge(ours, 1, f"{why}: the workspace version is kept")
    if on_conflict in (OnConflict.REF, OnConflict.WORKSPACE):
        prefer = "theirs" if on_conflict is OnConflict.REF else "ours"
        kept = merge_text(*texts, prefer=prefer).text
        kind, defect = judged(kept)
        if not defect:
            return FileMerge(kept.encode("utf-8"), validated=kind)
        detail = f"the {on_conflict.value} merge would break the {kind} file ({defect}){_keys_note(kind, texts)}"
        return FileMerge(merged.text.encode("utf-8"), merged.conflicts, detail=detail)
    if on_conflict is not OnConflict.MARKERS:
        lead = "theirs" if on_conflict is OnConflict.REF_THEN_WORKSPACE else "ours"
        led = merge_text(*texts, lead=lead).text
        kind, defect = judged(led)
        if not defect:
            return FileMerge(led.encode("utf-8"), validated=kind)
        detail = f"the {on_conflict.value} merge would break the {kind} file ({defect}){_keys_note(kind, texts)}"
        return FileMerge(merged.text.encode("utf-8"), merged.conflicts, detail=detail)
    return FileMerge(merged.text.encode("utf-8"), merged.conflicts)


def rebased_record(
    record: CheckoutRecord,
    *,
    ref: str,
    commit: str,
    theirs: Mapping[str, bytes],
    in_scope: Callable[[str], bool] | None = None,
) -> CheckoutRecord:
    """*record* moved to *commit* on *ref*: its files are the ref's tree, as a rebase moves the base.

    A recorded file outside *in_scope* (see :func:`plan`) keeps its entry.
    """
    files = {path: RecordedFile(sha256=_sha(data), size=len(data)) for path, data in theirs.items()}
    if in_scope is not None:
        for path, recorded in record.files.items():
            if path not in files and not in_scope(path):
                files[path] = recorded
    # The out-of-scope baseline is carried: a rebase never hides an edit made outside the scope (#10144).
    # The branch's baseline moves to the ref: the committed change is in the workspace again, as a change.
    return dataclasses.replace(record.carried(ref=ref, commit=commit, files=files), base_commit="", branch={})


#: Paths each list of the answer names before counting the rest.
MAX_LISTED = 20
#: Files a branch changes outside a narrow checkout that a rebase reads in one call (#11853); past it, refused.
MAX_PULLED = 200


@dataclass(frozen=True)
class BranchChanges:
    """The files the checkout's commit changes since its merge base with the ref (the provider's compare).

    *changed* holds the paths whose branch version exists (added, modified, renamed to); *removed* the
    paths the branch deletes (a rename's old name included); *capped* that the provider stopped listing.
    """

    changed: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    capped: bool = False

    def outside(self, record: CheckoutRecord) -> BranchChanges:
        """Only the paths *record* does not track: what a rebase of the narrow tree would otherwise miss."""
        return BranchChanges(
            changed=tuple(p for p in self.changed if not record.tracks(p)),
            removed=tuple(p for p in self.removed if not record.tracks(p)),
            capped=self.capped,
        )

    @property
    def paths(self) -> tuple[str, ...]:
        return (*self.changed, *self.removed)


def branch_changes(structured: Mapping[str, object]) -> BranchChanges | None:
    """The :class:`BranchChanges` a compare's ``structured`` carries, or ``None`` when it lists no ``files``."""
    raw = structured.get("files")
    if not isinstance(raw, list):
        return None
    changed: list[str] = []
    removed: list[str] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("filename") or "").strip().strip("/")
        if not name:
            continue
        status = str(item.get("status") or "")
        previous = str(item.get("previous_filename") or "").strip().strip("/")
        if status == "removed":
            removed.append(name)
            continue
        changed.append(name)
        if previous and previous != name:
            removed.append(previous)
    return BranchChanges(
        changed=tuple(dict.fromkeys(changed)),
        removed=tuple(p for p in dict.fromkeys(removed) if p not in changed),
        capped=bool(structured.get("files_capped")),
    )


def _listed(paths: list[str]) -> str:
    shown = ", ".join(paths[:MAX_LISTED])
    return shown + (f", +{len(paths) - MAX_LISTED} more" if len(paths) > MAX_LISTED else "")


@dataclass
class RebaseSummary:
    """What the rebase did, repository-relative: the answer's lists."""

    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    #: Files the workspace side adds (absent from the base and the ref): kept, never removed.
    added: list[str] = field(default_factory=list)
    merged: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    #: (path, why): conflict markers written, or the workspace side kept.
    conflicts: list[tuple[str, str]] = field(default_factory=list)
    #: Files the branch changes outside the checkout's sub_paths, brought into it and recorded (#11853).
    pulled: list[str] = field(default_factory=list)
    #: (path, format): merged files parsed after the merge (JSON, YAML, TOML), no parse error, no duplicate key.
    validated: list[tuple[str, str]] = field(default_factory=list)
    #: Files the workspace held older than the record's branch commit, brought to it before the merge (#1029).
    refreshed: list[str] = field(default_factory=list)

    def describe(self, *, shown_as: str, before: str, after: str) -> str:
        head = f"{shown_as}/: rebased from {before} onto {after}."
        lines = [head]
        if self.pulled:
            lines.append(
                f"Brought in from the branch, outside the checkout's sub_paths ({len(self.pulled)}): "
                f"{_listed(sorted(self.pulled))}. They are merged with the ref like every file of the checkout and "
                "recorded, so status, diff and from_checkout carry them and add_paths never overwrites them."
            )
        if self.refreshed:
            lines.append(
                f"Read from the branch commit first, the workspace held them as first checked out "
                f"({len(self.refreshed)}): {_listed(sorted(self.refreshed))}. The branch's version is the side "
                "merged with the ref, never the older text."
            )
        if not (self.updated or self.removed or self.added or self.merged or self.kept or self.conflicts):
            lines.append("Nothing changed on either side: the record moved, no file was written.")
        if self.updated:
            lines.append(f"Updated from the ref ({len(self.updated)}): {_listed(sorted(self.updated))}.")
        if self.removed:
            lines.append(f"Removed by the ref ({len(self.removed)}): {_listed(sorted(self.removed))}.")
        if self.merged:
            lines.append(f"Merged cleanly ({len(self.merged)}): {_listed(sorted(self.merged))}.")
        if self.validated:
            shown = _listed([f"{path} ({kind})" for path, kind in sorted(self.validated)])
            lines.append(f"Parsed after the merge, valid with no duplicate key ({len(self.validated)}): {shown}.")
        if self.added:
            lines.append(f"Your added files kept ({len(self.added)}): {_listed(sorted(self.added))}.")
        if self.kept:
            lines.append(f"Your changes kept as they were ({len(self.kept)}): {_listed(sorted(self.kept))}.")
        if self.conflicts:
            shown = "; ".join(f"{path} ({why})" for path, why in sorted(self.conflicts)[:MAX_LISTED])
            more = f"; +{len(self.conflicts) - MAX_LISTED} more" if len(self.conflicts) > MAX_LISTED else ""
            lines.append(
                f"In conflict ({len(self.conflicts)}): {shown}{more}. Resolve each one with sandbox_edit_file "
                f"(the markers read <<<<<<< {OURS_LABEL}, =======, >>>>>>> {THEIRS_LABEL}), then read "
                "sandbox_checkout_status before committing."
            )
        lines.append(
            "The checkout record now names the new ref: sandbox_checkout_status and "
            "create_github_commit(from_checkout=...) show only your changes on top of it."
        )
        return "\n".join(lines) + "\n"
