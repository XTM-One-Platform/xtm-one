# CTEM reconcile and scope gate

Second brick of the CTEM loop spine (signal, reconcile, scope gate, case,
validate, decide, mobilise, re-test, close and feed back). It takes the
signals of `ctem-signals.md` and decides, without a model call, whether each
one is real (reconcile) and whether it lands where it matters (scope gate).
The case step reads the outcome to open a case.

Code: `backend/app/services/ctem_scope_gate.py` (pure rules, `decide`),
`backend/app/services/ctem_scope_gate_store.py` (reads the inputs, writes the
decision). Tests: `backend/tests/test_ctem_scope_gate.py`.

## 1. When it runs

- After a signal is first recorded, by the API, the agent tool or a listener.
  A new sighting of a known signal does not re-run it. A failure leaves the
  signal `new`; it never refuses the signal.
- On demand, by an administrator: `POST /api/v1/ctem-signals/{id}/evaluate`
  (one signal) and `POST /api/v1/ctem-signals/evaluate` (every signal not yet
  in a case, at most 2,000, newest first), after a policy change or a change
  of an asset's criticality. A watchlist signal re-enters the gate this way.
  These two calls read the OpenCRQ risk scenarios with the administrator's
  OpenCRQ connection; the pass at recording time does not (no user to read
  them for), and its decision says so.
- A signal in `case_opened` is never moved back: the case step owns it.

## 2. Reconcile

1. **Keys**: the subject reference, the source external id and the assets,
   hostnames, URLs and IPs the evidence names are reduced to stable keys:
   a URL to its host, lower case, no port, no trailing dot; a cloud id or an
   external id lower-cased.
2. **Resolution** to the organization profile: assets (`assets_exposure`,
   including the rows imported from OpenAEV and OpenCRQ, by name or external
   id), asset groups, crown jewels (by name or `opencrq_asset_id`), and the
   tech stack for software (a product name, or the product part of a CPE),
   which adds the assets that run that product.
3. **Lenses**: the organization's other signals on the same subject (same
   kind and reference, any recorder, any path) are counted with this one.
   Confidence: `high` when a test proved it (path `proven_exploitable` or
   `mitigation_regression`, a verdict `exposed`, `exploitable` or `proven`,
   or a KEV flag) or three lenses saw it; `medium` for two lenses; `low` for
   one.
4. **Outcome**:
   - `discarded`: a verdict `not_exposed`, `not_affected`, `false_positive`
     or `fixed` on this signal or another on the same subject, or a policy
     exclusion (product, asset group or tag);
   - `to_validate`: nothing in the profile resolves the subject ("new, to
     validate");
   - else confirmed, and the scope gate decides.

## 3. Scope gate

The scope criticality is the highest of:

- the resolved assets' criticality (OpenAEV scale, `very_high` read as
  `critical`);
- the crown jewels they run (a crown jewel's `asset_names` holds the asset or
  its group) or that the subject names;
- the OpenCRQ risk scenarios that hold the asset (through
  `listDerivedSupportingAssets`, joined on the OpenCRQ asset id or name).
  A scenario weighs `critical` when its annual loss exposure is at least its
  tolerance, `high` from half of it, `medium` from a tenth, `low` below;
  `high` when OpenCRQ gives no figure.

Then, in order, each check stops at the first that fails, with its reason:

| Check | Outcome when it fails |
|---|---|
| An unexpired accepted risk on the subject, a CVE or a product (and its scope) | watchlist, owner and expiry named |
| CVE-bearing signal: `kev_always_in` and a KEV flag pass; else `cvss_min`, `epss_min` (an unscored value passes, noted) | watchlist, the score and minimum named |
| `internet_exposure_required`: an internet-facing asset, or the outside lens | watchlist |
| `min_crown_jewel_criticality` | watchlist |
| `require_risk_scenario`: a risk scenario holds the subject | watchlist, or "OpenCRQ not read" with why |
| criticality at or above `scope_min_criticality` | watchlist, the criticality and threshold named |

All pass: outcome `case`, state `confirmed`, with a severity.

## 4. Severity and SLA

Points = scope criticality (low 1, medium 2, high 3, critical 4) + lens
confidence (low 0, medium 1, high 2).

| Points | Severity |
|---|---|
| 5 or more | P1 |
| 4 | P2 |
| 3 or less | P3 |

So a critical asset seen by one lens is P2, by two lenses P1; a high asset
proven by a test is P1. The SLA is `sla_hours` of the policy for that
severity; the case step sets the case's due date from it.

## 5. Policy fields (prioritization policy of the profile)

| Field | Default | Meaning |
|---|---|---|
| `scope_min_criticality` | `high` | The threshold X, on the crown-jewel scale `low` to `critical` |
| `require_risk_scenario` | `false` | A case also needs an OpenCRQ risk scenario; off so the loop works without OpenCRQ |
| `sla_hours.p1`, `.p2`, `.p3` | 24, 72, 720 | Hours to fix a case of each severity |

The existing fields (`cvss_min`, `epss_min`, `kev_always_in`,
`internet_exposure_required`, `min_crown_jewel_criticality`, `exclusions`,
`accepted_risks`) are honoured as section 3 says. The defaults are open
product choices, configurable per organization.

## 6. Decision record

Stored as `decision` on the signal (version 1), with the state:

```json
{
  "version": 1,
  "inputs": {"trigger_path", "lens", "subject": {"kind", "ref"}, "source_provider", "verdict", "cves", "related_signals": [{"id", "lens", "provider", "verdict"}]},
  "reconcile": {"keys", "assets", "asset_groups", "crown_jewels_named", "tech_stack", "lenses", "confidence", "proven", "contradicted_by"},
  "scope": {"criticality", "sources": [{"from", "name", "criticality"}], "threshold", "require_risk_scenario", "opencrq": {"connected", "note"}, "risk_scenarios": [{"id", "name"}]},
  "policy_checks": [{"rule", "value", "threshold", "passed"}],
  "severity": "P1 | P2 | P3 | null",
  "sla_hours": 24,
  "outcome": "case | watchlist | to_validate | discarded",
  "state": "confirmed | watchlist | to_validate | discarded",
  "reason": "one line",
  "decided_at": "ISO-8601"
}
```

`scope` is `null` when the gate stopped at reconcile. The case step opens a
case for a signal in state `confirmed` whose decision outcome is `case`, and
reads `severity` and `sla_hours` from here.
