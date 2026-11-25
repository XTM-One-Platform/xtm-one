# CTEM Command Center - Honest Gap Assessment: OpenCTI + OpenAEV for true CTEM

> What it would take for OpenCTI + OpenAEV (orchestrated by XTM One) to deliver a
> *true*, Gartner-grade Continuous Threat Exposure Management program with
> genuinely actionable insights - and exactly what is missing today.
>
> Grounded in: (1) the Gartner CTEM framework and current 2026 practitioner
> guidance, (2) code-level capability dives into both products' APIs, and (3) the
> CTEM command center we actually built in XTM One.

---

## 1. TL;DR

We built a credible CTEM *command center*: a multi-vertical (cyber / FIMI / fraud),
five-stage exposure posture with live OpenCTI + OpenAEV signals, per-stage AI
analysts, threat-informed defense coverage (OpenCTI threats x OpenAEV simulations),
per-metric provenance, and a synthesis briefing.

But there is an honest distinction between a **CTEM command center** (a
posture/coverage lens - which is what we have) and a **CTEM operating platform**
(a closed loop that drives specific exposures from discovery to verified closure -
which we do not yet have). The three structural gaps that separate the two:

1. **There is no asset / business-context model.** CTEM is asset- and
   business-centric. OpenCTI is intel-centric; OpenAEV is simulation-centric.
   Neither owns "this asset, its business criticality, its owner, the service it
   supports". Without it, Scoping, Prioritization, and Mobilization can never be
   fully business-aligned.
2. **There is no unified "exposure" object.** CTEM acts on an *exposure* = (asset
   + weakness + context + validation verdict + remediation state). We synthesize
   that at the dashboard level, but no shared entity or API represents it across
   the two products, so insight cannot reliably become per-exposure action.
3. **Mobilization is the weakest link** - which is exactly where the industry says
   CTEM programs stall. No SLA model, no ownership routing, no ticketing as a
   first-class action, no automated re-validation, no risk-reduction reporting.

Everything below expands these, stage by stage, with concrete, code-level gaps in
OpenCTI, OpenAEV, and the XTM One orchestration layer, and a prioritized roadmap.

---

## 2. The bar: what "true CTEM" demands

CTEM (Gartner) is a **cyclical, five-stage operating model**, not a product. The
loop is continuous and business-aligned, and success is measured by **outcomes**
(reduced MTTR, lower dwell time for critical exposures, measurable reduction of the
*exploitable* attack surface) - not by counts of findings.

| Stage | What it requires in practice |
|---|---|
| **Scoping** | Define program boundaries by **business impact**: critical assets, revenue-generating services, regulated/internet-facing domains, and the **owners** of each. Re-scoped as the business changes. |
| **Discovery** | Continuously inventory the **full** attack surface: vulnerabilities, misconfigurations, **identity** exposures, secrets, shadow IT, third-party - across cloud, on-prem and code. Enriched with threat intelligence. |
| **Prioritization** | Rank by **real risk**, not CVSS alone: asset criticality x exploitability (CISA KEV, EPSS) x **reachability / attack path** x **adversary relevance** x business impact. (Research: ~75% of exposures are dead ends; ~2% reach critical assets.) |
| **Validation** | **Prove** exploitability and control efficacy in *your* environment via BAS / automated pen-test / red team. Threat-informed. (Research: ~63% of "critical/high" drop to ~10% truly urgent after validation.) |
| **Mobilization** | Route **validated** findings to owners with **SLAs**, ticketing, escalation, exception management, **closed-loop re-verification**, and **executive reporting in business terms**. This is where most programs fail organizationally, not technically. |

The recurring "actionable" non-negotiables across the literature: **business
context / asset criticality**, **exploit intelligence**, **attack-path /
reachability**, **adversary relevance (operationalized as Priority Intelligence
Requirements)**, **BAS-proven validation**, and **mobilization with ownership +
SLAs + ticketing + verification**.

---

## 3. What we built (current state)

