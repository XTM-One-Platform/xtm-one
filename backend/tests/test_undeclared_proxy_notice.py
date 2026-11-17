"""The notice for a reverse proxy whose client address the audit trail cannot read.

The audit trail records the connection's address unless the platform takes the
client address from the request. An audited request carrying
``X-Forwarded-For`` from a peer ``FORWARDED_ALLOW_IPS`` does not name - no
proxy declared, or another one - or naming its client only in ``X-Real-IP`` or
``Forwarded`` is reported: one warning a day for the whole platform, a
Redis marker naming the peer that the security audit reads across replicas.
Nothing it does changes what is recorded.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware, _TrustedHosts

from app.api.v1 import security_audit
from app.core import log_once, rate_limit, security_options, undeclared_proxy
from app.core.config import settings
from app.core.utils import client_address_is_forwarded, forwarded_proto_claim, request_client_ip
from app.models.audit_log import AuditLog
from app.services import audit as audit_service
from app.services.audit import _get_client_ip, log_audit
from tests.notice_store import new_process, share_notices

_LOGGER = undeclared_proxy.__name__
PROXY = "10.42.3.7"
USER = "198.51.100.23"
CONTROL = "network.trust_proxy_headers"
WARNING_ID = "network.client_addresses_undeclared_proxy"
WARNING_TITLE = "The audit trail records a proxy's address, not users'"
CONFIGURATION_DOCS = Path(__file__).resolve().parents[2] / "docs" / "docs" / "deployment" / "configuration.md"
INSTALLATION_DOCS = CONFIGURATION_DOCS.with_name("installation.md")


class FakeRedis:
    """The two commands the notice uses: ``fail`` raises, ``hang`` never answers."""

    def __init__(self, *, fail: bool = False, hang: bool = False) -> None:
        self.fail = fail
        self.hang = hang
        self.store: dict[str, tuple[str, int | None]] = {}
        self.writes = 0

    async def _answer(self) -> None:
        if self.hang:
            await asyncio.Event().wait()
        if self.fail:
            raise ConnectionError("Redis is unreachable")

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        await self._answer()
        self.writes += 1
        self.store[key] = (value, ex)
        return True

    async def get(self, key: str) -> str | None:
        await self._answer()
        stored = self.store.get(key)
        return None if stored is None else stored[0]


class FakeDB:
    """What ``log_audit`` needs of an ``AsyncSession``."""

    def __init__(self) -> None:
        self.added: list = []

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _fresh_process(monkeypatch):
    """No proxy declared, in a process that has reported nothing yet."""
    monkeypatch.setattr(settings, "trust_proxy_headers", False)
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    monkeypatch.setattr(undeclared_proxy, "_next_write_at", 0.0)
    monkeypatch.setattr(undeclared_proxy, "_next_change_at", 0.0)
    monkeypatch.setattr(undeclared_proxy, "_written", None)
    monkeypatch.setattr(undeclared_proxy, "_write_tasks", set())


def _use_redis(monkeypatch, fake: FakeRedis) -> FakeRedis:
    async def _client() -> FakeRedis:
        return fake

    monkeypatch.setattr(undeclared_proxy, "redis_client", _client)
    return fake


@pytest.fixture
def redis(monkeypatch) -> FakeRedis:
    return _use_redis(monkeypatch, FakeRedis())


@pytest.fixture
def clock(monkeypatch) -> SimpleNamespace:
    """The notice's own clock, moved by hand; the event loop keeps the real one."""
    now = SimpleNamespace(value=1000.0)
    monkeypatch.setattr(undeclared_proxy, "time", SimpleNamespace(monotonic=lambda: now.value))
    return now


def _request(
    forwarded_for: str | list[str] | None = USER,
    peer: str | None = PROXY,
    headers: tuple[tuple[str, str], ...] = (),
) -> Request:
    """A request as the platform receives it: one ``X-Forwarded-For`` header per value, from ``peer``."""
    values = [] if forwarded_for is None else [forwarded_for] if isinstance(forwarded_for, str) else forwarded_for
    return Request(
        {
            "type": "http",
            "headers": [(b"x-forwarded-for", value.encode("latin-1")) for value in values]
            + [(name.lower().encode("latin-1"), value.encode("latin-1")) for name, value in headers],
            "client": None if peer is None else (peer, 5555),
        }
    )


def _marker(peer: str | None = PROXY, header: str = "x-forwarded-for") -> str:
    """The marker value a sighting of *peer* writes: header names and an address, never a header value."""
    return json.dumps({"header": header, "peer": peer}, separators=(",", ":"), sort_keys=True)


def _user() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.UUID("00000000-0000-0000-0000-00000000abcd"), email="admin@example.com")


async def _audit(request) -> AuditLog:
    return await log_audit(
        FakeDB(),  # type: ignore[arg-type]
        user=_user(),  # type: ignore[arg-type]
        action="login",
        entity_type="auth",
        summary="User logged in",
        request=request,
    )


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == _LOGGER and r.levelno == logging.WARNING]


async def _settled() -> None:
    await asyncio.gather(*undeclared_proxy._write_tasks)


def _report(request) -> None:
    undeclared_proxy.report_undeclared_proxy(request)


async def _through_uvicorn(
    peer: str,
    *forwarded_for: str,
    trusted_hosts: str = "127.0.0.1",
    headers: tuple[tuple[str, str], ...] = (),
) -> Request:
    """The request the platform receives once the installed uvicorn's proxy middleware has run."""
    seen = {}

    async def app(scope, receive, send):
        seen["scope"] = scope

    scope = {
        "type": "http",
        "client": (peer, 5555),
        "headers": [(b"x-forwarded-for", value.encode()) for value in forwarded_for]
        + [(name.lower().encode(), value.encode()) for name, value in headers],
    }
    # 127.0.0.1 is uvicorn's own default: it trusts loopback, so a same-host proxy is read.
    await ProxyHeadersMiddleware(app, trusted_hosts=trusted_hosts)(scope, None, None)  # type: ignore[arg-type]
    return Request(seen["scope"])


