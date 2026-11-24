# Work-item staleness and fleet member health

One rule says when a work item is stale. Every view reads it from the same
place, so a list filtered on `stale_only`, the stale counters and the fleet
member health can never disagree.

## The rule

`app.services.work_item.is_work_item_stale` (Python, for one serialized item)
and `_stale_sql_clause` (its SQL form, for `stale_only`,
`count_stale_work_items` and `open_work_item_counts`) define it. A
non-terminal item is stale when:

1. **It is past its `due_at`, and no live run holds it.** A live run holds an
   item when the item is `claimed` or `in_progress`, records its holder
   (`claimed_by_run_id`) and that run or background task is alive
   (`LIVE_RUN_STATUSES` / `LIVE_TASK_STATUSES`, `_holder_alive_clause`). An item
   someone is working on is not neglected because a date passed.
2. **Or it is `claimed`, `in_progress` or `blocked` and quiet beyond the
   window** (`work_item_stale_after_hours`, default 24 h, on
   `last_activity_at`, else `created_at`), whoever holds it. A live holder that
   writes nothing on its item for a day is a dropped promise.

Never stale: a terminal item, a scheduled item (`action_mode`), an item blocked
on other items (`blocked_on`), an `open` item with no due date (backlog waiting
for its inbox). With the window disabled (`<= 0`) only rule 1 applies.

| Item | Past due | Quiet beyond the window | Stale |
|---|---|---|---|
| `in_progress`, holder run alive | yes | no | no |
| `in_progress`, holder run ended or not recorded | yes | no | yes |
| `claimed`, claim records no run | yes | no | yes |
| `open` | yes | any | yes |
| `blocked` (not on items) | yes | any | yes |
| `in_progress`, holder run alive | no | yes | yes |
| `open`, no due date | no | yes | no |

An item created `claimed` or `in_progress` (`create_work_item(status=...)`)
is held from its creation: `creator_claim_columns` records the creating agent
and the run (or background task) it is created in, so a sibling run reads a
live holder instead of an unheld item. A run handed back by a stopping worker
stays `running` under the same id until the next worker resumes it, so its
claims stand between workers.

