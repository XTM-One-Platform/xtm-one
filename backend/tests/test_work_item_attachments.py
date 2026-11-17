"""A work item read in full, and the files its creator attaches.

``get_work_item`` returns the whole item - description, payload, result - and
puts the files its creator attached in the reader's context; ``list_work_items``
shows the start of each description; ``create_work_item`` attaches only stored
files the creating turn holds. The run finalizer leaves a lent file where it
was, and the run an inbox dispatches an item to gets that item's files.
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

import app.services.builtin_tools._shared as shared_tools
import app.services.work_item as wi_service
import app.services.work_item_files as wi_files
from app.llm.base import FileRef
from app.services import work_item_context
from app.services.builtin_tools import build_work_item_tools
from app.services.builtin_tools import work_items as wi_tools
from app.services.builtin_tools.work_items import _attachment_ids_argument
from app.services.work_item import WorkItemOrigin
from app.tools.base import FileContext, ToolCall, ToolDefinition, offering_tools

LONG_DESCRIPTION = "Brief: " + "x" * 493 + "PAST-THE-PREVIEW" + "y" * 1484


@pytest.fixture(autouse=True)
def _pickup_state_unknown(monkeypatch):
    async def _unknown(item):
        return None

    monkeypatch.setattr(work_item_context, "new_item_pickup_state", _unknown)


@pytest.fixture(autouse=True)
def _user_owns_nothing(monkeypatch):
    """No database: the acting user owns no stored row unless a test says so."""

    async def _none(ids, user_id):
        return set()

    monkeypatch.setattr(wi_files, "_own_row_ids", _none)


def _row(name: str = "brief.md", content_type: str = "text/markdown") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        filename=name,
        content_type=content_type,
        size=1200,
        s3_object_name=f"chat/conv/{name}",
        source="upload",
        description=None,
    )


def _store(monkeypatch, *rows) -> None:
    """The ``file_attachments`` rows the lookups find."""
    by_id = {row.id: row for row in rows}

    async def _stored(ids):
        return {i: by_id[i] for i in ids if i in by_id}

    monkeypatch.setattr(wi_files, "_stored_rows", _stored)


def _item(**overrides) -> dict:
    now = datetime.now(UTC).isoformat()
    item = {
        "id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "title": "Review the vendor contract",
        "description": LONG_DESCRIPTION,
        "payload": {"vendor": "Acme", "clauses": [4, 7]},
        "attachment_file_ids": [],
        "status": "claimed",
        "priority": "high",
        "correlation_key": "contract-42",
        "owner_agent_id": str(uuid.uuid4()),
        "created_by_agent_id": str(uuid.uuid4()),
        "claimed_by_agent_id": None,
        "claimed_at": None,
        "due_at": None,
        "result": None,
        "is_stale": False,
        "created_at": now,
        "updated_at": now,
        "last_activity_at": now,
    }
    item.update(overrides)
    return item


def _tools(**kwargs):
    return build_work_item_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()), **kwargs)


async def _call(executor, name: str, **arguments):
    return await executor.execute_tool(ToolCall(id=str(uuid.uuid4()), name=name, arguments=arguments))


_READER = ToolDefinition(
    name="read_file_content",
    description="Read a file.",
    input_schema={"type": "object", "properties": {}},
    source="builtin:generate_file",
)


# ── get_work_item ────────────────────────────────────────────────────────────


def test_get_work_item_is_one_of_the_work_item_tools():
    defs, executor = _tools()
    assert "get_work_item" in {d.name for d in defs}
    assert "get_work_item" in set(executor.tool_names)


@pytest.mark.asyncio
async def test_get_work_item_returns_the_whole_item_as_the_agent_reads_it(monkeypatch):
    item = _item(result="Two clauses flagged.")
    seen = {}

    async def _get(work_item_id, *, user_id, reader=None):
        seen["reader"] = reader
        return item if work_item_id == item["id"] else None

    monkeypatch.setattr(wi_service, "get_work_item", _get)
    _defs, executor = _tools()
    result = await _call(executor, "get_work_item", work_item_id=item["id"])

    assert not result.is_error
    # The whole description, not the listing's preview.
    assert LONG_DESCRIPTION in result.content
    assert '"vendor": "Acme"' in result.content
    for field in ("owner_agent_id", "created_by_agent_id", "correlation_key", "created_at", "result"):
        assert str(item[field]) in result.content
    assert result.structured == item
    # Read as the agent, so its creator's narrowing applies.
    assert seen["reader"] is not None and seen["reader"].agent_id is not None


@pytest.mark.asyncio
async def test_get_work_item_answers_not_found(monkeypatch):
    async def _missing(work_item_id, *, user_id, reader=None):
        return None

    monkeypatch.setattr(wi_service, "get_work_item", _missing)
    _defs, executor = _tools()
    result = await _call(executor, "get_work_item", work_item_id=str(uuid.uuid4()))
    assert "not found or not accessible" in result.content


# ── get_work_item on a long description ─────────────────────────────

_HEAD = wi_tools.DESCRIPTION_HEAD_CHARS
_TAIL = wi_tools.DESCRIPTION_TAIL_CHARS


def _long_item(middle: int = 30_000) -> tuple[dict, str]:
    """An item whose description is newest notes, a long middle, then the brief."""
    description = (
        "LATEST-NOTE " + "n" * (_HEAD - 12) + "MIDDLE-START" + "m" * middle + "b" * (_TAIL - 11) + " THE-BRIEF."
    )
    return _item(description=description, result="half done"), description


async def _read(monkeypatch, item: dict, **window):
    async def _get(work_item_id, *, user_id, reader=None):
        return item

    monkeypatch.setattr(wi_service, "get_work_item", _get)
    _defs, executor = _tools()
    return await _call(executor, "get_work_item", work_item_id=item["id"], **window)


@pytest.mark.asyncio
async def test_a_long_description_comes_as_its_newest_part_and_its_brief(monkeypatch):
    item, description = _long_item()
    result = await _read(monkeypatch, item)

    assert not result.is_error
    assert description[:_HEAD] in result.content
    assert description[-_TAIL:] in result.content
    assert "MIDDLE-START" not in result.content
    assert f"offset={_HEAD}" in result.content
    assert f"{len(description):,} characters" in result.content
    # Everything but the description comes whole, the structured item too.
    assert '"vendor": "Acme"' in result.content and "half done" in result.content
    assert result.structured == item
    assert len(result.content) < _HEAD + _TAIL + 2_000
    # The description comes last, its first line naming how to read on: the
    # loop's result cap cuts from the end and can only shorten it.
    header = result.content.index("Description (")
    assert result.content.index("half done") < header
    assert result.content.index('"vendor": "Acme"') < header
    assert f"offset={_HEAD}" in result.content[header : header + 300]


@pytest.mark.asyncio
async def test_a_description_under_the_bound_comes_whole_without_a_marker(monkeypatch):
    description = "x" * (_HEAD + _TAIL)
    result = await _read(monkeypatch, _item(description=description))
    assert description in result.content
    assert "left out" not in result.content and "offset=" not in result.content


@pytest.mark.asyncio
async def test_offset_and_max_chars_read_a_window_and_name_the_next_offset(monkeypatch):
    item, description = _long_item()
    result = await _read(monkeypatch, item, offset=_HEAD, max_chars=100)

    assert description[_HEAD : _HEAD + 100] in result.content
    assert f"characters {_HEAD:,} to {_HEAD + 100:,} of {len(description):,}" in result.content
    assert f"continue with offset={_HEAD + 100}" in result.content
    assert "LATEST-NOTE" not in result.content
    header = result.content.index("Description, characters")
    assert f"continue with offset={_HEAD + 100}" in result.content[header : header + 200]


@pytest.mark.asyncio
async def test_the_last_window_names_no_next_offset(monkeypatch):
    item, description = _long_item()
    start = len(description) - 50
    result = await _read(monkeypatch, item, offset=start)
    assert description[start:] in result.content
    assert "continue with offset" not in result.content
    assert "; the end:" in result.content


@pytest.mark.asyncio
async def test_an_offset_past_the_end_says_so(monkeypatch):
    item, description = _long_item()
    result = await _read(monkeypatch, item, offset=len(description) + 5)
    assert "past its end" in result.content


@pytest.mark.asyncio
async def test_a_large_payload_shrinks_the_window_and_the_status_block_names_the_next_offset(monkeypatch):
    """The loop cuts a result from its end (57,600 characters on the 128K
    fallback): the answer stays under its budget and the line naming how to
    read on sits in the status block, above anything a cut can reach."""
    item, description = _long_item(middle=100_000)
    item["payload"] = {"blob": "p" * 40_000}
    item["result"] = "r" * 30_000
    item["claim_notes"] = [{"text": "c" * 20_000}]

    # A window reads the description alone: claim notes, payload and result
    # stay out, so the window asked is delivered whole.
    window = await _read(monkeypatch, item, offset=0, max_chars=40_000)
    assert len(window.content) <= wi_tools.RESULT_BUDGET_CHARS
    assert "Payload:" not in window.content and "claim note" not in window.content
    assert "r" * 1_000 not in window.content
    assert "without offset/max_chars shows them" in window.content
    status_line = next(line for line in window.content.splitlines() if line.startswith("  - description:"))
    assert int(status_line.rsplit("offset=", 1)[1]) == 40_000
    assert description[:40_000] in window.content

    # The default view keeps every field, each cut to its share of the
    # budget with a marker, so the whole answer fits and the description
    # still gets its room; its status line sits above the claim notes.
    default = await _read(monkeypatch, item)
    assert len(default.content) <= wi_tools.RESULT_BUDGET_CHARS
    status_line = next(line for line in default.content.splitlines() if line.startswith("  - description:"))
    assert default.content.index(status_line) < default.content.index("claim note")
    for what in ("claim notes", "payload", "result"):
        assert f"characters of the {what} left out" in default.content
    assert '"blob"' in default.content and "r" * 1_000 in default.content
    assert "LATEST-NOTE" in default.content
    head = int(status_line.rsplit("offset=", 1)[1].split()[0])
    assert description[:head] in default.content


@pytest.mark.asyncio
@pytest.mark.parametrize("cap", [None, 3_686])
async def test_oversized_fields_alone_never_pass_the_budget(monkeypatch, cap):
    """Claim notes, payload, result and files of any size are each cut to a
    share of the budget, so the serialized answer always fits."""
    from contextlib import nullcontext

    from app.tools.base import limiting_tool_results

    item, _description = _long_item()
    item["payload"] = {"blob": "p" * 200_000}
    item["result"] = "r" * 200_000
    item["claim_notes"] = [{"text": "c" * 5_000} for _ in range(40)]
    with limiting_tool_results(cap) if cap else nullcontext():
        result = await _read(monkeypatch, item)
        budget = wi_tools._answer_budget()
    assert len(result.content) <= budget
    assert result.content.splitlines()[0].startswith("Work item **")
    status_line = next(line for line in result.content.splitlines() if line.startswith("  - description:"))
    assert "offset=" in status_line


@pytest.mark.asyncio
async def test_a_long_title_never_hides_the_description_line(monkeypatch):
    """A title near the 2,000-character write limit under a low cap (2,300):
    the title is bounded, so the status and description lines survive."""
    from app.tools.base import limiting_tool_results

    item, _description = _long_item()
    item["title"] = "T" * 2_000
    with limiting_tool_results(2_300):
        result = await _read(monkeypatch, item)
        window = await _read(monkeypatch, item, offset=0, max_chars=40_000)
    for answer in (result, window):
        assert len(answer.content) <= 2_100
        assert "characters of the title left out" in answer.content.splitlines()[0]
        assert "  - status:" in answer.content
        status_line = next(line for line in answer.content.splitlines() if line.startswith("  - description:"))
        assert "offset=" in status_line


@pytest.mark.asyncio
async def test_the_answer_fits_the_loops_own_tool_result_cap(monkeypatch):
    """An 8K-token model caps a tool result at 3,686 characters: the answer
    is sized to it, and the offset named is where the delivered part ends."""
    from app.tools.base import limiting_tool_results

    item, description = _long_item()
    with limiting_tool_results(3_686):
        default = await _read(monkeypatch, item)
        window = await _read(monkeypatch, item, offset=_HEAD, max_chars=40_000)

    for result in (default, window):
        assert len(result.content) <= 3_686
    status_line = next(line for line in window.content.splitlines() if line.startswith("  - description:"))
    end = int(status_line.rsplit("offset=", 1)[1])
    assert _HEAD < end < _HEAD + 3_686
    assert description[_HEAD:end] in window.content
    head_line = next(line for line in default.content.splitlines() if line.startswith("  - description:"))
    assert "left out: get_work_item with offset=" in head_line

    # Outside a loop no cap is bound: the default budget applies.
    assert wi_tools._answer_budget() == wi_tools.RESULT_BUDGET_CHARS


@pytest.mark.asyncio
async def test_the_window_is_capped(monkeypatch):
    item, description = _long_item(middle=200_000)
    result = await _read(monkeypatch, item, offset=0, max_chars=10_000_000)
    cap = wi_tools.DESCRIPTION_WINDOW_MAX_CHARS
    assert f"characters 0 to {cap:,} of {len(description):,}" in result.content
    assert f"continue with offset={cap}" in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("window", "message"),
    [
        ({"max_chars": 0}, "max_chars must be at least 1"),
        ({"offset": -1}, "offset must be at least 0"),
    ],
)
async def test_a_bad_window_is_refused_before_any_read(monkeypatch, window, message):
    async def _must_not_read(*_args, **_kwargs):
        raise AssertionError("read with a bad window")

    monkeypatch.setattr(wi_service, "get_work_item", _must_not_read)
    _defs, executor = _tools()
    result = await _call(executor, "get_work_item", work_item_id=str(uuid.uuid4()), **window)
    assert message in result.content


@pytest.mark.asyncio
async def test_a_long_result_reads_whole_in_two_result_windows(monkeypatch):
    """A 7,700-character result is cut in the full view; field="result"
    with offset/max_chars pages it verbatim (dev:work-item-result-paging)."""
    from app.tools.base import limiting_tool_results

    result_text = "RESULT-START " + "".join(f"line {i:04d}\n" for i in range(770)) + " Seen render. THE-END"
    item = _item(description="brief", result=result_text)
    with limiting_tool_results(8_000):
        full = await _read(monkeypatch, item)
        assert "characters of the result left out" in full.content
        assert 'field="result"' in full.content

        first = await _read(monkeypatch, item, field="result", max_chars=4_000)
        status = next(line for line in first.content.splitlines() if line.startswith("  - result:"))
        nxt = int(status.rsplit("offset=", 1)[1])
        second = await _read(monkeypatch, item, field="result", offset=nxt, max_chars=4_000)
    assert len(first.content) <= 8_000 and len(second.content) <= 8_000
    assert result_text[:nxt] in first.content
    assert result_text[nxt:] in second.content
    assert "; the end" in second.content
    assert "Description:" not in first.content and "Payload:" not in first.content


@pytest.mark.asyncio
async def test_a_result_window_on_an_item_without_result_says_none(monkeypatch):
    result = await _read(monkeypatch, _item(description="brief", result=None), field="result")
    assert "Result: (none)" in result.content


@pytest.mark.asyncio
async def test_an_unknown_field_is_refused_before_any_read(monkeypatch):
    async def _must_not_read(*_args, **_kwargs):
        raise AssertionError("read with a bad field")

    monkeypatch.setattr(wi_service, "get_work_item", _must_not_read)
    _defs, executor = _tools()
    result = await _call(executor, "get_work_item", work_item_id=str(uuid.uuid4()), field="payload")
    assert 'field must be "description" or "result"' in result.content


def test_get_work_item_declares_the_window_arguments():
    defs, _executor = _tools()
    schema = next(d for d in defs if d.name == "get_work_item").input_schema
    assert schema["properties"]["offset"]["type"] == "integer"
    assert schema["properties"]["max_chars"]["type"] == "integer"
    assert schema["properties"]["field"]["enum"] == ["description", "result"]
    assert schema["required"] == ["work_item_id"]


@pytest.mark.asyncio
async def test_get_work_item_registers_the_attached_files_for_the_reader(monkeypatch):
    attached = _row()
    gone = uuid.uuid4()
    _store(monkeypatch, attached)
    item = _item(attachment_file_ids=[str(attached.id), str(gone)])

    async def _get(work_item_id, *, user_id, reader=None):
        return item

    monkeypatch.setattr(wi_service, "get_work_item", _get)
    _defs, executor = _tools()
    context = FileContext()
    executor.file_context = context
    with offering_tools([_READER]):
        result = await _call(executor, "get_work_item", work_item_id=item["id"])

    ref = context.get(str(attached.id))
    assert ref is not None and ref.filename == "brief.md"
    # Fetched to read: the reader works from it, it does not hand it over.
    assert str(attached.id) in context.fetched_ids
    assert str(attached.id) in result.content
    assert "read_file_content(file_id)" in result.content
    assert "1 attached file(s) no longer exist" in result.content


@pytest.mark.asyncio
async def test_get_work_item_never_promises_a_file_reader_the_loop_does_not_offer(monkeypatch):
    attached = _row()
    _store(monkeypatch, attached)
    item = _item(attachment_file_ids=[str(attached.id)])

    async def _get(work_item_id, *, user_id, reader=None):
        return item

    monkeypatch.setattr(wi_service, "get_work_item", _get)
    _defs, executor = _tools()
    executor.file_context = FileContext()
    with offering_tools([]):
        result = await _call(executor, "get_work_item", work_item_id=item["id"])

    assert str(attached.id) in result.content
    assert "read_file_content" not in result.content
    assert "cannot open them here" in result.content


# ── list_work_items ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_work_items_shows_the_start_of_each_description(monkeypatch):
    long_item = _item(attachment_file_ids=[str(uuid.uuid4())])
    short_item = _item(title="Ping the vendor", description="Ask for the signed copy.", payload={})

    async def _list(**kwargs):
        return [long_item, short_item]

    monkeypatch.setattr(wi_service, "list_work_items", _list)
    _defs, executor = _tools()
    result = await _call(executor, "list_work_items", open_only=True)

    assert LONG_DESCRIPTION[:500] in result.content
    assert "PAST-THE-PREVIEW" not in result.content
    assert "description truncated: 500 of 2,000 characters" in result.content
    assert "Ask for the signed copy." in result.content
    assert result.content.count("description truncated") == 1
    assert "has a payload" in result.content
    assert "1 attached file(s)" in result.content
    assert "get_work_item(work_item_id)" in result.content
    # The gate checks read the structured items, whole.
    assert result.structured[0]["description"] == LONG_DESCRIPTION


@pytest.mark.asyncio
async def test_list_work_items_with_nothing_left_out_points_nowhere(monkeypatch):
    async def _list(**kwargs):
        return [_item(description="Short.", payload={})]

    monkeypatch.setattr(wi_service, "list_work_items", _list)
    _defs, executor = _tools()
    result = await _call(executor, "list_work_items")
    assert "Short." in result.content
    assert "get_work_item(" not in result.content


# ── create_work_item attachments ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_work_item_attaches_a_stored_file_of_the_turn(monkeypatch):
    upload = _row()
    _store(monkeypatch, upload)
    captured = {}

    async def _create(**kwargs):
        captured.update(kwargs)
        return {"id": "wi-1", "title": kwargs["title"], "status": "open", "priority": "medium"}

    monkeypatch.setattr(wi_service, "create_work_item", _create)
    _defs, executor = _tools(origin=WorkItemOrigin(conversation_id=uuid.uuid4()))
    context = FileContext()
    context.add(FileRef.from_attachment(upload))
    executor.file_context = context
    result = await _call(
        executor,
        "create_work_item",
        title="Review the vendor contract",
        attachment_file_ids=[f"[[FILE:{upload.id}]]", str(upload.id)],
    )

    assert not result.is_error
    assert captured["attachment_file_ids"] == [str(upload.id)]
    assert "1 attached file(s)" in result.content
    assert "brief.md" in result.content


@pytest.mark.asyncio
async def test_create_work_item_refuses_a_file_the_turn_does_not_hold(monkeypatch):
    _store(monkeypatch)
    scoped = {}

    async def _not_scoped(file_id, *, conversation_id, assignment_run_id):
        scoped.update(conversation_id=conversation_id, assignment_run_id=assignment_run_id)
        return None

    async def _must_not_create(**kwargs):
        raise AssertionError("nothing is created when an attachment is refused")

    monkeypatch.setattr(shared_tools, "load_scoped_file_ref", _not_scoped)
    monkeypatch.setattr(wi_service, "create_work_item", _must_not_create)
    run_id = uuid.uuid4()
    _defs, executor = _tools(origin=WorkItemOrigin(run_id=run_id))
    executor.file_context = FileContext()
    foreign = str(uuid.uuid4())
    result = await _call(executor, "create_work_item", title="x", attachment_file_ids=[foreign])

    assert f"'{foreign}' cannot be attached" in result.content
    assert "Nothing was created" in result.content
    # The run's own rows were looked at, never anyone else's.
    assert scoped == {"conversation_id": None, "assignment_run_id": str(run_id)}


@pytest.mark.asyncio
async def test_create_work_item_refuses_a_file_held_only_in_memory(monkeypatch):
    _store(monkeypatch)

    async def _must_not_create(**kwargs):
        raise AssertionError("an unstored file cannot be attached")

    monkeypatch.setattr(wi_service, "create_work_item", _must_not_create)
    in_memory = FileRef.from_bytes(b"draft", "draft.txt", "text/plain")
    _defs, executor = _tools(origin=WorkItemOrigin(conversation_id=uuid.uuid4()))
    context = FileContext()
    context.add(in_memory)
    executor.file_context = context
    result = await _call(executor, "create_work_item", title="x", attachment_file_ids=[in_memory.file_id])
    assert "cannot be attached" in result.content


@pytest.mark.asyncio
async def test_create_work_item_caps_its_attachments(monkeypatch):
    async def _must_not_create(**kwargs):
        raise AssertionError("over the cap nothing is created")

    monkeypatch.setattr(wi_service, "create_work_item", _must_not_create)
    _defs, executor = _tools()
    ids = [str(uuid.uuid4()) for _ in range(wi_files.MAX_WORK_ITEM_ATTACHMENTS_PER_CALL + 1)]
    result = await _call(executor, "create_work_item", title="x", attachment_file_ids=ids)
    assert f"at most {wi_files.MAX_WORK_ITEM_ATTACHMENTS_PER_CALL}" in result.content
    assert f"({len(ids)} given)" in result.content


def test_attachment_ids_are_read_however_the_model_sends_them():
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    assert _attachment_ids_argument([a, " ", b]) == [a, b]
    assert _attachment_ids_argument(f"{a}, {b}") == [a, b]
    assert _attachment_ids_argument(json.dumps([a, b])) == [a, b]
    assert _attachment_ids_argument(f"[[FILE:{a}]]") == [f"[[FILE:{a}]]"]
    assert _attachment_ids_argument(None) == []


@pytest.mark.asyncio
async def test_resolve_attachable_files_reads_the_turns_own_rows(monkeypatch):
    produced = _row("report.pdf", "application/pdf")
    _store(monkeypatch, produced)

    async def _scoped(file_id, *, conversation_id, assignment_run_id):
        return FileRef.from_attachment(produced) if file_id == str(produced.id) else None

    monkeypatch.setattr(shared_tools, "load_scoped_file_ref", _scoped)
    found = await wi_files.resolve_attachable_files(
        [str(produced.id), str(produced.id)],
        file_context=FileContext(),
        conversation_id=None,
        assignment_run_id=uuid.uuid4(),
    )
    assert found.file_ids == [str(produced.id)]
    assert found.refused == []


@pytest.mark.asyncio
async def test_resolve_attachable_files_admits_a_file_of_another_conversation_of_the_user(monkeypatch):
    elsewhere = _row("reference.png", "image/png")
    _store(monkeypatch, elsewhere)
    user_id = str(uuid.uuid4())
    asked = {}

    async def _not_scoped(file_id, *, conversation_id, assignment_run_id):
        return None

    async def _owned(ids, owner):
        asked.update(ids=list(ids), owner=owner)
        return {elsewhere.id} if owner == user_id else set()

    monkeypatch.setattr(shared_tools, "load_scoped_file_ref", _not_scoped)
    monkeypatch.setattr(wi_files, "_own_row_ids", _owned)
    found = await wi_files.resolve_attachable_files(
        [f"[[FILE:{elsewhere.id}]]", str(elsewhere.id)],
        file_context=FileContext(),
        conversation_id=None,
        assignment_run_id=uuid.uuid4(),
        user_id=user_id,
    )
    assert found.file_ids == [str(elsewhere.id)]
    assert found.refused == []
    assert asked == {"ids": [elsewhere.id, elsewhere.id], "owner": user_id}


@pytest.mark.asyncio
async def test_resolve_attachable_files_refuses_a_file_of_another_user(monkeypatch):
    theirs = _row("their-notes.md")
    _store(monkeypatch, theirs)

    async def _not_scoped(file_id, *, conversation_id, assignment_run_id):
        return None

    monkeypatch.setattr(shared_tools, "load_scoped_file_ref", _not_scoped)
    found = await wi_files.resolve_attachable_files(
        [str(theirs.id)],
        file_context=FileContext(),
        conversation_id=None,
        assignment_run_id=uuid.uuid4(),
        user_id=str(uuid.uuid4()),
    )
    assert found.file_ids == []
    assert found.refused == [str(theirs.id)]


@pytest.mark.asyncio
async def test_resolve_attachable_files_without_a_user_reads_no_ownership(monkeypatch):
    stray = _row("stray.md")
    _store(monkeypatch, stray)

    async def _not_scoped(file_id, *, conversation_id, assignment_run_id):
        return None

    async def _must_not_look(ids, owner):
        raise AssertionError("no acting user, no ownership lookup")

    monkeypatch.setattr(shared_tools, "load_scoped_file_ref", _not_scoped)
    monkeypatch.setattr(wi_files, "_own_row_ids", _must_not_look)
    found = await wi_files.resolve_attachable_files(
        [str(stray.id)], file_context=FileContext(), conversation_id=None, assignment_run_id=None
    )
    assert found.refused == [str(stray.id)]


@pytest.mark.asyncio
async def test_resolve_attachable_files_admits_an_id_prefix_of_a_file_the_user_owns(monkeypatch):
    elsewhere = _row("design.png", "image/png")
    _store(monkeypatch, elsewhere)
    user_id = str(uuid.uuid4())
    prefix = str(elsewhere.id)[:8]

    async def _not_scoped(file_id, *, conversation_id, assignment_run_id):
        return None

    async def _by_prefix(wanted, owner):
        return [(elsewhere.id, "design.png")] if owner == user_id and str(elsewhere.id).startswith(wanted) else []

    async def _owned(ids, owner):
        return {elsewhere.id} if owner == user_id else set()

    monkeypatch.setattr(shared_tools, "load_scoped_file_ref", _not_scoped)
    monkeypatch.setattr(wi_files, "_own_rows_by_prefix", _by_prefix)
    monkeypatch.setattr(wi_files, "_own_row_ids", _owned)
    found = await wi_files.resolve_attachable_files(
        [prefix], file_context=FileContext(), conversation_id=None, assignment_run_id=uuid.uuid4(), user_id=user_id
    )
    assert found.file_ids == [str(elsewhere.id)]
    assert found.refused == []


@pytest.mark.asyncio
async def test_resolve_attachable_files_refuses_an_ambiguous_prefix_naming_the_candidates(monkeypatch):
    first, second = _row("a.md"), _row("b.md")
    _store(monkeypatch, first, second)

    async def _not_scoped(file_id, *, conversation_id, assignment_run_id):
        return None

    async def _by_prefix(wanted, owner):
        return [(first.id, "a.md"), (second.id, "b.md")]

    monkeypatch.setattr(shared_tools, "load_scoped_file_ref", _not_scoped)
    monkeypatch.setattr(wi_files, "_own_rows_by_prefix", _by_prefix)
    found = await wi_files.resolve_attachable_files(
        ["abcdef12"],
        file_context=FileContext(),
        conversation_id=None,
        assignment_run_id=None,
        user_id=str(uuid.uuid4()),
    )
    assert found.refused == ["abcdef12"]
    reason = found.refusal_reason()
    assert "is the start of the id of 2 files of yours" in reason
    assert f"`{first.id}` (a.md)" in reason and f"`{second.id}` (b.md)" in reason


@pytest.mark.asyncio
async def test_a_prefix_never_reaches_another_users_file(monkeypatch):
    theirs = _row("their-notes.md")
    _store(monkeypatch, theirs)
    asked = []

    async def _not_scoped(file_id, *, conversation_id, assignment_run_id):
        return None

    async def _by_prefix(wanted, owner):
        # The lookup is scoped to the owner's rows: another user's file is never a candidate.
        asked.append(owner)
        return []

    monkeypatch.setattr(shared_tools, "load_scoped_file_ref", _not_scoped)
    monkeypatch.setattr(wi_files, "_own_rows_by_prefix", _by_prefix)
    me = str(uuid.uuid4())
    found = await wi_files.resolve_attachable_files(
        [str(theirs.id)[:8]], file_context=FileContext(), conversation_id=None, assignment_run_id=None, user_id=me
    )
    assert found.file_ids == [] and found.refused == [str(theirs.id)[:8]]
    assert found.refusal_reason() == ""
    assert asked == [me]


def test_the_owned_file_lookup_is_scoped_to_the_owner():
    """The ownership clause the id and prefix lookups share is the Files page's "Mine" predicate."""
    sql = str(wi_files._mine_clause(uuid.uuid4()).compile(dialect=postgresql.dialect()))
    assert "file_attachments.user_id" in sql
    assert "conversations.user_id" in sql
    assert "assignment_runs.user_id" in sql


