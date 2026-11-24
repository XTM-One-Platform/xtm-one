// SPDX-License-Identifier: Apache-2.0

import { type ClassValue, clsx } from "clsx";
import { extendTailwindMerge } from "tailwind-merge";
import { CHAT_GENERAL_AGENT_PARAM, PROVIDER_DISPLAY_NAMES } from "@/lib/constants";
import { isPerIntegrationApiProvider } from "@/lib/tool-scope";
import { generalAssistantStandIn } from "@/lib/general-assistant-stand-in";
import { safeHttpUrl } from "@/lib/safe-url";
import { exactCount, exactCurrency, formatCount, formatCurrency, isCompacted } from "@/lib/number-format";
import type { Agent, Integration, LLMProvider, User } from "@/types";
import { MODEL_INDEPENDENT } from "@/lib/constants";

/**
 * The design system's composite type utilities (`content-*`, `title-*`, see the
 * `@source inline` safelist in index.css). Each one sets the whole type style
 * (size, line height, weight), so two of them on one element conflict.
 * tailwind-merge does not know them, kept both, and the one declared last in
 * the stylesheet won whatever the caller passed: `<Badge className="content-caption">`
 * rendered at the Badge base `content-compact` (12px) instead of 10px.
 * Declared as one class group each, the class passed last wins, as for any
 * other Tailwind utility. Named one by one so `content-center` and the other
 * `align-content` utilities keep their own group.
 */
const FDS_CONTENT_STYLES = [
  "base", "base-bold", "base-link", "base-medium", "button", "caption", "code",
  "compact", "compact-bold", "compact-link", "compact-medium", "highlight",
] as const;
const FDS_TITLE_STYLES = ["2xl", "jumbo", "lg", "md", "sm", "xl", "xs"] as const;

const twMerge = extendTailwindMerge<"fds-content" | "fds-title">({
  extend: {
    classGroups: {
      "fds-content": [{ content: [...FDS_CONTENT_STYLES] }],
      "fds-title": [{ title: [...FDS_TITLE_STYLES] }],
    },
  },
});

/** Merge Tailwind classes with clsx. */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/**
 * Escape a string for safe interpolation into a `RegExp`. Without this, a value
 * derived from user-controlled input (e.g. a URL path segment) that contains
 * regex metacharacters like `(` or `[` would throw a `SyntaxError` when passed
 * to `new RegExp(...)` and crash the render.
 *
 * This is the single source of truth — do NOT inline the metacharacter regex.
 */
export function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Fallback minimum password length used only when the live server value
 *  from ``/auth/config`` hasn't loaded yet. The server is the source of
 *  truth; callers should pass the value from ``useAuthConfig()`` so a
 *  customized backend policy never silently drifts from the UX. */
export const MIN_PASSWORD_LENGTH = 8;

/**
 * Validate a password against the platform policy (length + at least one
 * letter and one digit). Returns an error message, or null if valid.
 * Mirrors `check_password_policy` on the backend.
 *
 * Pass ``minLength`` (e.g. from ``useAuthConfig().data?.min_password_length``)
 * so the floor matches the server-side ``MIN_PASSWORD_LENGTH`` env var; the
 * fallback ``MIN_PASSWORD_LENGTH`` constant is only used while the auth-config
 * query is still in flight on first paint.
 *
 * The letter / digit regexes use Unicode property escapes (`\p{L}` / `\p{N}`
 * with the `u` flag) so the client matches Python's `str.isalpha()` /
 * `str.isdigit()` semantics — passwords using non-ASCII letters or digits
 * (e.g. `Pâté2024`, `كلمة1234`) are treated the same on both sides. The
 * previous `/[a-zA-Z]/` / `/[0-9]/` form would have rejected those
 * client-side even though the server happily accepts them.
 */
export function validatePassword(
  password: string,
  minLength: number = MIN_PASSWORD_LENGTH,
): string | null {
  // Leading/trailing whitespace is trimmed (the backend stores the trimmed
  // value), so validate against that to keep client and server in sync.
  const pw = password.trim();
  if (pw.length < minLength) {
    return `Password must be at least ${minLength} characters`;
  }
  if (!/\p{L}/u.test(pw)) return "Password must contain at least one letter";
  if (!/\p{N}/u.test(pw)) return "Password must contain at least one number";
  return null;
}

/**
 * Single source of truth for known frontend-gated flag ids. Add ids here
 * (and to the backend's ``KNOWN_FEATURE_FLAGS`` in
 * ``app/core/feature_flags.py``) only when a feature actually needs to be
 * hidden/shown in the UI; there's no shared codegen between the two, so
 * keep both lists in sync by hand.
 * @public
 */
