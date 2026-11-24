"""The xtm-one lab's theme switch (#1848): ``/api/v1/lab/theme/<light|dark>`` stores the theme and redirects.

``.xtm-one/lab/lab_locale.py`` is loaded by path; the database write is replaced by a recorder.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

LAB_DIR = Path(__file__).resolve().parents[2] / ".xtm-one" / "lab"


@pytest.fixture
def lab_locale():
    spec = importlib.util.spec_from_file_location("lab_theme_under_test", LAB_DIR / "lab_locale.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(lab_locale, path: str, query: bytes = b"", method: str = "GET"):
    scope = {"type": "http", "method": method, "path": path, "query_string": query}
    raw = lab_locale.asks_theme(scope)
    if raw is None:
        return None, [], []
    stored: list[str] = []
    sent: list[dict] = []

    async def store(theme: str) -> int:
        stored.append(theme)
        return 2

    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(lab_locale.answer_theme(scope, send, raw, store=store))
    return raw, stored, sent


def test_the_themes_are_the_values_the_preference_keeps(lab_locale):
    assert set(lab_locale.THEMES) == {"light", "dark"}
    assert lab_locale.resolve_theme("Light") == "light"
    assert lab_locale.resolve_theme("dark/") == "dark"
    assert lab_locale.resolve_theme("auto") is None


def test_the_switch_stores_the_theme_and_redirects_to_next(lab_locale):
    raw, stored, sent = _run(lab_locale, "/api/v1/lab/theme/light", b"next=%2Fctem%3Ftab%3Dcases")
    assert raw == "light"
    assert stored == ["light"]
    assert sent[0]["status"] == 302
    assert (b"location", b"/ctem?tab=cases") in sent[0]["headers"]


def test_an_off_origin_next_lands_on_the_root(lab_locale):
    _, stored, sent = _run(lab_locale, "/api/v1/lab/theme/dark", b"next=https%3A%2F%2Fevil.example")
    assert stored == ["dark"]
    assert (b"location", b"/") in sent[0]["headers"]


def test_an_unknown_theme_stores_nothing_and_names_the_choices(lab_locale):
    _, stored, sent = _run(lab_locale, "/api/v1/lab/theme/pastel")
    assert stored == []
    assert sent[0]["status"] == 404
    assert b"light, dark" in sent[1]["body"]


def test_other_paths_and_methods_go_to_the_application(lab_locale):
    assert lab_locale.asks_theme({"type": "http", "method": "GET", "path": "/api/v1/lab/locale/fr"}) is None
    assert lab_locale.asks_theme({"type": "http", "method": "POST", "path": "/api/v1/lab/theme/light"}) is None
    assert lab_locale.asks_theme({"type": "websocket", "path": "/api/v1/lab/theme/light"}) is None