def test_a_file_of_the_turn_resolves_by_an_id_prefix():
    ref = FileRef.from_bytes(data=b"x", filename="shot.png", content_type="image/png")
    other = FileRef.from_bytes(data=b"y", filename="other.png", content_type="image/png")
    context = FileContext()
    context.add(ref)
    context.add(other)
    assert shared_tools.resolve_file_ref(context, ref.file_id[:8]) is ref
    assert shared_tools.resolve_file_ref(context, f"[[FILE:{ref.file_id[:10]}]]") is ref
    assert shared_tools.resolve_file_ref(context, ref.file_id[:7]) is None


def test_an_ambiguous_prefix_in_the_turn_names_the_candidates():
    context = FileContext()
    for n, name in ((1, "a.png"), (2, "b.png")):
        context.add(
            FileRef.from_bytes(
                data=b"x", filename=name, content_type="image/png", file_id=f"abcdef12-0000-4000-8000-00000000000{n}"
            )
        )
    assert shared_tools.resolve_file_ref(context, "abcdef12") is None
    message = shared_tools.unresolved_file_message(context, "abcdef12")
    assert "is the start of the id of 2 files of this conversation" in message
    assert "a.png" in message and "b.png" in message


@pytest.mark.asyncio
async def test_create_work_item_attaches_a_file_its_user_owns_elsewhere(monkeypatch):
    elsewhere = _row("reference.png", "image/png")
    _store(monkeypatch, elsewhere)
    captured = {}

    async def _not_scoped(file_id, *, conversation_id, assignment_run_id):
        return None

    async def _create(**kwargs):
        captured.update(kwargs)
        return {"id": "wi-1", "title": kwargs["title"], "status": "open", "priority": "medium"}

    user_id = str(uuid.uuid4())

    async def _owned(ids, owner):
        return {elsewhere.id} if owner == user_id else set()

    monkeypatch.setattr(shared_tools, "load_scoped_file_ref", _not_scoped)
    monkeypatch.setattr(wi_files, "_own_row_ids", _owned)
    monkeypatch.setattr(wi_service, "create_work_item", _create)
    _defs, executor = build_work_item_tools(
        SimpleNamespace(), user_id, str(uuid.uuid4()), origin=WorkItemOrigin(run_id=uuid.uuid4())
    )
    executor.file_context = FileContext()
    result = await _call(executor, "create_work_item", title="UX review", attachment_file_ids=[str(elsewhere.id)])

    assert not result.is_error
    assert captured["attachment_file_ids"] == [str(elsewhere.id)]


