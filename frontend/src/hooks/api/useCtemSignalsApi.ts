// SPDX-License-Identifier: Apache-2.0

/**
 * CTEM signals (dev-docs/ctem-signals.md): the changes in exposure that start
 * the loop. Read as the caller may see them; the activity stream frame
 * `ctem_signal_update` (dev-docs/ctem-loop-visibility.md section 5) invalidates
 * the `["ctem-signals"]` keys, so a recorded signal shows without a reload.
 */
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, notFoundIsOrdinary } from "@/lib/api";
import { ctemCasesPollInterval, ctemCasesRetry } from "./useCtemCasesApi";

export const CTEM_SIGNAL_STATES = ["new", "confirmed", "to_validate", "discarded", "watchlist", "case_opened"] as const;
export type CtemSignalState = (typeof CTEM_SIGNAL_STATES)[number];
type CtemLens = "inside" | "outside" | "world";

/**
 * A decision record as the reconcile and scope gate writes it (version 1,
 * dev-docs/ctem-scope-gate.md section 6): `reason_code` and `reason_params`
 * are what the UI translates; `reason` is the English line agents read.
 */
export interface CtemDecisionRecord {
  version?: number;
  outcome?: string;
  state?: string;
  reason?: string;
  reason_code?: string;
  reason_params?: Record<string, string | number>;
  inputs?: { lens?: string; source_provider?: string; cves?: string[] } & Record<string, unknown>;
  reconcile?: {
    assets?: string[];
    primary_assets_named?: string[];
    tech_stack?: string[];
    lenses?: string[];
    confidence?: string;
  } & Record<string, unknown>;
  scope?: {
    criticality?: string | null;
    threshold?: string;
    /**
     * What gave the criticality: an asset, a primary asset, an owned domain (CTEM-29), a risk scenario.
     * A primary asset source carries its first chains (`chain`), how many there are (`chain_total`)
     * and how many inventory assets reach it (`assets_total`, #1979); older records carry none.
     */
    sources?: { from: string; name: string; criticality?: string; chain?: unknown; chain_total?: number; assets_total?: number }[];
    risk_scenarios?: { id: string; name: string }[];
  } | null;
  policy_checks?: { rule: string; value?: unknown; threshold?: unknown; passed: boolean }[];
  severity?: string | null;
  decided_at?: string;
}

/** One signal as the API answers it (`to_dict` in backend/app/services/ctem_signals.py). */
export interface CtemSignal {
  id: string;
  trigger_path: string;
  trigger_path_label: string;
  lens: CtemLens;
  source: { provider: string; external_id: string };
  subject: { kind: string; ref: string; label: string };
  observed_at: string | null;
  first_seen_at: string | null;
  last_seen_at: string | null;
  seen_count: number;
  evidence: { link?: string } & Record<string, unknown>;
  state: CtemSignalState;
  decision: CtemDecisionRecord | null;
  case_id: string | null;
  recorded_via: string;
}

export interface CtemSignalList {
  total: number;
  counts: Partial<Record<CtemSignalState, number>>;
  signals: CtemSignal[];
}

/** The newest signals the caller sees, for the Live activity card. */
export function useRecentCtemSignals(limit = 6) {
  return useQuery<CtemSignalList>({
    queryKey: ["ctem-signals", "recent", limit],
    queryFn: () => api.get(`/ctem-signals?limit=${limit}`),
    retry: ctemCasesRetry,
    // The stream frame is the accelerator; the slow poll stays the floor and
    // stops after a refused read (#11999), as the cases sections do.
    refetchInterval: (query) => ctemCasesPollInterval(query.state.status, query.state.error),
    throwOnError: false,
  });
}

/**
 * The signals in one state (or every state), a page of 50 at a time through
 * `offset`, for the signal list of the loop trace (CTEM-01-UI; #877 UX round 1
 * B2: every signal reachable). Same stream key family as the recent read, so a
 * recorded signal joins the open list without a reload. `triggerPath` narrows
 * it to one trigger path (#1189). The Signals page (#1263) reads every page of
 * one path at 200 a time, so its table searches, sorts and exports them all.
 */
export function useCtemSignalList(state: CtemSignalState | null, limit = 50, enabled = true, triggerPath: string | null = null) {
  const query = `${state ? `&state=${state}` : ""}${triggerPath ? `&trigger_path=${encodeURIComponent(triggerPath)}` : ""}`;
  return useInfiniteQuery({
    queryKey: ["ctem-signals", "list", state ?? "all", limit, triggerPath ?? "all"],
    queryFn: ({ pageParam }): Promise<CtemSignalList> => api.get(`/ctem-signals?limit=${limit}&offset=${pageParam}${query}`),
    initialPageParam: 0,
    getNextPageParam: (last: CtemSignalList, pages: CtemSignalList[]) => {
      const loaded = pages.reduce((sum, page) => sum + page.signals.length, 0);
      return last.signals.length > 0 && loaded < last.total ? loaded : undefined;
    },
    enabled,
    retry: ctemCasesRetry,
    refetchInterval: (q) => ctemCasesPollInterval(q.state.status, q.state.error),
    throwOnError: false,
  });
}

