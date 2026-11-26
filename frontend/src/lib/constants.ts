// SPDX-License-Identifier: Apache-2.0

/**
 * Shared constants used across the XTM One frontend.
 *
 * Centralises icon maps, status configs, trigger metadata, and cron presets
 * so that every page renders runs, agents, and assignments consistently.
 */

export const COMMIT_SHORT_SHA = import.meta.env.VITE_GIT_SHA?.slice(0, 7) || "dev";

// APP_VERSION is mapped to VITE_APP_VERSION by the Dockerfile because Vite
// only exposes VITE_* variables to browser code.
// Used as a fallback for the platform version before the /api config response
// arrives. Authoritative value comes from `platformConfig.platform_version`.
export const APP_VERSION = import.meta.env.VITE_APP_VERSION || "0.0.0-dev";

import {
  Bot, Mail, MessageSquare, GitPullRequest, Phone, Plane, Webhook, Sparkles, Cog,
  CheckCircle2, XCircle, Clock, Hourglass, Loader2, Play, Globe, Zap, Hand, BookOpen, Ban,
  AlertCircle, Shield, ShieldAlert, Newspaper, Rss, Search, Hash, SkipForward,
  Plus, Pencil, Trash, ToggleLeft, Link, PauseCircle, PlayCircle, EyeOff, RotateCcw, BellRing,
  // Extended icon set for agent icon picker
  Brain, Heart, Star, Code, Database, FileText, Users, Briefcase, Camera, Headphones,
  Palette, Rocket, Target, Terminal, Truck, Wrench, Anchor, Bell, Bolt, Boxes,
  Calculator, Cloud, Coffee, Compass, Crown, Diamond, Flame, Gamepad2, GraduationCap,
  Hammer, Key, Landmark, Leaf, Lightbulb, Map, Megaphone, Music, PenTool, PieChart,
  Radio, Radar, Satellite, Scale, SearchCheck, ShieldCheck, Stethoscope, Swords, Telescope,
  TreePine, Trophy, Tv, Wand2, FlaskConical, ScanEye, Crosshair, CircuitBoard, Layers,
  Cpu, TextSearch, Tag, Workflow, ListChecks, MessageCircleQuestion,
  // Integration catalog category icons
  Activity, Fingerprint, Share2, Gauge,
  // Icons the built-in agent seed names (backend/app/agents)
  BarChart3, BrainCircuit, Bug, FileCode, GitBranch, Handshake, LayoutDashboard, ListOrdered,
  Network, Scan, ScanSearch, TrendingUp,
} from "lucide-react";
import { BASE_PATH, withBasePath } from "@/lib/base-path";
import type { HotTone } from "@/components/ui/tone-badge";
import { HEADLINE_LABELS } from "@/lib/evaluation-headlines";
import { ALL_HANDOVER_TYPES } from "@/lib/handover-types";
import { providerLogoSrcFrom } from "@/lib/provider-logo-src";
import type {
  A2AExchange,
  AnnotationTargetType,
  FeedbackCriterionType,
  KnowledgeDocumentErrorCode,
  ImprovementProposalStatus,
  TriggerType,
} from "@/types";

/**
 * Static asset paths held as data (`/logos/...`, the brand marks) are
 * prefixed HERE, where the string is defined, with the URL path prefix the
 * platform is served under - so every `<img src>` renders them as is
 * and nothing prefixes them a second time. See `lib/base-path.ts`.
 */
function assetPaths<T extends Record<string, string>>(paths: T): T {
  return Object.fromEntries(
    Object.entries(paths).map(([key, path]) => [key, withBasePath(path)]),
  ) as T;
}

// Sentinel stored in EvaluationRun.model_id for a model-independent (skip_llm)
// agent, which runs once per evaluation instead of once per model. Keep in
// sync with the backend constant (app/services/evaluation/constants.py).
export const MODEL_INDEPENDENT = "model-independent";

// ── Agent / assignment icon map ───────────────────────────────────────────────
/** Maps agent `icon` string values to Lucide components. */
export const ICON_MAP: Record<string, React.ElementType> = {
  // Original icons
  Mail,
  MessageSquare,
  GitPullRequest,
  Phone,
  Plane,
  Webhook,
  Bot,
  Sparkles,
  Cog,
  BookOpen,
  Shield,
  Newspaper,
  Rss,
  Search,
  // Extended set
  Brain,
  Heart,
  Star,
  Code,
  Database,
  FileText,
  Users,
  Briefcase,
  Camera,
  Headphones,
  Palette,
  Rocket,
  Target,
  Terminal,
  Truck,
  Wrench,
  Anchor,
  Bell,
  Bolt,
  Boxes,
  Calculator,
  Cloud,
  Coffee,
  Compass,
  Crown,
  Diamond,
  Flame,
  Gamepad2,
  Globe,
  GraduationCap,
  Hammer,
  Key,
  Landmark,
  Leaf,
  Lightbulb,
  Map,
  Megaphone,
  Music,
  PenTool,
  PieChart,
  Radar,
  Radio,
  Satellite,
  Scale,
  SearchCheck,
  ShieldCheck,
  Stethoscope,
  Swords,
  Telescope,
  TreePine,
  Trophy,
  Tv,
  Wand2,
  FlaskConical,
  ScanEye,
  Crosshair,
  CircuitBoard,
  Layers,
  // Named by the built-in agent seed: a name missing here renders the Bot
  // fallback on every agent surface (map, lists, chat attribution).
  BarChart3,
  BrainCircuit,
  Bug,
  FileCode,
  Fingerprint,
  Gauge,
  GitBranch,
  Handshake,
  LayoutDashboard,
  ListChecks,
  ListOrdered,
  Network,
  Scan,
  ScanSearch,
  Share2,
  ShieldAlert,
  TrendingUp,
  Workflow,
};

// ── Run / assignment status ───────────────────────────────────────────────────
export interface StatusConfig {
  icon: React.ElementType;
  color: string;
  badge: "success" | "destructive" | "outline" | "cyan";
  label: string;
}

export const STATUS_CONFIG: Record<string, StatusConfig> = {
  completed:       { icon: CheckCircle2, color: "text-feedback-success-primary", badge: "success", label: "Completed" },
  done:            { icon: CheckCircle2, color: "text-feedback-success-primary", badge: "success", label: "Done" },
  failed:          { icon: XCircle,      color: "text-feedback-error-primary",     badge: "destructive", label: "Failed" },
  running:         { icon: Loader2,      color: "text-accent",        badge: "cyan", label: "Running" },
  pending:         { icon: Clock,        color: "text-feedback-warning-primary",   badge: "outline", label: "Pending" },
  delayed:         { icon: Clock,        color: "text-default-secondary", badge: "outline", label: "Delayed" },
  // An accepted webhook delivery waiting its turn behind the assignment's
  // active run (backend ``QUEUED_RUN_STATUS``). Distinct from "Pending", which
  // means the run is already handed to the orchestration engine.
  queued:          { icon: Hourglass,    color: "text-feedback-warning-primary",   badge: "outline", label: "Queued" },
  awaiting_human:  { icon: Hand,         color: "text-feedback-warning-primary",  badge: "outline", label: "Awaiting input" },
  dismissed:       { icon: Ban,          color: "text-default-secondary", badge: "outline", label: "Dismissed" },
  cancelled:       { icon: Ban,          color: "text-default-secondary", badge: "outline", label: "Cancelled" },
  // Held back before it started — deliberately NOT the pending clock,
  // which would read as "still queued" and recreate the "the scheduler stopped
  // working" misreading the skipped status exists to prevent.
  skipped:         { icon: SkipForward,  color: "text-feedback-warning-primary",   badge: "outline", label: "Skipped" },
  // Sub-agent run ended without finishing (wait-budget death with live children).
  incomplete:      { icon: Hourglass,    color: "text-feedback-warning-primary",   badge: "outline", label: "Incomplete" },
  // Not a status: the outcome of a failed run the platform ended (a rollout
  // cut, a restart). Neutral, never the error red: the agent did not fail.
  interrupted:     { icon: RotateCcw,    color: "text-default-secondary", badge: "outline", label: "Interrupted" },
  // Not a status: a completed external run whose A2A task waits on the answer
  // to the question it asked its caller (`a2a_exchange.asked.state`).
  waiting_on_caller: { icon: MessageCircleQuestion, color: "text-feedback-warning-primary", badge: "outline", label: "Waiting on caller" },
};

/** How a run reads: its status, except a failed run the platform interrupted
 *  (server `outcome`, `run_outcome.run_outcome`), which reads "Interrupted",
 *  and an external run whose task waits on its caller's answer, which reads
 *  "Waiting on caller" rather than a "Completed" with no result. */
export function runStatusConfig(run: {
  status: string;
  outcome?: string | null;
  a2a_exchange?: A2AExchange | null;
}): StatusConfig {
  if (run.outcome === "interrupted") return STATUS_CONFIG.interrupted;
  if (run.a2a_exchange?.asked?.state === "waiting") return STATUS_CONFIG.waiting_on_caller;
  return STATUS_CONFIG[run.status] || STATUS_CONFIG.pending;
}

// ── Trigger types ───────────────────────────────────────────────────────────
export const TRIGGER_TYPES: TriggerType[] = ["schedule", "webhook", "integration_event", "manual", "work_item", "platform_event"];

export const TRIGGER_ICONS: Record<string, React.ElementType> = {
  schedule: Clock,
  webhook: Globe,
  integration_event: Zap,
  manual: Play,
  work_item: ListChecks,
  platform_event: Radio,
  // The CTEM signal tile of the trigger picker (#1409), a platform_event trigger underneath.
  ctem_signal: Radar,
};

export const TRIGGER_LABELS: Record<string, string> = {
  schedule: "Scheduled",
  webhook: "Webhook",
  integration_event: "Integration event",
  manual: "Manual",
  work_item: "Work item",
  platform_event: "Platform event",
  ctem_signal: "CTEM signal",
  followup: "Follow-up",
};

// ── Cron helpers ────────────────────────────────────────────────────────────
export const CRON_PRESETS: { label: string; value: string }[] = [
  { label: "Every 5 minutes", value: "*/5 * * * *" },
  { label: "Every 10 minutes", value: "*/10 * * * *" },
  { label: "Every hour", value: "0 * * * *" },
  { label: "Daily at 8 AM", value: "0 8 * * *" },
  { label: "Daily at 2 AM", value: "0 2 * * *" },
  { label: "Weekdays at 8 AM", value: "0 8 * * 1-5" },
  { label: "Monday at 9 AM", value: "0 9 * * 1" },
];

const CRON_DESCRIPTIONS: Record<string, string> = {
  "*/5 * * * *":  "Every 5 minutes",
  "*/10 * * * *": "Every 10 minutes",
  "0 * * * *":    "Every hour",
  "0 8 * * *":    "Daily at 8:00 AM",
  "0 8 * * 1-5":  "Weekdays at 8:00 AM",
  "0 9 * * 1":    "Monday at 9:00 AM",
  "0 2 * * *":    "Daily at 2:00 AM",
  "0 3 * * *":    "Daily at 3:00 AM",
};

/** Human-readable description for a cron expression, or the raw string. */
export function describeCron(cron: string): string {
  return CRON_DESCRIPTIONS[cron] ?? cron;
}

// ── LLM provider display names ──────────────────────────────────────────────
/** Canonical display names for LLM providers (avoids CSS capitalize pitfalls). */
export const PROVIDER_DISPLAY_NAMES: Record<string, string> = {
  anthropic: "Anthropic",
  openai: "OpenAI",
  gemini: "Google Gemini",
  openrouter: "OpenRouter",
  ollama: "Ollama",
  mistral: "Mistral AI",
  xai: "SpaceXAI",
  azure_openai: "Azure OpenAI",
  bedrock: "AWS Bedrock",
  litellm: "LiteLLM",
  custom_openai: "Custom OpenAI-Compatible",
  tei: "Embedding Server (/embed)",
  filigran_gateway: "Filigran AI Gateway",
  elevenlabs: "ElevenLabs",
  kling: "Kling AI",
  seedance: "Seedance (BytePlus ModelArk)",
  runway: "Runway",
  vertex_ai: "Google Vertex AI",
  lmstudio: "LM Studio",
};

/** Providers that serve one capability and never chat (the backend's `only_capability`). */
export const SINGLE_CAPABILITY_PROVIDERS: Partial<Record<string, "embedding" | "speech" | "video">> = {
  tei: "embedding",
  elevenlabs: "speech",
  kling: "video",
  seedance: "video",
  runway: "video",
  vertex_ai: "video",
};

// ── Integration provider display names ──────────────────────────────────────
/** Canonical display names for integration providers (avoids CSS capitalize pitfalls like "github" → "Github"). */
export const INTEGRATION_PROVIDER_NAMES: Record<string, string> = {
  // Direct LLM-query integrations (query/evaluate external LLMs as tools)
  anthropic: "Anthropic",
  openai: "OpenAI",
  gemini: "Google Gemini",
  mistral: "Mistral AI",
  // A model server on the customer's network, reached through a relay.
  lmstudio: "LM Studio",
  google: "Google",
  microsoft: "Microsoft",
  github: "GitHub",
  slack: "Slack",
  notion: "Notion",
  hubspot: "HubSpot",
  jira: "Jira",
  kubernetes: "Kubernetes",
  drata: "Drata",
  hibob: "HiBob",
  carta: "Carta",
  figma: "Figma",
  filigran_ai: "Filigran AI",
  planhat: "Planhat",
  quotapath: "QuotaPath",
  metabase: "Metabase",
  odoo: "Odoo",
  openaev: "OpenAEV",
  opencti: "OpenCTI",
  opencrq: "OpenCRQ",
  // The core, a module Platform Health covers on an XTM One platform (#974).
  xtm_one: "XTM One",
  xtm_hub: "XTM Hub",
  intune: "Microsoft Intune",
  sentinel: "Microsoft Sentinel",
  defender: "Microsoft Defender XDR",
  entra: "Microsoft Entra ID",
  entra_a2a: "Foundry A2A",
  oauth_a2a: "OAuth A2A",
  purview: "Microsoft Purview",
  onepassword: "1Password",
  tanium: "Tanium",
  xsoar: "Cortex XSOAR",
  xdr: "Cortex XDR",
  qradar: "IBM QRadar",
  fortisiem: "Fortinet FortiSIEM",
  logrhythm: "LogRhythm",
  crowdsec: "CrowdSec",
  crowdstrike: "CrowdStrike Falcon",
  crowdstrike_ngsiem: "CrowdStrike Next-Gen SIEM",
  anyrun: "ANY.RUN",
  greynoise: "GreyNoise",
  hackerone: "HackerOne",
  intel471: "Intel 471",
  sentinelone: "SentinelOne",
  harfanglab: "HarfangLab",
  attackiq: "AttackIQ",
  elastic_security: "Elastic Security",
  google_secops: "Google SecOps",
  ses: "Amazon SES",
  letsignit: "Letsignit",
  aws_security: "AWS Security",
  tenable: "Tenable",
  thehive: "TheHive",
  databricks: "Databricks",
  supabase: "Supabase",
  threatconnect: "ThreatConnect",
  cyware: "Cyware",
  domaintools: "DomainTools",
  infoblox: "Infoblox",
  rapid7: "Rapid7",
  qualys: "Qualys",
  sumologic: "Sumo Logic",
  misp: "MISP",
  mitre_atlas: "MITRE ATLAS",
  mitre_attack: "MITRE ATT&CK",
  recorded_future: "Recorded Future",
  abuseipdb: "AbuseIPDB",
  abusech: "abuse.ch",
  ransomware_live: "Ransomware.live",
  sekoia: "Sekoia.io",
  sublime: "Sublime Security",
  aikido: "Aikido Security",
  airwallex: "Airwallex",
  rydoo: "Rydoo",
  spendesk: "Spendesk",
  whatsapp: "WhatsApp",
  securityscorecard: "SecurityScorecard",
  pagerduty: "PagerDuty",
  gitlab: "GitLab",
  grafana: "Grafana",
  zendesk: "Zendesk",
  mattermost: "Mattermost",
  proofpoint: "Proofpoint TAP",
  threatq: "ThreatQuotient",
  virustotal: "VirusTotal",
  gti: "Google Threat Intelligence",
  anomali: "Anomali ThreatStream",
  cybersixgill: "Cybersixgill",
  socradar: "SOCRadar",
  flashpoint: "Flashpoint",
  doppel: "Doppel",
  splunk: "Splunk",
  picus: "Picus Security",
  vuln_intel: "Vulnerability Intelligence",
  vulncheck: "VulnCheck",
  wiz: "Wiz",
  wordsmith: "Wordsmith",
  servicenow: "ServiceNow",
  shodan: "Shodan",
  urlscan: "urlscan.io",
  newsapi: "NewsAPI",
  feedly: "Feedly",
  rss: "RSS",
  platform_email: "Platform email",
  perk: "Perk",
  google_search: "Google Search",
  brave_search: "Brave Search",
  giphy: "GIPHY",
  credly: "Credly",
  concord: "Concord",
  dealhub: "DealHub",
  ashby: "Ashby",
  linkedin: "LinkedIn",
  x: "X (Twitter)",
  facebook: "Facebook",
  bluesky: "Bluesky",
  n8n: "n8n",
  youtrack: "YouTrack",
  openapi: "OpenAPI / Swagger",
  openrouter: "OpenRouter",
  ovhcloud: "OVHcloud",
  docusign: "DocuSign",
  google_a2a: "Google Agent Platform A2A",
  nova: "Nova Prompt Safety",
  netsuite: "NetSuite",
  pennylane: "Pennylane",
};

// ── MCP transport display names ─────────────────────────────────────────────
/** Canonical display names for MCP transport types (avoids CSS capitalize pitfalls like "sse" → "Sse"). */
export const TRANSPORT_DISPLAY_NAMES: Record<string, string> = {
  stdio: "Stdio",
  sse: "SSE",
  streamable_http: "Streamable HTTP",
};

