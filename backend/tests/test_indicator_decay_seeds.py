"""The indicator decay re-check built-ins.

Two built-in agents - the Guardian collects, enriches and applies; the Judge
decides on the platform routing model - and two assignments on the
Guardian: a manual one, and an hourly one an administrator turns on
(``admin_opt_in``). Pinned here: the seed data the design rests on, the seed
sync leaving an opt-in assignment's switch and run-as alone, and the update
route opening exactly ``enabled`` / ``run_as_user_id`` of exactly those
assignments to an administrator.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from croniter import croniter
from fastapi import HTTPException

from app.agents.builtin_agents import (
    ADMIN_OPT_IN_BUILTIN_ASSIGNMENTS,
    ADMIN_OPT_IN_WRITABLE_FIELDS,
    BUILTIN_AGENTS,
    BUILTIN_ASSIGNMENTS,
    is_admin_opt_in_builtin_assignment,
)
from app.agents.seed import _resolve_seed_content, _resolve_seed_schema, seed_builtin_assignments
from app.api.v1.schemas.assignment import UpdateAssignmentRequest
from tests.test_builtin_assignments_never_disabled import _Result
from tests.test_builtin_hitl_endpoint_wiring import (
    _admin,
    _assignment,
    _assignment_agent,
    _AssignmentDB,
    _invoke_assignment_update,
    _member,
    _request,
)

GUARDIAN = "indicator-decay-guardian"
JUDGE = "indicator-decay-judge"
MANUAL = "Re-check indicators about to decay"
HOURLY = "Hourly indicator decay re-check"


@pytest.fixture(autouse=True)
def _xtm_one_mode(monkeypatch):
    """The built-in read-only rule, and its opt-in carve-out, are XTM One only."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "platform_mode", "xtm_one", raising=False)


def _resuming_admin():
    """An admin as the resume path reads it: it logs who resumed."""
    admin = _admin()
    admin.email = "admin@example.com"
    return admin


def _agent(slug: str) -> dict:
    (entry,) = [a for a in BUILTIN_AGENTS if a["slug"] == slug]
    return entry


def _assignment_seed(name: str) -> dict:
    (entry,) = [a for a in BUILTIN_ASSIGNMENTS if a["name"] == name]
    return entry


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------


def test_the_judge_decides_on_the_routing_model_with_no_tools():
    judge = _agent(JUDGE)

    assert judge["llm_model_source"] == "routing"
    assert "llm_model" not in judge
    assert judge["builtin_tool_ids"] == []
    assert judge["integration_template_bindings"] == []
    assert judge["handover_agent_slugs"] == []
    assert judge["disable_chat"] is True
    assert judge["output_format"] == "json"


def test_the_judge_answers_in_the_three_verdicts():
    schema = _resolve_seed_schema(_agent(JUDGE)["output_schema"])
    item = schema["properties"]["verdicts"]["items"]

    assert item["properties"]["verdict"]["enum"] == [
        "still_malicious",
        "not_confirmed",
        "insufficient_evidence",
    ]
    assert set(item["required"]) == {
        "indicator_id",
        "verdict",
        "confidence",
        "proposed_score",
        "reason",
    }


def test_the_guardian_consults_the_judge_and_never_decides():
    guardian = _agent(GUARDIAN)

    assert guardian["handover_agent_slugs"] == [JUDGE]
    assert guardian["handover_type_slugs"] == {JUDGE: "consult"}
    assert guardian["builtin_tool_ids"] == ["workset"]
    assert guardian["integration_template_bindings"] == ["opencti"]
    assert "llm_model_source" not in guardian


@pytest.mark.parametrize("slug", [GUARDIAN, JUDGE])
def test_both_personas_resolve_from_seed_data(slug):
    persona = _resolve_seed_content("agent_persona", _agent(slug)["persona"])

    assert persona != slug
    assert "Indicator Decay" in persona


def test_the_manual_run_is_admin_only_and_runs_as_its_caller():
    """Company-managed, not per user, no private runs: ``manual_trigger_refusal``
    keeps it to administrators, and it runs as the one who starts it."""
    manual = _assignment_seed(MANUAL)

    assert manual["agent_slug"] == GUARDIAN
    assert manual["trigger_type"] == "manual"
    assert manual["enabled"] is True
    assert manual["run_as_all_users"] is False
    assert manual.get("hide_runs") is None
    assert not manual.get("admin_opt_in")


