"""Tests for the agentic-loop robustness fixes.

Two distinct, compounding weaknesses are covered:

  * **Stall guard** — a tool-free "final" answer that actually narrates an
    unperformed action ("now let me write the records", "executing now") is
    re-prompted to EXECUTE instead of being returned to the user as a
    half-finished checkpoint. Bounded so a genuinely finished / blocked /
    question-asking turn always ends.
  * **Tool-result curation (context-rot guard)** — large tool results are kept
    only up to a live byte budget (older ones stubbed) and exact duplicates are
    collapsed, independent of duplication, inputs, or overall window
    utilization. This is what keeps the working context focused well below the
    hard window limit, where the model otherwise degrades into narration.
"""

from __future__ import annotations

import time

import pytest

from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse, FileRef
from app.services.content_safety import untrusted_block_spans
from app.tools.agent_loop import (
    _MAX_STALL_CONTINUATIONS,
    _RECENT_TOOL_FILES_KEEP,
    _STALL_CONTINUATION_NUDGE,
    _TOOL_RESULT_FILES_LIVE_MAX_BYTES,
    _TOOL_RESULT_FILES_LIVE_MIN_BYTES,
    _TOOL_RESULT_LIVE_MAX_CHARS,
    _TOOL_RESULT_LIVE_MIN_CHARS,
    _clear_stale_tool_result_files,
    _clear_stale_tool_results,
    _collapse_redundant_tool_results,
    _compute_live_tool_result_budget,
    _compute_tool_result_files_budget,
    _emergency_prune_session_context,
    _looks_like_unfinished_action,
    _prune_session_context,
    run_agent_loop,
)
from app.tools.base import CallableToolExecutor, ToolCall, ToolDefinition


def _outside_blocks(text: str) -> str:
    """*text* without its quarantine blocks (and the line break that follows each)."""
    spans = untrusted_block_spans(text)
    out, cursor = [], 0
    for start, end in spans:
        out.append(text[cursor:start])
        cursor = end + 1 if text[end : end + 1] == "\n" else end
    out.append(text[cursor:])
    return "".join(out)


# ---------------------------------------------------------------------------
# _looks_like_unfinished_action — narration-without-execution detector
# ---------------------------------------------------------------------------

# Relaxed detector: ONLY blatant present-tense "I'm doing it now" claims trip
# the guard. Plain plans ("let me pull...", "next I'll write...") are allowed to
# end the turn.
_UNFINISHED = [
    "Executing now: assigning owners to the in-scope controls.",
    "Pending (executing now): strip stale owners, then assign.",
    "Now executing the writes.",
    "I'm assigning the owners now.",
    "Writing the records now.",
    "Let me execute the batch now.",
    "I'll submit these now.",
]

_FINISHED_OR_QUESTION = [
    "Here is the final summary of what I assigned.",
    "All 425 owners have been assigned. Task complete.",
    "Would you like me to proceed with the assignment?",
    "Let me know if you need anything else.",
    "What should I do next?",
    "I have summarized the results above.",
    "Now let me give you the final summary of the run.",  # text action, not a tool
    # Relaxed away — these are normal plans, not present-tense execution claims:
    "Let me now run the batch and assign the owners.",
    "Now let me pull the full control dataset and classify it.",
    "Next, I'll write the records to Drata.",
    "Let me confirm the exact tool signatures before writing at scale.",
    "Let me classify all 336 now.",
    "",
]


@pytest.mark.parametrize("text", _UNFINISHED)
def test_looks_like_unfinished_action_positive(text):
    assert _looks_like_unfinished_action(text) is True


@pytest.mark.parametrize("text", _FINISHED_OR_QUESTION)
def test_looks_like_unfinished_action_negative(text):
    assert _looks_like_unfinished_action(text) is False


def test_looks_like_unfinished_action_only_inspects_tail():
    # An action verb buried in a recap of PAST work (followed by a long, clearly
    # terminal tail) must not trip the detector.
    text = (
        "Earlier I had to run several queries and write the plan. "
        + "All of that is now done and the task is fully complete. " * 20
    )
    assert _looks_like_unfinished_action(text) is False


# ---------------------------------------------------------------------------
# _compute_live_tool_result_budget — window-scaled with floor + cap
# ---------------------------------------------------------------------------


def test_live_tool_budget_floor_for_small_window():
    assert _compute_live_tool_result_budget(10_000) == _TOOL_RESULT_LIVE_MIN_CHARS


def test_live_tool_budget_cap_for_huge_window():
    assert _compute_live_tool_result_budget(10_000_000) == _TOOL_RESULT_LIVE_MAX_CHARS


def test_live_tool_budget_within_bounds_for_zero_window():
    budget = _compute_live_tool_result_budget(0)
    assert _TOOL_RESULT_LIVE_MIN_CHARS <= budget <= _TOOL_RESULT_LIVE_MAX_CHARS


# ---------------------------------------------------------------------------
# _clear_stale_tool_results — keep recent bytes, stub older ones
# ---------------------------------------------------------------------------


def _tool_msg(idx: int, size: int, name: str = "list_x") -> ChatMessage:
    return ChatMessage(
        role="tool",
        content=f"result-{idx}-" + ("A" * size),
        tool_call_id=f"t{idx}",
        name=name,
    )


def test_clear_stale_tool_results_keeps_recent_within_budget():
    messages = [ChatMessage(role="user", content="q")]
    messages += [_tool_msg(i, 800) for i in range(4)]

    freed = _clear_stale_tool_results(messages, live_budget_chars=1_000)

    assert freed > 0
    # Newest tool result (index 4) is always kept verbatim.
    assert messages[4].content.startswith("result-3-")
    # Older results that overflow the budget are stubbed.
    for i in (1, 2, 3):
        assert messages[i].content.startswith("[Earlier ")


def test_clear_stale_keeps_single_newest_even_when_over_budget():
    messages = [
        ChatMessage(role="user", content="q"),
        _tool_msg(0, 2_000),
        _tool_msg(1, 2_000),
    ]

    _clear_stale_tool_results(messages, live_budget_chars=1_000)

    # The most-recent result is kept even though it alone exceeds the budget,
    # so the model never loses the data it is actively working on.
    assert messages[2].content.startswith("result-1-")
    assert messages[1].content.startswith("[Earlier ")


def test_clear_stale_noop_for_single_tool_result():
    messages = [ChatMessage(role="user", content="q"), _tool_msg(0, 5_000)]
    assert _clear_stale_tool_results(messages, live_budget_chars=10) == 0
    assert messages[1].content.startswith("result-0-")


def test_clear_stale_clears_every_older_large_result_once_over_budget():
    # Regression: once the live budget is crossed, EVERY older large result must
    # be cleared. The previous logic neither counted the stub bytes toward the
    # live size nor latched the over-budget state, so a slightly smaller older
    # result (here the oldest, 700 chars) slipped back under the budget and
    # survived verbatim — under-enforcing live_budget_chars.
    messages = [ChatMessage(role="user", content="q")]
    messages.append(_tool_msg(0, 700))  # oldest, large enough to be stubbable
    messages.append(_tool_msg(1, 1_400))  # middle, large
    messages.append(_tool_msg(2, 1_400))  # newest, kept verbatim

    freed = _clear_stale_tool_results(messages, live_budget_chars=2_150)

    assert freed > 0
    # Newest is always kept verbatim.
    assert messages[3].content.startswith("result-2-")
    # Both older large results are cleared (the oldest is the one the buggy
    # knapsack would have left untouched).
    assert messages[2].content.startswith("[Earlier ")
    assert messages[1].content.startswith("[Earlier ")


