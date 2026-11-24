# SPDX-License-Identifier: Apache-2.0

"""The signal sources of each CTEM trigger path (#1189): table coverage and status derivation."""

from __future__ import annotations

from app.models.ctem_signal import TRIGGER_PATHS
from app.services import ctem_signal_listeners as listeners
from app.services import ctem_signal_sources as sources
from app.services import ctem_signals


def test_every_path_has_a_description_and_a_sources_row() -> None:
    assert set(sources.PATH_DESCRIPTIONS) == set(TRIGGER_PATHS)
    assert set(sources.PATH_SOURCES) == set(TRIGGER_PATHS)
    assert all(s.kind in sources.SOURCE_KINDS for row in sources.PATH_SOURCES.values() for s in row)


def test_every_listener_row_is_named_as_a_listener_source_of_its_path() -> None:
    for row in listeners.LISTENERS:
        named = {(s.provider, s.kind) for s in sources.PATH_SOURCES[row.trigger_path]}
        assert (row.provider, "listener") in named, (row.provider, row.trigger_path)


def test_status_never_reads_connected_without_a_bound_source() -> None:
    assert sources.path_status([]) == "agents_api"
    assert sources.path_status([{"bound": False}, {"bound": False}]) == "not_connected"
    assert sources.path_status([{"bound": False}, {"bound": True}]) == "connected"
    assert sources.path_status([{"bound": False, "off": True}]) == "off"
    assert sources.path_status([{"bound": True, "off": True}]) == "connected"


def test_a_source_off_reads_off_and_never_bound() -> None:
    bound = sources.BoundSources({("opencti", "listener")})
    bound.off.add(("opencrq", "pass"))
    row = sources.path_sources("control_decay", bound)
    assert row[0]["bound"] is False and row[0]["off"] is True
    assert sources.path_status(row) == "off"


def test_the_catalog_carries_sources_status_and_activity() -> None:
    rows = {
        r["id"]: r
        for r in ctem_signals.path_catalog(
            {},
            bound={("shodan", "pass")},
            activity={"surface_drift": {"count_30d": 3, "last_signal_at": "2026-11-20T10:00:00+00:00"}},
        )
    }
    assert rows["surface_drift"]["status"] == "connected"
    assert rows["surface_drift"]["count_30d"] == 3
    assert rows["control_decay"]["status"] == "not_connected"
    assert rows["code_flaw"]["sources"][0] == {
        "provider": "aikido",
        "kind": "listener",
        "event": sources.PATH_SOURCES["code_flaw"][0].event,
        "bound": False,
        "off": False,
        "excluded": False,
        "available": False,
        "also_feeds": ["credential_leak"],
    }
    assert rows["new_exposure"]["last_signal_at"] is None


def test_the_catalog_without_activity_keeps_its_former_shape_plus_sources() -> None:
    row = ctem_signals.path_catalog()[0]
    assert {"id", "label", "default_lens", "listener", "description", "sources", "status", "capabilities"} == set(row)


def test_the_code_paths_name_the_fleet_capability_that_feeds_them() -> None:
    """#1434: a fleet without Application security says why code_flaw and credential_leak are silent."""
    from app.services.fleet_capability_slots import CAPABILITIES

    rows = {r["id"]: r for r in ctem_signals.path_catalog()}
    assert "application_security" in rows["code_flaw"]["capabilities"]
    assert rows["credential_leak"]["capabilities"] == ["application_security"]
    for caps in sources.PATH_CAPABILITIES.values():
        assert set(caps) <= set(CAPABILITIES)