// ── Integration status config ───────────────────────────────────────────────
export const INTEGRATION_STATUS: Record<string, { icon: React.ElementType; color: string; badge: "success" | "destructive" | "outline" | "warning"; label: string }> = {
  connected:    { icon: CheckCircle2, color: "text-feedback-success-primary", badge: "success",     label: "Connected" },
  reauthorize:  { icon: AlertCircle,  color: "text-feedback-warning-primary",   badge: "warning",      label: "Re-authorization required" },
  configured:   { icon: AlertCircle,   color: "text-feedback-warning-primary",   badge: "outline",      label: "Pre-configured" },
  untested:     { icon: Clock,        color: "text-default-secondary", badge: "outline",  label: "Not tested" },
  disconnected: { icon: XCircle,      color: "text-default-secondary", badge: "outline",  label: "Not connected" },
  error:        { icon: XCircle,      color: "text-feedback-error-primary",     badge: "destructive",  label: "Error" },
};

/**
 * Detect whether an OAuth integration needs re-authorization due to scope drift.
 * Compares the instance's current scopes against the latest required scopes from
 * the provider catalog. Returns true if there is ANY difference — scopes added
 * or removed in the catalog both require re-authorization to bring the instance
 * back in sync.
 */
export function needsReauthorization(integration: { auth_type: string; status: string; scopes: string | null; required_scopes: string | null; has_access_token: boolean }): boolean {
  if (integration.auth_type !== "oauth") return false;
  if (integration.status !== "connected") return false;
  if (!integration.required_scopes) return false;
  const current = new Set((integration.scopes || "").split(/[\s,]+/).filter(Boolean));
  const required = new Set(integration.required_scopes.split(/[\s,]+/).filter(Boolean));
  if (current.size !== required.size) return true;
  for (const scope of required) {
    if (!current.has(scope)) return true;
  }
  return false;
}

/**
 * The requested scopes the stored OAuth token was not granted (#9776): a
 * person can untick a box on a granular consent screen, and the token then
 * lacks a scope the integration asks for. Empty when the grant is unknown
 * (the provider did not say), when nothing is missing, or with no token.
 */
export function scopesNotGranted(integration: { auth_type: string; has_access_token: boolean; missing_scopes?: readonly string[] | null }): string[] {
  if (integration.auth_type !== "oauth" || !integration.has_access_token) return [];
  return [...(integration.missing_scopes ?? [])];
}

/**
 * The scopes the "Authorized Scopes" list shows: the requested scopes less
 * those the stored token was not granted (#9776, UX round 1 B1), so a
 * declined scope is never shown as authorized; it is listed once, under
 * "Requested scopes not granted".
 */
export function authorizedScopes(integration: { auth_type: string; has_access_token: boolean; scopes: string | null; missing_scopes?: readonly string[] | null }): string[] {
  const notGranted = new Set(scopesNotGranted(integration));
  return (integration.scopes || "").split(/[\s,]+/).filter((scope) => scope && !notGranted.has(scope));
}

/**
 * The status an integration is shown with (#8386): scope drift or a token
 * missing a requested scope (#9776) reads "reauthorize", and an OAuth
 * integration with no stored token never reads "connected", whatever its
 * stored status says: it reads "disconnected" ("Not connected"), one status
 * and one label wherever it is shown. A typed credential nobody tested reads
 * "untested" ("Not tested"), and a failed last test "error" (#11837): the
 * stored status only says a credential is there, never that it works.
 */
export function displayedIntegrationStatus(integration: { auth_type: string; status: string; scopes: string | null; required_scopes: string | null; has_access_token: boolean; missing_scopes?: readonly string[] | null; connection_test_status?: string | null }): string {
  if (needsReauthorization(integration)) return "reauthorize";
  if (integration.status === "connected" && scopesNotGranted(integration).length > 0) return "reauthorize";
  if (integration.auth_type === "oauth" && integration.status === "connected" && !integration.has_access_token) return "disconnected";
  if (integration.status === "connected" && integration.connection_test_status === "error") return "error";
  if (integration.status === "connected" && integration.connection_test_status === "untested") return "untested";
  return integration.status;
}

/**
 * Shown statuses, worst first (#8386): trouble wins over health, so a
 * provider card with a broken install among working ones reads the broken one.
 */
const INTEGRATION_STATUS_PRECEDENCE = ["error", "reauthorize", "disconnected", "configured", "untested", "connected"];

/**
 * The status a provider's catalog card reports for the viewer's installs: the
 * worst displayed status among them (an OAuth install with no token counts as
 * "disconnected" next to a connected one), "disconnected" when none is known.
 */
export function providerInstallsStatus(installs: ReadonlyArray<Parameters<typeof displayedIntegrationStatus>[0]>): string {
  const statuses = new Set(installs.map((i) => displayedIntegrationStatus(i)));
  return INTEGRATION_STATUS_PRECEDENCE.find((s) => statuses.has(s)) ?? "disconnected";
}

// ── Integration type labels ─────────────────────────────────────────────────
/** Human-readable labels for every integration type (capability), read by `t_i18n(INTEGRATION_TYPE_LABELS[type] ?? type)`. @i18n-keys */
const INTEGRATION_TYPE_LABEL_KEYS = {
  email: "Email",
  calendar: "Calendar",
  messaging: "Messaging & Chat",
  storage: "Cloud Storage",
  repository: "Code Repository",
  crm: "CRM",
  documentation: "Documentation",
  travel: "Travel & Booking",
  spend_management: "Spend Management",
  compliance: "Compliance",
  data_governance: "Data Governance",
  ediscovery: "eDiscovery",
  hr: "HR & People",
  sales_compensation: "Sales Compensation",
  design: "Design",
  siem: "SIEM",
  email_security: "Email Security",
  threat_intel: "Threat Intelligence",
  vulnerability_management: "Vulnerability Management",
  application_security: "Application Security",
  cloud_security: "Cloud Security",
  ddi: "DDI (DNS/DHCP/IPAM)",
  vulnerability_disclosure: "Vulnerability Disclosure",
  bug_bounty: "Bug Bounty",
  asm: "Attack Surface Management",
  third_party_risk: "Third-Party Risk",
  incident_management: "Incident Management & On-Call",
  brand_protection: "Brand Protection",
  news: "News & Research",
  search: "Web Search",
  credentials: "Credentials",
  adversarial_validation: "Adversarial Validation",
  ctem: "Continuous Threat Exposure Management",
  clm: "Contract Management",
  legal: "Legal",
  automation: "Automation",
  social: "Social Media",
  ats: "ATS",
  llm: "AI Models",
  soar: "SOAR",
  case_management: "Case Management",
  edr: "EDR",
  xdr: "XDR",
  identity: "Identity",
  secrets_management: "Secrets Management",
  audit: "Audit",
  iam: "IAM",
  directory: "Directory",
  endpoint_management: "Endpoint Management",
  itsm: "ITSM",
  ticketing: "Ticketing",
  cmdb: "CMDB",
  governance_risk_compliance: "GRC",
  images: "Image Search",
  videos: "Video Search",
  observability: "Observability",
  project_management: "Project Management",
  data_platform: "Data Platform & Lakehouse",
  a2a: "Agent to Agent (A2A)",
  marketplace: "Marketplace & Resources",
  guardrails: "Guardrails",
  accounting: "Accounting",
  erp: "ERP",
  finance: "Finance & Treasury",
  expense_management: "Expense Management",
  cpq: "Quoting (CPQ)",
} as const;

/** Integration type labels by type id; display them through `t_i18n`, search haystacks keep the English form. */
export const INTEGRATION_TYPE_LABELS: Record<string, string> = INTEGRATION_TYPE_LABEL_KEYS;

// ── Integration category taxonomy ───────────────────────────────────────────
// Cybersecurity-first taxonomy shared with the backend provider catalog
// (`integration_category` values in backend/app/api/v1/integration_catalog.py).

/** Human-readable labels for every integration category, read by `t_i18n(integrationCategoryLabel(id))`. @i18n-keys */
const INTEGRATION_CATEGORY_LABELS = {
  // Cybersecurity
  threat_intelligence: "Threat Intelligence",
  siem_analytics: "SIEM & Security Analytics",
  detection_response: "Detection & Response",
  exposure_management: "Vulnerability & Exposure Management",
  adversarial_validation: "Adversarial Exposure Validation",
  identity_access: "Identity & Device Security",
  grc: "Governance, Risk & Compliance",
  // AI & Research
  ai_models: "AI Models",
  search_news: "Search & News",
  // Business
  communication: "Communication & Collaboration",
  social_media: "Social Media",
  business_operations: "Business Operations",
  hr: "HR & Talent",
  // Engineering & Operations
  development: "Development & Engineering",
  automation: "Automation",
  observability: "Observability",
} as const;

/**
 * Resolve the display label of an integration category with a title-cased
 * fallback for ids not (yet) in `INTEGRATION_CATEGORY_LABELS` — a provider
 * merged with an unmapped category must never render as a raw lowercase id.
 * The result is the English catalog key: render it through `t_i18n`, and keep
 * the English form for search haystacks.
 */
export function integrationCategoryLabel(category: string): string {
  return (
    (INTEGRATION_CATEGORY_LABELS as Record<string, string>)[category]
    ?? category
      .split("_")
      .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
      .join(" ")
  );
}

/** Lucide icon for each integration category (catalog sidebar + section headers). */
export const INTEGRATION_CATEGORY_ICONS: Record<string, React.ElementType> = {
  threat_intelligence: Radar,
  siem_analytics: Activity,
  detection_response: ShieldAlert,
  exposure_management: Crosshair,
  adversarial_validation: Swords,
  identity_access: Fingerprint,
  grc: Scale,
  ai_models: Brain,
  search_news: Newspaper,
  communication: MessageSquare,
  social_media: Share2,
  business_operations: Briefcase,
  hr: Users,
  development: Code,
  automation: Workflow,
  observability: Gauge,
};

/** The English label of each catalog domain group, read by `t_i18n(group.label)`. @i18n-keys */
export const INTEGRATION_CATEGORY_GROUP_LABELS = {
  cybersecurity: "Cybersecurity",
  ai_research: "AI & Research",
  business: "Business & Productivity",
  engineering: "Engineering & Operations",
  other: "Other",
} as const;

interface IntegrationCategoryGroup {
  id: string;
  label: string;
  categories: string[];
}

/**
 * Ordered domain groups for the integration catalog sidebar and the
 * sectioned results view. Categories not listed here (future additions)
 * are appended to a trailing "Other" group by the catalog browser.
 */
export const INTEGRATION_CATEGORY_GROUPS: IntegrationCategoryGroup[] = [
  {
    id: "cybersecurity",
    label: INTEGRATION_CATEGORY_GROUP_LABELS.cybersecurity,
    categories: [
      "threat_intelligence",
      "siem_analytics",
      "detection_response",
      "exposure_management",
      "adversarial_validation",
      "identity_access",
      "grc",
    ],
  },
  {
    id: "ai_research",
    label: INTEGRATION_CATEGORY_GROUP_LABELS.ai_research,
    categories: ["ai_models", "search_news"],
  },
  {
    id: "business",
    label: INTEGRATION_CATEGORY_GROUP_LABELS.business,
    categories: ["communication", "social_media", "business_operations", "hr"],
  },
  {
    id: "engineering",
    label: INTEGRATION_CATEGORY_GROUP_LABELS.engineering,
    categories: ["development", "automation", "observability"],
  },
];

// ── Channel bot UI helpers ──────────────────────────────────────────────────

/** Maps channel provider keys to icon component, background, foreground classes, and optional brand logo. */
export const CHANNEL_ICONS: Record<string, { icon: React.ElementType; bg: string; fg: string; logo?: string }> = {
  web_chat: { icon: Globe, bg: "bg-accent/10", fg: "text-accent" },
  web_embed_chat: { icon: Code, bg: "bg-filigran-ia-primary/10", fg: "text-filigran-ia-primary" },
  slack_bot: { icon: Hash, bg: "bg-[#4A154B]/20", fg: "text-[#E01E5A]", logo: withBasePath("/logos/slack.svg") },
  teams_bot: { icon: MessageSquare, bg: "bg-[#464EB8]/20", fg: "text-[#6264A7]", logo: withBasePath("/logos/teams.svg") },
  whatsapp_bot: { icon: Phone, bg: "bg-[#25D366]/20", fg: "text-[#25D366]", logo: withBasePath("/logos/whatsapp.svg") },
  telegram_bot: { icon: MessageSquare, bg: "bg-[#229ED9]/20", fg: "text-[#2AABEE]", logo: withBasePath("/logos/telegram.svg") },
  discord_bot: { icon: Gamepad2, bg: "bg-[#5865F2]/20", fg: "text-[#5865F2]", logo: withBasePath("/logos/discord.svg") },
  googlechat_bot: { icon: MessageSquare, bg: "bg-[#00AC47]/20", fg: "text-[#00AC47]", logo: withBasePath("/logos/google_chat.svg") },
};

/** All channel types used for per-agent / GA routing. */
export const ALL_CHANNEL_TYPES = ["web", "web_embed", "slack", "teams", "whatsapp", "telegram", "discord", "google_chat"] as const;

/** External (non-web) channel types — use instead of inline arrays. */
export const EXTERNAL_CHANNEL_TYPES = ["slack", "teams", "whatsapp", "telegram", "discord", "google_chat"] as const;

/** Human-readable labels for channel routing types. */
export const CHANNEL_TYPE_LABELS: Record<string, string> = {
  web: "Web Chat",
  web_embed: "Embedded Chat",
  slack: "Slack",
  teams: "Teams",
  whatsapp: "WhatsApp",
  telegram: "Telegram",
  discord: "Discord",
  google_chat: "Google Chat",
};

/**
 * Maps a channel routing type (e.g. "slack") to its icon key in CHANNEL_ICONS
 * (e.g. "slack_bot").
 */
export const CHANNEL_ROUTING_ICON_KEY: Record<string, string> = {
  web: "web_chat",
  web_embed: "web_embed_chat",
  slack: "slack_bot",
  teams: "teams_bot",
  whatsapp: "whatsapp_bot",
  telegram: "telegram_bot",
  discord: "discord_bot",
  google_chat: "googlechat_bot",
};

// ── Integration provider logos ──────────────────────────────────────────────
/** Maps integration provider keys to their logo SVG paths (already under the
 *  platform's URL path prefix - render them as is). */
export const PROVIDER_LOGOS: Record<string, string> = assetPaths({
  // Direct LLM-query integrations (query/evaluate external LLMs as tools)
  anthropic: "/logos/anthropic.svg",
  openai: "/logos/openai.svg",
  gemini: "/logos/gemini.svg",
  mistral: "/logos/mistral.svg",
  google: "/logos/google.svg",
  microsoft: "/logos/microsoft.svg",
  github: "/logos/github.svg",
  slack: "/logos/slack.svg",
  notion: "/logos/notion.svg",
  hubspot: "/logos/hubspot.svg",
  jira: "/logos/jira.svg",
  drata: "/logos/drata.svg",
  hibob: "/logos/hibob.svg",
  carta: "/logos/carta.svg",
  figma: "/logos/figma.svg",
  filigran_ai: "/logos/filigran.svg",
  planhat: "/logos/planhat.svg",
  quotapath: "/logos/quotapath.svg",
  metabase: "/logos/metabase.svg",
  odoo: "/logos/odoo.svg",
  grafana: "/logos/grafana.svg",
  openaev: "/logos/openaev.svg",
  opencti: "/logos/opencti.svg",
  opencrq: "/logos/opencrq.svg",
  // The pinned emblem (#964 palette); the light theme reads PROVIDER_LOGOS_LIGHT (UX r1 B1 on #980).
  xtm_one: "/xtm-one-emblem.svg",
  xtm_hub: "/logos/xtm_hub.svg",
  intune: "/logos/intune.svg",
  sentinel: "/logos/sentinel.svg",
  defender: "/logos/defender.svg",
  entra: "/logos/entra.svg",
  // The Entra A2A provider shares the Entra brand mark; OAuth A2A is an open
  // standard with no mark to borrow, so it has its own house-style glyph.
  entra_a2a: "/logos/entra.svg",
  google_a2a: "/logos/google.svg",
  oauth_a2a: "/logos/oauth_a2a.svg",
  purview: "/logos/purview.svg",
  onepassword: "/logos/onepassword.svg",
  tanium: "/logos/tanium.svg",
  xsoar: "/logos/xsoar.svg",
  xdr: "/logos/xdr.svg",
  qradar: "/logos/qradar.svg",
  fortisiem: "/logos/fortisiem.svg",
  crowdsec: "/logos/crowdsec.svg",
  crowdstrike: "/logos/crowdstrike.svg",
  crowdstrike_ngsiem: "/logos/crowdstrike.svg",
  anyrun: "/logos/anyrun.svg",
  greynoise: "/logos/greynoise.svg",
  hackerone: "/logos/hackerone.svg",
  intel471: "/logos/intel471.svg",
  sentinelone: "/logos/sentinelone.svg",
  harfanglab: "/logos/harfanglab.svg",
  attackiq: "/logos/attackiq.svg",
  elastic_security: "/logos/elastic_security.svg",
  google_secops: "/logos/google_secops.svg",
  ses: "/logos/ses.svg",
  letsignit: "/logos/letsignit.png",
  aws_security: "/logos/aws_security.svg",
  tenable: "/logos/tenable.svg",
  thehive: "/logos/thehive.svg",
  databricks: "/logos/databricks.png",
  supabase: "/logos/supabase.svg",
  threatconnect: "/logos/threatconnect.svg",
  cyware: "/logos/cyware.svg",
  domaintools: "/logos/domaintools.svg",
  infoblox: "/logos/infoblox.svg",
  rapid7: "/logos/rapid7.svg",
  qualys: "/logos/qualys.svg",
  sumologic: "/logos/sumologic.svg",
  misp: "/logos/misp.svg",
  mitre_atlas: "/logos/mitre_atlas.svg",
  mitre_attack: "/logos/mitre_attack.svg",
  recorded_future: "/logos/recorded_future_mark.png",
  abuseipdb: "/logos/abuseipdb.svg",
  abusech: "/logos/abusech.svg",
  ransomware_live: "/logos/ransomware_live.svg",
  sekoia: "/logos/sekoia.svg",
  sublime: "/logos/sublime.svg",
  whatsapp: "/logos/whatsapp.svg",
  securityscorecard: "/logos/securityscorecard.svg",
  pagerduty: "/logos/pagerduty.svg",
  gitlab: "/logos/gitlab.svg",
  zendesk: "/logos/zendesk.svg",
  mattermost: "/logos/mattermost.svg",
  proofpoint: "/logos/proofpoint.svg",
  threatq: "/logos/threatq.svg",
  virustotal: "/logos/virustotal.svg",
  gti: "/logos/gti.svg",
  anomali: "/logos/anomali.svg",
  cybersixgill: "/logos/cybersixgill.svg",
  socradar: "/logos/socradar.svg",
  flashpoint: "/logos/flashpoint.svg",
  doppel: "/logos/doppel.svg",
  splunk: "/logos/splunk.svg",
  picus: "/logos/picus.svg",
  vuln_intel: "/logos/vuln_intel.svg",
  vulncheck: "/logos/vulncheck.svg",
  wiz: "/logos/wiz.svg",
  servicenow: "/logos/servicenow.svg",
  shodan: "/logos/shodan.png",
  urlscan: "/logos/urlscan.svg",
  newsapi: "/logos/newsapi.svg",
  feedly: "/logos/feedly.svg",
  rss: "/logos/rss.svg",
  platform_email: "/logos/platform_email.svg",
  perk: "/logos/perk.svg",
  google_search: "/logos/google_search.svg",
  brave_search: "/logos/brave_search.svg",
  giphy: "/logos/giphy.svg",
  credly: "/logos/credly.svg",
  concord: "/logos/concord.svg",
  dealhub: "/logos/dealhub.svg",
  ashby: "/logos/ashby.svg",
  linkedin: "/logos/linkedin.svg",
  x: "/logos/x.svg",
  facebook: "/logos/facebook.svg",
  bluesky: "/logos/bluesky.svg",
  n8n: "/logos/n8n.svg",
  youtrack: "/logos/youtrack.svg",
  openapi: "/logos/openapi.svg",
  telegram: "/logos/telegram.svg",
  slack_bot: "/logos/slack.svg",
  teams_bot: "/logos/teams.svg",
  whatsapp_bot: "/logos/whatsapp.svg",
  telegram_bot: "/logos/telegram.svg",
  discord_bot: "/logos/discord.svg",
  googlechat_bot: "/logos/google_chat.svg",
});

