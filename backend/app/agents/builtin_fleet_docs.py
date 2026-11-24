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

"""Seeded documentation of the built-in fleets (#9174, slice 6a).

One entry per slug of ``BUILTIN_FLEETS``, in the sections of
``app.services.fleet_docs.DOC_SECTIONS`` (markdown). The seed writes it on the
built-in fleet (:func:`app.services.fleet_docs.seed_documentation`, called by
``seed_builtin_fleets`` when the fleet entry carries no ``documentation`` of
its own); it is read only there and copied by a fork as the fork's version 1.

The text explains what the fleet does, never what a customer's run produced:
no figure, no organization, no person. The prose sections the About page
retired (#10123: members, approvals, outcomes, units, quota, switch-off) are
not seeded: their detail lives in the fleet's other tabs. Editing a text
here makes the next boot write a new version of the built-in fleet's
documentation; the structure fingerprint follows the seed by
itself.
"""

from __future__ import annotations

from typing import Any

_OPT_IN = (
    "Every automation of this fleet ships switched off. An administrator switches each one on "
    "from the fleet page (it then runs as that administrator, who needs the module integrations "
    "it reads). Until one is on, the fleet is inactive: its members answer as ordinary agents and "
    "the CTEM Assistant hands nothing over to it."
)

#: 1. The generic CTEM loop (CTEM-31, #1048): what it does on day one with the
#: customer's ecosystem. Absorbs the former threat response (F1), posture (F4)
#: and the three single-product desks, as roles.
CTEM_LOOP_DOC: dict[str, str] = {
    "purpose": (
        "Run the whole CTEM loop on your software, case by case: any change in exposure becomes a "
        "case, the case is validated, priced, decided once by you, mobilised and closed by a "
        "passing re-test. One fleet, every trigger path, every module in its role."
    ),
    "use_case": (
        "Day one, with OpenCTI, OpenAEV and OpenCRQ connected and the organization profile filled: "
        "a new threat, a vulnerability on software you run, a credential leak, a supplier incident "
        "or a regression is recorded as a signal, reconciled and gated on your scope. In scope, it "
        "opens a case on the command center: the Relevance role reads OpenCTI, the Validation role "
        "runs a read-only check in OpenAEV, the Risk role prices it in OpenCRQ, and the case waits "
        "for one decision of yours before tickets go out (Mobilize). The re-test closes it. Around "
        "the cases, the Posture role keeps the five stage assessments and the board report current, "
        "the Intelligence desk answers your PIRs in OpenCTI, the Validation desk measures detection "
        "coverage in OpenAEV and the Risk desk keeps the register priced in OpenCRQ. First result: "
        "the stage assessments on the command center within the first week; the first case within "
        "a day of the first in-scope signal once its automations are on."
    ),
    "triggers": (
        "- **Case opened or reopened**: a CTEM case opens, reopens on a failed re-test or breaches "
        "its SLA; one loop per case, on every trigger path (a use-case fleet takes the cases of its paths only while its own case automation is on). Cases opened before the fleet was enabled are taken on enable and on the hourly case sweep.\n"
        "- **Gate answered**: a person answers a gate a case waits on; the loop goes on from that step, "
        "or stops on a rejection.\n"
        "- **Loop reminder due**: a reminder the loop set on a case is due; the person a gate still waits "
        "on is reminded once.\n"
        "- **New relevant threat** and **Threat activity flagged in a PIR**: the OpenCTI intake; each "
        "delivery records a relevant threat signal, with no run of its own.\n"
        "- **Threat signal recorded**: a threat signal recorded or moved to 'to validate' is scored "
        "against the profile; a confirmed one goes to its case.\n"
        "- **Weekly threat sweep** and **Re-test due**: catches what the events missed, re-runs a "
        "validated scenario once a fix is deployed.\n"
        "- **Weekly assessments**: the five stage assessments and the exposure briefing, on by default.\n"
        "- **Event refresh** and **Monthly board report**: a closed case refreshes the stages it "
        "touched; the board report on the 1st of the month.\n"
        "- **PIR match created**: the Intelligence desk answers a PIR match in OpenCTI; with the "
        "**Weekly PIR answer**, the **Half-year threat landscape** and the **Stale indicator clean-up**.\n"
        "- **Simulation finished**: the Validation desk sorts every result of an OpenAEV simulation; "
        "with the **Weekly validation plan**, the **Weekly coverage report** and its own re-tests.\n"
        "- **Scenario re-priced**, **Finding re-scored** and **Control validation ingested**: the Risk "
        "desk explains what moved in OpenCRQ; with the **Weekly investment review** and the "
        "**Monthly Board pack**."
    ),
    "flow": (
        "1. Signal: any trigger path records a signal; reconcile and the scope gate decide case or watchlist.\n"
        "2. Prioritise: relevance and severity from the profile and the scope criticality.\n"
        "3. Validate: an OpenAEV check, read-only by default.\n"
        "4. Decide: the expected loss and the options from OpenCRQ.\n"
        "5. Mobilize: tickets, owners and the prepared change, after your approval.\n"
        "6. Re-test and close: the case closes only on a passing re-test, and feeds back into scope."
    ),
    "objects_created": (
        "CTEM signals and cases, OpenAEV scenarios, OpenCRQ loss estimates, drafted detections, "
        "remediation tickets (after your approval), stage assessments, exposure briefings, PIR "
        "answers, coverage reports and board packs."
    ),
    "prerequisites": (
        "OpenCTI, OpenAEV and OpenCRQ connected for the administrator who switches it on (each role "
        "works with its own module alone), and an organization profile (sector, regions, "
        "technology, crown jewels). " + _OPT_IN
    ),
}

CTEM_THREAT_RESPONSE_DOC: dict[str, str] = {
    "purpose": (
        "Turn a published threat into an approved, verified plan in days: is it relevant to the "
        "organization, can it work against us, what would it cost, and what do we do this week."
    ),
    "use_case": (
        "A new intrusion set, campaign or report lands in OpenCTI. The fleet scores its relevance "
        "against the organization profile (sector, regions, technology, priority intelligence "
        "requirements), proposes an OpenAEV scenario that reproduces it, prices the exposure in "
        "OpenCRQ and proposes the three actions of the week, each closed by a re-test."
    ),
    "triggers": (
        "- **New relevant threat**: an OpenCTI creation of an Intrusion Set or a Campaign records a relevant threat signal; no run of its own.\n"
        "- **Threat activity flagged in a PIR**: OpenCTI flags threat activity in an organization PIR and records a relevant threat signal; no run of its own.\n"
        "- **Threat signal recorded**: a relevant threat signal from OpenCTI is recorded, or the scope gate moves it to 'to validate'; the run scores its relevance against the profile. A confirmed signal opens its case through the case loop.\n"
        "- **Weekly threat sweep**: Mondays 07:00, catches what the events missed.\n"
        "- **Re-test due**: a re-test request opened once a fix is deployed re-runs the validated scenario.\n"
        "- **Case opened or reopened**: a CTEM case on any trigger path is opened, reopened by a failed "
        "re-test, or past its SLA, unless an active use-case fleet owns its path; the case is the record of the loop.\n"
        "- **Gate answered**: a person answers a gate the case waits on; the decision is written on the "
        "case and the loop goes on, or stops on a rejection.\n"
        "- **Loop reminder due**: a reminder set on a case is due; the person a gate still waits on is "
        "reminded once."
    ),
    "flow": (
        "1. Relevance: below the threshold the case closes as not relevant, with the reason.\n"
        "2. Validation: an OpenAEV scenario is proposed (approval), then its results are read.\n"
        "3. Risk: the expected loss is updated in OpenCRQ.\n"
        "4. Mobilization: the top three actions, the detections drafted and the ticket (approval).\n"
        "5. Re-test: the scenario runs again and closes the case."
    ),
    "objects_created": (
        "OpenAEV scenarios (after approval), OpenCRQ loss estimates, drafted detections, "
        "approval work items and remediation tickets (after approval)."
    ),
    "prerequisites": (
        "OpenCTI, OpenAEV and OpenCRQ connected for the administrator who switches it on, and "
        "an organization profile (sector, regions, technology, crown jewels). " + _OPT_IN
    ),
}

