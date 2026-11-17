"""A license whose subject holds non-ASCII text tagged PrintableString keeps validating.

node-forge (filigran-licenses) writes a name's non-ASCII characters - the ``ö``
of ``Försvarsmakten`` - as raw bytes in a PrintableString, which only allows a
restricted ASCII set. OpenCTI reads that; ``cryptography`` 50 refuses it, so
XTM One saw the OpenCTI Enterprise license as invalid and stayed Community
Edition. No simulation here: cryptography itself refuses these certificates.

These tests pin that the license still validates through the fallback in
``app/enterprise/license_certificate.py``, with the customer decoded, that the
signature is still verified over the issued octets, and that a certificate
cryptography refuses for anything else stays refused.
"""

from __future__ import annotations

import functools
import warnings

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import NameOID

from app.enterprise import license_certificate
from app.enterprise.license_certificate import TolerantLicenseCertificate, load_license_certificate
from app.enterprise.licensing import (
    LicenseValidationError,
    decode_xtm_license_pem,
    validate_enterprise_license_for_install,
)
from tests.test_license_serial_zero import (
    _contents,
    _der,
    _elements,
    _key,
    _oid,
    _pem,
    _serial_zero_ca,
    _serial_zero_license,
    _sign,
    _tbs_elements,
    _tlv,
)

_PRINTABLE_STRING = 0x13
_UTF8_STRING = 0x0C
_ORGANIZATION = "2.5.4.10"
_ORGANIZATIONAL_UNIT = "2.5.4.11"
_COMMON_NAME = "2.5.4.3"
_COUNTRY = "2.5.4.6"

CUSTOMER = "Försvarsmakten"
LATIN_1 = CUSTOMER.encode("latin-1")
UTF_8 = CUSTOMER.encode("utf-8")


def _name(*attributes: tuple[str, int, bytes]) -> bytes:
    """A Name of one single-valued RDN per ``(oid, string tag, raw value)``."""
    return _tlv(0x30, b"".join(_tlv(0x31, _tlv(0x30, _oid(oid) + _tlv(tag, raw))) for oid, tag, raw in attributes))


@functools.cache
def _issued_elements() -> tuple[bytes, ...]:
    """The test license's TBS elements, issued once: its validity is stamped from the clock to the second, so the
    licenses a test compares byte for byte are all built from the same ones."""
    return tuple(_tbs_elements(x509.load_der_x509_certificate(_der(_serial_zero_license(serial_zero=False)))))


def _license(
    organization: bytes = LATIN_1,
    *,
    tag: int = _PRINTABLE_STRING,
    platform: bytes = b"global",
    serial: bytes = b"\x00",
    issuer: bytes | None = None,
    signer: str = "ca",
) -> bytes:
    """The DER of a license issued the way node-forge writes it, signed by the test CA."""
    elements = list(_issued_elements())
    elements[1] = _tlv(0x02, serial)
    if issuer is not None:
        elements[3] = issuer
    elements[5] = _name(
        (_ORGANIZATION, tag, organization),
        (_ORGANIZATIONAL_UNIT, _UTF8_STRING, platform),
        (_COMMON_NAME, _UTF8_STRING, b"test-license"),
    )
    return _sign(elements, _key(signer))


def _refused_by_cryptography(der: bytes) -> None:
    with pytest.raises(ValueError, match=r"PrintableString|subject|issuer"):
        x509.load_der_x509_certificate(der)


@pytest.mark.parametrize(
    ("organization", "customer"),
    [
        pytest.param(LATIN_1, CUSTOMER, id="latin-1"),
        pytest.param(UTF_8, CUSTOMER, id="utf-8"),
        pytest.param(b"Acme & Co @ Nordic", "Acme & Co @ Nordic", id="ascii-outside-the-set"),
    ],
)
@pytest.mark.parametrize("serial", [b"\x00", b"\x01"], ids=["serial-0", "positive-serial"])
def test_a_license_with_an_invalid_printable_string_subject_validates(organization, customer, serial):
    ca_pem = _serial_zero_ca()
    der = _license(organization, serial=serial)
    pem = _pem(der)
    _refused_by_cryptography(der)

    assert isinstance(load_license_certificate(pem), TolerantLicenseCertificate)
    info = decode_xtm_license_pem(pem, ca_pem)
    installed = validate_enterprise_license_for_install(pem, ca_pem=ca_pem)

    assert info.license_valid_cert and info.license_validated and installed.license_validated
    assert (info.license_customer, info.license_platform, info.license_type) == (customer, "global", "standard")
    # The certificate handed to registering platforms is the one issued, byte for byte.
    assert info.license_certificate_pem is not None
    assert _der(info.license_certificate_pem) == der


