"""The one loader for Filigran license certificates, including the ones cryptography will refuse.

filigran-licenses issues every license certificate - Filigran licenses and the
OpenCTI / OpenAEV / OpenCRQ product licenses XTM One validates - and every
product CA certificate embedded here as a trust anchor with serial number 0
(node-forge writes it that way). RFC 5280 requires a positive serial, and
``cryptography`` already warns on each load: "Parsed a serial number which
wasn't positive [...] Loading this certificate will cause an exception in a
future release of cryptography." On that release every license check would fail
closed - no Enterprise Edition, AI off, for every customer.

``load_license_certificate`` is what every license check parses with:

* ``cryptography`` first, as it always was. While it still loads these
  certificates the caller gets the ``x509.Certificate`` it always got;
  ``_ignore_serial_number_warning`` silences the warning for this known case
  and nothing else.
* When ``cryptography`` refuses a certificate, the fallback is taken for the
  two known defects only, alone or together, and only when ``cryptography``
  loads the very same PEM text once the base64 characters encoding them are
  patched - those defects are its one objection - and the certificate is
  signed the way filigran-licenses signs: RSA PKCS#1 v1.5 with SHA-2, the same
  algorithm inside and outside the TBS. Every field is then read by
  ``cryptography`` from that patched twin, except the strings decoded below.
* The first defect is the serial: a minimally encoded zero or negative
  INTEGER, patched to a positive integer of the same length. A positive serial
  is left as issued, so ``cryptography`` still judges its encoding itself.
* The second defect is a subject or issuer string tagged PrintableString that
  is not one (node-forge writes a name's non-ASCII characters, such as the
  ``ö`` of an organization, there as raw bytes). ``cryptography`` 50
  refuses it. The twin holds ``?`` in place of each such string, same length,
  and the fallback decodes those strings itself - UTF-8 first, else Latin-1, so
  Latin-1 bytes that also form valid UTF-8 read as UTF-8 - into the
  ``subject`` it returns. When ``x509.NameAttribute`` refuses a decoded value
  (a country whose UTF-8 is not two octets), the certificate stays refused.

Every other refusal propagates unchanged.

Trust is untouched on the fallback: the twin differs from the certificate as
issued in the serial's content octets and the content octets of those name
strings alone - no length moves, so no other field shifts -
``tbs_certificate_bytes`` are the octets the issuer signed, and callers verify
the signature over them with the trust anchor's public key, RSA PKCS#1 v1.5 and
the hash the signature algorithm names: the same call either way.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
import warnings
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.types import PublicKeyTypes
from cryptography.utils import CryptographyDeprecationWarning

logger = logging.getLogger(__name__)

# The longest PEM text parsed at all, checked before any parsing. A license is
# about 2 KB of PEM; these texts come from the environment, the database, an
# administrator's paste and, at registration, a remote platform.
MAX_LICENSE_PEM_CHARS = 64 * 1024


def _ignore_serial_number_warning() -> None:
    """Silence cryptography's non-positive-serial warning for this module's own loads.

    On these certificates the warning is expected - it announces the refusal the
    fallback below exists for - and nothing an operator can act on. It is scoped
    three ways: the message, the category, and the module it is attributed to
    (cryptography warns at stack level 1, on the frame that called its loader:
    this module). Every other cryptography deprecation, and this one raised for
    any other caller, still surfaces. Appended, so a filter configured explicitly
    (``-W error``, a test turning the warning into an exception) still wins - the
    fallback then carries the load.
    """
    warnings.filterwarnings(
        "ignore",
        message=r"Parsed a (?:negative )?serial number",
        category=CryptographyDeprecationWarning,
        module=re.escape(__name__) + r"\Z",
        append=True,
    )


_ignore_serial_number_warning()


class LicenseCertificate(Protocol):
    """The members of an X.509 certificate the license checks read.

    Satisfied by ``cryptography.x509.Certificate`` (every certificate
    cryptography loads) and by ``TolerantLicenseCertificate`` (the fallback).
    """

    @property
    def tbs_certificate_bytes(self) -> bytes: ...

    @property
    def signature(self) -> bytes: ...

    @property
    def signature_hash_algorithm(self) -> hashes.HashAlgorithm | None: ...

    @property
    def subject(self) -> x509.Name: ...

    @property
    def not_valid_before_utc(self) -> datetime: ...

    @property
    def not_valid_after_utc(self) -> datetime: ...

    @property
    def extensions(self) -> x509.Extensions: ...

    def public_key(self) -> PublicKeyTypes: ...

    def public_bytes(self, encoding: serialization.Encoding) -> bytes: ...


@dataclass(frozen=True, slots=True)
class TolerantLicenseCertificate:
    """A license or trust-anchor certificate cryptography refuses for its serial number or name strings alone.

    ``tbs_certificate_bytes`` are the exact octets the issuer signed and
    ``public_bytes`` re-emits the certificate as issued, byte for byte what
    cryptography writes. The signature, its hash algorithm, the validity
    window, the extensions and the public key are read by cryptography from the
    twin, whose octets are the issued ones except the serial's content and the
    content of the name strings cryptography refuses. The subject is the twin's,
    with those strings decoded from the issued octets.
    """

    der: bytes = field(repr=False)
    tbs_certificate_bytes: bytes = field(repr=False)
    serial_number: int
    _twin: x509.Certificate = field(repr=False)
    _subject: x509.Name = field(repr=False)

    @property
    def signature(self) -> bytes:
        return self._twin.signature

    @property
    def signature_hash_algorithm(self) -> hashes.HashAlgorithm | None:
        return self._twin.signature_hash_algorithm

    @property
    def subject(self) -> x509.Name:
        return self._subject

    @property
    def not_valid_before_utc(self) -> datetime:
        return self._twin.not_valid_before_utc

    @property
    def not_valid_after_utc(self) -> datetime:
        return self._twin.not_valid_after_utc

    @property
    def extensions(self) -> x509.Extensions:
        return self._twin.extensions

    def public_key(self) -> PublicKeyTypes:
        return self._twin.public_key()

    def public_bytes(self, encoding: serialization.Encoding) -> bytes:
        if encoding is serialization.Encoding.DER:
            return self.der
        if encoding is serialization.Encoding.PEM:
            body = base64.b64encode(self.der).decode("ascii")
            lines = "\n".join(body[index : index + 64] for index in range(0, len(body), 64))
            return f"-----BEGIN CERTIFICATE-----\n{lines}\n-----END CERTIFICATE-----\n".encode("ascii")
        raise TypeError("encoding must be Encoding.DER or Encoding.PEM")


def load_license_certificate(pem: str) -> LicenseCertificate:
    """Parse a license or trust-anchor certificate PEM.

    Returns the ``x509.Certificate`` cryptography loads or - only for a
    certificate cryptography refuses solely for its non-positive serial or its
    invalid PrintableString name strings - a ``TolerantLicenseCertificate`` read
    from the same PEM. Anything else raises
    what ``x509.load_pem_x509_certificate`` raised; a text longer than
    ``MAX_LICENSE_PEM_CHARS`` is refused before any parsing.
    """
    if len(pem) > MAX_LICENSE_PEM_CHARS:
        raise ValueError(f"a license certificate PEM holds at most {MAX_LICENSE_PEM_CHARS} characters")
    data = pem.encode()
    try:
        return x509.load_pem_x509_certificate(data)
    except Exception:
        # Deliberately broad: what the refusing release will raise is not known
        # yet (today it is the warning itself, when a filter makes it an error).
        # The decision is made on the certificate, never on the exception, and
        # anything that is not the known case re-raises it.
        certificate = _load_tolerant_certificate(data)
        if certificate is None:
            raise
    _log_fallback()
    return certificate


_fallback_logged = False


def _log_fallback() -> None:
    """Say once per process that the fallback is carrying the license checks."""
    global _fallback_logged
    if _fallback_logged:
        logger.debug("License certificate loaded by the tolerant fallback")
        return
    _fallback_logged = True
    logger.info(
        "cryptography refused a license certificate for its non-positive serial number or "
        "invalid PrintableString name strings (filigran-licenses issues serial 0 and writes "
        "non-ASCII names there); license certificates are read through a patched twin, "
        "signature checks are unchanged"
    )


_PEM_CERTIFICATE = re.compile(rb"-----BEGIN ((?:X509 )?)CERTIFICATE-----([A-Za-z0-9+/=\s]*)-----END \1CERTIFICATE-----")
_PEM_WHITESPACE = frozenset(b" \t\n\r\x0b\x0c")

_SEQUENCE = 0x30
_INTEGER = 0x02
_BIT_STRING = 0x03
_EXPLICIT_VERSION = 0xA0

# The signature algorithms of filigran-licenses: RSA PKCS#1 v1.5 with SHA-2. It
# signs with SHA-256 - the embedded CA certificates do, and the OpenAEV and
# OpenCRQ license verifiers accept nothing else - so SHA-1 and MD5 are refused
# here. Keyed by the exact DER of the AlgorithmIdentifier: OID
# 1.2.840.113549.1.1.{14,11,12,13} with NULL parameters (as node-forge writes
# them) or none, the two forms cryptography reads.
_PKCS1_OID = bytes.fromhex("06092a864886f70d0101")
_LICENSE_SIGNATURE_ALGORITHMS: dict[bytes, type[hashes.HashAlgorithm]] = {
    algorithm: hash_type
    for arc, hash_type in ((14, hashes.SHA224), (11, hashes.SHA256), (12, hashes.SHA384), (13, hashes.SHA512))
    for algorithm in (
        b"\x30\x0d" + _PKCS1_OID + bytes([arc]) + b"\x05\x00",
        b"\x30\x0b" + _PKCS1_OID + bytes([arc]),
    )
}


def _load_tolerant_certificate(data: bytes) -> TolerantLicenseCertificate | None:
    """The fallback: ``None`` whenever the certificate is not one of the known cases."""
    match = _PEM_CERTIFICATE.search(data)
    if match is None:
        return None
    # Where, in the text, the base64 characters of the certificate block sit.
    positions = [index for index in range(*match.span(2)) if data[index] not in _PEM_WHITESPACE]
    encoded = bytes(data[index] for index in positions)
    try:
        der = base64.b64decode(encoded, validate=True)
    except binascii.Error:
        return None
    # Canonical base64 only, as cryptography's PEM reader requires.
    if not der or base64.b64encode(der) != encoded:
        return None
    try:
        layout = _certificate_layout(der)
    except ValueError:
        return None
    serial = der[layout.serial]
    serial_number = int.from_bytes(serial, "big", signed=True)
    try:
        bad_strings = _invalid_printable_strings(der, layout)
    except ValueError:
        return None
    non_positive = serial_number <= 0
    if (non_positive and not _is_minimal_integer(serial)) or not (non_positive or bad_strings):
        return None
    # Are those defects cryptography's only objection? Ask it about the same PEM
    # text with a positive serial of the same length (01 00 .. 00) and a "?" for
    # each octet of those strings: only base64 characters change, no length octet
    # moves, and cryptography applies its own PEM and DER rules to everything else.
    patches = [(string.contents, b"?" * (string.contents.stop - string.contents.start)) for string in bad_strings]
    positive_serial = serial
    if non_positive:
        positive_serial = b"\x01" + bytes(len(serial) - 1)
        patches.append((layout.serial, positive_serial))
    twin_der = bytearray(der)
    for where, replacement in patches:
        twin_der[where] = replacement
    twin_der = bytes(twin_der)
    twin_text = bytearray(data)
    for index, character in zip(positions, base64.b64encode(twin_der), strict=True):
        twin_text[index] = character
    try:
        twin = x509.load_pem_x509_certificate(bytes(twin_text))
    except Exception:
        logger.debug("License certificate refused for more than its known defects", exc_info=True)
        return None
    # cryptography read the block patched here, its TBS and serial where the walk found them.
    if (
        twin.public_bytes(serialization.Encoding.DER) != twin_der
        or twin.tbs_certificate_bytes != twin_der[layout.tbs]
        or twin.serial_number != int.from_bytes(positive_serial, "big")
    ):
        return None
    try:
        # Read lazily: a "?" placeholder of a length its attribute does not allow (a one-octet country)
        # makes cryptography warn here, and a filter turning that warning into an error raises.
        twin_subject = twin.subject
    except Exception:
        logger.debug("License certificate refused: the twin's subject could not be read", exc_info=True)
        return None
    subject = _decoded_subject(der, twin_subject, [string for string in bad_strings if string.in_subject])
    if subject is None:
        return None
    # RFC 5280 4.1.1.2: the algorithm the TBS names (signed) and the one beside it
    # (not signed, and the one the hash is taken from) are the same.
    algorithm = der[layout.inner_algorithm]
    hash_type = _LICENSE_SIGNATURE_ALGORITHMS.get(algorithm)
    if (
        algorithm != der[layout.outer_algorithm]
        or hash_type is None
        or type(twin.signature_hash_algorithm) is not hash_type
    ):
        logger.debug("License certificate not signed with RSA PKCS#1 v1.5 and SHA-2 as filigran-licenses signs")
        return None
    return TolerantLicenseCertificate(
        der=der,
        tbs_certificate_bytes=der[layout.tbs],
        serial_number=serial_number,
        _twin=twin,
        _subject=subject,
    )


def _is_minimal_integer(contents: bytes) -> bool:
    """X.690 8.3.2: one octet at least, and the first nine bits of a longer one neither all 0 nor all 1."""
    if len(contents) < 2:
        return len(contents) == 1
    return not (contents[0] in (0x00, 0xFF) and (contents[0] ^ contents[1]) & 0x80 == 0)


@dataclass(frozen=True, slots=True)
class _CertificateLayout:
    """Where the parts the fallback checks sit in a certificate's DER."""

    tbs: slice
    serial: slice
    inner_algorithm: slice
    outer_algorithm: slice
    issuer: slice
    subject: slice


