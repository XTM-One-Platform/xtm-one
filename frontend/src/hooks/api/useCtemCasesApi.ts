// SPDX-License-Identifier: Apache-2.0

/**
 * CTEM Cases (dev-docs/ctem-cases.md): open exposures grouped by the fix they
 * share. Every read renders the case for the caller from their own source
 * read (#11613): a case they see nothing of is absent (404 on its page), a
 * member issue they cannot see is a bare `{ restricted: true }` row.
 */
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, notFoundIsOrdinary } from "@/lib/api";
import { REFETCH_INTERVAL_SLOW, type CtemStageKey } from "@/lib/constants";
import { isNetworkError, retryQuery } from "@/lib/query-outage-policy";

/**
 * The statuses of a case in lifecycle order (backend models/ctem_case.CASE_STATES, #1503): the
 * loop moves a case through the open ones itself; a person accepts risk.
 */
export const CTEM_CASE_STATES = [
  "new",
  "analyzing",
  "waiting_decision",
  "remediating",
  "validating",
  "mitigated_watching",
  "resolved",
  "accepted_risk",
] as const;
export type CtemCaseState = (typeof CTEM_CASE_STATES)[number];
/** How a case came to be (`ctem_case_delete.origin`, #1723): a signal, a split, or a source read before signals. */
export type CtemCaseOpenedBy = "signal" | "split" | "legacy";
/** The statuses a case is still worked in (an SLA runs, a new signal joins it): backend OPEN_STATES. */
export const CTEM_OPEN_CASE_STATES = ["new", "analyzing", "waiting_decision", "remediating", "validating"] as const;

/** True for a status the case is still worked in. */
export function isOpenCaseState(state: string | null | undefined): boolean {
  return (CTEM_OPEN_CASE_STATES as readonly string[]).includes(state ?? "");
}
export type CtemCasePriority = "critical" | "high" | "medium" | "low";

/** A case as one list row (`summary` in backend/app/services/ctem_cases_view.py). */
export interface CtemCaseSummary {
  id: string;
  title: string;
  remediation_kind: string;
  product_label: string | null;
  fixed_version: string | null;
  state: CtemCaseState;
  state_since: string | null;
  owner_user_id: string | null;
  due_at: string | null;
  accepted_reason: string | null;
  accepted_until: string | null;
  priority: CtemCasePriority;
  priority_basis: string;
  issue_count: number;
  open_issue_count: number;
  restricted_count: number;
  /** Open members read without their details: then the detail aggregates are null, unknown for this reader, never 0. */
  members_without_details?: number;
  aggregates_restricted?: boolean;
  aggregates_reason?: string | null;
  asset_count: number | null;
  crown_jewel_count: number | null;
  kev_count: number | null;
  max_cvss: number | null;
  max_epss: number | null;
  first_seen_at: string | null;
  /** When the case was opened and last changed (#1176). */
  created_at?: string | null;
  updated_at?: string | null;
  /** The signals that opened or joined the case (#1176). */
  signal_count?: number;
  /** How the case came to be, read from its history (#1723): the list's "Opened by" filter. */
  opened_by?: CtemCaseOpenedBy;
  /** Products that did not answer this read: their issues show without details. */
  details_unavailable: string[];
  /** The loop (#857): how the case started, its severity and SLA, its proof. */
  trigger_path?: string;
  severity?: CtemCaseSeverity | null;
  sla_due_at?: string | null;
  sla_breached_at?: string | null;
  validations?: CtemCaseValidation[];
  resolution_kind?: string | null;
  /** Ticket references (#891); the one with `sync` is the ticket the sync manages (#917). */
  tickets?: CtemCaseTicket[];
  /** The CTEM phase the case is in, from its history (#1490, #1503); null with no event, absent on an older API. */
  phase?: CtemStageKey | null;
  /** The gate the case waits on now: its key, who (a user id, a role or a policy, as the loop wrote it) and since when (#1503). */
  waiting_on?: { gate: string | null; by: string | null; since: string | null } | null;
  /** When the case sweep found the case waiting on nobody, while nothing moved it since (#1736); null otherwise. */
  stalled_since?: string | null;
}

