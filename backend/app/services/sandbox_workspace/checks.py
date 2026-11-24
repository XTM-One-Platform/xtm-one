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

"""The repository's own checks and services, declared in ``.xtm-one/checks.toml``.

A checkout may declare what can be run on it (``[check.<name>]``: ``argv``,
``cwd``, ``timeout``, ``image``, ``report``, ``report_path``, ``env``,
``affected``, ``affected_globs``, ``affected_always``, ``affected_scan_patterns``, ``install_first``, ``fallback``, ``git_repo``) and how it is
started as a running service (``[service.<name>]``: ``argv``, ``cwd``,
``image``, ``port``, ``ready_path``, ``ready_log``, ``session_log``, ``timeout``,
``restart_target``, ``lifetime``, ``env``, ``depends_on``, ``needs``); :func:`load_checks` parses the checks
into :class:`CheckSpec` rows, :func:`load_services` the services into :class:`ServiceSpec` rows,
:func:`parse_junit` and :func:`parse_sarif` turn the report file a check
produced into bounded :class:`Finding` rows, :func:`format_result` renders
what ``sandbox_run_command`` answers. Pure functions: no sandbox, no store,
no knowledge of which tool produced the report.
"""

from __future__ import annotations

import contextlib
import json
import posixpath
import re
import tomllib
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from defusedxml.ElementTree import ParseError, fromstring

CHECKS_FILE = ".xtm-one/checks.toml"
RUNS_DIR = ".xtm/runs"
REPORT_KINDS = ("junit", "sarif", "lines")
#: What ``sandbox_run_command(affected=true)`` hands a check: the test files the changes affect (``tests``),
#: the changed files themselves (``files``), or the changed files with their changed line ranges (``lines``).
AFFECTED_MODES = ("tests", "files", "lines")
#: In the args of an ``affected_narrows_args`` check: run on the paths as given (a whole file), not narrowed.
WHOLE_ARG = "--whole"
#: Globs an ``affected_globs`` declares, at most.
MAX_AFFECTED_GLOBS = 16
#: The kinds of change a pre-push guard can be limited to (``pre_push_on``).
PRE_PUSH_KINDS: tuple[str, ...] = ("added", "modified", "removed")
#: Characters of an install check's ``fallback.when`` and ``fallback.reason``.
MAX_FALLBACK_CHARS = 256
DEFAULT_TIMEOUT = 600
MAX_TIMEOUT = 3600
MAX_ARGS = 64
MAX_ARG_CHARS = 512
#: Characters of one argv item of a ``[service.NAME]``: a lab's ``sh -c`` line outgrows the check bound.
SERVICE_MAX_ARG_CHARS = 8192
#: Characters of one argument a call passes (``args``, a command of its own): an inline SQL or JSON outgrows the
#: bound of a declared argv, which stays :data:`MAX_ARG_CHARS` (#788).
CALL_MAX_ARG_CHARS = 8192
MAX_FINDINGS = 50
#: Characters of stdout and of stderr kept in the result (head and tail each).
OUTPUT_HEAD = 2_000
OUTPUT_TAIL = 6_000
#: Characters of each output stream a check that exited 0 keeps: its last lines, where a runner prints its
#: totals (``Found 84 warnings and 0 errors``, ``5384 passed``); the rest stays in the run directory.
GREEN_OUTPUT_CHARS = 1_500
#: Longest block of lines whose back-to-back repeats are collapsed into one copy.
REPEAT_BLOCK_MAX = 8
#: A line seen this many times in one output is kept FREQUENT_KEEP times, the rest counted.
FREQUENT_LINE = 20
FREQUENT_KEEP = 3
#: Characters of the failures file (whole message and body of each failing test).
MAX_FAILURES_CHARS = 512 * 1024
FAILURES_FILE = "failures.txt"
#: Bytes of a report file the parsers accept.
MAX_REPORT_BYTES = 8 * 1024 * 1024
#: Severity names, lowest first, as the findings are sorted.
SEVERITIES = ("note", "warning", "error")
#: Seconds a service may take to answer on its port, by default and at most.
SERVICE_DEFAULT_TIMEOUT = 180
SERVICE_MAX_TIMEOUT = 900
#: Seconds a service's container lives without a status or logs call, by default and at most.
SERVICE_DEFAULT_LIFETIME = 3600
SERVICE_MAX_LIFETIME = 4 * 3600
#: Bounds on a service's environment.
SERVICE_MAX_ENV = 64
SERVICE_MAX_ENV_CHARS = 2048
#: Paths a service may declare it needs in the checkout.
SERVICE_MAX_NEEDS = 32
#: Characters of a service's ``ready_log`` text.
SERVICE_MAX_READY_LOG = 256
_ENV_NAME_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


class ChecksError(ValueError):
    """The checks file or the call cannot be used; the message says why."""