/** The marks drawn twice, one per theme: the light theme's file (UX r1 B1 on #980, the #964 palette). */
export const PROVIDER_LOGOS_LIGHT: Record<string, string> = assetPaths({
  xtm_one: "/xtm-one-emblem-light.svg",
});

/**
 * The logo `src` for an integration / bot provider: the curated entry in
 * `PROVIDER_LOGOS` when there is one, else the conventional
 * `/logos/<provider>.svg` with the slug URL-encoded - both under the
 * platform's URL path prefix. The one entry point for this fallback (spelled in
 * `providerLogoSrcFrom()`); never inline it at an `<img>`.
 */
export function providerLogoSrc(provider: string): string {
  return providerLogoSrcFrom(BASE_PATH, PROVIDER_LOGOS, provider);
}

// ── MCP server logos ────────────────────────────────────────────────────────
/** Maps MCP server name fragments (lowercase) to logo SVG paths. Used internally by `inferMCPLogo()`. */
const MCP_LOGOS: Record<string, string> = assetPaths({
  github: "/logos/github.svg",
  slack: "/logos/slack.svg",
  notion: "/logos/notion.svg",
  "google drive": "/logos/google.svg",
  "google maps": "/logos/google.svg",
  google: "/logos/google.svg",
});

/** Infer an MCP server logo from its name (fuzzy match). */
export function inferMCPLogo(name: string): string | null {
  const lower = name.toLowerCase();
  for (const [key, logo] of Object.entries(MCP_LOGOS)) {
    if (lower.includes(key)) return logo;
  }
  return null;
}

// ── SSO provider display names ──────────────────────────────────────────────
/** Maps `sso_provider` DB values to user-friendly badge labels. Used internally by `ssoProviderLabel()`. */
const SSO_PROVIDER_LABELS: Record<string, string> = {
  saml: "SAML",
  oidc: "OIDC",
  sso: "SSO",  // legacy fallback for pre-provisioned users before the fix
};

/** Return a consistent display label for an SSO provider value. */
export function ssoProviderLabel(provider: string | null | undefined): string | null {
  if (!provider) return null;
  return SSO_PROVIDER_LABELS[provider] ?? provider.toUpperCase();
}


// ── Assignment default prompt additions ──────────────────────────────────────
// These MUST stay in sync with the Python constants in
// backend/app/services/assignment_engine.py

export const DEFAULT_AI_LABEL_PROMPT =
  `AI Label: prepend '🤖 [AI-generated]' to all outgoing messages and content.`;

export const DEFAULT_HITL_PROMPT =
  `Human in the Loop: use \`request_human_input\` when unsure, need approval, or face high-stakes/irreversible decisions. Provide clear context and options. Only pause for genuine edge cases — handle routine tasks autonomously.`;

/** System notice shown after a HITL response is recorded. */
export const HITL_CONTINUATION_MESSAGE =
  "Your response has been received. The assignment is continuing in the background. You can safely leave this page.";

export const LANGFUSE_DEFAULT_HOST = "https://cloud.langfuse.com";

// ── Filigran commercial paths ────────────────────────────────────────────────
// The two ways out of Community Edition that are Filigran's to answer, named
// once so every upsell surface (dialogs, notices, splash, settings banners)
// points at the same place. The third path — activate a license — is the
// platform's own and lives in `LicenseActivationDialog`.
export const FILIGRAN_CONTACT_URL = "https://filigran.io/contact/";
export const XTM_ONE_TRIAL_URL = "https://filigran.io/filigran-xtm-one-enterprise-edition-trial/";

// ── Pagination defaults ──────────────────────────────────────────────────────
/**
 * Maximum number of records to fetch from the backend in a single request.
 * Used by Activity tabs and similar paginated views that do client-side
 * filtering on a pre-fetched dataset.
 */
export const BACKEND_FETCH_LIMIT = 500;

/** The error groups LLM Analytics pages through (`ANALYTICS_ERRORS_MAX_LIMIT` on the backend). */
export const ANALYTICS_ERRORS_LIMIT = 500;

/**
 * Maximum file size allowed for chat uploads (mirrors the backend hard cap
 * `MAX_UPLOAD_SIZE_BYTES` in `backend/app/services/file_storage.py`, pinned by
 * `test_frontend_upload_limit_is_the_upload_endpoints`). Files larger than
 * this are rejected by the frontend before the upload request is even sent,
 * giving the user an immediate, actionable error message.
 */
export const MAX_CHAT_UPLOAD_SIZE_MB = 40;
export const MAX_CHAT_UPLOAD_SIZE_BYTES = MAX_CHAT_UPLOAD_SIZE_MB * 1024 * 1024;
/**
 * Most files attached to one chat message (the panel's own default is 10).
 * A screenshot reaches the model at a 2048 px long edge, about 3,000 image
 * tokens on Claude: 30 of them still fit a 200k context beside the prompt,
 * the history and the tools, under every provider's per-request image count.
 */
export const MAX_CHAT_ATTACHMENTS_PER_MESSAGE = 30;
export const CHAT_DRAFT_STORAGE_KEY = "copilot:chat-input-draft";

/**
 * Sentinel sessionStorage key signalling that the next fresh `/chat` visit
 * should auto-submit its prefilled draft instead of only populating the
 * composer. Set by a launcher (`primeChatDraft`) and read (then cleared) once
 * by `Chat` on mount, so a refresh never re-fires.
 */
export const CHAT_AUTOSEND_STORAGE_KEY = `${CHAT_DRAFT_STORAGE_KEY}:autosend`;

/**
 * Sentinel sessionStorage key carrying the agent the next fresh `/chat` visit
 * should target. Set by a launcher (`primeChatDraft`) and read + cleared once
 * by `Chat` on mount, so the first message goes to the chosen agent instead of
 * racing the default-agent resolution onto the General Assistant. The value is
 * an agent id, or `CHAT_GENERAL_AGENT_PARAM` to force the General Assistant.
 */
export const CHAT_PENDING_AGENT_STORAGE_KEY = `${CHAT_DRAFT_STORAGE_KEY}:agent`;

/** Hand-off value meaning "use the General Assistant" (no specific agent). */
export const CHAT_GENERAL_AGENT_PARAM = "general";

// ── Max iterations (agentic loop cap) ───────────────────────────────────────
// Single source of truth mirroring the backend (general_assistant_config.py):
// MAX_ITERATIONS_CEILING / DEFAULT_MAX_ITERATIONS / clamp_max_iterations.
/** Upper bound an admin/agent owner may configure for max_iterations — the
 *  range-input ceiling on every max-iterations slider. Mirrors the backend
 *  MAX_ITERATIONS_CEILING the server enforces on save, also the most rounds a
 *  run still making progress may reach. */
export const MAX_ITERATIONS_CEILING = 1000;
/** Platform default used as the fallback before the General Assistant config
 *  has loaded. The server value is authoritative; this only avoids a flash of
 *  the wrong number on first paint. Mirrors the backend DEFAULT_MAX_ITERATIONS. */
export const MAX_ITERATIONS_DEFAULT = 300;

/**
 * Clamp a configured max_iterations into [1, MAX_ITERATIONS_CEILING] so a
 * legacy / out-of-band value (persisted before the server-side clamp existed)
 * can never feed a range input (min=1, max=1000) an out-of-range value. Mirrors
 * the backend clamp_max_iterations(); the server also clamps on the next save.
 */
export function clampMaxIterations(value: number): number {
  return Math.max(1, Math.min(value, MAX_ITERATIONS_CEILING));
}

// ── Wait bounds (the agentic `wait` primitive) ────────────────
// Fallbacks mirroring the backend (app/tools/wait_policy.py) until
// GET /settings/platform answers with its own ceilings. Waiting does not
// consume the iteration budget, so these bound how long an agent may pause.
/** Longest single wait an admin may configure, in seconds (1 hour). */
export const MAXIMUM_WAIT_TIME_CEILING = 3600;
/** Platform default for a single wait, in seconds (5 minutes). */
export const MAXIMUM_WAIT_TIME_DEFAULT = 300;
/** Ceiling for the cumulative per-run wait, in seconds (24 hours). */
export const MAXIMUM_TOTAL_WAIT_TIME_CEILING = 86400;
/** Platform default for the cumulative per-run wait, in seconds (30 minutes). */
export const MAXIMUM_TOTAL_WAIT_TIME_DEFAULT = 1800;


// ── Refetch intervals (ms) ──────────────────────────────────────────────────
/** Slow polling interval for expensive queries (e.g. dashboard stats). */
export const REFETCH_INTERVAL_SLOW = 30_000;
/** Standard polling interval for lists that auto-refresh (runs, counts, traces). */
export const REFETCH_INTERVAL_STANDARD = 15_000;
/** Faster polling interval for in-progress items (e.g. running assignment runs). */
export const REFETCH_INTERVAL_FAST = 5_000;

/** Duration (ms) to show "Copied!" feedback after clipboard copy actions. */
export const COPY_FEEDBACK_DELAY = 2_000;


// ── Audit log display helpers ────────────────────────────────────────────────
/** Maps audit log actions to icon components. */
export const AUDIT_ACTION_ICONS: Record<string, React.ElementType> = {
  create: Plus,
  update: Pencil,
  delete: Trash,
  configure: Cog,
  toggle: ToggleLeft,
  // Distinct from `toggle`: these two are the assignment pause/resume trail,
  // which is read to answer "who stopped this automation, and when
  // did it come back?" — worth telling apart at a glance in the table.
  pause: PauseCircle,
  resume: PlayCircle,
  trigger: Zap,
  bind: Link,
  unbind: Link,
  transfer_ownership: Users,
  login: Shield,
  logout: Shield,
  security_alert: ShieldAlert,
  runner_command: Terminal,
  // An admin's audited read of another user's private content: the
  // conversation-files acknowledgement, the oversight read of a
  // colleague's machine command history and the one of an agent's
  // delegations started by other users.
  break_glass: ScanEye,
  // A Security Audit finding an admin accepted, restored, or that came back at
  // a higher severity after its dismissal.
  dismiss: EyeOff,
  restore: RotateCcw,
  reopen: BellRing,
  // An administrator revoked a user's personal API key.
  revoke: Ban,
};

/** Maps audit log actions to Tailwind text color classes. */
export const AUDIT_ACTION_COLORS: Record<string, string> = {
  create: "text-feedback-success-primary",
  update: "text-accent",
  delete: "text-feedback-error-primary",
  configure: "text-feedback-warning-primary",
  toggle: "text-filigran-ia-primary",
  pause: "text-feedback-warning-primary",
  resume: "text-feedback-success-primary",
  trigger: "text-accent",
  bind: "text-feedback-info-primary",
  unbind: "text-feedback-warning-primary",
  transfer_ownership: "text-filigran-ia-primary",
  login: "text-feedback-success-primary",
  logout: "text-default-secondary",
  security_alert: "text-feedback-error-primary",
  runner_command: "text-accent",
  break_glass: "text-feedback-warning-primary",
  dismiss: "text-feedback-warning-primary",
  restore: "text-accent",
  reopen: "text-feedback-error-primary",
  revoke: "text-feedback-error-primary",
};

/**
 * The catalog key of each audit log entity type (`entity_type` of `log_audit`), read by
 * `auditEntityLabel`; a type missing here is shown humanised, never as its raw key.
 * @i18n-keys
 */
const AUDIT_ENTITY_LABELS = {
  access_notice: "Access notice",
  agent: "Agent",
  agent_account: "Agent account",
  agent_config_version: "Agent config version",
  agent_delegations: "Agent delegations",
  agent_group: "Flow", // legacy alias for historical audit entries
  agent_memory: "Agent memory",
  agent_persona_version: "Persona version",
  agent_skill: "Agent skill",
  api_key: "API Key",
  app_lock: "App lock",
  assignment: "Assignment",
  assignment_run: "Assignment run",
  assignment_run_approval: "Run approval",
  browser_profile: "Browser profile",
  channel_bot: "Channel Bot",
  chat_tool_calls: "Chat tool calls",
  concierge_cycle: "Platform Health cycle", // legacy alias for historical audit entries
  concierge_pet: "Platform Health pet", // legacy alias for historical audit entries
  concierge_settings: "Platform Health settings", // legacy alias for historical audit entries
  health_cycle: "Platform Health cycle",
  health_pet: "Platform Health pet",
  health_settings: "Platform Health settings",
  conversation: "Conversation",
  conversation_files: "Conversation files",
  custom_dashboard: "Custom dashboard",
  custom_tool: "Custom Tool",
  custom_tool_source: "Custom tool source",
  dashboard_widget_pack: "Widget pack",
  dataset: "Dataset",
  dataset_entry: "Dataset entry",
  evaluation: "Evaluation",
  feedback_criterion: "Feedback criterion",
  file: "File",
  fleet: "Fleet",
  fleet_member_preset: "Fleet member preset",
  flow: "Flow",
  flow_event_type: "Flow event type",
  flow_run: "Flow run",
  general_assistant: "General Assistant",
  group: "User group",
  hitl_whitelist: "Approval allow list",
  hook: "Hook",
  improvement_proposal: "Improvement proposal",
  integration: "Integration",
  integration_bundle: "Integration bundle",
  integration_catalog: "Integration catalog",
  intent_binding: "Intent binding",
  investigation_pack: "Investigation pack",
  invitation: "Invitation",
  knowledge_base: "Knowledge Base",
  langfuse: "Langfuse",
  llm_provider: "LLM Provider",
  mcp_server: "MCP Server",
  // What a `concierge_content_read` entry names (`services/health/consent.py`).
  memory: "Memory",
  memory_consolidation: "Memory consolidation",
  mfa: "MFA",
  mobile_device: "Mobile device",
  model_announcement: "Model announcement",
  nova_rule: "Nova rule",
  oauth_default: "OAuth Default",
  observability: "Observability",
  organization_context: "Organization context",
  organization_context_sync: "Organization context sync",
  password: "Password",
  persona_improvement: "Persona rewrite",
  platform: "Platform",
  platform_registration: "Platform registration",
  platform_settings: "Platform settings",
  platform_storage: "Platform storage",
  preferences: "Preferences",
  presentation: "Presentation",
  presentation_comment: "Presentation comment",
  prompt: "Prompt",
  prompt_version: "Prompt version",
  relay: "Relay",
  relay_enrollment_key: "Relay enrollment key",
  relay_group: "Relay group",
  roi_baseline: "ROI baseline",
  roi_chat_record: "ROI chat record",
  roi_declaration: "ROI declaration",
  roi_rate: "ROI rate",
  roi_record: "ROI record",
  roi_settings: "ROI settings",
  roi_unit: "ROI unit",
  runner: "Runner",
  runner_commands: "Runner commands",
  runner_enrollment_key: "Runner enrollment key",
  secret_vault: "Secret vault",
  security: "Security",
  security_audit_finding: "Security Audit finding",
  skill: "Skill",
  skill_version: "Skill version",
  sso: "SSO",
  template: "Template",
  template_container: "Template",
  terms_of_use: "Terms of use",
  tool_approval_preference: "Tool approval preference",
  trace: "Trace",
  trace_bundle: "Trace bundle",
  user: "User",
  variable: "Variable",
  video_clip: "Video clip",
  video_job: "Video job",
  video_media: "Video media",
  video_music: "Video music",
  video_project: "Video project",
  video_pronunciation_lexicon: "Pronunciation lexicon",
  work_item: "Work item",
  workspace: "Workspace",
  xtm_hub_registration: "XTM Hub registration",
} as const;

