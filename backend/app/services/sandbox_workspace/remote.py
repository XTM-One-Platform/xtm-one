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

"""The workspace in an OpenSandbox container: brought in as one archive, read back as one.

The container gets the files of its workspace only, never a storage key:
:func:`reconcile` makes ``/tmp/workspace`` hold exactly the manifest (what is
missing or different is sent as one tar, what the manifest does not list is
removed), and :func:`collect` lists what the code changed, then fetches the
changed files as one tar and hashes them again on the server - the sha256 the
container reports only decides what to fetch, never what is recorded.

Both run :data:`TOOL_SRC` with the container's own Python through the
sandbox's command API, outside the user-code wrapper; no user input reaches
its command line. Its ``exec`` action is the detached runner of a check
(:mod:`commands`): the argv comes from a JSON file, never the command line.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import tarfile
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.core.utils import format_exception_message
from app.services.sandbox_workspace import metrics
from app.services.sandbox_workspace.local import ScanResult
from app.services.sandbox_workspace.manifest import ManifestEntry, plan_commit
from app.services.sandbox_workspace.paths import WorkspacePathError, normalize
from app.services.sandbox_workspace.session import PendingFile, WorkspaceSession, sha256_bytes

logger = logging.getLogger(__name__)

REMOTE_WORKSPACE = "/tmp/workspace"  # noqa: S108 - the container's own working directory
TOOL_DIR = "/tmp/.xtm"  # noqa: S108
TOOL_PATH = f"{TOOL_DIR}/ws_tool.py"
_MTIME_TOLERANCE = 2e-3
#: Workspace blobs :func:`reconcile` reads at once from the object store. In
#: sequence, one round trip per file put minutes before the first command on a
#: checkout of several thousand files (the same cost labs.checkout_archive had).
BLOB_READ_CONCURRENCY = 32
#: Tries of one blob read before :func:`reconcile` gives up: a store hiccup under
#: 32 parallel reads is retried once, then the failure names the file.
BLOB_READ_ATTEMPTS = 2
#: Tries of the archive write and unpack before :func:`reconcile` gives up: a write cut by
#: the network, or an archive the helper finds short or different, is sent once more.
UPLOAD_ATTEMPTS = 2


class UploadError(RuntimeError):
    """The workspace archive could not be put in the container, with its size and every attempt's cause."""


class UploadReadError(RuntimeError):
    """A workspace file :func:`reconcile` could not read from the store, named with its cause."""


class HelperOutputError(RuntimeError):
    """The helper left no JSON output; *exit_code* is its exit code (``None`` when it was ended)."""

    def __init__(self, message: str, exit_code: int | None) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def first_leaf(exc: BaseException) -> BaseException:
    """The first exception an exception group wraps, however deep: the one that names the cause."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


TOOL_SRC = r"""
import fnmatch, hashlib, io, json, os, stat, sys, tarfile, time

