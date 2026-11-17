"""Serving the platform under a URL path prefix (``BASE_PATH``).

Two reverse-proxy configurations exist: one forwards the prefix
(``GET /xtm-one/api/v1/agents`` reaches the app), one strips it
(``GET /api/v1/agents`` does). ``BasePathMiddleware`` normalises both to the
ASGI mounted-app shape, so routing, ``request.url``, the static mount and the
SPA catch-all behave identically. These tests pin that contract, the config
normalisation, and the two documents the browser reads the prefix from: the
branded ``index.html`` and the web manifest.

Like ``test_spa_catchall_api_404.py``, the catch-all is installed on a
throwaway FastAPI app - the production registration needs a built
``frontend_dist`` at import time.
"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path

import pytest
from fastapi import FastAPI, Request, WebSocket
from starlette.responses import PlainTextResponse, StreamingResponse
from starlette.staticfiles import StaticFiles
from starlette.testclient import TestClient

import app.main as main_module
from app.core.base_path import (
    BASE_PATH_META_NAME,
    BASE_PATH_MODES,
    RESERVED_FIRST_SEGMENTS,
    SPA_FIRST_SEGMENTS,
    BasePathMiddleware,
    forwarded_prefix,
    normalize_base_path,
    normalize_base_path_mode,
    reserved_segment_problem,
    route_path,
    spa_route_problem,
    with_base_path,
)
from app.services.branding import (
    Branding,
    build_web_manifest,
    rebase_document_urls,
    render_branded_index_html,
)

# ── normalisation ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, ""),
        ("", ""),
        ("   ", ""),
        ("/", ""),
        ("//", ""),
        ("xtm-one", "/xtm-one"),
        ("/xtm-one", "/xtm-one"),
        ("/xtm-one/", "/xtm-one"),
        ("  /xtm-one/  ", "/xtm-one"),
        ("//tools//xtm-one/", "/tools/xtm-one"),
        # Every RFC 3986 unreserved character is welcome.
        ("/v2.0/xtm_one/~team-1", "/v2.0/xtm_one/~team-1"),
    ],
)
def test_normalize_base_path_canonical_form(raw, expected):
    assert normalize_base_path(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "/xtm one",
        "/xtm-one?x=1",
        "/xtm-one#frag",
        "https://host/xtm-one",
        "/../etc",
        "/a/./b",
        # Outside the unreserved set: the middleware rebuilds ``raw_path`` as
        # ASCII, the browser reports its pathname percent-encoded, and the
        # value lands in an HTML attribute, a <meta> and a <script> literal.
        "/xtm%20one",
        "/\u00e9quipe",
        '/xtm"one',
        "/xtm\\one",
        "/xtm<one>",
        "/xtm'one",
        "/xtm&one",
    ],
)
def test_normalize_base_path_refuses_what_is_not_a_prefix(raw):
    with pytest.raises(ValueError):
        normalize_base_path(raw)


@pytest.mark.parametrize("prefix", ["/api", "/v1", "/mcp", "/xtm", "/a2a", "/.well-known", "/office", "/assets"])
def test_reserved_namespaces_are_refused(prefix):
    assert reserved_segment_problem(prefix) is not None


@pytest.mark.parametrize("prefix", ["", "/xtm-one", "/apps/xtm-one", "/xtm-one-api"])
def test_other_prefixes_are_accepted(prefix):
    assert reserved_segment_problem(prefix) is None


# ── the proxy shape: BASE_PATH_MODE and the SPA-route collision ──────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, "auto"), ("", "auto"), ("  Auto ", "auto"), ("forwarded", "forwarded"), ("STRIPPED", "stripped")],
)
def test_normalize_base_path_mode(raw, expected):
    assert normalize_base_path_mode(raw) == expected
    assert expected in BASE_PATH_MODES


@pytest.mark.parametrize("raw", ["strip", "proxy", "yes", "auto forwarded"])
def test_normalize_base_path_mode_refuses_what_is_not_a_mode(raw):
    with pytest.raises(ValueError):
        normalize_base_path_mode(raw)


@pytest.mark.parametrize("prefix", ["/tools", "/agents", "/chat", "/settings", "/login", "/tools/xtm-one"])
def test_a_prefix_that_is_also_a_platform_page_is_ambiguous_without_a_declared_proxy(prefix):
    """Behind a stripping proxy the public ``/tools/tools/123`` arrives as
    ``/tools/123`` - the forwarded shape of the page ``/tools/123``. ``auto``
    refuses the prefix rather than route one of the two silently wrong; a
    declared proxy shape lifts the refusal, because the request rule is then
    exact (``forwarded``: never re-prefix a path under the prefix; ``stripped``:
    the header decides)."""
    problem = spa_route_problem(prefix, "auto")
    assert problem is not None
    assert "BASE_PATH_MODE=forwarded" in problem and "BASE_PATH_MODE=stripped" in problem
    assert spa_route_problem(prefix, "forwarded") is None
    assert spa_route_problem(prefix, "stripped") is None


@pytest.mark.parametrize("prefix", ["", "/xtm-one", "/apps/xtm-one", "/tooling"])
def test_a_prefix_that_is_not_a_platform_page_is_accepted_in_every_mode(prefix):
    for mode in BASE_PATH_MODES:
        assert spa_route_problem(prefix, mode) is None


def test_settings_refuse_a_page_prefix_in_auto_mode_and_accept_a_declared_proxy(monkeypatch):
    from app.core.config import Settings

    monkeypatch.setenv("BASE_PATH", "/tools")
    monkeypatch.delenv("BASE_PATH_MODE", raising=False)
    with pytest.raises(ValueError, match="BASE_PATH_MODE"):
        Settings()
    monkeypatch.setenv("BASE_PATH_MODE", "forwarded")
    assert Settings().base_path_mode == "forwarded"
    monkeypatch.setenv("BASE_PATH_MODE", "Stripped")
    assert Settings().base_path_mode == "stripped"
    monkeypatch.setenv("BASE_PATH_MODE", "sideways")
    with pytest.raises(ValueError, match="BASE_PATH_MODE"):
        Settings()
    # A prefix that is no page needs no declaration.
    monkeypatch.setenv("BASE_PATH", "/xtm-one")
    monkeypatch.delenv("BASE_PATH_MODE", raising=False)
    assert Settings().base_path_mode == "auto"


def test_spa_first_segments_cover_every_top_level_route_of_the_frontend_router():
    """``SPA_FIRST_SEGMENTS`` is hand-written; this derives the first segment of
    every absolute ``<Route path="/...">`` in ``frontend/src/App.tsx`` (relative
    child routes live under their parent's segment) and fails when a page is
    missing - a new top-level page must be added to the set, or a prefix named
    after it would be accepted in ``auto`` mode and misroute behind a stripping
    proxy."""
    import re

    source = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
    segments: set[str] = set()
    for path in re.findall(r'path="(/[^"]*)"', source):
        first = path.lstrip("/").split("/", 1)[0]
        if first and not first.startswith((":", "*")):
            segments.add(first)
    assert segments, "no absolute routes found in App.tsx"
    missing = sorted(segments - SPA_FIRST_SEGMENTS)
    assert not missing, f"platform pages not listed in SPA_FIRST_SEGMENTS: {missing}"
    # And the set is not carrying pages the router no longer has.
    stale = sorted(SPA_FIRST_SEGMENTS - segments)
    assert not stale, f"SPA_FIRST_SEGMENTS lists pages App.tsx does not route: {stale}"


def test_settings_validator_normalises_and_refuses(monkeypatch):
    from app.core.config import Settings

    monkeypatch.setenv("BASE_PATH", "xtm-one/")
    assert Settings().base_path == "/xtm-one"
    monkeypatch.setenv("BASE_PATH", "/")
    assert Settings().base_path == ""
    monkeypatch.setenv("BASE_PATH", "/api")
    with pytest.raises(ValueError):
        Settings()
    monkeypatch.setenv("BASE_PATH", "/\u00e9quipe")
    with pytest.raises(ValueError):
        Settings()


def test_reserved_first_segments_cover_every_namespace_the_platform_serves_at_the_root():
    """The refusal list is hand-written; this derives what the platform really
    serves at the root - every router prefix, mount and top-level route on the
    application, the catch-all's machine namespaces, and every file Vite copies
    from ``frontend/public`` next to its ``assets/`` directory - and fails when
    a namespace is missing. Under such a prefix a stripped request is
    indistinguishable from a direct request for the resource, so the list must
    grow with the routes."""
    served: set[str] = set(main_module._API_NAMESPACES)
    for route in main_module.application.routes:
        path = getattr(route, "path", "") or ""
        first = path.lstrip("/").split("/", 1)[0]
        if not first or first.startswith("{"):
            continue  # the SPA catch-all ``/{full_path:path}``
        served.add(first)
    public_dir = Path(__file__).resolve().parents[2] / "frontend" / "public"
    served |= {entry.name for entry in public_dir.iterdir()}
    # Vite's output directory and the document the catch-all serves by name.
    served |= {"assets", "index.html"}
    missing = sorted(served - RESERVED_FIRST_SEGMENTS)
    assert not missing, f"served at the root but not refused as a BASE_PATH: {missing}"


def test_lifespan_is_the_async_context_manager_factory_the_app_runs():
    """``@asynccontextmanager`` belongs on ``lifespan`` itself. Starlette still
    wraps a bare async generator function - with a deprecation warning, and one
    release away from refusing it - which is how a decorator displaced onto a
    neighbouring helper went unnoticed."""
    assert not inspect.isasyncgenfunction(main_module.lifespan)
    # Calling the factory runs nothing yet; it hands back the context manager
    # the server enters - which a bare async generator function never would.
    context = main_module.lifespan(main_module.application)
    assert hasattr(context, "__aenter__") and hasattr(context, "__aexit__")
    assert not inspect.isasyncgen(context)
    # The helper is a plain synchronous call, not a context manager in disguise.
    assert not inspect.isasyncgenfunction(main_module._log_base_path_configuration)
    assert not inspect.iscoroutinefunction(main_module._log_base_path_configuration)
    assert main_module._log_base_path_configuration() is None


def test_startup_log_names_the_prefix_and_flags_a_base_url_without_it(monkeypatch, caplog):
    from app.core.config import settings

    monkeypatch.setattr(settings, "base_path", "/xtm-one")
    monkeypatch.setattr(settings, "base_url", "https://intranet.example.com")
    monkeypatch.setattr(settings, "frontend_url", "https://intranet.example.com/xtm-one")
    with caplog.at_level(logging.INFO, logger=main_module.logger.name):
        main_module._log_base_path_configuration()
    assert "URL path prefix /xtm-one" in caplog.text
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "BASE_URL (https://intranet.example.com)" in warnings[0].getMessage()
    assert "Set BASE_URL" in warnings[0].getMessage()

    # FRONTEND_URL is where the post-SSO redirects go; it is checked too.
    caplog.clear()
    monkeypatch.setattr(settings, "base_url", "https://intranet.example.com/xtm-one/")
    monkeypatch.setattr(settings, "frontend_url", "https://intranet.example.com")
    with caplog.at_level(logging.INFO, logger=main_module.logger.name):
        main_module._log_base_path_configuration()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "FRONTEND_URL" in warnings[0].getMessage()

    caplog.clear()
    monkeypatch.setattr(settings, "frontend_url", "https://intranet.example.com/xtm-one/")
    with caplog.at_level(logging.INFO, logger=main_module.logger.name):
        main_module._log_base_path_configuration()
    assert "URL path prefix /xtm-one" in caplog.text
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    caplog.clear()
    monkeypatch.setattr(settings, "base_path", "")
    with caplog.at_level(logging.INFO, logger=main_module.logger.name):
        main_module._log_base_path_configuration()
    assert caplog.records == []


# ── with_base_path / route_path ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/api/v1/agents", "/xtm-one/api/v1/agents"),
        ("/", "/xtm-one/"),
        ("https://other.example/x", "https://other.example/x"),
        ("//cdn.example/x.js", "//cdn.example/x.js"),
        ("relative/path", "relative/path"),
        ("", ""),
    ],
)
def test_with_base_path_prefixes_only_root_relative_paths(path, expected):
    assert with_base_path(path, "/xtm-one") == expected
    assert with_base_path(path, "") == path


def test_with_base_path_is_deliberately_not_idempotent():
    """A guard skipping inputs already under the prefix would leave a legitimate
    ``/tools/...`` route unprefixed on a deployment served at ``/tools``; the
    contract is one call per root-relative path, at the sink."""
    assert with_base_path("/tools/123", "/tools") == "/tools/tools/123"


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        ({"path": "/api/health"}, "/api/health"),
        ({"path": "/api/health", "root_path": ""}, "/api/health"),
        ({"path": "/xtm-one/api/health", "root_path": "/xtm-one"}, "/api/health"),
        ({"path": "/xtm-one", "root_path": "/xtm-one"}, "/"),
        ({"path": "/xtm-one/", "root_path": "/xtm-one"}, "/"),
        # A path that merely shares the prefix's characters is not under it.
        ({"path": "/xtm-one-two/x", "root_path": "/xtm-one"}, "/xtm-one-two/x"),
    ],
)
def test_route_path_strips_exactly_the_root_path(scope, expected):
    assert route_path(scope) == expected


# ── the middleware, in both proxy configurations ──────────────────────────────


def _build_app(tmp_path: Path, base_path: str) -> TestClient:
    frontend_dir = tmp_path / "frontend_dist"
    frontend_dir.mkdir()
    (frontend_dir / "index.html").write_text("<html><body>spa</body></html>")
    (frontend_dir / "assets").mkdir()
    (frontend_dir / "assets" / "app.js").write_text("console.log('app');")
    (frontend_dir / "assets" / "index-B-x_12ab.js").write_text("console.log('hashed');")
    office_dir = tmp_path / "office"
    office_dir.mkdir()
    (office_dir / "taskpane.html").write_text("<html>office</html>")

    app = FastAPI()

    @app.get("/api/v1/whoami")
    async def whoami(request: Request):
        return {
            "path": request.scope["path"],
            "root_path": request.scope.get("root_path", ""),
            "url": str(request.url),
            "base_url": str(request.base_url),
        }

    @app.get("/api/v1/public/ping")
    async def public_ping():
        return {"pong": True}

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    @app.get("/metrics")
    async def metrics():
        return PlainTextResponse("up 1\n")

    @app.get("/api/v1/activity/stream")
    async def activity_stream():
        async def events():
            yield "data: one\n\n"
            yield "data: two\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.get("/xtm/auth/jwks")
    async def jwks():
        return {"keys": []}

    app.mount("/office", StaticFiles(directory=str(office_dir), html=True), name="office")
    main_module._install_spa_catchall(app, frontend_dir)
    app.add_middleware(main_module.PublicCORSMiddleware)
    app.add_middleware(BasePathMiddleware, base_path=base_path)
    return TestClient(app)


@pytest.fixture()
def prefixed(tmp_path: Path) -> TestClient:
    return _build_app(tmp_path, "/xtm-one")


@pytest.mark.parametrize("request_path", ["/api/v1/whoami", "/xtm-one/api/v1/whoami"])
def test_both_proxy_shapes_reach_the_same_route_and_read_the_public_url(prefixed, request_path):
    """Forwarded or stripped, the request lands on the route and describes itself
    as the public URL - so an absolute self-URL derived from ``request.url`` /
    ``request.base_url`` carries the prefix in both configurations."""
    resp = prefixed.get(request_path)
    assert resp.status_code == 200
    body = resp.json()
    assert body["path"] == "/xtm-one/api/v1/whoami"
    assert body["root_path"] == "/xtm-one"
    assert body["url"] == "http://testserver/xtm-one/api/v1/whoami"
    assert body["base_url"] == "http://testserver/xtm-one/"


@pytest.mark.parametrize("request_path", ["/api/health", "/metrics"])
def test_probes_addressed_directly_on_the_container_keep_working_under_a_prefix(prefixed, request_path):
    """A liveness / readiness probe or a Prometheus scrape reaches the container on
    its own port with no proxy in between - the stripped shape, by construction.
    Setting ``BASE_PATH`` must not move those targets."""
    resp = prefixed.get(request_path)
    assert resp.status_code == 200


@pytest.mark.parametrize("request_path", ["/api/v1/activity/stream", "/xtm-one/api/v1/activity/stream"])
def test_a_server_sent_event_stream_routes_in_both_shapes(prefixed, request_path):
    """The activity feed is an ``EventSource`` (a plain GET the browser keeps open);
    it must land on its route whether or not the wire path carried the prefix."""
    resp = prefixed.get(request_path)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.text == "data: one\n\ndata: two\n\n"


def test_a_forwarded_path_that_repeats_the_prefix_is_not_an_api_call(prefixed):
    """Forwarded shape, ``GET /xtm-one/xtm-one/api/v1/whoami``: the wire path is
    under the prefix, so nothing is re-added and the route path is
    ``/xtm-one/api/v1/whoami`` - an unknown SPA route served as ``index.html``,
    exactly as ``/anything/api/v1/whoami`` is at the root. It must never be read
    as the API route by peeling a second prefix."""
    resp = prefixed.get("/xtm-one/xtm-one/api/v1/whoami")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")


def _page_app(base_path: str) -> TestClient:
    """A prefix that is also a page: ``/tools/{item}`` is a route of the app."""
    app = FastAPI()

    @app.get("/tools/{item}")
    async def tool(item: str, request: Request):
        return {"item": item, "url": str(request.url)}

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    app.add_middleware(BasePathMiddleware, base_path=base_path)
    return TestClient(app)


def test_a_page_prefix_routes_in_both_shapes_once_the_stripping_proxy_sends_the_header():
    """``BASE_PATH=/tools`` (declared, so accepted). Forwarded shape: the public
    ``/tools/tools/123`` arrives intact and routes to the page. Stripped shape
    WITH ``X-Forwarded-Prefix``: the same public URL arrives as ``/tools/123``
    and the header says so, so it is re-prefixed and routes to the page too.
    Stripped shape WITHOUT the header is the ambiguity ``auto`` refuses: the
    path reads as the forwarded page ``/123``, which does not exist."""
    client = _page_app("/tools")
    forwarded = client.get("/tools/tools/123")
    assert forwarded.status_code == 200
    assert forwarded.json() == {"item": "123", "url": "http://testserver/tools/tools/123"}

    stripped = client.get("/tools/123", headers={"X-Forwarded-Prefix": "/tools"})
    assert stripped.status_code == 200
    assert stripped.json() == forwarded.json()

    assert client.get("/tools/123").status_code == 404
    # A header naming ANOTHER prefix is a foreign proxy's and changes nothing.
    assert client.get("/tools/123", headers={"X-Forwarded-Prefix": "/other"}).status_code == 404


@pytest.mark.parametrize("headers", [{}, {"X-Forwarded-Prefix": "/tools"}, {"X-Forwarded-Prefix": "/tools/"}])
def test_probes_and_stripped_requests_route_under_a_page_prefix_with_or_without_the_header(headers):
    client = _page_app("/tools")
    assert client.get("/api/health", headers=headers).status_code == 200


def test_the_header_marks_a_stripped_request_under_any_prefix(prefixed):
    """Under ``/xtm-one`` the header adds nothing the path did not already say -
    except for a request that HAPPENS to start with the prefix: with the
    header the public URL was ``/xtm-one/xtm-one/api/v1/whoami``, a page of the
    SPA, so it must not be read as the API call."""
    with_header = {"X-Forwarded-Prefix": "/xtm-one"}
    assert prefixed.get("/api/v1/whoami", headers=with_header).json()["url"] == "http://testserver/xtm-one/api/v1/whoami"
    resp = prefixed.get("/xtm-one/api/v1/whoami", headers=with_header)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    # The bare prefix, stripped to ``/`` by the proxy, is the SPA root.
    assert prefixed.get("/", headers=with_header).headers["content-type"].startswith("text/html")


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ([], None),
        ([(b"x-forwarded-prefix", b"/xtm-one")], "/xtm-one"),
        ([(b"X-Forwarded-Prefix", b"xtm-one/")], "/xtm-one"),
        ([(b"x-forwarded-prefix", b"")], ""),
        ([(b"x-forwarded-prefix", b"/xtm one")], None),
        ([(b"x-forwarded-prefix", b"https://host/xtm-one")], None),
    ],
)
def test_forwarded_prefix_reads_the_header_canonically(headers, expected):
    assert forwarded_prefix({"type": "http", "headers": headers}) == expected


async def _noop_app(scope, receive, send):
    """An inner app that answers nothing: the scope is inspected after the call."""


async def test_the_middleware_rebuilds_raw_path_alongside_path():
    """uvicorn hands out ``raw_path`` (bytes, still percent-encoded) next to
    ``path``; the stripped shape re-prefixes both so a reader of either sees one
    request, and the forwarded shape leaves both alone."""
    middleware = BasePathMiddleware(_noop_app, base_path="/xtm-one")

    stripped = {"type": "http", "path": "/api/v1/agents", "raw_path": b"/api/v1/agents"}
    await middleware(stripped, None, None)
    assert stripped["path"] == "/xtm-one/api/v1/agents"
    assert stripped["raw_path"] == b"/xtm-one/api/v1/agents"
    assert stripped["root_path"] == "/xtm-one"

    forwarded = {"type": "http", "path": "/xtm-one/api/v1/agents", "raw_path": b"/xtm-one/api/v1/agents"}
    await middleware(forwarded, None, None)
    assert forwarded["path"] == "/xtm-one/api/v1/agents"
    assert forwarded["raw_path"] == b"/xtm-one/api/v1/agents"
    assert forwarded["root_path"] == "/xtm-one"


async def test_a_root_path_the_server_already_set_is_not_prefixed_twice():
    """``uvicorn --root-path /xtm-one`` (or an ASGI server honouring a proxy's
    prefix header) already produces the mounted-app shape: ``path`` carries
    the prefix and ``root_path`` names it. The middleware recognises the shape
    and changes nothing."""
    middleware = BasePathMiddleware(_noop_app, base_path="/xtm-one")
    scope = {
        "type": "http",
        "path": "/xtm-one/api/v1/agents",
        "raw_path": b"/xtm-one/api/v1/agents",
        "root_path": "/xtm-one",
    }
    await middleware(scope, None, None)
    assert scope["path"] == "/xtm-one/api/v1/agents"
    assert scope["root_path"] == "/xtm-one"
    assert route_path(scope) == "/api/v1/agents"


async def test_the_middleware_leaves_a_lifespan_scope_alone():
    middleware = BasePathMiddleware(_noop_app, base_path="/xtm-one")
    scope = {"type": "lifespan"}
    await middleware(scope, None, None)
    assert "root_path" not in scope and "path" not in scope


@pytest.mark.parametrize("request_path", ["/office/taskpane.html", "/xtm-one/office/taskpane.html"])
def test_static_mount_resolves_in_both_shapes(prefixed, request_path):
    """Starlette's ``Mount`` derives the child ``root_path`` from the request path,
    which broke the stripped shape until the middleware re-added the prefix."""
    resp = prefixed.get(request_path)
    assert resp.status_code == 200
    assert resp.text == "<html>office</html>"


@pytest.mark.parametrize("request_path", ["/xtm/auth/jwks", "/xtm-one/xtm/auth/jwks"])
def test_a_route_sharing_the_prefix_characters_routes_in_both_shapes(prefixed, request_path):
    """``/xtm/auth/jwks`` under ``BASE_PATH=/xtm-one``: the stripped shape starts
    with the prefix's characters but not with the prefix, so it is re-prefixed
    and Starlette strips exactly once."""
    resp = prefixed.get(request_path)
    assert resp.status_code == 200
    assert resp.json() == {"keys": []}


@pytest.mark.parametrize(
    "request_path",
    ["/", "/xtm-one/", "/agents/123", "/xtm-one/agents/123", "/index.html"],
)
def test_spa_routes_serve_index_html_in_both_shapes(prefixed, request_path):
    resp = prefixed.get(request_path)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")


def test_bare_prefix_redirects_to_the_slash_form(prefixed):
    resp = prefixed.get("/xtm-one", follow_redirects=False)
    assert resp.status_code in (307, 308)
    assert resp.headers["location"] == "http://testserver/xtm-one/"


@pytest.mark.parametrize("request_path", ["/assets/app.js", "/xtm-one/assets/app.js"])
def test_bundle_assets_are_served_in_both_shapes(prefixed, request_path):
    resp = prefixed.get(request_path)
    assert resp.status_code == 200
    assert "console.log" in resp.text


@pytest.mark.parametrize(
    ("request_path", "headers"),
    [
        ("/assets/index-B-x_12ab.js", {}),
        ("/xtm-one/assets/index-B-x_12ab.js", {}),
        ("/assets/index-B-x_12ab.js", {"X-Forwarded-Prefix": "/xtm-one"}),
    ],
)
def test_a_hashed_bundle_asset_is_immutable_in_both_shapes(prefixed, request_path, headers):
    """Whether the proxy forwarded the prefix or stripped it, the hashed file
    is the same bytes under the same public URL: cached for a year."""
    resp = prefixed.get(request_path, headers=headers)
    assert resp.status_code == 200
    assert "hashed" in resp.text
    assert resp.headers["cache-control"] == "public, max-age=31536000, immutable"


@pytest.mark.parametrize("request_path", ["/assets/app.js", "/xtm-one/assets/app.js"])
def test_an_unhashed_bundle_file_is_not_immutable_in_both_shapes(prefixed, request_path):
    resp = prefixed.get(request_path)
    assert resp.status_code == 200
    assert "immutable" not in resp.headers.get("cache-control", "")


@pytest.mark.parametrize(
    ("request_path", "headers"),
    [
        ("/assets/ArcadeOverlay-gone.js", {}),
        ("/xtm-one/assets/ArcadeOverlay-gone.js", {}),
        ("/assets/ArcadeOverlay-gone.js", {"X-Forwarded-Prefix": "/xtm-one"}),
    ],
)
def test_a_missing_bundle_asset_is_a_404_in_both_shapes(prefixed, request_path, headers):
    """A chunk of a build a deploy replaced fails as a 404 whether or not
    the wire path carried the prefix - never the SPA page with a 200."""
    resp = prefixed.get(request_path, headers=headers)
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Not Found"}
    assert resp.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "request_path", ["/", "/xtm-one/", "/agents/123", "/xtm-one/agents/123", "/xtm-one/index.html"]
)
def test_the_spa_page_is_revalidated_in_both_shapes(prefixed, request_path):
    """The SPA page names the current build's chunks, so it is never
    served from a cache without asking."""
    resp = prefixed.get(request_path)
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["cache-control"] == "no-cache"


@pytest.mark.parametrize("request_path", ["/api/v1/nope", "/xtm-one/api/v1/nope"])
def test_unknown_api_paths_stay_json_404_in_both_shapes(prefixed, request_path):
    resp = prefixed.get(request_path)
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Not Found"}


@pytest.mark.parametrize("request_path", ["/api/v1/public/ping", "/xtm-one/api/v1/public/ping"])
def test_path_keyed_middleware_reads_the_route_path(prefixed, request_path):
    """``PublicCORSMiddleware`` keys on ``/api/v1/public``; it must recognise the
    public route whether or not the wire path carried the prefix."""
    resp = prefixed.get(request_path, headers={"Origin": "https://embed.example"})
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "*"


def test_site_webmanifest_redirect_carries_the_prefix(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "base_path", "/xtm-one")
    client = _build_app(tmp_path, "/xtm-one")
    resp = client.get("/xtm-one/site.webmanifest", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/xtm-one/api/v1/platform/manifest.webmanifest"


def test_blank_base_path_is_a_pass_through(tmp_path):
    client = _build_app(tmp_path, "")
    body = client.get("/api/v1/whoami").json()
    assert body == {
        "path": "/api/v1/whoami",
        "root_path": "",
        "url": "http://testserver/api/v1/whoami",
        "base_url": "http://testserver/",
    }
    assert client.get("/office/taskpane.html").status_code == 200
    assert client.get("/agents/1").headers["content-type"].startswith("text/html")


def test_websocket_scopes_are_normalised_too(tmp_path):
    app = FastAPI()

    @app.websocket("/api/v1/echo")
    async def echo(ws: WebSocket):
        await ws.accept()
        await ws.send_json({"path": ws.scope["path"], "root_path": ws.scope.get("root_path", "")})
        await ws.close()

    app.add_middleware(BasePathMiddleware, base_path="/xtm-one")
    client = TestClient(app)
    for request_path in ("/api/v1/echo", "/xtm-one/api/v1/echo"):
        with client.websocket_connect(request_path) as ws:
            assert ws.receive_json() == {"path": "/xtm-one/api/v1/echo", "root_path": "/xtm-one"}


def test_middleware_normalises_its_own_argument(tmp_path):
    app = FastAPI()

    @app.get("/ping")
    async def ping():
        return PlainTextResponse("pong")

    app.add_middleware(BasePathMiddleware, base_path="xtm-one/")
    client = TestClient(app)
    assert client.get("/xtm-one/ping").text == "pong"
    assert client.get("/ping").text == "pong"


# ── the two documents the browser learns the prefix from ──────────────────────

_BUILT_INDEX = (
    '<!doctype html><html><head><meta charset="UTF-8" />'
    "<title>XTM One</title>"
    '<meta property="og:image" content="/og-image.png" />'
    '<link rel="icon" href="./favicon.ico" sizes="any" />'
    '<link rel="manifest" href="./api/v1/platform/manifest.webmanifest" />'
    '<script type="module" crossorigin src="./assets/index-abc.js"></script>'
    '<link rel="modulepreload" crossorigin href="./assets/vendor-def.js" />'
    '<link rel="stylesheet" crossorigin href="./assets/index-ghi.css" />'
    '<script src="//cdn.example/lib.js"></script>'
    '</head><body><div id="root"></div></body></html>'
)


def test_rebase_document_urls_makes_relative_and_root_relative_absolute():
    out = rebase_document_urls(_BUILT_INDEX, "/xtm-one")
    assert 'src="/xtm-one/assets/index-abc.js"' in out
    assert 'href="/xtm-one/assets/vendor-def.js"' in out
    assert 'href="/xtm-one/assets/index-ghi.css"' in out
    assert 'href="/xtm-one/favicon.ico"' in out
    # Protocol-relative and ``content`` attributes are not the document's own.
    assert 'src="//cdn.example/lib.js"' in out
    assert 'content="/og-image.png"' in out


def test_rebase_document_urls_at_the_root_turns_relative_into_root_relative():
    """Vite writes ``./assets/...``; served for ``/agents/123`` that would resolve
    under ``/agents/``. At the root the renderer still anchors it to ``/``."""
    out = rebase_document_urls(_BUILT_INDEX, "")
    assert 'src="/assets/index-abc.js"' in out
    assert 'href="/favicon.ico"' in out
    assert "./assets" not in out


def test_render_branded_index_html_anchors_the_document_under_the_prefix():
    rendered = render_branded_index_html(
        _BUILT_INDEX,
        branding=Branding(name="Acme", favicon="data:image/png;base64,AAAA", og_image="/og.png"),
        is_xtm=True,
        base_path="/xtm-one",
    )
    assert f'<meta name="{BASE_PATH_META_NAME}" content="/xtm-one" />' in rendered
    assert 'src="/xtm-one/assets/index-abc.js"' in rendered
    assert 'href="/xtm-one/api/v1/platform/branding/asset/favicon?v=' in rendered
    assert 'href="/xtm-one/api/v1/platform/manifest.webmanifest?v=' in rendered
    assert '<meta property="og:image" content="/xtm-one/og.png" />' in rendered
    # Nothing prefixed twice, and the bundler's relative form is gone.
    assert "/xtm-one/xtm-one" not in rendered
    assert "./assets" not in rendered
    # The meta tag is written exactly once.
    assert rendered.count(f'name="{BASE_PATH_META_NAME}"') == 1


def test_render_branded_index_html_at_the_root_writes_an_empty_meta_and_keeps_root_links():
    rendered = render_branded_index_html(_BUILT_INDEX, branding=Branding(name="Acme"), is_xtm=True, base_path="")
    assert f'<meta name="{BASE_PATH_META_NAME}" content="" />' in rendered
    assert 'src="/assets/index-abc.js"' in rendered
    assert 'href="/xtp-one-icon.svg"' in rendered
    assert 'href="/api/v1/platform/manifest.webmanifest?v=' in rendered


def test_render_branded_index_html_never_stacks_two_meta_tags():
    """The renderer always starts from the raw built file (``refresh_branded_index_html``
    re-reads it from disk), but a source that already carries the tag - a hand-patched
    ``index.html`` - must still end up with exactly one."""
    tagged = _BUILT_INDEX.replace(
        '<meta charset="UTF-8" />',
        f'<meta charset="UTF-8" /><meta name="{BASE_PATH_META_NAME}" content="/stale" />',
    )
    rendered = render_branded_index_html(tagged, branding=Branding(name="Acme"), is_xtm=True, base_path="/xtm-one")
    assert rendered.count(f'name="{BASE_PATH_META_NAME}"') == 1
    assert 'content="/stale"' not in rendered
    assert f'<meta name="{BASE_PATH_META_NAME}" content="/xtm-one" />' in rendered


def test_the_real_index_html_leaves_no_document_url_outside_the_prefix():
    """The regex is keyed on ``src`` / ``href`` attributes; run the renderer over
    the source ``frontend/index.html`` so a new head element (a ``<link
    rel="preload">``, an ``og:url``) that the renderer would leave at the root
    is caught here rather than on a deployment."""
    import re

    source = (Path(__file__).resolve().parents[2] / "frontend" / "index.html").read_text(encoding="utf-8")
    rendered = render_branded_index_html(source, branding=Branding(name="Acme"), is_xtm=True, base_path="/xtm-one")
    urls = re.findall(r'\s(?:src|href|content)="([^"]+)"', rendered)
    document_urls = [u for u in urls if u.startswith(("/", "./")) or u.startswith("assets")]
    assert document_urls, "the source document carries no URL at all?"
    for url in document_urls:
        # ``/xtm-one`` alone is the base-path meta tag's own content.
        assert url == "/xtm-one" or url.startswith("/xtm-one/"), f"{url} escaped the prefix"
    assert "/xtm-one/xtm-one" not in rendered


def test_the_consent_page_script_literal_is_a_safe_javascript_string(monkeypatch):
    from app.api.v1 import mcp_oauth
    from app.core.config import settings

    monkeypatch.setattr(settings, "base_path", "/xtm-one")
    assert mcp_oauth._base_path_js_literal() == '"/xtm-one"'
    monkeypatch.setattr(settings, "base_path", "")
    assert mcp_oauth._base_path_js_literal() == '""'
    # The validator never lets these through; the literal is still inert if
    # they ever did.
    monkeypatch.setattr(settings, "base_path", "/</script><script>")
    literal = mcp_oauth._base_path_js_literal()
    assert "<" not in literal and ">" not in literal and "&" not in literal


def test_render_branded_index_html_leaves_an_absolute_og_image_alone():
    rendered = render_branded_index_html(
        _BUILT_INDEX,
        branding=Branding(name="Acme", og_image="https://cdn.example/og.png"),
        is_xtm=True,
        base_path="/xtm-one",
    )
    assert 'content="https://cdn.example/og.png"' in rendered


def test_web_manifest_urls_carry_the_prefix():
    """Manifest URLs resolve against the MANIFEST's URL (under ``/api/v1/platform``),
    so ``start_url`` and the icons must be root-relative with the prefix."""
    manifest = build_web_manifest(Branding(name="Acme"), is_xtm=True, base_path="/xtm-one")
    assert manifest["start_url"] == "/xtm-one/"
    assert manifest["scope"] == "/xtm-one/"
    assert {icon["src"] for icon in manifest["icons"]} == {
        "/xtm-one/xtp-one-icon.svg",
        "/xtm-one/xtm-one-icon-192.png",
        "/xtm-one/xtm-one-icon-512.png",
        "/xtm-one/xtm-one-icon-maskable-512.png",
    }
    assert manifest["id"] == "/xtm-one/"
    assert [s["url"] for s in manifest["shortcuts"]] == [
        "/xtm-one/",
        "/xtm-one/inbox",
        "/xtm-one/chat",
    ]
    assert manifest["share_target"]["action"] == "/xtm-one/share"

    uploaded = build_web_manifest(
        Branding(name="Acme", favicon="data:image/png;base64,AAAA"), is_xtm=False, base_path="/xtm-one"
    )
    assert uploaded["icons"][0]["src"].startswith("/xtm-one/api/v1/platform/branding/asset/favicon?v=")


def test_web_manifest_at_the_root_is_unchanged():
    manifest = build_web_manifest(Branding(name="Acme"), is_xtm=True, base_path="")
    assert manifest["start_url"] == "/"
    assert manifest["scope"] == "/"
    assert manifest["id"] == "/"
    assert {icon["src"] for icon in manifest["icons"]} == {
        "/xtp-one-icon.svg",
        "/xtm-one-icon-192.png",
        "/xtm-one-icon-512.png",
        "/xtm-one-icon-maskable-512.png",
    }


# ── MCP OAuth discovery documents under a prefix ──────────────────────────────


def _well_known_client(monkeypatch, base_path: str) -> TestClient:
    from app.api.v1 import mcp_oauth
    from app.core.config import settings

    monkeypatch.setattr(settings, "base_path", base_path)
    monkeypatch.setattr(settings, "base_url", f"https://intranet.example.com{base_path}")
    app = FastAPI()
    app.include_router(mcp_oauth.well_known_router, prefix="/.well-known")
    app.add_middleware(BasePathMiddleware, base_path=base_path)
    return TestClient(app)


def test_authorization_server_metadata_is_served_in_both_rfc_8414_shapes(monkeypatch):
    """Issuer ``https://host/xtm-one``: a client following RFC 8414 asks the HOST
    ROOT for ``/.well-known/oauth-authorization-server/xtm-one`` (path
    insertion), one following OIDC Discovery asks
    ``/xtm-one/.well-known/oauth-authorization-server`` (path appended). Both
    are this server's one document; any other suffix is nobody's."""
    client = _well_known_client(monkeypatch, "/xtm-one")
    appended = client.get("/xtm-one/.well-known/oauth-authorization-server")
    inserted = client.get("/.well-known/oauth-authorization-server/xtm-one")
    assert appended.status_code == inserted.status_code == 200
    assert appended.json() == inserted.json()
    assert appended.json()["issuer"] == "https://intranet.example.com/xtm-one"
    assert appended.json()["token_endpoint"] == "https://intranet.example.com/xtm-one/mcp/oauth/token"
    assert client.get("/.well-known/oauth-authorization-server/other").status_code == 404
    assert client.get("/.well-known/oauth-authorization-server/xtm-one/more").status_code == 404


def test_protected_resource_metadata_names_the_same_resource_in_both_rfc_9728_shapes(monkeypatch):
    client = _well_known_client(monkeypatch, "/xtm-one")
    challenge_form = client.get("/xtm-one/.well-known/oauth-protected-resource/mcp/user")
    inserted_form = client.get("/.well-known/oauth-protected-resource/xtm-one/mcp/user")
    assert challenge_form.status_code == inserted_form.status_code == 200
    assert challenge_form.json() == inserted_form.json()
    assert challenge_form.json()["resource"] == "https://intranet.example.com/xtm-one/mcp/user"
    assert challenge_form.json()["authorization_servers"] == ["https://intranet.example.com/xtm-one"]
    bare = client.get("/.well-known/oauth-protected-resource/xtm-one")
    assert bare.json()["resource"] == "https://intranet.example.com/xtm-one"


def test_discovery_documents_at_the_root_are_unchanged(monkeypatch):
    client = _well_known_client(monkeypatch, "")
    metadata = client.get("/.well-known/oauth-authorization-server")
    assert metadata.status_code == 200
    assert metadata.json()["issuer"] == "https://intranet.example.com"
    # No issuer path, so the path-insertion form names no document here.
    assert client.get("/.well-known/oauth-authorization-server/xtm-one").status_code == 404
    resource = client.get("/.well-known/oauth-protected-resource/mcp/user").json()
    assert resource["resource"] == "https://intranet.example.com/mcp/user"


# ── the routes that describe the platform's own URL to another system ────────


def test_saml_request_data_carries_the_prefix_in_script_name(monkeypatch):
    """python3-saml rebuilds ``https://host{script_name}`` and, in strict mode,
    refuses a Response whose ``Destination`` / ``Recipient`` (the ACS URL
    advertised from ``BASE_URL``, prefix included) is not that URL - so every
    SAML sign-in fails under a prefix unless ``script_name`` is the PUBLIC
    path."""
    from app.api import auth_sso
    from app.core.config import settings

    monkeypatch.setattr(settings, "base_path", "/xtm-one")
    monkeypatch.setattr(settings, "base_url", "https://intranet.example.com/xtm-one")
    req = auth_sso._saml_request_data("/api/auth/saml/acs", post_data={"SAMLResponse": "x"})
    assert req["script_name"] == "/xtm-one/api/auth/saml/acs"
    assert req["http_host"] == "intranet.example.com"
    assert req["https"] == "on"
    assert req["post_data"] == {"SAMLResponse": "x"} and req["get_data"] == {}

    monkeypatch.setattr(settings, "base_path", "")
    monkeypatch.setattr(settings, "base_url", "http://localhost:4000")
    assert auth_sso._saml_request_data("/api/auth/saml/login")["script_name"] == "/api/auth/saml/login"


def test_saml_routes_build_their_request_through_the_one_helper():
    """The three SAML routes must not spell ``script_name`` by hand again."""
    source = (Path(__file__).resolve().parents[1] / "app" / "api" / "auth_sso.py").read_text(encoding="utf-8")
    assert source.count('"script_name"') == 1, "script_name is spelled outside _saml_request_data"
    for route in ("/api/auth/saml/login", "/api/auth/saml/acs", "/api/auth/saml/metadata"):
        assert f'_saml_request_data("{route}"' in source or f'_saml_request_data(\n        "{route}"' in source


def test_render_base_url_puts_the_page_under_the_prefix(monkeypatch, tmp_path):
    """The headless browser loads a route of the SPA, whose router carries the
    prefix as ``basename`` and renders nothing outside it: every origin the
    page can be loaded from gets the prefix, the explicit one included."""
    from app.core.config import settings
    from app.services import presentation_export

    monkeypatch.setattr(settings, "base_path", "/xtm-one")
    monkeypatch.setattr(settings, "presentation_render_base_url", "http://xtm-one-platform:4000/")
    assert presentation_export.render_base_url() == "http://xtm-one-platform:4000/xtm-one"
    # A value already spelled with a path is the operator's and is kept.
    monkeypatch.setattr(settings, "presentation_render_base_url", "https://intranet.example.com/xtm-one")
    assert presentation_export.render_base_url() == "https://intranet.example.com/xtm-one"
    # The unified image's loopback default.
    monkeypatch.setattr(settings, "presentation_render_base_url", "")
    monkeypatch.setattr(presentation_export, "_FRONTEND_DIST", tmp_path)
    assert presentation_export.render_base_url() == (
        f"http://127.0.0.1:{presentation_export.UNIFIED_IMAGE_PORT}/xtm-one"
    )
    # At the root nothing changes.
    monkeypatch.setattr(settings, "base_path", "")
    monkeypatch.setattr(settings, "presentation_render_base_url", "http://xtm-one-platform:4000")
    assert presentation_export.render_base_url() == "http://xtm-one-platform:4000"


# ── the absolute self-URL handed to external consumers ────────────────────────


def test_external_base_keeps_the_prefix_through_the_https_upgrade(monkeypatch):
    from app.core.config import settings
    from app.core.utils import external_base

    monkeypatch.setattr(settings, "base_url", "https://xtm.example.com/xtm-one")
    monkeypatch.setattr(settings, "trust_proxy_headers", False)

    class _Req:
        base_url = "http://xtm.example.com/xtm-one/"
        headers: dict[str, str] = {}

    assert external_base(_Req()) == "https://xtm.example.com/xtm-one"

    class _Root:
        base_url = "http://xtm.example.com/"
        headers: dict[str, str] = {}

    monkeypatch.setattr(settings, "base_url", "https://xtm.example.com")
    assert external_base(_Root()) == "https://xtm.example.com"
