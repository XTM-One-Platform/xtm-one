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
    assert sources.path_status([{"bound": False}, {"bound": True}]) == "enabled"
    assert sources.path_status([{"bound": False, "off": True}]) == "off"
    assert sources.path_status([{"bound": True, "off": True}]) == "enabled"


def test_path_status_reads_the_sources_on_off_state_never_the_connection() -> None:
    """#2436 (Sam, 26 Nov 2026): Enabled when every used source is on, Partly enabled when some are,
    Off when the only source is switched off, whatever the integration connection says."""
    on = {"bound": True}
    off = {"bound": False, "off": True}
    assert sources.path_status([on, on]) == "enabled"
    assert sources.path_status([on, off]) == "partly_enabled"
    assert sources.path_status([on, {"bound": False, "not_set_up": True}]) == "partly_enabled"
    # A source taken off the path and a source of a provider not connected here never make it partial.
    assert sources.path_status([on, {"bound": False, "excluded": True, "off": True}]) == "enabled"
    assert sources.path_status([on, {"bound": False}]) == "enabled"
    assert sources.path_status([off]) == "off"
    assert "connected" not in sources.PATH_STATUSES


def test_a_provisioned_source_switched_off_reads_off_on_its_path() -> None:
    """Sam, 26 Nov 2026: Mitigation regression read Connected while its only source, the OpenAEV listener, was Off."""
    bound = sources.BoundSources()
    bound.off.add(("openaev", "listener"))
    row = sources.path_sources("mitigation_regression", bound)
    assert row[0]["off"] is True and row[0]["bound"] is False
    assert sources.path_status(row) == "off"


def test_one_status_per_path_a_provisioned_source_says_it() -> None:
    """UX round 1 on #2442, B1: a connected provider whose provisioned source is off reads Not set up,
    never Not connected; a source in error turns the path to Error, over any bound source."""
    bound = sources.BoundSources()
    bound.not_set_up.add(("openaev", "listener"))
    row = sources.path_sources("proven_exploitable", bound)
    assert row[0]["not_set_up"] is True and row[0]["error_count"] == 0
    assert sources.path_status(row) == "not_set_up"

    bound = sources.BoundSources({("openaev", "listener")})
    bound.error[("openaev", "listener")] = 1
    row = sources.path_sources("new_exposure", bound)
    assert row[0]["error_count"] == 1
    assert sources.path_status(row) == "error"
    assert sources.path_status([{"bound": False, "not_set_up": True}, {"bound": False, "off": True}]) == "not_set_up"


def test_a_cases_reader_of_a_connected_provider_reads_on_never_not_set_up() -> None:
    """UX round 1 on #2592, B1: the cases recompute needs no set-up, so a path whose other sources are
    on reads Enabled; an excluded reader reads Not used here; no reader without its provider."""
    assert sources.reader_pairs({"opencti": {"i1"}, "openaev": {"i2"}, "github": {"i3"}}) == {
        ("opencti", "cases_reader"),
        ("openaev", "cases_reader"),
    }
    path = next(
        p
        for p, row in sources.PATH_SOURCES.items()
        if any(s.provider == "opencti" and s.kind == "cases_reader" for s in row)
    )
    on = {(s.provider, s.kind) for s in sources.PATH_SOURCES[path] if s.provider == "opencti"}
    bound = sources.BoundSources(on | sources.reader_pairs({"opencti": {"i1"}}))
    row = sources.path_sources(path, bound)
    reader = next(s for s in row if s["kind"] == "cases_reader")
    assert reader["bound"] is True and reader["not_set_up"] is False
    assert sources.path_status([s for s in row if s["provider"] == "opencti"]) == "enabled"

    bound.excluded = {path: {("opencti", "cases_reader")}}
    reader = next(s for s in sources.path_sources(path, bound) if s["kind"] == "cases_reader")
    assert reader["bound"] is False and reader["excluded"] is True and reader["not_set_up"] is False
    assert sources.reader_pairs({}) == set()


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
    assert rows["surface_drift"]["status"] == "enabled"
    assert rows["surface_drift"]["count_30d"] == 3
    assert rows["control_decay"]["status"] == "not_connected"
    assert rows["code_flaw"]["sources"][0] == {
        "provider": "aikido",
        "kind": "listener",
        "key": "aikido:listener",
        "name": "",
        "default_on": True,
        "event": sources.PATH_SOURCES["code_flaw"][0].event,
        "bound": False,
        "off": False,
        "not_set_up": False,
        "error_count": 0,
        "errors": [],
        "not_configured": False,
        "excluded": False,
        "available": False,
        "also_feeds": ["credential_leak"],
        # #2736: the integrations the source reads; none when the caller asked for none.
        "integrations": [],
    }
    assert rows["new_exposure"]["last_signal_at"] is None


