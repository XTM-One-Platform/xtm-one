/**
 * Who is shown a provider-contributed tab - `yarn test:unit`.
 *
 * Nova's "Rules" tab was listed for everyone who could see the integration,
 * while its sync-source and model routes answer a non-admin "Admins only.":
 * the tab opened on "Failed to load this section".
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import type { Integration } from "@/types";
import {
  adminsOnly,
  integrationManagersOnly,
  isRestrictedEntry,
  visibleEntries,
  type ProviderTabViewer,
  type ViewerGatedEntry,
} from "./provider-tab-visibility.ts";

const integration = (overrides: Partial<Integration> = {}): Integration =>
  ({
    id: "i-1",
    name: "Nova Prompt Safety",
    provider: "nova",
    status: "connected",
    user_id: "owner",
    is_company_managed: false,
    shared_group_ids: [],
    shared_user_ids: [],
    ...overrides,
  }) as Integration;

const admin: ProviderTabViewer = { userId: "admin", isAdmin: true };
const owner: ProviderTabViewer = { userId: "owner", isAdmin: false };
const guest: ProviderTabViewer = { userId: "guest", isAdmin: false };

test("an admins-only tab is shown to an admin", () => {
  assert.equal(adminsOnly(integration(), admin), true);
  assert.equal(adminsOnly(integration({ is_company_managed: true, user_id: null }), admin), true);
});

test("an admins-only tab is hidden from a non-admin, even one who manages the integration", () => {
  assert.equal(adminsOnly(integration(), owner), false);
  assert.equal(adminsOnly(integration({ can_manage: true }), guest), false);
  assert.equal(adminsOnly(integration({ is_company_managed: true, user_id: null }), guest), false);
});

test("a managers-only tab follows the integration's manage right", () => {
  assert.equal(integrationManagersOnly(integration(), owner), true);
  assert.equal(integrationManagersOnly(integration(), admin), true);
  assert.equal(integrationManagersOnly(integration(), guest), false);
  assert.equal(integrationManagersOnly(integration({ can_manage: true }), guest), true);
  assert.equal(integrationManagersOnly(integration({ can_manage: false }), owner), false);
  assert.equal(integrationManagersOnly(integration({ is_company_managed: true, user_id: null }), guest), false);
});

const ENTRIES: ViewerGatedEntry[] = [{ key: "rules", visible: adminsOnly }, { key: "help" }];

test("an entry without a rule is shown to everyone, one with a rule to the viewers it names", () => {
  assert.deepEqual(visibleEntries(ENTRIES, integration(), admin).map((entry) => entry.key), ["rules", "help"]);
  assert.deepEqual(visibleEntries(ENTRIES, integration(), owner).map((entry) => entry.key), ["help"]);
});

test("a tab opened by URL is restricted only when it exists and its rule hides it", () => {
  assert.equal(isRestrictedEntry(ENTRIES, "rules", integration(), owner), true);
  assert.equal(isRestrictedEntry(ENTRIES, "rules", integration(), admin), false);
  assert.equal(isRestrictedEntry(ENTRIES, "help", integration(), owner), false);
  // A built-in tab, or a key no provider registers, is not the registry's to refuse.
  assert.equal(isRestrictedEntry(ENTRIES, "overview", integration(), owner), false);
  assert.equal(isRestrictedEntry(ENTRIES, "nope", integration(), owner), false);
});

test("every registry entry is bound to a named rule of this module, Nova's Rules tab to adminsOnly", () => {
  const registry = readFileSync(new URL("./provider-tabs.tsx", import.meta.url), "utf8");
  const entry = (key: string) => {
    const match = registry.match(new RegExp(`key: "${key}",[\\s\\S]*?render:`));
    assert.ok(match, `provider-tabs.tsx registers "${key}"`);
    return match[0];
  };
  assert.match(entry("rules"), /visible: adminsOnly,/);
  for (const key of ["usage", "models", "keys", "platform-provider"]) {
    assert.match(entry(key), /visible: integrationManagersOnly,/);
  }
  // A rule spelled inline would drift from the guard of the routes it mirrors.
  assert.doesNotMatch(registry, /visible: \(/);
  assert.doesNotMatch(registry, /canManageEntity/);
});
