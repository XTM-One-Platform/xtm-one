# SPDX-License-Identifier: Apache-2.0

"""GitLab dependency vulnerabilities as path 3 CTEM signals (#1331, slice 3)."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import settings
from app.services import ctem_case_loop, ctem_code_findings, ctem_scope_gate_store, ctem_signals

ORG = uuid.uuid4()

FINDING = {
    "external_id": "77:9001",
    "package": "lodash",
    "version": "4.17.20",
    "purl": "pkg:npm/lodash@4.17.20",
    "manifest_path": "yarn.lock",
    "name": "Prototype pollution",
    "severity": "high",
    "url": "https://gitlab.acme.io/acme/web/-/security/vulnerabilities/9001",
    "repository": "acme/platform/web",
    "repository_id": "77",
    "identity_key": "gitlab.acme.io:repository:77",
}


def test_a_finding_is_a_signal_on_its_repository() -> None:
    fields = ctem_code_findings.signal_fields(FINDING)
    assert fields is not None
    assert (fields["external_id"], fields["ref"]) == ("77:9001", "lodash")
    evidence = fields["evidence"]
    assert evidence["assets"] == ["acme/platform/web", "77", "gitlab.acme.io:repository:77"]
    assert evidence["affected_package"] == "lodash" and evidence["repository"] == "acme/platform/web"
    assert evidence["installed_version"] == "4.17.20" and evidence["purl"] == "pkg:npm/lodash@4.17.20"
    assert "fixed_version" not in evidence  # GitLab's dependency list names none: never guessed


def test_an_unknown_version_is_not_written_and_a_keyless_finding_is_nothing() -> None:
    fields = ctem_code_findings.signal_fields({**FINDING, "version": "unknown"})
    assert fields is not None and "installed_version" not in fields["evidence"]
    assert ctem_code_findings.signal_fields({**FINDING, "external_id": ""}) is None


def test_only_the_projects_read_whole_close_their_gone_findings() -> None:
    stored = {"77:1", "77:2", "88:5"}
    assert ctem_code_findings.gone_ids(stored, {"77:1"}, {"77"}) == {"77:2"}
    assert ctem_code_findings.gone_ids(stored, set(), {"77", "88"}) == {"77:1", "77:2", "88:5"}
    assert ctem_code_findings.gone_ids(stored, set(), set()) == set()


class _Db:
    def __init__(self) -> None:
        self.sync_session = object()

    @asynccontextmanager
    async def begin_nested(self):
        yield


@pytest.fixture
def xtm_one(monkeypatch):
    monkeypatch.setattr(type(settings), "is_copilot_like", property(lambda _s: False), raising=False)


async def test_record_findings_records_through_the_intake_and_closes_the_gone(monkeypatch, xtm_one) -> None:
    recorded: list[Any] = []
    closed: list[str] = []

    async def _record(_db, **kw):
        recorded.append(kw)
        return SimpleNamespace(id=uuid.uuid4()), True

    async def _gate(_db, _signal, *, created):
        return ctem_case_loop.PendingEvents()

    async def _stored(_db, _org, _projects):
        return {"77:9001", "77:42"}

    async def _close(_db, **kw):
        closed.append(kw["external_id"])
        return 1

    monkeypatch.setattr(ctem_signals, "record_signal", _record)
    monkeypatch.setattr(ctem_scope_gate_store, "evaluate_after_record", _gate)
    monkeypatch.setattr(ctem_code_findings, "_stored_ids", _stored)
    monkeypatch.setattr(ctem_case_loop, "close_signal_members", _close)
    monkeypatch.setattr(ctem_code_findings, "_publish_after_commit", lambda _db, _p: None)

    counts = await ctem_code_findings.record_findings(_Db(), {"77": [FINDING]}, organization_id=ORG)
    assert counts == {"recorded": 1, "closed": 1}
    signal = recorded[0]["signal"]
    assert (signal.trigger_path, signal.lens, signal.subject_kind) == ("code_flaw", "inside", "code")
    assert signal.source_provider == "gitlab" and signal.source_external_id == "77:9001"
    assert recorded[0]["recorded_via"] == "listener" and recorded[0]["recorded_by"] == "system"
    assert closed == ["77:42"]


async def test_record_findings_is_off_in_copilot_and_never_raises(monkeypatch) -> None:
    monkeypatch.setattr(type(settings), "is_copilot_like", property(lambda _s: True), raising=False)
    assert await ctem_code_findings.record_findings(_Db(), {"77": [FINDING]}, organization_id=ORG) == {
        "recorded": 0,
        "closed": 0,
    }
    monkeypatch.setattr(type(settings), "is_copilot_like", property(lambda _s: False), raising=False)

    async def _boom(_db, **_kw):
        raise RuntimeError("database down")

    monkeypatch.setattr(ctem_signals, "record_signal", _boom)
    counts = await ctem_code_findings.record_findings(_Db(), {"77": [FINDING]}, organization_id=ORG)
    assert counts == {"recorded": 0, "closed": 0}