/**
 * The signals that opened or joined one case, for its "Opened by" card
 * (CTEM-03-UI, #892). Read as the caller may see them; the `ctem_case_update`
 * frame of the case refreshes it.
 */
export function useCtemCaseSignals(caseId: string | undefined) {
  return useQuery<CtemSignalList>({
    queryKey: ["ctem-signals", "case", caseId],
    queryFn: () => api.get(`/ctem-signals?case_id=${caseId}&limit=50`),
    enabled: Boolean(caseId),
    retry: ctemCasesRetry,
    throwOnError: false,
  });
}

/**
 * Connected (a bound source), off (a pass whose provider is connected but which does not run, #1344),
 * not connected (a source exists, no integration of it is connected) or agents and API only (no source).
 */
export type CtemSignalPathStatus = "connected" | "off" | "not_connected" | "agents_api";

/** One way a trigger path is fired (#1189): an integration listener, a scheduled pass or the OpenCTI PIR playbook. */
export interface CtemSignalPathSource {
  provider: string;
  kind: "listener" | "pass" | "pir_playbook";
  /** The English line naming the event that fires the path (an i18n key). */
  event: string;
  /** Bound for the caller: a connected integration they can see (and, for a pass, the pass runs on it). */
  bound: boolean;
  /** A pass whose provider is connected but which does not run: turned off, or no integration it can pick (#1344). */
  off?: boolean;
  /** Taken off this path by an administrator (#1445): never bound here, it still feeds its other paths. */
  excluded?: boolean;
  /** Bound before the exclusion: the source is connected and would feed the path if not excluded (#1445). */
  available?: boolean;
  /** The other trigger paths this source feeds (a pass is organization-wide: one switch feeds them all). */
  also_feeds?: string[];
}

/** One trigger path with the signals the caller sees on it (`GET /ctem-signals/paths`, CTEM-30). */
export interface CtemSignalPath {
  id: string;
  label: string;
  default_lens: CtemLens;
  /** An integration listener records signals on this path. */
  listener: boolean;
  counts: Partial<Record<CtemSignalState, number>>;
  total: number;
  /** A listener feeds it or it holds a signal; otherwise it reads "Not configured", never 0. */
  configured: boolean;
  /** What fires it, in one sentence (an i18n key, #1189). */
  description?: string;
  sources?: CtemSignalPathSource[];
  status?: CtemSignalPathStatus;
  /** Signals the caller sees, last seen in the last 30 days. */
  count_30d?: number;
  last_signal_at?: string | null;
}

/** The 12 trigger paths with their counts, for the Discovery stage's signals card. */
export function useCtemSignalPaths() {
  return useQuery<{ paths: CtemSignalPath[] }>({
    queryKey: ["ctem-signals", "paths"],
    queryFn: () => api.get("/ctem-signals/paths"),
    retry: ctemCasesRetry,
    refetchInterval: (query) => ctemCasesPollInterval(query.state.status, query.state.error),
    throwOnError: false,
  });
}

/**
 * "Propose to raise the scope" on a watchlisted signal (CTEM-30): stores an
 * organization context proposal an administrator accepts in Settings >
 * Organization. Nothing changes in the profile until then; never a gate.
 */
export function useProposeSignalScope() {
  const qc = useQueryClient();
  return useMutation<{ proposal_id: string; status: string; field: string; value: string }, Error, string>({
    mutationFn: (id) => api.post(`/ctem-signals/${encodeURIComponent(id)}/propose-scope`, {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["ctem-signals"] }),
    meta: { suppressGlobalErrorToast: true },
  });
}

/** What a person may do to signals from the Signals page (#1505; `TRIAGE_ACTIONS` in ctem_scope_gate_store.py). */
export type CtemSignalTriageAction = "dismiss" | "restore" | "recheck";

/** The body of `POST /ctem-signals/triage`: the selected ids, or every signal one state and path match. */
export interface CtemSignalTriage {
  action: CtemSignalTriageAction;
  ids?: string[];
  filter?: { state?: CtemSignalState; trigger_path?: string };
}

export interface CtemSignalTriageResult {
  matched: number;
  counts: Partial<Record<CtemSignalState | "changed" | "skipped", number>>;
}

/**
 * Dismiss, restore or re-check signals in one call (#1505): the selected ones,
 * or every signal the state filter matches. The lists and counters read again.
 */