def _sha(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()

def _ignored(rel, patterns):
    # A pattern matches the path itself, or one of its directories when it
    # names a directory ("node_modules/", "*/.venv/"): a file under an ignored
    # directory is ignored too, never listed as changed nor as removed.
    if any(fnmatch.fnmatchcase(rel, p) for p in patterns):
        return True
    dir_patterns = [p for p in patterns if p.endswith("/")]
    if not dir_patterns:
        return False
    parts = rel.split("/")
    for depth in range(1, len(parts)):
        prefix = "/".join(parts[:depth]) + "/"
        if any(fnmatch.fnmatchcase(prefix, p) for p in dir_patterns):
            return True
    return False

def _list(root, snapshot_path, out_path, max_files, missing_ok, ignore_path=None):
    with open(snapshot_path, encoding="utf-8") as fh:
        snapshot = json.load(fh)
    patterns = []
    if ignore_path and os.path.exists(ignore_path):
        with open(ignore_path, encoding="utf-8") as fh:
            patterns = json.load(fh)
    out = {"changed": [], "removed": [], "skipped": [], "overflow": False, "unreadable": False}
    if missing_ok and not os.path.lexists(root):
        out["removed"] = [path for path in snapshot if not (patterns and _ignored(path, patterns))]
    elif os.path.islink(root) or not os.path.isdir(root):
        out["unreadable"] = True
    else:
        seen = set()
        count = 0
        for dirpath, dirs, files in os.walk(root, followlinks=False):
            for name in list(dirs):
                full = os.path.join(dirpath, name)
                if os.path.islink(full):
                    dirs.remove(name)
                    out["skipped"].append(os.path.relpath(full, root) + " (a link)")
                elif patterns and _ignored(os.path.relpath(full, root).replace(os.sep, "/") + "/", patterns):
                    dirs.remove(name)
            for name in files:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                if patterns and _ignored(rel, patterns):
                    continue
                st = os.lstat(full)
                if stat.S_ISLNK(st.st_mode):
                    out["skipped"].append(rel + " (a link)")
                    continue
                if not stat.S_ISREG(st.st_mode):
                    out["skipped"].append(rel + " (not a regular file)")
                    continue
                count += 1
                if count > max_files:
                    out["overflow"] = True
                    break
                seen.add(rel)
                held = snapshot.get(rel)
                if held and held[0] == st.st_size and abs(held[1] - st.st_mtime) < 2e-3:
                    continue
                sha = _sha(full)
                if held and held[2] == sha:
                    continue
                out["changed"].append([rel, st.st_size, st.st_mtime, sha])
            if out["overflow"]:
                break
        out["removed"] = [path for path in snapshot if path not in seen and not (patterns and _ignored(path, patterns))]
    # Written aside then renamed: a reader never sees a listing half written. The
    # aside name is this process's own, so two lists at once never rename each
    # other's file; the directory is made again if something removed it.
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    tmp = "%s.%d.tmp" % (out_path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(out, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, out_path)
    _sweep_lists(os.path.dirname(out_path) or ".")

# The per-call files of the list step (list-*-snapshot.json and its
# siblings) that no call has touched for an hour: a call ended before reading
# its own leaves them behind.
def _sweep_lists(directory, max_age=3600):
    now = time.time()
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        if not name.startswith(("list-", "stage-")):
            continue
        full = os.path.join(directory, name)
        try:
            if now - os.lstat(full).st_mtime > max_age:
                os.remove(full)
        except OSError:
            pass

def _pack(root, list_path, tar_path):
    with open(list_path, encoding="utf-8") as fh:
        paths = json.load(fh)
    with tarfile.open(tar_path, "w", format=tarfile.PAX_FORMAT) as tf:
        for rel in paths:
            full = os.path.join(root, rel)
            if os.path.isfile(full) and not os.path.islink(full):
                tf.add(full, arcname=rel, recursive=False)

def _unpack(root, tar_path, remove_path, size=None, sha=None):
    # The archive is checked whole before anything is removed or extracted: a
    # write cut short is refused (exit 3) with what arrived, never half applied.
    if size is not None:
        got = os.path.getsize(tar_path)
        digest = _sha(tar_path)
        if got != int(size) or digest != sha:
            sys.stderr.write("archive mismatch: %d of %d bytes, sha256 %s, expected %s\n" % (got, int(size), digest[:12], str(sha)[:12]))
            sys.exit(3)
    os.makedirs(root, exist_ok=True)
    with open(remove_path, encoding="utf-8") as fh:
        for rel in json.load(fh):
            full = os.path.join(root, rel)
            if os.path.isfile(full) or os.path.islink(full):
                os.remove(full)
    with tarfile.open(tar_path) as tf:
        try:
            tf.extractall(root, filter="data")
        except TypeError:
            for member in tf.getmembers():
                if member.isfile() and not member.name.startswith("/") and ".." not in member.name.split("/"):
                    tf.extract(member, root)

# The container's memory limit (cgroup v2, then v1), None when unbounded or unreadable.
def _memory_limit():
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            with open(path, encoding="utf-8") as fh:
                raw = fh.read().strip()
        except OSError:
            continue
        if raw.isdigit() and 0 < int(raw) < (1 << 60):
            return int(raw)
        return None
    return None

# V8 sizes its old space from the host, not from the container: a Node command
# (eslint, tsc) then dies near 2 GB on a pod that has more. Give it three
# quarters of the container's memory, unless the command's NODE_OPTIONS already
# names a heap size (a check may set its own).
def _node_heap_env(env, limit):
    current = env.get("NODE_OPTIONS", "")
    if not limit or "max-old-space-size" in current or "max_old_space_size" in current:
        return env
    heap_mb = int(limit * 3 / 4) // (1024 * 1024)
    if heap_mb < 256:
        return env
    env["NODE_OPTIONS"] = (current + " --max-old-space-size=%d" % heap_mb).strip()
    return env

# A check that runs `pip install X` where the image's site-packages is read-only
# gets a user install ("Defaulting to user installation"): X lands in the user
# base's bin, which is not on PATH, and the next check running `X ...` fails 127.
# Put that bin first on PATH, as the CI runner's own environment has the tools a
# job installed first on its PATH (PYTHONUSERBASE, else HOME/.local).
def _user_bin_env(env):
    base = env.get("PYTHONUSERBASE") or os.path.join(env.get("HOME") or os.path.expanduser("~"), ".local")
    user_bin = os.path.join(base, "bin")
    current = env.get("PATH", "")
    if user_bin in current.split(os.pathsep):
        return env
    env["PATH"] = user_bin + (os.pathsep + current if current else "")
    return env

def _proc_table():
    # pid -> (ppid, pgrp, session, state) read from /proc; {} where /proc is not readable.
    table = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return table
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % name, encoding="utf-8", errors="replace") as fh:
                stat = fh.read()
        except OSError:
            continue
        fields = stat[stat.rfind(")") + 2:].split()
        if len(fields) < 4:
            continue
        try:
            table[int(name)] = (int(fields[1]), int(fields[2]), int(fields[3]), fields[0])
        except ValueError:
            continue
    return table

def _tree_of(root, table):
    # root and every process it started: by parent link, or still in its process group or session.
    found = {root}
    grown = True
    while grown:
        grown = False
        for pid, (ppid, pgrp, sid, _state) in table.items():
            if pid not in found and (ppid in found or pgrp == root or sid == root):
                found.add(pid)
                grown = True
    return found

def _alive(pid, table):
    entry = table.get(pid)
    return entry is not None and entry[3] not in ("Z", "X")

def _end_tree(root):
    # SIGKILL the command's process group and every process it started, a child that left the group
    # (setsid, a detached worker) included: the tree is read before the kill, while the parent links
    # still hold. Returns the pids still alive afterwards ([] where /proc cannot tell).
    import signal, time
    me = os.getpid()
    killed = set()
    for _attempt in range(3):
        table = _proc_table()
        tree = {pid for pid in _tree_of(root, table) if pid != me and _alive(pid, table)} - killed
        for pid in sorted(tree):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        killed |= tree
        try:
            os.killpg(root, signal.SIGKILL)
        except OSError:
            pass
        if not tree:
            break
        time.sleep(0.05)
    deadline = time.monotonic() + 2.0
    while True:
        table = _proc_table()
        survivors = sorted(pid for pid in killed if pid != root and _alive(pid, table))
        if not survivors or time.monotonic() >= deadline:
            return survivors
        time.sleep(0.1)

def _exec(run_dir, cwd, timeout):
    # A detached check (commands.py): the argv in run_dir/argv.json (its declared
    # variables in run_dir/env.json when it has some), run in its own
    # session so a timeout ends its whole process group; stdout, stderr and the pid
    # under run_dir, then exit.json written atomically last: its presence is the end.
    # A file named "stop" in run_dir (sandbox_run_command stop=true) ends the
    # process group too: the end marker then says stopped, with the output so far.
    # A stop or a timeout ends every process the command started (_end_tree); one
    # still alive after it is named in the marker (survivors), never hidden.
    # No core file: a process that aborts (a V8 heap exhaustion, a native
    # crash) would otherwise leave a dump the size of its memory in its working
    # directory, under workspace/, and the read-back would refuse the whole
    # change set for the quota instead of reporting the crash.
    import resource, signal, subprocess, time

    def _no_core():
        try:
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        except (ValueError, OSError):
            pass

    with open(os.path.join(run_dir, "argv.json"), encoding="utf-8") as fh:
        argv = json.load(fh)
    env = dict(os.environ)
    declared = os.path.join(run_dir, "env.json")
    if os.path.exists(declared):
        # The check's own env table (.xtm-one/checks.toml), over the container's variables.
        with open(declared, encoding="utf-8") as fh:
            env.update({str(k): str(v) for k, v in json.load(fh).items()})
    env = _node_heap_env(_user_bin_env(env), _memory_limit())
    started = time.monotonic()
    result = {"exit_code": None, "timed_out": False, "stopped": False}
    stop_path = os.path.join(run_dir, "stop")
    with open(os.path.join(run_dir, "stdout.txt"), "wb") as out, open(os.path.join(run_dir, "stderr.txt"), "wb") as err:
        try:
            proc = subprocess.Popen(
                argv, cwd=cwd, env=env, stdout=out, stderr=err, stdin=subprocess.DEVNULL, start_new_session=True,
                preexec_fn=_no_core,
            )
        except OSError as exc:
            err.write(("%s: %s\n" % (argv[0] if argv else "argv", exc)).encode("utf-8"))
            result["exit_code"] = 127
        else:
            with open(os.path.join(run_dir, "pid"), "w", encoding="utf-8") as fh:
                fh.write(str(proc.pid))
            deadline = started + float(timeout)
            while True:
                try:
                    result["exit_code"] = proc.wait(timeout=max(0.0, min(0.25, deadline - time.monotonic())))
                    break
                except subprocess.TimeoutExpired:
                    pass
                if os.path.exists(stop_path):
                    result["stopped"] = True
                elif time.monotonic() >= deadline:
                    result["timed_out"] = True
                else:
                    continue
                survivors = _end_tree(proc.pid)
                try:
                    proc.kill()
                except OSError:
                    pass
                result["exit_code"] = proc.wait()
                if survivors:
                    result["survivors"] = survivors
                break
    result["seconds"] = time.monotonic() - started
    tmp = os.path.join(run_dir, "exit.json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(result, fh)
    os.replace(tmp, os.path.join(run_dir, "exit.json"))

def _probe(root, paths_path, out_path):
    # Which of the relative paths listed in paths_path are files under root (the
    # install cache checks that what an install produced is still there). The
    # paths come from a JSON file, never the command line; an absolute path or
    # one that climbs out of root is answered absent.
    with open(paths_path, encoding="utf-8") as fh:
        paths = json.load(fh)
    out = {}
    for rel in paths:
        if not isinstance(rel, str) or not rel or os.path.isabs(rel) or ".." in rel.split("/"):
            continue
        out[rel] = os.path.isfile(os.path.join(root, rel))
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(out, fh)

# Installed packages (installed.py): the files under a directory of the workspace that the
# manifest never records (node_modules, .venv), read where the install left them. A link is
# followed while its target stays under root (a pnpm package linked into .pnpm), never
# out of it; a directory reached twice through links is walked once.
def _inside(root, full):
    real_root = os.path.realpath(root)
    real = os.path.realpath(full)
    return real == real_root or real.startswith(real_root + os.sep)

def _safe_rel(rel):
    return isinstance(rel, str) and bool(rel) and not os.path.isabs(rel) and ".." not in rel.split("/")

def _tree(root, in_path, out_path, max_files):
    # The files under the relative directory named in in_path, with their sizes; a regular
    # file named there is its own one-file tree.
    with open(in_path, encoding="utf-8") as fh:
        prefix = json.load(fh)
    base = os.path.join(root, prefix) if prefix else root
    out = {"exists": False, "files": [], "overflow": False}
    if _safe_rel(prefix) and os.path.isfile(base) and _inside(root, base):
        out["exists"] = True
        out["files"].append([prefix, os.path.getsize(base)])
    elif _safe_rel(prefix) and os.path.isdir(base) and _inside(root, base):
        out["exists"] = True
        seen = set()
        for dirpath, dirs, files in os.walk(base, followlinks=True):
            real = os.path.realpath(dirpath)
            if real in seen or not _inside(root, dirpath):
                dirs[:] = []
                continue
            seen.add(real)
            dirs.sort()
            for name in sorted(files):
                full = os.path.join(dirpath, name)
                if not os.path.isfile(full) or not _inside(root, full):
                    continue
                if len(out["files"]) >= max_files:
                    out["overflow"] = True
                    break
                out["files"].append([os.path.relpath(full, root).replace(os.sep, "/"), os.path.getsize(full)])
            if out["overflow"]:
                break
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(out, fh)

def _packin(root, list_path, tar_path, max_bytes):
    # The listed files (relative, under root, at most max_bytes each) as one tar of regular members.
    with open(list_path, encoding="utf-8") as fh:
        paths = json.load(fh)
    with tarfile.open(tar_path, "w", format=tarfile.PAX_FORMAT) as tf:
        for rel in paths:
            if not _safe_rel(rel):
                continue
            full = os.path.join(root, rel)
            if not os.path.isfile(full) or not _inside(root, full) or os.path.getsize(full) > max_bytes:
                continue
            with open(full, "rb") as fh:
                data = fh.read()
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))

