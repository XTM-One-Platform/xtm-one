"""Content-Security-Policy on the HTML documents the platform serves.

The policy is defence in depth for the output guards:
report-only by default so no deployment can be broken by it, the same policy
enforced on request, nothing at all when switched off. These tests pin the
header per mode and per route, the rule that only documents carry it, the
inline-script hashes of every server-rendered page, and the URL path prefix
(``BASE_PATH``).

The middleware is exercised on a throwaway FastAPI app (like
``test_base_path.py``); the real application is only inspected, never booted.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.testclient import TestClient

import app.core.csp as csp
from app.core.base_path import BasePathMiddleware
from app.core.config import Settings, settings
from app.core.csp import (
    CSP_REPORT_PATH,
    REFERRER_POLICY,
    ContentSecurityPolicyMiddleware,
    PagePolicy,
    inline_script_hashes,
    normalize_csp_mode,
    normalize_frame_ancestor_sources,
    page_policy_for,
    register_page_policy,
)

ENFORCE = "content-security-policy"
REPORT_ONLY = "content-security-policy-report-only"


def _sha256_source(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return f"'sha256-{base64.b64encode(digest).decode('ascii')}'"


def _directives(policy: str) -> dict[str, list[str]]:
    parsed: dict[str, list[str]] = {}
    for part in policy.split(";"):
        tokens = part.split()
        if tokens:
            parsed[tokens[0]] = tokens[1:]
    return parsed


@pytest.fixture
def page_registry():
    """Register throwaway page policies without leaking them into other tests."""
    exact = dict(csp._PAGE_POLICIES)
    prefixes = list(csp._PAGE_PREFIX_POLICIES)
    yield
    csp._PAGE_POLICIES.clear()
    csp._PAGE_POLICIES.update(exact)
    csp._PAGE_PREFIX_POLICIES[:] = prefixes


@pytest.fixture
def mode(monkeypatch):
    def _set(value: str) -> None:
        monkeypatch.setattr(settings, "content_security_policy_mode", value)

    _set("report-only")
    monkeypatch.setattr(settings, "content_security_policy_frame_ancestors", [])
    return _set


def _app(*, base_path: str = "", spa_hashes: tuple[str, ...] = (), embedders=None) -> FastAPI:
    app = FastAPI()

    @app.get("/")
    async def spa():
        return HTMLResponse("<!doctype html><html><body><div id=root></div></body></html>")

    @app.get("/api/v1/data")
    async def data():
        return JSONResponse({"ok": True})

    @app.get("/download")
    async def download():
        return HTMLResponse(
            "<html><body>a stored file</body></html>",
            headers={"Content-Disposition": 'attachment; filename="page.html"'},
        )

    @app.get("/own-policy")
    async def own_policy():
        return HTMLResponse("<html></html>", headers={"Content-Security-Policy": "default-src 'none'"})

    @app.get("/own-referrer-policy")
    async def own_referrer_policy():
        return HTMLResponse("<html></html>", headers={"Referrer-Policy": "no-referrer"})

    @app.get("/csp-test/page")
    async def page():
        return HTMLResponse("<html></html>")

    @app.get("/csp-test/tree/{rest:path}")
    async def tree(rest: str):
        return HTMLResponse(f"<html>{rest}</html>")

    app.add_middleware(ContentSecurityPolicyMiddleware, spa_script_hashes=lambda: spa_hashes, embedders=embedders)
    app.add_middleware(BasePathMiddleware, base_path=base_path)
    return app


# -- modes -------------------------------------------------------------------


def test_default_mode_is_report_only_so_nothing_is_ever_blocked(mode):
    """Report-only adds one header the browser acts on by reporting only: no
    Referrer-Policy either, so one a proxy or CDN adds only when the response
    carries none keeps applying."""
    response = TestClient(_app()).get("/")
    assert response.status_code == 200
    assert REPORT_ONLY in response.headers
    assert ENFORCE not in response.headers
    assert "referrer-policy" not in response.headers


def test_enforce_sends_the_same_policy_as_an_enforced_header(mode):
    report_only = TestClient(_app()).get("/").headers[REPORT_ONLY]
    mode("enforce")
    response = TestClient(_app()).get("/")
    assert REPORT_ONLY not in response.headers
    assert response.headers[ENFORCE] == report_only
    assert response.headers["referrer-policy"] == REFERRER_POLICY == "strict-origin-when-cross-origin"


def test_a_referrer_policy_already_set_is_kept_under_enforce(mode):
    mode("enforce")
    response = TestClient(_app()).get("/own-referrer-policy")
    assert response.headers.get_list("referrer-policy") == ["no-referrer"]
    assert ENFORCE in response.headers


def test_off_sends_no_header_at_all(mode):
    mode("off")
    response = TestClient(_app()).get("/")
    assert ENFORCE not in response.headers
    assert REPORT_ONLY not in response.headers
    assert "reporting-endpoints" not in response.headers
    assert "referrer-policy" not in response.headers


def test_the_settings_default_is_report_only():
    assert Settings.model_fields["content_security_policy_mode"].default == "report-only"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "report-only"),
        ("", "report-only"),
        ("  ", "report-only"),
        ("report-only", "report-only"),
        ("REPORT_ONLY", "report-only"),
        (" Enforce ", "enforce"),
        ("off", "off"),
    ],
)
def test_normalize_csp_mode(raw, expected):
    assert normalize_csp_mode(raw) == expected


@pytest.mark.parametrize("raw", ["enforced", "on", "true", "block", "report"])
def test_an_unknown_mode_is_refused_rather_than_read_as_another(raw):
    with pytest.raises(ValueError, match="CONTENT_SECURITY_POLICY_MODE"):
        normalize_csp_mode(raw)


# -- only documents ----------------------------------------------------------


@pytest.mark.parametrize("platform_mode", ["report-only", "enforce"])
def test_json_responses_carry_no_policy_and_no_referrer_policy(mode, platform_mode):
    mode(platform_mode)
    response = TestClient(_app()).get("/api/v1/data")
    assert REPORT_ONLY not in response.headers
    assert ENFORCE not in response.headers
    assert "referrer-policy" not in response.headers


@pytest.mark.parametrize("platform_mode", ["report-only", "enforce"])
def test_a_downloaded_html_file_is_left_alone(mode, platform_mode):
    mode(platform_mode)
    response = TestClient(_app()).get("/download")
    assert REPORT_ONLY not in response.headers
    assert ENFORCE not in response.headers
    assert "referrer-policy" not in response.headers


def test_a_policy_a_handler_set_itself_is_never_replaced(mode):
    mode("enforce")
    response = TestClient(_app()).get("/own-policy")
    assert response.headers[ENFORCE] == "default-src 'none'"
    assert REPORT_ONLY not in response.headers


# -- the SPA policy ----------------------------------------------------------


def test_the_spa_policy(mode):
    policy = _directives(TestClient(_app(), base_url="https://testserver").get("/").headers[REPORT_ONLY])
    assert policy["default-src"] == ["'self'"]
    assert policy["script-src"] == ["'self'"]
    assert "'unsafe-inline'" not in policy["script-src"]
    assert "'unsafe-eval'" not in policy["script-src"]
    assert policy["style-src"] == ["'self'", "'unsafe-inline'"]
    assert policy["img-src"][:3] == ["'self'", "data:", "blob:"]
    assert "https:" in policy["img-src"]
    assert policy["font-src"] == ["'self'", "data:"]
    assert policy["media-src"] == ["'self'", "blob:"]
    assert policy["object-src"] == ["'none'"]
    assert policy["base-uri"] == ["'self'"]
    assert policy["worker-src"] == ["'self'", "blob:"]
    # Only the two video players a deck's video block embeds (presentation_spec.VIDEO_EMBED_ORIGINS).
    assert policy["frame-src"] == ["'self'", "https://www.youtube-nocookie.com", "https://player.vimeo.com"]
    assert policy["frame-ancestors"] == ["'self'"]
    # Every SPA form is submitted by script and SSO starts with a navigation.
    assert policy["form-action"] == ["'self'"]
    assert policy["report-uri"] == [CSP_REPORT_PATH]
    assert "report-to" not in policy


@pytest.mark.parametrize("platform_mode", ["report-only", "enforce"])
@pytest.mark.parametrize(
    "base_url",
    [
        "https://xtm.example.com",
        "http://localhost:4000",
        "http://127.0.0.1:4000",
        "http://xtm.example.com",
        "http://10.0.0.5:4000",
    ],
)
def test_reports_go_to_report_uri_alone_on_every_deployment_shape(mode, monkeypatch, platform_mode, base_url):
    """Every browser resolves ``report-uri`` against the document's own URL.
    ``report-to`` is never named: Chromium ignores ``report-uri`` in a policy
    that names it and drops a ``Reporting-Endpoints`` endpoint that is not
    HTTPS (a loopback page included), and an absolute endpoint could only come
    from ``BASE_URL``, which by default names another host."""
    mode(platform_mode)
    monkeypatch.setattr(settings, "base_url", Settings.model_fields["base_url"].default)
    response = TestClient(_app(), base_url=base_url).get("/")
    header = REPORT_ONLY if platform_mode == "report-only" else ENFORCE
    policy = _directives(response.headers[header])
    assert policy["report-uri"] == [CSP_REPORT_PATH]
    assert "report-to" not in policy
    assert "reporting-endpoints" not in response.headers
    assert "report-to" not in response.headers


def test_inline_scripts_of_the_spa_document_are_allowed_by_hash(mode):
    source = _sha256_source("window.__boot = 1;")
    policy = _directives(TestClient(_app(spa_hashes=(source,))).get("/").headers[REPORT_ONLY])
    assert policy["script-src"] == ["'self'", source]


def test_connect_src_names_the_websocket_origin_explicitly(mode):
    client = TestClient(_app(), base_url="http://xtm.example.com:4000")
    connect = _directives(client.get("/").headers[REPORT_ONLY])["connect-src"]
    assert connect[0] == "'self'"
    assert "wss://xtm.example.com:4000" in connect
    assert "ws://xtm.example.com:4000" in connect


def test_connect_src_allows_the_desktop_app_ipc(mode):
    # XTM One Desktop (Tauri v2) talks to its native side through a fetch to
    # these two origins before falling back to postMessage.
    connect = _directives(TestClient(_app()).get("/").headers[REPORT_ONLY])["connect-src"]
    assert "ipc:" in connect
    assert "http://ipc.localhost" in connect


def test_connect_src_includes_the_configured_public_origins(mode, monkeypatch):
    monkeypatch.setattr(settings, "base_url", "https://xtm.example.com/xtm-one")
    monkeypatch.setattr(settings, "frontend_url", "https://app.example.com")
    connect = _directives(TestClient(_app()).get("/").headers[REPORT_ONLY])["connect-src"]
    assert "wss://xtm.example.com" in connect
    assert "wss://app.example.com" in connect


@pytest.mark.parametrize(
    "host",
    [
        "evil.example.com; script-src *",
        "evil.example.com 'unsafe-inline'",
        "exa mple.com",
        "example.com/path",
        "user@example.com",
    ],
)
def test_a_forged_host_header_cannot_add_sources(mode, host, monkeypatch):
    monkeypatch.setattr(settings, "base_url", "")
    monkeypatch.setattr(settings, "frontend_url", "")
    policy = TestClient(_app()).get("/", headers={"host": host}).headers[REPORT_ONLY]
    assert host not in policy
    assert policy.count("script-src") == 1
    assert _directives(policy)["script-src"] == ["'self'"]
    assert _directives(policy)["connect-src"] == ["'self'", "ipc:", "http://ipc.localhost"]


def test_frame_ancestors_extras_come_from_the_setting(mode, monkeypatch):
    monkeypatch.setattr(
        settings,
        "content_security_policy_frame_ancestors",
        ["https://portal.example.com", "https://*.intranet.example.com"],
    )
    policy = _directives(TestClient(_app()).get("/").headers[REPORT_ONLY])
    assert policy["frame-ancestors"] == [
        "'self'",
        "https://portal.example.com",
        "https://*.intranet.example.com",
    ]


def test_frame_ancestors_include_the_registered_platforms(mode, monkeypatch):
    monkeypatch.setattr(settings, "content_security_policy_frame_ancestors", ["https://portal.example.com"])

    def embedders():
        return (
            "https://opencti.example.com",
            "https://openaev.example.com:8443",
            # Values read from the database are re-checked before they reach
            # the header.
            "https://evil.example.com; script-src *",
            "javascript:alert(1)",
        )

    policy = _directives(TestClient(_app(embedders=embedders)).get("/").headers[REPORT_ONLY])
    assert policy["frame-ancestors"] == [
        "'self'",
        "https://portal.example.com",
        "https://opencti.example.com",
        "https://openaev.example.com:8443",
    ]
    assert policy["script-src"] == ["'self'"]


def test_registered_platforms_do_not_widen_a_self_only_page(mode, page_registry):
    register_page_policy("/csp-test/page", PagePolicy(frame_ancestors="self"))
    response = TestClient(_app(embedders=lambda: ("https://opencti.example.com",))).get("/csp-test/page")
    assert _directives(response.headers[REPORT_ONLY])["frame-ancestors"] == ["'self'"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, []),
        ("", []),
        ("https://portal.example.com", ["https://portal.example.com"]),
        (
            " https://Portal.example.com/ , https://*.example.org:8443 ",
            ["https://portal.example.com", "https://*.example.org:8443"],
        ),
        ("https://a.example.com https://b.example.com", ["https://a.example.com", "https://b.example.com"]),
        ("https:", ["https:"]),
        ("*", ["*"]),
        ("'self'", ["'self'"]),
        (["https://a.example.com", "https://a.example.com"], ["https://a.example.com"]),
    ],
)
def test_normalize_frame_ancestor_sources(raw, expected):
    assert normalize_frame_ancestor_sources(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "'none'",
        "'unsafe-inline'",
        "https://portal.example.com;script-src *",
        "javascript:alert(1)",
        "https://portal.example.com/path?x=1",
        "https://user@portal.example.com",
        "http://[::1",
        # CSP host sources have no IPv6 form: a browser would drop the entry.
        "http://[::1]:8080",
        "https://[2001:db8::1]",
    ],
)
def test_a_malformed_frame_ancestor_is_refused(raw):
    with pytest.raises(ValueError, match="CONTENT_SECURITY_POLICY_FRAME_ANCESTORS"):
        normalize_frame_ancestor_sources(raw)


def _csp_warnings(caplog) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == "app.core.csp" and record.levelno >= logging.WARNING]


@pytest.mark.parametrize("source", ["*", "https:", "http:", "https://*", "https://*:8443", "chrome-extension:"])
def test_a_frame_ancestor_that_admits_every_site_is_named_at_startup(mode, monkeypatch, caplog, source):
    """Accepted - a deployment may really be embedded everywhere - but never
    silently: once enforced, every such site may frame the platform, which is
    the clickjacking ``frame-ancestors`` is there to stop."""
    monkeypatch.setattr(settings, "content_security_policy_frame_ancestors", ["https://portal.acme.test", source])
    with caplog.at_level(logging.WARNING, logger="app.core.csp"):
        csp.log_content_security_policy_configuration()
    [record] = _csp_warnings(caplog)
    message = record.getMessage()
    assert "CONTENT_SECURITY_POLICY_FRAME_ANCESTORS" in message
    assert source in message
    assert "portal.acme.test" not in message


def test_named_embedders_are_not_warned_about(mode, monkeypatch, caplog):
    monkeypatch.setattr(
        settings,
        "content_security_policy_frame_ancestors",
        ["'self'", "https://portal.acme.test", "https://*.intranet.acme.test", "portal.acme.test:8443"],
    )
    with caplog.at_level(logging.WARNING, logger="app.core.csp"):
        csp.log_content_security_policy_configuration()
    assert _csp_warnings(caplog) == []


def test_no_frame_ancestor_warning_while_the_policy_is_off(mode, monkeypatch, caplog):
    mode("off")
    monkeypatch.setattr(settings, "content_security_policy_frame_ancestors", ["*"])
    with caplog.at_level(logging.WARNING, logger="app.core.csp"):
        csp.log_content_security_policy_configuration()
    assert _csp_warnings(caplog) == []


def test_the_startup_checks_the_frame_ancestors_setting():
    import inspect

    import app.main as main_module

    assert "log_content_security_policy_configuration()" in inspect.getsource(main_module.lifespan)


# -- per-page policies -------------------------------------------------------


def test_a_registered_page_adds_its_sources_and_hashes(mode, page_registry):
    source = _sha256_source("init();")
    register_page_policy(
        "/csp-test/page",
        PagePolicy(
            sources={"script-src": ("https://cdn.example.com",), "font-src": ("https://fonts.example.com",)},
            script_hashes=lambda: (source,),
        ),
    )
    policy = _directives(TestClient(_app(spa_hashes=("'sha256-spa'",))).get("/csp-test/page").headers[REPORT_ONLY])
    assert policy["script-src"] == ["'self'", "https://cdn.example.com", source]
    assert policy["font-src"] == ["'self'", "data:", "https://fonts.example.com"]
    # The SPA's hashes belong to the SPA document, not to every page.
    assert "'sha256-spa'" not in policy["script-src"]


def test_frame_ancestors_per_page(mode, page_registry):
    register_page_policy("/csp-test/page", PagePolicy(frame_ancestors="any"))
    assert "frame-ancestors" not in _directives(TestClient(_app()).get("/csp-test/page").headers[REPORT_ONLY])
    register_page_policy("/csp-test/page", PagePolicy(frame_ancestors="self"))
    assert _directives(TestClient(_app()).get("/csp-test/page").headers[REPORT_ONLY])["frame-ancestors"] == ["'self'"]


def test_a_report_only_page_stays_report_only_when_the_platform_enforces(mode, page_registry):
    register_page_policy("/csp-test/page", PagePolicy(always_report_only=True))
    mode("enforce")
    response = TestClient(_app()).get("/csp-test/page")
    assert REPORT_ONLY in response.headers
    assert ENFORCE not in response.headers
    assert ENFORCE in TestClient(_app()).get("/").headers


def test_a_subtree_policy_covers_every_page_under_it(mode, page_registry):
    register_page_policy("/csp-test/tree", PagePolicy(frame_ancestors="any"), include_subpaths=True)
    assert page_policy_for("/csp-test/tree") is page_policy_for("/csp-test/tree/a/b.html")
    assert page_policy_for("/csp-test/treehouse") is None
    response = TestClient(_app()).get("/csp-test/tree/index.html")
    assert "frame-ancestors" not in _directives(response.headers[REPORT_ONLY])


def test_register_page_policy_refuses_a_path_that_is_not_route_relative(page_registry):
    with pytest.raises(ValueError):
        register_page_policy("csp-test/page", PagePolicy())


# -- BASE_PATH ---------------------------------------------------------------


@pytest.mark.parametrize("path", ["/xtm-one/", "/"])
def test_the_report_endpoint_carries_the_url_path_prefix(mode, monkeypatch, path):
    """Forwarded (``/xtm-one/``) and stripped (``/``) proxy shapes alike."""
    monkeypatch.setattr(settings, "base_path", "/xtm-one")
    response = TestClient(_app(base_path="/xtm-one"), base_url="https://testserver").get(path)
    policy = _directives(response.headers[REPORT_ONLY])
    assert policy["report-uri"] == ["/xtm-one/api/v1/csp-report"]
    assert "report-to" not in policy
    assert "reporting-endpoints" not in response.headers


def test_page_policies_match_on_the_route_path_under_a_prefix(mode, monkeypatch, page_registry):
    monkeypatch.setattr(settings, "base_path", "/xtm-one")
    register_page_policy("/csp-test/page", PagePolicy(frame_ancestors="any"))
    client = TestClient(_app(base_path="/xtm-one"))
    for path in ("/xtm-one/csp-test/page", "/csp-test/page"):
        assert "frame-ancestors" not in _directives(client.get(path).headers[REPORT_ONLY])


# -- inline script hashes ----------------------------------------------------


def test_inline_script_hashes():
    html = (
        "<html><head>"
        '<script src="/assets/index.js"></script>'
        "<script>window.a = 1;</script>"
        '<script type="module">import "./x.js";</script>'
        '<script type="application/json">{"not": "executed"}</script>'
        '<script type="application/ld+json">{"@context": "x"}</script>'
        "</head><body><script>\r\nb();\r\n</script></body></html>"
    )
    assert inline_script_hashes(html) == (
        _sha256_source("window.a = 1;"),
        _sha256_source('import "./x.js";'),
        # The HTML tokenizer normalises CR LF to LF before the browser hashes.
        _sha256_source("\nb();\n"),
    )


def test_inline_script_hashes_of_a_document_without_inline_script():
    assert inline_script_hashes('<html><script src="/a.js"></script></html>') == ()


def test_the_branded_index_exposes_the_hashes_of_its_inline_scripts(tmp_path: Path):
    from app.services import branding

    (tmp_path / "index.html").write_text(
        '<!doctype html><html><head><meta charset="UTF-8" /><title>x</title></head>'
        "<body><script>window.__flag = true;</script>"
        '<script type="module" src="./assets/index.js"></script></body></html>',
        encoding="utf-8",
    )
    previous_dir = branding._index_html_frontend_dir
    previous_html = branding._index_html_content
    previous_hashes = branding._index_html_script_hashes
    try:
        branding.configure_branded_index_html(tmp_path)
        assert branding.get_branded_index_script_hashes() == (_sha256_source("window.__flag = true;"),)
        (tmp_path / "index.html").write_text("<html><head></head><body></body></html>", encoding="utf-8")
        branding.refresh_configured_branded_index_html()
        assert branding.get_branded_index_script_hashes() == ()
    finally:
        branding._index_html_frontend_dir = previous_dir
        branding._index_html_content = previous_html
        branding._index_html_script_hashes = previous_hashes


def test_the_real_index_html_pre_paint_script_is_allowed_by_its_hash(tmp_path: Path):
    """The theme pre-paint script of ``frontend/index.html`` survives the
    branding render unchanged and is allowed by the hash of its own text, so an
    enforced policy never blocks it and never needs ``'unsafe-inline'``."""
    from app.services import branding

    source = (Path(__file__).resolve().parents[2] / "frontend" / "index.html").read_text(encoding="utf-8")
    scripts = re.findall(r"<script>(.*?)</script>", source, flags=re.DOTALL)
    assert len(scripts) == 1 and 'localStorage.getItem("copilot-theme")' in scripts[0]
    (tmp_path / "index.html").write_text(source, encoding="utf-8")
    previous_dir = branding._index_html_frontend_dir
    previous_html = branding._index_html_content
    previous_hashes = branding._index_html_script_hashes
    try:
        branding.configure_branded_index_html(tmp_path)
        assert branding.get_branded_index_script_hashes() == (_sha256_source(scripts[0]),)
    finally:
        branding._index_html_frontend_dir = previous_dir
        branding._index_html_content = previous_html
        branding._index_html_script_hashes = previous_hashes


# -- the real application ----------------------------------------------------


def test_the_middleware_sits_inside_the_base_path_normaliser():
    from app.main import application
    from app.services.branding import get_branded_index_script_hashes
    from app.services.registered_platform_origins import registered_platform_origins

    classes = [m.cls for m in application.user_middleware]
    assert ContentSecurityPolicyMiddleware in classes
    # ``user_middleware[0]`` is the outermost layer.
    assert classes.index(BasePathMiddleware) < classes.index(ContentSecurityPolicyMiddleware)
    middleware = next(m for m in application.user_middleware if m.cls is ContentSecurityPolicyMiddleware)
    assert middleware.kwargs["spa_script_hashes"] is get_branded_index_script_hashes
    assert middleware.kwargs["embedders"] is registered_platform_origins


def test_every_page_policy_names_a_route_the_application_serves():
    from app.main import application

    served = {getattr(route, "path", None) for route in application.routes}
    for path in csp._PAGE_POLICIES:
        assert path in served, f"page policy registered for {path}, which no route serves"


def test_the_report_endpoint_is_reachable_without_a_licence():
    from app.main import UNLICENSED_ALLOWED_PREFIXES

    assert any(CSP_REPORT_PATH.startswith(prefix) for prefix in UNLICENSED_ALLOWED_PREFIXES)


def test_reports_do_not_write_an_access_log_line_each_even_when_refused():
    """A refused report (rate limited, too large, not a report) is browser
    traffic; the aggregated CSP logging replaces one line per request. A
    server fault on the path is still an error, and other paths keep their
    warnings."""
    import logging

    from app.main import _access_log_level

    # 503: shed under load, or refused while Redis is down and rate limits fail closed.
    for status_code in (204, 400, 413, 415, 429, 503):
        assert _access_log_level(CSP_REPORT_PATH, status_code, error=False, cancelled=False) == logging.DEBUG
    assert _access_log_level(CSP_REPORT_PATH, 500, error=False, cancelled=False) == logging.ERROR
    assert _access_log_level("/api/health", 404, error=False, cancelled=False) == logging.WARNING
    assert _access_log_level(CSP_REPORT_PATH + "x", 404, error=False, cancelled=False) == logging.WARNING


def _inline_scripts(html: str) -> tuple[str, ...]:
    return inline_script_hashes(html)


def _policy_hashes(route: str) -> set[str]:
    policy = page_policy_for(route)
    assert policy is not None, f"no page policy registered for {route}"
    return set(policy.script_hashes() if policy.script_hashes else ())


def test_the_mcp_consent_page_runs_only_hashed_script_and_no_inline_handler():
    import app.main  # noqa: F401 - registers every page policy
    from app.api.v1.mcp_oauth import _consent_html

    html = _consent_html(client_name="Claude", client_id="c", redirect_uri="http://localhost:1/cb")
    # Event-handler attributes cannot be allowed by a hash without
    # 'unsafe-hashes'; the page binds its handlers from its script instead.
    assert not re.search(r"\son[a-z]+\s*=", html, flags=re.IGNORECASE)
    assert set(_inline_scripts(html)) <= _policy_hashes("/mcp/oauth/authorize")
    policy = page_policy_for("/mcp/oauth/authorize")
    assert policy.frame_ancestors == "self"
    assert not policy.always_report_only
    assert "https://fonts.googleapis.com" in policy.sources["style-src"]
    assert "https://fonts.gstatic.com" in policy.sources["font-src"]
    built = _directives(csp.build_policy({"type": "http", "headers": []}, page=policy))
    # Widened per response by the handlers (next test), never dropped.
    assert built["form-action"] == ["'self'"]
    assert built["frame-ancestors"] == ["'self'"]
    assert built["script-src"][0] == "'self'"


def test_the_consent_form_may_reach_base_url_only(monkeypatch):
    """Its form posts to BASE_URL. The client's redirect_uri is not a form
    target: the sign-in leaves by a page that navigates there (next test)."""
    from types import SimpleNamespace

    import app.main  # noqa: F401
    from app.api.v1 import mcp_oauth

    monkeypatch.setattr(mcp_oauth, "_base_url", lambda: "https://xtm.example.com/prefix")
    request = SimpleNamespace(scope={"type": "http", "headers": []})
    mcp_oauth._allow_consent_form_target(request)
    built = _directives(csp.build_policy(request.scope, page=page_policy_for("/mcp/oauth/authorize")))
    assert built["form-action"] == ["'self'", "https://xtm.example.com"]


def test_the_consent_sign_in_leaves_by_a_page_no_form_action_checks(mode, monkeypatch):
    """Browsers check form-action on every redirect of the navigation a form
    submission starts, so a 302 to an MCP client's callback that redirects
    again (to the client's own front end) was blocked on that second hop once
    the policy was enforced. A successful sign-in answers with a
    document instead - under the consent page's policy, never framed - whose
    refresh starts a navigation that is not a form submission. It runs no
    script and holds no form, so it needs nothing more from that policy, and
    the policy names no client origin."""
    import app.main  # noqa: F401
    from app.api.v1 import mcp_oauth

    monkeypatch.setattr(mcp_oauth, "_base_url", lambda: "https://xtm.example.com")
    target = "http://localhost:8202/api/oauth/callback?code=c0de&state=s"
    html = mcp_oauth._return_to_client_html(target=target, client_name="XTM One", app_name="XTM One")
    assert inline_script_hashes(html) == ()
    assert "<form" not in html and "<script" not in html
    assert f'<meta http-equiv="refresh" content="0; url={target.replace("&", "&amp;")}">' in html

    page = FastAPI()

    @page.post("/mcp/oauth/authorize")
    async def signed_in():
        return mcp_oauth._grant_response(target, client_name="XTM One", app_name="XTM One")

    mode("enforce")
    page.add_middleware(ContentSecurityPolicyMiddleware)
    with TestClient(page) as client:
        response = client.post("/mcp/oauth/authorize")
    assert response.status_code == 200
    assert "location" not in response.headers
    built = _directives(response.headers[ENFORCE])
    assert built["form-action"] == ["'self'"]
    assert built["frame-ancestors"] == ["'self'"]
    assert not any("localhost:8202" in source for sources in built.values() for source in sources)


def test_a_handler_widens_a_directive_for_its_own_response_only(mode):
    from starlette.applications import Starlette
    from starlette.responses import HTMLResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    async def widened(request):
        csp.add_response_sources(
            request,
            "form-action",
            "https://client.example.com",
            "javascript:alert(1)",
            None,
            "https://client.example.com/path",
            "https://evil.example.com; script-src *",
        )
        csp.add_response_sources(request, "not-a-directive", "https://y.example.com")
        return HTMLResponse("<p>widened</p>")

    async def plain(request):
        return HTMLResponse("<p>plain</p>")

    app = Starlette(routes=[Route("/widened", widened), Route("/plain", plain)])
    app.add_middleware(ContentSecurityPolicyMiddleware)
    with TestClient(app) as client:
        first = _directives(client.get("/widened").headers[csp.REPORT_ONLY_HEADER])
        second = _directives(client.get("/plain").headers[csp.REPORT_ONLY_HEADER])
    assert first["form-action"] == ["'self'", "https://client.example.com"]
    assert "not-a-directive" not in first
    assert first["script-src"] == ["'self'"]
    assert second["form-action"] == ["'self'"]


def test_the_teams_tab_page_runs_only_hashed_script():
    import asyncio

    import app.main  # noqa: F401
    from app.channels.teams import teams_tab

    class _NoDb:
        pass

    response = asyncio.run(teams_tab(db=_NoDb()))
    html = response.body.decode("utf-8")
    assert '<script src="https://res.cdn.office.net/teams-js/2.31.1/js/MicrosoftTeams.min.js"></script>' in html
    assert "microsoftTeams.appInitialization.notifySuccess();" in html
    assert set(_inline_scripts(html)) <= _policy_hashes("/api/channels/teams/tab")
    assert _inline_scripts(html)
    policy = page_policy_for("/api/channels/teams/tab")
    assert policy.always_report_only
    assert policy.frame_ancestors == "any"
    assert "https://res.cdn.office.net" in policy.sources["script-src"]


def test_the_api_reference_runs_only_hashed_script_and_escapes_the_platform_name(monkeypatch):
    import asyncio

    import app.main as main_module
    from app.services.branding import Branding

    monkeypatch.setattr(main_module, "get_cached_branding", lambda: Branding(name="</title><script>alert(1)</script>"))
    response = asyncio.run(main_module.scalar_api_reference())
    html = response.body.decode("utf-8")
    assert "<script>alert(1)</script>" not in html
    assert set(_inline_scripts(html)) <= _policy_hashes("/api/reference")
    policy = page_policy_for("/api/reference")
    assert policy.always_report_only
    assert "https://cdn.jsdelivr.net" in policy.sources["script-src"]
    # The Scalar bundle evaluates strings; the page is never enforced.
    assert "'unsafe-eval'" in policy.sources["script-src"]


def test_the_api_reference_hashes_come_from_a_constant_render_not_the_served_page(monkeypatch):
    import asyncio

    import app.main as main_module
    from app.services.branding import Branding

    main_module._scalar_script_hashes.cache_clear()
    before = main_module._scalar_script_hashes()
    assert before
    monkeypatch.setattr(main_module, "get_cached_branding", lambda: Branding(name="Acme"))
    asyncio.run(main_module.scalar_api_reference())
    assert main_module._scalar_script_hashes() == before
    assert csp.script_hash_source("alert(1)") not in _policy_hashes("/api/reference")


def test_no_page_allows_inline_script_and_only_the_api_reference_allows_eval():
    import app.main  # noqa: F401 - registers every page policy

    assert "'unsafe-eval'" not in csp.BASE_DIRECTIVES["script-src"]
    registered = [*csp._PAGE_POLICIES.items(), *csp._PAGE_PREFIX_POLICIES]
    assert registered
    for path, policy in registered:
        script_sources = policy.sources.get("script-src", ())
        assert "'unsafe-inline'" not in script_sources, path
        assert ("'unsafe-eval'" in script_sources) == (path == "/api/reference"), path


@pytest.mark.parametrize("route", ["/office", "/office/", "/office/index.html", "/office/assets/app.js"])
def test_the_office_add_in_is_report_only_and_framable_by_its_hosts(route):
    import app.main  # noqa: F401

    policy = page_policy_for(route)
    assert policy is not None
    assert policy.always_report_only
    assert policy.frame_ancestors == "any"
    assert "https://appsforoffice.microsoft.com" in policy.sources["script-src"]


# -- registered platforms (frame-ancestors) ----------------------------------


@pytest.fixture
def platform_origins_state(monkeypatch):
    from app.services import registered_platform_origins as module

    # Registered platforms are trusted embedders only behind a registration
    # token of the operator's choosing (see the dedicated test below).
    monkeypatch.setattr(settings, "platform_registration_token", "an-operator-chosen-token")
    saved = (
        module._origins,
        module._refreshed_at,
        module._refresh_task,
        module._started_generation,
        module._applied_generation,
        module._cap_warned_for,
    )
    yield module
    (
        module._origins,
        module._refreshed_at,
        module._refresh_task,
        module._started_generation,
        module._applied_generation,
        module._cap_warned_for,
    ) = saved


class _Rows:
    def __init__(self, urls):
        self._urls = urls

    def scalars(self):
        return self

    def all(self):
        return list(self._urls)


class _Session:
    def __init__(self, urls, statements, fail=False):
        self.urls, self.statements, self.fail = urls, statements, fail

    async def __aenter__(self):
        if self.fail:
            raise OSError("database unreachable")
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement):
        self.statements.append(statement)
        return _Rows(self.urls)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://opencti.example.com", "https://opencti.example.com"),
        ("https://OpenCTI.example.com:8443/some/path?token=x#y", "https://opencti.example.com:8443"),
        ("http://10.0.0.5:4000/", "http://10.0.0.5:4000"),
        # CSP host sources have no IPv6 form: a browser would drop it.
        ("http://[::1]:8080", None),
        ("ftp://files.example.com", None),
        ("javascript:alert(1)", None),
        ("https://exa mple.com", None),
        ("https://", None),
        ("", None),
        (None, None),
        (12, None),
    ],
)
def test_platform_origin(url, expected):
    from app.services.registered_platform_origins import platform_origin

    assert platform_origin(url) == expected


def test_the_active_registrations_are_read(monkeypatch, platform_origins_state):
    import asyncio

    import app.core.database as database

    statements: list = []
    urls = ["https://a.example.com/x", "https://a.example.com", "not a url", "https://b.example.com:9000"]
    monkeypatch.setattr(database, "async_session", lambda: _Session(urls, statements))
    origins = asyncio.run(platform_origins_state.refresh_registered_platform_origins())
    assert origins == ("https://a.example.com", "https://b.example.com:9000")
    assert platform_origins_state.registered_platform_origins() == origins
    assert "platform_registrations.status" in str(statements[0])


def test_a_failed_refresh_keeps_the_last_snapshot(monkeypatch, platform_origins_state):
    import asyncio

    import app.core.database as database

    platform_origins_state._origins = ("https://a.example.com",)
    monkeypatch.setattr(database, "async_session", lambda: _Session([], [], fail=True))
    assert asyncio.run(platform_origins_state.refresh_registered_platform_origins()) == ("https://a.example.com",)
    assert platform_origins_state._refreshed_at is not None


class _HangingSession(_Session):
    async def execute(self, statement):
        import asyncio

        await asyncio.sleep(3600)


def test_a_refresh_that_hangs_gives_up_and_keeps_the_last_snapshot(monkeypatch, platform_origins_state):
    """Awaited before the readiness gate and by a registration: a database that
    never answers costs a bounded wait, never readiness or the heartbeat."""
    import asyncio
    import time

    import app.core.database as database

    platform_origins_state._origins = ("https://a.example.com",)
    monkeypatch.setattr(platform_origins_state, "REFRESH_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(database, "async_session", lambda: _HangingSession([], []))
    started = time.monotonic()
    assert asyncio.run(platform_origins_state.refresh_registered_platform_origins()) == ("https://a.example.com",)
    assert time.monotonic() - started < 5


def test_the_registered_origins_are_capped_in_a_stable_order(monkeypatch, platform_origins_state, caplog):
    """Every origin lengthens the policy header of every page: past the cap a
    reverse proxy's response-header buffer could refuse the page (a 502)."""
    import asyncio

    import app.core.database as database

    cap = platform_origins_state.MAX_FRAMING_ORIGINS
    urls = [f"https://p{index:03}.example.com/path" for index in reversed(range(cap + 5))]
    statements: list = []
    monkeypatch.setattr(database, "async_session", lambda: _Session(urls, statements))
    with caplog.at_level(logging.WARNING, logger="app.services.registered_platform_origins"):
        asyncio.run(platform_origins_state.refresh_registered_platform_origins())
        asyncio.run(platform_origins_state.refresh_registered_platform_origins())
    served = platform_origins_state.registered_platform_origins()
    assert len(served) == cap
    assert list(served) == sorted(served)
    assert served[0] == "https://p000.example.com"
    [record] = [r for r in caplog.records if r.name == "app.services.registered_platform_origins"]
    assert str(cap + 5) in record.getMessage()
    assert "CONTENT_SECURITY_POLICY_FRAME_ANCESTORS" in record.getMessage()
    # A platform past the cap is still known: its heartbeat costs no query.
    asyncio.run(platform_origins_state.refresh_for_registration(f"https://p{cap + 4:03}.example.com"))
    assert len(statements) == 2


def test_a_stale_snapshot_is_refreshed_in_the_background(monkeypatch, platform_origins_state):
    import asyncio

    import app.core.database as database

    monkeypatch.setattr(database, "async_session", lambda: _Session(["https://c.example.com"], []))
    platform_origins_state._origins = ()
    platform_origins_state._refreshed_at = None
    platform_origins_state._refresh_task = None

    async def scenario():
        # Never waits on the database: the stale snapshot is served while the
        # refresh runs.
        assert platform_origins_state.registered_platform_origins() == ()
        await platform_origins_state._refresh_task
        return platform_origins_state.registered_platform_origins()

    assert asyncio.run(scenario()) == ("https://c.example.com",)


@pytest.mark.parametrize("token", ["", "   ", "xtm-default-registration-token"])
def test_registered_platforms_frame_nothing_without_a_chosen_registration_token(
    monkeypatch, platform_origins_state, token
):
    """With no token, or the published default that authenticates nobody,
    anyone reaching /platform/register could add an origin of their own: a
    registration then proves no trust, and no registered origin may frame."""
    platform_origins_state._origins = ("https://attacker.example.com",)
    platform_origins_state._refreshed_at = None
    platform_origins_state._refresh_task = None
    monkeypatch.setattr(settings, "platform_registration_token", token)
    assert platform_origins_state.registered_platform_origins() == ()
    assert platform_origins_state._refresh_task is None


def _counting_refresh(monkeypatch, module) -> list[int]:
    calls: list[int] = []

    async def refresh():
        calls.append(1)
        return module._origins

    monkeypatch.setattr(module, "refresh_registered_platform_origins", refresh)
    return calls


@pytest.mark.parametrize("token", ["", "xtm-default-registration-token"])
def test_a_heartbeat_costs_no_query_while_registered_platforms_cannot_frame(monkeypatch, platform_origins_state, token):
    """Registration is also each platform's heartbeat: while no registered
    origin may frame, re-reading the registrations would serve nothing."""
    import asyncio

    monkeypatch.setattr(settings, "platform_registration_token", token)
    platform_origins_state._origins = ("https://left.example.com",)
    calls = _counting_refresh(monkeypatch, platform_origins_state)
    asyncio.run(platform_origins_state.refresh_for_registration("https://new.example.com"))
    asyncio.run(
        platform_origins_state.refresh_for_registration("https://new.example.com", previous_url="https://left.example.com")
    )
    assert calls == []


def test_a_registration_refreshes_only_for_an_origin_the_snapshot_lacks(monkeypatch, platform_origins_state):
    import asyncio

    platform_origins_state._origins = ("https://known.example.com",)
    calls = _counting_refresh(monkeypatch, platform_origins_state)
    for url in ("https://known.example.com/path", "javascript:alert(1)", None):
        asyncio.run(platform_origins_state.refresh_for_registration(url))
    assert calls == []
    asyncio.run(platform_origins_state.refresh_for_registration("https://new.example.com"))
    assert calls == [1]


def test_a_registration_that_moves_a_platform_off_a_held_origin_refreshes(monkeypatch, platform_origins_state):
    """A heartbeat may move a platform onto an origin the snapshot already
    holds (another platform's): the origin it left must drop out now, not at
    the next background refresh."""
    import asyncio

    platform_origins_state._origins = ("https://left.example.com", "https://shared.example.com")
    calls = _counting_refresh(monkeypatch, platform_origins_state)
    for url, previous in (
        # Another path on the same origin: nothing moved.
        ("https://shared.example.com/b", "https://shared.example.com/a"),
        # A first registration, or an origin the snapshot never held: nothing to drop.
        ("https://shared.example.com", None),
        ("https://shared.example.com", "https://unknown.example.com"),
        ("https://shared.example.com", "javascript:alert(1)"),
    ):
        asyncio.run(platform_origins_state.refresh_for_registration(url, previous_url=previous))
    assert calls == []
    asyncio.run(
        platform_origins_state.refresh_for_registration(
            "https://shared.example.com/b", previous_url="https://left.example.com/x"
        )
    )
    assert calls == [1]


def test_the_registration_route_asks_the_service_whether_to_refresh():
    import inspect

    from app.api.v1 import platform

    source = inspect.getsource(platform.register_platform)
    assert "refresh_for_registration(reg.platform_url, previous_url=previous_platform_url)" in source
    # The stored URL is read before the upsert overwrites it.
    assert source.index("previous_platform_url = reg.platform_url") < source.index(
        "reg.platform_url = body.platform_url"
    )
    assert "registered_platform_origins()" not in source


@pytest.mark.parametrize("route", ["unregister_platform", "purge_registration"])
def test_every_route_that_removes_a_registration_refreshes_the_snapshot_after_its_commit(route):
    """Otherwise the removed platform may still frame the pages until the next background refresh."""
    import inspect

    from app.api.v1 import platform

    source = inspect.getsource(getattr(platform, route))
    commit = source.index("await db.commit()")
    assert source.index("await refresh_registered_platform_origins()", commit) > commit


class _GatedSession(_Session):
    """A session whose read resolves only once ``gate`` is set."""

    def __init__(self, urls, gate):
        super().__init__(urls, [])
        self.gate = gate

    async def execute(self, statement):
        await self.gate.wait()
        return _Rows(self.urls)


def test_an_older_refresh_never_overwrites_a_newer_one(monkeypatch, platform_origins_state):
    """A background read started before an unregistration commits, and
    finishing after the refresh that follows the commit, must not bring the
    unregistered origin back for the next interval."""
    import asyncio

    import app.core.database as database

    async def scenario():
        stale_gate, fresh_gate = asyncio.Event(), asyncio.Event()
        sessions = iter(
            [
                _GatedSession(["https://gone.example.com"], stale_gate),
                _GatedSession(["https://kept.example.com"], fresh_gate),
            ]
        )
        monkeypatch.setattr(database, "async_session", lambda: next(sessions))
        stale = asyncio.create_task(platform_origins_state.refresh_registered_platform_origins())
        await asyncio.sleep(0)
        fresh = asyncio.create_task(platform_origins_state.refresh_registered_platform_origins())
        await asyncio.sleep(0)
        fresh_gate.set()
        await fresh
        stale_gate.set()
        await stale
        return platform_origins_state.registered_platform_origins()

    assert asyncio.run(scenario()) == ("https://kept.example.com",)
