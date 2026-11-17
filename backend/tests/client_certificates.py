"""Self-signed client certificates for the mutual TLS tests."""

from __future__ import annotations

import datetime

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def make_client_certificate(
    common_name: str = "agent.acme.test",
    *,
    passphrase: str | None = None,
    days: int = 30,
) -> tuple[str, str]:
    """``(certificate PEM, private key PEM)``, the key encrypted with *passphrase*.

    Valid for *days* more days; a negative number gives one that expired then.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1 - min(days, 0)))
        .not_valid_after(now + datetime.timedelta(days=days))
        .sign(key, hashes.SHA256())
    )
    encryption = (
        serialization.BestAvailableEncryption(passphrase.encode())
        if passphrase
        else serialization.NoEncryption()
    )
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption)
    return certificate.public_bytes(serialization.Encoding.PEM).decode(), key_pem.decode()
