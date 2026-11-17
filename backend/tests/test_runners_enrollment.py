"""Re-enrollment convergence invariants (pure, no DB).

A machine re-enrolling (reinstall, credential loss, install-script re-run)
must fold into its existing ``Runner`` row instead of duplicating the fleet
list. ``apply_reenrollment`` is the single mutation applied to the matched
row; these tests pin its contract:

* the credential is ROTATED (one machine, one live credential),
* everything the machine *reports* is refreshed (hostname, facts, version,
  liveness),
* everything the *operator* configured is preserved — the enrollment key's
  defaults must never silently re-widen a runner an admin tightened,
* absent report fields never wipe previously known values.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.api.v1.runners import EnrollRequest
from app.services.runners import apply_reenrollment

_NOW = datetime(2026, 8, 16, 21, 0, 0, tzinfo=UTC)


def _enrolled_runner(**overrides):
    """A Runner-shaped object as it looks after an operator tuned it."""
    base = {
        "user_id": "user-1",
        "name": "Audit workstation",  # operator renamed it
        "platform": "windows",
        "hostname": "DESKTOP-OLD",
        "machine_id": "a" * 64,
        "key_prefix": "run-oldpref",
        "key_hash": "$2b$old-hash",
        "permission_mode": "restricted",
        "capabilities": ["screenshot"],
        "allowed_shell_commands": ["git status"],
        "allowed_paths": ["C:/audit"],
        "require_approval": True,
        "labels": ["audit", "eu-west"],
        "tags": ["finance"],
        "host_facts": {"os": "windows", "os_version": "10"},
        "agent_version": "1.260810.1",
        "status": "offline",
        "last_seen_at": None,
        "is_active": True,
        "is_company_managed": False,
        "shared_group_ids": ["g1"],
        "enrolled_at": datetime(2026, 8, 10, tzinfo=UTC),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _reenroll(runner, **overrides):
    kwargs = {
        "key_prefix": "run-newpref",
        "key_hash": "$2b$new-hash",
        "hostname": "DESKTOP-NEW",
        "host_facts": {"os": "windows", "os_version": "11"},
        "agent_version": "1.260816.9",
        "now": _NOW,
    }
    kwargs.update(overrides)
    apply_reenrollment(runner, **kwargs)


def test_reenrollment_rotates_the_credential():
    runner = _enrolled_runner()
    _reenroll(runner)
    assert runner.key_prefix == "run-newpref"
    assert runner.key_hash == "$2b$new-hash"


def test_reenrollment_refreshes_reported_state():
    runner = _enrolled_runner()
    _reenroll(runner)
    assert runner.hostname == "DESKTOP-NEW"
    assert runner.host_facts == {"os": "windows", "os_version": "11"}
    assert runner.agent_version == "1.260816.9"
    assert runner.status == "online"
    assert runner.last_seen_at == _NOW
    assert runner.enrolled_at == _NOW


def test_reenrollment_preserves_operator_config():
    # The enrollment key's defaults are NOT re-applied: a reusable fleet key
    # must never silently re-widen a runner an admin tightened.
    runner = _enrolled_runner()
    _reenroll(runner)
    assert runner.name == "Audit workstation"
    assert runner.permission_mode == "restricted"
    assert runner.capabilities == ["screenshot"]
    assert runner.allowed_shell_commands == ["git status"]
    assert runner.allowed_paths == ["C:/audit"]
    assert runner.require_approval is True
    assert runner.labels == ["audit", "eu-west"]
    assert runner.tags == ["finance"]
    assert runner.shared_group_ids == ["g1"]
    assert runner.is_company_managed is False
    assert runner.user_id == "user-1"
    assert runner.machine_id == "a" * 64


def test_reenrollment_absent_report_fields_keep_known_values():
    # A client that omits hostname / facts / version must not wipe what the
    # platform already knows about the machine.
    runner = _enrolled_runner()
    _reenroll(runner, hostname=None, host_facts=None, agent_version=None)
    assert runner.hostname == "DESKTOP-OLD"
    assert runner.host_facts == {"os": "windows", "os_version": "10"}
    assert runner.agent_version == "1.260810.1"
    # Liveness + credential still refresh.
    assert runner.status == "online"
    assert runner.key_prefix == "run-newpref"


def test_enroll_request_accepts_machine_id():
    body = EnrollRequest(
        enrollment_key="renr-x", platform="windows", machine_id="b" * 64
    )
    assert body.machine_id == "b" * 64


def test_enroll_request_defaults_machine_id_to_none():
    # Builds that predate the field enroll fine (no convergence).
    body = EnrollRequest(enrollment_key="renr-x", platform="linux")
    assert body.machine_id is None


def test_enroll_request_rejects_oversized_machine_id():
    # The field is a fixed-size salted hash; anything huge is a bad client.
    with pytest.raises(ValidationError):
        EnrollRequest(
            enrollment_key="renr-x", platform="linux", machine_id="c" * 300
        )
