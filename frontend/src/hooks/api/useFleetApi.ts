/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * Fleet API hooks: the reads behind the Fleets tab and the
 * fleet page. The contract is `backend/app/api/v1/fleets.py` and
 * `backend/app/api/v1/schemas/fleet.py`; the types below mirror the response
 * models field for field and nothing more.
 *
 * Every fleet endpoint sits behind the agentic AI licence: without it the API
 * answers 403 and `isFleetsUnavailable` lets the page say so instead of
 * showing an error boundary.
 */

import { useCallback, useEffect, useRef } from "react";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, isLicenceWallError } from "@/lib/api";
import { fleetAccess } from "@/lib/fleet-operations";
import { syncFlowNodeCatalog } from "@/lib/flow-node-catalog";
import { saveBlobAs } from "@/lib/utils";
import type { AgentBudgetConfig } from "@/types";

export interface Fleet {
  id: string;
  user_id: string | null;
  owner_name: string | null;
  name: string;
  slug: string;
  description: string;
  icon: string;
  color: string;
  workspace_id: string;
  workspace_name: string;
  orchestrator_agent_id: string | null;
  skills: { name: string; description?: string; tags?: string[] }[] | null;
  accepted_handover_types: string[] | null;
  is_archived: boolean;
  is_company_managed: boolean;
  /** Runs, traces, work items and state hidden from non-admins (#11624). */
  hide_activity_from_non_admins?: boolean;
  /** Seeded by the platform: read-only except archiving and the run defaults. */
  is_builtin?: boolean;
  shared_group_ids: string[] | null;
  shared_group_permissions: Record<string, string> | null;
  shared_user_ids: string[] | null;
  shared_user_permissions?: Record<string, string> | null;
  access_restricted: boolean;
  /** False on the built-in Platform Health fleet for everyone, administrators included. */
  can_manage: boolean;
  /** Why nobody may change it (the Platform Health fleet, #101 S1b); null otherwise. */
  read_only_reason?: string | null;
  member_count: number;
  connected_count: number;
  exposed_agent_ids: string[];
  /** Run budget default of the home members; `null` = none, their own wins. */
  budget_config?: AgentBudgetConfig | null;
  /** Iteration cap default of the home members; `null` = none. */
  max_iterations?: number | null;
  /** What a connected member inherits while it does not override the field. */
  member_defaults?: FleetMemberDefaults;
  /** Provenance of a fork (`POST /fleets/{id}/fork`); `null` = not a fork. */
  forked_from?: FleetProvenance | null;
  /** How many gated actions wait for a person (#9769); `full_gates` by default. */
  autonomy_level?: FleetAutonomyLevel;
  /** How many actions that level acts on (#10457); `0` = it gates nothing here. */
  gated_action_count?: number;
  /** Where its instructions put it on the scale (#10168); `null` = unset. */
  instructions_position?: FleetInstructionsPosition | null;
  /** The opt-in state of its automations, as on the health (#9977 S2, the list card). */
  automations?: FleetAutomations | null;
  /** The newest run of a home member the reader may see; `null` = never run. */
  last_run_at?: string | null;
  /** The Platform Health fleet only (#974): what starts it and what it covers, from its Settings. */
  health_starts?: FleetHealthStarts | null;
  created_at: string;
  updated_at: string;
}

/** What starts the Platform Health fleet (`FleetHealthStartsResponse`, #974): Settings schedules and signals. */
interface FleetHealthStarts {
  /** A signal pass is on: an investigation starts on an event. */
  events: boolean;
  /** The schedules switched on in Settings > Platform Health. */
  schedules: number;
  /** The soonest next start of those schedules; `null` when none is on. */
  next_run_at: string | null;
  /** The modules it covers, XTM One modes only; empty in Copilot mode. */
  modules: string[];
  /** Where an administrator switches and schedules it. */
  settings_route: string;
  /** Platform Health is on in Settings; off, nothing starts and `next_run_at` is `null` (UX r1 B2 on #980). */
  active?: boolean;
}

/** Where a forked fleet came from, as recorded at fork time (`fleets.forked_from`). */
interface FleetProvenance {
  fleet_id: string;
  slug: string;
  name: string;
  is_builtin: boolean;
  fingerprint: string;
  members?: FleetForkMembers;
  forked_at: string;
}

/** `copy`: every member becomes an editable copy. `keep`: the orchestrator is copied, the others are connected. */
export type FleetForkMembers = "copy" | "keep";

/** `POST /fleets/{id}/fork` body (`FleetFork`). */
export interface FleetForkRequest {
  name?: string | null;
  workspace_id?: string | null;
  members: FleetForkMembers;
}

/** `POST /fleets/{id}/fork` answer (`FleetForkResponse`). */
export interface FleetForkResult {
  fleet: Fleet;
  copied_agent_ids: Record<string, string>;
  kept_agent_ids: string[];
  assignments_copied: number;
  /** Always 0: every copied assignment is switched off. */
  assignments_enabled: number;
  warnings: string[];
}

/**
 * The organizations a member reasons about (`OrganizationScope`): the primary
 * alone, the primary and every monitored organization, or the primary and a
 * list of tracked organizations.
 */
export type FleetOrganizationScope =
  | { mode: "primary" }
  | { mode: "monitored" }
  | { mode: "list"; organization_ids: string[] };

/** The settings a member inherits from its fleet (`FleetMemberDefaults`). */
interface FleetMemberDefaults {
  context_inclusion?: FleetContextInclusion;
  collaboration_instruction?: string;
  /** Every member, home or connected. */
  organization_scope?: FleetOrganizationScope;
}

/** A member setting a row may override; the others inherit the fleet default. */
export type FleetMemberSetting = keyof FleetMemberDefaults;

/** `GET /fleets/{id}/cost`: the runs attributed to the fleet over a window. */
interface FleetAgentCost {
  agent_id: string | null;
  agent_name: string;
  trace_count: number;
  total_cost: number;
  total_tokens: number;
}

export interface FleetCost {
  fleet_id: string;
  days: number;
  since: string;
  total_cost: number;
  total_tokens: number;
  trace_count: number;
  agents: FleetAgentCost[];
}

type FleetGraphNodeKind = "member" | "connected" | "external" | "fleet";

export interface FleetGraphNode {
  id: string;
  kind: FleetGraphNodeKind;
  name: string;
  icon: string | null;
  agent_id: string | null;
  fleet_id: string | null;
  fleet_name: string;
  member_id: string | null;
  parent_agent_id: string | null;
  is_orchestrator: boolean;
  is_exposed: boolean;
  enabled: boolean;
  visible: boolean;
  collaboration_instruction: string;
  context_inclusion: string | null;
  entry_agent_id: string | null;
}

export interface FleetGraphEdge {
  source: string;
  target: string;
  type: string;
  scope: "internal" | "outbound" | "inbound";
  governed: boolean;
  allowed: boolean;
  source_fleet_id: string | null;
  target_fleet_id: string | null;
}

export interface FleetGraph {
  fleet_id: string;
  orchestrator_agent_id: string | null;
  nodes: FleetGraphNode[];
  edges: FleetGraphEdge[];
}

/** A node of the fleet data flow (`GET /fleets/{id}/flow`, #9829). */
export interface FleetFlowNode {
  id: string;
  lane: "source" | "agent" | "output";
  /** integration, platform_event, schedule, webhook, work_item, manual, agent or work_items. */
  kind: string;
  /** Empty when the reader cannot see it. */
  name: string;
  icon: string | null;
  provider: string | null;
  integration_id: string | null;
  agent_id: string | null;
  events: string[];
  enabled: boolean;
  /** disabled, agent disabled, not connected, paused. */
  reason: string | null;
  visible: boolean;
}

/** trigger (source to member, one per assignment), handover, write or read. */
export interface FleetFlowEdge {
  source: string;
  target: string;
  kind: "trigger" | "handover" | "write" | "read";
  assignment_id: string | null;
  assignment_name: string;
  events: string[];
  detail: string;
  /** A schedule trigger's cron and timezone, so the client words it in the reader's language. */
  cron?: string;
  timezone?: string;
  filter: string;
  gate: string;
  type: string | null;
  enabled: boolean;
  reason: string | null;
}

export interface FleetFlow {
  fleet_id: string;
  nodes: FleetFlowNode[];
  edges: FleetFlowEdge[];
}

export type FleetMemberHealthStatus = "failing" | "disabled" | "stale" | "idle" | "ok";

export interface FleetMemberHealth {
  agent_id: string;
  name: string;
  status: FleetMemberHealthStatus;
  enabled: boolean;
  last_run_at: string | null;
  last_run_status: string | null;
  /** `interrupted` when the platform ended the last run. */
  last_run_outcome?: string | null;
  runs_in_flight: number;
  /** The run verdict's counts (`run_outcome.member_verdict`): failed and
   *  judged runs of the window, and the runs the platform interrupted. */
  failures_24h?: number;
  recent_runs?: number;
  recent_interruptions?: number;
  open_work_items: number;
  stale_work_items: number;
}

/** The opt-in state of a fleet's automations (`FleetAutomationsResponse`, #9566 / #9769). */
export interface FleetAutomations {
  total: number;
  enabled: number;
  /** Switched off after being on (a person, a quarantine, the fleet's Pause all). */
  paused: number;
  /** Of `paused`, the ones the fleet's Pause all switched off: what Resume switches back on. */
  fleet_paused?: number;
}