def test_the_decoded_subject_is_what_cryptography_reads_from_a_utf8_string():
    fallback = load_license_certificate(_pem(_license(LATIN_1)))
    proper = x509.load_der_x509_certificate(_license(UTF_8, tag=_UTF8_STRING))

    assert isinstance(fallback, TolerantLicenseCertificate)
    assert fallback.subject == proper.subject
    assert [(a.oid, a.value) for a in fallback.subject] == [
        (NameOID.ORGANIZATION_NAME, CUSTOMER),
        (NameOID.ORGANIZATIONAL_UNIT_NAME, "global"),
        (NameOID.COMMON_NAME, "test-license"),
    ]
    # Everything else is read from the same twin as for any fallback certificate.
    assert fallback.not_valid_after_utc == proper.not_valid_after_utc
    assert fallback.public_key().public_numbers() == proper.public_key().public_numbers()
    assert fallback.tbs_certificate_bytes == _elements(_contents(_license(LATIN_1)))[0]
    assert fallback.public_bytes(Encoding.DER) == _license(LATIN_1)


def test_an_invalid_printable_string_in_the_organizational_unit_is_decoded_too():
    ca_pem = _serial_zero_ca()
    elements = _tbs_elements(x509.load_der_x509_certificate(_der(_serial_zero_license(serial_zero=False))))
    elements[1] = _tlv(0x02, b"\x00")
    elements[5] = _name(
        (_ORGANIZATION, _UTF8_STRING, b"Test Customer"),
        (_ORGANIZATIONAL_UNIT, _PRINTABLE_STRING, "plätform".encode("latin-1")),
    )
    pem = _pem(_sign(elements, _key("ca")))

    info = decode_xtm_license_pem(pem, ca_pem, platform_id="plätform")

    assert info.license_validated
    assert (info.license_customer, info.license_platform) == ("Test Customer", "plätform")
    # Bound to another platform, it is not the license of this one.
    assert not decode_xtm_license_pem(pem, ca_pem, platform_id="another").license_validated


def test_an_invalid_printable_string_in_the_issuer_is_tolerated():
    ca_pem = _serial_zero_ca()
    issuer = _name((_COMMON_NAME, _PRINTABLE_STRING, b"Filigran CA CERT"), (_ORGANIZATION, _PRINTABLE_STRING, LATIN_1))
    der = _license(issuer=issuer)
    _refused_by_cryptography(der)

    certificate = load_license_certificate(_pem(der))

    assert isinstance(certificate, TolerantLicenseCertificate)
    assert decode_xtm_license_pem(_pem(der), ca_pem).license_validated


def test_a_tampered_license_with_an_invalid_printable_string_is_still_rejected():
    ca_pem = _serial_zero_ca()
    der = _license()
    assert der.count(LATIN_1) == 1
    forged = {
        # Same length: only the signature can tell.
        "customer": _pem(der.replace(LATIN_1, "Försvarsmakter".encode("latin-1"))),
        "signature": _pem(der[:-1] + bytes([der[-1] ^ 0x01])),
        "signer": _pem(_license(signer="attacker")),
    }

    for what, pem in forged.items():
        assert isinstance(load_license_certificate(pem), TolerantLicenseCertificate), what
        info = decode_xtm_license_pem(pem, ca_pem)
        assert not info.license_valid_cert, what
        assert not info.license_validated, what
        with pytest.raises(LicenseValidationError, match="signature does not match"):
            validate_enterprise_license_for_install(pem, ca_pem=ca_pem)


def test_a_truncated_certificate_stays_refused():
    with pytest.raises(ValueError):
        load_license_certificate(_pem(_license()[:-1]))


def test_only_a_printable_string_is_rescued():
    # The same bytes in a UTF8String: cryptography loads the certificate but cannot read its subject,
    # and the fallback is not asked.
    pem = _pem(_license(LATIN_1, tag=_UTF8_STRING))

    assert isinstance(load_license_certificate(pem), x509.Certificate)
    assert not decode_xtm_license_pem(pem, _serial_zero_ca()).license_validated


def test_a_malformed_issuer_name_is_not_rescued():
    # An attribute with no value: the walk refuses it before cryptography is asked.
    der = _license(issuer=_tlv(0x30, _tlv(0x31, _tlv(0x30, _oid(_COMMON_NAME)))))

    with pytest.raises(ValueError):
        load_license_certificate(_pem(der))


