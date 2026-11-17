"""Tests for the background startup gate (crash-loop fix).

Verifies that:
1. GET /api/health (liveness) always returns 200 — even before startup completes.
2. GET /api/health/ready (readiness) passes through to the router during
   startup so it can return the detailed ``status=starting`` 503, never held.
3. A request reaching a worker whose startup is still RUNNING is held until
   startup completes and then served; the hold is bounded by
   ``STARTUP_HOLD_SECONDS``, after which it gets 503 + Retry-After. A worker
   is started while its pod already serves - the replacement of a worker
   recycled after MAX_REQUESTS, the sibling of the worker that made the pod
   Ready - and refusing its share of the traffic outright was a production
   outage of every request it received.
4. A request reaching a worker whose startup FAILED (or ends failing while it
   is held) gets 503 + Retry-After at once.
5. Once ``_startup_done`` is set, normal routes pass through the middleware.
6. The gate defaults to OPEN at import time so consumers that never run the
   lifespan (unit tests, scripts) keep normal request behaviour.
7. WebSocket handshakes follow the same rule: held while startup runs,
   rejected when it failed or did not finish in time (the DB may be
   mid-migration), passed through afterwards.
8. ``_run_startup_tasks`` settles the gate whatever the startup's outcome,
   cancellation included, so held requests never wait out their hold for
   nothing.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route, WebSocketRoute
from starlette.testclient import TestClient
from starlette.types import ASGIApp, Message
from starlette.websockets import WebSocket, WebSocketDisconnect

import app.main as main_module

StartupState = Literal["done", "starting", "failed"]


@contextmanager
def _startup_state(state: StartupState) -> Iterator[tuple[asyncio.Event, asyncio.Event]]:
    """Swap the gate's two events for fresh ones in the requested state."""
    done = asyncio.Event()
    settled = asyncio.Event()
    if state == "done":
        done.set()
    if state != "starting":
        settled.set()

    original = (main_module._startup_done, main_module._startup_settled)
    main_module._startup_done = done
    main_module._startup_settled = settled
    try:
        yield done, settled
    finally:
        main_module._startup_done, main_module._startup_settled = original


def _gated_app(routes: list[BaseRoute]) -> ASGIApp:
    return main_module.ConcurrencyLimitMiddleware(Starlette(routes=routes), limit=100)


@contextmanager
def _gated_client(routes: list[BaseRoute], *, state: StartupState) -> Iterator[TestClient]:
    """TestClient over ``ConcurrencyLimitMiddleware`` with a patched gate."""
    with _startup_state(state):
        yield TestClient(_gated_app(routes), raise_server_exceptions=False)


def _async_client(routes: list[BaseRoute]) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_gated_app(routes)), base_url="http://testserver"
    )


