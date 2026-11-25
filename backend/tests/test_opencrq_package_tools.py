"""Tests for the OpenCRQ provider package.

Covers tool presence, valid JSON schemas, handler registration, and
executor dispatch smoke (early-return validation paths only — no network).
"""

import contextlib
import json
from unittest.mock import patch

import jsonschema
import pytest

from app.integrations.providers.opencrq import _common as opencrq_common
from app.integrations.providers.opencrq import admin as opencrq_admin
from app.integrations.providers.opencrq import assets as opencrq_assets
from app.integrations.providers.opencrq import build_tools
from app.integrations.providers.opencrq import crq as opencrq_crq
from app.integrations.providers.opencrq import frameworks as opencrq_frameworks
from app.integrations.providers.opencrq import ops as opencrq_ops
from app.integrations.providers.opencrq import risk as opencrq_risk
from app.integrations.providers.opencrq._common import (
    ASSET_CATEGORIES,
    ASSET_SUBCATEGORIES,
    _clean_ids,
    _fmt_assessment,
    _fmt_control,
    _fmt_cve,
    _fmt_risk_scenario,
    _fmt_supporting_asset,
    _grc_link,
    _grc_primary_asset_link,
    _grc_scenario_link,
    _resolve_tenant,
)
from app.integrations.providers.opencrq.crq import _fmt_run
from app.integrations.providers.opencrq.ops import _fmt_job_row
from app.tools.base import CallableToolExecutor, ToolCall

TOKEN = "https://demo.opencrq.io||test-token"

EXPECTED_TOOLS = {
    # Tenancy / catalog
    "list_opencrq_tenants",
    "list_opencrq_organizations",
    "get_opencrq_organization",
    "list_opencrq_frameworks",
    "get_opencrq_framework",
    "list_opencrq_framework_requirements",
    "list_opencrq_drata_controls",
    # Controls
    "list_opencrq_control_capabilities",
    "get_opencrq_control_capability",
    "update_opencrq_control_capability_assessment",
    "manage_opencrq_scope_control_posture",
    "manage_opencrq_capability_techniques",
    "get_opencrq_control_capability_history",
    # Assessments
    "list_opencrq_compliance_assessments",
    "get_opencrq_compliance_assessment",
    "create_opencrq_compliance_assessment",
    "manage_opencrq_compliance_assessment",
    "update_opencrq_requirement_answer",
    # Risk
    "list_opencrq_risk_scenarios",
    "get_opencrq_risk_scenario",
    "create_opencrq_risk_scenario",
    "update_opencrq_risk_scenario",
    "delete_opencrq_risk_scenarios",
    "update_opencrq_risk_tolerance",
    "get_opencrq_scenario_exposure",
    "get_opencrq_cve_exploitation",
    "get_opencrq_attack_path_reach",
    "list_opencrq_attack_outcomes",
    "get_opencrq_attack_outcome",
    "get_opencrq_financial_summary",
    "get_opencrq_threat_relevance_settings",
    "update_opencrq_threat_relevance_settings",
    # CRQ
    "run_opencrq_crq_simulation",
    "get_opencrq_crq_run",
    "list_opencrq_crq_run_history",
    "get_opencrq_crq_what_if",
    "get_opencrq_tef_input",
    "update_opencrq_tef_input",
    "list_opencrq_loss_drivers",
    "get_opencrq_loss_driver",
    "update_opencrq_loss_estimates",
    "get_opencrq_crq_settings",
    "update_opencrq_crq_settings",
    "get_opencrq_financial_profile",
    "update_opencrq_financial_profile",
    # Assets & vulnerabilities
    "list_opencrq_primary_assets",
    "get_opencrq_primary_asset",
    "list_opencrq_business_value_categories",
    "create_opencrq_primary_asset",
    "update_opencrq_primary_asset",
    "delete_opencrq_primary_assets",
    "list_opencrq_supporting_assets",
    "get_opencrq_supporting_asset",
    "create_opencrq_supporting_asset",
    "update_opencrq_supporting_asset",
    "delete_opencrq_supporting_assets",
    "reset_opencrq_supporting_asset_to_connectors",
    "list_opencrq_asset_dependencies",
    "list_opencrq_dependency_rules",
    "preview_opencrq_dependency_rule",
    "create_opencrq_dependency_rule",
    "update_opencrq_dependency_rule",
    "delete_opencrq_dependency_rules",
    "create_opencrq_asset_dependency",
    "update_opencrq_asset_dependency_roles",
    "delete_opencrq_asset_dependencies",
    "create_opencrq_asset_evidence",
    "update_opencrq_evidence_state",
    "delete_opencrq_asset_evidence",
    "reset_opencrq_evidence_state",
    "get_opencrq_evidence",
    "list_opencrq_cve_evidences",
    "list_opencrq_unique_findings",
    "search_opencrq_cves",
    "list_opencrq_cves",
    "get_opencrq_cve",
    # MITRE ATT&CK reference data
    "list_opencrq_mitre_tactics",
    "search_opencrq_mitre_techniques",
    "get_opencrq_mitre_technique",
    "list_opencrq_mitre_mitigations",
    # Administration
    "list_opencrq_users",
    "get_opencrq_user",
    "get_opencrq_user_permissions",
    "list_opencrq_tenant_users",
    "update_opencrq_tenant_user_profile",
    "remove_opencrq_tenant_user",
    "create_opencrq_tenant",
    "delete_opencrq_tenants",
    "create_opencrq_organization",
    "update_opencrq_organization",
    "delete_opencrq_organizations",
    "list_opencrq_sectors",
    "list_opencrq_countries",
    "list_opencrq_groups",
    "get_opencrq_group",
    "create_opencrq_group",
    "update_opencrq_group",
    "delete_opencrq_group",
    "manage_opencrq_group_membership",
    "manage_opencrq_group_roles",
    "list_opencrq_roles",
    "get_opencrq_role",
    "create_opencrq_role",
    "update_opencrq_role",
    "delete_opencrq_role",
    "manage_opencrq_role_capabilities",
    "list_opencrq_capabilities",
    "list_opencrq_service_accounts",
    "get_opencrq_platform_rbac",
    # Background jobs
    "list_opencrq_jobs",
    "get_opencrq_job",
    "manage_opencrq_job",
}

#: Tools whose name marks them as write operations and that must carry the
#: confirm-with-the-user contract in their description.
_WRITE_PREFIXES = (
    "add_",
    "create_",
    "update_",
    "delete_",
    "remove_",
    "manage_",
    "reset_",
    "run_",
)


def _build():
    executor = CallableToolExecutor()
    tools = build_tools(TOKEN, executor)
    return executor, tools


def test_expected_tools_present():
    _, tools = _build()
    names = {t.name for t in tools}
    missing = EXPECTED_TOOLS - names
    assert not missing, f"missing tools: {missing}"


def test_no_duplicate_tool_names():
    _, tools = _build()
    names = [t.name for t in tools]
    assert len(names) == len(set(names)), "duplicate tool names registered"


def test_all_tool_schemas_are_valid():
    _, tools = _build()
    for tool in tools:
        assert tool.source == "integration:opencrq", tool.name
        schema = tool.input_schema
        assert schema.get("type") == "object", tool.name
        assert isinstance(schema.get("properties"), dict), tool.name
        json.dumps(schema)


def test_every_tool_has_a_handler():
    executor, tools = _build()
    for tool in tools:
        assert tool.name in executor._handlers, f"no handler for {tool.name}"


async def test_a_technique_given_by_its_attack_id_is_resolved_before_the_capability_is_changed():
    edge = {"techniqueId": "t-uuid-1", "function": "prevent", "source": "catalogue", "state": "recommended"}
    with _recorded_trpc(
        opencrq_frameworks,
        {
            "mitreAttack.getTechniqueByExternalId": {"id": "t-uuid-9", "externalId": "T1059", "name": "Command and Scripting Interpreter"},
            "controlCapabilities.getControlCapability": {"id": "c-1", "edges": [edge]},
        },
    ) as calls:
        out = await _run(
            "manage_opencrq_capability_techniques",
            action="add", capability_id="c-1", function="prevent", technique_id=" t1059 ",
        )
    assert [(c["procedure"], c["input"], c["mutation"]) for c in calls] == [
        ("mitreAttack.getTechniqueByExternalId", {"externalId": "T1059"}, False),
        ("controlCapabilities.getControlCapability", {"id": "c-1"}, False),
        (
            "controlCapabilities.addTechniqueOverride",
            {"capabilityId": "c-1", "techniqueId": "t-uuid-9", "function": "prevent"},
            True,
        ),
    ]
    assert "now covers T1059 Command and Scripting Interpreter on prevent" in out


async def test_adding_a_technique_the_catalogue_covers_is_refused_without_a_mutation():
    edge = {
        "techniqueId": "t-uuid-1", "techniqueExternalId": "T1059", "techniqueName": "Command and Scripting Interpreter",
        "function": "prevent", "source": "catalogue", "state": "recommended",
    }
    with _recorded_trpc(
        opencrq_frameworks,
        {
            "mitreAttack.getTechniqueByExternalId": {"id": "t-uuid-1", "externalId": "T1059", "name": "Command and Scripting Interpreter"},
            "controlCapabilities.getControlCapability": {"id": "c-1", "edges": [edge]},
        },
    ) as calls:
        out = await _run(
            "manage_opencrq_capability_techniques",
            action="add", capability_id="c-1", function="prevent", technique_id="T1059",
        )
    assert not any(c["mutation"] for c in calls)
    assert "already covered for prevent (recommended by the catalogue)" in out


async def test_an_unknown_attack_id_stops_before_the_capability_is_read():
    with _recorded_trpc(opencrq_frameworks) as calls:
        out = await _run(
            "manage_opencrq_capability_techniques",
            action="remove", capability_id="c-1", function="detect", technique_id="T9999",
        )
    assert [c["procedure"] for c in calls] == ["mitreAttack.getTechniqueByExternalId"]
    assert "MITRE technique T9999 not found." in out


def test_empty_base_url_returns_no_tools():
    executor = CallableToolExecutor()
    tools = build_tools("||token-only", executor)
    assert tools == []


# ── Dispatch smoke (early-return validation paths only — no network) ──────

