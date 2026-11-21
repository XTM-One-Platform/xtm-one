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
import type { HealthModule, HealthModuleCard, HealthOverview } from "@/types";

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
  "xtm_one.maintenance_stalled": "Hourly maintenance stalled",
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
  error: "Error",
  ERROR: "Errors",
  expires: "Expires",
  expires_at: "Expires",
  failed: "Failed",
  failed_runs: "Failed runs",
  failure_rate_pct: "Failure rate (%)",
  consumers: "Queue consumers",
  health: "Health",
  in_scope: "In scope",
  ingestion_lag_seconds: "Ingestion lag (seconds)",
  installed: "Installed version",
  items: "Items",
  job: "Job",
  judged_runs_read: "Rated runs read",
  kind: "Kind",
  last_completed_at: "Last completed",
  last_error: "Last error",
  last_heartbeat_at: "Last heartbeat",
  last_triggered_at: "Last triggered",
  limit: "Run limit",
  model: "Model",
  name: "Name",
  new_models: "New models",
  newer: "Newer version",
  oauth_status: "OAuth status",
  offline: "Offline",
  overdue_since: "Overdue since",
  owner_active: "Owner active",
  period: "Period",
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
  tokens_previous_7_days: "Tokens in the previous week",
  total: "Total",
  traces: "Traces",
  type: "Type",
  updated_at: "Updated",
  used: "Runs used",
  validated: "Validated",
  waiting: "Waiting",
  WARNING: "Warnings",
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

/**
 * What to do about a finding, by check id (UX round 2 B2 on #634: the backend writes it in
 * English; a person reads it in their language). health-modules.test.ts pins one entry per check.
 * @i18n-keys
 */
export const HEALTH_CHECK_RECOMMENDATIONS = {
  "agents.assignment_failing": "Read the last failed run of the assignment; fix it or pause the assignment.",
  "agents.blocked_without_blocker": "Name what each item waits for (blocked_on, blocked_on_refs or a reason) or reopen it.",
  "agents.cost_drift": "Compare a recent run with an older one for a larger read, a longer loop or a model change.",
  "agents.cost_spike": "Read the agent's runs of the day for a loop or an oversized read.",
  "agents.failure_spike": "Read the failed runs of the assignment for a common cause; fix it or pause the assignment.",
  "agents.feedback_drop": "Read the low-rated answers of the window; adjust the persona, skill or tools they share.",
  "agents.integration_broken": "Open the integration, test it, and reconnect or re-authorize it.",
  "agents.model_change": "Compare the agent's runs before and after the change; pin the model if the change was not intended.",
  "agents.orphaned_claims": "Released by the platform's claim sweep; if it persists, read the item's last run.",
  "agents.owner_disabled": "Hand the agent to an active owner or disable it.",
  "agents.schedule_silent": "Check the schedule and its gate; delete the assignment if its work is over.",
  "openaev.agents_offline": "Check the endpoints and the agent service: simulations cannot run on offline agents.",
  "openaev.healthcheck": "Open the scenarios and simulations OpenAEV flags for this check and fix the service, agent, collector or content it names.",
  "openaev.heartbeat": "Check that OpenAEV is running and reaches XTM One (the XTM One URL and token on the OpenAEV side, PLATFORM_REGISTRATION_TOKEN here).",
  "openaev.integration_silent": "Restart the injector, collector or executor, or check its logs; it has stopped reporting.",
  "opencrq.heartbeat": "Check that OpenCRQ is running and reaches XTM One (XTM_ONE__URL and XTM_ONE__TOKEN on the OpenCRQ side, PLATFORM_REGISTRATION_TOKEN here).",
  "opencrq.job_failing": "Open the job in OpenCRQ, read the error of its last run and retry it once the cause is fixed.",
  "opencti.connector_errors": "Open the connector's works in OpenCTI and fix the cause of the errors.",
  "opencti.connector_stale": "Restart the connector or check its logs; it has stopped reporting to OpenCTI.",
  "opencti.heartbeat": "Check that OpenCTI is running and reaches XTM One (XTM_ONE_URL and XTM_ONE_TOKEN on the OpenCTI side, PLATFORM_REGISTRATION_TOKEN here).",
  "opencti.ingestion_lag": "Add OpenCTI workers or check the broker: ingestion is falling behind.",
  "opencti.queue_consumers": "Start the OpenCTI workers: messages wait in the broker and nothing reads them.",
  "opencti.queue_depth": "Add OpenCTI workers or slow the connectors that fill the queue.",
  "opencti.search_disk": "Free or add disk on the search engine nodes, or set retention rules in OpenCTI.",
  "opencti.search_engine": "Check the Elasticsearch or OpenSearch cluster health: unassigned shards or a lost node.",
  "xtm_one.certificate_expiry": "Renew the TLS certificate of the public URL, or fix its chain and host name.",
  "xtm_one.default_model": "Choose a default model in Settings > Platform.",
  "xtm_one.dependency_unreachable": "Check that the dependency is up and reachable from the backend and the worker.",
  "xtm_one.embedding_model": "Choose an embedding model in Settings > Platform.",
  "xtm_one.hub_registration": "Register this platform with XTM Hub from Settings, or register it again.",
  "xtm_one.integration_broken": "Re-authorize the integration or fix its credentials, then test its connection.",
  "xtm_one.kb_indexing_stalled": "Check that the background worker runs, then reprocess the stalled documents.",
  "xtm_one.kb_stale_embeddings": "Re-embed the knowledge base with the platform's embedding model.",
  "xtm_one.licence": "Install a valid Filigran licence in Settings > Platform, or contact Filigran.",
  "xtm_one.licence_expiry": "Renew the Filigran licence before it expires.",
  "xtm_one.maintenance_stalled": "Read the web process logs for the scheduler tick errors; the hourly retention sweeps and purges wait until it completes again.",
  "xtm_one.mcp_server_broken": "Connect the MCP server again so its OAuth token is renewed.",
  "xtm_one.provider_error_rate": "Check the provider's status and the model's credentials, or switch the agents to another model.",
  "xtm_one.quota_near_ceiling": "Agentic runs stop for everyone once the ceiling is reached: raise it with Filigran or reduce the scheduled and event-driven runs.",
  "xtm_one.registration_token": "Set PLATFORM_REGISTRATION_TOKEN to a random secret here and on every connected OpenCTI, OpenAEV or OpenCRQ platform, then restart them.",
  "xtm_one.smtp": "Set SMTP_HOSTNAME and its credentials so the platform can send email.",
  "xtm_one.trace_backlog": "Check the trace store: traces are queued until it accepts them again.",
  "xtm_one.version_behind": "Upgrade XTM One to the latest release; its release notes list what changed.",
  "xtm_one.worker_absent": "Start the worker deployment, or read its logs for why it stopped.",
} as const;

