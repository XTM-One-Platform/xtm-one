# CTEM evidence decay and blind spots: the evidence health pass (CTEM-28)

Trigger paths 9 (control or evidence decay) and 12 (blind spot or coverage gap)
fire on absence and on time, not on a fresh observation: an in-scope asset that
nobody measured recently, or nobody measures at all, must never read as safe.
OpenCRQ knows the evidence behind each supporting asset; this pass reads it and
records each gap as a CTEM signal. The spine (reconcile, scope gate, cases:
`ctem-signals.md`, `ctem-scope-gate.md`) decides what each signal becomes. No
model call and no human gate on this path.

## 1. Profile

| Field | Meaning |
|---|---|
| `evidence_health.enabled` | Opt-in, off by default. |
| `evidence_health.integration` | The id of the OpenCRQ integration the pass reads. |
| `evidence_health.max_age_days` | The re-test delay: evidence older than this has expired (30 by default). |
| `evidence_health.cadence_hours` | Hours between two passes (24 by default). |

An exposure field: read by the pass, never dumped into the always-on profile
block, not a contract field.

## 2. The pass

`services/ctem_evidence_health.py`, called by the autonomous tick after the
attack surface pass, at most once per cadence window (Redis slot
`ctem:evidence-health:pass`, kept only by a pass that committed).

1. Read: the risk scenarios (`riskScenarios.getRiskScenariosOverview`, 50 at
   most), then for each `riskScenarios.getEvidenceHealth` with the re-test
   delay (OpenCRQ #45): per in-scope supporting asset a state and the date of
   its newest evidence, and per connector sync whether it emptied.
2. Map, in code (`gaps_of`, a pure function):

| OpenCRQ state | Event kind | Path |
|---|---|---|
| `expired`, `attested_only`, `never_measured` | `evidence_expired` | 9 `control_decay` |
| `no_source`, `version_unknown` | `coverage_gap` | 12 `blind_spot` |
| a connector sync that emptied (a run far below the previous one) | `coverage_gap` | 12 `blind_spot`, subject `opencrq:<sync kind>` |
| `current` | none | |

3. Record: each gap is a signal of the inside lens, subject kind `asset`,
   recorded by `system` through `record_signal`; a new one goes through the
   scope gate (`evaluate_after_record`). An asset or a sync seen in several
   scenarios is recorded once.
4. Commit by batch: the scheduled pass commits every `COMMIT_BATCH` (25) new
   signals and raises their case events at once, and hands the event loop back
   between signals. A pass cut by a restart keeps the batches it committed; the
   next pass finds them by dedupe key and goes on from there.

The dedupe key holds the asset, its state and the date of its last evidence
(`never` when none): two identical passes record nothing new; evidence that
came back and decayed again raises a new signal. An emptied source never closes
the findings it used to report: it is a gap, recorded as one.

## 3. Segregation

- OpenCRQ reads are system work: the credential is the one of the declared
  integration's owner (as the attack surface pass and the CTEM ticketing sync),
  listed in the credential gate (`tests/test_integration_credential_gate.py`).
  The token only reads the tenant's risk scenarios and their evidence health;
  OpenCRQ answers under its tenant RLS.
- The signals carry asset ids and names, the state, dates and source origins,
  never finding content. They are recorded by `system`: only an administrator
  reads them (`ctem-signals.md`, visibility).
- Markings fail closed (CTEM marking parity, G5): the owner's credential reads
  OpenCRQ rows its clearance allows, and an administrator reading the signal may
  not hold that clearance. OpenCRQ's evidence health states the markings of the
  scenario and of each asset (`markingRefs`, OpenCRQ #60); a gap is recorded only
  for an asset whose `markingRefs` is `[]` in a scenario whose `markingRefs` is
  `[]`. A marked row, or an answer that does not state its markings (an older
  OpenCRQ), names nothing; an emptied connector sync names no asset and is kept.
- The pass settings are profile fields: only the profile edit right changes them.

## 4. Failures

An OpenCRQ read that fails records nothing and gives the slot back; the next
tick retries. A missing or non-OpenCRQ integration skips the pass with its
reason in the log.