export interface FleetHealth {
  fleet_id: string;
  status: string;
  members: FleetMemberHealth[];
  workspace_open_work_items: number;
  /** Whether the fleet is set up: its automations, on and paused. */
  automations?: FleetAutomations;
  /** Its runs over the last 30 days, the reader's visible ones (#10123): the About KPI row. */
  run_stats?: FleetRunStats;
}

/** `GET /fleets/{id}/health` `run_stats` (`fleet_views.FleetRunStats`). */
interface FleetRunStats {
  days: number;
  runs: number;
  succeeded: number;
  /** Failed runs the platform did not interrupt. */
  failed: number;
  /** One count per day, oldest first. */
  by_day: number[];
  /** Runs of the fleet waiting for a person now. */
  awaiting_approval: number;
}

/** The outcome of a fleet's Pause all or Resume (`FleetControlResponse`, #9769 S2). */
export interface FleetControlResult {
  fleet_id: string;
  /** The automations this call switched off (pause) or back on (resume). */
  changed: number;
  automations: FleetAutomations;
}

const KEY = ["fleets"];

/** A licence refusal from a fleet endpoint: the route gate's 403, or the
 *  Copilot-mode licence wall the API client raises as BackendUnavailableError. */
export function isFleetsUnavailable(error: unknown): boolean {
  return (error as { status?: number } | null)?.status === 403 || isLicenceWallError(error);
}

/** A refusal (no licence, the route gate's 403 or the Copilot-mode licence
 *  wall) or a missing fleet: a state the page renders, not a crash. */
function isOrdinaryFleetError(error: unknown): boolean {
  return isFleetsUnavailable(error) || (error as { status?: number } | null)?.status === 404;
}

/** Retry anything but a refusal or a missing fleet: neither changes on retry. */
function retryUnlessRefused(failureCount: number, error: unknown): boolean {
  if (isOrdinaryFleetError(error)) return false;
  return failureCount < 2;
}

/**
 * The app throws a failed query to the page's ErrorBoundary (`main.tsx`); a
 * licence refusal or a 404 from a fleet read is rendered by the page itself
 * instead, without a retry.
 */
export const FLEET_QUERY_POLICY = {
  retry: retryUnlessRefused,
  throwOnError: (error: Error) => !isOrdinaryFleetError(error),
};

/**
 * The fleet list. With `optional`, the list only enriches another view (the
 * map's fleets layer): any failure stays on the query and the caller renders
 * without it instead of the page's ErrorBoundary. It has its own cache entry,
 * so an error it tolerated never reaches the Fleets tab's strict read.
 *
 * Turning `includeArchived` on or off keeps the previous list on screen while
 * the other one loads (`keepPreviousData`): the Fleets tab stays mounted, its
 * Filters panel open under the switch just pressed.
 */
export function useFleets(options: { enabled?: boolean; optional?: boolean; includeArchived?: boolean } = {}) {
  return useQuery<Fleet[]>({
    queryKey: [
      ...KEY,
      "list",
      ...(options.optional ? ["optional"] : []),
      ...(options.includeArchived ? ["archived"] : []),
    ],
    queryFn: () => api.get(options.includeArchived ? "/fleets?include_archived=true" : "/fleets"),
    enabled: options.enabled ?? true,
    placeholderData: keepPreviousData,
    ...FLEET_QUERY_POLICY,
    // Optional: a failure falls back at once (no retry), the map draws every agent.
    ...(options.optional ? { throwOnError: false, retry: false } : {}),
  });
}

/**
 * The fleets the reader can access and whether there is any: the one
 * rule every fleet filter gates on, on the optional list (a refusal hides the
 * filter instead of reaching the page's ErrorBoundary). It shares the cache
 * entry of `useFleets({ optional: true })`, so creating a fleet or being
 * granted one shows the filter at the next refetch.
 */
export function useAccessibleFleets(options: { enabled?: boolean } = {}) {
  const { data, isError } = useFleets({ optional: true, enabled: options.enabled });
  return { fleets: data ?? [], access: fleetAccess(data, isError) };
}

export function useFleet(fleetId: string | null | undefined) {
  return useQuery<Fleet>({
    queryKey: [...KEY, "detail", fleetId],
    queryFn: () => api.get(`/fleets/${fleetId}`),
    enabled: !!fleetId,
    ...FLEET_QUERY_POLICY,
  });
}

/**
 * The fleet's cost over the last `days` (manager only: the API answers 403 to
 * anyone else). An optional side card: its own inline line says a failed read,
 * so neither the app-wide `throwOnError` nor `FLEET_QUERY_POLICY` may hand the
 * failure to the page's ErrorBoundary (the graph, the members and the settings
 * stay on screen), and a retry would only delay that line.
 */
export function useFleetCost(fleetId: string | null | undefined, days: number, enabled = true) {
  return useQuery<FleetCost>({
    queryKey: [...KEY, "cost", fleetId, days],
    queryFn: () => api.get(`/fleets/${fleetId}/cost?days=${days}`),
    enabled: !!fleetId && enabled,
    ...FLEET_QUERY_POLICY,
    retry: false,
    throwOnError: false,
  });
}

/** The runs one organization of a scope adds to a fleet (dev-docs/ctem-fleets.md 10.3). */
interface FleetOrganizationRunEstimate {
  organization_id: string;
  name: string;
  role: "primary" | "tracked";
  monitoring: string;
  daily_run_cap: number | null;
  expected_runs_per_month: number;
  /** Why no figure could be read (no OpenCTI connection, the primary), else null. */
  note: string | null;
  /** The member assignments whose trigger serves this organization; `error` set when its count failed. */
  assignments: { assignment_id: string; expected_runs_per_month: number | null; error: string | null }[];
}

export interface FleetRunEstimate {
  fleet_id: string;
  window_days: number;
  organizations: FleetOrganizationRunEstimate[];
  expected_runs_per_month: number;
}

/**
 * Expected runs per month of the fleet's home members for the tracked
 * organizations a scope editor picked, before the save (`GET
 * /fleets/{id}/run-estimate`). Read only when at least one tracked
 * organization is picked: without ids the API answers the fleet's saved
 * scope, which is not what the editor shows. An optional line of a dialog:
 * a failed read stays in that line (no ErrorBoundary, no retry).
 */
export function useFleetRunEstimate(fleetId: string | null | undefined, organizationIds: readonly string[]) {
  const ids = [...organizationIds].sort();
  return useQuery<FleetRunEstimate>({
    queryKey: [...KEY, "run-estimate", fleetId, ids],
    queryFn: () =>
      api.get(`/fleets/${fleetId}/run-estimate?${ids.map((id) => `organization_ids=${encodeURIComponent(id)}`).join("&")}`),
    enabled: !!fleetId && ids.length > 0,
    placeholderData: keepPreviousData,
    ...FLEET_QUERY_POLICY,
    retry: false,
    throwOnError: false,
  });
}

export function useFleetGraph(fleetId: string | null | undefined) {
  return useQuery<FleetGraph>({
    queryKey: [...KEY, "graph", fleetId],
    queryFn: () => api.get(`/fleets/${fleetId}/graph`),
    enabled: !!fleetId,
    ...FLEET_QUERY_POLICY,
  });
}

/** The fleet's data flow: configuration only, never a run or quota. Read only while the view shows it. */
export function useFleetFlow(fleetId: string | null | undefined, shown = true) {
  return useQuery<FleetFlow>({
    queryKey: [...KEY, "flow", fleetId],
    queryFn: () => api.get(`/fleets/${fleetId}/flow`),
    enabled: !!fleetId && shown,
    ...FLEET_QUERY_POLICY,
    throwOnError: false,
  });
}

/** An integration as a capability slot shows it (`FleetSlotIntegration`, #914 slice 2). */
export interface FleetSlotIntegration {
  integration_id: string;
  provider: string;
  /** Empty when the reader cannot see the bound integration. */
  name: string;
}

/** One capability a member's role can use, and how it is filled (`FleetCapabilitySlot`). */
interface FleetCapabilitySlot {
  capability: string;
  label: string;
  state: "filled" | "empty" | "unavailable";
  bound: FleetSlotIntegration[];
  candidates: FleetSlotIntegration[];
  /** The built-in option, when the capability has one: it fills the slot by itself (#1201). */
  builtin?: FleetSlotBuiltin | null;
  /** Integrations of the capability this member cannot take, and why (#1373). */
  blocked?: FleetSlotBlockedIntegration[];
}

/** An integration of the capability a member cannot take (`FleetSlotBlockedIntegration`, #1373). */
export interface FleetSlotBlockedIntegration extends FleetSlotIntegration {
  /** `not_company_managed`: the members are company-managed and take company-managed integrations only. */
  reason: "not_company_managed" | "not_connected";
}

/** A capability's built-in option (`FleetSlotBuiltin`, #1201): served by XTM One, never stored as a binding. */
interface FleetSlotBuiltin {
  key: string;
  label: string;
  /** Always active: an external integration adds to it and it cannot be removed. */
  always_on: boolean;
}

/** One member using a capability type, in the expanded type row (`FleetCapabilityTypeMember`, #1201). */
export interface FleetCapabilityTypeMember {
  member_id: string;
  agent_id: string;
  name: string;
  role_label: string;
  can_edit: boolean;
  bound: FleetSlotIntegration[];
  /** Its bound set differs from the fleet choice: an override ("Custom"). */
  custom: boolean;
}

