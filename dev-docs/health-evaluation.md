# Concierge evaluation

Level 4 of the Platform Concierge (#1783, issue #8257; PRD `dev-docs/platform-concierge.md`, sections 6.7 and 6.8). Code: `backend/app/services/evaluation/availability.py`, `services/evaluation/flow_subject.py`, `services/evaluation/sandbox_cases.py`, `services/concierge/datasets.py`, `services/concierge/evaluations.py`, `services/concierge/evaluation_tools.py`.

## Two gates (decision B9)

| Gate | Reads | Decides |
|---|---|---|
| Surface, `evaluation_enabled()` | `EVALUATION` in a mode where AI runs (`xtm_one`, `copilot`, `dev`) | Every human surface, as `dev-docs/evaluation-feature-flag.md` lists them |
| Engine, `engine_accepts()` | The surface gate, or `CONCIERGE` on and `evaluations.concierge_built` | Whether a queued evaluation or rescore runs |

The Concierge has no user row: its evaluations and datasets have `user_id` NULL, are not company-managed and not shared, so administrators see them and nobody else. `concierge_built` is set only by the Concierge's tools; no copy, export or import carries it.

## A flow as subject

A link (`evaluation_agents`) and a run (`evaluation_runs`) name an agent or a flow (`ck_*_one_subject`). A flow runs once per launch on `MODEL_INDEPENDENT`, in the `flow` execution mode:

- the entry becomes the run's parameters: a JSON-object input is the parameter map, coerced against the manual trigger's declaration (`flow_runner.manual_run_trigger`, the `run_flow` tool's rule); any other input is the run's `prompt` (`message` for a chat trigger);
- the final output judged is the one a `flow.call` parent reads (`flow_runner.final_step_output`), uncut;
- a run that ends failed, cancelled or past the evaluation's timeout answers `Error: ...`: judged and counted, never reused; a run that cannot start (no trigger, parameters that do not fit, owner deactivated) leaves the entry out as a platform error;
- cost and latency come from the run's own trace spans, per serving model (`run_ledger.calls_from_spans`); traces not written in time leave the totals unavailable (`ledger_missing`), never partial;
- the reuse fingerprint names the flow and its version (every save is a version); an agent's fingerprint is unchanged;
- a placebo comparison and callback mode apply to agents only.

## Sandbox subjects

A subject that is a sandbox copy (level 2) runs every entry as one case: an agent copy's turn inside `sandbox_case`, on the entry's own recordings when the entry was built from production; a flow copy through `start_sandbox_flow_run(entry_id=)`, on the same recordings. The case's report (replayed calls, simulated and intercepted writes) is stored on the result under `execution_details["sandbox"]`, and a comparison states the simulated writes beside its verdict (B17).

## Datasets from production (B15, B16)

`build_dataset_from_traces(db, *, target_type, target_id, ...)`:

| Target | Cases to fix | Non-regression cases |
|---|---|---|
| `agent` | Negatively rated answers (chat and assignment runs), failed assignment runs | Positively rated answers |
| `assignment` | The same, restricted to the assignment's runs | The same |
| `flow` | Failed runs | Completed runs (nobody rates a flow run) |

Every source passes level 1's consent rule in the reading query (`consent_filter`) and again right before its content is copied (`require_content_read`, which audits it). A flow run also needs the consent of every agent node in the graph it executed and of every flow its `flow.call` nodes called, down the sub-runs they spawned (`FLOW_CALL_MAX_DEPTH`): their content is in its answer and its calls. Chat ratings are read from `message_feedback`, where the chat writes them. An entry keeps the content as it was, unredacted. A case to fix has no expected answer: its judge instructions carry the rating's comment and the rejected answer, each quarantined as data (`wrap_untrusted`). Too-long inputs or answers and oversized recordings are left out, never cut; the result counts what was left out and why. Rated and unrated cases of each source are taken in turn, newest first, so `max_entries` keeps some of each kind.

`concierge_dataset_entry_sources` keeps each entry's source (message, assignment run or flow run, its trace, its rating, its user, its rater, its agent or flow, a flow run's agent nodes and called flows) and its recorded tool calls (level 2's `RecordedCall`).

## Revocation

`withdraw_for_revocation` is registered with level 1's `register_revocation_handler` (its module imported at start-up by `register_revocation_handlers`, in the API and the worker), and runs in the revoking transaction:

| Revocation | Removed |
|---|---|
| `user_opt_out` | Entries whose conversation or run is the user's, or whose rating they gave |
| `agent_opt_in_revoked`, `flow_opt_in_revoked` | Entries built from that agent or flow, and flow-run entries whose graph ran that agent or called that flow |
| `scope_narrowed` | Entries whose agent or flow, or one of whose flow run's agent nodes or called flows, left the selected scope |

The entries go with their dataset-version copies and the results scored on them; a run that lost results gets `headline_details["withdrawn_entries"]` and its headlines read "not measured", never a complete verdict.

## Tools

Group `concierge_evaluation` (gate `concierge`), also on `/mcp/xtm`: `build_dataset_from_traces`, `run_health_evaluation`, `get_health_evaluation`, `compare_health_runs` (former names `run_concierge_evaluation`, `get_concierge_evaluation`, `compare_concierge_runs` kept as aliases, `platform-health.md` section 2.3). One rule on every surface: the `CONCIERGE` flag, then the Concierge, or an administrator while the evaluation surface is on. A launch takes a Concierge-built dataset and subjects that are sandbox copies only (an evaluation runs its subject's tools: a production agent or flow would write for real, B2), whose source is in the supervision scope and not a fleet agent; it never reuses an earlier answer (`reuse_default` off). Refusals are generic; reasons go to the logs. Budget: a launch in a Concierge run reserves on the run's cycle (its entries at `ENTRY_TOKENS_ESTIMATE`, or the experiment's sizing), held past the cycle's close; the tick charges what the runs measured once they end and stops them past a cap (`settle_concierge_evaluations`). A launch outside a cycle (an administrator over MCP) passes the budget gate and is not booked. A budget refusal is stated (`Not launched: ...`).

## Read paths

Every human read of an evaluation names a flow subject as such: the detail lists `flows` beside `agents` (`agent_ids` holds agents only), a run serves `subject_kind`, `flow_id` and the flow's name in `agent_name`, the chart and the run search key series by subject, the admin agent tools read flow runs under the same tier rule (`user_visible_filter` on the flow). A copy keeps the flow subjects and never the Concierge's mark; an export (a package of agents) and a placebo launch refuse a flow subject with a 422. The frontend links a flow subject to its builder (`lib/evaluation-subject.ts`). Shared helpers: `services/evaluation/subjects.py`.