SMOKE_CALLS = [
    ("get_opencrq_framework", {"framework_id": ""}),
    ("list_opencrq_framework_requirements", {"framework_id": ""}),
    ("get_opencrq_control_capability", {"capability_id": ""}),
    ("update_opencrq_control_capability_assessment", {"capability_id": "", "strength": "L2"}),
    ("update_opencrq_control_capability_assessment", {"capability_id": "x"}),
    ("update_opencrq_control_capability_assessment", {"capability_id": "x", "strength": "L4"}),
    ("update_opencrq_control_capability_assessment", {"capability_id": "x", "coverage": "3"}),
    (
        "update_opencrq_control_capability_assessment",
        {"capability_id": "x", "strength": "L2", "reattach": ["reliability"]},
    ),
    ("get_opencrq_compliance_assessment", {"assessment_id": ""}),
    ("update_opencrq_requirement_answer", {"assessment_id": "", "requirement_id": "r", "level": 3}),
    ("update_opencrq_requirement_answer", {"assessment_id": "a", "requirement_id": "r", "level": 9}),
    (
        "update_opencrq_requirement_answer",
        {"assessment_id": "a", "requirement_id": "r", "level": 3, "not_applicable": True},
    ),
    ("update_opencrq_requirement_answer", {"assessment_id": "a", "requirement_id": "r"}),
    ("update_opencrq_requirement_answer", {"assessment_id": "a", "requirement_id": "r", "due_date": "31/12/2026"}),
    ("update_opencrq_requirement_answer", {"assessment_id": "a", "requirement_id": "r", "due_date": "20261231"}),
    ("update_opencrq_requirement_answer", {"assessment_id": "a", "requirement_id": "r", "owner_id": "alice"}),
    ("manage_opencrq_scope_control_posture", {"action": "__bad__", "organization_id": "o", "capability_id": "c"}),
    ("manage_opencrq_scope_control_posture", {"action": "declare", "organization_id": "", "capability_id": "c", "strength": "L2"}),
    ("manage_opencrq_scope_control_posture", {"action": "declare", "organization_id": "o", "capability_id": "", "strength": "L2"}),
    ("manage_opencrq_scope_control_posture", {"action": "declare", "organization_id": "o", "capability_id": "c"}),
    ("manage_opencrq_scope_control_posture", {"action": "declare", "organization_id": "o", "capability_id": "c", "strength": "L4"}),
    ("manage_opencrq_scope_control_posture", {"action": "declare", "organization_id": "o", "capability_id": "c", "coverage": "L1", "dimension": "strength"}),
    ("manage_opencrq_scope_control_posture", {"action": "clear", "organization_id": "o", "capability_id": "c"}),
    ("manage_opencrq_scope_control_posture", {"action": "clear", "organization_id": "o", "capability_id": "c", "dimension": "comment"}),
    ("manage_opencrq_scope_control_posture", {"action": "clear", "organization_id": "o", "capability_id": "c", "dimension": "strength", "coverage": "L1"}),
    ("manage_opencrq_capability_techniques", {"action": "__bad__", "capability_id": "c", "function": "prevent"}),
    ("manage_opencrq_capability_techniques", {"action": "add", "capability_id": "", "function": "prevent", "technique_id": "T1059"}),
    ("manage_opencrq_capability_techniques", {"action": "add", "capability_id": "c", "function": "respond", "technique_id": "T1059"}),
    ("manage_opencrq_capability_techniques", {"action": "add", "capability_id": "c", "function": "", "technique_id": "T1059"}),
    ("manage_opencrq_capability_techniques", {"action": "add", "capability_id": "c", "function": "prevent"}),
    ("manage_opencrq_capability_techniques", {"action": "restore", "capability_id": "c", "function": "detect", "technique_id": " "}),
    ("manage_opencrq_capability_techniques", {"action": "reset", "capability_id": "c", "function": "prevent", "technique_id": "T1059"}),
    ("update_opencrq_control_capability_assessment", {"capability_id": "x", "reattach": ["scope"]}),
    (
        "update_opencrq_control_capability_assessment",
        {"capability_id": "x", "strength": "L2", "reattach": ["strength"]},
    ),
    ("update_opencrq_tef_input", {"risk_scenario_id": "s", "mode": "auto", "min_value": 1}),
    ("update_opencrq_tef_input", {"risk_scenario_id": "s", "min_value": 1, "likely_value": 2}),
    ("update_opencrq_tef_input", {"risk_scenario_id": "s", "mode": "__bad__"}),
    ("get_opencrq_risk_scenario", {"risk_scenario_id": ""}),
    ("create_opencrq_risk_scenario", {"organization_id": "", "name": ""}),
    ("update_opencrq_risk_scenario", {"risk_scenario_id": ""}),
    ("update_opencrq_risk_scenario", {"risk_scenario_id": "x"}),
    ("get_opencrq_financial_summary", {"risk_scenario_id": ""}),
    ("list_opencrq_primary_assets", {"organization_id": ""}),
    ("get_opencrq_supporting_asset", {"supporting_asset_id": ""}),
    ("get_opencrq_primary_asset", {"primary_asset_id": ""}),
    ("search_opencrq_cves", {"query": "ab"}),
    # Frameworks / controls (new)
    ("list_opencrq_drata_controls", {}),
    ("list_opencrq_drata_controls", {"framework_id": "f", "requirement_id": "r"}),
    ("get_opencrq_control_capability_history", {"capability_id": ""}),
    # Assessments (new)
    ("create_opencrq_compliance_assessment", {"framework_id": "", "name": "n"}),
    ("create_opencrq_compliance_assessment", {"framework_id": "f", "name": " "}),
    ("manage_opencrq_compliance_assessment", {"action": "__bad__", "assessment_id": "x"}),
    ("manage_opencrq_compliance_assessment", {"action": "publish", "assessment_id": ""}),
    ("manage_opencrq_compliance_assessment", {"action": "rename", "assessment_id": "x"}),
    # Risk (new)
    ("delete_opencrq_risk_scenarios", {"risk_scenario_ids": []}),
    ("get_opencrq_scenario_exposure", {"risk_scenario_id": ""}),
    ("get_opencrq_cve_exploitation", {"risk_scenario_id": "", "cve_id": "CVE-2024-3094"}),
    ("get_opencrq_cve_exploitation", {"risk_scenario_id": "s", "cve_id": "log4shell"}),
    ("get_opencrq_attack_path_reach", {"supporting_asset_id": ""}),
    ("get_opencrq_attack_path_reach", {"supporting_asset_id": "not-a-uuid"}),
    ("get_opencrq_attack_outcome", {"attack_outcome_id": ""}),
    ("update_opencrq_risk_tolerance", {}),
    ("update_opencrq_risk_tolerance", {"risk_scenario_id": "s", "organization_id": "o", "clear": True}),
    ("update_opencrq_risk_tolerance", {"risk_scenario_id": "s", "annual_loss": 1000}),
    ("update_opencrq_risk_tolerance", {"organization_id": "o", "clear": True}),
    ("update_opencrq_risk_tolerance", {"risk_scenario_id": "s", "annual_loss": 0, "max_exceedance_probability": 0.05}),
    ("update_opencrq_risk_tolerance", {"risk_scenario_id": "s", "annual_loss": 1000, "max_exceedance_probability": 5}),
    ("update_opencrq_risk_tolerance", {"risk_scenario_id": "s", "annual_loss": 1000, "max_exceedance_probability": 0.0001}),
    ("update_opencrq_risk_tolerance", {"risk_scenario_id": "s", "annual_loss": "lots", "max_exceedance_probability": 0.05}),
    ("update_opencrq_threat_relevance_settings", {}),
    ("update_opencrq_threat_relevance_settings", {"act_half_life_observed_days": 3}),
    ("update_opencrq_threat_relevance_settings", {"confidence_gate": 55}),
    (
        "update_opencrq_threat_relevance_settings",
        {"act_half_life_authored_days": "abc"},
    ),
    (
        "update_opencrq_threat_relevance_settings",
        {"act_half_life_observed_days": 400, "act_half_life_authored_days": 30},
    ),
    # CRQ (new)
    ("run_opencrq_crq_simulation", {"risk_scenario_id": ""}),
    ("get_opencrq_crq_run", {}),
    ("list_opencrq_crq_run_history", {"risk_scenario_id": ""}),
    ("get_opencrq_crq_what_if", {"risk_scenario_id": ""}),
    ("get_opencrq_tef_input", {"risk_scenario_id": ""}),
    ("update_opencrq_tef_input", {"risk_scenario_id": ""}),
    (
        "update_opencrq_tef_input",
        {
            "risk_scenario_id": "x",
            "min_value": 5,
            "likely_value": 2,
            "max_value": 10,
        },
    ),
    ("get_opencrq_loss_driver", {"loss_driver_id": ""}),
    ("update_opencrq_loss_estimates", {"risk_scenario_id": ""}),
    ("update_opencrq_loss_estimates", {"risk_scenario_id": "x", "estimates": []}),
    (
        "update_opencrq_loss_estimates",
        {"risk_scenario_id": "x", "estimates": [{"min_value": 1}]},
    ),
    (
        "update_opencrq_loss_estimates",
        {
            "risk_scenario_id": "x",
            "estimates": [
                {"loss_driver_id": "d", "min_value": 9, "likely_value": 1}
            ],
        },
    ),
    (
        # Non-numeric estimate values must early-return, not crash.
        "update_opencrq_loss_estimates",
        {
            "risk_scenario_id": "x",
            "estimates": [{"loss_driver_id": "d", "min_value": "abc"}],
        },
    ),
    ("update_opencrq_crq_settings", {}),
    ("update_opencrq_crq_settings", {"iterations": -5}),
    ("update_opencrq_crq_settings", {"iterations": 2001}),
    ("update_opencrq_crq_settings", {"susceptibility_gamma": 1.5}),
    ("update_opencrq_crq_settings", {"measured_validity_days": -1}),
    ("update_opencrq_crq_settings", {"unknown_criticality_counts_as": "unknown"}),
    ("update_opencrq_crq_settings", {"unknown_exposure_counts_as": "total"}),
    # Assets (new)
    ("create_opencrq_primary_asset", {"organization_id": "", "name": ""}),
    ("create_opencrq_primary_asset", {"organization_id": "o", "name": "n"}),
    ("update_opencrq_primary_asset", {"primary_asset_id": ""}),
    ("update_opencrq_primary_asset", {"primary_asset_id": "x"}),
    (
        "update_opencrq_primary_asset",
        {"primary_asset_id": "x", "business_value_category_ids": []},
    ),
    ("list_opencrq_supporting_assets", {"page": 0}),
    ("list_opencrq_supporting_assets", {"limit": 0}),
    ("delete_opencrq_primary_assets", {"primary_asset_ids": []}),
    ("create_opencrq_supporting_asset", {"name": ""}),
    ("create_opencrq_supporting_asset", {"name": "n", "category": "__bad__"}),
    (
        "create_opencrq_supporting_asset",
        {"name": "n", "category": "host", "exposure": "__bad__"},
    ),
    ("update_opencrq_supporting_asset", {"supporting_asset_id": ""}),
    ("update_opencrq_supporting_asset", {"supporting_asset_id": "x"}),
    (
        "update_opencrq_supporting_asset",
        {"supporting_asset_id": "x", "criticality": "__bad__"},
    ),
    (
        "create_opencrq_supporting_asset",
        {"name": "n", "category": "host", "subcategory": "firewall"},
    ),
    (
        "create_opencrq_supporting_asset",
        {"name": "n", "category": "generic_asset", "subcategory": "server"},
    ),
    (
        "create_opencrq_supporting_asset",
        {"name": "n", "category": "host", "properties": {"family": "identity"}},
    ),
    (
        "create_opencrq_supporting_asset",
        {"name": "n", "category": "host", "properties": {"department": "IT"}},
    ),
    (
        "create_opencrq_supporting_asset",
        {"name": "n", "category": "web_application", "properties": {"hostname": "h"}},
    ),
    (
        "update_opencrq_supporting_asset",
        {"supporting_asset_id": "x", "category": "host", "subcategory": "router"},
    ),
    (
        "update_opencrq_supporting_asset",
        {"supporting_asset_id": "x", "category": "identity", "properties": {"family": "machine"}},
    ),
    ("delete_opencrq_supporting_assets", {"supporting_asset_ids": []}),
    ("list_opencrq_asset_dependencies", {}),
    ("create_opencrq_asset_dependency", {"link_type": "__bad__"}),
    (
        "create_opencrq_asset_dependency",
        {"link_type": "support_to_primary", "roles": ["__bad__"]},
    ),
    (
        "create_opencrq_asset_dependency",
        {"link_type": "support_to_primary", "roles": ["unknown", "stores"]},
    ),
    ("create_opencrq_asset_dependency", {"link_type": "support_to_primary"}),
    (
        "create_opencrq_asset_dependency",
        {
            "link_type": "support_to_primary",
            "supporting_asset_id": "",
            "primary_asset_id": "p",
        },
    ),
    (
        "create_opencrq_asset_dependency",
        {
            "link_type": "support_to_support",
            "provider_asset_id": "",
            "dependent_asset_id": "d",
        },
    ),
    (
        "create_opencrq_asset_dependency",
        {
            "link_type": "support_to_support",
            "provider_asset_id": "a",
            "dependent_asset_id": "a",
        },
    ),
    (
        "create_opencrq_asset_dependency",
        {
            "link_type": "support_to_support",
            "provider_asset_id": "p",
            "dependent_asset_id": "d",
            "roles": ["stores"],
        },
    ),
    ("update_opencrq_asset_dependency_roles", {"roles": ["stores"]}),
    ("update_opencrq_asset_dependency_roles", {"link_ids": ["l-1"]}),
    ("update_opencrq_asset_dependency_roles", {"link_ids": ["l-1"], "roles": ["__bad__"]}),
    ("update_opencrq_asset_dependency_roles", {"link_ids": ["l-1"], "roles": ["unknown", "stores"]}),
    ("update_opencrq_asset_dependency_roles", {"link_ids": [f"l-{n}" for n in range(501)], "roles": ["stores"]}),
    ("delete_opencrq_asset_dependencies", {"link_type": "__bad__", "link_ids": []}),
    (
        "delete_opencrq_asset_dependencies",
        {"link_type": "support_to_primary", "link_ids": []},
    ),
    ("create_opencrq_asset_evidence", {"supporting_asset_id": ""}),
    ("update_opencrq_evidence_state", {"evidence_id": ""}),
    ("update_opencrq_evidence_state", {"evidence_id": "x", "state": "__bad__"}),
    ("update_opencrq_evidence_state", {"evidence_id": "x", "state": "dismissed"}),
    (
        "update_opencrq_evidence_state",
        {
            "evidence_id": "x",
            "state": "dismissed",
            "dismissal_type": "false_positive",
            "reason_code": "wont_fix",
            "reason": "r",
        },
    ),
    (
        "update_opencrq_evidence_state",
        {
            "evidence_id": "x",
            "state": "dismissed",
            "dismissal_type": "risk_accepted",
            "reason_code": "wont_fix",
        },
    ),
    ("update_opencrq_evidence_state", {"evidence_id": "x", "state": "applicable", "reason": "r"}),
    ("delete_opencrq_asset_evidence", {"evidence_id": ""}),
    ("reset_opencrq_evidence_state", {"evidence_id": ""}),
    ("reset_opencrq_supporting_asset_to_connectors", {"supporting_asset_id": ""}),
    ("get_opencrq_evidence", {"evidence_id": ""}),
    ("list_opencrq_cve_evidences", {"cve_id": ""}),
    ("list_opencrq_cve_evidences", {"cve_id": "CVE-2024-1", "page": 0}),
    ("list_opencrq_unique_findings", {"page": 0}),
    ("list_opencrq_unique_findings", {"limit": 1000}),
    ("list_opencrq_unique_findings", {"source": "__bad__"}),
    ("list_opencrq_unique_findings", {"sort_by": "__bad__"}),
    ("list_opencrq_cves", {"page": 0}),
    ("list_opencrq_cves", {"page": 1, "limit": 1000}),
    ("get_opencrq_cve", {"cve_id": ""}),
    # MITRE (new)
    ("get_opencrq_mitre_technique", {"technique_id": ""}),
    # Administration (new)
    ("get_opencrq_user", {"user_id": ""}),
    ("get_opencrq_user_permissions", {"user_id": ""}),
    ("update_opencrq_tenant_user_profile", {"user_id": ""}),
    ("update_opencrq_tenant_user_profile", {"user_id": "x"}),
    ("remove_opencrq_tenant_user", {"user_id": ""}),
    ("create_opencrq_tenant", {"name": ""}),
    ("delete_opencrq_tenants", {"tenant_ids": []}),
    ("create_opencrq_organization", {"name": ""}),
    ("update_opencrq_organization", {"organization_id": ""}),
    ("update_opencrq_organization", {"organization_id": "x"}),
    ("update_opencrq_organization", {"organization_id": "x", "revenue_band": "__bad__"}),
    ("update_opencrq_organization", {"organization_id": "x", "employee_count": -1}),
    ("update_opencrq_organization", {"organization_id": "x", "employee_count": "twelve"}),
    ("update_opencrq_organization", {"organization_id": "x", "reaction_time_min_hours": 1}),
    (
        "update_opencrq_organization",
        {
            "organization_id": "x", "reaction_time_min_hours": 5,
            "reaction_time_likely_hours": 4, "reaction_time_max_hours": 12,
        },
    ),
    ("update_opencrq_organization", {"organization_id": "x", "estate_mode": "half"}),
    ("create_opencrq_organization", {"name": "n", "reaction_time_likely_hours": 4}),
    ("delete_opencrq_organizations", {"organization_ids": []}),
    ("get_opencrq_group", {"group_id": ""}),
    ("create_opencrq_group", {"name": ""}),
    ("update_opencrq_group", {"group_id": "", "name": ""}),
    ("delete_opencrq_group", {"group_id": ""}),
    ("manage_opencrq_group_membership", {"action": "__bad__"}),
    (
        "manage_opencrq_group_membership",
        {"action": "add", "group_id": "", "user_id": ""},
    ),
    ("manage_opencrq_group_roles", {"action": "__bad__"}),
    (
        "manage_opencrq_group_roles",
        {"action": "remove", "group_id": "", "role_id": ""},
    ),
    ("get_opencrq_role", {"role_id": ""}),
    ("create_opencrq_role", {"name": ""}),
    ("update_opencrq_role", {"role_id": "", "name": ""}),
    ("delete_opencrq_role", {"role_id": ""}),
    ("manage_opencrq_role_capabilities", {"action": "__bad__"}),
    (
        "manage_opencrq_role_capabilities",
        {"action": "add", "role_id": "", "capability_id": ""},
    ),
    # Background jobs (new)
    ("list_opencrq_jobs", {"statuses": ["__bad__"]}),
    ("list_opencrq_jobs", {"limit": 0}),
    ("list_opencrq_jobs", {"limit": 201}),
    ("list_opencrq_jobs", {"offset": -1}),
    ("get_opencrq_job", {"job_id": ""}),
    ("manage_opencrq_job", {"action": "__bad__"}),
    ("manage_opencrq_job", {"action": "cancel", "job_id": ""}),
]


