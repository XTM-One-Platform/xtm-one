# ROI and outcomes for every agent and fleet, in both modes (#9206)

Part of #8975. Extends the ROI engine of #8990 (`dev-docs/ctem-fleets.md`
section 5) and the fleet documentation of #9174 (`dev-docs/fleet-fork.md`)
from the CTEM seeded fleets of XTM One mode to any agent or fleet a user
builds, in Copilot mode and in XTM One mode. Design first: this document, the
UX Director's review of section 6, decisions D1 and D2 for Sam (section 8),
then the slices of section 9.

## 1. What main has today (main 62f96fce, read 6 Nov 2026)

| Area | Today | Where |
|---|---|---|
| Router | `/api/v1/roi` mounted in both modes, administrators only | `main.py` 4442, `api/v1/roi.py` (`require_admin`) |
| Catalog | 20 built-in security units, closed: `declaration_error` refuses another unit, `validate_override` refuses an override of one | `services/roi.py` `BASELINES`, `services/roi_records.py` 166 |
| Recording | Only an assignment run of an agent with `output_format == "json"` whose `output_schema` carries `x-xtm-roi`; units counted from the structured output | `services/assignment_engine.py` 7776, `roi_records.record_run_units` |
| Declaration | Hand-written JSON in `output_schema`; built-ins through `BUILTIN_ROI_UNITS` | `agents/builtin_roi_units.py` |
| Outcome review | `set_run_outcome` exists, no route, no UI: every record stays `accepted` | `roi_records.py` 115 |
| Rates | Catalog rates by role (`analyst`, `ciso`, USD and EUR); the organization profile's `loaded_hourly_rate` applies to every role when its `currency` is the one reported | `roi_report.rates_for` |
| Organization profile | Settings > Organization, administrators, no mode check: it already shows in Copilot mode | `pages/Settings.tsx` 328, `OrganizationSection.tsx` |
| Surfaces | `FleetActivityCard` in the CTEM command center only (XTM One mode) | `pages/xtm-one/ctem/FleetActivityCard.tsx` |

So the backend is mode-neutral already; what keeps ROI a CTEM feature is the
closed catalog, the JSON-only recording, the missing review and the single
XTM One surface.

## 2. Principles

- **Mode-neutral core, mode-specific seeds.** Every table, route and screen
  below exists in both modes. Copilot mode seeds no unit, no fleet and no
  declaration; XTM One mode keeps its 20 built-in units and the
  `BUILTIN_ROI_UNITS` declarations.
- **No model, no run.** Recording, review and reporting are database reads
  and writes. Nothing here starts a run or calls a model (zero agentic quota);
  the only model call around fleets stays the user-clicked "Generate
  documentation" of #9174.
- **Unreviewed is not accepted.** A record carries `outcome = null` until a
  person reviews it. The figures count unreviewed records as value (today's
  behaviour, so CTEM figures stay identical), and every screen shows the
  review rate and the split, never the word "accepted" for an unreviewed run.
- **No regression.** For the same records, `build_report` returns the same
  CTEM figures before and after (fixture test, section 7). Existing
  `x-xtm-roi` declarations keep working unchanged.
- **Enterprise surface.** ROI on agentic runs is an Enterprise feature
  (b3cd836b): in Community Edition the views show the call to action of the
  CE system, never an empty page or a 403.

## 3. Model

### 3.1 Open unit catalog

