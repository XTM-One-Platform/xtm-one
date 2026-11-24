"""Where the CTEM loop guided setup stands (#1087 slice 2): each step's state, its facts, the step to resume on."""

from __future__ import annotations

import pytest

from app.services import fleet_setup_status as s
from app.services.organization_setup_prompt import STEPS

_FULL_PROFILE = s.ProfileFacts(
    saved=True, primary_assets=3, tech_stack=5, ticketing_target=True, scope_min_criticality="high"
)
_LOOP_ON = s.LoopFleetFacts(
    fleet_id="f1",
    autonomy_level="low",
    opt_in="on",
    automations_total=4,
    automations_enabled=4,
    waiting_gates=("ctem.mobilize",),
)


def _status(**kw: object) -> s.SetupStatus:
    args: dict[str, object] = {
        "recorded": {},
        "profile": s.ProfileFacts(),
        "modules": [],
        "slots": s.slot_summary(()),
        "fleet": s.LoopFleetFacts(),
    }
    args.update(kw)
    return s.setup_status(**args)  # type: ignore[arg-type]


def _states(status: s.SetupStatus) -> dict[str, str]:
    return {step.key: step.state for step in status.steps}


def test_a_fresh_instance_has_every_step_to_do_and_resumes_on_the_organization() -> None:
    status = _status()
    assert [step.key for step in status.steps] == list(STEPS)
    assert set(_states(status).values()) == {s.STEP_TODO}
    assert status.resume_on == "organization"


def test_a_recorded_step_wins_over_what_the_platform_holds() -> None:
    status = _status(recorded={"organization": "skipped", "modules": "done"})
    assert _states(status)["organization"] == "skipped"
    assert _states(status)["modules"] == "done"
    assert status.resume_on == "ecosystem"


def test_a_step_already_in_place_reads_ready_and_is_still_resumed_on() -> None:
    status = _status(profile=_FULL_PROFILE, modules=["opencti"])
    assert _states(status)["organization"] == s.STEP_READY
    assert _states(status)["modules"] == s.STEP_READY
    assert status.resume_on == "organization"


@pytest.mark.parametrize(
    "profile",
    [
        s.ProfileFacts(saved=True, primary_assets=0, tech_stack=4),
        s.ProfileFacts(saved=True, primary_assets=2, tech_stack=0),
        s.ProfileFacts(saved=False, primary_assets=2, tech_stack=4),
    ],
    ids=["no-primary-asset", "no-tech-stack", "never-saved"],
)
def test_the_organization_step_needs_a_saved_profile_with_primary_assets_and_a_tech_stack(
    profile: s.ProfileFacts,
) -> None:
    assert _states(_status(profile=profile))["organization"] == s.STEP_TODO


def test_the_modules_step_names_each_missing_module_with_what_the_loop_loses() -> None:
    step = next(x for x in _status(modules=["openaev", "unrelated"]).steps if x.key == "modules")
    assert step.facts["connected"] == ["openaev"]
    assert set(step.facts["missing"]) == {"opencti", "opencrq"}
    assert all(step.facts["missing"].values())


def test_the_slot_summary_keeps_the_best_state_of_each_capability_across_members() -> None:
    summary = s.slot_summary(
        [
            [{"capability": "ticketing", "state": "empty"}, {"capability": "notification", "state": "unavailable"}],
            [{"capability": "ticketing", "state": "filled"}, {"capability": "siem", "state": "filled"}],
        ]
    )
    assert summary == {
        "edr_xdr": "unavailable",
        "ticketing": "filled",
        "scanner": "unavailable",
        "notification": "unavailable",
    }


def test_the_ecosystem_step_is_ready_once_one_slot_is_filled() -> None:
    assert _states(_status(slots={"ticketing": "empty"}))["ecosystem"] == s.STEP_TODO
    assert _states(_status(slots={"ticketing": "filled"}))["ecosystem"] == s.STEP_READY


def test_without_the_loop_fleet_the_fleet_steps_stay_to_do() -> None:
    states = _states(_status())
    assert (states["gates"], states["preview"], states["enable"]) == (s.STEP_TODO,) * 3


def test_the_enable_step_is_ready_only_when_every_automation_is_on() -> None:
    partial = s.LoopFleetFacts(fleet_id="f1", autonomy_level="low", opt_in="partial", automations_total=4)
    assert _states(_status(fleet=partial))["enable"] == s.STEP_TODO
    assert _states(_status(fleet=_LOOP_ON))["enable"] == s.STEP_READY
    gates = next(x for x in _status(fleet=_LOOP_ON).steps if x.key == "gates")
    assert gates.facts == {"autonomy_level": "low", "waiting_gates": ["ctem.mobilize"]}


def test_nothing_to_resume_once_every_step_is_done_or_skipped() -> None:
    recorded = {key: ("skipped" if i % 2 else "done") for i, key in enumerate(STEPS)}
    assert _status(recorded=recorded).resume_on is None


def test_a_garbage_recorded_value_is_ignored() -> None:
    assert _states(_status(recorded={"organization": "maybe"}))["organization"] == s.STEP_TODO