async def _ok(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


async def _agents(request: Request) -> JSONResponse:
    return JSONResponse({"agents": []})


def _assert_initializing_503(resp: httpx.Response) -> None:
    assert resp.status_code == 503
    assert "initializing" in resp.json().get("detail", "").lower()
    assert resp.headers.get("retry-after") == "5"


def test_startup_gate_defaults_open_without_lifespan():
    """The module-level gate must default to SET so any consumer that never
    runs the lifespan (unit tests, scripts, ad-hoc TestClient usage) gets
    normal request behaviour instead of blanket 503s or held requests."""
    assert main_module._startup_done.is_set()
    assert main_module._startup_settled.is_set()


def test_the_hold_stays_under_a_webhook_delivery_timeout():
    """GitHub waits 10 s for a webhook answer and never redelivers: a hold
    longer than that turns a held delivery into a lost one."""
    assert 0 < main_module.STARTUP_HOLD_SECONDS < 10


def test_liveness_always_200_before_startup(monkeypatch):
    """GET /api/health must return 200 at once regardless of startup state."""
    monkeypatch.setattr(main_module, "STARTUP_HOLD_SECONDS", 30.0)
    with _gated_client([Route("/api/health", _ok)], state="starting") as client:
        began = time.monotonic()
        resp = client.get("/api/health")

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert time.monotonic() - began < 5


def test_non_health_routes_return_503_at_once_when_startup_failed(monkeypatch):
    """A failed startup never admits a request: answer it immediately rather
    than holding it for a startup that is over."""
    monkeypatch.setattr(main_module, "STARTUP_HOLD_SECONDS", 30.0)
    with _gated_client([Route("/api/v1/agents", _agents)], state="failed") as client:
        began = time.monotonic()
        resp = client.get("/api/v1/agents")

    _assert_initializing_503(resp)
    assert time.monotonic() - began < 5


def test_non_health_routes_return_503_once_the_hold_runs_out(monkeypatch):
    """A startup still running after ``STARTUP_HOLD_SECONDS`` answers the
    request it held with the initializing 503."""
    monkeypatch.setattr(main_module, "STARTUP_HOLD_SECONDS", 0.05)
    with _gated_client([Route("/api/v1/agents", _agents)], state="starting") as client:
        began = time.monotonic()
        resp = client.get("/api/v1/agents")

    _assert_initializing_503(resp)
    assert time.monotonic() - began >= 0.05


def test_routes_pass_through_after_startup():
    """Business routes return normally once _startup_done is set."""
    with _gated_client([Route("/api/v1/agents", _agents)], state="done") as client:
        resp = client.get("/api/v1/agents")

    assert resp.status_code == 200


async def test_request_held_during_startup_is_served_once_startup_completes(monkeypatch):
    """The request reaching a worker mid-startup waits for it and is served:
    the answer the load balancer's client gets is the route's, not a 503."""
    monkeypatch.setattr(main_module, "STARTUP_HOLD_SECONDS", 30.0)
    with _startup_state("starting") as (done, settled):
        async with _async_client([Route("/api/v1/agents", _agents)]) as client:
            pending = asyncio.create_task(client.get("/api/v1/agents"))
            await asyncio.sleep(0.05)
            assert not pending.done(), "the request must be held, not refused"

            done.set()
            settled.set()
            resp = await asyncio.wait_for(pending, 5)

    assert resp.status_code == 200
    assert resp.json() == {"agents": []}


async def test_held_request_gets_503_as_soon_as_startup_fails(monkeypatch):
    """A startup that fails while a request is held releases it at once."""
    monkeypatch.setattr(main_module, "STARTUP_HOLD_SECONDS", 30.0)
    with _startup_state("starting") as (_done, settled):
        async with _async_client([Route("/api/v1/agents", _agents)]) as client:
            pending = asyncio.create_task(client.get("/api/v1/agents"))
            await asyncio.sleep(0.05)
            assert not pending.done()

            settled.set()
            resp = await asyncio.wait_for(pending, 5)

    _assert_initializing_503(resp)


def test_readiness_passes_through_during_startup(monkeypatch):
    """GET /api/health/ready must reach the router during startup, unheld, so
    it can return detailed startup state (503 with status=starting) rather
    than the generic startup-gate 503 - and within the probe's timeout."""
    monkeypatch.setattr(main_module, "STARTUP_HOLD_SECONDS", 30.0)

    async def readiness(request: Request) -> JSONResponse:
        # Simulate the real handler's "starting" answer.
        return JSONResponse(
            {"status": "starting", "startup_error": None}, status_code=503
        )

    with _gated_client([Route("/api/health/ready", readiness)], state="starting") as client:
        began = time.monotonic()
        resp = client.get("/api/health/ready")

    # The startup gate MUST let /api/health/ready through so the router
    # handler returns the informative "starting" body.
    assert resp.status_code == 503
    assert resp.json().get("status") == "starting"
    assert time.monotonic() - began < 5


async def _ws_echo(websocket: WebSocket) -> None:
    await websocket.accept()
    await websocket.send_json({"status": "ok"})
    await websocket.close()


@pytest.mark.parametrize("state", ["failed", "starting"], ids=["failed", "hold-ran-out"])
def test_websocket_rejected_when_startup_did_not_admit_it(monkeypatch, state):
    """WebSocket handshakes must be rejected when startup failed or did not
    finish within the hold — letting a runner WebSocket through would hit
    routers (auth, runners) against a possibly mid-migration database.
    Rejecting the handshake matches the pre-gate behaviour where uvicorn was
    not accepting connections at all yet."""
    monkeypatch.setattr(main_module, "STARTUP_HOLD_SECONDS", 0.05)
    with (
        _gated_client([WebSocketRoute("/api/v1/runners/connect", _ws_echo)], state=state) as client,
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/api/v1/runners/connect"),
    ):
        pass


def test_websocket_passes_through_after_startup():
    """Once startup completes, WebSocket handshakes reach the router."""
    with (
        _gated_client([WebSocketRoute("/api/v1/runners/connect", _ws_echo)], state="done") as client,
        client.websocket_connect("/api/v1/runners/connect") as ws,
    ):
        assert ws.receive_json() == {"status": "ok"}


async def test_websocket_held_during_startup_is_accepted_once_startup_completes(monkeypatch):
    """A handshake reaching a worker mid-startup is held, then accepted."""
    monkeypatch.setattr(main_module, "STARTUP_HOLD_SECONDS", 30.0)
    sent: list[Message] = []
    inbox: asyncio.Queue[Message] = asyncio.Queue()
    await inbox.put({"type": "websocket.connect"})

    async def send(message: Message) -> None:
        sent.append(message)
        if message["type"] == "websocket.close":
            await inbox.put({"type": "websocket.disconnect", "code": 1000})

    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "path": "/api/v1/runners/connect",
        "raw_path": b"/api/v1/runners/connect",
        "root_path": "",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "subprotocols": [],
    }
    with _startup_state("starting") as (done, settled):
        app = _gated_app([WebSocketRoute("/api/v1/runners/connect", _ws_echo)])
        handshake = asyncio.create_task(app(scope, inbox.get, send))
        await asyncio.sleep(0.05)
        assert not handshake.done() and not sent, "the handshake must be held"

        done.set()
        settled.set()
        await asyncio.wait_for(handshake, 5)

    assert sent[0]["type"] == "websocket.accept"


async def test_run_startup_tasks_settles_the_gate_when_startup_fails(monkeypatch):
    async def _boom() -> None:
        raise RuntimeError("migration failed")

    monkeypatch.setattr(main_module, "_do_all_startup_work", _boom)
    monkeypatch.setattr(main_module, "_startup_error", None)
    with _startup_state("starting") as (done, settled):
        await main_module._run_startup_tasks()

        assert settled.is_set()
        assert not done.is_set()
    assert "migration failed" in (main_module._startup_error or "")


async def test_run_startup_tasks_settles_the_gate_when_cancelled(monkeypatch):
    """Shutdown cancels a startup still running: the requests it holds are
    answered then, not at the end of their hold."""
    started = asyncio.Event()

    async def _slow() -> None:
        started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(main_module, "_do_all_startup_work", _slow)
    with _startup_state("starting") as (done, settled):
        task = asyncio.create_task(main_module._run_startup_tasks())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert settled.is_set()
        assert not done.is_set()