# ---------------------------------------------------------------------------
# _collapse_redundant_tool_results — drop exact duplicate large results
# ---------------------------------------------------------------------------


def test_collapse_redundant_keeps_newest_duplicate():
    dup = "DUP-" + ("A" * 800)
    messages = [
        ChatMessage(role="user", content="q"),
        ChatMessage(role="tool", content=dup, tool_call_id="t1", name="list_x"),
        ChatMessage(role="tool", content=dup, tool_call_id="t2", name="list_x"),
        ChatMessage(
            role="tool", content="OTHER-" + ("C" * 800), tool_call_id="t3", name="list_y"
        ),
    ]

    freed = _collapse_redundant_tool_results(messages)

    assert freed > 0
    # Older identical copy is collapsed; the newest copy and the distinct result
    # are untouched.
    assert messages[1].content.startswith("[Duplicate ")
    assert messages[2].content == dup
    assert messages[3].content.startswith("OTHER-")


def test_collapse_redundant_leaves_distinct_results_untouched():
    messages = [
        ChatMessage(role="user", content="q"),
        ChatMessage(
            role="tool", content="A" * 800, tool_call_id="t1", name="list_x"
        ),
        ChatMessage(
            role="tool", content="B" * 800, tool_call_id="t2", name="list_x"
        ),
    ]
    assert _collapse_redundant_tool_results(messages) == 0
    assert messages[1].content == "A" * 800
    assert messages[2].content == "B" * 800


def test_collapse_redundant_does_not_collapse_head_tail_collision():
    # Regression: the previous fingerprint was (name, len, head[:200],
    # tail[-200:]), which collides for two DIFFERENT bodies that share the same
    # length, first 200 chars, and last 200 chars but differ in the middle.
    # Collapsing them would silently stub a non-duplicate (data loss in the
    # working context). A full-body hash treats them as distinct.
    head = "H" * 200
    tail = "T" * 200
    body_a = head + ("A" * 400) + tail
    body_b = head + ("B" * 400) + tail
    # Precondition: the bodies are distinct but indistinguishable to the old
    # (length, head, tail) fingerprint.
    assert body_a != body_b
    assert len(body_a) == len(body_b)
    assert body_a[:200] == body_b[:200]
    assert body_a[-200:] == body_b[-200:]

    messages = [
        ChatMessage(role="user", content="q"),
        ChatMessage(role="tool", content=body_a, tool_call_id="t1", name="list_x"),
        ChatMessage(role="tool", content=body_b, tool_call_id="t2", name="list_x"),
    ]

    assert _collapse_redundant_tool_results(messages) == 0
    assert messages[1].content == body_a
    assert messages[2].content == body_b


_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00"
    b"\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _shot_file(
    name: str = "desktop.png", *, size: int | None = None, file_id: str | None = None
) -> FileRef:
    ref = FileRef.from_bytes(_TINY_PNG, name, "image/png", file_id=file_id)
    if size is not None:
        # The retention budget reads the stored size, not the bytes.
        ref.size = size
    return ref


# ---------------------------------------------------------------------------
# _clear_stale_tool_result_files — retention counted in tool-call GROUPS,
# bounded by bytes, always self-describing (trace 722375f1, 2026-09-24)
# ---------------------------------------------------------------------------


def _image_batch(
    batch: int, count: int, *, size: int | None = None
) -> list[ChatMessage]:
    """One assistant ``tool_calls`` turn answered by ``count`` image results —
    the shape of a parallel batch of ``download_notion_file`` calls."""
    calls = [
        ToolCall(
            id=f"b{batch}-c{i}",
            name="download_notion_file",
            arguments={"block_id": f"block-{batch}-{i}"},
        )
        for i in range(count)
    ]
    out = [ChatMessage(role="assistant", content="", tool_calls=calls)]
    for i in range(count):
        file_id = f"file-{batch}-{i}"
        out.append(
            ChatMessage(
                role="tool",
                content=(
                    f"Downloaded 'diagram-{batch}-{i}.png' from Notion as a "
                    f"reusable file. [[FILE:{file_id}]]"
                ),
                tool_call_id=f"b{batch}-c{i}",
                name="download_notion_file",
                files=[
                    _shot_file(f"diagram-{batch}-{i}.png", size=size, file_id=file_id)
                ],
            )
        )
    return out


def _tool_msgs(messages: list[ChatMessage]) -> list[ChatMessage]:
    return [m for m in messages if m.role == "tool"]


def _assert_self_describing_stub(msg: ChatMessage, filename: str, file_id: str) -> None:
    """A message that lost its files must say which files, by id, and how to
    see them again — and must never claim the pixels are still attached."""
    assert msg.files is None
    assert filename in msg.content
    assert file_id in msg.content
    assert "read_file_content" in msg.content
    assert "no longer attached" in msg.content
    assert "re-fetchable" not in msg.content
    assert "Re-call the tool" not in msg.content


def test_clear_stale_tool_result_files_keeps_whole_latest_batch():
    """(a) A parallel batch of 3 image results in the latest group keeps all
    3 — the incident dropped the first one before the model ever saw it."""
    messages = [ChatMessage(role="user", content="look at the three diagrams")]
    messages += _image_batch(1, 3)

    dropped = _clear_stale_tool_result_files(messages)

    assert dropped == 0
    assert all(m.files for m in _tool_msgs(messages))
    # The stub contract only kicks in when something is dropped: the tool's
    # own text is untouched.
    assert all("removed from the working context" not in m.content for m in _tool_msgs(messages))


def test_clear_stale_tool_result_files_counts_groups_not_messages():
    """(b) Two older batches plus a new one: the window keeps the last K
    file-bearing GROUPS (K = _RECENT_TOOL_FILES_KEEP), whatever their size."""
    assert _RECENT_TOOL_FILES_KEEP == 2, "test assumes the default window of 2 groups"
    messages = [ChatMessage(role="user", content="q")]
    messages += _image_batch(1, 3)  # oldest -> dropped
    messages += _image_batch(2, 2)  # kept (second most recent group)
    messages += _image_batch(3, 3)  # kept (latest group)

    dropped = _clear_stale_tool_result_files(messages)

    assert dropped == 3
    by_batch = {
        b: [m for m in _tool_msgs(messages) if m.tool_call_id.startswith(f"b{b}-")]
        for b in (1, 2, 3)
    }
    assert all(m.files is None for m in by_batch[1])
    assert all(m.files for m in by_batch[2])
    assert all(m.files for m in by_batch[3])
    # Under the old per-message count only the last 2 MESSAGES would have kept
    # their pixels; here 5 messages across the last 2 groups do.
    assert sum(1 for m in _tool_msgs(messages) if m.files) == 5


