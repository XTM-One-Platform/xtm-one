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
| OpenCTI | A Vulnerability related by `has` to a Software observable matching the organization tech stack (the match already used by the tailored KEV, `ctem_metrics_collector._stack_matches`: CPE equal, else the product name with agreeing vendors, a side's vendor being its `vendor` or else the vendor its CPE names, #1693; a CVE OpenCTI links to several stack products is one issue, so one case, per product), and passing the prioritization policy of #11543 when one is set | GraphQL, per-user credentials (as the posture) |
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

### 4.2quater Version match on remediation and mobilize cards (#1472)

An upgrade proposal never asserts the hosts are vulnerable without a version
compared. `ctem_version_match.version_match` states three facts: the running
versions (from `running_versions` below, or `unknown: no asset reports
<product>`), the affected range (`unknown: OpenCTI holds no affected versions`
until a source gives one) and the fixed version (the case's, or `unknown`);
`blind` is true when the running version or the affected range is unknown.
`get_ctem_case` answers it as `version_match` on an upgrade case (`null`
otherwise), and the Remediation Coordinator's `actions` carry it.

The platform enforces the line where the card is written, not in a prompt:
`work_item.create_work_item` and `update_work_item` complete the ask of an item
keyed `ctem:remediation:`, `ctem:f1:case-mobilize:` or `ctem:f2:mobilize:`
and a case id, when the case is an upgrade and the ask has no `Version match:`
line: the line goes first in the context, then, when blind, "Proposed without
version matching: ..." and "Verify the running version, then upgrade". A case
that cannot be read leaves the ask as given; the version is never guessed.

### 4.2ter Running versions from the asset inventory (#1252)

The version an organization runs is read from its assets, never typed in the
tech stack: the canonical asset store (`organization_asset_software`, #1178)
holds one row per asset, product and version, with the sources that report it
(Intune, Defender, an SBOM, OpenAEV). The case read (`GET
/api/v1/ctem-cases/{id}` and the `get_ctem_case` tool) carries
`running_versions` for an upgrade case (`ctem_case_versions.for_case`), `null`
for any other case or when no asset runs the product. An operating system is
stored on the asset, not as software: a case whose product label is a whole OS
name (`Microsoft Windows`, `Apple macOS`, `Google Android`...,
`ctem_case_versions.OS_FAMILY_LABELS`) also counts the assets whose `os_family`
matches, their `os_version` as the version (`unknown` when empty) and the
source of that field; an asset already counted by a software row of the product
is not counted twice (#1430). The affected assets of the case page join the
same way.

- `versions`: one row per version of the case's product (its label, or vendor
  and product), the number of distinct assets running it and its sources, the
  most assets first, at most 50 (`more_versions` counts the rest);
  `asset_count` is the number of distinct assets running the product;
- with a fixed version known: each row's `status` (`below_fixed`, `fixed`,
  `not_comparable` for `unknown` or an unreadable version) and the totals
  `assets_below_fixed`, `assets_on_fixed`, `assets_not_comparable`. Each asset
  is counted once, by its worst version (below the fixed one, else not
  comparable, else fixed), so an asset running two versions or reported under
  two vendor and product spellings never inflates them and the three totals add
  up to `asset_count`;
- with no fixed version: `fixed_version_checked` names the sources looked in
  (section 4.2bis, steps 1 and 2); nothing asks the user to type a version.
- per platform (#1572): a row is one (version, `platform`), the platform
  being the asset's `fields.os_family` (Intune, Defender, OpenAEV fill it;
  `null` when unknown; read by a join on the software rows of the case's
  product keys, never an IN list of asset ids, so no estate size reaches the
  driver's bind cap), with its `asset_ids` (at most 20, `more_asset_ids`
  counts the rest). Each asset is compared with the fixed version of its own
  platform: `fixed_versions` `{platform: version}` when the remediation names
  one (the row then carries its `fixed_version`), else the case's generic
  `fixed_version`. A platform the per-platform map does not name, or a generic
  fix over assets of several platforms (`platform_unverified` true), is never
  read as fixed: an asset not below it is `not_comparable` with
  `status_reason` "fix not known for this platform" (counted in
  `assets_fix_not_known_for_platform`), so the case never leaves the scope on
  the inventory. One platform with a generic fix behaves as before. The answer
  also gives `platforms`, `assets_multi_version` (distinct assets running more
  than one version), `version_row_asset_sum` and `version_rows_note`: the rows
  may sum above `asset_count` because an asset running two versions is in each
  row, while the three status totals still add up to `asset_count`. The
  version match line names the platform (`36.0 (macOS) on 2 asset(s)`).
- `history` (#1694): why the versions moved, newest first (at most 10). Each
  recompute (`ctem_cases_store.apply_recompute`) compares the per-device state
  the inventory answers (`for_case(with_devices=True)`: versions, status
  against the fix, sources per asset id) with the snapshot of the case's last
  `versions` event and, on a change, writes one `versions` event
  (`ctem_case_version_history.record`): `to_value` the summary (`3 device(s)
  changed: 3 below the fix -> 0`, or `baseline: ...` the first time), `reason`
  one line per device (before, after, why: upgraded, downgraded, version
  became unknown with the reporting source, no longer reported by the
  inventory, newly reported, status changed), `evidence` the snapshot and the
  changes as JSON. An inventory that cannot be read records nothing, never
  every device gone. Each entry: `{at, summary, changes: [{asset_id, before,
  after, reason}], more_changes}`. The `versions` events stay out of the
  served `history` of the case and out of its phase; this field serves them.
  No version-range engine.
- the Cases list `asset_count` (#1694): the assets the visible issues name;
  when they name none (an OpenCTI-only case), the device count of the case's
  latest `versions` snapshot, with `asset_count_source` `issues`, `inventory`
  (and `asset_count_as_of`) or `unknown`: then `asset_count` is `null` with
  `asset_count_reason`, never a silent 0.
- where the per-platform fix comes from (slice 2 of #1572): the OpenCTI reader
  reads each Software that `remediates` the vulnerability with its `cpe`;
  `ctem_fixed_version.platform_of_software` takes the platform from the CPE
  `target_sw` (then `target_hw`), else from one operating system the name or
  the version names (two named: none; the bare word `ios` counts only in the
  CPE target fields, since `Cisco IOS` / `ios_xe` are not Apple's iOS). `fixes_by_platform` keeps the smallest
  fix above the declared version per platform; one remediating version above
  it with no platform keeps the fix generic (`{}`), since its reach is not
  known. The members of one product fold per platform like the generic target
  (`fold_by_platform`: greatest fix per platform; one member without a map
  makes it generic). The map travels on the remediation, the signal's
  `case_fix.fixed_versions` (bounded to 12 platforms) and is kept on
  `ctem_cases.fixed_versions` (JSON, `{}` by default, migration
  `c1572ctemfixplat1`): set at opening, at a retarget, and refreshed at each
  recompute from what the members' source names now. The case read and the
  affected assets pass it to the running versions; the case summary carries
  `fixed_versions`.

Two grouped reads and one platform read, in a savepoint: an unreadable store answers `null`, never a
failed case read. Versions compare with `ctem_fixed_version.compare_versions`.

### 4.2quater Affected assets on the case read

The case read also carries `affected_assets` (`ctem_case_versions.assets_for_case`):
one row per asset the caller's visible issues name (restricted rows and signal
rows add none), names folded on case and spaces, with `issue_count`,
`open_issue_count`, `kev` and `max_cvss`, the most exposed first, at most 500
(`more_assets` counts the rest). `kev` and `max_cvss` fold the facts of the
same CVE across every visible source: an asset named by an OpenAEV finding of a
CVE that OpenCTI marks known exploited reads `kev` true. The page cuts the
version and status cells on one line with a tooltip, hides Status below a 56 rem
card (back from the Columns pill), and draws Sources with the same `SourceMark`
as the Running versions card (logo or glyph, name in the tooltip). On an upgrade case each row is joined to the
inventory by asset name (`organization_assets.name_key`) and adds
`in_inventory`, the `versions` of the case's product it runs, their `sources`
and, with a fixed version known, its worst `status`; `version_source` is then
`inventory`. Any other case, or an inventory that cannot be read, answers
`version_source` `none` and the page says "Version source: none yet", never a
version to type in the tech stack. A row with `in_inventory` and an `asset_id`
opens the asset overview (`/ctem/inventory/assets/:id`: its software with every
version and source, SBOM included, its source records and its cases); a row
outside the inventory is no link (CTEM-20-UI).

**Leaving the scope.** At every recompute (`ctem_cases_store.apply_recompute`)
an upgrade case with a known fixed version, open or resolved, is checked
against the inventory (`ctem_case_versions.fixed_in_inventory`): when every
asset running the product runs the fixed version of its platform or later
(`all_on_fixed`: at least one asset on it, none below, none `unknown` or
unreadable, not `platform_unverified`), the case
leaves the scope. An open case resolves with the evidence "every asset running
the product runs the fixed version or later (asset inventory)" and resolution
kind `fixed`; a resolved case is not reopened by member issues that stay open
at source (OpenCTI still relates the CVE to a version nobody runs). The
members are left as the source reports them. A case with a validation plan
still closes only on its passing validation (#857); a case whose fixed version
is unknown never leaves the scope on the inventory; an asset that falls back
below the fix (or a new one running an old version) brings the case back
through the usual reopen rule.

**Read at opening, before the threat package.** The same check runs when
`ctem_case_loop.open_from_signal` creates an upgrade case (no validation plan,
a known fixed version): the installed versions are read from the inventory
before any loop starts, so a case every asset already runs the fix of resolves
at once (`fixed`, reason "not affected at opening", the inventory evidence with
the asset count) and raises `ctem_case.resolved` instead of `ctem_case.opened`:
no threat package is drafted and no gate reaches a person. Below the fix,
unknown, uncomparable or `platform_unverified` keeps the case open and the
loop starts as before (doubt moves forward, #1606).

**On the case page.** An upgrade case draws a "Running versions" card
(`frontend/src/pages/xtm-one/ctem/CaseRunningVersions.tsx`) from
`running_versions`: one sentence with the distinct assets and versions, the
assets still below the fixed version and those on it or later (ICU plurals with
an `=0` branch), then a table of versions with their asset count, their status
against the fixed version and their sources as logos (an integration or a
product by its `ProviderLogo`; `sbom`, `csv`, `manual`, `api` and `agent:*`,
which carry no brand, by a glyph; the name in the tooltip and for screen
readers). With no fixed version the card says it is unknown and what was
checked; with no asset running the product it says the versions come from the
assets. Nothing on the page asks the user to type a version in the tech stack.
Per platform (slice 3 of #1572): when a row names a `platform` the table adds a
Platform column, and when the case carries `fixed_versions` a Fixed version
column (the row's own platform fix) and the line "Fixed version per platform:
Windows: 36.0 and macOS: 37.0"; the totals then read "below / on the fixed
version of their platform". A row whose `status_reason` is "fix not known for
this platform" reads that status, never "Cannot be compared" alone; a
`platform_unverified` answer says the generic fix names no platform while the
assets run several, and `assets_fix_not_known_for_platform` is counted in its
own line. `assets_multi_version` above 0 adds the line that those assets are
counted in each of their rows, so the row sum above the asset count reads as
explained. A single-platform answer draws the card as before.

### 4.3 Recompute

- Cases are inferred, never refreshed by hand: the UI has no Recompute button
  (Sam, 22 Nov 2026). The recompute runs by itself on three paths
  (`services/ctem_cases_refresh.py`), rules only, no model call, no run, no quota:
  - the scheduler tick, once per `cases_recompute.interval_minutes` of the
    organization profile (default 15, minimum 5, a Redis slot
    `ctem:cases:recompute-pass:<vertical>`), as the run-as users of the CTEM loop
    fleet's enabled automations, else each user who opened Cases in the last 14
    days (newest 20), each with their own stored credentials; one identity
    failing never stops the others;
  - the page open: `GET /ctem-cases` starts a background recompute for a viewer
    whose last one is older than the interval (the same one-per-minute slot);
    the list re-reads on the `ctem_case_update` frame, in place, no toast;
  - events: an OpenAEV or OpenCTI delivery that already moves a case (s.9bis,
    `ctem_openaev_events`) keeps doing so between passes.
  Every recompute records its freshness (`freshness` on the list answer:
  `updated_at`, and `missing` with `missing_at` while a product did not answer);
  the list header shows "Updated 4 min ago" (exact time on hover) and, after an
  outage, a muted line "OpenAEV did not answer at 13:40, cases unchanged." with
  a link to the integrations. An outage is a product failure only (#1852): a
  recompute identity whose token a product rejected (`rejected`) or that has no
  credentials for it (`not_connected`) writes nothing on that shared line, so a
  scheduled-pass identity never shows an outage to a viewer whose own read
  answered.
- Every failed source read (#1852) records why per product
  (`SourceRead.reasons`): `rejected` (401, 403, AUTH_REQUIRED, Forbidden),
  `too_large` (too_many_clauses, DATABASE_ERROR), `timeout`, `not_connected`
  (no credentials for the reader), `unavailable` (anything else). The viewer
  view caches it (an older cache reads `unavailable`); its log lines name the
  user id, and the read binds `ctem_read_user_id` / `ctem_read_provider` and
  drops the integration fields an earlier token resolution left on the log
  context.
- A recompute never builds a case (#1529). The two ways into a case are a signal
  the scope gate kept (`ctem_case_loop.open_from_signal`) and a split by a
  person or an agent (`create_manual_case`, `create_ctem_case`);
  `tests/test_ctem_case_creation_paths.py` holds it. Each source issue whose fix
  no case holds is recorded as one signal (`services/ctem_source_signals.py`,
  intake `listener`, recorder `system`), idempotent on source, external id and
  fix key: an OpenCTI vulnerability on inventory software is `new_vulnerability`,
  an OpenAEV CVE finding on an asset is `proven_exploitable`. The signal carries
  the fix in `evidence.case_fix`, so `open_from_signal` opens or joins the case
  of that remediation key (one fix, one case, as before). The issue is a member
  of that case and so is its signal. An issue joining an existing case records
  its signal too, joined to the case (`join_case`). A signal the gate keeps out
  (watchlist, to validate, discarded) stays a signal on the Signals page and
  opens no case. The recompute keeps member upkeep only: add, resolve and reopen
  members, the inventory-fixed and not-affected rules. A source signal is as
  visible as its issue (`ctem_cases_view.source_signal_members`). A merge moves
  the merged case's signals to the survivor.
- `POST /api/v1/ctem-cases/recompute` stays as an INTERNAL route for agents and
  administrators (no screen calls it): any user, one per minute per user
  (`services/ctem_cases_source.py`). Every path reads OpenCTI and OpenAEV with the
  identity's own credentials and MERGES into the shared cases (#11613): what the
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
  history line "reopened: issues open again at source". A case a person, an
  agent or a gate resolved (its newest `resolved` transition carries evidence
  other than the two recompute writes, a gate answered not exploitable for
  example) holds while its issues stay as they were (#1646): recompute reopens
  it only on a changed issue (a new member, a member that closed and came
  back, a moved fixed version), with the history line "reopened: issues
  changed since the closure decision (<the closure evidence>)".
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
| `ctem_case_events` | case_id, at, actor (user id or agent id, or `system`), kind (`created`, `state`, `owner`, `due`, `issue_added`, `issue_closed`, `issue_reopened`, `merge`, `split`, `note`, `versions` (#1694)), issue (member id, else null), from, to, reason, evidence |

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

- **One colour per state and per priority, everywhere** (board, list badge,
  case page, widget, signal state of the loop trace), through the shared
  `ToneBadge` (`components/ui/tone-badge.tsx`), the hot hues of Work items and
  Tasks, drawn in the single chip style of the DS `Chip` since design system
  1.13.0 (30% fill, default label, 1px inset hairline in the hue; #1595) (Sam,
  22 Nov 2026, #1267): states needs attention orange, in progress blue,
  mitigated, watching yellow with the DS `eye` icon inside the badge, resolved
  green, accepted risk neutral; priorities critical red, high orange, medium
  yellow, low green; loop severity P1 red, P2 orange, P3 yellow. Each tone is a
  /10 wash of the hue, an edge and a label in its `-tertiary` token (4.5:1 text,
  3:1 edge on every elevation surface in both themes, guard
  `tests_unit/tone-badge-contrast.test.ts`). No CTEM page draws a state,
  priority, severity or signal state with the DS `Chip`, a tone map typed other
  than `HotTone`, or a `Badge` coloured from the feedback tokens (guard
  `tests_unit/ctem-tone-badge-guard.test.ts`; the stage assessment "Out of
  date" badge is an orange `ToneBadge`). Counts bar segments take the same
  hue (needs attention `feedback-warning-primary`, in progress
  `feedback-info-primary`, mitigated `feedback-alert-primary`, resolved
  `feedback-success-primary`, accepted risk `feedback-neutral-primary`). AI purple is never used on Cases (it is the AI
  colour only): crown jewels are a `neutral` Chip with the shield icon.
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
  issue (positive state "No open exposure on your software", last grouping
  time). The positive state is an all-clear only when every product
  answered: when the list carries `details_unavailable`, the line names the
  products instead ("OpenAEV could not be read, so open exposures there may
  be missing.") with a warning icon (#12139). The API sends product ids
  ("openaev"); the line shows their display names (CTEM_PRODUCT_INFO) and
  agrees the verb through an ICU plural on the product count. With
  `details_unavailable_reasons` (#1852) each product says why in plain words,
  never as a missing right: "OpenCTI rejected your sign-in token", "could not
  answer because the query was too large", "did not answer in time", "No
  OpenCTI connection could be used for this read"; a product with no known
  reason keeps the plural sentence.

### 7.2 Cases list `/ctem/cases`

- Page header (`CtemPageHeader`): title "Cases" alone, like Agents, Fleets
  or Work items (no eyebrow: the left menu group already says CTEM; no back
  link); its action slot holds only the quiet freshness line of s.4.3, never a
  refresh button. Export CSV is in the table toolbar. The totals live in the
  table toolbar's `TotalChip`, not in the title row.
- One full list, no state tabs and no "Act on this first" band (#1503):
  quick filter chips above the table (`QuickFilterChips`), each with its
  count and in the URL: the scope (`?state=`: Open by default, Waiting for
  decision, Closed, All) and the toggles (`?c_q=`: Mine, Unassigned, Overdue,
  Critical and high). A case Waiting for decision says what it waits on, who
  and since when (the "Waits on" column; `waiting_on` on each list row: the
  newest unanswered waiting gate, `ctem_case_audit.open_wait`).
- **List and Kanban (#1503)**: the toolbar's view switch draws the same rows
  (quick filters, search and filters applied) at `/ctem/cases` or as columns
  at `/ctem/cases/kanban`, a path never a query (#10203), the query kept.
  Columns are the eight statuses in lifecycle order, or the five CTEM phases
  when grouped by CTEM phase (`phase` on each list row:
  `ctem_case_audit.current_phase` of the history the viewer is served; a
  case with no event yet sits in a last "Phase not known yet" column). Cards:
  title, urgency, owner, due, what a waiting case waits on, the loop line. In
  the status layout a card is dragged to another column to change its status
  (`PATCH /ctem-cases/{id}`); New, Analyzing, Remediating and Validating are
  set by a drop (an unowned case moved to Remediating takes the person
  dropping it as owner), Waiting for decision, Mitigated watching, Resolved
  and Accepted risk are refused with their reason (each needs what a drop
  cannot carry: the case page sets them), and the API's own refusal is shown
  the same way. The Mobilization stage page's flow strip (7.1) stays a strip.
- Group by (`?c_group=`, both views in the list): owner, status, CTEM phase,
  priority, fix family.
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
- **Saved views (#1176)**: a "Views" pill beside Group by. Its panel lists the
  views saved in this browser (`localStorage` key `xtm.ctem-cases.views`, 20 at
  most, the oldest dropped) and saves the list as shown under a name: the state
  tab `?state=` and the table parameters `?c_*` (filters, search, sort, page
  size, grouping; never the page number; columns stay the table's own stored
  choice). A click opens a view on page 1 (other URL parameters kept), the
  trash deletes it, a name already used is replaced. One query, one view:
  saving a list that a view already opens renames that view, so the pill,
  accented, always names the view the list matches (`cases-saved-views.ts`).
- **Grouping (#1176)**: a "Group by" pill at the right of the toolbar, the
  anatomy of Columns (a toolbar select is refused by the data-table gates):
  No grouping, Owner, State, Priority, Fix family; accented and named while
  a grouping is on ("Group by: Owner"), kept in the URL as
  `?c_group=`. The shared `DataTable` `groupBy` (`lib/table-rows.ts`
  `groupRows`) keeps the rows of one group together, the sort kept inside each
  group, with one header row per group on the page naming it and its row count
  over every page. Order: states in their life order, priorities critical
  first, owners by id with unowned cases last, fix families by kind
  (`pages/xtm-one/ctem/cases-grouping.ts`).
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
  "No open exposure on your software", or the products that could not be read
  when `details_unavailable` is not empty, #12139), no results (filters match nothing, with
  "Clear all filters").

### 7.2bis Attention score and its points (#1176)

The list's default sort, its "Act on this first" band and the case page's
"Why this case needs attention now" read one explained score
(`frontend/src/pages/xtm-one/ctem/case-attention.ts`): the points of each
reason are added, and each reason is named in the why line. A reason whose
points are set to 0 adds nothing and is not named (the age when its points per
day or its cap is 0). Resolved cases and accepted risks score 0.

The points are the organization's, profile field `case_attention`
(`CaseAttentionWeights` in `services/organization_context.py`, an exposure
field: never in the agent block or the legacy dump), set by an administrator
with "Ranking points" on the band (a dialog, "Reset to defaults", saved with
`PUT /organization-context`). Defaults, each from 0 to 100:

| Reason | Field | Default |
|---|---|---|
| Priority critical / high / medium / low | `priority_*` | 40 / 25 / 10 / 0 |
| At least one KEV issue | `kev` | 15 |
| At least one crown jewel touched | `crown_jewel` | 15 |
| SLA breached or due date passed | `overdue` | 20 |
| Due within `due_soon_days` (1 to 90, default 3) | `due_soon` | 10 |
| No owner | `no_owner` | 5 |
| Per day open, capped at `age_max` (default 10) | `age_per_day` | 1 |

A profile saved before the field reads the defaults; every profile save of
the Settings form carries it whole.

### 7.3 Case page `/ctem/cases/:id`

- Header: the CTEM header band (`CtemPageHeader`) with the shared
  `LocationBackButton` "Back to Cases" first, above the band and outside its
  border, exactly as on an entity page (Sam, 22 Nov 2026: no CTEM-only back
  link inside the band), the fix as title, badges on the breadcrumb line
  (state Chip, priority Chip, owner Avatar, due date), actions
  in the platform order: Accept risk (while the state takes it), the next
  state as the filled primary ("Start", "Resolve", "Reopen"), then the
  "More actions" kebab (`EntityHeaderMoreActions` `menuOnly`, every width)
  with every other case action (#1751): Assign owner, Set due date, the other
  valid transitions (Resolve before remediation, Start), and Merge into another
  case (a searchable picker of the other open cases, `POST /ctem-cases/merge`,
  then the page of the case merged into). An action the state refuses stays
  listed, disabled with its reason ("Accepted risk: reopen first"). One action
  set for the bulk bar and the page: `ctem/case-actions.ts` (scope `both`,
  `bulk` or `single`), the dialogs in `useCaseActionDialogs`
  (`ctem/CasesBulkActions.tsx`); `case-actions.test.ts` fails when the two
  drift apart. Split stays in the Issues table selection.
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
- Who works the case (#1266, replaces the per-section sentence of #1206
  slice D): one card at the top of the case body (`ctem/CaseWorkCard.tsx`),
  read from live state (`ctem/case-workers.ts`), never assumed. The reader
  lists every assignment that can start on THIS case, whatever fleet or agent
  owns it (the built-in CTEM loop fleet is one way, never the default): its
  platform event trigger names `ctem_case.opened` or `ctem_case.reopened` (an
  automation on `ctem_case.resolved` or the SLA event alone continues a case,
  never starts it), and its `trigger_config.filter` takes the case event
  payload, evaluated with the backend's filter grammar
  (`integration_event_filters.payload_filter_reason`) on the fields the page
  knows (`case_id`, `object_id`, `object_type`, `trigger_path`). The two
  seeded case automations route by `trigger_path` (`in` and `not_in` the
  threat paths), so a case lists exactly one of them. A key the page cannot
  read (`vertical`, the state at the next event) or an operator it does not
  evaluate (`matches`) never drops an automation: the page refutes only what
  it proves (UX round 1 on #1275, B1). The reader also reads the runs the
  case events started on this case
  (`GET /fleets/case-runs/{id}`). First match wins: a run active on the case
  shows "Worked by <agent> (<fleet>)", queued or running since, and "Open
  the run"; the newest run failed shows the same line in the error colour
  with the run; an enabled automation shows "Worked by" and "Waits for the
  next case event"; automations that are all off are named with their agent
  and fleet and a "Switch on" button; nothing at all is one elevated empty
  state (icon, title, explanation, centred) with three choices on equal
  footing: Use the built-in CTEM loop (the guided setup for an administrator
  of XTM One, else the fleet page), Use your own fleet or agent (pick an
  agent; its page opens the assignment panel preset on `ctem_case.opened`
  and `ctem_case.reopened`, `?add-assignment=ctem-case`), Work it yourself
  (Start). The card is not shown on a resolved or accepted-risk case. Without
  agentic AI or on a read error nothing is claimed and the card is hidden.
  Calls to action are design system buttons; no inline link in the secondary
  colour.
- Loop sections, not started (#1266): How it would be exploited, Analyst
  review, Validation, Remediation decision and Treatment plan each say what
  they will hold and the product they are written from (OpenCRQ for the
  exploitation reading and the treatment plan, OpenCTI for the analyst
  review, OpenAEV for validation) with its logo and connection state, and a
  "Connect <product>" button when it is not registered (`CaseSectionReason`).
  No section repeats who works the case. A case is opened only by a signal
  or split into its own case by a person or an agent (#1529): "Opened by"
  lists the signals, and on a case with none says "Split into its own case
  by a person or an agent" (a `created` event to `manual`) or, for a case a
  source read built before the readers recorded signals, "Opened by a source
  read before signals (legacy)". Legacy cases are labelled, not backfilled:
  their members carry no reference that maps one signal per case for every
  viewer.
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

### 7.6 Cases in motion on the command center and the stage pages (#1621)

`GET /ctem-cases/overview` (`services/ctem_cases_overview.py`) rolls up the
cases the caller sees, with the list's visibility rule, so each count equals
the list it links to: `counts` by status, `open`, per CTEM phase (`phases`,
keyed by `ctem_case_audit.PHASES`) the open and waiting cases, the counts by
status and the five open cases that need attention first (waiting for a
decision, then priority, then the oldest status; each with the crown jewel it
touches among the issues the caller sees), `no_phase_open` (open cases with no
event yet: in the band, in no stage card), `waiting` (the open cases waiting
for a decision with their gate, the oldest one and its age in hours) and
`funnel` (`in_case`: distinct OpenCTI vulnerabilities, the funnel's unit,
carried by an open case; `resolved_recent`: cases resolved in the last
`resolved_window_days`). Three reads whatever the number of phases: the
cases, the viewer's view, the history of every visible case. No organization
context: every figure 0.

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

Threat package state from the live graph (#1547): a threat package reads `validated` only when its stored page is the Grouping page, and that page is stored only once the Grouping resolves in OpenCTI's live graph. `update_ctem_case` `threat_package` runs `ctem_threat_package_live.verify_package` with the acting user's own OpenCTI access (`_user_creds`, never a system token): a live Grouping keeps its page, a Grouping found inside an open draft is recorded with that draft's page (`drafted`), one found nowhere or unreadable is refused and nothing is written. A package with no page reads `drafted` too. Packages stored before the check: `POST /api/v1/ctem-cases/threat-packages/repair` (administrators, `{"dry_run": true}` by default answers the count and the cases) re-reads each case's newest package stored with a Grouping page with the caller's access and writes a new `threat_package` event with the draft page (Grouping in an open draft) or no page (Grouping absent); a live or unreadable one is left (`unreadable` counts the latter).

Admin delete of cases (#1723): `DELETE /api/v1/ctem-cases/{id}` (`dry_run` query, default false) and `POST /api/v1/ctem-cases/bulk-delete` (`case_ids` 1-200 and/or a filter `states`, `opened_by` `legacy` | `signal` | `split`, `vertical`; `dry_run` default true; `reason`), administrators only (403 otherwise). `services/ctem_case_delete.py`: the case row goes with its members and history (`ON DELETE CASCADE`); every non-terminal work item and every armed timer whose correlation key holds the case id is cancelled (the case UUID names one case, so the gate, validation, remediation, mobilize, fleet `:case:` and re-test keys and the `ctem-loop-reminder` / `ctem-gate-watch` / `ctem-loop-watch` timers are all found without a key list); each signal of the case goes back to the scope gate (`case_id` cleared, state `new`, decided again with `evaluate_one`) so the scheduled sweep regroups it, and since no case owns the fix key any more, the next case for it opens fresh with an empty history. One `audit_logs` row per case (`action` delete, `entity_type` `ctem_case`, `details.case` the summary, `details.reason`). `opened_by` reads the history as the Cases page does (`case-loop-reason.ts`): `legacy` is a case with neither a split nor a signal. The OpenCTI / OpenAEV member issues of a recompute-built case are not recreated by a recompute alone (recompute never builds a case, #1529): they come back only through their signals. Agent tools in the CTEM case set: `delete_ctem_cases` (the acting person must be an administrator, `dry_run` default true) and `bulk_update_ctem_cases` (one change on 1-200 cases, each checked as `update_ctem_case`). Tests: `tests/test_ctem_case_delete_postgres.py`, `tests/test_ctem_cases_api.py`. On the Cases list (slice 2 of #1723): each row of `GET /ctem-cases` carries `opened_by` (the same `origin` reading), the list's "Opened by" filter (`c_f_opened_by`: A signal, A split, Legacy); an administrator gets a Delete row action and a Delete selection action (`CasesDeleteAction.tsx`, drawn for no one else, no actions column for a non-administrator): the click asks `bulk-delete` with `dry_run` first, the confirm dialog shows the count and the dry-run summary (cases, open work items cancelled, timers cancelled, signals sent back to the gate) and the delete is typed back with the challenge code.

Threat package state from the review gate (#1573): the page alone never makes a package `validated`. `ctem_cases_view.review_outcome` reads the case's newest `ctem.analyst_review` gate event: `passed_automatically`, an answer by the analyst's mark, or an answered option whose WHOLE text is an approval (approve / accept / validate / publish, optionally followed by "and request coverage", "it" or "the package") is `approved`; an answered option whose whole text is a refusal (reject / refuse / deny / decline, optionally followed by "it" or "the package") is `rejected`; a leading word is never enough, so "Yes, but split the package first" or "No problem, publish it" stays `replan`; any other answered text (free text, another option) is `replan`; `waited` is `waiting`; an answer with no option recorded is `unread` and leaves the page rule alone. `threat_package_state(package, review)` answers `awaiting_review`, `replan` or `rejected` for those three whatever the stored page, else the #1547 page rule (`validated` or `drafted`); a case with no review gate keeps the page rule. A refusal or a re-plan stops only the package recorded before or at its answer (#1677): `ctem_cases_view.package_review` (what `threat_package_for` passes) answers no review when the case's newest `threat_package` event is newer than the newest `ctem.analyst_review` gate event and carries the draft page or no page, so the package the loop re-planned under the answer reads `drafted` (its `coverage_drafted` true when it has a coverage id) until a new review gate is written, which then decides as above; a Grouping page recorded after such an answer keeps `replan` or `rejected` (no approval covered it, and `verify_package` refuses it at record time). An option that names a retry in draft ("Retry in draft") is a re-plan, not an approval: only a whole-answer approval matches `approved` (#1576), and the new draft it produces reads `drafted` by the rule above. The repair route keeps `review_outcome` on purpose: it corrects only Grouping pages, which this rule still holds stopped. `coverage_requested` stays true for `validated` only; `coverage_drafted` and `draft_overdue` cover `drafted` and `awaiting_review`. `verify_package(..., review)` reads an open draft first while the review is not approved: the draft page is recorded, a Grouping found only live is refused (its page was never approved). The repair route reads the case's gate events too: a non-approved case's Grouping page becomes its draft page, or no page when the Grouping sits in no open draft; each corrected case names its `review`. The case page's Analyst review card shows `replan` and `rejected` as their own stages.

Attack path and blast radius (CTEM-23-UI, #1289): `get_opencrq_attack_path_reach` (`providers/opencrq/risk.py`) reads OpenCRQ's `supportingAssets.getAttackPathReach` for one supporting asset: the hops nearest first (each proven by an OpenAEV execution or inferred, with its simulation), the assets reached with their depth and criticality, and the primary assets behind them; hops unconfirmed past OpenCRQ's stale delay are left out. The triage analyst calls it on the matched asset with the highest impact and the orchestrator writes it with `update_ctem_case` `attack_path` (`ctem_case_loop.record_attack_path`: origin required, a hop with no end dropped, a nature other than proven read as inferred, capped at 40 hops and reached assets and 20 primary assets, replaced whole). Column `ctem_cases.attack_path` (JSON, `{}` until the first read; migration `c1289ctemattackpath1`). One `attack_path` case event per change, its evidence the counts alone. A hop also keeps `simulation_name`, `simulation_url` (an absolute http(s) page, else dropped) and `seen_at` (the ISO date of the execution, from OpenCRQ's last reported date). The case detail serves `attack_path` like the exploitation reading: asset names and the simulation name and page only to a viewer who reads OpenCRQ, else `Restricted`; the raw simulation id is never served; `null` before the first read. The case page shows the card "Attack path and blast radius" under "How it would be exploited" (`ctem/CaseAttackPath.tsx`): the origin as "Attack starts from", each hop with a Proven chip (high tone) or an Inferred chip (info tone), and for a proven hop "Proven by <simulation name>, <date>" (or "Proven by an OpenAEV simulation, <date>"), linked to the simulation page when there is one (UX round 1 on #1296).

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

### 8.1 Read-only on any agent: the `ctem_read` group

Any agent can be granted the reads through the built-in group `ctem_read`
(`backend/app/services/builtin_tools/ctem_read.py`): `list_ctem_signals`,
`list_ctem_cases` and `get_ctem_case`, no write function. Bound like any
other suite, wired by `ctem_read_tools_for` in `agent_chat` and
`assignment_engine` when a person is acted for, XTM One mode only. The rules
are the case tools' own: the case renders through that person's source read,
a case they see none of does not exist. A built-in CTEM agent that already
carries a function keeps its own; the group adds only what it lacks.
`get_ctem_case` there also answers `opened_by` (actor, time and how, from the
oldest `created` event), `history_total` and `empty_sections` (the reason each
empty section is empty). `list_ctem_cases` and `get_ctem_case` answer the
same `scope` block as `list_ctem_signals` (`dev-docs/ctem-signals.md`
section 5, #1359) for the signal members of a case, read by the case view's
rule (`ctem_cases_view.signal_members`, the admin flag alone); under `own`,
`hidden_by_scope` counts the case-bound signals the person does not see:
across the organization for `list_ctem_cases`, bound to that case alone for
`get_ctem_case` (its note then says "on this case").
Tests: `tests/test_ctem_read_tools.py`.

### 8.1bis Signals and cases on any agent: the `ctem` group

The built-in group `ctem` (`backend/app/services/builtin_tools/ctem.py`)
offers every signal tool (`record_ctem_signal`, `record_ctem_signals`,
`list_ctem_signals`), `triage_ctem_signals` (dismiss, restore or re-check, the
`POST /ctem-signals/triage` rule) and every case tool to any agent it is
bound to, wired by `ctem_tools_for` in `agent_chat` and `assignment_engine`
after `ctem_read`, XTM One mode only. The reach is the acting person's CTEM
right (`services/ctem_access.py`, #1748): an administrator or a `decide` right
reads and acts on the organization's signals and cases, a `view` right reads
them and every write is refused with the view-only message, anyone else works
on the signals they recorded and the cases their own read shows.
`triage_ctem_signals` reaches exactly what `list_ctem_signals` reads
(`reader_scope`, agent reach): an administrator kept to their own by strict
admin access for agent tools or the Platform Health reach triages only their
own signals. Administrator
operations (`delete_ctem_cases`, the `ctem_repair` group) stay administrator
only. A function the agent already carries (by its built-in slug or through
`ctem_read`) stays its own; the group never offers it twice.
Tests: `tests/test_ctem_group_tools.py`.

### 8.2 Administrator repairs: the `ctem_repair` group

Three administrator routes have agent tools in the built-in group
`ctem_repair` (`backend/app/services/builtin_tools/ctem_repair.py`), the same
service functions and answers as the routes:

| Tool | Route |
| --- | --- |
| `list_ctem_attack_surface_passes` | `GET /ctem-signals/attack-surface/passes` |
| `repair_ctem_attack_surface_new_hosts` | `POST /ctem-signals/attack-surface/repair-new-hosts` |
| `repair_ctem_threat_packages` | `POST /ctem-cases/threat-packages/repair` |

The group is wired for the built-in CTEM case agents (section 8) and for any
agent it is bound to, through `ctem_repair_tools_for` in `agent_chat` and
`assignment_engine`, in XTM One mode only. It acts for the person the turn or
run acts for, who must be an administrator, as on the routes; anyone else gets
that fact and nothing runs. A repair defaults to a dry run that writes nothing
and answers the counts and the ids; `dry_run` false applies it. There is no
approval card: the agent reads the dry run and decides.
Tests: `tests/test_ctem_repair_tools.py`.

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

**Proof.** `POST /api/v1/ctem-cases/{id}/validations` (any caller who sees the case) records a validation reference and sets the validation plan. `passed` or `not_exposed` resolves (`fixed` or `not_exposed`); with `mitigation_only` it goes to `mitigated_watching` and the re-test stays on; `failed` reopens a resolved or watched case to `needs_attention`; `pending` only records. A case with a validation plan resolves only on a passing newest reference (the PATCH route refuses with 422) and never on absence at source in a recompute, unless a person decided otherwise (#1675): a person's own move, or an agent's move once a person answered the case's `ctem.close_without_retest` decision (a `gate` event, `from` that key, `to` `answered`; `ctem_cases_store.close_without_retest_answered`). **A person's answer never moves the case by code (#1675)**: `ctem_case_status.after_gate` keeps only the bookkeeping (waited parks the case in `waiting_decision`; a gate that passed without a person moves it to `GATE_NEXT`); approve, reject or a free-text answer, the Gate answered run reads `payload.answer` and sets the next state itself with `update_ctem_case`. A case with no plan keeps the accepted proof of its kind as evidence (ticket done, re-scan, directory check). Accepted risk keeps its 90-day ceiling.

**Remediation evidence on a patch or upgrade case (#1500).** A reference carries `evidence_type`: `remediation` (a version inventory showing every in-scope endpoint at or above the fixed build) or `validation` (a detection or behaviour test). On a `patch` or `upgrade` case only remediation evidence resolves; a validation pass is recorded with `conclusive` false and the case stays open. When the writer does not say, a provider re-scan or a conclusive check (the fixed version read) counts as remediation, anything else as validation.

**No inject classification (#1810).** The platform never classifies an inject, never writes a gate's title or recommendation, and never derives `conclusive` from what an inject is. The agent reads the inject like an analyst (template, payload, targets), says in the gate what it will do to which targets and what its result will and will not prove, recommends, and sets `conclusive` itself on `record_ctem_case_validation`; the only control is the fleet's gate `ctem.validation.non_readonly` at its autonomy level (the key is kept: stored decisions, decision-item keys `ctem:validation:` and `ctem:f2:validate:`, autonomy pins and the case page read it). The former tool `assess_ctem_validation_inject` and the fields `safe_mode` and `inject_class` are gone; old validation entries that carry them stay stored as written and are no longer rendered.

**Schedule.** The autonomous tick runs `ctem_case_loop.sweep` once per `CTEM_CASE_SWEEP_SECONDS` (default 3600, minimum 60, a Redis slot): opens the cases of confirmed signals still waiting (a failed opening is retried here; each signal opens in its own savepoint, so one that fails is logged, counted `skipped` and left `confirmed` without stopping the pass) and escalates every open case past its SLA once. A case key or title longer than its 300-character column is bounded: the title is cut, the key keeps its head and a digest of the whole so two long subjects never share a case.

**Events (platform-wide, ids and facts only, no title).** `ctem_case.opened`, `ctem_case.reopened`, `ctem_case.resolved`, `ctem_case.sla_breached`, `ctem_case.stalled` (an open case that waits on nobody past `CTEM_CASE_STALL_HOURS`, a `stalled` case event written with it, `services/ctem_case_stall.py`), raised after the commit with a stable event id, by every writer of a case: the loop (signals, validations, sweep), the source recompute, a manual split and the backfill of section 4.3 (#1206); filter keys `case_id`, `state`, `severity`, `trigger_path`, `remediation_kind`, `validation.provider`, `validation.result` and the rest of `_CTEM_CASE_KEYS`. CTEM-04 starts fleet runs from them; CTEM-15 maps OpenAEV deliveries to the validation route.

**OpenAEV results move the case (CTEM-15, #918).** The seeded assignment "OpenAEV results into cases" of the vulnerability fleet provisions the OpenAEV triggers `simulation_finished`, `finding_updated`, `finding_deleted`, `scenario_score_degraded` and `critical_asset_reached` (optional: an OpenAEV that refuses it keeps the others, its state lists it under `unsupported_events` and the next check retries); its filter drops every delivery, so it never runs. On a verified delivery, the webhook route (`services/ctem_openaev_events.py`) finds the cases that wait on the delivered object (an `openaev` validation reference whose `ref` names it, or an `openaev` member with that id; states `needs_attention`, `in_progress`, `mitigated_watching`, `resolved`), writes one `source_event` history entry per case and raises `ctem_case.validation_finished` once per case and occurrence, with `openaev {event, resource_type, resource_id, platform_url}` on top of the case routing; like the other case events it is in `VIEWER_CHECKED_EVENTS`, so only a run whose user reads the case hears it (#933). Both loop fleets hear it (routed by `trigger_path`), read the result in OpenAEV and write it with `record_ctem_case_validation`, which resolves or reopens the case. No poll, no model call before the run. A `scenario_score_degraded` delivery records a path 8 (`mitigation_regression`) signal through the signal table; an `asset_stale` delivery (an `ASSET` update trigger filtered on `asset_status` = `STALE`, raised once by OpenAEV's freshness job when an asset stays unseen past the tenant's `asset_stale_after_days`, CTEM-27) records a path 6 (`stale_asset`) signal, outside lens, the asset as subject. OpenAEV sends it only to readers of the asset in the trigger's tenant; XTM One records it as a `system` signal, which only administrators read. A `critical_asset_reached` delivery (CTEM-13: a finished simulation's attack path reached a high or very high criticality asset) records a path 4 (`proven_exploitable`) signal with the simulation as subject and its OpenAEV link, and moves the cases waiting on that simulation like `simulation_finished`; the case history names it "Critical asset reached". OpenAEV names no asset in it: the reached assets are read in its attack path with the reader's own rights.

**Gate order (#1606, #1675).** A case moves scoping -> validation -> mobilization, one person decision at a time; the order is the orchestrator's judgement, stated in its persona. `services/ctem_gate_order.py` acts at the agent's work item write (`require_person_ask`, the `create_work_item` / `update_work_item` tools): (1) a `ctem.mobilize` item (`ctem:f1:case-mobilize:`, `ctem:f2:mobilize:` + case id) parked for a person while a `ctem.analyst_review` or `ctem.validation.non_readonly` item of the same case (`ctem:review:`, `ctem:validation:`, `ctem:f2:validate:`) still waits is written with a warning in the tool answer (`mobilize_before_review_warning`), never refused (#1675: ordering by code removed); two card-hygiene rules still refuse, because they protect what a person reads: (2) the question of a waiting gate item is never changed in place: the item is cancelled with a note naming its replacement and a new item is filed, so the stored decision and the cards sent keep their question; (3) a gate card never points to a run, a trace or a transcript: the evidence is in its context. Autonomous mode files no gate item, so nothing holds it. A person's own write and a system write are not checked. The doctrine the orchestrators apply (doubt moves forward, mobilize on unproven validation, deterministic closure of stale items with the reason, a stated outcome per phase) is in their personas.

**A person's answer resumes the case, whatever its key.** The seeded "Gate answered" listener of each CTEM loop (`agents/gate_answered.py`, `builtin_agents._gate_answered_assignment`) hears `work_item.status_changed` from blocked to open on every own item keyed `ctem:` whose event carries `answered: true`. Only `work_item.answer_work_item` sets that flag (`publish_transition(answered=True)`), so a decision the loop keyed outside its gate catalog (an evidence request such as `ctem:f1:version-read:`, an operational blocker such as `ctem:f1:jira-reauth:`) resumes the case, and a dependency release of a case or re-test item wakes nothing. The catalog `CTEM_F*_GATE_PREFIXES` still names the gates the case page draws and the personas file. Answers of different decisions run side by side: every seeded "Gate answered" listener names `max_concurrent_runs` (`GATE_ANSWERED_CONCURRENCY`, 5) over the one-run default of seeded event triggers, and `ordering_key: correlation_key`, so two answers on one decision key run one at a time in arrival order; the seed rewrites the trigger of an existing install at boot. The "Case opened or reopened" listeners of every case fleet and the loop's "Loop reminder due" do the same (#1923, `CTEM_CASE_LISTENER_CONCURRENCY`, 5): case events ordered by `case_id`, reminders by their timer key `ctem-loop-reminder:CASE_ID`, so one case's events run in order and another case's never wait behind them.

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
