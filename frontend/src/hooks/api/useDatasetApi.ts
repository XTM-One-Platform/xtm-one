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

import type { RefObject } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { API_BASE, api, getToken } from "@/lib/api";
import { saveBlobAs } from "@/lib/utils";
import type {
  CreateDatasetRequest,
  Dataset,
  DatasetDetail,
  DatasetEntry,
  DatasetVersion,
  DatasetVersionDetail,
  HFPreviewResponse,
  HFSplitsResponse,
  LinkedDataset,
  OutputSchema,
} from "@/types";

// An explicit VITE_API_URL (split dev setup) wins; otherwise the same-origin
// API under the URL path prefix the platform is served from.
const API_V1_BASE = import.meta.env.VITE_API_URL
  ? `${String(import.meta.env.VITE_API_URL).replace(/\/$/, "")}/api/v1`
  : API_BASE;
/** The backend's ceilings: a ZIP package (`MAX_ZIP_SIZE` in `api/v1/datasets.py`)
and a file attached to an entry (`MAX_UPLOAD_SIZE_BYTES` in `services/file_storage.py`). */
const MAX_DATASET_ZIP_BYTES = 500 * 1024 * 1024;
const MAX_ENTRY_FILE_BYTES = 40 * 1024 * 1024;
const MAX_STREAM_BUFFER_BYTES = 1024 * 1024;
export const MAX_HF_ROW_LIMIT = 500_000;

function clampRowLimit(rowLimit?: number): number | undefined {
  if (rowLimit == null) return undefined;
  const intValue = Math.trunc(rowLimit);
  if (intValue <= 0) return 1;
  return Math.min(intValue, MAX_HF_ROW_LIMIT);
}

function sanitizeErrorMessage(value: unknown, fallback: string): string {
  if (typeof value !== "string") return fallback;
  const sanitized = value.replace(/[<>]/g, "").trim();
  if (!sanitized) return fallback;
  return sanitized.slice(0, 300);
}

function validateFile(file: File, opts?: { zipOnly?: boolean }) {
  if (!file || file.size <= 0) {
    throw new Error("File is empty");
  }
  const maxBytes = opts?.zipOnly ? MAX_DATASET_ZIP_BYTES : MAX_ENTRY_FILE_BYTES;
  if (file.size > maxBytes) {
    throw new Error(`File is too large (max ${maxBytes / (1024 * 1024)} MB)`);
  }

  const name = file.name?.trim() ?? "";
  if (!name) {
    throw new Error("File name is invalid");
  }

  if (opts?.zipOnly) {
    const lowerName = name.toLowerCase();
    const isZipMime = file.type === "application/zip" || file.type === "application/x-zip-compressed";
    if (!lowerName.endsWith(".zip") && !isZipMime) {
      throw new Error("Only ZIP files are allowed");
    }
  }
}

async function authorizedApiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const token = getToken();
  const headers = new Headers(init.headers);
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }

  return fetch(`${API_V1_BASE}${path}`, {
    ...init,
    headers,
  });
}

// ---------------------------------------------------------------------------
// Dataset CRUD
// ---------------------------------------------------------------------------

/**
 * List datasets, optionally filtered by search text and/or output family.
 *
 * `family` maps to `GET /datasets?family=...`: it narrows the list to
 * compatible datasets (matching family, or none declared yet).
 *
 * `throwOnError: false` for a picker that says a failed search in its list
 * rather than handing the page to its ErrorBoundary, as
 * `useA2AVerificationState` does: such a read keeps a cache entry of its own,
 * so a strict reader of the same list (`Datasets.tsx`) never mounts on the
 * error it tolerated and throws it at once.
 */
export function useDatasets(search?: string, family?: string, options: { throwOnError?: boolean } = {}) {
  const params = new URLSearchParams();
  if (search) params.set("search", search);
  if (family) params.set("family", family);
  const qs = params.toString() ? `?${params.toString()}` : "";
  const tolerant = options.throwOnError === false;
  return useQuery<Dataset[]>({
    queryKey: ["datasets", search ?? "", family ?? "", ...(tolerant ? ["tolerant"] : [])],
    queryFn: () => api.get(`/datasets${qs}`),
    ...(tolerant ? { throwOnError: false } : {}),
  });
}

