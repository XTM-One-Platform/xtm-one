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

"""Pre-push guards: the checks a change must pass on the current tree before a commit.

A frontend change pushed without ``tsc`` or ``knip`` used to go red in CI one
round later (an unused export, a type error): nothing in the sandbox said the
check had not run on what was committed. A check of ``.xtm-one/checks.toml``
declared ``pre_push = [globs]`` is a guard:

- ``sandbox_run_command`` runs it whole (no args, no ``affected``) and keeps,
  under ``.xtm/runs/prepush/`` (:func:`state_path`), the fingerprint of the files it
  reads (its directory and ``needs``, filtered by the globs) with the exit code;
- ``create_github_commit(from_checkout=...)`` asks :func:`warning` which guards
  the change set touches and did not pass on that same fingerprint: never run,
  failed, or passed on an older tree. A warning in the result, not a refusal.

The fingerprint comes from the workspace manifest (the sha256 of each file the
store already holds): no container, no file read, so the guard costs nothing.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from app.services.sandbox_workspace.checkout import (
    RECORD_NAMES,
    CheckoutChanges,
    CheckoutRecord,
    CheckoutRecordError,
    is_generated,
)
from app.services.sandbox_workspace.checks import (
    CHECKS_FILE,
    RUNS_DIR,
    ChecksError,
    CheckSpec,
    command_argv,
    load_checks,
)
from app.services.sandbox_workspace.paths import WorkspacePathError, is_under, normalize

logger = logging.getLogger(__name__)


def state_path(checkout_path: str) -> str:
    """The workspace file keeping the guards' last runs of the checkout at *checkout_path*.

    Under the run outputs (``.xtm/runs``) at the workspace root: a check's output, never a change of any
    checkout, the checkout at the root included.
    """
    slug = checkout_path.strip("/").replace("/", "__") or "_root"
    return f"{RUNS_DIR}/prepush/{slug}.json"


def _scopes(spec: CheckSpec) -> tuple[str, ...]:
    """The repository directories a guard reads: its own and its ``needs``; the whole checkout for a ``git_repo``
    guard, whose command reads every file git lists (the Editions gate classifies a file under any directory)."""
    if spec.git_repo:
        return ("",)
    return (spec.cwd, *spec.needs)


def _guarded(repo_path: str, spec: CheckSpec) -> bool:
    """Whether *repo_path* is a file guard *spec* reads."""
    if not any(is_under(repo_path, scope) for scope in _scopes(spec)):
        return False
    name = repo_path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatchcase(repo_path, g) or fnmatch.fnmatchcase(name, g) for g in spec.pre_push)


def fingerprint(record: CheckoutRecord, entries: Mapping[str, object], spec: CheckSpec) -> str:
    """The sha256 of the (path, content sha) pairs of the files guard *spec* reads, as the workspace holds them."""
    pairs: list[str] = []
    for workspace_path, entry in entries.items():
        repo_path = record.repo_path(str(workspace_path))
        if repo_path is None or repo_path in RECORD_NAMES or is_generated(repo_path):
            continue
        if _guarded(repo_path, spec):
            pairs.append(f"{repo_path}\0{getattr(entry, 'sha256', '') or ''}")
    return hashlib.sha256("\n".join(sorted(pairs)).encode("utf-8")).hexdigest()


def guards(specs: Mapping[str, CheckSpec]) -> list[CheckSpec]:
    """The checks declared ``pre_push``, by name."""
    return [specs[name] for name in sorted(specs) if specs[name].pre_push]


def load_state(raw: bytes | None) -> dict[str, dict[str, Any]]:
    """The stored runs by check name; ``{}`` for none or an unreadable file."""
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def updated_state(state: Mapping[str, dict[str, Any]], check: str, *, fingerprint: str, exit_code: int) -> bytes:
    """*state* with the run of *check* replaced: what a guard's run writes."""
    merged = dict(state)
    merged[check] = {"fingerprint": fingerprint, "exit_code": exit_code, "at": datetime.now(UTC).isoformat()}
    return json.dumps(merged, indent=2, sort_keys=True).encode("utf-8")


def touched(changes: CheckoutChanges, spec: CheckSpec) -> list[str]:
    """The changed files of the change set guard *spec* reads, among the kinds of change it is declared on."""
    kinds = {"added": changes.added, "modified": changes.modified, "removed": changes.removed}
    paths = [p for kind in ("modified", "added", "removed") if kind in spec.pre_push_on for p in kinds[kind]]
    return [p for p in paths if _guarded(p, spec)]