# ---------------------------------------------------------------------------
# The warning
# ---------------------------------------------------------------------------


class TestTheWarning:
    async def test_it_fires_once_and_says_what_to_set(self, redis, caplog):
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            for _ in range(5):
                _report(_request())
            await _settled()
        [message] = _warnings(caplog)
        assert '"Trust proxy headers" (TRUST_PROXY_HEADERS) is off' in message
        assert "FORWARDED_ALLOW_IPS" in message
        assert "address or CIDR" in message
        assert f"relayed it, {PROXY}," in message

    async def test_it_is_said_once_a_day_for_the_whole_platform(self, monkeypatch, redis, caplog):
        """Each uvicorn worker and each MAX_REQUESTS recycle said it again: 5 lines in 30 minutes."""
        notices = share_notices(monkeypatch)
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            for _ in range(5):
                new_process()
                _report(_request())
                await log_once.settle()
            await _settled()
        [message] = _warnings(caplog)
        assert message.endswith(
            "Said once a day for the whole platform; the security audit (Settings > Access and security) reports it too."
        )
        assert "once per process" not in message
        [claim] = notices.claims()
        assert ":undeclared_proxy:x-forwarded-for:" in claim
        assert notices.ttls[claim] == 24 * 60 * 60

    async def test_another_peer_of_the_same_ingress_is_not_another_notice(self, redis, caplog):
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _report(_request(peer=PROXY))
            _report(_request(peer="10.42.3.8"))
            await _settled()
        assert len(_warnings(caplog)) == 1

    async def test_a_restart_on_another_proxy_configuration_says_it_again(self, monkeypatch, redis, caplog):
        """The notice says what the configuration leaves: a new FORWARDED_ALLOW_IPS is a new notice."""
        share_notices(monkeypatch)
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            for forwarded_allow_ips in (None, None, "10.9.0.0/16"):
                if forwarded_allow_ips is not None:
                    monkeypatch.setenv("FORWARDED_ALLOW_IPS", forwarded_allow_ips)
                new_process()
                _report(_request())
                await log_once.settle()
            await _settled()
        first, second = _warnings(caplog)
        assert "while no reverse proxy is declared" in first
        assert "FORWARDED_ALLOW_IPS (10.9.0.0/16) does not name" in second

    async def test_it_never_logs_the_header(self, redis, caplog):
        forged = "6.6.6.6, <script>alert(1)</script>\r\nINFO forged line"
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _report(_request(forwarded_for=forged))
            await _settled()
        [message] = _warnings(caplog)
        for fragment in ("6.6.6.6", "<script>", "forged"):
            assert fragment not in message

    @pytest.mark.parametrize(
        ("peer", "shown"),
        [
            ("fe80::7%<script>", "fe80::7"),
            ("unix:<proxy>", None),
        ],
    )
    async def test_the_peer_is_shown_only_as_an_address(self, redis, caplog, peer, shown):
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _report(_request(peer=peer))
            await _settled()
        [message] = _warnings(caplog)
        assert "<" not in message
        if shown:
            assert f"relayed it, {shown}," in message
        else:
            assert "the peer that relayed it instead of the user's" in message

    def test_outside_an_event_loop_only_the_warning_is_given(self, caplog):
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert _get_client_ip(_request()) == PROXY
        assert len(_warnings(caplog)) == 1
        assert undeclared_proxy._write_tasks == set()


# ---------------------------------------------------------------------------
# When it fires
# ---------------------------------------------------------------------------


class TestWhenItFires:
    async def test_not_without_the_header(self, redis, caplog):
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            for forwarded_for in (None, "", " , ", ["", " "]):
                _report(_request(forwarded_for=forwarded_for))
            await _settled()
        assert _warnings(caplog) == []
        assert redis.writes == 0

    def test_a_request_without_forwarding_headers_reads_no_configuration(self, monkeypatch):
        # Nearly every audited request: one pass over its header names, as on main.
        def _unexpected(*args, **kwargs):
            raise AssertionError("configuration read for a request carrying no forwarding header")

        monkeypatch.setattr(undeclared_proxy, "believed_from_any_peer", _unexpected)
        monkeypatch.setattr(undeclared_proxy, "trusted_proxies", _unexpected)
        request = _request(forwarded_for=None, headers=(("User-Agent", "curl/8"), ("X-Forwarded-Proto", "https")))
        assert undeclared_proxy.unread_client_address(request) is None

    async def test_not_without_a_peer(self, redis, caplog):
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _report(_request(peer=None))
            await _settled()
        assert _warnings(caplog) == []
        assert redis.writes == 0

    @pytest.mark.parametrize(
        "value",
        ["10.42.0.0/16", PROXY, "*", "127.0.0.1, 10.42.0.0/16", "*, 10.42.0.0/16", "0.0.0.0/0"],
    )
    async def test_not_from_a_peer_forwarded_allow_ips_names(self, monkeypatch, redis, caplog, value):
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", value)
        assert undeclared_proxy.proxy_declared() is True
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _report(_request())
            await _settled()
        assert _warnings(caplog) == []
        assert redis.writes == 0

    async def test_not_under_trust_proxy_headers(self, monkeypatch, redis, caplog):
        monkeypatch.setattr(settings, "trust_proxy_headers", True)
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _report(_request())
            await _settled()
        assert _warnings(caplog) == []
        assert redis.writes == 0

    async def test_not_under_trust_proxy_headers_set_in_settings_security(self, redis, caplog):
        security_options.publish_stored({"trust_proxy_headers": True})
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _report(_request())
            await _settled()
        assert _warnings(caplog) == []
        assert redis.writes == 0

    async def test_the_variable_wins_over_the_value_set_in_settings_security(self, monkeypatch, redis, caplog):
        # TRUST_PROXY_HEADERS=false is set: the stored "on" is not applied, so nothing is declared.
        monkeypatch.setattr(settings, "_environment_fields", frozenset({"trust_proxy_headers"}))
        security_options.publish_stored({"trust_proxy_headers": True})
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _report(_request())
            await _settled()
        assert len(_warnings(caplog)) == 1
        assert redis.writes == 1

    @pytest.mark.parametrize(
        "value",
        # Unset, empty, loopback only, a hostname (never resolved), a CIDR with
        # its host bits set, and "*" with spaces: uvicorn trusts no remote peer.
        [None, "", "127.0.0.1", "::1", "127.0.0.0/8, ::1", "ingress-nginx", "10.42.0.1/16", " * "],
    )
    def test_these_declare_no_proxy(self, monkeypatch, value):
        if value is None:
            monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
        else:
            monkeypatch.setenv("FORWARDED_ALLOW_IPS", value)
        assert undeclared_proxy.proxy_declared() is False

    @pytest.mark.parametrize(
        "value",
        [
            "*",
            " * ",
            "*,127.0.0.1",
            "nginx",
            "10.42.0.1/16",
            "10.42.0.0/16",
            "10.42.3.7/32",
            "127.0.0.0/8",
            "::1",
            "",
            " , ",
            "fd00::/8",
            "[fd00::7]",
            "127.0.0.1, 10.42.3.7",
            "0.0.0.0/0",
            "localhost, ::1",
        ],
    )
    def test_the_declaration_is_read_as_uvicorn_reads_it(self, monkeypatch, value):
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", value)
        trusted = _TrustedHosts(value)
        assert undeclared_proxy.proxy_declared() is ("10.42.3.7" in trusted or "fd00::7" in trusted)


