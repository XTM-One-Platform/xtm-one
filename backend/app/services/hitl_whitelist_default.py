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

"""The HITL whitelist shipped with the product.

**This file is data, and is reviewed as data.** Adding a name here exempts that
tool from human approval on every deployment that has not overridden it, so a
change to this list is a security-relevant content change, not a refactor.

It is a plain constant rather than a database seed on purpose. Because the
default lives in code, deploying a new release *is* the upgrade path for it:
new entries take effect with no administrator action, and there is no
whitelist data migration to get wrong. Administrator additions and removals are
stored separately (``hitl_whitelist_overrides``) and are applied on top, so an
operator's deliberate removal survives a release that re-adds the tool here.

``SHIPPED_WHITELIST_VERSION`` exists for display and support diagnostics only.
No logic may branch on it — if you find yourself wanting to, the two-layer
resolution in ``app.services.hitl_whitelist`` is the thing to change instead.

Inclusion rule: a tool belongs here only if calling it cannot change data
inside the platform and cannot reach outside it. When in doubt, leave it out —
an omitted tool merely prompts a human, whereas a wrongly-included one runs
unattended everywhere until somebody notices.
"""

# Bumped whenever the set below changes. Display/diagnostics only — never
# branch on this value.
SHIPPED_WHITELIST_VERSION = 12

# --------------------------------------------------------------------------
# Read-only inspection of platform objects.
# --------------------------------------------------------------------------
_READ_ONLY_GETTERS = frozenset(
    {
        "get_agent",
        "get_assignment",
        "get_custom_tool",
        "get_evaluation",
        "get_evaluation_run_values",
        "get_integration",
        "get_kb_document",
        "get_knowledge_base",
        "get_knowledge_document",
        "get_mcp_server",
        # A placebo comparison's runs and statistics: read, nothing called.
        "get_placebo_comparison",
        # A program's status computed from its work items and the pull
        # requests they name, read live: nothing is written.
        "get_program_status",
        "get_presentation",
        "get_prompt",
        "get_report",
        "get_run",
        "get_shared_state",
        "get_skill",
        "get_template",
        "get_variable",
        # One work item the agent can already list, in full; the files its
        # creator attached are put in the turn's context, no row is written.
        "get_work_item",
    }
)

_READ_ONLY_LISTERS = frozenset(
    {
        "list_agent_templates",
        "list_agents",
        "list_assignments",
        "list_available_resources",
        "list_background_tasks",
        "list_custom_tools",
        "list_evaluation_runs",
        "list_evaluations",
        # The caller's own friction ledger rows: read, nothing called.
        "list_frictions",
        # The platform's groups as ``GET /groups`` serves them (names, member
        # counts; members for an admin's agent): read, nothing called.
        "list_groups",
        "list_integration_catalog",
        # A provider's tool names and their read/write access, read from the
        # tool builders with no credential: nothing is called.
        "list_integration_tools",
        "list_integrations",
        "list_kb_documents",
        "list_knowledge_bases",
        "list_knowledge_documents",
        "list_mcp_servers",
        # Program roots with their children counted: read, nothing written.
        "list_programs",
        "list_prompts",
        "list_runs",
        "list_shared_state",
        "list_skills",
        "list_variables",
        "list_work_items",
    }
)

_READ_ONLY_SEARCH = frozenset(
    {
        "search_knowledge",
        "search_memory",
        # Reads the text of a deck the caller can already open, through the
        # same visibility check as ``get_presentation``; writes nothing.
        "search_presentation_text",
        "search_templates",
        # Reads the text of the skills, prompts, agents and assignments the
        # caller can already list, through the same visibility filters.
        "search_config_text",
        # Reads a skill definition or one of its resource files into context.
        "load_skill",
        "read_skill_resource",
    }
)

# Observability over runs the caller can already see. These read trace data;
# they do not re-run anything.
_READ_ONLY_TRACES = frozenset(
    {
        "inspect_agent_traces",
        "inspect_my_traces",
    }
)

# The platform's own figures: analytics, objects, builds, adoption, the
# audit trail and an overview, each read for the caller's scope (the platform
# for an administrator's agent, their own slice otherwise). ``render_chart``
# validates rows handed to it into a chart block: nothing persisted, no egress.
# ``query_audit_log`` writes one audit row of its own read, as
# ``inspect_agent_traces`` does - platform bookkeeping, not a change.
# ``get_platform_ai_settings`` reads the AI settings for an admin;
# its sibling ``update_platform_ai_settings`` is a write and stays gated.
# ``query_platform_metrics`` reads the in-process Prometheus registry.
# ``query_platform_frictions`` counts the friction ledger's rows.
# ``get_user_default_agents`` reads each person's own default chat agent.
# ``list_oauth_defaults`` reads which providers have an admin OAuth default
# (booleans, never a value); its upsert and delete siblings stay gated.
_READ_ONLY_PLATFORM_INSIGHTS = frozenset(
    {
        "get_platform_ai_settings",
        "get_platform_overview",
        "get_platform_release_history",
        "get_user_default_agents",
        "list_oauth_defaults",
        "list_platform_sessions",
        "list_platform_traces",
        "query_ai_adoption",
        "query_audit_log",
        "query_platform_analytics",
        "query_platform_frictions",
        "query_platform_metrics",
        "query_platform_objects",
        "render_chart",
    }
)

