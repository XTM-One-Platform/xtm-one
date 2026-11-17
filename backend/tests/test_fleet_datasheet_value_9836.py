"""The fleet datasheet's value figures (#9836 R6d, design 10.8), without a database or a browser.

Pinned here: the sheet prints "Hours saved", "Value" and "Net value" with
their ranges and the methodology line, from the same ``roi.RoiFigures`` the
fleet's ROI report computes; masked costs read "Hidden by your administrator",
never 0; a fleet with nothing counted says so; a failed read says so; the
route reads the fleet's own 30-day report, with no model call.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services import fleet_datasheet as sheet


def _figures(**overrides):
    base = {
        "currency": "EUR",
        "runs": 12,
        "units": 30,
        "hours_saved": 7.5,
        "hours_saved_low": 5.0,
        "hours_saved_high": 10.0,
        "value": 600.0,
        "value_low": 400.0,
        "value_high": 800.0,
        "net_value": 1550.4,
        "net_value_low": 350.0,
        "net_value_high": 2750.0,
    }
    return SimpleNamespace(**{**base, **overrides})


def _sheet(value: sheet.ValueFigures | None) -> str:
    return sheet.build_datasheet_html(
        fleet_name="Alert Desk",
        product_name="XTM One",
        sections={},
        structured={},
        version=1,
        status="on",
        automations_on=1,
        automations_total=1,
        nodes=[],
        prerequisites=[],
        generated_on=datetime(2026, 11, 8, tzinfo=UTC),
        value=value,
    )


def test_the_sheet_prints_the_three_figures_with_their_ranges_and_the_methodology():
    page = _sheet(sheet.value_figures(_figures(), cost_masked=False))
    for text in (
        "Value, last 30 days",
        "7.5 h",
        "Hours saved",
        "5.0 h to 10.0 h",
        "600 EUR",
        "400 EUR to 800 EUR",
        "1.6K EUR",
        "Net value",
        "350 EUR to 2.8K EUR",
        "12 runs, 30 units of work.",
        "Net value = value - AI cost.",
    ):
        assert text in page, text


def test_the_figures_are_the_report_figures_unchanged():
    figures = sheet.value_figures(_figures(), cost_masked=False)
    assert figures.hours_saved == 7.5
    assert figures.value_range == (400.0, 800.0)
    assert figures.net_value == 1550.4


def test_masked_costs_read_hidden_never_zero():
    page = _sheet(sheet.value_figures(_figures(net_value=0.0), cost_masked=True))
    assert sheet.COST_HIDDEN in page
    assert "0 EUR</b>Net value" not in page
    assert "350 EUR to 2.8K EUR" not in page, "the net range is a cost figure too"
    assert "600 EUR" in page, "the gross value stays"


def test_nothing_counted_says_so():
    page = _sheet(
        sheet.value_figures(
            _figures(
                runs=0,
                units=0,
                hours_saved=0.0,
                hours_saved_low=0.0,
                hours_saved_high=0.0,
                value=0.0,
                value_low=0.0,
                value_high=0.0,
                net_value=0.0,
                net_value_low=0.0,
                net_value_high=0.0,
            ),
            cost_masked=False,
        )
    )
    assert "No unit of work counted in the last 30 days." in page
    assert "Net value = value - AI cost." in page


def test_a_failed_read_says_so_never_zero():
    page = _sheet(None)
    assert sheet.VALUE_UNAVAILABLE in page
    assert "0 EUR" not in page


def test_singular_counts():
    page = _sheet(sheet.value_figures(_figures(runs=1, units=1), cost_masked=False))
    assert "1 run, 1 unit of work." in page


def test_the_route_reads_the_ledger_figures_of_the_fleet_over_30_days_with_no_model_call():
    """#12077 L4b: the sheet reads ``roi_ledger.figures(scope=fleet)`` for its reader, never ``load_report``."""
    from app.api.v1 import fleets

    helper = inspect.getsource(fleets._datasheet_value)
    assert 'window="30d"' in helper
    assert "scope=roi_figures.SCOPE_FLEET" in helper
    assert "roi_ledger.viewer_for(db, user_id=user.id, is_admin=user.is_admin)" in helper
    assert "load_report" not in helper
    assert "value=value" in inspect.getsource(fleets.export_fleet_datasheet_endpoint)


