/**
 * Unit tests for the Settings > Access and security options card - `yarn test:unit`.
 *
 * A locked option names the variable that sets it, or says why the platform
 * mode fixes it, and never reaches the PUT body; an editable one is
 * sent only when its draft changed.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import type { SecurityOption } from "@/types";
import {
  DEFAULT_VALUE_MESSAGE,
  LOCKED_BY_ENV_MESSAGE,
  LOCKED_BY_PLATFORM_MODE_MESSAGE,
  SAVED_IN_OPTIONS_MESSAGE,
  SECURITY_OPTION_CHOICE_LABELS,
  SECURITY_OPTION_FINDINGS,
  SECURITY_OPTION_LABELS,
  SECURITY_OPTION_PINNED_REASONS,
  auditFindingPath,
  changedValues,
  controlOption,
  controlOptionKey,
  displayValue,
  fieldText,
  groupLabel,
  groupOptions,
  intProblem,
  invalidListEntries,
  linkedOptionKey,
  listProblem,
  lockMessage,
  optionAnchorId,
  optionInfo,
  optionLocation,
  optionSourceMessage,
  parseInteger,
  parseList,
  recommendedOptionKey,
  targetedOptionKey,
} from "./security-options.ts";

const option = (overrides: Partial<SecurityOption> = {}): SecurityOption => ({
  key: "trust_proxy_headers",
  env_var: "TRUST_PROXY_HEADERS",
  kind: "bool",
  group: "network",
  choices: [],
  minimum: null,
  maximum: null,
  off_value: null,
  value: false,
  default: false,
  stored: null,
  source: "default",
  locked: false,
  locked_reason: null,
  ...overrides,
});

const t = (id: string, values?: Record<string, string | number>) =>
  id.replace(/\{(\w+)\}/g, (_, name: string) => String(values?.[name] ?? ""));

/** The quota fail mode as XTM One reports it: closed, locked by the platform mode. */
const pinnedQuotaFailMode = (overrides: Partial<SecurityOption> = {}): SecurityOption =>
  option({
    key: "quota_fail_mode",
    env_var: "QUOTA_FAIL_MODE",
    kind: "choice",
    choices: ["open", "closed"],
    value: "closed",
    default: "closed",
    stored: "open",
    source: "platform_mode",
    locked: true,
    locked_reason: "platform_mode",
    ...overrides,
  });

test("a locked option says which environment variable sets it", () => {
  const locked = option({ locked: true, locked_reason: "environment", source: "environment", value: true });
  assert.equal(lockMessage(locked, t), "Set by environment variable TRUST_PROXY_HEADERS");
  assert.equal(LOCKED_BY_ENV_MESSAGE, "Set by environment variable {name}");
});

test("an option the platform mode fixes says why, never that a variable sets it", () => {
  const message = lockMessage(pinnedQuotaFailMode(), t);
  assert.equal(message, SECURITY_OPTION_PINNED_REASONS.quota_fail_mode);
  assert.match(message ?? "", /quotas are part of the licence/);
  assert.doesNotMatch(message ?? "", /environment variable/);
});

test("a fixed option this frontend has no reason for still says it is fixed", () => {
  const unknown = pinnedQuotaFailMode({ key: "some_future_option", env_var: "SOME_FUTURE_OPTION" });
  assert.equal(lockMessage(unknown, t), LOCKED_BY_PLATFORM_MODE_MESSAGE);
});

test("an editable option shows no lock", () => {
  assert.equal(lockMessage(option(), t), null);
});

test("a locked option never reaches the PUT body, even with a draft", () => {
  const locked = option({ locked: true, locked_reason: "environment", value: true });
  assert.deepEqual(changedValues([locked], { trust_proxy_headers: false }), {});
  assert.deepEqual(changedValues([pinnedQuotaFailMode()], { quota_fail_mode: "open" }), {});
});

test("nothing links to the field of an option the platform mode fixes", () => {
  const options = [pinnedQuotaFailMode()];
  assert.equal(linkedOptionKey("infrastructure.quota_fails_open", options), null);
  assert.equal(controlOptionKey({ setting: "QUOTA_FAIL_MODE", stored_in: "config" }, options), null);
  const editable = [pinnedQuotaFailMode({ locked: false, locked_reason: null, source: "default" })];
  assert.equal(controlOptionKey({ setting: "QUOTA_FAIL_MODE", stored_in: "config" }, editable), "quota_fail_mode");
});

