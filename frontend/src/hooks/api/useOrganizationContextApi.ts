// SPDX-License-Identifier: Apache-2.0

/** The organization profile (#8987): the structured context every agent and fleet run reads. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { syncFlowNodeCatalog } from "@/lib/flow-node-catalog";
import type {
  AssetIdentitySettings,
  AttackSurfacePass,
  CaseAttentionProfile,
  PrioritizationPolicy,
  SignalSourcesSettings,
  TicketingSync,
} from "@/lib/organization-profile";

interface CrownJewel {
  name: string;
  business_service: string;
  owner: string;
  criticality: "low" | "medium" | "high" | "critical";
  opencrq_asset_id: string;
  /** The assets and asset groups (by name) it runs on (#11543); absent when none is named. */
  asset_names?: string[];
  /** Where the row came from (#1134): manual, csv, api, agent:<id> or a platform. */
  source?: string;
  external_id?: string;
  sync_id?: string;
}

interface TechStackEntry {
  product: string;
  vendor: string;
  cpe: string;
  /** The version the organization runs, optional (CTEM-11): absent on a profile saved before it. */
  version?: string;
  source?: string;
  external_id?: string;
  sync_id?: string;
}

export interface OrganizationProfile {
  name: string;
  sector: string;
  sub_sector: string;
  regions: string[];
  size: "" | "1-50" | "51-250" | "251-1000" | "1001-5000" | "5001-20000" | "20000+";
  crown_jewels: CrownJewel[];
  tech_stack: TechStackEntry[];
  // No `ai_systems` list since #1501: the AI systems are the assets of category `ai_target`.
  /** What the organization ships, with its source repositories (#1432); absent when none is declared. */
  shipped_products?: { product: string; vendor: string; cpe: string; repositories: string[] }[];
  pir_ids: string[];
  /** The last known OpenCTI name of each saved PIR (#9615). */
  pir_names?: Record<string, string>;
  risk_appetite: { annual_loss_threshold: number | null; approval_threshold: number | null };
  loaded_hourly_rate: number | null;
  currency: string;
  /** The ticketing target; `sync` is how CTEM cases sync with it (#917, absent on an older profile: the defaults). */
  ticketing: { integration: string; project: string; sync?: TicketingSync };
  /** The Exposure Posture score aimed for, 0-100 (#11547 S2e); absent or null: no target. */
  posture_target?: number | null;
  /** The CTEM prioritization rules (#11543); the Governance & Risk page reads its accepted risks. */
  prioritization_policy?: PrioritizationPolicy;
  /** The owned domains, address ranges and outside lens pass (CTEM-29, CTEM-26), edited on the Exposure tab. */
  owned_domains?: string[];
  address_ranges?: string[];
  attack_surface?: AttackSurfacePass;
  /** The points of the Cases attention score (#1176); absent on an older backend: the defaults. */
  case_attention?: CaseAttentionProfile;
  /** Parts edited elsewhere (CTEM-28, #1216), carried whole by every profile save. */
  evidence_health?: Record<string, unknown>;
  cases_recompute?: Record<string, unknown>;
  /** The sources taken off one CTEM trigger path (#1445), edited in CTEM > Signals, Sources. */
  signal_sources?: SignalSourcesSettings;
  /** Asset matching and field precedence (#1178), edited in CTEM > Inventory, Sources. */
  asset_identity?: AssetIdentitySettings;
}

export interface OrganizationContextResponse {
  version: number | null;
  source: string | null;
  created_at: string | null;
  author_user_id: string | null;
  profile: OrganizationProfile;
  missing_fields: string[];
}

interface OpenCrqSyncPlan {
  linked: { crown_jewel: string; opencrq_asset_id: string }[];
  pushed_to_opencrq: { opencrq_asset_id: string; name: string; criticality: string }[];
  pulled_from_opencrq: { opencrq_asset_id: string; name: string }[];
  ambiguous: string[];
  not_in_opencrq: { crown_jewel: string; opencrq_asset_id: string }[];
  /** Whether the profile and OpenCRQ agree (#1134 S2): the only answer that may say so; absent from an older backend. */
  in_sync?: boolean;
  /** The lowest OpenCRQ criticality the plan pulled (#1558). */
  min_criticality?: OpenCrqLevel;
  /** The supporting assets read, and their count per OpenCRQ criticality ("unknown" first) (#1558). */
  assets_read?: number;
  by_criticality?: Partial<Record<"unknown" | OpenCrqLevel, number>>;
}

