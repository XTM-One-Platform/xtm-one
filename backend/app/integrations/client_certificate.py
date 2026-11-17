# SPDX-License-Identifier: Apache-2.0

"""Client certificates (mutual TLS) for integrations that call user-supplied APIs.

An OpenAPI integration can hold a client certificate: its PEM certificate
chain and private key (optionally encrypted with a passphrase) live in the
integration's encrypted ``extra_config`` under :data:`CERTIFICATE_KEY`,
:data:`PRIVATE_KEY_KEY` and :data:`PASSPHRASE_KEY`. Every request the
integration makes — its tools and its OAuth token requests — then presents
the certificate during the TLS handshake.

* :func:`parse_client_certificate` validates what a user pastes: PEM
  certificates, a private key that matches the first (leaf) certificate, and
  the passphrase that opens the key. Its message is safe to show.
* :func:`client_ssl_context` builds the ``ssl.SSLContext`` the transports
  use. Python's ``ssl`` loads a key pair only from files, so the context is
  built from a private temporary directory that is removed as soon as the key
  is loaded; contexts are cached by the fingerprint of what they hold.
* :func:`certificate_info` describes the stored certificate (subject, issuer,
  validity) — never the key.

An expired certificate is refused, when pasted and when a stored one is used,
with a message saying so (a server would refuse it with a bare TLS alert); one
expiring within :data:`EXPIRY_WARNING_DAYS` is flagged to the UI as
``expires_soon``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import ssl
import tempfile
import threading
from collections import OrderedDict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization

logger = logging.getLogger(__name__)

CERTIFICATE_KEY = "client_certificate"
PRIVATE_KEY_KEY = "client_private_key"
PASSPHRASE_KEY = "client_private_key_password"  # noqa: S105 - config key name, not a secret
CERTIFICATE_KEYS = (CERTIFICATE_KEY, PRIVATE_KEY_KEY, PASSPHRASE_KEY)

_MAX_CERTIFICATE_CHARS = 64 * 1024
_MAX_KEY_CHARS = 32 * 1024
_MAX_PASSPHRASE_CHARS = 1024
_MAX_CACHED_CONTEXTS = 64
#: A certificate this close to its end is flagged as ``expires_soon``.
EXPIRY_WARNING_DAYS = 30


class ClientCertificateError(ValueError):
    """A certificate or key that cannot be used. The message is safe to show."""


@dataclass(frozen=True)
class CertificateInfo:
    """What the UI shows about a client certificate."""

    subject: str
    issuer: str
    not_before: str
    not_after: str
    fingerprint_sha256: str
    expired: bool
    #: Expires within :data:`EXPIRY_WARNING_DAYS`.
    expires_soon: bool
    #: Certificates in the chain, the leaf included.
    chain_length: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _load_certificates(pem: str) -> list[x509.Certificate]:
    text = (pem or "").strip()
    if not text:
        raise ClientCertificateError("Paste the client certificate (PEM).")
    if len(text) > _MAX_CERTIFICATE_CHARS:
        raise ClientCertificateError("The certificate is too large.")
    if "-----BEGIN CERTIFICATE-----" not in text:
        raise ClientCertificateError(
            "The certificate must be in PEM format (-----BEGIN CERTIFICATE-----)."
        )
    try:
        certificates = x509.load_pem_x509_certificates(text.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        raise ClientCertificateError("The certificate is not a valid PEM certificate.") from None
    if not certificates:
        raise ClientCertificateError("The certificate is not a valid PEM certificate.")
    return certificates


def _load_private_key(pem: str, passphrase: str | None) -> Any:
    text = (pem or "").strip()
    if not text:
        raise ClientCertificateError("Paste the private key of the certificate (PEM).")
    if len(text) > _MAX_KEY_CHARS:
        raise ClientCertificateError("The private key is too large.")
    if "PRIVATE KEY-----" not in text:
        raise ClientCertificateError(
            "The private key must be in PEM format (-----BEGIN PRIVATE KEY-----)."
        )
    if passphrase is not None and len(passphrase) > _MAX_PASSPHRASE_CHARS:
        raise ClientCertificateError("The passphrase is too long.")
    password = passphrase.encode("utf-8") if passphrase else None
    try:
        return serialization.load_pem_private_key(text.encode("ascii"), password=password)
    except TypeError:
        if password is None:
            raise ClientCertificateError(
                "The private key is encrypted: enter its passphrase."
            ) from None
        raise ClientCertificateError(
            "The private key is not encrypted: leave the passphrase empty."
        ) from None
    except (ValueError, UnicodeEncodeError):
        if password is not None:
            raise ClientCertificateError(
                "The private key could not be opened: check the passphrase."
            ) from None
        raise ClientCertificateError("The private key is not a valid PEM private key.") from None
    except Exception:  # an unsupported key type
        raise ClientCertificateError("This type of private key is not supported.") from None


def _public_der(key_or_certificate: Any) -> bytes:
    """The SubjectPublicKeyInfo of a private key or a certificate."""
    public = key_or_certificate.public_key()
    return public.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def _describe(certificates: list[x509.Certificate]) -> CertificateInfo:
    leaf = certificates[0]
    not_before = leaf.not_valid_before_utc
    not_after = leaf.not_valid_after_utc
    now = datetime.now(UTC)
    return CertificateInfo(
        subject=leaf.subject.rfc4514_string() or "—",
        issuer=leaf.issuer.rfc4514_string() or "—",
        not_before=not_before.isoformat(),
        not_after=not_after.isoformat(),
        fingerprint_sha256=leaf.fingerprint(hashes.SHA256()).hex(":").upper(),
        expired=not_after < now,
        expires_soon=now <= not_after < now + timedelta(days=EXPIRY_WARNING_DAYS),
        chain_length=len(certificates),
    )


def _refuse_expired(not_after: datetime) -> None:
    if not_after < datetime.now(UTC):
        raise ClientCertificateError(f"The certificate expired on {not_after:%Y-%m-%d}.")


def parse_client_certificate(
    certificate: str, private_key: str, passphrase: str | None = None
) -> CertificateInfo:
    """Validate a certificate + key pair; raises :class:`ClientCertificateError`."""
    certificates = _load_certificates(certificate)
    _refuse_expired(certificates[0].not_valid_after_utc)
    key = _load_private_key(private_key, passphrase or None)
    try:
        matches = _public_der(key) == _public_der(certificates[0])
    except Exception:
        matches = False
    if not matches:
        raise ClientCertificateError(
            "The private key does not belong to the certificate (the first certificate "
            "in the chain must be the client's own)."
        )
    # The TLS stack must accept the pair too (it is stricter about formats).
    _build_context(certificate.strip(), private_key.strip(), passphrase or None)
    return _describe(certificates)


def stored_certificate(extra: dict[str, Any] | None) -> tuple[str, str, str | None] | None:
    """``(certificate, key, passphrase)`` stored in an integration's config, or None."""
    if not isinstance(extra, dict):
        return None
    certificate = extra.get(CERTIFICATE_KEY)
    key = extra.get(PRIVATE_KEY_KEY)
    if not isinstance(certificate, str) or not certificate.strip():
        return None
    if not isinstance(key, str) or not key.strip():
        return None
    passphrase = extra.get(PASSPHRASE_KEY)
    return certificate.strip(), key.strip(), passphrase if isinstance(passphrase, str) and passphrase else None