class TestAHeaderUvicornAlreadyRead:
    """Pinned against the installed uvicorn, not against memory."""

    async def _through_uvicorn(self, peer: str, *forwarded_for: str, trusted_hosts: str = "127.0.0.1") -> Request:
        return await _through_uvicorn(peer, *forwarded_for, trusted_hosts=trusted_hosts)

    @pytest.mark.parametrize(
        "forwarded_for",
        [USER, f"{USER}:4711", "[2001:db8::7]:443", "[2001:db8::7]", f"203.0.113.9, {USER}"],
    )
    async def test_a_same_host_proxy_is_not_reported(self, redis, caplog, forwarded_for):
        request = await self._through_uvicorn("127.0.0.1", forwarded_for)
        assert request.client is not None and request.client.host != "127.0.0.1"
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _report(request)
            await _settled()
        assert _warnings(caplog) == []
        assert redis.writes == 0

    async def test_every_header_is_read_since_uvicorn_reads_the_last(self, redis, caplog):
        # A proxy that adds its own header line (HAProxy's option forwardfor)
        # after the one the client sent: uvicorn takes the client from the last.
        request = await self._through_uvicorn("127.0.0.1", "192.0.2.10", USER)
        assert request.client is not None and request.client.host == USER
        assert request.headers.get("x-forwarded-for") == "192.0.2.10"
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _report(request)
            await _settled()
        assert _warnings(caplog) == []
        assert redis.writes == 0

    async def test_a_proxy_uvicorn_does_not_trust_is_reported(self, redis, caplog):
        request = await self._through_uvicorn(PROXY, USER)
        assert request.client is not None and request.client.host == PROXY
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _report(request)
            await _settled()
        assert len(_warnings(caplog)) == 1
        assert redis.writes == 1

    async def test_a_same_host_proxy_uvicorn_trusts_nothing_from_is_reported(self, monkeypatch, redis, caplog):
        # FORWARDED_ALLOW_IPS set to empty: uvicorn trusts no peer, loopback included.
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", "")
        request = await self._through_uvicorn("127.0.0.1", USER, trusted_hosts="")
        assert request.client is not None and request.client.host == "127.0.0.1"
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _report(request)
            await _settled()
        [message] = _warnings(caplog)
        assert "relayed it, 127.0.0.1," in message

    @pytest.mark.parametrize(
        "forwarded_for",
        [
            # A client sent the proxy's own address and the proxy appended the client.
            (f"{PROXY}, {USER}",),
            # The client's line names the proxy, the proxy's own line the client:
            # uvicorn reads the last line only, and from a peer it trusts only.
            (PROXY, USER),
        ],
    )
    async def test_an_untrusted_proxy_named_in_the_chain_is_reported(self, redis, caplog, forwarded_for):
        request = await self._through_uvicorn(PROXY, *forwarded_for)
        assert request.client is not None and request.client.host == PROXY
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _report(request)
            await _settled()
        assert len(_warnings(caplog)) == 1
        assert redis.store[undeclared_proxy.MARKER_KEY][0] == _marker()


