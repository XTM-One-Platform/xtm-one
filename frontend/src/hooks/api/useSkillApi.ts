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

/** Agent Skill CRUD + refresh + upload API hooks. */

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useEntityListScope } from "@/hooks/useEntityListScope";
import { API_BASE, api, filenameFromContentDisposition, getToken } from "@/lib/api";
import { entityListKey, entityListPath } from "@/lib/entity-list-scope";
import { dropFromFlowNodeCatalog, syncFlowNodeCatalog } from "@/lib/flow-node-catalog";
import { saveBlobAs } from "@/lib/utils";
import type {
  AgentSkill,
  BulkSkillCollisionPolicy,
  CreateAgentSkillRequest,
  EntityListOptions,
  SkillBulkImportResponse,
  SkillBulkPackageValidationResponse,
  SkillPackageBindings,
  SkillPackageImportResponse,
  SkillLicense,
  SkillTier3ToolLink,
  SkillPackageValidationResponse,
  UpdateAgentSkillRequest,
  VersionDiffResponse,
  VersionListResponse,
} from "@/types";

// An explicit VITE_API_URL (split dev setup) wins; otherwise the same-origin
// API under the URL path prefix the platform is served from.
const API_V1_BASE = import.meta.env.VITE_API_URL
  ? `${String(import.meta.env.VITE_API_URL).replace(/\/$/, "")}/api/v1`
  : API_BASE;

function sanitizeErrorMessage(value: unknown, fallback: string): string {
  if (typeof value !== "string") return fallback;
  const sanitized = value.replace(/[<>]/g, "").trim();
  if (!sanitized) return fallback;
  return sanitized.slice(0, 500);
}

function extractErrorMessages(value: unknown): string[] {
  if (typeof value === "string") {
    const message = sanitizeErrorMessage(value, "");
    return message ? [message] : [];
  }
  if (Array.isArray(value)) {
    return value.flatMap((entry) => {
      if (typeof entry === "string") return extractErrorMessages(entry);
      if (entry && typeof entry === "object" && "msg" in entry) {
        return extractErrorMessages((entry as { msg?: unknown }).msg);
      }
      return [];
    });
  }
  if (value && typeof value === "object") {
    const obj = value as { detail?: unknown; errors?: unknown; message?: unknown };
    return [
      ...extractErrorMessages(obj.detail),
      ...extractErrorMessages(obj.errors),
      ...extractErrorMessages(obj.message),
    ];
  }
  return [];
}

async function parseErrorResponse(resp: Response, fallback: string): Promise<never> {
  const payload = await resp.json().catch(() => ({}));
  const messages = extractErrorMessages(payload);
  const message = messages[0] ?? `${fallback} (HTTP ${resp.status})`;
  throw new Error(message);
}

async function authorizedApiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const token = getToken();
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  return fetch(`${API_V1_BASE}${path}`, { ...init, headers });
}

// --- Skills (top-level shared entities) ---

/** The skills list; `{ scope: "all" }` is every skill for an admin (detail pages). */
export function useSkills(options?: EntityListOptions & { enabled?: boolean }) {
  const scope = useEntityListScope(options);
  return useQuery<AgentSkill[]>({
    queryKey: entityListKey(["skills"], scope),
    queryFn: () => api.get(entityListPath("/skills", scope)),
    enabled: options?.enabled ?? true,
  });
}

export function useSkill(skillId: string | undefined) {
  return useQuery<AgentSkill>({
    queryKey: ["skill", skillId],
    queryFn: () => api.get(`/skills/${skillId}`),
    enabled: !!skillId,
  });
}

/** Per-skill map of the Custom Tools / KBs a packaged skill hands to its agents.
 *
 * Attaching such a skill to an agent attaches these entities too (and detaching
 * removes them) — see `backend/app/services/skill_package_bindings.py`. Only
 * skills that actually carry packaged resources are returned.
 */
export function useSkillPackageBindings() {
  return useQuery<SkillPackageBindings[]>({
    queryKey: ["skill-package-bindings"],
    queryFn: () => api.get("/skills/package-bindings"),
  });
}

