/**
 * Unit tests for the legacy event matching notice - `yarn test:unit`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  hasPrefixAllowList,
  legacySiblingEvents,
  saveSwitchesToExact,
  unreadLegacyEventType,
} from "./event-matching.ts";

const GITHUB = [
  "issues",
  "issue_comment",
  "pull_request",
  "pull_request_review",
  "pull_request_review_comment",
  "repository",
  "repository_dispatch",
];

test("a bare selection lists the sibling events it matches by prefix", () => {
  assert.deepEqual(legacySiblingEvents("pull_request", GITHUB), [
    "pull_request_review",
    "pull_request_review_comment",
  ]);
  assert.deepEqual(legacySiblingEvents("repository", GITHUB), ["repository_dispatch"]);
});

test("a selection without siblings, or none at all, lists nothing", () => {
  assert.deepEqual(legacySiblingEvents("issues", GITHUB), []);
  assert.deepEqual(legacySiblingEvents("", GITHUB), []);
});

test("keys below the dot are not siblings: exact matching takes them too", () => {
  assert.deepEqual(legacySiblingEvents("issues", ["issues.opened", "issues"]), []);
});

test("a non-empty events list is a prefix allow-list whatever the trigger type", () => {
  // A legacy HubSpot integration-event trigger with events: ["entity"] is
  // widened too: the HubSpot and per-assignment URLs read it.
  assert.equal(hasPrefixAllowList(["entity"]), true);
  assert.equal(hasPrefixAllowList("entity"), true);
  assert.equal(hasPrefixAllowList([]), false);
  assert.equal(hasPrefixAllowList(undefined), false);
  assert.equal(hasPrefixAllowList(null), false);
});

const LEGACY = { provider: "github", event: "pull_request", integration_id: "i-1" };

test("a save that leaves the events alone keeps a legacy trigger on its prefix match", () => {
  // A new name, delay, filter or integration is not a new selection.
  assert.equal(saveSwitchesToExact("integration_event", LEGACY, "integration_event", { ...LEGACY }), false);
  assert.equal(
    saveSwitchesToExact("integration_event", LEGACY, "integration_event", {
      ...LEGACY,
      integration_id: "i-2",
      filter: { scope: { repository: ["a/b"] } },
    }),
    false,
  );
  // Absent, null and empty select nothing alike, as the backend reads them.
  assert.equal(
    saveSwitchesToExact("webhook", { path: "github" }, "webhook", { path: "github", events: [] }),
    false,
  );
  assert.equal(
    saveSwitchesToExact("webhook", { path: "github", events: ["a", "b"] }, "webhook", { events: ["a", "b"], path: "github" }),
    false,
  );
});

test("choosing the events again, or asking for it, switches a legacy trigger", () => {
  const cases: Record<string, unknown>[] = [
    { ...LEGACY, event: "issues" },
    { ...LEGACY, provider: "hubspot" },
    { ...LEGACY, events: ["pull_request.opened"] },
    { ...LEGACY, event_matching: "exact" },
  ];
  for (const next of cases) {
    assert.equal(saveSwitchesToExact("integration_event", LEGACY, "integration_event", next), true, JSON.stringify(next));
  }
  assert.equal(saveSwitchesToExact("webhook", { path: "github" }, "webhook", { path: "hubspot" }), true);
  assert.equal(saveSwitchesToExact("schedule", { cron: "* * * * *" }, "webhook", {}), true);
  assert.equal(saveSwitchesToExact("webhook", {}, "integration_event", {}), true);
});

test("an exact trigger, or one that selects no event, is never switched", () => {
  const exact = { ...LEGACY, event_matching: "exact" };
  assert.equal(saveSwitchesToExact("integration_event", exact, "integration_event", { ...LEGACY, event: "issues" }), false);
  assert.equal(saveSwitchesToExact("webhook", { path: "x" }, "schedule", { cron: "* * * * *" }), false);
  assert.equal(saveSwitchesToExact("webhook", { path: "x" }, "manual", {}), false);
});

test("the old event_type key is unread until the trigger itself matches exactly", () => {
  assert.equal(unreadLegacyEventType({ provider: "microsoft", event_type: "new_email" }), "new_email");
  assert.equal(unreadLegacyEventType({ provider: "microsoft", event_type: " new_email ", event: "" }), "new_email");
  assert.equal(unreadLegacyEventType({ event_type: "new_email", event: "new_email" }), null);
  assert.equal(unreadLegacyEventType({ event_type: "new_email", event_matching: "exact" }), null);
  assert.equal(unreadLegacyEventType({ event_type: "  " }), null);
  assert.equal(unreadLegacyEventType(undefined), null);
});
