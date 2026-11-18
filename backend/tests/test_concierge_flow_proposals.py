"""The flow proposal handlers of the Flow Lab on level 3 (#8260): loaded, typed, refused when stale."""

from __future__ import annotations

from types import SimpleNamespace


def test_level_3_loads_a_handler_for_every_flow_type():
    from app.models.improvement_proposal import TARGET_OF
    from app.services.improvement_proposals import handler_for
    from app.services.improvement_proposals.registry import self_watched_types

    for proposal_type in ("flow_repair", "flow_optimization", "new_flow", "agent_to_flow"):
        handler = handler_for(proposal_type)
        assert handler.proposal_type == proposal_type
        assert handler.target_type == TARGET_OF[proposal_type]
    assert "agent_to_flow" in self_watched_types()
    assert {"flow_repair", "flow_optimization", "new_flow"}.isdisjoint(self_watched_types())


async def test_a_flow_change_measured_on_an_older_version_is_stale():
    from app.services.health.flow_proposals import FlowGraphHandler

    handler = FlowGraphHandler("flow_repair")
    proposal = SimpleNamespace(base_version_number=3)
    assert await handler.stale_error(None, proposal, SimpleNamespace(version_number=3)) is None
    assert await handler.stale_error(None, proposal, SimpleNamespace(version_number=4)) == (
        "The flow changed since this variant was measured. Reject it and let the Flow Lab draft again."
    )


async def test_a_replacement_of_a_paused_or_changed_assignment_is_stale():
    from app.services.health.flow_proposals import AgentToFlowHandler, AssignmentTarget

    graph = {"nodes": [{"id": "t", "kind": "trigger.schedule", "config": {"cron": "0 8 * * *"}}], "edges": []}
    proposal = SimpleNamespace(change={"graph": graph})
    agent = SimpleNamespace(integration_bindings={})

    def target(enabled, cron):
        assignment = SimpleNamespace(enabled=enabled, trigger_type="schedule", trigger_config={"cron": cron})
        return AssignmentTarget(assignment, agent)

    handler = AgentToFlowHandler()
    assert await handler.stale_error(None, proposal, target(True, "0 8 * * *")) is None
    assert await handler.stale_error(None, proposal, target(False, "0 8 * * *")) == (
        "The assignment is paused: a flow cannot take over a trigger that does not fire."
    )
    assert await handler.stale_error(None, proposal, target(True, "0 9 * * *")) == (
        "The assignment changed since this replacement was measured: the flow's schedule differs from the assignment's."
    )


async def test_a_new_flow_its_manager_activated_is_never_archived_on_a_regression(monkeypatch):
    import uuid

    from app.models.flow import FLOW_STATUS_ACTIVE, FLOW_STATUS_DRAFT
    from app.services.health import flow_proposals
    from app.services.health.flow_proposals import NewFlowHandler
    from app.services.improvement_proposals.lifecycle import STATUS_REGRESSION_KEPT, _regress

    flow = SimpleNamespace(id=uuid.uuid4(), name="Digest", status=FLOW_STATUS_ACTIVE, version_number=1, user_id=None)

    class FakeDb:
        async def get(self, _model, _id):
            return flow

    async def no_audit(*_args, **_kwargs):
        raise AssertionError("an activated flow is never archived")

    monkeypatch.setattr(flow_proposals, "_system_audit", no_audit)
    proposal = SimpleNamespace(id=uuid.uuid4(), apply_state={"flow_id": str(flow.id)}, decided_by_user_id=None)
    handler = NewFlowHandler()

    # Activation keeps version 1: only the status says the manager took it on.
    assert await handler.is_still_applied(FakeDb(), proposal, None) is False
    await _regress(FakeDb(), proposal, handler, None, "The flow failed 40 % of its last 5 runs")
    assert (proposal.status, flow.status) == (STATUS_REGRESSION_KEPT, FLOW_STATUS_ACTIVE)
    assert await handler.rollback(FakeDb(), proposal, None, "regressed") is None
    assert flow.status == FLOW_STATUS_ACTIVE

    flow.status = FLOW_STATUS_DRAFT
    assert await handler.is_still_applied(FakeDb(), proposal, None) is True