def _ledger_figures(*, masked: bool, deliverables: int = 0):
    from app.services import roi_figures

    totals = roi_figures.Totals(
        runs=12,
        units=30,
        deliverables=deliverables,
        minutes_low=300.0,
        minutes_likely=450.0,
        minutes_high=600.0,
        value_low=400.0,
        value_likely=600.0,
        value_high=800.0,
        cost=50.0,
    )
    figures = roi_figures.Figures(
        scope=roi_figures.SCOPE_FLEET,
        scope_id=uuid.uuid4(),
        window_first=None,
        window_last=datetime(2026, 11, 8, tzinfo=UTC).date(),
        totals=totals,
        by_source=[],
        by_unit=[],
        by_agent=[],
        by_fleet=[],
        by_week=[],
        by_person=None,
        cost_masked=masked,
        since=None,
        period_days=30,
    )
    figures.currency = "EUR"
    return figures


def test_the_sheet_prints_the_ledger_hero_as_get_roi_answers_it():
    """Invariant (d) on the datasheet: its figures are ``figures.totals`` of ``GET /roi`` unchanged."""
    ledger = _ledger_figures(masked=False, deliverables=2)
    hero = ledger.as_dict()["totals"]
    figures = sheet.ledger_value_figures(ledger)
    assert (figures.runs, figures.units, figures.deliverables) == (hero["runs"], hero["units"], 2)
    assert figures.hours_saved == hero["hours"] == 7.5
    assert figures.hours_saved_range == (hero["hours_low"], hero["hours_high"]) == (5.0, 10.0)
    assert figures.value_range == (hero["value_low"], hero["value_high"]) == (400.0, 800.0)
    assert figures.net_value == hero["net_value"] == 550.0
    page = _sheet(figures)
    assert "12 runs, 30 units of work, 2 chat deliverables." in page
    assert "550 EUR" in page


def test_a_fleet_with_chat_only_is_not_reported_empty():
    ledger = _ledger_figures(masked=False, deliverables=1)
    ledger.totals.runs = 0
    ledger.totals.units = 0
    page = _sheet(sheet.ledger_value_figures(ledger))
    assert "No unit of work counted" not in page
    assert "0 runs, 0 units of work, 1 chat deliverable." in page


@pytest.mark.asyncio
@pytest.mark.parametrize("masked", [False, True])
async def test_the_sheet_masks_costs_exactly_as_the_ledger(monkeypatch, masked):
    """UX r1 B1 kept: the sheet follows the figures' own ``cost_masked``, so the PDF equals the screen."""
    from app.api.v1 import fleets
    from app.services import roi_ledger

    seen: dict[str, object] = {}

    async def _viewer_for(_db, *, user_id, is_admin):
        seen["reader"] = (user_id, is_admin)
        return "the-viewer"

    async def _figures(_db, **kwargs):
        seen.update(kwargs)
        return _ledger_figures(masked=masked)

    monkeypatch.setattr(roi_ledger, "viewer_for", _viewer_for)
    monkeypatch.setattr(roi_ledger, "figures", _figures)
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=False)
    fleet_id = uuid.uuid4()
    figures = await fleets._datasheet_value(None, fleet_id, user)
    assert figures is not None
    assert figures.cost_masked is masked
    assert seen["reader"] == (user.id, False)
    assert (seen["scope"], seen["scope_id"], seen["window"], seen["viewer"]) == ("fleet", fleet_id, "30d", "the-viewer")
    page = _sheet(figures)
    assert (sheet.COST_HIDDEN in page) is masked
    assert ("550 EUR" in page) is not masked


