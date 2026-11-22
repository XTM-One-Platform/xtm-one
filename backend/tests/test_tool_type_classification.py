"""Tests for the read/write tool classification.

The "Read" badge in the integration tools list is a security guarantee: a
customer enabling a read tool must be confident it cannot create, update,
delete, or perform privileged operations. The old substring heuristic
classified consolidated management tools (manage_*, edit_*) and many
response/remediation verbs (isolate, deploy, terminate, ...) as "read".

Classification is now token-based with a fail-safe default: unknown verbs
are presented as "write", never as "read".
"""

import pytest

from app.api.v1.integration_catalog import build_provider_tools
from app.services.hitl_whitelist_platform import HAND_GATED
from app.services.tool_classification import (
    READ_OVERRIDES,
    classify_tool_name,
    infer_tool_type,
)

# Tools explicitly reported as misclassified plus a sample of mutating/privileged
# tools that the old heuristic classified as "read".
MISCLASSIFIED_WRITE_TOOLS = [
    "manage_opencti_pir",
    "edit_opencti_playbook_graph",
    "manage_opencti_playbook",
    "manage_openaev_scenario",
    "manage_openaev_simulation",
    "isolate_defender_machine",
    "unisolate_xdr_endpoint",
    "deploy_tanium_action",
    "terminate_hibob_employee",
    "revoke_credly_badge",
    "quarantine_xdr_file",
    "mitigate_sentinelone_threat",
    "set_n8n_workflow_active",
    "execute_anything",
    "merge_github_pr",
    "trash_gmail",
    "archive_slack_channel",
    "transition_jira_issue",
    "mark_microsoft_email_read",
    "open_slack_dm",
    "use_opencti_draft",
    "kick_from_slack_channel",
    "post_figma_comment",
    "submit_crowdstrike_sandbox_analysis",
    "schedule_ashby_interview",
    "invite_concord_member",
    "collect_defender_investigation_package",
    "stop_and_quarantine_file",
    # "reset" is a write token: a word of the name that is a read
    # token (status, summary, overview, search) must not make it a read.
    "reset_opencrq_job_status",
    "reset_search_index",
    "reset_jira_board_summary",
    "reset_compliance_overview",
]

READ_TOOLS = [
    "list_opencti_capabilities",
    "query_opencti_pir_relationships",
    "get_qradar_reference_set",  # "set" appears, but the leading verb wins
    "get_jira_service_request",  # "request" appears, but the leading verb wins
    "list_github_workflow_runs",
    "list_sentinelone_blocklist",  # old heuristic matched "lock" inside "blocklist"
    "get_notion_block",  # old heuristic matched "lock" inside "block"
    "get_qradar_offense_addresses",  # old heuristic matched "add" inside "addresses"
    "search_opencti",
    "list_jira_service_requests",
    "get_jira_issue",
    "read_gmail",
    "validate_nlq_filter",
    "intune_list_devices",  # noun-prefixed read verb
    "intune_compliance_overview",
    "full_text_search_openaev",
    "download_slack_file",
    "count_opencti_entities",
    "lookup_cloudtrail_events",  # leading "lookup": CloudTrail LookupEvents is a pure read
]


def test_mutating_tools_are_classified_write():
    misclassified = [
        name for name in MISCLASSIFIED_WRITE_TOOLS if infer_tool_type(name) != "write"
    ]
    assert not misclassified, f"mutating tools presented as read: {misclassified}"


def test_read_tools_are_classified_read():
    misclassified = [name for name in READ_TOOLS if infer_tool_type(name) != "read"]
    assert not misclassified, f"read tools presented as write: {misclassified}"


def test_unknown_verbs_fail_safe_to_write():
    # A tool the heuristic knows nothing about must never get the read badge.
    assert infer_tool_type("frobnicate_the_widget") == "write"
    assert infer_tool_type("zap") == "write"


def test_overrides_still_apply():
    # A SAVED card, run by id: the "run" verb is the only write-ish thing here.
    assert infer_tool_type("run_metabase_question") == "read"
    # No longer an override: the leading "list" verb wins under token matching.
    assert infer_tool_type("list_microsoft_flagged_emails") == "read"
    # Arbitrary GraphQL can run mutations - explicitly not a read.
    assert infer_tool_type("github_graphql_query") == "write"


def test_free_form_statements_are_writes_whatever_the_verb_says():
    """``run_metabase_query`` was a read override with its siblings, on the
    reasoning that it "only executes queries". It takes a free-form ``query``
    and POSTs it verbatim as a native statement to /api/dataset with no
    restriction — unlike ``query_databricks_sql``, which allow-lists
    SELECT/WITH/SHOW/... Classified as a read it was auto-exempt from
    approval, so ``DROP TABLE`` against the warehouse never reached a human.
    """
    assert infer_tool_type("run_metabase_query") == "write"
    assert infer_tool_type("github_graphql_query") == "write"
    # The restricted siblings keep their read classification.
    assert infer_tool_type("query_databricks_sql") == "read"
    assert infer_tool_type("query_github_graphql") == "read"


def test_substring_fragments_do_not_leak_verbs():
    # Token matching: noun fragments must not match write verbs.
    assert infer_tool_type("whois_credit_lookup") == "write"  # unknown -> write
    assert infer_tool_type("get_credit_report") == "read"  # "credit" != "edit"
    assert infer_tool_type("list_flagged_items") == "read"  # "flagged" != "flag"