CTEM_VULN_PRIORITIZATION_DOC: dict[str, str] = {
    "purpose": (
        "Answer which vulnerabilities to fix on Monday: the ones that matter to the "
        "organization's assets, proven exploitable, priced, ticketed and closed by a re-test."
    ),
    "use_case": (
        "A CVE enters the known exploited list, its EPSS score jumps, or it is sighted. The fleet "
        "matches it to the organization's assets, validates exploitability in OpenAEV, scores "
        "the finding in OpenCRQ and drafts the remediation ticket."
    ),
    "triggers": (
        "- **New KEV or EPSS jump**: an OpenCTI creation or update of a Vulnerability.\n"
        "- **Vulnerability on software we run**: an OpenCTI has relationship to a Vulnerability whose source is in the software inventory or the profile assets, checked before any model call.\n"
        "- **Vulnerability flagged in a PIR**: OpenCTI flags a Vulnerability in an organization PIR; the run starts when its live PIR score reaches the policy threshold (70 by default, Settings).\n"
        "- **Vulnerability sighted**: an OpenCTI sighting, triaged at a higher priority.\n"
        "- **Daily backlog pass**: every day at 06:00, re-ranks the open cases.\n"
        "- **Re-test due**: a re-test request opened once the fix is deployed re-validates the exposure.\n"
        "- **Case opened or reopened**: while this automation is on, a CTEM case on the new vulnerability "
        "path is opened, reopened by a failed re-test, past its SLA, or an OpenAEV result it waits on arrived; "
        "the case is the record of the loop. Off, the CTEM loop runs those cases.\n"
        "- **OpenAEV results into cases**: listens to OpenAEV with no run of its own; a finished "
        "simulation or a changed finding moves the case it concerns, a degraded scenario score records "
        "a regression signal, an attack path reaching a critical asset records a proven-exploitable signal.\n"
        "- **Analyst review mark**: listens to OpenCTI with no run of its own; when the review gate "
        "waits, the analyst's label on the drafted Grouping approves its coverage request and resumes the run.\n"
        "- **Gate answered**: a person answers a gate the case waits on; the decision is written on the "
        "case and the loop goes on, or stops on a rejection."
    ),
    "flow": (
        "1. Asset match from the organization context: no matching asset closes the case, with the reason.\n"
        "2. Exploitability validated in OpenAEV (approval).\n"
        "3. Finding scored in OpenCRQ.\n"
        "4. Remediation ticket (approval).\n"
        "5. Re-test closes the case."
    ),
    "objects_created": (
        "OpenAEV validations (after approval), OpenCRQ finding scores, approval work items and "
        "remediation tickets (after approval)."
    ),
    "prerequisites": (
        "OpenCTI, OpenAEV and OpenCRQ connected, and the organization's assets and technology in "
        "its profile. " + _OPT_IN
    ),
}

CTEM_AI_SECURITY_DOC: dict[str, str] = {
    "purpose": (
        "Watch the threats aimed at AI systems and at AI-enabled tradecraft, map them to MITRE "
        "ATLAS, and test the organization's own AI systems behind approval."
    ),
    "use_case": (
        "A report labelled AI lands in OpenCTI. The fleet maps it to ATLAS techniques in an "
        "OpenCTI draft, checks which of the organization's AI systems it reaches, proposes "
        "OpenAEV AI tests (prompt injection, jailbreak, excessive agency, tool misuse, MCP tool "
        "poisoning) and tracks the exposure until it is fixed."
    ),
    "triggers": (
        "- **New AI-enabled report**: an OpenCTI creation of a Report labelled `ai`.\n"
        "- **Weekly AI threat watch**: Tuesdays 07:00, a summary of new AI campaigns.\n"
        "- **AI system registered**: a newly registered AI system asks for a baseline test proposal."
    ),
    "flow": (
        "1. ATLAS mapping in an OpenCTI draft, for an analyst's review.\n"
        "2. Relevance to the organization's AI systems.\n"
        "3. AI tests proposed, each behind approval.\n"
        "4. Exposure priced in OpenCRQ, fixes drafted, tracked to closure."
    ),
    "objects_created": ("OpenCTI drafts of ATLAS entities, approval work items, OpenAEV AI tests (after approval)."),
    "prerequisites": (
        "OpenCTI and OpenAEV connected, and the organization's AI systems listed in its profile "
        "(an AI system without an OpenAEV asset gets the proposal only). " + _OPT_IN
    ),
}

CTEM_POSTURE_DOC: dict[str, str] = {
    "purpose": (
        "Keep the CTEM posture current across OpenCTI, OpenAEV and OpenCRQ: the five stage "
        "assessments, the exposure briefing and the monthly board report."
    ),
    "use_case": (
        "The stage analysts assess scoping, discovery, prioritization, validation and "
        "mobilization every week; a case closed by another CTEM fleet refreshes the stages it "
        "touched; on the first of the month the orchestrator writes the board report."
    ),
    "triggers": (
        "- **Weekly assessments**: the weekly stage and briefing automations, unchanged.\n"
        "- **Event refresh**: a CTEM case closed by the threat response, vulnerability or AI "
        "security fleet, at most one refresh per stage per hour.\n"
        "- **Monthly board report**: the 1st of the month at 09:00."
    ),
    "flow": (
        "1. A closed case is mapped to the stages it changed.\n"
        "2. A stage refreshed less than an hour ago is skipped.\n"
        "3. Each remaining stage analyst refreshes its assessment.\n"
        "4. Monthly: stage scores and trend, fleet outcomes, loss avoided and ROI in one report."
    ),
    "objects_created": "Stage assessments, exposure briefings and the monthly board report.",
    "prerequisites": (
        "Read access to OpenCTI, OpenAEV and OpenCRQ for the administrator who switches the "
        "refresh and the report on. The weekly dashboard automations are on by default and stay "
        "as they are; the event refresh and the board report ship switched off."
    ),
}

CTEM_THIRD_PARTY_RISK_DOC: dict[str, str] = {
    "purpose": (
        "Watch the organization's tracked suppliers, customers and partners, price a supplier "
        "exposure and propose actions behind approval. Never contacts a third party."
    ),
    "use_case": (
        "A report or an incident in OpenCTI names a tracked organization. For a supplier, the "
        "fleet builds a case: the threat or breach, the services the supplier provides and the "
        "crown jewels behind them, the expected loss from the primary organization's OpenCRQ "
        "scenarios, and the proposed actions. A customer or partner gets an awareness case only."
    ),
    "triggers": (
        "- **Tracked organization named**: an OpenCTI creation of a Report or an Incident that "
        "names a tracked organization.\n"
        "- **Weekly third-party review**: Wednesdays 07:00."
    ),
    "flow": (
        "1. Relevance of the event to the tracked organization.\n"
        "2. Dependency: the services supplied and the primary assets they support.\n"
        "3. Expected loss read in OpenCRQ through that supporting asset.\n"
        "4. Actions proposed (contact the supplier, compensating control, contract clause, "
        "monitoring raised), each an approval item for a person."
    ),
    "objects_created": "Supplier-exposure cases, awareness cases and approval work items.",
    "prerequisites": (
        "OpenCTI and OpenCRQ connected, and the tracked organizations with their relationship "
        "and monitoring switched on in the organization settings. " + _OPT_IN
    ),
}

OPENCTI_PIR_DESK_DOC: dict[str, str] = {
    "purpose": (
        "Give every priority intelligence requirement (PIR) of the organization a ranked, current "
        "answer the CTI team can act on, instead of a flow of reports to read. Works with OpenCTI alone."
    ),
    "use_case": (
        "A CTI team runs a PIR programme in OpenCTI. When a report, an intrusion set, a campaign or a "
        "vulnerability flagged in one of the PIRs is created, the fleet scores it against each PIR and "
        "the organization profile, files what matters in one standing case per PIR, and builds a hunt "
        "package and detections for the top threats. Every Monday each PIR gets its answer: the threats "
        "ranked and what changed. Twice a year, a threat landscape report across every PIR. Stale "
        "indicators of the cases are re-checked and their revocation proposed to a person."
    ),
    "triggers": (
        "- **PIR match created**: an OpenCTI creation of a Report, an Intrusion Set, a Campaign or a "
        "Vulnerability flagged in one of the organization's PIRs.\n"
        "- **Weekly PIR answer**: Mondays 07:00.\n"
        "- **Half-year threat landscape**: 1 January and 1 July, 08:00.\n"
        "- **Stale indicator clean-up**: the 1st of each month, 06:00.\n"
        "- **Gate answered**: a person answers an indicator revocation; it is revoked or kept as decided."
    ),
    "flow": (
        "1. Triage: the PIR Triage Analyst scores the new entity against each PIR, with the evidence "
        "of each criterion met (sectors and regions today; any other criteria type a PIR carries).\n"
        "2. File: each match worth it goes into the standing OpenCTI case of its PIR.\n"
        "3. Hunt and detect: for a top threat, the OpenCTI Threat Hunt Creator builds the hunt package "
        "and the OpenCTI Detection Rule Converter its detections.\n"
        "4. Answer: the Landscape Reporter writes the weekly answer of each PIR and the half-year "
        "landscape report.\n"
        "5. Clean up: the Indicator Decay Guardian re-checks stale indicators; each revocation is an "
        "approval item for a person."
    ),
    "objects_created": (
        "One OpenCTI case per PIR, hunt packages, detection rules, weekly PIR answers, a half-year "
        "OpenCTI landscape report and approval work items for indicator revocations."
    ),
    "prerequisites": (
        "OpenCTI connected, with at least one PIR, and the organization's sector and regions in its profile. " + _OPT_IN
    ),
}