export function useSkillTier3Tools(skillId: string | undefined) {
  return useQuery<SkillTier3ToolLink[]>({
    queryKey: ["skill-tier3-tools", skillId],
    queryFn: () => api.get(`/skills/${skillId}/tier3-tools`),
    enabled: !!skillId,
  });
}

/** The skill's licence: its notice and the text of its bundled licence file. */
export function useSkillLicense(skillId: string | undefined) {
  return useQuery<SkillLicense>({
    queryKey: ["skill-license", skillId],
    queryFn: () => api.get(`/skills/${skillId}/license`),
    enabled: !!skillId,
  });
}

export function useCreateSkill() {
  const qc = useQueryClient();
  return useMutation<AgentSkill, Error, CreateAgentSkillRequest>({
    mutationFn: (data) => api.post("/skills", data),
    onSuccess: () => {
      syncFlowNodeCatalog(qc);
      return qc.invalidateQueries({ queryKey: ["skills"] });
    },
  });
}

export function useUploadSkill() {
  const qc = useQueryClient();
  return useMutation<AgentSkill, Error, File>({
    mutationFn: async (file) => {
      const formData = new FormData();
      formData.append("file", file);
      const resp = await authorizedApiFetch("/skills/upload", {
        method: "POST",
        body: formData,
      });
      if (!resp.ok) {
        return parseErrorResponse(resp, "Upload failed");
      }
      return resp.json();
    },
    onSuccess: () => {
      syncFlowNodeCatalog(qc);
      return qc.invalidateQueries({ queryKey: ["skills"] });
    },
  });
}

export function useUpdateSkill() {
  const qc = useQueryClient();
  return useMutation<AgentSkill, Error, { id: string } & UpdateAgentSkillRequest>({
    mutationFn: ({ id, ...data }) => api.put(`/skills/${id}`, data),
    onSuccess: (_d, { id }) => {
      qc.invalidateQueries({ queryKey: ["skills"] });
      syncFlowNodeCatalog(qc);
      qc.invalidateQueries({ queryKey: ["skill", id] });
      qc.invalidateQueries({ queryKey: ["skill-license", id] });
      qc.invalidateQueries({ queryKey: ["skill-versions", id] });
      qc.invalidateQueries({ queryKey: ["skill-version-diff", id] });
    },
  });
}

export function useSkillVersions(
  skillId: string,
  params: { limit?: number; offset?: number } = {},
) {
  const query = new URLSearchParams();
  if (params.limit !== undefined) query.set("limit", String(params.limit));
  if (params.offset !== undefined) query.set("offset", String(params.offset));
  const qs = query.toString();
  return useQuery<VersionListResponse>({
    queryKey: ["skill-versions", skillId, params],
    queryFn: () => api.get(`/skills/${skillId}/versions${qs ? `?${qs}` : ""}`),
    enabled: !!skillId,
    placeholderData: (prev) => prev,
  });
}

export function useSkillVersionDiff(
  skillId: string,
  fromVersion: number | null,
  toVersion: number | null,
  enabled = true,
) {
  return useQuery<VersionDiffResponse>({
    queryKey: ["skill-version-diff", skillId, fromVersion, toVersion],
    queryFn: () => api.get(`/skills/${skillId}/versions/diff?from=${fromVersion}&to=${toVersion}`),
    enabled: !!skillId && fromVersion !== null && toVersion !== null && enabled,
  });
}

export function useRollbackSkillVersion() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      skillId,
      version,
      commit_message,
    }: {
      skillId: string;
      version: number;
      commit_message?: string;
    }) => api.post(`/skills/${skillId}/versions/${version}/rollback`, { commit_message }),
    onSuccess: (_d, { skillId }) => {
      qc.invalidateQueries({ queryKey: ["skills"] });
      syncFlowNodeCatalog(qc);
      qc.invalidateQueries({ queryKey: ["skill", skillId] });
      qc.invalidateQueries({ queryKey: ["skill-versions", skillId] });
      qc.invalidateQueries({ queryKey: ["skill-version-diff", skillId] });
    },
  });
}

export function useDeleteSkillVersion() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ skillId, version }: { skillId: string; version: number }) =>
      api.delete(`/skills/${skillId}/versions/${version}`),
    onSuccess: (_d, { skillId }) => {
      qc.invalidateQueries({ queryKey: ["skill-versions", skillId] });
      qc.invalidateQueries({ queryKey: ["skill-version-diff", skillId] });
    },
  });
}

