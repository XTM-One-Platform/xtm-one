/**
 * Unit tests for the status an integration is shown with (#8386) - `yarn test:unit`.
 */

// vitest, as every test of ./constants: the module imports icon components,
// which Node's own runner cannot resolve.
import { test } from "vitest";
import assert from "node:assert/strict";

import { INTEGRATION_STATUS, authorizedScopes, displayedIntegrationStatus, providerInstallsStatus, scopesNotGranted } from "./constants";

const oauth = (overrides: Partial<{ status: string; has_access_token: boolean; scopes: string | null; required_scopes: string | null; missing_scopes: string[] | null }> = {}) => ({
  auth_type: "oauth",
  status: "connected",
  scopes: null,
  required_scopes: null,
  has_access_token: true,
  ...overrides,
});

test("an OAuth integration with no stored token never reads connected", () => {
  assert.equal(displayedIntegrationStatus(oauth({ has_access_token: false })), "disconnected");
  assert.equal(INTEGRATION_STATUS.disconnected.label, "Not connected");
});

test("a provider card with a connected install and an OAuth install without a token reads Not connected", () => {
  const status = providerInstallsStatus([oauth(), oauth({ has_access_token: false })]);
  assert.equal(status, "disconnected");
  assert.equal(INTEGRATION_STATUS[status].label, "Not connected");
});

test("a provider card shows the worst status of its installs", () => {
  assert.equal(providerInstallsStatus([oauth(), oauth({ status: "error" })]), "error");
  assert.equal(providerInstallsStatus([oauth(), oauth({ scopes: "a", required_scopes: "a b" })]), "reauthorize");
  assert.equal(providerInstallsStatus([oauth(), oauth({ status: "configured", has_access_token: false })]), "configured");
  assert.equal(providerInstallsStatus([oauth()]), "connected");
  assert.equal(providerInstallsStatus([]), "disconnected");
});

test("an OAuth integration with a token keeps its stored status", () => {
  assert.equal(displayedIntegrationStatus(oauth()), "connected");
  assert.equal(displayedIntegrationStatus(oauth({ status: "configured", has_access_token: false })), "configured");
});

test("scope drift still reads re-authorization required", () => {
  assert.equal(displayedIntegrationStatus(oauth({ scopes: "a", required_scopes: "a b" })), "reauthorize");
});

test("a token missing a requested scope reads re-authorization required (#9776)", () => {
  const declined = oauth({ missing_scopes: ["https://www.googleapis.com/auth/gmail.modify"] });
  assert.deepEqual(scopesNotGranted(declined), ["https://www.googleapis.com/auth/gmail.modify"]);
  assert.equal(displayedIntegrationStatus(declined), "reauthorize");
  assert.equal(providerInstallsStatus([oauth(), declined]), "reauthorize");
});

test("an unknown or complete grant, or no token, flags nothing (#9776)", () => {
  assert.deepEqual(scopesNotGranted(oauth({ missing_scopes: null })), []);
  assert.deepEqual(scopesNotGranted(oauth({ missing_scopes: [] })), []);
  assert.equal(displayedIntegrationStatus(oauth({ missing_scopes: [] })), "connected");
  assert.deepEqual(scopesNotGranted(oauth({ has_access_token: false, missing_scopes: ["a"] })), []);
  assert.equal(displayedIntegrationStatus(oauth({ status: "configured", has_access_token: false, missing_scopes: ["a"] })), "configured");
});

test("a scope the token was not granted never reads as authorized (#9776, UX B1)", () => {
  const declined = oauth({ scopes: "a b c d", missing_scopes: ["b", "d"] });
  assert.deepEqual(authorizedScopes(declined), ["a", "c"]);
  for (const scope of scopesNotGranted(declined)) assert.ok(!authorizedScopes(declined).includes(scope));
  // Unknown or complete grant, or no token: the requested list is shown as it is.
  assert.deepEqual(authorizedScopes(oauth({ scopes: "a,b", missing_scopes: null })), ["a", "b"]);
  assert.deepEqual(authorizedScopes(oauth({ scopes: "a b", missing_scopes: [] })), ["a", "b"]);
  assert.deepEqual(authorizedScopes(oauth({ scopes: "a b", has_access_token: false, missing_scopes: ["b"] })), ["a", "b"]);
  assert.deepEqual(authorizedScopes(oauth({ scopes: null })), []);
});

const apiKey = (connection_test_status: string | null) => ({
  auth_type: "api_key",
  status: "connected",
  scopes: null,
  required_scopes: null,
  has_access_token: false,
  connection_test_status,
});

test("a typed key nobody tested reads Not tested, never Connected (#11837)", () => {
  assert.equal(displayedIntegrationStatus(apiKey("untested")), "untested");
  assert.equal(INTEGRATION_STATUS.untested.label, "Not tested");
});

test("the status follows the last connection test (#11837)", () => {
  assert.equal(displayedIntegrationStatus(apiKey("error")), "error");
  assert.equal(displayedIntegrationStatus(apiKey("ok")), "connected");
  // Rows from before the test result was stored read as before.
  assert.equal(displayedIntegrationStatus(apiKey(null)), "connected");
});

test("a provider card ranks an untested install below trouble and above health (#11837)", () => {
  assert.equal(providerInstallsStatus([apiKey("ok"), apiKey("untested")]), "untested");
  assert.equal(providerInstallsStatus([apiKey("untested"), apiKey("error")]), "error");
});

test("a non-OAuth integration is shown as stored", () => {
  assert.equal(
    displayedIntegrationStatus({ auth_type: "api_key", status: "connected", scopes: null, required_scopes: null, has_access_token: false }),
    "connected",
  );
});