# ── complete_work_item: the result's files ──────────────────────────────────


@pytest.mark.asyncio
async def test_complete_work_item_attaches_a_stored_file_of_the_run(monkeypatch):
    produced = _row("result.csv", "text/csv")
    _store(monkeypatch, produced)
    captured = {}

    async def _complete(work_item_id, **kwargs):
        captured.update(kwargs)
        return {"id": work_item_id, "title": "Export", "status": "done"}

    monkeypatch.setattr(wi_service, "complete_work_item", _complete)
    _defs, executor = _tools(origin=WorkItemOrigin(run_id=uuid.uuid4()))
    context = FileContext()
    context.add(FileRef.from_attachment(produced))
    executor.file_context = context
    result = await _call(
        executor,
        "complete_work_item",
        work_item_id=str(uuid.uuid4()),
        result="done, see the CSV",
        attachment_file_ids=[f"[[FILE:{produced.id}]]", str(produced.id)],
    )

    assert not result.is_error
    assert captured["attachment_file_ids"] == [str(produced.id)]
    assert "1 result file(s) attached" in result.content


@pytest.mark.asyncio
async def test_complete_work_item_refuses_a_file_the_run_does_not_hold(monkeypatch):
    _store(monkeypatch)
    scoped = {}

    async def _not_scoped(file_id, *, conversation_id, assignment_run_id):
        scoped.update(conversation_id=conversation_id, assignment_run_id=assignment_run_id)
        return None

    async def _must_not_complete(*args, **kwargs):
        raise AssertionError("nothing is completed when an attachment is refused")

    monkeypatch.setattr(shared_tools, "load_scoped_file_ref", _not_scoped)
    monkeypatch.setattr(wi_service, "complete_work_item", _must_not_complete)
    run_id = uuid.uuid4()
    _defs, executor = _tools(origin=WorkItemOrigin(run_id=run_id))
    executor.file_context = FileContext()
    foreign = str(uuid.uuid4())
    result = await _call(
        executor, "complete_work_item", work_item_id=str(uuid.uuid4()), attachment_file_ids=[foreign]
    )

    assert f"'{foreign}' cannot be attached" in result.content
    assert "Nothing was completed" in result.content
    # Only the completing run's own rows were looked at.
    assert scoped == {"conversation_id": None, "assignment_run_id": str(run_id)}


