"""License certificates carrying serial number 0 keep validating once cryptography refuses them.

filigran-licenses (node-forge) issues every license and every product CA
certificate with serial number 0, and cryptography 50 warns on each load that a
future release will raise instead. These tests simulate that release two ways -
the deprecation warning turned into an error, and cryptography's loaders
monkeypatched to refuse a non-positive serial - on serial-0 certificates signed
by an in-memory test CA (plus the real, public, embedded CA certificates), and
pin that:

* every license check still validates through the fallback in
  ``app/enterprise/license_certificate.py``: Filigran licenses, product licenses,
  the embedded trust anchors, support codes, the product read for Filigran AI,
  for serial 0 and for negative serials of every length;
* the signature is still verified - a tampered certificate is still rejected -
  and every field is read from octets the signature covers: no restructured,
  re-encoded, re-framed or mutated certificate validates as another license;
* the fallback is taken for the known defects only - here a minimally encoded
  non-positive serial, the other being the invalid PrintableString name
  strings pinned in ``test_license_printable_string_subject.py`` - on a
  certificate signed the way filigran-licenses signs (RSA PKCS#1 v1.5, SHA-2,
  the same algorithm inside and outside the TBS), and reads exactly what
  cryptography reads;
* today's path is untouched: cryptography still loads these certificates
  itself, the fallback rescues nothing cryptography refuses today, and its
  serial-number warning is silenced for the loader only.
"""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import functools
import json
import random
import re
import uuid
import warnings
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
from cryptography.utils import CryptographyDeprecationWarning
from cryptography.x509.name import _ASN1Type
from cryptography.x509.oid import NameOID

from app.enterprise import license_certificate, licensing
from app.enterprise import platform_license_validator as plv
from app.enterprise.license_certificate import (
    MAX_LICENSE_PEM_CHARS,
    TolerantLicenseCertificate,
    load_license_certificate,
)
from app.enterprise.licensing import (
    LICENSE_OID_CREATOR,
    LICENSE_OID_DAILY_QUOTA,
    LICENSE_OID_MONTHLY_QUOTA,
    LICENSE_OID_OPENCTI_IDS,
    LICENSE_OID_YEARLY_QUOTA,
    LicenseInfo,
    LicenseValidationError,
    XTMLicenseInfo,
    _get_extension_value,
    _load_trust_anchor_public_key,
    clear_license_cache,
    decode_xtm_license_pem,
    extract_license_product,
    get_license_info,
    get_xtm_license_info,
    validate_enterprise_license_for_install,
)
from app.enterprise.platform_license_validator import OPENAEV_CA_PEM, OPENCTI_CA_PEM, validate_platform_license
from app.enterprise.xtm_ca import XTM_CA_PEM
from app.services.agent_failure_pool import POOL_RECHARGE_KIND, SUPPORT_CODE_PREFIX, verify_support_code

APP = Path(__file__).resolve().parent.parent / "app"

# cryptography 50.0.0, word for word.
_SERIAL_WARNING = (
    "Parsed a serial number which wasn't positive (i.e., it was negative or zero), which is "
    "disallowed by RFC 5280. Loading this certificate will cause an exception in a future "
    "release of cryptography."
)

# Every Filigran license OID the license checks read, standard and legacy.
_FILIGRAN_OIDS = [value for name, value in vars(licensing).items() if name.startswith("LICENSE_") and "_OID_" in name]


@pytest.fixture(autouse=True)
def _loader_warning_filter_as_in_production() -> Iterator[None]:
    """pytest reverts the filters a module installs while it is collected; restore the loader's one."""
    with warnings.catch_warnings():
        license_certificate._ignore_serial_number_warning()
        yield


# ── DER, for the certificates CertificateBuilder refuses to build ───────────