The built-in tool also takes `status` `done` or `cancelled` with a `result`
(#10190): a carrier item, filed only to hand a `fleet_impact` over at a
merge, is created `claimed` by the creating run (never dispatchable, so no
inbox pre-claims it) and completed through `complete_work_item` in the same
call, its `work_item.completed` event routed as any completion's. A finished
create with `blocked_on`, `blocked_on_refs`, `blocked_reason`, `needs_human`
or `ask`, or a `result` with any other status, is refused before any write; a
deduplicated create completes nothing.

One create call is atomic: every field it is given (description, payload,
attachments, blockers) is written in the one commit, and the dispatch wake is
sent after it, so no inbox claims an item before its create committed. A
creator that finishes an item in later calls passes `hold: true` (#11515): the
item is created `claimed` by the creating run, like a carrier, so no inbox
takes it; `update_work_item(status="open")` from that run releases it and
wakes the dispatcher. A run that ends without releasing it hands it back
through `release_ended_run_claims`, so a held item is never lost. `hold` is
refused with another status, on an item created blocked, and outside a run or
background task (no holder).

A claim held by a live run is never dropped by another run's write:
`update_work_item(status="open")` from an agent (the tool passes its
`writer_run_ids`) on an item whose `claimed_by_run_id` is a live run that is
not one of the writer's is refused with the holder named. Dropping it would
let the inbox start a second run on the item while the first still works it
(the creator of an `open` item setting it "back to open" after the creation
wake had dispatched it). The item returns to the queue by itself when its
holder ends (`release_ended_run_claims`). The writer's own run, an ended
holder, a claim with no run and a write with no run (a person's) requeue as
before.

The Python form needs the holders' state: `serialize_work_item` passes what
`holding_runs` read (`get_work_item`, `list_work_items`). A caller that reads no
holder (`holders=None`) keeps the strict reading of rule 1.

## Fleet member health

`app.services.fleet_views.member_health` gives each member one status, the
worst that applies: `failing` (its latest judged run failed), `disabled`,
`stale` (it owns at least one stale item by the rule above), `idle`, `ok`. The
stale count per member comes from `open_work_item_counts`, the same SQL clause
as `stale_only`, so the member reads `stale` exactly when
`list_work_items(stale_only=true, owner_agent_id=<member>)` lists an item.

## The home dashboard: what waits for the person

The dashboard notice does not read the stale count. An item an agent owns that
waits on another item, a pull request or a deployment, or that a run holds, is
the agent's work: it stays stale in Operations and on the fleet member, never
red on the person's home page. The notice reads `GET /work-items/waiting-for-me`
(`work_items_waiting_for_person`, SQL `_waits_for_person_clause`), the caller's
own non-terminal items (`user_id`) that are:

1. flagged `needs_human` (blocked on the person's decision), whoever owns them; or
2. owned by no agent, `open` and past their `due_at`.

A scheduled item is never counted. The answer is `{count, oldest}`, *oldest*
the item waiting longest (`id`, `title`, `reason` = its `blocked_reason`,
`since` = its last activity): the notice reads "waiting for you since <date>:
<reason>" and opens that item.

The moment an item starts waiting for a person, the work item service raises
the platform event `work_item.needs_human` after the commit of
`create_work_item` / `update_work_item` (`waits_for_person_transition`):
`needs_human` turned true, or the `blocked_reason` of an item no agent owns
changed to a new non-empty text. It is raised once per transition (the same
write again raises nothing) and never on a terminal item; the payload carries
`blocked_reason` and `waiting_since`. The person's assistant agent subscribes
to it (a `platform_event` trigger, filter on `status`, `owner_agent_id` or `blocked_reason`) to
reach the person where they read, so nobody has to watch the notice.

## Listing filters

`_filter_clauses` in `app.services.work_item` builds the WHERE clauses of the
list, its count and the bulk delete, so the three never drift apart. Its
`search` (the tool's `query`, the Operations tab's search box) is an `ILIKE`
on the title or the correlation key with the LIKE metacharacters escaped by
`_like_literal` (escape character `\`): a tool name such as `list_work_items`
matches its underscores literally. Visibility is never part of it: the caller
adds the scope clause (`fleet_work_item_clause` through the tool) as before.
The tool caps `query` at `LIST_QUERY_MAX_CHARS` and, with `compact`, renders
one line per item instead of the preview block.

## The graph read

`GET /work-items/graph` (`app.services.work_item_graph`) answers the Graph
view of Operations > Work Items in three reads: `list_work_items` with the
list's filters and visibility (first `MAX_GRAPH_ITEMS`), then
`list_visible_work_items_by_ids` for the blockers the filter left out (same
`_visible_clause`; a blocker the caller cannot see is dropped with its edge),
then `describe_work_items` for the owner names. `build_graph` is pure: item,
ref (`blocked_on_refs`, one node per reference) and person nodes; edges
`blocked_on`, `ref`, `person` from the waiter, `program` from the root.

## `due_at`, `not_before` and the inbox pick-up

Two fields, two meanings (#8753):

- `due_at` is the **deadline**: when the work should be done. It defers
  nothing. A past `due_at` on an item nobody holds is what rule 1 reports, and
  the abandonment sweep never cancels an item before it.
- `not_before` is the **earliest pick-up**: `list_dispatchable_work_items`
  hands an `open` item to its owner's work-item inbox when `not_before` is
  absent or already reached. Absent means now.

An item that must be picked up at once needs neither field: setting a past
`due_at` to wake an inbox is never needed and makes the item look overdue.
`update_work_item` (tool and `PUT /work-items/{id}`) clears either field with
an explicit `null`; a call that omits it (or sends `""` through the tool)
leaves it unchanged. The migration `n8753notbef01` adds the column and every
existing item keeps its `due_at`; an `open` owned item whose `due_at` is still
ahead (the old deferral) gets that `due_at` copied into `not_before`, so it
keeps its pick-up time across the deploy. Every other item is dispatchable at
once.

The readers show it (#10362): `list_work_items` names `not_before` on every
line that carries one (in the flags of the full listing, after the status in
`compact`), and `get_work_item` on its status line, a description window
included. An `open` item whose `not_before` is still ahead reads
`deferred until T`, so a watchdog tells a deferred item from a stuck one; any
other item reads `not_before T`; an item without the field shows nothing of it.
