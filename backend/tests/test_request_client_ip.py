"""The one client-address rule, ``app.core.utils.request_client_ip``.

The rate limiter's bucket key, the audit trail's ``ip_address`` and the
webhook miss diagnostics all answer "which address did this request come
from" through it; the access log's ``forwarded_for`` goes through its parse,
``first_forwarded_hop``, and its ``client_ip`` records the peer through the
same ``client_address`` reader:

* ``X-Forwarded-For`` only under ``trust_proxy_headers``, then its leftmost hop;
* otherwise the peer, which uvicorn has corrected when ``FORWARDED_ALLOW_IPS``
  names the proxy;
* always an IP address, never text that is not one: by default exactly as it
  was read (what earlier releases answered, pinned against their code below),
  in canonical form under the opt-in ``CLIENT_IP_STRICT``; the audit column is
  ``String(45)``, and a longer value, which failed the audited write, is
  canonical in both modes.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from starlette.requests import Request
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.api.v1 import webhooks
from app.core import rate_limit, utils
from app.core.config import settings
from app.core.utils import CLIENT_ADDRESS_MAX_LENGTH, first_forwarded_hop, request_client_ip
from app.models.audit_log import AuditLog
from app.services.audit import _get_client_ip

AUDIT_IP_LENGTH = AuditLog.__table__.c.ip_address.type.length


def _request(forwarded_for: str | None = None, peer: str | None = "10.0.0.5") -> Request:
    """A real Starlette request: header lookups are case-insensitive, as in production."""
    headers = []
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode("latin-1")))
    return Request(
        {"type": "http", "headers": headers, "client": (peer, 1234) if peer else None}
    )


def _through_uvicorn(forwarded_for: bytes, trusted_hosts: str) -> Request:
    """The request the app sees once uvicorn's proxy middleware has run."""
    seen = {}

    async def app(scope, receive, send):
        seen["scope"] = scope

    scope = {
        "type": "http",
        "client": ("10.0.0.5", 1234),
        "headers": [(b"x-forwarded-for", forwarded_for)],
    }
    asyncio.run(ProxyHeadersMiddleware(app, trusted_hosts=trusted_hosts)(scope, None, None))
    return Request(seen["scope"])


@pytest.fixture(autouse=True)
def _untrusted_proxy_headers(monkeypatch):
    """The shipped default: forwarding headers are caller-controlled."""
    monkeypatch.setattr(settings, "trust_proxy_headers", False)
    monkeypatch.setattr(settings, "client_ip_strict", False)
    monkeypatch.setattr(utils, "_forged_address_reported", False)


@pytest.fixture
def trusted(monkeypatch):
    monkeypatch.setattr(settings, "trust_proxy_headers", True)


@pytest.fixture
def strict(monkeypatch):
    """The opt-in, CLIENT_IP_STRICT=true."""
    monkeypatch.setattr(settings, "client_ip_strict", True)


def _hop_as_before(forwarded_for: str | None) -> str | None:
    """What earlier releases returned for the hop: ``split(",")[0].strip() or None``."""
    if not forwarded_for:
        return None
    return forwarded_for.split(",")[0].strip() or None


def test_the_length_bound_is_the_audit_column():
    assert CLIENT_ADDRESS_MAX_LENGTH == AUDIT_IP_LENGTH