OPENAEV_DETECTION_DESK_DOC: dict[str, str] = {
    "purpose": (
        "Prove that the organization's EDR and SIEM work: measured prevention, detection and logging "
        "coverage, every gap closed and proven closed by a re-test. Works with OpenAEV alone."
    ),
    "use_case": (
        "A SOC or detection engineering team runs OpenAEV. Every week the fleet plans simulations from the "
        "threat arsenal against the crown jewel assets and proposes each launch to a person. When a "
        "simulation ends, it sorts every inject and asset into blocked, detected, logged or missed, finds "
        "the root cause of each gap (no rule, no log, or a broken collector such as one silent for weeks), "
        "drafts the missing detection rule, raises the remediation and re-tests the gap once it is fixed. "
        "Every Friday, a coverage report with the gaps still open and the gaps proven closed."
    ),
    "triggers": (
        "- **Simulation finished**: an OpenAEV simulation ends (provisioned OpenAEV trigger).\n"
        "- **Weekly validation plan**: Mondays 06:00.\n"
        "- **Weekly coverage report**: Fridays 08:00.\n"
        "- **Re-test due**: a work item keyed `openaev:detection-desk:retest:` once a remediation is done.\n"
        "- **Gate answered**: a person answers a launch approval; the simulation is launched or the refusal recorded."
    ),
    "flow": (
        "1. Plan: the orchestrator picks the techniques with no recent result against the crown jewels; the "
        "OpenAEV Scenario Creator, Payload Creator and Scenario Publisher build what is missing. Each launch "
        "waits for a person.\n"
        "2. Sort: the Coverage Gap Analyst sorts the results into blocked, detected, logged and missed, with "
        "the prevention, detection and logging controls that failed.\n"
        "3. Root cause: no rule, no log, a broken collector or a matching defect, with the evidence.\n"
        "4. Rule: for a missing rule, the Rule Type Detector picks the language and the Splunk, CrowdStrike "
        "or AEV detection rule generator drafts it.\n"
        "5. Remediate: one remediation work item per gap.\n"
        "6. Re-test: the simulation runs again; the gap closes only when the result shows the control. The "
        "Coverage Reporter reports the coverage every week."
    ),
    "objects_created": (
        "OpenAEV scenarios and payloads, detection rules (SPL, CrowdStrike, Sigma), remediation and re-test "
        "work items, approval work items for each launch and a weekly coverage report."
    ),
    "prerequisites": (
        "OpenAEV connected, with assets and at least one security platform collector, and the organization's "
        "crown jewels and software in its profile. " + _OPT_IN
    ),
}

OPENCRQ_RISK_BUDGET_DESK_DOC: dict[str, str] = {
    "purpose": (
        "Defend the security budget in money: a risk register priced in expected annual loss that stays "
        "current, and the controls to fund ranked by the loss each removes per dollar. Works with OpenCRQ "
        "alone, on its own threat catalogue; OpenCTI is never required."
    ),
    "use_case": (
        "A CISO or a risk manager runs OpenCRQ and has to justify the security budget to the CFO, the Board "
        "or an insurer. When OpenCRQ re-prices a scenario, re-scores the findings or ingests a control "
        "validation, the fleet re-reads the register and explains what moved and why. Every Monday it runs "
        "the what-if of the next control upgrades and writes one investment case per control, ranked by "
        "loss removed per dollar, against the treatment plans already open. On the 1st of the month it "
        "produces the Board pack: the expected annual loss trend, the top controls by loss removed, and the "
        "progress of the treatment plans and budget reviews."
    ),
    "triggers": (
        "- **Scenario re-priced**: an OpenCRQ simulation or price recompute job succeeded.\n"
        "- **Finding re-scored**: an OpenCRQ finding score recompute job succeeded.\n"
        "- **Control validation ingested**: OpenCRQ ingested OpenAEV findings or recorded a proven posture.\n"
        "- **Weekly investment review**: Mondays 07:00.\n"
        "- **Monthly Board pack**: the 1st of the month at 08:00.\n"
        "- **Gate answered**: a person answers an investment recommendation; the decision is recorded."
    ),
    "flow": (
        "1. Refresh: on an OpenCRQ event, the orchestrator compares the register with its last snapshot.\n"
        "2. Explain: the Register Analyst names each scenario re-priced, its expected annual loss before "
        "and after, and the driver (frequency, magnitude, a control, an asset).\n"
        "3. What-if: the Investment Analyst prices the control upgrades from OpenCRQ's what-if and ranks "
        "one investment case per control by loss removed per dollar.\n"
        "4. Recommend: a control worth funding that no treatment plan covers becomes a recommendation work "
        "item for a person; XTM One never writes a treatment plan or a budget review.\n"
        "5. Report: the Board Reporter writes the monthly Board pack. A new exposure with no scenario goes "
        "to the Risk Quantifier, who drafts one."
    ),
    "objects_created": (
        "Register snapshots in the fleet's shared state, investment cases, recommendation work items, "
        "approval work items for any proposed register change, and a monthly Board pack."
    ),
    "prerequisites": (
        "OpenCRQ connected, with risk scenarios simulated at least once, and the organization's currency and "
        "risk appetite in its profile. OpenCRQ is in beta until its general availability (February 2027); "
        "XTM One reads its budget reviews and writes a treatment plan only through the register write gate. " + _OPT_IN
    ),
}

#: Built-in fleet slug to its seeded documentation: the four seeded fleets (CTEM-31). The texts of the retired fleets above stay
#: as the role descriptions of the generic fleet's folded roles.
BUILTIN_FLEET_DOCS: dict[str, dict[str, str]] = {
    "ctem-loop": CTEM_LOOP_DOC,
    "ctem-vuln-prioritization": CTEM_VULN_PRIORITIZATION_DOC,
    "ctem-ai-security": CTEM_AI_SECURITY_DOC,
    "ctem-third-party-risk": CTEM_THIRD_PARTY_RISK_DOC,
}


def builtin_fleet_documentation(entry: dict[str, Any]) -> dict[str, str] | None:
    """The documentation the seed writes for the built-in fleet *entry*: its own, else the shipped one."""
    return entry.get("documentation") or BUILTIN_FLEET_DOCS.get(entry.get("slug") or "")


# Structured parts (About page redesign S1, ``app.services.fleet_docs.StructuredDoc``):
# what the About page draws as trigger cards, a stepper, tables, a live
# checklist and a quota range. Run counts are an estimate per trigger, labelled
# as such on the page; they never come from a customer's runs.

_INTEGRATIONS = "/integrations"

#: The card of Settings > Organization that edits each profile field a prerequisite reads,
#: and the tab that shows it (frontend ``lib/organization-profile-tabs.ts``: PROFILE_FIELD_CARD,
#: PROFILE_CARD_TAB). The first tab is the bare section path.
_PROFILE_FIELD_TAB: dict[str, tuple[str, str]] = {
    "sector": ("identity", "identity"),
    "regions": ("identity", "identity"),
    "crown_jewels": ("business", "crown_jewels"),
    "tracked_organizations": ("business", "tracked_organizations"),
    "tech_stack": ("technology", "tech_stack"),
    "ai_systems": ("technology", "ai_systems"),
}


def profile_fix_route(field: str) -> str:
    """The page that fixes a missing profile *field*: its tab of Settings > Organization, landed on its card."""
    tab, card = _PROFILE_FIELD_TAB[field]
    base = "/settings/organization" if tab == "identity" else f"/settings/organization/profile/{tab}"
    return f"{base}?card={card}"


