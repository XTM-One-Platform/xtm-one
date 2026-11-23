# Fleet design and operations

You orchestrate a fleet. This skill is how a fleet works well in production with what the platform already does for you. Two things are platform rules and the platform refuses a write that breaks them: a **blocked** work item names what releases it (section 4), and a decision for a person is a complete **ask card** (section 7). Everything else is a **suggestion** with its reason: adapt it to the fleet's purpose, and tell the fleet's owner when you think one would help.

## Doctrine

- **React on events, not on a poll.** When you wait for something that emits an event (a work item done, a pull request merged, a review given, a run failed, a deployment), subscribe to that event. A schedule that re-checks it costs a run every time nothing happened and answers late when something did.
- **A blocked item names what releases it**, or carries a decision card for a named person. Nothing else wakes it.
- **One job, one agent.** Each member owns one kind of work, driven by its own inbox; a member doing two jobs is two members.
- **Decisions go to their owner**, the one person who decides, with a recommendation.

## 0. Start here: the shortest working path

1. Create the fleet with its orchestrator and members (`create_fleet`); give each home member a **Consult** handover to you and make it report to you (`set_fleet_member_parent`). The fleet's **Graph** view then shows members, handovers and live state; its **About** page lists what is missing.
2. Give each member one inbox: an assignment with trigger type `work_item` (filters below). Members are driven by work items, with chat off unless a person needs one directly.
3. Give yourself three listeners, each an assignment with trigger type `platform_event`: `work_item.completed` filtered on `fleet_impact.targets` (section 5), `run.failed` and `run.orphaned` (section 6). Give the `run.*` listeners a `batch_window_seconds` (60 is a good start) so a burst of failures is one run reading the list, not one run per event.
4. Turn on the built-in "Weekly fleet health check" (`set_fleet_automations`): it runs only when `get_fleet_health` reads the fleet unhealthy, so a healthy week costs no model call.
5. Hand work over with `create_work_item`: owner set, a correlation key prefix per member, the ask and the deliverable in the description.

A worked minimal example: a triage member and a reporting member. The orchestrator files `create_work_item(owner_agent_id=TRIAGE, correlation_key="triage:case-42", title=..., description=...)`; the triage member's `work_item` inbox (`correlation_prefix` "triage:") picks it up, works it and completes it with `fleet_impact` aimed at the orchestrator when the result changes what the reporting member does; the orchestrator's `work_item.completed` listener files the reporting item. No schedule, no polling, no hand-kept list.

## 1. Two-way coordination

- Each home member holds a Consult handover to you: a member asks a route, scope or context question (`consult_agent`) instead of guessing. You hold the handover types you need towards each member: delegate (`spawn_background_task`) for background work, consult for a question, transfer when the conversation moves.
- `delegate_to_assignment` starts a tracked run of one assignment with its own status and retries, when the work has a named procedure.

## 2. One entry point

- People talk to you; members are driven by their assignments. One entry point keeps routing, context and accountability in one place.

## 3. Reactive first: which wake-up for which wait

| You wait for | Use | Not |
|---|---|---|
| Work for a member | its `work_item` inbox | a schedule that lists items |
| A platform fact (an item done, released or needing a person, a run failed or orphaned, a release deployed, a timer due) | a `platform_event` assignment: `event` (one name or a list), `filter` on payload paths, `scope` own, fleet or all, `once` for a single event, `expires_at` for a deadline; `list_platform_events` names every event and its payload keys | a schedule that re-reads it |
| An event of a connected tool (a pull request review, a check suite, a workflow run, a merge queue entry, a new email or message) | an `integration_event` assignment with the event's `filter`; `list_integration_catalog` names each provider's events and filter keys | a scheduled poll of the tool |
| One event, once, for a background task | `watch_platform_event` (with `expires_at`, at most 7 days) | a temporary assignment |
| A date for one object (a follow-up in three days, a reminder at T-60) | `arm_timer` with a `correlation_key` and, when the wait can end early, `unless_work_item_id` or `unless_shared_state_key`: it fires `timer.due` only if still needed; `cancel_timer`, `list_timers` | a cron that scans every object |
| A one-off action at a time | `schedule_work_item` (mode exact or instruction); `list_scheduled_work_items`, `reschedule_scheduled_work_item`, `cancel_scheduled_work_item` | a new assignment that outlives its purpose |
| A permanent cycle a person reads at a fixed time (a Monday digest) | a `schedule` trigger, gated with `activation_check` so an empty week costs no model call | anything else |