async def test_dispatch_smoke_early_returns():
    executor, _ = _build()
    for idx, (name, args) in enumerate(SMOKE_CALLS):
        result = await executor.execute_tool(
            ToolCall(id=f"call-{idx}", name=name, arguments=args)
        )
        assert not result.is_error, f"{name} raised: {result.content}"
        assert isinstance(result.content, str) and result.content, name


# ── Write / delete contract language ───────────────────────────────────────


def test_write_tools_carry_confirmation_language():
    """Every write tool must instruct the agent to confirm with the user."""
    _, tools = _build()
    for tool in tools:
        if tool.name.startswith(_WRITE_PREFIXES):
            assert "confirm" in tool.description.lower(), (
                f"{tool.name} is a write tool but its description lacks the "
                "confirm-with-the-user contract"
            )


def test_delete_tools_carry_irreversibility_language():
    """Every delete tool must state that the operation is irreversible."""
    _, tools = _build()
    delete_tools = [t for t in tools if t.name.startswith("delete_opencrq")]
    assert delete_tools, "expected delete tools to exist"
    for tool in delete_tools:
        assert "irreversible" in tool.description.lower(), (
            f"{tool.name} is a delete tool but its description lacks "
            "irreversibility language"
        )


# ── Conditional schema requirements ────────────────────────────────────────


def _schema_errors(name: str, args: dict) -> list:
    """Validate *args* against a tool's input_schema like the agent loop does."""
    _, tools = _build()
    schema = next(t.input_schema for t in tools if t.name == name)
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    return list(validator_cls(schema).iter_errors(args))


def test_asset_dependency_schema_enforces_per_link_type_ids():
    """The schema must reject calls missing the link_type-specific IDs."""
    assert _schema_errors(
        "create_opencrq_asset_dependency", {"link_type": "support_to_primary"}
    ), "support_to_primary without asset IDs must fail schema validation"
    assert not _schema_errors(
        "create_opencrq_asset_dependency",
        {
            "link_type": "support_to_primary",
            "supporting_asset_id": "s",
            "primary_asset_id": "p",
            "roles": ["stores", "processes"],
        },
    )
    assert _schema_errors(
        "create_opencrq_asset_dependency",
        {
            "link_type": "support_to_primary",
            "supporting_asset_id": "s",
            "primary_asset_id": "p",
            "roles": ["__bad__"],
        },
    ), "a role outside the link roles must fail schema validation"
    assert _schema_errors(
        "create_opencrq_asset_dependency",
        {"link_type": "support_to_support", "provider_asset_id": "p"},
    ), "support_to_support without dependent_asset_id must fail schema validation"
    assert not _schema_errors(
        "create_opencrq_asset_dependency",
        {
            "link_type": "support_to_support",
            "provider_asset_id": "p",
            "dependent_asset_id": "d",
        },
    )


