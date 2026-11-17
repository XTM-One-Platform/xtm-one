"""The OpenAEV Ask Ariane lab: [service.lab-openaev-ask-ariane] (xtm-one #11275).

OpenAEV's own front, built from a nested OpenAEV checkout, with no OpenAEV backend: the Ask Ariane proxy
(``/api/xtmone/chat/*``) answers from the OpenCTI lab's fixtures and the session calls from a signed-in lab admin.
These pin the declaration, the routes the panel needs to open and the context usage the composer gauge reads.
"""

from __future__ import annotations

import importlib.util

import pytest

from tests.test_lab_seed_declarations import CHECKS, LAB, _services

SCRIPT = LAB.parent / "openaev_ask_ariane.py"

pytestmark = pytest.mark.skipif(not CHECKS.is_file() or not SCRIPT.is_file(), reason="no .xtm-one next to the backend")


def _module():
    spec = importlib.util.spec_from_file_location("lab_openaev_ask_ariane", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_service_runs_the_script_on_its_own_ports():
    service = _services()["lab-openaev-ask-ariane"]
    module = _module()
    assert service["argv"] == ["python", ".xtm-one/lab/openaev_ask_ariane.py"]
    assert service["image"] == "python-dev" and not service.get("depends_on")
    assert service["port"] == module.PORT and service["ready_path"].startswith(module.CHAT_PREFIX + "/")
    other = _services()["lab-opencti-ask-ariane"]["port"]
    assert len({module.PORT, module.VITE_PORT, other}) == 3


def test_the_ask_ariane_button_shows_and_opens_the_panel():
    """xtmOneAvailability.ts and AskArianeButton.tsx: XTM One connected, AI terms on, Enterprise Edition."""
    module = _module()
    status, settings = module.session_route("GET", "/api/settings", "dark", "3.0.0")
    assert status == 200
    assert settings["platform_xtm_one_configured"] is True
    assert settings["platform_xtm_one_url"].startswith("http")
    assert settings["filigran_chatbot_ai_cgu_status"] == "enabled"
    assert settings["platform_license"]["license_is_validated"] is True


def test_the_session_follows_the_tenant_url_and_the_theme():
    module = _module()
    _, me = module.session_route("GET", f"/api/tenants/{module.TENANT_ID}/me", "light", "")
    assert me["user_email"] == "lab@filigran.io" and me["user_theme"] == "light" and me["user_admin"] is True
    _, tenants = module.session_route("GET", "/api/me/tenants", "dark", "")
    assert [t["tenant_id"] for t in tenants] == [module.TENANT_ID]


def test_an_unknown_call_answers_empty_instead_of_failing():
    module = _module()
    assert module.session_route("GET", "/api/scenarios", "dark", "") == (200, [])
    status, page = module.session_route("POST", "/api/scenarios/search", "dark", "")
    assert status == 200 and page["content"] == [] and page["totalElements"] == 0


def test_the_chat_proxy_answers_the_fixtures_with_the_context_usage():
    module = _module()
    fixtures = module.fixtures
    route = "/chatbot" + "/api/xtmone/chat/agents".removeprefix(module.CHAT_PREFIX)
    status, _, agents = fixtures.chatbot_route("GET", route, {})
    assert status == 200 and agents
    before, after = fixtures.answer_frames("hello", "c1")
    thinking = next(frame for frame in before if frame.get("status") == "thinking")
    assert thinking["context_tokens"] < thinking["context_window"]
    assert after[-1]["type"] == "done" and "context_tokens" in after[-1]


def test_the_pins_override_reads_name_at_version_pairs(monkeypatch, tmp_path):
    module = _module()
    monkeypatch.setenv(module.PINS_ENV, "@filigran/chatbot@3.27.2, bad")
    assert module.requested_pins(tmp_path) == {"@filigran/chatbot": "3.27.2"}
    monkeypatch.delenv(module.PINS_ENV)
    (tmp_path / module.PINS_FILE).write_text('{"@filigran/chatbot": "3.27.2"}', encoding="utf-8")
    assert module.requested_pins(tmp_path) == {"@filigran/chatbot": "3.27.2"}