@dataclass(frozen=True)
class CheckSpec:
    name: str
    argv: tuple[str, ...]
    cwd: str = ""
    timeout: int = DEFAULT_TIMEOUT
    image: str = ""
    report: str = "lines"
    report_path: str = ""
    description: str = ""
    #: The arguments that follow ``argv`` when a call gives none (the target a
    #: linter reads, ``src/``); a call's ``args`` replace them, never follow them.
    default_args: tuple[str, ...] = ()
    #: Variables set for the command over the container's own (the test ``SECRET_KEY`` CI exports).
    env: tuple[tuple[str, str], ...] = ()
    #: Detected checks: the variables CI sets from a secret or an expression, never copied (the run names them).
    env_unset: tuple[str, ...] = ()
    #: What ``affected=true`` appends to argv: one of :data:`AFFECTED_MODES` (a formatter takes ``lines``).
    affected: str = "tests"
    #: With ``files`` or ``lines``: the changed files kept, by glob on their path under the check's directory.
    affected_globs: tuple[str, ...] = ()
    #: With ``tests``: the test files (globs on their path under the check's directory) selected on every source
    #: change, ahead of the named hits, so the argv bound never cuts them (a repository's ratchet gates).
    affected_always: tuple[str, ...] = ()
    #: With ``tests``: regular expressions a test file's text matches to be a source-scan gate of this check (a
    #: Python tripwire reading every module through a shared helper names no changed identifier).
    affected_scan_patterns: tuple[str, ...] = ()
    #: A call without args runs as ``affected=true``, never the whole tree: with ``files`` or ``lines`` the changed
    #: files (a formatter on a repository whose main is not format-clean answers on the change alone), with
    #: ``tests`` the affected test files (a suite whose whole run needs a server never starts by default).
    #: :data:`WHOLE_ARG` alone in args runs it whole, bounded by :attr:`whole_timeout`.
    affected_by_default: bool = False
    #: With ``affected_by_default``: the seconds a whole run (:data:`WHOLE_ARG` alone in args) gets at most
    #: (``0``: the check's ``timeout``). Declared on a ``tests`` check, it turns ``affected_by_default`` on.
    whole_timeout: int = 0
    #: With ``files`` or ``lines``: bare paths given as args are narrowed to the change (a modified file to
    #: ``PATH:A-B,C-D``), so a fixer named a file main never formatted rewrites the change's lines only;
    #: :data:`WHOLE_ARG` in args turns it off for that call.
    affected_narrows_args: bool = False
    #: With ``files``: the changed files outside ``cwd`` are passed too, relative to it (``../x.yml``), for a
    #: script that maps the whole change to a repository-wide selection (the merge queue's test selection).
    affected_whole_repository: bool = False
    #: Run the install check of the same directory and image first when its dependencies are not installed in
    #: the container (``false`` for a check that needs none, such as a linter of plain files).
    install_first: bool = True
    #: An install check only: the image the install (and the check that ran it first) runs on again when it
    #: fails on its own image with ``fallback_when`` in its output, e.g. an offline image whose baked package
    #: cache is older than the checkout's lockfile; ``fallback_reason`` is the one line that names the cause.
    fallback_image: str = ""
    fallback_when: str = ""
    fallback_reason: str = ""
    #: Repository paths outside ``cwd`` the check reads (a test importing ``../../apps/x/src``): a narrow
    #: checkout that holds the check's directory takes them too (``detect.check_needs``).
    needs: tuple[str, ...] = ()
    #: A pre-push guard (``prepush.py``): globs of the files under ``cwd`` and ``needs`` whose change CI checks
    #: with this check; ``create_github_commit(from_checkout=...)`` names it when it did not pass on the tree.
    pre_push: tuple[str, ...] = ()
    #: The kinds of change (``added``, ``modified``, ``removed``) that make a guard's globs count: the Editions
    #: gate judges a file added or removed, not every edit of a classified one. All three by default.
    pre_push_on: tuple[str, ...] = PRE_PUSH_KINDS
    #: Pre-push guards this check stands in for (``frontend-bump-knip`` for ``frontend-knip``): the same gate run
    #: on another image, where the guard's own image cannot install the tree (a dependency bump). Its whole run
    #: is kept as each named guard's run, so ``create_github_commit`` does not name the guard as never run.
    stands_for: tuple[str, ...] = ()
    #: The command reads git (``git ls-files``, ``git rev-parse --show-toplevel``): it runs inside a throwaway
    #: repository of the checkout (:data:`GIT_REPO_SHELL`), as CI runs it in a clone.
    git_repo: bool = False
    #: The command needs PostgreSQL (its CI job runs a postgres service): it runs inside :data:`STORES_RUNNER`,
    #: which starts the image's offline postgres on 127.0.0.1:5432, exports ``PGHOST`` / ``PGPORT`` / ``PGUSER``
    #: / ``PGPASSWORD`` / ``PGDATABASE`` and ``DATABASE_URL``, runs the command and stops it (#1858).
    stores: bool = False


#: The stores runner every sandbox image with ``/opt/stores`` carries (``.xtm-one/stores.py`` copied there
#: at install): a ``stores = true`` check of any repository runs through it, its checkout holding no copy.
STORES_RUNNER = ("python", "/opt/stores/stores.py", "exec", "--")


#: How a ``git_repo = true`` check runs (``$1`` the checkout root relative to the check's ``cwd``, then the
#: command): a checkout has no ``.git``, so a script that lists the tracked files or finds the repository root
#: failed in the sandbox and passed in CI. The run makes a repository at the checkout root with one commit of
#: the tree (``.gitignore`` honoured; the checkout record, ``.xtm/`` and installed trees left out), ``origin/main`` and
#: ``origin/master`` on it, runs the command and removes the repository whatever it ended with. A checkout
#: that holds a ``.git`` of its own is run as it is. The repository lives in a directory of its own outside the
#: checkout (``GIT_DIR`` and ``GIT_WORK_TREE`` exported to the command), so two such checks run at the same
#: time on one checkout never share a ``.git``: neither waits on the other's ``.git/config`` lock nor loses its
#: repository when the other one ends and removes it.
GIT_REPO_SHELL = """set -u
top="$1"; shift
if [ -e "$top/.git" ]; then "$@"; exit $?; fi
GIT_WORK_TREE=$(cd "$top" && pwd) || exit 1
GIT_DIR=$(mktemp -d "${TMPDIR:-/tmp}/xtm-git.XXXXXX") || exit 1
export GIT_DIR GIT_WORK_TREE
trap 'rm -rf "$GIT_DIR"' EXIT
g() { git -C "$top" -c user.name=xtm-one -c user.email=xtm-one@localhost -c init.defaultBranch=main "$@"; }
g init -q && g add -A -- . ':!.xtm' ':!.xtm-checkout' ':!.xtm-checkout.json' ':(exclude,glob)**/node_modules/**' \\
  ':(exclude,glob)**/.venv/**' ':(exclude,glob)**/.xtm/**' \\
  && g commit -q --allow-empty --no-verify -m checkout \\
  && g update-ref refs/remotes/origin/main HEAD && g update-ref refs/remotes/origin/master HEAD || exit 1
"$@"
exit $?
"""


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    argv: tuple[str, ...]
    port: int
    cwd: str = ""
    image: str = ""
    #: Path answered once the service is up (``/api/health``), ``""`` for a bare TCP check.
    ready_path: str = ""
    #: Text the service's output must hold before it counts ready (``"lab up: "``), ``""`` for the port alone:
    #: what a service logs after its port answers (a seed, the URLs it prints) is then in the first logs read.
    ready_log: str = ""
    #: Text that opens each signed-in session line the service logs (``"seeded signed-in session"``), ``""`` for none:
    #: an agentic lab repeats those lines (``<label>: <url>``, the URL on its preview) in every start and status answer,
    #: so a run resumed after a restart signs its browser in again with one navigate.
    session_log: str = ""
    #: Seconds the service may take to answer on its port (and to log ``ready_log``).
    timeout: int = SERVICE_DEFAULT_TIMEOUT
    #: Seconds a restart in place (``sandbox_start_service`` sync with restart) should take to be ready again; the
    #: answer says whether it was met. ``0``: none stated.
    restart_target: int = 0
    #: Seconds its container lives without a status or logs call.
    lifetime: int = SERVICE_DEFAULT_LIFETIME
    env: tuple[tuple[str, str], ...] = ()
    #: Services started before this one, in the same container.
    depends_on: tuple[str, ...] = ()
    description: str = ""
    #: Repository paths (files or directories, relative to the checkout root) the service reads:
    #: a start on a checkout without one of them is refused before any container.
    needs: tuple[str, ...] = ()


@dataclass
class Finding:
    path: str
    line: int
    rule: str
    message: str
    severity: str = "error"

    def render(self) -> str:
        where = f"{self.path}:{self.line}" if self.line > 0 else (self.path or "-")
        rule = f" [{self.rule}]" if self.rule else ""
        return f"{where}: {self.severity}{rule} {self.message}".rstrip()


@dataclass
class ReportSummary:
    kind: str
    findings: list[Finding] = field(default_factory=list)
    more: int = 0
    tests: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0
    problem: str = ""
    #: A remark on a report read in part (junk after its first document), shown after the headline.
    note: str = ""
    #: (test name, whole message and body) of every failed or errored test, unbounded by MAX_FINDINGS.
    failures: list[tuple[str, str]] = field(default_factory=list)

    def failures_text(self) -> str:
        """The failures file: each failing test's name then its whole message and body, bounded."""
        text = "\n\n".join(f"=== {name}\n{body}" for name, body in self.failures)
        return text[:MAX_FAILURES_CHARS]

    def headline(self) -> str:
        if self.problem:
            return f"{self.kind} report: {self.problem}"
        if self.kind == "junit":
            counts = (
                f"junit: {self.tests} test(s), {self.passed} passed, {self.failed} failed, "
                f"{self.errors} error(s), {self.skipped} skipped"
            )
            return f"{counts} ({self.note})" if self.note else counts
        if self.kind == "sarif":
            count = len(self.findings) + self.more
            return f"sarif: {count} finding(s)" if count else "sarif: no finding"
        return ""