@pytest.mark.asyncio
async def test_complete_work_item_without_files_passes_none(monkeypatch):
    captured = {}

    async def _complete(work_item_id, **kwargs):
        captured.update(kwargs)
        return {"id": work_item_id, "title": "x", "status": "done"}

    monkeypatch.setattr(wi_service, "complete_work_item", _complete)
    _defs, executor = _tools()
    result = await _call(executor, "complete_work_item", work_item_id=str(uuid.uuid4()))
    assert not result.is_error
    assert "attachment_file_ids" not in captured
    assert "result file" not in result.content


def test_complete_work_item_declares_attachment_file_ids():
    defs, _executor = _tools()
    schema = next(d for d in defs if d.name == "complete_work_item").input_schema
    assert schema["properties"]["attachment_file_ids"]["type"] == "array"


# ── update_work_item: files attached to an existing item (#12092) ────────────


def test_update_work_item_declares_the_file_lists():
    defs, _executor = _tools()
    schema = next(d for d in defs if d.name == "update_work_item").input_schema
    assert schema["properties"]["attachment_file_ids"]["type"] == "array"
    assert schema["properties"]["remove_attachment_file_ids"]["type"] == "array"


@pytest.mark.asyncio
async def test_update_work_item_attaches_a_stored_file_of_the_run_deduplicated(monkeypatch):
    capture = _row("capture-dark.png", "image/png")
    _store(monkeypatch, capture)
    captured = {}

    async def _update(work_item_id, **kwargs):
        captured.update(kwargs)
        return _item(id=work_item_id, title="UX review", attachment_file_ids=[str(capture.id)])

    monkeypatch.setattr(wi_service, "update_work_item", _update)
    _defs, executor = _tools(origin=WorkItemOrigin(run_id=uuid.uuid4()))
    context = FileContext()
    context.add(FileRef.from_attachment(capture))
    executor.file_context = context
    result = await _call(
        executor,
        "update_work_item",
        work_item_id=str(uuid.uuid4()),
        attachment_file_ids=[f"[[FILE:{capture.id}]]", str(capture.id)],
    )

    assert not result.is_error
    assert captured["attachment_file_ids"] == [str(capture.id)]
    assert captured["remove_attachment_file_ids"] is None
    assert "1 file(s) attached" in result.content
    assert "the item carries 1" in result.content