/**
 * The catalog key of each audit log action (`action` of `log_audit`), read by
 * `auditActionLabel`; an action missing here is shown humanised, never as its raw key.
 * @i18n-keys
 */
const AUDIT_ACTION_LABELS = {
  accept: "Accept",
  accept_card: "Accept card",
  apply_spec: "Apply spec",
  approval_gates_change: "Approval gates change",
  approve: "Approve",
  autonomy_level_change: "Autonomy level change",
  autonomy_level_seed_default: "Autonomy level default",
  bind: "Bind",
  break_glass: "Break glass",
  bulk_cancel_runs: "Cancel runs",
  cleanup_stale_runs: "Clean up stale runs",
  configure: "Configure",
  connect: "Connect",
  create: "Create",
  decline: "Decline",
  delete: "Delete",
  detach: "Detach",
  disable: "Disable",
  dismiss: "Dismiss",
  download: "Download",
  enable: "Enable",
  execute: "Execute",
  export: "Export",
  export_bundle: "Export bundle",
  expose: "Expose",
  fleet_enable: "Enable fleet",
  fleet_pause: "Pause fleet",
  fleet_resume: "Resume fleet",
  fleet_unset: "Unset fleet",
  followup: "Follow-up",
  followup_chat: "Follow-up chat",
  impersonate: "Impersonate",
  import: "Import",
  import_bundle: "Import bundle",
  import_rejected: "Import rejected",
  instructions_position_change: "Instructions position change",
  login: "Sign-in",
  login_declined: "Sign-in declined",
  login_refused: "Sign-in refused",
  logout: "Sign-out",
  pause: "Pause",
  prerequisite_confirmation: "Prerequisite confirmed",
  preview_openapi_spec: "Preview OpenAPI spec",
  purge: "Purge",
  purge_runs: "Purge runs",
  reject: "Reject",
  reopen: "Reopen",
  restore: "Restore",
  resume: "Resume",
  revoke: "Revoke",
  rollback: "Roll back",
  runner_command: "Runner command",
  security_alert: "Security alert",
  session_ended: "Session ended",
  share: "Share",
  sync: "Sync",
  test: "Test",
  toggle: "Toggle",
  toggle_bundle: "Toggle bundle",
  transfer_ownership: "Transfer ownership",
  trigger: "Run now",
  unbind: "Unbind",
  undo: "Undo",
  uninstall_bundle: "Uninstall bundle",
  unshare: "Unshare",
  update: "Update",
  use: "Use",
  validate_bundle: "Validate bundle",
  verify: "Verify",
} as const;