/** A capability across the fleet's members: one row of the type view (`FleetCapabilityType`, #1201). */
export interface FleetCapabilityType {
  capability: string;
  label: string;
  state: "connected" | "partial" | "not_bound" | "not_usable" | "none_connected";
  /** The fleet choice: the bound set most members share. */
  choice: FleetSlotIntegration[];
  candidates: FleetSlotIntegration[];
  members: FleetCapabilityTypeMember[];
  builtin?: FleetSlotBuiltin | null;
  /** What no member can take, and why (#1373): the row names it instead of "Connect one". */
  blocked?: FleetSlotBlockedIntegration[];
}

/** The capability slots of one home member (`FleetMemberSlots`). */
export interface FleetMemberSlots {
  member_id: string;
  agent_id: string;
  name: string;
  role_label: string;
  /** Whether the reader may bind or unbind on this member's agent. */
  can_edit: boolean;
  slots: FleetCapabilitySlot[];
}

/** `GET /fleets/{id}/capability-slots`. */
export interface FleetCapabilitySlots {
  fleet_id: string;
  members: FleetMemberSlots[];
  /** The same slots grouped by capability (#1201): one row per type. */
  types: FleetCapabilityType[];
}

/** Each home member's capability slots: configuration only, never a run or quota (#914). */
export function useFleetCapabilitySlots(fleetId: string | null | undefined, shown = true) {
  return useQuery<FleetCapabilitySlots>({
    queryKey: [...KEY, "capability-slots", fleetId],
    queryFn: () => api.get(`/fleets/${fleetId}/capability-slots`),
    enabled: !!fleetId && shown,
    ...FLEET_QUERY_POLICY,
    throwOnError: false,
  });
}

/** One step of the CTEM loop guided setup (`FleetSetupStep`, #1087). */
export interface FleetSetupStep {
  key: "organization" | "modules" | "ecosystem" | "gates" | "preview" | "enable";
  /** `done` / `skipped` as recorded for the current setup version, else `ready` (already in place) or `todo`. */
  state: "done" | "skipped" | "ready" | "todo";
  /** What the step shows, read live (counts, connected modules, slot states, level, opt-in). */
  facts: Record<string, unknown>;
}

/** `GET /fleets/setup/ctem-loop` (#1087): where the guided setup stands; administrators of XTM One only. */
export interface FleetSetupStatus {
  version: string;
  /** The CTEM loop fleet, null when it is not seeded on this instance. */
  fleet_id: string | null;
  steps: FleetSetupStep[];
  /** The step the flow opens on; null when every step is done or skipped. */
  resume_on: FleetSetupStep["key"] | null;
}

/** The CTEM loop setup's step states, read while the guided setup is open: read only, no quota. */
export function useCtemLoopSetupStatus(enabled: boolean) {
  return useQuery<FleetSetupStatus>({
    queryKey: [...KEY, "setup", "ctem-loop"],
    queryFn: () => api.get("/fleets/setup/ctem-loop"),
    enabled,
    staleTime: 0,
    retry: false,
    throwOnError: false,
  });
}

/** One object of a fork's source that moved since the fork (`FleetSourceChange`). */
export interface FleetSourceChange {
  object: "fleet" | "member" | "connected";
  /** The fleet's name, a member's agent slug, or a connected agent or fleet id. */
  name: string;
  action: "added" | "removed" | "changed";
  /** The fields that moved, on a change. */
  fields: string[];
}

/** `GET /fleets/{id}/source-changes`: whether, and how, the source of a fork changed since the fork. */
export interface FleetSourceChanges {
  is_fork: boolean;
  source_exists: boolean;
  /** `null` when unknown: not a fork, or the source is gone. */
  changed: boolean | null;
  /** False on a fork older than the kept structure: the diff is not known. */
  diff_available: boolean;
  changes: FleetSourceChange[];
  /** The seeded fleet that took over a retired built-in source (CTEM-31), else absent or `null`. */
  successor?: { fleet_id: string; slug: string; name: string } | null;
}

/**
 * What changed in a fork's source since the fork (#9174). An optional notice on
 * the fleet page: a failed read leaves the page as it is, never its ErrorBoundary.
 */
export function useFleetSourceChanges(fleetId: string | null | undefined, enabled = true) {
  return useQuery<FleetSourceChanges>({
    queryKey: [...KEY, "source-changes", fleetId],
    queryFn: () => api.get(`/fleets/${fleetId}/source-changes`),
    enabled: !!fleetId && enabled,
    ...FLEET_QUERY_POLICY,
    retry: false,
    throwOnError: false,
  });
}

/** Where a restored version came from (#9768 S2): the version brought back (`null`: the shipped text) and its text's author. */
export interface FleetDocumentationRestoredFrom {
  version: number | null;
  author: "seed" | "fork" | "ai";
}

/** `GET /fleets/{id}/documentation` (`FleetDocumentationResponse`): the fleet's About page. */
export interface FleetDocumentation {
  /** Every section key, in reading order, written or not. */
  section_keys: string[];
  /** Section key to markdown; a section never written is absent. */
  sections: Record<string, string>;
  /** 0 while nothing is written. */
  version: number;
  updated_at: string | null;
  /** A user id, `seed` (built-in) or `fork` (copied at the fork). */
  updated_by: string | null;
  /** Whether the reader may regenerate it: a fleet the reader manages, built-in or custom. */
  editable: boolean;
  /** A built-in fleet regenerated locally: "Restore shipped version" brings back the seed. */
  local?: boolean;
  /** Set when the version shown is a restore. */
  restored_from?: FleetDocumentationRestoredFrom | null;
  /** `null` while nothing is written; true once the fleet moved since the last save. */
  out_of_date: boolean | null;
  /** The structured parts the About page draws (`fleet_docs.StructuredDoc`); `null` when none. */
  structured?: FleetDocStructured | null;
  /** `structured.prerequisites` evaluated for the reader (rows only, no run). */
  prerequisite_states?: FleetPrerequisiteState[];
  /** The checklist derived from the members' configuration, checked live for the reader (#10258); wins over `prerequisite_states`. */
  live_prerequisites?: FleetLivePrerequisite[];
  /** The fleet's autonomy level (#9769). */
  autonomy_level?: FleetAutonomyLevel;
  /** Its declared gates with whether each waits, at this level and at every level (`fleet_autonomy.gate_catalog`). */
  autonomy_catalog?: FleetAutonomyGate[];
  /** What the AI found wrong with the fleet's configuration (#10246), dismissed ones left out; empty for a reader who cannot manage the fleet. */
  findings?: FleetDocFinding[];
  /** The dismissals that still hold (#10536): who, when and why, for the dismissed view and Restore; empty for a reader who cannot manage the fleet. */
  dismissed_findings?: FleetDismissedFinding[];
  /** A generation runs on the server (#10316, #10817): the first one at creation or a regenerate, from any tab or the fleet tool. The page spins and polls until it lands. */
  generating?: boolean;
  /** When the running generation started (ISO 8601 UTC); null when nothing runs or unknown. */
  generating_started_at?: string | null;
  /** Why the last generation failed, for a few minutes after it and while nothing runs (#10817). */
  generation_failed?: string | null;
}

/** One configuration finding (`fleet_doc_findings.clean_findings`): shown as a suggestion with Fix with AI, Open and Dismiss. */
export interface FleetDocFinding {
  id: string;
  kind:
    | "description_mismatch"
    | "missing_automation"
    | "no_gate_on_sensitive_action"
    | "missing_integration"
    | "member_without_role"
    | "member_cannot_consult_orchestrator"
    | "member_chat_open"
    | "owner_without_inbox"
    | "orchestrator_without_listeners"
    | "fleet_without_working_knowledge"
    | "orchestrator_cannot_write_knowledge"
    | "member_missing_working_knowledge"
    | "several_writers_on_working_knowledge"
    | "other";
  /** The member concerned, by name; empty when the finding is about the fleet. */
  member: string;
  finding: string;
  suggested_fix: string;
  /** The object the finding is about (assignment, integration, setting): what keeps a dismissal across regenerations (#10536). */
  subject?: string;
  /** `practice`: a good-practice suggestion computed from the configuration (`fleet_practice_suggestions`, #11948). */
  source?: "practice";
  /** A practice suggestion a person applies in one click (`POST .../findings/{id}/apply`). */
  applicable?: boolean;
  /** The member's agent, for a practice suggestion. */
  agent_id?: string;
  /** The knowledge base a knowledge practice suggestion is about (#11972). */
  knowledge_base_id?: string;
}

/** One dismissal that still holds (`fleet_docs.dismissed_view`, #10536). */
export interface FleetDismissedFinding {
  id: string;
  kind: FleetDocFinding["kind"];
  member: string;
  subject: string;
  finding: string;
  /** ISO time of the dismissal. */
  at: string;
  by: string;
  by_name: string;
  /** Optional one line: standing decision, false positive or free text. */
  reason: string;
  /** `practice` for a good-practice suggestion: worded in the reader's language like the open row (#11961). */
  source?: "practice" | "";
}

/** How often the About page reads the documentation again while its first version is generated (#10316). */
const DOC_GENERATING_REFRESH_MS = 5_000;

/** One version note (`fleet_docs.doc_note`): a code and its params, rendered in the reader's language. */
export interface FleetDocNote {
  code: string;
  params?: Record<string, string | number>;
}

/** How many of a fleet's gated actions wait for a person (`fleets.autonomy_level`, #9769). */
export type FleetAutonomyLevel = "full_gates" | "medium" | "low" | "autonomous";

