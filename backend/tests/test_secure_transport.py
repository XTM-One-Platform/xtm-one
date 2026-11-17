"""The transport guard: what counts as "arrived encrypted", and who may say so."""

import asyncio
import logging
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import deps
from app.api.deps import (
    credential_transport_route,
    refuse_insecure_credential_transport,
    refuses_credential_transport,
    require_secure_transport,
)
from app.api.v1 import a2a_callbacks, a2a_server, mcp_oauth, runners
from app.core import utils
from app.core.config import Settings, settings
from app.core.utils import (
    client_address_is_forwarded,
    forwarded_proto_claim,
    forwarded_scheme,
    is_secure_transport,
)
from app.services import ai_state


def _request(scheme="http", host="10.0.0.5", forwarded=None, forwarded_for=None):
    headers = {}
    if forwarded is not None:
        headers["x-forwarded-proto"] = forwarded
    if forwarded_for is not None:
        headers["x-forwarded-for"] = forwarded_for
    return SimpleNamespace(
        url=SimpleNamespace(scheme=scheme, path="/guarded"),
        client=SimpleNamespace(host=host),
        headers=headers,
        scope={},
    )


@pytest.fixture(autouse=True)
def _untrusting(monkeypatch):
    monkeypatch.setattr(settings, "trust_proxy_headers", False)
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "a2a_allow_insecure_loopback", False)
    monkeypatch.setattr(settings, "test_only_insecure_transport_peers", [])
    monkeypatch.setattr(settings, "credential_transport_strict", False)
    deps._reported_paths.clear()


class TestTheSchemeDecides:
    def test_https_is_secure(self):
        assert is_secure_transport(_request(scheme="https")) is True

    def test_plain_http_is_not(self):
        assert is_secure_transport(_request()) is False

    def test_an_untrusted_forwarded_header_is_not_believed(self, monkeypatch):
        assert is_secure_transport(_request(forwarded="https")) is False


class TestTheForwardedClaim:
    @pytest.fixture(autouse=True)
    def _trusting(self, monkeypatch):
        monkeypatch.setattr(settings, "trust_proxy_headers", True)

    def test_https_claim_is_honoured(self):
        assert is_secure_transport(_request(forwarded="https")) is True

    def test_http_claim_refuses(self):
        assert is_secure_transport(_request(forwarded="http")) is False

    def test_the_first_hop_decides(self):
        assert is_secure_transport(_request(forwarded="https, http")) is True

    @pytest.mark.parametrize("claim", ["  ", ",", ",,", "garbage"])
    def test_an_uninterpretable_claim_refuses(self, claim):
        assert is_secure_transport(_request(scheme="https", forwarded=claim)) is False

    def test_an_empty_header_is_no_claim_at_all(self):
        assert is_secure_transport(_request(scheme="https", forwarded="")) is True
        assert is_secure_transport(_request(scheme="http", forwarded="")) is False


class TestNoAddressStandsInForTheScheme:
    """``ProxyHeadersMiddleware`` rewrites ``scope["client"]`` from
    ``X-Forwarded-For`` without checking it is an address, so with
    ``FORWARDED_ALLOW_IPS="*"`` a caller chooses its own ``client.host``."""

    def test_a_peer_named_testclient_is_not_secure(self):
        assert is_secure_transport(_request(host="testclient")) is False

    def test_loopback_is_not_secure_without_debug(self):
        assert is_secure_transport(_request(host="127.0.0.1")) is False

    def test_loopback_is_exempt_in_debug_builds(self, monkeypatch):
        monkeypatch.setattr(settings, "debug", True)
        assert is_secure_transport(_request(host="127.0.0.1")) is True


