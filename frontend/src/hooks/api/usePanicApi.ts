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
 * Emergency stop ("panic button") hooks — `/api/v1/panic`.
 *
 * `self` halts everything that runs for the caller; `platform` (admins) halts
 * every user's activity and switches the platform AI off. The status query is
 * polled while signed in: the page-wide banner and the profile menu's "resume"
 * entries both read it.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { REFETCH_INTERVAL_STANDARD } from "@/lib/constants";

export type PanicScope = "self" | "platform";

interface EmergencyStopResult {
  scope: PanicScope;
  runs_cancelled: number;
  executions_cancelled: number;
  sub_agent_tasks_cancelled: number;
  chat_turns_cancelled: number;
  flow_runs_cancelled: number;
  runner_commands_cancelled: number;
  evaluation_runs_cancelled: number;
  autonomous_runs_cancelled: number;
  assignments_paused: number;
  ai_disabled: boolean;
  /** Runs admitted while the stop was in flight, cancelled by the post-commit
   *  sweep — already counted in `runs_cancelled`. */
  late_admissions_cancelled: number;
  /** `false` when cancelling the live work failed after the door was closed:
   *  the follow-up sweeps cancelled what they reach. */
  completed?: boolean;
  /** The `panic_stop` audit row, to annotate with the reason afterwards. */
  audit_id: string | null;
  stopped_at: string | null;
}

interface EmergencyStopReasonResult {
  audit_id: string;
  assignments_annotated: number;
}

interface EmergencyResumeResult {
  scope: PanicScope;
  assignments_resumed: number;
  ai_enabled: boolean;
}

interface EmergencyStopStatus {
  /** Assignments a `self` resume would re-enable. */
  self_paused_assignments: number;
  /** The viewer's own stop is in effect and still covers a paused assignment
   *  (another open stop may cover it too): its resume stays on offer. */
  self_stop_active?: boolean;
  /** Assignments a `platform` resume would re-enable. `null` for non-admins. */
  platform_paused_assignments: number | null;
  /** Whether the platform AI override is currently OFF. `null` for non-admins. */
  ai_disabled: boolean | null;
  /** A platform-wide stop is in effect (not yet lifted) — visible to everyone. */
  platform_stop_active: boolean;
  /** A stop is in effect for the viewer — drives the page-wide banner. */
  active: boolean;
  /** The most recent stop that concerns the viewer: who, when, why. */
  last_stop: { at: string | null; by: string | null; scope: PanicScope | null; reason: string | null } | null;
}

const EMERGENCY_STOP_STATUS_KEY = ["panic-status"] as const;

/** Polled while signed in: the banner on every page and the profile menu's
 *  "resume" entries both read it, so a stop pressed from another tab or by an
 *  administrator shows up here within one interval. */
export function useEmergencyStopStatus(enabled: boolean) {
  return useQuery<EmergencyStopStatus>({
    queryKey: EMERGENCY_STOP_STATUS_KEY,
    queryFn: () => api.get("/panic/status"),
    enabled,
    staleTime: 0,
    refetchInterval: REFETCH_INTERVAL_STANDARD,
  });
}

/** A stop or a resume changes the state of nearly every list in the app
 *  (runs, assignments, conversations, flows, platform config), so the whole
 *  cache is invalidated rather than an ever-growing enumeration of keys. */
function invalidateEverything(qc: ReturnType<typeof useQueryClient>) {
  void qc.invalidateQueries();
}

export function useEmergencyStop() {
  const qc = useQueryClient();
  return useMutation<EmergencyStopResult, Error, PanicScope>({
    mutationFn: (scope) => api.post("/panic/stop", { scope }),
    onSuccess: () => invalidateEverything(qc),
  });
}

/** Why the stop was pressed — asked right after the stop, never before it
 *  (an emergency does not wait for a form). Annotates the audit row and the
 *  assignments that stop paused. */
export function useRecordEmergencyStopReason() {
  const qc = useQueryClient();
  return useMutation<EmergencyStopReasonResult, Error, { auditId: string; reason: string }>({
    mutationFn: ({ auditId, reason }) => api.post(`/panic/stop/${auditId}/reason`, { reason }),
    // Annotating the stop rewrites the banner status AND every paused
    // assignment's details, so refresh the whole cache now rather than
    // leaving an open assignment page on the generic text until it polls.
    onSuccess: () => invalidateEverything(qc),
  });
}

/** A resume is not an emergency, so its reason is asked BEFORE acting and is
 *  mandatory: it lands on the `panic_resume` audit row and on every
 *  per-assignment `resume` row. */
export function useEmergencyResume() {
  const qc = useQueryClient();
  return useMutation<EmergencyResumeResult, Error, { scope: PanicScope; reason: string }>({
    mutationFn: ({ scope, reason }) => api.post("/panic/resume", { scope, reason }),
    onSuccess: () => invalidateEverything(qc),
  });
}

// ---------------------------------------------------------------------------
// Emergency stop API token (Settings > Access and security > Emergency stop, admins)
// ---------------------------------------------------------------------------

interface EmergencyTokenStatus {
  configured: boolean;
  prefix: string | null;
  created_at: string | null;
  created_by: string | null;
  last_used_at: string | null;
}

export interface EmergencyTokenCreated {
  /** Shown exactly once — only a bcrypt hash is kept server-side. */
  token: string;
  prefix: string;
  created_at: string;
  created_by: string;
}

const EMERGENCY_TOKEN_KEY = ["panic-token"] as const;

export function useEmergencyTokenStatus(enabled = true) {
  return useQuery<EmergencyTokenStatus>({
    queryKey: EMERGENCY_TOKEN_KEY,
    queryFn: () => api.get("/panic/token"),
    enabled,
  });
}

export function useGenerateEmergencyToken() {
  const qc = useQueryClient();
  return useMutation<EmergencyTokenCreated, Error, void>({
    mutationFn: () => api.post("/panic/token", {}),
    onSuccess: () => void qc.invalidateQueries({ queryKey: EMERGENCY_TOKEN_KEY }),
  });
}

export function useRevokeEmergencyToken() {
  const qc = useQueryClient();
  return useMutation<void, Error, void>({
    mutationFn: () => api.delete("/panic/token"),
    onSuccess: () => void qc.invalidateQueries({ queryKey: EMERGENCY_TOKEN_KEY }),
  });
}
