/**
 * Unit tests for the security audit report logic - `yarn test:unit`.
 *
 * The audit keeps gaining checks, so the screen must count, rank, filter and
 * name a category or a severity it has never seen instead of dropping it.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import type { SecurityAuditFinding, SecurityControlStatus } from "@/types";
import {
  CATEGORY_LABELS,
  CONTROL_STATE_LABELS,
  DISMISSABLE_SEVERITIES,
  KNOWN_CONTROL_STATES,
  KNOWN_SEVERITIES,
  NO_CONTROL_FILTERS,
  NO_FILTERS,
  SEVERITY_HINTS,
  SEVERITY_LABELS,
  canDismiss,
  categoryLabel,
  categoryRank,
  controlCategories,
  controlRank,
  controlStateLabel,
  controlStateTone,
  countOf,
  filterControls,
  filterFindings,
  findingKeys,
  findingsByControl,
  focusedFindings,
  fold,
  groupFindings,
  hardenedPhrase,
  hasActiveControlFilters,
  hasActiveFilters,
  headline,
  humanizeKey,
  isDismissed,
  passRate,
  percent,
  relativeTimeParts,
  scoreLabel,
  scoreTone,
  severityHint,
  severityLabel,
  severityRank,
  severityTone,
  sortControls,
  splitConfigTokens,
  splitDismissed,
  summarizeCategories,
  tally,
  tallyControls,
  worstLinkedFinding,
} from "./security-audit.ts";

const finding = (overrides: Partial<SecurityAuditFinding> = {}): SecurityAuditFinding => ({
  id: null,
  category: "infrastructure",
  severity: "pass",
  title: "Redis connection healthy",
  description: "Redis is reachable and responding.",
  recommendation: null,
  ...overrides,
});

const REPORT: SecurityAuditFinding[] = [
  finding({ id: "encryption.secret_key_configured", category: "encryption", severity: "pass", title: "Secret key configured" }),
  finding({
    id: "authentication.registration_token_default",
    category: "authentication",
    severity: "critical",
    title: "Platform registration token is the published default",
    recommendation: "Generate a random token and set PLATFORM_REGISTRATION_TOKEN.",
  }),
  finding({ id: "authentication.sso_not_configured", category: "authentication", severity: "info", title: "SSO not configured" }),
  finding({ id: "authentication.admin_email_configured", category: "authentication", severity: "pass", title: "Admin email configured" }),
  finding({ id: "infrastructure.debug_mode", category: "infrastructure", severity: "warning", title: "Debug mode is enabled", recommendation: "Set DEBUG=false in production environments." }),
  finding({ id: "infrastructure.redis_healthy", category: "infrastructure", severity: "pass" }),
  finding({ id: "mcp.global_key_reach", category: "mcp", severity: "notice", title: "The global MCP key reaches 2 agent(s)" }),
];

test("a tally counts every known severity, zero included, and keeps a severity it does not know", () => {
  const t = tally(REPORT);
  assert.deepEqual(t.severities, [
    { severity: "critical", count: 1 },
    { severity: "warning", count: 1 },
    { severity: "notice", count: 1 },
    { severity: "info", count: 1 },
    { severity: "pass", count: 3 },
  ]);
  assert.equal(t.total, 7);
  assert.equal(t.passed, 3);
  // An informational finding neither passes nor fails; an unknown severity is not a pass.
  assert.equal(t.checked, 6);
  assert.equal(countOf(t, "notice"), 1);
  assert.equal(countOf(t, "missing"), 0);
});

test("an empty report still has a card for each known severity and nothing to rate", () => {
  const t = tally([]);
  assert.deepEqual(t.severities.map((s) => s.severity), [...KNOWN_SEVERITIES]);
  assert.ok(t.severities.every((s) => s.count === 0));
  assert.equal(passRate(t), null);
  assert.deepEqual(headline(t), { kind: "clear" });
});

test("the pass rate leaves informational findings out", () => {
  const t = tally([finding({ severity: "pass" }), finding({ severity: "info" }), finding({ severity: "warning" })]);
  assert.equal(passRate(t), 0.5);
  assert.equal(passRate(tally([finding({ severity: "info" })])), null);
});

test("a percentage never claims 100 while a check fails, nor 0 while one passed", () => {
  assert.equal(percent(1), 100);
  assert.equal(percent(0), 0);
  assert.equal(percent(0.996), 99);
  assert.equal(percent(0.004), 1);
  assert.equal(percent(2 / 3), 67);
});

test("the headline names the most urgent severity the report carries", () => {
  assert.deepEqual(headline(tally(REPORT)), { kind: "critical", count: 1 });
  assert.deepEqual(headline(tally(REPORT.filter((f) => f.severity !== "critical"))), { kind: "warning", count: 1 });
  assert.deepEqual(headline(tally([finding({ severity: "notice" }), finding({ severity: "pass" })])), { kind: "review", count: 1 });
  assert.deepEqual(headline(tally([finding({ severity: "info" }), finding({ severity: "pass" })])), { kind: "clear" });
});

test("an unknown severity ranks right after warning, with a neutral tone", () => {
  assert.ok(severityRank("critical") < severityRank("warning"));
  assert.ok(severityRank("warning") < severityRank("high"));
  assert.ok(severityRank("high") < severityRank("info"));
  assert.ok(severityRank("info") < severityRank("pass"));
  assert.equal(severityTone("high"), "neutral");
  assert.equal(severityTone("critical"), "danger");
  assert.equal(severityTone("pass"), "success");
});

test("categories are summarised worst first, then by how many findings share that severity", () => {
  const summaries = summarizeCategories([
    ...REPORT,
    finding({ category: "channels", severity: "warning", title: "2 channel bot(s) allow anonymous access" }),
    finding({ category: "channels", severity: "warning", title: "Another warning" }),
  ]);
  assert.deepEqual(
    summaries.map((s) => [s.category, s.worst]),
    [
      ["authentication", "critical"],
      ["channels", "warning"],
      ["infrastructure", "warning"],
      ["mcp", "notice"],
      ["encryption", "pass"],
    ],
  );
  const authentication = summaries[0];
  assert.equal(authentication.total, 3);
  assert.equal(passRate(authentication), 0.5);
});

test("a category or severity nobody named reads from its key, never from Object.prototype", () => {
  assert.equal(categoryLabel("agents"), CATEGORY_LABELS.agents);
  assert.equal(categoryLabel("mcp"), CATEGORY_LABELS.mcp);
  assert.equal(categoryLabel("dns"), "DNS");
  assert.equal(categoryLabel("evaluations"), "Evaluations");
  assert.equal(categoryLabel("toString"), "ToString");
  assert.equal(categoryLabel("constructor"), "Constructor");
  assert.equal(severityLabel("pass"), SEVERITY_LABELS.pass);
  assert.equal(severityLabel("high_risk"), "High risk");
  assert.equal(severityHint("high_risk"), SEVERITY_HINTS.other);
  assert.equal(scoreLabel("at_risk"), "At risk");
  assert.equal(scoreLabel("under_review"), "Under review");
  assert.equal(scoreTone("under_review"), "neutral");
  assert.equal(scoreTone("toString"), "neutral");
  assert.equal(humanizeKey("sso"), "SSO");
  assert.equal(humanizeKey(""), "");
});

test("every known severity has a label and a hint", () => {
  for (const severity of KNOWN_SEVERITIES) {
    assert.ok(SEVERITY_LABELS[severity]);
    assert.ok(SEVERITY_HINTS[severity]);
  }
});

test("filters combine: severities (any of), category, and every word of the search", () => {
  assert.equal(filterFindings(REPORT, NO_FILTERS).length, REPORT.length);
  assert.equal(hasActiveFilters(NO_FILTERS), false);

  const problems = filterFindings(REPORT, { ...NO_FILTERS, severities: ["critical", "warning"] });
  assert.deepEqual(problems.map((f) => f.id), ["authentication.registration_token_default", "infrastructure.debug_mode"]);

  const authentication = filterFindings(REPORT, { ...NO_FILTERS, category: "authentication", severities: ["pass"] });
  assert.deepEqual(authentication.map((f) => f.id), ["authentication.admin_email_configured"]);

  // Words in any order, matched in the recommendation too.
  const search = filterFindings(REPORT, { ...NO_FILTERS, query: "  production   debug " });
  assert.deepEqual(search.map((f) => f.id), ["infrastructure.debug_mode"]);
  assert.equal(filterFindings(REPORT, { ...NO_FILTERS, query: "debug kubernetes" }).length, 0);
  assert.equal(hasActiveFilters({ ...NO_FILTERS, query: " " }), false);
  assert.equal(hasActiveFilters({ ...NO_FILTERS, query: "x" }), true);
});

test("the search ignores case and accents and reads the labels the screen shows", () => {
  assert.equal(fold("Sécurité ÉLEVÉE"), "securite elevee");
  const labelled = filterFindings(REPORT, { ...NO_FILTERS, query: "critique" }, (f) =>
    f.severity === "critical" ? "Critique Authentification" : "",
  );
  assert.deepEqual(labelled.map((f) => f.id), ["authentication.registration_token_default"]);
  assert.deepEqual(filterFindings(REPORT, { ...NO_FILTERS, query: "SECRET_KEY_configured" }).map((f) => f.id), [
    "encryption.secret_key_configured",
  ]);
  // The API's own names match whatever the screen's language: the severity as
  // well as the category.
  assert.deepEqual(filterFindings(REPORT, { ...NO_FILTERS, query: "critical authentication" }).map((f) => f.id), [
    "authentication.registration_token_default",
  ]);
});

test("grouped by severity: most urgent group first, the audit's order inside a group", () => {
  const groups = groupFindings(REPORT, "severity");
  assert.deepEqual(groups.map((g) => g.key), ["critical", "warning", "notice", "info", "pass"]);
  assert.deepEqual(groups[4].findings.map((f) => f.id), [
    "encryption.secret_key_configured",
    "authentication.admin_email_configured",
    "infrastructure.redis_healthy",
  ]);
});

test("grouped by category: worst category first, its most urgent findings first", () => {
  const groups = groupFindings(REPORT, "category");
  assert.deepEqual(groups.map((g) => g.key), ["authentication", "infrastructure", "mcp", "encryption"]);
  assert.deepEqual(groups[0].findings.map((f) => f.severity), ["critical", "info", "pass"]);
  assert.deepEqual(groups[1].findings.map((f) => f.severity), ["warning", "pass"]);
});

test("finding keys use the id, fall back to what the finding says, and never collide", () => {
  const twin = finding({ title: "Same" });
  const keys = findingKeys([finding({ id: "infrastructure.redis_healthy" }), twin, twin, finding({ title: "Other" })]);
  assert.equal(keys[0], "id:infrastructure.redis_healthy");
  assert.equal(new Set(keys).size, keys.length);
  assert.equal(keys[2], `${keys[1]}#1`);
});

test("configuration names are set apart from the prose around them", () => {
  const text = "Set DEBUG=false, then DISABLE_CUSTOM_TOOLS=true. Check REDIS_URL (or SSO, LLM and CORS).";
  const segments = splitConfigTokens(text);
  assert.deepEqual(
    segments.filter((s) => s.code).map((s) => s.text),
    ["DEBUG=false", "DISABLE_CUSTOM_TOOLS=true", "REDIS_URL"],
  );
  assert.equal(segments.map((s) => s.text).join(""), text);
});

test("a name inside a word or a lower-case identifier is prose", () => {
  for (const text of ["xtmOne.platform.env", "webhook_secret", "UPGRADING.md", "SECRET_KEYs", "X-Forwarded-Proto", "fooBAR_BAZ"]) {
    assert.deepEqual(splitConfigTokens(text), [{ text, code: false }], text);
  }
  assert.deepEqual(splitConfigTokens(""), []);
  assert.deepEqual(splitConfigTokens("CUSTOM_TOOL_SANDBOX=auto").map((s) => s.code), [true]);
});

// ---------------------------------------------------------------------------
// Controls
// ---------------------------------------------------------------------------

const control = (overrides: Partial<SecurityControlStatus> = {}): SecurityControlStatus => ({
  id: "web.public_widget_strict_rendering",
  category: "web",
  title: "Public chat widget strict rendering",
  setting: "PUBLIC_WIDGET_STRICT_RENDERING",
  stored_in: "config",
  where: "",
  value: "false",
  default: "false",
  hardened: "true",
  state: "off",
  opt_in: true,
  impact: "What enforcing it changes.",
  evidence: "",
  ...overrides,
});

const CONTROLS: SecurityControlStatus[] = [
  control({ id: "infrastructure.debug_mode", category: "infrastructure", title: "Debug mode off", setting: "DEBUG", state: "enforced", opt_in: false }),
  control({ id: "tools.tool_call_budget_strict", category: "tools", title: "Tool-call budget cut strictly", setting: "AGENT_LOOP_MAX_TOOL_CALLS_STRICT", state: "monitoring" }),
  control({ id: "authentication.min_password_length", category: "authentication", title: "Passwords of 12 characters or more", setting: "MIN_PASSWORD_LENGTH", value: "8", hardened: ">= 12" }),
  control({ impact: "Public chat widget answers show only the images the platform serves." }),
  control({
    id: "mcp.xtm_mcp_server",
    category: "mcp",
    title: "XTM One MCP server off",
    setting: "xtm_mcp_enabled",
    stored_in: "platform_settings",
    where: "Settings > Integrations > MCP Endpoint",
    opt_in: false,
  }),
  control({ id: "compliance.retention", category: "compliance", title: "Retention bounded", setting: "RETENTION_DAYS", state: "partial" }),
];

const LINKED: SecurityAuditFinding[] = [
  finding({ id: "web.public_widget_remote_images", category: "web", severity: "warning", title: "Public chat widget answers can load remote images", control: "web.public_widget_strict_rendering" }),
  finding({ id: "infrastructure.debug_off", category: "infrastructure", severity: "pass", title: "Debug mode is off", control: "infrastructure.debug_mode" }),
  finding({ id: "authentication.password_length_short", category: "authentication", severity: "info", title: "Passwords need only 8 character(s)", control: "authentication.min_password_length" }),
  finding({ id: "authentication.password_length_hint", category: "authentication", severity: "critical", title: "A critical finding about passwords", control: "authentication.min_password_length" }),
  finding({ id: "infrastructure.redis_healthy", category: "infrastructure", severity: "pass" }),
];

test("the categories are the taxonomy's fourteen, in its order", () => {
  assert.deepEqual(Object.keys(CATEGORY_LABELS), [
    "authentication",
    "access",
    "encryption",
    "network",
    "web",
    "integrations",
    "agents",
    "assignments",
    "flows",
    "channels",
    "tools",
    "mcp",
    "llm",
    "infrastructure",
  ]);
  assert.ok(categoryRank("authentication") < categoryRank("infrastructure"));
  assert.equal(categoryRank("compliance"), Object.keys(CATEGORY_LABELS).length);
  assert.equal(categoryRank("constructor"), Object.keys(CATEGORY_LABELS).length);
});

test("a control tally counts every known state, zero included, and keeps a state it does not know", () => {
  const t = tallyControls(CONTROLS);
  assert.deepEqual(t.states, [
    { state: "off", count: 3 },
    { state: "partial", count: 1 },
    { state: "monitoring", count: 1 },
    { state: "enforced", count: 1 },
  ]);
  assert.equal(t.total, 6);
  assert.equal(t.enforced, 1);
  assert.equal(t.monitoring, 1);
  const empty = tallyControls([]);
  assert.deepEqual(empty.states.map((s) => s.state), ["off", "monitoring", "enforced"]);
  assert.equal(empty.total, 0);
});

test("every known control state has a label and a tone; an unknown one reads from its key, neutral", () => {
  for (const state of KNOWN_CONTROL_STATES) assert.ok(CONTROL_STATE_LABELS[state]);
  assert.equal(controlStateLabel("enforced"), "Enforced");
  assert.equal(controlStateLabel("report_only"), "Report only");
  assert.equal(controlStateTone("enforced"), "success");
  assert.equal(controlStateTone("monitoring"), "info");
  assert.equal(controlStateTone("off"), "warning");
  assert.equal(controlStateTone("partial"), "neutral");
  assert.equal(controlStateTone("toString"), "neutral");
});

test("the findings about a control are the ones naming it, most urgent first", () => {
  const index = findingsByControl(LINKED);
  assert.deepEqual(index.get("authentication.min_password_length")?.map((f) => f.severity), ["critical", "info"]);
  assert.equal(index.get("web.public_widget_strict_rendering")?.length, 1);
  assert.equal(index.get("infrastructure.redis_healthy"), undefined);
  assert.equal([...index.values()].flat().length, 4);
});

test("controls sort off-with-a-warning first, then off, an unknown state, monitoring, enforced", () => {
  const index = findingsByControl(LINKED);
  assert.equal(controlRank(CONTROLS[3], index.get(CONTROLS[3].id) ?? []), 0);
  assert.equal(controlRank(CONTROLS[3], []), 1);
  // A pass about an off control does not raise it.
  assert.equal(controlRank(control({ state: "off" }), [finding({ severity: "pass" })]), 1);
  assert.deepEqual(sortControls(CONTROLS, index).map((c) => c.id), [
    // Off with a critical finding (authentication) and with a warning (web): both first, category order between them.
    "authentication.min_password_length",
    "web.public_widget_strict_rendering",
    "mcp.xtm_mcp_server",
    "compliance.retention",
    "tools.tool_call_budget_strict",
    "infrastructure.debug_mode",
  ]);
});

test("the categories of the controls come in the taxonomy's order, unknown ones last", () => {
  assert.deepEqual(controlCategories(CONTROLS), ["authentication", "web", "tools", "mcp", "infrastructure", "compliance"]);
});

test("control filters combine states (any of), category, and every word across title, setting, id and impact", () => {
  assert.equal(filterControls(CONTROLS, NO_CONTROL_FILTERS).length, CONTROLS.length);
  assert.equal(hasActiveControlFilters(NO_CONTROL_FILTERS), false);
  assert.deepEqual(
    filterControls(CONTROLS, { ...NO_CONTROL_FILTERS, states: ["monitoring", "enforced"] }).map((c) => c.id),
    ["infrastructure.debug_mode", "tools.tool_call_budget_strict"],
  );
  assert.deepEqual(filterControls(CONTROLS, { ...NO_CONTROL_FILTERS, category: "mcp" }).map((c) => c.id), ["mcp.xtm_mcp_server"]);
  assert.deepEqual(filterControls(CONTROLS, { ...NO_CONTROL_FILTERS, query: "min_password" }).map((c) => c.id), [
    "authentication.min_password_length",
  ]);
  assert.deepEqual(filterControls(CONTROLS, { ...NO_CONTROL_FILTERS, query: "images serves" }).map((c) => c.id), [
    "web.public_widget_strict_rendering",
  ]);
  assert.deepEqual(filterControls(CONTROLS, { ...NO_CONTROL_FILTERS, query: "endpoint" }).map((c) => c.id), ["mcp.xtm_mcp_server"]);
  const labelled = filterControls(CONTROLS, { ...NO_CONTROL_FILTERS, query: "applique" }, (c) => (c.state === "enforced" ? "Appliqué" : ""));
  assert.deepEqual(labelled.map((c) => c.id), ["infrastructure.debug_mode"]);
  // The API's own names match whatever the screen's language, as in the
  // findings search: the state as well as the category.
  assert.deepEqual(filterControls(CONTROLS, { ...NO_CONTROL_FILTERS, query: "monitoring tools" }).map((c) => c.id), [
    "tools.tool_call_budget_strict",
  ]);
  assert.equal(hasActiveControlFilters({ ...NO_CONTROL_FILTERS, states: ["off"] }), true);
  assert.equal(hasActiveControlFilters({ ...NO_CONTROL_FILTERS, query: "  " }), false);
});

test("the hardened value reads as a comparison, emptiness, or the value itself", () => {
  assert.deepEqual(hardenedPhrase(">= 12"), { kind: "atLeast", value: "12" });
  assert.deepEqual(hardenedPhrase("<= 1440"), { kind: "atMost", value: "1440" });
  assert.deepEqual(hardenedPhrase("> 0"), { kind: "above", value: "0" });
  assert.deepEqual(hardenedPhrase("<5"), { kind: "below", value: "5" });
  assert.deepEqual(hardenedPhrase("empty"), { kind: "empty" });
  assert.deepEqual(hardenedPhrase(" true "), { kind: "value", value: "true" });
  assert.deepEqual(hardenedPhrase("docker or opensandbox"), { kind: "value", value: "docker or opensandbox" });
  assert.deepEqual(hardenedPhrase(">= 12 or more"), { kind: "value", value: ">= 12 or more" });
});

test("the relative time reads now, then minutes, hours and days, and never the future", () => {
  const now = Date.UTC(2026, 8, 27, 12, 0, 0);
  const ago = (seconds: number) => relativeTimeParts(now - seconds * 1000, now);
  assert.deepEqual(ago(0), { value: 0, unit: "second" });
  assert.deepEqual(ago(44), { value: 0, unit: "second" });
  assert.deepEqual(ago(-120), { value: 0, unit: "second" });
  assert.deepEqual(ago(50), { value: -1, unit: "minute" });
  assert.deepEqual(ago(44 * 60), { value: -44, unit: "minute" });
  assert.deepEqual(ago(45 * 60), { value: -1, unit: "hour" });
  assert.deepEqual(ago(21 * 3600), { value: -21, unit: "hour" });
  assert.deepEqual(ago(22 * 3600), { value: -1, unit: "day" });
  assert.deepEqual(ago(3 * 86400), { value: -3, unit: "day" });
  assert.equal(relativeTimeParts(Number.NaN, now), null);
  // Without a `now`, the clock is read at the call.
  assert.deepEqual(relativeTimeParts(Date.now()), { value: 0, unit: "second" });
  assert.deepEqual(relativeTimeParts(Date.now() - 3 * 3600 * 1000), { value: -3, unit: "hour" });
});

test("a Security options link focuses only the findings this run reports", () => {
  assert.deepEqual(
    [...focusedFindings(REPORT, ["authentication.registration_token_default", "tools.runner_path_links"])],
    ["authentication.registration_token_default"],
  );
  assert.equal(focusedFindings(REPORT, []).size, 0);
  // A finding without an id is never focused, whatever is asked for.
  assert.equal(focusedFindings([finding()], [""]).size, 0);
});

// ---------------------------------------------------------------------------
// Dismissals
// ---------------------------------------------------------------------------

const DISMISSAL = { at: "2026-09-30T10:00:00+00:00", severity: "warning", reason: "Accepted risk", by_user_id: "u-1", by: "Ada Admin" };

test("a dismissed finding leaves every count and is listed apart, most urgent first", () => {
  const report = [
    finding({ id: "web.remote_images", category: "web", severity: "info", dismissed: { ...DISMISSAL, severity: "info" } }),
    finding({ id: "infrastructure.debug_mode", category: "infrastructure", severity: "warning" }),
    finding({ id: "authentication.registration_token_default", category: "authentication", severity: "critical", dismissed: DISMISSAL }),
    finding({ id: "encryption.secret_key_configured", category: "encryption", severity: "pass" }),
  ];
  const { active, dismissed } = splitDismissed(report);
  assert.deepEqual(active.map((f) => f.id), ["infrastructure.debug_mode", "encryption.secret_key_configured"]);
  assert.deepEqual(dismissed.map((f) => f.id), ["authentication.registration_token_default", "web.remote_images"]);
  assert.equal(countOf(tally(active), "critical"), 0);
  assert.deepEqual(headline(tally(active)), { kind: "warning", count: 1 });
  assert.equal(isDismissed(report[0]), true);
  assert.equal(isDismissed(report[1]), false);
  // A reopened finding is no longer dismissed: it counts again.
  const reopened = finding({ id: "web.remote_images", severity: "critical", reopened: { at: DISMISSAL.at, severity: "critical", dismissal: { ...DISMISSAL, at: null } } });
  assert.deepEqual(splitDismissed([reopened]).active, [reopened]);
});

test("only a critical, warning or info finding with the audit's id shape is offered a dismissal", () => {
  assert.deepEqual([...DISMISSABLE_SEVERITIES], ["critical", "warning", "info"]);
  for (const severity of ["critical", "warning", "info"]) {
    assert.equal(canDismiss(finding({ id: "web.public_widget_remote_images", severity })), true);
  }
  for (const severity of ["pass", "notice"]) {
    assert.equal(canDismiss(finding({ id: "web.public_widget_remote_images", severity })), false);
  }
  for (const id of [null, "", "web", "Web.remote_images", "web.remote-images", "web.remote_images.more", `web.${"x".repeat(200)}`]) {
    assert.equal(canDismiss(finding({ id, severity: "warning" })), false, String(id));
  }
  assert.equal(canDismiss(finding({ id: "web.remote_images", severity: "warning", dismissed: DISMISSAL })), false);
});

test("a dismissed finding neither raises its control nor stands for it", () => {
  const off = control({ state: "off" });
  const accepted = finding({ id: "web.remote_images", severity: "critical", control: off.id, dismissed: { ...DISMISSAL, severity: "critical" } });
  assert.equal(controlRank(off, [accepted]), 1);
  assert.equal(worstLinkedFinding([accepted]), null);
  const info = finding({ id: "web.widget_hint", severity: "info", control: off.id });
  const linked = findingsByControl([accepted, info]).get(off.id) ?? [];
  assert.deepEqual(linked.map((f) => f.id), ["web.remote_images", "web.widget_hint"]);
  assert.equal(worstLinkedFinding(linked)?.id, "web.widget_hint");
  assert.equal(controlRank(off, linked), 1);
});