def _der_element(der: bytes, offset: int, tag: int, limit: int) -> tuple[int, int]:
    """``(contents_start, end)`` of the DER element at ``offset``, which must end by ``limit``.

    DER only: the tag must be ``tag``, the length definite and minimally encoded.
    """
    if offset + 2 > limit or der[offset] != tag:
        raise ValueError(f"expected DER tag 0x{tag:02x} at offset {offset}")
    return _der_contents(der, offset, limit)


def _der_contents(der: bytes, offset: int, limit: int) -> tuple[int, int]:
    """``(contents_start, end)`` of the DER element at ``offset``, whatever its (one octet) tag."""
    if offset + 2 > limit or der[offset] & 0x1F == 0x1F:
        raise ValueError(f"unsupported DER tag at offset {offset}")
    length = der[offset + 1]
    start = offset + 2
    if length & 0x80:
        count = length & 0x7F
        if not 1 <= count <= 4 or start + count > limit:
            raise ValueError(f"unsupported DER length at offset {offset}")
        length = int.from_bytes(der[start : start + count], "big")
        if length < 0x80 or der[start] == 0:
            raise ValueError(f"non-minimal DER length at offset {offset}")
        start += count
    if start + length > limit:
        raise ValueError(f"truncated DER element at offset {offset}")
    return start, start + length


