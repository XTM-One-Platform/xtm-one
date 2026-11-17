"""A tool result an interrupted turn kept whole for its resume is never a deliverable (#9874).

``turn_checkpoint._spill`` keeps every replayed result over the cut as a
conversation file (source ``agent_checkpoint``) the resuming agent reads by
id. #9398 left those files out of the files panel; the resumed turn still
registered them as files of the conversation, and a reply that named one put
it under the answer as a download card of raw tool output. They are internal:
never carried by a reply, named or not, and shown as a working file wherever a
row is turned into a chip.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.llm.base import (
    CHECKPOINT_FILE_SOURCE,
    INTERNAL_FILE_SOURCES,
    WORKING_FILE_SOURCES,
    FileRef,
    is_internal_file_source,
    is_working_file_source,
)
from app.services import turn_checkpoint
from app.services.chat_messages import attachment_meta_from_file_attachment
from app.tools.base import FileContext, files_for_reply


def _ref(name: str, source: str) -> FileRef:
    return FileRef(
        file_id=str(uuid.uuid4()),
        filename=name,
        content_type="text/plain",
        size=55_000,
        s3_object_name=f"generated/{name}",
        source=source,
    )


def test_the_spill_source_is_the_internal_working_source():
    assert turn_checkpoint.SPILL_SOURCE == CHECKPOINT_FILE_SOURCE
    assert CHECKPOINT_FILE_SOURCE in INTERNAL_FILE_SOURCES
    assert INTERNAL_FILE_SOURCES <= WORKING_FILE_SOURCES
    assert is_internal_file_source("Agent_Checkpoint ")
    assert is_working_file_source(CHECKPOINT_FILE_SOURCE)
    assert not is_internal_file_source("agent_working")
    assert not is_internal_file_source(None)


def test_a_reply_never_carries_a_spill_file_even_when_it_names_it():
    ctx = FileContext()
    spill = _ref("interrupted-turn-result-list_chat_messages-abcd1234.txt", CHECKPOINT_FILE_SOURCE)
    report = _ref("report.pdf", "agent_download")
    ctx.add_fetched(spill)
    ctx.add_produced(report)
    ids = [spill.file_id, report.file_id]

    assert files_for_reply(ctx, ids, "done") == [report.file_id]
    assert files_for_reply(ctx, ids, f"read [[FILE:{spill.file_id}]] and {report.file_id}") == [report.file_id]


def test_a_spill_file_added_plainly_is_still_left_out():
    """A context that did not mark it fetched (a plain ``add``) still drops it."""
    ctx = FileContext()
    spill = _ref("interrupted-turn-result-query_notion_database-9f00aa11.txt", CHECKPOINT_FILE_SOURCE)
    ctx.add(spill)

    assert files_for_reply(ctx, [spill.file_id], spill.file_id) == []


def test_deliverables_and_unknown_ids_are_unchanged():
    ctx = FileContext()
    generated = _ref("export.csv", "agent_generated")
    working = _ref("figure.html", "agent_working")
    ctx.add(generated)
    ctx.add(working)
    unknown = str(uuid.uuid4())

    assert files_for_reply(ctx, [generated.file_id, working.file_id, unknown], None) == [
        generated.file_id,
        working.file_id,
        unknown,
    ]
    assert files_for_reply(None, [generated.file_id], None) == [generated.file_id]


def test_a_spill_row_is_a_working_chip_never_a_download_card():
    row = SimpleNamespace(
        id=uuid.uuid4(),
        filename="interrupted-turn-result-get_notion_page-0011aabb.txt",
        content_type="text/plain",
        size=46_000,
        source=CHECKPOINT_FILE_SOURCE,
        created_at=None,
        sha256=None,
    )

    assert attachment_meta_from_file_attachment(row)["file_tag"] == "working_file"
