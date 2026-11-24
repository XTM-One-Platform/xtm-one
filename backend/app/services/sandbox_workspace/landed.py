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

"""Whether a sandbox write the interrupted segment started landed, told from the workspace.

A resumed turn's note names the calls the cut segment started and never saw
answered (``turn_checkpoint.in_flight_calls``). For the two in-place writes of
the workspace the answer is in the workspace itself: ``sandbox_write_file``
landed when the path holds the content's sha256, ``sandbox_edit_file`` landed
when each replacement is in the file and its find text is gone. The verdict is
written on the in-flight call (``landed``, ``outcome``) so the note states it
and the loop's in-flight repeat guard lets a write that did not land run on its
first repeat (``turn_checkpoint.in_flight_writes``).

A line-range or ``insert_after_line`` edit is told from the lines at its
number (the replacing or inserted block is there, or the anchor line still
holds its expected text) when it is the call's only line-numbered edit. A
move (``move_to_after_line``) writes no text to look for: it gets no verdict,
and its key names its range and target.

The same verdict is the tool's ``landed_probe`` (:func:`edit_probe`) and its
anchors the tool's ``landed_key`` (:func:`edit_key`): a REWORDED repeat of an
insertion at the same anchor (another inserted text, the find kept) misses
the call digest, so it is matched by its path and anchors and answered
"already applied" when the interrupted call landed, instead of inserting the
block a second time (#12097). An edit at another anchor has another key and
runs.

A ``replace_from`` edit is told from the text of its source file, read again
(:func:`_with_sources`); a source file gone since leaves the call untold.

Anything this module cannot tell (an argument the replay cut, a file changed
since, line-numbered edits combined in one call) is left without a verdict:
the note keeps saying the call may have taken effect.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from typing import Any

from app.core.utils import format_exception_message
from app.services.sandbox_workspace.editing import EditError, replace_from_sources, with_replace_from
from app.services.sandbox_workspace.paths import WorkspacePathError, normalize

logger = logging.getLogger(__name__)

#: The writes told apart here, by tool name.
SANDBOX_WRITES = frozenset({"sandbox_edit_file", "sandbox_write_file"})
#: What ``turn_checkpoint._cut_arguments`` appends to a string it cut.
_CUT_MARK = "[cut for the replay:"


def _cut(value: object) -> bool:
    return isinstance(value, str) and _CUT_MARK in value


def _stripped_lines(text: str) -> list[str]:
    return [line.rstrip("\r\n") for line in text.splitlines()]


def _line_edit(edit: dict[str, Any]) -> bool:
    return "insert_after_line" in edit or "start_line" in edit or "end_line" in edit


def _holds(line: str, want: object, blank: object) -> bool:
    """Whether *line* holds the expected text of a line-numbered edit (or is blank when it asks so)."""
    if blank is True:
        return not line.strip()
    return isinstance(want, str) and bool(want.strip()) and want.replace("\r\n", "\n").rstrip("\n") in line


def _line_verdict(edit: dict[str, Any], text: str) -> bool | None:
    """Whether one ``insert_after_line`` or ``start_line``/``end_line`` *edit* is applied in *text*.

    Landed when the inserted or replacing block sits at its line; not landed
    when it does not and the anchor line still holds its expected text;
    ``None`` otherwise (a deletion, a cut argument, a file changed since).
    """
    replace = edit.get("replace")
    if not isinstance(replace, str) or not replace or _cut(replace) or _cut(edit.get("expect_first")):
        return None
    block = _stripped_lines(replace)
    lines = _stripped_lines(text)
    if "insert_after_line" in edit:
        after = edit.get("insert_after_line")
        if not isinstance(after, int) or isinstance(after, bool) or after < 0 or after > len(lines):
            return None
        if lines[after : after + len(block)] == block:
            return True
        if after == 0 or _holds(lines[after - 1], edit.get("expect_first"), edit.get("expect_first_blank")):
            return False
        return None
    start = edit.get("start_line")
    if not isinstance(start, int) or isinstance(start, bool) or start < 1 or start > len(lines):
        return None
    if lines[start - 1 : start - 1 + len(block)] == block:
        return True
    if _holds(lines[start - 1], edit.get("expect_first"), edit.get("expect_first_blank")):
        return False
    return None


def edit_verdict(edits: object, text: str | None) -> bool | None:
    """Whether the *edits* of a ``sandbox_edit_file`` call are applied in *text*: ``True``, ``False`` or ``None`` (cannot tell).

    ``None`` for a file that does not exist, an argument the replay cut,
    line-numbered edits combined with others (their numbers move with the
    edits before them), or edits some of which look applied and some not.
    """
    if text is None or not isinstance(edits, list) or not edits:
        return None
    if any(isinstance(edit, dict) and _line_edit(edit) for edit in edits):
        return _line_verdict(edits[0], text) if len(edits) == 1 and isinstance(edits[0], dict) else None
    verdicts: set[bool] = set()
    for edit in edits:
        if not isinstance(edit, dict):
            return None
        find, replace = edit.get("find"), edit.get("replace")
        if not isinstance(find, str) or not find or not isinstance(replace, str) or _cut(find) or _cut(replace):
            return None
        if find in replace:
            # An insertion around the find text: the find stays either way.
            if replace in text:
                verdicts.add(True)
            elif find in text:
                verdicts.add(False)
            else:
                return None
        elif find in text:
            verdicts.add(False)
        elif not replace or replace in text:
            verdicts.add(True)
        else:
            return None
    return verdicts.pop() if len(verdicts) == 1 else None


def edit_key(arguments: Mapping[str, Any]) -> str | None:
    """The natural key of a ``sandbox_edit_file`` call: its path and the anchor of each edit, whatever the text it writes.

    The anchor is the find text of a find/replace edit, the line and its
    expected text of an ``insert_after_line`` or line-range edit. ``None``
    for a call whose path or edits cannot be read (the digest alone guards it).
    """
    try:
        path = normalize(arguments.get("path"))
    except WorkspacePathError:
        return None
    edits = arguments.get("edits")
    if not isinstance(edits, list) or not edits:
        return None
    anchors: list[str] = []
    for edit in edits:
        if not isinstance(edit, dict):
            return None
        if "insert_after_line" in edit:
            anchors.append(f"insert after {edit.get('insert_after_line')}: {edit.get('expect_first') or ''}")
        elif "move_to_after_line" in edit:
            anchors.append(
                f"move {edit.get('start_line')}-{edit.get('end_line', edit.get('start_line'))} after "
                f"{edit.get('move_to_after_line')}: {edit.get('expect_first') or ''}"
            )
        elif _line_edit(edit):
            anchors.append(
                f"lines {edit.get('start_line')}-{edit.get('end_line', edit.get('start_line'))}: "
                f"{edit.get('expect_first') or ''}"
            )
        elif isinstance(edit.get("find"), str) and edit["find"] and not _cut(edit["find"]):
            anchors.append(f"find: {edit['find']}")
        else:
            return None
    return "\n".join([path, *anchors])


#: :func:`_with_sources` could not read a ``replace_from`` file: no verdict.
_UNTOLD = object()


async def _with_sources(session: Any, edits: object) -> object:
    """*edits* with each ``replace_from`` edit's replacement read from its workspace file, as the call wrote it.

    :data:`_UNTOLD` when a source file cannot be read (absent, moved since): the call gets no verdict.
    """
    try:
        sources = replace_from_sources(edits)
    except EditError:
        return _UNTOLD
    if not sources or not isinstance(edits, list):
        return edits
    texts: dict[int, str] = {}
    for index, source in sources:
        try:
            _entry, source_data = await session.get_bytes(normalize(source))
        except (WorkspacePathError, FileNotFoundError):
            return _UNTOLD
        texts[index] = source_data.decode("utf-8", errors="replace")
    return with_replace_from(edits, texts)


async def edit_probe(session: Any, arguments: Mapping[str, Any]) -> str | bool | None:
    """The ``landed_probe`` of ``sandbox_edit_file``: whether a call with *arguments* is applied in its file, read through *session*.

    A line naming the file when every edit is there (the repeat is answered
    "already applied"), ``False`` when the file still waits for them, ``None``
    when the file cannot tell (absent, changed since, a cut argument).
    """
    try:
        path = normalize(arguments.get("path"))
        entry, data = await session.get_bytes(path)
    except (WorkspacePathError, FileNotFoundError):
        return None
    edits = await _with_sources(session, arguments.get("edits"))
    if edits is _UNTOLD:
        return None
    verdict = edit_verdict(edits, data.decode("utf-8", errors="replace"))
    if verdict is True:
        return (
            f"already applied: workspace/{path} already holds every edit of that call "
            f"(sha256 {str(entry.sha256)[:12]}), read from the file"
        )
    return verdict


def replace_dry_run(arguments: Mapping[str, Any]) -> bool:
    """Whether a ``sandbox_replace_in_files`` call only previews (``dry_run``): a read a resumed turn may repeat."""
    return arguments.get("dry_run") is True


def replace_key(arguments: Mapping[str, Any]) -> str | None:
    """The natural key of a ``sandbox_replace_in_files`` call: its scope and pattern, whatever its replacement."""
    pattern = arguments.get("pattern")
    if not isinstance(pattern, str) or not pattern or _cut(pattern):
        return None
    globs = arguments.get("include_globs") or []
    if not isinstance(globs, list):
        return None
    return "\n".join(
        [
            f"prefix: {arguments.get('path_prefix') or ''}",
            f"globs: {','.join(sorted(str(g) for g in globs))}",
            f"pattern: {pattern} (ignore_case={arguments.get('ignore_case') is True})",
        ]
    )


def replace_verdict(texts: list[tuple[str, str]], regex: Any, template: str) -> bool | None:
    """Whether a replace of *regex* by *template* is applied in *texts* (path, text): ``True``, ``False`` or ``None``.

    Landed when every match already starts its own expansion (``foo`` to
    ``foobar`` over a file that reads ``foobar``: a repeat would write
    ``foobarbar``); not landed when no match does; ``None`` when nothing
    matches (a repeat answers its own "no match") or the matches disagree.
    """
    verdicts: set[bool] = set()
    for _path, text in texts:
        for match in regex.finditer(text):
            try:
                expansion = match.expand(template)
            except (IndexError, ValueError):
                return None
            if expansion == match.group(0):
                continue
            verdicts.add(text.startswith(expansion, match.start()))
    return verdicts.pop() if len(verdicts) == 1 else None


async def replace_probe(session: Any, arguments: Mapping[str, Any]) -> str | bool | None:
    """The ``landed_probe`` of ``sandbox_replace_in_files``: whether a call with *arguments* is applied in its files."""
    import re

    from app.services.sandbox_workspace import reading, replacing
    from app.services.sandbox_workspace.checkout import is_record_name

    if replace_dry_run(arguments) or replace_key(arguments) is None:
        return None
    try:
        searched = reading.compile_pattern(arguments.get("pattern"), ignore_case=arguments.get("ignore_case") is True)
        regex = re.compile(searched.pattern, searched.flags | re.MULTILINE)
        replacement = arguments.get("replacement", "")
        if _cut(replacement):
            return None
        template = replacing.check_template(replacement)
        prefix = normalize(arguments.get("path_prefix") or "", allow_root=True)
    except (reading.PatternError, replacing.ReplaceError, WorkspacePathError):
        return None
    globs = [str(g) for g in (arguments.get("include_globs") or []) if str(g).strip()]
    if not prefix and not globs:
        return None
    entries = await session.entries(refresh=True)
    texts: list[tuple[str, str]] = []
    total = 0
    for rel in sorted(entries):
        if (
            not reading.selected(rel, prefix, globs)
            or is_record_name(rel)
            or reading.unread_binary(rel)
            or entries[rel].size > reading.MAX_GREP_FILE_BYTES
        ):
            continue
        total += entries[rel].size
        if total > reading.MAX_GREP_BYTES:
            return None
        _entry, data = await session.read_entry(entries[rel])
        if reading.is_text(data):
            texts.append((rel, reading.decode(data)))
    verdict = replace_verdict(texts, regex, template)
    if verdict is True:
        scope = f"workspace/{prefix}" if prefix else "the workspace"
        return f"already applied: every match of that pattern under {scope} already holds its replacement, read from the files"
    return verdict


def write_verdict(content: object, sha256: str | None) -> bool | None:
    """Whether a ``sandbox_write_file`` of *content* landed on a path holding *sha256* (``None``: absent)."""
    if not isinstance(content, str) or _cut(content):
        return None
    if sha256 is None:
        return False
    return True if hashlib.sha256(content.encode("utf-8")).hexdigest() == sha256 else None


def outcome_line(name: str, path: str, landed: bool) -> str:
    """The note's words for a verdict."""
    if landed:
        return f"it landed (workspace/{path} holds its change): do not repeat it"
    if name == "sandbox_write_file":
        return f"it did not land (workspace/{path} does not exist): repeat it as it was"
    return f"it did not land (workspace/{path} still holds every find text): repeat it as it was"


