"""#2583 point 3: a report CONTAINING an entity flagged in a PIR reaches the PIR desk.

OpenCTI's PIR playbook (``create_rel``, ``listenPirEventsUtils.stixIdOfLinkedEntity``) reads
only a new relationship and never a container's ``object_refs``, so the linked mode does not
catch a report that merely contains a flagged entity. ``pir_relation: contained`` provisions a
live trigger on Reports scoped (``objects``) to the entities flagged in the PIRs.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.agents.opencti_pir_desk import PIR_DESK_ASSIGNMENTS, REPORT_CONTAINED
from app.services import opencti_trigger_provisioning as prov

from .test_opencti_trigger_provisioning import FakeOpenCTI


class OpenCTIWithFlagged(FakeOpenCTI):
    def __init__(self, flagged: list[dict[str, str]]) -> None:
        super().__init__()
        self.flagged = flagged
        self.flagged_reads: list[dict[str, Any]] = []

    async def __call__(self, query: str, variables: dict[str, Any] | None = None) -> tuple[dict[str, Any], str | None]:
        if "XtmPirFlagged" in query:
            self.flagged_reads.append(dict(variables or {}))
            return {"stixDomainObjects": {"edges": [{"node": n} for n in self.flagged]}}, None
        found: tuple[dict[str, Any], str | None] = await super().__call__(query, variables)
        return found


def _spec() -> dict[str, Any]:
    assignment = next(a for a in PIR_DESK_ASSIGNMENTS if a["name"] == REPORT_CONTAINED)
    spec = prov.read_provision_spec(assignment["trigger_config"])
    assert spec is not None
    return {**spec, "pir_ids": ["pir-1"]}


async def test_the_trigger_listens_to_reports_containing_a_flagged_entity() -> None:
    octi = OpenCTIWithFlagged([{"id": "malware-internal", "standard_id": "malware--abc"}])
    state = await prov.reconcile(octi, _spec(), None, url="https://xtm/hook", secret="s")
    assert state["status"] == "provisioned", state
    assert state["contained_ids"] == "2", "the scope size is visible on the source state"
    [trigger] = octi.triggers.values()
    filters = json.loads(trigger["filters"])
    assert {"key": ["entity_type"], "values": ["Report"], "operator": "eq", "mode": "or"} in filters["filters"]
    contained = [f for g in filters["filterGroups"] for f in g["filters"] if f["key"] == ["objects"]]
    assert contained and set(contained[0]["values"]) == {"malware-internal", "malware--abc"}
    assert not any(f["key"] == ["pir_score"] for f in filters["filters"]), "a report carries no PIR score"
    assert sorted(trigger["event_types"]) == ["create", "update"], "a report gaining a flagged entity is an update"
    regarding = octi.flagged_reads[0]["filters"]["filters"][0]
    pir_scope = {"key": "id", "values": ["pir-1"], "operator": "eq", "mode": "or"}
    assert regarding["key"] == "regardingOf" and pir_scope in regarding["values"]


async def test_nothing_flagged_yet_is_an_error_rechecked_never_a_trigger_on_every_report() -> None:
    octi = OpenCTIWithFlagged([])
    state = await prov.reconcile(octi, _spec(), None, url="https://xtm/hook", secret="s")
    assert state["status"] == "error" and "no entity is flagged" in state["error"]
    assert not octi.triggers


@pytest.mark.parametrize(
    ("provision", "words"),
    [
        ({"pir_relation": "contained", "entity_types": ["Report"]}, "organization_context"),
        (
            {"pir_relation": "contained", "entity_types": ["Vulnerability"], "organization_context": ["pir_ids"]},
            "containers only",
        ),
        (
            {
                "pir_relation": "contained",
                "mode": "pir_playbook",
                "entity_types": ["Report"],
                "organization_context": ["pir_ids"],
            },
            "live trigger",
        ),
    ],
)
def test_an_impossible_contained_spec_is_refused_with_its_reason(provision: dict[str, Any], words: str) -> None:
    with pytest.raises(prov.ProvisionSpecError, match=words):
        prov.read_provision_spec({"provider": "opencti", "provision": {"fleet": "F", **provision}})