def test_clear_stale_tool_result_files_stub_names_files_and_recovery_tool():
    """(c) Whenever files are dropped the content carries the filename, the
    file id and ``read_file_content`` — never a bare ``files=None``."""
    messages = [ChatMessage(role="user", content="q")]
    messages += _image_batch(1, 2)
    messages += _image_batch(2, 1)
    messages += _image_batch(3, 1)

    _clear_stale_tool_result_files(messages)

    old = [m for m in _tool_msgs(messages) if m.tool_call_id.startswith("b1-")]
    assert len(old) == 2
    for i, msg in enumerate(old):
        _assert_self_describing_stub(msg, f"diagram-1-{i}.png", f"file-1-{i}")
        # The tool's own text (the [[FILE:id]] marker the user's card needs)
        # is kept; the note is appended, not substituted.
        assert msg.content.startswith("Downloaded 'diagram-1-")
    # Idempotent: a second pass neither drops again nor stacks a second note.
    assert _clear_stale_tool_result_files(messages) == 0
    assert old[0].content.count("removed from the working context") == 1


def _assert_states_one_thing_about_the_pixels(msg: ChatMessage) -> None:
    """A message that lost its files states ONE thing about them — the note —
    never the tool's own "is attached" claim next to it, and never the old
    "was attached" retraction either (two statements about the same pixels,
    which the 24 Sept 2026 model read as a contradiction)."""
    assert "removed from the working context" in msg.content
    assert "is attached" not in msg.content
    assert "are attached" not in msg.content
    assert "was attached" not in msg.content
    assert "were attached" not in msg.content


def test_dropped_files_remove_the_tools_own_attachment_claim_sentence():
    """A kept body must not go on saying "the image is attached to this
    result" right before the note that says it is gone: the SENTENCE carrying
    the platform's own claim is cut out, the marker and the rest of the text
    are untouched, and a body without such a claim is unchanged."""
    messages = [ChatMessage(role="user", content="q")]
    messages += _image_batch(1, 1)
    messages[-1].content = (
        "File: diagram-1-0.png (image/png, 377.5 KB) - the image is attached to "
        "this result. [[FILE:file-1-0]]"
    )
    messages += [
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="s1", name="capture_screenshot", arguments={})],
        ),
        ChatMessage(
            role="tool",
            content=(
                "Captured. The PNG is attached as a native vision block you can "
                "read. Include this EXACT Markdown: ![shot](/api/v1/chat/files/shot-1/download)"
            ),
            tool_call_id="s1",
            name="capture_screenshot",
            files=[_shot_file("desktop.png", file_id="shot-1")],
        ),
    ]
    messages += _image_batch(3, 1)
    messages += _image_batch(4, 1)

    _clear_stale_tool_result_files(messages)

    first, shot = _tool_msgs(messages)[0], _tool_msgs(messages)[1]
    assert first.files is None and shot.files is None
    # The claim sentence is gone, the marker after it survives.
    assert _outside_blocks(first.content).startswith("[[FILE:file-1-0]]\n\n[Image(s) removed")
    _assert_states_one_thing_about_the_pixels(first)
    # The screenshot keeps its instructions and the Markdown the reply must
    # carry; only the attachment sentence is removed.
    assert shot.content.startswith(
        "Captured. Include this EXACT Markdown: ![shot](/api/v1/chat/files/shot-1/download)"
    )
    _assert_states_one_thing_about_the_pixels(shot)
    _assert_self_describing_stub(first, "diagram-1-0.png", "file-1-0")
    _assert_self_describing_stub(shot, "desktop.png", "shot-1")
    # The note's own "no longer attached to this result" is not a claim.
    assert first.content.count("attached to this result") == 1
    # Idempotent: a second pass leaves the rewritten body alone.
    before = first.content
    assert _clear_stale_tool_result_files(messages) == 0
    assert first.content == before


def test_dropped_files_note_is_the_only_statement_when_the_body_was_the_claim():
    """``read_file_content`` on an image says nothing but the claim; once the
    pixels are dropped the message IS the note: stored, not in context, how
    to view it."""
    messages = [ChatMessage(role="user", content="q")]
    messages += _image_batch(1, 1)
    messages[-1].name = "read_file_content"
    messages[-1].content = (
        "File: diagram-1-0.png (image/png, 377.5 KB) - the image is attached to "
        "this result."
    )
    messages += _image_batch(2, 1)
    messages += _image_batch(3, 1)

    _clear_stale_tool_result_files(messages)

    first = _tool_msgs(messages)[0]
    assert first.files is None
    assert _outside_blocks(first.content).startswith("[Image(s) removed from the working context")
    assert first.content.endswith("]")
    assert "stored but not in context; call read_file_content(file_id=" in first.content
    _assert_states_one_thing_about_the_pixels(first)
    _assert_self_describing_stub(first, "diagram-1-0.png", "file-1-0")


def test_dropped_files_note_for_a_seen_result_dates_the_view_and_quotes_the_notes():
    """A result the model has viewed (``seen_at_iteration`` set by the loop's
    consumed-tracking) loses its pixels with a note that says WHEN it was
    viewed and WHAT the model wrote about it - and never invites another
    ``read_file_content`` call, the invitation that was the 25 Sep 2026 loop.
    The file id stays in the note, so the notes tool still has its
    handle."""
    from app.tools.agent_loop import _drop_tool_result_files

    messages = [ChatMessage(role="user", content="q")]
    messages += _image_batch(1, 2)
    seen = _tool_msgs(messages)[0]
    seen.seen_at_iteration = 4
    seen.files[0].notes = "Founders card: two circular portraits; the ANSSI seal is the old one]"

    note = _drop_tool_result_files(seen)

    assert seen.files is None
    line = _outside_blocks(note)
    assert line.startswith("[Image(s) removed from the working context to save space")
    assert note.endswith("]")
    assert line.count("]") == 1, "the notes' own bracket is neutralised so the regex still finds the note"
    # The name and the saved notes are untrusted data: only the id is in the line.
    assert "diagram-1-0.png" not in line and "Founders card" not in line
    assert "Already viewed at step 4" in note
    assert "your notes: Founders card: two circular portraits; the ANSSI seal is the old one)" in note
    assert "file_id file-1-0" in note
    assert "Work from your notes" in note
    assert "call read_file_content" not in note
    assert "Do NOT call the download" not in note
    _assert_states_one_thing_about_the_pixels(seen)

    # A seen result WITHOUT notes says so and asks for them next time - still
    # without inviting a re-read.
    other = _tool_msgs(messages)[1]
    other.seen_at_iteration = 4
    note = _drop_tool_result_files(other)
    assert "no notes saved" in note
    assert "annotate_file" in note
    assert "call read_file_content" not in note