def _tlv(tag: int, contents: bytes) -> bytes:
    """A DER element with a minimal definite length."""
    size = len(contents)
    if size < 0x80:
        return bytes([tag, size]) + contents
    octets = size.to_bytes((size.bit_length() + 7) // 8, "big")
    return bytes([tag, 0x80 | len(octets)]) + octets + contents


def _long_form(tag: int, contents: bytes, octets: int) -> bytes:
    """A BER element whose length takes ``octets`` octets: non-minimal whenever fewer would do."""
    return bytes([tag, 0x80 | octets]) + len(contents).to_bytes(octets, "big") + contents


def _contents(element: bytes) -> bytes:
    """The contents of one well-formed DER element."""
    return element[2 + (element[1] & 0x7F if element[1] & 0x80 else 0) :]


def _elements(contents: bytes) -> list[bytes]:
    """The consecutive well-formed DER elements of a constructed element's contents."""
    elements = []
    while contents:
        count = contents[1] & 0x7F if contents[1] & 0x80 else 0
        length = int.from_bytes(contents[2 : 2 + count], "big") if count else contents[1]
        elements.append(contents[: 2 + count + length])
        contents = contents[2 + count + length :]
    return elements


def _oid(dotted: str) -> bytes:
    """The DER of an OBJECT IDENTIFIER."""
    arcs = [int(arc) for arc in dotted.split(".")]
    encoded = b""
    for arc in [40 * arcs[0] + arcs[1], *arcs[2:]]:
        septets = [arc & 0x7F]
        while arc > 0x7F:
            arc >>= 7
            septets.append(0x80 | (arc & 0x7F))
        encoded += bytes(reversed(septets))
    return _tlv(0x06, encoded)


def _extension(oid: x509.ObjectIdentifier | str, value: bytes, *, critical: bytes = b"") -> bytes:
    dotted = oid if isinstance(oid, str) else oid.dotted_string
    return _tlv(0x30, _oid(dotted) + critical + _tlv(0x04, value))


def _rsa_algorithm(arc: int, *, parameters: bytes = b"\x05\x00") -> bytes:
    """The AlgorithmIdentifier of 1.2.840.113549.1.1.<arc>: RSA PKCS#1 v1.5, 4 MD5, 5 SHA-1, 11-14 SHA-2."""
    return _tlv(0x30, _oid(f"1.2.840.113549.1.1.{arc}") + parameters)


def _tbs_elements(certificate: x509.Certificate) -> list[bytes]:
    """version, serialNumber, signature, issuer, validity, subject, subjectPublicKeyInfo[, extensions]."""
    return _elements(_contents(certificate.tbs_certificate_bytes))


def _sign(
    tbs_elements: list[bytes],
    signer: rsa.RSAPrivateKey,
    *,
    hash_algorithm: hashes.HashAlgorithm | None = None,
    outer_algorithm: bytes | None = None,
) -> bytes:
    """The DER of the certificate ``tbs_elements`` make up, signed by ``signer``.

    The signatureAlgorithm outside the TBS repeats the one inside unless ``outer_algorithm`` says otherwise.
    """
    tbs = _tlv(0x30, b"".join(tbs_elements))
    signature = signer.sign(tbs, padding.PKCS1v15(), hash_algorithm or hashes.SHA256())
    return _tlv(0x30, tbs + (outer_algorithm or tbs_elements[2]) + _tlv(0x03, b"\x00" + signature))


# ── Serial-0 certificates, issued the way filigran-licenses issues them ─────


@functools.cache
def _key(role: str) -> rsa.RSAPrivateKey:
    """One throwaway RSA key per role for the whole module: generating keys dominates the runtime."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _reissued(
    certificate: x509.Certificate,
    signer: rsa.RSAPrivateKey,
    *,
    version: int = 2,
    serial: bytes = b"\x00",
    algorithm: bytes | None = None,
    hash_algorithm: hashes.HashAlgorithm | None = None,
) -> bytes:
    """The DER of ``certificate`` re-issued with the serial whose content octets are ``serial``.

    ``CertificateBuilder`` refuses a non-positive serial (and SHA-1), so the
    certificate is built with serial 1 and its TBS rewritten and signed again
    with the same key. ``version`` rewrites the version (2 is v3), ``algorithm``
    both signature AlgorithmIdentifiers, signed with ``hash_algorithm``.
    """
    elements = _tbs_elements(certificate)
    elements[0] = _tlv(0xA0, _tlv(0x02, bytes([version])))
    elements[1] = _tlv(0x02, serial)
    if algorithm is not None:
        elements[2] = algorithm
    return _sign(elements, signer, hash_algorithm=hash_algorithm)


def _pem(der: bytes) -> str:
    body = base64.b64encode(der).decode()
    lines = "\n".join(body[index : index + 64] for index in range(0, len(body), 64))
    return f"-----BEGIN CERTIFICATE-----\n{lines}\n-----END CERTIFICATE-----\n"


def _der(pem: str) -> bytes:
    return base64.b64decode("".join(pem.strip().splitlines()[1:-1]))


_CA_NAME = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Filigran CA CERT")])


def _serial_zero_ca() -> str:
    """A self-signed product CA with serial number 0, like the ones XTM One embeds."""
    key = _key("ca")
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(_CA_NAME)
        .issuer_name(_CA_NAME)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=False)
        .sign(key, hashes.SHA256())
    )
    return _pem(_reissued(certificate, key))


def _serial_zero_license(
    product: str = "filigran xtm",
    *,
    customer: str = "Test Customer",
    extensions: dict[x509.ObjectIdentifier, bytes] | None = None,
    signer: str = "ca",
    version: int = 2,
    serial_zero: bool = True,
    serial: bytes = b"\x00",
    algorithm: bytes | None = None,
    hash_algorithm: hashes.HashAlgorithm | None = None,
) -> str:
    """A ``standard`` license for the ``global`` platform, issued by the ``signer`` CA."""
    now = datetime.now(UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name(
                [
                    x509.NameAttribute(NameOID.ORGANIZATION_NAME, customer),
                    x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "global"),
                    x509.NameAttribute(NameOID.COMMON_NAME, "test-license"),
                ]
            )
        )
        .issuer_name(_CA_NAME)
        .public_key(_key("license").public_key())
        .serial_number(1)
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=365))
    )
    values = {licensing.LICENSE_OID_TYPE: b"standard", licensing.LICENSE_OID_PRODUCT: product.encode()}
    for oid, value in {**values, **(extensions or {})}.items():
        builder = builder.add_extension(x509.UnrecognizedExtension(oid, value), critical=False)
    certificate = builder.sign(_key(signer), hashes.SHA256())
    if not serial_zero:
        return certificate.public_bytes(Encoding.PEM).decode()
    return _pem(
        _reissued(
            certificate,
            _key(signer),
            version=version,
            serial=serial,
            algorithm=algorithm,
            hash_algorithm=hash_algorithm,
        )
    )


def _edited_license(edit: Callable[[list[bytes]], None], **license_options: object) -> bytes:
    """The DER of a serial-0 license whose TBS elements ``edit`` rewrote in place, signed again by the test CA."""
    elements = _tbs_elements(
        x509.load_der_x509_certificate(_der(_serial_zero_license(serial_zero=False, **license_options)))
    )
    elements[1] = _tlv(0x02, b"\x00")
    edit(elements)
    return _sign(elements, _key("ca"))


def _add_extensions(*extensions: bytes) -> Callable[[list[bytes]], None]:
    def edit(elements: list[bytes]) -> None:
        present = _elements(_contents(_contents(elements[-1])))
        elements[-1] = _tlv(0xA3, _tlv(0x30, b"".join([*present, *extensions])))

    return edit


# ── The refusing cryptography release, simulated ────────────────────────────


@contextlib.contextmanager
def _warning_is_an_error() -> Iterator[None]:
    """The cheap simulation: today's deprecation warning raised instead of emitted."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", CryptographyDeprecationWarning)
        yield


def _refusing_non_positive_serials(load: Callable[..., x509.Certificate]) -> Callable[..., x509.Certificate]:
    def refuse(data: bytes, *args: object, **kwargs: object) -> x509.Certificate:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", CryptographyDeprecationWarning)
            certificate = load(data, *args, **kwargs)
            serial_number = certificate.serial_number
        if serial_number <= 0:
            raise ValueError("error parsing asn1 value: serial number is not positive (simulated)")
        return certificate

    return refuse


@contextlib.contextmanager
def _loaders_refuse_serial_zero() -> Iterator[None]:
    """The literal simulation: cryptography's certificate loaders raise on a non-positive serial."""
    with pytest.MonkeyPatch.context() as patch:
        for name in ("load_pem_x509_certificate", "load_der_x509_certificate"):
            patch.setattr(x509, name, _refusing_non_positive_serials(getattr(x509, name)))
        yield


_REFUSING_RELEASES = {
    "warning-is-an-error": _warning_is_an_error,
    "loaders-refuse-serial-zero": _loaders_refuse_serial_zero,
}

# What either simulated release raises for a non-positive serial: the refusal the
# loader re-raises when the fallback does not take the certificate.
_SERIAL_REFUSAL = (CryptographyDeprecationWarning, ValueError)


@pytest.fixture(params=sorted(_REFUSING_RELEASES))
def refusing_cryptography(request: pytest.FixtureRequest) -> Callable[[], contextlib.AbstractContextManager[None]]:
    """``with refusing_cryptography(): ...`` runs its body against the simulated release."""
    return _REFUSING_RELEASES[request.param]


def _cryptography_today(pem: str) -> x509.Certificate | Exception:
    """What cryptography alone makes of ``pem`` today."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", CryptographyDeprecationWarning)
        try:
            return x509.load_pem_x509_certificate(pem.encode())
        except Exception as exc:
            return exc


def _assert_loaded_as_cryptography_loads_it_today(pem: str) -> None:
    """Today the loader returns what cryptography returns, and refuses what it refuses: the fallback rescues nothing."""
    expected = _cryptography_today(pem)
    if isinstance(expected, Exception):
        with pytest.raises(type(expected)):
            load_license_certificate(pem)
    else:
        loaded = load_license_certificate(pem)
        assert isinstance(loaded, x509.Certificate)
        assert loaded.public_bytes(Encoding.DER) == expected.public_bytes(Encoding.DER)


def _decisions(info: LicenseInfo) -> tuple[object, ...]:
    """Everything a license decides or displays, from a validated ``XTMLicenseInfo``."""
    assert isinstance(info, XTMLicenseInfo)
    return (
        info.license_customer,
        info.license_platform,
        info.license_type,
        info.license_creator,
        info.license_platform_match,
        info.license_start_date,
        info.license_expiration_date,
        info.opencti_ids,
        info.openaev_ids,
        info.opencrq_ids,
        info.daily_quota,
        info.monthly_quota,
        info.yearly_quota,
        info.copilot_enabled,
        info.agentic_ai_enabled,
    )


# ── Today: cryptography still carries every load ────────────────────────────


def test_today_cryptography_still_loads_serial_zero_certificates_itself():
    """No fallback while cryptography loads these certificates: callers get the object they always got."""
    for pem in (_serial_zero_license(), _serial_zero_ca(), XTM_CA_PEM, OPENCTI_CA_PEM, OPENAEV_CA_PEM):
        assert isinstance(load_license_certificate(pem), x509.Certificate)


def test_the_serial_warning_is_silenced_for_the_loader_and_nothing_else():
    with warnings.catch_warnings(record=True) as caught:
        warnings.resetwarnings()
        license_certificate._ignore_serial_number_warning()  # what the module installs at import
        load_license_certificate(XTM_CA_PEM)
        assert caught == []
        # Any other caller still hears it...
        x509.load_pem_x509_certificate(XTM_CA_PEM.encode())
        # ...and any other deprecation attributed to the loader still surfaces.
        warnings.warn_explicit(
            "another deprecation",
            CryptographyDeprecationWarning,
            "license_certificate.py",
            1,
            module=license_certificate.__name__,
        )
    assert [str(warning.message) for warning in caught] == [_SERIAL_WARNING, "another deprecation"]


# ── Tomorrow: the tripwire ───────────────────────────────────────────────────


def test_a_serial_zero_xtm_license_still_validates_when_cryptography_refuses_it(refusing_cryptography):
    ca_pem = _serial_zero_ca()
    pem = _serial_zero_license(
        extensions={
            LICENSE_OID_CREATOR: b"Filigran",
            LICENSE_OID_DAILY_QUOTA: b"500",
            LICENSE_OID_MONTHLY_QUOTA: b"5000",
            LICENSE_OID_YEARLY_QUOTA: b"50000",
            LICENSE_OID_OPENCTI_IDS: json.dumps(["opencti-1"]).encode(),
        }
    )
    today = decode_xtm_license_pem(pem, ca_pem)

    with refusing_cryptography():
        # Both certificates really go through the fallback...
        assert isinstance(load_license_certificate(pem), TolerantLicenseCertificate)
        assert isinstance(load_license_certificate(ca_pem), TolerantLicenseCertificate)
        future = decode_xtm_license_pem(pem, ca_pem)
        installed = validate_enterprise_license_for_install(pem, ca_pem=ca_pem)

    # ...and the license comes out exactly as it does today.
    assert future.license_valid_cert and future.license_validated and installed.license_validated
    assert (future.license_customer, future.license_platform, future.license_type) == (
        "Test Customer",
        "global",
        "standard",
    )
    assert (future.daily_quota, future.monthly_quota, future.yearly_quota) == (500, 5000, 50000)
    assert future.opencti_ids == ["opencti-1"]
    assert future.license_creator == "Filigran"
    # The certificate handed to registering platforms is the one issued, byte for byte.
    assert future.license_certificate_pem is not None
    assert dataclasses.asdict(future) == dataclasses.asdict(today)


_NEGATIVE_SERIALS = {
    "-1": "ff",
    "-128": "80",
    "-129": "ff7f",
    "-32768": "8000",
    "8-octets": "ff00000000000000",
    "20-octets": "80" + "00" * 19,
}


@pytest.mark.parametrize("serial", sorted(_NEGATIVE_SERIALS))
def test_a_negative_serial_license_still_validates_when_cryptography_refuses_it(serial, refusing_cryptography):
    """The other half of the defect: a negative serial, one DER octet or twenty, replaced by 01 00 .. of its length."""
    contents = bytes.fromhex(_NEGATIVE_SERIALS[serial])
    ca_pem = _serial_zero_ca()
    pem = _serial_zero_license(serial=contents, extensions={LICENSE_OID_DAILY_QUOTA: b"500"})
    today = decode_xtm_license_pem(pem, ca_pem)

    with refusing_cryptography():
        certificate = load_license_certificate(pem)
        future = decode_xtm_license_pem(pem, ca_pem)

    assert isinstance(certificate, TolerantLicenseCertificate)
    assert certificate.serial_number == int.from_bytes(contents, "big", signed=True) < 0
    assert certificate._twin.serial_number == int.from_bytes(b"\x01" + bytes(len(contents) - 1), "big")
    assert certificate.public_bytes(Encoding.DER) == _der(pem)
    assert future.license_validated and future.daily_quota == 500
    assert dataclasses.asdict(future) == dataclasses.asdict(today)


def test_only_the_certificate_travels_when_the_fallback_reads_it(refusing_cryptography):
    """Whatever surrounds the certificate in the stored PEM - a pasted private key - stays behind."""
    ca_pem = _serial_zero_ca()
    pem = _serial_zero_license()
    private_key = _key("license").private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    stored = f"Pasted by an admin:\n{private_key}{pem}trailing notes\n"

    with refusing_cryptography():
        assert isinstance(load_license_certificate(stored), TolerantLicenseCertificate)
        info = decode_xtm_license_pem(stored, ca_pem)

    assert info.license_validated
    assert info.license_certificate_pem == pem
    assert "PRIVATE KEY" not in info.license_certificate_pem


def test_the_cached_license_resolution_works_through_the_fallback(refusing_cryptography):
    ca_pem = _serial_zero_ca()
    xtm_pem = _serial_zero_license()
    copilot_pem = _serial_zero_license("copilot")
    keys = ("serial-zero-xtm", "serial-zero-copilot")
    for key in keys:
        clear_license_cache(key)
    try:
        with refusing_cryptography():
            assert get_xtm_license_info(xtm_pem, None, ca_pem, cache_key=keys[0]).license_validated
            assert get_license_info("", copilot_pem, ca_pem, "copilot", cache_key=keys[1]).license_validated
    finally:
        for key in keys:
            clear_license_cache(key)


def test_a_tampered_serial_zero_license_is_still_rejected(refusing_cryptography):
    ca_pem = _serial_zero_ca()
    der = _der(_serial_zero_license())
    assert der.count(b"Test Customer") == 1
    forged = {
        # Same length: only the signature can tell.
        "customer": _pem(der.replace(b"Test Customer", b"Evil Customer")),
        "signature": _pem(der[:-1] + bytes([der[-1] ^ 0x01])),
        "signer": _serial_zero_license(signer="attacker"),
    }

    with refusing_cryptography():
        for what, pem in forged.items():
            # It still parses...
            assert isinstance(load_license_certificate(pem), TolerantLicenseCertificate), what
            # ...but its signature does not verify against the trust anchor.
            info = decode_xtm_license_pem(pem, ca_pem)
            assert not info.license_valid_cert, what
            assert not info.license_validated, what
            assert info.license_certificate_pem is None, what
            with pytest.raises(LicenseValidationError, match="signature does not match"):
                validate_enterprise_license_for_install(pem, ca_pem=ca_pem)


def test_a_serial_zero_platform_license_still_validates(refusing_cryptography, monkeypatch):
    ca_pem = _serial_zero_ca()
    monkeypatch.setitem(plv.PRODUCT_CA_CERTS, "opencti", (ca_pem, ("opencti",)))
    monkeypatch.setitem(plv.PRODUCT_CA_CERTS, "openaev", (ca_pem, ("openaev",)))
    opencti_pem = _serial_zero_license("opencti")
    # OpenAEV sends its license base64-encoded.
    openaev_pem = base64.b64encode(_serial_zero_license("openaev").encode()).decode()
    forged = _pem(_der(opencti_pem).replace(b"Test Customer", b"Evil Customer"))

    with refusing_cryptography():
        assert validate_platform_license("opencti", opencti_pem).license_validated
        assert validate_platform_license("openaev", openaev_pem).license_validated
        assert not validate_platform_license("opencti", forged).license_validated


def test_the_product_of_a_serial_zero_license_is_still_read(refusing_cryptography):
    pem = _serial_zero_license("opencti")
    with refusing_cryptography():
        assert extract_license_product(pem) == "opencti"


_EMBEDDED_TRUST_ANCHORS = {"xtm": XTM_CA_PEM, "opencti": OPENCTI_CA_PEM, "openaev": OPENAEV_CA_PEM}


@pytest.mark.parametrize("product", sorted(_EMBEDDED_TRUST_ANCHORS))
def test_the_embedded_trust_anchors_still_load(product, refusing_cryptography):
    """The real CA certificates filigran-licenses issued: the keys every license is verified with."""
    ca_pem = _EMBEDDED_TRUST_ANCHORS[product]
    expected = _load_trust_anchor_public_key(ca_pem).public_numbers()

    with refusing_cryptography():
        certificate = load_license_certificate(ca_pem)
        key = _load_trust_anchor_public_key(ca_pem)

    assert isinstance(certificate, TolerantLicenseCertificate)
    assert certificate.serial_number == 0
    assert key.public_numbers() == expected
    # Self-signed by node-forge: its own signature verifies over the TBS the fallback sliced.
    key.verify(
        certificate.signature,
        certificate.tbs_certificate_bytes,
        padding.PKCS1v15(),
        certificate.signature_hash_algorithm,
    )


def test_a_support_code_still_verifies_against_a_serial_zero_ca(refusing_cryptography):
    """Support codes are checked against the XTM CA certificate, which carries serial 0 too."""
    payload = {"v": 1, "kind": POOL_RECHARGE_KIND, "id": str(uuid.uuid4()), "platform": "global"}
    raw = json.dumps({**payload, "exp": "2099-01-01T00:00:00Z"}, separators=(",", ":"), sort_keys=True).encode()
    signature = _key("ca").sign(raw, padding.PKCS1v15(), hashes.SHA256())
    code = SUPPORT_CODE_PREFIX + ".".join(
        base64.urlsafe_b64encode(part).decode().rstrip("=") for part in (raw, signature)
    )
    ca_pem = _serial_zero_ca()

    with refusing_cryptography():
        assert verify_support_code(code, ca_pem, None).kind == POOL_RECHARGE_KIND


# ── The fallback is for the known defects, and only those ───────────────────


def test_a_refusal_of_a_certificate_without_a_known_defect_is_never_rescued(monkeypatch):
    """A positive serial and only valid name strings: nothing for the fallback to patch."""
    pem = _serial_zero_license(serial_zero=False)

    def refuse(data: bytes, *args: object, **kwargs: object) -> x509.Certificate:
        raise ValueError("refused for another reason")

    monkeypatch.setattr(x509, "load_pem_x509_certificate", refuse)
    with pytest.raises(ValueError, match="refused for another reason"):
        load_license_certificate(pem)


def test_a_serial_zero_certificate_refused_for_another_reason_stays_refused(refusing_cryptography):
    """The serial must be cryptography's only objection: here it also refuses version 3 (no such X.509 version)."""
    pem = _serial_zero_license(version=3)
    # The walk alone would take it - asking cryptography about the positive-serial twin is what refuses it.
    der = _der(pem)
    assert der[license_certificate._certificate_layout(der).serial] == b"\x00"

    for release in (contextlib.nullcontext, refusing_cryptography):
        with release(), pytest.raises(x509.InvalidVersion):
            load_license_certificate(pem)


@pytest.mark.parametrize("serial", ["0000", "ffff", "ff80", "007f"])
def test_a_serial_cryptography_cannot_even_read_is_never_rescued(serial, refusing_cryptography):
    """A serial in more octets than it needs breaks DER: cryptography refuses it today, whatever its value.

    The same-length positive twin (01 00 is minimal) would hide that, so the fallback checks the encoding itself.
    """
    ca_pem = _serial_zero_ca()
    pem = _serial_zero_license(serial=bytes.fromhex(serial))
    for release in (contextlib.nullcontext, refusing_cryptography):
        with release():
            with pytest.raises(ValueError):
                load_license_certificate(pem)
            assert not decode_xtm_license_pem(pem, ca_pem).license_valid_cert


_PUBLIC_KEY_PEM = _key("ca").public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()


@pytest.mark.parametrize(
    "pem",
    [
        "",
        "not a certificate",
        "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n",
        _PUBLIC_KEY_PEM,
    ],
    ids=["empty", "text", "junk-der", "public-key"],
)
def test_what_is_not_a_certificate_is_refused_as_before(pem, refusing_cryptography):
    with refusing_cryptography():
        with pytest.raises(ValueError):
            load_license_certificate(pem)
        assert extract_license_product(pem) is None


# ── Signed the way filigran-licenses signs: RSA PKCS#1 v1.5 with SHA-2 ──────


@pytest.mark.parametrize(
    ("arc", "parameters", "hash_algorithm"),
    [
        (11, b"", hashes.SHA256()),
        (14, b"\x05\x00", hashes.SHA224()),
        (12, b"\x05\x00", hashes.SHA384()),
        (13, b"\x05\x00", hashes.SHA512()),
    ],
    ids=["sha256-parameters-absent", "sha224", "sha384", "sha512"],
)
def test_every_sha2_license_signature_is_read_by_the_fallback(arc, parameters, hash_algorithm, refusing_cryptography):
    """SHA-256 with NULL parameters (node-forge's encoding) is every other test; the rest of SHA-2 and no parameters too."""
    ca_pem = _serial_zero_ca()
    pem = _serial_zero_license(algorithm=_rsa_algorithm(arc, parameters=parameters), hash_algorithm=hash_algorithm)
    today = decode_xtm_license_pem(pem, ca_pem)

    with refusing_cryptography():
        certificate = load_license_certificate(pem)
        future = decode_xtm_license_pem(pem, ca_pem)

    assert isinstance(certificate, TolerantLicenseCertificate)
    assert type(certificate.signature_hash_algorithm) is type(hash_algorithm)
    assert future.license_validated
    assert dataclasses.asdict(future) == dataclasses.asdict(today)


def test_a_sha1_signed_license_is_not_rescued(refusing_cryptography):
    """filigran-licenses signs with SHA-256 (the embedded CAs, and the OpenAEV and OpenCRQ verifiers take SHA-2 only).

    A SHA-1 license would be one somebody else signed, or one a chosen-prefix
    collision could have produced: the fallback never carries it. Built with
    NULL parameters, as node-forge writes them; cryptography's builder refuses
    SHA-1 outright.
    """
    ca_pem = _serial_zero_ca()
    pem = _serial_zero_license(algorithm=_rsa_algorithm(5), hash_algorithm=hashes.SHA1())  # noqa: S303 - the refused case
    # Today cryptography reads it and the signature decides; the fallback does not carry that forward.
    assert isinstance(load_license_certificate(pem), x509.Certificate)

    with refusing_cryptography():
        with pytest.raises(_SERIAL_REFUSAL, match="serial number"):
            load_license_certificate(pem)
        info = decode_xtm_license_pem(pem, ca_pem)

    assert not info.license_valid_cert
    assert not info.license_validated


_ECDSA_WITH_SHA256 = _tlv(0x30, _oid("1.2.840.10045.4.3.2"))
_RSA_PSS = _rsa_algorithm(10, parameters=b"")
_RSA_WITH_SHA3_256 = _tlv(0x30, _oid("2.16.840.1.101.3.4.3.14"))


@pytest.mark.parametrize(
    "algorithm",
    [_rsa_algorithm(4), _rsa_algorithm(5, parameters=b""), _RSA_PSS, _RSA_WITH_SHA3_256, _ECDSA_WITH_SHA256],
    ids=["md5", "sha1-parameters-absent", "rsa-pss", "sha3-256", "ecdsa-sha256"],
)
def test_other_signature_algorithms_are_not_rescued(algorithm, refusing_cryptography):
    pem = _serial_zero_license(algorithm=algorithm)
    with refusing_cryptography(), pytest.raises(_SERIAL_REFUSAL, match="serial number"):
        load_license_certificate(pem)


_SHA256 = _rsa_algorithm(11)


@pytest.mark.parametrize(
    ("outer_algorithm", "hash_algorithm"),
    [
        (_rsa_algorithm(13), hashes.SHA512()),
        (_rsa_algorithm(5), hashes.SHA1()),  # noqa: S303 - the refused case
        (_rsa_algorithm(11, parameters=b""), hashes.SHA256()),
    ],
    ids=["outer-sha512", "outer-sha1", "outer-parameters-dropped"],
)
def test_the_algorithm_outside_the_tbs_must_repeat_the_signed_one(
    outer_algorithm, hash_algorithm, refusing_cryptography
):
    """RFC 5280 4.1.1.2. The hash is taken from the unsigned outer algorithm, so it may not differ from the signed one.

    cryptography does not compare the two when it loads a certificate; each of
    these is signed with the outer algorithm's hash, so honouring the outer one
    alone would verify it.
    """
    ca_pem = _serial_zero_ca()
    certificate = x509.load_pem_x509_certificate(_serial_zero_license(serial_zero=False).encode())
    elements = _tbs_elements(certificate)
    elements[1] = _tlv(0x02, b"\x00")
    assert elements[2] == _SHA256
    pem = _pem(_sign(elements, _key("ca"), hash_algorithm=hash_algorithm, outer_algorithm=outer_algorithm))

    with refusing_cryptography():
        with pytest.raises(_SERIAL_REFUSAL, match="serial number"):
            load_license_certificate(pem)
        assert not decode_xtm_license_pem(pem, ca_pem).license_valid_cert


# ── Every field comes from the octets the signature covers ──────────────────


def _re_framed(der: bytes, variant: str) -> bytes:
    """The certificate ``der`` re-framed outside its TBS: the signature still verifies over every variant."""
    tbs, algorithm, signature = _elements(_contents(der))
    other = _der(_serial_zero_license(customer="Other Customer"))
    octets = _contents(signature)
    return {
        "trailing-garbage": lambda: der + b"\x00\x00",
        "trailing-second-certificate": lambda: der + other,
        "second-tbs": lambda: _tlv(0x30, tbs + _elements(_contents(other))[0] + algorithm + signature),
        "tbs-repeated": lambda: _tlv(0x30, tbs + tbs + algorithm + signature),
        "non-minimal-certificate-length": lambda: _long_form(0x30, _contents(der), 3),
        "indefinite-certificate-length": lambda: b"\x30\x80" + _contents(der) + b"\x00\x00",
        "non-minimal-algorithm-length": lambda: _tlv(0x30, tbs + _long_form(0x30, _contents(algorithm), 1) + signature),
        "non-minimal-signature-length": lambda: _tlv(0x30, tbs + algorithm + _long_form(0x03, octets, 3)),
        "signature-unused-bits": lambda: _tlv(0x30, tbs + algorithm + _tlv(0x03, b"\x01" + octets[1:])),
        "algorithm-as-a-set": lambda: _tlv(0x30, tbs + b"\x31" + algorithm[1:] + signature),
        "signature-as-octet-string": lambda: _tlv(0x30, tbs + algorithm + _tlv(0x04, octets[1:])),
    }[variant]()


@pytest.mark.parametrize(
    "variant",
    [
        "trailing-garbage",
        "trailing-second-certificate",
        "second-tbs",
        "tbs-repeated",
        "non-minimal-certificate-length",
        "indefinite-certificate-length",
        "non-minimal-algorithm-length",
        "non-minimal-signature-length",
        "signature-unused-bits",
        "algorithm-as-a-set",
        "signature-as-octet-string",
    ],
)
def test_a_certificate_re_framed_around_its_signed_tbs_is_refused(variant, refusing_cryptography):
    """Trailing certificates or bytes, a second TBS, BER lengths, loose tags: the octets outside the TBS are DER too."""
    ca_pem = _serial_zero_ca()
    pem = _pem(_re_framed(_der(_serial_zero_license()), variant))
    _assert_loaded_as_cryptography_loads_it_today(pem)
    with refusing_cryptography():
        with pytest.raises(_SERIAL_REFUSAL):
            load_license_certificate(pem)
        assert not decode_xtm_license_pem(pem, ca_pem).license_valid_cert


_TBS_EDITS: dict[str, Callable[[list[bytes]], None]] = {
    "field-after-the-extensions": lambda elements: elements.append(_tlv(0x04, b"after the extensions")),
    "boolean-true-as-01": _add_extensions(_extension("1.3.6.1.4.1.62944.999", b"x", critical=b"\x01\x01\x01")),
    "boolean-false-encoded": _add_extensions(_extension("1.3.6.1.4.1.62944.999", b"x", critical=b"\x01\x01\x00")),
    "extensions-application-class": lambda elements: elements.__setitem__(-1, b"\x63" + elements[-1][1:]),
    "subject-non-minimal-length": lambda elements: elements.__setitem__(5, _long_form(0x30, _contents(elements[5]), 2)),
    "subject-indefinite-length": lambda elements: elements.__setitem__(
        5, b"\x30\x80" + _contents(elements[5]) + b"\x00\x00"
    ),
    "version-non-minimal-integer": lambda elements: elements.__setitem__(0, _tlv(0xA0, _tlv(0x02, b"\x00\x02"))),
}


@pytest.mark.parametrize("edit", sorted(_TBS_EDITS))
def test_a_signed_tbs_that_is_not_der_is_refused(edit, refusing_cryptography):
    """Signed by the test CA, so only the parse can refuse them - today and through the fallback alike."""
    ca_pem = _serial_zero_ca()
    pem = _pem(_edited_license(_TBS_EDITS[edit]))
    _assert_loaded_as_cryptography_loads_it_today(pem)
    with refusing_cryptography():
        with pytest.raises(_SERIAL_REFUSAL):
            load_license_certificate(pem)
        assert not decode_xtm_license_pem(pem, ca_pem).license_valid_cert


def test_an_extension_present_twice_is_read_by_neither_path(refusing_cryptography):
    """Two daily quotas, both signed: cryptography refuses to read the extensions, and so does the fallback."""
    ca_pem = _serial_zero_ca()
    pem = _pem(
        _edited_license(
            _add_extensions(_extension(LICENSE_OID_DAILY_QUOTA, b"999999")),
            extensions={LICENSE_OID_DAILY_QUOTA: b"500"},
        )
    )
    today = decode_xtm_license_pem(pem, ca_pem)
    with refusing_cryptography():
        certificate = load_license_certificate(pem)
        future = decode_xtm_license_pem(pem, ca_pem)
        with pytest.raises(x509.DuplicateExtension):
            _ = certificate.extensions

    assert isinstance(certificate, TolerantLicenseCertificate)
    assert not future.license_validated and future.daily_quota == 0
    # Both reads stop before the validity window, so its fields keep their datetime.now() defaults.
    unread = {"license_start_date", "license_expiration_date"}
    assert {name: value for name, value in dataclasses.asdict(future).items() if name not in unread} == {
        name: value for name, value in dataclasses.asdict(today).items() if name not in unread
    }


def test_a_license_oid_wrapped_inside_another_extension_is_not_read(refusing_cryptography):
    """Only a top-level extension carries a license field; one nested in another extension's value is opaque."""
    ca_pem = _serial_zero_ca()
    wrapped = _extension(LICENSE_OID_DAILY_QUOTA, b"999999")
    pem = _pem(_edited_license(_add_extensions(_extension("1.3.6.1.4.1.62944.999", wrapped))))
    today = decode_xtm_license_pem(pem, ca_pem)
    with refusing_cryptography():
        future = decode_xtm_license_pem(pem, ca_pem)

    assert future.license_validated and future.daily_quota == 0
    assert dataclasses.asdict(future) == dataclasses.asdict(today)


@pytest.mark.parametrize("part", ["tbs", "serial"])
def test_the_twin_must_be_read_where_the_walk_located_its_parts(part, monkeypatch, refusing_cryptography):
    """cryptography's reading of the twin is the check on the hand-written walk: a walk that is off is refused.

    ``tbs``: one octet short, so cryptography's TBS is not the slice that would be verified.
    ``serial``: a zero octet of an extension value taken for the serial, so the twin keeps serial 0.
    """
    marker = _extension("1.3.6.1.4.1.62944.999", b"\x00")
    pem = _pem(_edited_license(_add_extensions(marker)))
    der = _der(pem)
    genuine = license_certificate._certificate_layout(der)
    zero = der.index(marker) + len(marker) - 1
    layout = {
        "tbs": dataclasses.replace(genuine, tbs=slice(genuine.tbs.start, genuine.tbs.stop - 1)),
        "serial": dataclasses.replace(genuine, serial=slice(zero, zero + 1)),
    }[part]
    with refusing_cryptography():
        assert isinstance(load_license_certificate(pem), TolerantLicenseCertificate)
        monkeypatch.setattr(license_certificate, "_certificate_layout", lambda _der: layout)
        with pytest.raises(_SERIAL_REFUSAL):
            load_license_certificate(pem)


def _non_canonical_base64(der: bytes) -> str | None:
    """``der`` in base64 whose last character carries padding bits that are not zero, or ``None`` without padding."""
    encoded = base64.b64encode(der).decode()
    if not encoded.endswith("="):
        return None
    index = len(encoded.rstrip("=")) - 1
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    return encoded[:index] + alphabet[alphabet.index(encoded[index]) | 1] + encoded[index + 1 :]


def test_the_fallback_reads_the_pem_text_as_cryptography_reads_it(refusing_cryptography):
    """The twin is the same text with only the serial's base64 changed, so cryptography's PEM rules still apply."""
    ca_pem = _serial_zero_ca()
    genuine = _serial_zero_license()
    forged = _serial_zero_license(customer="Evil Customer", signer="attacker")
    non_canonical = next(
        encoded
        for customer in ("Test Customer", "Test Customer.", "Test Customer..")
        if (encoded := _non_canonical_base64(_der(_serial_zero_license(customer=customer)))) is not None
    )
    refused = {
        "malformed-other-block": genuine + "-----BEGIN NOTES-----\n!!! not base64 !!!\n-----END NOTES-----\n",
        "malformed-leading-certificate": "-----BEGIN CERTIFICATE-----\n@@@@\n-----END CERTIFICATE-----\n" + genuine,
        "non-canonical-base64": f"-----BEGIN CERTIFICATE-----\n{non_canonical}\n-----END CERTIFICATE-----\n",
    }
    for what, pem in refused.items():
        assert isinstance(_cryptography_today(pem), Exception), what
        _assert_loaded_as_cryptography_loads_it_today(pem)
        with refusing_cryptography():
            with pytest.raises(ValueError):
                load_license_certificate(pem)
            assert not decode_xtm_license_pem(pem, ca_pem).license_valid_cert, what

    labelled = genuine.replace("BEGIN CERTIFICATE", "BEGIN X509 CERTIFICATE").replace(
        "END CERTIFICATE", "END X509 CERTIFICATE"
    )
    first_block_read = {"genuine-first": genuine + forged, "x509-label": labelled}
    lines = genuine.splitlines()
    # RFC 1421 headers: cryptography reads the block, the fallback declines rather than reading another one.
    with_headers = "\n".join([lines[0], "Proc-Type: 4,ENCRYPTED", "", *lines[1:]]) + "\n" + forged
    with refusing_cryptography():
        for what, pem in first_block_read.items():
            info = decode_xtm_license_pem(pem, ca_pem)
            assert info.license_validated and info.license_customer == "Test Customer", what
            assert info.license_certificate_pem == genuine, what
        assert not decode_xtm_license_pem(forged + genuine, ca_pem).license_valid_cert
        with pytest.raises(_SERIAL_REFUSAL):
            load_license_certificate(with_headers)


# ── Bounded before any parsing ──────────────────────────────────────────────


def test_a_pem_over_the_bound_is_refused_before_any_parsing(monkeypatch):
    pem = _serial_zero_license()
    at_the_bound = pem + "#" * (MAX_LICENSE_PEM_CHARS - len(pem))
    assert isinstance(load_license_certificate(at_the_bound), x509.Certificate)

    def parse(*args: object, **kwargs: object) -> x509.Certificate:
        raise AssertionError("parsed a PEM over the bound")

    monkeypatch.setattr(x509, "load_pem_x509_certificate", parse)
    monkeypatch.setattr(x509, "load_der_x509_certificate", parse)
    with pytest.raises(ValueError, match="at most"):
        load_license_certificate(at_the_bound + "#")
    assert extract_license_product(at_the_bound + "#") is None


def test_a_registering_platform_license_over_the_bound_is_refused_before_decoding(monkeypatch):
    ca_pem = _serial_zero_ca()
    monkeypatch.setitem(plv.PRODUCT_CA_CERTS, "openaev", (ca_pem, ("openaev",)))
    pem = _serial_zero_license("openaev")
    # The longest license a platform may send: the loader's bound, base64-encoded as OpenAEV sends it.
    longest = base64.b64encode((pem + "#" * (MAX_LICENSE_PEM_CHARS - len(pem))).encode()).decode()
    assert len(longest) == plv._MAX_REGISTRATION_LICENSE_CHARS
    assert validate_platform_license("openaev", longest).license_validated

    def decode(*args: object, **kwargs: object) -> str:
        raise AssertionError("decoded a license over the bound")

    monkeypatch.setattr(plv, "_decode_pem", decode)
    assert not validate_platform_license("openaev", longest + "A").license_validated


@pytest.mark.parametrize(
    "der",
    [
        bytes.fromhex("30 84 ff ff ff ff"),
        bytes.fromhex("30 85 00 00 00 00 10") + bytes(16),
        bytes.fromhex("30 80 30 80 02 01 00 00 00 00 00"),
        bytes.fromhex("30 81 05 02 01 00 30 00"),
        bytes.fromhex("30 00"),
        b"\x30\x82\xff\xf0" + b"\x30\x82\xff\xec" * 10,
        bytes.fromhex("30 0b 30 04 02 00 30 00 30 00 03 01 00"),
    ],
    ids=[
        "4-gib-length",
        "5-length-octets",
        "indefinite",
        "non-minimal-length",
        "empty",
        "truncated-nesting",
        "empty-serial",
    ],
)
def test_hostile_der_is_refused_by_the_walk_without_parsing_it(der):
    with pytest.raises(ValueError):
        license_certificate._certificate_layout(der)


# ── Mutation fuzz ───────────────────────────────────────────────────────────


def test_no_mutation_of_a_genuine_license_validates(refusing_cryptography):
    """Bytes flipped, inserted, deleted or cut all over a genuine serial-0 license: none validates.

    Today every mutant fares with the loader exactly as with cryptography alone;
    through the fallback not one validates, let alone as a different license.
    """
    ca_pem = _serial_zero_ca()
    genuine = _serial_zero_license(
        extensions={
            LICENSE_OID_CREATOR: b"Filigran",
            LICENSE_OID_DAILY_QUOTA: b"500",
            LICENSE_OID_MONTHLY_QUOTA: b"5000",
            LICENSE_OID_YEARLY_QUOTA: b"50000",
            LICENSE_OID_OPENCTI_IDS: json.dumps(["opencti-1"]).encode(),
        }
    )
    der = _der(genuine)
    layout = license_certificate._certificate_layout(der)
    extensions_start = layout.tbs.stop - len(_elements(_contents(der[layout.tbs]))[-1])
    regions = [
        range(0, len(der)),
        range(layout.tbs.start, layout.tbs.stop),
        range(extensions_start, layout.tbs.stop),
        range(layout.tbs.stop, len(der)),
    ]
    with refusing_cryptography():
        original = decode_xtm_license_pem(genuine, ca_pem)
    assert original.license_validated

    rng = random.Random(3855)  # noqa: S311 - a reproducible fuzz, not a secret
    accepted: list[tuple[str, int, bool]] = []
    for round_number in range(240):
        position = rng.choice(regions[round_number % len(regions)])
        operation = ("flip", "insert", "delete", "truncate")[round_number // len(regions) % 4]
        if operation == "flip":
            mutant = der[:position] + bytes([der[position] ^ (1 << rng.randrange(8))]) + der[position + 1 :]
        elif operation == "insert":
            mutant = der[:position] + bytes([rng.randrange(256)]) + der[position:]
        elif operation == "delete":
            mutant = der[:position] + der[position + 1 :]
        else:
            mutant = der[: max(position, 1)]
        pem = _pem(mutant)
        _assert_loaded_as_cryptography_loads_it_today(pem)
        with refusing_cryptography():
            info = decode_xtm_license_pem(pem, ca_pem)
        if info.license_validated:
            accepted.append((operation, position, _decisions(info) == _decisions(original)))
    assert accepted == []


# ── The fallback reads what cryptography reads ──────────────────────────────


def _exotic_license() -> str:
    """Every string type a subject may carry, a GeneralizedTime notAfter and a critical extension."""
    certificate = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name(
                [
                    x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Soci\u00e9t\u00e9", _type=_ASN1Type.BMPString),
                    x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "global", _type=_ASN1Type.UniversalString),
                    x509.NameAttribute(NameOID.COMMON_NAME, "test-license", _type=_ASN1Type.T61String),
                    x509.NameAttribute(NameOID.LOCALITY_NAME, "Paris", _type=_ASN1Type.IA5String),
                ]
            )
        )
        .issuer_name(_CA_NAME)
        .public_key(_key("license").public_key())
        .serial_number(1)
        .not_valid_before(datetime(1990, 1, 1, tzinfo=UTC))
        .not_valid_after(datetime(2051, 6, 1, 12, 30, tzinfo=UTC))
        .add_extension(x509.UnrecognizedExtension(licensing.LICENSE_OID_TYPE, b"\x0c\x08standard"), critical=True)
        .add_extension(x509.UnrecognizedExtension(licensing.LICENSE_OID_PRODUCT, b"filigran xtm"), critical=False)
        .sign(_key("ca"), hashes.SHA256())
    )
    return _pem(_reissued(certificate, _key("ca")))