@pytest.mark.parametrize(
    ("forwarded_for", "expected"),
    [
        (None, None),
        ("", None),
        (" , ", None),
        ("198.51.100.7", "198.51.100.7"),
        (" 198.51.100.7 , 10.0.0.1", "198.51.100.7"),
        ("198.51.100.7:5678", "198.51.100.7"),
        ("[2001:DB8::1]:443, 10.0.0.1", "2001:db8::1"),
        ("[2001:db8::1]", "2001:db8::1"),
        ("2001:DB8:0:0:0:0:0:1", "2001:db8::1"),
        ("::ffff:198.51.100.7", "198.51.100.7"),
        (
            "ffff:ffff:ffff:ffff:ffff:ffff:255.255.255.255",
            "ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff",
        ),
        ("fe80::1%eth0", "fe80::1"),
        ("[fe80::1%eth0]:8080", "fe80::1"),
        # A zone index has no length bound: it must never ride along.
        ("fe80::1%" + "z" * 200, "fe80::1"),
        ("::ffff:198.51.100.7%eth0", "198.51.100.7"),
        # A malformed zone is a malformed address, not its valid prefix.
        ("fe80::1%", None),
        ("[fe80::1%]:443", None),
        ("fe80::1%a%b", None),
        ("198.51.100.7%junk", None),
        ("unknown", None),
        ("evil.example.com", None),
        ("198.51.100.7:http", None),
        ("198.51.100.7:123456", None),
        ("198.51.100.7:\u00b2", None),
        ("[2001:db8::1", None),
        ("[2001:db8::1]x", None),
        ("[2001:db8::1]:", None),
        ("01.2.3.4", None),
        ("1.2.3.4.5", None),
        ("x" * 5000, None),
    ],
)
def test_under_strict_the_hop_is_a_canonical_address_or_nothing(strict, forwarded_for, expected):
    hop = first_forwarded_hop(forwarded_for)
    assert hop == expected
    assert hop is None or len(hop) <= AUDIT_IP_LENGTH


@pytest.mark.parametrize(
    "forwarded_for",
    [
        "198.51.100.7",
        " 198.51.100.7 , 10.0.0.1",
        "198.51.100.7:5678",
        "[2001:DB8::1]:443, 10.0.0.1",
        "[2001:db8::1]",
        "2001:DB8:0:0:0:0:0:1",
        "::ffff:198.51.100.7",
        "fe80::1%eth0",
        "[fe80::1%eth0]:8080",
        "::ffff:198.51.100.7%eth0",
        "fe80::1%12",
        "fe80::1%eth0.100",
        "[fe80::1%br-1a2b]:443",
        "unknown",
        "Unknown, 10.0.0.1",
    ],
)
def test_by_default_a_hop_a_proxy_writes_is_read_as_before(forwarded_for):
    # Ports, brackets, zone ids, case, several hops, spaces, and the unknown a
    # proxy writes for a client it hides: the hop earlier releases returned.
    assert first_forwarded_hop(forwarded_for) == _hop_as_before(forwarded_for)


@pytest.mark.parametrize(
    ("forwarded_for", "expected"),
    [
        (None, None),
        ("", None),
        (" , ", None),
        # Only a client writes these: ignored.
        ("evil.example.com", None),
        ("<script>, 10.0.0.1", None),
        ("198.51.100.7%junk", None),
        ("198.51.100.7:http", None),
        ("x" * 5000, None),
        # An address past the audit column failed the audited write: canonical.
        ("[ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff]:65535", "ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff"),
        ("fe80::1%" + "z" * 200, "fe80::1"),
        # A zone no interface name spells: the address, without it.
        ("fe80::1%\nforged line", "fe80::1"),
        ("[fe80::1% <script>]:443", "fe80::1"),
        ('fe80::1%a"b', "fe80::1"),
    ],
)
def test_by_default_only_what_is_not_an_address_or_too_long_changes(forwarded_for, expected):
    hop = first_forwarded_hop(forwarded_for)
    assert hop == expected
    assert hop is None or len(hop) <= AUDIT_IP_LENGTH


