"""Per-module coverage of the Filigran license on the licence detail.

The licensing service writes ``global`` in a module's IDs extension when the
module's "Enterprise Edition" box is checked (the default), the listed
platform IDs when the issuer restricts it, and an empty list when the box is
unchecked. ``/platform/config`` says per module whether it is covered so a
product (OpenCRQ first) reads one answer instead of re-deriving it.
"""

from __future__ import annotations

from app.api.v1.platform import ModuleCoverage, SubLicenses, _xtm_sublicense_grants_ee
from app.enterprise.licensing import XTMLicenseInfo


def _info(**ids: list[str]) -> XTMLicenseInfo:
    info = XTMLicenseInfo(license_validated=True)
    info.opencti_ids = ids.get("opencti", [])
    info.openaev_ids = ids.get("openaev", [])
    info.opencrq_ids = ids.get("opencrq", [])
    return info


def test_global_covers_every_instance_of_the_module() -> None:
    coverage = ModuleCoverage.from_ids(["global"])
    assert coverage.covered is True
    assert coverage.scope == "global"
    assert coverage.platform_ids == []


def test_listed_ids_cover_those_instances_only() -> None:
    coverage = ModuleCoverage.from_ids(["pf-1", "", "pf-2"])
    assert coverage.covered is True
    assert coverage.scope == "platforms"
    assert coverage.platform_ids == ["pf-1", "pf-2"]


def test_global_wins_over_listed_ids() -> None:
    coverage = ModuleCoverage.from_ids(["pf-1", "global"])
    assert coverage.scope == "global"
    assert coverage.platform_ids == []


def test_an_empty_list_covers_nothing() -> None:
    coverage = ModuleCoverage.from_ids([])
    assert coverage.covered is False
    assert coverage.scope == "none"


def test_the_detail_names_each_module_and_keeps_the_raw_ids() -> None:
    info = _info(opencti=["global"], openaev=["pf-aev"], opencrq=[])
    sub = SubLicenses.from_license(info)
    assert sub.opencti_ids == ["global"]
    assert sub.openaev_ids == ["pf-aev"]
    assert sub.opencrq_ids == []
    assert set(sub.modules) == {"opencti", "openaev", "opencrq"}
    assert sub.modules["opencti"].scope == "global"
    assert sub.modules["openaev"].platform_ids == ["pf-aev"]
    assert sub.modules["opencrq"].covered is False


def test_no_license_serialises_three_uncovered_modules() -> None:
    payload = SubLicenses().model_dump()
    assert payload["modules"] == {
        name: {"covered": False, "scope": "none", "platform_ids": []}
        for name in ("opencti", "openaev", "opencrq")
    }


def test_the_coverage_agrees_with_the_registration_grant() -> None:
    """The detail and the register answer read the same extension the same way."""
    info = _info(opencti=["global"], openaev=["pf-aev"], opencrq=[])
    sub = SubLicenses.from_license(info)
    assert _xtm_sublicense_grants_ee(info, "opencti", "any-instance")
    assert _xtm_sublicense_grants_ee(info, "openaev", "pf-aev")
    assert not _xtm_sublicense_grants_ee(info, "openaev", "another-instance")
    assert not _xtm_sublicense_grants_ee(info, "opencrq", "pf-crq")
    assert sub.modules["opencrq"].covered is False


def test_a_model_2_license_covers_every_module_whatever_its_extensions_say() -> None:
    """Licence model 2 (OID 62944.120, 17 Nov 2026): EE on every module by design."""
    info = _info(opencti=[], openaev=["pf-aev"], opencrq=[])
    info.license_model = 2
    assert info.covers_every_module
    sub = SubLicenses.from_license(info)
    for name in ("opencti", "openaev", "opencrq"):
        assert sub.modules[name].covered is True
        assert sub.modules[name].scope == "global"
    for identifier in ("opencti", "openaev", "openbas", "opencrq", "opengrc"):
        assert _xtm_sublicense_grants_ee(info, identifier, "any-instance")
    assert not _xtm_sublicense_grants_ee(info, "unknown-product", "any-instance")
    # The raw extensions stay on the wire as written.
    assert sub.openaev_ids == ["pf-aev"]


def test_a_license_without_the_model_claim_keeps_its_per_module_grants() -> None:
    """Every licence issued before 17 Nov 2026 has no model claim: model 1."""
    info = _info(opencti=["global"], openaev=[], opencrq=[])
    assert info.license_model == 1
    assert not info.covers_every_module
    assert not _xtm_sublicense_grants_ee(info, "openaev", "pf-aev")
    assert SubLicenses.from_license(info).modules["openaev"].covered is False


def test_an_invalid_model_2_license_grants_nothing() -> None:
    info = _info()
    info.license_validated = False
    info.license_model = 2
    assert not _xtm_sublicense_grants_ee(info, "opencti", "any-instance")


def test_the_legacy_opengrc_identifier_reads_the_opencrq_module() -> None:
    info = _info(opencrq=["global"])
    assert _xtm_sublicense_grants_ee(info, "opengrc", "pf-crq")
    assert SubLicenses.from_license(info).modules["opencrq"].covered is True