def test_anyof_selector_schemas_require_at_least_one_field():
    """Tools with either-or selectors must reject empty calls in-schema."""
    assert _schema_errors("list_opencrq_asset_dependencies", {})
    assert not _schema_errors(
        "list_opencrq_asset_dependencies", {"supporting_asset_id": "s"}
    )
    assert not _schema_errors(
        "list_opencrq_asset_dependencies", {"primary_asset_id": "p"}
    )
    assert _schema_errors("get_opencrq_crq_run", {})
    assert not _schema_errors("get_opencrq_crq_run", {"risk_scenario_id": "r"})
    assert not _schema_errors("get_opencrq_crq_run", {"run_id": "x"})
    update = "update_opencrq_control_capability_assessment"
    assert _schema_errors(update, {"capability_id": "c"})
    assert not _schema_errors(update, {"capability_id": "c", "coverage": "L2"})
    assert _schema_errors(update, {"capability_id": "c", "strength": "L4"})


def test_the_assessment_tools_refuse_a_reliability():
    """A model that still sends a reliability is told so, rather than having it dropped."""
    assert _schema_errors(
        "update_opencrq_control_capability_assessment",
        {"capability_id": "c", "strength": "L1", "reliability": "L2"},
    )
    assert _schema_errors(
        "manage_opencrq_scope_control_posture",
        {
            "action": "declare",
            "organization_id": "o",
            "capability_id": "c",
            "strength": "L1",
            "reliability": "L2",
        },
    )


def test_evidence_state_schema_requires_type_code_and_reason_when_dismissed():
    """The schema must reject dismissals that miss what OpenCRQ requires."""
    dismissed = {"evidence_id": "e", "state": "dismissed"}
    assert _schema_errors("update_opencrq_evidence_state", dismissed)
    assert _schema_errors(
        "update_opencrq_evidence_state", {**dismissed, "reason": "obsolete"}
    )
    assert not _schema_errors(
        "update_opencrq_evidence_state",
        {
            **dismissed,
            "dismissal_type": "false_positive",
            "reason_code": "detection_error",
            "reason": "obsolete",
        },
    )
    assert not _schema_errors(
        "update_opencrq_evidence_state",
        {"evidence_id": "e", "state": "applicable"},
    )


def test_threat_relevance_update_schema_requires_one_setting():
    update = "update_opencrq_threat_relevance_settings"
    assert _schema_errors(update, {})
    assert not _schema_errors(update, {"confidence_gate": 80})
    assert _schema_errors(update, {"confidence_gate": 55})
    assert _schema_errors(update, {"act_half_life_observed_days": 3})


# ── ID-list normalisation ──────────────────────────────────────────────────


def test_clean_ids_drops_none_blank_and_whitespace():
    """None entries must never become the literal string "None"."""
    assert _clean_ids(None) == []
    assert _clean_ids([]) == []
    assert _clean_ids(["a", None, "", "  ", " b "]) == ["a", "b"]


# ── UI link helpers ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("entity_type", "path"),
    [
        # Each path is a route of OpenCRQ's front
        # (front/src/routes/_auth/$tenantId/{-$organizationId}/...).
        ("framework", "/compliance/abc"),
        ("control", "/controls/abc/overview"),
        ("organization", "/abc/details/overview"),
        ("supporting_asset", "/assets/abc/overview"),
        ("loss_driver", "/settings/taxonomies/loss-drivers/abc"),
        ("cve", "/settings/taxonomies/cve/abc"),
        ("mitre_technique", "/settings/taxonomies/mitre-attack/techniques/abc/overview"),
        ("mitre_mitigation", "/settings/taxonomies/mitre-attack/mitigations/abc"),
        ("user", "/settings/security/users/abc"),
        ("service_account", "/settings/security/service-accounts/abc"),
    ],
)
def test_grc_link_known_entity(entity_type, path):
    link = _grc_link("https://grc.local", "tenant-1", entity_type, "abc")
    assert link == (
        f"  Link: [Open in OpenCRQ](https://grc.local/tenant-1{path})"
    )


@pytest.mark.parametrize(
    "entity_type",
    # No tool shows a tactic on its own; primary asset families have no
    # page any more.
    ["mitre_tactic", "primary_asset_family"],
)
def test_grc_link_none_for_entities_without_a_matching_page(entity_type):
    assert _grc_link("https://grc.local", "t", entity_type, "abc") == ""


def test_fmt_cve_links_the_cve_by_its_name_not_its_uuid():
    cve = {"id": "0b9e5c1e-uuid", "cveId": "CVE-2024-3400", "cvss": 10, "epss": 0.97, "kev": True}
    out = _fmt_cve(cve, "https://grc.local", "t1")
    assert (
        "  Link: [Open in OpenCRQ]"
        "(https://grc.local/t1/settings/taxonomies/cve/CVE-2024-3400)"
    ) in out
    assert "CVSS: 10" in out
    assert "Link:" not in _fmt_cve({"id": "0b9e5c1e-uuid"}, "https://grc.local", "t1")


def test_grc_link_unknown_entity_or_missing_parts():
    assert _grc_link("https://grc.local", "t", "unknown_type", "abc") == ""
    assert _grc_link("", "t", "control", "abc") == ""
    assert _grc_link("https://grc.local", "", "control", "abc") == ""
    assert _grc_link("https://grc.local", "t", "control", "") == ""
    assert _grc_link("https://grc.local", "t", "control", "N/A") == ""


def test_grc_links_only_plain_path_segments():
    # A ")" would end the Markdown link, a "/" or " " change the path, a
    # leading dot or ".." climb out of it: those ids get no link at all.
    for bad in ("CVE-1) x", "CVE-1)", "a b", "a/b", ".hidden", "..", "a/../b"):
        assert _grc_link("https://grc.local", "t1", "cve", bad) == "", bad
        assert _grc_link("https://grc.local", "t1", "cve", "CVE-2024-3400", organization_id=bad) == ""
        assert _grc_scenario_link("https://grc.local", "t1", bad, "s1") == "", bad
        assert _grc_scenario_link("https://grc.local", "t1", "org1", bad) == "", bad
        assert _grc_primary_asset_link("https://grc.local", "t1", bad, "a1") == "", bad
        assert _grc_primary_asset_link("https://grc.local", "t1", "org1", bad) == "", bad
    assert _grc_link("https://grc.local", "t 1", "cve", "CVE-2024-3400") == ""
    # A CVE name, an ATT&CK sub-technique id and a UUID still link.
    assert _grc_link("https://grc.local", "t1", "cve", "CVE-2024-3400").endswith(
        "/settings/taxonomies/cve/CVE-2024-3400)"
    )
    assert _grc_link("https://grc.local", "t1", "mitre_technique", "T1566.001").endswith(
        "/mitre-attack/techniques/T1566.001/overview)"
    )
    uuid_ = "0192e4c0-7b1e-7c3a-9d52-3f6a1b2c4d5e"
    assert _grc_link("https://grc.local", "t1", "control", uuid_).endswith(
        f"/controls/{uuid_}/overview)"
    )


def test_grc_scenario_link():
    link = _grc_scenario_link("https://grc.local", "t1", "org1", "s1")
    assert link == (
        "  Link: [Open in OpenCRQ]"
        "(https://grc.local/t1/org1/risk-scenarios/s1/overview)"
    )
    assert _grc_scenario_link("https://grc.local", "t1", None, "s1") == ""
    assert _grc_scenario_link("https://grc.local", "t1", "org1", None) == ""
    # The "N/A" sentinel used by the formatters must never produce a link.
    assert _grc_scenario_link("https://grc.local", "t1", "org1", "N/A") == ""
    assert _grc_scenario_link("https://grc.local", "t1", "N/A", "s1") == ""


def test_grc_primary_asset_link():
    link = _grc_primary_asset_link("https://grc.local", "t1", "org1", "a1")
    assert link == (
        "  Link: [Open in OpenCRQ](https://grc.local/t1/org1/primary-assets/a1)"
    )
    assert _grc_primary_asset_link("https://grc.local", "t1", None, "a1") == ""
    assert _grc_primary_asset_link("https://grc.local", "t1", "org1", "N/A") == ""


# ── Tenant resolution edge cases ──────────────────────────────────────────


def _patch_tenants(tenants):
    async def _fake_trpc(*args, **kwargs):
        return tenants, None

    return patch.object(opencrq_common, "_trpc", _fake_trpc)


async def test_resolve_tenant_sole_tenant():
    with _patch_tenants([{"id": "t-1", "name": "Acme"}]):
        tenant, err = await _resolve_tenant("https://grc.local", {}, "")
    assert (tenant, err) == ("t-1", None)


async def test_resolve_tenant_sole_tenant_without_id_is_an_error():
    """A blank sole-tenant id must surface an explicit error, never an
    empty tenant that silently drops the x-tenant-id header."""
    for tenants in ([{"name": "Acme"}], [{"id": None}], [{"id": "  "}]):
        with _patch_tenants(tenants):
            tenant, err = await _resolve_tenant("https://grc.local", {}, "")
        assert tenant is None
        assert err and "without an id" in err


# ── Formatters ─────────────────────────────────────────────────────────────


def test_fmt_assessment_list_row_shows_answers_and_levels():
    out = _fmt_assessment(
        {
            "id": "a-1",
            "name": "NIST CSF 2.0 T4",
            "status": "draft",
            "frameworkId": "f-1",
            "frameworkTitle": "NIST CSF 2.0",
            "createdAt": "2026-10-12T09:00:00.459Z",
            "updatedAt": "2026-10-13T09:00:00.459Z",
            "maturity": {
                "requirementCount": 106,
                "answeredCount": 41,
                "notApplicableCount": 2,
                "meanLevel": 2.048780487804878,
                "distribution": [5, 7, 13, 13, 3, 0],
            },
        }
    )
    assert "41 of 106 requirements, 2 not applicable" in out
    assert "Mean level (0-5): 2.05" in out
    assert "5, 7, 13, 13, 3, 0" in out
    assert "Status: draft" in out
    assert "None" not in out


def test_fmt_assessment_detail_computes_the_summary_from_nodes_and_answers():
    out = _fmt_assessment(
        {
            "id": "a-1",
            "name": "NIST CSF 2.0 T2",
            "status": "published",
            "frameworkId": "f-1",
            "frameworkTitle": "NIST CSF 2.0",
            "createdAt": "2026-06-16T09:00:00.459Z",
            "publishedAt": "2026-07-16T09:00:00.459Z",
            "createdBy": {"id": "u-1", "name": None, "email": "bob@example.com"},
            "publishedByServiceAccount": {"id": "s-1", "name": "Sync Bot"},
            "nodes": [
                {"id": "n-0", "isRequirement": False},
                {"id": "n-1", "isRequirement": True},
                {"id": "n-2", "isRequirement": True},
                {"id": "n-3", "isRequirement": True},
            ],
            "answers": [
                {"requirementId": "n-1", "level": 0, "notApplicable": False},
                {"requirementId": "n-2", "level": None, "notApplicable": True},
            ],
        }
    )
    assert "1 of 3 requirements, 1 not applicable" in out
    assert "Mean level (0-5): 0.00" in out
    assert "Created: 2026-06-16 by bob@example.com" in out
    assert "Published: 2026-07-16 by Sync Bot (service account)" in out
    assert "None" not in out


