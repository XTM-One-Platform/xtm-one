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
  unmatched (`to_validate`) or on the watchlist (`watchlist`, #2581:
  `RE_DECIDED_ON_SIGHTING`), so a rising EPSS, a new KEV flag or an asset now
  internet-facing can confirm a watchlist signal and open its case; a
  discarded signal (gate or person) is not decided again by a sighting. A
  failure is retried once with local reads only
  (no OpenCTI or OpenCRQ call); a second failure leaves the signal `new`; it
  never refuses the signal. A remote read on this path never commits the
  gate's session (it decides inside a savepoint) and is bounded in time. The
  bulk CVE prefetch (`prepare_cve_reads`, no savepoint open) still hands the
  pooled connection back during its OpenCTI reads.
- Every 90 seconds, on its own Redis slot (`ctem:signals:recheck-new`), never
  the hourly case sweep's (#2530): the signals still `new` past a 10-minute
  grace (a record-time failure, a case-delete release whose background
  re-decision was lost), oldest first, each in its own savepoint, 500 per
  batch committed one by one, batch after batch until a short batch or a
  20-second budget (`drain_new` over `recheck_new`): a backlog of thousands
  drains in minutes and nothing waits "for reconcile" for good. This path
  never opens a case; the case sweep keeps its cadence.