| CTEM stage | Cyber (default) signal we compute | Source |
|---|---|---|
| Scoping | OpenAEV active-agent coverage of endpoints + unprotected-endpoint gap; OpenCTI scope breadth fallback | OpenAEV + OpenCTI |
| Discovery | **Threat-informed defense coverage**: share of ATT&CK techniques tracked threats *use* (OpenCTI `uses` -> attack patterns, by MITRE T-code) that OpenAEV simulations exercise; untested-technique gap. Falls back to OpenCTI ATT&CK detection coverage | OpenCTI x OpenAEV |
| Prioritization | KEV / critical / high-EPSS CVEs, indicator sightings (observed activity), PIR count (EE) | OpenCTI |
| Validation | Prevention vs detection success, exercised ATT&CK, validated techniques, detection-stack depth (security platforms + collectors) | OpenAEV (+ OpenCTI detection-rule fallback) |
| Mobilization | Response-case closure (incident + RFI), automation playbook coverage, failed/partial validation backlog | OpenCTI (+ OpenAEV) |

Plus: per-user/per-vertical scoping (each call uses the user's own JWT), five
vertical-aware AI stage analysts + a synthesis briefing, per-metric provenance
dialogs, and dedicated deep links into the product resource pages.

This is a strong **posture and coverage** layer. The gaps below are what separate
it from a closed-loop CTEM *operating platform*.

---

## 4. Capability inventory (what the two products actually expose)

### OpenCTI (threat intelligence)
- Entity counts + distributions (`stixCoreObjectsMultiNumber`, `stixCyberObservablesNumber`, `stixCoreObjectsDistribution`).
- Threats -> techniques via `uses` relationships; ATT&CK attack patterns with `x_mitre_id`.
- Vulnerabilities with `x_opencti_cvss_base_score`, `x_opencti_epss_score`, `x_opencti_cisa_kev`.
- Indicators with decay (`x_opencti_score`, `revoked`, decay rules) and **sightings** (`stixSightingRelationshipsNumber`).
- **PIR** (Priority Intelligence Requirements, EE): `pirs`, `pirRelationships`, `pirRelationshipsDistribution` (per-entity `pir_score`).
- **Playbooks** (automation, EE): `playbooks`, `playbookManagerInfo`.
- Cases: Incident / RFI / RFT with workflow statuses (`x_opencti_workflow_id`, `subType.statuses`, `workflowDefinition`).
- Connectors / ingestion freshness; feeds / TAXII / streams.

### OpenAEV (breach & attack simulation)
- Endpoints + agents (active, last-seen, version, platform), asset groups.
- Simulations with `exercise_global_score` (PREVENTION / DETECTION / VULNERABILITY / HUMAN_RESPONSE; SUCCESS/PARTIAL/PENDING/FAILED).
- Per-simulation ATT&CK results (`/injects/results-by-attack-patterns`); home **security-coverage matrix** (Elasticsearch term aggregation, tenant-wide).
- Security platforms (EDR/XDR/SIEM/...), collectors, injectors.
- Findings (cve, credentials, ports, ...), scenarios + kill-chain phases, injector contracts (the technique library), atomic testing.

---

## 5. Gap assessment by CTEM stage

Legend for gap owner: **[CTI]** OpenCTI, **[AEV]** OpenAEV, **[X1]** XTM One orchestration.

### 5.1 Scoping

**Have:** OpenAEV agent coverage of endpoints (the real "what is in scope and
protected" signal); OpenCTI observable/identity breadth.

**Gaps:**
- **[AEV][CTI] No business-criticality or ownership model.** Endpoints/asset groups
  carry no criticality tier, business owner, or business-service mapping; OpenCTI
  identities are not a CMDB. CTEM Scoping is explicitly "align to business impact"
  - we cannot, because the data does not exist.
- **[AEV][CTI] Attack surface is narrow.** No external attack surface management
  (EASM), no cloud posture (CSPM), no identity exposure (ITDR), no code/secrets.
  Gartner Discovery+Scoping expect the *full* surface; we cover endpoints + intel.
- **[X1] No scope-domain construct.** No notion of "regulated zone", "internet-
  facing", "primary-asset service" to scope a program to.

**Recommend:** asset criticality + owner + business-service fields (AEV endpoints /
asset groups, or a shared model); EASM / cloud / identity discovery connectors;
a "scope domain" tag that drives every downstream stage filter.

### 5.2 Discovery

**Have:** OpenCTI threats / CVEs / indicators / ATT&CK; OpenAEV findings; our
threat-informed coverage metric.

**Gaps:**
- **[CTI] Vulnerability data is intel-side, not environment-side.** OpenCTI knows a
  CVE exists and its CVSS/EPSS/KEV; it does not robustly model "this CVE instance
  is present on these N of my assets" (no scalable affected-asset / reachability
  link). True Discovery needs the *instance*, not the *catalog entry*.
- **[AEV] Findings have no severity or status** and are simulation-derived, not a
  discovery scanner output. They cannot stand in for vulnerability discovery.
- **[CTI/AEV] No misconfiguration / identity / secrets discovery** (out of scope
  for both products today).
- **[X1] Threat-informed coverage is data-quality dependent.** It assumes OpenCTI
  `uses` relationships are populated and MITRE external IDs align across products.
  When intel is sparse, the "threat technique" denominator is thin.

**Recommend:** an asset<->vulnerability *instance* model (with affected assets and
basic reachability); vuln-scanner / EASM connectors feeding OpenCTI or a shared
exposure store; finding severity + status in OpenAEV.

### 5.3 Prioritization

**Have:** KEV / critical / high-EPSS counts, indicator sightings, PIR count.

**Gaps:**
- **[CTI/AEV/X1] No attack-path / reachability analysis.** The single biggest
  prioritization lever in modern CTEM ("only ~2% of exposures reach critical
  assets") needs attack-path graph analytics over the customer's environment.
  Neither product computes this; OpenCTI relationship graphs are intel graphs, not
  environment attack graphs.
- **[X1] No per-exposure composite risk score.** We score at the *stage* level. There
  is no per-CVE / per-asset risk that fuses severity x KEV/EPSS x reachability x
  asset criticality x adversary relevance - the actual CTEM prioritization output.
- **[CTI] PIR usage is shallow.** We count PIRs; we do not compute "which of my
  exposures match a PIR" or a per-exposure PIR-relevance score (the `pir_score` on
  entities exists but is not wired to exposures/CVEs).
- **[CTI] Sightings are intel sightings**, not "seen on my asset / by my sensors".
- **Business-impact weighting is impossible** without the asset model (5.1).

**Recommend:** a per-exposure composite risk score; attack-path analytics (or a
connector to one); PIR-to-exposure matching (use `pir_score`); business-impact
weighting once asset criticality exists.

### 5.4 Validation

**Have:** OpenAEV BAS (prevention/detection/human), per-technique ATT&CK results,
the global ES coverage matrix, security-platform depth. The OpenCTI -> OpenAEV
threat-informed validation workflow is real and is OpenAEV's strongest CTEM card.

**Gaps:**
- **[X1/AEV] The intel -> scenario loop is largely manual.** An analyst maps a
  report's TTPs to a scenario by hand. There is no turnkey "generate and launch a
  validation scenario for this OpenCTI threat/incident/technique-gap" pipeline at
  scale. (We have helper agents, not an automated closed loop.)
- **[AEV] Validation is technique-level, not exposure-level.** OpenAEV proves "can
  technique T1059 be detected/prevented", not "is CVE-X on asset-Y actually
  exploitable given its controls". Gartner Validation is exposure-specific; we meet
  the control-efficacy half, not the vulnerability-reachability half.
- **[AEV] BAS is not exploit validation.** No automated exploitation/pen-test of a
  specific CVE instance.
- **[CTI<-AEV] No write-back of a validation verdict.** A simulation result does not
  flag the originating OpenCTI exposure as "validated / refuted", so prioritization
  never learns from validation (the loop does not close).

**Recommend:** automated intel->scenario generation + scheduling; an exposure-level
validation linkage (validate a specific CVE/asset, not just a technique); write the
validation verdict back to OpenCTI so prioritization de-escalates refuted risk
(the "84% false-urgency reduction" CTEM promise).

### 5.5 Mobilization (the biggest gap)

**Have:** OpenCTI cases (incident/RFI/RFT) + workflow statuses; OpenAEV remediation
collectors; our case-closure metric + playbook coverage; XTM One agents that *can*
act.

**Gaps:**
- **[CTI] No SLA model.** Cases have no due date or SLA (only `Task.due_date`
  exists). No escalation on breach, no MTTR primitive (we approximate via
  `created_at` -> `updated_at`).
- **[X1] No ticketing as a first-class CTEM action.** A validated exposure does not
  become a Jira/ServiceNow ticket with technical context + owner + SLA. XTM One has
  integrations and agents, but no wired "mobilize this exposure" flow. Industry
  guidance is explicit that ticketing integration + SLA enforcement is where CTEM
  succeeds or stalls.
- **[CTI/AEV] No ownership model** -> nothing to route to.
- **[X1] No exception / risk-acceptance workflow.**
- **[X1/AEV] No automatic re-validation.** After a fix, nothing re-runs the BAS
  scenario to confirm closure. The loop is open.
- **[X1] No risk-reduction / executive reporting primitive** (business terms, not
  CVE counts). Our briefing is narrative, not a quantified risk-burn-down.
- **[CTI] Playbooks are intel-pipeline automation**, not remediation orchestration.

**Recommend (highest priority):** SLA + ownership model; ticketing connectors as a
first-class mobilization action; an automatic re-validation loop (fix -> re-run
scenario -> verify); a quantified risk-reduction report.

---

## 6. Cross-cutting gaps

1. **Asset / business-context model (the spine).** The single biggest structural
   gap. Without "asset + criticality + owner + business service", Scoping,
   Prioritization (business impact) and Mobilization (routing) are all capped.
2. **Unified exposure object + API.** No shared entity for an exposure spanning both
   products; we reconstruct it per-view in the dashboard. There is no contract to
   carry "this exposure was discovered here, prioritized this high, validated this
   way, and is being remediated under this SLA".
3. **Continuous-ness.** Collection is per-view / scheduled, not event-driven. Trend
   history is thin (we infer trends; there is no exposure event stream or
   point-in-time exposure snapshots to compute true MTTR / dwell / burn-down).
4. **Concrete API parity gaps we actually hit:**
   - OpenAEV had **no tenant-wide ATT&CK coverage API** (the home matrix is a
     dashboard *widget* needing a per-tenant UUID, split per prevention/detection,
     and computed in Elasticsearch). We are adding a parameter-free `/coverage`
     endpoint that reuses the *same* ES aggregation for number parity.
   - OpenAEV **findings have no severity/status**.
   - OpenCTI **cases have no SLA/due-date**.
   - OpenCTI has **no per-entity PIR score query** convenient for exposure scoring.
   - OpenAEV ES status is binary SUCCESS/FAILED while the PG path counts
     PARTIAL/PENDING - two coverage numbers can disagree if you mix the paths.

---

## 7. Multi-vertical: FIMI & Fraud gaps + a creative OpenAEV extension

OpenAEV is, today, a **cyber** BAS engine. The CTEM loop is universal, but
"Validation" outside cyber needs imagination.

### FIMI (foreign information manipulation / disinformation)
- **Have:** OpenCTI is FIMI-native (narratives, channels, events, attribution,
  infrastructure). Our FIMI verticalization scores Scoping/Discovery/Prioritization
  /Validation/Mobilization off OpenCTI.
- **Gaps:** **[AEV]** No FIMI validation at all - OpenAEV cannot exercise an
  information-operation. **[CTI]** No DISARM framework support (DISARM is the
  ATT&CK-equivalent for influence operations).
- **Creative extension (the idea worth pursuing):** generalize OpenAEV's
  **human-response / tabletop** capability to FIMI. Run a *crisis-simulation /
  tabletop* exercise: inject a simulated disinformation campaign targeting the
  brand and measure the organization's **detection** (does the SOC/comms team spot
  the narrative?) and **response** (time-to-counter, escalation, exec comms) as new
  expectation types. Map injects to **DISARM** TTPs the way cyber maps to ATT&CK.
  This makes "validation" meaningful for FIMI: you are validating *organizational
  readiness*, not a control.