def _prereq(check: str, label: str, fix_route: str) -> dict[str, Any]:
    return {"check": check, "label": label, "fix_route": fix_route}


_NEEDS_OPENCTI = _prereq("integration:opencti", "OpenCTI connected for you", _INTEGRATIONS)
_NEEDS_OPENAEV = _prereq("integration:openaev", "OpenAEV connected for you", _INTEGRATIONS)
_NEEDS_OPENCRQ = _prereq("integration:opencrq", "OpenCRQ connected for you", _INTEGRATIONS)
_NEEDS_SECTOR = _prereq("profile:sector", "Sector in the organization profile", profile_fix_route("sector"))
_NEEDS_REGIONS = _prereq("profile:regions", "Regions in the organization profile", profile_fix_route("regions"))
_NEEDS_JEWELS = _prereq(
    "profile:crown_jewels", "Crown jewels in the organization profile", profile_fix_route("crown_jewels")
)
_NEEDS_TECH = _prereq("profile:tech_stack", "Technology in the organization profile", profile_fix_route("tech_stack"))
_SWITCHED_ON = _prereq("manual", "An administrator switches each automation on", "")

#: Gate impact classes (#9769, ``app.services.fleet_autonomy``): ``low`` stops
#: waiting at the Medium autonomy level, ``high`` at Low, ``destructive`` only
#: at Full autonomous.
#: ``tools`` (#9769 S1b): the tool calls that are the gated action, paused
#: in the tool path while the fleet's level gates it.
#: Seeded fleets ship at Low (#10005), so the class says what still waits:
#: an OpenAEV launch executes payloads on the organization's endpoints or AI
#: systems, which the fleet cannot tell from production, and an executed
#: inject cannot be undone: ``destructive``. An action on a supplier (contact,
#: contract clause) leaves the platform irreversibly: ``destructive``. A
#: remediation ticket is a reversible internal write: ``low``.
_LAUNCH_IMPACT = "destructive"
_OPENAEV_LAUNCH_TOOLS = [
    {"name": "manage_openaev_simulation", "when": {"action": ["status"], "status": ["RUNNING"]}},
    {"name": "manage_openaev_atomic_testing", "when": {"action": ["launch", "relaunch"]}},
]
#: The coverage request of the review step (CTEM-16, #1089): OpenAEV builds and
#: schedules its scenario from it, so it is what an analyst review would hold.
_COVERAGE_REQUEST_TOOLS = [
    {"name": "manage_opencti_security_coverage", "when": {"action": ["create"]}},
]
#: The analyst review (#1480): validating the OpenCTI draft that holds the threat
#: package and its coverage publishes both, and only then does the coverage reach
#: OpenAEV. One decision with the coverage request: the approved request's answer
#: names its draft, and that grant lets the draft's validation run without a
#: second pause. A fleet with this gate is offered apply_opencti_draft with Live
#: writes off (``fleet_autonomy.gate_tool_names``).
_THREAT_PACKAGE_REVIEW_TOOLS = [
    *_COVERAGE_REQUEST_TOOLS,
    {"name": "apply_opencti_draft"},
]
_THREAT_PACKAGE_REVIEW_GRANT = {"objects": ["draft_id"], "fresh": []}
#: Every call that creates or starts an OpenAEV validation (Sam, 23 Nov 2026, #1339): the
#: gate ``ctem.validation.non_readonly`` of a fleet that has the OpenAEV validation step.
#: Reads (list, get, results, healthcheck, status reads) are not listed and stay free.
#: The coverage request is NOT one of them (#1467): it is the decision of the review
#: step, ``ctem.analyst_review`` alone, one decision and one gate key per call.
_OPENAEV_VALIDATION_TOOLS = [
    {"name": "create_openaev_scenario"},
    {"name": "import_openaev_scenario"},
    {"name": "manage_openaev_scenario", "when": {"action": ["launch", "schedule_recurrence", "duplicate"]}},
    {"name": "manage_openaev_simulation", "when": {"action": ["create", "duplicate"]}},
    *_OPENAEV_LAUNCH_TOOLS,
    {"name": "add_openaev_inject_to_scenario"},
    {"name": "manage_openaev_inject", "when": {"action": ["create", "duplicate", "trigger"]}},
    {"name": "create_openaev_atomic_testing"},
    {
        "name": "manage_openaev_atomic_testing",
        "when": {"action": ["create", "duplicate", "recurrence"]},
    },
]
#: One decision per validation (#1339): an approved call of the gate covers the later
#: calls of the same gate on the objects it named or created (a scenario, a simulation,
#: an atomic test, an inject). A call that names a new target or a new schedule asks again.
_OPENAEV_VALIDATION_GRANT = {
    "objects": ["scenario_id", "exercise_id", "inject_id", "parent_id"],
    "fresh": [
        "asset_ids",
        "asset_group_ids",
        "team_ids",
        "recurrence",
        "recurrence_cron",
        "recurrence_start",
        "recurrence_end",
        "start_date",
    ],
}
#: The action of the validation gate, in plain words (#1339).
CTEM_VALIDATION_ACTION = "Validate: create or launch an OpenAEV scenario or atomic test"
CTEM_VALIDATION_GATE = "ctem.validation.non_readonly"
_TICKET_TOOLS = [
    {"name": "create_jira_issue"},
    {"name": "create_servicenow_incident"},
    {"name": "create_servicenow_change"},
    # #917: the Mobilization role opens a case's ticket through the sync layer.
    {"name": "sync_ctem_case_ticket", "when": {"create": ["true"]}},
]

#: The CTEM loop gate catalog (#916, CTEM-34): one key per loop stage, declared
#: once and seeded on every CTEM loop fleet. Out of the box (the seeded Low
#: level) two gates wait per case (Sam, 23 Nov 2026, #1339): the OpenAEV
#: validation (``ctem.validation.non_readonly``, on a fleet that has the OpenAEV
#: validation step) and ``ctem.mobilize``, the ``destructive`` ones; every other
#: gate runs and the case records it as passed automatically.
#: Full human gates makes each of them wait, Full autonomous none, and a pin
#: (``Fleet.approval_pins``, writable on a built-in fleet, never written by the
#: seed) moves one gate either way and survives a seed upgrade. The former
#: tool gates map onto these keys: the OpenAEV launches are
#: ``ctem.validation.non_readonly``, the remediation tickets and a supplier
#: action are ``ctem.mobilize``.
CTEM_GATE_KEYS: tuple[str, ...] = (
    "ctem.validation.non_readonly",
    "ctem.analyst_review",
    "ctem.remediation.decision",
    "ctem.mobilize",
    "ctem.production_change",
    "ctem.accept_risk",
    "ctem.close_without_retest",
)
CTEM_MOBILIZE_GATE = "ctem.mobilize"


#: The OpenCRQ treatment plan writes (CTEM-18, #1055). On a CTEM loop fleet they
#: record the remediation decision, so they follow ``ctem.remediation.decision``
#: (runs out of the box, a pin makes it wait); on the risk budget desk they are
#: ``opencrq.register.write``.
OPENCRQ_TREATMENT_PLAN_WRITE_TOOLS: list[dict[str, str]] = [
    {"name": "create_opencrq_treatment_plan"},
    {"name": "update_opencrq_treatment_plan"},
    {"name": "set_opencrq_treatment_plan_moves"},
    {"name": "add_opencrq_treatment_plan_moves"},
]


