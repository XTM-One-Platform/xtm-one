# SPDX-License-Identifier: Apache-2.0

"""Per path source exclusions of the CTEM signals (#1445, ``dev-docs/ctem-signals.md`` s.2bis).

An administrator takes one connected source off one trigger path: the source is
not bound on that path (status and count follow), it keeps feeding its other
paths, and the recording points record nothing from it on that path.
"""

from __future__ import annotations

import pytest

from app.services import ctem_attack_surface as surface
from app.services import ctem_signal_sources as sources
from app.services import ctem_signals
from app.services import organization_context as oc


def _profile(excluded: dict[str, list[str]]) -> oc.OrganizationProfile:
    return oc.parse_profile({"signal_sources": {"excluded": excluded}})


def test_a_profile_without_the_field_excludes_nothing_and_dumps_no_new_text() -> None:
    profile = oc.parse_profile({})
    assert profile.signal_sources.excluded == {}
    assert sources.parse_excluded(profile) == {}
    assert "signal_sources" in oc.EXPOSURE_FIELDS


def test_the_setting_keeps_known_sources_only_sorted_and_deduplicated() -> None:
    profile = _profile({"new_exposure": ["shodan:pass", "gti:pass", "gti:pass"], "surface_drift": []})
    assert profile.signal_sources.excluded == {"new_exposure": ["gti:pass", "shodan:pass"]}
    with pytest.raises(ValueError):
        _profile({"new_exposure": ["opencti:listener"]})
    with pytest.raises(ValueError):
        _profile({"not_a_path": ["shodan:pass"]})
    with pytest.raises(ValueError):
        _profile({"new_exposure": ["shodan"]})


def test_an_excluded_source_is_not_bound_on_its_path_and_still_bound_on_the_others() -> None:
    bound = sources.BoundSources({("shodan", "pass"), ("gti", "pass")})
    bound.excluded = sources.parse_excluded(_profile({"new_exposure": ["gti:pass"]}))
    exposure = {s["provider"]: s for s in sources.path_sources("new_exposure", bound)}
    assert exposure["gti"]["bound"] is False and exposure["gti"]["excluded"] is True
    assert exposure["shodan"]["bound"] is True and exposure["shodan"]["excluded"] is False
    drift = {s["provider"]: s for s in sources.path_sources("surface_drift", bound)}
    assert drift["gti"]["bound"] is True and drift["gti"]["excluded"] is False


def test_a_path_whose_only_bound_source_is_excluded_no_longer_reads_connected() -> None:
    bound = sources.BoundSources({("opencrq", "pass")})
    bound.excluded = sources.parse_excluded(_profile({"control_decay": ["opencrq:pass"]}))
    rows = {r["id"]: r for r in ctem_signals.path_catalog({}, bound=bound, activity={})}
    assert rows["control_decay"]["status"] == "not_connected"
    assert rows["blind_spot"]["status"] == "connected"


def test_a_shared_pass_names_the_other_paths_it_feeds() -> None:
    row = {s["provider"]: s for s in sources.path_sources("new_exposure", None)}
    assert row["shodan"]["also_feeds"] == ["stale_asset", "surface_drift"]
    control = sources.path_sources("control_decay", None)[0]
    assert control["also_feeds"] == ["blind_spot"]


def test_is_excluded_reads_one_path_and_one_kind() -> None:
    excluded = sources.parse_excluded(_profile({"new_vulnerability": ["opencti:pir_playbook"]}))
    assert sources.is_excluded(excluded, "new_vulnerability", "opencti", "pir_playbook")
    assert not sources.is_excluded(excluded, "new_vulnerability", "opencti", "listener")
    assert not sources.is_excluded(excluded, "relevant_threat", "opencti", "pir_playbook")


def test_the_attack_surface_pass_skips_a_finding_only_excluded_providers_saw() -> None:
    excluded = sources.parse_excluded(_profile({"new_exposure": ["gti:pass"]}))
    assert surface._kept_for_path(excluded, "new_exposure", ["shodan"]) is True
    assert surface._kept_for_path(excluded, "new_exposure", ["gti"]) is False
    # Seen by both: Shodan still feeds the path, so the signal is recorded.
    assert surface._kept_for_path(excluded, "new_exposure", ["gti", "shodan"]) is True
    # The other paths of the same pass are untouched.
    assert surface._kept_for_path(excluded, "surface_drift", ["gti"]) is True
