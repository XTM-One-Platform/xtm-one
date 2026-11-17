/**
 * Unit tests for the Vertex AI credential - `yarn test:unit`.
 *
 * The form keeps the service account key masked and says whose it is; a region
 * typed beside it travels as the key's `location` member, and anything that is
 * not a JSON object (a vault reference) is sent as typed.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { serviceAccountSummary, vertexAiCredential } from "./vertex-ai-credential.ts";

// The helper never reads the private key: a placeholder that no secret scanner mistakes for one.
const KEY = JSON.stringify({
  type: "service_account",
  project_id: "acme-video",
  private_key: "placeholder-private-key\nsecond-line\n",
  client_email: "veo@acme-video.iam.gserviceaccount.com",
});

test("a service account key says whose it is, anything else says nothing", () => {
  assert.deepEqual(serviceAccountSummary(KEY), {
    clientEmail: "veo@acme-video.iam.gserviceaccount.com",
    projectId: "acme-video",
  });
  assert.equal(serviceAccountSummary('{"type": "authorized_user", "client_email": "a", "project_id": "b"}'), null);
  assert.equal(serviceAccountSummary('{"type": "service_account"'), null);
  assert.equal(serviceAccountSummary("vault://secrets/vertex"), null);
  assert.equal(serviceAccountSummary("[]"), null);
});

test("a region travels as the key's location member", () => {
  const sent = JSON.parse(vertexAiCredential(KEY, " europe-west4 "));
  assert.equal(sent.location, "europe-west4");
  assert.equal(sent.client_email, "veo@acme-video.iam.gserviceaccount.com");
  assert.equal(sent.private_key, "placeholder-private-key\nsecond-line\n");
});

test("without a region, or for what is not a key, the credential is sent as typed", () => {
  assert.equal(vertexAiCredential(`  ${KEY}\n`, ""), KEY);
  assert.equal(vertexAiCredential("vault://secrets/vertex", "us-central1"), "vault://secrets/vertex");
  assert.equal(vertexAiCredential("not json", "us-central1"), "not json");
});
