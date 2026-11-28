"""CTEM decisions of the same kind for one person are asked once (#2451): the pure rules."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.services import ctem_decision_groups as groups

NOW = datetime(2026, 11, 26, 7, 0, tzinfo=UTC)


def _case(**over: object) -> groups.CaseFacts:
    base: dict[str, object] = {
        "case_id": str(uuid.uuid4()),
        "title": "Measure kibana-1 for the first time (no test evidence yet)",
        "trigger_path": "control_decay",
        "severity": "P3",
        "sla_due_at": NOW + timedelta(days=30),
    }
    base.update(over)
    return groups.CaseFacts(**base)  # type: ignore[arg-type]


def test_a_first_measure_case_is_groupable() -> None:
    assert groups.group_kind(_case()) == groups.FIRST_MEASURE


def test_any_other_case_stays_individual() -> None:
    assert groups.group_kind(_case(title="Re-test the control on kibana-1")) is None
    assert groups.group_kind(_case(trigger_path="new_vulnerability", title="Upgrade Chrome")) is None


def test_a_p1_near_its_sla_keeps_its_own_card() -> None:
    assert groups.stays_individual(_case(severity="P1", sla_due_at=NOW + timedelta(hours=10)), NOW)
    assert not groups.stays_individual(_case(severity="P1", sla_due_at=NOW + timedelta(days=10)), NOW)
    assert not groups.stays_individual(_case(severity="P2", sla_due_at=NOW + timedelta(hours=1)), NOW)


def test_the_group_key_names_no_case() -> None:
    key = groups.group_key(groups.FIRST_MEASURE, "user-abc")
    assert groups.is_group_key(key)
    assert groups.case_id_of(key) is None
    case_id = str(uuid.uuid4())
    assert groups.case_id_of(f"ctem:validation:{case_id}") == case_id
    assert groups.case_id_of("ctem:review:" + case_id) is None


def _members(n: int) -> list[dict[str, object]]:
    return [
        {
            "item_id": str(uuid.uuid4()),
            "case_id": str(uuid.uuid4()),
            "title": f"Measure host-{i} for the first time (no test evidence yet)",
            "severity": "P2" if i % 2 else "P3",
            "due_at": (NOW + timedelta(days=i + 1)).isoformat(),
            "priority": "medium",
        }
        for i in range(n)
    ]


def test_the_card_counts_the_cases_and_fits_its_limits() -> None:
    members = _members(500)
    ask = groups.build_ask(groups.FIRST_MEASURE, members, cases_url="https://x/ctem/cases")
    assert "500" in ask["question"]
    assert len(ask["context"]) <= 800
    assert "more on the cases list" in ask["context"]
    assert [o["key"] for o in ask["options"]] == [
        groups.APPROVE_ALL,
        groups.APPROVE_SELECTED,
        groups.DECLINE_ALL,
        groups.LATER,
    ]
    assert ask["options"][-1]["keeps_waiting"] is True
    assert ask["recommendation"] == groups.APPROVE_ALL
    assert ask["links"][0]["url"] == "https://x/ctem/cases"
    from app.services.decision_ask import normalize_ask, person_ask_missing

    assert normalize_ask(ask) is not None
    assert person_ask_missing(ask) == []


def test_the_due_date_is_the_earliest_of_the_group() -> None:
    members = _members(3)
    assert groups.earliest_due(members) == NOW + timedelta(days=1)
    assert groups.earliest_due([{"due_at": None}]) is None


def test_approve_selected_reads_the_card_numbers_and_ranges() -> None:
    members = _members(6)
    chosen = groups.selected_members(members, "approve 1 and 4-5")
    assert [m["case_id"] for m in chosen] == [members[0]["case_id"], members[3]["case_id"], members[4]["case_id"]]
    assert groups.selected_members(members, "all of them") == []
    assert groups.selected_members(members, "9") == []
    assert groups.selected_members(members, "skip host-2 and kibana3") == []


# --- #2801: each member answers with an option of its own gate ---------------------------------


def _gate_ask(keys: list[str], recommended: str) -> dict[str, object]:
    return {
        "question": "How should this asset be measured?",
        "context": "No test evidence yet.",
        "options": [{"key": k, "label": k.replace("_", " ").capitalize()} for k in keys],
        "recommendation": recommended,
    }


AGENT_SET = ["install_agent", "connect_scanner", "rerun_interim", "ticket_instead"]
SCANNER_SET = ["connect_scanner", "agent_then_scenario", "ticket_instead", "do_not_run"]


def test_a_gate_joins_only_with_a_recommended_option() -> None:
    keys, recommended, label = groups.member_choice(_gate_ask(AGENT_SET, "install_agent"))  # type: ignore[misc]
    assert keys == tuple(sorted(AGENT_SET))
    assert (recommended, label) == ("install_agent", "Install agent")
    assert groups.member_choice({**_gate_ask(AGENT_SET, "install_agent"), "recommendation": None}) is None
    assert groups.member_choice(_gate_ask(["approve"], "approve")) is None
    assert groups.member_choice(None) is None


def test_gates_with_different_option_sets_never_share_a_group() -> None:
    a = groups.member_choice(_gate_ask(AGENT_SET, "install_agent"))
    b = groups.member_choice(_gate_ask(SCANNER_SET, "connect_scanner"))
    assert a and b
    assert groups.group_key(groups.FIRST_MEASURE, "user-1", a[0]) != groups.group_key(
        groups.FIRST_MEASURE, "user-1", b[0]
    )
    same = groups.member_choice(_gate_ask(list(reversed(AGENT_SET)), "connect_scanner"))
    assert same and groups.group_key(groups.FIRST_MEASURE, "user-1", same[0]) == groups.group_key(
        groups.FIRST_MEASURE, "user-1", a[0]
    )
    assert groups.case_id_of(groups.group_key(groups.FIRST_MEASURE, "user-1", a[0])) is None


def test_the_card_lists_each_case_with_its_option_and_never_says_launch() -> None:
    members = [{**m, "recommended": "install_agent", "recommended_label": "Install agent"} for m in _members(2)]
    ask = groups.build_ask(groups.FIRST_MEASURE, members, option_keys=sorted(AGENT_SET))
    lines = ask["context"].splitlines()
    assert lines[1].endswith(": Install agent") and lines[2].endswith(": Install agent")
    text = f"{ask['question']} {ask['context']}".lower()
    assert "launch" not in text and "not read-only" not in text
    # No member offers a decline: the card does not offer "decline all".
    assert groups.DECLINE_ALL not in [o["key"] for o in ask["options"]]
    with_decline = groups.build_ask(groups.FIRST_MEASURE, members, option_keys=sorted(SCANNER_SET))
    assert groups.DECLINE_ALL in [o["key"] for o in with_decline["options"]]
    from app.services.decision_ask import normalize_ask, person_ask_missing

    assert normalize_ask(ask) is not None
    assert person_ask_missing(ask) == []


def test_each_member_maps_to_its_own_option() -> None:
    assert groups.member_answer(_gate_ask(AGENT_SET, "install_agent"), approving=True, legacy=False) == (
        "install_agent",
        "Install agent",
    )
    assert groups.member_answer(_gate_ask(AGENT_SET, "install_agent"), approving=False, legacy=False) is None
    assert groups.member_answer(_gate_ask(SCANNER_SET, "connect_scanner"), approving=False, legacy=False) == (
        "do_not_run",
        "Do not run",
    )
    # A binary gate behaves as before.
    assert groups.member_answer(_gate_ask(["approve", "decline"], "approve"), approving=True, legacy=False) == (
        "approve",
        "Approve",
    )
    # A member with no readable ask: legacy groups keep approve, newer groups map to nothing.
    assert groups.member_answer(None, approving=True, legacy=True) == ("approve", "Approve")
    assert groups.member_answer(None, approving=True, legacy=False) is None


class _Rows:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def scalars(self) -> _Rows:
        return self

    def all(self) -> list[object]:
        return self._rows


class _Db:
    """The member rows on the first read, no closed decision on the next ones."""

    def __init__(self, rows: list[object]) -> None:
        self.results = [rows]

    async def execute(self, *_a: object, **_k: object) -> _Rows:
        return _Rows(self.results.pop(0) if self.results else [])


def _member_item(ask: object) -> object:
    from types import SimpleNamespace

    return SimpleNamespace(
        id=uuid.uuid4(),
        ask=ask,
        payload={},
        description="",
        status="blocked",
        blocked_on=["g"],
        blocked_reason="Grouped into g",
        claimed_by_agent_id=None,
        claimed_by_run_id=None,
        claimed_at=None,
        needs_human=None,
        waits_for_user_id=None,
        waits_for_group_ids=None,
        last_activity_at=None,
    )


def _group(items: list[object], recommended: list[str], option_keys: list[str] | None) -> object:
    from types import SimpleNamespace

    members = [
        {"item_id": str(i.id), "case_id": str(uuid.uuid4()), "title": f"case {n}", "recommended": r}  # type: ignore[attr-defined]
        for n, (i, r) in enumerate(zip(items, recommended, strict=True))
    ]
    spec: dict[str, object] = {"kind": groups.FIRST_MEASURE, "recipient": "user-1", "members": members}
    if option_keys is not None:
        spec["option_keys"] = option_keys
    return SimpleNamespace(
        id=uuid.uuid4(), payload={"group": spec}, waits_for_user_id=uuid.uuid4(), waits_for_group_ids=None
    )


async def test_approve_all_reopens_each_member_with_its_own_recommended_option() -> None:
    first = _member_item(_gate_ask(AGENT_SET, "install_agent"))
    second = _member_item(_gate_ask(AGENT_SET, "connect_scanner"))
    group = _group([first, second], ["install_agent", "connect_scanner"], sorted(AGENT_SET))
    answered, waiting = await groups.fan_out(
        _Db([first, second]),  # type: ignore[arg-type]
        group,
        option=groups.APPROVE_ALL,
        answer={"option": groups.APPROVE_ALL},
        note="ANSWERED",
    )
    assert set(answered) == {first.id, second.id} and waiting is False  # type: ignore[attr-defined]
    assert first.payload["answer"]["option"] == "install_agent"  # type: ignore[attr-defined]
    assert second.payload["answer"]["option"] == "connect_scanner"  # type: ignore[attr-defined]
    for item in (first, second):
        offered = {o["key"] for o in item.ask["options"]}  # type: ignore[attr-defined]
        assert item.payload["answer"]["option"] in offered  # type: ignore[attr-defined]
        assert item.status == "open"  # type: ignore[attr-defined]


async def test_a_member_with_no_option_for_the_answer_gets_its_own_card_back() -> None:
    mapped = _member_item(_gate_ask(SCANNER_SET, "connect_scanner"))
    unmapped = _member_item(None)
    group = _group([mapped, unmapped], ["connect_scanner", ""], sorted(SCANNER_SET))
    answered, _ = await groups.fan_out(
        _Db([mapped, unmapped]),  # type: ignore[arg-type]
        group,
        option=groups.DECLINE_ALL,
        answer={"option": groups.DECLINE_ALL},
        note="ANSWERED",
    )
    assert answered == [mapped.id]  # type: ignore[attr-defined]
    assert mapped.payload["answer"]["option"] == "do_not_run"  # type: ignore[attr-defined]
    assert "answer" not in unmapped.payload  # type: ignore[attr-defined]
    assert unmapped.needs_human is True and unmapped.blocked_on is None  # type: ignore[attr-defined]
    assert unmapped.waits_for_user_id == group.waits_for_user_id  # type: ignore[attr-defined]


async def test_decline_all_is_refused_when_the_cases_offer_no_decline() -> None:
    import pytest

    item = _member_item(_gate_ask(AGENT_SET, "install_agent"))
    group = _group([item], ["install_agent"], sorted(AGENT_SET))
    with pytest.raises(ValueError, match="offer no decline"):
        await groups.fan_out(
            _Db([item]),  # type: ignore[arg-type]
            group,
            option=groups.DECLINE_ALL,
            answer={},
            note="x",
        )


async def test_a_legacy_binary_group_still_answers_approve() -> None:
    item = _member_item(None)
    group = _group([item], [""], None)
    answered, _ = await groups.fan_out(
        _Db([item]),  # type: ignore[arg-type]
        group,
        option=groups.APPROVE_ALL,
        answer={},
        note="x",
    )
    assert answered == [item.id]  # type: ignore[attr-defined]
    assert item.payload["answer"]["option"] == groups.CASE_APPROVE  # type: ignore[attr-defined]