def test_the_hourly_run_is_off_and_left_to_an_administrator():
    """One writer per indicator: never fanned out per user."""
    hourly = _assignment_seed(HOURLY)

    assert hourly["agent_slug"] == GUARDIAN
    assert hourly["trigger_type"] == "schedule"
    assert croniter.is_valid(hourly["trigger_config"]["cron"])
    assert hourly["enabled"] is False
    assert hourly["admin_opt_in"] is True
    assert hourly["run_as_all_users"] is False


def test_only_the_hourly_run_is_opt_in():
    # The seeded fleet automations are opt-in too (#9160,
    # tests/test_ctem_fleets_opt_in.py); of the guardian's, only the hourly run.
    guardian = {key for key in ADMIN_OPT_IN_BUILTIN_ASSIGNMENTS if key[0] == GUARDIAN}
    assert guardian == {(GUARDIAN, HOURLY)}
    assert frozenset({"enabled", "run_as_user_id"}) == ADMIN_OPT_IN_WRITABLE_FIELDS


@pytest.mark.parametrize(
    ("is_builtin", "slug", "name", "expected"),
    [
        (True, GUARDIAN, HOURLY, True),
        (True, GUARDIAN, MANUAL, False),
        (False, GUARDIAN, HOURLY, False),
        (True, "ctem-discovery-analyst", HOURLY, False),
        (True, None, None, False),
    ],
)
def test_opt_in_is_the_builtin_agent_and_the_seeded_name(is_builtin, slug, name, expected):
    """A user agent that copies the slug and name of the opt-in one is not it."""
    agent = MagicMock(is_builtin=is_builtin, slug=slug)
    assignment = MagicMock()
    assignment.name = name

    assert is_admin_opt_in_builtin_assignment(agent, assignment) is expected


def test_opt_in_is_false_without_an_agent():
    assignment = MagicMock()
    assignment.name = HOURLY

    assert is_admin_opt_in_builtin_assignment(None, assignment) is False


# ---------------------------------------------------------------------------
# Seed sync
# ---------------------------------------------------------------------------


def _seed_db(agent, existing) -> AsyncMock:
    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            _Result(scalar=agent),  # phase 1: agent lookup for the stale sweep
            _Result(items=[]),  # phase 1: no stale assignments
            _Result(scalar=agent),  # phase 2: agent lookup
            _Result(scalar=existing, items=[existing] if existing else []),
        ]
    )
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.add = MagicMock()
    return db


def _guardian_row() -> MagicMock:
    agent = MagicMock()
    agent.id = uuid.uuid4()
    agent.slug = GUARDIAN
    agent.user_id = uuid.uuid4()
    agent.is_company_managed = True
    return agent


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_a_boot_leaves_the_opt_in_switch_and_run_as_as_they_are(monkeypatch, enabled):
    """Turned on by an administrator, it stays on; left off, it stays off and
    is not re-stamped - and the run-as account they chose is kept."""
    monkeypatch.setattr("app.agents.seed.BUILTIN_ASSIGNMENTS", [_assignment_seed(HOURLY)])
    pause = MagicMock()
    monkeypatch.setattr("app.agents.seed.pause_assignment", pause)
    run_as = uuid.uuid4()
    existing = MagicMock()
    existing.enabled = enabled
    existing.run_as_user_id = run_as
    existing.user_id = uuid.uuid4()
    existing.paused_reason = None if enabled else "user_toggle"

    inserted = await seed_builtin_assignments(
        _seed_db(_guardian_row(), existing), platform_mode="xtm_one"
    )

    assert inserted == 0
    assert existing.enabled is enabled
    assert existing.run_as_user_id == run_as
    assert existing.paused_reason == (None if enabled else "user_toggle")
    pause.assert_not_called()


