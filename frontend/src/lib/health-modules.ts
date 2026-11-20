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

/** Platform Health modules on the Health page (#101 S8): names and card copy. */
import type { HealthModule, HealthModuleCard } from "@/types";

/** Display names of the modules (catalog keys). */
export const HEALTH_MODULE_LABELS: Record<HealthModule, string> = {
  agents: "Agents",
  fleets: "Fleets",
  xtm_one: "XTM One",
  opencti: "OpenCTI",
  openaev: "OpenAEV",
  opencrq: "OpenCRQ",
};

/**
 * The title of each check, by check id (UX round 1 B2 on #634: a person reads a title, never
 * `agents.assignment_failing`). health-modules.test.ts pins one entry per check the engine registers.
 * @i18n-keys
 */
export const HEALTH_CHECK_TITLES = {
  "agents.assignment_failing": "Assignment failing",
  "agents.blocked_without_blocker": "Work item blocked with no blocker",
  "agents.cost_drift": "Cost per run drifting",
  "agents.cost_spike": "Token use spike",
  "agents.failure_spike": "Run failures rising",
  "agents.feedback_drop": "User ratings dropping",
  "agents.integration_broken": "Bound integration broken",
  "agents.model_change": "Model changed",
  "agents.orphaned_claims": "Work items held by no live run",
  "agents.owner_disabled": "Owner account disabled",
  "agents.schedule_silent": "Schedule not firing",
  "openaev.agents_offline": "OpenAEV agents offline",
  "openaev.healthcheck": "OpenAEV health check failing",
  "openaev.heartbeat": "OpenAEV heartbeat missing",
  "openaev.integration_silent": "OpenAEV integration silent",
  "opencrq.heartbeat": "OpenCRQ heartbeat missing",
  "opencrq.job_failing": "OpenCRQ job failing",
  "opencti.connector_errors": "OpenCTI connector in error",
  "opencti.connector_stale": "OpenCTI connector stale",
  "opencti.heartbeat": "OpenCTI heartbeat missing",
  "opencti.ingestion_lag": "OpenCTI ingestion lagging",
  "opencti.queue_consumers": "OpenCTI queue without consumer",
  "opencti.queue_depth": "OpenCTI queue backlog",
  "opencti.search_disk": "OpenCTI search disk filling up",
  "opencti.search_engine": "OpenCTI search engine unhealthy",
  "xtm_one.certificate_expiry": "Certificate expiring",
  "xtm_one.default_model": "No default model",
  "xtm_one.dependency_unreachable": "Dependency unreachable",
  "xtm_one.embedding_model": "No embedding model",
  "xtm_one.hub_registration": "XTM Hub registration failing",
  "xtm_one.integration_broken": "Integration broken",
  "xtm_one.kb_indexing_stalled": "Knowledge base indexing stalled",
  "xtm_one.kb_stale_embeddings": "Knowledge base embeddings outdated",
  "xtm_one.licence": "License not valid",
  "xtm_one.licence_expiry": "License expiring",
  "xtm_one.mcp_server_broken": "MCP server broken",
  "xtm_one.provider_error_rate": "Model provider errors",
  "xtm_one.quota_near_ceiling": "Quota near its ceiling",
  "xtm_one.registration_token": "Registration token not valid",
  "xtm_one.smtp": "Email server unreachable",
  "xtm_one.trace_backlog": "Trace backlog",
  "xtm_one.version_behind": "Version behind",
  "xtm_one.worker_absent": "Worker absent",
} as const;

/**
 * The label of each evidence value a check records, unit included.
 * @i18n-keys
 */
const HEALTH_EVIDENCE_LABELS = {
  behind: "Versions behind",
  configured: "Configured",
  connection_test: "Connection test",
  cost_per_run: "Cost per run (USD)",
  count: "Count",
  day: "Day",
  days_left: "Days left",
  disk_used_percent: "Disk used (%)",
  documents: "Documents",
  expires: "Expires",
  failed: "Failed",
  failed_runs: "Failed runs",
  failure_rate_pct: "Failure rate (%)",
  health: "Health",
  ingestion_lag_seconds: "Ingestion lag (seconds)",
  installed: "Installed version",
  items: "Items",
  job: "Job",
  judged_runs_read: "Rated runs read",
  kind: "Kind",
  last_error: "Last error",
  last_heartbeat_at: "Last heartbeat",
  last_triggered_at: "Last triggered",
  model: "Model",
  name: "Name",
  new_models: "New models",
  newer: "Newer version",
  oauth_status: "OAuth status",
  offline: "Offline",
  owner_active: "Owner active",
  platform_model: "Platform model",
  previous_cost_per_run: "Previous cost per run (USD)",
  previous_models: "Previous models",
  previous_runs: "Previous runs",
  previous_score: "Previous score",
  probe: "Probe",
  provider: "Provider",
  queue_messages: "Queued messages",
  rate: "Rate",
  ratings: "Ratings",
  running: "Running",
  runs: "Runs",
  score: "Score",
  state: "State",
  status: "Status",
  threshold: "Threshold",
  tokens_last_day: "Tokens in the last day",
  total: "Total",
  traces: "Traces",
  type: "Type",
  updated_at: "Updated",
  validated: "Validated",
  waiting: "Waiting",
  window_hours: "Window (hours)",
  works_in_error: "Works in error",
} as const;

/**
 * The label of each check category.
 * @i18n-keys
 */
export const HEALTH_CATEGORY_LABELS = {
  availability: "Availability",
  capacity: "Capacity",
  configuration: "Configuration",
  cost: "Cost",
  licence: "License",
  performance: "Performance",
  security: "Security",
} as const;

/** A key no table names yet (a check newer than this build): its words, never its snake case. */
function humanize(key: string): string {
  const words = key.replace(/^[a-z_]+\./, "").replace(/_/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : key;
}

function labelOf(table: Record<string, string>, key: string, t: (key: string) => string): string {
  return Object.prototype.hasOwnProperty.call(table, key) ? t(table[key]) : humanize(key);
}

/** The translated title of a check. */
export const checkTitle = (checkId: string, t: (key: string) => string) => labelOf(HEALTH_CHECK_TITLES, checkId, t);
/** The translated label of an evidence value. */
export const evidenceLabel = (key: string, t: (key: string) => string) => labelOf(HEALTH_EVIDENCE_LABELS, key, t);
/** The translated label of a category. */
export const categoryLabel = (category: string, t: (key: string) => string) => labelOf(HEALTH_CATEGORY_LABELS, category, t);

/** The card's subtitle: open counts by severity, then the week's change when known. */
export function moduleCardSubtitle(
  card: HealthModuleCard,
  t: (key: string, values?: Record<string, string | number>) => string,
): string {
  const counts = t("{critical} critical, {warning} warning, {info} info", {
    critical: card.critical,
    warning: card.warning,
    info: card.info,
  });
  if (card.previous_score === null) return counts;
  const delta = card.score - card.previous_score;
  const trend = delta === 0 ? t("unchanged in 7 days") : t("{delta} in 7 days", { delta: delta > 0 ? `+${delta}` : String(delta) });
  return `${counts} · ${trend}`;
}
