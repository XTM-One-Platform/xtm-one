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

/** Observability trace contracts shared by trace APIs and UI consumers. */

/** Which payload fields carry a bounded preview rather than the whole value. */
export type PayloadField = "input" | "output" | "metadata";

/**
 * Transport metadata for one payload field.
 *
 * Trace payloads are stored whole, so a span can carry megabytes; list and
 * detail responses return a bounded — but structurally intact — preview and
 * describe here what was held back. Fetch the complete value from the payload
 * endpoint (`useTraceApi().getSpanPayload`).
 */
export interface PayloadFieldMeta {
  /** The response value is a preview, not the full payload. */
  truncated: boolean;
  /** The payload lives in object storage rather than in the row. */
  offloaded: boolean;
  size_bytes?: number | null;
  sha256?: string | null;
  /** Object storage was unreachable at write time and the payload was not stored. */
  offload_failed?: boolean;
  error?: string | null;
}

type PayloadMeta = Partial<Record<PayloadField, PayloadFieldMeta>>;

/** One payload field, complete, as returned by the payload endpoints. */
export interface TracePayloadResponse {
  field: PayloadField;
  offloaded: boolean;
  size_bytes: number | null;
  sha256: string | null;
  /** The payload was never stored; `value` holds the preview captured instead. */
  unavailable: boolean;
  error: string | null;
  value: unknown;
}

/** Marker key of a media blob stored in object storage instead of in the payload. */
export const MEDIA_REFERENCE_KEY = "__xtm_media__";

/**
 * A media blob (image, PDF) that a trace payload references rather than embeds.
 *
 * The trace layer used to replace media with the placeholder `"[image]"`, so the
 * screenshot an agent received could not be recovered from its trace. Blobs now
 * go to object storage and the payload keeps this reference; the bytes are
 * fetched on demand from the payload media endpoint, addressed by `sha256`.
 */
export interface MediaReference {
  content_type: string;
  size_bytes: number;
  sha256: string;
  /** Absent when the upload failed — then `offload_failed` says why. */
  object_name?: string;
  offload_failed?: boolean;
  error?: string;
}

export interface LLMTrace {
  id: string;
  agent_id: string | null;
  user_id: string | null;
  conversation_id: string | null;
  assignment_run_id: string | null;
  parent_trace_id: string | null;
  source: string;
  name: string;
  model: string;
  environment: string;
  metadata: Record<string, unknown>;
  // `Any` on the wire: a payload is an object for most spans but a bare string
  // when a generation returned only content, and a preview of an oversized
  // payload can be a string too. Typing these as objects invited callers to
  // assume a shape that is not guaranteed.
  input: unknown;
  output: unknown;
  /** Set only for payload fields the response had to bound. */
  payload_meta?: PayloadMeta | null;
  tags: string[];
  started_at: string;
  ended_at: string | null;
  duration_ms: number | null;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  input_cost: number;
  output_cost: number;
  total_cost: number;
  status: string;
  error_message: string | null;
  /**
   * Structured failure cause. `null` on a successful trace, and also
   * on a failed one recorded before the classifier existed — which is why the
   * UI treats "no cause" as its own state rather than as `unknown`.
   */
  failure_cause: string | null;
  level: string;
  created_at: string;
  updated_at: string;
  agent_name?: string | null;
  agent_icon?: string | null;
  user_email?: string | null;
  /** Present on trace list items: whether the trace has any evaluation scores. */
  has_scores?: boolean;
}

export interface LLMSpan {
  id: string;
  trace_id: string;
  parent_span_id: string | null;
  type: string;
  name: string;
  model: string | null;
  model_parameters: Record<string, unknown> | null;
  /** See `LLMTrace.input`: object, string, or absent — narrow before use. */
  input: unknown;
  output: unknown;
  metadata: Record<string, unknown>;
  /** Set only for payload fields the response had to bound. */
  payload_meta?: PayloadMeta | null;
  level: string;
  status_message: string | null;
  started_at: string;
  ended_at: string | null;
  duration_ms: number | null;
  time_to_first_token: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  input_cost: number | null;
  output_cost: number | null;
  total_cost: number | null;
  usage_details: Record<string, unknown> | null;
  tool_call_id: string | null;
  tool_arguments: Record<string, unknown> | null;
  stop_reason: string | null;
  position: number;
  created_at: string;
}

