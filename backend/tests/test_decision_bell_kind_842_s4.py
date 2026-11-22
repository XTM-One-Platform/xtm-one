"""One ``decision`` bell kind for every human gate (#842, S4; human-gates.md section 5).

A paused run, a flow's Ask-user node and a work item's ask are presented by one
function, ring one urgent entry each, stay unread until answered (#991), push
as urgent and still honour a phone that muted the former kind.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import notification_feed as feed
from app.services.mobile_push import device_wants, push_payload

RUN_ID = "1b2c3d4e-1111-4a5b-9c7d-2e3f4a5b6c7d"
FLOW_ID = "2c3d4e5f-2222-4a5b-9c7d-2e3f4a5b6c7d"
FLOW_RUN_ID = "3d4e5f6a-3333-4a5b-9c7d-2e3f4a5b6c7d"
ITEM_ID = "4e5f6a7b-4444-4a5b-9c7d-2e3f4a5b6c7d"
LATER = datetime(2030, 1, 1, tzinfo=UTC)


def _run() -> SimpleNamespace:
    return SimpleNamespace(
        id=RUN_ID,
        status="awaiting_human",
        created_at="2026-11-21T08:00:00+00:00",
        assignment_name="Weekly brief",
        agent_name="Analyst",
        human_input_question="Approve the export?",
        result_summary=None,
    )


def _flow_ask() -> SimpleNamespace:
    return SimpleNamespace(
        id="5f6a7b8c-5555-4a5b-9c7d-2e3f4a5b6c7d",
        activity_type="notification",
        agent_name="Quarterly export",
        title="Export",
        summary="Send the report?",
        extra_data={"notification_source": "flow_hitl", "flow_id": FLOW_ID, "flow_run_id": FLOW_RUN_ID},
        created_at="2026-11-21T08:00:00+00:00",
    )


def _three() -> tuple[feed.NotificationItem, feed.NotificationItem, feed.NotificationItem]:
    run = feed.present_run(_run())
    flow = feed.present_activity(_flow_ask(), flows_enabled=True)
    item = feed.present_waiting_work_item(
        {"id": ITEM_ID, "title": "Vendor", "question": "A or B?", "since": "2026-11-21T08:00:00+00:00"}
    )
    assert run is not None and flow is not None and item is not None
    return run, flow, item


def test_the_three_sources_are_one_decision_kind() -> None:
    run, flow, item = _three()
    assert [i.kind for i in (run, flow, item)] == ["decision"] * 3
    assert [i.source for i in (run, flow, item)] == ["run", "flow_run", "work_item"]
    assert [i.title for i in (run, flow, item)] == ["Approve the export?", "Send the report?", "A or B?"]
    assert [i.subtitle for i in (run, flow, item)] == ["Analyst", "Quarterly export", None]
    assert run.route == f"/operations/executions/{RUN_ID}"
    assert flow.route == f"/flows/library/{FLOW_ID}?run={FLOW_RUN_ID}"
    assert item.route == f"/tasks/work-items/{ITEM_ID}"
    assert (run.run_id, flow.run_id, item.run_id) == (RUN_ID, FLOW_RUN_ID, None)
    # Ids are the ones each source always had: read and dismissed state survive.
    assert (run.id, item.id) == (RUN_ID, f"work_item:{ITEM_ID}")


def test_decision_is_urgent_and_unread_until_answered() -> None:
    assert "decision" in feed.NOTIFICATION_KINDS
    assert "decision" in feed.URGENT_KINDS
    assert "decision" in feed.UNREAD_UNTIL_ANSWERED_KINDS
    run, _flow, item = _three()
    for entry in (run, item):
        assert entry.awaiting_answer is True
        assert feed.is_unread(entry, read_at=LATER, dismissed={entry.id})


def test_the_former_kinds_still_read() -> None:
    for alias in ("hitl", "flow_hitl", "work_item_waiting"):
        assert alias in feed.NOTIFICATION_KINDS
        assert alias in feed.UNREAD_UNTIL_ANSWERED_KINDS
        entry = feed.NotificationItem(
            id=alias, kind=alias, created_at="2026-11-21T08:00:00+00:00", route="/", title="t", awaiting_answer=True
        )
        assert feed.is_unread(entry, read_at=LATER, dismissed={alias})


def test_an_unknown_source_is_refused() -> None:
    with pytest.raises(ValueError):
        feed.present_decision(
            entry_id="x", source="chat", source_id=ITEM_ID, created_at="", question="q", fallback_title="t", by=None
        )


def test_the_feed_response_carries_the_source() -> None:
    """The API model keeps ``source``, so the bell can read who asks (lab, round 1)."""
    from app.api.v1.notifications import NotificationItemResponse

    for entry in _three():
        body = NotificationItemResponse.model_validate({**entry.as_dict(), "unread": True}).model_dump()
        assert body["source"] == entry.source
        assert body["kind"] == "decision"


def test_every_decision_pushes_urgent() -> None:
    for entry in _three():
        assert push_payload(entry, minimal=False)["urgent"] is True


@pytest.mark.parametrize(
    ("source", "muted", "wanted"),
    [
        ("run", ["hitl"], False),
        ("flow_run", ["flow_hitl"], False),
        ("work_item", ["work_item_waiting"], False),
        ("run", ["decision"], False),
        ("run", ["flow_hitl"], True),
        ("work_item", [], True),
    ],
)
def test_a_phone_that_muted_the_former_kind_stays_muted(source: str, muted: list[str], wanted: bool) -> None:
    device = SimpleNamespace(push_enabled=True, muted_kinds=muted, quiet_hours=None)
    assert device_wants(device, "decision", LATER, source=source) is wanted


@pytest.mark.asyncio
async def test_a_flow_decision_is_pinned_only_while_its_run_waits() -> None:
    _run_entry, flow, _item = _three()

    class _Result:
        def scalars(self) -> _Result:
            return self

        def all(self) -> list[SimpleNamespace]:
            return [SimpleNamespace(id=FLOW_RUN_ID, status="awaiting", state={"waiting": {"kind": "human"}})]

    class _Db:
        async def execute(self, _statement: Any) -> _Result:
            return _Result()

    db: Any = _Db()
    [marked] = await feed.mark_waiting_flow_asks(db, [flow])
    assert marked.awaiting_answer is True
    assert feed.is_unread(marked, read_at=LATER, dismissed={marked.id})