def test_mark_tool_results_consumed_dates_a_result_once():
    from app.tools.agent_loop import _mark_tool_results_consumed

    messages = [ChatMessage(role="user", content="q")]
    messages += _image_batch(1, 1)
    result = _tool_msgs(messages)[0]
    consumed: dict = {}
    _mark_tool_results_consumed(messages, consumed, iteration=3)
    assert result.seen_at_iteration == 3
    _mark_tool_results_consumed(messages, consumed, iteration=7)
    assert result.seen_at_iteration == 3, "the first view is the one that counts"
    undated = ChatMessage(role="tool", content="x", tool_call_id="u", name="t")
    _mark_tool_results_consumed([undated], {})
    assert undated.seen_at_iteration is None, "the caller's history is marked without a step"
    # A result the loop dated at its append (the step its tool ran in) is
    # viewed at THAT step, not at the later request that carried it - the
    # number the tool's own "already viewed at step N" answer uses.
    appended = ChatMessage(role="tool", content="x", tool_call_id="a", name="t")
    appended.appended_at_iteration = 4
    _mark_tool_results_consumed([appended], {}, iteration=5)
    assert appended.seen_at_iteration == 4


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # The whole body was the claim.
        (
            "File: d.png (image/png, 377.5 KB) - the image is attached to this result.",
            "",
        ),
        # Browser screenshot: the claim sentence goes, instructions + Markdown stay.
        (
            "Screenshot captured (2500000 bytes) and saved as a file. The PNG is "
            "attached as a native vision block - you can see the pixels. Read the "
            "page yourself (URL bar, labels, errors). Also include this EXACT "
            "Markdown: ![Screenshot](/api/v1/chat/files/s1/download)",
            "Screenshot captured (2500000 bytes) and saved as a file. Read the page "
            "yourself (URL bar, labels, errors). Also include this EXACT Markdown: "
            "![Screenshot](/api/v1/chat/files/s1/download)",
        ),
        # Runner capture: "on this tool result" variant.
        (
            "Full-desktop screenshot captured on runner 'audit-box'.\nThe PNG is "
            "attached as a native vision block on this tool result - you can see "
            "the pixels. Read the URL bar yourself.\nsource_file_id: abc",
            "Full-desktop screenshot captured on runner 'audit-box'.\nRead the URL "
            "bar yourself.\nsource_file_id: abc",
        ),
        # Rendered pages: plural claim spanning to "LOOK at them."
        (
            "Rendered page(s) 1, 2 of **deck.pdf** (12 pages, 1.2 MB). The PNGs are "
            "attached to this result as native vision blocks — LOOK at them. Colours "
            "you name from a rendered page are ESTIMATES.",
            "Rendered page(s) 1, 2 of **deck.pdf** (12 pages, 1.2 MB). Colours you "
            "name from a rendered page are ESTIMATES.",
        ),
        # A claim mid-sentence takes its whole sentence; the list after it stays.
        (
            "Extracted the 3 smallest as platform files — the images among them are "
            "attached to this result, so you can see which one is the logo. Bind it "
            "with attach_template_asset using its file id:\n- logo.png (file_id a)\n"
            "- mark.png (file_id b)",
            "Bind it with attach_template_asset using its file id:\n- logo.png "
            "(file_id a)\n- mark.png (file_id b)",
        ),
        # download_opencti_file: the claim is the last line of a metadata block.
        (
            "File: import/global/report.pdf\nContent-Type: application/pdf\n"
            "Size: 48213 bytes\n\nThe file has been attached for analysis.",
            "File: import/global/report.pdf\nContent-Type: application/pdf\nSize: 48213 bytes",
        ),
        # HackerOne report PDF: "attached to this conversation" variant.
        (
            "Generated the PDF for HackerOne report #4242 (47 KB). It is attached "
            "to this conversation for analysis.",
            "Generated the PDF for HackerOne report #4242 (47 KB).",
        ),
        # No claim: untouched, whitespace included.
        (
            "Downloaded 'x.png' (377.5 KB) from Notion as a reusable file. "
            "[[FILE:f1]]\n\nForward it to another tool with file_id=f1.",
            "Downloaded 'x.png' (377.5 KB) from Notion as a reusable file. "
            "[[FILE:f1]]\n\nForward it to another tool with file_id=f1.",
        ),
        # The note's own wording is not a claim.
        (
            "[Image(s) removed from the working context to save space - no longer "
            "attached to this result: d.png (file_id x).]",
            "[Image(s) removed from the working context to save space - no longer "
            "attached to this result: d.png (file_id x).]",
        ),
    ],
)
def test_strip_attachment_claims_cuts_the_whole_sentence(body, expected):
    from app.tools.agent_loop import _strip_attachment_claims

    assert _strip_attachment_claims(body) == expected


def test_claims_are_only_stripped_from_a_body_that_is_losing_files():
    """The strip runs at the ONE moment the pixels go (``_drop_tool_result_files``
    with files attached). A file-less result — an error saying "no file 'x' is
    attached to this conversation" — is never rewritten, and a body that
    already lost its files on an earlier pass is not re-cut."""
    from app.tools.agent_loop import _drop_tool_result_files

    error = ChatMessage(
        role="tool",
        content="Error: no file 'x.png' is attached to this conversation. Use list_files.",
        tool_call_id="e1",
        name="read_file_content",
    )
    assert _drop_tool_result_files(error) == ""
    assert error.content == (
        "Error: no file 'x.png' is attached to this conversation. Use list_files."
    )

    pdf = ChatMessage(
        role="tool",
        content=(
            "Generated the PDF for HackerOne report #4242 (47 KB). It is attached "
            "to this conversation for analysis."
        ),
        tool_call_id="p1",
        name="download_hackerone_report_pdf",
        files=[FileRef.from_bytes(b"%PDF-1.4", "report-4242.pdf", "application/pdf", file_id="pdf-1")],
    )
    note = _drop_tool_result_files(pdf)
    assert pdf.files is None
    assert pdf.content == f"Generated the PDF for HackerOne report #4242 (47 KB).\n\n{note}"
    assert _outside_blocks(note).startswith("[File(s) removed from the working context")
    _assert_self_describing_stub(pdf, "report-4242.pdf", "pdf-1")
    _assert_states_one_thing_about_the_pixels(pdf)
    # Second pass on the already-stripped message: nothing to drop, nothing
    # to re-cut, no second note.
    before = pdf.content
    assert _drop_tool_result_files(pdf) == note
    assert pdf.content == before


def test_clear_stale_tool_result_files_oversize_batch_keeps_first_that_fit():
    """(d) The latest batch alone exceeds the byte budget: the first M results
    that fit (in call order) keep their pixels, the rest are stubbed — never
    silently."""
    messages = [ChatMessage(role="user", content="q")]
    # Five 1 MB images in one batch against a 2.5 MB budget: the first two
    # fit, the third crosses the budget, and everything after it is dropped
    # (recency cliff — nothing smaller slips back under the budget).
    messages += _image_batch(1, 5, size=1024 * 1024)

    dropped = _clear_stale_tool_result_files(
        messages, files_budget_bytes=int(2.5 * 1024 * 1024)
    )

    assert dropped == 3
    tool_msgs = _tool_msgs(messages)
    assert tool_msgs[0].files and tool_msgs[1].files
    for i in (2, 3, 4):
        _assert_self_describing_stub(tool_msgs[i], f"diagram-1-{i}.png", f"file-1-{i}")


def test_clear_stale_tool_result_files_always_keeps_first_of_latest_batch():
    """A single image bigger than the whole budget is still kept: the model
    must never be left with nothing to look at (mirrors the single-newest
    rule of the text pass)."""
    messages = [ChatMessage(role="user", content="q")]
    messages += _image_batch(1, 2, size=8 * 1024 * 1024)

    dropped = _clear_stale_tool_result_files(messages, files_budget_bytes=1024)

    assert dropped == 1
    tool_msgs = _tool_msgs(messages)
    assert tool_msgs[0].files
    assert tool_msgs[1].files is None


