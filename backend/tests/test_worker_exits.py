"""A web worker killed without a word is reported by its replacement.

uvicorn's supervisor SIGKILLs a worker that misses its health-check ping, and
the kernel's out-of-memory killer can kill one worker while the container
survives: neither leaves a WARNING, and Kubernetes sees no restart. Every
worker of a launch records itself; the next one to start reports a sibling
that is gone without its clean exit, saying whether the container's oom_kill
count rose.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile

import pytest

from app.core import launch_marker, worker_exits
from app.core.worker_exits import register_worker_and_report_lost, unregister_worker

LAUNCH = "0123456789abcdef0123456789abcdef"
OWN_START = 4242


@pytest.fixture
def host(monkeypatch, tmp_path):
    """A container: its temp directory, its processes' start times, its oom_kill count."""
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setenv(launch_marker.LAUNCH_ID_ENV, LAUNCH)
    starts = {os.getpid(): OWN_START}
    oom = {"count": 0}
    monkeypatch.setattr(worker_exits, "_process_start", lambda pid: starts.get(pid))
    monkeypatch.setattr(worker_exits, "_oom_kills", lambda: oom["count"])

    class _Host:
        root = tmp_path / "xtm-one-workers"
        launch_dir = tmp_path / "xtm-one-workers" / LAUNCH
        processes = starts
        oom_kills = oom

        def sibling(self, pid: int, *, start: int, oom_kills: int | None = 0) -> None:
            self.launch_dir.mkdir(parents=True, exist_ok=True)
            (self.launch_dir / str(pid)).write_text(json.dumps({"start": start, "oom_kills": oom_kills}))

    return _Host()


def test_a_worker_records_itself_and_forgets_itself_on_a_clean_exit(host):
    assert register_worker_and_report_lost() == []

    record = json.loads((host.launch_dir / str(os.getpid())).read_text())
    assert record == {"start": OWN_START, "oom_kills": 0}

    unregister_worker()
    assert not (host.launch_dir / str(os.getpid())).exists()


def test_a_sibling_still_running_is_left_alone(host):
    host.sibling(101, start=77)
    host.processes[101] = 77

    assert register_worker_and_report_lost() == []
    assert (host.launch_dir / "101").exists()


def test_a_sibling_killed_by_the_supervisor_is_reported(host, caplog: pytest.LogCaptureFixture):
    host.sibling(101, start=77)

    with caplog.at_level(logging.WARNING, logger="app.core.worker_exits"):
        assert register_worker_and_report_lost() == [101]

    message = caplog.records[0].getMessage()
    assert "web worker pid 101 of this launch stopped without shutting down" in message
    assert "missed its health-check ping" in message
    assert "the chat turns it ran ended without a reply" in message
    assert not (host.launch_dir / "101").exists()


def test_a_pid_reused_by_another_process_is_still_a_lost_worker(host):
    host.sibling(101, start=77)
    host.processes[101] = 9000

    assert register_worker_and_report_lost() == [101]


def test_a_sibling_the_oom_killer_took_says_so(host, caplog: pytest.LogCaptureFixture):
    host.sibling(101, start=77, oom_kills=1)
    host.oom_kills["count"] = 3

    with caplog.at_level(logging.WARNING, logger="app.core.worker_exits"):
        register_worker_and_report_lost()

    assert "out-of-memory killer killed 2 process(es)" in caplog.text
    assert "oom_kill 1 -> 3" in caplog.text


def test_an_earlier_launch_is_removed_unread(host, caplog: pytest.LogCaptureFixture):
    earlier = host.root / ("f" * 32)
    earlier.mkdir(parents=True)
    (earlier / "55").write_text(json.dumps({"start": 1, "oom_kills": 0}))

    with caplog.at_level(logging.WARNING, logger="app.core.worker_exits"):
        assert register_worker_and_report_lost() == []

    assert not earlier.exists()
    assert caplog.records == []


def test_a_process_the_launcher_did_not_start_records_nothing(host, monkeypatch):
    monkeypatch.delenv(launch_marker.LAUNCH_ID_ENV)

    assert register_worker_and_report_lost() == []
    unregister_worker()
    assert not host.root.exists()


def test_without_proc_nothing_is_recorded(host, monkeypatch):
    monkeypatch.setattr(worker_exits, "_process_start", lambda _pid: None)

    assert register_worker_and_report_lost() == []
    assert not host.root.exists()
