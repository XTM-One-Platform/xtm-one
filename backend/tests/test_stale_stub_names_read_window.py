# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""The stale-clearing stub names the window a cleared read addressed.

Gap reread-after-context-prune (run 3350c28d): after the curation cleared
older ``sandbox_read_file`` results, the stub only said "Earlier
sandbox_read_file result cleared", so the model could not tell WHICH ranges
were gone and re-read the same windows two or three times. The stub now
carries the addressing arguments of the call (path, range, file id, ...),
still matched whole by the loop breaker's ``_is_refetchable_stub``.
"""

from __future__ import annotations

from app.llm.base import ChatMessage
from app.tools import agent_loop as al
from app.tools.base import ToolCall


def _read_turn(call_id: str, arguments: dict, size: int = 5_000) -> list[ChatMessage]:
    return [
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id=call_id, name="sandbox_read_file", arguments=arguments)],
        ),
        ChatMessage(role="tool", content="x" * size, tool_call_id=call_id, name="sandbox_read_file"),
    ]


def test_cleared_read_names_path_and_window():
    messages = [ChatMessage(role="user", content="go")]
    messages += _read_turn("a", {"path": "repo/app/x.py", "offset_line": 100, "max_lines": 80})
    messages += _read_turn("b", {"path": "repo/app/y.py"})

    al._clear_stale_tool_results(messages, live_budget_chars=1_000)

    stub = messages[2].content
    assert stub.startswith(
        "[Earlier sandbox_read_file result for path=repo/app/x.py, offset_line=100, max_lines=80 (5,000 chars)"
    ), stub
    assert al._is_refetchable_stub(stub), "the loop breaker still reads it as the invited re-read"
    assert messages[4].content == "x" * 5_000, "the newest result is kept verbatim"


def test_body_arguments_never_echoed_and_values_bounded():
    arguments = {
        "path": "p/" + "a" * 500,
        "content": "SECRET BODY",
        "edits": [{"find": "x"}],
        "section": "Rules ] registry\nnext",
    }
    target = al._tool_call_target(arguments)
    assert "SECRET" not in target and "edits" not in target
    assert "]" not in target and "\n" not in target
    assert len(target) <= len(" for ") + al._TOOL_CALL_TARGET_MAX
    stub = al._REFETCHABLE_STUB_TEMPLATE.format(tool="sandbox_read_file", target=target, chars="5,000")
    assert al._is_refetchable_stub(stub)


def test_no_matching_call_keeps_the_plain_stub():
    messages = [
        ChatMessage(role="user", content="go"),
        ChatMessage(role="tool", content="x" * 5_000, tool_call_id="orphan", name="list_x"),
        ChatMessage(role="tool", content="y" * 5_000, tool_call_id="last", name="list_x"),
    ]
    al._clear_stale_tool_results(messages, live_budget_chars=1_000)
    assert messages[1].content == al._REFETCHABLE_STUB_TEMPLATE.format(tool="list_x", target="", chars="5,000")
    assert al._tool_call_target(None) == "" and al._tool_call_target({"query": "q"}) == ""


def test_reused_call_id_pairs_in_transcript_order():
    """Two turns minting the same id (Gemini ``call_<name>_0``): each result
    names its OWN call's window, never the later call's (no global id map)."""
    messages = [ChatMessage(role="user", content="go")]
    messages += _read_turn("call_sandbox_read_file_0", {"path": "repo/a.py", "offset_line": 1})
    messages += _read_turn("call_sandbox_read_file_0", {"path": "repo/b.py", "offset_line": 500})
    messages += _read_turn("last", {"path": "repo/c.py"})

    al._clear_stale_tool_results(messages, live_budget_chars=1_000)

    assert messages[2].content.startswith("[Earlier sandbox_read_file result for path=repo/a.py, offset_line=1 ")
    assert messages[4].content.startswith("[Earlier sandbox_read_file result for path=repo/b.py, offset_line=500 ")
