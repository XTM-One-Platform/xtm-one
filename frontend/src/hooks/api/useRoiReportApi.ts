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
 * The ROI report (`GET /roi`, #8990 / #9321) and its export, read by the
 * Analytics Outcomes tab (#9706), the fleet ROI view (#9785) and the agent
 * Overview (#9807; design
 * dev-docs/roi-outcomes.md 6.3). Reading the report never starts a run nor
 * calls a model.
 */

import { useMemo } from "react";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { saveBlobAs } from "@/lib/utils";

/** The windows `GET /roi` accepts (`services/roi_report.py` REPORT_WINDOWS); `quarter` is the calendar quarter so far (#9836 R5a). */
export type RoiWindow = "7d" | "30d" | "90d" | "quarter" | "all";

/** A `[low, high]` range of one figure (#9836 R4a); `[null, null]` for a cost figure the reader cannot see. */
export type RoiRange = [number | null, number | null];

/** The figures that carry a low / high range (`services/roi_report.py` `figures_dict`). */
type RoiRangedFigure = "minutes_saved" | "hours_saved" | "value" | "net_value" | "roi" | "payback_months";

interface RoiFigures {
  currency: string;
  runs: number;
  units: number;
  hours_saved: number;
  /** `null` when the report masks costs (`cost_masked`, #9836 R1): never drawn as 0. */
  llm_cost: number | null;
  value: number;
  /** `null` when the report masks costs. */
  net_value: number | null;
  roi: number | null;
  /** Months to recover the setup cost; `null` without a positive net value or when costs are masked. */
  payback_months?: number | null;
  /** The low / high end of every estimated figure (#9836 R4a, design 10.5). */
  range?: Partial<Record<RoiRangedFigure, RoiRange>>;
  reviewed: number;
  unreviewed: number;
  rejected: number;
  /** The chat deliverables the ledger's hero counts beside the runs (#12077 L6b3); absent on the report's own figures. */
  deliverables?: number;
  /** Of `runs`, the records of a work item (`per_work_item` counting), which no runs list shows (#9810). */
  work_items?: number;
  /** Of `unreviewed`, the records of a work item. */
  unreviewed_work_items?: number;
  /** `reviewed / runs`, `null` without a run. */
  review_rate: number | null;
  assumptions: string[];
  /** The same assumptions as structured facts, rendered in the reader's language (#9718). */
  assumption_facts?: RoiAssumptionFact[];
  /** How the figures are reached, with their numbers (design standards s6, #9810). */
  computation?: RoiComputationStep[];
}

/** One line of the computation behind the figures (`services/roi.py` `_computation_facts`). */
export type RoiComputationStep =
  | {
      kind: "saving";
      unit: string;
      name: string;
      units: number;
      manual_min: number;
      review_min: number;
      minutes_saved: number;
      rate: number;
      value: number;
      currency: string;
    }
  | {
      kind: "total";
      minutes_saved: number;
      hours_saved: number;
      value: number;
      llm_cost: number | null;
      net_value: number | null;
      currency: string;
    }
  | { kind: "review"; reviewed: number; total: number }
  | { kind: "rejected"; count: number }
  /** One row of the ledger's By source (automation, chat, other), drawn by the client so the lines sum to the hero (#12077 L6b3 UX r1 B1). */
  | { kind: "source"; source: string; runs: number; deliverables: number; hours: number; value: number; currency: string };

/** One assumption behind the figures (`services/roi.py` `_assumption_facts`). */
export type RoiAssumptionFact =
  | {
      kind: "unit";
      unit: string;
      name: string;
      manual_min: number;
      manual_min_low?: number;
      manual_min_high?: number;
      review_min: number;
      label: string;
      /** Where the minutes come from: a publication, or the reasoning of an assumption (#9836 R4a). */
      source?: string;
    }
  | { kind: "rate"; role: string; amount: number; currency: string; label: string; source: string }
  | { kind: "measured_review" }
  | { kind: "unknown_units"; units: string[] };

export interface RoiGroupFigures extends RoiFigures {
  id: string;
  name: string;
}

/** One week of the Value page's trend (#9836 R5a): Monday 00:00 UTC, oldest first; `net_value` `null` when costs are masked. */
export interface RoiTrendPoint {
  week: string;
  value: number;
  value_range: RoiRange;
  net_value: number | null;
  net_value_range: RoiRange;
  runs: number;
}

export interface RoiReportResponse {
  window: RoiWindow;
  currency: string;
  /** "assumption" while no rate is set in the organization profile. */
  rate_label: string;
  estimated: boolean;
  organization: RoiFigures;
  agents: RoiGroupFigures[];
  fleets: RoiGroupFigures[];
  /** The cost figures are `null` for this reader (the deployment's cost data setting, #9836 R1). */
  cost_masked?: boolean;
  /** The scope answered (`platform`, `mine`, `fleet`, `agent`). */
  scope?: RoiScope;
  /** One row per unit of work, summing to `organization` (#9836 R5a, design 10.7). */
  units?: RoiGroupFigures[];
  /** The weekly value and net value, summing to the hero (#9836 R5a). */
  trend?: RoiTrendPoint[];
  /** The rows a non-administrator cannot open on the organization view, summed (#9836 R1); `null` when none. */
  other_agents?: RoiFigures | null;
  other_fleets?: RoiFigures | null;
  /** The records of an agent outside any fleet: the By fleet rows plus this sum to the hero. */
  no_fleet?: RoiFigures | null;
  /** Where the AI cost comes from (#10441): `llm_analytics_by_agent` when it is the agents' LLM spend, as LLM Analytics reads it. */
  cost_source?: string;
  /** The part of the AI cost no recorded unit carries (the "Cost not tied to a unit" row); `null` when costs are masked. */
  unallocated_cost?: number | null;
  /** A fleet report: its factory reset time when it falls inside the window (#2580), the views' "Measured since <date> (reset)". */
  value_measured_since?: string | null;
  /** How many fleets are measured since a reset inside the window: the platform total leaves their earlier value out (#2580). */
  reset_fleets?: number;
  /** The ledger's figures of the same scope and reader (#12077, design 12.8): what the Value page draws (slice L6a). */
  figures?: RoiLedgerFigures;
}

/** The totals of a ledger row or hero (`services/roi_figures.py` `Totals.as_dict`); cost figures `null` when masked, never 0. */
export interface RoiLedgerTotals {
  runs: number;
  reviewed: number;
  units: number;
  deliverables: number;
  hours: number;
  hours_low: number;
  hours_high: number;
  value: number;
  value_low: number;
  value_high: number;
  ai_cost: number | null;
  net_value: number | null;
  net_value_low: number | null;
  net_value_high: number | null;
  roi: number | null;
  payback_months: number | null;
}

/** One breakdown row: an id, a unit key, a week, or a residual / folded key (`no_fleet`, `other`, ...). */
export interface RoiLedgerRow extends RoiLedgerTotals {
  key: string;
  /** By week: the last week a merged point holds (k rule, design 12.5). */
  until?: string;
}

/** `GET /roi` `figures` (`roi_ledger.figures`): one hero, every breakdown summing to it (invariant c). */
export interface RoiLedgerFigures {
  scope: string;
  currency: string;
  totals: RoiLedgerTotals;
  by_source: RoiLedgerRow[];
  by_unit: RoiLedgerRow[];
  by_agent: RoiLedgerRow[];
  by_fleet: RoiLedgerRow[];
  by_week: RoiLedgerRow[];
  by_person: RoiLedgerRow[] | null;
  cost_masked: boolean;
  /** The first day every fact names its person, when the window starts earlier (design 12.11). */
  since: string | null;
  /** The names of the agents, fleets and units the rows key; never a person. */
  names?: Record<string, string>;
  /** What the k rule did (design 12.5); `null` for Mine and administrators. */
  suppressed?: RoiLedgerSuppressed | null;
}

/** `figures.suppressed` (`services/roi_figures.py` `Suppressed.as_dict`). */
interface RoiLedgerSuppressed {
  k: number;
  groups: number;
  /** An agent or fleet backed by fewer than k people: the figures are the viewer's own share. */
  own_share_only: boolean;
  sources_split: boolean;
  weeks_merged: number;
}

/** `GET /roi/settings`: what the Value page offers (#9836 R1) and the anonymity rule (#12077 L5). */
export interface RoiSettings {
  roi_org_totals_visible: boolean;
  /** k of the k-anonymity rule: a group backed by fewer people is folded for non-administrators (2 to 50). */
  roi_min_group_size?: number;
  /** The administrators' "By person" breakdown (D12-1), audited on every read. */
  roi_by_person_visible?: boolean;
}

/** The bounds `PUT /roi/settings` accepts for `roi_min_group_size` (`api/v1/roi.py` RoiSettingsRequest). */
export const ROI_MIN_GROUP_SIZE_BOUNDS = { min: 2, max: 50, fallback: 5 } as const;

/** The scopes `GET /roi` answers (`services/roi_report.py`). */
export type RoiScope = "platform" | "mine" | "fleet" | "agent";

/**
 * The one read of an ROI report (design 10.5): every view and figure reads
 * `GET /roi` through it, so two screens showing one scope and window share one
 * cache entry and never differ. A failed read stays in the view's own error
 * line, never the page's ErrorBoundary.
 */
function useRoiScopeReport(scope: RoiScope, id: string | null, window: RoiWindow, options: { enabled?: boolean } = {}) {
  const target = id ? `&id=${encodeURIComponent(id)}` : "";
  return useQuery<RoiReportResponse>({
    queryKey: ["roi-report", scope, id ?? "", window],
    queryFn: () => api.get(scope === "platform" ? `/roi?window=${window}` : `/roi?scope=${scope}${target}&window=${window}`),
    enabled: options.enabled ?? true,
    throwOnError: false,
    retry: scope === "platform" ? undefined : false,
  });
}

/**
 * The Value page's report (#9836 R5b): `platform` (the whole organization; a
 * non-administrator's rows outside his scope come folded) or `mine`.
 */
export function useValueReport(scope: "platform" | "mine", window: RoiWindow, options: { enabled?: boolean } = {}) {
  return useRoiScopeReport(scope, null, window, options);
}

/**
 * One agent or fleet of a list page, from `GET /roi/summary` (#9836 R6a, design 10.8). Since
 * #12077 slice L7a its `figures` are the ledger's of that object for the reader (`GET
 * /roi?scope=agent|fleet&id=` `figures`): the hero, By source and the k rule's trace.
 */
export interface RoiSummaryItem {
  id: string;
  /** `false`: nothing counts for it (nothing counted in the window, no unit declared); the cell shows "-". */
  declared: boolean;
  figures?: Pick<RoiLedgerFigures, "scope" | "currency" | "totals" | "by_source" | "cost_masked" | "suppressed">;
  /** What the agent page's hero keeps from the records (UX r1 B1): rejected records, work items and the inputs. */
  records?: RoiSummaryRecords;
}

/** `GET /roi/summary` `items[].records` (`services/roi_report.py` `record_details`). */
interface RoiSummaryRecords {
  rejected: number;
  work_items: number;
  unreviewed_work_items: number;
  assumption_facts: RoiAssumptionFact[];
}

interface RoiSummaryResponse {
  subject_type: "agent" | "fleet";
  window: RoiWindow;
  currency: string;
  cost_masked: boolean;
  /** The ids the reader may read; an id outside his scope is left out. */
  items: RoiSummaryItem[];
}

/** The ids one `GET /roi/summary` takes (`roi_report.SUMMARY_MAX_IDS`). */
export const ROI_SUMMARY_MAX_IDS = 200;

/** The figures of a list, by id, and how to read them. */
export interface RoiSummaryByIds {
  byId: Map<string, RoiSummaryItem>;
  currency: string;
  costMasked: boolean;
  isLoading: boolean;
  isError: boolean;
}

/**
 * The list column's figures (#9836 R6b, design 10.8): one `GET /roi/summary`
 * per chunk of at most 200 ids, never one read per row. The ids are sorted so
 * a re-sort or a filter of the list reuses the cache. Reading it never starts
 * a run nor calls a model.
 */
export function useRoiSummary(
  subjectType: "agent" | "fleet",
  ids: readonly string[],
  window: RoiWindow,
  options: { enabled?: boolean } = {},
): RoiSummaryByIds {
  const chunks = useMemo(() => roiSummaryChunks(ids), [ids]);
  const results = useQueries({
    queries: chunks.map((chunk) => ({
      queryKey: ["roi-summary", subjectType, window, chunk.join(",")],
      queryFn: (): Promise<RoiSummaryResponse> =>
        api.get(`/roi/summary?subject_type=${subjectType}&window=${window}&${chunk.map((id) => `ids=${encodeURIComponent(id)}`).join("&")}`),
      enabled: options.enabled ?? true,
      throwOnError: false,
      retry: false,
    })),
  });
  // One stable object per answer, so a list's column definitions are rebuilt only when a read answers.
  const stamp = results.map((r) => `${r.status}:${r.dataUpdatedAt}`).join("|");
  return useMemo(() => {
    const byId = new Map<string, RoiSummaryItem>();
    let currency = "";
    let costMasked = false;
    for (const result of results) {
      if (!result.data) continue;
      currency = result.data.currency;
      costMasked = result.data.cost_masked;
      for (const item of result.data.items ?? []) byId.set(item.id, item);
    }
    return {
      byId,
      currency,
      costMasked,
      isLoading: results.some((r) => r.isLoading),
      isError: results.some((r) => r.isError),
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `stamp` changes exactly when one of the results does
  }, [stamp]);
}

/** The ids of a list, deduplicated, sorted and cut into reads of at most 200. */
export function roiSummaryChunks(ids: readonly string[]): string[][] {
  const sorted = [...new Set(ids)].sort();
  const chunks: string[][] = [];
  for (let i = 0; i < sorted.length; i += ROI_SUMMARY_MAX_IDS) chunks.push(sorted.slice(i, i + ROI_SUMMARY_MAX_IDS));
  return chunks;
}

/** The organization's ROI settings, read by every user (`GET /roi/settings`). */
export function useRoiSettings(options: { enabled?: boolean } = {}) {
  return useQuery<RoiSettings>({
    queryKey: ["roi-settings"],
    queryFn: () => api.get("/roi/settings"),
    enabled: options.enabled ?? true,
    throwOnError: false,
  });
}

/**
 * Saves the ROI settings (`PUT /roi/settings`, administrators): only the fields given change,
 * one audit entry each. The Value page and every ROI read then follow the stored answer.
 */
export function useSaveRoiSettings() {
  const qc = useQueryClient();
  return useMutation<Required<RoiSettings>, Error, Partial<RoiSettings>>({
    mutationFn: (body) => api.put("/roi/settings", body),
    onSuccess: (saved) => {
      qc.setQueryData(["roi-settings"], saved);
      // The k rule and By person change what every report answers.
      qc.invalidateQueries({ queryKey: ["roi-report"] });
      qc.invalidateQueries({ queryKey: ["roi-summary"] });
    },
  });
}

/** The platform-wide report of a window (administrators). */
export function useRoiReport(window: RoiWindow, options: { enabled?: boolean } = {}) {
  return useRoiScopeReport("platform", null, window, options);
}

/**
 * One fleet's report (`scope=fleet`): administrators and the fleet's ROI
 * readers (#9350). An optional view of the fleet page: a failed read stays in
 * the view's own error line, never the page's ErrorBoundary.
 */
export function useFleetRoiReport(fleetId: string, window: RoiWindow, options: { enabled?: boolean } = {}) {
  return useRoiScopeReport("fleet", fleetId, window, options);
}

/**
 * One agent's report (`scope=agent`, read by whoever sees the agent): the
 * agent Overview's ROI cards (#9807). A failed read stays in the cards' own
 * error line, never the page's ErrorBoundary.
 */
export function useAgentRoiReport(agentId: string, window: RoiWindow, options: { enabled?: boolean } = {}) {
  return useRoiScopeReport("agent", agentId, window, options);
}

/** The query of `GET /roi/export`: the scope the page shows (`mine` included, slice L6a), or one fleet's with `fleetId`. */
export function roiExportPath({ window, format, fleetId, scope }: { window: RoiWindow; format: "csv" | "pdf"; fleetId?: string; scope?: "platform" | "mine" }): string {
  const fleet = fleetId ? `&fleet_id=${encodeURIComponent(fleetId)}` : "";
  const scoped = !fleetId && scope ? `&scope=${scope}` : "";
  return `/roi/export?format=${format}&window=${window}${scoped}${fleet}`;
}

/** `GET /roi/export`: the CSV or the PDF of a scope, saved under the server's name; one fleet's with `fleetId`. */
export function useExportRoiReport() {
  return useMutation({
    mutationFn: async (query: { window: RoiWindow; format: "csv" | "pdf"; fleetId?: string; scope?: "platform" | "mine" }) => {
      const { window, format } = query;
      const { blob, filename } = await api.download(roiExportPath(query));
      saveBlobAs(blob, filename ?? `roi-${window}.${format}`);
    },
  });
}
