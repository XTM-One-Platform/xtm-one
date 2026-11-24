# Operations

`Operations` is where you review what agents have already done and what they are working on. It is the operational record for runs, waiting items, notifications, and failures.

## What Operations is for

Use `Operations` when you want to:

- check whether a task is still running
- review a completed result
- respond to a request for human input
- isolate notifications or digests
- follow work that moved between agents

## Open Operations

Choose `Work > Operations` from the left sidebar. The `Work` group gathers what your agents produce — `Operations` (their runs), [`Presentations`](presentations.md) (their decks) and [`Files`](files.md) (their outputs and your uploads) — and the `Work` hub page shows the three at a glance.

`Operations` opens on its `Executions` tab, which is the run history described in this chapter. The other tabs — `Background Tasks`, `Work Items`, `Shared State`, `Worksets` — cover the delegations, durable work items, cross-run state and corpora your agents coordinate through. The last one, `Workspaces`, lists the environments that group conversations and keep that coordination apart per topic (see [Workspaces](workspaces.md)). An agent builds a workset when it carries the `Coordination` built-in tools, chosen in the `Built-in Tools` card of its configuration: on an assignment the corpus belongs to that assignment and the person it runs as, and is kept between its runs; in a chat each agent has its own corpus, one per person and per workspace. A company-managed agent's chat corpus is itself company-managed, so whatever you add through that chat is listed in `Worksets` for every user, just like the work items and shared state such an agent produces. When an admin makes an agent company-managed, the worksets it has already built (on its assignments and in its chats) become company-managed with it, and they go back to their owners alone when it is made private again. Its automatic summaries stop at a call budget per corpus, after which the agent has to write the missing ones.

Some agents keep one corpus per conversation instead, such as the built-in `FIMI Analyst`. What such an agent reads in a chat is yours alone, whether or not the agent is company-managed. It is kept apart from your other conversations and deleted 30 days after the agent last used it. An agent with a workset can also send the whole result of a search or a listing into its corpus instead of reading it in the conversation. It then gets back only a receipt: how many items were stored, which were left out and why, and which dates it could not read. It reads the stored items with its workset tools, which keeps long sources cheap to go through.

!!! note

    Agents chatted with before each one got its own corpus wrote into a single shared corpus per person. That older corpus is no longer read by chats; it stays listed under `Worksets` until you delete it.

Every tab has the same visibility control, after the search box: `Mine` | `Company` | `Shared` | `All`, the control of every list page of XTM One. With no segment in the address (`?vis=`), a tab opens on the first segment holding a row: `Mine`, else `Company`, else `Shared`, else `All`. A segment named in the address always wins, an empty `Mine` included.

- `Mine` is what you are responsible for: what you created, and what the agents you own created or own.
- `Shared` is the work of agents shared with you, directly or through a group, that is neither yours nor company-managed.
- `Company` is the work of company-managed agents and runs that is not yours.
- `All` is everything you can see.

A background task always belongs to the person who started it and is never shared, so `Background Tasks` offers `Mine` and `All` only; `All` is every user's tasks for an administrator and your own for anyone else. When `Mine` holds nothing but other segments do, the tab says so and offers `Show all`. Your choice is kept in the page address as `?vis=`, so going back or sharing the link keeps it, and the counts follow it. An older link that carries `?ownership=` or `?scope=` still opens: `?ownership=shared` (or `company_managed`), which used to list shared and company-managed work together, opens on `All` so it hides none of it, and the other values open the segment they named. The address switches to `?vis=` on your next change.

Work items and shared state written by a company-managed agent are company-managed: everyone who can use that agent sees them, and they are read-only for everyone else. Only company-managed agents (whoever they are working for) and administrators can change them, claim them or delete them; a company-managed flow can also claim and settle the company-managed work items its work-item trigger picks up. Your own agents and flows can read them but not change them, and on these tabs only an administrator can resolve, complete or delete a company-managed entry, including one created while you were chatting with a company-managed agent.

