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

"""What a caller may see of flows, for the REST API and the flow-builder tool.

:func:`catalog_sources` is the live option lists of the node catalog's dynamic
``select`` fields (``services/flow_catalog.py`` holds the static node kinds),
scoped to what the caller can use; :func:`get_visible_run` loads a flow run
the caller may read. Both take the admin bypass from the caller's role unless
told otherwise: the flow-builder tool passes ``agent_tool_admin_bypass()``.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.helpers import (
    assignment_visible_filter,
    bindable_knowledge_base_clause,
    get_user_group_ids,
    parse_path_id,
    user_visible_query,
)
from app.core.security_options import security_option
from app.models.agent import Agent
from app.models.assignment import Assignment
from app.models.flow import Flow
from app.models.flow_run import FlowRun
from app.models.integration import Integration
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User
from app.models.workspace import Workspace
from app.services.flow_catalog import (
    SOURCE_AGENTS,
    SOURCE_ASSIGNMENTS,
    SOURCE_CUSTOM_TOOLS,
    SOURCE_EVENT_TYPES,
    SOURCE_FLEETS,
    SOURCE_FLOWS,
    SOURCE_INTEGRATIONS,
    SOURCE_KNOWLEDGE_BASES,
    SOURCE_LLM_PROVIDERS,
    SOURCE_MODELS,
    SOURCE_RELAY_GROUPS,
    SOURCE_REPORT_TEMPLATES,
    SOURCE_RUNNERS,
    SOURCE_SKILLS,
    SOURCE_TOOLS,
    SOURCE_VARIABLES,
    SOURCE_WORKSPACES,
)

# The built-in tool groups with their human names: (id, label, lucide icon
# name, one-line description). The registry is `_VALID_BUILTIN_TOOL_IDS`
# (services/builtin_tools/agent_builder.py); labels are curated here because
# no other backend surface serves humanized names. Read by the Tool node's
# select below and by the fleet datasheet (services/fleet_datasheet.py).
BUILTIN_TOOL_CATALOG: tuple[tuple[str, str, str, str], ...] = (
    ("web_fetch", "Web fetch", "Globe", "Fetch and read a web page."),
    ("browser", "Browser", "Globe", "Drive a headless browser."),
    ("code_interpreter", "Code interpreter", "Terminal", "Run Python in a sandbox."),
    ("generate_image", "Generate image", "Image", "Create an image from a prompt."),
    ("generate_file", "Generate file", "FileText", "Produce a downloadable file."),
    ("generate_presentation", "Generate presentation", "Presentation", "Build a slide deck."),
    ("generate_report", "Generate report", "FileText", "Compose a structured report."),
    ("custom_dashboards", "Custom dashboards", "LayoutDashboard", "Build a dashboard of widgets for the user."),
    ("phone", "Phone", "Smartphone", "Notify the user's linked phones."),
    ("video_studio", "Video Studio", "Clapperboard", "Record demos and produce videos."),
    ("computer_use", "Computer use", "MonitorSmartphone", "Operate an enrolled machine's desktop."),
    ("deep_investigation", "Deep investigation", "Telescope", "Run a sourced, multi-step investigation."),
    ("stix_bundle_validator", "STIX bundle validator", "ShieldCheck", "Validate a STIX 2.1 bundle."),
    ("fimi_analytics", "FIMI analytics", "ChartBar", "Analyse influence-operation data."),
    ("work_items", "Work items", "ListChecks", "Create and manage work items."),
    ("shared_state", "Shared state", "Boxes", "Read and write shared run state."),
    ("workset", "Workset", "Library", "Build and query a large working corpus."),
    ("template_search", "Template search", "Search", "Find remediation templates."),
    ("template_authoring", "Template authoring", "PenLine", "Author remediation templates."),
    ("document_vision", "Document vision", "ScanEye", "Look at a source PDF or PowerPoint."),
    ("agent_builder", "Agent builder", "Bot", "Create and edit agents."),
    ("flow_builder", "Flow builder", "Workflow", "Create and run flows."),
    # One trace group; a flow saved with a former trace id keeps
    # running through its [DEPRECATED] alias (``builtin_tool_aliases``).
    ("observability", "Traces", "Activity", "Review this agent's runs, and other agents' where supervision allows."),
    ("platform_insights", "Platform insights", "ChartLine", "Read the platform's usage, objects and audit trail."),
    ("fleets", "Fleets", "Boxes", "Create and manage fleets of agents."),
)


def available_node_kinds() -> list[dict]:
    """The static node kinds this platform can run, for every catalog consumer.

    The REST node catalog and the flow-builder tool both serve this list, so a
    node the platform cannot run (its kill-switch, transport or feature flag is
    off) is hidden from both or from neither. The validator still knows every
    kind: a graph saved before the switch flipped stays loadable, and the
    executor refuses the step with its own message.
    """
    from app.core.config import settings as app_settings
    from app.services.flow_catalog import (
        FEATURE_GATED_NODE_KINDS,
        SOURCE_RELAY_GROUPS,
        static_catalog,
    )
    from app.services.relays import relays_active

    hidden: set[str] = set()
    if security_option("disable_code_interpreter"):
        # The Run code node rides the code interpreter's sandbox.
        hidden.add("util.code")
    if security_option("disable_custom_tools"):
        # The Script node runs Custom tools, which the executor then refuses.
        hidden.add("action.script")
    if not app_settings.runners_enabled:
        # No enrolled-machine transport: a command could only fail.
        hidden.add("action.runner_command")
    from app.core.feature_flags import is_feature_enabled

    hidden.update(
        kind for kind, flag in FEATURE_GATED_NODE_KINDS.items() if not is_feature_enabled(flag)
    )
    kinds = [nk for nk in static_catalog() if nk["kind"] not in hidden]
    if not relays_active():
        # No relay transport here: the HTTP node keeps its form, minus a
        # "Route through" picker that could offer nothing.
        kinds = [
            {
                **nk,
                "config_fields": [
                    f for f in nk.get("config_fields") or [] if f.get("source") != SOURCE_RELAY_GROUPS
                ],
            }
            for nk in kinds
        ]
    return kinds


async def catalog_sources(
    db: AsyncSession,
    user: User,
    *,
    is_admin: bool | None = None,
    only: frozenset[str] | None = None,
    user_group_ids: list[str] | None = None,
) -> dict[str, list[dict[str, str]]]:
    """Live option lists for the catalog's dynamic `select` fields.

    Scoped to what the caller can actually see, so the node library never
    offers an agent or integration this user cannot use. *is_admin* is the
    admin bypass, the caller's role by default; the flow-builder tool passes
    ``agent_tool_admin_bypass()``. *only* names the sources a caller reads
    (the agent wizard's assistant): the others are not queried and come back
    empty. *user_group_ids* spares the lookup when the caller has them.
    """
    if is_admin is None:
        is_admin = user.is_admin
    if user_group_ids is None:
        user_group_ids = await get_user_group_ids(db, user.id)

    def _want(*sources: str) -> bool:
        return only is None or any(source in only for source in sources)

    agents = [] if not _want(SOURCE_AGENTS) else (
        (
            await db.execute(
                user_visible_query(Agent, user.id, user_group_ids, is_admin=is_admin)
            )
        )
        .scalars()
        .all()
    )
    integrations = [] if not _want(SOURCE_INTEGRATIONS) else (
        (
            await db.execute(
                user_visible_query(
                    Integration, user.id, user_group_ids, is_admin=is_admin
                )
            )
        )
        .scalars()
        .all()
    )
    knowledge_bases = [] if not _want(SOURCE_KNOWLEDGE_BASES) else (
        (
            await db.execute(
                user_visible_query(
                    KnowledgeBase, user.id, user_group_ids, is_admin=is_admin
                ).where(bindable_knowledge_base_clause())
            )
        )
        .scalars()
        .all()
    )
    # Archived workspaces are excluded: the runtime refuses to resolve one, so
    # offering it would be offering coverage the run will not have.
    workspaces = [] if not _want(SOURCE_WORKSPACES) else (
        (
            await db.execute(
                user_visible_query(
                    Workspace, user.id, user_group_ids, is_admin=is_admin
                ).where(Workspace.is_archived.is_(False))
            )
        )
        .scalars()
        .all()
    )

    from app.models.fleet import Fleet

    fleets = [] if not _want(SOURCE_FLEETS) else (
        (
            await db.execute(
                user_visible_query(
                    Fleet, user.id, user_group_ids, is_admin=is_admin
                )
                .where(Fleet.is_archived.is_(False))
                .order_by(Fleet.name)
            )
        )
        .scalars()
        .all()
    )
    # Template containers have no admin bypass anywhere (``search_containers``):
    # a report template is offered to whoever may dress a report in it.
    report_templates: list = []
    if _want(SOURCE_REPORT_TEMPLATES):
        from app.services.template_containers import REPORT_KIND, search_containers

        report_templates = await search_containers(
            db, user_id=user.id, user_group_ids=user_group_ids, kind=REPORT_KIND
        )

    # Assignments carry no audience of their own — they inherit their agent's,
    # with the same admin bypass as the agents above.
    assignments = [] if not _want(SOURCE_ASSIGNMENTS) else (
        (
            await db.execute(
                select(Assignment)
                .where(
                    assignment_visible_filter(
                        user.id, user_group_ids, is_admin=is_admin
                    )
                )
                .order_by(Assignment.created_at.desc())
            )
        )
        .scalars()
        .all()
    )

    flows = [] if not _want(SOURCE_FLOWS) else (
        (
            await db.execute(
                user_visible_query(Flow, user.id, user_group_ids, is_admin=is_admin)
            )
        )
        .scalars()
        .all()
    )

    # Platform variables for the Value node's picker. Secrets are deliberately
    # EXCLUDED: a primitive's value is published into run journals and step
    # outputs, which would put the secret in clear text.
    from app.models.variable import Variable

    variables = [] if not _want(SOURCE_VARIABLES) else (
        (
            await db.execute(
                user_visible_query(
                    Variable, user.id, user_group_ids, is_admin=is_admin
                )
                .where(Variable.is_secret.is_(False))
                .order_by(Variable.name)
            )
        )
        .scalars()
        .all()
    )

    from app.models.flow_event import FlowEventType

    event_types = [] if not _want(SOURCE_EVENT_TYPES) else (
        (
            await db.execute(
                user_visible_query(
                    FlowEventType, user.id, user_group_ids, is_admin=is_admin
                ).order_by(FlowEventType.name)
            )
        )
        .scalars()
        .all()
    )

    # Built-in tools first: they exist on every platform, so the Tool node is
    # never an empty select on an instance with no custom tools yet. Values
    # are `builtin:<id>` so the runner can tell them from custom-tool UUIDs.
    # The registry is `_VALID_BUILTIN_TOOL_IDS` (services/builtin_tools/
    # agent_builder.py); labels are curated here because no backend surface
    # serves humanized names today. Platform-disabled tools are excluded.
    from app.core.config import settings as app_settings
    from app.core.feature_flags import is_feature_enabled
    from app.models.agent_skill import AgentSkill
    from app.models.custom_tool import CustomTool

    _builtin_tools = BUILTIN_TOOL_CATALOG
    builtin_tool_options = [
        {
            "value": f"builtin:{tool_id}",
            "label": f"{label} (built-in)",
            "icon": icon,
            "description": desc,
        }
        for tool_id, label, icon, desc in _builtin_tools
        if not (tool_id == "code_interpreter" and security_option("disable_code_interpreter"))
        # ``flow_builder`` names the visual builder, so it follows the
        # "FLOW_BUILDER" feature flag. Filtered HERE rather than at the
        # consumers because this catalog has more than one: besides the flow
        # canvas it feeds the agent-creation wizard's "Tools the agent can
        # call" step, which is precisely why the /flows router stays mounted
        # while the flag is off — so the one endpoint that must keep answering
        # was also the one still advertising the feature.
        and not (tool_id == "flow_builder" and not is_feature_enabled("FLOW_BUILDER"))
        # The same rule for the suites the runtime withholds while their
        # deployment switch is off: offered, they would be wired and dropped.
        and not (tool_id == "video_studio" and not is_feature_enabled("VIDEO_STUDIO"))
        and not (tool_id == "custom_dashboards" and not is_feature_enabled("DASHBOARD_STUDIO"))
        and not (tool_id == "phone" and not is_feature_enabled("MOBILE_COMPANION"))
        and not (tool_id == "deep_investigation" and not is_feature_enabled("DEEP_INVESTIGATION"))
        and not (tool_id == "computer_use" and not app_settings.runners_enabled)
    ]

    custom_tools = [] if not _want(SOURCE_TOOLS, SOURCE_CUSTOM_TOOLS) else (
        (
            await db.execute(
                user_visible_query(
                    CustomTool, user.id, user_group_ids, is_admin=is_admin
                )
            )
        )
        .scalars()
        .all()
    )
    skills = [] if not _want(SOURCE_SKILLS) else (
        (
            await db.execute(
                user_visible_query(
                    AgentSkill, user.id, user_group_ids, is_admin=is_admin
                )
            )
        )
        .scalars()
        .all()
    )

    runners: list = []
    if app_settings.runners_enabled and _want(SOURCE_RUNNERS):
        from app.models.runner import Runner
        from app.services.runners import is_runner_online

        runners = (
            (
                await db.execute(
                    user_visible_query(
                        Runner, user.id, user_group_ids, is_admin=is_admin
                    )
                )
            )
            .scalars()
            .all()
        )

    from app.services.relays import relays_active

    relay_groups: list = []
    if relays_active() and _want(SOURCE_RELAY_GROUPS):
        from app.models.relay import RelayGroup

        relay_groups = (
            (
                await db.execute(
                    user_visible_query(
                        RelayGroup, user.id, user_group_ids, is_admin=is_admin
                    ).order_by(RelayGroup.name)
                )
            )
            .scalars()
            .all()
        )

    # Selectable LLM models across configured providers — the agent node's
    # model override picks from the same curated catalogs the agent editor uses.
    from app.llm.provider_access import provider_routing
    from app.llm.router import get_available_models
    from app.models.llm_config import LLMProviderConfig

    # A provider restricted to groups and users is offered to them, and to an
    # admin, who may edit anyone's flow.
    models = (
        await get_available_models(db, user_id=str(user.id), every_provider=is_admin)
        if _want(SOURCE_MODELS)
        else []
    )
    # The provider INSTANCES themselves, so an LLM node can pin one. Model ids
    # are not unique across providers (a gateway republishes its upstream's
    # catalog verbatim), so `provider_id` rides along on every model option and
    # the builder filters the model list once a provider is pinned.
    provider_configs = (
        (await db.execute(select(LLMProviderConfig))).scalars().all()
        if _want(SOURCE_LLM_PROVIDERS)
        else []
    )
    if not is_admin and provider_configs:
        provider_configs = (await provider_routing(db, str(user.id), provider_configs)).usable

    def _desc(text: str | None) -> str | None:
        text = (text or "").strip()
        return text[:200] or None

    return {
        SOURCE_FLOWS: [{"value": str(f.id), "label": f.name} for f in flows],
        SOURCE_TOOLS: builtin_tool_options
        + [
            {
                "value": str(tool.id),
                "label": tool.name,
                "description": _desc(tool.description),
                "icon": tool.icon_url or "Wrench",
            }
            for tool in custom_tools
        ],
        SOURCE_SKILLS: [
            {
                "value": str(skill.id),
                "label": skill.name,
                "description": _desc(skill.description),
                "icon": "GraduationCap",
            }
            for skill in skills
        ],
        SOURCE_MODELS: [
            {
                "value": m["model"],
                "label": f"{m['model']} ({m['provider']})",
                "provider_id": m.get("provider_id"),
                "icon": "Sparkles",
            }
            for m in models
        ],
        # Provider instances are offered to every builder user, not only
        # admins: pinning one is what makes a flow's LLM choice reproducible,
        # and a non-admin authors flows too. The vendor names this reveals are
        # already visible in SOURCE_MODELS above (`model (provider)`), so the
        # option carries the label alone — no model inventory, no counts.
        SOURCE_LLM_PROVIDERS: [
            {
                "value": str(cfg.id),
                "label": cfg.display_name or cfg.provider,
                "icon": "Cpu",
            }
            for cfg in provider_configs
        ],
        SOURCE_AGENTS: [{"value": str(a.id), "label": a.name} for a in agents],
        SOURCE_INTEGRATIONS: [
            {
                "value": str(i.id),
                "label": f"{i.name} ({i.provider})",
                "description": _desc(i.description),
                "icon": "Plug",
            }
            for i in integrations
        ],
        SOURCE_ASSIGNMENTS: [
            {"value": str(a.id), "label": a.name} for a in assignments
        ],
        SOURCE_VARIABLES: [
            {
                "value": str(v.id),
                "label": v.name,
                "description": _desc(v.description),
                "icon": "Variable",
            }
            for v in variables
        ],
        SOURCE_CUSTOM_TOOLS: [
            {
                "value": str(tool.id),
                "label": tool.name,
                "description": _desc(tool.description),
                "icon": tool.icon_url or "SquareCode",
            }
            for tool in custom_tools
        ],
        SOURCE_RUNNERS: [
            {
                "value": str(r.id),
                "label": r.name,
                "description": (
                    ("online" if is_runner_online(r) else "offline")
                    + (f" — {r.platform}" if getattr(r, "platform", None) else "")
                ),
                "icon": "MonitorSmartphone",
            }
            for r in runners
        ],
        SOURCE_EVENT_TYPES: [
            {
                "value": str(et.id),
                "label": et.name,
                "description": _desc(et.description),
                "icon": "Radio",
            }
            for et in event_types
        ],
        SOURCE_WORKSPACES: [
            {
                "value": str(w.id),
                "label": w.name,
                "description": _desc(w.description),
                "icon": "LayoutGrid",
            }
            for w in workspaces
        ],
        SOURCE_RELAY_GROUPS: [
            {
                "value": str(g.id),
                "label": g.name,
                "description": _desc(g.description),
                "icon": "Network",
            }
            for g in relay_groups
        ],
        SOURCE_FLEETS: [
            {
                "value": str(f.id),
                "label": f.name,
                "description": _desc(f.description),
                "icon": "Boxes",
            }
            for f in fleets
        ],
        SOURCE_REPORT_TEMPLATES: [
            {
                "value": str(t.id),
                "label": t.name,
                "description": _desc(t.description),
                "icon": "FileText",
            }
            for t in report_templates
        ],
        SOURCE_KNOWLEDGE_BASES: [
            {
                "value": str(kb.id),
                "label": kb.name,
                "description": _desc(kb.description),
                "icon": "BookOpen",
            }
            for kb in knowledge_bases
        ],
    }


async def get_visible_run(
    db: AsyncSession, user: User, run_id: str, *, is_admin: bool | None = None
) -> FlowRun:
    """A run is visible iff its flow is (the run snapshots the sharing).

    *is_admin* is the admin bypass, the caller's role by default; the
    flow-builder tool passes ``agent_tool_admin_bypass()``.
    """
    from app.api.v1.helpers import (
        get_group_permission,
        get_user_group_ids,
        get_user_permission,
    )

    rid = parse_path_id(run_id, not_found_detail="Run not found")
    run = (
        await db.execute(select(FlowRun).where(FlowRun.id == rid))
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if user.is_admin if is_admin is None else is_admin:
        return run
    # Tier for tier the same rule ``user_visible_filter`` applies to the flow
    # itself, over the run's snapshot of it: company-managed is a blanket grant
    # only while the flow is not narrowed to explicit grants, and a
    # narrowed one falls back to owner + user grants + group grants — exactly
    # what the flow listing does. ``access_restricted`` reads through
    # ``is not True`` so a NULL (never configured) stays unrestricted.
    if run.is_company_managed and run.access_restricted is not True:
        return run
    if run.user_id == user.id:
        return run
    # Through the shared grant resolvers rather than a raw set intersection:
    # they canonicalise the stored UUIDs, so a hand-edited or non-canonical id
    # cannot grant here what it would not grant on the flow itself. A run has no
    # permission maps of its own; every grant it carries resolves to read_only,
    # which is all a journal read needs.
    if get_user_permission(run, user.id) is not None:
        return run
    group_ids = await get_user_group_ids(db, user.id)
    if get_group_permission(run, group_ids) is not None:
        return run
    raise HTTPException(status_code=404, detail="Run not found")