### Fraud
- **Have:** OpenCTI observables/cases (phishing domains, leaked credentials/cards,
  personas, takedown RFTs). OpenAEV phishing simulations map naturally to
  human-response (we already surface phishing prevention).
- **Gaps:** **[AEV]** No fraud-specific inject/scenario library (account-takeover,
  payment-fraud, brand-abuse takedown drills); **[CTI]** findings have no fraud
  typing beyond raw observables.
- **Creative extension:** fraud "validation" = social-engineering + ATO simulation
  + takedown-time drills, with new expectation types like `takedown-time` and
  `counter-response-time`.

### Shared OpenAEV gap
The expectation model (PREVENTION / DETECTION / VULNERABILITY / HUMAN_RESPONSE) is
cyber-shaped. FIMI/Fraud need **new expectation types** and **non-ATT&CK technique
libraries** (DISARM for FIMI; a fraud TTP library). This is the key OpenAEV product
investment to make CTEM genuinely multi-vertical rather than cyber-with-relabeled-stages.

---

## 8. Actionability: insight -> action

Today the command center insights (scores, AI assessments, recommended actions) are
**read-only**; acting on them is manual or via separate agents. For genuine
actionability, each exposure / gap should offer one-click, wired actions:

- "Create remediation ticket" (Jira/ServiceNow) with context + owner + SLA.
- "Launch a validation scenario" for this untested technique / exposure.
- "Open a case" / "trigger a playbook".
- "Assign owner + SLA" and track to verified closure.

