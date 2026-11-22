"""The lab's WhatsApp decision render (#842, S12): every state drawn from the real payload builder.

``.xtm-one/lab/whatsapp_decision_preview.py`` is loaded by path; ``lab_asgi`` answers its two paths before the
application, with a stand-in for ``app.main``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest

from app.services.channel_notices import notice

LAB_DIR = Path(__file__).resolve().parents[2] / ".xtm-one" / "lab"


@pytest.fixture(autouse=True)
def _platform_address(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "frontend_url", "https://xtm.example.com")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def preview():
    return _load("whatsapp_decision_preview_under_test", LAB_DIR / "whatsapp_decision_preview.py")


@pytest.mark.parametrize("language", ["en", "es", "fr", "ja"])
def test_every_state_of_the_brief_renders_in_every_language(preview, language):
    ids = {s["id"] for s in preview.states(language)}
    assert {
        "buttons-2",
        "buttons-3",
        "list-6",
        "text-12",
        "free-text",
        "tool-approval",
        "tool-approval-many",
        "collision",
        "formatting",
        "answered-option",
        "answered-tool",
        "expired",
        "cancelled",
        "confirmation",
        "refusal-not-linked-decision",
        "refusal-decision-refused",
        "refusal-decision-already-answered",
        "refusal-changed",
        "refusal-decision-failed",
    } <= ids
    page = preview.page(f"lang={language}&theme=dark&width=desktop")
    assert f'<html lang="{language}">' in page
    assert notice("card_decision_expired", language) in page
    assert notice("card_choose", language) in page


def test_each_state_is_drawn_as_the_builder_sends_it(preview):
    by_id = {s["id"]: s for s in preview.states("en")}
    kinds = {
        key: preview._message(by_id[key]["view"], "en")[1]
        for key in (
            "buttons-2",
            "buttons-3",
            "list-6",
            "text-12",
            "free-text",
            "tool-approval",
            "tool-approval-many",
            "collision",
            "answered-option",
        )
    }
    assert kinds["buttons-2"]["interactive"]["type"] == "button"
    assert len(kinds["buttons-3"]["interactive"]["action"]["buttons"]) == 3
    assert kinds["list-6"]["interactive"]["type"] == "list"
    assert kinds["collision"]["interactive"]["type"] == "list"
    for key in ("text-12", "free-text", "tool-approval-many", "answered-option"):
        assert kinds[key]["type"] == "text"
    assert kinds["tool-approval"]["interactive"]["type"] == "button"


def test_one_state_theme_and_width_are_chosen_by_the_query(preview):
    page = preview.page(b"state=expired&theme=dark&width=phone&lang=fr")
    assert 'id="expired"' in page and 'id="buttons-2"' not in page
    assert "#0b141a" in page and "width: 390px" in page
    assert 'id="buttons-2"' in preview.page("lang=xx&theme=xx&width=xx")


def test_whatsapp_formatting_is_applied_as_the_client_shows_it(preview):
    assert preview._formatted("Delete *all* in _eu_ ~x~ `c`") == (
        "Delete <b>all</b> in <i>eu</i> <s>x</s> <code>c</code>"
    )
    assert preview._formatted("<script>") == "&lt;script&gt;"


def test_the_lab_answers_its_paths_before_the_application(monkeypatch):
    calls = []

    async def backend(scope, receive, send):
        calls.append(scope["path"])

    monkeypatch.setitem(sys.modules, "app.main", types.SimpleNamespace(application=backend))
    monkeypatch.syspath_prepend(str(LAB_DIR))
    lab_asgi = _load("lab_asgi_whatsapp_under_test", LAB_DIR / "lab_asgi.py")
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b""}

    scope = {"type": "http", "method": "GET", "path": "/api/v1/lab/whatsapp-decision", "query_string": b"lang=ja"}
    asyncio.run(lab_asgi.application(scope, receive, send))
    assert sent[0]["status"] == 200 and (b"content-type", b"text/html; charset=utf-8") in sent[0]["headers"]
    assert b'<html lang="ja">' in sent[1]["body"]
    index = {**scope, "path": "/api/v1/lab/whatsapp-decision/index", "query_string": b""}
    asyncio.run(lab_asgi.application(index, receive, send))
    assert b"lang=fr&theme=dark&width=desktop" in sent[3]["body"]
    other = {**scope, "path": "/api/v1/health"}
    asyncio.run(lab_asgi.application(other, receive, send))
    assert calls == ["/api/v1/health"]
