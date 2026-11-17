"""A custom dashboard as a PDF or a PNG (``dashboard_export``), rendered by the deck export's browser.

The Chromium render itself runs on the demo instance and in the deploy
pipeline; what is pinned here is what can regress silently: the confined
token (bound to one dashboard, refused as a session), the static page's
address, the PNG of the export root, the PDF's pages - one picture per tab, a
tall tab in equal slices, never more than ``MAX_PAGES`` - and the PDF itself.
"""

from __future__ import annotations

import asyncio
import io
import uuid
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from PIL import Image
from pypdf import PdfReader

from app.core.config import settings
from app.core.security import is_confined_token
from app.services import dashboard_export
from app.services.dashboard_export import (
    DASHBOARD_RENDER_TOKEN_SCOPE,
    DashboardRenderError,
    decode_dashboard_render_token,
    mint_dashboard_render_token,
    pdf_of_pictures,
    static_dashboard_url,
)
from app.services.presentation_export import PDF_CONTENT_TYPE, RENDER_PAGE_SIGNALS, RenderTokenError, mint_render_token


def _png(width: int, height: int) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (10, 20, 30)).save(out, format="PNG")
    return out.getvalue()


def test_the_render_token_is_confined_to_one_dashboard() -> None:
    dashboard_id, user_id = uuid.uuid4(), uuid.uuid4()
    token = mint_dashboard_render_token(dashboard_id, user_id)
    payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
    assert payload["scope"] == DASHBOARD_RENDER_TOKEN_SCOPE
    assert is_confined_token(payload)
    claims = decode_dashboard_render_token(token)
    assert (claims.dashboard_id, claims.user_id) == (dashboard_id, user_id)
    # A deck's render token is not a dashboard's.
    with pytest.raises(RenderTokenError):
        decode_dashboard_render_token(mint_render_token(dashboard_id, user_id))
    assert "dashboard" in RENDER_PAGE_SIGNALS


def test_the_static_page_address_names_the_tabs_and_the_visualization(monkeypatch) -> None:
    monkeypatch.setattr(dashboard_export, "render_base_url", lambda: "http://127.0.0.1:4000/xtm")
    dashboard_id = uuid.uuid4()
    url = static_dashboard_url(dashboard_id, "tok en", layout_ids=["main", "signals"])
    parts = urlsplit(url)
    assert parts.path == f"/xtm/dashboards/{dashboard_id}/static"
    assert parse_qs(parts.query) == {"render_token": ["tok en"], "tab": ["main", "signals"]}
    alone = parse_qs(urlsplit(static_dashboard_url(dashboard_id, "t", visualization_id="by_sev")).query)
    assert alone == {"render_token": ["t"], "viz": ["by_sev"]}


class _Locator:
    def __init__(self, seen: dict) -> None:
        self.seen = seen

    async def screenshot(self, **kwargs) -> bytes:
        self.seen["root"] = kwargs
        return b"PNG"


class _Page:
    def __init__(self, seen: dict, boxes: list) -> None:
        self.seen, self.boxes = seen, boxes
        seen["clips"] = []

    def locator(self, selector: str) -> _Locator:
        self.seen["selector"] = selector
        return _Locator(self.seen)

    async def evaluate(self, script: str):
        return self.boxes

    async def screenshot(self, **kwargs) -> bytes:
        clip = kwargs["clip"]
        self.seen["clips"].append((clip["y"], clip["height"]))
        assert kwargs["full_page"] is True
        return _png(int(clip["width"]) // 10, max(1, int(clip["height"]) // 10))


def _open(seen: dict, boxes: list):
    @asynccontextmanager
    async def opened(url, **kwargs):
        seen["url"], seen["open"] = url, kwargs
        yield _Page(seen, boxes)

    return opened


def test_a_png_is_the_export_root_at_twice_the_pixels(monkeypatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(dashboard_export, "open_render_page", _open(seen, []))
    dashboard_id, user_id = uuid.uuid4(), uuid.uuid4()
    exported = asyncio.run(
        dashboard_export.export_dashboard(
            dashboard_id=dashboard_id, user_id=user_id, title="Ops: week", export_format="png", visualization_id="by_sev"
        )
    )
    assert (exported.filename, exported.content_type, exported.data) == ("ops-week.png", "image/png", b"PNG")
    assert seen["selector"] == "[data-export-root]"
    assert (seen["open"]["signal"], seen["open"]["error"], seen["open"]["device_scale_factor"]) == ("dashboard", DashboardRenderError, 2)
    claims = decode_dashboard_render_token(parse_qs(urlsplit(seen["url"]).query)["render_token"][0])
    assert (claims.dashboard_id, claims.user_id) == (dashboard_id, user_id)


def test_a_pdf_has_a_page_per_tab_and_a_tall_tab_in_equal_slices(monkeypatch) -> None:
    seen: dict = {}
    # Two tabs: 820 pixels, and 5000 - three slices of 1666.67 at most 2400 each.
    boxes = [[0, 0, 1440, 820], [0, 820, 1440, 5000]]
    monkeypatch.setattr(dashboard_export, "open_render_page", _open(seen, boxes))
    exported = asyncio.run(
        dashboard_export.export_dashboard(dashboard_id=uuid.uuid4(), user_id=uuid.uuid4(), title="", export_format="pdf")
    )
    assert (exported.filename, exported.content_type) == ("dashboard.pdf", PDF_CONTENT_TYPE)
    third = 5000 / 3
    assert seen["clips"] == [(0, 820), (820, third), (820 + third, third), (820 + 2 * third, third)]
    assert len(PdfReader(io.BytesIO(exported.data)).pages) == 4


def test_a_pdf_never_holds_more_than_its_page_bound(monkeypatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(dashboard_export, "open_render_page", _open(seen, [[0, 0, 1440, 2400 * 60]]))
    exported = asyncio.run(
        dashboard_export.export_dashboard(dashboard_id=uuid.uuid4(), user_id=uuid.uuid4(), title="Tall", export_format="pdf")
    )
    assert len(seen["clips"]) == dashboard_export.MAX_PAGES
    assert len(PdfReader(io.BytesIO(exported.data)).pages) == dashboard_export.MAX_PAGES


def test_a_dashboard_with_nothing_to_draw_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(dashboard_export, "open_render_page", _open({}, []))
    with pytest.raises(DashboardRenderError):
        asyncio.run(
            dashboard_export.export_dashboard(dashboard_id=uuid.uuid4(), user_id=uuid.uuid4(), title="x", export_format="pdf")
        )


def test_each_picture_is_a_page_of_its_own_size() -> None:
    pdf = PdfReader(io.BytesIO(pdf_of_pictures([_png(288, 192), _png(288, 96)])))
    # At twice the pixels, 192 pixels an inch: 288 x 192 pixels is 1.5 x 1 inch, 108 x 72 points.
    sizes = [(round(float(page.mediabox.width)), round(float(page.mediabox.height))) for page in pdf.pages]
    assert sizes == [(108, 72), (108, 36)]