test("only changed editable values are sent", () => {
  const options = [
    option(),
    option({ key: "min_password_length", kind: "int", value: 8, minimum: 1, maximum: 1024 }),
    option({ key: "oauth_token_dev_trusted_origins", kind: "origins", value: ["http://localhost:8080"] }),
  ];
  assert.deepEqual(
    changedValues(options, {
      trust_proxy_headers: true,
      min_password_length: 8,
      oauth_token_dev_trusted_origins: ["http://localhost:8080"],
    }),
    { trust_proxy_headers: true },
  );
});

test("a reset sends null", () => {
  const stored = option({ value: true, stored: true, source: "database" });
  assert.deepEqual(changedValues([stored], { trust_proxy_headers: null }), { trust_proxy_headers: null });
});

test("a reset of a stored empty list is sent too", () => {
  const cleared = option({ key: "custom_tool_extra_allowed_modules", kind: "modules", value: [], stored: [], source: "database" });
  assert.deepEqual(changedValues([cleared], { custom_tool_extra_allowed_modules: null }), {
    custom_tool_extra_allowed_modules: null,
  });
  assert.deepEqual(changedValues([cleared], { custom_tool_extra_allowed_modules: [] }), {});
});

test("integer bounds are checked before saving", () => {
  const length = option({ key: "min_password_length", kind: "int", minimum: 1, maximum: 1024, value: 8 });
  assert.equal(intProblem(length, 12), null);
  assert.equal(intProblem(length, 0), "Between {minimum} and {maximum}");
  assert.equal(intProblem(length, 1.5), "Enter a whole number");
  assert.equal(intProblem(length, parseInteger("")), "Enter a whole number");
});

test("an integer with an off value accepts it below its minimum, and says so when refused", () => {
  const idle = option({
    key: "session_idle_timeout_minutes",
    kind: "int",
    minimum: 5,
    maximum: 525_600,
    off_value: 0,
    value: 0,
  });
  assert.equal(intProblem(idle, 0), null);
  assert.equal(intProblem(idle, 5), null);
  assert.equal(intProblem(idle, 3), "{off} (off), or between {minimum} and {maximum}");
  assert.equal(intProblem(idle, 525_601), "{off} (off), or between {minimum} and {maximum}");
  assert.equal(intProblem(idle, -1), "{off} (off), or between {minimum} and {maximum}");
});

test("number and list fields start from the value as it would be typed", () => {
  const length = option({ key: "min_password_length", kind: "int", value: 12 });
  assert.equal(fieldText(length, 12), "12");
  assert.equal(fieldText(option({ kind: "origins" }), ["http://a:1", "http://b:2"]), "http://a:1, http://b:2");
  assert.equal(fieldText(option(), true), "");
  assert.equal(parseInteger(" 14 "), 14);
});

test("lists are typed one entry per comma or line", () => {
  assert.deepEqual(parseList(" socket, http\nurllib ,, "), ["socket", "http", "urllib"]);
});

test("values read the way the card shows them", () => {
  assert.equal(displayValue(option(), true), "On");
  assert.equal(displayValue(option({ kind: "modules" }), []), "None");
  assert.equal(displayValue(option({ kind: "choice" }), "closed"), "Closed (refuse)");
});

test("an option the audit checks links to its findings, the others to nothing", () => {
  assert.equal(
    auditFindingPath("public_widget_strict_rendering"),
    "/settings/security?finding=web.public_widget_remote_images&finding=web.public_widget_strict",
  );
  assert.equal(
    auditFindingPath("webhook_require_secret"),
    "/settings/security?finding=assignments.webhook_without_secret&finding=assignments.webhook_without_secret_refused&finding=assignments.webhook_secrets_configured",
  );
  assert.equal(auditFindingPath("mcp_outbound_dcr_enabled"), null);
  assert.equal(auditFindingPath("no_such_option"), null);
});

