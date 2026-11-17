"""The OpenCTI Ask Ariane lab: [service.lab-opencti-ask-ariane] (chat-layout program, xtm-one #10910).

OpenCTI's own front, built from a nested OpenCTI checkout, next to the lab's OpenCTI, with the Ask Ariane
panel's ``/chatbot/*`` answered from fixtures. These pin the declaration and the fixture routes.
"""

from __future__ import annotations

import importlib.util
import json
import re

import pytest

from app.services.sandbox_workspace import checks
from tests.test_lab_seed_declarations import CHECKS, LAB, _services

SCRIPT = LAB.parent / "opencti_ask_ariane.py"
OPENCTI_KEYS = {"opencti", "opencti-search", "opencti-broker", "opencti-redis", "opencti-s3"}

pytestmark = pytest.mark.skipif(not CHECKS.is_file() or not SCRIPT.is_file(), reason="no .xtm-one next to the backend")


def _module():
    spec = importlib.util.spec_from_file_location("lab_opencti_ask_ariane", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_service_runs_the_script_next_to_the_lab_opencti():
    service = _services()["lab-opencti-ask-ariane"]
    assert service["argv"] == ["python", ".xtm-one/lab/opencti_ask_ariane.py"]
    assert service["image"] == "python-dev" and service["depends_on"] == ["lab-opencti"]
    module = _module()
    assert service["port"] == module.PORT and service["ready_path"] == "/chatbot/config"
    specs = checks.load_services(CHECKS.read_text(encoding="utf-8"))
    chain = checks.service_chain(specs, "lab-opencti-ask-ariane", own_images=OPENCTI_KEYS)
    ports = [spec.port for spec in chain]
    assert module.VITE_PORT not in ports and len(set(ports)) == len(ports)


def test_the_script_signs_in_with_the_lab_opencti_admin():
    module = _module()
    env = _services()["lab-opencti"]["env"]
    assert env["APP__ADMIN__TOKEN"] == module.OPENCTI_ADMIN_TOKEN
    assert module.OPENCTI_HEALTH.endswith(env["APP__HEALTH_ACCESS_KEY"])


def test_the_panel_is_configured_and_lists_the_seeded_conversations():
    module = _module()
    status, _, config = module.chatbot_route("GET", "/chatbot/config", {})
    assert status == 200 and config["xtm_one_configured"] is True
    _, _, agents = module.chatbot_route("GET", "/chatbot/agents?limit=50", {})
    assert {a["slug"] for a in agents} == {"ctem-assistant", "opencti-assistant"}
    _, _, sessions = module.chatbot_route("GET", "/chatbot/sessions", {})
    ids = [c["conversation_id"] for c in sessions["conversations"]]
    assert ids == [c["conversation_id"] for c in module.CONVERSATIONS]
    _, _, thread = module.chatbot_route("POST", "/chatbot/sessions", {"conversation_id": ids[0]})
    text = "\n".join(m["content"] for m in thread["messages"])
    # The long thread carries what the UX round checks: a table, a list, a code block, a citation.
    assert "| Stage |" in text and "\n1. " in text and "```kql" in text and "[1]" in text
    assert module.chatbot_route("GET", "/chatbot/unknown", {})[0] == 404


def test_a_turn_streams_the_answer_or_ends_on_an_error():
    module = _module()
    _, after = module.answer_frames("Summarise the campaign", "c1")
    assert after[-1]["type"] == "done" and after[-1]["content"] == module.LONG_ANSWER
    assert "".join(f["content"] for f in after if f["type"] == "stream") == module.LONG_ANSWER
    _, failed = module.answer_frames("show me an error", "c1")
    assert [f["type"] for f in failed] == ["error"]
    frame = module.sse([{"type": "status", "status": "streaming"}])[0]
    assert frame.startswith(b"data: ") and json.loads(frame[6:].decode()) == {"type": "status", "status": "streaming"}
    assert "tool_call_trace" not in after[-1] and "reasoning" not in after[-1]


def test_a_turn_holding_the_tools_keyword_calls_a_tool_with_reasoning():
    module = _module()
    _, after = module.answer_frames(f"Which loader indicators? Use {module.TOOLS_KEYWORD.upper()}", "c1")
    statuses = [f.get("status") for f in after if f["type"] == "status"]
    assert statuses[:3] == ["tool_start", "tool_done", "thinking"]
    assert after[0]["tools"] == [module.TOOL_NAME]
    done = after[-1]
    assert done["type"] == "done" and done["content"] == module.LONG_ANSWER
    assert done["tool_names"] == [module.TOOL_NAME] and done["reasoning"].strip()
    [trace] = done["tool_call_trace"]
    assert trace["name"] == module.TOOL_NAME and trace["success"] is True
    assert json.loads(trace["input"]) == module.TOOL_INPUT
    # A long JSON output, so the dialog's OUTPUT block scrolls and wraps as a real search result does.
    assert json.loads(trace["output"]) == module.TOOL_OUTPUT and len(trace["output"]) > 2000


def test_the_thinking_and_done_frames_carry_the_context_usage_the_gauge_reads():
    module = _module()
    before, after = module.answer_frames("Hello", "c1")
    for frame in (before[-1], after[-1]):
        assert frame["type"] in {"status", "done"}
        used, window = frame["context_tokens"], frame["context_window"]
        assert 0 < used < window and used / window < 0.95
        breakdown = frame["context_breakdown"]
        assert set(breakdown) == {"system", "tools", "conversation"} and all(v > 0 for v in breakdown.values())
        assert sum(breakdown.values()) == used
    assert before[-1]["status"] == "thinking"
    _, full = module.answer_frames("Make the context full", "c1")
    assert full[-1]["context_tokens"] / full[-1]["context_window"] > 0.95


def _markers(text: str) -> list[str]:
    return re.findall(r"\[\[(FILE|PRESENTATION|REPORT|VIDEO):[0-9a-f-]{36}\]\]", text)


def test_a_turn_holding_the_files_keyword_answers_with_file_and_artifact_cards():
    module = _module()
    _, after = module.answer_frames(f"Send me the {module.FILES_KEYWORD.upper()} please", "c1")
    done = after[-1]
    assert done["type"] == "done" and done["content"] == module.FILES_ANSWER
    assert "".join(f["content"] for f in after if f["type"] == "stream") == module.FILES_ANSWER
    lines = module.FILES_ANSWER.splitlines()
    # The markers stand where a card round checks them: a table row, a list item, a quote, alone on a line.
    assert any(line.startswith("| ") and "[[FILE:" in line for line in lines)
    assert any(line.startswith("- ") and "[[FILE:" in line for line in lines)
    assert any(line.startswith("> ") and "[[FILE:" in line for line in lines)
    assert any(re.fullmatch(r"\[\[VIDEO:[0-9a-f-]{36}\]\]", line) for line in lines)
    assert set(_markers(done["content"])) == {"FILE", "PRESENTATION", "REPORT", "VIDEO"}
    assert {a["file_tag"] for a in done["attachments"]} == {"download_file", "working_file"}
    for att in done["attachments"]:
        assert f"[[FILE:{att['file_id']}]]" in done["content"] and att["size"] > 0
    assert {a["kind"] for a in done["artifacts"]} == {"presentation", "report", "video"}
    for art in done["artifacts"]:
        assert f"[[{art['kind'].upper()}:{art['id']}]]" in done["content"] and art["downloads"]
    assert next(a for a in done["artifacts"] if a["kind"] == "video")["status"] == "ready"
    # Another turn keeps its answer: the keyword is a word, "profiles" does not hold it.
    _, plain = module.answer_frames("Show the threat actor profiles", "c1")
    assert plain[-1]["content"] == module.LONG_ANSWER and "attachments" not in plain[-1]


def test_the_seeded_files_conversation_restores_the_cards_and_the_files_download():
    module = _module()
    _, _, sessions = module.chatbot_route("GET", "/chatbot/sessions", {})
    seeded = next(c for c in sessions["conversations"] if c["title"] == "Files, decks and reports")
    _, _, thread = module.chatbot_route("POST", "/chatbot/sessions", {"conversation_id": seeded["conversation_id"]})
    answer = thread["messages"][-1]
    assert answer["role"] == "assistant" and answer["content"] == module.FILES_ANSWER
    assert answer["attachments"] == module.FILES_ATTACHMENTS and answer["artifacts"] == module.FILES_ARTIFACTS
    for att in answer["attachments"]:
        status, ctype, served = module.chatbot_route("GET", f"/chatbot/files/{att['file_id']}/download", {})
        assert status == 200 and ctype == att["content_type"]
        assert isinstance(served, module.StubFile) and len(served.data) == att["size"]
    assert module.chatbot_route("GET", "/chatbot/files/unknown/download", {})[0] == 404
    for art in answer["artifacts"]:
        path = f"/chatbot/artifacts/{art['kind']}/{art['id']}"
        assert module.chatbot_route("GET", path, {}) == (200, "application/json", art)
        status, ctype, served = module.chatbot_route("POST", f"{path}/download", {"format": art["downloads"][0]})
        assert status == 200 and ctype == served.content_type and served.data
        assert module.chatbot_route("POST", f"{path}/download", {"format": "docx"})[0] == 404
    assert module.STUB_PDF.startswith(b"%PDF-") and b"ftyp" in module.STUB_MP4


def _parse_quota(value: object) -> dict | None:
    """@filigran/chatbot's parseQuota (src/utils/quota.ts), stricter: None unless used is a number, and here the
    period and scope must be set too, so the read is the one the indicator words "N agentic actions today"."""
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("used"), int)
        or not isinstance(value.get("period"), str)
    ):
        return None
    if value.get("scope") not in {"user", "global"} or not (
        value.get("limit") is None or isinstance(value["limit"], int)
    ):
        return None
    return value


