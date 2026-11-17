"""``TRUST_PROXY_HEADERS`` is reported while it is on; ``FORWARDED_ALLOW_IPS`` is recommended.

The flag is a supported option, never described as deprecated, and the
report changes no behaviour, so this file pins three things:

* the signals - one startup warning while the flag is on, never otherwise,
  whose advice matches what ``FORWARDED_ALLOW_IPS`` trusts, never has the
  flag turned off before the access log confirms the trust list, and names
  where it is turned off (Settings > Access and security, or the variable that locks it);
  and the security audit's proxy trust warning, which describes the flag's
  leftmost-hop reading only while it is on;
* the ``FORWARDED_ALLOW_IPS`` entries uvicorn silently ignores, read exactly
  as uvicorn reads them;
* the client address every configuration resolves to (neither setting, either
  one, both), through uvicorn's real proxy middleware, as it was before.
"""

from __future__ import annotations

import asyncio
import inspect
import logging

import pytest
from starlette.requests import Request
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware, _TrustedHosts
from uvicorn.protocols.utils import get_remote_addr

from app.api.v1 import security_audit, webhooks
from app.core import proxy_headers, rate_limit
from app.core.config import settings
from app.core.utils import request_client_ip
from app.services.audit import _get_client_ip

LOGGER = "app.core.proxy_headers"
PROXY = "10.0.0.5"
# Behind a proxy that appends: the client wrote the first value, the proxy the last.
CHAIN = "6.6.6.6, 198.51.100.7"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(proxy_headers, "_reported", False)
    monkeypatch.setattr(settings, "trust_proxy_headers", False)
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING]