def _ctem_gates(*, launch_tools: bool, mobilize_tools: bool, mobilize_action: str) -> list[dict[str, Any]]:
    """The CTEM loop gate catalog of one fleet, in loop order (:data:`CTEM_GATE_KEYS`).

    *launch_tools*: the fleet has the OpenAEV validation step; its validation gate
    then covers every creating or starting call and waits out of the box
    (``destructive``, #1339). Without it the gate is stated to the agent only.
    """

    def gate(key: str, action: str, module: str, impact: str, tools: list | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {"key": key, "action": action, "module": module, "approver": "Fleet manager"}
        out["impact"] = impact
        if tools:
            out["tools"] = [
                {**t, "when": {k: list(v) for k, v in t["when"].items()}} if "when" in t else dict(t) for t in tools
            ]
        return out

    validation = gate(
        CTEM_VALIDATION_GATE,
        CTEM_VALIDATION_ACTION,
        "openaev",
        _LAUNCH_IMPACT if launch_tools else "high",
        _OPENAEV_VALIDATION_TOOLS if launch_tools else None,
    )
    if launch_tools:
        validation["grant"] = {k: list(v) for k, v in _OPENAEV_VALIDATION_GRANT.items()}
    review = gate(
        "ctem.analyst_review",
        "Review: an analyst checks the threat package, then its OpenCTI draft is validated and its security "
        "coverage requested: approving publishes the package and sends the coverage to OpenAEV",
        "opencti",
        "low",
        _THREAT_PACKAGE_REVIEW_TOOLS,
    )
    review["grant"] = {k: list(v) for k, v in _THREAT_PACKAGE_REVIEW_GRANT.items()}
    return [
        validation,
        review,
        gate(
            "ctem.remediation.decision",
            "Decide: choose the remediation option and record it as an OpenCRQ treatment plan",
            "platform",
            "high",
            OPENCRQ_TREATMENT_PLAN_WRITE_TOOLS,
        ),
        gate(CTEM_MOBILIZE_GATE, mobilize_action, "platform", "destructive", _TICKET_TOOLS if mobilize_tools else None),
        gate("ctem.production_change", "Apply a change on production through an integration", "platform", "high"),
        gate("ctem.accept_risk", "Accept a residual risk beyond the policy cap", "platform", "high"),
        gate("ctem.close_without_retest", "Close a case without a passing re-test", "platform", "high"),
    ]


_MOBILIZE_ACTION = "Mobilize: create the tickets, notify the owners, hand over the prepared change"

CTEM_THREAT_RESPONSE_STRUCTURED: dict[str, Any] = {
    "purpose_line": "Turn a published threat into an approved, verified plan in days.",
    "outcome_line": "A threat triaged, a scenario run, the expected loss updated, actions proven by a re-test.",
    "triggers": [
        {
            "module": "opencti",
            "name": "New relevant threat",
            # One card for the two listen-only OpenCTI intakes (New relevant threat and Threat activity
            # flagged in a PIR): the sheet caps its triggers (fleet_datasheet.CAPS) and neither runs.
            "filter": "An intrusion set or a campaign is created, or threat activity is flagged in an organization PIR",
            "detail": "Records a relevant threat signal, no run of its own",
        },
        {
            "module": "platform",
            "name": "Threat signal recorded",
            "filter": "A relevant threat signal is recorded or moved to 'to validate'",
            "detail": "One run per signal; a confirmed signal goes to its case",
        },
        {
            "module": "schedule",
            "name": "Weekly threat sweep",
            "filter": "Mondays at 07:00",
            "detail": "Catches what the events missed",
        },
        {
            "module": "work_item",
            "name": "Re-test due",
            "filter": "A fix is ready to be re-tested",
            "detail": "Opened by the remediation owner once the fix is deployed",
        },
        {
            "module": "platform",
            "name": "Case opened or reopened",
            "filter": "A threat, credential leak or third-party CTEM case opens, reopens or breaches its SLA",
            "detail": "One loop per case",
        },
        {
            "module": "platform",
            "name": "Gate answered",
            "filter": "A person answers a gate a case waits on",
            "detail": "The loop goes on from that step",
        },
        {
            "module": "platform",
            "name": "Loop reminder due",
            "filter": "A reminder the loop set on a case is due",
            "detail": "The person a gate still waits on is reminded once",
        },
    ],
    "steps": [
        {
            "title": "Relevance",
            "detail": "Scored against the organization profile; below the threshold the case closes with its reason.",
        },
        {
            "title": "Validation",
            "detail": "An OpenAEV scenario reproduces the threat, then its results are read.",
            "gate": True,
        },
        {"title": "Risk", "detail": "The expected loss is updated in OpenCRQ."},
        {"title": "Mobilization", "detail": "The top three actions, detections drafted and the ticket.", "gate": True},
        {"title": "Re-test", "detail": "The scenario runs again and closes the case."},
    ],
    "gates": _ctem_gates(launch_tools=True, mobilize_tools=True, mobilize_action=_MOBILIZE_ACTION),
    "objects": [
        {"module": "openaev", "objects": ["Scenarios (after approval)"]},
        {"module": "opencrq", "objects": ["Loss estimates"]},
        {
            "module": "platform",
            "objects": ["Drafted detections", "Approval work items", "Remediation tickets (after approval)"],
        },
    ],
    "prerequisites": [
        _NEEDS_OPENCTI,
        _NEEDS_OPENAEV,
        _NEEDS_OPENCRQ,
        _NEEDS_SECTOR,
        _NEEDS_REGIONS,
        _NEEDS_JEWELS,
        _SWITCHED_ON,
    ],
    "quota": [
        {"trigger": "New relevant threat", "basis": "No run: records signals", "runs_min": 0, "runs_max": 0},
        {
            "trigger": "Threat signal recorded",
            "basis": "One run per threat signal not yet confirmed",
            "runs_min": 4,
            "runs_max": 30,
        },
        {"trigger": "Weekly threat sweep", "basis": "One run a week", "runs_min": 4, "runs_max": 5},
        {"trigger": "Re-test due", "basis": "One run per fix re-tested", "runs_min": 0, "runs_max": 10},
        {"trigger": "Case opened or reopened", "basis": "One run per case event", "runs_min": 0, "runs_max": 20},
        {"trigger": "Gate answered", "basis": "One run per gate answered", "runs_min": 0, "runs_max": 40},
        {"trigger": "Loop reminder due", "basis": "One run per reminder due", "runs_min": 0, "runs_max": 40},
    ],
}

CTEM_VULN_PRIORITIZATION_STRUCTURED: dict[str, Any] = {
    "purpose_line": "Answer which vulnerabilities to fix on Monday, proven and priced.",
    "outcome_line": "CVEs matched to real assets, validated, ticketed and closed with a re-test.",
    "triggers": [
        {"module": "opencti", "name": "New KEV or EPSS jump", "filter": "A vulnerability is created or updated"},
        {
            "module": "opencti",
            "name": "Vulnerability on software we run",
            "filter": "Software, an infrastructure or a system in the profile has a vulnerability",
            "detail": "Kept only when the source is in the software inventory or the profile assets",
        },
        {
            "module": "opencti",
            "name": "Vulnerability flagged in a PIR",
            "filter": "A vulnerability is flagged in an organization PIR",
            "detail": "Kept when its live PIR score reaches the policy threshold (70 by default)",
        },
        {
            "module": "opencti",
            "name": "Vulnerability sighted",
            "filter": "A sighting of a vulnerability, at a higher priority",
        },
        {
            "module": "schedule",
            "name": "Daily backlog pass",
            "filter": "Every day at 06:00",
            "detail": "Re-ranks the open cases",
        },
        {
            "module": "work_item",
            "name": "Re-test due",
            "filter": "A fix is ready to be re-validated",
            "detail": "Opened by the remediation owner once the fix is deployed",
        },
        {
            "module": "platform",
            "name": "Case opened or reopened",
            "filter": "A CTEM case on any other path opens, reopens or breaches its SLA",
            "detail": "One loop per case",
        },
    ],
    "steps": [
        {"title": "Asset match", "detail": "No matching asset closes the case, with the reason."},
        # Each step names the gate it stands for (#1074): the About response badges it when that gate
        # waits at the fleet's live level and pins; the static flag is the seeded level's (Low).
        {"title": "Exploitability", "detail": "Validated in OpenAEV.", "gate": True, "gate_key": CTEM_VALIDATION_GATE},
        {"title": "Score", "detail": "The finding is scored in OpenCRQ."},
        {
            "title": "Ticket",
            "detail": "The remediation ticket is drafted.",
            "gate": True,
            "gate_key": CTEM_MOBILIZE_GATE,
        },
        {"title": "Re-test", "detail": "A re-validation closes the case."},
    ],
    "gates": _ctem_gates(launch_tools=True, mobilize_tools=True, mobilize_action=_MOBILIZE_ACTION),
    "objects": [
        {"module": "openaev", "objects": ["Validations (after approval)"]},
        {"module": "opencrq", "objects": ["Finding scores"]},
        {"module": "platform", "objects": ["Approval work items", "Remediation tickets (after approval)"]},
    ],
    "prerequisites": [_NEEDS_OPENCTI, _NEEDS_OPENAEV, _NEEDS_OPENCRQ, _NEEDS_JEWELS, _NEEDS_TECH, _SWITCHED_ON],
    "quota": [
        {
            "trigger": "New KEV or EPSS jump",
            "basis": "One run per matching vulnerability event",
            "runs_min": 5,
            "runs_max": 60,
        },
        {
            "trigger": "Vulnerability on software we run",
            "basis": "One run per vulnerability on software in the profile",
            "runs_min": 0,
            "runs_max": 40,
        },
        {
            "trigger": "Vulnerability flagged in a PIR",
            "basis": "One run per flag at or above the policy threshold",
            "runs_min": 0,
            "runs_max": 20,
        },
        {"trigger": "Vulnerability sighted", "basis": "One run per sighting", "runs_min": 0, "runs_max": 20},
        {"trigger": "Daily backlog pass", "basis": "One run a day", "runs_min": 28, "runs_max": 31},
        {"trigger": "Re-test due", "basis": "One run per fix re-validated", "runs_min": 0, "runs_max": 15},
        {"trigger": "Case opened or reopened", "basis": "One run per case event", "runs_min": 0, "runs_max": 40},
    ],
}

CTEM_AI_SECURITY_STRUCTURED: dict[str, Any] = {
    "purpose_line": "Watch the threats aimed at AI systems, map them to MITRE ATLAS and test your own AI behind approval.",
    "outcome_line": "AI threats mapped to ATLAS, AI tests run after approval, AI exposures fixed.",
    "triggers": [
        {"module": "opencti", "name": "New AI-enabled report", "filter": "A report labelled ai is created"},
        {
            "module": "schedule",
            "name": "Weekly AI threat watch",
            "filter": "Tuesdays at 07:00",
            "detail": "A summary of new AI campaigns",
        },
        {
            "module": "work_item",
            "name": "AI system registered",
            "filter": "An AI system is added to the profile",
            "detail": "Opened when an AI system is registered in the organization profile",
        },
    ],
    "steps": [
        {"title": "ATLAS mapping", "detail": "In an OpenCTI draft, for an analyst's review."},
        {"title": "Relevance", "detail": "Which of the organization's AI systems it reaches."},
        {
            "title": "AI tests",
            "detail": "Prompt injection, jailbreak, excessive agency, tool misuse, MCP tool poisoning.",
            "gate": True,
            "gate_key": CTEM_VALIDATION_GATE,
        },
        {
            "title": "Exposure",
            "detail": "Priced in OpenCRQ, fixes drafted and tracked to closure.",
            "gate": True,
            "gate_key": CTEM_MOBILIZE_GATE,
        },
    ],
    "gates": _ctem_gates(launch_tools=True, mobilize_tools=False, mobilize_action=_MOBILIZE_ACTION),
    "objects": [
        {"module": "opencti", "objects": ["Drafts of ATLAS entities"]},
        {"module": "openaev", "objects": ["AI tests (after approval)"]},
        {"module": "platform", "objects": ["Approval work items"]},
    ],
    "prerequisites": [
        _NEEDS_OPENCTI,
        _NEEDS_OPENAEV,
        _prereq("profile:ai_systems", "AI systems listed in the organization profile", profile_fix_route("ai_systems")),
        _SWITCHED_ON,
    ],
    "quota": [
        {"trigger": "New AI-enabled report", "basis": "One run per AI-labelled report", "runs_min": 2, "runs_max": 10},
        {"trigger": "Weekly AI threat watch", "basis": "One run a week", "runs_min": 4, "runs_max": 5},
        {"trigger": "AI system registered", "basis": "One run per AI system added", "runs_min": 0, "runs_max": 1},
    ],
}

CTEM_POSTURE_STRUCTURED: dict[str, Any] = {
    "purpose_line": "Keep the CTEM posture current across OpenCTI, OpenAEV and OpenCRQ.",
    "outcome_line": "Stage assessments kept current, the exposure briefing and a board-ready monthly report.",
    "triggers": [
        {"module": "schedule", "name": "Weekly assessments", "filter": "The weekly stage and briefing automations"},
        {
            "module": "work_item",
            "name": "Event refresh",
            "filter": "A CTEM case closed by another CTEM fleet",
            "detail": "At most one refresh per stage per hour",
        },
        {"module": "schedule", "name": "Monthly board report", "filter": "The 1st of the month at 09:00"},
    ],
    "steps": [
        {"title": "Map the case", "detail": "A closed case is mapped to the stages it changed."},
        {"title": "Debounce", "detail": "A stage refreshed less than an hour ago is skipped."},
        {"title": "Refresh", "detail": "Each remaining stage analyst refreshes its assessment."},
        {"title": "Report", "detail": "Monthly: stage scores and trend, fleet outcomes, loss avoided and ROI."},
    ],
    "objects": [
        {"module": "platform", "objects": ["Stage assessments", "Exposure briefings", "Monthly board report"]},
    ],
    "prerequisites": [_NEEDS_OPENCTI, _NEEDS_OPENAEV, _NEEDS_OPENCRQ],
    "quota": [
        {"trigger": "Weekly assessments", "basis": "Six assessments a week", "runs_min": 24, "runs_max": 30},
        {
            "trigger": "Event refresh",
            "basis": "At most one per stage per hour on closed cases",
            "runs_min": 0,
            "runs_max": 40,
        },
        {"trigger": "Monthly board report", "basis": "One run a month", "runs_min": 1, "runs_max": 1},
    ],
}

CTEM_THIRD_PARTY_RISK_STRUCTURED: dict[str, Any] = {
    "purpose_line": "Watch tracked suppliers, customers and partners and price a supplier exposure. Never contacts a third party.",
    "outcome_line": "Supplier exposures priced and acted on by a person; awareness cases for customers and partners.",
    "triggers": [
        {
            "module": "opencti",
            "name": "Tracked organization named",
            "filter": "A report or an incident names a tracked organization",
        },
        {"module": "schedule", "name": "Weekly third-party review", "filter": "Wednesdays at 07:00"},
    ],
    "steps": [
        {"title": "Relevance", "detail": "Of the event to the tracked organization."},
        {"title": "Dependency", "detail": "The services supplied and the primary assets they support."},
        {"title": "Expected loss", "detail": "Read in OpenCRQ through that supporting asset."},
        {
            "title": "Actions",
            "detail": "Contact, compensating control, contract clause, monitoring raised.",
            "gate": True,
            "gate_key": CTEM_MOBILIZE_GATE,
        },
    ],
    "gates": _ctem_gates(
        launch_tools=False,
        mobilize_tools=False,
        mobilize_action="Mobilize: carry out an action on a supplier (contact, control, clause, monitoring)",
    ),
    "objects": [
        {"module": "platform", "objects": ["Supplier-exposure cases", "Awareness cases", "Approval work items"]},
    ],
    "prerequisites": [
        _NEEDS_OPENCTI,
        _NEEDS_OPENCRQ,
        _prereq(
            "profile:tracked_organizations",
            "A tracked organization with monitoring on",
            profile_fix_route("tracked_organizations"),
        ),
        _SWITCHED_ON,
    ],
    "quota": [
        {
            "trigger": "Tracked organization named",
            "basis": "One run per event per organization, under its daily cap",
            "runs_min": 0,
            "runs_max": 60,
        },
        {"trigger": "Weekly third-party review", "basis": "One run a week", "runs_min": 4, "runs_max": 5},
    ],
}

OPENCTI_PIR_DESK_STRUCTURED: dict[str, Any] = {
    "purpose_line": "A ranked, current answer for every PIR of the organization, from OpenCTI alone.",
    "outcome_line": "Each PIR answered every week, its threats filed in one case, hunt packages for the top threats.",
    "triggers": [
        {
            "module": "opencti",
            "name": "PIR match created",
            "filter": "A report, intrusion set, campaign or vulnerability flagged in a PIR",
        },
        {"module": "schedule", "name": "Weekly PIR answer", "filter": "Mondays at 07:00"},
        {"module": "schedule", "name": "Half-year threat landscape", "filter": "1 January and 1 July at 08:00"},
        {"module": "schedule", "name": "Stale indicator clean-up", "filter": "The 1st of each month at 06:00"},
        {"module": "platform", "name": "Gate answered", "filter": "A person answers an indicator revocation"},
    ],
    "steps": [
        {"title": "Triage", "detail": "Scored against each PIR and the organization profile."},
        {"title": "File", "detail": "Into the standing OpenCTI case of the PIR."},
        {"title": "Hunt and detect", "detail": "Hunt package and detections for the top threats."},
        {"title": "Answer", "detail": "Weekly per PIR; a landscape report twice a year."},
        {"title": "Clean up", "detail": "Stale indicators re-checked, revocation proposed.", "gate": True},
    ],
    "gates": [
        {
            "key": "indicator.revoke",
            "action": "Revoke a stale indicator",
            "module": "opencti",
            "approver": "Fleet manager",
            "impact": "destructive",
        },
    ],
    "objects": [
        {
            "module": "opencti",
            "objects": ["One case per PIR", "Hunt packages", "Detection rules", "Landscape reports"],
        },
        {"module": "platform", "objects": ["Weekly PIR answers", "Approval work items"]},
    ],
    "prerequisites": [
        _NEEDS_OPENCTI,
        _NEEDS_SECTOR,
        _NEEDS_REGIONS,
        _SWITCHED_ON,
    ],
    "quota": [
        {
            "trigger": "PIR match created",
            "basis": "One run per entity flagged in a PIR, under its daily cap",
            "runs_min": 0,
            "runs_max": 300,
        },
        {"trigger": "Weekly PIR answer", "basis": "One run a week", "runs_min": 4, "runs_max": 5},
        {"trigger": "Half-year threat landscape", "basis": "Two runs a year", "runs_min": 0, "runs_max": 1},
        {"trigger": "Stale indicator clean-up", "basis": "One run a month", "runs_min": 1, "runs_max": 1},
        {"trigger": "Gate answered", "basis": "One run per revocation answered", "runs_min": 0, "runs_max": 20},
    ],
}

OPENAEV_DETECTION_DESK_STRUCTURED: dict[str, Any] = {
    "purpose_line": "Measured prevention, detection and logging coverage, from OpenAEV alone.",
    "outcome_line": "Every gap sorted, its rule drafted, and proven closed by a re-test.",
    "triggers": [
        {"module": "openaev", "name": "Simulation finished", "filter": "An OpenAEV simulation ends"},
        {"module": "schedule", "name": "Weekly validation plan", "filter": "Mondays at 06:00"},
        {"module": "schedule", "name": "Weekly coverage report", "filter": "Fridays at 08:00"},
        {"module": "platform", "name": "Re-test due", "filter": "A re-test work item of the desk"},
        {"module": "platform", "name": "Gate answered", "filter": "A person answers a launch approval"},
    ],
    "steps": [
        {"title": "Plan", "detail": "Simulations from the threat arsenal against the crown jewels.", "gate": True},
        {"title": "Sort", "detail": "Blocked, detected, logged or missed, per inject and asset."},
        {"title": "Root cause", "detail": "No rule, no log or a broken collector."},
        {"title": "Rule", "detail": "The missing detection rule drafted by the rule chain."},
        {"title": "Remediate", "detail": "One remediation work item per gap."},
        {"title": "Re-test", "detail": "The gap closes only on a re-test result.", "gate": True},
    ],
    "gates": [
        {
            "key": "openaev.simulation.launch",
            "action": "Launch an OpenAEV simulation",
            "module": "openaev",
            "approver": "Fleet manager",
            "impact": _LAUNCH_IMPACT,
            "tools": _OPENAEV_LAUNCH_TOOLS,
        },
    ],
    "objects": [
        {"module": "openaev", "objects": ["Scenarios", "Payloads"]},
        {
            "module": "platform",
            "objects": ["Detection rules", "Remediation work items", "Re-test work items", "Coverage reports"],
        },
    ],
    "prerequisites": [
        _NEEDS_OPENAEV,
        _NEEDS_JEWELS,
        _NEEDS_TECH,
        _SWITCHED_ON,
    ],
    "quota": [
        {
            "trigger": "Simulation finished",
            "basis": "One run per finished simulation, under its daily cap",
            "runs_min": 0,
            "runs_max": 60,
        },
        {"trigger": "Weekly validation plan", "basis": "One run a week", "runs_min": 4, "runs_max": 5},
        {"trigger": "Weekly coverage report", "basis": "One run a week", "runs_min": 4, "runs_max": 5},
        {"trigger": "Re-test due", "basis": "One run per remediated gap", "runs_min": 0, "runs_max": 60},
        {"trigger": "Gate answered", "basis": "One run per launch answered", "runs_min": 0, "runs_max": 60},
    ],
}

#: The OpenCRQ register-of-record writes (#10593): a person's call, logged at Low (impact high).
_OPENCRQ_REGISTER_WRITE_TOOLS = [
    {"name": "create_opencrq_risk_scenario"},
    {"name": "update_opencrq_risk_scenario"},
    {"name": "update_opencrq_loss_estimates"},
    {"name": "update_opencrq_risk_tolerance"},
    # CTEM-18: a treatment plan written to OpenCRQ is a register-of-record write too.
    *OPENCRQ_TREATMENT_PLAN_WRITE_TOOLS,
]

OPENCRQ_RISK_BUDGET_DESK_STRUCTURED: dict[str, Any] = {
    "purpose_line": "A risk register priced in money, kept current, from OpenCRQ alone.",
    "outcome_line": "What moved and why, the controls to fund by loss removed per dollar, a monthly Board pack.",
    "triggers": [
        {"module": "opencrq", "name": "Scenario re-priced", "filter": "A simulation or price recompute succeeded"},
        {"module": "opencrq", "name": "Finding re-scored", "filter": "A finding score recompute succeeded"},
        {
            "module": "opencrq",
            "name": "Control validation ingested",
            "filter": "OpenAEV findings ingested or a proven posture recorded",
        },
        {"module": "schedule", "name": "Weekly investment review", "filter": "Mondays at 07:00"},
        {"module": "schedule", "name": "Monthly Board pack", "filter": "The 1st of the month at 08:00"},
        {"module": "platform", "name": "Gate answered", "filter": "A person answers an investment recommendation"},
    ],
    "steps": [
        {"title": "Refresh", "detail": "The register compared with the last snapshot."},
        {"title": "Explain", "detail": "Each scenario re-priced, before and after, and its driver."},
        {"title": "What-if", "detail": "Control upgrades ranked by loss removed per dollar."},
        {"title": "Recommend", "detail": "A person decides; treatment plans stay read only.", "gate": True},
        {"title": "Report", "detail": "Monthly: loss trend, top controls, plan and budget progress."},
    ],
    "gates": [
        {
            "key": "opencrq.register.write",
            "action": "Change a risk scenario, its loss estimates, the risk tolerance or a treatment plan",
            "module": "opencrq",
            "approver": "Fleet manager",
            "impact": "high",
            "tools": _OPENCRQ_REGISTER_WRITE_TOOLS,
        },
    ],
    "objects": [
        {
            "module": "platform",
            "objects": ["Register snapshots", "Investment cases", "Recommendation work items", "Board packs"],
        },
    ],
    "prerequisites": [
        _NEEDS_OPENCRQ,
        _SWITCHED_ON,
    ],
    "quota": [
        {
            "trigger": "Scenario re-priced",
            "basis": "One run per poll batch of successful OpenCRQ jobs, one at a time, under its daily cap",
            "runs_min": 4,
            "runs_max": 60,
        },
        {
            "trigger": "Finding re-scored",
            "basis": "One run per poll batch of successful jobs, one at a time",
            "runs_min": 0,
            "runs_max": 30,
        },
        {
            "trigger": "Control validation ingested",
            "basis": "One run per poll batch of successful ingestions, one at a time",
            "runs_min": 0,
            "runs_max": 30,
        },
        {"trigger": "Weekly investment review", "basis": "One run a week", "runs_min": 4, "runs_max": 5},
        {"trigger": "Monthly Board pack", "basis": "One run a month", "runs_min": 1, "runs_max": 1},
        {"trigger": "Gate answered", "basis": "One run per recommendation answered", "runs_min": 0, "runs_max": 10},
    ],
}

# The Platform Health fleet (#974): seeded by ``app.services.health.fleet_seed``, not
# ``BUILTIN_FLEETS``. Read-only, switched and scheduled in Settings > Agents and
# automations > Platform Health, so the text names no action an administrator
# could take on the fleet itself (no role label, no handover, no automation to
# switch on). Its live prerequisite (Platform Health on) is computed by
# ``fleet_about.live_prerequisites``; nothing is documented as a manual step.
PLATFORM_HEALTH_DOC: dict[str, str] = {
    "purpose": (
        "Keep the platform's agents, flows and memory healthy without anyone asking: Platform Health "
        "watches quality, failures, cost and memory, investigates what degrades, and proposes the fix."
    ),
    "use_case": (
        "Built in and on by default. The Memory Steward tidies agent memory every day, the Quality "
        "Analyst and the Flow Lab review agents and flows every week, and a weekly digest summarizes "
        "what was found and fixed. A signal (a feedback drop, a failure rate, a cost increase, a model "
        "change) starts an investigation of the agent or flow concerned. In XTM One, the OpenCTI, OpenAEV "
        "and OpenCRQ Health Checkers run each module's health checks once a day, read-only through the "
        "module's integration, and report every check with its evidence."
    ),
    "triggers": (
        "- **Signals**: a feedback drop, a failure rate, a cost increase or a model change on an agent or a flow.\n"
        "- **Memory review**: daily, on the schedule set in Settings.\n"
        "- **Quality review**: weekly, on the schedule set in Settings.\n"
        "- **Flow review**: weekly, on the schedule set in Settings.\n"
        "- **Weekly digest**: on the schedule set in Settings.\n"
        "- **Module checks**: daily, on every registered OpenCTI, OpenAEV and OpenCRQ (XTM One)."
    ),
    "flow": (
        "1. Detect: a schedule fires or a signal crosses its threshold.\n"
        "2. Investigate: the specialist reads the runs, feedback and memory of the target.\n"
        "3. Propose: a finding and, when one helps, an improvement proposal a person decides on.\n"
        "4. Report: the weekly digest lists the cycles closed."
    ),
    "objects_created": "Health cycles, findings, module check reports, improvement proposals and the weekly digest.",
    "prerequisites": (
        "Platform Health switched on in Settings > Agents and automations > Platform Health, where its "
        "schedules, signals and budget are set. The fleet itself is built in and read-only."
    ),
}

PLATFORM_HEALTH_STRUCTURED: dict[str, Any] = {
    "purpose_line": "Agents, flows and memory kept healthy, without anyone asking.",
    "outcome_line": "Findings, improvement proposals a person decides on, and a weekly digest.",
    "triggers": [
        {
            "module": "platform",
            "name": "Signals",
            "filter": "Feedback drop, failure rate, cost increase or model change",
        },
        {"module": "schedule", "name": "Memory review", "filter": "Daily, on the schedule set in Settings"},
        {"module": "schedule", "name": "Quality review", "filter": "Weekly, on the schedule set in Settings"},
        {"module": "schedule", "name": "Flow review", "filter": "Weekly, on the schedule set in Settings"},
        {"module": "schedule", "name": "Weekly digest", "filter": "On the schedule set in Settings"},
        {"module": "schedule", "name": "Module checks", "filter": "Daily, on every registered module (XTM One)"},
    ],
    "steps": [
        {"title": "Detect", "detail": "A schedule fires or a signal crosses its threshold."},
        {"title": "Investigate", "detail": "The runs, feedback and memory of the target are read."},
        {"title": "Propose", "detail": "A finding, and an improvement proposal a person decides on.", "gate": True},
        {"title": "Report", "detail": "The weekly digest lists the cycles closed."},
    ],
    "objects": [
        {
            "module": "platform",
            "objects": ["Health cycles", "Findings", "Module check reports", "Improvement proposals", "Weekly digest"],
        },
    ],
}


def _loop_gates() -> list[dict[str, Any]]:
    """The CTEM gate catalog of the generic fleet, the folded desks' tool gates mapped onto it.

    The detection desk's simulation launches are already ``ctem.validation.non_readonly``
    tools; the risk desk's register writes join ``ctem.remediation.decision`` (impact high,
    logged, never waiting at the seeded level, as before). The PIR desk's indicator
    revocation declared no tool and is folded into ``ctem.mobilize``'s action text: out of
    the box one gate waits, ``ctem.mobilize``.
    """
    gates = _ctem_gates(
        launch_tools=True,
        mobilize_tools=True,
        mobilize_action=_MOBILIZE_ACTION + "; revoke a stale indicator",
    )
    for gate in gates:
        if gate["key"] == "ctem.remediation.decision":
            gate["tools"] = [dict(t) for t in _OPENCRQ_REGISTER_WRITE_TOOLS]
    return gates


#: 1. CTEM loop, the generic fleet (CTEM-31, #1048): the triggers of the former
#: threat response, posture and module desks under one case-driven loop.
CTEM_LOOP_STRUCTURED: dict[str, Any] = {
    "purpose_line": "The whole CTEM loop on your software: any change in exposure, one case, closed on proof.",
    "outcome_line": (
        "Cases validated, priced, decided once and closed by a re-test; posture, PIR answers, "
        "detection coverage and the risk register kept current."
    ),
    "triggers": [
        *CTEM_THREAT_RESPONSE_STRUCTURED["triggers"],
        *CTEM_POSTURE_STRUCTURED["triggers"],
        *(t for t in OPENCTI_PIR_DESK_STRUCTURED["triggers"] if t["module"] != "schedule"),
        *(t for t in OPENAEV_DETECTION_DESK_STRUCTURED["triggers"] if t["module"] == "openaev"),
        *(t for t in OPENCRQ_RISK_BUDGET_DESK_STRUCTURED["triggers"] if t["module"] == "opencrq"),
    ],
    "steps": [
        {"title": "Signal", "detail": "Any trigger path; reconcile and the scope gate open a case or watch it."},
        {"title": "Prioritise", "detail": "Relevance and severity from the profile and the scope criticality."},
        # Each step names its gate (#1074): the About response badges it when that gate waits at the
        # live level and pins. The static flag is the seeded level's (Low): the validation and ctem.mobilize wait.
        {
            "title": "Validate",
            "detail": "An OpenAEV scenario or atomic test, after your approval.",
            "gate": True,
            "gate_key": CTEM_VALIDATION_GATE,
        },
        {
            "title": "Decide",
            "detail": "Expected loss and options from OpenCRQ.",
            "gate_key": "ctem.remediation.decision",
        },
        {
            "title": "Mobilize",
            "detail": "Tickets, owners and the prepared change, after your approval.",
            "gate": True,
            "gate_key": CTEM_MOBILIZE_GATE,
        },
        {"title": "Re-test and close", "detail": "Closed only on a passing re-test; the lesson feeds the scope."},
    ],
    "gates": _loop_gates(),
    "objects": [
        {"module": "opencti", "objects": ["One case per PIR", "Hunt packages", "Detection rules"]},
        {"module": "openaev", "objects": ["Scenarios", "Payloads"]},
        {"module": "opencrq", "objects": ["Loss estimates"]},
        {
            "module": "platform",
            "objects": [
                "CTEM signals and cases",
                "Remediation tickets (after approval)",
                "Stage assessments",
                "Exposure briefings",
                "Board packs",
            ],
        },
    ],
    "prerequisites": [
        _NEEDS_OPENCTI,
        _NEEDS_OPENAEV,
        _NEEDS_OPENCRQ,
        _NEEDS_SECTOR,
        _NEEDS_REGIONS,
        _NEEDS_JEWELS,
        _SWITCHED_ON,
    ],
    "quota": [
        *CTEM_THREAT_RESPONSE_STRUCTURED["quota"],
        *CTEM_POSTURE_STRUCTURED["quota"],
        *(q for q in OPENCTI_PIR_DESK_STRUCTURED["quota"] if q["trigger"] == "PIR match created"),
        *(q for q in OPENAEV_DETECTION_DESK_STRUCTURED["quota"] if q["trigger"] == "Simulation finished"),
        *(
            q
            for q in OPENCRQ_RISK_BUDGET_DESK_STRUCTURED["quota"]
            if q["trigger"] in {"Scenario re-priced", "Finding re-scored", "Control validation ingested"}
        ),
    ],
}

#: Built-in fleet slug to the structured parts of its seeded documentation: the four
#: seeded fleets (CTEM-31).
BUILTIN_FLEET_STRUCTURED: dict[str, dict[str, Any]] = {
    "ctem-loop": CTEM_LOOP_STRUCTURED,
    "ctem-vuln-prioritization": CTEM_VULN_PRIORITIZATION_STRUCTURED,
    "ctem-ai-security": CTEM_AI_SECURITY_STRUCTURED,
    "ctem-third-party-risk": CTEM_THIRD_PARTY_RISK_STRUCTURED,
}


def builtin_fleet_structured(entry: dict[str, Any]) -> dict[str, Any] | None:
    """The structured parts the seed writes for the built-in fleet *entry*: its own, else the shipped ones."""
    return entry.get("documentation_structured") or BUILTIN_FLEET_STRUCTURED.get(entry.get("slug") or "")