export const KNOWN_FEATURE_FLAGS = [
  "AGENT_WIZARD_ASSIST",
  "DASHBOARD_STUDIO",
  "DEEP_INVESTIGATION",
  "EVALUATION",
  "FLOW_BUILDER",
  "MOBILE_COMPANION",
  "VIDEO_STUDIO",
] as const;

/** A valid frontend feature-flag id. Widen to a `Literal`-style union
 *  automatically as soon as `KNOWN_FEATURE_FLAGS` gains entries. */
export type FeatureFlag = (typeof KNOWN_FEATURE_FLAGS)[number];

/**
 * Check whether `flag` is enabled for this platform instance.
 *
 * Pass `enabledFeatures` from `useAuthConfig().data?.enabled_features`
 * (already resolved/expanded by the backend), or use the `useFeatureFlag` hook,
 * which reads that query for you.
 *
 * Presentation, never enforcement. How much the server backs a given flag up
 * varies per flag — `A2A_SERVER` does not even mount its routers when off,
 * while `FLOW_BUILDER` is deliberately UI-only and its routes keep answering —
 * so hiding a control here is not an authorization decision. See
 * `useFeatureFlag` for the details.
 */
export function isFeatureEnabled(
  enabledFeatures: string[] | undefined,
  flag: FeatureFlag,
): boolean {
  return !!enabledFeatures?.includes(flag);
}

/**
 * Relative time string for an ISO date (e.g. "3m ago", "2h ago", "5d ago").
 * This is the single source of truth — do NOT redefine in page files.
 *
 * Pass the viewer's `locale` (`useFormatter().locale`, e.g. `fr-fr`) to read it
 * in their language: "il y a 9 min", "hace 9 min", "9 分前", through
 * `Intl.RelativeTimeFormat`. English, or no locale, keeps the compact form.
 * Past the first minute the count is always a number (`numeric: "always"`), so
 * two days ago reads "il y a 2 j" / "hace 2 d" / "2 日前" like "2d ago", never
 * "avant-hier" / "anteayer" / "一昨日"; under a minute stays the
 * word for now ("maintenant"), never "il y a 0 s".
 */