A company-managed agent can narrow this for what it produces: its `Coordination access`, in the `Built-in Tools` card of its configuration, says which agents can read and which can change the work items it creates and the shared state it owns - everyone and every company-managed agent (the default), selected company-managed agents, or only itself. An agent that can change them can always read them, and the agent a work item is assigned to, and the agent that claimed it, can always work on it. An agent left out of an entry never sees it, so two groups of agents narrowed to themselves can each use the same key for their own entry. The setting applies at once to what the agent already created. It only concerns agents: these tabs show the same rows as before, and administrators keep full access.

### Reading a work item

Each row of the `Work Items` tab names the agent that owns the item (the one expected to act on it), the agent that created it, the chat, assignment run or flow run it came from, and the person it acts for. An agent that is not shared with you is shown as an agent you cannot open, never by name, and so is a run or a chat you cannot open. Filter the list by owner agent with the picker next to the status filters.

The view switch at the end of the toolbar shows the items as a list, as cards or as a `Graph` of what depends on what, and keeps your choice. The graph draws the items the filters, the scope and the search match: an arrow goes from an item to what it waits for (another item, a pull request, an issue, a deployment, a workflow run, or the person a decision card asks), a dashed line from a program to its items. An item a filtered-out item waits for is drawn with a dashed border so the chain does not stop short. Done and cancelled items are hidden until you show them, and items with no dependency are counted under the toolbar rather than drawn. Select a node to light the chain it waits for and the chain that waits for it; an item only a person can release has a red border. The open button of a node opens the same details as the list.

A badge says what will act on the item:

- `Picked up automatically`: an inbox (an assignment or a flow triggered by work items) drains the owner agent's queue and takes the item within about a minute. `Picked up once due` is the same for an item with a due date still to come.
- `No automatic pickup`: no inbox drains the owner agent's queue, so the item waits until someone asks that agent to process its work items, or completes it by hand.
- `Not picked up: filtered out`: the owner agent has an inbox, but its minimum priority or correlation key prefix leaves this item out.
- `Unassigned: nothing picks it up`: no owner agent is set.
- `Being worked on` and `Blocked: not picked up`: an agent holds the item, or it was blocked, for example after repeated failed dispatches.
- `Waiting for CI on #N`: the item names an open pull request whose CI is still running. When the CI completes, the platform hands the item to its owner agent at once, with the conclusion and a link to the run. An item that also waits for other work items keeps showing `Blocked on N items`: the CI end only adds a note, and the item goes back to the queue when those items are done.

### Pull requests on a work item

A work item can name the pull requests it waits on, in `payload.pull_requests` (`repo` as `owner/name` and `number`). An agent fills this in when it opens a pull request for the item: the GitHub `create_github_pr` tool takes an optional `work_item_id`. Each row links to its pull requests.

When the GitHub integration's signed webhook receives the CI events of a pull request (`workflow_run`, `check_suite`, `pull_request`), the item reacts:

- **CI completed**: once every CI run of the head commit has completed, or one has failed, the item is handed to its owner's work-item inbox at once. The run's trigger context has `reason: pr_ci_completed`, the conclusion and the run link. If a live run already holds the item, that run gets a dated note instead of a second run being started. The same head commit and conclusion dispatch only once: a repeated delivery, or another workflow failing or cancelled on a head already red, is dropped and counted on the pull request's CI record (`dropped`); a re-run of a workflow (a new attempt of the same run) that concludes wakes the item once more, red or green, and a late delivery of an older attempt is ignored. A run is counted as concluded only once it reports both completed and a conclusion. A run that does not test the pull request's own CI (on GitHub, a `pull_request_target` run such as a branch deploy, or a third-party app's check suite, which can finish before the workflows of the head are even requested) or a skipped check can fail a head but never makes it green alone: the head is green once at least one of its own CI runs succeeded and none is left running. When no CI delivery came, the live read also waits for every check the base branch requires to report.
- **New commits**: the CI of the previous head no longer counts.
- **Draft or ready**: the item records whether each pull request is a draft, when it is opened or pushed to and when it is marked ready or turned back into a draft. If an inbox run ends with the item still open on a pull request, the item waits for that pull request's merge or closing. A draft is the exception, because it is waiting for a review before it is marked ready. If its CI is still running, the item is blocked with a reason that names the CI completion, and that completion hands it back. If its CI has already completed, the item goes back in the queue a little later. The run's end reads each open pull request live first (draft or ready, its head, its CI), so a pull request marked ready during the run is seen. When a UX review round is open on the pull request, draft or ready, the item waits for that round's verdict, not for the CI or the merge. When a ready pull request's CI already finished green and no completion is left to hand the item back, it goes back in the queue at once for its next step. An item the run left blocked stays blocked.
- **CI read when no event came**: an item blocked with only a reason (nothing else releases it) on an open pull request is not left waiting for a CI event that never comes, for example when the webhook is unsigned or the head was made by merging the base branch. A few minutes after the item goes quiet, the platform reads the pull request's current head and its checks with the owner's credential. Once every check of that head has completed, or one has failed, the item is handed to its owner's inbox as if the CI event had come, once per head and conclusion, with a note saying it was read.
- **Merged or closed**: the item gets a dated note. If its payload sets `complete_on_merge: true`, it is completed with the merge SHA once every pull request it names is merged. An item blocked on its pull requests alone (a reason only, no blocking item, no open reference, no person to answer) goes back to the queue once none of them is open any more.
- **Items waiting for it**: when the pull request is on a repository whose deployments the platform observes (`WORK_ITEM_DEPLOYMENT_REPOSITORIES`), every item blocked on this one also waits for the pull request's deployment, with a note saying so. It is put back in the queue by the first running build that carries the merge, not by this item's completion at the merge.

This needs the GitHub integration's webhook with a signing secret and the `workflow_run`, `check_suite` and `pull_request` events. An unsigned delivery is ignored.

A note added to an item while an agent run is working on it reaches that run: the run sees it before its next step, marked as a new note on the item it holds, so a "duplicate, stop here" note written by another agent or by you takes effect at once. A note the run writes itself is not sent back to it. When no run is working on the item, the note simply stays on the item for the next run to read.

Open a row for the full picture: why you see the item (yours, company-managed, or filed in a workspace you can see), each inbox with whether it takes the item, the full description, result and payload, its timeline, and when the retention sweep would cancel an item nobody picks up or delete a finished one.

`Mark done` and `Cancel work item` ask for confirmation and let you record what was done, or why it is no longer needed. `Put back in the queue` reopens a claimed, blocked or finished item with its claim and failed dispatch attempts cleared, so an inbox can take it again. An item can wait for other work items (`blocked_on`, set by the agent that creates or updates it): it shows `Blocked on 1 item` (or more) with each item it waits for linked by its title, is never flagged stale, and goes back to the queue by itself once every one of them is done, at the very write that finishes the last one (an agent's update that drops one still open while the item stays blocked is refused, the item named, unless it passes `drop_open_blockers`); if one of them is cancelled, the item goes back to its owner with a `surfaced:` note naming the cancelled item (references it still waits for keep it blocked). When an item it waits for already names a pull request on a repository whose deployments are observed, it waits for the build that carries that pull request, whichever was written first. An item waiting for a check (a retest) can ask to go back only when it passed: with `blocked_on_release_on` set to `passed`, it reopens once every item it waits for is done with the verdict `PASS` (the optional `verdict` of `complete_work_item`: `PASS`, `FAIL` or `INCONCLUSIVE`); one done `FAIL`, `INCONCLUSIVE` or without a verdict keeps it blocked, with the claim note `still blocked: <id> completed FAIL`, and no run starts on it. An item can also wait for pull requests or issues (`blocked_on_refs`: the repository, the number and whether it waits for the merge, the close or either): it goes back to the queue by itself once every one of them merged or closed as asked, with a dated `released:` line on top of its description that gives the time the code host states for the merge or the close and, when later, the time of the release (`released: owner/name#12 merged at 2026-11-04T00:59:04Z; released at 2026-11-04T01:22:19Z`); a pull request closed without a merge that the item wanted merged keeps it blocked, with a claim note. An item can also wait for a change to be live (`blocked_on_refs` with kind `deployment`, naming a pull request or a commit): it waits for the merge first, then for the first build of the platform that contains the merged commit, and goes back to the queue within a minute of that build starting; meanwhile its `Waits for` line reads `deployment of owner/name#12 (merged 1a2b3c4, not in build <version>)`, and its `released:` line names the build. Nothing polls for it: the check runs when a new build starts and once when the item is blocked. An inbox takes a released item 45 seconds after the release (its `not_before`, which the `released:` line names), so the run that merged the pull request completes the item itself instead of a second run being started on it. The release follows the code host's events at once; for a repository that sends none, or a pull request that merged before the item was blocked on it, the platform also reads the state of the references itself every few minutes, with the item owner's own access to the code host, so the item is released within about five minutes. A blocked item is black or white: setting `Blocked` needs an owner agent and something that releases it, either the items or references it waits for, an open pull request bound to the item (its CI completion hands it back), or a person's decision (`needs_human` with an ask card: the question, at least two options and a recommendation); it is refused otherwise. A reason in words (`blocked_reason`) is context, never the blocker; an item deferred to later stays open with a `not_before`. An item it waits for that is cancelled hands it back to its owner. An item blocked before this rule keeps its state, and the fleet health lists it until it is given a real blocker or reopened. A claim made by an assignment run or a background task is held by that run: when the run ends without finishing the item (it failed, timed out, was cancelled or was stopped by a platform restart), the item goes back to the queue on its own within a minute, with a dated note saying which run released it, and its description and progress are kept; another run that claims it in the meantime takes it over, while an item held by a run still working stays refused to everyone else. Another agent run cannot take such an item either: its update or completion that puts it back to `Open`, blocks it or finishes it is refused, naming the holding run, unless it passes `take_over`; then the write lands, the claim goes, a `finished_under_holder` claim note says who did it, and the holding run is told to stop at its next step: a pause it is in ends at once with the stop, whether it is an assignment run, a background task, or a run that claimed the item mid-run. From then on that run's updates and completions on the item, a note included, are refused with the same stop, naming the item's state and its new holder, unless it passes `take_over` to take it back. A person's change goes through the same way without the flag. Adding a note or changing the priority never needs it from another run. A claim that names no run (made in a chat, or before runs were recorded) goes back to the queue the same way once the item has been quiet for 10 minutes and its agent has no run still working that started before the claim; an item put back to `Open` by hand loses its claim at once. An item whose holding run has ended is no longer shown as held or being worked on. Items created before this view existed show no origin: it was not recorded then.

### What the agent working on an item receives

The agent an item is assigned to reads it in full: its whole description, its payload, its result and the files attached to it. When an inbox picks the item up, the run it starts receives all of it. An agent that creates a work item in a chat or a run can attach up to 10 files of that chat or run (a file you uploaded, a file an agent generated or downloaded) rather than copying their content into the description. The agent working on the item can open them whatever chat or run it works in, and no agent that cannot see the item can. Each file stays in the chat or run it came from.

The `Executions` tab shows runs across your visible agents. It is designed for filtering first and investigation second.

## Use the main filter tabs

At the top of the page, XTM One provides quick filter tabs.

You can switch between:

- `All`
- `Notifications`
- `Digests`
- `Running`
- `Awaiting input`
- `Completed`
- `Failed`
- `Cancelled`

These tabs are the fastest way to narrow the page to the type of work you care about.

## Use the other filters

In addition to the main tabs, you can refine the list with:

- search
- visibility
- agent selection
- fleet selection

The `Fleet` filter (`All fleets` by default) keeps the runs of the fleet's current home members, its orchestrator included and its connected agents left out. The same filter sits on `Background Tasks` (tasks whose delegating or delegated agent is a home member), `Work Items` (owned or created by a home member), `Shared State` and `Worksets` (owned by a home member). Membership is read when the list loads, so a member added or removed counts at once. With a fleet chosen, the agent selection offers its home members only, the run type is the members' assignment runs, and `Select all matching` is not offered for a bulk delete. The fleet is kept in the address (`?fleet=`), so a link reopens the same filtered list. The `Operations` view of a fleet page and the `Operations` tab of a workspace have no `Fleet` filter: their lists are already locked on the fleet or the workspace.

Visibility has four segments:

- `Mine`
- `Shared`
- `Company`
- `All`

This is useful when you work in a shared environment and only want to focus on your own workload, on runs shared with you, or on the company-managed runs.

## Understand the run cards

Each run card gives you a quick status summary.

You may see:

- the agent name
- the assignment or run title
- a short summary
- the current state
- the creation time
- delegation indicators

Typical states include:

- running
- awaiting human input
- completed
- failed
- cancelled — a run cut by the [emergency stop](../administration/emergency-stop.md)
  carries the cause `Stopped by an emergency stop.`

## Notifications and digests

The `Notifications` and `Digests` filters are there for communication-oriented runs.

Use them when you want to:

- review automated updates sent by the platform
- separate digest-style outputs from normal task runs
- find communication outputs without scanning every run

## When a run is waiting for you

Some runs pause and ask for human input.

When that happens:

- the run appears under `Awaiting input`
- the run detail page shows the question or pending action
- you can respond directly from the run
- you can dismiss the waiting item if it is no longer needed

This is the main place where human approval or clarification appears in the UI.

## When an integration resource goes missing

An assignment can keep pointing at something its integration can no longer see - a Notion database that was deleted, moved, or is no longer shared with the integration. A single miss can be a typo in one run, so it changes nothing. When the integration reports the same resource missing in two separate runs, the platform treats it as the assignment's configuration:

- the run that used it again is marked `failed` with the cause `Integration resource missing`, and its error names the resource and the integration; whatever the agent produced stays in the result
- the assignment owner gets one notification when this starts, not one per run
- after a few consecutive scheduled runs like this (three by default), the assignment is paused; the assignment page shows why, and `Resume` switches it back on

To fix it, share the resource with the integration again, or update the assignment so it no longer uses it, then resume the assignment. Resuming starts a fresh count, and a scheduled run that succeeds ends the episode. Only a resource the agent used counts: an id it only read along the way - the target of a relation, a child block, a search result - never fails a run.

## When an assignment's owner is deactivated

While an administrator has disabled the account that owns an assignment, nothing starts that assignment unless it is company-managed - an assignment whose agent is shared with a group or a person pauses too: not its schedule, webhooks, integration events or work items, not a chained assignment or a retry, not a flow step, and not a manual run, whoever asks - an administrator included. The assignment page and the agent's assignment list show it as paused because its owner's account is deactivated.

- nothing about the assignment itself changes: its own on / off switch and any pause its owner set are kept, so re-enabling the account resumes it exactly as it was, and a schedule that came due meanwhile runs once, as after a resume
- a run that was already waiting to start (a delayed run, a queued webhook delivery, a retry) is marked `skipped` with the reason when it would have started; a run in progress finishes, but one that stopped to wait (for a partner's answer, for example) is marked `skipped` - stopped before resuming - when it would go on
- a webhook delivery to it is answered `ignored` with the reason; a question it asked stays pending until the account is back (dismissing it still works)
- company-managed assignments are not affected: they keep running even when the administrator who created them is deactivated
- to keep one running while the account stays disabled - a shared agent other people rely on, for example - an administrator transfers its agent to an active owner or makes the agent company-managed

## Open a run detail page

Click any run to open the detailed view.

The run detail page usually includes:

- the run title
- the status
- the agent
- the trigger that started the run
- timestamps for created, started, and completed states
- shortcuts to the related agent, assignment, or parent run

## Review the result

For completed runs, the detail page helps you understand what happened.

Depending on the run, you may see:

- a final summary
- tool actions
- intermediate reasoning or step history
- output sent to another system
- child runs created through delegation

This makes the page useful both for everyday checking and for troubleshooting.

## When a run stopped before it was done

A run can end on one of its limits rather than because the agent finished: it used all its `Max iterations`, its timeout ran out, the run budget cut its answer short, the model did not answer in time, or delegated tasks were still running when it stopped waiting. The agent still writes up what it has, so the run is `completed` and keeps that result, but the `Result` card says it is partial and names the limit, instead of showing it as a finished task.

For a run that used all its iterations, check the assignment's `Max iterations` first: when the assignment sets its own value, it is the one the run uses, whatever the agent's value is. The assignment page shows the value it uses, marked `Inherited` when it takes the agent's or the platform's.

## What the run struggled with

A run that met difficulties shows a `Frictions` card on its page, with the number of frictions it recorded: what this run struggled with, and what carries each fix. The card is shown only for a run that recorded at least one, and lists up to 50, the oldest first.

Each row names:

- the kind of friction, as the platform writes it: a tool error (`tool_error`), a retry, an approval pause, a sandbox or runner failure, a friction the agent recorded itself (`manual`), and a few others;
- the tool involved, when there is one, and `Met N times` when the run met the same friction more than once;
- its class, once the friction has been classified;
- what carries its fix: a work item, a shared state entry, a skill or prompt edit, each with its reference, or `accepted` when the behaviour is fine as it is. A friction nobody has dealt with yet reads `Undisposed`, and the card's header counts them.

`Evidence` unfolds the error or the call as it was recorded.

The platform records every tool error a run meets by itself, and the same error met again counts once more on the same row instead of adding one. An agent that carries the `Coordination` built-in tools can also record a friction, list the frictions of its runs, and classify one and name what carries its fix. Use the card to see why a run took longer than expected, or which fixes are still owed.

## Rate the result

Once a run has completed or failed, the `Result` card asks `Was this result useful?` with a thumbs up and a thumbs down, like the buttons under an answer in the chat. A thumbs down lets you say what went wrong. Your rating is yours: rating again replaces it, and clicking the thumb you chose removes it.

The same rating is offered wherever an agent's work can be read: on a finished background task that was started from a chat (in `Operations > Background Tasks`; a task started by an assignment is rated on its run page), and on the answer an agent gave when another agent consulted it for you, in the agent's `Delegations` tab (a background task is rated as the task or its run, and a transferred conversation in the chat, so the same work is never rated twice). The people who manage the agent see every rating in its `Feedback` tab, with a link to the rated run or conversation when they may open it.

When your administrators have defined feedback criteria (`Settings > Quality`), the dialog also asks you to score each of them, such as the accuracy of the answer on a scale of 1 to 5 or its tone. Score what matters to you and skip the rest: a thumb alone is still a complete rating.

## Follow delegated work

One run can create more runs if work is handed to another agent.

Use the `Executions` list and the detail page together to understand:

- where the work started
- whether another agent took over
- which parts succeeded or failed

This is especially important in environments that use multi-agent flows.

## Bulk actions

The `Executions` tab also supports selection and cleanup actions.

Depending on your permissions, you may be able to:

- select multiple visible runs
- delete selected runs
- select all runs matching the current filters

Bulk actions are useful when you need to clean up large sets of test runs or old operational records.

## Administrator-only cleanup

Administrators can also see `Cleanup Stale Runs`.

This action is meant for recovery situations, such as:

- a restart left some runs stuck
- a run exceeded its timeout
- the page still shows work as running when it should have failed

The cleanup action marks those stale runs as failed so the list becomes accurate again.

### Cancelling runs that have not started

When an agent piles up runs it cannot start (its daily quota is exhausted, or a burst of events arrived on a busy assignment), an administrator can cancel every run of an agent or an assignment that is still `pending`, `delayed` or `queued` in one call, optionally only those created before a given time. The runs stay in the history with the status `cancelled` and the reason `Cancelled by <your name>`; runs already running are never touched. The same action is available to an administrator's agent as the `cancel_runs` tool (with `dry_run` to count first), and through the API as `POST /api/v1/assignments/runs/bulk-cancel` (`dry_run: true` answers the count without changing anything).

On **Operations > Executions**, an administrator filters the list on one agent and clicks **Cancel runs not started**. The page first counts the agent's runs that are still `pending`, `delayed` or `queued` and asks for confirmation with that count ("Cancel 12 runs that have not started?"); nothing changes until you confirm. Once confirmed, the list switches to the **Cancelled** status filter and shows the runs that were just cancelled. Without an agent filter the action is disabled: a cancel on status alone would reach every waiting run of the platform, and the API refuses it. The per-run actions are unchanged.

## Next step

The next chapter covers [`Resources`](resources/index.md), which includes knowledge bases, prompts, skills, MCP servers, tools, variables, and integrations.