/** A per-action override of the level (`fleet_autonomy.APPROVAL_PINS`). */
export type FleetGatePin = "wait" | "run";

/** One declared gate of the fleet, as `fleet_autonomy.gate_catalog` returns it. */
export interface FleetAutonomyGate {
  key: string;
  action: string;
  module: FleetDocModule;
  /** The integration a gate proposed from write tools acts in (#10034); absent for a declared gate. */
  provider?: string;
  impact: "low" | "high" | "destructive";
  /** A per-action override of the level (#10153; on a built-in fleet too since #916): `wait` always waits, `run` always runs alone; absent, the gate follows the level. */
  pin?: FleetGatePin | null;
  /** Waits for a person at the fleet's level. */
  gated: boolean;
  /** Waits for a person, per level. */
  by_level: Record<FleetAutonomyLevel, boolean>;
}

/** One gate of a fleet's approval gate catalog, stored or proposed (`fleet_docs.DocGate`, #10014). */
export interface FleetApprovalGate {
  key: string;
  action: string;
  module: FleetDocModule;
  approver?: string;
  /** The integration a proposed gate writes to (#10034): the page draws its mark and name. */
  provider?: string;
  impact: "low" | "high" | "destructive";
  /** The tool calls the gate pauses; none = the rule is stated to the agent only. */
  tools?: { name: string; when?: Record<string, unknown> }[];
}

/** `GET /fleets/{id}/approval-gates` (#10014): the catalog, the proposals computed from the members, the approvals outside the level. */
export interface FleetApprovalGates {
  catalog: FleetApprovalGate[];
  /** The catalog is stored on the fleet (configuration), not read from its documentation. */
  stored: boolean;
  /** Computed from the members' write tools, no model call. */
  proposals: FleetApprovalGate[];
  /** Approvals that already apply outside the level: an agent's human in the loop, its tool-call approval, an automation's. */
  existing_approvals: { member: string; approval: string }[];
  /** The reader may write the catalog (a manager of a custom fleet). */
  editable: boolean;
  /** Where the fleet's instructions put it on the same scale (#10168); `null` = not set. Never enforced by the platform. */
  instructions_position?: FleetInstructionsPosition | null;
  /** The members are told the position in their run prompt (a person moved it). */
  instructions_position_announced?: boolean;
}

/** Where a fleet's instructions put it, between-step positions included (`fleet_autonomy.INSTRUCTIONS_POSITIONS`, #10168). */
export type FleetInstructionsPosition =
  | "full_gates"
  | "full_gates_medium"
  | "medium"
  | "medium_low"
  | "low"
  | "low_autonomous"
  | "autonomous";

/**
 * Where a trigger, a gate or an object lives (`fleet_docs.DocModule`): the page draws its mark. A base
 * module (opencti, openaev, opencrq, schedule, work_item, platform, webhook, manual) or, since #9979, the
 * key of any integration provider of the catalog (microsoft, github, notion...), drawn with its logo.
 */
export type FleetDocModule = string;

/** The structured parts of a fleet's documentation (`fleet_docs.StructuredDoc`), every field optional. */
export interface FleetDocStructured {
  /** One sentence, at most 25 words (#10123). */
  purpose_line?: string;
  outcome_line?: string;
  /** Who the fleet serves, as short chips (#10123). */
  serves?: string[];
  /** One line per member, at most 12 words (#10123), matched to the live members by name. */
  members?: { name: string; role?: string }[];
  /** `enabled` false: the automation behind the card is switched off (drawn with its state, never hidden). */
  triggers?: { module: FleetDocModule; name: string; filter?: string; detail?: string; automation_slug?: string; enabled?: boolean }[];
  steps?: { title: string; detail?: string; gate?: boolean }[];
  gates?: { action: string; module?: FleetDocModule; approver?: string }[];
  objects?: { module: FleetDocModule; objects: string[] }[];
  prerequisites?: { check: string; label: string; fix_route?: string }[];
  quota?: {
    trigger: string;
    basis?: string;
    runs_min: number;
    runs_max: number;
    open_ended?: boolean;
    /** The trigger kinds the row covers (UX r1 B1 on #10112); absent on a version written before. */
    kinds?: ("schedule" | "event" | "manual")[];
    /** runs_max is the count observed over the last 30 days. */
    observed?: boolean;
  }[];
  /** Computed from the configuration (`fleet_doc_generation.automation_facts`), never written by the AI. */
  automations?: { name: string; member?: string; trigger?: string; schedule?: string; event?: string; enabled?: boolean }[];
  /** What each member uses (`fleet_doc_generation.tool_use_facts`): integration provider keys and names. */
  integrations?: { member: string; integrations?: string[]; mcp_servers?: string[]; tool_groups?: string[]; skills?: string[] }[];
}

/** A derived prerequisite's state (`fleet_about.MET`...): `to_confirm` is a documented line no system can read. */
type FleetPrerequisiteLineState = "met" | "not_met" | "unreadable" | "to_confirm";

/** One line of the checklist derived from the configuration (`FleetLivePrerequisite`, #10258). */
export interface FleetLivePrerequisite {
  check: string;
  kind: string;
  label: string;
  state: FleetPrerequisiteLineState;
  /** Why it is not met, in plain words; empty when met. */
  reason: string;
  /** The home members that need it (agent ids). */
  member_ids: string[];
  /** A real app path that fixes it, or empty. */
  fix_route: string;
  /** The integration provider key (the logo), empty otherwise. */
  provider?: string;
  /** The reason as a code (`fleet_about.REASON_CODES`) the page translates, and its values (`date`, `detail`). */
  reason_code?: string;
  reason_params?: Record<string, string>;
  /** The label as a catalog key when the platform wrote it, and its values; empty for a name. */
  label_key?: string;
  label_params?: Record<string, string>;
  /** Whether the reader may open `fix_route` (an administrator's page is an admin's alone). */
  can_fix?: boolean;
  /** A `manual` line a person confirmed (#10469): their display name and the ISO time; empty otherwise. */
  confirmed_by?: string;
  confirmed_at?: string;
  /** Whether the reader may confirm or undo this `manual` line (a fleet manager). */
  can_confirm?: boolean;
}

/** One live line of the prerequisite checklist (`FleetPrerequisiteState`). */
interface FleetPrerequisiteState {
  check: string;
  label: string;
  /** An app route that fixes it, or empty. */
  fix_route: string;
  /** `null` for a `manual` check, never evaluated. */
  met: boolean | null;
}

/**
 * A fleet's documentation (#9174), built-in or custom. Read only, starts no
 * run; a refusal or a missing fleet is rendered by the About view itself.
 */
export function useFleetDocumentation(fleetId: string | null | undefined, enabled = true) {
  return useQuery<FleetDocumentation>({
    queryKey: [...KEY, "documentation", fleetId],
    queryFn: () => api.get(`/fleets/${fleetId}/documentation`),
    enabled: !!fleetId && enabled,
    ...FLEET_QUERY_POLICY,
    throwOnError: false,
    // Polls only while a generation runs on the server (#10817): the page follows it from any mount.
    refetchInterval: (query) => (query.state.data?.generating ? DOC_GENERATING_REFRESH_MS : false),
  });
}

/**
 * Once a generation the page followed ends (`generating` true, then false),
 * refresh the documentation's other reads, the version history included, so
 * the new version shows without a reload (#10817).
 */
export function useFollowFleetDocGeneration(fleetId: string, generating: boolean) {
  const qc = useQueryClient();
  const wasGenerating = useRef(generating);
  useEffect(() => {
    if (wasGenerating.current && !generating) {
      void qc.invalidateQueries({ queryKey: [...KEY, "documentation", fleetId] });
    }
    wasGenerating.current = generating;
  }, [fleetId, generating, qc]);
}

/**
 * `POST /fleets/{id}/documentation/regenerate` (#9768 S2): generate the fleet's
 * documentation with the caller's model and save it as the next version. The
 * one write path (no hand editing); user-initiated, counted like a chat. On a
 * built-in fleet the result is a local version (`local`).
 */
export function useRegenerateFleetDocumentation(fleetId: string) {
  const qc = useQueryClient();
  return useMutation<FleetDocumentation, Error, { section?: string; base_version?: number }>({
    mutationFn: (body) => api.post<FleetDocumentation>(`/fleets/${fleetId}/documentation/regenerate`, body),
    // The cached read says a generation runs from the click on (#10817): a page
    // opened again before the answer spins and polls the server, never idle.
    onMutate: () => {
      qc.setQueryData<FleetDocumentation>([...KEY, "documentation", fleetId], (current) =>
        current ? { ...current, generating: true, generation_failed: null } : current,
      );
    },
    onSuccess: (saved) => qc.setQueryData([...KEY, "documentation", fleetId], saved),
    onSettled: () => qc.invalidateQueries({ queryKey: [...KEY, "documentation", fleetId] }),
    meta: OWN_ERROR_TOAST,
  });
}

/** One version of a fleet's documentation in its history (`FleetDocumentationVersion`), without its text. */
export interface FleetDocumentationVersion {
  version: number;
  updated_at: string | null;
  /** A user id, `seed` (built-in) or `fork` (copied at the fork). */
  updated_by: string | null;
  /** The sections written in that version, in reading order. */
  section_keys: string[];
  /** The version the About page shows now. */
  current: boolean;
  /** Set when that version is a restore. */
  restored_from?: FleetDocumentationRestoredFrom | null;
  /** What the generator left out or derived for that version (#10246); empty for a reader who cannot manage the fleet. */
  notes?: FleetDocNote[];
  /** That version lost a visual block or misses a section: Regenerate is offered. */
  needs_regenerate?: boolean;
}