@pytest.mark.asyncio
async def test_update_work_item_refuses_a_foreign_file_and_writes_nothing(monkeypatch):
    _store(monkeypatch)

    async def _not_scoped(file_id, *, conversation_id, assignment_run_id):
        return None

    async def _must_not_update(*args, **kwargs):
        raise AssertionError("nothing is updated when an attachment is refused")

    monkeypatch.setattr(shared_tools, "load_scoped_file_ref", _not_scoped)
    monkeypatch.setattr(wi_service, "update_work_item", _must_not_update)
    _defs, executor = _tools(origin=WorkItemOrigin(run_id=uuid.uuid4()))
    executor.file_context = FileContext()
    foreign = str(uuid.uuid4())
    result = await _call(
        executor, "update_work_item", work_item_id=str(uuid.uuid4()), note="captures", attachment_file_ids=[foreign]
    )

    assert f"'{foreign}' cannot be attached" in result.content
    assert "Nothing was updated" in result.content


@pytest.mark.asyncio
async def test_update_work_item_caps_one_call(monkeypatch):
    async def _must_not_update(*args, **kwargs):
        raise AssertionError("nothing is updated past the per-call cap")

    monkeypatch.setattr(wi_service, "update_work_item", _must_not_update)
    _defs, executor = _tools()
    too_many = [str(uuid.uuid4()) for _ in range(wi_files.MAX_WORK_ITEM_ATTACHMENTS_PER_CALL + 1)]
    result = await _call(executor, "update_work_item", work_item_id=str(uuid.uuid4()), attachment_file_ids=too_many)
    assert "Nothing was updated" in result.content