@pytest.mark.parametrize("trusted_hosts", ["127.0.0.1", PROXY, "10.42.0.0/16", "", "fd00::/8, 127.0.0.1"])
@pytest.mark.parametrize("peer", [PROXY, "127.0.0.1"])
@pytest.mark.parametrize(
    "forwarded_for",
    [
        (USER,),
        (f"{USER}:4711",),
        ("[2001:db8::7]:443",),
        (f"{PROXY}, {USER}",),
        (PROXY, USER),
        (USER, PROXY),
        (USER, " "),
        (f"{USER}, ",),
        ("unknown",),
        (f"{USER}, 127.0.0.1",),
        ("127.0.0.1",),
    ],
)
async def test_it_is_silent_exactly_when_uvicorn_used_the_header(monkeypatch, trusted_hosts, peer, forwarded_for):
    # The installed uvicorn is the oracle: an X-Forwarded-For sighting is made
    # exactly when uvicorn left the peer in place, the line it reads names a
    # client and the peer is not one FORWARDED_ALLOW_IPS names - and it names
    # the socket peer, never an address taken from the header.
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", trusted_hosts)
    request = await _through_uvicorn(peer, *forwarded_for, trusted_hosts=trusted_hosts)
    assert request.client is not None
    replaced = (request.client.host, request.client.port) != (peer, 5555)
    names_a_client = any(hop.strip() for hop in forwarded_for[-1].split(","))
    expected = not replaced and names_a_client and peer not in _TrustedHosts(trusted_hosts)
    sighting = undeclared_proxy.unread_client_address(request)
    assert (sighting is not None) is expected
    if sighting is not None:
        assert sighting == undeclared_proxy.Sighting(peer, "x-forwarded-for")


@pytest.mark.parametrize(
    "hop",
    [
        "203.0.113.7",
        "203.0.113.7:4711",
        "1.2.3.4:+80",
        "host:port",
        "[2001:db8::1]",
        "[2001:db8::1]:443",
        "[2001:db8::1]:x",
        "[2001:db8::1]x",
        "[2001:db8::1",
        "2001:db8::1",
        "unknown",
        "",
    ],
)
def test_a_hop_is_read_the_way_uvicorn_reads_it(hop):
    from uvicorn.middleware.proxy_headers import _parse_host_port

    assert undeclared_proxy._hop_address(hop) == _parse_host_port(hop)


@pytest.mark.parametrize(
    "value",
    ["*", "*,127.0.0.1", "127.0.0.1", "10.42.0.0/16", "10.42.0.1/16", "nginx", "", " , ", "::1, 127.0.0.1", "fd00::/8"],
)
@pytest.mark.parametrize("host", ["127.0.0.1", "10.42.3.7", "192.0.2.10", "fd00::7", "::1", "nginx", "", None])
def test_forwarded_allow_ips_is_read_the_way_uvicorn_reads_it(value, host):
    assert undeclared_proxy.parse_forwarded_allow_ips(value).trusts(host) is (host in _TrustedHosts(value))


class TestAPeerTheDeclarationDoesNotName:
    """A proxy is declared, but the requests come through another peer."""

    @pytest.mark.parametrize("value", ["192.0.2.10", "2001:db8::/32", "10.43.0.0/16", "127.0.0.1, 192.0.2.10"])
    async def test_it_is_reported_naming_the_peer_and_the_list(self, monkeypatch, redis, caplog, value):
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", value)
        assert undeclared_proxy.proxy_declared() is True
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _report(_request())
            await _settled()
        [message] = _warnings(caplog)
        assert f"a peer FORWARDED_ALLOW_IPS ({value}) does not name" in message
        assert f"relayed it, {PROXY}," in message
        assert "add its address or CIDR to FORWARDED_ALLOW_IPS" in message
        assert USER not in message
        assert redis.store[undeclared_proxy.MARKER_KEY][0] == _marker()

    async def test_a_proxy_on_the_docker_host_behind_another_declared_address(self, monkeypatch, redis):
        # FORWARDED_ALLOW_IPS names the proxy host's own address, while the proxy
        # reaches the container from the Compose network's gateway: uvicorn reads
        # nothing, and the audit trail records the gateway for every user.
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", "192.0.2.10")
        request = await _through_uvicorn("172.18.0.1", USER, trusted_hosts="192.0.2.10")
        entry = await _audit(request)
        await _settled()
        assert entry.ip_address == "172.18.0.1"
        [finding] = await _findings()
        assert (finding.id, finding.severity, finding.control) == (WARNING_ID, "warning", CONTROL)
        assert "from 172.18.0.1, a peer FORWARDED_ALLOW_IPS (192.0.2.10) does not name" in finding.description
        recommendation = finding.recommendation or ""
        assert recommendation.startswith("If 172.18.0.1 is a reverse proxy or ingress of yours, add its address")
        assert "a comma-separated list, now 192.0.2.10" in recommendation
        assert "not a Settings > Access and security field" in recommendation
        assert USER not in finding.model_dump_json()

    @pytest.mark.parametrize(("base_url", "https"), [("https://xtm.example.com", True), ("http://xtm.internal", False)])
    async def test_https_through_it_is_named_on_an_https_deployment(self, monkeypatch, redis, base_url, https):
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", "192.0.2.10")
        monkeypatch.setattr(settings, "base_url", base_url)
        _report(_request())
        await _settled()
        [finding] = await _findings()
        assert ("403 https required" in finding.description) is https

    async def test_https_through_it_says_what_the_credential_routes_do(self, monkeypatch, redis):
        # The outcome network.proxy_trust_missing states, by credential_transport_strict.
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", "192.0.2.10")
        monkeypatch.setattr(settings, "base_url", "https://xtm.example.com")
        _report(_request())
        await _settled()
        [default] = await _findings()
        assert (
            "/runners/enroll, /mcp/oauth/register and /mcp/oauth/token log an unconfirmed-transport warning"
            in default.description
        )
        monkeypatch.setattr(settings, "credential_transport_strict", True)
        [strict] = await _findings()
        assert "/runners/enroll refuse it (403 https required)" in strict.description
        assert "/mcp/oauth/register answers invalid_client_metadata" in strict.description
        assert "/mcp/oauth/token answers invalid_request because" in strict.description

    async def test_the_warning_ends_once_the_peer_is_declared(self, monkeypatch, redis):
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", "192.0.2.10")
        _report(_request())
        await _settled()
        assert [f.id for f in await _findings()] == [WARNING_ID]
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", "192.0.2.10, 10.42.0.0/16")
        [finding] = await _findings()
        assert (finding.id, finding.severity) == ("network.client_addresses_declared_proxy", "pass")
        assert "No audited request of the last day, on any replica, came from a peer it does not name" in (
            finding.description
        )


