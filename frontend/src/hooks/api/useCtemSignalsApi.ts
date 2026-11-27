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
    /** UX r2 B7 on #1995: the profile asset list was cut at its cap, so `assets` is a floor, not the total. */
    assets_capped?: boolean;
    primary_assets_named?: string[];
    products?: string[];
    /** #1720 / #2523: every software reference the gate tried against the inventory and shipped products. */
    software_refs?: string[];
    /**
     * #2171 / #2523: how the OpenCTI software read went ("read", "no reader", "no OpenCTI access",
     * "OpenCTI did not answer"; #2542: "not needed", "not applicable", "not read"; null on older records).
     */
    cve_lookup?: string | null;
    threat_lookup?: string | null;
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
  /** #2542: an OpenCTI software read the subject needed did not answer; the gate retries it. */
  incomplete?: boolean;
  /** #2542: how many decisions were taken while that read kept failing. */
  lookup_attempts?: number;
  /** #2542: "scheduled" while the retry pass takes it again, "exhausted" once it stopped. */
  lookup_retry?: "scheduled" | "exhausted";
}

/** One signal as the API answers it (`to_dict` in backend/app/services/ctem_signals.py). */
export interface CtemSignal {
  id: string;
  trigger_path: string;
  trigger_path_label: string;
  lens: CtemLens;
  /** external_id is the dedupe key (path 11: "<OpenCTI id>:<organization id>"); entity_id, sent only when it differs, is the object's id in its module, the one a pivot opens. */
  source: { provider: string; external_id: string; entity_id?: string | null };
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
 * Connected (a bound source), error (a source XTM One provisions is in error, #2434), not set up (a source
 * XTM One provisions, its provider connected, switched on nowhere), off (a pass whose provider is connected
 * but which does not run, #1344), not connected (no integration of a source's provider is connected) or
 * agents and API only (no source).
 */
export type CtemSignalPathStatus = "enabled" | "partly_enabled" | "error" | "not_set_up" | "off" | "not_connected" | "agents_api";

/** One way a trigger path is fired (#1189): an integration listener, a scheduled pass or the OpenCTI PIR playbook. */
export interface CtemSignalPathSource {
  provider: string;
  kind: "listener" | "pass" | "pir_playbook" | "webhook" | "cases_reader";
  /** The English line naming the event that fires the path (an i18n key). */
  event: string;
  /** Bound for the caller: a connected integration they can see (and, for a pass, the pass runs on it). */
  bound: boolean;
  /** A pass whose provider is connected but which does not run: turned off, or no integration it can pick (#1344). */
  off?: boolean;
  /** A source XTM One provisions, its provider connected, switched on on none of its integrations (#2434). */
  not_set_up?: boolean;
  /** The integrations on which this source XTM One provisions is in error (#2434). */
  error_count?: number;
  /** Taken off this path by an administrator (#1445): never bound here, it still feeds its other paths. */
  excluded?: boolean;
  /** Bound before the exclusion: the source is connected and would feed the path if not excluded (#1445). */
  available?: boolean;
  /** The other trigger paths this source feeds (a pass is organization-wide: one switch feeds them all). */
  also_feeds?: string[];
  /** The key the profile names it by: `provider:kind`, `provider:kind:name` when named (#2436). */
  key?: string;
  /** Tells apart two sources of one provider and kind on one path (#2436). */
  name?: string;
  /** False: the source records nothing until an administrator switches it on for the path (#2436). */
  default_on?: boolean;
  /** Signals this source recorded on the path in the last 30 days, and the last one (#2436). */
  count_30d?: number;
  last_signal_at?: string | null;
  /** A generic signed webhook source (#2506): its id, and why its last delivery recorded nothing. */
  webhook_id?: string;
  last_error?: string | null;
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
  /** No signal in 30 days: why its pass raised nothing, when the attack-surface pass says it (#2634). */
  quiet_reason?: CtemSignalQuietReason;
  /** Its sources on, and its sources that are or could be on here (#2506). */
  sources_enabled?: number;
  sources_available?: number;
}

/** Why an outside path raised nothing (#2634): a code the screen words (quiet-reason.ts) and its data. */
export interface CtemSignalQuietReason {
  code: string;
  /** The last pass, ISO 8601. */
  at?: string | null;
  count?: number;
  scopes?: string[];
  /** Scopes the provider answered nothing for, on a pass that still read hosts. */
  empty_scopes?: string[];
}

/** A generic signed webhook source of one trigger path (#2506, dev-docs/ctem-signals.md 4ter). */
export interface CtemWebhookSource {
  id: string;
  trigger_path: string;
  name: string;
  enabled: boolean;
  subject_kind: string;
  mapping: { id: string; label?: string; severity?: string };
  receiver_path: string;
  receiver_url: string;
  signature_header: string;
  secret_rotated_at: string | null;
  last_delivery_at: string | null;
  last_error: string | null;
  created_at: string | null;
  /** In the create and rotate answers only: shown once, never read back. */
  secret?: string;
}

export interface CtemWebhookSourceCreate {
  trigger_path: string;
  name: string;
  subject_kind?: string;
  mapping: { id: string; label?: string | null; severity?: string | null };
}

/** The webhook sources, administrators only. */
export function useCtemWebhookSources(enabled = true) {
  return useQuery<{ sources: CtemWebhookSource[] }>({
    queryKey: ["ctem-signals", "webhook-sources"],
    queryFn: () => api.get("/ctem-signals/webhook-sources"),
    enabled,
    retry: false,
    throwOnError: false,
  });
}

/** Create a webhook source, off; the answer carries its secret once. */
export function useCreateCtemWebhookSource() {
  const qc = useQueryClient();
  return useMutation<CtemWebhookSource, Error, CtemWebhookSourceCreate>({
    mutationFn: (body) => api.post("/ctem-signals/webhook-sources", body),
    onSettled: () => qc.invalidateQueries({ queryKey: ["ctem-signals"] }),
    meta: { suppressGlobalErrorToast: true },
  });
}

/** Switch a webhook source on or off, or change its name or mapping. */
export function useUpdateCtemWebhookSource() {
  const qc = useQueryClient();
  return useMutation<CtemWebhookSource, Error, { id: string } & Partial<Pick<CtemWebhookSourceCreate, "name" | "mapping">> & { enabled?: boolean }>({
    mutationFn: ({ id, ...body }) => api.patch(`/ctem-signals/webhook-sources/${encodeURIComponent(id)}`, body),
    onSettled: () => qc.invalidateQueries({ queryKey: ["ctem-signals"] }),
    meta: { suppressGlobalErrorToast: true },
  });
}

/** A new secret for a webhook source; the old one is refused from now on. */
export function useRotateCtemWebhookSource() {
  const qc = useQueryClient();
  return useMutation<CtemWebhookSource, Error, string>({
    mutationFn: (id) => api.post(`/ctem-signals/webhook-sources/${encodeURIComponent(id)}/rotate`, {}),
    onSettled: () => qc.invalidateQueries({ queryKey: ["ctem-signals"] }),
    meta: { suppressGlobalErrorToast: true },
  });
}

/** What a webhook source's mapping reads in a sample: nothing is recorded. */
export function useTestCtemWebhookSource() {
  return useMutation<
    { ok: boolean; reason?: string; id?: string; label?: string | null; severity?: string | null },
    Error,
    { id: string; sample: unknown }
  >({
    mutationFn: ({ id, sample }) => api.post(`/ctem-signals/webhook-sources/${encodeURIComponent(id)}/test`, { sample }),
    meta: { suppressGlobalErrorToast: true },
  });
}

/** A source XTM One provisions in its platform: off, connected, or in error with its reason (#2434). */
export type CtemPlatformSourceStatus = "off" | "connected" | "error";

/** Why a platform source cannot run, as data: the UI renders the code, never the English `reason` (#2434). */
export type CtemPlatformSourceReasonCode =
  | "no_run_as"
  | "run_as_inactive"
  | "run_as_no_integration"
  | "no_webhook_secret"
  | "no_token"
  | "integration_not_found"
  | "organization_context_missing"
  | "objects_not_removed"
  | "not_provisioned_yet"
  | "provider_error";

/** One platform signal source on one integration (`GET /ctem-signals/sources`, dev-docs/ctem-signals.md 4bis). */
export interface CtemPlatformSource {
  /** `null` until it is switched on once. */
  id: string | null;
  provider: string;
  kind: "listener" | "pir_playbook";
  integration_id: string;
  integration_name: string;
  integration_connected: boolean;
  enabled: boolean;
  run_as_user_id: string | null;
  status: CtemPlatformSourceStatus;
  reason: string | null;
  reason_code: CtemPlatformSourceReasonCode | null;
  reason_params: Record<string, string>;
  checked_at: string | null;
  /** The objects it holds in its platform (#2583 point 2), for the Open in links. */
  objects?: CtemPlatformSourceObject[];
  /** The platform's web UI host, `null` when the integration stores none. */
  integration_url?: string | null;
  /** The trigger paths it feeds (#2583 point 6): turning it off stops them all. */
  feeds?: { path: string; label: string }[];
  /** The objects the fleets provisioned on the same integration, listed apart with their owner (#2640); on the listener row only. */
  fleet_objects?: CtemPlatformSourceObject[];
  /** OpenCTI only: the legacy Threat response objects XTM One still holds on this integration (#2627). */
  legacy_pending?: number;
}

/** One object a platform source provisioned: a notifier, a trigger or a playbook, by its id there. */
export interface CtemPlatformSourceObject {
  kind: "notifier" | "trigger" | "playbook";
  id: string;
  /** Its name in its platform ("XTM One - Signals - ... - Trigger"); empty when not known. */
  name?: string;
  /** Who owns it: "Signals" for a source's own objects, else the fleet or desk that provisioned it (#2640). */
  owner?: string;
  /** The module event a per-event trigger listens to. */
  event?: string;
  /** The other owners that keep their own trigger on the same module event (#2640). */
  shared_with?: string[];
  /** The trigger path a per-path object serves (the PIR playbooks, #2627), and its label. */
  path?: string;
  path_label?: string;
}

/** The platform signal sources, administrators only (the API answers 403 to anyone else). */
export function useCtemPlatformSources(enabled = true) {
  return useQuery<{ sources: CtemPlatformSource[] }>({
    queryKey: ["ctem-signals", "platform-sources"],
    queryFn: () => api.get("/ctem-signals/sources"),
    enabled,
    retry: false,
    throwOnError: false,
  });
}

/** Switch a source on as a run-as user: provisioned at once, or refused with `detail: {code, params, message}`. */
export function useEnableCtemPlatformSource() {
  const qc = useQueryClient();
  return useMutation<CtemPlatformSource, Error, { provider: string; kind: string; integration_id: string; run_as_user_id: string }>({
    mutationFn: (body) => api.post("/ctem-signals/sources/enable", body),
    onSettled: () => qc.invalidateQueries({ queryKey: ["ctem-signals"] }),
    meta: { suppressGlobalErrorToast: true },
  });
}

/** One integration of the legacy OpenCTI objects report (`POST /ctem-signals/sources/legacy-opencti-objects`). */
export interface CtemLegacyOpenctiReport {
  integration_id: string;
  objects?: { kind: string; id: string; name: string }[];
  removed?: number;
  errors?: string[];
  error?: string;
}

/** List (dry run) or remove the legacy Threat response objects left in OpenCTI (#2583 point 4). Administrators only. */
export function useRemoveLegacyOpenctiObjects() {
  const qc = useQueryClient();
  return useMutation<{ dry_run: boolean; integrations: CtemLegacyOpenctiReport[] }, Error, { dryRun: boolean }>({
    mutationFn: ({ dryRun }) => api.post(`/ctem-signals/sources/legacy-opencti-objects?dry_run=${dryRun}`, {}),
    onSettled: (_data, _error, { dryRun }) => {
      if (!dryRun) void qc.invalidateQueries({ queryKey: ["ctem-signals"] });
    },
    meta: { suppressGlobalErrorToast: true },
  });
}

/** Switch a source off: what it provisioned in the source platform is removed. */
export function useDisableCtemPlatformSource() {
  const qc = useQueryClient();
  return useMutation<CtemPlatformSource, Error, string>({
    mutationFn: (id) => api.post(`/ctem-signals/sources/${encodeURIComponent(id)}/disable`, {}),
    onSettled: () => qc.invalidateQueries({ queryKey: ["ctem-signals"] }),
    meta: { suppressGlobalErrorToast: true },
  });
}

/** One step of a purge job (#2528): pause, purge, resync, resume. */
export type CtemSignalPurgeStepState = "pending" | "running" | "done" | "failed" | "skipped";

export interface CtemSignalPurgeJob {
  id: string;
  state: "running" | "done" | "failed";
  resync: boolean;
  step: "pause" | "purge" | "resync" | "resume";
  steps: Record<"pause" | "purge" | "resync" | "resume", { state: CtemSignalPurgeStepState; counts: Record<string, unknown> }>;
  error: string | null;
  /** The job stopped before its end, its process restarted (#2539): read back failed, a new purge can start. */
  interrupted?: boolean;
  /** With interrupted: signal intake is still paused (its pause key not yet expired); false: intake runs again. */
  intake_paused?: boolean;
  started_at: string;
  finished_at: string | null;
}

/** One resync route of the purge (#2578): a world type, an inside source or a pass, with its default window. */
export interface CtemSignalResyncRoute {
  key: string;
  group: "world" | "inside" | "pass";
  /** English, translated by the dialog. */
  label: string;
  /** Days; null: no age limit. */
  default: number | null;
  /** False for a pass (it reads the current state) and for a route the resync does not read. */
  windowed: boolean;
  /** Why the resync does not read this route (English, translated by the dialog); null otherwise. */
  note?: string | null;
}

/** The resync's outcome on one route (#2578), from `steps.resync.counts.routes`. */
export interface CtemSignalResyncRouteResult {
  window_days: number | null;
  read?: number;
  recorded?: number;
  errors?: string[];
  skipped?: string;
  error?: string;
  note?: string;
  capped?: boolean;
}

export interface CtemSignalPurgeInfo {
  counts: { signals: number; cases: number };
  confirm_word: string;
  resync_routes: CtemSignalResyncRoute[];
  resync_max_days: number;
  job: CtemSignalPurgeJob | null;
}

/** What a purge would delete now and the running or last job (administrators only); polled while a job runs. */
export function useCtemSignalPurge(enabled: boolean) {
  return useQuery<CtemSignalPurgeInfo>({
    queryKey: ["ctem-signals", "purge"],
    queryFn: () => api.get("/ctem-signals/purge"),
    enabled,
    refetchInterval: (query) => (query.state.data?.job?.state === "running" ? 2000 : false),
  });
}

/** Start the purge of every signal (#2528): 422 without the word, 409 while a job runs. */
export function useStartCtemSignalPurge() {
  const qc = useQueryClient();
  return useMutation<
    { job: CtemSignalPurgeJob },
    Error,
    { confirm: string; resync: boolean; resync_windows?: Record<string, number | null> }
  >({
    mutationFn: (body) => api.post("/ctem-signals/purge", body),
    onSettled: () => qc.invalidateQueries({ queryKey: ["ctem-signals", "purge"] }),
    meta: { suppressGlobalErrorToast: true },
  });
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