def certificate_info(extra: dict[str, Any] | None) -> CertificateInfo | None:
    """The stored certificate described, or None when there is none (or it is unreadable)."""
    stored = stored_certificate(extra)
    if stored is None:
        return None
    try:
        return _describe(_load_certificates(stored[0]))
    except ClientCertificateError:
        return None


def certificate_id(extra: dict[str, Any] | None) -> str:
    """A stable, non-secret identifier of the stored pair ("" without one)."""
    stored = stored_certificate(extra)
    if stored is None:
        return ""
    return hashlib.sha256("\x1f".join(p or "" for p in stored).encode("utf-8")).hexdigest()


#: Built contexts with their certificate's end of validity, by pair.
_CONTEXTS: OrderedDict[str, tuple[ssl.SSLContext, datetime]] = OrderedDict()
_CONTEXTS_LOCK = threading.Lock()


def _build_context(certificate: str, private_key: str, passphrase: str | None) -> ssl.SSLContext:
    """An SSL context presenting the pair, verifying servers as usual."""
    context = httpx.create_ssl_context()
    directory = tempfile.mkdtemp(prefix="client-cert-")
    try:
        os.chmod(directory, 0o700)
        cert_path = os.path.join(directory, "certificate.pem")
        key_path = os.path.join(directory, "key.pem")
        for path, content in ((cert_path, certificate), (key_path, private_key)):
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                handle.write(content + "\n")
        try:
            context.load_cert_chain(cert_path, key_path, password=passphrase or None)
        except (ssl.SSLError, OSError, ValueError) as exc:
            logger.info("Client certificate rejected by the TLS stack: %s", type(exc).__name__)
            raise ClientCertificateError(
                "The certificate and key cannot be used for TLS: "
                + (getattr(exc, "reason", None) or "unsupported format").replace("_", " ").lower()
                + "."
            ) from None
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    return context


def client_ssl_context(extra: dict[str, Any] | None) -> ssl.SSLContext | None:
    """The SSL context presenting the stored certificate, or None without one.

    Raises :class:`ClientCertificateError` when the stored pair is unusable,
    and once the certificate has expired.
    """
    stored = stored_certificate(extra)
    if stored is None:
        return None
    key = certificate_id(extra)
    with _CONTEXTS_LOCK:
        cached = _CONTEXTS.get(key)
        if cached is not None:
            _CONTEXTS.move_to_end(key)
    if cached is None:
        not_after = _load_certificates(stored[0])[0].not_valid_after_utc
        cached = (_build_context(*stored), not_after)
        with _CONTEXTS_LOCK:
            _CONTEXTS[key] = cached
            while len(_CONTEXTS) > _MAX_CACHED_CONTEXTS:
                _CONTEXTS.popitem(last=False)
    context, not_after = cached
    _refuse_expired(not_after)
    return context


def clear_context_cache() -> None:
    """Forget every built context (tests)."""
    with _CONTEXTS_LOCK:
        _CONTEXTS.clear()
