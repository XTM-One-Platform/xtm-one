// SPDX-License-Identifier: Apache-2.0

/**
 * Security audit report logic (Settings > Access and security): how the findings
 * and the security controls of `GET /security-audit` are counted,
 * ranked, filtered and grouped.
 *
 * The audit gains checks over time, and a new check may bring a category or a
 * severity this module has never seen. Everything here takes any string: an
 * unknown severity is kept, counted and ranked right after `warning` (shown
 * among what needs a look, never buried under the passes), and an unknown
 * category is named from its key. Nothing is dropped. A control state this
 * module does not know is kept the same way, shown neutral.
 */
import type { SecurityAuditFinding, SecurityControlStatus } from "@/types";

export const KNOWN_SEVERITIES = ["critical", "warning", "info", "pass"] as const;
export type KnownSeverity = (typeof KNOWN_SEVERITIES)[number];

/** Severity names. @i18n-keys */
export const SEVERITY_LABELS = {
  critical: "Critical",
  warning: "Warning",
  info: "Info",
  pass: "Passed",
} as const satisfies Record<KnownSeverity, string>;

/** What each severity asks of the administrator, `other` for a severity this screen does not know. @i18n-keys */
export const SEVERITY_HINTS = {
  critical: "Needs immediate action",
  warning: "Should be reviewed",
  info: "For your information",
  pass: "Checks that passed",
  other: "Reported by the audit",
} as const satisfies Record<KnownSeverity | "other", string>;

/** @i18n-keys */
const SCORE_LABELS = {
  healthy: "Healthy",
  needs_attention: "Needs attention",
  at_risk: "At risk",
} as const;

/**
 * The categories of every finding and every control, in the order of the
 * backend's `SECURITY_CATEGORIES` (`app/core/security_controls.py`), which a
 * backend test holds this table to. @i18n-keys
 */
export const CATEGORY_LABELS = {
  authentication: "Authentication",
  access: "Sharing and access",
  encryption: "Encryption",
  network: "Network",
  web: "Browser security",
  integrations: "Integrations",
  agents: "Agents",
  assignments: "Assignments",
  flows: "Flows",
  channels: "Channels",
  tools: "Tools",
  mcp: "MCP and A2A",
  llm: "LLM",
  infrastructure: "Infrastructure",
} as const;

export const KNOWN_CONTROL_STATES = ["enforced", "monitoring", "off"] as const;
export type KnownControlState = (typeof KNOWN_CONTROL_STATES)[number];

/** @i18n-keys */
export const CONTROL_STATE_LABELS = {
  enforced: "Enforced",
  monitoring: "Monitoring",
  off: "Off",
} as const satisfies Record<KnownControlState, string>;

export type Tone = "danger" | "warning" | "info" | "success" | "neutral";

const SEVERITY_RANK: Record<KnownSeverity, number> = { critical: 0, warning: 1, info: 3, pass: 4 };
const UNKNOWN_SEVERITY_RANK = 2;

const SEVERITY_TONES: Record<KnownSeverity, Tone> = {
  critical: "danger",
  warning: "warning",
  info: "info",
  pass: "success",
};

const SCORE_TONES: Record<keyof typeof SCORE_LABELS, Tone> = {
  at_risk: "danger",
  needs_attention: "warning",
  healthy: "success",
};

function own<T extends object>(table: T, key: string): key is Extract<keyof T, string> {
  return Object.prototype.hasOwnProperty.call(table, key);
}

function isKnownSeverity(severity: string): severity is KnownSeverity {
  return (KNOWN_SEVERITIES as readonly string[]).includes(severity);
}

/** Sort rank, most urgent first. */
export function severityRank(severity: string): number {
  return isKnownSeverity(severity) ? SEVERITY_RANK[severity] : UNKNOWN_SEVERITY_RANK;
}

