"""Narrated tool-call scaffolding is folded out of delivered results.

The incident text: the sub-agent's final answer opened with a bare tool
name, a ``Request`` label, the JSON arguments, then a ``Tool Call Failed``
line listing tools that exist nowhere on the platform; the completion notice
delivered all of it into the parent conversation as if it were a result.
The fold is deterministic and narrow: it removes exactly that shape (and a
standalone ``Tool Call Failed`` line) and leaves every other kind of text -
a JSON code block, an HTTP request/response write-up, a heading - alone.
With the run's own tool vocabulary (``known_tools``) it is narrower still: a
block naming a tool the run never called is left alone unless it carries a
``Tool Call Failed`` line, so a legitimate API write-up survives.
"""

from __future__ import annotations

import inspect
import logging
import time
from types import SimpleNamespace

import pytest

from app.services import sub_agent, tool_scaffolding
from app.services.tool_scaffolding import (
    SCAFFOLDING_ONLY_NOTICE,
    fold_tool_scaffolding,
    transcript_tool_names,
)

INCIDENT_BLOCK = """update_work_item
Request
{
  "work_item_id": "1a35a6ad-3e41-4a5d-9b1c-0a0a0a0a0a0a",
  "status": "in_progress",
  "notes": "Triage started"
}
Tool Call Failed: Tool 'update_work_item' not found. Available tools: get_agent_state, math_calculator, spawn_background_task"""

REAL_ANSWER = (
    "I could not update the work item: this agent does not have the work "
    "items capability enabled.\n\nThe triage itself is done - the ticket is a "
    "duplicate of #4521."
)


def test_incident_block_is_folded_and_the_real_answer_survives():
    text = f"{INCIDENT_BLOCK}\n\n{REAL_ANSWER}"

    fold = fold_tool_scaffolding(text)

    assert fold.removed_blocks == 1
    assert fold.tool_names == ("update_work_item",)
    assert fold.text == REAL_ANSWER
    assert fold.delivered() == REAL_ANSWER
    assert "Tool Call Failed" not in fold.delivered()
    assert "math_calculator" not in fold.delivered()


def test_block_in_the_middle_leaves_both_sides_and_one_blank_line():
    text = f"Here is what I did.\n\n{INCIDENT_BLOCK}\n\n{REAL_ANSWER}"

    fold = fold_tool_scaffolding(text)

    assert fold.text == f"Here is what I did.\n\n{REAL_ANSWER}"


def test_only_scaffolding_yields_the_notice_not_an_empty_result():
    fold = fold_tool_scaffolding(INCIDENT_BLOCK)

    assert fold.text == ""
    assert fold.removed_blocks == 1
    assert fold.delivered() == SCAFFOLDING_ONLY_NOTICE.format(tools=" (update_work_item)")


def test_fenced_json_and_bold_name_and_tool_prefix_variants():
    text = (
        "**update_work_item**\n"
        "Arguments:\n"
        "```json\n"
        '{"work_item_id": "abc", "status": "done"}\n'
        "```\n"
        "Response\n"
        '{"error": "Tool not found"}\n'
        "\n"
        "Tool: `spawn_background_task`\n"
        'Input: {"agent_id": "x", "task": "y"}\n'
        "\n"
        "Done - the duplicate was closed."
    )

    fold = fold_tool_scaffolding(text)

    assert fold.removed_blocks == 2
    assert fold.tool_names == ("update_work_item", "spawn_background_task")
    assert fold.text == "Done - the duplicate was closed."


def test_nested_json_with_braces_in_strings_is_matched_whole():
    text = (
        "update_work_item\n"
        "Request\n"
        "{\n"
        '  "notes": "closing brace in a string } and an escaped quote \\" here",\n'
        '  "meta": {"a": {"b": [1, 2, {"c": "}"}]}}\n'
        "}\n"
        "\n"
        "All good."
    )

    fold = fold_tool_scaffolding(text)

    assert fold.removed_blocks == 1
    assert fold.text == "All good."


