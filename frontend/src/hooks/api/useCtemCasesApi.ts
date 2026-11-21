// SPDX-License-Identifier: Apache-2.0

/**
 * CTEM Cases (dev-docs/ctem-cases.md): open exposures grouped by the fix they
 * share. Every read renders the case for the caller from their own source
 * read (#11613): a case they see nothing of is absent (404 on its page), a
 * member issue they cannot see is a bare `{ restricted: true }` row.
 */
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, notFoundIsOrdinary } from "@/lib/api";
import { REFETCH_INTERVAL_SLOW } from "@/lib/constants";
import { isNetworkError, retryQuery } from "@/lib/query-outage-policy";

export const CTEM_CASE_STATES = ["needs_attention", "in_progress", "mitigated_watching", "resolved", "accepted_risk"] as const;
export type CtemCaseState = (typeof CTEM_CASE_STATES)[number];
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
  asset_count: number;
  crown_jewel_count: number;
  kev_count: number;
  max_cvss: number | null;
  max_epss: number | null;
  first_seen_at: string | null;
  /** Products that did not answer this read: their issues show without details. */
  details_unavailable: string[];
  /** The loop (#857): how the case started, its severity and SLA, its proof. */
  trigger_path?: string;
  severity?: CtemCaseSeverity | null;
  sla_due_at?: string | null;
  sla_breached_at?: string | null;
  validations?: CtemCaseValidation[];
  resolution_kind?: string | null;
}

export type CtemCaseSeverity = "P1" | "P2" | "P3";

/** One validation reference written on a case (OpenAEV run, re-test, provider scan). */
interface CtemCaseValidation {
  provider: string;
  kind: string;
  ref: string;
  result: "passed" | "not_exposed" | "failed" | "pending";
  at: string;
  mitigation_only?: boolean;
  note?: string | null;
}

/** A member issue the caller sees. */
interface CtemCaseVisibleIssue {
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

export interface CtemCaseDetail extends CtemCaseSummary {
  keys: string[];
  issues: CtemCaseIssue[];
  history: CtemCaseEvent[];
  /** Absent on an API older than #936. */
  exploitation?: CtemCaseExploitation[];
}

export interface CtemCaseList {
  vertical: string;
  counts: Record<CtemCaseState, number>;
  cases: CtemCaseSummary[];
  details_unavailable: string[];
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

export interface CtemRecomputeResult {
  applied: boolean;
  missing?: string[];
  [key: string]: unknown;
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

/** One change applied to several cases (the list's bulk actions, S5c of #11545). */
export function useBulkUpdateCtemCases() {
  const qc = useQueryClient();
  return useMutation<CtemCaseBulkResult, Error, CtemCaseUpdate & { case_ids: string[] }>({
    mutationFn: (body) => api.post("/ctem-cases/bulk", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["ctem-cases"] }),
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

export function useRecomputeCtemCases() {
  const qc = useQueryClient();
  return useMutation<CtemRecomputeResult, Error, void>({
    mutationFn: () => api.post("/ctem-cases/recompute"),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["ctem-cases"] }),
  });
}