def test_clear_stale_tool_result_files_budget_spent_newest_group_first():
    """The byte budget is shared by the kept groups and spent on the latest
    group first, so an older kept group is what loses out when they do not
    all fit."""
    messages = [ChatMessage(role="user", content="q")]
    messages += _image_batch(1, 2, size=1024 * 1024)  # older kept group
    messages += _image_batch(2, 2, size=1024 * 1024)  # latest group

    _clear_stale_tool_result_files(messages, files_budget_bytes=3 * 1024 * 1024)

    latest = [m for m in _tool_msgs(messages) if m.tool_call_id.startswith("b2-")]
    older = [m for m in _tool_msgs(messages) if m.tool_call_id.startswith("b1-")]
    assert all(m.files for m in latest)
    # 3 MB budget: latest batch takes 2 MB, the first result of the older
    # batch fits (3 MB), the second does not.
    assert older[0].files
    assert older[1].files is None


# ---------------------------------------------------------------------------
# ``is_unseen`` — a result the model has not been sent yet is never curated
# (the same-turn strip of 24 Sept 2026: the budget was spent WITHIN the
# just-returned batch, so its second image was gone before the model saw it)
# ---------------------------------------------------------------------------


def _unseen_latest_batch(messages: list[ChatMessage]):
    """The predicate the loop hands in, for a transcript whose latest
    tool-call group is what the previous iteration appended."""
    from app.tools.agent_loop import _mark_tool_results_consumed, _unseen_tool_result_predicate

    consumed: dict[int, ChatMessage] = {}
    groups = _tool_groups(messages)
    _mark_tool_results_consumed(
        [m for g in groups[:-1] for m in g], consumed
    )
    return _unseen_tool_result_predicate(consumed)


def _tool_groups(messages: list[ChatMessage]) -> list[list[ChatMessage]]:
    groups: list[list[ChatMessage]] = []
    current: list[ChatMessage] = []
    for m in messages:
        if m.role == "tool":
            current.append(m)
        elif current:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def test_unseen_files_are_never_stripped_whatever_the_budget():
    """The incident batch shape — [screenshot alone over the budget, read] —
    keeps BOTH images when the batch is unseen; the budget is then spent by
    the unseen batch and it is the older, SEEN group that loses its pixels."""
    messages = [ChatMessage(role="user", content="q")]
    messages += _image_batch(1, 1, size=1024 * 1024)  # seen: the earlier screenshot
    messages += _image_batch(2, 2, size=1024 * 1024)  # unseen: the just-returned batch
    is_unseen = _unseen_latest_batch(messages)

    dropped = _clear_stale_tool_result_files(
        messages, files_budget_bytes=1024, is_unseen=is_unseen
    )

    latest = [m for m in _tool_msgs(messages) if m.tool_call_id.startswith("b2-")]
    older = [m for m in _tool_msgs(messages) if m.tool_call_id.startswith("b1-")]
    assert dropped == 1
    assert all(m.files for m in latest), "an unseen result keeps its pixels, whatever it weighs"
    assert older[0].files is None
    _assert_self_describing_stub(older[0], "diagram-1-0.png", "file-1-0")


def test_unseen_oversize_batch_is_kept_whole_and_the_floor_yields_to_it():
    """Without the predicate the structural floor keeps the first result of
    the latest batch and the budget strips the rest (the pre-fix contract,
    still what a caller outside the loop gets); with it, the whole unseen
    batch is kept and nothing else is special-cased."""
    messages = [ChatMessage(role="user", content="q")]
    messages += _image_batch(1, 5, size=1024 * 1024)
    is_unseen = _unseen_latest_batch(messages)

    dropped = _clear_stale_tool_result_files(
        messages, files_budget_bytes=int(2.5 * 1024 * 1024), is_unseen=is_unseen
    )

    assert dropped == 0
    assert all(m.files for m in _tool_msgs(messages))
    assert all("removed from the working context" not in m.content for m in _tool_msgs(messages))


def test_unseen_results_are_kept_verbatim_by_the_text_pass():
    """A batch of large reads the model has not seen is sent whole; its bytes
    count first, so the older results are what the live budget clears."""
    messages = [ChatMessage(role="user", content="q")]
    messages += [_tool_msg(1, 900), _tool_msg(2, 900)]
    messages.append(ChatMessage(role="assistant", content="", tool_calls=[]))
    messages += [_tool_msg(3, 900), _tool_msg(4, 900), _tool_msg(5, 900)]
    is_unseen = _unseen_latest_batch(messages)

    _clear_stale_tool_results(messages, live_budget_chars=1_000, is_unseen=is_unseen)

    tool_msgs = _tool_msgs(messages)
    assert all(m.content.startswith("result-") for m in tool_msgs[2:]), "unseen batch verbatim"
    assert all(m.content.startswith("[Earlier list_x result") for m in tool_msgs[:2])


def test_unseen_results_are_skipped_by_the_proactive_prune():
    """The proactive compaction gate runs in the same iteration as the
    curation passes, before the request: an unseen batch wider than
    ``_RECENT_TOOL_RESULTS_KEEP`` keeps every body (and every file); the
    seen, older results are what the prune compresses."""
    from app.tools.agent_loop import _RECENT_TOOL_RESULTS_KEEP

    messages = [ChatMessage(role="user", content="q")]
    messages += _image_batch(1, 2)  # seen
    for m in messages[-2:]:
        m.content += "\n" + ("S" * 900)
    messages += _image_batch(2, _RECENT_TOOL_RESULTS_KEEP + 2)  # unseen, wider than the keep window
    for m in messages[-(_RECENT_TOOL_RESULTS_KEEP + 2) :]:
        m.content += "\n" + ("U" * 900)
    is_unseen = _unseen_latest_batch(messages)

    _prune_session_context(messages, max_session_chars=1_000, is_unseen=is_unseen)

    unseen = [m for m in _tool_msgs(messages) if m.tool_call_id.startswith("b2-")]
    seen = [m for m in _tool_msgs(messages) if m.tool_call_id.startswith("b1-")]
    assert all(m.files and "U" * 900 in m.content for m in unseen)
    assert all(m.files is None and m.content.startswith("[Pruned tool result") for m in seen)
    for i, m in enumerate(seen):
        _assert_self_describing_stub(m, f"diagram-1-{i}.png", f"file-1-{i}")


def test_unseen_copy_is_never_the_one_collapsed():
    """Exact duplicates: the older SEEN copy is stubbed, an unseen copy never
    is — even when, by transcript order, it is not the newest."""
    dup = "DUP-" + ("A" * 800)
    seen = ChatMessage(role="tool", content=dup, tool_call_id="t1", name="list_x")
    unseen = ChatMessage(role="tool", content=dup, tool_call_id="t2", name="list_x")
    newest = ChatMessage(role="tool", content=dup, tool_call_id="t3", name="list_x")
    messages = [ChatMessage(role="user", content="q"), seen, unseen, newest]

    _collapse_redundant_tool_results(messages, is_unseen=lambda m: m is unseen)

    assert seen.content.startswith("[Duplicate list_x result collapsed")
    assert unseen.content == dup
    assert newest.content == dup