/** A raw snake_case key in words ("login_declined" reads "Login declined"): the fallback of an unknown key. */
function humaniseAuditKey(key: string): string {
  const words = key.replace(/_+/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : key;
}

/** The translated label of an audit log entity type, never its raw key (design standards s.4b). */
export function auditEntityLabel(key: string, t: (key: string) => string): string {
  const label = (AUDIT_ENTITY_LABELS as Record<string, string>)[key];
  return label ? t(label) : humaniseAuditKey(key);
}

/** The translated label of an audit log action, never its raw key (design standards s.4b). */
export function auditActionLabel(key: string, t: (key: string) => string): string {
  const label = (AUDIT_ACTION_LABELS as Record<string, string>)[key];
  return label ? t(label) : humaniseAuditKey(key);
}


// ── Tool display names for chat reasoning indicator ──────────────────────────
// Maps tool function names (from integrations, MCP, built-in) to concise
// human-readable labels shown in the chat UI while the agent is working.
// Unknown tools fall back to a generic formatting helper below.
// One style across every label: a present participle and its object,
// no indefinite article ("Deleting video", never "Deleting a video"); "the"
// only before a thing there is one of ("Searching the web").

/** The catalog key of each tool's status label, read by `toolDisplayName`. @i18n-keys */
const TOOL_DISPLAY_NAMES = {
  // Notion
  search_notion: "Searching Notion",
  get_notion_page: "Reading Notion page",
  read_notion_page: "Reading Notion page", // legacy alias (renamed to get_notion_page)
  list_notion_databases: "Listing Notion databases",
  query_notion_database: "Querying Notion database",
  upload_notion_file: "Uploading file to Notion",
  download_notion_file: "Downloading file from Notion",
  restore_notion_page: "Restoring Notion page",
  get_notion_me: "Checking Notion connection",
  // Figma
  export_figma_images: "Rendering Figma images",
  download_figma_image: "Downloading Figma image",
  // Drata
  download_drata_file: "Downloading file from Drata",
  // GitHub (MCP)
  search_issues: "Searching GitHub issues",
  search_repositories: "Searching GitHub repositories",
  get_issue: "Reading GitHub issue",
  list_issues: "Listing GitHub issues",
  create_issue: "Creating GitHub issue",
  get_pull_request: "Reading pull request",
  list_pull_requests: "Listing pull requests",
  create_pull_request: "Creating pull request",
  search_code: "Searching code",
  get_file_contents: "Reading file contents",
  // Web / search
  search_web: "Searching the web",
  search_web_site: "Searching website",
  // Email (Microsoft)
  read_emails: "Reading emails",
  send_email: "Sending email",
  reply_email: "Replying to email",
  search_emails: "Searching emails",
  flag_email: "Flagging email",
  create_draft: "Drafting email",
  // Calendar
  read_calendar: "Reading calendar",
  create_event: "Creating calendar event",
  // Knowledge base
  search_knowledge: "Searching knowledge base",
  store_knowledge: "Storing knowledge",
  list_knowledge_documents: "Listing knowledge documents",
  get_knowledge_document: "Reading knowledge document",
  update_knowledge_document: "Updating knowledge document",
  // Memory
  search_memory: "Searching memory",
  // Agent delegation
  consult_agent: "Consulting agent",
  transfer_to_agent: "Transferring to agent",
  handover_to_agent: "Transferring to agent", // legacy alias
  spawn_background_task: "Delegating background task",
  check_task_status: "Checking task status",
  get_task_result: "Getting task result",
  list_background_tasks: "Listing background tasks",
  // Code interpreter
  execute_code: "Running code",
  // Image generation
  generate_image: "Generating image",
  // File generation (+ read-back / in-place revision of conversation files)
  generate_file: "Generating file",
  read_file_content: "Reading file",
  edit_file_content: "Editing file",
  // Presentation generation
  generate_presentation: "Building presentation",
  edit_presentation: "Editing presentation",
  get_presentation: "Reading presentation",
  search_presentation_text: "Searching presentation",
  duplicate_presentation: "Copying presentation",
  delete_presentation: "Deleting presentation",
  export_presentation: "Exporting presentation",
  // Review comments on a presentation
  list_presentation_comments: "Reading review comments",
  add_presentation_comment: "Leaving review comment",
  resolve_presentation_comment: "Closing review comment",
  // Who can open a presentation, and its public link
  share_presentation: "Sharing presentation",
  set_presentation_public_link: "Updating public link",
  // Video Studio
  list_browser_profiles: "Listing browser profiles",
  save_browser_profile: "Saving browser profile",
  sign_in_browser_profile: "Signing in",
  submit_browser_mfa_code: "Checking MFA code",
  check_browser_profile_session: "Checking browser session",
  snapshot_browser_profile_page: "Looking at application page",
  record_browser_demo: "Recording demo",
  get_video_recording: "Reading recording",
  list_video_recordings: "Listing recordings",
  video_studio_capabilities: "Checking video providers",
  generate_video_clip: "Generating video clip",
  save_video_frame: "Saving video frame",
  save_video_frames: "Saving video frames",
  get_video_insert_sweep: "Sweeping video screen inserts",
  preview_start_frame: "Previewing shot first frame",
  compose_picture: "Composing picture",
  generate_music: "Composing music",
  preview_voiceover: "Generating voice-over",
  create_video: "Producing video",
  edit_video: "Editing video",
  get_video: "Reading video",
  list_videos: "Listing videos",
  // Who can open a video, and its public link
  share_video: "Sharing video",
  set_video_public_link: "Updating video public link",
  delete_video_renders: "Cleaning up video renders",
  delete_video: "Deleting video",
  render_video: "Rendering video",
  render_video_scene: "Previewing video scene",
  get_video_jobs: "Waiting for video jobs",
  cancel_video_job: "Cancelling video job",
  // Web fetch
  web_fetch: "Fetching web page",
  // Browser
  browser: "Controlling browser",
  // Machines - computer_use (XTM One Desktop and runners)
  list_runners: "Listing machines",
  run_shell: "Running command",
  runner_read_file: "Reading machine file",
  runner_write_file: "Writing machine file",
  runner_list_dir: "Listing machine folder",
  // Former names, kept so older conversations keep their labels.
  read_file: "Reading machine file",
  write_file: "Writing machine file",
  list_dir: "Listing machine folder",
  computer_screenshot: "Capturing the screen",
  computer_action: "Using the mouse and keyboard",
  browser_control: "Driving browser",
  // Work items
  create_work_item: "Creating work item",
  update_work_item: "Updating work item",
  list_work_items: "Listing work items",
  get_work_item: "Reading work item",
  claim_work_item: "Claiming work item",
  complete_work_item: "Completing work item",
  delegate_to_assignment: "Delegating to assignment",
  // Shared state
  get_shared_state: "Reading shared state",
  set_shared_state: "Updating shared state",
  list_shared_state: "Listing shared state",
  resolve_shared_state: "Resolving shared state",
  delete_shared_state: "Purging shared state",
  // Template containers (structured metadata search — not RAG)
  search_templates: "Searching templates",
  get_template: "Reading template",
  // Slack
  search_slack: "Searching Slack",
  send_slack_message: "Sending Slack message",
  // Teams
  search_teams: "Searching Teams",
  send_teams_message: "Sending Teams message",
  // Jira - issues
  search_jira_issues: "Searching Jira issues",
  count_jira_issues: "Counting Jira issues",
  get_jira_issue: "Getting Jira issue",
  create_jira_issue: "Creating Jira issue",
  update_jira_issue: "Updating Jira issue",
  get_jira_transitions: "Getting Jira transitions",
  transition_jira_issue: "Transitioning Jira issue",
  assign_jira_issue: "Assigning Jira issue",
  get_jira_issue_changelog: "Getting Jira issue history",
  delete_jira_issue: "Deleting Jira issue",
  manage_jira_issue_watchers: "Managing Jira watchers",
  // Jira - fields (custom fields)
  list_jira_fields: "Listing Jira fields",
  get_jira_create_fields: "Getting Jira create fields",
  get_jira_edit_fields: "Getting Jira edit fields",
  // Jira - comments & worklogs
  list_jira_comments: "Listing Jira comments",
  get_jira_comment: "Reading Jira comment",
  add_jira_comment: "Commenting on Jira issue",
  update_jira_comment: "Updating Jira comment",
  delete_jira_comment: "Deleting Jira comment",
  list_jira_worklogs: "Listing Jira worklogs",
  add_jira_worklog: "Logging work on Jira issue",
  manage_jira_worklog: "Managing Jira worklog",
  // Jira - attachments
  list_jira_attachments: "Listing Jira attachments",
  download_jira_attachment: "Downloading Jira attachment",
  add_jira_attachment_text: "Attaching file to Jira issue",
  delete_jira_attachment: "Deleting Jira attachment",
  // Jira - agile
  list_jira_boards: "Listing Jira boards",
  get_jira_board_configuration: "Getting Jira board config",
  list_jira_board_backlog: "Listing Jira backlog",
  list_jira_sprints: "Listing Jira sprints",
  get_jira_sprint_issues: "Getting sprint issues",
  list_jira_epics: "Listing Jira epics",
  manage_jira_sprint: "Managing Jira sprint",
  manage_jira_epic: "Managing Jira epic",
  // Jira - projects
  list_jira_projects: "Listing Jira projects",
  get_jira_project: "Getting Jira project",
  list_jira_project_versions: "Listing Jira versions",
  list_jira_project_components: "Listing Jira components",
  manage_jira_version: "Managing Jira version",
  manage_jira_component: "Managing Jira component",
  // Jira - metadata
  list_jira_metadata: "Listing Jira metadata",
  // Jira - users
  get_jira_myself: "Getting Jira profile",
  search_jira_users: "Searching Jira users",
  find_jira_assignable_users: "Finding assignable users",
  manage_jira_group: "Managing Jira group",
  // Jira - links
  link_jira_issues: "Linking Jira issues",
  list_jira_issue_links: "Listing Jira issue links",
  delete_jira_issue_link: "Deleting Jira issue link",
  manage_jira_remote_links: "Managing Jira web links",
  // Jira - filters & dashboards
  list_jira_filters: "Listing Jira filters",
  manage_jira_filter: "Managing Jira filter",
  list_jira_dashboards: "Listing Jira dashboards",
  // Jira - service management
  list_jira_service_desks: "Listing service desks",
  list_jira_request_types: "Listing request types",
  get_jira_request_type_fields: "Getting request type fields",
  create_jira_service_request: "Creating service request",
  list_jira_service_requests: "Listing service requests",
  get_jira_service_request: "Getting service request",
  add_jira_service_request_comment: "Commenting on service request",
  transition_jira_service_request: "Transitioning service request",
  manage_jira_service_request: "Managing service request",
  list_jira_service_desk_queues: "Listing service desk queues",
  manage_jira_organization: "Managing JSM organization",
  list_jira_kb_articles: "Searching knowledge base",
  // HubSpot
  search_hubspot: "Searching HubSpot",
  // Generic
  request_human_input: "Requesting approval",
} as const satisfies Record<string, string>;

/** Return a concise human-readable label for a tool name, in the person's
 *  language. An unknown tool falls back to its name with the
 *  underscores spaced and the first letter capitalised, untranslated. */
export function toolDisplayName(name: string, t_i18n: (id: string) => string): string {
  if (Object.prototype.hasOwnProperty.call(TOOL_DISPLAY_NAMES, name)) {
    return t_i18n((TOOL_DISPLAY_NAMES as Record<string, string>)[name]);
  }
  // Fallback: "search_notion_pages" → "Search notion pages"
  const words = name.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** Every tool's status label in the reader's language: the chat panel's `toolLabels`. */
export function toolDisplayLabels(t_i18n: (id: string) => string): Record<string, string> {
  return Object.fromEntries(Object.entries(TOOL_DISPLAY_NAMES).map(([tool, label]) => [tool, t_i18n(label)]));
}

/* ------------------------------------------------------------------ */
/*  Speech API detection (runs once at module load)                    */
/* ------------------------------------------------------------------ */

export const SpeechRecognitionAPI: SpeechRecognitionConstructor | null =
  typeof window !== "undefined"
    ? window.SpeechRecognition ?? window.webkitSpeechRecognition ?? null
    : null;

// ---------------------------------------------------------------------------
// Timezone picker
// ---------------------------------------------------------------------------

/** Common IANA timezone names for the profile timezone picker. */
export const COMMON_TIMEZONES = [
  // Americas
  "America/New_York",
  "America/Chicago",
  "America/Denver",
  "America/Los_Angeles",
  "America/Anchorage",
  "America/Toronto",
  "America/Vancouver",
  "America/Mexico_City",
  "America/Bogota",
  "America/Lima",
  "America/Santiago",
  "America/Sao_Paulo",
  "America/Argentina/Buenos_Aires",
  // Europe
  "Europe/London",
  "Europe/Dublin",
  "Europe/Paris",
  "Europe/Berlin",
  "Europe/Madrid",
  "Europe/Rome",
  "Europe/Amsterdam",
  "Europe/Brussels",
  "Europe/Zurich",
  "Europe/Vienna",
  "Europe/Stockholm",
  "Europe/Oslo",
  "Europe/Copenhagen",
  "Europe/Helsinki",
  "Europe/Warsaw",
  "Europe/Prague",
  "Europe/Bucharest",
  "Europe/Athens",
  "Europe/Istanbul",
  "Europe/Moscow",
  // Middle East & Africa
  "Africa/Cairo",
  "Africa/Johannesburg",
  "Africa/Lagos",
  "Africa/Nairobi",
  "Asia/Dubai",
  "Asia/Riyadh",
  "Asia/Jerusalem",
  // Asia & Pacific
  "Asia/Kolkata",
  "Asia/Colombo",
  "Asia/Dhaka",
  "Asia/Bangkok",
  "Asia/Jakarta",
  "Asia/Singapore",
  "Asia/Kuala_Lumpur",
  "Asia/Hong_Kong",
  "Asia/Shanghai",
  "Asia/Taipei",
  "Asia/Seoul",
  "Asia/Tokyo",
  // Oceania
  "Australia/Perth",
  "Australia/Adelaide",
  "Australia/Sydney",
  "Australia/Melbourne",
  "Australia/Brisbane",
  "Pacific/Auckland",
  "Pacific/Fiji",
  "Pacific/Honolulu",
  // UTC
  "UTC",
] as const;

// ── Content display ──────────────────────────────────────────────────────────

/** Max characters shown for prompt/skill content previews before truncation. */
export const CONTENT_PREVIEW_LIMIT = 2_000;

/** Trace prompt/message blocks longer than this (in characters) are collapsed by default. */
export const TRACE_PROMPT_COLLAPSE_THRESHOLD = 600;

// ── Memory display constants ─────────────────────────────────────────────────
// Used by the agent MemoryTab.

/** Display config for memory source types. */
export const MEMORY_SOURCE_LABELS: Record<string, { label: string; icon: typeof Brain }> = {
  chat: { label: "Chat conversation", icon: Brain },
  compaction: { label: "Context compaction", icon: Cpu },
  consolidation: { label: "Memory consolidation", icon: Sparkles },
  reconciliation: { label: "Memory reconciliation", icon: Layers },
  assignment: { label: "Assignment execution", icon: Workflow },
  learning: { label: "Style learning", icon: TextSearch },
  tool: { label: "Agent tool", icon: Brain },
};

/** Display config for memory entry types (colors + icons). */
export const MEMORY_TYPE_CONFIG: Record<string, { bg: string; text: string; border: string; label: string; icon: typeof Brain }> = {
  consolidated: { bg: "bg-accent/10", text: "text-accent", border: "border-accent/20", label: "Consolidated", icon: Sparkles },
  fact: { bg: "bg-feedback-success-primary/10", text: "text-feedback-success-primary", border: "border-feedback-success-primary/20", label: "Fact", icon: FileText },
  learned: { bg: "bg-feedback-success-primary/10", text: "text-feedback-success-primary", border: "border-feedback-success-primary/20", label: "Learned", icon: FileText },
  style: { bg: "bg-filigran-ia-primary/10", text: "text-filigran-ia-primary", border: "border-filigran-ia-primary/20", label: "Style", icon: Tag },
  preference: { bg: "bg-feedback-alert-primary/10", text: "text-feedback-warning-primary", border: "border-feedback-alert-primary/20", label: "Preference", icon: Tag },
  state: { bg: "bg-feedback-info-primary/10", text: "text-feedback-info-primary", border: "border-feedback-info-primary/20", label: "State", icon: Camera },
  tradecraft: { bg: "bg-elevation-highlight", text: "text-default-primary", border: "border-elevation-subtle-soft", label: "Tradecraft", icon: Crosshair },
};

// ── Quota display constants ──────────────────────────────────────────────────

/**
 * Human labels for quota periods in usage sentences, e.g.
 * "12 agentic actions this month". Shared by the composer quota
 * indicator, the profile quota section, and the admin user detail page.
 */
export const QUOTA_PERIOD_LABELS: Record<string, string> = {
  daily: "today",
  monthly: "this month",
  yearly: "this year",
};

// ---------------------------------------------------------------------------
// Agent Delegation
// ---------------------------------------------------------------------------

// ── Reasoning effort ─────────────────────────────────────────────────────────
/** Valid reasoning effort levels for thinking/reasoning models. */
export const REASONING_EFFORT_LEVELS = ["low", "medium", "high", "max"] as const;
type ReasoningEffort = (typeof REASONING_EFFORT_LEVELS)[number];

/** Display config for each reasoning effort level. */
export const REASONING_EFFORT_CONFIG: Record<ReasoningEffort, { label: string; description: string }> = {
  low: { label: "Low", description: "Fastest and cheapest. May skip thinking for simple tasks." },
  medium: { label: "Medium", description: "Balanced speed and quality. Moderate thinking depth." },
  high: { label: "High", description: "Default. Deep reasoning for complex tasks." },
  max: { label: "Max", description: "Highest reasoning tier: slower and more expensive. Top reasoning models only (Claude Fable/Opus 4.6+, GPT-6 Astra); remapped to the closest tier elsewhere." },
};

// ── Organization context cascade (#11533) ──────────────────────────────────────
/** The modes an agent or an assignment stores, in the order a picker lists them. */
export const ORGANIZATION_CONTEXT_MODES = ["inherit", "always", "off"] as const;

/** @i18n-keys */
export const ORGANIZATION_CONTEXT_MODE_LABELS = {
  inherit: "Inherit",
  always: "Always",
  off: "Off",
} as const;

/**
 * Mirrors `services/organization_context_injection.resolve`: the assignment's
 * explicit mode wins, then the agent's, then the platform switch.
 */
export function organizationContextEffective(
  platformEnabled: boolean,
  agentMode?: string | null,
  assignmentMode?: string | null,
): boolean {
  for (const mode of [assignmentMode, agentMode]) {
    if (mode === "always") return true;
    if (mode === "off") return false;
  }
  return platformEnabled;
}

// ── Template containers ───────────────────────────────────────────────
/** The kinds a template can be created with: the surface it dresses.
 *  Mirrors `TEMPLATE_KINDS` in `backend/app/services/template_containers.py`
 *  (kept in sync by hand, no shared codegen); the routes refuse anything else
 *  with a 422, so the form offers exactly these. A row of a retired kind
 *  (`asset-library`, `dashboard`) still opens — `templateKindLabel()` names it
 *  as stored — it just cannot be created anymore. */
export const TEMPLATE_KINDS = ["presentation", "report", "video"] as const;
export type TemplateKind = (typeof TEMPLATE_KINDS)[number];

/** Display label and the one-sentence explanation the kind picker shows under
 *  each option — what the user is choosing, not a taxonomy to learn. */
export const TEMPLATE_KIND_CONFIG: Record<TemplateKind, { label: string; description: string }> = {
  presentation: {
    label: "Presentation",
    description: "Dresses slide decks and pages: palette per theme, fonts, logos, predefined assets, slide layouts.",
  },
  report: {
    label: "Report",
    description: "Dresses executive reports: document palette with risk colours, fonts, logos, predefined assets.",
  },
  video: {
    label: "Video",
    description: "Dresses Video Studio videos: palette per theme, fonts, logos, and the look, cards and locks every video keeps.",
  },
};

/** The display label of any stored kind: the picker's label for one of
 *  `TEMPLATE_KINDS`, the raw value for a retired kind so an old row reads as
 *  what it is. */
export function templateKindLabel(kind: string): string {
  return (TEMPLATE_KIND_CONFIG as Record<string, { label: string }>)[kind]?.label ?? kind;
}

/** The retired template `kind` of the seeded logo packs, recognised
 *  on an EXISTING row only (no layout catalog to show for it). Never offered:
 *  the partner / integration / customer logos are predefined
 *  assets of the brand deck itself. Mirrors `ASSET_LIBRARY_KIND` in
 *  `backend/app/services/template_containers.py`. */
export const ASSET_LIBRARY_KIND = "asset-library";

/** Recognised structured roles for a template asset. Mirrors `ASSET_ROLES` in
 *  `backend/app/models/template_container.py` — kept in sync by hand (same
 *  arrangement as `KNOWN_FEATURE_FLAGS`, no shared codegen).
 *
 *  The backend deliberately ACCEPTS values outside this list (a new consumer
 *  feature must not need a code change to label its own asset kinds) and only
 *  lower-cases them, so treat this as the suggested set for pickers, never as
 *  validation. */
export const TEMPLATE_ASSET_ROLES = [
  "font",
  "image",
  "logo",
  "logo_light",
  "logo_mark",
  "product_logo",
  "partner_logo",
  "integration_logo",
  "customer_logo",
  "people_photo",
  "background",
  "icon",
  "illustration",
  "diagram",
  "screenshot",
  "badge",
  "other",
] as const;
// ── Knowledge base document errors ───────────────────────────────────────────
/** Short label of each document failure cause (`error_code` /
 *  `latest_error_code`), translated where it is rendered.
 *  @i18n-keys */
export const KB_DOCUMENT_ERROR_LABELS = {
  no_content: "No content",
  knowledge_base_not_found: "Knowledge base not found",
  no_extractable_text: "No extractable text",
  embedding_not_configured: "No embedding model configured",
  embedding_not_available: "Embedding model not available to its author",
  embedding_provider_error: "Embedding provider error",
  dimension_mismatch: "Vector dimension mismatch",
  timeout: "Embedding timed out",
  worker_timeout: "Worker timed out",
  internal_error: "Internal error",
} as const satisfies Record<KnowledgeDocumentErrorCode, string>;

// ── Output format ────────────────────────────────────────────────────────────
/** Valid output format values for agents. */
export const OUTPUT_FORMATS = ["markdown", "html", "json", "plain_text"] as const;
export type OutputFormat = (typeof OUTPUT_FORMATS)[number];

/** Display config for each output format. */
export const OUTPUT_FORMAT_CONFIG: Record<OutputFormat, { label: string; description: string; icon: string }> = {
  markdown: {
    label: "Markdown",
    description: "Standard Markdown with headings, bold, italic, lists, and code blocks.",
    icon: "FileText",
  },
  html: {
    label: "HTML",
    description: "HTML markup using semantic elements (headings, paragraphs, lists, code).",
    icon: "Code",
  },
  json: {
    label: "JSON",
    description: "Valid JSON objects: define a schema for structured output validation.",
    icon: "Boxes",
  },
  plain_text: {
    label: "Plain Text",
    description: "Unformatted plain text without any markup syntax.",
    icon: "FileText",
  },
};

// ── Handover types ───────────────────────────────────────────────────────────
/** Handover types configurable per target agent. */
export const HANDOVER_TYPES = ["delegate", "consult", "transfer", "mandatory_transfer"] as const;
/** Display config for each handover type. */
// `lightEdgeColor`: the edge on the light canvas, a shade that keeps 3:1
// against it at the map's rest opacity (WCAG 1.4.11);
// `lib/agent-graph-ink.ts` holds the opacity and the test of the ratio.
// `dash`: the stroke pattern of the type's edge (SVG stroke-dasharray, null
// for a solid line), read by every edge renderer and by the legend swatch, so
// two types never differ by colour alone (#9667).
// Delegate draws a fixed cyan, not the accent token: in XTM One mode the
// accent is the IA violet, which sat 8 dE76 from the transfer violet.
export const HANDOVER_TYPE_CONFIG: Record<string, { label: string; description: string; color: string; edgeColor: string; lightEdgeColor: string; dash: string | null }> = {
  delegate: {
    label: "Delegate",
    description: "Spawn a background task on the target agent (asynchronous).",
    color: "text-accent",
    edgeColor: "#22d3ee",  // cyan 400
    lightEdgeColor: "#0e7490",  // cyan 700
    dash: null,  // solid
  },
  consult: {
    label: "Consult",
    description: "Ask the target agent a question and receive its answer (synchronous, you stay in control).",
    color: "text-feedback-success-primary",
    edgeColor: "var(--color-feedback-success-primary)",  // emerald
    lightEdgeColor: "#047857",  // emerald 700
    dash: "2 3",  // dotted
  },
  transfer: {
    label: "Transfer",
    description: "Permanently hand over the conversation to the target agent: it produces the final answer and the source agent never resumes this conversation (`out` carries the target's response).",
    color: "text-filigran-ia-primary",
    edgeColor: "#a78bfa",  // violet
    lightEdgeColor: "#6d28d9",  // violet 700
    dash: "6 4",  // dashed
  },
  mandatory_transfer: {
    label: "Mandatory transfer",
    description: "Automatically transfers after the agent completes its response: the target answers and the source agent never resumes this conversation. Add a condition to route to different agents based on output.",
    color: "text-feedback-warning-primary",
    edgeColor: "#eab308",  // gold
    lightEdgeColor: "#a16207",  // gold 700
    dash: null,  // solid, told from delegate by its gold
  },
};

/** Display config of a target that runs every handover type: no target of the
 *  agent has a type yet (the legacy mode, `lib/handover-types.ts`). */
export const ALL_HANDOVER_TYPES_CONFIG = {
  label: "All types",
  description: "Legacy mode: no handover agent has a type yet, so this agent can delegate to, consult or transfer to each one. Picking a type for one of them makes the others Delegate.",
  color: "text-default-primary",
  edgeColor: "#94a3b8",  // slate
  lightEdgeColor: "#475569",  // slate 600
  dash: "6 4" as string | null,
};

/** The display config of a shown handover type, `ALL_HANDOVER_TYPES` included. */
export function handoverTypeConfig(type: string): (typeof HANDOVER_TYPE_CONFIG)[string] {
  if (type === ALL_HANDOVER_TYPES) return ALL_HANDOVER_TYPES_CONFIG;
  return HANDOVER_TYPE_CONFIG[type] ?? HANDOVER_TYPE_CONFIG.delegate;
}

/** The handover types the General Assistant implements: an agent's,
 *  without `mandatory_transfer`, which is routed after an agent's answer on a
 *  path the General Assistant never takes. Mirrors
 *  `GENERAL_ASSISTANT_HANDOVER_TYPES` on the backend, which refuses any other. */
export const GA_HANDOVER_TYPES = ["delegate", "consult", "transfer"] as const;
export type GAHandoverType = (typeof GA_HANDOVER_TYPES)[number];

/** Tool names that represent agent-to-agent delegation interactions.
 *  `handover_to_agent` is a legacy alias for `transfer_to_agent` (used by
 *  General Assistant before it was unified) — kept for backward compat
 *  with historical agentic step data. */
export const DELEGATION_TOOL_NAMES = new Set(["consult_agent", "transfer_to_agent", "handover_to_agent", "spawn_background_task"]);

/** What a user can rate, in display order, with the label the agent
 *  Feedback tab and its filter use (i18n keys). */
export const ANNOTATION_TARGET_TYPES: readonly AnnotationTargetType[] = [
  "message",
  "assignment_run",
  "sub_agent_task",
  "agent_delegation",
  "agent_account_reply",
];
export const ANNOTATION_TARGET_LABELS: Record<AnnotationTargetType, string> = {
  message: "Chat messages",
  assignment_run: "Assignment runs",
  sub_agent_task: "Background tasks",
  agent_delegation: "Delegations",
  agent_account_reply: "Agent account answers",
};
/** Who rated, when no XTM One user did: a person reacting in a platform.
 *  @i18n-keys */
export const ANNOTATION_SOURCE_RATERS = {
  teams_agent_account: "Teams reaction",
} as const;

/** Where an improvement proposal stands (#1044, #8256): its label (an i18n key) and badge. */
export const IMPROVEMENT_PROPOSAL_STATUS_CONFIG: Record<
  ImprovementProposalStatus,
  { label: string; badge: "success" | "destructive" | "cyan" | "outline" | "warning" }
> = {
  pending_approval: { label: "Waiting for approval", badge: "cyan" },
  applying: { label: "Applying", badge: "cyan" },
  shadow: { label: "Running in shadow", badge: "cyan" },
  watching: { label: "Watching", badge: "cyan" },
  rejected: { label: "Rejected", badge: "outline" },
  kept: { label: "Kept", badge: "success" },
  rolled_back: { label: "Rolled back", badge: "destructive" },
  regression_kept: { label: "Regressed, human edit kept", badge: "warning" },
  failed: { label: "No verdict", badge: "outline" },
};

/** The kinds of feedback criterion, with their label and what the
 *  person rating does with one (i18n keys). */
export const FEEDBACK_CRITERION_TYPES: readonly FeedbackCriterionType[] = ["scale", "categorical", "numeric"];
export const FEEDBACK_CRITERION_TYPE_CONFIG: Record<FeedbackCriterionType, { label: string; description: string }> = {
  scale: { label: "Scale", description: "A whole step on a short scale, such as 1 to 5." },
  categorical: { label: "Categorical", description: "One of a few options, each worth a score from 0 to 1." },
  numeric: { label: "Numeric", description: "A number between a minimum and a maximum." },
};

/** Built-in coordination tools — the durable work-item queue and the cross-run
 *  shared-state store. They are surfaced with a distinct violet
 *  accent in every tool picker (chat, agent config, built-in tools list) so
 *  they read as a special cross-run coordination category, and the backend
 *  injects a matching capability nudge into the system prompt when wired. */
const COORDINATION_BUILTIN_TOOL_IDS = new Set<string>([
  "coordination",
  // [DEPRECATED] former ids of the coordination group.
  "work_items",
  "shared_state",
  "workset",
]);

/** True when `id` is a coordination built-in tool, tolerating an optional
 *  `builtin-` prefix (the Tools list uses `builtin-work_items` ids). */
export function isCoordinationBuiltinTool(id: string): boolean {
  return COORDINATION_BUILTIN_TOOL_IDS.has(id.replace(/^builtin-/, ""));
}

// ── Built-in tool registry ───────────────────────────────────────────────────
/** What a built-in tool picker configures: an agent (its configuration, an
 *  agent chat's session tools) or the General Assistant (Settings > General
 *  Assistant, the profile override, a General Assistant chat's session tools). */
export type BuiltinToolSurface = "agent" | "general_assistant";

/** The built-in suites themselves (names, taglines, icons, flags) are served
 *  by the backend registry (`GET /settings/builtin-tools/groups`) and
 *  read through `useBuiltinTools()`. */

// ---------------------------------------------------------------------------
// Evaluations
// ---------------------------------------------------------------------------

export const EVALUATION_SUB_SCORES: { key: string; label: string; weight: number; color: string }[] = [
  { key: "tool_selection_quality", label: "Tool Selection", weight: 20, color: "text-accent" },
  { key: "response_completeness", label: "Completeness", weight: 20, color: "text-feedback-success-primary" },
  { key: "response_accuracy", label: "Accuracy", weight: 20, color: "text-feedback-warning-primary" },
  { key: "instruction_adherence", label: "Instruction Adherence", weight: 20, color: "text-filigran-ia-primary" },
  { key: "coherence", label: "Coherence", weight: 20, color: "text-feedback-info-primary" },
];

export const EVALUATION_STATUS_CONFIG: Record<string, StatusConfig> = {
  completed: { icon: CheckCircle2, color: "text-feedback-success-primary", badge: "success", label: "Completed" },
  failed: { icon: XCircle, color: "text-feedback-error-primary", badge: "destructive", label: "Failed" },
  cancelled: { icon: Ban, color: "text-default-secondary", badge: "outline", label: "Cancelled" },
  running: { icon: Loader2, color: "text-accent", badge: "cyan", label: "Running" },
  pending: { icon: Clock, color: "text-feedback-warning-primary", badge: "outline", label: "Pending" },
};

/** What started a run, by its `trigger_reason`, read by `t_i18n`. `agent_change` is
 * kept for older rows; the backend writes `on_agent_change`. @i18n-keys */
const EVALUATION_TRIGGER_LABEL_TABLE = {
  schedule: "Scheduled",
  on_agent_change: "Agent Modified",
  agent_change: "Agent Modified",
  manual: "Manual",
  persona_improvement: "Persona rewrite",
  import: "Imported",
} as const;
export const EVALUATION_TRIGGER_LABELS: Record<string, string> = EVALUATION_TRIGGER_LABEL_TABLE;

/**
 * The chart series palette. Every fixed swatch reads at 3:1 or more as a
 * graphical object on the card in both themes: the 400 steps
 * (lime #a3e635 at 1.5:1, blue, violet, pink, fuchsia near 2.5:1 on white)
 * moved to the 500/600 steps. The amber is a fixed yellow-700 rather than the
 * alert token: `--color-feedback-alert-primary` (#F2BE3A) reads
 * 1.72:1 on the light card; #a16207 reads 4.92:1 on white and 3.15:1 or more
 * on the dark page and card, and stays a yellow next to the orange warning
 * token (index 7, stacked beside it on the agent account activity chart):
 * an amber-600 (#d97706) was only CIE76 dE 8.9 from it in dark.
 * `chart-palette-contrast.test.ts` holds both.
 */
export const EVALUATION_CHART_COLORS = [
  "var(--color-accent)", // cyan
  "#a16207", // amber (yellow-700)
  "#8b5cf6", // violet
  "var(--color-feedback-success-primary)", // emerald
  "var(--color-feedback-error-primary)", // red
  "#3b82f6", // blue
  "#ec4899", // pink
  "var(--color-feedback-warning-primary)", // orange
  "#65a30d", // lime
  "#d946ef", // fuchsia
];

/**
 * The three decoupled headline metrics.
 *
 * One definition of each headline's name and colour for every page. The
 * colour marks which headline a dot, a chart line or a group title belongs to; a
 * headline's value takes its score band from `headlineColorClass` instead. The
 * colours are fixed tokens outside the band hues (green, orange, red), so a marker
 * never reads as a band, and no product mode or branding changes them. They may
 * still match the accent's default value (blue in Copilot mode, purple in XTM One).
 * `color` is for dots and chart lines, `textClass` for text.
 *
 * The mode-dependent chart chrome (`gridStroke`, `axisStroke`, `tickFill`, ...)
 * is not here: Recharts stroke/fill cannot resolve CSS `var()` tokens, so a
 * hardcoded rgba() renders invisible in light mode.
 */
export const HEADLINES = [
  {
    id: "quality" as const,
    label: HEADLINE_LABELS.quality,
    color: "var(--color-feedback-info-primary)", // blue
    textClass: "text-feedback-info-primary",
    /** Absolute: reads the same whether the run is alone or one of twenty. */
    comparative: false,
  },
  {
    id: "performance" as const,
    label: HEADLINE_LABELS.performance,
    color: "var(--color-filigran-tonic-primary)", // turquoise
    textClass: "text-filigran-tonic-primary",
    /** Normalised across this evaluation's runs — meaningless outside it. */
    comparative: true,
  },
  {
    id: "judge" as const,
    label: HEADLINE_LABELS.judge,
    color: "var(--color-filigran-ia-primary)", // IA purple: the judge is an LLM
    textClass: "text-filigran-ia-primary",
    comparative: false,
  },
];

export type HeadlineDef = (typeof HEADLINES)[number];

/** One headline's definition, by id. */
export function headlineDef(id: HeadlineDef["id"]): HeadlineDef {
  return HEADLINES.find((h) => h.id === id)!;
}

/** Colour bands shared by every headline value, on every page. Higher is always better. */
export function headlineColorClass(value: number | null | undefined): string {
  if (value == null) return "text-default-secondary";
  if (value >= 70) return "text-feedback-success-primary";
  if (value >= 40) return "text-feedback-warning-primary";
  return "text-feedback-error-primary";
}

/**
 * Non-colour series identity.
 *
 * Colour alone cannot carry identity: ten series get hues too close to tell apart,
 * and some readers cannot see colour. Each series also gets a dash pattern (trend
 * lines) and a point shape (correlation views), keyed to the same index as its
 * colour so it reads identically across all six charts.
 *
 * Beyond these lists' length the encoding repeats, and the repetition is disclosed
 * rather than shown as a unique identity.
 */
const SERIES_DASH_PATTERNS = ["", "6 3", "2 3", "8 3 2 3", "12 4", "1 4", "10 2 2 2"];
// Capped at 7: Recharts has 7 built-in Scatter point shapes, so 7 is the ceiling
// for a colour-blind-safe dash+shape identity, not the 10 colours.
const SERIES_POINT_SHAPES = ["circle", "square", "triangle", "diamond", "star", "cross", "wye"] as const;

type SeriesPointShape = (typeof SERIES_POINT_SHAPES)[number];

export interface SeriesEncoding {
  color: string;
  dash: string;
  shape: SeriesPointShape;
  /** True when this series shares its dash/shape with an earlier one. */
  encodingReused: boolean;
}

/** Resolve the full visual identity of the `index`-th series. */
export function seriesEncoding(index: number): SeriesEncoding {
  return {
    color: EVALUATION_CHART_COLORS[index % EVALUATION_CHART_COLORS.length],
    dash: SERIES_DASH_PATTERNS[index % SERIES_DASH_PATTERNS.length],
    shape: SERIES_POINT_SHAPES[index % SERIES_POINT_SHAPES.length],
    encodingReused: index >= SERIES_DASH_PATTERNS.length,
  };
}

/** Trend windows offered by the shared range control. */
export const CHART_RANGES = [
  { value: "30d", label: "Last 30 days" },
  { value: "90d", label: "Last 90 days" },
  { value: "all", label: "All time" },
];
export const DEFAULT_CHART_RANGE = "90d";

// Evaluation Pipeline v1
/** Where a run parameter came from, read by `t_i18n`. @i18n-keys */
export const PARAM_SOURCE_LABELS = {
  auto: "Auto",
  evaluation: "Evaluation",
  manual: "Manual",
} as const;
export const PARAM_SOURCE_COLORS: Record<keyof typeof PARAM_SOURCE_LABELS, string> = {
  auto: "text-default-secondary",
  evaluation: "text-accent",
  manual: "text-feedback-warning-primary",
};

// The look of a reused prediction. Its label is a `t_i18n` literal where it is shown,
// where the i18n validator reads it.
export const REUSE_BADGE_CONFIG = {
  reused: { color: "text-feedback-success-primary", bg: "bg-feedback-success-primary/10" },
};

// The look of a run that ran in LLM Only, and of an entry that fell back to it.
// Their labels are literals in `components/evaluation/run-mode-badge.tsx`,
// where the i18n validator reads them.
export const RUN_MODE_BADGE_CONFIG = {
  llmOnly: { color: "text-feedback-warning-primary", bg: "bg-feedback-alert-primary/10" },
  fallback: { color: "text-feedback-warning-primary", bg: "bg-feedback-alert-primary/10" },
};

// The look of a run that checked its prompts and answers with the prompt guard.
export const PROMPT_GUARD_BADGE_CONFIG = { color: "text-filigran-ia-primary", bg: "bg-filigran-ia-primary/10" };
// The look of a run that held tool calls needing approval, and of an entry held.
export const TOOL_APPROVAL_BADGE_CONFIG = { color: "text-feedback-warning-primary", bg: "bg-feedback-warning-primary/10" };
// Entries the platform could not run: a semantic token, so it reads in both themes.
export const PLATFORM_ERROR_BADGE_CONFIG = { color: "text-feedback-error-primary", bg: "bg-feedback-error-primary/10" };

/** The one display name of a metric key, wherever it is shown, read by `t_i18n`:
 * every page reads it through `MetricName` or `metricDisplayName`. No family
 * prefix: a list only ever shows one family's metrics. The metric package's own labels
 * say the same in English (`backend/app/evaluation_metrics`); a key missing here falls
 * back to the label the backend serves, or to the key. @i18n-keys */
export const METRIC_KEY_LABELS = {
  // Execution-level keys no longer written. Kept so older runs render a
  // name instead of a raw key.
  ttft_seconds: "TTFT (s)",
  agent_seconds: "Agent Time (s)",
  input_tokens: "Input Tokens",
  output_tokens: "Output Tokens",
  llm_iterations: "LLM Iterations",
  tool_calls: "Tool Calls",
  cost_usd: "Cost (USD)",
  // Still written: the package has no equivalent for the judge timings.
  judge_seconds: "Judge Time (s)",
  run_duration_seconds: "Run Duration (s)",
  // Superseded by `run_duration_seconds`. Kept so older runs render a name.
  total_seconds: "Total Time (s)",
  // Not written any more. The label names stored `evaluation_metrics`
  // rows, which remain.
  schema_score: "Schema Score",
  // Package-sourced performance metrics, namespaced ``performance.*`` so
  // they never collide with the execution or quality (``json.*``/``stix.*``) keys.
  "performance.latency.wall_clock": "End-to-End Latency",
  "performance.latency.ttft": "Time to First Token",
  "performance.counts.model_calls": "Model Calls",
  // Same label as `tool_calls` on purpose: no result can carry both keys. Do not
  // add a `(perf)`/`(legacy)` marker to separate them.
  "performance.counts.tool_calls": "Tool Calls",
  "performance.counts.retries": "Retries",
  "performance.tokens.total": "Total Tokens",
  "performance.cost.llm": "Estimated LLM Cost",
  "performance.reliability.completion_rate": "Completion Rate",
  "performance.reliability.tool_error_rate": "Tool Error Rate",
  // Removed from the package in suite 6.0.0. Kept so older runs render a name.
  "performance.counts.iterations": "Iterations",
  "performance.reliability.error_rate": "Error Rate",
  "performance.reliability.timeout_rate": "Timeout Rate",
  "performance.reliability.cancelled_rate": "Cancelled Rate",
  // Quality metrics.
  "stix.entity_prf": "Entity F1",
  "stix.entity_prf.by_type": "Entity F1, Averaged per Entity Type",
  "stix.relationship_prf": "Relationship F1",
  "stix.entity_prf.aligned": "Entity F1, Aligned",
  "stix.entity_prf.soft": "Entity F1, Soft",
  "stix.relationship_prf.aligned": "Relationship F1, Aligned",
  "stix.entity_prf.aligned.closed": "Entity F1, Aligned, Dataset Types",
  "stix.relationship_prf.aligned.closed": "Relationship F1, Aligned, Dataset Types",
  "stix.envelope_validity": "Readable Bundle",
  "stix.semantic_validity": "Valid STIX 2.1",
  "classification.micro_prf": "Label F1, All Labels Pooled (Micro)",
  "classification.macro_prf": "Label F1, Averaged per Label (Macro)",
  "classification.samples_f1": "Label F1, Averaged per Entry (Samples)",
  "classification.subset_accuracy": "Exact Label Set",
  "classification.vocabulary_validity": "Only Known Labels",
  "prose.exact_match": "Exact Match (Normalised)",
  "prose.token_prf": "Word Overlap F1",
  "prose.rouge_l": "Word Sequence Overlap (ROUGE-L)",
  "prose.required_phrase_coverage": "Required Phrases Present",
  "prose.forbidden_phrase_absence": "Forbidden Phrases Absent",
  "json.schema_validity": "Schema Validity",
  "json.field_accuracy": "Field Accuracy",
} as const satisfies Record<string, string>;

/** What a metric measures, in one line: the tooltip on its name wherever it is shown.
 * A key missing here shows its name alone. Exported for
 * `backend/tests/test_evaluation_metric_catalog.py`, which requires one per package metric.
 * @i18n-keys @public */
export const METRIC_KEY_DESCRIPTIONS = {
  judge_seconds: "Time the LLM judge took to score the entry.",
  run_duration_seconds: "Time spent on the entry: the agent's steps and the LLM judge. On a reused or rescored entry, the judge only. The run header's Duration leaves the judge out.",
  "performance.latency.wall_clock": "Time from sending the entry to the agent's answer.",
  "performance.latency.ttft": "Time until the agent's first streamed token.",
  "performance.counts.model_calls": "LLM calls the agent made for the entry.",
  "performance.counts.tool_calls": "Tool calls the agent made for the entry.",
  "performance.counts.retries": "Calls the agent retried after a failure.",
  "performance.tokens.total": "Tokens the agent used for the entry, input and output.",
  "performance.cost.llm": "The agent's LLM cost for the entry, estimated from its tokens and the model's prices.",
  "performance.reliability.completion_rate": "Share of the agent's model and tool calls that succeeded.",
  "performance.reliability.tool_error_rate": "Share of the agent's tool calls that returned an error.",
  "stix.entity_prf": "Expected STIX objects found in the answer, extra ones penalised (F1), counted over every entry.",
  "stix.entity_prf.by_type": "The entity F1 of each entity type (malware, indicator…), then averaged: a rare type weighs as much as a common one.",
  "stix.relationship_prf": "Expected relationships (source, type, target) found in the answer, extra ones penalised (F1).",
  "stix.entity_prf.aligned": "Expected objects found once each answer object is paired one-to-one with the reference object it names: aliases, spelling variants and close types (intrusion set / threat actor) count (F1).",
  "stix.entity_prf.soft": "Like the aligned entity F1, with partial credit: a close name or a confusable type (malware / tool, sector / organization, country / region) earns part of a match.",
  "stix.relationship_prf.aligned": "Expected relationships found once each endpoint is read as the reference object it was paired with, whichever of its mentions or names the answer used (F1).",
  "stix.entity_prf.aligned.closed": "The aligned entity F1 counting only the object types the dataset's references annotate: a type its guideline never records is neither a hit nor an error. Read beside the aligned F1, never instead of it.",
  "stix.relationship_prf.aligned.closed": "The aligned relationship F1 counting only relationships between object types the dataset's references annotate; not scored when the dataset annotates no relationship. Read beside the aligned F1, never instead of it.",
  "stix.envelope_validity": "Share of answers that are a STIX bundle with a list of objects. Any other answer scores 0 on the F1 metrics.",
  "stix.semantic_validity": "Share of answers that pass full STIX 2.1 validation.",
  "classification.micro_prf": "Expected labels found, extra ones penalised (F1), counted over every label of every entry.",
  "classification.macro_prf": "The F1 of each label across entries, then averaged: a rare label weighs as much as a common one.",
  "classification.samples_f1": "The F1 of each entry's labels (found, missed, extra), then averaged over entries.",
  "classification.subset_accuracy": "Share of entries whose labels are exactly the expected ones, none missing and none extra.",
  "classification.vocabulary_validity": "Share of entries whose labels are all in the dataset's label list.",
  "prose.exact_match": "Share of answers identical to the reference, ignoring case, spacing and the punctuation around words.",
  "prose.token_prf": "Words of the reference found in the answer, extra words penalised (F1).",
  "prose.rouge_l": "How much of the reference appears in the answer in the same order: the longest common word sequence.",
  "prose.required_phrase_coverage": "Share of the required phrases the answer contains.",
  "prose.forbidden_phrase_absence": "Share of the forbidden phrases the answer avoids.",
  "json.schema_validity": "Share of answers valid against the dataset's JSON schema.",
  "json.field_accuracy": "Weighted share of the reference's fields the answer gets right, each compared as its schema declares it.",
  // Keys older runs recorded and no run writes any more: what they measured then.
  ttft_seconds: "Recorded by older runs: time until the agent's first streamed token.",
  agent_seconds: "Recorded by older runs: the agent's working time on the entry, summed over its steps.",
  total_seconds: "Recorded by older runs: the agent's and the LLM judge's time on the entry added together.",
  input_tokens: "Recorded by older runs: tokens the agent's model calls read for the entry.",
  output_tokens: "Recorded by older runs: tokens the agent's model calls wrote for the entry.",
  llm_iterations: "Recorded by older runs: model calls the agent made for the entry.",
  tool_calls: "Recorded by older runs: tool calls the agent made for the entry.",
  cost_usd: "Recorded by older runs: the agent's LLM cost for the entry, estimated from its tokens; a model with no price counted 0.",
  schema_score: "Recorded by older runs: how far the answer met the entry's output schema (a JSON schema, a pattern or a tool sequence), from 0 to 100.",
  "performance.counts.iterations": "Recorded by older runs: the agent's model calls for the entry, the same count as Model Calls.",
  "performance.reliability.error_rate": "Recorded by older runs: share of the agent's model and tool calls that ended in an error.",
  "performance.reliability.timeout_rate": "Recorded by older runs: share of the agent's model and tool calls that timed out.",
  "performance.reliability.cancelled_rate": "Recorded by older runs: share of the agent's model and tool calls that were cancelled.",
} as const satisfies Partial<Record<keyof typeof METRIC_KEY_LABELS, string>>;

/** A metric's name on a single entry, where it means something narrower than over a run:
 * the package's `entry_label`, which the API serves on an entry's metrics.
 * `backend/tests/test_evaluation_metric_catalog.py` requires the two to match.
 * @i18n-keys @public */
export const METRIC_ENTRY_LABELS = {
  "classification.micro_prf": "Label F1",
} as const satisfies Partial<Record<keyof typeof METRIC_KEY_LABELS, string>>;

/** What a metric measures on a single entry: the tooltip on its entry name.
 * @i18n-keys @public */
export const METRIC_ENTRY_DESCRIPTIONS = {
  "classification.micro_prf": "Expected labels found on this entry, extra ones penalised (F1).",
} as const satisfies Record<keyof typeof METRIC_ENTRY_LABELS, string>;

// Own-property checks: `TABLE["constructor"]` would otherwise resolve up the prototype chain.
function ownEntry(table: Record<string, string>, key: string): string | null {
  return Object.prototype.hasOwnProperty.call(table, key) ? table[key] : null;
}

/** The display name of `key` (an untranslated catalog key), or `null` when it has none. */
export function metricKeyLabel(key: string): string | null {
  return ownEntry(METRIC_KEY_LABELS, key);
}

/** What `key` measures (an untranslated catalog key), or `null` when it has no description. */
export function metricKeyDescription(key: string): string | null {
  return ownEntry(METRIC_KEY_DESCRIPTIONS, key);
}

/** The name `key` takes on a single entry (an untranslated catalog key), or `null`. */
function metricEntryLabel(key: string): string | null {
  return ownEntry(METRIC_ENTRY_LABELS, key);
}

/** What `key` measures on a single entry (an untranslated catalog key), or `null`. */
export function metricEntryDescription(key: string): string | null {
  return ownEntry(METRIC_ENTRY_DESCRIPTIONS, key);
}

/** A metric's name in the reader's language on a single entry. When the API says it
 * has an entry name (`entryLabel`): the table's, translated, else the API's as served, like
 * `metricDisplayName`'s fallback. Otherwise its own name. */
export function metricEntryDisplayName(
  key: string,
  t_i18n: (id: string) => string,
  entryLabel?: string | null,
  fallback?: string | null,
): string {
  if (!entryLabel) return metricDisplayName(key, t_i18n, fallback);
  const label = metricEntryLabel(key);
  return label ? t_i18n(label) : entryLabel;
}

/** A metric's name in the reader's language: the table's, else the backend's label
 * (`fallback`), else the key. */
export function metricDisplayName(key: string, t_i18n: (id: string) => string, fallback?: string | null): string {
  const label = metricKeyLabel(key);
  return label ? t_i18n(label) : fallback || key;
}

export const RUN_TAG_BASELINE_KEY = "baseline";

export const SHARE_PERMISSIONS = ["read_only", "config", "config_with_chats"] as const;
export const SHARE_PERMISSION_LABELS: Record<string, string> = {
  read_only: "Read only",
  config: "Can configure",
  config_with_chats: "Full access (incl. chats)",
};
export const SHARE_PERMISSION_DESCRIPTIONS: Record<string, string> = {
  read_only: "View and use, but cannot edit configuration",
  config: "View, use, and edit configuration",
  config_with_chats: "Full access including other users' chat history and the agent's traces",
};
/**
 * The same two tiers, worded for a presentation. A deck has no
 * "configuration": `read_only` is a reviewer (open, present, export, duplicate,
 * comment) and `config` is an editor (also change it — in the editor or through
 * their own assistant, which resolves the deck by id). Mirrors
 * `deck_permission()` in `services/presentations.py`.
 */
export const PRESENTATION_SHARE_PERMISSION_LABELS: Record<string, string> = {
  read_only: "Can view",
  config: "Can edit",
};
export const PRESENTATION_SHARE_PERMISSION_DESCRIPTIONS: Record<string, string> = {
  read_only: "Open, present, export, duplicate and comment",
  config: "Also edit it here or through their own assistant",
};
/** A custom dashboard's three tiers, as its Share dialog says them (the strongest stored as
 *  `config_with_chats`: OpenCTI's "admin" access, `custom_dashboards.dashboard_permission`). @i18n-keys */
export const DASHBOARD_SHARE_PERMISSION_LABELS = {
  read_only: "Can view",
  config: "Can edit",
  config_with_chats: "Can manage",
} as const;
/** What each tier lets a dashboard's grantee do. @i18n-keys */
export const DASHBOARD_SHARE_PERMISSION_DESCRIPTIONS = {
  read_only: "Open it, with live figures read with their own access",
  config: "Also rearrange, refine and share it, here or through their own assistant",
  config_with_chats: "Also delete it",
} as const;
/**
 * The same two tiers, worded for a video: `read_only` watches,
 * downloads and duplicates it; `config` also edits, renders and shares it - a
 * render becomes what the video plays, so it is an edit. Mirrors
 * `projects.video_permission()` and the `share_video` tool's wording.
 * @i18n-keys
 */
export const VIDEO_SHARE_PERMISSION_LABELS = {
  read_only: "Can view",
  config: "Can edit",
} as const;
/** What each tier lets a video's grantee do, translated where the picker renders it. @i18n-keys */
export const VIDEO_SHARE_PERMISSION_DESCRIPTIONS = {
  read_only: "Watch, download and duplicate",
  config: "Also edit, render and share it, here or through their own assistant",
} as const;
/**
 * The same two tiers, said for a workspace: a `config` grantee may
 * FILE conversations into it - their agents then write its work items and
 * shared state - which a `read_only` one may not.
 * @i18n-keys
 */
export const WORKSPACE_SHARE_PERMISSION_DESCRIPTIONS = {
  read_only: "See it and what its agents built there",
  config: "Also file their conversations into it, change, share and delete it",
} as const;

// ── CTEM Command Center ───────────────────────────────────────────────────────
/** The five CTEM lifecycle stages, in canonical order. Mirrors STAGE_KEYS in
 *  backend/app/services/ctem_scoring.py. */
export const CTEM_STAGE_KEYS = [
  "scoping",
  "discovery",
  "prioritization",
  "validation",
  "remediation",
] as const;
export type CtemStageKey = (typeof CTEM_STAGE_KEYS)[number];

/** CTEM verticals for the per-user dashboard switcher. Cyber is the default;
 *  FIMI and fraud reshape the KPIs, agents and briefing for that user. */
export const CTEM_VERTICALS = ["cyber", "fimi", "fraud"] as const;
export type CtemVerticalKey = (typeof CTEM_VERTICALS)[number];

export const CTEM_VERTICAL_CONFIG: Record<
  CtemVerticalKey,
  { label: string; short: string; icon: string; tagline: string }
> = {
  cyber: {
    label: "Cyber",
    short: "Cyber",
    icon: "ShieldCheck",
    tagline: "Cyber attack-surface exposure",
  },
  fimi: {
    label: "FIMI / Disinformation",
    short: "FIMI",
    icon: "Megaphone",
    tagline: "Foreign information manipulation & interference",
  },
  fraud: {
    label: "Fraud",
    short: "Fraud",
    icon: "Banknote",
    tagline: "Digital fraud, phishing & brand abuse",
  },
};

/** Per-stage display config: icon (Lucide name), accent color, tagline, the
 *  products that feed it, and the agent name keywords used to weave the agents
 *  working each stage into the dashboard. */
export const CTEM_STAGE_CONFIG: Record<
  CtemStageKey,
  {
    label: string;
    icon: string;
    color: string;
    tagline: string;
    products: string[];
    agentKeywords: string[];
  }
> = {
  scoping: {
    label: "Scoping",
    icon: "ScanEye",
    color: "text-feedback-info-primary",
    tagline: "Attack surface in scope",
    products: ["opencti", "openaev"],
    agentKeywords: ["asset", "endpoint", "scope", "inventory", "surface"],
  },
  discovery: {
    label: "Discovery",
    icon: "Radar",
    color: "text-accent",
    tagline: "Threats & detection content",
    products: ["opencti"],
    agentKeywords: ["cti", "threat", "intel", "opencti", "harvest", "stix", "hunt", "indicator", "detection"],
  },
  prioritization: {
    label: "Prioritization",
    icon: "ShieldAlert",
    color: "text-feedback-warning-primary",
    tagline: "What matters most",
    products: ["opencti", "openaev"],
    agentKeywords: ["vuln", "cve", "risk", "prioriti", "exposure"],
  },
  validation: {
    label: "Validation",
    icon: "FlaskConical",
    color: "text-filigran-ia-primary",
    tagline: "Detections in place, proven by emulation",
    products: ["opencti", "openaev"],
    agentKeywords: ["openaev", "bas", "simulation", "scenario", "attack", "emulation", "payload", "inject", "adversary"],
  },
  remediation: {
    label: "Remediation",
    icon: "Wrench",
    color: "text-feedback-success-primary",
    tagline: "Closing the loop",
    products: ["opencti", "openaev"],
    agentKeywords: ["remediat", "case", "incident", "response", "mitigat", "fix"],
  },
};

/** Posture / sub-score band display config. Mirrors BANDS in ctem_scoring.py.
 *  Every band is a level on the red to green scale, so none is drawn in the
 *  accent: the accent is the brand (purple in XTM One, blue in Copilot) and the
 *  colour of links, buttons and data bars, and a Good ring in it read as brand
 *  (#11661). Good takes the design system's turquoise (tonic) family: the ring
 *  is tonic-primary (turquoise-500 dark, 12:1 on the card; turquoise-700 light,
 *  3.8:1 on the card, 3.4:1 on the page) and the name tonic-accent
 *  (turquoise-500 dark; turquoise-800 light, 8.6:1). */
export const CTEM_BAND_CONFIG: Record<
  string,
  { label: string; color: string; ring: string }
> = {
  critical: { label: "Critical", color: "text-feedback-error-primary", ring: "var(--color-feedback-error-primary)" },
  at_risk: { label: "At risk", color: "text-feedback-warning-primary", ring: "var(--color-feedback-warning-primary)" },
  fair: { label: "Fair", color: "text-feedback-warning-primary", ring: "var(--color-feedback-alert-primary)" },
  good: { label: "Good", color: "text-filigran-tonic-accent", ring: "var(--color-filigran-tonic-primary)" },
  strong: { label: "Strong", color: "text-feedback-success-primary", ring: "var(--color-feedback-success-primary)" },
  unknown: { label: "No data", color: "text-default-secondary", ring: "#475569" },
};

/** Human-readable CTEM score-band scale, shared by the stage-score tooltip and
 *  the Exposure Posture explanation dialog. Mirrors the BANDS in ctem_scoring.py
 *  (critical < 40, at_risk < 60, fair < 75, good < 90, strong >= 90). */
export const CTEM_BAND_SCALE_TEXT =
  "0-100, higher is better: under 40 Critical, 40-59 At risk, 60-74 Fair, 75-89 Good, 90+ Strong.";

/** Priority of a CTEM briefing next step or recommended action: its label and
 *  the ToneBadge tone it is drawn in (#1354), so the High / Medium / Low chip
 *  wears the single chip style of every other CTEM chip (design system 1.13.0,
 *  #1595) and looks identical on the Exposure Briefing page,
 *  the briefing card, Act now and Recommended actions. The labels are context
 *  keys: the bare "Medium" key means "medium term" in fr, es and ja. */
export const CTEM_PRIORITY_CONFIG: Record<string, { label: string; tone: HotTone }> = {
  high: { label: "High (priority)", tone: "red" },
  medium: { label: "Medium (priority)", tone: "orange" },
  low: { label: "Low (priority)", tone: "neutral" },
};

/** Products that feed the CTEM command center. OpenCRQ is registration-driven
 *  like the others (the Filigran license carries `sub_licenses.opencrq_ids`); its
 *  dashboard pill shows a "Soon" placeholder only until an instance registers.
 *
 *  NOTE on connecting: OpenCTI / OpenAEV / OpenCRQ are `registration_only` - they are
 *  NOT connected from XTM One; the platform registers itself inbound by adding
 *  the XTM One URL + registration token to its own config. `productUrl` is only
 *  the public product page used as the secondary "don't have it yet? explore"
 *  link inside the connect dialog - never as the connect action itself. */
export const CTEM_PRODUCT_INFO: Record<
  string,
  { name: string; logo: string; desc: string; productUrl: string }
> = {
  opencti: {
    name: "OpenCTI",
    logo: withBasePath("/logos/opencti.svg"),
    desc: "Threat Intelligence",
    productUrl: "https://filigran.io/platform/opencti/",
  },
  openaev: {
    name: "OpenAEV",
    logo: withBasePath("/logos/openaev.svg"),
    desc: "Exposure Validation",
    productUrl: "https://filigran.io/platform/openaev/",
  },
  opencrq: {
    name: "OpenCRQ",
    logo: withBasePath("/logos/opencrq.svg"),
    desc: "Governance, Risk & Compliance",
    productUrl: "https://filigran.io/platform/",
  },
};

/** Per-(vertical, stage, product) DEEP LINK into the connected platform, so the
 *  stage footer lands on the DEDICATED resource page that backs the stage - not
 *  the product home. ``path`` is appended to the integration ``base_url``
 *  (OpenCTI lives under ``/dashboard``, OpenAEV under ``/admin``); ``resource``
 *  is the human label. Paths verified against both products' route definitions.
 *  When a (vertical, stage, product) has no dedicated page the footer link is
 *  simply omitted rather than pointing at a generic home page. */
type CtemDeepLinkEntry = { path: string; resource: string };
const CTEM_DEEP_LINKS: Record<
  string,
  Record<string, Partial<Record<"opencti" | "openaev", CtemDeepLinkEntry>>>
> = {
  cyber: {
    scoping: {
      openaev: { path: "/admin/assets", resource: "Assets" },
      opencti: { path: "/dashboard/observations/observables", resource: "Observables" },
    },
    discovery: {
      opencti: { path: "/dashboard/techniques/attack_patterns", resource: "ATT&CK techniques" },
      openaev: { path: "/admin/simulations", resource: "Simulations" },
    },
    prioritization: {
      opencti: { path: "/dashboard/arsenal/vulnerabilities", resource: "Vulnerabilities" },
      openaev: { path: "/admin/findings", resource: "Findings" },
    },
    validation: {
      openaev: { path: "/admin/simulations", resource: "Simulations" },
      opencti: { path: "/dashboard/techniques/attack_patterns", resource: "ATT&CK techniques" },
    },
    remediation: {
      opencti: { path: "/dashboard/cases/incidents", resource: "Incident cases" },
      openaev: { path: "/admin/findings", resource: "Findings" },
    },
  },
  fimi: {
    scoping: { opencti: { path: "/dashboard/techniques/narratives", resource: "Narratives" } },
    discovery: { opencti: { path: "/dashboard/arsenal/channels", resource: "Channels" } },
    prioritization: { opencti: { path: "/dashboard/arsenal/channels", resource: "Channels" } },
    validation: { opencti: { path: "/dashboard/arsenal/channels", resource: "Channels" } },
    remediation: { opencti: { path: "/dashboard/cases/incidents", resource: "Incident cases" } },
  },
  fraud: {
    scoping: { opencti: { path: "/dashboard/entities/organizations", resource: "Brands" } },
    discovery: { opencti: { path: "/dashboard/observations/observables", resource: "Observables" } },
    prioritization: { opencti: { path: "/dashboard/observations/observables", resource: "Observables" } },
    validation: {
      opencti: { path: "/dashboard/observations/indicators", resource: "Indicators" },
      openaev: { path: "/admin/simulations", resource: "Phishing simulations" },
    },
    remediation: { opencti: { path: "/dashboard/cases/rfts", resource: "Takedowns" } },
  },
};

/** Provenance for every dashboard metric: WHERE the number comes from and HOW it
 *  is computed, so each metric can carry a clickable info icon (full
 *  self-explanation, no blind spot). Keyed by the metric's display label (the
 *  same label rendered on the KPI strip + stage cards + stage detail). ``source``
 *  is one of: OpenCTI, OpenAEV, "OpenCTI + OpenAEV", Derived, XTM One. */
export interface CtemMetricInfo {
  source: "OpenCTI" | "OpenAEV" | "OpenCTI + OpenAEV" | "Derived" | "XTM One";
  how: string;
}
export const CTEM_METRIC_INFO: Record<string, CtemMetricInfo> = {
  // ── Top KPI strip ──────────────────────────────────────────────────────────
  "Exposure posture": {
    source: "Derived",
    how: "Weighted average of the five CTEM stage scores (Scoping, Discovery, Prioritization, Validation, Remediation), renormalized over the stages that actually have data. 0-100, higher is better.",
  },
  "Known-exploited": {
    source: "OpenCTI",
    how: "Count of Vulnerabilities flagged as CISA KEV (known-exploited in the wild) in OpenCTI (x_opencti_cisa_kev = true).",
  },
  "Detection coverage": {
    source: "OpenCTI",
    how: "Share of MITRE ATT&CK techniques covered by at least one still-active (non-revoked) OpenCTI indicator or a detection (indicates / detects relationships). Revoked indicators no longer count.",
  },
  "Validation success": {
    source: "OpenAEV",
    how: "Combined prevention + detection success rate across your finished OpenAEV simulations in the last 3 months (each simulation's expectation results), matching the OpenAEV home dashboard.",
  },
  Agents: {
    source: "XTM One",
    how: "Number of active agents configured on this platform, with their active assignments.",
  },
  "Agentic executions": {
    source: "XTM One",
    how: "Assignment runs executed across all your agents in the last 30 days.",
  },
  // ── Cyber - Scoping ─────────────────────────────────────────────────────────
  "Scope breadth": {
    source: "OpenAEV",
    how: "Share of OpenAEV scope dimensions in place - managed endpoints, asset groups, detection sources (security platforms) and attack domains with validation content. Blended with OpenCTI scope completeness to score Scoping, so a rich inventory is credited even before BAS attack-domain content exists.",
  },
  "Attack surface coverage": {
    source: "OpenAEV",
    how: 'Share of OpenAEV\'s core ATT&CK attack domains (Endpoint, Network, Web App, E-mail, Data Exfiltration, URL Filtering, Cloud) that have at least one injector contract - OpenAEV\'s "security domain power". Agent-agnostic: it does not depend on endpoint agents, since agentless assets (network/email/web targets) are a legitimate part of the attack surface.',
  },
  "Endpoints in scope": {
    source: "OpenAEV",
    how: "Total endpoints known to OpenAEV (POST /api/endpoints/search totalElements). Includes agentless assets - a valid, in-scope part of the attack surface.",
  },
  "Asset groups": {
    source: "OpenAEV",
    how: "Number of asset groups defined in OpenAEV (POST /api/asset_groups/search totalElements) - how the attack surface is organised into scope buckets.",
  },
  "Attack domains covered": {
    source: "OpenAEV",
    how: "How many of the 7 core attack domains have validation content (POST /api/injector_contracts/domain-counts). The agent-agnostic breadth of the validated attack surface.",
  },
  "Security platforms": {
    source: "OpenAEV",
    how: "Detection / prevention products registered in OpenAEV (POST /api/security_platforms/search totalElements) - the detection sources backing validation.",
  },
  "CVEs tracked": {
    source: "OpenCTI",
    how: "Total Vulnerability entities tracked in OpenCTI.",
  },
  Observables: {
    source: "OpenCTI",
    how: "Total cyber observables in OpenCTI (stixCyberObservablesNumber).",
  },
  "Scope completeness": {
    source: "Derived",
    how: "How many of the core OpenCTI scope dimensions (observables, CVEs, indicators, reports) are present. Blended with OpenAEV scope breadth to score Scoping (used alone when no OpenAEV BAS platform is connected).",
  },
  // ── Cyber - Discovery ─────────────────────────────────────────────────────────
  "Threat-informed coverage": {
    source: "OpenCTI + OpenAEV",
    how: "Of the MITRE ATT&CK techniques the threats OpenCTI tracks actually USE (intrusion sets / malware / campaigns via 'uses' relationships, matched by MITRE T-code), the share your OpenAEV simulations exercise. The untested techniques are the gap.",
  },
  "Threat techniques": {
    source: "OpenCTI",
    how: "Distinct MITRE ATT&CK techniques OpenCTI tracks as used by adversaries - Attack-Pattern entities linked via a 'uses' relationship (intrusion sets, malware, campaigns, threat actors and their tooling).",
  },
  "Simulated techniques": {
    source: "OpenAEV",
    how: "Distinct MITRE ATT&CK techniques exercised by your OpenAEV simulations (by MITRE T-code).",
  },
  "Untested techniques": {
    source: "OpenCTI + OpenAEV",
    how: "Threat techniques known to OpenCTI that your OpenAEV simulations have NOT exercised - the threat-informed defense gap to close.",
  },
  "Intrusion sets": {
    source: "OpenCTI",
    how: "Intrusion Set entities tracked in OpenCTI.",
  },
  Malware: {
    source: "OpenCTI",
    how: "Malware entities tracked in OpenCTI.",
  },
  "Valid indicators": {
    source: "OpenCTI",
    how: "Indicators in OpenCTI that are not revoked (still valid).",
  },
  // ── Cyber - Prioritization ────────────────────────────────────────────────────
  "Known-exploited (KEV)": {
    source: "OpenCTI",
    how: "Vulnerabilities flagged as CISA KEV (known-exploited in the wild) in OpenCTI.",
  },
  "Critical CVEs": {
    source: "OpenCTI",
    how: "Vulnerabilities with a CVSS base score >= 9 in OpenCTI.",
  },
  "High-EPSS CVEs": {
    source: "OpenCTI",
    how: "Vulnerabilities with an EPSS exploitation probability > 0.5 in OpenCTI.",
  },
  "Indicator sightings": {
    source: "OpenCTI",
    how: "Total sightings of your indicators - how often tracked threats were actually OBSERVED in your environment (real activity vs theoretical intel).",
  },
  "Priority intel requirements": {
    source: "OpenCTI",
    how: "Number of Priority Intelligence Requirements (PIR) defined in OpenCTI - your collection / prioritization focus. Enterprise Edition; omitted when unavailable.",
  },
  "Threats in PIR scope": {
    source: "OpenCTI",
    how: "Threat actors (groups & individuals), intrusion sets and campaigns flagged by your Priority Intelligence Requirements - the prioritized adversaries actively tracked against intel requirements (summed across PIRs). Enterprise Edition; omitted when unavailable.",
  },
  "Total CVEs": {
    source: "OpenCTI",
    how: "Total Vulnerability entities tracked in OpenCTI.",
  },
  Findings: {
    source: "OpenAEV",
    how: "Total findings surfaced by your latest OpenAEV simulations.",
  },
  // ── Cyber - Validation ─────────────────────────────────────────────────────────
  "Prevention success": {
    source: "OpenAEV",
    how: "Share of PREVENTION expectations that succeeded across your finished simulations in the last 3 months (the simulated attack was blocked). Matches the OpenAEV home donut.",
  },
  "Detection success": {
    source: "OpenAEV",
    how: "Share of DETECTION expectations that succeeded across your finished simulations in the last 3 months (the simulated attack was detected / alerted). Matches the OpenAEV home donut.",
  },
  "ATT&CK exercised": {
    source: "OpenAEV",
    how: "Share of the MITRE ATT&CK technique catalog exercised across all your simulations (distinct techniques run / total attack patterns known to OpenAEV). Cumulative, not limited to the 3-month window.",
  },
  "Validated techniques": {
    source: "OpenAEV",
    how: "Distinct MITRE techniques with at least one successful prevention or detection result across all your simulations (cumulative).",
  },
  "Finished simulations": {
    source: "OpenAEV",
    how: "Number of finished OpenAEV simulations in the last 3 months (the validation window).",
  },
  "Detection sources": {
    source: "OpenAEV",
    how: "Security platforms (EDR / XDR / SIEM / ...) configured in OpenAEV - the depth of the detection stack backing validation.",
  },
  "Active collectors": {
    source: "OpenAEV",
    how: "OpenAEV collectors currently running or recently executed (the detection-ingestion sources).",
  },
  "Detection rule coverage": {
    source: "OpenCTI",
    how: "Share of ATT&CK techniques with an OpenCTI detection rule (detects). Fallback readiness signal when no OpenAEV BAS data is available.",
  },
  // ── Cyber - Remediation ───────────────────────────────────────────────────────
  "Case closure": {
    source: "OpenCTI",
    how: "Share of OpenCTI response cases (incident + request-for-information) whose workflow status is resolved / closed.",
  },
  "Incident cases": {
    source: "OpenCTI",
    how: "Case-Incident entities in OpenCTI.",
  },
  "RFI cases": {
    source: "OpenCTI",
    how: "Case-RFI (request for information) entities in OpenCTI.",
  },
  "Automation playbooks": {
    source: "OpenCTI",
    how: "Running OpenCTI automation playbooks (response automation). Requires the AUTOMATION capability; omitted when unavailable.",
  },
  "Open findings": {
    source: "OpenAEV",
    how: "Open findings from your OpenAEV simulations - the remediation backlog.",
  },
  "Failed/partial validations": {
    source: "OpenAEV",
    how: "Prevention / detection expectations that failed or only partially succeeded across simulations - the controls to fix.",
  },
  // ── FIMI ───────────────────────────────────────────────────────────────────────
  "Information-space coverage": {
    source: "Derived",
    how: "How many of the core information-space dimensions (narratives, channels, languages, events) are tracked in OpenCTI.",
  },
  "Narratives tracked": { source: "OpenCTI", how: "Narrative entities tracked in OpenCTI." },
  "Channels monitored": { source: "OpenCTI", how: "Channel entities monitored in OpenCTI." },
  Events: { source: "OpenCTI", how: "Event entities tracked in OpenCTI." },
  Languages: { source: "OpenCTI", how: "Distinct languages tracked across OpenCTI content." },
  "Monitoring depth": {
    source: "Derived",
    how: "How much media content is captured and the diversity of channel platforms + sub-narrative structure under watch in OpenCTI.",
  },
  "Media content": { source: "OpenCTI", how: "Media-Content entities captured in OpenCTI." },
  "Channel platforms": { source: "OpenCTI", how: "Distinct channel platform types tracked in OpenCTI." },
  Narratives: { source: "OpenCTI", how: "Narrative entities tracked in OpenCTI (count of distinct disinformation narratives)." },
  "Attributed channels": {
    source: "OpenCTI",
    how: "Share of monitored channels attributed to a threat actor / intrusion set (belongs-to), vs anonymous noise.",
  },
  "Channels attributed": {
    source: "OpenCTI",
    how: "Channels linked to a threat actor / intrusion set via a belongs-to relationship in OpenCTI.",
  },
  "Election events": { source: "OpenCTI", how: "Events whose type references an election in OpenCTI." },
  "Corroborated channels": {
    source: "OpenCTI",
    how: "Share of channels with mapped publishing infrastructure (domains / URLs), evidencing real operations.",
  },
  // KPI-strip label (the per-stage card uses "Corroborated channels").
  Corroborated: {
    source: "OpenCTI",
    how: "Share of monitored channels with mapped publishing infrastructure (domains / URLs) in OpenCTI, evidencing real operations behind the narrative.",
  },
  "Channels with infrastructure": {
    source: "OpenCTI",
    how: "Channels that publish to domains / URLs / hostnames (publishes relationship) in OpenCTI.",
  },
  Incidents: { source: "OpenCTI", how: "Incident entities in OpenCTI." },
  "Open incidents": { source: "OpenCTI", how: "Incident entities in OpenCTI (open response work)." },
  // ── Fraud ───────────────────────────────────────────────────────────────────────
  "Brand & asset coverage": {
    source: "Derived",
    how: "How many of the core fraud-scope dimensions (brands, watched domains, impersonation personas) are present in OpenCTI.",
  },
  "Brands monitored": { source: "OpenCTI", how: "Organization entities monitored as brands in OpenCTI." },
  "Domains watched": { source: "OpenCTI", how: "Domain-Name observables watched in OpenCTI." },
  "Impersonation personas": { source: "OpenCTI", how: "Persona entities tracked in OpenCTI." },
  "Fraud surface discovered": {
    source: "Derived",
    how: "How much of the fraud surface (phishing domains / URLs, leaked credentials & cards, phishing infrastructure) has been discovered in OpenCTI.",
  },
  // KPI-strip label (the per-stage card uses "Phishing domains/URLs").
  "Phishing assets": {
    source: "OpenCTI",
    how: "Phishing domains, URLs and hostnames discovered in the wild (Domain-Name + Url + Hostname observables in OpenCTI).",
  },
  "Phishing domains/URLs": { source: "OpenCTI", how: "Domain-Name + Url + Hostname observables in OpenCTI." },
  "Leaked credentials": { source: "OpenCTI", how: "Credential observables in OpenCTI." },
  "Leaked cards": { source: "OpenCTI", how: "Payment-Card observables in OpenCTI." },
  "Phishing infrastructure": {
    source: "OpenCTI",
    how: "Infrastructure entities typed as phishing in OpenCTI.",
  },
  Containment: {
    source: "Derived",
    how: "Share of fraud indicators that have been neutralised (revoked) vs still live in OpenCTI.",
  },
  "Live phishing infra": { source: "OpenCTI", how: "Phishing-typed Infrastructure entities in OpenCTI." },
  "Confirmed malicious": {
    source: "OpenCTI",
    how: "Share of fraud indicators flagged for detection (validated malicious) in OpenCTI. OpenAEV phishing simulations override this when connected.",
  },
  "Phishing prevention": {
    source: "OpenAEV",
    how: "Prevention success of OpenAEV phishing simulations (human-response readiness), when connected.",
  },
  "Takedown closure": {
    source: "OpenCTI",
    how: "Share of phishing / brand-abuse takedown cases (Case-RFT) in a resolved / closed workflow status.",
  },
  "Takedown cases": { source: "OpenCTI", how: "Case-RFT (request for takedown) entities in OpenCTI." },
  "Open takedowns": { source: "OpenCTI", how: "Case-RFT entities not yet in a closed status in OpenCTI." },
};

/** Resolve a stage's dedicated deep link into the connected product. Returns the
 *  absolute URL and a "{resource} in {ProductName}" label, or ``null`` when there
 *  is no base URL or no dedicated page for the (vertical, stage, product). */
export function ctemDeepLink(
  baseUrl: string | null | undefined,
  vertical: string,
  stage: string,
  product: string,
): { url: string; label: string } | null {
  const canonical = product === "openbas" ? "openaev" : product;
  const entry = CTEM_DEEP_LINKS[vertical]?.[stage]?.[canonical as "opencti" | "openaev"];
  if (!baseUrl || !entry) return null;
  const base = baseUrl.replace(/\/+$/, "");
  const productName = CTEM_PRODUCT_INFO[canonical]?.name ?? canonical;
  return { url: `${base}${entry.path}`, label: `${entry.resource} in ${productName}` };
}

/** Pick the BEST footer deep link for a stage: the platform that actually drives
 *  it, not always OpenCTI. ``CTEM_DEEP_LINKS`` is authored owner-first per stage
 *  (OpenAEV for Scoping / Validation, OpenCTI for Discovery / Prioritization /
 *  Remediation), so we walk those products in that order but float the ones that
 *  are actually present (returned data) to the front - then return the first with
 *  a resolvable URL. Without this every card linked to OpenCTI, because the
 *  backend always lists ``opencti`` first in each stage's ``products_present``. */
export function ctemBestDeepLink(
  products: Record<string, { url?: string | null } | undefined> | undefined,
  vertical: string,
  stage: string,
  presentProducts: string[] | undefined,
): { url: string; label: string } | null {
  const entries = CTEM_DEEP_LINKS[vertical]?.[stage];
  if (!entries) return null;
  const present = new Set(presentProducts ?? []);
  // Stable sort keeps the owner-first key order within each present/absent group,
  // so e.g. Validation -> [openaev, opencti] both present -> OpenAEV wins.
  const candidates = Object.keys(entries).sort(
    (a, b) => Number(present.has(b)) - Number(present.has(a)),
  );
  for (const product of candidates) {
    const link = ctemDeepLink(products?.[product]?.url, vertical, stage, product);
    if (link) return link;
  }
  return null;
}

/** Expectation-RESULT bucket display for OpenAEV validation donuts (the donut
 *  segments / legend). */
export const CTEM_EXPECTATION_BUCKETS: { id: string; label: string; color: string }[] = [
  { id: "SUCCESS", label: "Success", color: "var(--color-feedback-success-primary)" },
  { id: "PARTIAL", label: "Partial", color: "var(--color-feedback-alert-primary)" },
  { id: "PENDING", label: "Pending", color: "#475569" },
  { id: "FAILED", label: "Failed", color: "var(--color-feedback-error-primary)" },
];

/** OpenAEV expectation TYPES (one validation donut each). The dashboard renders
 *  whatever types OpenAEV returns, so new types (OpenAEV plans to add more)
 *  appear automatically - this list only fixes the preferred display order and
 *  nice labels for the known ones; unknown/future types are humanized from their
 *  key via {@link ctemExpectationLabel}. */
export const CTEM_EXPECTATION_TYPE_ORDER = [
  "PREVENTION",
  "DETECTION",
  "VULNERABILITY",
  "HUMAN_RESPONSE",
] as const;

const CTEM_EXPECTATION_TYPE_LABELS: Record<string, string> = {
  PREVENTION: "Prevention",
  DETECTION: "Detection",
  VULNERABILITY: "Vulnerability",
  HUMAN_RESPONSE: "Human response",
};

/** Humanize an OpenAEV expectation type key, with a fallback for future types
 *  (e.g. "SOME_NEW_TYPE" -> "Some new type"). */
export function ctemExpectationLabel(type: string): string {
  return (
    CTEM_EXPECTATION_TYPE_LABELS[type] ??
    type
      .toLowerCase()
      .replace(/_/g, " ")
      .replace(/\b\w/g, (c) => c.toUpperCase())
  );
}

/** Sort key for expectation types: known order first, unknown types last. */
export function ctemExpectationOrder(type: string): number {
  const i = (CTEM_EXPECTATION_TYPE_ORDER as readonly string[]).indexOf(type);
  return i === -1 ? CTEM_EXPECTATION_TYPE_ORDER.length : i;
}

/**
 * Sentinel value used to represent "Platform default" (a null/empty model)
 * as a filterable option in the Traces / Trace Sessions Models multi-select.
 * A real empty string can't be used: it is stripped by the CSV query-param
 * parsing (`parseCsv(...).filter(Boolean)`) used by those pages, which would
 * silently drop the selection on the next render/reload.
 */
export const PLATFORM_DEFAULT_MODEL_FILTER_VALUE = "__platform_default__";

// ---------------------------------------------------------------------------
// Runners (native, pull-based OS-control daemons)
// ---------------------------------------------------------------------------

/** Per-capability display config for the XTM One Desktop page permission editor
 *  and runner cards. Keys mirror `RUNNER_CAPABILITIES` in
 *  `backend/app/models/runner.py` — keep the two in lockstep. Icons are Lucide
 *  names resolved through `ICON_MAP`. */
export const RUNNER_CAPABILITY_CONFIG: Record<
  string,
  { label: string; description: string; icon: string }
> = {
  screenshot: {
    label: "Screenshots",
    description: "Capture genuine full-desktop screenshots (audit evidence).",
    icon: "Camera",
  },
  computer_action: {
    label: "Mouse & Keyboard",
    description: "Synthesize input: move, click, drag, scroll, type, key combos.",
    icon: "MousePointerClick",
  },
  run_shell: {
    label: "Shell Commands",
    description: "Execute shell commands (PowerShell on Windows, sh on Unix).",
    icon: "Terminal",
  },
  file_ops: {
    label: "File Operations",
    description: "Read, write, and list files and directories.",
    icon: "FolderOpen",
  },
  browser_control: {
    label: "Browser Control",
    description: "Drive a real, already-authenticated local browser via CDP.",
    icon: "Globe",
  },
};

/** Ordered capability ids for rendering the permission editor consistently. */
export const RUNNER_CAPABILITY_ORDER = [
  "screenshot",
  "computer_action",
  "run_shell",
  "file_ops",
  "browser_control",
] as const;

/** Display config for the two permission modes. */
export const RUNNER_PERMISSION_MODE_CONFIG: Record<
  string,
  { label: string; description: string; color: string }
> = {
  restricted: {
    label: "Restricted",
    description:
      "Least privilege: only the capabilities you enable below. Shell commands can be allow/block-listed and gated by your approval.",
    color: "text-feedback-success-primary",
  },
  full_control: {
    label: "Full control",
    description:
      "Every capability, every command, no per-operation approval. Deliberate opt-in — still fully audited.",
    color: "text-feedback-warning-primary",
  },
};

/**
 * Display config per runner platform. The logos are the Filigran-family OS
 * brand marks (the exact dark-theme assets OpenAEV's PlatformIcon ships:
 * white Windows flag / Apple mark / Tux on transparent), rendered via the
 * shared ProviderLogo component — never a generic monitor glyph.
 */
export const RUNNER_PLATFORM_CONFIG: Record<
  string,
  { label: string; logo: string }
> = {
  windows: { label: "Windows", logo: withBasePath("/logos/windows.png") },
  macos: { label: "macOS", logo: withBasePath("/logos/macos.png") },
  linux: { label: "Linux", logo: withBasePath("/logos/linux.png") },
};

/** Human labels for runner command kinds in the audit trail. */
export const RUNNER_COMMAND_KIND_LABELS: Record<string, string> = {
  host_facts: "Host facts",
  screenshot: "Screenshot",
  computer_action: "Computer action",
  run_shell: "Shell command",
  // A read, tail or stop of the background jobs a run_shell started.
  shell_job: "Background job",
  read_file: "Read file",
  write_file: "Write file",
  list_dir: "List directory",
  browser_control: "Browser control",
};

// --- Evaluation output families ---
// Not mirrored here: a frontend copy of the families drifts from the backend
// unseen by its drift guards, and once showed stale state to users.
// `GET /evaluations/families` serves them; use `useEvaluationFamilies()`.
