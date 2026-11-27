"""The legacy Threat response objects XTM One still holds, per OpenCTI integration (#2627).

The "Remove legacy OpenCTI objects" action shows only while this count is above 0; it is read from
XTM One's database, never from OpenCTI.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import opencti_trigger_provisioning as prov

NOW = datetime(2026, 11, 27, 16, 0, tzinfo=UTC)


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _Rows:
        return self

    def all(self) -> list[Any]:
        return self._rows


class _SourcesDb:
    """Answers the enabled OpenCTI Signals sources query; the holders are patched in."""

    def __init__(self, enabled_integrations: list[str]) -> None:
        self._sources = [SimpleNamespace(integration_id=i) for i in enabled_integrations]

    async def execute(self, _statement: Any) -> _Rows:
        return _Rows(self._sources)


def _holder(name: str, state: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(name=name, trigger_config={"provider": "opencti", prov.STATE_KEY: state})


def _patch_holders(monkeypatch: pytest.MonkeyPatch, holders: list[tuple[SimpleNamespace, str]]) -> None:
    async def states(_db: Any) -> list[tuple[Any, str, dict[str, Any]]]:
        return [(a, kind, a.trigger_config[prov.STATE_KEY]) for a, kind in holders]

    monkeypatch.setattr(prov, "_legacy_holder_states", states)


@pytest.mark.asyncio
async def test_pending_counts_the_objects_held_by_holders_not_retired(monkeypatch: pytest.MonkeyPatch) -> None:
    threat = _holder(
        "New relevant threat",
        {"status": "provisioned", "trigger_id": "t1", "notifier_id": "n1", "integration_id": "i1"},
    )
    pir = _holder(
        "Threat activity flagged in a PIR",
        {"status": "provisioned", "playbook_id": "p1", "notifier_id": "n2", "integration_id": "i1"},
    )
    other = _holder("New relevant threat", {"status": "error", "trigger_id": "t9", "integration_id": "i2"})
    _patch_holders(monkeypatch, [(threat, "listener"), (pir, "pir_playbook"), (other, "listener")])
    # i2 has no Signals source on: its holder still delivers, nothing to clean there.
    assert await prov.legacy_pending(_SourcesDb(["i1"])) == {"i1": 4}


@pytest.mark.asyncio
async def test_nothing_pending_once_the_pass_retired_every_holder(monkeypatch: pytest.MonkeyPatch) -> None:
    retired = _holder("New relevant threat", {"status": prov.RETIRED, "integration_id": "i1"})
    _patch_holders(monkeypatch, [(retired, "listener")])
    assert await prov.legacy_pending(_SourcesDb(["i1"])) == {}


@pytest.mark.asyncio
async def test_a_removal_that_left_nothing_retires_the_holders_whose_source_delivers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivered = _holder(
        "New relevant threat",
        {"status": "provisioned", "trigger_id": "t1", "notifier_id": "n1", "integration_id": "i1"},
    )
    not_yet = _holder(
        "Threat activity flagged in a PIR",
        {"status": "provisioned", "playbook_id": "p1", "notifier_id": "n2", "integration_id": "i1"},
    )
    _patch_holders(monkeypatch, [(delivered, "listener"), (not_yet, "pir_playbook")])

    async def connected(_db: Any, _provider: str, kind: str, _integration_id: Any) -> bool:
        return kind == "listener"

    monkeypatch.setattr("app.services.ctem_platform_sources.source_connected", connected)
    await prov._retire_cleared_holders(None, "i1", now=NOW)
    assert delivered.trigger_config[prov.STATE_KEY] == {
        "status": prov.RETIRED,
        "integration_id": "i1",
        "checked_at": NOW.isoformat(),
    }
    # Its PIR source does not deliver yet: the holder keeps its state and provisions again.
    assert not_yet.trigger_config[prov.STATE_KEY]["status"] == "provisioned"
    assert delivered.trigger_config["provider"] == "opencti"
