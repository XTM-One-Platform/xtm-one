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

A caller may name another lens than the default (a `has` relationship is the
inside lens of path 1). `GET /api/v1/ctem-signals/paths` answers this table.

Aikido (CTEM-25): the code repository and container image of an issue (name
and id) go to `evidence.assets`, so the scope gate resolves them to a profile
asset row (source `aikido`, `external_id` the Aikido id) and to its crown
jewel by the existing asset names; an unmapped repository is `to_validate`.
A dependency issue that names its fixed version opens an upgrade case keyed
and titled on that version (`upgrade:-/<package>@<version>`). An
`issue.closed` records no signal: it closes the case member of the signals the
issue recorded, and the last member closed resolves the case on a passing
validation of kind `rescan` (`ctem_case_loop.close_signal_members`).

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
`last_signal_at` (the reader's visibility, as the signal list). Additive: the
former fields are unchanged. The table lives in
`services/ctem_signal_sources.py` (`PATH_SOURCES`); a test fails when a
listener row of section 2 has no source row.

| Source `kind` | What it is | `bound` when |
|---|---|---|
| `listener` | an integration delivery a listener row maps to the path | an integration of the provider is connected and visible to the reader |
| `pass` | a scheduled pass: attack surface (Shodan, GTI; paths 5, 6, 7), evidence health (OpenCRQ; paths 9, 12) | the pass runs on a connected integration of the provider; `off` (never `bound`) when the provider is connected but the pass is off or names none of its integrations |
| `pir_playbook` | the OpenCTI PIR playbook (CTEM-05), delivered through the OpenCTI listener (paths 1, 2) | OpenCTI is connected and an enabled assignment holds a provisioned playbook |

`status`: `connected` (one source bound), `off` (none bound, one pass `off`:
its provider is connected, the pass does not run; neutral tone, action "Turn
on" to its card in Settings > Organization), `not_connected` (a source exists,
none bound or off: no integration of the provider is connected), `agents_api`
(no automatic source: agents and the API only, never read as connected).

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
| `source_provider`, `source_external_id` | Who saw it (`opencti`, `openaev`, `aikido`, `manual`...) and the object's id there |
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
one to `seen_count`, keeps its evidence and label; recorder, state, decision
and case stay as the first sighting and the next steps left them.

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
   `list_ctem_signals`, XTM One mode only.
3. **API**: `POST /api/v1/ctem-signals` (answers `created` and the signal),
   `GET /api/v1/ctem-signals?state=&trigger_path=&lens=&limit=&offset=`
   (newest sighting first, `total`, `counts` by state),
   `GET /api/v1/ctem-signals/{id}`, `GET /api/v1/ctem-signals/paths`.

## 5. Visibility

Signals are organization data; their content is what the recorder chose to
write, not a mirror of a source product's object (cases render per viewer,
`ctem-cases.md` principle 8; signals do not, so they never copy a source's
restricted fields). An administrator reads every signal of the organization;
anyone else reads the signals recorded for them (their API calls and their
agents' tool calls). A listener's signal (`system`) is an administrator's
read only. Two organizations never share a row (the unique key holds the
organization).

An agent tool takes the administrator's reach through
`agent_tool_admin_bypass`: strict admin access for agent tools and the
Platform Health reach keep an administrator to their own signals. Every
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
`source_provider`, `subject_kind`, `verdict`, `case_id`, plus
`previous_verdict` on `triaged`. These are the filter keys. The agent reads the
signal itself with `list_ctem_signals signal_id=...` under the run's
permissions.

**Visibility.** Both events are viewer-checked (`VIEWER_CHECKED_EVENTS`), the
case events' delivery check: a subscription hears one only when the user its
run executes as reads the signal (`ctem_signals.user_reads_signal`: an
administrator, or the signal's recorder). A listener-recorded signal
(`recorded_by = system`) reaches only an assignment that runs as an
administrator.

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

## 7. Tests

`tests/test_ctem_signals.py` (input rules, listener table, API and tools:
one signal per path through each, idempotence, filters, recorder scope,
Copilot mode), `tests/test_ctem_signals_postgres.py` (the upsert, a new
sighting keeps the next steps' writes, organization isolation, filters, the
check constraints), `tests/test_ctem_signal_events.py` (section 6bis: first
sighting raises `recorded`, re-sighting nothing, a verdict change `triaged`,
a non-admin subscriber does not hear an admin-only signal, filters match).
