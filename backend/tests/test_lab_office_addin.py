"""The lab builds and serves the Office add-in task pane signed in (#11109).

lab.py is standard library only and starts the stores when imported, and lab_asgi.py imports the whole
application, so the functions under test are compiled one by one from their syntax trees (as
``test_lab_chatbot_link.py`` does). dev-docs/reference/agentic-labs.md, "The Office add-in in the lab".
"""

from __future__ import annotations

import ast
import asyncio
import os
import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"
ASGI = ROOT / ".xtm-one" / "lab" / "lab_asgi.py"
CHECKS = ROOT / ".xtm-one" / "checks.toml"

pytestmark = pytest.mark.skipif(
    not LAB.is_file() or not ASGI.is_file() or not CHECKS.is_file(), reason="no .xtm-one next to the backend"
)

_LAB_NAMES = (
    "OFFICE_ADDIN_APP",
    "OFFICE_JS_TAG",
    "OFFICE_ADDIN_SESSION",
    "office_addin_enabled",
    "prepare_office_addin_page",
    "build_office_addin",
    "office_addin_url",
)
_ASGI_NAMES = ("OFFICE_ADDIN_PREFIX", "_office_addin_files", "office_addin_scope", "office_addin_files")

#: The task pane page as apps/microsoft-office/index.html builds it (Office.js first, the module last).
BUILT_PAGE = """<!doctype html>
<html lang="en">
  <head>
    <title>Filigran</title>
    <script src="https://appsforoffice.microsoft.com/lib/1.1/hosted/office.js"></script>
    <script type="module" crossorigin src="/office/assets/taskpane.js"></script>
  </head>
  <body><div id="root"></div></body>
</html>
"""


def _compiled(path: Path, names: tuple[str, ...], namespace: dict) -> dict:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    body = [
        node
        for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name in names)
        or (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in node.targets))
        or (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id in names)
    ]
    exec(compile(ast.Module(body=body, type_ignores=[]), str(path), "exec"), namespace)  # noqa: S102 - the repo's own file
    return namespace


def _lab(checkout: Path, logged: list[str], ran: list[tuple[str, list[str], dict]], *, build=None) -> dict:
    def run(name, argv, **kwargs):
        ran.append((name, argv, kwargs))
        if build is not None:
            build(argv)

    return _compiled(
        LAB,
        _LAB_NAMES,
        {
            "Path": Path,
            "os": os,
            "re": re,
            "CHECKOUT": checkout,
            "OFFICE_ADDIN_DIST": checkout / "dist-out",
            "log": logged.append,
            "run": run,
        },
    )


def _app(checkout: Path) -> Path:
    app = checkout / "apps" / "microsoft-office"
    app.mkdir(parents=True)
    (app / "index.html").write_text(BUILT_PAGE, encoding="utf-8")
    (checkout / "frontend" / "node_modules").mkdir(parents=True)
    return app


def test_off_by_default_builds_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("LAB_OFFICE_ADDIN", raising=False)
    _app(tmp_path)
    logged, ran = [], []
    assert _lab(tmp_path, logged, ran)["build_office_addin"](Path("/node/bin/node")) is False
    assert not logged and not ran


def test_a_checkout_without_the_app_is_skipped_with_the_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_OFFICE_ADDIN", "on")
    logged, ran = [], []
    assert _lab(tmp_path, logged, ran)["build_office_addin"](Path("/node/bin/node")) is False
    assert not ran and logged and "no apps/microsoft-office/" in logged[0]