def test_fmt_control_shows_each_axis_with_its_mode_and_the_last_assessment():
    out = _fmt_control(
        {
            "id": "c-1",
            "externalId": "EHB",
            "name": "Endpoint Hardening Baseline",
            "assessment": {
                "strengthRung": "L1",
                "coverageBucket": "L2",
                "strengthMode": "auto",
                "coverageMode": "auto",
                "coverageBasis": "measured",
                "comment": "Scanner results.",
            },
            "lastAssessedAt": "2026-10-14T12:56:14.670Z",
            "lastAssessedBy": None,
            "lastAssessedConnector": "derivation",
            "edges": [
                {"function": "prevent", "state": "recommended"},
                {"function": "detect", "state": "removed"},
            ],
        },
        "https://grc.local",
        "t-1",
    )
    assert "[ControlCapability] Endpoint Hardening Baseline (EHB)" in out
    assert "Functions: prevent\n" in out + "\n" and "Functions: detect" not in out
    assert "Strength: L1 (auto); coverage: L2 (auto, measured)\n" in out + "\n"
    assert "Last assessed: 2026-10-14 by derivation" in out
    assert "Comment: Scanner results." in out
    assert "Link: [Open in OpenCRQ]" in out
    assert "None" not in out


def test_fmt_control_without_assessment_is_not_assessed():
    out = _fmt_control(
        {"id": "c-2", "externalId": "FW", "name": "Firewall", "assessment": None}
    )
    assert "Assessment: not assessed" in out
    assert "Strength" not in out


def test_fmt_control_declared_axes_name_their_author():
    out = _fmt_control(
        {
            "id": "c-3",
            "externalId": "MFA",
            "name": "Multi-Factor Authentication",
            "assessment": {
                "strengthRung": "L3",
                "coverageBucket": "L2",
                "strengthMode": "manual",
                "coverageMode": "manual",
                "coverageBasis": "declared",
                "comment": None,
            },
            "lastAssessedAt": "2026-05-24T04:29:59.203Z",
            "lastAssessedBy": {"userId": "u-1", "name": "Platform Admin"},
            "lastAssessedConnector": None,
        }
    )
    assert "Strength: L3 (manual); coverage: L2 (manual)\n" in out + "\n"
    assert "Last assessed: 2026-05-24 by Platform Admin" in out
    assert "Comment" not in out


def test_fmt_job_row_shows_a_progress_of_zero_expected():
    row = {"kind": "cve-epss-sync", "status": "running", "id": "j-1", "processedTotal": 0}
    assert "progress 0/0" in _fmt_job_row({**row, "expectedTotal": 0})
    assert "progress" not in _fmt_job_row({**row, "expectedTotal": None})


# ── Write contracts: what each tool sends, with the model's own vocabulary ──


@contextlib.contextmanager
def _recorded_trpc(module, responses: dict | None = None):
    """Patch ``_trpc`` in *module* and in ``_common``; yield the recorded calls."""
    calls: list[dict] = []
    answers = responses or {}

    async def fake(
        base_url, headers, procedure, action, *, input=None, mutation=False,
        tenant_id="", client=None,
    ):
        calls.append(
            {"procedure": procedure, "input": input, "mutation": mutation}
        )
        return answers.get(procedure), None

    with patch.object(module, "_trpc", fake), patch.object(
        opencrq_common, "_trpc", fake
    ):
        yield calls


async def _execute(tool: str, /, **arguments):
    executor, _ = _build()
    return await executor.execute_tool(
        ToolCall(id="call-1", name=tool, arguments={"tenant_id": "t-1", **arguments})
    )


async def _run(tool: str, /, **arguments) -> str:
    result = await _execute(tool, **arguments)
    assert not result.is_error, result.content
    return result.content


def _tool_properties(name: str) -> dict:
    _, tools = _build()
    return next(t.input_schema["properties"] for t in tools if t.name == name)


async def test_risk_scenario_update_sends_only_keys_opencrq_accepts():
    with _recorded_trpc(opencrq_risk) as calls:
        await _run(
            "update_opencrq_risk_scenario",
            risk_scenario_id="s-1",
            name="Renamed",
            primary_asset_ids=["a-1", None, " a-2 "],
        )
    assert calls == [
        {
            "procedure": "riskScenarios.updateRiskScenario",
            "input": {"id": "s-1", "name": "Renamed", "primaryAssetIds": ["a-1", "a-2"]},
            "mutation": True,
        }
    ]
    props = _tool_properties("update_opencrq_risk_scenario")
    assert "attack_outcome_id" not in props and "confidence" not in props


async def test_risk_scenario_creation_sends_no_confidence():
    with _recorded_trpc(opencrq_risk) as calls:
        await _run(
            "create_opencrq_risk_scenario",
            organization_id="o-1",
            name="Ransomware",
            attack_outcome_id="ao-1",
        )
    sent = calls[0]["input"]
    assert calls[0]["procedure"] == "riskScenarios.createRiskScenario"
    assert "confidence" not in sent
    assert sent["attackOutcomeId"] == "ao-1" and sent["primaryAssetIds"] == []
    assert "confidence" not in _tool_properties("create_opencrq_risk_scenario")


async def test_loss_estimates_carry_the_fingerprint_of_the_rows_read():
    rows = [
        {"lossDriverId": "d-1", "updatedAt": "2026-04-17T08:59:57.625Z"},
        {"lossDriverId": "d-2", "updatedAt": "2026-05-01T10:00:00.000Z"},
    ]
    with _recorded_trpc(
        opencrq_crq, {"scenarioLossEstimates.getScenarioLossEstimates": rows}
    ) as calls:
        out = await _run(
            "update_opencrq_loss_estimates",
            risk_scenario_id="s-1",
            estimates=[
                {"loss_driver_id": "d-1", "min_value": 1, "likely_value": 2, "max_value": 3},
                {"loss_driver_id": "d-2", "action": "clear"},
            ],
        )
    assert calls[1] == {
        "procedure": "scenarioLossEstimates.upsertScenarioLossEstimates",
        "mutation": True,
        "input": {
            "riskScenarioId": "s-1",
            "estimates": [
                {
                    "lossDriverId": "d-1", "action": "set",
                    "minValue": 1.0, "likelyValue": 2.0, "maxValue": 3.0,
                },
                {"lossDriverId": "d-2", "action": "clear"},
            ],
            "updatedAt": "2026-05-01T10:00:00.000Z",
            "rowCount": 2,
        },
    }
    assert "Set 1, cleared 1 and reverted 0" in out


async def test_a_reverted_loss_estimate_goes_back_to_its_derived_figures():
    with _recorded_trpc(opencrq_crq, {"scenarioLossEstimates.getScenarioLossEstimates": []}) as calls:
        out = await _run(
            "update_opencrq_loss_estimates",
            risk_scenario_id="s-1", estimates=[{"loss_driver_id": "d-3", "action": "revert"}],
        )
    # scenario-loss-estimates-router.ts: revertLossDriverIds, beside the entries and the fingerprint
    assert calls[1]["input"] == {
        "riskScenarioId": "s-1", "estimates": [], "updatedAt": None, "rowCount": 0,
        "revertLossDriverIds": ["d-3"],
    }
    assert "reverted 1" in out


async def test_a_tef_read_follows_the_scenario_mode():
    estimate = {"min": 0.0087, "likely": 0.153, "max": 2.71, "fallbackReason": None,
                "createdAt": "2026-10-14T12:00:00.000Z"}
    with _recorded_trpc(
        opencrq_crq,
        {"crqIndicators.getTefMode": "auto", "crqIndicators.getAutoTefEstimate": estimate},
    ) as calls:
        out = await _run("get_opencrq_tef_input", risk_scenario_id="s-1")
    assert [c["procedure"] for c in calls] == [
        "crqIndicators.getTefMode", "crqIndicators.getAutoTefEstimate",
    ]
    assert "automatic estimate, min 0.0087 / likely 0.153 / max 2.71 events per year" in out
    manual = {"min": 1, "likely": 2, "max": 4, "updatedAt": "2026-10-01T00:00:00.000Z"}
    with _recorded_trpc(
        opencrq_crq, {"crqIndicators.getTefMode": "manual", "crqIndicators.getTefInput": manual}
    ) as calls:
        out = await _run("get_opencrq_tef_input", risk_scenario_id="s-1")
    assert calls[1]["procedure"] == "crqIndicators.getTefInput"
    assert "manual, min 1 / likely 2 / max 4 events per year (updated 2026-10-01)" in out


async def test_a_tef_set_by_hand_switches_to_manual_before_writing_the_values():
    with _recorded_trpc(opencrq_crq) as calls:
        await _run(
            "update_opencrq_tef_input", risk_scenario_id="s-1",
            min_value=1, likely_value=2, max_value=4,
        )
        await _run("update_opencrq_tef_input", risk_scenario_id="s-1", mode="auto")
    # Switching to manual copies the automatic estimate over the stored values: it must come first.
    assert [(c["procedure"], c["input"]) for c in calls] == [
        ("crqIndicators.setTefMode", {"riskScenarioId": "s-1", "tefMode": "manual"}),
        (
            "crqIndicators.upsertTefInput",
            {"riskScenarioId": "s-1", "min": 1, "likely": 2, "max": 4},
        ),
        ("crqIndicators.setTefMode", {"riskScenarioId": "s-1", "tefMode": "auto"}),
    ]


async def test_a_scenario_owner_is_set_and_removed():
    with _recorded_trpc(opencrq_risk) as calls:
        await _run("update_opencrq_risk_scenario", risk_scenario_id="s-1", owner_id="u-1")
        await _run("update_opencrq_risk_scenario", risk_scenario_id="s-1", owner_id="")
    assert calls[0]["input"] == {"id": "s-1", "ownerId": "u-1"}
    assert calls[1]["input"] == {"id": "s-1", "ownerId": None}


async def test_loss_estimates_on_a_scenario_without_rows_send_a_null_date():
    with _recorded_trpc(opencrq_crq, {}) as calls:
        await _run(
            "update_opencrq_loss_estimates",
            risk_scenario_id="s-1",
            estimates=[{"loss_driver_id": "d-1", "likely_value": 5}],
        )
    sent = calls[1]["input"]
    assert sent["updatedAt"] is None and sent["rowCount"] == 0
    assert sent["estimates"][0]["minValue"] is None