# -- the declaration -----------------------------------------------------------


def _text(raw: dict, key: str, name: str) -> str:
    value = raw.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ChecksError(f"[check.{name}] {key} must be a string.")
    return value.strip()


def _relative(path: str, key: str, name: str) -> str:
    """*path* as a clean relative path inside the checkout, or :class:`ChecksError`."""
    if not path:
        return ""
    if path.startswith("/") or "\\" in path or "\x00" in path:
        raise ChecksError(f"[check.{name}] {key} must be a relative path inside the checkout.")
    clean = posixpath.normpath(path)
    if clean == ".":
        return ""
    if clean == ".." or clean.startswith("../"):
        raise ChecksError(f"[check.{name}] {key} must stay inside the checkout.")
    return clean


def declares_checks(text: str) -> bool:
    """Whether a ``checks.toml`` holds a ``check`` table; one that declares services only keeps the detected checks.

    A file that does not parse counts as declaring: :func:`load_checks` then names its error.
    """
    try:
        return "check" in tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return True


def load_checks(text: str) -> dict[str, CheckSpec]:
    """The checks a ``checks.toml`` declares, by name; :class:`ChecksError` on a bad file."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ChecksError(f"{CHECKS_FILE} is not valid TOML: {exc}") from exc
    table = data.get("check")
    if not isinstance(table, dict) or not table:
        raise ChecksError(f"{CHECKS_FILE} declares no [check.NAME] table.")
    specs: dict[str, CheckSpec] = {}
    for key, raw in table.items():
        name = str(key)
        if not isinstance(raw, dict):
            raise ChecksError(f"[check.{name}] must be a table.")
        argv = raw.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
            raise ChecksError(f"[check.{name}] needs argv, a non-empty list of strings.")
        if len(argv) > MAX_ARGS or any(len(a) > MAX_ARG_CHARS for a in argv):
            raise ChecksError(f"[check.{name}] argv is at most {MAX_ARGS} items of {MAX_ARG_CHARS} characters.")
        report = _text(raw, "report", name) or "lines"
        if report not in REPORT_KINDS:
            raise ChecksError(f"[check.{name}] report must be one of {', '.join(REPORT_KINDS)}.")
        report_path = _relative(_text(raw, "report_path", name), "report_path", name)
        if report != "lines" and not report_path:
            raise ChecksError(f"[check.{name}] report = {report!r} needs report_path, the file the check writes.")
        timeout_raw = raw.get("timeout", DEFAULT_TIMEOUT)
        if isinstance(timeout_raw, bool) or not isinstance(timeout_raw, int) or timeout_raw <= 0:
            raise ChecksError(f"[check.{name}] timeout must be a positive number of seconds.")
        defaults = raw.get("default_args", [])
        if not isinstance(defaults, list) or not all(isinstance(a, str) and a for a in defaults):
            raise ChecksError(f"[check.{name}] default_args must be a list of non-empty strings.")
        if len(argv) + len(defaults) > MAX_ARGS or any(len(a) > MAX_ARG_CHARS for a in defaults):
            raise ChecksError(f"[check.{name}] argv and default_args are at most {MAX_ARGS} items of {MAX_ARG_CHARS} characters.")
        mode = _text(raw, "affected", name) or "tests"
        if mode not in AFFECTED_MODES:
            raise ChecksError(f"[check.{name}] affected must be one of {', '.join(AFFECTED_MODES)}.")
        globs = raw.get("affected_globs", [])
        if not isinstance(globs, list) or not all(isinstance(g, str) and g for g in globs) or len(globs) > MAX_AFFECTED_GLOBS:
            raise ChecksError(f"[check.{name}] affected_globs must be a list of at most {MAX_AFFECTED_GLOBS} non-empty strings.")
        if globs and mode == "tests":
            raise ChecksError(f"[check.{name}] affected_globs applies to affected = \"files\" or \"lines\" only.")
        always = raw.get("affected_always", [])
        if (
            not isinstance(always, list)
            or not all(isinstance(g, str) and g for g in always)
            or len(always) > MAX_AFFECTED_GLOBS
        ):
            raise ChecksError(
                f"[check.{name}] affected_always must be a list of at most {MAX_AFFECTED_GLOBS} non-empty strings."
            )
        if always and mode != "tests":
            raise ChecksError(f'[check.{name}] affected_always applies to affected = "tests" only.')
        scans = raw.get("affected_scan_patterns", [])
        if (
            not isinstance(scans, list)
            or not all(isinstance(p, str) and p for p in scans)
            or len(scans) > MAX_AFFECTED_GLOBS
        ):
            raise ChecksError(
                f"[check.{name}] affected_scan_patterns must be a list of at most {MAX_AFFECTED_GLOBS} non-empty strings."
            )
        if scans and mode != "tests":
            raise ChecksError(f'[check.{name}] affected_scan_patterns applies to affected = "tests" only.')
        for pattern in scans:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ChecksError(
                    f"[check.{name}] affected_scan_patterns: {pattern!r} is not a regular expression ({exc})."
                ) from exc
        by_default = raw.get("affected_by_default", False)
        if not isinstance(by_default, bool):
            raise ChecksError(f"[check.{name}] affected_by_default must be true or false.")
        whole_timeout = raw.get("whole_timeout", 0)
        if isinstance(whole_timeout, bool) or not isinstance(whole_timeout, int) or whole_timeout < 0:
            raise ChecksError(f"[check.{name}] whole_timeout must be a positive number of seconds.")
        if whole_timeout and not by_default and mode != "tests":
            raise ChecksError(
                f'[check.{name}] whole_timeout applies to an affected = "tests" or affected_by_default check.'
            )
        # On a tests check whole_timeout alone is the switch (frontend-unit): the key a parser older than it
        # ignores, so a checks.toml that declares it never breaks the checks of a platform not yet carrying it.
        by_default = by_default or bool(whole_timeout)
        narrows = raw.get("affected_narrows_args", False)
        if not isinstance(narrows, bool):
            raise ChecksError(f"[check.{name}] affected_narrows_args must be true or false.")
        if narrows and mode == "tests":
            raise ChecksError(f'[check.{name}] affected_narrows_args applies to affected = "files" or "lines" only.')
        whole_repository = raw.get("affected_whole_repository", False)
        if not isinstance(whole_repository, bool):
            raise ChecksError(f"[check.{name}] affected_whole_repository must be true or false.")
        if whole_repository and mode != "files":
            raise ChecksError(f'[check.{name}] affected_whole_repository applies to affected = "files" only.')
        install_first = raw.get("install_first", True)
        if not isinstance(install_first, bool):
            raise ChecksError(f"[check.{name}] install_first must be true or false.")
        fallback_image, fallback_when, fallback_reason = _fallback(raw, name)
        git_repo = raw.get("git_repo", False)
        if not isinstance(git_repo, bool):
            raise ChecksError(f"[check.{name}] git_repo must be true or false.")
        stores = raw.get("stores", False)
        if not isinstance(stores, bool):
            raise ChecksError(f"[check.{name}] stores must be true or false.")
        pre_push = raw.get("pre_push", [])
        if (
            not isinstance(pre_push, list)
            or not all(isinstance(g, str) and g for g in pre_push)
            or len(pre_push) > MAX_AFFECTED_GLOBS
        ):
            raise ChecksError(
                f"[check.{name}] pre_push must be a list of at most {MAX_AFFECTED_GLOBS} non-empty globs."
            )
        pre_push_on = raw.get("pre_push_on", list(PRE_PUSH_KINDS))
        if (
            not isinstance(pre_push_on, list)
            or not pre_push_on
            or not all(isinstance(k, str) and k in PRE_PUSH_KINDS for k in pre_push_on)
        ):
            raise ChecksError(f"[check.{name}] pre_push_on must be a non-empty list of {', '.join(PRE_PUSH_KINDS)}.")
        stands_for = raw.get("stands_for", [])
        if (
            not isinstance(stands_for, list)
            or not all(isinstance(g, str) and g and g != name for g in stands_for)
            or len(stands_for) > MAX_AFFECTED_GLOBS
        ):
            raise ChecksError(
                f"[check.{name}] stands_for must be a list of at most {MAX_AFFECTED_GLOBS} names of other checks."
            )
        specs[name] = CheckSpec(
            name=name,
            argv=tuple(argv),
            cwd=_relative(_text(raw, "cwd", name), "cwd", name),
            timeout=min(timeout_raw, MAX_TIMEOUT),
            image=_text(raw, "image", name),
            report=report,
            report_path=report_path,
            description=_text(raw, "description", name),
            default_args=tuple(defaults),
            env=_env(raw, f"check.{name}"),
            affected=mode,
            affected_globs=tuple(globs),
            affected_always=tuple(always),
            affected_scan_patterns=tuple(scans),
            affected_by_default=by_default,
            whole_timeout=min(whole_timeout, timeout_raw, MAX_TIMEOUT),
            affected_narrows_args=narrows,
            affected_whole_repository=whole_repository,
            install_first=install_first,
            fallback_image=fallback_image,
            fallback_when=fallback_when,
            fallback_reason=fallback_reason,
            needs=_needs(raw, f"check.{name}"),
            pre_push=tuple(pre_push),
            pre_push_on=tuple(k for k in PRE_PUSH_KINDS if k in pre_push_on),
            stands_for=tuple(stands_for),
            git_repo=git_repo,
            stores=stores,
        )
    return specs


def _fallback(raw: dict[str, object], name: str) -> tuple[str, str, str]:
    """``fallback = { image, when, reason }`` of an install check: (image, when, reason), empty when absent."""
    table = raw.get("fallback")
    if table is None:
        return "", "", ""
    if name.rsplit(":", 1)[-1].rsplit("-", 1)[-1] != "install":
        raise ChecksError(f"[check.{name}] fallback applies to an install check only (a name ending in -install).")
    if not isinstance(table, dict) or set(table) - {"image", "when", "reason"}:
        raise ChecksError(f"[check.{name}] fallback must be a table of image, when and reason.")
    values = []
    for key in ("image", "when", "reason"):
        value = table.get(key, "")
        if not isinstance(value, str):
            raise ChecksError(f"[check.{name}] fallback.{key} must be a string.")
        values.append(value.strip())
    image, when, reason = values
    if not image or not when:
        raise ChecksError(
            f"[check.{name}] fallback needs image (where the install runs again) and when "
            "(the text of its output that sends it there)."
        )
    if len(when) > MAX_FALLBACK_CHARS or len(reason) > MAX_FALLBACK_CHARS:
        raise ChecksError(f"[check.{name}] fallback.when and fallback.reason are at most {MAX_FALLBACK_CHARS} characters.")
    if image == _text(raw, "image", name):
        raise ChecksError(f"[check.{name}] fallback.image must differ from the check's own image.")
    return image, when, reason


def _seconds(raw: dict[str, object], key: str, name: str, default: int, ceiling: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ChecksError(f"[service.{name}] {key} must be a positive number of seconds.")
    return min(int(value), ceiling)


def _env(raw: dict[str, object], table: str) -> tuple[tuple[str, str], ...]:
    """The ``env`` of ``[check.NAME]`` or ``[service.NAME]`` (*table*), validated and sorted."""
    env = raw.get("env", {})
    if env is None:
        return ()
    if not isinstance(env, dict):
        raise ChecksError(f"[{table}] env must be a table of NAME = \"value\".")
    if len(env) > SERVICE_MAX_ENV:
        raise ChecksError(f"[{table}] env holds at most {SERVICE_MAX_ENV} variables.")
    pairs = []
    for key, value in env.items():
        key = str(key)
        if not key or key[0].isdigit() or not set(key) <= _ENV_NAME_CHARS:
            raise ChecksError(f"[{table}] env name {key!r} must be upper-case letters, digits and _.")
        if isinstance(value, bool) or not isinstance(value, str | int):
            raise ChecksError(f"[{table}] env {key} must be a string.")
        text = str(value)
        if len(text) > SERVICE_MAX_ENV_CHARS or "\x00" in text:
            raise ChecksError(f"[{table}] env {key} is at most {SERVICE_MAX_ENV_CHARS} characters.")
        pairs.append((key, text))
    return tuple(sorted(pairs))


def _needs(raw: dict[str, object], table: str) -> tuple[str, ...]:
    """The ``needs`` of ``[check.NAME]`` or ``[service.NAME]`` (*table*): clean relative paths, deduplicated."""
    needs = raw.get("needs", []) or []
    if not isinstance(needs, list) or not all(isinstance(n, str) and n.strip() for n in needs):
        raise ChecksError(f"[{table}] needs must be a list of paths relative to the checkout root.")
    if len(needs) > SERVICE_MAX_NEEDS:
        raise ChecksError(f"[{table}] needs holds at most {SERVICE_MAX_NEEDS} paths.")
    paths: list[str] = []
    for need in needs:
        try:
            clean = _relative(need.strip().rstrip("/"), "needs", table)
        except ChecksError as exc:
            raise ChecksError(f"[{table}] needs {need!r} must be a relative path inside the checkout.") from exc
        if not clean:
            raise ChecksError(f"[{table}] needs {need!r} names the checkout itself: name a path inside it.")
        if clean not in paths:
            paths.append(clean)
    return tuple(paths)


def load_services(text: str) -> dict[str, ServiceSpec]:
    """The services a ``checks.toml`` declares, by name (none is fine); :class:`ChecksError` on a bad table."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ChecksError(f"{CHECKS_FILE} is not valid TOML: {exc}") from exc
    table = data.get("service") or {}
    if not isinstance(table, dict):
        raise ChecksError(f"{CHECKS_FILE}: service must be a set of [service.NAME] tables.")
    specs: dict[str, ServiceSpec] = {}
    for key, raw in table.items():
        name = str(key)
        if not isinstance(raw, dict):
            raise ChecksError(f"[service.{name}] must be a table.")
        argv = raw.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
            raise ChecksError(f"[service.{name}] needs argv, a non-empty list of strings.")
        if len(argv) > MAX_ARGS or any(len(a) > SERVICE_MAX_ARG_CHARS for a in argv):
            longest = max(len(a) for a in argv)
            raise ChecksError(
                f"[service.{name}] argv is at most {MAX_ARGS} items of {SERVICE_MAX_ARG_CHARS} characters "
                f"(found {len(argv)} items, the longest {longest} characters): a longer script goes in a file of the "
                'checkout, run as argv = ["sh", "path/to/script.sh"].'
            )
        port = raw.get("port")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ChecksError(f"[service.{name}] needs port, the TCP port it listens on (1-65535).")
        ready_path = raw.get("ready_path", "") or ""
        if not isinstance(ready_path, str) or (ready_path and (not ready_path.startswith("/") or any(c in ready_path for c in " \t\r\n\x00"))):
            raise ChecksError(f"[service.{name}] ready_path must be a path starting with /.")
        ready_log = raw.get("ready_log", "") or ""
        if not isinstance(ready_log, str) or len(ready_log) > SERVICE_MAX_READY_LOG or any(c in ready_log for c in "\r\n\x00"):
            raise ChecksError(f"[service.{name}] ready_log must be one line of at most {SERVICE_MAX_READY_LOG} characters.")
        session_log = raw.get("session_log", "") or ""
        if (
            not isinstance(session_log, str)
            or len(session_log) > SERVICE_MAX_READY_LOG
            or any(c in session_log for c in "\r\n\x00")
        ):
            raise ChecksError(
                f"[service.{name}] session_log must be one line of at most {SERVICE_MAX_READY_LOG} characters."
            )
        depends = raw.get("depends_on", []) or []
        if not isinstance(depends, list) or not all(isinstance(d, str) and d for d in depends):
            raise ChecksError(f"[service.{name}] depends_on must be a list of service names.")
        specs[name] = ServiceSpec(
            name=name,
            argv=tuple(argv),
            port=port,
            cwd=_relative(_text(raw, "cwd", name), "cwd", name),
            image=_text(raw, "image", name),
            ready_path=ready_path,
            ready_log=ready_log,
            session_log=session_log,
            timeout=_seconds(raw, "timeout", name, SERVICE_DEFAULT_TIMEOUT, SERVICE_MAX_TIMEOUT),
            restart_target=(
                _seconds(raw, "restart_target", name, SERVICE_DEFAULT_TIMEOUT, SERVICE_MAX_TIMEOUT)
                if "restart_target" in raw
                else 0
            ),
            lifetime=_seconds(raw, "lifetime", name, SERVICE_DEFAULT_LIFETIME, SERVICE_MAX_LIFETIME),
            env=_env(raw, f"service.{name}"),
            depends_on=tuple(depends),
            description=_text(raw, "description", name),
            needs=_needs(raw, f"service.{name}"),
        )
    for spec in specs.values():
        for dep in spec.depends_on:
            if dep not in specs:
                raise ChecksError(f"[service.{spec.name}] depends on {dep!r}, which is not declared.")
    return specs


