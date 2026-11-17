# Autonomous attack path - the XTM One orchestration brain

Internal reference for anyone touching the autonomous (AI-driven) attack-path
orchestrator: the run-coordination tools in
[`backend/app/integrations/providers/openaev/autonomous.py`](../backend/app/integrations/providers/openaev/autonomous.py),
the decision cycle in
[`backend/app/services/autonomous_pentest.py`](../backend/app/services/autonomous_pentest.py),
the durable parking adapter in
[`backend/app/services/orchestration_adapters/autonomous.py`](../backend/app/services/orchestration_adapters/autonomous.py),
and the orchestrator persona
[`backend/app/agents/seed_data/agent_persona/openaev-attack-path-orchestrator.md`](../backend/app/agents/seed_data/agent_persona/openaev-attack-path-orchestrator.md).

## The split: brain here, substrate in OpenAEV

The autonomous attack-path feature is a real AI penetration tester. The **brain**
lives in XTM One; **OpenAEV is the execution and visualization substrate**.

- **OpenAEV** owns the run record, executes injects through a chained Simulation,
  enforces scope + tenant isolation, and renders the live UI (attack-path graph +
  decision timeline). Its `AutonomousRunApi` (`/api/autonomous-runs`) is
  Enterprise-Edition gated and behind the `INJECT_CHAINING` preview flag
  (autonomy is a launch mode of chained scenarios, so it shares the chaining flag).
- **XTM One** hosts the `openaev-attack-path-orchestrator` agent, plans the
  attack, decides the next action, dispatches techniques through the normal
  OpenAEV integration tools, and calls back into OpenAEV to narrate the timeline,
  transition status, consume operator steering, and resolve capability gaps.

The operator drives everything from the OpenAEV UI. The XTM One chat is **not**
in the loop - the orchestrator only surfaces to the operator through OpenAEV
timeline `QUESTION` events when it is genuinely stuck.

## Autonomy is a launch mode, not a scenario type

"Autonomous" is **no longer a kind of scenario** in OpenAEV. A scenario is only
ever `CHAINED` (it has a workflow template of linked steps) or `TIME_BASED` (no
workflow). What used to be an "autonomous scenario" is now a **chained scenario
launched in autonomous mode**. This decouples authoring the attack path from
running it, and lets the same scenario be replayed as many times as needed.

There are three distinct ways the orchestrator interacts with a scenario, keyed
off two fields on the run payload - `scenario_id` and `simulation_id`:

| Interaction | `scenario_id` | `simulation_id` | `author_scenario` | What the orchestrator does |
|---|---|---|---|---|
| **Author scenario** (AI planning) | set | **null** | `true` | Designs the FULL reusable chained path and writes steps directly onto the scenario's workflow template. Nothing executes; ends `PLANNED`. |
| **Launch autonomous (from scenario)** | set | set | `false` | Seeds itself with the scenario's already-authored steps as the starting attack path, then adapts/extends live from findings against the simulation seeded from the scenario at launch. |
| **Launch autonomous (ad-hoc)** | null | set | `false` | Today's behavior - drives a fresh live simulation with no pre-authored steps. |

The discriminator for author-scenario mode is the **pairing** of
`author_scenario=true` with a **missing** `simulation_id`; a launched-from-scenario
LIVE run also carries a `scenario_id` but has a simulation and therefore executes.
`_build_cycle_prompt` derives this as
`author_scenario = bool(payload["author_scenario"]) and not simulation_id`, so a
stray flag can never turn a live run into a non-executing authoring pass.

Author-scenario differs from the older **BUILD LOGIC (non-executing)** mode: that
mode authored a throwaway plan against a disposable simulation, whereas
author-scenario writes onto the scenario's persistent workflow template - the
artifact survives and is launchable later in normal or autonomous mode. The
throwaway-simulation build mode is retained for backward compatibility.

### Plan-mode engagement deadline (2h safety net)