def test_a_valid_license_is_still_loaded_by_cryptography_itself():
    pem = _serial_zero_license(serial_zero=False, customer=CUSTOMER)

    assert isinstance(load_license_certificate(pem), x509.Certificate)


# ── Edge cases of the decoding ───────────────────────────────────────


def test_invalid_printable_strings_in_two_attributes_of_one_subject_are_both_decoded():
    ca_pem = _serial_zero_ca()
    elements = list(_issued_elements())
    elements[1] = _tlv(0x02, b"\x00")
    elements[5] = _name(
        (_ORGANIZATION, _PRINTABLE_STRING, LATIN_1),
        (_ORGANIZATIONAL_UNIT, _PRINTABLE_STRING, "plätform".encode()),
        (_COMMON_NAME, _UTF8_STRING, b"test-license"),
    )
    der = _sign(elements, _key("ca"))
    _refused_by_cryptography(der)

    certificate = load_license_certificate(_pem(der))
    info = decode_xtm_license_pem(_pem(der), ca_pem, platform_id="plätform")

    assert isinstance(certificate, TolerantLicenseCertificate)
    assert [(a.oid, a.value) for a in certificate.subject] == [
        (NameOID.ORGANIZATION_NAME, CUSTOMER),
        (NameOID.ORGANIZATIONAL_UNIT_NAME, "plätform"),
        (NameOID.COMMON_NAME, "test-license"),
    ]
    assert info.license_validated
    assert (info.license_customer, info.license_platform) == (CUSTOMER, "plätform")


def _country_license(country: bytes) -> bytes:
    """A positive-serial license whose country is ``country`` tagged PrintableString."""
    elements = list(_issued_elements())
    elements[1] = _tlv(0x02, b"\x01")
    elements[5] = _name(
        (_COUNTRY, _PRINTABLE_STRING, country),
        (_ORGANIZATION, _UTF8_STRING, b"Test Customer"),
        (_ORGANIZATIONAL_UNIT, _UTF8_STRING, b"global"),
    )
    return _sign(elements, _key("ca"))


@pytest.mark.parametrize(
    "country", [pytest.param("SVÖ".encode("latin-1"), id="latin-1"), pytest.param("SVÖ".encode(), id="utf-8")]
)
def test_a_decoded_value_x509_refuses_keeps_the_certificate_refused(country):
    """A country of three characters (four octets of UTF-8): ``x509.NameAttribute`` refuses it, so the certificate
    stays refused."""
    der = _country_license(country)
    _refused_by_cryptography(der)

    with pytest.raises(ValueError, match="PrintableString"):
        load_license_certificate(_pem(der))
    assert not decode_xtm_license_pem(_pem(der), _serial_zero_ca()).license_valid_cert


def test_a_twin_subject_cryptography_cannot_read_keeps_the_original_refusal():
    """The "?" placeholder of a one-octet country makes cryptography warn when the twin's subject is read; with
    warnings turned into errors that read raises, and the loader still raises cryptography's own refusal."""
    der = _country_license("Ö".encode("latin-1"))
    _refused_by_cryptography(der)

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with pytest.raises(ValueError, match="PrintableString"):
            load_license_certificate(_pem(der))


def test_latin_1_bytes_that_are_also_valid_utf_8_are_read_as_utf_8():
    """The order is UTF-8 first, then Latin-1: a Latin-1 ``\u00c3\u00a9`` holds the very octets of a UTF-8 ``é``."""
    organization = "Acme \u00c3\u00a9".encode("latin-1")
    assert organization == "Acme é".encode()

    info = decode_xtm_license_pem(_pem(_license(organization)), _serial_zero_ca())

    assert info.license_validated
    assert info.license_customer == "Acme é"


def test_a_non_minimal_positive_serial_with_an_invalid_printable_string_stays_refused():
    """Only a non-positive serial is patched: a positive one stays as issued and cryptography judges its encoding."""
    der = _license(serial=b"\x00\x01")
    layout = license_certificate._certificate_layout(der)
    # The walk alone would take it: a positive serial and a string to decode.
    assert der[layout.serial] == b"\x00\x01"
    assert len(license_certificate._invalid_printable_strings(der, layout)) == 1

    with pytest.raises(ValueError, match="serial"):
        load_license_certificate(_pem(der))
    assert not decode_xtm_license_pem(_pem(der), _serial_zero_ca()).license_valid_cert
