"""lab-chatbot renders the served chatbot panel floating, docked and full screen (#11319).

XTM One mounts @filigran/chatbot inline only; the lab copies a lab-only page into its own frontend/ copy
(never the repository's) and logs one signed-in URL per display. The functions are compiled one by one
from lab.py's syntax tree, as test_lab_chatbot_link.py does. dev-docs/reference/agentic-labs.md,
"A @filigran/chatbot branch in the lab".
"""

from __future__ import annotations

import ast
import os
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"
HOST = ROOT / ".xtm-one" / "lab" / "chatbot-host"
DOCS = ROOT / "dev-docs" / "reference" / "agentic-labs.md"

pytestmark = pytest.mark.skipif(not LAB.is_file() or not HOST.is_dir(), reason="no .xtm-one next to the backend")

_NAMES = (
    "CHATBOT_HOST_DIR",
    "CHATBOT_HOST_FILES",
    "CHATBOT_HOST_DISPLAYS",
    "CHATBOT_HOST_PARAMS",
    "install_chatbot_host",
    "chatbot_host_urls",
    "log_chatbot_host_urls",
)


def _lab(checkout: Path, logged: list[str], api) -> dict:
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    body = [
        node
        for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name in _NAMES)
        or (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in _NAMES for t in node.targets))
    ]
    namespace: dict = {
        "Path": Path,
        "os": os,
        "shutil": shutil,
        "CHECKOUT": checkout,
        "WEB_PORT": 3100,
        "log": logged.append,
        "api": api,
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), str(LAB), "exec"), namespace)  # noqa: S102 - the repo's own file
    return namespace


def _checkout(tmp_path: Path) -> Path:
    shutil.copytree(HOST, tmp_path / ".xtm-one" / "lab" / "chatbot-host")
    (tmp_path / "frontend" / "src").mkdir(parents=True)
    return tmp_path


def test_the_page_lands_in_the_lab_frontend_where_vite_serves_it(tmp_path):
    checkout = _checkout(tmp_path)
    _lab(checkout, [], None)["install_chatbot_host"]()
    page = (checkout / "frontend" / "lab-chatbot.html").read_text(encoding="utf-8")
    assert '<script type="module" src="/src/lab-chatbot-host.tsx"></script>' in page
    assert (checkout / "frontend" / "src" / "lab-chatbot-host.tsx").is_file()
    # The review-state parsing the host imports as ./lab-chatbot-params (#11360).
    assert (checkout / "frontend" / "src" / "lab-chatbot-params.ts").is_file()


def test_the_repository_frontend_never_holds_the_page():
    # Lab only: nothing of it reaches the product build.
    assert not (ROOT / "frontend" / "lab-chatbot.html").exists()
    assert not (ROOT / "frontend" / "src" / "lab-chatbot-host.tsx").exists()
    assert not (ROOT / "frontend" / "src" / "lab-chatbot-params.ts").exists()


def test_the_host_mounts_the_panel_through_xtm_one_in_the_display_of_the_url():
    source = (HOST / "lab-chatbot-host.tsx").read_text(encoding="utf-8")
    assert 'import { XtmChatPanel } from "@/components/chat-panel/XtmChatPanel";' in source
    assert "mode={display}" in source
    assert 'new URLSearchParams(search).get("mode")' in source
    # The view switcher writes the display back to the URL, so a reload keeps it.
    assert 'searchWith(window.location.search, "mode", next)' in source


def test_the_page_stores_the_logged_token_as_the_app_session_and_scrubs_it():
    page = (HOST / "lab-chatbot.html").read_text(encoding="utf-8")
    assert 'localStorage.setItem("copilot_token", token)' in page
    assert 'history.replaceState(null, "", window.location.pathname + window.location.search)' in page


def test_one_signed_in_url_per_display_is_logged(tmp_path):
    logged: list[str] = []
    calls: list[tuple] = []

    def api(method, path, body=None, **_):
        calls.append((method, path))
        return {"access_token": "tok"}

    _lab(tmp_path, logged, api)["log_chatbot_host_urls"]()
    assert calls == [("POST", "/auth/login")]
    assert logged == [
        "chatbot display floating: http://localhost:3100/lab-chatbot.html?mode=floating#token=tok",
        "chatbot display sidebar: http://localhost:3100/lab-chatbot.html?mode=sidebar#token=tok",
        "chatbot display fullscreen: http://localhost:3100/lab-chatbot.html?mode=fullscreen#token=tok",
        "chatbot display inline: http://localhost:3100/lab-chatbot.html?mode=inline#token=tok",
        "chatbot display params (add to the query): historyPending=1 welcome=placeholder conversation=ID holdHistory=1"
        " waiting=package waiting=package-nourl quota=1 quotaState=ok|warning|danger|unlimited|global|legacy",
    ]


def test_the_logged_params_are_the_ones_the_page_reads():
    # #11360: lab.py's list and the page's LAB_REVIEW_PARAMS name the same review states, in the same order.
    source = (HOST / "lab-chatbot-params.ts").read_text(encoding="utf-8")
    params = _lab(Path("."), [], None)["CHATBOT_HOST_PARAMS"]
    assert "LAB_REVIEW_PARAMS = [" + ", ".join(f'"{p}"' for p in params) + "] as const;" in source
    host = (HOST / "lab-chatbot-host.tsx").read_text(encoding="utf-8")
    assert 'from "./lab-chatbot-params"' in host


def test_a_refused_sign_in_skips_the_urls_and_keeps_the_lab(tmp_path):
    logged: list[str] = []

    def api(*_, **__):
        raise RuntimeError("401")

    _lab(tmp_path, logged, api)["log_chatbot_host_urls"]()
    assert logged == ["chatbot display host skipped: 401"]


def test_the_docs_name_the_page():
    docs = DOCS.read_text(encoding="utf-8")
    assert "/lab-chatbot.html?mode=" in docs and "chatbot display floating:" in docs
    assert "holdHistory=1" in docs and "chatbot display params" in docs
    assert "waiting=package-nourl" in docs
    assert "quota=1" in docs and "quotaState=" in docs


def test_the_quota_params_give_the_panel_the_route_xtm_one_leaves_off():
    # dev:lab-chatbot-quota-param: the package's quota indicator renders in the lab with no page-side script.
    host = (HOST / "lab-chatbot-host.tsx").read_text(encoding="utf-8")
    assert '{ ...CHAT_PANEL_ENDPOINTS, quota: QUOTA_PATH }' in host
    assert 'const QUOTA_PATH = "/web/chat/quota";' in host
    assert "quotaPayload(state)" in host