def service_chain(
    specs: dict[str, ServiceSpec], name: str, *, own_images: frozenset[str] | set[str] = frozenset()
) -> list[ServiceSpec]:
    """*name* and what it depends on, dependencies first; :class:`ChecksError` on a cycle or a mixed image.

    *own_images* are image keys a dependency may name although the chain shares one container elsewhere: an
    agentic lab runs each service in a container of its own (``AGENTIC_LABS_IMAGES``), so a dependency on one
    of them is not a mixed image. The started service itself always runs on the chain's image.
    """
    order: list[ServiceSpec] = []
    state: dict[str, str] = {}

    def visit(current: str, path: tuple[str, ...]) -> None:
        if state.get(current) == "done":
            return
        if state.get(current) == "open":
            raise ChecksError(f"[service.{name}] depends_on has a cycle: {' -> '.join((*path, current))}.")
        state[current] = "open"
        for dep in specs[current].depends_on:
            visit(dep, (*path, current))
        state[current] = "done"
        order.append(specs[current])

    if name not in specs:
        raise ChecksError(f"{name!r} is not a declared service.")
    visit(name, ())
    started = order[-1]
    strays = [spec for spec in order[:-1] if spec.image != started.image and spec.image not in own_images]
    if strays:
        named = "; ".join(f"{spec.name} runs on image key {spec.image or '(none)'!r}" for spec in strays)
        raise ChecksError(
            f"[service.{name}] and the services it depends on run in one container: they must name the same image "
            f"{started.image or '(none)'!r} ({named}). An agentic lab runs a dependency in a container of its own only "
            f"when AGENTIC_LABS_IMAGES configures its key (configured: {', '.join(sorted(own_images)) or 'none'}); "
            "add each key named here to it, or declare the dependency on the same image. Nothing was started."
        )
    ports = [spec.port for spec in order]
    if len(ports) != len(set(ports)):
        raise ChecksError(f"[service.{name}] and the services it depends on must listen on different ports.")
    return order


