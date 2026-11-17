"""Tests for the effective EE status returned by ``POST /platform/register``.

``RegistrationResponse.ee_enabled`` is XTM One's view of a platform's edition
(advisory: the XTM path is proven by ``xtm_license_pem``, see
``test_registration_license_proof.py``). It must reflect BOTH edition paths: a
server-validated enterprise license PEM (legacy) and Filigran license sub-licensing
(source of truth).
"""

from types import SimpleNamespace

from app.api.v1.platform import _registration_ee_enabled, _registration_ee_sources
from app.enterprise.licensing import XTMLicenseInfo


def _reg(
    identifier: str = "opencrq",
    platform_id: str | None = "instance-1",
    license_validated: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        platform_identifier=identifier,
        platform_id=platform_id,
        license_validated=license_validated,
    )


def _xtm(validated: bool = True, **ids) -> XTMLicenseInfo:
    info = XTMLicenseInfo()
    info.license_validated = validated
    for key, value in ids.items():
        setattr(info, key, value)
    return info


def test_validated_pem_grants_ee_without_xtm_license():
    assert _registration_ee_enabled(_reg(license_validated=True), _xtm(validated=False))


def test_no_pem_no_xtm_license_is_ce():
    assert not _registration_ee_enabled(_reg(), _xtm(validated=False))


def test_xtm_sublicense_grants_ee_by_platform_id():
    xtm = _xtm(opencrq_ids=["instance-1"])
    assert _registration_ee_enabled(_reg(platform_id="instance-1"), xtm)
    assert not _registration_ee_enabled(_reg(platform_id="other"), xtm)


def test_xtm_sublicense_global_grants_ee_for_platform_type():
    xtm = _xtm(opencrq_ids=["global"])
    assert _registration_ee_enabled(_reg(platform_id=None), xtm)


def test_xtm_sublicense_is_per_platform_type():
    # An OpenCTI sub-license must not grant EE to an OpenCRQ registration.
    xtm = _xtm(opencti_ids=["global"])
    assert not _registration_ee_enabled(_reg(identifier="opencrq"), xtm)
    assert _registration_ee_enabled(_reg(identifier="opencti"), xtm)


def test_openbas_alias_uses_openaev_ids():
    xtm = _xtm(openaev_ids=["global"])
    assert _registration_ee_enabled(_reg(identifier="openbas"), xtm)


def test_unvalidated_xtm_license_grants_nothing():
    xtm = _xtm(validated=False, opencrq_ids=["global"])
    assert not _registration_ee_enabled(_reg(), xtm)


def test_ee_sources_name_every_granting_path_in_a_fixed_order():
    xtm = _xtm(opencrq_ids=["instance-1"])
    assert _registration_ee_sources(_reg(license_validated=True), xtm) == [
        "platform_license",
        "xtm_sublicense",
    ]
    assert _registration_ee_sources(_reg(), xtm) == ["xtm_sublicense"]
    assert _registration_ee_sources(
        _reg(license_validated=True), _xtm(validated=False)
    ) == ["platform_license"]
    assert _registration_ee_sources(_reg(platform_id="other"), xtm) == []


def test_ee_enabled_is_exactly_a_non_empty_ee_sources():
    xtm = _xtm(opencrq_ids=["instance-1"])
    for reg in (
        _reg(),
        _reg(platform_id="other"),
        _reg(license_validated=True),
        _reg(platform_id="other", license_validated=True),
    ):
        for info in (xtm, _xtm(validated=False)):
            assert _registration_ee_enabled(reg, info) is bool(
                _registration_ee_sources(reg, info)
            )