class TestHeadersThePlatformNeverReads:
    """``X-Real-IP`` and ``Forwarded`` name a client the platform never reads."""

    @pytest.mark.parametrize(
        ("headers", "label"),
        [
            ((("X-Real-IP", USER),), "X-Real-IP"),
            ((("Forwarded", f"for={USER};proto=https"),), "Forwarded"),
            ((("Forwarded", 'proto=https, For="[2001:db8::7]"'),), "Forwarded"),
            # Never read, so any header line naming a client counts.
            ((("X-Real-IP", " "), ("X-Real-IP", USER)), "X-Real-IP"),
            ((("Forwarded", "proto=https"), ("Forwarded", f"for={USER}")), "Forwarded"),
        ],
    )
    @pytest.mark.parametrize("declared", [None, "10.42.0.0/16"])
    async def test_a_proxy_naming_the_client_only_there_is_reported(
        self, monkeypatch, redis, caplog, headers, label, declared
    ):
        if declared:
            monkeypatch.setenv("FORWARDED_ALLOW_IPS", declared)
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _report(_request(forwarded_for=None, headers=headers))
            await _settled()
        [message] = _warnings(caplog)
        assert f"named its client in {label} but not in X-Forwarded-For" in message
        assert "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;" in message
        assert ("set FORWARDED_ALLOW_IPS" in message) is (declared is None)
        assert USER not in message and "2001:db8::7" not in message
        assert redis.store[undeclared_proxy.MARKER_KEY][0] == _marker(header=label.lower())

    @pytest.mark.parametrize(
        "headers",
        [
            (("X-Real-IP", "  "),),
            (("Forwarded", "proto=https;host=xtm.example.com"),),
            (("Forwarded", 'for="";proto=https'),),
            (("Forwarded", "x-for=198.51.100.23"),),
            (("Forwarded", 'proto=https;by="for=198.51.100.23"'),),
        ],
    )
    async def test_a_request_naming_no_client_there_is_not(self, redis, caplog, headers):
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _report(_request(forwarded_for=None, headers=headers))
            await _settled()
        assert _warnings(caplog) == []
        assert redis.writes == 0

    async def test_an_x_forwarded_for_uvicorn_read_hides_them(self, redis, caplog):
        request = await _through_uvicorn("127.0.0.1", USER, headers=(("X-Real-IP", USER),))
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _report(request)
            await _settled()
        assert _warnings(caplog) == []

    async def test_a_blank_x_forwarded_for_leaves_them_reported(self, redis, caplog):
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _report(_request(forwarded_for=" , ", headers=(("X-Real-IP", USER),)))
            await _settled()
        [message] = _warnings(caplog)
        assert "named its client in X-Real-IP" in message

    @pytest.mark.parametrize("value", ["*", "0.0.0.0/0"])
    async def test_nothing_is_reported_while_every_peer_is_believed(self, monkeypatch, redis, caplog, value):
        # The security audit's any-peer warning speaks for that state.
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", value)
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            _report(_request(forwarded_for=None, headers=(("X-Real-IP", USER),)))
            await _settled()
        assert _warnings(caplog) == []
        assert redis.writes == 0


# ---------------------------------------------------------------------------
# The marker
# ---------------------------------------------------------------------------


class TestTheMarker:
    async def test_it_is_written_again_within_the_hour_with_a_one_day_ttl(self, redis, clock):
        for _ in range(3):
            _report(_request())
            await _settled()
        assert redis.writes == 1
        assert redis.store == {undeclared_proxy.MARKER_KEY: (_marker(), 24 * 60 * 60)}

        clock.value += 60 * 60 - 1
        _report(_request())
        await _settled()
        assert redis.writes == 1

        # A marker Redis lost (a restart, an eviction) is back within the hour.
        redis.store.clear()
        clock.value += 1
        _report(_request())
        await _settled()
        assert redis.writes == 2
        assert redis.store == {undeclared_proxy.MARKER_KEY: (_marker(), 24 * 60 * 60)}

    async def test_one_write_is_in_flight_at_a_time(self, redis):
        for _ in range(5):
            _report(_request())
        assert len(undeclared_proxy._write_tasks) == 1
        await _settled()
        assert redis.writes == 1

    async def test_it_names_the_peer_and_the_header_never_a_header_value(self, redis):
        _report(_request(forwarded_for=f"6.6.6.6, {USER}"))
        await _settled()
        value, _ttl = redis.store[undeclared_proxy.MARKER_KEY]
        assert json.loads(value) == {"header": "x-forwarded-for", "peer": PROXY}
        assert USER not in value and "6.6.6.6" not in value

    async def test_another_sighting_is_written_within_a_minute_not_an_hour(self, redis, clock):
        _report(_request())
        await _settled()
        _report(_request(peer="10.42.9.9"))
        await _settled()
        assert redis.writes == 1
        clock.value += 60
        _report(_request(peer="10.42.9.9"))
        await _settled()
        assert redis.writes == 2
        assert redis.store[undeclared_proxy.MARKER_KEY][0] == _marker("10.42.9.9")

    async def test_alternating_peers_write_at_most_once_a_minute(self, redis, clock):
        for index in range(20):
            _report(_request(peer=f"10.42.9.{index % 2}"))
            await _settled()
            clock.value += 5
        assert redis.writes == 2

    async def test_the_audit_trail_reports_and_records_the_peer_as_before(self, redis, caplog):
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            entry = await _audit(_request())
            await _settled()
        assert entry.ip_address == PROXY
        assert len(_warnings(caplog)) == 1
        assert redis.writes == 1