export function useLockSkillVersion() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ skillId, version }: { skillId: string; version: number }) =>
      api.post(`/skills/${skillId}/versions/${version}/lock`, {}),
    onSuccess: (_d, { skillId }) => {
      qc.invalidateQueries({ queryKey: ["skill-versions", skillId] });
    },
  });
}

export function useUnlockSkillVersion() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ skillId, version }: { skillId: string; version: number }) =>
      api.post(`/skills/${skillId}/versions/${version}/unlock`, {}),
    onSuccess: (_d, { skillId }) => {
      qc.invalidateQueries({ queryKey: ["skill-versions", skillId] });
    },
  });
}

export function useDeleteSkill() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete(`/skills/${id}`),
    onSuccess: (_d, id) => {
      qc.invalidateQueries({ queryKey: ["skills"] });
      dropFromFlowNodeCatalog(qc, "skill", id);
      // Deleting a skill also deletes the tools/KBs its package produced.
      qc.invalidateQueries({ queryKey: ["skill-package-bindings"] });
    },
  });
}

/** `POST /skills/{id}/fork` body (`SkillFork`). */
export interface SkillForkRequest {
  /** Empty: the server names the copy `Copy of` the source's name. */
  name?: string | null;
  /** A fleet whose member agents bound to the source are re-bound to the copy. */
  repoint_fleet_id?: string | null;
}

/** `POST /skills/{id}/fork` answer (`SkillForkResponse`). */
export interface SkillForkResult {
  skill: AgentSkill;
  resources_copied: number;
  repointed_agent_ids: string[];
  warnings: string[];
}

/**
 * Fork a skill, a built-in one included, into an editable personal copy with
 * its own history from v1 (#9174). One server call; starts no run.
 */
export function useForkSkill(skillId: string) {
  const qc = useQueryClient();
  return useMutation<SkillForkResult, Error, SkillForkRequest>({
    mutationFn: (body) => api.post<SkillForkResult>(`/skills/${skillId}/fork`, body),
    onSuccess: (result) => {
      qc.invalidateQueries({ queryKey: ["skills"] });
      qc.invalidateQueries({ queryKey: ["skill-package-bindings"] });
      if (result.repointed_agent_ids.length > 0) qc.invalidateQueries({ queryKey: ["agents"] });
      // The new skill (and any repointed agent) is listed in the flow node catalog.
      syncFlowNodeCatalog(qc);
    },
    // The dialog shows the refusal under its fields: no second, global toast.
    meta: { suppressGlobalErrorToast: true },
  });
}

export function useRefreshSkill() {
  const qc = useQueryClient();
  return useMutation<AgentSkill, Error, string>({
    mutationFn: (id) => api.post(`/skills/${id}/refresh`),
    onSuccess: (_d, id) => {
      qc.invalidateQueries({ queryKey: ["skills"] });
      syncFlowNodeCatalog(qc);
      qc.invalidateQueries({ queryKey: ["skill", id] });
      qc.invalidateQueries({ queryKey: ["skill-versions", id] });
      qc.invalidateQueries({ queryKey: ["skill-version-diff", id] });
    },
  });
}

export function useValidateSkillPackage() {
  return useMutation<SkillPackageValidationResponse, Error, File>({
    mutationFn: async (file) => {
      const formData = new FormData();
      formData.append("file", file);
      const resp = await authorizedApiFetch("/skills/validate-package", {
        method: "POST",
        body: formData,
      });
      if (!resp.ok) {
        return parseErrorResponse(resp, "Validation failed");
      }
      return resp.json();
    },
  });
}

/**
 * Invalidate everything a package import can create or rewire.
 *
 * Importing a package does not only add a skill: `scripts/` become Custom
 * Tools, `references/` become a Knowledge Base with its documents, and agents
 * already using the skill get those bindings added. Invalidating only
 * `["skills"]` left those lists on their cached copy for the whole staleTime
 * window, so a freshly imported knowledge base looked missing.
 */