/** Where a synced ticket stands against its case (backend ctem_case_loop.TICKET_SYNC_STATES, #917). */
export type CtemTicketSyncState = "linked" | "in_sync" | "out_of_date" | "error";

/** A ticket reference on a case (`case.tickets`, backend ctem_case_loop.record_ticket). */
export interface CtemCaseTicket {
  integration: string;
  key: string;
  url: string | null;
  status: "open" | "in_progress" | "done" | "cancelled";
  at: string;
  sync?: {
    state: CtemTicketSyncState;
    since?: string;
    provider?: string;
    external_status?: string;
    error?: string;
    fix?: string;
    /** The stable code of the fix (backend ctem_ticketing.FIX_CODES): the UI words it in the reader's language. */
    fix_code?: string;
  };
}

/** Whether a ticketing target is declared (CTEM-19-UI, #1032); absent on an older API. */
export interface CtemTicketingTarget {
  connected: boolean;
  reason?: string;
  settings: string;
}

export type CtemCaseSeverity = "P1" | "P2" | "P3";

/** One validation reference written on a case (OpenAEV run, re-test, provider scan). */
interface CtemCaseValidation {
  provider: string;
  kind: string;
  ref: string;
  /** "inconclusive" (#1692): a pass that proves nothing, the word given kept in `reported_result`. */
  result: "passed" | "not_exposed" | "failed" | "pending" | "inconclusive";
  reported_result?: "passed" | "not_exposed";
  at: string;
  mitigation_only?: boolean;
  note?: string | null;
  /** CTEM-12: the target asset groups in the order they were tested. */
  run_order?: string[];
}

/** A member issue the caller sees. */
export interface CtemCaseVisibleIssue {
  restricted?: false;
  source: string;
  external_id: string;
  open: boolean;
  name?: string;
  cve?: string | null;
  cvss?: number | null;
  epss?: number | null;
  kev?: boolean;
  assets?: string[];
  crown_jewels?: string[];
  first_seen_at?: string | null;
  details_unavailable?: boolean;
  /** Version knowledge from the viewer's own OpenCTI read (CTEM-11, #946): the declared version, the fix and where it came from. */
  installed_version?: string | null;
  fixed_version?: string | null;
  /** The OpenCTI Software ("name version") that remediates the vulnerability, when it gave the fix. */
  fixed_by?: string | null;
  /** The course of action that named the fix, when no remediating Software did. */
  course_of_action_name?: string | null;
  /** OpenCTI relates no vulnerability to the declared version: no case opens for it. */
  not_affected?: boolean;
  /** With a declared version: whether OpenCTI holds a Software of it; null when none is declared. */
  declared_version_known?: boolean | null;
  /** OpenAEV finding lifecycle (CTEM-14-UI); absent when the source carries none. */
  retest_status?: "open" | "not_reproduced" | "resolved" | null;
  last_tested_at?: string | null;
  retest_pending?: boolean;
}

/** A member issue the caller cannot see in its source product: nothing but the flag. */
export interface CtemCaseRestrictedIssue {
  restricted: true;
}

export type CtemCaseIssue = CtemCaseVisibleIssue | CtemCaseRestrictedIssue;

export interface CtemCaseEvent {
  at: string | null;
  actor: string;
  kind: string;
  /** The member it is about, or "Restricted" when the caller cannot see it. */
  issue: string | null;
  from: string | null;
  to: string | null;
  reason: string | null;
  evidence: string | null;
  /**
   * The audit trail (#1490): the CTEM phase the event belongs to, its autonomy class, who acted
   * (and the agent's name when the viewer may see it), one sentence for a note, the task, run and
   * item ids a note names. Absent on an API older than #1494.
   */
  phase?: string;
  autonomy?: string;
  actor_type?: "person" | "agent" | "system";
  actor_name?: string | null;
  headline?: string | null;
  refs?: { kind: "task" | "run" | "item"; id: string; full: boolean }[];
  /** A waiting gate: its later answer, null while it still waits. */
  answer?: { at: string | null; outcome: string | null; by: string | null; option: string | null } | null;
  /** The answer of a gate: since when it waited. */
  waited_since?: string | null;
}