/** `GET /fleets/{id}/documentation/history` (#9768 S2): the current version then the kept ones, newest first. */
export function useFleetDocumentationHistory(fleetId: string, enabled = true) {
  return useQuery<{ versions: FleetDocumentationVersion[] }>({
    queryKey: [...KEY, "documentation", fleetId, "history"],
    queryFn: () => api.get(`/fleets/${fleetId}/documentation/history`),
    enabled: !!fleetId && enabled,
    ...FLEET_QUERY_POLICY,
    throwOnError: false,
  });
}

/** `POST /fleets/{id}/documentation/restore` (#9768 S2): a kept version back as the next version; no model call. */
export function useRestoreFleetDocumentationVersion(fleetId: string) {
  const qc = useQueryClient();
  return useMutation<FleetDocumentation, Error, { version: number; base_version?: number }>({
    mutationFn: (body) => api.post<FleetDocumentation>(`/fleets/${fleetId}/documentation/restore`, body),
    onSuccess: (saved) => qc.setQueryData([...KEY, "documentation", fleetId], saved),
    onSettled: () => qc.invalidateQueries({ queryKey: [...KEY, "documentation", fleetId] }),
    meta: OWN_ERROR_TOAST,
  });
}

/** `POST /fleets/{id}/documentation/restore-shipped`: a built-in fleet's shipped documentation back; no model call. */
export function useRestoreShippedFleetDocumentation(fleetId: string) {
  const qc = useQueryClient();
  return useMutation<FleetDocumentation, Error, void>({
    mutationFn: () => api.post<FleetDocumentation>(`/fleets/${fleetId}/documentation/restore-shipped`, {}),
    onSuccess: (saved) => qc.setQueryData([...KEY, "documentation", fleetId], saved),
    onSettled: () => qc.invalidateQueries({ queryKey: [...KEY, "documentation", fleetId] }),
    meta: OWN_ERROR_TOAST,
  });
}

/**
 * `POST /fleets/{id}/documentation/prerequisites/confirm` (#10469): a fleet manager confirms a
 * `manual` prerequisite, or undoes it with `confirmed: false`; stored on the fleet, no model call.
 */
export function useConfirmFleetPrerequisite(fleetId: string) {
  const qc = useQueryClient();
  return useMutation<FleetDocumentation, Error, { check: string; confirmed: boolean }>({
    mutationFn: (body) => api.post<FleetDocumentation>(`/fleets/${fleetId}/documentation/prerequisites/confirm`, body),
    onSuccess: (saved) => qc.setQueryData([...KEY, "documentation", fleetId], saved),
    onSettled: () => qc.invalidateQueries({ queryKey: [...KEY, "documentation", fleetId] }),
    meta: OWN_ERROR_TOAST,
  });
}

/** `POST /fleets/{id}/documentation/findings/dismiss` (#10536): dismiss the ids given, or every visible one (`ids` null), with an optional reason. */
export function useDismissFleetDocFindings(fleetId: string) {
  const qc = useQueryClient();
  return useMutation<FleetDocumentation, Error, { ids: string[] | null; reason: string }>({
    mutationFn: (body) => api.post<FleetDocumentation>(`/fleets/${fleetId}/documentation/findings/dismiss`, body),
    onSuccess: (saved) => qc.setQueryData([...KEY, "documentation", fleetId], saved),
    onSettled: () => qc.invalidateQueries({ queryKey: [...KEY, "documentation", fleetId] }),
    meta: OWN_ERROR_TOAST,
  });
}

/**
 * `POST /fleets/{id}/documentation/findings/{finding}/apply` (#11948): apply a good-practice suggestion; no model call.
 * `agentId` is the suggestion's member (`agent_id`): its own query is refreshed, the flow builder's agent node reads it (#12033).
 */
export function useApplyFleetPracticeSuggestion(fleetId: string) {
  const qc = useQueryClient();
  return useMutation<FleetDocumentation, Error, { id: string; agentId?: string | null }>({
    mutationFn: ({ id }) => api.post<FleetDocumentation>(`/fleets/${fleetId}/documentation/findings/${encodeURIComponent(id)}/apply`, {}),
    onSuccess: (saved) => qc.setQueryData([...KEY, "documentation", fleetId], saved),
    onSettled: (_saved, _error, { agentId }) => {
      qc.invalidateQueries({ queryKey: [...KEY, "documentation", fleetId] });
      // The member's handovers changed: the agent graph and the agent pages read them.
      qc.invalidateQueries({ queryKey: ["agents"] });
      if (agentId) qc.invalidateQueries({ queryKey: ["agent", agentId] });
      qc.invalidateQueries({ queryKey: KEY });
      // The pickers' catalog lists agents too.
      syncFlowNodeCatalog(qc);
    },
    meta: OWN_ERROR_TOAST,
  });
}

/** `POST /fleets/{id}/documentation/findings/restore` (#10536): bring back the ids given, or every dismissed one (`ids` null); no model call. */
export function useRestoreFleetDocFindings(fleetId: string) {
  const qc = useQueryClient();
  return useMutation<FleetDocumentation, Error, { ids: string[] | null }>({
    mutationFn: (body) => api.post<FleetDocumentation>(`/fleets/${fleetId}/documentation/findings/restore`, body),
    onSuccess: (saved) => qc.setQueryData([...KEY, "documentation", fleetId], saved),
    onSettled: () => qc.invalidateQueries({ queryKey: [...KEY, "documentation", fleetId] }),
    meta: OWN_ERROR_TOAST,
  });
}

/**
 * `GET /fleets/{id}/documentation/datasheet.pdf`: the fleet's one-page A4
 * datasheet, rendered on the server from the stored documentation and live
 * state (#9768 S4). No model call, no quota; saved under the server's name.
 */
export function useExportFleetDatasheet(fleetId: string) {
  return useMutation<{ filename: string }, Error, void>({
    mutationFn: async () => {
      const { blob, filename } = await api.download(`/fleets/${fleetId}/documentation/datasheet.pdf`);
      const resolved = filename ?? "fleet-datasheet.pdf";
      saveBlobAs(blob, resolved);
      return { filename: resolved };
    },
    meta: OWN_ERROR_TOAST,
  });
}

/**
 * `POST /fleets/{id}/members/{member_id}/instruction-draft` (#9162 R5a): an AI
 * draft of a connected member's collaboration instruction, nothing saved. It is
 * the `generate` callback of the instruction editor's `PromptGenerator`; the
 * editor reports a refusal itself.
 */
export function useDraftFleetMemberInstruction(fleetId: string) {
  return useMutation<{ text: string; truncated: boolean }, Error, { memberId: string; brief: string; current_text: string | null }>({
    mutationFn: ({ memberId, brief, current_text }) =>
      api.post<{ text: string; truncated: boolean }>(`/fleets/${fleetId}/members/${memberId}/instruction-draft`, { brief, current_text }),
    meta: OWN_ERROR_TOAST,
  });
}

export type FleetContextInclusion = "none" | "summary" | "full";

/** A connected agent of the "New fleet" dialog (`FleetMemberCreate`, role connected). */
interface NewFleetConnected {
  agent_id: string;
  collaboration_instruction: string;
  context_inclusion: FleetContextInclusion;
}

/** What the "New fleet" dialog asks for. */
export interface NewFleet {
  name: string;
  description: string;
  /** An existing workspace; `null` creates one named after the fleet. */
  workspace_id: string | null;
  orchestrator_agent_id: string | null;
  member_agent_ids: string[];
  connected: NewFleetConnected[];
  /** Home members other fleets may reach; the orchestrator always is. */
  exposed_agent_ids: string[];
}

/** A creation that failed after the fleet existed, and whose undo failed too. */
export class FleetLeftBehindError extends Error {
  readonly fleet: Fleet;

  constructor(fleet: Fleet, cause: unknown) {
    super(cause instanceof Error ? cause.message : String(cause));
    this.name = "FleetLeftBehindError";
    this.fleet = fleet;
  }
}

/**
 * Every fleet write's caller reports its own refusal (a toast naming the
 * action, the dialog's or the settings panel's error line), so the global
 * MutationCache "Error" toast (`main.tsx`) stays out of it: one message per
 * refusal, never two.
 */
const OWN_ERROR_TOAST = { suppressGlobalErrorToast: true } as const;

/**
 * Create a fleet with its members through the existing routes: the fleet, its
 * home members (orchestrator first), the orchestrator, the connected agents,
 * then the exposure. A refusal after the fleet exists deletes it again, so the
 * dialog never leaves a half-made fleet behind (a workspace created with it is
 * kept, as a fleet delete keeps it); the error names the step that refused.
 */
