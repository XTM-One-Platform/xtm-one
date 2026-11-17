"""The lab seeds one chat agent with suggested actions, so the chat welcome screen renders its chips.

``GET /chat/suggestions`` answers an agent's own ``web_suggested_prompts`` and nothing else, so a lab with no
such agent never shows the chatbot's suggestion chips. lab.py starts the stores when imported, so the seed is
compiled from its syntax tree, as test_lab_chatbot_host.py does. dev-docs/reference/agentic-labs.md,
"A @filigran/chatbot branch in the lab".
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"
HOST = ROOT / ".xtm-one" / "lab" / "chatbot-host" / "lab-chatbot-host.tsx"
DOCS = ROOT / "dev-docs" / "reference" / "agentic-labs.md"

pytestmark = pytest.mark.skipif(not LAB.is_file(), reason="no .xtm-one next to the backend")

_NAMES = ("LAB_SUGGESTIONS_AGENT_SLUG", "LAB_CHAT_SUGGESTIONS", "seed_chat_suggestions")


def _lab(logged: list[str], api) -> dict:
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    body = [
        node
        for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name in _NAMES)
        or (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in _NAMES for t in node.targets))
    ]

    def created(answer):
        return str(answer["id"])

    namespace: dict = {"api": api, "log": logged.append, "_created": created, "SEED_NAME": "Lab sample"}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(LAB), "exec"), namespace)  # noqa: S102 - the repo's own file
    return namespace


def test_three_suggestions_one_long_enough_to_be_cut():
    suggestions = _lab([], None)["LAB_CHAT_SUGGESTIONS"]
    assert len(suggestions) == 3
    # The design system's Chip cuts a label at 250 px: one label is far past it.
    assert max(len(s) for s in suggestions) >= 60


def test_the_agent_is_created_given_its_prompts_and_read_back():
    logged: list[str] = []
    calls: list[tuple] = []
    lab: dict = {}

    def api(method, path, token=None, body=None):
        calls.append((method, path, body))
        if method == "POST":
            return {"id": "a1"}
        if method == "GET":
            return list(lab["LAB_CHAT_SUGGESTIONS"])
        return {}

    lab.update(_lab(logged, api))
    seeded = lab["seed_chat_suggestions"]("tok", "http://lab")
    slug = lab["LAB_SUGGESTIONS_AGENT_SLUG"]
    assert calls[0][:2] == ("POST", "/agents") and calls[0][2]["slug"] == slug
    assert calls[1] == ("PUT", "/agents/a1", {"web_suggested_prompts": lab["LAB_CHAT_SUGGESTIONS"]})
    assert calls[2][:2] == ("GET", f"/chat/suggestions?agent_slug={slug}")
    assert logged == [f"seeded chat suggestions: {slug}"]
    assert seeded == {"chat suggestions": {"id": "a1", "url": "http://lab/chat?agent=a1"}}


def test_a_read_back_without_the_suggestions_raises():
    def api(method, path, token=None, body=None):
        return {"id": "a1"} if method == "POST" else []

    with pytest.raises(RuntimeError, match="GET /chat/suggestions answers"):
        _lab([], api)["seed_chat_suggestions"]("tok", "http://lab")


def test_the_chatbot_host_opens_on_the_seeded_agent():
    slug = _lab([], None)["LAB_SUGGESTIONS_AGENT_SLUG"]
    source = HOST.read_text(encoding="utf-8")
    assert f'const SUGGESTIONS_AGENT = "{slug}";' in source
    assert "initialAgent={initialAgent}" in source


def test_the_docs_name_the_seed():
    assert "seeded chat suggestions: lab-chat-suggestions" in DOCS.read_text(encoding="utf-8")