function invalidateAfterPackageImport(qc: ReturnType<typeof useQueryClient>) {
  qc.invalidateQueries({ queryKey: ["skills"] });
  // The import materializes ``scripts/`` into Custom Tools and ``references/``
  // into KBs, which changes what each skill carries.
  qc.invalidateQueries({ queryKey: ["skill-package-bindings"] });
  qc.invalidateQueries({ queryKey: ["knowledge-bases"] });
  qc.invalidateQueries({ queryKey: ["custom-tools"] });
  qc.invalidateQueries({ queryKey: ["agents"] });
  syncFlowNodeCatalog(qc);
}

export function useImportSkillPackage() {
  const qc = useQueryClient();
  return useMutation<
    SkillPackageImportResponse,
    Error,
    { file: File; onCollision?: "cancel" | "replace" | "as_new"; newName?: string }
  >({
    mutationFn: async ({ file, onCollision = "cancel", newName }) => {
      const formData = new FormData();
      formData.append("file", file);
      formData.append("on_collision", onCollision);
      if (newName) formData.append("new_name", newName);
      const resp = await authorizedApiFetch("/skills/import-package", {
        method: "POST",
        body: formData,
      });
      if (!resp.ok) {
        return parseErrorResponse(resp, "Import failed");
      }
      return resp.json();
    },
    onSuccess: () => invalidateAfterPackageImport(qc),
  });
}

/** Pre-flight a multi-skill archive: detected skills vs ignored files. */
export function useValidateSkillBulkPackage() {
  return useMutation<SkillBulkPackageValidationResponse, Error, File>({
    mutationFn: async (file) => {
      const formData = new FormData();
      formData.append("file", file);
      const resp = await authorizedApiFetch("/skills/validate-bulk-package", {
        method: "POST",
        body: formData,
      });
      if (!resp.ok) {
        return parseErrorResponse(resp, "Validation failed");
      }
      return resp.json();
    },
  });
}

/** Import the skills the user approved on the bulk review screen. */
export function useImportSkillBulkPackage() {
  const qc = useQueryClient();
  return useMutation<
    SkillBulkImportResponse,
    Error,
    {
      file: File;
      selectedPaths: string[];
      onCollision?: BulkSkillCollisionPolicy;
      collisionOverrides?: Record<string, BulkSkillCollisionPolicy>;
    }
  >({
    mutationFn: async ({ file, selectedPaths, onCollision = "skip", collisionOverrides }) => {
      const formData = new FormData();
      formData.append("file", file);
      formData.append("selected_paths", JSON.stringify(selectedPaths));
      formData.append("on_collision", onCollision);
      if (collisionOverrides && Object.keys(collisionOverrides).length > 0) {
        formData.append("collision_overrides", JSON.stringify(collisionOverrides));
      }
      const resp = await authorizedApiFetch("/skills/import-bulk-package", {
        method: "POST",
        body: formData,
      });
      if (!resp.ok) {
        return parseErrorResponse(resp, "Import failed");
      }
      return resp.json();
    },
    onSuccess: () => invalidateAfterPackageImport(qc),
  });
}

export async function exportSkillPackage(id: string, name: string) {
  const resp = await authorizedApiFetch(`/skills/${id}/export?format=zip&include_versionning=true`, {
    method: "GET",
  });
  if (!resp.ok) {
    return parseErrorResponse(resp, "Export failed");
  }
  const blob = await resp.blob();
  const headerName = filenameFromContentDisposition(
    resp.headers.get("Content-Disposition"),
  );
  const fallbackName = `skill_${name.replace(/[^a-zA-Z0-9-_]/g, "_").slice(0, 60)}_${new Date().toISOString().slice(0, 10).replace(/-/g, "")}.zip`;
  saveBlobAs(blob, headerName || fallbackName);
}

export async function exportSkillJson(id: string, name: string) {
  const resp = await authorizedApiFetch(`/skills/${id}/export?format=json`, {
    method: "GET",
  });
  if (!resp.ok) {
    return parseErrorResponse(resp, "Export failed");
  }
  const blob = await resp.blob();
  const fallbackName = `skill-${name.replace(/[^a-zA-Z0-9-_]/g, "-").slice(0, 60)}.json`;
  saveBlobAs(blob, fallbackName);
}
