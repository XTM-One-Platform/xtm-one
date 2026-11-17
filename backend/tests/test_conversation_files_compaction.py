"""A conversation file stays resolvable after compaction or a resumed turn.

The chat turn used to build its FileContext from the messages that survived
compaction; a file whose carrying message was compacted (or that a cut turn's
replayed tool call stored) answered "not found in this conversation" to every
tool resolving it by id, although its row and bytes still existed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.llm.base import FileRef
from app.services import conversation_files as cf
from app.services.conversation_files import (
    conversation_file_refs,
    register_conversation_files,
)
from app.tools.base import PROMPT_BY_REFERENCE_FILES_LIMIT, FileContext


def _row(
    conv_id: uuid.UUID,
    name: str,
    *,
    minutes: int,
    source: str = "agent_download",
    bound: bool = True,
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        conversation_id=conv_id,
        message_id=uuid.uuid4() if bound else None,
        filename=name,
        content_type="application/pdf",
        size=1024,
        s3_object_name=f"chat/{conv_id}/{uuid.uuid4()}",
        source=source,
        description=None,
        created_at=datetime(2026, 11, 2, 13, 0, tzinfo=UTC) + timedelta(minutes=minutes),
        template_container_id=None,
    )


class _Store:
    """An ``async_session`` stand-in answering the rows newest first, like the query."""

    def __init__(self, rows):
        self.rows = rows
        self.opened = 0

    def __call__(self):
        store = self

        class _Session:
            async def __aenter__(self):
                store.opened += 1
                return self

            async def __aexit__(self, *_exc):
                return False

            async def execute(self, stmt):
                rows = sorted(store.rows, key=lambda r: r.created_at, reverse=True)
                limit = getattr(stmt, "_limit", None)
                if isinstance(limit, int):
                    rows = rows[:limit]
                return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

        return _Session()


@pytest.fixture
def store(monkeypatch):
    holder = _Store([])
    monkeypatch.setattr("app.core.database.async_session", holder)
    return holder


async def _turn_context(conv_id, history_refs, current_refs=()):
    """The chat turn's build (``chat.py``): current files, stored files, then history."""
    ctx = FileContext.from_file_refs(list(current_refs))
    initial = {r.file_id for r in ctx.list_files()}
    register_conversation_files(ctx, await conversation_file_refs(conv_id), initial)
    for ref in history_refs:
        ctx.add(ref)
        initial.add(ref.file_id)
    return ctx, initial


async def test_file_of_a_compacted_turn_still_resolves(store, monkeypatch):
    conv_id = uuid.uuid4()
    pdf = _row(conv_id, "Memorandum.pdf", minutes=0)
    shot = _row(conv_id, "image.png", minutes=30, source="chat")
    store.rows = [pdf, shot]

    async def _download(name):
        return b"%PDF-1.7 bytes" if name == pdf.s3_object_name else b"png"

    monkeypatch.setattr("app.services.file_storage.file_storage.download", _download)

    # Turn N after a compaction that dropped turn 1: only the screenshot's
    # message survives in the history.
    ctx, initial = await _turn_context(conv_id, [FileRef.from_attachment(shot)])

    resolved = await ctx.resolve_upload(str(pdf.id))
    assert resolved == (b"%PDF-1.7 bytes", "Memorandum.pdf", "application/pdf")
    # Known to the turn, so never attached to its reply as a new file.
    assert str(pdf.id) in initial


async def test_microsoft_attachment_forwarding_resolves_after_compaction(store, monkeypatch):
    from app.integrations.providers.microsoft._common import _resolve_platform_file

    conv_id = uuid.uuid4()
    pdf = _row(conv_id, "Memorandum.pdf", minutes=0)
    store.rows = [pdf]

    async def _download(_name):
        return b"%PDF"

    monkeypatch.setattr("app.services.file_storage.file_storage.download", _download)
    ctx, _ = await _turn_context(conv_id, [])

    resolved, error = await _resolve_platform_file(str(pdf.id), ctx)
    assert error is None
    assert resolved is not None and resolved[1] == "Memorandum.pdf"


async def test_file_stored_by_a_replayed_call_resolves_in_the_continuation(store, monkeypatch):
    conv_id = uuid.uuid4()
    earlier = _row(conv_id, "earlier.pdf", minutes=0)
    # The cut turn never stored its reply: no message shows this file yet.
    stored_by_replayed_call = _row(conv_id, "attachment.pdf", minutes=50, bound=False)
    store.rows = [earlier, stored_by_replayed_call]

    async def _download(_name):
        return b"%PDF"

    monkeypatch.setattr("app.services.file_storage.file_storage.download", _download)

    # The resumed turn: the replayed tool messages carry no FileRef (a tool
    # result is text), the history before them was compacted.
    ctx, initial = await _turn_context(conv_id, [])

    for row in (earlier, stored_by_replayed_call):
        assert await ctx.resolve_upload(str(row.id)) is not None
    assert str(earlier.id) in initial
    # Unbound: new to the continuation, so a reply naming it attaches it.
    assert str(stored_by_replayed_call.id) not in initial


