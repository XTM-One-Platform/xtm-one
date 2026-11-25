"""A deleted CTEM case names its threat package and a read answers deleted, not a bare miss (#2328)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from app.services import ctem_case_delete as case_delete

NOW = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)
DRAFT = "https://ctem.octi.example/dashboard/data/import/draft/52ad5359-0000-4000-8000-000000000000"


def _deletion(package: dict[str, Any] | None) -> case_delete.CaseDeletion:
    return case_delete.CaseDeletion(
        id=str(uuid.uuid4()),
        title="Upgrade Windows",
        state="in_review",
        keys=["upgrade:microsoft/windows@latest"],
        opened_by="signal",
        severity="high",
        created_at=NOW.isoformat(),
        members=3,
        events=5,
        threat_package=package,
    )


def test_the_summary_names_the_threat_package_left_in_opencti() -> None:
    package = {
        "grouping_id": "fb93530a",
        "name": "Windows package",
        "url": DRAFT,
        "coverage_id": "e28341aa",
        "at": None,
    }
    summary = _deletion(package).summary()
    assert summary["threat_package"]["grouping_id"] == "fb93530a"
    assert summary["threat_package"]["url"] == DRAFT
    assert "not remove" in summary["threat_package"]["left_in_opencti"]


def test_a_case_with_no_package_says_none() -> None:
    assert _deletion(None).summary()["threat_package"] is None


def test_a_read_of_a_deleted_case_answers_who_when_why_and_the_package() -> None:
    case_id = str(uuid.uuid4())
    row = SimpleNamespace(
        created_at=NOW,
        user_email="admin@test.local",
        details={
            "reason": "legacy reset",
            "case": {"threat_package": {"grouping_id": "fb93530a", "name": "Windows package", "url": DRAFT}},
        },
    )
    text = case_delete.deleted_case_text(case_id, row)
    assert text.startswith(f"Case {case_id} was deleted on {NOW.isoformat()} by admin@test.local (legacy reset).")
    assert "Grouping fb93530a" in text and DRAFT in text and "left in OpenCTI" in text
    assert "No case" not in text


def test_a_deleted_case_with_no_package_and_no_reason_still_says_deleted() -> None:
    row = SimpleNamespace(created_at=NOW, user_email=None, details=None)
    text = case_delete.deleted_case_text("abc", row)
    assert text.startswith("Case abc was deleted on") and "by an administrator." in text
    assert "threat package" not in text


def test_both_case_reads_fall_back_to_the_deleted_answer() -> None:
    from pathlib import Path

    tools = Path(__file__).resolve().parents[1] / "app" / "services" / "builtin_tools"
    for name in ("ctem_read.py", "ctem_cases.py"):
        assert "deleted_case_answer(db, wanted)" in (tools / name).read_text(encoding="utf-8"), name