class TestTheStartupWarning:
    def test_it_fires_once_while_the_flag_is_on(self, monkeypatch, caplog):
        monkeypatch.setattr(settings, "trust_proxy_headers", True)
        caplog.set_level(logging.WARNING, logger=LOGGER)

        proxy_headers.log_proxy_header_configuration()
        proxy_headers.log_proxy_header_configuration()

        messages = _warnings(caplog)
        assert len(messages) == 1
        assert messages[0].startswith(proxy_headers.TRUST_PROXY_HEADERS_RISK)
        assert "FORWARDED_ALLOW_IPS=<your proxy addresses>" in messages[0]
        assert "is set here" not in messages[0]

    def test_it_never_calls_the_flag_deprecated(self, monkeypatch, caplog):
        # No release deprecated it: a supported option is reported, never
        # announced as going away.
        monkeypatch.setattr(settings, "trust_proxy_headers", True)
        caplog.set_level(logging.WARNING, logger=LOGGER)

        proxy_headers.log_proxy_header_configuration()

        (message,) = _warnings(caplog)
        for phrase in ("deprecat", "removed", "removal", "future major", "until then"):
            assert phrase not in message.lower()

    @pytest.mark.parametrize("value", ["127.0.0.1", "", " , ", "127.0.0.1,"])
    def test_uvicorns_default_is_not_a_replacement_already_in_place(self, monkeypatch, caplog, value):
        # docker-compose always sets the variable, to uvicorn's own default.
        monkeypatch.setattr(settings, "trust_proxy_headers", True)
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", value)
        caplog.set_level(logging.WARNING, logger=LOGGER)

        proxy_headers.log_proxy_header_configuration()

        (message,) = _warnings(caplog)
        assert "is set here" not in message
        assert "FORWARDED_ALLOW_IPS=<your proxy addresses>" in message

    def test_a_trust_list_in_place_is_checked_before_the_flag_goes(self, monkeypatch, caplog):
        monkeypatch.setattr(settings, "trust_proxy_headers", True)
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", "10.42.0.0/16")
        caplog.set_level(logging.WARNING, logger=LOGGER)

        proxy_headers.log_proxy_header_configuration()

        (message,) = _warnings(caplog)
        assert "FORWARDED_ALLOW_IPS is set here, and the flag takes precedence over it" in message
        assert "check that the access log's client_ip shows real client addresses" in message

    def test_a_wildcard_is_no_replacement(self, monkeypatch, caplog):
        # "*" trusts every peer and reads the leftmost hop, exactly what the flag does.
        monkeypatch.setattr(settings, "trust_proxy_headers", True)
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
        caplog.set_level(logging.WARNING, logger=LOGGER)

        proxy_headers.log_proxy_header_configuration()

        (message,) = _warnings(caplog)
        assert "FORWARDED_ALLOW_IPS=* is set here but is no safer" in message
        assert "FORWARDED_ALLOW_IPS=<your proxy addresses>" in message

    @pytest.mark.parametrize(
        ("value", "ignored"),
        [("nginx", "'nginx'"), ("10.42.0.0/16, nginx", "'nginx'"), ("10.0.0.1, *", "'*'")],
    )
    def test_ignored_entries_are_corrected_before_the_flag_goes(self, monkeypatch, caplog, value, ignored):
        # An ignored entry trusts nobody: without the flag every request would
        # look like it came from the proxy, over plain HTTP.
        monkeypatch.setattr(settings, "trust_proxy_headers", True)
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", value)
        caplog.set_level(logging.WARNING, logger=LOGGER)

        proxy_headers.log_proxy_header_configuration()

        flag_warning, ignored_entries = _warnings(caplog)
        assert "no peer ever matches the entries named in the next warning" in flag_warning
        assert ignored in ignored_entries

    @pytest.mark.parametrize(
        "value", [None, "127.0.0.1", "", "10.42.0.0/16", "*", " *", "nginx", "10.42.0.0/16, nginx"]
    )
    def test_the_flag_is_only_ever_removed_after_the_access_log_check(self, monkeypatch, caplog, value):
        monkeypatch.setattr(settings, "trust_proxy_headers", True)
        if value is not None:
            monkeypatch.setenv("FORWARDED_ALLOW_IPS", value)
        caplog.set_level(logging.WARNING, logger=LOGGER)

        proxy_headers.log_proxy_header_configuration()

        flag_warning = _warnings(caplog)[0]
        assert "only step" not in flag_warning
        assert flag_warning.index("client_ip shows real client addresses") < flag_warning.index(
            "TRUST_PROXY_HEADERS=false"
        )

    def test_the_flag_is_turned_off_where_it_is_set(self, monkeypatch, caplog):
        # Not locked by its variable: the Settings > Access and security field, the
        # variable as the alternative that wins.
        monkeypatch.setattr(settings, "trust_proxy_headers", True)
        caplog.set_level(logging.WARNING, logger=LOGGER)

        proxy_headers.log_proxy_header_configuration()

        (message,) = _warnings(caplog)
        assert 'turn off "Trust proxy headers" in Settings > Access and security > Security options' in message
        assert "or set TRUST_PROXY_HEADERS=false and restart" in message

    def test_a_flag_its_variable_sets_is_turned_off_there(self, monkeypatch, caplog):
        monkeypatch.setattr(settings, "trust_proxy_headers", True)
        monkeypatch.setattr(settings, "_environment_fields", frozenset({"trust_proxy_headers"}))
        caplog.set_level(logging.WARNING, logger=LOGGER)

        proxy_headers.log_proxy_header_configuration()

        (message,) = _warnings(caplog)
        assert "set TRUST_PROXY_HEADERS=false and restart (the variable is set" in message
        assert 'turn off "Trust proxy headers"' not in message

    def test_it_runs_before_the_startup_work_the_ipv6_report_after_the_stored_options(self):
        # Before the provider seed's load_dotenv can add a FORWARDED_ALLOW_IPS
        # uvicorn never read; the IPv6 key once a stored prefix is known.
        from app import main

        lifespan = inspect.getsource(main.lifespan)
        startup = inspect.getsource(main._do_all_startup_work)
        assert "log_proxy_header_configuration()" in lifespan
        assert "log_proxy_header_configuration()" not in startup
        assert "log_rate_limit_configuration()" not in lifespan
        assert startup.index("await refresh_security_options_cache()") < startup.index(
            "log_rate_limit_configuration()"
        )

    @pytest.mark.parametrize("value", [None, "127.0.0.1", "10.42.0.0/16, fd00::/8", "*"])
    def test_it_never_fires_while_the_flag_is_off(self, monkeypatch, caplog, value):
        if value is not None:
            monkeypatch.setenv("FORWARDED_ALLOW_IPS", value)
        caplog.set_level(logging.DEBUG, logger=LOGGER)

        proxy_headers.log_proxy_header_configuration()

        assert _warnings(caplog) == []

    def test_an_unusable_trust_entry_is_named(self, monkeypatch, caplog):
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", "10.42.0.0/16, nginx, 10.42.0.1/16")
        caplog.set_level(logging.WARNING, logger=LOGGER)

        proxy_headers.log_proxy_header_configuration()

        (message,) = _warnings(caplog)
        assert "'nginx', '10.42.0.1/16'" in message
        assert "deprecated" not in message


