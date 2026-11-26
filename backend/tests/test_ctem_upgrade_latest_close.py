"""Upgrade cases close on known data only (#2047).

An issue whose known fixed version is at or below every version the asset
inventory shows running leaves the case's active issues; a case whose every
issue is settled that way resolves; a fixed version that is not known keeps
the case open, to validate; a software signal with nothing to install opens no
``@latest`` case; an issue whose source names no host lists the assets running
the product.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services import ctem_case_loop as loop
from app.services import ctem_case_versions as versions
from app.services import ctem_cases as cc
from app.services import ctem_cases_store as store

NOW = datetime(2026, 11, 25, 8, 0, tzinfo=UTC)
LATEST = "upgrade:meta/whatsapp@latest"


def _cve(ext: str, fixed: str | None, *, kev: bool = False, cvss: float | None = None) -> cc.SourceIssue:
    return cc.SourceIssue(
        source="opencti",
        external_id=ext,
        name=f"CVE-2019-{ext}",
        cve=f"CVE-2019-{ext}",
        product="whatsapp",
        vendor="Meta",
        fixed_version=fixed,
        kev=kev,
        cvss=cvss,
    )


def _case(members: dict[str, bool], running: tuple[str, ...] | None, **kw) -> cc.CaseSnapshot:
    return cc.CaseSnapshot(id="c1", keys=[LATEST], state="new", members=members, running_versions=running, **kw)


# --- Per issue --------------------------------------------------------------


@pytest.mark.parametrize(
    "fixed,running,expected",
    [
        ("2.19.0", ("25.1.0", "26.3.2"), True),
        ("25.1.0", ("25.1.0", "26.3.2"), True),
        ("26.0.0", ("25.1.0", "26.3.2"), False),
        (None, ("25.1.0",), False),
        ("2.19.0", None, False),
        ("2.19.0", ("25.1.0", versions.UNKNOWN), False),
    ],
)
def test_an_issue_is_settled_only_on_a_known_fix_below_every_known_running_version(fixed, running, expected):
    assert cc.fixed_below_running(_cve("1", fixed), running) is expected


def test_every_running_version_counts_an_asset_without_a_version_as_unknown() -> None:
    answer = {"devices": {"a": {"versions": ["25.1.0"]}, "b": {"versions": []}}}
    assert versions.every_running_version(answer) == ("25.1.0", versions.UNKNOWN)
    assert versions.every_running_version({"devices": {}}) is None
    assert versions.every_running_version(None) is None


# --- The case ---------------------------------------------------------------


def test_a_latest_case_whose_cves_are_all_fixed_below_every_running_version_resolves_with_the_reason() -> None:
    case = _case({"opencti:1": True, "opencti:2": True}, ("25.1.0", "26.3.2"))
    (plan,) = cc.plan_recompute([case], [_cve("1", "2.19.0"), _cve("2", "2.21.3")])
    assert plan.resolve == ["opencti:1", "opencti:2"] and plan.settled == ["opencti:1", "opencti:2"]
    assert plan.new_state == "resolved"
    assert plan.evidence == cc.ALL_FIXED_BELOW_RUNNING


def test_one_cve_with_an_unknown_fixed_version_keeps_the_case_open_to_validate() -> None:
    case = _case({"opencti:1": True, "opencti:2": True}, ("25.1.0", "26.3.2"))
    (plan,) = cc.plan_recompute([case], [_cve("1", "2.19.0"), _cve("2", None)])
    assert plan.resolve == ["opencti:1"]
    assert plan.new_state is None


def test_an_unknown_fixed_version_also_holds_back_the_inventory_resolution() -> None:
    case = _case({"opencti:1": True, "opencti:2": True}, ("25.1.0",), fixed_in_inventory=True)
    (plan,) = cc.plan_recompute([case], [_cve("1", "2.19.0"), _cve("2", None)])
    assert plan.new_state is None


def test_no_running_version_settles_nothing() -> None:
    case = _case({"opencti:1": True}, None)
    (plan,) = cc.plan_recompute([case], [_cve("1", "2.19.0")])
    assert plan.resolve == [] and plan.new_state is None


def test_a_new_cve_fixed_below_every_running_version_opens_no_second_case() -> None:
    case = _case({"opencti:2": True}, ("25.1.0",))
    plans = cc.plan_recompute([case], [_cve("1", "2.19.0"), _cve("2", "26.0.0")])
    assert [p.case_id for p in plans] == ["c1"]
    (plan,) = plans
    assert "opencti:1" not in plan.add and plan.resolve == [] and plan.new_state is None


def test_a_new_cve_of_another_product_still_opens_its_case() -> None:
    case = _case({"opencti:2": True}, ("25.1.0",))
    other = cc.SourceIssue(
        source="opencti", external_id="9", name="CVE-9", cve="CVE-9", product="nginx", vendor="F5", fixed_version="1.0"
    )
    plans = cc.plan_recompute([case], [other])
    assert [p.case_id for p in plans].count(None) == 1


# --- Priority recomputes from what still applies ----------------------------


class _Rows(list):
    def all(self):
        return list(self)


def test_the_priority_recomputes_from_the_cves_left_and_may_relax() -> None:
    case = SimpleNamespace(state="new", severity="P1", sla_due_at=NOW + timedelta(hours=24))
    left = [_cve("2", "26.0.0", cvss=7.5)]
    assert store._rerank(case, left, NOW) is True
    assert case.severity == "P2" and case.sla_due_at == NOW + timedelta(hours=72)


def test_the_priority_is_kept_when_nothing_is_left_to_rank() -> None:
    case = SimpleNamespace(state="new", severity="P1", sla_due_at=NOW)
    assert store._rerank(case, [], NOW) is False and case.severity == "P1"


def test_the_mixed_case_drops_the_kev_cve_fixed_below_and_ranks_on_the_rest() -> None:
    kev_old = _cve("1", "2.19.0", kev=True)
    current = _cve("2", "26.0.0", cvss=5.0)
    case = _case({"opencti:1": True, "opencti:2": True}, ("25.1.0",))
    (plan,) = cc.plan_recompute([case], [kev_old, current])
    assert plan.resolve == ["opencti:1"] and plan.new_state is None
    assert cc.default_priority([kev_old, current]) == "critical"
    assert cc.default_priority([current]) == "medium"


# --- No @latest case for a vendor fix made service-side ---------------------


def _software_signal(**evidence) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        trigger_path="new_vulnerability",
        lens="inside",
        subject_kind="software",
        subject_ref="WhatsApp",
        subject_label="WhatsApp",
        state="confirmed",
        case_id=None,
        evidence=evidence,
        decision={"outcome": "case", "state": "confirmed", "severity": "P2", "sla_hours": 72},
    )


def test_a_service_side_vendor_fix_opens_no_latest_case() -> None:
    signal = _software_signal(summary="fixed by the vendor on its servers")
    fix = loop.fix_of(signal)
    assert fix.key.endswith("@latest")
    assert loop.nothing_to_upgrade(signal, fix) is True


@pytest.mark.parametrize(
    "evidence", [{"cve": "CVE-2026-1"}, {"vulnerable_range": "< 2.3"}, {"affected_versions": ["2.1"]}]
)
def test_a_software_signal_with_a_cve_or_a_range_keeps_its_upgrade_case(evidence):
    signal = _software_signal(**evidence)
    assert loop.nothing_to_upgrade(signal, loop.fix_of(signal)) is False


@pytest.mark.asyncio
async def test_open_from_signal_parks_the_service_side_fix_to_validate() -> None:
    signal = _software_signal()

    class _Db:
        def add(self, _row):
            raise AssertionError("no case row is written")

    assert await loop.open_from_signal(_Db(), signal, now=NOW) is None
    assert signal.state == "to_validate"
    assert signal.decision["outcome"] == "review" and signal.decision["reason"] == loop.NOTHING_TO_UPGRADE


# --- Per-issue assets on an upgrade case ------------------------------------


def test_issues_without_assets_list_the_assets_running_the_product() -> None:
    issues = [
        {"cve": "CVE-2019-1", "assets": [], "open": True},
        {"cve": "CVE-2019-2", "assets": ["mac-07"], "open": True},
        {"restricted": True},
    ]
    running = [f"mac-{n:02d}" for n in range(24)]
    assert versions.attribute_assets(issues, running) == 1
    assert len(issues[0]["assets"]) == 24 and issues[0]["assets_from"] == versions.ASSETS_FROM_INVENTORY
    assert issues[1]["assets"] == ["mac-07"] and "assets_from" not in issues[1]
    assert "assets" not in issues[2]
    answer = versions.affected_assets(issues)
    assert answer["asset_count"] == 24


def test_no_running_asset_leaves_the_issues_as_they_are() -> None:
    issues = [{"cve": "CVE-2019-1", "assets": [], "open": True}]
    assert versions.attribute_assets(issues, []) == 0 and issues[0]["assets"] == []


@pytest.mark.asyncio
async def test_running_asset_names_reads_the_inventory_of_an_upgrade_case(monkeypatch):
    async def rows(_db, _org, _keys):
        return [("mac-02", "Meta", "WhatsApp"), ("mac-01", "Meta", "WhatsApp"), ("srv", "Other", "Thing")]

    monkeypatch.setattr(versions, "_read_running_names", rows)
    monkeypatch.setattr(versions, "matches_case", lambda _label, vendor, _product: vendor == "Meta")

    class _Db:
        def begin_nested(self):
            class _S:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *exc):
                    return False

            return _S()

    case = SimpleNamespace(remediation_kind="upgrade", product_label="Meta WhatsApp", organization_id=uuid.uuid4())
    assert await versions.running_asset_names(_Db(), case) == ["mac-01", "mac-02"]
    other = SimpleNamespace(remediation_kind="close_exposure", product_label="x", organization_id=uuid.uuid4())
    assert await versions.running_asset_names(_Db(), other) == []