export interface LLMTraceListResponse {
  items: LLMTrace[];
  total: number;
  limit: number;
}

export interface TraceSession {
  conversation_id: string;
  /** The conversation's title; null when the conversation is gone. */
  conversation_title?: string | null;
  /** The person the session's conversation belongs to. */
  user_email?: string | null;
  first_activity_at: string;
  last_activity_at: string;
  trace_count: number;
  error_count: number;
  agent_count: number;
  model_count: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  total_cost: number;
}

export interface TraceSessionListResponse {
  items: TraceSession[];
  total: number;
  limit: number;
}

export interface TraceSessionExplorerResponse {
  session_id: string;
  traces: LLMTrace[];
  spans_by_trace_id: Record<string, LLMSpan[]>;
  total_traces: number;
  total_spans: number;
  limit: number;
}

export interface TraceFilters {
  search?: string;
  conversation_id?: string;
  /** Traces recorded by any run of this assignment (`lib/trace-links.ts`). */
  assignment_id?: string;
  /** Traces recorded by this assignment run. */
  assignment_run_id?: string;
  source?: string[];
  agent_id?: string[];
  model?: string[];
  status?: string;
  /**
   * Failure causes to filter on. Repeatable. Accepts the sentinel
   * `unrecorded` for traces with no classification at all.
   */
  failure_cause?: string[];
  level?: string;
  date_from?: string;
  date_to?: string;
  cursor?: string;
  cursor_id?: string;
  limit?: number;
  /** Column to order by — must be one the API allows for the endpoint. */
  sort_by?: string;
  sort_dir?: "asc" | "desc";
  /**
   * Page offset. Used instead of `cursor` when the requested order is not the
   * default newest-first one, whose keyset cursor cannot describe it. The API
   * refuses one past `TRACE_LIST_MAX_OFFSET` (`lib/trace-list-paging.ts`).
   */
  offset?: number;
  /**
   * Read the list from its last row: the exact mirror of `sort_by` /
   * `sort_dir`, so `offset` counts from the end and the rows come back in that
   * mirrored order. How the explorer reaches pages past the offset bound.
   */
  reverse?: boolean;
  /**
   * Only the traces of the current members of these groups. A group
   * manager names groups they manage; naming none, they read all of them.
   */
  group_id?: string[];
}

/** `GET /traces/sessions` groups by conversation and takes no assignment scope. */
export type TraceSessionFilters = Omit<TraceFilters, "assignment_id" | "assignment_run_id">;

/** The analytics windows (`ANALYTICS_PERIODS` in `services/tracing/read_store.py`), and custom. */
export type TraceAnalyticsPeriod = "24h" | "7d" | "30d" | "90d" | "180d" | "1y" | "custom";

export interface TraceAnalyticsFilters {
  period?: TraceAnalyticsPeriod;
  date_from?: string;
  date_to?: string;
  agent_ids?: string[];
  models?: string[];
  sources?: string[];
  /** Narrow to the current members of these groups; a manager's team by default. */
  group_ids?: string[];
}

interface TraceAnalyticsSummaryMetrics {
  total_cost: number;
  total_tokens: number;
  trace_count: number;
  avg_duration_ms: number;
  error_rate: number;
  avg_feedback_score: number | null;
  quota_consumed: number;
  message_count: number;
  avg_quota_per_message: number | null;
}

export interface TraceAnalyticsSummary extends TraceAnalyticsSummaryMetrics {
  previous: TraceAnalyticsSummaryMetrics;
  last_aggregation_at: string | null;
  aggregation_stale: boolean;
  unknown_cost_trace_count: number;
  // Earliest day (YYYY-MM-DD) any figure can be reported for. When
  // window_starts_before_coverage is true the selected range reaches further
  // back than that, so the earlier days are absent rather than genuinely idle.
  stats_coverage_start: string | null;
  window_starts_before_coverage: boolean;
}

export interface TraceAnalyticsCostByModelItem {
  model: string;
  trace_count: number;
  input_tokens: number;
  output_tokens: number;
  input_cost: number | null;
  output_cost: number | null;
  total_cost: number;
}

export interface TraceAnalyticsCostByModelResponse {
  items: TraceAnalyticsCostByModelItem[];
}

interface TraceAnalyticsCostOverTimePoint {
  date: string;
  model: string;
  cost: number;
}