A build pass (any `plan_mode` run) only DESIGNS the path - it executes nothing and
normally converges in minutes - so it never needs the multi-day dead-letter window
a live run gets. `start_autonomous_run` caps a plan-mode run's absolute
`engagement_deadline` at `PLAN_MODE_ENGAGEMENT_DEADLINE_SECONDS` (**2h**, vs the
live-run `DEFAULT_ENGAGEMENT_DEADLINE_SECONDS` of 7 days). This is purely a
stuck-loop guard (not the happy path: a good plan finishes in well under 15
minutes): OpenAEV surfaces no timeout for a build, and the cap just
guarantees a build that never signals `completed` cannot park for days. It is
independent of the OpenAEV-side run timeout (a live autonomous run's own
operator-facing budget defaults to 24h).

The cap is the run's absolute `engagement_deadline`, so it bounds the run even
while it is parked. A `waiting_input` park has no deadline of its OWN (see below)
and is never dead-lettered on that basis, but the absolute engagement deadline
still applies underneath - so a plan-mode build stuck awaiting an operator is
dead-lettered at the 2h cap rather than lingering for days, exactly as a stuck
build loop would be.

## Why "one decision cycle at a time"

A pentest step resolves in seconds (a port scan) or in **hours/days** (a phishing
email that only yields credentials after a human clicks). An LLM agentic loop must
never sit open that long: it would pin a worker, burn tokens, and die on restart.

So a run is decomposed into bounded **decision cycles**. Each call to
`run_autonomous_cycle` runs the orchestrator for a wall-clock-bounded burst
(`CYCLE_WALLCLOCK_SECONDS` = 9 minutes on a LIVE run, kept below the reconciler's
`worker_job_timeout` 600s fallback; `PLAN_MODE_CYCLE_WALLCLOCK_SECONDS` = 25
minutes on a plan-mode build, so one cycle can consult the Payload Creator and
author the full path without a 9-minute context reset). Plan-mode executions set
`deadline_at` from `engagement_deadline_seconds`, so the reconciler timeout is
the remaining engagement deadline, not the 600s fallback:

1. consume any pending operator directives,
2. read current state + freshly-arrived findings,
3. decide and dispatch the next technique(s),
4. record timeline events for the live UI,
5. write a **decision signal** to `app.services.shared_state` describing what
   happens next: `continue` (more work now), `await_finding` (a slow step was
   dispatched - park with a `wait_seconds` deadline), `waiting_input` (stuck,
   needs the operator), `completed`, or `failed`.

> **Decision-signal freshness (must-read).** The signal lives under ONE key per
> run (`decision_state_key(run_id)`), shared across cycles. `run_autonomous_cycle`
> therefore **clears that key before every cycle**: a cycle that ends without
> re-writing a decision defaults to `continue` (the run keeps moving) rather than
> silently inheriting the PREVIOUS cycle's signal. This is what prevents the class
> of stall where cycle 0 asked a scope question (`waiting_input`), the operator
> answered, cycle 1 did its work but ended on a plain final answer, and the adapter
> re-read the stale `waiting_input` and re-parked. A cycle that genuinely still
> needs to wait MUST re-write `waiting_input` every time.

The durable orchestration engine (`orchestration_adapters/autonomous.py`) reads
that signal and maps it onto an FSM transition:

- **`continue`** (more work right now) does NOT park after every micro-decision.
  The adapter keeps running further cycles **in-process under the same lease** (a
  bounded "burst") so back-to-back progress doesn't pay the park + reschedule +
  reconciler-poll latency between cycles - the single biggest source of perceived
  slowness when several runs cycle at once. The burst yields back to the
  reconciler (a short-delay self-resume) once EITHER bound is hit:
  `AUTONOMOUS_BURST_MAX_CYCLES` (default 8) or `AUTONOMOUS_BURST_BUDGET_SECONDS`
  (default 180s), so leases still rotate fairly across concurrent runs.
- **`await_finding`** parks the run with `await_event` + a `deadline_at` derived
  from `wait_seconds` - **zero tokens burned while waiting**. Re-armed either by
  an OpenAEV "finding arrived" callback (event-driven) or the deadline sweep.