#: The services part of a check listing longer than this is answered by name only.
SERVICES_LISTING_MAX_CHARS = 2_000


def describe_services(specs: dict[str, ServiceSpec], *, budget: int = 0) -> str:
    """One line per declared service, for a refusal or a listing.

    *budget* (the check listing): past that many characters the services are
    named on one line, their descriptions one ``sandbox_start_service`` call
    away; a check listing read for one check name stays short on a large lab.
    """
    full = _describe_services(specs)
    if not budget or len(full) <= budget:
        return full
    names = ", ".join(spec.name for spec in specs.values())
    return (
        f"{len(specs)} services: {names}\n(sandbox_start_service without a service lists them with their "
        "descriptions, ports and dependencies)"
    )


def _describe_services(specs: dict[str, ServiceSpec]) -> str:
    lines = []
    for spec in specs.values():
        what = f" ({spec.description})" if spec.description else ""
        lines.append(f"- {spec.name}{what}: port {spec.port}" + (f", after {', '.join(spec.depends_on)}" if spec.depends_on else ""))
    return "\n".join(lines) or "(none)"


def extra_args(args: object) -> list[str]:
    """The arguments a call appends to a check's argv, bounded; :class:`ChecksError` otherwise."""
    if args is None:
        return []
    if isinstance(args, str):
        args = [args]
    if not isinstance(args, list):
        raise ChecksError("args must be a list of strings.")
    items = [str(a) for a in args]
    if len(items) > MAX_ARGS or any(len(a) > CALL_MAX_ARG_CHARS for a in items):
        longest = max((len(a) for a in items), default=0)
        raise ChecksError(
            f"args is at most {MAX_ARGS} items of {CALL_MAX_ARG_CHARS} characters (found {len(items)} items, "
            f"the longest {longest} characters): a longer text goes in a file of the checkout."
        )
    if any("\x00" in a for a in items):
        raise ChecksError("args holds no control character.")
    return items


