# CTEM loop visibility: the loop is never hidden

Issue #838. The reference for the user interface of the CTEM loop program:
every item of the loop (signals `ctem-signals.md`, the reconcile and scope
gate, cases from signals `ctem-cases.md`, validation, decision, mobilisation,
re-test) ships with a small, visible change on the surfaces that exist today.
This document says where each stage shows, how a decision explains itself,
how it moves in real time, what may be added and what may not, and how a demo
shows the real pipeline. It builds on `ctem-command-center-v2.md` (sections 2,
4 and 7 stay binding) and `ctem-cases.md` (principles 6 and 8 stay binding).

## 1. Rule

The user can always see that the loop is working and why it did what it did:

- every signal received shows, with its trigger path, its lens and its state;
- every decision shows its outcome and one human sentence, the inputs it used
  and links to the source objects; an ignored, discarded or watchlisted signal
  says why, in the same way as one that opened a case;
- every case transition (opened, updated, validated, decided, re-tested,
  closed, reopened, escalated) shows where the case is shown today;
- any signal or case can be followed through the whole loop from one place,
  the loop trace (section 4).

Three limits hold every change: it extends an existing surface, it never adds
a sample figure to a connected instance, and it breaks nothing (every widget
id, deep link, saved layout and test of `command-center-v2-default.test.ts`
keeps resolving).

## 2. Where each stage shows

The loop stages, the surface that shows each one (all of them exist today
except the loop trace), what the user sees there, and the item that feeds it.

| # | Stage | Surface | What the user sees | Fed by |
|---|---|---|---|---|
| 1 | Signal received | Live activity row, Done for you (modules lane), Discovery stage page | "Signal received" row: subject label, trigger path, lens chip, state chip, relative time; a dot on the timeline | CTEM-01 signals (`ctem_signals`), stream frame `ctem_signal_update` |
| 2 | Reconcile | Same row (state chip moves), loop trace | `confirmed`, `to_validate` or `discarded`, with the decision-reason chip | CTEM-02 reconcile (`ctem_signals.decision`) |
| 3 | Scope gate | Same row, Prioritization stage page (watchlist count), loop trace | `case_opened` or `watchlist`, with the reason ("Watchlist: criticality medium, below high") | CTEM-02 scope gate (`ctem_signals.decision`) |
| 4 | Case opened | Cases board, cases counts row, Act now, Done for you (outcomes lane) | New card on the board in "Needs attention", the counts row moves, an Act now row when the severity asks for it | CTEM-03 cases from signals, stream frame `ctem_case_update` |
| 5 | Prioritise | Case page summary, Act now ranking | Severity P1 to P3 chip with its one-line why, SLA time left | CTEM-02 severity, CTEM-03 SLA |
| 5b | Review (step 3) | Case page card "Analyst review", case page History | The threat package drafted in OpenCTI (the Grouping and its link, for a viewer who reads OpenCTI; "restricted" for anyone else), "Coverage requested in OpenCTI"; gate `ctem.analyst_review` as "Analyst review skipped by policy" (the default, never hidden), the waiting review on the unified work-item decision card (item `ctem:review:<case>`), or who answered: a person or the analyst's mark in OpenCTI; never blank | CTEM-16 (`manage_opencti_security_coverage`, `ctem_analyst_mark`), CTEM-16-UI (`threat_package` case event, `update_ctem_case` `threat_package`) |
| 6 | Validate | Case page card "Validation", case page history, Validation stage page, loop trace | "Validation requested", then "Not exposed" (closes) or "Exposed on N assets" with the link to the OpenAEV run; per validation reference the run order of the target asset groups; gate A (`ctem.validation.non_readonly`) as passed automatically at the fleet's level, or the waiting test on the unified work-item decision card (item `ctem:validation:<case>`) | CTEM-15 OpenAEV events, CTEM-13 exposure result, CTEM-12 (`validations[].run_order`; `safe_mode` no longer rendered, #1810) |
| 7 | Decide | Act now (case row: "Remediation decision waits"), case page card "Remediation decision", case History | "Decided by policy" with the option and why, or the waiting decision on the unified work-item decision card (options with figures, the recommendation; figures only for a reader of that item), or who answered and the option; each `gate` event of the History in words (gate, passed automatically, waited, answered) | CTEM-17 decision gate (`ctem.remediation.decision`), item `ctem:remediation:<case>` |
| 8 | Mobilise | Cases board, case page | Owner, due date, ticket key and status chip | CTEM-19 ticketing |
| 9 | Re-test | Case page history, Done for you (outcomes lane) | "Re-test passed" or "Re-test failed, reopened" with the run link | CTEM-03, CTEM-14, CTEM-15 |
| 10 | Close and feed back | Cases board (Resolved), Governance and Risk, loop trace | The resolution kind (not exposed, mitigated, fixed, accepted risk) and its proof; what was fed back (scope, inventory, PIRs) | CTEM-03, CTEM-30 measures |