async def test_an_unbound_file_reused_by_a_later_turn_is_attached_to_its_reply(store):
    conv_id = uuid.uuid4()
    # A file an earlier turn wrote before it failed: the row exists, no
    # message shows it. A later turn that regenerates the same bytes gets
    # this row back from _find_existing_tool_file and must attach it.
    orphan = _row(conv_id, "report.pdf", minutes=0, bound=False)
    shown = _row(conv_id, "shown.pdf", minutes=5)
    store.rows = [orphan, shown]

    ctx, initial = await _turn_context(conv_id, [])
    reply_file_ids = {r.file_id for r in ctx.list_files() if r.file_id not in initial}

    assert ctx.get(str(orphan.id)) is not None
    assert reply_file_ids == {str(orphan.id)}
    assert str(shown.id) in initial


async def test_an_unbound_file_is_carried_only_when_handed_back_or_named(store):
    """An unbound row enters as fetched: no chip on every later reply.

    A file an earlier turn only fetched and never named stays unbound for
    good; added as a plain file it would be attached to every later reply.
    """
    from app.tools.base import files_for_reply

    conv_id = uuid.uuid4()
    orphan = _row(conv_id, "report.pdf", minutes=0, bound=False)
    store.rows = [orphan]

    ctx, initial = await _turn_context(conv_id, [])
    candidates = [r.file_id for r in ctx.list_files() if r.file_id not in initial]

    assert files_for_reply(ctx, candidates, "done") == []
    assert files_for_reply(ctx, candidates, f"see [[FILE:{orphan.id}]]") == [str(orphan.id)]
    # This turn's write of the same bytes is handed the row back: carried.
    ctx.add_produced(FileRef.from_attachment(orphan))
    assert files_for_reply(ctx, candidates, "done") == [str(orphan.id)]


async def test_a_history_ref_wins_over_the_stored_one(store):
    conv_id = uuid.uuid4()
    row = _row(conv_id, "stored-name.pdf", minutes=0)
    store.rows = [row]
    inline = FileRef.from_bytes(b"inline", "fresh-name.pdf", "application/pdf", file_id=str(row.id))

    ctx, _ = await _turn_context(conv_id, [inline])

    assert ctx.get(str(row.id)).filename == "fresh-name.pdf"
    assert len(ctx.list_files()) == 1


async def test_a_current_turn_file_is_not_replaced(store):
    conv_id = uuid.uuid4()
    row = _row(conv_id, "upload.pdf", minutes=0, source="chat")
    store.rows = [row]
    current = FileRef.from_bytes(b"x", "upload.pdf", "application/pdf", file_id=str(row.id))

    ctx, _ = await _turn_context(conv_id, [], [current])

    assert ctx.get(str(row.id)) is current


async def test_available_files_block_lists_the_compacted_file(store):
    conv_id = uuid.uuid4()
    pdf = _row(conv_id, "Memorandum.pdf", minutes=0)
    store.rows = [pdf] + [_row(conv_id, "image.png", minutes=10 + i, source="chat") for i in range(4)]
    ctx, _ = await _turn_context(conv_id, [])

    section = ctx.to_prompt_section(inlined_file_ids=set())

    assert f'file_id="{pdf.id}"' in section
    assert "Memorandum.pdf" in section


def test_available_files_block_counts_what_it_does_not_list():
    refs = [
        FileRef(
            file_id=str(uuid.uuid4()),
            filename=f"f{i}.pdf",
            content_type="application/pdf",
            size=1,
            s3_object_name=f"k{i}",
            source="agent_download",
        )
        for i in range(PROMPT_BY_REFERENCE_FILES_LIMIT + 3)
    ]
    ctx = FileContext.from_file_refs(refs)

    section = ctx.to_prompt_section(inlined_file_ids=set())

    assert refs[-1].file_id in section
    assert refs[0].file_id not in section
    assert "and 3 earlier file(s) of this conversation" in section
    assert "resolvable by its file_id" in section


async def test_no_conversation_or_a_failed_read_keeps_the_turn_going(monkeypatch):
    assert await conversation_file_refs(None) == []
    assert await conversation_file_refs("not-a-uuid") == []

    def _broken():
        raise RuntimeError("db down")

    monkeypatch.setattr("app.core.database.async_session", _broken)
    assert await conversation_file_refs(uuid.uuid4()) == []


async def test_the_newest_files_are_kept_past_the_bound(store):
    conv_id = uuid.uuid4()
    store.rows = [_row(conv_id, f"f{i}.pdf", minutes=i) for i in range(5)]

    refs = await conversation_file_refs(conv_id, limit=3)

    assert [r.ref.filename for r in refs] == ["f2.pdf", "f3.pdf", "f4.pdf"]


def test_register_skips_a_missing_context():
    assert register_conversation_files(None, []) == 0
    assert cf.MAX_CONVERSATION_FILES >= 100