export function timeAgo(iso: string, locale?: string, nowMs: number = Date.now()): string {
  const diff = nowMs - new Date(iso).getTime();
  const mins = Math.floor(diff / 60000);
  const hrs = Math.floor(mins / 60);
  const lang = (locale ?? "en").split("-")[0].toLowerCase();
  if (lang !== "en") {
    if (mins < 1) return new Intl.RelativeTimeFormat(locale, { numeric: "auto", style: "short" }).format(0, "second");
    const rtf = new Intl.RelativeTimeFormat(locale, { numeric: "always", style: "short" });
    if (mins < 60) return rtf.format(-mins, "minute");
    if (hrs < 24) return rtf.format(-hrs, "hour");
    return rtf.format(-Math.floor(hrs / 24), "day");
  }
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

/**
 * Future-facing counterpart of timeAgo() (e.g. "in 12m", "in 2h").
 * Returns "shortly" for timestamps that are now or already past. Minutes
 * round up (45 s -> "in 1m"); hour/day buckets truncate like timeAgo()
 * (61 min -> "in 1h", not "in 2h").
 *
 * Pass the viewer's `locale` (`useFormatter().locale`) to read it in their
 * language, like timeAgo(): "dans 42 min", "dentro de 42 min",
 * "42 分後", through `Intl.RelativeTimeFormat` (short style, always a number).
 * A timestamp now or past reads the word for now ("maintenant"), never
 * "dans 0 min". English, or no locale, keeps the compact form.
 */
export function timeUntil(iso: string, locale?: string, nowMs: number = Date.now()): string {
  const diff = new Date(iso).getTime() - nowMs;
  const mins = Math.ceil(diff / 60000);
  const lang = (locale ?? "en").split("-")[0].toLowerCase();
  if (lang !== "en") {
    if (mins < 1) return new Intl.RelativeTimeFormat(locale, { numeric: "auto", style: "short" }).format(0, "second");
    const rtf = new Intl.RelativeTimeFormat(locale, { numeric: "always", style: "short" });
    if (mins < 60) return rtf.format(mins, "minute");
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return rtf.format(hrs, "hour");
    return rtf.format(Math.floor(hrs / 24), "day");
  }
  if (mins < 1) return "shortly";
  if (mins < 60) return `in ${mins}m`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `in ${hrs}h`;
  return `in ${Math.floor(hrs / 24)}d`;
}

/**
 * Format a date to a short human-readable string (e.g. "Jan 5, 2025").
 * Unlike timeAgo(), this returns an absolute date string for older dates.
 */
export function formatDate(dateStr: string): string {
  return new Date(dateStr).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

/**
 * Format a date+time to a human-readable string (e.g. "Jan 5, 2025, 2:30 PM").
 * Use this instead of inline `new Date(...).toLocaleString()` calls.
 */
export function formatDateTime(dateStr: string): string {
  return new Date(dateStr).toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

/** Normalize a date value to a Date object (accepts ISO strings or Date instances). */
function toDate(value: string | Date): Date {
  return typeof value === "string" ? new Date(value) : value;
}

/**
 * Format a date to a short string without the year (e.g. "Jan 5").
 * Accepts both ISO date strings and Date objects.
 * Use for "last activity" displays in StatCards and OverviewTabs.
 * This is the single source of truth — do NOT inline toLocaleDateString calls.
 */
export function formatShortDate(date: string | Date): string {
  return toDate(date).toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

/**
 * Format a date to a time-only string (e.g. "2:30 PM").
 * Accepts both ISO date strings and Date objects.
 * Use for "last activity" subtitles in StatCards and OverviewTabs.
 * This is the single source of truth — do NOT inline toLocaleTimeString calls.
 */
export function formatTime(date: string | Date): string {
  return toDate(date).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" });
}

/**
 * Format a date to a compact date+time string without the year (e.g. "Jan 5, 2:30 PM").
 * Accepts both ISO date strings and Date objects.
 * Use for inline timestamps in task lists and activity entries.
 * This is the single source of truth — do NOT inline toLocaleDateString calls with time options.
 */
export function formatShortDateTime(date: string | Date): string {
  return toDate(date).toLocaleDateString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

/**
 * Toggle an item in an array — adds it if missing, removes it if present.
 * Returns a new array (immutable). Use with React state setters:
 *
 *     setIds(prev => toggleArrayItem(prev, id))
 */
export function toggleArrayItem<T>(arr: T[], item: T): T[] {
  return arr.includes(item) ? arr.filter((x) => x !== item) : [...arr, item];
}

/**
 * The language the UI is drawn in, as `AppIntlProvider` writes it on
 * `<html lang>`; "en" outside a browser or before the provider mounts.
 */
function uiLocale(): string {
  if (typeof document === "undefined") return "en";
  return document.documentElement.lang || "en";
}

/**
 * A compact count in the UI's language, for KPIs and metrics (#10476): the
 * rules of `lib/number-format` (`formatCount`), so every surface writes one
 * scale: "492", "552.1", "1.2K", "25.9K", "1M" (never "1000K"), "36.5B";
 * fr "25,9 k". A missing figure (NaN, Infinity) reads "-".
 *
 * For a component with the exact value on hover, prefer `useNumberFormat`
 * with `CompactFigure`. This is the single source of truth outside React —
 * do NOT redefine in page files.
 */
export function formatCompactNumber(n: number): string {
  return formatCount(uiLocale(), n);
}

/**
 * A US dollar amount on the scale of `formatCompactNumber`, in the UI's
 * language (`lib/number-format` `formatCurrency`): four decimals under $0.01
 * ("$0.0040"), cents under $100 ("$12.50"), whole dollars to $999 ("$752"),
 * then compact ("$61.2K", "-$1.5K", "$3.4M"); "$999.99" reads "$1K". A
 * missing amount (NaN, Infinity) reads "-".
 */
export function formatCompactCost(value: number): string {
  return formatCurrency(uiLocale(), "USD", value);
}

/**
 * The exact US dollar amount of a `formatCompactCost` figure, for its `title`:
 * "$1,234.50" behind "$1.2K". `undefined` when the drawn text is already
 * exact ("$6.19", "$921"), so a figure under $1,000 carries no tooltip.
 */
export function exactCompactCost(value: number | null | undefined): string | undefined {
  if (value === null || value === undefined || !Number.isFinite(value)) return undefined;
  // The rule of `formatCurrency`: from $100 an amount is whole units, so $999.60 already reads $1K.
  const shown = Math.abs(value) >= 100 ? Math.round(Math.abs(value)) : Math.abs(value);
  return isCompacted(shown) ? exactCurrency(uiLocale(), "USD", value) : undefined;
}

/**
 * Format a CTEM / KPI fact value for human-readable display.
 *
 * The CTEM collector computes percentages and ratios as raw floats with full
 * precision (e.g. `5.751765893037336`), and counts are conceptually whole
 * numbers. This helper guarantees every fact value rendered in the dashboard is
 * human-readable regardless of what the API returns:
 *   - numbers (and numeric strings) are rounded to a whole number and drawn on
 *     the compact scale of `formatCompactNumber` (`5.751765893037336` -> "6",
 *     `620965` -> "621K", #10476) so they match the integer score, donut and
 *     meter readouts; `exactMetricValue` gives the exact figure for the hover;
 *   - booleans render as "Yes" / "No";
 *   - non-numeric strings pass through unchanged;
 *   - null / undefined render as `placeholder` (default "--").
 *
 * This is the single source of truth — do NOT inline number rounding/formatting
 * at CTEM fact render sites (CtemStageCard, CtemStageDetail, ...).
 */
export function formatMetricValue(
  value: number | string | boolean | null | undefined,
  placeholder = "--",
): string {
  if (value === null || value === undefined) return placeholder;
  if (typeof value === "boolean") return value ? "Yes" : "No";
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n)) return String(value);
  return formatCount(uiLocale(), Math.round(n));
}

/**
 * The exact form of a fact value `formatMetricValue` drew compact ("620,965"),
 * for its `title`; undefined when the drawn text is already exact (under
 * 1,000, a boolean, a non-numeric string or a missing value).
 */
export function exactMetricValue(value: number | string | boolean | null | undefined): string | undefined {
  if (value === null || value === undefined || typeof value === "boolean") return undefined;
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n) || !isCompacted(Math.round(n))) return undefined;
  return exactCount(uiLocale(), Math.round(n));
}

/**
 * Format a 0-100 percentage-scaled metric figure for display, e.g. `83.3%`.
 *
 * Distinct from `formatMetricValue`, which renders counts: a PRF breakdown shows
 * both side by side, and an unsuffixed `83.33` next to `tp 25` reads as a count.
 * Never inline percentage formatting at render sites.
 */
export function formatPercentValue(value: number): string {
  return `${value.toFixed(1)}%`;
}

/**
 * Format a knowledge-base's document + chunk counts for inline display.
 * Returns e.g. "12 docs · 348 chunks".
 *
 * This is the single source of truth — do NOT redefine in page files.
 */
export function formatKBStats(documentCount: number, chunkCount: number): string {
  return `${formatCompactNumber(documentCount)} docs · ${formatCompactNumber(chunkCount)} chunks`;
}

/**
 * Label used wherever a numeric quota or its "unlimited" placeholder would
 * otherwise render, when the deployment is on the CE-only tier (no XTM
 * license, no EE platform). In that tier the agentic engine cannot run at
 * all, so any quota number — current, limit, or fallback — is irrelevant
 * and the UI should read "Not Applicable" instead of "0", "Unlimited",
 * or "No platform limit". Centralised here so the four quota call sites
 * (Settings → Usage and quotas → Quotas gauges + editor placeholders, UserDetail → Quotas,
 * Profile → Quotas) can't drift.
 */
export const QUOTA_NOT_APPLICABLE_LABEL = "Not Applicable";

/**
 * Display string for a quota row whose numeric value is either an actual
 * limit (formatted as ``X limit period``) or a "no cap" placeholder
 * (``Unlimited period``).
 *
 * When ``options.ceOnly`` is ``true`` the function always returns
 * :data:`QUOTA_NOT_APPLICABLE_LABEL` instead — this helper is pure and
 * has no knowledge of the deployment edition on its own, so callers must
 * resolve it via :func:`useIsCommunityEdition` (or another upstream signal)
 * and pass it in explicitly.
 */
export function formatQuotaLimit(
  limit: number | null | undefined,
  period: string,
  options: { ceOnly?: boolean } = {},
): string {
  if (options.ceOnly) return QUOTA_NOT_APPLICABLE_LABEL;
  // Any non-null limit counts — including an explicit 0, which the quota
  // editor allows (min=0) and the backend enforces as a hard stop
  // (``current >= default_limit``). "Unlimited" is reserved for the
  // truly-unconfigured null/undefined case.
  if (limit != null) return `${formatCompactNumber(limit)} limit ${period}`;
  return `Unlimited ${period}`;
}

export { mergeSandboxIntoAgenticCount } from "@/lib/agentic-quota";

/**
 * Simple English pluralization helper.
 * Returns ``singular`` when count is 1, otherwise ``plural`` (or ``singular + "s"``).
 *
 *     pluralize(1, "agent")       // "agent"
 *     pluralize(3, "agent")       // "agents"
 *     pluralize(2, "child", "children") // "children"
 *
 * This is the single source of truth — do NOT inline ternaries like
 * `count !== 1 ? "s" : ""` in page files.
 */
export function pluralize(count: number, singular: string, plural?: string): string {
  return count === 1 ? singular : (plural ?? `${singular}s`);
}

/**
 * Format a duration in seconds compactly: "45s", "5m", "1m 30s", "2h 5m".
 *
 * Mirrors the backend `format_wait_duration` (app/tools/wait_policy.py) so a
 * wait budget reads the same in Settings, in the chat countdown, and in the
 * tool result the agent sees. Single source of truth — do not inline
 * `Math.floor(s / 60)` duration maths in page files.
 */
export function formatDurationShort(totalSeconds: number): string {
  const total = Math.round(Math.max(0, totalSeconds));
  if (total < 60) return `${total}s`;
  if (total < 3600) {
    const minutes = Math.floor(total / 60);
    const seconds = total % 60;
    return seconds ? `${minutes}m ${seconds}s` : `${minutes}m`;
  }
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  return minutes ? `${hours}h ${minutes}m` : `${hours}h`;
}

/**
 * Format a live countdown as `m:ss` (or `h:mm:ss` past an hour) — the ticking
 * form used while an agent is paused on a `wait` call, where a stable digit
 * width matters more than the compact unit form above.
 */
export function formatCountdown(totalSeconds: number): string {
  const total = Math.max(0, Math.ceil(totalSeconds));
  const seconds = total % 60;
  const minutes = Math.floor(total / 60) % 60;
  const hours = Math.floor(total / 3600);
  const pad = (n: number) => String(n).padStart(2, "0");
  return hours ? `${hours}:${pad(minutes)}:${pad(seconds)}` : `${minutes}:${pad(seconds)}`;
}

/**
 * Format an LLM model identifier for display, falling back to "Platform
 * default" when the value is empty/null (traces, sessions, and analytics
 * all persist a nullable/blank model for calls that used the platform
 * default rather than an explicit override). Single source of truth so the
 * label is never rendered as a blank square/space in filter UIs.
 */
export function formatModelLabel(model: string | null | undefined, t_i18n: (id: string) => string): string {
  if (model === MODEL_INDEPENDENT) return t_i18n("Model-independent");
  return model && model.trim().length > 0 ? model : t_i18n("Platform default");
}

/**
 * True on macOS (and iOS) - decides whether a shortcut hint reads `Cmd` or
 * `Ctrl` (the command palette's `⌘K`, the deck editor's shortcut sheet).
 * Single source of truth: never re-spell the platform sniff in a component.
 */
export function isMac(): boolean {
  if (typeof navigator === "undefined") return false;
  return /mac|iphone|ipad/i.test(navigator.platform || navigator.userAgent);
}

/**
 * Parse a URL `page` query param into a safe zero-based page index.
 *
 * Guards against a hand-edited / stale value (e.g. `?page=abc`, `?page=-3`,
 * `?page=2.9`) so the pagination UI can never render "Page NaN" or compute a
 * negative / fractional offset. Anything that is not a finite positive integer
 * collapses to `0`.
 *
 * This is the single source of truth — do NOT inline `Number(...)` parsing of
 * the page param in page files.
 */
export function parsePageParam(value: string | null | undefined): number {
  const n = Math.floor(Number(value));
  return Number.isFinite(n) && n > 0 ? n : 0;
}

export { canManageEntity, canViewAgentChats, configLockReason, isEntityShared } from "@/lib/entity-permissions";

/**
 * Whether anybody but the viewer can reach this entity.
 *
 * Distinct from `isEntityShared`, which answers "was this shared *with* me" for
 * a badge on a list. This answers "whose work would a change to it reach", so
 * an entity the viewer owns and has shared counts, and a company-managed one
 * counts for everyone.
 */
export function reachesOthers(
  entity:
    | {
        is_company_managed: boolean;
        shared_group_ids: string[] | null;
        shared_user_ids?: string[] | null;
      }
    | null
    | undefined,
): boolean {
  if (!entity) return false;
  return (
    entity.is_company_managed ||
    !!entity.shared_group_ids?.length ||
    !!entity.shared_user_ids?.length
  );
}

/**
 * Strip markdown formatting from text for clean TTS (text-to-speech) output.
 * Removes code blocks, inline code, bold, italic, strikethrough, headings,
 * links, images, list markers, blockquotes, and collapses whitespace.
 *
 * This is the single source of truth — do NOT redefine in component files.
 */
export function stripMarkdown(md: string): string {
  return md
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\*\*(.+?)\*\*/g, "$1")
    .replace(/__(.+?)__/g, "$1")
    .replace(/\*(.+?)\*/g, "$1")
    .replace(/_(.+?)_/g, "$1")
    .replace(/~~(.+?)~~/g, "$1")
    .replace(/#{1,6}\s+/g, "")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/!\[([^\]]*)\]\([^)]+\)/g, "")
    .replace(/^\s*[-*+]\s+/gm, "")
    .replace(/^\s*\d+\.\s+/gm, "")
    .replace(/^\s*>\s+/gm, "")
    .replace(/\n{2,}/g, ". ")
    .replace(/\n/g, " ")
    .trim();
}

/**
 * Extract the set of required integration template provider names for an agent.
 * Pipeline tool refs (prepare/finalize) are intentionally excluded — their
 * integrations are loaded independently by the backend's pipeline executor
 * and must NOT be added to the LLM's tool belt.
 */
export function getAgentRequiredTemplateProviders(agent: Partial<Agent> | null | undefined): string[] {
  if (!agent) return [];
  return [...new Set(agent.integration_template_bindings || [])];
}

/**
 * Whether a per-user (template) binding can stand for *provider*. Not for a
 * provider whose every integration is a different API (OpenAPI): "the user's
 * own integration of this provider" names no API in particular, so it only
 * binds as one specific integration. Mirrors the backend's
 * ``PER_USER_UNRESOLVABLE_PROVIDERS``.
 */
export function resolvesPerUser(provider: string): boolean {
  return !isPerIntegrationApiProvider(provider);
}

/** Fields needed to decide whether a per-user binding may resolve to a row. */
type TemplateCandidate = Pick<
  Integration,
  | "id"
  | "provider"
  | "user_id"
  | "is_company_managed"
  | "auth_type"
  | "oauth_share_acknowledged"
>;

export {
  canAuthorizeIntegration,
  credentialShareChoice,
  isConnectedAccountWithheld,
  isPerUserIntegration,
  isSharedOauthIntegration,
  lendsConnectedAccount,
  isSharedTokenUnacknowledged,
} from "@/lib/oauth-share";

/**
 * True when an integration is a genuine company-wide instance — an explicit
 * "Company-managed" one, or a ``platform_managed`` one whose credentials are
 * minted per acting user rather than stored per owner.
 */
function isCompanyWideIntegration(
  integ: Pick<Integration, "is_company_managed" | "auth_type">,
): boolean {
  return integ.is_company_managed || integ.auth_type === "platform_managed";
}

/**
 * Integrations of *provider* a per-user binding is allowed to resolve to.
 *
 * Ownership matters: ``GET /integrations`` also returns integrations
 * *group-shared* by colleagues, so matching on provider alone resolved a
 * per-user binding to a teammate's personal account — and because these ids are
 * sent as the session's enabled integrations, the backend then ran the agent
 * with their credentials. Only the current user's OWN integrations and genuine
 * company-wide ones are eligible, exactly as the backend's
 * ``template_candidate_clause`` defines it — including its OAuth gate: a
 * company-wide OAuth row is only usable by a non-owner once its sharing
 * consent is recorded - which the Configuration card's save records
 * unless the owner opted out of sharing their connected account.
 * Skipping that gate here would leave the session claiming the provider is
 * enabled while the backend excludes it at runtime.
 * Own rows come first so callers that need a single pick can take the head.
 * A provider that does not resolve per user (see {@link resolvesPerUser}) has
 * no eligible row, as in the backend clause.
 */
export function eligibleTemplateIntegrations<T extends TemplateCandidate>(
  provider: string,
  connectedIntegrations: T[],
  currentUserId: string | undefined,
  excludedIds?: Set<string>,
): T[] {
  if (!resolvesPerUser(provider)) return [];
  const isOwn = (i: T) => !!currentUserId && i.user_id === currentUserId;
  const isUsableCompanyWide = (i: T) =>
    isCompanyWideIntegration(i) &&
    (i.auth_type !== "oauth" || i.oauth_share_acknowledged);
  return connectedIntegrations
    .filter(
      (i) =>
        i.provider === provider &&
        !excludedIds?.has(i.id) &&
        (isOwn(i) || isUsableCompanyWide(i)),
    )
    .sort((a, b) => Number(isOwn(b) && !b.is_company_managed) - Number(isOwn(a) && !a.is_company_managed));
}

/**
 * Resolve template bindings (provider names) to concrete integration IDs
 * by matching against the user's connected integrations.  This mirrors the
 * backend resolution in agent_chat.py and assignment_engine.py — including its
 * ownership rule (see {@link eligibleTemplateIntegrations}) and its single pick
 * per provider, preferring the user's own connection over a company-wide one.
 *
 * Static bindings are excluded to avoid double-counting integrations that
 * are both explicitly bound and template-matched.
 */
export function resolveTemplateBindings(
  templateProviders: string[] | null | undefined,
  connectedIntegrations: TemplateCandidate[],
  staticBindings: Record<string, string[]> | null | undefined,
  currentUserId: string | undefined,
): string[] {
  if (!templateProviders?.length) return [];
  const staticIds = new Set(Object.values(staticBindings ?? {}).flat());
  const ids: string[] = [];
  for (const provider of templateProviders) {
    const picked = eligibleTemplateIntegrations(
      provider,
      connectedIntegrations,
      currentUserId,
      staticIds,
    )[0];
    if (picked) ids.push(picked.id);
  }
  return ids;
}

/**
 * Resolve the default chat agent exactly the way the chat page does, so other
 * surfaces (notably the standalone home) pre-select and route to the SAME agent
 * and the experience stays aligned across modes. Priority:
 *   1. the user's explicit default agent (`User.default_agent_id`), if chat-usable
 *   2. the agent standing in for the General Assistant (`generalAssistantStandIn`)
 *   3. (XTM One) the CTEM Assistant by slug, else the first company-managed
 *      chat-eligible agent
 *   4. otherwise `null` = the General Assistant
 * Returns `null` while `agents` is still loading or when the General Assistant
 * is the resolved default.
 */
function resolveDefaultChatAgent(
  user: User | null | undefined,
  agents: Agent[] | undefined,
  isXtmOne: boolean,
  linkedAgentId?: string | null,
): Agent | null {
  if (!agents) return null;
  const chatEligible = (a: Agent) => a.enabled && !a.disable_chat;
  if (user?.default_agent_id) {
    const explicit = agents.find((a) => a.id === user.default_agent_id && chatEligible(a));
    if (explicit) return explicit;
  }
  const standIn = generalAssistantStandIn(linkedAgentId, agents);
  if (standIn) return standIn;
  if (isXtmOne) {
    const companyChat = (a: Agent) => chatEligible(a) && a.is_company_managed;
    return (
      agents.find((a) => a.slug === "ctem-assistant" && companyChat(a)) ||
      agents.find(companyChat) ||
      null
    );
  }
  return null;
}

/**
 * The agent a new `/chat` starts with: the one `?agent=` names, then the
 * launcher's hand-off (an agent id, or `CHAT_GENERAL_AGENT_PARAM`),
 * then the user's default (`resolveDefaultChatAgent`). `?agent=` and the
 * hand-off are explicit picks: an agent built for assignments (`disable_chat`)
 * opens too, as its page offers (a work item's Explain reaches its owner,
 * #774); the default names chat agents only. `null` = the General Assistant.
 */
export function initialChatAgent(
  picked: { agentParam: string | null; pendingAgent: string | null },
  user: User | null | undefined,
  agents: Agent[],
  isXtmOne: boolean,
  linkedAgentId: string | null,
): Agent | null {
  const enabledAgent = (id: string | null) => agents.find((a) => a.id === id && a.enabled) ?? null;
  return (
    enabledAgent(picked.agentParam) ??
    (picked.pendingAgent === CHAT_GENERAL_AGENT_PARAM
      ? generalAssistantStandIn(linkedAgentId, agents)
      : (enabledAgent(picked.pendingAgent) ?? resolveDefaultChatAgent(user, agents, isXtmOne, linkedAgentId)))
  );
}

/**
 * Short "extension label" for a file chip when the filename has no
 * extension. Mirrors the backend `_attachment_meta_from_file_attachment`
 * helper in `backend/app/api/v1/chat.py` so chip labels stay consistent
 * whether the `type` is supplied by the backend (chat view) or derived on
 * the client (run-detail view). Prefers the filename extension; otherwise
 * derives from the content-type subtype, dropping `+xml` / `+json`
 * suffixes and `;` params, and shortening `vnd.*` subtypes to their last
 * dotted segment — so e.g.
 * `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`
 * renders as `sheet` rather than the unwieldy full subtype.
 */
export function attachmentTypeLabel(
  filename: string,
  contentType: string | null | undefined,
): string {
  if (filename.includes(".")) {
    return filename.split(".").pop()!.toLowerCase();
  }
  const raw = (contentType ?? "").toLowerCase();
  const slash = raw.indexOf("/");
  let subtype = slash !== -1 ? raw.slice(slash + 1) : "";
  subtype = subtype.split("+")[0].split(";")[0].trim();
  if (subtype.startsWith("vnd.")) {
    subtype = subtype.split(".").pop() ?? subtype;
  }
  return subtype || "txt";
}

/**
 * `safeHttpUrl()` (`lib/safe-url.ts`, the one rule) for an attribute that
 * takes `undefined` rather than `null`: an absolute http(s) URL without
 * userinfo, else `undefined`, so a stored, unvalidated URL never reaches an
 * `href` or `window.open()` as an executable target.
 */
export function safeExternalUrl(url?: string | null): string | undefined {
  return safeHttpUrl(url) ?? undefined;
}


/**
 * Save a downloaded blob to the user's disk under *filename*.
 *
 * The `createObjectURL` → anchor → click → `revokeObjectURL` dance, in one place.
 * The revoke matters: without it the blob is pinned in memory for the lifetime of
 * the document, which for a multi-megabyte template package is a real leak.
 *
 * Pair it with `api.download()`, which already parses the server's
 * `Content-Disposition` filename via `filenameFromContentDisposition()` — pass
 * that through and fall back to your own name only when the header is absent.
 *
 * This replaced a local `triggerBrowserDownload` in `AssetsTab.tsx` and the API
 * hooks' own copies, so this is the only copy of the sequence under
 * `hooks/api/`. Reach for it rather than adding a new one.
 *
 * (An earlier version of this note said one of those hook copies was missing its
 * `revokeObjectURL`. That was wrong — all of them revoked; the migration was a
 * dedupe, not a leak fix.)
 */
export function saveBlobAs(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}


/**
 * Encode a model choice as `<provider_id>::<model_id>` for a Select's value.
 *
 * A model id is not unique — two provider configs can advertise the same one — so
 * a bare id as the value gives React duplicate keys, makes Radix render every
 * match in the trigger, and cannot express which config the admin meant.
 */
export function modelChoiceValue(
  model?: string | null,
  providerId?: string | null,
): string {
  return model ? `${providerId ?? ""}::${model}` : "";
}

/** The model picker's value for "run on the platform routing model". */
export const ROUTING_MODEL_CHOICE = "__routing__";

/** An agent's model picker value: the routing model, or its own model choice. */
export function agentModelChoice(agent: {
  llm_model_source?: string | null;
  llm_model?: string | null;
  llm_provider_id?: string | null;
}): string {
  return agent.llm_model_source === "routing"
    ? ROUTING_MODEL_CHOICE
    : modelChoiceValue(agent.llm_model, agent.llm_provider_id);
}

/**
 * Decode a `modelChoiceValue()`. A value with no separator is treated as a bare
 * model id with no provider, which is what an older stored choice looks like.
 */
export function parseModelChoice(value: string): { model: string; providerId: string | null } {
  const separator = value.indexOf("::");
  if (separator < 0) return { model: value, providerId: null };
  return { model: value.slice(separator + 2), providerId: value.slice(0, separator) || null };
}


/** The name to show for one provider config: its own name, else the vendor's. */
export function effectiveProviderName(
  p: { provider: string; display_name?: string | null },
): string {
  return p.display_name || PROVIDER_DISPLAY_NAMES[p.provider] || p.provider;
}

type ProviderConfigRef = {
  id: string;
  provider: string;
  display_name?: string | null;
};

/**
 * Which of the identically-named configs this one is — 2 for the second, 3 for
 * the third, and null for the first (and for a name that is already unique).
 *
 * `display_name` is optional, so several configs of one vendor can all fall back
 * to the vendor's name and read identically. This is what distinguishes them,
 * following the familiar "name", "name (2)" convention so the oldest config
 * keeps the name an admin already recognises.
 *
 * The order is the one `providers` arrives in — the list endpoint orders by
 * `created_at, id`. Re-deriving it here would be a second source for one
 * ordering, free to disagree with the list it labels.
 */
function providerOrdinal(
  config: ProviderConfigRef,
  providers?: ProviderConfigRef[] | null,
): number | null {
  const base = effectiveProviderName(config);
  const sameName = (providers ?? []).filter(
    (p) => effectiveProviderName(p) === base,
  );
  if (sameName.length < 2) return null;
  const index = sameName.findIndex((p) => p.id === config.id);
  return index < 1 ? null : index + 1;
}

/**
 * The name of one provider config, everywhere it is shown: `Anthropic`,
 * `Anthropic (2)`, or whatever the admin named it.
 *
 * Single formatter on purpose — the providers list and every model picker must
 * read the same, or the same config appears under two names.
 */
export function providerConfigName(
  config: ProviderConfigRef,
  providers?: ProviderConfigRef[] | null,
): string {
  const base = effectiveProviderName(config);
  const ordinal = providerOrdinal(config, providers);
  return ordinal ? `${base} (${ordinal})` : base;
}

/**
 * The name to show for a provider, preferring the specific config.
 *
 * Resolving by config id first is what lets two configs of one vendor read
 * differently — a picker that renders both as "Anthropic" cannot express which
 * one a choice meant, which is the point.
 *
 * Identically-named configs are disambiguated by `providerConfigName`, so a
 * picker option and the providers list always show the same name for the same
 * config.
 */
export function providerDisplayName(
  provider: string,
  providerId?: string | null,
  providers?: ProviderConfigRef[] | null,
): string {
  const byId = providerId ? providers?.find((p) => p.id === providerId) : undefined;
  if (byId) return providerConfigName(byId, providers);
  const byType = providers?.find((p) => p.provider === provider);
  if (byType?.display_name) return effectiveProviderName(byType);
  return PROVIDER_DISPLAY_NAMES[provider] || provider;
}

/**
 * The decision providers whose model id is typed: those that cannot list
 * their decision models (OpenRouter). One that lists is picked from its list,
 * even when it lists none.
 */
export function typedDecisionProviders(providers: LLMProvider[] | null | undefined) {
  return (providers ?? [])
    .filter((p) => p.capabilities?.includes("decision") && !p.lists_decision_models)
    .map((p) => ({ id: p.id, label: providerDisplayName(p.provider, p.id, providers) }));
}

/** Label a model option as `model (provider)`. */
export function modelOptionLabel(
  m: { model: string; provider: string; provider_id?: string | null },
  providers?: ProviderConfigRef[] | null,
): string {
  return `${m.model} (${providerDisplayName(m.provider, m.provider_id, providers)})`;
}
