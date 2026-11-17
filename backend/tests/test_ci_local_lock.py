"""The queue of ``scripts/ci-local.sh`` runs.

``scripts/ci-local-lock.sh`` (sourced by ci-local.sh) and its holder
``scripts/ci-local-lock.py`` let one local CI run go at a time per clone.
These tests source the real file from a stand-in run that logs when its
"checks" start and end, and pin:

* concurrent runs go one after another, and the queued one says it waits;
* a caller that reads the run's output to EOF before reaping it (Python
  ``capture_output``) is not deadlocked, and the lock is free afterwards;
* a ``kill -9`` of a run that leads its own process group keeps the lock until
  the checks it left running are done;
* a waiting run that is killed leaves no holder behind;
* without flock(2) or without python3 the run goes on, unqueued, and says so;
* ``CI_LOCAL_NO_LOCK=1`` skips the queue, and a lock that cannot be taken
  stops the run before any check;
* outside a git checkout the lock falls back to a per-user file in the
  temporary directory, created private, and a symlink at the lock path is
  refused rather than followed;
* the waiting notice names the holder by its record alone: a shorter record
  written over a longer one shows no tail of it, neither once the old text is
  cut nor between the write and the cut.

POSIX only: the queue relies on flock(2), process groups and ``ps``.
"""

from __future__ import annotations

import errno
import fcntl
import importlib.util
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BASH = shutil.which("bash")
PGREP = shutil.which("pgrep")

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or BASH is None or PGREP is None or shutil.which("ps") is None,
    reason="the local CI queue runs on macOS / Linux with bash and ps (pgrep finds its holders)",
)

WAITING_NOTICE = "==> Another local CI run is in progress"