/** OpenCRQ's criticality levels, lowest first: the thresholds the crown-jewel check accepts (#1558). */
export type OpenCrqLevel = "low" | "medium" | "high" | "very_high";

export interface OpenCrqSyncResponse {
  applied: boolean;
  plan: OpenCrqSyncPlan;
  errors: string[];
  version: number | null;
}

const KEY = ["organization-context"] as const;

export function useOrganizationContext() {
  return useQuery<OrganizationContextResponse>({
    queryKey: KEY,
    queryFn: () => api.get("/organization-context"),
  });
}

/** Whether the organization profile has never been saved: the read answered
 *  and holds no version. `false` while the read is pending or failed, so no
 *  "generic" notice flashes on load (#11324). */
export function useNoOrganizationProfile(): boolean {
  const context = useOrganizationContext();
  return context.data !== undefined && context.data.version == null;
}

/** The integration providers the outside lens pass reads (CTEM-26): the Settings picker offers only these. */
export function useAttackSurfaceProviders() {
  return useQuery<{ providers: string[] }>({
    queryKey: [...KEY, "attack-surface-providers"],
    queryFn: () => api.get("/organization-context/attack-surface/providers"),
    staleTime: 5 * 60_000,
  });
}

/** Whether every agent's prompt starts with the organization summary (#11533):
 *  the platform default an agent or an assignment overrides with its own mode. */
export interface OrganizationContextInjection {
  enabled: boolean;
  /** The categories every inheriting agent preloads after the basic block (none by default). */
  categories?: string[];
  /** Every category of the profile model, in order (read-only). */
  available_categories?: string[];
}

export function useOrganizationContextInjection() {
  return useQuery<OrganizationContextInjection>({
    queryKey: [...KEY, "injection"],
    queryFn: () => api.get("/organization-context/injection"),
  });
}

export function useSaveOrganizationContextInjection() {
  const qc = useQueryClient();
  return useMutation<OrganizationContextInjection, Error, { enabled: boolean; categories?: string[] }>({
    mutationFn: (body) => api.put("/organization-context/injection", body),
    onSuccess: (saved) => qc.setQueryData([...KEY, "injection"], saved),
  });
}

/** A fleet that reads a part of the profile, from its documented prerequisites (#9173, S2). */
export interface OrganizationFieldReader {
  fleet_id: string;
  name: string;
}

/** A seeded agent whose persona reads a part of the profile (#10063 r1 A3). */
export interface OrganizationAgentReader {
  agent_id: string;
  name: string;
}

/** Profile field ("sector", "crown_jewels", ...) -> the fleets, and the seeded agents, the reader can see that need it. */
export function useOrganizationReaders(enabled = true) {
  return useQuery<{ readers: Record<string, OrganizationFieldReader[]>; agents?: Record<string, OrganizationAgentReader[]> }>({
    queryKey: [...KEY, "readers"],
    queryFn: () => api.get("/organization-context/readers"),
    enabled,
  });
}

/** The steps of the guided CTEM loop setup, in order (`organization_setup_prompt.STEPS`, #1087). */
export type SetupStepKey = "organization" | "modules" | "ecosystem" | "gates" | "preview" | "enable";

/**
 * The first-connection setup proposal (#9173 S3, versioned by #1087): proposed
 * at install and once after an upgrade that carries a new setup version.
 */
export interface OrganizationSetupPrompt {
  /** True when the caller must be shown the proposal now. */
  propose: boolean;
  /** The instance's recorded answer to the current version, null while unanswered. */
  outcome: OrganizationSetupAnswer | null;
  answered_at: string | null;
  /** The setup version this build carries (`ctem-loop-1`). */
  version?: string;
  /** Step -> `done` or `skipped`, recorded for the current version. */
  steps?: Partial<Record<SetupStepKey, "done" | "skipped">>;
}

export type OrganizationSetupAnswer = "started" | "later" | "dismissed";

/** Asked only for an administrator in XTM One mode: the server decides, the client never shows it twice. */
export function useOrganizationSetupPrompt(enabled: boolean) {
  return useQuery<OrganizationSetupPrompt>({
    queryKey: [...KEY, "setup-prompt"],
    queryFn: () => api.get("/organization-context/setup-prompt"),
    enabled,
    staleTime: Infinity,
  });
}

