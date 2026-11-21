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
| 3 | `code_flaw` | inside | Aikido `issue.open.created` (and its open-source and SAST variants) |
| 4 | `proven_exploitable` | inside | tool and API |
| 5 | `new_exposure` | outside | tool and API |
| 6 | `stale_asset` | outside | tool and API |
| 7 | `surface_drift` | outside | tool and API |
| 8 | `mitigation_regression` | inside | tool and API |
| 9 | `control_decay` | inside | tool and API |
| 10 | `credential_leak` | world | tool and API |
| 11 | `third_party_event` | world | tool and API |
| 12 | `blind_spot` | inside | tool and API |

A caller may name another lens than the default (a `has` relationship is the
inside lens of path 1). `GET /api/v1/ctem-signals/paths` answers this table.

## 3. Model (`ctem_signals`, migration `c826ctemsignals1`)

One row per `(organization_id, dedupe_key)`.

| Column | Meaning |
|---|---|
| `organization_id` | The organization (FK, cascade) |
| `trigger_path`, `lens` | Section 2 (check constraints) |
| `source_provider`, `source_external_id` | Who saw it (`opencti`, `openaev`, `aikido`, `manual`...) and the object's id there |
| `subject_kind`, `subject_ref`, `subject_label` | What it is about: `asset`, `software`, `identity`, `third_party`, `vulnerability`, `threat`, `code`; a stable reference (hostname, IP, URL, CPE, CVE id, account, organization) |
| `observed_at`, `first_seen_at`, `last_seen_at`, `seen_count` | The latest sighting, the first, the latest time seen, how many sightings |
| `evidence` | JSON, at most 8,000 characters: the facts and a `link` |
| `dedupe_key` | Default `<path>:<provider>:<external id>`, else `<path>:<subject kind>:<lower-cased ref>`, hashed past 200 characters; a caller may set its own |
| `state` | `new` (written here), then `confirmed`, `to_validate`, `discarded`, `watchlist` (reconcile and scope gate), `case_opened` (cases from signals; a check constraint requires `case_id`) |
| `decision` | JSON written by the reconcile and scope gate: inputs, outcome, reason |
| `case_id` | The case the signal opened or joined (FK to `ctem_cases`, set null on delete) |
| `recorded_via`, `recorded_by` | `api`, `tool` or `listener`; the user id, or `system` for a listener |

Idempotence: `services/ctem_signals.record_signal` is one `INSERT ... ON
CONFLICT (organization_id, dedupe_key) DO UPDATE`. A sighting of a known key
moves `last_seen_at` to the later of the stored value and its own time, adds
one to `seen_count`, keeps its evidence and label; recorder, state, decision
and case stay as the first sighting and the next steps left them.

## 4. Three ways in

1. **Listeners** (`services/ctem_signal_listeners.py`): `LISTENERS` is the
   declarative table (provider, event keys, object types, path, lens, subject
   kind). The three webhook routes that carry integration events (provider,
   integration, integration-assignment) hand every delivery to `note_event`
   after its signature check; no model call, no run, no quota. A provisioned
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

## 6. Agents

The tools are offered to the built-in `ctem-assistant`,
`ctem-discovery-analyst`, `ctem-vuln-prioritization-orchestrator`,
`ctem-vuln-triage-analyst`, `ctem-threat-response-orchestrator` and
`ctem-threat-relevance-analyst`, acting for the person the turn or run acts
for. No persona or seeded assignment uses them yet: the fleets are rewired on
signals and cases in a later step of the loop.

## 7. Tests

`tests/test_ctem_signals.py` (input rules, listener table, API and tools:
one signal per path through each, idempotence, filters, recorder scope,
Copilot mode), `tests/test_ctem_signals_postgres.py` (the upsert, a new
sighting keeps the next steps' writes, organization isolation, filters, the
check constraints).
