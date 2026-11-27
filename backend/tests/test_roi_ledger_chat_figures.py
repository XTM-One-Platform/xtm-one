"""The ledger's chat totals answered by GET /roi/chat and get_roi_chat_series (issue #12077, slice L4b).

``roi_ledger.chat_figures`` is the chat row of ``roi_ledger.figures`` for the series'
scope (design 12.8), so the chat card and the Value page's By source cannot differ.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, date, datetime

import pytest

from app.services import roi_figures, roi_ledger
from app.services.roi_figures import CLASS_AUTOMATION, LedgerFact, Viewer, chat_fact

DAY = date(2026, 11, 10)
NOW = datetime(2026, 11, 15, 12, tzinfo=UTC)
ME, OTHER = uuid.UUID(int=1), uuid.UUID(int=2)
AGENT, HIDDEN_AGENT = uuid.UUID(int=10), uuid.UUID(int=11)
FLEET = uuid.UUID(int=20)


def _facts() -> list[LedgerFact]:
    return [
        chat_fact(
            day=DAY, person_id=ME, agent_id=AGENT, fleet_id=FLEET, unit="presentation_built", minutes=60, rate=60.0
        ),
        chat_fact(
            day=DAY, person_id=OTHER, agent_id=AGENT, fleet_id=FLEET, unit="presentation_built", minutes=30, rate=60.0
        ),
        chat_fact(
            day=DAY,
            person_id=OTHER,
            agent_id=HIDDEN_AGENT,
            fleet_id=None,
            unit="report_written",
            minutes=120,
            rate=60.0,
        ),
        LedgerFact(
            day=DAY,
            person_id=ME,
            attribution="user",
            agent_id=AGENT,
            fleet_id=FLEET,
            cost_class=CLASS_AUTOMATION,
            runs=1,
            cost=1.0,
        ),
    ]


@pytest.fixture
def ledger(monkeypatch):
    async def _ledger_facts(_db, **_kw):
        return _facts(), "EUR"

    async def _fleet_supervisors(_db, _fleet_ids):
        return {}

    monkeypatch.setattr(roi_ledger, "ledger_facts", _ledger_facts)
    monkeypatch.setattr(roi_ledger, "fleet_supervisors", _fleet_supervisors)


def _member() -> Viewer:
    return Viewer(user_id=ME, agent_ids=frozenset({AGENT}), fleet_ids=frozenset({FLEET}))


@pytest.mark.asyncio
async def test_the_chat_totals_are_the_chat_row_of_the_scope(ledger):
    viewer = _member()
    answer = await roi_ledger.chat_figures(None, viewer=viewer, agent_id=AGENT, now=NOW)
    full = await roi_ledger.figures(None, scope="agent", viewer=viewer, scope_id=AGENT, now=NOW)
    chat_row = next(r for r in full.as_dict()["by_source"] if r["key"] == roi_figures.CLASS_CHAT)
    assert answer is not None
    assert answer["scope"] == "agent" and answer["id"] == str(AGENT) and answer["currency"] == "EUR"
    assert answer["totals"] == {k: v for k, v in chat_row.items() if k != "key"}
    assert answer["totals"]["deliverables"] == 2
    assert answer["totals"]["hours"] == 1.5


@pytest.mark.asyncio
async def test_mine_reads_only_the_viewer_s_chat(ledger):
    answer = await roi_ledger.chat_figures(None, viewer=_member(), mine=True, agent_id=AGENT, now=NOW)
    assert answer is not None
    assert answer["scope"] == "mine"
    assert (answer["totals"]["deliverables"], answer["totals"]["hours"]) == (1, 1.0)


@pytest.mark.asyncio
async def test_the_organization_total_is_the_same_for_every_viewer(ledger):
    """Invariant (b) on the chat source: the organization's chat total does not depend on who reads it."""
    member = await roi_ledger.chat_figures(None, viewer=_member(), now=NOW)
    admin = await roi_ledger.chat_figures(None, viewer=Viewer(user_id=OTHER, organization=True), now=NOW)
    assert member is not None and admin is not None
    assert member["totals"] == admin["totals"]
    assert member["totals"]["deliverables"] == 3


@pytest.mark.asyncio
async def test_an_object_the_viewer_does_not_see_answers_no_totals(ledger):
    assert await roi_ledger.chat_figures(None, viewer=_member(), agent_id=HIDDEN_AGENT, now=NOW) is None
    assert await roi_ledger.chat_figures(None, viewer=_member(), fleet_id=uuid.uuid4(), now=NOW) is None


@pytest.mark.asyncio
async def test_a_scope_with_no_chat_answers_zeros_never_absent(ledger):
    empty = await roi_ledger.chat_figures(
        None, viewer=Viewer(user_id=uuid.uuid4(), organization=True), mine=True, now=NOW
    )
    assert empty is not None
    assert (empty["totals"]["deliverables"], empty["totals"]["value"]) == (0, 0.0)


@pytest.mark.asyncio
async def test_a_fleet_s_chat_card_hero_and_by_fleet_count_the_same_chat(ledger):
    """Invariant card = ledger for chat (#12077): the fleet's chat is the chat stamped with the fleet.

    A member agent's chat stamped with no fleet (written before it joined) is in "No fleet" on
    the organization's By fleet, and in neither the fleet's card nor its hero.
    """
    admin = Viewer(user_id=OTHER, organization=True)
    card = await roi_ledger.chat_figures(None, viewer=admin, fleet_id=FLEET, now=NOW)
    hero = await roi_ledger.figures(None, scope="fleet", viewer=admin, scope_id=FLEET, now=NOW)
    org = (await roi_ledger.figures(None, scope="organization", viewer=admin, now=NOW)).as_dict()
    assert card is not None
    hero_chat = next(r for r in hero.as_dict()["by_source"] if r["key"] == roi_figures.CLASS_CHAT)
    fleet_row = next(r for r in org["by_fleet"] if r["key"] == str(FLEET))
    assert (card["totals"]["deliverables"], card["totals"]["hours"]) == (2, 1.5)
    assert card["totals"]["hours"] == hero_chat["hours"]
    assert card["totals"]["value"] == hero_chat["value"]
    # The fleet's By fleet row holds the same chat value plus its automation value (zero here).
    assert fleet_row["value"] == hero.as_dict()["totals"]["value"]


def test_the_fleet_chat_series_selects_the_lines_the_ledger_stamps_with_the_fleet():
    """The card's lines follow ``roi_chat_records.fleet_id``, never today's member agents (#12077)."""
    from app.api.v1 import roi as roi_api
    from app.services import roi_chat_series

    route = inspect.getsource(roi_api.get_roi_chat_series)
    assert "fleet_member_agents" not in route
    assert "fleet_id=fleet_id," in route
    assert "RoiChatRecord.fleet_id == fleet_id" in inspect.getsource(roi_chat_series.load_series)


def test_the_route_and_the_tool_answer_the_ledger_chat_totals():
    from app.api.v1 import roi as roi_api
    from app.services.builtin_tools import roi as roi_tools

    route = inspect.getsource(roi_api.get_roi_chat_series)
    assert 'series["figures"] = await roi_ledger.chat_figures(' in route
    assert "mine=mine, agent_id=agent_id, fleet_id=fleet_id" in route
    assert 'series["figures"] = await roi_ledger.chat_figures(' in inspect.getsource(roi_tools)