export interface TraceAnalyticsCostOverTimeResponse {
  series: TraceAnalyticsCostOverTimePoint[];
}

interface TraceAnalyticsVolumeOverTimePoint {
  date: string;
  source: string;
  count: number;
}

export interface TraceAnalyticsVolumeOverTimeResponse {
  series: TraceAnalyticsVolumeOverTimePoint[];
}

interface TraceAnalyticsLatencyOverall {
  p50: number | null;
  p75: number | null;
  p90: number | null;
  p95: number | null;
  p99: number | null;
}

export interface TraceAnalyticsLatencyByModelItem extends TraceAnalyticsLatencyOverall {
  model: string;
  trace_count: number;
}

interface TraceAnalyticsLatencyDailyMedianPoint {
  date: string;
  p50: number | null;
}

export interface TraceAnalyticsLatencyResponse {
  overall: TraceAnalyticsLatencyOverall;
  by_model: TraceAnalyticsLatencyByModelItem[];
  daily_median: TraceAnalyticsLatencyDailyMedianPoint[];
  // True when some percentile had to be recombined from stored per-group values
  // instead of merged exactly (only possible on windows longer than 7 days).
  approximate: boolean;
}

export interface TraceAnalyticsCostByAgentItem {
  agent_id: string;
  agent_name: string | null;
  agent_icon: string | null;
  /** Who the agent belongs to; all null for a company-managed agent or one deleted since. */
  owner_id: string | null;
  owner_name: string | null;
  owner_email: string | null;
  is_company_managed: boolean;
  /** A deleted agent keeps its last known name in `agent_name`. */
  agent_status: "active" | "disabled" | "deleted";
  /** Whether the caller may open the agent's page, decided by the server. */
  can_open: boolean;
  /** The page to open, set only with `can_open`. */
  agent_url: string | null;
  trace_count: number;
  total_cost: number;
  avg_cost_per_trace: number;
  avg_duration_ms: number;
  avg_feedback_score: number | null;
}

export interface TraceAnalyticsCostByAgentResponse {
  items: TraceAnalyticsCostByAgentItem[];
}

export interface TraceAnalyticsUsageTotals {
  trace_count: number;
  total_tokens: number;
  total_cost: number;
  avg_cost_per_trace: number;
}

export interface TraceAnalyticsCostByUserItem extends TraceAnalyticsUsageTotals {
  user_id: string;
  user_email: string | null;
  user_name: string | null;
  // null once the account is deleted: its traces keep the id.
  is_active: boolean | null;
  group_names: string[];
  agent_count: number;
}

interface TraceAnalyticsOtherUsersUsage extends TraceAnalyticsUsageTotals {
  user_count: number;
}

export interface TraceAnalyticsCostByUserResponse {
  items: TraceAnalyticsCostByUserItem[];
  total_users: number;
  // The users past the requested limit, counted together so the response adds up to the total.
  other_users: TraceAnalyticsOtherUsersUsage | null;
  // Traces no user can be attributed to (anonymous channels, public widget, platform MCP key, ownerless runs).
  unattributed: TraceAnalyticsUsageTotals | null;
}

export interface TraceAnalyticsCostByGroupItem extends TraceAnalyticsUsageTotals {
  group_id: string;
  group_name: string;
  member_count: number;
  active_user_count: number;
  avg_cost_per_active_user: number;
}

interface TraceAnalyticsUngroupedUsage extends TraceAnalyticsUsageTotals {
  active_user_count: number;
  avg_cost_per_active_user: number;
}

export interface TraceAnalyticsCostByGroupResponse {
  items: TraceAnalyticsCostByGroupItem[];
  ungrouped: TraceAnalyticsUngroupedUsage | null;
  unattributed: TraceAnalyticsUsageTotals | null;
  // A user in several groups counts in each, so the group rows add up to more than the total.
  overlapping_membership: boolean;
}

export interface TraceAnalyticsErrorItem {
  error_message: string;
  count: number;
  last_seen: string | null;
  agent_name: string | null;
  model: string | null;
}

export interface TraceAnalyticsErrorsResponse {
  items: TraceAnalyticsErrorItem[];
}

export interface TraceAnalyticsRefreshResponse {
  status: string;
  message: string;
}

export interface LLMSpanListResponse {
  items: LLMSpan[];
  total: number;
}