if __name__ == "__main__":
    action = sys.argv[1]
    if action == "list":
        _list(sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5]), sys.argv[6] == "1", sys.argv[7] if len(sys.argv) > 7 else None)
    elif action == "pack":
        _pack(sys.argv[2], sys.argv[3], sys.argv[4])
    elif action == "unpack":
        if len(sys.argv) > 6:
            _unpack(sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5]), sys.argv[6])
        else:
            _unpack(sys.argv[2], sys.argv[3], sys.argv[4])
    elif action == "drop":
        # A call's own stage files, once read: never left for the hourly sweep.
        for path in sys.argv[2:]:
            try:
                os.remove(path)
            except OSError:
                pass
    elif action == "exec":
        _exec(sys.argv[2], sys.argv[3], sys.argv[4])
    elif action == "probe":
        _probe(sys.argv[2], sys.argv[3], sys.argv[4])
    elif action == "tree":
        _tree(sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5]))
    elif action == "packin":
        _packin(sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5]))
"""


@dataclass
class RemoteState:
    """What the container's workspace holds, as the manifest entries it matches."""

    entries: dict[str, ManifestEntry] = field(default_factory=dict)
    bytes_read: int = 0


def _stderr_tail(result: Any) -> str:
    """The last 500 characters of a helper run's stderr (``""`` when it has none or is unreadable)."""
    try:
        return "\n".join(line.text for line in result.logs.stderr)[-500:]
    except Exception:
        return ""