class TestTheTrustGate:
    def test_a_forged_header_is_ignored_by_default(self):
        assert request_client_ip(_request("6.6.6.6", peer="198.51.100.7")) == "198.51.100.7"

    def test_the_leftmost_hop_wins_under_the_flag(self, trusted):
        request = _request("198.51.100.7, 10.0.0.2", peer="10.0.0.1")
        assert request_client_ip(request) == "198.51.100.7"

    def test_a_hop_that_is_not_an_address_falls_back_to_the_peer(self, trusted):
        assert request_client_ip(_request("x" * 200, peer="10.0.0.1")) == "10.0.0.1"

    def test_no_address_at_all_is_none(self, trusted):
        assert request_client_ip(_request("<script>", peer=None)) is None

    def test_by_default_a_hop_with_a_port_keeps_it(self, trusted):
        request = _request("198.51.100.7:5678, 10.0.0.2", peer="10.0.0.1")
        assert request_client_ip(request) == "198.51.100.7:5678"

    def test_under_strict_the_hop_is_canonical(self, trusted, strict):
        request = _request("198.51.100.7:5678, 10.0.0.2", peer="10.0.0.1")
        assert request_client_ip(request) == "198.51.100.7"

    def test_by_default_the_unknown_a_proxy_writes_is_kept(self, trusted):
        # Squid's forwarded_for off: the hop earlier releases keyed and recorded.
        assert request_client_ip(_request("unknown", peer="10.0.0.1")) == "unknown"

    def test_under_strict_the_unknown_falls_back_to_the_peer(self, trusted, strict):
        assert request_client_ip(_request("unknown", peer="10.0.0.1")) == "10.0.0.1"


def test_a_value_that_is_not_an_address_is_warned_about_once(trusted, caplog):
    caplog.set_level(logging.WARNING, logger="app.core.utils")
    request_client_ip(_request("<script>", peer="10.0.0.1"))
    request_client_ip(_request("evil.example.com", peer="10.0.0.1"))

    warnings = [r.getMessage() for r in caplog.records if r.name == "app.core.utils"]
    assert len(warnings) == 1
    # The value itself is never written to the log.
    assert "<script>" not in warnings[0]


def test_the_unknown_a_proxy_writes_is_not_warned_about(trusted, caplog):
    caplog.set_level(logging.WARNING, logger="app.core.utils")
    request_client_ip(_request("unknown", peer="10.0.0.1"))
    assert [r for r in caplog.records if r.name == "app.core.utils"] == []


class TestThePeer:
    def test_by_default_it_is_read_as_uvicorn_left_it(self):
        assert request_client_ip(_request(peer="::ffff:10.0.0.5")) == "::ffff:10.0.0.5"

    def test_under_strict_it_is_canonical(self, strict):
        assert request_client_ip(_request(peer="::ffff:10.0.0.5")) == "10.0.0.5"

    def test_a_peer_that_is_not_an_address_is_none(self):
        assert request_client_ip(_request(peer="testclient")) is None

    def test_the_address_uvicorn_corrected_is_used(self):
        # FORWARDED_ALLOW_IPS names the proxy: uvicorn reads the chain from
        # the end, and the raw header must not shadow what it derived.
        request = _through_uvicorn(b"6.6.6.6, 198.51.100.7", trusted_hosts="10.0.0.5")
        assert request_client_ip(request) == "198.51.100.7"

    def test_a_peer_uvicorn_copied_from_the_header_is_checked_too(self):
        # FORWARDED_ALLOW_IPS="*": uvicorn writes the leftmost hop into
        # scope["client"] without checking that it is an address.
        request = _through_uvicorn(b"x" * 200, trusted_hosts="*")
        assert request.client.host == "x" * 200
        assert request_client_ip(request) is None


@pytest.mark.parametrize(
    ("trust", "forwarded_for", "peer"),
    [
        (False, None, "10.0.0.5"),
        (False, "6.6.6.6", "10.0.0.5"),
        (True, "198.51.100.7, 10.0.0.2", "10.0.0.5"),
        (True, "[2001:DB8::1]:443", "10.0.0.5"),
        (True, "evil" * 20, "10.0.0.5"),
        (True, "unknown", None),
        (False, None, "testclient"),
        (False, None, None),
    ],
)
@pytest.mark.parametrize("strict_mode", [False, True])
def test_every_reader_gives_the_same_answer(monkeypatch, trust, forwarded_for, peer, strict_mode):
    monkeypatch.setattr(settings, "trust_proxy_headers", trust)
    monkeypatch.setattr(settings, "client_ip_strict", strict_mode)
    request = _request(forwarded_for, peer)
    expected = request_client_ip(request)

    assert _get_client_ip(request) == expected
    assert rate_limit._client_ip(request) == rate_limit.client_bucket(expected)
    assert webhooks._webhook_request_source(request)[0] == (expected or "<unknown>")
