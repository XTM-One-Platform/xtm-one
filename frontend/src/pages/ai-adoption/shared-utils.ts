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

import { createContext, useContext } from "react";

import type { DashboardStats, AdoptionTimeSeries } from "@/types";

// ===================================================================
// Activity range + chart helpers
// ===================================================================
export const ACTIVITY_RANGES = [
  { value: "all", label: "All" }, { value: "1y", label: "1Y" }, { value: "6m", label: "6M" },
  { value: "3m", label: "3M" }, { value: "1m", label: "1M" }, { value: "1w", label: "1W" },
] as const;
export type ActivityRange = (typeof ACTIVITY_RANGES)[number]["value"];
export type BarChartKey = keyof Pick<DashboardStats, "top_users" | "top_agents" | "top_assignments" | "top_api_key_users" | "top_integrations" | "top_channels">;

export const TS_INTERVALS = [{ value: "1d", label: "1D" }, { value: "1w", label: "1W" }, { value: "1m", label: "1M" }] as const;
export type TSInterval = (typeof TS_INTERVALS)[number]["value"];
export const TS_COLORS: Record<string, { stroke: string; stop: string }> = { cyan: { stroke: "var(--color-accent)", stop: "var(--color-accent)" }, emerald: { stroke: "var(--color-feedback-success-primary)", stop: "var(--color-feedback-success-primary)" }, violet: { stroke: "var(--color-filigran-ia-primary)", stop: "var(--color-filigran-ia-primary)" } };
export function fmtChartDate(d: string, i: TSInterval) { const dt = new Date(d + "T00:00:00"); return i === "1m" ? dt.toLocaleDateString("en", { month: "short" }) : dt.toLocaleDateString("en", { month: "short", day: "numeric" }); }

// ===================================================================
// Overview range
// ===================================================================
export const OVERVIEW_RANGES = [
  { value: 9999, label: "All" }, { value: 365, label: "1Y" }, { value: 90, label: "3M" }, { value: 30, label: "30D" }, { value: 7, label: "7D" },
] as const;
export type OverviewRange = (typeof OVERVIEW_RANGES)[number]["value"];

// ===================================================================
// Data filtering & aggregation utilities
// ===================================================================
export function rangeToDays(range: ActivityRange): number {
  if (range === "all") return 9999;
  if (range === "1y") return 365;
  if (range === "6m") return 180;
  if (range === "3m") return 90;
  if (range === "1m") return 30;
  return 7;
}

export function rangeDiffLabel(range: ActivityRange): string {
  if (range === "all") return "vs last month";
  if (range === "1y") return "vs prev. year";
  if (range === "6m") return "vs prev. 6 months";
  if (range === "3m") return "vs prev. quarter";
  if (range === "1m") return "vs prev. month";
  return "vs prev. week";
}

export function filterByDays<T extends { date: string }>(data: T[], days: number): T[] {
  const cutoff = new Date();
  cutoff.setDate(cutoff.getDate() - days);
  const cutoffStr = cutoff.toISOString().slice(0, 10);
  return data.filter((d) => d.date >= cutoffStr);
}

// ===================================================================
// Group filter context
// ===================================================================
export const GroupFilterContext = createContext<string | undefined>(undefined);
export function useGroupFilter() { return useContext(GroupFilterContext); }

// Re-export AdoptionTimeSeries for TimeSeriesChart usage
export type { AdoptionTimeSeries };