def test_standalone_failed_line_is_folded_with_its_wrapped_listing():
    text = (
        "Tool Call Failed: Tool 'update_work_item' not found. Available tools: a, b,\n"
        "c, d\n"
        "The item is a duplicate of #4521.\n"
    )

    fold = fold_tool_scaffolding(text)

    assert fold.removed_blocks == 1
    assert fold.tool_names == ("update_work_item",)
    assert fold.text == "The item is a duplicate of #4521."


def test_response_label_followed_by_prose_keeps_the_prose():
    """A model's real conclusion can sit right under a ``Response`` label; a
    stray label costs less than a lost answer."""
    text = (
        "update_work_item\n"
        "Request\n"
        '{"work_item_id": "abc"}\n'
        "Response\n"
        "I could not update the work item, so I am reporting the triage instead.\n"
    )

    fold = fold_tool_scaffolding(text)

    assert fold.removed_blocks == 1
    assert fold.text == ("I could not update the work item, so I am reporting the triage instead.")


def test_legitimate_json_code_block_is_untouched():
    text = 'Here is the configuration to apply:\n\n```json\n{"retries": 3, "timeout": 30}\n```\n\nApply it and restart.'

    fold = fold_tool_scaffolding(text)

    assert fold.removed_blocks == 0
    assert fold.text == text
    assert fold.delivered() == text


def test_http_request_response_write_up_without_a_tool_name_is_untouched():
    text = '## Reproduction\n\nRequest\n{"query": "status"}\nResponse\n{"status": "ok"}\n'

    fold = fold_tool_scaffolding(text)

    assert fold.removed_blocks == 0
    assert fold.text == text


def test_snake_case_line_followed_by_prose_is_untouched():
    text = "update_work_item\nis the tool I would have needed for this; it is not enabled.\n"

    fold = fold_tool_scaffolding(text)

    assert fold.removed_blocks == 0
    assert fold.text == text


def test_request_word_starting_a_sentence_is_not_a_label():
    text = 'update_work_item\nRequest handling is done by the ticketing system, not by me.\n{"unrelated": true}\n'

    fold = fold_tool_scaffolding(text)

    assert fold.removed_blocks == 0
    assert fold.text == text


def test_unbalanced_json_is_untouched():
    text = 'update_work_item\nRequest\n{\n  "work_item_id": "abc",\n\nSee above.'

    fold = fold_tool_scaffolding(text)

    assert fold.removed_blocks == 0
    assert fold.text == text


def test_fold_is_idempotent_and_handles_edge_inputs():
    once = fold_tool_scaffolding(f"{INCIDENT_BLOCK}\n\n{REAL_ANSWER}")
    twice = fold_tool_scaffolding(once.text)
    assert twice.removed_blocks == 0
    assert twice.text == once.text

    empty = fold_tool_scaffolding("")
    assert empty.text == "" and empty.removed_blocks == 0 and empty.delivered() == ""

    one_line = fold_tool_scaffolding("Just a sentence.")
    assert one_line.text == "Just a sentence." and one_line.removed_blocks == 0


def test_crlf_line_endings_are_handled():
    text = INCIDENT_BLOCK.replace("\n", "\r\n") + "\r\n\r\n" + "Done."

    fold = fold_tool_scaffolding(text)

    assert fold.removed_blocks == 1
    assert fold.text == "Done."


def test_crlf_block_in_the_middle_leaves_one_blank_line():
    text = "Intro.\r\n\r\n" + INCIDENT_BLOCK.replace("\n", "\r\n") + "\r\n\r\nDone."

    fold = fold_tool_scaffolding(text)

    assert fold.removed_blocks == 1
    assert fold.text == "Intro.\r\n\r\nDone."


# --- the known-tools contract --------------------------------

API_WRITE_UP = (
    "## How to create a user\n"
    "\n"
    "create_user\n"
    "Request\n"
    '{"email": "ada@example.com", "role": "analyst"}\n'
    "Response\n"
    '{"id": "u_42", "status": "created"}\n'
    "\n"
    "The endpoint is idempotent on the email address."
)


def test_legitimate_write_up_naming_an_uncalled_tool_is_kept_with_known_tools():
    """A snake_case name + Request + JSON is also the shape of an API doc.
    When the run's vocabulary is known, a block about a name the run never
    called and that carries no failure line is not scaffolding."""
    fold = fold_tool_scaffolding(API_WRITE_UP, known_tools={"get_agent_state", "search_knowledge"})

    assert fold.removed_blocks == 0
    assert fold.text == API_WRITE_UP


