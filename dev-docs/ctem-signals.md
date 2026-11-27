# CTEM signals: one intake for every trigger path

Issue #826. First brick of the CTEM loop spine: signal, reconcile, scope gate,
case, validate, decide, mobilise, re-test, close and feed back. Cases
(`ctem-cases.md`) are built today only from OpenCTI `has` relations and
OpenAEV CVE findings; a signal is the way in for every other change in
exposure.

## 1. Why

The trigger of a CTEM loop is a change in exposure, not only a new CVE. Eight
of the twelve trigger paths carry no CVE and no actor. Exposure is seen
through three lenses: inside (what runs, where, which version, which
configuration), outside (what the Internet reaches) and world (advisories,
intelligence, leaks, suppliers). A trigger is a delta: a lens moves, or two
lenses disagree. Before this change none of that had a place to land.

## 2. The twelve trigger paths

| # | `trigger_path` | Default lens | Listener today |
|---|---|---|---|
| 1 | `new_vulnerability` | world | OpenCTI Vulnerability created or updated; OpenCTI `has` relationship to a Vulnerability (lens inside) |
| 2 | `relevant_threat` | world | OpenCTI Intrusion Set, Threat Actor, Campaign or Malware created or updated |
| 3 | `code_flaw` | inside | Aikido `issue.open.created` (and its open-source and SAST variants), `issue.sla.breached` (re-sights the issue's signal), `ci.gate.failed`, `scan.image.finished` with issues; GitHub `dependabot_alert` created, reopened, auto-reopened or reintroduced (#1331) |
| 4 | `proven_exploitable` | inside | OpenAEV `critical_asset_reached`: a finished simulation's attack path reached a high or very high criticality asset, the simulation as subject (CTEM-13); tool and API |
| 5 | `new_exposure` | outside | tool and API |
| 6 | `stale_asset` | outside | OpenAEV asset turned stale (CTEM-27), tool and API |
| 7 | `surface_drift` | outside | tool and API |
| 8 | `mitigation_regression` | inside | OpenAEV scenario score degraded (CTEM-15) |
| 9 | `control_decay` | inside | tool and API |
| 10 | `credential_leak` | world | Aikido `leaked_secret` issue; an OpenCTI `user-account` or `email-addr` observable with a domain, the identity as subject (the secret is never stored: a fingerprint) |
| 11 | `third_party_event` | world | an OpenCTI report or incident naming a tracked organization by identity, one signal per organization (the tracked-organizations trigger of the third-party fleet) |
| 12 | `blind_spot` | inside | tool and API |

Path 9 (`control_decay`, evidence state `never_measured`, `attested_only` or
`expired`) means OpenCRQ holds no current evidence for a control, not that no
control exists (#2354): the Loop Orchestrator reconciles the case first
(`CTEM_CONTROL_DECAY_RECONCILE` in its case prompt, the section of its persona
of the same name). It reads the OpenAEV results on the asset, the EDR and
device-management coverage the inventory holds (`assets_exposure`, filtered by
`source:`), and `get_opencrq_control_validation`; a control found that OpenCRQ
lacks or under-rates becomes one `ctem:remediation:<case id>` decision to record
it in OpenCRQ, none found keeps the decision to put a control in place, and the
decision lists every source checked with what it returned.

What measures the control (#2369, `CTEM_CONTROL_MEASUREMENT_RULE`): the loop
chooses it from the asset type and the tenant's live integrations, read at
decision time, a permanent control first: (1) a control an integration already
offers for the asset (a repository with Aikido: enable Aikido scanning; an IP
or host with a vulnerability scanner such as Tenable, Qualys, Rapid7 or Wiz:
permanent scanning; an endpoint with an EDR: its policy or coverage); (2) a
recurring OpenAEV scenario at a period below `evidence_health.max_age_days`,
with DETECTION or PREVENTION expectations on a covered technique or OpenAEV
generic vulnerability scanning (CVE output, which OpenCRQ books); (3) a one-off
only when neither fits, with the reason (its result expires and the pass opens
the case again). A port-only scan with no expectation and no CVE output reaches
OpenCRQ as nothing: never the measurement, never reused as evidence. Several
options may be offered, the recommended one first, each saying permanent or
one-off and what OpenCRQ will and will not update; nothing applicable at all:
"no measuring source connected" and the integration to connect, no scan. The
platform puts a line first on the `ctem:validation:<case id>` card of a
`control_decay` case stating permanent versus one-off and what OpenCRQ updates,
the re-run period applying to a recurring option only
(`ctem_control_measurement.complete_gate_ask`, called from the work item write,
from the case and the profile's re-test delay).

A `never_measured` case (#2461) closes only on a first measurement OpenCRQ books
for the asset: a DETECTION or PREVENTION expectation result on a covered
technique, or CVE findings from a scan that reads versions. A clean
unauthenticated scan has nothing to book: its pass is recorded `inconclusive`
and never closes the case. Each option of the gate says whether its pass
"closes the case" or "does not close the case"; a non-closing option is never
recommended, and when nothing on the instance can close the case the first and
only decision is the structural one (connect a version-reading or measuring
source), any interim scan offered inside it as non-closing. The card of such a
case carries `never_measured_line` instead of the general line, and the
platform moves a recommendation that sits on a non-closing option to the first
option that closes the case (`closing_recommendation`), on every write of the
card. The case is recognised by its platform title
(`ctem_case_loop._CONTROL_DECAY_TITLES`).

A caller may name another lens than the default (a `has` relationship is the
inside lens of path 1). `GET /api/v1/ctem-signals/paths` answers this table.

OpenCTI `has` relationship and sighting of a vulnerability (#2524): the signal
is named by the target's value (the OpenCTI extension's `target_value`, a CVE
id), else by the CVE id the event message names, and by the target's STIX id
only when neither exists. The STIX id stays in `evidence.target_ref` and the
CVE goes to `evidence.cve`, so the same CVE delivered as a vulnerability object
and as a relationship is one subject (related signals, case key, KEV/EPSS).
The dedupe key is unchanged (the relationship's internal id). Migration
`r2524relcve01` renames the earlier rows the same way.

Aikido (CTEM-25): the code repository and container image of an issue (name
and id) go to `evidence.assets`, so the scope gate resolves them to a profile
asset row (source `aikido`, `external_id` the Aikido id) and to its primary
asset by the existing asset names; an unmapped repository is `to_validate`.
A dependency issue that names its fixed version opens an upgrade case keyed
and titled on that version (`upgrade:-/<package>@<version>`). An
`issue.closed` records no signal: it closes the case member of the signals the
issue recorded, and the last member closed resolves the case on a passing
validation of kind `rescan` (`ctem_case_loop.close_signal_members`).

An asset the inventory retires closes its cases too
(ctem-autoresolve-archived-asset, `ctem_case_asset_retire.close_retired_asset_cases`,
run by the scheduled case sweep): a signal member whose `asset` subject names
inventory records that are all `inactive` (an archived repository, a retired
host), or a record of that name the change log shows deleted, is closed with the
evidence `asset archived` or `asset removed`. A case whose every open member is
such a signal resolves `not_exposed` with that evidence, raises
`ctem_case.resolved` and cancels its open `ctem:<gate>:<case id>` decision items.
A subject the inventory never held (an outside-lens hostname) is never retired,
and a case with a source issue or one live asset left keeps its state.

A GitHub Dependabot alert (#1331, slice 2 of the code repository sources) is
a path 3 signal keyed `<repository id>:<alert number>` (alert numbers restart
per repository). Its evidence names the package, ecosystem, manifest, vulnerable
range, GHSA and CVE ids, and writes the repository's full name, id and identity
key (`github.com:repository:<id>`, the key the GitHub asset source writes) to
`evidence.assets`, so the scope gate resolves it to the repository asset. With
a first patched version the case is an upgrade in that repository, keyed
`upgrade:<repository>/<package>@<version>` and titled "Upgrade <package> to
<version> in <repository>". `fixed`, `dismissed` and `auto_dismissed` close its
members as Aikido's `issue.closed` does. Read-only: nothing is written to GitHub.

A GitLab dependency vulnerability (#1331, slice 3) comes with the GitLab asset
source, not from a webhook: the project's dependency list
(`GET /projects/:id/dependencies`, GitLab Ultimate) names the vulnerabilities
of each dependency when the token may read the security dashboard
(`organization_context_code.gitlab_dependency_findings`). After the sync saved,
`ctem_code_findings.record_findings` records each one as a path 3 signal keyed
`<project id>:<vulnerability id>`, provider `gitlab`, with the package,
installed version, PURL, manifest, severity and link, and the repository's
path, id and identity key (`<host>:repository:<id>`) in `evidence.assets`. The
dependency list names no fixed version, so the case keys on the path and the
package (`ctem_case_loop.fix_of`), never a guessed version. A vulnerability
a project read whole no longer lists closes its members (a cut, refused or
off-tier list closes nothing). Read-only: nothing is written to GitLab.

A repository dependency known vulnerable in OpenCTI (#1331, slice 4,
`ctem_dependency_matches`): after a GitHub or GitLab sync is saved, each
repository's components that carry a PURL and a known version are looked up in
OpenCTI with the sync owner's own credential, in a session of its own after the
sync's commit. A Software whose `x_opencti_purl` names the same package (type,
namespace, name; qualifiers ignored) at the same version is the component; never
a name-only match. Each `has` relation from it to a Vulnerability is one path 3
signal (`code_flaw`, lens `inside`, subject `code`, source `opencti`, key
`<repository identity key>|<purl base>|<vulnerability id>`) on the repository
asset, with the CVE, CVSS, EPSS and KEV in the evidence. The fixed version is the
smallest Software version OpenCTI says `remediates` it above the installed one
(same PURL base when the remediating Software names one); the case then reads
"Upgrade <library> to <fixed version> in <repository>". No fix known: the case
keys on the package, nothing guessed. At most 2,000 components and 2,000
relations per sync. OpenCTI absent or failing records nothing and never stops the
sync. Read-only.

A GitHub repository security advisory the organization published on one of its
repositories (#1410) is a vulnerability in a product it ships: a path 1 signal
(`new_vulnerability`, lens `inside`, subject kind `vulnerability`) keyed
`<repository id>:<GHSA id>`. Only `repository_advisory.published` with the
advisory's own `state` at `published` records one: a private report in triage
(`reported`), a draft or a withdrawn advisory records nothing. The subject is
the CVE id when the advisory has one, else its GHSA id, so the same CVE seen
from OpenCTI lands on the same subject (related signals, one case per fix). The
evidence names the GHSA and CVE ids, severity, CVSS score, summary, advisory
link, affected packages with their vulnerable range and patched versions, and
writes the repository to `evidence.assets` as for a Dependabot alert.

## 2bis. Signal sources (the dialog of the command center)

`GET /api/v1/ctem-signals/paths` also names, per path, what fires it
(`description`), its `sources` and its `status`, plus `count_30d` and
`last_signal_at` (the reader's visibility, as the signal list). Each source
also carries its own `count_30d` and `last_signal_at` (#2436): every system
recording point stamps `evidence.xtm_source_key` with the source key
(`provider:kind[:name]`), and `activity_by_source` groups on it; a signal
recorded before the stamp counts under its provider, credited to that
provider's unnamed source only when the path has exactly one. Additive: the
former fields are unchanged. The table lives in
`services/ctem_signal_sources.py` (`PATH_SOURCES`); a test fails when a
listener row of section 2 has no source row.

| Source `kind` | What it is | `bound` when |
|---|---|---|
| `listener` | an integration delivery a listener row maps to the path | an integration of the provider is connected and visible to the reader; for the OpenCTI and OpenAEV listeners, which XTM One provisions, only when their objects exist and the last check passed (section 4bis) |
| `pass` | a scheduled pass: attack surface (Shodan, GTI; paths 5, 6, 7), evidence health (OpenCRQ; paths 9, 12) | the pass runs on a connected integration of the provider; `off` (never `bound`) when the provider is connected but the pass is off or names none of its integrations |
| `pir_playbook` | the OpenCTI PIR playbook (CTEM-05), delivered through the OpenCTI listener (paths 1, 2) | an enabled platform source holds a provisioned playbook whose last check passed (section 4bis) |

`status`, one per path, the one its sources say (UX round 1 on #2442):
`error` (a source XTM One provisions is in error on one of its integrations,
`error_count` on the source, and per integration in `errors` its `reason`,
`reason_code`, `last_attempt_at` (the last check) and `fix` / `fix_route`
(#2637); critical tone, the chip says "N sources in
error"), `enabled` (one source bound and every source used on the path, of a
connected provider and not taken off it, is on; low tone) or `partly_enabled`
(one bound, another used source off or not set up; medium tone), never
`connected`: the status reads the sources' on/off state, never the integration
connection alone (#2436, Sam 26 Nov 2026; a provisioned source whose platform
source exists switched off reads `off` on the source, and an assignment trigger
left on that integration never turns it back on), `not_set_up` (none bound, a
source XTM One provisions has its provider connected but is switched on
nowhere, `not_set_up` on the source; neutral tone, action "Set up"), `off`
(none bound, one source `off`: its provider is connected, the source does not
run; neutral tone, action "Turn on" to its card in Settings > Organization),
`not_connected` (a source exists, none bound, off or not set up: no
integration of the provider is connected), `agents_api` (no automatic source:
agents and the API only, never read as connected). In the path panel the
source row of a provisioned source reads its platform status (Off, Connected
or Error), the same word as its lines.

The evidence health pass is on by default (#1344): `evidence_health.enabled`
is unset until an administrator saves it. Unset (or the legacy stored default,
off with no integration declared) with no integration declared, the pass runs
on the single connected OpenCRQ integration; several connected and none
declared read `off` until one is picked. A declared integration is read as
declared, never swapped for another; an administrator who turned the pass off
keeps it off (the card's save of an off on the default setting names the single
connected integration, so the off holds: `evidenceHealthSavedIntegration`).
`ctem_evidence_health.resolve_pass` is the one rule of the badge
and the tick, so the badge never reads Connected when the pass would skip. The
attack surface pass stays opt-in (it needs owned domains or ranges and spends
provider credits): a connected Shodan or GTI with the pass off reads `off`.
Listeners are bound on the integrations visible to the caller; both passes
are read on the platform's connected integrations
(`ctem_signal_sources.platform_connected_ids`), because a pass is
organization-wide: a reader who cannot see the administrator's Shodan or
OpenCRQ integration reads the same `connected` or `off` as the administrator.

The Signals page (`/ctem/signals`) is a row of the CTEM menu group, between
Cases and Inventory (#1401), with the standard list header and no tabs: a
secondary "Sources" button top right of the header, with the connected count,
opens the signal sources as a page of their own (the Inventory Sources and
Platform Health settings pattern), "Back to Signals" first, then its title.
That page (`/ctem/signals/sources`, `SignalSourcesPanel.tsx`) is a compact table grouped
by lens: trigger path, sources (the event sentence in a tooltip), status,
signals in 30 days, last signal, action; rows of at most two lines. The
Sources column lists what feeds the path on this platform: every source of a
connected provider (a provider with a bound source or a pass `off` on any
path, `connectedProviders`), each with its state: bound, Off, or Not set up
(the provider is connected, this source is not, e.g. the PIR playbook of a
connected OpenCTI). A source whose provider has no integration here is folded
into one muted "+N possible" whose tooltip lists them with "Not connected on
this platform" (`splitSources`, `sourceState`); a path with no source of a
connected provider lists them muted inline, under its Not connected status.
Connect sends a listener to
`/integrations/new?provider=`; Connect or Turn on sends a pass and the
playbook to their card in Settings > Organization (attack surface: outside
lens; evidence health: its card beside it on CTEM > Inventory, whose `?card=`
link opens the edit view on that card and writes `edit=1` in the same URL, so
the view outlives the card parameter the landing drops:
`ctem/inventory-edit-view.ts`). Those two edit the organization profile,
so only an administrator gets the button (`connectNeedsAdmin`); a reader reads
the status. Below the 4xl width the sources step into line 2 of the first cell
on one line, cut with an ellipsis, the whole list in a tooltip. An empty signal list
shows the table's standard empty statement alone; the summary strip carries
the connected count.

**Managing a path's sources (#1445).** A row of the signal sources page opens the
path's side panel (`SignalPathSourcesSheet`): every source of the path,
connected or not, its state (feeds this path, not used on this path, off, not
set up, not connected on this platform) and the control that exists for it.
A scheduled pass of the profile shows its organization-wide switch (the same
`enabled` its card writes, so no second store), the integrations it runs on,
a pointer to its card when no integration is chosen or no owned domain or
range is set, and the other paths it also feeds (`also_feeds`). A live
listener has no switch in the model: it runs while its integration is
connected, and the panel says so. The PIR playbook shows its state and points
to the PIR field. A provider with no integration here gets Connect.

Every connected source has "Use on this path": the per path exclusion
`signal_sources.excluded` of the organization profile, `{path: ["provider:kind"]}`
(`SignalSourcesSettings`, validated against `PATH_SOURCES`). An excluded source
is not bound on that path (`bound` false, `excluded` true, `available` true:
its provider still reads connected), so the path status and the connected count
follow, and it keeps feeding its other paths. The recording points read the
exclusions before they record (`ctem_signal_sources.excluded_sources`,
`is_excluded`): the listener and PIR playbook delivery
(`ctem_signal_listeners.note_event`), the OpenCTI third party listener
(`note_tracked_organizations`), the evidence health pass, and the attack
surface pass, which skips a finding only excluded providers saw
(`ctem_attack_surface._kept_for_path`). Every write is a profile save, so the
controls are an administrator's; a reader sees the same states, controls
disabled.

## 3. Model (`ctem_signals`, migration `c826ctemsignals1`)

One row per `(organization_id, recorded_by, dedupe_key)`.

| Column | Meaning |
|---|---|
| `organization_id` | The organization (FK, cascade) |
| `trigger_path`, `lens` | Section 2 (check constraints) |
| `source_provider`, `source_external_id` | Who saw it (`opencti`, `openaev`, `aikido`, `manual`...) and the object's id there; for OpenCTI the internal id of its extension (#2505: the STIX `id` is the `standard_id`, which changes on a rename or a merge), the STIX `id` when the extension is absent, the STIX ids kept in the evidence (`stix_id`, `stix_ids`) |
| `subject_kind`, `subject_ref`, `subject_label` | What it is about: `asset`, `software`, `identity`, `third_party`, `vulnerability`, `threat`, `code`; a stable reference (hostname, IP, URL, CPE, CVE id, account, organization) |
| `observed_at`, `first_seen_at`, `last_seen_at`, `seen_count` | The latest sighting, the first, the latest time seen, how many sightings |
| `evidence` | JSON, at most 8,000 characters: the facts and a `link` |
| `dedupe_key` | Default `<path>:<provider>:<external id>`, else `<path>:<subject kind>:<lower-cased ref>`, hashed past 200 characters; a caller may set its own |
| `state` | `new` (written here), then `confirmed`, `to_validate`, `discarded`, `watchlist` (reconcile and scope gate, `ctem-scope-gate.md`), `case_opened` (cases from signals; a check constraint requires `case_id`) |
| `decision` | JSON written by the reconcile and scope gate: inputs, outcome, reason (`ctem-scope-gate.md` section 6) |
| `case_id` | The case the signal opened or joined (FK to `ctem_cases`, set null on delete) |
| `recorded_via`, `recorded_by` | `api`, `tool` or `listener`; the user id, or `system` for a listener |

Idempotence: `services/ctem_signals.record_signal` is one `INSERT ... ON
CONFLICT (organization_id, recorded_by, dedupe_key) DO UPDATE`. The recorder
is part of the key: a caller never reads back, nor overwrites, a row another
user or a listener recorded under the same (guessable) key; it gets its own
row, and the reconcile step merges what several recorders saw. A sighting of
a known key by the same recorder moves `last_seen_at` to the later of the stored value and its own time, adds
one to `seen_count`, merges its evidence into the stored one (#2543,
`merge_evidence`, the stored row read under a row lock) and keeps its label;
recorder, state, decision and case stay as the first sighting and the next
steps left them. The merge takes the latest sighting's facts, except that the
PIR matches (`pir`) are merged by PIR id with the highest `score_at_delivery`
kept, `pir_floor_met` is the stored value OR the new one (a sighting with no
PIR data never downgrades it, and the listener writes no floor without PIR
data), and every source key that saw the signal is kept in `xtm_sources`
(written once two sources saw it), so `activity_by_source` counts the signal
under each of them. The result stays under 8,000 characters (the lowest PIR
scores dropped first).

## 4. Three ways in

1. **Listeners** (`services/ctem_signal_listeners.py`): `LISTENERS` is the
   declarative table (provider, event keys, object types, path, lens, subject
   kind). The three webhook routes that carry integration events (provider,
   integration, integration-assignment) hand every delivery to `note_event`
   after its signature check; no model call, no run, no quota. Only a
   delivery verified against a configured `webhook_secret` records a signal
   (`verified=True`): the routes accept an unsigned delivery when no secret
   is set, and such a delivery never plants a signal nor costs a write. A provisioned
   OpenCTI trigger delivers on the integration-assignment route, so the
   signal exists only for the objects an assignment subscribes to. OpenAEV
   events (`simulation_finished`, `atomic_test_finished`, `finding_created`)
   carry no exposure verdict yet: they get a row once their payload says
   what was proven.
2. **Agent tools** (`builtin_tools/ctem_signals.py`): `record_ctem_signal`,
   `record_ctem_signals` (#1726: up to 100 rows in one call, one savepoint per
   row and one commit, one result per row with its index; a refused row is
   rolled back alone; idempotent on the dedupe key like the single tool),
   `list_ctem_signals`, XTM One mode only.
3. **API**: `POST /api/v1/ctem-signals` (answers `created` and the signal),
   `GET /api/v1/ctem-signals?state=&trigger_path=&lens=&limit=&offset=`
   (newest sighting first, `total`, `counts` by state),
   `GET /api/v1/ctem-signals/{id}`, `GET /api/v1/ctem-signals/paths`.

## 4bis. Platform signal sources (#2434)

The sources XTM One must provision in the source platform before they
deliver (`ctem_platform_sources.PROVISIONED`: the OpenAEV listener, the
OpenCTI live listener, the OpenCTI PIR playbook) are platform objects, one
`ctem_signal_sources` row per `(provider, kind, integration_id)` (model
`models/ctem_signal_source.py`, migration `s2434sigsrc01`). No fleet,
agent or assignment holds them: disabling or deleting one never stops a
source.

- **Off by default.** A fresh platform has no row; the Sources API reads a
  source never switched on as `off`.
- **Enable with a run-as.** `POST /api/v1/ctem-signals/sources/enable`
  (administrator) takes the provider, kind, integration and run-as user, and
  provisions at once as that user (`ctem_platform_sources.enable`). It answers
  the row `connected`, or 400 with the exact reason (no run-as, run-as
  deactivated, run-as without a connection to the integration, no webhook
  secret, integration not connected, organization context missing for the PIR
  playbook) and leaves no object behind in the source platform.
- **Disable.** `POST /api/v1/ctem-signals/sources/{id}/disable` removes the
  provisioned objects; a removal that fails keeps their ids and the error on
  the row, and the next pass finishes it.
- **Recheck.** `ctem_platform_sources.maintain` runs on the provisioning tick
  (`trigger_provisioning`): every enabled source is rechecked each
  `RECHECK_SECONDS`, an edited or deleted object is repaired, and a run-as
  that is gone or disconnected puts the source in `error` with that reason,
  never a silent stop.
- **Status.** `connected` only when the objects exist and the last check
  passed (`is_connected`); `off`; `error` with its reason. The path statuses of
  section 2bis follow the same rule (`ctem_signal_sources.provisioned_pairs`):
  a connected integration alone never reads Connected.
- **Receiver.** The provisioned objects post to
  `/api/hooks/integrations/{integration_id}/signal-sources/{source_id}`
  (`webhooks.receive_signal_source_webhook`): integration `webhook_secret`,
  per-source throttle, delivery de-duplication, then `note_event` records the
  signal. A source that is off or unknown is ignored. No run starts here.

## 4ter. Generic signed webhook sources (#2506)

A tool XTM One has no integration for records signals on one trigger path
through a webhook source (`ctem_webhook_sources`, model
`models/ctem_webhook_source.py`, migration `w2506webhooksrc01`). It is a
source of its path in the signal sources view: kind `webhook`, provider
`webhook`, key `webhook:webhook:<id>`, listed with its 30-day count.

- **Off by default.** `POST /api/v1/ctem-signals/webhook-sources`
  (administrator) creates the source off, with its name, path, subject kind
  and mapping; `PATCH .../{id}` switches it on or off, renames it or changes
  the mapping; `DELETE .../{id}` removes it (its signals stay).
- **Per-source secret, shown once.** The create and
  `POST .../{id}/rotate` answers carry `secret` once; it is stored encrypted
  and never read back. A rotation refuses the old secret at once.
- **Signed receiver.** `POST /api/hooks/ctem-webhook-sources/{id}`
  (`webhooks.receive_ctem_webhook_source`) checks `X-XTM-Signature:
  sha256=<hex HMAC-SHA256 of the raw body>` before anything else: a wrong or
  missing signature answers 401 and records nothing, the source on or off. A
  verified delivery to a source that is off answers `ignored`.
- **Mapping.** `mapping` names the dotted path (`event.id`, `items.0.name`)
  of the signal fields in the posted JSON: `id` (required, the signal's
  external id and dedupe key), `label` (the subject label), `severity` (kept
  in the evidence). A delivery with no id at its path answers `unmapped` and
  keeps the reason on the source (`last_error`).
- **Test.** `POST .../{id}/test` with a sample answers what the mapping reads
  in it, recording nothing.
- **Counts.** Every path of `GET /api/v1/ctem-signals/paths` carries
  `sources_enabled` (its sources on) and `sources_available` (its sources that
  are or could be on here); a source of a provider with no integration here
  counts in neither.

**Fleets route on signals.** The F2 triage assignments of the vulnerability
prioritization orchestrator (New KEV or EPSS jump, Vulnerability on software
we run, Vulnerability flagged in a PIR, Vulnerability sighted) are
`platform_event` triggers on `ctem_signal.recorded`, scope all, filtered on
`trigger_path`, `source_provider` and one routing fact each (section 6bis).
They provision nothing.

**Migration, no breaking change.** `adopt_from_assignments` runs first in
every provisioning pass. For each enabled built-in holder
(`HOLDER_ASSIGNMENTS`) whose objects exist, the source of the same
integration is enabled with the holder's run-as and provisioned. The holder's
own objects stay until that source is enabled and connected
(`replaced_by_source`), then its provider pass removes them: new before old,
no delivery lost. A refused adoption stays off with its reason and is tried
once (`adopted_from`).

## 4quater. The inventory evaluation of path 6 (#2642)

`services/ctem_inventory_staleness.py` evaluates the inventory records of the primary organization as they stand: a record whose `last_seen_at` is older than `attack_surface.stale_after_days` (14 by default) is `unseen`, a record whose `sync_id` names a sync that no longer exists is `orphaned`. A record of a sync whose last run failed or that paused itself is withheld and counted (`withheld_sync_failed`): its age is the sync's failure. Each finding is recorded through `record_signal` (source `xtm_one:pass`, dedupe `stale_asset:xtm_one:<detail>:<record id>`). It runs daily on the autonomous tick (Redis slot `ctem:inventory-staleness:pass`) and once in the resync step of a purge (`resync_passes`); the source is always on and can be taken off the path like any other.

The last outcome is kept (`ctem:inventory-staleness:last`, 30 days). `GET /ctem-signals/paths` answers, on every path that is on with no signal in 30 days, `silent_reason` `{code, detail, evaluation?}`: `never_evaluated`, `no_asset_past_threshold`, `upstream_sync_failed` or `inventory_truncated` (the inventory holds more records than one evaluation reads, 100,000 in pages of 5,000 keyed on the record id; the outcome counts them as `truncated`) for path 6, `listener_never_fired` for a path no source ever fed.

## 5. Visibility

Signals are organization data; their content is what the recorder chose to
write, not a mirror of a source product's object (cases render per viewer,
`ctem-cases.md` principle 8; signals do not, so they never copy a source's
restricted fields). An administrator reads every signal of the organization;
anyone else reaches CTEM only through a right one of their groups grants
(#1748, #2441, `services/ctem_access.py`, the one reader of
`groups.ctem_access`). Without a right there is no CTEM at all: every signal
and case route answers 403 (a router dependency), no CTEM tool is wired in
their turns or runs (`ctem_access.wires_ctem`), the `ctem_signal.*` and
`ctem_case.*` events are neither offered nor saved in their triggers
(`platform_events.ctem_event_error`), and the menu hides the section. There
is no own-signals fallback. Two groups are seeded in XTM One
(`services/ctem_groups.py`, found by `groups.seed_key`): "CTEM Viewers"
(`view`; Auto join on, #2478: every active account at its first seed, every
account created afterwards once, at its creation; the single sign-on auto join
skips it (`ctem_groups.JOINED_AT_CREATION_KEYS`), so an administrator's removal
holds; migration `t2478viewersaj01` turned the toggle on once on platforms
seeded before, and nothing turns it on again, so a toggle-off holds) and
"CTEM Operators" (`decide`, empty). `view`
reads every signal and case of the organization as an administrator does and
writes nothing (a triage or a case write answers 403, a tool answers
"Refused"); `decide` also triages the signals and changes the cases.
`groups.ctem_notify` (on by default) is the switch of the gate notifications
of a `decide` group. Two organizations never share a row (the unique key holds the
organization).

An agent tool takes the administrator's reach through
`agent_tool_admin_bypass`: strict admin access for agent tools and the
Platform Health reach keep an administrator to their own signals. A CTEM
right of the person the run acts for gives the organization's reach in the
tools too (`scope.ctem_right` names it). Every
`list_ctem_signals` answer says which rule applied (#1359), so an empty page
is never read as an empty organization: `scope` holds `applied` (`own` or
`organization`), `acting_user`, `is_admin` and `reason` (`administrator`,
`not an administrator`, `strict admin access for agent tools`, `Platform
Health reach`); under `own`, `hidden_by_scope` counts the signals the same
filters match beyond the person's own (a count only, no content) and `note`
is the line the agent repeats. Who reads what is unchanged.

## 6. Agents

The tools are offered to the built-in `ctem-assistant`,
`ctem-discovery-analyst`, `ctem-vuln-prioritization-orchestrator`,
`ctem-vuln-triage-analyst`, `ctem-threat-response-orchestrator` and
`ctem-threat-relevance-analyst`, acting for the person the turn or run acts
for. No persona or seeded assignment uses them yet: the fleets are rewired on
signals and cases in a later step of the loop.

`list_ctem_signal_sources` (#2637) answers every path with its `status`,
`sources_in_error` and each source's `state` (`error`, `on`, `off`,
`not_used_here`, `not_configured`, `not_set_up`, `not_connected`), an error
carrying its reason, last attempt and fix: the status and text of the Signal
sources page, so a monitoring agent reads why a source is in error. A source
switched off after a refused enable for a missing setting (no run-as, run-as
without the integration, no webhook secret, organization context missing;
`ctem_platform_sources.CONFIGURATION_CODES`) is `not_configured`, never
`error`, and counts in no path's errors.

Any other agent reads the signals through the read-only built-in group
`ctem_read` (`list_ctem_signals` with the same visibility as section 5, plus
the case reads; `dev-docs/ctem-cases.md` section 8.1). It never records a
signal.

## 6bis. Signal events and the CTEM signal trigger (#1409)

Two platform events (`services/platform_events.PLATFORM_EVENTS`, listed by
`list_platform_events`), never one per sighting:

| Event | Raised when | Raised by |
|---|---|---|
| `ctem_signal.recorded` | the first sighting of a signal, after the scope gate gave its first verdict (or none when the gate did not run) | `ctem_scope_gate_store.evaluate_after_record` |
| `ctem_signal.triaged` | a later evaluation changes a verdict the signal already had (`confirmed`, `to_validate`, `watchlist`, `discarded`) | `ctem_scope_gate_store.evaluate` with `events`, from `POST /ctem-signals/{id}/evaluate` and `POST /ctem-signals/evaluate` |

A re-sighting raises nothing; the first verdict of a new signal is carried by
`recorded`, never a `triaged`. Both are queued in `ctem_case_loop.PendingEvents`
and published after the commit, `recorded` ahead of the case events of the
same sighting. Event ids are stable (one per signal for `recorded`, one per
verdict change for `triaged`).

**Payload: ids and routing only.** `signal_id`, `trigger_path`, `lens`,
`source_provider`, `subject_kind`, `verdict`, `case_id`, `pir_floor_met`,
`facts`, plus `previous_verdict` on `triaged`. These are the filter keys.
`facts` (#2434, `ctem_signals.routing_facts`) holds yes/no only, never a
value: `kev`, `epss_high` (EPSS at or above 0.5), `sighted`,
`software_named`, `pir_flagged`. The agent reads the
signal itself with `list_ctem_signals signal_id=...` under the run's
permissions.

**Visibility.** Both events are viewer-checked (`VIEWER_CHECKED_EVENTS`), the
case events' delivery check: a subscription hears one only when the user its
run executes as reads the signal (`ctem_signals.user_reads_signal`: an
administrator or a member of a group with a CTEM right, read at delivery
time, #2441). A delivery dropped because the run-as person lost the right is
counted (`xtmone_platform_event_object_dropped_total`, label `reason`) and
logged.

**The trigger.** The assignment form shows a "CTEM signal" tile next to
"Platform event" (XTM One mode). It saves a `platform_event` trigger: `event`
is one of the two events or both, and `filter` holds an any-of list for each
of `trigger_path`, `lens`, `source_provider`, `subject_kind` and `verdict`
that is filled (empty: every signal). A stored `platform_event` trigger on the
signal events alone, with those filter keys only and no one-off watch,
deadline or scope, reopens on the tile (`frontend/src/lib/ctem-signal-trigger.ts`).
`create_assignment` and `update_assignment` set the same trigger with
`trigger_type="platform_event"`; no new trigger type.

**Bursts.** One run per signal, under the assignment's `max_concurrent_runs`
(the platform-event queue). Platform events have no batching today, so the
trigger offers no "batch every N minutes" setting.

## 6ter. Purge every signal (#2528)

When the scope threshold was set wrong and too many cases were opened, an
administrator starts over from the Signals page: a destructive icon button next
to **Sources**, administrators only. `app.services.ctem_signal_purge` runs one
job at a time on the platform (Redis lock `ctem:signals:purge-job`), in a strict
order:

1. **Pause intake.** The flag `ctem:signals:intake-paused` (2 h TTL, so a lost
   pod never leaves intake off) is read by `ctem_signals.record_signal`, which
   every source goes through: while it is set each one is refused with
   `SignalIntakePausedError` (a `SignalError`) and records nothing. The scheduled
   recheck and case sweep, the attack surface pass and the evidence health pass
   skip their tick (their cadence slot is left for after the purge).
2. **Purge.** The cases a signal opened or joined (a signal names it, a
   `signal` member, or a `created ... signal` or `signal` event) go 200 at a time
   through `ctem_case_delete.delete_cases`: members, history, open work items,
   armed timers, coverage, one tombstone audit row per case. Their signals are
   never handed back to the scope gate: every signal is deleted next, 1,000 per
   transaction. Then the raised state outside `ctem_signals`: the attack surface
   pass's `ctem_surface_observations.signalled` keys, each undone so the next pass
   finds its difference again (a raised port leaves the host's baseline ports, a
   raised new host keeps its row with the `rearm:new_host` mark, so the next pass
   that sees it raises it once without the provider-date check, then drops the
   mark; a deleted row was withheld `known_to_provider` and never raised again).
   A raised port of path 5 or 7 also leaves a `rearm:<signal key>` mark.
   No other source keeps raised state outside the signal row.
3. **Resync**, only when the administrator ticked it (unchecked by default),
   `app.services.ctem_signal_resync`, recording through the paused intake for this
   step only. It opens with `reraise_held` (#2634): every `rearm:` mark of the
   baseline is raised again at once, dated now, from what the last committed pass
   held (a new host still undeclared on path 7, a held port on path 5 or 7), and
   the mark is replaced by the signal key, so the next pass dedupes on it; the
   pass itself runs only after every route and diffs against the pre-purge pass.
   While the routes replay, the paths list gives the outside paths the reason
   `resync_queued` (`quiet_reason`, `ctem_attack_surface.quiet_reasons`);
   after it, an outside path with no signal in 30 days names why (first pass
   baseline, no undeclared host, new hosts withheld, re-armed hosts waiting, or
   an owned scope the provider read `empty`). The reason is data, never a
   sentence: `{code, at, count, scopes, empty_scopes}`; the signal sources panel
   words each code through `t_i18n` (`quiet-reason.ts`) and renders `at` with
   the locale date format. The routes follow, each route with its own read window and its own cap
   (`MAX_PER_ROUTE`), so no type starves another. The routes and their default
   windows live in `WINDOWS`; the dialog sends the chosen ones as
   `resync_windows`, validated by `validate_windows` (1 to 365 days; an
   inside-state route also takes null, no age limit; a pass takes none):
   - one route per OpenCTI type the live listener reads (paths 1, 2, 10;
     vulnerabilities 7 days by default, the others 30), from every enabled
     OpenCTI source as its run-as, each fed to `note_event` as an `update`
     delivery (same extractor, exclusions, internal-id dedupe and scope gate);
   - PIR matches: the entities flagged in each PIR, delivered as the PIR
     playbook delivers them (`xtm_source` and the PIR score), so the relevance
     floor is computed as live;
   - path 11: the reports naming a tracked organization's OpenCTI identity,
     through `note_tracked_organizations`;
   - inside state (paths 3, 10): the open Aikido issues and the open Dependabot
     alerts of each GitHub organization, read with the integrations visible to
     the administrator who started the purge and a token minted for them;
   - path 5 (#2641, #2643): the open findings of every enabled OpenAEV listener
     source (`openaev_findings`, no age limit by default), read through
     `POST /api/findings/search` with the integration owner's credential and fed
     to `note_event` as the provisioned `finding_created` trigger delivers them
     (`finding_delivery`: same `_openaev_event` shape, keyed on the finding id, so
     a second resync adds nothing); skipped with its reason when that source is
     off on path 5, a refused read is one error line and the other routes go on;
   - the attack surface (5, 6, 7) and evidence health (9, 12) passes, once each,
     their outcome reported (recorded, off in the profile, skipped and why, the
     error, or nothing to raise).
   GitLab findings (the GitLab asset sync) and OpenAEV simulation results and
   score degradations (transitions of a simulation run: run a simulation to raise
   them again) are reported with that reason. The step's counts carry `windows`,
   `routes` (per route: window, read, recorded, capped, errors, skipped) and `by_path`
   (signals and cases per trigger path).
4. **Resume intake**, always, also after a failure, which the job status and the
   audit row report.

Sources, trigger paths, webhook sources, exclusions and the case-opening switch
are never touched. One `audit_logs` row (`entity_type` `ctem_signal_purge`)
holds the actor, the resync choice and the counts per step.

`GET /api/v1/ctem-signals/purge` (administrators): the live counts
(`{signals, cases}`), the confirmation word, the resync routes with their
default windows (`resync_routes`, `resync_max_days`) and the running or last job.
`POST /api/v1/ctem-signals/purge` `{confirm, resync, resync_windows?}`
(administrators): 422 without the word `PURGE` or on a window it refuses, 409
while a job runs, else 202 with the job's first status (its `windows` filled
in with the defaults). A non-administrator gets 403 on both.

A job whose process restarts mid-job never writes its end: its status stays
`running` while the job lock and the intake pause expire (2 h). `read_status`
reports such a status (`running` with no job lock) as interrupted (#2539):
`state` `failed`, `interrupted` true, a reason in `error`, the step it reached
failed and the counts reached kept. Its steps say what is true now:
`intake_paused` reads `PAUSE_KEY`; once the pause is gone, intake runs again, so
`resume` reads `done` and a `resync` that never started reads `skipped`; while
the pause is still there, `resume` stays `pending` and the dialog says intake is
still paused. The progress dialog stops polling and the purge button opens the
confirm dialog again.

## 6quater. Retention of discarded and watchlist signals (#2581)

Without it a signal is only deleted by the purge (section 6ter) or with its
organization. `services/ctem_signal_retention.py` deletes the signals nobody will
act on, in the hourly retention pass of the autonomous tick
(`autonomous._run_retention_pass`, next to the file, coordination and run
retentions):

| State | Deleted when | Window key (Settings > Observability) | Default |
|---|---|---|---|
| `discarded` by the scope gate | not seen (`last_seen_at`) for the window | `coordination_retention.ctem_signal_discarded_days` | `CTEM_SIGNAL_DISCARDED_RETENTION_DAYS`, 30 |
| `watchlist` | not seen for the window | `coordination_retention.ctem_signal_watchlist_days` | `CTEM_SIGNAL_WATCHLIST_RETENTION_DAYS`, 90 |

- **0 turns a rule off.** The windows are keys of
  `platform_settings.coordination_retention` (`services/coordination_retention.py`),
  saved through `PUT /api/v1/settings/observability` (`CoordinationRetention`,
  optional: a body without them keeps the stored windows), and fall back to the
  environment defaults.
- **Never deleted:** a signal a person dismissed (`decision.reason_code`
  `dismissed_by_user`: deleting it would bring it back at its next sighting);
  a signal in a case (it names one in `case_id`, or a case holds it as a
  `signal` member, `ctem_case_issues`); any other state.
- **A later sighting** of a deleted signal records it again as a first
  sighting: the scope gate decides it anew and `ctem_signal.recorded` is raised.
- **Watchlist re-decided on every sighting:** a new sighting of a watchlist
  signal runs the scope gate again (`ctem-scope-gate.md` section 1), so it
  rarely reaches its window while it keeps being seen.
- **Batches:** `SIGNAL_RETENTION_BATCH` (1,000) per transaction, oldest
  sighting first; the DELETE re-applies every rule, so a signal seen again or
  joined to a case meanwhile stays. A pass deletes at most
  `MAX_BATCHES_PER_PASS` batches per state; one that reached it sets the
  `RETENTION_DRAIN` backlog and the next ticks drain the rest.
- **Skipped during a purge:** `purge_running()` reads the purge job's guards
  (a job of this process, the intake pause `PAUSE_KEY`, the job lock
  `JOB_LOCK_KEY`); a Redis error never stops retention for good.
- **Audit:** one platform audit row per pass that deleted anything
  (`system:ctem_signal_retention`, action `retention_purge`, entity type
  `ctem_signal`), with `details.deleted` per state and `details.retention_days`.

## 7. Tests

`tests/test_ctem_signal_retention.py` and
`tests/test_ctem_signal_retention_postgres.py` (section 6quater: each rule on
PostgreSQL, a 0 window, the batches, the audit row, the purge guard),
`tests/test_ctem_signals.py` (input rules, listener table, API and tools:
one signal per path through each, idempotence, filters, recorder scope,
Copilot mode), `tests/test_ctem_signals_postgres.py` (the upsert, a new
sighting keeps the next steps' writes, organization isolation, filters, the
check constraints), `tests/test_ctem_signal_events.py` (section 6bis: first
sighting raises `recorded`, re-sighting nothing, a verdict change `triaged`,
a non-admin subscriber does not hear an admin-only signal, filters match),
`tests/test_ctem_platform_sources.py` (section 4bis: off by default, enable
with a run-as provisions, refusals with their reason, disable deprovisions,
an assignment off never stops a source, adoption new before old, status reads
Connected only when provisioned), `tests/test_ctem_f2_vuln_prioritization.py`
(the F2 assignments on `ctem_signal.recorded` and their routing facts),
`tests/test_ctem_signal_purge.py` and `tests/test_ctem_signal_purge_postgres.py`
(section 6ter: the admin gate, the paused intake refuses every source, the job
order, intake resumed after a forced failure, the cascade with no orphan, a large
set in batches, the attack surface pass raising again after a purge).