Loop measures (stage 10, Governance and Risk, card "Loop measures"):
`GET /ctem-cases/measures` (`services/ctem_loop_measures.py`) gives the median
hours from signal to case (the earliest origin signal's `observed_at` to the
case's `created` event), case to first `validation` event, case to decision
(the first `state` event to `in_progress` or `accepted_risk`; CTEM-17's
decision record replaces it), case to passing re-test (the first `validation`
event `passed` or `not_exposed`), and the share of resolved cases whose newest
validation passes (closed on proof). Only the cases the reader sees, and only
the signals the reader may read, are measured; a step no case reached reads
"No case yet", never 0.

Gates (Loop document naming): gate A (tests approved) shows on stage 6, gate B
(remediation decision) on stage 7, gate C (production change) on stage 8.
Each gate in the loop trace is drawn passed, waiting or stopped, with who and
when.

## 3. The why pattern

One component, `DecisionReason`, everywhere a decision is shown (activity
row, case page, Act now row, loop trace). It renders the decision record the
backend writes on the signal (`decision`) or the case event (`reason`):

- **outcome chip**: the state in words, its colour from the state tokens of
  `ctem-cases.md` section 7.0 (never a raw state key);
- **one sentence**, built from the record, never written by a model at read
  time. Examples: "Ignored: host not in any crown jewel scope (threshold
  high)", "Watchlist: criticality medium, below high", "Discarded: not
  confirmed by inventory", "Case opened: crown jewel Customer SaaS hosting,
  criticality critical";
- **inputs**, in a tooltip or the loop trace: each input the rule read
  (threshold, criticality found, lenses that confirmed, policy rule) with its
  value and source;
- **links** to the source objects (the OpenCTI, OpenAEV or Aikido object of
  the signal's `evidence.link`, the crown jewel in Settings > Organization,
  the case).

Contract for the backend items: a decision record carries at least
`{outcome, reason_code, reason_params, decided_at}`; the reconcile and scope
gate record (dev-docs/ctem-scope-gate.md section 6) adds `reconcile`, `scope`
and `policy_checks`, which the loop trace lists as labelled inputs. `reason_code` is a stable key the frontend translates with
`reason_params` (en, fr, es, ja catalogs; ICU plurals for counts); the
English `reason` line the gate also stores is for agents and is never drawn. A record with an unknown
`reason_code` shows its outcome chip and "Reason not available in this
version", never an empty cell. A signal with no decision yet shows "Waiting
for reconcile".