async def _run(sandbox: Any, command: str) -> Any:
    from opensandbox.models.execd import RunCommandOpts

    result = await sandbox.commands.run(command, opts=RunCommandOpts(working_directory="/tmp"))  # noqa: S108
    if getattr(result, "exit_code", 0) not in (0, None):
        raise RuntimeError(f"workspace helper failed ({command.split()[2]}): {_stderr_tail(result)}")
    return result


async def _read_helper_json(sandbox: Any, path: str, *, action: str, result: Any, detail: str = "") -> Any:
    """The JSON the helper's *action* wrote at *path*, or an error that names why there is none.

    The helper writes its output file last: an empty or missing file means it ended before (killed for its
    time or memory, typically on a large tree), which a bare JSON decode reported as "Expecting value: line 1
    column 1" and nothing else.
    """
    try:
        raw = await sandbox.files.read_bytes(path)
    except Exception as exc:
        raw, missing = b"", format_exception_message(exc)
    else:
        missing = ""
    try:
        return json.loads(raw)
    except ValueError as exc:
        exit_code = getattr(result, "exit_code", None)
        stderr = _stderr_tail(result)
        state = f"missing ({missing})" if missing else f"{len(raw)} bytes and not JSON ({exc})"
        ended = (
            "no exit code (the helper was likely ended for its time or memory)"
            if exit_code is None
            else f"exit {exit_code}"
        )
        raise HelperOutputError(
            f"workspace helper failed ({action}): its output file {path} is {state}; {ended}{detail}"
            + (f"; stderr: {stderr}" if stderr else ""),
            exit_code,
        ) from None


