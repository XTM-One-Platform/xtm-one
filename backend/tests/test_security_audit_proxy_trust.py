"""The security audit names an HTTPS deployment that trusts no proxy.

Behind a TLS-terminating proxy the platform can confirm a request encrypted
only from a proxy it trusts, so without one every proxied A2A request is
refused (``403 https required``) - the Helm charts' default. The admin hears it
from the audit before a partner does.
"""

import inspect
import ipaddress
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api.v1 import security_audit
from app.core.config import settings
from app.core.undeclared_proxy import parse_forwarded_allow_ips


@pytest.fixture(autouse=True)
def _https_deployment_that_trusts_no_proxy(monkeypatch):
    monkeypatch.setattr(settings, "base_url", "https://xtm.example.com")
    monkeypatch.setattr(settings, "trust_proxy_headers", False)
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)


def _findings():
    findings = []
    security_audit._check_proxy_trust(findings)
    return findings


def test_uvicorn_s_default_trust_list_is_reported():
    [finding] = _findings()
    assert finding.severity == "warning"
    assert (finding.id, finding.category) == ("network.proxy_trust_missing", "network")
    assert finding.control == "network.trust_proxy_headers"
    assert "FORWARDED_ALLOW_IPS (127.0.0.1)" in finding.description
    assert "403 https required" in finding.description
    assert finding.recommendation.startswith("Set FORWARDED_ALLOW_IPS to the proxy's address")


@pytest.mark.parametrize("value", ["127.0.0.1", "::1, 127.0.0.1", "", " , "])
def test_a_loopback_only_or_empty_trust_list_is_reported(monkeypatch, value):
    # docker-compose.yml always passes the variable, set to uvicorn's own
    # default, and an empty value trusts no peer at all.
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", value)
    assert len(_findings()) == 1


@pytest.mark.parametrize(
    ("value", "ignored"),
    [
        ("*,127.0.0.1", "*"),
        ("nginx", "nginx"),
        ("10.42.0.1/16", "10.42.0.1/16"),
        ("localhost, ::1", "localhost"),
        ("127.0.0.0/8", None),
    ],
)
def test_entries_uvicorn_cannot_match_trust_no_proxy(monkeypatch, value, ignored):
    # uvicorn keeps these as literals no TCP peer equals: * only as the whole
    # value, hostnames unresolved, a CIDR only with its host bits at zero.
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", value)
    [finding] = _findings()
    if ignored:
        assert f"uvicorn matches no peer against {ignored}:" in finding.description
    else:
        assert "matches no peer" not in finding.description


@pytest.mark.parametrize(
    "value",
    ["10.42.0.0/16", "127.0.0.1,172.18.0.1", "fd00::/8", "10.42.0.0/16, nginx"],
)
def test_a_trusted_proxy_is_reported_as_a_pass(monkeypatch, value):
    # No silent pass: the proxy trust that is declared says so.
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", value)
    [finding] = _findings()
    assert (finding.id, finding.severity) == ("network.proxy_trust_declared", "pass")
    assert f"FORWARDED_ALLOW_IPS ({value})" in finding.description


@pytest.mark.parametrize("value", ["*", "0.0.0.0/0", "10.0.0.0/8, ::/0"])
def test_trusting_every_peer_is_a_warning_not_a_declared_proxy(monkeypatch, value):
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", value)
    [finding] = _findings()
    assert (finding.id, finding.severity) == ("network.forwarded_headers_trusted", "warning")
    assert f"FORWARDED_ALLOW_IPS ({value}) trusts every peer" in finding.description
    assert finding.control == "network.trust_proxy_headers"


def test_the_parser_mirrors_uvicorn():
    # One reading of the trust list, shared with the startup report
    # (``core/proxy_headers.py``, whose ignored entries are pinned there).
    from uvicorn.middleware.proxy_headers import _TrustedHosts

    from app.core.proxy_headers import trusted_proxy_entries

    for value in ["*", "*,127.0.0.1", "nginx", "10.42.0.1/16", "10.42.0.0/16", "127.0.0.0/8", "::1", "", "fd00::/8"]:
        uvicorn_trusts_remote = "10.42.3.7" in _TrustedHosts(value) or "fd00::7" in _TrustedHosts(value)
        remote, _ignored = trusted_proxy_entries(value)
        assert remote is uvicorn_trusts_remote, value


def _trusted_proxy_entries_before_4532(value):
    """``_trusted_proxy_entries`` as it read the variable before it delegated to ``core/undeclared_proxy``."""
    if value == "*":
        return True, []
    remote, ignored = False, []
    for entry in (item.strip() for item in value.split(",")):
        if not entry:
            continue
        try:
            trusted = ipaddress.ip_network(entry) if "/" in entry else ipaddress.ip_address(entry)
        except ValueError:
            ignored.append(entry)
            continue
        remote = remote or not trusted.is_loopback
    return remote, ignored


