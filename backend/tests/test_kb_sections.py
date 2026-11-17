"""Heading sections of a knowledge document and the section tools.

``app.services.kb_sections`` parses the ATX outline of a markdown source and
addresses one section; ``get_knowledge_document`` reads it (``outline`` /
``section``) and ``update_knowledge_document`` changes it
(``replace_section`` / ``insert_after`` / ``edit`` / ``file_id``) with every
byte outside the section kept.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.kb_sections import (
    SectionError,
    find_section,
    insert_after,
    parse_outline,
    replace_section,
)
from tests.test_kb_full_document_access import (
    DOC_ID,
    _call,
    _doc,
    _FakeSession,
    _kb,
    _patch_session,
    _read_executor,
    _update_executor,
)

DOC = (
    "# Runbook\n"
    "intro\n"
    "## Rules\n"
    "rule text\n"
    "### Merge\n"
    "merge text\n"
    "```\n"
    "## not a heading (fenced)\n"
    "```\n"
    "## Lessons\n"
    "lesson text\n"
    "### Merge\n"
    "lesson merge\n"
    "## Last\n"
    "tail without newline"
)


# ---------------------------------------------------------------------------
# Outline and selector
# ---------------------------------------------------------------------------


def test_outline_lists_every_heading_outside_fenced_code():
    outline = parse_outline(DOC)
    assert [(h.level, h.text) for h in outline] == [
        (1, "Runbook"),
        (2, "Rules"),
        (3, "Merge"),
        (2, "Lessons"),
        (3, "Merge"),
        (2, "Last"),
    ]
    assert all(DOC[h.start :].startswith("#") for h in outline)
    assert outline[0].end == len(DOC)
    assert outline[3].ancestors == ("Runbook",)
    assert outline[4].ancestors == ("Runbook", "Lessons")


def test_a_section_runs_to_the_next_heading_of_the_same_or_higher_level():
    rules = find_section(DOC, "Rules")
    assert DOC[rules.start : rules.end] == (
        "## Rules\nrule text\n### Merge\nmerge text\n```\n## not a heading (fenced)\n```\n"
    )


def test_the_last_section_runs_to_the_end_without_a_trailing_newline():
    last = find_section(DOC, "Last")
    assert DOC[last.start : last.end] == "## Last\ntail without newline"


def test_selector_by_path_index_and_case_insensitive_fallback():
    assert find_section(DOC, "Lessons > Merge").start == DOC.index("### Merge\nlesson")
    assert find_section(DOC, "Runbook > Rules > Merge").start == DOC.index("### Merge\nmerge")
    assert find_section(DOC, "#4").text == "Lessons"
    assert find_section(DOC, "4").text == "Lessons"
    assert find_section(DOC, 2).text == "Rules"
    assert find_section(DOC, "## lessons").text == "Lessons"


def test_a_duplicate_heading_is_refused_with_each_offset():
    with pytest.raises(SectionError) as exc:
        find_section(DOC, "Merge")
    message = str(exc.value)
    assert "matches 2 headings" in message
    assert str(DOC.index("### Merge\nmerge")) in message
    assert str(DOC.index("### Merge\nlesson")) in message


def test_a_missing_heading_names_the_closest_ones():
    with pytest.raises(SectionError) as exc:
        find_section(DOC, "Lesson")
    assert "Closest headings" in str(exc.value)
    assert "'Lessons'" in str(exc.value)


def test_index_out_of_range_and_no_heading_are_refused():
    with pytest.raises(SectionError, match="out of range"):
        find_section(DOC, "#99")
    with pytest.raises(SectionError, match="no markdown heading"):
        find_section("plain text only", "x")


def test_closing_hashes_and_non_headings():
    outline = parse_outline("## Title ##\n#hashtag\n####### seven\n")
    assert [h.text for h in outline] == ["Title"]


# ---------------------------------------------------------------------------
# Pure section writes
# ---------------------------------------------------------------------------


def test_replace_section_keeps_every_byte_outside_the_section():
    rules = find_section(DOC, "Rules")
    new, old, (start, end) = replace_section(DOC, "Rules", "## Rules\nnew rules\n")
    assert new[: rules.start] == DOC[: rules.start]
    assert new[end:] == DOC[rules.end :]
    assert new[start:end] == "## Rules\nnew rules\n"
    assert old.text == "Rules"


def test_replace_section_adds_a_newline_before_the_following_heading():
    new, _, _ = replace_section(DOC, "Lessons", "## Lessons\nno newline")
    assert "no newline\n## Last" in new


def test_replace_section_requires_a_heading_line():
    with pytest.raises(SectionError, match="must be a whole section"):
        replace_section(DOC, "Rules", "just text")


def test_insert_after_goes_after_the_subsections():
    new, anchor, (start, end) = insert_after(DOC, "Rules", "## Added\nadded\n")
    assert anchor.text == "Rules"
    assert new[start:end] == "## Added\nadded\n"
    assert new[end:].startswith("## Lessons")
    assert new.replace("## Added\nadded\n", "", 1) == DOC


def test_insert_after_the_last_section_without_a_trailing_newline():
    new, _, _ = insert_after(DOC, "Last", "## After\nx")
    assert new.endswith("tail without newline\n## After\nx")


def test_a_large_document_outline_and_section_round_trip():
    parts = []
    for i in range(400):
        parts.append(f"## Section {i}\n" + ("body line\n" * 40))
        parts.append(f"### Detail {i}\n" + ("detail line\n" * 0 + "x" * 30 + "\n"))
    big = "# Big\n" + "".join(parts)
    assert len(big) > 160_000
    outline = parse_outline(big)
    assert len(outline) == 801
    target = find_section(big, "Section 200")
    new, _, (start, end) = replace_section(big, "Section 200", "## Section 200\nshort\n")
    assert new[:start] == big[: target.start]
    assert new[end:] == big[target.end :]


# ---------------------------------------------------------------------------
# get_knowledge_document: outline and section
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_document_outline(monkeypatch):
    session = _FakeSession(doc_results=[[_doc(content=DOC)]], kb=_kb())
    _patch_session(monkeypatch, session)
    _, executor = _read_executor()
    out = await _call(executor, "get_knowledge_document", {"document_id": DOC_ID, "outline": True})
    assert "Outline (6 headings" in out
    assert "#4   ## Lessons [offset" in out
    assert "lesson text" not in out


@pytest.mark.asyncio
async def test_get_document_section_is_verbatim(monkeypatch):
    session = _FakeSession(doc_results=[[_doc(content=DOC)]], kb=_kb())
    _patch_session(monkeypatch, session)
    _, executor = _read_executor()
    out = await _call(executor, "get_knowledge_document", {"document_id": DOC_ID, "section": "Lessons"})
    lessons = find_section(DOC, "Lessons")
    assert DOC[lessons.start : lessons.end] in out
    assert f"characters {lessons.start}-{lessons.end} of {len(DOC)}" in out
    assert "rule text" not in out


@pytest.mark.asyncio
async def test_get_document_ambiguous_section_is_refused(monkeypatch):
    session = _FakeSession(doc_results=[[_doc(content=DOC)]], kb=_kb())
    _patch_session(monkeypatch, session)
    _, executor = _read_executor()
    out = await _call(executor, "get_knowledge_document", {"document_id": DOC_ID, "section": "Merge"})
    assert "Error: section 'Merge' matches 2 headings" in out


# ---------------------------------------------------------------------------
# update_knowledge_document: section modes, edit, file_id, guards
# ---------------------------------------------------------------------------


@pytest.fixture
def _stub_update(monkeypatch):
    enqueue = AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.kb_helpers.enqueue_document_processing", enqueue)
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "app.api.v1.helpers.is_package_origin_knowledge_base",
        AsyncMock(return_value=False),
    )
    return enqueue


@pytest.mark.asyncio
async def test_update_replace_section_changes_only_that_section(monkeypatch, _stub_update):
    doc = _doc(content=DOC)
    _patch_session(monkeypatch, _FakeSession(doc_results=[[doc]], kb=_kb()))
    _, executor = _update_executor()
    out = await _call(
        executor,
        "update_knowledge_document",
        {
            "document_id": DOC_ID,
            "mode": "replace_section",
            "section": "Lessons > Merge",
            "content": "### Merge\nfixed\n",
        },
    )
    assert doc.content == DOC.replace("### Merge\nlesson merge\n", "### Merge\nfixed\n")
    assert doc.status == "pending"
    _stub_update.assert_awaited_once_with(DOC_ID)
    assert "Replaced one section of" in out
    assert "every character outside it is unchanged" in out


@pytest.mark.asyncio
async def test_update_insert_after(monkeypatch, _stub_update):
    doc = _doc(content=DOC)
    _patch_session(monkeypatch, _FakeSession(doc_results=[[doc]], kb=_kb()))
    _, executor = _update_executor()
    out = await _call(
        executor,
        "update_knowledge_document",
        {"document_id": DOC_ID, "mode": "insert_after", "section": "#2", "content": "## New\nn\n"},
    )
    assert "## New\nn\n## Lessons" in doc.content
    assert "Inserted a section into" in out


@pytest.mark.asyncio
async def test_update_section_mode_without_section_is_refused():
    _, executor = _update_executor()
    out = await _call(
        executor,
        "update_knowledge_document",
        {"document_id": DOC_ID, "mode": "replace_section", "content": "## X\n"},
    )
    assert "needs 'section'" in out


@pytest.mark.asyncio
async def test_update_ambiguous_section_writes_nothing(monkeypatch, _stub_update):
    doc = _doc(content=DOC)
    session = _FakeSession(doc_results=[[doc]], kb=_kb())
    _patch_session(monkeypatch, session)
    _, executor = _update_executor()
    out = await _call(
        executor,
        "update_knowledge_document",
        {"document_id": DOC_ID, "mode": "replace_section", "section": "Merge", "content": "### Merge\n"},
    )
    assert "matches 2 headings" in out
    assert "Nothing was changed" in out
    assert doc.content == DOC
    assert session.commits == 0
    _stub_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_edit_mode_applies_find_replace(monkeypatch, _stub_update):
    doc = _doc(content=DOC)
    _patch_session(monkeypatch, _FakeSession(doc_results=[[doc]], kb=_kb()))
    _, executor = _update_executor()
    out = await _call(
        executor,
        "update_knowledge_document",
        {"document_id": DOC_ID, "mode": "edit", "edits": [{"find": "rule text", "replace": "rule TEXT"}]},
    )
    assert doc.content == DOC.replace("rule text", "rule TEXT")
    assert "Edited" in out


@pytest.mark.asyncio
async def test_update_edit_mode_refuses_a_missing_snippet(monkeypatch, _stub_update):
    doc = _doc(content=DOC)
    session = _FakeSession(doc_results=[[doc]], kb=_kb())
    _patch_session(monkeypatch, session)
    _, executor = _update_executor()
    out = await _call(
        executor,
        "update_knowledge_document",
        {"document_id": DOC_ID, "mode": "edit", "edits": [{"find": "absent", "replace": "x"}]},
    )
    assert "not found" in out
    assert doc.content == DOC
    assert session.commits == 0


@pytest.mark.asyncio
async def test_update_refuses_a_file_marker_as_content():
    _, executor = _update_executor()
    out = await _call(
        executor,
        "update_knowledge_document",
        {"document_id": DOC_ID, "content": "[[FILE:0452276e-8a43-4875-977f-965330bad0b9]]"},
    )
    assert "only a file marker" in out
    assert "file_id" in out


@pytest.mark.asyncio
async def test_update_refuses_a_large_shrink_without_confirmation(monkeypatch, _stub_update):
    long_source = "# Doc\n" + "x" * 5_000
    doc = _doc(content=long_source)
    session = _FakeSession(doc_results=[[doc]], kb=_kb())
    _patch_session(monkeypatch, session)
    _, executor = _update_executor()
    out = await _call(executor, "update_knowledge_document", {"document_id": DOC_ID, "content": "short"})
    assert "would shrink the document" in out
    assert doc.content == long_source
    assert session.commits == 0


@pytest.mark.asyncio
async def test_update_shrink_with_confirmation_is_written(monkeypatch, _stub_update):
    doc = _doc(content="# Doc\n" + "x" * 5_000)
    _patch_session(monkeypatch, _FakeSession(doc_results=[[doc]], kb=_kb()))
    _, executor = _update_executor()
    await _call(
        executor,
        "update_knowledge_document",
        {"document_id": DOC_ID, "content": "short", "confirm_shrink": True},
    )
    assert doc.content == "short"


@pytest.mark.asyncio
async def test_update_with_a_file_id_uses_the_file_text(monkeypatch, _stub_update):
    doc = _doc(content=DOC)
    _patch_session(monkeypatch, _FakeSession(doc_results=[[doc]], kb=_kb()))
    ref = SimpleNamespace(file_id=str(uuid.uuid4()), filename="runbook.md")
    monkeypatch.setattr("app.services.builtin_tools.kb.resolve_file_ref", lambda _ctx, _fid: ref)
    monkeypatch.setattr(
        "app.services.chunking.extract_text_from_file_async",
        AsyncMock(return_value="## Lessons\nfrom the file\n"),
    )
    context = SimpleNamespace(read_bytes=AsyncMock(return_value=b"bytes"))
    _, executor = _update_executor()
    executor.file_context = context
    out = await _call(
        executor,
        "update_knowledge_document",
        {"document_id": DOC_ID, "mode": "replace_section", "section": "Lessons", "file_id": ref.file_id},
    )
    assert "## Lessons\nfrom the file\n## Last" in doc.content
    assert "Replaced one section of" in out


@pytest.mark.asyncio
async def test_update_with_an_unknown_file_id_writes_nothing(monkeypatch, _stub_update):
    monkeypatch.setattr("app.services.builtin_tools.kb.resolve_file_ref", lambda _ctx, _fid: None)
    _, executor = _update_executor()
    out = await _call(executor, "update_knowledge_document", {"document_id": DOC_ID, "file_id": "nope"})
    assert "no file 'nope'" in out
    _stub_update.assert_not_awaited()