export function severityTone(severity: string): Tone {
  return isKnownSeverity(severity) ? SEVERITY_TONES[severity] : "neutral";
}

export function scoreTone(score: string): Tone {
  return own(SCORE_TONES, score) ? SCORE_TONES[score] : "neutral";
}

/**
 * A readable name for a key no table knows: `needs_attention` -> "Needs
 * attention", and a key of three characters or fewer is taken for an acronym
 * (`mcp` -> "MCP").
 */
export function humanizeKey(key: string): string {
  const words = key.replace(/[_.-]+/g, " ").trim();
  if (!words) return key;
  if (/^[a-z0-9]{1,3}$/i.test(words)) return words.toUpperCase();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** The label of a severity: a catalog key when known, its humanized name otherwise. */
export function severityLabel(severity: string): string {
  return isKnownSeverity(severity) ? SEVERITY_LABELS[severity] : humanizeKey(severity);
}

export function severityHint(severity: string): string {
  return isKnownSeverity(severity) ? SEVERITY_HINTS[severity] : SEVERITY_HINTS.other;
}

export function categoryLabel(category: string): string {
  return own(CATEGORY_LABELS, category) ? CATEGORY_LABELS[category] : humanizeKey(category);
}

export function scoreLabel(score: string): string {
  return own(SCORE_LABELS, score) ? SCORE_LABELS[score] : humanizeKey(score);
}

// ---------------------------------------------------------------------------
// Counting
// ---------------------------------------------------------------------------

interface SeverityCount {
  severity: string;
  count: number;
}

export interface Tally {
  /** The four known severities (zero included) and every other one found, most urgent first. */
  severities: SeverityCount[];
  total: number;
  passed: number;
  /** Findings that passed or did not: the informational ones are neither. */
  checked: number;
}

function bySeverityRank<T>(items: readonly T[], severityOf: (item: T) => string): T[] {
  return items
    .map((item, order) => ({ item, order }))
    .sort((a, b) => severityRank(severityOf(a.item)) - severityRank(severityOf(b.item)) || a.order - b.order)
    .map(({ item }) => item);
}

export function tally(findings: readonly SecurityAuditFinding[]): Tally {
  const counts = new Map<string, number>(KNOWN_SEVERITIES.map((severity) => [severity, 0]));
  for (const finding of findings) counts.set(finding.severity, (counts.get(finding.severity) ?? 0) + 1);
  const severities = bySeverityRank(
    [...counts].map(([severity, count]) => ({ severity, count })),
    (entry) => entry.severity,
  );
  const passed = counts.get("pass") ?? 0;
  return { severities, total: findings.length, passed, checked: findings.length - (counts.get("info") ?? 0) };
}

export function countOf(t: Tally, severity: string): number {
  return t.severities.find((entry) => entry.severity === severity)?.count ?? 0;
}

/** Share of the checked findings that passed, `null` when none was checked. */
export function passRate(t: Tally): number | null {
  return t.checked > 0 ? t.passed / t.checked : null;
}

/**
 * A pass rate as a whole percentage, never rounded to 100 while a check still
 * fails, nor to 0 while one passed.
 */
export function percent(rate: number): number {
  const rounded = Math.round(rate * 100);
  if (rounded === 100 && rate < 1) return 99;
  if (rounded === 0 && rate > 0) return 1;
  return rounded;
}

type Headline =
  | { kind: "critical"; count: number }
  | { kind: "warning"; count: number }
  | { kind: "review"; count: number }
  | { kind: "clear" };

/** What the report says first: the most urgent severity it carries. */
export function headline(t: Tally): Headline {
  const critical = countOf(t, "critical");
  if (critical > 0) return { kind: "critical", count: critical };
  const warning = countOf(t, "warning");
  if (warning > 0) return { kind: "warning", count: warning };
  const review = t.severities
    .filter((entry) => !isKnownSeverity(entry.severity))
    .reduce((sum, entry) => sum + entry.count, 0);
  if (review > 0) return { kind: "review", count: review };
  return { kind: "clear" };
}

// ---------------------------------------------------------------------------
// Categories
// ---------------------------------------------------------------------------

export interface CategorySummary extends Tally {
  category: string;
  /** The severity of its most urgent finding. */
  worst: string;
}

/** One summary per category, the one needing attention most first. */
export function summarizeCategories(findings: readonly SecurityAuditFinding[]): CategorySummary[] {
  const byCategory = new Map<string, SecurityAuditFinding[]>();
  for (const finding of findings) {
    const bucket = byCategory.get(finding.category);
    if (bucket) bucket.push(finding);
    else byCategory.set(finding.category, [finding]);
  }
  const summaries = [...byCategory].map(([category, items]) => {
    const t = tally(items);
    const worst = bySeverityRank(items, (finding) => finding.severity)[0].severity;
    return { ...t, category, worst };
  });
  return summaries.sort(
    (a, b) =>
      severityRank(a.worst) - severityRank(b.worst) ||
      countOf(b, b.worst) - countOf(a, a.worst) ||
      a.category.localeCompare(b.category),
  );
}

// ---------------------------------------------------------------------------
// Filtering and grouping
// ---------------------------------------------------------------------------

export interface FindingFilters {
  /** Empty: every severity. */
  severities: readonly string[];
  /** `null`: every category. */
  category: string | null;
  query: string;
}

export const NO_FILTERS: FindingFilters = { severities: [], category: null, query: "" };

export function hasActiveFilters(filters: FindingFilters): boolean {
  return filters.severities.length > 0 || filters.category !== null || filters.query.trim() !== "";
}

/** Case- and accent-insensitive form, so `securite` finds "Sécurité". */
export function fold(text: string): string {
  return text.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
}

/**
 * Whether every word of `query` appears in the finding: its title, text,
 * recommendation, id, category and severity as the API names them, plus
 * `extra` (the labels the screen shows for it, which are translated).
 */
function matchesQuery(finding: SecurityAuditFinding, query: string, extra = ""): boolean {
  const terms = fold(query).split(/\s+/).filter(Boolean);
  if (terms.length === 0) return true;
  const haystack = fold(
    [
      finding.title,
      finding.description,
      finding.recommendation ?? "",
      finding.id ?? "",
      finding.category,
      finding.severity,
      extra,
    ].join("\n"),
  );
  return terms.every((term) => haystack.includes(term));
}

export function filterFindings(
  findings: readonly SecurityAuditFinding[],
  filters: FindingFilters,
  labelsOf: (finding: SecurityAuditFinding) => string = () => "",
): SecurityAuditFinding[] {
  return findings.filter(
    (finding) =>
      (filters.severities.length === 0 || filters.severities.includes(finding.severity)) &&
      (filters.category === null || finding.category === filters.category) &&
      matchesQuery(finding, filters.query, labelsOf(finding)),
  );
}

export type GroupBy = "severity" | "category";

export interface FindingGroup {
  /** The severity or the category the group gathers. */
  key: string;
  findings: SecurityAuditFinding[];
}

/**
 * Groups in the order they call for attention. Inside a group, findings keep
 * the order the audit ran its checks in; a category group puts its most urgent
 * findings first.
 */
export function groupFindings(findings: readonly SecurityAuditFinding[], by: GroupBy): FindingGroup[] {
  if (by === "category") {
    return summarizeCategories(findings).map(({ category }) => ({
      key: category,
      findings: bySeverityRank(
        findings.filter((finding) => finding.category === category),
        (finding) => finding.severity,
      ),
    }));
  }
  const groups = new Map<string, SecurityAuditFinding[]>();
  for (const finding of bySeverityRank(findings, (f) => f.severity)) {
    const bucket = groups.get(finding.severity);
    if (bucket) bucket.push(finding);
    else groups.set(finding.severity, [finding]);
  }
  return [...groups].map(([key, items]) => ({ key, findings: items }));
}

/**
 * React keys for a list of findings: the finding id when it has one, what it
 * says otherwise, a repeat numbered so no two keys collide.
 */
export function findingKeys(findings: readonly SecurityAuditFinding[]): string[] {
  const seen = new Map<string, number>();
  return findings.map((finding) => {
    const base = finding.id ? `id:${finding.id}` : `finding:${finding.category}:${finding.severity}:${finding.title}`;
    const repeat = seen.get(base) ?? 0;
    seen.set(base, repeat + 1);
    return repeat === 0 ? base : `${base}#${repeat}`;
  });
}

// ---------------------------------------------------------------------------
// Dismissals
// ---------------------------------------------------------------------------

/** Mirrors the backend's `DISMISSABLE_SEVERITIES`: a passing check has nothing to accept. */
export const DISMISSABLE_SEVERITIES: readonly string[] = ["critical", "warning", "info"];
/** Mirrors `FINDING_ID_PATTERN` and `FINDING_ID_MAX_CHARS`, the ids the dismissal route accepts. */
const DISMISSABLE_ID = /^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$/;
const FINDING_ID_MAX_CHARS = 200;
/** Mirrors `DISMISSAL_REASON_MAX_CHARS`. */
export const DISMISSAL_REASON_MAX_CHARS = 1000;

export function isDismissed(finding: SecurityAuditFinding): boolean {
  return !!finding.dismissed;
}

/** What an administrator may dismiss: a stable id and a severity that is not a pass. */
export function canDismiss(finding: SecurityAuditFinding): boolean {
  const id = finding.id ?? "";
  return (
    !isDismissed(finding) &&
    id.length <= FINDING_ID_MAX_CHARS &&
    DISMISSABLE_ID.test(id) &&
    DISMISSABLE_SEVERITIES.includes(finding.severity)
  );
}

/**
 * The report cut in two: what counts (every tally, the score, the filters)
 * and what an administrator dismissed, most urgent first.
 */
export function splitDismissed(findings: readonly SecurityAuditFinding[]): {
  active: SecurityAuditFinding[];
  dismissed: SecurityAuditFinding[];
} {
  const active: SecurityAuditFinding[] = [];
  const dismissed: SecurityAuditFinding[] = [];
  for (const finding of findings) (isDismissed(finding) ? dismissed : active).push(finding);
  return { active, dismissed: bySeverityRank(dismissed, (finding) => finding.severity) };
}

/**
 * The ids among *requested* (a Security options link, `?finding=`) that this
 * report actually carries: only those are highlighted.
 */
export function focusedFindings(findings: readonly SecurityAuditFinding[], requested: readonly string[]): Set<string> {
  const wanted = new Set(requested);
  return new Set(findings.flatMap((finding) => (finding.id && wanted.has(finding.id) ? [finding.id] : [])));
}

// ---------------------------------------------------------------------------
// Controls
// ---------------------------------------------------------------------------

const CONTROL_STATE_TONES: Record<KnownControlState, Tone> = {
  enforced: "success",
  monitoring: "info",
  off: "warning",
};

// Off first (what an administrator may enforce), an unknown state right after.
const CONTROL_STATE_RANK: Record<KnownControlState, number> = { off: 1, monitoring: 3, enforced: 4 };
const UNKNOWN_CONTROL_STATE_RANK = 2;
const ALARMING_OFF_RANK = 0;

const CATEGORY_ORDER: readonly string[] = Object.keys(CATEGORY_LABELS);

function isKnownControlState(state: string): state is KnownControlState {
  return (KNOWN_CONTROL_STATES as readonly string[]).includes(state);
}

export function controlStateLabel(state: string): string {
  return isKnownControlState(state) ? CONTROL_STATE_LABELS[state] : humanizeKey(state);
}

export function controlStateTone(state: string): Tone {
  return isKnownControlState(state) ? CONTROL_STATE_TONES[state] : "neutral";
}

/** Where a category sorts: the taxonomy's order, a category it does not name after all of them. */
export function categoryRank(category: string): number {
  const index = CATEGORY_ORDER.indexOf(category);
  return index === -1 ? CATEGORY_ORDER.length : index;
}

interface StateCount {
  state: string;
  count: number;
}

export interface ControlTally {
  /** The three known states (zero included) and every other one found, off first. */
  states: StateCount[];
  total: number;
  enforced: number;
  monitoring: number;
}

export function tallyControls(controls: readonly SecurityControlStatus[]): ControlTally {
  const counts = new Map<string, number>(KNOWN_CONTROL_STATES.map((state) => [state, 0]));
  for (const control of controls) counts.set(control.state, (counts.get(control.state) ?? 0) + 1);
  const rank = (state: string) => (isKnownControlState(state) ? CONTROL_STATE_RANK[state] : UNKNOWN_CONTROL_STATE_RANK);
  const states = [...counts]
    .map(([state, count]) => ({ state, count }))
    .sort((a, b) => rank(a.state) - rank(b.state));
  return {
    states,
    total: controls.length,
    enforced: counts.get("enforced") ?? 0,
    monitoring: counts.get("monitoring") ?? 0,
  };
}

/** The findings that report on each control (`finding.control`), most urgent first. */
export function findingsByControl(findings: readonly SecurityAuditFinding[]): Map<string, SecurityAuditFinding[]> {
  const index = new Map<string, SecurityAuditFinding[]>();
  for (const finding of bySeverityRank(findings, (f) => f.severity)) {
    if (!finding.control) continue;
    const bucket = index.get(finding.control);
    if (bucket) bucket.push(finding);
    else index.set(finding.control, [finding]);
  }
  return index;
}

/** A critical or warning finding an administrator has not dismissed. */
function isAlarming(finding: SecurityAuditFinding): boolean {
  return !isDismissed(finding) && (finding.severity === "critical" || finding.severity === "warning");
}

/** The most urgent finding about a control that still counts, `null` when every one was dismissed. */
export function worstLinkedFinding(linked: readonly SecurityAuditFinding[]): SecurityAuditFinding | null {
  return linked.find((finding) => !isDismissed(finding)) ?? null;
}

/**
 * Sort rank: an off control a warning or critical finding is about first, then
 * off, an unknown state, monitoring, enforced. A dismissed finding raises nothing.
 */
export function controlRank(control: SecurityControlStatus, linked: readonly SecurityAuditFinding[]): number {
  if (control.state === "off" && linked.some(isAlarming)) return ALARMING_OFF_RANK;
  return isKnownControlState(control.state) ? CONTROL_STATE_RANK[control.state] : UNKNOWN_CONTROL_STATE_RANK;
}

/** The order the Controls view lists controls in: by rank, then category (the taxonomy's order), then title. */
export function sortControls(
  controls: readonly SecurityControlStatus[],
  linked: ReadonlyMap<string, readonly SecurityAuditFinding[]>,
): SecurityControlStatus[] {
  return controls
    .map((control, order) => ({ control, order, rank: controlRank(control, linked.get(control.id) ?? []) }))
    .sort(
      (a, b) =>
        a.rank - b.rank ||
        categoryRank(a.control.category) - categoryRank(b.control.category) ||
        a.control.category.localeCompare(b.control.category) ||
        a.control.title.localeCompare(b.control.title) ||
        a.order - b.order,
    )
    .map(({ control }) => control);
}

/** The categories controls fall in, in the taxonomy's order. */
export function controlCategories(controls: readonly SecurityControlStatus[]): string[] {
  return [...new Set(controls.map((control) => control.category))].sort(
    (a, b) => categoryRank(a) - categoryRank(b) || a.localeCompare(b),
  );
}

export interface ControlFilters {
  /** Empty: every state. */
  states: readonly string[];
  /** `null`: every category. */
  category: string | null;
  query: string;
}

export const NO_CONTROL_FILTERS: ControlFilters = { states: [], category: null, query: "" };

export function hasActiveControlFilters(filters: ControlFilters): boolean {
  return filters.states.length > 0 || filters.category !== null || filters.query.trim() !== "";
}

/**
 * Whether every word of `query` appears in the control: its title, setting,
 * id, impact, where it is changed, and its state and category as the API names
 * them, plus `extra` (the translated labels the screen shows for it).
 */
function matchesControlQuery(control: SecurityControlStatus, query: string, extra = ""): boolean {
  const terms = fold(query).split(/\s+/).filter(Boolean);
  if (terms.length === 0) return true;
  const haystack = fold(
    [
      control.title,
      control.setting,
      control.id,
      control.impact,
      control.where,
      control.state,
      control.category,
      extra,
    ].join("\n"),
  );
  return terms.every((term) => haystack.includes(term));
}

export function filterControls(
  controls: readonly SecurityControlStatus[],
  filters: ControlFilters,
  labelsOf: (control: SecurityControlStatus) => string = () => "",
): SecurityControlStatus[] {
  return controls.filter(
    (control) =>
      (filters.states.length === 0 || filters.states.includes(control.state)) &&
      (filters.category === null || control.category === filters.category) &&
      matchesControlQuery(control, filters.query, labelsOf(control)),
  );
}

/**
 * The value that enforces a control, read for a sentence: `>= 12` is "12 or
 * more", `empty` is "unset or empty", anything else is the value itself.
 */
type HardenedPhrase =
  | { kind: "empty" }
  | { kind: "atLeast" | "atMost" | "above" | "below" | "value"; value: string };

const COMPARISONS = { ">=": "atLeast", "<=": "atMost", ">": "above", "<": "below" } as const;

export function hardenedPhrase(hardened: string): HardenedPhrase {
  const text = hardened.trim();
  if (text === "empty") return { kind: "empty" };
  const match = /^(>=|<=|>|<)\s*(\S+)$/.exec(text);
  if (match) return { kind: COMPARISONS[match[1] as keyof typeof COMPARISONS], value: match[2] };
  return { kind: "value", value: text };
}

// ---------------------------------------------------------------------------
// Text
// ---------------------------------------------------------------------------

interface TextSegment {
  text: string;
  /** A configuration name the admin types somewhere (`DEBUG=false`, `REDIS_URL`). */
  code: boolean;
}

/**
 * An UPPER_SNAKE name, with its `=value` when the text gives one, or a single
 * upper-case word followed by `=value`. A value does not take the sentence's
 * closing punctuation.
 */
const CONFIG_TOKEN =
  /\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+(?![A-Za-z0-9_])(?:=[^\s,;()]*[^\s,;().])?|\b[A-Z][A-Z0-9]+=[^\s,;()]*[^\s,;().]/g;

/** `text` cut into prose and the configuration names it spells out. */
export function splitConfigTokens(text: string): TextSegment[] {
  const segments: TextSegment[] = [];
  let last = 0;
  for (const match of text.matchAll(CONFIG_TOKEN)) {
    const start = match.index ?? 0;
    if (start > last) segments.push({ text: text.slice(last, start), code: false });
    segments.push({ text: match[0], code: true });
    last = start + match[0].length;
  }
  if (last < text.length) segments.push({ text: text.slice(last), code: false });
  return segments;
}

// ---------------------------------------------------------------------------
// Time
// ---------------------------------------------------------------------------

// The localized counterpart of `timeAgo()` now lives in `lib/relative-time.ts`
// (shared with the Work items list). A relative path with its extension:
// Node's test runner loads this file and does not know the "@/" alias.
export { relativeTimeParts } from "../../lib/relative-time.ts";
