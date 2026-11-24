// SPDX-License-Identifier: Apache-2.0

/**
 * Representative SAMPLE CTEM data.
 *
 * Rendered (greyed, non-interactive) when a stage - or the whole posture - has
 * no real data yet, so the command center always previews exactly what it will
 * look like once OpenCTI / OpenAEV are connected and reporting. This is clearly
 * labelled "sample" in the UI and is replaced automatically by live data; it is
 * never persisted or sent anywhere.
 */
import type { CtemBriefing, CtemFact, CtemPosture, CtemStage, CtemVertical } from "@/types";

const f = (
  label: string,
  value: number | string | boolean,
  unit = "",
  kind: "fact" | "derived" = "fact",
) => ({ label, value, unit, kind });

// Compact CtemStage builder for the FIMI / Fraud sample stages (always shown as a
// fully-connected preview). Defaults to a single OpenCTI primary product and no
// dedicated chart - so non-cyber verticals never inherit cyber CVSS/donut viz.
const mk = (
  key: string,
  label: string,
  score: number,
  prev_score: number,
  band: CtemStage["band"],
  facts: CtemFact[],
  methodology: string,
  opts: { primary?: string[]; secondary?: string[]; viz?: CtemStage["viz"] } = {},
): CtemStage => ({
  key,
  label,
  score,
  prev_score,
  band,
  covered: true,
  confidence: "high",
  primary_products: opts.primary ?? ["opencti"],
  secondary_products: opts.secondary ?? [],
  missing_products: [],
  products_present: [...(opts.primary ?? ["opencti"]), ...(opts.secondary ?? [])],
  methodology,
  facts,
  viz: opts.viz ?? {},
});