async def install_tool(sandbox: Any) -> None:
    from opensandbox.models.filesystem import WriteEntry

    await sandbox.files.create_directories([WriteEntry(path=TOOL_DIR, mode=700)])
    await sandbox.files.write_file(TOOL_PATH, TOOL_SRC.encode("utf-8"))


async def probe_files(sandbox: Any, root: str, paths: list[str]) -> set[str]:
    """Which of *paths* (relative to the container directory *root*) are files there; the tool must be installed.

    The paths go to the helper in a JSON file; *root* is quoted on its command line.
    """
    import shlex

    await sandbox.files.write_file(f"{TOOL_DIR}/probe.json", json.dumps(list(paths)).encode("utf-8"))
    await _run(sandbox, f"python {TOOL_PATH} probe {shlex.quote(root)} {TOOL_DIR}/probe.json {TOOL_DIR}/probe-out.json")
    found = json.loads(await sandbox.files.read_bytes(f"{TOOL_DIR}/probe-out.json"))
    return {path for path in paths if found.get(path) is True}


def snapshot_json(entries: dict[str, ManifestEntry]) -> bytes:
    """*entries* as the helper's snapshot (``path -> [size, mtime, sha256]``), the baseline :func:`collect` compares to."""
    return json.dumps({e.path: [e.size, e.mtime, e.sha256] for e in entries.values()}).encode("utf-8")