_PARITY_CASES: dict[str, Callable[[], str]] = {
    "xtm-ca": lambda: XTM_CA_PEM,
    "opencti-ca": lambda: OPENCTI_CA_PEM,
    "openaev-ca": lambda: OPENAEV_CA_PEM,
    "test-ca": _serial_zero_ca,
    "license": lambda: _serial_zero_license(
        customer="Soci\u00e9t\u00e9 G\u00e9n\u00e9rale",
        extensions={
            LICENSE_OID_CREATOR: b"Filigran",
            LICENSE_OID_DAILY_QUOTA: b"500",
            LICENSE_OID_OPENCTI_IDS: json.dumps(["opencti-1", "opencti-2"]).encode(),
            licensing.LICENSE_LEGACY_OID_CREATOR: b"legacy creator",
        },
    ),
    "exotic-encodings": _exotic_license,
}


@pytest.mark.parametrize("case", sorted(_PARITY_CASES))
def test_the_fallback_reads_exactly_what_cryptography_reads(case):
    pem = _PARITY_CASES[case]()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", CryptographyDeprecationWarning)
        expected = x509.load_pem_x509_certificate(pem.encode())
        assert expected.serial_number == 0
    tolerant = license_certificate._load_tolerant_certificate(pem.encode())
    assert tolerant is not None

    assert tolerant.serial_number == 0
    assert tolerant.tbs_certificate_bytes == expected.tbs_certificate_bytes
    assert tolerant.signature == expected.signature
    assert type(tolerant.signature_hash_algorithm) is type(expected.signature_hash_algorithm)
    assert tolerant.not_valid_before_utc == expected.not_valid_before_utc
    assert tolerant.not_valid_after_utc == expected.not_valid_after_utc
    assert tolerant.subject == expected.subject
    assert [(attribute.oid, attribute.value) for attribute in tolerant.subject] == [
        (attribute.oid, attribute.value) for attribute in expected.subject
    ]
    assert [(extension.oid, extension.critical, extension.value) for extension in tolerant.extensions] == [
        (extension.oid, extension.critical, extension.value) for extension in expected.extensions
    ]
    for oid in _FILIGRAN_OIDS:
        assert _get_extension_value(tolerant, oid) == _get_extension_value(expected, oid), oid
    assert tolerant.public_key().public_numbers() == expected.public_key().public_numbers()
    for encoding in (Encoding.PEM, Encoding.DER):
        assert tolerant.public_bytes(encoding) == expected.public_bytes(encoding)
    with pytest.raises(TypeError):
        tolerant.public_bytes(Encoding.OpenSSH)