const DEMO_STAGE_LIST: CtemStage[] = [
  {
    key: "scoping",
    label: "Scoping",
    score: 84,
    prev_score: 80,
    band: "good",
    covered: true,
    confidence: "high",
    primary_products: ["opencti", "openaev"],
    secondary_products: [],
    missing_products: [],
    products_present: ["opencti", "openaev"],
    methodology: "Breadth of the attack surface in scope: ATT&CK domain coverage, asset inventory and detection sources (agent-agnostic).",
    facts: [
      f("Attack surface coverage", 71, "%", "derived"),
      f("Endpoints in scope", 1240),
      f("Asset groups", 18),
      f("Attack domains covered", 5),
      f("Security platforms", 7),
      f("CVEs tracked", 4214),
      f("Observables", 52800),
      f("Priority intel requirements", 6),
    ],
    viz: {},
  },
  {
    key: "discovery",
    label: "Discovery",
    score: 64,
    prev_score: 58,
    band: "at_risk",
    covered: true,
    confidence: "high",
    primary_products: ["opencti"],
    secondary_products: [],
    missing_products: [],
    products_present: ["opencti"],
    methodology: "ATT&CK techniques covered by at least one detection.",
    facts: [
      f("Threat-informed coverage", 64, "%", "derived"),
      f("Detection coverage", 71, "%", "derived"),
      f("Threat techniques", 188),
      f("Simulated techniques", 142),
      f("Untested techniques", 68),
      f("Intrusion sets", 318),
      f("Threats in PIR scope", 84),
      f("Malware", 1106),
      f("Valid indicators", 38500),
    ],
    viz: {},
  },
  {
    key: "prioritization",
    label: "Prioritization",
    score: 64,
    prev_score: 58,
    band: "at_risk",
    covered: true,
    confidence: "high",
    primary_products: ["opencti"],
    secondary_products: ["openaev"],
    missing_products: [],
    products_present: ["opencti", "openaev"],
    methodology: "Exposure containment, weighed against exploitability, observed activity, PIRs and your software.",
    facts: [
      f("Known-exploited (KEV)", 47),
      f("Critical CVEs", 213),
      f("High-EPSS CVEs", 156),
      f("Indicator sightings", 1820),
      f("Priority intel requirements", 6),
      f("Total CVEs", 4214),
      f("Findings", 89),
    ],
    viz: {
      severity_distribution: [
        { label: "critical", value: 213 },
        { label: "high", value: 542 },
        { label: "medium", value: 981 },
        { label: "low", value: 418 },
      ],
      top_vulnerabilities: [
        { name: "CVE-2021-44228 (Log4Shell)", cvss: 10, severity: "critical", epss: 0.97, kev: true },
        { name: "CVE-2024-3400 (PAN-OS)", cvss: 10, severity: "critical", epss: 0.94, kev: true },
        { name: "CVE-2023-23397 (Outlook)", cvss: 9.8, severity: "critical", epss: 0.89, kev: true },
      ],
    },
  },
  {
    key: "validation",
    label: "Validation",
    score: 67,
    prev_score: 61,
    band: "fair",
    covered: true,
    confidence: "high",
    primary_products: ["opencti", "openaev"],
    secondary_products: [],
    missing_products: [],
    products_present: ["opencti", "openaev"],
    methodology: "OpenAEV prevention vs detection efficacy and exercised ATT&CK, backed by detection-stack depth.",
    facts: [
      f("Prevention success", 72, "%", "derived"),
      f("Detection success", 66, "%", "derived"),
      f("ATT&CK exercised", 58, "%", "derived"),
      f("Validated techniques", 96),
      f("Finished simulations", 34),
      f("Detection sources", 5),
      f("Active collectors", 4),
    ],
    viz: {
      // Every expectation type OpenAEV reports renders its own donut.
      expectation_buckets: {
        PREVENTION: { SUCCESS: 221, PARTIAL: 58, PENDING: 21, FAILED: 88 },
        DETECTION: { SUCCESS: 184, PARTIAL: 72, PENDING: 26, FAILED: 106 },
        VULNERABILITY: { SUCCESS: 142, PARTIAL: 34, PENDING: 12, FAILED: 61 },
        HUMAN_RESPONSE: { SUCCESS: 96, PARTIAL: 41, PENDING: 18, FAILED: 73 },
      },
    },
  },
  {
    key: "remediation",
    label: "Remediation",
    score: 76,
    prev_score: 70,
    band: "good",
    covered: true,
    confidence: "high",
    primary_products: ["opencti"],
    secondary_products: ["openaev"],
    missing_products: [],
    products_present: ["opencti", "openaev"],
    methodology: "Closure of response cases (incident + RFI), with automation playbooks and the validation backlog.",
    facts: [
      f("Case closure", 76, "%", "derived"),
      f("Incident cases", 64),
      f("RFI cases", 22),
      f("Automation playbooks", 9),
      f("Open findings", 89),
      f("Failed/partial validations", 330),
    ],
    viz: {},
  },
];

const DEMO_STAGE_BY_KEY: Record<string, CtemStage> = Object.fromEntries(
  DEMO_STAGE_LIST.map((s) => [s.key, s]),
);

const DEMO_POSTURE: CtemPosture = {
  posture_score: 78,
  posture_band: "good",
  covered_stages: 5,
  total_stages: 5,
  weights: { scoping: 0.15, discovery: 0.25, prioritization: 0.25, validation: 0.2, remediation: 0.15 },
  stages: DEMO_STAGE_LIST,
  products: {
    opencti: { present: true, healthy: true, version: "sample", url: null },
    openaev: { present: true, healthy: true, version: "sample", url: null },
    opencrq: { present: false, coming_soon: true },
  },
  trend: [61, 63, 62, 65, 67, 66, 69, 71, 70, 73, 75, 78].map((score, i) => ({
    date: `d${i}`,
    score,
  })),
  blind_spots: [],
  headline: {
    kev_count: 47,
    critical_count: 213,
    vulnerabilities_total: 4214,
    detection_coverage_pct: 71,
    prevention_success_pct: 72,
    detection_success_pct: 66,
    exercised_coverage_pct: 58,
    validation_success_pct: 69,
  },
  last_sync: {},
};

