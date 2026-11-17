"""``POST /runners/enroll`` checks its transport before its body is read.

Off (the default) changes nothing: the route as main declared it - a plain
``APIRoute`` whose transport check was a dependency - and the route as it is
mounted now give every request the same status, headers and body, whatever
the transport. On, an unconfirmed transport gets ``403 https required`` for
every body, a malformed one included, without a byte of it read; a confirmed
one is answered as on main.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, status
from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1 import runners
from app.core.config import settings
from app.core.database import get_db

_ENROLL = "/api/v1/runners/enroll"
_REFUSAL = {"detail": deps.INSECURE_TRANSPORT_DETAIL}
_VALID = {"enrollment_key": "renr-unknown", "platform": "linux"}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    for name, value in (
        ("trust_proxy_headers", False),
        ("debug", False),
        ("a2a_allow_insecure_loopback", False),
        ("test_only_insecure_transport_peers", []),
        ("credential_transport_strict", False),
        ("runners_enabled", True),
    ):
        monkeypatch.setattr(settings, name, value)
    deps._reported_paths.clear()

    async def _no_such_key(_raw, _db):
        return None

    monkeypatch.setattr(runners, "resolve_enrollment_key", _no_such_key)


async def _main_transport_check(request: Request) -> None:
    """main's ``require_secure_credential_transport``, verbatim."""
    if deps.refuses_credential_transport(request):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=deps.INSECURE_TRANSPORT_DETAIL)


def _main_router() -> APIRouter:
    """The enrollment route exactly as main declared it: the check as its last dependency."""

    async def enroll_runner(
        body: runners.EnrollRequest,
        db=Depends(get_db),
        _secure: None = Depends(_main_transport_check),
    ):
        return await runners.enroll_runner(body, db)

    enroll_runner.__doc__ = runners.enroll_runner.__doc__
    router = APIRouter()
    router.post("/enroll", status_code=status.HTTP_201_CREATED)(enroll_runner)
    return router


def _app(router: APIRouter) -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/runners")

    async def _db():
        yield object()

    app.dependency_overrides[get_db] = _db
    return app


def _post(app: FastAPI, transport, request: dict):
    _name, base_url, client, headers, _debug, _confirmed = transport
    kwargs = dict(request)
    merged = {**headers, **kwargs.pop("headers", {})}
    return TestClient(app, base_url=base_url, client=client).post(_ENROLL, headers=merged, **kwargs)


def _answer(response) -> tuple:
    return response.status_code, list(response.headers.items()), response.content


#: (id, base_url, client, headers, debug, confirmed)
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


def _runners_disabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "runners_enabled", False)


#: (id, request, setup): every answer enrollment gives before it needs a real key.
_SCENARIOS = [
    ("unknown_key", {"json": _VALID}, None),
    ("invalid_platform", {"json": {**_VALID, "platform": "amiga"}}, None),
    ("missing_fields", {"json": {}}, None),
    ("invalid_body_shape", {"json": {**_VALID, "machine_id": "x" * 200}}, None),
    ("malformed_json", {"content": b"{", "headers": {"content-type": "application/json"}}, None),
    ("form_encoded", {"data": {"enrollment_key": "renr-x"}}, None),
    ("no_body", {}, None),
    ("runners_disabled", {"json": _VALID}, _runners_disabled),
]


class TestSwitchOffIsMain:
    @pytest.mark.parametrize("transport", _TRANSPORTS, ids=[t[0] for t in _TRANSPORTS])
    @pytest.mark.parametrize("scenario", _SCENARIOS, ids=[s[0] for s in _SCENARIOS])
    def test_every_answer_is_main_s(self, monkeypatch, scenario, transport):
        _name, request, setup = scenario
        monkeypatch.setattr(settings, "debug", transport[4])
        if setup is not None:
            setup(monkeypatch)
        main = _post(_app(_main_router()), transport, request)
        now = _post(_app(runners.router), transport, request)
        assert _answer(now) == _answer(main)

    def test_the_route_is_documented_as_on_main(self):
        main, now = FastAPI(), FastAPI()
        main.include_router(_main_router(), prefix="/api/v1/runners")
        now.include_router(runners.router, prefix="/api/v1/runners")
        main_op = main.openapi()["paths"][_ENROLL]["post"]
        now_op = now.openapi()["paths"][_ENROLL]["post"]
        assert now_op == main_op
        schema = "EnrollRequest"
        assert now.openapi()["components"]["schemas"][schema] == main.openapi()["components"]["schemas"][schema]


class TestSwitchOn:
    @pytest.fixture(autouse=True)
    def _strict(self, monkeypatch):
        monkeypatch.setattr(settings, "credential_transport_strict", True)

    @pytest.mark.parametrize("transport", _TRANSPORTS, ids=[t[0] for t in _TRANSPORTS])
    @pytest.mark.parametrize("scenario", _SCENARIOS, ids=[s[0] for s in _SCENARIOS])
    def test_only_an_unconfirmed_transport_is_refused(self, monkeypatch, scenario, transport):
        _name, request, setup = scenario
        monkeypatch.setattr(settings, "debug", transport[4])
        if setup is not None:
            setup(monkeypatch)
        main = _post(_app(_main_router()), transport, request)
        now = _post(_app(runners.router), transport, request)
        if transport[-1]:
            assert _answer(now) == _answer(main)
            return
        assert (now.status_code, now.json()) == (403, _REFUSAL)
        # The one strict-only difference: main answered a malformed body's 422 first.
        if scenario[0] == "malformed_json":
            assert main.status_code == 422
        else:
            assert _answer(now) == _answer(main)


def _raw_enroll(app: FastAPI, body: bytes | None) -> tuple[int, bytes, int]:
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
        "path": _ENROLL,
        "raw_path": _ENROLL.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"testserver"), (b"content-type", b"application/json")],
        "client": ("203.0.113.9", 50000),
        "server": ("testserver", 80),
    }
    asyncio.run(app(scope, receive, send))
    status_code = next(m["status"] for m in sent if m["type"] == "http.response.start")
    content = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status_code, content, reads


class TestBeforeTheBodyIsRead:
    def test_strict_refuses_without_reading_a_byte(self, monkeypatch):
        monkeypatch.setattr(settings, "credential_transport_strict", True)
        status_code, content, reads = _raw_enroll(_app(runners.router), None)
        assert (status_code, json.loads(content), reads) == (403, _REFUSAL, 0)

    def test_off_reads_it_as_main_does(self):
        # The same probe sees a read: the strict case above is not vacuous.
        status_code, _content, reads = _raw_enroll(_app(runners.router), json.dumps(_VALID).encode())
        assert status_code == 401
        assert reads >= 1


def test_the_application_mounts_the_checked_route():
    from app.main import application

    [route] = [r for r in application.routes if getattr(r, "path", None) == _ENROLL]
    assert type(route).__name__ == "CredentialTransportRoute"
    assert route.methods == {"POST"}
    assert route.endpoint is runners.enroll_runner
