"""Web page fetches trust the certificates an administrator adds, and only those.

``web_fetch_trusted_ca_certificates`` (Settings > Access and security, or its
variable) ADDS PEM certificates to the verification of the pinned fetches
(``core/pinned_request.py``): a host with a self-signed certificate is reached
once that certificate is listed, and verification is never turned off. The
TLS handshakes run against a real local server; the destination policy, which
refuses loopback, is replaced by a stand-in resolver for that one host.
"""

from __future__ import annotations

import datetime
import socket
import ssl
import threading
from collections.abc import Iterator

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app.core import pinned_request
from app.core import security_options as so
from app.core.config import parse_trusted_certificates
from app.core.security_controls import format_value

HOST = "lab.internal.test"


def _self_signed(common_name: str) -> tuple[str, bytes]:
    """A self-signed certificate for *common_name* (PEM) and its key (PEM)."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(common_name)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return certificate.public_bytes(serialization.Encoding.PEM).decode("ascii"), key_pem


@pytest.fixture
def stored(monkeypatch):
    """Publish a stored value of the option, nothing else stored, no variable set."""
    monkeypatch.delenv("WEB_FETCH_TRUSTED_CA_CERTIFICATES", raising=False)
    monkeypatch.setattr(so, "_environment_fields", lambda: frozenset())
    monkeypatch.setattr(so, "_stored", {})
    pinned_request._ssl_context.cache_clear()

    def publish(value: str) -> None:
        monkeypatch.setattr(so, "_stored", {"web_fetch_trusted_ca_certificates": value})

    yield publish
    pinned_request._ssl_context.cache_clear()


@pytest.fixture
def tls_server(tmp_path) -> Iterator[tuple[str, int]]:
    """A one-thread HTTPS server on 127.0.0.1 serving *HOST* with a self-signed certificate:
    its certificate (PEM) and its port."""
    certificate, key = _self_signed(HOST)
    (tmp_path / "cert.pem").write_text(certificate)
    (tmp_path / "key.pem").write_bytes(key)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(tmp_path / "cert.pem", tmp_path / "key.pem")
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    stop = threading.Event()

    def serve() -> None:
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                raw, _ = listener.accept()
            except TimeoutError:
                continue
            try:
                with context.wrap_socket(raw, server_side=True) as conn:
                    conn.recv(65536)
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            except (ssl.SSLError, OSError):
                raw.close()  # the client refused the certificate

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield certificate, port
    stop.set()
    thread.join(timeout=2)
    listener.close()


@pytest.fixture
def resolves_to_loopback(monkeypatch):
    """*HOST* resolves to 127.0.0.1, admitted for this test only: the TLS part is under test."""

    def resolve(host: str, port: int | None = None) -> list[str]:
        assert host == HOST
        return ["127.0.0.1"]

    monkeypatch.setattr(pinned_request, "resolve_pinned_addresses", resolve)


async def _fetch(port: int) -> httpx.Response:
    async with pinned_request.pinned_client(timeout=5) as client:
        response = await pinned_request.send_pinned(client, "GET", f"https://{HOST}:{port}/")
        await response.aread()
        return response


# ---------------------------------------------------------------------------
# Verification against a real TLS server
# ---------------------------------------------------------------------------


async def test_a_default_install_refuses_a_self_signed_host(stored, tls_server, resolves_to_loopback):
    _, port = tls_server
    with pytest.raises(httpx.ConnectError) as caught:
        await _fetch(port)
    assert pinned_request._tls_failure(caught.value)


async def test_a_listed_self_signed_certificate_is_trusted(stored, tls_server, resolves_to_loopback):
    certificate, port = tls_server
    stored(parse_trusted_certificates(certificate, "TEST"))
    response = await _fetch(port)
    assert response.status_code == 200
    assert response.text == "ok"


async def test_another_certificate_listed_still_refuses_the_host(stored, tls_server, resolves_to_loopback):
    _, port = tls_server
    other, _ = _self_signed(HOST)
    stored(parse_trusted_certificates(other, "TEST"))
    with pytest.raises(httpx.ConnectError):
        await _fetch(port)


async def test_a_listed_certificate_never_vouches_for_another_name(stored, tls_server, monkeypatch):
    # The name is still checked: the listed certificate names HOST, the
    # request names another host served by the same server.
    certificate, port = tls_server
    stored(parse_trusted_certificates(certificate, "TEST"))
    monkeypatch.setattr(pinned_request, "resolve_pinned_addresses", lambda host, port=None: ["127.0.0.1"])
    async with pinned_request.pinned_client(timeout=5) as client:
        with pytest.raises(httpx.ConnectError):
            await pinned_request.send_pinned(client, "GET", f"https://other.internal.test:{port}/")


def test_the_context_always_verifies(stored):
    certificate, _ = _self_signed(HOST)
    for value in ("", parse_trusted_certificates(certificate, "TEST")):
        stored(value)
        context = pinned_request.fetch_ssl_context()
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True


def test_the_context_follows_the_value(stored):
    certificate, _ = _self_signed(HOST)
    base = pinned_request.fetch_ssl_context()
    stored(parse_trusted_certificates(certificate, "TEST"))
    added = pinned_request.fetch_ssl_context()
    assert added is not base
    assert added.cert_store_stats()["x509_ca"] == base.cert_store_stats()["x509_ca"] + 1
    stored("")
    assert pinned_request.fetch_ssl_context() is base


# ---------------------------------------------------------------------------
# The value an administrator sets
# ---------------------------------------------------------------------------


def test_empty_is_none():
    assert parse_trusted_certificates("", "X") == ""
    assert parse_trusted_certificates("  \n", "X") == ""
    assert parse_trusted_certificates(None, "X") == ""


def test_certificates_are_normalised_and_deduplicated():
    first, _ = _self_signed("a.internal.test")
    second, _ = _self_signed("b.internal.test")
    value = parse_trusted_certificates(f"\r\n{first}\r\n{second}\n{first}", "X")
    assert value.count("-----BEGIN CERTIFICATE-----") == 2
    assert "\r" not in value


@pytest.mark.parametrize(
    "text",
    [
        "not a certificate",
        "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n",
    ],
)
def test_what_is_not_a_certificate_is_refused(text):
    with pytest.raises(ValueError):
        parse_trusted_certificates(text, "X")


def test_text_around_the_certificates_is_refused():
    certificate, _ = _self_signed(HOST)
    with pytest.raises(ValueError, match="PEM certificates only"):
        parse_trusted_certificates(f"my lab CA\n{certificate}", "X")


def test_a_private_key_is_refused_by_name():
    certificate, key = _self_signed(HOST)
    with pytest.raises(ValueError, match="private key"):
        parse_trusted_certificates(certificate + key.decode("ascii"), "X")


def test_the_option_validates_a_write():
    option = so.SECURITY_OPTIONS["web_fetch_trusted_ca_certificates"]
    certificate, _ = _self_signed(HOST)
    assert option.normalize(certificate).startswith("-----BEGIN CERTIFICATE-----")
    assert option.normalize("") == ""
    with pytest.raises(ValueError):
        option.normalize(["a list"])
    with pytest.raises(ValueError):
        option.normalize("not a certificate")


def test_the_variable_is_validated_at_boot(monkeypatch):
    from app.core.config import Settings

    monkeypatch.setenv("WEB_FETCH_TRUSTED_CA_CERTIFICATES", "not a certificate")
    with pytest.raises(ValueError, match="WEB_FETCH_TRUSTED_CA_CERTIFICATES"):
        Settings()


def test_a_default_install_trusts_no_extra_certificate():
    assert so.SECURITY_OPTIONS["web_fetch_trusted_ca_certificates"].default() == ""


def test_the_audit_and_the_audit_log_show_a_count_never_the_certificate():
    from app.services.security_options import _display

    certificate, _ = _self_signed(HOST)
    value = parse_trusted_certificates(certificate, "X")
    assert format_value(value) == "1 certificate"
    assert _display(value) == "1 certificate"
    assert format_value("") == "empty"


def test_the_audit_recommends_emptying_the_field():
    text = so.where_to_set("web_fetch_trusted_ca_certificates", "")
    assert text.startswith('empty the "Trusted certificates for web fetches" field')


def test_the_requested_url_is_never_fetched_with_verification_off():
    # No ``verify=False`` and no CERT_NONE anywhere on the pinned path.
    import inspect

    source = inspect.getsource(pinned_request)
    assert "verify=False" not in source
    assert "CERT_NONE" not in source
    assert "check_hostname = False" not in source