def test_unseen_predicate_is_identity_based_and_marks_the_whole_transcript():
    """Consumed-tracking is by object identity — a same-valued copy is not the
    message the model was sent — and marking records every tool result of
    the transcript, so nothing appended before a completed call stays
    protected after it."""
    import dataclasses

    from app.tools.agent_loop import _mark_tool_results_consumed, _unseen_tool_result_predicate

    consumed: dict[int, ChatMessage] = {}
    is_unseen = _unseen_tool_result_predicate(consumed)
    history = _tool_msg(1, 10)
    fresh = _tool_msg(2, 10)
    user = ChatMessage(role="user", content="q")

    _mark_tool_results_consumed([user, history], consumed)
    assert not is_unseen(history)
    assert not is_unseen(user), "only tool results are ever unseen"
    assert is_unseen(fresh)
    assert is_unseen(dataclasses.replace(history)), "a copy is not the sent object"

    _mark_tool_results_consumed([user, history, fresh], consumed)
    assert not is_unseen(fresh)
    # The map keeps the objects alive: an id can never be recycled onto a
    # new, unseen result.
    assert consumed[id(fresh)] is fresh


def test_tool_result_files_budget_is_window_scaled_with_floor_and_cap():
    assert _compute_tool_result_files_budget(1_000) == _TOOL_RESULT_FILES_LIVE_MIN_BYTES
    assert _compute_tool_result_files_budget(50_000_000) == _TOOL_RESULT_FILES_LIVE_MAX_BYTES
    mid = _compute_tool_result_files_budget(400_000)
    assert _TOOL_RESULT_FILES_LIVE_MIN_BYTES <= mid <= _TOOL_RESULT_FILES_LIVE_MAX_BYTES
    # The incident batch (3 x 377.5 KB) fits the floor with room to spare.
    assert _compute_tool_result_files_budget(128_000) > 3 * 377_500


def test_clear_stale_tool_results_file_bearing_stub_points_to_read_file_content():
    """(D) A large tool result whose value was an attached file gets a stub
    that names the file id and ``read_file_content`` — not the "stale and
    re-fetchable, re-call the tool" wording that sent the incident model back
    to the download tool."""
    big_download = "Downloaded 'diagram.png' from Notion. [[FILE:file-x]] " + ("A" * 900)
    messages = [
        ChatMessage(role="user", content="q"),
        ChatMessage(
            role="tool",
            content=big_download,
            tool_call_id="t1",
            name="download_notion_file",
            files=[_shot_file("diagram.png", file_id="file-x")],
        ),
        _tool_msg(2, 900),
        _tool_msg(3, 900),
    ]

    _clear_stale_tool_results(messages, live_budget_chars=1_000)

    stubbed = messages[1]
    assert stubbed.content.startswith("[Earlier download_notion_file result")
    _assert_self_describing_stub(stubbed, "diagram.png", "file-x")
    # The plain-text result keeps the plain-text stub.
    assert "re-fetchable" in messages[2].content


def test_clear_stale_tool_results_preserves_an_earlier_files_note():
    """A message whose files were already dropped (note appended) keeps that
    note when the text pass later stubs its body."""
    messages = [ChatMessage(role="user", content="q")]
    messages += _image_batch(1, 1)
    # Pad the download text so the text pass considers it worth clearing.
    messages[-1].content += "\n" + ("B" * 900)
    messages += _image_batch(2, 1)
    messages += _image_batch(3, 1)
    _clear_stale_tool_result_files(messages)  # drops batch 1's pixels + note
    first = _tool_msgs(messages)[0]
    assert first.files is None and "read_file_content" in first.content

    messages += [_tool_msg(9, 900), _tool_msg(10, 900)]
    _clear_stale_tool_results(messages, live_budget_chars=1_000)

    assert first.content.startswith("[Earlier download_notion_file result")
    _assert_self_describing_stub(first, "diagram-1-0.png", "file-1-0")
    assert first.content.count("removed from the working context") == 1


def test_emergency_prune_truncated_tool_result_names_dropped_files():
    """Phase 2 of the emergency prune drops files from a truncated tool
    result; the truncated body must still name them and read_file_content,
    and the phase stays size-monotonic."""
    huge = "X" * 5_000
    messages = [
        ChatMessage(role="system", content="persona"),
        ChatMessage(role="user", content="q"),
        ChatMessage(
            role="tool",
            content=huge,
            tool_call_id="t1",
            name="computer_screenshot",
            files=[_shot_file("desktop.png", file_id="shot-1")],
        ),
    ]

    _emergency_prune_session_context(messages, max_session_chars=2_500)

    shot = messages[2]
    assert len(shot.content) < len(huge)
    _assert_self_describing_stub(shot, "desktop.png", "shot-1")
    # Same choke point as every other drop site: one note, never stacked, and
    # a second pass over the already-stubbed message is a no-op for the files.
    assert shot.content.count("removed from the working context") == 1
    before = shot.content
    _emergency_prune_session_context(messages, max_session_chars=2_500)
    assert shot.files is None
    assert shot.content.count("removed from the working context") == 1
    assert len(shot.content) <= len(before)


def test_collapse_redundant_drops_files_on_stubbed_copy():
    dup = "DUP-" + ("A" * 800)
    messages = [
        ChatMessage(
            role="tool",
            content=dup,
            tool_call_id="t1",
            name="list_x",
            files=[_shot_file()],
        ),
        ChatMessage(
            role="tool",
            content=dup,
            tool_call_id="t2",
            name="list_x",
            files=[_shot_file()],
        ),
    ]

    _collapse_redundant_tool_results(messages)

    assert messages[0].content.startswith("[Duplicate ")
    assert messages[0].files is None
    assert messages[1].files
    # The collapsed copy says which file it lost and how to see it again.
    assert "desktop.png" in messages[0].content
    assert "read_file_content" in messages[0].content


def test_prune_session_context_drops_files_on_stubbed_results():
    huge = "X" * 5_000
    messages = [ChatMessage(role="user", content="q")]
    for i in range(6):
        messages.append(
            ChatMessage(
                role="tool",
                content=huge,
                tool_call_id=f"t{i}",
                name="read_file",
                files=[_shot_file()],
            )
        )

    _prune_session_context(messages, max_session_chars=8_000)

    stubbed = [m for m in messages if m.role == "tool" and m.content.startswith("[Pruned")]
    assert stubbed
    assert all(m.files is None for m in stubbed)
    # A pruned file-bearing result still names its file and read_file_content.
    assert all("desktop.png" in m.content for m in stubbed)
    assert all("read_file_content" in m.content for m in stubbed)


def test_prune_session_context_without_the_predicate_is_the_count_alone():
    """The pre-fix contract, still what a caller without the predicate gets
    (the emergency prune's phase 1): the count prunes the first results of a
    wider just-returned batch, image included — which is exactly why the two
    in-loop callers pass ``is_unseen``."""
    messages = [ChatMessage(role="user", content="q")]
    messages += [_tool_msg(1, 300), _tool_msg(2, 300)]
    messages.append(ChatMessage(role="assistant", content="", tool_calls=[]))
    batch = [_tool_msg(10 + i, 300, name="read_file_content") for i in range(6)]
    batch[0].files = [_shot_file("first-of-batch.png", file_id="first-1")]
    messages += batch

    _prune_session_context(messages, max_session_chars=1_000)

    tool_msgs = _tool_msgs(messages)
    assert all(m.content.startswith("[Pruned tool result") for m in tool_msgs[:4])
    assert all(m.content.startswith("result-") for m in tool_msgs[4:])
    assert tool_msgs[2].files is None
    _assert_self_describing_stub(tool_msgs[2], "first-of-batch.png", "first-1")