export function useDataset(id: string) {
  return useQuery<DatasetDetail>({
    queryKey: ["dataset", id],
    queryFn: () => api.get(`/datasets/${id}`),
    enabled: !!id,
  });
}

// ---------------------------------------------------------------------------
// Dataset Versions (reproducibility snapshots)
// ---------------------------------------------------------------------------

export function useDatasetVersions(datasetId: string) {
  return useQuery<DatasetVersion[]>({
    queryKey: ["dataset-versions", datasetId],
    queryFn: () => api.get(`/datasets/${datasetId}/versions`),
    enabled: !!datasetId,
  });
}

export function useDatasetVersion(datasetId: string, versionNumber?: number) {
  return useQuery<DatasetVersionDetail>({
    queryKey: ["dataset-version", datasetId, versionNumber],
    queryFn: () => api.get(`/datasets/${datasetId}/versions/${versionNumber}`),
    enabled: !!datasetId && versionNumber != null,
  });
}

export function useCreateDataset() {
  const qc = useQueryClient();
  return useMutation<DatasetDetail, Error, CreateDatasetRequest>({
    mutationFn: (data) => api.post("/datasets", data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["datasets"] });
    },
  });
}

export function useUpdateDataset() {
  const qc = useQueryClient();
  // `PUT /datasets/{id}` returns `DatasetResponse`, without `entries`: typed as
  // `Dataset` so callers do not assume `entries` is present.
  return useMutation<Dataset, Error, { id: string } & Partial<CreateDatasetRequest>>({
    mutationFn: ({ id, ...data }) =>
      api.put(`/datasets/${id}`, data),
    onSuccess: (_d, { id }) => {
      qc.invalidateQueries({ queryKey: ["datasets"] });
      qc.invalidateQueries({ queryKey: ["dataset", id] });
      // Every evaluation it is linked to reads its family: no family, or a conflict.
      qc.invalidateQueries({ queryKey: ["evaluation-datasets"] });
    },
  });
}

export function useDeleteDataset() {
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: (id) => api.delete(`/datasets/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["datasets"] });
    },
  });
}

/**
 * Preview a dataset's ground-truth material seeded from an agent's own declaration.
 *
 * Writes nothing; the preview is a server-side deep copy, independent of the agent.
 * The caller edits it and saves through `useUpdateDataset`. `found: false` means the
 * agent has nothing to seed for this dataset's family: a displayable state, not an error.
 *
 * A GET fetched on demand via `enabled: false` + `refetch()`, like `useHFSplits`.
 * The agent id is read from a ref because the caller sets it and fetches in the same
 * click handler: a state update would not reach `refetch()`'s closure until the next
 * render.
 */
export function useSeedGroundTruth(
  datasetId: string,
  agentIdRef: RefObject<string | null>,
) {
  return useQuery<{ found: boolean; output_schema?: OutputSchema; message?: string }>({
    queryKey: ["dataset-seed-ground-truth", datasetId],
    queryFn: () => {
      // Fail fast rather than send `agent_id=` and surface the backend's generic 422.
      if (!agentIdRef.current) {
        return Promise.reject(new Error("No agent selected to seed from"));
      }
      return api.get(
        `/datasets/${datasetId}/seed-ground-truth?agent_id=${encodeURIComponent(agentIdRef.current)}`,
      );
    },
    enabled: false,
  });
}

// ---------------------------------------------------------------------------
// Dataset Entry CRUD
// ---------------------------------------------------------------------------

export function useAddDatasetEntry() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      datasetId,
      ...data
    }: {
      datasetId: string;
      description?: string | null;
      input_prompt: string;
      expected_answer: string;
      evaluation_prompt?: string | null;
      output_schema?: OutputSchema | null;
    }) => api.post<DatasetEntry>(`/datasets/${datasetId}/entries`, data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dataset"] });
      qc.invalidateQueries({ queryKey: ["dataset-versions"] });
    },
  });
}

export function useUpdateDatasetEntry() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      entryId,
      ...data
    }: {
      entryId: string;
      description?: string | null;
      input_prompt: string;
      expected_answer: string;
      evaluation_prompt?: string | null;
      file_ids?: string[] | null;
      output_schema?: OutputSchema | null;
    }) => api.put(`/datasets/entries/${entryId}`, data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dataset"] });
      qc.invalidateQueries({ queryKey: ["dataset-versions"] });
    },
  });
}

export function useDeleteDatasetEntry() {
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: (entryId) => api.delete(`/datasets/entries/${entryId}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dataset"] });
      qc.invalidateQueries({ queryKey: ["dataset-versions"] });
    },
  });
}

