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

"""Apply a unified diff to the files of a checkout: all or nothing, fuzz 0.

Pure: no I/O. ``sandbox_apply_patch`` (``builtin_tools/sandbox_workspace.py``)
reads a diff the workspace holds (a pull request's diff written by
``get_github_pr_diff(to_workspace_path)``, a compare, a saved ``git diff``),
parses it here (:func:`parse`), applies every file's hunks to the checkout's
current text (:func:`apply_file`) and writes the whole change set in ONE
workspace commit only when every hunk applied: re-landing a pull request on a
fresh branch, porting a change to another base, without a code_interpreter
hunk applier rewriting each source file.

A hunk applies when its context and removed lines are found exactly (fuzz 0:
no line of context is ever dropped). It may sit at an offset from the line
its header states (the file moved since the diff was taken): the nearest
exact match at or after the end of the previous hunk is taken and the offset
reported. A CRLF file matches a patch written with LF endings, and the lines
the patch adds take the file's endings. Anything else is a stale hunk, named
with its file, its header and the first line that differs.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field

#: Files one patch changes, at most.
MAX_FILES = 500
#: Stale hunks a refusal names, at most (the rest counted).
MAX_REPORTED_FAILURES = 20
#: Characters of a line quoted in a refusal.
QUOTE_CHARS = 120


class PatchError(ValueError):
    """A patch that cannot be read or applied; ``str()`` says which and why, for the model."""


@dataclass
class Hunk:
    header: str
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    #: (tag, text): tag is " " (context), "-" (removed) or "+" (added); text without its line ending.
    lines: list[tuple[str, str]] = field(default_factory=list)
    #: The old side's last line has no newline at the end of the file.
    old_no_newline: bool = False
    #: The new side's last line has no newline at the end of the file.
    new_no_newline: bool = False

    @property
    def old_lines(self) -> list[str]:
        return [text for tag, text in self.lines if tag != "+"]

    @property
    def new_lines(self) -> list[str]:
        return [text for tag, text in self.lines if tag != "-"]


@dataclass
class FilePatch:
    #: Repository-relative paths; ``None`` on the side of a new (old_path) or deleted (new_path) file.
    old_path: str | None
    new_path: str | None
    hunks: list[Hunk] = field(default_factory=list)
    binary: bool = False
    #: A mode change only (``old mode`` / ``new mode``): nothing to write.
    mode_only: bool = False

    @property
    def path(self) -> str:
        """The path the patch is known by: the new one, else the old one (a deletion)."""
        return self.new_path or self.old_path or ""

    @property
    def kind(self) -> str:
        if self.old_path is None:
            return "added"
        if self.new_path is None:
            return "deleted"
        if self.old_path != self.new_path:
            return "renamed"
        return "modified"

    def stat(self) -> tuple[int, int]:
        added = sum(1 for h in self.hunks for tag, _ in h.lines if tag == "+")
        removed = sum(1 for h in self.hunks for tag, _ in h.lines if tag == "-")
        return added, removed


@dataclass
class Applied:
    text: str
    #: (hunk header, offset in lines from the stated position), one per hunk.
    offsets: list[tuple[str, int]]


@dataclass
class Failure:
    path: str
    header: str
    message: str


_C_ESCAPES = {"n": 10, "t": 9, "r": 13, '"': 34, "\\": 92, "a": 7, "b": 8, "f": 12, "v": 11}


def _unquote(quoted: str) -> str:
    """A path git quoted as a C string (special characters and non-ASCII bytes octal-escaped), decoded."""
    out = bytearray()
    i = 0
    while i < len(quoted):
        char = quoted[i]
        if char == "\\" and i + 1 < len(quoted):
            nxt = quoted[i + 1]
            if quoted[i + 1 : i + 4].isdigit() and len(quoted[i + 1 : i + 4]) == 3:
                out.append(int(quoted[i + 1 : i + 4], 8) & 0xFF)
                i += 4
                continue
            out.append(_C_ESCAPES.get(nxt, ord(nxt) if nxt.isascii() else 63))
            i += 2
            continue
        out.extend(char.encode("utf-8"))
        i += 1
    return out.decode("utf-8", "replace")


def _strip_prefix(raw: str) -> str | None:
    """The repository path of a ``---`` / ``+++`` / ``diff --git`` name, ``None`` for /dev/null."""
    name = raw.split("\t", 1)[0].rstrip("\r")
    if len(name) >= 2 and name.startswith('"') and name.endswith('"'):
        name = _unquote(name[1:-1])
    name = name.strip()
    if name == "/dev/null":
        return None
    if name.startswith(("a/", "b/")):
        name = name[2:]
    return name


def _git_names(rest: str) -> tuple[str | None, str | None]:
    """The two paths of a ``diff --git a/X b/Y`` line (the same path twice splits at its middle)."""
    rest = rest.rstrip("\r")
    if len(rest) % 2 == 1:
        half = len(rest) // 2
        left, right = rest[:half], rest[half + 1 :]
        if left[2:] == right[2:] and left.startswith("a/") and right.startswith("b/"):
            return _strip_prefix(left), _strip_prefix(right)
    cut = rest.find(" b/")
    if cut < 0:
        return None, None
    return _strip_prefix(rest[:cut]), _strip_prefix(rest[cut + 1 :])


def _hunk_header(line: str) -> Hunk:
    try:
        ranges = line[3 : line.index(" @@", 3)].split()
        old, new = ranges[0], ranges[1]
        if not old.startswith("-") or not new.startswith("+"):
            raise ValueError(line)

        def _range(text: str) -> tuple[int, int]:
            start, _, length = text[1:].partition(",")
            return int(start), int(length) if length else 1

        (old_start, old_len), (new_start, new_len) = _range(old), _range(new)
    except (ValueError, IndexError) as exc:
        raise PatchError(f"unreadable hunk header {line[:QUOTE_CHARS]!r}") from exc
    return Hunk(
        header=line.split(" @@", 1)[0] + " @@",
        old_start=old_start,
        old_len=old_len,
        new_start=new_start,
        new_len=new_len,
    )


def parse(text: str) -> list[FilePatch]:
    """The file patches of a git or plain unified diff, or :class:`PatchError` on a malformed one."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    files: list[FilePatch] = []
    current: FilePatch | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            old, new = _git_names(line[len("diff --git ") :])
            current = FilePatch(old_path=old, new_path=new)
            files.append(current)
        elif current is not None and line.startswith("new file mode"):
            current.old_path = None
        elif current is not None and line.startswith("deleted file mode"):
            current.new_path = None
        elif current is not None and line.startswith("rename from "):
            current.old_path = line[len("rename from ") :].rstrip("\r")
        elif current is not None and line.startswith("rename to "):
            current.new_path = line[len("rename to ") :].rstrip("\r")
        elif current is not None and (line.startswith("Binary files ") or line.startswith("GIT binary patch")):
            current.binary = True
        elif line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            old, new = _strip_prefix(line[4:]), _strip_prefix(lines[i + 1][4:])
            if current is None or current.hunks:
                current = FilePatch(old_path=old, new_path=new)
                files.append(current)
            else:
                current.old_path, current.new_path = old, new
            i += 1
        elif line.startswith("@@ "):
            if current is None:
                raise PatchError(f"line {i + 1}: a hunk before any file header ('--- ' / '+++ ' or 'diff --git')")
            hunk = _hunk_header(line)
            old_left, new_left = hunk.old_len, hunk.new_len
            i += 1
            while i < len(lines) and (old_left > 0 or new_left > 0 or lines[i].startswith("\\")):
                body = lines[i]
                if body.startswith("\\"):
                    last = hunk.lines[-1][0] if hunk.lines else " "
                    if last in (" ", "-"):
                        hunk.old_no_newline = True
                    if last in (" ", "+"):
                        hunk.new_no_newline = True
                elif body.startswith((" ", "-", "+")) or body == "":
                    tag = body[:1] or " "
                    hunk.lines.append((tag, body[1:]))
                    if tag != "+":
                        old_left -= 1
                    if tag != "-":
                        new_left -= 1
                    if old_left < 0 or new_left < 0:
                        raise PatchError(
                            f"line {i + 1}: hunk {hunk.header} of {current.path} holds more lines than its header counts"
                        )
                else:
                    raise PatchError(
                        f"line {i + 1}: hunk {hunk.header} of {current.path} ends before its header's count ({body[:QUOTE_CHARS]!r})"
                    )
                i += 1
            if old_left > 0 or new_left > 0:
                raise PatchError(
                    f"hunk {hunk.header} of {current.path} is cut: the patch ends before its header's count"
                )
            current.hunks.append(hunk)
            continue
        i += 1
    for patch in files:
        if (
            not patch.hunks
            and not patch.binary
            and patch.old_path is not None
            and patch.new_path is not None
            and patch.old_path == patch.new_path
        ):
            patch.mode_only = True
    if not files:
        raise PatchError(
            "no file patch found: the text is not a unified diff (no 'diff --git', '--- ' / '+++ ' or '@@' line)"
        )
    return files


