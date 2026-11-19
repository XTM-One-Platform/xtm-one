# Platform Health

Platform Health (formerly the Concierge) is a company-managed fleet of agents that looks after the platform's other agents and flows. It consolidates memories, diagnoses agents and flows from their traces, ratings and run history, tries changes on sandbox copies, measures them with evaluations, and files **improvement proposals** that the people who manage the target approve. Nothing it proposes reaches production without that approval, and an applied change that regresses is rolled back.

!!! note "Built in"
    Platform Health is built in: no feature flag is needed. An administrator turns it on in **Settings > Automations > Platform Health**. The former `/concierge` page and `/api/v1/concierge` routes still answer for one minor release: the page redirects to `/health`, the API routes are deprecated aliases of `/api/v1/health` and carry a `Deprecation` header.

## The fleet

The platform ships a built-in fleet named **Platform Health**, in a company-managed workspace of the same name, with five agents:

| Agent | Role |
|---|---|
| **Platform Health** | The orchestrator. Investigates a target after a signal or a request by handing the work to the specialists, writes the weekly digest, and answers administrators in chat. |
| **Memory Steward** | Consolidates the memory of agents whose managers opted in. |
| **Quality Analyst** | Diagnoses agents and drafts persona, skill and assignment-prompt variants. |
| **Flow Lab** | Diagnoses flows and drafts repairs, optimizations and new flows on sandbox copies. |
| **Experimenter** | Measures variants with evaluations and files the proposals. |

The fleet, its agents, their assignments and its workspace are **built in and read-only for everyone**, administrators included: nobody edits, switches off, deletes, duplicates or forks them, and every attempt answers "Platform Health is built in and read-only." The platform rewrites their definition at every start. What an administrator tunes is set in Settings > Automations > Platform Health (below). None of the agents can edit an agent or a flow directly. The fleet never supervises itself.

Every piece of Platform Health work is a run of one of the fleet's assignments, and every run starts inside a **cycle**: a unit of work with its own share of the Platform Health budget. Each cycle ends with a digest.

## Settings > Automations > Platform Health

| Section | What it sets |
|---|---|
| Enable, name, scope, budget | Whether Platform Health works, the name the entry points and refusals use, which agents and flows it supervises among those their managers enrolled (all of them, or a list), and its budget per cycle and per month, in tokens and in dollars. |
| Schedule | When each fleet-wide cycle starts, as a cron expression in the time zone chosen: memory consolidation (default every night at 02:00), quality review (Mondays 03:00), flow review (Mondays 04:00) and the weekly digest (Mondays 07:00). Each can be switched off. A schedule never fires at start-up: it first fires at its next occurrence. |
| Signals | The thresholds below, each with its own switch. |
| Durations | How long an applied proposal is watched for a regression (7 days), how long a flow that replaces an assignment runs in shadow first (3 days), how long it is watched after the switch (7 days), and how many shadow runs it needs (5). |
| Dry-run | Every write of every Platform Health agent is logged instead of executed: memory changes, proposals, sandbox copies. Digests count what was logged. |

### Name

The **Name** field sets the name the entry points ("Improve with …") and the refusals use. The page, the navigation entry, the Settings section, the orchestrator agent, its fleet and its workspace always read **Platform Health**: the fleet is built in, so the platform writes that name on them at every start, over a former Concierge name too. Left empty, the name is **Platform Health**. It is 1 to 40 characters, without line breaks or control characters; leading and trailing spaces are trimmed.

Saving a new name renames no agent, fleet or workspace, and the agents' personas are not changed: the name is what people read, not an instruction to the model.

A schedule that is due while the budget is reached, or while agentic AI is off, starts nothing: its last firing and the reason are shown under the schedule, and it tries again at its next occurrence.

### Companion pets

The **Companion pets** block lists the pets users may pick for their companion (see [Platform Health](../usage/health.md#the-companion)). **Butler** is built in and cannot be removed. **Add a pet** takes a pet in the Codex pet format, as one `.zip` holding `pet.json` and its spritesheet, or as the two files:

- `pet.json` with `id` (lowercase letters, digits and dashes), `displayName`, an optional `description`, `spritesheetPath` (a `.webp` or `.png` path inside the package) and, for a version 2 pet, `"spriteVersionNumber": 2`;
- a spritesheet of 8 columns of 192x208 cells: 1536x1872 for version 1 (9 rows: idle, running right, running left, waving, jumping, failed, waiting, running, review), 1536x2288 for version 2 (two more rows, the look directions).

A package of more than 8 MB, an SVG or any image that is not a single-frame PNG or WebP of the exact size, a path that leaves the package, or a field out of bounds is refused with the rule it broke. The platform keeps its own re-encoding of the spritesheet, never the uploaded file. Removing a pet shows the default pet to the users who had picked it.

## Signals

Signals are read by the scheduler **without any model call**, only for the agents and flows of the supervision scope, from figures the platform already keeps (no conversation content):

| Signal | Default threshold | Read |
|---|---|---|
| Feedback drop | An agent's weighted feedback score is 10 points lower over the last 3 days than over the 3 days before, with at least 10 ratings in the last 3 days | Every 15 minutes |
| Failure spike | More than 5 % of the last 20 finished runs of an assignment or a flow failed | Every 15 minutes, for the assignments and flows that ran since |
| Cost drift | An agent's cost per assignment run is 20 % higher over the last 7 days than over the 7 days before, with at least 10 runs in each period | Hourly, from the daily usage figures |
| Model change | The model a supervised agent resolves to changes | Hourly |

A signal starts **one investigation** on its target when it crosses its threshold. It does not start another while it stays crossed; it is ready again once the figure is back under the threshold. While an investigation, or any other cycle, is open on a target, nothing starts a second one there: the request or the signal is recorded and refused.

## Digest

Each cycle ends with a digest built from what the cycle actually did:

- the memory consolidations (counts per operation, never the content);
- the findings, new and already known;
- the experiments, by outcome;
- the proposals filed, applied and rolled back, and how many are unmeasured;
- the budget spent;
- everything skipped or not completed, with the reason (budget reached, a run that failed, experiments skipped).

A cycle reads **Completed** only when its run completed with nothing skipped. Otherwise it reads **Partly completed**, **Budget reached**, **Failed** or **Cancelled**. A figure that could not be read is shown as *Unavailable*, never as 0. The orchestrator's own report, written in the platform language, is shown under the figures.

The weekly digest assignment summarizes the cycles closed since the previous one; its result appears in the dashboard's latest results.

## The Platform Health page

Administrators find every proposal, cycle and signal on the **Platform Health** page (`/health`):

- **Improvements**: every proposal, filtered by type and status, with its measured result, the number of writes simulated during its experiment, and its evidence. Evidence the viewer may not open is counted, not linked.
- **Cycles**: every cycle and its digest.
- **Signals**: the signals currently crossed, and whether each started an investigation.

Managers see the proposals and cycles of the agents and flows they manage on those pages (see [Platform Health](../usage/health.md)).

## Chat with the orchestrator

The Platform Health orchestrator answers in chat. With an administrator it reads every cycle and signal of its scope; with anyone else, only those of the agents and flows that person manages, and it may start an investigation on one of them on their behalf.