async def test_asset_dependency_creation_sends_an_array_with_roles():
    with _recorded_trpc(
        opencrq_assets,
        {"assetDependencies.createSupportToPrimaryLinks": {"skippedIds": [], "goneIds": []}},
    ) as calls:
        out = await _run(
            "create_opencrq_asset_dependency",
            link_type="support_to_primary",
            supporting_asset_id="s-1",
            primary_asset_id="p-1",
            roles=["Stores", "processes", "stores"],
        )
    assert calls[0]["procedure"] == "assetDependencies.createSupportToPrimaryLinks"
    (link,) = calls[0]["input"]
    assert link["roles"] == ["stores", "processes"]
    assert link["supportingAssetId"] == "s-1" and link["primaryAssetId"] == "p-1"
    assert f"id: {link['id']}" in out


async def test_asset_dependency_creation_reports_a_link_that_was_not_written():
    async def gone(base_url, headers, procedure, action, *, input=None, **_kw):
        return {"goneIds": [input[0]["id"]]}, None

    async def skipped(base_url, headers, procedure, action, *, input=None, **_kw):
        return {"skippedIds": [input[0]["id"]], "goneIds": []}, None

    with patch.object(opencrq_assets, "_trpc", gone):
        result = await _execute(
            "create_opencrq_asset_dependency",
            link_type="support_to_support",
            provider_asset_id="a",
            dependent_asset_id="b",
        )
    assert result.is_error and "no link was created" in result.content

    with patch.object(opencrq_assets, "_trpc", skipped):
        out = await _run(
            "create_opencrq_asset_dependency",
            link_type="support_to_primary",
            supporting_asset_id="s",
            primary_asset_id="p",
        )
    assert out.startswith("No link created")


async def test_asset_dependency_roles_update_lists_the_links_that_are_gone():
    with _recorded_trpc(
        opencrq_assets,
        {"assetDependencies.updateSupportToPrimaryLinks": {"skippedIds": ["l-2"]}},
    ) as calls:
        out = await _run(
            "update_opencrq_asset_dependency_roles",
            link_ids=["l-1", " l-2 ", "l-1", "l-3"],
            roles=["Stores", "reads", "stores"],
        )
    assert calls == [
        {
            "procedure": "assetDependencies.updateSupportToPrimaryLinks",
            "input": [{"id": f"l-{n}", "roles": ["stores", "reads"]} for n in (1, 2, 3)],
            "mutation": True,
        }
    ]
    assert "Updated 2 support_to_primary dependency link(s) to roles stores, reads." in out
    assert "Skipped 1 gone link(s) (deleted or not visible): l-2." in out


async def test_primary_asset_creation_sends_business_value_categories():
    with _recorded_trpc(opencrq_assets) as calls:
        await _run(
            "create_opencrq_primary_asset",
            organization_id="o-1",
            name="Payments",
            business_value_category_ids=["c-1", " c-2 "],
        )
    sent = calls[0]["input"]
    assert calls[0]["procedure"] == "primaryAssets.createPrimaryAsset"
    assert sent["businessValueCategoryIds"] == ["c-1", "c-2"]
    assert "familyIds" not in sent and "categoryIds" not in sent
    for name in ("create_opencrq_primary_asset", "update_opencrq_primary_asset"):
        props = _tool_properties(name)
        assert "family_ids" not in props and "category_ids" not in props


async def test_primary_asset_update_sends_native_fields_and_keeps_what_is_omitted() -> None:
    with _recorded_trpc(opencrq_assets) as calls:
        await _run(
            "update_opencrq_primary_asset",
            primary_asset_id="p-1",
            availability="high",
            integrity="HIGH",
            confidentiality="very_high",
            traceability="medium",
            business_function=" Card payments ",
            rto_minutes=240,
        )
        await _run("update_opencrq_primary_asset", primary_asset_id="p-1", rto_minutes=60)
        await _run("update_opencrq_primary_asset", primary_asset_id="p-1", availability=None, reference="")
    assert calls[0]["procedure"] == "primaryAssets.updatePrimaryAsset"
    assert calls[0]["input"] == {
        "id": "p-1",
        "availability": "high",
        "integrity": "high",
        "confidentiality": "very_high",
        "traceability": "medium",
        "businessFunction": "Card payments",
        "rtoMinutes": 240,
    }
    # Only what is passed is sent: OpenCRQ leaves an omitted key unchanged.
    assert calls[1]["input"] == {"id": "p-1", "rtoMinutes": 60}
    # null (or an empty string) is sent as null, which clears the field.
    assert calls[2]["input"] == {"id": "p-1", "availability": None, "reference": None}


async def test_primary_asset_creation_sends_native_fields_only_when_given() -> None:
    with _recorded_trpc(opencrq_assets) as calls:
        await _run(
            "create_opencrq_primary_asset",
            organization_id="o-1",
            name="Payments",
            business_value_category_ids=["c-1"],
            reference="PA-01",
            confidentiality="very_high",
            mtpd_minutes=1440,
        )
    sent = calls[0]["input"]
    assert sent["reference"] == "PA-01"
    assert sent["confidentiality"] == "very_high"
    assert sent["mtpdMinutes"] == 1440
    assert not {"availability", "integrity", "traceability", "rtoMinutes", "businessFunction"} & set(sent)


@pytest.mark.parametrize(
    ("args", "refusal"),
    [
        ({"availability": "critical"}, "availability must be one of: low, medium, high, very_high"),
        ({"rto_minutes": -1}, "rto_minutes must be a whole number of minutes from 0 to 5256000"),
        ({"rpo_minutes": 5256001}, "rpo_minutes must be a whole number of minutes"),
        ({"mtpd_minutes": 1.5}, "mtpd_minutes must be a whole number of minutes"),
        ({"reference": "x" * 65}, "reference must be at most 64 characters"),
        ({"business_function": "x" * 129}, "business_function must be at most 128 characters"),
    ],
)
async def test_a_primary_asset_value_opencrq_would_refuse_is_refused_before_any_call(
    args: dict[str, object], refusal: str
) -> None:
    with _recorded_trpc(opencrq_assets) as calls:
        out = await _run("update_opencrq_primary_asset", primary_asset_id="p-1", **args)
        out_create = await _run(
            "create_opencrq_primary_asset",
            organization_id="o-1",
            name="P",
            business_value_category_ids=["c-1"],
            **args,
        )
    assert refusal in out
    assert refusal in out_create
    assert calls == []


def test_primary_asset_tools_state_the_scale_and_the_units() -> None:
    update = _tool_properties("update_opencrq_primary_asset")
    create = _tool_properties("create_opencrq_primary_asset")
    for criterion in ("availability", "integrity", "confidentiality", "traceability"):
        assert update[criterion]["enum"] == ["low", "medium", "high", "very_high", None]
        assert create[criterion]["enum"] == ["low", "medium", "high", "very_high"]
        assert "low < medium < high < very_high" in update[criterion]["description"]
        assert "null" in update[criterion]["type"]
    for duration in ("rto_minutes", "rpo_minutes", "mtpd_minutes"):
        assert "whole minutes" in update[duration]["description"]
        assert update[duration]["maximum"] == 10 * 365 * 24 * 60
    assert update["reference"]["maxLength"] == 64
    assert update["business_function"]["maxLength"] == 128


async def test_primary_asset_list_and_get_show_native_fields_and_the_computed_overall_level() -> None:
    asset = {
        "id": "p-1",
        "organizationId": "o-1",
        "organization": {"id": "o-1", "name": "Acme"},
        "name": "Payments",
        "reference": "PA-01",
        "businessFunction": "Card payments",
        "availability": "high",
        "integrity": "high",
        "confidentiality": "very_high",
        "traceability": "medium",
        "rtoMinutes": 240,
        "rpoMinutes": None,
        "mtpdMinutes": 1440,
        "owner": {"id": None, "name": None},
        "businessValueCategories": [{"id": "c-1", "code": "pd", "name": "Personal data"}],
    }
    unset = {"id": "p-2", "name": "Archive"}
    with _recorded_trpc(
        opencrq_assets,
        {"primaryAssets.getPrimaryAssets": [asset, unset], "primaryAssets.getPrimaryAsset": asset},
    ) as calls:
        listed = await _run("list_opencrq_primary_assets", organization_id="o-1")
        got = await _run("get_opencrq_primary_asset", primary_asset_id="p-1")
    assert calls[1]["procedure"] == "primaryAssets.getPrimaryAsset"
    assert calls[1]["input"] == {"id": "p-1"}
    assert "A/I/C/T: high/high/very_high/medium (overall very_high, computed)" in listed
    assert "RTO 240 min (4 h), RPO not set, MTPD 1440 min (1 d)" in listed
    assert "Business function: Card payments" in listed
    assert "ref PA-01" in listed
    assert "A/I/C/T: not set/not set/not set/not set (overall not set, computed)" in listed
    for expected in (
        "[PrimaryAsset] Payments",
        "Reference: PA-01",
        "Business function: Card payments",
        "Availability: high",
        "Confidentiality: very_high",
        "Traceability: medium",
        "Overall level (computed, the highest criterion set): very_high",
        "RTO: 240 min (4 h)",
        "RPO: not set",
        "MTPD: 1440 min (1 d)",
        "Business value categories: Personal data",
        "Owner: none",
    ):
        assert expected in got, expected


async def test_supporting_asset_writes_send_criticality():
    with _recorded_trpc(opencrq_assets) as calls:
        await _run(
            "create_opencrq_supporting_asset",
            name="srv", category="ai_target", criticality="very_high",
        )
        await _run(
            "update_opencrq_supporting_asset",
            supporting_asset_id="s-1", criticality="low",
        )
    assert calls[0]["input"]["criticality"] == "very_high"
    assert calls[0]["input"]["categoryId"] == "ai_target"
    assert calls[1]["input"] == {"id": "s-1", "criticality": "low"}
    for name in ("create_opencrq_supporting_asset", "update_opencrq_supporting_asset"):
        assert "criticity" not in _tool_properties(name)


async def test_supporting_asset_classification_is_refused_before_any_call():
    refusals = [
        (
            "create_opencrq_supporting_asset",
            {"name": "n", "category": "host", "subcategory": "firewall"},
            "subcategory 'firewall' does not belong to category 'host'",
        ),
        (
            "create_opencrq_supporting_asset",
            {"name": "n", "category": "generic_asset", "subcategory": "server"},
            "it has none",
        ),
        (
            "create_opencrq_supporting_asset",
            {"name": "n", "category": "host", "properties": {"family": "identity"}},
            "category 'host' takes a 'machine' block",
        ),
        (
            "create_opencrq_supporting_asset",
            {"name": "n", "category": "host", "properties": {"department": "IT"}},
            "belongs to the 'identity' block",
        ),
        (
            "create_opencrq_supporting_asset",
            {"name": "n", "category": "host", "properties": {"hardware_uuid": "u"}},
            "is not a field of the 'machine' block",
        ),
        (
            "create_opencrq_supporting_asset",
            {"name": "n", "category": "saas_application", "properties": {"vendor": "v"}},
            "has no properties block",
        ),
        (
            "update_opencrq_supporting_asset",
            {"supporting_asset_id": "x", "category": "identity", "properties": {}},
            "at least one field",
        ),
    ]
    with _recorded_trpc(opencrq_assets) as calls:
        for tool, args, expected in refusals:
            assert expected in await _run(tool, **args), (tool, args)
    assert calls == []