export function useCreateFleet() {
  const qc = useQueryClient();
  return useMutation<Fleet, Error, NewFleet>({
    mutationFn: async (input) => {
      const fleet = await api.post<Fleet>("/fleets", {
        name: input.name,
        description: input.description,
        ...(input.workspace_id ? { workspace_id: input.workspace_id } : {}),
      });
      try {
        const members = input.orchestrator_agent_id
          ? [input.orchestrator_agent_id, ...input.member_agent_ids.filter((id) => id !== input.orchestrator_agent_id)]
          : input.member_agent_ids;
        for (const agentId of members) {
          await api.post(`/fleets/${fleet.id}/members`, { role: "member", agent_id: agentId });
        }
        if (input.orchestrator_agent_id) {
          await api.put(`/fleets/${fleet.id}`, { orchestrator_agent_id: input.orchestrator_agent_id });
        }
        for (const link of input.connected) {
          await api.post(`/fleets/${fleet.id}/members`, { role: "connected", ...link });
        }
        if (input.exposed_agent_ids.length > 0) {
          await api.put(`/fleets/${fleet.id}/exposure`, { agent_ids: input.exposed_agent_ids });
        }
      } catch (error) {
        // Undo the fleet; a failed undo is never silent: the error names the
        // fleet left behind so the user removes it before trying again.
        const undone = await api
          .delete(`/fleets/${fleet.id}`)
          .then(() => true)
          .catch(() => false);
        if (!undone) throw new FleetLeftBehindError(fleet, error);
        throw error;
      }
      return fleet;
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: KEY });
      qc.invalidateQueries({ queryKey: ["agents"] });
      // The chosen (or created) workspace now hosts the fleet: its
      // card, page and actions menu read hosted_fleets from the workspaces cache.
      qc.invalidateQueries({ queryKey: ["workspaces"] });
      // The agents' home fleet changed: the pickers' catalog lists agents too.
      syncFlowNodeCatalog(qc);
    },
    meta: OWN_ERROR_TOAST,
  });
}

// ── Agent page: where one agent sits ──

/** `AgentHomeFleet`: the agent's home fleet, as its agent page shows it. */
export interface AgentHomeFleet {
  fleet_id: string;
  fleet_name: string;
  member_id: string;
  /** `null`: top level, reports to the orchestrator. */
  parent_member_id: string | null;
  /** Empty when the caller cannot see the parent agent. */
  parent_name: string;
  role_label: string;
  is_orchestrator: boolean;
  /** The fleet names an orchestrator (a top-level member reports to it). */
  has_orchestrator: boolean;
  is_exposed: boolean;
  is_archived: boolean;
  can_manage: boolean;
}

/** `AgentConnectedFleet`: a fleet that connects the agent without being its home. */
export interface AgentConnectedFleet {
  fleet_id: string;
  fleet_name: string;
  member_id: string;
  collaboration_instruction: string;
  context_inclusion: FleetContextInclusion;
  is_archived: boolean;
  can_manage: boolean;
}

/** `GET /fleets/by-agent/{agent_id}` (`AgentFleetsResponse`). */
export interface AgentFleets {
  agent_id: string;
  home: AgentHomeFleet | null;
  /** A home fleet exists but the caller cannot see it (never named). */
  home_hidden: boolean;
  connected_to: AgentConnectedFleet[];
}

/**
 * The agent page's Fleet card read. Optional like the map's fleets layer: a
 * refusal (no licence) stays on the query and the card is not drawn, never the
 * page's ErrorBoundary. Under the fleets key, so every fleet write refreshes it.
 */
export function useAgentFleets(agentId: string | null | undefined) {
  return useQuery<AgentFleets>({
    queryKey: [...KEY, "by-agent", agentId],
    queryFn: () => api.get(`/fleets/by-agent/${agentId}`),
    enabled: !!agentId,
    retry: false,
    throwOnError: false,
  });
}

// ── Edition: the fleet page writes through these hooks ──

/** A row of `GET /fleets/{id}/members` (`FleetMemberResponse`). */
export interface FleetMember {
  id: string;
  fleet_id: string;
  role: "member" | "connected";
  agent_id: string | null;
  name: string;
  target_fleet_id: string | null;
  parent_member_id: string | null;
  is_orchestrator: boolean;
  /** Reachable from other fleets: its own flag, or the orchestrator (always exposed). */
  is_exposed: boolean;
  /** The stored flag alone, without the orchestrator's implicit exposure: what an edit starts from. */
  exposed_explicitly: boolean;
  /** What a home member does in the fleet; a label only. */
  role_label: string;
  /** Effective values: the row's own, else the fleet default. */
  collaboration_instruction: string;
  context_inclusion: FleetContextInclusion;
  /** Effective organization scope: the row's own, else the fleet's. */
  organization_scope?: FleetOrganizationScope;
  /** The settings the row sets itself; the others inherit the fleet default. */
  overridden?: FleetMemberSetting[];
  created_at: string;
}

export function useFleetMembers(fleetId: string | null | undefined) {
  return useQuery<FleetMember[]>({
    queryKey: [...KEY, "members", fleetId],
    queryFn: () => api.get(`/fleets/${fleetId}/members`),
    enabled: !!fleetId,
    ...FLEET_QUERY_POLICY,
  });
}

/** The fields of `PUT /fleets/{id}` the settings panel writes (`FleetUpdate`). */
export interface FleetSettingsUpdate {
  name?: string;
  description?: string;
  icon?: string;
  color?: string;
  workspace_id?: string;
  orchestrator_agent_id?: string | null;
  is_archived?: boolean;
  is_company_managed?: boolean;
  /** Administrator only (#11624). */
  hide_activity_from_non_admins?: boolean;
  shared_group_ids?: string[];
  shared_group_permissions?: Record<string, string>;
  shared_user_ids?: string[];
  shared_user_permissions?: Record<string, string>;
  access_restricted?: boolean;
  /** `null` clears the fleet's budget default. */
  budget_config?: AgentBudgetConfig | null;
  /** `null` clears the fleet's iteration default. */
  max_iterations?: number | null;
  /** Raising it is administrator only (403 otherwise); lowering it is open (#9769). */
  autonomy_level?: FleetAutonomyLevel;
  /** The fields given replace the fleet's member defaults. */
  member_defaults?: { [K in FleetMemberSetting]?: FleetMemberDefaults[K] | null };
}

/**
 * Refresh every read a fleet write can change: the fleet, its graph, members,
 * health, the agents' home fleet, and the workspaces, whose cards and actions
 * menu read the fleets they host: a move, archive or delete changes it.
 */
function useInvalidateFleet() {
  const qc = useQueryClient();
  return () => {
    qc.invalidateQueries({ queryKey: KEY });
    qc.invalidateQueries({ queryKey: ["agents"] });
    qc.invalidateQueries({ queryKey: ["workspaces"] });
    syncFlowNodeCatalog(qc);
  };
}

/**
 * Refresh one fleet's graph, members and health after a write outside the
 * fleet API that they draw, such as a handover edited from the fleet graph
 * (an agent update): the graph's edges and crossing flags come from
 * the members' handovers.
 */
export function useRefreshFleetReads(fleetId: string | null | undefined) {
  const qc = useQueryClient();
  return useCallback(() => {
    if (!fleetId) return;
    for (const read of ["graph", "members", "health"]) {
      qc.invalidateQueries({ queryKey: [...KEY, read, fleetId] });
    }
  }, [qc, fleetId]);
}

export function useUpdateFleet(fleetId: string) {
  const invalidate = useInvalidateFleet();
  return useMutation<Fleet, Error, FleetSettingsUpdate>({
    mutationFn: (body) => api.put<Fleet>(`/fleets/${fleetId}`, body),
    onSettled: invalidate,
    meta: OWN_ERROR_TOAST,
  });
}

/**
 * Set the fleet's autonomy level (`PUT /fleets/{id}` `autonomy_level`, #9769 S3).
 * Re-reads the fleet and its documentation, whose catalog says what waits.
 */
export function useSetFleetAutonomy(fleetId: string) {
  const invalidate = useInvalidateFleet();
  const qc = useQueryClient();
  return useMutation<Fleet, Error, FleetAutonomyLevel>({
    mutationFn: (level) => api.put<Fleet>(`/fleets/${fleetId}`, { autonomy_level: level }),
    onSettled: () => {
      invalidate();
      qc.invalidateQueries({ queryKey: [...KEY, "documentation", fleetId] });
    },
    meta: OWN_ERROR_TOAST,
  });
}

/**
 * Apply the level and the per-action pins in one call (`PUT /fleets/{id}/autonomy`,
 * #10153 C3; built-in fleets take pins since #916, CTEM-34). The pins are the
 * whole map: a gate left out follows the level. Re-reads the documentation,
 * whose `autonomy_catalog` the strip and the matrix draw.
 */
export function useSetFleetGatePins(fleetId: string) {
  const invalidate = useInvalidateFleet();
  const qc = useQueryClient();
  return useMutation<unknown, Error, { level: FleetAutonomyLevel; pins: Record<string, FleetGatePin> }>({
    mutationFn: (body) => api.put(`/fleets/${fleetId}/autonomy`, body),
    onSettled: () => {
      invalidate();
      qc.invalidateQueries({ queryKey: [...KEY, "documentation", fleetId] });
      qc.invalidateQueries({ queryKey: [...KEY, "approval-gates", fleetId] });
    },
    meta: OWN_ERROR_TOAST,
  });
}

/**
 * A fleet's approval gate catalog and the gates proposed from its configuration
 * (`GET /fleets/{id}/approval-gates`, #10014). Read only: no model call, no quota, no run.
 */
export function useFleetApprovalGates(fleetId: string | null | undefined, enabled = true) {
  return useQuery<FleetApprovalGates>({
    queryKey: [...KEY, "approval-gates", fleetId],
    queryFn: () => api.get(`/fleets/${fleetId}/approval-gates`),
    enabled: !!fleetId && enabled,
    ...FLEET_QUERY_POLICY,
    throwOnError: false,
  });
}

