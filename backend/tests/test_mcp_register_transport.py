"""``POST /mcp/oauth/register`` under ``CREDENTIAL_TRANSPORT_STRICT``.

Off (the default) changes nothing: the route as main mounted it - a plain
``APIRoute`` around the unchanged ``register_client`` - and the route as it is
mounted now give every request the same status, headers and body, whatever
the transport. On, a transport the platform cannot confirm encrypted gets RFC
7591's registration error before the body is read and before anything is
spent or issued; a confirmed one is answered as on main.
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1 import mcp_oauth
from app.core.config import settings
from app.core.database import get_db

_REGISTER = "/mcp/oauth/register"
_REDIRECT_URI = "https://app.example.com/callback"
_PUBLIC = {"client_name": "VS Code", "redirect_uris": [_REDIRECT_URI]}
_REFUSAL = {"error": "invalid_client_metadata", "error_description": deps.INSECURE_TRANSPORT_DETAIL}
_RFC_7591_ERROR_CODES = {
    "invalid_redirect_uri",
    "invalid_client_metadata",
    "invalid_software_statement",
    "unapproved_software_statement",
}


class _StubDB:
    """Just enough session for a registration below the client cap."""

    def __init__(self) -> None:
        self.added: list = []
        self.commits = 0

    async def scalar(self, _stmt):
        return 0

    async def execute(self, _stmt, _params=None):
        return SimpleNamespace(rowcount=0)

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    for name, value in (
        ("trust_proxy_headers", False),
        ("debug", False),
        ("a2a_allow_insecure_loopback", False),
        ("test_only_insecure_transport_peers", []),
        ("credential_transport_strict", False),
        ("mcp_dcr_enabled", True),
        ("mcp_dcr_max_clients", 500),
    ):
        monkeypatch.setattr(settings, name, value)
    deps._reported_paths.clear()


@pytest.fixture
def spent(monkeypatch):
    """What the requests spent: rate-limit checks and database sessions."""
    record = SimpleNamespace(rate_limit_calls=0, sessions=[])

    async def _rate_limit(_request, _name, *, limit, window_seconds):
        record.rate_limit_calls += 1

    monkeypatch.setattr(mcp_oauth, "enforce_rate_limit", _rate_limit)
    return record


def _main_router() -> APIRouter:
    """The registration route exactly as main declared it."""
    router = APIRouter()
    router.post("/register")(mcp_oauth.register_client)
    return router


def _app(router: APIRouter, record) -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/mcp/oauth")

    async def _db():
        session = _StubDB()
        record.sessions.append(session)
        yield session

    app.dependency_overrides[get_db] = _db
    return app


def _post(app: FastAPI, transport, request: dict):
    _name, base_url, client, headers, _debug, _confirmed = transport
    kwargs = dict(request)
    merged = {**headers, **kwargs.pop("headers", {})}
    return TestClient(app, base_url=base_url, client=client).post(_REGISTER, headers=merged, **kwargs)


def _answer(response) -> tuple:
    """Status, headers in order and body, the two values a registration mints at random masked."""
    try:
        parsed = json.loads(response.content)
    except ValueError:
        return response.status_code, list(response.headers.items()), response.content
    if isinstance(parsed, dict):
        for key in ("client_id", "client_secret"):
            if key in parsed:
                parsed[key] = f"<{key}: {len(parsed[key])} characters>"
        parsed = list(parsed.items())
    return response.status_code, list(response.headers.items()), parsed


#: (id, base_url, client, headers, debug, confirmed): whether the platform can
#: tell the request arrived over HTTPS.
_TRANSPORTS = [
    ("plain_http", "http://testserver", ("203.0.113.9", 50000), {}, False, False),
    (
        "untrusted_proxy_https_claim",
        "http://testserver",
        ("10.42.0.7", 50000),
        {"X-Forwarded-Proto": "https", "X-Forwarded-For": "198.51.100.7"},
        False,
        False,
    ),
    ("https", "https://testserver", ("203.0.113.9", 50000), {}, False, True),
    ("loopback_in_debug", "http://testserver", ("127.0.0.1", 50000), {}, True, True),
]


def _dcr_disabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "mcp_dcr_enabled", False)


def _throttled(monkeypatch) -> None:
    async def _rate_limit(*_args, **_kwargs):
        raise HTTPException(status_code=429, detail="Too many requests", headers={"Retry-After": "3600"})

    monkeypatch.setattr(mcp_oauth, "enforce_rate_limit", _rate_limit)


#: (id, request, setup): every answer the route gives on main.
_SCENARIOS = [
    ("public_client", {"json": _PUBLIC}, None),
    ("confidential_client", {"json": {**_PUBLIC, "token_endpoint_auth_method": "client_secret_post"}}, None),
    ("defaults_only", {"json": {}}, None),
    ("invalid_redirect_uri", {"json": {**_PUBLIC, "redirect_uris": ["http://app.example.com/cb"]}}, None),
    ("invalid_body_shape", {"json": {"redirect_uris": "not-a-list"}}, None),
    ("malformed_json", {"content": b"{", "headers": {"content-type": "application/json"}}, None),
    ("form_encoded", {"data": {"client_name": "VS Code"}}, None),
    ("no_body", {}, None),
    ("dcr_disabled", {"json": _PUBLIC}, _dcr_disabled),
    ("throttled", {"json": _PUBLIC}, _throttled),
]


class TestSwitchOffIsMain:
    @pytest.mark.parametrize("transport", _TRANSPORTS, ids=[t[0] for t in _TRANSPORTS])
    @pytest.mark.parametrize("scenario", _SCENARIOS, ids=[s[0] for s in _SCENARIOS])
    def test_every_answer_is_main_s(self, monkeypatch, spent, scenario, transport):
        _name, request, setup = scenario
        monkeypatch.setattr(settings, "debug", transport[4])
        if setup is not None:
            setup(monkeypatch)
        main = _post(_app(_main_router(), spent), transport, request)
        now = _post(_app(mcp_oauth.router, spent), transport, request)
        assert _answer(now) == _answer(main)

    def test_the_differential_sees_a_minted_secret(self, spent):
        # The masking keeps the fields it masks: a confidential client still
        # receives its secret, a public one none.
        transport = _TRANSPORTS[0]
        confidential = _post(
            _app(mcp_oauth.router, spent),
            transport,
            {"json": {**_PUBLIC, "token_endpoint_auth_method": "client_secret_post"}},
        )
        public = _post(_app(mcp_oauth.router, spent), transport, {"json": _PUBLIC})
        assert confidential.status_code == public.status_code == 201
        assert confidential.json()["client_secret"]
        assert "client_secret" not in public.json()

    def test_the_route_is_documented_as_on_main(self):
        main, now = FastAPI(), FastAPI()
        main.include_router(_main_router(), prefix="/mcp/oauth")
        now.include_router(mcp_oauth.router, prefix="/mcp/oauth")
        assert now.openapi()["paths"][_REGISTER] == main.openapi()["paths"][_REGISTER]
        schemas = ("ClientRegistrationRequest", "HTTPValidationError", "ValidationError")
        for schema in schemas:
            assert now.openapi()["components"]["schemas"][schema] == main.openapi()["components"]["schemas"][schema]

    def test_an_unconfirmed_transport_is_reported_once_like_the_other_credential_routes(
        self, spent, caplog
    ):
        # The one addition off: the log line enroll and token already write,
        # so "no unconfirmed-transport warning left" still names every request
        # the switch would refuse.
        client = TestClient(_app(mcp_oauth.router, spent), base_url="http://testserver")
        with caplog.at_level(logging.WARNING, logger="app.api.deps"):
            for _ in range(3):
                assert client.post(_REGISTER, json=_PUBLIC).status_code == 201
        assert caplog.text.count(f"{_REGISTER} served over unconfirmed transport") == 1

    def test_https_is_not_reported(self, spent, caplog):
        client = TestClient(_app(mcp_oauth.router, spent), base_url="https://testserver")
        with caplog.at_level(logging.WARNING, logger="app.api.deps"):
            assert client.post(_REGISTER, json=_PUBLIC).status_code == 201
        assert caplog.text == ""


class TestSwitchOn:
    @pytest.fixture(autouse=True)
    def _strict(self, monkeypatch):
        monkeypatch.setattr(settings, "credential_transport_strict", True)

    @pytest.mark.parametrize("transport", _TRANSPORTS, ids=[t[0] for t in _TRANSPORTS])
    @pytest.mark.parametrize("scenario", _SCENARIOS, ids=[s[0] for s in _SCENARIOS])
    def test_only_an_unconfirmed_transport_is_refused(self, monkeypatch, spent, scenario, transport):
        _name, request, setup = scenario
        monkeypatch.setattr(settings, "debug", transport[4])
        if setup is not None:
            setup(monkeypatch)
        main = _post(_app(_main_router(), spent), transport, request)
        before = (spent.rate_limit_calls, len(spent.sessions))
        now = _post(_app(mcp_oauth.router, spent), transport, request)
        if transport[-1]:
            assert _answer(now) == _answer(main)
            return
        assert (now.status_code, now.json()) == (400, _REFUSAL)
        assert now.headers["content-type"] == "application/json"
        # Nothing spent or issued: no rate-limit check, no session, no row.
        assert (spent.rate_limit_calls, len(spent.sessions)) == before

    def test_the_refusal_is_rfc_7591_s_error_shape(self, spent):
        response = TestClient(_app(mcp_oauth.router, spent), base_url="http://testserver").post(
            _REGISTER, json=_PUBLIC
        )
        assert response.status_code == 400
        body = response.json()
        assert set(body) == {"error", "error_description"}
        assert body["error"] in _RFC_7591_ERROR_CODES

    def test_the_refusal_is_reported_once(self, spent, caplog):
        client = TestClient(_app(mcp_oauth.router, spent), base_url="http://testserver")
        with caplog.at_level(logging.WARNING, logger="app.api.deps"):
            responses = [client.post(_REGISTER, json=_PUBLIC) for _ in range(3)]
        assert [response.json() for response in responses] == [_REFUSAL] * 3
        assert caplog.text.count(f"{_REGISTER} refused over unconfirmed transport") == 1
        # The remediation is the operator's: the caller learns nothing about the deployment.
        assert "FORWARDED_ALLOW_IPS" in caplog.text
        assert all("FORWARDED_ALLOW_IPS" not in response.text for response in responses)

    def test_the_decision_is_the_shared_helper(self, monkeypatch, spent):
        # No second spelling of the transport decision: the route answers what
        # refuses_credential_transport says, whatever the request looks like.
        asked: list[str] = []

        def _refuse(request) -> bool:
            asked.append(request.url.path)
            return True

        monkeypatch.setattr(deps, "refuses_credential_transport", _refuse)
        response = TestClient(_app(mcp_oauth.router, spent), base_url="https://testserver").post(
            _REGISTER, json=_PUBLIC
        )
        assert (response.status_code, response.json()) == (400, _REFUSAL)
        assert asked == [_REGISTER]


def _raw_register(app: FastAPI, body: bytes | None) -> tuple[int, bytes, int]:
    """One ASGI request with a ``receive`` that counts reads; ``None`` fails any read."""
    reads = 0
    sent: list[dict] = []

    async def receive():
        nonlocal reads
        reads += 1
        if body is None:
            raise AssertionError("the request body must not be read")
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": _REGISTER,
        "raw_path": _REGISTER.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"testserver"), (b"content-type", b"application/json"), (b"content-length", b"2")],
        "client": ("203.0.113.9", 50000),
        "server": ("testserver", 80),
    }
    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    content = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, content, reads


class TestBeforeTheBodyIsRead:
    def test_strict_refuses_without_reading_a_byte(self, monkeypatch, spent):
        monkeypatch.setattr(settings, "credential_transport_strict", True)
        status, content, reads = _raw_register(_app(mcp_oauth.router, spent), None)
        assert (status, json.loads(content), reads) == (400, _REFUSAL, 0)

    def test_off_reads_it_as_main_does(self, spent):
        # The same probe sees a read: the strict case above is not vacuous.
        status, _content, reads = _raw_register(_app(mcp_oauth.router, spent), b"{}")
        assert status == 201
        assert reads >= 1


def test_the_application_mounts_the_checked_route():
    from app.main import application

    [route] = [r for r in application.routes if getattr(r, "path", None) == _REGISTER]
    assert isinstance(route, mcp_oauth._TransportFirstRoute)
    assert route.methods == {"POST"}
    assert route.endpoint is mcp_oauth.register_client