# Retrieval of results from work the platform has already done. Retrieving a
# result does not re-execute the task that produced it.
_READ_ONLY_RESULTS = frozenset(
    {
        "collect_task_results",
        "get_full_task_result",
        "get_task_result",
    }
)

# Pure computation over data supplied in the call or already readable. No
# persistence, no egress.
_READ_ONLY_ANALYSIS = frozenset(
    {
        "analyze_stix_timeline",
        "compare_stix_bundles",
        "compute_fimi_derived_metrics",
        "compute_fimi_metrics",
        "run_statistical_test",
        # Parses a bundle handed to it in the call and reports what is wrong
        # with it. Held out of the shipped default at first on the suspicion
        # that parsing untrusted third-party STIX might have side effects — it
        # does not. The module imports json, uuid and copy and nothing else,
        # and its one network-capable dependency (pycti) is constructed against
        # an unroutable address with ``perform_health_check=False`` purely to
        # reach a local deterministic-ID helper. Nothing is persisted and
        # nothing leaves the process, which is the same ground on which
        # ``analyze_stix_timeline`` and ``compare_stix_bundles`` sit above.
        "stix_bundle_validator",
        # Reads the run's own file or text, calls the run's own model and reads
        # the public ATT&CK catalogue (the MITRE ATT&CK integration's source);
        # writes nothing anywhere.
        "extract_stix_from_document",
        "validate_presentation",
    }
)

# --------------------------------------------------------------------------
# Deliberate individual decisions — each of these deserves its own reasoning.
# --------------------------------------------------------------------------
_DELIBERATE_INCLUSIONS = frozenset(
    {
        # ⚠️ HIGHEST-RISK ENTRY IN THIS FILE. ⚠️
        #
        # ``code_interpreter`` executes arbitrary code written by the model. It
        # is exempted here by an explicit product decision (confirmed with
        # the product owner), on the grounds that an agent which pauses before
        # every computation is impractical and that the sandbox bounds the blast
        # radius.
        #
        # Be clear-eyed about what that trades away: with this entry present,
        # generated code runs with no human in the loop on every run, including
        # scheduled ones that nobody is watching. It also leans on the sandbox as
        # a security boundary, which the project constitution's Security
        # Requirements deliberately decline to treat as complete ("defense-in-
        # depth, not a complete security boundary on their own").
        #
        # This is the FIRST entry to remove for a stricter posture, and it should
        # be re-examined if the sandbox's isolation guarantees change. Removing
        # it is a one-line administrator action, not a fork.
        "code_interpreter",
        # ``request_human_input`` cannot do anything except ask a human. Gating
        # it would mean pausing for approval to ask for approval, so it is
        # exempt to avoid a pointless double prompt. It never reaches an
        # executor in any case — it raises immediately.
        "request_human_input",
        # ``ask_the_caller`` is the same thing on an external A2A task: it only
        # puts a question to the partner that tasked the agent. Gated, the task
        # completed with "approval required" instead of reaching input-required.
        "ask_the_caller",
        # ``transfer_to_agent`` hands the conversation to a specialist. Exempt
        # on three grounds, and the middle one is what the other two rest on:
        #
        # 1. It executes nothing. The tool resolves the target, records it in an
        #    in-memory handover dict and returns a sentence; the caller runs the
        #    target afterwards (``general_chat``/``agent_chat``).
        # 2. The agent it hands to is gated independently — ``run_agent_chat``
        #    installs its own approval gate, so every tool the specialist calls
        #    is still evaluated one by one. This is the invariant that makes the
        #    exemption safe rather than a hole, and it is pinned by
        #    ``tests/test_handover_gating.py``. If a future refactor runs a
        #    handover target without a gate, REMOVE THIS ENTRY.
        # 3. Gating it does not delay a consequential action, it prevents a
        #    routing decision. The tool is terminal (``exit_tool_names``), so
        #    the loop stops right after it — and on the two surfaces that
        #    install a gate with no way to answer one, the General Assistant and
        #    the public embed, that turned every handoff into "I need approval
        #    before running: transfer_to_agent", with no way to grant it. The
        #    feature was unreachable rather than merely slower.
        #
        # Unlike the entries above it, this one is not side-effect-free: the
        # conversation records the agent it came from so the user can be handed
        # back. That is the user's own conversation, changed by their own
        # request, and reversible — which is why it sits here with its reasons
        # written out rather than among the read-only getters.
        "transfer_to_agent",
        # ``wait`` is the agent's timer and does nothing else: it sleeps, is
        # bounded by ``maximum_wait_time`` / ``maximum_total_wait_time`` rather
        # than by the tool budget, is interruptible from both sides (cancel the
        # run, or skip just the pause), and a round that only waited does not
        # even consume a loop iteration.
        #
        # Gating it is the same absurdity ``request_human_input`` is exempt to
        # avoid, pointing the other way: pausing to ask permission to pause. And
        # it is worse than a double prompt — a gated agent that delegates work
        # and waits 20 seconds instead enters ``awaiting_human`` until a person
        # answers, so the wait budget that exists to bound the pause is replaced
        # by a pause with no bound at all.
        "wait",
        # ``check_task_status`` is the poll beside it, and was excluded on the
        # record until the usage pattern was understood — that exclusion invited
        # exactly this revisit. It reads a status of work the agent already
        # started and does nothing else, and the thing that made it look risky
        # (driving a polling loop) is bounded elsewhere: its own short, fixed
        # budget in ``sub_agent._STATUS_WAIT_BUDGET``, which the model cannot
        # steer, and the iteration waiver it shares with ``wait``.
        #
        # Gated, it produced the same absurdity: an agent that delegated work
        # stopped to ask permission to look at whether the work had finished.
        "check_task_status",
        # The sandbox workspace's file tools do nothing
        # ``code_interpreter`` - exempt above - cannot do itself: they move a
        # file between the conversation and the conversation's own sandbox
        # directory, or remove one from it. None reaches outside the platform.
        # They follow ``code_interpreter``: removing it for a stricter posture
        # is the cue to remove these four too.
        "sandbox_list_files",
        "sandbox_put_file",
        "sandbox_get_file",
        "sandbox_remove_files",
    }
)

