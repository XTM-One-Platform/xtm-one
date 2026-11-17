"""Images a public widget answer shows load through the widget session.

The platform writes a stored file or a generated image into an answer as
``/api/v1/chat/files/<id>`` or ``/api/v1/chat/images/<id>``: routes behind a
platform sign-in a widget visitor never has. The widget asks its session's own
routes for them instead (``widget.js`` render-core, tested in
``frontend/tests/embedded-markdown-xss.test.ts``). These tests pin the image
route the database fallback needs and the wiring between the widget and the two
session routes; who may read which image is pinned on PostgreSQL in
``test_public_chat_session_images_postgres.py``.
"""

from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1 import public_chat
from app.api.v1.public_chat import WIDGET_FILE_NOT_FOUND
from tests.test_public_chat_mcp_files import _RowSession


@pytest.fixture
def rate_limits(monkeypatch):
    buckets: list[str] = []

    async def _record(_request, bucket, **_kwargs):
        buckets.append(bucket)

    monkeypatch.setattr(public_chat, "enforce_rate_limit", _record)
    return buckets


def _serve(monkeypatch, row) -> None:
    async def _agent(*_args, **_kwargs):
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(public_chat, "_resolve_agent", _agent)
    monkeypatch.setattr(public_chat, "async_session", lambda: _RowSession(row))


@pytest.mark.parametrize(
    ("session_id", "image_id"),
    [("not-a-uuid", str(uuid.uuid4())), (str(uuid.uuid4()), "not-a-uuid"), ("", "")],
    ids=["malformed-session", "malformed-image", "empty"],
)
async def test_a_malformed_id_gets_the_unknown_file_answer(rate_limits, session_id, image_id):
    with pytest.raises(HTTPException) as refused:
        await public_chat.download_session_image("tok", session_id, image_id, request=SimpleNamespace())
    assert (refused.value.status_code, refused.value.detail) == (404, WIDGET_FILE_NOT_FOUND)


async def test_an_image_the_session_does_not_have_gets_the_unknown_file_answer(monkeypatch, rate_limits):
    _serve(monkeypatch, None)
    with pytest.raises(HTTPException) as refused:
        await public_chat.download_session_image(
            "tok", str(uuid.uuid4()), str(uuid.uuid4()), request=SimpleNamespace()
        )
    assert (refused.value.status_code, refused.value.detail) == (404, WIDGET_FILE_NOT_FOUND)


async def test_the_session_image_answers_the_file_contract(monkeypatch, rate_limits):
    """What the visitor's browser receives: an image painted inline, never
    sniffed, never kept by any cache (the capability rides in the URL), and
    readable by the embedding site; counted in the widget's download budget."""
    image = SimpleNamespace(id=uuid.uuid4(), image_data=b"\x89PNG fox", content_type="image/png")
    _serve(monkeypatch, image)

    response = await public_chat.download_session_image(
        "tok", str(uuid.uuid4()), str(image.id), request=SimpleNamespace()
    )

    assert response.status_code == 200
    assert response.body == b"\x89PNG fox"
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-disposition"] == "inline"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "private, no-store"
    assert "etag" not in response.headers
    assert response.headers["access-control-allow-origin"] == "*"
    assert rate_limits == ["public-chat-file-ip"]


@pytest.mark.parametrize("content_type", ["image/svg+xml", "text/html", "application/octet-stream", "", None])
async def test_an_image_row_a_browser_would_not_paint_is_never_served(monkeypatch, rate_limits, content_type):
    """Only a raster image is served: an SVG or an HTML body is a document on
    the platform origin, never an image of an answer."""
    _serve(monkeypatch, SimpleNamespace(id=uuid.uuid4(), image_data=b"<svg/>", content_type=content_type))
    with pytest.raises(HTTPException) as refused:
        await public_chat.download_session_image(
            "tok", str(uuid.uuid4()), str(uuid.uuid4()), request=SimpleNamespace()
        )
    assert (refused.value.status_code, refused.value.detail) == (404, WIDGET_FILE_NOT_FOUND)


def test_the_session_image_route_answers_through_the_helper_without_storing():
    """Same contract as the session file route: the helper with
    ``store=False``, never a hand-written ``Cache-Control``."""
    source = inspect.getsource(public_chat.download_session_image)
    assert "file_response_headers(image, store=False)" in source
    assert "Cache-Control" not in source


def test_the_widget_asks_the_session_routes_for_the_platform_media():
    """Tripwire: the addresses render-core maps land on the two session routes."""
    paths = {
        route.endpoint: route.path for route in public_chat.router.routes if getattr(route, "endpoint", None)
    }
    assert paths[public_chat.download_session_file] == "/chat/{web_token}/sessions/{session_id}/files/{file_id}"
    assert paths[public_chat.download_session_image] == "/chat/{web_token}/sessions/{session_id}/images/{image_id}"

    source = public_chat.WIDGET_JS_PATH.read_text(encoding="utf-8")
    start = source.index("function sessionRoute")
    route = source[start : source.index("}", start)]
    assert 'API + "/chat/" + encodeURIComponent(TOKEN) + "/sessions/" + encodeURIComponent(sessionId)' in route
    core = source[source.index("// render-core:start") : source.index("// render-core:end")]
    assert 'session + "/files/"' in core
    assert 'session + "/images/"' in core
