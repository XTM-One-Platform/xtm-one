# Improvement proposals (Platform Concierge, level 3)

Issue #8256, epic #1783. PRD: `dev-docs/platform-concierge.md` section 6.10, decisions B6, B11, B18.

A typed, evidenced change to a production target, approved by the people who manage it, watched, and rolled back on regression. The #1044 persona self-improvement loop is the `persona` type; the Concierge's Experimenter files the others.

## Model

`improvement_proposals` (`models/improvement_proposal.py`, migrations `c3cnc8254031` and `c3cnc8254032`).

- Type and target: `persona` (agent), `skill` (skill), `assignment_prompt` (assignment), `flow_repair`, `flow_optimization` (flow), `new_flow` (the agent whose usage it automates), `agent_to_flow` (assignment). `TARGET_OF` fixes the target type of each.
- `agent_id` is the only key (`ON DELETE CASCADE`): an agent deleted mid-loop takes its proposals with it. A proposal whose target is gone otherwise (a skill, a flow, an assignment whose agent remains) ends `failed` at its next settlement and answers 404 at a decision.
- The change: `base_content` / `proposed_content` (text, the base kept verbatim for the rollback), or a structured `change`; `apply_state` for what applying did.
- Evidence and measurement: `finding`, `evidence_refs`, `experiment_refs` (`{"kind", "id"}` only, bounded), `experiment`, and `experiment_verdict`, which an agent must state: `progression`, `neutral`, `regression` or `unmeasured`. A person's #1044 request has none; its re-evaluation measures it.
- The watch: `evaluation_id`, `baseline_run_id`, `candidate_run_id`, both scores, `delta`, `verdict`, `watch_until`, `outcome_note`.
- Dedup: `sha256(type | target type | target id | evidence fingerprint)`, the fingerprint being the finding's own when given, else the sorted `kind:id` lines of the evidence. One open proposal per key, and an agent does not refile a key a person rejected. One open change per target (`new_flow` exempt).

`c3cnc8254032` copies the `persona_improvements` rows in under their ids (`evaluating` becomes `watching`) and leaves that table for the previous release's replicas; a later release drops it.

## Lifecycle

`pending_approval` → `rejected` | `applying` → `shadow` (agent-to-flow only) → `watching` → `kept` | `rolled_back` | `regression_kept` | `failed`.

`services/improvement_proposals/`:

- `file_proposal(db, *, spec, author_agent=None, requested_by=None)`: one author; the type's handler validates the change (targeted: `text_change.check_text_change`, invariant 16).
- `approve` / `reject`: by whoever may edit the target by hand, no administrator bypass on a private target (`approval_error`, B11), on the locked rows. Approving refuses a stale base (409) and writes the change through the handler, audited.
- `settle_open_proposals` (scheduler step 9b, whatever the flags): the re-evaluation against the 2-point margin (`evaluation_comparison`), live watchers (`register_live_watcher`), the watch window (`watch_days(db)`: Settings > Concierge `proposal_watch_days`, 7 days by default, B18; a person's request has none). A window nothing read (no re-evaluation, no live watcher) ends `failed`, the change still in force, unmeasured: never `kept`. Like for like (`candidate.py`): a baseline that ran a sandbox copy is compared with a candidate on a fresh sandbox copy of the same source made once the change is written, never on the production agent (its writes would happen again for real); when no such copy can be made, or a `concierge_built` evaluation's baseline ran no copy, the change applies unmeasured and its outcome note says why. It picks only a proposal with something to read: a finished re-evaluation, a live watcher, or a window that is over. A regression rolls back as a new version while the change is still in force; a human edit meanwhile is kept (`regression_kept`).
- `set_proposal_status`: for a handler that drives its own shadow and watch (`owns_watch`), with the lifecycle's transitions enforced.
- `register_proposal_type(type, handler)`, listed in `registry._HANDLER_MODULES`.

| Type | Handler | Applies as | Rolls back as |
|---|---|---|---|
| `persona` | `persona.py` | a persona version, and a candidate run of the evaluation when one is named | a persona version of the base |
| `skill` | `skill.py` (never a built-in, nor a URL- or integration-sourced skill) | a skill version, and a candidate run of the evaluation when one is named, on the agent its baseline ran | a skill version of the base |
| `assignment_prompt` | `assignment_prompt.py` | the prompt, audited (assignments have no version table); no evaluation runs a prompt, so its watch ends unmeasured | the base prompt, audited |
| `flow_repair`, `flow_optimization` | `services/concierge/flow_proposals.py` (#8260) | a flow version of the measured graph (`change["graph"]`), watched live (`register_live_watcher`) | a flow version of the base graph (`apply_state`) |
| `new_flow` | same | a new **draft** flow, its manager activates it | the draft archived, unless someone edited it |
| `agent_to_flow` | same, `owns_watch` | a shadow on the assignment's live triggers (`shadow`), then the switch-over: the flow created and activated, the assignment paused (`concierge/flow_shadow.py`) | flow deactivated, assignment resumed, both audited |

## Surfaces and access

One rule on every surface, read on the rows:

- Reading: the target's managers (`can_read`: `can_manage`, administrators included); when the Concierge's own agent calls the tool (`calling_health_principal`: the principal's agent is the calling agent, so a sandbox copy or a consulted agent run inside its work reads as its user), the targets of the supervision scope (`builtin_tools.improvements.target_in_scope`: an assignment through its agent, a skill through an in-scope agent that carries it).
- A person's #1044 request is reachable while `EVALUATION` is on, an agent's proposal while `CONCIERGE` is (`surface_enabled`, `surface_filter`). An agent's pending change nobody can see does not block a person's request: filing it withdraws that change (`failed`, `blocking_proposal_on`).
- REST `/api/v1/improvement-proposals`: list of one target, detail, approve, reject; `POST /persona` (administrators, `EVALUATION`). Served and published only while `EVALUATION` or `CONCIERGE` is on (`any_surface_enabled`).
- Built-in group `improvement_proposals` (gate `CONCIERGE`), also on `/mcp/xtm`: `list_improvements` (a read, on the MCP read list) returns what a proposal carries as untrusted data; `propose_improvement` files only for the Concierge's Experimenter, a property of the run re-read on the agent row (`filing_agent`: a principal of role `experimenter`, calling as its own agent, whose row is a company-managed fleet agent with that role). A listing reads pages of 200 up to 2,000 rows and answers `complete: false` when it stops short. Every other caller, an MCP caller included, gets one generic refusal; the reason is logged. Its target is in scope, never a sandbox copy or a fleet agent. It files `unmeasured` only: a measured verdict comes from `propose_from_experiment`. Both refuse while `CONCIERGE` is off.