/**
 * Move where the fleet's instructions put it (`PUT /fleets/{id}/instructions-position`, #10168):
 * never a platform gate; audited; toward more autonomy is administrator only. `announce: false` records it
 * without a line in the members' run prompt (#10255).
 */
export function useSetFleetInstructionsPosition(fleetId: string) {
  const qc = useQueryClient();
  return useMutation<FleetApprovalGates, Error, { position: FleetInstructionsPosition; announce: boolean }>({
    mutationFn: ({ position, announce }) =>
      api.put<FleetApprovalGates>(`/fleets/${fleetId}/instructions-position`, { position, announce }),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: [...KEY, "approval-gates", fleetId] });
    },
    meta: OWN_ERROR_TOAST,
  });
}

/**
 * Store a fleet's approval gate catalog (`PUT /fleets/{id}/approval-gates`, #10014).
 * Re-reads the documentation, whose `autonomy_catalog` the strip draws.
 */
export function useSetFleetApprovalGates(fleetId: string) {
  const invalidate = useInvalidateFleet();
  const qc = useQueryClient();
  return useMutation<FleetApprovalGates, Error, FleetApprovalGate[]>({
    mutationFn: (gates) => api.put<FleetApprovalGates>(`/fleets/${fleetId}/approval-gates`, { gates }),
    onSettled: () => {
      invalidate();
      qc.invalidateQueries({ queryKey: [...KEY, "documentation", fleetId] });
      qc.invalidateQueries({ queryKey: [...KEY, "approval-gates", fleetId] });
    },
    meta: OWN_ERROR_TOAST,
  });
}

/**
 * Pause all (`POST /fleets/{id}/pause`) or Resume (`POST /fleets/{id}/resume`)
 * the fleet's automations (#9769 S2): Pause all switches off every automation
 * that is on, Resume switches back on exactly those. Runs in flight continue.
 */
export function useFleetControl(fleetId: string, action: "pause" | "resume") {
  const invalidate = useInvalidateFleet();
  const qc = useQueryClient();
  return useMutation<FleetControlResult, Error, void>({
    mutationFn: () => api.post<FleetControlResult>(`/fleets/${fleetId}/${action}`, {}),
    onSettled: () => {
      invalidate();
      // The Schedule view and the assignment pages read the switched automations.
      qc.invalidateQueries({ queryKey: ["assignments"] });
      // The flow node catalog lists the assignments whose state just changed.
      syncFlowNodeCatalog(qc);
    },
    meta: OWN_ERROR_TOAST,
  });
}

/** One automation of the fleet that is off (`FleetEnableItemResponse`): switched on when selected. */
export interface FleetEnableItem {
  id: string;
  name: string;
  /** Module providers it acts through (`opencti`, `openaev`, `opencrq`). */
  providers: string[];
  /** Of `providers`, the ones the run-as user has not connected. */
  missing: string[];
  /** Part of the call (every automation when the request picked none). */
  selected: boolean;
  /** Runs a day its schedule starts (0 for a trigger that is not a schedule, #1538). */
  runs_per_day?: number;
  /** It runs once per event: its daily count is not known ahead. */
  event_driven?: boolean;
}

/** The expected load of the automations the plan picks (`FleetEnableEstimateResponse`, #1538): an order of magnitude. */
export interface FleetEnableEstimate {
  scheduled_runs_per_day: number;
  event_automations: number;
  /** Mean cost of one run of the picked automations over `cost_days`; null when none ran. */
  avg_cost_per_run: number | null;
  cost_per_day: number | null;
  cost_days: number;
}

/** The plan (`dry_run`) or the outcome of Enable fleet (`FleetEnableResponse`, #9769 S4b). */
export interface FleetEnableResult {
  fleet_id: string;
  dry_run: boolean;
  run_as_user_id: string;
  /** The automations switched on (or, on a dry run, that would be). */
  changed: number;
  /** False when nothing is off or a module is missing: Confirm stays disabled. */
  ready: boolean;
  missing: string[];
  items: FleetEnableItem[];
  automations: FleetAutomations;
  /** On a dry run: the expected runs a day and cost of the picked automations (#1538). */
  estimate?: FleetEnableEstimate | null;
}

/**
 * What Enable fleet would switch on, read while its setup dialog is open
 * (`POST /fleets/{id}/enable` with `dry_run`: nothing is written). `runAs`
 * `null` is the caller. Not cached: every opening and every run-as change
 * reads it again.
 */
export function useFleetEnablePlan(fleetId: string, runAs: string | null, open: boolean) {
  return useQuery<FleetEnableResult>({
    queryKey: [...KEY, "enable-plan", fleetId, runAs],
    queryFn: () => api.post<FleetEnableResult>(`/fleets/${fleetId}/enable`, { run_as_user_id: runAs, dry_run: true }),
    enabled: open,
    gcTime: 0,
    staleTime: 0,
    retry: false,
    throwOnError: false,
  });
}

/** What Enable fleet switches on: `runAs` null is the caller; `assignmentIds` absent picks every automation that is off. */
export interface FleetEnableRequest {
  runAs: string | null;
  assignmentIds?: string[];
}

/**
 * Enable fleet (`POST /fleets/{id}/enable`, #9769 S4b): the automations of
 * the fleet that are off are switched on, every one or the picked
 * `assignmentIds` (partial enable, #9173 S5), the seeded ones as
 * `run_as_user_id` (the caller when null). All or nothing over the pick.
 */
export function useFleetEnable(fleetId: string) {
  const invalidate = useInvalidateFleet();
  const qc = useQueryClient();
  return useMutation<FleetEnableResult, Error, FleetEnableRequest>({
    mutationFn: ({ runAs, assignmentIds }) =>
      api.post<FleetEnableResult>(`/fleets/${fleetId}/enable`, {
        run_as_user_id: runAs,
        dry_run: false,
        ...(assignmentIds ? { assignment_ids: assignmentIds } : {}),
      }),
    onSettled: () => {
      invalidate();
      // The Schedule view and the assignment pages read the switched automations.
      qc.invalidateQueries({ queryKey: ["assignments"] });
      // The flow node catalog lists the assignments whose state just changed.
      syncFlowNodeCatalog(qc);
    },
    meta: OWN_ERROR_TOAST,
  });
}

/** `POST /fleets/bulk` actions (#9769 S3). */
type FleetBulkActionName = "pause" | "resume" | "set_autonomy";

/** `POST /fleets/bulk` body (`FleetBulkAction`): the selection, or `all_fleets` (administrators, pause only). */
export interface FleetBulkRequest {
  action: FleetBulkActionName;
  fleet_ids?: string[];
  all_fleets?: boolean;
  autonomy_level?: FleetAutonomyLevel;
  /** Count what the call would change, write nothing (the confirmation dialog). */
  dry_run?: boolean;
}

/** What a bulk call did, or would do, on one fleet (`FleetBulkOutcome`). */
interface FleetBulkOutcome {
  fleet_id: string;
  name: string;
  /** False when the fleet was refused; `detail` says why. */
  ok: boolean;
  /** Automations switched (pause, resume), or 1 when the level changed. */
  changed: number;
  detail: string;
}

/** `POST /fleets/bulk` answer (`FleetBulkResponse`). */
export interface FleetBulkResult {
  action: FleetBulkActionName;
  dry_run: boolean;
  changed: number;
  refused: number;
  fleets: FleetBulkOutcome[];
}

/**
 * What a bulk call would change, read while its confirmation dialog is open
 * (`dry_run`: nothing is written). Not cached: every opening reads it again.
 */
export function useFleetBulkPreview(request: FleetBulkRequest | null) {
  return useQuery<FleetBulkResult>({
    queryKey: [...KEY, "bulk-preview", request],
    queryFn: () => api.post<FleetBulkResult>("/fleets/bulk", { ...request, dry_run: true }),
    enabled: request !== null,
    gcTime: 0,
    staleTime: 0,
    retry: false,
  });
}

/**
 * Pause all, Resume or set the autonomy level on several fleets at once, or
 * the administrators' switch that pauses every fleet automation
 * (`POST /fleets/bulk`, #9769 S3). Each fleet follows its single-fleet rule;
 * a refused fleet is reported in the answer, the others still apply.
 */
export function useFleetBulkAction() {
  const invalidate = useInvalidateFleet();
  const qc = useQueryClient();
  return useMutation<FleetBulkResult, Error, FleetBulkRequest>({
    mutationFn: (body) => api.post<FleetBulkResult>("/fleets/bulk", { ...body, dry_run: false }),
    onSettled: () => {
      invalidate();
      // The Schedule views and the assignment pages read the switched automations.
      qc.invalidateQueries({ queryKey: ["assignments"] });
      // The flow node catalog lists the assignments whose state just changed.
      syncFlowNodeCatalog(qc);
    },
    meta: OWN_ERROR_TOAST,
  });
}

export function useSetFleetExposure(fleetId: string) {
  const invalidate = useInvalidateFleet();
  return useMutation<string[], Error, string[]>({
    mutationFn: (agentIds) => api.put<string[]>(`/fleets/${fleetId}/exposure`, { agent_ids: agentIds }),
    onSettled: invalidate,
    meta: OWN_ERROR_TOAST,
  });
}

/**
 * Fork a fleet, a built-in one included, into an editable personal copy. One
 * server call: the copy, its members and its switched-off assignments are
 * written in one transaction, and the fork starts no run.
 */