interface DemoActivity {
  id: string;
  agent: string;
  action: string;
  status: "completed" | "running" | "failed";
  ago: string;
}

export const DEMO_ACTIVITY: DemoActivity[] = [
  { id: "a1", agent: "OpenCTI Dashboard Creator", action: "Refreshing the executive exposure dashboard", status: "running", ago: "now" },
  { id: "a2", agent: "CTI STIX Harvester", action: "Imported 1,204 indicators from threat feeds", status: "completed", ago: "2m" },
  { id: "a3", agent: "OpenAEV Scenario Creator", action: "Built a ransomware emulation across 12 ATT&CK techniques", status: "completed", ago: "14m" },
  { id: "a4", agent: "CTEM Assistant", action: "Prioritized 47 known-exploited exposures for remediation", status: "completed", ago: "31m" },
  { id: "a5", agent: "AEV Detection Rules Generator", action: "Drafted 8 Sigma rules from validation gaps", status: "completed", ago: "1h" },
];

/** Sample weekly briefing (greyed preview until the analyst publishes its first
 *  real one). Mirrors what the seeded Exposure Briefing Analyst produces. */
const DEMO_BRIEFING: CtemBriefing = {
  headline: "Exposure held steady, but an exploited CVE maps to an undetected technique",
  summary:
    "Threat intake stayed high with 47 known-exploited CVEs tracked. Controls held prevention at 72% while detection lagged at 66%. One actively exploited technique slipped past detection in last week's emulations.",
  posture_trend: "stable",
  coverage: { opencti: true, openaev: true },
  key_findings: [
    { title: "Log4Shell (CVE-2021-44228) exploited in the wild", detail: "CISA KEV, CVSS 10, mapped to 3 internet-facing assets in scope.", severity: "critical", source: "opencti", recommendation: "Patch or virtually-patch the 3 exposed assets within the KEV SLA, then re-scan to confirm." },
    { title: "T1059 Command & Scripting not detected", detail: "Exercised by 4 simulations last week; detected by 0.", severity: "high", source: "openaev", recommendation: "Author and deploy an EDR/SIEM detection for T1059, then re-run the emulation." },
    { title: "Detection trails prevention by 6 points", detail: "Detection 66% vs prevention 72% - gaps in lateral movement.", severity: "medium", source: "openaev", recommendation: "Prioritize detection content for lateral-movement techniques in the next sprint." },
    { title: "ATT&CK intel coverage up 4%", detail: "New intelligence expanded technique coverage to 71%.", severity: "info", source: "opencti" },
  ],
  cross_product_insight:
    "An actively exploited CVE (Log4Shell) maps to T1059 - the exact technique your controls failed to detect last week. Closing this single loop removes a live, validated attack path.",
  recommended_focus:
    "Deploy and validate a detection for T1059 on internet-facing assets, then re-run the emulation to confirm the gap is closed.",
  next_steps: [
    { action: "Patch the 3 internet-facing assets exposed to Log4Shell", rationale: "Actively exploited (CISA KEV) on in-scope crown-jewel assets - highest live risk.", priority: "high" },
    { action: "Deploy a detection for T1059 Command & Scripting", rationale: "Exercised by 4 simulations and detected by none - a validated blind spot.", priority: "high" },
    { action: "Re-run the ransomware emulation after the T1059 detection ships", rationale: "Closes the loop - proves the gap is actually fixed, not assumed.", priority: "medium" },
    { action: "Close detection-vs-prevention gap on lateral movement", rationale: "Detection trails prevention by 6 points; lateral movement is the weak phase.", priority: "medium" },
  ],
};

