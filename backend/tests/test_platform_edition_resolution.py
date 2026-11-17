"""Regression coverage for platform-registration edition resolution.

The home-page platform card (``CtemOpsCenter``) reads
``PlatformRegistration.contract`` while the integrations list reads the
company-managed integration's edition tag (``Integration.tags``). Both are
written by ``register_platform`` and MUST agree. Previously the contract was
``"EE"`` whenever EE was granted by validated PEM *or* by an EE
``license_type``, but the integration tag was derived from the PEM validation
alone — so a platform self-reporting an EE ``license_type`` without a
server-validated PEM showed EE on the home page but a stale CE tag on the
integration row.

``_resolve_edition`` is now the single source of truth for both values. These
tests pin its behaviour so the two surfaces can never diverge again.
"""

from __future__ import annotations

import pytest

from app.api.v1.platform import (
    _EE_LICENSE_TYPES,
    _resolve_edition,
    _xtm_sublicense_grants_ee,
)
from app.enterprise.licensing import XTMLicenseInfo


def test_validated_pem_is_ee():
    """A server-validated enterprise license PEM always grants EE."""
    assert _resolve_edition(license_validated=True, license_type=None) == "EE"
    # PEM wins even if the self-reported type is not in the EE set.
    assert _resolve_edition(license_validated=True, license_type="community") == "EE"


@pytest.mark.parametrize("license_type", sorted(_EE_LICENSE_TYPES))
def test_ee_license_type_without_pem_is_ee(license_type):
    """The bug case: an EE ``license_type`` with no validated PEM is EE.

    This is exactly the mismatch where the integration tag used to
    fall back to CE here while the home-page contract was EE.
    """
    assert _resolve_edition(license_validated=False, license_type=license_type) == "EE"


def test_non_ee_license_type_without_pem_is_ce():
    assert _resolve_edition(license_validated=False, license_type="community") == "CE"


def test_no_license_type_and_no_pem_is_ce():
    assert _resolve_edition(license_validated=False, license_type=None) == "CE"
    assert _resolve_edition(license_validated=False, license_type="") == "CE"


def test_xtm_sublicense_without_pem_or_license_type_is_ee():
    """The OpenCRQ bug case: XTM sub-license alone must display EE.

    OpenCRQ has no product CA (PEM never validates) and sends no
    ``license_type``, so before the fix an XTM-sublicensed OpenCRQ instance
    registered with ``ee_enabled=True`` but ``contract`` (home-page card)
    and the integration tag stayed "CE".
    """
    assert (
        _resolve_edition(
            license_validated=False, license_type=None, xtm_sublicensed=True
        )
        == "EE"
    )


def _xtm(validated: bool = True, **ids) -> XTMLicenseInfo:
    info = XTMLicenseInfo()
    info.license_validated = validated
    for key, value in ids.items():
        setattr(info, key, value)
    return info


def test_xtm_sublicense_predicate_matches_platform_id_or_global():
    xtm = _xtm(opencrq_ids=["instance-1"])
    assert _xtm_sublicense_grants_ee(xtm, "opencrq", "instance-1")
    assert not _xtm_sublicense_grants_ee(xtm, "opencrq", "other")
    assert not _xtm_sublicense_grants_ee(xtm, "opencrq", None)

    xtm_global = _xtm(opencrq_ids=["global"])
    assert _xtm_sublicense_grants_ee(xtm_global, "opencrq", None)


def test_xtm_sublicense_predicate_is_per_platform_type():
    xtm = _xtm(opencti_ids=["global"])
    assert not _xtm_sublicense_grants_ee(xtm, "opencrq", "instance-1")
    assert _xtm_sublicense_grants_ee(xtm, "opencti", "instance-1")


def test_unvalidated_xtm_license_grants_nothing():
    xtm = _xtm(validated=False, opencrq_ids=["global"])
    assert not _xtm_sublicense_grants_ee(xtm, "opencrq", "instance-1")
