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

import type { MetricUnit } from "@/types";

/** A run metric's value as the run page shows it, formatted by its declared unit. */
export function formatRunMetricValue(key: string, value: number | null, unit?: MetricUnit | null): string {
  if (value == null) return "—";
  if (unit === "currency") return `$${value.toFixed(4)}`;
  if (unit === "milliseconds") return `${(value / 1000).toFixed(2)}s`;
  if (unit === "ratio") return `${(value * 100).toFixed(1)}%`;
  // A mean or percentile of tokens has no meaningful fraction; one of counts does
  // (2.5 tool calls per entry), so it keeps one decimal.
  if (unit === "tokens") return value.toLocaleString(undefined, { maximumFractionDigits: 0 });
  if (unit === "count") return value.toLocaleString(undefined, { maximumFractionDigits: 1 });
  // The platform's own keys declare no unit, but their names do: the current
  // `judge_seconds` and `run_duration_seconds`, and `cost_usd` on older recorded
  // runs.
  if (unit == null) {
    if (key === "cost_usd") return `$${value.toFixed(4)}`;
    if (key.endsWith("_seconds")) return `${value.toFixed(2)}s`;
  }
  if (Number.isInteger(value)) return value.toLocaleString();
  return value.toFixed(2);
}