// ─────────────────────────────────────────────────────────────────────────────
// FIMI / disinformation sample (narratives, channels, attribution - NO CVSS/CVE)
// ─────────────────────────────────────────────────────────────────────────────
const DEMO_STAGE_LIST_FIMI: CtemStage[] = [
  mk("scoping", "Scoping", 70, 64, "fair", [
    f("Information-space coverage", 70, "%", "derived"),
    f("Narratives tracked", 142),
    f("Channels monitored", 386),
    f("Events", 57),
    f("Languages", 12),
  ], "Breadth of narratives, channels, languages and events tracked in OpenCTI."),
  mk("discovery", "Discovery", 58, 51, "at_risk", [
    f("Monitoring depth", 58, "%", "derived"),
    f("Media content", 4820),
    f("Channel platforms", 9),
    f("Narratives", 142),
  ], "Media content captured and the diversity of channel platforms under watch."),
  mk("prioritization", "Prioritization", 63, 59, "fair", [
    f("Attributed channels", 63, "%", "derived"),
    f("Channels attributed", 243),
    f("Election events", 11),
    f("Channels monitored", 386),
  ], "Share of monitored channels attributed to a threat actor / intrusion set."),
  mk("validation", "Validation", 49, 44, "at_risk", [
    f("Corroborated channels", 49, "%", "derived"),
    f("Channels with infrastructure", 189),
    f("Incidents", 27),
  ], "Share of channels with mapped infrastructure, evidencing real operations."),
  mk("remediation", "Mobilization", 66, 60, "fair", [
    f("Case closure", 66, "%", "derived"),
    f("Incident cases", 31),
    f("Open incidents", 27),
  ], "Closure rate of FIMI incident / response cases."),
];

const DEMO_POSTURE_FIMI: CtemPosture = {
  posture_score: 61,
  posture_band: "fair",
  covered_stages: 5,
  total_stages: 5,
  vertical: "fimi",
  weights: { scoping: 0.2, discovery: 0.2, prioritization: 0.25, validation: 0.2, remediation: 0.15 },
  stages: DEMO_STAGE_LIST_FIMI,
  products: {
    opencti: { present: true, healthy: true, version: "sample", url: null },
    openaev: { present: false, coming_soon: false },
    opencrq: { present: false, coming_soon: true },
  },
  trend: [54, 56, 55, 58, 57, 60, 59, 61].map((score, i) => ({ date: `d${i}`, score })),
  blind_spots: [],
  headline: {
    kev_count: null, critical_count: null, vulnerabilities_total: null,
    detection_coverage_pct: null, prevention_success_pct: null,
    detection_success_pct: null, exercised_coverage_pct: null,
    narratives_total: 142, channels_total: 386, attribution_pct: 63,
    corroboration_pct: 49, incidents: 27, case_closure_pct: 66,
  },
  last_sync: {},
};

const DEMO_BRIEFING_FIMI: CtemBriefing = {
  headline: "A coordinated election narrative is gaining reach across 3 new channels",
  summary:
    "Tracking stayed broad - 142 narratives across 386 channels in 12 languages - but only 49% of active channels are corroborated with infrastructure and 63% are attributed. Anonymous amplifiers remain the blind spot.",
  posture_trend: "stable",
  coverage: { opencti: true, openaev: false },
  key_findings: [
    { title: "Election-targeting narrative amplifying", detail: "11 election events linked; cross-posted on 3 new channels this week.", severity: "high", source: "opencti", recommendation: "Map the amplifier channels to their hosting infrastructure and open a response case." },
    { title: "37% of active channels are unattributed", detail: "Anonymous amplifiers dilute attribution and slow takedown.", severity: "medium", source: "opencti", recommendation: "Cluster anonymous channels by behavioural fingerprints to attribute the operation." },
    { title: "Corroboration lags at 49%", detail: "Half of monitored channels have no mapped publishing infrastructure.", severity: "medium", source: "opencti", recommendation: "Enrich channels with domain/URL infrastructure to evidence real operations." },
    { title: "Narrative coverage expanded", detail: "12 languages now under watch (+2 this month).", severity: "info", source: "opencti" },
  ],
  cross_product_insight:
    "The fastest-growing narrative is also the least attributed - closing attribution on its top 3 channels would unlock coordinated takedown.",
  recommended_focus:
    "Attribute and map infrastructure for the election narrative's amplifier channels, then open response cases.",
  next_steps: [
    { action: "Attribute the top 3 amplifier channels of the election narrative", rationale: "Highest-reach narrative with the weakest attribution - blocks coordinated response.", priority: "high" },
    { action: "Map publishing infrastructure for uncorroborated channels", rationale: "Corroboration at 49% - infrastructure evidence is needed for takedown.", priority: "high" },
    { action: "Open response cases for the 27 active incidents", rationale: "Incidents are outpacing case closure (66%).", priority: "medium" },
    { action: "Expand monitoring on 2 emerging platforms", rationale: "Amplification is shifting to platforms not yet fully covered.", priority: "medium" },
  ],
};