A stopped signal (watchlist, to validate, discarded) also says what would
make it a case, built from the same `reason_code` and `reason_params`
("Becomes a case if criticality rises to High"), in the loop trace under the
sentence; an in-scope or unknown code has none. The inputs say whether an
asset matched ("None matched" on a decided record) and the criticality against
the threshold X ("Medium, below High"). The Prioritization stage draws the two
gates as one line under the funnel, never a new card: signals in, confirmed,
in scope, opened a case and the watchlist, from the counts by state of
`GET /ctem-signals`, refreshed by the `ctem_signal_update` frame. The line
opens with "Signals:" and says "opened a case", never "cases": the same card
already counts open cases, and a count that shares a noun with another count
on the same card names its scope (UX r1 B1 on #875).

## 4. The loop trace

The only new surface of the program. It answers "where is this object in the
loop, and why".

- **Shape**: a centred dialog, not a drawer (principle 7 of
  `ctem-command-center-v2.md`, Sam, 7 Nov 2026), with its own deep link. Since
  #1263 the trace is a section of the Signals page (`/ctem/signals?signal=<id>`);
  the `?loop=` links are gone (CTEM is unreleased, no redirect is kept), so a trace can
  be shared and reopened after a reload. Focus returns to the row that opened
  it.
- **One sheet, opened in place** (#1934, Sam, 24 Nov 2026): the trace is one
  side sheet, `ctem/loop/LoopTraceSheet.tsx`, used by the Signals page and by
  the case page's "Opened by" card. On a case, "See the loop trace" opens it over
  the case: the pathname is kept and the case URL carries `?signal=<id>`, so a
  reload reopens it and Back, Close or Escape return to the case. Off the
  Signals page the sheet adds "Open in Signals", the only way to leave the case.
  A signal with a `case_id` shows "Open case" on its "Case opened" stage,
  except on that case's own page.
- **Reached from**: a signal row of Live activity, a case card of the board,
  the case page header (More menu, "Show in the loop"), a Done for you point
  that is a signal or a case, an Act now row that comes from a case. Nothing
  else links to it; no menu entry.
- **Content**: the ten stages of section 2 as a horizontal mini-timeline
  (vertical at 1024 and below), the current stage lit, each passed stage with
  its time and its `DecisionReason`, each gate passed, waiting or stopped
  (a `gate` event of the case history, CTEM-34 #975: the catalog key, the
  outcome `passed_automatically`, `waited` or `answered`, what let it run or
  who answered and the option, written by `update_ctem_case` `gate`),
  the stages not reached greyed with "Not reached". A signal stopped at the
  scope gate shows the path up to "Watchlist" and why; a case shows its origin
  signals (several when one fix covers several signals, `ctem-cases.md`
  principle 1) above its own stages.
- **Live**: the dialog subscribes to the same frames as the rows (section 5);
  a stage lights within the stream latency, without a reload.
- **Data**: `GET /api/v1/ctem-signals/{id}` (signal, decision, case id) and
  `GET /api/v1/ctem-cases/{id}` (the case read per viewer, history). A
  dedicated `GET .../loop` read is added only when a stage needs data neither
  holds; the item that needs it says so.
- **Visibility**: the trace reads through the same API as the rows, so a
  signal or case the viewer cannot read answers the not-found state of the
  dialog ("This item is not available to you"), never another user's data
  (`ctem-signals.md` section 5, `ctem-cases.md` principle 8).

### 4.1 The case audit trail (#1490)

`GET /ctem-cases/{id}` serves every `history` event with the audit fields
`services/ctem_case_audit.py` derives at read time (no stored row rewritten,
so an event written before #1490 renders the same way), on top of the
viewer-filtered event:

- `phase`: the CTEM phase (`scoping`, `discovery`, `prioritization`,
  `validation`, `remediation`, the last one worded Mobilization on the case
  page). A kind with its own phase (`KIND_PHASE`, a gate by `GATE_PHASE`, a
  note by its opening words) sets it; a carry kind (state, owner, due, note,
  SLA) takes the phase the case was in.
- `autonomy`: `autonomous` (an agent acted, no gate), `validated_by_person`
  (gate answered), `validated_by_policy` (gate passed automatically),
  `waiting` (gate waited), `changed` (state, owner, due, ticket, validation,
  plan: from -> to), `by_person`, `system`.
- `actor_type` (person, agent, system) and `actor_name` (an agent the viewer
  may see, else `null`).
- `headline` (a note's first sentence; the announced catch-up and backfill
  worded) and `refs` (task, run and decision item ids a note names).
- On a gate: `answer` on the waiting row (at, outcome, by, option) and
  `waited_since` on the answer, so the two read as one story.

The case also carries `phases` (per phase, in CTEM order: entered_at,
exited_at, seconds, events, runs, autonomy counts, waiting, current) and
`current_phase`. A new event kind joins the trail by adding its phase to
`KIND_PHASE` (or `CARRY_KINDS`) and, when it records a change, to
`CHANGE_KINDS`.

## 5. Real time

No polling page and no new channel: the loop rides the activity stream
(`api/v1/activity.py` `activity_stream`, `hooks/useActivityStream.ts`).

- **Frames**: `{"type": "ctem_signal_update", "signal_id", "state",
  "trigger_path", "lens", "at"}` and `{"type": "ctem_case_update",
  "case_id", "state", "event_kind", "at"}`. A frame carries ids and states
  only, never a subject, evidence, score or name: the client re-reads the
  object through the API, which applies the viewer's visibility.
- **Channels**: a signal recorded by a user goes to `activity:<recorder id>`;
  a listener's signal (recorder `system`) goes to the personal channel of each
  administrator of the organization, the only people who can read it.
  A case frame goes to the personal channel of each active user; the re-read
  decides what each sees (a case of no visible member is a 404). Never
  `activity:company` for loop frames.
- **Client**: `useActivityStream` routes the two types narrowly (as it does
  for `flow_run_update`): a signal frame invalidates the `["ctem-signals"]`
  recent, list and one-signal keys; a case frame invalidates
  `["ctem-cases", "list"]`, `["ctem-cases", "case", id]` and
  `["ctem-signals", "case", id]` (the "Opened by" card) and nothing else, so
  a busy pipeline does not refresh every query of the page.
- **Writers**: `services/ctem_signals.record_signal` (and the decision and
  case writers after it) publish after the commit, best effort with a 2 s
  timeout like `scheduled_work_items`; a publish failure never fails the
  write. Target: a recorded signal shows in Live activity within 2 s. The case
  writers of `services/ctem_case_loop.py` collect one frame per case open,
  signal join, validation and SLA breach on their `PendingEvents` and
  `publish_pending` sends them after the caller's commit (a rolled-back
  signal drops its frames with its events).
- **Case screens** (CTEM-03-UI): the case page shows "Opened by" (the case's
  signals from `GET /ctem-signals?case_id=`, each with lens, path, the gate's
  reason and a link to its trace) and history rows for a signal join, a
  validation with its result and proof reference, and an SLA breach; the
  Cases board and the case header show the severity chip, the SLA countdown
  (ticking each minute) and the reopened (last validation failed) and
  escalated (SLA breached) marks; Act now lists up to two open P1 cases first,
  as "Do first", with the SLA left as the reason.

## 6. No-inflation budget

What each surface may receive over the whole program, and what is
forbidden. A companion item that needs more stops and asks Sam.

| Surface | Allowed | Not allowed |
|---|---|---|
| Live activity | one row type per object (signal, case transition) through `ActivityRow` | a second list, a filter bar |
| Done for you | signal and case points in the existing lanes (modules, outcomes), dashed link signal to case | a new lane |
| Act now | case rows ranked by severity and scope criticality, the decision approval row | a second queue |
| Lifecycle band | a counter or a chip on an existing stage chip (for example "3 to validate" on Discovery) | a new chip, a new band |
| Cases board, cases counts row | origin, severity and SLA chips on a card; a watchlist count | a new column of the board |
| Case page | an origin signals section, a loop section in History, "Show in the loop" | a new tab |
| Stage pages | one card per stage for its loop part (signals by path and lens on Discovery, the watchlist on Prioritization) | a new stage page |
| Anywhere | the loop trace dialog (section 4) | a new menu entry, a new full page per item, a new dashboard band, a drawer, without Sam's go |

## 7. Demo mode

The demo shows the real pipeline moving, never a fake state.

- A lab seed script, `.xtm-one/lab/seed_ctem_loop_demo.py`, records signals
  through the real API (`POST /api/v1/ctem-signals`) at a chosen pace
  (`--pace 5s`), on a scenario file (default: the NGINX HTTP/2 case of the
  Loop document, one signal per path it touches, plus one out-of-scope and one
  unconfirmed signal so that "why ignored" shows). Each later item extends it
  with its own step (decision, case, validation, re-test, close) through its
  real API, never by writing rows directly.
- A lab service runs it on start for demos (`lab-ctem-loop-demo`, dark and
  light), declared in `.xtm-one/checks.toml` by the S2 slice.
- `ctem/ctem-demo.ts` stays the labelled empty-state preview of a page with no
  product connected (`ctem-command-center-v2.md` principle 6). It never feeds
  a live surface, and no loop surface reads it.

## 8. Shared building blocks (S2)

Delivered once `ctem-signals.md` is on main, in one PR:

| Block | Where | Reused by |
|---|---|---|
| `DecisionReason` (outcome chip, sentence, inputs tooltip, links) | `frontend/src/pages/xtm-one/ctem/loop/DecisionReason.tsx` | every companion |
| `LoopTraceDialog` and its `?loop=` deep link | `frontend/src/pages/xtm-one/ctem/loop/LoopTraceDialog.tsx` | rows, cards, case page |
| `loop-stages.ts`: the ten stages, the gates, a pure function from a signal (and its case) to the stage reached | `frontend/src/pages/xtm-one/ctem/loop/` | trace, stage counters |
| `signal` activity event and its row (`ActivityEvent` kind `signal_received`) | `ctem/activity-feed.ts`, `ctem/ActivityFeed.tsx` | Live activity, Done for you |
| `ctem_signal_update` and `ctem_case_update` frames, their routing | `services/ctem_signals.py` (publish), `hooks/useActivityStream.ts` | every later writer |
| Demo seed and lab service | `.xtm-one/lab/`, `.xtm-one/checks.toml` | demos, UX rounds |

Acceptance of S2: a signal recorded through the API shows in Live activity
within 2 s without a reload, with lens, trigger path and state; clicking it
opens the loop trace on "Signal received, waiting for reconcile"; the layout
of the command center is unchanged except the new row type; the frontend
suite stays green; UX Director APPROVE in the lab, dark and light.

## 9. Companion procedure (every `dev:ctem-NN-ui` item)

1. **UI impact assessment** in the PR description, before any code: the
   surfaces touched (section 2 row), what is added, what stays untouched,
   before screenshots, and the check against the budget of section 6.
2. **Implement on the existing components** and the blocks of section 8; the
   backend item it follows is merged first. Copy in the four catalogs.
3. **Lab proof** with the real API, through the demo seed extended with the
   item's step: dark and light, 1440 and 1024; before and after screenshots of
   the command center in the PR.
4. **Nothing broken**: the frontend suite stays green; no route, widget id,
   deep link or test removed without the reason in the PR body.
5. **UX gate**: the UX Director's APPROVE on the head in the lab (work item
   `ux:xtm-one-<PR>`), then merge at green.
6. **What the user now sees**: one line and a screenshot in the completion
   result, appended to the program's shipped log.

## 10. Not decided here

- The `reason_code` list belongs to CTEM-02 (reconcile and scope gate); this
  document fixes only its shape (section 3).
- Severity and SLA values belong to CTEM-02 and CTEM-03 (defaults P1 24 h,
  P2 72 h, P3 30 days, in the prioritization policy).
- Copilot mode is unchanged: every surface here is XTM One mode only.
