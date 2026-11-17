/**
 * Unit tests for which OAuth app an integration runs on - `yarn test:unit`.
 *
 * Pinned: what the integration recorded decides, whatever the provider-wide
 * flag says; an integration that recorded nothing keeps the provider-wide
 * answer it had before the field existed; an edit about to replace the client
 * ID or leave OAuth reads as the user's own app, as the save records it; a
 * new integration is on the admin's app exactly when the backend copies the
 * admin's OAuth defaults into it.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { createsOnAdminOAuthApp, usesAdminOAuthApp } from "./admin-oauth-app.ts";

const oauth = (recorded?: boolean | null) => ({ auth_type: "oauth", uses_admin_oauth_credentials: recorded });

test("what the integration recorded wins over the provider-wide flag", () => {
  assert.equal(usesAdminOAuthApp(oauth(false), { has_oauth_defaults: true }), false);
  assert.equal(usesAdminOAuthApp(oauth(true), { has_oauth_defaults: false }), true);
  assert.equal(usesAdminOAuthApp(oauth(true), { has_oauth_defaults: true }), true);
  assert.equal(usesAdminOAuthApp(oauth(false), undefined), false);
});

test("an integration that recorded nothing keeps the provider-wide answer", () => {
  assert.equal(usesAdminOAuthApp(oauth(null), { has_oauth_defaults: true }), true);
  assert.equal(usesAdminOAuthApp(oauth(null), { has_oauth_defaults: false }), false);
  assert.equal(usesAdminOAuthApp(oauth(), { has_oauth_defaults: true }), true);
  assert.equal(usesAdminOAuthApp(oauth(), undefined), false);
});

test("an edit that replaces the client ID or leaves OAuth is on the user's own app", () => {
  const defaults = { has_oauth_defaults: true };
  assert.equal(usesAdminOAuthApp(oauth(true), defaults, { clientId: "my-own-app" }), false);
  assert.equal(usesAdminOAuthApp(oauth(null), defaults, { clientId: "my-own-app" }), false);
  assert.equal(usesAdminOAuthApp(oauth(true), defaults, { authType: "api_key" }), false);
  // Nothing typed, or still OAuth: what is stored decides.
  assert.equal(usesAdminOAuthApp(oauth(true), defaults, { clientId: "  ", authType: "oauth" }), true);
  assert.equal(usesAdminOAuthApp(oauth(null), defaults, { clientId: "" }), true);
  // Not an OAuth integration to begin with: no transition to read.
  assert.equal(usesAdminOAuthApp({ auth_type: "api_key", uses_admin_oauth_credentials: null }, defaults, { authType: "api_key" }), true);
});

test("a new integration is on the admin's app when created on OAuth without a client ID", () => {
  const defaults = { has_oauth_defaults: true };
  assert.equal(createsOnAdminOAuthApp(defaults, "oauth"), true);
  assert.equal(createsOnAdminOAuthApp(defaults, "oauth", " "), true);
  assert.equal(createsOnAdminOAuthApp(defaults, "oauth", "my-own-app"), false);
  assert.equal(createsOnAdminOAuthApp(defaults, "api_key"), false);
  assert.equal(createsOnAdminOAuthApp(defaults, undefined), false);
  assert.equal(createsOnAdminOAuthApp({ has_oauth_defaults: false }, "oauth"), false);
});