@pytest.mark.asyncio
async def test_a_new_opt_in_assignment_is_born_off_awaiting_an_administrator(monkeypatch):
    monkeypatch.setattr("app.agents.seed.BUILTIN_ASSIGNMENTS", [_assignment_seed(HOURLY)])
    db = _seed_db(_guardian_row(), None)

    inserted = await seed_builtin_assignments(db, platform_mode="xtm_one")

    assert inserted == 1
    (seeded,) = [call.args[0] for call in db.add.call_args_list]
    assert seeded.name == HOURLY
    assert seeded.enabled is False
    assert seeded.run_as_all_users is False
    assert seeded.paused_reason == "created_disabled"
    assert "administrator" in seeded.paused_details


@pytest.mark.asyncio
async def test_a_non_opt_in_builtin_is_still_reenabled_at_boot(monkeypatch):
    """The opt-in exception does not leak to the manual run."""
    monkeypatch.setattr("app.agents.seed.BUILTIN_ASSIGNMENTS", [_assignment_seed(MANUAL)])
    existing = MagicMock()
    existing.enabled = False
    existing.user_id = uuid.uuid4()

    await seed_builtin_assignments(_seed_db(_guardian_row(), existing), platform_mode="xtm_one")

    assert existing.enabled is True


# ---------------------------------------------------------------------------
# PUT /assignments/{id}
# ---------------------------------------------------------------------------


def _opt_in_pair(name: str = HOURLY):
    agent = _assignment_agent()
    agent.slug = GUARDIAN
    assignment = _assignment(agent.id)
    assignment.name = name
    assignment.enabled = False
    return agent, assignment


@pytest.mark.asyncio
async def test_an_admin_turns_the_opt_in_assignment_on(monkeypatch):
    agent, assignment = _opt_in_pair()

    await _invoke_assignment_update(
        monkeypatch, UpdateAssignmentRequest(enabled=True), agent, assignment, _resuming_admin()
    )

    assert assignment.enabled is True


@pytest.mark.asyncio
async def test_an_admin_turns_it_on_and_picks_its_run_as_in_one_save(monkeypatch):
    """Both fields of the carve-out together pass the guard and are written."""
    agent, assignment = _opt_in_pair()
    run_as = str(uuid.uuid4())
    resolved: list = []

    async def _resolve(_db, _agent, raw):
        resolved.append(raw)
        return uuid.UUID(raw)

    monkeypatch.setattr("app.api.v1.assignments._resolve_run_as_user_id", _resolve)

    await _invoke_assignment_update(
        monkeypatch,
        UpdateAssignmentRequest(enabled=True, run_as_user_id=run_as),
        agent,
        assignment,
        _resuming_admin(),
    )

    assert resolved == [run_as]
    assert assignment.run_as_user_id == uuid.UUID(run_as)
    assert assignment.enabled is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        pytest.param(UpdateAssignmentRequest(prompt="rewritten"), id="prompt"),
        pytest.param(UpdateAssignmentRequest(enabled=True, prompt="rewritten"), id="wider"),
        pytest.param(UpdateAssignmentRequest(trigger_config={"cron": "* * * * *"}), id="trigger"),
        pytest.param(UpdateAssignmentRequest(run_as_all_users=True), id="fan-out"),
        pytest.param(UpdateAssignmentRequest(hide_runs=True), id="hide-runs"),
        pytest.param(UpdateAssignmentRequest(), id="empty"),
    ],
)
async def test_the_rest_of_the_opt_in_assignment_stays_read_only(monkeypatch, body):
    agent, assignment = _opt_in_pair()

    with pytest.raises(HTTPException) as exc:
        await _invoke_assignment_update(monkeypatch, body, agent, assignment, _admin())

    assert exc.value.status_code == 403
    assert exc.value.detail == "Cannot edit assignments on built-in agents."
    assert assignment.enabled is False


@pytest.mark.asyncio
async def test_a_non_admin_cannot_turn_it_on(monkeypatch):
    agent, assignment = _opt_in_pair()

    with pytest.raises(HTTPException) as exc:
        await _invoke_assignment_update(
            monkeypatch, UpdateAssignmentRequest(enabled=True), agent, assignment, _member()
        )

    assert exc.value.status_code == 403
    assert assignment.enabled is False