async def _listing(
    sandbox: Any,
    baseline: dict[str, ManifestEntry],
    max_files: int,
    *,
    missing_ok: bool,
    ignore: list[str] | None = None,
) -> dict:
    """What the container's workspace holds that *baseline* does not; paths matching *ignore* are neither listed nor removed.

    Every call has its own snapshot, ignore and listing files (``list-<token>-*``): two commands run at once
    on one container (two checks of one checkout in one round) list side by side, never reading or renaming
    each other's listing.
    """
    files = _list_files()
    await sandbox.files.write_file(files["snapshot"], snapshot_json(baseline))
    await sandbox.files.write_file(files["ignore"], json.dumps(list(ignore or [])).encode("utf-8"))
    command = (
        f"python {TOOL_PATH} list {REMOTE_WORKSPACE} {files['snapshot']} {files['listing']} "
        f"{int(max_files)} {int(missing_ok)} {files['ignore']}"
    )
    detail = f", listing a workspace of {len(baseline)} files"
    try:
        return await _list_once(sandbox, command, files["listing"], detail)
    except HelperOutputError as exc:
        # Exit 0 with no listing is transient (seen once on a 13k-file tree, the next call passed): one
        # more try. A helper that was ended (no exit code) would be ended again: no retry.
        if exc.exit_code != 0:
            raise
        logger.warning("sandbox workspace list left no listing on exit 0, retrying once: %s", exc)
    return await _list_once(sandbox, command, files["listing"], detail + ", on its second try")


def _list_files() -> dict[str, str]:
    """The container paths of one list step's snapshot, ignore and listing files, named for this call alone."""
    token = uuid.uuid4().hex[:16]
    return {name: f"{TOOL_DIR}/list-{token}-{name}.json" for name in ("snapshot", "ignore", "listing")}