class TestTheAuditedRequest:
    async def test_a_redis_error_never_reaches_it(self, monkeypatch, caplog):
        broken = _use_redis(monkeypatch, FakeRedis(fail=True))
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            entry = await _audit(_request())
            await _settled()
        assert entry.ip_address == PROXY
        assert len(_warnings(caplog)) == 1
        assert broken.writes == 0

    async def test_an_unavailable_client_never_reaches_it(self, monkeypatch):
        async def _unavailable():
            raise RuntimeError("Redis sentinel client is not initialized.")

        monkeypatch.setattr(undeclared_proxy, "redis_client", _unavailable)
        entry = await _audit(_request())
        await _settled()
        assert entry.ip_address == PROXY

    async def test_a_hanging_redis_never_holds_it(self, monkeypatch):
        hung = _use_redis(monkeypatch, FakeRedis(hang=True))
        monkeypatch.setattr(undeclared_proxy, "_REDIS_TIMEOUT_SECONDS", 0.05)
        entry = await asyncio.wait_for(_audit(_request()), timeout=1)
        assert entry.ip_address == PROXY
        await asyncio.wait_for(_settled(), timeout=1)
        assert hung.writes == 0

    async def test_a_failed_write_is_retried_later_not_per_request(self, monkeypatch, clock):
        broken = _use_redis(monkeypatch, FakeRedis(fail=True))
        _report(_request())
        await _settled()
        _report(_request())
        assert undeclared_proxy._write_tasks == set()

        broken.fail = False
        clock.value += 5 * 60
        _report(_request())
        await _settled()
        assert broken.writes == 1

    async def test_a_request_the_notice_cannot_read_is_still_recorded(self, redis, caplog):
        class _UnreadableHeaders:
            def getlist(self, key: str) -> list[str]:
                raise RuntimeError("unreadable")

        unreadable = SimpleNamespace(client=SimpleNamespace(host=PROXY), headers=_UnreadableHeaders())
        # A peer that is not text: warned about without an address, nothing raised.
        no_host = SimpleNamespace(client=SimpleNamespace(host=None), headers=_request().headers)
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert (await _audit(unreadable)).ip_address == PROXY
            assert _warnings(caplog) == []
            assert (await _audit(no_host)).ip_address is None
            await _settled()
        [message] = _warnings(caplog)
        assert "the peer that relayed it instead of the user's" in message

    def test_the_response_is_served(self, monkeypatch):
        async def _unreachable():
            raise ConnectionError("Redis is unreachable")

        monkeypatch.setattr(undeclared_proxy, "redis_client", _unreachable)
        app = FastAPI()

        @app.post("/audited")
        async def _audited(request: Request):
            entry = await _audit(request)
            return {"ip": entry.ip_address}

        # A real peer address: the test client's placeholder host is not one.
        response = TestClient(app, client=(PROXY, 50000)).post("/audited", headers={"X-Forwarded-For": USER})
        assert response.status_code == 200
        assert response.json() == {"ip": PROXY}


# ---------------------------------------------------------------------------
# The security audit
# ---------------------------------------------------------------------------


async def _findings() -> list[security_audit.AuditFinding]:
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_forwarded_client_addresses(findings)
    return findings


def _marked(redis: FakeRedis) -> None:
    """The marker another replica left."""
    redis.store[undeclared_proxy.MARKER_KEY] = ("1", undeclared_proxy.MARKER_TTL_SECONDS)