# A stand-in for ci-local.sh: the same lock, then "checks" that log their
# start and end. The fifth argument, when set, is run as a foreground subshell
# that outlives a kill -9 of this shell.
RUN_SH = """\
set -euo pipefail
ROOT="$1"; NAME="$2"; LOG="$3"; HOLD="$4"; ORPHAN="${5:-}"
source "$ROOT/scripts/ci-local-lock.sh"
acquire_run_lock
echo "start $NAME" >> "$LOG"
if [[ -n "$ORPHAN" ]]; then
  ( echo "orphan start $NAME" >> "$LOG"; sleep "$ORPHAN"; echo "orphan end $NAME" >> "$LOG" )
fi
sleep "$HOLD"
echo "end $NAME" >> "$LOG"
"""


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.lock = tmp_path / "ci-local.lock"
        self.log = tmp_path / "run.log"
        self.log.touch()
        self.script = tmp_path / "run.sh"
        self.script.write_text(RUN_SH)
        # python3 on PATH is the interpreter running the tests.
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        (self.bin / "python3").symlink_to(sys.executable)
        self.env = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "CI_LOCAL_LOCK_FILE": str(self.lock),
        }
        self.env.pop("CI_LOCAL_NO_LOCK", None)

    def args(self, name: str, hold: float, orphan: float | None = None) -> list[str]:
        argv = [BASH, str(self.script), str(REPO_ROOT), name, str(self.log), str(hold)]
        if orphan is not None:
            argv.append(str(orphan))
        return argv

    def start(self, name: str, hold: float, orphan: float | None = None, **kwargs) -> subprocess.Popen:
        return subprocess.Popen(  # noqa: S603
            self.args(name, hold, orphan),
            env=kwargs.pop("env", self.env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **kwargs,
        )

    def lines(self) -> list[str]:
        return self.log.read_text().splitlines()

    def holder_of(self, run: subprocess.Popen) -> int | None:
        """The PID of the run's lock holder (its child on this lock file)."""
        out = subprocess.run(  # noqa: S603
            [PGREP, "-P", str(run.pid), "-f", str(self.lock)], capture_output=True, text=True, check=False
        ).stdout.split()
        return int(out[0]) if len(out) == 1 else None


def wait_for(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        time.sleep(0.05)


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def lock_is_free(path: Path) -> bool:
    with open(path, "a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(handle, fcntl.LOCK_UN)
        return True


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def test_concurrent_runs_go_one_after_another(harness: Harness) -> None:
    first = harness.start("A", hold=2)
    wait_for(lambda: harness.lines() == ["start A"])
    second = harness.start("B", hold=0)

    second_out, second_err = second.communicate(timeout=20)
    first_out, _ = first.communicate(timeout=20)

    assert first.returncode == 0
    assert second.returncode == 0
    assert harness.lines() == ["start A", "end A", "start B", "end B"]
    assert second_err.count(WAITING_NOTICE) == 1
    assert f"(pid {first.pid}, {REPO_ROOT})" in second_err
    assert re.fullmatch(r"==> Local CI lock acquired after \d+s\n", second_out)
    assert first_out == ""


def test_caller_reading_output_to_eof_is_not_deadlocked(harness: Harness) -> None:
    # capture_output reads both pipes to EOF before it reaps the shell: a
    # holder that kept the run's stderr, or waited for the PID to vanish,
    # would never let it return.
    done = subprocess.run(harness.args("A", 0), env=harness.env, capture_output=True, text=True, timeout=15)  # noqa: S603

    assert done.returncode == 0
    assert harness.lines() == ["start A", "end A"]
    wait_for(lambda: lock_is_free(harness.lock), timeout=3)


def test_kill_9_keeps_the_lock_until_the_checks_left_running_end(harness: Harness) -> None:
    # A terminal job leads its own process group; so does a run started in a
    # new session.
    # No pipes on this run: the orphan would inherit them, and reading them to
    # EOF would wait for the orphan here instead of in the lock.
    first = subprocess.Popen(  # noqa: S603
        harness.args("A", hold=30, orphan=2),
        env=harness.env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    # Killed once the orphan exists: a kill before its fork would leave none.
    wait_for(lambda: harness.lines() == ["start A", "orphan start A"])
    os.kill(first.pid, signal.SIGKILL)
    first.wait(timeout=10)

    second = harness.start("B", hold=0)
    second.communicate(timeout=20)

    assert second.returncode == 0
    assert harness.lines() == ["start A", "orphan start A", "orphan end A", "start B", "end B"]


def test_killed_waiting_run_leaves_no_holder_behind(harness: Harness) -> None:
    first = harness.start("A", hold=8)
    wait_for(lambda: harness.lines() == ["start A"])
    wait_for(lambda: harness.holder_of(first) is not None)
    first_holder = harness.holder_of(first)
    second = harness.start("B", hold=0)
    wait_for(lambda: harness.holder_of(second) is not None)
    second_holder = harness.holder_of(second)

    os.kill(second.pid, signal.SIGKILL)
    second.communicate(timeout=10)
    # Its shell is gone: the queued holder stops waiting instead of taking the
    # lock for nobody once the first run ends.
    wait_for(lambda: not process_alive(second_holder), timeout=5)
    first.communicate(timeout=20)

    assert harness.lines() == ["start A", "end A"]
    wait_for(lambda: not process_alive(first_holder), timeout=5)


def test_without_flock_the_run_goes_on_unqueued(harness: Harness) -> None:
    shadow = harness.tmp / "no-fcntl"
    shadow.mkdir()
    (shadow / "fcntl.py").write_text('raise ImportError("no fcntl here")\n')
    env = {**harness.env, "PYTHONPATH": str(shadow)}

    done = subprocess.run(harness.args("A", 0), env=env, capture_output=True, text=True, timeout=15)  # noqa: S603

    assert done.returncode == 0
    assert done.stderr.splitlines() == [
        "==> WARNING: no flock(2) on this platform: running without the local CI queue, other runs may overlap this one"
    ]
    assert harness.lines() == ["start A", "end A"]


def test_without_python3_the_run_goes_on_unqueued(harness: Harness) -> None:
    bare = harness.tmp / "bare-bin"
    bare.mkdir()
    for tool in ("sleep", "dirname"):
        (bare / tool).symlink_to(shutil.which(tool))
    env = {**harness.env, "PATH": str(bare)}

    done = subprocess.run(harness.args("A", 0), env=env, capture_output=True, text=True, timeout=15)  # noqa: S603

    assert done.returncode == 0
    assert done.stderr.splitlines() == [
        "==> WARNING: python3 not found: running without the local CI queue, other runs may overlap this one"
    ]
    assert harness.lines() == ["start A", "end A"]


def test_no_lock_skips_the_queue(harness: Harness) -> None:
    first = harness.start("A", hold=2)
    wait_for(lambda: harness.lines() == ["start A"])

    done = subprocess.run(  # noqa: S603
        harness.args("B", 0),
        env={**harness.env, "CI_LOCAL_NO_LOCK": "1"},
        capture_output=True,
        text=True,
        timeout=15,
    )
    first.communicate(timeout=20)

    assert done.returncode == 0
    assert done.stdout.splitlines() == ["==> CI_LOCAL_NO_LOCK=1: not waiting for other local CI runs"]
    assert harness.lines() == ["start A", "start B", "end B", "end A"]


def test_lock_that_cannot_be_taken_stops_the_run_before_any_check(harness: Harness) -> None:
    unwritable = harness.tmp / "missing-dir" / "ci-local.lock"
    env = {**harness.env, "CI_LOCAL_LOCK_FILE": str(unwritable)}

    done = subprocess.run(harness.args("A", 0), env=env, capture_output=True, text=True, timeout=15)  # noqa: S603

    assert done.returncode == 1
    assert done.stderr.splitlines() == [
        f"Could not take the local CI lock {unwritable}: [Errno 2] No such file or directory: '{unwritable}'",
        "Set CI_LOCAL_LOCK_FILE to a writable path, or CI_LOCAL_NO_LOCK=1 to skip the wait.",
    ]
    assert harness.lines() == []


def test_symlink_at_the_lock_path_is_refused(harness: Harness) -> None:
    target = harness.tmp / "someone-elses-file"
    target.write_text("keep\n")
    link = harness.tmp / "planted.lock"
    link.symlink_to(target)
    env = {**harness.env, "CI_LOCAL_LOCK_FILE": str(link)}

    done = subprocess.run(harness.args("A", 0), env=env, capture_output=True, text=True, timeout=15)  # noqa: S603

    assert done.returncode == 1
    assert done.stderr.splitlines() == [
        f"Could not take the local CI lock {link}: [Errno {errno.ELOOP}] {os.strerror(errno.ELOOP)}: '{link}'",
        "Set CI_LOCAL_LOCK_FILE to a writable path, or CI_LOCAL_NO_LOCK=1 to skip the wait.",
    ]
    assert target.read_text() == "keep\n"
    assert harness.lines() == []


def test_outside_a_git_checkout_the_lock_is_a_private_per_user_file(harness: Harness) -> None:
    checkout = harness.tmp / "tarball-checkout"
    checkout.mkdir()
    temp_dir = harness.tmp / "tmpdir"
    temp_dir.mkdir()
    run = harness.tmp / "plain-run.sh"
    run.write_text(
        'set -euo pipefail\nROOT="$2"\nsource "$1/scripts/ci-local-lock.sh"\nacquire_run_lock\necho "start" >> "$3"\n'
    )
    env = {**harness.env, "TMPDIR": str(temp_dir), "GIT_CEILING_DIRECTORIES": str(harness.tmp)}
    env.pop("CI_LOCAL_LOCK_FILE")

    proc = subprocess.Popen(  # noqa: S603
        [BASH, str(run), str(REPO_ROOT), str(checkout), str(harness.log)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    out, err = proc.communicate(timeout=15)

    assert (proc.returncode, out, err) == (0, "", "")
    assert harness.lines() == ["start"]
    lock = temp_dir / f"xtm-one-ci-local-{os.getuid()}.lock"
    assert lock.read_text() == f"pid {proc.pid}, {checkout}\n"
    assert lock.stat().st_mode & 0o777 == 0o600


def load_holder():
    """The holder script as a module (its file name is not importable)."""
    spec = importlib.util.spec_from_file_location("ci_local_lock_holder", REPO_ROOT / "scripts" / "ci-local-lock.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


OLD_RECORD = b"pid 4242, /Users/someone/a/much/longer/checkout/path/of/the/previous/run\n"


def test_shorter_record_shows_no_tail_between_the_write_and_the_cut(tmp_path: Path, monkeypatch) -> None:
    holder = load_holder()
    path = tmp_path / "ci-local.lock"
    path.write_bytes(OLD_RECORD)
    record = b"pid 7, /a\n"
    seen: list[tuple[bytes, str]] = []
    real_ftruncate = os.ftruncate

    def ftruncate_spy(fd: int, length: int) -> None:
        # What a waiter reads in the window between pwrite and ftruncate.
        seen.append((path.read_bytes(), holder.read_holder(fd)))
        real_ftruncate(fd, length)

    monkeypatch.setattr(holder.os, "ftruncate", ftruncate_spy)
    fd = os.open(path, os.O_RDWR)
    try:
        holder.write_record(fd, record)
    finally:
        os.close(fd)

    padded = record + b" " * (len(OLD_RECORD) - len(record))
    assert seen == [(padded, "pid 7, /a")]
    # A waiter running an older holder script strips the whole read instead.
    assert padded.decode().strip() == "pid 7, /a"
    assert path.read_bytes() == record


def test_longer_record_is_written_unpadded(tmp_path: Path) -> None:
    holder = load_holder()
    path = tmp_path / "ci-local.lock"
    path.write_bytes(b"pid 7, /a\n")
    fd = os.open(path, os.O_RDWR)
    try:
        holder.write_record(fd, OLD_RECORD)
    finally:
        os.close(fd)

    assert path.read_bytes() == OLD_RECORD


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        # An older holder script writes without padding: the tail of the
        # longer record it overwrote follows the first newline until it cuts.
        (b"pid 7, /a\n" + OLD_RECORD[len(b"pid 7, /a\n") :], "pid 7, /a"),
        (b"pid 7, /a\n        ", "pid 7, /a"),
        (b"pid 7, /a\n", "pid 7, /a"),
        (b"", "unknown run"),
        (b"   \n", "unknown run"),
        (b"\npid 7, /a\n", "unknown run"),
    ],
)
def test_waiter_names_the_holder_by_the_first_line_only(tmp_path: Path, content: bytes, expected: str) -> None:
    holder = load_holder()
    path = tmp_path / "ci-local.lock"
    path.write_bytes(content)
    fd = os.open(path, os.O_RDONLY)
    try:
        assert holder.read_holder(fd) == expected
    finally:
        os.close(fd)


def test_notice_names_the_holder_alone_after_a_longer_record(harness: Harness) -> None:
    harness.lock.write_bytes(OLD_RECORD + b"pid 4243, /a/second/stale/line\n")
    first = harness.start("A", hold=2)
    wait_for(lambda: harness.lines() == ["start A"])
    assert harness.lock.read_bytes() == f"pid {first.pid}, {REPO_ROOT}\n".encode()
    second = harness.start("B", hold=0)

    _, second_err = second.communicate(timeout=20)
    first.communicate(timeout=20)

    assert second.returncode == 0
    assert second_err.splitlines() == [f"{WAITING_NOTICE} (pid {first.pid}, {REPO_ROOT}); waiting for it to finish..."]