> Shipped in slice O1 (#9252): `roi_units`, `roi_unit_versions`,
> `roi_records.create_unit` / `update_unit` / `archive_unit` / `known_units`,
> `effective_baselines` merging the custom units (archived ones included, so
> history keeps its figures; their minutes are labelled "organization unit"),
> and the `/api/v1/roi/units` routes of section 4. A custom unit's `rate_role`
> is one of the built-in rate roles until `roi_rates` (section 3.4) ships.
> The recording path does not validate the unit (an unknown unit records
> cost only, as today); slice O2's resolver and declarations API check a
> declared unit against `known_units(db)`.

New table `roi_units` (one migration, revising the single head of main at the
time of the slice):

| Column | Type | Notes |
|---|---|---|
| `key` | `String(64)` PK | slug, `[a-z0-9_]`, unique across built-in and custom |
| `name` | `String(200)` | shown everywhere |
| `role` | `String(100)` | the human role the unit replaces ("Legal counsel") |
| `manual_min_low` / `_likely` / `_high` | `Float` | low <= likely <= high |
| `review_min` | `Float` | under likely |
| `rate_role` | `String(64)` | a key of the rate table (section 3.4) |
| `source` | `String(300)` | label of the minutes ("internal time study, Q3") |
| `archived_at` | timestamp, nullable | archived units stay readable in history, cannot be picked |
| `version` | `Integer` | +1 on every change |
| `created_by_user_id`, `updated_by_user_id`, timestamps | | |

- Built-in units stay in code (`roi.BASELINES`), read-only, overridable through
  `roi_baseline_overrides` as today. In Copilot mode the built-in list is not
  shown as a picker default (it is security wording): the picker lists custom
  units first and shows the built-ins under a collapsed "Security units"
  group. In XTM One mode both groups are open.
- `effective_baselines(db)` becomes the merge of the built-ins, the overrides
  and the custom units. `declaration_error` and `validate_override` read that
  merged set (`known_units`), so a custom unit is declarable and the closed
  check moves to "known in this organization".
- Every write goes to `roi_unit_versions` (unit key, version, the full row as
  JSON, user, time) and to the audit log (`audit_service`), like the
  organization profile versions.
- A unit in use (records or declarations) is archived, never deleted.

### 3.2 Declaration without JSON

> Shipped in slice O2 (#9285): `roi_declarations` (one row per agent,
> assignment or fleet: unit, `count_mode`, `count_path`, `units_per_count`),
> `roi_run_records.work_item_id` (unique), `roi_records.resolve_declaration`
> (assignment, agent, legacy `x-xtm-roi`, home fleet), `per_run` counted for
> any output format by the assignment engine, `per_work_item` counted once
> when an item owned by the agent reaches `done` (never `cancelled`), and
> `GET` / `PUT` / `DELETE /api/v1/roi/declarations` for whoever manages the
> subject (an assignment follows its agent) or an administrator. The
> "Outcomes and ROI" UI section is slice O5.

New table `roi_declarations`, one row per subject:

| Column | Notes |
|---|---|
| `id` | PK |
| `subject_type` | `agent`, `assignment` or `fleet` |
| `subject_id` | the agent, assignment or fleet id (unique with `subject_type`) |
| `unit` | a key of the effective catalog |
| `count_mode` | `per_run`, `per_output_item`, `per_work_item` |
| `count_path` | only for `per_output_item` (today's dot path) |
| `units_per_count` | `Float`, default 1 (a run that always reviews three contracts) |

Resolution at the end of a run, first match wins:

1. the assignment's declaration;
2. the agent's declaration;
3. the agent's `output_schema` `x-xtm-roi` (legacy, unchanged; built-ins keep
   it through `BUILTIN_ROI_UNITS`);
4. the home fleet's declaration (fleet default, inherited by members).

Counting:

| Mode | Units of a completed run | Works for |
|---|---|---|
| `per_run` | `units_per_count` when the run completed successfully, 0 otherwise (a run its tool check or activation prompt skipped counts 0, in the live hook and the backfill) | text-output agents |
| `per_output_item` | `count_units(path, structured output)` (today) | JSON-output agents |
| `per_work_item` | recorded when a work item owned by the agent reaches `done` (not `cancelled`), one record per item | agents working a queue |

A `per_work_item` declaration may carry a selector in `count_path` (#10412),
so a desk counts only the done items that delivered its unit:

| Selector | Counts a done item when |
|---|---|
| (none) | always (the #9285 behaviour) |
| `pull_request_merged` | its `payload.pull_requests` names a merged pull request |
| `result_contains:A\|B` | its completion result holds one of the texts, case-insensitive |

A non-matching item writes no record; the retroactive pass recounts an
existing record of an item that no longer matches to 0 units, its review kept.
Any other `count_path` on `per_work_item` is refused with 422.

`per_work_item` writes its record from the work item completion path
(`services/work_item.py` completion), keyed by `work_item_id`, never twice:
`roi_run_records` gets a nullable `work_item_id` with a unique index, and
`run_id` becomes nullable for those rows (it is already nullable in the
model). The assignment engine hook at `assignment_engine.py` 7776 calls one
resolver `roi_records.resolve_declaration(db, agent, assignment)` instead of
reading `output_schema` directly, and drops the `output_format == "json"`
condition for `per_run`.

### 3.3 Outcome review

> Shipped in slice O3 (#9310): nullable `roi_run_records.outcome` (new
> records unreviewed, existing rows keep `accepted`), `reviewed_by_user_id`,
> `reviewed_at`, `review_note`; `RoiFigures.reviewed` / `unreviewed` /
> `rejected` / `review_rate` in every group of the report;
> `PUT /api/v1/roi/records/{record_id}/outcome` (the run's action rule, the
> work item's owner, then only the reviewer or an administrator; audited),
> `GET /api/v1/roi/records` (administrators; D1 widening is slice O9) and
> `review=unreviewed` on the runs list, its count and the assignment runs
> list. The `roi` block on the run and work item responses and the review row
> are slice O7.

- `roi_run_records.outcome` becomes nullable, default `null` (unreviewed) for
  new rows; existing rows keep `accepted` (they were written before reviews
  existed; the migration leaves them as is, so the CTEM figures do not move).
- New columns `reviewed_by_user_id`, `reviewed_at`, `review_note`
  (`String(500)`).
- `compute_roi`: `rejected` counts zero value and keeps its cost (today);
  `modified` counts its value with the measured review minutes; `null` counts
  like `accepted` for value and is reported in a new `unreviewed` count.
  `RoiFigures` gains `reviewed`, `unreviewed`, `rejected` and
  `review_rate = reviewed / runs`.
- Routes (section 4) let the run's viewer mark an outcome; the review is
  editable by its author and by administrators, every change audited.

### 3.4 Rates and currency

- The organization profile (`loaded_hourly_rate`, `currency`) already shows
  in Copilot mode (Settings > Organization has no mode check). Nothing to
  move. Its copy is mode-neutral today; the slice checks it shows no CTEM
  wording in Copilot mode.
- The rate table gains custom roles: `roi_rates` (`role`, `currency`,
  `amount`, `source`), administrators only, so a custom unit's `rate_role`
  ("legal") has a rate. Resolution: the role's custom rate in the currency
  asked, else the organization's `loaded_hourly_rate` when the currency
  matches, else the built-in role rate, else `analyst`, each labelled.

## 4. API

All under `/api/v1/roi`, Enterprise license, mode-neutral.

| Route | Who | What |
|---|---|---|
| `GET /` | admin; `scope=fleet` also the fleet's ROI readers (D1 = B, #9350) | today's report plus `scope=agent|fleet|platform`, `id=` |
| `GET /export?format=csv|pdf&fleet_id=` | admin; with `fleet_id` also the fleet's ROI readers (#9350) | today's export, now per fleet too; the PDF title loses "XTM One" in Copilot mode ("Value delivered by your agents") |
| `GET /units`, `POST /units`, `PUT /units/{key}`, `POST /units/{key}/archive` | read: any user; write: admin | catalog (built-ins flagged `builtin`, read-only) |
| `GET /units/{key}/versions` | admin | history |
| `GET /declarations?subject_type=&subject_id=`, `PUT`, `DELETE` | owner of the subject or admin | declaration |
| `PUT /records/{record_id}/outcome` | the run's or work item's owner, the record's fleet ROI readers (#9350), admin | `{outcome, review_min, note}` |
| `GET /records?agent_id=&fleet_id=&outcome=unreviewed` | admin; with `fleet_id` also the fleet's ROI readers (#9350) | review queue |
| `GET /api/v1/assignments/runs/all?review=unreviewed` and its count route | as today | runs list filtered server-side on a `null` outcome, count and pager included (6.2, D3) |
| `GET /api/v1/assignments/{assignment_id}/runs?review=unreviewed` | as today | the assignment Activity tab's runs list (`useAssignmentRuns`, route `assignments.py:4345`) filtered the same way, so its list follows the filter as its count does (6.2, E1) |
| `GET /rates`, `PUT /rates/{role}` | admin | custom roles |

The run detail and the work item detail read their record through the run or
item response (`roi: {unit, units, outcome, ...} | null`), no extra call.

Tools (#10006 slice S2): six functions of the `platform` group, alias `roi`
(`app/services/builtin_tools/roi.py`): `list_roi_units`,
`get_roi_declarations`, `set_roi_declaration` (backfill true by default,
`dry_run` previews and saves nothing), `delete_roi_declaration`,
`get_roi_report` (scope mine, agent, fleet or platform) and
`list_roi_records` (`outcome=unreviewed` is the review queue). They apply the
REST routes' rules, held once in `app/services/roi_subjects.py` (manage and
read a subject) and `roi_access.py` (read a report or records); a write is
audited as the user, `via: agent`.

Unit catalog tools (#11881, Sam, 14 Nov 2026): `create_roi_unit`,
`update_roi_unit` and `archive_roi_unit`, administrators only (the handler
refuses anyone else, as `require_admin` does on `POST /units`,
`PUT /units/{key}`, `PUT /baselines/{unit}` and `POST /units/{key}/archive`),
over the same `roi_records` calls. An agent declares a seeded unit first and
adds its own only when none fits the work, or when a seeded unit's minutes do
not fit the organization's industry: `update_roi_unit` on a built-in key
stores the organization's override of its minutes (origin `customized`, the
seed is never rewritten; name and rate role stay seeded), on a custom key a
new version. `set_roi_declaration` on an unknown key answers the three closest
declarable units and names `create_roi_unit`. No tool creates a rate role, and
a built-in unit cannot be hidden.

`get_roi_report` stays bounded on a wide scope (#10627): scope `mine` or
`platform` answers `roi_report.compact_report_dict`, not `report_dict`. The
organization's figures are `totals`; `assumption_facts` and `computation`
are written once at the top level; `agents`, `fleets` and
`units` are pages of compact rows (id, name, runs, units, hours_saved, value,
llm_cost, net_value, roi, unreviewed), highest value first, each page with
`total`, `remaining`, `next_offset` and `idle_not_listed` (groups with no run
and no unit, listed with `include_idle`). `limit` (default 20, at most 200)
and `offset` page the three lists together. Scope `agent` or `fleet` with an
id answers the whole report as before, its computation included. A live
platform answered 281,176 characters before the change, cut at the tool cap.
`GET /roi` and the UI are unchanged.

The compact answer stays under 20,000 characters for 15 units, 6 rate roles
and 20 agent rows (#10664; 24,215 live after #10627): the prose
`assumptions` are left out (they repeat `assumption_facts` line for line and
stay on the agent or fleet report), each distinct `source` string is written
once in `sources` and a fact carries `source_ref`, its index, a `saving` step
of `computation` leaves out the unit name and the currency the report already
gives, and the ROI tools write JSON without spaces after the separators. Guard:
`test_a_wide_answer_stays_under_the_budget` (totals reconciled with the rows).

### 4.1 Retroactive records (#10006, slice S1)

A declaration counts the subject's whole history, not only what comes after
it. `PUT /declarations` takes `backfill` (default `true`): once the
declaration is committed, the SAQ job `roi_backfill_job`
(`services/roi_backfill.py`) replays the completion rules over every
`completed` run of the subject (an assignment's runs, an agent's runs, a
fleet's member agents' runs) and every `done` work item of its agents,
resolving each run's declaration exactly as at completion (assignment, agent,
legacy `x-xtm-roi`, home fleet). The answer carries
`backfill: {requested, queued}`.

- Idempotent: the unique `run_id` / `work_item_id` keys are kept, a second
  pass writes nothing.
- A record that now resolves to another unit or count is recounted (`unit`,
  `units`); its outcome, review minutes and reviewer are never touched.
- The LLM cost of an old run is read from its traces; past retention it is 0,
  counted in `cost_not_retained`.
- A backfilled record carries `backfilled_at` and the `created_at` of its
  run's or item's completion, so the windows place it in its own period; its
  `fleet_id` is the agent's home fleet at the pass.
- No model call, no quota, not gated on the AI switch.

`GET /declarations/backfill-preview?subject_type=&subject_id=` (whoever
manages the subject, or an admin) answers what a pass would write
(`runs_scanned`, `work_items_scanned`, `records_created`,
`records_recounted`, `units_added`, `cost_not_retained`) and writes nothing.
The report answers `backfill: {records, last_at}` for the window, the source
of "N records backfilled on <date>" in How this is computed (UI in #9836 R5b).

### 4.2 Office units in the Copilot catalog (#10006, slice S3)

`CORPORATE_BASELINES` (`services/roi.py`) carries the units the office desks
deliver, so a Copilot-mode fleet declares outcomes without a custom unit:

| Function | Units | Rate role |
|---|---|---|
| Engineering | `change_delivered_pr_merged`, `bug_fixed_pr_merged`, `ui_review`, `release_notes_written` | `software_engineer` |
| Executive office | `meeting_scheduled_or_moved`, `executive_digest_prepared` | `executive_assistant` |
| Operations | `governance_cycle_prepared` | `manager` |
| Marketing | `press_quote_drafted` | `marketing_specialist` |
| Security | `security_alert_triaged`, `vulnerability_assessed`, `compliance_evidence_collected`, `access_review_completed`, `policy_reviewed`, `vendor_risk_assessed`, `sharing_exposure_reviewed` (#10412) | `security_analyst` |
| Legal | `legal_request_triaged` | `legal_counsel` |
| Product | `product_spec_drafted` | `knowledge_worker` |

- Minutes are desk assumptions (`minutes_label` "assumption"), the review
  about 10 to 20% of the likely manual time, as the rest of the catalog;
  sourced figures replace them through the organization overrides.
- One rate role is added, seeded: `security_analyst`, the published
  occupation of XTM One mode's `analyst` rate (BLS SOC 15-1212) with the
  corporate loading, EUR on ISCO OC2. No custom unit or role is created.
- The XTM One security units stay declarable in Copilot mode: declarations
  and figures read `BASELINES_BY_UNIT`, the union of both catalogs; the
  listing keeps one catalog per mode.
- A unit is counted on the desk that did the work; an orchestrator declares
  only what it delivers itself.

## 5. Fleet documentation link (#9174)

The About page of a fleet (fork program, slice 4 or later) gets an "Outcomes"
block: the fleet's unit, its counting mode, the last 30 days (hours saved,
net value, review rate) and a link to the full ROI view filtered on the
fleet. Same component in both modes; it reads `GET /roi?scope=fleet&id=`.
Until the About page ships, the block lives in the fleet page's new "ROI"
view (section 6.3).

## 6. Screens (ASCII wireframes, for the UX Director)

UX round 1 (design review on head `b2160b13`, changes requested, B1 to B6 and
A1 to A5) and UX round 2 (head `f50cc577`, changes requested, C1 to C6 and A1
to A5 of round 2, cited below as C1 to C6 and R2-A1 to R2-A5) are written into
this section. Every control below is a shared
component that the product already has; no screen draws a local radio, badge,
call to action or tab bar. Every figure goes through the locale formatter:
locale numbers, the currency of the organization profile, U+00A0 before "h"
and before the currency. Every new string goes through the four catalogs (en,
fr, es, ja) with no em dash; fr, es and ja are measured at 1024 on the 6.1 row
and on the KPI cards (A4).

### 6.1 Agent and assignment: "Outcomes" sub-section

**Place (B2).** A new "Outcomes" entry of the agent Configuration section row
(`ConfigTab.tsx`: General, Tooling, Knowledge, Channels, Outcomes), and on the
assignment page the "Prompt & Bindings" tab (`prompt`, one of overview,
prompt, channels and activity, `AssignmentDetail.tsx:55-60`) gets the same
block (D5). The fleet default is the same block in
`FleetSettingsPanel` (`FleetPage.tsx:295`, R2-A3). No eleventh agent tab (`agent-detail/tabs.ts` pins the list).

**Edition badge (B2, C1).** One shared badge in both modes: `<EditionBadge />`.
In XTM One mode it keeps today's behaviour (gated on `useIsXtmOne()`). Slice O5
extends it so that, when `useIsXtmOne()` is false (Copilot mode), it answers
"Licensed" or "Community" from the licence state, with one style for each. Today
the licence-state badges are hand-drawn `Badge` markup in two styles
(`QuotasSection.tsx:921-923` cyan "Licensed", `:1012-1014` outline warning
"Community"); O5 migrates these two `QuotasSection` badges to
`<EditionBadge />` and adds a unit test of the two modes. Not migrated (D1):
`MCPEndpointSection.tsx:235` secondary "Community" is not a licence state but a
feature-tier marker, drawn in every mode and licence state (`:96-97`), that
names the platform MCP servers a Community feature; as an `EditionBadge` it
would read "Enterprise Edition" or "Licensed", which is wrong. A shared
feature-tier marker, if wanted, is its own proposal, not `EditionBadge`. The
grep test is scoped to the licence-state badges: it fails on
`t_i18n("Licensed")` or `t_i18n("Community")` inside a `Badge` in
`QuotasSection` and `ParametersSection`, and allowlists
`MCPEndpointSection.tsx` by name. Never an edition badge drawn by hand.
Not an edition either (E3): the Copilot-mode Enterprise License card of
`ParametersSection.tsx:816-824` draws "Trial" / "Active" / "Not Activated".
These are the licence activation status, not the edition: they carry trial and
not activated, which `EditionBadge` does not. They stay, are named licence
activation status, and are allowlisted by line in the grep test; in Copilot
mode the Quotas card reads "Licensed" (edition) and the licence card "Active"
(activation status), two different facts.

Overridden (or declared on this object, nothing inherited):

```
Outcomes                                                  <EditionBadge />
Unit of work   [ Contract reviewed                v ]   Manage units (admin)
Count          [ Per run | Per output item | Per work item ]
               Units per run [ 1 ]
Preview        Last 30 days: 42 runs, 42 units, 28.0 h saved, 1,130 EUR net
                                                       [ Reset to inherited ]
```

Inherited (read-only):

```
Outcomes                                                  <EditionBadge />
Inherited from fleet "Legal desk"                             ( Override )
Unit of work   Contract reviewed
Count          Per run, 1 unit per run
Preview        Last 30 days: 42 runs, 42 units, 28.0 h saved, 1,130 EUR net
```

- **Count mode (B1).** `SegmentedFilter selection="radio"` with three options,
  "Per run", "Per output item", "Per work item". The mode's own field is one
  row under it: "Units per run" (number, minimum 1) for `per_run`, "Output
  path" (text, e.g. `result.contracts`) for `per_output_item`, nothing for
  `per_work_item`. Slice O6 adds `disabled` and a disabled `hint` to
  `SegmentedFilterOption` in `components/ui/segmented-filter.tsx`, with a unit
  test; "Per output item" is disabled on a text-output agent with the hint
  "Needs a JSON output schema." Never a local radio. The arrow-key `move()` of
  the radio group (`segmented-filter.tsx:82-93`) skips a disabled option, and
  the hint is not only a `title` on the disabled button (a disabled element
  gets no hover and no focus): while it applies, "Needs a JSON output schema."
  shows as text under the row. The O6 unit test covers both (R2-A1).
- **Unit picker (A5, C6).** A grouped `SearchableSelect`, with no collapse
  (its groups are headers only and stay so; no local addition to the
  primitive). The built-in group is headed "Security units" and the other
  "Custom", both headers shown in both modes (A2). `sort={false}` in both
  modes (D4): the default sort orders groups by label
  (`searchable-select.tsx:131-135`, `sortByGroupThenLabel`), which would put
  "Custom" first in en and change with the locale, and `sort={false}` also
  stops the sort inside a group. So the caller orders the groups, in XTM One
  mode "Security units" then "Custom", in Copilot mode "Custom" then
  "Security units", and sorts the rows inside each group by locale label
  (`localeCompare` on the translated label). The O6 unit test asserts the group
  order in both modes, in en and fr. Archived units are absent from the picker
  and shown greyed, with "Archived", wherever history shows them.
- **Inheritance state machine (B2).** Two states, never shown together.
  Inherited: every value read-only, the line "Inherited from fleet {name}" (on
  the assignment: "Inherited from agent {name}") and an outline "Override"
  button that copies the inherited values into an editable declaration.
  Overridden: the controls are editable and a "Reset to inherited" action
  deletes the declaration (confirmation dialog). Nothing to inherit and
  nothing declared: the line "Not counted in ROI. Pick a unit to measure the
  time this agent saves." and the picker.
- **"Manage units" (B2)** links to Settings > Organization > Units of work and
  shows to administrators only (Settings is behind `AdminRoute`).
- **Preview (B2)** is written through the formatter, never a literal.

### 6.2 Run and work item: outcome review

```
ROI outcome   [ Accepted ] [ Modified ] [ Rejected ]
              Review time [ 5 ] min      (Modified only)
              Note (optional) [                                 ]  0 / 500
              Unreviewed: counted as delivered in ROI until someone reviews it.
```

- **Relation to the thumbs (B3).** The run Result card header keeps
  `AnnotationButtons` ("Was this result useful?"): it is the rater's feedback
  on the answer and moves no figure. "ROI outcome" is the ROI review: it has
  its own row under the result body, never in the header next to the thumbs,
  and it moves the figures of section 3.3. The two are independent; neither
  sets the other.
- **Control (B3).** A shared `OutcomeReview` component used by the run page
  and the work item side panel: three buttons with `aria-pressed`, none
  pressed while unreviewed; clicking the pressed one returns to unreviewed
  (the `AnnotationButtons` retract pattern). Not `SegmentedFilter`, which needs
  a non-null value.
- **Review time (B3).** Shown when Modified: a labelled number input with a
  "min" suffix, the unit's `review_min` as placeholder; a negative or empty
  value sets `aria-invalid`, shows the error under the field and keeps the
  focus there. The note has a visible counter to 500.
- **Who sees the row (B3, decided).** The row shows to whoever may mark the
  run (section 4), under D1 A as under D1 B. It shows no figure (no hours, no
  value), so it reveals nothing a viewer cannot read; the figures stay on the
  ROI views of 6.3. The row shows only when the run or item carries a record.
- **Review queue (A1, C4).** The agent's review queue is the agent Activity
  tab with a review filter, linked from the review-rate KPI card of 6.3. No new
  tab. The Activity tab has no filter today, only search (`ActivityTab.tsx:39-52`,
  `AssignmentRunList`), so the slice that ships the KPI link (O7) adds it to
  `AssignmentRunList`: a shared `SegmentedFilter` toggle "All" / "Unreviewed",
  URL-backed by the query parameter `review` (`review=unreviewed`; absent means
  All), which the KPI card links to. It shows wherever `AssignmentRunList` is
  used on a page that can carry ROI records (the agent Activity tab and the
  assignment Activity tab); elsewhere it is hidden. The filter is server-side
  (D3): `AssignmentRunList` pages on the server (`useAllAssignmentRuns`, offset,
  20 per page, and `useAssignmentRunCount`, `ActivityTab.tsx:39-60`), so a
  client-side filter would filter one page and leave the count and the pager
  wrong. The runs list and its count endpoint (`/assignments/runs/all` and its
  count route) take `review=unreviewed` (section 4, slice O3). The assignment
  Activity tab lists through another hook and route (E1): `useAssignmentRuns`
  (`assignment-detail/ActivityTab.tsx:18`, `useAssignmentApi.ts:128-134`) on
  `GET /assignments/{assignment_id}/runs` (`assignments.py:4345`), which takes
  `review=unreviewed` too (section 4, slice O3). Unit test, on both the agent
  and the assignment Activity tabs: the parameter sets the filter, the toggle
  writes the parameter, Unreviewed lists only runs whose record has a `null`
  outcome, and the list, the count and the pager follow the filter.
- **Accessibility (R2-A4).** The `OutcomeReview` buttons sit in a
  `role="group"` with `aria-label` "ROI outcome".

### 6.3 ROI views

**Window.** Every ROI view reuses an existing window control; no new period
set. The fleet ROI view, `FleetCostCard` and the agent KPIs share the
`FleetCostCard` control ("Last 7 / 30 / 90 days", `SegmentedFilter size="sm"
selection="radio"`) and one URL parameter, `window` (`7d`, `30d`, `90d`;
default `30d`, as the 6.1 preview). `FleetCostCard` keeps its window in
`useState` today (`FleetCostCard.tsx:28`); slice O8 moves it to `window`, with
a test that the card and the ROI view read the same value (R2-A2).

- **Platform-wide (B4, C3).** `/analytics` is renamed: page title and nav
  label "Analytics" (today "LLM Analytics", `Analytics.tsx:278`, nav
  `nav-model.ts:266`), and it gets `DetailTabs` (as on the agent page),
  URL-backed: "Model usage" (today's page, unchanged) and "Outcomes". The
  labels "Analytics", "Model usage" and "Outcomes" go into the four catalogs;
  the nav entry keeps `groupManagers: true` (the "Model usage" tab stays as
  visible as today). The tab label follows this title decision (R2-A5).
  Every other string that names the page moves with it (D2): `GroupDetail.tsx:291`
  and `:582`, `DepartmentsSection.tsx:201` and `:291`, and the two catalog
  entries whose source is outside `frontend/src` (en.json "...the LLM
  Analytics read path..." and "The org units LLM Analytics and the platform
  insights roll usage up to..."), each reworded to "Analytics" (or "Model
  usage" where the text means the usage tab), with new keys in en, fr, es and
  ja; `nav-model.test.ts:68` asserts the new label. Three more user-facing
  strings move with it (E2): the command palette entry "Open LLM analytics"
  (`Traces.commands.ts:27`) becomes "Open analytics", the toast "LLM analytics
  refresh started" (`Analytics.tsx:259`) becomes "Analytics refresh started"
  (both today in the four catalogs), and the admin notification title of
  `backend/app/services/tracing/unpriced_models.py:160` "...their cost is
  missing from LLM Analytics" ends "...their cost is missing from Analytics";
  new keys in en, fr, es and ja. "model", never "LLM".
  The Outcomes tab reuses `PERIODS` / `PERIOD_LABELS` from
  `pages/analytics/constants` and `AnalyticsFilters` with the fleet and agent
  pickers; the model and source pickers are hidden on that tab. Content: three
  KPI cards (hours saved with the low / likely / high range, net value, review
  rate), a table by fleet then by agent (runs, units, hours, cost, net,
  reviewed %), the assumptions list, Export CSV / PDF (PDF title "Value
  delivered by your agents", through the catalogs). Visibility: under D1 A a
  group manager sees no Outcomes tab (hidden: never a 403 or an empty table);
  under D1 B they see the tab with their fleets' rows only.
- **Per fleet (B5).** A fourth `VIEW_OPTIONS` pill "ROI" on the fleet page
  (`pages/flows/FleetPage.tsx`), shown only to those who can read that fleet's
  ROI (administrators under D1 A, `can_manage` under D1 B); everyone else sees
  the three pills of today. The view reuses the `FleetCostCard` window control
  and labels ("Last 7 / 30 / 90 days", `SegmentedFilter size="sm"
  selection="radio"`) and shows the table of the platform view filtered on the
  fleet, with the per-fleet export. Decided: `FleetCostCard` stays in the Graph
  view's side column, unchanged, because `can_manage` reads the cost under D1
  A too; the ROI view and the card share one window value (one URL parameter),
  so the two never show different windows.
- **Per agent (C2).** The agent detail page gets the same three KPI cards
  scoped to the agent, with the shared window control of this section above
  them (same `window` parameter, default 30 d). Since #11037 they live first in
  the agent's Value tab (the former Budget tab, ROI then run budget), no longer
  on the Overview; `/agents/:id/budget` and an Overview URL carrying the ROI
  state (`#roi`, `?window=`, `chat_*`) redirect there (`legacyAgentTabTarget`). The review-rate
  card links to the Activity tab with `review=unreviewed` (A1, C4).
- **Catalog and rates (B6).** Two sub-sections of the #9173 Settings
  Organization page (`/settings/organization/:section?`, the shared
  `SectionNav`): "Units of work" (`organization/units`: built-ins read-only
  with their override action, custom units editable, archived collapsed) and
  "Rates by role" (`organization/rates`). They land after #9173 S1; never
  appended under the rate and currency fields of the Profile form.
- **XTM One mode command center (A3, C5).** `FleetActivityCard` keeps its
  layout and reads the same report (`GET /roi`). It already draws six
  `Figure`s in `grid-cols-3` per tile (`FleetActivityCard.tsx:164-178`), so the
  review rate is not a seventh figure: it is appended to the tile's caption
  line as "Reviewed {rate}" (7 d, like the others), so no tile gets a third
  row with a lone cell. fr, es and ja are measured at 1024 on that caption
  (the 7 d labels already wrap, #9099 B1). The "Open fleets" / "All"
  links of that card (wrong route on main, #9170 B5) are fixed in slice O8 if
  #9170 has not merged by then.

**States, per view (B6).** Loading is a `Skeleton` of the view's own layout.
Errors are one `role="alert"` line. Empty states are `EmptyState` with a
sentence and an action.

| View | Error line | No unit declared | No record in the window |
|---|---|---|---|
| Analytics Outcomes tab | "The outcomes could not be read." | "No agent is measured yet. Pick a unit of work on an agent or a fleet." Action: "Open agents" | "Nothing was counted in this period." Action: widen the window |
| Fleet ROI view | "The ROI of this fleet could not be read." | "This fleet measures nothing yet. Pick a unit of work for the fleet or one of its agents." Action: "Set the fleet default" | "Nothing was counted in this period." Action: widen the window |
| Agent KPIs | "The ROI of this agent could not be read." | "This agent is not counted in ROI." Action: "Pick a unit" (opens 6.1) | "Nothing was counted in this period." |
| Run / item review row | "The outcome could not be saved." (after a failed write; the pressed state rolls back) | row not shown | row not shown |
| Settings Units of work | "The units of work could not be read." | not applicable (built-ins always listed) | not applicable |
| Settings Rates by role | "The rates could not be read." | "Every role uses the organization rate." Action: "Add a rate" | not applicable |

**Edition matrix (B6).**

| Mode and state | ROI views (6.1, 6.3) | Review row (6.2) |
|---|---|---|
| XTM One, Enterprise | Full | Full |
| XTM One, Community Edition | The shared `EnterpriseUpsell*` components in place of the view; never a local call to action | Not shown |
| AI disabled (either mode) | The views read as usual (they call no model); every upsell stays silent (`useEnterpriseLockedReason()` answers null) | Full |
| Copilot mode, active Enterprise licence | Full, with `<EditionBadge />` answering "Licensed" (C1) | Full |
| Copilot mode, no Enterprise licence | The same shared `EnterpriseUpsell*` components, gated by `useEnterpriseLockedReason()` | Not shown |

## 7. Tests and acceptance

- Fixture test: the same `roi_run_records` rows give identical CTEM figures
  (`build_report` output equal field by field) before and after every slice.
- The 14 built-in dashboard and decay assignments: snapshot of their
  `output_schema` unchanged; `BUILTIN_ROI_UNITS` unchanged.
- Unit tests: catalog validation, resolution order of section 3.2, the three
  counting modes, `per_work_item` idempotence, `compute_roi` with `null`,
  `modified`, `rejected`, rates resolution.
- Tripwire: no model or run entry point reachable from `api/v1/roi.py` (as
  #9195 does for the CTEM posture surface).
- Copilot-mode lab, no seeded fleet: a fleet of two text-output agents with a
  custom unit, one run reviewed modified and one rejected, per-agent and
  per-fleet ROI and the export. XTM One mode lab: command center figures
  unchanged. Captures for the program close.

## 8. Decisions for Sam

Decided by Sam on 6 Nov 2026: D1 = B (built in O9, #9350) and D2 = B, later
(not part of O1 to O9; its own UX round on the chat surface). Both apply in
Copilot and XTM One mode, to every agent and fleet.

| | A | B | Recommended |
|---|---|---|---|
| D1 who reads ROI | Administrators only (today) | Administrators, plus a fleet's owner and the group managers of its workspace read that fleet's ROI and review its runs; the platform-wide view stays administrators only | B: the person who built the fleet is the one who proves its value; reading a fleet's own figures leaks nothing outside it |
| D2 chat turns | Never counted | Counted only on an explicit mark by the user on an assistant message ("Saved me time: unit, outcome"), one record per message, no automatic counting | B later, not built in these slices: it needs its own UX round on the chat surface |

## 9. Slices

Each slice is one PR merged at green; UI slices through the UX gate.

| Slice | Content | Acceptance |
|---|---|---|
| O1 | `roi_units`, versions, audit, merged catalog, `declaration_error` and overrides on the merged set, units API | A custom unit is created, versioned and declarable in `x-xtm-roi`; CTEM fixture figures identical |
| O2 | `roi_declarations`, resolver, `per_run` and `per_output_item` in the assignment engine, `per_work_item` on completion, declarations API | A text-output agent's run records one unit; a work item `done` records one, `cancelled` none |
| O3 | Nullable outcome, review columns, review route, `compute_roi` review counts, `review=unreviewed` on the runs list, its count route and the assignment runs route `GET /assignments/{assignment_id}/runs` (D3, E1) | Review modified / rejected moves the figures; unreviewed is reported |
| O4 | Report scopes (agent, fleet, platform), per-fleet export, mode-neutral PDF title, rates by role (#9321: `GET /roi?scope=agent\|fleet\|platform&id=`, `GET /roi/export?fleet_id=`, PDF "Value delivered by your agents" in Copilot mode, `roi_rates` with `GET /roi/rates`, `PUT` and `DELETE /roi/rates/{role}`; a role resolves to its organization rate in the currency reported, else the profile's loaded rate, else the catalog's, else the analyst rate, each labelled) | Per-fleet CSV and PDF |
| O5 (UI) | Settings: units of work and rates, as #9173 `SectionNav` sub-sections (after #9173 S1); `<EditionBadge />` Licensed / Community in Copilot mode, the two `QuotasSection` badges migrated (not `MCPEndpointSection`, D1), unit and scoped grep tests (C1), the `ParametersSection` licence activation status badges kept and allowlisted by line (E3). Shipped by #9448: `/settings/organization/units` and `/settings/organization/rates` (`RoiUnitsSection`, `RoiRatesSection`, checks in `roi-catalog-form.ts` mirroring `roi_records.validate_unit` / `validate_rate`), the scoped grep in `pages/settings/edition-badge-gate.test.ts` (it matches a `Badge` whose text is `Licensed` or `Community`, so the activation status badges need no allowlist) | UX APPROVE |
| O6 (UI) | Agent and assignment "Outcomes" sub-section, fleet default in `FleetSettingsPanel`, `SegmentedFilterOption` `disabled` and `hint` (keyboard skip, visible hint) with its unit test, grouped unit picker without collapse, `sort={false}` in both modes with caller-ordered groups and locale-sorted rows (C6, D4) | UX APPROVE |
| O7a (backend) | The `roi` block on `GET /assignments/runs/{run_id}` and `GET /work-items/{id}` (`roi_records.review_block`: record id, unit name, outcome, review minutes with the unit's own as placeholder, note, `can_review` by the rule of the review route); no figure in it (6.2 B3) | CI green |
| O7 (UI) | Shared `OutcomeReview` row on the run and the work item (`role="group"`), server-side `review` filter on `AssignmentRunList` with its unit test on the agent and the assignment Activity tabs (`useAllAssignmentRuns` and `useAssignmentRuns`), list, count and pager included (C4, D3, E1) | UX APPROVE |
| O8 (UI) | `/analytics` renamed "Analytics" with "Model usage" / "Outcomes" tabs (C3), every "LLM Analytics" string and `nav-model.test.ts` moved with it, the command palette entry, the refresh toast and the unpriced-models notification title included (D2, E2), fleet "ROI" view, agent KPIs with the window control (C2), shared `window` parameter for `FleetCostCard` (R2-A2), command center "Reviewed {rate}" caption (C5) (and the `FleetActivityCard` links if #9170 has not merged) | UX APPROVE, both labs |
| O9 | D1 = B (#9350): `services/roi_access.fleet_roi_reader` (administrators; the fleet's managers by `can_manage`, a company-managed fleet administrators only; the group managers of a group the fleet's workspace is shared with) gates `GET /roi?scope=fleet&id=`, `GET /roi/export?fleet_id=` and `GET /roi/records?fleet_id=`; the platform and agent scopes and the unscoped records stay administrators only (403); a fleet reader reviews that fleet's records (never a company-managed run's, never another reviewer's) and `can_review` of the `roi` block follows | A fleet owner and a workspace group manager read and review their fleet; anyone else 403 |

## 10. ROI everywhere (#9836)

Sam, 7 Nov 2026: ROI is the business case the product defends, in both modes,
for every user, with out-of-the-box estimates everyone can change and a
computation anyone can redo by hand. This section supersedes D1 = B for the
platform-wide view only (10.2); the fleet rule of #9350 stays as is. Design
first: this section and its wireframes go to the UX Director before any code;
then slices R1 to R7 (10.9), each its own PR with `Refs #9836`, built on main
after O8c (#9810) merges.

### 10.1 What main has (main 31867ae0, read 7 Nov 2026)

| Area | Today | Where |
|---|---|---|
| Report | `GET /roi?scope=platform\|agent\|fleet&id=`, `GET /roi/export` | `api/v1/roi.py` 218, 242 |
| Who reads | administrators; a fleet's ROI readers for `scope=fleet` (#9350) | `_require_roi_reader` (`roi.py` 203), `services/roi_access.fleet_roi_reader` |
| Built-in declarations | 12 of 81 built-in agents (`BUILTIN_ROI_UNITS`) written into `output_schema` at seed | `agents/builtin_roi_units.py`, `agents/seed.py` 299, 638 |
| Seeded fleet members without a unit | `ctem-scoping-analyst`, `ctem-discovery-analyst`, `ctem-prioritization-analyst`, `ctem-validation-analyst`, `ctem-mobilization-analyst` (CTEM posture fleet) | `agents/builtin_fleets.py` |
| Baseline overrides | `PUT` / `DELETE /roi/baselines/{unit}`, audited, `overridden` flag in the list | `roi.py` 348-393, 674 |
| Formula | `value = units x (manual - review) min x rate`; `net = value - llm_cost`; `roi = net / llm_cost`; `payback_months = setup_cost / (net per 30 d)` | `services/roi.compute_roi` (`roi.py` 694) |
| Cost masking | `OBSERVABILITY_COST_DATA_ENABLED` off: costs answered `null` with `cost_masked` | `core/config.py` 2315, `platform_insights/analytics.py` 1191 |
| Surfaces | Analytics Outcomes tab (admin), fleet ROI view, agent KPIs (#9810), command center tile | `components/roi/RoiReportParts.tsx` |

### 10.2 Access scope (R1, backend)

> Superseded by section 12 (#12077) wherever they conflict: "Mine" is what the
> reader did, not the agents and fleets the reader sees (12.4); the
> organization total is one figure for every reader (12.6, invariant b);
> groups of fewer than k people are suppressed for a non-administrator
> (12.5); `scope=platform` is an alias of `organization`. What still holds
> from this section: `roi_org_totals_visible` and who may open an agent's or
> a fleet's view. The as-built state is 12.14.

One new function beside `fleet_roi_reader` in `services/roi_access.py`, the
single place the rule lives:

- `reader_scope(db, user) -> RoiScope`: `RoiScope(organization: bool,
  agent_ids: frozenset, fleet_ids: frozenset)`. An administrator gets
  `organization=True`. Anyone else gets the agents of
  `helpers.user_visible_query(Agent, user_id, group_ids)` (own, group-shared,
  company-managed: the library the user already sees and runs) and the fleets
  of the same query on `Fleet`, plus the fleets `fleet_roi_reader` already
  answers true for.
- `GET /roi?scope=agent&id=` opens to a user whose scope holds the agent;
  `scope=fleet` keeps `fleet_roi_reader` OR the fleet in scope.
- `GET /roi?scope=mine` (new, every user): the report over the records of
  `agent_ids` and `fleet_ids`, with `scope_label = "mine"`. A record counts
  once even when its agent and its fleet are both in scope (record id dedupe,
  test).
- `GET /roi?scope=platform`: administrators always; a non-administrator when
  the organization setting `roi_org_totals_visible` is on (default on), and
  then the answer carries totals and the by-unit rows only, never the by-agent
  or by-fleet rows of objects outside the user's scope (rows outside scope are
  summed into one "Other agents and fleets" row of the by-agent and by-fleet
  lists; the by-unit rows are never folded, a unit names no object; test).
- Every list of the report adds up to its totals for the same scope and
  window (round 2 B1): the by-agent rows, the by-fleet rows (with a "No
  fleet" row for runs outside any fleet) and the by-unit rows each sum to the
  report's hours, value, AI cost and net value. Test on the fixture, for
  `mine`, `platform` as an administrator and `platform` as a
  non-administrator.
- The setting lives on `PlatformSettings` (`roi_org_totals_visible: bool`,
  default true, one Alembic migration), written by `PUT /roi/settings`
  (administrators, `log_audit` action `configure`, entity `roi_settings`),
  read by `GET /roi/settings` (every user: it only says what the page shows).
- Cost masking: when `observability_cost_data_enabled` is off (the
  deployment variable `OBSERVABILITY_COST_DATA_ENABLED`, `core/config.py`,
  exposed as `cost_data_available` in the auth config; there is no setting
  screen for it and this design adds none), `llm_cost`,
  `net_value`, `roi` and `payback_months` are `null` and the report carries
  `cost_masked: true`; every screen renders "Hidden" with the reason in an
  InfoTip, never 0 and never the reason as the figure (10.5, 10.7).
- Editing is unchanged: units, baselines and rates stay administrators;
  declarations stay the subject's owner, the fleet's managers or an
  administrator.
- Tripwire: no model or run entry point reachable from the new routes (the
  existing `api/v1/roi.py` tripwire covers the module).

As built (R1): `RoiScope` and `reader_scope` in `services/roi_access.py`
(managers' fleets read through `Workspace.shared_group_ids`, never a
company-managed fleet), `fleet_visible` opening a fleet's report (not its
export nor its records list) to whoever sees the fleet, `fold_outside` and the
`no_fleet`, `other_agents`, `other_fleets` and `cost_masked` fields of the
report in `services/roi_report.py`, migration `h9836roiorg1`. `cost_masked`
is set on the reports R1 opens (`mine`, `platform` read by a
non-administrator); the views already shipped (administrator platform report,
agent and fleet scopes) keep their numeric costs until R4 renders "Hidden" on
every surface, so no screen ever draws a `null` cost as 0. Tests:
`tests/test_roi_reader_scope.py`.

### 10.3 Out-of-the-box coverage (R2, backend)

- `BUILTIN_ROI_UNITS` grows; beside it `BUILTIN_ROI_EXEMPT: dict[str, str]`
  lists every built-in agent that produces no unit with its reason (one of
  `orchestrator` "counts its fleet's outcome", `transformer` "rewrites
  another agent's artefact", `utility` "a step inside another agent's run",
  `chat` "answers in chat, counted by D2 when it lands"). A test asserts that
  every slug of `BUILTIN_AGENTS` is in exactly one of the two tables, and the
  same for every built-in assignment, so a new seeded agent cannot ship
  uncounted and unexplained.
- No double counting: an agent listed `orchestrator` records nothing; a fleet
  default declaration applies only to members without their own (resolution
  order of 3.2, unchanged). Test: a fleet run whose orchestrator and two
  members all ran counts the members' units only.
- New catalog units for the seeded agents that have none, XTM One mode only:
  `ctem_scope_reviewed` (scoping), `exposure_inventory_refreshed`
  (discovery), `exposure_prioritized` (prioritization, per item of the
  ranked list), `validation_reviewed` (validation), `mobilization_plan`
  (mobilization), `posture_refreshed` (posture orchestrator's fleet outcome,
  once per fleet run), `board_report_drafted`, `indicator_decay_reviewed`
  (decay judge, per indicator judged). Their minutes are never invented: the
  slice files a `research:` item on the Analyst for each (low / likely / high
  manual minutes, review minutes, source), and until the answer lands each
  unit carries `minutes_label = "assumption"` with the reasoning in
  `minutes_source`.
- As built in R2 (#9836): six units land in `CTEM_BASELINES`
  (`ctem_scope_reviewed`, `exposure_inventory_refreshed`,
  `exposure_prioritized`, `validation_reviewed`, `mobilization_plan`,
  `indicator_decay_reviewed`), each labelled assumption, declared on the five
  stage analysts (count path `headline`, one assessment per run) and the decay
  judge (`verdicts`, one per indicator). `posture_refreshed` and
  `board_report_drafted` are not added: the posture orchestrator stays
  `orchestrator` (its fleet's value is the five stage units, never a second
  time), and the monthly board report is an assignment of that orchestrator,
  so its unit is a default assignment declaration of R3 (Default /
  Customized). Every other built-in agent is in `BUILTIN_ROI_EXEMPT`; the
  tests assert the two tables cover every seeded agent and assignment, that
  every seeded fleet has a declaring agent, and that an exempt orchestrator
  declares nothing.
- One spelling (round 2 A4): keys and display names use US English, like
  "organization" and "Customized" everywhere else (`exposure_prioritized`,
  "Exposure prioritized"). Seeded unit display names are i18n keys
  (`roi.unit.<key>.name`, en, fr, es, ja), never stored English shown in
  fr; a custom unit keeps the name its author typed.
- Copilot mode seeds nothing new (principle of section 2).
- Suggested unit for a custom agent (both modes): `services/roi_suggest.py`,
  `suggest_unit(name, description, output_schema, catalog) -> (unit, score,
  matched_terms) | None`, a deterministic token match (lower-case, stemmed by
  suffix strip, stop words removed) of the agent's name, description and
  output property names against each unit's name, key and keywords; ties
  broken by catalog order; a score under the threshold answers `None`. No
  model call (tripwire). `GET /roi/suggest?agent_id=` answers it for the agent
  page; the create dialog calls it on the draft fields.
- As built in R8a (#9836, backend): `services/roi_suggest.py`. Terms are
  lower-case words of 3 letters or more, camelCase split, one suffix stripped
  (`ations`, `ation`, `ings`, `ing`, `ied`, `ies`, `ed`, `es`, `er`, `s`,
  `e`, stem kept at 3 letters or more), stop words dropped. A unit's terms come
  from its key, its name and its keywords (none yet); the agent's from its
  name, description and every output property name (nested objects and array
  items included). Score = shared terms / unit terms, rounded to 3 places; a
  suggestion needs a score of at least 0.5 and 2 shared terms (every term of a
  one-term unit such as `dashboard_built`). Catalog order: the mode's built-in
  baselines, then the custom units not archived. `GET /roi/suggest` takes
  `agent_id` (read rule of the agent, 404 when not visible) or the draft
  `name` / `description`; it answers `{agent_id, declared, suggestion: {unit,
  name, builtin, score, matched_terms} | null}`. A built-in agent answers
  `declared: true` and no suggestion (it ships its declaration); `declared`
  is true when the agent has a declaration row or an `x-xtm-roi` output
  schema. Accepting a suggestion is the existing `PUT /roi/declarations`
  (R8b, UI, UX gate).
- As built in R8b (#9836, UI): one shared `components/roi/RoiSuggestionRow`
  ("Suggested unit: {unit}", an "i" naming the matched words and saying
  that accepting counts one unit per finished run, and "Use this unit"),
  read through `useRoiSuggestion` (`hooks/api/useRoiDeclarationApi.ts`, quiet
  on error, no global toast). Agent page: the Outcomes card shows the row
  under its not-counted line only for an editor of an agent with nothing
  declared nor inherited (never for a fleet or an assignment, never for a
  viewer, so no read happens); one click saves `{unit, per_run, 1}` with the
  existing PUT, a refusal reads "The suggested unit could not be saved. Pick
  a unit instead." Create wizard (Identity step): the row follows the draft
  name and description (debounced 400 ms, not read on Community Edition);
  "Use this unit" marks it chosen ("Counted in ROI as {unit}", Undo), and
  the declaration is written right after the agent is created; a refusal
  leaves the agent created, says so in a toast, and its Outcomes card offers
  the suggestion again. Accepting never starts a run or calls a model.

### 10.4 Defaults you can edit (R3, backend + UI)

Like a seeded knowledge base whose source you can change:

- Every built-in unit, baseline, rate and built-in declaration answers
  `origin: "default" | "customized"`, `customized_by`, `customized_at` and
  `default` (the seed's own value) beside the effective value. Baselines
  already keep their override row (`roi_records.upsert_override`); built-in
  declarations get the same: an administrator's change writes a
  `roi_declarations` row flagged `overrides_builtin`, and the seed
  (`with_builtin_roi_unit`, `seed.py` 299 and 638) skips an agent that holds
  one. Test: customize, run the full seed sync, the customization stands and
  the figure follows it.
- `DELETE` on the override answers "Reset to default" (existing for
  baselines; new for declarations and rates of a built-in role); audited.
- UI (round 1 B4): Settings > Organization > Units of work and Rates by
  role, and the agent / fleet Outcomes block, gain one "Origin" column
  beside "Basis": a `Badge` "Default" or "Customized", the who and when in
  its tooltip; the two badges sit in two columns, never stacked in one cell.
  "Compare with default" and "Reset to default" are entries of the row
  actions menu ("Reset to default" disabled with a tooltip on a Default
  row). Compare opens the existing edit dialog with a two-column diff,
  default -> current, of the changed fields; Reset asks a confirm dialog.
  Rows keep the table's one height at 1024 in fr (mockup, probe).
- R3 removes main's "Overridden" badge of `RoiUnitsSection.tsx` (rendered
  at `content-caption`, 10 px): the Origin column replaces it, a 12 px
  `Badge` with the vocabulary of 10.6 (round 2 A3). The "Customized by"
  tooltip is anchored to its badge (`Tooltip` on the badge itself, side
  top), never to the cell.
- R3 keeps main's `DataTable` toolbar on Units of work and Rates by role
  (search, filters, column picker); the Origin column is one more filter
  (Default, Customized) (round 2 A5).
- As built in R3a (backend, #9836): no `overrides_builtin` flag and no
  migration. A built-in agent's administrator declaration is a plain
  `roi_declarations` row of subject `agent`: it already wins over the seeded
  `x-xtm-roi` (resolution order of 3.2), and the seed writes only
  `output_schema`, never a declaration, override or rate row, so a seed sync
  cannot overwrite a customization and the seed needs no skip. The API answers
  the origin fields: `GET /roi/baselines` and `GET /roi/units` (built-ins)
  carry `origin`, `customized_by` (full name, else e-mail), `customized_at` and
  `default` (the catalog minutes, label and source); `GET /roi/rates` carries
  them on every organization rate (`custom` for a role with no built-in rate)
  and `origin` on every built-in rate; `GET` / `PUT /roi/declarations` carry
  them, with `default` the `BUILTIN_ROI_UNITS` declaration of a built-in
  agent (`custom` for any other subject). Reset to default is the existing
  audited `DELETE` of each (baseline, rate, declaration). `overridden` stays on
  the baselines for the current settings table until R3b. Tests:
  `tests/test_roi_defaults_9836.py` (origin of each, a customized built-in
  declaration wins over the reseeded schema and a reset restores the seeded
  one, the seed module writes no ROI row). R3b is the UI of this section.
- As built in R3b (frontend, #9836): `components/roi/roi-origin.ts` holds the
  vocabulary (`ORIGIN_LABELS`: Default, Customized, Custom), `originOf`, the
  table's Origin filter and the "Changed by {name} on {date}" line;
  `RoiOriginBadge` is the one badge (12 px `Badge`, the line in a `Tip` on a
  `BadgeFocusTrigger`, keyboard reachable); `RoiDefaultDiff` lists the changed
  fields, default then current. Units of work (built-ins) and both Rates
  tables gain an Origin column and filter; the built-in unit row and a
  customized rate row carry Compare with default and Reset to default in an
  `OverflowMenu` (disabled with "Already the default" on a Default unit; a
  rate of a custom role keeps Remove). Compare opens the edit dialog with the
  diff on top. The agent Outcomes card shows the Origin of its own
  declaration and, on a customized built-in agent, the diff to the shipped
  declaration and "Reset to default". main's "Overridden" badge and "Minutes
  source" filter are gone; `overridden` stays in the API for older readers.
  Tests: `RoiCatalogSections.test.tsx` (Origin column and badges, the diff in
  the unit and rate dialogs, Remove only on a custom role).

### 10.5 One figure component (R4, UI)

`components/roi/RoiFigure.tsx`: the only way a screen draws an ROI number.
Props: the report figures, the metric (`hours`, `value`, `cost`, `net`,
`roi`, `payback`, `review_rate`), the window, `cost_masked`, and a `layout`
(round 1 B2): `card` (the value at 24 px, then the range as the caption,
"8 to 17 h") for cards, `inline` ("12 h (8 to 17)") for text and table
cells. One format per layout, never a third.

| Metric | Range | Card value (en / fr) | Card caption (en / fr) |
|---|---|---|---|
| hours | yes | "128 h" / "128 h" | "96 to 170 h" / "96 à 170 h" |
| value | yes | "12,160 EUR" / "12 160 EUR" | "9,120 to 16,150 EUR" / "9 120 à 16 150 EUR" |
| cost | no | "410 EUR" / "410 EUR" | "Measured" / "Mesuré" |
| net | yes | "11,750 EUR" / "11 750 EUR" | "8,710 to 15,740 EUR" / "8 710 à 15 740 EUR" |
| roi | yes | "28.7 x" / "28,7 x" | "21.2 to 38.4 x" / "21,2 à 38,4 x" |
| payback | yes | "Under a month" / "Moins d'un mois" | "2 to 5 months", or the basis when the range collapses (below) |
| review_rate | no | "62%" / "62 %" | "148 of 239 runs" / "148 sur 239 exécutions" |

It formats through the locale formatter (round 2 A2): en groups thousands
with a comma and puts no space before "%"; fr groups thousands and spaces
"%" with U+202F (as `Intl.NumberFormat("fr-FR")` does) and puts U+00A0
before units and currency; the strings above are the expected
output per locale and a unit test asserts them. When the low and high ends
of a range format to the value itself (a payback under a month at both
ends), the caption shows the basis instead of repeating the value (round 2
A1): payback "Setup cost 0 EUR" (fr "Coût de mise en place 0 EUR"), never
"Under a month" twice.
A payback under 30 days reads "Under a month" (fr "Moins d'un mois")
everywhere, never "< 1 month". A masked cost renders "Hidden" with the
InfoTip of 10.7, an absent value "-" with the tooltip of 10.8. The info
button of 10.6 sits beside the figure. `RoiKpiCards`, the command center
tile, the agent and fleet views, the home card, the list columns and the
Value page all render it, all reading `GET /roi` through the one hook
`useRoiReport(scope, id, window)`. Test: for the same fixture, scope and
window, the home card, the Value page hero and the fleet view render the same
strings.

As built (R4a, backend): R4 ships in two PRs. R4a adds the ranges the
component draws: `RoiFigures` gains `minutes_saved_low/high`,
`value_low/high`, `net_value_low/high`, `roi_low/high` and
`payback_months_low/high`, computed in `compute_roi` from the catalog's
`manual_min_low` and `manual_min_high` with the same review minutes, rate and
model cost (payback is shortest at the high end, so `payback_months_low`
comes from the high net value). The report answers them per group as
`range: {minutes_saved, hours_saved, value, net_value, roi, payback_months}`,
each `[low, high]`; a masked cost masks its range (`[null, null]`, never 0).
The `unit` assumption fact gains `manual_min_low`, `manual_min_high` and
`source` (the popover's source line of 10.6). The likely figures, the CSV
and the PDF are unchanged. Tests: `tests/test_roi_ranges_9836.py`. R4b is
the UI (`RoiFigure`, `useRoiReport`, the popover, the views migrated).

As built (R4b, UI): `components/roi/RoiFigure.tsx` draws every figure of
`RoiKpiCards` (so the Analytics Outcomes tab, the fleet ROI view and the
agent section) in the `card` layout, the range as the caption with the unit
written once ("14 to 31.5 h"), the scope's own line (runs, cost) under it, a
masked cost "Hidden" with the reason in an InfoTip, a payback range that
collapses "No setup cost". The pure half (metric to figure, ranges, the label
vocabulary of 10.6, the popover sums) is `lib/roi-figure.ts`. The four report
hooks read `GET /roi` through one `useRoiScopeReport(scope, id, window)`.
`RoiHowComputed.tsx` is the 28 px info button and its popover: a portal
panel (the design system ships no `Popover` export in 1.2.0), 520 px wide,
scrolling past 70 vh, closed by Escape or a click outside, the click stopped
so the card never opens. The table cells, the command center tile and the
`/value` surfaces move to `RoiFigure` with R5 and R6. Tests:
`components/roi/roi-figure.test.tsx`.

### 10.6 "How this is computed" (R4, UI; R7, docs)

The trigger (round 1 B7): a figure is text, never a button. Beside it a
28 px icon button (`Info`, `aria-label` "How this is computed", keyboard
reachable, focus ring) opens the popover; in a `DataTable` cell or a
clickable card it stops the event, so it never opens the row or the card.
The popover is the existing `Popover` primitive, 480 to 560 px wide, a max
height with inner scroll; no side sheet (1024 is the review floor).

```
How this is computed                                   Last 30 days
--------------------------------------------------------------------
Value = units x (manual minutes - review minutes) x hourly rate
      = 42 x (35 - 5) min x 95 EUR/h            = 1,995 EUR
Net value = value - AI cost = 1,995 - 38        = 1,957 EUR
ROI = net value / AI cost = 1,957 / 38          = 51.5 x
Payback = setup cost / net value per month
        = 0 / 1,957                             = Under a month

Inputs
  Manual minutes 35 (25 to 50)   Published estimate
    Source: industry SOC survey (example), triage time per alert
  Review minutes 5               Assumption
    Why: one analyst read of the summary, until research lands
  Hourly rate 95 EUR/h           Customized
    By A. Admin, 3 Nov (default 80 EUR/h)
  AI cost 38 EUR                 Measured
    Each model call of this period, chat and other usage included

Counted: 42 units from 17 runs   [ See the runs ]
Not counted: 3 rejected; 9 unreviewed counted apart (shown in the range)
                                       [ Read the methodology ]
```

One vocabulary on every surface (round 1 B3): Default, Customized,
Assumption, Published estimate, Measured. The API labels of `compute_roi`
and main's `FIGURE_LABELS` ("organization override", "organization unit",
...) map to it in one table in `roi-outcomes.ts`, with a test that every API
label has an entry; "Override" is never shown to a person.

| API label | Shown (en) | Shown (fr) |
|---|---|---|
| seed / built-in value | Default | Par défaut |
| organization override, organization unit, declaration override | Customized | Personnalisé |
| assumption | Assumption | Hypothèse |
| published | Published estimate | Estimation publiée |
| measured | Measured | Mesuré |

Each input row shows its label badge and, on a second line under it, its
source (round 2 B4): the publication for a published estimate, the reasoning
for an assumption, who and when (and the default) for a customized value,
the measured span for AI cost. The source line is one line in the 540 px
popover, truncated with the whole text in a tooltip. Every formula line
keeps its formula in every locale, payback included: fr "Amortissement =
coût de mise en place / valeur nette par mois = Moins d'un mois". Sources
come from `compute_roi` as it already lists them; the numbers are the
report's own, so the popover reproduces the figure by hand (test: the
popover's arithmetic, recomputed in the test from the shown inputs, equals
the figure). "See the runs" opens the Activity list
filtered on the scope and the window. A help bubble on every setting of 10.4
says the unit and what a change moves ("Raising the manual minutes raises the
value of every past and future run of this unit").

The methodology (R7): a product page `/value/methodology` (both modes, every
user, no admin gate) and the same text in `dev-docs/roi-methodology.md`: the
model, the catalog and its labels, the review rule, what is excluded, how to
change each default. CSV exports gain an `assumptions` sheet section and a
`methodology` line; the PDF gains a last page with the formula and the
assumptions list.

As built (R7a, backend + docs): R7 is split like R4 to R6. R7a ships the
exports and `dev-docs/roi-methodology.md`: `report_csv` keeps its figure
lines unchanged, then a blank line, a `methodology` line
(`roi_report.METHODOLOGY`) and an `assumptions` section, one row per
assumption fact (`assumption, kind, name, value, label, source`);
`report_markdown` ends with a page break and "How these figures are
computed": the formula with the report's own numbers per unit, net value,
ROI and payback, what is counted apart, the assumptions table with label and
source. A masked cost reads "Hidden by your administrator" in both exports,
never 0 (`HIDDEN_COST`). Tests: `test_roi_exports_methodology_9836.py`. R7b
(UI, UX gate): the `/value/methodology` page rendering the same text, linked
from every "Read the methodology" of the popover.

As built (R7b, UI): `pages/ValueMethodology.tsx` at `/value/methodology`
(declared before `/value/:tab?`, every user, both modes, no admin gate, no
API read) renders `dev-docs/roi-methodology.md` sections 1 to 6: the four
formulas with the popover's own words, each input, the five labels in the
one vocabulary (`SHOWN_LABEL_NAMES`), what is counted, who sees what, how to
change a default (links to Units of work and Rates by role for an
administrator only; a user reads "Your administrator changes these
defaults.") and the exports. `RoiHowComputed` ends with "Read the
methodology" (28 px link, a router `Link`, a plain base-path link where no
router is mounted). 37 keys in 4 locales. Tests:
`pages/value-methodology.test.tsx`.

### 10.7 The Value page (R5, UI)

Route `/value`, one main navigation entry "Value" (icon `TrendingUp`),
visible to every user in both modes, directly under the home entry, above the
work sections. "Value" is the word in both modes ("ROI" names one of six
figures, "Impact" overlaps Operations). It is the single nav change allowed
before the left-menu program (cca585ad): `nav-model.ts` marks it so with a
comment naming #9836, and a nav-model test asserts its position.

```
Value                                      [ Last 30 days v ] [ Export v ]
What you did             [ Mine | Whole organization ]      [ Methodology ]
(the scope line names the selected scope: "Whole organization" when it is)
+----------------------+----------------------+----------------------+
| Hours saved      (i) | Value            (i) | AI cost          (i) |
| 128 h                | 12,160 EUR           | 410 EUR              |
| 96 to 170 h          | 9,120 to 16,150 EUR  | Measured             |
+----------------------+----------------------+----------------------+
| Net value        (i) | ROI              (i) | Payback          (i) |
| 11,750 EUR           | 28.7 x               | Under a month        |
| 8,710 to 15,740 EUR  | 21.2 to 38.4 x       | Setup cost 0 EUR     |
+----------------------+----------------------+----------------------+
Reviewed: 148 of 239 runs (62%) [ Review the rest ]
          work items: 31 of 40 (78%) [ Open the work items ]
[ trend: value and net value per week, line chart, the range shaded ]
By fleet | By agent | By unit of work           (DetailTabs, URL-backed)
DataTable: name, runs, units, hours saved, value, AI cost, net value,
reviewed %   (search, filter, sort; row click opens the agent or fleet ROI
view; the (i) of a cell opens 10.6 without opening the row)
```

- Hero grid (round 1 B1, measured on the round 2 mockups): 2 x 3 below
  1280 px of viewport, 3 x 2 from 1280, six across from 1760. With 240 px
  navigation, 24 px page padding, 16 px gaps and 17 px card padding, 3 x 2
  at 1024 leaves a 200 px content box, which the probe found too tight for
  fr "Moins d'un mois" (219 px with system fonts) and en "Under a month"
  (202 px); 2 x 3 at 1024 leaves 315 px and 3 x 2 at 1440 leaves 339 px,
  both clean. Six across gives 145 px at 1440 and 171 px at 1600 and only
  clears 210 px from 1760. The value never wraps; the caption may wrap to
  two lines and the cards of a row share the tallest height.
- Costs masked (`cost_masked`): Hours saved and Value stay; AI cost, Net
  value, ROI and Payback are replaced by one card, value "Hidden", caption
  "Costs hidden on this deployment", an InfoTip with the reason as it is
  (round 2 B3): "Cost data is hidden on this deployment; an administrator
  sets it at install. The value is shown before AI cost." No link: there is
  no setting screen (the deployment variable of 10.2), and this design adds
  none. The sentence is never the 24 px value. The three cards fill one
  row at every width.
- Window: the `PERIODS` of `pages/analytics/constants` plus "This quarter"
  and "All time"; one `window` URL parameter shared with the other ROI views.
- Scope (round 2 B2): a segmented "Mine | Whole organization" control, and
  the line beside it names the selected scope: "What you did"
  (fr "Ce que vous avez fait") on Mine, "Whole organization" (fr
  "Toute l'organisation") on the organization view. An administrator gets
  the control, defaulted to Whole organization. A non-administrator gets it
  when `roi_org_totals_visible` is on (totals and by-unit rows, the other
  rows folded, 10.2), defaulted to Mine; with the setting off he sees Mine
  only, no control. The choice is the `scope` URL parameter.
- Tables add up to the hero (round 2 B1): for the same scope and window, the
  rows of each tab sum to the hero's hours, value, AI cost and net value
  (the report rule of 10.2; a UI test renders the fixture and checks the
  sums of the three tabs against the hero strings). The mockups use numbers
  that add up.
- Reviewed line (round 1 B5): the count and its link read the same scope and
  the same objects. Runs and work items are counted apart, each with the link
  of the list that shows them (Activity filtered on scope and window; Work
  items filtered the same). On "Whole organization" for a non-administrator
  the shares are shown without links, with an InfoTip "Totals of the whole
  organization; you can open only your own runs".
- Tables: "By fleet", "By agent" and "By unit of work" each keep their own
  URL keys (`fleet_q`, `agent_q`, `unit_q` with their sort and filter), so
  each survives a reload independently. The folded "Other agents and fleets"
  row (10.2) exists in By agent and By fleet only, never in By unit of work;
  it is plain text, no link, with a tooltip "Agents and fleets you cannot
  open, summed", and it always sorts last.
- Columns at 1024 (round 2 A5): every column of the three tables shows from
  1280; below 1280 `hideBelow` drops "Runs", "Units" and "AI cost" (the
  column picker brings them back), and "Name", "Hours saved", "Value",
  "Net value" and "Reviewed %" always show. Each table keeps the shared
  `DataTable` toolbar (search, filters, column picker).
- Trend: one line chart, value and net value, the range shaded with a legend
  ("Range: low to high estimate"), fixed height 200 px, no wheel zoom
  (Standards s8). Fewer than two points: "Not enough history yet" in place
  of the chart.
- Wording by mode (mode-split rules): XTM One mode "Value of your exposure
  management", unit names of the security catalog; Copilot mode "Value of
  your agents", the corporate catalog.
- States: loading `Skeleton` of the layout; error one `role="alert"` line
  "The value report could not be read." with Retry; nothing in the window
  "Nothing was counted in this period." with "Widen the window".
- Nothing declared: `EmptyState` "No agent is measured yet." For a user who
  can edit agents: "Pick a unit on an agent", which opens `/agents` filtered
  on the agents he can edit, plus the suggestions of 10.3 for his custom
  agents. For a user who can edit none: "Your administrator or an agent's
  owner declares what an agent produces", no button.
- XTM One mode with CTEM fleets off: never an upsell. The empty state says
  the value comes from agent runs and the built-in automations, with one
  compact "Set up a fleet" row under it.
- Community Edition: the shared `EnterpriseUpsell*` in place of the page,
  gated by `useEnterpriseLockedReason()`, per b3cd836b, and the nav entry
  locked exactly like the other Enterprise entries of `Sidebar.tsx`; never a
  dead end, never a second treatment.
- The Analytics Outcomes tab (O8a) stays for administrators and links to
  `/value`; its figures are unchanged (fixture).

As built (R5a, backend): the page reads one `GET /roi` answer. The report
now carries `units` (one row per unit of work, the shape of an agent row, `id`
the unit key, highest net value first; the rows sum to `organization` like the
agent rows) and `trend` (one point per week, Monday 00:00 UTC, oldest first:
`week`, `value`, `value_range`, `net_value`, `net_value_range`, `runs`; its
values sum to the hero's). `window=quarter` is the current calendar quarter so
far (payback over the days elapsed), next to `7d`, `30d`, `90d` and `all`.
With `cost_masked`, the unit rows mask their cost figures and the trend's net
value is `null`, never 0. The UI of 10.7 is slice R5b.

**As built (R5b, #9836).** `frontend/src/pages/Value.tsx` at `/value/:tab?` (tabs `fleets`, `agents`, `units`), every user, both modes; one `GET /roi` read through `useValueReport` (`scope=platform` for "Whole organization", `scope=mine` for "Mine"), so the hero and the tables never differ from another screen for the same scope and window. The arithmetic lives in `lib/roi-value.ts` (pure, `roi-value.test.ts`): each tab's rows plus its remainders ("Agents outside a fleet" in By fleet, "Other agents and fleets" for the folded rows of a non-administrator) add up to the hero; `roi_report.fold_outside` folds "Agents outside a fleet" too, so a non-administrator's row holds only the fleetless records of agents in their scope (Sam, 15 Nov 2026); a masked cost stays `null` in every sum (three hero cards, no net line on the trend). The scope switch follows `valueScopes(isAdmin, roi_org_totals_visible)` (administrator: both, organization first; others: both while the setting is on, their own first; else theirs only). Window `quarter` added to the select; the Analytics Outcomes tab keeps its four windows and links to the page. The nav entry "Value" sits under Dashboard (`requiresLicense`: locked on Community Edition only, never by the AI switch, `nav-model.test.ts`); on Community Edition the page shows the Enterprise upgrade path. The `cost_masked` state is reviewed on the lab service `lab-roi-cost-masked` (`.xtm-one/checks.toml`: the ROI records seeded with `OBSERVABILITY_COST_DATA_ENABLED=false`). Not in R5b: the home card and the list columns (R6).

### 10.8 Every other surface (R6, UI)

One name per figure, on every surface and in every export (round 1 B6):

| Figure | en | fr | Meaning |
|---|---|---|---|
| hours | Hours saved | Heures économisées | units x (manual - review) minutes |
| value | Value | Valeur | hours x rate, before AI cost (gross) |
| cost | AI cost | Coût IA | measured model usage |
| net | Net value | Valeur nette | value - AI cost |
| roi | ROI | ROI | net value / AI cost |
| payback | Payback | Amortissement | setup cost / net value per month |

"Value" alone is only ever the gross value; a surface that shows the net
value says "Net value".

- Home dashboard: a card titled "Value" (both modes) with two labelled
  figures, "Hours saved" and "Net value", for the user's scope over 30 days,
  the info button of 10.6 beside each, and a "See the value" link to
  `/value`; the card reads the same hook and `RoiFigure` (layout `card`).
  Costs masked: "Hours saved" and "Value" instead.
- Agent and fleet lists: a sortable "Net value (30d)" `DataTable` column
  (`RoiFigure` layout `inline`, no range in the cell, the range in the
  popover), from one batched read `GET /roi/summary?ids=` (scope-checked per
  id, no N+1). Nothing declared: "-" with the tooltip "No unit of work
  declared" and, for a user who can edit that agent, a "Declare a unit" link
  to its Outcomes tab (the link stops the row click). Costs masked: the
  column is "Value (30d)" (gross) and its header InfoTip says costs are
  hidden.
- Fleet About section and the fleet PDF datasheet (4597792d): "Hours saved",
  "Value" and "Net value", and the methodology line.
- Command center tile and agent KPIs: unchanged layout, rendered through
  `RoiFigure` with the names of the table above.

**As built (R6a, #9836).** `GET /api/v1/roi/summary?subject_type=agent|fleet&ids=<id>&ids=<id>&window=` (every signed-in user) answers one page of the agent or fleet list (at most 200 ids, 422 above) in a fixed number of reads whatever the page size (no read per row): one records query for every id, the pricing (`roi_report.load_pricing`, the one `load_report` now uses too) and the declarations of the page. Each id outside `roi_access.reader_scope` is left out of `items`, never refused one by one; a duplicate id is answered once. Each item is `{id, declared, runs, units, hours_saved, value, llm_cost, net_value, roi, range: {hours_saved, value, net_value, roi}, unreviewed}` and equals `GET /roi?scope=agent|fleet&id=` of that id for the same window (`summarize` groups by the record's agent, or its home fleet, as the scoped report filters; `test_roi_summary_9836.py`). An id with no record in the window answers `declared: false` unless it declares a unit (a declaration row, a built-in agent's shipped unit, a legacy `x-xtm-roi`; a fleet also through an agent member that declares one), in which case it answers its zero figures. `cost_masked` follows `OBSERVABILITY_COST_DATA_ENABLED`: AI cost, net value and ROI `null` with their ranges, never 0. No run, no model call. The UI of 10.8 (home card, list columns, fleet About and datasheet) is slice R6b.

**As built (R6b, #9836).** The Copilot home dashboard gains an eleventh grid widget, `value` (`COPILOT_WIDGETS` and `services/dashboard_layouts.py`; default place under every other widget, so a saved layout gains it without a move): `components/roi/RoiValueCard.tsx` shows "Hours saved" and "Net value" ("Value" when costs are masked) over 30 days through `RoiFigure` layout `card`, for the Value page's opening scope (whole organization for an administrator, "What you did" otherwise) from the same `useValueReport` cache entry, so the card and `/value` never differ; "See the value" opens `/value`; Community Edition says where Value lives. The agent list (list view) gains the sortable "Net value (30d)" column ("Value (30d)" when costs are masked): `useRoiSummary` reads `GET /roi/summary` once per chunk of at most 200 ids (sorted, deduplicated, never per row), `components/roi/RoiListCell.tsx` draws `RoiFigure` layout `inline` with `hideRange` (the range stays in the popover); nothing declared shows "-" with "No unit of work declared" and, for a reader who may manage the agent, "Declare a unit" to its Outcomes picker (an `interactive` cell, the row click is not fired); a row with no figure sorts after every figure. Tests: `lib/roi-summary.test.ts` (same numbers as the summary, masked never 0, chunks), `components/roi/roi-list-cell.test.tsx`, `Dashboard.test.tsx` and `copilot-dashboard-layout.test.ts` (eleven widgets). The fleet list column, the fleet About section and the PDF datasheet are slice R6c; the CTEM command center keeps its tile.

**As built (R6b, UX round 1 fixes, #10107).** A `GET /roi/summary` row now carries what the cell's "How this is computed" writes out, built from `figures_dict` (and `_masked` when costs are masked) so a row says what the scoped report says: `reviewed`, `unreviewed`, `rejected`, `work_items`, `unreviewed_work_items`, `review_rate`, `computation` and `assumption_facts` (`roi_report.SUMMARY_FIELDS`). The list popover shows the formula with its numbers, the Inputs with their label and source, and a true "Not counted" line, the same as on /value (test: `test_a_summary_row_carries_the_popover_inputs_of_the_scoped_report`, `roi-list-cell.test.tsx`). The home card's "See the value" is the siblings' 28 px header action with ArrowUpRight, and its scope line is 12 px.

**As built (R6c, #9836).** The Fleets list is a card grid, so the "Net value (30d)" column is a value line on each card (`FleetListCard`, `fleet-card-value`): the same `RoiListCell` as the agent list, fed by one `useRoiSummary("fleet", ids)` read for the filtered fleets (never one read per card), "Value (30d)" when the reader's costs are masked, absent on Community Edition. The list's Sort bar gains "Net value (30d)" (`?sort=value`, `sortFleets` with a value reader: highest first, a fleet without a figure last, ties by name; hidden on Community Edition). The fleet About hero gains a fourth figure: the shared `RoiFigure` (card layout, its range and "How this is computed") on the fleet's own report (`useFleetRoiReport`, the ROI view's cache entry), so the About and the ROI view never differ; refused or on Community Edition the hero keeps its three figures. The PDF datasheet's value figures move to R6d (server render, backend).

**As built (R6d, #9836).** The fleet PDF datasheet (`GET /fleets/{id}/documentation/datasheet.pdf`, `services/fleet_datasheet.py`) gains a full-width section "Value, last 30 days": "Hours saved", "Value" and "Net value", each with its low to high range, the runs and units counted, and the methodology line (`VALUE_METHODOLOGY`: the formula in words, where every input and its source is shown in the product). The figures are the fleet's own report (`roi_report.load_report(scope=fleet, window=30d)`, the read behind `GET /roi?scope=fleet&id=`, the fleet card value line and the About hero), read by the route for a reader who already sees the fleet, so the PDF never differs from the screen for the same window. Costs masked exactly when that report masks them (`report.cost_masked`, never the `OBSERVABILITY_COST_DATA_ENABLED` setting alone, so an administrator's sheet shows what his About hero shows): "Net value" reads "Hidden by your administrator" with no range, never 0, the gross "Value" stays. Hours round half up from the decimal value (2.55 h prints "2.6 h", as the screens), the figure and both range ends. Compact from 1,000 as the screens (#10476, `compact_figure`, the rules of `frontend/src/lib/number-format.ts`): amounts in whole units then "1.6K EUR", "-40.5K USD", hours "1.2K h", the runs and units counted and the quota estimate "4.2K runs", "40 to 2.5K runs a month"; 999,950 reads "1M", never "1000K"; the page is printed, so the exact figures stay in the fleet's ROI report and its CSV export. Nothing counted: "No unit of work counted in the last 30 days."; a failed read prints that the value could not be read, never 0, and never fails the export. No model call, no quota; the page stays one A4 page (`first_page_only`). Tests: `test_fleet_datasheet_value_9836.py`.

### 10.9 Slices

| Slice | Content | Acceptance |
|---|---|---|
| R1 (backend) | `roi_access.reader_scope`, `scope=mine`, agent scope opened to the agent's readers, `roi_org_totals_visible` (migration, `GET`/`PUT /roi/settings`, audit), platform totals for non-admins with out-of-scope rows folded, cost masking in the report | Non-admin reads `mine` and own agents, 403 elsewhere; record counted once; admin figures unchanged (fixture) |
| R2 (backend) | `BUILTIN_ROI_EXEMPT`, completeness test over agents and assignments, new XTM One units (minutes from the Analyst research or labelled assumption), stage analyst declarations, no-double-count test, `roi_suggest` + `GET /roi/suggest` | Every seeded fleet non-zero after one run; Copilot seeds unchanged |
| R3 (backend + UI) | Default / Customized / Reset on units, baselines, rates and built-in declarations; a customization is a row the seed never writes. R3a backend (origin fields), R3b UI (Origin column, Compare, Reset) | Customization survives a reseed and moves the figure |
| R4 (UI) | `RoiFigure`, `useRoiReport`, "How this is computed" popover, help bubbles; existing views migrated | Same strings on every surface for one fixture; popover arithmetic test |
| R5 (UI) | `/value` page, nav entry, scope line and toggle, trend, three tables, CE upsell | UX APPROVE, both labs, non-admin and admin |
| R6 (UI) | Home card, list columns with `GET /roi/summary`, fleet About and datasheet | UX APPROVE |
| R7 (docs + UI) | `/value/methodology`, `dev-docs/roi-methodology.md`, exports with methodology and assumptions. R7a backend + docs (exports, methodology doc), R7b UI (page, popover link) | Export carries the assumptions; page reachable from every popover |

Mockups for the UX round (round 2, after the round 1 verdict on bdd1d804),
rendered from 10.7 and 10.8 as HTML at the real dimensions (240 px
navigation, 24 px page padding, 16 px gaps, 24 px figure, 12 px caption),
run through the overflow probe and attached to the ux:xtm-one-9838 item: the
Value page for an administrator, for a non-administrator on "Whole
organization" and with costs masked; the popover; the Default / Customized
row of Settings > Units of work; the home card and the list column. Each
composite carries dark and light, en and fr, at 1440 and 1024.

## 11. Chat deliverables, counted automatically on the low side (#10544)

> "Where it shows" is superseded by section 12 (#12077): chat value and chat
> cost are one source line, "Chat (estimated)", of the same hero (12.6), in
> every scope; the chat lines of `GET /roi/chat` show the viewer's own lines
> only (every line for an administrator). The counting rules below (what
> counts, use replaces confirmation, how much) are unchanged. The as-built
> state is 12.14.

Decision of 10 Nov 2026 (option A): work an agent delivers in a conversation
counts in ROI with no mark, no chat card, no notification and no review, and
is deliberately undercounted. It replaces D2 = B (section 8) for chat only;
automation counting is unchanged.

- **What counts.** One record per finished deliverable (presentation, report,
  video, generated document file), keyed on the deliverable's own id
  (`roi_chat.record_key`): iterations update the same record. A message, a
  quick answer, an email or a lookup is never a record.
- **Use replaces confirmation.** A record counts once it carries a use signal
  (`exported`, `shared`, `public_link`, `presented`, `sent`, `attached`) or is
  still there 7 days after its last edit (`roi_chat.is_counted`). Deleted
  before that, it counts nothing.
- **How much.** The LOW bound of the kind's seeded unit (`executive_deck_built`
  10 slides, `consolidated_report_built` 10 pages, `decision_brief_written` 2
  pages, `video_produced` 2 minutes) scaled by the deliverable's size, minus
  the person's measured time in the conversation (gaps between their messages,
  each capped at 10 min) plus 25%, capped per kind (24 h; 8 h for a document
  file), floored at 0 (`roi_chat.estimate`). Worked example: a 30-slide deck
  is 20 h low by hand; 3 h of measured user time subtracts 3.75 h; 16.25 h
  count.
- **Where it shows.** A separate "From chat, estimated" series on the Value
  page and the fleet and agent ROI, attributed to the person and the agent,
  never inside a fleet's automation count; the person of a line or an
  administrator may raise or lower it from the ROI page (Sam, 15 Nov 2026:
  formerly anyone), and agent tools read and adjust records with the same
  permissions, logged. Nothing is added to the chat surface.

| Slice | Content |
|---|---|
| S1 | `services/roi_chat.py` rules and the four seeded units, unit tests |
| S2 | `roi_chat_records` storage and migration, recorder hooks on creation and use events |
| S2b | `exported` on deck and report exports (UI and agent tools), deletion stamps on decks and videos |
| S2c | `shared` in `presentation_sharing.apply_sharing` (a deck or video gains a person, a group or the company), `presented` on poll control, generated PDF/Word files of `generate_file` recorded as `file` (pages = source chars / 3000, at least 1; a re-render under the same name moves the record), `attached` on a generated document resolved outbound by `FileContext.resolve_upload` |
| S3 | `services/roi_chat_backfill.py`: one pass over past chat decks, reports, videos and generated PDF/Word files (no run, a conversation), `GET /api/v1/roi/chat/backfill-preview` (dry run, counts per kind) then `POST /api/v1/roi/chat/backfill` (administrators, audited, idempotent: `ON CONFLICT (record_key) DO NOTHING`, a hook's record never touched). Past signals = what the database still shows (a public link, a grant to a person, a group or the company); otherwise a deliverable counts once settled. Past documents size at one page (their source is not stored); a same-name rewrite is one deliverable on its latest file. Rows carry `backfilled_at` and their deliverable's dates |
| S4 | `services/roi_chat_series.py`: the "From chat, estimated" series applied on read (`roi_chat.is_counted`, `roi_chat.estimate` with the measured user time of the conversation, priced with `roi_report.load_pricing` at the unit's rate role; a person's `adjusted_min` replaces the estimate of a counted line, a pending line counts 0). `GET /api/v1/roi/chat?window=&currency=&agent_id=&limit=&offset=` (totals, by kind, a page of lines, the method) and `PUT /api/v1/roi/chat/records/{id}` (`{"minutes": N}` or `null` to return to the estimate, 0 to 43,200, audited `roi_chat_record`). Who reads a line corrects it: an administrator every line, anyone else only the lines attributed to them (seeing the agent, shared or company-managed, opens nothing: Sam, 15 Nov 2026); another line is a 404. A non-administrator's organization read (no `mine`, agent or fleet; 403 while `roi_org_totals_visible` is off) answers `aggregate_only: true`: the totals and the counts by kind, no line. Each line carries `can_adjust`. Agent tools `get_roi_chat_series` and `adjust_roi_chat_line` in the `roi` suite, same rules, audited `via: agent`. Nothing reads or writes `roi_run_records`: fleet totals never move with a chat record. |
| S5 | `components/roi/RoiChatSeriesCard.tsx` (hook `hooks/api/useRoiChatApi.ts`): one "From chat, estimated" card under the automation figures of the Value page (scope `mine=true` or the organization), the fleet ROI view (`fleet_id`: the chat records stamped with that fleet, `roi_chat_records.fleet_id`, the ledger's attribution of 12.3; before #12077 the fleet's member agents of today, which made the card count chat the fleet hero put in "No fleet") and the agent's Value tab (`agent_id`; the agent Overview before #11037), never added to them. Totals (counted, waiting for use, hours, value, compact with the exact figure on hover), one row per deliverable (kind and size, agent, person, status Waiting for use / Counted / Corrected, hours, value), searched and filtered by kind and status. "Adjust" on a counted line opens a dialog in hours (0 to 720) with the estimate's computation and "Back to the estimate" on a corrected line; nothing is added to the chat surface. `GET /api/v1/roi/chat` gains `fleet_id`, `mine`, and `agent_name` / `user_name` on each line of the page. UX gate |

## 12. One ledger, one attribution, one figure everywhere (#12077)

Sam, 15 Nov 2026: "nothing is consistent... it does not count only my
trace, my chat and my runs, even on shared agents... full audit, full
transformation, best in class practice for anonymity and showing the value
of the org, no leak, and above all consistency across multiple views,
multiple profiles, usage of shared agents."

This section supersedes 10.2 (access scope) and 11 ("Where it shows")
wherever they conflict; 10.3 to 10.8 keep their screens, which 12.9 re-points
at the ledger. The leak stop (item dev:roi-value-leak-stop) ships first and
is not repeated here; this section builds on main after it.

### 12.1 Audit (main 03287078, 15 Nov 2026)

Live, a non-administrator, Value page, 30 days: Mine shows 45.8 h, value
EUR 1.4K, AI cost EUR 12.1K, net -EUR 10.7K, ROI -0.9x; Whole organization
shows AI cost EUR 121K and net EUR 110K; its "Agents outside a fleet" row costs
EUR 42.3K there and EUR 11.7K on Mine; the chat card is the same on both.

| # | Defect | Where (main 03287078) | Effect |
|---|---|---|---|
| A1 | "Mine" is "the agents and fleets I can see", not "what I did" | `roi_access.reader_scope` (own, shared, company-managed through `user_visible_filter`); `roi_report.load_report` 484-496 selects every record of those agents; 524-526 passes `scope_agents = reader.agent_ids | fleet_member_agents(...)` to `price_rows` | A user carries every colleague's run of a company-managed or shared agent, as value and as cost |
| A2 | Cost is an agent's spend spread pro rata over its records | `roi_spend.agent_spend` (breakdown `keys=["agent"]`, read as administrator), `roi_spend.allocate` (share = spend / records of the agent, every scope) | A record's cost is not the cost of its run; one heavy run and nine light ones cost the same |
| A3 | Cost-only rows dump an agent's whole spend on any reader of it | `roi_spend.allocate` 144-149: one row per agent of `scope_agents` with spend and no record | The -EUR 10.7K of the audit: colleagues' chat with company-managed agents lands on Mine |
| A4 | Chat is counted on another perimeter | `roi_chat_series.readable` (own lines OR any visible agent), `load_series` outside the hero, no AI cost; agentless usage is "not counted" by `agent_spend` | Value and cost of chat never meet; Mine and organization show the same chat card |
| A5 | Admin and non-admin organization totals take different paths | `roi_report.fold_outside` (non-admin only), `_masked` (876), `no_fleet` left unfolded | No guarantee that one scope gives one figure |
| A6 | Each surface has its own entry point | `load_report` (agent, fleet, platform, mine), `load_summary` (list column), `load_series` (chat), `/roi/export` (admin or fleet, no mine), `builtin_tools/roi.py` 582 and 649, `fleet_datasheet.py` 165, `fleets.py` 1809 | No test asserts that one record yields one figure everywhere |
| A7 | A value record names no person | `roi_run_records` has `agent_id`, `fleet_id`, `run_id`, `work_item_id`, no `user_id` | Mine cannot be "what I did" without a column |
| A8 | The person of a cost already exists and is not used | `llm_traces.attributed_user_id` / `attribution` and the same grain on `llm_daily_stats` (`services/tracing/attribution.py`: user, assignment, parent, conversation, agent_owner; else system, anonymous_web, unknown) | LLM Analytics and ROI disagree on who spent |
| A9 | No anonymity rule | names and titles on chat lines (leak stop), no minimum group size on any breakdown | A small fleet or agent reveals one person's activity |

### 12.2 Principles

1. **One fact, one person, one agent, one fleet.** Every unit of value and
   every unit of AI cost is a ledger fact attributed once.
2. **Cost is what was spent, where it was spent.** A fact's cost is its own
   traces' cost, never a share of an agent's spend.
3. **Mine is what I did**, on any agent; never another person's activity on an
   agent I can see.
4. **An object's view is that object's activity, by everyone**, in aggregate.
5. **The organization is one figure for everyone**; profiles differ only in
   breakdown depth.
6. **Nobody is identifiable** by a non-administrator: no name, email or title
   of another person, no group under k people.
7. **One function computes every figure**; surfaces format it, never recompute.

### 12.3 The ledger

Two fact sources, one shape (`LedgerFact`: `day`, `person_id | null`,
`attribution`, `agent_id | null`, `fleet_id | null`, `class`, `unit | null`,
`units`, `hours_low/likely/high`, `value_*`, `cost`, `reviewed`, `source_id`).

**Value facts** (unchanged tables, two new columns):

| Fact | Table | Person (first that names someone) | Agent | Fleet |
|---|---|---|---|---|
| Run record | `roi_run_records` (run_id) | `AssignmentRun.user_id` (the acting identity: who triggered, else run-as, else owner), else `Assignment.user_id` | `agent_id` | `fleet_id` (home fleet at run end, as today) |
| Work item record | `roi_run_records` (work_item_id) | `WorkItem.user_id` (the person the item works for) | `agent_id` | `fleet_id` |
| Chat deliverable | `roi_chat_records` | `user_id` (the conversation's person) | `agent_id` (null: general chat) | the agent's home fleet when the record is counted, stamped in a new `fleet_id` column |

A member's chat belongs to a fleet exactly when its record carries that
fleet's id: the hero, By fleet, the fleet view's "From chat, estimated" card
and its "How this is computed" popover all read that one attribution. The card
selects its lines by `roi_chat_records.fleet_id` (never by today's member
agents) and shows the ledger's chat totals of the scope (`GET /roi/chat`
`figures.totals`, the Chat line of By source), so a fleet's card, hero and
`by_fleet` row agree; chat of an agent with no home fleet when it was written
stays in "No fleet" on every view (#12077).

New columns on `roi_run_records`: `user_id` (UUID, nullable, indexed with
`created_at`) and `attribution` (String(20), the rule that named the person,
or `system` / `unknown`); written by `record_run_units` and
`record_work_item_units` through ONE helper `roi_ledger.person_of_run` /
`person_of_work_item`, which reuses the rule order of
`tracing/attribution.py` (no second rule set).

**Cost facts** (new table `roi_cost_facts`, one row per day, person,
attribution, agent, fleet and class):

- Built by the daily aggregator from `llm_traces` (`total_cost`,
  `attributed_user_id`, `attribution`, `agent_id`, `assignment_run_id`,
  `conversation_id`, `source`), the day closed once, so a later fleet move
  never rewrites history (the rule `roi_run_records.fleet_id` already
  follows). The open day is read live from `llm_traces` with the same query.
- `class`: `automation` (a trace of an assignment run or a work item:
  `assignment_run_id` set, sources assignment, work_item, webhook, flow),
  `chat` (agent-chat, general-chat, channel, web widget), `platform` (memory,
  evaluation, background: what LLM Analytics attributes as `system` or to a
  person with no run or conversation).
- `fleet_id`: the fleet of the run's record when the trace's run has one,
  else the agent's home fleet at aggregation (`fleets.home_fleets_of`).
- Cost masking (`observability_cost_data_enabled` off): the column is read
  as null by `figures`, once, for every scope (12.6).
- Built (L2): `roi_ledger.cost_facts(first, last)` reads the facts live
  from `llm_traces`; `roi_ledger.close_cost_days` writes a complete day once
  its `llm_daily_stats` rollup exists, from the trace maintenance right after
  the rollup, with the same query, the whole day in one transaction under a
  lock (one row per grain). A day whose traces no longer reach the rollup's
  count is closed from the rollup (`built_from` `rollup`, the agent's home
  fleet at closing); a day whose facts no longer sum to the rollup's cost (a
  repricing) is closed again. So a closed day always sums to what LLM
  Analytics shows for it. Sources map to classes: `assignment`, `flow` =
  automation; `agent-chat`, `general-chat` = chat; the rest = platform.

What disappears: `roi_spend.allocate`, cost-only rows, the pro-rata share,
`roi_run_records.llm_cost` as a figure source (kept for history, read by
nothing in the report).

Usage with no counted output is a cost fact with zero value: it is in the AI
cost of the person, agent and fleet that spent it, and nowhere else.

### 12.4 Scopes and depth

`roi_ledger.figures(db, scope, viewer, window, currency) -> Figures` is the
only function that computes a figure.

| Scope | Facts selected | Non-administrator | Administrator |
|---|---|---|---|
| `mine` | `person_id = viewer` (every agent, own, shared, company-managed, and agentless chat) | totals, by class, by unit, by agent, by fleet, by week; agents and fleets named because he used them | same |
| `agent:<id>` | `agent_id = id`, every person | open to whoever sees the agent; totals, by class, by unit, by week; no person; k rule (12.5) | same plus "By person" when the setting is on (12.5) |
| `fleet:<id>` | `fleet_id = id`, every person | open to whoever sees the fleet (`fleet_visible`); totals, by class, by unit, by agent of the fleet, by week; k rule | same plus "By person" when on |
| `organization` | every fact | totals identical to the administrator's; by class, by unit, by week; by agent and by fleet for the objects he sees, the rest in "Other agents and fleets"; k rule | every row named; "By person" when on |

Every fact lands in exactly one row of every breakdown, so each breakdown sums
to the hero without counting a fact twice. Each breakdown keeps its own
residual row, for the facts that lack its own dimension only:

| Breakdown | Residual row(s) | A fact with no person lands in | Suppressed row, non-administrator under k (12.5) |
|---|---|---|---|
| By source | no residual row (every fact has a class) | its class row | "Other sources"; when the suppression would leave one named row, the tab shows the hero alone (12.5) |
| By agent | "No agent (general chat)", "Other agents and fleets" (folded) | its agent's row | "Other agents and fleets" (the same row: agents he cannot see and agents under k) |
| By fleet | "No fleet", "Other agents and fleets" (folded) | its fleet's row ("No fleet" when it has none) | "Other agents and fleets" (the same row) |
| By unit of work | "No unit" (chat and platform usage) | its unit row | "Other units" |
| By week | no residual row (every fact has a day) | its week | no row: an under-k week is merged with the next week (12.5) |
| By person (administrator, setting on) | "No person" (`system`, `anonymous_web`, `unknown`) | "No person" | not applicable (administrators only, k rule off) |

"No person" is a row of By person only, never of another breakdown. It is one
shared label of the Value page and LLM Analytics (which today says "No user
account"); its tooltip holds one sentence per residual class present in the
row, from frontend i18n keys named after the classes of `RESIDUAL_NOTES`
(`app/services/tracing/attribution.py`), never backend English, on both
surfaces.

`scope=platform` stays an alias of `organization` for one release.

Built (L3): `roi_ledger.figures(db, scope=, viewer=, scope_id=, window=,
currency=)` loads every fact of the window (`roi_ledger.ledger_facts`: run and
work item records priced as `roi.compute_roi` prices a run, counted chat
deliverables with their correction, closed cost days, and the open days read
live from `llm_traces`) and calls `roi_figures.compute`, which reads no
database: the selection of 12.4, the hero, By source, By unit, By agent,
By fleet, By week and, for an administrator with the setting on, By person,
every fact in exactly one row of each. A non-administrator's organization and
fleet views fold the agents and fleets he does not see into `other`; Mine
names every agent he used. `cost_masked` makes every cost-derived figure
`None`. `since` is the day after the newest fact not attributed yet. The
record's own `llm_cost` is not read. The old entry points (`roi_report`,
`roi_chat_series`) still answer the surfaces: they move onto `figures` in L4
with the tripwire of 12.7.

### 12.5 Anonymity

- **No other person in a non-administrator answer.** No person id, name,
  email or deliverable title of anyone but the viewer, in the JSON as well as
  on screen (chat lines: the viewer's own only; titles: author and
  administrators only). Tested on the serialized answer (invariant f).
- **k-anonymity, k = 5 by default** (`PlatformSettings.roi_min_group_size`,
  2 to 50, administrators, audited). For a non-administrator, any group of a
  breakdown (agent, fleet, unit, week, class) backed by fewer than k distinct
  persons in the window is suppressed, and every suppressed row names what it
  holds in its own breakdown's words (12.4, last column): "Other sources",
  "Other agents and fleets", "Other units". The caption under the table reads
  "Groups of fewer than {k, plural, one {# person} other {# people}} are
  summed" (interpolated from the setting, never a literal 5). Persons are
  counted over value and cost facts together.
- **Complementary suppression.** When exactly one group of a breakdown is
  suppressed, the next smallest group is folded with it, so no group can be
  recovered by subtracting the visible rows from the total.
- **By source, one named row left.** By source has three rows: when the
  suppression plus its complement would leave a single named row next to
  "Other sources", that row equals the hero minus "Other sources" and tells
  nothing the hero does not; the tab then shows no table, the hero alone, and
  the line "Sources are not split: fewer than {k} people used some of them in
  the period."
- **By week.** A week is never folded into an "Other" row (a trend has no
  such point). A week backed by fewer than k people is merged with the next
  week (the last week of the window with the previous one), repeatedly until
  each point is backed by k people; the merged point is plotted at its first
  week and labelled with its range ("3 Nov to 16 Nov"), with the caption
  "Weeks of fewer than {k} people are merged with the next week." The trend
  keeps its total; when every week merges into one point, the trend shows
  that one point and the caption, never an empty chart.
- **Object views under k.** On an agent or fleet view backed by fewer than k
  people, a non-administrator sees his own share only ("Your use of this
  agent") and the line "Totals are shown once {k} people use it in the
  period" ({k} interpolated, plural form). The organization total is never
  suppressed (it is the whole organization); Mine is never suppressed (it is
  the viewer). Every figure the k rule can suppress has a named state on
  every surface that shows it (12.10, "Under-k states").
- **Administrators.** Every row named, the k rule off. "By person" (person,
  department, hours, value, AI cost, net) behind the organization setting
  `roi_by_person_visible` (default off); each read writes `log_audit`
  (action `read`, entity `roi_by_person`). Decision D12-1.
- **Same rule everywhere.** The suppression is applied inside `figures`, so
  the page, exports, agent tools and the datasheet cannot differ.

### 12.6 One total

- Hero = automation + chat + platform usage: hours, value, AI cost, net,
  ROI, payback over every fact of the scope.
- The breakdown "By source" is the first and default tab: three rows,
  "Automations", "Chat (estimated)" and "Other AI usage", columns hours,
  value, AI cost, net, summing to the hero. "Other AI usage" is cost only
  (platform calls the scope is attributed): its hours and value cells show
  "-" with a tooltip worded per scope: Mine "Platform calls attributed to
  you: cost only"; an agent, fleet or the organization "Platform calls
  attributed to this agent / this fleet / the organization: cost only".
  Chat cost now meets chat value. There is no separate
  source strip above the tabs (one breakdown, drawn once).
- `cost_masked` is one flag of `Figures`; every surface renders "Hidden"
  (10.5) from it; no surface masks on its own.
- The reviewed share counts run and work item facts of the same selection as
  the hero (12.4), never another object list.
- A fleet's value is measured from its last factory reset (#2580,
  `services/fleet_value_marks.py`). The reset stamps `fleets.value_measured_since`;
  from then on every read leaves out what that fleet did before the mark: its run,
  work item and chat records (by `fleet_id` and `created_at`) and the LLM spend of
  its home member agents (the ledger's cost facts, the report's spend read again
  from the mark, so no pre-reset cost-only row returns). It is applied where the
  facts are read (`roi_ledger.ledger_facts`, `roi_report.load_report` and
  `clip_spend_to_marks`, `roi_records.list_records`), so the fleet view, the
  fleet's row of the organization breakdown and the organization total exclude
  the same part; `reset_fleets` on a platform report counts the fleets measured
  since a reset inside the window. Nothing is deleted. A fleet report carries
  `value_measured_since` when the mark falls inside the window, drawn as
  "Measured since <date> (reset)". A fleet never reset (mark `NULL`) reads as
  before.

### 12.7 Invariants (tests/test_roi_ledger_invariants.py)

A seeded fixture: 7 people (one administrator), an own agent, a shared agent,
a company-managed agent, two fleets (one of 2 people, one of 6), agentless
chat, system traces, a deleted agent, records of every class over 5 weeks,
priced in EUR and with costs masked. Each invariant runs on the fixture and on
200 seeded random draws of it.

| Id | Invariant |
|---|---|
| a | the sum over people of Mine, plus the facts with no person (the "No person" row of By person), equals the organization total (hours, value, cost, net) |
| b | the organization totals are identical for the administrator and every non-administrator |
| c | every breakdown of every scope sums to its hero, folded, suppressed and unattributed rows included |
| d | agent X's figures are identical on the agent view, the list column, the administrator's by-agent row, the export and the `get_roi` tool |
| e | a run or chat of person P on a shared or company-managed agent changes P's Mine and nobody else's |
| f | no non-administrator answer (JSON, CSV, PDF text, tool result) contains another person's id, name, email or deliverable title |
| g | no group backed by fewer than k people is shown to a non-administrator, and no suppressed group is recoverable by subtraction, on every surface of 12.9 (Value page, agent and fleet views, list column cell, fleet datasheet, CSV / PDF export, built-in tools); each suppressed figure carries the named state of 12.10; each suppressed row carries the label of its breakdown (12.4: "Other sources", "Other agents and fleets", "Other units"), By source with one named row left shows the hero alone, and every By week point is backed by k people or more |
| h | every fact of a breakdown other than By person lands in exactly one of its rows (no fact counted twice, none dropped from its agent, fleet, unit or week row); "No person" appears in By person only |

A tripwire test (`tests/test_roi_ledger_tripwire.py`) lists every caller of
`roi_report.build_report`, `load_report`, `load_summary` and
`roi_chat_series.load_series` outside `roi_ledger`: a new caller fails it and
the list only shrinks. The report keys the Value page and the agent and fleet
views draw stay next to `figures` until L6 redraws those screens, so the list
is empty at the end of slice L6 (L4 split in L4a, the API and tools answering
`figures`, L4b, the datasheet and the chat totals, and L4c, the CSV / PDF
export with `mine`).

### 12.8 API

- `GET /roi?scope=mine|agent|fleet|organization&id=&window=&currency=`: one
  answer shape for every scope (`Figures`: `totals`, `by_source`, `by_unit`,
  `by_agent`, `by_fleet`, `by_week`, `by_person` (administrator, setting on),
  `suppressed` {groups, k}, `cost_masked`, `since`, `perimeter` sentence).
- `GET /roi/summary?subject_type=&ids=`: reads `figures` per id (list column).
- `GET /roi/export?scope=...&format=csv|pdf`: every scope for whoever reads
  it, `mine` included (A6 today: administrators and fleet readers only).
- `GET /roi/chat` keeps the lines (the viewer's own; every line for an
  administrator); its totals come from `figures(by_source.chat)`.
- Built-in tools `get_roi` and `get_roi_chat_series` call the same routes'
  service with the acting user as viewer (`agent_tool_admin_bypass` keeps its
  meaning).

Built (L4a): `GET /roi` answers `figures` (the shape above, `roi_ledger.figures`
for the caller's `roi_ledger.viewer_for`) next to the report keys, for every
scope; `GET /roi/summary` gives each item `totals` from
`roi_ledger.figures_for_subjects` (one load of the window's facts per page,
each entry `roi_figures.compute` of that object's scope); `get_roi_report`
answers `figures` too (the hero and By source alone on a wide scope).
Invariant (d) runs in the suite on the agent view, the list column and the
administrator's by-agent and by-fleet rows.

Built (L4b): the fleet datasheet's value section reads
`roi_ledger.figures(scope=fleet)` for the datasheet's reader
(`fleets._datasheet_value`, `fleet_datasheet.ledger_value_figures`): the
figures `GET /roi?scope=fleet&id=` answers under `figures.totals` for the same
user and window, chat included ("N chat deliverables" next to the runs and
units), costs masked as there; it leaves the tripwire's allow-list. `Totals`
carries `units` (a run's units as `roi.compute_roi` counts them: none rejected,
known unit). `GET /roi/chat` and `get_roi_chat_series` answer `figures`
(`roi_ledger.chat_figures`: the chat row of By source for the series' scope,
mine, agent, fleet or organization; `null` for an agent or fleet the caller
does not see) next to the lines and their own totals, which stay until the
chat card reads `figures` (L6). The export moves in L4c.

Built (L4c): `GET /roi/export?scope=&id=&format=csv|pdf&window=` renders
`roi_ledger.figures` for the caller (`services/roi_export.py`), the answer
`GET /roi` gives under `figures` for the same scope and window: the hero
(`total` line), then every row of By source, By agent, By fleet, By unit, By
week and, for an administrator with the setting on, By person, each section
summing to the hero with its residual and folded rows named as in 12.4
("Automations", "Chat (estimated)", "Other AI usage", "No agent (general
chat)", "No fleet", "No unit", "Other agents and fleets"); the `since` line,
the methodology and the assumptions of the units counted follow. The PDF is
the hero, the breakdowns (first 8 rows, the CSV carries the rest) and the
method page. Access is `GET /roi`'s rule: `mine` every user, `agent` and
`fleet` their readers, `organization` administrators and, while
`roi_org_totals_visible` is on, anyone. `fleet_id` alone still reads as
`scope=fleet&id=` (the fleet view's button). A masked cost reads "Hidden by
your administrator", never 0. The route leaves the tripwire's allow-list;
`roi_report.report_csv` and `report_markdown` stay for the report shape until
L6 drops it.

### 12.9 Surfaces

| Surface | Today | After |
|---|---|---|
| Value page Mine | `load_report(mine)` + chat card | `figures(mine)`; one hero; By source / fleet / agent / unit |
| Value page organization | `load_report(platform)` (+ fold for non-admin) + chat card | `figures(organization)` |
| Agent ROI tab | `load_report(agent)` + chat card | `figures(agent)` |
| Fleet ROI view | `load_report(fleet)` + chat card | `figures(fleet)` |
| List column | `load_summary` | `figures(agent|fleet).totals` |
| Home card | `GET /roi` | `figures(mine)` |
| Fleet datasheet | `load_report(fleet)` | `figures(fleet)` with the datasheet's reader |
| CSV / PDF export | `/roi/export` | `figures(scope)` |
| Built-in roi tools | `load_report`, `load_series` | `figures(scope)` |
| Analytics > Outcomes | its own read | link to `/value`; figures from `figures(organization)` |

### 12.10 Screens (for the UX Director)

Shared rules: the 10.7 hero grid and states; one hero; "By source" the first,
default tab (no source strip); the "since" line when history is partial;
suppressed and folded rows (named per breakdown, 12.4) plain text, last, with their tooltip; no person outside Mine for
a non-administrator. Every figure renders through `RoiFigure` in compact form
("EUR 1.4K", "45.8 h"), the exact value on hover and in exports; the
wireframes below show the compact form.

Checklist: every figure the k rule can suppress has a named state on every
surface that shows it.

Non-administrator, Mine:

```
Value                                         [ Last 30 days v ] [ Export v ]
Your use of AI                  [ Mine | Whole organization ]   [ Methodology ]
+------------------+------------------+------------------+
| Hours saved  (i) | Value        (i) | AI cost      (i) |
| 45.8 h           | EUR 1.4K         | EUR 310          |
| 34 to 61 h       | EUR 1.1K to 1.9K | Measured         |
+------------------+------------------+------------------+
| Net value    (i) | ROI          (i) | Payback      (i) |
| EUR 1.1K         | 3.5 x            | Under a month    |
+------------------+------------------+------------------+
[ trend ]
[By source] | By fleet | By agent | By unit of work
DataTable By source (default):  hours   value      AI cost   net
  Automations                    31.2 h  EUR 960    EUR 120   EUR 840
  Chat (estimated)               14.6 h  EUR 440    EUR 170   EUR 270
  Other AI usage                 -  (i)  -  (i)     EUR 20    -EUR 20
  (rows sum to the hero; (i) "Platform calls attributed to you: cost only")
DataTable By agent: agent, runs, chats, hours, value, AI cost, net
  (the agents you used, company-managed and shared ones included)
Your chat deliverables (titles, Adjust) ... only your own lines
```

Non-administrator, Whole organization:

```
Value                                         [ Last 30 days v ] [ Export v ]
Whole organization               [ Mine | Whole organization ]  [ Methodology ]
[ the same hero, the same figures an administrator sees ]
[By source] | By fleet | By agent | By unit of work
DataTable By fleet:
  CTI fleet  [owner marker]  ...
  Other agents and fleets    ... (i) "{n} agents and {m} fleets you cannot
                                     open, and groups of fewer than {k}
                                     people, summed" (ICU plurals)
  No fleet                   ...
  (no "No person" row: a fact with no person stays in its fleet's row or
   in "No fleet"; it counts in the hero)
```

Administrator, Whole organization:

```
[ the same hero ]
By source | By fleet | By agent | By unit of work | By person (setting on)
By person: person, department, hours, value, AI cost, net
  (i) "Visible to administrators; every read is audited"
  No person   ... (i) one sentence per residual class present
                      (system, anonymous_web, unknown; i18n keys)
Off: no tab; Settings > Value: "Show value by person to administrators"
```

Agent page, Value tab, non-administrator, agent used by 3 people (k = 5):

```
Value of this agent                                 [ Last 30 days v ]
Your use of this agent
[ hero over the viewer's own facts on this agent ]
(i) Totals are shown once {k} people use this agent in the period.
```

Agent page, 8 people: the agent's hero, by source, by unit, trend; no person.

Under-k states (non-administrator; {k} interpolated, ICU plural), one per
surface that can show a suppressed figure:

| Surface | Under k, viewer used it | Under k, viewer did not use it |
|---|---|---|
| Agent page Value tab | "Your use of this agent" hero + "Totals are shown once {k} people use this agent in the period." | No hero; empty state "Fewer than {k} people used this agent in the period: totals are hidden." |
| Fleet view Value | "Your use of this fleet" hero + "Totals are shown once {k} people use this fleet in the period." | No hero; "Fewer than {k} people used this fleet in the period: totals are hidden." |
| List column cell (agents, fleets) | "-" (sorts last, never mixed with totals), tooltip "Your use: {figure}; totals shown from {k} people" | "-" with tooltip "Fewer than {k} people: hidden" |
| Fleet datasheet | Value block replaced by "Fewer than {k} people used this fleet: value totals are hidden." | same |
| CSV / PDF export | Suppressed groups exported as one row named per breakdown (12.4: "Other sources", "Other agents and fleets", "Other units"; weeks merged as on screen), a footnote naming k; an under-k object scope exports the viewer's own facts only, with the same footnote | header row and the footnote only |
| Built-in tools | `suppressed` {groups, k} in the answer, the viewer's own share only | `suppressed` {k}, no totals |
| Breakdown rows (any scope) | the suppressed row named per breakdown (12.4): "Other sources", "Other agents and fleets", "Other units", with the k caption; By source with one named row left: hero alone and "Sources are not split: fewer than {k} people used some of them in the period." | same |
| Trend (By week) | under-k weeks merged with the next week, the point labelled with its range, caption "Weeks of fewer than {k} people are merged with the next week."; all weeks merged: one point and the caption | same |

Empty states: Mine with no fact "You have not used AI in this period." with
"Widen the window"; organization with no fact 10.7's; history partial:
"Counted per person since {date}. Earlier usage counts in the organization
total only." (12.11).

### 12.11 Migration and history

- `roi_run_records.user_id`, `attribution`: one Alembic migration, backfill
  in batches from `assignment_runs.user_id` (then `assignments.user_id`) and
  `work_items.user_id`. A record whose run and item are deleted (`run_id`
  null, item gone) gets `attribution = unknown`: it counts in the
  organization (By person: "No person"; its agent and fleet rows as usual),
  never in a Mine.
- `roi_chat_records.fleet_id`: backfilled from the agent's home fleet now,
  flagged `fleet_backfilled`.
- Slice L1 (as built): migration `l12077ledger1` adds the four columns
  (metadata-only); `roi_ledger.run_ledger_backfill` stamps the older rows in
  bounded batches from the trace maintenance, right after the trace
  attribution walk, every write a `WHERE` on what is still missing. The
  Settings > Value trigger with its dry-run count comes with L4
  (`run_ledger_backfill(dry_run=True)` already answers the count).
- `roi_cost_facts`: rebuilt from `llm_traces` within the trace retention and
  from `llm_daily_stats` (which carries `attributed_user_id`, `attribution`
  and `agent_id`) before it; days rebuilt from the rollup take the agent's
  current home fleet and `class` from `source`. Rollup days with no
  attribution (before the attribution backfill) count as "No person".
- `Figures.since`: the day after the newest fact of the window whose
  attribution is not stamped yet (`attribution` null, before the backfill
  reaches it; `roi_figures._since`), `null` when every fact of the window is
  stamped or that day is not after the window's first day; the page
  and the export state "since <date>" when it is set, so history that cannot
  be attributed is never presented as complete.
- Idempotent, resumable (a cursor row like `trace_attribution_backfill`),
  administrators trigger it from Settings > Value with a dry-run count first.

### 12.12 Decisions (decided 15 Nov 2026)

All four decided by Sam on 15 Nov 2026 as recommended; none is open, and
slices L1 to L6 build on them.

| Id | Question | Decision |
|---|---|---|
| D12-1 | Administrators get a "By person" tab? | Yes, behind `roi_by_person_visible`, default off, every read audited |
| D12-2 | k for non-administrators | 5, an administrator setting from 2 to 50 |
| D12-3 | AI cost counts every attributed usage (general chat and platform calls), not only agents with a unit | Yes: the cost a person or the organization really spent; "Other AI usage" names it |
| D12-4 | An agent's owner sees totals of his agent under k | No: the owner is a non-administrator like any other reader |

### 12.13 Slices

| Slice | Content | Invariants green |
|---|---|---|
| L1 | `roi_run_records.user_id` / `attribution`, `roi_chat_records.fleet_id`, writers through `person_of_run` / `person_of_work_item`, backfill | (none: data) |
| L2 | `roi_cost_facts`, aggregator step, live open day, rebuild from traces and rollup | (none: data) |
| L3 | `services/roi_ledger.py` `figures` over `services/roi_figures.py` (pure), scopes, sources, folding, masking, `since`; fixture and invariant suite (built; the old entry points move in L4) | a, b, c, e, h |
| L4a | `GET /roi` and `get_roi_report` answer `figures`, list column `totals` from `figures_for_subjects`, tripwire of 12.7 (built) | d |
| L4b | datasheet and chat totals (route and the chat series tool) on `figures` | d (datasheet) |
| L4c | CSV / PDF export on `figures`, every scope, `mine` included (built) | d (export) |
| L5 | k-anonymity, complementary suppression, object views under k, administrator By person behind its setting and audit (built) | f, g |
| L6 (UI) | L6a (built): Value page hero, By fleet / By agent / By unit of work and the weekly trend on `figures` (`GET /roi` `figures.names` names the rows from the export's labels, never a person), residual and folded rows as remainders, Export CSV / PDF of the selected scope (`mine` included). L6b1 (built): Settings > Organization > Rates by role, card "Value visibility and privacy" (`PUT /roi/settings`: organization totals for every user, the k of the anonymity rule 2 to 50, By person for administrators; read view at rest, one audit entry per field changed). L6b2 (built): cost masking identical on every scope and reader (`GET /roi` answers `cost_masked` true for the administrator's platform report, an agent's and a fleet's whenever costs are hidden, so the Value page draws the "Hidden" hero, invariant 6). L6b: "How this is computed" and the review line on `figures`, agent and fleet views, the By person tab, empty states, methodology page and docs | UX APPROVE as administrator and non-administrator |

**Built (L5).** `roi_figures.compute` applies the rule for a viewer whose
`min_group_size` is set (`roi_ledger.viewer_for`: non-administrators, from
`PlatformSettings.roi_min_group_size`, default 5, bounds 2 to 50; a failed read
answers the default, never the rule off). Organization, agent and fleet scopes;
Mine and administrators never. `suppress_rows` folds every under-k row of By
source, By unit, By agent and By fleet into `other_sources`, `other_units` or
`other` (residual rows included) with the complementary fold when exactly one
row is under k; By source collapses to one `other_sources` row when one named
row would be left; `merge_weeks` merges an under-k week with the next (the last
with the previous), the merged point keyed by its first week with `until`; an
agent or fleet under k answers the viewer's own facts. The answer carries
`suppressed` {k, groups, own_share_only, sources_split, weeks_merged}, `null`
when the rule did not apply. Persons are counted over value and cost facts
together. `GET/PUT /roi/settings` read and set `roi_min_group_size` and
`roi_by_person_visible` (administrators, one audit entry per field changed);
an answer carrying By person (`GET /roi`, `GET /roi/export`) writes
`log_audit` (action `read`, entity `roi_by_person`) and commits before the
response. `GET /roi/summary` reads the same viewer, so the list column equals
the agent view under k. Tests: `tests/test_roi_ledger_anonymity.py`
(invariants f and g, b, c and d under k = 2 to 6, every named case). The UI
states (captions with {k}, "Your use of this agent", merged week labels) are
L6.

### 12.14 Methodology as built (L7c, main fa8c4612, 16 Nov 2026)

The state of the code on main, for whoever reads a figure and asks how it
was made. It wins over 10.2, 10.6 (AI cost line) and 11 ("Where it shows");
the plain-language form is `dev-docs/roi-methodology.md`.

**Attribution chain.** Each fact names at most one person, through one rule
order (`tracing/attribution.py`, reused by `roi_ledger.person_of_run` and
`person_of_work_item`, never a second rule set):

| Fact | Person, first that names someone | `attribution` when none |
|---|---|---|
| Run record | the run's acting identity (`AssignmentRun.user_id`: who triggered, else run-as, else owner), else `Assignment.user_id` | `system` / `unknown` |
| Work item record | `WorkItem.user_id` | `unknown` |
| Chat deliverable | the conversation's person | (always a person) |
| Cost fact (a trace) | `llm_traces.attributed_user_id`: user, assignment run, parent trace, conversation, agent owner | `system`, `anonymous_web`, `unknown` |

A record whose run and item are deleted is `unknown`: it counts in the
organization, its agent and its fleet, in the "No person" row of By person,
and in nobody's Mine.

**Ledger tables.** Value: `roi_run_records` (`user_id`, `attribution`, L1,
migration `l12077ledger1`, backfilled by `run_ledger_backfill`) and
`roi_chat_records` (`fleet_id`, `fleet_backfilled`). Cost:
`roi_cost_facts`, one row per day, person, attribution, agent, fleet and
class (L2, migration `m12077costfct1`), a closed day written once by
`close_cost_days` and always summing to the `llm_daily_stats` rollup of that
day (LLM Analytics' figure); the open day read live by `cost_facts`.
`roi_run_records.llm_cost` is history only, read by no figure. The pro-rata
share (`roi_spend.allocate`) is still called by the old report
(`roi_report.py`) behind the report keys the tripwire allow-list names;
no `figures` value reads it.

**One function.** `roi_ledger.figures` (database) over `roi_figures.compute`
(pure) computes every figure: `GET /roi` (`figures`), `GET /roi/summary`
(`figures_for_subjects`), `GET /roi/chat` (`chat_figures`), `GET /roi/export`
(`services/roi_export.py`), the fleet datasheet, `get_roi_report` and
`get_roi_chat_series`. Surfaces format, never recompute.

**Sources.** By source has three rows summing to the hero: "Automations"
(run and work item records, and the cost of traces of `assignment` and
`flow` sources), "Chat (estimated)" (chat deliverables, and the cost of
`agent-chat` and `general-chat` traces) and "Other AI usage" (every other
attributed call: memory, evaluation, background; cost only, its hours and
value cells "-"). Decision D12-3: AI cost is every attributed usage, general
chat and platform calls included.

**Anonymity.** For a non-administrator on the organization, an agent or a
fleet: k = `PlatformSettings.roi_min_group_size`, default 5, bounds 2 to 50
(administrators set it in Settings > Organization > Rates by role, card
"Value visibility and privacy", one audit entry per field changed; a failed
read answers 5, never the rule off). Under-k rows fold into "Other sources",
"Other agents and fleets" or "Other units" with the complementary fold;
under-k weeks merge with the next; an agent or fleet under k answers the
viewer's own share. Mine and administrators are never suppressed; the
organization total never is.

**By person.** Administrators only, behind `roi_by_person_visible` (default
off, migration `n12077kanon1`); every answer that carries it (`GET /roi`,
`GET /roi/export`) writes `log_audit` (action `read`, entity
`roi_by_person`) before it responds. No non-administrator answer carries
another person's id, name, email or deliverable title.

**Cost masking.** `observability_cost_data_enabled` off sets
`Figures.cost_masked`: AI cost, net value, ROI and payback are `null` in
every scope and for every reader (L6b2, `0807fd10`), rendered "Hidden" ("Hidden
by your administrator" in the exports), never 0.

**History.** `Figures.since` (12.11) marks a window that starts before every
fact carries its attribution; rollup days with no attribution count as "No person".

**Invariants and where each is tested** (the fixture of 12.7, 200 seeded
draws each):

| Id | Test |
|---|---|
| a | `tests/test_roi_ledger_invariants.py::test_a_mine_of_every_person_plus_no_person_is_the_organization` |
| b | `test_roi_ledger_invariants.py::test_b_the_organization_totals_are_the_same_for_every_viewer`; under k: `test_roi_ledger_anonymity.py::test_b_the_organization_total_is_never_suppressed`; chat: `test_roi_ledger_chat_figures.py::test_the_organization_total_is_the_same_for_every_viewer` |
| c | `test_roi_ledger_invariants.py::test_c_every_breakdown_of_every_scope_sums_to_its_hero`; under k: `test_roi_ledger_anonymity.py::test_c_under_k_every_breakdown_still_sums_to_its_hero` |
| d | `test_roi_ledger_invariants.py::test_d_an_agent_s_figures_are_the_same_on_its_view_the_list_column_and_the_by_agent_row`; under k: `test_roi_ledger_anonymity.py::test_d_under_k_the_list_column_equals_the_object_view`; export: `tests/test_roi_export_ledger.py`; datasheet: `tests/test_fleet_datasheet_value_9836.py` |
| e | `test_roi_ledger_invariants.py::test_e_a_fact_on_a_shared_agent_moves_only_its_persons_mine` |
| f | `test_roi_ledger_anonymity.py::test_f_the_serialized_non_administrator_answer_names_nobody_else` |
| g | `test_roi_ledger_anonymity.py::test_g_no_group_under_k_is_shown_to_a_non_administrator` and the named cases of that file (complementary fold, By source not split, merged weeks, own share under k) |
| h | `test_roi_ledger_invariants.py::test_h_every_fact_lands_in_exactly_one_row_and_no_person_is_by_person_only` |

Also tested: costs masked answer `None`, never 0
(`test_costs_masked_answer_none_never_zero`), `since`
(`test_since_is_the_day_after_the_last_unattributed_fact`), the attribution
of run, work item and chat records and the backfill
(`tests/test_roi_ledger_postgres.py`), and the tripwire on the old entry
points (`tests/test_roi_ledger_tripwire.py`). The suite of every invariant as
one CI gate on every surface is slice L7d.