We have the substrate for this (XTM One agents, integrations, assignments) - what is
missing is the **wiring from a specific dashboard exposure to a specific CTEM
action**, plus the SLA/ownership model those actions need.

---

## 9. Prioritized roadmap

| # | Gap | Owner | Impact | Effort | Priority |
|---|---|---|---|---|---|
| 1 | Asset + business-context model (criticality, owner, business service) | AEV / CTI | Very high (unlocks Scoping, Prioritization, Mobilization) | High | P0 |
| 2 | Mobilization: SLA + ownership + ticketing + re-validation loop | X1 / CTI | Very high (the stall point) | High | P0 |
| 3 | Unified exposure object + validation verdict write-back | CTI / AEV / X1 | High (closes the loop) | High | P1 |
| 4 | Per-exposure composite risk + attack-path / reachability | X1 / CTI | High (real prioritization) | High | P1 |
| 5 | Automated intel -> scenario validation pipeline | X1 / AEV | High | Medium | P1 |
| 6 | FIMI/Fraud OpenAEV extension (DISARM + tabletop/human-response + new expectation types) | AEV | Medium-high (true multi-vertical) | High | P2 |
| 7 | API parity fixes: finding severity/status, case SLA, per-entity PIR score, tenant-wide ATT&CK matrix | CTI / AEV | Medium (removes blind spots) | Low-medium | P2 (matrix in progress) |
| 8 | Continuous exposure event stream + point-in-time snapshots (true MTTR / dwell / burn-down) | X1 | Medium | Medium | P2 |
| 9 | Discovery breadth: EASM / cloud / identity / secrets connectors | CTI / X1 | Medium | High | P3 |