/** One CTEM phase of a case (#1490): when it was entered and left, its time and counts. */
export interface CtemCasePhase {
  phase: string;
  entered_at: string | null;
  exited_at: string | null;
  seconds: number;
  events: number;
  runs: number;
  /** Gates of the phase still waiting for a person. */
  waiting: number;
  current: boolean;
  autonomy?: Record<string, number>;
}

/**
 * How the CVE would be exploited on one supporting asset of the risk scenario
 * (CTEM-24, #936), read from OpenCRQ by the triage step. `asset` is
 * "Restricted" and `risk_scenario_id` null for a viewer who does not read OpenCRQ.
 */
export interface CtemCaseExploitation {
  asset: string;
  risk_scenario_id: string | null;
  exposure: "internet_facing" | "internal" | "unknown";
  /** ATT&CK technique ids in attack order. */
  techniques: string[];
  reading: string;
  at: string | null;
}

/**
 * What a finding on the affected asset reaches along the OpenAEV attack paths
 * (CTEM-23-UI, #1289), read from OpenCRQ by the triage step. Every asset name is
 * "Restricted" and the simulation name and page null for a viewer who does not
 * read OpenCRQ. The raw simulation id is never served (UX round 1 on #1296, B2).
 */
export interface CtemCaseAttackPath {
  origin: { asset: string; exposure: "internet_facing" | "internal" | "unknown" };
  /** Nearest first: each hop proven by an OpenAEV execution, or inferred. */
  hops: {
    from: string;
    to: string;
    nature: "proven" | "inferred";
    /** The OpenAEV simulation that showed the hop, by name, and its page. */
    simulation_name: string | null;
    simulation_url: string | null;
    /** ISO date of that execution. */
    seen_at: string | null;
  }[];
  reached: { asset: string; depth: number | null; criticality: string | null }[];
  /** The primary (business) assets behind the reached ones: the blast radius. */
  primary: string[];
  at: string | null;
}

/**
 * The OpenCRQ treatment plan the remediation decision created for a case
 * (CTEM-18-UI, #1068). `restricted` true for a viewer who does not read
 * OpenCRQ: then only `at` is served.
 */
export interface CtemCaseTreatmentPlan {
  restricted: boolean;
  id?: string;
  name?: string | null;
  url?: string | null;
  at: string | null;
}

/**
 * The threat package the review step drafted for a case (CTEM-16-UI, #1115):
 * the OpenCTI Grouping and the security coverage requested on it. `restricted`
 * true for a viewer who does not read OpenCTI: then only the status fields
 * (`state`, `coverage_requested`, `coverage_drafted`, `draft_overdue`) and `at`
 * are served. `state` is `drafted` while the package sits in an OpenCTI draft
 * (its link is the draft page, nothing reached OpenAEV) and `validated` once it
 * is in the live graph; `coverage_requested` is true only for a validated
 * package (#1480). From the analyst review gate (#1573): `awaiting_review`
 * while it waits, `replan` once answered with free text or another option,
 * `rejected` once refused; `validated` needs the review approved.
 */
export type CtemThreatPackageState = "drafted" | "awaiting_review" | "replan" | "rejected" | "validated";

export interface CtemCaseThreatPackage {
  restricted: boolean;
  state?: CtemThreatPackageState;
  coverage_requested: boolean;
  /** The coverage is written in the draft: not sent to OpenAEV until the draft is validated. */
  coverage_drafted?: boolean;
  /** Still drafted past the case SLA. */
  draft_overdue?: boolean;
  grouping_id?: string;
  name?: string | null;
  url?: string | null;
  coverage_id?: string | null;
  at: string | null;
}

/** One version of an upgrade case's product on the assets (backend ctem_case_versions.running_versions, #1252). */
export interface CtemCaseRunningVersion {
  /** The version string, or "unknown" when a source reports the product without one. */
  version: string;
  asset_count: number;
  /** The sources that report it: an integration (intune, defender), a product (openaev), sbom, csv, manual, api. */
  sources: string[];
  /** Against the fixed version, when one is known. */
  status?: "below_fixed" | "fixed" | "not_comparable";
  /** The asset's os_family (#1572); null when the inventory does not know it. */
  platform?: string | null;
  /** The first asset ids of the row; more_asset_ids counts the rest (#1572). */
  asset_ids?: string[];
  more_asset_ids?: number;
  /** "fix not known for this platform" when no fixed version applies to the row's platform (#1572). */
  status_reason?: string;
  /** The fixed version of the row's platform, when the remediation names one (#1572). */
  fixed_version?: string;
}

