"""``list_work_items(newest_note_prefix=...)`` shows each item's newest matching note (#2263).

A status sweep over N items (the newest ``PARTNER-STATUS`` note of each
partner ticket) read one ``get_work_item`` per item. With the prefix, each row
carries the newest platform-stamped note whose text starts with it, or
"none", in the full and the compact listing.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import app.services.work_item as wi_service
from app.services.builtin_tools import build_work_item_tools
from app.services.work_item import stamped_note
from app.services.work_item_note_runs import NEWEST_NOTE_MAX_CHARS, newest_note_with_prefix
from app.tools.base import ToolCall

RUN_A = "11111111-1111-4111-8111-111111111111"
RUN_B = "22222222-2222-4222-8222-222222222222"


def _notes(*notes: str) -> str:
    """A description: the notes newest first, then the brief."""
    return "\n\n".join([*notes, "The brief of the item."])


def _stamp(text: str, hour: int, run: str | None = None) -> str:
    from datetime import UTC, datetime

    return stamped_note(text, datetime(2026, 11, 24, hour, 5, tzinfo=UTC), run)


def _item(description: str) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "title": "Partner ticket",
        "status": "open",
        "priority": "medium",
        "owner_agent_id": str(uuid.uuid4()),
        "correlation_key": "office:partner-x",
        "is_stale": False,
        "description": description,
    }


# ── the parser ──────────────────────────────────────────────────────────────


def test_the_newest_matching_note_wins_over_older_ones_and_other_notes():
    description = _notes(
        _stamp("other: not a status", 12, RUN_A),
        _stamp("PARTNER-STATUS: b", 11, RUN_B),
        _stamp("PARTNER-STATUS: a", 10, RUN_A),
    )
    newest = newest_note_with_prefix(description, "PARTNER-STATUS")
    assert newest == {"at": "2026-11-24 11:05Z", "run": RUN_B, "text": "PARTNER-STATUS: b"}


def test_a_note_stamped_without_a_run_is_read_and_an_unstamped_brief_is_not():
    description = _notes(_stamp("PARTNER-STATUS: answered", 9))
    assert newest_note_with_prefix(description, "PARTNER-STATUS") == {
        "at": "2026-11-24 09:05Z",
        "run": "",
        "text": "PARTNER-STATUS: answered",
    }
    assert newest_note_with_prefix("PARTNER-STATUS: in the brief only", "PARTNER-STATUS") is None


def test_no_match_or_an_empty_prefix_answers_none_and_a_long_note_is_bounded():
    assert newest_note_with_prefix(_notes(_stamp("other", 9, RUN_A)), "PARTNER-STATUS") is None
    assert newest_note_with_prefix(_notes(_stamp("PARTNER-STATUS: x", 9, RUN_A)), "  ") is None
    long_note = "PARTNER-STATUS: " + "x" * 1000
    newest = newest_note_with_prefix(_notes(_stamp(long_note, 9, RUN_A)), "PARTNER-STATUS")
    assert newest is not None
    assert len(newest["text"]) == NEWEST_NOTE_MAX_CHARS and newest["text"].endswith("...")


def test_a_note_followed_by_another_stamp_keeps_its_paragraphs():
    description = _notes(
        _stamp("PARTNER-STATUS: line one\n\nline two", 11, RUN_A),
        _stamp("PARTNER-STATUS: older", 10, RUN_B),
    )
    newest = newest_note_with_prefix(description, "PARTNER-STATUS")
    assert newest is not None and newest["text"] == "PARTNER-STATUS: line one\n\nline two"


# ── the tool ────────────────────────────────────────────────────────────────


async def _call(executor, name: str, **arguments):
    return await executor.execute_tool(ToolCall(id=str(uuid.uuid4()), name=name, arguments=arguments))


@pytest.fixture
def _scope(monkeypatch):
    from app.services import coordination_scope

    scope = SimpleNamespace(work_item_clause=lambda: object(), label=lambda: "Scope: fleet.")

    async def _resolve(*_args, **_kwargs):
        return scope

    monkeypatch.setattr(coordination_scope, "resolve_coordination_scope", _resolve)


async def _listed(monkeypatch, rows: list[dict], **arguments):
    async def _list(**_kwargs):
        return rows

    monkeypatch.setattr(wi_service, "list_work_items", _list)
    _defs, executor = build_work_item_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(executor, "list_work_items", **arguments)
    assert not result.is_error
    return result


def _three_items() -> tuple[dict, dict, dict]:
    first = _item(_notes(_stamp("PARTNER-STATUS: b", 11, RUN_B), _stamp("PARTNER-STATUS: a", 10, RUN_A)))
    second = _item(_notes(_stamp("other", 12, RUN_A)))
    third = _item(_notes(_stamp("note", 12, RUN_A), _stamp("PARTNER-STATUS: c", 8, RUN_A)))
    return first, second, third


@pytest.mark.asyncio
@pytest.mark.usefixtures("_scope")
async def test_the_full_listing_names_each_items_newest_matching_note_or_none(monkeypatch):
    first, second, third = _three_items()
    result = await _listed(monkeypatch, [first, second, third], newest_note_prefix="PARTNER-STATUS")
    content = result.content
    assert f"newest note 'PARTNER-STATUS': 2026-11-24 11:05Z (run {RUN_B}): PARTNER-STATUS: b" in content
    assert "newest note 'PARTNER-STATUS': none" in content
    assert f"newest note 'PARTNER-STATUS': 2026-11-24 08:05Z (run {RUN_A}): PARTNER-STATUS: c" in content
    by_id = {row["id"]: row for row in result.structured}
    assert by_id[first["id"]]["newest_note"]["text"] == "PARTNER-STATUS: b"
    assert by_id[second["id"]]["newest_note"] is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("_scope")
async def test_the_compact_listing_carries_the_note_under_its_line(monkeypatch):
    first, second, _third = _three_items()
    result = await _listed(monkeypatch, [first, second], newest_note_prefix="PARTNER-STATUS", compact=True)
    lines = result.content.splitlines()
    index = next(i for i, line in enumerate(lines) if first["id"] in line)
    assert "PARTNER-STATUS: b" in lines[index + 1]
    index = next(i for i, line in enumerate(lines) if second["id"] in line)
    assert lines[index + 1].strip() == "newest note 'PARTNER-STATUS': none"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_scope")
async def test_without_the_prefix_the_listing_is_unchanged(monkeypatch):
    first, _second, _third = _three_items()
    result = await _listed(monkeypatch, [first])
    assert "newest note" not in result.content
    assert "newest_note" not in result.structured[0]