export function useUploadDatasetEntryFile() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ entryId, file }: { entryId: string; file: File }) => {
      validateFile(file);
      const formData = new FormData();
      formData.append("file", file);
      const res = await authorizedApiFetch(
        `/datasets/entries/${entryId}/upload`,
        {
          method: "POST",
          body: formData,
        },
      );
      if (!res.ok) throw new Error("Upload failed");
      return res.json();
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dataset"] });
      qc.invalidateQueries({ queryKey: ["dataset-versions"] });
    },
  });
}

export function useRemoveDatasetEntryFile() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ entryId, fileId }: { entryId: string; fileId: string }) =>
      api.delete(`/datasets/entries/${entryId}/files/${fileId}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dataset"] });
      qc.invalidateQueries({ queryKey: ["dataset-versions"] });
    },
  });
}

// ---------------------------------------------------------------------------
// Export / Import
// ---------------------------------------------------------------------------

export function useImportDatasetZip() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (file: File) => {
      validateFile(file, { zipOnly: true });
      const formData = new FormData();
      formData.append("file", file);
      const res = await authorizedApiFetch("/datasets/import", {
        method: "POST",
        body: formData,
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: "Import failed" }));
        throw new Error(sanitizeErrorMessage(err.detail, "Import failed"));
      }
      return res.json() as Promise<DatasetDetail>;
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["datasets"] });
    },
  });
}

export async function exportDataset(id: string, name: string) {
  const res = await authorizedApiFetch(`/datasets/${id}/export`);
  if (!res.ok) throw new Error("Export failed");
  const blob = await res.blob();
  const safeName = name.replace(/[^a-zA-Z0-9-_]/g, "_").slice(0, 60);
  const date = new Date().toISOString().slice(0, 10).replace(/-/g, "");
  saveBlobAs(blob, `dataset_${safeName}_${date}.zip`);
}

// ---------------------------------------------------------------------------
// HuggingFace
// ---------------------------------------------------------------------------

export function useHFSplits(datasetId: string) {
  return useQuery<HFSplitsResponse>({
    queryKey: ["hf-splits", datasetId],
    queryFn: () =>
      api.get(`/datasets/hf-splits?dataset=${encodeURIComponent(datasetId)}`),
    enabled: false,
  });
}

export function useHFPreview(datasetId: string, config?: string, split?: string) {
  const params = new URLSearchParams({ dataset: datasetId });
  if (config) params.set("config", config);
  if (split) params.set("split", split);
  return useQuery<HFPreviewResponse>({
    queryKey: ["hf-preview", datasetId, config, split],
    queryFn: () => api.get(`/datasets/hf-preview?${params.toString()}`),
    enabled: !!datasetId && !!split,
  });
}

export interface HFImportProgress {
  fetched: number;
  total: number;
  saving: boolean;
}

interface HFImportPayload {
  hf_dataset_id: string;
  config?: string;
  split: string;
  input_column: string;
  expected_answer_column?: string;
  evaluation_prompt_column?: string;
  description_column?: string;
  row_limit?: number;
  name?: string;
  description?: string;
  evaluation_instructions?: string;
  is_read_only?: boolean;
}

function parseHFImportSSELine(line: string): Record<string, unknown> | null {
  if (!line.startsWith("data: ")) return null;
  try {
    return JSON.parse(line.slice(6)) as Record<string, unknown>;
  } catch {
    throw new Error("Invalid import stream response");
  }
}

function normalizeProgressValue(value: unknown): number {
  const n = Number(value);
  return Number.isFinite(n) && n >= 0 ? n : 0;
}

/** The dataset an import produced, or the identical one that already existed (`reused`). */
interface HFImportResult {
  id: string;
  name: string;
  reused: boolean;
}

function handleHFImportEvent(
  event: Record<string, unknown>,
  lastProgress: HFImportProgress,
  onProgress: (p: HFImportProgress) => void,
): { nextProgress: HFImportProgress; done?: HFImportResult } {
  const eventType = typeof event.type === "string" ? event.type : "";
  if (!eventType) {
    return { nextProgress: lastProgress };
  }

  if (eventType === "progress") {
    const nextProgress: HFImportProgress = {
      fetched: normalizeProgressValue(event.fetched),
      total: normalizeProgressValue(event.total),
      saving: false,
    };
    onProgress(nextProgress);
    return { nextProgress };
  }

  if (eventType === "saving") {
    const nextProgress = { ...lastProgress, saving: true };
    onProgress(nextProgress);
    return { nextProgress };
  }

  if (eventType === "done") {
    const id = typeof event.id === "string" ? event.id : "";
    const name = typeof event.name === "string" ? event.name : "";
    if (!id || !name) {
      throw new Error("Invalid import result payload");
    }
    // `reused`: an identical dataset already existed, and it is returned instead.
    return { nextProgress: lastProgress, done: { id, name, reused: event.reused === true } };
  }

  if (eventType === "error") {
    throw new Error(sanitizeErrorMessage(event.message, "Import failed"));
  }

  return { nextProgress: lastProgress };
}

async function* iterateSSEDataLines(
  body: ReadableStream<Uint8Array>,
  maxBufferBytes: number,
): AsyncGenerator<string, void, void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }

    buffer += decoder.decode(value, { stream: true });
    if (buffer.length > maxBufferBytes) {
      throw new Error("Import stream response is too large");
    }

    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    for (const line of lines) {
      if (line.startsWith("data: ")) {
        yield line;
      }
    }
  }
}

export async function importHFDataset(
  payload: HFImportPayload,
  onProgress: (p: HFImportProgress) => void,
): Promise<HFImportResult> {
  const resp = await authorizedApiFetch("/datasets/hf-import", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      ...payload,
      row_limit: clampRowLimit(payload.row_limit),
    }),
  });

  if (!resp.ok || !resp.body) {
    const text = await resp.text().catch(() => resp.statusText);
    throw new Error(text || "Import failed");
  }

  let lastProgress: HFImportProgress = { fetched: 0, total: 0, saving: false };

  for await (const line of iterateSSEDataLines(resp.body, MAX_STREAM_BUFFER_BYTES)) {
    const event = parseHFImportSSELine(line);
    if (!event) continue;

    const { nextProgress, done: importDone } = handleHFImportEvent(
      event,
      lastProgress,
      onProgress,
    );
    lastProgress = nextProgress;
    if (importDone) {
      return importDone;
    }
  }
  throw new Error("Stream ended without a result");
}

// ---------------------------------------------------------------------------
// Link / Unlink
// ---------------------------------------------------------------------------

export function useEvaluationDatasets(evaluationId: string) {
  return useQuery<LinkedDataset[]>({
    queryKey: ["evaluation-datasets", evaluationId],
    queryFn: () => api.get(`/datasets/for-evaluation/${evaluationId}`),
    enabled: !!evaluationId,
  });
}

export function useLinkDataset() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      datasetId,
      evaluationId,
    }: {
      datasetId: string;
      evaluationId: string;
    }) => api.post(`/datasets/${datasetId}/link/${evaluationId}`),
    onSuccess: (_d, { evaluationId }) => {
      qc.invalidateQueries({ queryKey: ["evaluation-datasets", evaluationId] });
      qc.invalidateQueries({ queryKey: ["evaluation", evaluationId] });
      qc.invalidateQueries({ queryKey: ["evaluations"] });
    },
  });
}

export function useUnlinkDataset() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      datasetId,
      evaluationId,
    }: {
      datasetId: string;
      evaluationId: string;
    }) => api.delete(`/datasets/${datasetId}/link/${evaluationId}`),
    onSuccess: (_d, { evaluationId }) => {
      qc.invalidateQueries({ queryKey: ["evaluation-datasets", evaluationId] });
      qc.invalidateQueries({ queryKey: ["evaluation", evaluationId] });
      qc.invalidateQueries({ queryKey: ["evaluations"] });
    },
  });
}