class TestAForwardedPeerIsNeverExempt:
    """An address exemption reads the real socket peer only.

    uvicorn rewrites ``scope["client"]`` from ``X-Forwarded-For`` whenever the
    peer is a trusted proxy, so a request carrying the header may name any
    address - loopback included."""

    @pytest.mark.parametrize("flag", ["debug", "a2a_allow_insecure_loopback"])
    def test_a_forwarded_loopback_is_not_exempt(self, monkeypatch, flag):
        monkeypatch.setattr(settings, flag, True)
        req = _request(host="127.0.0.1", forwarded_for="127.0.0.1")
        assert is_secure_transport(req) is False

    @pytest.mark.parametrize("flag", ["debug", "a2a_allow_insecure_loopback"])
    def test_real_loopback_stays_exempt(self, monkeypatch, flag):
        monkeypatch.setattr(settings, flag, True)
        assert is_secure_transport(_request(host="::1")) is True

    def test_the_header_forfeits_the_exemption_whatever_it_names(self, monkeypatch):
        monkeypatch.setattr(settings, "a2a_allow_insecure_loopback", True)
        assert is_secure_transport(_request(host="127.0.0.1", forwarded_for="")) is False

    def test_https_is_unaffected_by_the_header(self):
        req = _request(scheme="https", host="203.0.113.9", forwarded_for="198.51.100.1")
        assert is_secure_transport(req) is True

    @pytest.mark.parametrize(
        ("trusted", "xff"),
        [
            ("*", b"127.0.0.1"),
            ("10.0.0.5", b"127.0.0.1"),  # a pinned proxy passing the header through
            ("10.0.0.0/8", b"127.0.0.1, 10.1.2.3"),  # a trust list covering the client
        ],
    )
    def test_the_uvicorn_rewrite_cannot_claim_loopback(self, monkeypatch, trusted, xff):
        from starlette.requests import Request
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        monkeypatch.setattr(settings, "debug", True)
        seen = {}

        async def app(scope, receive, send):
            seen["scope"] = scope

        scope = {
            "type": "http",
            "scheme": "http",
            "path": "/a2a/partner/agents",
            "headers": [(b"x-forwarded-for", xff)],
            "client": ("10.0.0.5", 1234),
        }
        asyncio.run(ProxyHeadersMiddleware(app, trusted_hosts=trusted)(scope, None, None))
        request = Request(seen["scope"])
        assert request.client.host == "127.0.0.1"  # the rewrite happened
        assert is_secure_transport(request) is False

    def test_the_trust_proxy_headers_flag_cannot_claim_loopback_either(self, monkeypatch):
        # Under trust_proxy_headers the platform's client address IS the first
        # X-Forwarded-For hop (request_client_ip), so a forwarded 127.0.0.1
        # from a remote peer would otherwise read as loopback.
        monkeypatch.setattr(settings, "trust_proxy_headers", True)
        monkeypatch.setattr(settings, "a2a_allow_insecure_loopback", True)
        assert is_secure_transport(_request(host="203.0.113.9", forwarded_for="127.0.0.1")) is False
        assert is_secure_transport(_request(host="127.0.0.1")) is True


class TestOneRuleForTheClientAddress:
    """The exemption decides "forwarded" and reads the address the way the rest
    of the platform does - the rate limiter, the audit trail and the webhook
    diagnostics - never with a reading of its own."""

    @pytest.fixture(autouse=True)
    def _loopback_allowed(self, monkeypatch):
        monkeypatch.setattr(settings, "a2a_allow_insecure_loopback", True)

    def test_the_address_is_the_platforms_client_address(self, monkeypatch):
        monkeypatch.setattr(utils, "request_client_ip", lambda _request: "127.0.0.1")
        assert is_secure_transport(_request(host="10.0.0.5")) is True
        monkeypatch.setattr(utils, "request_client_ip", lambda _request: "10.0.0.5")
        assert is_secure_transport(_request(host="127.0.0.1")) is False

    def test_forwarded_is_the_platforms_decision(self, monkeypatch):
        monkeypatch.setattr(utils, "client_address_is_forwarded", lambda _request: True)
        assert is_secure_transport(_request(host="127.0.0.1")) is False

    @pytest.mark.parametrize("value", ["", "127.0.0.1", "198.51.100.1, 10.0.0.5"])
    def test_any_x_forwarded_for_marks_the_address_forwarded(self, value):
        assert client_address_is_forwarded(_request(forwarded_for=value)) is True

    def test_no_header_means_the_socket_peer(self):
        assert client_address_is_forwarded(_request()) is False

    def test_a_starlette_request_is_read_from_its_asgi_headers(self):
        from starlette.requests import Request

        scope = {"type": "http", "headers": [(b"x-forwarded-for", b"127.0.0.1")], "client": ("10.0.0.5", 1)}
        assert client_address_is_forwarded(Request(scope)) is True