def warning(
    record: CheckoutRecord,
    changes: CheckoutChanges,
    entries: Mapping[str, object],
    specs: Iterable[CheckSpec],
    state: Mapping[str, dict[str, Any]],
    *,
    checkout: str,
) -> str:
    """The sentence naming each guard the change set touches that did not pass on the current tree; ``""`` when none."""
    lines: list[str] = []
    for spec in specs:
        hit = touched(changes, spec)
        if not hit:
            continue
        run = state.get(spec.name)
        current = fingerprint(record, entries, spec)
        if run is None:
            why = "never ran on this checkout"
        elif run.get("fingerprint") != current:
            why = "ran on an older tree (files it reads changed since)"
        elif run.get("exit_code") != 0:
            why = f"failed (exit {run.get('exit_code')}) on this tree"
        else:
            continue
        shown = ", ".join(hit[:3]) + (f", +{len(hit) - 3} more" if len(hit) > 3 else "")
        lines.append(f"{spec.name} {why} ({shown})")
    if not lines:
        return ""
    return (
        "Pre-push guard: CI will run these on the commit and the sandbox did not see them pass on this tree: "
        + "; ".join(lines)
        + f'. Run each with sandbox_run_command(checkout="{checkout}", check="its name") and no args before the next push.'
    )


async def _read(session: Any, path: str) -> bytes | None:
    try:
        _entry, data = await session.get_bytes(path)
    except FileNotFoundError:
        return None
    return bytes(data)


async def _context(session: Any, checkout: str) -> tuple[CheckoutRecord, dict[str, CheckSpec]] | None:
    """The checkout record at *checkout* and its declared checks; ``None`` without either."""
    try:
        root = normalize(checkout)
    except WorkspacePathError:
        return None
    raw_record = None
    for name in RECORD_NAMES:
        raw_record = await _read(session, f"{root}/{name}")
        if raw_record is not None:
            break
    raw_checks = await _read(session, f"{root}/{CHECKS_FILE}")
    if raw_record is None or raw_checks is None:
        return None
    try:
        record = CheckoutRecord.from_json(raw_record, path=root)
        specs = load_checks(raw_checks.decode("utf-8", errors="replace"))
    except (CheckoutRecordError, ChecksError):
        return None
    return record, specs


def recorded_guards(spec: CheckSpec, specs: Mapping[str, CheckSpec]) -> list[CheckSpec]:
    """The guards a whole run of *spec* counts for: itself when it is one, and each declared guard it stands for."""
    kept = [spec] if spec.pre_push else []
    for name in spec.stands_for:
        guard = specs.get(name)
        if guard is not None and guard.pre_push and guard not in kept:
            kept.append(guard)
    return kept


def _ran_whole(spec: CheckSpec, argv: tuple[str, ...], *, on_change: bool) -> bool:
    """Whether *argv* is the whole run of *spec*: its declared argv with no args.

    A check declared ``affected_by_default`` called without args (*on_change*) runs on the change: the files the
    platform appends ARE its whole run (``backend-queue-tests`` runs the merge queue's selection of the change).
    """
    declared = command_argv(spec, [])
    if argv == declared:
        return True
    return on_change and spec.affected_by_default and argv[: len(declared)] == declared


async def record_run(
    session: Any,
    checkout: str,
    *,
    check: str,
    argv: Iterable[str],
    exit_code: int | None,
    on_change: bool = False,
) -> bool:
    """Keep the run of guard *check* when it ran whole (its declared argv, no args) and ended; whether it was kept.

    A check declaring ``stands_for`` (the same gate on another image) is kept as the run of each guard it names.
    A guard run on some files (args, ``affected``) proves nothing about the tree: it is not kept, except the run
    of a check declared ``affected_by_default`` called without args (*on_change*): it runs on the whole change.
    Never raises: the check ran, so a state that cannot be written is logged, not thrown.
    """
    if not check or exit_code is None:
        return False
    try:
        context = await _context(session, checkout)
        if context is None:
            return False
        record, specs = context
        spec = specs.get(check)
        if spec is None or not _ran_whole(spec, tuple(argv), on_change=on_change):
            return False
        kept = recorded_guards(spec, specs)
        if not kept:
            return False
        entries = await session.entries(refresh=True)
        path = state_path(record.path)
        state = load_state(await _read(session, path))
        for guard in kept:
            data = updated_state(
                state, guard.name, fingerprint=fingerprint(record, entries, guard), exit_code=exit_code
            )
            state = load_state(data)
        await session.put_tree([(path, data)], operation="run_command")
    except Exception:
        logger.warning("Pre-push guard state of %s not written for %s", checkout, check, exc_info=True)
        return False
    return True


async def commit_warning(session: Any, record: CheckoutRecord, changes: CheckoutChanges) -> str:
    """The :func:`warning` of a ``from_checkout`` change set, read from the workspace; empty on any failure."""
    try:
        raw_checks = await _read(session, f"{record.path}/{CHECKS_FILE}")
        if raw_checks is None:
            return ""
        declared = guards(load_checks(raw_checks.decode("utf-8", errors="replace")))
        if not declared:
            return ""
        entries = await session.entries(refresh=True)
        state = load_state(await _read(session, state_path(record.path)))
        return warning(record, changes, entries, declared, state, checkout=record.path)
    except Exception:
        logger.warning("Pre-push guard of %s not evaluated", record.path, exc_info=True)
        return ""