---

## 10. Honest caveats (what we do NOT claim)

- The command center measures **program-level posture and coverage**, not
  per-exposure risk. It is a maturity / coverage lens, not a vulnerability-management
  workbench.
- "Validation" today proves **control efficacy by technique**, not the
  exploitability of a **specific CVE on a specific asset**.
- Many signals are **data-quality dependent**: PIR and `uses` relationships must be
  populated, agents must be deployed, simulations must be run. Sparse data yields
  thin (but honest) numbers - which is why every metric now carries a provenance
  note.
- We are **strong on the threat-informed loop** (OpenCTI threats <-> OpenAEV
  validation) and **weak on the business/asset spine and mobilization** - the
  inverse of most VM tools, and complementary to them.

---

## 11. Sources

- Gartner CTEM five-stage framework (Scoping, Discovery, Prioritization, Validation,
  Mobilization) and 2026 practitioner guidance (ArmorCode, Vectra, Splunk, Brinqa,
  Palo Alto Networks, Wiz, decryptiondigest CTEM Guide 2026).
- Blade Intel, "Why Threat Intelligence Is the Missing Link in CTEM Prioritization
  and Validation" - describes the OpenCTI -> OpenAEV threat-informed validation
  workflow and PIR operationalization.
- Code-level API investigations of OpenCTI (`opencti-graphql`) and OpenAEV
  (`openaev-api` / `openaev-front`) performed for the command center.
- The XTM One CTEM command center implementation (`backend/app/services/ctem_*`,
  `frontend/src/pages/xtm-one/`).