class TestTheTestOnlyPeers:
    @pytest.fixture(autouse=True)
    def _bridge(self, monkeypatch):
        # As the end-to-end stack runs: the peers only ever come with the
        # loopback exemption they extend.
        monkeypatch.setattr(settings, "a2a_allow_insecure_loopback", True)
        monkeypatch.setattr(settings, "test_only_insecure_transport_peers", ["172.16.0.0/12"])

    def test_a_listed_peer_is_exempt(self):
        assert is_secure_transport(_request(host="172.18.0.1"), allow_test_peers=True) is True

    def test_an_unlisted_peer_is_not(self):
        assert is_secure_transport(_request(host="10.0.0.5"), allow_test_peers=True) is False

    def test_a_listed_peer_forfeits_it_with_a_forwarding_header(self):
        req = _request(host="172.18.0.1", forwarded_for="203.0.113.9")
        assert is_secure_transport(req, allow_test_peers=True) is False

    def test_a_peer_that_is_not_an_address_is_not_exempt(self):
        assert is_secure_transport(_request(host="testclient"), allow_test_peers=True) is False

    def test_the_peers_count_only_alongside_the_loopback_switch(self, monkeypatch):
        # The settings refuse the peers without the switch; the predicate does
        # not rely on that alone.
        monkeypatch.setattr(settings, "a2a_allow_insecure_loopback", False)
        assert is_secure_transport(_request(host="172.18.0.1"), allow_test_peers=True) is False
        monkeypatch.setattr(settings, "debug", True)
        assert is_secure_transport(_request(host="172.18.0.1"), allow_test_peers=True) is False

    def test_the_peers_count_only_where_a_caller_opts_in(self):
        assert is_secure_transport(_request(host="172.18.0.1")) is False

    def test_the_a2a_refusal_admits_a_listed_peer(self):
        assert asyncio.run(require_secure_transport(_request(host="172.18.0.1"))) is None

    def test_the_strict_credential_routes_admit_a_listed_peer_too(self, monkeypatch):
        # Under CREDENTIAL_TRANSPORT_STRICT enroll and token refuse with the
        # A2A exemptions: the end-to-end suite fetches A2A tokens from the
        # bridge network.
        monkeypatch.setattr(settings, "credential_transport_strict", True)
        assert refuses_credential_transport(_request(host="172.18.0.1")) is False
        assert refuses_credential_transport(_request(host="10.0.0.5")) is True

    def test_off_a_listed_peer_is_still_reported_as_before(self, caplog):
        # The default reports exactly what main reported: the test-only peers
        # never silence a report.
        with caplog.at_level(logging.WARNING):
            assert refuses_credential_transport(_request(host="172.18.0.1")) is False
        assert "served over unconfirmed transport" in caplog.text


class TestTheTestOnlyPeersSetting:
    @pytest.fixture(autouse=True)
    def _no_inherited_environment(self, monkeypatch):
        for name in ("TEST_ONLY_INSECURE_TRANSPORT_PEERS", "A2A_ALLOW_INSECURE_LOOPBACK"):
            monkeypatch.delenv(name, raising=False)

    def test_entries_are_normalised_to_networks(self):
        parsed = Settings._parse_test_only_insecure_transport_peers(
            " 172.16.0.0/12, 192.168.1.7 ,, 172.16.0.0/12"
        )
        assert parsed == ["172.16.0.0/12", "192.168.1.7/32"]

    def test_unset_is_empty(self):
        assert Settings._parse_test_only_insecure_transport_peers("") == []

    def test_a_malformed_entry_stops_startup(self):
        with pytest.raises(ValueError, match="TEST_ONLY_INSECURE_TRANSPORT_PEERS"):
            Settings._parse_test_only_insecure_transport_peers("172.16.0.0/12,bridge")

    @pytest.mark.parametrize("entry", ["0.0.0.0/0", "::/0", "8.8.8.0/24", "100.64.0.0/10", "2001:4860::/32"])
    def test_a_network_that_is_not_private_stops_startup(self, entry):
        with pytest.raises(ValueError, match="not a private network"):
            Settings._parse_test_only_insecure_transport_peers(f"172.16.0.0/12,{entry}")

    def test_the_end_to_end_job_s_value_is_accepted(self):
        # .github/workflows/e2e.yml, plus an IPv6 bridge network.
        parsed = Settings._parse_test_only_insecure_transport_peers("172.16.0.0/12,192.168.0.0/16,fd00::/8")
        assert parsed == ["172.16.0.0/12", "192.168.0.0/16", "fd00::/8"]

    def test_the_peers_alone_stop_startup(self):
        # An operator whose partners are refused behind a proxy is told to
        # trust the proxy, not left with an exemption that seems to work.
        with pytest.raises(ValueError, match="A2A_ALLOW_INSECURE_LOOPBACK") as refused:
            Settings(_env_file=None, test_only_insecure_transport_peers="172.16.0.0/12")
        assert "FORWARDED_ALLOW_IPS" in str(refused.value)

    def test_the_end_to_end_stack_starts(self):
        cfg = Settings(
            _env_file=None,
            test_only_insecure_transport_peers="172.16.0.0/12",
            a2a_allow_insecure_loopback=True,
        )
        assert cfg.test_only_insecure_transport_peers == ["172.16.0.0/12"]

    def test_never_on_by_default(self):
        cfg = Settings(_env_file=None)
        assert cfg.test_only_insecure_transport_peers == []
        assert cfg.a2a_allow_insecure_loopback is False