export function useTriageCtemSignals() {
  const qc = useQueryClient();
  return useMutation<CtemSignalTriageResult, Error, CtemSignalTriage>({
    mutationFn: (body) => api.post("/ctem-signals/triage", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["ctem-signals"] }),
    meta: { suppressGlobalErrorToast: true },
  });
}

/** The body of `POST /ctem-signals/add-to-inventory`: the selected ids, or every signal one state and path match. */
export type CtemSignalInventoryAdd = Omit<CtemSignalTriage, "action">;

export interface CtemSignalInventoryAddResult {
  matched: number;
  added: { assets: string[]; products: string[]; domains: string[] };
  version: number | null;
  counts: Partial<Record<CtemSignalState | "changed" | "skipped", number>>;
}

/**
 * Add the subjects of unmatched signals to the inventory (#1505, administrators):
 * one profile version, then the gate runs on them again. The lists, counters and
 * the organization context read again.
 */
export function useAddCtemSignalsToInventory() {
  const qc = useQueryClient();
  return useMutation<CtemSignalInventoryAddResult, Error, CtemSignalInventoryAdd>({
    mutationFn: (body) => api.post("/ctem-signals/add-to-inventory", body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["ctem-signals"] });
      void qc.invalidateQueries({ queryKey: ["organization-context"] });
    },
    meta: { suppressGlobalErrorToast: true },
  });
}

/** The body of `POST /ctem-signals` (`SignalCreate` in backend/app/api/v1/ctem_signals.py). */
export interface CtemSignalCreate {
  trigger_path: string;
  lens?: CtemLens;
  source: { provider: string; external_id?: string };
  subject: { kind: string; ref: string; label?: string };
  observed_at?: string;
  evidence?: Record<string, unknown>;
  dedupe_key?: string;
}

/**
 * Record one signal through the real intake (`POST /ctem-signals`): the
 * reconcile and scope gate run on it and the stream frame shows it moving.
 * Used by "Send a test signal" at the end of the guided setup (#1087).
 */
export function useRecordCtemSignal() {
  const qc = useQueryClient();
  return useMutation<{ created: boolean; signal: CtemSignal }, Error, CtemSignalCreate>({
    mutationFn: (body) => api.post("/ctem-signals", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["ctem-signals"] }),
  });
}

/** One inventory asset through which a signal reaches a primary asset (#1979). */
export interface CtemSupportingAsset {
  asset: string;
  software: string;
  through: string;
  through_kind: "asset" | "asset_group" | string;
  source: string;
  roles: string[];
}

export interface CtemSupportingAssetPage {
  primary_asset: string;
  total: number;
  offset: number;
  limit: number;
  /** The order the server applied before cutting the page. */
  sort?: "asset" | "through";
  direction?: "asc" | "desc";
  /** The inventory read was cut at its bounds: the total is a floor. */
  truncated: boolean;
  assets: CtemSupportingAsset[];
}

/** The columns the supporting-assets endpoint orders by (UX r1 B3 on #1995). */
export type CtemSupportingAssetSort = "asset" | "through";

/** One page of the supporting assets of one primary asset a signal reaches, searched by name and sorted on the server (#1979). */
export function useCtemSupportingAssets(
  signalId: string,
  primaryAsset: string,
  page: { search: string; offset: number; limit: number; sort?: CtemSupportingAssetSort; direction?: "asc" | "desc" },
  enabled = true,
) {
  const params = new URLSearchParams({ offset: String(page.offset), limit: String(page.limit) });
  if (page.search) params.set("q", page.search);
  const sort = page.sort ?? "asset";
  const direction = page.direction ?? "asc";
  if (sort !== "asset" || direction !== "asc") {
    params.set("sort", sort);
    params.set("direction", direction);
  }
  return useQuery<CtemSupportingAssetPage>({
    queryKey: ["ctem-signals", "supporting-assets", signalId, primaryAsset, page.search, page.offset, page.limit, sort, direction],
    queryFn: () =>
      api.get(`/ctem-signals/${encodeURIComponent(signalId)}/primary-assets/${encodeURIComponent(primaryAsset)}/supporting-assets?${params}`),
    enabled: enabled && Boolean(signalId && primaryAsset),
    placeholderData: (previous) => previous,
    retry: ctemCasesRetry,
    throwOnError: false,
  });
}

/** One signal; one the caller cannot read is a 404 the loop trace shows as "not available". */
export function useCtemSignal(id: string | undefined) {
  return useQuery<CtemSignal>({
    queryKey: ["ctem-signals", "signal", id],
    queryFn: () => api.get(`/ctem-signals/${id}`),
    enabled: Boolean(id),
    ...notFoundIsOrdinary,
  });
}
