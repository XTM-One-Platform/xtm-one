"""Tests for server-side OpenCRQ platform license validation.

OpenCRQ registrations used to resolve to ``contract=CE`` no matter how valid
the forwarded ``enterprise_license_pem`` was, because ``PRODUCT_CA_CERTS``
had no ``"opencrq"`` entry. The OpenCRQ trust anchor is the bare product
signing PUBLIC KEY PEM (not a CA certificate): filigran-licenses signs every
license with the product keypair and its CA certificate is only a
self-signed wrapper around that key, so these tests pin both the public-key
trust-anchor mechanics in ``decode_license_pem`` and the ``opencrq`` wiring
in ``validate_platform_license``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import NameOID

from app.enterprise import platform_license_validator as plv
from app.enterprise.licensing import (
    LICENSE_OID_PRODUCT,
    LICENSE_OID_TYPE,
    decode_license_pem,
)
from app.enterprise.platform_license_validator import (
    PRODUCT_CA_CERTS,
    validate_platform_license,
)

_KEY_SIZE = 2048


def _make_signed_license(
    product: str = "opencrq",
    license_type: str = "standard",
    ou: str = "global",
) -> tuple[str, str]:
    """Return ``(client_pem, signer_public_key_pem)`` for a product license."""
    now = datetime.now(UTC)
    signer_key = rsa.generate_private_key(public_exponent=65537, key_size=_KEY_SIZE)
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Filigran CA CERT")])
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Test Customer"),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, ou),
            x509.NameAttribute(NameOID.COMMON_NAME, "test-license"),
        ]
    )
    client_cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(signer_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(
            x509.UnrecognizedExtension(LICENSE_OID_TYPE, license_type.encode()),
            critical=False,
        )
        .add_extension(
            x509.UnrecognizedExtension(LICENSE_OID_PRODUCT, product.encode()),
            critical=False,
        )
        .sign(signer_key, hashes.SHA256())
    )
    public_key_pem = signer_key.public_key().public_bytes(
        Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return client_cert.public_bytes(Encoding.PEM).decode(), public_key_pem.decode()


def test_opencrq_has_a_registered_trust_anchor():
    anchor, expected_products = PRODUCT_CA_CERTS["opencrq"]
    assert expected_products[0] == "opencrq"
    assert "-----BEGIN PUBLIC KEY-----" in anchor


def test_decode_license_pem_accepts_public_key_trust_anchor():
    pem, signer_public_key_pem = _make_signed_license()
    info = decode_license_pem(pem, signer_public_key_pem, "opencrq")
    assert info.license_valid_cert
    assert info.license_validated
    assert info.license_type == "standard"
    assert info.license_customer == "Test Customer"


def test_decode_license_pem_rejects_wrong_signer_public_key():
    pem, _ = _make_signed_license()
    _, other_public_key_pem = _make_signed_license()
    info = decode_license_pem(pem, other_public_key_pem, "opencrq")
    assert not info.license_valid_cert
    assert not info.license_validated


def test_validate_platform_license_accepts_opencrq_pem(monkeypatch):
    pem, signer_public_key_pem = _make_signed_license()
    monkeypatch.setitem(plv.PRODUCT_CA_CERTS, "opencrq", (signer_public_key_pem, ("opencrq",)))
    info = validate_platform_license("opencrq", pem)
    assert info.license_validated


def test_validate_platform_license_rejects_product_mismatch(monkeypatch):
    pem, signer_public_key_pem = _make_signed_license(product="opencti")
    monkeypatch.setitem(plv.PRODUCT_CA_CERTS, "opencrq", (signer_public_key_pem, ("opencrq",)))
    info = validate_platform_license("opencrq", pem)
    assert not info.license_validated


def test_legacy_opengrc_identifier_shares_the_opencrq_trust_anchor():
    """A licence issued before the OpenGRC -> OpenCRQ rename still validates.

    The rename did not rotate the signing keypair, so both entries pin the
    same anchor; only the product string baked into the certificate differs,
    and a pre-rename licence carries the old one.
    """
    legacy_anchor, legacy_products = PRODUCT_CA_CERTS["opengrc"]
    current_anchor, _ = PRODUCT_CA_CERTS["opencrq"]
    assert legacy_products == ("opengrc",)
    assert legacy_anchor == current_anchor


def test_validate_platform_license_accepts_legacy_opengrc_pem(monkeypatch):
    pem, signer_public_key_pem = _make_signed_license(product="opengrc")
    monkeypatch.setitem(
        plv.PRODUCT_CA_CERTS, "opengrc", (signer_public_key_pem, ("opengrc",))
    )
    info = validate_platform_license("opengrc", pem)
    assert info.license_validated


def test_renamed_product_string_is_not_accepted_under_the_legacy_identifier(
    monkeypatch,
):
    """The alias is not a wildcard: each identifier pins one product string."""
    pem, signer_public_key_pem = _make_signed_license(product="opencrq")
    monkeypatch.setitem(
        plv.PRODUCT_CA_CERTS, "opengrc", (signer_public_key_pem, ("opengrc",))
    )
    info = validate_platform_license("opengrc", pem)
    assert not info.license_validated


# ── Identifier normalisation ─────────────────────────────────────────────────
# A licence issued before the rename validates under ``opengrc`` (above), but
# nothing downstream should ever see that spelling: the identifier is
# normalised right after the licence check so the persisted registration,
# provider key and EE resolution all agree on ``opencrq``.


def test_legacy_identifier_normalises_to_the_canonical_one():
    from app.api.v1.platform import _canonical_platform_identifier

    assert _canonical_platform_identifier("opengrc") == "opencrq"


def test_normalisation_leaves_every_other_identifier_alone():
    from app.api.v1.platform import _canonical_platform_identifier

    for identifier in ("opencrq", "opencti", "openaev", "openbas", "unknown"):
        assert _canonical_platform_identifier(identifier) == identifier


def test_legacy_identifier_still_resolves_xtm_sublicensed_ids():
    """A row that predates both the migration and the normalisation."""
    from app.enterprise.licensing import XTMLicenseInfo
    from app.services.xtm_license import xtm_sublicensed_ids

    info = XTMLicenseInfo(opencrq_ids=["instance-1"])
    assert xtm_sublicensed_ids(info, "opencrq") == ["instance-1"]
    assert xtm_sublicensed_ids(info, "opengrc") == ["instance-1"]


# ── Product-OID tolerance ────────────────────────────────────────────────────
# A platform ships its new identifier as soon as it upgrades, but the licence
# in the customer's hands keeps the OID it was issued with. Without tolerance
# that window silently drops an EE platform to CE.


def test_real_opencrq_entry_accepts_both_product_oids():
    _, expected_products = PRODUCT_CA_CERTS["opencrq"]
    assert expected_products == ("opencrq", "opengrc")


def test_opencrq_identifier_accepts_a_pre_rename_licence(monkeypatch):
    pem, signer_public_key_pem = _make_signed_license(product="opengrc")
    monkeypatch.setitem(
        plv.PRODUCT_CA_CERTS,
        "opencrq",
        (signer_public_key_pem, ("opencrq", "opengrc")),
    )
    info = validate_platform_license("opencrq", pem)
    assert info.license_validated, (
        "a licence issued before the rename must still grant EE once the "
        "platform reports the new identifier"
    )


def test_oid_tolerance_does_not_weaken_the_trust_anchor(monkeypatch):
    """Accepting two OIDs must not accept two signers."""
    pem, _ = _make_signed_license(product="opengrc")
    _, other_public_key_pem = _make_signed_license()
    monkeypatch.setitem(
        plv.PRODUCT_CA_CERTS,
        "opencrq",
        (other_public_key_pem, ("opencrq", "opengrc")),
    )
    info = validate_platform_license("opencrq", pem)
    assert not info.license_validated


def test_an_unrelated_product_oid_is_still_rejected(monkeypatch):
    pem, signer_public_key_pem = _make_signed_license(product="opencti")
    monkeypatch.setitem(
        plv.PRODUCT_CA_CERTS,
        "opencrq",
        (signer_public_key_pem, ("opencrq", "opengrc")),
    )
    info = validate_platform_license("opencrq", pem)
    assert not info.license_validated


# ── Legacy-OID observability ─────────────────────────────────────────
# The legacy ``opengrc`` entry is a back-compat shim meant to be removed. It can
# only be removed on evidence, so a match on it is logged; and a total failure
# reports the canonical attempt, never the legacy one.

_VALIDATOR_LOGGER = plv.logger.name


def _validator_messages(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == _VALIDATOR_LOGGER]


def test_canonical_oid_validation_logs_nothing_extra(monkeypatch, caplog):
    pem, signer_public_key_pem = _make_signed_license(product="opencrq")
    monkeypatch.setitem(
        plv.PRODUCT_CA_CERTS,
        "opencrq",
        (signer_public_key_pem, ("opencrq", "opengrc")),
    )
    with caplog.at_level("INFO", logger=_VALIDATOR_LOGGER):
        info = validate_platform_license("opencrq", pem)
    assert info.license_validated
    assert _validator_messages(caplog) == []


def test_legacy_oid_validation_logs_one_info_naming_the_replacement(monkeypatch, caplog):
    pem, signer_public_key_pem = _make_signed_license(product="opengrc")
    monkeypatch.setitem(
        plv.PRODUCT_CA_CERTS,
        "opencrq",
        (signer_public_key_pem, ("opencrq", "opengrc")),
    )
    with caplog.at_level("INFO", logger=_VALIDATOR_LOGGER):
        info = validate_platform_license("opencrq", pem)
    assert info.license_validated
    messages = _validator_messages(caplog)
    assert len(messages) == 1, messages
    assert "'opengrc'" in messages[0] and "'opencrq'" in messages[0]
    assert all(r.levelname == "INFO" for r in caplog.records if r.name == _VALIDATOR_LOGGER)


def test_opengrc_identifier_with_its_own_oid_logs_no_deprecation(monkeypatch, caplog):
    """A platform that has not shipped the rename has one accepted OID: its
    licence is canonical for it, not a legacy hit."""
    pem, signer_public_key_pem = _make_signed_license(product="opengrc")
    monkeypatch.setitem(
        plv.PRODUCT_CA_CERTS, "opengrc", (signer_public_key_pem, ("opengrc",))
    )
    with caplog.at_level("INFO", logger=_VALIDATOR_LOGGER):
        info = validate_platform_license("opengrc", pem)
    assert info.license_validated
    assert _validator_messages(caplog) == []


def test_total_failure_returns_the_canonical_attempt(monkeypatch):
    """Every attempt fails: the result describes the canonical OID, not the
    legacy one tried last."""
    attempts: dict[str, object] = {}

    def _fake_decode(*, pem, ca_pem, expected_product):
        info = plv.LicenseInfo(license_platform=f"attempt-{expected_product}")
        attempts[expected_product] = info
        return info

    monkeypatch.setattr(plv, "decode_license_pem", _fake_decode)
    monkeypatch.setitem(
        plv.PRODUCT_CA_CERTS, "opencrq", ("anchor", ("opencrq", "opengrc"))
    )
    info = validate_platform_license("opencrq", "-----BEGIN CERTIFICATE-----")
    assert list(attempts) == ["opencrq", "opengrc"]
    assert info is attempts["opencrq"]
    assert not info.license_validated


def test_total_failure_on_a_real_pre_rename_licence_reports_the_canonical_mismatch(
    monkeypatch,
):
    """An expired trial issued as ``opengrc`` fails both attempts. The legacy
    attempt matched its product (it failed on expiry); the canonical one did
    not - and the canonical one is what comes back."""
    now = datetime.now(UTC)
    signer_key = rsa.generate_private_key(public_exponent=65537, key_size=_KEY_SIZE)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-license")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(signer_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=30))
        .not_valid_after(now - timedelta(days=1))
        .add_extension(x509.UnrecognizedExtension(LICENSE_OID_TYPE, b"trial"), critical=False)
        .add_extension(x509.UnrecognizedExtension(LICENSE_OID_PRODUCT, b"opengrc"), critical=False)
        .sign(signer_key, hashes.SHA256())
    )
    pem = cert.public_bytes(Encoding.PEM).decode()
    anchor = signer_key.public_key().public_bytes(
        Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    monkeypatch.setitem(plv.PRODUCT_CA_CERTS, "opencrq", (anchor, ("opencrq", "opengrc")))

    info = validate_platform_license("opencrq", pem)
    assert not info.license_validated
    assert info.license_platform_match is False