def test_the_quota_read_renders_the_indicator_and_the_keyword_moves_it_near_the_cap():
    module = _module()
    module.QUOTA_STATE["near_cap"] = False
    status, ctype, quota = module.chatbot_route("GET", "/chatbot/quota", {})
    assert status == 200 and ctype == "application/json" and _parse_quota(quota) is not None
    assert (quota["used"], quota["limit"], quota["period"]) == (*module.QUOTA_TODAY, "daily")
    assert [p["period"] for p in quota["periods"]] == ["daily", "monthly", "yearly"]
    for row in quota["periods"]:
        assert _parse_quota(row) is not None and row["used"] <= row["limit"]
        assert row["resets_at"].endswith("T00:00:00Z")
    # 31 Dec 2026 at noon UTC: every counter starts again on 1 Jan 2027.
    dated = module.quota_status(now=1798718400)
    assert {p["resets_at"] for p in dated["periods"]} == {"2027-01-01T00:00:00Z"}
    module.chatbot_route("POST", "/chatbot/messages", {"content": "How much QUOTA is left?"})
    _, _, near = module.chatbot_route("GET", "/chatbot/quota", {})
    assert (near["used"], near["limit"]) == module.QUOTA_NEAR_CAP and near["used"] / near["limit"] >= 0.8