def command_argv(spec: CheckSpec, args: object = None) -> tuple[str, ...]:
    """What a call of *spec* runs: its argv, then the call's *args*, else its ``default_args``.

    The call's arguments replace the declared defaults, so a check declared
    ``argv = ["yarn", "eslint"]``, ``default_args = ["src/"]`` lints one file
    with ``args=["src/x.tsx"]`` and the whole ``src/`` without args.
    """
    given = extra_args(args)
    argv = (*spec.argv, *(given or spec.default_args))
    if spec.stores:
        argv = (*STORES_RUNNER, *argv)
    if not spec.git_repo:
        return argv
    depth = len([part for part in spec.cwd.strip("/").split("/") if part])
    top = "/".join([".."] * depth) or "."
    return ("sh", "-c", GIT_REPO_SHELL, "xtm-git", top, *argv)


def shown_argv(argv: Iterable[str]) -> str:
    """*argv* joined for a listing or an answer, an inline script (an argument over several lines) named, not printed.

    A detected check may carry a whole shell script as one argument
    (``sh -c <script> name args``, :data:`detect.SCRIPT_TEST_SHELL`): it is
    the platform's, the same on every run, so the answer says what runs in
    one line instead of echoing it into the context each time.
    """
    return " ".join(f"(inline script of {a.count(chr(10)) + 1} lines)" if "\n" in a else a for a in argv)


#: A check listing longer than this is answered compact: one short line per check.
LISTING_MAX_CHARS = 4_000
#: Characters of a check's description in a compact listing.
COMPACT_DESCRIPTION_CHARS = 90


def describe_checks(specs: dict[str, CheckSpec], *, compact: bool = False) -> str:
    """One line per declared check, for a refusal or a listing; *compact*: its name, short description and directory."""
    lines = []
    for spec in specs.values():
        where = f" in {spec.cwd}/" if spec.cwd else ""
        if compact:
            short = spec.description[:COMPACT_DESCRIPTION_CHARS]
            short += "..." if len(spec.description) > COMPACT_DESCRIPTION_CHARS else ""
            lines.append(f"- {spec.name}" + (f": {short}" if short else "") + where)
            continue
        what = f" ({spec.description})" if spec.description else ""
        image = f" [image {spec.image}]" if spec.image else ""
        report = f" [report {spec.report}: {spec.report_path}]" if spec.report != "lines" and spec.report_path else ""
        defaults = f" [default args, replaced by args: {shown_argv(spec.default_args)}]" if spec.default_args else ""
        affected = f" [affected=true: the changed {spec.affected}]" if spec.affected != "tests" else ""
        if spec.affected_by_default:
            bound = spec.whole_timeout or spec.timeout
            affected += f" [no args: affected=true; {WHOLE_ARG} alone: the whole run, at most {bound} s]"
        if spec.affected_narrows_args:
            affected += f" [args narrowed to the changed {spec.affected}; {WHOLE_ARG}: as given]"
        if spec.affected_whole_repository:
            affected += " [the changed files of the whole repository, relative to its directory]"
        if spec.git_repo:
            affected += " [in a throwaway git repository of the checkout]"
        if spec.stores:
            affected += " [with an offline postgres on 127.0.0.1:5432, PG* variables set]"
        lines.append(f"- {spec.name}{what}: {shown_argv(spec.argv)}{where}{image}{report}{defaults}{affected}")
    return "\n".join(lines)


# -- the reports ---------------------------------------------------------------


def _bounded(summary: ReportSummary, findings: list[Finding]) -> ReportSummary:
    order = {name: index for index, name in enumerate(SEVERITIES)}
    findings.sort(key=lambda f: (-order.get(f.severity, len(order)), f.path, f.line))
    summary.findings = findings[:MAX_FINDINGS]
    summary.more = max(0, len(findings) - MAX_FINDINGS)
    return summary


#: A finding message saying a file was missing, and the quoted path it names.
_MISSING_FILE = re.compile(r"No such file or directory|ENOENT|FileNotFoundError|Cannot find module")
_QUOTED_PATH = re.compile(r"""['"`]([^'"`\s]+)['"`]""")


def fold_missing_files(findings: list[Finding]) -> list[Finding]:
    """*findings* with the tests failing on one missing file folded into one line per missing path.

    On a narrow checkout dozens of tests fail on the same absent file (a fixture outside the
    sub_paths): one finding per test buried the real failures (36 lines for 3 relevant tests).
    The first finding of each path is kept, its message saying how many more failed on it.
    """
    out: list[Finding] = []
    first: dict[str, Finding] = {}
    extra: dict[str, int] = {}
    for finding in findings:
        if _MISSING_FILE.search(finding.message):
            quoted = _QUOTED_PATH.search(finding.message)
            key = quoted.group(1) if quoted else finding.message
            if key in first:
                extra[key] += 1
                continue
            first[key] = finding
            extra[key] = 0
        out.append(finding)
    for key, more in extra.items():
        if more:
            first[key].message += f" (and {more} more test(s) failing on the same missing file)"
    return out


#: An ANSI colour or cursor sequence: a coloured runner output or a highlighted DOM dump. A JUnit
#: report drops the escape character (not valid XML) and keeps the rest, so a colour code without
#: it ("[36m") is one too.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\[[0-9;]{1,8}m")
#: Where a testing-library error starts echoing the DOM it searched (the element list or the whole body).
_DOM_DUMP = re.compile(r"\n\s*\n(?=\s*(?:Here are the matching elements:|Ignored nodes:|<[A-Za-z!/]))")
#: The characters one finding's message keeps.
FINDING_MESSAGE_CHARS = 400


def finding_message(message: str) -> str:
    """*message* as one finding shows it: colour codes stripped, a testing-library DOM dump cut, bounded.

    A query matching several elements echoed each element whole, colour codes included, in every
    failing test's finding (22 findings, 26.9k characters, one root cause). The failures file of the
    run directory keeps the whole message.
    """
    text = _ANSI.sub("", message)
    dump = _DOM_DUMP.search(text)
    if dump is None:
        return text[:FINDING_MESSAGE_CHARS]
    marker = " [DOM dump left out: the failures file keeps it]"
    return text[: dump.start()].rstrip()[: FINDING_MESSAGE_CHARS - len(marker)] + marker


def fold_same_message(findings: list[Finding]) -> list[Finding]:
    """*findings* with the tests failing on one same message folded into the first, the others counted.

    One cause failing many tests (an ambiguous query in a shared helper) listed the same message
    once per test; the first finding is kept, its message saying how many more failed with it.
    """
    out: list[Finding] = []
    first: dict[str, Finding] = {}
    extra: Counter[str] = Counter()
    for finding in findings:
        if finding.message and finding.message in first:
            extra[finding.message] += 1
            continue
        if finding.message:
            first[finding.message] = finding
        out.append(finding)
    for message, more in extra.items():
        kept = first[message]
        kept.message += f" (and {more} more test(s) failing with the same message, named in the failures file)"
    return out