- **`waiting_input`** parks with **no deadline** (a human may take arbitrarily
  long, and a run awaiting input must never be silently dead-lettered). A
  periodic self-resume re-check (`AUTONOMOUS_WAITING_INPUT_RECHECK_SECONDS`,
  default **5 min**) is scheduled as a belt-and-suspenders so an answered
  directive (or an out-of-band OpenAEV stop) is picked up even if the directive
  callback's resume event was lost - the run can never strand. The re-check is a
  cheap OpenAEV status read, so it is kept short: a residual stale park self-heals
  in minutes, not the hour the old 3600s default cost.

The **plan-mode `completed`** path has a backstop: settling a build run to
`PLANNED` is normally the orchestrator's own `update_openaev_run_status(PLANNED)`
call, but if that call is dropped the adapter posts `PLANNED` to OpenAEV itself
(`_settle_openaev_planned`) so the run reaches its settled state instead of
stranding pre-`PLANNED`. OpenAEV treats a redundant `PLANNED` on an already
settled run as an idempotent no-op, so a well-behaved cycle is unaffected. The
orchestrator's `update_openaev_run_status` tool also exposes `PLANNING`, so a
resumed build run can represent "still authoring" without misusing `RUNNING`
(which means live execution).

This mirrors how OpenAEV itself bounds async results: technical expectations
expire after 6h, human expectations (phishing clicks) after 24h, and the chained
workflow has its own timeout - nothing ever waits unbounded. The live-run cycle wall-clock bound is deliberately kept below the reconciler's
`worker_job_timeout` fallback so the inner bound is what fires, never the outer
safety net. Plan-mode uses a longer cycle bound (25 min) because its execution
`deadline_at` is the remaining 2h engagement deadline, not that 600s fallback.

### Parallelism (dedicated concurrency budget)

Autonomous runs are long-lived and cheaply parked, and an operator can launch
many at once. They therefore run on their **own reconciler concurrency
semaphore** (`MAX_CONCURRENT_AUTONOMOUS_RUNS`, default 12), separate from the
shared assignment / sub-agent pool (`MAX_CONCURRENT_RUNS +
MAX_CONCURRENT_SUB_AGENT_TASKS`). A burst of dozens of simultaneous engagements
can't starve normal assignment execution, and vice-versa. Set it to `0` to fall
back to the shared pool. Because each actively-cycling run holds only short-lived
DB sessions (the connection is released during the agentic loop), this budget
adds far less pool pressure than its slot count - but if you push it well past a
dozen, raise `DB_POOL_SIZE` accordingly. This is a generic per-adapter capability
on the orchestration engine (`OrchestrationAdapter.concurrency_limit`), not
autonomous-specific plumbing.

### Cancellation (immediate stop)

When an operator stops / deletes a run, OpenAEV calls
`POST /autonomous/runs/{id}/cancel`, which flips the orchestration execution
terminal (`cancel_execution`). That alone does NOT interrupt a burst already
running under its lease in the worker: without a fast signal, the current cycle
would run to completion (authoring one more decision) and only the NEXT cycle's
cross-side stop check would notice - the "it kept going and authored a decision
after I hit Stop" bug.

So the reconciler runs a **fast cancel watcher** for any adapter that opts in via
`OrchestrationAdapter.cancel_poll_seconds` (the autonomous adapter sets
`AUTONOMOUS_CANCEL_POLL_SECONDS`, default 2s). It polls the execution row and
fires an `asyncio.Event` (`AdapterContext.cancel_event`) the instant the row goes
terminal - even though the cancel was committed from a *different* process. The
autonomous adapter:

- threads `cancel_event` into every cycle's `run_agent_chat`, whose agent loop
  already checks it between iterations and mid-stream, so the in-flight cycle
  aborts within one iteration instead of finishing;
- checks it between burst cycles, so a cancel that lands mid-cycle stops the
  burst immediately rather than authoring the next decision.