#: FORWARDED_ALLOW_IPS values the parity tests read both ways.
_TRUST_LIST_VALUES = [
    "",
    "*",
    " * ",
    "*\n",
    "127.0.0.1",
    "127.0.0.1,10.0.0.1",
    "  127.0.0.1 ,  10.0.0.1  ",
    "10.0.0.1,,10.0.0.2",
    "a,,b",
    "127.0.0.1,",
    ",",
    "10.0.0.0/8",
    "10.0.0.1/8",
    "10.0.0.0/33",
    "10.0.0.0/255.0.0.0",
    "0.0.0.0/0",
    "::/0",
    "127.0.0.0/8",
    "::1",
    "[::1]",
    "[::1]:80",
    "10.0.0.1:80",
    "fd00::/8",
    "FD00::1",
    "::ffff:10.0.0.1",
    "::ffff:127.0.0.1",
    "fe80::1%eth0",
    "fe80::%eth0/64",
    "proxy",
    "localhost, ::1",
    "/run/uvicorn.sock",
    "unix:/run/uvicorn.sock",
    "*,10.0.0.1",
    "10.0.0.1,*",
    "010.0.0.1",
    "10.0.0.1\t10.0.0.2",
    "\t127.0.0.1\n",
    "\u00a010.0.0.1\u00a0",
    "10.0.0.1,10.0.0.1,proxy,proxy",
    "nginx, 10.42.0.0/16, ingress-nginx",
]


@pytest.mark.parametrize("value", _TRUST_LIST_VALUES)
def test_the_trust_list_reads_as_before(value):
    # The configuration finding reads the parser, and says what it always said
    # for any value: whether a proxy beyond loopback is trusted, and the
    # entries, in order, that uvicorn matches no peer against.
    parsed = parse_forwarded_allow_ips(value)
    assert (parsed.trusts_remote, list(parsed.literals)) == _trusted_proxy_entries_before_4532(value)


def _trusts_every_peer_before_4532(value):
    """``_trusts_every_peer`` as it read the variable before ``core/undeclared_proxy`` did."""
    if value == "*":
        return True
    for entry in (item.strip() for item in value.split(",")):
        if "/" not in entry:
            continue
        try:
            if ipaddress.ip_network(entry).prefixlen == 0:
                return True
        except ValueError:
            continue
    return False


@pytest.mark.parametrize("value", _TRUST_LIST_VALUES)
def test_trusting_every_peer_reads_as_before(value):
    assert security_audit._trusts_every_peer(value) is _trusts_every_peer_before_4532(value)
    assert parse_forwarded_allow_ips(value).trusts_every_peer is _trusts_every_peer_before_4532(value)


@pytest.mark.parametrize("base_url", ["https://xtm.example.com", "http://localhost:4000"])
@pytest.mark.parametrize("debug", [False, True])
def test_the_trust_proxy_headers_flag_is_reported_whatever_the_deployment(monkeypatch, base_url, debug):
    # It confirms the scheme by itself, so the missing-proxy warning has nothing
    # to say; but any peer then picks the address the rate limits record.
    monkeypatch.setattr(settings, "trust_proxy_headers", True)
    monkeypatch.setattr(settings, "base_url", base_url)
    monkeypatch.setattr(settings, "debug", debug)
    [finding] = _findings()
    assert (finding.id, finding.category, finding.severity) == (
        "network.forwarded_headers_trusted",
        "network",
        "warning",
    )
    assert finding.description.startswith('"Trust proxy headers" (TRUST_PROXY_HEADERS) is on: ')
    assert "rate limits and the audit trail record" in finding.description
    assert finding.recommendation.startswith("Set FORWARDED_ALLOW_IPS to the reverse proxy's address")


@pytest.mark.parametrize("base_url", ["http://localhost:4000", "", "https://[::1"])
def test_a_deployment_not_reached_over_https_is_not(monkeypatch, base_url):
    monkeypatch.setattr(settings, "base_url", base_url)
    assert _findings() == []


def test_a_dev_build_is_not(monkeypatch):
    monkeypatch.setattr(settings, "debug", True)
    assert _findings() == []


def test_the_audit_runs_the_check():
    assert "_check_proxy_trust(findings)" in inspect.getsource(security_audit.build_security_audit_report)


class _CountsDb:
    """Answers the active runners count, then the registered MCP OAuth clients count."""

    def __init__(self, runners: int = 0, clients: int = 0):
        self._counts = [runners, clients]
        self.queries = 0

    async def execute(self, *_args, **_kwargs):
        count = self._counts[self.queries]
        self.queries += 1
        return SimpleNamespace(scalar=lambda: count)


async def _transport_findings(db=None):
    findings = []
    await security_audit._check_insecure_transport(db or _CountsDb(), findings)
    return findings