# ---------------------------------------------------------------------------
# Stall guard — integration through run_agent_loop
# ---------------------------------------------------------------------------


def _work_tools() -> tuple[list[ToolDefinition], CallableToolExecutor]:
    executor = CallableToolExecutor()

    async def _do_work(**_kwargs):
        return "work done"

    executor.register("do_work", _do_work)
    tools = [
        ToolDefinition(
            name="do_work",
            description="Perform a unit of work.",
            input_schema={"type": "object", "properties": {}},
            source="test",
        )
    ]
    return tools, executor


class _ScriptedLLM(BaseLLMProvider):
    """LLM stub that returns a scripted sequence of responses per ``chat`` call."""

    provider_name = "stub"
    context_window = 200_000

    def __init__(self, script):
        self._script = script
        self.calls = 0

    async def chat(
        self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs
    ):
        idx = self.calls
        self.calls += 1
        return self._script(idx, model)

    async def chat_stream(
        self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs
    ):
        yield "x"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


@pytest.mark.asyncio
async def test_stall_guard_reprompts_then_finishes():
    """A mid-task tool-free narration is re-prompted to execute; the loop does
    NOT surface the checkpoint and returns the eventual real final answer."""
    tool_call = ToolCall(id="t1", name="do_work", arguments={})

    def script(idx, model):
        if idx == 0:
            return ChatResponse(
                content="", model=model or "stub", tool_calls=[tool_call],
                stop_reason="tool_use",
            )
        if idx == 1:
            return ChatResponse(
                content="Done analyzing. Executing the writes now.",
                model=model or "stub",
            )
        return ChatResponse(
            content="All owners assigned. Task complete.", model=model or "stub"
        )

    llm = _ScriptedLLM(script)
    tools, executor = _work_tools()
    messages = [ChatMessage(role="user", content="Assign all owners")]

    response = await run_agent_loop(
        llm=llm,
        messages=messages,
        tools=tools,
        executor=executor,
        max_iterations=6,
    )

    assert response.content == "All owners assigned. Task complete."
    assert llm.calls == 3  # tool call -> narration (nudged) -> real answer
    assert any(
        _STALL_CONTINUATION_NUDGE in (m.content or "") for m in messages
    )


@pytest.mark.asyncio
async def test_stall_guard_is_bounded_when_model_keeps_narrating():
    """If the model keeps narrating, the guard nudges at most
    ``_MAX_STALL_CONTINUATIONS`` times then returns the narration — never an
    infinite loop."""
    tool_call = ToolCall(id="t1", name="do_work", arguments={})
    narration = "Executing the writes now."

    def script(idx, model):
        if idx == 0:
            return ChatResponse(
                content="", model=model or "stub", tool_calls=[tool_call],
                stop_reason="tool_use",
            )
        return ChatResponse(content=narration, model=model or "stub")

    llm = _ScriptedLLM(script)
    tools, executor = _work_tools()
    messages = [ChatMessage(role="user", content="Assign all owners")]

    response = await run_agent_loop(
        llm=llm,
        messages=messages,
        tools=tools,
        executor=executor,
        max_iterations=12,
    )

    assert response.content == narration
    nudges = sum(
        1 for m in messages if (m.content or "") == _STALL_CONTINUATION_NUDGE
    )
    assert nudges == _MAX_STALL_CONTINUATIONS


@pytest.mark.asyncio
async def test_stall_guard_composing_event_carries_stall_reason():
    """The stall-guard re-prompt emits ``composing`` tagged with
    reason="stall_continuation" so truncation latches (chat WS, platform
    proxy) can tell it apart from budget-driven wind-down."""
    tool_call = ToolCall(id="t1", name="do_work", arguments={})

    def script(idx, model):
        if idx == 0:
            return ChatResponse(
                content="", model=model or "stub", tool_calls=[tool_call],
                stop_reason="tool_use",
            )
        if idx == 1:
            return ChatResponse(
                content="Done analyzing. Executing the writes now.",
                model=model or "stub",
            )
        return ChatResponse(
            content="All owners assigned. Task complete.", model=model or "stub"
        )

    llm = _ScriptedLLM(script)
    tools, executor = _work_tools()
    messages = [ChatMessage(role="user", content="Assign all owners")]
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)

    response = await run_agent_loop(
        llm=llm,
        messages=messages,
        tools=tools,
        executor=executor,
        max_iterations=25,
        on_event=on_event,
    )

    assert response.content == "All owners assigned. Task complete."
    composing = [e for e in events if e.get("status") == "composing"]
    # The only composing event on this run is the stall-guard re-prompt —
    # the budget (25) was never approached, so no wind-down/strip fired.
    assert composing
    assert all(e.get("reason") == "stall_continuation" for e in composing)
    assert not any(e.get("status") == "wind_down" for e in events)


@pytest.mark.asyncio
async def test_budget_driven_composing_event_has_no_stall_reason():
    """Genuine budget-driven tool stripping keeps emitting a bare
    ``composing`` event (no stall reason), so truncation latches still flag
    those turns."""
    tool_call = ToolCall(id="t1", name="do_work", arguments={})

    class _ToolHungryLLM(_ScriptedLLM):
        """Calls a tool on every turn until tools are stripped."""

        async def chat(
            self, messages, model=None, temperature=0.7, max_tokens=4096,
            tools=None, **kwargs,
        ):
            self.calls += 1
            if tools:
                return ChatResponse(
                    content="", model=model or "stub", tool_calls=[tool_call],
                    stop_reason="tool_use",
                )
            return ChatResponse(
                content="Best-effort summary.", model=model or "stub"
            )

    llm = _ToolHungryLLM(lambda idx, model: None)
    tools, executor = _work_tools()
    messages = [ChatMessage(role="user", content="Assign all owners")]
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)

    response = await run_agent_loop(
        llm=llm,
        messages=messages,
        tools=tools,
        executor=executor,
        max_iterations=6,
        on_event=on_event,
    )

    assert response.content == "Best-effort summary."
    composing = [e for e in events if e.get("status") == "composing"]
    assert composing
    assert all(e.get("reason") != "stall_continuation" for e in composing)


@pytest.mark.asyncio
async def test_no_stall_nudge_without_prior_tool_use():
    """A turn that never used a tool is never forced to continue, even when it
    matches the present-tense execution pattern (gated on had_tool_calls_this_run)."""

    def script(idx, model):
        return ChatResponse(
            content="Executing the writes now.", model=model or "stub"
        )

    llm = _ScriptedLLM(script)
    tools, executor = _work_tools()
    messages = [ChatMessage(role="user", content="hi")]

    response = await run_agent_loop(
        llm=llm,
        messages=messages,
        tools=tools,
        executor=executor,
        max_iterations=6,
    )

    assert response.content == "Executing the writes now."
    assert llm.calls == 1  # returned immediately, no nudge
    assert not any(
        _STALL_CONTINUATION_NUDGE in (m.content or "") for m in messages
    )