The follow-up transition on the (already-`cancelled`) row raises `LeaseLostError`
and is swallowed, so the run simply stays `cancelled` - no self-resume is parked
that could revive it. With the poll disabled (`0`) detection falls back to the
lease heartbeat (`orchestration_lease_seconds / 3`, ~20s) - still bounded, just
not immediate.

### Who may act on a run

Every route that acts on a known run - `start` naming a run id that already has
an execution (a re-engage resets it), `wake`, `cancel` and its `purge` - asks one
rule, `caller_may_manage_autonomous_run` in `services/autonomous_pentest.py`,
against the payload the run was started with:

- an XTM One admin;
- the user recorded as `user_id` (the one who started or last re-engaged it);
- the registered platform that started it, over a cross-platform JWT, whichever
  XTM One user that JWT resolves to - so any operator OpenAEV lets manage the
  run (OpenAEV's own `assertCanManage`: LAUNCH on the run's simulation or
  scenario) can pause, resume, stop, restart, delete or steer it, and OpenAEV's
  read-path reconcile can stop it too. The platform is matched on the
  registration identifier recorded as `started_by_platform` (one registration
  per product, kept when the platform moves to a new URL and re-registers), so
  runs started before a base-URL change stay manageable. A run that recorded no
  identifier is matched on the URL it was started under, normalized:
  `started_by_issuer`, or for a run started before either was recorded its
  `callback_base_url`, the URL OpenAEV signs its JWTs with.

Anyone else gets the answer an unknown run gets (`ignored`, `purged`, a 404 on
`start`) and nothing is written but a `security_alert` audit entry, with a
`Refused <operation> of autonomous run ...` WARNING in the server log - the
platform ignores the answer, so the log is where a misrouted call shows. Executions
are never deleted, so a run id with no execution never ran on this install: only
an admin may purge it, since that purge would only clear the shared
orchestrator's memory. Every one of them decides on the locked row
(`get_execution_by_dedup_key(..., for_update=True)`) and holds it until its
write commits, so a concurrent re-engage either lands first and is what the
decision reads, or waits - and a `wake` event it follows is neutralized by the
reset like the rest of the prior engagement's.

### Where the callbacks go

`callback_base_url` is OpenAEV's identity, the URL it registered with. Every
worker-side callback (status reads and writes, scope, directives, events, the
dead-letter and settle reports) resolves it through
`platform_identity.resolve_platform_call`: the request goes where XTM One
reaches that registration - its `OPENAEV_API_URL` captured at registration -
and the operator JWT it carries stays addressed to the identity as registered.
A URL no registration knows is called and addressed as given, and so is every
URL when the registrations cannot be read. The run-coordination tools below
call the OpenAEV integration's `base_url` with the token addressed by
`platform_token_audience`. See
[Cross-platform token identity](cross-platform-identity.md).

## Run-coordination tools

Built once per OpenAEV integration bind in
`providers/openaev/autonomous.py::register`. They take `run_id` as an explicit
argument (the cycle prompt always carries the OpenAEV run id) rather than
capturing it at build time, so the same tool set is safe to reuse across runs.

| Tool | OpenAEV endpoint | Purpose |
|---|---|---|
| `record_openaev_event` | `POST /api/autonomous-runs/{id}/events` | Append a timeline entry (one of the `_EVENT_TYPES`). |
| `update_openaev_run_status` | `POST /api/autonomous-runs/{id}/status` | Drive the run to `PLANNING` / `RUNNING` / `WAITING_INPUT` / `COMPLETED` / `FAILED` / `PLANNED` (the build-mode states `PLANNING` / `PLANNED` included). Operator-only states (`CREATED`, `PAUSED`, `CANCELED`) are rejected. |
| `consume_openaev_directives` | `POST /api/autonomous-runs/{id}/directives/consume` | Fetch and mark-consumed pending operator steering directives. |
| `resolve_openaev_capabilities` | `POST /api/autonomous-runs/capabilities/resolve` | Resolve requested techniques / output types against the installed arsenal; report satisfying contracts or marketplace connectors to install. |