/**
 * The period of a quota ceiling, as the evidence records it (UX round 2 B2 on #634: never "month" raw).
 * @i18n-keys
 */
const HEALTH_PERIOD_LABELS = {
  day: "Daily",
  daily: "Daily",
  week: "Weekly",
  weekly: "Weekly",
  month: "Monthly",
  monthly: "Monthly",
  year: "Yearly",
  yearly: "Yearly",
} as const;

/**
 * A key no table names yet (a check newer than this build): its words, never its snake case.
 * The module segment goes, and the `agentic` segment of a Module Analyst check id with it: the
 * row's Agentic badge already says so (UX round 1 B2 on #953).
 */
function humanize(key: string): string {
  const words = key.replace(/^[a-z_]+\.(?:agentic\.)?/, "").replace(/_/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : key;
}

function labelOf(table: Record<string, string>, key: string, t: (key: string) => string): string {
  return Object.prototype.hasOwnProperty.call(table, key) ? t(table[key]) : humanize(key);
}

/** The translated title of a check. */
export const checkTitle = (checkId: string, t: (key: string) => string) => labelOf(HEALTH_CHECK_TITLES, checkId, t);
/** The translated label of an evidence value. */
export const evidenceLabel = (key: string, t: (key: string) => string) => labelOf(HEALTH_EVIDENCE_LABELS, key, t);
/** The translated label of a quota period; an unknown period as it is. */
export const periodLabel = (period: string, t: (key: string) => string) =>
  Object.prototype.hasOwnProperty.call(HEALTH_PERIOD_LABELS, period) ? t(HEALTH_PERIOD_LABELS[period as keyof typeof HEALTH_PERIOD_LABELS]) : period;
/** The translated recommendation of a check; a check newer than this build keeps the backend's text. */
export const checkRecommendation = (checkId: string, fallback: string, t: (key: string) => string) =>
  Object.prototype.hasOwnProperty.call(HEALTH_CHECK_RECOMMENDATIONS, checkId)
    ? t(HEALTH_CHECK_RECOMMENDATIONS[checkId as keyof typeof HEALTH_CHECK_RECOMMENDATIONS])
    : fallback;
/** The translated label of a category. */
export const categoryLabel = (category: string, t: (key: string) => string) => labelOf(HEALTH_CATEGORY_LABELS, category, t);

/** The command center pill of each product reads the engine's module of the same name (#101 S8b). */
const HEALTH_PRODUCT_MODULES: Partial<Record<string, HealthModule>> = {
  opencti: "opencti",
  openaev: "openaev",
  opencrq: "opencrq",
};

/**
 * Checks a dedicated dashboard notice already says (it names the fix and carries its action), so
 * the Platform Health notice does not say them twice: the model configuration and knowledge base
 * notices, which a plain user sees too and which read the live setting, not the last pass.
 */
export const HEALTH_NOTICE_COVERED_CHECKS: ReadonlySet<string> = new Set([
  "xtm_one.default_model",
  "xtm_one.embedding_model",
  "xtm_one.kb_indexing_stalled",
  "xtm_one.kb_stale_embeddings",
]);

/** What a product pill shows from the engine: its open critical and warning findings. */
export interface ProductHealth {
  critical: number;
  warning: number;
}

/** The engine's open findings of a product, or undefined when its module has no card (not an administrator, Copilot mode). */
export function productHealth(product: string, overview: HealthOverview | undefined): ProductHealth | undefined {
  const module = HEALTH_PRODUCT_MODULES[product];
  const card = module ? overview?.modules.find((m) => m.module === module) : undefined;
  return card ? { critical: card.critical, warning: card.warning } : undefined;
}

/** The pill's tone: unreachable or a critical finding is an error, a warning finding a warning, else healthy. */
export function productPillTone(healthy: boolean | undefined, health: ProductHealth | undefined): "error" | "warning" | "success" {
  if (!healthy || (health?.critical ?? 0) > 0) return "error";
  if ((health?.warning ?? 0) > 0) return "warning";
  return "success";
}

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