class TestTheAuditFinding:
    """One finding for the flag: the proxy trust warning (``network.forwarded_headers_trusted``) says what it reads."""

    @pytest.fixture(autouse=True)
    def _https(self, monkeypatch):
        monkeypatch.setattr(settings, "base_url", "https://xtm.example.com")
        monkeypatch.setattr(settings, "debug", False)

    def test_the_warning_describes_the_flag_while_it_is_on(self, monkeypatch):
        monkeypatch.setattr(settings, "trust_proxy_headers", True)
        findings: list[security_audit.AuditFinding] = []

        security_audit._check_proxy_trust(findings)

        (finding,) = findings
        assert (finding.id, finding.category, finding.severity, finding.control) == (
            "network.forwarded_headers_trusted",
            "network",
            "warning",
            "network.trust_proxy_headers",
        )
        assert "TRUST_PROXY_HEADERS is also unsafe behind a proxy that appends" in finding.description
        assert "deprecat" not in finding.description.lower()
        assert "wins over FORWARDED_ALLOW_IPS" in finding.description
        assert '"Canonical client addresses" (CLIENT_IP_STRICT) is on' in finding.description
        recommendation = finding.recommendation or ""
        # The trust list is checked in the access log before the flag goes.
        assert recommendation.index("client_ip shows real client addresses") < recommendation.index(
            '"Trust proxy headers"'
        )

    @pytest.mark.parametrize("value", [None, "10.42.0.0/16", "*"])
    def test_it_never_says_so_while_the_flag_is_off(self, monkeypatch, value):
        if value is not None:
            monkeypatch.setenv("FORWARDED_ALLOW_IPS", value)
        findings: list[security_audit.AuditFinding] = []

        security_audit._check_proxy_trust(findings)

        assert [f for f in findings if "TRUST_PROXY_HEADERS is also" in f.description] == []
        assert "client_ip shows real client addresses" not in " ".join(f.recommendation or "" for f in findings)

    def test_the_audit_builds_no_second_finding_for_the_flag(self, monkeypatch):
        monkeypatch.setattr(settings, "trust_proxy_headers", True)
        assert not hasattr(security_audit, "_check_reverse_proxy")
        source = inspect.getsource(security_audit)
        assert "trust_proxy_headers_deprecated" not in source


@pytest.mark.parametrize(
    ("value", "unusable"),
    [
        ("*", []),
        ("127.0.0.1", []),
        ("", []),
        ("10.42.0.0/16, fd00::/8, ::1, 2001:db8::7", []),
        ("10.0.0.1,", []),
        ("nginx", ["nginx"]),
        ("[::1]", ["[::1]"]),
        ("10.42.0.1/16", ["10.42.0.1/16"]),
        ("10.42.0.0/33", ["10.42.0.0/33"]),
        ("10.0.0.1, *", ["*"]),
        # uvicorn reads "*" as every peer only when it is the exact value.
        (" *", ["*"]),
    ],
)
def test_unusable_entries_are_exactly_those_uvicorn_keeps_as_literals(value, unusable):
    assert proxy_headers.unusable_forwarded_allow_ips(value) == unusable
    if value != "*":
        literals = _TrustedHosts(value).trusted_literals - {""}
        assert literals == set(unusable)