def _int(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _first_document(text: str, exc: ParseError) -> str:
    """*text* up to where the parser met junk after the first XML document, ``""`` for any other error."""
    if "junk after document element" not in str(exc):
        return ""
    line, column = getattr(exc, "position", (0, 0))
    if line < 1:
        return ""
    lines = text.splitlines(keepends=True)
    return "".join(lines[: line - 1]) + (lines[line - 1][:column] if line <= len(lines) else "")


def parse_junit(text: str) -> ReportSummary:
    """Tests counted and the failed or errored ones as findings (JUnit XML as pytest writes it).

    A report with junk after its first document (a second write appended to the file) is read
    from that first document; any other unreadable report keeps its raw text as the failures
    file, so the run directory still shows what the runner wrote.
    """
    summary = ReportSummary(kind="junit")
    root: Any = None
    try:
        root = fromstring(text)
    except ParseError as exc:
        first = _first_document(text, exc)
        if first:
            with contextlib.suppress(ParseError):
                root = fromstring(first)
        if root is None:
            summary.problem = f"not valid JUnit XML ({exc}); the raw report is in the failures file"
            summary.failures.append(("raw JUnit report", text))
            return summary
        summary.note = f"junk after the first document ignored ({exc})"
    findings: list[Finding] = []
    for case in root.iter("testcase"):
        summary.tests += 1
        name = f"{case.get('classname', '')}::{case.get('name', '')}".strip(":")
        path = case.get("file") or ""
        line = _int(case.get("line")) + 1 if case.get("line") else 0
        failed = case.find("failure")
        errored = case.find("error")
        if case.find("skipped") is not None:
            summary.skipped += 1
            continue
        node = failed if failed is not None else errored
        if node is None:
            summary.passed += 1
            continue
        if failed is not None:
            summary.failed += 1
        else:
            summary.errors += 1
        message = node.get("message") or ""
        body = (node.text or "").strip()
        if not message:
            body_lines = body.splitlines()
            message = body_lines[-1] if body_lines else ""
        where = f"{path}:{line} " if path and line else (f"{path} " if path else "")
        summary.failures.append((f"{where}{name}".strip(), "\n".join(part for part in (message, body) if part)))
        findings.append(
            Finding(
                path=path or name,
                line=line,
                rule=name if path else "",
                message=finding_message(message),
                severity="error",
            )
        )
    return _bounded(summary, fold_same_message(fold_missing_files(findings)))


_SARIF_LEVELS = {"error": "error", "warning": "warning", "note": "note", "none": "note"}


def parse_sarif(text: str) -> ReportSummary:
    """Every SARIF result as a finding (path, line, rule, message, level)."""
    summary = ReportSummary(kind="sarif")
    try:
        data = json.loads(text)
    except ValueError as exc:
        summary.problem = f"not valid SARIF JSON ({exc})"
        return summary
    runs = data.get("runs") if isinstance(data, dict) else None
    if not isinstance(runs, list):
        summary.problem = "no runs[] in the SARIF document"
        return summary
    findings: list[Finding] = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        for result in run.get("results") or []:
            if not isinstance(result, dict):
                continue
            path, line = "", 0
            for location in result.get("locations") or []:
                physical = (location or {}).get("physicalLocation") or {}
                artifact = physical.get("artifactLocation") or {}
                path = str(artifact.get("uri") or "")
                if path.startswith("file://"):
                    path = path[len("file://") :]
                line = _int((physical.get("region") or {}).get("startLine"))
                break
            message = result.get("message") or {}
            text_message = message.get("text") if isinstance(message, dict) else str(message)
            level = _SARIF_LEVELS.get(str(result.get("level") or "warning"), "warning")
            findings.append(
                Finding(
                    path=path,
                    line=line,
                    rule=str(result.get("ruleId") or ""),
                    message=str(text_message or "")[:400],
                    severity=level,
                )
            )
    return _bounded(summary, findings)


def parse_report(kind: str, text: str) -> ReportSummary | None:
    """The summary of a *kind* report, ``None`` for ``lines``."""
    if kind == "junit":
        return parse_junit(text)
    if kind == "sarif":
        return parse_sarif(text)
    return None


# -- the result ----------------------------------------------------------------


_PYTEST_HEADER = re.compile(r"^={3,} (.+?) ={3,}$")
_COVERAGE_START = re.compile(r"^-+ coverage: |^_+ coverage: |% Stmts|Coverage report from |^Name\s+Stmts\s+Miss")
_TABLE_LINE = re.compile(r"^\s*$|\||^[-=\s]+$|^\S.*\s\d+%\s*\S*$|^TOTAL\s|^Name\s+Stmts\s")


def _drop_sections(lines: list[str]) -> list[str]:
    """*lines* without the pytest warnings summary and coverage tables, each left as a one-line marker."""
    kept: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        header = _PYTEST_HEADER.match(line.strip())
        if header and header.group(1).strip() == "warnings summary":
            end = index + 1
            while end < len(lines) and not _PYTEST_HEADER.match(lines[end].strip()):
                end += 1
            kept.append(f"[... pytest warnings summary, {end - index} line(s), left out: see the run directory ...]")
            index = end
            continue
        if _COVERAGE_START.search(line):
            end = index + 1
            while end < len(lines) and _TABLE_LINE.search(lines[end]) and not _PYTEST_HEADER.match(lines[end].strip()):
                end += 1
            if end - index > 2:
                kept.append(f"[... coverage table, {end - index} line(s), left out: see the run directory ...]")
                index = end
                continue
        kept.append(line)
        index += 1
    return kept


#: pytest sections the report's findings already carry: left out of stdout when the findings are shown.
_REPORTED_SECTIONS = frozenset({"FAILURES", "ERRORS", "short test summary info"})
#: A vitest section rule (U+23AF runs around a title: "Failed Tests 22"), its title captured; the
#: "[3/22]" rules between two failures carry no spaced title and are not one.
_VITEST_HEADER = re.compile(r"^\u23af{2,}\s+(\S.*?)\s+\u23af{2,}$")
#: The vitest totals that close its report (" Test Files  1 failed | 3 passed (4)").
_VITEST_TOTALS = re.compile(r"^(Test Files|Tests|Start at|Duration)\s")


def _reported_section_end(lines: list[str], index: int) -> tuple[int, str]:
    """The end of the failure section *lines[index]* opens and its title, ``(index, "")`` when it opens none.

    A pytest FAILURES, ERRORS or short summary section runs to the next pytest header; vitest's
    "Failed Tests N" section (each failure with its stack and any DOM dump) runs to its next
    section rule or its totals.
    """
    stripped = lines[index].strip()
    header = _PYTEST_HEADER.match(stripped)
    if header and header.group(1).strip() in _REPORTED_SECTIONS:
        end = index + 1
        while end < len(lines) and not _PYTEST_HEADER.match(lines[end].strip()):
            end += 1
        return end, f"pytest {header.group(1).strip()}"
    header = _VITEST_HEADER.match(stripped)
    if header and header.group(1).startswith("Failed Tests"):
        end = index + 1
        while end < len(lines) and not (
            _VITEST_HEADER.match(lines[end].strip()) or _VITEST_TOTALS.match(lines[end].strip())
        ):
            end += 1
        return end, f"vitest {header.group(1).strip()}"
    return index, ""


def _drop_reported(lines: list[str]) -> list[str]:
    """*lines* without the pytest and vitest failure sections the findings repeat, each left as a one-line marker."""
    kept: list[str] = []
    index = 0
    while index < len(lines):
        end, title = _reported_section_end(lines, index)
        if title:
            # The closing summary line ("=== 2 failed, 9 passed in 3s ===", vitest's totals) is kept.
            kept.append(
                f"[... {title}, {end - index} line(s), left out: the findings above "
                "carry them, the run directory keeps them ...]"
            )
            index = end
            continue
        kept.append(lines[index])
        index += 1
    return kept


#: One line of a bundler's asset listing: a built file under a directory and its size, vite's gzip and map
#: columns included ("dist/assets/index-a1b2.js   1,234.56 kB │ gzip: 345.67 kB").
_ASSET_LINE = re.compile(r"^\s*\S+/\S+\s+[\d.,]+\s*[kKmM]?i?B(\s*[│|].*)?$")
#: Consecutive asset lines from which the listing is folded into a marker.
ASSET_LISTING_MIN = 3


def _drop_asset_listing(lines: list[str]) -> list[str]:
    """*lines* with each bundler asset listing (vite's ``dist/assets/...  12.3 kB``) left as a one-line marker.

    A build's listing names every emitted file with its size: thousands of characters that say nothing
    a reader acts on (a passing frontend build answered with about 9.8k characters of it). Its warnings
    ("Some chunks are larger than 500 kB") and its closing line ("built in 12.3s") are kept.
    """
    kept: list[str] = []
    index = 0
    while index < len(lines):
        end = index
        while end < len(lines) and _ASSET_LINE.match(lines[end]):
            end += 1
        if end - index >= ASSET_LISTING_MIN:
            kept.append(f"[... build asset listing, {end - index} file(s), left out: see the run directory ...]")
            index = end
            continue
        kept.extend(lines[index : max(end, index + 1)])
        index = max(end, index + 1)
    return kept


def _collapse_repeats(lines: list[str]) -> list[str]:
    """*lines* with back-to-back repeats of a block of up to REPEAT_BLOCK_MAX lines kept once and counted."""
    kept: list[str] = []
    index = 0
    while index < len(lines):
        for size in range(1, REPEAT_BLOCK_MAX + 1):
            block = lines[index : index + size]
            if len(block) < size or not any(part.strip() for part in block):
                continue
            repeats = 0
            while lines[index + size * (repeats + 1) : index + size * (repeats + 2)] == block:
                repeats += 1
            if repeats:
                kept.extend(block)
                what = "line" if size == 1 else f"{size} lines"
                kept.append(f"[... previous {what} repeated {repeats} more time(s) ...]")
                index += size * (repeats + 1)
                break
        else:
            kept.append(lines[index])
            index += 1
    return kept


def _thin_frequent(lines: list[str]) -> list[str]:
    """*lines* with every line seen FREQUENT_LINE times or more kept FREQUENT_KEEP times, the rest counted."""
    counts = Counter(line for line in lines if line.strip())
    frequent = {line for line, count in counts.items() if count >= FREQUENT_LINE}
    if not frequent:
        return lines
    seen: Counter[str] = Counter()
    kept: list[str] = []
    dropped = 0
    for line in lines:
        if line in frequent:
            seen[line] += 1
            if seen[line] > FREQUENT_KEEP:
                dropped += 1
                continue
        kept.append(line)
    kept.append(f"[... {dropped:,} more copies of {len(frequent)} line(s) seen {FREQUENT_LINE}+ times left out ...]")
    return kept


def condense(text: str, *, reported: bool = False) -> str:
    """*text* without what buries a failure: warnings, coverage tables, asset listings, repeated blocks.

    Pure and lossless for the reader: every part left out is named by a marker line, and the
    run directory keeps the output whole. *reported*: the findings already list each failure,
    so pytest's FAILURES, ERRORS and short summary sections are left out too.
    """
    if not text:
        return text
    # Colour codes carry nothing for the reader and hide repeats from the comparisons below.
    lines = _ANSI.sub("", text).splitlines()
    if reported:
        lines = _drop_reported(lines)
    return "\n".join(_thin_frequent(_collapse_repeats(_drop_asset_listing(_drop_sections(lines)))))


def clip(text: str, head: int = OUTPUT_HEAD, tail: int = OUTPUT_TAIL) -> str:
    """*text* whole when it fits, else its head and tail with the elided count."""
    if len(text) <= head + tail:
        return text
    return text[:head] + f"\n[... {len(text) - head - tail:,} characters elided; the whole output is in the run directory ...]\n" + text[-tail:]


def green_tail(text: str, limit: int = GREEN_OUTPUT_CHARS) -> str:
    """The last whole lines of *text* within *limit* characters, the lines left out counted in a marker.

    A check that passed answers with its totals, not its progress or warnings: a linter's pre-existing
    warnings and pytest's progress dots flooded the result of a green run (13k and 18k characters).
    """
    if len(text) <= limit:
        return text
    lines = text.splitlines()
    kept: list[str] = []
    size = 0
    for line in reversed(lines):
        if kept and size + len(line) + 1 > limit:
            break
        kept.append(line[-limit:])
        size += len(line) + 1
    kept.reverse()
    left = len(lines) - len(kept)
    return (
        f"[... exit 0: {left:,} line(s) before the totals left out (warnings, progress); "
        "the whole output is in the run directory, sandbox_grep reads it ...]\n" + "\n".join(kept)
    )


def format_result(
    *,
    check: str,
    argv: list[str],
    cwd: str,
    exit_code: int | None,
    seconds: float,
    timed_out: bool,
    stdout: str,
    stderr: str,
    run_dir: str,
    report: ReportSummary | None,
    report_missing: str = "",
    failures_saved: bool = False,
    green_summary: bool = False,
) -> str:
    """What ``sandbox_run_command`` answers, bounded; the output condensed (:func:`condense`) then clipped.

    *green_summary*: a declared or detected check (never the built-in script or an argv, whose output is
    the answer) that exited 0 keeps only the end of each stream (:func:`green_tail`).
    """
    status = "timed out" if timed_out else ("exit 0" if exit_code == 0 else f"exit {exit_code}")
    lines = [f"{check}: {status} in {seconds:.1f}s ({shown_argv(argv)} in {cwd}/)"]
    if report is not None:
        lines.append(report.headline())
        if report.findings:
            lines.append("Findings:")
            lines.extend(f"  {finding.render()}" for finding in report.findings)
            if report.more:
                lines.append(f"  ... {report.more} more in the report file")
            if failures_saved and report.failures:
                lines.append(f"Whole message of each failing test: {run_dir}/{FAILURES_FILE}.")
        elif report.problem and failures_saved and report.failures:
            lines.append(f"Raw report: {run_dir}/{FAILURES_FILE}; the output below is the runner's own.")
        elif not report.problem and report.kind == "junit" and not (report.failed or report.errors):
            lines.append("No failing test.")
    elif report_missing:
        lines.append(report_missing)
    reported = bool(report is not None and report.findings and failures_saved and report.failures)
    out = clip(condense(stdout.strip(), reported=reported))
    err = clip(condense(stderr.strip(), reported=reported))
    if green_summary and exit_code == 0 and not timed_out:
        out, err = green_tail(out), green_tail(err)
    if out:
        lines.append("stdout:\n" + out)
    if err:
        lines.append("stderr:\n" + err)
    if not out and not err:
        lines.append("(no output)")
    lines.append(f"Whole output and metadata: {run_dir}/ (stdout.txt, stderr.txt, run.json).")
    return "\n".join(lines)
