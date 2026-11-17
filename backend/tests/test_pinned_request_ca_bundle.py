"""A pinned request trusts the CA bundle the environment names, and still ignores its proxies.

``pinned_client()`` builds its client with ``trust_env=False`` so that no
environment proxy resolves the name the policy checked. httpx reads
``SSL_CERT_FILE`` / ``SSL_CERT_DIR`` only for a client that trusts the
environment, so the SSL context is built explicitly: a deployment with a
private CA or a TLS-inspecting egress gateway keeps verifying certificates
against it. What is pinned here, over a real TLS handshake with a local
server: the CA named by ``SSL_CERT_FILE`` is trusted (and only it), the name
is still the one verified, and a proxy set in the environment is not used.
A deployment that set a proxy is told once at startup, by variable name only.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import socket
import ssl
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from app.core import pinned_request
from app.core.pinned_request import (
    environment_proxy_variables,
    log_environment_proxy_bypass,
    pinned_client,
    send_pinned,
)

HOST = "pinned.example"
_ENV = (
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NO_PROXY",
    "no_proxy",
    *(name for variable in pinned_request.PROXY_VARIABLES for name in (variable, variable.lower())),
)


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _certificate(
    subject: str, key, issuer: str, issuer_key, *, ca: bool, san: str | None = None
) -> x509.Certificate:
    now = datetime.datetime.now(datetime.UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(subject))
        .issuer_name(_name(issuer))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(hours=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), critical=False
        )
    )
    if ca:
        builder = builder.add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
    else:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(san or subject)]), critical=False
        ).add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
    return builder.sign(issuer_key, hashes.SHA256())


def _pem(certificate: x509.Certificate) -> bytes:
    return certificate.public_bytes(serialization.Encoding.PEM)


@pytest.fixture
def private_ca(tmp_path: Path) -> dict[str, Path]:
    """A private CA, a server certificate it issued for ``HOST``, and an unrelated CA."""
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = _certificate("XTM test private CA", ca_key, "XTM test private CA", ca_key, ca=True)
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = _certificate(HOST, leaf_key, "XTM test private CA", ca_key, ca=False)
    other_key = ec.generate_private_key(ec.SECP256R1())
    other = _certificate("XTM test other CA", other_key, "XTM test other CA", other_key, ca=True)

    paths = {
        "ca": tmp_path / "private-ca.pem",
        "other_ca": tmp_path / "other-ca.pem",
        "cert": tmp_path / "server.pem",
        "key": tmp_path / "server.key",
    }
    paths["ca"].write_bytes(_pem(ca))
    paths["other_ca"].write_bytes(_pem(other))
    paths["cert"].write_bytes(_pem(leaf))
    paths["key"].write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return paths


@pytest.fixture
async def tls_server(private_ca):
    """A local HTTPS server presenting the private CA's certificate for ``HOST``."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(private_ca["cert"], private_ca["key"])
    requests: list[bytes] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            requests.append(head)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        except (ssl.SSLError, ConnectionError, asyncio.IncompleteReadError):
            pass  # the client refused the certificate
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=context)
    port = server.sockets[0].getsockname()[1]
    try:
        yield {"port": port, "requests": requests}
    finally:
        server.close()
        await server.wait_closed()


@pytest.fixture
def resolves_to_loopback(monkeypatch):
    """``HOST`` resolves to the local server, and the policy admits it (the test's only exemption)."""

    def _getaddrinfo(host, port, *args, **kwargs):
        if host != HOST:
            raise socket.gaierror("unknown")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo)
    monkeypatch.setattr(pinned_request, "forbidden_address_reason", lambda address: None)


async def _get(port: int) -> httpx.Response:
    async with pinned_client(timeout=5.0) as client:
        response = await send_pinned(client, "GET", f"https://{HOST}:{port}/")
        await response.aread()
        return response


@pytest.mark.asyncio
async def test_the_ca_named_by_ssl_cert_file_is_trusted_and_the_environment_proxy_ignored(
    monkeypatch, private_ca, tls_server, resolves_to_loopback
):
    monkeypatch.setenv("SSL_CERT_FILE", str(private_ca["ca"]))
    # A proxy nobody listens on: a client that used it could not connect.
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")

    response = await _get(tls_server["port"])

    assert (response.status_code, response.text) == (200, "ok")
    assert str(response.url) == f"https://{HOST}:{tls_server['port']}/"
    (request,) = tls_server["requests"]
    assert request.startswith(b"GET / HTTP/1.1\r\n"), "sent straight to the server, not as a proxy request"
    assert f"\r\nHost: {HOST}:{tls_server['port']}\r\n".encode() in request


@pytest.mark.asyncio
async def test_without_ssl_cert_file_the_private_ca_is_not_trusted(
    private_ca, tls_server, resolves_to_loopback
):
    with pytest.raises(httpx.ConnectError) as refused:
        await _get(tls_server["port"])
    assert pinned_request._tls_failure(refused.value)
    assert tls_server["requests"] == []


@pytest.mark.asyncio
async def test_ssl_cert_file_replaces_the_bundle_another_ca_is_refused(
    monkeypatch, private_ca, tls_server, resolves_to_loopback
):
    monkeypatch.setenv("SSL_CERT_FILE", str(private_ca["other_ca"]))
    with pytest.raises(httpx.ConnectError) as refused:
        await _get(tls_server["port"])
    assert pinned_request._tls_failure(refused.value)
    assert tls_server["requests"] == []


def test_ssl_cert_dir_is_read_as_the_ca_directory(monkeypatch, tmp_path):
    calls: list[dict] = []
    real = ssl.create_default_context

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return real()

    monkeypatch.setattr(ssl, "create_default_context", spy)
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path))
    client = pinned_client(timeout=5.0)
    assert calls == [{"capath": str(tmp_path)}]
    assert client._trust_env is False


@pytest.mark.parametrize("variable", ["HTTP_PROXY", "https_proxy", "ALL_PROXY"])
def test_a_proxy_variable_is_reported_at_startup_by_name_only(monkeypatch, caplog, variable):
    monkeypatch.setenv(variable, "http://user:proxy-secret@proxy.internal:3128")
    caplog.set_level(logging.WARNING, logger=pinned_request.logger.name)

    log_environment_proxy_bypass()

    (record,) = caplog.records
    assert record.levelno == logging.WARNING
    assert record.args == (variable.upper(),)
    assert "proxy-secret" not in record.getMessage()
    assert "proxy.internal" not in record.getMessage()


def test_several_proxy_variables_are_named_together(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.internal:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    assert environment_proxy_variables() == ["HTTP_PROXY", "HTTPS_PROXY"]


@pytest.mark.parametrize("value", [None, "", "   "])
def test_no_proxy_variable_means_no_startup_warning(monkeypatch, caplog, value):
    if value is not None:
        monkeypatch.setenv("HTTPS_PROXY", value)
    caplog.set_level(logging.DEBUG, logger=pinned_request.logger.name)
    log_environment_proxy_bypass()
    assert caplog.records == []
