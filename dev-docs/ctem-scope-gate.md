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
  A new sighting of a known signal re-runs it only when the signal is
  unmatched (`to_validate`). A failure leaves the signal `new`; it never
  refuses the signal.
- After a profile change (assets, groups, crown jewels, owned domains, tech
  stack, shipped products): the autonomous tick decides again the unmatched
  signals last decided before the newest profile version, 500 per tick
  (`recheck_parked`). A signal it confirms opens its case at the next case
  sweep, grouped by case key, never one case per host.
- From the Signals page, by the people who read the signals:
  `POST /api/v1/ctem-signals/triage` dismisses (state `discarded`, reason
  `dismissed_by_user`, kept out of every pass until restored), restores or
  re-checks the selected signals or every signal a state filter matches. An
  administrator also has `POST /api/v1/ctem-signals/add-to-inventory`: one
  profile version declares the subjects of the selected unmatched signals (a
  host an asset, a software a tech stack product, an identity its email domain
  as an owned domain), then the gate runs on them again.
- A host under an owned domain (`*.corp.example`) resolves to it
  (`hosted_under` in the reconcile record) and goes to the watchlist with an
  unknown criticality, never to a case of its own.
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
   **Shipped products** (`shipped_products`, what the organization ships,
   apart from the tech stack it runs): a software reference or a CPE of the
   evidence (`software`, `cpe`; the OpenCTI vulnerability listener copies
   the vulnerability's CPEs into `cpe`) matches a row by CPE first (the
   vendor and product of the row's CPE, any version), else by product and
   vendor (a CPE naming them, or the product name). A CPE match wins over a
   product match. The matched rows' repositories (`owner/name`) resolve the
   subject (`reconcile.repositories`, `reconcile.shipped_products` with
   `by: cpe | product`), and the profile asset of the same name (a
   repository the GitHub or GitLab import wrote) gives its criticality. The
   case key stays `patch:vulnerability/<cve>`, so the CVE from OpenCTI and
   the GitHub repository advisory of the same CVE share one case. No match
   leaves the subject `subject_unresolved` as before.
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
  "reason_code": "in_scope | below_scope_threshold | criticality_unknown | no_risk_scenario | risk_scenario_unreadable | crown_jewel_below_policy | internet_exposure_required | score_below_policy | accepted_risk | excluded_by_policy | subject_unresolved | contradicted_by_signals | contradicted_by_evidence | no_dependency | account_disabled",
  "reason_params": {"criticality", "threshold", "confidence", "minimum", "score", "value", "owner", "until", "kind", "count"},
  "decided_at": "ISO-8601"
}
```

Third parties and identities (CTEM-29): a `third_party` subject resolves to the
profile asset row that names the tracked organization and, through a crown
jewel's `asset_names`, to that crown jewel; a crown jewel named in
`evidence.crown_jewels` and OpenCRQ supporting assets in
`evidence.opencrq_asset_ids` count too. Unresolved, it goes to the watchlist
(`no_dependency`), never to "to validate". An `identity` subject
(`name@domain`) resolves when its domain, or a parent of it, is one of the
profile's `owned_domains`; its scope criticality is the policy's
`identity_criticality` (default high). Evidence saying the account is disabled
(`account_disabled` true or `account_enabled` false) discards it "not exposed"
(`account_disabled`). A `credential_leak` signal never stores a secret: every
secret-named evidence field (`password`, `token`, `secret`, `credential`...) is
kept as its SHA-256 fingerprint, whatever the intake.

`reason` is the English line agents and logs read. The UI never draws it: it
translates `reason_code` with `reason_params` (one code per outcome branch of
`decide`, sentences in `frontend/src/pages/xtm-one/ctem/loop/DecisionReason.tsx`
and the four catalogs), and lists `reconcile`, `scope` and `policy_checks` as
the labelled inputs of the loop trace. A new branch adds its code, its
sentence and a case in `test_every_outcome_carries_a_reason_code_the_frontend_translates`.

`scope` is `null` when the gate stopped at reconcile. The case step opens a
case for a signal in state `confirmed` whose decision outcome is `case`, and
reads `severity` and `sla_hours` from here.
