"""#10910: ``LAB_CHAT_LAYOUT=on`` seeds the rows the chat-layout UX rounds review.

The plan and the Markdown are pure functions of ``seed_chat_layout.py``: a change that drops an
agent icon, a date bucket, a long title or a Markdown element fails here, not in a UX round. The
services are the default lab and its light twin with the switch.
"""

from __future__ import annotations

import ast
import importlib.util
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"
SCRIPT = LAB.parent / "seed_chat_layout.py"
CHECKS = ROOT / ".xtm-one" / "checks.toml"

pytestmark = pytest.mark.skipif(not LAB.is_file() or not SCRIPT.is_file(), reason="no .xtm-one next to the backend")


def _module():
    spec = importlib.util.spec_from_file_location("lab_seed_chat_layout", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_agents_and_workspaces_cover_icons_default_and_long_names():
    module = _module()
    icons = [icon for _, icon in module.AGENTS]
    assert len(module.AGENTS) >= 10 and "Bot" in icons
    assert len(set(icons)) == len(icons)
    assert len(module.PREFIX + module.LONG_AGENT) > 60
    assert len(module.WORKSPACES) >= 10
    assert max(len(name) for name, _, _ in module.WORKSPACES) > 60
    assert len({color for _, _, color in module.WORKSPACES if color}) >= 8


def test_conversations_span_every_sidebar_bucket_with_long_and_french_titles():
    module = _module()
    now = datetime.now(UTC)
    plan = module.conversation_plan(now)
    assert len(plan) >= 40
    days = {(now - entry["at"]).days for entry in plan}
    assert {0, 1} <= days and any(2 <= d <= 7 for d in days) and any(d > 7 for d in days)
    titles = [entry["title"] for entry in plan]
    assert len(set(titles)) == len(titles)
    assert any(len(t) > 120 for t in titles) and any("é" in t for t in titles)
    assert any(entry["workspace"] is None for entry in plan) and any(entry["workspace"] is not None for entry in plan)


def test_the_thread_carries_every_markdown_element_tools_reasoning_and_attachments():
    module = _module()
    messages = module.thread_messages(datetime.now(UTC))
    assert len(messages) == 2 * module.THREAD_TURNS >= 60
    answers = "\n".join(m["content"] for m in messages if m["role"] == "assistant")
    for part in ("# Finding", "## Summary", "### Detail", "|---|", "```python", "`CreateRemoteThread`",
                 "  - Phishing", "> The actor", "](https://", "![Lab image](data:image/png", "**Sources**"):
        assert part in answers, part
    code = module.code_block()
    assert code.count("\n") - 1 == 200
    metas = [m["metadata"] for m in messages]
    assert any("tool_call_trace" in m for m in metas) and any("reasoning" in m for m in metas)
    kinds = {a["type"] for m in metas for a in m.get("attachments", [])}
    assert kinds == {"png", "pdf", "csv"}


def test_the_lab_starts_the_seed_under_the_switch_and_reads_the_thread_back():
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_chat_layout"])
    for part in ("'seed_chat_layout.py'", "sys.executable", "env=seed_script_env()", "/messages?limit=20",
                 "seeded chat layout: ", "stderr_summary(process.stderr)"):
        assert part in helper, part
    seeded = ast.unparse(functions["seed_entities"])
    call = "seed_chat_layout(token, base, peer_id)"
    assert call in seeded and "skipped['chat layout']" in seeded
    assert "lab_switch('LAB_CHAT_LAYOUT', LAB_CHAT_LAYOUT_STATES) == 'on'" in seeded


def test_the_chat_layout_labs_are_the_default_labs_with_the_switch():
    services = tomllib.loads(CHECKS.read_text(encoding="utf-8"))["service"]
    twins = {
        "lab-chat-layout": ("lab", {"LAB_CHAT_LAYOUT": "on", "LAB_OFFICE_ADDIN": "on"}),
        "lab-chat-layout-light": ("lab", {"LAB_CHAT_LAYOUT": "on", "LAB_THEME": "light", "LAB_OFFICE_ADDIN": "on"}),
    }
    for name, (parent, switches) in twins.items():
        service, base = services[name], services[parent]
        assert service["env"] == {**base["env"], **switches}, name
        # The Office add-in task pane (#11109) builds apps/microsoft-office: the twins ship it and wait longer.
        assert service["needs"] == [*base["needs"], "apps/microsoft-office"], name
        assert service["timeout"] >= base["timeout"], name
        own = ("env", "description", "needs", "timeout")
        rest = {k: v for k, v in service.items() if k not in own}
        assert rest == {k: v for k, v in base.items() if k not in own}, name
    assert not any("LAB_CHAT_LAYOUT" in s.get("env", {}) for n, s in services.items() if n not in twins)
