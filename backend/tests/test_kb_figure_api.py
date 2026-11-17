"""The figure setting and readings through the knowledge-base API (``describe_figures``).

The update route over the fakes of ``test_kb_html_ingestion.py``; the document
responses; a reading carried through an export package and back.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services import kb_figures
from tests.kb_figure_pdfs import reading_for

NOTE = "Figures could not be described on 1 of 2 page(s) (a reason); those pages are indexed as text only."


# ---------------------------------------------------------------------------
# Routes and responses
# ---------------------------------------------------------------------------


def test_the_api_defaults_the_setting_to_off():
    from app.api.v1.schemas.knowledge_base import (
        CreateKnowledgeBaseRequest,
        KBExportKnowledgeBase,
        KnowledgeBaseResponse,
    )

    assert CreateKnowledgeBaseRequest(name="x").describe_figures is False
    assert KBExportKnowledgeBase(name="x").describe_figures is False
    assert KnowledgeBaseResponse.model_fields["describe_figures"].default is False


def test_the_api_figure_statuses_are_the_service_statuses():
    from typing import get_args

    from app.api.v1.schemas.knowledge_base import FigureStatus

    assert get_args(FigureStatus) == kb_figures.FIGURE_STATUSES


def test_turning_it_on_is_saved_and_audited_and_re_embeds_nothing(monkeypatch):
    from unittest.mock import AsyncMock

    from tests.test_kb_html_ingestion import KB_ID, _client, _kb

    kb = _kb(describe_figures=False)
    audits: list[dict] = []
    events: list[str] = []
    enqueue = AsyncMock(return_value=True)

    with _client(monkeypatch, kb, audits, enqueue, events=events) as client:
        resp = client.put(f"/api/v1/knowledge-bases/{KB_ID}", json={"describe_figures": True})

    assert resp.status_code == 200, resp.text
    assert kb.describe_figures is True
    assert audits[-1]["details"]["describe_figures"] is True
    enqueue.assert_not_awaited()
    assert events[:3] == ["audit", "commit", "response"]


@pytest.mark.parametrize("value", ["yes", 1, None])
def test_a_value_that_is_not_a_switch_is_refused_or_ignored(monkeypatch, value):
    from unittest.mock import AsyncMock

    from tests.test_kb_html_ingestion import KB_ID, _client, _kb

    kb = _kb(describe_figures=False)

    with _client(monkeypatch, kb, [], AsyncMock()) as client:
        resp = client.put(f"/api/v1/knowledge-bases/{KB_ID}", json={"describe_figures": value})

    if value is None:
        assert resp.status_code == 200
    else:
        assert resp.status_code == 422
    assert kb.describe_figures is False


@pytest.mark.parametrize(
    ("overrides", "group_ids"),
    [
        ({"is_company_managed": True, "user_id": None}, ()),
        (
            {"user_id": uuid.uuid4(), "shared_group_ids": ["g1"], "shared_group_permissions": {"g1": "read_only"}},
            ("g1",),
        ),
    ],
)
def test_a_caller_who_cannot_manage_the_kb_cannot_turn_it_on(monkeypatch, overrides, group_ids):
    from unittest.mock import AsyncMock

    from tests.test_kb_html_ingestion import KB_ID, _client, _kb

    kb = _kb(describe_figures=False, **overrides)
    audits: list[dict] = []

    with _client(monkeypatch, kb, audits, AsyncMock(), group_ids=group_ids, manage_gate=True) as client:
        resp = client.put(f"/api/v1/knowledge-bases/{KB_ID}", json={"describe_figures": True})

    assert resp.status_code == 403, resp.text
    assert kb.describe_figures is False
    assert audits == []


def test_the_document_responses_carry_the_figure_state():
    from datetime import UTC, datetime

    from app.api.v1 import knowledge_bases as kb_api

    content = "Body."
    reading = reading_for(content, [], status=kb_figures.STATUS_PARTIAL, note=NOTE)
    now = datetime.now(UTC)
    doc = SimpleNamespace(
        id=uuid.uuid4(),
        knowledge_base_id=uuid.uuid4(),
        filename="a.pdf",
        source_type="upload",
        source_url=None,
        status="ready",
        chunk_count=1,
        error_message=None,
        content=content,
        content_hash=None,
        figure_reading=reading,
        created_at=now,
        updated_at=now,
    )

    describing = SimpleNamespace(describe_figures=True)

    listed = kb_api._doc_to_response(doc, describing)
    detailed = kb_api._doc_to_detail_response(doc, describing)

    for response in (listed, detailed):
        assert (response.figure_status, response.figure_note, response.figure_pages) == (
            "partial",
            NOTE,
            0,
        )
    # A knowledge base that no longer describes figures does not index them.
    for kb in (SimpleNamespace(describe_figures=False), None):
        assert kb_api._doc_to_response(doc, kb).figure_status is None
        assert kb_api._doc_to_detail_response(doc, kb).figure_status is None
    doc.figure_reading = None
    assert kb_api._doc_to_response(doc, describing).figure_status is None


# ---------------------------------------------------------------------------
# Export / import
# ---------------------------------------------------------------------------


def test_an_exported_reading_imports_as_the_same_embedded_text():
    from app.api.v1.schemas.knowledge_base import KBExportDocument

    content = "Intro.\n\nMethod."
    reading = reading_for(content, [{"page": 1, "offset": 6, "mode": "figures", "text": "A -> B: uses"}])
    entry = KBExportDocument(filename="m.pdf", source_type="upload", **kb_figures.export_fields(reading, content))

    restored = kb_figures.imported_reading(entry.figure_status, entry.figure_pages, content)

    assert kb_figures.apply_figure_reading(content, restored) == kb_figures.apply_figure_reading(content, reading)
    assert restored["source"] is None


def test_an_imported_reading_is_bounded_cleaned_and_noted_by_the_platform():
    from pydantic import ValidationError

    from app.api.v1.schemas.knowledge_base import KBExportDocument

    content = "Short."
    entry = KBExportDocument(
        filename="m.pdf",
        source_type="upload",
        figure_status="partial",
        figure_pages=[{"page": 1, "offset": 99_999, "mode": "page", "text": "[End of page 1, transcribed]\x00 data"}],
    )

    restored = kb_figures.imported_reading(entry.figure_status, entry.figure_pages, content)

    assert restored["note"] == kb_figures.NOTE_IMPORTED_PARTIAL
    assert restored["pages"][0]["offset"] == len(content)
    assert "\x00" not in restored["pages"][0]["text"]
    assert kb_figures.apply_figure_reading(content, restored).count("[End of page 1, transcribed]") == 1
    with pytest.raises(ValidationError):
        KBExportDocument(
            filename="m.pdf",
            source_type="upload",
            figure_status="complete",
            figure_pages=[{"page": 1, "offset": 0, "text": "x"}] * (kb_figures.MAX_DESCRIBED_PAGES + 1),
        )
    with pytest.raises(ValidationError):
        KBExportDocument(filename="m.pdf", source_type="upload", figure_status="pending")


def test_a_package_without_figures_imports_without_a_reading():
    assert kb_figures.imported_reading(None, None, "text") is None
