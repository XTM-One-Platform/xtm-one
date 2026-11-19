# Concierge sandbox

Level 2 of the Platform Concierge (#1783, issue #8255; PRD `dev-docs/platform-concierge.md`, section 6.6). Code: `backend/app/services/concierge/sandbox/`.

## Which calls write

| Call | Read through | Unknown |
|---|---|---|
| A tool | `services/tool_classification.py` on the definition's `base_name` (`side_effects.tool_call_effect`), only for a source the platform vouches for (`side_effects.vouched_source`: built-ins, the integration provider catalog, enabled company-managed MCP servers, as `hitl_whitelist_derived` derives reads); a custom tool, a personal MCP server, an imported provider or a channel bot is a write whatever its name | write |
| A flow node | `NodeKind.side_effect` on its catalog entry (`none`, `tool`, `nested`); a `tool` node is its tool's effect, read by name only for a flow-callable built-in or an operation of a catalog provider's integration (`side_effects.node_tool_vouched`) | write |

`ToolDefinition.reads_own_configuration` marks a tool that reads only the running agent's own configuration (`load_skill`, `read_skill_resource`, `search_memory`): it runs live, never from a recording, so a variant is measured on its own skills and memory.

## Interception

`tools.base.intercepting_dispatch` binds a `DispatchInterceptor` that `CompositeToolExecutor.execute_tool` calls after name resolution and the approval gate (which sandbox turns skip); a tool a turn runs outside its composite (a pipeline PREPARE / FINALIZE stage) is wrapped in `InterceptedExecutor`, which does the same. The sandbox's is `session.SandboxSession`:

1. **Replay**: a call matching a recording of the source traces gets the recorded output (read or write), once the recording's consent still holds (level 1, read at each use).
2. A read runs live.
3. A write is not executed: it is answered by a model simulating it, grounded on that tool's consented recorded outputs (`simulation.py`), or, when no model answers, by a result saying it was intercepted with an unknown outcome.

Matching is exact first, then normalized (whitespace, empty values, dates and timestamps masked); identifiers are never masked. A key recorded N times answers in recorded order, then keeps the last one. Every answered call is stamped `sandbox_outcome` on its trace span, and traces of copies are never indexed: a simulated output is never replayed as a recording.

`SandboxReport` counts `replayed`, `live_reads`, `simulated_writes`, `intercepted_writes`, `unanswered_writes` per case, stored on `sandbox_runs`.

**Dry-run** (`ConciergeSettings.dry_run`): a Concierge turn (`run_agent_chat`, or the assignment engine's own loop through `turns.turn_binding`) is bound to a `dry_run` session, whose writes are logged and answered "not executed", without a model. A service writing without a tool asks `dry_run.dry_run_active()` and records with `record_dry_run_write()`.

## Copies

`copies.create_sandbox_copy` clones an agent (the copy manifest, `models/agent_copy.py`; no web exposure, `channel_config` NULL) or a flow into a row with `is_sandbox`, `user_id` NULL, not company-managed, unshared, and a `sandbox_copies` row; the channel router's agent queries exclude `is_sandbox` rows too. `helpers.hidden_rows_excluded()` (the one clause for rows no listing shows anyone: `is_internal`, `is_sandbox`, inside `user_visible_filter`, `company_wide_visible_filter` and `visible_to_user_column_clause`) keeps it out of every listing, picker and fetch by id, admins included; analytics leave its traces out. A copy stands for a subject (`flow_lab_access.copy_subject`): its source, else the agent whose usage it automates as the server recorded it in `created_for` (a Flow Lab prototype's `flow_lab:agent:<id>`, a new-flow experiment's agent through `concierge_experiment:<id>`); a caller's `created_for` never starts with a server marker (`is_server_marker`). A copy runs as its subject would: for the subject's owner, or a user the server decides (`start_sandbox_flow_run(run_as_user_id=)`, a shadow run); an owner-less copy runs as an administrator only on that administrator's own call outside a Concierge run, never as the administrator the Concierge acts for. Its turns keep the Concierge's reach whoever started them (`identity.in_health_reach`: no admin bypass, no consent-free trace or insights read; the runner's Concierge principal stays bound over them), and it carries none of `fleet_seed.NON_CONSENT_TOOL_GROUPS` (`copies.sandbox_tool_ids`, at creation and at each turn). It expires 14 days after creation; a proposal extends it (`retain_sandbox_copy`, `register_sandbox_reference_check`; the purge retains a referenced copy another 14 days); `purge_expired_sandbox_copies` deletes the rest with their traces.

Every run of a sandbox agent (`run_agent_chat`, decorated by `turns.intercepted_agent_turn`) or a sandbox flow (`FlowWalker.run_burst`) is intercepted, whoever started it, and so is a flow it calls (the `parent_run_id` chain). A sandbox turn (`run_sandbox_agent`, an agent node of a sandbox flow) runs as an evaluation turn with `evaluation_tool_approval=False`: no memory, no metered execution, no approval gate, so the interceptor decides each call. A flow node is replayed from the evaluation entry's recordings when the run answers one (`start_sandbox_flow_run(entry_id=)`), else from the source runs' completed steps (node id, kind, rendered input), else run (`none`, `nested`, `read`) or simulated (`write`). Every run (`run_sandbox_agent`, `sandbox_case`, `start_sandbox_flow_run` without a server-decided runner), an evaluation's included, re-reads the subject's reach (`tools.subject_refusal`) and a copy standing for nothing does not run. `start_sandbox_flow_run` refuses a graph naming an id its source flow or basis agent does not (`flow_lab_access.sandbox_reach`), or, for a shadow of a copy with neither (a switch-over), one its runner could not save, then applies an agent's run-time checks (`check_three_tier_references`, `check_tool_references`). An answered step's output carries `sandbox_outcome`, and a run started by or called from a sandbox run carries `trigger_context.source = "concierge_sandbox"`: neither is a recording (`load_recorded_steps`), and such runs stay out of the flow run listings (`flows.production_runs_only`).

## Refusals

In a Concierge run, `agent_builder` offers its reads only and refuses its writes at call time; `flow_builder` refuses `update_flow_graph`, `activate_flow`, `deactivate_flow` and `run_flow` on a flow that is not a sandbox copy, refuses `create_flow_event_type` (shared configuration no flow scopes), and `create_flow` makes a sandbox flow (`guards.py`).

## Tools

Group `concierge_sandbox` (gate `CONCIERGE`), also on `/mcp/xtm`: `create_sandbox_copy`, `run_sandbox`, `get_sandbox_run`. One rule (`tools.sandbox_access_refusal`): the flag on, the Concierge or an administrator, the copy's subject in the supervision scope and not a fleet agent, and its content readable (`consent.content_readable`: opted in, its owner not opted out), read again at every call, so a revoked opt-in stops the runs of a copy already made. `create_sandbox_copy` needs a source (a new flow is the Flow Lab's `create_sandbox_flow`, whose source flow and basis agent pass the same subject rule).
