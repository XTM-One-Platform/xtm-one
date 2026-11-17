"""Tests for the CI license expiration cap.

``ci``-type licenses are issued for a single pipeline run, so their effective
expiration is capped to ``min(platform_created_at + 45 min, cert_notBefore +
1 yr)`` — mirroring OpenCTI's ``licensing.ts``. These tests generate a real
CA + client certificate pair with the Filigran license OID extensions and pin
the cap semantics end-to-end: the raw decode, the cache validity check in
``get_xtm_license_info``, and the install-time validation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import NameOID

from app.enterprise.licensing import (
    LICENSE_OID_PRODUCT,
    LICENSE_OID_TYPE,
    LicenseValidationError,
    clear_license_cache,
    decode_xtm_license_pem,
    get_xtm_license_info,
    validate_enterprise_license_for_install,
)

_KEY_SIZE = 2048
_PRODUCT = "filigran xtm"


def _make_license_pair(
    license_type: str = "ci",
    not_before: datetime | None = None,
    not_after: datetime | None = None,
) -> tuple[str, str]:
    """Return ``(client_pem, ca_pem)`` for a license cert of the given type."""
    now = datetime.now(UTC)
    not_before = not_before or (now - timedelta(minutes=5))
    not_after = not_after or (now + timedelta(days=365))

    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=_KEY_SIZE)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test License CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))
        .sign(ca_key, hashes.SHA256())
    )

    client_key = rsa.generate_private_key(public_exponent=65537, key_size=_KEY_SIZE)
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Test Customer"),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "global"),
            x509.NameAttribute(NameOID.COMMON_NAME, "test-license"),
        ]
    )
    client_cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_name)
        .public_key(client_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(
            x509.UnrecognizedExtension(LICENSE_OID_TYPE, license_type.encode()),
            critical=False,
        )
        .add_extension(
            x509.UnrecognizedExtension(LICENSE_OID_PRODUCT, _PRODUCT.encode()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    return (
        client_cert.public_bytes(Encoding.PEM).decode(),
        ca_cert.public_bytes(Encoding.PEM).decode(),
    )


def test_ci_license_valid_within_platform_window():
    pem, ca_pem = _make_license_pair("ci")
    created_at = datetime.now(UTC) - timedelta(minutes=10)

    info = decode_xtm_license_pem(pem, ca_pem, platform_created_at=created_at)

    assert info.license_valid_cert
    assert info.license_type == "ci"
    assert info.license_expiration_date == created_at + timedelta(minutes=45)
    assert not info.license_expired
    assert info.license_validated


def test_ci_license_expires_45_minutes_after_platform_creation():
    pem, ca_pem = _make_license_pair("ci")
    created_at = datetime.now(UTC) - timedelta(hours=2)

    info = decode_xtm_license_pem(pem, ca_pem, platform_created_at=created_at)

    assert info.license_valid_cert
    assert info.license_expiration_date == created_at + timedelta(minutes=45)
    assert info.license_expired
    # CI licenses get no grace period — invalidated immediately.
    assert not info.license_validated
    assert not info.license_extra_expiration


def test_ci_cap_uses_cert_start_plus_one_year_when_earlier():
    # Cert issued 400 days ago: notBefore + 1yr is already in the past, so it
    # wins over the fresh platform window and the license is expired.
    now = datetime.now(UTC)
    pem, ca_pem = _make_license_pair(
        "ci",
        not_before=now - timedelta(days=400),
        not_after=now + timedelta(days=365),
    )

    info = decode_xtm_license_pem(pem, ca_pem, platform_created_at=now)

    assert info.license_valid_cert
    assert info.license_expiration_date == info.license_start_date + timedelta(days=365)
    assert info.license_expired
    assert not info.license_validated


def test_ci_license_is_refused_without_platform_created_at():
    # No creation date, no 45-minute cap: the certificate would run to its own
    # end, so it is not granted (OpenCTI's xtm-one-license.ts does the same).
    pem, ca_pem = _make_license_pair("ci")

    info = decode_xtm_license_pem(pem, ca_pem)

    assert info.license_valid_cert
    assert info.license_type == "ci"
    assert not info.license_validated


def test_install_validation_refuses_a_ci_license_without_platform_created_at():
    pem, ca_pem = _make_license_pair("ci")

    with pytest.raises(LicenseValidationError, match="creation date"):
        validate_enterprise_license_for_install(pem, ca_pem=ca_pem)


def test_non_ci_license_ignores_platform_created_at():
    pem, ca_pem = _make_license_pair("standard")
    created_at = datetime.now(UTC) - timedelta(days=30)

    info = decode_xtm_license_pem(pem, ca_pem, platform_created_at=created_at)

    assert info.license_valid_cert
    assert info.license_type == "standard"
    assert not info.license_expired
    assert info.license_validated


def test_non_ci_license_needs_no_platform_created_at():
    pem, ca_pem = _make_license_pair("standard")

    assert decode_xtm_license_pem(pem, ca_pem).license_validated


def test_cache_revalidates_when_platform_created_at_changes():
    """A cached entry decoded without a creation date must not mask a dated call."""
    pem, ca_pem = _make_license_pair("ci")
    cache_key = "xtm_test_ci_cache"
    clear_license_cache(cache_key)
    try:
        info_undated = get_xtm_license_info(
            pem_from_env=pem,
            pem_from_db=None,
            ca_pem=ca_pem,
            cache_key=cache_key,
        )
        assert not info_undated.license_validated

        info_in_window = get_xtm_license_info(
            pem_from_env=pem,
            pem_from_db=None,
            ca_pem=ca_pem,
            platform_created_at=datetime.now(UTC) - timedelta(minutes=10),
            cache_key=cache_key,
        )
        assert info_in_window.license_validated

        created_at = datetime.now(UTC) - timedelta(hours=2)
        info_capped = get_xtm_license_info(
            pem_from_env=pem,
            pem_from_db=None,
            ca_pem=ca_pem,
            platform_created_at=created_at,
            cache_key=cache_key,
        )
        assert info_capped.license_expired
        assert not info_capped.license_validated
    finally:
        clear_license_cache(cache_key)


def test_cache_reused_for_non_ci_license_when_platform_created_at_changes():
    """Non-CI entries ignore ``platform_created_at`` — no needless re-decode."""
    pem, ca_pem = _make_license_pair("standard")
    cache_key = "xtm_test_standard_cache"
    clear_license_cache(cache_key)
    try:
        info_first = get_xtm_license_info(
            pem_from_env=pem,
            pem_from_db=None,
            ca_pem=ca_pem,
            cache_key=cache_key,
        )
        assert info_first.license_validated

        info_second = get_xtm_license_info(
            pem_from_env=pem,
            pem_from_db=None,
            ca_pem=ca_pem,
            platform_created_at=datetime.now(UTC) - timedelta(hours=2),
            cache_key=cache_key,
        )
        # Same object served from cache: the cap only applies to ci licenses.
        assert info_second is info_first
        assert info_second.license_validated
    finally:
        clear_license_cache(cache_key)


def test_install_validation_rejects_expired_ci_license():
    pem, ca_pem = _make_license_pair("ci")
    created_at = datetime.now(UTC) - timedelta(hours=2)

    with pytest.raises(LicenseValidationError, match="expired"):
        validate_enterprise_license_for_install(
            pem,
            ca_pem=ca_pem,
            platform_created_at=created_at,
        )


def test_ci_license_whose_notafter_has_passed_is_granted_in_its_window():
    # OpenCTI's pattern for its public CI certificate: notAfter one day after
    # notBefore, long passed. The 45-minute window replaces it.
    now = datetime.now(UTC)
    pem, ca_pem = _make_license_pair(
        "ci",
        not_before=now - timedelta(days=26),
        not_after=now - timedelta(days=25),
    )

    info = decode_xtm_license_pem(pem, ca_pem, platform_created_at=now - timedelta(minutes=5))

    assert info.license_validated
    assert info.license_refusal_reason == ""


def test_refusal_reason_names_the_ci_window():
    created_at = datetime.now(UTC) - timedelta(hours=2)
    pem, ca_pem = _make_license_pair("ci")

    info = decode_xtm_license_pem(pem, ca_pem, platform_created_at=created_at)

    assert "45 minutes after the platform's creation" in info.license_refusal_reason
    assert created_at.isoformat() in info.license_refusal_reason


def test_refusal_reason_names_a_notbefore_to_come():
    now = datetime.now(UTC)
    pem, ca_pem = _make_license_pair("ci", not_before=now + timedelta(days=1))

    info = decode_xtm_license_pem(pem, ca_pem, platform_created_at=now)

    assert not info.license_validated
    assert info.license_refusal_reason.startswith("it is not valid before its notBefore")


def test_refusal_reason_names_a_foreign_signature():
    pem, _ = _make_license_pair("ci")
    _, other_ca_pem = _make_license_pair("ci")

    info = decode_xtm_license_pem(pem, other_ca_pem, platform_created_at=datetime.now(UTC))

    assert not info.license_valid_cert
    assert info.license_refusal_reason == ("its signature does not match the 'filigran xtm' certificate authority")


def test_refusal_reason_names_an_unreadable_pem():
    _, ca_pem = _make_license_pair("ci")

    info = decode_xtm_license_pem("not a certificate", ca_pem)

    assert info.license_refusal_reason.startswith("the certificate could not be read")


def test_a_refused_license_is_logged_with_its_reason(caplog):
    created_at = datetime.now(UTC) - timedelta(hours=2)
    pem, ca_pem = _make_license_pair("ci")
    clear_license_cache("xtm_refusal_log")

    with caplog.at_level("WARNING", logger="app.enterprise.licensing"):
        get_xtm_license_info(
            pem_from_env=pem,
            pem_from_db=None,
            ca_pem=ca_pem,
            platform_created_at=created_at,
            cache_key="xtm_refusal_log",
        )
    clear_license_cache("xtm_refusal_log")

    assert any(
        "The Filigran license from ENTERPRISE_LICENSE is refused: this ci license ended" in record.getMessage()
        for record in caplog.records
    )