test("a finding links back to the option its recommendation names, from the same table", () => {
  assert.equal(recommendedOptionKey("web.public_widget_remote_images"), "public_widget_strict_rendering");
  assert.equal(recommendedOptionKey("tools.custom_tool_sandbox_none"), "disable_custom_tools");
  assert.equal(recommendedOptionKey("network.credential_transport_strict_off"), "credential_transport_strict");
  // Decided by an option without recommending it: the audit link only.
  assert.equal(recommendedOptionKey("tools.custom_tools_disabled"), null);
  assert.equal(recommendedOptionKey("network.proxy_trust_missing"), null);
  assert.equal(recommendedOptionKey("infrastructure.debug_mode"), null);
  assert.equal(recommendedOptionKey(null), null);
  // Every finding that links back is one the option links to.
  for (const [key, findings] of Object.entries(SECURITY_OPTION_FINDINGS)) {
    for (const finding of findings ?? []) {
      if (finding.recommends) assert.equal(recommendedOptionKey(finding.id), key);
      assert.ok(auditFindingPath(key)?.includes(`finding=${finding.id}`), finding.id);
    }
  }
});

test("a finding links to its option only while the environment variable does not lock it", () => {
  const widget = "web.public_widget_remote_images";
  const listed = (locked: boolean) => [{ key: "public_widget_strict_rendering", locked }];
  assert.equal(linkedOptionKey(widget, listed(false)), "public_widget_strict_rendering");
  // Locked: the recommendation names the variable, the one thing to change.
  assert.equal(linkedOptionKey(widget, listed(true)), null);
  // The options are not read (yet, or at all): no link, the text still says where.
  assert.equal(linkedOptionKey(widget, undefined), null);
  assert.equal(linkedOptionKey(widget, []), null);
  assert.equal(linkedOptionKey("constructor", listed(false)), null);
});

test("an option's field has a stable address in the section", () => {
  assert.equal(optionAnchorId("webhook_require_secret"), "option-webhook_require_secret");
  assert.deepEqual(optionLocation("webhook_require_secret"), {
    pathname: "/settings/security/options",
    hash: "#option-webhook_require_secret",
  });
  assert.equal(targetedOptionKey("#option-webhook_require_secret"), "webhook_require_secret");
  assert.equal(targetedOptionKey("#option-"), null);
  assert.equal(targetedOptionKey("#findings"), null);
  assert.equal(targetedOptionKey(""), null);
});

test("the fail mode choices have keys of their own, not the verb Open", () => {
  // The catalogs translate "Open" as the verb (open a file): the fail mode
  // must never read "Ouvrir" / "Abrir" next to "Closed".
  for (const label of Object.values(SECURITY_OPTION_CHOICE_LABELS)) assert.notEqual(label, "Open");
});

test("sections are the audit's categories, in its order, an unknown one last", () => {
  const groups = groupOptions([
    option({ group: "dns_checks" }),
    option({ group: "tools" }),
    option({ group: "authentication" }),
    option({ group: "network" }),
  ]);
  assert.deepEqual(
    groups.map(([group]) => group),
    ["authentication", "network", "tools", "dns_checks"],
  );
});

test("a section is named like the audit category, an unknown one from its key", () => {
  assert.equal(groupLabel("mcp"), "MCP and A2A");
  assert.equal(groupLabel("access"), "Sharing and access");
  assert.equal(groupLabel("dns_checks"), "Dns checks");
  assert.equal(groupLabel("constructor"), "Constructor");
});

test("a control finds the option it is, locked or not", () => {
  const control = { setting: "TRUST_PROXY_HEADERS", stored_in: "config" };
  const locked = option({ locked: true, locked_reason: "environment", source: "environment" });
  assert.equal(controlOption(control, [locked]), locked);
  assert.equal(controlOptionKey(control, [locked]), null);
  assert.equal(controlOption({ setting: "local_auth_enabled", stored_in: "sso_config" }, [locked]), null);
  assert.equal(controlOption({ setting: "DEBUG", stored_in: "config" }, [locked]), null);
  assert.equal(controlOption(control, undefined), null);
});

