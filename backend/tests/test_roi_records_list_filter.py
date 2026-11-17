"""The ROI records list counts what the report counts (issue #10570).

The review-rate card of a report links to ``GET /roi/records``: the list and
its count take one filter (:func:`roi_records._records_query`) that narrows by
agent, fleet, outcome and the report window, and every record says whether it
is a run or a work item, so a count that includes work items is never shown
behind a runs-only list.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.api.v1 import roi as roi_api
from app.models.roi import RoiRunRecord
from app.services import roi, roi_records


def _sql(query) -> str:
    return str(query.compile(dialect=postgresql.dialect()))


def test_the_filter_narrows_by_agent_window_and_unreviewed_outcome():
    since = datetime(2026, 10, 11, tzinfo=UTC)
    query = roi_records._records_query(
        select(RoiRunRecord), agent_id=uuid.uuid4(), fleet_id=None, outcome=roi.OUTCOME_UNREVIEWED, since=since
    )
    sql = _sql(query)
    assert "roi_run_records.agent_id =" in sql
    assert "roi_run_records.created_at >=" in sql
    assert "roi_run_records.outcome IS NULL" in sql
    # Runs and work items alike: the list never narrows to one kind.
    assert "run_id" not in sql.split("WHERE", 1)[1]
    assert "work_item_id" not in sql.split("WHERE", 1)[1]


def test_without_a_window_every_record_is_kept():
    sql = _sql(
        roi_records._records_query(select(RoiRunRecord), agent_id=None, fleet_id=uuid.uuid4(), outcome=None, since=None)
    )
    assert "created_at >=" not in sql
    assert "roi_run_records.fleet_id =" in sql


def test_the_list_and_its_count_share_the_one_filter():
    for fn in (roi_records.list_records, roi_records.count_records):
        assert "_records_query(" in inspect.getsource(fn)


def test_the_search_narrows_by_title_for_the_list_and_its_count():
    query = roi_records._records_query(
        select(RoiRunRecord), agent_id=uuid.uuid4(), fleet_id=None, outcome=None, since=None, search="50%_off"
    )
    sql = _sql(query)
    assert "ILIKE" in sql.upper()
    assert "work_items.title" in sql and "assignments.name" in sql
    params = query.compile(dialect=postgresql.dialect()).params
    assert "%50\\%\\_off%" in params.values()
    # A blank search is no filter.
    blank = _sql(
        roi_records._records_query(
            select(RoiRunRecord), agent_id=None, fleet_id=None, outcome=None, since=None, search="  "
        )
    )
    assert "ILIKE" not in blank.upper()
    for fn in (roi_api.list_roi_records, roi_api.count_roi_records):
        assert "search" in inspect.signature(fn).parameters
        assert "search=search" in inspect.getsource(fn)


def test_the_list_sorts_by_date_both_ways():
    params = inspect.signature(roi_api.list_roi_records).parameters
    assert "direction" in params
    assert "direction=direction" in inspect.getsource(roi_api.list_roi_records)
    source = inspect.getsource(roi_records.list_records)
    assert "created_at.asc()" in source and "created_at.desc()" in source


def test_a_record_says_its_kind():
    base = {
        "id": uuid.uuid4(),
        "run_id": None,
        "work_item_id": None,
        "agent_id": None,
        "fleet_id": None,
        "unit": "alert_triaged",
        "units": 1,
        "llm_cost": 0.0,
        "duration_s": 0.0,
        "outcome": None,
        "review_min": None,
        "reviewed_by_user_id": None,
        "reviewed_at": None,
        "review_note": None,
        "created_at": None,
    }
    assert roi_records.record_dict(SimpleNamespace(**{**base, "run_id": uuid.uuid4()}))["kind"] == "run"
    assert roi_records.record_dict(SimpleNamespace(**{**base, "work_item_id": uuid.uuid4()}))["kind"] == "work_item"


def test_the_routes_take_the_report_window_and_an_agent_reader():
    for fn in (roi_api.list_roi_records, roi_api.count_roi_records):
        params = inspect.signature(fn).parameters
        assert "window" in params and "agent_id" in params
        assert "agent_id=agent_id" in inspect.getsource(fn)