export function useAnswerOrganizationSetupPrompt() {
  const qc = useQueryClient();
  return useMutation<OrganizationSetupPrompt, Error, OrganizationSetupAnswer>({
    mutationFn: (outcome) => api.post("/organization-context/setup-prompt", { outcome }),
    onSuccess: (state) => qc.setQueryData([...KEY, "setup-prompt"], state),
  });
}

/**
 * Record one step of the guided setup as done or skipped (`PUT
 * /organization-context/setup-prompt/steps`, administrators, #1087): each step
 * saves on its own, so leaving midway loses nothing. A progress record only.
 */
export function useRecordSetupStep() {
  const qc = useQueryClient();
  return useMutation<OrganizationSetupPrompt, Error, { step: SetupStepKey; state: "done" | "skipped" }>({
    mutationFn: (body) => api.put("/organization-context/setup-prompt/steps", body),
    onSuccess: (state) => {
      qc.setQueryData([...KEY, "setup-prompt"], state);
      qc.invalidateQueries({ queryKey: ["fleets", "setup", "ctem-loop"] });
    },
  });
}

/**
 * How many times Reset onboarding succeeded in this page session: a client-only
 * counter, never fetched. The setup proposal reopens on a new value only, not on
 * a refetch of the setup-prompt query (which any `organization-context`
 * invalidation triggers with identical data).
 */
const SETUP_PROMPT_RESETS_KEY = [...KEY, "setup-prompt-resets"] as const;

export function useOrganizationSetupPromptResets(): number {
  const { data } = useQuery<number>({
    queryKey: SETUP_PROMPT_RESETS_KEY,
    queryFn: () => 0,
    enabled: false,
    initialData: 0,
    staleTime: Infinity,
    gcTime: Infinity,
  });
  return data;
}

/**
 * Reset onboarding (`DELETE /organization-context/setup-prompt`, administrators,
 * XTM One mode): the answer and step progress are forgotten, so the guided setup
 * is proposed again as after a fresh install. Nothing else is changed.
 */
export function useResetOrganizationSetupPrompt() {
  const qc = useQueryClient();
  return useMutation<OrganizationSetupPrompt, Error, void>({
    mutationFn: () => api.delete("/organization-context/setup-prompt"),
    onSuccess: (state) => {
      qc.setQueryData([...KEY, "setup-prompt"], state);
      qc.setQueryData<number>(SETUP_PROMPT_RESETS_KEY, (count) => (count ?? 0) + 1);
      qc.invalidateQueries({ queryKey: [...KEY, "setup-prompt"] });
      qc.invalidateQueries({ queryKey: ["fleets", "setup", "ctem-loop"] });
    },
  });
}

/** A profile proposed from what OpenCTI, OpenAEV and OpenCRQ hold: nothing is saved. */
export interface OrganizationDraftResponse {
  based_on_version: number | null;
  profile: OrganizationProfile;
  /** One entry per field the draft filled, with the platform it came from. */
  filled: { field: string; source: string; detail: string }[];
  /** One line per platform that could not be read, and why. */
  unavailable: string[];
  missing_fields: string[];
}

/** "form" for a profile typed by hand, "draft" for one started from the platforms, "ai_draft" for accepted AI proposals. */
export type ProfileSource = "form" | "draft" | "ai_draft";

interface SaveOrganizationContextBody {
  profile: OrganizationProfile;
  source: ProfileSource;
  /** With "ai_draft": the accepted proposals' provenance, field -> {source, basis}, kept in the version history. */
  sources?: Record<string, { source: string; basis: string }>;
}

export function useSaveOrganizationContext() {
  const qc = useQueryClient();
  return useMutation<OrganizationContextResponse, Error, SaveOrganizationContextBody>({
    mutationFn: (body) => api.put("/organization-context", body),
    onSuccess: (saved) => {
      qc.setQueryData(KEY, saved);
      // The Inventory Assets tab and the asset page read the canonical assets (useOrganizationAssetsApi):
      // a saved profile adds, updates or deletes rows there, and a stale list maps row positions
      // onto the fresh profile (#1375 review).
      void qc.invalidateQueries({ queryKey: ["organization-assets"] });
    },
  });
}

/** One per-field proposal of "Draft with AI" (#9484): nothing is saved until it is accepted. */
export interface AiDraftProposal {
  /** A contract path, such as "tech_stack" or "risk_appetite.approval_threshold". */
  field: string;
  value: unknown;
  /** "description", "document:<name>", "website:<url>", "opencti", "openaev" or "opencrq". */
  source: string;
  confidence: "high" | "medium" | "low";
  basis: string;
  /** An inferred amount: shown "Check this", never accepted in bulk. */
  check: boolean;
  current: unknown;
  /** The current version already holds a value: never pre-accepted. */
  conflict: boolean;
}