class TestTheSecurityAudit:
    async def test_a_forwarded_request_without_a_declared_proxy_is_a_warning(self, redis):
        _report(_request())
        await _settled()
        [finding] = await _findings()
        assert (finding.id, finding.category, finding.severity, finding.control) == (
            WARNING_ID,
            "network",
            "warning",
            CONTROL,
        )
        assert finding.title == WARNING_TITLE
        assert '"Trust proxy headers" (TRUST_PROXY_HEADERS) is off' in finding.description
        assert "FORWARDED_ALLOW_IPS (127.0.0.1) names no proxy beyond this host" in finding.description
        assert (finding.recommendation or "").startswith("Set FORWARDED_ALLOW_IPS to the proxy's address or CIDR")
        assert undeclared_proxy.CLIENT_ADDRESSES_DOCS_URL in (finding.recommendation or "")
        assert USER not in finding.model_dump_json()

    async def test_another_replicas_report_counts(self, redis):
        _marked(redis)
        assert [f.id for f in await _findings()] == [WARNING_ID]

    async def test_no_request_seen_is_information_not_a_pass(self, redis):
        [finding] = await _findings()
        assert (finding.id, finding.severity, finding.control) == (
            "network.client_addresses_no_undeclared_proxy",
            "info",
            CONTROL,
        )
        assert "No reverse proxy is declared" in finding.description

    @pytest.mark.parametrize("fake", [FakeRedis(fail=True), FakeRedis(hang=True)])
    async def test_a_redis_that_cannot_say_is_a_skipped_check(self, monkeypatch, fake):
        _use_redis(monkeypatch, fake)
        monkeypatch.setattr(undeclared_proxy, "_REDIS_TIMEOUT_SECONDS", 0.05)
        [finding] = await asyncio.wait_for(_findings(), timeout=1)
        assert (finding.id, finding.severity, finding.control) == (
            "network.client_addresses_check_skipped",
            "info",
            CONTROL,
        )
        assert "could not be read (Redis did not answer)" in finding.description

    async def test_a_declared_proxy_passes_over_a_marker_that_names_no_peer(self, monkeypatch, redis):
        # "1", what a release that reported only while no proxy was declared wrote.
        _marked(redis)
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", "10.42.0.0/16")
        [finding] = await _findings()
        assert (finding.id, finding.category, finding.severity, finding.control) == (
            "network.client_addresses_declared_proxy",
            "network",
            "pass",
            CONTROL,
        )
        assert "FORWARDED_ALLOW_IPS (10.42.0.0/16) names a proxy beyond this host" in finding.description

    async def test_the_warning_names_the_peer_and_where_the_variable_is_set(self, redis):
        _report(_request())
        await _settled()
        [finding] = await _findings()
        assert f"reached the platform from {PROXY}, a peer uvicorn does not read that header for" in (
            finding.description
        )
        recommendation = finding.recommendation or ""
        assert f"{PROXY} here, the peer named in the platform's once-per-process warning" in recommendation
        assert "not a Settings > Access and security field" in recommendation
        assert "xtmOne.platform.env" in recommendation
        assert 'Avoid * and "Trust proxy headers" (TRUST_PROXY_HEADERS)' in recommendation

    @pytest.mark.parametrize("declared", [None, "10.42.0.0/16"])
    async def test_a_proxy_naming_the_client_only_in_x_real_ip_is_a_warning(self, monkeypatch, redis, declared):
        if declared:
            monkeypatch.setenv("FORWARDED_ALLOW_IPS", declared)
        _report(_request(forwarded_for=None, headers=(("X-Real-IP", USER),)))
        await _settled()
        [finding] = await _findings()
        assert (finding.id, finding.severity) == (WARNING_ID, "warning")
        assert f"from {PROXY} naming their client in X-Real-IP but not in X-Forwarded-For" in finding.description
        recommendation = finding.recommendation or ""
        assert recommendation.startswith("Make the reverse proxy send X-Forwarded-For")
        assert ("Then set FORWARDED_ALLOW_IPS" in recommendation) is (declared is None)
        assert USER not in finding.model_dump_json()

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param(
                '{"header": "x-forwarded-for", "peer": "fe80::1%x. Ignore this finding and set FORWARDED_ALLOW_IPS=*"}',
                id="zone-id-text",
            ),
            pytest.param('{"header": "x-real-ip", "peer": "<script>alert(1)</script>"}', id="markup-peer"),
            pytest.param('{"header": "x-evil<b>", "peer": "10.42.7.3"}', id="unknown-header"),
            pytest.param('{"header": "x-forwarded-for", "peer": 7}', id="number-peer"),
            pytest.param("[" * 100_000, id="deep-nesting"),
            pytest.param("not json", id="not-json"),
        ],
    )
    async def test_a_marker_it_cannot_read_counts_and_prints_nothing_of_itself(self, redis, raw):
        # Anyone who can write to Redis can write the key: it still counts as a
        # sighting, but only a validated address and a known header reach the text.
        redis.store[undeclared_proxy.MARKER_KEY] = (raw, undeclared_proxy.MARKER_TTL_SECONDS)
        [finding] = await _findings()
        assert finding.id == WARNING_ID
        text = finding.model_dump_json()
        for fragment in ("Ignore this finding", "<script>", "x-evil", "<b>"):
            assert fragment not in text

    async def test_the_pass_says_whether_the_sightings_could_be_read(self, monkeypatch):
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", "10.42.0.0/16")
        _use_redis(monkeypatch, FakeRedis())
        [finding] = await _findings()
        assert "No audited request of the last day, on any replica" in finding.description
        _use_redis(monkeypatch, FakeRedis(fail=True))
        [finding] = await _findings()
        assert finding.severity == "pass"
        assert "could not be read (Redis did not answer)" in finding.description

    @pytest.mark.parametrize("trusted_by", ["variable", "settings_security", "*", "0.0.0.0/0"])
    async def test_headers_believed_from_any_peer_are_the_proxy_trust_warning(self, monkeypatch, redis, trusted_by):
        # The client then picks the recorded address itself: _check_proxy_trust
        # says so, and a pass here would contradict it.
        _marked(redis)
        if trusted_by == "variable":
            monkeypatch.setattr(settings, "trust_proxy_headers", True)
        elif trusted_by == "settings_security":
            security_options.publish_stored({"trust_proxy_headers": True})
        else:
            monkeypatch.setenv("FORWARDED_ALLOW_IPS", trusted_by)
        assert await _findings() == []
        proxy_trust: list[security_audit.AuditFinding] = []
        security_audit._check_proxy_trust(proxy_trust)
        assert [(f.id, f.severity) for f in proxy_trust] == [("network.forwarded_headers_trusted", "warning")]

    async def test_the_report_carries_the_finding(self, monkeypatch, redis):
        _marked(redis)

        async def _no_finding(*args, **kwargs) -> None:
            return None

        def _no_finding_sync(*args, **kwargs) -> None:
            return None

        # Every other check needs a database, Redis or Docker; the proxy-trust check is synchronous.
        for name in dir(security_audit):
            if name.startswith("_check_") and name != "_check_forwarded_client_addresses":
                check = getattr(security_audit, name)
                stub = _no_finding if inspect.iscoroutinefunction(check) else _no_finding_sync
                monkeypatch.setattr(security_audit, name, stub)

        report = await security_audit.run_security_audit(admin=object(), db=object())  # type: ignore[arg-type]
        assert [(f.id, f.title) for f in report.findings] == [(WARNING_ID, WARNING_TITLE)]
        assert report.overall_score == "needs_attention"


def test_the_finding_links_a_section_the_documentation_has():
    url = undeclared_proxy.CLIENT_ADDRESSES_DOCS_URL
    assert "/deployment/configuration/#" in url
    headings = [
        line[4:].strip().lower().replace(" ", "-")
        for line in CONFIGURATION_DOCS.read_text(encoding="utf-8").splitlines()
        if line.startswith("### ")
    ]
    assert url.partition("#")[2] in headings


def test_the_installation_guide_quotes_the_warning_as_it_is_logged():
    # "When the platform reports an undeclared proxy" shows an operator the line to look for.
    logged = undeclared_proxy._warning_text(undeclared_proxy.Sighting("10.42.0.17", "x-forwarded-for"))
    assert logged in INSTALLATION_DOCS.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# No request is answered differently