@pytest.mark.asyncio
async def test_update_work_item_without_files_passes_none(monkeypatch):
    captured = {}

    async def _update(work_item_id, **kwargs):
        captured.update(kwargs)
        return _item(id=work_item_id)

    monkeypatch.setattr(wi_service, "update_work_item", _update)
    _defs, executor = _tools()
    result = await _call(executor, "update_work_item", work_item_id=str(uuid.uuid4()), note="progress")
    assert not result.is_error
    assert captured["attachment_file_ids"] is None
    assert captured["remove_attachment_file_ids"] is None
    assert "Files:" not in result.content


def test_updated_files_append_after_the_carried_ones_deduplicated():
    held, new = str(uuid.uuid4()), str(uuid.uuid4())
    item = SimpleNamespace(attachment_file_ids=[held])
    assert wi_service._updated_attachment_ids(item, [new, held, new], []) == [held, new]
    assert wi_service._updated_attachment_ids(SimpleNamespace(attachment_file_ids=None), [new], []) == [new]


def test_updated_files_remove_by_id_or_unique_prefix():
    first, second, third = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    item = SimpleNamespace(attachment_file_ids=[first, second])
    assert wi_service._updated_attachment_ids(item, [], [first.upper()]) == [second]
    assert wi_service._updated_attachment_ids(item, [third], [second[:8]]) == [first, third]
    assert wi_service._updated_attachment_ids(SimpleNamespace(attachment_file_ids=[first]), [], [first]) is None


