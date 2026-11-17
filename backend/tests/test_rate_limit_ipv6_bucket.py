"""A native IPv6 client is rate-limited per address, or per network by opt-in.

A host or a site is handed a whole /64, so keying the per-IP limiters on the
full address lets one IPv6 client draw a fresh bucket for every request.
``RATE_LIMIT_IPV6_PREFIX`` closes that gap by opt-in (``64`` keys on the
/64); the default, ``128``, keeps one bucket per address exactly as before,
because every device of one /64 (a corporate IPv6 LAN) would otherwise share
one budget. The key is built in one place, ``rate_limit.client_bucket``;
IPv4 (IPv4-mapped included) keeps its full address under every prefix, the
``"unknown"`` bucket is unchanged, and only the key changes - the audit
trail still records the full address.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from app.api.v1 import webhooks
from app.core import rate_limit
from app.core.config import Settings, settings
from app.services.audit import log_audit


class _FakeRedis:
    """Just the two Lua scripts and the TTL read the limiter uses."""

    def __init__(self) -> None:
        self.store: dict[str, int] = {}

    async def eval(self, script, numkeys, key, *args):
        if script == rate_limit.INCR_EXPIRE_LUA:
            self.store[key] = self.store.get(key, 0) + 1
            return self.store[key]
        if script == rate_limit.DECR_IF_POSITIVE_LUA:
            if self.store.get(key, 0) > 0:
                self.store[key] -= 1
            return self.store.get(key, 0)
        raise AssertionError("unexpected script")

    async def ttl(self, key):
        return 60


@pytest.fixture
def redis(monkeypatch):
    fake = _FakeRedis()

    async def _client():
        return fake

    monkeypatch.setattr(rate_limit, "redis_client", _client)
    monkeypatch.setattr(settings, "trust_proxy_headers", False)
    return fake


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_ipv6_prefix", 128)
    monkeypatch.setattr(settings, "client_ip_strict", False)


@pytest.fixture
def per_64(monkeypatch):
    """The opt-in, RATE_LIMIT_IPV6_PREFIX=64."""
    monkeypatch.setattr(settings, "rate_limit_ipv6_prefix", 64)


@pytest.fixture
def strict(monkeypatch):
    """The opt-in, CLIENT_IP_STRICT=true."""
    monkeypatch.setattr(settings, "client_ip_strict", True)


def _from(peer: str, forwarded_for: str | None = None) -> Request:
    headers = []
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode("latin-1")))
    return Request({"type": "http", "headers": headers, "client": (peer, 1234)})


async def _login(request: Request, limit: int) -> None:
    await rate_limit.enforce_rate_limit(request, "login", limit=limit, window_seconds=60)


def test_the_default_is_one_bucket_per_address():
    assert Settings.model_fields["rate_limit_ipv6_prefix"].default == 128


async def test_by_default_every_ipv6_address_is_its_own_bucket(redis):
    # The key main used: every address, in the spelling it arrived in.
    await _login(_from("2001:db8:1:2::a"), limit=1)
    await _login(_from("2001:db8:1:2::b"), limit=1)
    await _login(_from("2001:DB8:1:2:0:0:0:a"), limit=1)
    assert redis.store == {
        "rl:login:2001:db8:1:2::a": 1,
        "rl:login:2001:db8:1:2::b": 1,
        "rl:login:2001:DB8:1:2:0:0:0:a": 1,
    }


async def test_under_strict_one_address_is_one_bucket_whatever_its_spelling(redis, strict):
    await _login(_from("2001:db8:1:2::a"), limit=1)
    with pytest.raises(HTTPException):
        await _login(_from("2001:DB8:1:2:0:0:0:a"), limit=1)
    assert redis.store == {"rl:login:2001:db8:1:2::a": 2}


@pytest.mark.parametrize("prefix", [32, 48, 56, 64, 128])
def test_a_prefix_between_32_and_128_is_accepted(prefix):
    assert Settings(rate_limit_ipv6_prefix=prefix).rate_limit_ipv6_prefix == prefix


@pytest.mark.parametrize("prefix", [-64, 0, 31, 129])
def test_a_prefix_outside_32_to_128_is_refused_at_startup(prefix):
    with pytest.raises(ValidationError, match="RATE_LIMIT_IPV6_PREFIX"):
        Settings(rate_limit_ipv6_prefix=prefix)


async def test_two_addresses_in_one_64_share_a_bucket(redis, per_64):
    await _login(_from("2001:db8:1:2::a"), limit=2)
    await _login(_from("2001:db8:1:2:ffff:ffff:ffff:ffff"), limit=2)
    with pytest.raises(HTTPException) as exc:
        await _login(_from("2001:DB8:1:2:0:0:0:b"), limit=2)
    assert exc.value.status_code == 429
    assert redis.store == {"rl:login:2001:db8:1:2::/64": 3}


async def test_two_64s_do_not_share_a_bucket(redis, per_64):
    await _login(_from("2001:db8:1:2::1"), limit=1)
    await _login(_from("2001:db8:1:3::1"), limit=1)
    assert redis.store == {"rl:login:2001:db8:1:2::/64": 1, "rl:login:2001:db8:1:3::/64": 1}


async def test_a_refund_lands_in_the_bucket_it_was_charged_to(redis, per_64):
    await rate_limit.enforce_rate_limit(
        _from("2001:db8:1:2::a"), "a2a:preauth", limit=5, window_seconds=60
    )
    await rate_limit.refund_rate_limit(_from("2001:db8:1:2::b"), "a2a:preauth")
    assert redis.store == {"rl:a2a:preauth:2001:db8:1:2::/64": 0}


@pytest.mark.parametrize("prefix", [128, 64])
async def test_ipv4_keeps_its_full_address(redis, monkeypatch, prefix):
    monkeypatch.setattr(settings, "rate_limit_ipv6_prefix", prefix)
    await _login(_from("198.51.100.7"), limit=1)
    await _login(_from("198.51.100.8"), limit=1)
    # An IPv4-mapped peer keeps the key it arrived with, as before.
    await _login(_from("::ffff:198.51.100.7"), limit=1)
    assert redis.store == {
        "rl:login:198.51.100.7": 1,
        "rl:login:198.51.100.8": 1,
        "rl:login:::ffff:198.51.100.7": 1,
    }


@pytest.mark.parametrize("prefix", [128, 64])
async def test_under_strict_an_ipv4_mapped_peer_is_its_ipv4_client(redis, strict, monkeypatch, prefix):
    monkeypatch.setattr(settings, "rate_limit_ipv6_prefix", prefix)
    await _login(_from("198.51.100.7"), limit=1)
    with pytest.raises(HTTPException):
        await _login(_from("::ffff:198.51.100.7"), limit=1)
    assert redis.store == {"rl:login:198.51.100.7": 2}


async def test_the_forwarded_hop_is_bucketed_the_same_way(redis, per_64, monkeypatch):
    monkeypatch.setattr(settings, "trust_proxy_headers", True)
    await _login(_from("10.0.0.1", "[2001:db8:1:2::a]:443, 10.0.0.2"), limit=5)
    await _login(_from("10.0.0.1", "2001:db8:1:2::b"), limit=5)
    assert redis.store == {"rl:login:2001:db8:1:2::/64": 2}


@pytest.mark.parametrize(
    ("prefix", "address", "bucket"),
    [
        # 128: the address exactly as request_client_ip() returned it.
        (128, "198.51.100.7", "198.51.100.7"),
        (128, "198.51.100.7:5678", "198.51.100.7:5678"),
        (128, "2001:db8:1:2:3:4:5:6", "2001:db8:1:2:3:4:5:6"),
        (128, "2001:DB8:1:2:0:0:0:B", "2001:DB8:1:2:0:0:0:B"),
        (128, "fe80::1%eth0", "fe80::1%eth0"),
        (128, "::1", "::1"),
        (128, "unknown", "unknown"),
        (64, "198.51.100.7", "198.51.100.7"),
        (64, "198.51.100.7:5678", "198.51.100.7:5678"),
        (64, "::ffff:198.51.100.7", "::ffff:198.51.100.7"),
        (64, "2001:db8:1:2:3:4:5:6", "2001:db8:1:2::/64"),
        (64, "2001:DB8:0:0:1::1", "2001:db8::/64"),
        (64, "[2001:db8:1:2::a]:443", "2001:db8:1:2::/64"),
        # Loopback is a single host: keyed on itself, not on ::/64.
        (64, "::1", "::1"),
        # Link-local: the zone index is dropped, so every link-local
        # neighbour shares fe80::/64.
        (64, "fe80::1", "fe80::/64"),
        (64, "fe80::aaaa:bbbb:cccc:dddd%eth0", "fe80::/64"),
        (56, "2001:db8:1:2ff::1", "2001:db8:1:200::/56"),
        (64, "unknown", "unknown"),
        (128, None, "unknown"),
        (64, "", "unknown"),
    ],
)
def test_client_bucket(monkeypatch, prefix, address, bucket):
    monkeypatch.setattr(settings, "rate_limit_ipv6_prefix", prefix)
    assert rate_limit.client_bucket(address) == bucket


@pytest.mark.parametrize(
    ("strict_mode", "prefix", "first", "second"),
    [
        (False, 128, "fe80::1%eth0", "fe80::2%eth1"),
        (True, 128, "fe80::1", "fe80::2"),
        (False, 64, "fe80::/64", "fe80::/64"),
        (True, 64, "fe80::/64", "fe80::/64"),
    ],
)
def test_link_local_and_loopback_through_a_real_request(monkeypatch, strict_mode, prefix, first, second):
    monkeypatch.setattr(settings, "client_ip_strict", strict_mode)
    monkeypatch.setattr(settings, "rate_limit_ipv6_prefix", prefix)
    # scope["client"] may carry a zone index: kept as read by default, dropped
    # under strict or when the key is a network.
    assert rate_limit._client_ip(_from("fe80::1%eth0")) == first
    assert rate_limit._client_ip(_from("fe80::2%eth1")) == second
    assert rate_limit._client_ip(_from("::1")) == "::1"


class _DB:
    def add(self, obj) -> None:
        self.obj = obj

    async def flush(self) -> None:
        return None


async def test_only_the_key_changes_the_audit_trail_keeps_the_full_address(redis, per_64):
    request = _from("2001:db8:1:2::abcd")
    assert rate_limit._client_ip(request) == "2001:db8:1:2::/64"

    user = SimpleNamespace(id=uuid.UUID(int=0xEEEE), email="auditor@example.com")
    entry = await log_audit(
        _DB(),  # type: ignore[arg-type]
        user=user,  # type: ignore[arg-type]
        action="login",
        entity_type="auth",
        summary="User logged in",
        request=request,
    )
    assert entry.ip_address == "2001:db8:1:2::abcd"
    assert webhooks._webhook_request_source(request)[0] == "2001:db8:1:2::abcd"


@pytest.mark.parametrize("prefix", [128, 64])
def test_the_startup_report_names_the_ipv6_key_once(monkeypatch, caplog, prefix):
    monkeypatch.setattr(settings, "rate_limit_ipv6_prefix", prefix)
    monkeypatch.setattr(rate_limit, "_configuration_reported", False)

    with caplog.at_level("INFO", logger=rate_limit.__name__):
        rate_limit.log_rate_limit_configuration()
        rate_limit.log_rate_limit_configuration()

    (record,) = [r for r in caplog.records if r.name == rate_limit.__name__]
    assert record.levelname == "INFO"
    assert f'"IPv6 rate-limit prefix length" (RATE_LIMIT_IPV6_PREFIX) is {prefix}' in record.getMessage()
    if prefix == 128:
        # Where to set it: the Settings > Access and security field, or the variable that wins.
        assert '"IPv6 rate-limit prefix length" in Settings > Access and security > Security options to 64' in record.getMessage()
        assert "RATE_LIMIT_IPV6_PREFIX=64" in record.getMessage()


async def test_the_security_audit_recommends_the_64_while_the_key_is_per_address():
    from app.api.v1 import security_audit

    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_ipv6_rate_limit_key(findings)

    (finding,) = findings
    assert (finding.id, finding.category, finding.severity, finding.control) == (
        "network.ipv6_rate_limit_per_address",
        "network",
        "info",
        "network.rate_limit_ipv6_prefix",
    )
    assert "RATE_LIMIT_IPV6_PREFIX=64" in (finding.recommendation or "")
    assert "content security policy reports" in finding.description


async def test_a_network_narrower_than_a_64_is_no_pass(monkeypatch):
    # A /96 still hands one client every /96 of its /64: not the control's /64.
    from app.api.v1 import security_audit

    monkeypatch.setattr(settings, "rate_limit_ipv6_prefix", 96)
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_ipv6_rate_limit_key(findings)

    (finding,) = findings
    assert (finding.id, finding.severity) == ("network.ipv6_rate_limit_narrow_network", "info")
    assert "/96" in finding.title
    assert "RATE_LIMIT_IPV6_PREFIX=64" in (finding.recommendation or "")


@pytest.mark.parametrize("prefix", [64, 56, 48])
async def test_the_security_audit_passes_once_the_key_is_a_64_or_wider(monkeypatch, prefix):
    from app.api.v1 import security_audit

    monkeypatch.setattr(settings, "rate_limit_ipv6_prefix", prefix)
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_ipv6_rate_limit_key(findings)

    (finding,) = findings
    assert (finding.id, finding.severity, finding.control) == (
        "network.ipv6_rate_limit_per_prefix",
        "pass",
        "network.rate_limit_ipv6_prefix",
    )
    assert f"/{prefix}" in finding.title


def test_the_audit_runs_the_ipv6_check_next_to_the_proxy_check():
    import inspect

    from app.api.v1 import security_audit

    source = inspect.getsource(security_audit.build_security_audit_report)
    assert source.index("_check_proxy_trust(findings)") < source.index("await _check_ipv6_rate_limit_key(findings)")
