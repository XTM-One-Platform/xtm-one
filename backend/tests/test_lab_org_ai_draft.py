"""The lab's stub model answers Draft with AI of Settings > Organization, and lab_asgi its 429 (#9632).

lab.py is standard library only and starts the stores when imported, so its functions are compiled
one by one from its syntax tree (as ``test_lab_doc_draft.py`` does). The request is built with the
backend's own ``build_messages`` and the answer parsed with its own ``parse_proposals``: a change of
the drafter's prompt that the stub no longer recognises fails here, not in a lab.
dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

from app.services.organization_context import parse_profile
from app.services.organization_context_ai_draft import Evidence, build_messages, parse_proposals

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"

_NAMES = ("org_ai_draft_answer", "last_user_text", "LAB_ORG_AI_DRAFT_SYSTEM", "LAB_ORG_PROFILE")


def _lab_namespace() -> dict:
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    body = [
        node
        for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name in _NAMES)
        or (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in _NAMES for t in node.targets))
    ]
    namespace: dict = {"json": json}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(LAB), "exec"), namespace)  # noqa: S102 - the repo's own file
    return namespace


def _evidence(**texts: str) -> Evidence:
    evidence = Evidence()
    evidence.texts.update(texts or {"description": "A lab company."})
    return evidence


def _request(current: dict, evidence: Evidence) -> dict:
    messages = build_messages(parse_profile(current), evidence)
    return {"model": "lab-model", "messages": [{"role": m.role, "content": m.content} for m in messages]}


def test_the_stub_answers_every_review_state_the_parser_keeps():
    lab = _lab_namespace()
    current = parse_profile(lab["LAB_ORG_PROFILE"])
    evidence = _evidence()
    answer = lab["org_ai_draft_answer"](_request(lab["LAB_ORG_PROFILE"], evidence))
    proposals, not_supported, suggested, notes = parse_proposals(answer, current, evidence)
    by_field = {p.field: p for p in proposals}
    assert notes == []
    # The sector equals the saved one: answered unchanged, so the dialog renders "Unchanged (1)".
    assert by_field["sector"].value == by_field["sector"].current == "Financial services"
    assert not by_field["sector"].conflict
    assert [p.field for p in proposals if p.value == p.current] == ["sector"]
    assert by_field["name"].conflict and by_field["name"].current == "Lab sample organization"
    assert by_field["loaded_hourly_rate"].check
    assert by_field["currency"].confidence == "low"
    assert by_field["regions"].value == ["FR", "DE"] and not by_field["regions"].conflict
    assert {p.source for p in proposals} == {"description"}
    # The draft of Settings > Organization never names an inventory field or the PIRs (#1991).
    assert "primary_assets" not in not_supported and "loaded_hourly_rate" not in not_supported
    assert suggested


def test_the_stub_cites_a_source_the_request_gives():
    lab = _lab_namespace()
    evidence = _evidence(opencti='{"sector": "Financial services"}')
    answer = lab["org_ai_draft_answer"](_request({}, evidence))
    proposals, _, _, notes = parse_proposals(answer, parse_profile({}), evidence)
    assert proposals and notes == []
    assert {p.source for p in proposals} == {"opencti"}
    assert not any(p.conflict for p in proposals)


def test_any_other_request_is_left_to_the_other_answers():
    answer = _lab_namespace()["org_ai_draft_answer"]
    assert answer({"messages": [{"role": "user", "content": "hello"}]}) is None
    assert (
        answer({"messages": [{"role": "system", "content": "You write the documentation of an agent fleet"}]}) is None
    )


def _lab_asgi_module(monkeypatch):
    calls: list[tuple[dict, bytes]] = []

    async def backend(scope, receive, send):
        message = await receive()
        calls.append((scope, message.get("body", b"")))

    monkeypatch.setitem(sys.modules, "app.main", types.SimpleNamespace(application=backend))
    monkeypatch.syspath_prepend(str(LAB.parent))
    spec = importlib.util.spec_from_file_location("lab_asgi_org_ai_draft", LAB.parent / "lab_asgi.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, calls


def _multipart(description: str) -> bytes:
    return (
        b'--b\r\nContent-Disposition: form-data; name="description"\r\n\r\n'
        + description.encode()
        + b'\r\n--b\r\nContent-Disposition: form-data; name="use_platforms"\r\n\r\ntrue\r\n--b--\r\n'
    )


def _post(module, body: bytes) -> list[dict]:
    sent: list[dict] = []
    chunks = [
        {"type": "http.request", "body": body[:10], "more_body": True},
        {"type": "http.request", "body": body[10:]},
    ]

    async def receive():
        return chunks.pop(0)

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": module.LAB_ORG_AI_DRAFT_PATH, "query_string": b""}
    asyncio.run(module.application(scope, receive, send))
    return sent


def test_lab_quota_answers_the_routes_429_and_any_other_description_reaches_the_app(monkeypatch):
    module, calls = _lab_asgi_module(monkeypatch)
    monkeypatch.setenv("LAB_ORG_AI_DRAFT", "1")
    sent = _post(module, _multipart("lab:quota please"))
    assert sent[0]["status"] == 429 and not calls
    assert json.loads(sent[1]["body"]) == {"detail": module.LAB_QUOTA_MESSAGE}
    from app.services.agent_chat import QUOTA_EXCEEDED_MESSAGE

    assert module.LAB_QUOTA_MESSAGE == QUOTA_EXCEEDED_MESSAGE

    body = _multipart("A lab company in Paris.")
    assert _post(module, body) == [] and calls[0][1] == body[:10]

    monkeypatch.setenv("LAB_ORG_AI_DRAFT", "0")
    assert _post(module, _multipart("lab:quota")) == [] and len(calls) == 2
