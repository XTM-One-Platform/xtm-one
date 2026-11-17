/**
 * Unit tests for the home dashboard's XTM Hub pill - `yarn test:unit`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  isXtmHubPlatformToken,
  readXtmHubRegistrationMessage,
  xtmHubOrigin,
  xtmHubPill,
  xtmHubRegistrationPageUrl,
  xtmHubRegistrationUrl,
  xtmHubUrl,
} from "./xtm-hub.ts";

// The backend's XTM_HUB_DEFAULT_URL, as the registration status serves it.
const DEFAULT_HUB = "https://hub.default.example.com";
const UNREGISTERED = { status: "unregistered", hub_url: null, default_hub_url: DEFAULT_HUB } as const;

const hub = (status: string, extra_config: Record<string, unknown> | null = null) => ({
  provider: "xtm_hub",
  status,
  extra_config,
});

test("no XTM Hub integration means no pill to show as connected", () => {
  assert.deepEqual(xtmHubPill(undefined), { present: false });
  assert.deepEqual(xtmHubPill([]), { present: false });
  assert.deepEqual(
    xtmHubPill([{ provider: "opencti", status: "connected", extra_config: null }]),
    { present: false },
  );
  // A custom bundle provider is not the native XTM Hub.
  assert.deepEqual(xtmHubPill([{ provider: "custom:xtm-hub", status: "connected", extra_config: null }]), {
    present: false,
  });
});

test("a connected XTM Hub is healthy and links to its hub", () => {
  assert.deepEqual(xtmHubPill([hub("connected", { base_url: "https://hub.example.com/" })]), {
    present: true,
    healthy: true,
    url: "https://hub.example.com",
  });
});

test("an XTM Hub that is not connected is shown, never as healthy", () => {
  for (const status of ["error", "not_connected", "configured", "reauthorize"]) {
    assert.deepEqual(xtmHubPill([hub(status)], UNREGISTERED), { present: true, healthy: false, url: DEFAULT_HUB }, status);
  }
});

test("an XTM Hub integration naming no hub has no link until the default hub is known", () => {
  assert.deepEqual(xtmHubPill([hub("connected")]), { present: true, healthy: true, url: undefined });
});

test("a connected XTM Hub wins over one listed before it that is not", () => {
  const pill = xtmHubPill([
    hub("error", { base_url: "https://broken.example.com" }),
    hub("connected", { base_url: "https://hub.example.com" }),
  ]);
  assert.deepEqual(pill, { present: true, healthy: true, url: "https://hub.example.com" });
});

test("the link falls back to the default hub for anything but an absolute http(s) URL", () => {
  for (const extraConfig of [
    null,
    {},
    { base_url: "" },
    { base_url: "   " },
    { base_url: 42 },
    { base_url: "hub.example.com" },
    { base_url: "javascript:alert(1)" },
    { base_url: "ftp://hub.example.com" },
    { base_url: "https://user:pw@hub.example.com" },
  ]) {
    assert.equal(xtmHubUrl(extraConfig, DEFAULT_HUB), DEFAULT_HUB, JSON.stringify(extraConfig));
  }
});

test("the link keeps a path-prefixed hub's prefix", () => {
  assert.equal(
    xtmHubUrl({ base_url: " https://intranet.example.com/xtm-hub/ " }, DEFAULT_HUB),
    "https://intranet.example.com/xtm-hub",
  );
  assert.equal(xtmHubUrl({ base_url: "http://localhost:4002" }, DEFAULT_HUB), "http://localhost:4002");
});

test("the platform registration decides the pill before any integration", () => {
  const integrations = [hub("connected", { base_url: "https://integration.example.com" })];
  assert.deepEqual(xtmHubPill(integrations, { ...UNREGISTERED, status: "registered", hub_url: "https://hub.example.com" }), {
    present: true,
    healthy: true,
    url: "https://hub.example.com",
  });
  for (const status of ["lost_connectivity", "needs_reregistration"] as const) {
    assert.deepEqual(xtmHubPill(integrations, { ...UNREGISTERED, status, hub_url: "https://hub.example.com" }), {
      present: true,
      healthy: false,
      url: "https://hub.example.com",
    }, status);
  }
  // Unregistered, the integrations decide as before.
  assert.deepEqual(xtmHubPill(integrations, UNREGISTERED), {
    present: true,
    healthy: true,
    url: "https://integration.example.com",
  });
  assert.deepEqual(xtmHubPill([], UNREGISTERED), { present: false });
  assert.deepEqual(xtmHubPill([], null), { present: false });
});

const PARAMS = {
  hubUrl: "https://hub.example.com/",
  platformId: "6a3c2a8e-1f2b-4c3d-9e8f-0a1b2c3d4e5f",
  platformUrl: "https://xtm.example.com/xtm-one",
  platformTitle: "Acme & Co <XTM>",
  platformContract: "EE" as const,
  platformVersion: "1.260930.0",
  state: "s-123",
};

test("the register page is the Hub redirect route with every parameter encoded", () => {
  const url = new URL(xtmHubRegistrationPageUrl("register", PARAMS));
  assert.equal(url.origin + url.pathname, "https://hub.example.com/redirect/register-xtmone");
  assert.deepEqual(Object.fromEntries(url.searchParams), {
    platform_id: PARAMS.platformId,
    platform_url: PARAMS.platformUrl,
    platform_title: PARAMS.platformTitle,
    platform_contract: "EE",
    platform_version: "1.260930.0",
    state: "s-123",
  });
});

test("the unregister page only names the platform", () => {
  const url = new URL(xtmHubRegistrationPageUrl("unregister", PARAMS));
  assert.equal(url.pathname, "/redirect/unregister-xtmone");
  assert.deepEqual(Object.fromEntries(url.searchParams), { platform_id: PARAMS.platformId, state: "s-123" });
});

test("a Hub URL that is no http(s) URL never becomes a page to open", () => {
  assert.throws(() => xtmHubRegistrationPageUrl("register", { ...PARAMS, hubUrl: "javascript:alert(1)" }));
  assert.throws(() => xtmHubOrigin("javascript:alert(1)"));
  assert.equal(xtmHubOrigin("https://hub.example.com/sub/"), "https://hub.example.com");
});

// Generated, so no literal looks like a credential to a secret scanner.
const TOKEN = crypto.randomUUID();
const POPUP = { name: "the popup" };
const EXPECTED = { hubOrigin: "https://hub.example.com", popup: POPUP, state: "s-123" };
const message = (data: unknown, overrides: Partial<{ origin: string; source: unknown }> = {}) => ({
  data,
  origin: "https://hub.example.com",
  source: POPUP,
  ...overrides,
});

test("a register message from the popup and the Hub origin gives its token", () => {
  assert.deepEqual(readXtmHubRegistrationMessage(message({ action: "register", token: TOKEN, state: "s-123" }), EXPECTED), {
    action: "register",
    token: TOKEN,
  });
  assert.deepEqual(readXtmHubRegistrationMessage(message({ action: "unregister", state: "s-123" }), EXPECTED), {
    action: "unregister",
  });
  assert.deepEqual(readXtmHubRegistrationMessage(message({ action: "cancel", state: "s-123" }), EXPECTED), {
    action: "cancel",
  });
});

test("anything else is ignored", () => {
  const cases: [string, ReturnType<typeof message>][] = [
    ["another origin", message({ action: "register", token: TOKEN }, { origin: "https://evil.example.com" })],
    ["a lookalike origin", message({ action: "register", token: TOKEN }, { origin: "https://hub.example.com.evil.com" })],
    ["another window", message({ action: "register", token: TOKEN }, { source: { name: "another" } })],
    ["no window", message({ action: "register", token: TOKEN }, { source: null })],
    ["another state", message({ action: "register", token: TOKEN, state: "s-999" })],
    // A stale document in the reused popup, or a Hub that echoes no state:
    // the paste fallback covers the latter.
    ["no state", message({ action: "register", token: TOKEN })],
    ["no state on unregister", message({ action: "unregister" })],
    ["a state that is no string", message({ action: "register", token: TOKEN, state: 123 })],
    ["a token that is no UUID", message({ action: "register", token: "<img src=x>" })],
    ["no token", message({ action: "register" })],
    ["an unknown action", message({ action: "set-token", token: TOKEN })],
    ["a string", message("register")],
    ["null", message(null)],
  ];
  for (const [reason, event] of cases) {
    assert.equal(readXtmHubRegistrationMessage(event, EXPECTED), null, reason);
  }
  assert.equal(
    readXtmHubRegistrationMessage(message({ action: "register", token: TOKEN }), { ...EXPECTED, popup: null }),
    null,
    "no popup was opened",
  );
});

test("a pasted platform token must be a UUID", () => {
  assert.equal(isXtmHubPlatformToken(` ${TOKEN} `), true);
  assert.equal(isXtmHubPlatformToken("xhp_abc"), false);
  assert.equal(isXtmHubPlatformToken(""), false);
});

test("a registration goes only to an https Hub, or an http one on this machine, never to a fallback", () => {
  const cases: [string, string | null][] = [
    ["", null],
    ["   ", null],
    [" https://hub.example.com/ ", "https://hub.example.com"],
    ["https://intranet.example.com/xtm-hub/", "https://intranet.example.com/xtm-hub"],
    ["http://localhost:3002", "http://localhost:3002"],
    ["http://127.0.0.1:3002/", "http://127.0.0.1:3002"],
    ["http://[::1]:3002", "http://[::1]:3002"],
    ["hub.example.com", null],
    ["http://hub.example.com", null],
    ["http://10.0.0.5:3002", null],
    ["ftp://hub.example.com", null],
    ["javascript:alert(1)", null],
    ["https://user:pw@hub.example.com", null],
    ["https://hub.example.com/?next=x", null],
    ["https://hub.example.com/#frag", null],
  ];
  for (const [raw, expected] of cases) {
    assert.equal(xtmHubRegistrationUrl(raw), expected, JSON.stringify(raw));
  }
});