/** The versions an upgrade case's product runs on the organization's assets, read from the asset inventory (#1252). */
export interface CtemCaseRunningVersions {
  product_label: string | null;
  fixed_version: string | null;
  versions: CtemCaseRunningVersion[];
  more_versions: number;
  version_count: number;
  /** Distinct assets running the product. */
  asset_count: number;
  sources: string[];
  /** With a fixed version: each asset counted once, by its worst version. */
  assets_below_fixed?: number;
  assets_on_fixed?: number;
  assets_not_comparable?: number;
  /** With no fixed version: the sources the fixed version was looked for in. */
  fixed_version_checked?: string[];
  /** {platform: version} when the remediation names platforms (#1572). */
  fixed_versions?: Record<string, string>;
  /** A generic fixed version over assets of several platforms: never read as fixed (#1572). */
  platform_unverified?: boolean;
  assets_fix_not_known_for_platform?: number;
  /** Distinct assets running more than one version: counted in each of their rows (#1572). */
  assets_multi_version?: number;
  platforms?: string[];
}

/** One asset a case's visible issues name (#1176 slice 3b), with the version it runs when the inventory knows it. */
export interface CtemCaseAffectedAsset {
  name: string;
  issue_count: number;
  open_issue_count: number;
  kev: boolean;
  max_cvss: number | null;
  /** Upgrade cases read from the inventory: whether an inventory asset of that name runs the product. */
  in_inventory?: boolean;
  asset_id?: string;
  versions?: string[];
  sources?: string[];
  status?: "below_fixed" | "fixed" | "not_comparable";
}

/** The assets of a case and where their versions come from: the asset inventory, or none yet. */
export interface CtemCaseAffectedAssets {
  assets: CtemCaseAffectedAsset[];
  asset_count: number;
  more_assets: number;
  version_source: "inventory" | "none";
}

export interface CtemCaseDetail extends CtemCaseSummary {
  keys: string[];
  issues: CtemCaseIssue[];
  history: CtemCaseEvent[];
  /** The phases of the audit trail in CTEM order (#1490); absent on an API older than #1494. */
  phases?: CtemCasePhase[];
  current_phase?: string | null;
  /** Absent on an API older than #936. */
  exploitation?: CtemCaseExploitation[];
  /** Null before triage read it; absent on an API older than #1289. */
  attack_path?: CtemCaseAttackPath | null;
  /** Null when no plan is attached; absent on an API older than #1068. */
  treatment_plan?: CtemCaseTreatmentPlan | null;
  /** Null when no package is attached; absent on an API older than #1115. */
  threat_package?: CtemCaseThreatPackage | null;
  ticketing?: CtemTicketingTarget;
  /** Upgrade cases only: null when no asset runs the product; absent on an API older than #1252. */
  running_versions?: CtemCaseRunningVersions | null;
  /** Absent on an API older than #1176 slice 3b. */
  affected_assets?: CtemCaseAffectedAssets;
  /** The fleet that runs the case (#1390); absent on an API older than #1396. */
  owner_fleet?: CtemCaseOwnerFleet | null;
  /** One resolved decision record per gate the case met (#1518); absent on an API older than #1524. */
  gates?: CtemCaseGateRecord[];
}

/** The status of a gate record (`ctem_case_gate_records.STATUSES`). */
export type CtemCaseGateStatus = "waiting" | "answered" | "passed_automatically" | "expired" | "cancelled";

/** Why a gate ran without a person (`ctem_case_gate_records.REASON_KINDS`). */
type CtemCaseGateReasonKind =
  | "runbook"
  | "within_threshold"
  | "above_threshold"
  | "loss_unknown"
  | "fleet_level"
  | "person";

/** A person, group or audience a decision was asked of; `name` null when the viewer may not see it. */
export interface CtemCaseGateRecipient {
  type: "user" | "group" | "company" | "admins" | string;
  id?: string;
  name?: string | null;
}

/**
 * The decision record of one gate (#1518, `ctem_case_gate_records.resolve`):
 * the decision row joined with the case's `gate` events, so an answered
 * decision reads as answered even before the case recorded it.
 */
