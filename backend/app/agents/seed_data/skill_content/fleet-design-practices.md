# Fleet design and operations

You orchestrate a fleet. This skill collects the practices that make a fleet work well in production. Each one is a **suggestion** with its reason: adapt it to the fleet's purpose, and tell the fleet's owner when you think one would help. None of them is a platform rule, and the platform refuses nothing that departs from them.

## 1. Two-way coordination

- A good default: each home member holds a **Consult** handover to you, the orchestrator. A member can then ask you a route, scope or context question instead of guessing. A delegated task may consult the agent that spawned it when that agent is one of its Consult handovers.
- You hold the handover types you need towards each member (delegate for work that runs in the background, consult for a question, transfer when the conversation moves).
- Consider making every member report to you in the fleet layout (`set_fleet_member_parent`): the graph then reads the way the fleet works.

## 2. One entry point

- People talk to you; members are best driven by their assignments, with chat turned off, unless a person needs one of them directly. One entry point keeps routing, context and accountability in one place.

## 3. Reactive first

- Prefer waking a member on an event: its work item inbox (a `work_item` trigger), a `platform_event`, or an `integration_event`. An event costs nothing while nothing happens and answers at once when something does.
- Keep a schedule (cron) for a permanent cycle a person reads at a fixed time, plus at most one safety net with its exit criterion written down.
- Temporary work (a watch, a retest, a follow-up) fits a timer, a one-off scheduled item or a watch better than a new assignment, which outlives its purpose.

## 4. Work through work items

- Consider one correlation key prefix per member, the owner set, a precise ask and the expected deliverable in the description.
- Deduplicate by key; use `not_before` to defer and `due_at` for a real deadline only; attach files with `attachment_file_ids` rather than pasting them.
- Make sure something wakes every item you create (an inbox trigger on its owner, a blocker that releases it). An item nobody is woken for waits forever.
- Read a member's result back before you use it.

## 5. Closed loop

- When an item's completion changes what the fleet runs on (a new capability, a fix, a changed setting), give it a `fleet_impact` aimed at you, and listen on `work_item.completed` to apply it. The change then reaches the fleet without anyone remembering it.

## 6. Self-healing

- Useful listeners on the orchestrator: `run.orphaned` (release what the run held and assess), `run.failed` (name the cause, fix what is in your remit, run it again), and the frictions of your runs (classify each and dispose of it).
- Read `get_fleet_health` weekly with the skill "Fleet health advisory".

## 7. Decisions to people

- When a decision belongs to a person, park the item with `needs_human` and a structured ask: the question, two to four options with their effect, and your recommendation.
- Read "waiting on a person" live from the items; a hand-kept list goes stale.
- The run that closes a loop also resolves the shared state and memory entries about it, in the same run.

## 8. Shared state hygiene

- One stable key per thing, its terminal condition named when you create it, resolved when it is done, lists kept bounded. Stale entries mislead the next run.

## 9. Knowledge and records

- **One shared working knowledge base per fleet**, named after the fleet followed by " - Working knowledge". A good default: you, the orchestrator, hold its write grant and every member is bound to it read-only. One writer means no conflicting rewrites, and every member starts from the same facts. The About page of the fleet suggests the grant and the bindings when they are missing.
- **Members propose, you apply.** Members search the knowledge base at the start of every task and propose an update in their run result (a `KNOWLEDGE:` line); you apply it.
- **Numbered documents, one per subject**, rewritten whole in place, each with a "Last rewritten" date. A good default set: 00 How this knowledge base works; 10 Fleet runbook; 15 Integration routing (which integration and account does what, who is bound to it, read-only or not); 20 Counterparts directory; 30 Members' working notes; 40 Owner's standing preferences; 50 Programs context; 60 to 89 domain documents; 90 Lessons log (dated: what failed, what fixed it).
- **The runbook mirrors the live fleet**: members and their ids, accounts, each member's assignments with their trigger, skills, shared state and memory keys, event triggers, a register of temporary assignments each with its exit criterion, a rules registry (the rule, its date, who set it, where it lives), known gaps and their stand-ins, and a change log of the last 14 days (older lines move to an archive document). Keep it under a size bound, with a detail document for the long form.
- **Where each kind of knowledge lives.** Personas stay generic and stable; procedures live in skills, each rule written once, dated, in the skill of the member that applies it; durable facts live in the knowledge base; short-lived state lives in memory or shared state. A fact in the knowledge base is a lead: the system of record wins.
- **What stays out**: credentials, secrets, identity numbers, personal data, message content and confidential figures.
- **A weekly inventory read** compares the live fleet with the runbook and corrects the runbook (it fits the weekly read of the skill "Fleet health advisory"). A change of the fleet's shape (a member added or removed, an assignment created or deleted, a handover or integration changed) goes into the change log the same day.
- **Atomic writes.** Write a long document in one write from a workspace draft rather than assembling it across several writes that a cut run could leave half done.
- **Housekeeping.** Consistent names (each member named after the fleet in square brackets then its role, skills prefixed by the member that owns them, one work item key prefix per member, shared state keys made of the fleet's short name, a colon and the thing they track). Delete a retired member, assignment or skill once its replacement is verified rather than leaving it disabled. Members share one settings baseline (model, reasoning, memory, human in the loop) unless a reason is recorded.
- **Run summaries.** Every member ends its internal run summary or work item result with what it used and its frictions. These internal lines stay out of what a person reads (a chat answer, a message, an email, a deliverable).

## 10. Smart notifications

- Act by default on safe work; ask only for a real decision, with a recommendation; tell the affected person once, after the fact; send nothing for a non-event. Fewer, better messages keep people reading them.

## 11. Verify

- Read back every write; do not assume an id, a name or a status; remove the test artefacts a run created in that same run.

## 12. Least tools

- Scope each member's built-in tool groups to what it does (`builtin_allowed_tools`). A smaller tool list is cheaper, faster and safer.