- A schedule is not a safety net for something that emits an event: if the event can be missed, fix the subscription.
- `activation_check` runs a read-only tool before any model call and skips the run at zero tokens when its condition fails: gate every schedule and every broad listener with it.

## 4. Work through work items

- **Statuses**: `open`, `claimed`, `in_progress`, `blocked`, `done`, `cancelled`. An inbox claims an open item for its owner; the run completes it with `complete_work_item` and a result. Leave an item you created as it is, no write back to open: the platform already dispatched it.
- **Inbox filters** on a `work_item` trigger: `correlation_prefix` (one prefix per member), `priority_min`, `max_concurrent_runs` (up to 10 items in parallel, urgent first).
- **Blocked is black or white.** A blocked item has an owner and one of these, else the write is refused:
  - `blocked_on`: the work items it waits for; it reopens when all are done (`blocked_on_release_on`: `done`, `passed` for a check that passed, `deployed` for the build carrying its pull request);
  - `blocked_on_refs`: a `pull_request` or `issue` (`release_on` `merged`, `closed` or `merged_or_closed`), a `deployment` (`deployed`: the build carries the merge), a `workflow_run` (`succeeded`: a later green run);
  - `needs_human` with a complete `ask` card for a named person (section 7).
  `blocked_reason` is context, not a blocker. To defer, use `not_before`, not blocked.
- **What the platform does for you, so you do not re-check by hand**: it releases an item when its blockers are met (`work_item.released`); it surfaces an item whose pull request closed unmerged, back to open with the reason; it breaks a wait cycle (`work_item.wait_cycle`); after repeated inbox runs that ended without completing an item it parks it on a decision card (`work_item.release_exhausted`); it raises `work_item.needs_human` when an item starts waiting for a person; it releases the claims of a run that ended (`run.orphaned`).
- Deduplicate by key (`dedupe`, `dedupe_by`); `due_at` for a real deadline only; attach files with `attachment_file_ids` rather than pasting them; a progress note goes in `note`, not in the description.
- **Programs**: a root item keyed `program:NAME` with its children; `list_programs` and `get_program_status` read them live.
- Read a member's result back before you use it.

## 5. Closed loop

- When an item's completion changes what the fleet runs on (a new capability, a fix, a changed setting), give it a `fleet_impact` (`targets`, `kind`, `instructions`) aimed at you, and listen on `work_item.completed` with the filter `fleet_impact.targets` containing your agent id. The change reaches the fleet without anyone remembering it.
- A change that has to be live before the follow-through waits on a `deployment` reference (`blocked_on_refs`, `release_on` `deployed`) or on `platform.release.deployed`, not on a schedule.

## 6. Self-healing

- Listeners on the orchestrator: `run.orphaned` (assess what the run left), `run.failed` (name the cause, fix what is in your remit, run it again), `run.friction_recorded` and `run.completed_with_undisposed_frictions` (classify each friction and dispose of it with `dispose_friction`; `record_friction` and `list_frictions` for your own runs), `work_item.release_exhausted` and `work_item.wait_cycle` (an item the platform could not release).
- Fleet health is the built-in weekly check (section 0) with the skill "Fleet health advisory", not a schedule of your own.

## 7. Decisions to people

