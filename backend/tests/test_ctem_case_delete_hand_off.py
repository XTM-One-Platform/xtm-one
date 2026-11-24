"""The hand-off every caller of ``delete_cases`` makes after its commit (#1950): re-decision scheduled, ids dropped."""

from __future__ import annotations

import uuid

import pytest

from app.services import ctem_case_delete as case_delete


def test_released_signals_are_scheduled_and_only_their_count_is_answered(monkeypatch: pytest.MonkeyPatch) -> None:
    scheduled: list[list[uuid.UUID]] = []
    monkeypatch.setattr(case_delete, "schedule_decide_released", lambda ids: scheduled.append(list(ids)))
    ids = [uuid.uuid4() for _ in range(3)]

    answer = case_delete.hand_off_released({"dry_run": False, "count": 1, "cases": [], "released_signals": ids})

    assert scheduled == [ids]
    assert answer == {"dry_run": False, "count": 1, "cases": [], "released": 3}


def test_a_dry_run_hands_off_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    scheduled: list[list[uuid.UUID]] = []
    monkeypatch.setattr(case_delete, "schedule_decide_released", lambda ids: scheduled.append(list(ids)))

    answer = case_delete.hand_off_released({"dry_run": True, "count": 2, "cases": [], "released_signals": []})

    assert scheduled == [[]]
    assert answer["released"] == 0 and "released_signals" not in answer


def test_the_agent_tool_and_the_route_both_hand_off() -> None:
    """Both callers of ``delete_cases`` go through the hand-off (review of #1955: the agent tool left signals new)."""
    from pathlib import Path

    backend = Path(case_delete.__file__).resolve().parents[2]
    for caller in ("app/api/v1/ctem_cases.py", "app/services/builtin_tools/ctem_cases.py"):
        source = (backend / caller).read_text(encoding="utf-8")
        assert "case_delete.delete_cases(" in source
        assert "case_delete.hand_off_released(" in source, caller
