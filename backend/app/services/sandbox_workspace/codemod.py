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

"""A transform script run over many workspace files: all or nothing.

Pure: no I/O. ``sandbox_codemod`` (``builtin_tools/sandbox_workspace.py``) is the
tool for a sweep a regular expression cannot express (each site needs its
context: the enclosing element, an import list, a ternary rewritten as two
siblings). The transform is a module the workspace holds; it runs in the
checkout's declared codemod check (``[check.<name>codemod]`` in
``.xtm-one/checks.toml``: the repository's own runner, in its own image, with
its installed parser), never as a command of the agent's own.

The exchange with the runner goes through a staging directory at the workspace
root (:data:`STAGING_DIR`, outside every checkout and read back, unlike
``.xtm/``): the tool writes :data:`REQUEST_FILE` (the transform, the files, as
workspace paths, and the workspace root as seen from the check's directory), the runner writes
:data:`RESULT_FILE` (per file: changed or not, the transform's error, the parse
errors of the new text) and each new text under :data:`OUTPUT_DIR`. The runner
never writes a source file: the tool reads the staged texts back, refuses the
whole change set when a transform failed or a changed file no longer parses,
and writes every change in ONE workspace commit that also drops the staging
directory. ``dry_run`` answers the same report and drops the staging directory
alone.
"""

from __future__ import annotations

import json
import posixpath
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.services.sandbox_workspace import replacing

#: Files one call may change; a sweep past it runs in parts.
MAX_FILES = 500
#: Files one call hands the runner.
MAX_SELECTED = 2_000
#: Directory of a checkout the tool and the runner exchange through, removed by every call.
STAGING_DIR = ".xtm-codemod"
REQUEST_FILE = "request.json"
RESULT_FILE = "result.json"
OUTPUT_DIR = "out"
#: Errors named per refused file, and characters of one error.
MAX_ERRORS_SHOWN = 20
MAX_ERROR_CHARS = 300


class CodemodError(ValueError):
    """A codemod that cannot apply; ``str()`` says which and why, for the model."""


@dataclass
class Plan:
    changes: list[replacing.FileChange] = field(default_factory=list)
    files_scanned: int = 0
    #: (path, error) of each file the transform threw on or whose new text does not parse.
    refused: list[tuple[str, str]] = field(default_factory=list)


def staging_dir(run: str) -> str:
    """The staging directory of one call (a workspace path)."""
    return posixpath.join(STAGING_DIR, run)


def root_from(cwd: str) -> str:
    """The workspace root as seen from a check's working directory *cwd* (workspace-relative): ``..`` per segment."""
    depth = len([p for p in cwd.split("/") if p and p != "."])
    return "/".join([".."] * depth) or "."


def request(*, transform: str, files: list[str], root: str) -> bytes:
    """The request the runner reads: *transform* and *files* as workspace paths, *root* the workspace from the check's directory."""
    return json.dumps({"version": 1, "root": root, "transform": transform, "files": files}, indent=1).encode("utf-8")


def read_result(raw: bytes) -> dict[str, dict[str, Any]]:
    """The runner's per-file results, keyed by checkout-relative path; :class:`CodemodError` when malformed."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CodemodError(
            f"the codemod runner wrote an unreadable {RESULT_FILE} ({exc}). Nothing was written."
        ) from exc
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, list):
        raise CodemodError(f"the codemod runner's {RESULT_FILE} has no files list. Nothing was written.")
    out: dict[str, dict[str, Any]] = {}
    for item in files:
        if isinstance(item, dict) and isinstance(item.get("path"), str):
            out[item["path"]] = item
    if isinstance(data.get("error"), str) and data["error"]:
        raise CodemodError(f"the transform could not run: {data['error'][:MAX_ERROR_CHARS]}. Nothing was written.")
    return out


def plan(befores: list[tuple[str, str]], results: dict[str, dict[str, Any]], afters: dict[str, str]) -> Plan:
    """The change set: *befores* (path, text) as selected, the runner's *results*, the staged new texts *afters*.

    A file the transform threw on, or whose new text has parse errors, is
    refused; a file the runner reports changed without a staged text is too.
    """
    out = Plan()
    for path, before in befores:
        out.files_scanned += 1
        result = results.get(path)
        if result is None:
            out.refused.append((path, "the runner reported nothing for it"))
            continue
        if result.get("error"):
            out.refused.append((path, f"the transform threw: {str(result['error'])[:MAX_ERROR_CHARS]}"))
            continue
        if not result.get("changed"):
            continue
        parse_errors = [str(e)[:MAX_ERROR_CHARS] for e in (result.get("parse_errors") or [])]
        if parse_errors:
            out.refused.append((path, "no longer parses: " + "; ".join(parse_errors[:3])))
            continue
        after = afters.get(path)
        if after is None:
            out.refused.append((path, "reported changed, but no new text was staged"))
            continue
        if after == before:
            continue
        out.changes.append(replacing.FileChange(path=path, before=before, after=after, replacements=1))
    return out


def refusal(planned: Plan, display: Callable[[str], str]) -> str:
    """The refusal text of a plan with refused files, every file named up to :data:`MAX_ERRORS_SHOWN`."""
    lines = [f"- {display(path)}: {why}" for path, why in planned.refused[:MAX_ERRORS_SHOWN]]
    more = len(planned.refused) - MAX_ERRORS_SHOWN
    if more > 0:
        lines.append(f"- ... and {more} more file(s)")
    return (
        f"the codemod is refused on {len(planned.refused)} file(s) of {planned.files_scanned}: nothing was "
        "written. Fix the transform and run it again (dry_run=true previews).\n" + "\n".join(lines)
    )


def report(changes: list[replacing.FileChange], display: Callable[[str], str]) -> tuple[str, str]:
    """The diffstat lines and the bounded diffs of *changes*."""
    stats, diffs = replacing.report(changes, display)
    return stats.replace(": 1 replacement(s), ", ": "), diffs
