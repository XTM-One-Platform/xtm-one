/**
 * Unit tests for offering the Slack events Request URL - `yarn test:unit`.
 *
 * The Webhooks card and the creation form offer the URL only once the catalog
 * asks for the Slack app's signing secret, which is what lets the endpoint
 * refuse a delivery nobody can vouch for.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { oauthDefaultWebhookPath, offersSlackEventsUrl } from "./slack-events.ts";

const SIGNING_SECRET = { webhook_secret: "Signing secret (Slack app > Basic Information)" };

test("Slack offers the events Request URL once the catalog asks for the app's signing secret", () => {
  assert.equal(offersSlackEventsUrl({ provider: "slack", extra_fields: SIGNING_SECRET }), true);
  assert.equal(offersSlackEventsUrl({ provider: "slack", extra_fields: { base_url: "Base URL", ...SIGNING_SECRET } }), true);
});

test("no Request URL while there is no signing secret to verify a delivery with", () => {
  assert.equal(offersSlackEventsUrl({ provider: "slack", extra_fields: null }), false);
  assert.equal(offersSlackEventsUrl({ provider: "slack", extra_fields: {} }), false);
  assert.equal(offersSlackEventsUrl({ provider: "slack", extra_fields: { base_url: "Base URL" } }), false);
  assert.equal(offersSlackEventsUrl(undefined), false);
  assert.equal(offersSlackEventsUrl(null), false);
});

test("only the Slack integration uses this endpoint", () => {
  for (const provider of ["slack_bot", "github", "microsoft", "perk"]) {
    assert.equal(offersSlackEventsUrl({ provider, extra_fields: SIGNING_SECRET }), false, provider);
  }
});

test("OAuth defaults: the Slack app gets the events Request URL only with the signing secret field", () => {
  assert.equal(
    oauthDefaultWebhookPath({ provider: "slack", extra_fields: SIGNING_SECRET }),
    "/api/v1/integrations/notifications/slack",
  );
  assert.equal(oauthDefaultWebhookPath({ provider: "slack", extra_fields: null }), null);
  assert.equal(oauthDefaultWebhookPath({ provider: "slack", extra_fields: { base_url: "Base URL" } }), null);
});

test("OAuth defaults: every other provider keeps its provider hook", () => {
  for (const provider of ["github", "hubspot", "slack_bot"]) {
    assert.equal(oauthDefaultWebhookPath({ provider, extra_fields: null }), `/api/hooks/providers/${provider}`, provider);
    assert.equal(oauthDefaultWebhookPath({ provider, extra_fields: SIGNING_SECRET }), `/api/hooks/providers/${provider}`, provider);
  }
});