def excluded(patch: FilePatch, exclude: list[str]) -> bool:
    """Whether a path or glob of *exclude* names the patch's old or new path (a directory excludes what it holds)."""
    for pattern in exclude:
        want = pattern.strip().strip("/")
        if not want:
            continue
        for path in (patch.old_path, patch.new_path):
            if path and (path == want or path.startswith(want + "/") or fnmatch.fnmatchcase(path, want)):
                return True
    return False


def check_path(path: str) -> None:
    """:class:`PatchError` on a path a patch may not write: absolute, or climbing out with ``..``."""
    if not path or path.startswith("/") or any(part == ".." for part in path.split("/")):
        raise PatchError(f"the patch names the path {path!r}, outside the checkout: nothing was written")


def _split(text: str) -> tuple[list[str], bool]:
    """The lines of *text* without their LF, and whether it ends with a newline."""
    if text == "":
        return [], True
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
        return lines, True
    return lines, False


def _same(file_line: str, patch_line: str, crlf: bool) -> bool:
    if file_line == patch_line:
        return True
    return crlf and file_line.rstrip("\r") == patch_line.rstrip("\r")


def _quote(line: str) -> str:
    return repr(line if len(line) <= QUOTE_CHARS else line[:QUOTE_CHARS] + "...")


def _matches_at(lines: list[str], at: int, old: list[str], crlf: bool) -> bool:
    if at < 0 or at + len(old) > len(lines):
        return False
    return all(_same(lines[at + k], old[k], crlf) for k in range(len(old)))


