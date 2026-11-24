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

"""Run a check of a checkout, in the workspace's OpenSandbox container.

A checkout may carry ``.xtm-one/checks.toml`` (:mod:`checks`): what an agent
may run on it, as one argv per check. One that does not (almost every
repository) gets the checks :mod:`detect` reads from its own files: its CI
workflows, its manifests, its ``Makefile`` (:func:`checkout_checks`); the file
is an override, never a requirement. :func:`resolve` turns a call
(``check`` + ``args``, or a bare ``argv`` when the platform allows one) into a
:class:`ResolvedCommand` bound to the workspace; :func:`run_command` runs it
in a container of the check's image key, kept per workspace like the
``session=true`` kernel (``kernel_state["commands"]``), with the workspace
reconciled before and read back after, the report file parsed, the whole
output saved under ``.xtm/runs/<id>/`` in the workspace.

The command runs DETACHED: the workspace helper's ``exec`` action
starts it in the background and writes its output and exit code to files in
the container, so no HTTP call is held for the command's duration. The call
waits for the end within :func:`wait_budget` (the agent loop's deadline for
the tool call, less what the read-back needs); a command still running then
(a real ``npm ci`` or ``pip install`` takes longer than one tool call) is
answered ``running`` with its ``run_id``, recorded in
``kernel_state["commands"][<image>]["run"]``, and :func:`poll_run` waits for it
again from any replica until it exits; the result is then the same as a
command that ended within one call.

Generic on purpose: no tool name is known here (invariant 18); what a check
is, which image carries it and what it reports is the repository's
declaration. The container has no network and no credential, as every
sandbox; the command's environment is the container's own plus
:data:`SAFE_ENV`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import posixpath
import re
import shlex
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from app.core.utils import format_exception_message
from app.services.sandbox_workspace import audit, detect, jobs, metrics
from app.services.sandbox_workspace.checks import (
    CHECKS_FILE,
    DEFAULT_TIMEOUT,
    FAILURES_FILE,
    LISTING_MAX_CHARS,
    MAX_REPORT_BYTES,
    RUNS_DIR,
    SERVICES_LISTING_MAX_CHARS,
    WHOLE_ARG,
    ChecksError,
    CheckSpec,
    ReportSummary,
    command_argv,
    declares_checks,
    describe_checks,
    describe_services,
    extra_args,
    format_result,
    load_checks,
    load_services,
    parse_report,
    shown_argv,
)
from app.services.sandbox_workspace.manifest import QuotaExceededError as WorkspaceQuotaError
from app.services.sandbox_workspace.paths import WorkspacePathError, display, is_under, normalize
from app.services.sandbox_workspace.run import SESSION, WorkspaceCall, commit_scan
from app.services.sandbox_workspace.session import WorkspaceSession, WorkspaceUnavailableError

logger = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]

#: The image key a check runs on when it names none.
DEFAULT_IMAGE_KEY = "python"
#: The check every checkout has unless it declares one of that name (:func:`_script_command`): one file of the
#: workspace run with the interpreter its extension names, the checkout root as cwd, its own args after it. The
#: program is fixed by the extension, so it opens no arbitrary argv (``CODE_INTERPRETER_COMMAND_ALLOW_ARGV``).
SCRIPT_CHECK = "script"
#: File extension to (stack, program): the stack picks the image (:data:`detect.STACK_IMAGES`).
SCRIPT_RUNNERS: dict[str, tuple[str, tuple[str, ...]]] = {
    ".py": ("python", ("python",)),
    ".mjs": ("node", ("node",)),
    ".cjs": ("node", ("node",)),
    ".js": ("node", ("node",)),
    ".ts": ("node", ("node", "--experimental-strip-types")),
    ".mts": ("node", ("node", "--experimental-strip-types")),
}
#: The prefixes a detected Python check runs under when the project's environment is a runner's venv.
_PROJECT_RUNNERS = (("uv", "run"), ("poetry", "run"))
SCRIPT_LISTING = (
    f"Built in: {SCRIPT_CHECK}: args = one workspace file ({', '.join(SCRIPT_RUNNERS)}; relative to the checkout "
    "or workspace/...) then its own args; run with python or node on the image of the nearest check of its "
    "directory (its install bound, the `uv run --with` deps of its checks added), the checkout root as cwd "
    "unless cwd names a directory of the checkout."
)
#: What a command builds in the container and the workspace never records
#: (``fnmatch`` over the workspace path; a directory as ``dir/``): the
#: container keeps it between commands, the manifest and a commit never see it.
IGNORED_PATTERNS = (
    ".xtm/",
    "*/.xtm/",
    "__pycache__/",
    "*/__pycache__/",
    "*.pyc",
    ".venv/",
    "*/.venv/",
    "node_modules/",
    "*/node_modules/",
    ".mypy_cache/",
    "*/.mypy_cache/",
    ".ruff_cache/",
    "*/.ruff_cache/",
    ".pytest_cache/",
    "*/.pytest_cache/",
    ".yarn/",
    "*/.yarn/",
    # The throwaway repository of a git_repo check, should a run end before its trap removes it.
    ".git/",
    "*/.git/",
)
#: Variables every command gets on top of the container's own.
SAFE_ENV = {
    "CI": "1",
    "NO_COLOR": "1",
    "TERM": "dumb",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
    "HOME": "/tmp",  # noqa: S108 - the container's own writable directory
}
#: Seconds past the command's own timeout before a run with no end marker is
#: given up (the runner ends the command at its timeout and writes the marker:
#: no marker past this means the runner itself is gone) and the container discarded.
HARD_TIMEOUT_MARGIN = 30
#: Characters of stdout and of stderr the run directory keeps.
MAX_SAVED_OUTPUT = 2 * 1024 * 1024
#: Run directories (``.xtm/runs/<id>/``) a workspace keeps: saving one more drops the oldest beyond it in the
#: same change set, so saved results never fill the workspace's file limit (gap workspace-file-limit-drops-check-writes:
#: 924 run directories, 2,868 files, left a check's own writes refused at the 20,000 file limit).
MAX_KEPT_RUNS = 100
#: Seconds a call waits for its command when no agent-loop deadline bounds the call.
COMMAND_WAIT_SECONDS = 240
#: Seconds kept before the call's deadline for the read-back, the save and the answer.
FINISH_RESERVE_SECONDS = 45
#: Seconds of the call kept after a container create for the workspace upload and the start: a create
#: still running past the call's budget less this answers ``starting`` (it goes on) instead of the loop's
#: bare timeout.
UPLOAD_RESERVE_SECONDS = 60
#: Seconds an ``affected=true`` selection may read test files, at most: it runs in the same call before the command.
SELECTION_MAX_SECONDS = 90.0
#: Seconds of the call's budget an ``affected=true`` selection leaves to the container create, the upload,
#: the start and a first wait on the command, so a long selection still ends in ``running``, never the loop's timeout.
SELECTION_LEAVES_SECONDS = 120.0
#: Interval between two looks at a detached run's end marker: from the first to the last, doubling.
POLL_START = 0.5
POLL_MAX = 5.0
#: Characters of the output so far a ``running`` answer shows.
RUNNING_TAIL = 2_000
#: Seconds a ``background`` start waits for the command's first lines before it answers ``running``.
BACKGROUND_FIRST_LINES_SECONDS = 1.0
#: Seconds a stop waits for the helper to end the process group and write the end marker.
STOP_WAIT_SECONDS = 10.0


@dataclass(frozen=True)
class ResolvedCommand:
    """A command bound to the workspace: what runs, where, for how long, on which image."""

    check: str
    argv: tuple[str, ...]
    #: Workspace-relative working directory, ``""`` for the workspace root.
    cwd: str
    timeout: int
    image: str
    report: str = "lines"
    #: Workspace-relative path of the report file the check writes, ``""`` for none.
    report_path: str = ""
    #: The check's ``default_args`` that end ``argv`` because the call gave no args
    #: (``()`` otherwise): what a selection of files (``affected=true``) replaces.
    default_args: tuple[str, ...] = ()
    #: The check's declared ``env``, set for the command over the container's own (the call's ``env`` over it).
    env: tuple[tuple[str, str], ...] = ()
    #: The variables CI sets from a secret or an expression that this run leaves unset (named in the answer).
    env_unset: tuple[str, ...] = ()
    #: What ``affected=true`` appends: the affected ``tests``, the changed ``files``, or the changed ``lines``.
    affected: str = "tests"
    #: With ``files`` or ``lines``: the globs the changed files must match (relative to the check's directory).
    affected_globs: tuple[str, ...] = ()
    #: With ``tests``: the globs of the test files selected on every source change, ranked after the changed tests.
    affected_always: tuple[str, ...] = ()
    #: With ``tests``: the patterns a test file's text matches to be selected as a source-scan gate.
    affected_scan_patterns: tuple[str, ...] = ()
    #: The check declares ``affected_by_default`` and the call gave no args: it runs as ``affected=true``.
    affected_by_default: bool = False
    #: The check declares ``affected_narrows_args`` and the call gave args without ``--whole``: those args, the
    #: tail of :attr:`argv`, narrowed to the change before the run (a bare modified path to ``PATH:A-B``).
    narrow_args: tuple[str, ...] = ()
    #: The check declares ``affected_whole_repository``: the changed files outside its directory are passed too.
    affected_whole_repository: bool = False
    #: The install check of the same directory and image, run first when the container has no installed
    #: dependencies there (:func:`_needs_install`); ``None`` for an install, an argv or an ``install_first = false`` check.
    install: ResolvedCommand | None = None
    #: The image its install check declares as ``fallback`` (``""`` for none): :func:`run_with_fallback` runs
    #: the same command there when the install fails on this image with :attr:`fallback_when` in its output,
    #: if :attr:`fallback_configured` (the platform runs that image); :attr:`fallback_reason` names the cause.
    fallback_image: str = ""
    fallback_configured: bool = False
    fallback_when: str = ""
    fallback_reason: str = ""

    @property
    def label(self) -> str:
        return self.check or "argv"

    def without_defaults(self) -> tuple[str, ...]:
        """``argv`` without the default arguments it ends with, if any."""
        return self.argv[: len(self.argv) - len(self.default_args)] if self.default_args else self.argv


@dataclass
class CommandOutcome:
    """What a command did: its text for the tool and the facts behind it."""

    text: str
    exit_code: int | None = None
    timed_out: bool = False
    seconds: float = 0.0
    run_dir: str = ""
    report: ReportSummary | None = None
    refused: bool = False
    container: str = ""
    #: The command is still running in its container: :func:`poll_run` with :attr:`run_id` waits for it again.
    running: bool = False
    run_id: str = ""
    #: The command the outcome is about (set when a poll finishes a run, for the audit).
    command: ResolvedCommand | None = None
    #: The container is still being created past the call's budget: nothing ran, the same call again runs it.
    starting: bool = False
    #: The command was stopped on request (``sandbox_run_command`` ``stop``) with its process group.
    stopped: bool = False
    #: Workspace paths the command added or changed, as read back (empty when nothing was saved).
    written: list[str] = field(default_factory=list)
    #: The part of :attr:`written` that existed before the command (changed, not added).
    rewritten: list[str] = field(default_factory=list)
    #: Workspace paths its output says were missing (a ``No such file or directory`` line), see :func:`missing_paths`.
    missing: list[str] = field(default_factory=list)
    #: Test files pytest could not collect (relative to the check's directory), see :func:`uncollected_files`.
    uncollected: list[str] = field(default_factory=list)
    #: pytest stopped at collection (``Interrupted: N error(s) during collection``): nothing of the session ran.
    interrupted: bool = False
    #: Another command holds the container (:attr:`run_id` names it): nothing ran, a state rather than an error.
    busy: bool = False
    #: A polled run's container or run directory is gone (a restart, a rollout): its result is lost, a state
    #: the answer names so the turn runs the check again, never an error (a friction the platform created).
    lost: bool = False


#: pytest's names for a test file it could not collect: the section header and the short summary line.
_UNCOLLECTED = (
    re.compile(r"ERROR collecting (\S+\.py)\b"),
    re.compile(r"^ERROR (\S+\.py)(?: - |\s*$)"),
)


_KEEP_GOING = "--continue-on-collection-errors"


def continue_on_collection_errors(argv: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """The flag that keeps a pytest run going past a file it cannot collect: ``()`` for another runner or when set."""
    runs_pytest = any(posixpath.basename(arg) in ("pytest", "py.test") for arg in argv[:4])
    if not runs_pytest or _KEEP_GOING in argv:
        return ()
    return (_KEEP_GOING,)


def uncollected_files(output: str) -> list[str]:
    """The test files (sorted) pytest's *output* reports as collection errors; a test's own error is not one."""
    out: set[str] = set()
    for line in output.splitlines():
        for pattern in _UNCOLLECTED:
            out.update(pattern.findall(line.strip()))
    return sorted(out)


_INTERRUPTED = re.compile(r"Interrupted: \d+ errors? during collection")


def uncollected_note(outcome: CommandOutcome) -> str:
    """The sentence naming the test files pytest could not collect in a narrow checkout, and what the rest did."""
    files = ", ".join(outcome.uncollected[:5]) + (", ..." if len(outcome.uncollected) > 5 else "")
    if outcome.interrupted:
        return (
            f"Not runnable in this checkout: {len(outcome.uncollected)} test file(s) not collected ({files}), and "
            "pytest stopped the whole session at collection: no test ran. Pass --continue-on-collection-errors in "
            "args (affected=true does) to run the rest."
        )
    text = f"Not runnable in this checkout: {len(outcome.uncollected)} test file(s) not collected, the rest ran: {files}."
    report = outcome.report
    if report is not None and report.kind == "junit" and not report.problem:
        errors = max(0, report.errors - len(outcome.uncollected))
        verdict = "pass" if not (report.failed or errors) else "do not pass"
        text += (
            f" Without them: {report.passed} passed, {report.failed} failed, {errors} error(s): the collected tests "
            f"{verdict} (the exit code also counts the files not collected)."
        )
    return text


def missing_file_verdict(outcome: CommandOutcome, outside: list[str]) -> str:
    """The sentence giving the run's verdict without the tests that failed on a file outside the checkout, or ``""``.

    *outside* holds the workspace paths of :attr:`CommandOutcome.missing` the narrow checkout does not
    hold. A JUnit report names each failing test with its body: a test whose body names one of them
    failed on the checkout, not on the change. Without the count the answer said "1 failed" next to a
    note calling it environmental, and the run was read as red (issue #1111).
    """
    report = outcome.report
    if report is None or report.kind != "junit" or report.problem or not report.failures or not outside:
        return ""
    on_checkout = [name for name, body in report.failures if any(path in body for path in outside)]
    if not on_checkout:
        return ""
    rest = len(report.failures) - len(on_checkout)
    verdict = "the tests this checkout can run pass" if not rest else f"{rest} other test(s) fail on the change"
    names = ", ".join(on_checkout[:3]) + (", ..." if len(on_checkout) > 3 else "")
    return (
        f"Without the {len(on_checkout)} test(s) failing on those files ({names}): {report.passed} passed, "
        f"{rest} failed: {verdict} (the exit code still counts them)."
    )


#: A line of output saying a file was missing: Python, Node, TypeScript and the shell spell it so.
_MISSING_LINE = re.compile(r"No such file or directory|ENOENT|FileNotFoundError|Cannot find module|error TS6053")
#: A TypeScript diagnostic for a relative import it could not resolve: ``src/a.ts(8,30): error TS2307: Cannot
#: find module './b.ts'``, the importing file relative to the check's directory.
_TS_RELATIVE_IMPORT = re.compile(
    r"^(?P<file>[^\s(]+)\(\d+,\d+\): error TS2307: Cannot find module '(?P<module>\.{1,2}/[^']+)'"
)


def missing_paths(output: str, root: str, cwd: str = "") -> list[str]:
    """The workspace paths (relative, sorted) under the container's *root* that a missing-file line of *output* names.

    A TypeScript ``TS2307`` line naming a relative module is resolved from the importing file under *cwd*
    (the check's directory, relative to the workspace): ``tsc`` prints it without the absolute path.
    """
    found = re.compile(re.escape(root.rstrip("/")) + r"/([^\s'\"`:,)\]]+)")
    out: set[str] = set()
    for line in output.splitlines():
        if not _MISSING_LINE.search(line):
            continue
        out.update(found.findall(line))
        relative = _TS_RELATIVE_IMPORT.match(line.strip())
        if relative and not posixpath.isabs(relative["file"]):
            base = posixpath.join(cwd.strip("/"), posixpath.dirname(relative["file"]))
            path = posixpath.normpath(posixpath.join(base, relative["module"]))
            if not path.startswith("../") and path != "..":
                out.add(path)
    return sorted(out)


def wait_budget(clock: Clock = time.monotonic) -> float:
    """Seconds this call may wait for its command: the tool call's deadline less :data:`FINISH_RESERVE_SECONDS`.

    The agent loop cancels a tool call at its per-tool deadline
    (``app.tools.tool_events.current_tool_deadline``); a call outside the loop
    waits :data:`COMMAND_WAIT_SECONDS`.
    """
    from app.tools.tool_events import current_tool_deadline

    deadline = current_tool_deadline()
    if deadline is None:
        return float(COMMAND_WAIT_SECONDS)
    return max(1.0, deadline - clock() - FINISH_RESERVE_SECONDS)


#: Files whose content decides what an install check installs: the cache key of the install hashes them.
LOCKFILES = (
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "package.json",
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    "pyproject.toml",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
    "gradle.lockfile",
    "Cargo.lock",
    "packages.yml",
    "package-lock.yml",
)
#: Where a container keeps the cache keys of the installs that succeeded in it.
INSTALL_CACHE_DIR = "/tmp/.xtm/cache/installs"  # noqa: S108 - the container's own directory
#: Files a package manager leaves in the directory it installed into (relative to the check's cwd): a
#: successful install records the ones present, and a cache hit is answered only while they all still are.
INSTALL_STATE_FILES = (
    "node_modules/.yarn-state.yml",
    "node_modules/.package-lock.json",
    "node_modules/.modules.yaml",
    ".yarn/install-state.gz",
    ".venv/pyvenv.cfg",
)
#: The state files that say a Node directory has its dependencies installed: the ``node_modules/`` state of
#: Yarn, npm and pnpm, or Yarn PnP's loader. Not ``.yarn/install-state.gz``: Yarn 4 writes it on a failed
#: ``yarn run`` in a never-installed directory, before it finds no ``node_modules``.
NODE_STATE_FILES = (*(path for path in INSTALL_STATE_FILES if path.startswith("node_modules/")), ".pnp.cjs")
#: What Yarn says when a script runs in a directory with no installed dependencies (also triggers the hint).
YARN_NO_INSTALL_MESSAGE = "Couldn't find the node_modules state file"
#: Lockfiles that make a directory a Node project (a check failing there with no install state gets a hint).
NODE_LOCKFILES = ("package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml")


def is_install(command: ResolvedCommand) -> bool:
    """Whether *command* is a dependency install (a check named ``install`` or ``<scope>:<lang>-install``)."""
    return is_install_name(command.check)


def is_install_name(name: str) -> bool:
    """Whether a check named *name* is a dependency install (``install``, ``frontend-install``, ``x:node-install``)."""
    return bool(name) and name.rsplit(":", 1)[-1].rsplit("-", 1)[-1] == "install"


def rewritten_manifests(command: ResolvedCommand | None, repo_paths: Iterable[str]) -> list[str]:
    """The manifests and lockfiles among *repo_paths* (files a run changed) when *command* is an install, sorted.

    ``[]`` for any other check: only an install is expected to leave the tree as it found it, so a
    ``package.json`` it rewrote would otherwise reach ``from_checkout`` as a change of the branch.
    """
    if command is None or not is_install(command):
        return []
    return sorted({path for path in repo_paths if posixpath.basename(path) in LOCKFILES})


def install_cache_key(command: ResolvedCommand, entries: dict) -> str | None:
    """The cache key of an install: its image, directory and argv with the hash of every lockfile it reads.

    ``None`` for a command that is not an install or a directory with no
    lockfile (nothing pins what it installs, so nothing can be reused). The
    hashes are the workspace manifest's own (no file is read); a
    ``requirements*.txt`` counts as a lockfile.
    """
    import hashlib

    if not is_install(command):
        return None
    prefix = f"{command.cwd}/" if command.cwd else ""
    locks = []
    for path, entry in entries.items():
        if not path.startswith(prefix) or "/" in path[len(prefix) :]:
            continue
        name = path[len(prefix) :]
        if name in LOCKFILES or (name.startswith("requirements") and name.endswith(".txt")):
            locks.append((name, entry.sha256))
    # A workspace member installs from the root lockfile: that lockfile pins it, beside its own package.json.
    lock_dir = node_lock_dir(command, entries)
    if lock_dir is not None and lock_dir != command.cwd.strip("/"):
        here = f"{lock_dir}/" if lock_dir else ""
        locks += [(f"{here}{n}", entries[f"{here}{n}"].sha256) for n in NODE_LOCKFILES if f"{here}{n}" in entries]
    if not locks:
        return None
    material = json.dumps({"image": command.image, "cwd": command.cwd, "argv": list(command.argv), "locks": sorted(locks)})
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def lock_line(command: ResolvedCommand, entries: dict[str, Any], *, image: str) -> str:
    """One line naming the lockfiles *command*'s install read (path and short sha256) and the image it ran on.

    ``""`` for a command with no install or a directory with no lockfile. A lock newer than a dev image's
    baked cache (a bump merged on main before the image is republished) is then visible in the answer.
    """
    install = command if is_install(command) else command.install
    if install is None:
        return ""
    prefix = f"{install.cwd}/" if install.cwd else ""
    locks = []
    for path, entry in entries.items():
        name = path[len(prefix) :] if path.startswith(prefix) else ""
        if not name or "/" in name:
            continue
        if name in LOCKFILES or (name.startswith("requirements") and name.endswith(".txt")):
            locks.append(f"{path} {str(entry.sha256)[:12]}")
    if not locks:
        return ""
    return f"Lockfiles: {', '.join(sorted(locks))} (dependencies from {image})."


async def _name_locks(
    outcome: CommandOutcome, command: ResolvedCommand, workspace: WorkspaceSession, *, image: str
) -> CommandOutcome:
    """*outcome* with :func:`lock_line` appended once it ended (a running, refused or busy answer is left as is)."""
    if outcome.running or outcome.refused or outcome.busy or outcome.exit_code is None:
        return outcome
    line = ""
    with contextlib.suppress(Exception):
        line = lock_line(command, await workspace.entries(), image=image)
    if line and line not in outcome.text:
        outcome.text = f"{outcome.text.rstrip()}\n{line}"
    return outcome


def node_lock_dir(command: ResolvedCommand, entries: dict[str, Any]) -> str | None:
    """The directory whose Node lockfile pins the check's dependencies, ``None`` when there is none.

    The check's own directory when it holds a lockfile; else, for a package of a Yarn, npm or pnpm workspace
    (a directory with a ``package.json`` and no lockfile), the nearest enclosing directory of the same checkout
    that holds one: the workspace root installs every member and keeps the install state
    (``node_modules/.yarn-state.yml``) there, so a member's check probes and keys its install at the root.
    """
    from app.services.sandbox_workspace.checkout import RECORD_NAME

    cwd = command.cwd.strip("/")
    prefix = f"{cwd}/" if cwd else ""
    if any(f"{prefix}{name}" in entries for name in NODE_LOCKFILES):
        return cwd
    if f"{prefix}package.json" not in entries:
        return None
    where = cwd
    while where:
        if f"{where}/{RECORD_NAME}" in entries:
            return None
        where = posixpath.dirname(where)
        here = f"{where}/" if where else ""
        if any(f"{here}{name}" in entries for name in NODE_LOCKFILES):
            return where
    return None


def is_node_dir(command: ResolvedCommand, entries: dict[str, Any]) -> bool:
    """Whether the check's directory is a Node project: its own lockfile or a workspace root's (:func:`node_lock_dir`)."""
    return node_lock_dir(command, entries) is not None


def keep_vanished_checkouts(removals: list[str]) -> tuple[list[str], list[str]]:
    """*removals* without every checkout whose record is among them, and those checkout roots.

    A command's read-back lists as removed what the container no longer holds. A checkout whose
    own record (``.xtm-checkout.json``) went with it was not removed by the command: the container
    lost its tree (a restart, a resumed run's stale baseline). Saving that removal deleted a whole
    checkout from the workspace (10,094 files after a resumed backend-unit run); it is kept instead,
    and the answer names it. A file removed inside a checkout whose record is still there stays a removal.
    """
    from app.services.sandbox_workspace.eviction import checkout_roots
    from app.services.sandbox_workspace.paths import is_under

    vanished = checkout_roots(removals)
    if not vanished:
        return removals, []
    kept = [path for path in removals if not any(is_under(path, root) for root in vanished)]
    return kept, vanished


def staged_entries(entries: dict[str, Any], cwd: str, linked: Iterable[str] = ()) -> tuple[dict[str, Any], str]:
    """The part of the workspace a command of *cwd* is staged with, and the checkout root it was cut to.

    A command whose directory is inside a checkout (a directory holding a checkout record) gets that
    checkout and the files outside every checkout, never the other checkouts: a workspace holding
    several large checkouts failed the upload where the one checkout passed. The checkouts in *linked*
    (the ones a manifest's relative path names, :func:`linked_checkouts`) are staged with it. A command
    outside every checkout (an argv at the workspace root) gets the whole workspace, root ``""``.
    """
    from app.services.sandbox_workspace.eviction import checkout_roots
    from app.services.sandbox_workspace.paths import is_under

    roots = checkout_roots(entries)
    holders = [root for root in roots if cwd and is_under(cwd, root)]
    if not holders:
        return entries, ""
    own = max(holders, key=len)
    kept_roots = set(linked)
    others = [root for root in roots if root != own and not is_under(own, root) and root not in kept_roots]
    kept = {path: entry for path, entry in entries.items() if not any(is_under(path, root) for root in others)}
    return kept, own


#: The manifests whose relative paths may name another checkout (a path dependency, a local package).
LINK_MANIFESTS = ("pyproject.toml", "package.json", "requirements.txt", "requirements-dev.txt", "setup.cfg")

#: A relative path written in a manifest: ``../x``, ``./x`` (a ``file:`` prefix is cut by the search itself).
_RELATIVE_PATH = re.compile(r"(?<![\w.])(\.\.?/[^\s\"',;\]\)}]+)")


def link_manifest_paths(entries: Mapping[str, Any], cwd: str, root: str) -> list[str]:
    """The manifests (:data:`LINK_MANIFESTS`) of *cwd* and of each directory up to the checkout *root*."""
    from app.services.sandbox_workspace.paths import is_under

    found: list[str] = []
    where = cwd.strip("/")
    while True:
        prefix = f"{where}/" if where else ""
        found.extend(f"{prefix}{name}" for name in LINK_MANIFESTS if f"{prefix}{name}" in entries)
        if not where or where == root or not is_under(where, root):
            return found
        where = posixpath.dirname(where)


def linked_checkouts(manifests: Mapping[str, str], own: str, roots: Iterable[str]) -> list[str]:
    """The checkouts other than *own* that a relative path in *manifests* (path to text) resolves into.

    OpenAEV injectors' ``nmap/pyproject.toml`` names ``pyoaev @ ../../client-python``: with
    ``client-python/`` checked out beside the injectors, the install needs both in the container.
    """
    from app.services.sandbox_workspace.paths import is_under

    others = [root for root in roots if root != own and not is_under(own, root)]
    linked: list[str] = []
    for path, text in manifests.items():
        base = posixpath.dirname(path)
        for match in _RELATIVE_PATH.finditer(text):
            target = posixpath.normpath(posixpath.join(base, match.group(1)))
            if target.startswith(".."):
                continue
            for root in others:
                if root not in linked and (is_under(target, root) or is_under(root, target)) and target:
                    linked.append(root)
    return sorted(linked)


async def build_output_ignores(workspace: Any, entries: dict[str, Any], roots: Iterable[str]) -> list[str]:
    """The ignore patterns of a command: :data:`IGNORED_PATTERNS` plus the build-output directories of each staged checkout.

    A checkout's own ``.gitignore`` names where its checks build (OpenCTI: ``build``, ``__generated__``,
    ``coverage``): left out of the reconcile and the read-back like ``node_modules/``, those thousands of
    untracked files stay in the container and never refuse a check's real writes at the file limit (#1639).
    A checkout whose record or rules cannot be read adds nothing.
    """
    from app.services.sandbox_workspace.checkout import RECORD_NAME, CheckoutRecord, CheckoutRecordError
    from app.services.sandbox_workspace.gitignore import build_output_patterns, gitignore_paths

    patterns: list[str] = list(IGNORED_PATTERNS)
    for root in roots:
        if not root:
            continue
        record_entry = entries.get(f"{root}/{RECORD_NAME}")
        if record_entry is None:
            continue
        try:
            _entry, data = await workspace.read_entry(record_entry)
            record = CheckoutRecord.from_json(data, path=root)
        except (CheckoutRecordError, FileNotFoundError, ValueError):
            continue
        except Exception:
            logger.warning("build output ignores: the record of %s could not be read", root, exc_info=True)
            continue
        rules: list[tuple[str, bytes]] = []
        for repo_path in gitignore_paths(p for p in map(record.repo_path, entries) if p):
            entry = entries.get(record.workspace_path(repo_path))
            if entry is None:
                continue
            try:
                _entry, rule_bytes = await workspace.read_entry(entry)
            except Exception:
                logger.warning("build output ignores: %s could not be read", entry.path, exc_info=True)
                continue
            rules.append((repo_path, rule_bytes))
        for pattern in build_output_patterns(root, rules, record.files):
            if pattern not in patterns:
                patterns.append(pattern)
    return patterns


async def _linked_checkouts(workspace: Any, entries: dict[str, Any], cwd: str) -> list[str]:
    """:func:`linked_checkouts` of the command's manifests, read from the workspace; ``[]`` on any read error."""
    from app.services.sandbox_workspace.eviction import checkout_roots
    from app.services.sandbox_workspace.paths import is_under

    roots = checkout_roots(entries)
    holders = [root for root in roots if cwd and is_under(cwd, root)]
    if not holders or len(roots) < 2:
        return []
    own = max(holders, key=len)
    manifests: dict[str, str] = {}
    for path in link_manifest_paths(entries, cwd, own):
        try:
            _entry, data = await workspace.read_entry(entries[path])
        except Exception:  # an unreadable manifest only means no linked checkout
            logger.debug("manifest %s unreadable for linked checkouts", path, exc_info=True)
            continue
        manifests[path] = data[:262_144].decode("utf-8", "replace")
    return linked_checkouts(manifests, own, roots)


def _largest_directories(entries: dict[str, Any], top: int = 3) -> str:
    """The workspace's largest top-level directories, the ones to prune when an upload fails on size."""
    sizes: dict[str, int] = {}
    for path, entry in entries.items():
        head, sep, _rest = path.partition("/")
        if sep:
            sizes[head] = sizes.get(head, 0) + entry.size
    if len(sizes) < 2:
        return ""
    ranked = sorted(sizes.items(), key=lambda item: (-item[1], item[0]))[:top]
    listed = ", ".join(f"{name}/ {size / 1e6:.1f} MB" for name, size in ranked)
    return f". Largest directories of the workspace: {listed}; a checkout this command does not need can be removed."


def _remote_cwd(command: ResolvedCommand) -> str:
    from app.services.sandbox_workspace.remote import REMOTE_WORKSPACE

    return f"{REMOTE_WORKSPACE}/{command.cwd}" if command.cwd else REMOTE_WORKSPACE


def _with_cwd(command: ResolvedCommand, cwd: str) -> ResolvedCommand:
    """*command* moved to directory *cwd* (the workspace root of a member, where its install state is)."""
    import dataclasses

    return dataclasses.replace(command, cwd=cwd)


async def _present(sandbox: Any, command: ResolvedCommand, paths: tuple[str, ...] | list[str]) -> set[str] | None:
    """Which of *paths* exist under the check's directory in the container; ``None`` when the probe failed."""
    from app.services.sandbox_workspace.remote import probe_files

    try:
        return await probe_files(sandbox, _remote_cwd(command), list(paths))
    except Exception:
        logger.debug("install state probe failed for %s", command.label, exc_info=True)
        return None


def remote_run_dir(run_id: str) -> str:
    """Where a detached run keeps its argv, baseline, output and end marker in the container (outside the workspace)."""
    from app.services.sandbox_workspace.remote import TOOL_DIR

    return f"{TOOL_DIR}/runs/{run_id}"


def _join(prefix: str, rel: str) -> str:
    return normalize(f"{prefix}/{rel}" if prefix and rel else (prefix or rel), allow_root=True)


@dataclass
class CheckoutChecks:
    """What can run on a checkout and where it comes from."""

    specs: dict[str, CheckSpec]
    #: ``declared`` (``.xtm-one/checks.toml``) or ``detected`` (:mod:`detect`).
    origin: str
    infra: list[detect.Infra]
    #: The checkout holds ``.xtm-one/checks.toml`` (a detected listing then means it declares services only).
    has_checks_file: bool = False

    def listing(self, root: str) -> str:
        if self.origin == "declared":
            head = f"Declared in {display(_join(root, CHECKS_FILE))}:"
        elif self.has_checks_file:
            head = (
                f"Detected from the repository's own files ({display(_join(root, CHECKS_FILE))} declares no "
                "[check.NAME] table, its services are listed below; the source of each check in parentheses):"
            )
        else:
            head = f"Detected from the repository's own files (no {CHECKS_FILE}; the source of each check in parentheses):"
        full = describe_checks(self.specs) or "(none)"
        if len(full) > LISTING_MAX_CHARS:
            # A 21k listing for one check name: the details stay one read away.
            source = display(_join(root, CHECKS_FILE)) if self.origin == "declared" else "the source named"
            full = (
                describe_checks(self.specs, compact=True)
                + f"\n({len(self.specs)} checks, compact: the argv, image, report and default args of each are in "
                f"{source}; sandbox_grep on the check name reads one)"
            )
        lines = [head, full]
        if self.infra:
            lines += ["Services its CI or compose files start beside the code:", detect.describe_infra(self.infra)]
        return "\n".join(lines)


async def checkout_checks(session: WorkspaceSession, root: str, *, images: set[str] | frozenset[str]) -> CheckoutChecks:
    """The checks of the checkout at *root*: its ``.xtm-one/checks.toml`` when it declares checks, else detected.

    A declared checkout also gets the detected ``actions-lint`` when it holds a
    workflow or a composite action and declares no check of that name. A
    ``checks.toml`` that declares services only (no ``check`` table) keeps the
    detected checks: a repository adds a lab without restating its CI.
    """
    entries = await session.entries(refresh=True)
    path = _join(root, CHECKS_FILE)
    prefix = f"{root}/" if root else ""
    listing = {p[len(prefix) :]: entry for p, entry in entries.items() if p.startswith(prefix)}
    text = None
    if path in entries:
        _entry, raw = await session.get_bytes(path)
        text = raw.decode("utf-8", errors="replace")
    if text is not None and declares_checks(text):
        declared = load_checks(text)
        actions = detect.actions_lint_check(listing, images=images)
        if actions is not None and actions.name not in declared:
            declared[actions.name] = actions
        return CheckoutChecks(declared, "declared", [])
    record = await session.record(create=False)
    files: dict[str, bytes] = {}
    for rel in detect.candidates(listing):
        entry = listing[rel]
        if record is None or entry.size > detect.MAX_FILE_BYTES:
            continue
        files[rel] = await session.store.read_blob(record.id, entry.sha256)
    found = detect.detect(listing, files, images=images)
    return CheckoutChecks(found.checks, "detected", found.infra, has_checks_file=text is not None)


async def _checks_file(session: WorkspaceSession, root: str) -> bytes | None:
    """The ``.xtm-one/checks.toml`` of the checkout at *root*, ``None`` when it has none."""
    path = _join(root, CHECKS_FILE)
    if path not in await session.entries():
        return None
    _entry, raw = await session.get_bytes(path)
    return raw


async def list_checks(session: WorkspaceSession, *, checkout: str, known_images: set[str] | frozenset[str]) -> str:
    """What ``sandbox_run_command`` without a check answers: the checks of *checkout*, and its declared services.

    A normal answer, not a refusal: :class:`ChecksError` only when the path is
    not a checkout path, ``checks.toml`` does not parse, or nothing can run.
    """
    try:
        root = normalize(checkout or "", allow_root=True)
    except WorkspacePathError as exc:
        raise ChecksError(str(exc)) from exc
    found = await checkout_checks(session, root, images=known_images)
    if not found.specs:
        raise ChecksError(_nothing(root, has_checks_file=found.has_checks_file))
    lines = [found.listing(root)]
    if SCRIPT_CHECK not in found.specs:
        lines.append(SCRIPT_LISTING)
    raw = await _checks_file(session, root)
    if raw is not None:
        try:
            services = load_services(raw.decode("utf-8", errors="replace"))
        except ChecksError as exc:
            lines.append(f"Its [service.NAME] tables do not load: {exc}")
        else:
            if services:
                lines += [
                    "Services it declares (sandbox_start_service):",
                    describe_services(services, budget=SERVICES_LISTING_MAX_CHARS),
                ]
    lines.append("Run one with check=NAME (args are appended to its argv, replacing its default args).")
    return "\n".join(lines)


async def resolve(
    session: WorkspaceSession,
    *,
    checkout: str,
    check: str = "",
    args: object = None,
    argv: object = None,
    cwd: str = "",
    allow_argv: bool,
    max_timeout: int,
    known_images: set[str] | frozenset[str],
    env: object = None,
) -> ResolvedCommand:
    """The command a call asks for, bound to *session*; :class:`ChecksError` when it cannot run.

    *env* (name to value) is set for this run over the check's own variables."""
    try:
        root = normalize(checkout or "", allow_root=True)
    except WorkspacePathError as exc:
        raise ChecksError(str(exc)) from exc
    given_env = env_override(env)
    resolved = await _resolve(
        session,
        root,
        check=check,
        args=args,
        argv=argv,
        cwd=cwd,
        allow_argv=allow_argv,
        max_timeout=max_timeout,
        known_images=known_images,
    )
    if not given_env:
        return resolved
    import dataclasses

    merged = {**dict(resolved.env), **given_env}
    return dataclasses.replace(
        resolved,
        env=tuple(sorted(merged.items())),
        env_unset=tuple(name for name in resolved.env_unset if name not in given_env),
    )


#: Variables a call's ``env`` may not set: the container's own, and the CI runner's.
_OWNED_ENV = frozenset(
    {"PATH", "HOME", "SHELL", "USER", "PWD", "TMPDIR", "WORKSPACE_DIR", "LD_PRELOAD", "LD_LIBRARY_PATH"}
)
_OWNED_ENV_PREFIXES = ("GITHUB_", "RUNNER_", "ACTIONS_")


def unset_env_note(names: tuple[str, ...]) -> str:
    """The answer line naming the variables CI sets from a secret or an expression that the run left unset."""
    shown = ", ".join(names[:12]) + (f" and {len(names) - 12} more" if len(names) > 12 else "")
    return (
        f"Unset here, set by CI from a secret or an expression: {shown}. A failure reading one of them is "
        'the sandbox, not the change: pass a placeholder with env (e.g. env={"NAME": "ci"}).'
    )


def env_override(env: object) -> dict[str, str]:
    """A call's ``env`` as name to value; :class:`ChecksError` naming what is wrong with it."""
    if env is None or env == {}:
        return {}
    if not isinstance(env, dict):
        raise ChecksError('env takes an object of variable names to values, e.g. {"PG_PASSWORD": "ci"}.')
    if len(env) > detect.MAX_ENV:
        raise ChecksError(f"env holds at most {detect.MAX_ENV} variables.")
    out: dict[str, str] = {}
    for key, raw in env.items():
        name = str(key)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ChecksError(f"env: {name!r} is not a variable name.")
        if name in _OWNED_ENV or name.startswith(_OWNED_ENV_PREFIXES):
            raise ChecksError(f"env: {name} belongs to the sandbox container or the CI runner, a call does not set it.")
        text = ("true" if raw else "false") if isinstance(raw, bool) else str(raw)
        if isinstance(raw, dict | list) or raw is None or len(text) > detect.MAX_ENV_CHARS or "\x00" in text:
            raise ChecksError(f"env: {name} takes a text value of at most {detect.MAX_ENV_CHARS} characters.")
        out[name] = text
    return out


async def _resolve(
    session: WorkspaceSession,
    root: str,
    *,
    check: str,
    args: object,
    argv: object,
    cwd: str,
    allow_argv: bool,
    max_timeout: int,
    known_images: set[str] | frozenset[str],
) -> ResolvedCommand:
    if check and argv:
        raise ChecksError("give check (a declared or detected check) or argv (a command), not both.")
    if not check:
        if not argv:
            found = await checkout_checks(session, root, images=known_images)
            if not found.specs:
                raise ChecksError(_nothing(root, has_checks_file=found.has_checks_file))
            raise ChecksError(f"check names the check to run (or argv a command, when the platform allows one). {found.listing(root)}")
        if not allow_argv:
            raise ChecksError(
                "an arbitrary argv is not allowed on this platform (CODE_INTERPRETER_COMMAND_ALLOW_ARGV): "
                "run one of the checkout's checks (declared or detected); sandbox_run_command without a check lists them."
            )
        items = extra_args(argv)
        if not items:
            raise ChecksError("argv is empty.")
        try:
            where = _join(root, normalize(cwd, allow_root=True)) if cwd else root
        except WorkspacePathError as exc:
            raise ChecksError(str(exc)) from exc
        return ResolvedCommand(
            check="", argv=tuple(items), cwd=where, timeout=min(DEFAULT_TIMEOUT, max_timeout), image=DEFAULT_IMAGE_KEY
        )
    found = await checkout_checks(session, root, images=known_images)
    if check == SCRIPT_CHECK and SCRIPT_CHECK not in found.specs:
        return await _script_command(
            session, root, found.specs, args=args, cwd=cwd, max_timeout=max_timeout, known_images=known_images
        )
    if not found.specs:
        raise ChecksError(_nothing(root, has_checks_file=found.has_checks_file))
    spec = found.specs.get(check)
    if spec is None:
        raise ChecksError(f"{check!r} is not a check of {_shown(root)}. {found.listing(root)}")
    image = spec.image or DEFAULT_IMAGE_KEY
    if image not in known_images:
        raise ChecksError(
            f"check {check!r} runs on image key {image!r}, which OPENSANDBOX_IMAGES does not configure "
            f"(configured: {', '.join(sorted(known_images)) or 'none'})."
        )
    where = _join(root, spec.cwd)
    given = extra_args(args or [])
    narrow: tuple[str, ...] = ()
    timeout = spec.timeout
    if spec.affected_narrows_args and given:
        # --whole is the platform's switch, never the check's: it leaves the args as given and is not passed on.
        if WHOLE_ARG in given:
            given = [a for a in given if a != WHOLE_ARG]
            if not given:
                raise ChecksError(f"{WHOLE_ARG} takes the paths to run on whole as args.")
        else:
            narrow = tuple(given)
    elif spec.affected_by_default and WHOLE_ARG in given:
        # A check that runs on the change by default (frontend-unit) runs whole only when asked: --whole alone
        # is that ask, bounded by the check's whole_timeout; with paths it is dropped and the paths run.
        given = [a for a in given if a != WHOLE_ARG]
        if not given:
            timeout = spec.whole_timeout or spec.timeout
    argv = command_argv(spec, given)
    install = None
    if spec.install_first and not is_install_name(check):
        install = _sibling_install(found.specs, spec, root=root, image=image, max_timeout=max_timeout)
    resolved = ResolvedCommand(
        check=check,
        argv=argv,
        cwd=where,
        timeout=min(timeout, max_timeout),
        image=image,
        report=spec.report,
        report_path=_join(where, spec.report_path) if spec.report_path else "",
        # Read from the call, not from argv's tail: a git_repo check wraps its argv (checks.GIT_REPO_SHELL).
        default_args=spec.default_args if tuple(given or spec.default_args) == spec.default_args else (),
        env=spec.env,
        env_unset=spec.env_unset,
        affected=spec.affected,
        affected_globs=spec.affected_globs,
        affected_always=spec.affected_always,
        affected_scan_patterns=spec.affected_scan_patterns,
        affected_by_default=spec.affected_by_default and not extra_args(args or []),
        narrow_args=narrow,
        affected_whole_repository=spec.affected_whole_repository,
        install=install,
    )
    install_spec = spec if is_install_name(check) else (found.specs.get(install.check) if install else None)
    return bind_fallback(resolved, install_spec, known_images=known_images)


async def _script_command(
    session: WorkspaceSession,
    root: str,
    specs: dict[str, CheckSpec],
    *,
    args: object,
    cwd: str = "",
    max_timeout: int,
    known_images: set[str] | frozenset[str],
) -> ResolvedCommand:
    """The built-in :data:`SCRIPT_CHECK` of the checkout at *root*: ``args[0]`` run, the rest its arguments.

    The file must be in the workspace (relative to the checkout, else to the workspace root; an absolute or
    ``..`` path is refused). It runs on the image of the deepest check of its stack whose directory holds
    the file (a non-install first among equals), with that check's env and the install of its directory bound,
    so a repro reads the dependencies the checkout's checks installed; else on the stack's first configured
    image. The cwd is the checkout root, or *cwd*, a directory of the checkout, when given: a module script
    that reads files relative to its package (``openaev-front/scripts/*.js``) runs from there. The program
    stays the one the extension names, so *cwd* opens no arbitrary argv.
    """
    given = extra_args(args or [])
    if not given:
        raise ChecksError(
            f"check {SCRIPT_CHECK!r} takes the file to run as its first arg, then the script's own args "
            '(e.g. args=["work/repro.py", "--flag"]).'
        )
    script = _script_path(given[0], root, await session.entries())
    runner = SCRIPT_RUNNERS.get(posixpath.splitext(script)[1].lower())
    if runner is None:
        raise ChecksError(
            f"check {SCRIPT_CHECK!r} runs a {', '.join(SCRIPT_RUNNERS)} file; {display(script)} is none of them."
        )
    stack, program = runner
    where = _script_cwd(cwd, root, await session.entries())
    prefix = f"{root}/" if root else ""
    inside = not root or is_under(script, root)
    directory = posixpath.dirname(script[len(prefix) :]) if inside else ""
    stack_images = detect.STACK_IMAGES[stack]
    anchors = [
        (_depth(spec.cwd), is_install_name(name), name)
        for name, spec in specs.items()
        if (spec.image or DEFAULT_IMAGE_KEY) in stack_images
        and (spec.cwd.strip("/") == directory or _encloses(spec.cwd, directory))
    ]
    env: tuple[tuple[str, str], ...] = ()
    if anchors:
        anchor = specs[min(anchors, key=lambda a: (-a[0], a[1], a[2]))[2]]
        image, env = anchor.image or DEFAULT_IMAGE_KEY, anchor.env
        runners = [
            tuple(spec.argv[:2])
            for spec in specs.values()
            if spec.cwd.strip("/") == anchor.cwd.strip("/") and tuple(spec.argv[:2]) in _PROJECT_RUNNERS
        ]
        if stack == "python" and runners:
            # The project's environment lives in the runner's venv, not in the image's python; the `uv run --with`
            # dependencies the directory's checks add (`uv run --with pyyaml pytest`) are the script's too.
            withs = (
                _uv_with_args(specs, anchor.cwd, run_from=_under(where, root)) if runners[0] == ("uv", "run") else ()
            )
            program = (*runners[0], *withs, *program)
        elif stack == "python":
            # The install of a project pinned to another interpreter (``python3.12 -m pip install``)
            # writes that interpreter's site-packages: the script runs on the same one.
            program = (_pinned_python(anchor, specs, anchor.env) or program[0], *program[1:])
    else:
        image = next((key for key in stack_images if key in known_images), stack_images[0])
    if image not in known_images:
        raise ChecksError(
            f"check {SCRIPT_CHECK!r} runs {display(script)} on image key {image!r}, which OPENSANDBOX_IMAGES does "
            f"not configure (configured: {', '.join(sorted(known_images)) or 'none'})."
        )
    install = _sibling_install(
        specs, CheckSpec(name=SCRIPT_CHECK, argv=(), cwd=directory), root=root, image=image, max_timeout=max_timeout
    )
    resolved = ResolvedCommand(
        check=SCRIPT_CHECK,
        argv=(*program, posixpath.relpath(script, where or "."), *given[1:]),
        cwd=where,
        timeout=min(DEFAULT_TIMEOUT, max_timeout),
        image=image,
        env=env,
        install=install,
    )
    return bind_fallback(resolved, specs.get(install.check) if install else None, known_images=known_images)


#: The ``uv run`` options that add a dependency to the run's environment, then whether their value is a path.
_UV_WITH_OPTIONS = {"--with": False, "--with-requirements": True, "--with-editable": True}


def _uv_with_args(specs: dict[str, CheckSpec], cwd: str, *, run_from: str = "") -> tuple[str, ...]:
    """The ``--with`` options the ``uv run`` checks of directory *cwd* pass before their command, each once.

    Read in check-name order; a path value (``--with-requirements requirements.txt``) is rewritten relative
    to *run_from*, the checkout directory :data:`SCRIPT_CHECK` runs from (its root by default). Reading stops
    at the first word that is not an option, so the command's own arguments are never taken.
    """
    out: list[str] = []
    seen: set[tuple[str, str]] = set()
    directory = cwd.strip("/")
    for name in sorted(specs):
        spec = specs[name]
        if spec.cwd.strip("/") != directory or tuple(spec.argv[:2]) != ("uv", "run"):
            continue
        words = list(spec.argv[2:])
        while words and words[0].startswith("-"):
            option, _, inline = words.pop(0).partition("=")
            if option not in _UV_WITH_OPTIONS:
                continue
            value = inline or (words.pop(0) if words else "")
            if not value:
                break
            if _UV_WITH_OPTIONS[option] and (directory or run_from) and not posixpath.isabs(value):
                value = posixpath.relpath(posixpath.join(directory or ".", value), run_from or ".")
            if (option, value) not in seen:
                seen.add((option, value))
                out += [option, value]
    return tuple(out)


#: A versioned interpreter of a detected Python check's argv (``python3.12``), the one its install used.
_VERSIONED_PYTHON = re.compile(r"^python3\.\d+$")


def _pinned_python(anchor: CheckSpec, specs: dict[str, CheckSpec], env: tuple[tuple[str, str], ...]) -> str:
    """The versioned interpreter the checks of *anchor*'s directory run (``""`` when they run the image's own).

    Read from the argv of *anchor* first, then of any check of its directory (its install), then from the
    ``UV_PYTHON`` its env carries.
    """
    same = [anchor, *(spec for spec in specs.values() if spec.cwd.strip("/") == anchor.cwd.strip("/"))]
    for spec in same:
        if spec.argv and _VERSIONED_PYTHON.match(spec.argv[0]):
            return spec.argv[0]
    version = dict(env).get("UV_PYTHON", "")
    return f"python{version}" if re.fullmatch(r"3\.\d+", version) else ""


def _under(path: str, root: str) -> str:
    """*path* relative to the checkout *root* (``""`` for the root itself). Pure."""
    if not root:
        return path
    return "" if path == root else path[len(root) + 1 :]


def _script_cwd(raw: str, root: str, entries: dict[str, Any]) -> str:
    """The workspace directory :data:`SCRIPT_CHECK` runs from: the checkout root, or *raw* under it.

    *raw* is relative to the checkout and must hold at least one file of the workspace; an absolute or
    ``..`` path is refused, so the script never runs outside the checkout.
    """
    if not str(raw or "").strip():
        return root
    try:
        relative = normalize(str(raw).strip().strip("/") or ".", allow_root=True)
    except WorkspacePathError as exc:
        raise ChecksError(f"check {SCRIPT_CHECK!r}: cwd {raw!r}: {exc}") from exc
    where = _join(root, relative) if relative not in ("", ".") else root
    if where != root and not any(is_under(str(path), where) for path in entries):
        raise ChecksError(
            f"check {SCRIPT_CHECK!r}: cwd {raw!r} is not a directory of the checkout (nothing under "
            f"{display(where)}); give a directory relative to the checkout root."
        )
    return where


def _script_path(raw: str, root: str, entries: dict[str, Any]) -> str:
    """The workspace path of the file :data:`SCRIPT_CHECK` runs: *raw* under the checkout, else under the workspace."""
    try:
        path = normalize(raw)
    except WorkspacePathError as exc:
        raise ChecksError(f"check {SCRIPT_CHECK!r}: {exc}") from exc
    candidates = list(dict.fromkeys([_join(root, path), path] if root else [path]))
    for candidate in candidates:
        if candidate in entries:
            return candidate
    raise ChecksError(
        f"check {SCRIPT_CHECK!r}: {raw!r} is not a file of the workspace (looked for "
        f"{', '.join(display(c) for c in candidates)}); write it first (sandbox_write_file)."
    )


def bind_fallback(
    command: ResolvedCommand, install_spec: CheckSpec | None, *, known_images: set[str] | frozenset[str]
) -> ResolvedCommand:
    """*command* with the fallback its install check declares, or unchanged when that install declares none.
    A fallback image the platform does not configure is bound too: the failure is then named, not rerun."""
    import dataclasses

    if install_spec is None or not install_spec.fallback_image:
        return command
    image = install_spec.fallback_image
    return dataclasses.replace(
        command,
        fallback_image=image,
        fallback_configured=image in known_images,
        fallback_when=install_spec.fallback_when,
        fallback_reason=install_spec.fallback_reason,
    )


def falls_back(command: ResolvedCommand, outcome: CommandOutcome) -> bool:
    """Whether *outcome* is the failure *command*'s install fallback is declared for: an ended run that
    exited non-zero (not stopped, timed out, refused, busy or still running) with ``fallback_when`` in its text."""
    if not command.fallback_when or outcome.exit_code in (None, 0):
        return False
    if outcome.running or outcome.refused or outcome.busy or outcome.starting or outcome.stopped or outcome.timed_out:
        return False
    return command.fallback_when in outcome.text


#: ``kernel_state`` key of the installs that fell back in a workspace: install cache key -> {image, at}.
INSTALL_FALLBACKS_KEY = "install_fallbacks"


def _on_fallback_image(command: ResolvedCommand) -> ResolvedCommand:
    """*command* (and its install) on its fallback image, with no fallback left to take."""
    import dataclasses

    image = command.fallback_image
    return dataclasses.replace(
        command,
        image=image,
        install=None if command.install is None else dataclasses.replace(command.install, image=image),
        fallback_image="",
        fallback_configured=False,
        fallback_when="",
        fallback_reason="",
    )


async def _fallback_key(command: ResolvedCommand, workspace: WorkspaceSession) -> str | None:
    """The install cache key of *command*'s install on its own image (``None``: no install, no lockfile)."""
    install = command if is_install(command) else command.install
    if install is None:
        return None
    with contextlib.suppress(Exception):
        return install_cache_key(install, await workspace.entries())
    return None


async def _fallen_back(workspace: WorkspaceSession, key: str | None) -> str:
    """The image the install of *key* fell back to earlier in *workspace* (``""``: none recorded)."""
    if not key:
        return ""
    with contextlib.suppress(Exception):
        held = ((await workspace.latest_kernel_state() or {}).get(INSTALL_FALLBACKS_KEY) or {}).get(key) or {}
        return str(held.get("image") or "")
    return ""


async def _remember_fallback(workspace: WorkspaceSession, key: str | None, image: str) -> None:
    """Record that the install of *key* runs on *image* in *workspace*, so the next check bound to it starts there."""
    if not key:
        return

    def change(state: dict) -> dict | None:
        held = dict(state.get(INSTALL_FALLBACKS_KEY) or {})
        if (held.get(key) or {}).get("image") == image:
            return None
        held[key] = {"image": image, "at": datetime.now(UTC).isoformat()}
        return {**state, INSTALL_FALLBACKS_KEY: held}

    with contextlib.suppress(Exception):
        await workspace.update_kernel_state(change)


def _install_passed(command: ResolvedCommand, outcome: CommandOutcome) -> bool:
    """Whether *outcome* shows *command*'s install ended well (the command itself may still have failed)."""
    if (
        outcome.running
        or outcome.refused
        or outcome.busy
        or outcome.starting
        or outcome.lost
        or outcome.exit_code is None
    ):
        return False
    if outcome.exit_code == 0:
        return True
    if is_install(command):
        return False
    return not (outcome.command is not None and is_install(outcome.command))


async def run_with_fallback(command: ResolvedCommand, **call: Any) -> CommandOutcome:
    """:func:`run_command`, then, when its install failed as its ``fallback`` declares, the same command on
    the fallback image in the same call, the answer opening with one line that names the cause.

    The first failure is audited and its output kept in its run directory; a fallback image the platform
    does not configure gets the line alone. A run answered ``running`` is not followed (its poll answers it).

    A fallback that installed is remembered in the workspace (``kernel_state["install_fallbacks"]``, keyed by
    the install's cache key: its image, directory, argv and lockfile hashes): every later check bound to that
    install goes to the fallback image at once, the container that holds the install and what earlier checks
    built there (a ``dist`` a check reads), instead of failing the install on its own image again or starting
    on a container that never installed. A changed lockfile is a new key: the declared image is tried first.
    """
    workspace = call["workspace"]
    key = await _fallback_key(command, workspace) if command.fallback_image and command.fallback_configured else None
    remembered = await _fallen_back(workspace, key)
    if remembered and remembered == command.fallback_image:
        install = command if is_install(command) else command.install
        label = install.label if install is not None else command.label
        outcome = await run_command(_on_fallback_image(command), **call)
        outcome.text = (
            f"Ran {command.label} on {remembered}: {label} fell back there earlier in this workspace "
            f"({command.fallback_reason or 'its declared fallback'}; lockfiles unchanged).\n{outcome.text}"
        )
        return await _name_locks(outcome, command, workspace, image=remembered)
    outcome = await run_command(command, **call)
    return await _follow_fallback(command, outcome, key=key, **call)


async def _follow_fallback(
    command: ResolvedCommand, outcome: CommandOutcome, *, key: str | None = None, **call: Any
) -> CommandOutcome:
    """*outcome* of *command* as it ended, or, when it is the failure its install ``fallback`` is declared for,
    the same command on the fallback image (the tail of :func:`run_with_fallback`, shared with :func:`poll_run`:
    a run answered ``running`` first, ``background=true`` included, falls back when its poll sees it end)."""
    workspace = call["workspace"]
    if not falls_back(command, outcome):
        return await _name_locks(outcome, command, workspace, image=command.image)
    if key is None and command.fallback_configured:
        key = await _fallback_key(command, workspace)
    failed = outcome.command or command.install or command
    cause = command.fallback_reason or f"its output holds {command.fallback_when!r}"
    where = f" (output in {display(outcome.run_dir)})" if outcome.run_dir else ""
    if not command.fallback_configured:
        outcome.text = (
            f"{failed.label} failed on {command.image}: {cause}. Its declared fallback image "
            f"{command.fallback_image!r} is not configured on this platform (OPENSANDBOX_IMAGES).\n{outcome.text}"
        )
        return await _name_locks(outcome, command, workspace, image=command.image)
    with contextlib.suppress(Exception):
        await audit_run(
            user_id=call.get("user_id"), workspace_id=workspace.workspace_id, command=failed, outcome=outcome
        )
    again = await run_command(_on_fallback_image(command), **{**call, "background": False})
    if _install_passed(command, again):
        await _remember_fallback(workspace, key, command.fallback_image)
    again.text = (
        f"{failed.label} failed on {command.image}{where}: {cause}. "
        f"Ran {command.label} again on {command.fallback_image}, the fallback {failed.label} declares.\n{again.text}"
    )
    return await _name_locks(again, command, workspace, image=command.fallback_image)


def _sibling_install(
    specs: dict[str, CheckSpec], spec: CheckSpec, *, root: str, image: str, max_timeout: int
) -> ResolvedCommand | None:
    """The install check of *spec*'s directory on its image, bound like a call with no args.

    The install of the same directory wins (the first by name); without one, the install of the
    nearest enclosing directory: a Yarn, npm or pnpm workspace installs every package from its
    root, so ``packages/x:node-unit`` binds the root ``node-install`` (a check of a package of a
    monorepo otherwise ran on a container with no node_modules and failed with Yarn's
    "Couldn't find the node_modules state file").
    """
    candidates = [
        (_depth(other.cwd), name)
        for name, other in specs.items()
        if name != spec.name
        and is_install_name(name)
        and (other.image or DEFAULT_IMAGE_KEY) == image
        and (other.cwd.strip("/") == spec.cwd.strip("/") or _encloses(other.cwd, spec.cwd))
    ]
    if not candidates:
        return None
    # The deepest directory first (the same directory before an enclosing one), then the first by name.
    _depth_of, name = min(candidates, key=lambda c: (-c[0], c[1]))
    other = specs[name]
    return ResolvedCommand(
        check=name,
        argv=command_argv(other, None),
        cwd=_join(root, other.cwd),
        timeout=min(other.timeout, max_timeout),
        image=image,
        default_args=other.default_args,
        env=other.env,
    )


def install_check_for(specs: dict[str, CheckSpec], directory: str) -> str:
    """The install check whose install writes the installed tree of *directory* (``""`` for the checkout root).

    The install of that directory wins (the first by name); without one, the install of the
    nearest enclosing directory, as :func:`_sibling_install` binds it. ``""`` when none.
    """
    candidates = [
        (_depth(other.cwd), name)
        for name, other in specs.items()
        if is_install_name(name)
        and (other.cwd.strip("/") == directory.strip("/") or _encloses(other.cwd, directory))
    ]
    if not candidates:
        return ""
    return min(candidates, key=lambda c: (-c[0], c[1]))[1]


def _depth(directory: str) -> int:
    """How many path segments *directory* has below the checkout root (0 for the root)."""
    stripped = directory.strip("/")
    return len(stripped.split("/")) if stripped else 0


def _encloses(parent: str, child: str) -> bool:
    """Whether directory *parent* (``""`` for the checkout root) strictly encloses *child*."""
    parent = parent.strip("/")
    child = child.strip("/")
    if parent == child:
        return False
    return parent == "" or child.startswith(parent + "/")


def _shown(root: str) -> str:
    return display(root).rstrip("/") + "/"


def _nothing(root: str, *, has_checks_file: bool = False) -> str:
    why = f"its {CHECKS_FILE} has no [check.NAME] table" if has_checks_file else f"no {CHECKS_FILE}"
    return (
        f"{_shown(root)} declares no check ({why}) and none was detected from its files: "
        "materialize_repository the whole tree, or with the directories of its manifests and .github/workflows "
        "among sub_paths (package.json, pyproject.toml, pom.xml, build.gradle, Cargo.toml, dbt_project.yml, Makefile)."
    )


def _default_api(user_id: uuid.UUID | None) -> Any:
    from opensandbox import Sandbox

    from app.core.config import settings
    from app.services._sandbox_selector import _build_opensandbox_config, _create_opensandbox, sandbox_pool_state
    from app.services.code_interpreter import MAX_IMAGES
    from app.services.sandbox_workspace.paths import WORKSPACE_ENV
    from app.services.sandbox_workspace.registry_mirror import command_egress, command_env
    from app.services.sandbox_workspace.remote import REMOTE_WORKSPACE

    async def _connect(sandbox_id: str):
        return await Sandbox.connect(sandbox_id, connection_config=_build_opensandbox_config())

    async def _create(image_key: str, lifetime: float):
        return await _create_opensandbox(
            image_key=image_key,
            timeout_seconds=int(lifetime),
            kind_label="sandbox-command",
            usage_type="sandbox_command",
            user_id=user_id,
            # Package installs reach the read-only registry mirror only, when one is set.
            egress_allow_list=command_egress(),
            resource=settings.sandbox_resources(image_key),
            env={
                **command_env(),
                "MPLBACKEND": "Agg",
                "COPILOT_MAX_IMAGES": str(MAX_IMAGES),
                WORKSPACE_ENV: REMOTE_WORKSPACE,
            },
            # The create runs as its own task past the call (``_created``): a pod-ready timeout
            # (HTTP 504, a cold Pool pulling the image) is retried until this window, not twice.
            retry_timeouts_for=CREATE_RETRY_WINDOW_SECONDS,
        )

    async def _pool_state(image_key: str):
        return await sandbox_pool_state(image_key)

    return SimpleNamespace(connect=_connect, create=_create, pool_state=_pool_state)


class ContainerStartingError(Exception):
    """The container is still being created when the call's wait budget ends; the create goes on."""

    def __init__(self, seconds: float) -> None:
        super().__init__(f"container still starting after {seconds:.0f} s")
        self.seconds = seconds


#: Creates still running after the call that started them answered, per (workspace id, image key):
#: a cold image (a node pulling several GB, the server's own create timeout and one retry on its
#: HTTP 504) can outlive one tool call. The next call on the workspace takes the result from here.
_CREATES: dict[tuple[str, str], tuple[asyncio.Task[Any], float]] = {}
#: Seconds after which a create nobody came back for is dropped (its container ends at its lifetime).
CREATE_ABANDONED_SECONDS = 3600
#: Seconds a background create keeps retrying the server's pod-ready timeout (HTTP 504) before it
#: fails: long enough for a cold node to pull a multi-GB image, well inside the abandon window.
CREATE_RETRY_WINDOW_SECONDS = 900.0

#: The claims on a command container, per (event loop, workspace, image key): a lock and the calls holding or
#: waiting for it. A :func:`run_command` call holds it from its read of the run the container holds to the record
#: of its own run (the upload, the install probe and the start between them), so two calls of one round on one
#: checkout (frontend-unit and frontend-types, node-types and node-lint-file) can no longer both read "nothing
#: runs here", both find no ``node_modules`` and both run the install in the same container, the slower one
#: failing in the link step (ENOENT, EEXIST): the second reads the first's run and queues behind it. Process-local:
#: the calls of one turn run in one worker.
_CLAIMS: dict[tuple[int, str, str], list[Any]] = {}


class _Claim:
    """The claim of one :func:`run_command` call on its container (:data:`_CLAIMS`); released once, idempotently."""

    def __init__(self) -> None:
        self._key: tuple[int, str, str] | None = None
        self._held = False

    async def take(self, workspace: WorkspaceSession, image: str, timeout: float) -> bool:
        """Wait up to *timeout* seconds for the container of *image*; ``False`` when another call kept it longer."""
        owner = getattr(workspace, "workspace_id", None)
        key = (id(asyncio.get_running_loop()), str(owner) if owner is not None else f"session-{id(workspace)}", image)
        entry = _CLAIMS.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1
        self._key = key
        try:
            await asyncio.wait_for(entry[0].acquire(), max(0.1, timeout))
        except TimeoutError:
            self._drop()
            return False
        except BaseException:
            self._drop()
            raise
        self._held = True
        return True

    def release(self) -> None:
        """Free the container for the next call (a no-op when not held or already released)."""
        if self._held and self._key is not None and self._key in _CLAIMS:
            self._held = False
            _CLAIMS[self._key][0].release()
        self._drop()

    def _drop(self) -> None:
        key, self._key = self._key, None
        entry = _CLAIMS.get(key) if key is not None else None
        if entry is None:
            return
        entry[1] -= 1
        if entry[1] <= 0:
            _CLAIMS.pop(key, None)


def selection_deadline(clock: Clock = time.monotonic) -> float:
    """The *clock* instant an ``affected=true`` selection stops reading test files.

    :data:`SELECTION_MAX_SECONDS` at most, and never later than what leaves
    :data:`SELECTION_LEAVES_SECONDS` of :func:`wait_budget` to the command (5 s at least).
    """
    room = wait_budget(clock) - SELECTION_LEAVES_SECONDS
    return clock() + max(5.0, min(SELECTION_MAX_SECONDS, room))


async def _pool_words(api: Any, image: str) -> str:
    """The pool state of *image* in one sentence (``api.pool_state``), ``""`` when unknown; never raises."""
    from app.services._sandbox_selector import pool_state_words

    reader = getattr(api, "pool_state", None)
    if reader is None:
        return ""
    try:
        return pool_state_words(image, await reader(image))
    except Exception:
        return ""


async def _create_and_remember(workspace: WorkspaceSession, image: str, api: Any, lifetime: float) -> Any:
    sandbox = await api.create(image, lifetime)
    entry = {"sandbox_id": str(sandbox.id), "started_at": datetime.now(UTC).isoformat()}

    def change(state: dict) -> dict:
        held = ((state.get("commands") or {}).get(image) or {}).get("run")
        changed = {**state, "commands": {**(state.get("commands") or {}), image: entry}}
        if isinstance(held, dict) and held.get("run_id"):
            # The run the replaced container held went with it: kept, so a later poll of its run_id (from any
            # call, any worker) reruns it once (poll_run), never answers a bare "lost".
            changed[LOST_RUNS] = _bounded_links(state.get(LOST_RUNS), str(held["run_id"]), held, _MAX_RERUNS)
        return changed

    await workspace.update_kernel_state(change)
    return sandbox


async def _created(workspace: WorkspaceSession, record: Any, image: str, api: Any, lifetime: float, budget: float | None) -> Any:
    """The container a create (this call's, or one an earlier call left running) gives, within *budget* seconds.

    The create runs as its own task, shielded from the call: when *budget* ends first,
    :class:`ContainerStartingError` is raised and the create goes on, recording the
    container in ``kernel_state`` when it is up, where the next call finds it.
    """
    now = time.monotonic()
    for stale in [k for k, (_t, at) in _CREATES.items() if now - at > CREATE_ABANDONED_SECONDS]:
        _CREATES.pop(stale, None)
    key = (str(record.id), image)
    held = _CREATES.get(key)
    if held is None:
        created = asyncio.create_task(_create_and_remember(workspace, image, api, lifetime))
        # A failure nobody comes back for is still retrieved (no "exception never retrieved" at shutdown).
        created.add_done_callback(lambda t: t.cancelled() or t.exception())
        held = (created, now)
        _CREATES[key] = held
    task, started = held
    try:
        if budget is None:
            sandbox = await asyncio.shield(task)
        else:
            sandbox = await asyncio.wait_for(asyncio.shield(task), timeout=max(0.0, budget))
    except TimeoutError:
        if task.done():
            _CREATES.pop(key, None)
            return task.result()
        raise ContainerStartingError(time.monotonic() - started) from None
    except BaseException:
        if task.done():
            _CREATES.pop(key, None)
        raise
    _CREATES.pop(key, None)
    return sandbox


async def _container(
    workspace: WorkspaceSession, image: str, api: Any, lifetime: float, *, budget: float | None = None
) -> tuple[Any, str]:
    """The workspace's command container for *image*, found again by id or created; its status.

    A create is waited for *budget* seconds at most (:func:`_created`).
    """
    from datetime import timedelta

    record = await workspace.record(create=True)
    if (str(record.id), image) in _CREATES:
        # An earlier call's create still running or done and not taken: that container, not a second one.
        return await _created(workspace, record, image, api, lifetime, budget), "new"
    state = dict(record.kernel_state or {})
    held = (state.get("commands") or {}).get(image) or {}
    sandbox = None
    status = "new"
    if held.get("sandbox_id"):
        try:
            sandbox = await api.connect(held["sandbox_id"])
            await sandbox.renew(timedelta(seconds=lifetime))
            status = "reused"
        except Exception:
            sandbox = None
            status = "restarted"
    if sandbox is None:
        sandbox = await _created(workspace, record, image, api, lifetime, budget)
    return sandbox, status


async def _forget(workspace: WorkspaceSession, image: str) -> None:
    record = await workspace.record(create=False)
    if record is None or not record.kernel_state:
        return

    def change(state: dict) -> dict | None:
        commands = dict(state.get("commands") or {})
        if commands.pop(image, None) is None:
            return None
        return {**state, "commands": commands}

    await workspace.update_kernel_state(change)


async def _release_to_idle(sandbox: Any, command: ResolvedCommand) -> None:
    """Bring the container's lifetime back to the idle window once nothing runs in it.

    A create or a reuse gives the container the check's whole run lifetime (timeout
    + margin, 1890 s for an 1800 s check) so a long command is never cut. Kept after
    the command ended, that lifetime held a Pool slot for half an hour after a 30 s
    run: a few workspaces filled the dev Pool and the next create waited until the
    server's HTTP 504. The container stays reusable (install cache) for the idle
    window, as a reused one does. Best effort: a failed renew leaves the longer lifetime.
    """
    from datetime import timedelta

    from app.core.config import settings

    idle = float(max(30, int(settings.code_interpreter_kernel_idle_seconds)))
    if float(command.timeout) + HARD_TIMEOUT_MARGIN + 60 <= idle:
        return
    with contextlib.suppress(Exception):
        await sandbox.renew(timedelta(seconds=idle))


def _lines(messages: Any) -> str:
    return "\n".join(getattr(line, "text", str(line)) for line in (messages or []))


async def run_command(
    command: ResolvedCommand,
    *,
    workspace: WorkspaceSession,
    user_id: uuid.UUID | None = None,
    quota_user_id: uuid.UUID | None = None,
    db: Any = None,
    sandbox_api: Any = None,
    sleep: Sleep = asyncio.sleep,
    background: bool = False,
    _reaped: bool = False,
    _rerun_install: ResolvedCommand | None = None,
) -> CommandOutcome:
    """Run *command* detached in the workspace's container of its image; the outcome, never an exception.

    The outcome is ``running`` (with its ``run_id``) when the command has not
    ended within :func:`wait_budget`: :func:`poll_run` waits for it again.
    With *background* the call waits only :data:`BACKGROUND_FIRST_LINES_SECONDS`
    for the first lines, so it answers ``running`` at once; :func:`poll_run` (with
    ``stop=True`` to end it) follows it with its ``run_id``.
    A run the container holds whose process already exited (its turn was cut before
    anyone polled it) is collected first and the command starts: never answered busy.
    *sandbox_api* stands in for the SDK (``connect(id)``, ``create(image_key, lifetime)``) in tests.
    *_rerun_install* is the install a check runs right after (its install ran first in this container, so
    *command* carries none): it is kept in the run record only, so a rerun after a container restart installs
    the new container first instead of running the check with no dependencies.
    """
    from app.services.quota import (
        QUOTA_EXCEEDED_MESSAGE,
        QUOTA_UNAVAILABLE_MESSAGE,
        QuotaCheckUnavailableError,
        QuotaExceededError,
        enforce_combined_sandbox_quota,
    )
    effective_quota_user = quota_user_id if quota_user_id is not None else user_id
    try:
        await enforce_combined_sandbox_quota(effective_quota_user)
    except QuotaExceededError:
        return CommandOutcome(text=QUOTA_EXCEEDED_MESSAGE, refused=True)
    except QuotaCheckUnavailableError:
        return CommandOutcome(text=QUOTA_UNAVAILABLE_MESSAGE, refused=True)

    # One call at a time sets a container up (held run read, upload, install probe, start): a sibling call of
    # the same round then reads this one's run and queues behind it instead of racing its install (_CLAIMS).
    claim = _Claim()
    if not await claim.take(workspace, command.image, max(1.0, wait_budget() - UPLOAD_RESERVE_SECONDS)):
        return CommandOutcome(
            text=(
                f"Another call of this workspace is still setting up its {command.image!r} container (upload, "
                f"install probe or start); {command.label} did not start. Call sandbox_run_command again with "
                "the same check: it then queues behind that call's run."
            ),
            busy=True,
        )
    try:
        return await _run_claimed(
            command,
            claim,
            workspace=workspace,
            user_id=user_id,
            quota_user_id=quota_user_id,
            effective_quota_user=effective_quota_user,
            db=db,
            sandbox_api=sandbox_api,
            sleep=sleep,
            background=background,
            _reaped=_reaped,
            _rerun_install=_rerun_install,
        )
    finally:
        claim.release()


async def _run_claimed(
    command: ResolvedCommand,
    claim: _Claim,
    *,
    workspace: WorkspaceSession,
    user_id: uuid.UUID | None,
    quota_user_id: uuid.UUID | None,
    effective_quota_user: uuid.UUID | None,
    db: Any,
    sandbox_api: Any,
    sleep: Sleep,
    background: bool,
    _reaped: bool,
    _rerun_install: ResolvedCommand | None,
) -> CommandOutcome:
    """The body of :func:`run_command` once its *claim* on the container is held.

    The claim is released before any wait on another run or any nested call (which takes it again), and right
    after this call's own run is recorded, so the run itself never holds it.
    """
    from app.services.sandbox_workspace.remote import first_leaf, install_tool, reconcile

    api = sandbox_api if sandbox_api is not None else _default_api(user_id)
    from app.core.config import settings

    idle = float(max(30, int(settings.code_interpreter_kernel_idle_seconds)))
    lifetime = max(idle, float(command.timeout) + HARD_TIMEOUT_MARGIN + 60)
    create_started = time.monotonic()
    try:
        # The create may take the call's budget less what the upload and the start need after it.
        create_budget = max(1.0, wait_budget() - UPLOAD_RESERVE_SECONDS)
        sandbox, status = await _container(workspace, command.image, api, lifetime, budget=create_budget)
        created_at = time.monotonic()
    except ContainerStartingError as exc:
        pool = await _pool_words(api, command.image)
        return CommandOutcome(
            text=(
                f"{command.label}: the {command.image!r} container is still starting ({exc.seconds:.0f} s: a cold "
                "image is being pulled, or the sandbox server is waiting for its pod); nothing ran yet and the "
                "workspace was not sent. The create goes on (a pod-ready timeout is retried for up to "
                f"{CREATE_RETRY_WINDOW_SECONDS / 60:.0f} min): call sandbox_run_command again with the same check "
                "in a minute, it uses that container once it is up; when nothing is running for this image key, "
                "push and let CI check instead of waiting." + (f" {pool}" if pool else "")
            ),
            starting=True,
            container="starting",
        )
    except Exception as exc:
        return CommandOutcome(
            text=(
                f"Error: the command container could not be created (stage create, image key {command.image!r}, "
                f"{time.monotonic() - create_started:.0f} s; the workspace was not sent yet): {format_exception_message(exc)}"
            ),
            refused=True,
        )

    held_run = await _held_run(workspace, command.image) if status == "reused" else None
    held_ended = False
    if held_run is not None and not _reaped:
        # The held run's process may have exited while nobody polled it (its turn cut by a worker loss).
        held_ended = await _ended(sandbox, remote_run_dir(str(held_run.get("run_id")))) is not None
    if held_run is not None:
        with contextlib.suppress(Exception):
            await sandbox.close()
        # Every way out of here waits on the held run or calls again (which claims again): free the container.
        claim.release()
        # The same check asked again is that run only for the tree it was staged with: a file of its checkout
        # edited since it started (a fix written after a resumed turn's replay) makes it another run.
        same = _same_command(held_run, command) and not await _edited_since_start(workspace, held_run, command.cwd)
        if not same and (held_ended or not background):
            # Its process exited: collect its result (saved under .xtm/runs, the container released), then start.
            # Still running (another check of this checkout, an install-first of a sibling call): this call queues
            # behind it within its own budget instead of racing it or answering busy at once.
            reaped = await poll_run(
                str(held_run.get("run_id")),
                workspace=workspace,
                user_id=user_id,
                quota_user_id=quota_user_id,
                db=db,
                sandbox_api=api,
                sleep=sleep,
            )
            if not reaped.running:
                outcome = await run_command(
                    command,
                    workspace=workspace,
                    user_id=user_id,
                    quota_user_id=quota_user_id,
                    db=db,
                    sandbox_api=api,
                    sleep=sleep,
                    background=background,
                    _reaped=True,
                    _rerun_install=_rerun_install,
                )
                held_id = str(held_run.get("run_id"))
                held_check = (held_run.get("command") or {}).get("check") or "argv"
                outcome.text = (
                    (
                        f"(Run {held_id} had ended without being collected: its result was saved in "
                        f"{display(f'{RUNS_DIR}/{held_id}')}/ and its container released.)\n{outcome.text}"
                    )
                    if held_ended
                    else (
                        f"(Queued behind {held_check}, run {held_id}, which held this container; it ended and its "
                        f"result was saved in {display(f'{RUNS_DIR}/{held_id}')}/.)\n{outcome.text}"
                    )
                )
                return outcome
        if same:
            # The same check asked again (a resumed turn that did not know its run_id): that run, waited for.
            polled = await poll_run(
                str(held_run.get("run_id")),
                workspace=workspace,
                user_id=user_id,
                quota_user_id=quota_user_id,
                db=db,
                sandbox_api=api,
                sleep=sleep,
            )
            if not polled.lost:
                return polled
            # Its run went with a container restart (the poll cleared it): the replayed check starts anew, once.
            return await run_command(
                command,
                workspace=workspace,
                user_id=user_id,
                quota_user_id=quota_user_id,
                db=db,
                sandbox_api=api,
                sleep=sleep,
                background=background,
                _rerun_install=_rerun_install,
            )
        # One command per container: another one is answered busy, a state and not a failure of the call.
        return CommandOutcome(
            text=(
                f"A command is still running in this workspace's {command.image!r} container "
                f"({held_run.get('command', {}).get('check') or 'argv'}, run_id {held_run.get('run_id')}, "
                f"{_since(held_run):.0f} s); {command.label} did not start. Wait for it with "
                f"sandbox_run_command(run_id=\"{held_run.get('run_id')}\") (stop=true as well ends it), then run "
                f"{command.label}."
            ),
            busy=True,
            run_id=str(held_run.get("run_id") or ""),
            container=status,
        )

    run_id = uuid.uuid4().hex[:12]
    all_entries = await workspace.entries(refresh=True)
    # Only the checkout the command runs from (and the files outside every checkout) goes to the container.
    linked = await _linked_checkouts(workspace, all_entries, command.cwd)
    entries, staged_root = staged_entries(all_entries, command.cwd, linked)
    size_mb = sum(e.size for e in entries.values()) / 1e6
    total_mb = sum(e.size for e in all_entries.values()) / 1e6
    linked_words = "".join(f", {display(root)}/" for root in linked)
    staged_words = (
        f"staged {size_mb:.1f} MB: {display(staged_root)}/{linked_words} and the files outside every checkout, of a {total_mb:.1f} MB workspace"
        if staged_root
        else f"workspace {size_mb:.1f} MB"
    )
    stage, stage_started = "upload", time.monotonic()
    try:
        await install_tool(sandbox)
        ignore = await build_output_ignores(workspace, entries, [staged_root, *linked] if staged_root else [])
        ws_state = await reconcile(sandbox, workspace, entries, ignore=ignore)
        if command.install is not None:
            stage, stage_started = "install probe", time.monotonic()
            if await _needs_install(sandbox, command.install, ws_state.entries):
                with contextlib.suppress(Exception):
                    await sandbox.close()
                claim.release()
                return await _install_then(
                    command,
                    workspace=workspace,
                    user_id=user_id,
                    quota_user_id=quota_user_id,
                    db=db,
                    sandbox_api=api,
                    sleep=sleep,
                    background=background,
                )
        cache_key = install_cache_key(command, ws_state.entries)
        stale = ""
        if cache_key and status == "reused":
            stage, stage_started = "cache", time.monotonic()
            hit = await _cache_hit(sandbox, cache_key)
            if hit is not None:
                missing = await _missing_state(sandbox, command, hit)
                if not missing:
                    await _release_to_idle(sandbox, command)
                    with contextlib.suppress(Exception):
                        await sandbox.close()
                    return _cached(command, hit, status)
                stale = (
                    f"Not answered from the install cache: {', '.join(display_cwd(command, path) for path in missing)} "
                    f"(left by run {hit.get('run_id')}) is no longer in this container, so the install ran again."
                )
        stage, stage_started = "start", time.monotonic()
        await _start(sandbox, command, run_id, ws_state.entries)
        start_seconds = time.monotonic() - stage_started
        metrics.observe_command_stage("start", "ok", start_seconds)
        _annotate_tool_span(
            {
                "sandbox.image": command.image,
                "sandbox.container": status,
                "sandbox.create_ms": f"{(created_at - create_started) * 1000.0:.0f}",
                # Workspace upload (and install-cache probe) between the create and the start.
                "sandbox.upload_ms": f"{(stage_started - created_at) * 1000.0:.0f}",
                "sandbox.start_ms": f"{start_seconds * 1000.0:.0f}",
            }
        )
    except Exception as exc:
        if stage == "start":
            metrics.observe_command_stage("start", "error", time.monotonic() - stage_started)
        with contextlib.suppress(Exception):
            await sandbox.close()
        what = "the workspace could not be put in the container" if stage == "upload" else "the command could not be started"
        # A task group's own message ("1 sub-exception") names nothing: its first leaf does.
        cause = format_exception_message(first_leaf(exc))
        return CommandOutcome(
            text=(
                f"Error: {what} (stage {stage}, {staged_words}, "
                f"{time.monotonic() - stage_started:.0f} s): {cause}"
                + (_largest_directories(entries) if stage == "upload" else "")
            ),
            refused=True,
            container=status,
        )
    run = {
        "run_id": run_id,
        "started_at": time.time(),
        "container": status,
        "command": _command_record(command, rerun_install=_rerun_install),
        "cache_key": cache_key,
        # Kept with the run so the result names it, whichever call (this one or poll_run) waits for the end.
        "cache_note": stale,
        # The read-back leaves out what the reconcile left out: the checkout's build output too (#1639).
        "ignore": [p for p in ignore if p not in IGNORED_PATTERNS],
    }
    await _remember_run(workspace, command.image, run)
    # Recorded: a sibling call now reads this run and queues behind it; the run itself holds no claim.
    claim.release()
    # The job of the call that asked for this check: a resumed turn's repeat of that call reads its result.
    await jobs.record(workspace, run_id, command.label)
    return await _wait_and_finish(
        sandbox,
        workspace,
        command,
        run,
        entries=ws_state.entries,
        bytes_read=ws_state.bytes_read,
        user_id=user_id,
        quota_user_id=effective_quota_user,
        db=db,
        sleep=sleep,
        budget=BACKGROUND_FIRST_LINES_SECONDS if background else None,
    )


async def _needs_install(sandbox: Any, install: ResolvedCommand, entries: dict[str, Any]) -> bool:
    """Whether *install* must run before a check of its directory: a Node directory with no installed dependencies.

    True when none of :data:`NODE_STATE_FILES` is there, or when they are but no install of the current
    lockfiles succeeded in this container (a lockfile changed since: its cache key has no record). False for a
    directory with no Node lockfile (no state to probe: the check runs as asked) and when the probe fails.
    """
    import dataclasses

    lock_dir = node_lock_dir(install, entries)
    if lock_dir is None:
        return False
    # A workspace member's install state is at the workspace root, where its lockfile is.
    root = install if lock_dir == install.cwd.strip("/") else dataclasses.replace(install, cwd=lock_dir)
    present = await _present(sandbox, root, NODE_STATE_FILES)
    if present is None:
        return False
    if not present:
        return True
    key = install_cache_key(install, entries)
    if key is None:
        return False
    return await _cache_hit(sandbox, key) is None


async def _install_then(command: ResolvedCommand, **call: Any) -> CommandOutcome:
    """Run *command*'s install check, then *command* itself, in one call; what stopped short is answered plainly."""
    import dataclasses

    install = command.install
    if install is None:
        return await run_command(command, **call)
    why = f"no installed dependencies in {display(command.cwd)} in this container"
    first = await run_command(install, **call)
    if first.running and first.run_id:
        # The install goes on past this call: the poll of its run_id runs the check once it ended with exit 0.
        await _attach_then(call["workspace"], install.image, first.run_id, dataclasses.replace(command, install=None))
        first.text += (
            f'\n{command.label} runs after it ({why}): sandbox_run_command(run_id="{first.run_id}") waits for '
            f"the install, then answers {command.label}."
        )
        return first
    if first.refused or first.busy or first.starting or first.lost or first.running:
        first.text += (
            f"\n{command.label} did not run yet: it needs {install.label} first ({why}). Once {install.label} ended, "
            f"call {command.label} again: the install is then skipped."
        )
        return first
    if first.exit_code != 0 or first.timed_out or first.stopped:
        first.text = (
            f"{command.label} did not run: its install check {install.label} ran first ({why}) and failed; "
            f"fix the install, then call {command.label} again.\n{first.text}"
        )
        first.command = install
        return first
    with contextlib.suppress(Exception):
        # The check's own outcome is audited by the tool; the install that ran first is audited here.
        await audit_run(
            user_id=call.get("user_id"), workspace_id=call["workspace"].workspace_id, command=install, outcome=first
        )
    # The install stays in the check's run record: a rerun after a container restart installs the new one first.
    rest = await run_command(dataclasses.replace(command, install=None), _rerun_install=install, **call)
    rest.text = (
        f"{install.label} ran first ({why}): exit 0 in {first.seconds:.1f}s (output in {display(first.run_dir)}). "
        f"The next {command.label} call skips it while the lockfiles are unchanged.\n{rest.text}"
    )
    return rest


async def poll_run(
    run_id: str,
    *,
    workspace: WorkspaceSession,
    user_id: uuid.UUID | None = None,
    quota_user_id: uuid.UUID | None = None,
    db: Any = None,
    sandbox_api: Any = None,
    sleep: Sleep = asyncio.sleep,
    stop: bool = False,
    _hops: int = 0,
) -> CommandOutcome:
    """Wait again for the detached run *run_id* (answered ``running`` before); the outcome, never an exception.

    With *stop* the run is asked to end first: the helper sees the ``stop``
    file in its run directory within a quarter second, ends the command's process
    group and writes the end marker with ``stopped``; the outcome carries the output
    so far and the workspace changes, like any finished run.
    """
    from datetime import timedelta

    # As stored now, not as this session first read it: a resumed turn on another worker wrote it since.
    state = dict(await workspace.latest_kernel_state() or {})
    found = None
    for image, held in (state.get("commands") or {}).items():
        run = (held or {}).get("run") or {}
        if run.get("run_id") == run_id:
            found = (image, held, run)
            break
    if found is None:
        rerun_as = str((state.get(RERUNS) or {}).get(run_id) or "")
        if rerun_as and rerun_as != run_id and _hops < _MAX_RERUN_HOPS and not await _saved_result(workspace, run_id):
            # Lost with its container and started again once (_rerun_lost): the run that replaced it answers.
            followed = await poll_run(
                rerun_as,
                workspace=workspace,
                user_id=user_id,
                quota_user_id=quota_user_id,
                db=db,
                sandbox_api=sandbox_api,
                sleep=sleep,
                stop=stop,
                _hops=_hops + 1,
            )
            followed.text = (
                f"(Run {run_id} was lost with its container and started again as run {rerun_as}; "
                f"run {rerun_as} answers.)\n{followed.text}"
            )
            return followed
        lost_run = (state.get(LOST_RUNS) or {}).get(run_id)
        if isinstance(lost_run, dict) and not stop and not await _saved_result(workspace, run_id):
            # Its container was replaced by a later call (a restart seen by another check): rerun once, as a
            # poll that finds the container gone does, so every check lost to a restart is answered alike.
            await _forget_lost(workspace, run_id)
            why = (
                f"its container was replaced by a new one {_since(lost_run):.0f} s after the command started "
                "(a container restart)"
            )
            return await _rerun_lost(
                _lost_command(lost_run),
                run_id,
                why,
                record=lost_run,
                workspace=workspace,
                user_id=user_id,
                quota_user_id=quota_user_id,
                db=db,
                sandbox_api=sandbox_api if sandbox_api is not None else _default_api(user_id),
                sleep=sleep,
            )
        return await _not_running(workspace, run_id)
    image, held, run = found
    if await _saved_result(workspace, run_id):
        # Its result was saved but the record still holds it (a clear lost with its worker): answered, then freed.
        await _remember_run(workspace, image, None)
        return await _not_running(workspace, run_id)
    command = _command_from_record(run["command"])
    api = sandbox_api if sandbox_api is not None else _default_api(user_id)
    started = time.monotonic()
    rerun = {
        "workspace": workspace,
        "user_id": user_id,
        "quota_user_id": quota_user_id,
        "db": db,
        "sandbox_api": api,
        "sleep": sleep,
    }
    try:
        sandbox = await api.connect(held["sandbox_id"])
        await sandbox.renew(timedelta(seconds=float(command.timeout) + HARD_TIMEOUT_MARGIN + 60))
    except Exception as exc:
        await _forget(workspace, image)
        why = (
            f"the container of run {run_id} is gone (stage poll, {time.monotonic() - started:.0f} s, "
            f"{_since(run):.0f} s after the command started: {format_exception_message(exc)})"
        )
        if not stop:
            return await _rerun_lost(_lost_command(run), run_id, why, record=run, **rerun)
        return CommandOutcome(
            text=(
                f"{command.label}: lost. The container of run {run_id} is gone (stage poll, "
                f"{time.monotonic() - started:.0f} s, {_since(run):.0f} s after the command started: "
                f"{format_exception_message(exc)}); no output of it is kept. Nothing holds the workspace any more: "
                "run the check again (the same call starts it in a new container)."
            ),
            lost=True,
            run_id=run_id,
            command=command,
        )
    try:
        baseline = json.loads(await sandbox.files.read_bytes(f"{remote_run_dir(run_id)}/baseline.json"))
    except Exception as exc:
        with contextlib.suppress(Exception):
            await sandbox.close()
        await _remember_run(workspace, image, None)
        if not stop:
            why = (
                f"run {run_id} left no run directory in its container (stage poll: {format_exception_message(exc)}): "
                "the container was restarted since the command started"
            )
            return await _rerun_lost(_lost_command(run), run_id, why, record=run, **rerun)
        return CommandOutcome(
            text=(
                f"{command.label}: lost. Run {run_id} left no run directory in its container (stage poll: "
                f"{format_exception_message(exc)}): the container was restarted since the command started, so no "
                "output of it is kept. Run the check again (the same call starts it anew)."
            ),
            lost=True,
            run_id=run_id,
            command=command,
        )
    from app.services.sandbox_workspace.manifest import ManifestEntry

    entries = {path: ManifestEntry(path=path, size=int(size), sha256=str(sha), mtime=float(mtime)) for path, (size, mtime, sha) in baseline.items()}
    effective_quota_user = quota_user_id if quota_user_id is not None else user_id
    if stop:
        try:
            await sandbox.files.write_file(f"{remote_run_dir(run_id)}/stop", b"stop\n")
        except Exception as exc:
            with contextlib.suppress(Exception):
                await sandbox.close()
            return CommandOutcome(
                text=f"Error: run {run_id} could not be asked to stop (stage stop): {format_exception_message(exc)}.",
                refused=True,
                command=command,
            )
    outcome = await _wait_and_finish(
        sandbox,
        workspace,
        command,
        run,
        entries=entries,
        bytes_read=0,
        user_id=user_id,
        quota_user_id=effective_quota_user,
        db=db,
        sleep=sleep,
        budget=max(wait_budget(), STOP_WAIT_SECONDS) if stop else None,
    )
    if stop:
        return outcome
    # A run answered running first (background=true, or longer than the wait budget) ends here: the install
    # fallback it declares applies as it does when run_with_fallback sees the end itself.
    outcome = await _follow_fallback(command, outcome, **rerun)
    then = run.get("then")
    if isinstance(then, dict) and not outcome.running and outcome.exit_code == 0 and not outcome.timed_out:
        # An install-first answered running (its check waits for it): the check runs now, in the same container.
        # The install that just ran stays in the check's run record: a rerun after a restart installs first.
        rest = await run_command(_command_from_record(then), _rerun_install=command, **rerun)
        rest.text = (
            f"{command.label} (run {run_id}) ended with exit 0 in {outcome.seconds:.1f}s (output in "
            f"{display(outcome.run_dir)}); {rest.command.label if rest.command else 'the check'} that waited for it "
            f"ran next.\n{rest.text}"
        )
        return rest
    return outcome


async def _rerun_lost(
    command: ResolvedCommand, run_id: str, why: str, *, record: dict[str, Any] | None = None, **call: Any
) -> CommandOutcome:
    """Run *command* once more after its run *run_id* went with a container restart (*why*), never ``lost``.

    The same argv, directory and image start in a new container (its install first when the stored command
    names one and the container has no dependencies), the answer opening with one line naming the lost run.
    Once only: the rerun's own outcome is answered as it is (``running`` with its new ``run_id``, or its end).

    A rerun that did not start (the new container still starting after the restart, the container busy with
    another check, a refused create) keeps the lost run's *record* in :data:`LOST_RUNS`, so the next poll of
    *run_id* tries it again (at most :data:`_MAX_RERUN_ATTEMPTS` times) instead of answering ``lost``: after a
    restart every concurrent check meets a cold create, not only the first one polled.
    """
    outcome = await run_command(command, **call)
    # A busy answer names the run holding the container, never this check's rerun: it is not linked.
    started = bool(outcome.run_id and outcome.run_id != run_id and not (outcome.busy or outcome.refused or outcome.starting))
    if started:
        # A later poll of the lost run_id (a resumed turn that knew only it) is answered by this run.
        await _remember_rerun(call["workspace"], run_id, str(outcome.run_id))
    elif isinstance(record, dict) and (outcome.busy or outcome.refused or outcome.starting):
        attempts = int(record.get("rerun_attempts") or 0) + 1
        if attempts < _MAX_RERUN_ATTEMPTS:
            await _remember_lost(call["workspace"], run_id, {**record, "rerun_attempts": attempts})
            outcome.text += (
                f'\nThe rerun of run {run_id} did not start yet (attempt {attempts} of {_MAX_RERUN_ATTEMPTS - 1}): '
                f'sandbox_run_command(run_id="{run_id}") tries it again, nothing else to do.'
            )
    outcome.text = (
        f"(Run {run_id} of {command.label} was lost: {why}. It was started again once in a new container"
        f"{f', as run {outcome.run_id}' if started else ''}.)\n{outcome.text}"
    )
    return outcome


#: Polls of a lost run_id that may try its rerun when the earlier ones could not start it.
_MAX_RERUN_ATTEMPTS = 6


async def _remember_lost(workspace: WorkspaceSession, run_id: str, record: dict[str, Any]) -> None:
    """Keep the lost run *run_id* (its *record*) in :data:`LOST_RUNS` so its next poll reruns it; best effort."""

    def change(state: dict[str, Any]) -> dict[str, Any]:
        return {**state, LOST_RUNS: _bounded_links(state.get(LOST_RUNS), run_id, record, _MAX_RERUNS)}

    with contextlib.suppress(Exception):
        await workspace.update_kernel_state(change)


#: ``kernel_state`` key of the lost run_ids and the run each was started again as (:func:`_rerun_lost`).
RERUNS = "reruns"
#: Lost-to-rerun links a workspace keeps (the oldest go first).
_MAX_RERUNS = 32
#: Links a poll follows at most (a rerun lost in turn was started again too).
_MAX_RERUN_HOPS = 3


#: ``kernel_state`` key of the runs whose container a later call replaced (:func:`_create_and_remember`), by run_id:
#: the run record, so a poll of that run_id reruns it once (:func:`poll_run`) instead of answering ``lost``.
LOST_RUNS = "lost_runs"


def _bounded_links(links: Any, key: str, value: Any, bound: int) -> dict[str, Any]:
    """*links* with *key* set to *value* last, the oldest dropped past *bound*."""
    kept = {k: v for k, v in (links or {}).items() if k != key} if isinstance(links, dict) else {}
    kept[key] = value
    while len(kept) > bound:
        kept.pop(next(iter(kept)))
    return kept


async def _remember_rerun(workspace: WorkspaceSession, lost: str, rerun: str) -> None:
    """Record that run *lost* went with its container and was started again as *rerun*; best effort."""

    def change(state: dict[str, Any]) -> dict[str, Any]:
        return {**state, RERUNS: _bounded_links(state.get(RERUNS), lost, rerun, _MAX_RERUNS)}

    with contextlib.suppress(Exception):
        await workspace.update_kernel_state(change)


def _lost_command(run: dict[str, Any]) -> ResolvedCommand:
    """What a lost *run* reruns: its check, or for an install-first the check that waited for it, with that install.

    A new container has no dependencies: the rerun probes the install first (:func:`run_command`), so the check
    never meets Yarn's missing node_modules state file.
    """
    import dataclasses

    command = _command_from_record(run["command"])
    then = run.get("then")
    if isinstance(then, dict):
        return dataclasses.replace(_command_from_record(then), install=command)
    return command


async def _attach_then(workspace: WorkspaceSession, image: str, run_id: str, then: ResolvedCommand) -> None:
    """Record on the held run *run_id* (an install-first) the check *then* that runs once it ended; best effort."""

    def change(state: dict[str, Any]) -> dict[str, Any] | None:
        commands = dict(state.get("commands") or {})
        held = dict(commands.get(image) or {})
        run = held.get("run")
        if not isinstance(run, dict) or run.get("run_id") != run_id:
            return None
        held["run"] = {**run, "then": _command_record(then)}
        commands[image] = held
        return {**state, "commands": commands}

    with contextlib.suppress(Exception):
        await workspace.update_kernel_state(change)


async def _forget_lost(workspace: WorkspaceSession, run_id: str) -> None:
    """Drop *run_id* from :data:`LOST_RUNS` (its one rerun is starting); best effort."""

    def change(state: dict[str, Any]) -> dict[str, Any] | None:
        lost = dict(state.get(LOST_RUNS) or {})
        if lost.pop(run_id, None) is None:
            return None
        return {**state, LOST_RUNS: lost}

    with contextlib.suppress(Exception):
        await workspace.update_kernel_state(change)


async def _not_running(workspace: WorkspaceSession, run_id: str) -> CommandOutcome:
    """The answer to a poll of a *run_id* the workspace no longer holds as running; never an error.

    A resumed turn polls the run_id its cut segment started: the run may have ended (its
    ``.xtm/runs/<id>/run.json`` was saved, so its result is answered from there) or its
    container may have been lost with the worker (nothing kept: ``lost``, the check runs again).
    """
    saved = await saved_outcome(workspace, run_id)
    if saved is not None:
        return saved
    return CommandOutcome(
        text=(
            f"Run {run_id}: lost. This workspace holds no running command and no saved result with that run_id "
            "(its container went with a restart before the result was saved, or it never started). "
            "Run the check again."
        ),
        lost=True,
        run_id=run_id,
    )


async def saved_outcome(workspace: WorkspaceSession, run_id: str) -> CommandOutcome | None:
    """The result of run *run_id* as saved under ``.xtm/runs/<id>/`` (exit code, last output), ``None`` when none is."""
    run_dir = f"{RUNS_DIR}/{run_id}"
    try:
        _entry, data = await workspace.get_bytes(f"{run_dir}/run.json")
        meta = json.loads(data)
    except Exception:
        meta = None
    if isinstance(meta, dict):
        label = meta.get("check") or " ".join(str(a) for a in (meta.get("argv") or ())[:3]) or "argv"
        tail = ""
        with contextlib.suppress(Exception):
            _entry, out = await workspace.get_bytes(f"{run_dir}/stdout.txt")
            tail = out.decode("utf-8", "replace")[-_SAVED_TAIL:]
        state = "stopped" if meta.get("stopped") else ("timed out" if meta.get("timed_out") else f"exit code {meta.get('exit_code')}")
        text = (
            f"{label}: ended ({state}, {meta.get('seconds')} s, finished {meta.get('finished_at')}); run {run_id} "
            f"is no longer running and its result was saved in {run_dir}/ (stdout.txt, stderr.txt, run.json)."
        )
        if tail.strip():
            text += f"\nLast output:\n{tail}"
        exit_code = meta.get("exit_code")
        return CommandOutcome(text=text, exit_code=exit_code if isinstance(exit_code, int) else None, run_dir=run_dir, run_id=run_id)
    return None


async def _saved_result(workspace: WorkspaceSession, run_id: str) -> bool:
    """Whether ``.xtm/runs/<run_id>/run.json`` is in the workspace (the run's end was collected)."""
    with contextlib.suppress(Exception):
        return f"{RUNS_DIR}/{run_id}/run.json" in await workspace.entries(refresh=True)
    return False


#: Characters of a saved run's stdout a poll of an ended run quotes.
_SAVED_TAIL = 2000


def _since(run: dict[str, Any]) -> float:
    return max(0.0, time.time() - float(run.get("started_at") or time.time()))


def _same_command(run: dict[str, Any], command: ResolvedCommand) -> bool:
    """Whether the detached *run* is *command*: the same check, argv, directory and image."""
    held = run.get("command") or {}
    return (
        held.get("check") == command.check
        and list(held.get("argv") or ()) == list(command.argv)
        and (held.get("cwd") or "") == command.cwd
        and held.get("image") == command.image
    )


async def _edited_since_start(workspace: WorkspaceSession, run: dict[str, Any], cwd: str) -> bool:
    """Whether a file of the checkout *cwd* runs from changed after the held *run* was staged.

    Its result would answer for a tree that is gone. The run's own outputs are written back at its collection,
    after this read, and ``.xtm/`` holds run records only. ``False`` when the start time or the manifest cannot
    be read (the run is reattached, as before).
    """
    try:
        started = float(run.get("started_at") or 0.0)
        entries = await workspace.entries(refresh=True)
    except Exception:
        return False
    if not started:
        return False
    staged, root = staged_entries(entries, cwd)
    for path, entry in staged.items():
        if path.startswith(".xtm/") or (root and not is_under(path, root)):
            continue
        if float(getattr(entry, "mtime", 0.0) or 0.0) > started + jobs.CLOCK_SLACK:
            return True
    return False


#: Commands a resume note names at most.
_IN_FLIGHT_MAX = 5


def in_flight_lines(kernel_state: dict[str, Any] | None) -> list[str]:
    """One line per detached command a workspace's ``kernel_state`` holds as still running (bounded).

    What a resumed turn reads so it waits on the command its cut segment
    started instead of starting it again. A run past its timeout and margin is
    left out: its runner ended it.
    """
    lines: list[str] = []
    for image, held in sorted(((kernel_state or {}).get("commands") or {}).items()):
        run = (held or {}).get("run") if isinstance(held, dict) else None
        if not isinstance(run, dict) or not run.get("run_id"):
            continue
        record = run.get("command") or {}
        if _since(run) > float(record.get("timeout") or DEFAULT_TIMEOUT) + HARD_TIMEOUT_MARGIN:
            continue
        what = record.get("check") or " ".join(str(a) for a in (record.get("argv") or ())[:3]) or "argv"
        lines.append(f"{what} on image {image!r}, run_id {run['run_id']}, running for {_since(run):.0f} s")
    return lines[:_IN_FLIGHT_MAX]


def _command_record(command: ResolvedCommand, *, rerun_install: ResolvedCommand | None = None) -> dict[str, Any]:
    """The stored form of *command*; *rerun_install* is recorded as its install when it carries none."""
    record: dict[str, Any] = {
        "check": command.check,
        "argv": list(command.argv),
        "cwd": command.cwd,
        "timeout": command.timeout,
        "image": command.image,
        "report": command.report,
        "report_path": command.report_path,
        "env": [list(pair) for pair in command.env],
    }
    install = command.install if command.install is not None else rerun_install
    if install is not None:
        # Kept so a run lost with its container reruns with its install probe (a new container has no dependencies),
        # also when the install ran first in the old container and the check itself ran without one.
        record["install"] = _command_record(install)
    if command.fallback_image:
        # Kept so poll_run falls back like run_with_fallback when the run was answered running first (background).
        record["fallback_image"] = command.fallback_image
        record["fallback_configured"] = command.fallback_configured
        record["fallback_when"] = command.fallback_when
        record["fallback_reason"] = command.fallback_reason
    return record


def _command_from_record(record: dict[str, Any]) -> ResolvedCommand:
    """The :class:`ResolvedCommand` a :func:`_command_record` stored (a record without ``install`` has none)."""
    fields = {key: value for key, value in record.items() if key != "install"}
    install = record.get("install")
    return ResolvedCommand(
        **{
            **fields,
            "argv": tuple(fields["argv"]),
            "env": tuple((str(k), str(v)) for k, v in fields.get("env") or ()),
            "install": _command_from_record(install) if isinstance(install, dict) else None,
        }
    )


async def _held_run(workspace: WorkspaceSession, image: str) -> dict[str, Any] | None:
    # As stored now: a poll on another worker may have cleared the run since this session first read the record.
    state = await workspace.latest_kernel_state()
    if not state:
        return None
    run = ((state.get("commands") or {}).get(image) or {}).get("run")
    if not run:
        return None
    if _since(run) > float((run.get("command") or {}).get("timeout") or DEFAULT_TIMEOUT) + HARD_TIMEOUT_MARGIN:
        # Past its timeout and never answered: its runner ended it; nothing holds the container any more.
        await _remember_run(workspace, image, None)
        return None
    return dict(run)


async def _remember_run(workspace: WorkspaceSession, image: str, run: dict[str, Any] | None) -> None:
    """Record (or with ``None`` clear) the detached run of the *image* container in ``kernel_state``."""
    def change(state: dict) -> dict | None:
        commands = dict(state.get("commands") or {})
        held = dict(commands.get(image) or {})
        if run is None:
            if "run" not in held:
                return None
            held.pop("run", None)
        else:
            held["run"] = run
        commands[image] = held
        return {**state, "commands": commands}

    await workspace.update_kernel_state(change)


async def _start(sandbox: Any, command: ResolvedCommand, run_id: str, entries: dict[str, Any]) -> None:
    """Start *command* detached through the workspace helper's ``exec`` action; returns once it is launched."""
    from opensandbox.models.execd import RunCommandOpts
    from opensandbox.models.filesystem import WriteEntry

    from app.services.sandbox_workspace.registry_mirror import command_env, write_maven_settings
    from app.services.sandbox_workspace.remote import REMOTE_WORKSPACE, TOOL_PATH, snapshot_json

    rdir = remote_run_dir(run_id)
    await sandbox.files.create_directories([WriteEntry(path=rdir, mode=700)])
    # The Maven settings MAVEN_ARGS names, written beside each command (a pooled container may be fresh).
    await write_maven_settings(sandbox)
    if command.report_path and "/" in command.report_path:
        # The report's directory, so a check that does not create it still writes the file.
        parent = command.report_path.rsplit("/", 1)[0]
        with contextlib.suppress(Exception):
            await sandbox.files.create_directories([WriteEntry(path=f"{REMOTE_WORKSPACE}/{parent}", mode=755)])
    await sandbox.files.write_file(f"{rdir}/argv.json", json.dumps(list(command.argv)).encode("utf-8"))
    if command.env:
        # The check's declared variables, over the container's own (the helper reads env.json when present).
        await sandbox.files.write_file(f"{rdir}/env.json", json.dumps(dict(command.env)).encode("utf-8"))
    # What the workspace held when the command started: what a later call compares the container to.
    await sandbox.files.write_file(f"{rdir}/baseline.json", snapshot_json(entries))
    where = f"{REMOTE_WORKSPACE}/{command.cwd}" if command.cwd else REMOTE_WORKSPACE
    await sandbox.commands.run(
        f"python {TOOL_PATH} exec {shlex.quote(rdir)} {shlex.quote(where)} {int(command.timeout)}",
        # The registry mirror's variables travel with the command too: a pooled container (poolRef) does not
        # carry the create's env into the process execd starts, so corepack and yarn fell back to the public
        # registries the egress policy denies.
        opts=RunCommandOpts(background=True, working_directory="/tmp", envs={**command_env(), **SAFE_ENV}),  # noqa: S108
    )


async def _cache_hit(sandbox: Any, key: str) -> dict[str, Any] | None:
    try:
        hit: dict[str, Any] = json.loads(await sandbox.files.read_bytes(f"{INSTALL_CACHE_DIR}/{key}.json"))
        return hit
    except Exception:
        return None


async def _missing_state(sandbox: Any, command: ResolvedCommand, hit: dict[str, Any]) -> list[str]:
    """The state files the cached install left that are gone from the container (none recorded: nothing to check).

    A probe that fails counts every recorded file as missing: the install runs again rather than answer a
    cache hit nobody could check.
    """
    recorded = [path for path in hit.get("state") or [] if isinstance(path, str) and path in INSTALL_STATE_FILES]
    if not recorded:
        return []
    present = await _present(sandbox, command, recorded)
    if present is None:
        return recorded
    return [path for path in recorded if path not in present]


def display_cwd(command: ResolvedCommand, path: str) -> str:
    """*path* (relative to the check's cwd) as the workspace path an agent reads."""
    return display(f"{command.cwd}/{path}" if command.cwd else path)


async def _cache_store(sandbox: Any, command: ResolvedCommand, key: str, run_id: str, seconds: float) -> None:
    from opensandbox.models.filesystem import WriteEntry

    present = await _present(sandbox, command, INSTALL_STATE_FILES)
    record = {
        "run_id": run_id,
        "seconds": round(seconds, 1),
        "at": datetime.now(UTC).isoformat(),
        # What the install left in its directory: a later hit is answered only while these are all there.
        "state": sorted(present or ()),
    }
    with contextlib.suppress(Exception):
        await sandbox.files.create_directories([WriteEntry(path=INSTALL_CACHE_DIR, mode=700)])
        await sandbox.files.write_file(f"{INSTALL_CACHE_DIR}/{key}.json", json.dumps(record).encode("utf-8"))


def _cached(command: ResolvedCommand, hit: dict[str, Any], status: str) -> CommandOutcome:
    """The answer of an install whose lockfiles did not change since it succeeded in this container."""
    text = (
        f"{command.label}: exit 0 in 0.0s (cached: the same lockfiles were installed by run {hit.get('run_id')} at "
        f"{hit.get('at')} in {hit.get('seconds')} s; the dependencies are still in this container, nothing was run). "
        "A change to a lockfile or to the check's argv runs the install again."
    )
    return CommandOutcome(text=text, exit_code=0, container=status, command=command)


async def _ended(sandbox: Any, rdir: str) -> dict[str, Any] | None:
    try:
        ended: dict[str, Any] = json.loads(await sandbox.files.read_bytes(f"{rdir}/exit.json"))
        return ended
    except Exception:
        return None


async def _read_text(sandbox: Any, path: str) -> str:
    try:
        raw = await sandbox.files.read_bytes(path)
    except Exception:
        return ""
    return bytes(raw[-MAX_SAVED_OUTPUT:]).decode("utf-8", errors="replace")


async def _wait_and_finish(
    sandbox: Any,
    workspace: WorkspaceSession,
    command: ResolvedCommand,
    run: dict[str, Any],
    *,
    entries: dict[str, Any],
    bytes_read: int,
    user_id: uuid.UUID | None,
    quota_user_id: uuid.UUID | None,
    db: Any,
    sleep: Sleep,
    budget: float | None = None,
) -> CommandOutcome:
    """Wait for the detached *run* within *budget* (default :func:`wait_budget`); ``running`` when it has not ended, else its result."""
    from app.core.config import settings
    from app.services.quota import increment_sandbox_usage
    from app.services.sandbox_call_logging import log_sandbox_call
    from app.services.sandbox_workspace.remote import REMOTE_WORKSPACE, RemoteState, collect

    run_id = run["run_id"]
    rdir = remote_run_dir(run_id)
    status = run.get("container", "reused")
    budget = wait_budget() if budget is None else budget
    waited = time.monotonic()
    delay = POLL_START
    lost = False
    no_install = False
    survivors: list[int] = []
    ended: dict[str, Any] | None = None
    try:
        while True:
            ended = await _ended(sandbox, rdir)
            if ended is not None:
                break
            if _since(run) > command.timeout + HARD_TIMEOUT_MARGIN:
                lost = True
                break
            if time.monotonic() - waited + delay > budget:
                stdout = await _read_text(sandbox, f"{rdir}/stdout.txt")
                stderr = await _read_text(sandbox, f"{rdir}/stderr.txt")
                tail = "\n".join(
                    f"{name} so far (tail):\n{text[-RUNNING_TAIL:]}" for name, text in (("stdout", stdout), ("stderr", stderr)) if text.strip()
                )
                return CommandOutcome(
                    text=(
                        f"{command.label}: running for {_since(run):.0f} s of its {command.timeout} s timeout "
                        f"(run_id {run_id}, container {status}); this call waited {time.monotonic() - waited:.0f} s. "
                        "It keeps running in its container."
                        + (f"\n{tail}" if tail else "")
                    ),
                    running=True,
                    run_id=run_id,
                    container=status,
                    command=command,
                )
            await sleep(delay)
            delay = min(delay * 2, POLL_MAX)

        outcome = CommandOutcome(text="", run_dir=f"{RUNS_DIR}/{run_id}", container=status, run_id=run_id, command=command)
        call = WorkspaceCall(mode=SESSION, kernel=status)
        call.mounted = True
        stdout = await _read_text(sandbox, f"{rdir}/stdout.txt")
        stderr = await _read_text(sandbox, f"{rdir}/stderr.txt")
        if lost or ended is None:
            outcome.timed_out = True
            outcome.seconds = _since(run)
        else:
            outcome.exit_code = ended.get("exit_code")
            outcome.timed_out = bool(ended.get("timed_out"))
            outcome.stopped = bool(ended.get("stopped"))
            outcome.seconds = float(ended.get("seconds") or 0.0)
            # Processes the helper's stop or timeout kill left alive: the container goes with them (below).
            survivors = [int(pid) for pid in ended.get("survivors") or () if str(pid).isdigit()]
        metrics.observe_command_stage("command", _command_outcome(outcome, lost=lost), outcome.seconds or 0.0)
        _annotate_tool_span(
            {
                "sandbox.command_ms": f"{(outcome.seconds or 0.0) * 1000.0:.0f}",
                "sandbox.command_outcome": _command_outcome(outcome, lost=lost),
            }
        )
        report_missing = ""
        if not lost and command.report != "lines" and command.report_path:
            try:
                raw = await sandbox.files.read_bytes(f"{REMOTE_WORKSPACE}/{command.report_path}")
            except Exception:
                report_missing = f"No {command.report} report at {display(command.report_path)}: the check wrote none (see its output)."
            else:
                if len(raw) > MAX_REPORT_BYTES:
                    report_missing = f"The {command.report} report at {display(command.report_path)} is larger than {MAX_REPORT_BYTES // (1024 * 1024)} MB: not parsed."
                else:
                    outcome.report = parse_report(command.report, raw.decode("utf-8", errors="replace"))
        readback = ""
        vanished: list[str] = []
        if not lost:
            readback_started = time.monotonic()
            try:
                ignore = [*IGNORED_PATTERNS, *(str(p) for p in run.get("ignore") or [])]
                scanned = await collect(
                    sandbox, workspace, RemoteState(entries=dict(entries), bytes_read=bytes_read), ignore=ignore
                )
            except WorkspaceQuotaError as exc:
                metrics.quota_refused("sandbox_run_command")
                call.refused = str(exc)
            except Exception as exc:
                readback = f"stage read-back, {time.monotonic() - readback_started:.0f} s: {format_exception_message(exc)}"
            else:
                scanned.removals, vanished = keep_vanished_checkouts(scanned.removals)
                await commit_scan(workspace, scanned, call, bytes_read=bytes_read)
        if run.get("cache_key") and outcome.exit_code == 0 and not outcome.timed_out:
            await _cache_store(sandbox, command, run["cache_key"], run_id, outcome.seconds)
        if not lost and outcome.exit_code not in (0, None) and not is_install(command) and is_node_dir(command, entries):
            if YARN_NO_INSTALL_MESSAGE in stdout or YARN_NO_INSTALL_MESSAGE in stderr:
                no_install = True
            else:
                # A workspace member's install state is at the workspace root, where its lockfile is (:func:`_needs_install`).
                lock_dir = node_lock_dir(command, entries)
                probed = command if lock_dir is None or lock_dir == command.cwd.strip("/") else _with_cwd(command, lock_dir)
                present = await _present(sandbox, probed, NODE_STATE_FILES)
                no_install = present is not None and not present
        if not lost and not survivors:
            await _release_to_idle(sandbox, command)
        await increment_sandbox_usage(quota_user_id, db=db)
        await log_sandbox_call(
            usage_type="sandbox_command",
            outcome="failure" if lost else "success",
            user_id=user_id,
            failure_reason="execution_error" if lost else None,
            egress_allow_list=settings.opensandbox_network_allow_list,
        )
    finally:
        teardown_started = time.monotonic()
        if lost or survivors:
            # A process that outlived the stop is ended with its container: never a stop answered over a live one.
            with contextlib.suppress(Exception):
                await sandbox.kill()
            await _forget(workspace, command.image)
        with contextlib.suppress(Exception):
            await sandbox.close()
        metrics.observe_command_stage("teardown", "lost" if lost else "ok", time.monotonic() - teardown_started)

    saved = await _save_run(
        workspace,
        outcome.run_dir,
        command=command,
        stdout=stdout,
        stderr=stderr,
        exit_code=outcome.exit_code,
        timed_out=outcome.timed_out,
        stopped=outcome.stopped,
        seconds=outcome.seconds,
        failure=readback,
        container=status,
        failures=outcome.report.failures_text() if outcome.report is not None else "",
    )
    # Cleared only once the result is saved: a worker lost in between leaves the run held (the next call
    # collects it), never a run with no record and no result that a resumed turn must start again.
    if not lost:
        await _remember_run(workspace, command.image, None)
    body = format_result(
        check=command.label,
        argv=list(command.argv),
        cwd=command.cwd or ".",
        exit_code=outcome.exit_code,
        seconds=outcome.seconds,
        timed_out=outcome.timed_out,
        stdout=stdout,
        stderr=stderr,
        run_dir=display(outcome.run_dir),
        report=outcome.report,
        report_missing=report_missing,
        failures_saved=bool(saved),
        # A check that passed answers with its totals (a script's or an argv's output is its answer, kept).
        green_summary=bool(command.check) and command.check != SCRIPT_CHECK,
    )
    notes = [run["cache_note"]] if run.get("cache_note") else []
    if lost:
        notes.append(
            f"The command gave no end past its timeout ({command.timeout}s + {HARD_TIMEOUT_MARGIN}s, the runner is gone); "
            "the container was discarded, the next command starts a new one."
        )
    elif outcome.timed_out:
        notes.append(f"The command exceeded its timeout ({command.timeout}s) and was stopped with its process group.")
    elif outcome.stopped:
        notes.append(
            f"Stopped on request after {outcome.seconds:.1f} s, with every process it started; the output above is "
            "what it printed until then."
        )
    if survivors:
        notes.append(
            f"{len(survivors)} process(es) it started outlived the kill of its process tree (pids "
            f"{', '.join(str(pid) for pid in survivors[:5])}): the container was discarded with them, the next "
            "command starts a new one."
        )
    if vanished:
        notes.append(
            f"Kept in the workspace: {', '.join(display(root) for root in vanished[:5])}/ "
            "(its checkout record was gone from the container when the command ended, so the whole checkout was "
            "missing there, not removed by the command; nothing of it was deleted)."
        )
    if readback:
        notes.append(f"What the command changed under the workspace could not be read back ({readback}).")
    elif call.refused:
        notes.append(f"What the command changed under the workspace was not saved: {call.refused}")
    elif not call.diff.empty:
        from app.services.sandbox_workspace.manifest import format_diff

        notes.append("Workspace: " + format_diff(call.diff))
        outcome.written = [*call.diff.added, *call.diff.changed]
        outcome.rewritten = list(call.diff.changed)
    outcome.missing = missing_paths(f"{stdout}\n{stderr}", REMOTE_WORKSPACE, command.cwd)
    outcome.uncollected = uncollected_files(f"{stdout}\n{stderr}")
    outcome.interrupted = bool(_INTERRUPTED.search(f"{stdout}\n{stderr}"))
    if no_install:
        notes.append(
            f"No installed dependencies in {display(command.cwd)} in this container (no node_modules or Yarn "
            "PnP state): run the checkout's install check first, then this check again."
        )
    if not saved:
        notes.append("The run directory could not be written (quota or storage); the output above is all there is.")
    outcome.text = body + ("\n" + "\n".join(notes) if notes else "")
    return outcome


def _annotate_tool_span(fields: dict[str, str]) -> None:
    """Put a run's stage figures on the ``sandbox_run_command`` tool span; never raises."""
    with contextlib.suppress(Exception):
        from app.services.langfuse_tracing import annotate_current_tool_span

        annotate_current_tool_span(fields)


def _command_outcome(outcome: CommandOutcome, *, lost: bool) -> str:
    """The ``outcome`` label of a finished command's stage timing."""
    if lost:
        return "lost"
    if outcome.timed_out:
        return "timeout"
    if outcome.stopped:
        return "stopped"
    return "ok" if outcome.exit_code == 0 else "failed"


async def _save_run(
    workspace: WorkspaceSession,
    run_dir: str,
    *,
    command: ResolvedCommand,
    stdout: str,
    stderr: str,
    exit_code: int | None,
    timed_out: bool,
    seconds: float,
    failure: str,
    container: str,
    stopped: bool = False,
    failures: str = "",
) -> str:
    """Write the run's output, metadata and failing tests' whole messages under *run_dir*; the directory, or ``""`` when it could not be."""
    meta = {
        "check": command.check,
        "argv": list(command.argv),
        "cwd": command.cwd,
        "image": command.image,
        "timeout": command.timeout,
        "exit_code": exit_code,
        "timed_out": timed_out,
        # Ended on request (``stop=true``): a reader of the workspace tells a stop from a SIGKILL crash.
        "stopped": stopped,
        "seconds": round(seconds, 3),
        "container": container,
        "failure": failure,
        "report": command.report,
        "report_path": command.report_path,
        "finished_at": datetime.now(UTC).isoformat(),
    }
    files = [
        (f"{run_dir}/stdout.txt", stdout[-MAX_SAVED_OUTPUT:].encode("utf-8")),
        (f"{run_dir}/stderr.txt", stderr[-MAX_SAVED_OUTPUT:].encode("utf-8")),
        (f"{run_dir}/run.json", json.dumps(meta, indent=2).encode("utf-8")),
    ]
    if failures:
        files.append((f"{run_dir}/{FAILURES_FILE}", failures.encode("utf-8")))
    try:
        stale = _stale_run_files(await workspace.entries(refresh=True), run_dir)
    except Exception:
        stale = []
    try:
        await workspace.put_tree(files, remove=stale, operation="run_command")
    except (WorkspaceQuotaError, WorkspaceUnavailableError) as exc:
        logger.info("Run directory %s not written: %s", run_dir, exc)
        return ""
    except Exception as exc:
        logger.warning("Run directory %s not written: %s", run_dir, format_exception_message(exc))
        return ""
    return display(run_dir)


def _stale_run_files(entries: Mapping[str, Any], run_dir: str, keep: int | None = None) -> list[str]:
    """The files of the run directories older than the newest *keep* - 1 (the one being saved, *run_dir*, counts).

    A run directory's age is its newest file's mtime; ``.xtm/runs/prepush/`` (the pre-push guard's records) is
    never a run directory and is kept.
    """
    prefix = f"{RUNS_DIR}/"
    newest: dict[str, float] = {}
    files: dict[str, list[str]] = {}
    for path, entry in entries.items():
        if not path.startswith(prefix):
            continue
        run_id, sep, _rest = path[len(prefix) :].partition("/")
        if not sep or run_id == "prepush" or f"{prefix}{run_id}" == run_dir:
            continue
        newest[run_id] = max(newest.get(run_id, 0.0), float(getattr(entry, "mtime", 0.0) or 0.0))
        files.setdefault(run_id, []).append(path)
    kept = max(0, (MAX_KEPT_RUNS if keep is None else keep) - 1)
    old = sorted(newest, key=lambda run_id: newest[run_id], reverse=True)[kept:]
    return [path for run_id in old for path in files[run_id]]


async def audit_run(
    *,
    user_id: uuid.UUID | None,
    workspace_id: uuid.UUID | None,
    command: ResolvedCommand,
    outcome: CommandOutcome,
) -> None:
    await audit.record(
        "sandbox_run_command",
        user_id=user_id,
        workspace_id=workspace_id,
        summary=(
            f"Ran {command.label} ({shown_argv(command.argv)[:200]}) in the sandbox workspace: "
            + ("timed out" if outcome.timed_out else "stopped on request" if outcome.stopped else f"exit {outcome.exit_code}")
        ),
        details={
            "check": command.check,
            "argv": list(command.argv),
            "cwd": command.cwd,
            "image": command.image,
            "exit_code": outcome.exit_code,
            "timed_out": outcome.timed_out,
            "stopped": outcome.stopped,
            "seconds": round(outcome.seconds, 3),
            "run_dir": outcome.run_dir,
            "findings": len(outcome.report.findings) + outcome.report.more if outcome.report else None,
        },
    )