export interface AiDraftResponse {
  based_on_version: number | null;
  proposals: AiDraftProposal[];
  /** Fields the profile misses that nothing given supports, named, never guessed. */
  not_supported: string[];
  suggested_pirs: string[];
  unavailable: string[];
  notes: string[];
  usage: Record<string, number>;
}

export interface AiDraftInput {
  description: string;
  documents: File[];
  website: string;
  usePlatforms: boolean;
}

/** One metered model call on the administrator's click; 429 is the quota refusal, answered before any read. */
export function useAiDraftOrganizationContext() {
  return useMutation<AiDraftResponse, Error, AiDraftInput>({
    mutationFn: ({ description, documents, website, usePlatforms }) => {
      const form = new FormData();
      form.append("description", description);
      form.append("website", website);
      form.append("use_platforms", String(usePlatforms));
      for (const file of documents) form.append("documents", file, file.name);
      return api.postForm("/organization-context/ai-draft", form);
    },
    // The dialog says every failure inline; the global toast would repeat the backend text.
    meta: { suppressGlobalErrorToast: true },
  });
}

/**
 * The connected OpenCTI's PIRs, read in view and edit mode alike (#9615); `unavailable` says
 * why the list is empty (the page then shows the saved names), `opencti_url` links each PIR.
 */
export interface OrganizationPirsResponse {
  pirs: { id: string; name: string }[];
  unavailable: "not_connected" | "unreadable" | null;
  opencti_url?: string;
}

export function useOrganizationPirs(enabled: boolean) {
  return useQuery<OrganizationPirsResponse>({
    queryKey: [...KEY, "pirs"],
    queryFn: () => api.get("/organization-context/pirs"),
    enabled,
    staleTime: 5 * 60_000,
  });
}

/** One field change of a stored proposal (#9484 S-a2; CTEM-30 "propose scope"): nothing changes until an administrator accepts it. */
export interface OrganizationContextFieldProposal {
  /** A contract path, such as "prioritization_policy.scope_min_criticality". */
  field: string;
  value: unknown;
  basis: string;
  current: unknown;
  conflict: boolean;
}

interface OrganizationContextProposal {
  id: string;
  kind: "fields" | "new_tracked";
  proposals: OrganizationContextFieldProposal[];
  status: "pending" | "accepted" | "rejected";
  created_at: string;
}

/** The pending proposals, newest first (administrators, like the page that lists them). */
export function useOrganizationContextProposals() {
  return useQuery<OrganizationContextProposal[]>({
    queryKey: [...KEY, "proposals"],
    queryFn: () => api.get("/organization-context/proposals?status=pending"),
  });
}

/** Accepts the named fields of a proposal (a new profile version) or rejects it with none. */
export function useDecideOrganizationContextProposal() {
  const qc = useQueryClient();
  return useMutation<{ status: "accepted" | "rejected"; version: number | null }, Error, { id: string; accept: string[] }>({
    mutationFn: ({ id, accept }) => api.post(`/organization-context/proposals/${encodeURIComponent(id)}/decide`, { accept }),
    // The profile and the list both move: an accepted field is a new version.
    onSuccess: () => qc.invalidateQueries({ queryKey: KEY }),
    // The card says the failure inline.
    meta: { suppressGlobalErrorToast: true },
  });
}

export function useDraftOrganizationContext() {
  return useMutation<OrganizationDraftResponse, Error, void>({
    mutationFn: () => api.post("/organization-context/draft", {}),
    // The section shows its own catalogued toast; the global one would repeat the backend text.
    meta: { suppressGlobalErrorToast: true },
  });
}

/** A profile section the platform import fills (#11780); one run imports one section. */
export type ImportSectionKey = "assets" | "asset_groups" | "ai_systems" | "tech_stack" | "crown_jewels" | "pirs";
/** The Filigran platforms, then the endpoint sources read through a Microsoft integration (#1251). */
export type ImportSourceKey =
  | "openaev"
  | "opencti"
  | "opencrq"
  | "intune"
  | "defender"
  | "tanium"
  | "crowdstrike"
  | "servicenow"
  | "github"
  | "gitlab";