# ``load_tools`` is deliberately NOT here, and it is the near miss worth
# recording: it looks like the two above but never reaches the gate at all. The
# loop answers it inline (``approval.LOOP_HANDLED_TOOLS``) and records it as
# ``auto_run_meta_not_dispatched``, so an entry for it would be configuration
# that decides nothing, on a screen where every other line decides something.

SHIPPED_DEFAULT_WHITELIST: frozenset[str] = (
    _READ_ONLY_GETTERS
    | _READ_ONLY_LISTERS
    | _READ_ONLY_SEARCH
    | _READ_ONLY_TRACES
    | _READ_ONLY_PLATFORM_INSIGHTS
    | _READ_ONLY_RESULTS
    | _READ_ONLY_ANALYSIS
    | _DELIBERATE_INCLUSIONS
)


# --------------------------------------------------------------------------
# Deliberate exclusions.
#
# These are NOT part of the whitelist and are listed only so that a future
# reader can see they were considered rather than overlooked. Nothing consumes
# this constant; a tool gates simply by being absent from the set above.
# --------------------------------------------------------------------------
DELIBERATELY_EXCLUDED: dict[str, str] = {
    "browser": (
        "Drives a real browser session: reaches outside the platform and can "
        "act on whatever it reaches."
    ),
    "web_fetch": (
        "Fetches arbitrary URLs. Egress is a side effect on someone else's "
        "system, and the destination is model-chosen."
    ),
    "materialize_repository": (
        "Downloads a repository's tree through a connected integration, like "
        "the hand-gated download tools it calls; approving it approves that "
        "download for the call."
    ),
    "sandbox_checkout_diff": (
        "Reads the changed files' originals through a connected integration "
        "(its repository-file download tool), like materialize_repository; "
        "approving it approves those reads for the call."
    ),
    "sandbox_checkout_rebase": (
        "Downloads the repository's tree at a new ref and the changed files' "
        "originals through a connected integration, like materialize_repository, "
        "and rewrites the checkout's files; approving it approves those reads "
        "for the call."
    ),
    "consult_agent": (
        "Invokes another agent inline and returns its answer. NOT because the "
        "consulted agent runs ungated — it goes through ``run_agent_chat`` and "
        "is gated like any other, the same property that makes "
        "``transfer_to_agent`` exempt. The reason is that a pause cannot cross "
        "the consult boundary: the gated call raises inside the nested chat, "
        "behind the consult's own error handling, and reaches the user as "
        "\"Consultation failed\" rather than as a question they could answer. "
        "Exempting the consult would not fix that, and would hide it."
    ),
}
