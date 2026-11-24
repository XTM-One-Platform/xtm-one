"""The chat onboarding of the organization profile (#11224).

The next-question logic, the answers and skips it saves, the rendering the
assistant reads, and the tool's guards and wiring. No database: the pure
functions, and the tool against a patched store.
"""

from __future__ import annotations

import ast
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import organization_context as oc
from app.services import organization_onboarding as onboarding
from app.services.builtin_tools import ORGANIZATION_ONBOARDING_TOOL, build_organization_onboarding_tool
from app.tools.base import ToolCall

APP_DIR = Path(__file__).resolve().parents[1] / "app"


def _profile(**fields) -> oc.OrganizationProfile:
    return oc.parse_profile({"name": "Acme", "sector": "Energy", "regions": ["FR"], "size": "251-1000", **fields})


def test_every_contract_field_is_in_exactly_one_group_with_a_hint():
    grouped = [path for group in onboarding.ONBOARDING_GROUPS for path in group]
    assert sorted(grouped) == sorted(path for path, _ in oc.CONTRACT_FIELDS)
    assert len(grouped) == len(set(grouped))
    assert set(onboarding.FIELD_HINTS) == set(grouped)


def test_the_next_question_is_the_first_missing_field():
    assert onboarding.next_group(_profile()) == ["crown_jewels"]
    assert onboarding.next_group(oc.parse_profile({})) == ["name"]


def test_currency_and_hourly_rate_are_asked_together():
    profile = _profile(
        crown_jewels=[{"name": "SCADA"}],
        tech_stack=[{"product": "Windows"}],
        pir_ids=["pir-1"],
    )
    assert onboarding.next_group(profile) == ["currency", "loaded_hourly_rate"]
    assert onboarding.next_group(profile.model_copy(update={"currency": "EUR"})) == ["loaded_hourly_rate"]


def test_a_skipped_field_is_not_asked_again_and_still_reported_missing():
    profile = onboarding.skip_fields(_profile(), ["crown_jewels"])
    assert profile.onboarding_skipped == ["crown_jewels"]
    assert onboarding.next_group(profile) == ["tech_stack"]
    assert "crown_jewels" in oc.missing_fields(profile)


def test_a_filled_field_is_never_skipped_and_an_unknown_one_is_refused():
    assert onboarding.skip_fields(_profile(), ["sector"]).onboarding_skipped == []
    with pytest.raises(onboarding.OnboardingError, match="not a field"):
        onboarding.skip_fields(_profile(), ["favourite_colour"])
    with pytest.raises(onboarding.OnboardingError, match="at least one"):
        onboarding.skip_fields(_profile(), [])


def test_an_answer_is_validated_and_leaves_the_skipped_list():
    profile = onboarding.skip_fields(_profile(), ["currency", "loaded_hourly_rate"])
    updated = onboarding.apply_answers(profile, {"currency": "eur", "loaded_hourly_rate": 95})
    assert updated.currency == "EUR"
    assert updated.loaded_hourly_rate == 95
    assert updated.onboarding_skipped == []


def test_a_nested_answer_is_set_in_place():
    updated = onboarding.apply_answers(_profile(), {"risk_appetite.approval_threshold": 50000})
    assert updated.risk_appetite.approval_threshold == 50000
    assert updated.risk_appetite.annual_loss_threshold is None


@pytest.mark.parametrize(
    ("answers", "message"),
    [
        ({}, "at least one"),
        ({"favourite_colour": "blue"}, "not a field"),
        ({"currency": "EU1"}, "ISO 4217"),
        ({"ticketing": {"integration": "jira", "project": ""}}, "empty or incomplete"),
    ],
)
def test_a_bad_answer_is_refused_with_its_reason(answers, message):
    with pytest.raises(onboarding.OnboardingError, match=message):
        onboarding.apply_answers(_profile(), answers)


def test_onboarding_skipped_keeps_contract_paths_only():
    assert oc.parse_profile({"onboarding_skipped": ["tech_stack", "x", "tech_stack"]}).onboarding_skipped == [
        "tech_stack"
    ]


def test_the_step_names_the_next_question_and_the_completion():
    text = onboarding.render_step(_profile(), version=3, saved=["sector"])
    assert "Saved as version 3: sector." in text
    assert "4 of 12 fields filled" in text
    assert "crown_jewels:" in text
    full = {
        "crown_jewels": [{"name": "SCADA"}],
        "tech_stack": [{"product": "Windows"}],
        "pir_ids": ["pir-1"],
        "currency": "EUR",
        "loaded_hourly_rate": 90,
        "risk_appetite": {"annual_loss_threshold": 1, "approval_threshold": 2},
    }
    done = onboarding.skip_fields(_profile(**full), ["ticketing"])
    assert "onboarding is complete" in onboarding.render_step(done, version=9)
    assert "/settings/organization" in onboarding.render_step(done, version=9)


def test_the_agent_profile_never_shows_the_skipped_list():
    row = SimpleNamespace(
        profile=onboarding.skip_fields(_profile(), ["tech_stack"]).model_dump(mode="json"),
        version=2,
        created_at=datetime(2026, 11, 11, tzinfo=UTC),
        source="ai_draft",
    )
    assert "onboarding_skipped" not in oc.render_for_agent(row)  # type: ignore[arg-type]


async def test_a_non_administrator_cannot_save_or_skip():
    _defs, executor = build_organization_onboarding_tool(user_id=str(uuid.uuid4()), caller_is_admin=False)
    for args in ({"action": "save", "answers": {"currency": "EUR"}}, {"action": "skip", "fields": ["currency"]}):
        result = await executor.execute_tool(ToolCall(id="1", name=ORGANIZATION_ONBOARDING_TOOL, arguments=args))
        assert "Refused" in str(result.content if hasattr(result, "content") else result)


async def test_an_unknown_action_is_refused():
    _defs, executor = build_organization_onboarding_tool(user_id=str(uuid.uuid4()), caller_is_admin=True)
    result = await executor.execute_tool(
        ToolCall(id="1", name=ORGANIZATION_ONBOARDING_TOOL, arguments={"action": "drop"})
    )
    assert "Unknown action" in str(result.content if hasattr(result, "content") else result)


@pytest.mark.parametrize("source", ["services/agent_chat.py", "services/general_chat.py"])
def test_both_chats_offer_the_tool_to_an_administrator_only(source):
    text = (APP_DIR / source).read_text(encoding="utf-8")
    tree = ast.parse(text)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "build_organization_onboarding_tool"
    ]
    assert calls, f"build_organization_onboarding_tool is not called from {source}"
    guards = [
        node for node in ast.walk(tree) if isinstance(node, ast.If) and any(call in ast.walk(node) for call in calls)
    ]
    assert any("admin" in ast.unparse(guard.test) for guard in guards)