export interface ImportSourcesResponse {
  sections: {
    section: ImportSectionKey;
    /** The profile field the section writes. */
    field: string;
    /** reason_code: `not_connected` or `no_token` when the dialog words the reason itself (#1251); empty: `reason` as given. */
    sources: { source: ImportSourceKey; available: boolean; reason: string; reason_code?: string; filters: string[] }[];
  }[];
}

/** Which platform can feed each section for this administrator, and why not; read when the dialog opens. */
export function useOrganizationImportSources(enabled: boolean) {
  return useQuery<ImportSourcesResponse>({
    queryKey: [...KEY, "import-sources"],
    queryFn: () => api.get("/organization-context/import/sources"),
    enabled,
  });
}

export interface ImportRequestBody {
  section: ImportSectionKey;
  source: ImportSourceKey;
  filters: Record<string, string | string[]>;
  /** The asset type of the batch, for a source without a type taxonomy (OpenCTI Systems). */
  batch_type: string;
  based_on_version?: number | null;
  /** Remove the rows this source wrote before that it no longer returns (default: kept). */
  remove_gone?: boolean;
}

/** One value a source offers for an import filter, read live (#11886). */
export interface ImportFilterOption {
  value: string;
  label: string;
  description: string;
  /** The category of a subcategory. */
  parent: string;
}

export interface ImportFilterOptionsResponse {
  options: Record<string, ImportFilterOption[]>;
  /** filter key -> why the source offers none (an older OpenCTI, a list that could not be read). */
  unavailable: Record<string, string>;
}

/** The values each filter of a section offers in a source, so the dialog picks instead of typing. */
export function useOrganizationImportOptions(section: ImportSectionKey | null, source: ImportSourceKey | null, enabled: boolean) {
  return useQuery<ImportFilterOptionsResponse>({
    queryKey: [...KEY, "import-options", section, source],
    queryFn: () => api.get(`/organization-context/import/options?section=${section}&source=${source}`),
    enabled: enabled && section !== null && source !== null,
    // An optional side read: a failure is said inline by the filters, never the page's error boundary.
    throwOnError: false,
    retry: 1,
    meta: { suppressGlobalErrorToast: true },
  });
}

/** What an import does: the source's own count, then new, updated, unchanged and skipped rows. */
interface ImportPlan {
  section: ImportSectionKey;
  source: ImportSourceKey;
  source_total: number | null;
  read: number;
  counts: { new: number; updated: number; unchanged: number; skipped: number; gone: number };
  new: string[];
  updated: string[];
  skipped: string[];
  /** Rows a previous import from this source wrote that it no longer returns. */
  gone: string[];
  remove_gone: boolean;
  /** Rows that took the batch type because the source gave them no category. */
  batch_typed: number;
  sample: Record<string, unknown>[];
  truncated: boolean;
  error: string | null;
}

export interface ImportPreviewResponse {
  based_on_version: number | null;
  plan: ImportPlan;
  applied: boolean;
  version: number | null;
}

/** The preview: nothing is written. */
export function usePreviewOrganizationImport() {
  return useMutation<ImportPreviewResponse, Error, ImportRequestBody>({
    mutationFn: (body) => api.post("/organization-context/import/preview", body),
    // The dialog says every failure inline.
    meta: { suppressGlobalErrorToast: true },
  });
}

/** The import: the source is read again and the next profile version saved. */
export function useApplyOrganizationImport() {
  const qc = useQueryClient();
  return useMutation<ImportPreviewResponse, Error, ImportRequestBody>({
    mutationFn: (body) => api.post("/organization-context/import/apply", body),
    onSuccess: (result) => {
      if (result.applied) qc.invalidateQueries({ queryKey: KEY });
    },
    meta: { suppressGlobalErrorToast: true },
  });
}

export type SyncFrequency = "hourly" | "daily" | "weekly";

export type SyncDirection = "inbound" | "outbound";

/** Per section and platform, the directions a sync may take and why outbound is not offered (#1134 S3). */
export interface SyncDirectionSupport {
  section: ImportSectionKey;
  source: ImportSourceKey;
  directions: SyncDirection[];
  /** Why outbound is not offered; empty when it is. */
  reason: string;
}

