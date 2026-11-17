"""Client certificates (mutual TLS): validation, description, TLS contexts."""

from __future__ import annotations

import ssl

import pytest

from app.integrations import client_certificate as cc
from app.integrations.client_certificate import (
    ClientCertificateError,
    certificate_id,
    certificate_info,
    client_ssl_context,
    parse_client_certificate,
)
from tests.client_certificates import make_client_certificate


@pytest.fixture(autouse=True)
def _fresh():
    cc.clear_context_cache()
    yield
    cc.clear_context_cache()


def test_a_valid_pair_is_described():
    certificate, key = make_client_certificate("robot.acme.test")
    info = parse_client_certificate(certificate, key)
    assert info.subject == info.issuer == "CN=robot.acme.test"
    assert not info.expired and info.chain_length == 1
    assert len(info.fingerprint_sha256.split(":")) == 32


def test_a_chain_is_accepted_with_the_client_first():
    certificate, key = make_client_certificate("leaf.acme.test")
    issuer, _issuer_key = make_client_certificate("ca.acme.test")
    assert parse_client_certificate(certificate + issuer, key).chain_length == 2
    with pytest.raises(ClientCertificateError, match="first certificate"):
        parse_client_certificate(issuer + certificate, key)


@pytest.mark.parametrize(
    ("certificate", "key", "passphrase", "message"),
    [
        ("", "k", None, "Paste the client certificate"),
        ("hello", "k", None, "PEM format"),
        ("-----BEGIN CERTIFICATE-----\nnope\n-----END CERTIFICATE-----", "k", None, "not a valid PEM certificate"),
        (None, "", None, "Paste the private key"),
        (None, "not a key", None, "PEM format"),
    ],
)
def test_malformed_input(certificate, key, passphrase, message):
    valid_certificate, _ = make_client_certificate()
    with pytest.raises(ClientCertificateError, match=message):
        parse_client_certificate(certificate if certificate is not None else valid_certificate, key, passphrase)


def test_passphrases():
    certificate, key = make_client_certificate(passphrase="s3cret")
    with pytest.raises(ClientCertificateError, match="encrypted: enter its passphrase"):
        parse_client_certificate(certificate, key)
    with pytest.raises(ClientCertificateError, match="check the passphrase"):
        parse_client_certificate(certificate, key, "wrong")
    assert parse_client_certificate(certificate, key, "s3cret").subject == "CN=agent.acme.test"
    plain_certificate, plain_key = make_client_certificate()
    with pytest.raises(ClientCertificateError, match="not encrypted"):
        parse_client_certificate(plain_certificate, plain_key, "s3cret")


def test_an_expired_certificate_is_refused():
    certificate, key = make_client_certificate(days=-3)
    with pytest.raises(ClientCertificateError, match=r"The certificate expired on \d{4}-\d{2}-\d{2}\."):
        parse_client_certificate(certificate, key)


@pytest.mark.parametrize(("days", "soon"), [(10, True), (29, True), (45, False), (400, False)])
def test_a_certificate_near_its_end_is_flagged(days, soon):
    info = parse_client_certificate(*make_client_certificate(days=days))
    assert not info.expired and info.expires_soon is soon


def test_a_stored_certificate_that_expired_stops_being_presented():
    certificate, key = make_client_certificate(days=-3)
    extra = {"client_certificate": certificate, "client_private_key": key}
    for _attempt in range(2):  # built, then cached: refused both times
        with pytest.raises(ClientCertificateError, match="expired on"):
            client_ssl_context(extra)
    assert certificate_info(extra).expired


def test_contexts_are_built_once_per_pair():
    certificate, key = make_client_certificate(passphrase="pw")
    extra = {"client_certificate": certificate, "client_private_key": key, "client_private_key_password": "pw"}
    context = client_ssl_context(extra)
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED  # servers are still verified
    assert client_ssl_context(dict(extra)) is context
    assert client_ssl_context({}) is None
    assert certificate_info(extra).subject == "CN=agent.acme.test"
    assert certificate_info({"client_certificate": certificate}) is None
    assert certificate_id(extra) and certificate_id(extra) != certificate_id({**extra, "client_private_key_password": "x"})
    with pytest.raises(ClientCertificateError):
        client_ssl_context({**extra, "client_private_key_password": "wrong"})