@pytest.mark.asyncio
async def test_a_failed_ledger_read_says_so(monkeypatch):
    from app.api.v1 import fleets
    from app.services import roi_ledger

    async def _boom(*_a, **_k):
        raise RuntimeError("down")

    monkeypatch.setattr(roi_ledger, "viewer_for", _boom)
    assert await fleets._datasheet_value(None, uuid.uuid4(), SimpleNamespace(id=uuid.uuid4(), is_admin=True)) is None


@pytest.mark.parametrize(
    ("hours", "expected"),
    [(2.55, "2.6 h"), (0.05, "0.1 h"), (1.25, "1.3 h"), (2.54, "2.5 h"), (1234.45, "1.2K h"), (999.95, "1K h")],
)
def test_hours_round_half_up_as_the_screens(hours, expected):
    """UX r1 B2: 2.55 h printed "2.5 h" (float formatting) while the fleet ROI view printed "2.6 h"."""
    assert sheet._hours(hours) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (999, None),
        (999.94, None),
        (999.95, "1K"),
        (1_000, "1K"),
        (1_238, "1.2K"),
        (97_767, "97.8K"),
        (316_151, "316K"),
        (999_950, "1M"),
        (3_470_600, "3.5M"),
        (1_200_000_000, "1.2B"),
        (-40_522, "-40.5K"),
    ],
)
def test_figures_from_1000_are_compact_as_the_screens(value, expected):
    """#10476: the printed page follows number-format.ts, never "1000K", never a raw "316,151"."""
    assert sheet.compact_figure(value) == expected


def test_amounts_counts_and_quota_are_compact_from_1000():
    assert sheet._amount(921.4, "EUR") == "921 EUR"
    assert sheet._amount(999.6, "EUR") == "1K EUR"
    assert sheet._amount(-40_522.0, "USD") == "-40.5K USD"
    assert sheet._amount(316_151.0, "USD") == "316K USD"
    page = _sheet(sheet.value_figures(_figures(runs=4_231, units=25_900), cost_masked=False))
    assert "4.2K runs, 25.9K units of work." in page
    assert sheet.quota_text({"quota": [{"runs_min": 40, "runs_max": 2_500}]}) == "40 to 2.5K runs a month (estimate)"


def test_the_hours_range_ends_round_half_up_too():
    page = _sheet(
        sheet.value_figures(_figures(hours_saved=2.55, hours_saved_low=1.05, hours_saved_high=6.25), cost_masked=False)
    )
    assert "2.6 h" in page
    assert "1.1 h to 6.3 h" in page


def test_an_under_k_fleet_hides_the_value_block_as_the_fleet_hero() -> None:
    """#12077 L7b, design 12.10: fewer than k people, the sheet hides the totals, never the reader's share."""
    from app.services import roi_figures

    ledger = _ledger_figures(masked=False)
    ledger.suppressed = roi_figures.Suppressed(k=5, own_share_only=True)
    assert ledger.as_dict()["suppressed"]["own_share_only"] is True
    figures = sheet.ledger_value_figures(ledger)
    assert figures.under_k == 5
    page = _sheet(figures)
    assert "Fewer than 5 people used this fleet: value totals are hidden." in page
    for hidden in ("550 EUR", "12 runs", "Hours saved"):
        assert hidden not in page


def test_folded_rows_alone_keep_the_hero_on_the_sheet() -> None:
    """Rows folded into "Other" (12.5) leave the hero as GET /roi answers it: the sheet still prints it."""
    from app.services import roi_figures

    ledger = _ledger_figures(masked=False)
    ledger.suppressed = roi_figures.Suppressed(k=5, groups=2)
    figures = sheet.ledger_value_figures(ledger)
    assert figures.under_k is None
    assert figures.net_value == ledger.as_dict()["totals"]["net_value"] == 550.0
    assert "550 EUR" in _sheet(figures)
