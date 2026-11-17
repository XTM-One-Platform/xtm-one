/**
 * Unit tests for the OpenAPI credential fields → create payload mapping.
 *
 * Run with `yarn test:unit` (Node's built-in runner, types stripped natively).
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  certificateConfig,
  credentialPayload,
  credentialProblem,
  emptyCredentials,
  prefillCredentials,
} from "./openapi-auth.ts";

const option = (kind: string, fields: Record<string, unknown> = {}) =>
  ({ id: kind, kind, label: kind, scopes: {}, supported: true, integration_auth_type: "oauth", ...fields }) as never;

test("prefill takes the spec's URLs and the scopes to request", () => {
  const device = option("oauth2_device", {
    token_url: "https://idp.test/token",
    device_authorization_url: "https://idp.test/device",
    scopes: { openid: "", email: "", offline_access: "" },
    default_scopes: ["openid", "offline_access"],
  });
  const creds = prefillCredentials(device, emptyCredentials());
  assert.equal(creds.deviceUrl, "https://idp.test/device");
  assert.equal(creds.tokenUrl, "https://idp.test/token");
  assert.equal(creds.scopes, "openid offline_access");
  const all = prefillCredentials(option("oauth2_implicit", { scopes: { read: "", write: "" } }), emptyCredentials());
  assert.equal(all.scopes, "read write");
});

test("each sign-in kind sends its own fields", () => {
  const creds = {
    ...emptyCredentials(),
    clientId: " web ",
    clientSecret: "sec",
    authorizeUrl: "https://idp.test/authorize",
    tokenUrl: "https://idp.test/token",
    deviceUrl: "https://idp.test/device",
    scopes: " read ",
    username: " alice ",
    password: "p:w",
  };
  assert.deepEqual(credentialPayload(option("oauth2_implicit"), creds), {
    client_id: "web",
    authorize_url: "https://idp.test/authorize",
    scopes: "read",
  });
  assert.deepEqual(credentialPayload(option("oauth2_device"), creds), {
    client_id: "web",
    client_secret: "sec",
    authorize_url: "https://idp.test/device",
    token_url: "https://idp.test/token",
    scopes: "read",
  });
  assert.deepEqual(credentialPayload(option("oauth2_password"), creds), {
    client_id: "web",
    client_secret: "sec",
    api_key: "alice:p:w",
    token_url: "https://idp.test/token",
    scopes: "read",
  });
  assert.deepEqual(credentialPayload(option("mutual_tls"), creds), {});
});

test("what each kind still needs", () => {
  const empty = emptyCredentials();
  assert.equal(credentialProblem(option("oauth2_implicit"), empty), "Enter the OAuth client ID.");
  assert.equal(
    credentialProblem(option("oauth2_implicit"), { ...empty, clientId: "c" }),
    "Enter the authorization URL.",
  );
  assert.equal(
    credentialProblem(option("oauth2_device"), { ...empty, clientId: "c", tokenUrl: "t" }),
    "Enter the device authorization and token URLs.",
  );
  assert.equal(credentialProblem(option("oauth2_password"), { ...empty, tokenUrl: "t" }), "Enter the username.");
  assert.equal(
    credentialProblem(option("oauth2_password"), { ...empty, username: "u", tokenUrl: "t", clientSecret: "s" }),
    "Enter the OAuth client ID.",
  );
  assert.equal(credentialProblem(option("oauth2_password"), { ...empty, username: "u", tokenUrl: "t" }), null);
  assert.equal(credentialProblem(option("mutual_tls"), empty), "Paste the client certificate and its private key.");
  assert.equal(
    credentialProblem(option("mutual_tls"), { ...empty, certificate: "CERT", privateKey: "KEY" }),
    null,
  );
});

test("a client certificate goes with any option, whole or not at all", () => {
  const half = { ...emptyCredentials(), apiKey: "k", certificate: "CERT" };
  assert.equal(credentialProblem(option("api_key"), half), "Paste both the client certificate and its private key.");
  assert.deepEqual(certificateConfig(emptyCredentials()), {});
  assert.deepEqual(certificateConfig({ ...half, certificate: " CERT\n", privateKey: "KEY\n", passphrase: "pw" }), {
    client_certificate: "CERT",
    client_private_key: "KEY",
    client_private_key_password: "pw",
  });
});
