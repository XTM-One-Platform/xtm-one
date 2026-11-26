# Fleet health advisory

You orchestrate a fleet. This skill is how you keep it healthy and tell its owner what needs them, without noise.

## When

- On the built-in weekly fleet health check: turn it on with `set_fleet_automations` rather than a schedule of your own; it is gated on `get_fleet_health` and runs only when the fleet has a finding, so a healthy week costs no model call.
- On the events that already name a problem, through your `platform_event` listeners: `run.failed`, `run.orphaned`, `work_item.release_exhausted`, `work_item.wait_cycle`, and `work_item.past_due` (an open item crossed its due date, raised once per due date; the seeded "Overdue work items" listener takes it with the health check). React to the event; do not wait for the weekly check, and never add a schedule that re-reads the health.
- When a member reports a failure you cannot explain, or a person asks how the fleet is doing.

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
| `not_picked_up` | owner | an open item no inbox will take: no owner, an owner with no work-item inbox, or inboxes whose correlation prefix or priority leave it out; the fix is an owner whose inbox takes it, a wider inbox, or cancelling it |
| `blocked_without_blocker` | owner | a blocked item is not black or white: no owner, only a free-text reason, needs_human without an ask card, or its blocked_on items all cancelled; the fix is a valid blocker (`blocked_on`, `blocked_on_refs`, or `needs_human` with an ask; skill "Fleet design and operations" section 4), or `not_before` for a deferral |
| `decision_card_not_readable` | owner | an item waits for a person on an ask that is missing, incomplete or full of ids |
| `decision_unseen` | owner | a decision gate nobody opened in 24 hours (`work_item_decision_unseen_hours`); the administrators were notified once and `work_item.needs_human_unseen` was raised. Remind the recipient, or name an active one with `recipient`; opening the item clears it |
| `decision_recipient_inactive` | owner | a decision waits for a recipient that never signed in, not in 7 days (`work_item_recipient_inactive_days`), the seed account, or a group none of whose members signed in. Recommend naming an active person or group with `recipient` |
| `wait_cycle` | owner | blocked items wait on each other (blocked_on, or a pull request a review item gates) |
| `blocked_chain_stalled` | owner | a chain of blocked items, all quiet 2 hours, none held by a live run, not waiting on a person's open decision. A chain that ends on a readable, unanswered decision before its due date is no finding: the report lists it under `waiting_on_people` with its recipient and age; never advise cancelling it |
| `oversize_state` | owner | a shared-state entry is over the size limit |
| `handover_not_allowed` | owner | a handover from an agent outside the fleet points at a member the fleet does not expose; the summary names each source with its home fleet. Recommend re-pointing the handover at the orchestrator; exposing the target opens it to every other fleet |
| `cost_spike` | owner | a member used over 3 times its daily tokens |

A finding you can explain and that needs nothing (a member disabled on purpose, a schedule paused for a known reason you can cite) is **ignore**: leave it out of the advisory and say why in your run summary. When the owner tells you a finding needs nothing and you hold the tool, dismiss it once with `dismiss_fleet_health_findings(fleet_id, ids, reason)`, using the finding's id or its kind and the owner's reason. A dismissed finding no longer counts and is listed under Dismissed.

## 3. Apply the safe fixes only

Call `get_fleet_health(fleet_id, apply_safe_fixes=true)` once. It releases the orphaned claims through the platform's own claim sweep, which never touches a claim a live run holds; the answer lists what it applied.

Never apply an `owner` fix yourself: do not enable or disable agents, pause or delete assignments, edit shared state you do not own, change exposure or cancel items.

## 4. One advisory to the owner

When at least one `owner` finding remains, give the fleet's owner **one** advisory:

- One line per finding: what is wrong, the recommendation (`advice`), and the evidence ids as links.
- The safe fixes already applied, with their counts.
- Nothing else: no greeting, no progress narrative.

When a finding needs the owner's choice (fix, ignore, change the fleet), put it on one work item for the owner, keyed `fleet-health:` followed by the fleet id (a suffix after it when there are several), blocked with `needs_human` and an `ask` card: the question, the options with their effect, your recommendation, the owner as `recipient`, the finding ids in its description. The owner answers from the card; the answer reopens the item to your assignment "Fleet health answered", which dismisses an ignored finding once with the owner's reason and completes the item. Otherwise send the advisory on the channel your assignment names.

Deduplicate per finding: before sending, read your record of the last advisory (section 5). A finding with the same kind and the same evidence as last time is not repeated: while its card is open it stays in the owner's bell, so never re-send it on a timer. A finding that is gone is not mentioned.

## 5. Record

Keep one record of the advisory (the owner's work item, or one shared-state entry keyed by the fleet, its `terminal_condition` "fleet healthy" written at creation) holding the finding kinds and evidence ids sent, so the next check can deduplicate. Resolve it (`resolve_shared_state`, or complete the item) when the fleet is healthy again.
