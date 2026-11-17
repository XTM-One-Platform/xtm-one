"""The "From chat, estimated" series (#10544, slice S4): who reads a line, the correction, the figures."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import roi, roi_chat, roi_chat_series
from app.services.builtin_tools import roi as roi_tools

_NOW = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)


def _row(**over: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "kind": roi_chat.KIND_PRESENTATION,
        "deliverable_id": str(uuid.uuid4()),
        "title": "All Hands",
        "user_id": uuid.uuid4(),
        "agent_id": uuid.uuid4(),
        "conversation_id": uuid.uuid4(),
        "size": 30.0,
        "signals": [roi_chat.SIGNAL_EXPORTED],
        "last_edit_at": _NOW - timedelta(days=1),
        "deleted_at": None,
        "backfilled_at": None,
        "adjusted_min": None,
        "adjusted_by_user_id": None,
        "adjusted_at": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _rate(amount: float = 50.0) -> roi.HourlyRate:
    return roi.HourlyRate("knowledge_worker", "USD", amount, "test", "test")


def test_an_administrator_reads_every_line():
    reader = roi_chat_series.Reader(user_id=uuid.uuid4(), organization=True)
    assert roi_chat_series.readable(reader, agent_id=None, user_id=None)


def test_a_person_reads_only_their_own_lines_whatever_the_agent():
    """Sam, 15 Nov 2026: seeing an agent (shared, company-managed) never opens a colleague's line."""
    me, seen = uuid.uuid4(), uuid.uuid4()
    reader = roi_chat_series.Reader(user_id=me)
    assert roi_chat_series.readable(reader, agent_id=uuid.uuid4(), user_id=me)
    assert roi_chat_series.readable(reader, agent_id=None, user_id=me)
    assert not roi_chat_series.readable(reader, agent_id=seen, user_id=uuid.uuid4())
    assert not roi_chat_series.readable(reader, agent_id=uuid.uuid4(), user_id=uuid.uuid4())
    assert not roi_chat_series.readable(reader, agent_id=None, user_id=None)
    assert not roi_chat_series.readable(roi_chat_series.Reader(user_id=None), agent_id=None, user_id=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("is_admin", [True, False])
async def test_the_reader_needs_no_agent_scope(is_admin: bool):
    me = uuid.uuid4()
    reader = await roi_chat_series.reader_for(None, user_id=me, is_admin=is_admin)
    assert reader == roi_chat_series.Reader(user_id=me, organization=is_admin)


# --- The routes: no other person's line, name or title reaches a non-administrator ----------


A_USER, B_USER = uuid.uuid4(), uuid.uuid4()
SHARED_AGENT = uuid.uuid4()


def _b_line() -> dict[str, Any]:
    return {"id": str(uuid.uuid4()), "user_id": str(B_USER), "agent_id": str(SHARED_AGENT), "title": "Acme deck"}


def _patch_series(monkeypatch, seen: dict[str, Any], *, org_visible: bool = True) -> None:
    """``load_series`` answers B's deck to whoever reads the organization, nothing to anyone else."""
    from app.api.v1 import roi as roi_api

    async def _load(_db, *, reader, aggregate_only=False, **kw):
        seen["reader"], seen["aggregate_only"], seen["kw"] = reader, aggregate_only, kw
        opens = roi_chat_series.readable(reader, agent_id=SHARED_AGENT, user_id=B_USER)
        lines = [_b_line()] if opens and not aggregate_only else []
        return {"totals": {"deliverables": 1}, "by_kind": {}, "lines": lines, "aggregate_only": aggregate_only}

    async def _settings(_db):
        return SimpleNamespace(roi_org_totals_visible=org_visible)

    async def _viewer_for(_db, *, user_id, is_admin):
        return SimpleNamespace(user_id=user_id, organization=is_admin)

    async def _chat_figures(_db, *, viewer, mine=False, agent_id=None, fleet_id=None, **_kw):
        # The ledger's chat totals (#12077 L4b): the scope the route asked for, recorded.
        seen["figures_scope"] = "mine" if mine else "agent" if agent_id else "fleet" if fleet_id else "organization"
        return {"scope": seen["figures_scope"], "totals": {"deliverables": 1}}

    from app.services import roi_ledger

    monkeypatch.setattr(roi_chat_series, "load_series", _load)
    monkeypatch.setattr(roi_api, "get_platform_settings", _settings)
    monkeypatch.setattr(roi_ledger, "viewer_for", _viewer_for)
    monkeypatch.setattr(roi_ledger, "chat_figures", _chat_figures)


async def _read(user, **over):
    from app.api.v1 import roi as roi_api

    args: dict[str, Any] = {
        "window": "30d",
        "currency": None,
        "agent_id": None,
        "fleet_id": None,
        "mine": False,
        "limit": 50,
        "offset": 0,
    }
    args.update(over)
    return await roi_api.get_roi_chat_series(db=None, user=user, **args)


@pytest.mark.asyncio
async def test_a_non_administrator_reads_no_colleague_line_on_mine_or_the_organization(monkeypatch):
    """Acceptance 1: A made nothing, B made a deck with a company-managed agent A sees."""
    seen: dict[str, Any] = {}
    _patch_series(monkeypatch, seen)
    a = SimpleNamespace(id=A_USER, is_admin=False)
    mine = await _read(a, mine=True)
    assert mine["lines"] == [] and seen["reader"] == roi_chat_series.Reader(user_id=A_USER)
    org = await _read(a)
    assert org["lines"] == [] and seen["aggregate_only"] is True
    agent = await _read(a, agent_id=SHARED_AGENT)
    assert agent["lines"] == [] and seen["aggregate_only"] is False
    for answer in (mine, org, agent):
        text = repr(answer)
        assert str(B_USER) not in text and "Acme deck" not in text


@pytest.mark.asyncio
async def test_the_organization_chat_totals_follow_the_org_totals_setting(monkeypatch):
    from fastapi import HTTPException

    seen: dict[str, Any] = {}
    _patch_series(monkeypatch, seen, org_visible=False)
    with pytest.raises(HTTPException) as refused:
        await _read(SimpleNamespace(id=A_USER, is_admin=False))
    assert refused.value.status_code == 403 and "reader" not in seen


@pytest.mark.asyncio
async def test_an_administrator_still_reads_every_line(monkeypatch):
    """Acceptance 5: the administrator's read is unchanged."""
    seen: dict[str, Any] = {}
    _patch_series(monkeypatch, seen)
    admin = SimpleNamespace(id=uuid.uuid4(), is_admin=True)
    org = await _read(admin)
    assert [line["user_id"] for line in org["lines"]] == [str(B_USER)] and seen["aggregate_only"] is False
    # "Mine" reads as the person: an administrator who made nothing reads nothing.
    assert (await _read(admin, mine=True))["lines"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("who", "is_admin", "allowed"), [("a", False, False), ("b", False, True), ("admin", True, True)]
)
async def test_only_the_person_or_an_administrator_corrects_a_line(monkeypatch, who, is_admin, allowed):
    """Acceptance 3: a non-administrator's PUT on someone else's chat record answers 404."""
    from fastapi import HTTPException

    from app.api.v1 import roi as roi_api

    row = _row(user_id=B_USER, agent_id=SHARED_AGENT)
    audits: list[dict[str, Any]] = []

    async def _get_line(_db, _rid):
        return row

    async def _audit(_db, **kw):
        audits.append(kw)

    class _Db:
        async def commit(self):
            return None

    monkeypatch.setattr(roi_chat_series, "get_line", _get_line)
    monkeypatch.setattr(roi_api, "log_audit", _audit)
    user = SimpleNamespace(id={"a": A_USER, "b": B_USER}.get(who, uuid.uuid4()), is_admin=is_admin)
    body = roi_api.ChatCorrectionRequest(minutes=60)
    if allowed:
        answer = await roi_api.put_roi_chat_correction(row.id, body, db=_Db(), user=user)
        assert answer["adjusted_min"] == 60.0 and len(audits) == 1
    else:
        with pytest.raises(HTTPException) as refused:
            await roi_api.put_roi_chat_correction(row.id, body, db=_Db(), user=user)
        assert refused.value.status_code == 404
        assert row.adjusted_min is None and audits == []


@pytest.mark.asyncio
async def test_the_agent_tool_reads_the_same_rule(monkeypatch):
    """The builtin roi tools share ``reader_for``: a non-administrator's reader holds no agent scope."""
    import inspect

    source = inspect.getsource(roi_tools.build_roi_tools)
    assert "roi_chat_series.reader_for(" in source and "roi_chat_series.readable(" in source
    assert not hasattr(roi_chat_series.Reader(user_id=A_USER), "agent_ids")


def test_a_correction_is_set_then_dropped():
    row = _row()
    who = uuid.uuid4()
    roi_chat_series.apply_correction(row, minutes=120, user_id=who, now=_NOW)
    assert (row.adjusted_min, row.adjusted_by_user_id, row.adjusted_at) == (120.0, who, _NOW)
    roi_chat_series.apply_correction(row, minutes=None, user_id=who)
    assert (row.adjusted_min, row.adjusted_by_user_id, row.adjusted_at) == (None, None, None)


@pytest.mark.parametrize("bad", [-1, float("nan"), roi_chat_series.CORRECTION_MAX_MIN + 1])
def test_a_correction_out_of_range_is_refused(bad: float):
    row = _row()
    with pytest.raises(ValueError):
        roi_chat_series.apply_correction(row, minutes=bad, user_id=None)
    assert row.adjusted_min is None


def test_a_counted_line_shows_the_low_estimate_and_its_value():
    row = _row()
    line = roi_chat_series.line_dict(row, now=_NOW, user_min=180.0, baselines=roi.BASELINES_BY_UNIT, rate=_rate())
    expected = roi_chat.estimate(roi_chat.KIND_PRESENTATION, 30.0, user_min=180.0)
    assert line["counted"] is True
    assert line["minutes"] == pytest.approx(round(expected.counted_min, 1))
    assert line["estimate"]["subtracted_min"] == pytest.approx(round(180.0 * (1 + roi_chat.MARGIN), 1))
    assert line["value"] == pytest.approx(round(expected.counted_min / 60.0 * 50.0, 2))
    assert line["size_label"] == "slides"


def test_a_correction_replaces_the_estimate_on_a_counted_line():
    row = _row(adjusted_min=90.0)
    line = roi_chat_series.line_dict(row, now=_NOW, user_min=0.0, baselines=roi.BASELINES_BY_UNIT, rate=_rate())
    assert line["minutes"] == 90.0
    assert line["hours"] == 1.5
    assert line["value"] == 75.0


def test_a_pending_line_counts_nothing_even_corrected():
    """No signal and not settled: reported as pending, never valued."""
    row = _row(signals=[], last_edit_at=_NOW - timedelta(days=1), adjusted_min=600.0)
    line = roi_chat_series.line_dict(row, now=_NOW, user_min=0.0, baselines=roi.BASELINES_BY_UNIT, rate=_rate())
    assert line["counted"] is False
    assert line["minutes"] == 0.0
    assert line["value"] == 0.0


def test_a_line_never_goes_below_zero():
    row = _row(size=1.0)
    line = roi_chat_series.line_dict(row, now=_NOW, user_min=10_000.0, baselines=roi.BASELINES_BY_UNIT, rate=_rate())
    assert line["minutes"] == 0.0


def test_the_suite_carries_the_chat_tools_in_the_alias():
    from app.services.builtin_tool_aliases import alias_scoped_functions

    assert {"get_roi_chat_series", "adjust_roi_chat_line"} <= set(roi_tools.ROI_FUNCTIONS)
    assert tuple(alias_scoped_functions("roi")) == roi_tools.ROI_FUNCTIONS
    defs, _ = roi_tools.build_roi_tools(str(uuid.uuid4()))
    by_name = {d.name: d for d in defs}
    assert by_name["adjust_roi_chat_line"].input_schema["required"] == ["record_id"]


def test_the_series_reads_no_automation_record():
    """Fleet totals never move with a chat record: the series module never names roi_run_records."""
    from pathlib import Path

    source = Path(roi_chat_series.__file__).read_text(encoding="utf-8")
    assert "RoiRunRecord" not in source
    assert "roi_run_records" not in source.replace("``roi_run_records``", "")
