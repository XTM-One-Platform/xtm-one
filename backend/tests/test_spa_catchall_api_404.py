"""Regression tests for the SPA catch-all route.

Unknown GET paths under the machine-facing namespaces (``/api``, ``/v1``,
``/mcp``, ``/xtm``, ``/.well-known``) must return a JSON 404 instead of
HTTP 200 + the branded index.html. Serving HTML there is what broke the
OpenCTI / OpenAEV chatbot proxies: a version-skewed client calling an
endpoint that does not exist on an older deployment received 200 + HTML
and choked on parsing instead of degrading cleanly on a 404.

The same holds for a file missing under ``/assets/``: it is a chunk
of a build a deploy replaced, asked for by a tab opened before it, and the
SPA page served in its place broke the arcade with a MIME-type error. The SPA
page itself is revalidated on every load, so a reload gets the current build.

The production registration in ``app.main`` only happens when the built
``frontend_dist`` directory exists at import time (never the case in the
test environment), so these tests install the same catch-all via
``_install_spa_catchall`` on a throwaway FastAPI app with a temporary
frontend directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import app.main as main_module


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    frontend_dir = tmp_path / "frontend_dist"
    frontend_dir.mkdir()
    (frontend_dir / "index.html").write_text("<html><body>spa</body></html>")
    assets = frontend_dir / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("console.log('app');")
    # Vite's shape, the hash holding the two base64url symbols it can carry.
    (assets / "index-B-x_12ab.js").write_text("console.log('hashed');")
    (assets / "fonts").mkdir()
    (assets / "fonts" / "plex-Ab3dE_9z.woff2").write_text("font")
    # Copied from ``public/`` as is: a name can look hashed without being one.
    (frontend_dir / "brand-standard.svg").write_text("<svg/>")
    (frontend_dir / "sw.js").write_text("self.addEventListener('fetch', () => {});")
    # A file OUTSIDE the frontend dir that a traversal attempt would target.
    (tmp_path / "secret.txt").write_text("top-secret")

    app = FastAPI()

    @app.get("/api/v1/known")
    async def known():
        return {"ok": True}

    main_module._install_spa_catchall(app, frontend_dir)
    return TestClient(app)


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/does-not-exist",
        "/api/v1/platform/chat/sessions-typo",
        "/v1/does-not-exist",
        "/mcp/does-not-exist",
        "/xtm/auth/does-not-exist",
        "/.well-known/does-not-exist",
        "/api",
        "/v1",
        "/mcp",
        "/xtm",
    ],
)
def test_unknown_api_namespace_paths_return_json_404(client: TestClient, path: str):
    resp = client.get(path)
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json() == {"detail": "Not Found"}


def test_registered_api_routes_take_priority_over_catchall(client: TestClient):
    resp = client.get("/api/v1/known")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


@pytest.mark.parametrize("path", ["/", "/index.html", "/agents", "/settings/llm"])
def test_frontend_routes_still_serve_index_html(client: TestClient, path: str):
    resp = client.get(path)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")


def test_namespace_match_is_exact_first_segment(client: TestClient):
    """A frontend route that merely STARTS with a namespace token (e.g.
    ``/api-docs``) must not be caught by the 404 short-circuit."""
    resp = client.get("/api-docs")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")


def test_site_webmanifest_redirects_to_platform_manifest(client: TestClient):
    resp = client.get("/site.webmanifest", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/api/v1/platform/manifest.webmanifest"


def test_static_asset_is_served_from_frontend_dir(client: TestClient):
    resp = client.get("/assets/app.js")
    assert resp.status_code == 200
    assert resp.text == "console.log('app');"


@pytest.mark.parametrize("path", ["/assets/ArcadeOverlay-AQxG0y25.js", "/assets/fonts/gone.woff2"])
def test_missing_build_asset_is_a_404_not_the_spa_page(client: TestClient, path: str):
    resp = client.get(path)
    assert resp.status_code == 404
    assert not resp.headers["content-type"].startswith("text/html")
    # Mid rolling deploy the file may exist on another replica: never cached.
    assert resp.headers["cache-control"] == "no-store"


def test_assets_match_is_exact_first_segment(client: TestClient):
    resp = client.get("/assets-library")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")


@pytest.mark.parametrize("path", ["/", "/index.html", "/agents", "/settings/llm"])
def test_spa_page_is_revalidated_on_every_load(client: TestClient, path: str):
    resp = client.get(path)
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-cache"


def test_a_served_build_asset_is_not_revalidated(client: TestClient):
    """Only the SPA page is: a hashed file never changes under its name."""
    resp = client.get("/assets/app.js")
    assert resp.status_code == 200
    assert "no-cache" not in resp.headers.get("cache-control", "")


def test_a_hashed_build_file_is_cached_for_a_year(client: TestClient):
    """New bytes get a new name, so the browser never has to ask again."""
    resp = client.get("/assets/index-B-x_12ab.js")
    assert resp.status_code == 200
    assert resp.text == "console.log('hashed');"
    assert resp.headers["cache-control"] == "public, max-age=31536000, immutable"


@pytest.mark.parametrize(
    "path",
    [
        "/assets/app.js",  # no hash in the name
        "/assets/fonts/plex-Ab3dE_9z.woff2",  # not something Vite writes: it keeps assets/ flat
        "/brand-standard.svg",  # a public file outside assets/, hashed-looking or not
    ],
)
def test_nothing_else_is_marked_immutable(client: TestClient, path: str):
    resp = client.get(path)
    assert resp.status_code == 200
    assert "immutable" not in resp.headers.get("cache-control", "")
    assert "max-age" not in resp.headers.get("cache-control", "")


def test_the_service_worker_is_revalidated_on_every_check(client: TestClient):
    """A cached worker would keep a deployment's old one running on the phones
    that installed the app."""
    resp = client.get("/sw.js")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-cache"
    assert resp.headers["content-type"].startswith("text/javascript")


@pytest.mark.parametrize("path", ["/", "/index.html", "/agents"])
def test_the_spa_page_is_never_immutable(client: TestClient, path: str):
    assert client.get(path).headers["cache-control"] == "no-cache"


def test_every_file_vite_writes_under_assets_is_named_by_its_content():
    """What the year-long lifetime rests on: ``assets/`` only ever holds what
    Vite writes there, under its default ``[name]-[hash]`` names. A file placed
    in ``frontend/public/assets/`` would be copied as is, and renamed outputs
    could drop the hash: either must revisit ``_HASHED_BUILD_FILE_RE`` first."""
    frontend = Path(__file__).resolve().parents[2] / "frontend"
    assert not (frontend / "public" / "assets").exists()
    config = (frontend / "vite.config.ts").read_text(encoding="utf-8")
    for option in ("assetsDir", "assetFileNames", "chunkFileNames", "entryFileNames", "hashCharacters"):
        assert option not in config, f"vite.config.ts sets {option}"
    for name in ("index-B-x_12ab.js", "abnfDiagram-VCTEODGH-2CIk8r-t.js", "plex-sans-latin-400-normal-HDzGo9_7.woff2"):
        assert main_module._HASHED_BUILD_FILE_RE.fullmatch(name), name
    for name in ("app.js", "index.js", "logo-dark.svg", "fonts/plex-Ab3dE_9z.woff2"):
        assert not main_module._HASHED_BUILD_FILE_RE.fullmatch(name), name


def test_path_traversal_falls_back_to_index_html(client: TestClient):
    """A ``..`` traversal attempt must never leak files outside the frontend
    dir; it falls through to the SPA fallback instead."""
    resp = client.get("/assets/../../secret.txt")
    assert resp.status_code == 200
    assert "top-secret" not in resp.text
    assert resp.headers["content-type"].startswith("text/html")
