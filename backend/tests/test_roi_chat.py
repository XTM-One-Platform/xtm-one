"""ROI of chat work: finished deliverables counted automatically, on the low side (#10544)."""

from datetime import UTC, datetime, timedelta

import pytest

from app.services import roi, roi_chat

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


def test_every_kind_is_valued_with_a_seeded_unit_of_known_typical_size():
    for kind in roi_chat.KINDS:
        unit = roi_chat.UNIT_BY_KIND[kind]
        assert unit in roi.BASELINES_BY_UNIT
        assert roi_chat.TYPICAL_SIZE[unit] > 0
        assert roi_chat.SIZE_LABEL[kind]
        assert roi_chat.CAP_MIN[kind] > 0
    assert roi_chat.SERIES_LABEL == "From chat, estimated"


def test_one_record_per_deliverable_whatever_the_rounds():
    keys = {roi_chat.record_key("presentation", "deck-1") for _ in range(5)}
    assert keys == {"presentation:deck-1"}
    assert roi_chat.record_key("presentation", "deck-1") != roi_chat.record_key("report", "deck-1")


@pytest.mark.parametrize(("kind", "deliverable_id"), [("message", "m-1"), ("email", "e-1"), ("presentation", " ")])
def test_a_message_an_email_or_a_deliverable_without_id_is_never_a_record(kind, deliverable_id):
    with pytest.raises(ValueError):
        roi_chat.record_key(kind, deliverable_id)


@pytest.mark.parametrize("signal", roi_chat.USE_SIGNALS)
def test_any_use_signal_counts_at_once(signal):
    assert roi_chat.is_counted([signal], last_edit_at=T0, now=T0 + timedelta(minutes=1))


def test_without_a_signal_it_counts_only_once_settled():
    assert not roi_chat.is_counted([], last_edit_at=T0, now=T0 + timedelta(days=6, hours=23))
    assert roi_chat.is_counted([], last_edit_at=T0, now=T0 + timedelta(days=7))
    assert not roi_chat.is_counted(["opened"], last_edit_at=T0, now=T0 + timedelta(days=1))


def test_deleted_before_use_and_before_settling_counts_nothing():
    deleted = T0 + timedelta(days=2)
    assert not roi_chat.is_counted([], last_edit_at=T0, now=T0 + timedelta(days=30), deleted_at=deleted)
    # Used, then deleted: the work was used, the record stays.
    assert roi_chat.is_counted(["exported"], last_edit_at=T0, now=T0 + timedelta(days=30), deleted_at=deleted)
    # Deleted after it settled: it had counted.
    late = T0 + timedelta(days=8)
    assert roi_chat.is_counted([], last_edit_at=T0, now=T0 + timedelta(days=30), deleted_at=late)


def test_measured_time_sums_the_gaps_each_capped():
    times = [T0, T0 + timedelta(minutes=4), T0 + timedelta(minutes=9), T0 + timedelta(hours=3)]
    # 4 + 5 + min(171, 10)
    assert roi_chat.measured_user_minutes(times) == pytest.approx(19)
    assert roi_chat.measured_user_minutes(reversed(times)) == pytest.approx(19)
    assert roi_chat.measured_user_minutes([T0]) == 0
    assert roi_chat.measured_user_minutes([]) == 0


def test_the_low_bound_scales_with_the_size():
    low = roi.BASELINES_BY_UNIT["executive_deck_built"].manual_min_low
    ten = roi_chat.estimate("presentation", 10, user_min=0)
    twenty = roi_chat.estimate("presentation", 20, user_min=0)
    assert ten.manual_min_low == pytest.approx(low)
    assert twenty.manual_min_low == pytest.approx(2 * low)
    # The low bound, never the likely one.
    assert ten.counted_min < roi.BASELINES_BY_UNIT["executive_deck_built"].manual_min_likely


def test_the_person_time_plus_margin_is_subtracted():
    e = roi_chat.estimate("report", 10, user_min=60)
    assert e.subtracted_min == pytest.approx(75)
    assert e.counted_min == pytest.approx(e.manual_min_low - 75)


def test_the_all_hands_deck_counts_about_16_hours():
    """The worked example of the decision: ~20 h low by hand, 3 h measured, 3.75 h subtracted, ~16 h counted."""
    e = roi_chat.estimate("presentation", 30, user_min=180)
    assert e.manual_min_low == pytest.approx(20 * 60)
    assert e.subtracted_min == pytest.approx(3.75 * 60)
    assert e.counted_min / 60 == pytest.approx(16.25)


def test_a_deliverable_is_capped():
    e = roi_chat.estimate("presentation", 200, user_min=0)
    assert e.counted_min == roi_chat.CAP_MIN["presentation"] == 24 * 60


def test_a_deliverable_never_counts_negative():
    e = roi_chat.estimate("file", 1, user_min=600)
    assert e.counted_min == 0
    assert roi_chat.estimate("video", -3, user_min=-5).counted_min == 0


def test_an_organization_override_of_the_unit_is_used():
    base = roi.BASELINES_BY_UNIT["video_produced"]
    lower = roi.Baseline(base.unit, base.name, base.fleet, base.role, 60, 120, 240, base.review_min)
    e = roi_chat.estimate("video", 2, user_min=0, baselines={"video_produced": lower})
    assert e.counted_min == pytest.approx(60)