def test_the_build_uses_the_frontend_packages_and_the_page_drops_office_js(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_OFFICE_ADDIN", "on")
    app = _app(tmp_path)
    out = tmp_path / "dist-out"

    def build(argv):
        out.mkdir()
        (out / "index.html").write_text(BUILT_PAGE, encoding="utf-8")

    logged, ran = [], []
    assert _lab(tmp_path, logged, ran, build=build)["build_office_addin"](Path("/node/bin/node")) is True
    ((name, argv, kwargs),) = ran
    assert name == "office-addin-build" and kwargs["cwd"] == app
    assert argv[1].endswith("frontend/node_modules/vite/bin/vite.js") and argv[2] == "build"
    assert argv[argv.index("--outDir") + 1] == str(out)
    # The hosted build: vite.config.ts gives the /office/ base under NODE_ENV=production.
    assert kwargs["env"]["NODE_ENV"] == "production"
    assert (app / "node_modules").resolve() == (tmp_path / "frontend" / "node_modules").resolve()
    page = (out / "index.html").read_text(encoding="utf-8")
    assert "appsforoffice.microsoft.com" not in page
    assert page.index('src="/office/lab-session.js"') < page.index("/office/assets/taskpane.js")
    session = (out / "lab-session.js").read_text(encoding="utf-8")
    # The keys the add-in reads: src/lib/api.ts (token, base URL) and src/hooks/useTheme.ts (theme).
    src = ROOT / "apps" / "microsoft-office" / "src"
    for key, reader in (
        ("filigran_copilot_token", "lib/api.ts"),
        ("filigran_copilot_url", "lib/api.ts"),
        ("filigran_copilot_theme", "hooks/useTheme.ts"),
    ):
        assert key in session
        if (src / reader).is_file():
            assert f'"{key}"' in (src / reader).read_text(encoding="utf-8")
    assert "replaceState" in session


def test_a_failed_build_is_logged_and_never_stops_the_lab(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_OFFICE_ADDIN", "on")
    _app(tmp_path)

    def build(argv):
        raise SystemExit("lab: office-addin-build exited with 1")

    logged, ran = [], []
    assert _lab(tmp_path, logged, ran, build=build)["build_office_addin"](Path("/node/bin/node")) is False
    assert logged == ["office add-in skipped: lab: office-addin-build exited with 1"]


def test_the_logged_url_carries_the_session_and_the_theme(tmp_path):
    lab = _lab(tmp_path, [], [])
    assert lab["office_addin_url"]("http://localhost:3100", "tok", "light") == (
        "http://localhost:3100/office/#token=tok&theme=light"
    )


def _asgi() -> dict:
    return _compiled(ASGI, _ASGI_NAMES, {"Path": Path, "os": os})


def test_the_backend_wrapper_serves_office_only_once_the_build_exists(tmp_path):
    asgi = _asgi()
    scope = {"type": "http", "method": "GET", "path": "/office/"}
    assert asgi["office_addin_scope"](scope, "") is None
    assert asgi["office_addin_scope"](scope, str(tmp_path)) is None  # not built yet: the application answers
    (tmp_path / "index.html").write_text(BUILT_PAGE, encoding="utf-8")
    assert asgi["office_addin_scope"](scope, str(tmp_path))["path"] == "/"
    assert asgi["office_addin_scope"]({**scope, "path": "/office"}, str(tmp_path))["path"] == "/"
    assert asgi["office_addin_scope"]({**scope, "path": "/office/assets/a.js"}, str(tmp_path))["path"] == "/assets/a.js"
    assert asgi["office_addin_scope"]({**scope, "path": "/officers"}, str(tmp_path)) is None
    assert asgi["office_addin_scope"]({**scope, "method": "POST"}, str(tmp_path)) is None


def test_the_static_files_answer_the_page(tmp_path):
    (tmp_path / "index.html").write_text(BUILT_PAGE, encoding="utf-8")
    asgi = _asgi()
    scope = asgi["office_addin_scope"](
        {
            "type": "http",
            "method": "GET",
            "path": "/office/",
            "headers": [],
            "query_string": b"",
            "root_path": "",
        },
        str(tmp_path),
    )
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(asgi["office_addin_files"](str(tmp_path))(scope, receive, send))
    assert sent[0]["status"] == 200
    assert b"/office/assets/taskpane.js" in b"".join(m.get("body", b"") for m in sent[1:])


def test_the_chat_layout_labs_turn_it_on():
    services = tomllib.loads(CHECKS.read_text(encoding="utf-8"))["service"]
    for name in ("lab-chat-layout", "lab-chat-layout-light"):
        assert services[name]["env"]["LAB_OFFICE_ADDIN"] == "on"
        assert "apps/microsoft-office" in services[name]["needs"]
    assert "LAB_OFFICE_ADDIN" not in services["lab"]["env"]


def test_the_preview_proxies_office_to_the_lab_backend():
    config = (ROOT / "frontend" / "vite.config.ts").read_text(encoding="utf-8")
    block = config[config.index('"/office": {') :]
    block = block[: block.index("}")]
    assert "VITE_API_URL" in block and "localhost:8000" not in block
