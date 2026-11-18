"""The Flow Lab on level 6 (#8260): its experiment kinds, and its findings and drafts as its own writes."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("handler", ["_record_flow_finding", "_draft_flow_variant"])
@pytest.mark.parametrize(
    "caller",
    [
        SimpleNamespace(is_health_principal=False, principal=None),
        SimpleNamespace(is_health_principal=True, principal=SimpleNamespace(role="quality_analyst", agent_id=None)),
    ],
    ids=["person", "other-concierge-role"],
)
async def test_findings_and_drafts_are_the_flow_labs_own_writes(handler, caller):
    from app.services.builtin_tools import flow_lab as tools

    assert await getattr(tools, handler)(None, caller) == "Error: only the Platform Health Flow Lab can do this."


def test_level_6_measures_every_flow_type():
    import app.services.builtin_tools.flow_lab  # noqa: F401 - what loads the kinds at startup
    from app.services.health.experiments import experiment_kind
    from app.services.health.flow_experiments import AgentToFlowKind, FlowGraphKind, NewFlowKind

    assert isinstance(experiment_kind("flow_repair"), FlowGraphKind)
    assert isinstance(experiment_kind("flow_optimization"), FlowGraphKind)
    assert type(experiment_kind("new_flow")) is NewFlowKind
    assert isinstance(experiment_kind("agent_to_flow"), AgentToFlowKind)


@pytest.mark.parametrize(
    ("kind", "finding_target"),
    [("flow_repair", "agent"), ("new_flow", "flow"), ("agent_to_flow", "agent")],
)
async def test_a_variant_must_target_its_findings_target(kind, finding_target):
    import app.services.builtin_tools.flow_lab  # noqa: F401 - loads the kinds
    from app.services.health.experiments import ExperimentError, experiment_kind

    target_id = uuid.uuid4()
    finding = SimpleNamespace(target_type=finding_target, target_id=target_id)
    with pytest.raises(ExperimentError):
        await experiment_kind(kind).prepare_draft(None, finding=finding, target_id=target_id, payload={})


def test_a_variant_says_what_it_changes():
    from app.services.health.experiments import ExperimentError
    from app.services.health.flow_experiments import _summary

    assert _summary({"summary": "  Retry the  HTTP node twice. "}) == "Retry the HTTP node twice."
    assert len(_summary({"summary": "x" * 900})) == 500
    with pytest.raises(ExperimentError, match="summary must say"):
        _summary({"summary": "   "})
