/**
 * Unit tests for the XTM One Desktop bridge reader -
 * `yarn test:unit`.
 *
 * The web app may be newer than the installed shell: everything beyond the
 * original bridge is feature-detected, and a shell that predates it keeps
 * today's UI.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";

import {
  DESKTOP_FEATURE_ATTENTION,
  DESKTOP_FEATURE_LOCAL_PATHS,
  DESKTOP_FEATURE_SESSION,
  adminObjectsLoading,
  adminObjectsTypeFilter,
  canActOnLocalPath,
  desktopSessionAtBoot,
  desktopSupports,
  loadDesktopSession,
  localPathFailure,
  readDesktopBridge,
  readDesktopSession,
  runnersEnabledFrom,
} from "./desktop.ts";

const RUNNER = "2f1c9d7a-0000-4000-8000-000000000000";

const facts = (overrides: Record<string, unknown> = {}) => ({
  version: "1.260926.1",
  platform: "windows",
  arch: "x64",
  runner_id: RUNNER,
  runner_name: "SAMUEL-LAPTOP",
  server_url: "https://one.filigran.io",
  features: ["local_paths", "attention"],
  open_extensions: ["txt", "pdf"],
  ...overrides,
});

test("the new facts are read, and default to nothing on an older shell", () => {
  const bridge = readDesktopBridge(facts());
  assert.ok(bridge);
  assert.deepEqual(bridge.features, ["local_paths", "attention"]);
  assert.deepEqual(bridge.open_extensions, ["txt", "pdf"]);
  const older = readDesktopBridge(facts({ features: undefined, open_extensions: undefined }));
  assert.ok(older);
  assert.deepEqual(older.features, []);
  assert.deepEqual(older.open_extensions, []);
  assert.equal(desktopSupports(DESKTOP_FEATURE_LOCAL_PATHS, older), false);
});

test("junk in the lists is dropped", () => {
  const bridge = readDesktopBridge(facts({ features: ["local_paths", 3, "<script>", ""], open_extensions: "pdf" }));
  assert.ok(bridge);
  assert.deepEqual(bridge.features, ["local_paths"]);
  assert.deepEqual(bridge.open_extensions, []);
});

test("features are detected per shell, never on the web", () => {
  const bridge = readDesktopBridge(facts({ features: ["attention"] }));
  assert.equal(desktopSupports(DESKTOP_FEATURE_ATTENTION, bridge), true);
  assert.equal(desktopSupports(DESKTOP_FEATURE_LOCAL_PATHS, bridge), false);
  assert.equal(desktopSupports(DESKTOP_FEATURE_ATTENTION, null), false);
});

test("a card's paths are actionable only on the machine the app runs on", () => {
  const bridge = readDesktopBridge(facts());
  assert.equal(canActOnLocalPath(RUNNER, bridge), true);
  assert.equal(canActOnLocalPath("another-runner", bridge), false);
  assert.equal(canActOnLocalPath(null, bridge), false);
  // A path quoted in a reply names no machine: the app's own.
  assert.equal(canActOnLocalPath(undefined, bridge), true);
  assert.equal(canActOnLocalPath(RUNNER, null), false);
  assert.equal(canActOnLocalPath(RUNNER, readDesktopBridge(facts({ features: [] }))), false);
});

test("the shell's refusal tokens are recognised, anything else is a failure", () => {
  assert.equal(localPathFailure("not_found"), "not_found");
  assert.equal(localPathFailure("not_openable"), "not_openable");
  assert.equal(localPathFailure(new Error("remote_path")), "remote_path");
  assert.equal(localPathFailure("command desktop_open_local_path not allowed by ACL"), "failed");
  assert.equal(localPathFailure(undefined), "failed");
});

test("runners are not known while /auth/config loads, so no Desktop entry flashes in", () => {
  assert.equal(runnersEnabledFrom(undefined, true), undefined);
});

test("runners follow RUNNERS_ENABLED once /auth/config answered", () => {
  assert.equal(runnersEnabledFrom({ runners_enabled: true }, false), true);
  assert.equal(runnersEnabledFrom({ runners_enabled: false }, false), false);
});

test("a config without the flag, or that failed to load, keeps the server default: on", () => {
  assert.equal(runnersEnabledFrom({}, false), true);
  assert.equal(runnersEnabledFrom(undefined, false), true);
});

test("admin Objects keeps offering Machines while the flag loads, and drops the filter once runners are off", () => {
  assert.deepEqual(adminObjectsTypeFilter(["runner"], undefined), { offerRunners: true, objectTypes: ["runner"], waitForRunners: true });
  assert.deepEqual(adminObjectsTypeFilter(["runner"], true), { offerRunners: true, objectTypes: ["runner"], waitForRunners: false });
  assert.deepEqual(adminObjectsTypeFilter(["runner"], false), { offerRunners: false, objectTypes: [], waitForRunners: false });
  // Several types: machines dropped once runners are off, the others kept.
  assert.deepEqual(adminObjectsTypeFilter(["agent", "runner"], false), { offerRunners: false, objectTypes: ["agent"], waitForRunners: false });
  assert.deepEqual(adminObjectsTypeFilter(["agent", "runner"], undefined), { offerRunners: true, objectTypes: ["agent", "runner"], waitForRunners: true });
  // Every other type is fetched at once, whatever the flag says.
  assert.deepEqual(adminObjectsTypeFilter(["agent"], undefined), { offerRunners: true, objectTypes: ["agent"], waitForRunners: false });
  assert.deepEqual(adminObjectsTypeFilter([], false), { offerRunners: false, objectTypes: [], waitForRunners: false });
});

test("a runner-only list waiting for the flag shows the skeleton, not the rows of the previous filter", () => {
  const rows = { items: [{ object_type: "agent" }], total: 1 };
  // `useAdminObjects` keeps the previous request's rows as placeholder data.
  assert.equal(adminObjectsLoading(adminObjectsTypeFilter(["runner"], undefined), { isLoading: false, data: rows }), true);
  assert.equal(adminObjectsLoading(adminObjectsTypeFilter(["runner"], undefined), { isLoading: false, data: undefined }), true);
  // Anything else: the skeleton only while nothing is there to show.
  assert.equal(adminObjectsLoading(adminObjectsTypeFilter(["runner"], true), { isLoading: true, data: undefined }), true);
  assert.equal(adminObjectsLoading(adminObjectsTypeFilter(["runner"], true), { isLoading: true, data: rows }), false);
  assert.equal(adminObjectsLoading(adminObjectsTypeFilter(["agent"], undefined), { isLoading: false, data: rows }), false);
});

const SIGNED_IN = { token: "h.new.sig", admin_token: null };
const ROLLED_BACK = { token: "h.old.sig", admin_token: null };
const SIGNED_OUT = { token: null, admin_token: null };
const IMPERSONATING = { token: "h.imp.sig", admin_token: "h.admin.sig" };

test("the session the shell kept wins over a webview storage that rolled back", () => {
  // The case that signed people out at every update: the storage brought back
  // an older token, the shell holds the one the page last set.
  assert.deepEqual(desktopSessionAtBoot(SIGNED_IN, ROLLED_BACK), { kind: "adopt", session: SIGNED_IN });
  // A sign-out the storage lost is still a sign-out.
  assert.deepEqual(desktopSessionAtBoot(SIGNED_OUT, ROLLED_BACK), { kind: "adopt", session: SIGNED_OUT });
  assert.deepEqual(desktopSessionAtBoot(IMPERSONATING, SIGNED_IN), { kind: "adopt", session: IMPERSONATING });
  assert.deepEqual(desktopSessionAtBoot(SIGNED_IN, IMPERSONATING), { kind: "adopt", session: SIGNED_IN });
  // Already the same: nothing to write.
  assert.deepEqual(desktopSessionAtBoot(SIGNED_IN, { ...SIGNED_IN }), { kind: "keep" });
  assert.deepEqual(desktopSessionAtBoot(SIGNED_OUT, SIGNED_OUT), { kind: "keep" });
});

test("a shell that keeps no session yet takes the page's, and an unreachable one changes nothing", () => {
  assert.deepEqual(desktopSessionAtBoot(null, SIGNED_IN), { kind: "hand_over", session: SIGNED_IN });
  assert.deepEqual(desktopSessionAtBoot(null, IMPERSONATING), { kind: "hand_over", session: IMPERSONATING });
  assert.deepEqual(desktopSessionAtBoot(null, SIGNED_OUT), { kind: "keep" });
  assert.deepEqual(desktopSessionAtBoot(undefined, ROLLED_BACK), { kind: "keep" });
});

test("the shell's answer is read defensively", () => {
  assert.equal(readDesktopSession(null), null);
  assert.deepEqual(readDesktopSession({ token: "h.p.s", admin_token: null }), { token: "h.p.s", admin_token: null });
  assert.deepEqual(readDesktopSession({ token: "h.p.s" }), { token: "h.p.s", admin_token: null });
  assert.deepEqual(readDesktopSession({ token: null, admin_token: null }), SIGNED_OUT);
  assert.deepEqual(readDesktopSession(IMPERSONATING), IMPERSONATING);
  for (const unreadable of [undefined, "h.p.s", 42, { token: 42 }, { token: "" }, { token: "h.p.s", admin_token: 1 }, { admin_token: "h.p.s" }]) {
    assert.equal(readDesktopSession(unreadable), undefined, JSON.stringify(unreadable));
  }
});

test("outside the app the kept session is unreadable, never an error", async () => {
  assert.equal(await loadDesktopSession(50), undefined);
  assert.equal(desktopSupports(DESKTOP_FEATURE_SESSION, readDesktopBridge(facts({ features: ["session"] }))), true);
  assert.equal(desktopSupports(DESKTOP_FEATURE_SESSION, readDesktopBridge(facts())), false);
});

// Every write of the session goes through `lib/api.ts`, which hands it to the
// shell: a write anywhere else would leave the shell's copy behind, and the
// next launch would bring back the older session.
test("only lib/api.ts names the session's storage keys", () => {
  const SRC = new URL("../", import.meta.url);
  const writers = readdirSync(SRC, { recursive: true })
    .map((path) => String(path).replace(/\\/g, "/"))
    .filter((path) => /\.tsx?$/.test(path) && !/\.test\.tsx?$/.test(path))
    .filter((path) => /["'`]copilot_(admin_)?token["'`]/.test(readFileSync(new URL(path, SRC), "utf8")))
    .sort();
  assert.deepEqual(writers, ["lib/api.ts"]);
});

// A surface reading the flag itself decides alone what a loading config
// means, and `runners_enabled ?? true` reads it as on.
test("only runnersEnabledFrom reads runners_enabled", () => {
  const SRC = new URL("../", import.meta.url);
  const readers = readdirSync(SRC, { recursive: true })
    .map((path) => String(path).replace(/\\/g, "/"))
    .filter((path) => /\.tsx?$/.test(path) && !/\.test\.tsx?$/.test(path))
    .filter((path) => /\.runners_enabled\b/.test(readFileSync(new URL(path, SRC), "utf8")))
    .sort();
  assert.deepEqual(readers, ["lib/desktop.ts"]);
});
