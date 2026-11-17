"""The OpenCRQ Ask Ariane lab: [service.lab-opencrq-ask-ariane] (xtm-one #11275).

OpenCRQ's own front, built from a nested OpenCRQ checkout, with no OpenCRQ backend: the Ask Ariane proxy
(``/api/xtm-one/chat/*``) answers from the OpenCTI lab's fixtures and the shell's tRPC calls from a signed-in lab
admin. These pin the declaration, the procedures the launcher needs to show and the tRPC wire formats.
"""

from __future__ import annotations

import importlib.util
import json

import pytest

from tests.test_lab_seed_declarations import CHECKS, LAB, _services

SCRIPT = LAB.parent / "opencrq_ask_ariane.py"

pytestmark = pytest.mark.skipif(not CHECKS.is_file() or not SCRIPT.is_file(), reason="no .xtm-one next to the backend")


def _module():
    spec = importlib.util.spec_from_file_location("lab_opencrq_ask_ariane", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_service_runs_the_script_on_its_own_ports():
    service = _services()["lab-opencrq-ask-ariane"]
    module = _module()
    assert service["argv"] == ["python", ".xtm-one/lab/opencrq_ask_ariane.py"]
    assert service["image"] == "python-dev" and not service.get("depends_on")
    assert service["port"] == module.PORT and service["ready_path"].startswith(module.CHAT_PREFIX + "/")
    others = {_services()[name]["port"] for name in ("lab-opencti-ask-ariane", "lab-openaev-ask-ariane")}
    assert len({module.PORT, module.VITE_PORT} | others) == 4


def test_the_sub_paths_carry_the_root_tsconfigs_and_the_declaration_names_them_all():
    """front/tsconfig.json extends ../tsconfig.base.json: without it Vite answers TSCONFIG_ERROR and nothing renders."""
    module = _module()
    assert {"tsconfig.base.json", "tsconfig.json", "package.json", "yarn.lock", "front"} <= set(
        module.OPENCRQ_SUB_PATHS
    )
    comment = " ".join(
        CHECKS.read_text(encoding="utf-8").split("[service.lab-opencrq-ask-ariane]")[0].splitlines()[-12:]
    )
    for sub_path in module.OPENCRQ_SUB_PATHS:
        assert f'"{sub_path}"' in comment, sub_path


def test_the_start_refuses_a_checkout_without_the_root_tsconfig(monkeypatch, tmp_path):
    module = _module()
    where = tmp_path / "opencrq"
    for sub_path in module.OPENCRQ_SUB_PATHS:
        target = where / sub_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if "." in sub_path.rsplit("/", 1)[-1]:
            target.write_text("{}", encoding="utf-8")
        else:
            target.mkdir()
    monkeypatch.setattr(module.lab, "CHECKOUT", tmp_path)
    monkeypatch.delenv("LAB_OPENCRQ_CHECKOUT", raising=False)
    assert module.opencrq_checkout() == where
    (where / "tsconfig.base.json").unlink()
    assert module.missing_sub_paths(where) == ["tsconfig.base.json"]
    with pytest.raises(SystemExit) as refused:
        module.opencrq_checkout()
    assert "lacks tsconfig.base.json" in str(refused.value)


def test_the_ask_ariane_launcher_shows_and_opens_the_panel():
    """ask-ariane.tsx: XTM One configured, Enterprise Edition on authStatus, a signed-in user on a tenant."""
    module = _module()
    assert module.procedure_answer("xtmOne.getSettings", "dark")["configured"] is True
    status = module.procedure_answer("authStatus", "dark")
    assert status["initialized"] is True and status["edition"] == "ee"
    assert module.procedure_answer("isLoggedIn", "dark")["email"] == "lab@filigran.io"
    assert module.procedure_answer("tenants.getTenants", "dark") == [module.TENANT]
    assert module.procedure_answer("users.getUserTheme", "light") == "light"


def test_an_unknown_procedure_answers_empty_instead_of_failing():
    module = _module()
    assert module.procedure_answer("riskScenarios.getRiskScenarios", "dark") == []
    assert module.procedure_answer("findings.countFindings", "dark") == 0
    assert module.procedure_answer("dashboards.getDashboard", "dark") is None


def test_the_trpc_answers_follow_the_calling_link():
    module = _module()
    target = "/trpc/authStatus,isLoggedIn?batch=1&input=%7B%7D"
    ctype, body = module.trpc_response("GET", target, "application/jsonl", "dark")
    assert ctype == "application/jsonl" and body.endswith(b"\n")
    head = json.loads(body)
    assert head["0"] == [[{"result": {"data": module.procedure_answer("authStatus", "dark")}}]]
    assert head["1"][0][0]["result"]["data"]["id"] == module.USER_ID
    ctype, body = module.trpc_response("POST", "/trpc/signOut?batch=1", "", "dark")
    assert ctype == "application/json" and json.loads(body) == [{"result": {"data": None}}]
    _, body = module.trpc_response("GET", "/trpc/health", "", "dark")
    assert json.loads(body) == {"result": {"data": {"status": "ok"}}}


def test_the_chat_proxy_answers_the_fixtures_with_the_context_usage():
    module = _module()
    fixtures = module.fixtures
    route = "/chatbot" + "/api/xtm-one/chat/agents".removeprefix(module.CHAT_PREFIX)
    status, _, agents = fixtures.chatbot_route("GET", route, {})
    assert status == 200 and agents
    before, after = fixtures.answer_frames("hello", "c1")
    thinking = next(frame for frame in before if frame.get("status") == "thinking")
    assert thinking["context_tokens"] < thinking["context_window"]
    assert after[-1]["type"] == "done" and "context_tokens" in after[-1]


def test_the_pins_override_reads_its_own_variable_and_file(monkeypatch, tmp_path):
    module = _module()
    monkeypatch.setenv(module.PINS_ENV, "@filigran/chatbot@3.27.2")
    assert module.product.requested_pins(tmp_path, module.PINS_ENV, module.PINS_FILE) == {"@filigran/chatbot": "3.27.2"}
    monkeypatch.delenv(module.PINS_ENV)
    (tmp_path / module.PINS_FILE).write_text('{"@filigran/chatbot": "3.27.1"}', encoding="utf-8")
    assert module.product.requested_pins(tmp_path, module.PINS_ENV, module.PINS_FILE) == {"@filigran/chatbot": "3.27.1"}