def _certificate_layout(der: bytes) -> _CertificateLayout:
    """Locate the TBSCertificate, its serialNumber contents and both signature AlgorithmIdentifiers."""
    tbs_start, certificate_end = _der_element(der, 0, _SEQUENCE, len(der))
    if certificate_end != len(der):
        raise ValueError("trailing data after the certificate")
    offset, tbs_end = _der_element(der, tbs_start, _SEQUENCE, certificate_end)
    if offset < tbs_end and der[offset] == _EXPLICIT_VERSION:
        offset = _der_element(der, offset, _EXPLICIT_VERSION, tbs_end)[1]
    serial_start, serial_end = _der_element(der, offset, _INTEGER, tbs_end)
    if serial_start == serial_end:
        raise ValueError("empty serialNumber")
    inner_algorithm_end = _der_element(der, serial_end, _SEQUENCE, tbs_end)[1]
    issuer_start, issuer_end = _der_element(der, inner_algorithm_end, _SEQUENCE, tbs_end)
    validity_end = _der_element(der, issuer_end, _SEQUENCE, tbs_end)[1]
    subject_start, subject_end = _der_element(der, validity_end, _SEQUENCE, tbs_end)
    outer_algorithm_end = _der_element(der, tbs_end, _SEQUENCE, certificate_end)[1]
    signature_start, signature_end = _der_element(der, outer_algorithm_end, _BIT_STRING, certificate_end)
    if signature_end != certificate_end:
        raise ValueError("unexpected data after the signature")
    if signature_start == signature_end or der[signature_start] != 0:
        raise ValueError("the signature is not a whole number of octets")
    return _CertificateLayout(
        tbs=slice(tbs_start, tbs_end),
        serial=slice(serial_start, serial_end),
        inner_algorithm=slice(serial_end, inner_algorithm_end),
        outer_algorithm=slice(tbs_end, outer_algorithm_end),
        issuer=slice(issuer_start, issuer_end),
        subject=slice(subject_start, subject_end),
    )