def test_grammar_only_fold_still_removes_the_same_block_without_known_tools():
    fold = fold_tool_scaffolding(API_WRITE_UP)

    assert fold.removed_blocks == 1
    assert fold.tool_names == ("create_user",)
    assert fold.text == "## How to create a user\n\nThe endpoint is idempotent on the email address."


def test_block_naming_a_called_tool_is_folded_even_without_a_failure_line():
    text = (
        "update_work_item\n"
        "Request\n"
        '{"work_item_id": "abc", "status": "done"}\n'
        "\n"
        "Closed as duplicate."
    )

    fold = fold_tool_scaffolding(text, known_tools={"update_work_item"})

    assert fold.removed_blocks == 1
    assert fold.text == "Closed as duplicate."


def test_failure_line_folds_a_block_whatever_the_run_called():
    """The incident block names a tool the run was REFUSED - it is not in the
    transcript's successful calls, but it IS in the transcript (the refused
    call).  Even with an empty vocabulary the explicit failure line is
    conclusive: nothing but a narrated transcript carries one."""
    text = f"{INCIDENT_BLOCK}\n\n{REAL_ANSWER}"

    fold = fold_tool_scaffolding(text, known_tools=frozenset())

    assert fold.removed_blocks == 1
    assert fold.text == REAL_ANSWER


def test_standalone_failed_sentence_without_a_marker_is_prose():
    text = "Tool Call Failed: the API rejected the request, so I stopped.\nNext step is manual review.\n"

    fold = fold_tool_scaffolding(text)

    assert fold.removed_blocks == 0
    assert fold.text == text


def test_inline_payload_followed_by_prose_keeps_the_whole_line():
    """``Request: {...} - I could not ...`` is one line carrying both the
    arguments and the model's conclusion; folding it would lose the
    conclusion, so the block is not a match at all."""
    text = (
        "update_work_item\n"
        'Request: {"work_item_id": "abc"} - I could not run this, the tool is not enabled.\n'
        "The ticket is a duplicate of #4521.\n"
    )

    fold = fold_tool_scaffolding(text)

    assert fold.removed_blocks == 0
    assert fold.text == text


def test_fence_opened_on_the_label_line_is_consumed_with_its_closing_fence():
    text = (
        "update_work_item\n"
        "Arguments: ```json\n"
        '{"work_item_id": "abc"}\n'
        "```\n"
        "\n"
        "Done."
    )

    fold = fold_tool_scaffolding(text)

    assert fold.removed_blocks == 1
    assert fold.text == "Done."
    assert "```" not in fold.text


@pytest.mark.parametrize(
    "text",
    [
        # Unbalanced payload opened on every third line: each candidate used
        # to scan to the end of the text (quadratic).
        "a_b_c\nRequest\n{\n" * 60_000,
        # Thousands of candidates, each followed by a long tail.
        "tool_x\nRequest\n{\n" * 3_000 + "filler line\n" * 20_000,
        # Markup / snake_case runs long enough to make a backtracking label
        # regex or name regex crawl.
        "*" * 100_000 + " x\nRequest\n{}\n",
        "_".join(["ab"] * 30_000) + " x\nRequest\n{}\n",
        # Deeply nested, never closed, with a long tail after it.
        "tool_x\nRequest\n{" + '"k": {' * 5_000 + "\n" + "}" * 4_999 + "\nrest\n" + "line\n" * 20_000,
    ],
    ids=["unbalanced-repeat", "many-candidates", "markup-line", "snake-line", "deep-unbalanced"],
)
def test_pathological_inputs_are_folded_in_bounded_time(text):
    # CPU time of this thread: a loaded runner stretches wall-clock time, not
    # the work the bound is about.
    started = time.thread_time()

    fold = fold_tool_scaffolding(text)

    assert time.thread_time() - started < 2.0
    assert fold.removed_blocks == 0
    assert fold.text == text


