# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""ROI of agentic work: the unit catalog, the unit an agent declares, the figures.

CTEM seeded fleets, slice S4 (``dev-docs/ctem-fleets.md`` section 5, issue
#8990). Pure module: no database, no I/O. The storage of per-run unit records,
the per-organization overrides and the API build on it.

* **Baselines.** :data:`CTEM_BASELINES` (XTM One mode) and
  :data:`CORPORATE_BASELINES` (Copilot mode, issue #9550) are the built-in
  catalogs, :func:`builtin_baselines` picks the mode's: one row per unit of
  work a seeded agent performs, with the manual analyst minutes it replaces
  (low / likely / high) and the human review minutes left once an agent does
  it. Every minute figure is an ASSUMPTION (``minutes_label``), the one
  published exception being the low bound of a CVE assessment; an
  administrator overrides them per organization, and run telemetry or a
  customer time-and-motion study replaces them.
* **Rates.** :data:`HOURLY_RATES` is the fully loaded hourly cost per role and
  currency, each with its source: a published median wage divided by the
  employer cost share (US) or multiplied by an on-cost assumption (EU).
* **Unit declaration.** An agent (built-in or custom) declares the unit its
  runs produce in its ``output_schema`` with the ``x-xtm-roi`` keyword:
  ``{"unit": "<catalog key>", "count": "<dot path in the structured output>"}``.
  The count is read from the run's structured output (an array is counted by
  its length, an integer taken as is, a non-empty string or an object as one
  artefact), never from what the model says it did. The keyword never reaches
  the model: ``structured_output`` drops ``x-xtm-*`` keys before the schema is
  sent to a provider or written into a prompt.
* **Figures.** :func:`compute_roi` turns the run measures of a period into
  hours saved, cost of the agentic work, value at the loaded rate, net value,
  ROI and payback, and lists the assumptions every figure rests on.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

#: The JSON Schema keyword an ``output_schema`` declares its ROI unit with.
ROI_SCHEMA_KEYWORD = "x-xtm-roi"

#: Run outcomes. A rejected run produced nothing the organization kept.
OUTCOME_ACCEPTED = "accepted"
OUTCOME_MODIFIED = "modified"
OUTCOME_REJECTED = "rejected"
OUTCOMES: tuple[str, ...] = (OUTCOME_ACCEPTED, OUTCOME_MODIFIED, OUTCOME_REJECTED)
#: The ``outcome`` filter value of a run nobody reviewed yet (stored as ``None``, #9310).
OUTCOME_UNREVIEWED = "unreviewed"

#: Reporting windows in days; ``None`` is since the start.
WINDOWS: dict[str, int | None] = {"7d": 7, "30d": 30, "90d": 90, "all": None}

#: Figure labels, shown next to every number.
LABEL_ASSUMPTION = "assumption"
LABEL_PUBLISHED = "published estimate"
LABEL_MEASURED = "measured"
LABEL_OVERRIDE = "organization override"

_US_RATE_SOURCE = (
    "BLS OOH median wage (May 2025) / 0.70, the wage share of private-industry employer cost (BLS ECEC, June 2026)"
)
_EU_RATE_SOURCE = (
    "Stepstone DE Cyber Security Analyst median EUR 51,400/yr (read 5 Nov 2026) "
    "/ 1,720 h x 1.30 employer on-cost (assumption)"
)


@dataclass(frozen=True)
class HourlyRate:
    """A fully loaded hourly cost of one role in one currency."""

    role: str
    currency: str
    amount: float
    label: str
    source: str


#: Fully loaded hourly cost by role, then currency: the CTEM roles, XTM One mode's built-ins.
CTEM_HOURLY_RATES: dict[str, dict[str, HourlyRate]] = {
    "analyst": {
        "USD": HourlyRate(
            "analyst", "USD", 88.73, LABEL_PUBLISHED, "Information security analysts, " + _US_RATE_SOURCE
        ),
        "EUR": HourlyRate("analyst", "EUR", 38.85, LABEL_ASSUMPTION, _EU_RATE_SOURCE),
    },
    "ciso": {
        "USD": HourlyRate(
            "ciso", "USD", 120.29, LABEL_PUBLISHED, "Computer and information systems managers, " + _US_RATE_SOURCE
        ),
        "EUR": HourlyRate(
            "ciso", "EUR", 52.67, LABEL_ASSUMPTION, "EU analyst rate x 1.356, the US manager to analyst wage ratio"
        ),
    },
}

_ASSUMED = "desk estimate, to replace with run telemetry or a customer time-and-motion study"


@dataclass(frozen=True)
class Baseline:
    """The manual effort one unit of work replaces."""

    unit: str
    name: str
    fleet: str
    role: str
    manual_min_low: float
    manual_min_likely: float
    manual_min_high: float
    review_min: float
    rate_role: str = "analyst"
    minutes_label: str = LABEL_ASSUMPTION
    minutes_source: str = _ASSUMED

    @property
    def gained_min_likely(self) -> float:
        """Minutes gained per unit at the likely baseline, review deducted."""
        return max(self.manual_min_likely - self.review_min, 0.0)


def _b(
    unit: str, name: str, fleet: str, role: str, low: float, likely: float, high: float, review: float, **kw: Any
) -> Baseline:
    return Baseline(unit, name, fleet, role, low, likely, high, review, **kw)


#: The CTEM catalog, XTM One mode's built-ins (Analyst item research:ctem-roi-baselines, 5 Nov 2026).
CTEM_BASELINES: tuple[Baseline, ...] = (
    _b("threat_report_triaged", "Threat report triaged for relevance", "CTI", "CTI analyst", 10, 20, 45, 3),
    _b("intrusion_set_profiled", "Intrusion set profile built", "CTI", "CTI analyst", 240, 480, 960, 45),
    _b("ttp_extraction", "TTP extraction from a report (ATT&CK mapping)", "CTI", "CTI analyst", 30, 60, 120, 10),
    _b("stix_bundle_built", "STIX bundle built from a document", "CTI", "CTI analyst", 45, 90, 180, 15),
    _b("ioc_batch_enriched", "IOC enrichment (batch of about 20 observables)", "CTI", "CTI analyst", 15, 30, 60, 5),
    _b(
        "detection_rule_written",
        "Detection rule written (Sigma, SPL, KQL, IOA)",
        "Detection",
        "Detection engineer",
        60,
        180,
        480,
        30,
    ),
    _b(
        "threat_hunt_package",
        "Threat hunt package (hypothesis, queries, playbook)",
        "Detection",
        "Detection engineer",
        240,
        480,
        960,
        60,
    ),
    _b("scenario_built", "OpenAEV scenario built from a threat", "Exposure validation", "Red team", 60, 180, 360, 30),
    _b("attack_path_designed", "Attack path designed", "Exposure validation", "Red team", 120, 240, 480, 45),
    _b("phishing_content_built", "Phishing simulation content", "Exposure validation", "Red team", 60, 120, 240, 20),
    _b("simulation_result_analysed", "Simulation result analysed", "Exposure validation", "Red team", 30, 90, 180, 15),
    _b(
        "cve_assessed",
        "CVE assessed against the estate (KEV, EPSS, asset match)",
        "Vulnerability management",
        "VM analyst",
        3,
        20,
        60,
        3,
        minutes_label="low: published estimate; likely and high: assumption",
        minutes_source="Low bound 3 min per manual CVE lookup (MDPI Information 17(9) 846, 2026); likely and high: "
        + _ASSUMED,
    ),
    _b(
        "exploitability_validated", "Exploitability validated", "Vulnerability management", "Red team", 60, 180, 480, 30
    ),
    _b(
        "remediation_ticket_drafted",
        "Remediation ticket drafted",
        "Vulnerability management",
        "VM analyst",
        10,
        20,
        45,
        3,
    ),
    _b("retest_cycle", "Re-test cycle", "Vulnerability management", "Red team", 30, 60, 240, 10),
    _b("risk_scenario_quantified", "Risk scenario quantified (OpenCRQ)", "Risk", "Risk manager", 240, 480, 1440, 60),
    _b("briefing_written", "Board or exposure briefing written", "Risk", "CISO", 120, 240, 480, 45, rate_role="ciso"),
    _b("dashboard_built", "Dashboard built", "Risk", "VM analyst", 60, 180, 480, 20),
    _b(
        "ai_test_plan",
        "AI system test plan (prompt injection, tool misuse)",
        "AI security",
        "Red team",
        240,
        480,
        960,
        60,
    ),
    _b("atlas_mapping", "ATLAS mapping of an AI system", "AI security", "Red team", 60, 120, 240, 20),
    # CTEM posture fleet and indicator decay (#9836 R2): desk assumptions until the Analyst's figures land.
    _b("ctem_scope_reviewed", "CTEM scope reviewed", "Posture", "Exposure analyst", 30, 60, 120, 10),
    _b("exposure_inventory_refreshed", "Exposure inventory refreshed", "Posture", "Exposure analyst", 45, 90, 180, 15),
    _b("exposure_prioritized", "Exposure backlog prioritized", "Posture", "Exposure analyst", 30, 60, 120, 10),
    _b("validation_reviewed", "Validation results reviewed", "Posture", "Exposure analyst", 30, 60, 120, 10),
    _b("mobilization_plan", "Remediation mobilization plan", "Posture", "Exposure analyst", 30, 60, 120, 10),
    _b("indicator_decay_reviewed", "Decaying indicator reviewed", "CTI", "CTI analyst", 5, 10, 20, 2),
    # OpenCRQ risk budget desk (#10593): desk assumption until the Analyst's figures land.
    _b("investment_case_written", "Control investment case written", "Risk", "Risk manager", 45, 90, 180, 15),
)

# ── Corporate catalog: Copilot mode's built-ins (mode split S1, issue #9550) ──
#
# Analyst item research:corporate-roi-baselines, 7 Nov 2026. Minutes are desk
# assumptions (review about 10 to 20% of the likely manual time); rates are
# published wages, loaded with published ratios. US figures are medians, EU
# figures are means by ISCO-08 major group: the two are never converted.

_CORPORATE_US_LOADING = (
    "BLS OEWS national hourly median (May 2025) / 0.70, the wage share of private-industry employer cost "
    "(BLS ECEC, June 2026)"
)
_CORPORATE_EU_LOADING = (
    "Eurostat SES 2022 mean hourly earnings (earn_ses22_14, EU27, firms 10+) x 1.3304 labour cost / wages "
    "(lc_lci_lev 2022) x 1.1542 wage growth 2022 to 2025 (lc_lci_lev); the ISCO-08 group of the role is an assumption"
)


def _corporate_rate(
    role: str, occupation: str, soc: str, usd: float, isco: str, isco_label: str, eur: float
) -> dict[str, HourlyRate]:
    return {
        "USD": HourlyRate(role, "USD", usd, LABEL_PUBLISHED, f"{occupation} (SOC {soc}), {_CORPORATE_US_LOADING}"),
        "EUR": HourlyRate(role, "EUR", eur, LABEL_ASSUMPTION, f"{isco_label} ({isco}), {_CORPORATE_EU_LOADING}"),
    }


#: Fully loaded hourly cost of the corporate roles, Copilot mode's built-ins.
CORPORATE_HOURLY_RATES: dict[str, dict[str, HourlyRate]] = {
    "knowledge_worker": _corporate_rate(
        "knowledge_worker",
        "Business and financial operations occupations",
        "13-0000",
        56.77,
        "OC2",
        "Professionals",
        37.47,
    ),
    "executive_assistant": _corporate_rate(
        "executive_assistant",
        "Executive secretaries and executive administrative assistants",
        "43-6011",
        52.60,
        "OC3",
        "Technicians and associate professionals",
        30.25,
    ),
    "sales_rep": _corporate_rate(
        "sales_rep",
        "Sales representatives, wholesale and manufacturing, technical and scientific products",
        "41-4011",
        72.06,
        "OC3",
        "Technicians and associate professionals",
        30.25,
    ),
    "legal_counsel": _corporate_rate("legal_counsel", "Lawyers", "23-1011", 109.66, "OC2", "Professionals", 37.47),
    "finance_analyst": _corporate_rate(
        "finance_analyst", "Financial and investment analysts", "13-2051", 70.57, "OC2", "Professionals", 37.47
    ),
    "hr_specialist": _corporate_rate(
        "hr_specialist", "Human resources specialists", "13-1071", 52.16, "OC2", "Professionals", 37.47
    ),
    "support_agent": _corporate_rate(
        "support_agent", "Computer user support specialists", "15-1232", 42.49, "OC4", "Clerical support workers", 24.31
    ),
    "marketing_specialist": _corporate_rate(
        "marketing_specialist",
        "Market research analysts and marketing specialists",
        "13-1161",
        54.10,
        "OC2",
        "Professionals",
        37.47,
    ),
    "software_engineer": _corporate_rate(
        "software_engineer", "Software developers", "15-1252", 93.40, "OC2", "Professionals", 37.47
    ),
    "manager": _corporate_rate(
        "manager", "General and operations managers", "11-1021", 72.64, "OC1", "Managers", 50.76
    ),
    # The office security desks (#10006 S3): the occupation of XTM One mode's analyst rate, corporate loading.
    "security_analyst": _corporate_rate(
        "security_analyst", "Information security analysts", "15-1212", 88.73, "OC2", "Professionals", 37.47
    ),
}

_CORPORATE_ASSUMED = (
    "desk estimate (Analyst, research:corporate-roi-baselines), to replace with run telemetry or a customer time study"
)


def _c(
    unit: str,
    name: str,
    function: str,
    role: str,
    low: float,
    likely: float,
    high: float,
    review: float,
    rate_role: str,
) -> Baseline:
    return Baseline(
        unit, name, function, role, low, likely, high, review, rate_role=rate_role, minutes_source=_CORPORATE_ASSUMED
    )


#: The corporate catalog, Copilot mode's built-ins (``fleet`` holds the office function).
CORPORATE_BASELINES: tuple[Baseline, ...] = (
    _c(
        "email_triaged_answered",
        "Email triaged and answered",
        "Executive office",
        "Executive assistant",
        2,
        4,
        10,
        1,
        "executive_assistant",
    ),
    _c(
        "meeting_brief_prepared",
        "Meeting brief prepared (attendees, account, open items)",
        "Executive office",
        "Executive assistant",
        20,
        45,
        90,
        8,
        "executive_assistant",
    ),
    _c(
        "meeting_notes_actions",
        "Meeting notes and action items written up",
        "Operations",
        "Knowledge worker",
        10,
        20,
        40,
        4,
        "knowledge_worker",
    ),
    _c(
        "document_drafted",
        "Document or report drafted (2 to 5 pages)",
        "Operations",
        "Knowledge worker",
        60,
        120,
        240,
        20,
        "knowledge_worker",
    ),
    _c(
        "presentation_drafted",
        "Presentation drafted (10 to 15 slides)",
        "Marketing",
        "Marketing specialist",
        90,
        180,
        360,
        30,
        "marketing_specialist",
    ),
    _c(
        "rfp_questionnaire_answered",
        "RFP or security questionnaire answered (about 100 questions)",
        "Sales",
        "Sales rep",
        240,
        480,
        960,
        60,
        "sales_rep",
    ),
    _c(
        "contract_first_review",
        "Contract first review (NDA or standard commercial agreement)",
        "Legal",
        "Legal counsel",
        30,
        60,
        180,
        15,
        "legal_counsel",
    ),
    _c(
        "invoice_expense_checked",
        "Invoice or expense report checked",
        "Finance",
        "Finance analyst",
        3,
        8,
        20,
        1,
        "finance_analyst",
    ),
    _c(
        "month_end_variance_commentary",
        "Month-end variance commentary drafted",
        "Finance",
        "Finance analyst",
        60,
        120,
        240,
        20,
        "finance_analyst",
    ),
    _c("candidate_screened", "Candidate application screened", "HR", "HR specialist", 5, 10, 20, 2, "hr_specialist"),
    _c(
        "onboarding_pack_prepared",
        "Onboarding pack prepared for a new hire",
        "HR",
        "HR specialist",
        45,
        90,
        180,
        15,
        "hr_specialist",
    ),
    _c(
        "policy_question_answered",
        "Employee policy question answered",
        "HR",
        "HR specialist",
        5,
        10,
        25,
        2,
        "hr_specialist",
    ),
    _c(
        "support_ticket_resolved",
        "Support ticket resolved (tier 1)",
        "IT / support",
        "Support agent",
        10,
        20,
        45,
        4,
        "support_agent",
    ),
    _c(
        "kb_article_written",
        "Knowledge base article written from a resolved ticket",
        "IT / support",
        "Support agent",
        20,
        40,
        90,
        8,
        "support_agent",
    ),
    _c(
        "account_research_summarised",
        "Account or market research summarised",
        "Sales",
        "Sales rep",
        30,
        60,
        120,
        10,
        "sales_rep",
    ),
    _c(
        "outreach_email_personalised",
        "Personalised outreach email drafted",
        "Sales",
        "Sales rep",
        5,
        12,
        25,
        2,
        "sales_rep",
    ),
    _c("crm_update_after_call", "CRM record updated after a call", "Sales", "Sales rep", 5, 10, 20, 1, "sales_rep"),
    _c(
        "research_summarised",
        "Research topic summarised (reading several sources)",
        "Executive office",
        "Knowledge worker",
        60,
        120,
        240,
        15,
        "knowledge_worker",
    ),
    _c(
        "page_translated",
        "Page translated (about 500 words)",
        "Marketing",
        "Marketing specialist",
        30,
        60,
        90,
        10,
        "marketing_specialist",
    ),
    _c(
        "content_piece_drafted",
        "Blog post or newsletter drafted (about 800 words)",
        "Marketing",
        "Marketing specialist",
        90,
        180,
        300,
        25,
        "marketing_specialist",
    ),
    _c(
        "data_pull_chart",
        "Data pull and chart produced",
        "Finance",
        "Finance analyst",
        20,
        45,
        120,
        8,
        "finance_analyst",
    ),
    _c(
        "code_review",
        "Code review (pull request of a few hundred lines)",
        "Engineering",
        "Software engineer",
        15,
        30,
        90,
        8,
        "software_engineer",
    ),
    _c(
        "unit_tests_written",
        "Unit tests written for a change",
        "Engineering",
        "Software engineer",
        30,
        60,
        120,
        12,
        "software_engineer",
    ),
    _c(
        "weekly_status_report",
        "Weekly team status report compiled",
        "Operations",
        "Manager",
        30,
        60,
        120,
        10,
        "manager",
    ),
    # The units the office fleets produce (#10006 S3): engineering, executive office, governance,
    # communications, security, legal and product desks. Existing rate roles, plus security_analyst.
    _c(
        "change_delivered_pr_merged",
        "Change delivered (pull request merged)",
        "Engineering",
        "Software engineer",
        120,
        240,
        480,
        30,
        "software_engineer",
    ),
    _c(
        "bug_fixed_pr_merged",
        "Bug fixed (pull request merged)",
        "Engineering",
        "Software engineer",
        60,
        120,
        240,
        20,
        "software_engineer",
    ),
    _c(
        "ui_review",
        "UI review of a change (screens, states, verdict)",
        "Engineering",
        "Software engineer",
        20,
        45,
        90,
        8,
        "software_engineer",
    ),
    _c(
        "release_notes_written",
        "Release notes written",
        "Engineering",
        "Software engineer",
        20,
        45,
        90,
        8,
        "software_engineer",
    ),
    _c(
        "meeting_scheduled_or_moved",
        "Meeting scheduled or moved (attendees, slot, invitation)",
        "Executive office",
        "Executive assistant",
        5,
        10,
        25,
        1,
        "executive_assistant",
    ),
    _c(
        "executive_digest_prepared",
        "Executive digest prepared (daily or weekly)",
        "Executive office",
        "Executive assistant",
        30,
        60,
        120,
        10,
        "executive_assistant",
    ),
    _c(
        "governance_cycle_prepared",
        "Governance cycle prepared (agenda, pre-reads, recap, decision log)",
        "Operations",
        "Manager",
        120,
        240,
        480,
        40,
        "manager",
    ),
    _c(
        "press_quote_drafted",
        "Press quote or statement drafted",
        "Marketing",
        "Marketing specialist",
        20,
        45,
        90,
        8,
        "marketing_specialist",
    ),
    _c(
        "security_alert_triaged",
        "Security alert or ticket triaged",
        "Security",
        "Security analyst",
        10,
        20,
        45,
        3,
        "security_analyst",
    ),
    _c(
        "vulnerability_assessed",
        "Vulnerability assessed for the organization",
        "Security",
        "Security analyst",
        30,
        60,
        120,
        10,
        "security_analyst",
    ),
    _c(
        "compliance_evidence_collected",
        "Compliance evidence collected for a control",
        "Security",
        "Security analyst",
        20,
        45,
        120,
        8,
        "security_analyst",
    ),
    _c(
        "access_review_completed",
        "Access review completed for an application",
        "Security",
        "Security analyst",
        30,
        60,
        180,
        10,
        "security_analyst",
    ),
    # Governance desks (#10412): policy lifecycle, vendor risk, external sharing.
    _c(
        "policy_reviewed",
        "Policy reviewed in its lifecycle (owner, due date, changes)",
        "Security",
        "Security analyst",
        20,
        45,
        90,
        8,
        "security_analyst",
    ),
    _c(
        "vendor_risk_assessed",
        "Vendor risk assessed or reassessed",
        "Security",
        "Security analyst",
        30,
        60,
        180,
        12,
        "security_analyst",
    ),
    _c(
        "sharing_exposure_reviewed",
        "External sharing exposure reviewed (links, guests, revocations)",
        "Security",
        "Security analyst",
        15,
        30,
        60,
        5,
        "security_analyst",
    ),
    _c(
        "legal_request_triaged",
        "Legal request triaged and routed",
        "Legal",
        "Legal counsel",
        10,
        20,
        45,
        4,
        "legal_counsel",
    ),
    _c(
        "product_spec_drafted",
        "Product specification drafted",
        "Product",
        "Knowledge worker",
        120,
        240,
        480,
        40,
        "knowledge_worker",
    ),
    # Chat deliverables (#10544): what an agent builds in a conversation, counted at the LOW
    # bound by ``services/roi_chat.py``. Each row is a deliverable of typical size
    # (``roi_chat.TYPICAL_SIZE``); the count scales it by the deliverable's own size.
    _c(
        "executive_deck_built",
        "Executive deck built (10 slides)",
        "Executive office",
        "Knowledge worker",
        400,
        900,
        1800,
        60,
        "knowledge_worker",
    ),
    _c(
        "consolidated_report_built",
        "Consolidated report built (10 pages, several sources)",
        "Executive office",
        "Knowledge worker",
        300,
        600,
        1200,
        45,
        "knowledge_worker",
    ),
    _c(
        "decision_brief_written",
        "Decision brief written (2 pages)",
        "Executive office",
        "Knowledge worker",
        60,
        120,
        240,
        15,
        "knowledge_worker",
    ),
    _c(
        "video_produced",
        "Video produced (2 minutes, script, voice and edit)",
        "Marketing",
        "Marketing specialist",
        240,
        600,
        1440,
        30,
        "marketing_specialist",
    ),
)

#: Every built-in unit of both catalogs. Lookups use the union so a run recorded
#: under either catalog keeps its figures whatever the mode; listings use
#: :func:`builtin_baselines`, the mode's own catalog.
BASELINES: tuple[Baseline, ...] = CTEM_BASELINES + CORPORATE_BASELINES
BASELINES_BY_UNIT: dict[str, Baseline] = {b.unit: b for b in BASELINES}
#: Every built-in rate role of both catalogs (lookups), :func:`builtin_rates` for listings.
HOURLY_RATES: dict[str, dict[str, HourlyRate]] = {**CTEM_HOURLY_RATES, **CORPORATE_HOURLY_RATES}

#: The role a unit without a known rate falls back to, per mode.
CTEM_FALLBACK_ROLE = "analyst"
CORPORATE_FALLBACK_ROLE = "knowledge_worker"


def builtin_baselines(*, xtm_one: bool) -> tuple[Baseline, ...]:
    """The built-in catalog a mode shows: CTEM in XTM One mode, corporate in Copilot mode."""
    return CTEM_BASELINES if xtm_one else CORPORATE_BASELINES


def builtin_rates(*, xtm_one: bool) -> dict[str, dict[str, HourlyRate]]:
    """The built-in rate roles a mode shows."""
    return CTEM_HOURLY_RATES if xtm_one else CORPORATE_HOURLY_RATES


def fallback_rate_role(*, xtm_one: bool) -> str:
    """The role whose rate a unit with an unknown role is valued at, per mode."""
    return CTEM_FALLBACK_ROLE if xtm_one else CORPORATE_FALLBACK_ROLE


@dataclass(frozen=True)
class UnitDeclaration:
    """The unit an agent's runs produce and where its count is read."""

    unit: str
    count_path: str


def unit_declaration(output_schema: Mapping[str, Any] | None) -> UnitDeclaration | None:
    """The ROI unit an ``output_schema`` declares, ``None`` when it declares none.

    Accepts the agent shape (the JSON Schema itself) and the dataset shape
    (``{"type": "json_schema", "definition": {...}}``).
    """
    if not isinstance(output_schema, Mapping):
        return None
    definition = output_schema.get("definition") if output_schema.get("type") == "json_schema" else output_schema
    if not isinstance(definition, Mapping):
        return None
    raw = definition.get(ROI_SCHEMA_KEYWORD)
    if not isinstance(raw, Mapping):
        return None
    unit, count = raw.get("unit"), raw.get("count")
    if not isinstance(unit, str) or not unit or not isinstance(count, str) or not count:
        return None
    return UnitDeclaration(unit=unit, count_path=count)


def declaration_error(
    output_schema: Mapping[str, Any] | None, *, known_units: Iterable[str] | None = None
) -> str | None:
    """Why the schema's ``x-xtm-roi`` is unusable, ``None`` when absent or valid."""
    if not isinstance(output_schema, Mapping):
        return None
    definition = output_schema.get("definition") if output_schema.get("type") == "json_schema" else output_schema
    if not isinstance(definition, Mapping) or ROI_SCHEMA_KEYWORD not in definition:
        return None
    declaration = unit_declaration(output_schema)
    if declaration is None:
        return f"{ROI_SCHEMA_KEYWORD} needs a non-empty 'unit' and 'count' (a dot path in the output)"
    units = set(known_units) if known_units is not None else set(BASELINES_BY_UNIT)
    if declaration.unit not in units:
        return f"{ROI_SCHEMA_KEYWORD}.unit '{declaration.unit}' is not a known unit"
    return None


def count_units(declaration: UnitDeclaration, output: Any) -> int:
    """Units a structured output carries at the declared path (0 when absent or malformed)."""
    node: Any = output
    for part in declaration.count_path.split("."):
        if isinstance(node, Mapping) and part in node:
            node = node[part]
        else:
            return 0
    if isinstance(node, bool):
        return 0
    if isinstance(node, list):
        return len(node)
    if isinstance(node, int):
        return max(node, 0)
    # One artefact: a bundle id, a rule body, a briefing headline.
    if isinstance(node, str):
        return 1 if node.strip() else 0
    if isinstance(node, Mapping):
        return 1 if node else 0
    return 0


@dataclass(frozen=True)
class RunMeasure:
    """What one run produced and cost, as recorded at its end."""

    unit: str
    units: int
    llm_cost: float = 0.0
    duration_s: float = 0.0
    #: ``None`` while unreviewed: counted like ``accepted`` for value, reported apart.
    outcome: str | None = OUTCOME_ACCEPTED
    #: Measured review minutes (approval wait, edits); ``None`` falls back to the baseline.
    review_min: float | None = None
    #: The record of a work item (``per_work_item`` counting), not of a run (#9810).
    work_item: bool = False
    #: Model spend of an agent with no recorded run in the window (#10441): it counts in
    #: ``llm_cost`` only, never as a run, a unit or an unknown unit.
    cost_only: bool = False


#: The unit key and name of the spend no recorded run carries (#10441): the "By unit of work" row
#: "Cost not tied to a unit", so the unit rows still sum to the totals.
COST_ONLY_UNIT = "_cost_not_tied_to_a_unit"
COST_ONLY_NAME = "Cost not tied to a unit"


@dataclass
class RoiFigures:
    """The ROI of a set of runs over a period, every figure with its assumptions."""

    currency: str
    runs: int = 0
    units: int = 0
    minutes_saved: float = 0.0
    llm_cost: float = 0.0
    value: float = 0.0
    net_value: float = 0.0
    roi: float | None = None
    payback_months: float | None = None
    #: The same figures at the catalog's low and high manual minutes (the likely ones above), so a
    #: screen draws "12 h (8 to 17)" from the report alone (#9836 R4a, design 10.5). The review
    #: minutes, the rate and the model cost are the same at both ends.
    minutes_saved_low: float = 0.0
    minutes_saved_high: float = 0.0
    value_low: float = 0.0
    value_high: float = 0.0
    net_value_low: float = 0.0
    net_value_high: float = 0.0
    roi_low: float | None = None
    roi_high: float | None = None
    #: Payback is shortest at the high end: ``payback_months_low`` comes from the high net value.
    payback_months_low: float | None = None
    payback_months_high: float | None = None
    units_by_kind: dict[str, int] = field(default_factory=dict)
    assumptions: list[str] = field(default_factory=list)
    #: The same assumptions as structured facts, so a client renders them in its own language (#9718).
    assumption_facts: list[dict[str, Any]] = field(default_factory=list)
    #: How the figures are reached, with their numbers: one ``saving`` fact per unit, the ``total``,
    #: the ``review`` count and the ``rejected`` runs left out (design standards s6, #9810).
    computation: list[dict[str, Any]] = field(default_factory=list)
    #: Runs a person reviewed (any outcome), runs nobody reviewed, runs rejected (#9310).
    reviewed: int = 0
    unreviewed: int = 0
    rejected: int = 0
    #: Of ``runs``, the records of a work item, and of ``unreviewed``, those of a work item: a
    #: view that lists runs says the rest apart (#9810).
    work_items: int = 0
    unreviewed_work_items: int = 0

    @property
    def hours_saved(self) -> float:
        return round(self.minutes_saved / 60.0, 2)

    @property
    def hours_saved_low(self) -> float:
        return round(self.minutes_saved_low / 60.0, 2)

    @property
    def hours_saved_high(self) -> float:
        return round(self.minutes_saved_high / 60.0, 2)

    @property
    def review_rate(self) -> float | None:
        """``reviewed / runs``; ``None`` without a run."""
        return round(self.reviewed / self.runs, 4) if self.runs else None


def compute_roi(
    measures: Iterable[RunMeasure],
    *,
    currency: str = "USD",
    baselines: Mapping[str, Baseline] | None = None,
    rates: Mapping[str, Mapping[str, HourlyRate]] | None = None,
    period_days: int | None = None,
    setup_cost: float = 0.0,
    overridden_units: Iterable[str] = (),
) -> RoiFigures:
    """The ROI of ``measures`` over a period of ``period_days`` days.

    Per run: ``minutes_saved = units x (manual_likely - review)``, the review
    being the measured one when recorded, else the baseline's; a rejected run
    saves nothing and still costs its LLM spend. ``value`` is the minutes at
    the loaded rate of the unit's role, ``net_value = value - llm_cost``,
    ``roi = net_value / llm_cost`` and ``payback_months = setup_cost / (net
    value per month)`` (``None`` when the net is not positive, ``0`` without a
    setup cost). A unit the catalog does not know counts in ``runs`` and
    ``llm_cost`` only, and is listed in the assumptions. An unreviewed run
    (``outcome`` ``None``) counts like an accepted one for value and is
    counted in ``unreviewed``; ``reviewed`` counts every other run.
    """
    catalog = baselines if baselines is not None else BASELINES_BY_UNIT
    rate_table = rates if rates is not None else HOURLY_RATES
    overridden = set(overridden_units)
    figures = RoiFigures(currency=currency)
    used: set[str] = set()
    unknown: set[str] = set()
    measured_review = False
    # Per unit: units counted, manual and review minutes, minutes saved, value (the formula lines, #9810 r3).
    per_unit: dict[str, dict[str, float]] = {}
    for m in measures:
        figures.llm_cost += max(m.llm_cost, 0.0)
        if m.cost_only:
            continue
        figures.runs += 1
        if m.work_item:
            figures.work_items += 1
        if m.outcome is None:
            figures.unreviewed += 1
            if m.work_item:
                figures.unreviewed_work_items += 1
        else:
            figures.reviewed += 1
            if m.outcome == OUTCOME_REJECTED:
                figures.rejected += 1
        baseline = catalog.get(m.unit)
        if baseline is None:
            unknown.add(m.unit)
            continue
        if m.outcome == OUTCOME_REJECTED or m.units <= 0:
            continue
        figures.units += m.units
        figures.units_by_kind[m.unit] = figures.units_by_kind.get(m.unit, 0) + m.units
        if m.review_min is not None:
            measured_review = True
            review_total = max(m.review_min, 0.0)
        else:
            review_total = baseline.review_min * m.units
        saved = max(baseline.manual_min_likely * m.units - review_total, 0.0)
        saved_low = max(baseline.manual_min_low * m.units - review_total, 0.0)
        saved_high = max(baseline.manual_min_high * m.units - review_total, 0.0)
        figures.minutes_saved += saved
        figures.minutes_saved_low += saved_low
        figures.minutes_saved_high += saved_high
        rate = rate_table[baseline.rate_role][currency]
        figures.value += saved / 60.0 * rate.amount
        figures.value_low += saved_low / 60.0 * rate.amount
        figures.value_high += saved_high / 60.0 * rate.amount
        used.add(m.unit)
        line = per_unit.setdefault(
            m.unit, {"units": 0, "manual_min": 0.0, "review_min": 0.0, "minutes_saved": 0.0, "value": 0.0}
        )
        line["units"] += m.units
        line["manual_min"] += baseline.manual_min_likely * m.units
        line["review_min"] += review_total
        line["minutes_saved"] += saved
        line["value"] += saved / 60.0 * rate.amount
    figures.net_value = figures.value - figures.llm_cost
    figures.net_value_low = figures.value_low - figures.llm_cost
    figures.net_value_high = figures.value_high - figures.llm_cost
    figures.roi = _roi(figures.net_value, figures.llm_cost)
    figures.roi_low = _roi(figures.net_value_low, figures.llm_cost)
    figures.roi_high = _roi(figures.net_value_high, figures.llm_cost)
    figures.payback_months = _payback(setup_cost, figures.net_value, period_days)
    figures.payback_months_low = _payback(setup_cost, figures.net_value_high, period_days)
    figures.payback_months_high = _payback(setup_cost, figures.net_value_low, period_days)
    figures.minutes_saved = round(figures.minutes_saved, 1)
    figures.minutes_saved_low = round(figures.minutes_saved_low, 1)
    figures.minutes_saved_high = round(figures.minutes_saved_high, 1)
    figures.value = round(figures.value, 2)
    figures.value_low = round(figures.value_low, 2)
    figures.value_high = round(figures.value_high, 2)
    figures.llm_cost = round(figures.llm_cost, 4)
    figures.net_value = round(figures.net_value, 2)
    figures.net_value_low = round(figures.net_value_low, 2)
    figures.net_value_high = round(figures.net_value_high, 2)
    figures.assumptions = _assumptions(used, unknown, overridden, measured_review, currency, catalog, rate_table)
    figures.assumption_facts = _assumption_facts(
        used, unknown, overridden, measured_review, currency, catalog, rate_table
    )
    figures.computation = _computation_facts(figures, per_unit, catalog, rate_table)
    return figures


def _roi(net_value: float, llm_cost: float) -> float | None:
    """``net_value / llm_cost``; ``None`` without a model cost."""
    return round(net_value / llm_cost, 2) if llm_cost > 0 else None


def _payback(setup_cost: float, net_value: float, period_days: int | None) -> float | None:
    """Months to recover ``setup_cost`` at ``net_value`` per period: ``0`` without a setup cost,
    ``None`` when the net is not positive (or the period is unbounded)."""
    if setup_cost <= 0:
        return 0.0 if net_value > 0 else None
    if net_value > 0 and period_days:
        return round(setup_cost / (net_value / period_days * 30.0), 2)
    return None


def _computation_facts(
    figures: RoiFigures,
    per_unit: Mapping[str, Mapping[str, float]],
    catalog: Mapping[str, Baseline],
    rate_table: Mapping[str, Mapping[str, HourlyRate]],
) -> list[dict[str, Any]]:
    """How the figures are reached, with their numbers (design standards s6, UX #9810 r3 B2).

    One ``saving`` fact per unit (units x manual minutes, minus the review minutes, the minutes
    saved at the role's rate), the ``total`` (hours saved, value, model cost, net value), the
    ``review`` count behind the review rate and, when any, the ``rejected`` runs left out.
    """
    if figures.runs == 0:
        return []
    facts: list[dict[str, Any]] = []
    for unit in sorted(per_unit):
        line = per_unit[unit]
        b = catalog[unit]
        facts.append(
            {
                "kind": "saving",
                "unit": unit,
                "name": b.name,
                "units": int(line["units"]),
                "manual_min": round(line["manual_min"], 1),
                "review_min": round(line["review_min"], 1),
                "minutes_saved": round(line["minutes_saved"], 1),
                "rate": rate_table[b.rate_role][figures.currency].amount,
                "value": round(line["value"], 2),
                "currency": figures.currency,
            }
        )
    facts.append(
        {
            "kind": "total",
            "minutes_saved": figures.minutes_saved,
            "hours_saved": figures.hours_saved,
            "value": figures.value,
            "llm_cost": figures.llm_cost,
            "net_value": figures.net_value,
            "currency": figures.currency,
        }
    )
    facts.append({"kind": "review", "reviewed": figures.reviewed, "total": figures.runs})
    if figures.rejected:
        facts.append({"kind": "rejected", "count": figures.rejected})
    return facts


def _assumptions(
    used: set[str],
    unknown: set[str],
    overridden: set[str],
    measured_review: bool,
    currency: str,
    catalog: Mapping[str, Baseline],
    rate_table: Mapping[str, Mapping[str, HourlyRate]],
) -> list[str]:
    notes: list[str] = []
    for unit in sorted(used):
        b = catalog[unit]
        label = LABEL_OVERRIDE if unit in overridden else b.minutes_label
        notes.append(f"{b.name}: {b.manual_min_likely:g} manual min, {b.review_min:g} review min per unit ({label})")
    for role in sorted({catalog[u].rate_role for u in used}):
        r = rate_table[role][currency]
        notes.append(f"{role} rate {r.amount:g} {currency}/h ({r.label}: {r.source})")
    if measured_review:
        notes.append(f"review minutes: {LABEL_MEASURED} where recorded, baseline otherwise")
    if unknown:
        notes.append("no baseline for unit(s) " + ", ".join(sorted(unknown)) + ": cost counted, no time saved")
    return notes


def _assumption_facts(
    used: set[str],
    unknown: set[str],
    overridden: set[str],
    measured_review: bool,
    currency: str,
    catalog: Mapping[str, Baseline],
    rate_table: Mapping[str, Mapping[str, HourlyRate]],
) -> list[dict[str, Any]]:
    """The facts behind :func:`_assumptions`, one dict per line, no prose: ``kind`` then its values."""
    facts: list[dict[str, Any]] = []
    for unit in sorted(used):
        b = catalog[unit]
        facts.append(
            {
                "kind": "unit",
                "unit": unit,
                "name": b.name,
                "manual_min": b.manual_min_likely,
                "manual_min_low": b.manual_min_low,
                "manual_min_high": b.manual_min_high,
                "review_min": b.review_min,
                "label": LABEL_OVERRIDE if unit in overridden else b.minutes_label,
                # Where the minutes come from (a publication, or the reasoning of an assumption):
                # the source line of "How this is computed" (#9836 R4a, design 10.6).
                "source": b.minutes_source,
            }
        )
    for role in sorted({catalog[u].rate_role for u in used}):
        r = rate_table[role][currency]
        facts.append(
            {
                "kind": "rate",
                "role": role,
                "amount": r.amount,
                "currency": currency,
                "label": r.label,
                "source": r.source,
            }
        )
    if measured_review:
        facts.append({"kind": "measured_review"})
    if unknown:
        facts.append({"kind": "unknown_units", "units": sorted(unknown)})
    return facts