async def _list_once(sandbox: Any, command: str, listing_path: str, detail: str) -> dict:
    """One run of the helper's ``list`` action and the listing it wrote at *listing_path*."""
    with contextlib.suppress(Exception):
        # The second try of a call must not read the first try's listing.
        await sandbox.files.write_file(listing_path, b"")
    result = await _run(sandbox, command)
    return await _read_helper_json(sandbox, listing_path, action="list", result=result, detail=detail)


async def reconcile(
    sandbox: Any, session: WorkspaceSession, entries: dict[str, ManifestEntry], *, ignore: list[str] | None = None
) -> RemoteState:
    """Make the container's ``/tmp/workspace`` hold *entries* exactly; the tool must be installed.

    Files matching an *ignore* pattern (``fnmatch`` over the workspace path, a
    directory as ``dir/``, which covers every path under it) stay in the
    container untouched: what a command builds there (dependencies, caches)
    and never enters the manifest. Manifest files under an ignored directory
    (a previous run's ``.xtm/runs/<id>/``, ``.venv`` packages) are neither
    sent to the container nor reported as removed by :func:`collect`.
    """
    started = time.monotonic()
    record = await session.record(create=False) if entries else None
    listing = await _listing(sandbox, entries, session.limits.max_files, missing_ok=True, ignore=ignore)
    stale = [row[0] for row in listing.get("changed", [])]
    missing = set(listing.get("removed", []))
    wanted = [entries[path] for path in sorted(set(stale) | missing) if path in entries]
    extra = [path for path in stale if path not in entries]
    state = RemoteState(entries=dict(entries))
    blobs: dict[str, bytes] = {}
    gate = asyncio.Semaphore(BLOB_READ_CONCURRENCY)
    # The first path of each distinct content: the file a failed read is named by.
    first_path: dict[str, str] = {}
    for entry in wanted:
        first_path.setdefault(entry.sha256, entry.path)

    async def read(sha256: str) -> None:
        # One more try for a read that failed, then the failure names the file:
        # a TaskGroup's own message ("1 sub-exception") names nothing.
        async with gate:
            for attempt in range(1, BLOB_READ_ATTEMPTS + 1):
                try:
                    blobs[sha256] = await session.store.read_blob(record.id, sha256)
                    return
                except Exception as exc:
                    if attempt == BLOB_READ_ATTEMPTS:
                        raise UploadReadError(
                            f"{first_path[sha256]} could not be read from the workspace store "
                            f"({format_exception_message(exc)}, after {attempt} attempts)"
                        ) from exc

    try:
        async with asyncio.TaskGroup() as reads:
            for sha256 in first_path:
                reads.create_task(read(sha256))
    except BaseExceptionGroup as group:
        raise first_leaf(group) from group
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for entry in wanted:
            data = blobs[entry.sha256]
            info = tarfile.TarInfo(entry.path)
            info.size = len(data)
            info.mtime = entry.mtime
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(data))
            state.bytes_read += len(data)
    await _upload(sandbox, buffer.getvalue(), json.dumps(extra).encode("utf-8"))
    elapsed = time.monotonic() - started
    metrics.observe_sync("materialize", elapsed)
    logger.info("Workspace reconciled: %d files, %.1f MB sent in %.1f s", len(wanted), state.bytes_read / 1e6, elapsed)
    return state


def _stage_files() -> dict[str, str]:
    """The container paths of one upload's or read-back's archive and lists, named for this call alone.

    Two commands of one container (two checks in one round) once shared ``in.tar``: one unpacked the
    other's archive, or a half-written one.
    """
    token = uuid.uuid4().hex[:16]
    return {name: f"{TOOL_DIR}/stage-{token}-{name}" for name in ("in.tar", "remove.json", "pack.json", "out.tar")}


