"""Tests for the reconciler liveness heartbeat + healthcheck CLI.

The orchestration reconciler runs inside the SAQ worker (no HTTP server), so a
dead/hung reconciler is invisible to Kubernetes. The reconciler touches a
per-pod heartbeat file every cycle and the worker livenessProbe runs
``python -m app.healthcheck reconciler``; a stale/missing file exits non-zero so
K8s restarts the wedged worker.
"""

from __future__ import annotations

import os
import time

from app.core.config import settings
from app.healthcheck import check_reconciler, main
from app.services import orchestration_reconciler as reconciler


def test_healthcheck_missing_heartbeat_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "reconciler_heartbeat_path", str(tmp_path / "absent"))
    assert check_reconciler() == 1


def test_healthcheck_fresh_heartbeat_ok(monkeypatch, tmp_path):
    hb = tmp_path / "hb"
    hb.write_text("1")
    monkeypatch.setattr(settings, "reconciler_heartbeat_path", str(hb))
    monkeypatch.setattr(settings, "reconciler_heartbeat_max_age_seconds", 120)
    assert check_reconciler() == 0


def test_healthcheck_stale_heartbeat_fails(monkeypatch, tmp_path):
    hb = tmp_path / "hb"
    hb.write_text("1")
    stale = time.time() - 9999
    os.utime(hb, (stale, stale))
    monkeypatch.setattr(settings, "reconciler_heartbeat_path", str(hb))
    monkeypatch.setattr(settings, "reconciler_heartbeat_max_age_seconds", 120)
    assert check_reconciler() == 1


def test_healthcheck_disabled_is_healthy(monkeypatch):
    monkeypatch.setattr(settings, "reconciler_heartbeat_path", "")
    assert check_reconciler() == 0


def test_main_unknown_check_returns_2():
    assert main(["definitely-not-a-check"]) == 2


def test_write_heartbeat_creates_file(monkeypatch, tmp_path):
    hb = tmp_path / "hb"
    monkeypatch.setattr(settings, "reconciler_heartbeat_path", str(hb))
    reconciler._write_heartbeat()
    assert hb.exists()
    # Fresh write must read as healthy.
    monkeypatch.setattr(settings, "reconciler_heartbeat_max_age_seconds", 120)
    assert check_reconciler() == 0


def test_write_heartbeat_disabled_is_noop(monkeypatch):
    monkeypatch.setattr(settings, "reconciler_heartbeat_path", "")
    # Must not raise when disabled.
    reconciler._write_heartbeat()