async def test_the_strict_transport_off_behind_https_is_an_info_recommending_it():
    # Opt-in: behind an HTTPS endpoint off is context - never a
    # warning a deployment that changed nothing would suddenly see - and
    # nothing is counted.
    db = _CountsDb(runners=3, clients=2)
    [finding] = await _transport_findings(db)
    assert (finding.id, finding.category, finding.severity) == (
        "network.credential_transport_strict_off",
        "network",
        "info",
    )
    assert db.queries == 0
    assert finding.control == "network.credential_transport_strict"
    assert "FORWARDED_ALLOW_IPS" in finding.recommendation
    assert 'turn on "Require HTTPS for runner enrollment and MCP tokens"' in finding.recommendation
    assert "_check_insecure_transport(db, findings)" in inspect.getsource(security_audit.build_security_audit_report)


@pytest.mark.parametrize(("runners", "clients"), [(3, 0), (0, 2), (1, 1)])
async def test_credentials_issued_over_plain_http_are_a_warning(monkeypatch, runners, clients):
    # What the switch protects is in use: the platform is served over plain
    # HTTP and runners or MCP clients hold credentials those routes issued.
    monkeypatch.setattr(settings, "base_url", "http://xtm.example.com")
    [finding] = await _transport_findings(_CountsDb(runners=runners, clients=clients))
    assert (finding.id, finding.severity) == ("network.credential_transport_strict_off", "warning")
    assert finding.title == "Runner and MCP credentials are issued over plain HTTP"
    assert "BASE_URL is not an https URL" in finding.description
    assert ("active runner(s)" in finding.description) is bool(runners)
    assert ("registered MCP OAuth client(s)" in finding.description) is bool(clients)
    assert finding.recommendation.startswith("Serve the platform over HTTPS")
    assert 'turn on "Require HTTPS for runner enrollment and MCP tokens"' in finding.recommendation


async def test_plain_http_with_nothing_issued_or_in_a_dev_build_is_context(monkeypatch):
    monkeypatch.setattr(settings, "base_url", "http://xtm.example.com")
    [finding] = await _transport_findings(_CountsDb())
    assert finding.severity == "info"
    monkeypatch.setattr(settings, "debug", True)
    db = _CountsDb(runners=3)
    [finding] = await _transport_findings(db)
    assert (finding.severity, db.queries) == ("info", 0)


async def test_the_strict_transport_on_is_a_pass_naming_the_field(monkeypatch):
    monkeypatch.setattr(settings, "credential_transport_strict", True)
    [finding] = await _transport_findings()
    assert (finding.id, finding.category, finding.severity) == (
        "network.credential_transport_strict_on",
        "network",
        "pass",
    )
    assert finding.control == "network.credential_transport_strict"
    assert finding.description.startswith(
        '"Require HTTPS for runner enrollment and MCP tokens" (CREDENTIAL_TRANSPORT_STRICT) is on: '
    )


def test_the_missing_proxy_finding_says_what_the_credential_routes_do(monkeypatch):
    [default] = _findings()
    assert (
        "/runners/enroll, /mcp/oauth/register and /mcp/oauth/token log an unconfirmed-transport warning"
        in default.description
    )
    monkeypatch.setattr(settings, "credential_transport_strict", True)
    [strict] = _findings()
    assert strict.id == "network.proxy_trust_missing"
    assert "/mcp/oauth/register answers invalid_client_metadata" in strict.description
    assert "/mcp/oauth/token answers invalid_request because" in strict.description


def test_forwarded_allow_ips_is_read_in_one_place():
    # uvicorn's reading of the variable lives in app.core.undeclared_proxy:
    # a second module reading it from the environment could drift
    # from the one uvicorn applies. Call forwarded_allow_ips() and
    # parse_forwarded_allow_ips() instead.
    app_dir = Path(security_audit.__file__).resolve().parents[2]
    reader = re.compile(r"""(?:environ|getenv)\b[\s\S]{0,80}?["']FORWARDED_ALLOW_IPS["']""")
    readers = sorted(
        path.relative_to(app_dir).as_posix()
        for path in app_dir.rglob("*.py")
        if reader.search(path.read_text(encoding="utf-8"))
    )
    assert readers == ["core/undeclared_proxy.py"]


def test_forwarded_allow_ips_is_parsed_in_one_place():
    # The startup report (core/proxy_headers.py) reads the trust list
    # through the same parser, so it and the security audit cannot drift apart.
    from app.core import proxy_headers

    assert "ipaddress" not in inspect.getsource(proxy_headers)
    for value in _TRUST_LIST_VALUES:
        parsed = parse_forwarded_allow_ips(value)
        assert proxy_headers.trusted_proxy_entries(value) == (parsed.trusts_remote, list(parsed.literals)), value
