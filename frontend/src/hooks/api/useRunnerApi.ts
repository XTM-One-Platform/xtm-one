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

/** Runner hooks – native, pull-based OS-control daemons (the XTM One Desktop page).
 *
 * Runners are a first-class three-tier entity (own + group-shared +
 * company-managed), not an integration. These hooks cover fleet management
 * (list / update / revoke / delete), enrollment-key lifecycle, the per-runner
 * command audit trail, and HITL command approval. The daemon transport
 * endpoints (enroll / poll / result / WebSocket) are called by the runner
 * binary, never from the browser, so they have no hooks here.
 */

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useEntityListScope } from "@/hooks/useEntityListScope";
import { api, notFoundIsOrdinary, unavailableIsOrdinary } from "@/lib/api";
import { REFETCH_INTERVAL_STANDARD } from "@/lib/constants";
import { entityListKey, entityListPath } from "@/lib/entity-list-scope";
import { dropFromFlowNodeCatalog, syncFlowNodeCatalog } from "@/lib/flow-node-catalog";
import type {
  EntityListOptions,
  Runner,
  RunnerUpdate,
  RunnerCommand,
  RunnerJobsAnswer,
  RunnerEnrollmentKey,
  RunnerEnrollmentKeyCreated,
  RunnerEnrollmentKeyCreate,
  RunnerCapabilitiesInfo,
  RunnerDownloadManifest,
} from "@/types";

// --- Runners (fleet) ---
/** The runners list; `{ scope: "all" }` is every machine for an admin (detail pages). */
export function useRunners(options?: EntityListOptions & { enabled?: boolean }) {
  const scope = useEntityListScope(options);
  return useQuery<Runner[]>({
    queryKey: entityListKey(["runners"], scope),
    // A runner's online/offline flips on its heartbeat cadence; keep the fleet
    // list warm so status dots track reality without a manual refresh.
    queryFn: () => api.get(entityListPath("/runners", scope)),
    refetchInterval: REFETCH_INTERVAL_STANDARD,
    // Callers on pages where runners may be irrelevant (e.g. an assignment
    // whose agent has no computer_use tool) can gate the fetch.
    enabled: options?.enabled ?? true,
  });
}

export function useRunner(id: string | undefined, options?: { tolerateNotFound?: boolean }) {
  return useQuery<Runner>({
    queryKey: ["runner", id],
    queryFn: () => api.get(`/runners/${id}`),
    enabled: !!id,
    refetchInterval: REFETCH_INTERVAL_STANDARD,
    // The detail page answers a 404 with its own not-found state: a
    // machine deleted meanwhile, or an id typed into the URL, is an ordinary
    // answer there, not a page-level fault.
    ...(options?.tolerateNotFound ? notFoundIsOrdinary : {}),
  });
}

/**
 * The machine the XTM One Desktop shell says it embeds. The id comes
 * from the shell, not from anything this account chose: the runner may have
 * been deleted, its sharing withdrawn, or a DIFFERENT account may be signed
 * in to the app on that machine - all of which the platform answers with a
 * 404 / 403. Those are states the sidebar, the machine bar and the Desktop
 * page render ("unknown machine"), never faults: with the app-wide
 * `throwOnError`, the plain `useRunner` would send the whole shell to the
 * error boundary from the sidebar on every page.
 */
export function useDesktopRunner(id: string | null | undefined) {
  return useQuery<Runner>({
    queryKey: ["runner", id],
    queryFn: () => api.get(`/runners/${id}`),
    enabled: !!id,
    refetchInterval: REFETCH_INTERVAL_STANDARD,
    ...unavailableIsOrdinary,
  });
}

export function useRunnerCapabilities() {
  return useQuery<RunnerCapabilitiesInfo>({
    queryKey: ["runner-capabilities"],
    queryFn: () => api.get("/runners/capabilities"),
    // Static server vocabulary — fetch once and keep.
    staleTime: Infinity,
  });
}

/** Version-matched download links + whether downloads are configured on this
 *  deployment. The runner binary tracks the same manifest for self-update. */
export function useRunnerDownloadManifest() {
  return useQuery<RunnerDownloadManifest>({
    queryKey: ["runner-download-manifest"],
    queryFn: () => api.get("/runners/update-manifest"),
    staleTime: 60_000,
  });
}

