from __future__ import annotations

import uuid

import pytest

import app.services.integration_tools as integration_tools
from app.services.assignment_engine import (
    _format_missing_integration_notice,
    _format_missing_integration_prompt_note,
)


class _EmptyRowsResult:
    def scalars(self):
        return self

    def all(self):
        return []


class _FakeDB:
    async def execute(self, _statement):
        return _EmptyRowsResult()


@pytest.mark.asyncio
async def test_build_integration_tools_records_missing_binding_in_context():
    missing_id = str(uuid.uuid4())
    context: dict[str, object] = {"source": "assignment", "assignment": "Weekly XTM One Demo Audit"}

    defs, _executor = await integration_tools._build_integration_tools_inner(  # type: ignore[arg-type]
        _FakeDB(),
        {"hubspot": missing_id},
        context,
    )

    assert defs == []
    assert context["missing_integration_bindings"] == [
        {
            "binding_key": "hubspot",
            "provider": "hubspot",
            "integration_id": missing_id,
        }
    ]


def test_format_missing_integration_notice_markdown_and_html():
    missing = [
        {"provider": "hubspot", "integration_id": str(uuid.uuid4())},
    ]

    markdown_notice = _format_missing_integration_notice(missing, "markdown")
    html_notice = _format_missing_integration_notice(missing, "html")

    assert markdown_notice.startswith("⚠️ Tool Availability Notice:")
    assert "hubspot (" in markdown_notice
    assert html_notice.startswith("<p><strong>Tool Availability Notice:</strong>")


def test_format_missing_integration_notice_empty_and_blank_entries():
    assert _format_missing_integration_notice([], "markdown") == ""
    # Entries that sanitize down to nothing fall back to "unknown".
    notice = _format_missing_integration_notice(
        [{"provider": "   ", "integration_id": ""}], "markdown"
    )
    assert "unknown" in notice


def test_format_missing_integration_notice_dedupes_and_truncates():
    dup = {"provider": "hubspot", "integration_id": "abc"}
    extras = [
        {"provider": f"provider{i}", "integration_id": ""} for i in range(4)
    ]
    notice = _format_missing_integration_notice([dup, dup, *extras], "markdown")

    # Duplicate (provider, id) pairs collapse to one label.
    assert notice.count("hubspot (abc)") == 1
    # 5 unique labels -> 3 shown + "+2 more".
    assert "+2 more" in notice
    assert "provider2" not in notice


def test_format_missing_integration_notice_escapes_html_and_control_chars():
    missing = [
        {"provider": "<script>alert(1)</script>", "integration_id": "x\ny"},
    ]

    html_notice = _format_missing_integration_notice(missing, "html")
    assert "<script>" not in html_notice
    assert "&lt;script&gt;" in html_notice

    markdown_notice = _format_missing_integration_notice(missing, "markdown")
    # Control characters are collapsed so labels stay single-line.
    assert "\n" not in markdown_notice.replace("⚠️", "")
    assert "x y" in markdown_notice


def test_format_missing_integration_prompt_note():
    missing = [
        {"provider": "hubspot", "integration_id": "a"},
        {"provider": "hubspot", "integration_id": "b"},
        {"provider": 'not"ion', "integration_id": ""},
    ]

    note = _format_missing_integration_prompt_note(missing)

    assert note.startswith("\n\n**Tool Availability Notice**")
    # Providers dedupe and are json.dumps-escaped.
    assert note.count('"hubspot"') == 1
    assert '"not\\"ion"' in note

    assert _format_missing_integration_prompt_note([]) == ""


def test_a_withheld_binding_is_said_with_its_reason_by_provider_only():
    from app.services.integration_credentials import invisible_binding, no_person_binding

    hidden = {**invisible_binding("jira", uuid.uuid4()), "integration_name": "Owner's Jira"}
    nobody = no_person_binding("github", uuid.uuid4())
    notice = _format_missing_integration_notice([hidden, hidden, nobody], "markdown")

    assert notice.count("jira (the acting user cannot see it)") == 1
    assert "github (the run acts for no person it could be checked against)" in notice
    assert "Owner's Jira" not in notice and hidden["integration_id"] not in notice