@pytest.mark.asyncio
async def test_the_manual_run_takes_its_switch_but_not_its_run_as(monkeypatch):
    """Every built-in assignment takes its switch (#2192); the run-as identity
    stays the opt-in assignments' alone."""
    agent, assignment = _opt_in_pair(MANUAL)

    with pytest.raises(HTTPException) as exc:
        await _invoke_assignment_update(
            monkeypatch,
            UpdateAssignmentRequest(enabled=True, run_as_user_id=str(uuid.uuid4())),
            agent,
            assignment,
            _admin(),
        )
    assert exc.value.status_code == 403
    assert assignment.enabled is False

    await _invoke_assignment_update(
        monkeypatch, UpdateAssignmentRequest(enabled=True), agent, assignment, _resuming_admin()
    )
    assert assignment.enabled is True


@pytest.mark.asyncio
async def test_the_opt_in_assignment_cannot_be_deleted(monkeypatch):
    """The carve-out is the update route's: delete keeps the built-in gate."""
    from app.api.v1 import assignments as assignments_api

    agent, assignment = _opt_in_pair()

    async def _groups(_db, _user_id):
        return []

    async def _get_user_assignment(_db, _user_id, _aid, _groups=None, **_kwargs):
        return assignment

    monkeypatch.setattr(assignments_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(assignments_api, "get_user_assignment", _get_user_assignment)

    with pytest.raises(HTTPException) as exc:
        await assignments_api.delete_assignment(
            str(assignment.id), _request(), _admin(), _AssignmentDB(agent)
        )

    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# The run prompt names only what its tools take and return
# ---------------------------------------------------------------------------


def test_the_dedupe_key_the_prompt_names_is_the_one_the_list_tool_prints():
    """Step 2 skips indicators already judged by matching ``ref=``; the list
    tool prints exactly that for an item added with an ``external_ref``."""
    from types import SimpleNamespace

    from app.agents.builtin_agents import _INDICATOR_DECAY_PROMPT
    from app.services.builtin_tools.workset import list_item_line

    item = SimpleNamespace(
        item_id=uuid.uuid4(),
        title="[ipv4-addr:value = '203.0.113.7']",
        status="ready",
        external_ref="indicator--5467",
        priority_score=80,
        occurred_at="2026-10-07T10:00:00+00:00",
        summary="still_malicious: two sources",
        corroboration_count=0,
    )

    assert "ref= value" in _INDICATOR_DECAY_PROMPT
    assert "external_ref the\n   indicator id" in _INDICATOR_DECAY_PROMPT
    assert list_item_line(item).endswith(f"cite: item={item.item_id}, ref=indicator--5467")


@pytest.mark.parametrize("value", ["-1d", "-0h", "-22h"])
def test_the_relative_dates_the_prompt_passes_the_workset_parse(value):
    from app.agents.builtin_agents import _INDICATOR_DECAY_PROMPT
    from app.services.builtin_tools.workset import _parse_when

    assert f'"{value}"' in _INDICATOR_DECAY_PROMPT
    assert _parse_when(value) is not None


def test_a_degraded_indicator_is_recorded_with_a_date_that_expires_its_dedupe_in_hours():
    """A degraded verdict decided nothing: its record must leave step 2's
    ``since "-1d"`` window within hours, not after 24 of them, or an indicator
    a few hours from revocation is never enriched again."""
    from datetime import timedelta

    from app.agents.builtin_agents import _INDICATOR_DECAY_PROMPT
    from app.services.builtin_tools.workset import _parse_when

    step_6 = _INDICATOR_DECAY_PROMPT.split("\n6. ", 1)[1].split("\n7. ", 1)[0]
    assert '"-0h"' in step_6
    assert 'or "-22h" for a degraded one' in step_6

    window = _parse_when("-1d")
    judged = _parse_when("-0h")
    degraded = _parse_when("-22h")
    # Skipped by the run right after, judged or degraded.
    assert judged >= window
    assert degraded >= window
    # A degraded record falls out of the window 2 hours later; a judged one
    # stays in it for the full day.
    later_window = window + timedelta(hours=2, minutes=1)
    assert degraded < later_window
    assert judged >= later_window


def test_the_candidate_window_is_relative_so_the_model_computes_no_date():
    """OpenCTI's ``within`` operator takes ``now`` / ``now+48h`` as they are."""
    from app.agents.builtin_agents import _INDICATOR_DECAY_PROMPT

    assert '"values": ["now", "now+48h"], "operator": "within"' in _INDICATOR_DECAY_PROMPT