def _front(tmp_path):
    front = tmp_path / "opencti-front"
    front.mkdir()
    manifest = {
        "name": "opencti-front",
        "dependencies": {"@filigran/chatbot": "3.19.1", "@filigran/design-system": "1.3.0"},
    }
    (front / "package.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return front


def test_the_pins_file_rewrites_the_front_pins_without_a_hand_edit(tmp_path, monkeypatch):
    module = _module()
    monkeypatch.delenv(module.PINS_ENV, raising=False)
    assert module.requested_pins(tmp_path) == {}
    pins = {"@filigran/chatbot": "3.25.0", "@filigran/design-system": "1.5.0"}
    (tmp_path / module.PINS_FILE).write_text(json.dumps(pins), encoding="utf-8")
    front = _front(tmp_path)
    changes = module.override_pins(front, module.requested_pins(tmp_path))
    assert changes == ["@filigran/chatbot 3.19.1 -> 3.25.0", "@filigran/design-system 1.3.0 -> 1.5.0"]
    assert json.loads((front / "package.json").read_text(encoding="utf-8"))["dependencies"] == pins
    # Already at the named versions: nothing changes, so the install stays immutable.
    assert module.override_pins(front, pins) == []


def test_the_pins_variable_wins_and_bad_pins_are_ignored(tmp_path, monkeypatch):
    module = _module()
    (tmp_path / module.PINS_FILE).write_text(json.dumps({"@filigran/chatbot": "9.9.9"}), encoding="utf-8")
    monkeypatch.setenv(
        module.PINS_ENV,
        "@filigran/chatbot@3.25.0-rc.1, @filigran/unknown@1.0.0, @filigran/design-system@^1.5.0, nonsense",
    )
    pins = module.requested_pins(tmp_path)
    assert pins == {
        "@filigran/chatbot": "3.25.0-rc.1",
        "@filigran/unknown": "1.0.0",
        "@filigran/design-system": "^1.5.0",
    }
    front = _front(tmp_path)
    # A package the front lacks is never added, a range is never written: only the exact chatbot pin lands.
    assert module.override_pins(front, pins) == ["@filigran/chatbot 3.19.1 -> 3.25.0-rc.1"]
    dependencies = json.loads((front / "package.json").read_text(encoding="utf-8"))["dependencies"]
    assert dependencies == {"@filigran/chatbot": "3.25.0-rc.1", "@filigran/design-system": "1.3.0"}


def test_a_malformed_pins_file_stops_the_start(tmp_path, monkeypatch):
    module = _module()
    monkeypatch.delenv(module.PINS_ENV, raising=False)
    (tmp_path / module.PINS_FILE).write_text('["3.25.0"]', encoding="utf-8")
    with pytest.raises(SystemExit, match="object of package name to version"):
        module.requested_pins(tmp_path)


def _tree(root, files):
    for relative, text in files.items():
        path = root / "opencti-platform" / "opencti-front" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def test_the_host_overlay_copies_master_s_panel_and_themes_over_the_tag_front(tmp_path, monkeypatch):
    module = _module()
    monkeypatch.delenv(module.HOST_PATHS_ENV, raising=False)
    tag, host = tmp_path / "opencti", tmp_path / "opencti-host"
    _tree(
        tag,
        {
            "package.json": '{"version": "7.261106.0"}',
            "src/components/ThemeDark.ts": "export const brand = '#42caff';\n",
            "src/private/components/chatbox/Old.tsx": "export {};\n",
            "src/utils/hooks.ts": "export {};\n",
        },
    )
    _tree(
        host,
        {
            "src/components/ThemeDark.ts": "export const brand = 'palette.ai.main';\n",
            "src/private/components/chatbox/AskArianePanel.tsx": (
                "import { x } from '../../../utils/hooks';\nimport y from './Missing';\nimport z from '@filigran/chatbot';\n"
            ),
            "src/components/Elsewhere.ts": "not named by the overlay\n",
        },
    )
    copied, unresolved = module.overlay_host(tag, host, module.host_paths())
    assert copied == ["src/private/components/chatbox/AskArianePanel.tsx", "src/components/ThemeDark.ts"]
    front = tag / "opencti-platform" / "opencti-front"
    assert "palette.ai.main" in (front / "src/components/ThemeDark.ts").read_text(encoding="utf-8")
    # A tag file the host ref lacks stays, a host file outside the paths is not copied.
    assert (front / "src/private/components/chatbox/Old.tsx").is_file()
    assert not (front / "src/components/Elsewhere.ts").exists()
    # Only the relative import the tag front cannot resolve is reported; a package import is never checked.
    assert unresolved == ["src/private/components/chatbox/AskArianePanel.tsx imports ./Missing"]


def test_a_declaration_file_resolves_a_relative_import_of_the_overlay(tmp_path, monkeypatch):
    module = _module()
    monkeypatch.delenv(module.HOST_PATHS_ENV, raising=False)
    tag, host = tmp_path / "opencti", tmp_path / "opencti-host"
    _tree(
        tag,
        {
            "package.json": '{"version": "7.261106.0"}',
            "src/components/Theme.d.ts": "export interface Theme {}\n",
            "src/components/types/index.d.ts": "export {};\n",
        },
    )
    _tree(
        host,
        {
            "src/components/ThemeDark.ts": (
                "import type { Theme } from './Theme';\nimport t from './types';\nimport g from './Gone';\n"
            ),
        },
    )
    _, unresolved = module.overlay_host(tag, host, module.host_paths())
    # Theme.d.ts and types/index.d.ts resolve their imports; a truly missing file is still reported.
    assert unresolved == ["src/components/ThemeDark.ts imports ./Gone"]


def test_the_host_paths_variable_wins_and_stays_under_the_front(monkeypatch):
    module = _module()
    monkeypatch.setenv(module.HOST_PATHS_ENV, " src/components/ThemeLight.ts , /src/private/components/chatbox/ ,")
    assert module.host_paths() == ["src/components/ThemeLight.ts", "src/private/components/chatbox"]
    monkeypatch.setenv(module.HOST_PATHS_ENV, "src/../../package.json")
    with pytest.raises(SystemExit, match="must be a path under opencti-front"):
        module.host_paths()


def test_the_start_log_names_the_host_ref_of_the_overlay(tmp_path, monkeypatch):
    module = _module()
    logged = []
    monkeypatch.setattr(module.lab, "CHECKOUT", tmp_path)
    monkeypatch.setattr(module.lab, "log", logged.append)
    monkeypatch.delenv(module.HOST_CHECKOUT_ENV, raising=False)
    monkeypatch.delenv(module.HOST_PATHS_ENV, raising=False)
    tag = tmp_path / "opencti"
    _tree(tag, {"package.json": '{"version": "7.261106.0"}'})
    # No host checkout: nothing is overlaid and nothing is logged.
    assert module.apply_host_overlay(tag) is None and logged == []
    host = tmp_path / "opencti-host"
    _tree(host, {"src/components/ThemeDark.ts": "export {};\n"})
    record = {"repository": "OpenCTI-Platform/opencti", "ref": "master", "commit": "cd6e0a81" + "0" * 32}
    (host / ".xtm-checkout.json").write_text(json.dumps(record), encoding="utf-8")
    origin = module.apply_host_overlay(tag)
    assert origin == "OpenCTI-Platform/opencti@cd6e0a810000 (master)"
    assert any(line.startswith("ask-ariane: host paths absent from OpenCTI-Platform/opencti@") for line in logged)
    assert any(f"host files from {origin} overlaid on the 7.261106.0 front: 1 file(s)" in line for line in logged)