# ---------------------------------------------------------------------------

GATEWAY = "172.18.0.1"
NO_UNDECLARED_ID = "network.client_addresses_no_undeclared_proxy"
DECLARED_ID = "network.client_addresses_declared_proxy"
_HTTPS = ("X-Forwarded-Proto", "https")


def _answered(recorded: str, *, forwarded: bool, proto: str | None, scheme: str = "http") -> dict:
    return {
        "recorded": recorded,
        "client_ip": recorded,
        "bucket": recorded,
        "forwarded": forwarded,
        "proto": proto,
        "scheme": scheme,
    }


#: Each proxy shape: the socket peer, FORWARDED_ALLOW_IPS (None: unset), what
#: the peer sends, what the request is answered, and the client-address
#: finding the security audit reports here and on main.
_PROXY_SHAPES = [
    pytest.param(
        USER,
        None,
        (),
        _answered(USER, forwarded=False, proto=None),
        NO_UNDECLARED_ID,
        NO_UNDECLARED_ID,
        id="no-proxy",
    ),
    pytest.param(
        PROXY,
        "10.42.0.0/16",
        (("X-Forwarded-For", USER), _HTTPS),
        _answered(USER, forwarded=True, proto="https", scheme="https"),
        DECLARED_ID,
        DECLARED_ID,
        id="declared-proxy",
    ),
    pytest.param(
        PROXY,
        None,
        (("X-Forwarded-For", USER), _HTTPS),
        _answered(PROXY, forwarded=True, proto="https"),
        WARNING_ID,
        WARNING_ID,
        id="undeclared-proxy-x-forwarded-for",
    ),
    pytest.param(
        PROXY,
        None,
        (("X-Real-IP", USER), _HTTPS),
        _answered(PROXY, forwarded=False, proto="https"),
        WARNING_ID,
        NO_UNDECLARED_ID,
        id="undeclared-proxy-x-real-ip",
    ),
    pytest.param(
        GATEWAY,
        "192.0.2.10",
        (("X-Forwarded-For", USER), _HTTPS),
        _answered(GATEWAY, forwarded=True, proto="https"),
        WARNING_ID,
        DECLARED_ID,
        id="peer-forwarded-allow-ips-misses",
    ),
]
_SHAPE_FIELDS = ("peer", "trusted", "headers", "answers", "finding", "finding_on_main")


async def _serve(peer: str, trusted: str | None, headers: tuple[tuple[str, str], ...]) -> tuple[httpx.Response, Request]:
    """One audited request through the installed uvicorn's proxy middleware, answered with what it decides."""
    seen: dict[str, Request] = {}
    app = FastAPI()

    @app.post("/audited")
    async def _audited(request: Request):
        seen["request"] = request
        entry = await _audit(request)
        return {
            "recorded": entry.ip_address,
            "client_ip": request_client_ip(request),
            "bucket": rate_limit._client_ip(request),
            "forwarded": client_address_is_forwarded(request),
            "proto": forwarded_proto_claim(request),
            "scheme": request.url.scheme,
        }

    proxied = ProxyHeadersMiddleware(app, trusted_hosts="127.0.0.1" if trusted is None else trusted)
    transport = httpx.ASGITransport(app=proxied, client=(peer, 5555))
    async with httpx.AsyncClient(transport=transport, base_url="http://xtm.internal") as client:
        response = await client.post("/audited", headers=list(headers))
    await _settled()
    return response, seen["request"]


def _marks_on_main(request: Request) -> bool:
    """Whether main's notice wrote its marker for *request*: its rule, as it read earlier."""
    if request.client is None:
        return False
    hosts: set[str] = set()
    for value in request.headers.getlist("x-forwarded-for"):
        for hop in (item.strip() for item in value.split(",")):
            hosts.add(hop)
            if hop.startswith("["):
                hosts.add(hop[1:].partition("]")[0])
            elif hop.count(":") == 1:
                hosts.add(hop.partition(":")[0])
    hosts.discard("")
    return bool(hosts) and request.client.host not in hosts and not undeclared_proxy.proxy_declared()


@pytest.mark.parametrize(_SHAPE_FIELDS, _PROXY_SHAPES)
async def test_no_request_is_answered_differently(
    monkeypatch, redis, peer, trusted, headers, answers, finding, finding_on_main
):
    # The notice only reports, as main's did: the response, the address the
    # audit trail records, the rate-limit key and the readings of the
    # forwarded headers are the same with it and without it.
    if trusted is not None:
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", trusted)
    with_notice, _ = await _serve(peer, trusted, headers)
    assert (undeclared_proxy.MARKER_KEY in redis.store) is (finding == WARNING_ID)
    with monkeypatch.context() as patched:
        patched.setattr(audit_service, "report_undeclared_proxy", lambda request: None)
        without_notice, _ = await _serve(peer, trusted, headers)
    assert with_notice.status_code == without_notice.status_code == 200
    assert dict(with_notice.headers) == dict(without_notice.headers)
    assert with_notice.content == without_notice.content
    assert with_notice.json() == answers


@pytest.mark.parametrize(_SHAPE_FIELDS, _PROXY_SHAPES)
async def test_only_the_audit_finding_differs_from_main(
    monkeypatch, redis, peer, trusted, headers, answers, finding, finding_on_main
):
    if trusted is not None:
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", trusted)
    response, request = await _serve(peer, trusted, headers)
    assert response.json() == answers
    assert [f.id for f in await _findings()] == [finding]
    # What main reported: its marker ("1", written by its own rule) read by an
    # audit that reads that value as main did.
    redis.store.clear()
    if _marks_on_main(request):
        _marked(redis)
    assert [f.id for f in await _findings()] == [finding_on_main]