The event and status vocabularies mirror the OpenAEV enums exactly and must be
kept in lockstep:

- `_EVENT_TYPES` mirrors `io.openaev.database.model.autonomous.AutonomousEventType`
  (`NARRATION`, `DECISION`, `TOOL_ACTION`, `HANDOVER`, `AGENT_DELEGATION`, `GAP`,
  `STATUS`, `DIRECTIVE`, `QUESTION`, `PROOF`).
- `_RUN_STATUSES` is the cycle-drivable subset of
  `io.openaev.database.model.autonomous.AutonomousRunStatus`.

If you add or rename an enum value on the OpenAEV side, update these lists (and
the tool descriptions) in the same change - a drift produces a rejected callback,
not a graceful degrade.

## Scope resolution: target selection is mandatory

Target selection is **mandatory for every objective** - there is no
"environment-wide, so no target needed" shortcut. The orchestrator must never
infer, guess, or sweep the whole environment on its own, not even for an
objective that reads as environment-wide (auto-picking the perimeter is the exact
failure to avoid). "Whole authorized environment" is a valid choice, but it is a
choice the operator makes explicitly, not a default the orchestrator assumes.

`_build_cycle_prompt` renders three cases keyed off whether a scope was already
pre-selected (`has_preselected_scope` - a live allow-list read from OpenAEV, a
mixed snapshot scope list, or the legacy asset-group / team facets):

- **Scope pre-selected** - treat it as the authoritative, FINAL perimeter and
  attack only within it (a single target or the whole environment - either way the
  operator already chose it). Never re-ask for target selection.
- **No scope, cycle 0** - survey the candidates to understand the environment's
  shape, then record ONE synthetic `QUESTION` (a short menu of coarse-grained
  choices plus an explicit "Whole authorized environment" option), call
  `update_openaev_run_status(status=WAITING_INPUT)`, and park. Nothing is created
  or attacked before the operator answers.
- **No scope, later cycle** - honor the operator's answered directive as the
  authoritative scope (consume it, persist it with `set_openaev_run_scope`, then
  plan within it); never re-ask.

See `backend/tests/test_autonomous_pentest_cycle.py`.

## Steering (real-time, no restart)

Operator steering is first-class and never stops the run:

- **Directives** are queued in OpenAEV and consumed by the orchestrator at the
  top of its next cycle via `consume_openaev_directives`; each applied directive
  is echoed back as a `DIRECTIVE` timeline event.
- **Live configuration** edits (scope / rate-limit / safe-mode) are applied to
  the running chained Simulation through the OpenAEV configuration endpoint
  without a restart.
- **Questions** move the run to `WAITING_INPUT`; the operator's answer re-arms the
  cycle.

## Persona contract

The orchestrator persona (`openaev-attack-path-orchestrator.md`) is the behavioral
contract. Two invariants that are easy to break:

- Capability gaps are recorded as **`GAP`** events (not `CAPABILITY_GAP`) - the
  enum name is `GAP`.
- When stuck, the persona must record **one** precise `QUESTION` event, call
  `update_openaev_run_status(status=WAITING_INPUT)`, and signal `waiting_input` -
  in that order - so the run parks cleanly instead of spinning.

Per architecture invariants #15 / #16, behavioral fixes belong in deterministic
code (tool validation, the cycle prompt, enum lockstep) rather than persona
inflation. Keep new persona text net-neutral.

## Tests

- `backend/tests/test_openaev_autonomous_tools.py` - tool registration, argument
  validation (run id, event type, content), operator-only status rejection,
  directive formatting, and the capability report formatter. `_api` is
  monkeypatched to avoid network calls.
- `backend/tests/test_autonomous_pentest_cycle.py` - `decision_state_key`
  stability, wait-seconds clamping/defaulting, cycle-prompt rendering per
  pre-selected-scope state, author-scenario mode (targets the scenario not a simulation, and
  only when `simulation_id` is missing), decision parsing (including the
  SharedState envelope), and `next_run_at`.