/** An import saved with a schedule (#11913); section "all" is the Sync all schedule that runs every other one. */
export interface OrganizationSync {
  id: string;
  section: ImportSectionKey | "all";
  source: ImportSourceKey | "";
  filters: Record<string, string | string[]>;
  batch_type: string;
  remove_gone: boolean;
  /** The source ids this sync never imports (#1134); omitted in a body: the stored list is kept. */
  excluded_external_ids?: string[];
  /**
   * inbound: the platform is the master; outbound: the XTM One list is (#1134 S3).
   * Omitted in a body: the stored direction is kept, so an edit never resets an outbound sync.
   */
  direction?: SyncDirection;
  /** Outbound: how many platform rows this sync created and keeps in step. */
  outbound_count?: number;
  frequency: SyncFrequency;
  hour: number;
  minute: number;
  /** 0 Monday ... 6 Sunday (weekly). */
  weekday: number;
  timezone: string;
  enabled: boolean;
  /** Why the sync stopped by itself (a missing or revoked token); empty while it runs. */
  paused_reason: string;
  owner_email: string | null;
  next_run_at: string | null;
  last_run_at: string | null;
  last_result: { added?: number; updated?: number; removed?: number; version?: number | null; error?: string | null } | null;
}

export type OrganizationSyncBody = Pick<
  OrganizationSync,
  "section" | "source" | "filters" | "batch_type" | "remove_gone" | "excluded_external_ids" | "direction" | "frequency" | "hour" | "minute" | "weekday" | "timezone" | "enabled"
>;

const SYNCS = [...KEY, "syncs"] as const;

/** Where a list of syncs is shown (#1134 S2): the business profile, the inventory, or every sync (""). */
export type OrganizationSyncScope = "profile" | "inventory" | "";

/** `enabled` false for a reader: the syncs API is for administrators only (403 otherwise, UX r1 B1 on #1540). */
export function useOrganizationSyncs(scope: OrganizationSyncScope = "", enabled = true) {
  return useQuery<OrganizationSync[]>({
    queryKey: [...SYNCS, scope],
    queryFn: () => api.get(scope ? `/organization-context/syncs?scope=${scope}` : "/organization-context/syncs"),
    enabled,
    // The list says its own failure with a retry; it never takes the page down.
    throwOnError: false,
    meta: { suppressGlobalErrorToast: true },
  });
}

/** The direction support matrix the sync dialog offers (#1134 S3); read only while the dialog is open. */
export function useOrganizationSyncDirections(enabled: boolean) {
  return useQuery<SyncDirectionSupport[]>({
    queryKey: [...SYNCS, "directions"],
    queryFn: () => api.get("/organization-context/syncs/directions"),
    enabled,
    staleTime: Infinity,
    throwOnError: false,
    meta: { suppressGlobalErrorToast: true },
  });
}

export function useCreateOrganizationSync() {
  const qc = useQueryClient();
  return useMutation<OrganizationSync, Error, OrganizationSyncBody>({
    mutationFn: (body) => api.post("/organization-context/syncs", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: SYNCS }),
    meta: { suppressGlobalErrorToast: true },
  });
}

export function useUpdateOrganizationSync() {
  const qc = useQueryClient();
  return useMutation<OrganizationSync, Error, { id: string; body: OrganizationSyncBody }>({
    mutationFn: ({ id, body }) => api.put(`/organization-context/syncs/${id}`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: SYNCS }),
    meta: { suppressGlobalErrorToast: true },
  });
}

/**
 * What happens to the rows a deleted sync wrote (#1276): ``leave`` keeps them owned by the
 * source, ``release`` hands them to people (editable by hand), ``purge`` removes them.
 */
export type SyncRowsMode = "leave" | "release" | "purge";

