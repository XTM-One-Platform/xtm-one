"""Hold the lock that queues scripts/ci-local.sh runs (see ci-local-lock.sh).

Usage: ci-local-lock.py <lock-file> <run-pid> <run-checkout>

Started by the run as its direct child. Takes an exclusive flock(2) on
<lock-file>, waiting while another run holds it, and keeps it until the run is
over; the kernel drops the lock when this process exits, so nothing is left
behind however the run ends. The first line on stdout is the verdict the run
reads:

* ``acquired`` (``acquired after waiting`` when another run held it): the
  lock is held for the run;
* ``unlocked <reason>``: this platform cannot lock, the run goes on without
  the queue;
* ``error <reason>``: the lock could not be taken.

"The run is over" means both:

* the run's shell exited. A direct child is re-parented the moment its parent
  exits, reaped or not, so ``getppid()`` is the signal: ``kill(pid, 0)`` stays
  true for an exited shell nobody reaped yet (a harness reading its output to
  EOF before ``wait()``) and for a recycled PID;
* when the run leads its own process group (a terminal job, or a harness that
  starts it in a new session), no live process is left in that group: a
  ``kill -9`` of the shell does not stop the pytest or yarn it was running,
  and the next run must not share the database with them.
"""

from __future__ import annotations

import os
import signal
import sys
import time

WAIT_POLL_SECONDS = 1.0
RUN_POLL_SECONDS = 0.2
GROUP_POLL_SECONDS = 1.0
# How much of the lock file a waiter reads to name the run holding it.
HOLDER_READ_BYTES = 4096


def read_holder(lock: int) -> str:
    """The run holding the lock, as its record names it.

    Only the first line: the record ends in a newline, and whatever follows it
    is what a shorter record left of a longer one while its holder was between
    writing and truncating (a holder from an older checkout of the clone does
    not pad, see ``write_record``).
    """
    text = os.pread(lock, HOLDER_READ_BYTES, 0).decode(errors="replace")
    return text.partition("\n")[0].strip() or "unknown run"


def write_record(lock: int, record: bytes) -> None:
    """Replace the lock file's text with ``record`` (one line).

    Written before the old text is cut, so a waiter reading meanwhile never
    finds the file empty; padded with spaces over the old text a waiter reads,
    so that in between it never finds the tail of a longer previous record.
    """
    padding = min(os.fstat(lock).st_size, HOLDER_READ_BYTES) - len(record)
    os.pwrite(lock, record + b" " * max(padding, 0), 0)
    os.ftruncate(lock, len(record))


def main() -> int:
    path, parent, root = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    try:
        import fcntl
    except ImportError:
        print("unlocked no flock(2) on this platform", flush=True)
        return 0
    # subprocess imports fcntl itself on POSIX: import it once fcntl is known
    # to exist, or a platform without it would fail before giving its verdict.
    import subprocess

    # Bash runs the single command of `<(exec ...)` as its own child; if some
    # shell ever adds a process in between, fall back to the PID probe.
    direct = os.getppid() == parent
    run_pgid = None
    try:
        if os.getpgid(parent) == parent:
            run_pgid = parent
    except OSError:
        pass
    if run_pgid is not None:
        # Leave the run's group so this process does not count as one of its
        # members; it then sits in a background group of the terminal, where
        # writing the waiting notice must not stop it.
        signal.signal(signal.SIGTTOU, signal.SIG_IGN)
        os.setpgid(0, 0)

    def shell_alive() -> bool:
        if direct:
            return os.getppid() == parent
        try:
            os.kill(parent, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass
        return True

    def group_busy() -> bool:
        if run_pgid is None:
            return False
        try:
            listing = subprocess.run(
                ["ps", "-A", "-o", "pid=", "-o", "pgid=", "-o", "stat="],
                capture_output=True,
                text=True,
                check=False,
            ).stdout
        except OSError:
            return False
        for line in listing.splitlines():
            fields = line.split()
            # Exited members nobody reaped yet (stat Z) are not running checks.
            if len(fields) >= 3 and fields[1] == str(run_pgid) and not fields[2].startswith("Z"):
                return True
        return False

    try:
        # Never follow a symlink planted at the path (the fallback lives in a
        # shared temporary directory): the holder writes to the file it opens.
        lock = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)  # held until exit
    except OSError as exc:
        print(f"error {exc}", flush=True)
        return 1

    waiting = False
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            pass
        if not shell_alive():
            return 0
        if not waiting:
            waiting = True
            holder = read_holder(lock)
            print(
                f"==> Another local CI run is in progress ({holder}); waiting for it to finish...",
                file=sys.stderr,
                flush=True,
            )
        time.sleep(WAIT_POLL_SECONDS)

    write_record(lock, f"pid {parent}, {root}\n".encode())
    try:
        print("acquired after waiting" if waiting else "acquired", flush=True)
    except BrokenPipeError:
        return 0
    # Hold none of the run's streams from here on: a caller that reads them to
    # EOF must see EOF as soon as the run's own processes are done.
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)

    while shell_alive():
        time.sleep(RUN_POLL_SECONDS)
    while group_busy():
        time.sleep(GROUP_POLL_SECONDS)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # the run reads one line: give it the cause, not a traceback
        print(f"error {type(exc).__name__}: {exc}", flush=True)
        sys.exit(1)
