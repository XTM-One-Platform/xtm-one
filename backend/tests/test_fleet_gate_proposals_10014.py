"""Approval gates proposed from a fleet's configuration (#10014, slice A2 of #10000).

Pinned here: the impact table (reads never gated, marks low, sends and
deletions destructive, other writes high); proposals grouped by provider and
impact, honouring read-only bindings and tool scoping; proposals accepted as a
catalog validate as ``DocGate`` and are enforced by the level's tool rules;
the approvals outside the level are listed; a catalog change that lets an
action run with less approval is detected.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.api.v1.schemas.fleet import FleetApprovalGatesUpdateRequest
from app.services.fleet_autonomy import fleet_gates, gated_tool_rules, is_gated
from app.services.fleet_docs import GATES_MAX, DocGate, clean_structured
from app.services.fleet_gate_proposals import (
    GATE_TOOLS_MAX,
    existing_approvals,
    propose_gates,
    removes_a_gate,
    tool_impact,
)


@pytest.mark.parametrize(
    ("name", "impact"),
    [
        ("list_github_prs", None),
        ("get_jira_issue", None),
        ("search_microsoft_emails", None),
        ("add_github_labels", "high"),
        ("mark_microsoft_email_read", "low"),
        ("flag_microsoft_email", "low"),
        ("create_jira_issue", "high"),
        ("create_github_pr", "high"),
        ("post_slack_message", "high"),
        ("send_slack_message", "high"),
        ("reply_slack_thread", "high"),
        ("send_microsoft_chat_message", "high"),
        ("send_microsoft_email", "destructive"),
        ("reply_microsoft_email", "destructive"),
        ("forward_microsoft_email", "destructive"),
        ("send_gmail", "destructive"),
        ("send_docusign_envelope", "destructive"),
        ("delete_github_file", "destructive"),
        # A draft is internal until sent (UX round 2 B2 on #10034).
        ("delete_draft_email", "low"),
        ("create_microsoft_draft_email", "low"),
        ("create_microsoft_draft_reply", "low"),
        ("delete_microsoft_draft", "low"),
        ("send_microsoft_draft", "destructive"),
        ("send_gmail_draft", "destructive"),
        ("add_slack_reaction", "low"),
        ("add_slack_bot_reaction", "low"),
        ("compare_github_commits", None),
        # Arbitrary GraphQL runs mutations too: it stays gated (tool_classification.WRITE_OVERRIDES).
        ("github_graphql_query", "high"),
        ("intune_isolate_device", "destructive"),
        ("launch_openaev_simulation", "high"),
        ("frobnicate_widget", "high"),
    ],
)
def test_the_impact_table(name, impact):
    assert tool_impact(name) == impact


def _agent(**fields):
    base = {
        "id": uuid.uuid4(),
        "name": "Agent",
        "integration_bindings": {},
        "integration_template_bindings": [],
        "integration_read_only": None,
        "integration_allowed_tools": None,
        "tool_deny_list": None,
        "human_in_the_loop": False,
        "approve_tool_calls": None,
    }
    base.update(fields)
    return SimpleNamespace(**base)


_TOOLS = {
    "microsoft": ["list_microsoft_emails", "send_microsoft_email", "flag_microsoft_email", "create_microsoft_event"],
    "jira": ["get_jira_issue", "create_jira_issue"],
}


def test_proposals_group_write_tools_by_provider_and_impact_most_severe_first():
    agent = _agent(integration_bindings={"microsoft": ["m1"], "jira": ["j1"]})
    gates = propose_gates([agent], _TOOLS, {"microsoft": "Microsoft 365"})
    assert [(g["key"], g["impact"]) for g in gates] == [
        ("microsoft.destructive-writes", "destructive"),
        ("jira.high-writes", "high"),
        ("microsoft.high-writes", "high"),
        ("microsoft.low-writes", "low"),
    ]
    destructive = gates[0]
    assert [t["name"] for t in destructive["tools"]] == ["send_microsoft_email"]
    # A plain action per row, the integration named, no tool name in it (UX round 1 B1, B2 on #10034).
    assert destructive["action"] == "Delete, or send in a person's name, in Microsoft 365"
    assert destructive["provider"] == "microsoft"
    assert gates[1]["action"] == "Create or change content in jira"
    assert gates[3]["action"] == "Mark, label or draft in Microsoft 365"
    assert all("_" not in g["action"] for g in gates)
    assert all(t["name"] not in {"list_microsoft_emails", "get_jira_issue"} for g in gates for t in g["tools"])


def test_a_read_only_binding_and_tool_scoping_are_honoured():
    read_only = _agent(integration_bindings={"microsoft": ["m1"]}, integration_read_only={"microsoft": ["m1"]})
    assert propose_gates([read_only], _TOOLS) == []
    scoped = _agent(
        integration_bindings={"microsoft": ["m1"]},
        integration_allowed_tools={"microsoft": ["list_microsoft_emails", "flag_microsoft_email"]},
    )
    assert [g["key"] for g in propose_gates([scoped], _TOOLS)] == ["microsoft.low-writes"]
    denied = _agent(integration_bindings={"jira": ["j1"]}, tool_deny_list=["create_jira_issue"])
    assert propose_gates([denied], _TOOLS) == []


def test_template_bindings_count_and_members_merge():
    one = _agent(integration_template_bindings=["jira"])
    two = _agent(integration_bindings={"jira": ["j2"]})
    gates = propose_gates([one, two], _TOOLS)
    assert [g["key"] for g in gates] == ["jira.high-writes"]
    assert gates[0]["module"] == "platform"


def test_one_gate_per_integration_and_impact_never_numbered_parts():
    # UX round 2 B1 on #10034: identical rows "(1)" to "(4)" told the reader nothing.
    tools = {"github": [f"create_github_thing{i}" for i in range(40)]}
    gates = propose_gates([_agent(integration_bindings={"github": ["g"]})], tools)
    assert [g["key"] for g in gates] == ["github.high-writes"]
    assert len(gates[0]["tools"]) == 40
    assert gates[0]["action"] == "Create or change content in github"
    DocGate.model_validate(gates[0])


def test_a_long_tool_list_is_split_under_the_gate_bound():
    tools = {"github": [f"create_github_thing{i}" for i in range(GATE_TOOLS_MAX + 3)]}
    gates = propose_gates([_agent(integration_bindings={"github": ["g"]})], tools)
    assert [g["key"] for g in gates] == ["github.high-writes-1", "github.high-writes-2"]
    assert [len(g["tools"]) for g in gates] == [GATE_TOOLS_MAX, 3]
    assert [g["action"] for g in gates] == [
        "Create or change content in github (1/2)",
        "Create or change content in github (2/2)",
    ]


def test_accepted_proposals_are_a_valid_catalog_the_level_enforces():
    agent = _agent(integration_bindings={"microsoft": ["m1"], "jira": ["j1"]})
    catalog = (clean_structured({"gates": propose_gates([agent], _TOOLS)}) or {})["gates"]
    gates = fleet_gates(catalog, None)
    full = {r["name"] for r in gated_tool_rules("full_gates", gates)}
    assert full == {"send_microsoft_email", "create_microsoft_event", "flag_microsoft_email", "create_jira_issue"}
    low = {r["name"] for r in gated_tool_rules("low", gates)}
    assert low == {"send_microsoft_email"}
    assert gated_tool_rules("autonomous", gates) == []
    assert is_gated("medium", "high") and not is_gated("medium", "low")


def test_every_proposal_of_a_large_fleet_is_accepted_at_once():
    # UX round 4 B1 on #10034: "Accept all (25)" was refused by a catalog bound of 20.
    providers = [f"prov{i:02d}" for i in range(30)]
    tools = {p: [f"delete_{p}_item", f"create_{p}_item", f"flag_{p}_item"] for p in providers}
    agent = _agent(integration_bindings={p: [p] for p in providers})
    proposals = propose_gates([agent], tools)
    assert len(proposals) == 90 <= GATES_MAX
    body = FleetApprovalGatesUpdateRequest.model_validate({"gates": proposals})
    catalog = (clean_structured({"gates": body.gates}) or {})["gates"]
    assert [g["key"] for g in catalog] == [g["key"] for g in proposals]


def test_the_put_body_and_the_catalog_share_one_bound():
    assert FleetApprovalGatesUpdateRequest.model_fields["gates"].metadata[0].max_length == GATES_MAX


def test_proposals_never_exceed_the_catalog_bound():
    providers = [f"prov{i:03d}" for i in range(GATES_MAX)]
    tools = {p: [f"delete_{p}_item", f"create_{p}_item"] for p in providers}
    agent = _agent(integration_bindings={p: [p] for p in providers})
    assert len(propose_gates([agent], tools)) == GATES_MAX


def test_existing_approvals_are_listed():
    agent = _agent(name="Mail", human_in_the_loop=True, approve_tool_calls=True)
    other = _agent(name="Dev")
    runs = [SimpleNamespace(agent_id=other.id, name="Merge", human_in_the_loop=True)]
    assert existing_approvals([agent, other], runs) == [
        {"member": "Mail", "approval": "Asks a person before acting (human in the loop)."},
        {"member": "Mail", "approval": "Each tool call waits for approval."},
        {"member": "Dev", "approval": "Automation 'Merge' asks a person before acting."},
    ]


def _gate(key, impact="high", tools=("create_jira_issue",)):
    return {"key": key, "action": key, "impact": impact, "tools": [{"name": t, "when": {}} for t in tools]}


def test_a_change_that_lets_an_action_run_with_less_approval_is_detected():
    before = [_gate("a"), _gate("b", "destructive", ("send_microsoft_email",))]
    assert not removes_a_gate([], before)
    assert not removes_a_gate(before, [*before, _gate("c")])
    assert not removes_a_gate([_gate("a", "low")], [_gate("a", "high")])
    assert removes_a_gate(before, before[:1])
    assert removes_a_gate(before, [_gate("a"), _gate("b", "high", ("send_microsoft_email",))])
    assert removes_a_gate(before, [_gate("a", tools=()), before[1]])