export function useUpdateRunner() {
  const qc = useQueryClient();
  return useMutation<Runner, Error, { id: string; body: RunnerUpdate }>({
    mutationFn: ({ id, body }) => api.patch(`/runners/${id}`, body),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ["runners"] });
      qc.invalidateQueries({ queryKey: ["runner", data.id] });
      syncFlowNodeCatalog(qc);
    },
  });
}

export function useRevokeRunner() {
  const qc = useQueryClient();
  return useMutation<Runner, Error, string>({
    mutationFn: (id) => api.post(`/runners/${id}/revoke`),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ["runners"] });
      qc.invalidateQueries({ queryKey: ["runner", data.id] });
      syncFlowNodeCatalog(qc);
    },
  });
}

export function useDeleteRunner() {
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: (id) => api.delete(`/runners/${id}`),
    onSuccess: (_d, id) => {
      dropFromFlowNodeCatalog(qc, "runner", id);
      return qc.invalidateQueries({ queryKey: ["runners"] });
    },
  });
}

// Polled, and the list never shows a result, which may hold up to 16 MiB of
// JSON: the rows come back with `result: null`.
export function useRunnerCommands(id: string | undefined, limit = 50) {
  return useQuery<RunnerCommand[]>({
    queryKey: ["runner-commands", id, limit],
    queryFn: () => api.get(`/runners/${id}/commands?limit=${limit}&include_result=false`),
    enabled: !!id,
    refetchInterval: REFETCH_INTERVAL_STANDARD,
  });
}

// --- HITL command approval ---
// `remember` persists the decision as standing policy: approve+remember adds
// the exact command to the runner's shell allow-list (never queues again),
// reject+remember adds it to the block-list (refused outright from now on).
interface RunnerApprovalDecision {
  commandId: string;
  remember?: boolean;
}

export function useApproveRunnerCommand() {
  const qc = useQueryClient();
  return useMutation<
    { ok: boolean; status: string; remembered?: boolean },
    Error,
    RunnerApprovalDecision
  >({
    mutationFn: ({ commandId, remember }) =>
      api.post(`/runners/commands/${commandId}/approve`, { remember: !!remember }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["runner-commands"] });
      // A remembered decision mutated the runner's allow-list.
      qc.invalidateQueries({ queryKey: ["runners"] });
    },
  });
}

export function useRejectRunnerCommand() {
  const qc = useQueryClient();
  return useMutation<
    { ok: boolean; status: string; remembered?: boolean },
    Error,
    RunnerApprovalDecision
  >({
    mutationFn: ({ commandId, remember }) =>
      api.post(`/runners/commands/${commandId}/reject`, { remember: !!remember }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["runner-commands"] });
      qc.invalidateQueries({ queryKey: ["runners"] });
    },
  });
}

/** Stop a shell command running on its machine; the card settles from the tool's result. */
export function useStopRunnerCommand() {
  const qc = useQueryClient();
  return useMutation<{ ok: boolean; stopping: boolean; status: string }, Error, string>({
    mutationFn: (commandId) => api.post(`/runners/commands/${commandId}/stop`, {}),
    // A background job is stopped by the same route: its list reads it again.
    onSuccess: () => qc.invalidateQueries({ queryKey: ["runner-jobs"] }),
  });
}

/**
 * The background shell jobs a machine holds, asked to the machine itself.
 * Not polled: every read is a `shell_job` command in the machine's audit trail,
 * so it is read when the card opens, after a Stop, and on Refresh.
 */
export function useRunnerJobs(id: string | undefined) {
  return useQuery<RunnerJobsAnswer>({
    queryKey: ["runner-jobs", id],
    queryFn: () => api.get(`/runners/${id}/jobs`),
    enabled: !!id,
    refetchOnWindowFocus: false,
  });
}

// --- Enrollment keys ---
export function useRunnerEnrollmentKeys() {
  return useQuery<RunnerEnrollmentKey[]>({
    queryKey: ["runner-enrollment-keys"],
    queryFn: () => api.get("/runners/enrollment-keys"),
  });
}

export function useCreateRunnerEnrollmentKey() {
  const qc = useQueryClient();
  return useMutation<RunnerEnrollmentKeyCreated, Error, RunnerEnrollmentKeyCreate>({
    mutationFn: (body) => api.post("/runners/enrollment-keys", body),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ["runner-enrollment-keys"] }),
  });
}

export function useDeleteRunnerEnrollmentKey() {
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: (id) => api.delete(`/runners/enrollment-keys/${id}`),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ["runner-enrollment-keys"] }),
  });
}
