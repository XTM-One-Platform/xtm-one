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

"""The platform's registry of built-in tool ids and their sub-tool definitions.

:func:`build_builtin_tool_catalog` is the one list of every built-in suite an
agent can carry (``Agent.builtin_tool_ids``), each built from its runtime
builder so it can never drift from the tools the agents actually get. Read by
the settings catalog endpoint, skill-package validation, the HITL whitelist
and tool-approval surfaces, and the built-in suite index of
``app.tools.builtin_suites``.

The builders are imported inside the function: importing this module stays
cheap, and a caller in ``app.tools`` keeps the builders out of its own import.
"""

from __future__ import annotations


def withhold_evaluation_tools(catalog: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Drop the evaluation tools from the ``platform`` group while evaluation is off.

    For what an endpoint offers; the registry itself keeps them. A member
    catalog keyed by the former ``agent_builder`` id is handled the same way.
    """
    from app.services.builtin_tools.evaluations import EVALUATION_TOOL_NAMES
    from app.services.evaluation.availability import evaluation_enabled

    if evaluation_enabled():
        return catalog
    for key in ("platform", "agent_builder"):
        if key in catalog:
            catalog[key] = [d for d in catalog[key] if d["name"] not in EVALUATION_TOOL_NAMES]
    return catalog


def withhold_health_tools(catalog: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Drop every Platform Health group (``gate="health"``) while Platform Health is not available.

    For what an endpoint offers; the registry itself keeps them, so an agent
    already carrying one stays valid.
    """
    from app.services.builtin_tool_groups import BUILTIN_TOOL_GROUPS
    from app.services.health.availability import health_enabled

    if health_enabled():
        return catalog
    for group in BUILTIN_TOOL_GROUPS:
        if group.gate == "health":
            catalog.pop(group.id, None)
    return catalog


def withhold_flow_builder_tools(catalog: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Drop the flow functions from the ``platform`` group while "FLOW_BUILDER" is off.

    The flow builder was a card of its own, offered only with the flag on; it
    is a sub-area of the platform group now, so what an endpoint offers drops
    its functions instead of the card. The registry itself keeps them (see
    :func:`build_builtin_tool_member_catalog`).
    """
    from app.core.feature_flags import is_feature_enabled
    from app.services.builtin_tool_aliases import alias_scoped_functions

    if is_feature_enabled("FLOW_BUILDER"):
        return catalog
    flow_functions = set(alias_scoped_functions("flow_builder"))
    catalog.pop("flow_builder", None)
    if "platform" in catalog:
        catalog["platform"] = [d for d in catalog["platform"] if d["name"] not in flow_functions]
    return catalog


def build_builtin_tool_catalog(*, include_disabled: bool = False) -> dict[str, list[dict]]:
    """Build sub-tool definitions for every built-in group, keyed by group id.

    :func:`build_builtin_tool_member_catalog` lists every builder under the id
    it had before the consolidation; a group that merged several former ids
    (``builtin_tool_aliases.BUILTIN_GROUP_ALIASES``) is listed here
    once, under its group id, with the functions of its former ids in
    declaration order. A former id is never a key: it is a [DEPRECATED] alias
    the readers resolve. Key order follows the member catalog (the group
    takes the position of its first former id), so a tool's owner is still
    the first group that lists it.
    """
    from app.services.builtin_tool_aliases import alias_group

    members = build_builtin_tool_member_catalog(include_disabled=include_disabled)
    catalog: dict[str, list[dict]] = {}
    for key, definitions in members.items():
        group = alias_group(key)
        if group is None:
            catalog.setdefault(key, []).extend(definitions)
            continue
        merged = catalog.setdefault(group, [])
        names = {d["name"] for d in merged}
        merged.extend(d for d in definitions if d["name"] not in names)
    return catalog


def build_builtin_tool_member_catalog(*, include_disabled: bool = False) -> dict[str, list[dict]]:
    """Build sub-tool definitions for every built-in builder, keyed by its own id.

    The ids are those the builders had before the consolidation:
    ``web_fetch`` and ``browser`` stay two keys here although they form the
    ``web`` group. :func:`build_builtin_tool_catalog` is the registry by group.

    Returns a dict mapping ``builtin_id`` to a list of
    ``{name, description, input_schema}`` dicts.  Tool builders are called
    with dummy parameters so no real side-effects occur.

    The keys are the platform's registry of built-in ids: every id the runtime
    wires from ``Agent.builtin_tool_ids`` (``agent_chat`` / the assignment
    engine). That is a SUPERSET of ``_VALID_BUILTIN_TOOL_IDS`` in
    ``services/builtin_tools/agent_builder.py``, the subset the agent-builder
    tool lets a model select - ``flow_builder`` and ``deep_investigation``
    are registry keys an agent carries through the UI or the REST API only.
    The two trace ids are listed on their own AND under the ``agent_builder``
    alias, in that order: a tool's owner is the first suite that lists it (the
    narrowest grant), the alias is what an agent holding ``agent_builder``
    additionally receives (``agent_chat`` wires it that way).

    ``include_disabled`` also lists suites this deployment has switched off
    (``computer_use`` without Runners). The catalog endpoint and the skill
    package validator want what the platform advertises; the refusal
    classifier in ``app.tools.builtin_suites`` needs every built-in that
    EXISTS, otherwise a call into a disabled suite is mistaken for a typo of
    an unrelated offered tool.
    """
    from app.services.builtin_tools import (
        build_browser_tool,
        build_code_interpreter_tool,
        build_generate_file_tool,
        build_web_fetch_tool,
    )

    catalog: dict[str, list[dict]] = {}

    def _extract(defs: list) -> list[dict]:
        return [
            {
                "name": d.name,
                "description": d.description,
                "input_schema": d.input_schema,
            }
            for d in defs
        ]

    ci_defs, _ = build_code_interpreter_tool()
    catalog["code_interpreter"] = _extract(ci_defs)

    catalog["generate_image"] = [
        {
            "name": "generate_image",
            "description": (
                "Generate an image from a text description using AI. "
                "Returns the generated image to the user."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Text description of the image to generate.",
                    },
                    "size": {
                        "type": "string",
                        "description": "Image size (e.g. '1024x1024').",
                        "default": "1024x1024",
                    },
                },
                "required": ["prompt"],
            },
        }
    ]

    gf_defs, _ = build_generate_file_tool(
        db=None,  # type: ignore[arg-type]
        user_id="",
        conversation_id=None,
    )
    catalog["generate_file"] = _extract(gf_defs)

    wf_defs, _ = build_web_fetch_tool()
    catalog["web_fetch"] = _extract(wf_defs)

    # Deep Investigation. Same dummy-argument pattern: the builder
    # only uses the session, the caller and the event sink at execution time,
    # so a definition can be produced without any of them. Registered here
    # because this function's KEYS are the platform's set of built-in tool
    # ids — an agent or skill package naming one that is absent is rejected.
    from app.services.builtin_tools import build_deep_investigation_tool

    di_defs, _ = build_deep_investigation_tool(
        None,  # type: ignore[arg-type]
        "",
    )
    catalog["deep_investigation"] = _extract(di_defs)

    # Flow-builder sub-tools: the builder discards ``db``/``user_id`` at
    # definition time (they only matter at execution), so dummies are safe —
    # same single-source-of-truth pattern as the trace tools below.
    #
    # NOT filtered on "FLOW_BUILDER" here, deliberately. This function is the
    # platform's registry of built-in tool ids, not a presentation layer:
    # ``agent_skills._build_builtin_id_set()`` derives "which built-ins exist on
    # this platform" from its keys, and rejects a skill package that asks for
    # one that does not. Dropping the key here made importing a package whose
    # linked agent carries ``flow_builder`` fail with 400 on a flag-off
    # instance — the flag is UI-only and must not break an existing agent's
    # tool. The flag is applied by ``get_builtin_tool_catalog`` instead.
    from app.services.builtin_tools import build_flow_builder_tool

    fb_defs, _ = build_flow_builder_tool(
        None,  # type: ignore[arg-type]
        "00000000-0000-0000-0000-000000000000",
    )
    catalog["flow_builder"] = _extract(fb_defs)

    br_defs, _ = build_browser_tool(session_key="__catalog__")
    catalog["browser"] = _extract(br_defs)

    # STIX bundle validator
    from app.services.builtin_tools import build_stix_bundle_validator_tool

    sv_defs, _ = build_stix_bundle_validator_tool()
    catalog["stix_bundle_validator"] = _extract(sv_defs)

    # Grounded STIX extraction: the definition alone, no model at catalog time.
    from app.services.builtin_tools import build_stix_extraction_tool

    sx_defs, _ = build_stix_extraction_tool(None, None)
    catalog["stix_extractor"] = _extract(sx_defs)

    # FIMI analytics tools (single source of truth for the sub-tool set)
    from app.services.builtin_tools import build_fimi_analytics_composite

    fimi_defs, _ = build_fimi_analytics_composite()
    catalog["fimi_analytics"] = _extract(fimi_defs)

    # Platform insights: every call opens its own session and reads the
    # caller's scope at execution time, so the builder needs neither.
    from app.services.builtin_tools import build_platform_insights_tools

    pi_defs, _ = build_platform_insights_tools("")
    catalog["platform_insights"] = _extract(pi_defs)

    # Organization profile editing (#11594): every call checks the acting
    # person is an administrator, so the builder needs no identity here.
    from app.services.builtin_tools import build_organization_context_write_tools

    oc_defs, _ = build_organization_context_write_tools(user_id=None, caller_is_admin=False)
    catalog["organization_context"] = _extract(oc_defs)

    # User administration (#250): every call checks the acting person is an
    # administrator, so the builder needs no identity here.
    from app.services.builtin_tools import build_user_admin_tools

    ua_defs, _ = build_user_admin_tools(user_id=None, caller_is_admin=False)
    catalog["user_admin"] = _extract(ua_defs)

    # Read-only CTEM signals and cases (#1337), from the real builder.
    from app.services.builtin_tools import build_ctem_read_tools

    cr_defs, _ = build_ctem_read_tools(user_id="", agent_id="")
    catalog["ctem_read"] = _extract(cr_defs)

    # CTEM administrator repairs (#1778), from the real builder.
    from app.services.builtin_tools import build_ctem_repair_tools

    crp_defs, _ = build_ctem_repair_tools(user_id="")
    catalog["ctem_repair"] = _extract(crp_defs)

    # CTEM signal and case tools on any agent (#1748), from the real builder.
    from app.services.builtin_tools import build_ctem_tools

    ctem_defs, _ = build_ctem_tools(user_id="", agent_id="")
    catalog["ctem"] = _extract(ctem_defs)

    # Platform Health's one internal group (#1943): memory consolidation (#8258),
    # diagnosis and experiments (#8259), Flow Lab (#8260), module checks (#101 S14),
    # sandbox (#8255), evaluation (#8257) and improvement proposals (#8256). Every
    # call opens its own session and applies its own access rule.
    from app.services.builtin_tools import (
        build_flow_lab_tools,
        build_health_diagnosis_tools,
        build_health_experiment_tools,
        build_memory_steward_tools,
    )
    from app.services.builtin_tools.health_modules import build_health_module_tools
    from app.services.builtin_tools.improvements import build_improvement_tools
    from app.services.health.evaluation_tools import build_health_evaluation_tools
    from app.services.health.sandbox.tools import build_sandbox_tools

    catalog["health_specialists"] = _extract(
        [
            *build_memory_steward_tools()[0],
            *build_sandbox_tools(None, None)[0],  # type: ignore[arg-type]
            *build_health_evaluation_tools(None, None)[0],  # type: ignore[arg-type]
            *build_improvement_tools(None, None)[0],  # type: ignore[arg-type]
            *build_health_diagnosis_tools("")[0],
            *build_health_experiment_tools("")[0],
            *build_flow_lab_tools("")[0],
            *build_health_module_tools("")[0],
        ]
    )

    # Fleets (#7030): every call opens its own session and loads the caller.
    from app.services.builtin_tools import build_fleet_tools

    fleet_defs, _ = build_fleet_tools("")
    catalog["fleets"] = _extract(fleet_defs)

    # ROI (#10006 S2): every call opens its own session and loads the caller.
    from app.services.builtin_tools.roi import build_roi_tools

    catalog["roi"] = _extract(build_roi_tools("")[0])

    # Platform Health orchestrator (#8261): every call opens its own session.
    from app.services.builtin_tools.health_orchestrator import build_health_orchestrator_tools

    co_defs, _ = build_health_orchestrator_tools("")
    catalog["health_orchestrator"] = _extract(co_defs)

    # Work-item queue tools (#1713). Builders take no real side-effects at build
    # time, so dummy parameters are safe here.
    from app.services.builtin_tools import build_work_item_tools

    wi_defs, _ = build_work_item_tools(
        db=None,  # type: ignore[arg-type]
        user_id="",
        agent_id="",
    )
    catalog["work_items"] = _extract(wi_defs)

    # Cross-run shared-state tools.
    from app.services.builtin_tools import build_shared_state_tools

    ss_defs, _ = build_shared_state_tools(
        db=None,  # type: ignore[arg-type]
        user_id="",
        agent_id="",
    )
    from app.services.builtin_tools import build_friction_tools

    fr_defs, _ = build_friction_tools("", "")
    from app.services.builtin_tools import build_timer_tools

    tm_defs, _ = build_timer_tools("", "")
    catalog["shared_state"] = _extract(ss_defs + fr_defs + tm_defs)

    # Workset — a large working corpus.
    from app.services.builtin_tools import build_workset_tools

    # implicit=True: the catalog lists every function of the group, and
    # ``workset_resolve`` is only offered on an implicit corpus (#12168).
    ws_defs, _ = build_workset_tools(
        db=None,  # type: ignore[arg-type]
        user_id="",
        agent_id=None,
        implicit=True,
    )
    catalog["workset"] = _extract(ws_defs)

    # Interactive presentation generation.
    from app.services.builtin_tools import build_presentation_tools

    pres_defs, _ = build_presentation_tools(
        db=None,  # type: ignore[arg-type]
        user_id="",
        conversation_id=None,
    )
    catalog["generate_presentation"] = _extract(pres_defs)

    # Structured report generation.
    from app.services.builtin_tools import build_report_tools

    rep_defs, _ = build_report_tools(
        db=None,  # type: ignore[arg-type]
        user_id="",
        conversation_id=None,
    )
    catalog["generate_report"] = _extract(rep_defs)

    # Agent-built custom dashboards: every call opens its own session.
    from app.services.builtin_tools import build_custom_dashboard_tools

    dash_defs, _ = build_custom_dashboard_tools(None, user_id="")
    catalog["custom_dashboards"] = _extract(dash_defs)
    # A user's linked phones: its own session too.
    from app.services.builtin_tools import build_phone_tools

    phone_defs, _ = build_phone_tools(None, user_id="")
    catalog["phone"] = _extract(phone_defs)

    # Video Studio: every call opens its own session, so the builder
    # needs no session at all.
    from app.services.builtin_tools import build_video_studio_tools

    vs_defs, _ = build_video_studio_tools(user_id="")
    catalog["video_studio"] = _extract(vs_defs)

    # Structured (non-vectorized) template lookup tools.
    from app.services.builtin_tools import build_template_search_tools

    tpl_defs, _ = build_template_search_tools(
        db=None,  # type: ignore[arg-type]
        user_id="",
    )
    catalog["template_search"] = _extract(tpl_defs)

    # Template authoring — the write half, on its own toggle.
    from app.services.builtin_tools import build_template_authoring_tools

    tpl_write_defs, _ = build_template_authoring_tools(
        db=None,  # type: ignore[arg-type]
        user_id="",
    )
    catalog["template_authoring"] = _extract(tpl_write_defs)

    # Document vision — look at a source PDF / PowerPoint instead of reading
    # only its extracted words.
    from app.services.builtin_tools import build_document_vision_tools

    doc_defs, _ = build_document_vision_tools(
        db=None,  # type: ignore[arg-type]
        user_id="",
    )
    catalog["document_vision"] = _extract(doc_defs)

    # Native OS-control Runners — ``computer_use`` (full-desktop screenshots,
    # real-browser control, shell + file ops on an enrolled machine). Only
    # advertised when the Runner subsystem is enabled (or when the caller asks
    # for the disabled suites too — see the docstring).
    from app.core.config import settings as _runner_settings

    if include_disabled or _runner_settings.runners_enabled:
        from app.services.builtin_tools import build_computer_use_tools

        cu_defs, _ = build_computer_use_tools(
            user_id="",
            is_admin=False,
            user_group_ids=[],
            agent_runner_ids=[],
            agent_runner_labels=[],
        )
        catalog["computer_use"] = _extract(cu_defs)

    # Trace + evaluation sub-tools come from the real builders — the same
    # single-source-of-truth pattern as fimi_analytics — so the catalog can
    # never drift from the runtime ToolDefinitions. Both builders discard the
    # ``db`` handle at build time, so dummy parameters are safe here.
    from app.services.builtin_tools import build_evaluation_tools, build_trace_tools

    trace_defs, _ = build_trace_tools(
        db=None,  # type: ignore[arg-type]
        user_id="",
        agent_id="",
    )
    # The registry lists the evaluation tools whatever the EVALUATION flag.
    ev_defs, _ = build_evaluation_tools(
        db=None,  # type: ignore[arg-type]
        user_id="",
        include_withheld=True,
    )
    trace_tools = _extract(trace_defs)

    # One ``observability`` group owns the trace functions; its former
    # ids ``inspect_my_traces`` / ``inspect_agent_traces`` are [DEPRECATED]
    # aliases (``builtin_tool_aliases``). Listed BEFORE ``agent_builder``,
    # which grants them too, so that the group, not the alias, owns them.
    catalog["observability"] = trace_tools

    # Agent-builder tools come from the real builder as well.  It is pure at
    # build time - the body is closure definitions plus ``executor.register``
    # calls, and every closure captures ``db`` / ``user_id`` for execution
    # only - so the same dummy parameters are safe here.  The hand-written
    # copy this replaces had fallen 36 tools behind the runtime (runs,
    # prompts, skills, variables, MCP servers, integrations, the knowledge-base
    # and custom-tool write half), so a refused call to any of those could not
    # be classified as a built-in and fell back to the fuzzy hint.
    from app.services.builtin_tools import build_agent_builder_tool

    ab_defs, _ = build_agent_builder_tool(
        None,  # type: ignore[arg-type]
        "",
    )
    catalog["agent_builder"] = _extract(ab_defs)

    # The platform built-in alias (agent_builder) also grants the trace and
    # evaluation sub-tools, so the alias lists them too (built above).
    catalog["agent_builder"].extend(trace_tools)
    catalog["agent_builder"].extend(_extract(ev_defs))

    # The fleets, flow builder and ROI members are sub-areas of the ``platform``
    # group with ``agent_builder``. A group takes the position of its
    # first former id, and a tool's owner is the first group listing it: kept
    # after ``agent_builder``, the group stays after ``observability``, which
    # owns the trace functions the builder also grants.
    for key in ("fleets", "flow_builder", "roi"):
        catalog[key] = catalog.pop(key)

    return catalog