def test_asset_subcategories_cover_every_category_once():
    assert set(ASSET_SUBCATEGORIES) == set(ASSET_CATEGORIES)
    flat = [s for subs in ASSET_SUBCATEGORIES.values() for s in subs]
    assert len(flat) == len(set(flat))
    assert ASSET_SUBCATEGORIES["generic_asset"] == ()


def test_the_opencrq_taxonomy_is_the_profile_taxonomy():
    # xtm-one #1233: one taxonomy, source_code included, so a profile asset pushes as is.
    from app.services.organization_context import ASSET_TAXONOMY

    assert list(ASSET_TAXONOMY) == ASSET_CATEGORIES
    assert dict(ASSET_TAXONOMY) == ASSET_SUBCATEGORIES
    assert "code_repository" in ASSET_SUBCATEGORIES["source_code"]


async def test_evidence_dismissal_sends_type_code_and_reason():
    with _recorded_trpc(opencrq_assets) as calls:
        await _run(
            "update_opencrq_evidence_state",
            evidence_id="e-1",
            state="dismissed",
            dismissal_type="false_positive",
            reason_code="detection_error",
            reason=" scanner bug ",
        )
        await _run("update_opencrq_evidence_state", evidence_id="e-1", state="applicable")
    assert calls[0]["input"] == {
        "evidenceId": "e-1",
        "state": "dismissed",
        "dismissalType": "false_positive",
        "reasonCode": "detection_error",
        "reason": "scanner bug",
    }
    assert calls[1]["input"] == {"evidenceId": "e-1", "state": "applicable"}


async def test_evidence_creation_sends_the_cve_record_id_as_cve_id():
    with _recorded_trpc(opencrq_assets) as calls:
        await _run(
            "create_opencrq_asset_evidence",
            supporting_asset_id="s-1", cve_record_id="cve-uuid",
        )
    assert calls[0]["input"] == {"supportingAssetId": "s-1", "cveId": "cve-uuid"}


async def test_threat_relevance_update_keeps_the_settings_it_is_not_given():
    stored = {
        "actHalfLifeObservedDays": 180,
        "actHalfLifeAuthoredDays": 365,
        "confidenceGate": 60,
    }
    with _recorded_trpc(
        opencrq_risk, {"threatRelevanceSettings.getThreatRelevanceSettings": stored}
    ) as calls:
        await _run("update_opencrq_threat_relevance_settings", confidence_gate=0)
        out = await _run(
            "update_opencrq_threat_relevance_settings", act_half_life_observed_days=400
        )
    assert calls[1]["input"] == {**stored, "confidenceGate": 0}
    assert calls[1]["mutation"] is True
    assert "must not exceed" in out
    assert len(calls) == 3


async def test_jobs_list_sends_kinds_and_statuses_and_reads_the_envelope():
    page = {
        "items": [
            {
                "id": "j-1", "kind": "crq-simulation", "status": "dlq",
                "enqueuedAt": "2026-10-14T10:00:00.000Z", "lastError": "boom",
            }
        ],
        "total": 41,
    }
    with _recorded_trpc(opencrq_ops, {"jobs.listAllRuns": page}) as calls:
        out = await _run(
            "list_opencrq_jobs", kinds=["crq-simulation"], statuses=["DLQ"], limit=10
        )
    assert calls[0]["input"] == {
        "kinds": ["crq-simulation"], "statuses": ["dlq"], "limit": 10, "offset": 0,
    }
    assert "41, showing 1" in out and "crq-simulation: dlq" in out and "boom" in out


async def test_jobs_list_filters_on_the_partial_status_opencrq_writes():
    """OpenCRQ's terminal ``partial`` (a bounded run handed to a continuation) is a status filter."""
    page = {"items": [{"id": "j-2", "kind": "nist-sync", "status": "partial"}], "total": 1}
    with _recorded_trpc(opencrq_ops, {"jobs.listAllRuns": page}) as calls:
        out = await _run("list_opencrq_jobs", statuses=["partial"])
    assert calls[0]["input"]["statuses"] == ["partial"]
    assert "nist-sync: partial" in out
    schema = next(
        d for d in opencrq_ops.register("https://crq", {}, CallableToolExecutor()) if d.name == "list_opencrq_jobs"
    ).input_schema
    assert "partial" in schema["properties"]["statuses"]["items"]["enum"]


async def test_crq_settings_update_sends_every_setting_opencrq_accepts():
    with _recorded_trpc(opencrq_crq) as calls:
        await _run(
            "update_opencrq_crq_settings", susceptibility_gamma=0,
            measured_validity_days=30, unknown_criticality_counts_as="very_high",
            unknown_exposure_counts_as="internet_facing",
        )
        await _run("update_opencrq_crq_settings", measured_validity_days=0)
    # crq-indicator-settings-router.ts: UpdateCrqIndicatorSettingsSchema, every key optional
    assert calls[0]["input"] == {
        "susceptibilityGamma": 0, "measuredValidityDays": 30,
        "unknownCriticalityCountsAs": "very_high", "unknownExposureCountsAs": "internet_facing",
    }
    # A null window turns it off; an omitted key would keep it.
    assert calls[1]["input"] == {"measuredValidityDays": None}


async def test_organization_update_sends_revenue_band():
    with _recorded_trpc(opencrq_admin) as calls:
        await _run(
            "update_opencrq_organization", organization_id="o-1", revenue_band="1b_10b"
        )
    assert calls[0]["input"] == {"id": "o-1", "revenueBand": "1b_10b"}
    assert "revenue" not in _tool_properties("update_opencrq_organization")


def test_a_field_an_update_tool_can_remove_says_so():
    for tool in (
        "update_opencrq_primary_asset",
        "update_opencrq_supporting_asset",
        "update_opencrq_risk_scenario",
        "update_opencrq_organization",
    ):
        assert "empty string removes" in _tool_properties(tool)["description"]["description"], tool


# ── Formatters on the current OpenCRQ shapes ───────────────────────────────


def test_fmt_run_shows_means_with_their_bands_and_no_single_loss_expectancy():
    out = _fmt_run(
        {
            "id": "r-1",
            "iterations": 400,
            "vulnerabilityP5": 0.3,
            "vulnerabilityP50": 0.31,
            "vulnerabilityP95": 0.32,
            "lefMean": 0.17048,
            "lefP10": 0.03,
            "lefP90": 0.35,
            "aleMean": 188117.27,
            "aleP10": 30576.99,
            "aleP90": 406269.17,
            "outcomeBlocked": 0.69,
            "outcomeImpactUndetected": 0.28,
            "techniqueCount": 43,
            "coveredTechniqueCount": 40,
        }
    )
    assert "ALE (annualized loss expectancy): mean 188,117 (P10 30,577 / P90 406,269)" in out
    assert "LEF (loss event frequency): mean 0.17 (P10 0.03 / P90 0.35)" in out
    assert "blocked 69%" in out and "impact unnoticed 28%" in out
    assert "40 covered of 43" in out
    assert "SLE" not in out and "None" not in out and "N/A" not in out


def test_fmt_run_without_a_price_says_so():
    out = _fmt_run({"id": "r-2", "iterations": 10, "aleMean": None, "lefMean": None})
    assert "ALE (annualized loss expectancy): not computed" in out


def test_fmt_risk_scenario_overview_row_shows_latest_run_and_readiness():
    out = _fmt_risk_scenario(
        {
            "id": "s-1",
            "name": "Breach",
            "organization": {"id": "o-1", "name": "Acme"},
            "attackOutcome": {"id": "ao-1", "name": "Data Breach"},
            "primaryAssets": [{"id": "p-1", "name": "Customer data"}],
            "owner": None,
            "latestRun": {
                "aleMean": 1500.0, "aleP10": 100.0, "aleP90": 3000.0,
                "lefMean": 0.2, "techniqueCount": 41, "coveredTechniqueCount": 40,
            },
            "readiness": {"status": "incomplete", "missing": ["tef"], "outdated": []},
        }
    )
    assert "Latest run: ALE mean 1,500 (P10 100 / P90 3,000), LEF mean 0.2" in out
    assert "40 of 41 techniques covered" in out
    assert "Readiness: incomplete (missing tef)" in out
    assert "Confidence" not in out and "None" not in out


def test_fmt_supporting_asset_reads_a_nullable_name_and_the_properties_block():
    out = _fmt_supporting_asset(
        {
            "id": "a-1",
            "name": None,
            "derivedName": "172.16.1.192",
            "categoryId": "host",
            "criticality": "very_high",
            "exposure": "internal",
            "sources": [{"source": "microsoft_graph"}],
            "properties": {
                "family": "machine", "hostname": "srv", "vendor": "Dell",
                "ipAddresses": [], "model": None,
            },
        }
    )
    assert "[SupportingAsset] 172.16.1.192" in out
    assert "Criticality: very_high" in out and "Sources: microsoft_graph" in out
    assert "Properties (machine): hostname srv, vendor Dell" in out
    assert "None" not in out


def test_fmt_cve_reads_cvss_epss_title_and_description():
    out = _fmt_cve(
        {
            "id": "u-1", "cveId": "CVE-2026-1", "cveTitle": "Title",
            "cvss": 6.5, "epss": 0.12, "kev": False,
            "cveDescription": "Long text",
        }
    )
    assert "CVSS: 6.5" in out and "EPSS: 0.12" in out and "CISA KEV: no" in out
    assert "Title: Title" in out and "Description: Long text" in out
    assert "Published" not in out


# ── Follow-ups of the tool alignment review ──


def _capabilities(count: int) -> list[dict]:
    return [
        {"id": f"c-{i:02d}", "name": f"Capability {i:02d}", "externalId": f"CC-{i:02d}"}
        for i in range(count)
    ]


async def test_control_capabilities_list_pages_past_the_first_forty():
    answers = {"controlCapabilities.getControlCapabilities": _capabilities(45)}
    with _recorded_trpc(opencrq_frameworks, answers):
        first = await _run("list_opencrq_control_capabilities")
        rest = await _run("list_opencrq_control_capabilities", offset=40)
    # The page is announced before the rows, so a reader that stops early knows it did.
    assert first.startswith("Control capabilities (45; page 1 of 2, next offset 40):\n")
    assert first.count("[ControlCapability]") == 40
    assert "Capability 39 (CC-39)" in first and "Capability 40" not in first
    assert first.endswith("Showing 1-40 of 45; call again with offset=40 for the next ones.")
    assert rest.startswith("Control capabilities (45; page 2 of 2, last page):\n")
    assert rest.count("[ControlCapability]") == 5
    assert "Capability 39" not in rest and "Capability 44 (CC-44)" in rest
    assert rest.endswith("Showing 41-45 of 45.")