# ---------------------------------------------------------------------------
# Compaction gate — the proactive prune never stubs an unseen result
# ---------------------------------------------------------------------------


class _TranscriptRecordingLLM(BaseLLMProvider):
    """Scripted LLM that records the tool bodies of every request it is sent."""

    provider_name = "stub"
    context_window = 200_000

    def __init__(self, script):
        self._script = script
        self.tool_bodies_per_request: list[list[str]] = []

    async def chat(self, messages, model=None, tools=None, **kwargs):
        self.tool_bodies_per_request.append(
            [m.content or "" for m in messages if m.role == "tool"]
        )
        return self._script(len(self.tool_bodies_per_request) - 1, model)

    async def chat_stream(self, messages, **kwargs):
        yield "x"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


@pytest.mark.asyncio
async def test_compaction_gate_prune_keeps_the_unseen_batch_whole(monkeypatch):
    """Drive the loop with the compaction gate forced open on every iteration
    (trigger and target utilisation at 0). Two parallel batches of six
    300-char reads: the request after batch A carries all six verbatim (the
    count alone would have stubbed the first two before the model saw them),
    and the request after batch B carries B whole while A — seen by then AND
    written about by the model (a seen result the model has not
    referenced is protected too, see ``test_turn_pruning_and_recall``) — is
    what the prune stubs."""
    from app.tools import agent_loop as al

    monkeypatch.setattr(al, "_COMPACTION_TRIGGER_UTILIZATION", 0.0)
    monkeypatch.setattr(al, "_COMPACTION_TARGET_UTILIZATION", 0.0)

    executor = CallableToolExecutor()

    async def _read(n: int, **_kwargs):
        return f"work-{n}-" + ("W" * 300)

    executor.register("read_chunk", _read)
    tools = [
        ToolDefinition(
            name="read_chunk",
            description="Read one chunk.",
            input_schema={
                "type": "object",
                "properties": {"n": {"type": "integer"}},
                "required": ["n"],
            },
            source="test",
        )
    ]

    def _batch(first: int) -> list[ToolCall]:
        return [
            ToolCall(id=f"c{first + i}", name="read_chunk", arguments={"n": first + i})
            for i in range(6)
        ]

    def script(idx, model):
        if idx == 0:
            return ChatResponse(
                content="", model=model or "stub", tool_calls=_batch(0), stop_reason="tool_use"
            )
        if idx == 1:
            # Substantive text about batch A: the model has USED it, so the
            # gate may stub it on the next pass.
            return ChatResponse(
                content=(
                    "Chunks zero to five are read: each is a 300-character work "
                    "block and none of them needs a second pass. Reading the "
                    "second half of the file now, chunks ten to fifteen, before "
                    "putting the whole picture together for the answer. "
                    # Past the loop's preamble line (``_PREAMBLE_HOLDBACK_CHARS``):
                    # an account of the batch, not a stage plan.
                    + "What the first half says: " + "the work blocks agree. " * 32
                ),
                model=model or "stub",
                tool_calls=_batch(10),
                stop_reason="tool_use",
            )
        return ChatResponse(content="Read everything.", model=model or "stub")

    llm = _TranscriptRecordingLLM(script)
    messages = [ChatMessage(role="user", content="Read the twelve chunks.")]

    response = await run_agent_loop(
        llm=llm,
        messages=messages,
        tools=tools,
        executor=executor,
        max_iterations=6,
    )

    assert response.content == "Read everything."
    assert len(llm.tool_bodies_per_request) == 3

    after_a = llm.tool_bodies_per_request[1]
    assert len(after_a) == 6
    assert all(body.startswith("work-") for body in after_a), "unseen batch A sent whole"

    after_b = llm.tool_bodies_per_request[2]
    assert len(after_b) == 12
    assert all(body.startswith("[Pruned tool result") for body in after_b[:6]), "seen A gives way"
    assert all(body.startswith("work-1") for body in after_b[6:]), "unseen batch B sent whole"


_SIX_CHUNKS_ANSWER = (
    "All six chunks were read: each one is a 300-character work block, numbered "
    "zero to five, and none of them needed a second pass or a follow-up read."
)


@pytest.mark.asyncio
async def test_forced_answer_prune_keeps_the_unseen_batch_whole(monkeypatch):
    """The wall-clock guard can end the loop right after a wide batch was
    appended, before any request carried it; the forced-answer backstop's
    prune (``max_session_chars`` tiny, so it fires) skips that batch, and the
    synthesis request is what shows it — whole. The count alone would have
    stubbed its first two results (``_RECENT_TOOL_RESULTS_KEEP`` = 4)."""
    from app.tools import agent_loop as al

    executor = CallableToolExecutor()

    async def _read(n: int, **_kwargs):
        return f"work-{n}-" + ("W" * 300)

    executor.register("read_chunk", _read)
    tools = [
        ToolDefinition(
            name="read_chunk",
            description="Read one chunk.",
            input_schema={
                "type": "object",
                "properties": {"n": {"type": "integer"}},
                "required": ["n"],
            },
            source="test",
        )
    ]

    class _GuardTrippingLLM(BaseLLMProvider):
        """Asks for six reads on its first call and raises the in-loop
        wall-clock guard above the remaining budget as it does, so the NEXT
        iteration breaks to the backstop before any request carries them.
        The backstop call (the ``_FORCED_FINAL_ANSWER`` marker) records the
        tool bodies it is handed."""

        provider_name = "stub"
        context_window = 200_000

        def __init__(self) -> None:
            self.calls = 0
            self.forced_tool_bodies: list[str] | None = None

        async def chat(self, messages, model=None, tools=None, **kwargs):
            self.calls += 1
            if any((m.content or "") == al._FORCED_FINAL_ANSWER for m in messages):
                self.forced_tool_bodies = [m.content or "" for m in messages if m.role == "tool"]
                # Long enough for the backstop's "too short" check, or it
                # would retry the synthesis with a stronger prompt.
                return ChatResponse(content=_SIX_CHUNKS_ANSWER, model=model or "stub")
            monkeypatch.setattr(al, "_IN_LOOP_MIN_REMAINING_SECS", 10_000)
            return ChatResponse(
                content="",
                model=model or "stub",
                stop_reason="tool_use",
                tool_calls=[
                    ToolCall(id=f"c{i}", name="read_chunk", arguments={"n": i}) for i in range(6)
                ],
            )

        async def chat_stream(self, messages, **kwargs):
            yield "x"

        async def embed(self, text, model=None):
            return [0.0]

        async def list_models(self):
            return []

    llm = _GuardTrippingLLM()
    response = await run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="Read the six chunks.")],
        tools=tools,
        executor=executor,
        max_iterations=10,
        max_session_chars=100,  # every transcript with results is over budget
        progressive_tools=False,
        wall_clock_deadline=time.monotonic() + 600.0,
    )

    assert response.content == _SIX_CHUNKS_ANSWER
    assert llm.calls == 2, "one in-loop call, then the backstop synthesis"
    assert llm.forced_tool_bodies is not None and len(llm.forced_tool_bodies) == 6
    assert all(body.startswith("work-") for body in llm.forced_tool_bodies), (
        "the unseen batch reaches the synthesis whole"
    )