def test_each_source_carries_its_own_30_day_activity() -> None:
    """#2436: the path detail table shows the signals of the last 30 days and the last signal per source."""
    by_source = {
        "credential_leak": {
            "github:listener:secret_scanning": {"count_30d": 4, "last_signal_at": "2026-11-25T08:00:00+00:00"},
            "aikido:listener": {"count_30d": 1, "last_signal_at": "2026-11-20T08:00:00+00:00"},
            # Recorded before #2436 stamped the source key: counted under the provider's only unnamed source.
            "aikido": {"count_30d": 2, "last_signal_at": "2026-11-24T08:00:00+00:00"},
        },
        # Two unnamed OpenCTI sources on this path: a legacy signal is never guessed onto one of them.
        "new_vulnerability": {"opencti": {"count_30d": 5, "last_signal_at": "2026-11-24T08:00:00+00:00"}},
    }
    rows = {r["id"]: r for r in ctem_signals.path_catalog({}, by_source=by_source)}
    leak = {s["key"]: s for s in rows["credential_leak"]["sources"]}
    assert leak["github:listener:secret_scanning"]["count_30d"] == 4
    assert leak["aikido:listener"]["count_30d"] == 3
    assert leak["aikido:listener"]["last_signal_at"] == "2026-11-24T08:00:00+00:00"
    assert leak["opencti:listener"]["count_30d"] == 0 and leak["opencti:listener"]["last_signal_at"] is None
    assert all(s["count_30d"] == 0 for s in rows["new_vulnerability"]["sources"] if s["provider"] == "opencti")


def test_the_catalog_without_activity_keeps_its_former_shape_plus_sources() -> None:
    row = ctem_signals.path_catalog()[0]
    # #2506: plus the sources enabled / available counts of the path list.
    assert {
        "id",
        "label",
        "default_lens",
        "listener",
        "description",
        "sources",
        "status",
        "capabilities",
        "sources_enabled",
        "sources_available",
    } == set(row)


def test_a_source_in_error_carries_its_reason_last_attempt_and_fix_on_its_path() -> None:
    """#2637: an error on a path says why, when it was last tried and what fixes it, in the path answer."""
    from app.services.ctem_platform_sources import error_detail

    bound = sources.BoundSources()
    bound.error[("opencti", "listener")] = 1
    bound.error_details[("opencti", "listener")] = [
        error_detail(
            "i-1",
            "the run-as user is deactivated or no longer exists: pick another run-as",
            {"status": "error", "checked_at": "2026-11-27T15:00:00+00:00"},
            enabled=True,
        )
    ]
    row = sources.path_sources("third_party_event", bound)
    assert row[0]["error_count"] == 1 and sources.path_status(row) == "error"
    (detail,) = row[0]["errors"]
    assert detail["reason_code"] == "run_as_inactive"
    assert detail["last_attempt_at"] == "2026-11-27T15:00:00+00:00"
    assert detail["fix"] and detail["fix_route"]


def test_a_source_off_for_a_missing_setting_reads_not_configured_never_error() -> None:
    """#2637: a source never switched on for a missing setting is no error: its path counts no error."""
    bound = sources.BoundSources()
    bound.not_set_up.add(("opencti", "listener"))
    bound.not_configured.add(("opencti", "listener"))
    row = sources.path_sources("third_party_event", bound)
    assert row[0]["not_configured"] is True and row[0]["error_count"] == 0 and row[0]["errors"] == []
    assert sources.path_status(row) != "error"


def test_the_agent_tool_reads_the_same_status_and_errors_as_the_page() -> None:
    """#2637: list_ctem_signal_sources answers each path's status and each source's state and error."""
    from app.services.builtin_tools.ctem_signals import signal_sources_report
    from app.services.ctem_platform_sources import error_detail

    bound = sources.BoundSources()
    bound.error[("opencti", "listener")] = 1
    bound.error_details[("opencti", "listener")] = [
        error_detail(
            "i-1", "objects not removed: trigger t-1", {"checked_at": "2026-11-27T15:00:00+00:00"}, enabled=False
        )
    ]
    report = signal_sources_report(bound, "third_party_event")
    (path,) = report["paths"]
    assert path["path"] == "third_party_event" and path["status"] == "error" and path["sources_in_error"] == 1
    (source,) = path["sources"]
    assert source["state"] == "error" and source["errors"][0]["reason_code"] == "objects_not_removed"

    quiet = sources.BoundSources()
    quiet.not_set_up.add(("opencti", "listener"))
    quiet.not_configured.add(("opencti", "listener"))
    (path,) = signal_sources_report(quiet, "third_party_event")["paths"]
    assert path["sources_in_error"] == 0 and path["sources"][0]["state"] == "not_configured"
    assert "errors" not in path["sources"][0]


def test_the_code_paths_name_the_fleet_capability_that_feeds_them() -> None:
    """#1434: a fleet without Application security says why code_flaw and credential_leak are silent."""
    from app.services.fleet_capability_slots import CAPABILITIES

    rows = {r["id"]: r for r in ctem_signals.path_catalog()}
    assert "application_security" in rows["code_flaw"]["capabilities"]
    assert rows["credential_leak"]["capabilities"] == ["application_security"]
    for caps in sources.PATH_CAPABILITIES.values():
        assert set(caps) <= set(CAPABILITIES)