class _UnixSocketTransport:
    """What asyncio hands uvicorn for a Unix-socket connection: a peer name that is a path."""

    def get_extra_info(self, name):
        return self if name == "socket" else None

    def getpeername(self):
        return ""


@pytest.mark.parametrize("peer", [(PROXY, 1234), None])
def test_a_literal_matches_no_peer_a_unix_socket_included(peer):
    # A Unix-socket connection has no client address (the None case), so a
    # socket path in the list trusts no peer either, as the warning says.
    assert get_remote_addr(_UnixSocketTransport()) is None
    seen = {}

    async def app(scope, receive, send):
        seen["scope"] = scope

    scope = {
        "type": "http",
        "scheme": "http",
        "client": peer,
        "headers": [(b"x-forwarded-for", CHAIN.encode()), (b"x-forwarded-proto", b"https")],
    }
    literals = "/run/xtm-one.sock, unix:/run/xtm-one.sock, nginx"
    asyncio.run(ProxyHeadersMiddleware(app, trusted_hosts=literals)(scope, None, None))

    assert seen["scope"]["client"] == peer
    assert seen["scope"]["scheme"] == "http"


def _as_the_app_sees_it(forwarded_allow_ips: str | None) -> Request:
    """A request from the proxy, after uvicorn's middleware ran with that trust list."""
    seen = {}

    async def app(scope, receive, send):
        seen["scope"] = scope

    scope = {
        "type": "http",
        "scheme": "http",
        "client": (PROXY, 1234),
        "headers": [(b"x-forwarded-for", CHAIN.encode()), (b"x-forwarded-proto", b"https")],
    }
    trusted = "127.0.0.1" if forwarded_allow_ips is None else forwarded_allow_ips
    asyncio.run(ProxyHeadersMiddleware(app, trusted_hosts=trusted)(scope, None, None))
    return Request(seen["scope"])


@pytest.mark.parametrize(
    ("trust", "forwarded_allow_ips", "expected"),
    [
        # Neither: uvicorn trusts only loopback, the proxy's address is the client.
        (False, None, PROXY),
        # FORWARDED_ALLOW_IPS naming the proxy: the chain is read from its end.
        (False, PROXY, "198.51.100.7"),
        (False, "10.0.0.0/8", "198.51.100.7"),
        # "*": every peer, and the caller-controlled start of the chain.
        (False, "*", "6.6.6.6"),
        # The flag: the leftmost hop, whatever uvicorn trusts.
        (True, None, "6.6.6.6"),
        (True, PROXY, "6.6.6.6"),
        (True, "*", "6.6.6.6"),
    ],
)
def test_every_configuration_resolves_the_client_as_before(monkeypatch, trust, forwarded_allow_ips, expected):
    monkeypatch.setattr(settings, "trust_proxy_headers", trust)
    request = _as_the_app_sees_it(forwarded_allow_ips)

    assert request_client_ip(request) == expected
    assert _get_client_ip(request) == expected
    assert rate_limit._client_ip(request) == expected
    assert webhooks._webhook_request_source(request)[0] == expected


def test_an_invalid_hop_under_the_flag_falls_back_to_the_corrected_peer(monkeypatch):
    monkeypatch.setattr(settings, "trust_proxy_headers", True)
    seen = {}

    async def app(scope, receive, send):
        seen["scope"] = scope

    scope = {
        "type": "http",
        "client": (PROXY, 1234),
        "headers": [(b"x-forwarded-for", b"not-an-address, 198.51.100.7")],
    }
    asyncio.run(ProxyHeadersMiddleware(app, trusted_hosts=PROXY)(scope, None, None))

    assert request_client_ip(Request(seen["scope"])) == "198.51.100.7"
