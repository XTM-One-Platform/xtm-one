# CTEM Cases: open exposures grouped by shared remediation

Issue #11545 (work item dev:ctem-cases, proposal C of the exposure
prioritization program). Read on main `9ee057f3`. This document is slice S0:
the design the UX Director reviews before any code. Proposal A (the
prioritization funnel, Prioritization stage) and proposal B (#11543, assets,
exposure and prioritization policy in the organization profile) are built in
parallel; this design consumes their fields and never invents them.

## 1. Why

The Remediation stage (`/ctem/remediation`, labelled "Mobilization" in the
cyber vertical, `ctem_verticals.py`) shows facts (case closure, playbooks,
blocked simulations) and an AI assessment. Nothing turns the open exposures
into units of work: a team that patches one product version closes dozens of
findings at once, but the page counts findings, not fixes. Act now ranks what
to do first; no object follows a fix to closure with an owner, a due date and
a state.

Reference behaviour (exposure-management products, 2026): 17,000 open issues
sharing the same fix become 479 cases with four states (needs attention, in
progress, resolved, accepted risk).

## 2. Principles

1. **One case = one fix.** A case groups every open issue that one remediation
   closes. Two fixed versions of the same product are two cases.
2. **Nothing invented.** A case exists only when a source issue exists and the
   remediation key is read from the data (section 4). An issue whose fix is not
   known is never forced into a case: it is counted in "No known fix" on the
   list, with a link to the issue in its product.
3. **The user's work is never lost.** Recompute adds and removes member issues;
   state, owner, due date, notes, manual merges and splits and history stay.
4. **Every number traceable.** Each case lists its member issues with source
   (OpenCTI, OpenAEV), the id and a link to the object in its product.
5. **Additive.** Act now, Recommended actions and Done for you keep their
   behaviour; a case links to them and they link to cases, nothing is replaced.
   Existing widgets, saved layouts and agent tool contracts are unchanged.
6. No new drawer (Sam, 7 Nov 2026): a case opens a page with its own URL; bulk
   actions and the accepted-risk form are centred dialogs.
7. XTM One mode only. Light and dark, en / fr / es / ja, 1080 to 1728 wide,
   text >= 12 px, controls 36 px, keyboard reachable, exact counts (no "99+").
8. **Shared cases, per-viewer content: the source products are the only judge
   (#11613).** Cases (the fix and the workflow: state, owner, due date,
   history) are shared by the organization; their content is not. The
   database stores ids and workflow only: no name, CVE, score, KEV, asset,
   crown jewel, priority or count. Every read renders the case for the viewer
   from the viewer's OWN source read (their JWT, the same reads recompute
   makes; `services/ctem_cases_view.py`), cached per user and vertical for
   5 minutes. XTM One mirrors no group, role, marking, organization or scope.
   - A case with no issue the viewer sees is absent from their list and their
     counts (a count is data) and answers 404 (`No case` for a tool).
   - A member issue the viewer cannot see is a row `{"restricted": true}`,
     nothing else: no id, source, name, score, asset or link (OpenCTI's
     "Restricted" pattern). The member count shows the total and the
     restricted count; priority, scores, assets and crown jewels are
     computed on the visible issues only and may differ from one viewer to
     the next. A history line about an issue they cannot see shows
     "Restricted".
   - A product that does not answer: the viewer sees the cases they were
     shown before (ids kept 7 days, ids only) with their details marked
     unavailable, never empty and never someone else's.
   - An owner must see at least one issue of the case, checked with the
     owner's own read; an issue the actor cannot see cannot be split out.
   Audit of every CTEM surface: `ctem-data-segregation-audit.md`.

## 3. Information architecture

```
/ctem/remediation            Mobilization stage page
  └─ Cases board (signature viz, top of the page, above the AI assessment)
       needs attention -> in progress -> resolved | accepted risk
       each column: count, ageing bands, owners; "View all cases"
/ctem/cases                  Cases list (filters, bulk actions, export CSV)
/ctem/cases/:id              Case page (fix, issues, assets, history)
Command center               widget "cases" (counts by state, overdue, expiring)
/ctem/prioritization         funnel last layer "Cases" (proposal A) -> /ctem/cases
```

The board is the Mobilization entry of the per-stage signature viz system
(dev:ctem-stage-viz): one viz per stage, its own shape, the same tokens,
legends, tooltips (source and period) and click-through rule. The command
center widget is not the board in small: it answers "how many fixes are stuck"
in one line (section 7.3).

## 4. Grouping rule

### 4.1 Source issues (open only)

| Source | What counts as an open issue | Read |
|---|---|---|
| OpenCTI | A Vulnerability related by `has` to a Software observable matching the organization tech stack (the match already used by the tailored KEV, `ctem_metrics_collector._stack_matches`), and passing the prioritization policy of #11543 when one is set | GraphQL, per-user credentials (as the posture) |
| OpenAEV | A finding of type `cve` on an asset or asset group, not older than the validation window | `/api/findings/search`, per-user credentials |

Without a tech stack in the profile, OpenCTI contributes nothing (no case from
the whole vulnerability base: that would be a pass-through); the list says
"Add your tech stack in Settings > Organization to build cases from OpenCTI".

### 4.2 Remediation key

The key is computed per issue, first rule that answers wins:

| # | Rule | Key | Case title |
|---|---|---|---|
| R1 | The vulnerability has a Course of Action related by `remediates` or `mitigates` | `coa:<course-of-action standard id>` | the course of action name |
| R2 | The matched Software observable carries a version and the vulnerability states a fixed version for that product (CPE range end, `versionEndExcluding`, read from the vulnerability's external data when OpenCTI has it) | `upgrade:<vendor>/<product>@<fixed version>` | "Upgrade <vendor product> to <fixed version>" |
| R3 | An OpenAEV CVE finding whose CVE is an OpenCTI vulnerability keyed by R1 or R2 | the same key | joins that case |
| R4 | Nothing above | none | counted in "No known fix" |

Not verified yet (checked live in S1 before any grouping code): OpenCTI's
Vulnerability schema has no fixed-version attribute (`vulnerability.ts` on
master: CVSS, EPSS, KEV, SSVC, CWE, first seen active); R2 depends on the
connector data reaching OpenCTI (external references, CPE ranges in the
description, or a Software `version` with a `has` relation). If S1 finds no
reliable fixed version, R2 falls back to `upgrade:<vendor>/<product>@latest`
("Upgrade <vendor product> to a fixed version") and the case page says the
target version is not known.

Keys are normalized (lower case, trimmed, vendor folded to the stack entry).
The same vulnerability on two products yields two memberships, one per key.
A configuration change is R1 only (a course of action); it is never guessed.

### 4.2bis Fixed version and declared version (CTEM-11, #894, #946)

Affected and fixed versions are knowledge: they live in OpenCTI, XTM One
reads them (#946, which removed the range engine and the VulnCheck NVD 2.0
read of #895 and #932). The reader (`ctem_cases_source._read_opencti`) reads,
with the acting user's own OpenCTI credentials and nothing else:

- the stack's Software observables with their `version`
  (`_OCTI_STACK_SOFTWARE`);
- `Software has Vulnerability` from those nodes (`_OCTI_STACK_VULNS`);
- `Course-Of-Action remediates|mitigates Vulnerability` (`_OCTI_REMEDIATIONS`);
- `Software remediates Vulnerability` (`_OCTI_SOFTWARE_REMEDIATES`), the
  relation OpenCTI models between a Software version and the vulnerability
  it fixes.

`opencti_issues` then fills, per (vulnerability, stack product):

1. the fixed version: the smallest version of a Software of that product
   that `remediates` the vulnerability, above the declared version
   (`ctem_fixed_version.smallest_fix_above`);
2. else the version a course of action names when it is a plain upgrade
   ("Upgrade to 9.0.98", `version_from_course_of_action`): the case stays
   R1, its `fixed_version` is the named version;
3. else nothing: the member says "fixed version unknown" and the key stays
   `@latest`.

A tech stack entry takes an optional `version` (the version the organization
runs). The declared version is the Software node of that product whose
`version` equals it (`1.2` equals `1.2.0`) or whose CPE equals the entry's.
With it:

- a vulnerability that node has no `has` to is "not affected: version <v>
  has no 'has' relation to <CVE> in OpenCTI": it opens no case, is counted
  `not_affected` and listed in the recompute answer (`not_affected_issues`,
  at most 50), and an existing member that turns not affected is closed with
  that reason as evidence;
- when OpenCTI holds no Software of the declared version, nothing is marked
  not affected and every member says "version not known to OpenCTI: <v>";
- the upgrade members of one product fold onto ONE key, the smallest version
  that fixes every member (the greatest of their fixed versions), titled
  "Upgrade <vendor product> to <version>";
- that folded key moves when a member with a higher fix appears, when the
  member that set it is patched, or the first time a version is declared on an
  `@latest` case: recompute never opens a second case then. The existing case
  of the same `upgrade:<vendor>/<product>@` that holds the most of the new
  key's members takes the new key (added to its `keys`, the old ones kept),
  its title and `fixed_version` follow, and a `retargeted` event records the
  key (`ctem_cases._holder_of`, `CasePlan.add_keys`).

What OpenCTI could not tell is listed in the recompute answer
(`version_notes`, at most 50), never guessed. Without a declared version
nothing changes (two fixed versions stay two cases, as section 2 says).
Versions compare part by part (numbers as numbers, `rc`/`beta`/`alpha`/`dev`
before the release, a post-release letter after it, an epoch `1:` and a `v`
prefix read); distribution backports are not visible.

### 4.3 Recompute

- Runs on demand: `POST /api/v1/ctem-cases/recompute` (the list's "Recompute"),
  any user, one per minute per user, no model call, no run, no quota
  (`services/ctem_cases_source.py`). It reads OpenCTI and OpenAEV with the
  caller's own credentials and MERGES into the shared cases (#11613): what the
  caller sees open is added or reopened; a member is closed only when the
  caller can still read it by id and it is no longer open; a member the
  caller cannot see is left as it is, so a narrower view never closes or
  shrinks a case. A read that hit its cap closes nothing. A product
  that cannot answer makes it write nothing (`applied: false`, the product in
  `missing`): an outage never resolves a case.
- Accepted risk expiry runs on the scheduler tick (`expire_acceptances`),
  no model call, not gated on AI.
- For each key: an existing case gets the new members the caller sees (history
  `issue_added`, the member id only); a new key creates a case in "needs
  attention"; a case whose members are all closed is moved to "resolved" with
  the system evidence "every member issue closed at source" (history line),
  unless it is in "accepted risk", which keeps its state until expiry.
- A resolved case whose issues return is reopened ("needs attention") with a
  history line "reopened: issues open again at source".
- State, owner, due date, notes and history are never written by recompute.
- Every case a recompute writes is worked by the loop (#1206): a new case
  raises `ctem_case.opened`, a reopen `ctem_case.reopened`, a resolve
  `ctem_case.resolved`, collected in `ctem_case_loop.PendingEvents` and
  published after the commit with the loop's stable event ids, so the fleets'
  case listeners start a run on a reader case exactly as on a signal case. A
  recompute that changes nothing raises nothing. The opening writes an
  `announced` case event; a manual split announces its new case the same way.
- Severity and SLA of a reader case (#1206): an open case takes the scope gate
  severity of the priority of the issues the read sees (critical P1 24 h, high
  P2 72 h, medium and low P3 30 days, `DEFAULT_SLA_HOURS`); a more urgent read
  escalates it and pulls the SLA earlier, a less urgent one never relaxes it.
- Backfill (#1206): the scheduled case sweep (`announce_unannounced`) raises
  `ctem_case.opened` once for every open case that holds neither an
  `announced` event nor a `created` event from a signal (the cases written
  before #1206), at most 200 per pass, and records `announced`, so it never
  repeats. No migration.
- Manual merge: the surviving case lists the merged keys; recompute maps all
  of them to it. Manual split: the user picks member issues; the new case
  carries a pinned key `split:<uuid>` that owns those issue ids; recompute
  keeps them there while they stay open.

## 5. Model (additive, reversible migration)

| Table | Columns |
|---|---|
| `ctem_cases` | id, organization_id, vertical, title, remediation_kind (`upgrade` / `patch` / `config` / `course_of_action` / `manual`), remediation_label, product_label, fixed_version, keys (list), pinned_issues, state, owner_user_id, due_at, accepted_reason, accepted_until, state_since, created_at, updated_at |
| `ctem_case_issues` | case_id, source (`opencti` / `openaev`), external_id, open |
| `ctem_case_events` | case_id, at, actor (user id or agent id, or `system`), kind (`created`, `state`, `owner`, `due`, `issue_added`, `issue_closed`, `issue_reopened`, `merge`, `split`, `note`), issue (member id, else null), from, to, reason, evidence |

No source content is stored (#11613): priority, counts, scores, assets, crown
jewels and names are computed per viewer at read time (principle 8).

The downgrade drops the three tables; nothing else changes.

Migration `ctemscope11613` (#11613) deletes the cases written before it
(built from one user's view and readable by all; the next recompute rebuilds
them), drops the content columns and adds `ctem_case_events.issue`. Its
downgrade deletes the shared rows and restores the columns.

## 6. States

```
           +--------------------+
  new ---> |  needs attention   | <--------------------------+
           +--------------------+                            |
              |  start (owner set)          expiry passed /  |
              v                             issues reappear  |
           +--------------------+                            |
           |    in progress     |                            |
           +--------------------+                            |
              |  resolve (evidence)        |  accept risk    |
              v                            v                 |
           +----------+          +-----------------------+   |
           | resolved |          |     accepted risk     |---+
           +----------+          +-----------------------+
```

| Transition | Requires | Who |
|---|---|---|
| any -> in progress | an owner | user, agent |
| any -> resolved | evidence: every member closed at source (system) or a reason with a reference (ticket, change, validation run) | system, user; an agent only with a reference |
| any -> accepted risk | owner, reason, expiry date (max from the policy of #11543, default 90 days) | user only |
| accepted risk -> needs attention | expiry passed (system) or a user | system, user |
| resolved -> needs attention | member issues open again (system) or a user | system, user |

Priority comes from the prioritization policy of #11543 (KEV always in, CVSS and
EPSS thresholds, internet exposure, crown jewel criticality). Without a policy
the priority is computed from KEV, max CVSS and max EPSS with the platform
defaults, labelled "default policy" in the case.

Due date: set by the user, or suggested from the priority SLA of the policy
(critical 7 d, high 30 d, medium 90 d, low none when no policy says otherwise).
"Overdue" and "Accepted risk expiring in 14 days" are computed, never stored.

## 7. Screens

### 7.0 Shared visual language (UX round 1 B4, B5, B7; round 2 B1)

- **One colour per state, everywhere** (board, list badge, case page, widget),
  through the DS `Chip` `severity` and its feedback tokens, never a hand chip:
  needs attention `medium` (alert), in progress `info`, mitigated, watching
  `low` with the DS `eye` icon as the Chip's own `startIcon` (inside the chip,
  same left edge and baseline as its siblings) and the
  `feedback-success-tertiary` segment on every counts bar (#857, UX rounds 1
  B3 and 2 B1: no free Chip severity, LIBRARY-FEEDBACK F06), resolved `low` (success,
  `feedback-success-primary` segment), accepted risk `neutral`. AI purple is never used on Cases (it is
  the AI colour only): crown jewels are a `neutral` Chip with the shield icon.
- **Owners** are the DS `Avatar` (initials, name in its tooltip); unassigned
  reads "Unassigned" in secondary text, never a warning colour.
- **Period switch**: the shared `SegmentedFilter` (`size="sm"`, radio). One
  period per page: the board's switch drives every period figure of the page.
- **Ageing bar** carries age bands only, one blue ramp from the DS palette,
  each band >= 3:1 against the surface it is drawn on, the board column, in
  both themes, 2 px surface gaps between bands so neighbours need no
  contrast with each other. The column surface is fixed per theme (round 2
  B1): dark, the column is layer 2 `#13213e` inside the layer 1 card; light,
  the column is `#ffffff` (layer 1) with the `border-subtle` outline, never
  the `#f4f4f6` layer 2 tint, on which the lightest band fell to 2.77:1.
  Ratios measured on those surfaces:

  | Band | Dark token (ratio on column #13213e) | Light token (ratio on column #ffffff) |
  |---|---|---|
  | 0-7 d | `--blue-700` #0079a8 (3.27:1) | `--blue-600` #009edb (3.04:1) |
  | 8-30 d | `--blue-600` #009edb (5.26:1) | `--blue-700` #0079a8 (4.88:1) |
  | 31-90 d | `--blue-400` #42caff (8.46:1) | `--blue-800` #005575 (8.22:1) |
  | > 90 d | `--blue-200` #a8e7ff (11.82:1) | `--darkblue-700` #0015a8 (12.55:1) |

  Older reads stronger in both themes. **Overdue is not a band**: it is a
  separate line under the bar (error icon in `feedback-error-primary` +
  "<n> overdue" in `text-feedback-error-tertiary`, which holds 4.5:1 on the
  dark column where the error primary measures 4.26:1, LIBRARY-FEEDBACK F48),
  because an overdue case can sit in any age band. The column surface and both
  line colours are held by `frontend/tests_unit/ctem-cases-board-contrast.test.ts`.
- **Counts**: compact in prose and headers (`formatCompactNumber`: "1.3K open
  issues in 61 cases"), the exact figure in the tooltip and in table cells;
  state counts under 10,000 are exact.
- **Focus**: every row, card and link takes the DS focus ring (`ring-focus`),
  never a coloured left bar.
- **Control heights (round 2 B2)**: one height per toolbar row. Table
  toolbars (search, Filters, clear, TotalChip, Columns, page size, CSV) and
  the bulk action row are all 32 px (DS `size="sm"`); page header actions
  (list and case page) are the DS `Button` default, 36 px, `IconButton`
  default for More.
- **Sorting (round 2 B3)**: every data table here is sortable on its value
  columns through the `DataTable` header (`aria-sort`, arrow on the active
  column, a neutral sort glyph on the others); the sort is kept in the URL
  (`?sort=priority&dir=desc`) with the filters, so a shared link and Back
  restore it. Default sorts are named per table in 7.2 and 7.3.
- **Locale (round 2 B6)**: every string of these screens, table headers,
  Chips, dates (`Intl.DateTimeFormat`, day and short month), totals, page
  size and pager included, comes from the i18n catalogs; the width proofs of
  7.2 are rendered fully translated in fr and ja, not over English labels.
- **Actors**: a person is the DS `Avatar` (initials); the system and the
  agents are an icon in the same 24 px circle (system: monitor, agent: robot)
  with their name in the tooltip, never initials such as "SY" (round 2 A4).
  Notices carry the DS info `Icon`, never a typed "i" (round 2 A3).

### 7.1 Mobilization stage page: the Cases board (signature viz)

Built in S5c of #11545 (`frontend/src/pages/xtm-one/ctem/CasesBoard.tsx`,
figures in `ctem/cases-board.ts`). As built: the inflow line counts the cases
now in a state that entered it in the period (`state_since`), since the API
returns no transition counts yet; the resolved-column median and the footer
links wait for that read. Bulk actions go through `POST /ctem-cases/bulk`
(each case checked alone, refused ones named with their rule), Merge through
`POST /ctem-cases/merge` (survivor keeps state, owner and due date, takes the
keys, members and history), Split from the case page's issue selection
through `POST /ctem-cases/split`.

Placed inside the real `CtemStageDetail` page (B8): `EntityPageHeader`
(back link, posture gauge, "Mobilization", methodology, badges, owner
agent), `ScorePair`, `CompanyContextNotice`, then the Cases board full width,
then the existing AI assessment and facts grid unchanged.

- **Flow strip** (UX decision Q2: a flow strip, not a Kanban; a Kanban
  duplicates the list and drag would bypass the owner, evidence and expiry
  rules): one column per case state (five since #857), on one row from xl
  (`BOARD_GRID`, held equal to the state count by
  `tests_unit/ctem-cases-board-grid.test.ts`), stacked below, never an
  orphan column; each a header with the state Chip, the exact
  count and, for the two open states, the ageing bar with its legend and the
  overdue line.
- **Transitions** of the period ("+14 started", "+22 resolved", "+3 risk
  accepted", "+2 reopened") are never lost (B6, round 3 B1): at every width
  each transition is the first line of the column it flows into ("+14 started
  in 30 d" heads In progress, "+3 risk accepted in 30 d" heads Accepted
  risk); at >= 1440 the arrows between the open columns repeat the two flows
  of the main path; from 1280 the five columns share one row, below it they
  stack in one column (#857 UX round 1 B2: the 2 x 2 left Accepted risk
  alone on a row).
  Every column carries that inflow line, Needs
  attention included ("+18 new in 30 d, +2 reopened"), at a fixed one-line
  height, so the state Chips sit on the same baseline across the row
  (round 2 B7); a period with no inflow reads "No new case in
  30 d", never an empty gap.
- **Top cases** under each open column: the three highest-priority cases
  (title on one line with ellipsis and its full text in the tooltip, priority
  Chip, issue count, crown jewel Chip, owner Avatar or "Unassigned", age),
  each a link to its page.
- **Resolved** column: cases resolved in the period, the median time to
  resolve, and the issues they closed ("22 cases, 412 issues closed"; the
  list's Resolved tab says "All time" next to its count, so the two figures
  never read as one, A1). The column header carries no period label: the
  inflow line already names it (round 2 A5).
- **Accepted risk** column: the next expiries with their date; one expiring
  within 14 days carries the `medium` "Expires in 6 d" Chip, and the column
  header says "<n> expire in 14 days" (A2).
- Card header: "Cases", "1.3K open issues in 61 cases" (exact in tooltip),
  period `SegmentedFilter` 7 / 30 / 90 d, "View all cases" (filled primary,
  right end). Footer: last grouping time and sources, "No known fix: 96
  issues" link, "Recommended actions" link.
- States of the card: loading (skeleton of the strip at its final height),
  section read error (inline error with Retry, the rest of the page loads),
  no source configured (setup row: "Add your tech stack" to Settings >
  Organization and "Connect OpenAEV" to Integrations, A9), data but no open
  issue (positive state "No open exposure on your stack", last grouping
  time). The positive state is an all-clear only when every product
  answered: when the list carries `details_unavailable`, the line names the
  products instead ("OpenAEV could not be read, so open exposures there may
  be missing.") with a warning icon (#12139). The API sends product ids
  ("openaev"); the line shows their display names (CTEM_PRODUCT_INFO) and
  agrees the verb through an ICU plural on the product count.

### 7.2 Cases list `/ctem/cases`

- Page header (`EntityPageHeader` list form): back link "Back to
  Mobilization", title "Cases"; actions in the platform order (secondary
  first, filled primary last), DS `Button` default height: Recompute,
  Export CSV. The totals live in the
  table toolbar's `TotalChip`, not in the title row.
- State tabs: the shared `SegmentedFilter` (`overflow="menu"`, radio) with
  exact counts: Needs attention, In progress, Resolved ("All time" in its
  tooltip, A1), Accepted risk, All.
- **The shared `DataTable`, its own toolbar anatomy (B2)**, one row at 1080
  and 1728 in en, fr, es and ja: search ("Search fix, product or CVE"), ONE
  `FiltersPill` holding the facets (owner: me / unassigned / a person,
  priority, crown jewel, source, product, overdue, accepted risk expiring in
  14 days) with its count badge, "Clear all filters" (an icon with its
  tooltip in compact mode), then right: `TotalChip`, Columns, page size, CSV
  export. Active filters show as the pill's count and the panel's checked
  options, never as a second row of pills. Pagination in the footer.
- **Columns at 1080 (B1)**, no horizontal scroll (table scrollWidth =
  clientWidth, measured in the four locales with the longest fix title):
  selection, Priority (Chip), Fix (one line, `max-w` + ellipsis, full title
  and "14 CVEs from OpenCTI and OpenAEV" in its tooltip; the sub line is
  gone, so every row is one line), Issues, Owner (Avatar + name, truncate),
  Due ("6 Nov" with the error-tone "Overdue" Chip), State (Chip). Hidden below
  `xl` and offered in Columns: Crown jewels, Assets, KEV, In state. At 1728
  every column shows.
- **Sort (round 2 B3)**: Priority, Fix, Issues, Crown jewels, Assets, KEV,
  Owner, Due and In state are sortable. Default: Priority descending, then
  Due ascending (the most urgent case on top), `?sort=priority&dir=desc` in
  the URL; a click on the active column flips the direction, another column
  takes over the sort.
- **Selection**: `SelectionBox` rows and header (indeterminate when part of
  the page is selected, A4), "N selected" and "Select all 34 matching" in the
  toolbar, bulk actions as the shared `ListBulkAction` buttons (32 px, the
  height of the toolbar row): Start (filled primary, last), Assign owner, Set
  due date, Accept risk, Merge (2 or more, disabled with its reason
  otherwise), Export selection. Below 1440 only Assign owner and Start stay
  visible; the others move into a More `IconButton` menu, so the row never
  wraps or overflows in fr or ja. Escape clears.
- **Bulk dialogs** (centred, shared `Dialog`, fields per section 2 of the
  form rules: label above, required mark, helper line, error under the
  field; DS `Dialog` size `md`, 640 px, labels at body size 14 px, fields
  36 px, round 2 B5): Assign owner (person `Combobox`, required); Set due
  date (DS `DatePicker`, the policy SLA suggested as helper text); Accept
  risk (owner `Combobox`, required; reason, required, 500 characters with
  counter; expiry DS `DatePicker`, required, max from the policy, default
  today + 90 d, round 2 A1; "Applies to 2 cases" summary; primary
  "Accept risk"); Merge (target case radio list, the merged keys listed,
  primary "Merge 2 cases").
- **Keyboard**: the table's row focus uses the DS focus ring (A5); Up and Down
  move it, Space selects, Enter opens; the `a` shortcut assigns only while
  the table has focus and a selection exists, and is announced in the Assign
  button's tooltip (A7). Every action is reachable by Tab.
- States: loading (table skeleton, toolbar mounted), read error (table error
  with Retry), empty (no source: the setup row of 7.1; sources but no case:
  "No open exposure on your stack", or the products that could not be read
  when `details_unavailable` is not empty, #12139), no results (filters match nothing, with
  "Clear all filters").

### 7.3 Case page `/ctem/cases/:id`

- Header: `EntityPageHeader` with back link "Back to Cases", the fix as
  title, badges (state Chip, priority Chip, owner Avatar, due date), actions
  in the platform order: More (`IconButton` + `Menu`: Split, Merge, Open in
  Act now, A6), Accept risk, Assign, then the next state as the filled
  primary ("Start", "Resolve", "Reopen").
- Summary: the shared `StatCard` row (issues, KEV, max CVSS, max EPSS, assets,
  crown jewels, first seen), wrapping to two rows at 1080; each value's
  tooltip names its source and read time.
- "Why this case" sentence: the key rule that grouped it (R1 / R2 / R3) and
  the policy rules that set its priority.
- Layout (UX decision Q3: issues first, always): at >= 1440 two columns,
  Issues (2/3) and Assets + History (1/3); at 1080 to 1439 one column,
  Issues, then Assets, then History, so no column overlaps another (B3).
- Issues: the shared `DataTable` with the same anatomy as the list (round 2
  B4): search ("Search CVE"), ONE `FiltersPill` (Source, KEV, CVSS band),
  clear-all icon, then right `TotalChip`, Columns, page size (10 / 25 / 50,
  default 10), CSV, all 32 px; pagination in the footer, no "Show 10 more";
  columns CVE (link), Source (Chip), CVSS, EPSS, KEV, Assets, Open since, and
  the external link `IconButton` to OpenCTI or OpenAEV with its accessible
  name "Open in OpenCTI". Sortable on every value column; default CVSS
  descending, then EPSS descending, kept in the URL (round 2 B3); the rows
  are drawn in that order (round 3 B2). Column hiding (round 3 B3): a column
  hides only when the table would otherwise overflow its card (scrollWidth >
  clientWidth), lowest value first (Open since, then Assets); the columns of
  the active and default sort (CVSS, EPSS) never hide. At 1080 one column the
  table fits, so every column shows.
- Restricted members (#11613): a member issue the viewer cannot see is a row
  of the same Issues table, at the same height, its first cell the lock icon
  and "Restricted", every other cell empty (no id, source, score, asset or
  link); the Source filter offers "Restricted". A case the viewer sees
  nothing of is the not-found page, never an empty case.
- Assets and crown jewels (from #11543 when present; otherwise "Link assets
  in Settings > Organization").
- History: vertical event list with actor (person Avatar, or the system or
  agent icon of 7.0), time
  (relative with the locale date in its tooltip), reason, evidence link.
- Accepted risk state: a `DashboardNotice` (neutral) with owner, reason and
  expiry at the top; "Expires in 6 d" Chip when within 14 days. Resolved
  state: a success notice with the evidence (system: "every member issue
  closed at source", or the user's reference) and "Reopen".

### 7.4 Command center widget `cases`

UX decision Q1: in the default layout right after Act now, as a compact
full-width row (one line at 1440, two at 1080): the state Chips (every
state of `CASE_STATES`, counted by the list API since #857) with
their exact counts on one segmented bar (the state colours of 7.0), then
"5 overdue" and "2 accepted risks expire in 14 days", each a link to the
filtered list. Not configured (no tech stack and no OpenAEV): one dismissible
setup row ("Build cases from your tech stack", actions "Add tech stack" and
"Connect OpenAEV"), per the dashboard notice rule. The id `cases` is added to
`CTEM_WIDGETS`, `CTEM_DASHBOARD_WIDGETS` and the backend allowlist
(`dashboard_layouts.py`) in the same PR; the default layout gains it, saved
layouts load unchanged (test).

Shipped in S5a (#11545): `frontend/src/pages/xtm-one/ctem/CasesCountsCard.tsx`
(the row; counts from `GET /ctem-cases`), `ctem/cases-due.ts` (overdue: an
open case past its due date; expiring: an accepted risk ending within 14
days; one definition for the row and the list's "Due" filter). The Cases list
reads its tab from `?state=` and its Due filter from `c_f_due`. Not configured
= no case, no organization context and no OpenAEV. The setup row closes for
the session. Tests: `ctem/cases-counts-card.test.tsx`,
`ctem/command-center-v2-default.test.ts`, `tests/test_ctem_dashboard_layout.py`.

### 7.5 Funnel last layer (proposal A)

The last layer of the funnel is "Cases" (layer key `open_issues`, source
`ctem_cases`, #11545 S5b): the exact count of the open cases the viewer sees
(needs attention plus in progress), read from `GET /ctem-cases` counts, so it
never counts a case the list would hide. It links to `/ctem/cases`. Cases are
another unit than the vulnerability layers above: the layer keeps the width
above it, names no drop and is left out of the monotonic check. It is "not
configured" (call to action: Settings > Organization) when no case exists and
none can be built (no tech stack and no OpenAEV), never an invented 0; until
the Cases API answers, the layer shows no count. The sample funnel keeps it
"not available yet". The open issue count and the grouping ratio need a
viewer-scoped total of open member issues the Cases API does not answer yet;
they are not drawn rather than estimated from one page of the list.

## 8. Agent tools (new, no existing contract changed)

| Tool | Does |
|---|---|
| `list_ctem_cases` | filters (state, owner, priority), paging (max 50); returns the API's case rows and the counts by state |
| `get_ctem_case` | one case with members (source, id), the latest 20 history events |
| `create_ctem_case` | splits open issues (`source:external_id`) out of their cases into a manual case that pins them (`ctem_cases_store.create_manual_case`; a `split` event on each case they leave) |
| `update_ctem_case` | owner, due date, note, state with `reason`; `resolved` needs `evidence`; `accepted_risk` refused for an agent (a person accepts risk) |
| `record_ctem_case_validation` | a validation or re-test reference (provider, ref, result, kind, mitigation_only) through `ctem_case_loop.record_validation`: passed or not_exposed resolves (mitigated, watching when mitigation only), failed reopens, the events raised after the commit |
| `record_ctem_case_ticket` | a ticket reference (integration, key, url, status) through `ctem_case_loop.record_ticket`: one entry per `(integration, key)` updated in place, a `ticket` event on change, never a state move |
| `decide_ctem_case_remediation` | the remediation decision (CTEM-17, gate B, key `ctem.remediation.decision`): options with figures and the recommendation; rules in `services/ctem_remediation_decision.py` (see 9ter) |

### 9ter. Remediation decision (CTEM-17)

No decision structure is stored on the case: the decision is the gate `ctem.remediation.decision` of the fleet catalog (CTEM-34), its outcome a `gate` event plus one `note` event listing every option. Rule, no model call: a level or pin of the agent's home fleet that makes the gate wait always wins (no home fleet, or a fleet that does not declare the gate: it waits); otherwise, only when `risk_appetite.approval_threshold` is set, an expected loss above it (or unknown to the deciding user) waits unless the recommended option is a pre-approved runbook; with no threshold the recommendation is applied and recorded "decided by policy". A wait opens one `needs_human` work item keyed `ctem:remediation:<case id>` whose `ask` is the unified decision (`services/decision_ask.py`): question, options with their effect, recommendation; the answer is recorded with `answered_option`. Option figures come from OpenCRQ read with the acting user's identity; a user with no OpenCRQ access (`organization_context_opencrq._opencrq_connection` refuses) gets the options without cost or expected loss in the ask. The case trail (the `gate` event reason and the `note`) never carries a figure, whoever decides: every viewer of the case reads its history whatever their own OpenCRQ access, so the figures stay in the ask and the tool answer (`Decision.case_reason`, `outcome_note`). Tests: `tests/test_ctem_remediation_decision.py`, `tests/test_ctem_case_tools.py`.

### 9quater. Treatment plan in OpenCRQ (CTEM-18)

A decision is recorded in OpenCRQ as a treatment plan through four OpenCRQ provider tools on the procedures OpenCRQ already serves (`providers/opencrq/treatment.py`): `create_opencrq_treatment_plan` (`treatmentPlans.createTreatmentPlan`: name, owner, period, one-off and annual cost, rationale, first control moves), `update_opencrq_treatment_plan` (`updateTreatmentPlan`, partial: an empty value clears a nullable field), `set_opencrq_treatment_plan_moves` (`setTreatmentPlanScope`, replaces the moves) and `add_opencrq_treatment_plan_moves` (`addTreatmentPlanMoves`). A move is a control capability with its target strength rung and coverage bucket (L0 to L3, a target left out keeps that axis); OpenCRQ re-prices the plan after a change of its moves. No new OpenCRQ entity: the accepted residual risk is the evidence dismissal `risk_accepted` with a reason code (`update_opencrq_evidence_state`), its expiry the profile policy `accepted_risks`. Every call carries the acting user's own OpenCRQ token (per-user JWT): a user OpenCRQ refuses gets that refusal and nothing is written for them. Gates: on a CTEM loop fleet the four tools are rules of `ctem.remediation.decision` (runs out of the box, a pin or Full human gates makes it wait, so the plan follows the decision without a second approval); on the risk budget desk they are rules of `opencrq.register.write`. Tests: `tests/test_opencrq_tool_contracts.py`, `tests/test_opencrq_risk_budget_desk_fleet.py`.

On the case (CTEM-18-UI, #1068): `create_opencrq_treatment_plan` answers the plan's OpenCRQ link (`Link:` line, `/treatment-plans/<id>`), and the fleet attaches the plan to the case with `update_ctem_case` `treatment_plan` {id, name, url} (`ctem_case_loop.record_treatment_plan`: the id a UUID, the url the OpenCRQ page of that id, else refused; the same plan again writes nothing). No migration: one `treatment_plan` case event (`to` the id, `reason` the name, `evidence` the url). The case detail serves `treatment_plan`, the newest such event, as the exploitation reading (#936): id, name and link only to a viewer whose own OpenCRQ read is available, `{restricted: true, at}` to anyone else, and the history row of that kind carries no value for them. The case page shows it in a "Treatment plan" card (name linked to OpenCRQ, attach date; never blank) and the history line "Attached the treatment plan". The accepted residual risk and its expiry stay where they were: the accepted-risk card of the case and Governance & Risk > Accepted risks. Tests: `tests/test_ctem_case_treatment_plan.py`, `frontend/src/pages/xtm-one/ctem/case-treatment-plan.test.tsx`.

Granted to the built-in CTEM loop orchestrators (vulnerability prioritization,
threat response, which run a case from `ctem_case.opened`, #891), the
Remediation Coordinator and the Mobilization Analyst (their tool lists only),
RBAC of the posture (the caller's integrations).
Every write is an event with the agent as actor.

Shipped in S2 (#11545): `backend/app/services/builtin_tools/ctem_cases.py`,
offered by `offers_ctem_case_tools` (built-in agent, slug above, XTM One
mode) in `agent_chat` and `assignment_engine` next to the organization
profile tool, acting for the person the turn or run acts for. Tests:
`tests/test_ctem_case_tools.py`, `tests/test_ctem_cases_postgres.py`
(manual split survives a recompute).

## 9. UX decisions (round 1, 13 Nov 2026)

1. Command center widget: in the default layout right after Act now, as a
   compact full-width row; saved layouts unchanged; not configured = one
   dismissible setup row.
2. Board shape: a flow strip with ageing bars, not a Kanban (a Kanban
   duplicates the list; drag would bypass owner, evidence and expiry).
3. Case page: Issues first, always; assets and history in the right column
   at >= 1440, under the issues below it.

## 9bis. The loop: cases opened from signals, updated on events, closed on proof (#857)

The third brick of the spine: signal (`dev-docs/ctem-signals.md`) -> reconcile and scope gate (`dev-docs/ctem-scope-gate.md`) -> case. Rules in `services/ctem_case_loop.py`, no model call.

**Model (migration `c857ctemcaseloop1`, additive).** A case carries `trigger_path` (the path of the signal that opened it; `new_vulnerability` for a case from the two readers), `risk_scenario_ids`, `severity` (P1 to P3), `sla_due_at`, `sla_breached_at` (set once), `tickets` (`[{integration, key, url, status, at}]`, written by CTEM-19), `validations` (`[{provider, kind, ref, result, at, mitigation_only, note}]`, newest last, 50 kept), `validation_plan` and `resolution_kind` (`not_exposed`, `mitigated`, `fixed`, `accepted_risk`). New state `mitigated_watching`; new remediation kinds `close_exposure`, `decommission`, `rotate_credential`, `restore_control`, `supplier_action`, `restore_coverage`; new member source `signal`. Existing cases read as path `new_vulnerability` with no origin signal. The list and case reads (`GET /api/v1/ctem-cases`, `GET /api/v1/ctem-cases/{id}`, `list_ctem_cases`, `get_ctem_case`) return these fields as stored, the same for every viewer: ids, enums, dates and references, never a source issue's content.

**Opening.** A signal the scope gate leaves `confirmed` with outcome `case` opens the case of its fix in the same transaction (`ctem_scope_gate_store.evaluate_after_record`), or joins the open case with the same key: one case per remediation, every origin signal a `signal` member. Key: a software subject on a vulnerability path is `upgrade:-/<product>@latest` (the reader's key, so both meet); any other is `<kind>:<subject kind>/<subject ref>`, the kind from the trigger path. Severity, SLA (`sla_hours` of the decision record, else P1 24 h, P2 72 h, P3 30 days) and risk scenarios come from the decision record; a joining signal keeps the most urgent severity and the earliest SLA. The signal moves to `case_opened`. Paths `proven_exploitable` and `mitigation_regression` open with a validation plan.

**Proof.** `POST /api/v1/ctem-cases/{id}/validations` (any caller who sees the case) records a validation reference and sets the validation plan. `passed` or `not_exposed` resolves (`fixed` or `not_exposed`); with `mitigation_only` it goes to `mitigated_watching` and the re-test stays on; `failed` reopens a resolved or watched case to `needs_attention`; `pending` only records. A case with a validation plan resolves only on a passing newest reference (the PATCH route refuses with 422) and never on absence at source in a recompute. A case with no plan keeps the accepted proof of its kind as evidence (ticket done, re-scan, directory check). Accepted risk keeps its 90-day ceiling.

**Schedule.** The autonomous tick runs `ctem_case_loop.sweep` once per `CTEM_CASE_SWEEP_SECONDS` (default 3600, minimum 60, a Redis slot): opens the cases of confirmed signals still waiting (a failed opening is retried here; each signal opens in its own savepoint, so one that fails is logged, counted `skipped` and left `confirmed` without stopping the pass) and escalates every open case past its SLA once. A case key or title longer than its 300-character column is bounded: the title is cut, the key keeps its head and a digest of the whole so two long subjects never share a case.

**Events (platform-wide, ids and facts only, no title).** `ctem_case.opened`, `ctem_case.reopened`, `ctem_case.resolved`, `ctem_case.sla_breached`, raised after the commit with a stable event id, by every writer of a case: the loop (signals, validations, sweep), the source recompute, a manual split and the backfill of section 4.3 (#1206); filter keys `case_id`, `state`, `severity`, `trigger_path`, `remediation_kind`, `validation.provider`, `validation.result` and the rest of `_CTEM_CASE_KEYS`. CTEM-04 starts fleet runs from them; CTEM-15 maps OpenAEV deliveries to the validation route.

**OpenAEV results move the case (CTEM-15, #918).** The seeded assignment "OpenAEV results into cases" of the vulnerability fleet provisions the OpenAEV triggers `simulation_finished`, `finding_updated`, `finding_deleted`, `scenario_score_degraded` and `critical_asset_reached` (optional: an OpenAEV that refuses it keeps the others, its state lists it under `unsupported_events` and the next check retries); its filter drops every delivery, so it never runs. On a verified delivery, the webhook route (`services/ctem_openaev_events.py`) finds the cases that wait on the delivered object (an `openaev` validation reference whose `ref` names it, or an `openaev` member with that id; states `needs_attention`, `in_progress`, `mitigated_watching`, `resolved`), writes one `source_event` history entry per case and raises `ctem_case.validation_finished` once per case and occurrence, with `openaev {event, resource_type, resource_id, platform_url}` on top of the case routing; like the other case events it is in `VIEWER_CHECKED_EVENTS`, so only a run whose user reads the case hears it (#933). Both loop fleets hear it (routed by `trigger_path`), read the result in OpenAEV and write it with `record_ctem_case_validation`, which resolves or reopens the case. No poll, no model call before the run. A `scenario_score_degraded` delivery records a path 8 (`mitigation_regression`) signal through the signal table; an `asset_stale` delivery (an `ASSET` update trigger filtered on `asset_status` = `STALE`, raised once by OpenAEV's freshness job when an asset stays unseen past the tenant's `asset_stale_after_days`, CTEM-27) records a path 6 (`stale_asset`) signal, outside lens, the asset as subject. OpenAEV sends it only to readers of the asset in the trigger's tenant; XTM One records it as a `system` signal, which only administrators read. A `critical_asset_reached` delivery (CTEM-13: a finished simulation's attack path reached a high or very high criticality asset) records a path 4 (`proven_exploitable`) signal with the simulation as subject and its OpenAEV link, and moves the cases waiting on that simulation like `simulation_finished`; the case history names it "Critical asset reached". OpenAEV names no asset in it: the reached assets are read in its attack path with the reader's own rights.

**Visibility.** A `signal` member is shown to a viewer under the signal API's rule (an administrator, or the user it was recorded for); a case with no member the viewer sees stays absent, as before (#11613).

## 10. Slices

| Slice | Content | Gate |
|---|---|---|
| S0 | this document and the mockups | UX Director design APPROVE |
| S1 | model, migration, grouping, state machine, API, tests | CI |
| S2 | agent tools, grants, tests | CI |
| S3 | board, list, case page, widget, i18n x4 | CI + UX APPROVE, before/after captures |

## 11. Tests owed

- Grouping fixtures: same product and fixed version -> one case; two fixed
  versions -> two cases; course of action wins over version; no fix -> "No
  known fix", no case.
- Recompute keeps state, owner, due date and history; resolved when every
  member closed; reopened when they come back; merges and splits survive.
- State machine: accepted risk needs owner, reason, expiry; expiry returns to
  needs attention; an agent cannot accept risk; resolve needs evidence.
- API and tool round-trip; existing CTEM tools unchanged (contract test).
- Layout: saved layouts without `cases` load unchanged.