export interface CtemCaseGateRecord {
  key: string;
  phase: string | null;
  status: CtemCaseGateStatus;
  asked_at: string | null;
  asked_of: CtemCaseGateRecipient | null;
  asked_by_agent: { id: string; name: string | null } | null;
  deadline: string | null;
  answered_at: string | null;
  answered_by: { id: string; name: string | null } | null;
  /** The channel the answer came from (`decision_router.SURFACES`). */
  surface: string | null;
  option: { key: string; label: string | null } | null;
  comment: string | null;
  reason: { kind: CtemCaseGateReasonKind; runbook?: string } | null;
  passed_at: string | null;
  wait_seconds: number | null;
  decision_id: string | null;
  work_item_id: string | null;
  /** False while the answer is on the decision but no `gate answered` event is on the case yet. */
  recorded_on_case: boolean;
  /** The first later event of the case: its kind, and `from` / `to` as the history stores them (a state's new value, a gate key and its outcome). */
  next: {
    at: string;
    kind: string | null;
    from?: string | null;
    to?: string | null;
    headline: string | null;
    actor_type: string | null;
    actor_name: string | null;
  } | null;
}

/** `owner_fleet` of a case: `active` false when no active fleet owns it (#1390). */
export interface CtemCaseOwnerFleet {
  slug: string;
  name: string;
  active: boolean;
}

/**
 * When the shared cases were last recomputed from the source products, with no
 * user action (dev-docs/ctem-cases.md s.4.3), and the products that did not
 * answer since. Absent on an API older than the automatic recompute.
 */
export interface CtemCasesFreshness {
  updated_at: string | null;
  missing: string[];
  missing_at: string | null;
}

export interface CtemCaseList {
  vertical: string;
  counts: Record<CtemCaseState, number>;
  cases: CtemCaseSummary[];
  details_unavailable: string[];
  /** Why each product of `details_unavailable` did not answer (#1852): rejected, too_large, timeout, not_connected, unavailable. */
  details_unavailable_reasons?: Record<string, string>;
  ticketing?: CtemTicketingTarget;
  freshness?: CtemCasesFreshness;
}

export interface CtemCaseUpdate {
  state?: CtemCaseState;
  owner_user_id?: string;
  clear_owner?: boolean;
  due_at?: string;
  reason?: string;
  evidence?: string;
  accepted_until?: string;
  note?: string;
}

/** The answer of a bulk change: the cases written, and each refused one with its rule. */
export interface CtemCaseBulkResult {
  updated: string[];
  failed: { id: string; reason: string }[];
}

export function isRestrictedIssue(issue: CtemCaseIssue): issue is CtemCaseRestrictedIssue {
  return issue.restricted === true;
}

/**
 * A read the server refused (an HTTP answer such as a 404) is one request per
 * view (#11999): no automatic retry. An outage (the backend unreachable) keeps
 * the app's outage policy, so the sections catch up once the backend answers.
 */
export function ctemCasesRetry(failureCount: number, error: unknown): boolean {
  return isNetworkError(error) ? retryQuery(failureCount, error) : false;
}

/**
 * The poll stops after a read the server refused (#11999): the section's Retry
 * reads again, never a request every 30 s. After an outage the poll goes on,
 * so the sections never freeze on data read before it.
 */
export function ctemCasesPollInterval(status: "pending" | "error" | "success", error?: unknown): number | false {
  return status === "error" && !isNetworkError(error) ? false : REFETCH_INTERVAL_SLOW;
}

/** Every case the caller sees (the page holds them all: the API caps a page at 200). */
export function useCtemCases(options?: {
  /**
   * A section of a larger page (the command center Cases row) passes `false`:
   * a failed read then stays in its own line with a Retry instead of handing
   * the whole page to the error boundary.
   */
  throwOnError?: boolean;
}) {
  return useQuery<CtemCaseList>({
    queryKey: ["ctem-cases", "list"],
    queryFn: () => api.get("/ctem-cases?limit=200"),
    retry: ctemCasesRetry,
    refetchInterval: (query) => ctemCasesPollInterval(query.state.status, query.state.error),
    placeholderData: keepPreviousData,
    ...(options?.throwOnError === false ? { throwOnError: false } : {}),
  });
}