- After a profile change (assets, groups, primary assets, owned domains, tech
  stack, shipped products): the autonomous tick decides again the unmatched
  signals last decided before the newest profile version, 500 per tick
  (`recheck_parked`), only those seen within the unmatched retention window
  (#2726, `ctem_signal_unmatched_days`, 30 by default; 0 takes every one): an
  older one is left as it is and decided again at its next sighting. A signal
  it confirms opens its case at the next case sweep, grouped by case key,
  never one case per host.
- From the Signals page, by the people who read the signals:
  `POST /api/v1/ctem-signals/triage` dismisses (state `discarded`, reason
  `dismissed_by_user`, kept out of every pass until restored), restores or
  re-checks the selected signals or every signal a state filter matches.
  `dry_run: true` (route and `triage_ctem_signals`, #2341) runs the same
  selection and decisions, answers `signals` (id, state before and after,
  `changed`, `unchanged` or `skipped`) with the counts, and rolls back: nothing
  is written and no `ctem_signal.triaged` event is raised. An
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
- Retention (#2581, #2726, `ctem-signals.md` section 6quater): a gate-discarded
  signal not seen for 30 days, a watchlist signal not seen for 90 days and an
  unmatched signal not seen for 30 days are deleted by the hourly retention
  pass; a later sighting records the signal again and this gate decides it as a first sighting. A signal a person
  dismissed (`dismissed_by_user`) or in a case is never deleted.

## 2. Reconcile

1. **Keys**: the subject reference, the source external id and the assets,
   hostnames, URLs and IPs the evidence names are reduced to stable keys:
   a URL to its host, lower case, no port, no trailing dot; a cloud id or an
   external id lower-cased.
2. **Resolution** to the organization profile: assets (`assets_exposure`,
   including the rows imported from OpenAEV and OpenCRQ, by name or external
   id), asset groups, primary assets (by name or `opencrq_asset_id`), and the
   tech stack for software (a product name, or the product part of a CPE),
   which adds the assets that run that product.
   **Named assets** (#2701): when the evidence's asset keys (`assets`,
   `asset`, `hostnames`, `urls`, `ips`) name an asset (by name, external id
   or hostname), an asset group or a primary asset, those are the whole
   reach. The software still labels the case and version-checks the named
   assets, but adds no asset, shipped repository or primary asset of its
   own; `reconcile.scope` reads `evidence_assets` and the loop trace's
   supporting list follows the same named assets. Without asset evidence
   the product expansion is unchanged (`reconcile.scope` is `null`).
   **Threat subjects** (#1720, a campaign, an intrusion set, a malware): the
   software they reach is read from the evidence lists `targets`, `uses`,
   `targeted_software`, `targeted_products`, from `relationships`
   (`{relationship_type, target}` of type `targets`, `uses` or `has`) and
   from the scorer's `matched_fields` (`software (Microsoft 365, GitHub)`; the former `tech_stack (...)` form is still read),
   then matched to the organization's software like any software reference.
   `reconcile.software_refs` lists every reference tried; an unresolved
   subject carries `reason_params` `{keys, software}`, the counts tried.
   **CVE subjects without software** (#2171): a signal that names CVEs
   (subject, label or evidence `cve` / `cves`) and resolves to nothing
   without them has its CVEs looked up in OpenCTI with the recorder's own
   access (a `system` signal: the OpenCRQ sync owner's): the Software each
   is related to by `has` (name and CPE) joins the software references
   (`reconcile.cve_software`). `reconcile.cve_lookup` says how the read went
   (`read`, `no reader`, `no OpenCTI access`, `OpenCTI did not answer`;
   since #2542 never `null`: `not needed` when the evidence resolved the
   subject, `not applicable` when the signal names no CVE, `not read` when
   needed but not taken by that pass). The case recompute, which holds signal row
   locks (#1953), decides without this read; the bulk re-decide passes
   (`evaluate_open`, `recheck_parked`, `recheck_held`, `triage`) take it up
   front with `prepare_cve_reads`, before any row is written, and decide with
   `remote_reads=False` from the session cache. `record_ctem_signal` names the
   evidence keys the gate reads and answers a `hint` when the gate left the
   signal `subject_unresolved` and its evidence holds none of them.
   **Threat subjects read from OpenCTI** (#2523): the OpenCTI listener records a
   threat (`intrusion-set`, `threat-actor`, `threat-actor-group`, `campaign`,
   `malware`) from the event payload alone, so its evidence holds none of the
   lists above. A threat that resolves to nothing on its evidence has its
   OpenCTI id (the source external id, else `evidence.stix_id`) read with the
   recorder's own access, as the CVE read: the Software it `targets` or `uses`
   (the threat as source) and the Software that `has` each Vulnerability it
   targets (at most 50) join the software references
   (`reconcile.threat_software`, name and CPE, at most 200).
   `reconcile.threat_lookup` says how the read went, with the CVE read's values
   (`not applicable` for a signal that is no OpenCTI threat). The read is bounded (200 relations, 15 s), cached
   per session, reader and threat id, never raises, and follows the CVE read's
   paths: the record-time gate reads inside its savepoint without releasing the
   connection, the bulk passes (the hourly `recheck_new` included) take it up
   front in `prepare_cve_reads` with the connection released (#2516). The loop
   trace's Reconcile step lists `software_refs` as "Software tried" when the
   decision is `subject_unresolved`.
   **A read that did not answer is retried** (#2542): an unresolved decision
   whose `cve_lookup` or `threat_lookup` is not `read`, `not needed` or
   `not applicable` is written with `incomplete: true`, `lookup_attempts` (the
   decisions taken while it kept failing) and `lookup_retry` (`scheduled`, or
   `exhausted` after 6 attempts); the Signals page reads "OpenCTI software not
   read, will retry" instead of a final Unmatched. A failed read is never cached
   as an answer (`_session_cached` keeps only `read` answers), so one miss does
   not decide the other signals of the same threat or CVE list for good. Within
   one bulk prefetch (`prepare_cve_reads`) a failure is met once per key: the
   rows sharing that threat id or CVE list reuse it instead of paying one
   OpenCTI timeout each, and the next prefetch reads the key again. Every 90 s,
   after `drain_new`, `recheck_lookup_failed` decides again, 500 per call, the
   `to_validate` / `subject_unresolved` rows flagged incomplete (or, written by
   a build before #2542, carrying a failed lookup, such as the #2523 window),
   once their backoff ran out (5 min after the first decision, doubling to 80
   min), reads prefetched by `prepare_cve_reads` with no row lock held and each
   row in its own savepoint. After the cap the failed state stays visible and a
   manual Recheck still decides it again.
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
- the primary assets they run (a primary asset's `asset_names` holds the asset or
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
| `min_primary_asset_criticality` | watchlist |
| `require_risk_scenario`: a risk scenario holds the subject | watchlist, or "OpenCRQ not read" with why |
| criticality at or above `scope_min_criticality` | watchlist, the criticality and threshold named |

All pass: outcome `case`, state `confirmed`, with a severity.

### 3.1 Primary assets reached through the inventory (#1591, S3)

`services/ctem_primary_assets.py` reads, once per organization and decision pass, the inventory assets that reach a primary asset through `organization_primary_asset_links` (sources `manual`, `opencrq`, `rule`; a removed link reaches nothing): directly, or as a member of a linked asset group. For each it keeps what the asset runs: its own software list, its OS family (Intune, Defender) and the installed software endpoint sources report (`organization_asset_software`). A signal's software (names, CPEs, matched tech stack products) or subject key reaching such an asset adds the primary asset to `primary_assets` with its chain (`scope.sources[].chain`: software, asset, through, through_kind, source, roles) and to `reconcile.primary_assets`; `reconcile.inventory_links_read` says the index was read. The legacy name list (`asset_names`) is still read for one release.

#2711: a software reference that is one joined package name (`mail-mcp-bridge 1.3.0`, `xcode_mcp_server`) names an inventory label only by its full name; a long title (more than `LONG_TITLE_WORDS` = 3 name words, versions left out) by its full name, its words after the vendor, or a run of two words or more at its start (vendor and product), the label compared with its version kept (`Windows Server 2022` does not open `Windows Server 2019 Standard Edition`) and a bare family or generic label (`Microsoft Windows`, `Client`) by the full name alone (#2628, #2685); never by one of its words (`Mail`, `mcp`, `analytics`), in the device and the repository branches alike, so such a word match is never `version_verified` either (`reference_names` in `ctem_primary_assets.py`). A `reach` hop's `software` is the inventory label the asset runs; the software reference that named it is in `reference`.

#2685: a bare generic product word (`GENERIC_PRODUCT_WORDS`: `client`, `agent`, `server`, `updater`...) as a CVE software reference names only an equal label, is never `version_verified`, and never reaches a code repository through its SBOM rows; a CPE whose product is such a word (`cpe:2.3:a:trueconf:client:...`) names only a label that also names its vendor. KEV proves exploitation in the wild (`exploited_in_wild`), not that the organization runs the product: a reach by a software name alone (no versioned row, no asset key, no typed software row) is never `proven` by the KEV flag.

Every path decides with the same index and the same OpenCRQ read: a `system` signal recorded by a listener reads OpenCRQ with the owner of the enabled OpenCRQ primary-asset sync. The per-signal path (`evaluate_after_record`, called in a loop by intake such as `record_matches`) builds the index once per session and organization and the OpenCRQ read once per session and reader, kept 5 minutes in `session.info`; the batch paths pass theirs in. `recheck_held` filters the held reasons in SQL before its batch limit, so watchlist rows held for another reason never starve them. After a sync that wrote links or changed a device's software, `recheck_held` decides again the watchlist signals held by `primary_asset_below_policy`, `criticality_unknown` or `below_scope_threshold` (500 per call, idempotent); the case sweep then opens one case per fix key. `scope.threshold_source` says whether `scope_min_criticality` is the policy's or the default.

OpenCRQ Dependency Rule links come through `assetDependencies.getSupportToPrimaryLinksByPrimaryAssetId` (field `source: rule`, `ruleId`) and are stored with source `rule`; an OpenCRQ supporting asset is matched to an inventory record by its OpenCRQ id, then by name or device hostname.

The loop trace (`DecisionReason`, `frontend/src/pages/xtm-one/ctem/loop/decision-primary-assets.ts`) draws a row "Primary assets reached" with each chain (software, asset, `group <name>`, primary asset `as <role>`), from `scope.sources[].chain` and else `reconcile.primary_assets`; when `inventory_links_read` is true and nothing is reached it says so. The "Scope threshold" row names its source: "set by the prioritization policy" or "scope default" from `scope.threshold_source`.

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
| `scope_min_criticality` | `high` | The threshold X, on the primary-asset scale `low` to `critical` |
| `require_risk_scenario` | `false` | A case also needs an OpenCRQ risk scenario; off so the loop works without OpenCRQ |
| `sla_hours.p1`, `.p2`, `.p3` | 24, 72, 720 | Hours to fix a case of each severity |

The existing fields (`cvss_min`, `epss_min`, `kev_always_in`,
`internet_exposure_required`, `min_primary_asset_criticality`, `exclusions`,
`accepted_risks`) are honoured as section 3 says. The defaults are open
product choices, configurable per organization.

## 6. Decision record

Stored as `decision` on the signal (version 1), with the state:

```json
{
  "version": 1,
  "inputs": {"trigger_path", "lens", "subject": {"kind", "ref"}, "source_provider", "verdict", "cves", "related_signals": [{"id", "lens", "provider", "verdict"}]},
  "reconcile": {"keys", "assets", "asset_groups", "primary_assets_named", "products", "lenses", "confidence", "proven", "exploited_in_wild", "contradicted_by"},
  "scope": {"criticality", "sources": [{"from", "name", "criticality"}], "threshold", "require_risk_scenario", "opencrq": {"connected", "note"}, "risk_scenarios": [{"id", "name"}]},
  "policy_checks": [{"rule", "value", "threshold", "passed"}],
  "severity": "P1 | P2 | P3 | null",
  "sla_hours": 24,
  "outcome": "case | watchlist | to_validate | discarded",
  "state": "confirmed | watchlist | to_validate | discarded",
  "reason": "one line",
  "reason_code": "in_scope | below_scope_threshold | criticality_unknown | no_risk_scenario | risk_scenario_unreadable | primary_asset_below_policy | internet_exposure_required | score_below_policy | accepted_risk | excluded_by_policy | subject_unresolved | contradicted_by_signals | contradicted_by_evidence | no_dependency | account_disabled",
  "reason_params": {"criticality", "threshold", "confidence", "minimum", "score", "value", "owner", "until", "kind", "count"},
  "decided_at": "ISO-8601"
}
```

Third parties and identities (CTEM-29): a `third_party` subject resolves to the
profile asset row that names the tracked organization and, through a primary
asset's `asset_names`, to that primary asset; a primary asset named in
`evidence.primary_assets` and OpenCRQ supporting assets in
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