async def judge_for_resume(
    in_flight: list[dict[str, Any]], conversation_id: object, user_id: object, *, assignment_run_id: object = None
) -> int:
    """:func:`judge` on the workspace of the conversation (or run) a resumed turn continues; 0 without one."""
    if not any(c.get("name") in SANDBOX_WRITES and not c.get("answered") for c in in_flight):
        return 0
    from app.services.sandbox_workspace.session import WorkspaceSession, workspace_enabled
    from app.services.sandbox_workspace.store import WorkspaceScope

    if user_id is None or not workspace_enabled():
        return 0
    try:
        scope = WorkspaceScope.build(
            user_id=user_id, conversation_id=conversation_id, assignment_run_id=assignment_run_id
        )
        if scope is None:
            return 0
        session = WorkspaceSession(scope)
        if await session.record(create=False) is None:
            return 0
    except Exception as exc:
        logger.debug("No workspace to judge the interrupted sandbox writes in: %s", format_exception_message(exc))
        return 0
    return await judge(in_flight, session)


async def judge(in_flight: list[dict[str, Any]], session: object) -> int:
    """Write ``landed`` and ``outcome`` on each unanswered sandbox write of *in_flight*, read through *session*.

    *session* is a ``WorkspaceSession``. Returns how many calls got a verdict;
    a call this cannot judge is left as it was, and a read failure is logged
    and judges nothing.
    """
    calls = [c for c in in_flight if c.get("name") in SANDBOX_WRITES and not c.get("answered")]
    if not calls:
        return 0
    judged = 0
    try:
        entries = await session.entries(refresh=True)  # type: ignore[attr-defined]
        for call in calls:
            raw = call.get("arguments")
            arguments: dict[str, Any] = raw if isinstance(raw, dict) else {}
            try:
                path = normalize(arguments.get("path"))
            except WorkspacePathError:
                continue
            entry = entries.get(path)
            if call["name"] == "sandbox_write_file":
                landed = write_verdict(arguments.get("content"), entry.sha256 if entry is not None else None)
            else:
                text = None
                if entry is not None:
                    _entry, data = await session.read_entry(entry)  # type: ignore[attr-defined]
                    text = data.decode("utf-8", errors="replace")
                edits = await _with_sources(session, arguments.get("edits"))
                if edits is _UNTOLD:
                    continue
                landed = edit_verdict(edits, text)
            if landed is None:
                continue
            call["landed"] = landed
            call["outcome"] = outcome_line(str(call["name"]), path, landed)
            judged += 1
    except Exception as exc:
        logger.warning(
            "Could not tell whether the interrupted sandbox writes landed: %s", format_exception_message(exc)
        )
    return judged
