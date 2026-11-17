"""#63: the xtm-one lab raises the stale build notice natively under ``LAB_STALE_BUILD=on``.

The page build comes from the Vite dev server (``vite.config.ts`` plugin ``lab-page-build-id``), the server build
from ``lab_asgi`` over ``GET /api/v1/auth/config``; these pin the two ids, the rewrite, the wiring and the docs.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types

import pytest

from tests.test_lab_seed_declarations import CHECKS, LAB, _lab_namespace, _lab_source, _services

pytestmark = pytest.mark.skipif(not CHECKS.is_file() or not LAB.is_file(), reason="no .xtm-one next to the backend")

VITE = CHECKS.parents[1] / "frontend" / "vite.config.ts"
DOCS = CHECKS.parents[1] / "dev-docs" / "reference" / "agentic-labs.md"
NAMES = (
    "LAB_STALE_BUILD_STATES",
    "LAB_STALE_PAGE_BUILD_ID",
    "LAB_STALE_SERVER_BUILD_ID",
    "lab_switch",
    "stale_build_ids",
)


def test_the_ids_are_two_different_builds_only_when_switched_on():
    assert _lab_namespace(*NAMES)["stale_build_ids"]() == ("", "")
    page, server = _lab_namespace(*NAMES, env={"LAB_STALE_BUILD": "on"})["stale_build_ids"]()
    assert page and server and page != server
    # The dev server writes only a hexadecimal id.
    assert all(c in "0123456789abcdef" for c in page + server)


def test_the_lab_declares_the_switch_and_wires_both_sides():
    services = _services()
    lab, stale = services["lab"], services["lab-stale-build"]
    assert stale["env"] == {**lab["env"], "LAB_STALE_BUILD": "on"}
    assert "LAB_STALE_BUILD" not in lab["env"]
    assert "'LAB_STALE_BUILD_SERVER_ID': stale_build_ids()[1]" in _lab_source("backend_env")
    assert "VITE_LAB_PAGE_BUILD_ID" in _lab_source("main")
    seed = _lab_source("seed_entities")
    assert "seeded['stale build notice'] = seed_stale_build_notice(token, base)" in seed
    assert "if lab_switch('LAB_STALE_BUILD', LAB_STALE_BUILD_STATES) == 'on':" in seed
    vite = VITE.read_text(encoding="utf-8")
    assert 'apply: "serve"' in vite and "process.env.VITE_LAB_PAGE_BUILD_ID" in vite
    assert '<meta name="build-id"' in vite


def test_the_seed_reads_both_ids_back(monkeypatch):
    page = '<html><head>\n  <meta name="build-id" content="1ab0000000000001" />\n  </head></html>'

    class _Answer:
        def __init__(self, body: str):
            self.body = body.encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return self.body

    served = {"html": page}
    request = types.SimpleNamespace(urlopen=lambda url, timeout: _Answer(served["html"]))
    config = {"build_id": "1ab0000000000002"}
    ns = _lab_namespace(
        *NAMES,
        "seed_stale_build_notice",
        env={"LAB_STALE_BUILD": "on"},
        urllib=types.SimpleNamespace(request=request),
        WEB_PORT=3100,
        api=lambda method, path, token=None, body=None: config,
    )
    seeded = ns["seed_stale_build_notice"]("t", "http://localhost:3100")
    assert seeded == {"id": "page 1ab0000000000001, server 1ab0000000000002", "url": "http://localhost:3100/"}
    config["build_id"] = ""
    with pytest.raises(RuntimeError, match="answers build_id"):
        ns["seed_stale_build_notice"]("t", "http://localhost:3100")
    config["build_id"] = "1ab0000000000002"
    served["html"] = "<html><head></head></html>"
    with pytest.raises(RuntimeError, match="carries no"):
        ns["seed_stale_build_notice"]("t", "http://localhost:3100")


def _lab_asgi(monkeypatch):
    async def backend(scope, receive, send):
        body = json.dumps({"build_id": "", "sso_enabled": False}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json"), (b"content-length", b"1")],
            }
        )
        await send({"type": "http.response.body", "body": body})

    lab_dir = CHECKS.parent / "lab"
    monkeypatch.setitem(sys.modules, "app.main", types.SimpleNamespace(application=backend))
    monkeypatch.syspath_prepend(str(lab_dir))
    spec = importlib.util.spec_from_file_location("lab_asgi_stale_build_under_test", lab_dir / "lab_asgi.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_lab_asgi_answers_the_server_build_on_auth_config_only(monkeypatch):
    module = _lab_asgi(monkeypatch)
    scope = {"type": "http", "method": "GET", "path": "/api/v1/auth/config"}
    on = {"LAB_STALE_BUILD_SERVER_ID": "1ab0000000000002"}
    assert module.asks_stale_build(scope, {}) == ""
    assert module.asks_stale_build(scope, {"LAB_STALE_BUILD_SERVER_ID": ""}) == ""
    assert module.asks_stale_build({**scope, "path": "/api/v1/auth/me"}, on) == ""
    assert module.asks_stale_build({**scope, "method": "POST"}, on) == ""
    assert module.asks_stale_build(scope, on) == "1ab0000000000002"
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b""}

    asyncio.run(module.answer_stale_build(scope, receive, send, "1ab0000000000002"))
    body = json.loads(sent[1]["body"])
    assert body == {"build_id": "1ab0000000000002", "sso_enabled": False}
    assert dict(sent[0]["headers"])[b"content-length"] == str(len(sent[1]["body"])).encode()
    assert module.with_build_id(b"not json", "x") == b"not json"


def test_the_switch_is_documented():
    docs = DOCS.read_text(encoding="utf-8")
    assert "[service.lab-stale-build]" in docs and "LAB_STALE_BUILD" in docs
    assert "seeded stale build notice" in docs