def test_no_consolidated_management_tool_is_read():
    """Full-catalog audit: every consolidated manage_*/edit_* tool is write.

    These action-based tools bundle create/update/delete operations behind a
    neutral name, which is exactly what slipped through the old heuristic.
    """
    catalog = build_provider_tools()
    offenders = [
        tool.name
        for tools in catalog.values()
        for tool in tools
        if tool.tool_type == "read"
        and tool.name not in READ_OVERRIDES
        and tool.name not in DECLARED_READ_ONLY_HUNTS
        and tool.name.split("_")[0] in {"manage", "edit", "set", "run"}
    ]
    assert not offenders, f"consolidated mutating tools presented as read: {offenders}"


#: Hunting tools whose definition declares ``read_only``: each only queries
#: telemetry (KQL, Deep Visibility, ES|QL, EQL, HarfangLab hunting GETs).
#: The NetSuite pair queries too: ``run_netsuite_suiteql`` refuses anything
#: but a SELECT (or WITH ... SELECT), and ``run_netsuite_saved_search`` runs a
#: saved search by id through the companion RESTlet's read action.
DECLARED_READ_ONLY_HUNTS = {
    "run_defender_hunting_query": "defender",
    "run_sentinelone_dv_query": "sentinelone",
    "run_elastic_esql_query": "elastic_security",
    "run_elastic_eql_query": "elastic_security",
    "hunt_harfanglab_process_hash": "harfanglab",
    "run_netsuite_suiteql": "netsuite",
    "run_netsuite_saved_search": "netsuite",
}


def test_declared_read_only_hunts_carry_the_read_badge() -> None:
    """The name classifier still calls them writes ("run", unknown "hunt");
    the definition's declaration is what makes the catalog badge a read, as a
    read-only binding already keeps them. The approval whitelist still derives
    from the name alone: widening it is a separate decision."""
    catalog = build_provider_tools()
    for name, provider in DECLARED_READ_ONLY_HUNTS.items():
        assert infer_tool_type(name) == "write"
        tool = next(t for t in catalog[provider] if t.name == name)
        assert tool.tool_type == "read", name


def test_a_read_only_binding_keeps_the_declared_hunts() -> None:
    from app.integrations.providers import defender
    from app.services.integration_read_only import withhold_write_tools
    from app.tools.base import CallableToolExecutor

    executor = CallableToolExecutor()
    defs = defender.build_tools(token="tenant||client||secret", executor=executor)
    kept = {d.name for d in withhold_write_tools(defs, executor, integration_label="Defender")}
    assert "run_defender_hunting_query" in kept
    assert "create_defender_indicator" not in kept


def test_definition_tool_type_reads_the_declaration_then_the_name() -> None:
    from types import SimpleNamespace

    from app.services.tool_classification import definition_tool_type

    assert definition_tool_type(SimpleNamespace(name="run_x_query", read_only=True)) == "read"
    assert definition_tool_type(SimpleNamespace(name="run_x_query", read_only=False)) == "write"
    assert definition_tool_type(SimpleNamespace(name="list_x")) == "read"


# "lookup" is a leading read verb, and only a leading one. The verbs
# that name a read for one service and a write for another keep failing safe.
LOOKUP_RULE_CASES = [
    ("lookup_greynoise_ip", ("read", "read_verb")),
    ("lookup_rf_identity_exposures", ("read", "read_verb")),
    ("lookup_cloudtrail_events", ("read", "read_verb")),
    # Later in the name "lookup" is the object of an unknown verb.
    ("whois_credit_lookup", ("write", "default")),
    ("purge_lookup_cache", ("write", "default")),
    ("upload_crowdstrike_ngsiem_lookup_file", ("write", "write_token")),
    ("list_crowdstrike_ngsiem_lookup_files", ("read", "read_verb")),
    # A conjunction naming a write still beats the leading verb.
    ("lookup_or_create_contact", ("write", "write_token")),
    # Ambiguous verbs: a read for a threat-intel vendor, a write elsewhere.
    ("reverse_shodan_dns", ("write", "default")),
    ("reverse_transaction", ("write", "default")),
    ("identify_vulncheck_product", ("write", "default")),
    ("identify_user", ("write", "default")),
    ("triage_rf_indicators", ("write", "default")),
    ("triage_issue", ("write", "default")),
    ("enrich_rf_indicators", ("write", "default")),
    ("enrich_contact", ("write", "default")),
    ("assess_vulnintel_cve", ("write", "default")),
    ("resolve_servicenow_incident", ("write", "default")),
    ("resolve_google_doc_comment", ("write", "default")),
    ("analyse_gti_code", ("write", "default")),
    ("analyze_sublime_link", ("write", "write_token")),
]


@pytest.mark.parametrize(("name", "expected"), LOOKUP_RULE_CASES)
def test_lookup_reads_only_as_the_leading_verb(name, expected):
    assert classify_tool_name(name) == expected


def test_every_catalog_lookup_tool_is_a_read_by_its_verb():
    """Every ``lookup_*`` tool of the catalog gets the Read badge from its verb
    and is exempt from approval (none is hand-gated), so a new one is a read
    the day it lands - and shows up in the HITL integration snapshot's diff."""
    catalog = build_provider_tools()
    lookups = {
        tool.name: classify_tool_name(tool.name)
        for tools in catalog.values()
        for tool in tools
        if tool.name.split("_")[0] == "lookup"
    }
    assert "lookup_greynoise_ip" in lookups
    assert "lookup_cloudtrail_events" in lookups
    assert {name for name, rule in lookups.items() if rule != ("read", "read_verb")} == set()
    assert set(lookups) & set(HAND_GATED) == set()
    assert "lookup_cloudtrail_events" not in READ_OVERRIDES