# X.680 41.4: what a PrintableString may hold.
_PRINTABLE_STRING = 0x13
_PRINTABLE_CHARACTERS = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 '()+,-./:=?")
_SET = 0x31
_OBJECT_IDENTIFIER = 0x06


@dataclass(frozen=True, slots=True)
class _InvalidPrintableString:
    """A name attribute value tagged PrintableString that holds other characters."""

    in_subject: bool
    rdn: int
    attribute: int
    contents: slice


def _invalid_printable_strings(der: bytes, layout: _CertificateLayout) -> list[_InvalidPrintableString]:
    """Every such value in the issuer and the subject, in the order they sit in the certificate."""
    found = []
    for in_subject, name in ((False, layout.issuer), (True, layout.subject)):
        offset, rdn = name.start, 0
        while offset < name.stop:
            attribute_offset, rdn_end = _der_element(der, offset, _SET, name.stop)
            attribute = 0
            while attribute_offset < rdn_end:
                type_start, attribute_end = _der_element(der, attribute_offset, _SEQUENCE, rdn_end)
                value_offset = _der_element(der, type_start, _OBJECT_IDENTIFIER, attribute_end)[1]
                value_start, value_end = _der_contents(der, value_offset, attribute_end)
                if value_end != attribute_end:
                    raise ValueError("unexpected data after an attribute value")
                if der[value_offset] == _PRINTABLE_STRING and not _PRINTABLE_CHARACTERS.issuperset(
                    der[value_start:value_end]
                ):
                    found.append(_InvalidPrintableString(in_subject, rdn, attribute, slice(value_start, value_end)))
                attribute_offset = attribute_end
                attribute += 1
            offset = rdn_end
            rdn += 1
    return found


def _decoded_subject(der: bytes, twin_subject: x509.Name, strings: list[_InvalidPrintableString]) -> x509.Name | None:
    """The twin's subject with each refused string decoded from the issued octets: UTF-8, else Latin-1."""
    if not strings:
        return twin_subject
    decoded = {}
    for string in strings:
        contents = der[string.contents]
        try:
            decoded[string.rdn, string.attribute] = contents.decode("utf-8")
        except UnicodeDecodeError:
            decoded[string.rdn, string.attribute] = contents.decode("latin-1")
    # Every refused string must sit in the twin's subject where the walk saw it.
    positions = {(rdn, index) for rdn, relative in enumerate(twin_subject.rdns) for index in range(len(relative))}
    if not decoded.keys() <= positions:
        return None
    try:
        return x509.Name(
            [
                x509.RelativeDistinguishedName(
                    [
                        x509.NameAttribute(attribute.oid, decoded[rdn, index]) if (rdn, index) in decoded else attribute
                        for index, attribute in enumerate(relative)
                    ]
                )
                for rdn, relative in enumerate(twin_subject.rdns)
            ]
        )
    except ValueError:
        return None
