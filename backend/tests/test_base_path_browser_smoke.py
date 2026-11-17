"""Browser smoke: the BUILT SPA served under a URL path prefix (``BASE_PATH``).

The unit tests in ``test_base_path.py`` prove the middleware, the renderer and
the helpers one at a time; this one proves the thing an operator sees - the
bundle Vite emitted with a relative ``base``, rewritten absolute by the
renderer, booting in a real browser under ``/xtm-one`` with every request
(chunks, fonts, config calls, the manifest) staying under the prefix, and a
hard-loaded deep link landing on the prefixed login page.

Opt-in by construction: it needs ``frontend/dist`` (``yarn build``) and the
Playwright Chromium the presentation export uses, neither of which the backend
CI job has, so it SKIPS when either is missing. Run it locally after a change
to the renderer, the Vite ``base`` or ``lib/base-path.ts``::

    cd frontend && yarn build && cd ../backend && pytest tests/test_base_path_browser_smoke.py -s
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

import app.main as main_module
from app.core.base_path import BasePathMiddleware
from app.core.config import settings
from app.services.branding import Branding, build_web_manifest, configure_branded_index_html

DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"

pytestmark = pytest.mark.skipif(
    not (DIST / "index.html").is_file(),
    reason="needs the built frontend (cd frontend && yarn build)",
)


def _platform_config(base_url: str) -> dict:
    """The unauthenticated config the login page reads, with every field the
    frontend's ``PlatformConfig`` declares so nothing renders undefined."""
    return {
        "instance_id": "00000000-0000-0000-0000-000000000000",
        "platform_mode": "xtm_one",
        "platform_name": "XTM One",
        "platform_logo": None,
        "platform_logo_hash": None,
        "platform_description": "smoke",
        "platform_login_message": "Sign in",
        "platform_language": "auto",
        "platform_theme_color": "#0a0f1c",
        "platform_accent_color": None,
        "platform_background_color": None,
        "platform_default_accent_color": "#0fbcff",
        "platform_default_background_color": "#0a0f1c",
        "platform_favicon": None,
        "platform_favicon_hash": None,
        "platform_og_image": "/og-image.png",
        "platform_og_image_hash": None,
        "platform_version": "0.0.0-smoke",
        "license_required": False,
        "license_valid": True,
        "license_customer": None,
        "license_type": None,
        "license_expiration": None,
        "features": {
            "ai_adoption": False,
            "all_integrations": True,
            "all_templates": True,
            "has_ee_platform": False,
            "active_registration_count": 0,
        },
        "integration_status": {
            "opencti": {"status": "not_registered", "connected": False, "last_checked_at": None},
            "openaev": {"status": "not_registered", "connected": False, "last_checked_at": None},
            "linked": False,
            "last_checked_at": None,
        },
        "xtm_license": None,
        "agentic_enabled": True,
        "agentic_quotas": {"daily": 30, "monthly": 100, "yearly": 300},
        "agentic_source": "none",
        "edition": "enterprise",
        "chat_enabled": True,
        "disable_code_interpreter": False,
        "disable_custom_tools": False,
        "disable_custom_variables": False,
        "ai_catalog_enabled": False,
        "ai_enabled": True,
        "base_url": base_url,
    }


def _build_app(base_path: str, base_url: str) -> FastAPI:
    app = FastAPI()

    @app.get("/api/v1/platform/config")
    async def platform_config():
        return _platform_config(base_url)

    @app.get("/api/v1/auth/config")
    async def auth_config():
        return {
            "base_url": base_url,
            "local_enabled": True,
            "registration_enabled": False,
            "allowed_domains": [],
            "oidc_enabled": False,
            "oidc_display_name": None,
            "saml_enabled": False,
            "saml_display_name": None,
            "enabled_features": [],
            "runners_enabled": False,
        }

    @app.get("/api/v1/platform/manifest.webmanifest")
    async def manifest():
        return JSONResponse(
            build_web_manifest(Branding(name="XTM One"), is_xtm=True, base_path=base_path),
            media_type="application/manifest+json",
        )

    configure_branded_index_html(DIST)
    main_module._install_spa_catchall(app, DIST)
    app.add_middleware(main_module.PublicCORSMiddleware)
    app.add_middleware(BasePathMiddleware, base_path=base_path)
    return app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_port(port: int, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError("the smoke server did not start")


@pytest.mark.parametrize("base_path", ["", "/xtm-one", "/apps/xtm-one"])
def test_the_built_spa_boots_under_the_prefix_with_every_request_under_it(base_path, monkeypatch):
    import uvicorn

    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover - the export dependency is always installed
        pytest.skip("playwright is not installed")

    port = _free_port()
    origin = f"http://127.0.0.1:{port}"
    base_url = origin + base_path
    # The renderer and the manifest read the configured prefix; the SPA reads
    # the meta tag the renderer writes from it.
    monkeypatch.setattr(settings, "base_path", base_path)
    monkeypatch.setattr(settings, "base_url", base_url)
    monkeypatch.setattr(settings, "frontend_url", base_url)

    server = uvicorn.Server(uvicorn.Config(_build_app(base_path, base_url), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    try:
        _wait_port(port)
        problems: list[str] = []
        requests: list[str] = []
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except PlaywrightError as exc:
                pytest.skip(f"no Chromium for Playwright here: {exc}")
            page = browser.new_page()
            page.on("requestfailed", lambda r: problems.append(f"request failed: {r.url} ({r.failure})"))
            page.on("response", lambda r: problems.append(f"HTTP {r.status}: {r.url}") if r.status >= 400 else None)
            page.on("console", lambda m: problems.append(f"console {m.type}: {m.text}") if m.type == "error" else None)
            page.on("pageerror", lambda e: problems.append(f"page error: {e}"))
            page.on("request", lambda r: requests.append(r.url))

            page.goto(f"{base_url}/login", wait_until="networkidle")
            page.wait_for_selector("#root > *", timeout=15000)
            page.wait_for_selector("form", timeout=15000)
            meta = page.evaluate("document.querySelector('meta[name=\"base-path\"]')?.getAttribute('content')")
            assert meta == base_path
            assert page.evaluate("window.location.pathname") == f"{base_path}/login"

            # A deep link, hard-loaded: the router reads the route under the
            # prefix and sends an anonymous visitor to the login page under it.
            page.goto(f"{base_url}/agents/00000000-0000-0000-0000-000000000001", wait_until="networkidle")
            page.wait_for_selector("#root > *", timeout=15000)
            page.wait_for_url(f"{base_url}/login**", timeout=15000)
            browser.close()

        escaped = [u for u in requests if u.startswith(origin) and not u.startswith(f"{base_url}/")]
        problems.extend(f"request outside the prefix: {u}" for u in escaped)
        assert len(requests) > 50, "the bundle did not load"
        assert not problems, "\n".join(problems)
    finally:
        server.should_exit = True