# ── Tripwire: nothing parses a certificate around the loader ────────────────

_DIRECT_CERTIFICATE_LOAD = re.compile(r"\bload_(?:pem|der)_x509_certificates?\b")

# Modules that load an X.509 certificate that is NOT a Filigran license or trust
# anchor, and so may use cryptography directly. Empty today.
_NOT_LICENSE_CERTIFICATES: frozenset[str] = frozenset(
    {
        # A user's own mutual TLS client certificate for an OpenAPI integration.
        "app/integrations/client_certificate.py",
    }
)


def test_every_certificate_the_app_parses_goes_through_the_license_loader():
    """A license or trust anchor parsed with cryptography directly fails for every customer on the refusing release."""
    loader = APP / "enterprise" / "license_certificate.py"
    offenders: list[str] = []
    scanned = 0
    for path in APP.rglob("*.py"):
        relative = path.relative_to(APP.parent).as_posix()
        if path == loader or relative in _NOT_LICENSE_CERTIFICATES:
            continue
        scanned += 1
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _DIRECT_CERTIFICATE_LOAD.search(line):
                offenders.append(f"{relative}:{lineno}: {line.strip()}")
    # Never pass vacuously: the scan proves something only if it walked the tree.
    assert scanned >= 100, f"scanned only {scanned} modules under {APP}"
    assert not offenders, (
        "Parse license and trust-anchor certificates with "
        "app.enterprise.license_certificate.load_license_certificate (filigran-licenses issues "
        "serial number 0, which cryptography will refuse); a certificate that is not a Filigran "
        "license belongs in _NOT_LICENSE_CERTIFICATES:\n" + "\n".join(offenders)
    )