def _mismatch(lines: list[str], at: int, old: list[str], crlf: bool) -> str:
    """Where the hunk's old side differs from the file at its stated position, for the refusal."""
    for k, want in enumerate(old):
        if at + k >= len(lines):
            return f"line {at + k + 1}: the patch expects {_quote(want)}, the file ends at line {len(lines)}"
        if not _same(lines[at + k], want, crlf):
            return f"line {at + k + 1}: the patch expects {_quote(want)}, the file has {_quote(lines[at + k])}"
    return "the context is found nowhere at or after the previous hunk"


def apply_file(text: str | None, patch: FilePatch) -> Applied:
    """*patch* applied to *text* (``None`` for an absent file), or :class:`PatchError` naming the stale hunk."""
    if patch.kind == "added":
        if text is not None:
            raise PatchError("the patch adds the file, which already exists in the checkout")
        text = ""
    elif text is None:
        raise PatchError("the file is not in the checkout")
    crlf = text.count("\n") > 0 and text.count("\r\n") == text.count("\n")
    lines, final_newline = _split(text)
    out: list[str] = []
    cursor = 0
    offsets: list[tuple[str, int]] = []
    for hunk in patch.hunks:
        old = hunk.old_lines
        stated = hunk.old_start - 1 if hunk.old_len else hunk.old_start
        stated = max(stated, 0)
        at = -1
        if not old:
            at = stated if stated >= cursor and stated <= len(lines) else -1
        else:
            span = max(len(lines), 1)
            for delta in range(span + 1):
                for candidate in (stated + delta,) if delta == 0 else (stated - delta, stated + delta):
                    if candidate >= cursor and _matches_at(lines, candidate, old, crlf):
                        at = candidate
                        break
                if at >= 0:
                    break
        if at < 0:
            where = max(stated, cursor)
            raise PatchError(f"hunk {hunk.header} does not apply: {_mismatch(lines, where, old, crlf)}")
        if hunk.old_no_newline and (at + len(old) != len(lines) or (final_newline and lines)):
            raise PatchError(
                f"hunk {hunk.header} does not apply: the patch expects the file to end without a newline after line {at + len(old)}"
            )
        out.extend(lines[cursor:at])
        for tag, body in hunk.lines:
            if tag == " ":
                out.append(lines[at])
                at += 1
            elif tag == "-":
                at += 1
            else:
                out.append(body)
        cursor = at
        offsets.append((hunk.header, (at - len(old)) - stated))
    out.extend(lines[cursor:])
    if any(h.new_no_newline for h in patch.hunks):
        final_newline = False
    elif any(h.old_no_newline for h in patch.hunks) or patch.kind == "added":
        final_newline = True
    if patch.kind == "deleted" and out:
        raise PatchError(
            f"the patch deletes the file but {len(out)} line(s) of it are not in the patch: the file changed since the diff"
        )
    result = "\r\n".join(line.rstrip("\r") for line in out) if crlf else "\n".join(out)
    if out and final_newline:
        result += "\r\n" if crlf else "\n"
    return Applied(text=result, offsets=offsets)