/** The cases on one asset (an issue the caller sees names it): the Inventory asset page (#1178). */
export function useCtemCasesForAsset(asset: string | undefined) {
  const name = asset?.trim() ?? "";
  return useQuery<CtemCaseList>({
    queryKey: ["ctem-cases", "asset", name],
    queryFn: () => api.get(`/ctem-cases?limit=200&asset=${encodeURIComponent(name)}`),
    enabled: name !== "",
    retry: ctemCasesRetry,
    throwOnError: false,
  });
}

/** One loop duration over the cases the caller sees: the median in hours, `null` with no sample. */
interface CtemLoopDuration {
  median_hours: number | null;
  count: number;
}

/** How fast the loop runs (`GET /ctem-cases/measures`, CTEM-30), read from the case history. */
interface CtemLoopMeasures {
  signal_to_case: CtemLoopDuration;
  case_to_validation: CtemLoopDuration;
  case_to_decision: CtemLoopDuration;
  case_to_passing_retest: CtemLoopDuration;
  closed_on_proof: { share: number | null; proven: number; resolved: number };
  case_count: number;
}

/** One open case a phase card lists (`GET /ctem-cases/overview`, #1621). */
export interface CtemCasesOverviewCase {
  id: string;
  title: string;
  state: CtemCaseState;
  priority: CtemCasePriority;
  created_at: string | null;
  updated_at: string | null;
  state_since: string | null;
  waiting_on: { gate: string | null; by: string | null; since: string | null } | null;
  /** The primary asset the case touches among the issues the caller sees; null when none. */
  crown_jewel: string | null;
}

/** One CTEM phase of the overview: its open and waiting cases and the five that need attention first. */
interface CtemCasesOverviewPhase {
  open: number;
  waiting: number;
  counts: Record<CtemCaseState, number>;
  top: CtemCasesOverviewCase[];
}

/** A case waiting for a decision, with the gate it waits on. */
export interface CtemCasesOverviewWait {
  case_id: string;
  title: string;
  phase: CtemStageKey | null;
  gate: string | null;
  since: string | null;
}

/**
 * The cases in motion for the command center and the stage pages (#1621): the same
 * visibility as the list, so every count equals the list it links to.
 */
export interface CtemCasesOverview {
  vertical: string;
  counts: Record<CtemCaseState, number>;
  open: number;
  phases: Record<CtemStageKey, CtemCasesOverviewPhase>;
  no_phase_open: number;
  waiting: { count: number; oldest: CtemCasesOverviewWait | null; oldest_hours: number | null; cases: CtemCasesOverviewWait[] };
  funnel: { in_case: number; resolved_window_days: number; resolved_recent: number };
}

export function useCtemCasesOverview() {
  return useQuery<CtemCasesOverview>({
    queryKey: ["ctem-cases", "overview"],
    queryFn: () => api.get("/ctem-cases/overview"),
    retry: ctemCasesRetry,
    refetchInterval: (query) => ctemCasesPollInterval(query.state.status, query.state.error),
    throwOnError: false,
  });
}

export function useCtemLoopMeasures() {
  return useQuery<{ vertical: string; measures: CtemLoopMeasures }>({
    queryKey: ["ctem-cases", "measures"],
    queryFn: () => api.get("/ctem-cases/measures"),
    retry: ctemCasesRetry,
    refetchInterval: (query) => ctemCasesPollInterval(query.state.status, query.state.error),
    throwOnError: false,
  });
}

export function useCtemCase(id: string | undefined) {
  return useQuery<CtemCaseDetail>({
    queryKey: ["ctem-cases", "case", id],
    queryFn: () => api.get(`/ctem-cases/${id}`),
    enabled: Boolean(id),
    // An unknown case, or one the caller sees nothing of (#11613), is a 404
    // the page shows as NotFoundState, never the ErrorBoundary.
    ...notFoundIsOrdinary,
  });
}

/** One fleet run a case event started (`CaseRunEntry`, `GET /fleets/case-runs/{id}`, #915). */
export interface CtemCaseRun {
  id: string;
  agent_id: string;
  agent_name: string | null;
  status: string;
  created_at: string;
  completed_at: string | null;
  outcome: string;
}