def test_updated_files_refuse_removing_a_file_the_item_does_not_carry():
    item = SimpleNamespace(attachment_file_ids=[str(uuid.uuid4())])
    with pytest.raises(ValueError) as refused:
        wi_service._updated_attachment_ids(item, [], [str(uuid.uuid4())])
    assert "is not a file attached to this item" in str(refused.value)
    assert "Nothing was updated" in str(refused.value)


def test_updated_files_past_the_life_cap_are_refused():
    cap = wi_files.MAX_WORK_ITEM_ATTACHMENTS
    full = SimpleNamespace(attachment_file_ids=[str(uuid.uuid4()) for _ in range(cap)])
    with pytest.raises(ValueError) as refused:
        wi_service._updated_attachment_ids(full, [str(uuid.uuid4())], [])
    assert f"at most {cap} attached files" in str(refused.value)
    assert "Nothing was updated" in str(refused.value)
    # Room made by a removal in the same call is used.
    assert len(wi_service._updated_attachment_ids(full, [str(uuid.uuid4())], [full.attachment_file_ids[0]])) == cap


@pytest.mark.asyncio
async def test_the_rest_detail_names_the_files_for_the_dialog(monkeypatch):
    """GET /work-items/{id} lists the stored files by name and counts the gone ones."""
    from app.api.v1 import work_items as wi_api

    capture = _row("capture-dark.png", "image/png")
    _store(monkeypatch, capture)
    block = await wi_api._attachments_block(_item(attachment_file_ids=[str(capture.id), str(uuid.uuid4())]))
    assert block == {
        "files": [
            {"file_id": str(capture.id), "filename": "capture-dark.png", "content_type": "image/png", "size": 1200}
        ],
        "gone": 1,
    }
    assert await wi_api._attachments_block(_item()) == {"files": [], "gone": 0}


@pytest.mark.asyncio
async def test_the_owner_reads_a_file_attached_by_update(monkeypatch):
    """A file attached after creation is opened by the owner's get_work_item by id."""
    capture = _row("capture-light.png", "image/png")
    _store(monkeypatch, capture)
    item = _item(attachment_file_ids=[str(capture.id)])

    async def _get(work_item_id, *, user_id, reader=None):
        return item

    monkeypatch.setattr(wi_service, "get_work_item", _get)
    _defs, owner = _tools()
    context = FileContext()
    owner.file_context = context
    with offering_tools([_READER]):
        result = await _call(owner, "get_work_item", work_item_id=item["id"])
    assert context.get(str(capture.id)) is not None
    assert str(capture.id) in result.content


def test_cited_file_ids_reads_the_markers_not_bare_ids():
    a, b, other = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    text = f"CSV [[FILE:{a}]], chart ![c](file:{b.upper()}), again [[FILE:{a}]]; item {other}"
    assert wi_files.cited_file_ids(text) == [a, b]
    assert wi_files.cited_file_ids(None) == []


@pytest.mark.asyncio
async def test_complete_work_item_attaches_a_file_the_result_cites(monkeypatch):
    produced = _row("result.csv", "text/csv")
    _store(monkeypatch, produced)
    captured = {}

    async def _complete(work_item_id, **kwargs):
        captured.update(kwargs)
        return {
            "id": work_item_id,
            "title": "Export",
            "status": "done",
            "attachment_file_ids": list(kwargs.get("cited_file_ids") or []),
        }

    monkeypatch.setattr(wi_service, "complete_work_item", _complete)
    _defs, executor = _tools(origin=WorkItemOrigin(run_id=uuid.uuid4()))
    context = FileContext()
    context.add(FileRef.from_attachment(produced))
    executor.file_context = context
    result = await _call(
        executor,
        "complete_work_item",
        work_item_id=str(uuid.uuid4()),
        result=f"done, the CSV is [[FILE:{produced.id}]]",
    )

    assert not result.is_error
    assert captured["cited_file_ids"] == [str(produced.id)]
    assert "attachment_file_ids" not in captured
    assert "1 file(s) the result cites attached too" in result.content


@pytest.mark.asyncio
async def test_complete_work_item_warns_on_a_cited_file_it_cannot_attach(monkeypatch):
    _store(monkeypatch)
    captured = {}

    async def _not_scoped(file_id, *, conversation_id, assignment_run_id):
        return None

    async def _complete(work_item_id, **kwargs):
        captured.update(kwargs)
        return {"id": work_item_id, "title": "Export", "status": "done"}

    monkeypatch.setattr(shared_tools, "load_scoped_file_ref", _not_scoped)
    monkeypatch.setattr(wi_service, "complete_work_item", _complete)
    _defs, executor = _tools(origin=WorkItemOrigin(run_id=uuid.uuid4()))
    executor.file_context = FileContext()
    foreign = str(uuid.uuid4())
    result = await _call(
        executor, "complete_work_item", work_item_id=str(uuid.uuid4()), result=f"see [[FILE:{foreign}]]"
    )

    # Completed all the same: a citation never costs the completion, nor grants a file.
    assert not result.is_error
    assert "cited_file_ids" not in captured
    assert f"Warning: {foreign} cited but not attached" in result.content


def test_cited_files_fill_the_room_left_under_the_cap():
    cap = wi_files.MAX_WORK_ITEM_ATTACHMENTS
    held = [str(uuid.uuid4()) for _ in range(cap - 1)]
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    item = SimpleNamespace(attachment_file_ids=list(held))
    assert wi_service._with_cited_attachment_ids(item, [held[0], first, second]) == [*held, first]
    assert wi_service._with_cited_attachment_ids(SimpleNamespace(attachment_file_ids=None), [first]) == [first]


def test_result_files_join_the_creators_files_deduplicated():
    creator, result_file = str(uuid.uuid4()), str(uuid.uuid4())
    item = SimpleNamespace(attachment_file_ids=[creator])
    assert wi_service._merged_attachment_ids(item, [result_file, creator]) == [creator, result_file]
    assert wi_service._merged_attachment_ids(SimpleNamespace(attachment_file_ids=None), [result_file]) == [
        result_file
    ]


def test_result_files_past_the_cap_are_refused_naming_the_cap_and_the_count():
    cap = wi_files.MAX_WORK_ITEM_ATTACHMENTS
    full = SimpleNamespace(attachment_file_ids=[str(uuid.uuid4()) for _ in range(cap)])
    with pytest.raises(ValueError) as refused:
        wi_service._merged_attachment_ids(full, [str(uuid.uuid4()), str(uuid.uuid4())])
    message = str(refused.value)
    assert f"at most {cap} attached files" in message
    assert f"it carries {cap} and this call adds 2 new ({cap + 2})" in message
    assert "Nothing was completed" in message


def test_the_life_cap_holds_several_rounds_of_evidence():
    life, per_call = wi_files.MAX_WORK_ITEM_ATTACHMENTS, wi_files.MAX_WORK_ITEM_ATTACHMENTS_PER_CALL
    inlined = wi_files.MAX_DISPATCH_INLINED_ATTACHMENTS
    assert life >= 5 * per_call
    assert inlined <= per_call


