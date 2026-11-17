"""Regression tests for ``strip_file_content_from_history``.

Bug: after the *initial* message in a conversation, the agent could no
longer read an attached file.  ``get_conversation_history`` rehydrates
``ChatMessage.files`` from the ``FileAttachment`` rows specifically so the
file content re-inlines on follow-up turns, but
``strip_file_content_from_history`` cleared ``files=None`` on every
history message — so on turn 2+ no message carried the file content while
the system prompt still claimed the content was "ALREADY INCLUDED".  The
LLM was left with only metadata and (correctly) reported it could not read
the file.

These tests pin the contract: legacy inline ``[File: …]`` *text* blocks are
still stripped (to avoid duplicating the authoritative re-inlined copy),
but the structured ``files`` attachments MUST be preserved so the file
resolver re-inlines them on the current turn.
"""

from __future__ import annotations

from app.llm.base import ChatMessage, FileRef
from app.services.agent_chat import strip_file_content_from_history


def _ref(file_id: str = "aaa-111") -> FileRef:
    return FileRef(
        file_id=file_id,
        filename="report.xlsx",
        content_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        size=44_200,
        s3_object_name=f"s3/{file_id}",
    )


def test_preserves_structured_files_on_plain_user_message():
    """The common case (``send_message`` with ``file_ids``): the user
    message text has no inline ``[File:]`` block, the file rides on
    ``ChatMessage.files``.  Stripping MUST keep ``files`` so the content
    re-inlines on follow-up turns — this is the actual regression.
    """
    history = [
        ChatMessage(role="user", content="Here is the ledger", files=[_ref()]),
        ChatMessage(role="assistant", content="Got it."),
    ]
    cleaned = strip_file_content_from_history(history)

    assert cleaned[0].files is not None
    assert len(cleaned[0].files) == 1
    assert cleaned[0].files[0].file_id == "aaa-111"
    # User text is untouched.
    assert cleaned[0].content == "Here is the ledger"


def test_strips_legacy_inline_block_but_keeps_files():
    """The legacy ``upload_file`` flow stores ``[File: name]\n\n<text>`` in
    the message content AND creates a ``FileAttachment`` (rehydrated onto
    ``files``).  The stale text copy is stripped, but ``files`` is kept so
    the resolver re-inlines exactly one authoritative copy (no duplicate).
    """
    history = [
        ChatMessage(
            role="user",
            content="[File: report.xlsx]\n\nrow1,row2,row3\n...big extract...",
            files=[_ref()],
        ),
    ]
    cleaned = strip_file_content_from_history(history)

    # Inline text extract is gone ...
    assert "row1,row2,row3" not in cleaned[0].content
    # ... but the structured attachment survives for re-inlining ...
    assert cleaned[0].files is not None
    assert cleaned[0].files[0].file_id == "aaa-111"
    # ... and we don't emit the misleading "(file content removed)" marker
    # when the content is going to be re-inlined from ``files``.
    assert cleaned[0].content == ""


def test_legacy_block_without_files_keeps_removed_marker():
    """An old-conversation message that only carried inline text (no
    ``FileAttachment`` to rehydrate) has nothing left to re-inline, so the
    explicit "(file content removed)" marker is retained so the LLM knows
    something was there but is no longer available.
    """
    history = [
        ChatMessage(
            role="user",
            content="[File: notes.txt]\n\nsome stale extract",
            files=None,
        ),
    ]
    cleaned = strip_file_content_from_history(history)

    assert cleaned[0].files is None
    assert cleaned[0].content == "(file content removed)"


def test_preserves_assistant_tool_metadata():
    """Stripping must not drop assistant tool-call metadata or tool-result
    identifiers when it rebuilds a message that had an inline block.
    """
    history = [
        ChatMessage(
            role="tool",
            content="[File: out.csv]\n\na,b,c",
            tool_call_id="call-1",
            name="code_interpreter",
            files=[_ref("bbb-222")],
        ),
    ]
    cleaned = strip_file_content_from_history(history)

    assert cleaned[0].tool_call_id == "call-1"
    assert cleaned[0].name == "code_interpreter"
    assert cleaned[0].files is not None
    assert cleaned[0].files[0].file_id == "bbb-222"


def test_preserves_thinking_and_provider_extra_on_rebuild():
    """Rebuilding a message that had an inline block must not drop the
    ``thinking`` / ``_provider_extra`` fields used for multi-turn replay
    (e.g. Gemini raw parts) and reasoning propagation — they must survive
    exactly like the tool metadata.
    """
    history = [
        ChatMessage(
            role="assistant",
            content="[File: out.csv]\n\na,b,c",
            thinking="reasoning trace",
            _provider_extra={"gemini": "raw-parts"},
            files=[_ref("ccc-333")],
        ),
    ]
    cleaned = strip_file_content_from_history(history)

    assert cleaned[0].thinking == "reasoning trace"
    assert cleaned[0]._provider_extra == {"gemini": "raw-parts"}
    # And the inline block is still stripped + files preserved.
    assert "a,b,c" not in cleaned[0].content
    assert cleaned[0].files is not None
    assert cleaned[0].files[0].file_id == "ccc-333"


def test_message_without_files_or_block_passes_through_unchanged():
    """A plain text message with neither inline block nor files is returned
    as-is (same object) — no needless copying.
    """
    msg = ChatMessage(role="user", content="just a question")
    cleaned = strip_file_content_from_history([msg])
    assert cleaned[0] is msg
