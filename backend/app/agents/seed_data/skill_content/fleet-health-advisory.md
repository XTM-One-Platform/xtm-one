# Fleet health advisory

You orchestrate a fleet. This skill is how you keep it healthy and tell its owner what needs them, without noise.

## When

- On the weekly fleet health check (the automation "Weekly fleet health check" runs only when the fleet has a finding), and whenever a member reports a failure you cannot explain.
- When a person asks how the fleet is doing.

## 1. Read the health

Call `get_fleet_health(fleet_id)`. It returns the member verdicts and one finding per problem, each with:

- `kind`: what is wrong.
- `evidence`: the ids it was read from (agents, assignments, work items, shared-state keys, handovers as `source->target`) and the full `count`.
- `fix`: `auto` (safe for you to apply) or `owner` (a person decides).
- `advice`: the recommended action, one line.

No finding means healthy: stop there. Send nothing, write nothing.

## 2. Classify each finding

| kind | fix | what it means |
|---|---|---|
| `orphaned_claim` | auto | an item still claimed by a run that ended |
| `member_failing` | owner | a member's latest run failed |
| `member_disabled` | owner | a member is disabled |
| `disabled_owner` | owner | a disabled member still owns open items |
| `assignment_failing` | owner | an assignment failed its last 3 judged runs |
| `assignment_silent` | owner | a schedule has not fired for 14 days |
| `past_due` | owner | an open item is past its due date |
| `blocked_without_blocker` | owner | a blocked item is not black or white: no owner, only a free-text reason, needs_human without an ask card, or its blocked_on items all cancelled |
| `wait_cycle` | owner | blocked items wait on each other (blocked_on, or a pull request a review item gates) |
| `blocked_chain_stalled` | owner | a chain of blocked items, all quiet 2 hours, none held by a live run |
| `oversize_state` | owner | a shared-state entry is over the size limit |
| `handover_not_allowed` | owner | a handover points at a member the fleet does not expose |
| `cost_spike` | owner | a member used over 3 times its daily tokens |

A finding you can explain and that needs nothing (a member disabled on purpose, a schedule paused for a known reason you can cite) is **ignore**: leave it out of the advisory and say why in your run summary. When the owner tells you a finding needs nothing and you hold the tool, dismiss it once with `dismiss_fleet_health_findings(fleet_id, ids, reason)`, using the finding's id or its kind and the owner's reason. A dismissed finding no longer counts and is listed under Dismissed.

## 3. Apply the safe fixes only

Call `get_fleet_health(fleet_id, apply_safe_fixes=true)` once. It releases the orphaned claims through the platform's own claim sweep, which never touches a claim a live run holds; the answer lists what it applied.

Never apply an `owner` fix yourself: do not enable or disable agents, pause or delete assignments, edit shared state you do not own, change exposure or cancel items.

## 4. One advisory to the owner

When at least one `owner` finding remains, send the fleet's owner **one** message (the channel your assignment names, else a work item for the owner):

- One line per finding: what is wrong, the recommendation (`advice`), and the evidence ids as links.
- The safe fixes already applied, with their counts.
- Nothing else: no greeting, no progress narrative.

Deduplicate per finding: before sending, read what you sent last time (your own work item or shared-state record of the advisory). A finding with the same kind and the same evidence as last time is not repeated unless the owner has not answered for 7 days. A finding that is gone is not mentioned.

## 5. Record

Keep one record of the advisory (a work item for the owner, or one shared-state entry keyed by the fleet) holding the finding kinds and evidence ids sent, so the next check can deduplicate. Resolve it when the fleet is healthy again.