def test_a_ux_item_attaches_each_round_of_evidence():
    """A review item completed round after round attaches a full round each time."""
    per_call = wi_files.MAX_WORK_ITEM_ATTACHMENTS_PER_CALL
    item = SimpleNamespace(attachment_file_ids=[str(uuid.uuid4()) for _ in range(3)])
    rounds = []
    for _ in range(4):
        evidence = [str(uuid.uuid4()) for _ in range(per_call)]
        item.attachment_file_ids = wi_service._merged_attachment_ids(item, evidence)
        rounds.append(evidence)
    assert len(item.attachment_file_ids) == 3 + 4 * per_call
    # Every round stays carried, in order: the older evidence stays readable.
    for evidence in rounds:
        assert all(file_id in item.attachment_file_ids for file_id in evidence)
    assert item.attachment_file_ids[-per_call:] == rounds[-1]


@pytest.mark.asyncio
async def test_complete_work_item_caps_one_call_naming_the_count(monkeypatch):
    async def _must_not_complete(*args, **kwargs):
        raise AssertionError("over the per-call cap nothing is completed")

    monkeypatch.setattr(wi_service, "complete_work_item", _must_not_complete)
    _defs, executor = _tools()
    ids = [str(uuid.uuid4()) for _ in range(wi_files.MAX_WORK_ITEM_ATTACHMENTS_PER_CALL + 1)]
    result = await _call(
        executor, "complete_work_item", work_item_id=str(uuid.uuid4()), result="r", attachment_file_ids=ids
    )
    assert f"at most {wi_files.MAX_WORK_ITEM_ATTACHMENTS_PER_CALL} files ({len(ids)} given)" in result.content
    assert "Nothing was completed" in result.content


# ── claim / serialization ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_claim_points_at_the_full_read(monkeypatch):
    async def _claim_ok(work_item_id, *, agent_id, user_id, writer):
        return {"claimed": True, "work_item": {"id": work_item_id}}

    monkeypatch.setattr(wi_service, "claim_work_item", _claim_ok)
    _defs, executor = _tools()
    result = await _call(executor, "claim_work_item", work_item_id="wi-1")
    assert "Claimed work item" in result.content
    assert "get_work_item" in result.content


def test_a_serialized_item_names_its_attachments():
    now = datetime.now(UTC)
    row = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=None,
        is_company_managed=True,
        owner_agent_id=None,
        created_by_agent_id=None,
        title="t",
        description=None,
        payload={},
        status="open",
        priority="medium",
        correlation_key=None,
        due_at=None,
        claimed_by_agent_id=None,
        claimed_at=None,
        source="agent",
        result=None,
        last_activity_at=now,
        created_at=now,
        updated_at=now,
    )
    assert wi_service.serialize_work_item(row)["attachment_file_ids"] == []
    row.attachment_file_ids = ["f-1"]
    assert wi_service.serialize_work_item(row)["attachment_file_ids"] == ["f-1"]


# ── The run side ─────────────────────────────────────────────────────────────


class _LinkSession:
    def __init__(self):
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)

    @asynccontextmanager
    async def begin_nested(self):
        yield


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


@pytest.mark.asyncio
async def test_the_run_finalizer_leaves_a_lent_file_where_it_is():
    from app.services.assignment_engine import _link_run_output_files

    produced = FileRef.from_attachment(_row("summary.md"))
    lent = FileRef.from_attachment(_row("brief.md"))
    context = FileContext()
    context.add_produced(produced)
    context.add_fetched(lent)
    db = _LinkSession()
    await _link_run_output_files(db, SimpleNamespace(id=uuid.uuid4()), context, [])

    by_file = {stmt.compile().params["id_1"]: _sql(stmt) for stmt in db.statements}
    # What the run produced is linked to it, as before.
    assert "conversation_id IS NULL" not in by_file[uuid.UUID(produced.file_id)]
    # What it fetched is linked only when no conversation or other run holds it.
    lent_sql = by_file[uuid.UUID(lent.file_id)]
    assert "file_attachments.conversation_id IS NULL" in lent_sql
    assert "file_attachments.assignment_run_id IS NULL" in lent_sql


def _run(*, context_type: str, item_id: str, user_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.UUID(user_id),
        trigger_context={"type": context_type, "work_item_id": item_id},
        trigger_payload={
            "trigger_type": "work_item",
            "work_item_id": item_id,
            "work_item": {"id": item_id, "user_id": user_id},
        },
    )


@pytest.mark.asyncio
async def test_the_dispatched_run_gets_its_work_items_files(monkeypatch):
    from app.services.assignment_engine import load_dispatched_work_item_files

    attached = _row()
    _store(monkeypatch, attached)
    item = _item(attachment_file_ids=[str(attached.id)])
    reads = []

    async def _get(work_item_id, *, user_id, reader=None):
        reads.append((work_item_id, user_id))
        return item

    monkeypatch.setattr(wi_service, "get_work_item", _get)
    run = _run(context_type="work_item", item_id=item["id"], user_id=item["user_id"])
    refs = await load_dispatched_work_item_files(run)
    assert [ref.file_id for ref in refs] == [str(attached.id)]
    # Re-read through the item's visibility, never taken from the payload.
    assert reads == [(item["id"], item["user_id"])]


@pytest.mark.asyncio
async def test_the_dispatched_run_inlines_the_newest_round_only(monkeypatch):
    from app.services.assignment_engine import load_dispatched_work_item_files

    rows = [_row() for _ in range(wi_files.MAX_DISPATCH_INLINED_ATTACHMENTS + 3)]
    _store(monkeypatch, *rows)
    item = _item(attachment_file_ids=[str(row.id) for row in rows])

    async def _get(work_item_id, *, user_id, reader=None):
        return item

    monkeypatch.setattr(wi_service, "get_work_item", _get)
    run = _run(context_type="work_item", item_id=item["id"], user_id=item["user_id"])
    refs = await load_dispatched_work_item_files(run)
    newest = rows[-wi_files.MAX_DISPATCH_INLINED_ATTACHMENTS :]
    assert [ref.file_id for ref in refs] == [str(row.id) for row in newest]


@pytest.mark.asyncio
async def test_a_run_that_merely_names_a_work_item_gets_none_of_its_files(monkeypatch):
    from app.services.assignment_engine import load_dispatched_work_item_files

    async def _must_not_read(*args, **kwargs):
        raise AssertionError("only the inbox's own dispatch is read")

    monkeypatch.setattr(wi_service, "get_work_item", _must_not_read)
    item_id = str(uuid.uuid4())
    run = _run(context_type="manual", item_id=item_id, user_id=str(uuid.uuid4()))
    assert await load_dispatched_work_item_files(run) == []