class TestForwardedSchemeParsing:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, None),
            ("", None),
            ("  ", None),
            (",", None),
            ("https", "https"),
            ("HTTPS", "https"),
            (" https ,http", "https"),
            ("ftp", "ftp"),
        ],
    )
    def test_it_parses_the_leftmost_hop(self, raw, expected):
        assert forwarded_scheme(raw) == expected


class TestTheForwardedProtoClaim:
    """The one reading of ``X-Forwarded-Proto`` behind the transport decision,
    ``external_base`` and the unconfirmed-transport report."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, None),
            ("", None),
            ("https", "https"),
            ("HTTPS, http", "https"),
            ("http", "http"),
            ("  ", "other"),
            (",", "other"),
            ("ftp", "other"),
            ("https<script>", "other"),
        ],
    )
    def test_it_classifies_the_claim_and_never_returns_the_raw_text(self, raw, expected):
        assert forwarded_proto_claim(_request(forwarded=raw)) == expected

    @pytest.mark.parametrize(("claim", "expected"), [("https", "https"), ("http", "http"), ("ftp", "http")])
    def test_external_base_reads_it_under_the_flag(self, monkeypatch, claim, expected):
        from app.core.utils import external_base

        monkeypatch.setattr(settings, "trust_proxy_headers", True)
        monkeypatch.setattr(settings, "base_url", "")
        request = SimpleNamespace(base_url="http://xtm.example.com/", headers={"x-forwarded-proto": claim})
        assert external_base(request) == f"{expected}://xtm.example.com"


def _guarded_app() -> FastAPI:
    """One GET route behind the credential routes' check and enrollment's refusal."""
    app = FastAPI()

    async def _guarded():
        return {"ok": True}

    app.router.add_api_route(
        "/guarded",
        _guarded,
        methods=["GET"],
        route_class_override=credential_transport_route(refuse_insecure_credential_transport),
    )
    return app


class TestTheCredentialRoutesByDefault:
    """Off (the default): served, and a transport the platform cannot confirm
    encrypted reported once per route."""

    def test_https_is_silent(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert refuses_credential_transport(_request(scheme="https")) is False
        assert caplog.text == ""

    def test_plain_http_is_served_and_reported_once_per_route(self, caplog):
        with caplog.at_level(logging.WARNING):
            for _ in range(5):
                assert refuses_credential_transport(_request()) is False
        assert caplog.text.count("FORWARDED_ALLOW_IPS") == 1
        assert "/guarded served over unconfirmed transport" in caplog.text

    def test_each_route_is_reported(self, caplog):
        other = _request()
        other.url.path = "/other"
        with caplog.at_level(logging.WARNING):
            refuses_credential_transport(_request())
            refuses_credential_transport(other)
        assert caplog.text.count("FORWARDED_ALLOW_IPS") == 2

    def test_debug_loopback_is_not_reported(self, monkeypatch, caplog):
        monkeypatch.setattr(settings, "debug", True)
        with caplog.at_level(logging.WARNING):
            assert refuses_credential_transport(_request(host="127.0.0.1")) is False
        assert caplog.text == ""

    def test_the_route_class_serves(self):
        app = _guarded_app()
        response = TestClient(app, base_url="http://testserver").get("/guarded")
        assert (response.status_code, response.json()) == (200, {"ok": True})


class TestTheStrictSwitch:
    """``CREDENTIAL_TRANSPORT_STRICT`` on: the A2A surface's refusal, with its exemptions."""

    @pytest.fixture(autouse=True)
    def _strict(self, monkeypatch):
        monkeypatch.setattr(settings, "credential_transport_strict", True)

    def test_it_is_silent_over_https(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert refuses_credential_transport(_request(scheme="https")) is False
        assert caplog.text == ""

    def test_plain_http_is_refused_and_reported_once_per_route(self, caplog):
        with caplog.at_level(logging.WARNING):
            for _ in range(5):
                assert refuses_credential_transport(_request()) is True
        assert caplog.text.count("FORWARDED_ALLOW_IPS") == 1
        assert "/guarded refused over unconfirmed transport" in caplog.text

    @pytest.mark.parametrize("flag", ["debug", "a2a_allow_insecure_loopback"])
    def test_real_loopback_keeps_its_exemption(self, monkeypatch, flag):
        monkeypatch.setattr(settings, flag, True)
        assert refuses_credential_transport(_request(host="127.0.0.1")) is False
        assert refuses_credential_transport(_request(host="127.0.0.1", forwarded_for="127.0.0.1")) is True

    def test_a_switch_turned_on_at_runtime_reports_the_refusal_too(self, monkeypatch, caplog):
        # The option changes without a restart (Settings > Access and security): the
        # once-per-route report is kept per outcome, so the refusal that
        # follows the served requests is logged as well.
        monkeypatch.setattr(settings, "credential_transport_strict", False)
        with caplog.at_level(logging.WARNING):
            assert refuses_credential_transport(_request()) is False
            monkeypatch.setattr(settings, "credential_transport_strict", True)
            for _ in range(3):
                assert refuses_credential_transport(_request()) is True
        assert caplog.text.count("/guarded served over unconfirmed transport") == 1
        assert caplog.text.count("/guarded refused over unconfirmed transport") == 1

    def test_the_a2a_surface_refuses_either_way(self, monkeypatch):
        from fastapi import HTTPException

        for strict in (True, False):
            monkeypatch.setattr(settings, "credential_transport_strict", strict)
            with pytest.raises(HTTPException):
                asyncio.run(require_secure_transport(_request()))

    def test_the_route_class_answers_the_terse_refusal(self):
        app = _guarded_app()
        assert TestClient(app, base_url="http://testserver").get("/guarded").json() == {
            "detail": deps.INSECURE_TRANSPORT_DETAIL
        }
        assert TestClient(app, base_url="https://testserver").get("/guarded").status_code == 200


class TestTheCredentialEndpoints:
    def test_enroll_carries_the_check_before_its_body(self):
        # FastAPI reads a declared body before any dependency: the check wraps the handler.
        route = next(r for r in runners.router.routes if r.path == "/enroll")
        assert type(route).__name__ == "CredentialTransportRoute"
        assert route.status_code == 201

    def test_strict_enroll_refuses_a_malformed_body_too(self, monkeypatch):
        # The dependency answers before FastAPI's json_invalid 422.
        monkeypatch.setattr(settings, "credential_transport_strict", True)
        app = FastAPI()
        app.include_router(runners.router, prefix="/api/v1/runners")
        response = TestClient(app, base_url="http://testserver").post(
            "/api/v1/runners/enroll", content=b"{", headers={"content-type": "application/json"}
        )
        assert (response.status_code, response.json()) == (403, {"detail": deps.INSECURE_TRANSPORT_DETAIL})

    def test_strict_enroll_refuses_plain_http_before_anything_else(self, monkeypatch):
        # No gate stubbed: the enrollment switch, the key lookup and the body
        # would each answer (or fail) on their own.
        monkeypatch.setattr(settings, "credential_transport_strict", True)
        app = FastAPI()
        app.include_router(runners.router, prefix="/api/v1/runners")
        response = TestClient(app, base_url="http://testserver").post("/api/v1/runners/enroll", json={})
        assert response.status_code == 403
        assert response.json() == {"detail": deps.INSECURE_TRANSPORT_DETAIL}

    def test_register_carries_the_check_before_its_body(self):
        # FastAPI reads a declared body before any dependency: the check wraps the handler.
        route = next(r for r in mcp_oauth.router.routes if r.path == "/register")
        assert isinstance(route, mcp_oauth._TransportFirstRoute)

    def test_strict_register_refuses_in_the_rfc_7591_envelope(self, monkeypatch):
        # RFC 7591 section 3.2.2, checked before the malformed body's 422.
        monkeypatch.setattr(settings, "credential_transport_strict", True)
        app = FastAPI()
        app.include_router(mcp_oauth.router, prefix="/mcp/oauth")
        response = TestClient(app, base_url="http://testserver").post(
            "/mcp/oauth/register", content=b"{", headers={"content-type": "application/json"}
        )
        assert response.status_code == 400
        assert response.json() == {
            "error": "invalid_client_metadata",
            "error_description": deps.INSECURE_TRANSPORT_DETAIL,
        }

    def test_strict_token_endpoint_refuses_in_the_oauth_envelope(self, monkeypatch):
        # RFC 6749 section 5.2: an OAuth client parses ``error``, not ``detail``.
        monkeypatch.setattr(settings, "credential_transport_strict", True)
        app = FastAPI()
        app.include_router(mcp_oauth.router, prefix="/mcp/oauth")
        response = TestClient(app, base_url="http://testserver").post(
            "/mcp/oauth/token", data={"grant_type": "client_credentials"}
        )
        assert response.status_code == 400
        assert response.json() == {
            "error": "invalid_request",
            "error_description": deps.INSECURE_TRANSPORT_DETAIL,
        }


_ENROLL = "/api/v1/runners/enroll"
_REGISTER = "/mcp/oauth/register"
_TOKEN = "/mcp/oauth/token"
_UNTRUSTED_PROXY = ("10.42.0.7", 50000)
_PROXY_CLAIM = {"X-Forwarded-Proto": "https", "X-Forwarded-For": "198.51.100.7"}

#: Every deployment shape: (name, base_url, client, headers, prefix, trusted
#: proxy for uvicorn, trust_proxy_headers, debug, confirmed). ``confirmed`` is
#: whether the platform can tell the request arrived over HTTPS.
_SHAPES = [
    ("proxy_not_declared", "http://testserver", _UNTRUSTED_PROXY, _PROXY_CLAIM, "", None, False, False, False),
    ("plain_http_direct", "http://testserver", ("203.0.113.9", 50000), {}, "", None, False, False, False),
    ("https_direct", "https://testserver", ("203.0.113.9", 50000), {}, "", None, False, False, True),
    ("loopback_dev_build", "http://testserver", ("127.0.0.1", 50000), {}, "", None, False, True, True),
    ("loopback_without_debug", "http://testserver", ("127.0.0.1", 50000), {}, "", None, False, False, False),
    ("proxy_in_forwarded_allow_ips", "http://testserver", _UNTRUSTED_PROXY, _PROXY_CLAIM, "", "10.42.0.7", False, False, True),
    ("trust_proxy_headers", "http://testserver", _UNTRUSTED_PROXY, _PROXY_CLAIM, "", None, True, False, True),
    ("base_path_forwarded_https", "https://testserver", ("203.0.113.9", 50000), {}, "/xtm-one", None, False, False, True),
    ("base_path_forwarded_http", "http://testserver", _UNTRUSTED_PROXY, _PROXY_CLAIM, "/xtm-one", None, False, False, False),
    (
        "base_path_stripped_http",
        "http://testserver",
        _UNTRUSTED_PROXY,
        {**_PROXY_CLAIM, "X-Forwarded-Prefix": "/xtm-one"},
        "",
        None,
        False,
        False,
        False,
    ),
]


def _deployment(monkeypatch, shape):
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    from app.core.base_path import BasePathMiddleware

    _name, base_url, client, headers, prefix, trusted, trust_flag, debug, _confirmed = shape
    monkeypatch.setattr(settings, "trust_proxy_headers", trust_flag)
    monkeypatch.setattr(settings, "debug", debug)
    app = FastAPI()
    app.include_router(runners.router, prefix="/api/v1/runners")
    app.include_router(mcp_oauth.router, prefix="/mcp/oauth")
    app.add_middleware(BasePathMiddleware, base_path="/xtm-one")
    asgi = ProxyHeadersMiddleware(app, trusted_hosts=trusted) if trusted else app
    return TestClient(asgi, base_url=base_url, client=client), headers, prefix


class TestEveryDeploymentShape:
    """The default changes nothing anywhere; the switch refuses only the unconfirmed.

    What each route answers when the transport is let through is the answer
    it gives without the strict mode: the enrollment and registration bodies are validated
    (422), and the token endpoint reads an unreadable body as
    ``invalid_request`` with no description.
    """

    @pytest.mark.parametrize("shape", _SHAPES, ids=[shape[0] for shape in _SHAPES])
    @pytest.mark.parametrize("strict", [False, True], ids=["default", "strict"])
    def test_enroll_register_and_token(self, monkeypatch, shape, strict):
        monkeypatch.setattr(settings, "credential_transport_strict", strict)
        client, headers, prefix = _deployment(monkeypatch, shape)
        enroll = client.post(prefix + _ENROLL, json={}, headers=headers)
        register = client.post(prefix + _REGISTER, json={"redirect_uris": "not-a-list"}, headers=headers)
        token = client.post(
            prefix + _TOKEN, content=b"[]", headers={"content-type": "application/json", **headers}
        )
        refused = strict and not shape[-1]
        if refused:
            assert (enroll.status_code, enroll.json()) == (403, {"detail": deps.INSECURE_TRANSPORT_DETAIL})
            assert (register.status_code, register.json()) == (
                400,
                {"error": "invalid_client_metadata", "error_description": deps.INSECURE_TRANSPORT_DETAIL},
            )
            assert (token.status_code, token.json()) == (
                400,
                {"error": "invalid_request", "error_description": deps.INSECURE_TRANSPORT_DETAIL},
            )
        else:
            assert enroll.status_code == 422
            assert "https required" not in enroll.text
            assert register.status_code == 422
            assert "https required" not in register.text
            assert (token.status_code, token.json()) == (400, {"error": "invalid_request"})


_A2A_ROUTES = [
    ("get", "/a2a/tok/.well-known/agent-card.json"),
    ("get", "/a2a/partner/agents"),
    ("post", "/a2a/tok/jsonrpc"),
    ("post", "/a2a/callbacks/deleg"),
]


def _a2a_app():
    app = FastAPI()
    app.include_router(a2a_server.router)
    app.include_router(a2a_callbacks.router)
    return app


class TestTheA2ASurfaceRefuses:
    """Every A2A route, driven over http, not just the predicate in isolation."""

    @pytest.mark.parametrize(("method", "path"), _A2A_ROUTES)
    def test_every_route_refuses_before_any_other_gate(self, method, path, caplog):
        # No gate is stubbed: the rate limit, AI state, licence and exposure
        # lookup would each answer (or fail) on their own. The terse body is
        # the transport refusal's, so this also pins that it runs first.
        client = TestClient(_a2a_app(), base_url="http://testserver")
        with caplog.at_level(logging.WARNING):
            response = client.request(method, path, json={})
        assert response.status_code == 403
        assert response.json() == {"detail": deps.INSECURE_TRANSPORT_DETAIL}

    def test_the_refusal_says_nothing_about_the_deployment(self, caplog):
        client = TestClient(_a2a_app(), base_url="http://testserver", client=("203.0.113.5", 50000))
        with caplog.at_level(logging.WARNING):
            response = client.get("/a2a/partner/agents")
        assert "FORWARDED_ALLOW_IPS" not in response.text
        # The remediation is the operator's, with what they need to correlate.
        assert "FORWARDED_ALLOW_IPS" in caplog.text
        assert "/a2a/partner/agents refused" in caplog.text
        assert "scheme=http" in caplog.text
        assert "peer=203.0.113.5" in caplog.text
        assert "x_forwarded_proto=none, x_forwarded_for=absent" in caplog.text

    def test_an_untrusted_proxy_is_recognisable_in_the_log(self, caplog):
        # A TLS-terminating proxy the deployment does not trust: its https
        # claim is not believed, and the operator is shown both it and the
        # peer to put in FORWARDED_ALLOW_IPS.
        client = TestClient(_a2a_app(), base_url="http://testserver", client=("203.0.113.5", 50000))
        with caplog.at_level(logging.WARNING):
            response = client.get(
                "/a2a/partner/agents",
                headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "198.51.100.7"},
            )
        assert response.status_code == 403
        assert response.json() == {"detail": deps.INSECURE_TRANSPORT_DETAIL}
        assert "peer=203.0.113.5, x_forwarded_proto=https, x_forwarded_for=present" in caplog.text

    def test_a_peer_that_is_not_an_address_is_never_logged(self, caplog):
        # FORWARDED_ALLOW_IPS="*": uvicorn copies the leftmost X-Forwarded-For
        # value into scope["client"] without checking it is an address.
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        app = ProxyHeadersMiddleware(_a2a_app(), trusted_hosts="*")
        client = TestClient(app, base_url="http://testserver", client=("203.0.113.5", 50000))
        with caplog.at_level(logging.WARNING):
            response = client.get("/a2a/partner/agents", headers={"X-Forwarded-For": "not-an-address-<x>"})
        assert response.status_code == 403
        assert "peer=None, x_forwarded_proto=none, x_forwarded_for=present" in caplog.text
        assert "not-an-address" not in caplog.text

    def test_the_log_says_when_the_peer_may_be_a_client_a_proxy_named(self, caplog):
        # A trusted proxy sending X-Forwarded-For without X-Forwarded-Proto:
        # uvicorn has replaced the peer with the client, so the log must not
        # read as naming the proxy to trust.
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        app = ProxyHeadersMiddleware(_a2a_app(), trusted_hosts="testclient")
        client = TestClient(app, base_url="http://testserver")
        with caplog.at_level(logging.WARNING):
            response = client.get("/a2a/partner/agents", headers={"X-Forwarded-For": "198.51.100.7"})
        assert response.status_code == 403
        assert "peer=198.51.100.7, x_forwarded_proto=none, x_forwarded_for=present" in caplog.text

    def test_the_log_never_carries_the_raw_claim(self, caplog):
        client = TestClient(_a2a_app(), base_url="http://testserver")
        with caplog.at_level(logging.WARNING):
            client.get("/a2a/partner/agents", headers={"X-Forwarded-Proto": "https<script>"})
        assert "x_forwarded_proto=other" in caplog.text
        assert "<script>" not in caplog.text

    @pytest.mark.parametrize(("method", "path"), _A2A_ROUTES)
    def test_https_is_not_refused_on_transport(self, monkeypatch, method, path):
        async def _noop(*_args, **_kwargs):
            return None

        async def _ai_on(_db=None):
            return True

        monkeypatch.setattr(a2a_server, "_enforce_preauth_rate_limit", _noop)
        monkeypatch.setattr(a2a_callbacks, "_enforce_preauth_rate_limit", _noop)
        monkeypatch.setattr(deps, "require_chat_enabled", _noop)
        monkeypatch.setattr(ai_state, "is_ai_enabled", _ai_on)
        # The exposure lookup still wants a database it may not have.
        client = TestClient(
            _a2a_app(), base_url="https://testserver", raise_server_exceptions=False
        )
        response = client.request(method, path, json={})
        assert response.status_code != 403 or response.json() != {
            "detail": deps.INSECURE_TRANSPORT_DETAIL
        }

    @pytest.mark.parametrize("router", [a2a_server.router, a2a_callbacks.router])
    def test_the_routers_carry_the_refusal_not_the_warning(self, router):
        calls = [dep.dependency for dep in router.dependencies]
        assert require_secure_transport in calls


class TestTheDocumentedChainOrdering:
    """Pins the ``.env.sample`` claim to the installed uvicorn, not to memory."""

    def _client_host(self, trusted, xff):
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        seen = {}

        async def app(scope, receive, send):
            seen["client"] = scope["client"]

        scope = {
            "type": "http",
            "scheme": "http",
            "client": ("10.0.0.5", 1234),
            "headers": [(b"x-forwarded-for", xff)],
        }
        asyncio.run(ProxyHeadersMiddleware(app, trusted_hosts=trusted)(scope, None, None))
        return seen["client"][0]

    def test_star_reads_the_start_of_the_chain(self):
        assert self._client_host("*", b"1.2.3.4, 9.9.9.9") == "1.2.3.4"

    def test_a_pinned_peer_reads_the_end(self):
        assert self._client_host("10.0.0.0/8", b"1.2.3.4, 9.9.9.9") == "9.9.9.9"