// ─────────────────────────────────────────────────────────────────────────────
// Fraud sample (brands, phishing, leaked data, takedowns - NO CVSS/ATT&CK)
// ─────────────────────────────────────────────────────────────────────────────
const DEMO_STAGE_LIST_FRAUD: CtemStage[] = [
  mk("scoping", "Scoping", 80, 73, "good", [
    f("Brand & asset coverage", 80, "%", "derived"),
    f("Brands monitored", 14),
    f("Domains watched", 320),
    f("Impersonation personas", 86),
  ], "Monitored brands, watched domains and impersonation personas in scope."),
  mk("discovery", "Discovery", 60, 55, "fair", [
    f("Fraud surface discovered", 60, "%", "derived"),
    f("Phishing domains/URLs", 742),
    f("Leaked credentials", 18400),
    f("Leaked cards", 2630),
    f("Phishing infrastructure", 318),
  ], "Phishing domains/URLs, leaked credentials & cards, personas and infrastructure."),
  mk("prioritization", "Prioritization", 57, 52, "at_risk", [
    f("Containment", 57, "%", "derived"),
    f("Live phishing infra", 138),
    f("Leaked cards", 2630),
    f("Leaked credentials", 18400),
  ], "Share of fraud indicators that are NOT high-confidence live threats."),
  mk("validation", "Validation", 64, 58, "fair", [
    f("Confirmed malicious", 64, "%", "derived"),
    f("Validated indicators", 512),
    f("Phishing prevention", 71, "%", "derived"),
  ], "Validated-malicious share of fraud indicators, proven by phishing simulations.", { primary: ["opencti", "openaev"] }),
  mk("remediation", "Mobilization", 73, 66, "fair", [
    f("Takedown closure", 73, "%", "derived"),
    f("Takedown cases", 96),
    f("Open takedowns", 26),
  ], "Closure rate of phishing / brand-abuse takedown cases.", { secondary: ["openaev"] }),
];

const DEMO_POSTURE_FRAUD: CtemPosture = {
  posture_score: 66,
  posture_band: "fair",
  covered_stages: 5,
  total_stages: 5,
  vertical: "fraud",
  weights: { scoping: 0.15, discovery: 0.25, prioritization: 0.25, validation: 0.2, remediation: 0.15 },
  stages: DEMO_STAGE_LIST_FRAUD,
  products: {
    opencti: { present: true, healthy: true, version: "sample", url: null },
    openaev: { present: true, healthy: true, version: "sample", url: null },
    opencrq: { present: false, coming_soon: true },
  },
  trend: [58, 60, 59, 62, 64, 63, 65, 66].map((score, i) => ({ date: `d${i}`, score })),
  blind_spots: [],
  headline: {
    kev_count: null, critical_count: null, vulnerabilities_total: null,
    detection_coverage_pct: null, prevention_success_pct: 71,
    detection_success_pct: null, exercised_coverage_pct: null,
    phishing_assets: 742, credentials_total: 18400, confirmed_pct: 64,
    containment_pct: 57, case_closure_pct: 73,
  },
  last_sync: {},
};