async def _upload(sandbox: Any, archive: bytes, removals: bytes) -> None:
    """Write *archive* to the container and unpack it there, checked whole (size and sha256) before it is applied.

    A failed write, a failed unpack or an archive that arrived short or different is sent once more
    (:data:`UPLOAD_ATTEMPTS`); then :class:`UploadError` names the archive's size and each cause.
    """
    files = _stage_files()
    digest = sha256_bytes(archive)
    causes: list[str] = []
    try:
        for attempt in range(1, UPLOAD_ATTEMPTS + 1):
            try:
                await sandbox.files.write_file(files["in.tar"], archive)
                await sandbox.files.write_file(files["remove.json"], removals)
                await _run(
                    sandbox,
                    f"python {TOOL_PATH} unpack {REMOTE_WORKSPACE} {files['in.tar']} {files['remove.json']} "
                    f"{len(archive)} {digest}",
                )
                return
            except Exception as exc:
                causes.append(f"attempt {attempt}: {format_exception_message(first_leaf(exc))}")
                logger.warning("sandbox workspace upload failed (attempt %d of %d): %s", attempt, UPLOAD_ATTEMPTS, exc)
    finally:
        with contextlib.suppress(Exception):
            await _run(sandbox, f"python {TOOL_PATH} drop {files['in.tar']} {files['remove.json']}")
    raise UploadError(
        f"the {len(archive) / 1e6:.1f} MB workspace archive could not be unpacked in the container "
        f"after {UPLOAD_ATTEMPTS} attempts ({'; '.join(causes)})"
    )


async def collect(
    sandbox: Any, session: WorkspaceSession, state: RemoteState, *, ignore: list[str] | None = None
) -> ScanResult:
    """What the code changed in the container's workspace, its bytes fetched and hashed here."""
    started = time.monotonic()
    result = ScanResult()
    try:
        listing = await _listing(sandbox, state.entries, session.limits.max_files, missing_ok=False, ignore=ignore)
    except Exception as exc:
        result.unreadable = f"workspace/ could not be read back from the sandbox ({format_exception_message(exc)})"
        return result
    if listing.get("unreadable"):
        result.unreadable = "workspace/ was removed or replaced by a link: nothing under it was saved"
        return result
    result.overflow = bool(listing.get("overflow"))
    result.skipped = [str(item)[:160] for item in listing.get("skipped", [])]
    if result.overflow:
        return result
    changed: dict[str, tuple[int, float]] = {}
    for rel_raw, size, mtime, _sha in listing.get("changed", []):
        try:
            rel = normalize(str(rel_raw))
        except WorkspacePathError:
            result.skipped.append(f"{str(rel_raw)[:120]} (a name the workspace does not keep)")
            continue
        changed[rel] = (int(size), float(mtime))
    result.removals = sorted(p for p in listing.get("removed", []) if p in state.entries)
    if changed:
        # Held to the quota on the sizes the container reports before anything
        # is fetched; the commit checks the real sizes again.
        plan_commit(
            state.entries,
            [ManifestEntry(path=p, size=s, sha256="?", mtime=m) for p, (s, m) in changed.items()],
            result.removals,
            session.limits,
        )
        files = _stage_files()
        await sandbox.files.write_file(files["pack.json"], json.dumps(sorted(changed)).encode("utf-8"))
        await _run(sandbox, f"python {TOOL_PATH} pack {REMOTE_WORKSPACE} {files['pack.json']} {files['out.tar']}")
        try:
            raw = await sandbox.files.read_bytes(files["out.tar"])
        finally:
            with contextlib.suppress(Exception):
                await _run(sandbox, f"python {TOOL_PATH} drop {files['pack.json']} {files['out.tar']}")
        with tarfile.open(fileobj=io.BytesIO(raw)) as tf:
            for member in tf.getmembers():
                if not member.isfile() or member.name not in changed:
                    continue
                handle = tf.extractfile(member)
                if handle is None:
                    continue
                data = handle.read()
                sha = sha256_bytes(data)
                held = state.entries.get(member.name)
                if held is not None and held.sha256 == sha:
                    continue
                result.upserts.append(
                    PendingFile(
                        ManifestEntry(path=member.name, size=len(data), sha256=sha, mtime=changed[member.name][1]),
                        data=data,
                    )
                )
    metrics.observe_sync("scan", time.monotonic() - started)
    return result
