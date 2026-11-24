// SPDX-License-Identifier: Apache-2.0

/**
 * File retention hooks - `/api/v1/settings/file-retention` (#396).
 *
 * How long each category of stored file is kept, what each holds today, the
 * next and last sweep; the change (administrators, audited) and its dry run.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";

/** One category of stored files and its rule (`file_retention.Rule.as_dict` + holdings). */
export interface FileRetentionCategory {
  category: string;
  /** 0 = never deleted. */
  days: number;
  never: boolean;
  /** `last_use`: days count from the last download, preview or read; `created`: from creation. */
  basis: "last_use" | "created";
  source: "default" | "settings" | "environment";
  /** Set by an environment variable: shown, never editable here. */
  locked: boolean;
  default_days: number;
  env_var: string;
  unit: "days";
  files: number;
  bytes: number;
  deleted: number;
}

export interface FileRetentionSettings {
  categories: FileRetentionCategory[];
  max_days: number;
  /** The last completed hourly pass plus an hour; null when the sweep is due now. */
  next_sweep_at: string | null;
  /** No hourly pass completed in two hours. */
  sweep_overdue: boolean;
  maintenance_completed_at: string | null;
  last_sweep: { at: string; files: Record<string, number>; bytes: Record<string, number> } | null;
  can_edit: boolean;
}

export interface FileRetentionPreview {
  categories: { category: string; files: number; bytes: number; kept_referenced: number; more: boolean }[];
  total_files: number;
  total_bytes: number;
}

const KEY = ["settings", "file-retention"] as const;

export function useFileRetention(enabled = true) {
  return useQuery<FileRetentionSettings>({
    queryKey: KEY,
    queryFn: () => api.get("/settings/file-retention"),
    enabled,
    staleTime: 60_000,
  });
}

export function useSaveFileRetention() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (days: Record<string, number>) => api.put<FileRetentionSettings>("/settings/file-retention", { days }),
    onSuccess: (data) => queryClient.setQueryData(KEY, data),
  });
}

export function usePreviewFileRetention() {
  return useMutation({
    mutationFn: (days: Record<string, number>) => api.post<FileRetentionPreview>("/settings/file-retention/preview", { days }),
  });
}