const DEMO_BRIEFING_FRAUD: CtemBriefing = {
  headline: "Phishing infrastructure spiked - leaked credentials are the validated takeover path",
  summary:
    "742 phishing domains/URLs and 318 infrastructure nodes are tracked alongside 18.4k leaked credentials. Confirmed-malicious sits at 64% and takedown closure at 73%, but 26 takedowns remain open.",
  posture_trend: "stable",
  coverage: { opencti: true, openaev: true },
  key_findings: [
    { title: "138 live phishing infrastructure nodes", detail: "High-confidence active infra impersonating monitored brands.", severity: "high", source: "opencti", recommendation: "Submit takedowns for the 138 live nodes and block them at the email/web gateway." },
    { title: "18,400 leaked credentials in the wild", detail: "Exposed credentials enable account-takeover fraud.", severity: "high", source: "opencti", recommendation: "Force-reset matched accounts and enroll them in step-up authentication." },
    { title: "Phishing simulation click rate 29%", detail: "Human-response readiness below target on Finance & Support.", severity: "medium", source: "openaev", recommendation: "Run targeted awareness for the highest-click departments, then re-test." },
    { title: "Brand coverage expanded", detail: "14 brands now monitored (+2 this quarter).", severity: "info", source: "opencti" },
  ],
  cross_product_insight:
    "Leaked credentials map to the same brands targeted by live phishing infra - resetting exposed accounts removes a validated account-takeover path.",
  recommended_focus:
    "Take down the 138 live phishing nodes and reset credentials for impacted brands, then re-run the phishing simulation.",
  next_steps: [
    { action: "Submit takedowns for the 138 live phishing infrastructure nodes", rationale: "Active, high-confidence impersonation of monitored brands - live customer risk.", priority: "high" },
    { action: "Force-reset the accounts behind leaked credentials", rationale: "18.4k exposed credentials are a direct account-takeover vector.", priority: "high" },
    { action: "Re-run the phishing simulation after awareness training", rationale: "29% click rate - confirm human-response readiness actually improves.", priority: "medium" },
    { action: "Clear the 26 open takedown cases", rationale: "Open takedowns extend brand-abuse dwell time; closure at 73%.", priority: "medium" },
  ],
};

// ── Per-vertical accessors (the command center reshapes to the active vertical) ─
const DEMO_STAGE_BY_KEY_BY_VERTICAL: Record<CtemVertical, Record<string, CtemStage>> = {
  cyber: DEMO_STAGE_BY_KEY,
  fimi: Object.fromEntries(DEMO_STAGE_LIST_FIMI.map((s) => [s.key, s])),
  fraud: Object.fromEntries(DEMO_STAGE_LIST_FRAUD.map((s) => [s.key, s])),
};

const DEMO_POSTURE_BY_VERTICAL: Record<CtemVertical, CtemPosture> = {
  cyber: DEMO_POSTURE,
  fimi: DEMO_POSTURE_FIMI,
  fraud: DEMO_POSTURE_FRAUD,
};

const DEMO_BRIEFING_BY_VERTICAL: Record<CtemVertical, CtemBriefing> = {
  cyber: DEMO_BRIEFING,
  fimi: DEMO_BRIEFING_FIMI,
  fraud: DEMO_BRIEFING_FRAUD,
};

/** Sample stages keyed by stage key for a vertical (merge/preview placeholder). */
export function demoStageByKey(vertical: CtemVertical): Record<string, CtemStage> {
  return DEMO_STAGE_BY_KEY_BY_VERTICAL[vertical] ?? DEMO_STAGE_BY_KEY;
}
/** Sample posture for a vertical (fully-connected preview). */
export function demoPosture(vertical: CtemVertical): CtemPosture {
  return DEMO_POSTURE_BY_VERTICAL[vertical] ?? DEMO_POSTURE;
}
/** Sample briefing for a vertical (greyed preview until the analyst publishes). */
export function demoBriefing(vertical: CtemVertical): CtemBriefing {
  return DEMO_BRIEFING_BY_VERTICAL[vertical] ?? DEMO_BRIEFING;
}
