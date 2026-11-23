# SPDX-License-Identifier: Apache-2.0

"""The version match a CTEM remediation or mobilize card states, or its blindness (#1472)."""

from __future__ import annotations

import json
import pathlib
import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.services import ctem_version_match as matching
from app.services import work_item

_KNOWN = {
    "versions": [
        {"version": "0.49.1", "asset_count": 2, "status": "below_fixed"},
        {"version": "0.50.3", "asset_count": 1, "status": "fixed"},
    ],
    "asset_count": 3,
    "assets_below_fixed": 2,
    "assets_on_fixed": 1,
    "assets_not_comparable": 0,
}
_ASK = {
    "question": "File the three remediation tickets and notify their owners?",
    "context": "The case is P1.",
    "options": [{"key": "A", "label": "File all three tickets"}, {"key": "B", "label": "Hold"}],
    "recommendation": "A",
}


def test_versions_known_from_the_inventory_and_an_affected_range_is_not_blind() -> None:
    match = matching.version_match(_KNOWN, product_label="Example BI", fixed_version="0.50.3", affected="< 0.50.3")
    assert match["blind"] is False
    assert match["running"] == "0.49.1 on 2 asset(s), 0.50.3 on 1 asset(s)"
    assert (match["affected"], match["fixed"]) == ("< 0.50.3", "0.50.3")
    assert match["assets_below_fixed"] == 2
    lines = matching.card_lines(match)
    assert lines == [
        "Version match: running 0.49.1 on 2 asset(s), 0.50.3 on 1 asset(s); affected < 0.50.3; fixed 0.50.3."
    ]


def test_the_running_line_names_the_platform_and_the_unverified_fix() -> None:
    """#1572: a row with a platform reads 'version (platform)'; a fix not verified per platform is carried."""
    running = {
        "versions": [
            {"version": "36.0", "platform": "Windows", "asset_count": 4},
            {"version": "36.0", "platform": "macOS", "asset_count": 2},
        ],
        "asset_count": 6,
        "assets_below_fixed": 0,
        "assets_on_fixed": 4,
        "assets_not_comparable": 2,
        "platform_unverified": True,
    }
    match = matching.version_match(running, product_label="Acme Browser", fixed_version="36.0", affected="< 36.0")
    assert match["running"] == "36.0 (Windows) on 4 asset(s), 36.0 (macOS) on 2 asset(s)"
    assert match["platform_unverified"] is True
    assert match["assets_not_comparable"] == 2


def test_no_asset_reports_the_product_and_no_affected_range_is_blind() -> None:
    match = matching.version_match(None, product_label="Example BI", fixed_version=None)
    assert match == {
        "running": "unknown: no asset reports Example BI",
        "affected": "unknown: OpenCTI holds no affected versions",
        "fixed": "unknown",
        "blind": True,
    }
    lines = matching.card_lines(match)
    assert lines[1] == "Proposed without version matching: running version unknown, affected range unknown."
    assert lines[2].startswith("Verify the running version, then upgrade")


def test_running_versions_known_but_no_affected_range_is_still_blind() -> None:
    match = matching.version_match(_KNOWN, product_label="Example BI", fixed_version="0.50.3")
    assert match["blind"] is True
    assert matching.blind_reasons(match) == ["affected range unknown"]


def test_assets_with_no_version_reported_count_as_unknown() -> None:
    running = {"versions": [{"version": "unknown", "asset_count": 4}], "asset_count": 4}
    match = matching.version_match(running, product_label="Example BI", fixed_version="2.0")
    assert match["blind"] is True
    assert match["running"] == "unknown: 4 asset(s) run Example BI with no version reported"


def test_the_line_goes_first_in_the_context_and_is_never_added_twice() -> None:
    match = matching.version_match(None, product_label="Example BI", fixed_version=None)
    completed = matching.complete_ask(_ASK, match)
    assert completed["context"].startswith("Version match: running unknown")
    assert completed["context"].endswith("The case is P1.")
    assert matching.complete_ask(completed, match) == completed
    assert _ASK["context"] == "The case is P1."  # the given ask is not changed


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("ctem:remediation:{id}", True),
        ("ctem:f1:case-mobilize:{id}", True),
        ("ctem:f2:mobilize:{id}", True),
        ("ctem:review:{id}", False),
        ("dev:something", False),
        ("ctem:f2:mobilize:not-a-uuid", False),
    ],
)
def test_only_the_remediation_and_mobilize_gate_keys_name_a_case(key: str, expected: bool) -> None:
    case_id = uuid.uuid4()
    assert (matching.case_id_of(key.format(id=case_id)) == case_id) is expected


class _Db:
    def __init__(self, case: Any) -> None:
        self.case = case

    async def get(self, _model: Any, _id: Any) -> Any:
        return self.case


def _case(kind: str = "upgrade") -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), remediation_kind=kind, product_label="Example BI", fixed_version=None)


async def test_the_platform_completes_a_mobilize_card_of_a_blind_upgrade_case(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import ctem_case_versions

    async def _no_inventory(_db: Any, _case: Any) -> None:
        return None

    monkeypatch.setattr(ctem_case_versions, "for_case", _no_inventory)
    case = _case()
    ask = await work_item._complete_case_ask(_Db(case), f"ctem:f1:case-mobilize:{case.id}", _ASK)
    assert ask["context"].splitlines()[:3] == [
        "Version match: running unknown: no asset reports Example BI; "
        "affected unknown: OpenCTI holds no affected versions; fixed unknown.",
        "Proposed without version matching: running version unknown, affected range unknown.",
        matching.BLIND_ACTION,
    ]


async def test_a_card_of_a_case_that_is_not_an_upgrade_or_of_another_key_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case("configuration")
    assert await work_item._complete_case_ask(_Db(case), f"ctem:remediation:{case.id}", _ASK) == _ASK
    assert await work_item._complete_case_ask(_Db(_case()), "dev:other", _ASK) == _ASK
    assert await work_item._complete_case_ask(_Db(_case()), "ctem:remediation:x", None) is None


async def test_an_unreadable_inventory_leaves_the_card_as_given(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import ctem_case_versions

    async def _fails(_db: Any, _case: Any) -> None:
        raise RuntimeError("inventory down")

    monkeypatch.setattr(ctem_case_versions, "for_case", _fails)
    case = _case()
    assert await matching.complete_gate_ask(cast(Any, _Db(case)), f"ctem:remediation:{case.id}", _ASK) == _ASK


def test_the_coordinator_schema_carries_the_version_match() -> None:
    path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "app/agents/seed_data/agent_output_schema/ctem-remediation-coordinator.json"
    )
    action = json.loads(path.read_text(encoding="utf-8"))["properties"]["actions"]["items"]
    field = action["properties"]["version_match"]
    assert field["required"] == ["running", "affected", "fixed", "blind"]
    assert field["properties"]["blind"]["type"] == "boolean"
    assert "version_match" not in action["required"]  # a non-upgrade action carries none