test("a control's details say where its value comes from", () => {
  assert.equal(
    optionSourceMessage(option({ locked: true, locked_reason: "environment", source: "environment" }), t),
    "Set by environment variable TRUST_PROXY_HEADERS",
  );
  assert.equal(optionSourceMessage(pinnedQuotaFailMode(), t), SECURITY_OPTION_PINNED_REASONS.quota_fail_mode);
  assert.equal(optionSourceMessage(option({ source: "database", stored: true }), t), SAVED_IN_OPTIONS_MESSAGE);
  assert.equal(optionSourceMessage(option(), t), DEFAULT_VALUE_MESSAGE);
});

test("the info bubble gives a number option its unit and bounds, the off value included", () => {
  const idle = option({
    key: "session_idle_timeout_minutes",
    kind: "int",
    minimum: 5,
    maximum: 1440,
    off_value: 0,
    value: 0,
    default: 0,
  });
  assert.deepEqual(optionInfo(idle, t), ["Unit: minutes", "0 (off), or between 5 and 1440"]);
  const length = option({ key: "min_password_length", kind: "int", minimum: 8, maximum: 128, value: 12, default: 12 });
  assert.deepEqual(optionInfo(length, t), ["Unit: characters", "Between 8 and 128"]);
});

test("the info bubble gives a list option an example as typed and what empty does", () => {
  const origins = option({ key: "mcp_server_trusted_origins", kind: "origins", value: [], default: [] });
  assert.deepEqual(optionInfo(origins, t), [
    "Example: http://localhost:8080",
    "Empty: no origin is trusted beyond the policy.",
  ]);
  const tools = option({ key: "prompt_guard_scanned_tools", kind: "tools", value: [], default: [] });
  assert.deepEqual(optionInfo(tools, t), ["Empty: no tool is screened."]);
  assert.deepEqual(optionInfo(option(), t), []);
});

test("an origin needs a scheme and a host, as the backend parses it", () => {
  const origins = option({ key: "oauth_token_dev_trusted_origins", kind: "origins", value: [], default: [] });
  const draft = ["http://localhost:8080", "not a url", "https://a.example", "localhost"];
  assert.deepEqual(invalidListEntries(origins, draft), ["not a url", "localhost"]);
  assert.equal(
    t(listProblem(origins, draft) ?? "", { entries: "not a url, localhost", example: "http://localhost:8080" }),
    "Not an origin with a scheme and host: not a url, localhost. Example: http://localhost:8080",
  );
  assert.equal(listProblem(origins, ["http://127.0.0.1:3000"]), null);
  assert.equal(listProblem(origins, null), null);
});

test("a module entry is dotted Python names", () => {
  const modules = option({ key: "custom_tool_extra_allowed_modules", kind: "modules", value: [], default: [] });
  assert.deepEqual(invalidListEntries(modules, ["requests", "urllib.parse", "my-module", "1abc"]), ["my-module", "1abc"]);
  assert.equal(listProblem(modules, ["requests"]), null);
  const tools = option({ key: "prompt_guard_scanned_tools", kind: "tools", value: [], default: [] });
  assert.equal(listProblem(tools, ["any name"]), null);
});

test("no choice label is clipped by the select trigger in any locale", () => {
  // The trigger grows with its value (sm:w-auto) up to max-w-md (448 px):
  // 448 minus the padding and the chevron leaves about 390 px, 52 characters
  // at 14 px. The French "Permissive" value (42 characters) clipped at 288 px.
  const source = readFileSync(new URL("./SecurityOptionsSection.tsx", import.meta.url), "utf8");
  assert.match(source, /<SelectTrigger \{\.\.\.labelled\} className="h-9 w-full sm:w-auto sm:min-w-72 sm:max-w-md">/);
  for (const locale of ["en", "fr", "es", "ja"]) {
    const catalog = JSON.parse(readFileSync(new URL(`../../i18n/lang/${locale}.json`, import.meta.url), "utf8")) as Record<string, string>;
    for (const label of Object.values(SECURITY_OPTION_CHOICE_LABELS)) {
      const shown = catalog[label] ?? label;
      assert.ok(shown.length <= 52, `${locale} "${shown}" is ${shown.length} characters, over the 52 the trigger shows`);
    }
  }
});

test("every label is non-empty", () => {
  for (const label of Object.values(SECURITY_OPTION_LABELS)) assert.ok(label.length > 0);
});