async def test_control_capabilities_list_pages_after_the_search():
    answers = {"controlCapabilities.getControlCapabilities": _capabilities(45)}
    with _recorded_trpc(opencrq_frameworks, answers):
        out = await _run("list_opencrq_control_capabilities", search="cc-4")
        past = await _run("list_opencrq_control_capabilities", search="cc-4", offset=5)
    # The external ids CC-40 to CC-44, and nothing else.
    assert out.startswith("Control capabilities (5):\n")
    assert out.count("[ControlCapability]") == 5
    assert "Showing" not in out
    assert past.endswith("offset 5 is past the end: 5 in total.")


async def test_control_capabilities_list_refuses_a_negative_offset():
    with _recorded_trpc(opencrq_frameworks) as calls:
        out = await _run("list_opencrq_control_capabilities", offset=-1)
    assert out == "offset must be >= 0."
    assert calls == []


def test_control_capabilities_list_declares_its_offset():
    assert _tool_properties("list_opencrq_control_capabilities")["offset"]["minimum"] == 0


async def test_control_capabilities_read_page_by_page_reaches_every_row() -> None:
    # A reader that follows "next offset" until the last page reads all 51 capabilities.
    answers = {"controlCapabilities.getControlCapabilities": _capabilities(51)}
    seen, offset = 0, 0
    with _recorded_trpc(opencrq_frameworks, answers):
        while True:
            out = await _run("list_opencrq_control_capabilities", offset=offset)
            seen += out.count("[ControlCapability]")
            header = out.split("\n", 1)[0]
            if "next offset" not in header:
                break
            offset = int(header.rsplit("next offset ", 1)[1].rstrip("):"))
    assert seen == 51 and offset == 40


# ── Control what-if: an empty answer says why ──


def _latest_run(profile: list[str] | None) -> dict[str, object]:
    run: dict[str, object] = {"id": "r-9", "createdAt": "2026-11-20T10:00:00.000Z"}
    if profile is not None:
        run["profile"] = profile
    return run


async def test_what_if_empty_after_price_runs_names_the_levers_recompute() -> None:
    answers = {
        "crqIndicators.getWhatIfResults": [],
        "crqIndicators.getLatestRun": _latest_run([]),
    }
    with _recorded_trpc(opencrq_crq, answers) as calls:
        out = await _run("get_opencrq_crq_what_if", risk_scenario_id="s-1")
    assert [c["procedure"] for c in calls] == [
        "crqIndicators.getWhatIfResults",
        "crqIndicators.getLatestRun",
    ]
    assert calls[1]["input"] == {"riskScenarioId": "s-1"}
    assert "computed the baseline only" in out and "r-9" in out
    assert 'run_opencrq_crq_simulation(risk_scenario_id, family="levers")' in out
    assert "run a CRQ simulation first" not in out


async def test_what_if_empty_after_a_techniques_run_names_what_it_computed() -> None:
    answers = {
        "crqIndicators.getWhatIfResults": [],
        "crqIndicators.getLatestRun": _latest_run(["techniques"]),
    }
    with _recorded_trpc(opencrq_crq, answers):
        out = await _run("get_opencrq_crq_what_if", risk_scenario_id="s-1")
    assert "computed techniques only" in out and 'family="levers"' in out


async def test_what_if_empty_after_a_levers_run_says_no_control_acts_and_not_run_again() -> None:
    answers = {
        "crqIndicators.getWhatIfResults": [],
        "crqIndicators.getLatestRun": _latest_run(["techniques", "levers", "plans"]),
    }
    with _recorded_trpc(opencrq_crq, answers):
        out = await _run("get_opencrq_crq_what_if", risk_scenario_id="s-1")
    assert "found no control acting on this scenario's techniques" in out
    assert "Running again changes nothing" in out
    assert "family=" not in out


async def test_what_if_empty_without_any_run_asks_for_a_first_simulation() -> None:
    answers: dict[str, object] = {"crqIndicators.getWhatIfResults": [], "crqIndicators.getLatestRun": None}
    with _recorded_trpc(opencrq_crq, answers):
        out = await _run("get_opencrq_crq_what_if", risk_scenario_id="s-1")
    assert "has no CRQ run yet" in out


async def test_what_if_empty_with_an_unstated_profile_does_not_guess() -> None:
    answers = {
        "crqIndicators.getWhatIfResults": [],
        "crqIndicators.getLatestRun": _latest_run(None),
    }
    with _recorded_trpc(opencrq_crq, answers):
        out = await _run("get_opencrq_crq_what_if", risk_scenario_id="s-1")
    assert "does not say which figures it computed" in out and 'family="levers"' in out


async def test_what_if_rows_skip_the_latest_run_read() -> None:
    answers = {"crqIndicators.getWhatIfResults": [{"controlId": "c-1", "aleReductionMean": 5}]}
    with _recorded_trpc(opencrq_crq, answers) as calls:
        await _run("get_opencrq_crq_what_if", risk_scenario_id="s-1")
    assert "crqIndicators.getLatestRun" not in [c["procedure"] for c in calls]


async def test_simulation_with_family_enqueues_the_family_recompute() -> None:
    answers = {"crqIndicators.enqueueFamilyRecompute": {"jobId": "j-1"}}
    with _recorded_trpc(opencrq_crq, answers) as calls:
        out = await _run("run_opencrq_crq_simulation", risk_scenario_id="s-1", family="levers")
    assert calls == [
        {
            "procedure": "crqIndicators.enqueueFamilyRecompute",
            "input": {"riskScenarioId": "s-1", "family": "levers"},
            "mutation": True,
        }
    ]
    assert "control upgrade what-ifs" in out and "j-1" in out and "get_opencrq_crq_what_if" in out


async def test_simulation_without_family_stays_a_full_simulation() -> None:
    with _recorded_trpc(opencrq_crq, {"crqIndicators.enqueueSimulation": {"jobId": "j-2"}}) as calls:
        out = await _run("run_opencrq_crq_simulation", risk_scenario_id="s-1")
    assert calls[0]["procedure"] == "crqIndicators.enqueueSimulation"
    assert calls[0]["input"] == {"riskScenarioId": "s-1"}
    assert "get_opencrq_crq_run" in out


async def test_simulation_refuses_an_unknown_family() -> None:
    with _recorded_trpc(opencrq_crq) as calls:
        result = await _execute("run_opencrq_crq_simulation", risk_scenario_id="s-1", family="plans")
    assert calls == [] and "family" in result.content


async def test_crq_settings_read_as_labelled_values():
    settings = {
        "iterations": 10000,
        "exposureWeightInternetFacing": 1.5,
        "exposureWeightInternal": 0.75,
        "exposureWeightUnknown": 1,
        "susceptibilityGamma": 0.333333,
        "measuredValidityDays": 90,
        "unknownCriticalityCountsAs": "low",
        "unknownExposureCountsAs": "internet_facing",
    }
    with _recorded_trpc(
        opencrq_crq, {"crqIndicatorSettings.getCrqIndicatorSettings": settings}
    ) as calls:
        out = await _run("get_opencrq_crq_settings")
    assert calls == [
        {
            "procedure": "crqIndicatorSettings.getCrqIndicatorSettings",
            "input": None,
            "mutation": False,
        }
    ]
    assert out.splitlines() == [
        "CRQ engine settings:",
        "  Simulation iterations: 10,000",
        "  Exposure weights: internet-facing 1.5, internal 0.75, unknown 1",
        "  Susceptibility gamma: 0.333 (how stacked control capabilities combine "
        "on a technique: 1 counts each fully, 0 only the strongest)",
        "  A test result counts as a measurement for: 90 days",
        "  Unknown criticality counts as: low",
        "  Unknown exposure counts as: internet_facing",
    ]


async def test_crq_settings_read_a_null_window_as_no_limit_and_skip_a_missing_key():
    # An older OpenCRQ returns no ``exposureWeightUnknown`` and no finding-score knobs.
    settings = {
        "iterations": 500,
        "exposureWeightInternetFacing": 2,
        "exposureWeightInternal": 1,
        "susceptibilityGamma": 1,
        "measuredValidityDays": None,
    }
    with _recorded_trpc(
        opencrq_crq, {"crqIndicatorSettings.getCrqIndicatorSettings": settings}
    ):
        out = await _run("get_opencrq_crq_settings")
    assert out.splitlines() == [
        "CRQ engine settings:",
        "  Simulation iterations: 500",
        "  Exposure weights: internet-facing 2, internal 1",
        "  Susceptibility gamma: 1 (how stacked control capabilities combine "
        "on a technique: 1 counts each fully, 0 only the strongest)",
        "  A test result counts as a measurement for: no limit (window off)",
    ]


async def test_threat_relevance_settings_read_as_labelled_values():
    settings = {
        "actHalfLifeObservedDays": 180,
        "actHalfLifeAuthoredDays": 365,
        "confidenceGate": 60,
    }
    with _recorded_trpc(
        opencrq_risk, {"threatRelevanceSettings.getThreatRelevanceSettings": settings}
    ) as calls:
        out = await _run("get_opencrq_threat_relevance_settings")
    assert calls == [
        {
            "procedure": "threatRelevanceSettings.getThreatRelevanceSettings",
            "input": None,
            "mutation": False,
        }
    ]
    assert out.splitlines() == [
        "Threat relevance settings:",
        "  Half-life of observed threat activity: 180 days",
        "  Half-life of authored threat activity: 365 days",
        "  Confidence gate: 60 (only threat claims at this confidence or above are read)",
    ]


@pytest.mark.parametrize(
    "name",
    [
        # ``publish`` freezes the answers and feeds the control capabilities.
        "manage_opencrq_compliance_assessment",
        "update_opencrq_requirement_answer",
    ],
)
def test_a_compliance_write_asks_for_approval(name):
    from app.services import hitl_whitelist_derived, hitl_whitelist_platform
    from app.services.hitl_whitelist_default import SHIPPED_DEFAULT_WHITELIST
    from app.services.tool_classification import infer_tool_type

    _, tools = _build()
    assert name in {t.name for t in tools}
    assert infer_tool_type(name) == "write"
    assert name not in SHIPPED_DEFAULT_WHITELIST
    platform_reads = hitl_whitelist_platform.platform_read_tools()["opencrq"]
    hitl_whitelist_derived.reset_cache()
    try:
        integration_reads = hitl_whitelist_derived.provider_read_tools()["opencrq"]
    finally:
        hitl_whitelist_derived.reset_cache()
    assert name not in platform_reads
    assert name not in integration_reads
    # The derivation does exempt the provider's reads: the absence above is the classification's.
    assert "get_opencrq_compliance_assessment" in platform_reads
    assert "get_opencrq_compliance_assessment" in integration_reads
