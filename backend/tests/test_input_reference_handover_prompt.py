"""A detached ``$input`` document never reaches a delegated run's system prompt (#12095).

``format_handover_context_prompt`` copies the task text as "Original question":
for a delegated run (``run_agent_once``, ``spawn_background_task``, consult,
transfer) that text is the run's message, whose large document the agent loop
detaches into ``attachments/input.json``. The prompt copy goes through
``prompt_safe_task_text``: the same reference block, then a cap.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.builtin_tools import format_handover_context_prompt
from app.tools.session_input import (
    INPUT_INLINE_MAX_CHARS,
    INPUT_MOUNT_NAME,
    PROMPT_TASK_TEXT_MAX_CHARS,
    detach_input_document,
    prompt_safe_task_text,
)

_INSTRUCTIONS = "Add the label phishing to the report.\n--- STIX BUNDLE ---\n"


def _bundle(chars: int, objects: int = 400) -> dict[str, Any]:
    """A bundle of *objects* notes, about *chars* characters in all."""
    filler = "x" * max(1, chars // objects)
    return {
        "type": "bundle",
        "id": "bundle--5d0092c5-5f74-4287-9642-33f4c354e56d",
        "objects": [
            {"type": "note", "id": f"note--{i:08d}-0000-4000-8000-000000000000", "content": filler}
            for i in range(objects)
        ],
    }


def _task(chars: int = 300_000) -> tuple[str, str]:
    body = json.dumps(_bundle(chars))
    return _INSTRUCTIONS + body + "\nAnswer with the patch only.", body


@pytest.mark.parametrize("ho_type", ["sub_agent_task", "consult", "transfer", "handover"])
def test_no_handover_prompt_holds_a_detached_document(ho_type: str) -> None:
    task, body = _task()
    assert len(body) > 300_000 > INPUT_INLINE_MAX_CHARS
    prompt = "\n".join(
        format_handover_context_prompt(
            {
                "type": ho_type,
                "source_agent": "background task",
                "original_question": task,
                "source_agent_response": task,
            }
        )
    )
    assert body not in prompt
    assert "x" * 700 not in prompt, "no object of the document body is copied"
    assert "Input document detached from this message" in prompt
    assert f"attachments/{INPUT_MOUNT_NAME}" in prompt
    assert "objects: 400 (note 400)" in prompt
    # A few thousand characters, not the 300,000 of the bundle: well under
    # the 20k input tokens the first iteration of a delegated run may cost.
    assert len(prompt) < 20_000


def test_the_message_and_the_prompt_carry_the_same_reference_block() -> None:
    task, _body = _task()
    messages = [SimpleNamespace(role="user", content=task)]
    document, detached = detach_input_document(messages)
    assert detached and len(document["objects"]) == 400
    # The prompt copy reads like the message the run received.
    assert prompt_safe_task_text(task) == messages[0].content


def test_a_long_task_without_a_document_is_capped_with_a_pointer() -> None:
    task = "y" * (PROMPT_TASK_TEXT_MAX_CHARS * 3)
    line = prompt_safe_task_text(task)
    assert line.startswith("y" * PROMPT_TASK_TEXT_MAX_CHARS)
    assert f"{PROMPT_TASK_TEXT_MAX_CHARS * 2:,} more characters cut" in line
    assert "the whole task is the message you received" in line
    assert len(line) < PROMPT_TASK_TEXT_MAX_CHARS + 300


def test_a_short_task_and_a_small_document_are_copied_as_they_are() -> None:
    small = _INSTRUCTIONS + json.dumps(_bundle(2_000, objects=5))
    assert prompt_safe_task_text("Summarize the last 10 CVEs.") == "Summarize the last 10 CVEs."
    assert prompt_safe_task_text(small) == small
    assert prompt_safe_task_text(None) == ""
    assert prompt_safe_task_text("") == ""


def test_an_uncapped_copy_only_replaces_the_document() -> None:
    long_text = "z" * (PROMPT_TASK_TEXT_MAX_CHARS * 2)
    assert prompt_safe_task_text(long_text, max_chars=None) == long_text
