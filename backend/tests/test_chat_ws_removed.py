"""No chat WebSocket: ``/api/v1/chat/conversations/{id}/ws`` answers 404 and accepts no WebSocket."""

from __future__ import annotations

import uuid

import httpx

WS_PATH = f"/api/v1/chat/conversations/{uuid.uuid4()}/ws"


async def test_the_chat_websocket_route_answers_404():
    from app.main import application

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(WS_PATH, params={"token": "anything"})

    assert response.status_code == 404


async def test_no_websocket_is_accepted_on_the_chat_route():
    from app.main import application

    sent: list[dict] = []
    received = iter([{"type": "websocket.connect"}])

    async def receive():
        return next(received, {"type": "websocket.disconnect", "code": 1000})

    async def send(message):
        sent.append(message)

    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "path": WS_PATH,
        "raw_path": WS_PATH.encode(),
        "root_path": "",
        "query_string": b"token=anything",
        "headers": [(b"host", b"test")],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
        "subprotocols": [],
        "state": {},
    }
    await application(scope, receive, send)

    assert sent, "the route said nothing"
    assert all(message["type"] != "websocket.accept" for message in sent)


def test_no_chat_websocket_route_is_registered():
    from starlette.routing import WebSocketRoute

    from app.main import application

    chat_sockets = [
        route.path
        for route in application.routes
        if isinstance(route, WebSocketRoute) and route.path.startswith("/api/v1/chat")
    ]
    assert chat_sockets == []
