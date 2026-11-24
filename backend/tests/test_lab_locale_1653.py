"""The xtm-one lab's language switch (#1653): ``/api/v1/lab/locale/<lang>`` stores the language and redirects.

``.xtm-one/lab/lab_locale.py`` is loaded by path; the database write is replaced by a recorder.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

from app.core.utils import SUPPORTED_LANGUAGES

LAB_DIR = Path(__file__).resolve().parents[2] / ".xtm-one" / "lab"


@pytest.fixture
def lab_locale():
    spec = importlib.util.spec_from_file_location("lab_locale_under_test", LAB_DIR / "lab_locale.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(lab_locale, path: str, query: bytes = b"", method: str = "GET"):
    scope = {"type": "http", "method": method, "path": path, "query_string": query}
    raw = lab_locale.asks_locale(scope)
    if raw is None:
        return None, [], []
    stored: list[str] = []
    sent: list[dict] = []

    async def store(language: str) -> int:
        stored.append(language)
        return 2

    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(lab_locale.answer(scope, send, raw, store=store))
    return raw, stored, sent


def test_every_short_code_names_a_language_the_platform_ships(lab_locale):
    shipped = {value for key, value in lab_locale.LOCALES.items() if key != "default"}
    assert shipped == set(SUPPORTED_LANGUAGES)
    assert lab_locale.resolve("default") == "auto"
    assert lab_locale.resolve("FR") == "fr-fr"
    assert lab_locale.resolve("ja-jp") == "ja-jp"
    assert lab_locale.resolve("de") is None


def test_the_switch_stores_the_language_and_redirects_to_next(lab_locale):
    raw, stored, sent = _run(lab_locale, "/api/v1/lab/locale/fr", b"next=%2Fctem%3Ftab%3Dcases")
    assert raw == "fr"
    assert stored == ["fr-fr"]
    start = sent[0]
    assert start["status"] == 302
    assert (b"location", b"/ctem?tab=cases") in start["headers"]


def test_default_clears_the_language_and_lands_on_the_root(lab_locale):
    _, stored, sent = _run(lab_locale, "/api/v1/lab/locale/default")
    assert stored == ["auto"]
    assert (b"location", b"/") in sent[0]["headers"]


@pytest.mark.parametrize("target", ["https://evil.example", "//evil.example/x", "ctem", "/\\evil"])
def test_next_outside_the_lab_lands_on_the_root(lab_locale, target):
    assert lab_locale.safe_next(f"next={target}".encode()) == "/"


def test_an_unknown_language_stores_nothing_and_names_the_choices(lab_locale):
    _, stored, sent = _run(lab_locale, "/api/v1/lab/locale/de")
    assert stored == []
    assert sent[0]["status"] == 404
    assert b"en, fr, es, ja, default" in sent[1]["body"]


def test_other_paths_and_methods_go_to_the_application(lab_locale):
    assert lab_locale.asks_locale({"type": "http", "method": "GET", "path": "/api/v1/lab/whatsapp-decision"}) is None
    assert lab_locale.asks_locale({"type": "http", "method": "POST", "path": "/api/v1/lab/locale/fr"}) is None
    assert lab_locale.asks_locale({"type": "websocket", "path": "/api/v1/lab/locale/fr"}) is None
