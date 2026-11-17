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

import { exactCurrency, formatCurrency, isCompacted } from "./number-format.ts";

/**
 * A duration for a person to read: whole milliseconds under a second, one
 * decimal above (the API sends float milliseconds, e.g. 413.11199999999997).
 */
export function formatDuration(ms?: number | null): string {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return "-";
  // 999.6 ms rounds to a whole second, never to "1000ms".
  if (Math.round(ms) < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${(ms / 60_000).toFixed(1)}m`;
}

/** The catalog key of each trace status the backend writes. @i18n-keys */
const TRACE_STATUS_LABELS = {
  ok: "OK",
  error: "Error",
} as const;

/**
 * The catalog key to show for a trace status: the known ones translated, an
 * unknown one (a status a newer backend added) shown as written.
 */
export function traceStatusLabelKey(status: string): string {
  return (TRACE_STATUS_LABELS as Record<string, string>)[status] ?? status;
}

/**
 * A run's cost in US dollars on the platform's compact rule (`lib/number-format`,
 * #10476): four decimals under one cent ("$0.0040"), cents under $100, whole
 * dollars to $999, then compact ("$1.2K"); zero reads in cents ("$0.00") and a
 * missing cost "—". `exactCost` is its hover.
 */
export function formatCost(cost?: number | null, locale = "en"): string {
  if (cost === null || cost === undefined || !Number.isFinite(cost)) return "—";
  if (cost === 0) return exactCurrency(locale, "USD", 0);
  return formatCurrency(locale, "USD", cost);
}

/** The exact amount behind a compacted `formatCost` figure, for its `title`; undefined when the drawn text is already exact. */
export function exactCost(cost?: number | null, locale = "en"): string | undefined {
  if (cost === null || cost === undefined || !Number.isFinite(cost)) return undefined;
  const shown = Math.abs(cost) >= 100 ? Math.round(Math.abs(cost)) : Math.abs(cost);
  return isCompacted(shown) ? exactCurrency(locale, "USD", cost) : undefined;
}

export function resolveProviderFromMetadata(
  metadata?: Record<string, unknown> | null,
): string | null {
  if (!metadata) return null;
  for (const key of ["provider", "llm_provider", "model_provider"] as const) {
    const candidate = metadata[key];
    if (typeof candidate !== "string") continue;
    const value = candidate.trim();
    if (value) return value;
  }
  return null;
}

/** The provider config that served a trace (``app.llm.attribution`` on the backend). */
interface TraceProviderConfig {
  id: string;
  /** The config's display name; `null` when the admin gave it none. */
  label: string | null;
  /** The config is a fallback that answered a call of the trace. */
  failover: boolean;
  /** The config was reached by a budget-driven model downgrade of the run. */
  downgraded: boolean;
}

/**
 * Which provider config served the trace, when the backend recorded it.
 *
 * The provider type alone cannot tell two configs of the same type apart;
 * traces written before the config was recorded return `null`.
 */
export function resolveProviderConfigFromMetadata(
  metadata?: Record<string, unknown> | null,
): TraceProviderConfig | null {
  if (!metadata) return null;
  const rawId = metadata.llm_provider_config_id;
  if (typeof rawId !== "string" || !rawId.trim()) return null;
  const id = rawId.trim();
  const label = metadata.llm_provider_config_label;
  const failover = metadata.llm_provider_failover;
  const downgrade = metadata.llm_model_downgrade;
  const namesConfig = (entry: unknown) =>
    !!entry && typeof entry === "object" && (entry as Record<string, unknown>).config_id === id;
  return {
    id,
    label: typeof label === "string" && label.trim() ? label.trim() : null,
    // Each flag says how the NAMED config was reached: a primary that served
    // again after a failover is named without it, and a later failover names
    // another config than the downgrade moved to.
    failover: Array.isArray(failover) && failover.some(namesConfig),
    downgraded: namesConfig(downgrade),
  };
}

/** File reference recorded on trace/span payloads by the backend
 * (``serialize_file_refs`` in ``langfuse_tracing.py``). */
export interface TraceFileRef {
  file_id: string;
  filename: string;
  content_type?: string;
  size?: number;
}

function parseFileRefArray(value: unknown): TraceFileRef[] {
  if (!Array.isArray(value)) return [];
  const out: TraceFileRef[] = [];
  for (const item of value) {
    if (!item || typeof item !== "object") continue;
    const rec = item as Record<string, unknown>;
    if (typeof rec.file_id !== "string" || typeof rec.filename !== "string") continue;
    if (!rec.file_id || !rec.filename) continue;
    out.push({
      file_id: rec.file_id,
      filename: rec.filename,
      content_type: typeof rec.content_type === "string" ? rec.content_type : undefined,
      size: typeof rec.size === "number" ? rec.size : undefined,
    });
  }
  return out;
}

/** Extract structured file references from a span/trace input or output
 * payload — looks for a top-level ``files`` array of
 * ``{file_id, filename, ...}`` objects. */
export function extractFileRefs(payload: unknown): TraceFileRef[] {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return [];
  return parseFileRefArray((payload as Record<string, unknown>).files);
}

/** Dedupe file refs by ``file_id``, preserving first-seen order. */
export function dedupeFileRefs(refs: TraceFileRef[]): TraceFileRef[] {
  const seen = new Set<string>();
  const out: TraceFileRef[] = [];
  for (const ref of refs) {
    if (seen.has(ref.file_id)) continue;
    seen.add(ref.file_id);
    out.push(ref);
  }
  return out;
}
