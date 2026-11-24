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

"""The registry of built-in tool groups: what each built-in suite is.

``build_builtin_tool_catalog`` (``builtin_tool_catalog.py``) answers which
functions a built-in id grants; this module answers everything else a reader
needs about the id: its card name, tagline, longer detail, category chip, icon,
the deployment switch it depends on and its audience flags. It replaces the
frontend's hand-kept ``BUILTIN_TOOLS`` list: the settings API serves it
(``GET /settings/builtin-tools/groups``) with each group's functions and schema
size, and ``useBuiltinTools()`` renders every picker from it.

``name``, ``tagline``, ``detail`` and ``category`` are i18n keys of the frontend
catalogs, kept verbatim; ``detail`` may name the platform as ``{name}``.
``tests/test_builtin_tool_groups.py`` pins the registry against the catalog
keys, the agent-keyed and administrator-only id sets and the General
Assistant's default set, so a suite is described once.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Literal

#: A deployment switch a group depends on: while it is off the group is not
#: offered, and an entity already carrying it shows it disabled.
BuiltinToolGate = Literal[
    "code_interpreter",
    "runners",
    "flow_builder",
    "deep_investigation",
    "video_studio",
    "mobile_companion",
    "dashboard_studio",
    "health",
]


@dataclass(frozen=True)
class BuiltinToolGroup:
    """One built-in suite as every surface describes it."""

    id: str
    #: i18n key of the card title (the platform's own name when ``named_after_platform``).
    name: str
    #: i18n key of the one-line description.
    tagline: str
    #: i18n key of the category chip on the Tools page.
    category: str
    #: lucide-react icon name.
    icon: str
    #: i18n key of the longer description on the tool's own page; the tagline when empty.
    detail: str = ""
    gate: BuiltinToolGate | None = None
    #: Seeded on a new General Assistant (``DEFAULT_GA_BUILTIN_TOOL_IDS``).
    default_on: bool = False
    #: Offered to an administrator's General Assistant only (``ADMIN_ONLY_GA_BUILTIN_TOOL_IDS``).
    admin_only: bool = False
    #: Bound to an agent by an administrator alone (``ADMIN_BOUND_BUILTIN_TOOL_IDS``):
    #: a picker shows it locked to anyone else, unless the agent already carries it.
    bind_admin_only: bool = False
    #: Acts as the agent, never on the General Assistant (``AGENT_KEYED_BUILTIN_TOOL_IDS``).
    agent_keyed: bool = False
    #: A Platform Health specialist's suite, carried by the fleet's own agents
    #: (``health.fleet_seed``), whose writes refuse anyone but the
    #: Platform Health: a chat's session tools never offer it.
    health_only: bool = False
    #: Titled with the platform's own name.
    named_after_platform: bool = False
    #: Former ids this group answers to, [DEPRECATED] (``builtin_tool_aliases``).
    aliases: tuple[str, ...] = ()
    #: The functions that stay always-on (its create and read functions);
    #: the group's other functions are loadable on demand through ``load_tools``
    #: as group ``builtin:<id>``. ``None``: the whole group is always-on.
    core: tuple[str, ...] | None = None


#: Every built-in group, in display order.
BUILTIN_TOOL_GROUPS: tuple[BuiltinToolGroup, ...] = (
    BuiltinToolGroup(
        id="platform",
        default_on=True,
        name="Platform",
        tagline="Manage the platform from chat: agents, assignments, knowledge bases and fleets",
        detail="Manage your {name} platform through conversation, acting as the user and within what the user may do: agents, assignments, runs, knowledge bases, prompts, skills, variables, integrations, MCP servers, custom tools, workspaces and evaluations; and fleets of agents. A function the user may not run is not offered.",
        category="platform",
        icon="Blocks",
        # The platform builder, fleets and the flow builder merged here.
        aliases=("agent_builder", "fleets", "flow_builder", "roi"),
        core=(
            "list_available_resources",
            "list_agents",
            "get_agent",
            "create_agent",
            "list_assignments",
            "get_assignment",
            "create_assignment",
            "list_fleets",
            "get_fleet",
            "create_fleet",
            # The first call of the fleet setup interview (#11250): offered at once,
            # never behind load_tools.
            "get_fleet_setup",
            "list_flows",
            "get_flow",
        ),
    ),
    BuiltinToolGroup(
        id="health_sandbox",
        name="Health Sandbox",
        tagline="Copy agents and flows into the sandbox and run them with writes intercepted",
        detail="Make hidden copies of agents and flows and run them in the sandbox: calls recorded in the source traces are replayed, reads run live, and every write is intercepted and answered by a simulated result, counted per case. For Platform Health and administrators, within the supervision scope.",
        category="platform",
        icon="FlaskConical",
        gate="health",
        agent_keyed=True,
    ),
    BuiltinToolGroup(
        id="health_evaluation",
        name="Health Evaluation",
        tagline="Build datasets from production and measure agents and flows against them",
        detail="Turn rated answers and failed runs of consented agents and flows into evaluation datasets, evaluate agents, flows and their sandbox copies on them, and compare runs on quality, cost and latency with the simulated writes stated. For Platform Health, and for administrators where evaluation is on.",
        category="platform",
        icon="Scale",
        gate="health",
        agent_keyed=True,
    ),
    BuiltinToolGroup(
        id="improvement_proposals",
        name="Improvement Proposals",
        tagline="List improvement proposals and file one for the target's managers to approve",
        detail="List the improvement proposals on the agents, skills, assignments and flows the caller may read, with the verdict of their experiment or that they are unmeasured. Platform Health's Experimenter files new ones, with their evidence and measurement; the target's managers approve or reject them, and a regression is rolled back.",
        category="platform",
        icon="Sparkles",
        gate="health",
        agent_keyed=True,
    ),
    BuiltinToolGroup(
        id="files",
        default_on=True,
        name="Files",
        tagline="Generate, read and edit conversation files, render HTML to images and generate images",
        detail="Generate downloadable files from structured content (JSON, Markdown, text, CSV, and other MIME types), read and edit the files of the conversation, render HTML to an image, and generate images from text prompts with the configured model.",
        category="file",
        icon="FileText",
        # File and image generation merged here.
        aliases=("generate_file", "generate_image"),
    ),
    BuiltinToolGroup(
        id="generate_presentation",
        default_on=True,
        name="Presentation Generation",
        tagline="Build interactive slide decks (reveal.js) the user can present and export to PDF",
        detail="Build an interactive slide deck from structured slides: the agent picks a controlled layout per slide (title, section, agenda, content, dashboard, roadmap, takeaways, closing) composed of typed blocks, native interactive charts included, and the deck renders in chat with keyboard, touch and fullscreen navigation, speaker notes and PDF export. The agent authors structured JSON, never HTML: every human-readable string is flat inline markup rendered through a tokenizer, never an HTML sink. Once a deck exists, edit_presentation changes it surgically (one slide, one block, one word), with every other slide guaranteed byte-identical; calling generate_presentation again with the presentation_id rewrites the same deck wholesale.",
        category="file",
        icon="Presentation",
        core=("generate_presentation", "edit_presentation", "get_presentation"),
    ),
    BuiltinToolGroup(
        id="generate_report",
        default_on=True,
        name="Report Generation",
        tagline="Build structured executive reports the user can read in the app and export to PDF",
        category="file",
        icon="FileChartColumn",
    ),
    BuiltinToolGroup(
        id="custom_dashboards",
        name="Custom Dashboards",
        tagline="Build pages of widgets for the user and keep them up to date",
        detail="Compose a dashboard the user opens on the web and on their phone - scores, stage progress, decisions waiting for them, agent activity, follow-up questions - from the data the agent gathered. The agent picks typed widgets and fills their values; the platform draws them with its design system, so a dashboard has no HTML, no colour and no link outside the platform an agent could write. Widgets are refreshed one at a time by id, and a scheduled assignment can keep the same dashboard current.",
        category="file",
        icon="LayoutGrid",
        gate="dashboard_studio",
    ),
    BuiltinToolGroup(
        id="phone",
        name="Phone Notifications",
        tagline="Notify the user's linked phones when something needs them",
        detail="Send a short notification to the phones the user linked to the mobile companion, and to their notification bell: a critical finding, a run that needs them, a result they asked to be told about. It reaches only the person the agent acts for, respects their quiet hours and opt-outs, links only to pages of the platform, and is capped per run and per hour. Reminders reach the phone through scheduled work items, and approvals through the run's own notification.",
        category="file",
        icon="Smartphone",
        gate="mobile_companion",
    ),
    BuiltinToolGroup(
        id="video_studio",
        default_on=True,
        name="Video Studio",
        tagline="Record product demos in the platform's browser and produce narrated, branded videos",
        detail="Record scripted demos of any web application in the platform's own headless browser at high resolution (signed in with a browser profile: encrypted credentials, TOTP or an MFA code given in the chat, a saved session reused by every recording), then produce the video separately: title cards, recordings with an automatic camera that zooms on what is clicked and typed, a drawn cursor with click ripples, callouts and spotlights anchored to steps, deck slides, AI-generated clips, voice-over from the narration, music and captions, rendered to MP4 up to 4K.",
        category="file",
        icon="Clapperboard",
        gate="video_studio",
        core=("video_studio_capabilities", "create_video", "get_video", "list_videos"),
    ),
    BuiltinToolGroup(
        id="design",
        default_on=True,
        name="Design",
        tagline="Find, author and check design templates, and read the exact design of a source document",
        detail="Find a reusable design template (deck, report, dashboard) by its metadata and read its layouts, theme and assets; author a template from a conversation and bind the files the user uploaded as its logo or fonts; look at the pages of a PDF and read the exact palette and typefaces of a PowerPoint or HTML page. Writing a template is loaded on demand.",
        category="templates",
        icon="Palette",
        # Template search, template authoring and document vision merged here.
        aliases=("template_search", "template_authoring", "document_vision"),
        core=(
            "search_templates",
            "get_template",
            "render_source_pages",
            "inspect_pptx_identity",
            "inspect_html_identity",
            "compare_palette",
            "create_template",
            "update_template",
        ),
    ),
    BuiltinToolGroup(
        id="code_interpreter",
        # On the default assistants.
        default_on=True,
        name="Code Interpreter",
        tagline="Execute Python code in a sandbox",
        detail="Execute Python code in a sandboxed environment. When enabled on an agent or the General Assistant, the AI can write and run Python code to perform calculations, generate charts, process data, and more.",
        category="code",
        icon="Code2",
        gate="code_interpreter",
    ),
    BuiltinToolGroup(
        id="web",
        default_on=True,
        name="Web",
        tagline="Read web pages, download public images and control a headless browser",
        detail="Fetch and read public web pages and documents, download a public image as a conversation file, and control a headless browser to navigate, interact with elements, extract content and take screenshots. The browser session persists across tool calls within the same conversation.",
        category="web",
        icon="Globe",
        # Web fetch and browser merged here.
        aliases=("web_fetch", "browser"),
    ),
    BuiltinToolGroup(
        id="computer_use",
        name="Computer Use (Runners)",
        tagline="Drive an enrolled machine: genuine full-desktop screenshots, real-browser control, shell + file ops",
        category="automation",
        icon="MonitorCog",
        gate="runners",
    ),
    BuiltinToolGroup(
        id="cti",
        # On the default assistants.
        default_on=True,
        name="Threat Intelligence",
        tagline="Validate, compare and analyse STIX 2.1 bundles, with statistical tests and FIMI metrics",
        detail="Validate STIX 2.1 bundle structure and references, compare two bundles field by field, analyse a timeline, compute deterministic metrics (overlap, coverage, growth, volatility), run statistical hypothesis tests (chi-squared, Mann-Whitney, KS) and FIMI-derived metrics. Every output includes the formula, intermediate values and interpretation.",
        category="security",
        icon="ShieldCheck",
        # STIX bundle validator and FIMI analytics merged here.
        aliases=("stix_bundle_validator", "fimi_analytics"),
    ),
    BuiltinToolGroup(
        # Read-only CTEM signals and cases for any agent (#1337), as the person
        # the turn or run acts for, with the CTEM routes' permission model.
        id="ctem_read",
        name="CTEM Read",
        tagline="Read the CTEM signals and cases the person you act for can see, read-only",
        detail="Lets the agent list the CTEM signals with their evidence, list the CTEM cases with their counts by state, and read one case with its issues, history, who opened it and why each empty section is empty. It acts for the person the turn or run acts for, with the same rules as the CTEM pages: a case they cannot see does not exist for the agent, an issue they cannot see is a Restricted row. It writes nothing. XTM One mode only.",
        category="security",
        icon="ScanEye",
        agent_keyed=True,
    ),
    BuiltinToolGroup(
        # CTEM administrator repairs (#1778): the attack surface pass summary and
        # repair and the threat package repair, the REST routes' admin scope.
        id="ctem_repair",
        name="CTEM Repair",
        tagline="Read the attack surface passes and run the CTEM repairs, administrators",
        detail="Lets the agent read the latest attack surface passes (what each read covered, hosts observed, signals per path), discard the new-host signals raised without a complete previous read, and correct the threat packages stored with a Grouping page whose Grouping is not live. A repair defaults to a dry run that writes nothing and answers the counts and the ids. It acts for the person the turn or run acts for, who must be an administrator, as on the REST routes. XTM One mode only.",
        category="security",
        icon="Stethoscope",
        agent_keyed=True,
    ),
    BuiltinToolGroup(
        # CTEM signal and case tools on any agent (#1748): the owner's CTEM rights
        # (administrator, decide, view or own signals) decide the reach.
        id="ctem",
        name="CTEM",
        tagline="Record, triage and manage CTEM signals and cases within the rights of the person you act for",
        detail="Lets the agent record, list, dismiss, restore and re-check CTEM signals, and open, read and manage CTEM cases: owner, due date, notes, state, validation, tickets and remediation decisions. It acts for the person the turn or run acts for, with the rights of the CTEM pages: an administrator or a member of a group with the CTEM decide right reads and acts on every signal and case, a view right reads them and every change is refused, anyone else works on the signals they recorded and the cases they can see.",
        category="security",
        icon="ScanEye",
        agent_keyed=True,
    ),
    BuiltinToolGroup(
        id="stix_extractor",
        # Off by default and never reached through Threat Intelligence: one call reads a
        # report through hundreds of model calls (up to 30 min), so an agent carries it
        # only when switched on for it (the CTI STIX Extractor built-in agent).
        name="STIX Extraction",
        tagline="Extract a STIX 2.1 bundle from threat reports, every object and relationship grounded in the text",
        detail="Reads the attached reports chunk by chunk with the agent's own model: entities, relationships, indicators and ATT&CK techniques, each tied to the sentence that states it, in OpenCTI's ontology with its standard ids. A long report takes minutes and many model calls; switch it on only for agents that extract.",
        category="security",
        icon="FileText",
        # Runs on the agent's own model for an agent built to extract; never on the
        # General Assistant.
        agent_keyed=True,
    ),
    BuiltinToolGroup(
        id="platform_insights",
        default_on=True,
        name="Platform Insights",
        tagline="Read this platform's LLM usage and cost, objects, releases, AI adoption and audit trail, and chart them in chat",
        detail="Read-only access to the figures the administration pages show: LLM usage and cost by agent, user, group, model or period (the LLM Analytics read path), the objects of the platform counted per day, week, month or deployed build, the release history, AI adoption, the audit log and an overview of what the platform holds. Results come with the dashboard's caveats (current group membership, stale aggregation, coverage start, hidden costs), and render_chart draws them inline in the answer. An administrator's agent reads the whole platform; anyone else their own slice.",
        category="analytics",
        icon="ChartLine",
        admin_only=True,
        core=("get_platform_overview", "query_platform_analytics", "query_platform_metrics", "render_chart"),
    ),
    BuiltinToolGroup(
        # The write side of the organization profile (#11594); every agent
        # reads it with the core get_organization_context. Only an
        # administrator binds it, and a turn wires it for an administrator alone.
        id="organization_context",
        name="Organization Profile Editing",
        tagline="Administrators only: update the organization profile, or propose a change for review",
        detail="Lets the agent change the organization profile (crown jewels, software, risk appetite and the other fields of Settings > Organization) for the administrator it acts for: each change is saved as a new version authored by that administrator, with the agent and the basis kept, so the history can roll it back. It can also propose a change for an administrator to review. Only an administrator can bind it, and the tools are offered only on a turn that acts for an administrator; every agent keeps reading the profile without it.",
        category="platform",
        icon="Building2",
        agent_keyed=True,
        bind_admin_only=True,
    ),
    BuiltinToolGroup(
        # Users and groups for an administrator's agent (#250): the one
        # deactivation cascade, the group membership, the review queue. Only an
        # administrator binds it, and a turn wires it for an administrator alone.
        id="user_admin",
        name="User Administration",
        tagline="Administrators only: list, deactivate and reactivate accounts, manage group members",
        detail="Lets the agent do what Administration > Users and groups offers for the administrator it acts for: list and read accounts (source, status, groups, last sign-in, what they own), preview and run the deactivation cascade on one or many accounts (dry run by default), reactivate an account, add or remove group members (a group the identity provider manages stays read-only) and work the deactivation review queue. Every change is audited with the administrator, the agent and the run. Only an administrator can add it to an agent, and it runs only when the agent acts for an administrator.",
        category="platform",
        icon="UserCog",
        agent_keyed=True,
        bind_admin_only=True,
    ),
    BuiltinToolGroup(
        id="memory_steward",
        name="Memory Steward",
        tagline="Consolidate agent memory lanes, reversibly, for Platform Health",
        detail="Platform Health's memory consolidation: list the memory lanes it may read, run a pass over one (merge duplicates, settle contradictions, demote time-bound entries, promote recurring observations) and count what was applied. Every change is reversible by the lane's owner from their profile. Only the Platform Health identity may call these tools.",
        category="platform",
        icon="Brain",
        gate="health",
        agent_keyed=True,
        health_only=True,
    ),
    BuiltinToolGroup(
        id="coordination",
        name="Coordination",
        tagline="Work items, shared state and worksets: hand work over and keep state across runs",
        detail="Coordinate a fleet across runs: a durable, claimable work queue (create, update, list, atomically claim and complete work items, delegate a tracked run to an assignment), a cross-run key-value store so agents never cold-start, and a workset, a working corpus for material too large for one context, summarised and searchable. Work items are shared data: they never trigger an agent by themselves.",
        category="coordination",
        icon="ListTodo",
        agent_keyed=True,
        # Work items, shared state and workset merged here.
        aliases=("work_items", "shared_state", "workset"),
    ),
    BuiltinToolGroup(
        id="observability",
        name="Traces",
        tagline="Read this agent's traces, and other agents' traces where supervision allows it",
        category="observability",
        icon="Activity",
        agent_keyed=True,
        # The two trace ids merged here; reading another agent's traces
        # is a permission inside inspect_agent_traces (administrator or the
        # agent's supervision allow-list), not a second card.
        aliases=("inspect_my_traces", "inspect_agent_traces"),
    ),
    BuiltinToolGroup(
        id="health_orchestrator",
        name="Platform Health",
        tagline="Read Platform Health's signals, cycles and digests, and ask it to improve an agent or flow",
        detail="The Platform Health orchestrator's tools: the signals crossed, the cycles and their digests, and a request to investigate one agent or flow. In a person's chat they read only the targets that person manages; in a Platform Health run, its supervision scope.",
        category="platform",
        icon="Compass",
        gate="health",
        agent_keyed=True,
    ),
    BuiltinToolGroup(
        id="deep_investigation",
        name="Deep Investigation",
        tagline="Investigate a subject across its sources, every claim cited",
        detail="Runs a Deep Investigation on a subject: queries the source packs that cover it, follows the leads they return, and answers with a report whose every statement cites the passage it rests on. One investigation per turn.",
        category="security",
        icon="Telescope",
        gate="deep_investigation",
        agent_keyed=True,
    ),
    BuiltinToolGroup(
        id="health_diagnosis",
        name="Health Diagnosis",
        tagline="Diagnose supervised agents from their traces, ratings and costs",
        detail="Platform Health's Quality Analyst: reads the quality signals of the agents in its supervision scope, records each finding once with its evidence, and drafts the variant an experiment will measure. Works only inside a Platform Health run.",
        category="observability",
        icon="Stethoscope",
        gate="health",
        agent_keyed=True,
        health_only=True,
        core=("get_quality_signals", "list_findings"),
    ),
    BuiltinToolGroup(
        id="health_experiments",
        name="Health Experiments",
        tagline="Measure variants on sandbox copies and file what they earn",
        detail="Platform Health's Experimenter: sizes each experiment to the Platform Health budget, evaluates the current configuration and a variant as sandbox copies on the same production cases, and files the improvement proposal the measurement earned, or unmeasured with its reason. Works only inside a Platform Health run.",
        category="observability",
        icon="FlaskConical",
        gate="health",
        agent_keyed=True,
        health_only=True,
        core=("list_experiments", "get_experiment"),
    ),
    BuiltinToolGroup(
        id="flow_lab",
        name="Health Flow Lab",
        tagline="Diagnose flows and measure flow replacements in shadow",
        detail="Platform Health's Flow Lab: diagnoses a supervised flow's failed runs and costs, finds the tool-call sequences users repeat, checks whether an assignment follows a path a flow could reproduce, and measures a sandbox flow on an assignment's live triggers with every write intercepted. Works only inside a Platform Health run; the reads are also served to the managers of the flow or assignment.",
        category="flows",
        icon="Workflow",
        gate="health",
        agent_keyed=True,
        health_only=True,
        core=("get_flow_diagnosis", "get_assignment_path", "get_flow_shadow"),
    ),
    BuiltinToolGroup(
        id="health_modules",
        name="Health Modules",
        tagline="Investigate registered modules and recommend on their findings",
        detail="Platform Health's Module Analyst: reads the open findings of a registered OpenCTI, OpenAEV or OpenCRQ with the recommendations already attached, and attaches its own recommendation to each finding the module reads explain. Never writes to a module. Works only inside a Platform Health run.",
        category="observability",
        icon="Stethoscope",
        gate="health",
        agent_keyed=True,
        health_only=True,
        core=("list_module_findings",),
    ),
)


def builtin_source(group_id: str) -> str:
    """The ``ToolDefinition.source`` every function of *group_id* carries."""
    return f"builtin:{group_id}"


def builtin_on_demand_groups() -> dict[str, tuple[str, frozenset[str]]]:
    """The groups loadable on demand, as the tool gate reads them.

    Group id -> (catalogue label, the always-on core). A group without a
    declared ``core`` is not listed: all its functions stay always-on.
    """
    return {
        group.id: (group.name, frozenset(group.core))
        for group in BUILTIN_TOOL_GROUPS
        if group.core is not None
    }


def schema_chars(definition: dict[str, Any]) -> int:
    """Characters one function costs on the wire: its name, description and input schema."""
    return (
        len(definition["name"])
        + len(definition["description"])
        + len(json.dumps(definition["input_schema"], separators=(",", ":"), ensure_ascii=False))
    )


def builtin_tool_groups_payload(catalog: dict[str, list[dict]]) -> list[dict[str, Any]]:
    """The registry as the settings API serves it: every group, with the functions *catalog* offers.

    Every group is listed, so an entity that carries a switched-off suite still
    names it; a group the catalog withholds (a flag off, Runners disabled) comes
    with no function, ``offered`` false.
    """
    payload: list[dict[str, Any]] = []
    for group in BUILTIN_TOOL_GROUPS:
        definitions = catalog.get(group.id) or []
        entry = asdict(group)
        entry["offered"] = group.id in catalog
        entry["aliases"] = list(group.aliases)
        entry["core"] = list(group.core) if group.core is not None else None
        entry["functions"] = [definition["name"] for definition in definitions]
        entry["function_count"] = len(definitions)
        entry["schema_chars"] = sum(schema_chars(definition) for definition in definitions)
        payload.append(entry)
    return payload