/**
 * The fleet runs working on one case, oldest first, the ones the caller may
 * see. Only with agentic AI licensed (`enabled`): the route is gated like the
 * other fleet reads. Polled while a run is in flight so steps show live.
 */
export function useCtemCaseRuns(id: string | undefined, enabled: boolean) {
  return useQuery<{ case_id: string; runs: CtemCaseRun[] }>({
    queryKey: ["ctem-cases", "runs", id],
    queryFn: () => api.get(`/fleets/case-runs/${id}`),
    enabled: Boolean(id) && enabled,
    refetchInterval: (query) => (query.state.data?.runs.some((r) => r.outcome === "active") ? 10_000 : REFETCH_INTERVAL_SLOW),
    ...notFoundIsOrdinary,
  });
}

export function useUpdateCtemCase(id: string) {
  const qc = useQueryClient();
  return useMutation<CtemCaseDetail, Error, CtemCaseUpdate>({
    mutationFn: (body) => api.patch(`/ctem-cases/${id}`, body),
    onSuccess: (data) => {
      qc.setQueryData(["ctem-cases", "case", id], data);
      void qc.invalidateQueries({ queryKey: ["ctem-cases", "list"] });
    },
  });
}

/** One case's status changed from the list (the Kanban's drop, #1503): the case id goes with the change. */
export function useMoveCtemCase() {
  const qc = useQueryClient();
  return useMutation<CtemCaseDetail, Error, CtemCaseUpdate & { id: string }>({
    mutationFn: ({ id, ...body }) => api.patch(`/ctem-cases/${id}`, body),
    onSuccess: (data, { id }) => {
      qc.setQueryData(["ctem-cases", "case", id], data);
      void qc.invalidateQueries({ queryKey: ["ctem-cases", "list"] });
    },
  });
}

/** One change applied to several cases (the list's bulk actions, S5c of #11545). */
export function useBulkUpdateCtemCases() {
  const qc = useQueryClient();
  return useMutation<CtemCaseBulkResult, Error, CtemCaseUpdate & { case_ids: string[] }>({
    mutationFn: (body) => api.post("/ctem-cases/bulk", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["ctem-cases"] }),
  });
}

/** What one case delete removes (`CaseDeletion.summary` in backend/app/services/ctem_case_delete.py, #1723). */
interface CtemCaseDeletion {
  id: string;
  title: string;
  state: CtemCaseState;
  opened_by: CtemCaseOpenedBy;
  members: number;
  events: number;
  /** Open work items of the case, cancelled with a note. */
  work_items: string[];
  /** Armed timers of the case, cancelled. */
  timers: string[];
  /** Signals of the case, sent back to the scope gate. */
  signals: string[];
}

export interface CtemCaseDeleteResult {
  dry_run: boolean;
  count: number;
  cases: CtemCaseDeletion[];
  unknown: string[];
}

/**
 * Delete cases, administrators only (POST /ctem-cases/bulk-delete, #1723): `dry_run` answers
 * what would go and writes nothing; the real delete refreshes every case read.
 */
export function useDeleteCtemCases() {
  const qc = useQueryClient();
  return useMutation<CtemCaseDeleteResult, Error, { case_ids: string[]; dry_run: boolean; reason?: string }>({
    mutationFn: (body) => api.post("/ctem-cases/bulk-delete", body),
    onSuccess: (result) => {
      if (!result.dry_run) void qc.invalidateQueries({ queryKey: ["ctem-cases"] });
    },
  });
}

/** Merge cases into a surviving one, which keeps its state, owner and history. */
export function useMergeCtemCases() {
  const qc = useQueryClient();
  return useMutation<CtemCaseDetail, Error, { target_id: string; case_ids: string[]; reason?: string }>({
    mutationFn: (body) => api.post("/ctem-cases/merge", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["ctem-cases"] }),
  });
}

/** Split open issues out of their cases into a new manual case. */
export function useSplitCtemCase() {
  const qc = useQueryClient();
  return useMutation<CtemCaseDetail, Error, { title: string; issue_ids: string[]; reason?: string }>({
    mutationFn: (body) => api.post("/ctem-cases/split", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["ctem-cases"] }),
  });
}
