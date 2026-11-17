/**
 * Unit tests for the secret vault forms and references - `yarn test:unit`.
 *
 * The provider dialog sends only the settings of its type and sign-in method,
 * never re-sends a stored credential, clears the ones of a method left behind,
 * and reads its grants back as the access mode they mean; a reference the
 * picker writes parses as the backend parses it.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import type { SecretVaultProvider } from "../types/index.ts";
import {
  accessModeOf,
  emptyVaultForm,
  formatVaultReference,
  parentPrefix,
  parseVaultReference,
  pathWithin,
  recentVaultPaths,
  rememberVaultPath,
  vaultFormFromProvider,
  requiredVaultFields,
  vaultFormProblems,
  vaultInputFromForm,
  visibleFields,
} from "./secret-vaults.ts";

const stored: SecretVaultProvider = {
  id: "11111111-1111-1111-1111-111111111111",
  name: "corp",
  display_name: "Corporate Vault",
  type: "hashicorp",
  source: "database",
  enabled: true,
  status: "connected",
  status_reason: null,
  status_description: null,
  status_detail: null,
  last_check_at: null,
  settings: { address: "https://vault.example.com", auth_method: "approle", role_id: "role", kv_version: "2" },
  credentials_set: { secret_id: true, token: false },
  grants: [{ principal: "group", id: "g1", paths: ["secret/soc"], label: "SOC" }],
  browsable: true,
  cache_seconds: null,
  writers: null,
  paths: [],
  usage_count: 2,
  created_at: null,
  updated_at: null,
  updated_by_name: null,
};

test("a form shows the fields of its type and sign-in method only", () => {
  const keys = (method: string) => visibleFields("hashicorp", method).map((field) => field.key);
  assert.ok(keys("token").includes("token"));
  assert.ok(!keys("token").includes("secret_id"));
  assert.ok(keys("approle").includes("role_id") && keys("approle").includes("secret_id"));
  assert.deepEqual(
    visibleFields("aws", "default_chain").map((field) => field.key),
    ["region", "endpoint_url"],
  );
});

test("an edit keeps a stored credential it does not retype and clears those of a method left behind", () => {
  const form = vaultFormFromProvider(stored);
  assert.equal(form.method, "approle");
  assert.equal(form.access, "grants");
  assert.deepEqual(vaultFormProblems(form, stored), []);

  const kept = vaultInputFromForm(form);
  assert.equal(kept.credentials.secret_id, undefined);
  assert.equal(kept.credentials.token, null);
  assert.equal(kept.settings.auth_method, "approle");
  assert.deepEqual(kept.grants, [{ principal: "group", id: "g1", paths: ["secret/soc"] }]);

  const switched = vaultInputFromForm({ ...form, method: "token", credentials: { token: "hvs.new" } });
  assert.equal(switched.credentials.token, "hvs.new");
  assert.equal(switched.credentials.secret_id, null);
  assert.equal(switched.settings.role_id, undefined);

  const cleared = vaultInputFromForm({ ...form, cleared: ["secret_id"] });
  assert.equal(cleared.credentials.secret_id, null);
});

test("a required credential counts as given when it is stored and not cleared", () => {
  const form = vaultFormFromProvider(stored);
  assert.deepEqual(vaultFormProblems({ ...form, cleared: ["secret_id"] }, stored), [{ kind: "required", field: "secret_id" }]);
  const fresh = { ...emptyVaultForm("azure"), name: "az", method: "client_secret", settings: { vault_url: "https://v.vault.azure.net" } };
  assert.deepEqual(
    vaultFormProblems(fresh).map((problem) => (problem.kind === "required" ? problem.field : problem.kind)),
    ["tenant_id", "client_id", "client_secret"],
  );
  assert.deepEqual(vaultFormProblems({ ...emptyVaultForm(), name: "Bad Name" }).slice(0, 1), [{ kind: "name" }]);
});

test("the fields marked required are the ones the Missing footer names", () => {
  assert.deepEqual([...requiredVaultFields(emptyVaultForm("hashicorp"))].sort(), ["address", "token"]);
  const kv1 = { ...emptyVaultForm("hashicorp"), settings: { kv_version: "1" } };
  assert.ok(requiredVaultFields(kv1).has("kv_mount"));
  assert.deepEqual(
    vaultFormProblems({ ...kv1, name: "hv" }).map((problem) => (problem.kind === "required" ? problem.field : problem.kind)),
    ["address", "token", "kv_mount"],
  );
  assert.deepEqual([...requiredVaultFields({ ...emptyVaultForm("azure"), method: "client_secret" })].sort(), ["client_id", "client_secret", "tenant_id", "vault_url"]);
});

test("a 1Password provider asks for its Connect server and token, the token stored as a credential", () => {
  const form = { ...emptyVaultForm("onepassword"), name: "op", settings: { address: "https://connect.example.com" } };
  assert.equal(form.method, "connect_token");
  assert.deepEqual(vaultFormProblems(form), [{ kind: "required", field: "token" }]);
  const input = vaultInputFromForm({ ...form, credentials: { token: "ops-token" } });
  assert.deepEqual(input.settings, { auth_method: "connect_token", address: "https://connect.example.com" });
  assert.deepEqual(input.credentials, { token: "ops-token" });
});

test("the access mode is read from the grants and written back as them", () => {
  assert.equal(accessModeOf([]), "admins");
  assert.equal(accessModeOf([{ principal: "everyone", id: null, paths: [] }]), "everyone");
  const everyone = vaultInputFromForm({ ...emptyVaultForm(), name: "corp", access: "everyone", everyonePaths: ["shared/", "/ci ", "shared"] });
  assert.deepEqual(everyone.grants, [{ principal: "everyone", id: null, paths: ["shared", "ci"] }]);
  assert.deepEqual(vaultInputFromForm({ ...emptyVaultForm(), name: "corp", access: "admins" }).grants, []);
  const granted = vaultInputFromForm({
    ...emptyVaultForm(),
    name: "corp",
    access: "grants",
    grants: [{ principal: "group", id: "g1", paths: ["a/b/", " c", "", "a/b"] }],
  });
  assert.deepEqual(granted.grants, [{ principal: "group", id: "g1", paths: ["a/b", "c"] }]);
  assert.deepEqual(vaultFormFromProvider({ ...stored, grants: [{ principal: "everyone", id: null, paths: ["x"] }] }).everyonePaths, ["x"]);
});

test("a reference parses and formats as the backend reads it", () => {
  assert.deepEqual(parseVaultReference(" vault://corp/secret/github#token "), { provider: "corp", path: "secret/github", key: "token" });
  assert.deepEqual(parseVaultReference("vault://aws/prod/db"), { provider: "aws", path: "prod/db", key: null });
  for (const bad of ["vault://Corp/x", "vault://corp", "vault://corp/a/../b", "vault://corp/a b", "secret"]) {
    assert.equal(parseVaultReference(bad), null, bad);
  }
  assert.equal(formatVaultReference({ provider: "corp", path: "/secret/github/", key: "token" }), "vault://corp/secret/github#token");
});

test("a grant's prefixes match whole segments, as the server checks them", () => {
  assert.ok(pathWithin("secret/soc/github", ["secret/soc"]));
  assert.ok(!pathWithin("secret/soc-staging/x", ["secret/soc"]));
  assert.ok(pathWithin("anything", null));
  assert.equal(parentPrefix("secret/soc/"), "secret/");
  assert.equal(parentPrefix("secret/"), "");
});

test("recent paths are kept per provider, most recent first, paths only", () => {
  const memory = new Map<string, string>();
  const storage = { getItem: (key: string) => memory.get(key) ?? null, setItem: (key: string, value: string) => void memory.set(key, value) };
  rememberVaultPath("corp", "secret/a", storage);
  rememberVaultPath("corp", "secret/b", storage);
  rememberVaultPath("corp", "secret/a", storage);
  assert.deepEqual(recentVaultPaths("corp", storage), ["secret/a", "secret/b"]);
  assert.deepEqual(recentVaultPaths("other", storage), []);
  assert.deepEqual(recentVaultPaths("corp", { getItem: () => "{broken" }), []);
});
