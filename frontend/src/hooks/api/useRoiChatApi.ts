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
 * The "From chat, estimated" series (#10544 S4, S5; design
 * dev-docs/roi-outcomes.md section 11): `GET /roi/chat` and the correction
 * `PUT /roi/chat/records/{id}`. A side read of the ROI views: a failed read
 * stays in the card's own error line, never the page's ErrorBoundary.
 * Reading it never starts a run nor calls a model.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { RoiWindow } from "@/hooks/api/useRoiReportApi";

/** The kinds of chat deliverable (`services/roi_chat.py` KINDS). */
export type RoiChatKind = "presentation" | "report" | "video" | "file";

/** One deliverable of the series. */
export interface RoiChatLine {
  id: string;
  kind: RoiChatKind;
  deliverable_id: string;
  title: string | null;
  user_id: string | null;
  user_name?: string | null;
  agent_id: string | null;
  agent_name?: string | null;
  conversation_id: string | null;
  size: number;
  size_label: string;
  signals: string[];
  /** A use signal or settled for 7 days: only a counted line carries minutes. */
  counted: boolean;
  last_edit_at: string | null;
  deleted: boolean;
  backfilled: boolean;
  unit: string;
  estimate: { manual_min_low: number; user_min: number; subtracted_min: number; cap_min: number; counted_min: number };
  /** A person's correction, in minutes; it replaces the estimate. */
  adjusted_min: number | null;
  adjusted_at: string | null;
  minutes: number;
  hours: number;
  value: number | null;
  /** The reader may correct this line: it is theirs, or they are an administrator. */
  can_adjust?: boolean;
}

export interface RoiChatSeries {
  label: string;
  window: string;
  currency: string;
  rate_label: string;
  totals: { deliverables: number; pending: number; corrected: number; minutes: number; hours: number; value: number };
  by_kind: Record<RoiChatKind, { deliverables: number; minutes: number }>;
  /** A non-administrator's organization read: the totals and the kinds, never a line. */
  aggregate_only?: boolean;
  lines: RoiChatLine[];
  total_lines: number;
  offset: number;
  limit: number;
  method: string;
  /**
   * The ledger's chat totals of the same scope (#12077, design 12.8): the Chat line of By source,
   * the figure the card shows, so the card, the hero and By fleet never differ. `null`: a scope the
   * reader does not see.
   */
  figures?: { totals: { deliverables: number; hours: number; value: number } } | null;
}

/** Whose lines: the reader's own ("mine"), the organization (lines for an administrator, totals for anyone else), one fleet or one agent. */
export type RoiChatScope = { kind: "mine" } | { kind: "organization" } | { kind: "fleet"; id: string } | { kind: "agent"; id: string };

/** The most lines one read answers (`roi_chat_series.LINES_MAX`): the table pages them on the client. */
const LINES = 200;

const CHAT_KEY = ["roi-chat"] as const;

export function roiChatPath(scope: RoiChatScope, window: RoiWindow): string {
  const params = new URLSearchParams({ window, limit: String(LINES) });
  if (scope.kind === "mine") params.set("mine", "true");
  if (scope.kind === "fleet") params.set("fleet_id", scope.id);
  if (scope.kind === "agent") params.set("agent_id", scope.id);
  return `/roi/chat?${params.toString()}`;
}

export function useRoiChatSeries(scope: RoiChatScope, window: RoiWindow, options: { enabled?: boolean } = {}) {
  const path = roiChatPath(scope, window);
  return useQuery<RoiChatSeries>({
    queryKey: [...CHAT_KEY, path],
    queryFn: () => api.get(path),
    enabled: options.enabled ?? true,
    throwOnError: false,
    retry: false,
  });
}

/** Raise or lower one line (`minutes`), or return it to its estimate (`null`); refreshes every series read. */
export function useCorrectRoiChatLine() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, minutes }: { id: string; minutes: number | null }) =>
      api.put(`/roi/chat/records/${encodeURIComponent(id)}`, { minutes }),
    onSuccess: () => qc.invalidateQueries({ queryKey: CHAT_KEY }),
  });
}