export function useForkFleet(fleetId: string) {
  const invalidate = useInvalidateFleet();
  return useMutation<FleetForkResult, Error, FleetForkRequest>({
    mutationFn: (body) => api.post<FleetForkResult>(`/fleets/${fleetId}/fork`, body),
    onSettled: invalidate,
    meta: OWN_ERROR_TOAST,
  });
}

/** Delete a fleet: its members go back to the playground, no agent is deleted. */
export function useDeleteFleet() {
  const invalidate = useInvalidateFleet();
  return useMutation<void, Error, string>({
    mutationFn: (fleetId) => api.delete(`/fleets/${fleetId}`),
    onSettled: invalidate,
    meta: OWN_ERROR_TOAST,
  });
}

/** A row to add: a home member, or a connected agent or fleet (`FleetMemberCreate`). */
type FleetMemberAdd =
  | { role: "member"; agent_id: string; parent_member_id?: string | null }
  | {
      role: "connected";
      agent_id?: string;
      target_fleet_id?: string;
      collaboration_instruction: string;
      context_inclusion: FleetContextInclusion;
    };

export function useAddFleetMember(fleetId: string) {
  const invalidate = useInvalidateFleet();
  return useMutation<FleetMember, Error, FleetMemberAdd>({
    mutationFn: (body) => api.post<FleetMember>(`/fleets/${fleetId}/members`, body),
    onSettled: invalidate,
    meta: OWN_ERROR_TOAST,
  });
}

/** The fields of `PATCH /fleets/{id}/members/{member_id}` (`FleetMemberUpdate`). */
interface FleetMemberPatch {
  parent_member_id?: string | null;
  is_exposed?: boolean;
  role_label?: string;
  /** `null` resets the field: the row inherits the fleet default. */
  collaboration_instruction?: string | null;
  context_inclusion?: FleetContextInclusion | null;
}

export function useUpdateFleetMember(fleetId: string) {
  const invalidate = useInvalidateFleet();
  return useMutation<FleetMember, Error, { memberId: string; patch: FleetMemberPatch }>({
    mutationFn: ({ memberId, patch }) => api.patch<FleetMember>(`/fleets/${fleetId}/members/${memberId}`, patch),
    onSettled: invalidate,
    meta: OWN_ERROR_TOAST,
  });
}

/** The fleet choice of a capability: exactly these integrations on every member slot of that type (#1201). */
export interface FleetCapabilityWrite {
  capability: string;
  /** Empty leaves the built-in option alone, when the capability has one. */
  integrationIds: string[];
  /** Also write the members whose slot is custom; otherwise they keep their override. */
  overrideCustom: boolean;
}

/**
 * Write the fleet choice of a capability on every member slot of that type
 * (`PUT /fleets/{id}/capabilities/{capability}`, #1201). The caller says the refusal.
 */
export function useSetFleetCapability(fleetId: string) {
  const invalidate = useInvalidateFleet();
  return useMutation<FleetCapabilitySlots, Error, FleetCapabilityWrite>({
    mutationFn: ({ capability, integrationIds, overrideCustom }) =>
      api.put<FleetCapabilitySlots>(`/fleets/${fleetId}/capabilities/${encodeURIComponent(capability)}`, {
        integration_ids: integrationIds,
        override_custom: overrideCustom,
      }),
    onSettled: invalidate,
    meta: OWN_ERROR_TOAST,
  });
}

/** One member's exact slot: an override, or its reset to the fleet choice (#1201). */
export interface FleetMemberSlotWrite {
  memberId: string;
  capability: string;
  integrationIds: string[];
}

/**
 * Set exactly these integrations on one member's slot
 * (`PUT /fleets/{id}/members/{member}/slots/{capability}`, #1201). The caller says the refusal.
 */
export function useSetFleetMemberSlot(fleetId: string) {
  const invalidate = useInvalidateFleet();
  return useMutation<FleetMemberSlots, Error, FleetMemberSlotWrite>({
    mutationFn: ({ memberId, capability, integrationIds }) =>
      api.put<FleetMemberSlots>(`/fleets/${fleetId}/members/${memberId}/slots/${encodeURIComponent(capability)}`, {
        integration_ids: integrationIds,
      }),
    onSettled: invalidate,
    meta: OWN_ERROR_TOAST,
  });
}

/** A bulk change: the member settings to write; `null` resets a field to the fleet default. */
export type FleetMembersBulkSet = { [K in FleetMemberSetting]?: FleetMemberDefaults[K] | null };

/**
 * Set the member settings of several rows at once (the organization scope on
 * any row, the conversation and the instruction on connected rows):
 * `POST /fleets/{id}/members/bulk`, one transaction, all or nothing. The
 * caller says the refusal itself (one toast with the reason).
 */
export function useBulkUpdateFleetMembers(fleetId: string) {
  const invalidate = useInvalidateFleet();
  return useMutation<FleetMember[], Error, { memberIds: string[]; set: FleetMembersBulkSet }>({
    mutationFn: ({ memberIds, set }) =>
      api.post<FleetMember[]>(`/fleets/${fleetId}/members/bulk`, { member_ids: memberIds, set }),
    onSettled: invalidate,
    meta: OWN_ERROR_TOAST,
  });
}

/**
 * A company preset of member settings (`FleetMemberPresetResponse`): a named
 * set of values for any of the three settings. A built-in (`builtin:<key>`)
 * lives in code and is read only; the others are managed by administrators.
 */
export interface FleetMemberPreset {
  id: string;
  name: string;
  values: FleetMemberDefaults;
  builtin: boolean;
}

/** `GET /fleet-member-presets`: the built-ins first, then the company's by name. */
export function useFleetMemberPresets(options: { enabled?: boolean } = {}) {
  return useQuery<FleetMemberPreset[]>({
    queryKey: [...KEY, "member-presets"],
    queryFn: () => api.get("/fleet-member-presets"),
    enabled: options.enabled ?? true,
    ...FLEET_QUERY_POLICY,
  });
}

/** The body of `POST /fleet-member-presets` and `PATCH /fleet-member-presets/{id}` (administrators). */
export interface FleetMemberPresetInput {
  name: string;
  /** The settings the preset writes; the server refuses a `null` (a preset never resets a field). */
  values: FleetMembersBulkSet;
}

function useInvalidatePresets() {
  const qc = useQueryClient();
  return () => qc.invalidateQueries({ queryKey: [...KEY, "member-presets"] });
}

/** Create a company preset (administrators). The caller says the refusal itself. */
export function useCreateFleetMemberPreset() {
  const invalidate = useInvalidatePresets();
  return useMutation<FleetMemberPreset, Error, FleetMemberPresetInput>({
    mutationFn: (input) => api.post<FleetMemberPreset>("/fleet-member-presets", input),
    onSettled: invalidate,
    meta: OWN_ERROR_TOAST,
  });
}

/** Rename a company preset or replace its values whole (administrators). */
export function useUpdateFleetMemberPreset() {
  const invalidate = useInvalidatePresets();
  return useMutation<FleetMemberPreset, Error, { id: string; input: FleetMemberPresetInput }>({
    mutationFn: ({ id, input }) => api.patch<FleetMemberPreset>(`/fleet-member-presets/${encodeURIComponent(id)}`, input),
    onSettled: invalidate,
    meta: OWN_ERROR_TOAST,
  });
}

/** Delete a company preset (administrators); fleets keep the values it wrote. */
export function useDeleteFleetMemberPreset() {
  const invalidate = useInvalidatePresets();
  return useMutation<void, Error, string>({
    mutationFn: (id) => api.delete(`/fleet-member-presets/${encodeURIComponent(id)}`),
    onSettled: invalidate,
    meta: OWN_ERROR_TOAST,
  });
}

export function useRemoveFleetMember(fleetId: string) {
  const invalidate = useInvalidateFleet();
  return useMutation<void, Error, string>({
    mutationFn: (memberId) => api.delete(`/fleets/${fleetId}/members/${memberId}`),
    onSettled: invalidate,
    meta: OWN_ERROR_TOAST,
  });
}

/**
 * Move a home member to another fleet: `POST /fleets/{id}/members/{member_id}/move`,
 * one transaction on the server. A refused join changes nothing: the
 * member stays where it was, parent, exposure, role label and orchestrator
 * included, and the refusal is the mutation's error.
 */
export function useMoveFleetMember(fleetId: string) {
  const invalidate = useInvalidateFleet();
  return useMutation<FleetMember, Error, { member: FleetMember; targetFleetId: string }>({
    mutationFn: ({ member, targetFleetId }) =>
      api.post<FleetMember>(`/fleets/${fleetId}/members/${member.id}/move`, { target_fleet_id: targetFleetId }),
    onSettled: invalidate,
    meta: OWN_ERROR_TOAST,
  });
}

/**
 * A fleet's health. With `optional`, the health only enriches another view (the
 * case page's empty states): any failure stays on the query, no retry, and the
 * caller renders without it instead of the page's ErrorBoundary.
 */
export function useFleetHealth(fleetId: string | null | undefined, options: { optional?: boolean } = {}) {
  return useQuery<FleetHealth>({
    // Its own cache entry when optional, so a tolerated error never reaches a strict read.
    queryKey: [...KEY, "health", fleetId, ...(options.optional ? ["optional"] : [])],
    queryFn: () => api.get(`/fleets/${fleetId}/health`),
    enabled: !!fleetId,
    ...FLEET_QUERY_POLICY,
    ...(options.optional ? { throwOnError: false, retry: false } : {}),
    refetchInterval: 30_000,
  });
}
