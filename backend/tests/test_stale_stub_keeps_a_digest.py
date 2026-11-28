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

"""The stale-clearing stub keeps a digest of what it replaced (#2722).

An inventory search returned 2 rows twice; both results were stubbed with
the tool name and a size only, and the model wrote its conclusion as "the
search returned no rows". The stub now carries a bounded digest of the body
(the item count of a JSON list, else its first line) and says the result was
not empty and must be re-called before anything is stated about it, while
the loop breaker still forgives the re-read it invites.
"""

from __future__ import annotations

import json

from app.llm.base import ChatMessage
from app.tools import agent_loop as al
from app.tools.agent_loop import _LoopBreaker
from app.tools.base import ToolCall

_ROWS = [{"id": f"asset-{i}", "name": f"host-{i}.example.test", "notes": "n" * 400} for i in range(2)]


def _turn(call_id: str, name: str, arguments: dict[str, object], body: str) -> list[ChatMessage]:
    return [
        ChatMessage(role="assistant", content="", tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)]),
        ChatMessage(role="tool", content=body, tool_call_id=call_id, name=name),
    ]


def test_a_two_row_list_written_about_then_crossed_keeps_its_item_count() -> None:
    body = json.dumps(_ROWS)
    messages = [ChatMessage(role="user", content="scope the exposure")]
    messages += _turn("s1", "search_assets", {"query": "host"}, body)
    messages.append(ChatMessage(role="assistant", content="Stage plan: " + "detail " * 150))
    for i in range(3):
        messages += _turn(f"r{i}", "get_page", {"page": i}, f"page {i} " + "x" * 20_000)

    al._clear_stale_tool_results(messages, live_budget_chars=al._TOOL_RESULT_LIVE_MIN_CHARS)

    stub = messages[2].content
    assert ", 2 items)" in stub, stub
    assert "the result was not empty" in stub
    assert "Re-call the tool before stating anything about it" in stub
    assert "host" not in stub, "the query argument is a body argument, never echoed"
    assert al._is_curation_stub(stub)
    assert al._is_refetchable_stub(stub)


def test_an_object_result_names_its_list_count_and_total() -> None:
    digest = al._tool_result_digest(json.dumps({"items": _ROWS, "total": 2}))
    assert digest == ", items: 2 items, total 2"
    assert al._tool_result_digest("[]") == ", 0 items"
    assert al._tool_result_digest(json.dumps([{"a": 1}])) == ", 1 item"


def test_a_plain_text_result_keeps_its_first_line_capped_and_never_a_body_argument() -> None:
    body = "\n\n2 matching assets ] for scope " + "w" * 600 + "\nrow 1\nrow 2"
    messages = [ChatMessage(role="user", content="go")]
    messages += _turn("a", "search_assets", {"query": "SECRET QUERY", "content": "SECRET BODY"}, body)
    messages += _turn("b", "search_assets", {"query": "other"}, "y" * 5_000)

    al._clear_stale_tool_results(messages, live_budget_chars=1_000)

    stub = messages[2].content
    assert ', first line "2 matching assets ) for scope ' in stub, stub
    assert "SECRET" not in stub
    assert len(al._tool_result_digest(body)) <= len(", ") + al._TOOL_RESULT_DIGEST_MAX
    assert "\n" not in stub
    assert al._is_refetchable_stub(stub)


def test_a_third_identical_call_after_two_stubbed_results_is_neither_warned_nor_refused() -> None:
    transcript: list[ChatMessage] = []
    breaker = _LoopBreaker(
        identical_warn=3, identical_refuse=5, similar_warn=6, similar_refuse=10, transcript=transcript
    )
    # Bound 3: without the forgiveness of the invited re-read, the third ask warns.
    args: dict[str, object] = {"query": "host"}
    body = json.dumps(_ROWS)
    for i in range(2):
        assert breaker.screen("search_assets", args).action == "allow"
        transcript += _turn(f"s{i}", "search_assets", args, body)
        breaker.record_answer("search_assets", args, transcript[-1])
    transcript += _turn("last", "get_page", {"page": 1}, "z" * 5_000)

    al._clear_stale_tool_results(transcript, live_budget_chars=1_000)
    assert all(al._is_refetchable_stub(transcript[i].content) for i in (1, 3)), "the premise: both were stubbed"

    decision = breaker.screen("search_assets", args)
    assert decision.action == "allow"
    assert breaker.events == [], "the invited re-read is never warned or refused"


def test_a_stub_never_longer_than_the_body_it_would_replace() -> None:
    short = "a" * (al._STALE_TOOL_RESULT_MIN_CHARS + 10)
    messages = [ChatMessage(role="user", content="go")]
    messages += _turn("a", "t" * 200, {"path": "p" * 120, "section": "s" * 120}, short)
    messages += _turn("b", "t", {}, "y" * 5_000)

    al._clear_stale_tool_results(messages, live_budget_chars=1_000)

    assert messages[2].content == short, "a stub that frees nothing leaves the body in place"
