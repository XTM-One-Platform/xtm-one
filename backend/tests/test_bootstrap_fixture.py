"""The bootstrap fixture's own failure path (``tests/test_bootstrap.py``).

A backend that never becomes ready must fail with the phase it stopped in and
the stacks of its threads, never with a bare timeout: a red that names no
cause cannot be fixed at the root. These run in the unit job, with a fake
process: the bootstrap job starts a real server only on the healthy path.
"""

from __future__ import annotations

import signal
import subprocess

from tests.test_bootstrap import (
    BASE_URL,
    FAULTHANDLER_MARKER,
    STACK_DUMP_GRACE,
    STARTUP_TIMEOUT,
    _startup_timeout_message,
    _stop_with_stacks,
)


class _FakeProcess:
    """The part of ``subprocess.Popen`` the stop uses, recording the calls."""

    def __init__(self, *, exited: bool = False, survives_abort: bool = False) -> None:
        self.calls: list[tuple[str, object]] = []
        self._exited = exited
        self._survives_abort = survives_abort

    def poll(self) -> int | None:
        return 0 if self._exited else None

    def send_signal(self, sig: int) -> None:
        self.calls.append(("signal", sig))

    def kill(self) -> None:
        self.calls.append(("kill", None))
        self._survives_abort = False

    def wait(self, timeout: float | None = None) -> int:
        self.calls.append(("wait", timeout))
        if self._survives_abort:
            raise subprocess.TimeoutExpired("uvicorn", timeout or 0)
        return -6


def test_a_stalled_server_is_aborted_so_faulthandler_writes_its_stacks():
    proc = _FakeProcess()
    _stop_with_stacks(proc)  # type: ignore[arg-type]
    assert proc.calls == [("signal", signal.SIGABRT), ("wait", STACK_DUMP_GRACE)]


def test_a_server_that_ignores_the_abort_is_killed():
    proc = _FakeProcess(survives_abort=True)
    _stop_with_stacks(proc)  # type: ignore[arg-type]
    assert proc.calls == [("signal", signal.SIGABRT), ("wait", STACK_DUMP_GRACE), ("kill", None), ("wait", 5)]


def test_a_process_already_gone_is_left_alone():
    proc = _FakeProcess(exited=True)
    _stop_with_stacks(proc)  # type: ignore[arg-type]
    assert proc.calls == []


def test_a_server_that_never_listened_is_named_as_such():
    message = _startup_timeout_message(False, None, "", "BASE_URL is not set\nThread 0x1 (most recent call first):")
    assert message.startswith(f"Backend did not become healthy within {STARTUP_TIMEOUT}s: ")
    assert f"never accepted a connection on {BASE_URL}" in message
    assert "an import or the lifespan start" in message
    assert message.endswith("Thread 0x1 (most recent call first):")


def test_a_server_that_listened_names_its_last_readiness_answer():
    message = _startup_timeout_message(True, 503, '{"status": "starting", "step": "migrations"}', "out")
    assert 'last answered 503: {"status": "starting", "step": "migrations"}' in message
    assert "never accepted" not in message


def test_the_output_kept_is_its_end_where_the_stacks_are():
    message = _startup_timeout_message(False, None, "", "x" * 10_000 + "STACKS")
    assert message.endswith("STACKS")
    assert message.count("x") <= 6000


def test_a_long_stack_dump_keeps_the_server_log_before_it():
    """The stacks after SIGABRT never push out the log naming the startup step (#806)."""
    output = "boot\n" + "y" * 10_000 + "\nSTEP running migrations\n" + FAULTHANDLER_MARKER + "\n" + "z" * 10_000
    message = _startup_timeout_message(True, 503, '{"status": "starting"}', output)
    assert "STEP running migrations" in message
    assert FAULTHANDLER_MARKER in message
    assert message.index("STEP running migrations") < message.index(FAULTHANDLER_MARKER)
    assert message.count("y") <= 4000
    assert message.count("z") <= 4000
