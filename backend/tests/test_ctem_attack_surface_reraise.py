# SPDX-License-Identifier: Apache-2.0

"""#2634: what a purge resync raises again from the baseline, and why an empty outside path raised nothing."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.services import ctem_attack_surface as surface

NOW = datetime(2026, 11, 27, 16, 0, tzinfo=UTC)


class _Asset:
    def __init__(self, name: str) -> None:
        self.name = name
        self.external_id = ""


def _row(*signalled: str, ports: tuple[str, ...] = ()) -> surface.Baseline:
    return surface.Baseline(
        host="dev.corp.example", ports=ports, last_seen_at=NOW, signalled=signalled, ip="203.0.113.20"
    )


def test_a_new_host_mark_raises_the_drift_again_for_an_undeclared_host() -> None:
    found = surface.held_findings(_row(surface.REARM_NEW_HOST), {})
    assert [(f.trigger_path, f.detail) for f in found] == [("surface_drift", "new_host")]
    assert found[0].dedupe_key == "surface_drift:attack_surface:dev.corp.example:new_host"


def test_a_host_declared_since_the_purge_raises_no_drift() -> None:
    declared: dict[str, Any] = {"dev.corp.example": _Asset("dev.corp.example")}
    assert surface.held_findings(_row(surface.REARM_NEW_HOST), declared) == []


def test_a_port_mark_raises_its_path_again() -> None:
    key = "surface_drift:attack_surface:dev.corp.example:port:8080/tcp"
    found = surface.held_findings(_row(surface.rearm_mark(key)), {})
    assert [f.dedupe_key for f in found] == [key]
    declared: dict[str, Any] = {"dev.corp.example": _Asset("dev.corp.example")}
    exposure = "new_exposure:attack_surface:dev.corp.example:port:8080/tcp"
    assert [f.dedupe_key for f in surface.held_findings(_row(surface.rearm_mark(exposure)), declared)] == [exposure]


def test_only_marks_are_raised_again() -> None:
    held = "surface_drift:attack_surface:dev.corp.example:port:22/tcp"
    assert surface.held_findings(_row(held), {}) == []


def test_empty_reads_name_the_scopes_the_provider_answered_nothing_for() -> None:
    statuses = {"shodan|opencrq.io": "empty", "shodan|corp.example": "complete", "gti|opencrq.io": "empty"}
    assert surface.empty_reads(statuses) == ["opencrq.io"]
    assert surface.empty_reads({}) == []
