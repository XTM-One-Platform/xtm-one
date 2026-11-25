"""The health_modules tool group (#101 S14a-2b, dev-docs/platform-health.md section 12):
what it registers, who may read and who may write, before any database read."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from app.services.builtin_tools import health_modules as tools
from app.services.builtin_tools.health_quality import RefusalError


def _reader(*, role: str | None = None, admin: bool = False, cycle_id: uuid.UUID | None = None):
    from app.services.health.access import Reader

    principal = SimpleNamespace(role=role) if role else None
    user = SimpleNamespace(is_admin=admin) if admin else None
    return Reader(user=user, principal=principal, scope=SimpleNamespace(opted_out_user_ids=()), cycle_id=cycle_id)


def test_the_group_offers_one_read_and_two_writes() -> None:
    definitions, _executor = tools.build_health_module_tools("")
    assert [d.name for d in definitions] == [
        "list_module_findings",
        "record_finding_recommendation",
        "record_module_check_report",
    ]
    assert all(d.source == "builtin:health_specialists" for d in definitions)
    read_only, read_executor = tools.build_health_module_tools("", read_only=True)
    assert {d.name for d in read_only} == tools.MODULES_READ_TOOLS == {"list_module_findings"}
    assert "record_finding_recommendation" not in read_executor._handlers
    assert "record_module_check_report" not in read_executor._handlers


@pytest.mark.parametrize("role", [None, "quality_analyst", "orchestrator"])
async def test_only_a_module_checker_records_a_check_report(role: str | None) -> None:
    from app.services.health.access import WRITE_REFUSED

    answer = await tools._record_module_check_report(
        None,  # type: ignore[arg-type]
        _reader(role=role, admin=True),
        checks=[{"id": "jobs_state", "verdict": "pass"}],
    )
    assert answer == WRITE_REFUSED


@pytest.mark.parametrize("role", ["opencti_checker", "openaev_checker", "opencrq_checker"])
async def test_a_check_report_outside_a_cycle_is_refused(role: str) -> None:
    with pytest.raises(RefusalError) as refused:
        await tools._record_module_check_report(
            None,  # type: ignore[arg-type]
            _reader(role=role),
            checks=[{"id": "jobs_state", "verdict": "pass"}],
        )
    assert refused.value.message == tools._NOT_IN_CYCLE_REPORT


async def test_a_malformed_check_report_is_refused_before_any_read() -> None:
    with pytest.raises(RefusalError):
        await tools._record_module_check_report(
            None,  # type: ignore[arg-type]
            _reader(role="opencrq_checker", cycle_id=uuid.uuid4()),
            checks=[{"id": "jobs_state", "verdict": "fail"}],
        )


def test_report_start_time_needs_a_zone() -> None:
    assert tools._parse_time("") is None
    assert tools._parse_time("yesterday") is None
    assert tools._parse_time("2026-11-22T10:00:00") is None
    assert tools._parse_time("2026-11-22T10:00:00+00:00") is not None


def test_no_tool_of_the_group_reaches_a_module():
    """Section 12.3: the module reads are the allow-listed integration tools, never these."""
    from app.services.health.module_analyst import MODULE_CHECKER_TOOLS

    listed = {name for names in MODULE_CHECKER_TOOLS.values() for name in names}
    assert listed.isdisjoint(d.name for d in tools.build_health_module_tools("")[0])


def test_each_module_checker_carries_the_group() -> None:
    from app.services.health.fleet_seed import FLEET_AGENTS
    from app.services.health.identity import CHECKER_ROLE_BY_PROVIDER

    checkers = [a for a in FLEET_AGENTS if a.role in CHECKER_ROLE_BY_PROVIDER.values()]
    assert sorted(a.role for a in checkers) == sorted(CHECKER_ROLE_BY_PROVIDER.values())
    for checker in checkers:
        assert "health_specialists" in checker.builtin_tool_ids
        assert checker.disable_chat
    # The module functions are scoped to the checkers alone (#1943: one internal group).
    writes = {"record_finding_recommendation", "record_module_check_report"}
    assert [a.slug for a in FLEET_AGENTS if writes & set(a.specialist_functions)] == [a.slug for a in checkers]


def test_the_group_is_wired_in_xtm_one_mode_only(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(type(settings), "is_xtm_one", property(lambda _self: False))
    assert tools.module_tools_offered() is False
    monkeypatch.setattr(type(settings), "is_xtm_one", property(lambda _self: True))
    assert tools.module_tools_offered() is True


@pytest.mark.parametrize("role", [None, "quality_analyst", "orchestrator"])
async def test_only_a_module_checker_records_a_recommendation(role: str | None) -> None:
    answer = await tools._record_finding_recommendation(
        None, _reader(role=role, admin=True), finding_id="f", text="t", confidence="high"
    )
    from app.services.health.access import WRITE_REFUSED

    assert answer == WRITE_REFUSED


async def test_a_recommendation_outside_a_cycle_is_refused():
    with pytest.raises(RefusalError) as refused:
        await tools._record_finding_recommendation(
            None, _reader(role="module_analyst"), finding_id="f", text="t", confidence="high"
        )
    assert refused.value.message == tools._NOT_IN_CYCLE


@pytest.mark.parametrize("role", ["opencti_checker", "openaev_checker", "opencrq_checker", "module_analyst"])
async def test_every_checker_role_reaches_the_cycle_check(role: str) -> None:
    with pytest.raises(RefusalError) as refused:
        await tools._record_finding_recommendation(
            None,  # type: ignore[arg-type]
            _reader(role=role),
            finding_id="f",
            text="t",
            confidence="high",
        )
    assert refused.value.message == tools._NOT_IN_CYCLE


@pytest.mark.parametrize(
    ("text", "confidence"),
    [("", "high"), ("x" * 1501, "high"), ("cause", "certain")],
)
async def test_text_and_confidence_are_bounded(text, confidence):
    with pytest.raises(RefusalError):
        await tools._record_finding_recommendation(
            None,
            _reader(role="module_analyst", cycle_id=uuid.uuid4()),
            finding_id="f",
            text=text,
            confidence=confidence,
        )


@pytest.mark.parametrize(
    "evidence",
    [{"tool": "x"}, [{"values": {}}], [{"tool": "x"}] * (tools.MAX_EVIDENCE_ENTRIES + 1)],
)
def test_evidence_names_its_tool_and_is_bounded(evidence):
    with pytest.raises(RefusalError):
        tools._clean_evidence(evidence)


def test_evidence_is_trimmed_to_its_fields():
    cleaned = tools._clean_evidence(
        [{"tool": " list_opencrq_jobs ", "object_ids": [1, "a"], "values": {"failed": 3}, "extra": "dropped"}]
    )
    assert cleaned == [
        {"tool": "list_opencrq_jobs", "object_ids": ["1", "a"], "values": {"failed": 3}, "read_at": None}
    ]
    assert json.dumps(cleaned)


@pytest.mark.parametrize(("role", "admin"), [(None, False), ("quality_analyst", False)])
async def test_a_non_admin_never_reads_module_findings(role, admin):
    with pytest.raises(RefusalError) as refused:
        await tools._list_module_findings(None, _reader(role=role, admin=admin), registration_id=str(uuid.uuid4()))
    assert refused.value.message == tools._NOT_ALLOWED


def test_findings_are_ordered_by_severity_weight_not_by_the_string() -> None:
    """critical, warning, info: an alphabetical ORDER BY severity would put info before warning."""
    from sqlalchemy.dialects import postgresql

    from app.services.health.engine import SEVERITY_WEIGHTS

    query = tools._open_findings_query("opencti", uuid.uuid4(), 10)
    pg = postgresql.dialect()  # type: ignore[no-untyped-call]
    sql = str(query.compile(dialect=pg, compile_kwargs={"literal_binds": True}))
    order_by = sql.split("ORDER BY", 1)[1]
    assert order_by.lstrip().startswith("CASE")
    assert "END DESC" in order_by
    assert "WHEN 'critical' THEN 25" in order_by
    assert SEVERITY_WEIGHTS["critical"] > SEVERITY_WEIGHTS["warning"] > SEVERITY_WEIGHTS["info"]
    assert "LIMIT 10" in sql


def test_readers_allowed_to_list():
    assert tools._may_read(_reader(role="module_analyst"))
    assert tools._may_read(_reader(role="openaev_checker"))
    assert tools._may_read(_reader(admin=True))
    assert not tools._may_read(_reader(role="flow_lab"))