def test_many_real_blocks_are_all_folded_in_linear_work(monkeypatch):
    # Operation counts, not a time budget: a shared CI runner under
    # coverage stretched CPU time past 2 s on unchanged work.  Linear means
    # each line is tried as a block start at most once and each payload line
    # is brace-scanned once; a candidate rescanning the answer breaks both.
    scanners: list[tool_scaffolding._Scanner] = []
    match_calls = 0

    class CountingScanner(tool_scaffolding._Scanner):
        __slots__ = ()

        def __init__(self, lines: list[str]) -> None:
            super().__init__(lines)
            scanners.append(self)

        def match_block(self, i: int) -> tuple[int, str, bool] | None:
            nonlocal match_calls
            match_calls += 1
            return super().match_block(i)

    monkeypatch.setattr(tool_scaffolding, "_Scanner", CountingScanner)
    blocks = 1_500
    text = ("update_work_item\nRequest\n" + '{"a": "' + "x" * 100 + '"}\nok\n') * blocks

    fold = fold_tool_scaffolding(text, known_tools={"update_work_item"})

    assert fold.removed_blocks == blocks
    assert fold.text == "\n".join(["ok"] * blocks)
    (scanner,) = scanners
    assert match_calls <= scanner.n
    assert tool_scaffolding._MAX_SCAN_LINES_TOTAL - scanner.budget == blocks


# --- the run's tool vocabulary ---------------------------------------------------


def test_transcript_tool_names_collects_calls_and_tool_results_only():
    messages = [
        SimpleNamespace(role="user", content="triage this", tool_calls=None),
        SimpleNamespace(
            role="assistant",
            content="",
            tool_calls=[SimpleNamespace(name="update_work_item"), SimpleNamespace(name="get_agent_state")],
        ),
        SimpleNamespace(role="tool", name="update_work_item", content="Tool 'update_work_item' is not available"),
        SimpleNamespace(role="assistant", content="done", tool_calls=[]),
        "not a message at all",
        SimpleNamespace(role="tool", name="", content="unnamed"),
    ]

    assert transcript_tool_names(messages) == frozenset({"update_work_item", "get_agent_state"})
    assert transcript_tool_names([]) == frozenset()


# --- the sub-agent delivery path -------------------------------------------------


def test_fold_delivered_answer_uses_the_transcript_vocabulary(caplog):
    transcript = [
        SimpleNamespace(role="assistant", content="", tool_calls=[SimpleNamespace(name="update_work_item")]),
        SimpleNamespace(role="tool", name="update_work_item", content="refused"),
    ]
    snapshot = list(transcript)

    with caplog.at_level(logging.INFO, logger="app.services.sub_agent"):
        delivered = sub_agent._fold_delivered_answer("task-1", f"{INCIDENT_BLOCK}\n\n{REAL_ANSWER}", transcript)

    assert delivered == REAL_ANSWER
    assert transcript == snapshot, "the raw transcript must keep the real tool calls and refusals"
    assert any("folded 1 narrated tool-call block" in r.getMessage() for r in caplog.records)


def test_fold_delivered_answer_leaves_a_write_up_about_an_uncalled_tool_alone():
    transcript = [SimpleNamespace(role="assistant", content="", tool_calls=[SimpleNamespace(name="search_knowledge")])]

    assert sub_agent._fold_delivered_answer("task-2", API_WRITE_UP, transcript) == API_WRITE_UP


def test_fold_delivered_answer_normalises_a_missing_answer():
    assert sub_agent._fold_delivered_answer("task-3", None, []) == ""
    assert sub_agent._fold_delivered_answer("task-4", "plain answer", []) == "plain answer"


def test_sub_agent_delivery_folds_before_persisting_the_result():
    """Everything delivered from a background task derives from
    ``agent_response`` - the fold must run before the first persisted use of
    it, and the raw transcript (``chat_messages``) is read, never rewritten."""
    source = inspect.getsource(sub_agent._execute_sub_agent_task)
    fold_at = source.find("agent_response = _fold_delivered_answer(")
    assert fold_at != -1, "the delivered answer must be folded"
    assert fold_at < source.find("task2.result ="), "the fold must run before the task result is persisted"
    assert "chat_messages = _fold_delivered_answer(" not in source, (
        "the raw transcript must keep the real tool calls and refusals"
    )
