"""The ``roi`` block of the run and work item detail (issue #9206, slice O7, ``dev-docs/roi-outcomes.md`` 6.2).

Pure checks of :func:`roi_records.may_review` (the review route's rule) and
:func:`roi_records.review_view` (what the review row reads); the database
read is covered in ``test_roi_records_postgres.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from app.services import roi, roi_records

OWNER = uuid.uuid4()
OTHER = uuid.uuid4()


def _row(**kw):
    base = {
        "id": uuid.uuid4(),
        "unit": "cve_assessed",
        "units": 3,
        "outcome": None,
        "review_min": None,
        "review_note": None,
        "reviewed_at": None,
        "reviewed_by_user_id": None,
    }
    return SimpleNamespace(**{**base, **kw})


def test_an_administrator_may_always_review():
    row = _row(outcome="accepted", reviewed_by_user_id=OTHER)
    assert roi_records.may_review(row, user_id=OWNER, is_admin=True, subject_owner_id=None, company_managed=True)


def test_the_owner_reviews_an_unreviewed_record_and_its_own_review():
    assert roi_records.may_review(_row(), user_id=OWNER, is_admin=False, subject_owner_id=OWNER)
    own = _row(outcome="modified", reviewed_by_user_id=OWNER)
    assert roi_records.may_review(own, user_id=OWNER, is_admin=False, subject_owner_id=OWNER)


def test_nobody_else_reviews():
    # Another person's run, a company-managed run, another reviewer's review, no user.
    assert not roi_records.may_review(_row(), user_id=OTHER, is_admin=False, subject_owner_id=OWNER)
    assert not roi_records.may_review(
        _row(), user_id=OWNER, is_admin=False, subject_owner_id=OWNER, company_managed=True
    )
    reviewed = _row(outcome="rejected", reviewed_by_user_id=OTHER)
    assert not roi_records.may_review(reviewed, user_id=OWNER, is_admin=False, subject_owner_id=OWNER)
    assert not roi_records.may_review(_row(), user_id=None, is_admin=False, subject_owner_id=None)


def test_the_view_carries_what_the_row_needs_and_no_figure():
    at = datetime(2026, 11, 6, 18, 0, tzinfo=UTC)
    row = _row(outcome="modified", review_min=4.0, review_note="fixed two CVSS", reviewed_at=at)
    baseline = roi.BASELINES_BY_UNIT["cve_assessed"]
    view = roi_records.review_view(row, baseline=baseline, can_review=True)
    assert view == {
        "record_id": str(row.id),
        "unit": "cve_assessed",
        "unit_name": baseline.name,
        "units": 3,
        "outcome": "modified",
        "review_min": 4.0,
        "default_review_min": baseline.review_min,
        "review_note": "fixed two CVSS",
        "reviewed_at": at.isoformat(),
        "can_review": True,
    }
    # No hours, value or cost: the figures stay on the ROI views (6.2, B3).
    assert not {"hours_saved", "value", "llm_cost", "net_value"} & set(view)


def test_an_unknown_unit_falls_back_to_its_key():
    view = roi_records.review_view(_row(unit="gone_unit"), baseline=None, can_review=False)
    assert (view["unit_name"], view["default_review_min"], view["outcome"]) == ("gone_unit", None, None)


def test_the_run_detail_declares_the_block():
    from app.api.v1.schemas.assignment import AssignmentRunDetailResponse

    field = AssignmentRunDetailResponse.model_fields["roi"]
    assert field.default is None