- When a decision belongs to a person, block the item with `needs_human` and an `ask`: `question` (one plain sentence), `context`, two to four `options` each with `key`, `label` and `effect`, `recommendation` (the key of one option), and `deadline` and `links` when they exist. No ids, timestamps or message ids in the readable fields: the platform refuses them. `recipient` names the person; by default the acting user, then the agent's owner, then administrators.
- The person answers from the decision card (the item dialog, the bell, the channel card); the answer is stamped on the item, `needs_human` is cleared and the item reopens to its owner. A separate message asking the same thing, or a re-ask on a timer, only adds noise: the open card stays in the bell until answered.
- Read "waiting on a person" live from the items; a hand-kept list goes stale.
- The run that closes a loop also resolves the shared state and memory entries about it, in the same run.

## 8. Shared state

- One stable key per thing; `terminal_condition` written when you create it, `expires_at` when it ends on a date; `resolve_shared_state` when it is done, `delete_shared_state` when it is useless.
- **Lists change on the server**: `append_json`, `remove_json`, `update_json` (match, set, increment, append), `unique_by_json` so a repeated append replaces instead of duplicating, `keep_last_json` with an `archive_key` that receives what is trimmed, `retention_json` for a standing policy. Reading a list and rewriting it whole loses the writes of a concurrent run.
- **Leases**: `hold` take on a key before a write two runs could race on (a branch, a document); a live holder answers held; release it after; the platform frees the hold of a run that ended.
- Read with `get_shared_state`: `path` for one key, `where_json` for the matching list items.

## 9. Worksets: material larger than one context

- When a task spans more material than a run can hold (two weeks of reports, a long export), put it in the workset: `workset_add_item`, or any tool's `to_workset` argument that stores its whole result as items; `workset_declare_total` when you know the count; `workset_seal` when all is in.
- Work through it with `workset_next_pending`, `workset_set_summary` and `workset_set_priority`; find passages with `workset_search` and `workset_read_item`; write the synthesis from `workset_digest`, citing item ids; `workset_resolve` when done.

## 10. Knowledge and records

- **One shared working knowledge base per fleet**, named after the fleet followed by " - Working knowledge". You, the orchestrator, hold its write grant and every member is bound to it read-only: one writer, no conflicting rewrites. The About page suggests the grant and the bindings when they are missing.
- **Members propose, you apply.** Members search the knowledge base at the start of a task and propose an update in their run result (a `KNOWLEDGE:` line); you apply it.
- **Numbered documents, one per subject**, rewritten whole in place, each with a "Last rewritten" date. A good default set: 00 How this knowledge base works; 10 Fleet runbook; 15 Integration routing; 20 Counterparts directory; 30 Members' working notes; 40 Owner's standing preferences; 50 Programs context; 60 to 89 domain documents; 90 Lessons log.
- **The runbook mirrors the live fleet**: members and their ids, each member's assignments with their trigger, skills, shared state keys, event triggers, timers in use, a rules registry (the rule, its date, who set it, where it lives), known gaps and a change log of the last 14 days. Keep it under a size bound.
- **Where each kind of knowledge lives.** Personas stay generic and stable; procedures live in skills, each rule written once; durable facts live in the knowledge base; short-lived state lives in memory or shared state. A fact in the knowledge base is a lead: the system of record wins.
- **What stays out**: credentials, secrets, identity numbers, personal data, message content and confidential figures.
- **Atomic writes.** Write a long document in one write from a workspace draft rather than across several writes a cut run could leave half done.
- **Housekeeping.** Consistent names (each member named after the fleet in square brackets then its role, one work item key prefix per member, shared state keys made of the fleet's short name, a colon and the thing they track). Delete a retired member, assignment or skill once its replacement is verified. A built-in skill is read-only and follows the platform's updates; to adapt one, fork it: the copy is yours and the platform's updates leave it as it is.
- **Run summaries.** Every member ends its internal run summary or work item result with what it used and its frictions. These internal lines stay out of what a person reads.

## 11. Smart notifications

- Act by default on safe work; ask only for a real decision, on a card, with a recommendation; tell the affected person once, after the fact; send nothing for a non-event or a progress echo.

## 12. Verify

- Read back every write; do not assume an id, a name or a status; remove the test artefacts a run created in that same run.

## 13. Least tools

- Scope each member's built-in tool groups to what it does (`builtin_allowed_tools`). A smaller tool list is cheaper, faster and safer.