/** Delete a sync; ``rows`` says what its rows become (the API default, ``leave``, when omitted). */
export function useDeleteOrganizationSync() {
  const qc = useQueryClient();
  return useMutation<void, Error, string | { id: string; rows: SyncRowsMode }>({
    mutationFn: (target) =>
      typeof target === "string" ? api.delete(`/organization-context/syncs/${target}`) : api.delete(`/organization-context/syncs/${target.id}?rows=${target.rows}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: SYNCS });
      qc.invalidateQueries({ queryKey: KEY });
    },
    meta: { suppressGlobalErrorToast: true },
  });
}

/** One source of rows of the profile (#1276): a platform, an agent or partner, a CSV import, the API or a person. */
export interface OrganizationSource {
  key: string;
  kind: "platform" | "agent" | "csv" | "api" | "manual";
  display_name: string;
  /** Profile field -> rows of this source. */
  rows: Record<string, number>;
  total: number;
  /** The agent that last wrote this source's rows, when a saved version names it. */
  written_by_agent: string;
  sync_ids: string[];
}

export function useOrganizationSources(scope: OrganizationSyncScope = "") {
  return useQuery<OrganizationSource[]>({
    queryKey: [...KEY, "sources", scope],
    queryFn: () => api.get(scope ? `/organization-context/sources?scope=${scope}` : "/organization-context/sources"),
    throwOnError: false,
    meta: { suppressGlobalErrorToast: true },
  });
}

/** What a purge or a release would touch, per profile field, and the rows of other sources kept (#1276). */
export interface RowsPlan {
  source: string;
  mode: string;
  count: number;
  counts: Record<string, number>;
  rows: Record<string, string[]>;
  kept_elsewhere: Record<string, string[]>;
  version?: number | null;
}

/** The dry run of the delete dialog: a sync's rows (target.sync) or a source's rows (target.source). */
export function useOrganizationRowsPreview(target: { sync?: string; source?: string } | null, mode: "release" | "purge") {
  const path = target?.sync
    ? `/organization-context/syncs/${target.sync}/rows-preview?mode=${mode}`
    : target?.source
      ? `/organization-context/sources/${encodeURIComponent(target.source)}/rows-preview?mode=${mode}`
      : "";
  return useQuery<RowsPlan>({
    queryKey: [...KEY, "rows-preview", path],
    queryFn: () => api.get(path),
    enabled: path !== "",
    throwOnError: false,
    meta: { suppressGlobalErrorToast: true },
  });
}

/** Purge or release every row a source without a sync wrote (an agent, a partner, a CSV import). */
export function useApplyOrganizationSourceRows() {
  const qc = useQueryClient();
  return useMutation<RowsPlan, Error, { source: string; mode: "release" | "purge" }>({
    mutationFn: ({ source, mode }) => api.post(`/organization-context/sources/${encodeURIComponent(source)}/rows`, { mode }),
    onSuccess: () => qc.invalidateQueries({ queryKey: KEY }),
    meta: { suppressGlobalErrorToast: true },
  });
}

/** One entry of a sync's history: a run that changed the profile or failed, or a save. */
export interface SyncHistoryEntry {
  at: string;
  action: string;
  by: string | null;
  summary: string;
  trigger: string;
  added: number | null;
  updated: number | null;
  removed: number | null;
  version: number | null;
  error: string | null;
}

export function useOrganizationSyncHistory(id: string | null) {
  return useQuery<SyncHistoryEntry[]>({
    queryKey: [...SYNCS, "history", id],
    queryFn: () => api.get(`/organization-context/syncs/${id}/history`),
    enabled: id !== null,
    throwOnError: false,
    meta: { suppressGlobalErrorToast: true },
  });
}

/** Run now: a run that changed the profile saves a version, so the profile is read again too. */
export function useRunOrganizationSync() {
  const qc = useQueryClient();
  return useMutation<OrganizationSync, Error, string>({
    mutationFn: (id) => api.post(`/organization-context/syncs/${id}/run`, {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: KEY }),
    meta: { suppressGlobalErrorToast: true },
  });
}

/** What Run all syncs did: the syncs that ran and those whose run was refused or failed (#1548). */
export interface RunAllSyncsResult {
  ran: number;
  failed: number;
}

/**
 * Run all syncs (#1548): every given sync runs now, one after the other (two runs never write the profile at once),
 * through the per-sync Run now endpoint; the list is read again after each so its Last run cell follows. No "Sync all"
 * row is created.
 */
export function useRunOrganizationSyncs() {
  const qc = useQueryClient();
  return useMutation<RunAllSyncsResult, Error, string[]>({
    mutationFn: async (ids) => {
      let ran = 0;
      let failed = 0;
      for (const id of ids) {
        try {
          const done: OrganizationSync = await api.post(`/organization-context/syncs/${id}/run`, {});
          if (done?.last_result?.error) failed += 1;
          else ran += 1;
        } catch {
          failed += 1;
        }
        await qc.invalidateQueries({ queryKey: KEY });
      }
      return { ran, failed };
    },
    meta: { suppressGlobalErrorToast: true },
  });
}

/** A knowledge base the profile links as enterprise knowledge (#11543), readable or not for the caller. */
export interface EnterpriseKnowledgeLink {
  knowledge_base_id: string;
  label: string;
  kind: string;
  readable: boolean;
}

export function useEnterpriseKnowledge(enabled: boolean) {
  return useQuery<EnterpriseKnowledgeLink[]>({
    queryKey: [...KEY, "enterprise-knowledge"],
    queryFn: () => api.get("/organization-context/enterprise-knowledge"),
    enabled,
  });
}

/** The preview (apply false) or the save (apply true) of an assets CSV (#11543). */
export interface AssetsImportResponse {
  applied: boolean;
  assets: { name: string; type: string; environment: string; internet_facing: string; owner: string; group: string }[];
  links: Record<string, string[]>;
  errors: { line: number; name: string; message: string }[];
  unknown_crown_jewels: string[];
  replaced: string[];
  version: number | null;
}

export function useImportOrganizationAssets() {
  const qc = useQueryClient();
  return useMutation<AssetsImportResponse, Error, { file: File; apply: boolean }>({
    mutationFn: ({ file, apply }) => {
      const form = new FormData();
      form.append("file", file, file.name);
      form.append("apply", String(apply));
      return api.postForm("/organization-context/assets/import", form);
    },
    onSuccess: (result) => {
      if (result.applied) qc.invalidateQueries({ queryKey: KEY });
    },
    // The dialog says every failure inline.
    meta: { suppressGlobalErrorToast: true },
  });
}

/** The fill agent of one connected integration (#11888): created once, its daily sync with `schedule`. */
export interface FillAgentResponse {
  agent_id: string;
  agent_name: string;
  created: boolean;
  assignment_id: string | null;
  /** The preview instruction the chat starts with. */
  prompt: string;
}

export function useCreateOrganizationFillAgent() {
  const qc = useQueryClient();
  return useMutation<FillAgentResponse, Error, { integration_id: string; schedule: boolean; cron?: string; timezone?: string }>({
    mutationFn: (body) => api.post("/organization-context/fill-agent", body),
    // The chat picks its first agent from the cached agents list: refetch it (an unmounted list too)
    // before the dialog navigates, else a new fill agent is missing and the chat opens on the default one.
    onSuccess: () => {
      // A new agent (and its sync assignment) is a node of the flow catalog too.
      syncFlowNodeCatalog(qc);
      return qc.invalidateQueries({ queryKey: ["agents"], refetchType: "all" });
    },
    // The dialog says the failure inline.
    meta: { suppressGlobalErrorToast: true },
  });
}

export function useOpenCrqSync() {
  const qc = useQueryClient();
  // min_criticality left out: the saved crown-jewel sync's filter, else high (#1558).
  return useMutation<OpenCrqSyncResponse, Error, { apply: boolean; min_criticality?: OpenCrqLevel }>({
    mutationFn: (body) => api.post("/organization-context/opencrq-sync", body),
    onSuccess: (result) => {
      if (result.applied) qc.invalidateQueries({ queryKey: KEY });
    },
  });
}

/** A saved version of the profile, as the history lists it (#1473, #1558). */
export interface OrganizationContextVersionSummary {
  version: number;
  source: string;
  created_at: string;
  author_user_id: string | null;
  missing_fields: string[];
  /** The crown jewels the version holds; absent from an older backend. */
  crown_jewels?: number;
}

/** The saved versions, newest first (administrators); read only while the restore dialog is open. */
export function useOrganizationContextVersions(enabled: boolean) {
  return useQuery<OrganizationContextVersionSummary[]>({
    queryKey: [...KEY, "versions"],
    queryFn: () => api.get("/organization-context/versions?limit=100"),
    enabled,
  });
}

/** One saved version whole, for the preview of what a restore brings back. */
export function useOrganizationContextVersion(version: number | null) {
  return useQuery<OrganizationContextResponse>({
    queryKey: [...KEY, "versions", version],
    queryFn: () => api.get(`/organization-context/versions/${version}`),
    enabled: version !== null,
  });
}

/**
 * Restores *fields* of a past version as a new version (#1473): the other fields stay as they are,
 * and a version saved since *based_on_version* answers 409, nothing written.
 */
export function useRestoreOrganizationContextVersion() {
  const qc = useQueryClient();
  return useMutation<OrganizationContextResponse, Error, { version: number; fields: string[]; based_on_version: number | null }>({
    mutationFn: ({ version, ...body }) => api.post(`/organization-context/versions/${version}/restore`, body),
    // The dialog shows the refusal inline (a 409 offers a reload of the versions): no global toast.
    meta: { suppressGlobalErrorToast: true },
    onSuccess: (saved) => {
      qc.setQueryData(KEY, saved);
      qc.invalidateQueries({ queryKey: KEY });
    },
  });
}
